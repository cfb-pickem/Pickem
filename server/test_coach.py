"""
Opus coach mode (Charlie 2026-11-23): picky_coach.py + the group_chat._coach step + the live switch's modes.

(a) verdict routing: pass = Picky's draft goes out unchanged, fix = Opus's answer (after the checks)
(b) any failure (call fails, timeout, bad JSON, off-schema) = the draft, never a blocked reply
(c) the JSON parse guard
(d) the leak guard on corrected answers (member pick claims, ungrounded numbers, Picky's send-point guards)
(e) lessons: persisted (no phone numbers), deduped, injected into Picky's prompt, refreshed without a restart
(f) the live switch CLI: --mode coach|direct|raw, --sample, status shows the mode; coach never sends a
    member's draft generation to Claude (direct mode's hook stays off)
(g) test / canary traffic never reaches the coach; the deep health canary passes in coach mode
(h) the nightly: coach lessons become flub candidates (never-twice fixtures, fix round) + the scorecard

No network, no real claude, no GPU.   Run:  ./.venv/bin/python3 -I test_coach.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
TMP = Path(tempfile.mkdtemp(prefix="picky-coach-test-"))
os.environ["PICKY_CLAUDE_USAGE_DIR"] = str(TMP / "usage")
os.environ["PICKY_LIVE_CLAUDE"] = str(TMP / "switch.json")
os.environ["PICKY_COACH_DIR"] = str(TMP / "coach")
os.environ.update({"PICKY_FLUB_DIR": str(TMP / "flubs"), "PICKY_FLUB_QUEUE": str(TMP / "flubs" / "queue.jsonl"),
                   "PICKY_EXCHANGE_LOG": str(TMP / "exchanges.jsonl"), "PICKY_GROUP_STDERR": str(TMP / "stderr.log"),
                   "PICKY_REPLAY_DIR": str(TMP / "replay"), "PICKY_FLUB_ITEMS_PUBLIC": str(TMP / "flub_items.json"),
                   "PICKY_FLUB_ITEMS_PRIVATE": str(TMP / "flubs" / "items-private.json"),
                   "PICKY_FLUB_KILL_FILE": str(TMP / "FLUB_AUTOFIX_OFF"), "PICKY_GROUP_MEMBERS": str(TMP / "members.json")})
os.environ.pop("PICKY_NO_ANTHROPIC", None)
os.environ.pop("PICKY_COACH_LESSONS", None)

import picky_coach as co  # noqa: E402

FAILED: list[str] = []


def check(name, ok, got=None):
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else f"\n     got: {got!r}"[:900]))
    if not ok:
        FAILED.append(name)


# ---- (c) the JSON parse guard ---------------------------------------------------------------------------
P = co.parse
check("parse: pass", P(json.dumps({"verdict": "pass", "score": 9, "answer": "x", "lesson": "y", "category": "fact"}))
      == {"verdict": "pass", "score": 9, "answer": "", "lesson": "", "category": ""})
fx = P(json.dumps({"verdict": "fix", "score": 3, "answer": "Bravo leads at 30-14.", "lesson": "Don't dodge; answer.",
                   "category": "dodge"}), draft="Ask the commissioner.")
check("parse: fix", fx and fx["verdict"] == "fix" and fx["answer"] == "Bravo leads at 30-14." and fx["score"] == 3, fx)
for bad, why in (("", "empty"), ("not json", "garbage"), ("[]", "a list"), ('{"verdict": "maybe", "score": 5}', "verdict"),
                 ('{"verdict": "pass"}', "no score"), ('{"verdict": "pass", "score": 11}', "score 11"),
                 ('{"verdict": "pass", "score": true}', "bool score"),
                 ('{"verdict": "fix", "score": 4, "answer": "", "lesson": "x", "category": "fact"}', "fix, no answer"),
                 (json.dumps({"verdict": "fix", "score": 4, "answer": "same", "lesson": "x", "category": "fact"}),
                  "fix = the draft"),
                 (json.dumps({"verdict": "fix", "score": 4, "answer": "x" * 2000, "lesson": "x", "category": "fact"}),
                  "fix too long")):
    check(f"parse refuses {why}", P(bad, draft="same") is None, P(bad, draft="same"))
check("parse: unknown category on a fix = other",
      P(json.dumps({"verdict": "fix", "score": 2, "answer": "a", "lesson": "l", "category": "weird"}))["category"] == "other")

# ---- (d) checks on the corrected answer --------------------------------------------------------------
facts = "STANDINGS: 1. Bravo 30-14 (+3 ahead). Iowa -3.5 vs Nebraska. Buy-in $75."
check("grounded numbers pass", co.ungrounded("Bravo's 30-14, up 3. Buy-in is $75.", facts) == [])
check("an invented number is caught", co.ungrounded("Bravo's 31-14.", facts) == ["31"])
check("numbers from the draft / question count", co.ungrounded("Week 12 was rough", facts, "how was week 12?") == [])
check("3.50 matches 3.5", co.ungrounded("Iowa -3.50", facts) == [])
members = ["Bravo", "Kevin's Team"]
check("an unknown member pick claim is caught", co.pick_claims("Bravo took Iowa this week, bold.", members, facts)
      == ["Bravo took Iowa"])
check("a pick claim the chat already said is fine",
      co.pick_claims("Bravo took Iowa, like he said.", members, "bravo took iowa lol") == [])
check("no pick verb, no claim", co.pick_claims("Bravo leads the league at 30-14.", members, facts) == [])

# ---- (e) lessons: persistence, dedupe, prompt block ---------------------------------------------------
co.record({"verdict": "fix", "score": 3, "answer": "Buy-in is $75.", "lesson": "Don't dodge rules questions; answer "
           "with the number from the rules.", "category": "dodge"},
          q="what's the buy-in? text me at 847-555-1212", draft="Ask the commish.", sent="answer", side="group",
          team="Bravo", lane="full", now=time.time() - 50)
co.record({"verdict": "pass", "score": 9, "answer": "", "lesson": "", "category": ""}, q="hi", draft="yo",
          sent="draft", side="dm")
co.record(None, q="hi", draft="yo", sent="draft", side="dm", why="timeout")
L = co.read_lessons()
check("only fix verdicts become lessons", len(L) == 1 and L[0]["category"] == "dodge", L)
check("no phone numbers in the lesson log", "847" not in (TMP / "coach" / "lessons.jsonl").read_text())
check("lesson record fields", all(k in L[0] for k in ("ts", "side", "question", "draft", "answer", "lesson",
                                                     "category", "score", "sent")), L[0])
check("checks log: pass, fix, error", [json.loads(x)["verdict"] for x in
                                       (TMP / "coach" / "checks.jsonl").read_text().splitlines()] == ["fix", "pass", "error"])
check("private files are 0600", oct((TMP / "coach" / "lessons.jsonl").stat().st_mode & 0o777) == "0o600")
recs = [{"ts": 1, "lesson": "Don't dodge rules questions; answer with the number from the rules."},
        {"ts": 2, "lesson": "Don't dodge rules questions; answer with the actual number from the rules."},
        {"ts": 3, "lesson": "Don't roast instead of answering; answer first, then one joke."},
        {"ts": 4, "lesson": "ok"}]
top = co.top_lessons(recs)
check("similar lessons dedupe, the repeated one ranks first", len(top) == 2 and "actual number" in top[0], top)
_W = "apple banana cherry dates elder figs grape honeydew iris jasmine kale lemon mango nectar olive papaya quince " \
     "radish spinach tomato ugli vanilla walnut xigua yam zucchini almond basil cumin dill fennel garlic leek".split()
many = [{"ts": i, "lesson": f"Avoid {_W[i]} {_W[(i + 11) % 32]} {_W[(i + 23) % 32]} {_W[(i + 5) % 32]}."} for i in range(30)]
check("lessons capped at LESSONS_N", len(co.top_lessons(many)) == co.LESSONS_N)
co._block.update(at=0.0, mtime=None, text="")
blk = co.rules_block()
check("rules block carries the lesson", "LESSONS FROM REVIEWED REPLIES" in blk and "answer with the number" in blk, blk)
sig = co.block_sig()
co.record({"verdict": "fix", "score": 4, "answer": "a", "lesson": "Don't answer a different question; answer the "
           "one asked first.", "category": "dodge"}, q="q", draft="d", sent="answer", side="dm")
check("a new lesson waits for the refresh window (the prefix cache stays put)", co.rules_block() == blk)
co._block["at"] -= co.LESSONS_REFRESH_S + 1
check("after the window the block refreshes without a restart", "one asked first" in co.rules_block()
      and co.block_sig() != sig)
with patch.dict(os.environ, {"PICKY_COACH_LESSONS": "0"}):
    check("PICKY_COACH_LESSONS=0 turns the block off", co.rules_block() == "")
check("sampling: 1 in 3", [co.sample_hit(3) for _ in range(6)].count(True) == 2 and co.sample_hit(1))

# ---- (h) scorecard + graduation ------------------------------------------------------------------------
import datetime as dt  # noqa: E402
cdir = TMP / "coach2"
with patch.object(co, "COACH_DIR", cdir):
    day = dt.date(2026, 11, 23)
    rows = []
    for i in range(7):
        d = (day - dt.timedelta(days=i)).isoformat()
        for j in range(20):
            rows.append({"date": d, "verdict": "pass", "score": 9, "sent": "draft"})
    rows.append({"date": day.isoformat(), "verdict": "fix", "score": 4, "category": "voice", "sent": "answer"})
    cdir.mkdir()
    (cdir / "checks.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    sc = co.scorecard("2026-11-23")
    check("scorecard: today's line", sc[0].startswith("## Opus coach: 21 checked, 20 passed, 1 fixed (95% pass"), sc)
    check("scorecard: trend, avg, categories", "prior: 11-22 100%" in sc[0] and "voice 1" in sc[1] and "avg" in sc[1], sc)
    check("graduation READY writes the flag once", "READY" in sc[2] and (cdir / "GRADUATION_READY").exists()
          and "first time" in sc[3], sc)
    check("second night: no second 'first time' line", len(co.scorecard("2026-11-23")) == 3)
    (cdir / "GRADUATION_READY").unlink()
    with open(cdir / "checks.jsonl", "a") as f:
        f.write(json.dumps({"date": "2026-11-23", "verdict": "fix", "score": 2, "category": "fact", "sent": "answer"}) + "\n")
    sc = co.scorecard("2026-11-23")
    check("a fact fix blocks graduation", "not yet" in sc[2] and "1 fact fixes" in sc[2]
          and not (cdir / "GRADUATION_READY").exists(), sc)

# ---- integration: group_chat._coach, the switch modes, the CLI ----------------------------------------
import contextlib  # noqa: E402
import io  # noqa: E402

import chat  # noqa: E402
import group_chat as gc  # noqa: E402
import ollama_gate  # noqa: E402
import picky_claude as pc  # noqa: E402
import router  # noqa: E402

co.COACH_DIR = TMP / "coach3"  # a clean log for the integration part
FAKE = TMP / "claude"
FAKE.write_text("#!/bin/sh\necho x >> \"$(dirname \"$0\")/calls\"\necho 'claude says hi'\n")
FAKE.chmod(0o755)


def claude_calls() -> int:
    try:
        return len((TMP / "calls").read_text().splitlines())
    except OSError:
        return 0


def switch(cfg):
    pc.LIVE_SWITCH.write_text(json.dumps(cfg))
    pc._live.update(mtime=None, fails=0)


def live(coro):
    async def go():
        with ollama_gate.priority("live"):
            return await coro()
    return asyncio.run(go())


FACTS = "STANDINGS: 1. Bravo 30-14. 2. Charlie 28-16. LEAGUE RULES: buy-in $75, 4 games a week."
DRAFT = "Ask the commish, I just work here."
REVIEW = {}
SEEN = {}


async def fake_live_reply(messages, kind="reply", schema=None, timeout=None):
    SEEN.update(messages=messages, kind=kind, schema=schema, timeout=timeout)
    r = REVIEW.get("raw")
    if isinstance(r, Exception):
        raise r
    return r


async def fake_facts():
    return FACTS


def coach(draft=DRAFT, q="what's the buy-in?", mode="dm", route=None, raw=None):
    REVIEW["raw"] = raw
    SEEN.clear()
    s = gc.Sender(handle="+15555550100", team="Bravo", name="Bravo")
    return live(lambda: gc._coach(draft, q=q, s=s, mode=mode, route=route or router.Route("full"), key="k",
                                  charlie=False))


def fixjson(answer, score=3, cat="dodge", lesson="Don't send rules questions to the commissioner; answer them."):
    return json.dumps({"verdict": "fix", "score": score, "answer": answer, "lesson": lesson, "category": cat})


def last_check() -> dict:
    return json.loads((co.COACH_DIR / "checks.jsonl").read_text().splitlines()[-1])


with patch.object(pc, "CLAUDE", str(FAKE)), patch.object(pc, "live_reply", fake_live_reply), \
        patch.object(gc, "_raw_facts", fake_facts):
    switch({"on": True, "model": "opus", "mode": "coach", "raw": False})
    # (a) pass / fix routing
    out = coach(raw=json.dumps({"verdict": "pass", "score": 8, "answer": "", "lesson": "", "category": ""}))
    check("(a) pass: Picky's draft goes out unchanged", out == DRAFT and last_check()["verdict"] == "pass", out)
    check("(a) one Opus call: coach kind, the schema, the 25 s budget", SEEN.get("kind") == "coach"
          and SEEN.get("schema") == co.SCHEMA and SEEN.get("timeout") == co.TIMEOUT == 25.0, SEEN)
    sysm, user = SEEN["messages"][0]["content"], SEEN["messages"][1]["content"]
    check("(a) the review sees the facts, the question and the draft", FACTS in sysm and "what's the buy-in?" in user
          and DRAFT in user, user)
    check("(a) a rules question: the coach gets the rules facts Picky's prompt had", "LEAGUE RULES (for this question)"
          in sysm and "$75" in sysm, sysm[-600:])
    out = coach(raw=fixjson("Buy-in's $75, 4 games a week. Pay up."))
    check("(a) fix: the coach's answer goes out", out == "Buy-in's $75, 4 games a week. Pay up.", out)
    rec = co.read_lessons()[-1]
    check("(a) fix: lesson recorded with the draft and the answer", rec["draft"] == DRAFT and rec["sent"] == "answer"
          and rec["category"] == "dodge" and rec["side"] == "dm" and "commissioner" in rec["lesson"], rec)
    # (b) failures = the draft
    for name, raw in (("call failed / timed out (None)", None), ("bad JSON", "{nope"), ("off-schema", '{"verdict": "x"}'),
                      ("an exception", RuntimeError("boom"))):
        out = coach(raw=raw)
        check(f"(b) {name}: the draft goes out", out == DRAFT and last_check()["verdict"] == "error", (out, last_check()))
    # (d) the leak / grounding guard on a corrected answer
    n = len(co.read_lessons())
    out = coach(raw=fixjson("Buy-in's $80, pay up."))
    check("(d) an ungrounded number: the draft goes out, the lesson is still logged", out == DRAFT
          and len(co.read_lessons()) == n + 1 and co.read_lessons()[-1]["sent"] == "draft"
          and "ungrounded" in co.read_lessons()[-1]["why"], (out, co.read_lessons()[-1]))
    check("_coach_members works with no league ctx", isinstance(gc._coach_members(), list))
    with patch.object(gc, "_coach_members", lambda: ["Bravo", "Charlie"]):
        out = coach(raw=fixjson("Charlie took Iowa this week, so he's toast.", cat="fact"))
    check("(d) a member pick claim nobody made: the draft goes out", out == DRAFT and "pick claim" in last_check()["why"],
          last_check())
    with patch.object(gc, "sides_guard", lambda reply, *a: asyncio.sleep(0, result="dodged")):
        out = coach(raw=fixjson("I'd take Iowa, easy.", cat="unhelpful"), mode="group")
    check("(d) a send-point guard that changes the fix (Picky's hidden side): the draft goes out", out == DRAFT
          and "guard" in last_check()["why"], last_check())
    with patch.object(gc, "fact_final", lambda reply, q: "For the record, you're 30-14."):
        out = coach(raw=fixjson("You're 30-14, first place.", cat="fact"))
    check("(d) fact_final runs on the fix too", out == DRAFT, out)
    # skips
    out = coach(raw=fixjson("x"), route=router.Route("fact", "cfp_bracket"))
    check("cfp_bracket (code text, verbatim): no review", out == DRAFT and not SEEN, SEEN)
    switch({"on": True, "model": "opus", "mode": "coach", "raw": False, "sample": 2})
    co._count[0] = 0
    coach(raw=json.dumps({"verdict": "pass", "score": 9, "answer": "", "lesson": "", "category": ""}))
    first = bool(SEEN)
    coach(raw=fixjson("y"))
    check("--sample 2: every other reply reviewed", first and not SEEN)
    # (g) who reaches the coach
    switch({"on": True, "model": "opus", "mode": "coach", "raw": False})
    check("(g) coach_on: member text, coach mode", live(lambda: asyncio.sleep(0, result=gc.coach_on())))
    tok = gc.TEST_REQ.set(True)
    check("(g) test traffic (canary / deep health: TEST_REQ) never reaches the coach",
          not live(lambda: asyncio.sleep(0, result=gc.coach_on())))
    gc.TEST_REQ.reset(tok)

    async def low_on():
        with ollama_gate.priority("low"):
            return gc.coach_on()
    check("(g) low-priority traffic (exams, no-model eval) never reaches the coach", not asyncio.run(low_on()))
    check("(g) the site widget keeps the normal path", not live(lambda: asyncio.sleep(0, result=gc.coach_on({"x": 1}))))
    with patch.dict(os.environ, {"PICKY_NO_ANTHROPIC": "1"}):
        check("(g) the kill switch shuts the coach", not live(lambda: asyncio.sleep(0, result=gc.coach_on())))
    for cfg, name in (({"on": True, "model": "opus"}, "a pre-coach file (direct)"),
                      ({"on": True, "mode": "raw", "raw": True}, "raw mode"), ({"on": False, "mode": "coach"}, "off")):
        switch(cfg)
        check(f"(g) no coach in {name}", not live(lambda: asyncio.sleep(0, result=gc.coach_on())))
    import inspect  # noqa: E402
    hsrc = inspect.getsource(gc.health)
    check("(g) deep health runs as test traffic (so the canary skips the coach)",
          "TEST_REQ.set(True)" in hsrc and hsrc.index("TEST_REQ.set(True)") < hsrc.index("await asyncio.wait_for(answer("))
    asrc = inspect.getsource(gc._answer)
    check("(a) _answer: coach after the send-point guards, before the reply is saved",
          asrc.index("reply = await sides_guard(") < asrc.index("reply = await _coach(") < asrc.index("await save_turn(key, q_hist, reply, who, lane=route.label())\n        return {\"reply\": reply, \"image\": image"))

# (f) coach mode drafts on gemma: direct mode's hook stays off
GEMMA = []


async def fake_raw(messages, prefix=None):
    GEMMA.append(1)
    yield chat._sse({"content": "gemma says hi"})
    yield "data: [DONE]\n\n"


async def collect():
    out = ""
    async for x in chat._ollama_stream([{"role": "user", "content": "hi"}]):
        if x.startswith("data: {"):
            out += json.loads(x[6:]).get("content", "")
    return out

with patch.object(pc, "CLAUDE", str(FAKE)), patch.object(chat, "_ollama_stream_raw", fake_raw):
    switch({"on": True, "model": "opus", "mode": "coach", "raw": False})
    n = claude_calls()
    out = live(collect)
    check("(f) coach mode: Picky's draft comes from gemma, zero Claude draft calls", out == "gemma says hi"
          and claude_calls() == n and GEMMA, (out, claude_calls() - n))
    switch({"on": True, "model": "opus", "mode": "direct", "raw": False})
    out = live(collect)
    check("(f) direct mode still drafts on Claude", out == "claude says hi" and claude_calls() == n + 1, out)
    switch({"on": True, "model": "opus", "raw": True})
    check("(f) a pre-coach raw:true file is raw mode", pc.live_mode() == "raw" and live(
        lambda: asyncio.sleep(0, result=gc.picky_claude_raw())))
    switch({"on": True, "model": "opus", "mode": "coach", "raw": False})
    check("(f) coach mode is not raw mode", not live(lambda: asyncio.sleep(0, result=gc.picky_claude_raw())))

    def cli(*args) -> str:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            pc.main(["live", *args])
        return buf.getvalue().strip().splitlines()[-1]
    switch({"on": False, "model": "opus", "raw": True, "sample": 5})
    last = cli("on")
    cfg = pc.live_config()
    check("(f) CLI `live on` = coach mode, every reply, raw false", cfg.get("on") is True and cfg.get("mode") == "coach"
          and cfg.get("raw") is False and cfg.get("sample") == 1, cfg)
    check("(f) status shows ON, opus, coach mode", "ON (opus)" in last and "mode coach" in last and "raw false" in last, last)
    last = cli("on", "--model", "sonnet")
    check("(f) `live on --model sonnet` (the coordinator's 'turn on picky claude sonnet'): coach on sonnet",
          pc.live_config().get("model") == "sonnet" and pc.live_mode() == "coach", pc.live_config())
    last = cli("on", "--model", "opus", "--mode", "coach", "--sample", "10")
    check("(f) `--mode coach --sample 10`: spot-check 1 in 10", pc.live_sample() == 10 and "reviews 1 in 10" in last, last)
    cli("on", "--mode", "direct")
    check("(f) `--mode direct`", pc.live_mode() == "direct" and pc.live_config().get("raw") is False)
    cli("on", "--raw")
    check("(f) `--raw` = raw mode (as before)", pc.live_mode() == "raw" and pc.live_config().get("raw") is True)
    cli("on", "--mode", "raw")
    check("(f) `--mode raw`", pc.live_mode() == "raw" and pc.live_config().get("raw") is True)
    last = cli("off")
    check("(f) `live off`: off, mode kept for the next `on`", pc.live_config().get("on") is False
          and "off: off" in last and "mode raw" in last, last)
    cli("on")
    last = cli("status")
    check("(f) `live status` after `on`", "ON (opus)" in last and "mode coach" in last, last)
    pc.live_set(True, raw=False)
    check("(f) live_set(raw=False) (older callers) leaves coach mode alone", pc.live_mode() == "coach")
    pc.live_set(True, mode="raw")
    pc.live_set(True, raw=False)
    check("(f) live_set(raw=False) out of raw mode = direct", pc.live_mode() == "direct")

# (e) the lessons block rides in Picky's local prompt
co.COACH_DIR = TMP / "coach"  # has lessons from the top of this file
co._block.update(at=0.0, mtime=None, text="")
pre = chat.prompt_prefix({})
check("(e) prompt_prefix carries the lessons, before LEAGUE DATA", "LESSONS FROM REVIEWED REPLIES" in pre
      and pre.index("LESSONS FROM REVIEWED REPLIES") < pre.index("LEAGUE DATA:"))
check("(e) the prefix stays byte-identical between messages", chat.prompt_prefix({}) == pre)
check("(e) keep-warm re-primes when the block changes (data_sig carries its hash)", co.block_sig() in gc.data_sig())
co.COACH_DIR = TMP / "nothing-here"
co._block.update(at=0.0, mtime=None, text="")
check("(e) no lessons: the prefix has no block", "LESSONS FROM" not in chat.prompt_prefix({}))

# (h) the nightly: coach lessons -> flub candidates -> never-twice fixtures + the fix round; scorecard in the report
import flub_loop as fl  # noqa: E402
co.COACH_DIR = TMP / "coach4"
t0 = time.time() - 3600
co.record({"verdict": "fix", "score": 3, "answer": "Buy-in's $75.", "lesson": "Don't dodge rules questions; answer.",
           "category": "dodge"}, q="what's the buy-in?", draft="Ask the commish.", sent="answer", side="group",
           team="Bravo", lane="full", now=t0)
date = dt.datetime.fromtimestamp(t0, co.CT).date().isoformat()
(TMP / "flubs").mkdir(exist_ok=True)
h = fl.harvest(date, since=t0 - 60, until=t0 + 60)
c = next((c for c in h["candidates"] if c.get("priority") == "coach"), None)
check("(h) harvest: the coach fix is a candidate (Picky's draft = the flub)", c and c["a"] == "Ask the commish."
      and c["q"] == "what's the buy-in?" and c["mode"] == "group" and h["coach"] == 1, h)
with patch.object(pc, "available", lambda *a: True):
    cl = fl.classify(date, facts={"facts": FACTS, "personas": {"mid": "Bravo"}, "names": ["Bravo"]},
                     opus=lambda prompt: {"verdicts": [{"cid": c["cid"], "flub": False, "category": "", "why": "fine"}]})
v = next((v for v in cl["verdicts"] if v["cid"] == c["cid"]), {})
check("(h) a coach fix stays a flub even if the nightly classifier disagrees", v.get("flub") is True
      and v.get("category") == "dodge", cl)
ad = fl.items(date)
regs = fl.load_regressions()
fx = next((r for r in regs if r.get("cid") == c["cid"]), {})
check("(h) never twice: a permanent fixture (the draft must not come back)", fx
      and fx["expect"].get("not_exact") == ["Ask the commish."] and fx["expect"].get("expected_fail"), (ad, regs))
todo = fl.fix_list(date)
e = next((e for e in todo if e.get("priority") == "NEW"), {})
check("(h) the fix round gets it, with the lesson", "Don't dodge" in (e.get("coach") or "")
      and e.get("coach_answer") == "Buy-in's $75.", todo)
rep = fl.report(date, dry=True)
check("(h) the nightly report carries the coach scorecard", "## Opus coach: 1 checked, 0 passed, 1 fixed" in rep
      and "graduation (7 days): not yet" in rep and "Opus coach fixes harvested: 1" in rep, rep)

print("FAILED: " + ", ".join(FAILED) if FAILED else "all ok")
sys.exit(1 if FAILED else 0)
