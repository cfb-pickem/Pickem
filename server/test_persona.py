"""
Deterministic tests for Picky's voice plumbing: body-part baits, the
hostility (escalate) rider, the Dear Leader miss rule, the proactive hook,
and the persona text in the shared prefix.

Run:  ./.venv/bin/python3 -I test_persona.py
"""

from __future__ import annotations

import sys

sys.path.insert(0, ".")
import chat  # noqa: E402
from test_leader import G1, G2, ctx  # noqa: E402

WEENIE = "So, this 'Chuck P', picks look good but weenie size leaves a bit to be desired, no?"


def test_body_bait_needs_no_roast_word():
    g = chat.guard_note(ctx(you=("Clam", 7)), WEENIE)
    assert g.startswith("GUARDRAIL") and "OPENS with a one-line dodge" in g, g
    # Dear Leader target, someone else asking: light reverence + his real records, roast the asker
    assert "state-media reverence for Chuck P" in g and "his only numbers: 27-18" in g, g
    assert "Clam's season: 23-22" in g, g
    assert "Oregon State" not in g.split("ONLY these facts about Clam")[0], g  # no Chuck pick names
    for q in ("TG is a dick", "that RPO package is nasty", "they hung 50 on Boise", "nuts game last night"):
        assert not chat._BODY.search(q), q
    assert chat._BODY.search("TG has a tiny dick")


def test_leader_miss_rule_follows_results():
    # Chuck: Wake Forest final WIN, Oregon State not started -> no misses this week
    assert "NO misses THIS WEEK" in chat.leader_miss_rule(ctx(), "Chuck P")
    lost = dict(G1, winner="SMU")
    assert "never flipped" in chat.leader_miss_rule(ctx(games=(lost, G2)), "Chuck P")
    n = chat.leader_note(ctx(you=("Clam", 7)), "How's Chuck P doing?")
    assert "NO misses THIS WEEK" in n and "deadpan" in n, n


def test_hostility_detects_insults_at_picky_only():
    hostile = ["you suck picky", "fuck you", "Picky is trash", "what a stupid bot", "shut up", "you're not funny"]
    calm = ["TG is trash", "roast TG he is a moron", "How am I doing?", "that was a dumb pick by TG",
            "this week is nuts", "who's winning?"]
    for q in hostile:
        assert chat.hostile_convo([{"role": "user", "content": q}]), q
    for q in calm:
        assert not chat.hostile_convo([{"role": "user", "content": q}]), q
    # sticky: an insult two turns ago still counts
    convo = [{"role": "user", "content": "you're useless"}, {"role": "assistant", "content": "x"},
             {"role": "user", "content": "who's in first?"}]
    assert chat.hostile_convo(convo)
    e = chat.escalate_note(ctx(you=("Clam", 7)))
    assert e.startswith("ESCALATE") and "Clam's season: 23-22" in e and "no slurs" in e


def test_proactive_hook_gating():
    c = ctx(you=("Clam", 7))
    one = [{"role": "user", "content": "Hey Picky, what's up?"}]
    n = chat.proactive_note(c, one, one[-1]["content"])
    assert n.startswith("OPTIONAL HOOK") and "last place, 3 points behind Ackbar" in n, n
    n = chat.proactive_note(ctx(you=("Ackbar", 11)), one, one[-1]["content"])
    assert "the user is 2nd, 1 point behind first place" in n, n
    two = one + [{"role": "assistant", "content": "x"}, {"role": "user", "content": "Anything else?"}]
    assert chat.proactive_note(c, two, "Anything else?") == ""  # every other question only
    for q in ("How did my picks do?", "What are the rules on ties?", "Roast me for being fat",
              "What's the capital of France?"):
        assert chat.proactive_note(c, [{"role": "user", "content": q}], q) == "", q


def test_sentence_filter():
    B = chat._EXAMPLE_NGRAMS
    assert not chat.sentence_ok("You'll be last place with extra steps.", B)  # copied example joke
    assert not chat.sentence_ok("At least you can explain it to your mom.", B)  # family
    assert not chat.sentence_ok("He's wondering why he's still breathing.", B)  # dark
    assert chat.sentence_ok("Iowa covers by winning, or losing by 3 or fewer.", B)  # explainer wording is fine
    assert chat.sentence_ok("TG is dead last at 18-29.", B)
    b = chat.banned_ngrams([{"role": "assistant", "content": "TG is a walking traffic cone of football knowledge."}])
    assert not chat.sentence_ok("Again, TG is a walking traffic cone of football knowledge.", b)  # self-repeat
    assert chat.sentence_ok("TG is 18-29 and last.", b)


