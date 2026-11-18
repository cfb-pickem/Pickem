"""
Live-priority latency probe: DM asks marked X-Picky-Test + X-Picky-Priority: live, sent to a sidecar
that treats them as a member's text (the live sidecar :8768, or a probe sidecar started with
PICKY_ROLE=live and its own state dir). Used by eval/gate_ab.sh to time live replies while an exam
runs, with and without the Ollama gate.

  live_probe.py --url http://127.0.0.1:8820/group --n 10 --gap 40 --out probes.json [--hist <file>]

The asks rotate fact / banter / full-brain texts (so every lane is timed). --hist resets the probe
sidecar's DM history before each ask, so no ask is read as a follow-up. Never run it against the live
sidecar without the X-Picky-Test token (it's added here from the keychain).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from run import MEMBERS_FILE, group_handles  # noqa: E402

PROBES = [  # (lane we expect, text): lanes that stay put after the speed lanes
    ("fact", "whats my record"),
    ("banter", "you suck picky"),
    ("full", "who do you think wins the Big Ten this year and why"),
    ("fact", "who's in first?"),
    ("banter", "best beer?"),
    ("full", "what do you think of Marcus Freeman?"),
]


def keychain(service: str) -> str:
    r = subprocess.run(["security", "find-generic-password", "-a", "drake", "-s", service, "-w"],
                       capture_output=True, text=True)
    return r.stdout.strip()


async def probe(url: str, n: int, gap: float, team: str, hist: str, start_delay: float) -> list[dict]:
    secret = os.environ.get("PICKY_GROUP_SECRET") or keychain("CFB_PICKEM__PICKY_GROUP_SECRET")
    token = os.environ.get("PICKY_TEST_TOKEN") or keychain("CFB_PICKEM__TEST_TOKEN")
    handle = group_handles()[team]
    out = []
    await asyncio.sleep(start_delay)
    async with httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=5.0)) as c:
        for i in range(n):
            lane, text = PROBES[i % len(PROBES)]
            if hist:
                Path(hist).write_text("{}")
            t0 = time.time()
            try:
                r = await c.post(url, headers={"X-Picky-Group-Secret": secret, "X-Picky-Test": token,
                                               "X-Picky-Priority": "live"},
                                 json={"handle": handle, "text": text, "chat": "", "dm": True})
                body = r.json() if r.status_code == 200 else {}
                reply, got = body.get("reply") or f"[HTTP {r.status_code}]", body.get("lane")
            except httpx.HTTPError as e:
                reply, got = f"[{type(e).__name__}]", None
            secs = time.time() - t0
            out.append({"i": i, "lane": lane, "got_lane": got, "q": text, "secs": round(secs, 1),
                        "ts": round(t0), "reply": reply[:200]})
            print(f"probe {i:2d} {lane:6s} {got or '-':16s} {secs:6.1f}s  {text}", flush=True)
            await asyncio.sleep(max(0.0, gap - secs) if gap > secs else 5.0)
    return out


def summary(rows: list[dict]) -> dict:
    s = sorted(r["secs"] for r in rows)
    if not s:
        return {}
    p90 = s[min(len(s) - 1, int(round(0.9 * (len(s) - 1))))]
    by = {}
    for lane in ("fact", "banter", "full"):
        x = [r["secs"] for r in rows if r["lane"] == lane]
        if x:
            by[lane] = round(statistics.median(x), 1)
    return {"n": len(s), "median": round(statistics.median(s), 1), "p90": p90, "max": s[-1], "by_lane": by}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--n", type=int, default=10)
    ap.add_argument("--gap", type=float, default=40.0, help="seconds between ask starts")
    ap.add_argument("--team", default="Wedge Mulshine")
    ap.add_argument("--hist", default="")
    ap.add_argument("--delay", type=float, default=0.0, help="wait before the first ask (let the exam start)")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    assert MEMBERS_FILE.exists(), "private member map missing"
    rows = asyncio.run(probe(a.url, a.n, a.gap, a.team, a.hist, a.delay))
    res = {"summary": summary(rows), "rows": rows}
    Path(a.out).write_text(json.dumps(res, indent=1))
    print(json.dumps(res["summary"]))


if __name__ == "__main__":
    main()
