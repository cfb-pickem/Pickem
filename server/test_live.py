"""
Live scores: the per-game live line, the covering math, and the rule that
live data never reaches prompt_prefix().

Run:  ./.venv/bin/python3 -I test_live.py

Covering uses the pool `line` convention (home-team adjustment, negative =
home favored) and set_winner_on_final()'s sum: home covers when
HomePts + line > AwayPts, otherwise Away (so a dead-even push goes to Away).
"""

from __future__ import annotations

import datetime as dt
import sys

sys.path.insert(0, ".")
import chat  # noqa: E402

NOW_UTC = dt.datetime(2026, 11, 15, 3, 5, tzinfo=dt.timezone.utc)
NOW_CT = dt.datetime(2026, 11, 14, 21, 5)
FRESH = "2026-11-15T03:04:50+00:00"


def row(away, home, a, h, line, status="in_progress", period=2, clock="12:31", sn=None, **kw):
    sn = sn or {"in_progress": "STATUS_IN_PROGRESS", "final": "STATUS_FINAL"}.get(status, "STATUS_SCHEDULED")
    return {"GameId": kw.pop("gid", hash((away, home)) & 0xffff), "Away": away, "Home": home,
            "AwayPts": a, "HomePts": h, "line": line, "Status": status, "Period": period, "Clock": clock,
            "status_name": sn, "SourceUpdatedAt": kw.pop("updated", FRESH),
            "Start (CT)": kw.pop("start", "2026-11-14T20:00:00"), "winner": kw.pop("winner", None), **kw}


def trigger_winner(g):
    """set_winner_on_final(), verbatim in Python."""
    return g["Home"] if g["HomePts"] + g["line"] > g["AwayPts"] else g["Away"]


# -- covering math ------------------------------------------------------------------

def test_home_dog_covering():
    g = row("North Texas", "UTSA", 3, 7, 2.5)
    assert chat.cover_margin(g) == 6.5
    assert chat.covering_side(g) == "UTSA"
    assert chat._cover_text(g) == "UTSA +2.5 (by 6.5)", chat._cover_text(g)


def test_away_fav_covering():
    g = row("North Texas", "UTSA", 10, 3, 2.5)
    assert chat.covering_side(g) == "North Texas"
    assert chat._cover_text(g) == "North Texas -2.5 (by 4.5)", chat._cover_text(g)


def test_home_fav_wins_but_dog_covers():
    # Boise State -13.5 won by 5: Oregon State +13.5 covered (the real week 11 final)
    g = row("Oregon State", "Boise State", 41, 46, -13.5, status="final", period=4, clock="0:00")
    assert chat.covering_side(g) == "Oregon State" == trigger_winner(g)


def test_matches_trigger_on_a_grid():
    for line in (-13.5, -3, -0.5, 0, 2.5, 7):
        for a in range(0, 30, 3):
            for h in range(0, 30, 4):
                g = row("Away U", "Home U", a, h, line)
                assert chat.covering_side(g) == trigger_winner(g), (line, a, h)


def test_push_goes_to_away_like_the_standings():
    g = row("Away U", "Home U", 7, 10, -3)  # home -3 wins by exactly 3
    assert chat.cover_margin(g) == 0
    assert chat.covering_side(g) == "Away U" == trigger_winner(g)
    assert "push for Away U" in chat._cover_text(g)


def test_ot_does_not_change_the_math():
    g = row("Away U", "Home U", 31, 34, -2.5, period=6)
    assert chat.covering_side(g) == "Home U"
    assert "in 2OT (overtime has no clock)" in chat.game_state(g, NOW_UTC, NOW_CT)


def test_no_line_or_score():
    assert chat.covering_side(row("A", "B", None, 7, 2.5)) is None
    assert chat.covering_side(row("A", "B", 3, 7, None)) is None


# -- live line formatting -------------------------------------------------------------

def test_live_line():
    g = row("North Texas", "UTSA", 3, 7, 2.5, possession="home", is_red_zone=True)
    assert chat.game_state(g, NOW_UTC, NOW_CT) == (
        "North Texas 3, UTSA 7, LIVE, 12:31 left in Q2 (42:31 left in the game), UTSA has the ball in the red zone")


def test_halftime_end_of_quarter_delay():
    s = chat.game_state(row("A", "B", 3, 7, 1.5, sn="STATUS_HALFTIME", possession="home"), NOW_UTC, NOW_CT)
    assert s == "A 3, B 7, LIVE, halftime (30:00 left in the game)", s
    assert "end of Q3 (15:00 left in the game)" in chat.game_state(row("A", "B", 3, 7, 1.5, period=3, sn="STATUS_END_PERIOD"), NOW_UTC, NOW_CT)
    assert "DELAYED" in chat.game_state(row("A", "B", 3, 7, 1.5, sn="STATUS_DELAYED"), NOW_UTC, NOW_CT)