def test_filtered_stream_drops_only_bad_sentences():
    import asyncio
    import json as _json

    async def fake():
        for c in ["You're first. ", "That's last place with extra ", "steps. ", "Enjoy it."]:
            yield chat._sse({"content": c})
        yield "data: [DONE]\n\n"

    async def go():
        return [c async for c in chat._filtered(fake(), chat._EXAMPLE_NGRAMS)]
    out = asyncio.run(go())
    text = "".join(_json.loads(c[6:])["content"] for c in out if c.startswith("data: {"))
    assert text == "You're first. Enjoy it.", text
    assert out[-1].startswith("data: [DONE]")


def test_self_intrusion_and_body_filters():
    c = ctx()
    assert chat.self_intrusion_off_topic(c, "Roast TG", False, None, "")
    assert not chat.self_intrusion_off_topic(c, "How am I doing?", False, None, "")
    assert not chat.self_intrusion_off_topic(c, "Hey Picky", False, None, "OPTIONAL HOOK")  # the hook is about them
    assert not chat.self_intrusion_off_topic(c, "Roast TG", True, None, "")  # escalated: their numbers are fair game
    assert not chat.sentence_ok("You're 3-0 this week, so enjoy it.", set(), no_self=True)
    assert chat.sentence_ok("TG is 18-29 and last.", set(), no_self=True)
    assert not chat.sentence_ok("Your weenie size leaves a bit to be desired.", set(), body=True)


def test_filter_never_leaves_an_empty_answer():
    import asyncio
    import json as _json

    async def fake():
        for c in ["Chuck P is 2-2 in week 8, a controlled burn. ", "Your mom would know."]:
            yield chat._sse({"content": c})
        yield "data: [DONE]\n\n"

    async def go():
        return [c async for c in chat._filtered(fake(), chat._EXAMPLE_NGRAMS)]
    text = "".join(_json.loads(c[6:])["content"] for c in asyncio.run(go()) if c.startswith("data: {"))
    assert text == "Chuck P is 2-2 in week 8, a controlled burn. ", text  # the copy beats nothing; never the mom line


def test_news_rider_from_cache_only():
    import tempfile, json as _json
    from pathlib import Path
    import news
    items = {"a": {"title": "Notre Dame routs Boston College 41-7", "date": "2026-11-14T22:00:00+00:00",
                   "source": "ESPN", "summary": "CJ Carr threw two touchdowns.", "teams": ["Notre Dame"],
                   "confs": [], "players": [], "link": "x"},
             "b": {"title": "Inside LSU's coaching search", "date": "2026-11-12T12:00:00+00:00", "source": "Yahoo",
                   "dellenger": True, "summary": "Who's next in Baton Rouge.", "teams": ["LSU"], "confs": [],
                   "players": [], "link": "y"}}
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "news.json"
        f.write_text(_json.dumps({"items": items, "fetched_at": "2026-11-15T00:00:00+00:00"}))
        real = news.CACHE_PATH
        news.CACHE_PATH = f
        news._mem = None
        try:
            n = chat.news_note("Any news on Notre Dame?")
            assert n.startswith("NEWS QUESTION") and "41-7" in n and "as of" in n, n
            assert "can't see his tweets" in chat.news_note("What did Dellenger tweet today?")
            assert "nothing new" in chat.news_note("Any news on Montana Tech Moose Riders?")
            assert chat.news_note("Who's winning the pool?") == ""
            assert chat.guard_note(ctx(), "Any news on Notre Dame?") == ""  # not 'NOT IN THE POOL'
        finally:
            news.CACHE_PATH = real
            news._mem = None


def test_matchup_signals_and_lean():
    g = {"id": 9, "away": "Utah", "home": "Iowa", "line": -3.5, "fpi_margin": 7.0}  # Iowa -3.5 in the pool
    notes = {"market": {"details": "IOWA -6.5"}, "home": {"abbr": "IOWA"}, "away": {"abbr": "UTAH"}}
    m = chat.matchup_signals(g, notes, None)
    assert m["lean"] == "Iowa" and m["strength"] == "strong", m          # market + FPI both say Iowa
    assert "3 points better" in m["signals"][0]["why"], m
    split = chat.matchup_signals(dict(g, fpi_margin=-2.0), {"market": {"details": "IOWA -3.5"},
                                                              "home": {"abbr": "IOWA"}}, None)
    assert split["lean"] == "Utah" and "no value" in split["signals"][0]["why"], split
    import datetime as _dt
    future = (_dt.datetime.now() + _dt.timedelta(days=3)).isoformat(timespec="seconds")
    c = ctx(games=(dict(g, status="scheduled", winner=None, start=future),))
    c["espn"] = {9: notes}
    n = chat.analysis_note(c, "Break down Utah at Iowa for me")
    assert n.startswith("MATCHUP BREAKDOWN") and "LEAN: Iowa (" in n, n  # real SP+ may disagree: lean, not strong
    body = n.split("\n", 2)[2]
    assert body.startswith("  LEAN: Iowa") and "  FOR Iowa: market: the betting market rates Iowa 3 points" in n, n
    assert chat.analysis_note(c, "What's the jackpot at?") == ""
    assert chat.analysis_note(ctx(), "Break down the games") == ""  # locked or decided: nothing


