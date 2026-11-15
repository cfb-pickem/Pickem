"""
Deterministic edge signals for Picky's matchup breakdown (chat.matchup_signals).
No LLM anywhere in here: every number is computed, the model only explains.

Sources (all free, keyless, or already held):
  - line movement: ESPN pickcenter open vs now (espn.py), CFBD spreadOpen vs
    spread from the daily cache as the fallback;
  - efficiency: CFBD advanced season stats (PPA per play, success rate,
    explosiveness, havoc), from the daily cache, never the API;
  - public betting splits: DraftKings Network's public splits page (server-
    rendered HTML, no login, no key). Optional: any failure = no split signal;
  - weather: Open-Meteo forecast (keyless) at the stadium's lat/lon from the
    CFBD venues cache, only when ESPN has no forecast for the game.

WEIGHTS (a signal's weight goes to the side it favors; the side with more
total weight is the lean; equal = no lean). Market-vs-pool is the real edge
in a pick'em (Picky's own auto-picks take any 1+ point gap outright), so a
gap of 1.5+ points outweighs every power rating combined. The rating family
(SP+, efficiency, unit matchups, FPI) all price the same thing, so each
stays small:

  market vs pool line   3 (gap 0.5-1.4)  or  6 (gap 1.5+)
  SP+ projection        2
  PPA efficiency        1
  unit matchups         1   (3+ more clear rank edges on one side)
  ESPN FPI              1
  line movement         1   (open -> now moved 1.5+ points)
  betting splits        1   (money % beats ticket % by 15+ on one side)
  weather               0.5 (wind/rain at kickoff -> the side getting points)

Strength: "strong" when the lean side has 4+ and the other side at most a
third of that; otherwise "lean". Dossier/insider intel never feeds this.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import html as htmllib
import logging
import re
import time

import httpx

import cfbd

log = logging.getLogger("picky.edge")

W_MARKET, W_MARKET_BIG, MARKET_BIG_GAP = 3, 6, 1.5
W_SP, W_EFF, W_UNITS, W_FPI, W_MOVE, W_SPLITS, W_WEATHER = 2, 1, 1, 1, 1, 1, 0.5

HOME_FIELD = 2.5          # points, same as chat.SP_HOME_FIELD / picky_picks.HOME_FIELD
PLAYS_PER_GAME = 68       # per team, roughly the FBS average; turns PPA/play into points
MOVE_MIN = 1.5            # points of open->now movement that count as a signal
UNIT_GAP = 30             # rank gap (of ~136 FBS) that counts as a clear unit edge
UNIT_EDGE_MIN = 3         # one side needs this many more clear edges for the signal
SPLIT_GAP = 15            # money% minus tickets% on one side
WIND_MPH, GUST_MPH, RAIN_PCT = 15, 25, 70

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36",
    "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
}


def _n(x: float) -> str:
    x = round(float(x), 1)
    return str(int(x)) if x == int(x) else str(x)


# -----------------------------------------------------------------------------
# line movement
# -----------------------------------------------------------------------------

def line_move(notes: dict | None, away: str, home: str, idx: dict | None) -> dict | None:
    """{'open': home spread at open, 'now': home spread now, 'src': ...}.
    ESPN first (refreshed every 10 min), CFBD's daily copy as the fallback."""
    m = (notes or {}).get("market") or {}
    if m.get("home_open") is not None and m.get("home_now") is not None:
        return {"open": float(m["home_open"]), "now": float(m["home_now"]), "src": m.get("book") or "ESPN"}
    if not idx:
        return None
    h, a = cfbd.resolve_team(home, idx), cfbd.resolve_team(away, idx)
    if not h or not a:
        return None
    games = [g for g in h.get("games") or [] if cfbd.norm(g.get("awayTeam")) == cfbd.norm(a["name"])
             and g.get("homeScore") is None]
    if not games:
        return None
    g = max(games, key=lambda g: g.get("startDate") or "")
    ln = next((x for x in g.get("lines") or [] if x.get("spread") is not None
               and x.get("spreadOpen") is not None), None)
    if not ln:
        return None
    return {"open": float(ln["spreadOpen"]), "now": float(ln["spread"]), "src": ln.get("provider") or "CFBD"}


def _home_spread_text(team_home: str, team_away: str, home_spread: float) -> str:
    if home_spread == 0:
        return "pick'em"
    fav = team_home if home_spread < 0 else team_away
    return f"{fav} -{_n(abs(home_spread))}"


