"""
League playoff race math: magic numbers, clinch / elimination, scenarios.

Run:  ./.venv/bin/python3 -I test_playoff_race.py
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import playoff_race as pr  # noqa: E402

# through week 11 of 2026 (real numbers), 4 weeks x 4 games left
WK11 = {"Chuck P": 30, "Ackbar": 28, "Blinkstradamus": 27, "DaddyFreeman": 27, "Liam Bryson": 26,
        "Rettino": 26, "Clam": 25, "Steve Drolet": 24, "Kevin Culligan": 24, "Brian": 23,
        "Andrew Bub": 23, "Wedge Mulshine": 22, "Breaking Horrendous": 21, "TG": 19}


def even(points, r):
    return {t: r for t in points}


def test_cut_line_and_ranks():
    res = pr.race(WK11, even(WK11, 16))
    assert res["Chuck P"]["rank"] == 1
    assert res["Blinkstradamus"]["rank"] == 3 and res["DaddyFreeman"]["rank"] == 3  # ties share a rank
    assert res["Liam Bryson"]["rank"] == 5
    assert all(r["cut"] == 27 for r in res.values())
    assert res["Liam Bryson"]["gap"] == 1 and res["Chuck P"]["gap"] == -3


def test_magic_number_week11():
    res = pr.race(WK11, even(WK11, 16))
    # others' maxes: Ackbar 44, Blinks 43, DF 43, Liam 42 ... -> 43 beats every 4th-place max
    assert res["Chuck P"]["magic"] == 13, res["Chuck P"]
    # Ackbar: Chuck 46, Blinks 43, DF 43, Liam 42 -> needs 43 -> 15 more
    assert res["Ackbar"]["magic"] == 15
    # Liam: max 42; Chuck 46, Ackbar 44, Blinks 43, DF 43 all can pass 42 -> can't clinch alone
    assert res["Liam Bryson"]["magic"] is None
    assert res["Liam Bryson"]["status"] == "alive"
    assert not res["Chuck P"]["clinched"] and not res["TG"]["eliminated"]


def test_clinch_and_eliminate():
    pts = {"A": 40, "B": 38, "C": 37, "D": 36, "E": 30, "F": 20}
    res = pr.race(pts, even(pts, 4))
    # A: threats are teams whose max >= 40: B 42, C 41, D 40 -> 3 < 4 -> clinched
    assert res["A"]["clinched"] and res["A"]["status"] == "clinched" and res["A"]["magic"] == 0
    # F: max 24, A-E all already above 24 -> eliminated
    assert res["F"]["eliminated"] and res["F"]["status"] == "eliminated" and res["F"]["magic"] is None
    # E: max 34, A-D all above 34 already -> eliminated too
    assert res["E"]["eliminated"]
    # D: magic: needs > every 4th rival max. rivals' maxes: A 44, B 42, C 41, E 34 -> D at 36+k,
    # threats >= 36+k must be < 4: k=0 -> A,B,C (3) -> clinched already
    assert res["D"]["clinched"]


def test_tie_only():
    # E can at best tie D for 4th: tiebreakers would decide it
    pts = {"A": 50, "B": 50, "C": 50, "D": 40, "E": 36}
    rem = {"A": 0, "B": 0, "C": 0, "D": 0, "E": 4}
    res = pr.race(pts, rem)
    assert res["E"]["status"] == "tiebreaker", res["E"]
    assert not res["E"]["eliminated"]
    # D can only be caught (tied) by E: 1 tied threat + 3 locked ahead = 4 -> not clinched outright
    assert not res["D"]["clinched"] and res["D"]["status"] == "alive"


def test_need_scenarios():
    pts = {"A": 30, "B": 29, "C": 28, "D": 27, "E": 26}
    res = pr.race(pts, even(pts, 4))
    e = res["E"]
    # win out = 30; A (34), B (33), C (32), D (31) can all reach 30; need 1 of them to finish under 30
    assert e["win_out"] == 30 and e["need_fall"] == 1
    falls = {x["team"]: x["max_wins"] for x in e["help"]}
    # D finishes under 30 by winning at most 2 of its last 4
    assert falls["D"] == 2 and falls["A"] == -1  # A is already at 30: can't finish under it
    assert e["status"] == "alive"


def test_remaining_points():
    week12 = [
        {"id": 1, "tiebreaker": False, "final": True},
        {"id": 2, "tiebreaker": False, "final": False},
        {"id": 3, "tiebreaker": False, "final": False},
        {"id": 4, "tiebreaker": True, "final": False},  # tiebreakers never score
    ]
    teams = ["A", "B", "C"]
    picks = {"A": {1, 2, 3, 4}, "B": {2}, "C": set()}
    # locked week: only picks on undecided pool games can still score; 3 weeks of 4 after it
    rem = pr.remaining_points(teams, {12: {"games": week12, "locked": True, "picks": picks}}, 12)
    assert rem == {"A": 2 + 12, "B": 1 + 12, "C": 0 + 12}, rem
    # unlocked week: anyone can still pick every undecided pool game
    rem = pr.remaining_points(teams, {12: {"games": week12, "locked": False, "picks": picks}}, 12)
    assert rem == {"A": 14, "B": 14, "C": 14}
    # no slate yet: 4 a week through week 15
    assert pr.remaining_points(teams, {}, 12) == {"A": 16, "B": 16, "C": 16}
    # regular season over
    assert pr.remaining_points(teams, {}, 16) == {"A": 0, "B": 0, "C": 0}


def test_season_over():
    pts = {"A": 40, "B": 38, "C": 37, "D": 36, "E": 36, "F": 20}
    res = pr.race(pts, even(pts, 0))
    assert res["A"]["clinched"] and res["C"]["clinched"]
    assert res["D"]["status"] == "tiebreaker" and res["E"]["status"] == "tiebreaker"
    assert res["F"]["eliminated"]


def test_need_answer_is_exact():
    res = pr.race(WK11, even(WK11, 16))
    meta = {"weeks_left": 4, "points_left": 16, "first_week": 12}
    txt = pr.need_answer("Liam Bryson", "Liam", res, meta)
    assert "26" in txt and "27" in txt and "16" in txt, txt
    assert "Chuck" in txt or "4 of" in txt or "help" in txt.lower(), txt
    txt = pr.need_answer("Chuck P", "Charlie", res, meta)
    assert "13" in txt and "16" in txt, txt
    pts = {"A": 40, "B": 38, "C": 37, "D": 36, "E": 30, "F": 20}
    res2 = pr.race(pts, even(pts, 4))
    assert "eliminat" in pr.need_answer("F", "Fred", res2, meta).lower()
    assert "clinched" in pr.need_answer("A", "Al", res2, meta).lower()


def test_need_question_matcher():
    yes = ["what do I need to make playoffs", "@Picky what do i need to make the playoffs?",
           "can I still make the playoffs", "am I eliminated", "magic number?", "what does Liam need to make playoffs",
           "have I clinched", "playoff math", "what's my magic number"]
    no = ["who won the CFP last year", "college football playoff rankings", "what's the line on Texas",
          "who's winning"]
    for q in yes:
        assert pr.is_need_question(q), q
    for q in no:
        assert not pr.is_need_question(q), q


def test_summary_lines():
    res = pr.race(WK11, even(WK11, 16))
    meta = {"weeks_left": 4, "points_left": 16, "first_week": 12}
    s = pr.summary_text(res, meta)
    assert "4 weeks" in s and "16" in s and "27" in s, s
    assert "Chuck P" in s or "Charlie" in s



def test_last_pool_week_2026_is_14():
    """2026: conference championships (Dec 5) are week 14; week 15 (Army-Navy) has no pool games."""
    assert pr.last_pool_week(2026) == 14 and pr.last_pool_week("2026") == 14
    assert pr.last_pool_week(2025) == 15 and pr.last_pool_week(None) == 15
    teams = ["A", "B"]
    slate13 = {13: {"locked": False, "picks": {}, "games": [{"id": i, "final": False} for i in range(4)]}}
    rem = pr.remaining_points(teams, slate13, 13, last_week=pr.last_pool_week(2026))
    assert rem == {"A": 8, "B": 8}, rem  # week 13's 4 + week 14's 4: 2 weeks / 8 points left
    import pick_strategy
    ctx = {"season": 2026, "current_week": 13, "you": {"team_name": "A"},
           "standings_ytd": [{"team": t, "w": 30 - i, "l": 20, "rank": i + 1} for i, t in enumerate("ABCDEF")],
           "games_this_week": [{"winner": None} for _ in range(4)]}
    n = pick_strategy.note(ctx, "what's my strategy")
    assert "about 8 picks left" in n and "weeks through 14" in n, n


if __name__ == "__main__":
    tests = [v for k, v in dict(globals()).items() if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  ok  {t.__name__}")
        except AssertionError as e:
            print(f"FAIL  {t.__name__}: {e}")
            failed += 1
        except Exception as e:  # noqa: BLE001
            print(f"ERR   {t.__name__}: {type(e).__name__}: {e}")
            failed += 1
    print("\nALL GREEN" if not failed else f"\n{failed} FAILED")
    sys.exit(1 if failed else 0)
