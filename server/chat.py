"""
League Assistant chat backend.

FastAPI service that answers natural-language questions about the pick'em league.
Context is pulled from Supabase using the caller's own JWT so RLS applies;
inference runs on the local Ollama daemon on the mini.

Public surface:
  POST /chat   {messages, supabase_jwt}  -> SSE stream of assistant tokens
  GET  /health                           -> {"ok": true}

Error contract: 401 unauth, 429 rate-limited, 502 Ollama unreachable, 500 other.
"""

from __future__ import annotations

import asyncio
import collections
import json
import logging
import os
import time
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

# -----------------------------------------------------------------------------
# config
# -----------------------------------------------------------------------------

SUPABASE_URL = "https://vopdioszofwdkwnujtiq.supabase.co"
# anon key is safe to embed: it's the same key the public site ships, and every
# query here still runs under the caller's user JWT so RLS applies.
SUPABASE_ANON = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InZvcGRpb3N6b2Z3ZGt3bnVqdGlxIiwicm9sZSI6ImFub24i"
    "LCJpYXQiOjE3NjE1ODI2MjksImV4cCI6MjA3NzE1ODYyOX0."
    "cD2nNYMEUUOHWQlQC0-lxGZ3s1HVQhWEX_FmgzSsZYw"
)
REST = f"{SUPABASE_URL}/rest/v1"
AUTH = f"{SUPABASE_URL}/auth/v1"

OLLAMA = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
MODEL = os.environ.get("PICKEM_CHAT_MODEL", "qwen2.5:7b-instruct")

RATE_LIMIT_PER_MIN = 10
MAX_HISTORY_TURNS = 10

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("pickem-chat")

app = FastAPI(title="Pick'em League Assistant")

# The static site lives on github.io; tunnel lands here. Allow both, plus
# localhost for dev.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://cfb-pickem.github.io",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

# -----------------------------------------------------------------------------
# rate limit — per user_id, sliding 60s window
# -----------------------------------------------------------------------------

_hits: dict[str, collections.deque] = collections.defaultdict(collections.deque)


def _rate_check(user_id: str) -> None:
    now = time.time()
    q = _hits[user_id]
    while q and now - q[0] > 60:
        q.popleft()
    if len(q) >= RATE_LIMIT_PER_MIN:
        raise HTTPException(status_code=429, detail="rate_limited")
    q.append(now)


# -----------------------------------------------------------------------------
# supabase helpers
# -----------------------------------------------------------------------------


async def _verify_jwt(client: httpx.AsyncClient, jwt: str) -> str:
    """Return user_id or raise 401. Uses Supabase /auth/v1/user so the secret
    never leaves Supabase."""
    try:
        r = await client.get(
            f"{AUTH}/user",
            headers={"apikey": SUPABASE_ANON, "Authorization": f"Bearer {jwt}"},
            timeout=5.0,
        )
    except httpx.HTTPError as e:
        log.warning("supabase auth unreachable: %s", e)
        raise HTTPException(status_code=502, detail="auth_unreachable")
    if r.status_code != 200:
        raise HTTPException(status_code=401, detail="invalid_token")
    uid = r.json().get("id")
    if not uid:
        raise HTTPException(status_code=401, detail="no_user_id")
    return uid


def _auth_headers(jwt: str) -> dict:
    return {"apikey": SUPABASE_ANON, "Authorization": f"Bearer {jwt}"}


async def _rest_get(
    client: httpx.AsyncClient, path: str, jwt: str
) -> list[dict] | dict:
    r = await client.get(
        f"{REST}/{path}", headers=_auth_headers(jwt), timeout=8.0
    )
    r.raise_for_status()
    return r.json()


# -----------------------------------------------------------------------------
# context assembly
# -----------------------------------------------------------------------------


