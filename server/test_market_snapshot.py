"""
Market history (market_snapshot.py, plan 2.1 market_snapshot): ESPN's scoreboard odds (DraftKings open + current
spread/total/moneyline, already fetched by the live-games poller) become dated snapshots in a local SQLite cache,
mirrored to Supabase picky.market_snapshot. Every 15 minutes (5 on game days), a row only when the number moved
or 6 h passed, old games trimmed to their open + close rows. Nothing here fetches anything new.

Run:  ./.venv/bin/python3 -I test_market_snapshot.py
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, ".")
import live_games as L  # noqa: E402
import market_snapshot as M  # noqa: E402

FIX = Path(__file__).resolve().parent / "eval" / "fixtures" / "factstore"
SB = json.loads((FIX / "espn_scoreboard.json").read_text())
WED = dt.datetime(2026, 11, 18, 15, 30, tzinfo=dt.timezone.utc).timestamp()   # not a game day
SAT = dt.datetime(2026, 11, 21, 18, 0, tzinfo=dt.timezone.utc).timestamp()    # game day
MIN = 60
UMASS = "401866467"  # Akron at UMass, MASS -3.5, total 47.5 in the fixture


def board():
    return L.parse_scoreboard(json.loads(json.dumps(SB)))


def tmpdb():
    return Path(tempfile.mkdtemp()) / "market.sqlite"


def test_scoreboard_keeps_home_spread_open_and_moneyline():
    g = board()[UMASS]
    od = g["odds"]
    assert od["spread_home"] == -3.5 and od["total"] == 47.5, od
    assert od["spread_home_open"] == -3.5 and od["total_open"] == 47.5, od
    assert od["ml_home"] == -170 and od["ml_away"] == 142, od
    assert od["book"] == "DraftKings"
    assert od["details"] == "MASS -3.5"  # existing field untouched


def test_home_spread_sign_comes_from_the_home_side_not_the_abbreviation():
    # ESPN writes 'BUF -2.5' while the team abbreviation is BUFF: the home pointSpread line is the truth
    hits = [g for g in board().values() if (g.get("odds") or {}).get("details", "").startswith("BUF ")]
    assert hits and hits[0]["odds"]["spread_home"] == -2.5, hits[:1]
    fav_home = {e["id"]: ((e["competitions"][0].get("odds") or [{}])[0].get("homeTeamOdds") or {}).get("favorite")
                for e in SB["events"]}
    for gid, g in board().items():
        sh = (g.get("odds") or {}).get("spread_home")
        if sh:
            assert (sh < 0) == bool(fav_home[gid]), (gid, g["odds"])


def test_rows_from_board():
    rows = M.rows_from_board(board(), WED)
    r = next(x for x in rows if x["game_id"] == UMASS)
    assert r["spread_home"] == -3.5 and r["total"] == 47.5 and r["book"] == "DraftKings" and r["ts"] == WED
    assert r["open_spread_home"] == -3.5 and r["kickoff"].startswith("2026-11-19")
    assert len(rows) >= 50


def test_record_throttles_15_min_normal_5_min_game_day():
    db = tmpdb()
    assert M.record(board(), WED, db=db) > 0
    assert M.record(board(), WED + 10 * MIN, db=db) == -1, "not due yet"
    assert M.record(board(), WED + 16 * MIN, db=db) == 0, "due, nothing moved"
    db2 = tmpdb()
    M.record(board(), SAT, db=db2)
    assert M.record(board(), SAT + 6 * MIN, db=db2) == 0, "game day: due after 5 min"


def test_record_writes_only_on_change_or_heartbeat():
    db = tmpdb()
    b = board()
    M.record(b, WED, db=db)
    b[UMASS]["odds"]["spread_home"] = -4.5
    assert M.record(b, WED + 20 * MIN, db=db) == 1
    assert M.record(b, WED + 40 * MIN, db=db) == 0
    assert M.record(b, WED + 7 * 3600, db=db) >= 50, "6 h heartbeat rewrites every game"
    h = M.history(UMASS, db=db)
    assert [x["spread_home"] for x in h][:2] == [-3.5, -4.5]


def test_summary_open_now_move_and_lock_point():
    db = tmpdb()
    b = board()
    M.record(b, WED, db=db)
    b[UMASS]["odds"].update(spread_home=-6.5, total=44.5)
    M.record(b, WED + 3 * 3600, db=db)
    s = M.summary(UMASS, db=db, lock_ts=WED + 3600)
    assert s["open_spread_home"] == -3.5 and s["spread_home"] == -6.5, s
    assert s["move"] == -3.0 and s["total_move"] == -3.0, s
    assert s["at_lock_spread_home"] == -3.5 and s["since_lock"] == -3.0, s
    assert s["n"] == 2 and s["source"] == "ESPN DraftKings"


def test_trim_keeps_open_and_close_for_old_games():
    db = tmpdb()
    b = board()
    for i, sp in enumerate((-3.5, -4.5, -5.5, -6.5)):
        b[UMASS]["odds"]["spread_home"] = sp
        M.record(b, WED + i * 20 * MIN, db=db)
    assert len(M.history(UMASS, db=db)) == 4
    M.trim(db=db, now=WED + 30 * 86400)
    h = M.history(UMASS, db=db)
    assert [x["spread_home"] for x in h] == [-3.5, -6.5], h
    M.trim(db=db, now=WED + 2 * 86400)  # recent games are never trimmed
    assert len(M.history(UMASS, db=db)) == 2


def test_push_marks_rows_and_keeps_them_on_failure():
    db = tmpdb()
    M.record(board(), WED, db=db)
    sent = []
    assert M.push(db=db, pusher=lambda rows: sent.extend(rows)) == len(sent) > 0
    assert M.push(db=db, pusher=lambda rows: sent.extend(rows)) == 0, "already pushed"
    b = board()
    b[UMASS]["odds"]["spread_home"] = -9.5
    M.record(b, WED + 20 * MIN, db=db)

    def down(rows):
        raise OSError("supabase down")
    assert M.push(db=db, pusher=down) == 0
    got = []
    assert M.push(db=db, pusher=lambda rows: got.extend(rows)) == 1 and got[0]["spread_home"] == -9.5
    assert "pushed" not in got[0]


def test_hook_never_raises():
    M.hook({"x": {"odds": {"spread_home": "garbage"}}}, WED, db=Path("/nonexistent/dir/m.sqlite"), pusher=None)


def test_poller_calls_snapshot_after_each_scoreboard():
    seen = []

    class R:
        status_code, headers = 200, {}

        def json(self):
            return json.loads(json.dumps(SB))

        def raise_for_status(self):
            pass

    class C:
        def get(self, url, headers=None, timeout=None):
            return R()
    p = L.Poller(client=C(), clock=lambda: WED, cache=Path(tempfile.mkdtemp()) / "lg.json", pool_ids_fn=lambda: [],
                 snapshot=lambda games, now: seen.append((len(games), now)))
    p.games = {}
    p.step()
    assert seen and seen[0][0] >= 60 and seen[0][1] == WED, seen


def test_poller_without_snapshot_hook_is_unchanged():
    p = L.Poller(client=None, clock=lambda: WED, cache=Path(tempfile.mkdtemp()) / "lg.json", pool_ids_fn=lambda: [])
    assert p.snapshot is None


def test_run_wires_the_real_hook():
    import inspect
    assert "market_snapshot" in inspect.getsource(L.run)


if __name__ == "__main__":
    fails = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok   {name}")
            except Exception as e:  # noqa: BLE001
                fails += 1
                print(f"FAIL {name}: {e!r}"[:800])
    print("ALL PASS" if not fails else f"{fails} FAILED")
    sys.exit(1 if fails else 0)