def move_signal(away: str, home: str, mv: dict | None) -> dict | None:
    if not mv:
        return None
    d = mv["now"] - mv["open"]  # < 0: home got more favored, the money came in on home
    o, n = _home_spread_text(home, away, mv["open"]), _home_spread_text(home, away, mv["now"])
    if abs(d) < 0.5:
        return {"side": None, "w": 0, "why": f"line movement: opened {o}, still {n}, no move"}
    side = home if d < 0 else away
    if abs(d) < MOVE_MIN:
        return {"side": None, "w": 0, "why": f"line movement: opened {o}, now {n}, a small move toward {side}"}
    return {"side": side, "w": W_MOVE, "why": f"line movement: opened {o}, now {n}, the market moved "
                                              f"{_n(abs(d))} points toward {side}"}


# -----------------------------------------------------------------------------
# advanced-stat efficiency and unit matchups (CFBD cache)
# -----------------------------------------------------------------------------

def _adv(t: dict | None, side: str) -> dict:
    return ((t or {}).get("adv") or {}).get(side) or {}


def efficiency_margin(ta: dict | None, th: dict | None) -> float | None:
    """Projected home margin from season PPA per play (not opponent-adjusted):
    each offense's PPA averaged with the defense it faces, times plays, plus
    home field."""
    vals = [_adv(ta, "offense").get("ppa"), _adv(ta, "defense").get("ppa"),
            _adv(th, "offense").get("ppa"), _adv(th, "defense").get("ppa")]
    if any(not isinstance(v, (int, float)) for v in vals):
        return None
    a_off, a_def, h_off, h_def = (float(v) for v in vals)
    home_ppa = (h_off + a_def) / 2
    away_ppa = (a_off + h_def) / 2
    return round(PLAYS_PER_GAME * (home_ppa - away_ppa) + HOME_FIELD, 1)


UNIT_LABEL = {"success": "success rate", "explosive": "explosiveness", "havoc": "havoc"}


def unit_edges(away: str, home: str, idx: dict | None) -> list[dict]:
    """Clear unit-vs-unit rank edges: each offense against the defense it
    faces on success rate, explosiveness and havoc. [{team, gap, text}]"""
    if not idx:
        return []
    ta, th = cfbd.resolve_team(away, idx), cfbd.resolve_team(home, idx)
    if not ta or not th:
        return []
    out = []
    for off_t, def_t in ((ta, th), (th, ta)):
        for stat in ("success", "explosive", "havoc"):
            ro, rd = _adv(off_t, "offense").get(stat + "_rank"), _adv(def_t, "defense").get(stat + "_rank")
            if not ro or not rd or abs(ro - rd) < UNIT_GAP:
                continue
            lab = UNIT_LABEL[stat]
            if stat == "havoc":
                o_txt, d_txt = f"{off_t['name']} offense avoids havoc #{ro}", f"{def_t['name']} defense havoc #{rd}"
            else:
                o_txt, d_txt = f"{off_t['name']} offense {lab} #{ro}", f"{def_t['name']} defense {lab} #{rd}"
            winner = off_t if ro < rd else def_t
            out.append({"team": winner["name"], "gap": abs(ro - rd), "text": f"{o_txt} vs {d_txt}: edge {winner['name']}"})
    out.sort(key=lambda e: -e["gap"])
    return out


def units_signal(away: str, home: str, idx: dict | None) -> tuple[dict | None, list[dict]]:
    """(signal or None, the edges) for the unit matchups."""
    edges = unit_edges(away, home, idx)
    if not edges:
        return None, []
    a_name = (cfbd.resolve_team(away, idx) or {}).get("name")
    na = sum(1 for e in edges if e["team"] == a_name)
    nh = len(edges) - na
    if abs(na - nh) < UNIT_EDGE_MIN:
        return ({"side": None, "w": 0, "why": f"unit matchups: clear edges split {away} {na}, {home} {nh}"}, edges)
    side = away if na > nh else home
    return ({"side": side, "w": W_UNITS, "why": f"unit matchups: {side} wins {max(na, nh)} of {len(edges)} clear "
                                                 "unit-vs-unit edges (success rate, explosiveness, havoc)"}, edges)


# -----------------------------------------------------------------------------
# public betting splits (DraftKings Network page, keyless HTML)
# -----------------------------------------------------------------------------

DK_SPLITS = "https://dknetwork.draftkings.com/draftkings-sportsbook-betting-splits/"
DK_NCAAF = "87637"  # DraftKings' college football event group
SPLITS_TTL = 20 * 60
SPLITS_MAX_PAGES = 8
_ROW = re.compile(r"\|\s*([^|]+?)\s+([+-]?\d+(?:\.\d+)?|PK|EVEN)\s*\|\s*[^|]*\|\s*(\d{1,3})%\s*\|\s*(\d{1,3})%")


