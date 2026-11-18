"""One place for every Anthropic (claude -p) call in the Picky system.

Claude is an optional, capped BOOSTER (Charlie 2026-11-18: "If Claude is active, Claude can help Picky with
the advanced analysis. If not, Picky should still be able to exist even though his takes may be less good.
I really don't want to spend a bunch on Claude or make him over-reliant." / "If I cancel Anthropic or log in
after 5 months away it should just all be working perfectly").

The gate (why_not / available / guard) lets a call through only when ALL of these hold:
  - the kill switch is off (PICKY_NO_ANTHROPIC unset or 0);
  - the process is not a live reply path (LIVE_ENTRIES: chat.py, group_chat.py, canary.py, watcher.py). A
    live reply never calls or waits on Claude, not even by hand;
  - the claude binary is installed;
and, for scheduled boosters (every entry except exam tooling under server/eval and PICKY_OPUS_MANUAL=1
hand runs), also:
  - `claude auth status` says logged in with the claude.ai subscription (cached AUTH_TTL_S; a run that comes
    back "please log in" / usage limit marks it down right away), so a cancelled plan just turns boosters off;
  - today's booster use (CT) is under DAILY_CALLS calls and DAILY_MINUTES minutes.
Every caller has a code / local-gemma fallback that runs when the gate says no or the call fails
(test_zero_maintenance.py: BOOSTERS).

Every call goes through run() (or the `run` CLI below for shell scripts):
  - model MODEL (claude-opus-5-5; PICKY_CLAUDE_MODEL overrides),
  - guard() raises AnthropicDisabled (with the reason) before anything spawns,
  - --strict-mcp-config + server/empty.mcp.json (zero MCP servers / channel plugins),
  - ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN stripped (subscription only, never a billed key),
  - one line per call in the daily usage log, USAGE_DIR/<YYYY-MM-DD>.jsonl (private dir;
    PICKY_CLAUDE_USAGE_DIR overrides): caller, model, seconds, result, booster (counts toward the cap).
    A booster refused by the cap logs a "refused: daily cap" line too.

CLI:
  picky_claude.py usage [--days N]       calls and minutes per day and caller
  picky_claude.py run --caller NAME --timeout S [--tools T | --all-tools] [--format text|json]
                  [--cwd DIR] [-- extra claude args]
      prompt on stdin, claude's stdout/stderr passed through, exit = claude's
      (124 timeout, 3 the gate said no: kill switch, live process, no binary/auth, cap).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

MODEL = os.environ.get("PICKY_CLAUDE_MODEL", "claude-opus-5-5")
CLAUDE = os.environ.get("PICKY_CLAUDE_BIN", "/opt/homebrew/bin/claude")
EMPTY_MCP = Path(__file__).resolve().with_name("empty.mcp.json")
USAGE_DIR = Path(os.environ.get("PICKY_CLAUDE_USAGE_DIR", "/Users/Agent/cfb-pickem-private/claude_usage"))
CT = ZoneInfo("America/Chicago")
_STRIP = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
HERE = Path(__file__).resolve().parent
LIVE_ENTRIES = ("chat.py", "group_chat.py", "canary.py", "watcher.py")  # live reply processes: never Claude
UNCAPPED_DIRS = ("eval",)   # server/eval/**: exam graders (the integration lead's tooling), not boosters
DAILY_CALLS = 12            # booster calls per CT day (scheduled use was ~4-19/day before the cap)
DAILY_MINUTES = 45          # booster minutes per CT day
AUTH_TTL_S = 6 * 3600       # how long an auth check (good or bad) is trusted
_AUTH_DOWN = re.compile(r"(?i)invalid api key|/login|not logged in|log ?in again|authenticat|unauthori[sz]ed|"
                        r"oauth|usage limit|credit balance")


class AnthropicDisabled(RuntimeError):
    pass


def disabled() -> bool:
    return os.environ.get("PICKY_NO_ANTHROPIC", "") not in ("", "0")


def manual() -> bool:
    """A human running an offline CLI by hand (no plist sets it): not capped."""
    return os.environ.get("PICKY_OPUS_MANUAL", "") not in ("", "0")


def entry_script(entry=None) -> Path | None:
    a = entry or (sys.argv[0] if sys.argv else "")
    return Path(a).resolve() if a else None


def live_entry(entry=None) -> bool:
    p = entry_script(entry)
    return p is not None and p.name in LIVE_ENTRIES


def capped(entry=None) -> bool:
    """A scheduled booster: everything but exam tooling and hand runs."""
    p = entry_script(entry)
    return not manual() and not (p and any(p.is_relative_to(HERE / d) for d in UNCAPPED_DIRS))


def used_today(now: dt.datetime | None = None) -> tuple[int, float]:
    """(booster calls, booster minutes) logged today (CT)."""
    day = (now or dt.datetime.now(CT)).date().isoformat()
    n, m = 0, 0.0
    try:
        lines = (USAGE_DIR / f"{day}.jsonl").read_text().splitlines()
    except OSError:
        return 0, 0.0
    for line in lines:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("booster"):
            n += 1
            m += float(r.get("secs") or 0) / 60
    return n, m


def _auth_file() -> Path:
    return USAGE_DIR / "auth.json"


def mark_auth(ok: bool, why: str) -> None:
    try:
        USAGE_DIR.mkdir(parents=True, exist_ok=True)
        _auth_file().write_text(json.dumps({"at": time.time(), "ok": ok, "why": why[:200], "bin": CLAUDE}))
    except OSError:
        pass


def auth_problem() -> str:
    """'' when `claude auth status` says logged in with the claude.ai subscription, else why not. Cached."""
    try:
        c = json.loads(_auth_file().read_text())
        if c.get("bin") == CLAUDE and time.time() - float(c["at"]) < AUTH_TTL_S:
            return "" if c.get("ok") else str(c.get("why") or "auth down")
    except (OSError, ValueError, KeyError, TypeError):
        pass
    try:
        p = subprocess.run([CLAUDE, "auth", "status"], capture_output=True, text=True, timeout=30, env=env())
        s = json.loads(p.stdout)
        if not s.get("loggedIn"):
            why = "auth: not logged in"
        elif s.get("authMethod") != "claude.ai":
            why = f"auth: not on the claude.ai subscription ({s.get('authMethod')}), boosters run subscription only"
        else:
            why = ""
    except (OSError, ValueError, subprocess.TimeoutExpired, AttributeError) as e:
        why = f"auth: status check failed ({type(e).__name__})"
    mark_auth(not why, why)
    return why


def why_not(entry=None) -> str:
    """'' when this process may call Claude now, else the reason (cheapest checks first)."""
    if disabled():
        return "PICKY_NO_ANTHROPIC set: claude -p disabled"
    if live_entry(entry):
        return f"live reply process ({entry_script(entry).name}): live replies never call Claude"
    if not os.access(CLAUDE, os.X_OK):
        return f"claude binary missing at {CLAUDE}"
    if not capped(entry):
        return ""
    n, m = used_today()
    if n >= DAILY_CALLS or m >= DAILY_MINUTES:
        return f"booster daily cap hit ({n}/{DAILY_CALLS} calls, {m:.0f}/{DAILY_MINUTES} min today)"
    return auth_problem()


def allowed(entry=None) -> bool:
    """May this process ever call Claude (kill switch off, not a live reply path)?"""
    return not disabled() and not live_entry(entry)


def available(entry=None) -> bool:
    """allowed() and Claude can be used right now (binary, subscription auth, under the daily cap)."""
    return not why_not(entry)


def guard(caller: str | None = None) -> None:
    """Raise before spawning claude -p when the gate says no (a cap refusal is logged)."""
    why = why_not()
    if why:
        if "daily cap" in why:
            log_call(caller or Path(sys.argv[0] or "?").stem, 0.0, "refused: daily cap")
        raise AnthropicDisabled(why)


def env() -> dict:
    e = {k: v for k, v in os.environ.items() if k not in _STRIP}
    e.update({"DISABLE_AUTOUPDATER": "1", "TERM": "dumb",
              "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"})
    return e


def command(tools: str | None = "", output_format: str = "json", schema: dict | None = None,
            extra: tuple | list = ()) -> list[str]:
    """tools None = claude's default tool set (no --tools flag)."""
    cmd = [CLAUDE, "-p", "--model", MODEL]
    if tools is not None:
        cmd += ["--tools", tools]
    cmd += ["--dangerously-skip-permissions", "--strict-mcp-config", "--mcp-config", str(EMPTY_MCP),
            "--output-format", output_format]
    if schema is not None:
        cmd += ["--json-schema", json.dumps(schema)]
    return cmd + list(extra)


