"""
Weekly deep breakdown of the 4 pool games, posted in the league group right
after the "picks are up" announcement (Charlie, 2026-11-17: "a super deep
analysis breaking down the games after the text too, and do that every week";
his example: "This game is cold weather and long travel. This defense has an
edge...").

Code computes every fact (facts_for):
  the pool game card (cache/game_cards/<week>/<id>.json: league line, lean,
  reasons, best case against, line move + cause, honesty backtest), the market
  now (ESPN), slate_cards' computed lines (key numbers, implied totals, ATS
  splits, last meeting, rest/lookahead, sourced availability), unit-vs-unit
  ranks from the CFBD cache (rush O vs rush D, pass O vs pass D, pass rush vs
  protection, finishing drives, explosiveness, turnovers), a Saturday forecast
  at the stadium (Open-Meteo, free, keyless) and travel distance / time zones
  (CFBD venues).
The box's local gemma phrases it offline (local_llm.py; no Claude since phase 8): one text
per game in Picky's voice, ending in "Lean: <the card's lean label>".
verify() checks every draft in code: every number and W-L must be in that
game's facts, capitalized names must be in the facts, the lean line must match
the card, no league member names, no "I picked", no body/family jokes. Two
drafts; then lines with problems are dropped; if a game can't keep its lean
line it gets the plain code template, so the post never carries a made-up
number and still goes out with Anthropic down.

The group only sees ONE short text (picks_up_text, Charlie 2026-11-19: "make sure the posts aren't too
long"): lock time, the 4 lines, and each game's single sharpest verified line (the writer's second line,
under ANGLE_MAX) for as many games as fit in max_chars_long. The full per-game texts stay in the state file.

CLI:
  week_breakdown.py facts [--week N]      print the fact sheets
  week_breakdown.py draft [--week N]      facts + gemma + verify, print (no queue)
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import logging
import math
import re
import sqlite3
import sys
import zoneinfo
from collections import Counter
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import picky_reveal  # noqa: E402

log = logging.getLogger("week_breakdown")
CT = zoneinfo.ZoneInfo("America/Chicago")
UTC = dt.timezone.utc
CARD_DIR = HERE / "cache" / "game_cards"
OPEN_METEO = "https://api.open-meteo.com/v1/forecast"
MAX_CHARS = 900
ANGLE_MAX = 90                    # the one line per game that makes the group post

# -----------------------------------------------------------------------------
# facts
# -----------------------------------------------------------------------------


def _ord_rank(rows: dict, getter, high_good: bool) -> dict[str, int]:
    vals = []
    for team, r in rows.items():
        try:
            v = getter(r)
        except (KeyError, TypeError, ZeroDivisionError):
            continue
        if isinstance(v, (int, float)):
            vals.append((team, v))
    vals.sort(key=lambda x: x[1], reverse=high_good)
    return {t: i for i, (t, _) in enumerate(vals, 1)}


def unit_ranks(idx: dict) -> dict[str, dict]:
    """Per normalized team: ranks (1 = best) for each unit, from the CFBD cache."""
    import cfbd
    import slate_cards
    adv = slate_cards._advanced()
    stats = {cfbd.norm(t["name"]): t.get("stats") or {} for t in idx["teams"].values() if t.get("stats")}
    specs = {
        "rush_off": (adv, lambda r: r["offense"]["rushingPlays"]["ppa"], True),
        "rush_def": (adv, lambda r: r["defense"]["rushingPlays"]["ppa"], False),
        "pass_off": (adv, lambda r: r["offense"]["passingPlays"]["ppa"], True),
        "pass_def": (adv, lambda r: r["defense"]["passingPlays"]["ppa"], False),
        "finish_off": (adv, lambda r: r["offense"]["pointsPerOpportunity"], True),
        "finish_def": (adv, lambda r: r["defense"]["pointsPerOpportunity"], False),
        "expl_off": (adv, lambda r: r["offense"]["explosiveness"], True),
        "expl_def": (adv, lambda r: r["defense"]["explosiveness"], False),
        "sacks": (stats, lambda s: s["sacks"] / s["games"], True),
        "sacks_allowed": (stats, lambda s: s["sacksOpponent"] / s["games"], False),
        "to_margin": (stats, lambda s: s["turnoversOpponent"] - s["turnovers"], True),
    }
    out: dict[str, dict] = {}
    for name, (rows, get, hi) in specs.items():
        for team, rk in _ord_rank(rows, get, hi).items():
            out.setdefault(team, {})[name] = rk
    for team, s in stats.items():
        try:
            out.setdefault(team, {})["sacks_pg"] = s["sacks"] / s["games"]
            out[team]["sacks_allowed_pg"] = s["sacksOpponent"] / s["games"]
            out[team]["to_margin_n"] = s["turnoversOpponent"] - s["turnovers"]
        except (KeyError, TypeError, ZeroDivisionError):
            pass
    return out


def unit_lines(away: str, home: str, ranks: dict) -> list[str]:
    import cfbd
    a, h = ranks.get(cfbd.norm(away)) or {}, ranks.get(cfbd.norm(home)) or {}
    out = []

    def pair(off_t, o, def_t, d, okey, dkey, what_o, what_d):
        if o.get(okey) and d.get(dkey):
            out.append(f"{what_o}: {off_t} offense #{o[okey]} vs {def_t} defense #{d[dkey]} ({what_d})")

    for (ot, o, dt_, d) in ((away, a, home, h), (home, h, away, a)):
        pair(ot, o, dt_, d, "rush_off", "rush_def", "Run game (rushing EPA/play)", "rushing EPA allowed")
        pair(ot, o, dt_, d, "pass_off", "pass_def", "Passing game (passing EPA/play)", "passing EPA allowed")
        if o.get("sacks_allowed") and d.get("sacks"):
            out.append(f"Pass rush vs protection: {dt_} gets {d['sacks_pg']:.1f} sacks a game (#{d['sacks']}); "
                       f"{ot} allows {o['sacks_allowed_pg']:.1f} a game (#{o['sacks_allowed']} in fewest allowed)")
        pair(ot, o, dt_, d, "finish_off", "finish_def", "Red zone / finishing drives (points per scoring chance)",
             "points per scoring chance allowed")
        pair(ot, o, dt_, d, "expl_off", "expl_def", "Explosive plays", "explosiveness allowed")
    for t, r in ((away, a), (home, h)):
        if r.get("to_margin") and "to_margin_n" in r:
            n = r["to_margin_n"]
            out.append(f"Turnovers: {t} is {'+' if n > 0 else ''}{n} in turnover margin (#{r['to_margin']})")
    return out


def _home_venue(team: str, games_ds: dict) -> int | None:
    import cfbd
    c = Counter(g.get("venueId") for g in games_ds.values()
                if cfbd.norm(g.get("homeTeam") or "") == cfbd.norm(team) and not g.get("neutralSite") and g.get("venueId"))
    return c.most_common(1)[0][0] if c else None


def venues() -> dict[int, dict]:
    import cfbd
    try:
        db = sqlite3.connect(cfbd.DB_PATH)
        row = db.execute("select json from datasets where name='venues'").fetchone()
        db.close()
        return {v["id"]: v for v in json.loads(row[0])} if row else {}
    except Exception as e:  # noqa: BLE001
        log.warning("venues: %r", e)
        return {}


def miles(a: dict, b: dict) -> float:
    la1, lo1, la2, lo2 = map(math.radians, (a["latitude"], a["longitude"], b["latitude"], b["longitude"]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 3958.8 * 2 * math.asin(math.sqrt(h))


def travel_line(g: dict, games_ds: dict, vs: dict, kick: dt.datetime) -> str | None:
    meta = games_ds.get(g["id"]) or {}
    site = vs.get(meta.get("venueId"))
    home_v = vs.get(_home_venue(g["awayTeam"], games_ds))
    if not site or not home_v or site.get("latitude") is None or home_v.get("latitude") is None:
        return None
    d = miles(home_v, site)
    shift = ""
    try:
        o1 = kick.astimezone(zoneinfo.ZoneInfo(home_v["timezone"])).utcoffset()
        o2 = kick.astimezone(zoneinfo.ZoneInfo(site["timezone"])).utcoffset()
        hrs = round((o2 - o1).total_seconds() / 3600)
        shift = "same time zone" if hrs == 0 else f"{abs(hrs)} time zone{'s' if abs(hrs) > 1 else ''} {'east' if hrs > 0 else 'west'}"
    except Exception:  # noqa: BLE001
        pass
    s = (f"Travel: {g['awayTeam']} goes about {round(d / 10) * 10:.0f} miles ({home_v.get('city')}, "
         f"{home_v.get('state')} to {site.get('city')}, {site.get('state')})")
    s += f", {shift}" if shift else ""
    if site.get("dome"):
        s += "; the stadium is a dome"
    return s


def forecast(lat: float, lon: float, kick: dt.datetime, client: httpx.Client | None = None) -> dict | None:
    """Open-Meteo hourly forecast at kickoff (free, no key)."""
    day = kick.astimezone(UTC).date()
    params = {"latitude": lat, "longitude": lon, "temperature_unit": "fahrenheit", "wind_speed_unit": "mph",
              "hourly": "temperature_2m,apparent_temperature,precipitation_probability,wind_speed_10m,wind_gusts_10m",
              "timezone": "UTC", "start_date": day.isoformat(), "end_date": (day + dt.timedelta(days=1)).isoformat()}
    c = client or httpx.Client(timeout=20)
    try:
        r = c.get(OPEN_METEO, params=params)
        r.raise_for_status()
        h = r.json()["hourly"]
    except Exception as e:  # noqa: BLE001
        log.warning("forecast failed: %r", e)
        return None
    finally:
        if client is None:
            c.close()
    want = kick.astimezone(UTC).replace(minute=0, second=0, microsecond=0, tzinfo=None).isoformat(timespec="minutes")
    try:
        i = h["time"].index(want)
    except ValueError:
        return None
    win = range(i, min(i + 4, len(h["time"])))  # kickoff through ~3 hours in

    def mx(k):
        vals = [h[k][j] for j in win if h[k][j] is not None]
        return max(vals) if vals else None
    if h["temperature_2m"][i] is None:
        return None
    return {"temp": round(h["temperature_2m"][i]), "feels": round(h["apparent_temperature"][i]),
            "precip": mx("precipitation_probability"), "wind": round(h["wind_speed_10m"][i]),
            "gust": round(mx("wind_gusts_10m") or 0)}


def weather_line(fc: dict | None, venue_name: str, pulled: dt.date, dome: bool = False) -> str | None:
    if dome:
        return f"Weather: {venue_name} is a dome, so no weather"
    if not fc:
        return None
    s = (f"Forecast for kickoff (Open-Meteo, pulled {pulled.strftime('%a %b %-d')}): {fc['temp']}F"
         + (f" (feels like {fc['feels']}F)" if abs(fc["feels"] - fc["temp"]) >= 3 else "")
         + f", wind {fc['wind']} mph gusting to {fc['gust']}")
    if fc.get("precip") is not None:
        s += f", {round(fc['precip'])}% chance of {'rain' if fc['temp'] > 38 else 'rain or snow'}"
    return s


def load_cards(week: int, base: Path | None = None) -> dict[int, dict]:
    d = (base or CARD_DIR) / str(week)
    out = {}
    for p in sorted(d.glob("*.json")) if d.exists() else []:
        try:
            c = json.loads(p.read_text())
            out[int(c["game"]["id"])] = c
        except (OSError, ValueError, KeyError):
            continue
    return out


def lean_of(card: dict) -> dict | None:
    """The pool card's lean (pick_brief's factor engine vs the league line)."""
    ln = card.get("lean") or {}
    if not ln.get("side") or not ln.get("label"):
        return None
    return {"side": ln["side"], "label": ln["label"], "say": (card.get("confidence") or {}).get("say") or ""}


def fmt_line(team: str, sp: float) -> str:
    return f"{team} {sp:+g}" if sp else f"{team} PK"


def favorite_text(away: str, home: str, line: float) -> str:
    """'Iowa -8.5 at Illinois' from the pool's `line` (home adjustment, negative = home favored)."""
    line = float(line)
    if line < 0:
        return f"{away} at {home} {line:+g}"
    if line > 0:
        return f"{away} -{line:g} at {home}"
    return f"{away} at {home} (pick'em)"


