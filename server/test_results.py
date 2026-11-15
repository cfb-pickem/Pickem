"""
Per-pick results graded in code, and the guard that checks Picky's answer
against them before it goes out.

Run:  ./.venv/bin/python3 -I test_results.py

The 9B redid spread math from the raw score ("Boise won, so your Oregon State
pick didn't cover") and miscounted records. grade_pick / results_block state
WIN/LOSS and the record outright; check_answer flags any answer that says
otherwise (so _guarded_stream regenerates once, then falls back to a template).
"""

from __future__ import annotations

import sys

sys.path.insert(0, ".")
import chat  # noqa: E402

# `line` is the home adjustment: -13.5 = home (Boise State) favored by 13.5
OSU, BSU, WF, SMU, UTSA, NT = "Oregon State", "Boise State", "Wake Forest", "SMU", "UTSA", "North Texas"


def test_underdog_loses_by_less_than_spread_is_win():
    r = chat.grade_pick(OSU, OSU, BSU, -13.5, winner=OSU, status="final", away_pts=41, home_pts=46)
    assert r["result"] == "WIN" and r["label"] == "Oregon State +13.5", r
    assert "lost the game 41-46, by 5" in r["why"] and "13 or fewer" in r["why"], r


def test_favorite_wins_but_fails_to_cover_is_loss():
    r = chat.grade_pick(BSU, OSU, BSU, -13.5, winner=OSU, status="final", away_pts=41, home_pts=46)
    assert r["result"] == "LOSS" and r["label"] == "Boise State -13.5", r
    assert "won the game by 5" in r["why"] and "14 or more" in r["why"], r


def test_underdog_wins_outright_and_favorite_covers():
    assert chat.grade_pick(WF, WF, SMU, 1.5, winner=WF, status="final", away_pts=49, home_pts=7)["result"] == "WIN"
    # line +1.5 = home SMU gets 1.5, so Wake Forest is the -1.5 favorite
    r = chat.grade_pick(WF, WF, SMU, 1.5, winner=WF, status="final", away_pts=49, home_pts=7)
    assert r["label"] == "Wake Forest -1.5" and "won by 42" in r["why"], r
    r = chat.grade_pick(SMU, WF, SMU, 1.5, winner=WF, status="final", away_pts=49, home_pts=7)
    assert r["result"] == "LOSS" and "lost by 42" in r["why"], r
    r = chat.grade_pick(SMU, WF, SMU, 1.5, winner=SMU, status="final", away_pts=20, home_pts=24)
    assert r["result"] == "WIN" and "won outright 24-20 as the underdog" in r["why"], r


def test_push_goes_to_away():
    # home -3 wins by exactly 3: home + line == away, the league grades Away
    away = chat.grade_pick(UTSA, UTSA, NT, -3, winner=UTSA, status="final", away_pts=20, home_pts=23)
    home = chat.grade_pick(NT, UTSA, NT, -3, winner=UTSA, status="final", away_pts=20, home_pts=23)
    assert away["result"] == "WIN" and home["result"] == "LOSS"
    assert "exactly on the number" in away["why"] and "push" in home["why"]


def test_winner_beats_score_math():
    # graded winner is the truth; a stale score never flips the result
    r = chat.grade_pick(OSU, OSU, BSU, -13.5, winner=OSU, status="final", away_pts=10, home_pts=46)
    assert r["result"] == "WIN" and r["why"] == "covered", r


def test_in_progress_and_not_started():
    r = chat.grade_pick(UTSA, UTSA, NT, -2.5, status="in_progress", away_pts=14, home_pts=3)
    assert r["result"] == "IN PROGRESS" and r["why"] == "currently covering", r
    r = chat.grade_pick(NT, UTSA, NT, -2.5, status="in_progress", away_pts=14, home_pts=3)
    assert r["why"] == "currently NOT covering", r
    assert chat.grade_pick(NT, UTSA, NT, -2.5, status="scheduled")["result"] == "NOT STARTED"


# ---- a week-11 snapshot: Chuck P 2-0 on decided games, Picky 0-1 ---------------------------

