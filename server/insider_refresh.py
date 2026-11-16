#!/usr/bin/env python3
"""Refresh Picky's insider notes (Charlie, 2026-11-16: "have all the knowledge of Ross
Dellenger") with one offline `claude -p` (Opus via picky_claude) research run.

The notes file is private (cfb-pickem-private/insider_bank.md, PICKY_INSIDER overrides);
chat.insider_note reads it on topic. This script never edits it in place:
  1. copy it to a work file, 2. claude -p edits the work file (web tools + Read/Edit only,
  zero MCP servers, ANTHROPIC_API_KEY stripped), 3. validate (one Read/Edit repair pass on
  errors), 4. only then back up the old file to .prev, stamp 'Last refreshed <today>'
  and swap the new one in atomically.
Anthropic down, PICKY_NO_ANTHROPIC=1, a timeout or a bad file = the last good copy stays,
and chat.insider_note's age check says "as of <date>" (8+ days) or drops it (21+ days).

Usage:
  insider_refresh.py run      # what com.pickem.insider-refresh runs (Sun + Wed)
  insider_refresh.py check    # validate the current notes file, no Anthropic
Log: server/logs/insider_refresh.log
"""
from __future__ import annotations

import datetime as dt
import fcntl
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))  # run with python3 -I (no script dir on the path)
import picky_claude  # noqa: E402
BANK = Path(os.environ.get("PICKY_INSIDER", "/Users/Agent/cfb-pickem-private/insider_bank.md"))
WORK_DIR = Path(os.environ.get("PICKY_INSIDER_WORK", "/Users/Agent/cfb-pickem-private/tmp/insider-work"))
PROMPT = HERE / "insider_refresh_prompt.md"
EMPTY_MCP = HERE / "empty.mcp.json"
LOG = HERE / "logs" / "insider_refresh.log"
LOCK = Path("/tmp/picky-insider-refresh.lock")
CLAUDE = "/opt/homebrew/bin/claude"
RESEARCH_TIMEOUT_S, REPAIR_TIMEOUT_S = 2400, 600

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
    """Text result of one `claude -p` run; raises on any failure."""
    picky_claude.guard()
    env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
    env.update({"DISABLE_AUTOUPDATER": "1", "TERM": "dumb",
                "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"})
    cmd = [CLAUDE, "-p", "--model", picky_claude.MODEL, "--tools", tools,
           "--dangerously-skip-permissions", "--strict-mcp-config", "--mcp-config", str(EMPTY_MCP),
           "--output-format", "text"]
    p = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=timeout, cwd=WORK_DIR, env=env)
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


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "run"
    if cmd == "check":
        errors, n = validate(BANK.read_text())
        print("\n".join(f"ERROR: {e}" for e in errors) or f"OK notes={n}")
        return 1 if errors else 0
    if cmd != "run":
        print(__doc__)
        return 2
    with LOCK.open("w") as lk:
        try:
            fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log("SKIP another refresh is running")
            return 0
        log(f"START (model {picky_claude.MODEL})")
        return 0 if refresh() else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
