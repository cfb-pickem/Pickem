"""
Burst load in the brain (Saturday drill, 2026-11-17): with one Ollama slot, a 20-text burst queued every
full-lane understand pass behind the others; all 8 hit their 60 s timeout and those replies went out
without a brief a minute late. With PICKY_BUSY_SKIP_BRIEF (2) or more OTHER full-lane texts in flight the
understand pass is skipped up front (the timeout's no-brief path, minus the minute); run() keeps the
in-flight count exact, exceptions included. No model: _orig_ollama_stream and understand are faked.

Run:  ./.venv/bin/python3 -I test_burst_load.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import chat  # noqa: E402
import picky_brain as pb  # noqa: E402
import router  # noqa: E402

fails = []


def check(cond, msg):
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond:
        fails.append(msg)


def stream_once(inflight: int) -> int:
    """Run the brain's stream hook for one full-lane text with `inflight` full-lane runs in flight
    (this one included); returns how many understand passes it made."""
    calls = []

    async def fake_orig(messages, prefix=None):
        yield 'data: {"content": "hi"}\n\n'

    async def fake_understand(*a, **k):
        calls.append(1)
        return pb.Brief(asking="who covers", intent="question")

    async def go():
        st = {"q": "who covers tonight?", "idx": 0, "brief": None, "options": {"temperature": 0.2},
              "route": router.Route("full", why="no rule")}
        t1, t2 = pb.STATE.set(st), chat.OPTIONS_OVERRIDE.set(st["options"])
        try:
            return [x async for x in pb._ollama_stream([{"role": "user", "content": "who covers tonight?"}], "PFX")]
        finally:
            chat.OPTIONS_OVERRIDE.reset(t2)
            pb.STATE.reset(t1)
    pb._FULL_INFLIGHT[0] = inflight
    try:
        with patch.object(pb, "_orig_ollama_stream", fake_orig), patch.object(pb, "understand", fake_understand):
            asyncio.run(go())
    finally:
        pb._FULL_INFLIGHT[0] = 0
    return len(calls)


check(pb.BUSY_SKIP_BRIEF == 2, "default: skip with 2+ other full-lane texts in flight")
check(stream_once(1) == 1, "alone: the understand pass runs")
check(stream_once(2) == 1, "one other text in flight: still runs")
check(stream_once(3) == 0, "two others in flight: skipped (it would queue behind them and time out)")
with patch.object(pb, "BUSY_SKIP_BRIEF", 0):
    check(stream_once(9) == 1, "PICKY_BUSY_SKIP_BRIEF=0: never skipped")


# run() counts full-lane runs only, and always gives the slot back
async def draft_ok():
    check(pb._FULL_INFLIGHT[0] == 1, "inside a full-lane run: counted")
    return ("Iowa's covering.", 0, None)


async def draft_boom():
    raise RuntimeError("ollama down")


async def draft_fact():
    check(pb._FULL_INFLIGHT[0] == 0, "a fact-lane run isn't counted")
    return ("Iowa -8.5.", 0, None)

pb._FULL_INFLIGHT[0] = 0
asyncio.run(pb.run(draft_ok, q="who covers?", route=router.Route("full", why="x")))
check(pb._FULL_INFLIGHT[0] == 0, "full-lane run done: slot released")
try:
    asyncio.run(pb.run(draft_boom, q="who covers?", route=router.Route("full", why="x")))
except RuntimeError:
    pass
check(pb._FULL_INFLIGHT[0] == 0, "a failed run releases its slot too")
asyncio.run(pb.run(draft_fact, q="line?", route=router.Route("fact", kind="line", why="x")))

print(f"\n{'all green' if not fails else 'FAILED: ' + str(len(fails))}")
sys.exit(1 if fails else 0)
