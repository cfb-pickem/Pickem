"""
Live-first Ollama gate (ollama_gate.py): live texts go straight through, low calls (exams, tests,
batch jobs) go one at a time, wait for live texts and get cancelled + retried when one arrives
before their first token. Stale flags and dead lock holders self-heal.

Run:  ./.venv/bin/python3 -I test_ollama_gate.py

Offline: temp flag dir + temp lock file, fake model calls (no Ollama).
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

for k in ("PICKY_SIDE", "PICKY_GROUP_PORT", "PICKY_ROLE", "PICKY_GATE"):
    os.environ.pop(k, None)
sys.path.insert(0, ".")
import live_flag  # noqa: E402
import ollama_gate as g  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="picky-gate-test-"))
live_flag.DIR = TMP / "live"
g.LOCK = TMP / "low.lock"
g.POLL = 0.02
FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("ok   " if ok else "FAIL ") + name + (f"  ({detail})" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


def live_on() -> Path:
    live_flag.DIR.mkdir(exist_ok=True)
    p = live_flag.DIR / f"test-{time.monotonic_ns()}"
    p.touch()
    return p


def holder(seconds: float) -> subprocess.Popen:
    """Another process holding the low slot (an exam's in-flight call)."""
    code = ("import fcntl,os,sys,time;fd=os.open(sys.argv[1],os.O_RDWR|os.O_CREAT,0o666);"
            "fcntl.flock(fd,fcntl.LOCK_EX);print('held',flush=True);time.sleep(float(sys.argv[2]))")
    p = subprocess.Popen([sys.executable, "-c", code, str(g.LOCK), str(seconds)], stdout=subprocess.PIPE, text=True)
    p.stdout.readline()
    return p


def roles():
    check("default process is low (exams, scripts, tests)", g.is_low())
    g.serve_live()
    check("serve_live() makes the process live", not g.is_low())
    with g.priority("low"):
        check("a test request inside the live process is low", g.is_low())
    os.environ["PICKY_GROUP_PORT"] = "8810"
    g.serve_live()
    check("a side sidecar stays low even if it calls serve_live()", g.is_low())
    os.environ["PICKY_ROLE"] = "live"
    check("PICKY_ROLE=live (latency probe sidecar) is live", not g.is_low() and not live_flag.is_side())
    for k in ("PICKY_GROUP_PORT", "PICKY_ROLE"):
        os.environ.pop(k)
    g._live_process = False


async def timed(coro) -> tuple[object, float]:
    t0 = time.monotonic()
    out = await coro
    return out, time.monotonic() - t0


async def fake(result="ok", secs=0.0, log=None):
    if log is not None:
        log.append("start")
    await asyncio.sleep(secs)
    if log is not None:
        log.append("end")
    return result


async def main():
    roles()

    # live never waits for the low slot
    h = holder(1.0)
    with g.priority("live"):
        out, dt = await timed(g.call(lambda: fake("live")))
    check("live call runs while a low call holds the slot", out == "live" and dt < 0.3, f"{dt:.2f}s")
    # a low call waits for the other process's low call (one low generation in Ollama at a time)
    out, dt = await timed(g.call(lambda: fake("low")))
    check("low call waits for the slot held by another process", out == "low" and dt > 0.6, f"{dt:.2f}s")
    h.wait()

    # dead holder: the kernel drops the flock
    h = holder(30)
    h.kill()
    h.wait()
    out, dt = await timed(g.call(lambda: fake("after-crash")))
    check("a killed holder's lock self-heals", out == "after-crash" and dt < 0.3, f"{dt:.2f}s")

    # low waits while a live text is in flight
    f = live_on()
    asyncio.get_running_loop().call_later(0.5, f.unlink)
    out, dt = await timed(g.call(lambda: fake("low")))
    check("low call waits for an in-flight live text", dt >= 0.45, f"{dt:.2f}s")

    # stale flag (crashed live process) doesn't block
    f = live_on()
    old = time.time() - live_flag.STALE - 5
    os.utime(f, (old, old))
    out, dt = await timed(g.call(lambda: fake("low")))
    check("a stale live flag doesn't block low calls", dt < 0.3, f"{dt:.2f}s")
    f.unlink()

    # preemption: a live text arriving mid-call cancels the low call, which reruns after
    log: list[str] = []
    flag: list[Path] = []
    loop = asyncio.get_running_loop()
    loop.call_later(0.2, lambda: flag.append(live_on()))
    loop.call_later(0.6, lambda: flag[0].unlink())
    out, dt = await timed(g.call(lambda: fake("low", 0.5, log)))
    check("low call is cancelled by a live text and retried", out == "low" and log == ["start", "start", "end"]
          and g.STATS["preempted"] >= 1, f"{log} {dt:.2f}s")

    # live process: a test request (low) yields to a member text in the same process
    g.serve_live()
    log = []
    loop.call_later(0.1, lambda: flag.append(live_on()))
    loop.call_later(0.4, lambda: flag[-1].unlink())
    with g.priority("low"):
        out, _ = await timed(g.call(lambda: fake("t", 0.3, log)))
    check("a test request in the live process is preempted by a member text", log == ["start", "start", "end"],
          str(log))
    g._live_process = False

    # streams: preempted before the first chunk, never after it
    async def chunks(n, first_delay, log):
        log.append("start")
        await asyncio.sleep(first_delay)
        for i in range(n):
            yield i
            await asyncio.sleep(0.1)

    log = []
    loop.call_later(0.1, lambda: flag.append(live_on()))
    loop.call_later(0.4, lambda: flag[-1].unlink())
    got = [x async for x in g.stream(lambda: chunks(3, 0.3, log))]
    check("low stream restarts when a live text beats its first token", got == [0, 1, 2] and log == ["start", "start"],
          f"{got} {log}")
    log = []
    loop.call_later(0.15, lambda: flag.append(live_on()))
    loop.call_later(0.5, lambda: flag[-1].unlink())
    got = [x async for x in g.stream(lambda: chunks(4, 0.05, log))]
    check("low stream that already has tokens finishes (no lost text)", got == [0, 1, 2, 3] and log == ["start"],
          f"{got} {log}")

    # PICKY_GATE=0: the old rule (a non-side low process doesn't wait; no preemption)
    g.ENABLED = False
    f = live_on()
    out, dt = await timed(g.call(lambda: fake("old")))
    check("PICKY_GATE=0 restores the old behaviour", out == "old" and dt < 0.3, f"{dt:.2f}s")
    os.environ["PICKY_SIDE"] = "1"
    loop.call_later(0.4, f.unlink)
    out, dt = await timed(g.call(lambda: fake("old-side")))
    check("PICKY_GATE=0: a side process still waits before the call (live_flag)", dt >= 0.35, f"{dt:.2f}s")
    os.environ.pop("PICKY_SIDE")
    g.ENABLED = True

    # sync slot (batch scripts)
    f = live_on()
    loop.call_later(0.3, f.unlink)
    t0 = time.monotonic()

    def batch():
        with g.slot_sync():
            return g.status()["low_slot_free"]
    held_inside = await asyncio.to_thread(batch)
    check("slot_sync waits for a live text and holds the slot", time.monotonic() - t0 >= 0.25 and held_inside is False)
    check("status() shows the slot free afterwards", g.status()["low_slot_free"] is True)


asyncio.run(main())
print("\nall green" if not FAILS else "\nFAILED: " + ", ".join(FAILS))
sys.exit(1 if FAILS else 0)