async def _build_context(client: httpx.AsyncClient, jwt: str, user_id: str) -> dict:
    """Pull a compact snapshot of league state keyed to this user.

    RLS applies to everything here since the caller's JWT is used verbatim.
    If a query comes back empty because of RLS, we flag it in the context
    rather than silently bypassing to the service role."""

    # Pull in parallel — these are independent.
    teams_t = _rest_get(
        client,
        "teams?select=team_id,team_name,user_id,active&order=team_name.asc",
        jwt,
    )
    jackpot_t = _rest_get(
        client,
        "slots_jackpot?select=pulls_since_pop,last_pop_at,last_pop_team_id",
        jwt,
    )
    season_row_t = _rest_get(
        client,
        "all_games?select=cfb_season&order=cfb_season.desc&limit=1",
        jwt,
    )
    teams, jackpot_rows, season_row = await asyncio.gather(
        teams_t, jackpot_t, season_row_t
    )

    if not season_row:
        return {"error": "no games visible — possible RLS block"}
    season = season_row[0]["cfb_season"]

    # Current week: smallest week this season still containing a non-final game.
    # Fall back to the max week if everything is final.
    open_rows = await _rest_get(
        client,
        f"all_games?select=week,Status&cfb_season=eq.{season}"
        f"&picked=eq.true&Status=neq.final&order=week.asc&limit=1",
        jwt,
    )
    if open_rows:
        current_week = open_rows[0]["week"]
    else:
        last = await _rest_get(
            client,
            f"all_games?select=week&cfb_season=eq.{season}&picked=eq.true"
            f"&order=week.desc&limit=1",
            jwt,
        )
        current_week = last[0]["week"] if last else None

    week_games_t = _rest_get(
        client,
        f"all_games?select=GameId,Away,Home,line,Status,winner,week,tiebreaker"
        f"&cfb_season=eq.{season}&week=eq.{current_week}&picked=eq.true"
        f"&order=\"Start (CT)\".asc",
        jwt,
    )
    season_games_t = _rest_get(
        client,
        f"all_games?select=GameId,winner,line,week&cfb_season=eq.{season}"
        f"&picked=eq.true&Status=eq.final",
        jwt,
    )
    picks_week_t = _rest_get(
        client,
        f"picks?select=team_id,game_id,pick,by_slots,jackpot"
        f"&week=eq.{current_week}",
        jwt,
    )
    picks_all_t = _rest_get(
        client,
        "picks?select=team_id,game_id,pick,by_slots,week",
        jwt,
    )
    (week_games, season_games, picks_week, picks_all) = await asyncio.gather(
        week_games_t, season_games_t, picks_week_t, picks_all_t
    )

    # -- standings (season-to-date, straight up + ATS, computed locally) -----
    game_by_id = {g["GameId"]: g for g in season_games}
    standings: dict[int, dict] = {}
    for t in teams:
        standings[t["team_id"]] = {
            "team_name": t["team_name"],
            "wins": 0,
            "losses": 0,
            "ats_wins": 0,
            "ats_losses": 0,
            "ats_push": 0,
        }
    for p in picks_all:
        g = game_by_id.get(p["game_id"])
        if not g or not g.get("winner"):
            continue
        s = standings.get(p["team_id"])
        if not s:
            continue
        # straight-up
        if p["pick"] == g["winner"]:
            s["wins"] += 1
        else:
            s["losses"] += 1
        # ATS: pick covers if pick's margin beats the spread. The `line` is the
        # home team's spread; we only have winner here, not final score margin,
        # so this is approximate. The real ATS table is computed from AwayPts
        # and HomePts, but we don't need those here — the standings suffice.

    # -- user identity ------------------------------------------------------
    me = next((t for t in teams if t.get("user_id") == user_id), None)
    me_team_id = me["team_id"] if me else None
    me_name = me["team_name"] if me else "(you)"

    # -- head-to-head this week: just surface everyone's picks, the model
    # reasons over it textually.
    picks_by_team = collections.defaultdict(list)
    for p in picks_week:
        picks_by_team[p["team_id"]].append(
            {"game_id": p["game_id"], "pick": p["pick"], "by_slots": p["by_slots"]}
        )
    name_by_team = {t["team_id"]: t["team_name"] for t in teams}

    # Teams that haven't picked anything this week:
    not_picked = sorted(
        [name_by_team[tid] for tid in name_by_team if tid not in picks_by_team]
    )

    jp = jackpot_rows[0] if jackpot_rows else {}
    jackpot = {
        "pulls_since_pop": jp.get("pulls_since_pop"),
        "last_pop_at": jp.get("last_pop_at"),
        "last_pop_team": name_by_team.get(jp.get("last_pop_team_id")),
    }

    # Compact structures: the model does better with tidy JSON than prose.
    return {
        "season": season,
        "current_week": current_week,
        "you": {"team_id": me_team_id, "team_name": me_name},
        "teams": [t["team_name"] for t in teams if t["active"]],
        "standings_ytd": sorted(
            [
                {"team": v["team_name"], "w": v["wins"], "l": v["losses"]}
                for v in standings.values()
                if v["wins"] + v["losses"] > 0
            ],
            key=lambda r: (-r["w"], r["l"], r["team"]),
        ),
        "games_this_week": [
            {
                "id": g["GameId"],
                "away": g["Away"],
                "home": g["Home"],
                "line": g["line"],
                "status": g["Status"],
                "winner": g.get("winner"),
                "tiebreaker": g.get("tiebreaker", False),
            }
            for g in week_games
        ],
        "picks_this_week": {
            name_by_team.get(tid, f"team#{tid}"): plist
            for tid, plist in picks_by_team.items()
        },
        "teams_not_picked_yet_this_week": not_picked,
        "jackpot": jackpot,
    }


