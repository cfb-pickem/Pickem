"""
Feed health (zero maintenance, phase 8): when a free feed changes its format or comes back empty, the refresher
keeps the last good copy and writes one line here, no Claude. feed_monitor kicks a refresher whose cache goes
stale; this catches the quieter failure, a fetch that "works" but returns the wrong shape.

    data, ok = accept("cfbd:records", new, prev, kind=list)    # prev kept (and a line logged) when new is bad
    note("rankings", "ESPN rankings: no CFP poll in the response")

Log: server/logs/feed_health.log (PICKY_FEED_HEALTH_LOG overrides). One line per event:
    2026-11-18 09:40:01 cfbd:records kept last good: got dict, want list
"""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _log_path() -> Path:
    return Path(os.environ.get("PICKY_FEED_HEALTH_LOG", HERE / "logs" / "feed_health.log"))


def note(name: str, msg: str) -> None:
    """One health line. Never raises."""
    try:
        p = _log_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a") as f:
            f.write(f"{dt.datetime.now():%Y-%m-%d %H:%M:%S} {name} {msg}\n")
    except OSError:
        pass


def accept(name: str, new, prev, kind: type | tuple = (list, dict), check=None, allow_empty: bool = False):
    """(data to keep, accepted?). `new` is rejected when it isn't `kind`, is empty while `prev` wasn't (unless
    allow_empty: a legitimately empty new season), or fails `check(new)` (returns an error string or None)."""
    why = None
    if not isinstance(new, kind):
        want = kind.__name__ if isinstance(kind, type) else "/".join(k.__name__ for k in kind)
        why = f"got {type(new).__name__}, want {want}"
    elif not new and prev and not allow_empty:
        why = "empty response"
    elif check is not None:
        why = check(new)
    if why is None:
        return new, True
    note(name, f"kept last good: {why}" if prev else f"rejected (no previous copy): {why}")
    return prev, False
