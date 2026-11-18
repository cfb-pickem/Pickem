"""Canary + test-traffic mode (review R5), model-free."""
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp())
os.environ["PICKY_IMESSAGE_HISTORY"] = str(TMP / "hist.json")
os.environ["PICKY_EXCHANGE_LOG"] = str(TMP / "ex.jsonl")
os.environ.setdefault("PICKY_NO_ANTHROPIC", "1")
sys.path.insert(0, str(Path(__file__).resolve().parent))

import canary  # noqa: E402
import group_chat as gc  # noqa: E402
import picky_brain as pb  # noqa: E402


def test_check():
    assert canary.check("Buy-in is $75 a player.") and not canary.check("") and not canary.check("Art department's closed")


def test_test_requests_save_nothing():
    tok = gc.TEST_REQ.set(True)
    try:
        asyncio.run(gc.save_turn("dm:canary", "q", "a", "canary", "fact"))
    finally:
        gc.TEST_REQ.reset(tok)
    assert not (TMP / "hist.json").exists() and not (TMP / "ex.jsonl").exists()
    asyncio.run(gc.save_turn("dm:canary", "q", "a", "canary", "fact"))
    assert (TMP / "hist.json").exists()


def test_code_only_never_calls_the_model():
    tok = pb.CODE_ONLY.set(True)
    try:
        asyncio.run(pb._json_call("P", "T", "U", {}, 0.1, 10, 1.0, prime_wait=pb.PRIME_WAIT))
        raised = False
    except pb.PrimeSlow:
        raised = True
    finally:
        pb.CODE_ONLY.reset(tok)
    assert raised


def test_canary_logs_and_alerts(monkeypatch=None):
    log = TMP / "canary.jsonl"
    canary.WATCHDOG_LOG = TMP / "watchdog.log"
    real = canary.ask
    canary.ask = lambda url, model, timeout: (_ for _ in ()).throw(ConnectionError("down"))
    try:
        canary.run(log=log)
        canary.run(log=log)
    finally:
        canary.ask = real
    rows = [json.loads(x) for x in log.read_text().splitlines()]
    assert [r["ok"] for r in rows] == [False, False]
    assert "ALERT canary failed twice" in canary.WATCHDOG_LOG.read_text()


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("ok  ", name)
            except AssertionError as e:
                fails += 1
                print("FAIL", name, e)
    print("ALL PASS" if not fails else f"{fails} FAILED")
    sys.exit(1 if fails else 0)
