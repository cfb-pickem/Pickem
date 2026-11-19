"""
Case ledger (plan phase 3: case_ledger.py + the pushback lanes pick / why / other_side / change_mind).

- Every pick_brief factor with a source and a side becomes a reason row {side, weight, text, facts_used, kind},
  ranked by the hand weights (ledger B12: nothing fitted). Context factors (weight 0), the asker-specific
  standings factor and factors with no source never become reasons; a film-room reason with a projection number
  ('SP+ has X by 10') never does either (no model-written projection numbers).
- pick (turn 1) = the lean + the top 2 reasons; 'why' walks the rest with no repeats and says it's out when it is.
- other_side: that side's top 2 reasons, never one of ours.
- change_mind: computed triggers: the line through the nearest key number against us, a QB status flip on an
  availability note, wind over 20 mph for the total.
- Every number in every pushback answer is backed by its facts_used (factstore.unsupported == []).

Synthetic cards/briefs (no network, no model) plus the frozen fact-store fixture for the store-backed lanes.

Run:  ./.venv/bin/python3 -I test_case_ledger.py
"""

from __future__ import annotations
import os
os.environ["PICKY_LANES_OFF"] = ""  # these tests exercise the held-back lanes (router.LANES_OFF_DEFAULT)

import re
import sys

sys.path.insert(0, ".")
import case_ledger as CL  # noqa: E402
import factstore as FS  # noqa: E402
import game_cards  # noqa: E402
import pick_brief as PB  # noqa: E402
import router  # noqa: E402
import units  # noqa: E402
import test_factstore as TF  # noqa: E402

TT, BAY = "Texas Tech", "Baylor"


def F(n, side, w, why):
    return {"n": n, "name": PB.NAMES[n], "side": side, "w": float(w), "why": why}


FACTORS = [
    F(49, TT, 2, "SP+ (ratings + 2.5 home field): projects Texas Tech by 6.2, so Texas Tech -3.5 covers by 2.7"),
    F(2, TT, 1, "line move: opened Texas Tech -1.5, now Texas Tech -3.5, the market moved 2 points toward Texas Tech"),
    F(18, TT, 1, "bye: Texas Tech is off a bye (13 days rest) vs Baylor on 7"),
    F(23, TT, 1, "revenge: Texas Tech lost this game last season by 10"),
    F(26, TT, 0.5, "title stakes: Texas Tech (AP #12, 9-1) is in the playoff race, Baylor (6-4) isn't"),
    F(40, TT, 0.5, "run game (rushing PPA): Texas Tech offense #12 vs Baylor defense #88"),
    F(8, BAY, 1, "QB status: Texas Tech QB Behren Morton is questionable (ankle, per ESPN Nov 16)"),
    F(30, BAY, 1, "travel: Texas Tech crosses 2 time zones for an early kick"),
    F(37, BAY, 1, "EPA (season PPA per play): projects Baylor by 0.9, so Baylor +3.5 covers by 4.4"),
    F(17, BAY, 0.5, "team ATS: Baylor is 6-2 ATS as an underdog this season (overall 7-3)"),
    F(51, BAY, 0.5, "head-to-head: Baylor covered the last meeting by 9"),
    F(33, BAY, 0.5, "wind: forecast (Open-Meteo): 50F, wind 18 mph, wind shortens games, a tilt to the points (Baylor)"),
    F(9, None, 0, "backup QB: Texas Tech's next QB by attempts is Will Hammond (88 passes this season)"),
    F(60, TT, 1, "standings need: you're 2 back of the cut, so take the side the league fades"),
]


