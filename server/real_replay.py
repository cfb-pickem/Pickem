"""
Real-traffic regression replay: the league's real group asks, replayed through a side sidecar's router
+ brain, graded in code. Fixtures and reports hold member text, so they live ONLY in the private dir;
this file is code only.

  real_replay.py build  [--date YYYY-MM-DD] [--since HH:MM]   real group asks -> fixtures (labels kept)
  real_replay.py run    [--ref HEAD] [--port 8807] [--ids f1,f2] [--original]
  real_replay.py nightly [--date YYYY-MM-DD]                   run + queue newly broken fixtures
  real_replay.py report [<report.json>]                        print a report's markdown

build    reads picky_exchanges.jsonl (mode group, a mapped team, not side/exam traffic) and writes
         <private>/eval/replay/fixtures-<date>.json. Labels (`expect`) are added by hand and survive
         a rebuild. Fixture: {id, date, ts, team, q, original: {reply, lane, secs}, expect: {lanes,
         answers, must_not, max_words, expected_fail, why}}.
run      inside the shared exam lock (/usr/bin/lockf /tmp/picky-exam.lock; re-execs itself under it),
         from a throwaway detached worktree of <ref> (cache/.venv symlinked from the main tree) on a
         side sidecar (eval/genius/side.sh: every state file in a temp dir, PICKY_NO_ANTHROPIC=1,
         PICKY_GROUP_EXAM=1 so it yields to members via live_flag). Each ask goes to POST /group with
         the group secret and X-Picky-Test, in its own fresh "exam-replay-*" chat. --original grades
         the replies logged live instead (no model, no lock).
checks   lane (expected lane set), privacy (eval/checks.check_privacy: no hidden pick or submission
         status before lock), answers (every sub-question regex matches), facts (record claims true
         per eval/truth + every quoted pool line matches the cache + must_not patterns), length (group
         <= group_chat.MAX_CHARS chars, optional max_words), no_error.
status   PASS / FAIL, or for fixtures labeled expected_fail (a queued flub the nightly loop owns):
         XFAIL (still failing) / XPASS (the fix round flipped it). pass rate = (PASS + XPASS) / n.
nightly  after flub_loop's fix round: run, then any fixture that passed in the previous report and
         fails now (re-run once to rule out a dice roll) goes to the flag queue (flag_flub, source
         "replay") so the NEXT night's harvest picks it up. Summary -> flubs/replay-<date>.json.

Never texts anyone. Reports: <private>/eval/replay/reports/replay-<stamp>.{json,md} (0600).
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import flag_flub  # noqa: E402  (light: queue + chat hash)

CT = ZoneInfo("America/Chicago")
PRIVATE = Path(os.environ.get("PICKY_PRIVATE_DIR", "/Users/Agent/cfb-pickem-private"))
REPLAY_DIR = Path(os.environ.get("PICKY_REPLAY_DIR", PRIVATE / "eval" / "replay"))
REPORTS = REPLAY_DIR / "reports"
EXCHANGES = Path(os.environ.get("PICKY_EXCHANGE_LOG", PRIVATE / "picky_exchanges.jsonl"))
MEMBERS_FILE = Path(os.environ.get("PICKY_GROUP_MEMBERS", PRIVATE / "picky_group_members.json"))
FLUB_DIR = Path(os.environ.get("PICKY_FLUB_DIR", PRIVATE / "flubs"))
REPO = Path(os.environ.get("PICKY_REPO", "/Users/Agent/cfb-pickem"))
PY = "/Users/Agent/cfb-pickem/server/.venv/bin/python3"
LOCK = "/tmp/picky-exam.lock"
LOCK_ENV = "PICKY_REPLAY_LOCKED"
GROUP_MAX_CHARS = 480  # group_chat.MAX_CHARS (not imported: group_chat pulls in the whole brain)
DM_MAX_CHARS = 1500
TRIGGER = re.compile(r"@picky\b", re.I)  # the sidecar wants a literal @Picky (the watcher adds it for mentions)


# ---------------------------------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------------------------------

def fixtures_path(date: str) -> Path:
    return REPLAY_DIR / f"fixtures-{date}.json"


def write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.replace(tmp, path)


def build_fixtures(date: str, since: str = "00:00", rows: list[dict] | None = None,
                   old: list[dict] | None = None) -> list[dict]:
    """The day's real group asks (from `since` CT) as fixtures; `old` fixtures' labels are kept."""
    day = dt.date.fromisoformat(date)
    hh, mm = (int(x) for x in since.split(":"))
    start = dt.datetime(day.year, day.month, day.day, hh, mm, tzinfo=CT).timestamp()
    end = dt.datetime(day.year, day.month, day.day, tzinfo=CT).timestamp() + 86400
    if rows is None:
        rows = []
        for ln in EXCHANGES.read_text().splitlines() if EXCHANGES.exists() else []:
            try:
                rows.append(json.loads(ln))
            except ValueError:
                continue
    keep = {f["q"]: f for f in old or []}
    out, n = [], 0
    for r in sorted(rows, key=lambda r: r.get("ts") or 0):
        if r.get("mode") != "group" or not r.get("team") or r.get("side"):
            continue
        if not start <= float(r.get("ts") or 0) < end or not str(r.get("q") or "").strip():
            continue
        n += 1
        f = {"id": f"{date.replace('-', '')}-g{n:02d}", "date": date, "ts": r["ts"], "team": r["team"],
             "q": r["q"], "original": {"reply": r.get("a"), "lane": r.get("lane"), "secs": r.get("secs")},
             "expect": {}}
        if r["q"] in keep:
            f["expect"] = keep[r["q"]].get("expect") or {}
        out.append(f)
    return out


def load_fixtures(paths: list[Path] | None = None) -> list[dict]:
    out = []
    for p in paths or sorted(REPLAY_DIR.glob("fixtures-*.json")):
        out += json.loads(p.read_text())
    return out


# ---------------------------------------------------------------------------------------------------
# grading (code only)
# ---------------------------------------------------------------------------------------------------

def _checks():
    sys.path.insert(0, str(HERE / "eval"))
    import checks  # noqa: E402  (imports eval/truth -> chat; only when grading)
    return checks


def line_claims(reply: str, games: list[dict]) -> list[str]:
    """Every '<pool team> +/-N' in the reply that isn't that side's pool line (line = home's number)."""
    bad = []
    for g in games:
        try:
            line = float(g["line"])
        except (TypeError, ValueError, KeyError):
            continue
        for team, want in ((g["away"], -line), (g["home"], line)):
            for name in {team, team.replace("'", ""), re.sub(r"\bState\b", "St", team)}:
                for m in re.finditer(rf"(?<!\w){re.escape(name)}\s*\(?\s*([+-]\s?\d{{1,2}}(?:\.\d)?)(?!\d|\.\d)",
                                     reply, re.I):
                    got = float(m.group(1).replace(" ", ""))
                    if abs(got - want) > 1e-6:
                        bad.append(f"{team} {m.group(1)} (pool {want:+g})")
    return sorted(set(bad))


def grade(fx: dict, reply: str | None, lane: str | None, truth, dm: bool = False) -> list[tuple[str, bool, str]]:
    """[(check, ok, detail)] for one replayed (or logged) reply."""
    exp = fx.get("expect") or {}
    reply = reply or ""
    out = []
    ok = bool(reply.strip()) and not reply.startswith("[ERROR") and not reply.startswith("[HTTP")
    out.append(("no_error", ok, "" if ok else (reply[:80] or "no reply")))
    lanes = exp.get("lanes") or []
    if lanes:
        got = lane or "-"
        hit = any(got == want or got.split(":")[0] == want for want in lanes)
        out.append(("lane", hit, "" if hit else f"got {got}, want {'|'.join(lanes)}"))
    ck = _checks()
    _n, ok, d = ck.check_privacy(reply, truth, fx.get("team"))
    out.append(("privacy", ok, d))
    miss = [a for a in exp.get("answers") or [] if not re.search(a, reply, re.I)]
    if exp.get("answers"):
        out.append(("answers", not miss, ("missing: " + "; ".join(miss))[:200] if miss else ""))
    bad = []
    _n, ok, d = ck.check_record_claims(reply, truth, fx.get("team"))
    if not ok:
        bad.append(f"record {d}")
    lc = line_claims(reply, truth.games)
    if lc:
        bad.append("line " + ", ".join(lc))
    bad += [f"must_not /{p}/" for p in exp.get("must_not") or [] if re.search(p, reply, re.I)]
    out.append(("facts", not bad, "; ".join(bad)[:200]))
    cap = DM_MAX_CHARS if dm else GROUP_MAX_CHARS
    words = len(reply.split())
    long_ = len(reply) > cap or (exp.get("max_words") and words > exp["max_words"])
    out.append(("length", not long_, f"{len(reply)} chars, {words} words" if long_ else ""))
    return out


def status(fx: dict, results: list[tuple[str, bool, str]]) -> str:
    if not results or not next((ok for n, ok, _d in results if n == "no_error"), True):
        return "ERROR"
    passed = all(ok for _n, ok, _d in results)
    if (fx.get("expect") or {}).get("expected_fail"):
        return "XPASS" if passed else "XFAIL"
    return "PASS" if passed else "FAIL"


def summarize(rows: list[dict]) -> dict:
    by = {}
    for r in rows:
        by[r["status"]] = by.get(r["status"], 0) + 1
    n = len(rows)
    good = by.get("PASS", 0) + by.get("XPASS", 0)
    secs = sorted(r["secs"] for r in rows if r.get("secs") is not None)
    return {"n": n, "pass": good, "pass_rate": round(good / n, 3) if n else 0.0, "by_status": by,
            "p50_s": secs[len(secs) // 2] if secs else None, "max_s": secs[-1] if secs else None}


def newly_broken(prev: dict | None, cur: dict) -> list[dict]:
    """Fixtures that passed (PASS/XPASS) in `prev` and fail (FAIL/XFAIL) in `cur`."""
    if not prev:
        return []
    was = {r["id"]: r["status"] for r in prev.get("rows") or []}
    return [r for r in cur.get("rows") or [] if was.get(r["id"]) in ("PASS", "XPASS")
            and r["status"] in ("FAIL", "XFAIL")]


def flipped(prev: dict | None, cur: dict) -> list[dict]:
    """Expected-fail fixtures the fix round flipped to pass since `prev` (or ever, with no prev)."""
    was = {r["id"]: r["status"] for r in (prev or {}).get("rows") or []}
    return [r for r in cur.get("rows") or [] if r["status"] == "XPASS" and was.get(r["id"]) != "XPASS"]


# ---------------------------------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------------------------------

def _clip(s, n=110) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[:n - 1] + "…"


def markdown(rep: dict) -> str:
    s = rep["summary"]
    bs = s["by_status"]
    L = [f"# Real-traffic replay {rep['stamp']} ({rep['mode']}, {rep.get('sha') or '-'})", "",
         f"- pass rate {s['pass']}/{s['n']} ({s['pass_rate']:.0%}): PASS {bs.get('PASS', 0)}, XPASS "
         f"{bs.get('XPASS', 0)} (expected-fail now passing), XFAIL {bs.get('XFAIL', 0)} (expected-fail, still "
         f"failing), FAIL {bs.get('FAIL', 0)}, ERROR {bs.get('ERROR', 0)}",
         f"- reply time p50 {s['p50_s']}s, max {s['max_s']}s" if s.get("p50_s") is not None else "- reply time n/a",
         ""]
    for r in rep["rows"]:
        bad = [f"{n}: {d}" if d else n for n, ok, d in r["checks"] if not ok]
        L.append(f"- **{r['status']}** {r['id']} [{r.get('lane') or '-'}"
                 + (f", {r['secs']}s" if r.get("secs") is not None else "") + f"] \"{_clip(r['q'], 80)}\"")
        L.append(f"  - reply: \"{_clip(r.get('reply'), 160)}\"")
        if bad:
            L.append("  - failed: " + _clip("; ".join(bad), 220))
        if r.get("expected_fail"):
            L.append(f"  - expected-fail: {r['expected_fail']}")
    for k in ("flipped", "newly_broken", "queued"):
        if rep.get(k):
            L.append(f"\n{k.replace('_', ' ')}: " + ", ".join(rep[k]))
    return "\n".join(L) + "\n"


def save_report(rep: dict) -> Path:
    p = REPORTS / f"replay-{rep['stamp']}.json"
    write_private(p, json.dumps(rep, indent=1))
    write_private(p.with_suffix(".md"), markdown(rep))
    return p


def previous_report(mode: str = "replay", before: str | None = None) -> dict | None:
    for p in sorted(REPORTS.glob("replay-*.json"), reverse=True):
        try:
            rep = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        if rep.get("mode") == mode and (before is None or rep.get("stamp", "") < before):
            return rep
    return None


# ---------------------------------------------------------------------------------------------------
# replay (side sidecar inside the exam lock)
# ---------------------------------------------------------------------------------------------------

def _keychain(service: str) -> str:
    return subprocess.run(["security", "find-generic-password", "-a", "drake", "-s", service, "-w"],
                          capture_output=True, text=True).stdout.strip()


def _relock(argv: list[str]) -> int:
    """Re-run this command inside the shared exam lock (waits up to 4 h for it)."""
    env = dict(os.environ, **{LOCK_ENV: "1"})
    return subprocess.call(["/usr/bin/lockf", "-t", "14400", LOCK, PY, "-I", str(Path(__file__).resolve())] + argv,
                           env=env)


def make_worktree(ref: str) -> Path:
    wt = Path(f"/private/tmp/picky-wt-replay-{os.getpid()}")
    subprocess.run(["git", "-C", str(REPO), "worktree", "add", "-q", "--detach", str(wt), ref], check=True,
                   capture_output=True)
    for d in ("cache", ".venv"):
        (wt / "server" / d).symlink_to(REPO / "server" / d)
    return wt


def drop_worktree(wt: Path) -> None:
    subprocess.run(["git", "-C", str(REPO), "worktree", "remove", "--force", str(wt)], capture_output=True)


def ask_text(q: str) -> str:
    """What the watcher hands the sidecar for a group text: a bare 'Picky ...' was an iMessage mention."""
    return q if TRIGGER.search(q) else "@Picky " + q


def ask_all(fixtures: list[dict], port: int, tag: str) -> dict[str, dict]:
    """POST each fixture's ask to the side sidecar on `port`: {id: {reply, lane, secs}}."""
    import httpx
    members = json.loads(MEMBERS_FILE.read_text())["members"]
    handle = {m["team"]: m["handles"][0] for m in members if m.get("handles")}
    hdr = {"X-Picky-Group-Secret": _keychain("CFB_PICKEM__PICKY_GROUP_SECRET"),
           "X-Picky-Test": _keychain("CFB_PICKEM__TEST_TOKEN")}
    out = {}
    with httpx.Client(timeout=httpx.Timeout(connect=10, read=600, write=10, pool=10)) as cl:
        for fx in fixtures:
            q = ask_text(fx["q"])
            t0 = time.time()
            try:
                j = cl.post(f"http://127.0.0.1:{port}/group", headers=hdr,
                            json={"handle": handle.get(fx["team"], ""), "text": q, "dm": False,
                                  "chat": f"exam-replay-{tag}-{fx['id']}"}).json()
                reply, lane = j.get("reply"), j.get("lane")
            except Exception as e:  # noqa: BLE001
                reply, lane = f"[ERROR {e!r}]", None
            out[fx["id"]] = {"reply": reply, "lane": lane, "secs": round(time.time() - t0, 1)}
            print(f"  {fx['id']} {out[fx['id']]['secs']:5.1f}s {lane or '-':<14} {_clip(reply, 90)}", flush=True)
    return out


def replay(fixtures: list[dict], ref: str = "HEAD", port: int = 8807) -> tuple[dict[str, dict], str]:
    """Side sidecar from a worktree of `ref`; every fixture asked once. Caller holds the exam lock."""
    import httpx
    wt = make_worktree(ref)
    sha = subprocess.run(["git", "-C", str(wt), "rev-parse", "--short", "HEAD"], capture_output=True,
                         text=True).stdout.strip()
    run_dir = Path(f"/private/tmp/picky-replay-run/{os.getpid()}")
    run_dir.mkdir(parents=True, exist_ok=True)
    side = subprocess.Popen(["/bin/zsh", str(wt / "server/eval/genius/side.sh"), str(wt), str(port), str(run_dir)],
                            stdout=open(run_dir / "side.log", "w"), stderr=subprocess.STDOUT,
                            start_new_session=True)
    try:
        for _ in range(120):
            try:
                if httpx.get(f"http://127.0.0.1:{port}/health", timeout=2).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            if side.poll() is not None:
                raise RuntimeError(f"side sidecar exited rc={side.returncode}; see {run_dir}/side.log")
            time.sleep(2)
        else:
            raise RuntimeError("side sidecar never came up")
        return ask_all(fixtures, port, str(os.getpid())), sha
    finally:
        try:
            os.killpg(side.pid, signal.SIGTERM)
            side.wait(15)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            side.kill()
        drop_worktree(wt)


async def _truth():
    sys.path.insert(0, str(HERE / "eval"))
    from truth import Truth  # noqa: E402
    return await Truth().load()


def grade_all(fixtures: list[dict], answers: dict[str, dict], truth, mode: str, sha: str = "") -> dict:
    rows = []
    for fx in fixtures:
        a = answers.get(fx["id"]) or {}
        res = grade(fx, a.get("reply"), a.get("lane"), truth)
        rows.append({"id": fx["id"], "team": fx.get("team"), "q": fx["q"], "reply": a.get("reply"),
                     "lane": a.get("lane"), "secs": a.get("secs"), "checks": res, "status": status(fx, res),
                     "expected_fail": (fx.get("expect") or {}).get("expected_fail")})
    stamp = dt.datetime.now(CT).strftime("%Y%m%d-%H%M%S")
    return {"stamp": stamp, "mode": mode, "sha": sha, "rows": rows, "summary": summarize(rows)}


def cmd_run(a, argv: list[str]) -> dict | None:
    fixtures = load_fixtures()
    if a.ids:
        want = set(a.ids.split(","))
        fixtures = [f for f in fixtures if f["id"] in want]
    if not fixtures:
        print("no fixtures")
        return None
    truth = asyncio.run(_truth())
    if a.original:
        answers = {f["id"]: dict(f.get("original") or {}) for f in fixtures}
        rep = grade_all(fixtures, answers, truth, "original")
    else:
        if os.environ.get(LOCK_ENV) != "1":
            raise SystemExit(_relock(argv))
        answers, sha = replay(fixtures, a.ref, a.port)
        rep = grade_all(fixtures, answers, truth, "replay", sha)
    path = save_report(rep)
    print(markdown(rep))
    print(f"report: {path}")
    return rep


def queue_regression(row: dict, fx: dict) -> str:
    bad = "; ".join(f"{n}: {d}" if d else n for n, ok, d in row["checks"] if not ok)
    ctx = [{"ts": round(time.time()), "mode": "group", "team": fx.get("team"), "q": fx["q"], "a": row.get("reply") or "",
            "lane": row.get("lane"), "secs": row.get("secs")}]
    return flag_flub.log_flub(text=row.get("reply") or "", note=(
        f"Replay regression {fx['id']} (passed last night, fails now): {bad}. "
        f"Fixture expects lanes {fx.get('expect', {}).get('lanes')}; {fx.get('expect', {}).get('why', '')}").strip(),
        source="replay", context=ctx)


def already_queued(fid: str) -> bool:
    try:
        lines = flag_flub.queue_path().read_text().splitlines()
    except OSError:
        return False
    for ln in lines:
        try:
            r = json.loads(ln)
        except ValueError:
            continue
        if r.get("source") == "replay" and not r.get("consumed") and f"Replay regression {fid} " in (r.get("note") or ""):
            return True
    return False


def cmd_nightly(a, argv: list[str]) -> int:
    if os.environ.get(LOCK_ENV) != "1":
        return _relock(argv)
    date = a.date or dt.datetime.now(CT).date().isoformat()
    fixtures = load_fixtures()
    summary = {"date": date, "status": "ok"}
    try:
        if not fixtures:
            summary["status"] = "no fixtures"
            return 0
        prev = previous_report("replay")
        truth = asyncio.run(_truth())
        answers, sha = replay(fixtures, "HEAD", a.port)
        rep = grade_all(fixtures, answers, truth, "replay", sha)
        broke = newly_broken(prev, rep)
        if broke:  # a 12B model rolls dice: a regression must fail twice before it's queued
            by_id = {f["id"]: f for f in fixtures}
            again, _ = replay([by_id[r["id"]] for r in broke], "HEAD", a.port)
            re_rows = grade_all([by_id[r["id"]] for r in broke], again, truth, "replay", sha)["rows"]
            still = {r["id"] for r in re_rows if r["status"] in ("FAIL", "XFAIL")}
            broke = [r for r in broke if r["id"] in still]
        rep["newly_broken"] = [r["id"] for r in broke]
        rep["flipped"] = [r["id"] for r in flipped(prev, rep)]
        by_id = {f["id"]: f for f in fixtures}
        rep["queued"] = [queue_regression(r, by_id[r["id"]]) for r in broke if not already_queued(r["id"])]
        path = save_report(rep)
        summary.update({"report": str(path), "sha": sha, **rep["summary"], "newly_broken": rep["newly_broken"],
                        "flipped": rep["flipped"], "queued": rep["queued"]})
        print(markdown(rep))
        return 0
    except Exception as e:  # noqa: BLE001
        summary.update(status="error", error=repr(e)[:300])
        print(f"replay nightly failed: {e!r}", file=sys.stderr)
        return 1
    finally:
        write_private(FLUB_DIR / f"replay-{date}.json", json.dumps(summary, indent=1))


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    ap = argparse.ArgumentParser(prog="real_replay.py")
    ap.add_argument("cmd", choices=["build", "run", "nightly", "report"])
    ap.add_argument("path", nargs="?", default="")
    ap.add_argument("--date", default=None)
    ap.add_argument("--since", default="00:00")
    ap.add_argument("--ref", default="HEAD")
    ap.add_argument("--port", type=int, default=8807)
    ap.add_argument("--ids", default="")
    ap.add_argument("--original", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd in ("run", "nightly"):
        import freeze  # game day: no exams Sat 10:00-24:00 CT (review R9)
        freeze.exam_guard("real_replay")
    if a.cmd == "build":
        date = a.date or dt.datetime.now(CT).date().isoformat()
        p = fixtures_path(date)
        old = json.loads(p.read_text()) if p.exists() else []
        fx = build_fixtures(date, a.since, old=old)
        write_private(p, json.dumps(fx, indent=1, ensure_ascii=False))
        print(f"{len(fx)} fixtures -> {p} ({sum(bool(f['expect']) for f in fx)} labeled)")
        return 0
    if a.cmd == "run":
        rep = cmd_run(a, argv)
        return 0 if rep is not None else 1
    if a.cmd == "nightly":
        return cmd_nightly(a, argv)
    p = Path(a.path) if a.path else max(REPORTS.glob("replay-*.json"), default=None)
    if not p:
        print("no reports")
        return 1
    print(markdown(json.loads(Path(p).read_text())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
