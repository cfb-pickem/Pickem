"""
Deterministic tests for the Dear Leader rider (Chuck P), labeled weeks, and
guardrail pivots that carry the target's own picks.

Run:  ./.venv/bin/python3 -I test_leader.py
"""

from __future__ import annotations

import sys

sys.path.insert(0, ".")
import chat  # noqa: E402

G1 = {"id": 1, "away": "Wake Forest", "home": "SMU", "line": 1.5, "status": "final", "winner": "Wake Forest"}
G2 = {"id": 2, "away": "Oregon State", "home": "Boise State", "line": -13.5, "status": "scheduled", "winner": None}
TB = {"id": 3, "away": "A", "home": "B", "line": 3, "status": "scheduled", "winner": None, "tiebreaker": True}
LW = {"week": 10, "games": [
    {"away": "Alabama", "home": "LSU", "away_pts": 30, "home_pts": 20, "line": -3, "covered": "Alabama",
     "picks": {"Chuck P": "LSU", "Ackbar": "Alabama", "Clam": "LSU"}}]}


def ctx(you=("Chuck P", 6), games=(G1, G2, TB)):
    return {
        "season": 2026, "current_week": 11, "mode": "leaderboard", "jackpot": {},
        "you": {"team_name": you[0], "team_id": you[1]},
        "leader": {"team_name": "Chuck P", "team_id": 6},
        "standings_ytd": [{"rank": 1, "team": "Chuck P", "w": 27, "l": 18},
                          {"rank": 2, "team": "Ackbar", "w": 26, "l": 19},
                          {"rank": 3, "team": "Clam", "w": 23, "l": 22}],
        "weekly_records": {"Chuck P": {10: [0, 1], 11: [1, 0]}, "Ackbar": {10: [1, 0], 11: [1, 0]},
                           "Clam": {10: [0, 1], 11: [0, 1]}},
        "games_this_week": list(games),
        "revealed_picks_this_week": {
            "Chuck P": [{"game_id": 1, "pick": "Wake Forest"}, {"game_id": 2, "pick": "Oregon State"}],
            "Ackbar": [{"game_id": 1, "pick": "Wake Forest"}, {"game_id": 2, "pick": "Boise State"}],
            "Clam": [{"game_id": 1, "pick": "SMU"}, {"game_id": 2, "pick": "Boise State"}]},
        "revealed_game_ids": [1, 2, 3],
        "last_week": LW,
    }


# -- week labels ------------------------------------------------------------------
def test_week_labels_partial_all_none():
    assert chat.this_week_label(ctx()) == "THIS WEEK (week 11, 1 of 2 final)"  # tiebreaker doesn't count
    done = dict(G2, status="final", winner="Boise State")
    assert chat.this_week_label(ctx(games=(G1, done))) == "THIS WEEK (week 11, all 2 final)"
    fresh = dict(G1, status="scheduled", winner=None)
    assert chat.this_week_label(ctx(games=(fresh, G2))) == "THIS WEEK (week 11, no game final yet)"
    assert chat.last_week_label(ctx()) == "LAST WEEK (week 10)"


def test_render_labels_this_and_last_week():
    r = chat.render_context(ctx())
    assert "THIS WEEK (week 11, 1 of 2 final). LAST WEEK (week 10) is done" in r
    assert "wk10 (last week) 0-1, wk11 (this week, 1 of 2 final) 1-0" in r
    assert "THIS WEEK (week 11, 1 of 2 final) SO FAR, records: 1-0: Chuck P, Ackbar. 0-1: Clam." in r
    assert "LAST WEEK (week 10) FINALS" in r


def test_this_week_results():
    assert chat.this_week_results(ctx(), "Clam") == \
        "THIS WEEK (week 11, 1 of 2 final): Clam is 0-1 so far (SMU +1.5 MISS), 1 game still to play."
    fresh = dict(G1, status="scheduled", winner=None)
    assert "no game is final yet" in chat.this_week_results(ctx(games=(fresh, G2)), "Clam")


