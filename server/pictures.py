"""Picture policy + async delivery (review R2, decided 2026-11-17).

A picture unloads gemma and holds the box for 3-7 minutes, so:
  - never during the Saturday game window (11:00-24:00 CT) or while another member text is pending:
    a polite refusal in Picky's voice instead;
  - at most PER_HOUR (5) per person per hour (image.py enforces the same number per user_id);
  - one at a time;
  - asynchronous on the text path: the reply is "drawing it" right away, the picture is generated in the
    background and delivered through the outbox (group_outbox.post_picture) to the chat that asked.
The image prompt is only the member's own words, expanded in code by picture_prompt.py with public team data
(mascot, colors) and a no-lettering style suffix; no league context or member data goes to the image service.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
import random
import time
import zoneinfo
from collections import deque

import live_flag

CT = zoneinfo.ZoneInfo("America/Chicago")
PER_HOUR = 5
GAME_DAY = 5  # Saturday (Monday = 0)
WINDOW = (dt.time(11, 0), dt.time(23, 59, 59))
log = logging.getLogger("picky.pictures")

DRAWING = ["Drawing it. Give me a few minutes, art takes time.",
           "On it. The art department needs a few minutes.",
           "Sketching now. Back in a few minutes with it."]
REFUSE_GAMEDAY = ["No pictures on game day, the whole art department is watching football. Ask me tomorrow.",
                  "Art department's closed Saturdays during games. Hit me up tomorrow and I'll draw it."]
REFUSE_BUSY = ["Give me a minute, I'm answering somebody. Ask for the picture again in a bit.",
               "Hang on, somebody's waiting on an answer first. Try the picture again in a minute."]
REFUSE_DRAWING = ["One masterpiece at a time. I'm already drawing one, ask again in a few minutes."]
REFUSE_LIMIT = [f"That's {PER_HOUR} pictures this hour, the art department needs a break. Try again later."]
FAILED = ["The brush slipped on that one. My fault. Try again in a bit."]

_recent: dict[str, deque] = {}
_busy = {"since": 0.0}


def game_window(now: dt.datetime | None = None) -> bool:
    now = (now or dt.datetime.now(CT)).astimezone(CT)
    return now.weekday() == GAME_DAY and WINDOW[0] <= now.time() <= WINDOW[1]


def pending_texts(own: int = 0) -> int:
    """Member texts in flight across processes (live_flag files), minus the caller's own."""
    now = time.time()
    try:
        n = sum(1 for e in os.scandir(live_flag.DIR) if now - e.stat().st_mtime < live_flag.STALE)
    except OSError:
        n = 0
    return max(0, n - own)


def drawing() -> bool:
    return bool(_busy["since"]) and time.time() - _busy["since"] < 600


def _over_limit(who: str, now: float, mark: bool) -> bool:
    hits = _recent.setdefault(who, deque())
    while hits and now - hits[0] > 3600:
        hits.popleft()
    if len(hits) >= PER_HOUR:
        return True
    if mark:
        hits.append(now)
    return False


def refusal(who: str, own_flag: int = 0, now: dt.datetime | None = None, mark: bool = True) -> str | None:
    """A polite no (Picky's voice), or None = go ahead (and count it against the hourly limit)."""
    if game_window(now):
        return random.choice(REFUSE_GAMEDAY)
    if drawing():
        return random.choice(REFUSE_DRAWING)
    if pending_texts(own_flag):
        return random.choice(REFUSE_BUSY)
    if _over_limit(who or "?", time.time(), mark):
        return random.choice(REFUSE_LIMIT)
    return None


def start(prompt: str, user_id: str, deliver) -> asyncio.Task:
    """Generate in the background; `deliver(text, image_b64 | None)` runs when it's done (or failed)."""
    import chat  # late: chat imports nothing from here

    async def job():
        _busy["since"] = time.time()
        try:
            status, b64 = await chat.generate_image(prompt, user_id)
            if status == 200 and b64:
                await asyncio.to_thread(deliver, "", b64)
            elif status == 429:
                await asyncio.to_thread(deliver, random.choice(REFUSE_LIMIT), None)
            else:
                await asyncio.to_thread(deliver, random.choice(FAILED), None)
        except Exception as e:  # noqa: BLE001 - a background job: log, tell them, never raise
            log.exception("picture job failed: %s", e)
            try:
                await asyncio.to_thread(deliver, random.choice(FAILED), None)
            except Exception:  # noqa: BLE001
                pass
        finally:
            _busy["since"] = 0.0

    t = asyncio.create_task(job())
    _TASKS.add(t)
    t.add_done_callback(_TASKS.discard)
    return t


_TASKS: set = set()
