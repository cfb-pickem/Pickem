#!/usr/bin/env python3
"""Game-day windows (review R4/R9, decided 2026-11-17). Pure stdlib, CT clock.

  FREEZE   Fri 18:00 -> Sun 04:00 CT: nothing reloads or pushes to live. No release.sh promote, no
           flub_loop.fix push/reload, no feed_monitor.repair push. The nightly autofix commits to a branch
           flub/<date> instead, and the next run outside the freeze promotes it through release.sh.
           Override for a real emergency only: PICKY_FREEZE_OVERRIDE=1 (logged by release.sh).
  NO EXAMS Sat 10:00 -> 24:00 CT: no exams, no side sidecars, no eval runners. Keep-warm ignores the
           exam lock all Saturday.

CLI (exit 0 = allowed, 3 = inside the window; prints why):
  freeze.py check-freeze        # release.sh, flub_loop, feed_monitor
  freeze.py check-exam          # eval runners, side sidecars, job specs
  freeze.py status
"""

from __future__ import annotations

import datetime as dt
import os
import sys
import zoneinfo

CT = zoneinfo.ZoneInfo("America/Chicago")


def _now(now: dt.datetime | None) -> dt.datetime:
    return (now or dt.datetime.now(CT)).astimezone(CT)


def frozen(now: dt.datetime | None = None) -> bool:
    """Fri 18:00 through Sun 04:00 CT."""
    n = _now(now)
    wd, t = n.weekday(), n.time()
    return (wd == 4 and t >= dt.time(18, 0)) or wd == 5 or (wd == 6 and t < dt.time(4, 0))


def no_exams(now: dt.datetime | None = None) -> bool:
    """Saturday 10:00 through midnight CT."""
    n = _now(now)
    return n.weekday() == 5 and n.time() >= dt.time(10, 0)


def saturday(now: dt.datetime | None = None) -> bool:
    return _now(now).weekday() == 5


def override() -> bool:
    return os.environ.get("PICKY_FREEZE_OVERRIDE") == "1"


def exam_guard(what: str = "this exam") -> None:
    """Eval runners and side sidecars call this first: inside the no-exam window it exits 3."""
    if no_exams():
        print(f"{what}: refused, Saturday 10:00-24:00 CT is game time (no exams, no side sidecars)", file=sys.stderr)
        sys.exit(3)


def main(argv: list[str]) -> int:
    cmd = argv[0] if argv else "status"
    if cmd == "check-freeze":
        if frozen() and not override():
            print("FROZEN: game-day freeze Fri 18:00 - Sun 04:00 CT; nothing ships to live")
            return 3
        return 0
    if cmd == "check-exam":
        if no_exams():
            print("NO EXAMS: Saturday 10:00-24:00 CT is game time; no exams or side sidecars")
            return 3
        return 0
    print(f"frozen={frozen()} no_exams={no_exams()} saturday={saturday()} now={_now(None):%a %H:%M} CT")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
