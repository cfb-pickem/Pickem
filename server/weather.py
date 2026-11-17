"""
Kickoff-hour forecasts for this week's FBS games, cached per game in
server/cache/weather.json. Fetched ONLY on a schedule (`weather.py refresh`,
hooked into the hourly com.pickem.slate-intel tick, which runs it at most every
6 h); the GAME CARD reads the cache and never fetches per message.

Source: Open-Meteo forecast API (free, no key), hourly temperature, precip
probability, wind and gusts at the stadium's lat/lon (CFBD 'venues'). Domes are
skipped (stored as dome=true). Games past Open-Meteo's 16-day window are skipped.

    python3 -I weather.py refresh [--force]     # fetch (respects the 6 h gap unless --force)
    python3 -I weather.py show
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
CACHE = Path(os.environ.get("PICKY_WEATHER_CACHE", HERE / "cache" / "weather.json"))
CT = ZoneInfo("America/Chicago")
METEO = "https://api.open-meteo.com/v1/forecast"
EVERY = dt.timedelta(hours=6)       # refresh gap
WINDOW = dt.timedelta(days=15)      # Open-Meteo forecasts 16 days out
STALE = dt.timedelta(hours=18)      # older than this, the card says nothing rather than an old forecast
WIND_MPH, GUST_MPH, RAIN_PCT, COLD_F = 15, 25, 60, 32
log = logging.getLogger("weather")
_MEMO: dict = {}


def load() -> dict:
    """The cache file (mtime-memoized); {} when missing or unreadable."""
    try:
        m = CACHE.stat().st_mtime
    except OSError:
        return {}
    if _MEMO.get("m") != m:
        try:
            _MEMO.update({"m": m, "d": json.loads(CACHE.read_text())})
        except (OSError, ValueError):
            return {}
    return _MEMO.get("d") or {}


def for_game(game_id) -> dict | None:
    return (load().get("games") or {}).get(str(game_id))


def _fetch_one(client, lat: float, lon: float, kick: dt.datetime) -> dict | None:
    k = kick.astimezone(dt.timezone.utc)
    r = client.get(METEO, params={
        "latitude": lat, "longitude": lon, "timezone": "UTC",
        "hourly": "temperature_2m,precipitation_probability,wind_speed_10m,wind_gusts_10m",
        "temperature_unit": "fahrenheit", "wind_speed_unit": "mph",
        "start_date": k.date().isoformat(), "end_date": k.date().isoformat()}, timeout=15)
    r.raise_for_status()
    h = r.json()["hourly"]
    i = min(range(len(h["time"])), key=lambda j: abs(dt.datetime.fromisoformat(h["time"][j] + "+00:00") - k))
    if h["temperature_2m"][i] is None:
        return None
    return {"temp_f": h["temperature_2m"][i], "precip_pct": h["precipitation_probability"][i],
            "wind_mph": h["wind_speed_10m"][i], "gust_mph": h["wind_gusts_10m"][i]}


def due(now: dt.datetime | None = None) -> bool:
    now = now or dt.datetime.now(dt.timezone.utc)
    try:
        at = dt.datetime.fromisoformat(load()["fetched_at"])
    except (KeyError, ValueError, TypeError):
        return True
    return now - at >= EVERY


def refresh(force: bool = False, now: dt.datetime | None = None, client=None) -> dict:
    """Fetch this week's FBS games inside the forecast window (domes skipped) into the cache. Keeps the
    previous entry for a game whose fetch fails. Returns a small summary."""
    import cfbd
    import slate_cards
    import travel
    now = now or dt.datetime.now(dt.timezone.utc)
    if not force and not due(now):
        return {"skipped": "not due"}
    idx = cfbd.load_index()
    if not idx:
        return {"skipped": "no cfbd cache"}
    games = [g for g in slate_cards.upcoming_games(idx, now) if slate_cards._sd(g) and slate_cards._sd(g) - now <= WINDOW]
    t = travel.tables()
    prev = load().get("games") or {}
    out, fetched, failed, domes = {}, 0, 0, 0
    own = client is None
    if own:
        import httpx
        client = httpx.Client()
    try:
        for g in games:
            v, _ = travel.game_venue(g["id"], g["homeTeam"], t)
            kick = slate_cards._sd(g)
            base = {"away": g["awayTeam"], "home": g["homeTeam"], "kickoff": kick.isoformat(),
                    "venue": (v or {}).get("name")}
            if v and v.get("dome"):
                out[str(g["id"])] = {**base, "dome": True}
                domes += 1
                continue
            if not v or v.get("latitude") is None:
                continue
            try:
                wx = _fetch_one(client, float(v["latitude"]), float(v["longitude"]), kick)
            except Exception as e:  # noqa: BLE001 - one bad game never sinks the run
                log.warning("forecast %s failed: %r", g["id"], e)
                wx = None
            if wx:
                out[str(g["id"])] = {**base, **wx, "as_of": now.isoformat(timespec="seconds"),
                                     "src": "Open-Meteo forecast"}
                fetched += 1
            else:
                failed += 1
                if str(g["id"]) in prev:
                    out[str(g["id"])] = prev[str(g["id"])]
    finally:
        if own:
            client.close()
    doc = {"fetched_at": now.isoformat(timespec="seconds"), "games": out}
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CACHE.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, indent=1))
    os.replace(tmp, CACHE)
    return {"games": len(games), "fetched": fetched, "domes": domes, "failed": failed}


def _n(x) -> str:
    return str(int(round(float(x))))


def line(game_id, now: dt.datetime | None = None) -> str | None:
    """The card's weather line, from the cache only ('' fetch never happens here). None when there's no
    entry (outside the window / not refreshed) or it's stale."""
    w = for_game(game_id)
    if not w:
        return None
    if w.get("dome"):
        return "  Weather: indoors (dome), no weather factor."
    now = now or dt.datetime.now(dt.timezone.utc)
    try:
        at = dt.datetime.fromisoformat(w["as_of"])
    except (KeyError, ValueError):
        return None
    if now - at > STALE:
        return None
    bits = [f"{_n(w['temp_f'])}F"]
    if w.get("wind_mph") is not None:
        bits.append(f"wind {_n(w['wind_mph'])} mph" + (f" (gusts {_n(w['gust_mph'])})" if w.get("gust_mph") is not None else ""))
    if w.get("precip_pct") is not None:
        bits.append(f"{_n(w['precip_pct'])}% chance of precip")
    flags = []
    if (w.get("wind_mph") or 0) >= WIND_MPH or (w.get("gust_mph") or 0) >= GUST_MPH:
        flags.append("windy enough to hurt passing and kicking (leans under, a small tilt to the points)")
    if w.get("temp_f") is not None and w["temp_f"] <= COLD_F:
        flags.append("cold")
    if (w.get("precip_pct") or 0) >= RAIN_PCT:
        flags.append("a wet ball is likely")
    verdict = ", ".join(flags) if flags else "no real weather factor"
    days = max(0, (dt.datetime.fromisoformat(w["kickoff"]) - now).days) if w.get("kickoff") else None
    return (f"  Weather (forecast as of {at.astimezone(CT).strftime('%a %-I:%M %p CT')}, for kickoff"
            + (f", {days} days out, it can change" if days else "") + f"): {', '.join(bits)}: {verdict}.")


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    sys.path.insert(0, str(HERE))
    cmd = argv[1] if len(argv) > 1 else "show"
    if cmd == "refresh":
        print("weather:", refresh(force="--force" in argv))
        return 0
    print(json.dumps(load(), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
