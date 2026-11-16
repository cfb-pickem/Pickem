"""
ATS pick brief (Charlie, 2026-11-16): every factor in docs/picky-pick-factors.md,
computed in code against the LEAGUE line, scored for which side it helps cover.

The league is graded only against the spread, so every factor here answers
"which side does this help COVER the league line", never "who wins".

No LLM anywhere in here. The model gets only the 3-5 strongest edges and the
red flags (brief_note), never the whole table; the whole table (full_note) is
for a "why?" / "give me everything" follow-up.

WEIGHTING: a simple, hand-set heuristic, NOT a trained model. Each factor puts
its weight on the side it helps cover; the side with more total weight is the
lean. Tiers:
  HEAVY   line value (league line vs market): 3 for a 0.5-1.4 pt gap, 6 for 1.5+
          (+1 when the gap crosses a key number: 3, 7, 10, 14)
          starting QB out: 4 to the other side (doubtful 2, questionable 1, flagged)
  MEDIUM  SP+ projection 2; FPI 1; line move open->now (1.5+ pts) 1; bye vs normal
          rest 1; short week 1; lookahead 1; letdown 1; revenge 1; travel/body
          clock 1; altitude 1; other key injuries 0.5 each (max 1.5); league game
          theory 1 (asker-specific)
  LIGHT   0.5: each matchup stat (success, explosiveness, run, pass, havoc, third
          down, red zone, kicker, turnovers, tempo, style clash, backdoor), team
          ATS by role, home ATS, recent ATS form, head-to-head, betting splits,
          weather, bowl eligibility, title stakes, style points, big-spread-low-total,
          SRS
  CONTEXT 0: explains or flags but doesn't move the lean (cause of a move, backup
          QB, coaching/program turmoil, kickoff time, turf, Senior Day, ...)
Strength: "strong" when the lean side has 4+ and the other side at most a third
of that; "lean" otherwise; "none" on a tie. Constants below; tests pin them.

STATUS (live / partial / none + source) is the single source of truth for the
status column in docs/picky-pick-factors.md; test_pick_brief checks they match.
"""

from __future__ import annotations

import collections
import datetime as dt
import json
import math
import re
import sqlite3
from pathlib import Path

import cfbd

# -----------------------------------------------------------------------------
# weights and thresholds
# -----------------------------------------------------------------------------

W_VALUE, W_VALUE_BIG, VALUE_BIG_GAP, W_KEY = 3, 6, 1.5, 1
W_QB_OUT, W_QB_DOUBT, W_QB_Q = 4, 2, 1
W_SP, W_FPI, W_SRS, W_EFF = 2, 1, 0.5, 1
W_MED, W_LIGHT = 1, 0.5
W_INJ, W_INJ_MAX = 0.5, 1.5
W_THEORY = 1
HOME_FIELD = 2.5            # points; same as chat.SP_HOME_FIELD and picky_picks.HOME_FIELD
PLAYS_PER_GAME = 68
KEY_NUMBERS = (3, 7, 10, 14)
MOVE_MIN, MOVE_FLAG = 1.5, 3.0
RANK_GAP = 30               # of ~136 FBS: a clear unit-vs-unit edge
BYE_DAYS, SHORT_DAYS = 13, 5
BIG_GAME_SP_RANK = 25       # an opponent this good makes a "big game"
TRAVEL_TZ, EARLY_KICK_HOUR = 2, 12   # 2+ time zones east for an 11am CT kick
ALTITUDE_M, LOW_HOME_M = 1200, 600
STRONG_MIN = 4
TOP_EDGES = 5

# n -> (status, source). "none" factors are listed in the doc and skipped here.
STATUS = {
    1: ("live", "all_games.line (league) vs ESPN pickcenter market, CFBD lines fallback"),
    2: ("live", "ESPN pickcenter open/now, CFBD spreadOpen fallback"),
    3: ("partial", "news.json + game dossier items for either team dated within 7 days of a move"),
    4: ("live", "league line vs market line (computed)"),
    5: ("live", "ESPN / CFBD over-under"),
    6: ("partial", "league line (frozen at the Tue 11 AM CT lock) vs current market; no tick history"),
    7: ("live", "DraftKings Network public betting splits page (edge.py)"),
    8: ("partial", "game dossier injury facts + news.json (only games with a dossier or a story)"),
    9: ("partial", "CFBD player season passing stats (only when the starter is out)"),
    10: ("partial", "game dossier injury facts"),
    11: ("partial", "dossier + news.json keyword match"),
    12: ("partial", "dossier + news.json keyword match"),
    13: ("partial", "dossier + news.json keyword match"),
    14: ("none", "no depth-chart feed"),
    15: ("partial", "news.json + dossier + insider notes keyword match"),
    16: ("partial", "insider notes coaching-carousel section + news.json"),
    17: ("partial", "CFBD lines: team ATS this season as favorite/underdog (no multi-year coach ATS)"),
    18: ("live", "CFBD schedule (game dates)"),
    19: ("live", "CFBD schedule (game dates)"),
    20: ("live", "CFBD schedule + SP+ (big game = top-25 SP+ opponent next week)"),
    21: ("live", "CFBD schedule, scores and lines (last week's upset win)"),
    22: ("live", "CFBD schedule + SP+"),
    23: ("live", "CFBD last season's lines/scores"),
    24: ("none", "no rivalry list in any cached source"),
    25: ("live", "CFBD records + schedule"),
    26: ("partial", "ESPN AP rank + CFBD conference record (no clinch/elimination feed)"),
    27: ("live", "CFBD schedule (last home game)"),
    28: ("partial", "ESPN AP rank (committee incentive approximated by a top-12 AP team)"),
    29: ("live", "CFBD lines: home team's home ATS this season"),
    30: ("live", "CFBD venues lat/lon + timezone, team home stadiums from the schedule"),
    31: ("live", "all_games kickoff time"),
    32: ("live", "CFBD venues elevation"),
    33: ("live", "ESPN forecast, Open-Meteo fallback (edge.py)"),
    34: ("live", "ESPN forecast / Open-Meteo + CFBD venues (dome, latitude)"),
    35: ("live", "CFBD venues grass flag"),
    36: ("live", "CFBD games neutralSite"),
    37: ("live", "CFBD advanced season stats (PPA per play)"),
    38: ("live", "CFBD advanced season stats (success rate)"),
    39: ("live", "CFBD advanced season stats (explosiveness)"),
    40: ("live", "CFBD advanced season stats (rushing PPA, line yards, stuff rate)"),
    41: ("partial", "CFBD player rushing stats for the QB vs run defense (no QB-run-allowed split)"),
    42: ("live", "CFBD advanced season stats (passing PPA)"),
    43: ("live", "CFBD advanced havoc + team sacks"),
    44: ("live", "CFBD team season stats (third downs)"),
    45: ("live", "CFBD advanced points per scoring opportunity"),
    46: ("live", "CFBD player kicking stats (FG%)"),
    47: ("live", "CFBD team season stats (turnovers, fumbles)"),
    48: ("live", "CFBD advanced plays per game"),
    49: ("live", "CFBD SP+ and SRS, ESPN FPI (all_games.fpi_margin)"),
    50: ("live", "CFBD lines: last 4 games ATS"),
    51: ("live", "CFBD lines this season + last season, team level (no QB-vs-opponent split: no player game logs cached)"),
    52: ("live", "CFBD advanced run rate (offense) vs run rate faced (defense)"),
    53: ("partial", "spread size + underdog passing explosiveness (no garbage-time split)"),
    54: ("partial", "news.json + insider notes (realignment) naming the team"),
    55: ("partial", "news.json + insider notes (revenue sharing / NIL) naming the team"),
    56: ("partial", "news.json keyword match (investigation, AD change, coach search)"),
    57: ("none", "no clinch, seeding or resting-starters feed"),
    58: ("live", "Supabase graded picks (both seasons): each rival's tendencies"),
    59: ("live", "Supabase graded picks: predicted league lean from past tendencies"),
    60: ("live", "Supabase standings: asker's gap to the leader / playoff cut line"),
}

NAMES = {
    1: "line value", 2: "line move", 3: "cause of the move", 4: "key numbers", 5: "total",
    6: "after the lock", 7: "betting splits", 8: "QB status", 9: "backup QB", 10: "key injuries",
    11: "suspensions", 12: "opt-outs", 13: "portal departures", 14: "depth chart", 15: "coaching change",
    16: "hot seat", 17: "team ATS", 18: "bye", 19: "short week", 20: "lookahead", 21: "letdown",
    22: "sandwich", 23: "revenge", 24: "rivalry", 25: "bowl eligibility", 26: "title stakes",
    27: "Senior Day", 28: "style points", 29: "home field", 30: "travel", 31: "kickoff time",
    32: "altitude", 33: "wind", 34: "rain/cold", 35: "turf", 36: "neutral site", 37: "EPA",
    38: "success rate", 39: "explosiveness", 40: "run game", 41: "running QB", 42: "pass game",
    43: "havoc/sacks", 44: "third downs", 45: "red zone", 46: "kicker", 47: "turnovers",
    48: "tempo", 49: "power ratings", 50: "recent form", 51: "head-to-head", 52: "style clash",
    53: "backdoor", 54: "realignment", 55: "NIL/roster turmoil", 56: "program turmoil",
    57: "CFP seeding", 58: "rivals' tendencies", 59: "league lean", 60: "standings need",
}


