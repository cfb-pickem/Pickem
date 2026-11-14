"""
League Assistant chat backend.

FastAPI service that answers natural-language questions about the pick'em league.
Context is pulled from Supabase using the caller's own JWT so RLS applies;
inference runs on the local Ollama daemon on the mini. The Claude Haiku path
is still here but off unless PICKY_USE_CLAUDE=1 (then Ollama is its fallback).

Public surface:
  POST /chat   {messages, supabase_jwt}  -> SSE stream of assistant tokens
  GET  /health                           -> {"ok": true}

Error contract: 401 unauth, 429 rate-limited, 502 Supabase unreachable, 500 other.
"""

from __future__ import annotations

import asyncio
import collections
import datetime as dt
import json
import logging
import os
import re
import subprocess
import time
import unicodedata
import zoneinfo
from typing import Any

import anthropic
import httpx

import espn

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
CLAUDE_MODEL = os.environ.get("PICKY_CLAUDE_MODEL", "claude-haiku-5-5")
CLAUDE_EFFORT = os.environ.get("PICKY_CLAUDE_EFFORT", "medium")
# The API key never touches the repo (public GitHub Pages): env first, else
# the login keychain, account "drake".
KEYCHAIN_SERVICE = os.environ.get("PICKY_ANTHROPIC_KEYCHAIN", "DCC__ANTHROPIC_API_KEY")
IMAGE_URL = os.environ.get("PICKY_IMAGE_URL", "http://127.0.0.1:8767")

# Ollama's default num_ctx (4096 here) silently drops the front of a long
# prompt: league context + ten turns of history. The prompt is ~6.5k tokens
# with ESPN stats, so 16384 leaves room for history. Same options on warmup so the model is
# never reloaded at a different size. Low temperature: facts over flair.
# presence_penalty 0 overrides qwen3.5's default 1.5, which pushes it off
# names and numbers it has already said and garbles facts.
OLLAMA_OPTIONS = {"num_ctx": 16384, "temperature": 0.2, "presence_penalty": 0}
# The fallback only loads when Claude fails; let it unload afterwards instead
# of pinning ~6.5 GB next to the VM and the image model.
OLLAMA_FALLBACK_KEEP_ALIVE = "15m"
# As the primary model: how long it stays loaded after the last message.
OLLAMA_KEEP_ALIVE = os.environ.get("PICKY_OLLAMA_KEEP_ALIVE", "30m")
# Thinking models (qwen3.5): "false" skips the reasoning pass. Unset = don't send.
OLLAMA_THINK = os.environ.get("PICKY_OLLAMA_THINK")

RATE_LIMIT_PER_MIN = 10
MAX_HISTORY_TURNS = 10

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("picky")


