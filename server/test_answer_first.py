"""
Brian's web chat, 2026-11-18 night (chat fbe51e7d3f), as regressions. No model, no network.

1. "What are all 8 teams that we are picking this week records against the spread this season" got a roast
   ("...your habit of picking losers like you did with North Texas last week."): a full-lane data ask whose
   reply has no number and no plain "I don't have it" now gets an honest gap line + an offer.
2. "I won't just give me the numbers" got "Yep.": an insist follow-up re-asks his last real question.
3. "Are you coded to be nicer to me than everyone else... you're not as mean as I expected" got "Can't argue
   with that.": a code answer that answers it.
4. Slots: "Is the slot jackpot about to hit" / "It's progressive right? What are the current odds it pops for
   me" got "the chance for a pop is shown on the football" (the site hides the odds on purpose).
5. The web chat was logged with team null (the flub loop and member memory skip those rows).

Run:  ./.venv/bin/python3 -I test_answer_first.py
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import answer_first as af  # noqa: E402
import group_chat as gc  # noqa: E402
import league_rules  # noqa: E402
import picky_brain as pb  # noqa: E402
import router  # noqa: E402
import chat  # noqa: E402

ATS_Q = "What are all 8 teams that we are picking this week records against the spread this season"
ROAST = ("Just make sure you don't let these numbers distract you from your habit of picking losers like you did "
         "with North Texas last week.")
INSIST_Q = "I won’t just give me the numbers"
TONE_Q = "Are you coded to be nicer to me than everyone else… you’re not as mean as I expected"
JP1 = "Is the slot jackpot about to hit"
JP2 = "It’s progressive right? What are the current odds it pops for me"
BRIAN = gc.Sender(handle="web:26b58adf", team="Brian", team_id=5, user_id="uid-brian", name="Brian")


# --- unit -------------------------------------------------------------------------------------------------------

def test_roast_on_a_data_ask_is_no_answer():
    assert af.data_ask(ATS_Q)
    assert af.no_answer(ROAST, ATS_Q)
    assert not af.no_answer("Illinois is 7-3 against the spread, Iowa 5-5.", ATS_Q)  # numbers = an answer
    assert not af.no_answer("I don't have ATS records loaded, but I can give you the lines.", ATS_Q)  # honest gap
    assert not af.no_answer(ROAST, "you suck at picking")  # not a data ask: banter stays banter
    assert not af.no_answer("Vanderbilt, by a mile.", "who has the best ATS record?")  # a team answer stays
    assert not af.no_answer("Ohio State is undefeated.", "what's Ohio State's record?")
    assert af.no_answer("Next question.", ATS_Q)


def test_gap_reply_is_honest_and_offers():
    r = af.gap_reply(ATS_Q)
    assert "against the spread" in r and "make them up" in r or "won't guess" in r or "made-up" in r, r
    assert "lines" in r and "North Texas" not in r and "loser" not in r, r
    assert "my read" in r  # DM/web: his read on each game is on offer
    g = af.gap_reply(ATS_Q, group=True)
    assert "my read" not in g and "lines" in g, g  # group: his sides stay hidden till the lock


def test_insist_carries_the_last_real_question():
    chatter = lambda p: router.classify(p).lane in ("banter", "ignore", "jailbreak")  # noqa: E731
    assert af.insist(INSIST_Q)
    assert af.carry(INSIST_Q, ["Who should I pick this week. Keep it brief, dipshit", ATS_Q], chatter) == ATS_Q
    assert af.carry("just give me the numbers", [ATS_Q, "answer the question"], chatter) == ATS_Q  # skips insists
    assert af.carry(INSIST_Q, [], chatter) is None
    assert af.carry(INSIST_Q, ["you suck lol"], chatter) is None  # nothing real to carry
    for q in ("just tell me who to pick", "can you tell me Michigan's record", "give me the Iowa line"):
        assert not af.insist(q), q  # a new question is answered as itself


def test_tone_question_gets_an_answer():
    assert af.tone_ask(TONE_Q)
    for stance in ("", "neutral", "cold", "warm"):
        r = af.tone_answer(TONE_Q, stance)
        assert r.startswith("Nope") and r not in pb.REACTIONS and "energy" in r, r
    assert "friendly" in af.tone_answer(TONE_Q, "warm", charlie=True)
    assert not af.tone_ask("who's nicer, Clam or Ackbar?")


def test_questions_never_drop_to_banter():
    for q in (INSIST_Q, TONE_Q, ATS_Q):
        assert af.keep_full(q), q
    assert not af.keep_full("lol nice")


def test_slots_answers_match_the_rules():
    ctx = {"jackpot": {"pulls_since_pop": 234, "last_pop_at": None, "last_pop_team": None}}
    for q in (JP1, JP2):
        assert router.classify(q).label() == "fact:jackpot_hit", (q, router.classify(q).label())
        a = router.fact_answer("jackpot_hit", ctx, q)
        assert "234" in a and "secret" in a and "shown on the football" not in a, a
        assert "%" not in a, a  # the site hides the odds on purpose (js/jackpot.js): no number from Picky either
    assert router.fact_answer("jackpot_hit", ctx, JP2).startswith("Sort of"), "it's progressive in odds, not prize"
    assert "anybody's pull" in router.fact_answer("jackpot_hit", ctx, JP2)  # 'for me': no per-person odds
    ctx["jackpot"] = {"pulls_since_pop": 2, "last_pop_at": "2026-11-01T16:00:00", "last_pop_team": "TG"}
    assert "no more pops till the playoffs" in router.fact_answer("jackpot_hit", ctx, JP1)
    # unchanged: history asks, game asks
    assert router.classify("have i ever hit it").label() == "fact:jackpot_hit"
    assert router.classify("who won Texas LSU?").kind != "jackpot_hit"
    assert router.classify("what are the odds Ohio State covers").kind != "jackpot_hit"


def test_rules_text_on_the_jackpot():
    txt = league_rules.section("LET THE SLOTS DECIDE", Path(HERE / "league_rules.md").read_text())
    assert "shown on the football" not in txt and "never above" not in txt, txt
    # 50f699b replaced the capped boost ('up to 5% a pull', odds 'secret on purpose') with the weeks-left ramp
    assert "up to 5% a pull" not in txt and "75% in the last jackpot week" in txt and "not the live odds" in txt, txt


# --- the web path (group_chat._answer with the brain faked) -----------------------------------------------------

def _web(text, history, reply, lane="full"):
    """One web turn for Brian. Returns (result, the q the brain was asked or None, exchange rows)."""
    asked, real_classify = [], router.classify

    async def fake_run(draft, *, q, route=None, **k):
        asked.append(q)
        return reply, None

    def classify(q):
        if lane == "full":  # route-agnostic on purpose: the ATS job may give the ATS ask a code lane
            return router.Route("full", why="test")
        return real_classify(q)

    with tempfile.TemporaryDirectory() as d, contextlib.ExitStack() as es:
        x = Path(d) / "x.jsonl"
        for p in (patch.object(gc, "HISTORY_FILE", Path(d) / "h.json"), patch.object(gc, "EXCHANGE_LOG", x),
                  patch.object(gc.VIEWER, "session", lambda c: asyncio.sleep(0)),
                  patch.object(gc.picky_brain, "run", fake_run), patch.object(gc.router, "classify", classify),
                  patch.object(gc.member_memory, "stance_hint", lambda *a, **k: {"stance": "neutral"})):
            es.enter_context(p)
        out = asyncio.run(gc._answer("", text, dm=True, web={"sender": BRIAN, "jwt": "jwt-brian",
                                                              "history": history}))
        rows = [json.loads(r) for r in x.read_text().splitlines()] if x.exists() else []
    return out, (asked[0] if asked else None), rows


def test_web_insist_reasks_and_never_roasts():
    hist = [{"role": "user", "content": ATS_Q}, {"role": "assistant", "content": ROAST}]
    out, asked, rows = _web(INSIST_Q, hist, ROAST)
    assert asked == ATS_Q, asked  # the brain got the question he asked, not 'Yep.' material
    assert out["reply"] != ROAST and "North Texas" not in out["reply"] and "lines" in out["reply"], out
    assert rows and rows[-1]["team"] == "Brian" and rows[-1]["q"] == INSIST_Q, rows  # mapped, logged as typed


def test_web_data_ask_roast_becomes_gap_line():
    out, asked, rows = _web(ATS_Q, [], ROAST)
    assert asked == ATS_Q and "don't" in out["reply"].lower() or "can't" in out["reply"].lower() \
        or "aren't" in out["reply"], out
    assert "loser" not in out["reply"], out
    out, _, _ = _web(ATS_Q, [], "Illinois is 7-3 ATS, Iowa 5-5, Tennessee 4-6 and LSU 3-7.")
    assert out["reply"].startswith("Illinois is 7-3"), out  # a real answer goes out untouched


def test_web_tone_question_answered_in_code():
    out, asked, rows = _web(TONE_Q, [], "Can't argue with that.", lane="real")
    assert asked is None and out["lane"] == "banter:tone_q" and out["reply"].startswith("Nope"), (out, asked)
    assert rows[-1]["team"] == "Brian"


def test_brief_cannot_banter_brians_questions():
    """The understand pass called the insist and the tone question banter (-> 'Yep.' / 'Can't argue with that.')."""
    for q in (INSIST_Q, TONE_Q):
        used = []

        async def fake_orig(messages, prefix=None):
            yield 'data: {"content": "Here you go."}\n\n'

        async def fake_understand(*a, **k):
            return pb.Brief(asking="", intent="banter")

        async def fake_banter(st, prefix):
            used.append(1)
            return "Yep."

        async def go():
            rt = router.Route("full", why="no rule")
            st = {"q": q, "idx": 0, "brief": None, "options": {"temperature": 0.2}, "route": rt}
            t1, t2 = pb.STATE.set(st), chat.OPTIONS_OVERRIDE.set(st["options"])
            try:
                [x async for x in pb._ollama_stream([{"role": "user", "content": q}], "PFX")]
                return rt.label()
            finally:
                chat.OPTIONS_OVERRIDE.reset(t2)
                pb.STATE.reset(t1)
        with patch.object(pb, "_orig_ollama_stream", fake_orig), patch.object(pb, "understand", fake_understand), \
                patch.object(pb, "banter_reply", fake_banter), patch.object(pb, "football_thread", lambda st: False):
            lane = asyncio.run(go())
        assert lane == "full" and not used, (q, lane, used)


if __name__ == "__main__":
    tests = [v for k, v in dict(globals()).items() if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"  ok  {fn.__name__}")
        except AssertionError as e:
            print(f"FAIL  {fn.__name__}: {e}")
            failed += 1
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            print(f"ERR   {fn.__name__}: {type(e).__name__}: {e}")
            failed += 1
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