def facts_for(pool_games: list[dict], week: int, now: dt.datetime | None = None,
              notes: dict | None = None, client: httpx.Client | None = None) -> list[dict]:
    """[{game_id, away, home, matchup, lean, facts: [str]}] for the week's pool games,
    pool_games = all_games rows (GameId, Away, Home, line, "Start (CT)")."""
    import cfbd
    import espn
    import slate_cards
    now = now or dt.datetime.now(CT)
    idx = cfbd.load_index()
    cards = load_cards(week)
    games_ds = slate_cards._games_dataset()
    vs = venues()
    ranks = unit_ranks(idx)
    ids = [int(g["GameId"]) for g in pool_games]
    if notes is None:
        try:
            notes = asyncio.run(espn.game_notes(ids))
        except Exception as e:  # noqa: BLE001
            log.warning("espn notes: %r", e)
            notes = {}
    by_id = {}
    for t in idx["teams"].values():
        for g in t.get("games") or []:
            by_id.setdefault(g["id"], g)
    out = []
    for pg in pool_games:
        gid = int(pg["GameId"])
        card = cards.get(gid)
        g = by_id.get(gid)
        if not card or not g:
            log.warning("game %s: no %s", gid, "card" if not card else "CFBD row")
            continue
        away, home = g["awayTeam"], g["homeTeam"]
        meta = games_ds.get(gid) or {}
        site = vs.get(meta.get("venueId")) or {}
        kick = slate_cards._sd(g)
        n = notes.get(gid) or {}
        mk = n.get("market") or {}
        L = float(pg["line"])
        lfav, lsp = (home, L) if L < 0 else (away, -L)
        locked = bool(card["league_line"].get("lock_at")) and now >= dt.datetime.fromisoformat(
            card["league_line"]["lock_at"]).replace(tzinfo=CT)
        F = [f"Game: {away} at {home}, {kick.astimezone(CT).strftime('%a %b %-d %-I:%M %p CT') if kick else 'TBD'}, "
             f"{site.get('name') or meta.get('venue') or ''}"
             + (f" ({site.get('city')}, {site.get('state')})" if site.get("city") and site.get("city") not in (site.get("name") or "") else ""),
             f"League line ({'locked' if locked else 'locks Tue 11 AM CT'}): {fmt_line(lfav, lsp)}"]
        names = [n.get(k, {}).get("name") for k in ("away", "home") if (n.get(k) or {}).get("name")]
        if names:
            F.append("Teams: " + ", ".join(names))
        F += [x.strip() for x in slate_cards.league_vs_market(g, L, mk)[1:]]
        F += [x.strip() for x in slate_cards.market_line(g, mk)]
        if (card.get("move") or {}).get("cause"):
            F.append(f"Line move cause, per reports: {card['move']['cause']}")
        for team in (away, home):
            t = cfbd.resolve_team(team, idx)
            s = slate_cards.ats_line(team, slate_cards.team_rows(team, idx, g["startDate"]), t)
            sp = (t or {}).get("sp") or {}
            if sp.get("rank"):
                s += f"; SP+ #{sp['rank']} (offense #{sp.get('off_rank')}, defense #{sp.get('def_rank')})"
            F.append(s)
        F += unit_lines(away, home, ranks)
        for x in (slate_cards.style_line(away, home, idx), slate_cards.rest_line(g, idx, games_ds),
                  slate_cards.last_meeting(g)):
            if x:
                F.append(x.strip())
        wp = n.get("espn_win_prob")
        if wp:
            F.append(f"ESPN FPI: {home} {wp['home']:.0f}% to win")
        avail = []
        for x in slate_cards.news_lines(away, home, gid):
            x = x.strip()
            if x.startswith(("These are the only", "News: nothing")) or x.endswith(":"):
                continue
            avail.append(x.lstrip("- "))
        F += avail
        fc = None
        if kick and site.get("latitude") is not None:
            fc = None if site.get("dome") else forecast(site["latitude"], site["longitude"], kick, client)
            F.append(weather_line(fc, site.get("name") or "the stadium", now.date(), bool(site.get("dome"))) or "")
        if kick:
            F.append(travel_line(g, games_ds, vs, kick) or "")
        lean = lean_of(card)
        if lean:
            F.append(f"THE LEAN (the pool card, graded vs the league line): {lean['label']} ({lean['say']})")
            F += [f"Reason for the lean: {r}" for r in (card.get("reasons") or [])[:4]]
            case = card.get("case") or {}
            agrees = case.get("agrees_with_lean", case.get("pick") == lean["side"])
            film = [r["text"] for r in (case.get("reasons") or [])[:3]]
            F += [f"Best case against the lean: {r}" for r in (card.get("against") or [])[:3]]
            if agrees:
                F += [f"Film room reason for the lean: {r}" for r in film]
                if (case.get("counter") or {}).get("text"):
                    F.append(f"Best case against (film room): {case['counter']['text']}")
            else:  # the film room took the other side: its reasons are the case against
                F += [f"Best case against (the film room takes {case.get('pick')}): {r}" for r in film]
            if (card.get("confidence") or {}).get("backtest"):
                F.append(f"Honesty check: {card['confidence']['backtest']}")
        out.append({"game_id": gid, "away": away, "home": home, "matchup": f"{away} at {home}",
                    "header": favorite_text(away, home, L), "lean": lean, "facts": [f for f in F if f],
                    "market_home": mk.get("home_now"), "forecast": fc, "news": avail,
                    "venue_city": site.get("city") or home})
    return out


