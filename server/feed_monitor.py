"""
Self-healing feed monitor for text-Picky (Charlie, 2026-11-16: "It's your job
to fix if a feed is broken, not mine"). LaunchAgent com.pickem.feed-monitor
runs one pass every 15 min. Rules come from freshness.py (max ages, refresher
LaunchAgent, repair allowlist, tests per feed).

A feed is watched only while its refresher LaunchAgent is loaded (booted out on
purpose = left alone) and once its cache has existed (a feed that was never
installed isn't broken). Broken = cache past max_age ("age" feeds) or the
refresher's last exit code != 0 ("exit" feeds). Escalation per feed:
  check 1 broken   note it
  check 2 broken   kickstart the refresher (bootout + bootstrap if wedged)
  check 3 broken   unattended Claude repair, in the background:
                   claude -p (picky_claude.MODEL, guard(), --strict-mcp-config +
                   empty MCP config, no API key) in a fresh worktree on branch
                   picky-repair/<feed>-<stamp>, 30 min timeout, one at a time
                   (lock file). Auto-merged (fast-forward into main, pushed,
                   refresher kicked) only if every changed file is in that
                   feed's allowlist, none is chat.py / picky_brain.py /
                   group_chat.py, and the feed's unit tests pass. Otherwise the
                   branch is kept for a look and main is untouched.
                   Then the count starts over (kick, then repair again).
  caps             3 repair runs per day in total (CT), 3 per outage per feed.
  Charlie          texted (existing notify path) at most once a day, and only
                   for a feed down 24h+ whose repairs are used up.
Every repair writes one JSON line to cfb-pickem-private/feed_repairs.log
(result, reason, files, duration) and one line to the project memory so the
coordinator knows.

  feed_monitor.py              # one pass
  feed_monitor.py --dry-run    # observe + print the plan, change nothing
  feed_monitor.py status       # state + recent repairs
  feed_monitor.py repair <feed> [--dry-run]   # what the pass spawns
"""

from __future__ import annotations

import datetime as dt
import fcntl
import json
import os
import plistlib
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))  # run with -I

import freshness  # noqa: E402
from freshness import NEVER_REPAIR, Source  # noqa: E402

REPO = Path(os.environ.get("PICKY_REPO", "/Users/Agent/cfb-pickem"))
PRIVATE = freshness.PRIVATE
STATE_FILE = PRIVATE / "feed_monitor_state.json"
REPAIR_LOG = PRIVATE / "feed_repairs.log"
LOCK_FILE = PRIVATE / "feed_repair.lock"
MEMORY = Path(os.environ.get("PICKY_MEMORY_FILE",
                             "/Users/Agent/.claude/projects/-Users-Agent/memory/project_cfb_pickem.md"))
AGENTS_DIR = Path.home() / "Library/LaunchAgents"
PY = str(REPO / "server/.venv/bin/python3")
# same path the Picky watchdog and the LoRA cycle use to text Charlie
NOTIFY = [PY, "-I", "/Users/Agent/picky-marathon/wit_eval.py", "notify"]

DAILY_CAP = 3            # repair runs per CT day, all feeds together
OUTAGE_CAP = 3           # repair runs per feed per outage
TELL_AFTER = 24 * 3600   # down this long with repairs used up -> one text to Charlie
REPAIR_TIMEOUT = 30 * 60
TEST_TIMEOUT = 10 * 60
CLAUDE_TOOLS = "Bash,Read,Edit,Write,Grep,Glob"


def log(msg: str) -> None:
    print(f"{dt.datetime.now():%Y-%m-%d %H:%M:%S} {msg}", flush=True)