def _n(x) -> str:
    x = round(float(x), 1)
    return str(int(x)) if x == int(x) else str(x)


def F(n: int, side: str | None, w: float, why: str, flag: bool = False) -> dict:
    return {"n": n, "name": NAMES[n], "side": side if w else None, "w": float(w) if side else 0.0,
            "why": why, "flag": flag}


def side_label(side: str, g: dict) -> str:
    """'Oklahoma -7.5' / 'Texas A&M +7.5': a side as written on the league line."""
    ln = float(g["line"])
    if ln == 0:
        return f"{side} (pick'em)"
    fav = g["home"] if ln < 0 else g["away"]
    return f"{side} {'-' if side == fav else '+'}{_n(abs(ln))}"


def league_line_text(g: dict) -> str:
    ln = float(g["line"])
    if ln == 0:
        return "pick'em"
    fav, dog = (g["home"], g["away"]) if ln < 0 else (g["away"], g["home"])
    return f"{fav} -{_n(abs(ln))} / {dog} +{_n(abs(ln))}"


def _other(g: dict, side: str) -> str:
    return g["home"] if side == g["away"] else g["away"]


# -----------------------------------------------------------------------------
# raw CFBD datasets not in cfbd.build_index (games, srs, last season's lines)
# -----------------------------------------------------------------------------

_raw_memo: dict[str, tuple[float, object]] = {}


def raw(name: str, db_path=None):
    """A cached CFBD dataset as parsed JSON (None when absent). Memoized on
    fetched_at so a refresh is picked up without a restart."""
    try:
        con = sqlite3.connect(f"file:{db_path or cfbd.DB_PATH}?mode=ro", uri=True)
        row = con.execute("SELECT fetched_at, json FROM datasets WHERE name=?", (name,)).fetchone()
        con.close()
    except sqlite3.Error:
        return None
    if not row:
        return None
    hit = _raw_memo.get(name)
    if hit and hit[0] == row[0]:
        return hit[1]
    data = json.loads(row[1])
    _raw_memo[name] = (row[0], data)
    return data


def _venue_by_id(idx: dict) -> dict:
    return {v.get("id"): v for v in (idx or {}).get("venues") or []}


def _games_by_team(games: list | None) -> dict:
    out = collections.defaultdict(list)
    for g in games or []:
        out[cfbd.norm(g.get("homeTeam"))].append(g)
        out[cfbd.norm(g.get("awayTeam"))].append(g)
    return out


def _start(g: dict) -> dt.datetime | None:
    s = g.get("startDate") or g.get("start_date")
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None


def _line(g: dict) -> tuple[float | None, float | None, float | None]:
    """(home spread now, home spread open, over/under) from a CFBD lines row,
    DraftKings first, then whoever has a spread."""
    ls = [x for x in g.get("lines") or [] if x.get("spread") is not None]
    if not ls:
        return None, None, None
    ls.sort(key=lambda x: (x.get("provider") != "DraftKings", x.get("provider") or ""))
    x = ls[0]
    return float(x["spread"]), (float(x["spreadOpen"]) if x.get("spreadOpen") is not None else None), \
        (float(x["overUnder"]) if x.get("overUnder") is not None else None)


# -----------------------------------------------------------------------------
# per-team derived numbers (schedule, ATS)
# -----------------------------------------------------------------------------

def _is(team_row: dict | None, name: str | None) -> bool:
    return bool(team_row) and cfbd.norm(name) == cfbd.norm(team_row["name"])


def team_ats(t: dict | None) -> dict:
    """This season's ATS for a CFBD team row: {'all','fav','dog','home','away': [w,l,p],
    'last': [cover margins, oldest first]}. Cover margin > 0 = covered."""
    out = {k: [0, 0, 0] for k in ("all", "fav", "dog", "home", "away")}
    out["last"] = []
    if not t:
        return out
    done = [g for g in t.get("games") or [] if g.get("homeScore") is not None and g.get("awayScore") is not None]
    done.sort(key=lambda g: g.get("startDate") or "")
    for g in done:
        sp, _, _ = _line(g)
        if sp is None:
            continue
        home = _is(t, g.get("homeTeam"))
        hm = g["homeScore"] + sp - g["awayScore"]       # home covers when > 0
        m = hm if home else -hm
        spread_for_team = sp if home else -sp            # negative = team favored
        k = 0 if m > 0 else 1 if m < 0 else 2
        for key in ("all", "home" if home else "away", "fav" if spread_for_team < 0 else "dog"):
            out[key][k] += 1
        out["last"].append(m)
    return out


def _rec(r) -> str:
    return f"{r[0]}-{r[1]}" + (f"-{r[2]}" if r[2] else "")


def schedule(t: dict | None, games_raw_by_team: dict, season_week: int | None = None) -> list[dict]:
    """The team's games this season, oldest first, as
    {start, opp, home, neutral, venue_id, pts, opp_pts, spread_team}. CFBD /games
    when cached (has venues and neutral sites), else the lines rows."""
    if not t:
        return []
    rows = games_raw_by_team.get(cfbd.norm(t["name"])) or []
    lines_by_id = {g.get("id"): g for g in t.get("games") or []}
    src = rows or list(t.get("games") or [])
    out = []
    for g in src:
        home = _is(t, g.get("homeTeam"))
        lg = lines_by_id.get(g.get("id")) or (g if "lines" in g else {})
        sp, _, _ = _line(lg) if lg else (None, None, None)
        hs = g.get("homePoints", g.get("homeScore"))
        as_ = g.get("awayPoints", g.get("awayScore"))
        out.append({"id": g.get("id"), "start": _start(g), "week": g.get("week"),
                    "opp": g.get("awayTeam") if home else g.get("homeTeam"), "home": home,
                    "neutral": bool(g.get("neutralSite")), "venue_id": g.get("venueId"),
                    "pts": hs if home else as_, "opp_pts": as_ if home else hs,
                    "spread_team": (sp if home else -sp) if sp is not None else None,
                    "season_type": g.get("seasonType")})
    out = [x for x in out if x["start"] and x.get("season_type") in (None, "regular")]
    out.sort(key=lambda x: x["start"])
    return out


def _split(sched: list[dict], game_start: dt.datetime | None) -> tuple[list, list]:
    if not game_start:
        return sched, []
    before = [x for x in sched if x["start"] < game_start - dt.timedelta(hours=12)]
    after = [x for x in sched if x["start"] > game_start + dt.timedelta(hours=12)]
    return before, after


def _sp_rank(name: str | None, idx: dict) -> int | None:
    t = cfbd.resolve_team(name or "", idx) if name else None
    return ((t or {}).get("sp") or {}).get("rank")


def home_venue(t: dict | None, sched: list[dict], vby: dict) -> dict | None:
    """The stadium a team plays most of its true home games in."""
    c = collections.Counter(x["venue_id"] for x in sched if x["home"] and not x["neutral"] and x["venue_id"])
    if not c:
        return None
    return vby.get(c.most_common(1)[0][0])


def _haversine_mi(a: dict, b: dict) -> float | None:
    try:
        la1, lo1, la2, lo2 = (math.radians(float(v)) for v in (a["latitude"], a["longitude"],
                                                               b["latitude"], b["longitude"]))
    except (TypeError, KeyError, ValueError):
        return None
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 3959 * 2 * math.asin(math.sqrt(h))


_TZ_OFFSET = {"America/New_York": -5, "America/Detroit": -5, "America/Indiana/Indianapolis": -5,
              "America/Kentucky/Louisville": -5, "America/Chicago": -6, "America/Denver": -7,
              "America/Boise": -7, "America/Phoenix": -7, "America/Los_Angeles": -8,
              "Pacific/Honolulu": -10, "America/Anchorage": -9}


def _tz(v: dict | None) -> int | None:
    if not v:
        return None
    if v.get("timezone") in _TZ_OFFSET:
        return _TZ_OFFSET[v["timezone"]]
    try:
        return round(float(v["longitude"]) / 15)   # rough, only when CFBD has no timezone
    except (TypeError, KeyError, ValueError):
        return None


# -----------------------------------------------------------------------------
# intel (dossier facts, news, insider notes): text matched to a team
# -----------------------------------------------------------------------------

_OUT = re.compile(r"\b(ruled out|out for|will not play|won'?t play|is out|season[- ]ending|torn|surgery|"
                  r"miss(es|ing)? (the|saturday|this)|sidelined|not expected to play)\b", re.I)
_DOUBT = re.compile(r"\bdoubtful\b", re.I)
_QUES = re.compile(r"\b(questionable|game[- ]time decision|day[- ]to[- ]day|limited in practice|banged up)\b", re.I)
_AVAIL = re.compile(r"\b(will play|available|cleared|probable|ready to go|expected to start|returns?)\b", re.I)


