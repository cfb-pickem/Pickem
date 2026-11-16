"""
ESPN game notes for Picky: market line, records, last five, season team
stats, stat leaders, ESPN's FPI matchup projection, weather, injuries (when
ESPN has any; it publishes none for college) and team-tagged headlines for
the games in this week's pool.

Public, keyless JSON (the same site.api.espn.com the Supabase edge functions
use). Cached ~10 minutes per event. Fails soft: any error for a game becomes
{"unavailable": True} and Picky is told the data is missing, never left to
fill the gap himself.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time

import httpx

import freshness

log = logging.getLogger("picky.espn")

SUMMARY = os.environ.get("PICKY_ESPN_SUMMARY") or (  # replays point this at their fake ESPN (never set live)
    "https://site.api.espn.com/apis/site/v2/sports/football/college-football/"
    "summary?event="
)
# ESPN 403s some bare clients; ordinary browser headers are served.
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
}
TTL_S = 600
MAX_HEADLINES = 3
MAX_INJURIES = 6
LEADER_CATS = ("Passing Yards", "Rushing Yards", "Receiving Yards", "Sacks")

_cache: dict[int, tuple[float, dict]] = {}


def _team_notes(comp: dict, last5: dict, injuries: dict, stats: dict, leaders: dict) -> dict:
    t = comp.get("team") or {}
    tid = str(t.get("id") or "")
    rec = next(
        (r.get("summary") for r in comp.get("record") or [] if r.get("type") == "total"),
        None,
    )
    rank = comp.get("rank")
    games = []
    for e in (last5.get(tid) or [])[-5:]:
        opp = (e.get("opponent") or {}).get("displayName") or "?"
        games.append(f"{e.get('gameResult', '?')} {e.get('score', '')} {e.get('atVs', 'vs')} {opp}")
    inj = []
    for p in (injuries.get(tid) or [])[:MAX_INJURIES]:
        ath = (p.get("athlete") or {})
        pos = ((ath.get("position") or {}).get("abbreviation")) or ""
        inj.append(f"{ath.get('displayName', '?')} {pos} ({p.get('status', '?')})".replace("  ", " "))
    return {
        "name": t.get("displayName") or t.get("location"),
        "abbr": t.get("abbreviation"),
        "ap_rank": int(rank) if str(rank or "").isdigit() and int(rank) <= 25 else None,
        "record": rec,
        "last5": games,
        "injuries": inj,
        # season per-game averages, e.g. "Points Per Game 43.3"
        "stats": [f"{s.get('label')} {s.get('displayValue')}"
                  for s in stats.get(tid) or [] if s.get("label") and s.get("displayValue")],
        # season leaders, e.g. "Passing Yards: Gio Lopez (153/228, 2,176 YDS, 18 TD, 1 INT)"
        "leaders": [
            f"{c.get('displayName')}: {((c['leaders'][0].get('athlete') or {}).get('displayName'))} "
            f"({c['leaders'][0].get('displayValue')})"
            for c in leaders.get(tid) or []
            if c.get("leaders") and c.get("displayName") in LEADER_CATS
        ],
    }


def parse_summary(d: dict) -> dict:
    """Shrink a ~100KB summary payload to the few facts Picky needs."""
    comp = ((d.get("header") or {}).get("competitions") or [{}])[0]
    competitors = {c.get("homeAway"): c for c in comp.get("competitors") or []}
    last5 = {
        str((x.get("team") or {}).get("id")): x.get("events") or []
        for x in d.get("lastFiveGames") or []
    }
    injuries = {
        str((x.get("team") or {}).get("id")): x.get("injuries") or []
        for x in d.get("injuries") or []
    }
    stats = {
        str((x.get("team") or {}).get("id")): x.get("statistics") or []
        for x in (d.get("boxscore") or {}).get("teams") or []
    }
    leaders = {
        str((x.get("team") or {}).get("id")): x.get("leaders") or []
        for x in d.get("leaders") or []
    }
    home = _team_notes(competitors.get("home") or {}, last5, injuries, stats, leaders)
    away = _team_notes(competitors.get("away") or {}, last5, injuries, stats, leaders)

    w = (d.get("gameInfo") or {}).get("weather") or {}
    weather = None
    if w.get("temperature") is not None:
        weather = f"{w['temperature']}F"
        if w.get("gust"):
            weather += f", wind gusts {w['gust']} mph"
        if w.get("precipitation") is not None:
            weather += f", {w['precipitation']}% chance of precipitation"

    market = None
    pc = (d.get("pickcenter") or [None])[0]
    if pc:
        hs = ((pc.get("pointSpread") or {}).get("home")) or {}
        market = {
            "book": (pc.get("provider") or {}).get("name"),
            "details": pc.get("details"),  # e.g. "BOIS -16.5"
            "over_under": pc.get("overUnder"),
            "away_ml": (pc.get("awayTeamOdds") or {}).get("moneyLine"),
            "home_ml": (pc.get("homeTeamOdds") or {}).get("moneyLine"),
            # home spread at open and now (negative = home favored), for line movement
            "home_open": _spread((hs.get("open") or {}).get("line")),
            "home_now": _spread((hs.get("close") or {}).get("line")),
        }

    v = (d.get("gameInfo") or {}).get("venue") or {}
    venue = {"id": v.get("id"), "name": v.get("fullName"),
             "city": (v.get("address") or {}).get("city"),
             "indoor": v.get("indoor")} if v.get("fullName") else None

    pred = d.get("predictor") or {}
    win_prob = None
    try:
        win_prob = {
            "home": float(pred["homeTeam"]["gameProjection"]),
            "away": float(pred["awayTeam"]["gameProjection"]),
        }
    except (KeyError, TypeError, ValueError):
        pass

    # Summary news is league-wide; keep only stories tagged with one of these
    # two teams so Picky never quotes an unrelated headline as game news.
    ids = {
        str(((competitors.get(s) or {}).get("team") or {}).get("id"))
        for s in ("home", "away")
    }
    heads = []
    for a in (d.get("news") or {}).get("articles") or []:
        tagged = {
            str(c.get("teamId"))
            for c in a.get("categories") or []
            if c.get("type") == "team"
        }
        if tagged & ids and a.get("headline"):
            heads.append(a["headline"])
        if len(heads) >= MAX_HEADLINES:
            break

    return {
        "home": home,
        "away": away,
        "market": market,
        "espn_win_prob": win_prob,
        "weather": weather,
        "venue": venue,
        "headlines": heads,
        "injury_report_available": bool(d.get("injuries")),
    }


def _spread(v) -> float | None:
    """'-19.5' / '+3' / 'PK' / 'EVEN' -> float, else None."""
    t = str(v or "").strip().upper()
    if t in ("PK", "EVEN", "PICK"):
        return 0.0
    try:
        return float(t)
    except ValueError:
        return None


async def _fetch_one(client: httpx.AsyncClient, event_id: int) -> dict:
    hit = _cache.get(event_id)
    if hit and time.time() - hit[0] < min(TTL_S, freshness.max_age("lines") / 2):  # 5 min on game days
        return hit[1]
    try:
        r = await client.get(f"{SUMMARY}{event_id}", headers=HEADERS, timeout=6.0)
        r.raise_for_status()
        notes = parse_summary(r.json())
    except Exception as e:  # noqa: BLE001 - fail soft on anything ESPN does
        log.warning("espn summary %s failed: %s", event_id, e)
        # a stale copy only inside the lines' hard limit, and flagged so chat
        # says "as of" instead of "now" (freshness.py)
        if hit and freshness.check("lines", hit[0], refresh=False).use:
            return dict(hit[1], stale_at=hit[0])
        return {"unavailable": True}
    _cache[event_id] = (time.time(), notes)
    return notes


async def game_notes(event_ids: list[int]) -> dict[int, dict]:
    """{event_id: notes} for every id; never raises."""
    if not event_ids:
        return {}
    async with httpx.AsyncClient() as c:
        res = await asyncio.gather(
            *(_fetch_one(c, i) for i in event_ids), return_exceptions=True
        )
    return {
        i: (r if isinstance(r, dict) else {"unavailable": True})
        for i, r in zip(event_ids, res)
    }
