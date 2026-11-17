"""
"What channel is LSU Tennessee on?" (Saturday drill, 2026-11-17): Picky said he had no idea. The live
poller parsed ESPN's scoreboard but dropped its broadcasts, so neither the chat nor close_cover's
scoreboard fallback ever had a network. live_games.parse_scoreboard now keeps them, and the router's
channel lane (channel.py) answers in code from the live cache: never a guess, None (full brain) when
ESPN doesn't list one or no game is named.

Run:  ./.venv/bin/python3 -I test_channel.py
"""
from __future__ import annotations

import gzip
import json
import sys
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import channel  # noqa: E402
import close_cover  # noqa: E402
import live_games as L  # noqa: E402
import router  # noqa: E402

fails = []


def check(cond, msg):
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond:
        fails.append(msg)


sb = json.load(gzip.open(HERE / "eval/fixtures/live-2026-11-14/scoreboard.json.gz"))
games = L.parse_scoreboard(sb)
with_tv = [g for g in games.values() if g.get("broadcasts")]
check(len(with_tv) >= len(games) // 2 and all(isinstance(n, str) and n for g in with_tv for n in g["broadcasts"]),
      f"scoreboard: broadcasts kept ({len(with_tv)} of {len(games)} games)")
g0 = with_tv[0]
check(close_cover.network(None, g0) == g0["broadcasts"][0], "close_cover's scoreboard fallback now gets the network")

cache = {"games": {"1": {"id": "1", "away": {"name": "LSU", "abbr": "LSU", "nick": "Tigers"},
                         "home": {"name": "Tennessee", "abbr": "TENN", "nick": "Volunteers"},
                         "broadcasts": ["ABC"], "state": "pre", "start": "2026-11-21T20:30Z"},
                   "2": {"id": "2", "away": {"name": "Texas Tech", "abbr": "TTU", "nick": "Red Raiders"},
                         "home": {"name": "Baylor", "abbr": "BAY", "nick": "Bears"}, "state": "pre"}}}
with patch.object(L, "load", lambda *a, **k: cache):
    a = channel.answer({}, "what channel is LSU Tennessee on")
    check(a is not None and "ABC" in a and "LSU" in a and "Tennessee" in a, f"named game with TV: {a!r}")
    check(channel.answer({}, "what channel is Texas Tech on") is None, "no network listed: None (never a guess)")
    check(channel.answer({}, "what channel is the game on") is None, "no game named: None")
    check(router.fact_answer("channel", {"games_this_week": []}, "where can I watch LSU Tennessee") == a,
          "router's channel lane uses it")

for q in ("what channel is LSU Tennessee on", "what network is Iowa Illinois on?", "where can I watch the LSU game",
          "what's the LSU game on", "is Texas Tech on ESPN or FOX?"):
    check(router.classify(q).label() == "fact:channel", f"{q!r} -> fact:channel (got {router.classify(q).label()})")
check(router.classify("who's on the ball in the LSU game").label() != "fact:channel", "not every 'on' is a channel ask")

print(f"\n{'all green' if not fails else 'FAILED: ' + str(len(fails))}")
sys.exit(1 if fails else 0)
