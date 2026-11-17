"""
Group burst vs chat.py's per-user rate limit (Saturday drill, 2026-11-17). Every group ask runs chat.py
in-process with PICKY's own session, so the per-user 10-a-minute bucket was shared by the whole group,
drafts and rerolls included: the 11th model call in a minute raised 429, the first draft of that ask
failed, /group errored and the watcher sent nothing (member's ask silently dropped). The sidecar's
in-process calls now skip that bucket (chat.SKIP_RATE, set in group_chat._answer); the watcher still
caps group replies (10 s gap, 60 an hour) and site asks are still counted once at chat.py's /chat.

Run:  ./.venv/bin/python3 -I test_group_rate.py
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import chat  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from starlette.requests import Request as StarletteRequest  # noqa: E402

fails = []


def check(cond, msg):
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond:
        fails.append(msg)


class Past(Exception):
    """Raised right after the rate check: the call got through it."""


def inproc_request() -> StarletteRequest:
    body = json.dumps({"messages": [{"role": "user", "content": "who's covering?"}], "supabase_jwt": "jwt",
                       "mode": "leaderboard"}).encode()

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}
    return StarletteRequest({"type": "http", "method": "POST", "path": "/chat", "headers": [], "query_string": b""},
                            receive)


async def calls(n: int, skip: bool) -> list[str]:
    out = []
    tok = chat.SKIP_RATE.set(True) if skip else None
    try:
        for _ in range(n):
            try:
                await chat.chat(inproc_request())
                out.append("ok")
            except Past:
                out.append("ok")
            except HTTPException as e:
                out.append(str(e.status_code))
    finally:
        if tok is not None:
            chat.SKIP_RATE.reset(tok)
    return out


async def fake_verify(_client, _jwt):
    return "picky-own-uid"


def stop(*_a, **_k):
    raise Past()


with patch.object(chat, "_verify_jwt", fake_verify), patch.object(chat, "land_ack_for", stop):
    chat._hits.clear()
    got = asyncio.run(calls(12, skip=False))
    check(got[:10] == ["ok"] * 10 and got[10:] == ["429", "429"],
          "without the flag: one shared session hits 429 on the 11th call in a minute (the bug)")
    chat._hits.clear()
    got = asyncio.run(calls(25, skip=True))
    check(got == ["ok"] * 25, "group sidecar in-process calls (SKIP_RATE): 25 in a minute, no 429")
    check(chat.SKIP_RATE.get() is False, "the flag is per-context and off by default (site /chat still limited)")
    chat._hits.clear()
    got = asyncio.run(calls(11, skip=False))
    check(got[-1] == "429", "a site user is still capped at 10 a minute")

src = (HERE / "group_chat.py").read_text()
i = src.index("async def _answer(")
body = src[i:src.index("\n\n\n", i)]
check("chat.SKIP_RATE.set(True)" in body and "chat.SKIP_RATE.reset(" in body,
      "group_chat._answer sets SKIP_RATE for its in-process chat.py calls and resets it")

print(f"\n{'all green' if not fails else 'FAILED: ' + str(len(fails))}")
sys.exit(1 if fails else 0)