def test_final_and_final_ot():
    assert chat.game_state(row("A", "B", 20, 17, 1.5, "final", 4, "0:00"), NOW_UTC, NOW_CT) == "A 20, B 17, FINAL, game over (no time left)"
    assert chat.game_state(row("A", "B", 21, 27, 1.5, "final", 5, "0:00"), NOW_UTC, NOW_CT).endswith("FINAL in OT, game over (no time left)")


def test_not_started_and_late():
    s = chat.game_state(row("NDSU", "Hawai'i", 0, 0, 6.5, "scheduled", 0, start="2026-11-14T22:00:00"),
                        NOW_UTC, NOW_CT)
    assert s == "NDSU at Hawai'i: not started, kicks off Sat 10:00 PM CT", s
    late = chat.game_state(row("A", "B", 0, 0, 6.5, "scheduled", 0, start="2026-11-14T20:30:00"), NOW_UTC, NOW_CT)
    assert "hasn't shown it starting" in late


def test_stale_feed_is_flagged():
    old = row("A", "B", 3, 7, 1.5, sn="STATUS_HALFTIME", updated="2026-11-15T02:55:00+00:00")
    fresh = row("C", "D", 3, 7, 1.5)
    # a game sitting still at halftime isn't stale while the feed writes other games
    assert chat.feed_age([old, fresh], NOW_UTC) == 10
    assert "STALE" not in chat.game_state(old, NOW_UTC, NOW_CT, chat.feed_age([old, fresh], NOW_UTC))
    assert "STALE" in chat.game_state(old, NOW_UTC, NOW_CT, chat.feed_age([old], NOW_UTC))
    assert chat.feed_age([], NOW_UTC) is None


chat._now_utc = lambda: NOW_UTC


def pool_ctx(rows, pool):
    return {"games_this_week": pool, "live_games": rows, "live_at": NOW_CT}


def test_live_block_pool_games():
    ut = row("North Texas", "UTSA", 3, 7, 2.5, gid=1, possession="home")
    bs = row("Oregon State", "Boise State", 41, 46, -13.5, "final", 4, "0:00", gid=2, winner="Oregon State")
    pool = [{"id": 1, "away": "North Texas", "home": "UTSA", "line": 2.5, "winner": None},
            {"id": 2, "away": "Oregon State", "home": "Boise State", "line": -13.5, "winner": "Oregon State"},
            {"id": 3, "away": "X", "home": "Y", "line": 1.5, "winner": None}]
    b = chat.live_block(pool_ctx([ut, bs], pool))
    assert "as of 9:05 PM CT" in b
    assert "1. North Texas 3, UTSA 7, LIVE, 12:31 left in Q2 (42:31 left in the game), UTSA has the ball. " \
           "Currently covering the pool line: UTSA +2.5 (by 6.5)." in b, b
    assert "2. Oregon State 41, Boise State 46, FINAL, game over (no time left). Covered: Oregon State +13.5; " \
           "Boise State -13.5 did NOT cover." in b, b
    assert "3. X at Y: no score data." in b
    assert chat.live_block({"games_this_week": pool}) == ""  # not loaded: nothing
    assert "unreachable" in chat.live_block({**pool_ctx([], pool), "live_error": True})


def test_final_uses_the_graded_winner():
    # standings grade off all_games.winner; the live line must say the same side
    bs = row("A", "B", 7, 10, -3, "final", 4, "0:00", gid=2)
    pool = [{"id": 2, "away": "A", "home": "B", "line": -3, "winner": "A"}]
    assert "Covered: A +3; B -3 did NOT cover." in chat.live_block(pool_ctx([bs], pool))


# -- other games, on demand -------------------------------------------------------------

OTHERS = [
    row("Texas", "LSU", 10, 14, -3.5, period=3, clock="7:09", gid=10),
    row("Texas Tech", "Oklahoma State", 14, 0, -3.5, period=1, clock="8:06", gid=11),
    row("Stanford", "Virginia Tech", 24, 14, -15.5, period=3, clock="5:07", gid=12),
    row("Washington", "Michigan State", 21, 14, 3.5, "final", 4, "0:00", gid=13, start="2026-11-14T14:30:00"),
    row("Colorado State", "Washington State", 0, 0, -9.5, "scheduled", 0, gid=14, start="2026-11-14T21:30:00"),
    row("NDSU", "Hawai'i", 0, 0, 6.5, "scheduled", 0, gid=15, start="2026-11-14T22:00:00"),
    row("Boise Tech", "UTSA", 3, 7, 2.5, gid=1),
]