# -----------------------------------------------------------------------------
# verify
# -----------------------------------------------------------------------------

_NUM = re.compile(r"(?<![\w.])\d+(?:\.\d+)?")
_WL = re.compile(r"(?<![\w.])\d{1,3}-\d{1,3}(?:-\d{1,2})?(?![\w.])")
_CAP = re.compile(r"(?<![\w'])([A-Z][A-Za-z'.&]+)")
_SELF_PICK = re.compile(r"\b(i picked|i'?m on|i took|my pick|i'?m taking|i'?ve got|i locked)\b", re.I)
_BODY = re.compile(r"\b(penis|dick|weiner|wiener|tits|boobs|fat|ugly|bald|mom|mother|wife|girlfriend|dad|father)\b", re.I)
COMMON_CAPS = {
    "lean", "i", "i'm", "i'd", "i'll", "the", "a", "an", "and", "but", "so", "if", "it", "it's", "this", "that",
    "saturday", "sat", "sunday", "monday", "tuesday", "wednesday", "thursday", "friday", "nov", "november",
    "ct", "ats", "sp+", "sp", "epa", "fpi", "espn", "qb", "qbs", "ot", "td", "tds", "int", "ints", "fg", "mph", "f",
    "big", "ten", "sec", "big 12", "acc", "cfp", "ap", "week", "picky", "forecast", "open-meteo", "vegas",
    "market", "draftkings", "dk", "pk", "no", "yes", "not", "one", "two", "three", "four", "five", "six", "seven",
    "eight", "nine", "ten", "home", "road", "under", "over", "total", "key", "line", "rain", "snow", "wind",
}


