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
  - model model_for(caller): CALLER_MODELS puts the lighter boosters on Haiku / Sonnet (Charlie 2026-11-19);
    every other caller (flub loop, improve-nightly, exam judges) and anything run from server/eval stays on
    MODEL (claude-opus-5-5); PICKY_CLAUDE_MODEL overrides every caller,
  - guard() raises AnthropicDisabled (with the reason) before anything spawns,
  - --strict-mcp-config + server/empty.mcp.json (zero MCP servers / channel plugins),
  - ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN stripped (subscription only, never a billed key),
  - one line per call in the daily usage log, USAGE_DIR/<YYYY-MM-DD>.jsonl (private dir;
    PICKY_CLAUDE_USAGE_DIR overrides): caller, model used, seconds, result, booster (counts toward the cap).
    A booster refused by the cap logs a "refused: daily cap" line too.

CLI:
  picky_claude.py usage [--days N]       calls and minutes per day and caller
  picky_claude.py live on|off|status [--model opus|sonnet] [--until ISO] [--mode coach|direct|raw] [--raw]
                  [--sample N]   the live-reply switch (below); `on` = coach mode (every reply, or 1 in N with
      --sample) unless --mode / --raw say otherwise; it sets raw false unless raw mode is asked for
  picky_claude.py run --caller NAME --timeout S [--tools T | --all-tools] [--format text|json]
                  [--cwd DIR] [-- extra claude args]
      prompt on stdin, claude's stdout/stderr passed through, exit = claude's
      (124 timeout, 3 the gate said no: kill switch, live process, no binary/auth, cap).