def log_call(caller: str, secs: float, result: str, booster: bool = False) -> None:
    """Append one line to today's usage log. Never raises."""
    try:
        now = dt.datetime.now(CT)
        USAGE_DIR.mkdir(parents=True, exist_ok=True)
        rec = {"ts": now.isoformat(timespec="seconds"), "caller": caller, "model": MODEL,
               "secs": round(secs, 1), "result": result}
        if booster:
            rec["booster"] = True
        with open(USAGE_DIR / f"{now.date().isoformat()}.jsonl", "a") as f:
            f.write(json.dumps(rec) + "\n")
    except OSError:
        pass


def run(prompt: str, *, timeout: float, caller: str | None = None, cwd=None, tools: str | None = "",
        output_format: str = "json", schema: dict | None = None,
        extra: tuple | list = ()) -> subprocess.CompletedProcess:
    """One `claude -p` run. Raises AnthropicDisabled (the gate said no), subprocess.TimeoutExpired
    or OSError; a non-zero exit is returned for the caller to judge."""
    caller = caller or Path(sys.argv[0] or "?").stem
    guard(caller)
    booster = capped()
    t0 = time.time()
    try:
        p = subprocess.run(command(tools, output_format, schema, extra), input=prompt, capture_output=True,
                           text=True, timeout=timeout, cwd=cwd, env=env())
    except subprocess.TimeoutExpired:
        log_call(caller, time.time() - t0, "timeout", booster)
        raise
    except OSError as e:
        log_call(caller, time.time() - t0, f"error {type(e).__name__}", booster)
        raise
    log_call(caller, time.time() - t0, "ok" if p.returncode == 0 else f"exit {p.returncode}", booster)
    if p.returncode != 0 and _AUTH_DOWN.search(f"{p.stdout or ''}\n{p.stderr or ''}"[-2000:]):
        mark_auth(False, f"auth: last run said {(p.stderr or p.stdout or '').strip()[-120:]}")
    return p


