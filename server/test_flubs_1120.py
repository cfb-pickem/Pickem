"""
Flub fix round 2026-11-20 (fixtures rg-20261117-6, rg-20261119-1/2/4/5/8/11/12/13), model-free:

- 'how many slots should I pull to ensure the jackpot triggers?' -> the jackpot odds lane, which says no number of
  pulls guarantees it (was the slots rules blurb, fact:rules2).
- 'The dealer has a 6 and I have a soft 17, what should I do' -> a Pit blackjack hand, answered with basic strategy
  for the Pit's rules (was pick'em standings strategy, fact:strategy).
- 'all 8 teams we are picking ... records against the spread' -> every pool team's ATS record from the fact store
  (was a dodge / 'I don't have them'); the 'just give me the numbers' insist re-asks it.
- 'Which games should I pick' / 'What picks should I make' -> the slate lane, a call on every open game.
- 'What was my worst pick last week?' -> the sender's biggest miss last week (was Picky's shakiest pick this week).

    ./.venv/bin/python3 -I test_flubs_1120.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
_TMP = tempfile.mkdtemp()
os.environ.setdefault("PICKY_NO_REPEAT_STATE", str(Path(_TMP) / "nr.json"))  # never the live ledger
os.environ.setdefault("PICKY_CALLBACKS_STATE", str(Path(_TMP) / "cb.json"))

import answer_first  # noqa: E402
import chat  # noqa: E402
import factstore as FS  # noqa: E402
import router  # noqa: E402


def test_jackpot_guarantee_ask_says_no_count_guarantees_it():
    q = "how many slots should I pull to ensure the jackpot triggers?"
    r = router.classify(q)
    assert r.label() == "fact:jackpot_hit", r.label()
    ctx = {"jackpot": {"pulls_since_pop": 230, "last_pop_at": None, "last_pop_team": None}}
    out = router.fact_answer("jackpot_hit", ctx, q)
    assert out.startswith("No number of pulls guarantees it"), out
    assert "230 slots pulls" not in out, out
    assert router.classify("how many pulls to guarantee a pop on the football").kind == "jackpot_hit"
    # plain slots rules asks keep the rules lane, and the plain odds answer has no guarantee line
    assert router.classify("what are the slots").label() == "fact:rules2"
    assert "guarantee" not in router.jackpot_odds_text(ctx, "is the jackpot about to hit")


def test_blackjack_hand_gets_basic_strategy_not_pick_strategy():
    q = "The dealer has a 6 and I have a soft 17, what should I do"
    r = router.classify(q)
    assert r.label() == "fact:blackjack", r.label()
    out = router.fact_answer("blackjack", {"teams": []}, q)
    assert out.startswith("Soft 17 against a dealer showing a 6: double down"), out
    assert "cut line" not in out, out
    cases = {"dealer shows a 10, I have 16": "Hard 16 against a dealer showing a 10: hit.",
             "I've got hard 11 and the dealer has an ace": "Hard 11 against a dealer showing an ace: hit.",
             "dealer has a king and I'm on 12": "Hard 12 against a dealer showing a 10: hit.",
             "dealer has a 5 and I have 12": "Hard 12 against a dealer showing a 5: stand.",
             "dealer's showing a 7, soft 18": "Soft 18 against a dealer showing a 7: stand.",
             "dealer has a 4 and I have soft 18": "Soft 18 against a dealer showing a 4: double down (past two "
                                                  "cards you can't, so stand).",
             "dealer shows 9, I have 10": "Hard 10 against a dealer showing a 9: double down"}
    for q, want in cases.items():
        assert router.classify(q).kind == "blackjack", q
        out = router.blackjack_text({}, q)
        assert out.startswith(want), (q, out)
    # pick'em strategy asks keep their lane
    for q in ("what should I do this week to catch up", "should I go against the field?"):
        assert router.classify(q).kind == "strategy", q
    assert router.classify("the dealer has 6 cards and I have 16").kind != "blackjack"


def _fixture_lane(fn, ctx, q):
    import test_factstore as TF
    FS.use(TF.built()["path"], TF.inputs(), now=TF.NOW)
    try:
        return fn(ctx, q)
    finally:
        FS.use(None, None)


def test_slate_ats_lists_every_pool_teams_record():
    q = "What are all 8 teams that we are picking this week records against the spread this season"
    import test_factstore as TF
    FS.use(TF.built()["path"], TF.inputs(), now=TF.NOW)
    try:
        r = router.classify(q)
        assert r.label() == "fact:slate_ats", r.label()
        assert router.classify("can you tell me all the teams' ATS records").kind == "slate_ats"
        assert router.classify("how's Duke against the number this year?").kind == "ats_profile"  # one team: unchanged
        assert router.classify("what's my ATS record").kind != "slate_ats"
    finally:
        FS.use(None, None)
    pairs = [("Texas Tech", "TCU"), ("Tennessee", "South Carolina"), ("UTSA", "Tulane"), ("Wyoming", "Nevada")]
    ctx = {"games_this_week": [{"id": i, "away": a, "home": h, "line": 1.5} for i, (a, h) in enumerate(pairs)]}
    out = _fixture_lane(router.slate_ats_text, ctx, q)
    assert isinstance(out, FS.Answer), out
    assert FS.unsupported(out, out.facts_used) == [], FS.unsupported(out, out.facts_used)
    import re
    assert len(re.findall(r"\b\d{1,2}-\d{1,2}\b", str(out))) == 8, out
    for t in ("Texas Tech 5-5", "Tennessee 6-4", "UTSA 6-4", "Wyoming 6-4"):
        assert t in str(out), (t, out)


def test_insist_after_the_ats_ask_re_asks_it():
    first = "What are all 8 teams that we are picking this week records against the spread this season"
    again = answer_first.carry("I won’t just give me the numbers", [first],
                               lambda p: router.classify(p).lane in ("banter", "ignore", "jailbreak"))
    assert again == first, again
    assert router.classify(again).kind == "slate_ats"


def test_which_games_should_i_pick_is_the_slate_lane():
    for q in ("Which games should I pick", "What picks should I make", "which teams should we take this week"):
        r = router.classify(q)
        assert r.label() == "fact:slate", (q, r.label(), r.why)
    assert router.classify("which side should I take in Iowa").kind != "slate"  # a named game: unchanged


def test_my_worst_pick_last_week_is_my_biggest_miss():
    from test_keeper import _wk12_picks
    q = "What was my worst pick last week?"
    assert router.classify(q).label() == "fact:results", router.classify(q).label()
    assert router.classify("what's the worst pick this week").kind == "worst"  # Picky's shakiest: unchanged
    ctx = _wk12_picks()
    ctx["you"] = dict(ctx.get("you") or {}, team_name="Ackbar")
    with patch.object(chat, "load_lore", lambda: {}), patch.object(chat, "member_nicknames", lambda: {"Ackbar": "Ackbar"}):
        out = router.fact_answer("results", ctx, q)
    assert out == "Your worst pick in week 11 was Boise State -13.5: missed the cover by 8.5.", out


if __name__ == "__main__":
    fails = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as e:  # noqa: BLE001
                fails += 1
                print(f"FAIL {name}: {e!r}")
    print(f"{fails} failed")
    sys.exit(1 if fails else 0)
