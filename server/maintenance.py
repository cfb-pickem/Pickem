"""
Picky's out-of-office replies (Charlie, 2026-11-15: "have some witty
pre-programmed answers if he gets asked a question while down").

Maintenance is on when the private flag file exists (the LoRA training run
creates it before stopping Ollama and removes it after) OR Ollama doesn't
answer. Then group_chat returns one canned line instantly instead of calling
the model:
  - lines come from a private bank (cfb-pickem-private/picky_maintenance_lines.json,
    written offline with claude -p): "general" plus a few per team name;
    no numbers or stats in any line, so nothing can be wrong
  - random pick, never one of the last 5 sent
  - at most one maintenance reply per chat every 10 minutes (else silence)
"""

from __future__ import annotations

import json
import os
import random
import time
from collections import deque
from pathlib import Path

import httpx

PRIVATE = Path("/Users/Agent/cfb-pickem-private")
FLAG = Path(os.environ.get("PICKY_MAINTENANCE_FILE", PRIVATE / "PICKY_MAINTENANCE"))
LINES_FILE = Path(os.environ.get("PICKY_MAINTENANCE_LINES", PRIVATE / "picky_maintenance_lines.json"))
OLLAMA = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
PER_CHAT_GAP = 600.0   # seconds between maintenance replies in one chat
NO_REPEAT = 5          # never reuse any of the last N lines
PROBE_TTL = 10.0       # seconds to cache the Ollama reachability probe
FALLBACK = ["Getting my brain worked on. Back in a bit, try not to miss me too much.",
            "At the shop getting smarter. Leave a message and I'll get to it when I'm back.",
            "Down for upgrades right now. Back soon with fresh material."]

_recent: deque[str] = deque(maxlen=NO_REPEAT)
_last_by_chat: dict[str, float] = {}
_probe: tuple[float, bool] = (0.0, True)
_bank: tuple[float, dict] = (-1.0, {})


def ollama_up(now: float | None = None) -> bool:
    global _probe
    now = time.time() if now is None else now
    if now - _probe[0] < PROBE_TTL:
        return _probe[1]
    try:
        ok = httpx.get(f"{OLLAMA}/api/tags", timeout=1.5).status_code == 200
    except httpx.HTTPError:
        ok = False
    _probe = (now, ok)
    return ok


def active(now: float | None = None) -> bool:
    return FLAG.exists() or not ollama_up(now)


def bank() -> dict:
    global _bank
    try:
        mt = LINES_FILE.stat().st_mtime
        if mt != _bank[0]:
            d = json.loads(LINES_FILE.read_text())
            _bank = (mt, {k: [s for s in v if isinstance(s, str) and s.strip()] for k, v in d.items()
                          if isinstance(v, list)})
    except (OSError, ValueError):
        return {"general": FALLBACK}
    return _bank[1] if _bank[1].get("general") else {**_bank[1], "general": FALLBACK}


def reply(chat_key: str, team: str | None = None, now: float | None = None,
          rng: random.Random | None = None) -> str | None:
    """One canned line for this chat, or None while the chat is inside its 10-minute gap."""
    now = time.time() if now is None else now
    rng = rng or random
    if now - _last_by_chat.get(chat_key, -1e9) < PER_CHAT_GAP:
        return None
    b = bank()
    tailored = [s for s in b.get(team or "", []) if s not in _recent] if team else []
    pool = tailored if tailored and rng.random() < 0.5 else [s for s in b["general"] if s not in _recent]
    pool = pool or b["general"]
    line = rng.choice(pool)
    _recent.append(line)
    _last_by_chat[chat_key] = now
    return line
