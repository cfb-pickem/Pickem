#!/usr/bin/env python3
"""Picky's insider notes (Charlie, 2026-11-16: "have all the knowledge of Ross Dellenger"),
code first with an Opus summary at most once a day.

Why Opus stays here: the notes are paraphrased Dellenger-style reporting (realignment, revenue
sharing, the CFP, the carousel, commissioners, governance), which takes reading the articles,
and a headline alone can't say what was reported. So:

  - wire (pure code, every tick): insider-bylined / national-desk headlines from news.py's
    cache that match one of chat.INSIDER_TOPICS, with byline, outlet, date and team tags, written
    to the private insider_wire.json (PICKY_INSIDER_WIRE overrides). chat.insider_note adds the
    newest ones (headline only, dated, credited) on top of the notes between Opus refreshes.
  - notes (Opus): one `claude -p` research run rewrites the notes file, at most once a day:
    a tick runs it only when the notes are REFRESH_EVERY_DAYS+ old and the last attempt was
    ATTEMPT_GAP+ ago (about twice a week), never with PICKY_NO_ANTHROPIC=1. `run` forces one.

The notes file is private (cfb-pickem-private/insider_bank.md, PICKY_INSIDER overrides);
chat.insider_note reads it on topic. This script never edits it in place:
  1. copy it to a work file, 2. claude -p edits the work file (web tools + Read/Edit only,
  zero MCP servers, ANTHROPIC_API_KEY stripped), 3. validate (one Read/Edit repair pass on
  errors), 4. only then back up the old file to .prev, stamp 'Last refreshed <today>'
  and swap the new one in atomically.
Anthropic down, PICKY_NO_ANTHROPIC=1, a timeout or a bad file = the last good copy stays,
and chat.insider_note's age check says "as of <date>" (8+ days) or drops it (21+ days); the
wire keeps fresh headlines coming either way.

Usage:
  insider_refresh.py tick     # what com.pickem.insider-refresh runs (every 2 h): wire + Opus if due
  insider_refresh.py wire     # code-only wire refresh
  insider_refresh.py run      # on demand: wire + an Opus notes refresh now
  insider_refresh.py check    # validate the current notes file, no Anthropic
Log: server/logs/insider_refresh.log
"""
from __future__ import annotations

import datetime as dt
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))  # run with python3 -I (no script dir on the path)
import enrich  # noqa: E402
import picky_claude  # noqa: E402
BANK = Path(os.environ.get("PICKY_INSIDER", "/Users/Agent/cfb-pickem-private/insider_bank.md"))
WORK_DIR = Path(os.environ.get("PICKY_INSIDER_WORK", "/Users/Agent/cfb-pickem-private/tmp/insider-work"))
PROMPT = HERE / "insider_refresh_prompt.md"
LOG = HERE / "logs" / "insider_refresh.log"
LOCK = Path("/tmp/picky-insider-refresh.lock")
RESEARCH_TIMEOUT_S, REPAIR_TIMEOUT_S = 2400, 600
WIRE = Path(os.environ.get("PICKY_INSIDER_WIRE", "/Users/Agent/cfb-pickem-private/insider_wire.json"))
STATE = WORK_DIR / "state.json"
REFRESH_EVERY_DAYS = 3                 # notes this old get an Opus refresh (about twice a week)
ATTEMPT_GAP = dt.timedelta(hours=20)   # and never more than one attempt a day
WIRE_MAX_DAYS, WIRE_MAX_ITEMS = 6, 40  # headlines older than 6 days (rankings!) never ride

SECTIONS = ["Realignment", "Revenue sharing / House / NIL", "CFP format and committee",
            "Coaching carousel and hot seats", "Commissioners and power politics",
            "NCAA governance, Congress, CBA"]
NOTE_RE = re.compile(r"^- ((?:[A-Z][a-z]{2} \d{1,2}(?:-(?:[A-Z][a-z]{2} )?\d{1,2})?)|\d{4}(?:-\d{2})?): (.+) "
                     r"\(([^()]+)\) \[checked (\d{4}-\d{2}-\d{2})\]$")
MIN_TOTAL, MAX_TOTAL, MAX_WORDS = 20, 70, 45
MIN_KEEP = 0.6  # a refresh that loses 40%+ of the notes is a truncated file, not an edit


def log(msg: str) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(f"[{dt.datetime.now():%Y-%m-%d %H:%M:%S}] {msg}\n")


