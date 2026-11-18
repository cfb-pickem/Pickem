"""
Live-first priority for the one Ollama slot, shared by every process on the box.

Ollama runs one generation at a time (OLLAMA_NUM_PARALLEL=1) and queues the rest FIFO, so a member's
text used to wait behind whatever exam item, test ask or batch blurb got there first. Two roles:

  live  the real league: com.pickem.picky-group (:8768) and com.pickem.chat (:8766) call serve_live()
        at startup; their member texts (group, DMs, signed-in web asks) run straight through, and
        live_flag marks each one in flight.
  low   everything else: exam/side sidecars, X-Picky-Test requests inside the live processes,
        eval/test scripts, batch jobs (picky_picks blurbs). Low calls
          - wait while any live text is in flight (live_flag),
          - go one at a time machine-wide (flock on LOCK), so at most ONE low generation is ever in
            Ollama's queue and a live text waits for that one at most,
          - are cancelled when a live text arrives before their first token (closing the HTTP request
            makes Ollama drop it), then retried once the live text is done. Once tokens flow, a low
            stream finishes (its consumer already has part of the text).

Stale state self-heals: the kernel drops a flock when its process dies, and live_flag ignores flag
files older than live_flag.STALE. No daemon.

Knobs: PICKY_GATE=0 restores the old behaviour (side processes only wait for live_flag before a call;
no slot, no preemption). PICKY_ROLE=live|low forces a process's role (probe sidecars for latency
measurements use live). A request can override its own role with PRIORITY (a ContextVar).
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import contextvars
import fcntl
import logging
import os
import time
from pathlib import Path

import live_flag

log = logging.getLogger("ollama_gate")

LOCK = Path(os.environ.get("PICKY_LOW_LOCK", "/tmp/picky-ollama-low.lock"))
ENABLED = os.environ.get("PICKY_GATE", "1") != "0"
MAX_PREEMPTS = 20       # a low call cancelled this many times runs to the end on its next try (no starvation)
POLL = 0.2              # s between live-flag checks
MAX_LOW_WAIT = 900.0    # s; a low call waiting this long for the slot goes ahead anyway

PRIORITY: contextvars.ContextVar[str | None] = contextvars.ContextVar("picky_priority", default=None)
_live_process = False
STATS = collections.Counter()


def serve_live() -> None:
    """Called once by the live servers at startup (a side sidecar stays low: live_flag.is_side())."""
    global _live_process
    _live_process = not live_flag.is_side()


def is_low() -> bool:
    p = PRIORITY.get()
    if p in ("live", "low"):
        return p == "low"
    role = os.environ.get("PICKY_ROLE", "")
    if role in ("live", "low"):
        return role == "low"
    return not _live_process or live_flag.is_side()


@contextlib.contextmanager
def priority(level: str):
    """Run the block (and tasks it starts) at 'live' or 'low' priority."""
    tok = PRIORITY.set(level)
    try:
        yield
    finally:
        PRIORITY.reset(tok)


def _open():
    return os.open(LOCK, os.O_RDWR | os.O_CREAT, 0o666)


def _try_lock(fd: int) -> bool:
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False


def _unlock(fd: int) -> None:
    with contextlib.suppress(OSError):
        fcntl.flock(fd, fcntl.LOCK_UN)


async def _acquire() -> tuple[int, float]:
    """The low slot, taken only while no live text is in flight. Returns (fd, seconds waited)."""
    t0 = time.monotonic()
    fd = _open()
    try:
        while True:
            late = time.monotonic() - t0 > MAX_LOW_WAIT
            if not late and live_flag.busy():
                await asyncio.sleep(POLL)
                continue
            if _try_lock(fd):
                if late or not live_flag.busy():
                    return fd, time.monotonic() - t0
                _unlock(fd)  # a live text showed up while we took the lock: it goes first
            await asyncio.sleep(POLL)
    except BaseException:
        os.close(fd)
        raise


def _release(fd: int) -> None:
    _unlock(fd)
    os.close(fd)


@contextlib.asynccontextmanager
async def slot():
    """Hold the low slot for the block (low priority only; live and PICKY_GATE=0 pass straight through,
    the latter after the old live_flag wait)."""
    if not ENABLED:
        await live_flag.wait_clear()  # the old rule: only side processes wait, before the call
        yield
        return
    if not is_low():
        yield
        return
    fd, waited = await _acquire()
    if waited > 1:
        STATS["low_waits"] += 1
        log.info("gate: low call waited %.1fs for live texts / the low slot", waited)
    try:
        yield
    finally:
        _release(fd)


async def _live_arrived() -> None:
    while not live_flag.busy():
        await asyncio.sleep(POLL)


async def call(make):
    """One non-streamed model call: `make()` returns an awaitable that does the HTTP request(s).
    Low priority: inside the slot, cancelled if a live text arrives, retried when it's done."""
    if not is_low() or not ENABLED:
        async with slot():
            return await make()
    for attempt in range(MAX_PREEMPTS + 1):
        async with slot():
            task = asyncio.ensure_future(make())
            if attempt == MAX_PREEMPTS:
                return await task
            watch = asyncio.ensure_future(_live_arrived())
            try:
                done, _ = await asyncio.wait({task, watch}, return_when=asyncio.FIRST_COMPLETED)
            except BaseException:
                task.cancel()
                raise
            finally:
                watch.cancel()
            if task in done:
                return task.result()
            task.cancel()
            with contextlib.suppress(BaseException):
                await task
            STATS["preempted"] += 1
            log.info("gate: low call preempted by a live text (try %d)", attempt + 1)
    raise AssertionError("unreachable")