def card(**over):
    fs = [f for f in FACTORS if f["n"] != 60]
    tt = sorted((f for f in fs if f["side"] == TT), key=lambda f: (-f["w"], f["n"]))
    bay = sorted((f for f in fs if f["side"] == BAY), key=lambda f: (-f["w"], f["n"]))
    c = {"game": {"away": TT, "home": BAY, "id": 7, "line": 3.5, "start": "2099-11-21T11:00:00"},
         "generated_at": "2099-11-16T18:00:00-06:00",
         "league_line": {"home": 3.5, "locked": False, "lock_at": "2099-11-17T11:00:00", "text": "Texas Tech -3.5 / Baylor +3.5"},
         "market": None, "value": None,
         "lean": {"side": TT, "label": "Texas Tech -3.5", "strength": "lean", "score": {TT: 6.0, BAY: 4.5}},
         "reasons": [f["why"] for f in tt][:4], "against": [f["why"] for f in bay][:2],
         "factors": fs, "confidence": {"word": "slight lean", "say": "a slight lean at best"},
         "case": {"pick": TT, "by": "opus", "reasons": [
             {"text": "SP+ has Texas Tech by about 10 once Morton plays."},
             {"text": "Baylor's secondary has allowed the most completions of 40-plus yards in the Big 12."}]}}
    c.update(over)
    return c


PG = {"id": 7, "away": TT, "home": BAY, "line": 3.5}
CTX = {"games_this_week": [PG], "current_week": 12}


class Patch:
    """Point the lanes at the synthetic card / brief, no unit edges unless asked."""

    def __init__(self, c=None, edges=None, brief=None):
        self.c, self.edges, self.brief = c, edges or {}, brief

    def __enter__(self):
        import slate_cards
        self.saved = (router._card_for, game_cards.is_locked, slate_cards.find_game, units.edges_for, FS.brief,
                      FS.find_game)
        router._card_for = lambda ctx, g: self.c
        game_cards.is_locked = lambda c, now=None: False
        row = {"id": 7, "awayTeam": TT, "homeTeam": BAY, "startDate": "2099-11-21T17:00:00Z", "lines": []}
        slate_cards.find_game = lambda text, ctx, idx=None: (row, PG if self.c else None)
        FS.find_game = lambda q, ctx: (row, PG if self.c else None)
        units.edges_for = lambda side, a, h, tab=None: list(self.edges.get(side) or [])
        FS.brief = lambda row, notes=None: self.brief
        return self

    def __exit__(self, *a):
        import slate_cards
        (router._card_for, game_cards.is_locked, slate_cards.find_game, units.edges_for, FS.brief,
         FS.find_game) = self.saved


def brief():
    g = {"id": 7, "away": TT, "home": BAY, "line": 3.5, "start": "2099-11-21T11:00:00"}
    return PB.score(g, [dict(f, flag=False) for f in reversed(FACTORS)])


# --- the ledger -------------------------------------------------------------------------------------------------

def test_rows_have_the_contract_shape_and_kinds():
    with Patch(card(), edges={TT: ["Texas Tech's run game (#12) vs Baylor's run defense (#88): edge Texas Tech"]}):
        led = CL.from_card(card())
    assert led["rows"], led
    for r in led["rows"]:
        assert {"side", "weight", "text", "facts_used", "kind"} <= set(r), r
        assert r["facts_used"] and all(f["source"] for f in r["facts_used"]), r
    kinds = {r["kind"] for r in led["rows"]}
    assert {"QB status", "ATS split", "unit edge", "spot", "H2H", "weather", "travel", "stakes", "line move"} <= kinds, kinds


def test_rows_ranked_by_hand_weights():
    led = CL.from_brief(brief())
    for side in (TT, BAY):
        ws = [r["weight"] for r in CL.reasons(led, side, units=False)]
        assert ws == sorted(ws, reverse=True), (side, ws)
    assert CL.reasons(led, TT)[0]["kind"] == "ratings", CL.reasons(led, TT)[0]


def test_context_standings_and_unsourced_factors_never_become_reasons():
    led = CL.from_brief(brief())
    raws = [r["raw"] for r in led["rows"]]
    assert not any(x.startswith(("backup QB:", "standings need:")) for x in raws), raws
    assert all(PB.STATUS.get(r["n"], ("none",))[0] != "none" for r in led["rows"] if r.get("n")), raws


