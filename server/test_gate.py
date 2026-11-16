"""
Priority gate tests: league members never wait behind test traffic.

Run:  ./.venv/bin/python3 -I test_gate.py

Offline: points chat.py at a fake Ollama on a local port (slow NDJSON token
stream, counts open connections), so it touches neither the GPU nor the
league. Pins: the test token is what makes a request low priority (wrong or
missing = member); a member arriving mid-test aborts the test's Ollama
stream (prime or generation) and the test gets {"preempted": true}; tests
wait while a member is active and for TEST_BACKOFF after; 300 preemptions
leave no open connections and a clean gate.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import sys
import time

os.environ["PICKY_TEST_TOKEN"] = "unit-test-token"
sys.path.insert(0, ".")
import chat  # noqa: E402

OPEN = {"n": 0, "served": 0, "aborted": 0}


async def fake_ollama(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    OPEN["n"] += 1
    try:
        head = await reader.readuntil(b"\r\n\r\n")
        n = int([ln.split(b":")[1] for ln in head.split(b"\r\n") if ln.lower().startswith(b"content-length")][0])
        body_in = json.loads(await reader.readexactly(n) or b"{}")
        path = head.split(b" ")[1]
        # _prime: slow-ish, non-streaming (qwen: raw /api/generate; gemma: /api/chat with stream false)
        if path == b"/api/generate" or body_in.get("stream") is False:
            await asyncio.sleep(random.uniform(0, 0.15))
            body = json.dumps({"response": "", "prompt_eval_duration": 1}).encode()
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
                         + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)
            await writer.drain()
            return
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/x-ndjson\r\nConnection: close\r\n\r\n")
        for i in range(40):  # ~2 s of "tokens"
            writer.write(json.dumps({"message": {"content": f"w{i} "}, "done": False}).encode() + b"\n")
            await writer.drain()
            await asyncio.sleep(0.05)
            if reader.at_eof():
                raise ConnectionResetError
        writer.write(json.dumps({"message": {"content": ""}, "done": True}).encode() + b"\n")
        await writer.drain()
        OPEN["served"] += 1
    except (ConnectionError, asyncio.IncompleteReadError):
        OPEN["aborted"] += 1
    finally:
        OPEN["n"] -= 1
        writer.close()


class Req:
    def __init__(self, headers):
        self.headers = {k.lower(): v for k, v in headers.items()}


def make_stream():
    msgs = [{"role": "system", "content": "prefix tail"}, {"role": "user", "content": "hi"}]
    return chat._model_stream(msgs, "prefix")


async def drain(stream) -> list[dict]:
    out = []
    async for chunk in stream:
        if chunk.startswith("data: {"):
            out.append(json.loads(chunk[6:]))
    return out


def reset():
    chat.GATE = chat.Gate()


async def test_token():
    assert chat.is_test_request(Req({"X-Picky-Test": "unit-test-token"}))
    assert not chat.is_test_request(Req({"X-Picky-Test": "wrong"}))
    assert not chat.is_test_request(Req({}))


async def test_member_unaffected_when_idle():
    reset()
    h = chat.GATE.member_in()
    t0 = time.monotonic()
    got = await drain(chat._gated(False, h, make_stream))
    assert any(o.get("content") for o in got) and not any(o.get("preempted") for o in got)
    assert time.monotonic() - t0 < 3.5
    assert chat.GATE.members() == 0


async def test_test_runs_when_idle():
    reset()
    got = await drain(chat._gated(True, None, make_stream))
    assert "gate_wait" in got[0] and got[0]["gate_wait"] < 0.5
    assert "".join(o.get("content", "") for o in got).startswith("w0")
    assert not any(o.get("preempted") for o in got)
    assert not chat.GATE.test_busy


async def test_test_waits_for_member_and_backoff():
    reset()
    h = chat.GATE.member_in()
    task = asyncio.create_task(drain(chat._gated(True, None, make_stream)))
    await asyncio.sleep(1.0)
    assert not chat.GATE.test_busy, "test started while a member was active"
    chat.GATE.member_out(h)
    t_out = time.monotonic()
    await asyncio.sleep(chat.TEST_BACKOFF - 0.5)
    assert not chat.GATE.test_busy, "test started inside the backoff window"
    got = await task
    assert got[0]["gate_wait"] >= 1.0 + chat.TEST_BACKOFF - 0.3
    assert time.monotonic() - t_out >= chat.TEST_BACKOFF
    assert not any(o.get("preempted") for o in got)


async def test_test_times_out_cleanly():
    reset()
    old, chat.TEST_MAX_WAIT = chat.TEST_MAX_WAIT, 0.5
    h = chat.GATE.member_in()
    try:
        got = await drain(chat._gated(True, None, make_stream))
    finally:
        chat.TEST_MAX_WAIT = old
        chat.GATE.member_out(h)
    assert got == [{"preempted": True, "retry_after": chat.TEST_BACKOFF, "reason": "busy"}]
    assert not chat.GATE.test_busy


async def test_preemptions(n=300):
    """A member arrives at a random point (during prime or mid-generation):
    the test is preempted, Ollama's connection closes, the member starts at once."""
    reset()
    chat.TEST_BACKOFF, backoff = 0.0, chat.TEST_BACKOFF  # keep 300 cycles quick
    waits = []
    try:
        for i in range(n):
            task = asyncio.create_task(drain(chat._gated(True, None, make_stream)))
            await asyncio.sleep(random.uniform(0.01, 1.2))
            h = chat.GATE.member_in()
            t0 = time.monotonic()
            await chat.GATE.member_ready()
            waits.append(time.monotonic() - t0)
            got = await asyncio.wait_for(task, 5)
            assert got[-1].get("preempted") is True, got[-3:]
            chat.GATE.member_out(h)
            for _ in range(50):  # the fake notices a closed socket on its next write
                if OPEN["n"] == 0:
                    break
                await asyncio.sleep(0.02)
            assert OPEN["n"] == 0, f"cycle {i}: {OPEN['n']} Ollama connections still open"
    finally:
        chat.TEST_BACKOFF = backoff
    assert chat.GATE.stats["preemptions"] == n
    assert not chat.GATE.test_busy and chat.GATE.members() == 0
    assert max(waits) < 0.5, f"member waited {max(waits):.2f}s for a preempted test"
    print(f"  {n} preemptions, member wait max {max(waits) * 1000:.0f} ms, "
          f"aborted streams {OPEN['aborted']}")


async def test_client_disconnect_frees_gate():
    reset()
    agen = chat._gated(True, None, make_stream)
    it = agen.__aiter__()
    await it.__anext__()  # gate_wait
    await it.__anext__()  # first token
    await agen.aclose()   # test client went away
    await asyncio.sleep(0.3)
    assert not chat.GATE.test_busy and OPEN["n"] == 0


async def main():
    server = await asyncio.start_server(fake_ollama, "127.0.0.1", 0)
    chat.OLLAMA = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
    tests = [test_token, test_member_unaffected_when_idle, test_test_runs_when_idle,
             test_test_waits_for_member_and_backoff, test_test_times_out_cleanly,
             test_preemptions, test_client_disconnect_frees_gate]
    failed = 0
    for t in tests:
        try:
            await t()
            print(f"ok   {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {t.__name__}: {e}")
    server.close()
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    asyncio.run(main())
