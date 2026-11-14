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
import datetime as dt
import json
import logging
import os
import time
import zoneinfo
from typing import Any

import httpx

CT = zoneinfo.ZoneInfo("America/Chicago")
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
# privacy filter — unrevealed picks never leave this process
# -----------------------------------------------------------------------------
#
# RLS already hides other users' unlocked picks on reads, but RLS cannot save
# us if the caller happens to be the commissioner (exempt) or if we derive
# submission status from the picks list (e.g. "these teams haven't submitted").
# This filter runs regardless of who's asking so Mr. Locks never has unlocked
# picks in his context window.


def _now_ct_naive() -> dt.datetime:
    """Current time in America/Chicago as a naive datetime. Pulled out so
    tests can monkeypatch the clock without touching datetime.datetime."""
    return dt.datetime.now(CT).replace(tzinfo=None)


def _game_is_locked(game: dict, now_ct_naive: dt.datetime) -> bool:
    """A game is 'locked' once kickoff has passed or the game has ended.
    Matches the SQL picks_open_for_game() semantics, flipped."""
    if game.get("Status") in ("final", "in_progress", "in", "live"):
        return True
    start = game.get("Start (CT)")
    if not start:
        return False
    try:
        start_naive = dt.datetime.fromisoformat(start)
    except ValueError:
        return False
    # Supabase stores "Start (CT)" as a naive CT timestamp; compare like-for-like.
    if start_naive.tzinfo is not None:
        start_naive = start_naive.astimezone(CT).replace(tzinfo=None)
    return start_naive <= now_ct_naive


def filter_visible_picks(
    picks: list[dict],
    week_games: list[dict],
    caller_team_id: int | None,
    now_ct_naive: dt.datetime,
) -> list[dict]:
    """Return picks the caller is allowed to see:
      - all of the caller's own picks
      - other users' picks only for games that have already kicked off
    """
    locked_ids = {
        g["GameId"] for g in week_games if _game_is_locked(g, now_ct_naive)
    }
    out = []
    for p in picks:
        if caller_team_id is not None and p["team_id"] == caller_team_id:
            out.append(p)
        elif p["game_id"] in locked_ids:
            out.append(p)
    return out


# -----------------------------------------------------------------------------
# context assembly
# -----------------------------------------------------------------------------


def _compute_rivals(
    standings_ytd: list[dict],
    teams: list[dict],
    me_team_name: str,
    rival_predictions: dict | None,
    name_by_team: dict,
) -> list[dict]:
    """Return 4 rivals by standings proximity: the two above and two below
    the caller. Rank-1 or last-place fall back to the four nearest in-range.

    `rival_predictions` comes from the client — it's the output of the
    existing in-browser scouting model (js/scoutData.js). We don't recompute
    it server-side; we just thread it through with a clear label."""
    rivals: list[dict] = []
    if not standings_ytd:
        return rivals

    ranked = [dict(r, rank=i + 1) for i, r in enumerate(standings_ytd)]
    my_ix = next((i for i, r in enumerate(ranked) if r["team"] == me_team_name), None)
    if my_ix is None:
        return rivals

    # Pick 2 above + 2 below, shifting toward the middle at the edges.
    lo = max(0, my_ix - 2)
    hi = min(len(ranked), my_ix + 3)   # +3 because slice is exclusive
    window = [r for r in ranked[lo:hi] if r["team"] != me_team_name]
    # Widen if we're short of 4 (user near the edge).
    extra_lo = lo
    extra_hi = hi
    while len(window) < 4 and (extra_lo > 0 or extra_hi < len(ranked)):
        if extra_lo > 0:
            extra_lo -= 1
            if ranked[extra_lo]["team"] != me_team_name:
                window.insert(0, ranked[extra_lo])
        if len(window) >= 4:
            break
        if extra_hi < len(ranked):
            cand = ranked[extra_hi]
            extra_hi += 1
            if cand["team"] != me_team_name:
                window.append(cand)
    window = window[:4]

    tid_by_name = {t["team_name"]: t["team_id"] for t in teams}
    for r in window:
        tid = tid_by_name.get(r["team"])
        preds_raw = (rival_predictions or {}).get(str(tid), {}) if tid else {}
        # normalise to a compact shape
        probable = []
        for gid, obj in (preds_raw or {}).items():
            if not isinstance(obj, dict):
                continue
            pick = obj.get("pick")
            conf = obj.get("confidence")
            if pick is None:
                continue
            probable.append({
                "game_id": int(gid) if str(gid).isdigit() else gid,
                "pick": pick,
                "confidence": round(float(conf), 2) if conf is not None else None,
            })
        rivals.append({
            "team_name": r["team"],
            "rank": r["rank"],
            "record": f"{r['w']}-{r['l']}",
            "probable_picks": probable,
        })
    return rivals