def test_film_room_projection_numbers_never_become_reasons():
    led = CL.from_card(card())
    texts = " | ".join(r["text"] for r in led["rows"])
    assert "by about 10" not in texts, texts
    assert any(r["kind"] == "film room" for r in led["rows"]), texts


def test_lean_label_and_word():
    led = CL.from_card(card())
    assert led["lean"] == TT and led["labels"] == {TT: "Texas Tech -3.5", BAY: "Baylor +3.5"} and led["word"] == "slight lean"
    m = CL.from_brief(brief())
    assert m["lean"] == TT and m["labels"][BAY] == "Baylor +3.5" and m["word"] in ("coin flip", "slight lean"), m["word"]


def test_report_text_is_cut_to_the_source_and_truncated_pieces_drop():
    g = {"id": 9, "away": "LSU", "home": "Tennessee", "line": 1.5, "start": "2099-11-21T14:30:00"}
    fs = [dict(F(8, "LSU", 1, "QB status: Tennessee QB George MacIntyre is questionable (Tennessee QB George MacIntyre hurt "
                              "his right knee at Texas A&M; Josh Heupel said he is working toward playing vs LSU,..., "
                              "Yahoo Sports)"), flag=False),
          dict(F(10, "LSU", 0.5, "key injuries (Tennessee): Tennessee's top wide receiver Mike Matthews and big-play "
                                 "running back Daune Morris are...; Tennessee RB Daune Morris is out for the season"),
               flag=False),
          dict(F(10, "Tennessee", 0.5, "key injuries (LSU): LSU LB Harold Perkins sustained the..."), flag=False)]
    with Patch(None):
        led = CL.from_brief(PB.score(g, fs))
    qb = next(r for r in led["rows"] if r["kind"] == "QB status")
    assert qb["text"] == "Tennessee QB George MacIntyre is questionable (per Yahoo Sports)", qb["text"]
    inj = [r for r in led["rows"] if r["kind"] == "injuries"]
    assert [r["text"] for r in inj] == ["Tennessee RB Daune Morris is out for the season"], [r["text"] for r in inj]


# --- pick + why -----------------------------------------------------------------------------------------------

def test_pick_turn_one_is_the_lean_plus_top_two_reasons():
    with Patch(card()):
        out = router.fact_answer("pick", CTX, "who you got in Texas Tech at Baylor?")
    top = [r["text"] for r in CL.top(CL.from_card(card()), TT, 2)]
    assert out and out.startswith("I'd take Texas Tech -3.5 (slight lean): "), out
    assert all(t.rstrip(".") in out for t in top) and len(top) == 2, (out, top)
    assert FS.unsupported(out, out.facts_used) == [], FS.unsupported(out, out.facts_used)


def test_pick_on_a_non_pool_game_reads_the_market_ledger():
    import slate_cards
    saved = slate_cards.covers
    slate_cards.covers = lambda q, ctx: True  # a non-pool game the full brain would have taken before
    try:
        with Patch(None, brief=brief()):
            out = router.fact_answer("pick", {"games_this_week": [], "current_week": 12},
                                     "who you got in Texas Tech at Baylor?")
    finally:
        slate_cards.covers = saved
    assert out and out.startswith("I'd take Texas Tech -3.5"), out
    assert FS.unsupported(out, out.facts_used) == [], (out, FS.unsupported(out, out.facts_used))


def test_three_whys_never_repeat_a_reason_and_then_say_its_out():
    with Patch(card(), edges={TT: ["Texas Tech's run game (#12) vs Baylor's run defense (#88): edge Texas Tech"]}):
        t1 = router.fact_answer("pick", CTX, "who you got in Texas Tech at Baylor?")
        said = [t1]
        outs = []
        for _ in range(3):
            a = router.fact_answer("why", CTX, "why? ||| " + " ||| ".join(reversed(said)))
            assert a, said
            outs.append(a)
            said.append(a)
        led = CL.from_card(card())
        seen = {}
        for i, a in enumerate([t1] + outs):
            for r in CL.reasons(led, TT):
                if r["text"].rstrip(".") in a:
                    assert r["text"] not in seen, (r["text"], seen.get(r["text"]), i)
                    seen[r["text"]] = i
        more = []
        for _ in range(6):
            a = router.fact_answer("why", CTX, "why? ||| " + " ||| ".join(reversed(said)))
            more.append(a)
            said.append(a)
            if "all I've got" in a:
                break
        assert "all I've got" in more[-1], more
        for a in outs + more:
            assert FS.unsupported(a, getattr(a, "facts_used", None)) == [], (a, FS.unsupported(a, a.facts_used))


