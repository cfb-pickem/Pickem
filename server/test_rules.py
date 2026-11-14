"""
League-rules tests for Picky.

Run:  ./.venv/bin/python3 -I test_rules.py          (offline, fast)
      ./.venv/bin/python3 -I test_rules.py --live   (also asks the local model)

Pins: league_rules.md tracks the "League Rules" block on cfb-genius.html
(hash + tiebreaker order), rules are injected only for rules questions and
never change the system prompt (Ollama prompt cache), and slurs are scrubbed
from the model's output even when split across stream chunks.
--live asks rules questions in-process as Chuck P (service-key reads, auth
stubbed) and checks the answers for the facts the rules state.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import os
import re
import sys
from pathlib import Path

# Same model settings as the com.pickem.chat plist; without them chat.py
# falls back to the old 7B and --live grades the wrong model.
os.environ.setdefault("PICKEM_CHAT_MODEL", "qwen3.5:9b-q4_K_M")
os.environ.setdefault("PICKY_OLLAMA_THINK", "false")
sys.path.insert(0, ".")
import chat  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
RULES = Path(__file__).with_name("league_rules.md").read_text()


def _site_block() -> str:
    s = (ROOT / "cfb-genius.html").read_text()
    return s[s.index("<!-- Rules -->"):s.index("<!-- Champion -->")]


def test_rules_file_matches_site():
    want = re.search(r"source-hash: ([0-9a-f]{16})", RULES).group(1)
    got = hashlib.sha256(_site_block().encode()).hexdigest()[:16]
    assert got == want, (f"cfb-genius.html rules changed ({got} != {want}): update "
                         "server/league_rules.md to match, then its source-hash")


def test_tiebreaker_order_matches_site():
    lists = re.findall(r"<ol[^>]*>(.*?)</ol>", _site_block(), re.S)
    assert len(lists) == 2, "expected regular + playoff tiebreaker lists"
    for site_ol, label in zip(lists, ("Regular season", "Playoff")):
        items = [html.unescape(re.sub(r"<[^>]+>", "", li)).strip()
                 for li in re.findall(r"<li>(.*?)</li>", site_ol, re.S)]
        after = RULES.split(f"{label} tiebreakers, in order:\n", 1)[1].splitlines()
        ours = []
        for ln in after:
            m = re.match(r"\d\) (.+)", ln)
            if not m:
                break
            ours.append(m.group(1))
        assert len(ours) == len(items), (label, ours, items)
        for a, b in zip(ours, items):
            key = b.split()[0].lower().replace("chips", "chip")
            assert key in a.lower(), (label, a, b)


def test_wants_rules():
    yes = ["How does scoring work?", "What are the tiebreakers?", "How does the jackpot work",
           "When's the deadline?", "What happens with a push?", "What does second place win?",
           "When do the lines lock?", "What if a game gets postponed?", "How do chips work"]
    no = ["Roast my picks", "Who's winning the pool?", "Explain the Utah Iowa spread",
          "Who did TG pick?", "Is Iowa's QB healthy?"]
    for q in yes:
        assert chat.wants_rules(q), q
    for q in no:
        assert not chat.wants_rules(q), q


def _ctx():
    return {"you": {"team_name": "Chuck P"}, "standings_ytd": [], "games_this_week": [
        {"id": 1, "away": "Utah", "home": "Iowa", "start": "2030-09-07T11:00:00"}],
        "season": 2030, "current_week": 2}


def test_rules_injected_only_when_asked():
    ctx = _ctx()
    t = chat._user_turn(ctx, "What are the tiebreakers?")
    assert "LEAGUE RULES" in t and "Coin flip".lower() in t.lower()
    assert "kicks off Sat 11:00 AM CT" in t and "lock then" in t
    assert t.endswith("What are the tiebreakers?")
    assert "LEAGUE RULES" not in chat._user_turn(ctx, "Roast my picks")
    assert "#" not in chat.LEAGUE_RULES.splitlines()[0]


def test_system_prompt_has_no_rules_text():
    """Rules ride in the user turn; the system prompt must not depend on the
    question, or the 6k-token prompt cache breaks."""
    ctx = _ctx()
    sp = chat._system_prompt(ctx)
    assert "LEAGUE RULES" in sp and "Coin flip" not in sp


def test_scrub_and_split_chunks():
    assert chat.scrub("you faggot") == "you dumbass"
    assert chat.scrub("Spicy take, Homer") == "Spicy take, Homer"

    async def fake():
        for piece in ["Not touching that, you f", "agg", "ot. TG is ", "18-26."]:
            yield chat._sse({"content": piece})
        yield "data: [DONE]\n\n"

    async def run():
        out = []
        async for c in chat._scrubbed(fake()):
            if c.startswith("data: {"):
                out.append(json.loads(c[6:])["content"])
        return "".join(out)

    got = asyncio.run(run())
    assert got == "Not touching that, you dumbass. TG is 18-26.", got


# --- live: real model, real data -------------------------------------------

LIVE = [
    ("How does scoring work in this league?", [r"\b(one|1) point", r"spread|cover"]),
    ("What are the tiebreakers?", [r"conference champ", r"army", r"coin flip"]),
    ("What happens if a game ends in a push?", [r"half"]),
    ("What does second place win?", [r"\$25|twenty.?five"]),
    ("When do the lines lock?", [r"tuesday", r"11"]),
    ("What happens if one of our games gets postponed?", [r"commissioner"]),
]


async def live():
    import subprocess
    import httpx
    key = subprocess.check_output(["security", "find-generic-password", "-a", "drake", "-s",
                                   "CFB_PICKEM__SUPABASE_SERVICE_KEY", "-w"], text=True).strip()
    chat._auth_headers = lambda _jwt: {"apikey": key, "Authorization": f"Bearer {key}"}

    async def fake_verify(_c, _j):
        return "35e1c78c-0fbc-4d9e-bc8d-f7df5ea73908"  # Chuck P
    chat._verify_jwt = fake_verify
    chat.RATE_LIMIT_PER_MIN = 10_000
    bad = 0
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=chat.app),
                                 base_url="http://picky", timeout=240) as c:
        for q, pats in LIVE:
            ans = []
            async with c.stream("POST", "/chat", json={"messages": [{"role": "user", "content": q}],
                                                       "supabase_jwt": "stub"}) as r:
                async for ln in r.aiter_lines():
                    if ln.startswith("data: {"):
                        ans.append(json.loads(ln[6:]).get("content", ""))
            a = "".join(ans)
            miss = [p for p in pats if not re.search(p, a, re.I)]
            bad += bool(miss)
            print(f"  {'ok ' if not miss else 'BAD'} {q}\n      {a}" + (f"\n      missing {miss}" if miss else ""))
    return bad


if __name__ == "__main__":
    fails = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ok  {name}")
            except AssertionError as e:
                fails += 1
                print(f"  FAIL {name}: {e}")
    if "--live" in sys.argv:
        fails += asyncio.run(live())
    print("\nALL GREEN" if not fails else f"\n{fails} FAILED")
    sys.exit(1 if fails else 0)