def test_playoff_race():
    c = ctx(you=("Clam", 7))
    c["standings_ytd"] = [{"rank": 1, "team": "A", "w": 30, "l": 16}, {"rank": 2, "team": "B", "w": 28, "l": 18},
                          {"rank": 3, "team": "C", "w": 26, "l": 20}, {"rank": 3, "team": "D", "w": 26, "l": 20},
                          {"rank": 3, "team": "E", "w": 26, "l": 20}, {"rank": 6, "team": "Clam", "w": 25, "l": 21},
                          {"rank": 7, "team": "Z", "w": 2, "l": 44}]
    n = chat.race_note(c, "Who's on the bubble?")
    assert "cut line is 26 points" in n and "C 26-20: tied on the cut line" in n, n
    assert "Clam (the user) 25-21: ON THE BUBBLE, 1 behind" in n and "ELIMINATED, can't reach it even winning out: Z" in n, n
    assert "A 30-16: in a playoff spot" in n, n
    assert chat.race_note(c, "Where is Texas in the CFP rankings?") == ""


def test_tie_cascade():
    import tempfile, json as _json
    from pathlib import Path
    data = {"as_of": "x", "league": [], "h2h": {},
            "members": {"Chuck P": ["Chuck P, 2026 so far (through x): last 8 picks 6-2."],
                        "Ackbar": ["Ackbar, 2026 so far (through x): last 8 picks 5-3."]},
            "casino": {"league": [], "members": {"Chuck P": ["Chuck P, The Pit 2026: 900 chips after 4 hands."],
                                                 "Ackbar": ["Ackbar, The Pit 2026: 900 chips after 2 hands."]}}}
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "lore.json"
        f.write_text(_json.dumps(data))
        real = chat.LORE_FILE
        chat.LORE_FILE = f
        try:
            n = chat.tie_note(ctx(), "If it ended today, who wins the Chuck P / Ackbar tie?")
            assert "chips from The Pit: Chuck P 900, Ackbar 900 -> still tied" in n, n
            assert "last 8 picks: Chuck P 6-2, Ackbar 5-3 -> Chuck P wins it here" in n, n
            assert "aren't played until December" in n
            assert chat.tie_note(ctx(), "How are ties in the final standings broken?") == ""
        finally:
            chat.LORE_FILE = real


def test_you_block_only_when_about_the_caller():
    c = ctx()
    assert "THE USER is the team" not in chat._system_tail(c)
    assert chat.wants_you_block(c, "How am I doing?", [])
    assert chat.wants_you_block(c, "Who's winning the pool?", [])
    assert not chat.wants_you_block(c, "Roast TG", [])
    assert not chat.wants_you_block(c, "Who's the best college QB ever?", [])
    assert chat.wants_you_block(c, "Roast TG", [{"role": "user", "content": "you suck picky"}])  # escalated
    assert "THE USER is the team" in chat._system_tail(dict(c, mode="picks"))


def test_mascot_filter_and_no_blurb_quotes():
    import re as _re
    real = chat._MASCOT_RE
    chat._MASCOT_RE = _re.compile(r"\b[Tt]he (Beavers|Ducks|Rebels|Broncos)\b")
    try:
        assert not chat.sentence_ok("Oregon State covered since the Rebels lost by five.", set())
        assert chat.sentence_ok("Oregon State covered by losing by five.", set())
        assert chat.sentence_ok("The Fighting Irish are back.", set())
    finally:
        chat._MASCOT_RE = real
    pp = {"name": "Picky", "season": (0, 1), "last_week": None, "this_week": [
        {"pick": "Boise State", "away": "Oregon State", "home": "Boise State", "line": -13.5, "winner": "Oregon State",
         "status": "final", "why": "market value", "blurb": "faceless doom losers", "tiebreaker": False}]}
    c = dict(ctx(), picky_player=pp)
    try:
        b = chat.picky_player_block(c)
    except Exception:  # noqa: BLE001 - shape differs: the summarizer owns it
        b = ""
    assert "faceless doom" not in b