def test_why_never_says_its_out_while_a_fresh_reason_is_left():
    """A unit edge that shares a number with an earlier row (#12, #88) must not mark the film room row before it as
    said: the walk reaches every fresh row before 'that's all I've got'."""
    with Patch(card(), edges={TT: ["Texas Tech's run game (#12) vs Baylor's run defense (#88): edge Texas Tech"]}):
        said = [router.fact_answer("pick", CTX, "who you got in Texas Tech at Baylor?")]
        for _ in range(4):
            a = router.fact_answer("why", CTX, "why? ||| " + " ||| ".join(reversed(said)))
            said.append(a)
            if "all I've got" in a:
                break
    assert any("secondary" in a for a in said), said


def test_why_lane_tests_still_hold_without_factors():
    """test_units' shape: a card with reasons but no factors list still walks without repeats."""
    c = card(factors=[], case={})
    with Patch(c):
        t1 = "I'd take Texas Tech -3.5 (slight lean): SP+ has Texas Tech by about 6."
        a2 = router.fact_answer("why", CTX, "why? ||| " + t1)
        assert a2 and a2.startswith("For Texas Tech -3.5: beyond the SP+ edge,"), a2


# --- other side -----------------------------------------------------------------------------------------------

def test_other_side_gives_their_top_two_never_ours():
    with Patch(card()):
        t1 = router.fact_answer("pick", CTX, "who you got in Texas Tech at Baylor?")
        out = router.fact_answer("other_side", CTX, "ok what's the best case for the other side? ||| " + t1)
    led = CL.from_card(card())
    ours = [r["text"].rstrip(".") for r in CL.reasons(led, TT)]
    theirs = [r["text"].rstrip(".") for r in CL.top(led, BAY, 2, units=True)]
    assert out and "Baylor +3.5" in out and all(t in out for t in theirs), (out, theirs)
    case = out.split("I'm still on", 1)[0]  # the hold tail says what keeps Picky on his side (one of ours)
    assert not any(t in case for t in ours), out
    assert "Texas Tech -3.5" in out, "says where Picky still is"
    assert FS.unsupported(out, out.facts_used) == [], FS.unsupported(out, out.facts_used)


def test_argue_for_a_named_side_and_devils_advocate():
    with Patch(card()):
        a = router.fact_answer("other_side", CTX, "argue for Baylor")
        b = router.fact_answer("other_side", CTX, "play devil's advocate ||| I'd take Texas Tech -3.5 (slight lean): x.")
    assert a and a.startswith("Best case for Baylor +3.5"), a
    assert b and b.startswith("Best case for Baylor +3.5"), b


def test_other_side_with_no_reasons_says_so():
    c = card(factors=[f for f in card()["factors"] if f["side"] != BAY], against=[], case={})
    with Patch(c):
        out = router.fact_answer("other_side", CTX, "devil's advocate ||| I'd take Texas Tech -3.5 (slight lean): x.")
    assert out and "no real case for Baylor +3.5" in out, out


def test_other_side_after_a_non_pool_pick():
    with Patch(None, brief=brief()):
        t1 = router.pick_text({"games_this_week": []}, "who you got in Texas Tech at Baylor?")
        out = router.fact_answer("other_side", {"games_this_week": []}, "best case for the other side ||| " + t1)
    assert out and "Baylor +3.5" in out and "projects Texas Tech" not in out, out
    assert FS.unsupported(out, out.facts_used) == []


