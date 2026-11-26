"""Phrasing bank + verdict lines for the code lanes (ontology plan Phase 1, 2026-11-18). Model-free.

The bank (server/phrasing/*.json): 6-10 surface variants per slot, same facts in every variant (same
placeholders), picked by a stable hash of (lane, slot, seed). Every variant renders every fixture, stays
short, carries no banned tic or em dash, and survives router.debot untouched. Verdict lines: one computed
"so what" per lane, with lean words (coin flip / slight lean / lean / I like it), never a lock.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.pop("PICKY_PHRASING", None)  # default: on

import phrasing  # noqa: E402
import router  # noqa: E402
import pick_strategy  # noqa: E402

LANES = ("line_move", "hypo_value", "keynum", "qb_status", "conf_race", "lookahead", "interim", "h2h", "strategy")
FIELD = re.compile(r"{(\w+)}")


def _bank(lane):
    return json.loads((Path(phrasing.BANK_DIR) / f"{lane}.json").read_text())


# ---------------------------------------------------------------- the bank itself

def test_every_lane_has_a_bank():
    for lane in LANES:
        b = _bank(lane)
        assert b["slots"] and b["fixtures"], lane


def test_six_to_ten_variants_per_slot():
    for lane in LANES:
        for slot, vs in _bank(lane)["slots"].items():
            assert 6 <= len(vs) <= 10, (lane, slot, len(vs))
            assert len(set(vs)) == len(vs), (lane, slot, "duplicate variant")


def test_same_facts_in_every_variant():
    for lane in LANES:
        for slot, vs in _bank(lane)["slots"].items():
            want = sorted(set(FIELD.findall(vs[0])))
            for v in vs:
                assert sorted(set(FIELD.findall(v))) == want, (lane, slot, v)


def test_every_variant_renders_every_fixture():
    for lane in LANES:
        b = _bank(lane)
        for fx in b["fixtures"]:
            for slot, vs in b["slots"].items():
                for v in vs:
                    out = v.format_map(fx)  # KeyError = a slot the fixture can't fill
                    assert "{" not in out and "}" not in out, (lane, slot, out)


def test_rendered_variants_are_short_and_clean():
    import no_repeat
    for lane in LANES:
        b = _bank(lane)
        for fx in b["fixtures"]:
            for slot, vs in b["slots"].items():
                for v in vs:
                    out = v.format_map(fx)
                    assert len(out) <= 700, (lane, slot, len(out))
                    assert not no_repeat.banned_tic(out), (lane, slot, out, no_repeat.banned_tic(out))
                    assert "—" not in out and "–" not in out and " - " not in out, (lane, slot, out)
                    assert not re.search(r"\b(lock of the|a lock\b|mortal lock|can'?t lose|guarantee\w*)", out, re.I), out


def test_variants_survive_debot():
    for lane in LANES:
        b = _bank(lane)
        fx = b["fixtures"][0]
        for slot, vs in b["slots"].items():
            for v in vs:
                out = v.format_map(fx)
                got = router.debot(out, "a question about the game", "fact")
                assert got.lower() == out.lower(), (lane, slot, out, got)


def test_validator_script_passes():
    assert phrasing.validate() == [], phrasing.validate()


# ---------------------------------------------------------------- picking a variant

def test_same_seed_same_wording():
    assert phrasing.say("h2h", "verdict", "seed-a") == phrasing.say("h2h", "verdict", "seed-a")


def test_seeds_spread_across_variants():
    got = {phrasing.say("h2h", "verdict", f"seed-{i}") for i in range(60)}
    assert len(got) >= 4, got


def test_flag_off_is_variant_zero():
    os.environ["PICKY_PHRASING"] = "0"
    try:
        assert not phrasing.on()
        v0 = _bank("h2h")["slots"]["met"][0]
        fx = {"yr": 2025, "win": "Duke", "score": "46-45", "where": "at Clemson"}
        assert all(phrasing.say("h2h", "met", f"s{i}", **fx) == v0.format_map(fx) for i in range(20))
    finally:
        os.environ.pop("PICKY_PHRASING", None)
    assert phrasing.on()


def test_seed_for_varies_by_asker_and_text():
    a = phrasing.seed_for({"you": {"team_name": "A"}}, "q one")
    assert a == phrasing.seed_for({"you": {"team_name": "A"}}, "q one")
    assert a != phrasing.seed_for({"you": {"team_name": "B"}}, "q one")
    assert a != phrasing.seed_for({"you": {"team_name": "A"}}, "q two")


def test_join_fits_under_the_cap():
    parts = [(0, "A" * 300 + "."), (2, "C" * 200 + "."), (1, "B" * 250 + ".")]
    out = phrasing.join(parts, limit=600)
    assert len(out) <= 600 and out.startswith("A") and "B" * 250 in out and "C" * 200 not in out
    assert phrasing.join([(0, "x."), (1, "y.")]) == "x. y."


# ---------------------------------------------------------------- verdict lines, per lane

LEAN_WORDS = re.compile(r"\b(coin flip|slight lean|lean|I like it|priced in|room|tiebreaker|doesn'?t move the number|"
                        r"wait|margin|worse number|better ticket|control)\b", re.I)


def test_line_move_verdict_priced_in_vs_room():
    v = router.move_verdict(5, True, "seed")
    assert v and "5" in v and re.search(r"priced in", v, re.I), v
    v = router.move_verdict(1.5, True, "seed")
    assert v and "1.5" in v and re.search(r"room", v, re.I), v
    assert router.move_verdict(5, False, "seed") is None  # no confirmed QB out: the card lean speaks


def test_low_total_note_only_when_it_applies():
    assert router.low_total_note(40.5, 8.5, "s")  # 8.5 in a 40.5 total is a lot
    assert router.low_total_note(62.5, 8.5, "s") is None
    assert router.low_total_note(40.5, 3, "s") is None


def test_hypo_value_verdict_names_both_sides():
    q = "Say the league locks Duke -13.5 on Tuesday and by Saturday the market's Duke -15. Did I get value on Duke or Clemson?"
    out = router.hypo_value_text({}, q)
    assert "Duke" in out and "-13.5" in out and "-15" in out and "1.5" in out and "14" in out, out
    assert "Clemson" in out and LEAN_WORDS.search(out), out
    assert len(out) <= 600


def test_hypo_value_verdict_counts_as_the_lean():
    """chat.lean_fact tacks 'Lean: X, mainly because <pasted source>' onto an answer that never names the lean side
    with a lean word (judge 11-18: 'the QB note is pasted citation text'). The verdict must count as that lean."""
    import chat
    card = ("GAME CARD Clemson at Duke\n  COMPUTED ATS LEAN (vs the current market line): Duke -15 (lean; factor score "
            "Duke 2.0)\n    + for Duke: QB status: Clemson QB Tait Reynolds is out\n")
    q = "Say the league locks Duke -13.5 on Tuesday and by Saturday the market's Duke -15. Did I get value on Duke or Clemson?"
    check, _ = chat.lean_fact(card, q)
    for i in range(40):
        os.environ["PICKY_PHRASING_SEED"] = f"lf{i}"
        try:
            out = router.hypo_value_text({}, q)
        finally:
            os.environ.pop("PICKY_PHRASING_SEED", None)
        assert check(out), out


def test_hypo_value_half_point_on_three():
    out = router.hypo_value_text({}, "League has Northwestern -2.5 at Minnesota and the books are at -3. Which side has the value?")
    assert "Northwestern" in out and "-2.5" in out and "-3" in out and "3" in out, out
    assert re.search(r"hook|half point", out, re.I) and "Minnesota" in out, out


def test_hypo_value_flag_off_is_the_old_shape():
    os.environ["PICKY_PHRASING"] = "0"
    try:
        q = "Say the league locks Duke -13.5 on Tuesday and by Saturday the market's Duke -15. Did I get value on Duke or Clemson?"
        out = router.hypo_value_text({}, q)
        assert out.startswith("Value on Duke: you'd have Duke at -13.5 when the market says -15, a 1.5-point edge"), out
        assert "Clemson" not in out  # no verdict line with the flag off
    finally:
        os.environ.pop("PICKY_PHRASING", None)


def test_keynum_verdict_picks_the_hook_on_three():
    hooks = [(7, "Indiana -7.5 at Washington", "Indiana", "Washington", True),
             (3, "Texas Tech -3.5 at Baylor", "Texas Tech", "Baylor", True)]
    v = router.keynum_verdict(hooks, "s")
    assert "Texas Tech -3.5 at Baylor" in v and "Baylor" in v and "3" in v, v
    v = router.keynum_verdict([(7, "Utah -6.5 at TCU", "Utah", "TCU", False)], "s")
    assert "Utah -6.5 at TCU" in v and "7" in v, v
    assert router.keynum_verdict([], "s") is None


def _days_ago(n):
    import datetime as _dt
    return ((_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=6)).date() - _dt.timedelta(days=n)).isoformat()


INTEL = {"as_of": "Wed Nov 18 2:45 AM CT", "games": {"1": {"away": "Clemson", "home": "Duke", "facts": [
    {"kind": "fact", "team": "away", "players": ["Trent Pearman", "Brock Bradley"], "source_name": "Yahoo Sports",
     "date": _days_ago(1), "text": "Clemson will start fifth-year walk-on Trent Pearman at QB at Duke in his first career "
                                   "start (2-of-6 for 6 yards vs Georgia Tech); freshman Brock Bradley is QB2."},
    {"status": "out", "kind": "injury", "team": "away", "players": ["Christopher Vizzina"], "source_name": "Yahoo Sports",
     "date": _days_ago(1), "text": "Clemson QB Christopher Vizzina is out vs Duke with what Dabo Swinney called a "
                                   "'pretty severe' concussion."}]},
    "2": {"away": "South Carolina", "home": "Georgia", "facts": [
        {"status": "questionable", "kind": "injury", "team": "away", "players": ["LaNorris Sellers"], "source_name": "WIS-TV",
         "date": _days_ago(1), "text": "South Carolina QB LaNorris Sellers (ankle) sat out the loss at Arkansas and Shane "
                                       "Beamer gave no specific update on him; backup Cutter Woods started."}]},
    "3": {"away": "UCLA", "home": "Michigan", "facts": [
        {"status": "out", "kind": "injury", "team": "home", "players": ["JJ Buchanan", "Blake Frazier"],
         "source_name": "Maize n Brew", "date": _days_ago(1),
         "text": "Michigan's JJ Buchanan is out for the season and Blake Frazier is doubtful."}]}}}


MOVES = {("Clemson", "Duke"): (-8.5, -13.5), ("South Carolina", "Georgia"): (-10.5, -16.5)}


def _store(intel, moves):
    """Stub the fact store with slate_intel-shaped notes (the store's intel_facts) and open/now spreads."""
    fs = router.factstore
    sheets = [{"id": k, "away": g["away"], "home": g["home"], "intel_facts": g.get("facts") or []}
              for k, g in (intel.get("games") or {}).items()]
    teams = {t for sh in sheets for t in (sh["away"], sh["home"])}

    def market(row):
        sh = row.get("sheet") or row
        mv = moves.get((sh.get("away") or sh.get("awayTeam"), sh.get("home") or sh.get("homeTeam")))
        return {"spread": mv[1], "spreadOpen": mv[0], "move": mv[1] - mv[0]} if mv else {}
    stub = {"has_personnel": lambda: False,
            "all_availability": lambda: [(sh, f) for sh in sheets for f in sh["intel_facts"]],
            "availability": lambda away, home: (next((sh["intel_facts"] for sh in sheets if {sh["away"], sh["home"]}
                                                      == {away, home}), []), intel.get("as_of")),
            "teams_named": lambda q: [t for t in sorted(teams, key=lambda t: (q or "").find(t)) if t in (q or "")],
            "resolve": lambda name: name if name in teams else None,
            "market": market, "player_status": lambda *a, **k: [], "facts": lambda *a, **k: []}
    saved = {k: getattr(fs, k) for k in stub}
    for k, v in stub.items():
        setattr(fs, k, v)
    return lambda: [setattr(fs, k, v) for k, v in saved.items()]


def _with_intel(fn, intel=None, moves=None):
    undo = _store(INTEL if intel is None else intel, MOVES if moves is None else moves)
    try:
        return fn()
    finally:
        undo()


def test_qb_status_names_the_starter_when_the_qb_is_out():
    out = _with_intel(lambda: router.qb_status_text({}, "Who's Clemson's QB Friday? Has he ever played?"))
    assert out and "Pearman" in out and "Vizzina" in out, out
    assert re.search(r"priced in", out, re.I) and "5" in out, out  # Duke -8.5 -> -13.5 already moved on it
    assert len(out) <= 600


def test_qb_status_player_ask_brings_the_backup():
    out = _with_intel(lambda: router.qb_status_text({}, "Is Vizzina playing Saturday? If not, what's the backup done?"))
    assert out and "out" in out.lower() and "Pearman" in out, out


def test_qb_status_questionable_says_wait():
    out = _with_intel(lambda: router.qb_status_text({}, "Is LaNorris Sellers playing vs Georgia?"))
    assert out and "questionable" in out.lower() and re.search(r"\bwait\b|coin flip", out, re.I), out


def test_qb_status_non_qb_injuries_matter_at_the_margin():
    out = _with_intel(lambda: router.qb_status_text({}, "Any injuries that matter for Michigan vs UCLA?"))
    assert out and "Buchanan" in out and re.search(r"margin|doesn'?t move the number", out, re.I), out


def _with_move_game(fn, line, lean, intel=None):
    fs = router.factstore
    undo = _store(INTEL if intel is None else intel, {})
    saved = (fs.find_game, fs.market, fs.lean)
    fs.find_game = lambda q, ctx: ({"id": "1", "awayTeam": "Clemson", "homeTeam": "Duke", "lines": [line]}, None)
    fs.market = lambda row: line
    fs.lean = lambda g, q="": lean
    try:
        return fn()
    finally:
        fs.find_game, fs.market, fs.lean = saved
        undo()


LEAN = ["COMPUTED ATS LEAN (vs the current market line): Duke -13.5 (lean; factor score Duke 2.0)",
        "    + for Duke: QB status: Clemson QB Tait Reynolds is out (Yahoo Sports)"]


def test_line_move_names_starter_says_priced_in_and_keeps_the_facts():
    q = "Clemson-Duke went from Duke -8.5 to -13.5. What happened, and is the move already priced in?"
    out = _with_move_game(lambda: router.slate_move_text({}, q), {"spread": -13.5, "spreadOpen": -8.5, "overUnder": 52.5}, LEAN)
    for f in ("-8.5", "-13.5", "5", "Vizzina", "Pearman"):
        assert f in out, (f, out)
    assert re.search(r"priced in", out, re.I) and re.search(r"\blean\b", out, re.I) and "Duke -13.5" in out, out
    assert "mainly because QB status" not in out and "Biggest reason: QB status" not in out, out  # the QB is in the number
    assert len(out) <= 600 and router.debot(out, q, "fact") == out, out


def test_line_move_unexplained_big_move_says_dont_fight_it():
    q = "Duke went from -8.5 to -13.5, what changed?"
    empty = {"games": {}, "as_of": "Wed Nov 18 2:45 AM CT"}
    import news
    orig_b, news.breaking_for = news.breaking_for, lambda *a, **k: []  # nothing in the news feed either
    try:
        out = _with_move_game(lambda: router.slate_move_text({}, q), {"spread": -13.5, "spreadOpen": -8.5}, LEAN, intel=empty)
    finally:
        news.breaking_for = orig_b
    assert "-8.5" in out and "-13.5" in out and "Wed Nov 18" in out, out
    assert not re.search(r"\blean\b", out, re.I), out  # no lean against a move nobody can explain


def test_line_move_low_total_note_only_in_a_low_total():
    q = "Duke went from -8.5 to -13.5, why, and is that a lot with the total?"
    out = _with_move_game(lambda: router.slate_move_text({}, q), {"spread": -13.5, "spreadOpen": -8.5, "overUnder": 62.5}, LEAN)
    assert "62.5" in out and "low total" not in out.lower(), out


OLD_QB = {"as_of": "now", "games": {"1": {"away": "Clemson", "home": "Duke", "facts": [
    {"status": "out", "kind": "injury", "team": "away", "players": ["Christopher Vizzina"], "source_name": "Yahoo Sports",
     "date": _days_ago(13), "text": "Clemson QB Christopher Vizzina (shoulder) is expected to miss the rest of the regular season."}]}}}


def test_line_move_old_injury_was_already_in_the_opener():
    q = "Duke went from -8.5 to -13.5. Why'd it move?"
    sc = router.slate_cards
    out = _with_move_game(lambda: router.slate_move_text({}, q), {"spread": -13.5, "spreadOpen": -8.5}, LEAN, intel=OLD_QB)
    assert "Vizzina" in out and not re.search(r"priced in|\broom\b", out, re.I), out
    assert re.search(r"open(er|ing|ed)", out, re.I), out  # old news: the opening number already had it
    assert sc is router.slate_cards


def test_qb_status_old_injury_was_already_in_the_opener():
    out = _with_intel(lambda: router.qb_status_text({}, "Is Vizzina playing Saturday?"), intel=OLD_QB,
                      moves={("Clemson", "Duke"): (-8.5, -9.5)})
    assert out and "Vizzina" in out and not re.search(r"priced in|\broom\b", out, re.I), out
    assert re.search(r"open(er|ing)", out, re.I), out


QUESTIONABLE = {"as_of": "now", "games": {"1": {"away": "Clemson", "home": "Duke", "facts": [
    {"status": "questionable", "kind": "injury", "team": "away", "players": ["Christopher Vizzina"], "source_name": "X",
     "date": _days_ago(1), "text": "Clemson QB Christopher Vizzina (ankle) sat out last week and Dabo Swinney gave no update."}]}}}


def test_line_move_unsure_qb_wait_line_counts_as_the_lean():
    """'Wait for the status' must read as the answer's side call, or chat.lean_fact tacks a contradicting
    'Lean: Duke ..., mainly because SP+ ...' after it (genius r2 lv-uga-sc-move)."""
    import chat
    card = ("GAME CARD Clemson at Duke\n  COMPUTED ATS LEAN (vs the current market line): Duke -13.5 (lean; x)\n"
            "    + for Duke: SP+ projects Duke by 15\n")
    q = "Duke went from -8.5 to -13.5. Why'd it move, and is 13.5 worth laying?"
    check, _ = chat.lean_fact(card, q)
    try:
        for i in range(30):
            os.environ["PICKY_PHRASING_SEED"] = f"c{i}"
            out = _with_move_game(lambda: router.slate_move_text({}, q), {"spread": -13.5, "spreadOpen": -8.5}, LEAN,
                                  intel=QUESTIONABLE)
            assert "Vizzina" in out and check(out), out
    finally:
        os.environ.pop("PICKY_PHRASING_SEED", None)


def test_line_move_keeps_the_key_number_when_asked():
    q = "Duke is only -3 after opening -5.5. Why is it dropping, and does sitting right on 3 matter?"
    for i in range(20):
        os.environ["PICKY_PHRASING_SEED"] = f"k{i}"
        try:
            out = _with_move_game(lambda: router.slate_move_text({}, q), {"spread": -3, "spreadOpen": -5.5}, LEAN)
        finally:
            os.environ.pop("PICKY_PHRASING_SEED", None)
        assert re.search(r"key number", out) and len(out) <= 600, out


def test_line_move_news_feed_qb_out_says_priced_in():
    import news
    q = "Duke went from -8.5 to -13.5. What changed?"
    orig_b = news.breaking_for
    news.breaking_for = lambda *a, **k: ["Clemson QB Christopher Vizzina out with upper-body injury."]
    try:
        out = _with_move_game(lambda: router.slate_move_text({}, q), {"spread": -13.5, "spreadOpen": -8.5}, LEAN,
                              intel={"games": {}, "as_of": "now"})
    finally:
        news.breaking_for = orig_b
    assert "Vizzina" in out and re.search(r"priced in", out, re.I), out
    assert "QB status" not in out, out


def test_conf_control():
    v = router.conf_control([("Utah", "7-0"), ("Texas Tech", "6-1"), ("BYU", "5-2")], "s")
    assert "Utah" in v and "Texas Tech" in v and re.search(r"control|win out", v, re.I), v
    v = router.conf_control([("UCLA", "7-0"), ("Indiana", "6-1"), ("Oregon", "6-1")], "s")
    assert "UCLA" in v and "Indiana" in v and "Oregon" in v and re.search(r"tiebreak", v, re.I), v


def test_lookahead_verdict_is_honest_about_spots():
    v = router.spots_verdict(["lookahead"], "s")
    assert re.search(r"coin flip", v, re.I) and re.search(r"tiebreaker|not a reason", v, re.I), v
    v = router.spots_verdict(["lookahead", "letdown"], "s")
    assert re.search(r"letdown", v, re.I), v


def test_interim_verdict():
    assert re.search(r"wouldn'?t fade|no reason to fade", router.interim_verdict("Baylor", 2, 1, "s"), re.I)
    assert re.search(r"fade", router.interim_verdict("Baylor", 0, 3, "s"), re.I)
    assert re.search(r"doesn'?t move the number|coin flip", router.interim_verdict("Baylor", 1, 1, "s"), re.I)


def test_h2h_has_a_verdict_and_keeps_the_facts():
    # h2h_text reads factstore since cd96879 (was slate_cards): patch there, or the live slate answers
    orig_find, orig_lm = router.factstore.find_game, router.factstore.last_meeting
    try:
        router.factstore.find_game = lambda q, ctx: ({"awayTeam": "Clemson", "homeTeam": "Duke"}, None)
        router.factstore.last_meeting = lambda g: "  Last meeting (2025): Duke won 46-45 at Clemson, line Clemson -4: Duke covered by 5"
        out = router.h2h_text({}, "last time Clemson played Duke?")
        for f in ("2025", "Duke", "46-45", "Clemson -4", "5"):
            assert f in out, (f, out)
        assert re.search(r"tiebreaker|not a reason|doesn'?t move the number", out, re.I), out
    finally:
        router.factstore.find_game, router.factstore.last_meeting = orig_find, orig_lm


ST = [{"rank": 1, "team": "Alpha", "w": 30, "l": 18}, {"rank": 2, "team": "Ack", "w": 28, "l": 20},
      {"rank": 3, "team": "Bravo", "w": 27, "l": 21}, {"rank": 3, "team": "Cobra", "w": 27, "l": 21},
      {"rank": 5, "team": "Lima", "w": 26, "l": 22}, {"rank": 14, "team": "Tango", "w": 19, "l": 29}]


def _sctx(me):
    return {"standings_ytd": ST, "current_week": 11, "games_this_week": [], "you": {"team_name": me}}


def test_strategy_names_who_is_around_you():
    out = pick_strategy.answer(_sctx("Ack"), "Should I start going against the field to catch him?")
    assert "28-20" in out and "Lima" in out and re.search(r"protect", out, re.I), out
    out = pick_strategy.answer(_sctx("Lima"), "what's my strategy?")
    assert "26-22" in out and ("Bravo" in out or "Cobra" in out), out
    out = pick_strategy.answer(_sctx("Tango"), "any pick strategy?")
    assert "19-29" in out and ("Bravo" in out or "Cobra" in out) and re.search(r"fade", out, re.I), out


def test_fact_guard_keeps_every_strategy_variant():
    import fact_guard  # fact_final drops a sentence that ties a member to a wrong place/W-L ('first outside the cut' = 1st)
    for me in ("Ack", "Lima", "Tango", "Bravo"):
        ctx = dict(_sctx(me), weekly_records={}, last_week={"week": 10, "games": []})
        ok = fact_guard.member_claim_check(ctx, "strategy?", {}, None)
        for i in range(60):
            os.environ["PICKY_PHRASING_SEED"] = f"g{i}"
            try:
                out = pick_strategy.answer(ctx, "strategy?")
            finally:
                os.environ.pop("PICKY_PHRASING_SEED", None)
            for sent in re.split(r"(?<=[.!?])\s+", out):
                assert ok is None or ok(sent), (me, sent)


def test_lane_answers_stay_under_the_cap():
    q = "Say the league locks Duke -13.5 on Tuesday and by Saturday the market's Duke -15. Did I get value on Duke or Clemson?"
    for i in range(30):
        os.environ["PICKY_PHRASING_SEED"] = f"x{i}"  # tests only: force a different variant draw
        try:
            assert len(router.hypo_value_text({}, q)) <= 600
            assert len(pick_strategy.answer(_sctx("Ack"), "strategy?")) <= 600
        finally:
            os.environ.pop("PICKY_PHRASING_SEED", None)


# ---------------------------------------------------------------- the voice line after a code answer

def test_voice_line_cant_claim_missing_data():
    ans = "Clemson will start Trent Pearman at QB."
    for line in ("I don't have any data on a backup for that mess.", "I haven't seen anything else on that.",
                 "I haven't seen any data on that game, stay in your lane", "No clue who else is back there."):
        assert not router.voice_line_ok(line, ans, {}), line
    assert router.voice_line_ok("That quarterback room is a crime scene.", ans, {})


if __name__ == "__main__":
    fails = 0
    for name, f in list(globals().items()):
        if name.startswith("test_") and callable(f):
            try:
                f()
                print("ok  ", name)
            except Exception as ex:  # noqa: BLE001
                fails += 1
                print("FAIL", name, repr(ex)[:400])
    print(f"\n{fails} failed" if fails else "\nall ok")
    sys.exit(1 if fails else 0)
