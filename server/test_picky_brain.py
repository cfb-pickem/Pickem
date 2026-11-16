"""
Offline tests for text Picky's multi-pass path (picky_brain.py) and the
banter bank (banter.py). No model, no network.

    ./.venv/bin/python3 -I test_picky_brain.py
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import random
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

import banter  # noqa: E402
import chat  # noqa: E402
import picky_brain as pb  # noqa: E402


def test_parse_brief_is_robust():
    b = pb.parse_brief('{"asking": "best cereal", "intent": "banter", "about": "cereal", "tone": "joking", '
                       '"facts_needed": [], "answer": "Cinnamon Toast Crunch is the best cereal."}')
    assert b.ok() and b.intent == "banter" and b.answer.startswith("Cinnamon")
    assert not pb.parse_brief("").ok() and not pb.parse_brief("not json").ok()
    b = pb.parse_brief('{"asking": "x", "intent": "rant", "tone": "weird", "facts_needed": [1, "standings"]}')
    assert b.intent == "question" and b.tone == "friendly" and b.facts_needed == ["standings"]
    b = pb.parse_brief(json.dumps({"asking": "x", "intent": "insult", "answer": " ".join(["w"] * 60)}))
    assert len(b.answer.split()) == 25


def test_brief_rider_and_insertion():
    b = pb.Brief(asking="your take on the games", intent="question", about="the games", tone="curious",
                 facts_needed=["this week's finals"], answer="Wake Forest covered.")
    r = pb.brief_rider(b)
    assert "They want: your take on the games" in r and "Wake Forest covered." in r and "LEAGUE DATA" in r
    assert "Wake Forest covered." not in pb.brief_rider(b, core=False)
    assert "Fire back" in pb.brief_rider(pb.Brief(asking="a shot at you", intent="insult"))
    assert pb.brief_rider(pb.Brief(asking="")) == ""
    msgs = [{"role": "system", "content": "S"}, {"role": "user", "content": "RIDER A\n\nwho's winning?"}]
    out = pb.insert_rider(msgs, "NOTES", "who's winning?")
    assert out[-1]["content"] == "RIDER A\n\nNOTES\n\nwho's winning?" and msgs[-1]["content"].startswith("RIDER A")
    assert pb.insert_rider(msgs, "NOTES", "not there")[-1]["content"].startswith("NOTES\n\n")


def test_eligible_prefers_whole_fresh_distinct_drafts():
    D = pb.Draft
    ds = [D("You're in first.", 1, 0), D("Clean answer one.", 0, 1), D("Clean answer one!", 0, 2),
          D("Another clean one.", 0, 3), D("", 0, 4)]
    out = pb.eligible(ds, ["Another clean one, again"])
    assert [d.idx for d in out] == [1], out  # cut one out, empty out, dup out, repeated opener out
    assert [d.idx for d in pb.eligible([D("a b c", 2, 0), D("d e f", 1, 1)], [])] == [1]
    # every draft repeats an opener: keep them rather than nothing
    assert len(pb.eligible([D("Same old line", 0, 0)], ["same old line again"])) == 1


def test_stale_records_catch_a_true_record_from_the_wrong_week():
    ctx = {"current_week": 11, "standings_ytd": [{"team": "Blinkstradamus", "w": 27, "l": 21},
                                                  {"team": "Ackbar", "w": 28, "l": 20}],
           "weekly_records": {"Blinkstradamus": {8: [1, 3], 10: [3, 1], 11: [3, 1]},
                              "Ackbar": {10: [4, 0], 11: [3, 1]}}}
    bad = pb.stale_records("Don't expect me to fix your 1-3 record while you're snacking.", ctx, "Blinkstradamus")
    assert bad, "1-3 was week 8, not this week"
    assert not pb.stale_records("You went 3-1 and you're 27-21.", ctx, "Blinkstradamus")
    assert not pb.stale_records("You were 1-3 in week 8.", ctx, "Blinkstradamus")  # qualified: other checks own it
    assert pb.stale_records("Ackbar is 2-2, bless him.", ctx, "Blinkstradamus")
    assert not pb.stale_records("Ackbar went 4-0 last week.", ctx, "Blinkstradamus")
    assert not pb.stale_records("I'm 0-3 this season.", ctx, "Blinkstradamus")  # Picky's own: not a member


def _with_judge(fn):
    """These pin the PICKY_JUDGE=1 path (3 drafts + judge); the default is one draft, no judge."""
    def wrapped():
        old = pb.USE_JUDGE, pb.N_DRAFTS
        pb.USE_JUDGE, pb.N_DRAFTS = True, 3
        try:
            fn()
        finally:
            pb.USE_JUDGE, pb.N_DRAFTS = old
    wrapped.__name__ = fn.__name__
    return wrapped


def test_pipeline_default_one_draft_no_judge():
    calls = []

    async def draft():
        st = pb.STATE.get()
        calls.append(st["idx"])
        st["prefix"] = "PREFIX"
        return ("Cinnamon Toast Crunch, no contest.", 0, None)

    async def no_judge(*a, **k):
        raise AssertionError("judge must not run by default")

    old_j, old_u = pb.judge, pb.understand
    async def fake_understand(*a, **k):
        return pb.Brief()
    pb.judge, pb.understand = no_judge, fake_understand
    try:
        out, img = asyncio.run(pb.run(draft, q="best cereal?", sender="Blinkstradamus"))
    finally:
        pb.judge, pb.understand = old_j, old_u
    assert calls == [0] and out.startswith("Cinnamon") and img is None


@_with_judge
def test_pipeline_drafts_screens_and_judges():
    texts = iter([("You're 1-3, Blinky.", 0, None), ("Cinnamon Toast Crunch, no contest.", 0, None),
                  ("Frosted Flakes. Obviously.", 0, None)])
    seen = []

    async def draft():
        st = pb.STATE.get()
        seen.append(st["idx"])
        # what chat.py would do: build the prompt, stream through the hook
        st["ctx"] = {"current_week": 11, "standings_ytd": [{"team": "Blinkstradamus", "w": 27, "l": 21}],
                     "weekly_records": {"Blinkstradamus": {11: [3, 1]}}}
        st["prefix"] = "PREFIX"
        return next(texts)

    async def fake_judge(prefix, q, sender, brief, drafts, timeout=45.0):
        assert prefix == "PREFIX" and len(drafts) == 2 and "1-3" not in " ".join(drafts)
        return 1

    with patch.object(pb, "judge", fake_judge):
        reply, image = asyncio.run(pb.run(draft, q="best cereal?", sender="Blinky", team="Blinkstradamus"))
    assert reply == "Frosted Flakes. Obviously." and image is None
    assert seen == [0, 1, 2], seen
    assert chat.OPTIONS_OVERRIDE.get() is None and pb.STATE.get() is None  # reset after


@_with_judge
def test_pipeline_extra_draft_only_when_all_cut_and_image_short_circuits():
    calls = []

    async def draft():
        calls.append(1)
        return ("cut reply", 1, None)

    with patch.object(pb, "judge", lambda *a, **k: asyncio.sleep(0, result=0)):
        reply, _ = asyncio.run(pb.run(draft, q="x"))
    assert len(calls) == 4 and reply == "cut reply"

    async def pic():
        return ("", 0, "data:image/png;base64,AAA")
    assert asyncio.run(pb.run(pic, q="draw me a dog")) == ("", "data:image/png;base64,AAA")


def test_hook_injects_brief_ammo_angle_and_temperature():
    got = {}

    async def fake_orig(messages, prefix=None):
        got["user"] = messages[-1]["content"]
        got["temp"] = chat.OPTIONS_OVERRIDE.get()["temperature"]
        yield 'data: {"content": "hi"}\n\n'

    async def fake_understand(prefix, q, sender, recent, last_reply="", timeout=60.0, stance=""):
        return pb.Brief(asking="a shot at you", intent="insult", about="Picky", answer="Own it, jab back.")

    async def go():
        st = {"q": "you suck", "idx": 2, "brief": None, "options": {"temperature": 0.2}}
        t1, t2 = pb.STATE.set(st), chat.OPTIONS_OVERRIDE.set(st["options"])
        try:
            return [x async for x in pb._ollama_stream([{"role": "user", "content": "R\n\nyou suck"}], "PFX")], st
        finally:
            chat.OPTIONS_OVERRIDE.reset(t2)
            pb.STATE.reset(t1)

    with patch.object(pb, "_orig_ollama_stream", fake_orig), patch.object(pb, "understand", fake_understand), \
            patch.object(banter, "load", lambda: {"suck": ["Line one about sucking.", "Line two, sharper."]}):
        out, st = asyncio.run(go())
    assert out and got["temp"] == 0.9 and st["prefix"] == "PFX"
    u = got["user"]
    assert u.endswith("you suck") and "YOUR READ OF THIS TEXT" in u and "AMMO" in u and "ANGLE FOR THIS DRAFT" in u
    assert u.index("AMMO") < u.rindex("you suck")


def test_banter_match_options_and_rider():
    assert banter.match("you suck") == "suck" and banter.match("all bark no bite lol") == "bark"
    assert banter.match("why don't you work better") == "bad" and banter.match("are you a bot or what") == "bot"
    assert banter.match("you're a pussy") == "coward" and banter.match("best cereal?") == "cereal"
    assert banter.match("what's the best tailgate beer") == "beer" and banter.match("who's winning?") is None
    assert banter.match("who wins the super bowl") == "nfl" and banter.match("eat shit") is None
    lines = [f"Line number {w} here." for w in "abcdefgh"]
    with patch.object(banter, "load", lambda: {"suck": lines}):
        banter._recent.clear()
        rng = random.Random(1)
        a, b = banter.options("suck", rng=rng), banter.options("suck", rng=rng)
        assert len(a) == 2 and not set(a) & set(b)  # no repeats across back-to-back calls
        r = banter.ammo_rider("nobody likes you", intent="insult")  # no regex hit: the brief's intent
        assert r.startswith("AMMO") and r.count("\n- ") == 2
    assert banter.ammo_rider("who's winning?") == ""


def test_banter_validate_and_due():
    ok = banter.line_ok("Cinnamon Toast Crunch, and the leftover cinnamon is the postgame show.", ["blinky"])
    assert ok
    assert banter.line_ok("Cinnamon Toast Crunch, and the cinnamon milk is the postgame show.", []) is None  # milk bit
    assert banter.line_ok("You went 3-1, relax.", []) is None             # numbers
    assert banter.line_ok("Blinky would pick Froot Loops anyway, honestly.", ["blinky"]) is None  # member
    assert banter.line_ok("Your mom picks better than you do on Saturdays.", []) is None  # family
    assert banter.line_ok("Too short.", []) is None
    assert banter.line_ok("Nice try — but no.", []) is None or "—" not in banter.line_ok("Nice try — but I'm still here.", [])
    cats, dropped = banter.validate({"categories": {"suck": ["You went 4-0? Sure.", "A fine comeback line right here."]}},
                                    prev={"suck": ["old one", "old two", "old three", "old four"]})
    assert cats["suck"] == ["old one", "old two", "old three", "old four"] and dropped  # too few: kept last week's
    now = dt.datetime(2026, 11, 18, 11, 0, tzinfo=banter.CT)  # a Wednesday, after 10
    assert banter.due(None, now) == "missing"
    assert banter.due({"categories": {"x": []}, "generated_at": "2026-11-15T17:00:00-06:00"}, now) == "weekly"
    assert banter.due({"categories": {"x": []}, "generated_at": "2026-11-18T10:30:00-06:00"}, now) is None
    assert banter.due({"categories": {"x": []}, "generated_at": "2026-11-01T10:30:00-06:00"}, now) == "stale"


def test_banter_refresh_writes_private_and_keeps_old_on_failure():
    good = {"categories": {k: [f"A perfectly fine {k} line number {w}." for w in "abcdef"]
                           for k, *_ in banter.CATEGORIES}}
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "bank.json"
        res = banter.refresh(force=True, research=lambda p: good, out=out)
        assert res["built"] and oct(out.stat().st_mode & 0o777) == "0o600"
        assert len(json.loads(out.read_text())["categories"]) == len(banter.CATEGORIES)
        try:
            banter.refresh(force=True, research=lambda p: {"categories": {}}, out=out)
            raise AssertionError("should have refused an empty bank")
        except RuntimeError:
            pass
        assert len(json.loads(out.read_text())["categories"]) == len(banter.CATEGORIES)  # old bank kept


def test_rare_bits_milk_budget_once_a_month():
    import rare_bits
    with tempfile.TemporaryDirectory() as d, patch.object(rare_bits, "STATE", Path(d) / "bits.json"):
        assert rare_bits.available("milk")
        assert rare_bits.mentions("milk", "Nice milk, freak.") and rare_bits.mentions("milk", "the Jeep girls")
        assert not rare_bits.mentions("milk", "Cinnamon Toast Crunch, no contest.")
        assert rare_bits.note_sent("Clean reply, no bits.") == [] and rare_bits.available("milk")
        t0 = 1_800_000_000
        assert rare_bits.note_sent("Mr. Two Percent strikes again.") == ["milk"]
        rare_bits.spend("milk", now=t0)
        assert oct((Path(d) / "bits.json").stat().st_mode & 0o777) == "0o600"
        assert not rare_bits.available("milk", now=t0 + 29 * 86400)
        assert rare_bits.available("milk", now=t0 + 30 * 86400)


def test_milk_gated_everywhere_and_opener_gone():
    import rare_bits
    assert not hasattr(chat, "BLINKSTRADAMUS_LINE")  # Charlie: the "nice milk, freak." opener is removed
    blinky = {"you": {"team_name": "Blinkstradamus"}}
    other = {"you": {"team_name": "Ackbar"}, "games_this_week": [], "last_week": {}}
    with patch.object(rare_bits, "available", lambda b: False):
        assert not chat.milk_allowed(blinky, "best cereal?")
        assert "No milk" in chat.blinks_note(other, "what's the deal with blinky")
        tok = chat.MILK_OK.set(False)
        try:
            assert not chat.sentence_ok("He's the roadside carton legend.", set())
            assert chat.sentence_ok("Blinky went 3-1 and still found a way to complain.", set())
        finally:
            chat.MILK_OK.reset(tok)
    with patch.object(rare_bits, "available", lambda b: True):
        assert chat.milk_allowed(blinky, "best cereal?") and chat.milk_allowed(other, "how's blinky doing")
        assert not chat.milk_allowed(other, "who's winning?")  # never in a reply that isn't about Blinky
        assert "NICE MILK FREAK" in chat.blinks_note(other, "what's the deal with blinky")


# ---- stance computed in code (live 2026-11-15: 0-3 Picky told 4-0 Chuck P to "look in the mirror") ----

STANDINGS_WK11 = [("Chuck P", 30, 18, 1), ("Ackbar", 28, 20, 2), ("Blinkstradamus", 27, 21, 3), ("DaddyFreeman", 27, 21, 3),
                  ("Liam Bryson", 26, 22, 5), ("Rettino", 26, 22, 5), ("Breaking Horrendous", 25, 23, 7),
                  ("Wedge Mulshine", 25, 23, 7), ("Clam", 24, 24, 9), ("Brian", 23, 25, 10), ("Steve Drolet", 23, 25, 10),
                  ("Andrew Bub", 22, 26, 12), ("Kevin Culligan", 21, 23, 13), ("TG", 19, 29, 14)]


def _stance_ctx(me: str, picky_wins: bool = False) -> dict:
    """The real week 11 (2026-11-15): Chuck P 4-0 and 1st, Picky 0-3."""
    g = lambda i, a, h, ln, w: {"id": i, "away": a, "home": h, "line": ln, "winner": w, "status": "final",
                                 "tiebreaker": False}
    games = [g(1, "Wake Forest", "SMU", 1.5, "Wake Forest"), g(2, "Oregon State", "Boise State", -13.5, "Oregon State"),
             g(3, "North Texas", "UTSA", 2.5, "UTSA"), g(4, "North Dakota State", "Hawai'i", 6.5, "North Dakota State")]
    live = [{"GameId": 1, "Away": "Wake Forest", "Home": "SMU", "AwayPts": 49, "HomePts": 7, "Status": "final"},
            {"GameId": 2, "Away": "Oregon State", "Home": "Boise State", "AwayPts": 41, "HomePts": 46, "Status": "final"},
            {"GameId": 3, "Away": "North Texas", "Home": "UTSA", "AwayPts": 16, "HomePts": 35, "Status": "final"},
            {"GameId": 4, "Away": "North Dakota State", "Home": "Hawai'i", "AwayPts": 21, "HomePts": 13, "Status": "final"}]
    pk = lambda *xs: [{"game_id": i + 1, "pick": x} for i, x in enumerate(xs) if x]
    mine = [("Boise State", games[1]), ("North Texas", games[2]), ("Hawai'i", games[3])]
    if picky_wins:  # a Picky who went 3-0: he took the side that covered
        mine = [(gm["winner"], gm) for _, gm in mine]
    this_week = [{"pick": p, "away": gm["away"], "home": gm["home"], "line": gm["line"], "winner": gm["winner"],
                  "status": "final", "tiebreaker": False, "label": p, "game": f"{gm['away']} at {gm['home']}",
                  "result": "covered" if p == gm["winner"] else "missed", "why": None, "blurb": None}
                 for p, gm in mine]
    w = sum(p["result"] == "covered" for p in this_week)
    return {"current_week": 11, "games_this_week": games, "live_games": live,
            "revealed_picks_this_week": {
                "Chuck P": pk("Wake Forest", "Oregon State", "UTSA", "North Dakota State"),
                "Ackbar": pk("Wake Forest", "Boise State", "UTSA", "North Dakota State"),
                "Andrew Bub": pk("SMU", "Boise State", "North Texas", "Hawai'i"),
                "TG": pk("SMU", "Oregon State", "North Texas", "Hawai'i")},
            "weekly_records": {"Chuck P": {11: [4, 0]}, "Ackbar": {11: [3, 1]}, "Andrew Bub": {11: [0, 4]},
                               "TG": {11: [1, 3]}},
            "standings_ytd": [{"team": t, "w": a, "l": b, "rank": r} for t, a, b, r in STANDINGS_WK11],
            "you": {"team_id": 1, "team_name": me}, "leader": {"team_id": 6, "team_name": "Chuck P"},
            "picky_player": {"name": "Picky", "season": [w, 3 - w], "last_week": [0, 0], "this_week": this_week,
                             "hidden_count": 0,
                             "revealed": [{k: p[k] for k in ("pick", "away", "home", "line", "winner", "status",
                                                             "tiebreaker")} | {"week": 11} for p in this_week]}}


def _stance(me, q, **kw):
    lore = {"members": {"Chuck P": ["Chuck P, 2026 so far (through 2026-11-15): takes the favorite 54% of the time."],
                        "Ackbar": ["Ackbar, 2026 so far (through 2026-11-15): takes the favorite 75% of the time."]}}
    with patch.object(chat, "load_lore", lambda: lore), patch.object(chat, "member_nicknames", lambda: {}):
        return pb.compute_stance(_stance_ctx(me, **kw), q, me)


def test_stance_4_0_asker_vs_0_3_picky_is_deflect():
    s = _stance("Chuck P", "you suck at picks?")
    assert s and s["kind"] == "picky" and s["stance"] in ("deflect", "own_it"), s
    r = pb.stance_rider(s)
    assert "Chuck P (the sender): 4-0 this week, 30-18 on the season, 1st place of 14." in r, r
    assert "You (Picky): 0-3 this week, 0-3 on the season, not in the standings." in r, r
    assert "your pick Boise State -13.5 = LOSS" in r and "missed the cover by 8.5" in r, r
    assert "never mock them" in r and "Chuck P's 4-0 this week" in r and "Chuck P's 1st place" in r, r
    assert "look in the mirror" in r.lower() and "takes the favorite 54%" in r, r
    assert "Oregon State +13.5 = " not in r  # Oregon State was never Picky's pick


def test_stance_own_it_brag_and_none():
    assert _stance("TG", "you suck")["stance"] == "own_it"            # 1-3 / 19-29 still beats 0-3 / 0-3
    assert _stance("Andrew Bub", "picky you're trash")["stance"] == "own_it"
    assert _stance("Ackbar", "why don't you work better")["stance"] == "deflect"  # 3-1 and 2nd: crushing it
    s = _stance("Andrew Bub", "you suck at picks", picky_wins=True)   # 3-0 Picky vs an 0-4 week
    assert s["stance"] == "brag" and "Andrew Bub's 0-4 this week" in s["bad"], s
    assert _stance("TG", "you suck at this", picky_wins=True)["stance"] == "brag"
    for q in ("what's the best cereal?", "how are my picks doing?", "who's winning the league?", "why do you like Boise?"):
        assert _stance("Chuck P", q) is None, q


def test_stance_for_roast_requests():
    s = _stance("TG", "roast Ackbar")
    assert s["kind"] == "roast" and s["stance"] == "deflect", s
    r = pb.stance_rider(s)
    assert "Ackbar (who they want roasted): 3-1 this week, 28-20 on the season, 2nd place of 14." in r, r
    assert "TG (the sender): 1-3 this week, 19-29 on the season, 14th place of 14." in r, r
    assert "Between them, Ackbar is doing better." in r and "Ackbar's 3-1 this week" in r, r
    assert "takes the favorite 75%" in r and "Boise State -13.5" in r, r  # his real miss, graded in code
    s = _stance("Ackbar", "roast Charlie")                              # Dear Leader is never roasted by others
    assert s["stance"] == "deflect" and s["jabs"] and all("Chuck P" not in j for j in s["jabs"]), s
    assert any("Ackbar" in j for j in s["jabs"]) and "refuse to roast him" in pb.stance_rider(s)
    s = _stance("Ackbar", "roast Andrew Bub")                          # 0-4 week, Picky 0-3
    assert s["stance"] == "own_it" and "Andrew Bub's 0-4 this week" in s["bad"], s
    assert "Fair game (actually bad): Andrew Bub's 0-4 this week" in pb.stance_rider(s)
    assert _stance("Ackbar", "roast Andrew Bub", picky_wins=True)["stance"] == "brag"
    assert _stance("Chuck P", "roast me")["stance"] == "deflect"


def test_stance_violations_cut_superior_lines():
    s = _stance("Chuck P", "you suck at picks?")
    reply = ("You suck at picks? Look in the mirror, Chuck P. You're 4-0 this week, but you're still last in the "
             "league at being a Notre Dame homer. That 4-0 is a fluke. Fair, 4-0 is 4-0 and I'm 0-3.")
    assert pb.stance_violations(reply, s) == ["Look in the mirror, Chuck P.", "That 4-0 is a fluke."]
    live2 = "I'm 0-3 this week, but you're 4-0, so you're the one who needs a reality check. Fair play."
    assert pb.stance_violations(live2, s) == [live2.rsplit(" Fair", 1)[0]]  # replay 2026-11-16
    live3 = "But you took LSU last week, so maybe you're just lucky to be perfect right now."
    assert pb.stance_violations(live3, s) == [live3]
    assert pb.stance_violations("Look in the mirror.", _stance("Andrew Bub", "you suck", picky_wins=True)) == []


def test_hook_carries_stance_and_drops_escalate():
    got = {}

    async def fake_orig(messages, prefix=None):
        got["user"] = messages[-1]["content"]
        yield 'data: {"content": "hi"}\n\n'

    async def fake_understand(prefix, q, sender, recent, last_reply="", timeout=60.0, stance=""):
        got["understand_stance"] = stance
        return pb.Brief(asking="a shot at you", intent="insult", about="Picky", answer="Own it.")

    ctx = _stance_ctx("Chuck P")
    body = "ESCALATE: this user has come at you. Build the burns from their real record. top it.\n\nyou suck at picks?"

    async def go():
        st = {"q": "you suck at picks?", "team": "Chuck P", "ctx": ctx, "idx": 0, "brief": None,
              "options": {"temperature": 0.2}}
        t1, t2 = pb.STATE.set(st), chat.OPTIONS_OVERRIDE.set(st["options"])
        try:
            return [x async for x in pb._ollama_stream([{"role": "user", "content": body}], "PFX")], st
        finally:
            chat.OPTIONS_OVERRIDE.reset(t2)
            pb.STATE.reset(t1)

    with patch.object(pb, "_orig_ollama_stream", fake_orig), patch.object(pb, "understand", fake_understand), \
            patch.object(chat, "load_lore", lambda: {}), patch.object(chat, "member_nicknames", lambda: {}):
        _, st = asyncio.run(go())
    u = got["user"]
    assert st["stance"]["stance"] == "deflect" and "STANCE" in got["understand_stance"]
    assert "STANCE" in u and u.index("STANCE") < u.rindex("you suck at picks?"), u
    assert "ESCALATE" not in u and "Fire back at them first" not in u, u


def test_bare_insult_gets_stance_from_brief_and_drops_hook():
    """Live 2026-11-16 DM: Chuck P (4-0, 1st) texted 'Pussy' after a hostile turn. No regex caught it, so no
    stance: the brief said "fire back first" and 0-3 Picky answered "That's a lot of big talk from a guy who's
    basically just a professional point-collector ... You're currently in first place, 2 points ahead of second."
    (the last sentence was chat.py's OPTIONAL HOOK)."""
    assert _stance("Chuck P", "Pussy") is None  # the regexes alone miss it
    got = {}

    async def fake_orig(messages, prefix=None):
        got["user"] = messages[-1]["content"]
        yield 'data: {"content": "hi"}\n\n'

    async def fake_understand(prefix, q, sender, recent, last_reply="", timeout=60.0, stance=""):
        return pb.Brief(asking="The user is calling me a pussy.", intent="insult", about="Picky", tone="hostile",
                        answer="Chuck P is 4-0 this week while I'm 0-3.")

    ctx = _stance_ctx("Chuck P")
    body = ("OPTIONAL HOOK: after answering, you may add ONE short sentence, in your own words and voice, with this "
            "fact about the user (say 'you'): the user is in first place, 2 points ahead of second. Copy its numbers "
            "exactly. Skip it if it would feel forced.\n\nPussy")

    async def go(q):
        st = {"q": q, "team": "Chuck P", "ctx": ctx, "idx": 0, "brief": None, "options": {"temperature": 0.2}}
        t1, t2 = pb.STATE.set(st), chat.OPTIONS_OVERRIDE.set(st["options"])
        try:
            return [x async for x in pb._ollama_stream([{"role": "user", "content": body}], "PFX")], st
        finally:
            chat.OPTIONS_OVERRIDE.reset(t2)
            pb.STATE.reset(t1)

    with patch.object(pb, "_orig_ollama_stream", fake_orig), patch.object(pb, "understand", fake_understand), \
            patch.object(chat, "load_lore", lambda: {}), patch.object(chat, "member_nicknames", lambda: {}):
        _, st = asyncio.run(go("Pussy"))
    u = got["user"]
    assert st["stance"] and st["stance"]["kind"] == "picky" and st["stance"]["stance"] == "deflect", st["stance"]
    assert "STANCE" in u and "Chuck P's 1st place" in u and u.endswith("Pussy"), u
    assert "Fire back at them first" not in u and "OPTIONAL HOOK" not in u, u
    live = ("That's a lot of big talk from a guy who's basically just a professional point-collector. "
            "I'm sitting at 0-3 this week while you're out here being 4-0.")
    assert pb.stance_violations(live, st["stance"]) == [live.split(" I'm", 1)[0]]


def test_pipeline_cuts_off_stance_sentence_and_rerolls():
    texts = iter([("Look in the mirror, Chuck P. You're 4-0, I'm 0-3.", 0, None),
                  ("Fair, 4-0 is 4-0. I went 0-3, so I'll sit down.", 0, None)])

    async def draft():
        st = pb.STATE.get()
        st["stance"] = _stance("Chuck P", "you suck at picks?")
        return next(texts)

    async def fake_understand(*a, **k):
        return pb.Brief()
    with patch.object(pb, "understand", fake_understand):
        out, _ = asyncio.run(pb.run(draft, q="you suck at picks?", team="Chuck P"))
    assert out == "Fair, 4-0 is 4-0. I went 0-3, so I'll sit down.", out


def test_few_shot_turns_match_and_respect_stance():
    bank = {"cereal": ["Cinnamon Toast Crunch, no debate.", "Frosted Flakes, grown man cereal.", "Cap'n Crunch."],
            "suck": ["Bold talk from the guy asking a bot for help.", "Noted. Filed under jealousy.", "Cry more."]}
    with patch.object(banter, "load", lambda: bank):
        t = pb.few_shot_turns("best cereal?", "question", None)
        assert [m["role"] for m in t] == ["user", "assistant"] * 3 and t[0]["content"] == "best cereal?", t
        assert pb.few_shot_turns("you suck", "insult", {"stance": "brag"})
        assert pb.few_shot_turns("you suck", "insult", {"stance": "own_it"}) == []   # smug shots fight own_it
        assert pb.few_shot_turns("who's winning?", "question", None) == []


if __name__ == "__main__":
    fails = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ok  {name}")
            except Exception as e:  # noqa: BLE001
                fails += 1
                print(f"FAIL  {name}: {e!r}")
    print("\nALL GREEN" if not fails else f"\n{fails} FAILED")
    sys.exit(1 if fails else 0)