def test_games_named():
    ids = lambda q: [g["GameId"] for g in chat.games_named(q, OTHERS)]  # noqa: E731
    assert ids("how's Texas Tech doing") == [11]          # not Texas
    assert ids("Texas score?") == [10]
    assert ids("is hawaii winning") == [15]
    assert ids("what about Washington") == [13]           # not Washington State
    assert ids("colorado st score") == [14]
    assert ids("score of the virginia tech game") == [12]
    assert ids("who's winning the Duke game") == []


def test_games_named_state_stem():
    g = [row("Oregon State", "Boise State", 1, 2, -13.5, gid=2)]
    assert [x["GameId"] for x in chat.games_named("is Boise covering", g)] == [2]


def test_live_note_named_other_game():
    pool = [{"id": 1, "away": "Boise Tech", "home": "UTSA", "line": 2.5}]
    n = chat.live_note(pool_ctx(OTHERS, pool), "what's the score of the Texas game?")
    assert "Texas 10, LSU 14, LIVE, 7:09 left in Q3" in n and "not a pool game" in n, n
    assert n.startswith("SCORE QUESTION")
    assert chat.live_note(pool_ctx(OTHERS, pool), "who's in first?") == ""
    assert chat.live_note(pool_ctx(OTHERS, pool), "what's the score of the UTSA game") == ""  # tail has it


def test_live_note_missing_game():
    pool = [{"id": 1, "away": "Boise Tech", "home": "UTSA", "line": 2.5}]
    n = chat.live_note(pool_ctx(OTHERS, pool), "what's the Duke score")
    assert n.startswith("SCORE QUESTION") and "isn't in this week's score data" in n
    t = chat.live_note(pool_ctx(OTHERS, pool), "how much time is left")
    assert "Pool games live now:\n- Boise Tech 3, UTSA 7, LIVE, 12:31 left in Q2" in t, t


def test_upsets_and_on_now():
    pool = [{"id": 1, "away": "Boise Tech", "home": "UTSA", "line": 2.5}]
    n = chat.live_note(pool_ctx(OTHERS, pool), "any upsets right now?")
    # Stanford +15.5 up 10, Texas Tech (away, -3.5 home fav OSU) up 14, Washington won as a +3.5? no:
    # line 3.5 = home MSU +3.5, so Washington was the favorite: not an upset
    assert "Stanford 24" in n and "15.5-point underdog" in n and "Texas Tech 14" in n, n
    assert "Washington 21" not in n and "Texas 10" not in n
    assert n.index("Stanford") < n.index("Texas Tech")
    on = chat.live_note(pool_ctx(OTHERS, pool), "what's on right now")
    assert "MORE GAMES ON RIGHT NOW (3 live" in on and on.index("Texas 10") < on.index("Stanford"), on
    assert "UTSA" not in on  # pool games live in the tail


def test_finals_today():
    pool = [{"id": 1, "away": "Boise Tech", "home": "UTSA", "line": 2.5}]
    orig = chat._now_ct_naive
    chat._now_ct_naive = lambda: NOW_CT
    try:
        n = chat.live_note(pool_ctx(OTHERS, pool), "final scores from today")
    finally:
        chat._now_ct_naive = orig
    assert "Washington 21, Michigan State 14, FINAL" in n, n


# -- the prefix never carries live data ---------------------------------------------------

def test_prefix_identical_a_minute_apart_with_live_data():
    import test_leader
    c1 = test_leader.ctx()
    c2 = {**c1, "live_games": OTHERS, "live_at": NOW_CT}
    c3 = {**c1, "live_games": [row("Texas", "LSU", 17, 14, -3.5, gid=10)],
          "live_at": NOW_CT + dt.timedelta(minutes=1)}
    orig = chat._now_ct_naive
    try:
        chat._now_ct_naive = lambda: NOW_CT
        p1, p2 = chat.prompt_prefix(c1), chat.prompt_prefix(c2)
        chat._now_ct_naive = lambda: NOW_CT + dt.timedelta(minutes=1)
        p3 = chat.prompt_prefix(c3)
        t2 = chat._system_tail(c2)
    finally:
        chat._now_ct_naive = orig
    assert p1 == p2 == p3
    assert "LIVE SCOREBOARD" not in p1 and "(as of" not in p1 and "9:05" not in p1
    assert "LIVE SCOREBOARD" in t2  # it goes in the tail instead


if __name__ == "__main__":
    tests = [v for k, v in dict(globals()).items() if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  ok  {t.__name__}")
        except Exception as e:  # noqa: BLE001
            print(f"FAIL  {t.__name__}: {type(e).__name__}: {e}")
            failed += 1
    print(f"{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
