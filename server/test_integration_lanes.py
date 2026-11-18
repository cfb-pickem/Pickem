"""Integration round 2026-11-18: the lanes stop stealing each other's asks (model-free)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import chat  # noqa: E402
import news  # noqa: E402
import personnel  # noqa: E402
import picky_brain  # noqa: E402
import router  # noqa: E402


def test_fact_fallback_keeps_a_kept_voice_line():
    ans = "Tough ask: UCLA's D has the edge, #2 vs #117. Style: Michigan runs it 55% of the time."
    line = "That offense is a crime scene."
    cut = "Tough ask: UCLA's D has the edge, #2 vs #117. " + line  # the filter cut the Style sentence
    assert picky_brain.fact_fallback(ans, cut, ans + " " + line) == ans + " " + line
    assert picky_brain.fact_fallback(ans, "Tough ask: UCLA's D has the edge, #2 vs #117.", ans + " " + line) == ans
    assert picky_brain.fact_fallback(ans, ans + " " + line, ans + " " + line) == ans + " " + line


def test_bare_tech_alias_leaves_virginia_tech_alone():
    games = [{"Away": "Texas Tech", "Home": "Baylor", "id": 1}]
    assert chat.games_named("Can Virginia Tech run the ball on Miami?", games) == []
    assert chat.games_named("tech is overrated", games) == games


def test_thin_citations_drops_an_emptied_credit():
    t = news.thin_citations("A is out (per SI, Monday). B is out (per ESPN, Monday). C (per X, last report Tue 11/10).")
    assert "(," not in t and "(per SI, Monday)" in t, t


def test_a_decision_to_sit_last_week_is_not_a_benching():
    assert personnel.status_of("Kanell and Kelly question Beamer's decision to sit LaNorris Sellers vs. Arkansas.") is None
    assert personnel.status_of("Coach says he will sit the starter Saturday") == "benched"


def test_new_routes_by_question_type():
    assert router.classify("Indiana's 9-1 but have they been covering?").kind == "ats_profile"
    assert router.classify("Navy and Memphis history, anything there?").kind == "h2h"


def test_gameday_override_keeps_pre_game_football_asks():
    import gameday
    import time
    soon = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 7200))
    team = lambda i, n, ab: {"id": i, "name": n, "abbr": ab, "nick": "", "rank": None}  # noqa: E731
    gs = {"1": {"id": "1", "away": team("235", "Memphis", "MEM"), "home": team("2426", "Navy", "NAVY"),
                "state": "pre", "start": soon},
          "2": {"id": "2", "away": team("259", "Virginia Tech", "VT"), "home": team("2390", "Miami", "MIA"),
                "state": "pre", "start": soon}}
    for q in ("Can Virginia Tech run the ball on Miami?", "Who's rested in Memphis-Navy?",
              "Memphis -3.5 at Navy. How does Memphis's defense match up with the option?"):
        rt = router.classify(q)
        assert not gameday.override(rt, q, "", gs).kind.startswith("gd_"), q


if __name__ == "__main__":
    fails = 0
    for name, f in list(globals().items()):
        if name.startswith("test_"):
            try:
                f()
                print("ok  ", name)
            except Exception as e:  # noqa: BLE001
                fails += 1
                print("FAIL", name, repr(e)[:300])
    print(f"\n{fails} failed" if fails else "\nall ok")
    sys.exit(1 if fails else 0)