def ctx_week11():
    games = [
        {"id": 1, "away": WF, "home": SMU, "line": 1.5, "status": "final", "winner": WF, "tiebreaker": False},
        {"id": 2, "away": OSU, "home": BSU, "line": -13.5, "status": "final", "winner": OSU, "tiebreaker": False},
        {"id": 3, "away": UTSA, "home": NT, "line": -2.5, "status": "in_progress", "winner": None, "tiebreaker": False},
    ]
    live = [{"GameId": 1, "Away": WF, "Home": SMU, "AwayPts": 49, "HomePts": 7, "Status": "final"},
            {"GameId": 2, "Away": OSU, "Home": BSU, "AwayPts": 41, "HomePts": 46, "Status": "final"},
            {"GameId": 3, "Away": UTSA, "Home": NT, "AwayPts": 14, "HomePts": 3, "Status": "in_progress"}]
    mk = lambda picks: [{"game_id": g, "pick": p, "by_slots": False, "jackpot": False} for g, p in picks]  # noqa: E731
    return {
        "current_week": 11,
        "you": {"team_id": 1, "team_name": "Chuck P"},
        "leader": {"team_id": 1, "team_name": "Chuck P"},
        "standings_ytd": [{"rank": 1, "team": "Chuck P", "w": 28, "l": 18},
                          {"rank": 2, "team": "Ackbar", "w": 26, "l": 20}],
        "weekly_records": {"Chuck P": {10: [2, 2], 11: [2, 0]}, "Ackbar": {11: [1, 1]}},
        "games_this_week": games,
        "live_games": live,
        "revealed_picks_this_week": {"Chuck P": mk([(1, WF), (2, OSU), (3, UTSA)]),
                                     "Ackbar": mk([(1, SMU), (2, OSU), (3, NT)])},
        "picky_player": {"name": "Picky", "season": [30, 20], "last_week": [2, 2], "hidden_count": 0,
                         "this_week": [{"pick": BSU, "away": OSU, "home": BSU, "line": -13.5, "winner": OSU,
                                        "status": "final", "tiebreaker": False}]},
    }


def test_results_block_for_caller():
    f = chat.results_facts(ctx_week11(), "how did my Oregon State pick do?")
    b = chat.results_block(f)
    assert "- Oregon State +13.5 = WIN (covered: lost the game 41-46, by 5" in b, b
    assert "- Wake Forest -1.5 = WIN" in b, b
    assert "- UTSA +2.5 = IN PROGRESS (currently covering)" in b, b
    assert "RECORD THIS WEEK ON DECIDED GAMES: 2-0, 1 still in progress" in b, b
    assert "PICKY'S OWN" not in b  # not asked about Picky


def test_results_block_for_picky_only_his_games():
    b = chat.results_block(chat.results_facts(ctx_week11(), "what's Picky's record this week?"))
    assert "- Boise State -13.5 = LOSS (did not cover: won the game by 5 (46-41)" in b, b
    assert "PICKY'S RECORD THIS WEEK ON DECIDED GAMES: 0-1" in b, b
    picky_part = b.split("PICKY'S OWN")[1]
    assert "Wake Forest" not in picky_part  # he never picked it


def test_member_asking_about_chuck():
    ctx = ctx_week11()
    ctx["you"] = {"team_id": 2, "team_name": "Ackbar"}
    b = chat.results_block(chat.results_facts(ctx, "how did Chuck's Oregon State pick do?"))
    assert "CHUCK P'S PICKS THIS WEEK (not the user" in b and "Oregon State +13.5 = WIN" in b, b
    assert "THE USER'S (Ackbar's)" in b


def test_privacy_hidden_picks_never_graded():
    ctx = ctx_week11()
    ctx["revealed_picks_this_week"]["Ackbar"] = []  # not revealed yet
    ctx["you"] = {"team_id": 1, "team_name": "Chuck P"}
    b = chat.results_block(chat.results_facts(ctx, "how did Ackbar do this week?"))
    assert "ACKBAR'S PICKS" not in b