def parse_dk_splits(page: str) -> list[dict]:
    """[{'away','home','sides': {team: {'handle': %, 'bets': %}}}] for the
    spread market on one splits page."""
    out = []
    starts = [m.start() for m in re.finditer(r"tb-se-title", page)]
    for i, s in enumerate(starts):
        blk = page[s: starts[i + 1] if i + 1 < len(starts) else len(page)]
        txt = htmllib.unescape(re.sub(r"<[^>]+>", " | ", blk))
        txt = re.sub(r"\s+", " ", txt).replace("−", "-")
        txt = re.sub(r"(\|\s*)+", "| ", txt)
        title = re.search(r"\|\s*([^|@]+?)\s+@\s+([^|]+?)\s*\|", txt)
        if not title:
            continue
        sides = {}
        body = txt[txt.find("Spread"):] if "Spread" in txt else txt
        for m in _ROW.finditer(body):
            sides[m.group(1).strip()] = {"handle": int(m.group(3)), "bets": int(m.group(4))}
            if len(sides) == 2:
                break
        if len(sides) == 2:
            out.append({"away": title.group(1).strip(), "home": title.group(2).strip(), "sides": sides})
    return out


_splits_cache: tuple[float, list[dict]] | None = None


def _same(a: str, b: str, idx: dict | None) -> bool:
    na, nb = cfbd.norm(a), cfbd.norm(b)
    if na == nb:
        return True
    if idx:
        ta, tb = cfbd.resolve_team(a, idx), cfbd.resolve_team(b, idx)
        return bool(ta and tb and ta is tb)
    return False


def find_split(rows: list[dict], away: str, home: str, idx: dict | None) -> dict | None:
    for r in rows:
        if _same(r["away"], away, idx) and _same(r["home"], home, idx):
            sides = {}
            for team, v in r["sides"].items():
                key = away if _same(team, away, idx) else home if _same(team, home, idx) else None
                if key:
                    sides[key] = v
            if len(sides) == 2:
                return sides
    return None


async def fetch_splits(client: httpx.AsyncClient, want: list[tuple[str, str]], idx: dict | None) -> list[dict]:
    """Splits rows, paging until every wanted game is found (or pages run
    out). Cached 20 min; on failure the last good copy is kept."""
    global _splits_cache
    if _splits_cache and time.time() - _splits_cache[0] < SPLITS_TTL:
        return _splits_cache[1]
    rows: list[dict] = []
    try:
        for page in range(1, SPLITS_MAX_PAGES + 1):
            r = await client.get(DK_SPLITS, params={"tb_eg": DK_NCAAF, "tb_edate": "n7days", "tb_emt": "Spread",
                                                    "tb_page": page}, headers=HEADERS, timeout=8.0)
            r.raise_for_status()
            got = parse_dk_splits(r.text)
            if not got:
                break
            rows += got
            if all(find_split(rows, a, h, idx) for a, h in want):
                break
    except Exception as e:  # noqa: BLE001 - optional source, fail soft
        log.warning("dk splits failed: %s", e)
        return _splits_cache[1] if _splits_cache else []
    _splits_cache = (time.time(), rows)
    return rows


def splits_signal(away: str, home: str, sides: dict | None) -> dict | None:
    if not sides:
        return None
    a, h = sides[away], sides[home]
    if any(not 5 <= v[k] <= 95 for v in (a, h) for k in ("handle", "bets")):
        return None  # 100/0 early in the week = a handful of bets, not a read
    da, dh = a["handle"] - a["bets"], h["handle"] - h["bets"]
    pub = away if a["bets"] > h["bets"] else home
    pub_pct = max(a["bets"], h["bets"])
    base = f"betting splits (DraftKings public page): {pub_pct}% of spread bets on {pub}"
    side, d = (away, da) if da >= dh else (home, dh)
    if d >= SPLIT_GAP:
        v = sides[side]
        return {"side": side, "w": W_SPLITS, "why": f"{base}; but {v['handle']}% of the money is on {side} "
                                                    f"from {v['bets']}% of the bets, so the bigger bettors are on {side}"}
    return {"side": None, "w": 0, "why": f"{base}; money and ticket counts agree, no sharp-vs-public split"}


# -----------------------------------------------------------------------------
# weather (Open-Meteo, keyless) as the fallback when ESPN has no forecast
# -----------------------------------------------------------------------------

OPEN_METEO = "https://api.open-meteo.com/v1/forecast"
WEATHER_TTL = 60 * 60
_weather_cache: dict[str, tuple[float, dict]] = {}


def _kick_ct(start) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(str(start)).replace(tzinfo=None)
    except ValueError:
        return None