def validate(text: str, prev: str | None = None) -> tuple[list[str], int]:
    """(errors, note count). Same rules the prompt gives, plus the shrink guard."""
    lines = text.splitlines()
    errors, notes, seen, cur = [], [], [], None
    if not lines or not lines[0].startswith("# Picky insider notes (PRIVATE"):
        errors.append("line 1 must be the '# Picky insider notes (PRIVATE...' title")
    if not any(ln.startswith("## Sources") for ln in lines):
        errors.append("missing '## Sources' section")
    for ln in lines:
        if ln.startswith("### "):
            cur = ln[4:].strip()
            seen.append(cur)
        elif ln.startswith("## "):
            cur = None
        elif cur and ln.strip():
            m = NOTE_RE.match(ln)
            if not m:
                errors.append(f"bad note line under '{cur}': {ln[:90]}")
                continue
            if re.search(r"[\"“”]", m.group(2)):
                errors.append(f"quotation marks in a note (paraphrase only): {ln[:80]}")
            if len(m.group(2).split()) > MAX_WORDS:
                errors.append(f"note over {MAX_WORDS} words: {ln[:80]}")
            notes.append(cur)
    if seen != SECTIONS:
        errors.append(f"sections must be exactly, in order: {SECTIONS}; got {seen}")
    for s in SECTIONS:
        if notes.count(s) < 2:
            errors.append(f"section '{s}' has {notes.count(s)} notes (need at least 2)")
    if not MIN_TOTAL <= len(notes) <= MAX_TOTAL:
        errors.append(f"{len(notes)} notes; must be {MIN_TOTAL}-{MAX_TOTAL}")
    if prev is not None:
        _, n_prev = validate(prev)
        if n_prev and len(notes) < MIN_KEEP * n_prev:
            errors.append(f"{len(notes)} notes vs {n_prev} before: looks truncated")
    return errors, len(notes)


def stamp(text: str, today: dt.date) -> str:
    """'Last refreshed <today>' under the title: chat.insider_note's as-of date."""
    line = f"Last refreshed {today.isoformat()} (insider_refresh.py, Opus)."
    if re.search(r"^Last refreshed .*$", text, re.M):
        return re.sub(r"^Last refreshed .*$", line, text, count=1, flags=re.M)
    head, _, rest = text.partition("\n")
    return f"{head}\n\n{line}\n{rest}"


def run_claude(prompt: str, tools: str, timeout: int) -> str:
    """Text result of one `claude -p` run (picky_claude: Opus, zero MCP, no API key); raises on any failure."""
    p = picky_claude.run(prompt, timeout=timeout, caller="insider_refresh", cwd=WORK_DIR, tools=tools,
                         output_format="text")
    if p.returncode != 0:
        raise RuntimeError(f"claude exit {p.returncode}: {(p.stderr or p.stdout)[-400:]}")
    return p.stdout


def refresh(today: dt.date | None = None, claude=run_claude) -> bool:
    """One refresh; True when a new file was swapped in. The live file is only ever
    replaced by a validated work copy."""
    today = today or dt.date.today()
    try:
        old = BANK.read_text()
    except OSError as e:
        log(f"FAIL no notes file to refresh ({e}); nothing changed")
        return False
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    work = WORK_DIR / "insider_bank.md"
    work.write_text(old)
    prompt = (f"Today is {today:%A %Y-%m-%d} (America/Chicago).\n\n"
              + PROMPT.read_text().replace("{WORK}", str(work)))
    try:
        out = claude(prompt, "WebSearch,WebFetch,Read,Edit", RESEARCH_TIMEOUT_S)
        errors, n = validate(work.read_text(), old)
        if errors:
            log(f"repair pass for {len(errors)} validator errors")
            claude(f"Fix these validator errors in {work} with Read and Edit only (no research; delete a note "
                   f"rather than guess; keep the format and sections exactly):\n- " + "\n- ".join(errors[:30]),
                   "Read,Edit", REPAIR_TIMEOUT_S)
            errors, n = validate(work.read_text(), old)
    except (picky_claude.AnthropicDisabled, RuntimeError, OSError, subprocess.TimeoutExpired) as e:
        log(f"FAIL {type(e).__name__}: {str(e)[:300]}; kept the last good copy")
        return False
    if errors:
        log(f"FAIL validation ({'; '.join(errors[:5])}); kept the last good copy")
        return False
    new = stamp(work.read_text(), today)
    shutil.copy2(BANK, BANK.with_name(BANK.name + ".prev"))
    tmp = BANK.with_name(BANK.name + ".tmp")
    tmp.write_text(new)
    os.chmod(tmp, 0o600)
    os.replace(tmp, BANK)
    log(f"OK notes={n} {' '.join(ln for ln in out.splitlines() if ln.startswith(('INSIDER', 'BIGGEST')))[:400]}")
    return True


