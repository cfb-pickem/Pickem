"""
Ask Picky ad-hoc questions as a real member, at test priority.

    cd server
    ./.venv/bin/python3 -I eval/ask.py --as me "how did my Oregon State pick do?"
    ./.venv/bin/python3 -I eval/ask.py --as last --url http://127.0.0.1:8777/chat "q1" "follow-up"
    ./.venv/bin/python3 -I eval/ask.py --as "TG" --each "q1" "q2"     # separate conversations

--as: me (Chuck P), last, df, mid, or a team name. Turns form one
conversation unless --each. Prints each answer with first-token time.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import run  # noqa: E402  (loads the plist env, the test token, mint_jwt, ask)
import questions  # noqa: E402
from truth import Truth, service_key  # noqa: E402

CACHE = HERE / "results" / ".ask_jwts.json"


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--as", dest="who", default="me")
    ap.add_argument("--url", default=run.LOCAL)
    ap.add_argument("--mode", default="leaderboard")
    ap.add_argument("--each", action="store_true", help="each question is its own conversation")
    ap.add_argument("--json", default="", help="also append results to this JSONL file")
    ap.add_argument("turns", nargs="+")
    a = ap.parse_args()

    t = Truth()
    await t.load()
    P = questions.personas(t)
    team = P.get(a.who, a.who)
    uid = t.uid_by_team.get(team)
    if not uid:
        sys.exit(f"no member {team!r}; members: {', '.join(t.uid_by_team)}")
    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    jwt, exp = cache.get(uid, ["", 0])
    if time.time() > exp - 300:
        jwt = run.mint_jwt(service_key(), uid)
        cache[uid] = [jwt, time.time() + 3000]
        CACHE.parent.mkdir(exist_ok=True)
        CACHE.write_text(json.dumps(cache))

    headers = {"X-Picky-Test": run.test_token()}
    async with httpx.AsyncClient(timeout=httpx.Timeout(300, connect=20), headers=headers) as client:
        history: list[dict] = []
        for q in a.turns:
            if a.each:
                history = []
            history.append({"role": "user", "content": q})
            ans, ttft, total, _img, gate = await run.ask(client, a.url, jwt, history, a.mode)
            history.append({"role": "assistant", "content": ans})
            print(f"\n[{team}] Q: {q}\n  ({ttft:.1f}s first token, {total:.1f}s total"
                  f"{', preempted %d' % gate['preemptions'] if gate['preemptions'] else ''})\nA: {ans}", flush=True)
            if a.json:
                with open(a.json, "a") as f:
                    f.write(json.dumps({"who": team, "q": q, "a": ans, "ttft": ttft, "total": total}) + "\n")


if __name__ == "__main__":
    asyncio.run(main())
