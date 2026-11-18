"""
Phase 7 code lanes (plan 2026-11-18 s.3.3 / s.5): weather_alt_travel and interim, written in the s.3.1 template:

  [answer]   the direct answer, computed
  [support]  the 2-3 facts that question type needs
  [verdict]  one "so what" for the spread/total in lean words (coin flip / slight lean / lean; never a lock),
             or "doesn't move the number"
  [voice]    gemma's one line, added later by the router's FACT_TAIL (unchanged)

Reads only through sitfacts (cache interface); never fetches. Forecasts are always said as forecasts, with the
source and as-of time; past weather.STALE they aren't said at all.
"""

from __future__ import annotations

import datetime as dt
import re
from zoneinfo import ZoneInfo

import sitfacts

CT = ZoneInfo("America/Chicago")

WEATHER_ASK = re.compile(r"\b(weather|forecast|wind|winds|windy|gusts?|gusty|rain|raining|rainy|snow|snowing|freezing|"
                         r"temperature|temps?|how cold|cold (weather|game|temps?)|be cold)\b", re.I)
ALT_ASK = re.compile(r"\b(altitude|elevation|thin air|above sea level)\b", re.I)
TRAVEL_ASK = re.compile(r"\b(travel|traveling|travelling|time ?zones?|body ?clock|jet ?lag(ged)?|cross[- ]country|"
                        r"flying (east|west)|long trip)\b", re.I)
ASK = re.compile("|".join(p.pattern for p in (WEATHER_ASK, ALT_ASK, TRAVEL_ASK)), re.I)
NOT_SITUATIONAL = re.compile(r"\b(my picks|text (me|u|you)|out of town|wedding|on a plane)\b", re.I)

WIND_SLIGHT, GUST_SLIGHT = 15, 25   # weather.py's card thresholds: hurts passing and kicking a little
WIND_LEAN, GUST_LEAN = 20, 30       # strong enough to call a lean, never more
COLD_F, RAIN_PCT = 32, 60
HIGH_FT, LOW_FT = 4000, 2000        # travel.py's altitude thresholds


def _n(x) -> str:
    return str(int(round(float(x))))


def _poss(name: str) -> str:
    return name + ("'" if name.endswith("s") else "'s")


def _clock(x: dt.datetime) -> str:
    return x.strftime("%-I:%M %p").replace(":00 ", " ")


def _total(g: dict) -> str | None:
    return f"{float(g['total']):g}" if g.get("total") is not None else None


def _home_label(g: dict) -> str:
    sp = g.get("spread_home")
    return f"{g['home']} {float(sp):+g}" if sp not in (None, 0) else g["home"]


# -- weather -----------------------------------------------------------------------------------------------------------