def injury_status(text: str, given: str | None = None) -> str | None:
    if given in ("out", "doubtful", "questionable", "probable", "available"):
        return given
    t = text or ""
    if _AVAIL.search(t) and not _OUT.search(t):
        return "available"
    if _OUT.search(t):
        return "out"
    if _DOUBT.search(t):
        return "doubtful"
    if _QUES.search(t):
        return "questionable"
    return None


def intel_for(team: str, intel: list[dict], now: dt.datetime | None = None, days: int = 10) -> list[dict]:
    """Intel items about one team: dossier facts with team == that side, news
    items tagged with the team (not big roundups), insider lines naming it."""
    now = now or dt.datetime.now(dt.timezone.utc)
    key = cfbd.norm(team)
    out = []
    for it in intel or []:
        if cfbd.norm(it.get("team") or "") != key and key not in [cfbd.norm(x) for x in it.get("teams") or []]:
            continue
        d = it.get("date")
        if d:
            try:
                when = dt.datetime.fromisoformat(str(d).replace("Z", "+00:00"))
                if when.tzinfo is None:
                    when = when.replace(tzinfo=dt.timezone.utc)
                if (now - when).days > days:
                    continue
            except ValueError:
                pass
        out.append(it)
    return out


def _intel_text(it: dict) -> str:
    return f"{it.get('title') or ''} {it.get('text') or it.get('summary') or ''}".strip()


def _intel_src(it: dict) -> str:
    return it.get("label") or it.get("source") or it.get("source_name") or "report"


def _short(s: str, n: int = 140) -> str:
    s = re.sub(r"\s+", " ", s or "").strip()
    return s if len(s) <= n else s[: n - 1].rsplit(" ", 1)[0] + "..."


# -----------------------------------------------------------------------------
# the factors
# -----------------------------------------------------------------------------

def market_home(notes: dict | None, g: dict, t_home: dict | None, t_away: dict | None) -> dict | None:
    """{'now','open','ou','src'}: the market as a home spread. ESPN pickcenter
    first (10-min cache), CFBD's daily copy as the fallback."""
    m = (notes or {}).get("market") or {}
    if m.get("home_now") is not None:
        return {"now": float(m["home_now"]), "open": float(m["home_open"]) if m.get("home_open") is not None else None,
                "ou": m.get("over_under"), "src": m.get("book") or "ESPN"}
    for t in (t_home, t_away):
        for cg in (t or {}).get("games") or []:
            if cfbd.norm(cg.get("homeTeam")) == cfbd.norm((t_home or {}).get("name")) and \
                    cfbd.norm(cg.get("awayTeam")) == cfbd.norm((t_away or {}).get("name")) and cg.get("homeScore") is None:
                now, op, ou = _line(cg)
                if now is not None:
                    return {"now": now, "open": op, "ou": ou, "src": "CFBD"}
    return None


def f_market(g: dict, mk: dict | None, locked: bool) -> list[dict]:
    out = []
    pool = float(g["line"])
    if not mk:
        return out
    gap = pool - mk["now"]                     # > 0: market likes home more than the league does
    mtxt = league_line_text({**g, "line": mk["now"]})
    if abs(gap) < 0.5:
        out.append(F(1, None, 0, f"league line {league_line_text(g)} matches the market ({mk['src']} {mtxt}): "
                                 "no line value either way"))
    else:
        side = g["home"] if gap > 0 else g["away"]
        w = W_VALUE_BIG if abs(gap) >= VALUE_BIG_GAP else W_VALUE
        out.append(F(1, side, w, f"line value: the market ({mk['src']}) has {mtxt}, the league has "
                                 f"{league_line_text(g)}, so {side_label(side, g)} gets {_n(abs(gap))} more "
                                 "points than the market gives", flag=abs(gap) >= MOVE_FLAG))
        a, b = sorted((abs(pool), abs(mk["now"])))
        keys = [k for k in KEY_NUMBERS if a < k < b or (a < k <= b and b == k) or (a == k < b)]
        same_fav = (pool < 0) == (mk["now"] < 0) or pool == 0 or mk["now"] == 0
        if keys and same_fav:
            out.append(F(4, side, W_KEY, f"key number: the gap crosses {', '.join(map(str, keys))}, "
                                         f"so {side_label(side, g)} is on the good side of it"))
    if locked:
        out.append(F(6, None, 0, f"after the lock: league line frozen at {league_line_text(g)}, market now "
                                 f"{mtxt}" + ("" if abs(gap) < 0.5 else f", moved {_n(abs(gap))} since")))
    return out


def f_move(g: dict, mk: dict | None) -> dict | None:
    if not mk or mk.get("open") is None:
        return None
    d = mk["now"] - mk["open"]
    o, n = league_line_text({**g, "line": mk["open"]}), league_line_text({**g, "line": mk["now"]})
    if abs(d) < 0.5:
        return F(2, None, 0, f"line move: opened {o.split(' / ')[0]}, still there")
    side = g["home"] if d < 0 else g["away"]
    w = W_MED if abs(d) >= MOVE_MIN else 0
    return F(2, side, w, f"line move: opened {o.split(' / ')[0]}, now {n.split(' / ')[0]}, the market moved "
                         f"{_n(abs(d))} point{'' if abs(d) == 1 else 's'} toward {side}", flag=abs(d) >= MOVE_FLAG)


def f_total(g: dict, mk: dict | None) -> dict | None:
    ou = (mk or {}).get("ou")
    if ou is None:
        return None
    ou, sp = float(ou), float(g["line"])
    fav, dog = (g["home"], g["away"]) if sp < 0 else (g["away"], g["home"])
    fav_tt, dog_tt = (ou + abs(sp)) / 2, (ou - abs(sp)) / 2
    why = (f"total: over/under {_n(ou)}, implied {fav} {_n(round(fav_tt, 1))}, {dog} {_n(round(dog_tt, 1))}")
    if abs(sp) >= 14 and ou <= 45:
        return F(5, dog, W_LIGHT, why + f"; a big spread in a low-scoring game is hard for {fav} to cover")
    return F(5, None, 0, why)


def f_splits(g: dict, sides: dict | None) -> dict | None:
    if not sides:
        return None
    a, h = sides.get(g["away"]), sides.get(g["home"])
    if not a or not h or any(not 5 <= v[k] <= 95 for v in (a, h) for k in ("handle", "bets")):
        return None
    pub = g["away"] if a["bets"] > h["bets"] else g["home"]
    base = f"betting splits (DraftKings): {max(a['bets'], h['bets'])}% of spread bets on {pub}"
    da, dh = a["handle"] - a["bets"], h["handle"] - h["bets"]
    side, d = (g["away"], da) if da >= dh else (g["home"], dh)
    if d >= 15:
        v = sides[side]
        return F(7, side, W_LIGHT, f"{base}, but {v['handle']}% of the money is on {side}: bigger bettors there")
    return F(7, None, 0, base + ", money agrees with tickets")


def starting_qb(t: dict | None, idx: dict, notes_side: dict | None = None) -> str | None:
    """The team's QB: ESPN's passing leader, else CFBD's most pass attempts."""
    for ln in (notes_side or {}).get("leaders") or []:
        m = re.match(r"Passing Yards: (.+?) \(", ln)
        if m:
            return m.group(1).strip()
    if not t:
        return None
    best = None
    for p in (idx.get("by_team") or {}).get(cfbd.norm(t["name"])) or []:
        att = ((p.get("stats") or {}).get("passing") or {}).get("ATT") or 0
        if att and (best is None or att > best[0]):
            best = (att, p["name"])
    return best[1] if best else None


def _qbs(t: dict | None, idx: dict) -> list[tuple[str, int]]:
    if not t:
        return []
    rows = []
    for p in (idx.get("by_team") or {}).get(cfbd.norm(t["name"])) or []:
        att = ((p.get("stats") or {}).get("passing") or {}).get("ATT") or 0
        if att:
            rows.append((p["name"], int(att)))
    return sorted(rows, key=lambda r: -r[1])


