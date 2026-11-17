"""
Straggler nudges (Charlie, 2026-11-17; site audit #8 OK'd 2026-11-15): about 2 hours
before the weekly lock, Picky DMs each member the site's Weekly Submissions panel
(RPC submission_status) still shows as "Not yet": a short, funny "you haven't
picked, lock is at 11" with the time left. Optional second nudge ~30 min before
lock, only to whoever still hasn't picked. Templated, no model call.

  - who: active, non-exhibition teams (group_posts.League.tid) with a mapped handle in
    cfb-pickem-private/picky_group_members.json; never anyone else;
  - what: only that THEY haven't picked. Never anyone's picks, never who else has/hasn't;
  - once per member per slot per week (outbox once-only keys nudge:<season>:<week>:<slot>:<team>);
  - quiet hours (23:30-08:00) skip the slot, the group pause stops it, dry run applies.

How it sits with group_posts' gameday reminder: the 7:00 group reminder on a Saturday
lock still names the stragglers in the group (unchanged). These DMs come after it,
to the people still missing at ~9:00, then ~10:30. Weekday locks: group_posts' own
reminder slots are untouched; the DMs key off the lock the same way.

Hooked from group_posts.tick (every 5 min). DMs ride the same outbox
(group_outbox.post_dm); the watcher sends them as 1-on-1 texts.
CLI: straggler_nudges.py preview [--as-of ISO] | selftest --to <Picky's own handle>
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import sys
import zoneinfo
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import group_outbox as ob  # noqa: E402

CT = zoneinfo.ZoneInfo("America/Chicago")
SLOTS = (("first", dt.timedelta(hours=2), dt.timedelta(minutes=45)),
         ("second", dt.timedelta(minutes=30), dt.timedelta(minutes=20)))
STOP_BEFORE = dt.timedelta(minutes=5)   # nothing in the last 5 minutes: it can't land in time


def due_slot(lock: dt.datetime | None, now: dt.datetime) -> str | None:
    if not lock or now >= lock - STOP_BEFORE:
        return None
    for name, before, grace in SLOTS:
        at = lock - before
        if at <= now < min(at + grace, lock - STOP_BEFORE):
            return name
    return None


def lock_words(lock: dt.datetime) -> str:
    """'11', '11:30', '6:30pm' (am implied for a morning lock: 'lock is at 11')."""
    t = lock.astimezone(CT)
    h = t.strftime("%-I") + (t.strftime(":%M") if t.minute else "")
    return h if t.hour < 12 else h + "pm"


def left_words(lock: dt.datetime, now: dt.datetime) -> str:
    mins = max(1, int((lock - now).total_seconds() // 60))
    if mins >= 90:
        hrs = round(mins / 60 * 2) / 2
        return f"about {hrs:g} hours"
    if mins >= 55:
        return "about an hour"
    return f"{mins} minutes"


def _variant(options: list[str], key: str) -> str:
    return options[int(hashlib.sha1(key.encode()).hexdigest(), 16) % len(options)]


def nudge_text(name: str, week: int, lock: dt.datetime, now: dt.datetime, slot: str, key: str) -> str:
    at, left = lock_words(lock), left_words(lock, now)
    if slot == "second":
        opts = [f"{name}, still no week {week} picks from you. Lock is at {at}, {left} from now. No pick, no point.",
                f"Last call, {name}. Week {week} locks at {at} ({left}). Even the slots button beats a blank card.",
                f"{name}: {left} till lock and your week {week} card is still empty. I can't pick for you. Well, I could. I won't."]
    else:
        opts = [f"{name}, you haven't made your week {week} picks. Lock is at {at}, so you've got {left}. Don't be the guy texting me at {(lock - dt.timedelta(minutes=2)).astimezone(CT):%-I:%M}.",
                f"Friendly reminder from your favorite bot: no week {week} picks from you yet, {name}. Lock is at {at} ({left}).",
                f"{name}. Picks. Week {week} locks at {at}, {left} from now, and your card is blank. A blank card scores zero.",
                f"Hey {name}, the site says you still haven't picked week {week}. Lock is at {at}, {left} left. Go."]
    return _variant(opts, key)


def stragglers(sub: dict[str, bool], active: set[str]) -> list[tuple[str, str, str]]:
    """[(team, what the group calls them, handle)] for active members with no picks in and a
    mapped handle. Exhibition teams aren't in `active` (group_posts.League.tid) or the map."""
    handles: dict[str, str] = {}
    for h, team in ob.member_handles().items():
        handles.setdefault(team, h)
    names = ob.member_names()
    return [(t, names.get(t) or t, handles[t]) for t in sorted(sub) if not sub[t] and t in active and t in handles]


def tick(lg, week: int | None, now: dt.datetime, dry_run: bool | None = None, echo=print) -> dict:
    """Queue this slot's nudges. Returns {team: outcome} ('' when nothing is due)."""
    if week is None:
        return {}
    import playoff_race as pr
    if week > pr.LAST_REGULAR_WEEK:
        return {}  # playoffs lock per game; not this
    lock = lg.lock_time(week)
    slot = due_slot(lock, now)
    if not slot or ob.in_quiet_hours(now):
        return {}
    out = {}
    for team, name, handle in stragglers(lg.submission(week), set(lg.tid.values())):
        key = f"nudge:{lg.season}:{week}:{slot}:{team}"
        if ob.done(key):
            continue
        txt = nudge_text(name, week, lock, now, slot, key)
        out[team] = ob.post_dm(team, handle, [txt], key, now=now, dry_run=dry_run, echo=echo,
                               expires_s=int(max(60, (lock - STOP_BEFORE - now).total_seconds())))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["preview", "selftest"])
    ap.add_argument("--as-of", dest="as_of")
    ap.add_argument("--to")
    a = ap.parse_args(argv)
    if a.cmd == "selftest":
        # one DM to Picky's OWN Apple ID through the real outbox + watcher (no member gets it)
        import subprocess
        me = subprocess.run(["security", "find-generic-password", "-a", "drake", "-s",
                             "CFB_PICKEM__PICKY_APPLEID_EMAIL", "-w"], capture_output=True, text=True).stdout.strip()
        if not a.to or ob.norm_handle(a.to) != ob.norm_handle(me):
            print("selftest only DMs Picky's own Apple ID (--to <it>)")
            return 2
        now = dt.datetime.now(CT)
        print(ob.post_dm("Picky", a.to, [f"DM outbox self-test {now:%H:%M:%S}"], f"dm-selftest:{now.timestamp():.0f}",
                         now=now, allow_unmapped=True))
        return 0
    import group_posts as gp
    now = dt.datetime.fromisoformat(a.as_of).replace(tzinfo=CT) if a.as_of else dt.datetime.now(CT)
    lg = gp.League(gp.DB(), now)
    w = lg.open_week()
    lock = lg.lock_time(w) if w is not None else None
    print(f"week {w} lock {lock} slot {due_slot(lock, now)}")
    for name, before, _ in SLOTS:
        if lock:
            print(f"  {name}: {(lock - before):%a %H:%M} CT")
    sub = lg.submission(w) if w is not None else {}
    for team, nm, _h in stragglers(sub, set(lg.tid.values())):
        print(f"  > {team}: {nudge_text(nm, w, lock, max(now, lock - SLOTS[0][1]), 'first', f'preview:{team}')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
