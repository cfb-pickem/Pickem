"""
Fact packs for the genius exam: everything a sharp would check before a pick,
per game, from free sources (read-only):

  - the CFBD local cache (cfbd.sqlite; never the CFBD API): records, SP+,
    season ATS, advanced stats, every 2026 game + line (for ATS splits, rest,
    lookahead) and 2025 lines (last year's meeting)
  - ESPN's free summary endpoint: current + opening spread/total (DraftKings),
    FPI matchup predictor, venue, kickoff, ATS records ESPN shows
  - ESPN rankings (AP / CFP)
  - Open-Meteo forecast at the venue for kickoff hour (free, no key)

    cd server && ./.venv/bin/python3 -I eval/genius/facts.py 401856754 ... > out.json

The answer keys in questions.json are written from these packs plus cited
public news; checks.py re-derives the numbers it grades from the same packs.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

SERVER = Path(__file__).resolve().parents[2]
DB = SERVER / "cache" / "cfbd.sqlite"
CT = ZoneInfo("America/Chicago")
ESPN_SUMMARY = "https://site.api.espn.com/apis/site/v2/sports/football/college-football/summary?event={}"
ESPN_RANKINGS = "https://site.api.espn.com/apis/site/v2/sports/football/college-football/rankings"
METEO = ("https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
         "&hourly=temperature_2m,precipitation_probability,wind_speed_10m,wind_gusts_10m"
         "&temperature_unit=fahrenheit&wind_speed_unit=mph&timezone=UTC&start_date={d}&end_date={d}")


def _ds(db, name):
    row = db.execute("select json from datasets where name=?", (name,)).fetchone()
    return json.loads(row[0]) if row else []


def _line(lines: list[dict]) -> dict | None:
    """Consensus-ish: DraftKings, else the first provider with a spread."""
    ls = [x for x in lines or [] if x.get("spread") is not None]
    return next((x for x in ls if x.get("provider") == "DraftKings"), ls[0] if ls else None)


def cover(home_pts, away_pts, spread) -> float | None:
    """Home cover margin (spread is home-relative, negative = home favored)."""
    if home_pts is None or away_pts is None or spread is None:
        return None
    return home_pts - away_pts + spread


def team_games(team: str, games: list[dict], lines: dict, upto_week: int) -> list[dict]:
    out = []
    for g in sorted(games, key=lambda g: g["startDate"]):
        if team not in (g["homeTeam"], g["awayTeam"]) or g.get("week", 99) >= upto_week:
            continue
        if g.get("seasonType") != "regular":
            continue
        home = g["homeTeam"] == team
        ln = _line(lines.get(g["id"], {}).get("lines"))
        sp = ln["spread"] if ln else None
        hc = cover(g.get("homePoints"), g.get("awayPoints"), sp)
        my_spread = (sp if home else -sp) if sp is not None else None
        my_cover = (hc if home else -hc) if hc is not None else None
        pts = (g.get("homePoints"), g.get("awayPoints")) if home else (g.get("awayPoints"), g.get("homePoints"))
        out.append({"week": g["week"], "date": g["startDate"][:10], "opp": g["awayTeam"] if home else g["homeTeam"],
                    "site": "N" if g.get("neutralSite") else ("H" if home else "A"), "score": pts,
                    "su": None if pts[0] is None else ("W" if pts[0] > pts[1] else "L"),
                    "spread": my_spread, "cover_margin": my_cover,
                    "ats": None if my_cover is None else ("push" if my_cover == 0 else "C" if my_cover > 0 else "L")})
    return out


def ats_splits(rows: list[dict]) -> dict:
    def rec(f):
        r = [x for x in rows if x["ats"] and f(x)]
        return f"{sum(x['ats'] == 'C' for x in r)}-{sum(x['ats'] == 'L' for x in r)}" + (
            f"-{sum(x['ats'] == 'push' for x in r)}" if any(x["ats"] == "push" for x in r) else "")
    last3 = [x for x in rows if x["ats"]][-3:]
    return {"all": rec(lambda x: True), "fav": rec(lambda x: x["spread"] < 0), "dog": rec(lambda x: x["spread"] > 0),
            "home": rec(lambda x: x["site"] == "H"), "road": rec(lambda x: x["site"] == "A"),
            "last3": "".join(x["ats"][0] for x in last3)}


def adv_summary(a: dict | None) -> dict:
    if not a:
        return {}
    o, d = a["offense"], a["defense"]
    r = lambda v: round(v, 3) if isinstance(v, (int, float)) else v  # noqa: E731
    return {"off_ppa": r(o["ppa"]), "off_success": r(o["successRate"]), "off_explosiveness": r(o["explosiveness"]),
            "off_rush_ppa": r(o["rushingPlays"]["ppa"]), "off_pass_ppa": r(o["passingPlays"]["ppa"]),
            "off_line_yards": r(o["lineYards"]), "off_stuff_rate": r(o["stuffRate"]),
            "off_pts_per_opp": r(o["pointsPerOpportunity"]), "off_havoc_allowed": r(o["havoc"]["total"]),
            "off_rush_rate": r(o["rushingPlays"]["rate"]),
            "def_ppa": r(d["ppa"]), "def_success": r(d["successRate"]), "def_explosiveness": r(d["explosiveness"]),
            "def_rush_ppa": r(d["rushingPlays"]["ppa"]), "def_pass_ppa": r(d["passingPlays"]["ppa"]),
            "def_line_yards": r(d["lineYards"]), "def_stuff_rate": r(d["stuffRate"]),
            "def_pts_per_opp": r(d["pointsPerOpportunity"]), "def_havoc": r(d["havoc"]["total"]),
            "off_plays_per_game": None}


def espn_odds(summary: dict) -> dict:
    pc = (summary.get("pickcenter") or [{}])[0]
    ps = pc.get("pointSpread") or {}
    tot = pc.get("total") or {}
    comp = summary["header"]["competitions"][0]
    side = {c["homeAway"]: c for c in comp["competitors"]}
    pred = summary.get("predictor") or {}
    return {"provider": (pc.get("provider") or {}).get("name"), "details": pc.get("details"),
            "home_spread_now": (ps.get("home") or {}).get("close", {}).get("line"),
            "home_spread_open": (ps.get("home") or {}).get("open", {}).get("line"),
            "total_now": pc.get("overUnder"), "total_open": ((tot.get("over") or {}).get("open") or {}).get("line"),
            "ml_home": (pc.get("homeTeamOdds") or {}).get("moneyLine"),
            "ml_away": (pc.get("awayTeamOdds") or {}).get("moneyLine"),
            "fav_at_open_home": (pc.get("homeTeamOdds") or {}).get("favoriteAtOpen"),
            "fpi_home_win_pct": (pred.get("homeTeam") or {}).get("gameProjection"),
            "kickoff_utc": comp.get("date"),
            "broadcast": ", ".join(b.get("media", {}).get("shortName", "") for b in summary.get("broadcasts") or []),
            "records": {k: {r["type"]: r.get("summary") for r in v.get("record") or []} for k, v in side.items()},
            "ranks": {k: v.get("curatedRank", {}).get("current") for k, v in side.items()},
            "venue": (summary.get("gameInfo") or {}).get("venue", {}).get("fullName"),
            "grass": (summary.get("gameInfo") or {}).get("venue", {}).get("grass")}


def weather(lat, lon, kickoff_utc: str) -> dict | None:
    try:
        k = dt.datetime.fromisoformat(kickoff_utc.replace("Z", "+00:00"))
        r = httpx.get(METEO.format(lat=lat, lon=lon, d=k.date().isoformat()), timeout=20).json()
        h = r["hourly"]
        i = min(range(len(h["time"])), key=lambda j: abs(
            dt.datetime.fromisoformat(h["time"][j] + "+00:00") - k))
        return {"temp_f": h["temperature_2m"][i], "precip_pct": h["precipitation_probability"][i],
                "wind_mph": h["wind_speed_10m"][i], "gust_mph": h["wind_gusts_10m"][i],
                "as_of": dt.datetime.now(CT).strftime("%Y-%m-%d %H:%M CT"), "source": "open-meteo forecast"}
    except Exception as e:  # noqa: BLE001
        return {"error": repr(e)[:120]}


def rankings() -> dict:
    out = {}
    try:
        d = httpx.get(ESPN_RANKINGS, timeout=20).json()
        for poll in d.get("rankings") or []:
            out[poll.get("shortName") or poll.get("name")] = {
                "date": poll.get("date"),
                "ranks": [(r["current"], r["team"].get("location") or r["team"].get("name"), r.get("recordSummary"))
                          for r in poll.get("ranks") or []]}
    except Exception as e:  # noqa: BLE001
        out["error"] = repr(e)[:120]
    return out


def build(ids: list[int]) -> dict:
    db = sqlite3.connect(DB)
    games = _ds(db, "games")
    lines = {g["id"]: g for g in _ds(db, "lines")}
    prev = _ds(db, "lines_prev")
    recs = {r["team"]: r for r in _ds(db, "records")}
    sp = {r["team"]: r for r in _ds(db, "sp")}
    ats = {r["team"]: r for r in _ds(db, "ats")}
    adv = {r["team"]: r for r in _ds(db, "advanced")}
    venues = {v["id"]: v for v in _ds(db, "venues")}
    fetched = {n: dt.datetime.fromtimestamp(t, CT).strftime("%Y-%m-%d %H:%M CT")
               for n, t in db.execute("select name, fetched_at from datasets")}
    by_id = {g["id"]: g for g in games}
    out = {"as_of": dt.datetime.now(CT).strftime("%Y-%m-%d %H:%M CT"), "cfbd_fetched": fetched,
           "rankings": rankings(), "games": {}}
    for gid in ids:
        g = by_id[gid]
        wk = g["week"]
        s = httpx.get(ESPN_SUMMARY.format(gid), timeout=30).json()
        v = venues.get(g.get("venueId")) or {}
        teams = {}
        for side in ("away", "home"):
            t = g[f"{side}Team"]
            rows = team_games(t, games, lines, wk)
            nxt = next((x for x in sorted(games, key=lambda x: x["startDate"])
                        if x.get("week") == wk + 1 and t in (x["homeTeam"], x["awayTeam"])), None)
            last = rows[-1] if rows else None
            rest = (dt.date.fromisoformat(g["startDate"][:10]) - dt.date.fromisoformat(last["date"])).days if last else None
            r = recs.get(t) or {}
            teams[side] = {
                "team": t, "conference": g[f"{side}Conference"],
                "record": f"{r.get('total', {}).get('wins')}-{r.get('total', {}).get('losses')}" if r else None,
                "conf_record": f"{r.get('conferenceGames', {}).get('wins')}-{r.get('conferenceGames', {}).get('losses')}" if r else None,
                "home_record": f"{r.get('homeGames', {}).get('wins')}-{r.get('homeGames', {}).get('losses')}" if r else None,
                "road_record": f"{r.get('awayGames', {}).get('wins')}-{r.get('awayGames', {}).get('losses')}" if r else None,
                "sp_rating": (sp.get(t) or {}).get("rating"), "sp_rank": (sp.get(t) or {}).get("ranking"),
                "sp_off": ((sp.get(t) or {}).get("offense") or {}).get("rating"),
                "sp_off_rank": ((sp.get(t) or {}).get("offense") or {}).get("ranking"),
                "sp_def": ((sp.get(t) or {}).get("defense") or {}).get("rating"),
                "sp_def_rank": ((sp.get(t) or {}).get("defense") or {}).get("ranking"),
                "cfbd_ats": ats.get(t) and f"{ats[t]['atsWins']}-{ats[t]['atsLosses']}-{ats[t]['atsPushes']}",
                "cfbd_avg_cover": ats.get(t) and ats[t].get("avgCoverMargin"),
                "ats_splits_computed": ats_splits(rows),
                "games": rows, "days_rest": rest, "off_bye": bool(rest and rest >= 13),
                "next_week": nxt and (f"vs {nxt['awayTeam']}" if nxt["homeTeam"] == t else f"at {nxt['homeTeam']}"),
                "advanced": adv_summary(adv.get(t)),
            }
        h2h = [x for x in prev if {x["homeTeam"], x["awayTeam"]} == {g["homeTeam"], g["awayTeam"]}]
        h2h_rows = []
        for x in h2h:
            ln = _line(x.get("lines"))
            h2h_rows.append({"season": x["season"], "week": x["week"], "home": x["homeTeam"], "away": x["awayTeam"],
                             "score": f"{x['awayTeam']} {x.get('awayScore')}, {x['homeTeam']} {x.get('homeScore')}",
                             "home_spread": ln and ln["spread"],
                             "home_cover_margin": cover(x.get("homeScore"), x.get("awayScore"), ln and ln["spread"])})
        cl = lines.get(gid, {}).get("lines") or []
        o = espn_odds(s)
        out["games"][str(gid)] = {
            "id": gid, "week": wk, "away": g["awayTeam"], "home": g["homeTeam"], "neutral": g.get("neutralSite"),
            "conference_game": g.get("conferenceGame"),
            "kickoff_ct": dt.datetime.fromisoformat(g["startDate"].replace("Z", "+00:00")).astimezone(CT).strftime("%a %b %d %I:%M %p CT"),
            "venue": {"name": g.get("venue"), "city": v.get("city"), "state": v.get("state"),
                      "elevation_m": v.get("elevation"), "dome": v.get("dome"), "grass": v.get("grass"),
                      "timezone": v.get("timezone")},
            "espn": o, "cfbd_lines": [{k: x.get(k) for k in ("provider", "spread", "spreadOpen", "overUnder", "overUnderOpen")}
                                      for x in cl],
            "h2h_2025": h2h_rows, "teams": teams,
            "weather": weather(v.get("latitude"), v.get("longitude"), o["kickoff_utc"] or g["startDate"])
            if v.get("latitude") and not v.get("dome") else None,
            "espn_news": [a.get("headline") for a in (s.get("news") or {}).get("articles", [])][:6],
        }
    return out


if __name__ == "__main__":
    print(json.dumps(build([int(x) for x in sys.argv[1:]]), indent=1, default=str))