def sh(*cmd, cwd=None, timeout=120, env=None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(list(cmd), capture_output=True, text=True, cwd=cwd, timeout=timeout, env=env)
    except (OSError, subprocess.TimeoutExpired) as e:
        return subprocess.CompletedProcess(list(cmd), 124, "", str(e))


def remember(text: str) -> None:
    """One line in the project memory: the coordinator reads it, Charlie doesn't."""
    try:
        with MEMORY.open("a") as f:
            f.write(f"\n- {dt.datetime.now():%Y-%m-%d %H:%M} CT feed-monitor: {text}\n")
    except OSError as e:
        log(f"memory append failed: {e}")


def ct_day(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, freshness.CT).date().isoformat()


# -----------------------------------------------------------------------------
# observe
# -----------------------------------------------------------------------------

def agent_info(label: str) -> dict | None:
    """None if the LaunchAgent isn't loaded; else {'exit': last exit code|None}."""
    r = sh("/bin/launchctl", "print", f"gui/{os.getuid()}/{label}")
    if r.returncode != 0:
        return None
    code = None
    for ln in r.stdout.splitlines():
        ln = ln.strip()
        if ln.startswith("last exit code ="):
            try:
                code = int(ln.split("=", 1)[1].split()[0])
            except (IndexError, ValueError):
                pass
            break
    return {"exit": code}


def watched() -> list[Source]:
    return [s for s in freshness.SOURCES.values() if s.monitor and s.agent]


def observe(now: float, info=agent_info) -> dict[str, dict]:
    """{feed: {ok, missing, why}} for every watched feed whose refresher is loaded."""
    out = {}
    for s in watched():
        ai = info(s.agent)
        if ai is None:
            continue
        v = freshness.check(s.name, now=now, refresh=False)
        bad_exit = ai.get("exit") not in (None, 0)
        if s.monitor == "exit":
            ok, why = not bad_exit, f"{s.agent} last exit code {ai.get('exit')}"
        else:
            ok = v.status == "fresh"
            why = (f"cache {v.status}" + (f", last refreshed {v.as_of}" if v.as_of != "unknown" else "")
                   + f" (max age {freshness.max_age(s.name, now) / 3600:g} h)"
                   + (f"; {s.agent} last exit code {ai['exit']}" if bad_exit else ""))
        out[s.name] = {"ok": ok, "missing": v.status == "missing", "why": why}
    return out


# -----------------------------------------------------------------------------
# decide (pure: tests drive it with fake observations)
# -----------------------------------------------------------------------------

def repairable(s: Source) -> bool:
    return bool(s.files and s.tests)


def decide(obs: dict[str, dict], state: dict, now: float) -> tuple[list[tuple[str, str]], dict]:
    st = json.loads(json.dumps(state or {}))
    feeds = st.setdefault("feeds", {})
    runs = [t for t in st.get("repairs", []) if ct_day(t) == ct_day(now)]
    st["repairs"] = runs
    acts: list[tuple[str, str]] = []
    down_long = []
    for name, o in obs.items():
        f = feeds.setdefault(name, {})
        if o["ok"]:
            if f.get("bad", 0) >= 2 or f.get("repairs"):
                acts.append(("remember", f"{name} feed recovered after ~{int((now - f.get('since', now)) // 60)} min."))
            feeds[name] = {"seen": True}
            continue
        if o.get("missing") and not f.get("seen"):
            continue  # never installed/produced yet: not broken
        f["bad"] = f.get("bad", 0) + 1
        f.setdefault("since", now)
        f["why"] = o["why"]
        s = freshness.source(name)
        if f["bad"] == 2:
            acts.append(("kick", name))
        elif f["bad"] >= 3 and repairable(s) and len(runs) < DAILY_CAP and f.get("repairs", 0) < OUTAGE_CAP:
            acts.append(("repair", name))
            runs.append(now)
            f["repairs"] = f.get("repairs", 0) + 1
            f["bad"] = 1  # start over: the next checks kick, then repair again if needed
        used_up = not repairable(s) or f.get("repairs", 0) >= OUTAGE_CAP or len(runs) >= DAILY_CAP
        if now - f["since"] >= TELL_AFTER and used_up:
            down_long.append(name)
    if down_long and st.get("told") != ct_day(now):
        names = ", ".join(sorted(down_long))
        acts.append(("tell", f"Heads up: Picky's {names} feed{'s have' if len(down_long) > 1 else ' has'} been "
                             "down 24h+ and my automatic fixes didn't work. I'll keep trying, but it may need a look."))
    return acts, st


# -----------------------------------------------------------------------------
# act
# -----------------------------------------------------------------------------

def kick(label: str) -> bool:
    dom = f"gui/{os.getuid()}"
    r = sh("/bin/launchctl", "kickstart", "-k", f"{dom}/{label}")
    if r.returncode != 0:  # wedged: reload it
        plist = AGENTS_DIR / f"{label}.plist"
        sh("/bin/launchctl", "bootout", f"{dom}/{label}")
        r = sh("/bin/launchctl", "bootstrap", dom, str(plist))
        if r.returncode != 0:  # bootstrap right after bootout can fail once with error 5
            time.sleep(2)
            r = sh("/bin/launchctl", "bootstrap", dom, str(plist))
    log(f"kick {label} rc={r.returncode} {r.stderr.strip()[-120:]}")
    return r.returncode == 0


def notify(text: str) -> bool:
    r = sh(*NOTIFY, text, timeout=90)
    log(f"told Charlie rc={r.returncode} {r.stdout.strip()[-80:]}")
    return r.returncode == 0


def spawn_repair(name: str) -> None:
    (HERE / "logs").mkdir(exist_ok=True)
    logf = open(HERE / "logs" / "feed_repair.log", "a")
    subprocess.Popen([PY, "-I", str(HERE / "feed_monitor.py"), "repair", name], cwd=HERE,
                     stdout=logf, stderr=subprocess.STDOUT, start_new_session=True)
    log(f"repair {name} spawned")


def run_pass(dry: bool = False, now: float | None = None) -> int:
    now = time.time() if now is None else now
    try:
        state = json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        state = {}
    obs = observe(now)
    acts, new = decide(obs, state, now)
    bad = {k: v["why"] for k, v in obs.items() if not v["ok"]}
    if dry:
        print(json.dumps({"watched": sorted(obs), "broken": bad, "plan": acts}, indent=1))
        return 0
    if acts or bad:
        log(f"broken={bad} -> {acts}")
    for kind, arg in acts:
        if kind == "kick":
            kick(freshness.source(arg).agent)
        elif kind == "repair":
            spawn_repair(arg)
        elif kind == "remember":
            remember(arg)
        elif kind == "tell":
            if notify(arg):
                new["told"] = ct_day(now)
    STATE_FILE.write_text(json.dumps(new, indent=1))
    return 0


# -----------------------------------------------------------------------------
# repair run
# -----------------------------------------------------------------------------

def gate(changed: list[str], s: Source) -> str | None:
    """None if the diff may auto-merge, else why not."""
    if not changed:
        return "no changes"
    brain = [f for f in changed if f in NEVER_REPAIR]
    if brain:
        return f"touches brain files {brain}"
    outside = [f for f in changed if f not in s.files]
    if outside:
        return f"outside the {s.name} allowlist: {outside}"
    return None


def _agent_cmd(label: str) -> tuple[str, str]:
    """(refresher command line, its log tail) from the installed plist."""
    try:
        pl = plistlib.loads((AGENTS_DIR / f"{label}.plist").read_bytes())
    except (OSError, ValueError):
        return "(plist not found)", ""
    tail = []
    for k in ("StandardErrorPath", "StandardOutPath"):
        p = pl.get(k)
        if p and p not in [t[0] for t in tail]:
            try:
                tail.append((p, "\n".join(Path(p).read_text(errors="replace").splitlines()[-40:])))
            except OSError:
                pass
    return " ".join(pl.get("ProgramArguments", [])), "\n\n".join(f"--- {p} (last 40 lines)\n{t}" for p, t in tail)


def prompt_for(s: Source, why: str, cmd: str, tail: str) -> str:
    return f"""You are fixing one broken data feed of text-Picky (a college football pick'em bot), unattended.
You're in a throwaway git worktree of the repo on its own branch; the server code is in server/.

Feed: {s.name}
Symptom: {why}
Refresher (LaunchAgent {s.agent}): {cmd}
{tail or '(no log output found)'}

Find the cause and make the SMALLEST fix.
- You may edit ONLY these files: {', '.join(s.files)}. Any other change (including chat.py,
  picky_brain.py, group_chat.py) gets the whole fix thrown away.
- Then run the tests from server/: {' '.join(f'{PY} -I {t}' for t in s.tests)}. They must pass.
- You may run the refresher once from this worktree to confirm (it writes into this worktree's
  server/cache). Be gentle with rate-limited APIs (CFBD: one run at most).
- Don't git commit, push, merge or rebase; don't run launchctl; don't pull, create, delete or load
  any Ollama model; nothing destructive; never print secrets or tokens.
- If the cause is outside these files (the upstream site is down, credentials expired, a
  LaunchAgent problem), change nothing and say so.
End with one line: FIXED: <what you changed> or NO_FIX: <why>."""


def call_claude(prompt: str, cwd: Path, timeout: int = REPAIR_TIMEOUT) -> tuple[int, str]:
    """One unattended claude -p run in cwd via picky_claude (Opus, guard(), zero MCP,
    no API key): (exit code, last of its output)."""
    import picky_claude
    try:
        p = picky_claude.run(prompt, timeout=timeout, caller="feed_monitor-repair", cwd=cwd, tools=CLAUDE_TOOLS)
    except subprocess.TimeoutExpired:
        return 124, "timed out"
    try:
        out = str(json.loads(p.stdout).get("result", ""))
    except ValueError:
        out = p.stdout or p.stderr
    return p.returncode, out[-600:]


def run_tests(s: Source, wt: Path) -> str | None:
    """None if every test of the feed passes in the worktree, else why not."""
    env = dict(os.environ, PICKY_NO_ANTHROPIC="1", PICKY_FRESHNESS_KICK="0",
               PICKY_CFBD_DB=os.environ.get("PICKY_CFBD_DB", str(REPO / "server/cache/cfbd.sqlite")))
    for t in s.tests:
        if not (wt / "server" / t).exists():
            return f"missing test {t}"
        r = sh(PY, "-I", t, cwd=wt / "server", timeout=TEST_TIMEOUT, env=env)
        if r.returncode != 0:
            return f"{t} failed: {(r.stdout + r.stderr).strip()[-300:]}"
    return None


def _git(*a, cwd=None) -> subprocess.CompletedProcess:
    return sh("git", *a, cwd=cwd or REPO, timeout=300)


def changed_files(wt: Path) -> list[str]:
    r = _git("diff", "--name-only", "main", "HEAD", cwd=wt)
    return sorted(set(r.stdout.split()))


def log_repair(rec: dict) -> None:
    with REPAIR_LOG.open("a") as f:
        f.write(json.dumps(rec) + "\n")


def repair(name: str, why: str = "", claude=call_claude, tests=run_tests, push: bool = True,
           kick_after=kick, dry: bool = False) -> dict:
    """One repair run for a feed. Returns (and logs) the result record."""
    s = freshness.source(name)
    t0 = time.time()
    stamp = dt.datetime.now(freshness.CT).strftime("%Y%m%d-%H%M%S-%f")
    branch = f"picky-repair/{name}-{stamp}"
    wt = Path(os.environ.get("PICKY_REPAIR_WT_DIR", "/private/tmp")) / f"picky-repair-{name}-{stamp}"
    rec = {"ts": dt.datetime.now(freshness.CT).isoformat(timespec="seconds"), "feed": name, "branch": branch}
    if not why:
        try:
            why = json.loads(STATE_FILE.read_text())["feeds"][name].get("why", "")
        except (OSError, ValueError, KeyError):
            why = ""
    cmd, tail = _agent_cmd(s.agent) if s.agent else ("", "")
    prompt = prompt_for(s, why or "stale or failing", cmd, tail)
    if dry:
        print(prompt)
        return {"result": "dry-run"}
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    lockf = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        rec.update(result="skipped", reason="another repair is running")
        log_repair(rec)
        return rec
    try:
        r = _git("worktree", "add", str(wt), "-b", branch, "main")
        if r.returncode != 0:
            rec.update(result="failed", reason=f"worktree: {r.stderr.strip()[-200:]}")
            return rec
        c0 = time.time()
        try:
            rc, out = claude(prompt, wt)
        except Exception as e:  # noqa: BLE001 - kill switch, missing binary: logged, never raised
            rc, out = 1, f"{type(e).__name__}: {e}"
        rec["claude_s"] = round(time.time() - c0)
        rec["claude_said"] = out.strip().splitlines()[-1][:300] if out.strip() else ""
        if rc != 0:
            rec.update(result="failed", reason=f"claude exit {rc}")
            return rec
        _git("add", "-A", cwd=wt)
        if _git("diff", "--cached", "--quiet", cwd=wt).returncode != 0:
            _git("commit", "-q", "-m", f"feed repair ({name}): automatic fix by the feed monitor\n\n"
                 f"{rec['claude_said']}\n\nCo-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>", cwd=wt)
        if _git("rebase", "main", cwd=wt).returncode != 0:  # main moved meanwhile
            _git("rebase", "--abort", cwd=wt)
            rec.update(result="rejected", reason="rebase onto main conflicted")
            return rec
        rec["files"] = changed_files(wt)
        why_not = gate(rec["files"], s) or tests(s, wt)
        if why_not:
            rec.update(result="rejected" if rec["files"] else "no-fix", reason=why_not)
            return rec
        if _git("symbolic-ref", "--short", "HEAD").stdout.strip() != "main":
            rec.update(result="rejected", reason="main isn't checked out in the repo")
            return rec
        m = _git("merge", "--ff-only", branch)
        if m.returncode != 0:
            rec.update(result="rejected", reason=f"merge: {m.stderr.strip()[-200:]}")
            return rec
        rec["result"] = "merged"
        rec["sha"] = _git("rev-parse", "--short", "HEAD").stdout.strip()
        if push:
            p = _git("push", "-q", "origin", "main")
            rec["pushed"] = p.returncode == 0
        if s.agent:
            kick_after(s.agent)
        return rec
    finally:
        rec["duration_s"] = round(time.time() - t0)
        if wt.exists():
            _git("worktree", "remove", "--force", str(wt))
        if rec.get("result") in ("merged", "failed", "no-fix"):
            _git("branch", "-D", branch)  # rejected branches stay for a look
        if rec.get("result") != "skipped":
            log_repair(rec)
            remember(f"repair run for the {name} feed: {rec.get('result')}"
                     + (f" ({rec['reason']})" if rec.get("reason") else "")
                     + (f", merged {rec['sha']}" if rec.get("sha") else "") + f", {rec['duration_s']} s.")
        fcntl.flock(lockf, fcntl.LOCK_UN)
        lockf.close()


def status() -> int:
    try:
        print(STATE_FILE.read_text())
    except OSError:
        print("{}")
    try:
        for ln in REPAIR_LOG.read_text().splitlines()[-10:]:
            print(ln)
    except OSError:
        pass
    return 0


def main(argv: list[str]) -> int:
    if argv[:1] == ["status"]:
        return status()
    if argv[:1] == ["repair"] and len(argv) >= 2:
        rec = repair(argv[1], dry="--dry-run" in argv)
        print(json.dumps(rec))
        return 0
    return run_pass(dry="--dry-run" in argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