async def fetch_weather(client: httpx.AsyncClient, lat: float, lon: float, start) -> dict | None:
    """Kickoff-hour forecast: {'temp_f', 'wind_mph', 'gust_mph', 'rain_pct', 'src'}.
    Open-Meteo forecasts 16 days out; anything further is None."""
    k = _kick_ct(start)
    if not k:
        return None
    key = f"{lat:.3f},{lon:.3f},{k:%Y-%m-%dT%H}"
    hit = _weather_cache.get(key)
    if hit and time.time() - hit[0] < WEATHER_TTL:
        return hit[1]
    try:
        r = await client.get(OPEN_METEO, params={
            "latitude": lat, "longitude": lon, "timezone": "America/Chicago",
            "hourly": "temperature_2m,precipitation_probability,wind_speed_10m,wind_gusts_10m",
            "temperature_unit": "fahrenheit", "wind_speed_unit": "mph",
            "start_date": k.date().isoformat(), "end_date": k.date().isoformat()}, timeout=6.0)
        r.raise_for_status()
        h = r.json()["hourly"]
        i = h["time"].index(k.strftime("%Y-%m-%dT%H:00"))
        w = {"temp_f": h["temperature_2m"][i], "wind_mph": h["wind_speed_10m"][i],
             "gust_mph": h["wind_gusts_10m"][i], "rain_pct": h["precipitation_probability"][i],
             "src": "Open-Meteo"}
        if w["temp_f"] is None:
            return None
    except Exception as e:  # noqa: BLE001 - optional source, fail soft
        log.warning("open-meteo failed: %s", e)
        return hit[1] if hit else None
    _weather_cache[key] = (time.time(), w)
    return w


_ESPN_WX = re.compile(r"(-?\d+)F(?:, wind gusts (\d+) mph)?(?:, (\d+)% chance of precipitation)?")


def espn_weather(text: str | None) -> dict | None:
    """espn.py's '47F, wind gusts 9 mph, 4% chance of precipitation' -> dict."""
    m = _ESPN_WX.match(text or "")
    if not m:
        return None
    return {"temp_f": int(m.group(1)), "wind_mph": None, "gust_mph": int(m.group(2)) if m.group(2) else None,
            "rain_pct": int(m.group(3)) if m.group(3) else None, "src": "ESPN"}


def weather_signal(away: str, home: str, line, wx: dict | None, dome: bool = False) -> dict | None:
    if dome:
        return {"side": None, "w": 0, "why": "weather: indoors (dome), no weather factor"}
    if not wx:
        return None
    bits = [f"{_n(wx['temp_f'])}F"]
    if wx.get("wind_mph") is not None:
        bits.append(f"wind {_n(wx['wind_mph'])} mph")
    if wx.get("gust_mph") is not None:
        bits.append(f"gusts {_n(wx['gust_mph'])} mph")
    if wx.get("rain_pct") is not None:
        bits.append(f"{_n(wx['rain_pct'])}% chance of rain")
    desc = f"weather at kickoff ({wx['src']}): " + ", ".join(bits)
    bad = ((wx.get("wind_mph") or 0) >= WIND_MPH or (wx.get("gust_mph") or 0) >= GUST_MPH
           or (wx.get("rain_pct") or 0) >= RAIN_PCT)
    try:
        pool_home = float(line)
    except (TypeError, ValueError):
        pool_home = 0.0
    if not bad or pool_home == 0:
        return {"side": None, "w": 0, "why": desc + ", no real weather factor" if not bad else desc}
    dog = away if pool_home < 0 else home
    return {"side": dog, "w": W_WEATHER, "why": desc + f": bad weather shortens games, a small tilt to the "
                                                       f"side getting points ({dog})"}


# -----------------------------------------------------------------------------
# per-request loader (only for breakdown questions)
# -----------------------------------------------------------------------------

async def load(games: list[dict], espn_notes: dict | None, idx: dict | None) -> dict:
    """{'splits': {game_id: sides}, 'weather': {game_id: wx}, 'dome': {game_id: bool}}
    for the given pool games. Never raises; missing data just means no signal."""
    out = {"splits": {}, "weather": {}, "dome": {}}
    if not games:
        return out
    espn_notes = espn_notes or {}
    async with httpx.AsyncClient() as c:
        async def wx_for(g):
            n = espn_notes.get(g["id"]) or {}
            v = n.get("venue") or {}
            ven = cfbd.venue(v.get("id"), v.get("name"), v.get("city"), idx=idx) if idx else None
            dome = bool(v.get("indoor")) or bool(ven and ven["dome"])
            out["dome"][g["id"]] = dome
            if dome:
                return
            w = espn_weather(n.get("weather"))
            if not w and ven:
                w = await fetch_weather(c, ven["lat"], ven["lon"], g.get("start"))
            if w:
                out["weather"][g["id"]] = w

        want = [(g["away"], g["home"]) for g in games]
        try:
            res = await asyncio.wait_for(asyncio.gather(
                fetch_splits(c, want, idx), *(wx_for(g) for g in games), return_exceptions=True), timeout=12.0)
        except asyncio.TimeoutError:
            log.warning("edge.load timed out")
            return out
        rows = res[0] if isinstance(res[0], list) else []
        for g in games:
            s = find_split(rows, g["away"], g["home"], idx)
            if s:
                out["splits"][g["id"]] = s
    return out
