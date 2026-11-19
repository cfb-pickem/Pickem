"""
Thinking mode on the full brain's analysis drafts (ledger M10 retry, Charlie OK 2026-11-19 12:27 CT).

M10 failed on qwen3.5 (457 s runaway). Here gemma4 thinks only on draft 1 of a full-brain football analysis
ask (picks, matchups, personnel, big picture, situational), under two hard caps: a thinking-token cap and a
wall-clock deadline. Either cap firing, or any error, returns None and the caller runs today's non-thinking
draft, so a runaway can never reach a member. Fact lanes, banter, news, rules and anything touching a live
game never think.

PICKY_THINK_ANALYSIS=0 turns it off. PICKY_THINK_TOKENS / PICKY_THINK_TIMEOUT set the caps.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import time

import httpx

import chat
import ollama_gate
import router

log = logging.getLogger("think_lane")

ENABLED = os.environ.get("PICKY_THINK_ANALYSIS", "1") != "0"
TOKENS = int(os.environ.get("PICKY_THINK_TOKENS") or 640)      # thinking chunks (~tokens) before we give up
TIMEOUT = float(os.environ.get("PICKY_THINK_TIMEOUT") or 45)   # seconds for the whole thinking draft

STATS = {"tried": 0, "ok": 0, "fallback": 0}

_NEWSY = re.compile(r"\b(news|headlines?|latest|what happened|rumou?rs?|tweet\w*|breaking|any updates?)\b", re.I)
_ANALYSIS = re.compile(
    r"\bwho (?:you|ya|u|do you|would you|d'?you) (?:got|like|taking|take|have|pick\w*)\b|\bwho covers\b|"
    r"\b(side|cover\w*|spread|line|lay(?:ing)?|points|total|over|under|value|edge|lean|pick\w*|bet|"
    r"match-?ups?|run game|pass rush|defen[cs]e|offense|qb|quarterback|starter|backup|injur\w*|hurt|"
    r"weather|wind|altitude|travel|rest|bye|letdown|lookahead|trap|motivat\w*|rivalry|revenge|"
    r"cfp|playoff|committee|title|clinch\w*|coach\w*|hot seat|interim|ats|too many|should i|worth)\b|[+-]\d",
    re.I)


def wanted(st: dict, intent: str) -> bool:
    """Draft 1 of a full-brain football analysis question, nothing else."""
    if not ENABLED or st.get("thought"):
        return False
    rt = st.get("route")
    if rt is None or rt.lane != "full" or (intent or "question") != "question":
        return False
    ctx = st.get("ctx") or {}
    if ctx.get("live_rider"):  # Saturday's live path stays fast
        return False
    q = st.get("q") or ""
    if _NEWSY.search(q) or chat.wants_rules(q) or router.provocation(q):
        return False
    return bool(_ANALYSIS.search(q))


async def _stream(messages: list[dict], prefix: str | None, options: dict, info: dict):
    payload = {"model": chat.MODEL, "messages": messages, "stream": True, "think": True,
               "options": {**chat.OLLAMA_OPTIONS, **options,
                           "num_predict": TOKENS + int(chat.OLLAMA_OPTIONS.get("num_predict") or 320)},
               "keep_alive": chat.OLLAMA_KEEP_ALIVE}
    timeout = httpx.Timeout(connect=5.0, read=TIMEOUT + 15, write=10.0, pool=5.0)
    async with httpx.AsyncClient(timeout=timeout) as c:
        if prefix:
            await chat._prime(c, prefix)
        info["t_gen"] = time.time()
        async with c.stream("POST", f"{chat.OLLAMA}/api/chat", json=payload) as r:
            if r.status_code != 200:
                info["why"] = f"http {r.status_code}"
                return
            async for line in r.aiter_lines():
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                m = obj.get("message") or {}
                if m.get("thinking"):
                    info["think"] += 1
                    if info["think"] > TOKENS:
                        info["why"] = "think cap"
                        return  # closing the stream stops Ollama
                if m.get("content"):
                    info.setdefault("t_first", time.time())
                    yield m["content"]
                if obj.get("done"):
                    info.update(done=True, prompt_eval=obj.get("prompt_eval_count"), out=obj.get("eval_count"),
                                prompt_secs=round((obj.get("prompt_eval_duration") or 0) / 1e9, 1))
                    return


async def draft(messages: list[dict], prefix: str | None, options: dict) -> tuple[str | None, dict]:
    """(answer or None, info). None = fall back to the non-thinking draft."""
    STATS["tried"] += 1
    info: dict = {"think": 0, "done": False, "why": ""}
    t0 = time.time()
    parts: list[str] = []

    async def collect():
        gen = ollama_gate.stream(lambda: _stream(messages, prefix, options, info))
        async with contextlib.aclosing(gen):
            async for x in gen:
                parts.append(x)
                if chat.looping("".join(parts)):
                    info["why"] = "looping"
                    return

    try:
        await asyncio.wait_for(collect(), TIMEOUT)
    except asyncio.TimeoutError:
        info["why"] = info["why"] or "timeout"
    except (httpx.HTTPError, ValueError) as e:
        info["why"] = f"error {type(e).__name__}"
    text = "".join(parts).strip()
    if info["done"] and not info["why"] and ("<|channel" in text or "<channel|>" in text):
        info["why"] = "thought markers in content"
    info["secs"] = round(time.time() - t0, 1)
    ok = info["done"] and not info["why"] and bool(text)
    if not ok and not info["why"]:
        info["why"] = "empty"
    STATS["ok" if ok else "fallback"] += 1
    log.info("think %s: %.1fs, think %d chunks, out %s, prompt %s tok in %ss%s", "ok" if ok else "fallback",
             info["secs"], info["think"], info.get("out"), info.get("prompt_eval"), info.get("prompt_secs"),
             f" ({info['why']})" if info["why"] else "")
    return (text if ok else None), info