# --- change my mind ---------------------------------------------------------------------------------------------

def test_line_trigger_is_the_nearest_key_number_against_us():
    t = CL.line_trigger
    assert t(-6.5)["target"] == -7.5 and t(-6.5)["key"] == 7, t(-6.5)   # laying 6.5: 7.5 crosses 7
    assert t(-7)["target"] == -7.5 and t(-7)["key"] == 7, t(-7)         # on 7: the hook against us
    assert t(-2.5)["target"] == -3.5 and t(-2.5)["key"] == 3
    assert t(3.5)["target"] == 2.5 and t(3.5)["key"] == 3, t(3.5)        # getting 3.5: 2.5 loses the 3
    assert t(10)["target"] == 9.5 and t(10)["key"] == 10
    assert t(13.5)["target"] == 9.5 and t(13.5)["key"] == 10, t(13.5)
    assert t(-14.5)["target"] == -17.5 and t(-14.5)["key"] == 17
    assert t(1.5)["target"] == 0 and t(1.5)["key"] is None, t(1.5)       # a dog under 3: a pick'em flips it


def test_qb_trigger_from_an_availability_note():
    notes = [{"team": BAY, "status": "questionable", "players": ["Sawyer Robertson"],
              "text": "Baylor QB Sawyer Robertson (shoulder) is questionable, per ESPN.", "source_name": "ESPN",
              "date": "2026-11-16"},
             {"team": TT, "status": "out", "players": ["Coy Eakin"], "text": "Texas Tech WR Coy Eakin is out.",
              "source_name": "ESPN", "date": "2026-11-16"}]
    ts = CL.qb_triggers(notes, ours=TT, other=BAY)
    assert len(ts) == 1 and "Sawyer Robertson" in ts[0]["text"] and re.search(r"\bplays?\b|cleared", ts[0]["text"]), ts
    ts = CL.qb_triggers(notes, ours=BAY, other=TT)
    assert len(ts) == 1 and "ruled out" in ts[0]["text"], ts
    assert CL.qb_triggers(notes[1:], ours=TT, other=BAY) == [], "a WR note is not a QB flip"


def test_season_ending_qb_is_no_trigger_and_one_per_team():
    notes = [{"team": BAY, "status": "out", "players": ["Tait Reynolds"], "text": "Baylor QB Tait Reynolds suffered a "
              "season-ending meniscus tear and had surgery.", "source_name": "Yahoo", "date": "2026-11-16"},
             {"team": BAY, "status": "out", "players": ["Al Smith"], "text": "Baylor QB Al Smith (ankle) is out this week.",
              "source_name": "Yahoo", "date": "2026-11-16"},
             {"team": BAY, "status": "out", "players": ["Bo Jones"], "text": "Baylor QB Bo Jones (hand) is out.",
              "source_name": "Yahoo", "date": "2026-11-16"}]
    ts = CL.qb_triggers(notes, ours=TT, other=BAY)
    assert [t["fact"]["subject"] for t in ts] == ["Al Smith"], ts


def test_wind_trigger_only_for_a_total_outdoors():
    w = CL.wind_trigger({"temp_f": 40, "wind_mph": 12, "as_of": "x", "src": "Open-Meteo forecast"}, 47.5)
    assert w and "20 mph" in w["text"] and "47.5" in w["text"] and "under" in w["text"], w
    w2 = CL.wind_trigger({"temp_f": 40, "wind_mph": 24, "as_of": "x", "src": "Open-Meteo forecast"}, 47.5)
    assert w2 and "24 mph" in w2["text"] and "already" in w2["text"], w2
    assert CL.wind_trigger(None, 47.5) is None and CL.wind_trigger({"temp_f": 40, "wind_mph": 5}, None) is None


def _fixture_store():
    FS.use(TF.built()["path"], TF.inputs(), now=TF.NOW)