def f_personnel(g: dict, side: str, t: dict | None, idx: dict, notes_side: dict | None,
                items: list[dict]) -> list[dict]:
    """8-13 for one team, from its intel items."""
    out = []
    opp = _other(g, side)
    qb = starting_qb(t, idx, notes_side)
    qb_last = (qb or "").split()[-1].lower() if qb else None
    qb_hit, inj = None, []
    for it in items:
        txt = _intel_text(it)
        kind = it.get("kind")
        # a news story tags every team it mentions: its injury counts for this
        # team only when it names one of this team's players (dossier facts are
        # already filed under a side)
        ours = _ours(it, t, idx)
        st = injury_status(txt, it.get("status")) if ours else None
        named = bool(qb and (qb.lower() in txt.lower() or (qb_last and it.get("team") is not None
                                                           and re.search(rf"\b{re.escape(qb_last)}\b", txt.lower()))))
        is_qb = named and (bool(re.search(r"\bQB\b|quarterback", txt)) or kind == "injury")
        if is_qb and st and (qb_hit is None or st in ("out", "doubtful")):
            qb_hit = (st, it)
        elif ours and st in ("out", "doubtful") and not is_qb and (kind in (None, "injury")):
            inj.append(it)
        low = txt.lower()
        if re.search(r"\bsuspen\w+|\bineligib\w+|academic", low):
            out.append(F(11, opp, W_LIGHT, f"suspension ({side}): {_short(txt)} ({_intel_src(it)})"))
        if re.search(r"\bopt(s|ed|ing)? out\b|\bsit(ting)? out .{0,30}draft|\bdeclar\w+ for the draft", low):
            out.append(F(12, opp, W_LIGHT, f"opt-out ({side}): {_short(txt)} ({_intel_src(it)})"))
        if re.search(r"\b(transfer portal|entered the portal|enter the portal|left the (team|program))\b", low):
            out.append(F(13, opp, W_LIGHT, f"departure ({side}): {_short(txt)} ({_intel_src(it)})"))
    if qb_hit:
        st, it = qb_hit
        w = {"out": W_QB_OUT, "doubtful": W_QB_DOUBT, "questionable": W_QB_Q}.get(st, 0)
        out.append(F(8, opp if w else None, w, f"QB status: {side} QB {qb} is {st} ({_short(_intel_text(it), 120)}, "
                                               f"{_intel_src(it)})", flag=st in ("out", "doubtful", "questionable")))
        if st in ("out", "doubtful"):
            backups = [r for r in _qbs(t, idx) if r[0] != qb]
            if backups:
                b = backups[0]
                out.append(F(9, None, 0, f"backup QB: {side}'s next QB by attempts is {b[0]} ({b[1]} passes this season)"))
            else:
                out.append(F(9, None, 0, f"backup QB: {side} has no other QB with a pass this season"))
    elif qb:
        out.append(F(8, None, 0, f"QB status: no injury report on {side} QB {qb} in the cached news"))
    if inj:
        w = min(W_INJ * len(inj), W_INJ_MAX)
        txt = "; ".join(_short(_intel_text(i), 90) for i in inj[:3])
        out.append(F(10, opp, w, f"key injuries ({side}): {txt}"))
    return out


def f_off_field(g: dict, side: str, items: list[dict]) -> list[dict]:
    out = []
    seen = set()
    for it in items:
        low = _intel_text(it).lower()
        src = _intel_src(it)
        for n, rx in ((15, r"\binterim (head )?coach\b|\b(fired|parted ways with|dismissed) (its |their |the )?(head )?coach|"
                           r"\bhead coach\b.{0,40}\b(fired|resign\w*|step(s|ping)? down|leaving for)\b"),
                      (16, r"\bhot seat\b|\bbuyout\b|\bjob security\b"),
                      (54, r"\brealign\w*|\bleaving the (conference|league)\b|\bjoin(s|ing) the (acc|sec|big|pac|mountain)"),
                      (55, r"\bnil\b|\brevenue[- ]shar\w*|\bcollective\b|\broster turmoil\b"),
                      (56, r"\binvestigat\w+|\bncaa (violation|inquiry|probe)|\bathletic director\b|\bcoach(ing)? search\b")):
            if n not in seen and re.search(rx, low):
                seen.add(n)
                out.append(F(n, None, 0, f"{NAMES[n]} ({side}): {_short(_intel_text(it))} ({src})", flag=n in (15, 56)))
    return out


def _ours(it: dict, t: dict | None, idx: dict) -> bool:
    """A dossier fact is filed under its side; a news story counts for a team
    only when it names one of that team's players."""
    if it.get("team") is not None:
        return True
    roster = {cfbd.norm(p["name"]) for p in (idx.get("by_team") or {}).get(cfbd.norm((t or {}).get("name"))) or []}
    return bool(roster & {cfbd.norm(x) for x in it.get("players") or []})


def f_cause(g: dict, move: dict | None, items_by_side: dict, teams: dict, idx: dict) -> dict | None:
    if not move or not move["side"]:
        return None
    for side in (g["away"], g["home"]):
        for it in items_by_side.get(side) or []:
            if not _ours(it, teams.get(side), idx):
                continue
            if it.get("kind") == "injury" or injury_status(_intel_text(it), it.get("status")) in ("out", "doubtful",
                                                                                               "questionable"):
                src = _intel_src(it)
                src = "" if "." in src or "/" in src else f" ({src})"  # members never get domains or links
                first = re.split(r"(?<=[.!?])\s+", re.sub(r"\s+", " ", _intel_text(it)).strip())[0]
                return F(3, None, 0, f"cause of the move: lines up with {side} news: "
                                     f"{_short(first, 200)}{src}")  # a whole sentence, never a cut-off one
    return F(3, None, 0, "cause of the move: no injury or news item in the cache explains it")


def f_ats(g: dict, ta: dict | None, th: dict | None) -> list[dict]:
    out = []
    sp = float(g["line"])
    roles = {g["home"]: "fav" if sp < 0 else "dog", g["away"]: "dog" if sp < 0 else "fav"}
    recs = {}
    for side, t in ((g["away"], ta), (g["home"], th)):
        a = team_ats(t)
        recs[side] = a
        r = a[roles[side]]
        if sum(r[:2]) >= 4:
            pct = r[0] / (r[0] + r[1])
            role = "favorite" if roles[side] == "fav" else "underdog"
            txt = f"team ATS: {side} is {_rec(r)} ATS as {'a favorite' if role == 'favorite' else 'an underdog'} " \
                  f"this season (overall {_rec(a['all'])})"
            if pct >= 0.7:
                out.append(F(17, side, W_LIGHT, txt))
            elif pct <= 0.3:
                out.append(F(17, _other(g, side), W_LIGHT, txt))
            else:
                out.append(F(17, None, 0, txt))
    hr = recs[g["home"]]["home"]
    if sum(hr[:2]) >= 3:
        pct = hr[0] / (hr[0] + hr[1])
        txt = f"home field: {g['home']} is {_rec(hr)} ATS at home this season"
        out.append(F(29, g["home"] if pct >= 0.7 else g["away"] if pct <= 0.3 else None,
                     W_LIGHT if pct >= 0.7 or pct <= 0.3 else 0, txt))
    for side in (g["away"], g["home"]):
        last = recs[side]["last"][-4:]
        if len(last) >= 4:
            cov = sum(1 for m in last if m > 0)
            txt = f"recent form: {side} covered {cov} of its last 4"
            if cov >= 4 or cov == 0:
                out.append(F(50, side if cov >= 4 else _other(g, side), W_LIGHT, txt))
            else:
                out.append(F(50, None, 0, txt))
    return out


def f_schedule(g: dict, side: str, t: dict | None, sched: list[dict], idx: dict, gstart: dt.datetime | None,
               opp_sched: list[dict]) -> list[dict]:
    """18-22, 25, 27 for one team."""
    out = []
    opp = _other(g, side)
    before, after = _split(sched, gstart)
    obefore, _ = _split(opp_sched, gstart)
    if before and obefore and gstart:
        rest = (gstart - before[-1]["start"]).days
        orest = (gstart - obefore[-1]["start"]).days
        if rest >= BYE_DAYS and orest < BYE_DAYS - 3:
            out.append(F(18, side, W_MED, f"bye: {side} is off a bye ({rest} days rest) vs {opp} on {orest}"))
        if rest <= SHORT_DAYS and orest >= rest + 2:
            out.append(F(19, opp, W_MED, f"short week: {side} plays on {rest} days rest, {opp} on {orest}"))
    sp = float(g["line"])
    fav = g["home"] if sp < 0 else g["away"] if sp > 0 else None
    opp_rank = _sp_rank(opp, idx)
    weak_now = opp_rank is None or opp_rank > BIG_GAME_SP_RANK
    if after and side == fav and weak_now:
        nxt = after[0]
        nr = _sp_rank(nxt["opp"], idx)
        if nr and nr <= BIG_GAME_SP_RANK:
            out.append(F(20, opp, W_MED, f"lookahead: {side} (favored) plays SP+ #{nr} {nxt['opp']} next"))
    if before and side == fav:
        last = before[-1]
        lr = _sp_rank(last["opp"], idx)
        won = last["pts"] is not None and last["opp_pts"] is not None and last["pts"] > last["opp_pts"]
        if won and ((last["spread_team"] or 0) > 0 or (lr and lr <= BIG_GAME_SP_RANK)):
            how = f"won outright as a {_n(last['spread_team'])}-point underdog" if (last["spread_team"] or 0) > 0 \
                else f"beat SP+ #{lr} {last['opp']}"
            if (last["spread_team"] or 0) > 0:
                how += f" vs {last['opp']}"
            out.append(F(21, opp, W_MED, f"letdown: {side} (favored) {how} last time out "
                                         f"({last['pts']}-{last['opp_pts']})"))
    if before and after and weak_now:
        pr, nr = _sp_rank(before[-1]["opp"], idx), _sp_rank(after[0]["opp"], idx)
        if pr and nr and pr <= BIG_GAME_SP_RANK and nr <= BIG_GAME_SP_RANK:
            out.append(F(22, opp, W_LIGHT, f"sandwich: {side} is between SP+ #{pr} {before[-1]['opp']} and "
                                           f"SP+ #{nr} {after[0]['opp']}"))
    rec = (t or {}).get("record") or ""
    m = re.match(r"(\d+)-(\d+)", rec)
    if m:
        w, l = int(m.group(1)), int(m.group(2))
        left = len(after) + 1
        if w == 5 and left <= 3:
            orec = re.match(r"(\d+)-(\d+)", (cfbd.resolve_team(opp, idx) or {}).get("record") or "")
            settled = orec and (int(orec.group(1)) >= 6 or int(orec.group(2)) >= 7)
            out.append(F(25, side if settled else None, W_LIGHT if settled else 0,
                         f"bowl eligibility: {side} is {rec}, one win from bowl-eligible"
                         + (f"; {opp} ({orec.group(0)}) has nothing on it" if settled else "")))
    if side == g["home"] and not g.get("neutral") and sched and not [x for x in after if x["home"]]:
        out.append(F(27, None, 0, f"Senior Day: this is {side}'s last home game"))
    return out


