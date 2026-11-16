"""One place for every Anthropic (claude -p) call in the Picky system.

All offline writers, graders and curators use Opus 5.5. Picky never needs
Anthropic at runtime: PICKY_NO_ANTHROPIC=1 makes every call fail fast so the
local fallbacks / last good copies run (kill-switch tests set it).

Every call goes through run() (or the `run` CLI below for shell scripts):
  - model MODEL (claude-opus-5-5; PICKY_CLAUDE_MODEL overrides),
  - guard(): PICKY_NO_ANTHROPIC=1 raises AnthropicDisabled before anything spawns,
  - --strict-mcp-config + server/empty.mcp.json (zero MCP servers / channel plugins),
  - ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN stripped (subscription only, never a billed key),
  - one line per call in the daily usage log, USAGE_DIR/<YYYY-MM-DD>.jsonl (private dir;
    PICKY_CLAUDE_USAGE_DIR overrides): caller, model, seconds, result.

CLI:
  picky_claude.py usage [--days N]       calls and minutes per day and caller
  picky_claude.py run --caller NAME --timeout S [--tools T | --all-tools] [--format text|json]
                  [--cwd DIR] [-- extra claude args]
      prompt on stdin, claude's stdout/stderr passed through, exit = claude's
      (124 timeout, 3 kill switch on).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
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


class AnthropicDisabled(RuntimeError):
    pass


def disabled() -> bool:
    return os.environ.get("PICKY_NO_ANTHROPIC", "") not in ("", "0")


def guard() -> None:
    """Raise before spawning claude -p when the kill switch is on."""
    if disabled():
        raise AnthropicDisabled("PICKY_NO_ANTHROPIC set: claude -p disabled")


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


def log_call(caller: str, secs: float, result: str) -> None:
    """Append one line to today's usage log. Never raises."""
    try:
        now = dt.datetime.now(CT)
        USAGE_DIR.mkdir(parents=True, exist_ok=True)
        rec = {"ts": now.isoformat(timespec="seconds"), "caller": caller, "model": MODEL,
               "secs": round(secs, 1), "result": result}
        with open(USAGE_DIR / f"{now.date().isoformat()}.jsonl", "a") as f:
            f.write(json.dumps(rec) + "\n")
    except OSError:
        pass


def run(prompt: str, *, timeout: float, caller: str | None = None, cwd=None, tools: str | None = "",
        output_format: str = "json", schema: dict | None = None,
        extra: tuple | list = ()) -> subprocess.CompletedProcess:
    """One `claude -p` run. Raises AnthropicDisabled (kill switch), subprocess.TimeoutExpired
    or OSError; a non-zero exit is returned for the caller to judge."""
    guard()
    caller = caller or Path(sys.argv[0] or "?").stem
    t0 = time.time()
    try:
        p = subprocess.run(command(tools, output_format, schema, extra), input=prompt, capture_output=True,
                           text=True, timeout=timeout, cwd=cwd, env=env())
    except subprocess.TimeoutExpired:
        log_call(caller, time.time() - t0, "timeout")
        raise
    except OSError as e:
        log_call(caller, time.time() - t0, f"error {type(e).__name__}")
        raise
    log_call(caller, time.time() - t0, "ok" if p.returncode == 0 else f"exit {p.returncode}")
    return p


def usage(days: int = 7, base: Path | None = None) -> list[dict]:
    """Per day (newest first): calls, minutes, failures, and calls/minutes per caller."""
    base = base or USAGE_DIR
    out = []
    for f in sorted(base.glob("*.jsonl"), reverse=True)[:days] if base.exists() else []:
        day = {"date": f.stem, "calls": 0, "minutes": 0.0, "failed": 0, "by_caller": {}}
        for line in f.read_text().splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            m = float(r.get("secs") or 0) / 60
            day["calls"] += 1
            day["minutes"] += m
            day["failed"] += r.get("result") != "ok"
            c = day["by_caller"].setdefault(str(r.get("caller")), {"calls": 0, "minutes": 0.0})
            c["calls"] += 1
            c["minutes"] += m
        day["minutes"] = round(day["minutes"], 1)
        for c in day["by_caller"].values():
            c["minutes"] = round(c["minutes"], 1)
        out.append(day)
    return out


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
            who = ", ".join(f"{k} {v['calls']}x/{v['minutes']}m" for k, v in sorted(d["by_caller"].items()))
            print(f"{d['date']}: {d['calls']} calls, {d['minutes']} min, {d['failed']} failed ({who})")
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
