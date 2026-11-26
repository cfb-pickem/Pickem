"""DraftKings' look-ahead opener isn't a line move (nightly 2026-11-26, genius lines 2/10: 'Pitt went from a
7.5-point underdog to a 9.5-point favorite, that's a 17-point swing!' got 'the market moved 17 points toward
Pittsburgh'; Bovada opened Pitt -9.5 and never moved). Run: .venv/bin/python3 -I -m pytest test_early_open.py"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import market_open  # noqa: E402

PITT_CAL = [{"provider": "DraftKings", "spread": 9.5, "spreadOpen": -7.5, "overUnder": 47.5, "overUnderOpen": 47.5},
            {"provider": "Bovada", "spread": 9.5, "spreadOpen": 9.5, "overUnder": 47.5, "overUnderOpen": 47.5}]
OSU_MICH = [{"provider": "DraftKings", "spread": -10.5, "spreadOpen": -13.5, "overUnder": 37.5, "overUnderOpen": 46.5},
            {"provider": "Bovada", "spread": -10.5, "spreadOpen": -11, "overUnder": 38, "overUnderOpen": 38.5}]
IOWA_NEB = [{"provider": "DraftKings", "spread": -4.5, "spreadOpen": -6.5, "overUnder": 40.5, "overUnderOpen": 38.5},
            {"provider": "Bovada", "spread": -4.5, "spreadOpen": -5.5, "overUnder": 40.5, "overUnderOpen": 39.5}]


def test_books_far_apart_on_the_open_flag_the_early_number():
    eo = market_open.early_open(PITT_CAL)
    assert eo and eo["spread"] == -7.5 and eo["spread_open"] == 9.5 and eo["vs"] == "Bovada"


def test_a_total_far_apart_flags_it_too():
    eo = market_open.early_open(OSU_MICH)  # spreads 2.5 apart, totals 8 apart
    assert eo and eo["total"] == 46.5 and eo["total_open"] == 38.5 and eo["spread_open"] == -11


def test_books_that_agree_are_a_real_move():
    assert market_open.early_open(IOWA_NEB) is None  # Iowa -6.5 / -5.5 -> -4.5: a real 1-2 point move
    assert market_open.early_open(PITT_CAL[:1]) is None  # one book: nothing to compare with
    assert market_open.early_open([]) is None


def test_fix_swaps_the_open_and_recomputes_the_moves():
    eo = market_open.early_open(OSU_MICH)
    m = market_open.fix({"spread_home": -10.5, "open_spread_home": -13.5, "move": 3.0, "total": 37.5,
                         "open_total": 46.5, "total_move": -9.0}, eo, "open_spread_home", "spread_home",
                        "open_total", "total")
    assert m["open_spread_home"] == -11 and m["move"] == 0.5
    assert m["open_total"] == 38.5 and m["total_move"] == -1.0
    assert m["early_open"]["spread"] == -13.5


def test_fix_takes_espns_copy_of_the_early_number():
    # ESPN carried DK's USC-UCLA opener as 11.5, CFBD as 10.5; Bovada opened 7
    eo = market_open.early_open([{"provider": "DraftKings", "spread": 7, "spreadOpen": 10.5},
                                 {"provider": "Bovada", "spread": 7, "spreadOpen": 7}])
    m = market_open.fix({"now": 6.5, "open": 11.5}, eo, "open", "now")
    assert m["open"] == 7 and m["early_open"]["spread"] == 11.5


def test_fix_leaves_a_market_that_isnt_the_early_number_alone():
    eo = market_open.early_open(PITT_CAL)
    m = {"now": 9.5, "open": 9.0}
    assert market_open.fix(m, eo, "open", "now") is m
    assert market_open.fix(m, None, "open", "now") is m


def test_sentence_says_no_real_move():
    eo = market_open.early_open(PITT_CAL)
    s = market_open.sentence(eo, 9.5, "Pittsburgh", "California")
    assert "California -7.5" in s and "DraftKings' early number" in s and "Pittsburgh -9.5" in s
    assert "hasn't really moved" in s and "17" not in s


def test_line_move_lane_doesnt_sell_the_early_number_as_news(monkeypatch):
    import factstore
    import router
    g = {"id": 1, "awayTeam": "Pittsburgh", "homeTeam": "California", "lines": PITT_CAL}
    monkeypatch.setattr(factstore, "find_game", lambda q, ctx: (g, None))
    monkeypatch.setattr(factstore, "market", lambda row: {
        "spread": 9.5, "spreadOpen": 9.5, "overUnder": 47.5, "move": 0.0,
        "early_open": market_open.early_open(PITT_CAL)})
    monkeypatch.setattr(factstore, "player_status", lambda **k: [])
    monkeypatch.setattr(factstore, "availability", lambda a, h: ([], "Thu 3 AM CT"))
    monkeypatch.setattr(factstore, "news_lines", lambda t, q: [])
    monkeypatch.setattr(factstore, "lean", lambda g, q: [])
    monkeypatch.setattr(factstore, "facts", lambda g, kinds=None: [])
    monkeypatch.setattr(factstore, "qb_room", lambda t: [])
    q = "Pitt went from a 7.5-point underdog to a 9.5-point favorite at Cal, a 17-point swing! What news did I miss?"
    assert router.classify(q).label() == "fact:line_move"
    out = str(router.slate_move_text({}, q))
    assert "early number" in out and "Bovada opened Pittsburgh -9.5" in out
    assert "17" not in out and "fight" not in out and "toward Pittsburgh" not in out, out


def test_slate_card_market_line_names_the_early_number():
    import slate_cards
    g = {"awayTeam": "Illinois", "homeTeam": "Northwestern", "lines": [
        {"provider": "DraftKings", "spread": -3, "spreadOpen": 3.5, "overUnder": 50.5, "overUnderOpen": 52.5},
        {"provider": "Bovada", "spread": -3, "spreadOpen": -2.5, "overUnder": 50, "overUnderOpen": 49.5}]}
    s = slate_cards.market_line(g, {"home_now": -2.5, "home_open": 3.5, "over_under": 50.5, "book": "DraftKings"})[0]
    assert "moved 6" not in s and "no move from the open" in s and "early number" in s, s


def test_pool_card_market_uses_the_honest_open():
    import pick_brief
    row = {"homeTeam": "Ohio State", "awayTeam": "Michigan", "homeScore": None, "lines": OSU_MICH}
    th, ta = {"name": "Ohio State", "games": [row]}, {"name": "Michigan", "games": [row]}
    mk = pick_brief.market_home({"market": {"home_now": -10.5, "home_open": -13.5, "book": "DraftKings"}}, {}, th, ta)
    assert mk["open"] == -11 and mk["early_open"]["spread"] == -13.5
    mk = pick_brief.market_home({"market": {"home_now": -4.5, "home_open": -6.5, "book": "DraftKings"}}, {},
                                {"name": "Iowa", "games": [dict(row, homeTeam="Iowa", awayTeam="Nebraska",
                                                                lines=IOWA_NEB)]}, {"name": "Nebraska"})
    assert mk["open"] == -6.5 and "early_open" not in mk


def test_non_pool_brief_reads_one_market_not_two(monkeypatch):
    """A non-pool game's lean ran pick_brief with no market notes, so pick_brief took CFBD's daily copy as 'the
    market' and the store's line as 'the league': 'the league has Texas -10.5' on a game with no league line."""
    import pick_brief
    import slate_cards
    import slate_intel
    seen = {}
    monkeypatch.setattr(pick_brief, "build", lambda cg, notes, idx, extras, intel: seen.update(cg=cg, notes=notes) or {})
    monkeypatch.setattr(slate_intel, "claims_for", lambda gid: ([], None))
    monkeypatch.setattr(pick_brief, "news_intel", lambda cache, teams: [])
    g = {"id": 7, "awayTeam": "Texas", "homeTeam": "Texas A&M", "startDate": None,
         "lines": [{"provider": "ESPN DraftKings", "spread": 10.5, "spreadOpen": 11.5, "overUnder": 40.5}]}
    slate_cards.brief_for(g, {"teams": {}}, None)
    mk = seen["notes"]["market"]
    assert seen["cg"]["line"] == 10.5 and mk["home_now"] == 10.5 and mk["home_open"] == 11.5, seen


class _MP:
    """The bit of pytest's monkeypatch these tests use (the suite runs test files as plain scripts)."""
    def __init__(self):
        self.undo = []

    def setattr(self, obj, name, val):
        self.undo.append((obj, name, getattr(obj, name)))
        setattr(obj, name, val)

    def close(self):
        for obj, name, val in reversed(self.undo):
            setattr(obj, name, val)


if __name__ == "__main__":
    import inspect
    fails = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            mp = _MP()
            try:
                fn(mp) if inspect.signature(fn).parameters else fn()
                print("ok  ", name)
            except AssertionError as e:
                fails += 1
                print("FAIL", name, e)
            finally:
                mp.close()
    print("FAILS:", fails)
    sys.exit(1 if fails else 0)
