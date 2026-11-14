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
import re
import time
import unicodedata
import zoneinfo
from typing import Any

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
IMAGE_URL = os.environ.get("PICKY_IMAGE_URL", "http://127.0.0.1:8767")

# Ollama's default num_ctx (4096 here) silently drops the front of a long
# prompt: league context + ten turns of history. 8192 leaves ~2x headroom
# over the biggest prompt measured. Same options on warmup so the model is
# never reloaded at a different size. Low temperature: facts over flair.
OLLAMA_OPTIONS = {"num_ctx": 8192, "temperature": 0.2}

RATE_LIMIT_PER_MIN = 10
MAX_HISTORY_TURNS = 10

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("picky")

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
    "\n\nLEADERBOARD MODE: the user is viewing standings. Keep answers "
    "short and conversational: league gossip and quick stats."
)

ROAST_EXAMPLES = (
    "Roast examples. The teams and numbers here are FAKE and only show the "
    "voice; real roasts use the real numbers from LEAGUE DATA:\n"
    "  Data line: '9th. Gus: 19-25 | weeks: wk8 1-3, wk9 2-2, wk10 0-4'\n"
    "  Roast: 'Gus. Nineteen and twenty-five. A coin flip would be beating you "
    "and it doesn't even watch football. Oh-for-four last week, ninth place, "
    "still sinking. Delete the app, champ.'\n"
    "  Data line: '1st. Rita: 30-14' and last week she was the only one on "
    "Iowa, and Iowa didn't cover.\n"
    "  Roast: 'Rita's up top at 30-14 and still went full lone genius on Iowa "
    "last week. First place can't buy taste, apparently.'"
)


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


def line_value(notes: dict, away: str, home: str, line) -> str | None:
    """Where the ESPN market and the pool line disagree. Both turned into a
    home spread (negative = home favored); a gap means one side of the pool
    line is getting points the market wouldn't give."""
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
    gap = pool_home - mkt_home       # > 0: market likes home more than the pool does
    if abs(gap) < 1:
        return "pool line and ESPN market are within a point, so no line value either way."
    side = home if gap > 0 else away
    fav_is_home = pool_home < 0
    sign = "-" if (side == home) == fav_is_home else "+"
    return (f"{side} {sign}{_num(abs(pool_home))} is the value side: the market rates "
            f"{side} {_num(abs(gap))} points better than the pool line does.")


def _team_line(name: str, t: dict) -> str:
    bits = [f"#{t['ap_rank']} AP" if t.get("ap_rank") else "unranked"]
    if t.get("record"):
        bits.append(f"record {t['record']}")
    out = f"  {name}: " + ", ".join(bits)
    if t.get("last5"):
        out += ". Last 5 (oldest first): " + "; ".join(t["last5"])
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
    L.append(f"Season {ctx.get('season')}, week {ctx.get('current_week')}. "
             f"The user is the team \"{me}\".")

    st = ctx.get("standings_ytd") or []
    L.append("")
    L.append(f"STANDINGS ({len(st)} teams; record = points-missed; points are picks "
             "that covered, the same number as the leaderboard's Points; T = tied):")
    for r in st:
        tied = sum(1 for x in st if x["rank"] == r["rank"]) > 1
        line = f"{'T' if tied else ''}{_ordinal(r['rank'])}. {r['team']}: {r['w']}-{r['l']}"
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
        L.append(f"First place: {_who(st[0]['rank'])}. Last place: {_who(st[-1]['rank'])}.")

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
            # ESPN's win probability is left out on purpose: even labeled
            # "NOT to cover", the 7B model kept quoting it as a cover chance.
            inj_a = (notes.get("away") or {}).get("injuries") or []
            inj_h = (notes.get("home") or {}).get("injuries") or []
            if inj_a or inj_h:
                L.append(f"  Injuries: {away}: {', '.join(inj_a) or 'none listed'}. "
                         f"{home}: {', '.join(inj_h) or 'none listed'}.")
            else:
                L.append("  Injuries: ESPN lists none for either team.")
            heads = notes.get("headlines") or []
            L.append("  Headlines: " + (" | ".join(heads) if heads else "none about these teams."))
        L.append(f"  {me}'s pick: {mine[g['id']]}" if g["id"] in mine else f"  {me}'s pick: not made yet")
        others = {t: p["pick"] for t, plist in revealed.items() if t != me
                  for p in plist if p["game_id"] == g["id"]}
        if others:
            sides = collections.defaultdict(list)
            for t, pk in sorted(others.items()):
                sides[pk].append(t)
            L.append("  Other picks (revealed): " + "; ".join(
                f"{pk}: {', '.join(ts)}" for pk, ts in sides.items()))
        elif status == "not started":
            L.append("  Other teams' picks: hidden until kickoff.")
    L.append(f"(That is all {len(games)} games.)")

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
        out.append(f"{n}{' (the user)' if n == me else ''}:")
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
            hidden = [f"{g['away']} at {g['home']}" for g in ctx.get("games_this_week") or []
                      if g["id"] not in shown]
            if hidden:
                out.append(f"  This week's picks: HIDDEN until kickoff for {', '.join(hidden)}. "
                           "If asked, say: 'Can't say, picks aren't locked in yet for that one.'")
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


