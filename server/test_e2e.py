"""
End-to-end probes for Picky: real questions through the real POST /chat.

Two transports:
  --remote   the live endpoint the site calls (pickem-chat.dunecreek.ai),
             signed in as Charlie's own account via a Supabase admin magic
             link (service key from the keychain; no email is sent).
  default    the same FastAPI app in-process, auth stubbed to the named team
             and reads done with the service key. Use this for other
             personas (e.g. last place) without touching their accounts.

    ./.venv/bin/python3 -I test_e2e.py [--remote] [--as "Chuck P"] [--only 3,5]

Prints every answer for a human to grade. Each probe is a conversation:
earlier turns are replayed so follow-ups ("change any of those") have
context, exactly like the browser's in-memory history.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import time

import httpx

sys.path.insert(0, ".")
import chat  # noqa: E402

REMOTE = "https://pickem-chat.dunecreek.ai/chat"
CHARLIE_UID = "35e1c78c-0fbc-4d9e-bc8d-f7df5ea73908"  # team "Chuck P"

# (persona, [user turns]); persona None = whoever --as names
PROBES = [
    (None, ["What are my picks this week?", "Should I change any of those"]),
    (None, ["Who did TG pick this week. Any injuries I need to know about"]),
    (None, ["Explain the Oregon State Boise State spread to me like I'm dumb"]),
    (None, ["Which pick is the best value this week?"]),
    (None, ["Who's winning the pool?"]),
    (None, ["How did everyone do last week?"]),
    (None, ["Roast my picks"]),
    ("TG", ["I'm in last place. Be honest, how screwed am I?"]),
    (None, ["Roast TG"]),
    (None, ["Is Wake Forest's quarterback healthy?"]),
    (None, ["Who do you like in Ohio State vs Michigan?"]),
    (None, ["Can you help me do my taxes?"]),
    (None, ["Who has Hawai'i this week?"]),
]


def _service_key() -> str:
    return subprocess.check_output(
        ["security", "find-generic-password", "-a", "drake",
         "-s", "CFB_PICKEM__SUPABASE_SERVICE_KEY", "-w"], text=True).strip()


def charlie_jwt() -> str:
    """A real session for Charlie's account: admin magic link -> verify."""
    key = _service_key()
    h = {"apikey": key, "Authorization": f"Bearer {key}"}
    email = httpx.get(f"{chat.AUTH}/admin/users/{CHARLIE_UID}", headers=h).json()["email"]
    link = httpx.post(f"{chat.AUTH}/admin/generate_link", headers=h,
                      json={"type": "magiclink", "email": email}).json()
    tok = (link.get("properties") or link)["hashed_token"]
    s = httpx.post(f"{chat.AUTH}/verify", headers={"apikey": chat.SUPABASE_ANON},
                   json={"type": "magiclink", "token_hash": tok}).json()
    return s["access_token"]


def read_sse(lines) -> str:
    out = []
    for line in lines:
        if not line.startswith("data:"):
            continue
        p = line[5:].strip()
        if p == "[DONE]":
            break
        obj = json.loads(p)
        if obj.get("error"):
            out.append(f"[ERROR {obj['error']}]")
        out.append(obj.get("content", ""))
    return "".join(out)


async def run(args):
    key = _service_key()
    uid_by_team = {}
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{chat.REST}/teams?select=team_name,user_id",
                        headers={"apikey": key, "Authorization": f"Bearer {key}"})
        uid_by_team = {t["team_name"]: t["user_id"] for t in r.json()}

    if args.remote:
        jwt = charlie_jwt()
        client = httpx.AsyncClient(timeout=180)
        url = REMOTE
    else:
        chat._auth_headers = lambda _jwt: {"apikey": key, "Authorization": f"Bearer {key}"}
        current = {"uid": uid_by_team[args.as_]}

        async def fake_verify(_client, _jwt):
            return current["uid"]
        chat._verify_jwt = fake_verify
        chat.RATE_LIMIT_PER_MIN = 10_000
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=chat.app),
                                   base_url="http://picky", timeout=180)
        url = "/chat"
        jwt = "stub"

    only = {int(x) for x in args.only.split(",")} if args.only else None
    for i, (persona, turns) in enumerate(PROBES, 1):
        if only and i not in only:
            continue
        who = persona or args.as_
        if args.remote and who != "Chuck P":
            print(f"\n#{i} [{who}] skipped: --remote only signs in as Charlie")
            continue
        if not args.remote:
            current["uid"] = uid_by_team[who]
        history = []
        for q in turns:
            history.append({"role": "user", "content": q})
            t0 = time.time()
            async with client.stream("POST", url, json={
                "messages": history, "supabase_jwt": jwt, "mode": args.mode,
            }) as r:
                if r.status_code != 200:
                    ans = f"[HTTP {r.status_code}] {(await r.aread())[:200]!r}"
                else:
                    lines = [ln async for ln in r.aiter_lines()]
                    ans = read_sse(lines)
            history.append({"role": "assistant", "content": ans})
            print(f"\n#{i} [{who}] Q: {q}  ({time.time() - t0:.1f}s)\nA: {ans}", flush=True)
            if args.remote:
                await asyncio.sleep(6.5)  # stay under 10 req/min
    await client.aclose()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--remote", action="store_true")
    ap.add_argument("--as", dest="as_", default="Chuck P")
    ap.add_argument("--mode", default="leaderboard", choices=["leaderboard", "picks"])
    ap.add_argument("--only", default="")
    asyncio.run(run(ap.parse_args()))