def _anthropic_key() -> str | None:
    if os.environ.get("ANTHROPIC_API_KEY"):
        return os.environ["ANTHROPIC_API_KEY"]
    try:
        r = subprocess.run(
            ["security", "find-generic-password", "-a", "drake", "-s", KEYCHAIN_SERVICE, "-w"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        log.warning("keychain lookup failed: %s", e)
        return None
    return r.stdout.strip() or None


# Off by default: Picky runs 100% on the local model so there are no API costs.
# PICKY_USE_CLAUDE=1 turns the Claude path back on (key isn't even read otherwise).
USE_CLAUDE = os.environ.get("PICKY_USE_CLAUDE") == "1"
_key = _anthropic_key() if USE_CLAUDE else None
CLAUDE = anthropic.AsyncAnthropic(api_key=_key, max_retries=1, timeout=60.0) if _key else None
if not CLAUDE:
    log.warning("Claude off (PICKY_USE_CLAUDE=%s); running on Ollama %s only",
                os.environ.get("PICKY_USE_CLAUDE", "unset"), MODEL)

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


PAGE = 1000  # PostgREST's max-rows cap; anything past it is silently dropped


async def _rest_get_all(client: httpx.AsyncClient, path: str, jwt: str) -> list[dict]:
    """_rest_get, paged past the 1000-row cap. `path` must have a stable order."""
    rows: list[dict] = []
    while True:
        page = await _rest_get(client, f"{path}&limit={PAGE}&offset={len(rows)}", jwt)
        rows.extend(page)
        if len(page) < PAGE:
            return rows


def _in_ids(ids) -> str:
    return "(" + ",".join(str(int(i)) for i in ids) + ")"


# -----------------------------------------------------------------------------
# privacy filter — unrevealed picks never leave this process
# -----------------------------------------------------------------------------
#
# RLS already hides other users' unlocked picks on reads, but RLS cannot save
# us if the caller happens to be the commissioner (exempt) or if we derive
# submission status from the picks list (e.g. "these teams haven't submitted").
# This filter runs regardless of who's asking so Picky never has unlocked
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


def _norm_team(v) -> str:
    """Port of js/utils.js norm(): accents, punctuation and case don't count."""
    s = unicodedata.normalize("NFD", str(v or ""))
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = s.replace("&", " and ").replace(".", "")
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def same_team(a, b) -> bool:
    """Port of js/utils.js sameTeam(): 'Oregon St.' == 'Oregon State'."""
    na, nb = _norm_team(a), _norm_team(b)
    st = lambda x: re.sub(r"\bst\b", "state", x)  # noqa: E731
    return na == nb or st(na) == st(nb)


def _graded(season_games: list[dict], picks: list[dict]):
    """(team_id, week, hit) for every pick on a decided, non-tiebreaker game."""
    by_id = {
        g["GameId"]: g
        for g in season_games
        if g.get("winner") and not g.get("tiebreaker")
    }
    for p in picks:
        g = by_id.get(p["game_id"])
        if g is not None:
            yield p["team_id"], g.get("week"), bool(p.get("jackpot") or same_team(p["pick"], g["winner"]))


def compute_weekly(teams: list[dict], season_games: list[dict], picks: list[dict]) -> dict:
    """{team_name: {week: [w, l]}}, same scoring as compute_standings."""
    name_by_team = {t["team_id"]: t["team_name"] for t in teams}
    out: dict[str, dict] = {}
    for tid, week, hit in _graded(season_games, picks):
        if tid not in name_by_team or week is None:
            continue
        wl = out.setdefault(name_by_team[tid], {}).setdefault(week, [0, 0])
        wl[0 if hit else 1] += 1
    return out


def compute_standings(teams: list[dict], season_games: list[dict], picks: list[dict]) -> list[dict]:
    """Season points exactly as the leaderboard scores them: a point when the
    pick matches `winner` (the side that covered) or the pick hit the slots
    jackpot; tiebreaker games never count. `l` is the graded picks that
    missed. Sorted like the board, most points first."""
    name_by_team = {t["team_id"]: t["team_name"] for t in teams}
    rec = {tid: [0, 0] for tid in name_by_team}
    for tid, _week, hit in _graded(season_games, picks):
        if tid in rec:
            rec[tid][0 if hit else 1] += 1
    rows = sorted(
        ({"team": name_by_team[tid], "w": w, "l": l} for tid, (w, l) in rec.items() if w + l),
        key=lambda r: (-r["w"], r["l"], r["team"]),
    )
    # Explicit rank (ties share it): left to infer it from list order, the 7B
    # model sets the caller aside and calls the next team "first".
    for i, r in enumerate(rows):
        r["rank"] = rows[i - 1]["rank"] if i and rows[i - 1]["w"] == r["w"] else i + 1
    return [{"rank": r["rank"], "team": r["team"], "w": r["w"], "l": r["l"]} for r in rows]


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
        f"all_games?select=GameId,Away,Home,line,Status,winner,week,tiebreaker,"
        f"\"Start (CT)\""
        f"&cfb_season=eq.{season}&week=eq.{current_week}&picked=eq.true"
        f"&order=\"Start (CT)\".asc",
        jwt,
    )
    season_games_t = _rest_get(
        client,
        f"all_games?select=GameId,Away,Home,AwayPts,HomePts,winner,line,week,tiebreaker"
        f"&cfb_season=eq.{season}"
        f"&picked=eq.true&Status=eq.final",
        jwt,
    )
    week_games, season_games = await asyncio.gather(week_games_t, season_games_t)

    # `picks` has no season column, so scope by game id: `week=eq.N` alone
    # also returns last season's week N. Season picks run past PostgREST's
    # 1000-row cap, so they're paged.
    week_ids = [g["GameId"] for g in week_games]
    final_ids = [g["GameId"] for g in season_games]
    picks_week_t = (
        _rest_get(
            client,
            f"picks?select=team_id,game_id,pick,by_slots,jackpot"
            f"&game_id=in.{_in_ids(week_ids)}",
            jwt,
        )
        if week_ids else asyncio.sleep(0, [])
    )
    picks_all_t = (
        _rest_get_all(
            client,
            f"picks?select=team_id,game_id,pick,by_slots,week,jackpot"
            f"&game_id=in.{_in_ids(final_ids)}&order=id.asc",
            jwt,
        )
        if final_ids else asyncio.sleep(0, [])
    )
    picks_week, picks_all = await asyncio.gather(picks_week_t, picks_all_t)

    # -- user identity ------------------------------------------------------
    me = next((t for t in teams if t.get("user_id") == user_id), None)
    me_team_id = me["team_id"] if me else None
    me_name = me["team_name"] if me else "(you)"
    name_by_team = {t["team_id"]: t["team_name"] for t in teams}

    # -- PRIVACY: hide unrevealed picks before anything else touches them ---
    # Even if the caller is the commissioner (RLS-exempt), Picky never
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
        # w = season points, same number as the leaderboard's Points column.
        "standings_ytd": compute_standings(teams, season_games, picks_all),
        "games_this_week": [
            {
                "id": g["GameId"],
                "start": g.get("Start (CT)"),
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
        # caller's own. Picky is contractually required to say "can't say,
        # picks aren't locked yet" when asked about an unstarted game.
        "revealed_picks_this_week": {
            name_by_team.get(tid, f"team#{tid}"): plist
            for tid, plist in picks_by_team.items()
        },
        "you_have_picks_this_week": you_have_picks,
        "jackpot": jackpot,
        "weekly_records": compute_weekly(teams, season_games, picks_all),
        "last_week": _last_week(current_week, season_games, picks_all, name_by_team, me_team_id),
        "mode": mode,
    }
    # Standings are needed before rivals can be computed — put them in first,
    # then layer rivals on top.
    result["rivals"] = _compute_rivals(
        result["standings_ytd"], teams, me_name, rival_predictions, name_by_team
    )
    return result


def _last_week(current_week, season_games, picks_all, name_by_team, me_team_id) -> dict:
    """Finals from the week before this one, who picked what, and the
    caller's record. Every game here is final, so every pick is revealed."""
    if not isinstance(current_week, int) or current_week < 2:
        return {}
    wk = current_week - 1
    games = [g for g in season_games if g.get("week") == wk and not g.get("tiebreaker")]
    if not games:
        return {}
    ids = {g["GameId"] for g in games}
    by_game: dict = collections.defaultdict(dict)
    for p in picks_all:
        if p["game_id"] in ids and p["team_id"] in name_by_team:
            by_game[p["game_id"]][name_by_team[p["team_id"]]] = p["pick"]
    mine = [0, 0]
    for tid, _w, hit in _graded(games, picks_all):
        if tid == me_team_id:
            mine[0 if hit else 1] += 1
    return {
        "week": wk,
        "you": f"{mine[0]}-{mine[1]}" if sum(mine) else None,
        "games": [
            {
                "away": g.get("Away"), "home": g.get("Home"),
                "away_pts": g.get("AwayPts"), "home_pts": g.get("HomePts"),
                "line": g.get("line"), "covered": g.get("winner"),
                "picks": by_game.get(g["GameId"], {}),
            }
            for g in games
        ],
    }


PICKS_MODE_RIDER = (
    "\n\nDEEP ANALYSIS MODE: the user is on the make-picks page. Lean into "
    "concrete recommendations, game by game, every game. RIVALS lists the "
    "four teams nearest the user in the standings with their 'probable "
    "picks': guesses from the pool's own model, NOT real picks. Say 'likely "
    "picking' or 'leans,' never 'is picking.' A rival with no probable picks "
    "listed: you have no read on them, so don't guess. Separation angles "
    "(fading what rivals likely pick) are a good reason when they apply."
)

LEADERBOARD_MODE_RIDER = (
    "\n\nLEADERBOARD MODE: the user is looking at the standings. Quick hits "
    "for gossip and stats; picks questions still get every game."
)

# Few-shot answers for a small local model. Fake names and numbers on purpose,
# so the model copies the shape and voice, not the facts.
EXAMPLES = """EXAMPLES. Names and numbers here are FAKE; copy the style, never the facts.

Q: Should I change any of my picks?
A: Two of these are flat-out stupid. Here's the fix:
1. Utah -3.5: keep. Value side, the market has Utah -6.
2. Iowa +7.5: stupid. Market rates Ohio 3 points better. Flip to Ohio -7.5.
3. Navy +2.5: no edge either way. Navy scores 31 a game to Army's 20; I'd keep it.

Q: Roast Gus
A: Gus is 19-25, dead last, and that's the nice part. Last week he was the only team on Kansas, and Kansas didn't cover. Even his good week was a .500 week. Gus doesn't need picks advice, he needs supervision.

Q: Who did Rita pick in Utah at Iowa?
A: Rita's pick unlocks at kickoff, Sat 2:30 PM CT. Patience, snoop.

Q: Who has Utah this week?
A: You took Iowa +3, so not you. Everyone else's pick in that game unlocks at kickoff, Sat 2:30 PM CT.

Q: Explain the Utah Iowa spread like I'm dumb
A: Fine, slowly. Pool line is Utah -3.5 / Iowa +3.5. Utah covers only by winning by 4 or more. Iowa covers by winning, or losing by 3 or fewer. The market has Utah -6, so Utah -3.5 is the value side. You took Iowa. Of course you did.

Q: Is Iowa's quarterback healthy?
A: There's no college injury report, genius. His health is unknown to me. Go read the Iowa beat writers.

Q: Who do you like in Texas vs Oklahoma?
A: That game isn't in the pool, chief. You can't beat the four you've got and you want a fifth?"""


def _num(x) -> str:
    try:
        f = float(x)
    except (TypeError, ValueError):
        return str(x)
    return str(int(f)) if f == int(f) else str(f)


def _ordinal(n: int) -> str:
    suf = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suf}"


def pool_line(away: str, home: str, line) -> str:
    """`line` is added to the home score (set_winner_on_final: home covers
    when home + line > away), so a negative line means home is favored."""
    try:
        ln = float(line)
    except (TypeError, ValueError):
        return "no line posted"
    if ln == 0:
        return "pick'em (no spread)"
    fav, dog = (home, away) if ln < 0 else (away, home)
    return f"{fav} -{_num(abs(ln))} / {dog} +{_num(abs(ln))}"


def _kickoff(start) -> str:
    try:
        t = dt.datetime.fromisoformat(str(start))
    except ValueError:
        return "kickoff time unknown"
    return t.strftime("%a %-I:%M %p CT")


def _market(notes: dict, away: str, home: str) -> str | None:
    m = notes.get("market") or {}
    det = (m.get("details") or "").strip()
    if not det:
        return None
    for side, name in (("home", home), ("away", away)):
        ab = (notes.get(side) or {}).get("abbr")
        if ab and det.upper().startswith(ab.upper() + " "):
            det = name + det[len(ab):]
            break
    ou = f", over/under {_num(m['over_under'])}" if m.get("over_under") is not None else ""
    return f"{det}{ou}"


def _market_gap(notes: dict, line) -> tuple[float, float] | None:
    """(pool home spread, gap). Both lines turned into a home spread
    (negative = home favored); gap > 0 means the market likes home more than
    the pool does. None when either line is missing or unreadable."""
    det = ((notes.get("market") or {}).get("details") or "").strip()
    try:
        pool_home = float(line)
    except (TypeError, ValueError):
        return None
    if det.upper() == "EVEN":
        mkt_home = 0.0
    else:
        m = re.match(r"^(\S+)\s+([+-]?\d+(?:\.\d+)?)$", det)
        if not m:
            return None
        ab, n = m.group(1).upper(), float(m.group(2))
        if ab == ((notes.get("home") or {}).get("abbr") or "").upper():
            mkt_home = n
        elif ab == ((notes.get("away") or {}).get("abbr") or "").upper():
            mkt_home = -n
        else:
            return None
    return pool_home, pool_home - mkt_home


def _side_label(side: str, home: str, pool_home: float) -> str:
    """'Boise State -13.5' / 'Oregon State +13.5': `side` as written on the pool line."""
    fav_is_home = pool_home < 0
    sign = "-" if (side == home) == fav_is_home else "+"
    return f"{side} {sign}{_num(abs(pool_home))}"


def line_value(notes: dict, away: str, home: str, line) -> str | None:
    """Where the ESPN market and the pool line disagree; a gap means one side
    of the pool line is getting points the market wouldn't give."""
    mg = _market_gap(notes, line)
    if mg is None:
        return None
    pool_home, gap = mg
    if abs(gap) < 1:
        return "pool line and ESPN market are within a point, so no line value either way."
    side = home if gap > 0 else away
    return (f"{_side_label(side, home, pool_home)} is the value side: the market rates "
            f"{side} {_num(abs(gap))} points better than the pool line does.")


def pick_verdict(notes: dict, away: str, home: str, line, pick) -> str | None:
    """Is `pick` on the value side of the pool line vs the market? Computed
    here so the model looks the answer up instead of doing spread math."""
    mg = _market_gap(notes, line)
    if mg is None or not pick:
        return None
    pool_home, gap = mg
    if pool_home == 0:
        return None
    mine = home if same_team(pick, home) else away if same_team(pick, away) else None
    if mine is None:
        return None
    if abs(gap) < 1:
        return (f"{_side_label(mine, home, pool_home)} has no price edge either way (pool and "
                "market within a point); keep or flip only for football reasons.")
    side = home if gap > 0 else away
    if mine == side:
        return (f"{_side_label(mine, home, pool_home)} IS the value side: the market rates {side} "
                f"{_num(abs(gap))} points better than the pool line. Keep it.")
    return (f"{_side_label(mine, home, pool_home)} is the WRONG side of the value: the market rates "
            f"{side} {_num(abs(gap))} points better than the pool line. Flipping to "
            f"{_side_label(side, home, pool_home)} puts the price on your side.")


def _team_line(name: str, t: dict) -> str:
    bits = [f"#{t['ap_rank']} AP" if t.get("ap_rank") else "unranked"]
    if t.get("record"):
        bits.append(f"record {t['record']}")
    out = f"  {name}: " + ", ".join(bits)
    if t.get("last5"):
        out += ". Last 5 (oldest first): " + "; ".join(t["last5"])
    if t.get("stats"):
        out += ". Season per game: " + ", ".join(t["stats"])
    if t.get("leaders"):
        out += ". Season leaders: " + "; ".join(t["leaders"])
    return out


def render_context(ctx: dict) -> str:
    """The league snapshot as labeled plain text. A 7B model reads this far
    more reliably than a JSON blob: explicit counts, explicit ranks, the
    graded line spelled out per game, and 'none'/'unavailable' written down
    instead of left as a gap for the model to fill."""
    if ctx.get("error"):
        return f"LEAGUE DATA UNAVAILABLE: {ctx['error']}"
    me = (ctx.get("you") or {}).get("team_name") or "(you)"
    games = ctx.get("games_this_week") or []
    revealed = ctx.get("revealed_picks_this_week") or {}
    espn_all = ctx.get("espn")
    weekly = ctx.get("weekly_records") or {}
    L: list[str] = []
    # The clock is not here: _system_prompt adds it at the end, to the hour,
    # so the prompt stays byte-identical between messages and Ollama reuses it.
    L.append(f"Season {ctx.get('season')}, week {ctx.get('current_week')}. "
             f"The user is the team \"{me}\".")

    st = ctx.get("standings_ytd") or []
    L.append("")
    L.append(f"STANDINGS ({len(st)} teams; record = points-missed; points are picks "
             "that covered, the same number as the leaderboard's Points; T = tied):")
    for r in st:
        tied = sum(1 for x in st if x["rank"] == r["rank"]) > 1
        line = (f"{'T' if tied else ''}{_ordinal(r['rank'])}. {r['team']}: {r['w']}-{r['l']} "
                f"({r['w']} points, {r['l']} misses)")
        wk = weekly.get(r["team"]) or {}
        recent = sorted(wk)[-3:]
        if recent:
            line += " | weeks: " + ", ".join(f"wk{w} {wk[w][0]}-{wk[w][1]}" for w in recent)
        if r["team"] == me:
            line += "  <- the user"
        L.append(line)
    if st:
        def _who(rank):
            names = [x["team"] for x in st if x["rank"] == rank]
            pts = next(x["w"] for x in st if x["rank"] == rank)
            if len(names) == 1:
                return f"{names[0]}, alone, with {pts} points"
            return f"{' and '.join(names)}, tied with {pts} points each"
        second = next((x["rank"] for x in st if x["rank"] > st[0]["rank"]), None)
        L.append(f"First place: {_who(st[0]['rank'])}."
                 + (f" Next: {_who(second)}." if second and second != st[-1]["rank"] else "")
                 + f" Last place: {_who(st[-1]['rank'])}.")

    L.append("")
    L.append(f"YOUR GAMES THIS WEEK ({len(games)}): every team picks every one of these "
             f"{len(games)} games against the POOL LINE. No other games are in the pool.")
    mine = {p["game_id"]: p["pick"] for p in revealed.get(me, [])}
    for i, g in enumerate(games, 1):
        away, home = g["away"], g["home"]
        status = {"final": "FINAL", "in_progress": "LIVE", "in": "LIVE", "live": "LIVE"}.get(
            g.get("status"), "not started")
        tb = " TIEBREAKER game (doesn't count for points)." if g.get("tiebreaker") else ""
        L.append(f"Game {i}: {away} at {home}, {_kickoff(g.get('start'))}, {status}.{tb}")
        L.append(f"  POOL LINE, the two sides you can pick (graded on this): {pool_line(away, home, g.get('line'))}")
        if g.get("winner"):
            L.append(f"  Covered: {g['winner']}")
        notes = (espn_all or {}).get(g["id"]) if espn_all is not None else None
        if not notes or notes.get("unavailable"):
            L.append("  ESPN data: unavailable right now (no market line, records, injuries or news).")
        else:
            mk = _market(notes, away, home)
            L.append(f"  ESPN market line now: {mk}" if mk else "  ESPN market line: none posted")
            lv = line_value(notes, away, home, g.get("line"))
            if lv:
                L.append(f"  Line value: {lv}")
            L.append(_team_line(away, notes.get("away") or {}))
            L.append(_team_line(home, notes.get("home") or {}))
            wp = notes.get("espn_win_prob") or {}
            if wp.get("away") is not None and wp.get("home") is not None:
                L.append(f"  ESPN FPI matchup predictor (chance to WIN outright, NOT to cover): "
                         f"{away} {_num(wp['away'])}%, {home} {_num(wp['home'])}%")
            if notes.get("weather"):
                L.append(f"  Weather forecast: {notes['weather']}")
            inj_a = (notes.get("away") or {}).get("injuries") or []
            inj_h = (notes.get("home") or {}).get("injuries") or []
            if notes.get("injury_report_available") and (inj_a or inj_h):
                L.append(f"  Injuries (ESPN): {away}: {', '.join(inj_a) or 'none listed'}. "
                         f"{home}: {', '.join(inj_h) or 'none listed'}.")
            else:
                # ESPN publishes no college injury feed: an empty list means
                # "unknown", never "healthy".
                L.append("  Injuries: NO injury report available (ESPN doesn't publish college "
                         "injury reports). Health of both teams is unknown.")
            heads = notes.get("headlines") or []
            L.append("  Headlines: " + (" | ".join(heads) if heads else "none about these teams."))
        L.append(f"  {me}'s pick: {mine[g['id']]}" if g["id"] in mine else f"  {me}'s pick: not made yet")
        if g["id"] in mine and notes and not notes.get("unavailable"):
            pv = pick_verdict(notes, away, home, g.get("line"), mine[g["id"]])
            if pv:
                L.append(f"  Value check on {me}'s pick: {pv}")
        others = {t: p["pick"] for t, plist in revealed.items() if t != me
                  for p in plist if p["game_id"] == g["id"]}
        if others:
            sides = collections.defaultdict(list)
            for t, pk in sorted(others.items()):
                sides[pk].append(t)
            L.append("  Other picks (revealed): " + "; ".join(
                f"{pk}: {', '.join(ts)}" for pk, ts in sides.items()))
        elif status == "not started":
            L.append(f"  Other teams' picks: locked away until kickoff ({_kickoff(g.get('start'))}). "
                     "Hidden is NOT the same as not picked.")
    L.append(f"(That is all {len(games)} games.)")
    if espn_all:
        vs, nv = [], []
        for i, g in enumerate(games, 1):
            notes = espn_all.get(g["id"]) or {}
            if notes.get("unavailable") or g.get("status") in ("final", "in_progress", "in", "live"):
                continue
            lv = line_value(notes, g["away"], g["home"], g.get("line"))
            if not lv:
                continue
            if lv.startswith("pool line and ESPN"):
                nv.append(f"game {i} ({g['away']} at {g['home']})")
            else:
                vs.append(f"{lv.split(' is the value side')[0]} (game {i}, "
                          f"{_num(abs(_market_gap(notes, g.get('line'))[1]))} points)")
        L.append("VALUE SIDES in games not started yet, all of them: " + ("; ".join(vs) if vs else "none")
                 + (f". No value side in {', '.join(nv)}." if nv else "."))

    lw = ctx.get("last_week") or {}
    if lw.get("games"):
        L.append("")
        L.append(f"LAST WEEK (week {lw['week']}) FINALS:")
        for g in lw["games"]:
            score = (f"{g['away']} {_num(g['away_pts'])}, {g['home']} {_num(g['home_pts'])}"
                     if g.get("away_pts") is not None else f"{g['away']} at {g['home']} (score missing)")
            mine_lw = (g.get("picks") or {}).get(me)
            you_lw = (f" {me} took {mine_lw} ({'HIT' if same_team(mine_lw, g.get('covered')) else 'MISS'})."
                      if mine_lw else "")
            L.append(f"- {score}. Pool line {pool_line(g['away'], g['home'], g.get('line'))}. "
                     f"Covered: {g.get('covered') or 'unknown'}.{you_lw}")
            sides = collections.defaultdict(list)
            for t, pk in sorted((g.get("picks") or {}).items()):
                sides[pk].append(t)
            if sides:
                L.append("  Picks: " + "; ".join(
                    f"{pk} ({len(ts)}): {', '.join(ts)}" for pk, ts in sides.items()))
        L.append(f"LAST WEEK BY TEAM (week {lw['week']}), each team's own picks:")
        by_team: dict = collections.defaultdict(list)
        for g in lw["games"]:
            for t, pk in (g.get("picks") or {}).items():
                by_team[t].append((pk, same_team(pk, g.get("covered"))))
        order = [r["team"] for r in st] + sorted(set(by_team) - {r["team"] for r in st})
        for t in order:
            if t not in by_team:
                continue
            res = by_team[t]
            hits = sum(ok for _, ok in res)
            L.append(f"- {t} went {hits}-{len(res) - hits}. "
                     f"Hits: {', '.join(pk for pk, ok in res if ok) or 'none'}. "
                     f"Misses: {', '.join(pk for pk, ok in res if not ok) or 'none'}."
                     + ("  <- the user" if t == me else ""))
    else:
        L.append("")
        L.append("LAST WEEK: no data.")

    jp = ctx.get("jackpot") or {}
    if jp.get("pulls_since_pop") is not None:
        last = f"; last popped by {jp['last_pop_team']}" if jp.get("last_pop_team") else ""
        L.append("")
        L.append(f"SLOTS JACKPOT: {jp['pulls_since_pop']} pulls since it last popped{last}.")

    L.append("")
    L.append(f"{me} has {'made' if ctx.get('you_have_picks_this_week') else 'NOT made'} picks this week.")

    if ctx.get("mode") == "picks" and ctx.get("rivals"):
        L.append("")
        L.append("RIVALS (nearest in standings). Probable picks are the pool model's guesses, not real picks:")
        gname = {g["id"]: f"{g['away']} at {g['home']}" for g in games}
        for r in ctx["rivals"]:
            pp = [f"{gname.get(p['game_id'], p['game_id'])}: leans {p['pick']}"
                  + (f" ({round(p['confidence'] * 100)}%)" if p.get("confidence") is not None else "")
                  for p in r.get("probable_picks") or []]
            L.append(f"- {r['team_name']} ({_ordinal(r['rank'])}, {r['record']}): "
                     + ("; ".join(pp) if pp else "no read on their picks"))
    return "\n".join(L)


def focus_block(ctx: dict, question: str) -> str:
    """Facts about the league members named in the latest message ('me',
    'my', 'myself' = the caller), pulled out of the full snapshot and put at
    the very end of the prompt. A 7B model asked to roast TG otherwise
    grabs picks from the wrong row of LAST WEEK BY TEAM."""
    me = (ctx.get("you") or {}).get("team_name")
    names = [r["team"] for r in ctx.get("standings_ytd") or []]
    q = question or ""
    hit = [n for n in names if re.search(rf"(?<!\w){re.escape(n)}(?!\w)", q, re.I)]
    if me and me not in hit and re.search(r"\b(me|my|myself|i)\b", q, re.I) \
            and re.search(r"\broast|\bhow (did|am|'?m) i\b|\bmy (record|week|picks?|season)\b", q, re.I):
        hit.append(me)
    if not hit:
        return ""
    text = render_context(ctx).splitlines()
    out = ["", "FOCUS: the facts below are everything known about the team(s) "
           "named in this question. Use these exact facts, nothing else, for them:"]
    for n in hit[:3]:
        out.append(f"{n} (the user, 'you'):" if n == me else
                   f"{n} (NOT the user; talk about {n} in the third person):")
        for ln in text:
            if re.match(rf"T?\d+(st|nd|rd|th)\. {re.escape(n)}:", ln):
                out.append("  Standings: " + ln.replace("  <- the user", ""))
                st = ctx["standings_ytd"]
                row = next(r for r in st if r["team"] == n)
                mates = [r["team"] for r in st if r["rank"] == row["rank"] and r["team"] != n]
                where = ("first place" if row["rank"] == 1 else
                         "last place" if row["rank"] == st[-1]["rank"] else
                         f"{_ordinal(row['rank'])} place")
                tie = f", tied with {', '.join(mates)}" if mates else ""
                out.append(f"  Place: {where}{tie}, out of {len(st)} teams.")
                wk = (ctx.get("weekly_records") or {}).get(n) or {}
                if wk:
                    out.append(f"  {n}'s own recent weeks: " + "; ".join(
                        f"week {w}: {wk[w][0]} hit{'s' * (wk[w][0] != 1)}, {wk[w][1]} miss{'es' * (wk[w][1] != 1)}"
                        for w in sorted(wk)[-3:]) + ".")
            elif ln.startswith(f"- {n} went "):
                out.append("  Last week: " + ln[2:].replace("  <- the user", ""))
                for g in (ctx.get("last_week") or {}).get("games") or []:
                    pk = (g.get("picks") or {}).get(n)
                    if pk is None:
                        continue
                    ok = same_team(pk, g.get("covered"))
                    out.append(f"    {g['away']} at {g['home']}: {n} took {pk}, "
                               + ("HIT." if ok else f"MISS ({g.get('covered')} covered, not {n}'s pick)."))
        if n == me:
            mine = {p["game_id"]: p["pick"] for p in (ctx.get("revealed_picks_this_week") or {}).get(me, [])}
            gl = [f"{mine[g['id']]}" for g in ctx.get("games_this_week") or [] if g["id"] in mine]
            out.append(f"  This week's picks: {', '.join(gl) if gl else 'none made yet'}.")
        else:
            shown = {p["game_id"] for p in (ctx.get("revealed_picks_this_week") or {}).get(n, [])}
            hidden = [f"{g['away']} at {g['home']} (unlocks {_kickoff(g.get('start'))})"
                      for g in ctx.get("games_this_week") or [] if g["id"] not in shown]
            if hidden:
                out.append(f"  This week's picks: HIDDEN until each kickoff: {'; '.join(hidden)}. "
                           f"If asked, say {n}'s pick unlocks at kickoff and give the time. "
                           "Never guess it, and never say or imply whether they have picked.")
        lw = ctx.get("last_week") or {}
        for g in lw.get("games") or []:
            pk = (g.get("picks") or {}).get(n)
            if pk is None:
                continue
            same = sorted(t for t, p in g["picks"].items() if p == pk and t != n)
            if len(same) <= 2:
                alone = "the ONLY team" if not same else f"one of only {len(same) + 1} teams (with {', '.join(same)})"
                ok = "it covered" if same_team(pk, g.get("covered")) else "it did NOT cover"
                out.append(f"  Last week {n} was {alone} on {pk}, and {ok}.")
    return "\n".join(out)


def _system_prompt(ctx: dict) -> str:
    """Byte-identical for a given user and data snapshot, so Ollama can reuse
    the processed prompt. Anything that changes per message (clock, focus on
    the people named in the question) goes in _user_turn instead."""
    you = ctx.get("you", {}).get("team_name", "you")
    n = len(ctx.get("games_this_week") or [])
    now = _now_ct_naive()
    return (
        "You are Picky, the mascot of a college football pick'em league among "
        "friends: a cocky, savage sports-radio heel who thinks everyone in this "
        "league is an idiot. You roast people, and you call bad picks stupid. "
        "But you ALWAYS answer the question correctly from LEAGUE DATA. "
        "Accuracy beats jokes.\n\n"
        "RULES:\n"
        "1. Answer exactly what was asked, in 40 to 90 words: one opening jab, "
        "then the facts. Game by game: one short line per game. Each answer is "
        "new: never repeat an earlier answer.\n"
        "2. Use only facts written in LEAGUE DATA. Copy names and numbers "
        "exactly. If it isn't there, say you don't have it. Your own memory of "
        "teams and players is out of date: never use it.\n"
        "3. Never invent streaks, trends, habits, injuries, picks or reasons. "
        "There is no money in this league: no dollars, units or bets. "
        "There are no ATS or cover records. 'Last 5' games have no dates, so "
        "never call one of them last week; LAST WEEK means the league's week "
        "under LAST WEEK. Check a claim against the data before you joke about "
        "it: first place is not 'losing', 3-1 is not a bad week. Before "
        "saying 'all' or 'both' about teams, check each one.\n"
        "4. VALUE: use each game's 'Line value' line and each 'Value check' "
        "line as written. Never redo the spread math. A game with 'no line "
        "value' has NO value side: don't name one.\n"
        f"5. Asked about picks or changing picks: cover all {n} games under "
        "YOUR GAMES THIS WEEK, numbered, each side written exactly as on the "
        "POOL LINE (favorite minus, underdog plus). One or two reasons each, "
        "from that game's block.\n"
        "6. FPI is the chance to WIN outright, never to cover.\n"
        "7. INJURIES: no college injury report exists. Say so and send them "
        "to the beat writers. Never say or hint that a team or player is "
        "healthy, fine or good to go.\n"
        "8. Only the games under YOUR GAMES THIS WEEK are in the pool. "
        "Anything else (another game, taxes, anything): roast them in one or "
        "two lines and stop. Don't do it, and don't bring up picks, games or "
        "standings.\n"
        "9. PRIVACY: other teams' picks for a game stay hidden until that game "
        "kicks off. Asked about one: say it unlocks at kickoff and give the "
        "time. Never guess it, and never say whether they have picked.\n"
        "10. Bring up the user's own picks only when the question is about "
        "their picks. Asked what their picks are: list each pick exactly as "
        "made (e.g. 'Wake Forest -1.5') with a short verdict.\n"
        "11. Asked to explain a spread: say in plain words what each side needs "
        "to cover, then the market line and value check.\n"
        "12. Never mention LEAGUE DATA, FOCUS or section names. Light "
        "markdown is fine. No emoji. Never correct yourself mid-answer.\n\n"
        "ROASTS:\n"
        "- Roast picks, records, standings and misses, using real numbers. "
        "Every burn must be literally true.\n"
        "- Asked to roast someone: three short burns from different facts "
        "(place, record, a bad week, a miss), hardest last.\n"
        "- Doing well? Roast them anyway, with grudging respect.\n"
        "- Never: slurs, race, religion, sexuality, gender, disability, looks, "
        "weight, family, jobs, money, health or tragedy.\n\n"
        "COVERING: the favorite (-13.5) must win by 14 or more. The underdog "
        "(+13.5) covers by winning, or losing by 13 or fewer. A covered pick "
        "is a point.\n\n"
        + EXAMPLES
        + (PICKS_MODE_RIDER if ctx.get("mode") == "picks" else LEADERBOARD_MODE_RIDER)
        + "\n\nLEAGUE DATA:\n"
        + render_context(ctx)
        + f"\n\nThe user reading this is \"{you}\": call them 'you'. Everyone "
        "else is 'he' or 'they', by name."
        # to the hour only: a per-minute clock would change the prompt (and
        # cost a full reprocess) on every message
        + f"\nRight now it is {now:%a %b %-d}, between {now:%-I}:00 and {now:%-I}:59 {now:%p} CT."
    )


def _user_turn(ctx: dict, question: str) -> str:
    """A user message plus the facts about the people it names. Applied to
    every user turn, not just the last, so a follow-up's prompt starts with
    the previous prompt and Ollama reuses it."""
    fb = focus_block(ctx, question)
    return f"{fb.strip()}\n\n{question}" if fb else question


# -----------------------------------------------------------------------------
# ollama streaming
# -----------------------------------------------------------------------------


async def _ollama_stream(messages: list[dict]):
    """Yield SSE-framed assistant tokens from Ollama."""
    payload = {"model": MODEL, "messages": messages, "stream": True, "options": OLLAMA_OPTIONS,
               "keep_alive": OLLAMA_FALLBACK_KEEP_ALIVE if CLAUDE else OLLAMA_KEEP_ALIVE}
    if OLLAMA_THINK is not None:
        payload["think"] = OLLAMA_THINK == "true"
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


async def _model_stream(messages: list[dict]):
    """Claude first; on any API failure before the first token, the local
    Ollama model answers instead. `messages[0]` is the system prompt."""
    if CLAUDE:
        started = False
        try:
            async with CLAUDE.messages.stream(
                model=CLAUDE_MODEL,
                max_tokens=4096,
                system=messages[0]["content"],
                messages=messages[1:],
                output_config={"effort": CLAUDE_EFFORT},
            ) as stream:
                async for text in stream.text_stream:
                    started = True
                    yield _sse({"content": text})
                final = await stream.get_final_message()
            if started:
                yield "data: [DONE]\n\n"
                return
            log.warning("claude returned no text (stop_reason=%s); falling back", final.stop_reason)
        except anthropic.APIError as e:
            log.warning("claude failed (%s); %s", type(e).__name__,
                        "mid-stream" if started else "falling back to ollama")
            if started:
                yield _sse({"error": "model_error"})
                return
    async for chunk in _ollama_stream(messages):
        yield chunk


def _sse(obj: dict) -> str:
    return f"data: {json.dumps(obj)}\n\n"


# -----------------------------------------------------------------------------
# image generation: handed to image.py on :8767 (own GPU lock + 20/hr limit)
# -----------------------------------------------------------------------------

# "draw me a bulldog", "make a picture of ...", "generate an image of ..."
_IMAGE_ASK = re.compile(
    r"^\s*(draw|sketch|paint)\b"
    r"|\b(draw|sketch|paint|generate|make|create|render|show)\b.{0,30}?"
    r"\b(image|picture|pic|drawing|painting|illustration|photo|meme|poster|wallpaper)s?\b",
    re.I,
)


def wants_image(text: str) -> bool:
    return bool(_IMAGE_ASK.search(text or ""))


async def _image_stream(prompt: str, user_id: str):
    """SSE: a one-liner, keep-alive pings while the GPU works, then the image
    as a data: URL. The prompt is only the user's own words; no league
    context goes to the image service."""
    yield _sse({"content": "Fine. Firing up the art department. Give me a minute or two..."})
    timeout = httpx.Timeout(connect=5.0, read=300.0, write=10.0, pool=5.0)
    async with httpx.AsyncClient(timeout=timeout) as c:
        # 16 GB box: the chat model (~6.7 GB) and the image model (~6 GB)
        # together swap, and a picture took 4+ min and timed out. Unload the
        # chat model first; the next chat message reloads it.
        try:
            await c.post(f"{OLLAMA}/api/generate", json={"model": MODEL, "keep_alive": 0}, timeout=30.0)
        except httpx.HTTPError as e:
            log.warning("couldn't unload %s before image: %s", MODEL, e)
        task = asyncio.create_task(
            c.post(f"{IMAGE_URL}/generate", json={"prompt": prompt, "user_id": user_id, "width": 768, "height": 768})
        )
        try:
            while not task.done():
                await asyncio.wait({task}, timeout=10)
                if not task.done():
                    yield ": ping\n\n"  # keep the tunnel from idling out
            r = task.result()
        except httpx.HTTPError as e:
            log.warning("image service unreachable: %s", e)
            yield _sse({"content": " Art department's closed. Try me later, Picasso."})
            return
        finally:
            task.cancel()
    if r.status_code == 429:
        yield _sse({"content": " Twenty pictures an hour is the limit and you blew through it. Go outside."})
    elif r.status_code != 200:
        log.error("image %s: %s", r.status_code, r.text[:200])
        yield _sse({"content": " Brush slipped. My fault, for once. Try again in a minute."})
    else:
        yield _sse({"image": "data:image/png;base64," + r.json()["image_base64"]})
    yield "data: [DONE]\n\n"


# -----------------------------------------------------------------------------
# routes
# -----------------------------------------------------------------------------


@app.get("/health")
async def health():
    return {"ok": True, "model": CLAUDE_MODEL if CLAUDE else MODEL,
            "fallback": MODEL if CLAUDE else None}


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
        last = messages[-1] if isinstance(messages[-1], dict) else {}
        if last.get("role") == "user" and wants_image(last.get("content", "")):
            return StreamingResponse(
                _image_stream(last["content"][:2000], user_id),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )
        try:
            ctx = await _build_context(
                client, jwt, user_id, mode=mode, rival_predictions=rival_predictions
            )
        except httpx.HTTPError as e:
            log.warning("supabase fetch failed: %s", e)
            raise HTTPException(status_code=502, detail="supabase_unreachable")
    if not ctx.get("error"):
        ctx["espn"] = await espn.game_notes([g["id"] for g in ctx["games_this_week"]])

    sys_msg = {"role": "system", "content": _system_prompt(ctx)}
    convo = [sys_msg] + [
        {"role": m["role"], "content": m["content"]}
        for m in messages
        if m.get("role") in ("user", "assistant") and m.get("content")
    ]
    convo = [convo[0]] + [
        {"role": "user", "content": _user_turn(ctx, m["content"])} if m["role"] == "user" else m
        for m in convo[1:]
    ]

    return StreamingResponse(
        _model_stream(convo),
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
    if CLAUDE:  # Ollama is only the fallback now; it loads on demand
        return

    async def _go():
        try:
            async with httpx.AsyncClient(timeout=60.0) as c:
                r = await c.post(
                    f"{OLLAMA}/api/chat",
                    json={
                        "model": MODEL,
                        "messages": [{"role": "user", "content": "ready?"}],
                        "stream": False,
                        "options": OLLAMA_OPTIONS,
                        "keep_alive": OLLAMA_KEEP_ALIVE,
                        **({"think": OLLAMA_THINK == "true"} if OLLAMA_THINK is not None else {}),
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
