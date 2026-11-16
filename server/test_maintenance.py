"""
Picky's maintenance replies: flag/Ollama detection, no repeats, per-chat rate
limit, tailored lines, and the /group endpoint short-circuit (no model call).

Run:  ./.venv/bin/python3 -I test_maintenance.py
"""

from __future__ import annotations

import asyncio
import json
import random
import re
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import maintenance as mt  # noqa: E402

fails = 0


def check(cond, msg):
    global fails
    print(("ok   " if cond else "FAIL ") + msg)
    fails += not cond


def reset():
    mt._recent.clear()
    mt._last_by_chat.clear()
    mt._probe = (0.0, True)


tmp = Path(tempfile.mkdtemp())
lines = {"general": [f"General line {chr(65 + i)}, back soon." for i in range(10)],
         "Ackbar": ["Ackbar line one, chalk.", "Ackbar line two, chalk."]}
(tmp / "lines.json").write_text(json.dumps(lines))
mt.LINES_FILE, mt.FLAG = tmp / "lines.json", tmp / "PICKY_MAINTENANCE"

# detection
reset()
with patch.object(mt, "ollama_up", return_value=True):
    check(not mt.active(), "off when no flag and Ollama is up")
    mt.FLAG.write_text("")
    check(mt.active(), "on when the flag file exists")
    mt.FLAG.unlink()
with patch.object(mt, "ollama_up", return_value=False):
    check(mt.active(), "on when Ollama is unreachable")
reset()
with patch.object(mt.httpx, "get", side_effect=mt.httpx.ConnectError("down")):
    check(mt.ollama_up(now=1000.0) is False, "connection error counts as down")
check(mt._probe == (1000.0, False), "probe result is cached")

# no repeats in the last 5, rate limit per chat
reset()
rng, sent = random.Random(3), []
for i in range(40):
    sent.append(mt.reply(f"chat{i}", None, now=float(i), rng=rng))
check(all(s for s in sent), "every new chat gets a line")
check(all(sent[i] not in sent[max(0, i - 5):i] for i in range(len(sent))), "no line repeats within the last 5")
check(mt.reply("chat0", None, now=100.0, rng=rng) is None, "same chat inside 10 min gets silence")
check(mt.reply("chat0", None, now=700.0, rng=rng) is not None, "same chat after 10 min gets a line")

# tailored lines show up for the matching team only
reset()
rng = random.Random(1)
got = [mt.reply(f"a{i}", "Ackbar", now=float(i * 1000), rng=rng) for i in range(30)]
check(any("Ackbar" in g for g in got), "Ackbar sometimes gets his tailored lines")
check(any("General" in g for g in got), "Ackbar still gets general lines too")
reset()
got = [mt.reply(f"b{i}", "TG", now=float(i * 1000), rng=rng) for i in range(30)]
check(not any("Ackbar" in g for g in got), "other members never get Ackbar's lines")

# a missing bank falls back to built-in lines
reset()
with patch.object(mt, "LINES_FILE", tmp / "nope.json"):
    check(mt.reply("x", None, now=1.0) in mt.FALLBACK, "missing bank uses the fallback lines")

# the real private bank: no digits or banned bits
real = Path("/Users/Agent/cfb-pickem-private/picky_maintenance_lines.json")
if real.exists():
    allx = [s for v in json.loads(real.read_text()).values() for s in v]
    check(len(allx) >= 25, f"real bank has {len(allx)} lines")
    check(not [s for s in allx if re.search(r"\d|milk|dairy|carton|jeep", s, re.I)], "real bank: no numbers, no milk")

# /group short-circuits before the model
try:
    import group_chat as gc
except Exception as e:  # noqa: BLE001
    print("skip /group check:", e)
else:
    reset()
    mt.FLAG.write_text("")

    class Req:
        def __init__(self, body):
            self.headers = {"x-picky-group-secret": "s"}
            self._b = body

        async def json(self):
            return self._b

    async def boom(*a, **k):
        raise AssertionError("model path called during maintenance")

    with patch.object(gc, "SECRET", "s"), patch.object(gc, "answer", boom), \
            patch.object(gc, "load_members", return_value={}), \
            patch.object(gc.group_outbox, "config", return_value={"group_guid": "g1"}):
        r1 = asyncio.run(gc.group(Req({"handle": "+15555550100", "text": "@Picky who's first", "chat": "g1"})))
        r2 = asyncio.run(gc.group(Req({"handle": "+15555550100", "text": "@Picky hello??", "chat": "g1"})))
        r3 = asyncio.run(gc.group(Req({"handle": "+15555550100", "text": "no trigger here", "chat": "g2"})))
        r4 = asyncio.run(gc.group(Req({"handle": "+15555550101", "text": "yo", "chat": "d1", "dm": True})))
        r5 = asyncio.run(gc.group(Req({"handle": "+15555550102", "text": "@Picky hi", "chat": "other-group"})))
    check(bool(r1.get("reply")), "group @Picky gets a canned line, no model call")
    check(r2.get("reply") is None, "second ask in the same chat within 10 min is silent")
    check(r3.get("reply") is None, "group text without @Picky still gets nothing")
    check(r4.get("reply") is None, "DM from an unmapped, non-allowlisted sender still gets nothing")
    check(r5.get("reply") is None, "@Picky in a group that isn't the league group gets nothing")
    mt.FLAG.unlink()

print("\nall passed" if not fails else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)