async def _build_context(
    client: httpx.AsyncClient,
    jwt: str,
    user_id: str,
    mode: str = "leaderboard",
    rival_predictions: dict | None = None,
) -> dict:
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
    name_by_team = {t["team_id"]: t["team_name"] for t in teams}

    # -- PRIVACY: hide unrevealed picks before anything else touches them ---
    # Even if the caller is the commissioner (RLS-exempt), Mr. Locks never
    # gets unrevealed picks in his context. Caller's own picks always pass.
    now_ct = _now_ct_naive()
    visible_week = filter_visible_picks(picks_week, week_games, me_team_id, now_ct)

    # -- head-to-head this week: only locked games + caller's own ----------
    picks_by_team = collections.defaultdict(list)
    for p in visible_week:
        picks_by_team[p["team_id"]].append(
            {"game_id": p["game_id"], "pick": p["pick"], "by_slots": p["by_slots"]}
        )

    # Submission status is itself sensitive — "who hasn't picked" leaks the
    # absence of a lock-in before the game starts. Only expose the caller's
    # own status.
    you_have_picks = any(p["team_id"] == me_team_id for p in picks_week)

    jp = jackpot_rows[0] if jackpot_rows else {}
    jackpot = {
        "pulls_since_pop": jp.get("pulls_since_pop"),
        "last_pop_at": jp.get("last_pop_at"),
        "last_pop_team": name_by_team.get(jp.get("last_pop_team_id")),
    }

    # Compact structures: the model does better with tidy JSON than prose.
    result = {
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
        # Only revealed picks appear here — never unlocked games except the
        # caller's own. Mr. Locks is contractually required to say "can't say,
        # picks aren't locked yet" when asked about an unstarted game.
        "revealed_picks_this_week": {
            name_by_team.get(tid, f"team#{tid}"): plist
            for tid, plist in picks_by_team.items()
        },
        "you_have_picks_this_week": you_have_picks,
        "jackpot": jackpot,
        "mode": mode,
    }
    # Standings are needed before rivals can be computed — put them in first,
    # then layer rivals on top.
    result["rivals"] = _compute_rivals(
        result["standings_ytd"], teams, me_name, rival_predictions, name_by_team
    )
    return result


PICKS_MODE_RIDER = (
    "\n\nDEEP ANALYSIS MODE — the user is on the make-picks page. Lean into "
    "concrete recommendations. You have the week's spreads and `rivals`: the "
    "four teams nearest the user in the standings, each with `probable_picks` "
    "— the output of the pool's own historical-behavior model, NOT confirmed "
    "picks. Say 'likely picking' or 'leans toward,' never 'is picking.' When "
    "the user asks what to pick, reason game-by-game: name the side, give one "
    "concrete reason tied to the spread or a rival-differentiation angle "
    "(e.g. 'top three rivals are all chalk-stacking Michigan, so Illinois +7 "
    "is where you get separation'). If `probable_picks` is empty for a rival, "
    "don't guess — you just don't have a read on them."
)

LEADERBOARD_MODE_RIDER = (
    "\n\nLEADERBOARD MODE — the user is viewing standings. Keep answers "
    "short and conversational, league-wide gossip and quick stats."
)


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
        "PRIVACY — HARD RULE. The JSON only contains picks for games that "
        "have already kicked off, plus the user's own picks. If someone asks "
        "'who's picking X this week' about a game that is not in "
        "revealed_picks_this_week, answer literally: 'Can't say — picks "
        "aren't locked in yet for that one.' NEVER speculate about who might "
        "pick what. NEVER comment on which teams have or haven't submitted "
        "picks for the upcoming week — you only know the caller's own "
        "submission status (you_have_picks_this_week).\n\n"
        + (PICKS_MODE_RIDER if ctx.get("mode") == "picks" else LEADERBOARD_MODE_RIDER)
        + "\n\nCurrent league state (JSON):\n"
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
    mode = body.get("mode") or "leaderboard"
    if mode not in ("leaderboard", "picks"):
        mode = "leaderboard"
    rival_predictions = body.get("rival_predictions") or None
    if rival_predictions is not None and not isinstance(rival_predictions, dict):
        rival_predictions = None
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
            ctx = await _build_context(
                client, jwt, user_id, mode=mode, rival_predictions=rival_predictions
            )
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
