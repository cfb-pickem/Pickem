"""
Deterministic tests for league lore (lore.py) and its rider (chat.lore_note).
Synthetic data only: no Supabase, no model.

Run:  ./.venv/bin/python3 -I test_lore.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, ".")
import chat  # noqa: E402
import lore  # noqa: E402
from test_leader import ctx  # noqa: E402

TEAMS = [{"team_id": 1, "team_name": "Ann Smith", "active": True, "is_exhibition": False},
         {"team_id": 2, "team_name": "Bo", "active": True, "is_exhibition": False},
         {"team_id": 26, "team_name": "Picky", "active": True, "is_exhibition": True}]


def g(gid, season, week, away, home, ap, hp, line, winner, tb=False):
    return {"GameId": gid, "cfb_season": season, "week": week, "Away": away, "Home": home, "AwayPts": ap,
            "HomePts": hp, "line": line, "winner": winner, "Status": "final", "tiebreaker": tb,
            "Start (CT)": f"{season}-09-{week + 1:02d}T12:{gid % 60:02d}:00"}


GAMES = [
    g(1, 2026, 0, "Utah", "Iowa", 20, 10, -7, "Utah"),        # Utah +7 won outright: upset hit
    g(2, 2026, 1, "Navy", "Army", 14, 13, -10.5, "Navy"),     # Army -10.5 lost outright: blown pick
    g(3, 2026, 1, "Ohio St.", "Penn State", 30, 20, 3, "Ohio State"),
    g(4, 2025, 3, "A", "B", 10, 20, -3, "B"),
    g(5, 2025, 16, "C", "D", 10, 20, -3, "D"),                # playoff week: never counts
    g(6, 2026, 2, "E", "F", 0, 0, 0, "E", tb=True),           # tiebreaker: not in season records
]
PICKS = [
    {"team_id": 1, "game_id": 1, "pick": "Utah", "jackpot": False},
    {"team_id": 2, "game_id": 1, "pick": "Iowa", "jackpot": False},
    {"team_id": 1, "game_id": 2, "pick": "Navy", "jackpot": False},
    {"team_id": 2, "game_id": 2, "pick": "Army", "jackpot": False},
    {"team_id": 1, "game_id": 3, "pick": "Ohio State", "jackpot": False},  # 'Ohio St.' == 'Ohio State'
    {"team_id": 2, "game_id": 3, "pick": "Typo U", "jackpot": False},      # matches neither side: a miss
    {"team_id": 1, "game_id": 4, "pick": "B", "jackpot": False},
    {"team_id": 2, "game_id": 4, "pick": "A", "jackpot": True},            # jackpot counts as a win
    {"team_id": 1, "game_id": 5, "pick": "D", "jackpot": False},
    {"team_id": 26, "game_id": 1, "pick": "Utah", "jackpot": False},       # exhibition: ignored
]


def comp():
    return lore.compute({"teams": TEAMS, "games": GAMES, "picks": PICKS})


def test_records_match_chat_grading():
    c = comp()
    s26 = {r["team"]: (r["w"], r["l"]) for r in c["standings"][2026]}
    assert s26 == {"Ann Smith": (3, 0), "Bo": (0, 3)}, s26  # week 0 counts; a typo pick is a miss
    assert "Picky" not in c["members"]
    assert {r["team"]: r["wins"] for r in c["site_wins"][2025]} == {"Ann Smith": 1, "Bo": 1}  # week 16 dropped


def test_upsets_blown_h2h():
    m = comp()["members"]
    assert any("Utah +7 covered by 17 and won outright 20-10" in u for u in m["Ann Smith"]["upsets"]), m["Ann Smith"]
    assert any("Army -10.5 missed the cover by 11.5, lost outright 13-14" in b for b in m["Bo"]["blown"]), m["Bo"]
    assert m["Ann Smith"]["h2h_all"]["Bo"] == [4, 0]  # incl. 2025; a jackpot hit is still a hit for Bo


def test_phrase_names_every_line_and_champions():
    out = lore.phrase(comp(), {2024: {"Bo": 20, "Ann Smith": 25}}, [{"season": 2025, "team": "Bo"}], "2026-11-15")
    for t, lines in out["members"].items():
        assert all(ln.startswith(t) for ln in lines), lines
    assert any("Bo is a past league champion" in ln for ln in out["members"]["Bo"])
    cur = [ln for ln in out["members"]["Ann Smith"] if "2026" in ln.split(":")[0]][0]
    assert "so far (through 2026-11-15)" in cur and "3-0" not in cur.split(":")[0], cur  # live owns W-L
    assert "LAST SEASON (2025) the champion was Bo" in out["league"][0], out["league"]


def test_site_champions_parse():
    ch = lore.champions()
    assert {"season": 2024, "team": "Breaking Horrendous"} in ch and {"season": 2025, "team": "Kevin Culligan"} in ch
    assert all(c["team"].upper() != "TBD" for c in ch)


def test_lore_note_rides_only_when_relevant():
    data = {"as_of": "2026-11-15", "league": ["League champions (CFB Genius), from the site: LAST SEASON (2025) the champion was Clam."],
            "members": {"Clam": ["Clam is a past league champion (CFB Genius): 2025. Respect that.",
                                 "Clam, 2026 so far (through 2026-11-15): best week 4-0 (week 2)."],
                        "Ackbar": ["Ackbar, 2026 so far (through 2026-11-15): worst week 0-4 (week 3)."]},
            "h2h": {"Clam": {"Ackbar": [5, 7]}, "Ackbar": {"Clam": [7, 5]}}}
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "lore.json"
        f.write_text(json.dumps(data))
        real = chat.LORE_FILE
        chat.LORE_FILE = f
        try:
            c = ctx(you=("Ackbar", 11))
            assert "(2025) the champion was Clam" in chat.lore_note(c, "Who won last year?")
            n = chat.lore_note(c, "How am I doing head to head vs Clam?")
            assert "Clam vs Ackbar" in n or "Ackbar vs Clam" in n, n
            assert "past league champion" in chat.lore_note(c, "What do you think of Clam?")  # respect, always
            assert chat.lore_note(c, "What games are on this week?") == ""
            assert chat.lore_note(c, "Roast Clam for being bald") == ""  # trait bait: no lore
        finally:
            chat.LORE_FILE = real


def test_blackjack_totals_and_blunders():
    # card = rank + 13*suit; rank 0 = ace, 9-12 = 10/J/Q/K
    assert lore.hand_total([0, 12]) == (21, True)          # A K
    assert lore.hand_total([0, 0, 8]) == (21, True)        # A A 9 -> 11+1+9
    assert lore.hand_total([9, 11, 5]) == (26, False)      # 10 Q 6: bust
    calls = []

    def fake_get_all(c, path):
        calls.append(path)
        if path.startswith("blackjack_bankrolls"):
            return [{"team_id": 1, "chips": 400, "hands_played": 2, "busted": False}]
        if path.startswith("blackjack_hands"):
            return [{"team_id": 1, "week": 3, "bet": 100, "doubled": False, "cards": [9, 7, 4],  # 10 8 = 18, hit -> 5
                     "state": "settled", "outcome": "lose", "payout": 0},
                    {"team_id": 1, "week": 4, "bet": 50, "doubled": False, "cards": [1, 5],    # 2 6 = 8, stood
                     "state": "settled", "outcome": "lose", "payout": 0}]
        return []
    real = lore._get_all
    lore._get_all = fake_get_all
    try:
        out = lore.casino("k", {1: "Ann Smith"}, 2026)
    finally:
        lore._get_all = real
    L = " ".join(out["members"]["Ann Smith"])
    assert "400 chips after 2 hands" in L and "hit on a hard 18 in week 3" in L and "busted" in L, L
    assert "stood on 8 in week 4" in L, L
    assert all("state=eq.settled" in c for c in calls if c.startswith("blackjack_hands")), calls  # settled only


def test_casino_note_gating():
    data = {"as_of": "2026-11-15", "league": [], "members": {}, "h2h": {},
            "casino": {"league": ["The Pit 2026 chip leaders: Clam 1,500."],
                       "members": {"Clam": ["Clam's blackjack blunders: hit on a hard 18 in week 3."]}}}
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "lore.json"
        f.write_text(json.dumps(data))
        real = chat.LORE_FILE
        chat.LORE_FILE = f
        try:
            c = ctx(you=("Ackbar", 11))
            assert "chip leaders: Clam 1,500" in chat.casino_note(c, "Who has the most chips?")
            assert "hard 18" in chat.casino_note(c, "Roast Clam's blackjack game")
            assert chat.casino_note(c, "Who's winning the pool?") == ""
            assert chat.casino_note(c, "Roast Clam's blackjack, he's fat") == ""  # bait: guardrail only
        finally:
            chat.LORE_FILE = real


def test_stats_page_oddities():
    """Charlie 2026-11-16: roast material from the stats page, computed, only bad numbers, 0-fers first."""
    def r(season, away, home, side, hit, line=-3):
        return {"team": "Ann Smith", "side": side, "hit": hit,
                "g": {"cfb_season": season, "Away": away, "Home": home, "line": line}}
    mine = ([r(2026, "Indiana", f"X{i}", "Indiana", False) for i in range(3)]          # 0-3 taking Indiana
            + [r(2025, "Indiana", f"Y{i}", f"Y{i}", False) for i in range(3)]          # 0-3 against Indiana
            + [r(2026, "Oklahoma", f"Z{i}", f"Z{i}", i == 0) for i in range(5)]        # 1-4 fading Oklahoma
            + [r(2026, "Georgia", f"W{i}", "Georgia", True) for i in range(6)])        # 6-0 on Georgia: good
    out = lore.oddities("Ann Smith", mine, [2025, 2026])
    assert out[0] == ("Ann Smith is 0-6 in Indiana games over the last two seasons (2025-2026), whichever side: 0-3 taking Indiana, "
                      "0-3 picking against them."), out
    assert "Ann Smith has picked against Oklahoma 5 times over the last two seasons (2025-2026) and gone 1-4." in out, out
    assert not any("Georgia" in x for x in out), out
    road = [r(2026, f"A{i}", f"H{i}", f"A{i}", i < 3, line=3) for i in range(12)]          # 3-9 on road dogs
    road += [r(2026, f"B{i}", f"C{i}", f"C{i}", i < 6, line=-3) for i in range(8)]          # 6-2 at home
    out = lore.oddities("Ann Smith", road, [2026])
    assert "Ann Smith 2026: 3-9 on road teams, 6-2 on home teams." in out, out
    c = comp()
    assert all("oddities" in m for m in c["members"].values())


if __name__ == "__main__":
    fails = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok   {name}")
            except Exception as e:  # noqa: BLE001
                fails += 1
                print(f"FAIL {name}: {e!r}"[:600])
    sys.exit(1 if fails else 0)
