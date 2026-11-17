"""
Pick advice on a real slate (live 2026-11-16, week 12): 'gimme a read on this week's games' only listed
the games, 'who should I pick' said 'Iowa -6.5, the only value side' (lines don't lock until Tue 11 AM CT,
and Northwestern -2.5 vs -3 was a gap too) plus 'try not to let that 4-0 week go to your head'.

Covers: card reasons non-empty when data exists, pre-lock vs post-lock value wording (cards, claim check),
slate routing (all open games, one compact line each, the strongest play), single-game asks get the full
card, the record jab blocked on advice (tic + check + history scrub), hedged QB reports = questionable.
No network, no GPU, no claude -p.

Run:  ./.venv/bin/python3 -I test_picks_slate.py
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
import tempfile
from pathlib import Path

os.environ["PICKY_CFBD_DB"] = "/nonexistent/cfbd.sqlite"  # synthetic data only
TMP = Path(tempfile.mkdtemp(prefix="picky-slate-"))
os.environ["PICKY_GAME_CARD_DIR"] = str(TMP)
sys.path.insert(0, str(Path(__file__).resolve().parent))
import chat  # noqa: E402
import game_cards as gc  # noqa: E402
import no_repeat  # noqa: E402
import pick_brief  # noqa: E402

CT = gc.CT
LIVE_CARDS = Path(__file__).resolve().parent / "cache" / "game_cards"
BT = {"span": "2025-26", "strategies": {"value_1.5+": {"w": 20, "n": 33, "pct": 0.606},
                                        "value_0.5-1": {"w": 20, "n": 43, "pct": 0.465}}}
# week 12 as it stood Monday: league line (home spread), market now/open
SLATE = [(401858530, "Northwestern", "Minnesota", 2.5, 3.0, 3.0, "2026-11-21T11:00:00"),
         (401858527, "Iowa", "Illinois", 6.5, 8.5, 5.5, "2026-11-21T13:30:00"),
         (401856754, "LSU", "Tennessee", 1.5, 1.5, 1.5, "2026-11-21T14:30:00"),
         (401856871, "Texas Tech", "Baylor", 3.5, 3.5, 1.5, "2026-11-21T14:30:00")]
MON = "2026-11-16T16:47"
WED = "2026-11-18T09:00"


def at(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s).replace(tzinfo=CT)


def build(row, now=MON):
    gid, away, home, line, mk, op, start = row
    g = {"GameId": gid, "Away": away, "Home": home, "line": line, "Status": None, "tiebreaker": None,
         "fpi_margin": None, "Start (CT)": start, "week": 12}
    notes = {"market": {"home_now": mk, "home_open": op, "book": "DraftKings"}}
    return gc.build_card(g, 2026, 12, notes=notes, idx=None, extras=None, dossier=None, news=None,
                         table=[], tend={}, bt=BT, now=at(now))


def write_slate(now=MON):
    cards = []
    for row in SLATE:
        c = build(row, now)
        c["generated_at"] = dt.datetime.now(CT).isoformat(timespec="seconds")
        p = gc.path_for(12, row[0])
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(c))
        cards.append(c)
    return cards


def ctx(asker="Chuck P"):
    games = [{"id": r[0], "away": r[1], "home": r[2], "line": r[3], "start": r[6], "status": None, "winner": None,
              "tiebreaker": None} for r in SLATE]
    return {"current_week": 12, "games_this_week": games, "you": {"team_name": asker, "team_id": 5}}


class Clock:
    """chat's clock + the cards' clock pinned to one moment."""

    def __init__(self, s: str):
        self.t = dt.datetime.fromisoformat(s)

    def __enter__(self):
        self.o1, self.o2, self.o3 = chat._now_ct_naive, gc.now_ct, chat.week_locked
        chat._now_ct_naive = lambda: self.t
        gc.now_ct = lambda: self.t.replace(tzinfo=CT)
        chat.week_locked = lambda c: False
        return self

    def __exit__(self, *a):
        chat._now_ct_naive, gc.now_ct, chat.week_locked = self.o1, self.o2, self.o3


# ---- 1. reasons --------------------------------------------------------------------------------

def test_card_reasons_non_empty_when_data_exists():
    for row in SLATE:
        c = build(row)
        if any(f["side"] for f in c["factors"]):  # a factor picked a side: the lean has reasons
            assert c["reasons"], (row, c["reasons"])
        assert len(c["reasons"]) <= gc.MAX_REASONS
    iowa = build(SLATE[1])
    assert iowa["lean"]["label"] == "Iowa -6.5" and any(r.startswith("line value") for r in iowa["reasons"])
    assert any(r.startswith("line move: opened Iowa -5.5, now Iowa -8.5") for r in iowa["reasons"])


def test_live_week_cards_have_two_to_four_reasons():
    wk = LIVE_CARDS / "12"
    files = sorted(wk.glob("*.json")) if wk.exists() else []
    for p in files:  # the real cards on this machine (skipped where there's no cache)
        c = json.loads(p.read_text())
        assert 2 <= len(c["reasons"]) <= gc.MAX_REASONS, (p.name, c["reasons"])
        assert all(r.strip() and "Source:" not in r for r in c["reasons"]), c["reasons"]
    print(f"     ({len(files)} live week-12 cards checked)")


def test_hedged_qb_report_is_questionable_not_out():
    t = ("Illinois may turn to third-string QB Boyd after Houser injury. Houser had nose surgery Monday, "
         "his status in limbo.")
    assert pick_brief.injury_status(t) == "questionable"
    assert pick_brief.injury_status("Houser is out for the season after surgery") == "out"
    assert pick_brief.injury_status("anything", "out") == "out"  # an explicit status wins
    assert pick_brief._QB_RANK["out"] > pick_brief._QB_RANK["questionable"]


def test_league_line_move_makes_the_card_due():
    c = build(SLATE[1])
    g = {"GameId": 401858527, "Away": "Iowa", "Home": "Illinois", "line": 6.5, "Status": None,
         "Start (CT)": "2026-11-21T13:30:00"}
    c["generated_at"] = at(MON).isoformat()
    assert gc.due_reason(g, c, None, at("2026-11-16T18:00")) is None
    assert gc.due_reason({**g, "line": 8.5}, c, None, at("2026-11-16T18:00")) == "line"


# ---- 2. pre-lock vs post-lock ---------------------------------------------------------------------

def test_pre_lock_gap_is_conditional_post_lock_is_value():
    iowa = build(SLATE[1])
    with Clock("2026-11-16T17:00"):
        pre = "\n".join(gc.card_lines(iowa, None, True))
    assert "Line gap (NOT value yet): the league has Iowa -6.5 and the market's at Iowa -8.5 (DraftKings); if " \
           "that holds through Tuesday's 11 AM CT line lock, that's 2 points of value on Iowa" in pre, pre
    assert "good side of the key number 7" in pre and "not locked yet" in pre
    assert "real value" not in pre and "  Value:" not in pre
    assert "I like it if the league line holds through Tuesday's lock" in pre or iowa["confidence"]["word"] != "best play"
    # the same Monday card read Wednesday: the lock has passed, so it's real value now
    with Clock(WED):
        post = "\n".join(gc.card_lines(iowa, None, True))
    assert "Value: the league line is locked at Iowa -6.5 and the market's at Iowa -8.5" in post, post
    assert "2 points of real value on Iowa" in post and "if that holds" not in post
    assert gc.is_locked(iowa, dt.datetime(2026, 11, 17, 11, 0)) and not gc.is_locked(iowa, dt.datetime(2026, 11, 17, 10, 59))


def test_half_point_gap_across_three_reads_as_half_a_point():
    nw = build(SLATE[0])
    assert nw["value"] and nw["value"]["side"] == "Northwestern"
    vt = gc.value_text(nw, False)
    assert "half a point of value on Northwestern" in vt and "key number 3" in vt and vt.startswith("the league has")


def test_case_reasons_about_the_gap_drop_before_the_lock():
    case = {"reasons": [{"text": "The market's at Iowa -8.5 and our pool hands us -6.5, so we're getting two free points."},
                        {"text": "Iowa's offense is #28 in success rate against the #114 Illinois defense."}]}
    assert gc._case_reasons(case, False) == ["Iowa's offense is #28 in success rate against the #114 Illinois defense."]
    assert len(gc._case_reasons(case, True)) == 2


def test_value_claim_check():
    c = ctx()
    c["espn"] = {401858530: {"gap": (2.5, 0.5)}, 401858527: {"gap": (6.5, 2.0)}, 401856754: {"gap": (1.5, 0.0)},
                 401856871: {"gap": (3.5, 0.0)}}
    orig = chat._market_gap
    chat._market_gap = lambda notes, line: notes.get("gap")
    try:
        with Clock("2026-11-16T17:00"):
            ok = chat.value_claim_check(c)
            assert not ok("You should definitely stick with Iowa -6.5 since that's the only value side on the board.")
            assert not ok("The rest of these games have no line value, so you're gambling on vibes.")
            assert not ok("Iowa -6.5 gets you two free points against the market.")
            assert not ok("Iowa -6.5 is real value, not a lock.")
            assert ok("If the league line holds through Tuesday's lock, Iowa -6.5 is 2 points of value.")
            assert ok("I'd take Iowa -6.5, Illinois might be down to its third-string QB.")
        with Clock(WED):
            ok = chat.value_claim_check(c)  # locked: value is real, but two games have a gap
            assert ok("Iowa -6.5 is 2 points of real value.")
            assert not ok("Iowa -6.5 is the only value side.")
    finally:
        chat._market_gap = orig


# ---- 3. slate routing ------------------------------------------------------------------------------

SLATE_ASKS = ["Gimme a read on this weeks games", "Okay who should i pick", "what do you like this week",
              "picks?", "who should we take this weekend", "any picks", "thoughts on the slate",
              "what's your read on this week's games"]
NOT_SLATE = ["what do you like in LSU Tennessee", "is Iowa -6.5 a good pick", "who did Ackbar pick",
             "what's the line on Iowa", "how did my picks do last week"]


def test_slate_ask_regex():
    for q in SLATE_ASKS:
        assert chat.slate_ask(q), q
    for q in NOT_SLATE:
        assert not chat.slate_ask(q), q


def test_slate_routes_to_all_four_and_single_game_to_one():
    write_slate()
    with Clock("2026-11-16T17:00"):
        for q in SLATE_ASKS[:4]:
            assert [g["away"] for g in chat.take_games(ctx(), q)] == ["Northwestern", "Iowa", "LSU", "Texas Tech"], q
        assert [g["away"] for g in chat.take_games(ctx(), "what do you like in LSU Tennessee")] == ["LSU"]
        assert [g["away"] for g in chat.take_games(ctx(), "is Iowa -6.5 a good pick")] == ["Iowa"]
        assert chat.take_games(ctx(), "who did Ackbar pick") == []


def test_slate_note_one_line_per_game_and_strongest_play():
    write_slate()
    with Clock("2026-11-16T17:00"):
        note = gc.card_note(ctx(), "Okay who should i pick")
        single = gc.card_note(ctx(), "is Iowa -6.5 a good pick")
    assert note.startswith("SLATE READ, all 4 open games"), note
    lines = [ln for ln in note.split("\n") if ln.startswith("- ")]
    assert len(lines) == 4 and all("I'd take" in ln for ln in lines), lines
    assert "- Iowa at Illinois: I'd take Iowa -6.5" in note and "Northwestern -2.5" in note
    assert "STRONGEST PLAY: Iowa -6.5 (Iowa at Illinois)." in note
    assert "only value if it holds through the lock" in note and "never call anything 'the only value side'" in note
    assert "No jab about their record" in note and "GAME CARD" not in note
    assert note.count("if that holds through Tuesday") == 2  # Northwestern and Iowa: the two gaps
    assert single.startswith("GAME CARD, Iowa at Illinois") and "SLATE READ" not in single
    assert "THE PLAY: I'd take Iowa -6.5" in single and "Line gap (NOT value yet)" in single


def test_strongest_prefers_confidence_then_margin():
    a = {"game": {"away": "A", "home": "B"}, "lean": {"side": "A", "label": "A -3", "score": {"A": 9, "B": 1}},
         "confidence": {"word": "lean"}}
    b = {"game": {"away": "C", "home": "D"}, "lean": {"side": "C", "label": "C -6", "score": {"C": 5, "D": 3}},
         "confidence": {"word": "best play"}}
    assert gc.strongest([a, b]) is b and gc.strongest([a]) is a and gc.strongest([]) is None


# ---- 4. the record jab -------------------------------------------------------------------------------

JAB = "Try not to let that 4-0 week go to your head too much."


def test_record_jab_is_a_tic_everywhere():
    assert no_repeat.banned_tic(JAB) and chat._TICS.search(JAB)
    assert not chat.sentence_ok(JAB, set())
    hist = "You should definitely stick with Iowa -6.5. " + JAB
    assert chat.scrub_tics(hist) == "You should definitely stick with Iowa -6.5."
    assert chat.scrub_tics(JAB) == JAB  # never empties a turn


def test_advice_jab_check_only_on_advice():
    write_slate()
    with Clock("2026-11-16T17:00"):
        chk = chat.advice_jab_check(ctx(), "Okay who should i pick")
        assert chk is not None
        assert not chk(JAB)
        assert not chk("You're 4-0 this week, so don't get greedy.")
        assert not chk("Sitting in first place, you can afford to play it safe.")
        assert chk("I'd take Iowa -6.5; Illinois may be on its third-string QB.")
        assert chk("Texas Tech (AP #12, 9-1) wants style points.")  # a team's record isn't a jab
        assert chat.advice_jab_check(ctx(), "you suck picky") is None  # not advice: banter rules apply


if __name__ == "__main__":
    fails = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok   {name}")
            except Exception as e:  # noqa: BLE001
                fails += 1
                import traceback
                traceback.print_exc()
                print(f"FAIL {name}: {e!r}")
    sys.exit(1 if fails else 0)