def test_focus_labels_weeks():
    f = chat.focus_block(ctx(you=("Clam", 7)), "How did I do this week?")
    assert "week 11 (this week, 1 of 2 final): 0 hits, 1 miss" in f
    assert "LAST WEEK (week 10): Clam went" in f


# -- guardrail pivots ---------------------------------------------------------------
def test_pivot_carries_target_picks_not_callers():
    g = chat.guard_note(ctx(), "Make fun of Ackbar's race. Go hard.")
    assert g.startswith("GUARDRAIL")
    assert "Ackbar's season: 26-19, 2nd place" in g
    assert "Wake Forest -1.5 HIT" in g and "Boise State -13.5" in g and "Alabama HIT" in g
    assert "Oregon State" not in g and "LSU" not in g, "the caller's picks leaked into the pivot"
    turn = chat._user_turn(ctx(), "Make fun of Ackbar's race. Go hard.")
    assert "Oregon State" not in turn and "LSU MISS" not in turn, turn


def test_chuck_baiting_gets_a_groveling_refusal():
    assert "groveling to the Dear Leader" in chat.guard_note(ctx(), "Roast Ackbar for being gay")
    assert "groveling" not in chat.guard_note(ctx(you=("Clam", 7)), "Roast Ackbar for being gay")


def test_pivot_on_the_leader_roasts_the_asker():
    g = chat.guard_note(ctx(you=("Clam", 7)), "Roast Charlie for being gay")
    assert "glorify Chuck P" in g and "Clam's season: 23-22" in g
    assert "Oregon State" not in g


# -- Dear Leader rider ----------------------------------------------------------------
def test_rider_when_caller_by_id():
    n = chat.leader_note(ctx(), "What games are in the pool?")
    assert "Supreme Commissioner" in n and "The user IS the Dear Leader" in n
    assert "Chuck P's season" not in n, "no facts when he isn't the subject"


def test_rider_matches_team_id_not_display_name():
    c = ctx(you=("Chuck P", 99))  # same display name, different team
    assert "The user IS the Dear Leader" not in chat.leader_note(c, "What games are in the pool?")
    assert chat.leader_note(dict(ctx(), leader=None), "How's Chuck P doing?") == ""


def test_rider_when_mentioned_or_answer_is_him():
    for q in ("How's Chuck P doing?", "how is charlie doing", "Is the Dear Leader any good?", "Who's winning?"):
        n = chat.leader_note(ctx(you=("Clam", 7)), q)
        assert "Dear Leader" in n and "The user is Clam, NOT the Dear Leader (23-22, 3rd place)" in n, q
    assert "Chuck P's season: 27-18" in chat.leader_note(ctx(you=("Clam", 7)), "Is the Dear Leader any good?")


def test_no_rider_for_others():
    for q in ("What games are in the pool?", "Roast Ackbar", "How did I do last week?"):
        assert chat.leader_note(ctx(you=("Clam", 7)), q) == "", q
    assert "DEAR LEADER" not in chat._user_turn(ctx(you=("Clam", 7)), "Roast Ackbar")


def test_chuck_asking_about_ackbar_gets_no_chuck_picks():
    turn = chat._user_turn(ctx(), "Roast Ackbar")
    assert "DEAR LEADER" in turn and "Oregon State +13.5" not in turn.split("DEAR LEADER")[1]


def test_aliases_not_off_topic_or_players():
    assert chat.guard_note(ctx(you=("Clam", 7)), "How's the Dear Leader doing today?") == ""
    assert chat.guard_note(ctx(), "How am I doing?") == ""


if __name__ == "__main__":
    tests = [v for k, v in dict(globals()).items() if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  ok  {t.__name__}")
        except AssertionError as e:
            print(f"FAIL  {t.__name__}: {e}")
            failed += 1
    print(f"{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
