"""
"bad picky": flag_flub.py (CLI + log_flub) writes the private flub queue (0600, image copied, recent
live exchanges as context), and group_chat's sidecar hook logs a mapped member's "that's wrong" with
the previous Picky exchange in that chat and sends no reply spam. Temp paths only (PICKY_FLUB_QUEUE).

Run:  ./.venv/bin/python3 -I test_flag_flub.py
"""
from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import stat
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
TMP = Path(tempfile.mkdtemp())
QUEUE = TMP / "flubs" / "queue.jsonl"
EX = TMP / "exchanges.jsonl"
os.environ.update({"PICKY_FLUB_QUEUE": str(QUEUE), "PICKY_EXCHANGE_LOG": str(EX)})
sys.path.insert(0, str(HERE))
import flag_flub  # noqa: E402

fails = []


def check(cond, msg):
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond:
        fails.append(msg)


def queue() -> list[dict]:
    return [json.loads(x) for x in QUEUE.read_text().splitlines()] if QUEUE.exists() else []


# ---- is_flag ------------------------------------------------------------------------------------
for t in ["bad picky", "Bad Picky!", "@Picky that's wrong", "that’s wrong", "thats wrong", "picky that is wrong",
          "that's not right", "bad bot lol", "nope that's wrong", "that's incorrect picky", "wrong answer", "flub"]:
    check(flag_flub.is_flag(t), f"flag: {t!r}")
for t in ["that's wrong, Clam is first", "who's wrong this week?", "am I on the wrong side?", "bad week for picky",
          "you suck", "picky", "what's wrong with you", "bad beat"]:
    check(not flag_flub.is_flag(t), f"not a flag: {t!r}")

# ---- log_flub + CLI ---------------------------------------------------------------------------------
now = time.time()
rows = [{"ts": now - 3 * 3600, "mode": "dm", "team": "Alpha", "q": "too old", "a": "x"},
        {"ts": now - 600, "mode": "dm", "team": None, "q": "test handle", "a": "x"},
        {"ts": now - 500, "mode": "group", "team": "Beta", "q": "side", "a": "x", "side": 1}]
rows += [{"ts": now - 400 + i, "mode": "dm", "team": "Alpha", "q": f"q{i}", "a": f"a{i}", "chat": "aaaaaaaaaa"}
         for i in range(7)]
rows.append({"ts": now - 100, "mode": "group", "team": "Beta", "q": "group q", "a": "group a", "chat": "bbbbbbbbbb"})
EX.write_text("".join(json.dumps(r) + "\n" for r in rows))
img = TMP / "shot.PNG"
img.write_bytes(b"\x89PNG fake")
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    rc = flag_flub.main(["--text", "Iowa -6.5, the only value side", "--image", str(img), "--note", "line is +8.5"])
line = buf.getvalue().strip()
q = queue()
check(rc == 0 and line.startswith("logged flub q-") and line.split()[-1] == q[0]["id"], f"prints one line: {line}")
r = q[0]
check(r["priority"] == "high" and r["source"] == "coordinator" and r["consumed"] is None, "high priority, unconsumed")
check(r["text"] == "Iowa -6.5, the only value side" and r["note"] == "line is +8.5", "text + note kept")
check(len(r["context"]) == 6 and r["context"][-1]["q"] == "group q", "context: the last 6 live exchanges")
check(not any(c["q"] in ("too old", "test handle", "side") for c in r["context"]),
      "context skips >2h old, test handles and side rows")
check(Path(r["image"]).exists() and Path(r["image"]).parent == QUEUE.parent / "img" and r["image"].endswith(".png"),
      "image copied into flubs/img")
check(stat.S_IMODE(os.stat(r["image"]).st_mode) == 0o600 and stat.S_IMODE(os.stat(QUEUE).st_mode) == 0o600,
      "queue and image are 0600")
flag_flub.log_flub("", source="dm", chat="aaaaaaaaaa")
check([c["q"] for c in queue()[-1]["context"]] == [f"q{i}" for i in range(1, 7)], "--chat narrows the context")
check(queue()[-1]["text"] == "", "empty --text is fine")
with contextlib.redirect_stderr(io.StringIO()):
    check(flag_flub.main(["--image", str(TMP / "nope.png")]) == 1, "a missing image fails cleanly")
check(flag_flub.chat_hash("dm:+15555550100") == flag_flub.chat_hash("dm:+15555550100") and
      "5555" not in flag_flub.chat_hash("dm:+15555550100"), "chat hash is stable and holds no handle")

# ---- the sidecar hook ------------------------------------------------------------------------------
import group_chat as gc  # noqa: E402

