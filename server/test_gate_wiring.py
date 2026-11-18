"""
ollama_gate wiring (2026-11-17): who is live and who is low inside the real code paths. No model, no network.

    ./.venv/bin/python3 -I test_gate_wiring.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
_TMP = Path(tempfile.mkdtemp())
os.environ["PICKY_TEST_TOKEN"] = "unit-test-token"
os.environ["PICKY_LIVE_FLAG_DIR"] = str(_TMP / "live")
os.environ["PICKY_LOW_LOCK"] = str(_TMP / "low.lock")
os.environ["PICKY_EXCHANGE_LOG"] = str(_TMP / "ex.jsonl")
for k in ("PICKY_SIDE", "PICKY_GROUP_PORT", "PICKY_ROLE", "PICKY_GATE"):
    os.environ.pop(k, None)
import group_chat as gc  # noqa: E402
import ollama_gate  # noqa: E402
import picky_brain  # noqa: E402

chat = gc.chat


class Req:
    def __init__(self, **headers):
        self.headers = {k.lower().replace("_", "-"): v for k, v in headers.items()}


def run(coro):
    return asyncio.run(coro)


def test_header_priority():
    assert gc.test_priority(Req(X_Picky_Test="unit-test-token")) == "low"
    assert gc.test_priority(Req(X_Picky_Test="unit-test-token", X_Picky_Priority="live")) == "live"  # latency probe
    assert chat.is_test_request(Req(X_Picky_Test="unit-test-token"))
    assert not chat.is_test_request(Req(X_Picky_Test="wrong")) and not chat.is_test_request(Req())  # members


def test_chat_streams_carry_priority():
    seen = []

    async def make():
        seen.append(ollama_gate.PRIORITY.get())
        yield "x"

    async def drain(gen):
        return [x async for x in gen]
    run(drain(chat._low(make)))
    h = chat.GATE.member_in()
    run(drain(chat._member_stream(h, make())))
    assert seen == ["low", "live"], seen


def test_sidecar_draft_keeps_the_callers_priority():
    """group_chat drafts through chat.chat with a header-less request: a test ask (low) must stay low and must not
    raise the live flag (its own low calls would wait on it); a member's draft stays live and holds the flag."""
    seen = []
    orig = chat._chat

    async def fake_chat(req, test, hold, brain=True):
        seen.append((ollama_gate.PRIORITY.get(), hold is not None))
        if hold is not None:
            chat.GATE.member_out(hold)
        return None
    chat._chat = fake_chat
    try:
        async def go(level):
            with ollama_gate.priority(level):
                await chat.chat(Req())
        run(go("low"))
        run(go("live"))
    finally:
        chat._chat = orig
    assert seen == [("low", False), ("live", True)], seen

    got = []

    async def make():
        got.append(ollama_gate.PRIORITY.get())
        yield "x"

    async def inherited():
        with ollama_gate.priority("low"):
            return [x async for x in chat._member_stream(None, make())]
    run(inherited())
    assert got == ["low"], got


def test_brain_json_call_goes_through_gate():
    calls = []
    orig = ollama_gate.call

    async def fake_call(make):
        calls.append(ollama_gate.is_low())
        return {"message": {"content": "{}"}}
    ollama_gate.call = fake_call
    try:
        run(picky_brain._json_call("P", "T", "U", {"type": "object"}, 0.2, 8, 5.0))
        with ollama_gate.priority("live"):
            run(picky_brain._json_call("P", "T", "U", {"type": "object"}, 0.2, 8, 5.0))
    finally:
        ollama_gate.call = orig
    assert calls == [True, False], calls  # a script/test process is low by default; a member's request is live


if __name__ == "__main__":
    n = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("  ok ", name)
            n += 1
    print(f"\n{n}/{n} passed")
