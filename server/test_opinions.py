"""
Deterministic tests for Charlie's opinions rider (Bo Nix, Marcus Freeman):
it rides only when the topic comes up, never on DaddyFreeman, and opinion
talk isn't read as off-topic, a picks question or a pool question.

Run:  ./.venv/bin/python3 -I test_opinions.py
"""

from __future__ import annotations

import sys
from pathlib import Path

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
    for q in ["What do you think of Marcus Freeman?", "Freeman made the title game"]:
        assert chat.FREEMAN_RIDER in chat.opinion_note(q), q


def test_homer_triggers():
    """Notre Dame / independents bias (Charlie, 2026-11-14): only on topic."""
    for q in ["Notre Dame is back.", "Who's your favorite team?", "What do you think of UConn?",
              "Who do you root for?", "Are the independents any good?", "Go Irish", "ND is overrated",
              "Freeman took Notre Dame to the national title game though. Give him some credit."]:
        assert chat.HOMER_RIDER in chat.opinion_note(q, CTX), q
    for q in ["What's the line on North Dakota State at Hawai'i?", "Who's winning the pool?",
              "What do you think of Marcus Freeman?", "Roast DaddyFreeman"]:
        assert chat.HOMER_RIDER not in chat.opinion_note(q, CTX), q


def test_homer_is_opinion_only():
    assert chat.INDEPENDENTS == ("Notre Dame", "UConn")  # 2026 FBS independents (CFBD FBS data)
    assert "never change a line, record, score, stat, standing or value call" in chat.HOMER_RIDER
    assert "follows the data" in chat.HOMER_RIDER
    assert "in spite of" in chat.HOMER_RIDER and "in spite of" in chat.FREEMAN_RIDER
    # the rider is per-question: never in the shared, cached prompt prefix
    assert "YOUR BIAS" not in chat._system_prompt(CTX)
    src = (Path(__file__).parent / "picky_picks.py").read_text()
    assert "HOMER" not in src and "opinion_note" not in src, "auto-picks must stay unbiased"


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
    # a real pick question about a non-pool team with no GAME CARD still gets the pool rail ...
    import slate_cards
    real = slate_cards.covers
    try:
        slate_cards.covers = lambda q, ctx: False
        assert chat.guard_note(CTX, "Should I take Notre Dame this week?").startswith("NOT IN THE POOL")
        # ... but with a GAME CARD (slate_cards.py) it's real data and gets answered (genius exam 2026-11-16)
        slate_cards.covers = lambda q, ctx: True
        assert not chat.guard_note(CTX, "Should I take Notre Dame this week?").startswith("NOT IN THE POOL")
    finally:
        slate_cards.covers = real



def _kelly_file(rows):
    """Point chat at a temp kelly_calls.json (the real one changes as Charlie reports assignments)."""
    import json
    import tempfile
    f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    json.dump(rows, f)
    f.close()
    chat.KELLY_CALLS_FILE = Path(f.name)


KELLY_ROWS = [{"date": "2026-11-27", "teams": ["Nebraska"], "aliases": ["Huskers"], "source": "test"}]


def test_kelly_mention_gets_the_jab():
    """Charlie 2026-11-27: Picky hates Brian Kelly. His name (or BK) brings the rider, in opinion_note."""
    import datetime as dt
    _kelly_file([])
    for q in ["What do you think of Brian Kelly?", "kelly is a clown", "BK on the call again?"]:
        assert chat.KELLY_RIDER in chat.opinion_note(q), q
        assert "KELLY ON THE CALL (" not in chat.kelly_note(q, dt.date(2026, 11, 27)), q  # no known game: none claimed
    for q in ["Chip Kelly's offense is fun", "i need a bk whopper", "Who wins Nebraska vs Iowa?"]:
        assert chat.KELLY_RIDER not in chat.opinion_note(q), q


def test_kelly_called_game_gets_the_mute_joke():
    """A game on the known list brings the mute line, even without his name; only that game, only that day."""
    import datetime as dt
    _kelly_file(KELLY_ROWS)
    d = dt.date(2026, 11, 27)
    for q in ["Who wins Nebraska vs Iowa?", "how are the huskers looking", "What's the score in the Nebraska game"]:
        n = chat.kelly_note(q, d)
        assert "KELLY ON THE CALL (" in n and "Nebraska" in n and "mute" in n, q
        assert chat.KELLY_RIDER not in n, q  # just the game: the hate bit rides only when he comes up
    assert "Nebraska" in chat.kelly_note("Is Brian Kelly calling anything today?", d)  # known list only
    assert chat.kelly_note("Who wins Nebraska vs Iowa?", dt.date(2026, 11, 28)) == ""  # past game: nothing


def test_kelly_never_on_an_unlisted_game():
    """He's never said to be calling a game that isn't on the list, and unrelated asks get nothing."""
    import datetime as dt
    _kelly_file(KELLY_ROWS)
    d = dt.date(2026, 11, 27)
    for q in ["Who wins Northwestern vs Illinois?", "What's the score of the Iowa State game?",
              "how does the jackpot work", "best pizza in Chicago?", "Who's winning the pool?"]:
        assert chat.kelly_note(q, d) == "", q
        assert "Kelly" not in chat.opinion_note(q), q
    n = chat.kelly_note("BK is the worst, who is he calling?", d)
    assert "Northwestern" not in n and "Iowa" not in n and n.count("KELLY ON THE CALL (") == 1, n
    _kelly_file([])
    assert "KELLY ON THE CALL (" not in chat.kelly_note("Is Brian Kelly on the Nebraska game?", d)
    _kelly_file("not a list")
    assert chat.kelly_calls(d) == []


def test_kelly_fact_lane_voice_cue():
    """Code-lane answers (live scores) get the mute cue on the voice line only for a known Kelly game."""
    import router
    assert "mute-the-TV joke about Kelly" in router.fact_user("nebraska score?", "0-0.", kelly=True)
    assert "Kelly" not in router.fact_user("iowa state score?", "0-0.")
    assert router.voice_line_ok("Kelly on the call? Mute button, immediately.", "Nebraska and Iowa are 0-0.",
                                {"games_this_week": [{"away": "Nebraska", "home": "Iowa"}]})

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
