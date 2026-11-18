"""
Cross-process "a live text is being answered" flag.

Ollama has one slot and chat.py's priority gate (GATE) only sees its own
process, so a side sidecar (lane exams, the scorecard) or eval/run.py could
put a live member's text behind an exam item. This flag is shared through
the filesystem:

  live processes (com.pickem.chat :8766, com.pickem.picky-group :8768)
      begin() / end() around every member reply: one file per reply in DIR
  side processes (PICKY_SIDE=1, or a group sidecar on any port but 8768)
      await wait_clear() before each model call: sleeps while any file is fresh

A file older than STALE seconds (a crashed process) no longer counts. Side
processes never set the flag, so they never block each other or themselves.
"""

from __future__ import annotations

import asyncio
import itertools
import os
import time
from contextlib import contextmanager
from pathlib import Path

DIR = Path(os.environ.get("PICKY_LIVE_FLAG_DIR", "/tmp/picky-live"))
STALE = 300.0      # s; a member reply never runs this long (the watcher gives up at 240)
MAX_WAIT = float(os.environ.get("PICKY_LIVE_MAX_WAIT", "600"))
LIVE_GROUP_PORT = "8768"
_ids = itertools.count()


def is_side() -> bool:
    """True for test/exam processes: they yield to live texts and never set the flag.
    PICKY_ROLE=live (a latency-probe sidecar on its own port) counts as live."""
    if os.environ.get("PICKY_ROLE") == "live":
        return False
    if os.environ.get("PICKY_SIDE", "") not in ("", "0"):
        return True
    port = os.environ.get("PICKY_GROUP_PORT")
    return bool(port) and port != LIVE_GROUP_PORT


def begin() -> str | None:
    """Mark one live reply in flight; returns a token for end(). No-op on side processes."""
    if is_side():
        return None
    try:
        DIR.mkdir(exist_ok=True)
        p = DIR / f"{os.getpid()}-{next(_ids)}"
        p.touch()
        return str(p)
    except OSError:
        return None


def end(token: str | None) -> None:
    if token:
        try:
            os.unlink(token)
        except OSError:
            pass


@contextmanager
def held():
    tok = begin()
    try:
        yield
    finally:
        end(tok)


def busy(now: float | None = None) -> bool:
    now = time.time() if now is None else now
    try:
        entries = list(os.scandir(DIR))
    except OSError:
        return False
    for e in entries:
        try:
            if now - e.stat().st_mtime < STALE:
                return True
        except OSError:
            continue
    return False


async def wait_clear(force: bool = False, max_wait: float | None = None, poll: float = 0.5) -> float:
    """On a side process (or with force), wait until no live reply is in flight.
    Returns the seconds waited; gives up after max_wait (default MAX_WAIT)."""
    if not (force or is_side()):
        return 0.0
    t0 = time.monotonic()
    limit = MAX_WAIT if max_wait is None else max_wait
    while busy() and time.monotonic() - t0 < limit:
        await asyncio.sleep(poll)
    return time.monotonic() - t0