def wire(items: list[dict] | None = None, now: dt.datetime | None = None, out: Path | None = None) -> dict:
    """Code-only insider wire from news.py's cache: insider-bylined or national-desk headlines on
    an insider topic, newest first, with byline, outlet, date and team tags. Atomic write."""
    import chat
    import news
    now = now or dt.datetime.now(news.CT)
    items = list((news.load_cache().get("items") or {}).values()) if items is None else items
    rows = []
    for it in items:
        if not (it.get("insider") or it.get("source") in news.TIER1) or not it.get("title"):
            continue
        heads = [h for h, rx in chat.INSIDER_TOPICS if re.search(rx, it["title"], re.I)]
        try:
            when = dt.datetime.fromisoformat(str(it.get("date")).replace("Z", "+00:00"))
        except ValueError:
            continue
        if not heads or (now - when).days > WIRE_MAX_DAYS:
            continue
        rows.append({"section": heads[0], "sections": heads, "title": it["title"].strip(),
                     "author": it.get("author"), "insider": it.get("insider"), "outlet": it.get("source"),
                     "date": when.isoformat(timespec="minutes"), "teams": it.get("teams") or [],
                     "url": it.get("link")})
    rows.sort(key=lambda r: r["date"], reverse=True)
    data = {"generated_at": now.isoformat(timespec="seconds"), "items": rows[:WIRE_MAX_ITEMS]}
    out = out or WIRE
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1))
    os.chmod(tmp, 0o600)
    os.replace(tmp, out)
    return data


def notes_day() -> dt.date | None:
    """The notes' 'Last refreshed' date (else the file's mtime day)."""
    try:
        text = BANK.read_text()
    except OSError:
        return None
    m = re.search(r"^Last refreshed (\d{4}-\d{2}-\d{2})", text, re.M)
    return dt.date.fromisoformat(m.group(1)) if m else dt.date.fromtimestamp(BANK.stat().st_mtime)


def opus_due(now: dt.datetime) -> bool:
    """At most one Opus attempt a day, only when the notes are REFRESH_EVERY_DAYS+ old."""
    if not enrich.available():
        return False
    day = notes_day()
    if day is not None and (now.date() - day).days < REFRESH_EVERY_DAYS:
        return False
    try:
        last = dt.datetime.fromisoformat(json.loads(STATE.read_text())["attempt_at"])
        return now - last >= ATTEMPT_GAP
    except (OSError, ValueError, KeyError, TypeError):
        return True


def tick(now: dt.datetime | None = None, claude=run_claude, force: bool = False) -> int:
    """Wire every time (code); the Opus notes refresh when due (or forced)."""
    now = now or dt.datetime.now().astimezone()
    try:
        w = wire(now=now)
        log(f"WIRE {len(w['items'])} headlines")
    except Exception as e:  # noqa: BLE001 - the notes refresh doesn't depend on it
        log(f"WIRE FAIL {type(e).__name__}: {str(e)[:200]}")
    if not (force or opus_due(now)):
        return 0
    if not enrich.available():
        log("SKIP Opus refresh: PICKY_NO_ANTHROPIC set; kept the last good copy")
        return 0
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps({"attempt_at": now.isoformat(timespec="seconds")}))
    log(f"START (model {picky_claude.MODEL})")
    return 0 if refresh(now.date(), claude=claude) else 1


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "run"
    if cmd == "check":
        errors, n = validate(BANK.read_text())
        print("\n".join(f"ERROR: {e}" for e in errors) or f"OK notes={n}")
        return 1 if errors else 0
    if cmd == "wire":
        print(f"{len(wire()['items'])} headlines -> {WIRE}")
        return 0
    if cmd not in ("run", "tick"):
        print(__doc__)
        return 2
    with LOCK.open("w") as lk:
        try:
            fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log("SKIP another refresh is running")
            return 0
        return tick(force=cmd == "run")


if __name__ == "__main__":
    sys.exit(main(sys.argv))