def _system_prompt(ctx: dict, question: str = "") -> str:
    you = ctx.get("you", {}).get("team_name", "you")
    n = len(ctx.get("games_this_week") or [])
    return (
        "You are Picky, the trash-talking football mascot of a college football "
        "pick'em league among friends. Voice: a cocky, quick, funny bar-stool "
        "sports guy who has watched every game. Helpful first, funny second. "
        "Keep it tight: usually under 80 words; when asked about every game, "
        "one or two lines per game. Light markdown is fine (**bold**, short "
        f"'- ' lists). The user's team is \"{you}\".\n\n"
        "HOW THE LEAGUE WORKS: every team picks a side in every game in the "
        "pool against the POOL LINE. A pick that covers is a point. Standings "
        "are season points.\n\n"
        "GROUNDING, HARD RULES:\n"
        "1. Every fact you state (records, ranks, spreads, scores, injuries, "
        "players, streaks, headlines, who picked what) must appear in LEAGUE "
        "DATA below. Copy numbers exactly.\n"
        "2. If it isn't in LEAGUE DATA, say you don't have it, in one short "
        "in-character line, e.g. 'Got nothing on that in my notes.' Never guess. "
        "Your own memory of teams, players and seasons is outdated: don't use it.\n"
        "3. Never invent players, injuries, stats, streaks, scores or reasons. "
        "The only players that exist are the ones named in LEAGUE DATA. An "
        "unknown player name: you have nothing on him.\n"
        "4. A game not under YOUR GAMES THIS WEEK is not in the pool: say so "
        "and stop. No line for it, not even a guess like 'around -7'.\n"
        f"5. Asked for picks: cover ALL {n} games under YOUR GAMES THIS WEEK, "
        f"exactly {n}, numbered. For each: your side copied exactly as it is "
        "written in the POOL LINE (e.g. 'Oregon State +13.5'; a favorite is "
        "always minus, an underdog always plus), plus "
        "one reason taken from that game's block (records, last 5, ESPN market "
        "vs pool line, injuries). Only when the block has "
        "nothing that favors either side, say 'coin flip, nothing in the data "
        "separates them' instead of a reason. Never give a reason and call it "
        "a coin flip in the same pick.\n"
        "6. Quote the POOL LINE as the spread. Mention the ESPN market line only "
        "as a comparison (a gap between them is a fair reason).\n"
        "7. Don't mention 'LEAGUE DATA', 'JSON' or 'the context'; just know it.\n\n"
        "PRIVACY, HARD RULE: LEAGUE DATA only has other teams' picks for games "
        "that have kicked off, plus the user's own picks. Asked who someone is "
        "picking in a game whose picks are hidden, answer exactly: 'Can't say, "
        "picks aren't locked in yet for that one.' Never speculate about it. "
        "Never say which teams have or haven't submitted picks; you only know "
        "the user's own status.\n\n"
        "ROAST MODE: when someone asks you to roast a league member (or "
        "themselves), go savage, like the meanest guy in the group chat. Build "
        "it from their real STANDINGS line (rank, record, weekly records) and "
        "real revealed picks from LAST WEEK, and quote those numbers exactly. "
        "Cursing is fine (PG-13 to R). Hard limits: no slurs; nothing about "
        "race, religion, sexuality, gender, disability, looks, family or "
        "tragedy; never reveal hidden picks; no made-up stats.\n"
        + ROAST_EXAMPLES
        + (PICKS_MODE_RIDER if ctx.get("mode") == "picks" else LEADERBOARD_MODE_RIDER)
        + "\n\nLEAGUE DATA:\n"
        + render_context(ctx)
        + focus_block(ctx, question)
    )


# -----------------------------------------------------------------------------
# ollama streaming
# -----------------------------------------------------------------------------


async def _ollama_stream(messages: list[dict]):
    """Yield SSE-framed assistant tokens from Ollama."""
    payload = {"model": MODEL, "messages": messages, "stream": True, "options": OLLAMA_OPTIONS}
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
    yield _sse({"content": "Firing up the art department. Give me a minute or two..."})
    timeout = httpx.Timeout(connect=5.0, read=300.0, write=10.0, pool=5.0)
    async with httpx.AsyncClient(timeout=timeout) as c:
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
            yield _sse({"content": " Art department's closed right now. Try me later."})
            return
        finally:
            task.cancel()
    if r.status_code == 429:
        yield _sse({"content": " You've maxed out the art department, 20 an hour. Cool it for a bit."})
    elif r.status_code != 200:
        log.error("image %s: %s", r.status_code, r.text[:200])
        yield _sse({"content": " Brush slipped. Try again in a minute."})
    else:
        yield _sse({"image": "data:image/png;base64," + r.json()["image_base64"]})
    yield "data: [DONE]\n\n"


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

    last_q = last.get("content", "") if last.get("role") == "user" else ""
    sys_msg = {"role": "system", "content": _system_prompt(ctx, last_q)}
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
                        "options": OLLAMA_OPTIONS,
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
