"""
A fact question the code lane couldn't answer stays a question (Saturday drill, 2026-11-17): "how am I
doing this week" missed the results lane (nothing graded yet), the understand pass called it "banter",
the route was demoted to the banter fast path, its line got cut, and Picky sent "Tough to argue."
picky_brain now keeps the full lane when the router's original call was a fact lane (why "fact miss: ..."),
like it already does for football follow-ups. Plain banter routing from the brief is unchanged.

Run:  ./.venv/bin/python3 -I test_fact_miss_banter.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import chat  # noqa: E402
import picky_brain as pb  # noqa: E402
import router  # noqa: E402

fails = []


def check(cond, msg):
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond:
        fails.append(msg)


def run(q: str, why: str) -> tuple[str, bool]:
    """(final lane, banter fast path used) for a full-lane text whose brief says intent=banter."""
    used = []

    async def fake_orig(messages, prefix=None):
        yield 'data: {"content": "You are 21-23."}\n\n'

    async def fake_understand(*a, **k):
        return pb.Brief(asking="their record this week", intent="banter", answer="21-23 on the season.")

    async def fake_banter(st, prefix):
        used.append(1)
        return "Tough to argue."

    async def go():
        rt = router.Route("full", why=why)
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
    return lane, bool(used)


lane, used = run("how am I doing this week", "fact miss: results")
check(lane == "full" and not used, f"fact-lane miss stays full, no banter fast path (got {lane}, banter={used})")
lane, used = run("lol you're cooked picky", "no rule")
check(lane.startswith("banter") and used, f"a no-rule text the brief reads as banter still goes banter (got {lane})")

print(f"\n{'all green' if not fails else 'FAILED: ' + str(len(fails))}")
sys.exit(1 if fails else 0)