def test_not_a_results_question():
    assert chat.results_facts(ctx_week11(), "Can you help me do my taxes?") is None


# ---- the guard ---------------------------------------------------------------------------

def facts(q="how did my Oregon State pick do?"):
    return chat.results_facts(ctx_week11(), q)


def test_guard_flags_the_live_repro_answers():
    p = chat.check_answer("Boise State won by 5 points, so your pick did NOT cover. You're 0-2 this week.", facts())
    assert any("pick lost, but it is a WIN" in x for x in p) and any("0-2" in x for x in p), p
    p = chat.check_answer("You took Oregon State +13.5 and lost by 5 points. You're 2-2 this week while I'm 2-0. "
                          "Boise State covered the spread by 5 points.", facts())
    assert any("Boise State covered" in x for x in p), p
    assert any("2-2" in x for x in p) and any("Picky is 2-0" in x for x in p), p
    p = chat.check_answer("I'm 0-1. That's my record: 1-3.", facts("what's Picky's record this week?"))
    assert p == ["says Picky is 1-3, but the record this week is 0-1"], p


def test_guard_passes_correct_answers():
    ok = ["Oregon State +13.5 is a WIN: they lost 46-41 but only needed to lose by 13 or fewer. "
          "You're 2-0 on decided games this week, 28-18 on the season. Boise State didn't cover.",
          "Your Oregon State pick cashed. Boise won the game by 5 but missed the number.",
          "You went 2-2 last week, and you're 2-0 this week."]
    for a in ok:
        assert chat.check_answer(a, facts()) == [], (a, chat.check_answer(a, facts()))
    assert chat.check_answer("I'm 0-1 this week and 30-20 on the season. Boise State -13.5 is a LOSS.",
                             facts("what's Picky's record this week?")) == []


def test_guard_reverse_direction():
    p = chat.check_answer("Boise State covered easily.", facts())
    assert p == ["says Boise State covered, but Boise State did NOT cover"], p
    p = chat.check_answer("Oregon State failed to cover.", facts())
    assert p == ["says Oregon State did not cover, but Oregon State COVERED"], p


def test_guard_game_winner_flip():
    p = chat.check_answer("My Boise State -13.5 pick lost the game 46-41, so I didn't cover.",
                          facts("what's Picky's record this week?"))
    assert any("Boise State lost the game, but Boise State WON" in x for x in p), p
    p = chat.check_answer("Oregon State won the game, so you covered.", facts())
    assert any("Oregon State won the game" in x for x in p), p
    ok = "Boise State won the game by 5 but didn't cover. Oregon State lost by 5 and still covered."
    assert chat.check_answer(ok, facts()) == [], chat.check_answer(ok, facts())


def test_guard_objects_and_pretext():
    assert chat.check_answer("Wake Forest beat SMU and covered easy.", facts()) == []
    assert chat.check_answer("Wake Forest crushed SMU, covered by a mile.", facts()) == []
    p = chat.check_answer("Meanwhile, you lost the Wake Forest game by 42 points, but still won the pick.", facts())
    assert any("Wake Forest lost the game" in x for x in p), p
    assert chat.check_answer("Boise State won by five, so my -13.5 pick lost the cover.",
                             facts("what's Picky's record this week?")) == []


def test_guard_but_clause():
    assert chat.check_answer("Boise State won, but your underdog pick still hit.", facts()) == []
    p = chat.check_answer("Boise State lost by 5, so Chuck P covered the spread.", facts())
    assert p and all("Boise State lost the game" in x for x in p), p


def test_guard_value_is_not_a_result():
    assert chat.check_answer("Oregon State +13.5 is a WIN. You took the wrong side of value, but you still got "
                             "a point.", facts()) == []


def test_guard_flags_invented_line():
    allowed = chat.allowed_line_numbers("POOL LINE: Boise State -13.5 / Oregon State +13.5\n"
                                        "ESPN market line now: Boise State -17.5, over/under 65.5")
    assert chat.check_answer("The market had Boise -17.5.", None, allowed) == []
    p = chat.check_answer("The market had Boise -20.5.", None, allowed)
    assert p == ["states a line -20.5 that isn't in the data"], p
    assert chat.check_answer("You're 2-0, up 41-7.", None, allowed) == []  # records/scores aren't lines