def _nums(text: str) -> set[str]:
    out = set()
    for n in _NUM.findall(text):
        out.add(n)
        out.add(f"{float(n):g}")
    return out


def verify(text: str, facts: list[str], lean: dict | None, member_names: list[str] | None = None,
           need_forecast: bool = False, hide: bool = False) -> list[str]:
    """Problems with one game's text, [] = good. Every number / W-L / name has to be in the facts.
    hide: Picky's sides are hidden (picky_reveal), so any lean/side talk is a problem."""
    fact_text = "\n".join(facts)
    probs = []
    nums = _nums(fact_text)
    bad = sorted({n for n in _NUM.findall(text) if n not in nums and f"{float(n):g}" not in nums})
    if bad:
        probs.append("numbers not in the facts: " + ", ".join(bad))
    ranks = set(re.findall(r"#\d+", fact_text))
    bad_rk = sorted({x for x in re.findall(r"#\d+", text) if x not in ranks})
    if bad_rk:
        probs.append("ranks not in the facts: " + ", ".join(bad_rk))
    wl_facts = set(_WL.findall(fact_text))
    bad_wl = sorted({x for x in _WL.findall(text) if x not in wl_facts})
    if bad_wl:
        probs.append("records/scores not in the facts: " + ", ".join(bad_wl))
    words = {w.lower().strip(".'") for w in re.findall(r"[A-Za-z][A-Za-z'.&+-]*", fact_text)}
    words |= {p for w in list(words) for p in re.split(r"[-.&+]", w) if p}
    unknown = set()
    for line in text.splitlines():
        for sent in re.split(r"(?<=[.!?:])\s+", line.strip()):
            for i, m in enumerate(_CAP.finditer(sent)):
                w = m.group(1).strip(".'").lower()
                if m.start() == 0 or w in COMMON_CAPS or w in words or w.rstrip("'s") in words:
                    continue
                unknown.add(m.group(1))
    if unknown:
        probs.append("names not in the facts: " + ", ".join(sorted(unknown)))
    if lean:
        if not re.search(rf"^\s*Lean: {re.escape(lean['label'])}\b", text, re.I | re.M):
            probs.append(f"must end with the line 'Lean: {lean['label']}' (the card's lean, exactly)")
    if _SELF_PICK.search(text):
        probs.append("talks about Picky's own pick; his picks stay hidden until lock, use 'Lean:' only")
    if hide and picky_reveal.LEAN_TALK.search(text):
        probs.append("takes a side; Picky's leans stay hidden until the picks lock at kickoff, give no lean or side")
    if _BODY.search(text):
        probs.append("goes after bodies or family")
    for nm in member_names or []:
        if len(nm) > 2 and re.search(rf"(?<!\w){re.escape(nm)}(?!\w)", text):
            probs.append(f"names league member {nm}; leave members out of the breakdown")
    if need_forecast and "forecast" not in text.lower():
        probs.append("say 'forecast' when you mention the weather (it's a forecast, not a fact)")
    if len(text) > MAX_CHARS:
        probs.append(f"{len(text)} characters; keep it under {MAX_CHARS}")
    return probs