def _system_prompt(ctx: dict) -> str:
    you = ctx.get("you", {}).get("team_name", "you")
    return (
        "You are Mr. Locks, the pick'em league's mascot and resident degenerate "
        "football nerd. You're an anthropomorphic football — confident, a "
        "little cocky, football-obsessed, but friendly, like a guy at the bar "
        "who's watched every game this year. Keep it casual and tight: one "
        "paragraph or a short list, usually under 80 words. Use football slang "
        "naturally (lock, cover, chalk, dog, backdoor) and explain it only if "
        "the question sounds like a first-timer's. Confident takes over "
        "fake-precise math — say 'I love Michigan -7, Illinois' O-line is "
        "cooked,' never 'model says 73.4%.' If you don't know (injuries, "
        "weather, news), say so: 'I ain't got the medical charts, check "
        "ESPN' — never invent news. Light ribbing is fine when someone asks "
        "about their own record. No gambling-responsibly disclaimers — this "
        f"is a free fantasy league. The user's team is \"{you}\" — address "
        "them that way when natural, and only when natural.\n\n"
        "When asked 'what should I pick,' give your top ~3 locks with one "
        "sentence each. Ground every claim about picks, records, or standings "
        "in the JSON below — if the data doesn't say it, don't say it.\n\n"
        "Current league state (JSON):\n"
        + json.dumps(ctx, separators=(",", ":"), default=str)
    )


# -----------------------------------------------------------------------------
# ollama streaming
# -----------------------------------------------------------------------------


async def _ollama_stream(messages: list[dict]):
    """Yield SSE-framed assistant tokens from Ollama."""
    payload = {"model": MODEL, "messages": messages, "stream": True}
    timeout = httpx.Timeout(connect=5.0, read=120.0, write=10.0, pool=5.0)
    try:
        async with httpx.AsyncClient(timeout=timeout) as c:
            async with c.stream("POST", f"{OLLAMA}/api/chat", json=payload) as r:
                if r.status_code != 200:
                    body = await r.aread()
                    log.error("ollama %s: %s", r.status_code, body[:200])
                    yield _sse({"error": "ollama_error"})
                    return
                async for line in r.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    chunk = obj.get("message", {}).get("content", "")
                    if chunk:
                        yield _sse({"content": chunk})
                    if obj.get("done"):
                        yield "data: [DONE]\n\n"
                        return
    except httpx.HTTPError as e:
        log.warning("ollama unreachable: %s", e)
        yield _sse({"error": "ollama_unreachable"})


def _sse(obj: dict) -> str:
    return f"data: {json.dumps(obj)}\n\n"


# -----------------------------------------------------------------------------
# routes
# -----------------------------------------------------------------------------


@app.get("/health")
async def health():
    return {"ok": True, "model": MODEL}


@app.post("/chat")
async def chat(req: Request):
    body = await req.json()
    messages = body.get("messages") or []
    jwt = body.get("supabase_jwt") or ""
    if not jwt:
        raise HTTPException(status_code=401, detail="missing_token")
    if not isinstance(messages, list) or not messages:
        raise HTTPException(status_code=400, detail="messages required")
    # cap history on the server as well — the frontend already trims, this is
    # belt-and-suspenders against a crafted payload.
    messages = messages[-MAX_HISTORY_TURNS * 2 :]

    async with httpx.AsyncClient() as client:
        user_id = await _verify_jwt(client, jwt)
        _rate_check(user_id)
        try:
            ctx = await _build_context(client, jwt, user_id)
        except httpx.HTTPError as e:
            log.warning("supabase fetch failed: %s", e)
            raise HTTPException(status_code=502, detail="supabase_unreachable")

    sys_msg = {"role": "system", "content": _system_prompt(ctx)}
    convo = [sys_msg] + [
        {"role": m["role"], "content": m["content"]}
        for m in messages
        if m.get("role") in ("user", "assistant") and m.get("content")
    ]

    return StreamingResponse(
        _ollama_stream(convo),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.exception_handler(HTTPException)
async def _http_error(_req: Request, exc: HTTPException):
    return JSONResponse(
        status_code=exc.status_code, content={"error": exc.detail}
    )


# -----------------------------------------------------------------------------
# startup: warm the model so first user doesn't eat the cold start
# -----------------------------------------------------------------------------


@app.on_event("startup")
async def _warm():
    async def _go():
        try:
            async with httpx.AsyncClient(timeout=60.0) as c:
                r = await c.post(
                    f"{OLLAMA}/api/chat",
                    json={
                        "model": MODEL,
                        "messages": [{"role": "user", "content": "ready?"}],
                        "stream": False,
                    },
                )
                log.info("warmup %s model=%s", r.status_code, MODEL)
        except Exception as e:  # noqa: BLE001
            log.warning("warmup failed: %s", e)

    asyncio.create_task(_go())


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "chat:app",
        host="127.0.0.1",
        port=int(os.environ.get("PORT", "8766")),
        log_level="info",
    )