def test_template_is_true():
    f = facts()
    t = chat.templated_answer(f)
    assert "Oregon State +13.5 is a WIN" in t and "You're 2-0" in t, t
    assert chat.check_answer(t, f) == [], t
    f = facts("what's Picky's record this week?")
    t = chat.templated_answer(f)
    assert "Boise State -13.5 is a LOSS" in t and "I'm 0-1" in t and chat.check_answer(t, f) == [], t


def test_guard_flags_a_pick_pinned_on_the_wrong_person():
    f = facts("what's Picky's record this week?")
    p = chat.check_answer("I took UTSA +2.5 and they're rolling.", f)  # UTSA is the caller's pick, not Picky's
    assert any("Picky took UTSA" in x for x in p), p
    assert chat.check_answer("I took Boise State -13.5 and it lost.", f) == [] or \
        not any("took Boise" in x for x in chat.check_answer("I took Boise State -13.5 and it lost.", f))
    p2 = chat.check_answer("You took Boise State, genius.", facts())
    assert any("the user took Boise State" in x for x in p2), p2
    assert not any("took" in x for x in chat.check_answer("You took Oregon State +13.5 and it's a WIN.", facts()))
    assert not any("took" in x for x in chat.check_answer(
        "You took Oregon State +13.5 and they lost to Boise State by 5.", facts()))  # object, not a pick
    p3 = chat.check_answer("You took the value side in Wake Forest and Boise State.", facts())
    assert any("the user took Boise State" in x for x in p3) and not any("took Wake Forest" in x for x in p3), p3


def _run_guard(drafts: list[list[str]]) -> tuple[str, int]:
    """Drive _guarded_stream with fake model drafts (lists of chunks).
    Returns (all content, index of the chunk where content first appeared)."""
    import asyncio
    import json as _json
    calls = iter(drafts)

    async def fake_stream(convo, prefix=None):
        for c in next(calls):
            yield chat._sse({"content": c})
        yield "data: [DONE]\n\n"

    async def go():
        real = chat._model_stream
        chat._model_stream = fake_stream
        try:
            out = [c async for c in chat._guarded_stream([{"role": "user", "content": chat.results_block(facts())}], None, facts())]
        finally:
            chat._model_stream = real
        return "".join(_json.loads(c[6:]).get("content", "") for c in out if c.startswith("data: {"))

    return asyncio.run(go())


def test_guard_streams_clean_answer_whole():
    a = _run_guard([["Oregon State +13.5 is a WIN. ", "You're 2-0 on decided games. ", "Enjoy it."]])
    assert a == "Oregon State +13.5 is a WIN. You're 2-0 on decided games. Enjoy it.", a


def test_guard_drops_bad_sentence_keeps_the_rest_and_appends_facts():
    a = _run_guard([["Nice week. ", "Your Oregon State pick didn't cover. ", "Sad."]])
    assert a.startswith("Nice week. Sad. For the record:") and "Oregon State +13.5 was a WIN" in a, a
    assert "didn't cover" not in a, a


def test_guard_bad_first_sentence_keeps_going():
    a = _run_guard([["Your Oregon State pick didn't cover. ", "Ouch."]])
    assert a.startswith("Ouch. For the record:") and "didn't cover" not in a, a


def test_guard_regenerates_only_when_nothing_survives():
    a = _run_guard([["Your Oregon State pick didn't cover."],
                    ["Oregon State +13.5 is a WIN, lucky you."]])
    assert a == "Oregon State +13.5 is a WIN, lucky you.", a


def test_prefix_untouched_by_results():
    # results ride in the user turn only; the prompt prefix must not depend on them
    import inspect
    assert "results_block" not in inspect.getsource(chat.prompt_prefix)


if __name__ == "__main__":
    fails = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok   {name}")
            except AssertionError as e:
                fails += 1
                print(f"FAIL {name}: {e}")
    sys.exit(1 if fails else 0)
