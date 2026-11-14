"""
Privacy tests for Mr. Locks's context assembly.

Run:  ./.venv/bin/python3 -I test_privacy.py

These tests pin the rule that unrevealed picks (games that have not kicked
off, by any user other than the caller) NEVER appear in the context dict
Mr. Locks sees — regardless of whether RLS would have hidden them. RLS
cannot save us if the caller is the commissioner, so we enforce here too.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import sys
from unittest.mock import patch

sys.path.insert(0, ".")
import chat  # noqa: E402


# ---- unit: the pure filter -------------------------------------------------

NOW = dt.datetime(2026, 11, 13, 12, 0, 0)  # CT, Thursday noon
STARTED = (NOW - dt.timedelta(hours=2)).isoformat(timespec="seconds")
NOT_STARTED = (NOW + dt.timedelta(days=2)).isoformat(timespec="seconds")

GAMES = [
    {"GameId": 1, "Status": "final", "Start (CT)": STARTED},
    {"GameId": 2, "Status": "scheduled", "Start (CT)": NOT_STARTED},
    {"GameId": 3, "Status": "scheduled", "Start (CT)": STARTED},  # kicked off, feed slow
]

PICKS = [
    {"team_id": 10, "game_id": 1, "pick": "Michigan", "by_slots": False},  # caller, locked
    {"team_id": 10, "game_id": 2, "pick": "Illinois", "by_slots": False},  # caller, unlocked
    {"team_id": 20, "game_id": 1, "pick": "Ohio State", "by_slots": False},  # other, locked
    {"team_id": 20, "game_id": 2, "pick": "Michigan", "by_slots": False},  # other, UNLOCKED
    {"team_id": 30, "game_id": 3, "pick": "Oregon", "by_slots": False},  # other, started-but-sched
]


def test_filter_strips_unrevealed_other_picks():
    out = chat.filter_visible_picks(PICKS, GAMES, caller_team_id=10, now_ct_naive=NOW)
    # caller's own: both visible, locked or not
    assert {"team_id": 10, "game_id": 1, "pick": "Michigan", "by_slots": False} in out
    assert {"team_id": 10, "game_id": 2, "pick": "Illinois", "by_slots": False} in out
    # other user's locked pick: visible
    assert {"team_id": 20, "game_id": 1, "pick": "Ohio State", "by_slots": False} in out
    # other user's UNLOCKED pick: must not appear
    for p in out:
        assert not (p["team_id"] != 10 and p["game_id"] == 2), (
            f"LEAK: {p}"
        )
    # game 3: scheduled status but kickoff passed → still locked, other's pick ok
    assert {"team_id": 30, "game_id": 3, "pick": "Oregon", "by_slots": False} in out


def test_no_caller_still_strips_everything_unlocked():
    """When we can't identify the caller, be maximally conservative."""
    out = chat.filter_visible_picks(PICKS, GAMES, caller_team_id=None, now_ct_naive=NOW)
    for p in out:
        game = next(g for g in GAMES if g["GameId"] == p["game_id"])
        assert chat._game_is_locked(game, NOW), f"LEAK unlocked no-caller: {p}"


# ---- integration: _build_context never emits an unrevealed pick -----------

async def _run_context_privacy_check():
    """Stub supabase REST calls and assert the output has no unlocked other-user picks."""

    teams = [
        {"team_id": 10, "team_name": "Caller", "user_id": "uuid-caller", "active": True},
        {"team_id": 20, "team_name": "Rival",  "user_id": "uuid-rival",  "active": True},
        {"team_id": 30, "team_name": "Other",  "user_id": "uuid-other",  "active": True},
    ]
    season_games_final = [
        {"GameId": 1, "winner": "Michigan", "line": -3.5, "week": 11},
    ]
    week_games = [
        {"GameId": 1, "Away": "Illinois", "Home": "Michigan", "line": -3.5,
         "Status": "final", "winner": "Michigan", "week": 11, "tiebreaker": False,
         "Start (CT)": STARTED},
        {"GameId": 2, "Away": "Ohio State", "Home": "Penn State", "line": -6.5,
         "Status": "scheduled", "winner": None, "week": 11, "tiebreaker": False,
         "Start (CT)": NOT_STARTED},
    ]

    async def fake_rest(client, path, jwt):
        if path.startswith("teams?"):
            return teams
        if path.startswith("slots_jackpot"):
            return [{"pulls_since_pop": 42, "last_pop_at": None, "last_pop_team_id": None}]
        if "order=cfb_season.desc" in path:
            return [{"cfb_season": 2026}]
        if "Status=neq.final" in path and "order=week.asc" in path:
            return [{"week": 11, "Status": "scheduled"}]
        if "week=eq.11" in path and "GameId" in path:
            return week_games
        if "picks?select=team_id,game_id,pick,by_slots,jackpot" in path:
            return [
                {"team_id": 10, "game_id": 1, "pick": "Michigan",   "by_slots": False, "jackpot": False},
                {"team_id": 10, "game_id": 2, "pick": "Ohio State", "by_slots": False, "jackpot": False},
                {"team_id": 20, "game_id": 1, "pick": "Illinois",   "by_slots": False, "jackpot": False},
                # LEAK CANDIDATE — rival's unrevealed pick:
                {"team_id": 20, "game_id": 2, "pick": "Penn State", "by_slots": False, "jackpot": False},
            ]
        if "picks?select=team_id,game_id,pick,by_slots,week" in path:
            return [{"team_id": 10, "game_id": 1, "pick": "Michigan", "by_slots": False, "week": 11}]
        if "Status=eq.final" in path:
            return season_games_final
        raise AssertionError(f"unexpected fetch: {path}")

    with patch.object(chat, "_rest_get", side_effect=fake_rest), \
         patch.object(chat, "_now_ct_naive", return_value=NOW):
        ctx = await chat._build_context(client=None, jwt="x", user_id="uuid-caller")

    revealed = ctx["revealed_picks_this_week"]
    # Caller sees own picks (locked and unlocked)
    assert any(p["game_id"] == 2 for p in revealed.get("Caller", [])), "caller own pick dropped"
    # Rival's unrevealed pick for game 2 must NOT be there
    for pick in revealed.get("Rival", []):
        assert pick["game_id"] != 2, f"LEAK: rival unrevealed pick in context: {pick}"
    # Submission-status leak check: no "teams_not_picked_yet_this_week" key
    assert "teams_not_picked_yet_this_week" not in ctx, "submission status leaked"
    # Caller's own submission bool is fine
    assert ctx["you_have_picks_this_week"] is True


def test_build_context_never_leaks_unrevealed():
    asyncio.run(_run_context_privacy_check())


# ---- runner ---------------------------------------------------------------

if __name__ == "__main__":
    tests = [
        test_filter_strips_unrevealed_other_picks,
        test_no_caller_still_strips_everything_unlocked,
        test_build_context_never_leaks_unrevealed,
    ]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  ok  {t.__name__}")
        except AssertionError as e:
            print(f"FAIL  {t.__name__}: {e}")
            failed += 1
    print()
    print("ALL GREEN" if failed == 0 else f"{failed} FAILED")
    sys.exit(0 if failed == 0 else 1)