def salvage(text: str, facts: list[str], lean: dict | None, member_names: list[str] | None = None,
            hide: bool = False) -> str | None:
    """Drop the lines that fail verify; keep the text only if the lean line and 3+ lines survive."""
    keep = [ln for ln in text.splitlines() if ln.strip() and not [
        p for p in verify(ln, facts, None, member_names, hide=hide) if not p.startswith(("must end", "say 'forecast'"))]]
    out = "\n".join(keep)
    if len(keep) < 3 or verify(out, facts, lean, member_names, hide=hide):
        return None
    return out


LEAN_FACTS = ("THE LEAN", "Reason for the lean", "Best case against", "Film room reason", "Honesty check")


def hide_leans(gfs: list[dict]) -> list[dict]:
    """Copies of the fact sheets with the card's lean and every lean-framed fact taken out (Picky's sides
    stay hidden in the group until the picks lock at kickoff: picky_reveal)."""
    return [gf | {"lean": None, "facts": [f for f in gf["facts"] if not f.startswith(LEAN_FACTS)]} for gf in gfs]


def plain_text(gf: dict) -> str:
    """Code-only fallback: the computed lines, lightly worded."""
    F = gf["facts"]

    def find(prefix):
        return next((f for f in F if f.startswith(prefix)), None)
    lines = [gf["header"]]
    for p in ("Market (", "Line move cause", "Run game", "Pass rush", "Forecast for kickoff", "Travel:"):
        x = find(p)
        if x:
            lines.append(x)
    if gf["lean"]:
        r = find("Reason for the lean:")
        if r:
            lines.append(r.replace("Reason for the lean: ", "Why: "))
        lines.append(f"Lean: {gf['lean']['label']}")
    return "\n".join(lines)


# -----------------------------------------------------------------------------
# writer
# -----------------------------------------------------------------------------

SCHEMA = {"type": "object", "properties": {
    "intro": {"type": "string"},
    "games": {"type": "array", "items": {"type": "object", "properties": {
        "game_id": {"type": "integer"}, "text": {"type": "string"}}, "required": ["game_id", "text"]}}},
    "required": ["intro", "games"]}

VOICE = """You are Picky, the college football guy in a 14-friend pick'em league's iMessage group. Everyone picks
these 4 games against the league's spread (the pick is who COVERS, not who wins). Voice: confident, quick, dry,
funny, medium heat, like a sharp who actually watches film texting his buddies. Helpful first: the analysis has to
be real. Never cruel about bodies, families, race, religion or sexuality, no slurs. Never call yourself a bot or AI.
Don't name or roast any league member in this post. Never say what you picked or that you're "on" a side: your
own picks stay hidden until the lock. Only give the lean."""


HIDDEN_TASK_END = """Last line: the sharpest angle from the facts. Give NO lean, NO side and NO pick (no "Lean:" line,
no "I like", no "take the points", no "value on X"): Picky's sides stay hidden in the group until the picks lock
at kickoff. The analysis is the angles, not a call."""


