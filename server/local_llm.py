"""
The local model for Picky's offline writers (zero maintenance, phase 8): the jobs that used to ask Opus for
JSON (league material lines, member-memory extraction, the weekly recap and breakdown texts) ask the box's own
gemma4:12b-it-qat instead. Same prompts, same schemas, same validators after; a bad or failed draft keeps the
job's last good copy or its code template, exactly as an Opus failure did.

  - Ollama's `format` = the job's JSON schema, temperature low, thinking off, chat.py's num_ctx + keep_alive
    (so the loaded chat model is reused, never reloaded);
  - every call goes through ollama_gate.slot_sync(): a batch job waits for live member texts and takes the one
    shared low slot, so it never slows a reply.

    json_call(system, user, schema, caller="league_material") -> dict   (raises LocalLLMError)
"""

from __future__ import annotations

import json
import logging
import os
import time

import httpx

import ollama_gate

log = logging.getLogger("picky.local_llm")
OLLAMA = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
MODEL = os.environ.get("PICKEM_CHAT_MODEL", "gemma4:12b-it-qat")
NUM_CTX = 16384          # chat.py's: a mismatch reloads the model (ledger O4)
TIMEOUT_S = 600.0


class LocalLLMError(RuntimeError):
    pass


def json_call(system: str, user: str, schema: dict, *, caller: str, temperature: float = 0.3,
              num_predict: int = 4096, timeout: float = TIMEOUT_S, transport=None) -> dict:
    body = {"model": MODEL, "stream": False, "think": False, "format": schema,
            "keep_alive": os.environ.get("PICKY_OLLAMA_KEEP_ALIVE", "24h"),
            "options": {"temperature": temperature, "num_ctx": NUM_CTX, "num_predict": num_predict},
            "messages": ([{"role": "system", "content": system}] if system else [])
            + [{"role": "user", "content": user}]}
    t0 = time.time()
    try:
        with ollama_gate.slot_sync(), httpx.Client(timeout=timeout, transport=transport) as c:
            r = c.post(f"{OLLAMA}/api/chat", json=body)
            r.raise_for_status()
            text = r.json()["message"]["content"]
    except (httpx.HTTPError, KeyError, ValueError, TypeError) as e:
        raise LocalLLMError(f"{caller}: local model call failed: {type(e).__name__}: {str(e)[:200]}") from e
    try:
        out = json.loads(text)
    except ValueError as e:
        raise LocalLLMError(f"{caller}: local model returned non-JSON: {text[:120]!r}") from e
    if not isinstance(out, dict):
        raise LocalLLMError(f"{caller}: local model returned {type(out).__name__}, not an object")
    log.info("%s: local %s %.0fs", caller, MODEL, time.time() - t0)
    return out