def f_travel(g: dict, ta: dict | None, th: dict | None, sched_a, sched_h, vby: dict, gstart, kick_ct) -> list[dict]:
    out = []
    venue = vby.get(g.get("venue_id")) if g.get("venue_id") else None
    hv_home = home_venue(th, sched_h, vby)
    venue = venue or hv_home
    av = home_venue(ta, sched_a, vby)
    if g.get("neutral"):
        out.append(F(36, None, 0, "neutral site: no home field (the projections drop the 2.5)"))
    if venue and av:
        mi = _haversine_mi(av, venue)
        tz_a, tz_g = _tz(av), _tz(venue)
        if mi is not None:
            txt = f"travel: {g['away']} travels {int(round(mi, -1))} miles"
            hours = (tz_g - tz_a) if tz_a is not None and tz_g is not None else 0
            early = kick_ct is not None and kick_ct.hour < EARLY_KICK_HOUR
            if hours >= TRAVEL_TZ and early:
                out.append(F(30, g["home"], W_MED, txt + f" and {hours} time zones east for an "
                                                        f"{kick_ct.strftime('%-I%p').lower()} CT kick (body clock "
                                                        f"{(kick_ct.hour - 6 - tz_a) % 12 or 12}am)"))
            else:
                out.append(F(30, None, 0, txt + (f", {abs(hours)} time zone(s)" if hours else "")))
        el_g, el_a = venue.get("elevation"), av.get("elevation")
        try:
            if el_g is not None and float(el_g) >= ALTITUDE_M and (el_a is None or float(el_a) <= LOW_HOME_M):
                out.append(F(32, g["home"], W_MED, f"altitude: {g['home']} plays at {int(float(el_g) * 3.281)} ft, "
                                                   f"{g['away']} lives near sea level"))
        except (TypeError, ValueError):
            pass
        if venue.get("grass") is not None and av.get("grass") is not None and venue["grass"] != av["grass"]:
            out.append(F(35, None, 0, f"turf: {'grass' if venue['grass'] else 'turf'} here, {g['away']} plays on "
                                      f"{'grass' if av['grass'] else 'turf'} at home"))
    if kick_ct is not None:
        night = kick_ct.hour >= 18
        out.append(F(31, None, 0, f"kickoff: {kick_ct.strftime('%a %-I:%M %p')} CT" + (", a night game" if night else "")))
    return out, venue, av


def f_weather(g: dict, wx: dict | None, dome: bool, av: dict | None, hv: dict | None) -> list[dict]:
    out = []
    if dome:
        return [F(33, None, 0, "weather: indoors, no weather factor")]
    if not wx:
        return out
    sp = float(g["line"])
    dog = g["away"] if sp < 0 else g["home"]
    wind = wx.get("wind_mph") or 0
    gust = wx.get("gust_mph") or 0
    bits = [f"{_n(wx['temp_f'])}F"] + ([f"wind {_n(wind)} mph"] if wx.get("wind_mph") is not None else []) + \
        ([f"gusts {_n(gust)} mph"] if wx.get("gust_mph") is not None else [])
    desc = f"forecast ({wx.get('src', 'forecast')}): " + ", ".join(bits)
    if (wind >= 15 or gust >= 25) and sp:
        out.append(F(33, dog, W_LIGHT, f"wind: {desc}, wind shortens games, a tilt to the points ({dog})", flag=True))
    else:
        out.append(F(33, None, 0, f"wind: {desc}, no real wind factor"))
    rain = wx.get("rain_pct") or 0
    temp = wx.get("temp_f")
    for side, v in ((g["away"], av),):
        warm = v and (v.get("dome") or (v.get("latitude") is not None and float(v["latitude"]) < 33))
        if warm and ((temp is not None and temp <= 35) or rain >= 70):
            why = f"{_n(temp)}F" if temp is not None and temp <= 35 else f"{_n(rain)}% chance of rain"
            out.append(F(34, _other(g, side), W_LIGHT, f"rain/cold: {why} for {side}, a "
                                                       f"{'dome' if v.get('dome') else 'warm-weather'} team",
                         flag=True))
            break
    else:
        if rain >= 70:
            out.append(F(34, dog if sp else None, W_LIGHT if sp else 0,
                         f"rain/cold: {_n(rain)}% chance of rain, sloppy games favor the points", flag=True))
    return out


def _adv(t, unit):
    return (t or {}).get("_adv_raw", {}).get(unit) or {}


def _rank_map(adv_rows: list, path: tuple, unit: str, higher_better: bool) -> dict:
    vals = []
    for r in adv_rows or []:
        v = r.get(unit) or {}
        for k in path:
            v = (v or {}).get(k) if isinstance(v, dict) else None
        if isinstance(v, (int, float)):
            vals.append((cfbd.norm(r.get("team")), float(v)))
    vals.sort(key=lambda x: -x[1] if higher_better else x[1])
    return {k: i + 1 for i, (k, _) in enumerate(vals)}