async def stream(make):
    """A streamed model call: `make()` returns an async iterator of chunks. Low priority: inside the slot,
    cancelled and restarted if a live text arrives before the first chunk; after that it finishes."""
    if not is_low() or not ENABLED:
        async with slot():
            async for x in make():
                yield x
        return
    for attempt in range(MAX_PREEMPTS + 1):
        async with slot():
            it = make().__aiter__()
            first = asyncio.ensure_future(it.__anext__())
            watch = asyncio.ensure_future(_live_arrived()) if attempt < MAX_PREEMPTS else None
            try:
                waits = {first} | ({watch} if watch else set())
                done, _ = await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
                if first in done:
                    try:
                        x = first.result()
                    except StopAsyncIteration:
                        return
                    yield x
                    async for x in it:
                        yield x
                    return
                first.cancel()
                with contextlib.suppress(BaseException):
                    await first
                STATS["preempted"] += 1
                log.info("gate: low stream preempted by a live text before its first token (try %d)", attempt + 1)
            finally:
                if watch is not None:
                    watch.cancel()
                if not first.done():
                    first.cancel()
                    with contextlib.suppress(BaseException):
                        await first
                with contextlib.suppress(BaseException):
                    await it.aclose()


@contextlib.contextmanager
def slot_sync(low: bool = True, max_wait: float = MAX_LOW_WAIT):
    """Blocking version for scripts (picky_picks blurbs): wait for live texts and the low slot."""
    if not low or not ENABLED:  # PICKY_GATE=0: batch scripts never waited before
        yield
        return
    t0 = time.monotonic()
    fd = _open()
    try:
        while True:
            late = time.monotonic() - t0 > max_wait
            if (late or not live_flag.busy()) and _try_lock(fd):
                if late or not live_flag.busy():
                    break
                _unlock(fd)
            time.sleep(0.5)
        yield
    finally:
        _unlock(fd)
        os.close(fd)


def status() -> dict:
    """For /health: who's low here, is a live text in flight, counters."""
    fd = _open()
    try:
        free = _try_lock(fd)
        if free:
            _unlock(fd)
    finally:
        os.close(fd)
    return {"enabled": ENABLED, "low_here": is_low(), "live_busy": live_flag.busy(), "low_slot_free": free,
            **STATS}