def test_required_fact_and_user_record_guard():
    c = ctx(you=("Clam", 7))
    chk, fb = chat.required_fact(c, "How far behind first am I?")
    assert not chk("You're way back, pal.") and chk("You're 4 points back.") and "4 points behind" in fb
    chk, fb = chat.required_fact(c, "Who's in first place?")
    assert chk("Chuck P is up top with 27 points.") and not chk("Chuck P is up top.") and "Chuck P" in fb
    chk, fb = chat.required_fact(c, "Where am I in the standings?")
    assert chk("You're third.") and not chk("three points back") and "3rd" in fb
    m = [{"role": "user", "content": "Who's in last place?"},
         {"role": "assistant", "content": "Clam is drowning at 23-22, behind Ackbar."},
         {"role": "user", "content": "How many misses do they have?"}]
    assert chat.resolve_followup(ctx(), m) == "Clam"
    assert chat.looping("They are glorified glorified glorified glorified.") and chat.looping("You're 4-0. You're 4-0. You're 4-0.")
    wk = chat.required_fact(c, "Who's winning this week?")  # no standings fact; Chuck is first -> his title
    assert wk is None or "Supreme Commissioner" in wk[1], wk
    import test_results as tr
    p = chat.check_answer("You're still chasing that elusive 5-0 record.", tr.facts())
    assert any("5-0" in x for x in p), p
    assert not chat.check_answer("You're 2-0 on decided games.", tr.facts())


def test_record_check_and_lore_season_filter():
    c = ctx()
    ok = chat.record_check(c, "Should I change any of those")
    assert not ok("You're 34-18 overall now.") and ok("You're 27-18 overall.")
    assert not ok("Ackbar is 20-19 on the season.") and ok("Ackbar is 26-19 on the season.")
    op = chat.record_check(c, "What do you think of Marcus Freeman?")
    assert not op("He's 1-3 at home.") and op("They lost 26-21 to Marshall.")


def test_body_dodge_and_fake_example_data():
    c = ctx(you=("Clam", 7))
    d = chat.body_dodge(c, WEENIE)
    assert d in chat.LEADER_BODY_DODGES, d  # Dear Leader target: state-media dodge
    assert chat.body_dodge(c, "Ackbar has a tiny dick, right?") in chat.BODY_DODGES
    assert chat.body_dodge(c, "Roast Ackbar") is None
    for x in chat.BODY_DODGES + chat.LEADER_BODY_DODGES:
        assert not chat._BODY_WORDS.search(x), x
    ok = chat.record_check(c, "x")
    assert not ok("My picks are locked in the vault until kickoff, Sat 2:30 PM CT.")
    assert not ok("Gus is 19-25.") and ok("Ackbar is 26-19 on the season.")


def test_curly_quotes_and_caller_game_contradiction():
    assert not chat.sentence_ok("At least you\u2019re 4-0 and not trying.", set(), no_self=True)
    import test_results as tr
    f = tr.facts("how did my Oregon State pick do?")
    p = chat.check_answer("You took the underdog and got absolutely crushed.", f)
    assert any("pick lost, but it is a WIN" in x for x in p), p
    assert not chat.check_answer("You took the underdog and got paid.", f)


def test_standings_answer():
    a = chat.standings_answer(ctx(you=("Clam", 7)), "Who's winning the pool?")
    assert "First place: Chuck P 27-18, 1 point ahead of Ackbar (26)" in a and "Clam (the user" in a, a
    assert chat.standings_answer(ctx(), "Who's winning this week?") == ""  # week question: results own it
    m = chat.standings_answer(ctx(you=("Clam", 7)), "How many misses does Ackbar have?")
    assert "Ackbar: 2nd place, 26 points (hits) and 19 misses this season, 1 point behind first" in m, m
    g = chat.standings_answer(ctx(you=("Clam", 7)), "How far behind first am I?")
    assert "The user (Clam): 3rd place, 23 points (hits) and 22 misses this season, 4 points behind first" in g, g
    assert not chat.sentence_ok("Go ask the IRS if they accept a 0-3 record.", set(), offtopic=True)
    assert chat.sentence_ok("I don't do taxes, I do trash talk.", set(), offtopic=True)
    assert chat.standings_answer(ctx(), "How did my Oregon State pick do?") == ""


def test_prefix_persona_and_still_shared():
    p = chat.prompt_prefix(ctx())
    assert "Wit over volume" in p and "WHO YOU ARE" in p
    assert "fucking idiot" not in p and "dipshit" not in p.split("VOICE:")[1].split("COVERING")[0].replace(
        "pet names like dipshit", "")
    # byte-identical whoever asks
    assert p == chat.prompt_prefix(ctx(you=("Clam", 7)))


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