def test_change_mind_lane_on_the_frozen_store():
    """LSU at Tennessee in the fixture: Tennessee +1.5, total 45.5, Tennessee's QB questionable (Yahoo)."""
    g = {"id": 401856754, "away": "LSU", "home": "Tennessee", "line": 1.5, "start": "2026-11-21T14:30:00"}
    b = PB.score(g, [dict(F(49, "LSU", 2, "SP+ (ratings + 2.5 home field): projects LSU by 4.1, so LSU -1.5 covers by 2.6"),
                          flag=False),
                     dict(F(17, "Tennessee", 0.5, "team ATS: Tennessee is 5-2 ATS at home"), flag=False)])
    _fixture_store()
    saved = FS.brief
    FS.brief = lambda row, notes=None: b
    try:
        out = router.fact_answer("change_mind", {"games_this_week": []},
                                 "what would change your mind? ||| I'd take LSU -1.5 (slight lean): SP+ has LSU by about 4.")
        out2 = router.fact_answer("change_mind", {"games_this_week": []}, "what flips you on Tennessee LSU")
    finally:
        FS.brief = saved
        FS.use(None, None)
    assert out and "LSU -3.5" in out and "3" in out, out                       # the key number against us
    assert "MacIntyre" in out and "cleared" in out, out                         # the QB note on their side
    assert "20 mph" in out and "45.5" in out, out                               # wind for the total
    assert FS.unsupported(out, out.facts_used) == [], (out, FS.unsupported(out, out.facts_used))
    assert out2 and FS.unsupported(out2, out2.facts_used) == [], out2


def test_no_projection_number_in_any_pushback_output_without_a_fact():
    outs = []
    with Patch(card(), edges={BAY: ["Baylor's pass rush (#7 in sack rate) vs Texas Tech's protection (#57): edge Baylor"]}):
        t1 = router.fact_answer("pick", CTX, "who you got in Texas Tech at Baylor?")
        outs += [t1, router.fact_answer("why", CTX, "why? ||| " + t1),
                 router.fact_answer("why", CTX, "why not Baylor? ||| " + t1),
                 router.fact_answer("other_side", CTX, "devil's advocate ||| " + t1)]
    with Patch(None, brief=brief()):
        t2 = router.pick_text({"games_this_week": []}, "who you got in Texas Tech at Baylor?")
        outs += [t2, router.fact_answer("other_side", {"games_this_week": []}, "argue for Baylor ||| " + t2)]
    for o in outs:
        assert o and getattr(o, "facts_used", None), o
        assert FS.unsupported(o, o.facts_used) == [], (o, FS.unsupported(o, o.facts_used))
        for m in re.finditer(r"\bby (?:about )?(\d+(?:\.\d)?)\b", o):  # every 'X by N' projection is a fact's number
            assert any(m.group(1) in (f.get("text", "") + str(f.get("value", ""))) for f in o.facts_used), (m.group(0), o)


# --- routing --------------------------------------------------------------------------------------------------

def test_router_classifies_pushback_phrasings():
    for q in ("what's the best case for the other side?", "devil's advocate?", "play devil's advocate", "argue for Baylor",
              "make the case for Nebraska", "steelman the other side", "ok give me the case against your pick"):
        assert router.classify(q).label() == "fact:other_side", (q, router.classify(q).label())
    for q in ("what would change your mind?", "what flips you?", "what would make you flip", "what would make you change your pick",
              "what gets you off Duke"):
        assert router.classify(q).label() == "fact:change_mind", (q, router.classify(q).label())
    for q in ("why?", "why not Baylor?"):
        assert router.classify(q).label() == "fact:why", q
    assert {"other_side", "change_mind"} <= router.CTX_KINDS


def test_pushback_lanes_are_store_lanes():
    names = {f.__name__ for f in router.STORE_LANES}
    assert {"other_side_text", "change_mind_text", "ats_profile_text"} <= names, names


if __name__ == "__main__":
    fails = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok   {name}")
            except Exception as e:  # noqa: BLE001
                fails += 1
                print(f"FAIL {name}: {e!r}"[:1200])
    print("ALL PASS" if not fails else f"{fails} FAILED")
    sys.exit(1 if fails else 0)
