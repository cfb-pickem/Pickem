"""Offline checks for cycle.py's window math and gate (no models, no services)."""
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cycle as c  # noqa: E402

T = lambda s: dt.datetime.fromisoformat(s).replace(tzinfo=c.CT)  # noqa: E731
cases = [
    ("2026-11-17T21:59", False, None), ("2026-11-17T22:00", True, "2026-11-18T08:30"),   # Tue night
    ("2026-11-18T03:00", True, "2026-11-18T08:30"), ("2026-11-18T08:30", False, None),    # Wed morning
    ("2026-11-15T22:30", True, "2026-11-17T08:30"),                                       # Sun night -> Tue
    ("2026-11-16T13:00", True, "2026-11-17T08:30"), ("2026-11-16T23:00", True, "2026-11-17T08:30"),  # Monday
    ("2026-11-17T08:00", True, "2026-11-17T08:30"), ("2026-11-17T09:00", False, None),    # Tue morning
    ("2026-11-21T12:00", False, None),                                                    # Saturday noon
]
bad = 0
for t, win, end in cases:
    got_w = c.in_window(T(t))
    got_e = c.window_end(T(t)) if got_w else None
    ok = got_w == win and (end is None or got_e == T(end))
    bad += not ok
    print(("ok  " if ok else "FAIL"), t, got_w, got_e and got_e.strftime("%a %F %H:%M"))
sys.exit(1 if bad else 0)
