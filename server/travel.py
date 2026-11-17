"""
Static venue table + travel facts for any FBS game, from the CFBD cache only
('venues': lat/lon, elevation in METERS, IANA timezone, dome; 'games': venueId,
neutralSite). No API calls.

  home_venue(team)            -> the team's home stadium (most common non-neutral home venue)
  game_venue(game_id, home)   -> the game's stadium (games.venueId), else the home team's
  trip(team, venue, kick_utc) -> miles, time zones crossed (zoneinfo offsets at kickoff, + = east),
                                 kickoff local + on the team's body clock
  travel_line(g) / altitude_line(g)  -> decided words for a GAME CARD
"""

from __future__ import annotations

import collections
import datetime as dt
import json
import math
import sqlite3
from zoneinfo import ZoneInfo

import cfbd

FT_PER_M = 3.28084
HIGH_FT = 4000        # altitude only matters when the game venue is this high ...
LOW_FT = 2000         # ... and the visitor lives below this
_MEMO: dict = {}


def _ds(name: str):
    try:
        db = sqlite3.connect(f"file:{cfbd.DB_PATH}?mode=ro", uri=True)
        row = db.execute("select fetched_at, json from datasets where name=?", (name,)).fetchone()
        db.close()
    except sqlite3.Error:
        return None, []
    return (row[0], json.loads(row[1])) if row else (None, [])


def tables() -> dict:
    """{'venues': {id: row}, 'home': {norm team: venue row}, 'games': {game id: row}}, memoized per refresh."""
    sv, venues = _ds("venues")
    sg, games = _ds("games")
    if _MEMO.get("key") == (sv, sg) and sv is not None:
        return _MEMO["t"]
    vby = {v.get("id"): v for v in venues or []}
    counts: dict = collections.defaultdict(collections.Counter)
    for g in games or []:
        if not g.get("neutralSite") and g.get("venueId") in vby:
            counts[cfbd.norm(g.get("homeTeam"))][g["venueId"]] += 1
    home = {t: vby[c.most_common(1)[0][0]] for t, c in counts.items() if c}
    t = {"venues": vby, "home": home, "games": {g.get("id"): g for g in games or []}}
    _MEMO.update({"key": (sv, sg), "t": t})
    return t


def home_venue(team: str, t: dict | None = None) -> dict | None:
    t = t or tables()
    n = cfbd.norm(team)
    return t["home"].get(n) or t["home"].get(cfbd.ALIASES.get(n, "~"))


def game_venue(game_id, home: str, t: dict | None = None) -> tuple[dict | None, bool]:
    """(venue row, neutral site?) for a game: games.venueId, else the home team's stadium."""
    t = t or tables()
    g = t["games"].get(game_id) or t["games"].get(int(game_id)) if str(game_id).isdigit() else None
    if g and g.get("venueId") in t["venues"]:
        return t["venues"][g["venueId"]], bool(g.get("neutralSite"))
    return home_venue(home, t), bool((g or {}).get("neutralSite"))


def elev_ft(v: dict | None) -> int | None:
    try:
        return int(round(float(v["elevation"]) * FT_PER_M))
    except (TypeError, KeyError, ValueError):
        return None


def miles(a: dict, b: dict) -> float | None:
    try:
        la1, lo1, la2, lo2 = (math.radians(float(x)) for x in (a["latitude"], a["longitude"], b["latitude"], b["longitude"]))
    except (TypeError, KeyError, ValueError):
        return None
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 3959 * 2 * math.asin(math.sqrt(h))


def _zone(v: dict | None) -> ZoneInfo | None:
    try:
        return ZoneInfo(v["timezone"]) if v and v.get("timezone") else None
    except Exception:  # noqa: BLE001 - unknown zone name
        return None


ZONE_NAME = {-10: "Hawaii", -9: "Alaska", -8: "Pacific", -7: "Mountain", -6: "Central", -5: "Eastern"}


def trip(team: str, venue: dict | None, kick_utc: dt.datetime | None, t: dict | None = None) -> dict | None:
    """One team's trip to a venue: miles, zones (+ = flying east), kickoff local / on its body clock."""
    hv = home_venue(team, t)
    if not hv or not venue or not kick_utc:
        return None
    zh, zg = _zone(hv), _zone(venue)
    out = {"team": team, "home": hv, "miles": miles(hv, venue), "same_venue": hv.get("id") == venue.get("id")}
    if zh and zg:
        oh, og = kick_utc.astimezone(zh).utcoffset(), kick_utc.astimezone(zg).utcoffset()
        out["zones"] = int(round((og - oh).total_seconds() / 3600))
        out["kick_local"] = kick_utc.astimezone(zg)
        out["kick_body"] = kick_utc.astimezone(zh)
        out["home_zone"] = ZONE_NAME.get(_std_hours(kick_utc, zh))
        out["venue_zone"] = ZONE_NAME.get(_std_hours(kick_utc, zg))
    return out