def build_prompt(gfs: list[dict], week: int, lock_text: str, problems: dict | None = None,
                 hide: bool = False) -> str:
    blocks = []
    for gf in gfs:
        blocks.append(f"GAME {gf['game_id']}: {gf['header']}"
                      + ("" if hide else f"  (lean: {gf['lean']['label'] if gf['lean'] else 'none'})") + "\n"
                      + "\n".join(f"  - {f}" for f in gf["facts"]))
    fix = ""
    if problems:
        fix = "\n\nYOUR LAST DRAFT WAS REJECTED BY THE FACT CHECKER. Fix every problem:\n" + "\n".join(
            f"- game {gid}: {'; '.join(p)}" for gid, p in problems.items())
    end = HIDDEN_TASK_END if hide else """Last line, exactly: "Lean: <the lean label from the GAME header>" plus a short reason after a dash
from the facts (e.g. "Lean: Illinois +8.5 - ..."). Say how strong the lean is honestly (slight lean = slight)."""
    voice = VOICE.replace("Only give the lean.", "Don't give a lean or a side either.") if hide else VOICE
    return f"""{voice}

TASK: the week {week} deep breakdown, posted right after "picks are up" (lock: {lock_text}). Charlie wants real
analysis per game, the kind of line a sharp would text: "This game is cold weather and long travel. This defense
has an edge...". For EACH game write ONE text of 4 to 7 short lines (separate lines with a newline, no bullets,
no headers, no emoji strings), under {MAX_CHARS} characters. First line: the game with the league line, as given
in the GAME header. Second line: the single sharpest angle, under {ANGLE_MAX} characters (that one line is what goes
in the group text; nobody reads long posts). Then pick the sharpest angles from its FACTS: league line vs the market and any move (with the
cause if given), key numbers, the unit-vs-unit edges that actually matter (run game vs run D, passing vs pass D,
pass rush vs protection, red zone, explosives, turnovers), ATS form, sourced QB/injury news, the Saturday forecast
(call it a forecast and say it's from Open-Meteo pulled on the date given), travel/time zone/rest, lookahead or
letdown spots. {end}
Also write a one-line "intro" for the whole post (no numbers needed, no member names).

HARD RULES: every number, record, score and name you write must appear in that game's FACTS below (copy them
exactly; no rounding, no new stats). If a fact isn't there, leave it out. Never invent injuries, weather or quotes.
No "locks"/"free money": ATS is close to a coin flip, say so where the facts do.

{chr(10).join(blocks)}{fix}

Return only the JSON object {{"intro": "...", "games": [{{"game_id": <id>, "text": "..."}}, ...]}}."""


def run_writer(prompt: str) -> dict:
    """The breakdown writer: the box's local gemma (zero maintenance, phase 8: no Claude). compose() verifies
    every number and falls back to the code-written text per game."""
    import local_llm
    return local_llm.json_call("", prompt, SCHEMA, caller="week_breakdown", temperature=0.5, num_predict=2000)


def compose(gfs: list[dict], week: int, lock_text: str, member_names: list[str] | None = None,
            writer=run_writer, hide: bool = False) -> tuple[list[str], dict]:
    """texts (intro + one per game) and a report {game_id: how}. Never raises.
    hide: no leans anywhere (the fact sheets lose them, the writer is told, verify rejects side talk)."""
    if hide:
        gfs = hide_leans(gfs)
    intro_facts = [f"week {week}", lock_text] + [gf["header"] for gf in gfs]
    texts: dict[int, str] = {}
    report: dict = {}
    intro = None
    problems: dict = {}
    drafts: dict[int, str] = {}
    for attempt in range(2):
        todo = [gf for gf in gfs if gf["game_id"] not in texts]
        if not todo:
            break
        try:
            out = writer(build_prompt(todo, week, lock_text, problems or None, hide=hide))
        except Exception as e:  # noqa: BLE001
            log.warning("writer failed (attempt %d): %r", attempt + 1, e)
            problems = {}
            continue
        if intro is None and out.get("intro") and not verify(out["intro"], intro_facts, None, member_names, hide=hide):
            intro = out["intro"].strip()
        got = {int(x.get("game_id", 0)): str(x.get("text") or "").strip() for x in out.get("games") or []}
        problems = {}
        for gf in todo:
            t = got.get(gf["game_id"])
            if not t:
                problems[gf["game_id"]] = ["missing"]
                continue
            drafts[gf["game_id"]] = t
            need_fc = any(f.startswith("Forecast for kickoff") for f in gf["facts"])
            p = verify(t, gf["facts"], gf["lean"], member_names, need_forecast=need_fc and "weather" in t.lower(),
                       hide=hide)
            if p:
                problems[gf["game_id"]] = p
                log.info("game %s draft %d rejected: %s", gf["game_id"], attempt + 1, "; ".join(p))
            else:
                texts[gf["game_id"]] = t
                report[gf["game_id"]] = f"opus draft {attempt + 1}"
    for gf in gfs:
        if gf["game_id"] in texts:
            continue
        s = salvage(drafts.get(gf["game_id"], ""), gf["facts"], gf["lean"], member_names, hide=hide)
        if s:
            texts[gf["game_id"]], report[gf["game_id"]] = s, "opus, failing lines dropped"
        else:
            texts[gf["game_id"]], report[gf["game_id"]] = plain_text(gf), "plain template"
    out_texts = ([intro] if intro else []) + [texts[gf["game_id"]] for gf in gfs]
    report["problems"] = problems
    return out_texts, report


# -----------------------------------------------------------------------------
# weekly automation (group_posts.tick calls tick_picks_up and refresh_texts)
# -----------------------------------------------------------------------------

POOL_SIZE = 4                     # the league picks 4 games a week
BREAKDOWN_GAP_S = 4 * 60          # (unused since the one-text rule)
MERGE_WAIT_S = 3 * 3600           # wait this long for the cards before announcing without a breakdown
REPREPARE_S = 12 * 3600           # a draft older than this is rebuilt before it posts
POST_QUIET_END = "09:00"          # overnight drops wait for 9am (quiet hours are 23:30-08:00)


def ann_key(season, week) -> str:
    return f"picksup:{season}:{week}"


def bd_key(season, week) -> str:
    return f"breakdown:{season}:{week}"


def state_path(season, week) -> Path:
    import group_outbox as ob
    return ob.PRIVATE / "breakdowns" / f"{season}-w{week}.json"


def load_state(season, week) -> dict:
    try:
        return json.loads(state_path(season, week).read_text())
    except (OSError, ValueError):
        return {}


def save_state(season, week, st: dict) -> None:
    import group_outbox as ob
    ob._write_private(state_path(season, week), json.dumps(st, indent=1, default=str))


