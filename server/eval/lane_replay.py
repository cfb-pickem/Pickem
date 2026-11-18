"""
Lane replay: every historical full-brain question (a private list built from picky_exchanges.jsonl) asked
once through a side sidecar, recording which lane answered and how long it took. Shows what share of the
old slow questions now take a code or banter lane. Member text stays in the private dir.

  lane_replay.py build --out <private json>              distinct questions the full lane answered
  lane_replay.py run --qs <private json> --url http://127.0.0.1:<port>/group --hist <side hist file> --out <json>
  lane_replay.py compare <before.json> <after.json>

build    rows with lane "full" plus older rows (no lane logged) that router.classify sends to "full", deduped
         by text; image asks are dropped (they take the picture path, never a text lane).
run      DM as the member who asked (else a mid-table member), history reset per ask, X-Picky-Test set.
         Call it inside the exam lock with a side sidecar (eval/genius/side.sh).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import statistics
import sys
import time
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
from live_probe import keychain  # noqa: E402
from run import group_handles  # noqa: E402

PRIVATE = Path(os.environ.get("PICKY_PRIVATE_DIR", "/Users/Agent/cfb-pickem-private"))
EXCHANGES = PRIVATE / "picky_exchanges.jsonl"
IMAGE_ASK = re.compile(r"\b(generate|draw|make|create)\b[^.?!]{0,20}\b(image|picture|pic|drawing)\b", re.I)


def build(out: str) -> None:
    import router
    seen: dict[str, dict] = {}
    for line in EXCHANGES.read_text().splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        q = (r.get("q") or "").strip()
        lane = r.get("lane") or ("full" if router.classify(q).lane == "full" and not r.get("lane") else None)
        if lane != "full" or not q or IMAGE_ASK.search(q):
            continue
        seen.setdefault(re.sub(r"\s+", " ", q.lower().replace("’", "'")), {"q": q, "team": r.get("team"),
                                                                             "secs": r.get("secs")})
    Path(out).write_text(json.dumps(list(seen.values()), indent=1))
    os.chmod(out, 0o600)
    print(f"{len(seen)} distinct full-lane questions -> {out}")


async def run(qs_file: str, url: str, hist: str, out: str, default_team: str) -> None:
    qs = json.loads(Path(qs_file).read_text())
    handles = group_handles()
    secret = os.environ.get("PICKY_GROUP_SECRET") or keychain("CFB_PICKEM__PICKY_GROUP_SECRET")
    token = os.environ.get("PICKY_TEST_TOKEN") or keychain("CFB_PICKEM__TEST_TOKEN")
    rows = []
    async with httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=5.0)) as c:
        for i, it in enumerate(qs):
            team = it.get("team") if it.get("team") in handles else default_team
            if hist:
                Path(hist).write_text("{}")
            t0 = time.time()
            r = await c.post(url, headers={"X-Picky-Group-Secret": secret, "X-Picky-Test": token},
                             json={"handle": handles[team], "text": it["q"], "chat": "", "dm": True})
            body = r.json() if r.status_code == 200 else {}
            secs = round(time.time() - t0, 1)
            rows.append({"q": it["q"], "team": team, "lane": body.get("lane") or "?", "secs": secs,
                         "reply": body.get("reply") or f"[HTTP {r.status_code}]"})
            print(f"{i:2d} {rows[-1]['lane']:18s} {secs:6.1f}s", flush=True)
    Path(out).write_text(json.dumps(rows, indent=1))
    os.chmod(out, 0o600)


def fast(lane: str) -> bool:
    return lane.split(":")[0] in ("fact", "banter", "jailbreak", "forget", "flag", "ignore")


def compare(a: str, b: str) -> None:
    A = {r["q"]: r for r in json.loads(Path(a).read_text())}
    B = {r["q"]: r for r in json.loads(Path(b).read_text())}
    qs = [q for q in A if q in B]
    for name, D in (("before", A), ("after", B)):
        s = [D[q]["secs"] for q in qs]
        n_fast = sum(fast(D[q]["lane"]) for q in qs)
        print(f"{name:6s} fast lane {n_fast}/{len(qs)} ({100 * n_fast / len(qs):.0f}%)  median {statistics.median(s):.1f}s"
              f"  p90 {sorted(s)[int(0.9 * (len(s) - 1))]:.1f}s  max {max(s):.1f}s")
    moved = [q for q in qs if not fast(A[q]["lane"]) and fast(B[q]["lane"])]
    print(f"moved to a fast lane by this change: {len(moved)}")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build"); b.add_argument("--out", required=True)
    r = sub.add_parser("run"); r.add_argument("--qs", required=True); r.add_argument("--url", required=True)
    r.add_argument("--hist", default=""); r.add_argument("--out", required=True)
    r.add_argument("--team", default="Wedge Mulshine")
    c = sub.add_parser("compare"); c.add_argument("a"); c.add_argument("b")
    x = ap.parse_args()
    if x.cmd == "build":
        build(x.out)
    elif x.cmd == "run":
        asyncio.run(run(x.qs, x.url, x.hist, x.out, x.team))
    else:
        compare(x.a, x.b)


if __name__ == "__main__":
    main()
