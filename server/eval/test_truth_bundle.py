"""
The judges' truth bundle (eval/run.py truth_bundle) carries finals, graded ATS margins and the lore
lines Picky may cite, so a true margin ("SMU missed the cover by 40.5") is never judged "invented".

Run:  cd server && ./.venv/bin/python3 -I eval/test_truth_bundle.py

Model-free and offline: a fake Truth with week-11-shaped data; no Supabase, no Ollama.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
import run as exam  # noqa: E402
import truth  # noqa: E402

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("ok   " if ok else "FAIL ") + name + (f"  ({detail})" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


# line = points added to the home score; home covers when HomePts + line > AwayPts
GAMES = [
    {"GameId": 1, "Away": "Wake Forest", "Home": "SMU", "AwayPts": 49, "HomePts": 7, "line": 1.5,
     "winner": "Wake Forest", "week": 11, "tiebreaker": False},
    {"GameId": 2, "Away": "Oregon State", "Home": "Boise State", "AwayPts": 41, "HomePts": 46, "line": -13.5,
     "winner": "Oregon State", "week": 11, "tiebreaker": False},
    {"GameId": 3, "Away": "Alabama", "Home": "LSU", "AwayPts": 31, "HomePts": 13, "line": -5.5,
     "winner": "Alabama", "week": 10, "tiebreaker": False},
    {"GameId": 4, "Away": "Navy", "Home": "Army", "AwayPts": 20, "HomePts": 17, "line": 3,
     "winner": "Navy", "week": 9, "tiebreaker": False},
]
NAMES = {10: "Clam", 11: "TG", 12: "Chuck P", 13: "Ackbar"}
PICKS = [
    {"team_id": 10, "game_id": 1, "pick": "SMU"}, {"team_id": 11, "game_id": 1, "pick": "SMU"},
    {"team_id": 12, "game_id": 1, "pick": "Wake Forest"},
    {"team_id": 12, "game_id": 2, "pick": "Oregon St."}, {"team_id": 13, "game_id": 2, "pick": "Boise State"},
    {"team_id": 12, "game_id": 3, "pick": "LSU"},
    {"team_id": 13, "game_id": 4, "pick": "Army", "jackpot": True},
]
LORE = {"as_of": "2026-11-16",
        "members": {"Chuck P": ["Chuck P, 2026 so far: best week 4-0 (week 11).",
                                "stats-page oddity: Chuck P is 2-7 in Oklahoma games over the last two seasons"]},
        "league": ["League champions: LAST SEASON (2025) the champion was Kevin Culligan."],
        "h2h": {"Chuck P": {"TG": [16, 7]}},
        "casino": {"members": {"Chuck P": ["Chuck P, The Pit 2026: 0 chips, BUSTED OUT."]}, "league": []}}
MATERIAL = {"season": 2026, "week": 11, "facts": {"f1": "Ackbar takes the favorite 75% of the time."}}


def fake_truth() -> truth.Truth:
    t = truth.Truth.__new__(truth.Truth)
    t.season, t.week = 2026, 11
    t.standings = [{"rank": 1, "team": "Chuck P", "w": 30, "l": 18}, {"rank": 2, "team": "Ackbar", "w": 28, "l": 20}]
    t.weekly = {"Chuck P": {11: (4, 0), 10: (3, 1)}}
    t.last_week = {"week": 10, "games": []}
    t.games = [{"id": 1, "away": "Wake Forest", "home": "SMU", "line": 1.5, "status": "final",
                "winner": "Wake Forest"}]
    t.ctx = {"revealed_picks_this_week": {}}
    t.picky = {"season": "0-3", "this_week": [{"label": "Boise State -13.5", "result": "LOSS"}]}
    t.season_games, t.season_picks, t.name_by_tid = GAMES, PICKS, NAMES
    return t


def test_bundle():
    b = exam.truth_bundle(fake_truth(), lore=LORE, material=MATERIAL)
    check("final score with the straight-up margin", "Wake Forest 49 at SMU 7 (final; Wake Forest won by 42)" in b)
    check("pool line as both spreads", "Pool line Wake Forest -1.5 / SMU +1.5" in b)
    check("the true margin a member was roasted with is in the bundle (SMU +1.5 missed by 40.5)",
          "SMU missed the cover by 40.5" in b and "Took SMU +1.5 (missed by 40.5): Clam, TG" in b)
    check("winner straight up vs ATS differ (Oregon State lost by 5, covered by 8.5)",
          "Boise State won by 5" in b and "Oregon State covered by 8.5" in b
          and "Took Oregon State +13.5 (covered by 8.5): Chuck P" in b and "Took Boise State -13.5 (missed by 8.5): Ackbar" in b)
    check("an older week's final is there too (LSU -5.5, missed by 23.5)", "Took LSU -5.5 (missed by 23.5): Chuck P" in b)
    check("a push is labeled, not given a margin", "Landed on the number; the league scored it for Navy" in b
          and "Took Army +3 (push): Ackbar (jackpot pick, counts as a hit)" in b)
    check("lore stats-page oddity Picky may cite", "2-7 in Oklahoma games" in b)
    check("league history, h2h, casino and material facts", all(x in b for x in (
        "Kevin Culligan", "Chuck P vs: TG 16-7", "BUSTED OUT", "favorite 75% of the time")))
    check("standings and Picky's own entry kept", "1. Chuck P 30-18" in b and "Boise State -13.5 LOSS" in b)


def test_single_line():
    s = exam.graded_game_line(GAMES[0])
    check("graded_game_line works without picks", "SMU missed the cover by 40.5" in s and "Took" not in s)
    s = exam.graded_game_line({"Away": "A", "Home": "B", "AwayPts": None, "HomePts": None, "line": None,
                               "winner": "A", "week": 3})
    check("missing scores degrade to the league-graded winner", "covered (league-graded): A" in s)


def test_judge_uses_bundle():
    import judge_text

    async def fake_load(self):
        t = fake_truth()
        self.__dict__.update(t.__dict__)
        return self
    orig = truth.Truth.load
    truth.Truth.load = fake_load
    try:
        sheet = judge_text.facts()
    finally:
        truth.Truth.load = orig
    check("judge_text's FACT SHEET is the truth bundle (true margin present)", "missed the cover by 40.5" in sheet)


if __name__ == "__main__":
    test_bundle()
    test_single_line()
    test_judge_uses_bundle()
    print(f"\n{len(FAILS)} failure(s)" if FAILS else "\nall passed")
    sys.exit(1 if FAILS else 0)
