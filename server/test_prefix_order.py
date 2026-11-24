"""
Prompt cache order (2026-11-24): prompt_prefix puts the league data that changes rarely (week, standings, last
week, Picky's own block) ahead of this week's games, and _prime leaves a checkpoint at the end of that stable
part, so a data refresh (market line, headline, weather) re-reads only the games, not the whole ~8k-token prefix.

Run:  ./.venv/bin/python3 -I test_prefix_order.py

Offline: chat.OLLAMA points at a closed port; the prime test uses a fake client.
"""

from __future__ import annotations

import asyncio
import collections
import sys
from unittest.mock import patch

sys.path.insert(0, ".")
import chat  # noqa: E402
import test_leader  # noqa: E402

chat.OLLAMA = "http://127.0.0.1:9"  # closed port
FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("ok   " if ok else "FAIL ") + name + (f"  ({detail})" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


c1 = test_leader.ctx()
c2 = test_leader.ctx(games=(test_leader.G1, dict(test_leader.G2, line=-14.5), test_leader.TB))  # a line moved
p1, p2 = chat.prompt_prefix(c1), chat.prompt_prefix(c2)
s1, s2 = chat.STABLE_PART.get(p1), chat.STABLE_PART.get(p2)


def test_stable_part_survives_a_refresh():
    check("a games change makes a new prefix", p1 != p2)
    check("the stable part is recorded and starts the prefix", bool(s1) and p1.startswith(s1) and p2.startswith(s2))
    check("the stable part is byte-identical across the refresh", s1 == s2)
    check("the stable part holds standings, last week and the instructions",
          "STANDINGS (" in s1 and "LAST WEEK (week 10) FINALS" in s1 and "GRADED ONLY AGAINST THE SPREAD" in s1)
    check("this week's games come after it", "YOUR GAMES THIS WEEK (3)" not in s1 and "Game 1:" not in s1
          and p1.index("YOUR GAMES THIS WEEK (3)") > p1.index("LAST WEEK (week 10) FINALS"))
    check("the prefix still ends the league data", p1.endswith("\n(End of league data.)"))


def test_same_lines_only_moved():
    data = p1.split("\nLEAGUE DATA:\n", 1)[1].rsplit("\n(End of league data.)", 1)[0]
    want = chat.render_context(c1) + chat.picky_player_block(c1)
    check("the league data has the same lines as before, only reordered",
          collections.Counter(data.split("\n")) == collections.Counter(want.split("\n")))
    err = {"error": "offline"}
    check("an error context keeps the old layout", chat.prompt_prefix(err).endswith(
        "LEAGUE DATA UNAVAILABLE: offline\n(End of league data.)"))


def test_drafts_share_the_primed_prefix():
    check("a draft's system prompt starts with exactly the primed prefix", chat._system_prompt(c1).startswith(p1))


async def test_prime_checkpoints_the_stable_part():
    sent = []

    class FakeClient:
        async def post(self, url, json=None, timeout=None):
            sent.append(json["messages"][0]["content"])

            class R:
                status_code = 200

                @staticmethod
                def json():
                    return {"prompt_eval_duration": 1}
            return R()

    fc = FakeClient()
    with patch.object(chat, "MODEL", "gemma4:12b-it-qat"), patch.dict(chat._PRIMED, {"prefix": None}):
        await chat._prime(fc, p1)
        check("a new prefix: the stable part first, then the prefix", sent == [s1, p1], str([len(x) for x in sent]))
        sent.clear()
        await chat._prime(fc, p1)
        check("the same prefix again: one prime, no extra call", sent == [p1])
        sent.clear()
        await chat._prime(fc, p2)
        check("refreshed data: the stable part again (cheap, it's cached), then the new prefix", sent == [s2, p2])
        sent.clear()
        await chat._prime(fc, "some other prefix")
        check("a prefix prompt_prefix didn't build: one prime as before", sent == ["some other prefix"])


if __name__ == "__main__":
    test_stable_part_survives_a_refresh()
    test_same_lines_only_moved()
    test_drafts_share_the_primed_prefix()
    asyncio.run(test_prime_checkpoints_the_stable_part())
    print(f"\n{'FAILED: ' + ', '.join(FAILS) if FAILS else 'all passed'}")
    sys.exit(1 if FAILS else 0)