"""
from __future__ import annotations

import argparse
import asyncio
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
HAIKU, SONNET = "claude-haiku-5-5", "claude-sonnet-5-5"
# Lighter boosters off Opus (Charlie 2026-11-19), picked by a side-by-side vs Opus on the same inputs
# (docs/picky-tried-ledger.md). Not listed = MODEL: flub_loop / flub_fix, improve-nightly, every exam judge.
# slate_intel and insider_refresh stayed Opus: on Sonnet they missed QB statuses, firings and the newest CFP ranking.
CALLER_MODELS = {
    "league_rules": HAIKU,
    "news_qa": SONNET, "banter": SONNET, "burn_book": SONNET, "game_cards": SONNET, "dossier-take": SONNET,
    "dossier": SONNET, "hot-takes": SONNET, "feed_monitor-repair": SONNET,
}
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


def exam_entry(entry=None) -> bool:
    """Exam tooling (server/eval/**)."""
    p = entry_script(entry)
    return bool(p and any(p.is_relative_to(HERE / d) for d in UNCAPPED_DIRS))


def capped(entry=None) -> bool:
    """A scheduled booster: everything but exam tooling and hand runs."""
    return not manual() and not exam_entry(entry)


def model_for(caller: str | None = None, entry=None) -> str:
    """The model this caller runs on: PICKY_CLAUDE_MODEL for everyone if set, else MODEL from server/eval
    (judge scores stay comparable), else CALLER_MODELS.get(caller, MODEL)."""
    override = os.environ.get("PICKY_CLAUDE_MODEL")
    if override:
        return override
    if caller is None or exam_entry(entry):
        return MODEL
    return CALLER_MODELS.get(caller, MODEL)


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
            caller = caller or Path(sys.argv[0] or "?").stem
            log_call(caller, 0.0, "refused: daily cap", model=model_for(caller))
        raise AnthropicDisabled(why)


def env() -> dict:
    e = {k: v for k, v in os.environ.items() if k not in _STRIP}
    e.update({"DISABLE_AUTOUPDATER": "1", "TERM": "dumb",
              "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"})
    return e


def command(tools: str | None = "", output_format: str = "json", schema: dict | None = None,
            extra: tuple | list = (), model: str | None = None) -> list[str]:
    """tools None = claude's default tool set (no --tools flag); model None = MODEL."""
    cmd = [CLAUDE, "-p", "--model", model or MODEL]
    if tools is not None:
        cmd += ["--tools", tools]
    cmd += ["--dangerously-skip-permissions", "--strict-mcp-config", "--mcp-config", str(EMPTY_MCP),
            "--output-format", output_format]
    if schema is not None:
        cmd += ["--json-schema", json.dumps(schema)]
    return cmd + list(extra)


def log_call(caller: str, secs: float, result: str, booster: bool = False, model: str | None = None,
             live: bool = False) -> None:
    """Append one line to today's usage log. Never raises."""
    try:
        now = dt.datetime.now(CT)
        USAGE_DIR.mkdir(parents=True, exist_ok=True)
        rec = {"ts": now.isoformat(timespec="seconds"), "caller": caller, "model": model or MODEL,
               "secs": round(secs, 1), "result": result}
        if booster:
            rec["booster"] = True
        if live:
            rec["live"] = True
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
    model = model_for(caller)
    t0 = time.time()
    try:
        p = subprocess.run(command(tools, output_format, schema, extra, model), input=prompt, capture_output=True,
                           text=True, timeout=timeout, cwd=cwd, env=env())
    except subprocess.TimeoutExpired:
        log_call(caller, time.time() - t0, "timeout", booster, model)
        raise
    except OSError as e:
        log_call(caller, time.time() - t0, f"error {type(e).__name__}", booster, model)
        raise
    log_call(caller, time.time() - t0, "ok" if p.returncode == 0 else f"exit {p.returncode}", booster, model)
    if p.returncode != 0 and _AUTH_DOWN.search(f"{p.stdout or ''}\n{p.stderr or ''}"[-2000:]):
        mark_auth(False, f"auth: last run said {(p.stderr or p.stdout or '').strip()[-120:]}")
    return p


# -----------------------------------------------------------------------------------------------------------
# LIVE REPLIES ON CLAUDE: a TEMPORARY, switch-gated exception to ledger RULEs M1/M15 (Charlie by iMessage
# 2026-11-19 ~7:51 PM CT, "Do it now": live replies on his Claude subscription, Opus, "just for right now").
# Off unless LIVE_SWITCH says {"on": true} (re-read on every call, mtime-cached: flip it without a restart);
# "until" (ISO time) turns it off by itself. When on, chat._ollama_stream and picky_brain._json_call (member
# texts only: ollama_gate not low) send the same system prompt + messages here instead of gemma; every
# guard / filter / fact check still runs on the output. Any failure (error, LIVE_TIMEOUT, empty, cap) = None,
# and the caller falls back to gemma for that reply. A usage-limit / login message or LIVE_MAX_FAILS failures
# in a row flips the switch off (off_reason in the file). Off by hand: `picky_claude.py live off`.
# MODES (key "mode"; a file without one is from before coach mode: "raw" if raw is true, else "direct"):
#   coach   (Charlie 2026-11-23, what `live on` means now) Picky drafts on gemma through every lane and guard,
#           then ONE Opus review fixes a bad draft (group_chat._coach, picky_coach.py). "sample": N reviews 1 in N.
#   direct  the 11-19 behavior above: a member text's generation goes to Claude instead of gemma.
#   raw     raw Opus mode (group_chat._raw_answer): no router, lanes or guards.
# -----------------------------------------------------------------------------------------------------------
LIVE_SWITCH = Path(os.environ.get("PICKY_LIVE_CLAUDE", "/Users/Agent/cfb-pickem-private/picky_live_claude.json"))
LIVE_TIMEOUT = 45.0
LIVE_DAILY_CALLS = 600      # live calls per CT day (a full-lane reply is ~2: brief + draft); over it = gemma
LIVE_MAX_FAILS = 3          # failures in a row that flip the switch off
LIVE_MODELS = {"opus": MODEL, "sonnet": SONNET, "haiku": HAIKU}
LIVE_MODES = ("coach", "direct", "raw")
LIVE_CWD = Path(os.environ.get("TMPDIR", "/tmp")) / "picky-live-claude"   # empty dir: no project CLAUDE.md
_LIMIT = re.compile(r"(?i)usage limit|limit reached|hit your limit|out of (extra )?usage|rate.?limit|"
                    r"/login|not logged in|log ?in again|invalid api key|credit balance|unauthori[sz]ed")
_live = {"mtime": None, "cfg": {}, "fails": 0}


def live_config() -> dict:
    """The switch file, re-read when its mtime changes. {} when missing / unreadable."""
    try:
        m = LIVE_SWITCH.stat().st_mtime_ns
    except OSError:
        _live.update(mtime=None, cfg={})
        return {}
    if m != _live["mtime"]:
        try:
            cfg = json.loads(LIVE_SWITCH.read_text())
            _live.update(mtime=m, cfg=cfg if isinstance(cfg, dict) else {})
        except (OSError, ValueError):
            _live.update(mtime=m, cfg={})
    return _live["cfg"]


def live_count(now: dt.datetime | None = None) -> int:
    day = (now or dt.datetime.now(CT)).date().isoformat()
    try:
        return sum('"live": true' in line for line in (USAGE_DIR / f"{day}.jsonl").read_text().splitlines())
    except OSError:
        return 0


def live_why_not(now: dt.datetime | None = None) -> str:
    """'' when a live reply may go to Claude now, else why not (switch off = 'off')."""
    cfg = live_config()
    if not cfg.get("on"):
        return "off"
    if disabled():
        return "PICKY_NO_ANTHROPIC set"
    until = cfg.get("until")
    if until:
        try:
            u = dt.datetime.fromisoformat(str(until))
            if (now or dt.datetime.now(CT)) >= (u if u.tzinfo else u.replace(tzinfo=CT)):
                return f"expired ({until})"
        except ValueError:
            return f"bad until {until!r}"
    if not os.access(CLAUDE, os.X_OK):
        return f"claude binary missing at {CLAUDE}"
    if live_count(now) >= LIVE_DAILY_CALLS:
        return f"live daily cap hit ({LIVE_DAILY_CALLS} calls)"
    return ""


def live_on() -> bool:
    return not live_why_not()


def live_mode(cfg: dict | None = None) -> str:
    """coach | direct | raw (see MODES above). Says nothing about on/off: callers check live_on() too."""
    cfg = live_config() if cfg is None else cfg
    mode = str(cfg.get("mode") or "")
    if mode in LIVE_MODES:
        return mode
    return "raw" if cfg.get("raw") else "direct"


def live_sample(cfg: dict | None = None) -> int:
    """Coach spot-check rate: review 1 reply in N (1 = every reply)."""
    cfg = live_config() if cfg is None else cfg
    try:
        return max(1, int(cfg.get("sample") or 1))
    except (TypeError, ValueError):
        return 1


def live_set(on: bool, why: str = "", model: str | None = None, until: str | None = None,
             raw: bool | None = None, mode: str | None = None, sample: int | None = None) -> dict:
    """Write the switch (keeps model / until / raw / mode / sample unless given). raw and mode stay in step:
    mode "raw" is raw true, any other mode raw false; raw alone (older callers) sets mode raw / leaves a
    non-raw mode alone."""
    cfg = dict(live_config())
    cfg["on"] = bool(on)
    if mode is not None:
        if mode not in LIVE_MODES:
            raise ValueError(f"mode must be one of {LIVE_MODES}")
        cfg["mode"], cfg["raw"] = mode, mode == "raw"
    elif raw is not None:
        cfg["raw"] = bool(raw)
        if raw:
            cfg["mode"] = "raw"
        elif cfg.get("mode") == "raw":
            cfg["mode"] = "direct"
    if sample is not None:
        cfg["sample"] = max(1, int(sample))
    if model:
        cfg["model"] = model
    if until is not None:
        cfg["until"] = until or None
    cfg.setdefault("model", "opus")
    cfg.setdefault("until", None)
    cfg["changed_at"] = dt.datetime.now(CT).isoformat(timespec="seconds")
    cfg["why"] = why
    try:
        LIVE_SWITCH.parent.mkdir(parents=True, exist_ok=True)
        tmp = LIVE_SWITCH.with_suffix(".tmp")
        tmp.write_text(json.dumps(cfg, indent=1) + "\n")
        tmp.replace(LIVE_SWITCH)
    except OSError:
        pass
    _live["fails"] = 0
    return cfg


def live_prompt(messages: list[dict]) -> tuple[str, str]:
    """(system prompt, prompt) from an Ollama-style message list: system turns -> --system-prompt, the rest
    as a transcript ending with the latest user turn."""
    system = "\n\n".join(str(m.get("content") or "") for m in messages if m.get("role") == "system")
    turns = [m for m in messages if m.get("role") != "system"]
    if len(turns) == 1:
        return system, str(turns[0].get("content") or "")
    body = "\n\n".join(f'<turn role="{m.get("role")}">\n{m.get("content") or ""}\n</turn>' for m in turns)
    return system, (body + "\n\nYou are the assistant. Write only your reply to the last user turn: the text "
                    "itself, no tags, no preamble.")


def live_command(system: str, model: str, effort: str, schema: dict | None = None) -> list[str]:
    cmd = [CLAUDE, "-p", "--model", model, "--tools", "", "--strict-mcp-config", "--mcp-config", str(EMPTY_MCP),
           "--setting-sources", "", "--no-session-persistence", "--system-prompt", system]
    if effort:
        cmd += ["--effort", effort]
    if schema is not None:
        return cmd + ["--output-format", "json", "--json-schema", json.dumps(schema)]
    return cmd + ["--output-format", "text"]


def _live_fail(kind: str, why: str, model: str, secs: float, limit: bool = False) -> None:
    log_call(f"live-{kind}", secs, why[:120], model=model, live=True)
    _live["fails"] += 1
    if limit:
        live_set(False, f"auto-off: usage limit / login ({why[:160]})")
    elif _live["fails"] >= LIVE_MAX_FAILS:
        live_set(False, f"auto-off: {LIVE_MAX_FAILS} failures in a row (last: {why[:160]})")
    print(f"picky_claude live {kind}: {why[:200]} -> gemma", file=sys.stderr)


async def live_reply(messages: list[dict], kind: str = "reply", schema: dict | None = None,
                     timeout: float | None = None, effort: str | None = None) -> str | None:
    """The reply text (schema: the structured output as a JSON string) from Claude, or None: the switch is
    off, or the call failed (logged, counted toward auto-off) and the caller uses gemma. Never raises."""
    if live_why_not():
        return None
    cfg = live_config()
    model = LIVE_MODELS.get(str(cfg.get("model") or "opus"), str(cfg.get("model") or MODEL))
    system, prompt = live_prompt(messages)
    t0 = time.time()
    try:
        LIVE_CWD.mkdir(parents=True, exist_ok=True)
        proc = await asyncio.create_subprocess_exec(
            *live_command(system, model, effort or str(cfg.get("effort", "low") or ""), schema),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            cwd=str(LIVE_CWD), env=env())
        try:
            out, err = await asyncio.wait_for(proc.communicate(prompt.encode()), timeout or LIVE_TIMEOUT)
        except (asyncio.TimeoutError, asyncio.CancelledError) as e:
            proc.kill()
            await proc.wait()
            if isinstance(e, asyncio.CancelledError):
                log_call(f"live-{kind}", time.time() - t0, "cancelled", model=model, live=True)
                raise
            _live_fail(kind, f"timeout {timeout or LIVE_TIMEOUT:g}s", model, time.time() - t0)
            return None
    except OSError as e:
        _live_fail(kind, f"error {type(e).__name__}: {e}", model, time.time() - t0)
        return None
    secs = time.time() - t0
    text, errs = out.decode(errors="replace").strip(), err.decode(errors="replace").strip()
    if proc.returncode != 0:
        tail = (text + "\n" + errs)[-400:]
        _live_fail(kind, f"exit {proc.returncode}: {tail.strip()}", model, secs, limit=bool(_LIMIT.search(tail)))
        return None
    if schema is not None:
        try:
            d = json.loads(text)
            res = d.get("structured_output")
            if d.get("is_error") or res is None:
                why = str(d.get("result") or d.get("subtype") or "no structured_output")
                _live_fail(kind, why, model, secs, limit=bool(_LIMIT.search(why)))
                return None
            text = json.dumps(res)
        except (ValueError, AttributeError) as e:
            _live_fail(kind, f"bad json ({type(e).__name__})", model, secs)
            return None
    elif len(text) < 200 and _LIMIT.search(text):
        _live_fail(kind, text, model, secs, limit=True)
        return None
    if not text:
        _live_fail(kind, "empty output", model, secs)
        return None
    _live["fails"] = 0
    log_call(f"live-{kind}", secs, "ok", model=model, live=True)
    return text


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
    lv = sub.add_parser("live", help="the live-reply Claude switch (temporary, see LIVE_SWITCH)")
    lv.add_argument("action", choices=("on", "off", "status"))
    lv.add_argument("--model", default=None, help="opus | sonnet | haiku")
    lv.add_argument("--until", default=None, help="ISO time it turns itself off ('' = never)")
    lv.add_argument("--raw", action="store_true", help="raw Opus mode (= --mode raw; `on` without it sets raw false)")
    lv.add_argument("--mode", default=None, choices=LIVE_MODES,
                    help="coach (default for `on`): gemma drafts, Opus reviews | direct: Claude drafts | raw: Opus only")
    lv.add_argument("--sample", type=int, default=None, help="coach mode: review 1 reply in N (default 1 = every reply)")
    a = ap.parse_args(argv)
    if a.cmd == "live":
        if a.action == "on":
            mode = a.mode or ("raw" if a.raw else "coach")
            live_set(True, f"by hand (on, {mode})", a.model, a.until, mode=mode, sample=a.sample or 1)
        elif a.action == "off":
            live_set(False, "by hand (off)", a.model, a.until)
        cfg, why = live_config(), live_why_not()
        mode, n = live_mode(cfg), live_sample(cfg)
        print(json.dumps(cfg))
        print(f"live replies on Claude: {'ON (' + str(cfg.get('model')) + ')' if not why else 'off: ' + why}; "
              f"mode {mode}" + (f" (reviews 1 in {n})" if mode == "coach" and n > 1 else "")
              + f"; raw {'true' if cfg.get('raw') else 'false'}; {live_count()} live calls today")
        return 0
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