def f_matchup(g: dict, ta: dict | None, th: dict | None, adv_rows: list, idx: dict) -> list[dict]:
    """37-48, 52, 53 from CFBD advanced + team season stats."""
    out = []
    if not ta or not th:
        return out
    na, nh = cfbd.norm(ta["name"]), cfbd.norm(th["name"])

    def unit_edge(n, path, label, off_higher=True):
        """Each offense vs the defense it faces, by rank. One factor line."""
        ro = _rank_map(adv_rows, path, "offense", off_higher)
        rd = _rank_map(adv_rows, path, "defense", not off_higher)
        edges = []
        for side, o, d in ((g["away"], na, nh), (g["home"], nh, na)):
            if o in ro and d in rd:
                gap = rd[d] - ro[o]          # > 0: offense ranks better than the defense it faces
                edges.append((side, gap, ro[o], rd[d]))
        if len(edges) < 2:
            return None
        txt = f"{label}: " + "; ".join(f"{s} offense #{o} vs {_other(g, s)} defense #{d}" for s, _, o, d in edges)
        (s1, g1, _, _), (s2, g2, _, _) = edges
        net = g1 - g2                             # > 0 favors the away side
        if abs(net) >= RANK_GAP * 2:
            side = s1 if net > 0 else s2
            return F(n, side, W_LIGHT, txt + f", edge {side}")
        return F(n, None, 0, txt + ", no clear edge")

    for n, path, label, hb in ((38, ("successRate",), "success rate", True),
                               (39, ("explosiveness",), "explosiveness", True),
                               (40, ("rushingPlays", "ppa"), "run game (rushing PPA)", True),
                               (42, ("passingPlays", "ppa"), "pass game (passing PPA)", True),
                               (45, ("pointsPerOpportunity",), "red zone (points per scoring chance)", True)):
        x = unit_edge(n, path, label, hb)
        if x:
            out.append(x)
    hv = _rank_map(adv_rows, ("havoc", "total"), "defense", True)
    hv_o = _rank_map(adv_rows, ("havoc", "total"), "offense", False)
    if all(k in m for k, m in ((na, hv), (nh, hv), (na, hv_o), (nh, hv_o))):
        sa = (ta.get("stats") or {})
        sh = (th.get("stats") or {})
        sacks = lambda s: (s.get("sacks") or 0) / max(s.get("games") or 1, 1)  # noqa: E731
        txt = (f"havoc/sacks: {g['away']} defense havoc #{hv[na]} ({_n(round(sacks(sa), 1))} sacks/game) vs "
               f"{g['home']} offense #{hv_o[nh]}; {g['home']} defense havoc #{hv[nh]} "
               f"({_n(round(sacks(sh), 1))} sacks/game) vs {g['away']} offense #{hv_o[na]}")
        net = (hv_o[nh] - hv[na]) - (hv_o[na] - hv[nh])
        out.append(F(43, (g["away"] if net > 0 else g["home"]) if abs(net) >= RANK_GAP * 2 else None,
                     W_LIGHT if abs(net) >= RANK_GAP * 2 else 0, txt))
    from edge import efficiency_margin  # same PPA projection the matchup breakdown uses
    em = efficiency_margin(ta, th)
    if em is not None:
        if g.get("neutral"):
            em -= HOME_FIELD
        out.append(_by_margin(g, 37, em, "EPA (season PPA per play)", W_EFF))

    def pct(s, a, b):
        return (s.get(a) or 0) / s[b] * 100 if s.get(b) else None
    sa, sh = ta.get("stats") or {}, th.get("stats") or {}
    a3, h3 = pct(sa, "thirdDownConversions", "thirdDowns"), pct(sh, "thirdDownConversions", "thirdDowns")
    a3d, h3d = pct(sa, "thirdDownConversionsOpponent", "thirdDownsOpponent"), \
        pct(sh, "thirdDownConversionsOpponent", "thirdDownsOpponent")
    if None not in (a3, h3, a3d, h3d):
        net = (a3 - h3d) - (h3 - a3d)   # each offense vs what the other defense allows
        txt = (f"third downs: {g['away']} converts {_n(round(a3))}%, {g['home']} allows {_n(round(h3d))}%; "
               f"{g['home']} converts {_n(round(h3))}%, {g['away']} allows {_n(round(a3d))}%")
        out.append(F(44, (g["away"] if net > 0 else g["home"]) if abs(net) >= 10 else None,
                     W_LIGHT if abs(net) >= 10 else 0, txt))
    for side, s in ((g["away"], sa), (g["home"], sh)):
        gms = s.get("games") or 0
        if not gms:
            continue
        margin = ((s.get("turnoversOpponent") or 0) - (s.get("turnovers") or 0)) / gms
        rec_ = s.get("fumblesRecovered") or 0
        lost_ = s.get("fumblesLost") or 0
        txt = f"turnovers: {side} {'+' if margin >= 0 else ''}{_n(round(margin, 1))} per game"
        if abs(margin) >= 1.0:
            out.append(F(47, _other(g, side) if margin > 0 else side, W_LIGHT,
                         txt + f" (fumbles: recovered {rec_}, lost {lost_}); margins that big tend to regress, "
                               f"so the market likely {'over' if margin > 0 else 'under'}rates {side}"))
        else:
            out.append(F(47, None, 0, txt))
    pa = (_adv_row(adv_rows, na).get("offense") or {}).get("plays")
    ph = (_adv_row(adv_rows, nh).get("offense") or {}).get("plays")
    if pa and ph and sa.get("games") and sh.get("games"):
        ppg_a, ppg_h = pa / sa["games"], ph / sh["games"]
        sp = float(g["line"])
        txt = f"tempo: {g['away']} {_n(round(ppg_a))} plays/game, {g['home']} {_n(round(ppg_h))}"
        fav, dog = (g["home"], g["away"]) if sp < 0 else (g["away"], g["home"])
        if abs(sp) >= 10 and ppg_a + ppg_h >= 150:
            out.append(F(48, fav, W_LIGHT, txt + f": lots of snaps give {fav} room to cover a big number"))
        elif abs(sp) >= 10 and ppg_a + ppg_h <= 125:
            out.append(F(48, dog, W_LIGHT, txt + f": few snaps make a big number hard, a tilt to {dog}"))
        else:
            out.append(F(48, None, 0, txt))
    ra = {cfbd.norm(r.get("team")): r for r in adv_rows or []}
    for side, o, d in ((g["away"], na, nh), (g["home"], nh, na)):
        orate = (((ra.get(o) or {}).get("offense") or {}).get("rushingPlays") or {}).get("rate")
        if orate and orate >= 0.65:
            drate = (((ra.get(d) or {}).get("defense") or {}).get("rushingPlays") or {}).get("rate")
            txt = f"style clash: {side} runs on {_n(round(orate * 100))}% of plays"
            if drate is not None and drate < 0.5:
                out.append(F(52, side, W_LIGHT, txt + f"; {_other(g, side)}'s defense sees runs only "
                                                       f"{_n(round(drate * 100))}% of the time"))
            else:
                out.append(F(52, None, 0, txt))
    sp = float(g["line"])
    if abs(sp) >= 14:
        dog = g["away"] if sp < 0 else g["home"]
        er = _rank_map(adv_rows, ("passingPlays", "explosiveness"), "offense", True).get(cfbd.norm(
            (ta if dog == g["away"] else th)["name"]))
        if er:
            txt = f"backdoor: {dog} passing explosiveness #{er}"
            out.append(F(53, dog if er <= 40 else None, W_LIGHT if er <= 40 else 0,
                         txt + (", it can score late against prevent" if er <= 40 else ", not much backdoor threat")))
    return out


def _adv_row(adv_rows, key):
    return next((r for r in adv_rows or [] if cfbd.norm(r.get("team")) == key), {})


def _by_margin(g: dict, n: int, home_margin: float, src: str, w: float) -> dict:
    cover = home_margin + float(g["line"])
    fav = g["home"] if home_margin >= 0 else g["away"]
    if abs(cover) < 0.5:
        return F(n, None, 0, f"{src}: projects {fav} by {_n(round(abs(home_margin), 1))}, right on the league line")
    side = g["home"] if cover > 0 else g["away"]
    return F(n, side, w, f"{src}: projects {fav} by {_n(round(abs(home_margin), 1))}, so {side_label(side, g)} "
                         f"covers by {_n(round(abs(cover), 1))}")


def f_ratings(g: dict, ta, th, srs_rows) -> list[dict]:
    out = []
    hf = 0 if g.get("neutral") else HOME_FIELD
    sa, sh = ((ta or {}).get("sp") or {}).get("rating"), ((th or {}).get("sp") or {}).get("rating")
    if sa is not None and sh is not None:
        out.append(_by_margin(g, 49, float(sh) - float(sa) + hf, f"SP+ (ratings{' + 2.5 home field' if hf else ''})",
                              W_SP))
    if g.get("fpi_margin") is not None:
        out.append(_by_margin(g, 49, float(g["fpi_margin"]), "ESPN FPI", W_FPI))
    if srs_rows and ta and th:
        by = {cfbd.norm(r.get("team")): r.get("rating") for r in srs_rows}
        ra, rh = by.get(cfbd.norm(ta["name"])), by.get(cfbd.norm(th["name"]))
        if ra is not None and rh is not None:
            out.append(_by_margin(g, 49, float(rh) - float(ra) + hf, "SRS", W_SRS))
    return out


def f_h2h(g: dict, ta, th, prev_lines: list | None) -> list[dict]:
    """51 head-to-head (this season + last) and 23 revenge (lost last year)."""
    out = []
    if not ta or not th:
        return out
    pair = {cfbd.norm(ta["name"]), cfbd.norm(th["name"])}
    kick = _kick_ct(g)
    cut = kick.replace(tzinfo=dt.timezone(dt.timedelta(hours=-6))) if kick else None
    meets = [x for x in (prev_lines or []) + list(ta.get("games") or [])
             if {cfbd.norm(x.get("homeTeam")), cfbd.norm(x.get("awayTeam"))} == pair
             and x.get("homeScore") is not None and x.get("awayScore") is not None
             and str(x.get("id")) != str(g.get("id"))
             and not (cut and _start(x) and _start(x) >= cut - dt.timedelta(hours=12))]
    if not meets:
        return out
    meets.sort(key=lambda x: x.get("startDate") or "")
    bits = []
    for x in meets[-3:]:
        sp, _, _ = _line(x)
        hm = x["homeScore"] - x["awayScore"]
        winner = x["homeTeam"] if hm > 0 else x["awayTeam"]
        score = f"{winner} won {max(x['homeScore'], x['awayScore'])}-{min(x['homeScore'], x['awayScore'])}"
        if sp is not None:  # ATS first: who covered that day's market line, the score as context
            cm = hm + sp
            cov = x["homeTeam"] if cm > 0 else x["awayTeam"] if cm < 0 else None
            s = (f"{x.get('season')}: {cov} covered the market line by {_n(abs(cm))} ({score})" if cov else
                 f"{x.get('season')}: landed on the market line ({score})")
        else:
            s = f"{x.get('season')}: {score} (no line on record)"
        bits.append(s)
    out.append(F(51, None, 0, "head-to-head: " + "; ".join(bits)))
    last = meets[-1]
    if last.get("season") == g.get("season", 0) - 1 or (g.get("season") is None and last in (prev_lines or [])):
        hm = last["homeScore"] - last["awayScore"]
        loser = last["awayTeam"] if hm > 0 else last["homeTeam"]
        side = g["away"] if cfbd.norm(loser) == cfbd.norm(ta["name"]) else g["home"]
        by = abs(hm)
        out.append(F(23, side, W_MED if by >= 14 else W_LIGHT,
                     f"revenge: {side} lost this game last season by {by}"))
    return out