def _std_hours(when: dt.datetime, z: ZoneInfo) -> int:
    x = when.astimezone(z)
    return int((x.utcoffset() - (x.dst() or dt.timedelta(0))).total_seconds() // 3600)


def _clock(x: dt.datetime) -> str:
    return x.strftime("%-I:%M %p").replace(":00 ", " ")


def body_clock_verdict(tr: dict) -> str:
    z, body = tr.get("zones"), tr.get("kick_body")
    if z is None or body is None:
        return "no time-zone data"
    h = body.hour + body.minute / 60
    if z >= 2 and h < 11:
        return "an early body-clock kick for a team flying east: the classic body-clock spot, a real edge for the home side"
    if z >= 2 and h < 13:
        return "early-ish on their body clock after flying east: a mild edge at most"
    if z <= -2 and h >= 22:
        return "a late kick on their body clock after flying west: a mild edge for the home side"
    if abs(z) >= 1:
        return "no real body-clock issue at that kick time"
    return "same time zone, no body-clock issue"


def travel_line(g: dict, t: dict | None = None) -> str | None:
    """'Travel: UCLA flies about 1,980 miles, 2 time zones east (Pacific to Eastern); kickoff 3:30 PM local =
    12:30 PM on UCLA's body clock: early-ish ... '. Neutral sites: both teams."""
    t = t or tables()
    away, home = g["awayTeam"], g["homeTeam"]
    venue, neutral = game_venue(g.get("id"), home, t)
    kick = _kick(g)
    bits = []
    for team in ((away, home) if neutral else (away,)):
        tr = trip(team, venue, kick, t)
        if not tr or tr.get("same_venue"):
            continue
        s = f"{team} flies about {int(round((tr['miles'] or 0), -1)):,} miles"
        z = tr.get("zones")
        if z:
            s += (f", {abs(z)} time zone{'s' if abs(z) > 1 else ''} {'east' if z > 0 else 'west'}"
                  + (f" ({tr['home_zone']} to {tr['venue_zone']})" if tr.get("home_zone") and tr.get("venue_zone") else ""))
        if tr.get("kick_local") and z:
            s += (f"; kickoff {_clock(tr['kick_local'])} local = {_clock(tr['kick_body'])} on {_poss(team)} body clock")
        s += f": {body_clock_verdict(tr)}"
        bits.append(s)
    if not bits:
        return None
    return "  Travel: " + "; ".join(bits) + ("" if not neutral else " (neutral site)")


def altitude_line(g: dict, t: dict | None = None) -> str | None:
    t = t or tables()
    away, home = g["awayTeam"], g["homeTeam"]
    venue, neutral = game_venue(g.get("id"), home, t)
    fg = elev_ft(venue)
    if fg is None:
        return None
    visitors = (away, home) if neutral else (away,)
    lows = [(team, elev_ft(home_venue(team, t))) for team in visitors]
    where = (venue or {}).get("name") or "the stadium"
    s = f"  Altitude: {where} sits at {fg:,} ft"
    s += "".join(f"; {_poss(team)} home field is at {f:,} ft" for team, f in lows if f is not None)
    if not neutral:
        hf = elev_ft(home_venue(home, t))
        if hf is not None and (home_venue(home, t) or {}).get("id") != (venue or {}).get("id"):
            s += f"; {_poss(home)} home field is at {hf:,} ft"
    if fg < HIGH_FT:
        return s + f": no altitude factor (under {HIGH_FT:,} ft)"
    low = [team for team, f in lows if f is not None and f < LOW_FT]
    mid = [team for team, f in lows if f is not None and LOW_FT <= f < HIGH_FT]
    if low:
        return s + f": a real altitude edge for {home} ({', '.join(low)} lives near sea level)"
    if mid:
        return s + f": some altitude edge for {home} ({', '.join(mid)} lives below {HIGH_FT:,} ft)"
    return s + f": altitude edge muted, {', '.join(t_ for t_, _ in lows)} lives at altitude too"


def _poss(name: str) -> str:
    return name + ("'" if name.endswith("s") else "'s")


def _kick(g: dict) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(str(g.get("startDate")).replace("Z", "+00:00"))
    except ValueError:
        return None
