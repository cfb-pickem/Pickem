"""
Cross-process live-text flag (live_flag.py): live replies mark themselves in
flight; side sidecars and eval/run.py wait before each model call.

Run:  ./.venv/bin/python3 -I test_live_flag.py

Offline: a temp flag dir, no Ollama (chat.OLLAMA points at a closed port).
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ["PICKY_TEST_TOKEN"] = "unit-test-token"
os.environ.pop("PICKY_SIDE", None)
os.environ.pop("PICKY_GROUP_PORT", None)
sys.path.insert(0, ".")
import live_flag  # noqa: E402

live_flag.DIR = Path(tempfile.mkdtemp(prefix="picky-live-test-"))
FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("ok   " if ok else "FAIL ") + name + (f"  ({detail})" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


def env(**kv):
    for k, v in kv.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def test_side_detection():
    env(PICKY_SIDE=None, PICKY_GROUP_PORT=None)
    check("live by default", not live_flag.is_side())
    env(PICKY_GROUP_PORT="8768")
    check("live group sidecar port is live", not live_flag.is_side())
    env(PICKY_GROUP_PORT="8785")
    check("group sidecar on another port is side", live_flag.is_side())
    env(PICKY_GROUP_PORT=None, PICKY_SIDE="1")
    check("PICKY_SIDE=1 is side", live_flag.is_side())
    env(PICKY_SIDE="0")
    check("PICKY_SIDE=0 is live", not live_flag.is_side())
    env(PICKY_SIDE=None)


def test_begin_end():
    check("idle at start", not live_flag.busy())
    a = live_flag.begin()
    b = live_flag.begin()
    check("begin sets the flag", live_flag.busy() and a and b and a != b)
    live_flag.end(a)
    check("one reply still in flight", live_flag.busy())
    live_flag.end(b)
    check("end clears it", not live_flag.busy())
    live_flag.end(b)  # double end is harmless
    live_flag.end(None)
    with live_flag.held():
        check("held() sets it", live_flag.busy())
    check("held() clears it", not live_flag.busy())
    try:
        with live_flag.held():
            raise ValueError
    except ValueError:
        pass
    check("held() clears on error", not live_flag.busy())


def test_side_never_sets():
    env(PICKY_SIDE="1")
    tok = live_flag.begin()
    check("side begin is a no-op", tok is None and not live_flag.busy())
    env(PICKY_SIDE=None)


def test_stale():
    tok = live_flag.begin()
    old = time.time() - live_flag.STALE - 5
    os.utime(tok, (old, old))
    check("stale file (crashed process) doesn't count", not live_flag.busy())
    live_flag.end(tok)


async def test_wait():
    check("live process never waits", await live_flag.wait_clear() == 0.0)
    env(PICKY_SIDE="1")
    check("side waits 0 when idle", await live_flag.wait_clear() < 0.2)
    env(PICKY_SIDE=None)
    tok = live_flag.begin()
    env(PICKY_SIDE="1")

    async def release():
        await asyncio.sleep(0.6)
        live_flag.end(tok)
    asyncio.ensure_future(release())
    w = await live_flag.wait_clear(poll=0.05)
    check("side waits until the live reply ends", 0.5 <= w < 1.5, f"{w:.2f}s")
    env(PICKY_SIDE=None)
    tok = live_flag.begin()
    w = await live_flag.wait_clear(force=True, max_wait=0.3, poll=0.05)
    check("force (eval/run.py) waits too, bounded by max_wait", 0.25 <= w < 0.8, f"{w:.2f}s")
    live_flag.end(tok)


async def test_chat_hooks():
    import chat
    k = chat.GATE.member_in()
    check("chat GATE.member_in sets the cross-process flag", live_flag.busy())
    chat.GATE.member_out(k)
    check("chat GATE.member_out clears it", not live_flag.busy())

    chat.OLLAMA = "http://127.0.0.1:9"  # closed port: the stream errors out as soon as it really starts
    tok = live_flag.begin()
    env(PICKY_SIDE="1")
    gen = chat._ollama_stream([{"role": "user", "content": "hi"}])
    task = asyncio.ensure_future(gen.__anext__())
    await asyncio.sleep(0.8)
    check("side _ollama_stream holds before calling Ollama", not task.done())
    live_flag.end(tok)
    try:
        first = await asyncio.wait_for(task, 10)
    except StopAsyncIteration:
        first = ""
    check("side _ollama_stream proceeds once clear", "error" in first or first == "", first[:80])
    env(PICKY_SIDE=None)
    check("keep_alive default keeps the chat model loaded", chat.OLLAMA_KEEP_ALIVE in ("24h", "-1")
          or "PICKY_OLLAMA_KEEP_ALIVE" in os.environ, chat.OLLAMA_KEEP_ALIVE)


async def main():
    test_side_detection()
    test_begin_end()
    test_side_never_sets()
    test_stale()
    await test_wait()
    await test_chat_hooks()


if __name__ == "__main__":
    asyncio.run(main())
    print(f"\n{len(FAILS)} failure(s)" if FAILS else "\nall passed")
    sys.exit(1 if FAILS else 0)
