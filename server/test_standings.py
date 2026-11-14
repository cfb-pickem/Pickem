"""
Standings tests: Picky's season points must match the leaderboard's.

Run:  ./.venv/bin/python3 -I test_standings.py

The leaderboard (index.html computeRegularWins) scores a point when the pick
matches `winner` (the side that covered) OR the pick hit the slots jackpot,
and never counts tiebreaker games. It also only reads picks for this
season's games: `picks` has no season column, so `week=eq.11` alone pulls
last season's week 11 too.
"""

from __future__ import annotations

import asyncio
import sys
from unittest.mock import patch

sys.path.insert(0, ".")
import chat  # noqa: E402

TEAMS = [
    {"team_id": 1, "team_name": "A", "user_id": "u1", "active": True},
    {"team_id": 2, "team_name": "B", "user_id": "u2", "active": True},
]
GAMES = [
    {"GameId": 100, "winner": "Oregon State", "week": 10, "tiebreaker": False},
    {"GameId": 101, "winner": "Iowa", "week": 10, "tiebreaker": False},
    {"GameId": 102, "winner": "Utah", "week": 10, "tiebreaker": True},
]
PICKS = [
    {"team_id": 1, "game_id": 100, "pick": "Oregon St.", "jackpot": False},  # St./State
    {"team_id": 1, "game_id": 101, "pick": "Ohio State", "jackpot": True},   # jackpot auto win
    {"team_id": 1, "game_id": 102, "pick": "Utah", "jackpot": False},        # tiebreaker: no point
    {"team_id": 2, "game_id": 100, "pick": "Boise State", "jackpot": False},
    {"team_id": 2, "game_id": 101, "pick": "Iowa", "jackpot": False},
    {"team_id": 2, "game_id": 999, "pick": "Iowa", "jackpot": False},        # last season's game
]


def test_points_match_leaderboard_rules():
    rows = {r["team"]: r for r in chat.compute_standings(TEAMS, GAMES, PICKS)}
    assert rows["A"]["w"] == 2 and rows["A"]["l"] == 0, rows["A"]
    assert rows["B"]["w"] == 1 and rows["B"]["l"] == 1, rows["B"]
    assert rows["A"]["rank"] == 1 and rows["B"]["rank"] == 2


def test_ties_share_rank():
    teams = TEAMS + [{"team_id": 3, "team_name": "C", "user_id": "u3", "active": True}]
    picks = PICKS + [{"team_id": 3, "game_id": 100, "pick": "Oregon State", "jackpot": False},
                     {"team_id": 3, "game_id": 101, "pick": "Iowa", "jackpot": False}]
    rows = {r["team"]: r["rank"] for r in chat.compute_standings(teams, GAMES, picks)}
    assert rows == {"A": 1, "C": 1, "B": 3}, rows


async def _run_paging_and_season_scope():
    calls = []
    page = [{"team_id": 1, "game_id": 100, "pick": "x", "by_slots": False, "jackpot": False}]

    async def fake_rest(client, path, jwt):
        calls.append(path)
        if path.startswith("teams?"):
            return TEAMS
        if path.startswith("slots_jackpot"):
            return []
        if "order=cfb_season.desc" in path:
            return [{"cfb_season": 2026}]
        if "Status=neq.final" in path:
            return [{"week": 11, "Status": "scheduled"}]
        if path.startswith("all_games") and "week=eq.11" in path:
            return [{"GameId": 200, "Away": "X", "Home": "Y", "line": -3, "Status": "scheduled",
                     "winner": None, "week": 11, "tiebreaker": False, "Start (CT)": "2099-01-01T12:00:00"}]
        if path.startswith("all_games") and "Status=eq.final" in path:
            return GAMES
        if path.startswith("picks?"):
            # Simulate PostgREST's 1000-row cap: first page full, second short.
            if "offset=0" in path:
                return page * 1000
            return page * 3
        raise AssertionError(path)

    with patch.object(chat, "_rest_get", side_effect=fake_rest):
        await chat._build_context(client=None, jwt="x", user_id="u1")
    pick_calls = [c for c in calls if c.startswith("picks?")]
    for c in pick_calls:
        assert "game_id=in.(" in c, f"picks query not scoped to this season's games: {c}"
        assert "week=eq." not in c, f"week filter without season: {c}"
    assert any("offset=1000" in c for c in pick_calls), "season picks not paged past 1000 rows"


def test_picks_are_paged_and_season_scoped():
    asyncio.run(_run_paging_and_season_scope())


if __name__ == "__main__":
    tests = [test_points_match_leaderboard_rules, test_ties_share_rank, test_picks_are_paged_and_season_scoped]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  ok  {t.__name__}")
        except Exception as e:  # noqa: BLE001
            print(f"FAIL  {t.__name__}: {type(e).__name__}: {e}")
            failed += 1
    print()
    print("ALL GREEN" if failed == 0 else f"{failed} FAILED")
    sys.exit(0 if failed == 0 else 1)