def post_window(now: dt.datetime) -> bool:
    """Picks-up posts go out 09:00-23:30 CT: quiet hours, with mornings held to 9am."""
    import group_outbox as ob
    cfg = ob.config() | {"quiet_end": POST_QUIET_END}
    return not ob.in_quiet_hours(now, cfg)


def lock_text(lock: dt.datetime) -> str:
    """'Saturday 11am' / 'Friday 6:30pm'."""
    lock = lock.astimezone(CT)
    t = lock.strftime("%-I:%M%p").lower().replace(":00", "")
    return f"{lock.strftime('%A')} {t}"


def angle_of(text: str) -> str | None:
    """A verified game text's one short analysis line for the group post: the first line after the
    header that isn't a lean and fits ANGLE_MAX, else the first such line's first sentence if that fits.
    None if there isn't one."""
    lines = [x.strip() for x in text.splitlines() if x.strip() and not x.strip().lower().startswith("lean")][1:]
    for ln in lines:
        if len(ln) <= ANGLE_MAX:
            return ln
    for ln in lines:
        first = re.split(r"(?<=[.!?])\s+", ln, maxsplit=1)[0]
        if len(first) <= ANGLE_MAX:
            return first
    return None


def picks_up_text(week: int, games: list[dict], lock: dt.datetime, game_texts: list[str] | None,
                  limit: int) -> str:
    """The picks-up post as ONE text under `limit`, in Charlie's wording (2026-11-23): the head, a heads-up
    when a non-Saturday kickoff sets the lock, the bare lines (live at post time), the @Picky line. No
    per-game notes; game_texts is unused. Code-assembled, so it can't run long."""
    import group_outbox as ob
    import group_posts as gp
    lk = lock.astimezone(CT)
    early = [g for g in games if lk.weekday() != 5 and gp.kick(g) == lock]
    if early:
        t = lk.strftime("%-I:%M%p").lower().replace(":00", "")
        head = (f"Week {week} picks are up. Heads up: {early[0]['Away']} at {early[0]['Home']} kicks "
                f"{lk.strftime('%A')} at {t}, so all 4 picks lock then, not Saturday.")
    else:
        head = f"Week {week} picks are up, lock is {lock_text(lock)}."
    lines = [favorite_text(g["Away"], g["Home"], g["line"]) for g in games]
    return ob.clip(["\n".join([head] + lines + ["@Picky me or DM me if you want my read."])], limit)[0]


def cards_ready(week: int, games: list[dict]) -> bool:
    cards = load_cards(week)
    return all(int(g["GameId"]) in cards and lean_of(cards[int(g["GameId"])]) for g in games)


def prepare(lg, week: int, games: list[dict], lock: dt.datetime, now: dt.datetime, writer=None,
            hide: bool = True) -> dict:
    """Facts + local draft + verify, saved to the week's state file. Returns the state. The saved
    "games" keep the card's lean (refresh_texts compares it); the texts carry it only when not hide."""
    import group_posts as gp
    gfs = facts_for(games, week, now=now)
    texts, rep = compose(gfs, week, gp.kick_text(lock), list(gp.all_names(lg.names)),
                         **({"writer": writer} if writer else {}), hide=hide)
    st = load_state(lg.season, week)
    st.update({"prepared": now.timestamp(), "texts": texts, "report": rep, "hidden": hide,
               "games": {str(gf["game_id"]): {k: gf[k] for k in ("header", "lean", "market_home", "forecast", "news",
                                                                 "venue_city", "away", "home")} for gf in gfs},
               "facts": {str(gf["game_id"]): gf["facts"] for gf in gfs}})
    save_state(lg.season, week, st)
    log.info("breakdown week %s prepared: %s", week, json.dumps(rep, default=str)[:400])
    return st


def tick_picks_up(lg, week: int | None, now: dt.datetime, send, writer=None, ready=cards_ready) -> str:
    """One pass of the picks-up trigger. `send(kind, texts, key, **kw)` queues a post.
    Returns what it did (for logs/tests)."""
    import group_outbox as ob
    if week is None:
        return "no-open-week"
    games = lg.week_games(week)
    lock = lg.lock_time(week)
    sea = lg.season
    if not lock or now >= lock:
        return "locked"
    if len(games) < POOL_SIZE or any(g.get("line") is None for g in games):
        return "waiting-games"
    if ob.done(ann_key(sea, week)) and ob.done(bd_key(sea, week)):
        return "done"
    st = load_state(sea, week)
    if "first_seen" not in st:
        st["first_seen"] = now.timestamp()
        save_state(sea, week, st)
    did = []
    # ONE text per week (Charlie, 2026-11-17: "just send 1 text when picks are up, not a bunch"):
    # the announcement and the breakdown go out together as a single message. If the cards
    # aren't ready within MERGE_WAIT_S of the games landing, the announcement goes alone
    # (still one text) and that week has no breakdown post.
    # Picky's leans stay out of the group until the picks lock at kickoff (picky_reveal; a League
    # without picky_revealed, e.g. a test double, counts as hidden). A draft made with leans is stale once hidden.
    rv = getattr(lg, "picky_revealed", None)
    hide = not (rv and rv(week))
    stale = (not st.get("texts") or now.timestamp() - st.get("prepared", 0) > REPREPARE_S
             or (hide and not st.get("hidden")))
    if stale and ready(week, games):
        st = prepare(lg, week, games, lock, now, writer, hide=hide)
        did.append("prepared")
    # st["not_before"] (epoch seconds, set by hand in the week's state file): hold this week's post until then.
    held = now.timestamp() < float(st.get("not_before") or 0)
    if not ob.done(ann_key(sea, week)) and post_window(now) and not held:
        expires = int(max(600, (lock - now).total_seconds()))
        lim = ob.limits("announce")[0]
        if st.get("texts") and (st.get("hidden") or not hide):
            send("announce", [picks_up_text(week, games, lock, st["texts"], lim)], ann_key(sea, week),
                 expires_s=expires)
            ob.mark(bd_key(sea, week), "merged")
            did.append("announced+breakdown")
        elif now.timestamp() - st["first_seen"] >= MERGE_WAIT_S:
            send("announce", [picks_up_text(week, games, lock, None, int(ob.config()["max_chars"]))], ann_key(sea, week),
                 expires_s=expires)
            ob.mark(bd_key(sea, week), "skipped-no-cards")
            did.append("announced-alone")
    elif ob.done(ann_key(sea, week)) and not ob.done(bd_key(sea, week)):
        ob.mark(bd_key(sea, week), "skipped-one-text")  # announced already: never a second text
    return ",".join(did) or "waiting"