def f_stakes(g: dict, notes: dict | None, ta, th) -> list[dict]:
    out = []
    ranks = {g["away"]: ((notes or {}).get("away") or {}).get("ap_rank"),
             g["home"]: ((notes or {}).get("home") or {}).get("ap_rank")}
    recs = {g["away"]: (ta or {}).get("record"), g["home"]: (th or {}).get("record")}
    sp = float(g["line"])
    fav = g["home"] if sp < 0 else g["away"] if sp > 0 else None
    alive = {}
    for side in (g["away"], g["home"]):
        m = re.match(r"(\d+)-(\d+)", recs[side] or "")
        if not m:
            continue
        l = int(m.group(2))
        alive[side] = bool(ranks[side]) and l <= 2
    if len(alive) == 2 and alive[g["away"]] != alive[g["home"]]:
        side = g["away"] if alive[g["away"]] else g["home"]
        out.append(F(26, side, W_LIGHT, f"title stakes: {side} (AP #{ranks[side]}, {recs[side]}) is in the playoff "
                                        f"race, {_other(g, side)} ({recs[_other(g, side)]}) isn't"))
    if fav and ranks.get(fav) and ranks[fav] <= 12 and not ranks.get(_other(g, fav)):
        out.append(F(28, fav, W_LIGHT, f"style points: {fav} (AP #{ranks[fav]}) wants a big win for the committee"))
    return out


def f_qb_run(g, ta, th, idx, notes, adv_rows) -> list[dict]:
    out = []
    rr = _rank_map(adv_rows, ("rushingPlays", "ppa"), "defense", False)
    for side, t, nside in ((g["away"], ta, "away"), (g["home"], th, "home")):
        qb = starting_qb(t, idx, (notes or {}).get(nside))
        if not qb or not t:
            continue
        p = next((p for p in (idx.get("by_team") or {}).get(cfbd.norm(t["name"])) or [] if p["name"] == qb), None)
        yds = (((p or {}).get("stats") or {}).get("rushing") or {}).get("YDS")
        gms = (t.get("stats") or {}).get("games") or 0
        if yds is None or not gms:
            continue
        ypg = float(yds) / gms
        if ypg < 35:
            continue
        opp = cfbd.resolve_team(_other(g, side), idx)
        dr = rr.get(cfbd.norm((opp or {}).get("name")))
        txt = f"running QB: {qb} runs for {_n(round(ypg))} yards a game"
        if dr and dr >= 90:
            out.append(F(41, side, W_LIGHT, txt + f"; {_other(g, side)} run defense ranks #{dr} (rushing PPA)"))
        else:
            out.append(F(41, None, 0, txt + (f"; {_other(g, side)} run defense #{dr}" if dr else "")))
    return out


def f_kicker(g, ta, th, idx) -> list[dict]:
    out = []
    for side, t in ((g["away"], ta), (g["home"], th)):
        if not t:
            continue
        best = None
        for p in (idx.get("by_team") or {}).get(cfbd.norm(t["name"])) or []:
            k = (p.get("stats") or {}).get("kicking") or {}
            if (k.get("FGA") or 0) >= 6 and (best is None or k["FGA"] > best[1]["FGA"]):
                best = (p["name"], k)
        if not best:
            continue
        name, k = best
        pct = 100 * (k.get("FGM") or 0) / k["FGA"]
        txt = f"kicker: {side}'s {name} is {k.get('FGM')}/{k['FGA']} on field goals ({_n(round(pct))}%)"
        out.append(F(46, _other(g, side) if pct < 65 else None, W_LIGHT if pct < 65 else 0, txt))
    return out


# -----------------------------------------------------------------------------
# game theory (58-60)
# -----------------------------------------------------------------------------

def tendencies(history: list[dict]) -> dict:
    """history: graded picks on FINAL games only, [{team, pick, fav, home, crowd}]
    (fav/home = that game's favorite/home side; crowd = the side most of the
    league took). -> {team: {'n','fav','home','crowd'}} as fractions."""
    acc = collections.defaultdict(lambda: [0, 0, 0, 0])
    for h in history:
        a = acc[h["team"]]
        a[0] += 1
        a[1] += h["pick"] == h.get("fav")
        a[2] += h["pick"] == h.get("home")
        a[3] += h["pick"] == h.get("crowd")
    return {t: {"n": n, "fav": f / n, "home": hm / n, "crowd": c / n} for t, (n, f, hm, c) in acc.items() if n}


def predict(t: dict | None, g: dict) -> str | None:
    """A member's likely side from past tendencies only (never their real pick)."""
    if not t or t["n"] < 8:
        return None
    sp = float(g["line"])
    fav = g["home"] if sp < 0 else g["away"] if sp > 0 else None
    if fav and t["fav"] >= 0.6:
        return fav
    if fav and t["fav"] <= 0.4:
        return _other(g, fav)
    if t["home"] >= 0.62:
        return g["home"]
    if t["home"] <= 0.38:
        return g["away"]
    return None


def _pct(x: float) -> str:
    return f"{round(x * 100)}%"