def _weather(g: dict, f) -> tuple[str, str]:
    import weather
    w = f.forecast(g["id"])
    tot = _total(g)
    if w and w.get("dome"):
        return f"{g['away']} at {g['home']} is indoors.", "So no weather factor: it doesn't move the number."
    try:
        at = dt.datetime.fromisoformat(w["as_of"]) if w else None
    except (KeyError, ValueError, TypeError):
        at = None
    if not w or at is None or f.now() - at > weather.STALE or w.get("wind_mph") is None:
        return (f"I don't have a current forecast for {g['away']} at {g['home']} yet.",
                "Forecasts get decent inside a few days of kickoff, so ask me closer to it.")
    days = (g["kickoff"] - f.now()).days if g.get("kickoff") else None
    bits = [f"{_n(w['temp_f'])}F"] if w.get("temp_f") is not None else []
    bits.append(f"wind {_n(w['wind_mph'])} mph" + (f" gusting to {_n(w['gust_mph'])}" if w.get("gust_mph") is not None else ""))
    if w.get("precip_pct") is not None:
        bits.append(f"{_n(w['precip_pct'])}% chance of " + ("rain or snow" if (w.get("temp_f") or 99) <= 35 else "rain"))
    src = w.get("src") or "forecast"
    ans = (f"The {src} for {g['away']} at {g['home']} (as of {at.astimezone(CT).strftime('%a %-I:%M %p CT')}"
           + (f", {days} days before kickoff, so it can change" if days and days > 0 else "")
           + f") calls for {', '.join(bits)} at kickoff.")
    wind, gust = w.get("wind_mph") or 0, w.get("gust_mph") or 0
    extra = []
    if w.get("temp_f") is not None and w["temp_f"] <= COLD_F:
        extra.append("the cold on its own barely moves totals")
    if (w.get("precip_pct") or 0) >= RAIN_PCT:
        extra.append("a wet ball is likely")
    tail = (" Also " + " and ".join(extra) + ".") if extra else ""
    if wind >= WIND_LEAN or gust >= GUST_LEAN:
        v = (f"Wind like that hurts passing and kicking, so lean under" + (f" {tot}" if tot else "")
             + ", if the forecast holds.")
    elif wind >= WIND_SLIGHT or gust >= GUST_SLIGHT:
        v = f"That wind trims passing and kicking a bit: slight lean under" + (f" {tot}" if tot else "") + ", not more."
    else:
        v = "Wind's not a factor, so it doesn't move the" + (f" {tot} total." if tot else " total.")
    return ans, v + tail


# -- altitude ----------------------------------------------------------------------------------------------------------

def _altitude(g: dict, f) -> tuple[str, str] | None:
    v = f.venue(g) or {}
    fg = v.get("elev_ft")
    he, ae = f.home_elev(g["home"]), f.home_elev(g["away"])
    if fg is None or he is None or ae is None:
        return None
    neutral = bool(v.get("neutral"))
    where = v.get("name") or "the stadium"
    ans = (f"{_poss(g['home'])} home field sits at {he:,} ft and {_poss(g['away'])} is at {ae:,} ft"
           + (f"; {where} (neutral site) is at {fg:,} ft." if neutral or fg != he else "."))
    visitors = [(g["away"], ae)] + ([(g["home"], he)] if neutral else [])
    if fg < HIGH_FT:
        return ans, f"No altitude factor under {HIGH_FT:,} ft, so it doesn't move the number."
    low = [t for t, e in visitors if e < LOW_FT]
    mid = [t for t, e in visitors if LOW_FT <= e < HIGH_FT]
    if neutral:
        return ans, "Neutral site, so altitude is a wash unless one side lives up there: coin flip on that alone."
    if low:
        return ans, (f"That's a real altitude edge for {g['home']} with {', '.join(low)} coming up from near sea level, "
                     f"but the books bake home altitude in, so slight lean {_home_label(g)} on that alone.")
    if mid:
        return ans, f"Some altitude edge for {g['home']}, small enough to call it a coin flip on that alone."
    return ans, f"Edge muted: {g['away']} lives at altitude too, so it doesn't move the number."


# -- travel ------------------------------------------------------------------------------------------------------------

def _travel(g: dict, f) -> tuple[str, str] | None:
    import travel
    neutral = bool((f.venue(g) or {}).get("neutral"))
    parts, verdicts = [], []
    for team in ((g["away"], g["home"]) if neutral else (g["away"],)):
        tr = f.trip(team, g)
        if not tr or tr.get("same_venue"):
            continue
        s = f"{team} flies about {int(round(tr.get('miles') or 0, -1)):,} miles"
        z = tr.get("zones")
        if z:
            s += (f", {abs(z)} time zone{'s' if abs(z) > 1 else ''} {'east' if z > 0 else 'west'}"
                  + (f" ({tr['home_zone']} to {tr['venue_zone']})" if tr.get("home_zone") and tr.get("venue_zone") else ""))
            if tr.get("kick_local") and tr.get("kick_body"):
                s += f"; kickoff {_clock(tr['kick_local'])} local is {_clock(tr['kick_body'])} on {_poss(team)} body clock"
        else:
            s += ", same time zone"
        parts.append(s)
        bv = travel.body_clock_verdict(tr)
        if "classic body-clock spot" in bv:
            verdicts.append(f"that's the classic body-clock spot, slight lean {_home_label(g)} on travel alone")
        elif "mild edge" in bv:
            verdicts.append("a mild body-clock edge at most, coin flip on travel alone")
    if not parts:
        return None
    return "; ".join(parts) + ".", ((verdicts[0][0].upper() + verdicts[0][1:] + ".") if verdicts
                                    else "No real body-clock issue, so travel doesn't move the number.")


