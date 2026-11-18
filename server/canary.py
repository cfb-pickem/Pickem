#!/usr/bin/env python3
"""Picky canary (review R5): one test ask through the live /group, checked.

  canary.py            # the 15-min LaunchAgent run: code lane only (code_only: no model call), logged
  canary.py --model    # release.sh: same ask, but the fact lane's gemma voice line runs too
  canary.py --url http://127.0.0.1:8820/group   # a side sidecar

Sends X-Picky-Test (keychain CFB_PICKEM__TEST_TOKEN), so the sidecar saves nothing: no history, no exchange
log, no live flag, no flub/callback hooks. Asks as an allowlisted DM from the fake handle "canary"; nothing
is ever sent to iMessage. Pass = a reply with "$75" (the buy-in, code-computed) inside the time limit.
Each run appends one row to cfb-pickem-private/picky_canary.jsonl; two failures in a row write an ALERT
line to the watchdog log (the watchdog's deep /health is what restarts things). Exit 0 = pass.
Stdlib only.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

PRIVATE = Path(os.environ.get("PICKY_PRIVATE_DIR", "/Users/Agent/cfb-pickem-private"))
LOG = PRIVATE / "picky_canary.jsonl"
WATCHDOG_LOG = Path.home() / "Library/Logs/picky-watchdog.log"
URL = "http://127.0.0.1:8768/group"
QUESTION = "what's the buy-in?"


def keychain(service: str) -> str:
    r = subprocess.run(["/usr/bin/security", "find-generic-password", "-a", "drake", "-s", service, "-w"],
                       capture_output=True, text=True, timeout=10)
    return r.stdout.strip()


def check(reply: str | None) -> bool:
    return bool(reply) and "75" in reply


def ask(url: str, model: bool, timeout: float) -> dict:
    body = {"handle": "canary", "text": QUESTION, "dm": True, "allowlisted": True, "code_only": not model}
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST", headers={
        "Content-Type": "application/json",
        "X-Picky-Group-Secret": os.environ.get("PICKY_GROUP_SECRET") or keychain("CFB_PICKEM__PICKY_GROUP_SECRET"),
        "X-Picky-Test": os.environ.get("PICKY_TEST_TOKEN") or keychain("CFB_PICKEM__TEST_TOKEN")})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode()) or {}


def run(url: str = URL, model: bool = False, timeout: float = 60.0, log: Path = LOG) -> dict:
    t0 = time.time()
    row = {"ts": round(t0), "mode": "model" if model else "code"}
    try:
        out = ask(url, model, timeout)
        row.update(ok=check(out.get("reply")), lane=out.get("lane"))
        if not row["ok"]:
            row["err"] = f"bad reply: {(out.get('reply') or '')[:80]!r}"
    except (urllib.error.URLError, TimeoutError, ConnectionError, ValueError) as e:
        row.update(ok=False, err=f"{type(e).__name__}: {e}"[:160])
    row["secs"] = round(time.time() - t0, 1)
    try:
        prev = [json.loads(x) for x in log.read_text().splitlines()[-1:]] if log.exists() else []
        with log.open("a") as f:
            f.write(json.dumps(row) + "\n")
        if not row["ok"] and prev and not prev[-1].get("ok") and not model:
            with WATCHDOG_LOG.open("a") as f:
                f.write(f"{dt.datetime.now():%Y-%m-%d %H:%M:%S} ALERT canary failed twice in a row: {row.get('err')}\n")
    except OSError:
        pass
    return row


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", action="store_true")
    ap.add_argument("--url", default=URL)
    ap.add_argument("--timeout", type=float, default=60.0)
    a = ap.parse_args(argv)
    row = run(a.url, a.model, a.timeout if not a.model else max(a.timeout, 150.0))
    print(json.dumps(row))
    return 0 if row["ok"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
