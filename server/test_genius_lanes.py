"""Genius lanes (2026-11-17): h2h / qb status / key numbers routing, the computed-lean appender (offline)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import chat  # noqa: E402
import router  # noqa: E402


def test_h2h_routes():
    for q in ["What happened last time Clemson played Duke?", "What happened when Northwestern and Minnesota played last year?",
              "head to head Iowa Illinois", "h2h lsu tennessee"]:
        assert router.classify(q).kind == "h2h", q
    for q in ["how did my pick do last week", "who covered last week", "last time I picked Iowa it burned me"]:
        assert router.classify(q).kind != "h2h", q


def test_keynum_routes():
    assert router.classify("Which lines this week are sitting on or next to a key number? Where does the hook matter?").kind == "keynum"
    assert router.classify("what are the key numbers this week").kind == "keynum"


def test_conf_and_lookahead_routes():
    for q in ["What's the Big 12 title game picture?", "who leads the SEC", "big ten race"]:
        assert router.classify(q).kind == "conf_race", q
    assert router.classify("Any lookahead or letdown spots this week?").kind == "lookahead_slate"
    assert router.classify("what's the CFP picture").kind != "conf_race"


def test_hypo_value_and_career():
    q = "Say the league locks Duke -13.5 on Tuesday and by Saturday the market's Duke -15. Did I get value on Duke or Clemson?"
    assert router.classify(q).kind == "hypo_value"
    out = router.hypo_value_text({}, q)
    assert out.startswith("Value on Duke") and "1.5-point" in out and "14" in out
    out = router.hypo_value_text({}, "if we get Illinois +8.5 locked and the market goes to Illinois +6.5, who has value?")
    assert out.startswith("Value on Illinois") and "2-point" in out
    assert router.classify("What's Lane Kiffin's career ATS record as a road underdog?").kind == "career_ats"
    assert router.career_ats_text({}, "Nick Nobody career ATS").startswith("I don't have career ATS splits")


def test_interim_routes():
    assert router.INTERIM_ASK.search("Baylor's playing for an interim coach right? Does that matter vs Texas Tech?")
    assert not router.INTERIM_ASK.search("who's the coach at Baylor")


def test_live80_misroutes():
    assert router.classify("who won Texas LSU?").kind == "score"
    assert router.classify("how many points have been scored in the Hawaii game?").kind == "score"
    assert router.classify("did SMU ever score?").kind == "score"
    assert router.classify("who won the jackpot?").kind == "jackpot_hit"
    assert router.classify("how many points do you get for a correct pick?").kind == "rules"
    rg = [{"away": {"name": "Wake Forest"}, "home": {"name": "SMU"}, "away_score": 49, "home_score": 7,
           "scoring": [{"team": "Wake Forest", "text": "Gio Lopez 5 Yd Run"},
                       {"team": "SMU", "text": "Yannick Smith 19 Yd pass from Kevin Jennings (Brendan Baird Kick)"}]}]
    out = router._scored_text({"live_rider_games": rg}, "did SMU ever score?", {"Away": "Wake Forest", "Home": "SMU"})
    assert "Yannick Smith 19 Yd pass from Kevin Jennings" in out
    rg[0]["home_score"] = 0
    assert router._scored_text({"live_rider_games": rg}, "did SMU score?", {"Away": "Wake Forest", "Home": "SMU"}) == "SMU hasn't scored."


CTX = {"current_week": 12, "you": {"team_name": "Wedge"},
       "standings_ytd": [{"rank": 1, "team": "Alpha", "w": 30, "l": 18}, {"rank": 2, "team": "Bravo", "w": 28, "l": 20},
                         {"rank": 3, "team": "Wedge", "w": 25, "l": 23}],
       "weekly_records": {"Wedge": {11: [2, 2]}}, "games_this_week": [], "last_week": {"week": 11, "games": []}}


def test_fact_exam_router_fixes():
    assert router.classify("how many points is Bravo behind first?").kind == "standings"
    assert router.fact_answer("standings", CTX, "how many points is Bravo behind first?") == "Bravo is 2nd at 28-20, 2 points back of first."
    assert router.fact_answer("standings", CTX, "where's Wedge in the standings?").startswith("You're 3rd at 25-23")
    assert router.fact_answer("record", CTX, "what was my record in week 14?") == "Week 14 hasn't been played yet, so no record for it."
    assert router.fact_answer("record", CTX, "what was my record in week 11?") == "You went 2-2 in week 11."
    out = router.fact_answer("standings", CTX, "now that I'm in first, where am I")
    assert out.startswith("You're 3rd at 25-23"), out  # the answer already states the real place: no double fix
    out = router.fact_answer("record", CTX, "now that I'm in first, what's Bravo's record?")
    assert out.startswith("Not quite: you're 3rd at 25-23") and "Bravo" in out, out


def test_fact_exam_round1_lanes():
    assert router.classify("how'd your picks do last week?").kind == "picky_record"
    assert router.classify("what did Ackbar go in week 9?").kind == "record"
    pp = {"season": [0, 3], "last_week": [0, 3], "revealed": [
        {"week": 11, "pick": "Boise State", "away": "Oregon State", "home": "Boise State", "line": -13.5,
         "winner": "Oregon State", "status": "final", "tiebreaker": False}]}
    ctx = dict(CTX, picky_player=pp, last_week={"week": 11, "games": []})
    assert router.picky_record_text(ctx, "how'd your picks do last week?") == \
        "I went 0-1 in week 11: Boise State -13.5 missed. I'm 0-3 on the season."
    assert router.unknown_school("line on Hogwarts Tech") == "Hogwarts Tech"
    assert router.unknown_school("line on Texas Tech") is None
    last = dict(CTX, you={"team_name": "Wedge"})
    assert router.chase_text(last, "whos chasing me").startswith("Nobody's behind you: you're last at 25-23")
    assert not router.voice_line_ok("Oregon State ruined your weekend.", "x", {})


def test_move_why_routes():
    for q in ["Georgia-South Carolina opened around Georgia -10.5 and it's -16.5 now. Why'd it move that much?",
              "Clemson-Duke went from Duke -8.5 to -13.5. What happened?",
              "Oklahoma went from -2.5 to -7.5 vs A&M. Who got hurt?",
              "North Texas -6.5 at Tulane. Some books had it double digits early. What changed?"]:
        assert router.classify(q).kind == "line_move", q
    assert router.classify("why are you so bad at this").kind != "line_move"


def test_premise_lane():
    ctx = dict(CTX, you={"team_name": "Wedge"}, last_week={"week": 11, "games": [
        {"away": "Oregon State", "home": "Boise State", "away_pts": 41, "home_pts": 46, "line": -13.5,
         "covered": "Oregon State", "picks": {"Wedge": "Oregon State"}}]})
    assert router.classify("Boise State covered last week right?").kind == "premise"
    assert router.fact_answer("premise", ctx, "Boise State covered last week right?") == \
        "Nope: Oregon State covered (Oregon State 41, Boise State 46)."
    assert router.fact_answer("premise", ctx, "Oregon State covered last week right?").startswith("Yep, Oregon State covered")
    assert router.fact_answer("premise", ctx, "why did I take Boise State last week?") == \
        "You didn't take Boise State, you had Oregon State and it covered."


def test_typos_and_followups():
    st = CTX["standings_ytd"]
    assert [r["team"] for r in router.fuzzy_members("whats bravvo record", st, "Wedge")] == ["Bravo"]
    assert router.fuzzy_members("whats my record", st, "Wedge") == []
    assert router.classify("whats ackbr record").kind == "record"
    assert router.classify("and the week before that?").kind == "record"
    assert router.classify("whats the record for most points").kind != "record"


def test_qb_status_team_possessive():
    out = router.qb_status_text({}, "is Clemson's QB playing this week?")
    assert out is None or "Clemson" in out.split(".")[0], out


def test_lw_pick_and_past_lean():
    ctx = dict(CTX, you={"team_name": "Wedge"}, last_week={"week": 11, "games": [
        {"away": "Wake Forest", "home": "SMU", "away_pts": 49, "home_pts": 7, "line": 1.5, "covered": "Wake Forest",
         "picks": {"Bravo": "SMU", "Wedge": "Wake Forest"}}]})
    assert router.classify("who did Bravo take in Wake Forest vs SMU last week?").kind == "lw_pick"
    assert router.lw_pick_text(ctx, "who did Bravo take in Wake Forest vs SMU last week?") == "Bravo took SMU +1.5 (missed) in week 11."
    assert router.lw_pick_text(ctx, "what did I take last week?") == "You took Wake Forest -1.5 (covered) in week 11."
    assert chat.lean_fact(CARD, "who did Bravo take in Memphis Navy last week?") is None
    assert chat.lean_fact(CARD, "is my Memphis pick covering right now?") is None  # live ask (live-80 r3)
    assert router.unknown_school("how many total points have been scored in Boise Oregon State?") is None


def test_conf_not_looks():
    assert router.classify("Picky what would you rate Charlie's looks on a scale of 1-10").kind != "conf"
    assert router.classify("how confident are you in your picks 1-10").kind == "conf"


def test_h2h_text_format():
    orig_find, orig_lm = router.slate_cards.find_game, router.slate_cards.last_meeting
    try:
        router.slate_cards.find_game = lambda q, ctx, idx=None: ({"awayTeam": "Clemson", "homeTeam": "Duke"}, None)
        router.slate_cards.last_meeting = lambda g: "  Last meeting (2025): Duke won 46-45 at Clemson, line Clemson -4: Duke covered by 5"
        out = router.h2h_text({}, "last time Clemson played Duke?")
        assert out == "Last time they played (2025), Duke won 46-45 at Clemson. Clemson -4 was the line, so Duke covered by 5."
        router.slate_cards.last_meeting = lambda g: None
        assert "don't have a 2025 meeting" in router.h2h_text({}, "last time Clemson played Duke?")
    finally:
        router.slate_cards.find_game, router.slate_cards.last_meeting = orig_find, orig_lm


CARD = ("GAME CARD Memphis at Navy\n  COMPUTED ATS LEAN (vs the current market line): Navy +3.5 (lean; factor score "
        "Memphis 1.2, Navy 2.0). Who COVERS, not who wins.\n    + for Navy: Navy is 4-1 ATS at home\n")


def test_lean_fact():
    check, add = chat.lean_fact(CARD, "Memphis -3.5 at Navy. How does Memphis's defense match up with the option?")
    assert not check("Memphis is 62nd vs the run, the option will cook them.")
    assert check("I like Navy +3.5 to cover here.")
    assert add.strip() == "Lean: Navy +3.5, mainly because Navy is 4-1 ATS at home."
    coin = CARD.replace("(lean;", "(coin flip;")
    assert "coin flip" in chat.lean_fact(coin, "who covers Memphis Navy?")[1]
    assert chat.lean_fact(CARD, "what channel is Memphis Navy on") is None
    pool = ("GAME CARD, LSU at Tennessee (built offline ...):\n  League line: LSU -1.5 (locked).\n"
            "  THE PLAY: I'd take Tennessee +1.5 (slight lean).\n  Why (use two, three at most): SP+ projects Tennessee by 1.9 / home dog\n")
    check, add = chat.lean_fact(pool, "who do you like in LSU Tennessee?")
    assert add.strip() == "Lean: Tennessee +1.5, mainly because SP+ projects Tennessee by 1.9." and not check("LSU is rolling.")


if __name__ == "__main__":
    fails = 0
    for name, f in list(globals().items()):
        if name.startswith("test_"):
            try:
                f()
                print("ok  ", name)
            except AssertionError as ex:
                fails += 1
                print("FAIL", name, ex)
    sys.exit(1 if fails else 0)