def weather_alt_travel_text(ctx: dict, q: str, facts=None) -> str | None:
    """Weather / altitude / travel for this week's game a text names. None when no game resolves."""
    if not ASK.search(q or "") or NOT_SITUATIONAL.search(q or ""):
        return None
    f = facts or sitfacts.live()
    g = f.find_game(q)
    if not g:
        return None
    want = [k for k, rx in (("weather", WEATHER_ASK), ("altitude", ALT_ASK), ("travel", TRAVEL_ASK)) if rx.search(q)]
    answers, verdicts = [], []
    for k in want:
        part = {"weather": _weather, "altitude": _altitude, "travel": _travel}[k](g, f)
        if part:
            answers.append(part[0])
            verdicts.append(part[1])
    if not answers:
        return None
    return " ".join(answers + verdicts)


# -- interim -----------------------------------------------------------------------------------------------------------

MONTHS = {"Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12}


def _hire_date(c: dict) -> dt.date | None:
    try:
        return dt.date.fromisoformat(c["date"])
    except (KeyError, TypeError, ValueError):
        m = re.search(r"\b(Sep|Oct|Nov|Dec)\w*\.? (\d{1,2})", c.get("text") or "")
        return dt.date(2026, MONTHS[m.group(1)], int(m.group(2))) if m else None


def _local_date(r: dict) -> dt.date | None:
    s = r.get("start") or r.get("date")
    try:
        x = dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    return x.astimezone(CT).date() if x.tzinfo else x.date()


def interim_text(ctx: dict, q: str, facts=None) -> str | None:
    """A midseason coaching change for the team a text names, plus straight-up and ATS since the hire date (games
    on a later local date than the change). Small sample, said plainly."""
    f = facts or sitfacts.live()
    for team in f.teams_named(q):
        c = f.coaching(team)
        if not c:
            continue
        hire = _hire_date(c)
        src = re.sub(r"^www\.", "", c.get("source_name") or "")
        out = c["text"].rstrip(".") + (f" (per {src})." if src else ".")
        if not hire:
            return out
        rows = [r for r in f.results(team) if (_local_date(r) or hire) > hire]
        when = f"{hire.strftime('%b')} {hire.day}"
        if not rows:
            return out + f" {team} hasn't played since the change, so there's no record under the interim yet."
        su_w = sum(1 for r in rows if r["pts"][0] > r["pts"][1])
        graded = [r for r in rows if r.get("cover") is not None]
        c_, l_ = sum(1 for r in graded if r["cover"] > 0), sum(1 for r in graded if r["cover"] < 0)
        p_ = len(graded) - c_ - l_
        ats = f"{c_}-{l_}" + (f"-{p_}" if p_ else "")
        games = ", ".join(f"{'beat' if r['pts'][0] > r['pts'][1] else 'lost to'} {r['opp']} {r['pts'][0]}-{r['pts'][1]}"
                          for r in rows)
        n = len(rows)
        return (out + f" Since {when}: {su_w}-{n - su_w} straight up" + (f", {ats} ATS" if graded else "")
                + f" ({games})."
                + f" Small sample ({n} game{'s' if n != 1 else ''}), so it's a nudge, not a reason on its own.")
    return None