def refresh_texts(lg, week: int, now: dt.datetime, notes: dict | None = None,
                  leans: bool = False) -> list[str] | None:
    """Thursday refresh: only what changed since the breakdown (market moves of 1+ point, new
    availability lines, forecast swings, a flipped lean only when `leans`: Picky's sides are
    revealed to the group, picky_reveal). None = no breakdown on file (use the
    old preview); [] = nothing changed (skip the post)."""
    st = load_state(lg.season, week)
    if not st.get("games"):
        return None
    games = lg.week_games(week)
    try:
        cur = {str(gf["game_id"]): gf for gf in facts_for(games, week, now=now, notes=notes)}
    except Exception as e:  # noqa: BLE001
        log.warning("refresh facts failed: %r", e)
        return []
    lines = []
    for gid, old in st["games"].items():
        new = cur.get(gid)
        if not new:
            continue
        bits = []
        a, b = old.get("market_home"), new.get("market_home")
        if a is not None and b is not None and abs(float(a) - float(b)) >= 1:
            bits.append(f"market moved to {fmt_line(new['home'], float(b)) if float(b) < 0 else fmt_line(new['away'], -float(b))}"
                        f" (was {fmt_line(new['home'], float(a)) if float(a) < 0 else fmt_line(new['away'], -float(a))})")
        since = dt.datetime.fromtimestamp(st.get("prepared", 0), CT).date().isoformat()
        fresh = [x for x in new.get("news") or [] if x not in (old.get("news") or [])
                 and (m := re.search(r"(\d{4}-\d{2}-\d{2})\)\s*(\[rumor\])?$", x)) and m.group(1) > since]
        bits += [f"new: {x}" for x in fresh[:2]]
        fo, fn = old.get("forecast") or {}, new.get("forecast") or {}
        if fo and fn and (abs(fo["temp"] - fn["temp"]) >= 8 or abs(fo["gust"] - fn["gust"]) >= 10
                          or abs((fo.get("precip") or 0) - (fn.get("precip") or 0)) >= 30):
            bits.append(f"forecast now {fn['temp']}F, gusts to {fn['gust']} mph, {round(fn.get('precip') or 0)}% chance "
                        f"of precip (Tuesday's was {fo['temp']}F, gusts {fo['gust']}, {round(fo.get('precip') or 0)}%)")
        lo, ln = (old.get("lean") or {}).get("label"), (new.get("lean") or {}).get("label")
        if leans and lo and ln and lo != ln:
            bits.append(f"lean flipped: now {ln} (was {lo})")
        if bits:
            lines.append(f"{new['header']}: " + "; ".join(bits))
    if not lines:
        return []
    return [f"Week {week}, what's changed since the breakdown (Open-Meteo forecasts):\n" + "\n".join(lines)]


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------


def _pool_rows(week: int | None):
    import group_posts as gp
    lg = gp.League(gp.DB(), dt.datetime.now(CT))
    w = week or lg.open_week()
    return lg, w, lg.week_games(w)


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    ap = argparse.ArgumentParser(prog="week_breakdown.py")
    ap.add_argument("cmd", choices=["facts", "draft"])
    ap.add_argument("--week", type=int)
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    lg, w, games = _pool_rows(a.week)
    gfs = facts_for(games, w)
    if a.cmd == "facts":
        for gf in gfs:
            print(f"\n=== {gf['header']} (lean {gf['lean']}) ===")
            print("\n".join("  " + f for f in gf["facts"]))
        return 0
    import group_posts as gp
    lock = lg.lock_time(w)
    texts, rep = compose(gfs, w, gp.kick_text(lock), list(gp.all_names(lg.names)))
    print(json.dumps({"texts": texts, "report": rep, "facts": {gf["game_id"]: gf["facts"] for gf in gfs}}, indent=1)
          if a.out is None else "")
    if a.out:
        Path(a.out).write_text(json.dumps({"texts": texts, "report": rep,
                                           "facts": {gf["game_id"]: gf["facts"] for gf in gfs}}, indent=1))
        for t in texts:
            print("-----\n" + t)
        print(json.dumps(rep))
    return 0


if __name__ == "__main__":
    sys.exit(main())