def usage(days: int = 7, base: Path | None = None) -> list[dict]:
    """Per day (newest first): calls, minutes, failures, and calls/minutes per caller."""
    base = base or USAGE_DIR
    out = []
    for f in sorted(base.glob("*.jsonl"), reverse=True)[:days] if base.exists() else []:
        day = {"date": f.stem, "calls": 0, "minutes": 0.0, "failed": 0, "booster": 0, "booster_minutes": 0.0,
               "by_caller": {}}
        for line in f.read_text().splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            m = float(r.get("secs") or 0) / 60
            day["calls"] += 1
            day["minutes"] += m
            day["failed"] += r.get("result") != "ok"
            if r.get("booster"):
                day["booster"] += 1
                day["booster_minutes"] += m
            c = day["by_caller"].setdefault(str(r.get("caller")), {"calls": 0, "minutes": 0.0})
            c["calls"] += 1
            c["minutes"] += m
        day["minutes"] = round(day["minutes"], 1)
        day["booster_minutes"] = round(day["booster_minutes"], 1)
        for c in day["by_caller"].values():
            c["minutes"] = round(c["minutes"], 1)
        out.append(day)
    return out


def usage_line(d: dict) -> str:
    who = ", ".join(f"{k} {v['calls']}x/{v['minutes']}m" for k, v in sorted(d["by_caller"].items()))
    return (f"{d['date']}: {d['calls']} calls, {d['minutes']} min, {d['failed']} failed; booster {d['booster']}/"
            f"{DAILY_CALLS} calls, {d['booster_minutes']}/{DAILY_MINUTES} min ({who})")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="picky_claude.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    u = sub.add_parser("usage")
    u.add_argument("--days", type=int, default=7)
    r = sub.add_parser("run")
    r.add_argument("--caller", required=True)
    r.add_argument("--timeout", type=float, required=True)
    r.add_argument("--tools", default="")
    r.add_argument("--all-tools", action="store_true", help="claude's default tool set")
    r.add_argument("--format", default="text", choices=("text", "json"))
    r.add_argument("--cwd", default=None)
    r.add_argument("extra", nargs="*")
    a = ap.parse_args(argv)
    if a.cmd == "usage":
        for d in usage(a.days):
            print(usage_line(d))
        why = why_not()
        print(f"booster gate now: {'open' if not why else why}")
        return 0
    try:
        p = run(sys.stdin.read(), timeout=a.timeout, caller=a.caller, cwd=a.cwd,
                tools=None if a.all_tools else a.tools, output_format=a.format, extra=a.extra)
    except AnthropicDisabled as e:
        print(e, file=sys.stderr)
        return 3
    except subprocess.TimeoutExpired:
        print(f"claude -p timed out after {a.timeout:g}s", file=sys.stderr)
        return 124
    sys.stdout.write(p.stdout)
    sys.stderr.write(p.stderr)
    return p.returncode


if __name__ == "__main__":
    sys.exit(main())
