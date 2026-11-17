"""
Speed lane 2026-11-17 (p95 < 60 s): the reroll budget, the results guard's regen budget, the cold-prime
fallback of the fact/banter calls, the prime's own timeout, and the live group sidecar's keep-warm prime
(gating, exam lock, two-tier rebuild, in-flight tracking).

Run:  ./.venv/bin/python3 -I test_keep_warm.py

Offline: fake clock, fake drafts, fake prime/build; chat.OLLAMA points at a closed port so nothing here
can reach the live model (a stray call would evict the league's prefix checkpoint).
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

os.environ["PICKY_TEST_TOKEN"] = "unit-test-token"
for k in ("PICKY_SIDE", "PICKY_GROUP_PORT", "PICKY_GROUP_EXAM", "PICKY_KEEP_WARM", "PICKY_SOFT_BUDGET"):
    os.environ.pop(k, None)
sys.path.insert(0, ".")
import chat  # noqa: E402
import group_chat as gc  # noqa: E402
import picky_brain as pb  # noqa: E402
import router  # noqa: E402

chat.OLLAMA = "http://127.0.0.1:9"  # closed port
FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("ok   " if ok else "FAIL ") + name + (f"  ({detail})" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


class Clock:
    def __init__(self, t: float = 1_000_000.0):
        self.t = t

    def time(self) -> float:
        return self.t

    def __getattr__(self, name):  # anything else (sleep, monotonic) from the real module
        return getattr(time, name)


# -----------------------------------------------------------------------------
# reroll budget
# -----------------------------------------------------------------------------

def test_unusable():
    for t in ("", "   ", "Next question.", "I do picks, not paperwork.", "I do picks, not paperwork. Next question.",
              "Fair.", "Ha. Fair enough."):
        check(f"unusable: {t!r}", pb.unusable(t))
    for t in ("Northwestern -2.5 is your safest bet since they have a 78.1% FPI to win outright.",
              "Steve Drolet and Andrew Bub are both only 4 points ahead of you."):
        check(f"usable: {t[:40]!r}", not pb.unusable(t))


def test_reroll_ok():
    with patch.object(pb, "SOFT_BUDGET", 50.0), patch.object(pb, "DRAFT_BUDGET", 105.0):
        go, why = pb.reroll_ok(20.0, 15.0, ["A real answer with enough words here."])
        check("reroll runs when the finish fits the soft budget", go, why)
        go, why = pb.reroll_ok(40.0, 20.0, ["A real answer with enough words here."])
        check("reroll skipped when it would bust the soft budget", not go, why)
        check("skip reason names the projection", "63s" in why and "50s" in why, why)
        go, why = pb.reroll_ok(40.0, 20.0, ["Next question."])
        check("an unusable draft rerolls up to DRAFT_BUDGET", go, why)
        go, why = pb.reroll_ok(80.0, 30.0, ["Next question."])
        check("an unusable draft past DRAFT_BUDGET doesn't", not go, why)
        go, why = pb.reroll_ok(40.0, 20.0, ["Next question.", "A real answer with enough words here."])
        check("one usable draft among several: soft budget applies", not go, why)
        with patch.object(pb, "UNUSABLE_BUDGET", 60.0):
            go, why = pb.reroll_ok(40.0, 20.0, ["Next question."])
            check("PICKY_UNUSABLE_BUDGET caps the unusable reroll", not go, why)


async def _run_brain(drafts: list[tuple[float, str, int]], soft: float = 50.0, brief_secs: float = 0.0):
    """pb.run with a fake clock: each draft advances it by its seconds. Returns (reply, n drafts run)."""
    clock = Clock()
    calls = []

    async def draft():
        secs, text, drops = drafts[len(calls)]
        calls.append(text)
        if len(calls) == 1 and brief_secs:
            pb.STATE.get()["brief_secs"] = brief_secs  # what the understand hook records
        clock.t += secs
        return text, drops, None

    with patch.object(pb, "time", clock), patch.object(pb, "SOFT_BUDGET", soft):
        reply, _ = await pb.run(draft, q="who covers tonight", t0=clock.t, route=router.Route("full", why="test"))
    return reply, len(calls)


async def test_run_budget():
    good1 = "Northwestern -2.5 because the run game is the edge, and Minnesota can't stop it."
    good2 = "Northwestern -2.5, the better run game against a soft front."
    _, n = await _run_brain([(40.0, good1, 1), (20.0, good2, 0)])
    check("run: a cut draft at 40 s doesn't reroll (projected 83 s)", n == 1, str(n))
    _, n = await _run_brain([(15.0, good1, 1), (12.0, good2, 0)])
    check("run: a cut draft at 15 s still rerolls (quality when there's time)", n == 2, str(n))
    reply, n = await _run_brain([(15.0, good1, 1), (12.0, good2, 0)])
    check("run: the reroll with fewer cuts wins", reply == good2, reply)
    _, n = await _run_brain([(40.0, "Next question.", 2), (20.0, good2, 0)])
    check("run: a bare dodge at 40 s rerolls (unusable, fits DRAFT_BUDGET)", n == 2, str(n))
    _, n = await _run_brain([(40.0, good1, 0), (20.0, good2, 0)])
    check("run: an uncut draft never rerolls", n == 1, str(n))
    card = "Still Texas Tech -3.5. SP+ has Texas Tech by about 6."
    with patch.object(pb, "undodge", lambda st, text, *a, **k: card if pb._DODGE.match(text or "") else text):
        reply, n = await _run_brain([(40.0, "Next question.", 2), (20.0, good2, 0)])
    check("run: a dodge the card call replaces doesn't reroll past the soft budget", n == 1, str(n))
    check("run: ... and the card call goes out", reply == card, reply)
    # draft 1 took 40 s but 25 of it was the understand pass, which a reroll doesn't redo: 40 + 15 + 3 = 58
    _, n = await _run_brain([(40.0, good1, 1), (15.0, good2, 0)], soft=60.0, brief_secs=25.0)
    check("run: the understand pass isn't counted in the reroll's cost", n == 2, str(n))


async def test_regen_budget_hook():
    seen = {}
    clock = Clock()

    async def draft():
        st = pb.STATE.get()
        st["gen_t0"] = clock.t
        clock.t += 10
        seen["early"] = chat.REGEN_OK.get()()
        clock.t += 35
        seen["late"] = chat.REGEN_OK.get()()
        return "A real answer with enough words in it.", 0, None

    with patch.object(pb, "time", clock), patch.object(pb, "SOFT_BUDGET", 50.0):
        await pb.run(draft, q="how did I do", t0=clock.t, route=router.Route("full", why="test"))
    check("regen_ok: a regen that fits is allowed", seen.get("early") is True, str(seen))
    check("regen_ok: a regen that busts the budget isn't", seen.get("late") is False, str(seen))
    check("REGEN_OK is reset after the run", chat.REGEN_OK.get() is None)


async def test_guard_regen_skipped():
    """chat._guarded_stream: no sentence survived; with no time left the templated answer goes out and
    no second model call is made. With REGEN_OK unset (the site) it still regenerates."""
    calls = []

    async def fake_model_stream(convo, prefix):
        calls.append(1)
        yield chat._sse({"content": "Boise State covered easily."})
        yield "data: [DONE]\n\n"

    async def run_guard():
        out = ""
        async for ch in chat._guarded_stream([{"role": "system", "content": "s"}, {"role": "user", "content": "q"}],
                                             None, {"question": "q"}):
            if ch.startswith("data: {"):
                import json
                out += json.loads(ch[6:]).get("content", "")
        return out

    with patch.object(chat, "_model_stream", fake_model_stream), \
            patch.object(chat, "check_answer", lambda *a, **k: ["says Boise State covered, but it did NOT"]), \
            patch.object(chat, "allowed_line_numbers", lambda *a, **k: set()), \
            patch.object(chat, "correction_rider", lambda *a, **k: "FIX"), \
            patch.object(chat, "templated_answer", lambda facts: "TEMPLATE"):
        tok = chat.REGEN_OK.set(lambda: False)
        try:
            out = await run_guard()
        finally:
            chat.REGEN_OK.reset(tok)
        check("guard: out of time -> templated answer", out == "TEMPLATE", out)
        check("guard: out of time -> one model call, no regen", len(calls) == 1, str(len(calls)))
        calls.clear()
        out = await run_guard()
        check("guard: no budget set -> regenerates (site unchanged)", len(calls) == 2, str(len(calls)))


# -----------------------------------------------------------------------------
# prime: own timeout, background prime, cold-prime fallback
# -----------------------------------------------------------------------------

async def test_prime_timeout():
    got = {}

    class FakeClient:
        async def post(self, url, json=None, timeout=None):
            got["timeout"] = timeout

            class R:
                status_code = 200

                @staticmethod
                def json():
                    return {"prompt_eval_duration": 1}
            return R()

    with patch.object(chat, "MODEL", "gemma4:12b-it-qat"):
        await chat._prime(FakeClient(), "prefix")
        t = got.get("timeout")
        check("prime has its own read timeout (not the caller's 30/60 s)",
              t is not None and t.read == chat.PRIME_TIMEOUT and chat.PRIME_TIMEOUT >= 120, repr(t))
        long = FakeClient()
        long.timeout = chat.httpx.Timeout(connect=5.0, read=420.0, write=10.0, pool=5.0)
        await chat._prime(long, "prefix")
        check("a caller with a longer timeout keeps it (site stream: 420 s)", got["timeout"].read == 420.0,
              repr(got["timeout"]))


async def test_cold_prime_fallback():
    slow = {}

    def fake_start_prime(prefix):
        async def run():
            await asyncio.sleep(1.0)
            slow["done"] = True
        t = asyncio.create_task(run())
        slow["task"] = t
        return t

    with patch.object(chat, "start_prime", fake_start_prime), patch.object(pb, "PRIME_WAIT", 0.05):
        t0 = time.time()
        try:
            await pb._json_call("P", "T", "U", {}, 0.1, 10, 30.0, prime_wait=pb.PRIME_WAIT)
            raised = False
        except pb.PrimeSlow:
            raised = True
        check("json call: a cold prime raises PrimeSlow fast", raised and time.time() - t0 < 0.5)
        check("json call: the prime keeps running in the background", not slow["task"].done())
        st = {"route": router.Route("fact", kind="pick"), "ctx": {"error": "offline"}, "q": "what are your picks",
              "sender": ""}
        out = await pb.fact_reply(st, "P", "My 4 calls: Northwestern -2.5.")
        check("fact lane: cold prime -> the code answer alone", out == "My 4 calls: Northwestern -2.5.", out)
        await slow["task"]
        check("background prime finished", slow.get("done") is True)


# -----------------------------------------------------------------------------
# keep-warm
# -----------------------------------------------------------------------------

def test_keep_warm_on():
    check("live sidecar: on", gc.keep_warm_on({}))
    check("live port 8768: on", gc.keep_warm_on({"PICKY_GROUP_PORT": "8768"}))
    check("exam sidecar: off", not gc.keep_warm_on({"PICKY_GROUP_EXAM": "1"}))
    check("side: off", not gc.keep_warm_on({"PICKY_SIDE": "1"}))
    check("other port: off", not gc.keep_warm_on({"PICKY_GROUP_PORT": "8804"}))
    check("PICKY_KEEP_WARM=1 wins over the side default", gc.keep_warm_on(
        {"PICKY_GROUP_EXAM": "1", "PICKY_SIDE": "1", "PICKY_GROUP_PORT": "8804", "PICKY_KEEP_WARM": "1"}))
    check("PICKY_KEEP_WARM=0: off on live", not gc.keep_warm_on({"PICKY_KEEP_WARM": "0"}))


def test_exam_lock():
    d = tempfile.mkdtemp(prefix="picky-kw-")
    path = os.path.join(d, "exam.lock")
    check("missing lock file: not held (and not created)", not gc.exam_lock_held(path) and not os.path.exists(path))
    open(path, "w").close()
    check("free lock: not held", not gc.exam_lock_held(path))
    p = subprocess.Popen(["/usr/bin/lockf", "-t", "0", path, "sleep", "3"])
    try:
        held = False
        for _ in range(40):
            held = gc.exam_lock_held(path)
            if held:
                break
            time.sleep(0.05)
        check("lockf holding it: held", held)
        check("checking never takes it from lockf", p.poll() is None)
    finally:
        p.terminate()
        p.wait()
    check("released: not held", not gc.exam_lock_held(path))
    check("empty path: never checked", not gc.exam_lock_held(""))


def test_warm_skip():
    now = 5000.0
    base = {"inflight": 0, "last": now - 100}
    check("idle: primes", gc.warm_skip(now, base, False, False, 30) is None)
    check("in flight: skip", gc.warm_skip(now, {**base, "inflight": 1}, False, False, 30) is not None)
    check("a text 5 s ago: skip", gc.warm_skip(now, {**base, "last": now - 5}, False, False, 30) is not None)
    check("live text elsewhere: skip", gc.warm_skip(now, base, False, True, 30) is not None)
    check("exam lock held: skip", "exam lock" in (gc.warm_skip(now, base, True, False, 30) or ""))
    check("memory pressure: skip", "memory" in (gc.warm_skip(now, base, False, False, 5) or ""))
    check("memory unknown: primes", gc.warm_skip(now, base, False, False, None) is None)


async def test_warm_once():
    primes, builds = [], []

    async def prime(c, prefix):
        primes.append(prefix)

    async def build():
        builds.append(1)
        return "PREFIX-BUILT"

    def reset(**kv):
        gc.WARM.update({"inflight": 0, "last": 0.0, "hash": None, "at": 0.0, "built": 0.0, **kv})
        primes.clear()
        builds.clear()

    now = 10_000.0
    with patch.object(gc, "exam_lock_held", lambda *a: False), patch.object(gc, "mem_free_pct", lambda: 40), \
            patch.object(gc.live_flag, "busy", lambda *a: False), patch.object(gc, "KEEP_WARM_CTX_EVERY", 600.0):
        reset()
        out = await gc.warm_once(now, build=build, prime=prime, latest=lambda: None)
        check("no prefix yet: builds and primes it", builds and primes == ["PREFIX-BUILT"], out)
        check("first prime reports a new prefix", "new prefix" in out, out)
        reset(built=now - 60)
        out = await gc.warm_once(now, build=build, prime=prime, latest=lambda: "PREFIX-LAST")
        check("rebuilt recently: re-primes the latest prefix, no Supabase read", not builds and primes == ["PREFIX-LAST"],
              out)
        reset(built=now - 700)
        out = await gc.warm_once(now, build=build, prime=prime, latest=lambda: "PREFIX-LAST")
        check("rebuild due: builds the current context", builds and primes == ["PREFIX-BUILT"], out)
        check("rebuild stamps the time", gc.WARM["built"] == now)

        async def failed_build():
            builds.append(1)
            return None
        reset(built=now - 700)
        out = await gc.warm_once(now, build=failed_build, prime=prime, latest=lambda: "PREFIX-LAST")
        check("failed build: falls back to the latest prefix", primes == ["PREFIX-LAST"], out)
        reset(inflight=1)
        out = await gc.warm_once(now, build=build, prime=prime, latest=lambda: "PREFIX-LAST")
        check("text in flight: no prime", not primes and out.startswith("skip"), out)
        reset(last=now - 3)
        out = await gc.warm_once(now, build=build, prime=prime, latest=lambda: "PREFIX-LAST")
        check("text just finished: no prime", not primes, out)

        async def build_then_text():
            gc.WARM["inflight"] = 1  # a member text lands during the Supabase reads
            return "PREFIX-BUILT"
        reset()
        out = await gc.warm_once(now, build=build_then_text, prime=prime, latest=lambda: None)
        check("text arrives mid-build: it primes for itself", not primes and "arrived" in out, out)
    with patch.object(gc, "exam_lock_held", lambda *a: True), patch.object(gc, "mem_free_pct", lambda: 40), \
            patch.object(gc.live_flag, "busy", lambda *a: False):
        reset()
        out = await gc.warm_once(now, build=build, prime=prime, latest=lambda: "PREFIX-LAST")
        check("exam lock held: no prime, no build", not primes and not builds, out)
    reset()


async def test_member_not_blocked():
    """A text arriving mid keep-warm prime isn't held by any Python lock: only Ollama's queue."""
    started = asyncio.Event()

    async def slow_prime(c, prefix):
        started.set()
        await asyncio.sleep(0.6)

    with patch.object(gc, "exam_lock_held", lambda *a: False), patch.object(gc, "mem_free_pct", lambda: 40), \
            patch.object(gc.live_flag, "busy", lambda *a: False):
        gc.WARM.update(inflight=0, last=0.0, built=time.time())
        warm = asyncio.create_task(gc.warm_once(build=None, prime=slow_prime, latest=lambda: "P"))
        await started.wait()
        t0 = time.time()
        async with gc._HIST_LOCK:  # the only module lock a text takes
            pass
        await gc.save_turn(None, "q", "r")  # no-op without a key, but takes the history path
        check("text path not blocked by a keep-warm prime", time.time() - t0 < 0.2, f"{time.time() - t0:.2f}s")
        await warm


async def test_inflight_middleware():
    seen = {}

    async def app_(scope, receive, send):
        seen["inflight"] = gc.WARM["inflight"]
        await asyncio.sleep(0)

    mw = gc._InflightMiddleware(app_)
    gc.WARM.update(inflight=0, last=0.0)
    await mw({"type": "http", "path": "/group"}, None, None)
    check("middleware counts /group in flight", seen["inflight"] == 1 and gc.WARM["inflight"] == 0, str(seen))
    check("middleware stamps the finish", time.time() - gc.WARM["last"] < 2)
    gc.WARM.update(last=0.0)
    await mw({"type": "http", "path": "/outbox"}, None, None)
    check("the watcher's /outbox poll doesn't count", gc.WARM["last"] == 0.0 and seen["inflight"] == 0, str(seen))


def test_log_levels():
    import logging
    check("warm re-prime logs at debug", gc.warm_log_level("primed abc123 in 0.3s", None) == logging.DEBUG)
    check("cold re-prime logs at info", gc.warm_log_level("primed abc123 in 58.0s (was cold)", None) == logging.INFO)
    check("same skip again logs at debug",
          gc.warm_log_level("skip (exam lock held)", "skip (exam lock held)") == logging.DEBUG)
    check("new skip logs at info", gc.warm_log_level("skip (exam lock held)", None) == logging.INFO)


async def main():
    test_unusable()
    test_reroll_ok()
    await test_run_budget()
    await test_regen_budget_hook()
    await test_guard_regen_skipped()
    await test_prime_timeout()
    await test_cold_prime_fallback()
    test_keep_warm_on()
    test_exam_lock()
    test_warm_skip()
    await test_warm_once()
    await test_member_not_blocked()
    await test_inflight_middleware()
    test_log_levels()


if __name__ == "__main__":
    asyncio.run(main())
    print(f"\n{len(FAILS)} failure(s)" if FAILS else "\nall passed")
    sys.exit(1 if FAILS else 0)
