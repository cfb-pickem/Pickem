"""
Deterministic tests for Charlie's opinions rider (Bo Nix, Marcus Freeman):
it rides only when the topic comes up, never on DaddyFreeman, and opinion
talk isn't read as off-topic, a picks question or a pool question.

Run:  ./.venv/bin/python3 -I test_opinions.py
"""

from __future__ import annotations

import sys

sys.path.insert(0, ".")
import chat  # noqa: E402

CTX = {"season": 2026, "current_week": 11, "mode": "leaderboard", "jackpot": {},
       "you": {"team_name": "Clam", "team_id": 9}, "leader": {"team_name": "Chuck P", "team_id": 6},
       "standings_ytd": [{"rank": 1, "team": "Chuck P", "w": 27, "l": 18},
                         {"rank": 2, "team": "DaddyFreeman", "w": 26, "l": 19},
                         {"rank": 3, "team": "Clam", "w": 23, "l": 22}],
       "games_this_week": [{"id": 1, "away": "Wake Forest", "home": "SMU", "line": 1.5,
                            "status": "scheduled", "winner": None}]}


def test_nix_triggers():
    for q in ["Who's the best college QB ever?", "Bo Nix overrated?", "greatest quarterback of all time",
              "best Oregon quarterback"]:
        assert chat.NIX_RIDER in chat.opinion_note(q), q


def test_freeman_triggers():
    for q in ["What do you think of Marcus Freeman?", "Notre Dame is back.", "Freeman made the title game"]:
        assert chat.FREEMAN_RIDER in chat.opinion_note(q), q


def test_quiet_otherwise():
    for q in ["Who's winning the pool?", "Who's the best QB in this week's games?", "Roast DaddyFreeman",
              "How is Daddy Freeman doing?", "Can you help me do my taxes?"]:
        assert chat.opinion_note(q) == "", q
        assert "YOUR OPINION" not in chat._user_turn(CTX, q), q


def test_rider_facts_pinned():
    for s in ("61 career starts", "77.4%", "45 touchdowns and 3 interceptions", "Heisman finalist"):
        assert s in chat.NIX_RIDER, s
    for s in ("26-21 to unranked Marshall", "16-14 to Stanford", "16-14 to Northern Illinois", "34-23"):
        assert s in chat.FREEMAN_RIDER, s


def test_not_offtopic_or_pool_or_picks():
    for q in ["Bo Nix overrated?", "What do you think of Marcus Freeman?", "Notre Dame is back.",
              "Freeman took Notre Dame to the national title game though. Give him some credit."]:
        assert chat.guard_note(CTX, q) == "", q
        assert "LOCKED" not in chat._user_turn(CTX, q), q
    # a real pick question about a non-pool team still gets the pool rail
    assert chat.guard_note(CTX, "Should I take Notre Dame this week?").startswith("NOT IN THE POOL")


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