gc.EXCHANGE_LOG = TMP / "gc-exchanges.jsonl"
hist = TMP / "hist.json"
now = time.time()
hist.write_text(json.dumps({
    "group:g1": [{"role": "user", "content": "who's first", "ts": now - 60, "sender": "+15555550101"},
                 {"role": "assistant", "content": "Beta, by two.", "ts": now - 60, "sender": "+15555550101"}],
    "dm:+15555550100": [{"role": "user", "content": "my record?", "ts": now - 30, "sender": None},
                        {"role": "assistant", "content": "9-1, sure.", "ts": now - 30, "sender": None}]}))
members = {"+15555550100": {"team": "Alpha", "name": "A"}, "+15555550101": {"team": "Beta", "name": "B"}}


class Req:
    def __init__(self, body):
        self.headers = {"x-picky-group-secret": "s"}
        self._b = body

    async def json(self):
        return self._b


async def boom(*a, **k):
    raise AssertionError("model path called for a flag")

QUEUE.unlink()
with patch.object(gc, "SECRET", "s"), patch.object(gc, "answer", boom), patch.object(gc, "HISTORY_FILE", hist), \
        patch.object(gc, "load_members", return_value=members), \
        patch.object(gc.group_outbox, "config", return_value={"group_guid": "g1"}):
    r1 = asyncio.run(gc.group(Req({"handle": "+15555550100", "text": "@Picky that's wrong", "chat": "g1"})))
    r2 = asyncio.run(gc.group(Req({"handle": "+15555550100", "text": "bad picky", "chat": "x", "dm": True})))
q = queue()
check(r1.get("reply") is None, "group flag: no reply")
check(r2.get("reply") == gc.FLAG_ACK and len(r2["reply"]) <= 10, "DM flag: one-word ack at most")
check(len(q) == 2 and [x["source"] for x in q] == ["group", "dm"], "both flags queued with their source")
check(q[0]["context"] == [{"q": "who's first", "a": "Beta, by two."}], "group: the previous Picky exchange in that chat")
check(q[1]["context"] == [{"q": "my record?", "a": "9-1, sure."}], "DM: the previous exchange in that DM")
check(q[0]["chat"] == flag_flub.chat_hash("group:g1") and "5555" not in json.dumps(q), "chat hashed, no handles stored")

QUEUE.unlink()
called = []


async def fake_answer(*a, **k):
    called.append(a)
    return {"reply": "sure", "image": None, "mode": "group"}

with patch.object(gc, "SECRET", "s"), patch.object(gc, "answer", fake_answer), patch.object(gc, "HISTORY_FILE", hist), \
        patch.object(gc, "load_members", return_value=members), \
        patch.object(gc.callbacks, "hook", lambda *a, **k: None), \
        patch.object(gc.VIEWER, "sender", lambda h: gc.Sender(handle=h)), \
        patch.object(gc.group_outbox, "config", return_value={"group_guid": "g1"}):
    r3 = asyncio.run(gc.group(Req({"handle": "+15555550199", "text": "@Picky that's wrong", "chat": "g1"})))
    r4 = asyncio.run(gc.group(Req({"handle": "+15555550100", "text": "@Picky that's wrong, Beta is 2nd", "chat": "g1"})))
check(not QUEUE.exists(), "unmapped sender / a longer correction: nothing queued")
check(len(called) == 2 and r4.get("reply") == "sure", "...and those go through the normal pipeline")

# ---- the exchange log's new fields ------------------------------------------------------------------
tok = gc.REQ_T0.set(1000.0)
with patch.object(gc, "load_members", return_value=members):
    gc.log_exchange("group:g1", "q", "a", "+15555550101", 1012.34, "full")
    gc.log_exchange("group:exam-t-x", "q", "a", "+15555550101", 1013.0)
gc.REQ_T0.reset(tok)
rows = [json.loads(x) for x in gc.EXCHANGE_LOG.read_text().splitlines()]
check(rows[0]["chat"] == flag_flub.chat_hash("group:g1") and rows[0]["secs"] == 12.3 and "side" not in rows[0],
      "exchange row: chat hash + secs")
check(rows[1].get("side") == 1, "exam chats are marked side")
check(stat.S_IMODE(os.stat(gc.EXCHANGE_LOG).st_mode) == 0o600 and "5555" not in gc.EXCHANGE_LOG.read_text(),
      "exchange log stays 0600, handle-free")

print(f"\n{'all green' if not fails else 'FAILED: ' + str(len(fails))}")
sys.exit(1 if fails else 0)