def game_theory(g: dict, asker: str | None, standings: list[dict], tend: dict, cut_rank: int = 4) -> list[dict]:
    """58-60 for the asker. standings: [{team, w, l, rank}] best first."""
    if not asker or not standings:
        return []
    me = next((r for r in standings if r["team"] == asker), None)
    if not me:
        return []
    out = []
    rivals = [r for r in standings if r["team"] != asker and abs(r["w"] - me["w"]) <= 3][:4]
    if not rivals:
        rivals = sorted((r for r in standings if r["team"] != asker), key=lambda r: abs(r["w"] - me["w"]))[:2]
    bits, preds = [], []
    for r in rivals:
        t = tend.get(r["team"])
        p = predict(t, g)
        if t:
            lean = (f"takes the favorite {_pct(t['fav'])}" if abs(t["fav"] - 0.5) >= abs(t["home"] - 0.5)
                    else f"takes the home team {_pct(t['home'])}")
            bits.append(f"{r['team']} ({r['w']} pts) {lean}" + (f", so probably {p}" if p else ""))
        if p:
            preds.append(p)
    if bits:
        out.append(F(58, None, 0, "rivals' tendencies (past graded picks, not this week's): " + "; ".join(bits)))
    league = [predict(tend.get(r["team"]), g) for r in standings]
    league = [p for p in league if p]
    if league:
        c = collections.Counter(league).most_common(1)[0]
        out.append(F(59, None, 0, f"league lean (from past habits): about {c[1]} of {len(league)} members "
                                  f"with a clear habit would land on {c[0]}"))
    leader = standings[0]
    cut = next((r for r in standings if r["rank"] >= cut_rank), standings[min(cut_rank - 1, len(standings) - 1)])
    gap_lead = leader["w"] - me["w"]
    second = next((r for r in standings if r["team"] != asker), None)
    rc = collections.Counter(preds).most_common(1)
    crowd = rc[0][0] if rc and rc[0][1] >= max(2, len(preds) // 2 + 1) else None
    if me["rank"] == 1 and second and me["w"] - second["w"] >= 1:
        need = f"{asker} leads by {me['w'] - second['w']}: protect it, match the field"
        side = crowd
    elif gap_lead >= 3 or me["w"] < cut["w"]:
        behind = f"{gap_lead} behind the leader" + (f", {cut['w'] - me['w']} behind the playoff cut"
                                                    if me["w"] < cut["w"] else "")
        need = f"{asker} is {behind}: chasing, so fade the crowd"
        side = _other(g, crowd) if crowd else None
    else:
        need = f"{asker} is {gap_lead} off the lead: no need to get cute"
        side = None
    if crowd:
        need += f" (rivals' habits point to {crowd})"
    out.append(F(60, side, W_THEORY if side else 0, "standings need: " + need))
    return out


# -----------------------------------------------------------------------------
# the brief
# -----------------------------------------------------------------------------

def line_lock(game_start: dt.datetime | None) -> dt.datetime | None:
    """When the league line freezes: Tuesday 11:00 AM CT of the game's week, a port of the site's
    public.line_lock_at() (supabase/migrations/20260822010000_line_locks_tuesday.sql): the week's
    Saturday (Sun/Mon belong to the Saturday before) minus 4 days, at 11:00."""
    if not game_start:
        return None
    d = game_start.date()
    dow = (d.weekday() + 1) % 7                     # Postgres dow: Sun 0 .. Sat 6
    sat = d - dt.timedelta(days=1) if dow == 0 else d - dt.timedelta(days=2) if dow == 1 else \
        d + dt.timedelta(days=6 - dow)
    return dt.datetime.combine(sat - dt.timedelta(days=4), dt.time(11, 0))


def _kick_ct(g: dict) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(str(g.get("start"))).replace(tzinfo=None)
    except (TypeError, ValueError):
        return None


def build(g: dict, notes: dict | None = None, idx: dict | None = None, extras: dict | None = None,
          intel: list[dict] | None = None, asker: str | None = None, standings: list[dict] | None = None,
          tend: dict | None = None, now_ct: dt.datetime | None = None) -> dict:
    """Every factor for one game against its league line.

    g: {id, away, home, line (league home line), start (CT iso), season, week,
        fpi_margin, neutral?, venue_id?}
    Returns {'factors': [...], 'lean', 'strength', 'score', 'pick', 'top', 'flags',
             'locked', 'lock_at', 'missing': [factor numbers with no data here]}."""
    idx = idx or {}
    extras = extras or {}
    now_ct = now_ct or dt.datetime.now().replace(tzinfo=None)
    kick = _kick_ct(g)
    lock_at = line_lock(kick)
    locked = bool(lock_at and now_ct >= lock_at)
    ta = cfbd.resolve_team(g["away"], idx) if idx else None
    th = cfbd.resolve_team(g["home"], idx) if idx else None
    adv_rows = raw("advanced") or []
    games_raw = raw("games")
    gbt = _games_by_team(games_raw)
    vby = _venue_by_id(idx)
    # neutral site / venue from CFBD /games when the pool row doesn't say
    if games_raw and ta and th and "neutral" not in g:
        cg = next((x for x in gbt.get(cfbd.norm(th["name"])) or []
                   if cfbd.norm(x.get("awayTeam")) == cfbd.norm(ta["name"]) and x.get("homePoints") is None), None)
        if cg:
            g = {**g, "neutral": bool(cg.get("neutralSite")), "venue_id": cg.get("venueId")}
    sched_a, sched_h = schedule(ta, gbt), schedule(th, gbt)
    gstart = None
    if kick:
        gstart = kick.replace(tzinfo=dt.timezone(dt.timedelta(hours=-6)))
    factors: list[dict] = []
    mk = market_home(notes, g, th, ta)
    factors += f_market(g, mk, locked)
    mv = f_move(g, mk)
    if mv:
        factors.append(mv)
    for x in (f_total(g, mk), f_splits(g, (extras.get("splits") or {}).get(g.get("id")))):
        if x:
            factors.append(x)
    items = {side: intel_for(side, intel or []) for side in (g["away"], g["home"])}
    for side, t, ns in ((g["away"], ta, "away"), (g["home"], th, "home")):
        factors += f_personnel(g, side, t, idx, (notes or {}).get(ns), items[side])
        factors += f_off_field(g, side, items[side])
    c = f_cause(g, mv if mv and mv["w"] else None, items, {g["away"]: ta, g["home"]: th}, idx)
    if c:
        factors.append(c)
    factors += f_ats(g, ta, th)
    factors += f_schedule(g, g["away"], ta, sched_a, idx, gstart, sched_h)
    factors += f_schedule(g, g["home"], th, sched_h, idx, gstart, sched_a)
    tr, venue, av = f_travel(g, ta, th, sched_a, sched_h, vby, gstart, kick)
    factors += tr
    dome = bool((extras.get("dome") or {}).get(g.get("id"))) or bool(venue and venue.get("dome"))
    factors += f_weather(g, (extras.get("weather") or {}).get(g.get("id")), dome, av, venue)
    factors += f_ratings(g, ta, th, raw("srs"))
    factors += f_matchup(g, ta, th, adv_rows, idx)
    factors += f_qb_run(g, ta, th, idx, notes, adv_rows)
    factors += f_kicker(g, ta, th, idx)
    factors += f_h2h(g, ta, th, raw("lines_prev"))
    factors += f_stakes(g, notes, ta, th)
    factors += game_theory(g, asker, standings or [], tend or {})
    return score(g, factors, locked, lock_at)


def score(g: dict, factors: list[dict], locked: bool = False, lock_at=None) -> dict:
    tot = collections.Counter()
    for f in factors:
        if f["side"]:
            tot[f["side"]] += f["w"]
    a, b = tot.get(g["away"], 0.0), tot.get(g["home"], 0.0)
    lean = g["away"] if a > b else g["home"] if b > a else None
    hi, lo = max(a, b), min(a, b)
    strength = "strong" if lean and hi >= STRONG_MIN and lo <= hi / 3 else "lean" if lean else "none"
    if lean:
        pick, why = lean, "factor score"
    else:  # dead even: league game theory, else take the points
        gt = next((f for f in factors if f["n"] == 60 and f["side"]), None)
        sp = float(g["line"])
        pick = gt["side"] if gt else (g["away"] if sp < 0 else g["home"]) if sp else g["home"]
        why = "standings need" if gt else "coin flip: take the points"
    top = sorted((f for f in factors if f["side"]), key=lambda f: (-f["w"], f["n"]))[:TOP_EDGES]
    flags = [f for f in factors if f["flag"] and f not in top]
    have = {f["n"] for f in factors}
    missing = sorted(n for n, (st, _) in STATUS.items() if st != "none" and n not in have)
    return {"game": g, "factors": factors, "lean": lean, "strength": strength,
            "score": {g["away"]: round(a, 1), g["home"]: round(b, 1)},
            "pick": pick, "pick_label": side_label(pick, g), "pick_why": why,
            "top": top, "flags": flags, "locked": locked, "lock_at": lock_at, "missing": missing}


def _lock_text(b: dict) -> str:
    if b["locked"]:
        return "locked"
    la = b.get("lock_at")
    return f"not locked yet, locks {la:%a} 11 AM CT" if la else "lock time unknown"


def brief_note(b: dict) -> str:
    """The compact prompt block: recommendation, 3-5 strongest edges, red flags."""
    g = b["game"]
    s = b["score"]
    head = (f"PICK BRIEF, {g['away']} at {g['home']}: league line {league_line_text(g)} ({_lock_text(b)}). "
            "ATS only: the pick is who COVERS the league line, not who wins the game.")
    conf = {"strong": "strong lean", "lean": "lean", "none": "coin flip"}[b["strength"]]
    rec = (f"RECOMMENDATION: {b['pick_label']} ({conf}; factor score {g['away']} {_n(s[g['away']])}, "
           f"{g['home']} {_n(s[g['home']])}" + ("" if b["lean"] else f"; {b['pick_why']}") + ")")
    lines = [head, rec, "STRONGEST EDGES:"]
    for f in b["top"][:TOP_EDGES]:
        lines.append(f"- for {f['side']}: {f['why']}")
    if not b["top"]:
        lines.append("- none: no factor leans either way")
    if b["flags"]:
        lines.append("RED FLAGS:")
        lines += [f"- {f['why']}" for f in b["flags"][:3]]
    return "\n".join(lines)


def full_note(b: dict) -> str:
    """Every computed factor, for 'why?' / 'give me everything'."""
    g = b["game"]
    lines = [f"FULL FACTOR TABLE, {g['away']} at {g['home']}, league line {league_line_text(g)} "
             f"({_lock_text(b)}). Pick: {b['pick_label']}. Each line: factor number, name, the side it helps "
             "cover (or none), weight, reason."]
    for f in sorted(b["factors"], key=lambda f: (f["n"], -f["w"])):
        lines.append(f"{f['n']}. {f['name']}: {f['side'] or 'none'} ({_n(f['w'])}) {f['why']}")
    if b["missing"]:
        lines.append("Nothing notable or no data for this game: " + ", ".join(NAMES[n] for n in b["missing"]))
    nos = [NAMES[n] for n, (st, _) in STATUS.items() if st == "none"]
    lines.append("Never available (no source): " + ", ".join(nos))
    return "\n".join(lines)


def live_count() -> dict:
    c = collections.Counter(st for st, _ in STATUS.values())
    return {"live": c["live"], "partial": c["partial"], "none": c["none"]}


# -----------------------------------------------------------------------------
# intel loaders (cache files only, never the network)
# -----------------------------------------------------------------------------

def dossier_intel(d: dict | None) -> list[dict]:
    if not d:
        return []
    out = []
    for f in d.get("facts") or []:
        team = d.get("away") if f.get("team") == "away" else d.get("home") if f.get("team") == "home" else None
        if team:
            out.append({"team": team, "text": f.get("text"), "kind": f.get("kind"), "status": f.get("status"),
                        "date": f.get("date"), "source": f.get("source_name")})
    return out


def news_intel(cache: dict | None, teams: list[str], max_tags: int = 3) -> list[dict]:
    """News items tagged with one of `teams` and at most `max_tags` teams (a
    bowl-projection roundup tags 60 teams and says nothing about any one)."""
    keys = {cfbd.norm(t) for t in teams}
    out = []
    for it in ((cache or {}).get("items") or {}).values():
        tags = it.get("teams") or []
        if not tags or len(tags) > max_tags or not keys & {cfbd.norm(t) for t in tags} or it.get("superseded_by"):
            continue  # superseded: a newer status / confirmation replaced it (news.py)
        out.append({"teams": tags, "players": it.get("players") or [], "title": it.get("title"), "summary": it.get("summary"), "date": it.get("date"),
                    "source": "Dellenger" if it.get("dellenger") else it.get("source"),
                    "kind": it.get("kind"), "status": it.get("status")})  # news.py's injury tagging
    return out


_followup = re.compile(r"\b(why|how come|explain|everything|all (the |your )?(factors|reasons)|full (breakdown|brief|"
                       r"table|list)|show (me )?(your|the) work|what else|go deeper|more detail|break it all down)\b",
                       re.I)


def wants_full(text: str) -> bool:
    return bool(_followup.search(text or ""))

