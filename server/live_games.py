"""
Game-day live mode for Picky: a plain-code poller (no LLM) that keeps a live
state per game from ESPN, and the LIVE rider chat.py puts in front of a
question that touches a live game.

Sources (public, keyless JSON, the same site.api.espn.com the edge functions use):
  scoreboard  every SB_EVERY s while any FBS game is live or about to kick off
              (score, clock, quarter, possession, down & distance, last play),
              every IDLE_EVERY s otherwise.
  summary     per live game: pool games every POOL_EVERY s, ranked games every
              OTHER_EVERY s, any other FBS game every REST_EVERY s (at most
              MAX_DETAIL games at once): scoring plays with
              who/how/yards/time, turnovers and big plays, "injured on the play"
              notes and QB changes from the play-by-play, ESPN headlines that
              name a team in the game.
ESPN sends no ETag/Last-Modified (checked 2026-11-16), so "conditional" here
means: send If-None-Match / If-Modified-Since only if a validator ever shows up,
gzip always, honor Retry-After, exponential backoff on errors (cap 5 min).

The poller writes server/cache/live_games.json atomically. chat.py only reads
it (mtime-memoized) and, when a game the question touches is live and its data
is older than REFRESH_AGE, fetches that game's summary itself before answering
(in-process overlay; it never writes the shared file). Data older than MAX_AGE
during a live game is never given as current.

CLI: live_games.py run | once | show [team] | status
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
import sys
import threading
import time
import urllib.parse
from pathlib import Path

import httpx

log = logging.getLogger("picky.live")

HERE = Path(__file__).resolve().parent
BASE = os.environ.get("PICKY_LIVE_BASE",
                      "https://site.api.espn.com/apis/site/v2/sports/football/college-football").rstrip("/")
SCOREBOARD_URL = BASE + "/scoreboard?groups=80&limit=300"
SUMMARY_URL = BASE + "/summary?event="
CACHE = Path(os.environ.get("PICKY_LIVE_CACHE", str(HERE / "cache" / "live_games.json")))
SUPABASE_URL = "https://vopdioszofwdkwnujtiq.supabase.co"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Encoding": "gzip, deflate",
}

SB_EVERY = 20         # s, scoreboard while live (ESPN caches it ~6 s)
POOL_EVERY = 30       # s, summary for a live pool game
OTHER_EVERY = 60      # s, summary for another ranked live game
REST_EVERY = 120      # s, summary for any other live FBS game
IDLE_EVERY = 300      # s, scoreboard when nothing is live or close
SOON_S = 20 * 60      # a kickoff this close counts as live for polling
MAX_DETAIL = 40       # live games tracked play by play at once (pool, ranked, then the rest)
POOL_IDS_EVERY = 600  # s, re-read this week's pool game ids
REFRESH_AGE = 60      # s, chat refetches a live game it's about to talk about past this
MAX_AGE = 90          # s, a live game's data past this is never stated as current
BACKOFF_CAP = 300
KEEP_SCORING = 6
KEEP_KEY = 6
BIG_PLAY_YDS = 30
CT = dt.timezone(dt.timedelta(hours=-6))  # display only; CFB season is CST after early Nov

try:  # freshness.py owns the max ages ('live_game' per game, 'live_games' for the board file)
    import freshness as _freshness
except Exception:  # noqa: BLE001 - standalone (tests, replay) falls back to MAX_AGE
    _freshness = None


def game_max_age() -> float:
    try:
        return float(_freshness.max_age("live_game")) if _freshness else MAX_AGE
    except Exception:  # noqa: BLE001
        return MAX_AGE


def board_max_age() -> float:
    try:
        return float(_freshness.max_age("live_games")) if _freshness else MAX_AGE
    except Exception:  # noqa: BLE001
        return MAX_AGE

# ----------------------------------------------------------------------------
# small helpers
# ----------------------------------------------------------------------------


def _int(x):
    try:
        return int(float(x))
    except (TypeError, ValueError):
        return None


def _rank(c: dict):
    r = ((c.get("curatedRank") or {}).get("current"))
    r = _int(r)
    return r if r and r <= 25 else None


def _team(c: dict) -> dict:
    t = c.get("team") or {}
    return {"id": str(t.get("id") or ""), "name": t.get("location") or t.get("shortDisplayName")
            or t.get("displayName") or "?", "abbr": t.get("abbreviation") or "", "nick": t.get("name") or "",
            "rank": _rank(c)}


def period_label(p) -> str:
    p = _int(p) or 0
    return {1: "1Q", 2: "2Q", 3: "3Q", 4: "4Q"}.get(p, "OT" if p == 5 else f"{p - 4}OT" if p > 5 else "")


def clock_text(g: dict) -> str:
    """'2:41 3Q', 'Halftime', 'End 3Q', 'OT', 'Final', 'Final/OT', 'not started'."""
    st, sn, p = g.get("state"), (g.get("status_name") or "").upper(), g.get("period")
    if st == "post":
        if any(w in sn for w in ("POSTPONED", "CANCELED", "CANCELLED", "SUSPENDED", "FORFEIT")):
            return sn.replace("STATUS_", "").lower()
        return "Final" + (f"/{period_label(p)}" if (_int(p) or 0) > 4 else "")
    if st != "in":
        return "not started"
    if "HALFTIME" in sn:
        return "Halftime"
    if "END_PERIOD" in sn:
        return f"End {period_label(p)}"
    if (_int(p) or 0) > 4:
        return period_label(p)
    return f"{g.get('clock') or '?'} {period_label(p)}"


def _tkey(period, clock: str) -> int:
    """Sortable game time: later plays have larger keys."""
    try:
        m, s = str(clock or "0:00").split(":")[:2]
        secs = int(m) * 60 + int(float(s))
    except ValueError:
        secs = 0
    return (_int(period) or 0) * 10000 + (900 - secs if (_int(period) or 0) <= 4 else 0)


def _status(comp: dict) -> dict:
    st = comp.get("status") or {}
    ty = st.get("type") or {}
    return {"state": ty.get("state"), "status_name": ty.get("name"), "period": _int(st.get("period")) or 0,
            "clock": st.get("displayClock")}


def _base(event_id, comp: dict, date=None) -> dict | None:
    by = {c.get("homeAway"): c for c in comp.get("competitors") or []}
    if "away" not in by or "home" not in by:
        return None
    g = {"id": str(event_id), "away": _team(by["away"]), "home": _team(by["home"]),
         "away_score": _int(by["away"].get("score")), "home_score": _int(by["home"].get("score")),
         "start": date or comp.get("date"), **_status(comp)}
    if g["away_score"] is None:
        g["away_score"] = 0 if g["state"] == "in" else None
    if g["home_score"] is None:
        g["home_score"] = 0 if g["state"] == "in" else None
    return g


def _side_of(g: dict, team_id) -> str | None:
    tid = str(team_id or "")
    return "away" if tid and tid == g["away"]["id"] else "home" if tid and tid == g["home"]["id"] else None


# ----------------------------------------------------------------------------
# parsers (pure)
# ----------------------------------------------------------------------------


def parse_scoreboard(d: dict) -> dict[str, dict]:
    """{event id: game} with score, clock, possession, down & distance, last play."""
    out = {}
    for e in d.get("events") or []:
        comp = (e.get("competitions") or [{}])[0]
        g = _base(e.get("id"), comp, e.get("date"))
        if not g:
            continue
        sit = comp.get("situation") or {}
        if g["state"] == "in" and sit:
            g["possession"] = _side_of(g, sit.get("possession"))
            g["down_distance"] = sit.get("downDistanceText") or sit.get("shortDownDistanceText")
            g["red_zone"] = bool(sit.get("isRedZone"))
            lp = sit.get("lastPlay") or {}
            if lp.get("text"):
                g["last_play"] = _clean_play(lp["text"])
        out[g["id"]] = g
    return out


_PLAY_TIME = re.compile(r"^\(\d{1,2}:\d{2}\)\s*")
_FORMATION = re.compile(r"^(No Huddle-|No Huddle )?(Shotgun|Pistol|Under Center)\s+", re.I)
_NAME = r"[A-Z][A-Za-z'\-]*\.\s?[A-Z][A-Za-z'\-]+(?:[ -][A-Z][A-Za-z'\-]+)*(?:,? (?:Jr\.|Sr\.|II|III|IV))?"
_INJ = re.compile(r"#(\d{1,2}) (" + _NAME + r") injured on the play")
# '#2 T.Jackson, #20 Z.Morris injured on the play' names two players (the QB came first and was missed)
_INJ_LIST = re.compile(r"((?:#\d{1,2} " + _NAME + r"(?:,| and|, and)? ?)+) injured on the play")
_INJ_ONE = re.compile(r"#(\d{1,2}) (" + _NAME + r")")
_PASSER = re.compile(r"#(\d{1,2}) (" + _NAME + r") (?:pass\b|sacked\b|scrambles?\b)")
_PLAYER_IN = re.compile(r"#(\d{1,2}) (" + _NAME + r")")
TURNOVER_TYPES = {"Fumble Recovery (Opponent)", "Pass Interception Return", "Interception",
                  "Interception Return Touchdown", "Fumble Return Touchdown", "Blocked Field Goal",
                  "Blocked Punt", "Blocked Punt Touchdown", "Blocked Field Goal Touchdown"}
NON_PLAYS = {"Timeout", "End Period", "End of Half", "End of Game", "End of Regulation", "Penalty"}


def _clean_play(text: str) -> str:
    t = _PLAY_TIME.sub("", str(text or "").strip())
    t = _FORMATION.sub("", t)
    t = re.sub(r"\s*\((?:H|LS): [^)]*\)", "", t)
    t = re.sub(r"#\d{1,2} ", "", t)
    return re.sub(r"\s{2,}", " ", t).strip()[:220]


def _abbr_key(name: str) -> str:
    """'B.Edwards Jr.' and 'Bryce Edwards Jr.' -> 'b.edwards'."""
    n = re.sub(r",? (Jr\.?|Sr\.?|II|III|IV)$", "", str(name or "").strip())
    n = n.replace(". ", ".")
    if "." in n.split(" ")[0] and len(n.split(".")[0]) <= 2:
        first, last = n.split(".", 1)
    else:
        parts = n.split(" ", 1)
        if len(parts) < 2:
            return n.lower()
        first, last = parts
    return f"{first[:1]}.{last}".lower().replace(" ", "")


def _roster(d: dict) -> dict:
    """{(team id, jersey): {"name", "pos"}} and {(team id, abbr key): same} from
    the boxscore (players with a stat line only; names/positions, never stats)."""
    out = {}
    for t in (d.get("boxscore") or {}).get("players") or []:
        tid = str((t.get("team") or {}).get("id") or "")
        for cat in t.get("statistics") or []:
            pos = "QB" if cat.get("name") == "passing" else None
            for a in cat.get("athletes") or []:
                ath = a.get("athlete") or {}
                name = ath.get("displayName")
                if not name:
                    continue
                rec = {"name": name, "pos": pos}
                for k in ((tid, str(ath.get("jersey") or "")), (tid, _abbr_key(name))):
                    if k[1] and (k not in out or pos):
                        out[k] = {**out.get(k, {}), **{x: y for x, y in rec.items() if y}}
    return out


def _flat_plays(d: dict) -> list[dict]:
    dr = d.get("drives") or {}
    seen, out = set(), []
    for drive in (dr.get("previous") or []) + ([dr["current"]] if dr.get("current") else []):
        off = str(((drive.get("team") or {}).get("id")) or "")
        for p in drive.get("plays") or []:
            pid = p.get("id")
            if pid in seen:
                continue
            seen.add(pid)
            out.append({**p, "_off": off})
    return out  # drives and their plays come oldest first


def _when(p: dict) -> tuple[int, str]:
    return (_int((p.get("period") or {}).get("number")) or 0, (p.get("clock") or {}).get("displayValue") or "")


def parse_summary(d: dict, event_id=None) -> dict | None:
    """One game's live state from an ESPN summary payload."""
    hdr = d.get("header") or {}
    comp = (hdr.get("competitions") or [{}])[0]
    g = _base(event_id or hdr.get("id"), comp, comp.get("date"))
    if not g:
        return None
    abbr = {g["away"]["id"]: g["away"]["abbr"], g["home"]["id"]: g["home"]["abbr"]}
    name = {g["away"]["id"]: g["away"]["name"], g["home"]["id"]: g["home"]["name"]}
    plays = _flat_plays(d)
    roster = _roster(d)
    wall = {p.get("id"): p.get("wallclock") for p in plays}

    # scoring plays, oldest first
    scoring = []
    for s in d.get("scoringPlays") or []:
        per, clk = _when(s)
        tid = str((s.get("team") or {}).get("id") or "")
        scoring.append({"team": name.get(tid, (s.get("team") or {}).get("displayName") or "?"),
                        "abbr": abbr.get(tid, ""), "type": (s.get("type") or {}).get("text") or "Score",
                        "text": re.sub(r"\s{2,}", " ", str(s.get("text") or "")).strip(),
                        "period": per, "clock": clk, "away": _int(s.get("awayScore")),
                        "home": _int(s.get("homeScore")), "id": s.get("id"), "wallclock": wall.get(s.get("id"))})
    scoring.sort(key=lambda s: (_tkey(s["period"], s["clock"]), (s["away"] or 0) + (s["home"] or 0)))

    key, injuries, passers, turnovers = [], [], {}, {}
    for i, p in enumerate(plays):
        ty = (p.get("type") or {}).get("text") or ""
        text = str(p.get("text") or "")
        per, clk = _when(p)
        off = p["_off"]
        ct = _clean_play(text)
        yds = _int(p.get("statYardage")) or 0
        kind = None
        if p.get("isTurnover") or ty in TURNOVER_TYPES:
            kind = "turnover"
        elif "TURNOVER ON DOWNS" in text.upper():
            kind = "turnover on downs"
        elif ty in ("Field Goal Missed", "Blocked Field Goal", "Blocked Punt"):
            kind = "missed FG" if ty == "Field Goal Missed" else "blocked kick"
        elif ty == "Safety":
            kind = "safety"
        elif yds >= BIG_PLAY_YDS and ty in ("Rush", "Pass Reception", "Passing Touchdown", "Rushing Touchdown",
                                            "Punt Return", "Kickoff Return (Offense)", "Kickoff Return Touchdown"):
            kind = f"big play ({yds} yds)"
        if kind:
            key.append({"kind": kind, "team": name.get(off, ""), "period": per, "clock": clk, "text": ct,
                        "wallclock": p.get("wallclock")})
        if kind == "turnover" and off:  # every giveaway, by the team that had the ball (key plays keep only the last few)
            who = re.search(r"#\d{1,2} (" + _NAME + r") pass intercepted|fumbled? by #\d{1,2} (" + _NAME + ")", text)
            what = ("INT" if "intercept" in (text + ty).lower() else "fumble lost" if "fumbl" in (text + ty).lower()
                    else "blocked kick" if "Blocked" in ty else "turnover")
            who = (who.group(1) or who.group(2)) if who else None
            if not who and what == "INT" and passers.get(off):  # 'Travis Anderson 58 Yd Interception Return' names
                who = passers[off][-1]["name"]                    # no passer: it was the QB in the game
            turnovers.setdefault(name.get(off, off), []).append(
                {"kind": what, "who": who, "period": per, "clock": clk, "td": "Touchdown" in ty})
        m = _PASSER.search(text)
        if m and ty not in NON_PLAYS and off:
            passers.setdefault(off, []).append({"name": m.group(2), "jersey": m.group(1), "i": i,
                                                "period": per, "clock": clk})
        for ml in _INJ_LIST.finditer(text):
            for m in _INJ_ONE.finditer(ml.group(1)):
                injuries.append({"jersey": m.group(1), "name": m.group(2), "i": i, "period": per, "clock": clk,
                                 "wallclock": p.get("wallclock"), "off": off, "text": ct})

    # who's at QB, and any change: a new passer with 2+ straight dropbacks (one
    # trick-play throw isn't a QB change), or the next passer after the starter
    # was reported injured.
    qb = {}
    injured_keys = {_abbr_key(x["name"]) for x in injuries}
    for tid, seq in passers.items():
        if not seq:
            continue
        starter = seq[0]["name"]
        runs = []
        for s in seq:
            if runs and runs[-1]["name"] == s["name"]:
                runs[-1]["n"] += 1
                runs[-1]["last"] = s
            else:
                runs.append({"name": s["name"], "first": s, "last": s, "n": 1})
        cur = runs[-1]
        changes = []
        settled = runs[0]["name"]
        for r in runs[1:]:
            if r["name"] == settled:
                continue
            if r["n"] >= 2 or _abbr_key(settled) in injured_keys:
                changes.append({"from": settled, "to": r["name"], "period": r["first"]["period"],
                                "clock": r["first"]["clock"]})
                settled = r["name"]
        qb[name.get(tid, tid)] = {"starter": starter, "current": settled if changes else starter,
                                  "last_seen": {r["name"]: (r["last"]["period"], r["last"]["clock"]) for r in runs},
                                  "dropbacks_current": sum(r["n"] for r in runs if r["name"] == (settled if changes else starter)),
                                  "changes": changes}

    inj_out = []
    for x in injuries:
        tid = None
        for t in (g["away"]["id"], g["home"]["id"]):
            if (t, x["jersey"]) in roster and _abbr_key(roster[(t, x["jersey"])]["name"]) == _abbr_key(x["name"]):
                tid = t
            elif (t, _abbr_key(x["name"])) in roster:
                tid = tid or t
        r = roster.get((tid, x["jersey"])) or roster.get((tid, _abbr_key(x["name"]))) or {}
        is_qb = r.get("pos") == "QB" or any(_abbr_key(s["name"]) == _abbr_key(x["name"])
                                            for s in passers.get(tid or "", []) + (passers.get(x["off"], []) if not tid else []))
        if tid is None and is_qb:
            tid = x["off"]
        # back on the field after the injury? any later play that names him
        back = None
        for p in plays[x["i"] + 1:]:
            if (p.get("type") or {}).get("text") in NON_PLAYS:
                continue
            for m in _PLAYER_IN.finditer(str(p.get("text") or "")):
                if m.group(1) == x["jersey"] and _abbr_key(m.group(2)) == _abbr_key(x["name"]) and \
                        " injured on the play" not in str(p.get("text") or "")[m.end():m.end() + 22]:
                    back = _when(p)
                    break
            if back:
                break
        inj_out.append({"player": x["name"], "full_name": r.get("name"), "jersey": x["jersey"],
                        "team": name.get(tid or "", None), "qb": bool(is_qb), "period": x["period"],
                        "clock": x["clock"], "wallclock": x["wallclock"], "returned": back,
                        "source": "ESPN play-by-play", "play": x["text"]})

    # where the ball is: the last real play's end state
    last = next((p for p in reversed(plays) if (p.get("type") or {}).get("text") not in
                 ("End Period", "End of Half", "End of Game", "End of Regulation")), None)
    g["receiving"] = None
    if last and g["state"] == "in":
        end = last.get("end") or {}
        g["possession"] = _side_of(g, (end.get("team") or {}).get("id"))
        g["down_distance"] = end.get("downDistanceText") or end.get("shortDownDistanceText")
        g["last_play"] = _clean_play(last.get("text"))
        yte = _int(end.get("yardsToEndzone"))
        g["red_zone"] = yte is not None and yte <= 20
        lty = (last.get("type") or {}).get("text") or ""
        sc_last = scoring[-1] if scoring and scoring[-1].get("id") == last.get("id") else None
        if sc_last and ("Touchdown" in lty or lty in ("Field Goal Good", "Safety") or "Touchdown" in sc_last["type"]):
            # the play that just scored: its end state is the goal line, not who has the ball next
            scorer = next((t for t in (g["away"], g["home"]) if t["name"] == sc_last["team"]), None)
            if scorer:
                other = g["home"] if scorer is g["away"] else g["away"]
                g["possession"], g["down_distance"], g["red_zone"] = None, None, False
                g["receiving"] = scorer["name"] if lty == "Safety" else other["name"]
    # headlines naming a team in this game, published after kickoff - 6 h
    news = []
    try:
        k0 = dt.datetime.fromisoformat(str(g.get("start")).replace("Z", "+00:00")) - dt.timedelta(hours=6)
    except ValueError:
        k0 = None
    for a in ((d.get("news") or {}).get("articles") or [])[:25]:
        h = str(a.get("headline") or "")
        try:
            pub = dt.datetime.fromisoformat(str(a.get("published")).replace("Z", "+00:00"))
        except ValueError:
            continue
        if k0 and pub >= k0 and any(re.search(rf"\b{re.escape(t['name'])}\b", h)
                                    for t in (g["away"], g["home"]) if len(t["name"]) > 3):
            news.append({"headline": h[:160], "published": a.get("published"), "source": "ESPN"})
    tds = {}  # every TD so far, by player: 'how many TDs does Rasean Jones have' (scoring keeps the last 6)
    for x in scoring:
        if "Touchdown" not in x["type"] and not re.search(r"\d+ Yd (pass|run|.*return)", x["text"], re.I):
            continue
        m = re.match(r"([A-Z][A-Za-z.'\- ]+?(?: Jr\.| Sr\.| II| III| IV)?) \d+ Yd (pass from ([A-Z][A-Za-z.'\- ]+?"
                     r"(?: Jr\.| Sr\.| II| III| IV)?) \(|[Rr]un\b|\w)", x["text"])
        if not m:
            continue
        kind = "rec" if m.group(3) else "rush" if m.group(2).lower() == "run" else "return"
        t = tds.setdefault(m.group(1), {"team": x["team"], "rec": 0, "rush": 0, "return": 0, "pass": 0})
        t[kind] += 1
        if m.group(3):
            tds.setdefault(m.group(3), {"team": x["team"], "rec": 0, "rush": 0, "return": 0, "pass": 0})["pass"] += 1
    firsts = {}  # last name -> first names the box score has ('Arch' Manning, never 'Austin')
    for (_tid, k), r in roster.items():
        if "." in k and r.get("name") and " " in r["name"]:
            parts = re.sub(r",? (Jr\.?|Sr\.?|II|III|IV)$", "", r["name"]).split(" ")
            firsts.setdefault(parts[-1].lower(), set()).add(parts[0])
    for x in scoring:  # scoring-play text carries full names too
        for rx in (r"^([A-Z][a-z'\-]+) ([A-Z][A-Za-z'\-]+)(?:,? (?:Jr\.|Sr\.|II|III|IV))? \d+ Yd",
                   r"\bpass from ([A-Z][a-z'\-]+) ([A-Z][A-Za-z'\-]+)", r"\(([A-Z][a-z'\-]+) ([A-Z][A-Za-z'\-]+) Kick\)"):
            for m in re.finditer(rx, x["text"]):
                firsts.setdefault(m.group(2).lower(), set()).add(m.group(1))
    g.update({"scoring": scoring[-KEEP_SCORING:], "scoring_n": len(scoring), "key": key[-KEEP_KEY:], "tds": tds,
              "firsts": {k: sorted(v) for k, v in firsts.items()},
              "turnovers": turnovers, "latest_wallclock": next((p.get("wallclock") for p in reversed(plays)
                                                                if p.get("wallclock")), None),
              "injuries": inj_out[-8:], "qb": qb, "news": news[:3], "detail": True,
              "players": sorted({_abbr_key(m.group(2)).split(".", 1)[-1] for p in plays
                                 for m in _PLAYER_IN.finditer(str(p.get("text") or ""))})[:400]})
    return g


# ----------------------------------------------------------------------------
# cover status vs the league's locked line (same rule as set_winner_on_final:
# home covers when HomePts + line > AwayPts; dead even goes to Away)
# ----------------------------------------------------------------------------


def _num(x: float) -> str:
    return str(int(x)) if float(x) == int(x) else f"{x:g}"


def side_label(side: str, home: str, line: float) -> str:
    h = float(line) if side == home else -float(line)
    return f"{side} {'+' if h > 0 else ''}{_num(h)}" if h else f"{side} (pick'em)"


def cover_status(away: str, home: str, line, ap, hp) -> str | None:
    """'Texas +5.5 covering by 6.5 (LSU -5.5 not covering)' at this score."""
    try:
        ln, a, h = float(line), float(ap), float(hp)
    except (TypeError, ValueError):
        return None
    m = h + ln - a
    cov, other = (home, away) if m > 0 else (away, home)
    if m == 0:
        return (f"{side_label(away, home, ln)} covering exactly on the number "
                f"(a dead-even finish goes to {away} in this league)")
    lead = (h - a) if cov == home else (a - h)  # the covering side's margin on the scoreboard
    spread = ln if cov == home else -ln
    how = (f"{cov} {'leads' if lead > 0 else 'trails' if lead < 0 else 'is tied'}"
           + (f" by {_num(abs(lead))}" if lead else "") + f", {'+' if spread > 0 else ''}{_num(spread)} on the line")
    # both numbers spelled out: the 12B said 'covering by 11' (the lead) when the cover margin was 13.5
    return (f"{side_label(cov, home, ln)} covering by {_num(abs(m))} ({how}; "
            f"{side_label(other, home, ln)} not covering)")


def cover_side(pool: dict, g: dict) -> str | None:
    """'home' / 'away': who covers the locked line at this score (dead even goes to away)."""
    try:
        return "home" if float(g["home_score"]) + float(pool["line"]) - float(g["away_score"]) > 0 else "away"
    except (KeyError, TypeError, ValueError):
        return None


def cover_margin(pool: dict, g: dict) -> float | None:
    try:
        return abs(float(g["home_score"]) + float(pool["line"]) - float(g["away_score"]))
    except (KeyError, TypeError, ValueError):
        return None


# ----------------------------------------------------------------------------
# cache (poller writes; chat reads + overlays its own on-demand refreshes)
# ----------------------------------------------------------------------------

_mem = {"mtime": None, "data": None}
_overlay: dict[str, dict] = {}
_lock = threading.Lock()


def _write(data: dict, path: Path = None) -> None:
    path = path or CACHE
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, separators=(",", ":")))
    os.replace(tmp, path)


def load(path: Path = None) -> dict:
    """The poller's cache merged with this process's on-demand refreshes."""
    path = path or CACHE
    try:
        mt = path.stat().st_mtime
    except OSError:
        mt = None
    if mt != _mem["mtime"]:
        try:
            _mem["data"] = json.loads(path.read_text()) if mt else {}
        except (OSError, ValueError):
            _mem["data"] = _mem["data"] or {}
        _mem["mtime"] = mt
    data = dict(_mem["data"] or {})
    games = dict(data.get("games") or {})
    with _lock:
        for gid, g in _overlay.items():
            if (g.get("ts") or 0) > ((games.get(gid) or {}).get("ts") or 0):
                games[gid] = {**(games.get(gid) or {}), **g}
    data["games"] = games
    return data


def age(g: dict, now: float | None = None) -> float | None:
    ts = g.get("ts")
    return None if not ts else max(0.0, (now or time.time()) - ts)


def is_live(g: dict) -> bool:
    return g.get("state") == "in"


def _get(client: httpx.Client, url: str, validators: dict | None = None, timeout: float = 8.0):
    h = dict(HEADERS)
    v = (validators or {}).get(url) or {}
    if v.get("etag"):
        h["If-None-Match"] = v["etag"]
    if v.get("last_modified"):
        h["If-Modified-Since"] = v["last_modified"]
    r = client.get(url, headers=h, timeout=timeout)
    if r.status_code == 304:
        return None, r
    r.raise_for_status()
    if validators is not None and (r.headers.get("etag") or r.headers.get("last-modified")):
        validators[url] = {"etag": r.headers.get("etag"), "last_modified": r.headers.get("last-modified")}
    return r.json(), r


def fetch_game(event_id: str, client: httpx.Client | None = None, timeout: float = 4.0) -> dict | None:
    own = client is None
    client = client or httpx.Client()
    try:
        d, _ = _get(client, SUMMARY_URL + str(event_id), timeout=timeout)
        g = parse_summary(d, event_id) if d else None
        if g:
            g["ts"] = g["detail_ts"] = time.time()
        return g
    except (httpx.HTTPError, ValueError) as e:
        log.warning("live: summary %s failed: %s", event_id, e)
        return None
    finally:
        if own:
            client.close()


def ensure_fresh(game_ids, refresh_age: float = REFRESH_AGE, cap: int = 3) -> list[str]:
    """Chat side, before answering: refetch the summary of each live game here
    whose data is older than refresh_age (at most `cap`, in parallel, 4 s
    timeout). Results go in this process's overlay. Returns ids refreshed."""
    data = load()
    games = data.get("games") or {}
    now = time.time()
    due = [gid for gid in game_ids if gid in games and is_live(games[gid])
           and (age(games[gid], now) or 1e9) > refresh_age][:cap]
    got = []

    def one(gid):
        g = fetch_game(gid)
        if g:
            with _lock:
                _overlay[gid] = g
            got.append(gid)

    ths = [threading.Thread(target=one, args=(gid,), daemon=True) for gid in due]
    for t in ths:
        t.start()
    for t in ths:
        t.join(6)
    return got


def refresh_board_if_stale(max_age: float | None = None) -> bool:
    """Chat side: if the poller's file is older than max_age (poller down) and
    games may be on, read the scoreboard once into the overlay."""
    max_age = board_max_age() if max_age is None else max_age
    data = load()
    if data.get("updated_ts") and time.time() - data["updated_ts"] <= max_age:
        return False
    try:
        with httpx.Client() as c:
            d, _ = _get(c, SCOREBOARD_URL, timeout=4.0)
    except (httpx.HTTPError, ValueError) as e:
        log.warning("live: on-demand scoreboard failed: %s", e)
        return False
    now = time.time()
    with _lock:
        for gid, g in parse_scoreboard(d or {}).items():
            prev = _overlay.get(gid) or {}
            _overlay[gid] = {**prev, **g, "ts": now, "sb_ts": now}
    return True


# ----------------------------------------------------------------------------
# poller
# ----------------------------------------------------------------------------


class Poller:
    def __init__(self, client: httpx.Client | None = None, clock=time.time, cache: Path | None = None,
                 pool_ids_fn=None):
        self.client = client or httpx.Client(http2=False)
        self.clock = clock
        self.cache = cache or CACHE
        self.games: dict[str, dict] = {}
        self.pool_ids: set[str] = set()
        self.pool_fn = pool_ids_fn or fetch_pool_ids
        self.next_sb = 0.0
        self.next_pool = 0.0
        self.next_detail: dict[str, float] = {}
        self.fails: dict[str, int] = {}
        self.hold: dict[str, float] = {}
        self.validators: dict = {}
        self.final_done: set[str] = set()
        self.mode = "idle"
        self.stats = {"sb": 0, "summary": 0, "errors": 0}
        try:  # warm start: keep detail across restarts
            old = json.loads(self.cache.read_text())
            self.games = old.get("games") or {}
            self.pool_ids = set(old.get("pool_ids") or [])
        except (OSError, ValueError):
            pass

    def _fail(self, key: str, now: float, retry_after=None) -> None:
        n = self.fails.get(key, 0) + 1
        self.fails[key] = n
        self.stats["errors"] += 1
        wait = min(BACKOFF_CAP, 10 * 2 ** (n - 1))
        if retry_after:
            wait = max(wait, min(BACKOFF_CAP, float(retry_after)))
        self.hold[key] = now + wait

    def _ok(self, key: str) -> None:
        self.fails.pop(key, None)
        self.hold.pop(key, None)

    def _held(self, key: str, now: float) -> bool:
        return self.hold.get(key, 0) > now

    def tracked(self) -> list[str]:
        """Live (or just-final) games worth play-by-play: pool games first, then
        ranked matchups (both ranked, then one), capped at MAX_DETAIL."""
        live = [g for g in self.games.values() if g.get("state") == "in"]

        def pri(g):
            ranks = [r for r in (g["away"].get("rank"), g["home"].get("rank")) if r]
            return (0 if g["id"] in self.pool_ids else 1 if len(ranks) == 2 else 2 if ranks else 3,
                    min(ranks) if ranks else 99)
        return [g["id"] for g in sorted(live, key=pri)][:MAX_DETAIL]

    def step(self) -> float:
        """One pass; returns seconds until the next thing is due."""
        now = self.clock()
        changed = False
        if now >= self.next_pool:
            try:
                ids = self.pool_fn()
                if ids is not None:
                    self.pool_ids = set(map(str, ids))
            except Exception as e:  # noqa: BLE001 - pool ids are a nice-to-have
                log.warning("live: pool ids failed: %s", e)
            self.next_pool = now + POOL_IDS_EVERY
        if now >= self.next_sb and not self._held("sb", now):
            try:
                d, _ = _get(self.client, SCOREBOARD_URL, self.validators)
                self.stats["sb"] += 1
                if d is not None:
                    for gid, g in parse_scoreboard(d).items():
                        prev = self.games.get(gid) or {}
                        keep_detail = {k: v for k, v in prev.items() if k not in g}
                        self.games[gid] = {**keep_detail, **g, "sb_ts": now, "ts": now}
                    changed = True
                self._ok("sb")
            except (httpx.HTTPError, ValueError) as e:
                ra = getattr(getattr(e, "response", None), "headers", {}).get("retry-after") \
                    if isinstance(e, httpx.HTTPStatusError) else None
                log.warning("live: scoreboard failed: %s", e)
                self._fail("sb", now, ra)
            live = any(g.get("state") == "in" for g in self.games.values())
            soon = any(self._kick_soon(g, now) for g in self.games.values())
            mode = "live" if live else "pregame" if soon else "idle"
            if mode != self.mode:
                log.info("live: mode %s -> %s", self.mode, mode)
                self.mode = mode
            self.next_sb = now + (SB_EVERY if mode != "idle" else IDLE_EVERY)
        # play by play for tracked live games, plus one last pass when one goes final
        want = self.tracked()
        for gid, g in self.games.items():
            if g.get("state") == "post" and g.get("detail") and gid not in self.final_done:
                want.append(gid)
        for gid in want:
            if now < self.next_detail.get(gid, 0) or self._held(gid, now):
                continue
            try:
                d, _ = _get(self.client, SUMMARY_URL + gid, self.validators)
                self.stats["summary"] += 1
                if d is not None:
                    g = parse_summary(d, gid)
                    if g:
                        prev = self.games.get(gid) or {}
                        self.games[gid] = {**prev, **g, "detail_ts": now, "ts": now}
                        changed = True
                        if g.get("state") == "post":
                            self.final_done.add(gid)
                self._ok(gid)
            except (httpx.HTTPError, ValueError) as e:
                log.warning("live: summary %s failed: %s", gid, e)
                self._fail(gid, now)
            g = self.games.get(gid) or {}
            ranked = (g.get("away") or {}).get("rank") or (g.get("home") or {}).get("rank")
            self.next_detail[gid] = now + (POOL_EVERY if gid in self.pool_ids else OTHER_EVERY if ranked else REST_EVERY)
        if changed:
            self.save(now)
        nxt = [self.next_sb, self.next_pool] + [self.next_detail.get(g, now) for g in self.tracked()]
        nxt += list(self.hold.values())
        return max(1.0, min(nxt) - self.clock())

    def _kick_soon(self, g: dict, now: float) -> bool:
        if g.get("state") != "pre":
            return False
        try:
            k = dt.datetime.fromisoformat(str(g.get("start")).replace("Z", "+00:00")).timestamp()
        except ValueError:
            return False
        return -3600 < k - now < SOON_S

    def save(self, now: float) -> None:
        # drop games two days past
        for gid in [k for k, g in self.games.items() if g.get("state") == "post" and now - (g.get("ts") or now) > 2 * 86400]:
            self.games.pop(gid, None)
        _write({"v": 1, "updated_ts": now, "updated_at": dt.datetime.fromtimestamp(now, dt.timezone.utc).isoformat(),
                "mode": self.mode, "pool_ids": sorted(self.pool_ids), "games": self.games,
                "intervals": {"scoreboard": SB_EVERY, "pool": POOL_EVERY, "ranked": OTHER_EVERY, "other": REST_EVERY,
                              "idle": IDLE_EVERY}},
               self.cache)


def _anon_key() -> str:
    s = (HERE / "chat.py").read_text()
    m = re.search(r"SUPABASE_ANON = \((.*?)\)", s, re.S)
    return "".join(re.findall(r'"([^"]*)"', m.group(1))) if m else ""


def fetch_pool_ids() -> list[str] | None:
    """This week's pool games (picked=true, kicking off from 2 days ago to 8 days out)."""
    key = _anon_key()
    if not key:
        return None
    now = dt.datetime.now(CT).replace(tzinfo=None)
    lo, hi = (now - dt.timedelta(days=2)).isoformat(timespec="seconds"), (now + dt.timedelta(days=8)).isoformat(timespec="seconds")
    col = urllib.parse.quote('"Start (CT)"')
    url = (f"{SUPABASE_URL}/rest/v1/all_games?select=GameId&picked=eq.true&{col}=gte.{lo}&{col}=lte.{hi}")
    r = httpx.get(url, headers={"apikey": key, "Authorization": f"Bearer {key}"}, timeout=10)
    r.raise_for_status()
    return [str(x["GameId"]) for x in r.json()]


def run(stop: threading.Event | None = None, sleep=time.sleep, poller: Poller | None = None) -> None:
    p = poller or Poller()
    log.info("live: poller up, cache %s", CACHE)
    last_note = 0.0
    while not (stop and stop.is_set()):
        try:
            wait = p.step()
        except Exception:  # noqa: BLE001 - never die; launchd would restart, but keep state
            log.exception("live: step crashed")
            wait = 30
        if p.mode == "live" and time.time() - last_note > 600:
            log.info("live: %d live, tracking %s, calls %s", sum(g.get("state") == "in" for g in p.games.values()),
                     len(p.tracked()), p.stats)
            last_note = time.time()
        sleep(wait)


# ----------------------------------------------------------------------------
# the LIVE rider (chat side)
# ----------------------------------------------------------------------------

_LIVE_ASK = re.compile(
    r"\b(scores?|scored|scoring|score is|live|right now|happening|going on|on now|what'?s on|winning|losing|leading|"
    r"lead|up by|down by|quarter|half(time)?|clock|time left|td|tds|touchdowns?|field goal|fg|pick six|pick-six|"
    r"interception|intercepted|picked off|fumbled?|turnovers?|injur\w*|hurt|limping|carted|helped off|left the game|"
    r"out of the game|leave the game|went down|go down|backup|qb\d?|quarterbacks?|covering|cover|drive|red zone|"
    r"possession|ball|who has the ball|game|games|update|updates|picks?)\b", re.I)
# 'how are my picks doing' during games: every pool game (finals, live, not started) with its cover status
_PICKS_ASK = re.compile(r"\b(my|our) (picks?|week|bets?|sides?|games)\b|\bhow (am|'?m) i doing\b|"
                        r"\bpicks? (doing|going|looking)\b", re.I)
_WHAT_HAPPENING = re.compile(r"\b(what'?s happening|what'?s going on|any updates?|what'?s on|games? on|live games?|"
                             r"scores?\b(?! of)|around the country|anything good)\b", re.I)


# what people actually text ('Wake', 'Boise'); only short names no other FBS team shares
ALIASES = {"Wake Forest": ["Wake"], "Boise State": ["Boise"], "Alabama": ["Bama"], "Vanderbilt": ["Vandy"],
           "Pittsburgh": ["Pitt"], "Missouri": ["Mizzou"], "Syracuse": ["Cuse"]}
# a name inside a longer team name is that other team ('Texas' in 'Texas A&M' / 'North Texas')
_PRE = "".join(f"(?<!{w} )" for w in ("North", "South", "East", "West", "Central", "Eastern", "Western", "Northern",
                                       "Southern", "Coastal", "Middle", "New"))
_POST = r"(?!\s+(?:A&M|Tech|State|St\b|Southern|Christian|Poly|International)\b)"


def _name_keys(t: dict) -> list[tuple[str, bool]]:
    """(text, case_sensitive) ways a question names a team."""
    out = [(t["name"], False)]
    n = t["name"]
    out += [(x, False) for x in ALIASES.get(n, [])]
    if n.endswith(" State"):
        out.append((n[:-6] + " St", False))
    out.append((n.replace("'", "").replace("’", ""), False))
    if t.get("abbr") and len(t["abbr"]) >= 2:
        out.append((t["abbr"], True))
    if t.get("nick") and len(t["nick"]) > 4:
        out.append((t["nick"], False))
    return out


_COMMON = {"long", "young", "black", "white", "green", "gray", "grey", "hall", "king", "love", "hope", "will", "west",
           "north", "south", "east", "little", "small", "short", "price", "rice", "bell", "best", "early", "good", "more",
           "much", "many", "back", "just", "last", "game", "ball", "down", "over", "under", "half", "what", "when",
           "with", "that", "this", "they", "them", "then", "than", "have", "been", "doing", "going", "about", "after",
           "still", "right", "know", "lead", "score", "drive", "time", "play", "says", "said", "news", "week", "picks"}


_TEAMS = {"stamp": None, "names": set()}


def _known_teams() -> set:
    """Division I team names (lowercase) from the local CFBD cache (never the API); empty if it's missing."""
    try:
        import cfbd
        ix = cfbd.load_index() or {}
        if ix.get("fetched_at") != _TEAMS["stamp"]:
            _TEAMS["names"] = {str(k).lower() for k in (ix.get("by_team") or {})}
            _TEAMS["stamp"] = ix.get("fetched_at")
    except Exception:  # noqa: BLE001 - nice to have
        pass
    return _TEAMS["names"]


def missing_teams(question: str, games: dict, known: set | None = None) -> list[str]:
    """Teams the question names that have no game in the feed ('Texas A&M', 'Oregon' when only Oregon State plays)."""
    known = _known_teams() if known is None else known
    if not known or not question:
        return []
    feed = {k.lower() for g in games.values() for side in ("away", "home") for k, _ in _name_keys(g[side])}
    q, out = question, []
    for name in sorted(known, key=len, reverse=True):
        if len(name) < 4:
            continue
        rx = rf"(?<![\w']){re.escape(name)}(?![\w&])(?!\s+(?:A&M|Tech|State|St\b))"
        if re.search(rx, q, re.I):
            if name not in feed:
                out.append(name.title().replace("A&m", "A&M"))
            q = re.sub(rx, " | ", q, flags=re.I)
    return out[:2]


def named_games(question: str, games: dict) -> list[dict]:
    """Games whose team the question names (longest names first)."""
    hits, taken = [], question or ""
    pairs = []
    for g in games.values():
        for side in ("away", "home"):
            for k, cs in _name_keys(g[side]):
                pairs.append((len(k), k, cs, g))
    for _, k, cs, g in sorted(pairs, key=lambda x: -x[0]):
        rx = re.compile(rf"(?<![\w']){_PRE}{re.escape(k)}(?!\w|'(?!s\b)){_POST}", 0 if cs else re.I)
        if rx.search(taken):
            if g not in hits:
                hits.append(g)
            taken = rx.sub(" | ", taken)
    return hits


def games_for(question: str, games: dict, pool_ids=()) -> list[dict]:
    """Games the question touches: named teams (longest names first), then
    players in a game's feed; 'how are my picks doing' gets every pool game; a
    generic live ask gets the live pool games plus the closest ranked live games."""
    q = question or ""
    hits = named_games(q, games)
    if not hits and _PICKS_ASK.search(q) and pool_ids:
        return sorted((g for g in games.values() if g["id"] in set(pool_ids)), key=lambda g: str(g.get("start")))[:6]
    if not hits:  # a player the feed knows: last names of 4+ letters (not everyday words: 'how LONG ago')
        words = {w.lower() for w in re.findall(r"[A-Za-z][A-Za-z'\-]{3,}", q)} - _COMMON
        for g in games.values():
            names = set(g.get("players") or [])
            for x in (g.get("injuries") or []):
                names.add(_abbr_key(x["player"]).split(".", 1)[-1])
            for s in g.get("scoring") or []:
                names.update(w.lower() for w in re.findall(r"[A-Z][a-z'\-]{3,}", s.get("text") or ""))
            if words & {n.lower() for n in names} and g.get("state") in ("in", "post"):
                hits.append(g)
    if not hits and _LIVE_ASK.search(q):
        live = [g for g in games.values() if g.get("state") == "in"]
        pool = [g for g in live if g["id"] in set(pool_ids)]
        rest = sorted((g for g in live if g["id"] not in set(pool_ids) and (g["away"].get("rank") or g["home"].get("rank"))),
                      key=lambda g: abs((g.get("home_score") or 0) - (g.get("away_score") or 0)))
        hits = pool + (rest[:3] if _WHAT_HAPPENING.search(q) or not pool else [])
    return hits[:4]


def _score(g: dict) -> str:
    return f"{g['away']['name']} {g.get('away_score')}, {g['home']['name']} {g.get('home_score')}"


def _play_desc(s: dict, g: dict) -> str:
    return (f"{s['team']} {s['type']} {period_label(s['period'])} {s['clock']}: {s['text']} "
            f"(score after: {g['away']['name']} {s['away']}, {g['home']['name']} {s['home']})").replace("  ", " ")


def game_block(g: dict, pool: dict | None = None, now: float | None = None) -> str:
    now = now or time.time()
    a = age(g, now)
    st = g.get("state")
    lines = []
    head = f"- {g['away']['name']} at {g['home']['name']}: "
    if st == "pre":
        lines.append(head + f"not started (kickoff {_kick_ct(g.get('start'))}).")
    elif st == "in" and a is not None and a > game_max_age():
        lines.append(head + f"LIVE but the feed is {int(a)} s behind and the refresh failed: do NOT give a "
                     "current score; say the live feed is lagging.")
        return "\n".join(lines)
    else:
        s = head + _score(g) + f", {'LIVE ' if st == 'in' else ''}{clock_text(g)}"
        if st == "in":
            pos = g.get(g.get("possession") or "", {}) if g.get("possession") in ("away", "home") else None
            if pos and "Half" not in clock_text(g) and not clock_text(g).startswith("End"):
                s += f"; {pos['name']} ball" + (f", {g['down_distance']}" if g.get("down_distance") else "") \
                     + (" (red zone)" if g.get("red_zone") else "")
            elif g.get("receiving") and "Half" not in clock_text(g):
                s += f"; kickoff after that score, {g['receiving']} gets the ball next"
        lines.append(s + ".")
    sc = list(reversed(g.get("scoring") or []))
    if sc:
        ago = since(sc[0], g, now) if st == "in" else None
        lines.append("  Last score: " + _play_desc(sc[0], g) + (f", {ago}" if ago else "") + ".")
        if len(sc) > 1:
            lines.append("  Earlier scores (newest first): " + " | ".join(_play_desc(x, g) for x in sc[1:]) + ".")
    if g.get("tds"):
        lines.append("  TDs so far, whole game (by player): " + "; ".join(
            f"{n} ({v['team']}) " + ", ".join(f"{v[k]} {lab}" for k, lab in (("rush", "rushing"), ("rec", "receiving"),
                                                                              ("return", "return"), ("pass", "passing"))
                                              if v.get(k)) for n, v in g["tds"].items()) + ".")
    elif st == "in" and g.get("detail"):
        lines.append("  No points yet.")
    if g.get("detail") and st in ("in", "post") and g.get("qb"):
        lines.append("  QBs " + ("right now" if st == "in" else "at the end") + ": " + "; ".join(
            f"{team} {q.get('current') or q.get('starter')}" for team, q in (g.get("qb") or {}).items()) + ".")
    if g.get("detail") and st in ("in", "post"):
        to = g.get("turnovers") or {}
        lines.append("  Turnovers so far (all of them): " + ("; ".join(
            f"{team} {len(xs)} (" + ", ".join(f"{x.get('who') + ' ' if x.get('who') else ''}{x['kind']}"
                                               f"{' returned for a TD' if x.get('td') else ''} {period_label(x['period'])} "
                                               f"{x['clock']}" for x in xs) + ")" for team, xs in to.items())
                                                                    or "none") + ".")
    key = list(reversed(g.get("key") or []))[:3]
    if key:
        lines.append("  Key plays (newest first): " + " | ".join(
            f"{k['team']} {k['kind']} {period_label(k['period'])} {k['clock']}: {k['text'][:130]}" for k in key) + ".")
    st_lines = []
    # QB news first, then everyone else newest first
    for x in sorted(reversed(g.get("injuries") or []), key=lambda x: not x.get("qb")):
        who = f"{x['team'] + ' ' if x.get('team') else ''}{'QB ' if x.get('qb') else ''}{x.get('full_name') or x['player']}"
        s = f"{who} injured on the play {period_label(x['period'])} {x['clock']}"
        if x.get("returned"):
            s += f", back in the game {period_label(x['returned'][0])} {x['returned'][1]}"
        elif st == "in":
            s += ", not seen on a play since"
            q = (g.get("qb") or {}).get(x.get("team") or "") or {}
            if x.get("qb") and not any(_tkey(c["period"], c["clock"]) >= _tkey(x["period"], x["clock"])
                                       for c in q.get("changes") or []):
                s += "; no replacement QB has taken a snap in the feed yet (don't name one)"
        st_lines.append(s)
    qb_lines = []
    for team, q in (g.get("qb") or {}).items():
        for c in q.get("changes") or []:
            qb_lines.append(f"{team} QB change: {c['to']} took over from {c['from']} at {period_label(c['period'])} "
                            f"{c['clock']}")
        if q.get("changes") and q.get("current") != q.get("starter"):
            ls = (q.get("last_seen") or {}).get(q["starter"])
            if ls:
                qb_lines.append(f"{team} starter {q['starter']} last dropped back {period_label(ls[0])} {ls[1]}; "
                                f"{q['current']} has {q.get('dropbacks_current')} dropbacks")
    n_qb_inj = sum(1 for x in g.get("injuries") or [] if x.get("qb"))
    st_lines = st_lines[:n_qb_inj] + qb_lines + st_lines[n_qb_inj:]
    if st_lines:
        lines.append("  Player status (REPORTED from ESPN's play-by-play, not an official injury report; say "
                     "'reported'): " + "; ".join(st_lines[:6]) + ".")
    for n in g.get("news") or []:
        lines.append(f"  ESPN headline ({n['published'][:16].replace('T', ' ')} UTC): {n['headline']}")
    if pool and pool.get("line") is not None and st in ("in", "post") and g.get("away_score") is not None:
        cs = cover_status(pool["away"], pool["home"], pool["line"], g.get("away_score"), g.get("home_score"))
        if cs:
            lines.append(f"  POOL GAME, league line {side_label(pool['home'], pool['home'], pool['line'])}: "
                         f"{'right now ' if st == 'in' else 'final: '}{cs}.")
    return "\n".join(lines)


def since(s: dict, g: dict, now: float | None = None) -> str | None:
    """'about 4 min ago' for a play: the game's own clock on the wall (newest play's wallclock), so a replay
    reads the same as a live Saturday."""
    try:
        w = dt.datetime.fromisoformat(str(s.get("wallclock")).replace("Z", "+00:00")).timestamp()
        latest = dt.datetime.fromisoformat(str(g.get("latest_wallclock")).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None
    secs = max(0.0, latest - w) + (age(g, now) or 0.0)
    return "under a minute ago (real time)" if secs < 60 else f"about {int(secs // 60)} min ago (real time)"


def _kick_ct(start) -> str:
    try:
        k = dt.datetime.fromisoformat(str(start).replace("Z", "+00:00")).astimezone(CT)
    except ValueError:
        return "time unknown"
    return f"{k:%a %-I:%M %p} CT"


def rider(question: str, pool: dict | None = None, refresh: bool = True, now: float | None = None) -> tuple[str, list]:
    """(LIVE rider text, games used) for a question that touches a live game, a
    team in one, a player in one, or 'what's happening'. pool: {event id:
    {"away","home","line"}} with the league's names and locked line. Refreshes
    stale live games on demand first. ('', []) when nothing applies."""
    pool = {str(k): v for k, v in (pool or {}).items()}
    data = load()
    games = data.get("games") or {}
    if refresh and _LIVE_ASK.search(question or "") and (not games or not data.get("updated_ts")
                                                         or time.time() - data["updated_ts"] > board_max_age()):
        if refresh_board_if_stale():
            data = load()
            games = data.get("games") or {}
    if not games:
        return "", []
    hit = games_for(question, games, list(pool) or data.get("pool_ids") or [])
    named = {g["id"] for g in named_games(question, games)}
    missing = missing_teams(question, games) if not named else []
    if missing and not _PICKS_ASK.search(question or ""):
        hit = [g for g in hit if g["id"] in named]  # the generic fallback's games aren't what they asked about
    hit = [g for g in hit if g.get("state") in ("in", "post") or g["id"] in pool or g["id"] in named]
    # finals only when the question is about that game or nothing is live; skip stale finals from days ago
    miss_txt = "".join(
        f"\n  NOT IN THE LIVE FEED: {m} has no game in the feed right now, so there's no live score for {m}"
        + "".join(f" ({t} is a different team: don't give its numbers as {m}'s)"
                  for t in sorted({g[s_]['name'] for g in games.values() for s_ in ('away', 'home')
                                   if g[s_]['name'].lower().startswith(m.lower().split(' ')[0]) and g[s_]['name'] != m}))
        + ". Say you don't have a live score for it." for m in missing)
    if not hit:
        if miss_txt:
            return ("LIVE GAME FEED (ESPN): what the feed knows about the team(s) asked about." + miss_txt,
                    [{"id": "_missing", "missing": missing}])
        return "", []
    if refresh:
        if ensure_fresh([g["id"] for g in hit if is_live(g)]):
            games = load().get("games") or {}
            hit = [games.get(g["id"], g) for g in hit]
    now = now or time.time()
    newest = max((g.get("ts") or 0) for g in hit)
    as_of = dt.datetime.fromtimestamp(newest, CT).strftime("%-I:%M:%S %p CT") if newest else "unknown"
    live_now = [g for g in hit if is_live(g)]
    head = ("LIVE GAME FEED (ESPN, polled every 20-60 s during games; read at "
            f"{as_of}, {int(now - newest) if newest else '?'} s ago). A live-game question is one you DO answer, "
            "pool game or not; never call it off-topic. These are the CURRENT facts: use these exact "
            "scores, times and names, say 'as of <game clock>' for a live game, never a different score or a "
            "player/event not listed here. Injury/QB items are REPORTED from the play-by-play: say 'reported' "
            "or 'per ESPN', never call anyone healthy and never guess how bad it is.")
    if not live_now:
        head = head.replace("These are the CURRENT facts", "No game named here is live; these are the latest facts")
    body = "\n".join(game_block(g, pool.get(g["id"]), now) for g in hit)
    body += no_injury_lines(question, hit) + miss_txt
    hit = [{**g, "cover_margin": cover_margin(pool[g["id"]], g), "cover_side": cover_side(pool[g["id"]], g),
            "in_pool": True} if g["id"] in pool else {**g, "in_pool": False if pool else None} for g in hit]
    return head + "\n" + body, hit


def no_injury_lines(question: str, hit: list[dict]) -> str:
    """'did Kienholz get hurt?' about a player the feed has on plays but never 'injured on the play': say so
    (the 12B otherwise guesses from the question)."""
    if not _INJ_WORD.search(question or ""):
        return ""
    out = []
    for g in hit:
        injured = {_abbr_key(x["player"]).split(".", 1)[-1].lower() for x in g.get("injuries") or []}
        known = {p.lower() for p in g.get("players") or []}
        for w in dict.fromkeys(re.findall(r"\b[A-Za-z][a-z'\-]{3,}\b", question or "")):  # 'did kienholz get hurt' too
            if w.lower() in known and w.lower() not in injured and w.lower() not in _COMMON:
                out.append(f"  NO injury reported in the live feed for {w.capitalize()} ({g['away']['name']} at {g['home']['name']}): "
                           f"no 'injured on the play' note for him. Say there's no report; don't confirm the rumor.")
    return ("\n" + "\n".join(out)) if out else ""


_STATUS = {"in": "in_progress", "post": "final"}


def unfinished_overlay(week_games: list[dict], season_games: list[dict]) -> tuple[list[dict], list[dict]]:
    """chat._build_context, before grading: a pool game the live feed (fresh) has as not started or in progress
    is ungraded (no winner, out of the finals that feed standings and weekly records), so 'you went 4-0' can't
    come from a premature or stale final. A no-op when the feed is idle or stale."""
    try:
        games = load().get("games") or {}
    except Exception:  # noqa: BLE001 - never break context building
        return week_games, season_games
    now = time.time()
    open_ = {gid: g["state"] for gid, g in games.items()
             if g.get("state") in ("pre", "in") and (age(g, now) or 1e9) <= game_max_age()}
    if not open_:
        return week_games, season_games
    wk = [{**g, "Status": "in_progress" if open_[str(g.get("GameId"))] == "in" else "scheduled", "winner": None}
          if str(g.get("GameId")) in open_ else g for g in week_games]
    return wk, [g for g in season_games if str(g.get("GameId")) not in open_]
_FOLLOWUP = re.compile(r"\b(that|it|him|he|his|they|them|then|why|when|how long|how come|replaced?)\b|^\s*(and|so|but)\b",
                       re.I)


def apply_to_ctx(ctx: dict, question: str, refresh: bool = True, prev: str = "") -> str:
    """chat.py's one hook (run off the event loop). Builds the LIVE rider for
    the question (refreshing stale live games first) into ctx['live_rider'] /
    ['live_rider_games'], and lays the poller's fresher score over the pool
    rows chat already read from all_games (so live_block, cover status and the
    results guard all see the same, newest score). Returns the rider."""
    pool = {str(g["id"]): {"away": g["away"], "home": g["home"], "line": g.get("line")}
            for g in ctx.get("games_this_week") or [] if not g.get("tiebreaker")}
    try:
        text, hit = "", []
        ctx["live_followup"] = False
        if prev and not named_games(question, load().get("games") or {}) and _FOLLOWUP.search(question or ""):
            text, hit = rider(f"{prev} {question}", pool, refresh=refresh)  # 'how long ago was that?': last question's game
            ctx["live_followup"] = bool(text) and bool(named_games(prev, {g["id"]: g for g in hit}))
        if not text:
            text, hit = rider(question, pool, refresh=refresh)
        if not text and prev:  # any other follow-up the feed can't place on its own
            text, hit = rider(f"{prev} {question}", pool, refresh=refresh)
            ctx["live_followup"] = bool(text) and bool(named_games(prev, {g["id"]: g for g in hit}))
    except Exception as e:  # noqa: BLE001 - the live rider must never break a reply
        log.warning("live rider failed: %s", e)
        text, hit = "", []
    ctx["live_rider"], ctx["live_rider_games"] = text, hit
    ctx["live_injuries"] = any(g.get("injuries") or any((q or {}).get("changes") for q in (g.get("qb") or {}).values())
                               for g in hit)
    games = load().get("games") or {}
    now = time.time()
    rows = {str(r.get("GameId")): r for r in ctx.get("live_games") or []}
    for pg in ctx.get("games_this_week") or []:
        g = games.get(str(pg["id"]))
        if not g or (age(g, now) or 1e9) > game_max_age():
            continue
        r = rows.get(str(pg["id"]))
        if r is None:
            continue
        if g.get("state") == "pre":  # not kicked off: no score, nothing graded
            r.update({"AwayPts": None, "HomePts": None, "Status": "scheduled", "winner": None})
            pg["winner"], pg["status"] = None, "scheduled"
            continue
        if g.get("state") not in _STATUS:
            continue
        r.update({"AwayPts": g.get("away_score"), "HomePts": g.get("home_score"), "Status": _STATUS[g["state"]],
                  "Period": g.get("period"), "Clock": g.get("clock"), "status_name": g.get("status_name"),
                  "possession": g.get("possession"), "is_red_zone": bool(g.get("red_zone")),
                  "SourceUpdatedAt": dt.datetime.fromtimestamp(g["ts"], dt.timezone.utc).isoformat()})
        if g["state"] == "in":  # can't be graded yet, whatever an older read said
            r["winner"] = None
            pg["winner"], pg["status"] = None, "in_progress"
    return text


# ----------------------------------------------------------------------------
# the computed answer: chat._ensure adds this sentence when the reply never
# stated the number/name (and claim_check cuts any wrong one), so a live
# score, cover margin, scorer or QB can't go out wrong or missing
# ----------------------------------------------------------------------------

_Q_PICKS = _PICKS_ASK
_Q_COVER = re.compile(r"\bcover\w*\b|\bspread\b|\bagainst the (line|number)\b", re.I)
_Q_TOTAL = re.compile(r"\btotal\b|\bcombined\b|\bhow many points\b|\bpace\b|\bthe over\b|\bthe under\b", re.I)
_Q_SCORER = re.compile(r"\bwho scored\b|\bhow did\b.{0,40}\bscore\b|\bwho (threw|caught|ran)\b|\bwhat (just )?happened\b|"
                       r"\blast (score|td|touchdown)\b|\bscored (that|the) (last|td)\b", re.I)
_Q_QB = re.compile(r"\bqb\b|\bquarterback\b", re.I)
_Q_SCORE = re.compile(r"\bscores?\b|\bwho'?s winning\b|\bwinning\b|\bleading\b|\bhow'?s\b.{0,40}\b(doing|going)\b|"
                      r"\bupdate\b|\bwhat'?s (happening|going on)\b|\bfinal\b|\bwho won\b|\bhalftime\b", re.I)
_Q_POSS = re.compile(r"\bwho has the ball\b|\bpossession\b|\bwho'?s got the ball\b", re.I)


def _num_rx(x: float) -> str:
    return rf"(?<![\d.]){re.escape(_num(x))}(?![\d]|\.\d)"


def _target(ctx: dict, question: str) -> list[dict]:
    """The game(s) the question names, or the last question's on a follow-up. Never a generic pick: 'what's the
    Texas A&M score?' must not get the one live pool game's score as its answer."""
    hit = ctx.get("live_rider_games") or []
    games = {g["id"]: g for g in hit}
    named = [games[g["id"]] for g in named_games(question, games)] if question else []
    return named or (hit if ctx.get("live_followup") else [])


def _pool_rows(ctx: dict) -> dict:
    return {str(g["id"]): g for g in ctx.get("games_this_week") or [] if not g.get("tiebreaker")}


def _scorer(s: dict) -> str | None:
    m = re.match(r"([A-Z][A-Za-z.'\- ]+?(?: Jr\.| Sr\.| II| III| IV)?) \d+ Yd", s.get("text") or "")
    return m.group(1) if m else None


def picks_fact(ctx: dict) -> tuple | None:
    me = (ctx.get("you") or {}).get("team_name")
    picks = {str(p["game_id"]): p["pick"] for p in (ctx.get("revealed_picks_this_week") or {}).get(me) or []}
    rows = _pool_rows(ctx)
    games = {g["id"]: g for g in ctx.get("live_rider_games") or []}
    if not picks or not games:
        return None
    parts, need = [], []
    for gid, row in sorted(rows.items(), key=lambda kv: str(kv[1].get("start"))):
        if gid not in picks:
            continue
        pick, g = picks[gid], games.get(gid)
        lab = side_label(row["home"] if _same(pick, row["home"]) else row["away"], row["home"], row["line"])
        team = lab.rsplit(" ", 1)[0] if not lab.endswith("(pick'em)") else lab[:-10]
        need.append(re.escape(team.split(" ")[0]))
        if not g or g.get("state") == "pre" or g.get("away_score") is None:
            parts.append(f"{lab} not started")
            continue
        m = g["home_score"] + float(row["line"]) - g["away_score"]
        mine = (m > 0) == _same(pick, row["home"]) if m else not _same(pick, row["home"])
        sc = f"{g['away']['name']} {g['away_score']}-{g['home']['name']} {g['home_score']}"
        if g.get("state") == "post":
            parts.append(f"{lab} {'WON, covered' if mine else 'LOST, missed'} by {_num(abs(m))} (final {sc})")
        else:
            parts.append(f"{lab} {'covering' if mine else 'NOT covering'} by {_num(abs(m))} ({sc}, {clock_text(g)})")
            need.append(_num_rx(abs(m)))
    if not parts:
        return None
    if any(x.endswith("not started") for x in parts):  # 'locked in for this week' isn't 'hasn't kicked off'
        need.append(r"not (started|kicked|underway|begun)|hasn'?t (started|kicked|begun)|haven'?t (started|kicked)|"
                    r"kicks? off|kickoff|yet to|later|tonight")
    if any("WON" in x or "LOST" in x for x in parts):
        need.append(r"\b(won|win|covered|cashed|hit|lost|loss|missed)\b")
    return (lambda t, need=need: all(re.search(r, t, re.I) for r in need),
            " Your picks right now: " + "; ".join(parts) + ".")


def _same(a: str, b: str) -> bool:
    n = lambda x: re.sub(r"[^a-z]", "", str(x or "").lower())  # noqa: E731
    return n(a) == n(b)


def required_fact(ctx: dict, question: str) -> tuple | None:
    """(check(reply) -> bool, sentence) for a live factual question, else None."""
    q = question or ""
    hit = ctx.get("live_rider_games") or []
    if not hit or not q:
        return None
    miss = [m for g in hit if g.get("id") == "_missing" for m in g["missing"]]
    if miss:
        return (lambda t: bool(re.search(r"don'?t (have|see)|no (live )?(score|game|data)|not in (my|the) (live )?feed|"
                                         r"isn'?t (playing|on|in)|not (playing|on)", t, re.I)),
                f" I don't have a live score for {miss[0]}: no game of theirs in my live feed right now.")
    hit = [g for g in hit if g.get("id") != "_missing"]
    ctx = {**ctx, "live_rider_games": hit}
    if not hit:
        return None
    if _Q_PICKS.search(q) and not named_games(q, {g["id"]: g for g in hit}):
        return picks_fact(ctx)
    tg = _target(ctx, q)
    if len(tg) != 1:
        return None
    g = tg[0]
    rows = _pool_rows(ctx)
    hd = f"{g['away']['name']} at {g['home']['name']}"
    st = g.get("state")
    if st == "pre":
        if _Q_SCORE.search(q) or _Q_COVER.search(q):
            return (lambda t: bool(re.search(r"not (started|kicked|underway|on yet|begun)|hasn'?t (started|kicked|begun)|"
                                             r"kicks? off|kickoff|yet to|later", t, re.I)),
                    f" {hd} hasn't kicked off yet ({_kick_ct(g.get('start'))}).")
        return None
    if g.get("away_score") is None:
        return None
    a, h = g["away_score"], g["home_score"]
    when = clock_text(g)
    score = f"{g['away']['name']} {a}, {g['home']['name']} {h}"
    both = (lambda t, a=a, h=h: bool(re.search(_num_rx(a), t)) and bool(re.search(_num_rx(h), t)))
    if _Q_COVER.search(q):
        row = rows.get(str(g["id"]))
        if not row or row.get("line") is None:
            return (lambda t: bool(re.search(r"\b(not|isn'?t|wasn'?t|aren'?t)\b.{0,25}\bpool\b|\bno (league |pool )?(line|spread)\b|"
                                             r"\boutside the pool\b|\bnot one of\b", t, re.I)),
                    f" {hd} isn't a pool game this week, so there's no league line to cover.")
        cs = cover_status(row["away"], row["home"], row["line"], a, h)
        m = abs(h + float(row["line"]) - a)
        if not cs:
            return None
        return (lambda t, m=m: bool(re.search(_num_rx(m), t)),
                f" {'Final' if st == 'post' else 'Right now (' + when + ')'}: {cs.replace('covering', 'covered', 1) if st == 'post' else cs}.")
    if _Q_TOTAL.search(q) and not _Q_PICKS.search(q):
        tot = a + h
        return (lambda t, tot=tot: bool(re.search(_num_rx(tot), t)), f" {tot} total points so far ({score}, {when}).")
    if _Q_QB.search(q):
        names = [t for t in (g["away"], g["home"]) if any(re.search(rf"(?<![\w']){re.escape(k)}(?!\w)", q, 0 if cs_ else re.I)
                                                          for k, cs_ in _name_keys(t))]
        team = names[0]["name"] if len(names) == 1 else None
        qb = (g.get("qb") or {}).get(team) if team else None
        if not qb:
            return None
        cur = qb.get("current") or qb.get("starter")
        last = re.sub(r",? (Jr\.?|Sr\.?|II|III|IV)$", "", cur).split(".")[-1].split(" ")[-1]
        ch = (qb.get("changes") or [])[-1:]
        why = ""
        if ch:
            inj = any(x.get("qb") and _abbr_key(x["player"]) == _abbr_key(ch[0]["from"]) for x in g.get("injuries") or [])
            why = (f" (took over from {ch[0]['from']} at {period_label(ch[0]['period'])} {ch[0]['clock']}"
                   + (f", after {ch[0]['from']} was reported injured" if inj else "") + ")")
        return (lambda t, last=last: last.lower() in t.lower(), f" {team} QB right now: {cur}{why}".rstrip(".") + ".")
    m_td = re.search(r"\bhow many (tds?|touchdowns?|scores)\b", q, re.I)
    if m_td and g.get("tds") is not None:
        words = {w.lower() for w in re.findall(r"[A-Za-z][a-z'\-]{2,}", q)}
        who = [n for n in g["tds"] if re.sub(r",? (Jr\.?|Sr\.?|II|III|IV)$", "", n).split(" ")[-1].lower() in words]
        if len(who) == 1:
            v = g["tds"][who[0]]
            n = v["rush"] + v["rec"] + v["return"]
            parts = ", ".join(f"{v[k]} {lab}" for k, lab in (("rush", "rushing"), ("rec", "receiving"), ("return", "return"),
                                                            ("pass", "passing")) if v.get(k))
            nums = {0: "zero", 1: "one", 2: "two", 3: "three", 4: "four", 5: "five"}
            return (lambda t, n=n: bool(re.search(rf"\b({n}|{nums.get(n, n)})\b", t, re.I)),
                    f" {who[0]} has {n} TD{'s' * (n != 1)} so far ({parts}, as of {when}).")
    m_cnt = re.search(r"\bhow many (picks|ints?|interceptions?|turnovers|fumbles)\b", q, re.I)
    if m_cnt and g.get("detail"):
        words = {w.lower() for w in re.findall(r"[A-Za-z][a-z'\-]{2,}", q)}
        tos = [(tm, x) for tm, lst in (g.get("turnovers") or {}).items() for x in lst]
        qbs = {re.sub(r",? (Jr\.?|Sr\.?|II|III|IV)$", "", n).split(".")[-1].split(" ")[-1].lower(): n
               for q_ in (g.get("qb") or {}).values() for n in (q_.get("last_seen") or {})}
        who = [n for last, n in qbs.items() if last in words]
        if len(who) == 1 and re.search(r"pick|int", m_cnt.group(1), re.I):
            key_ = _abbr_key(who[0])
            n = sum(1 for _, x in tos if x["kind"] == "INT" and x.get("who") and _abbr_key(x["who"]) == key_)
            nums = {0: "zero|no|none", 1: "one|1", 2: "two|2", 3: "three|3", 4: "four|4"}
            return (lambda t, n=n: bool(re.search(rf"\b({nums.get(n, n)})\b", t, re.I)),
                    f" {who[0]} has thrown {n} interception{'s' * (n != 1)} so far (as of {when}).")
    m_int = re.search(r"\bwho (threw|tossed)\b.{0,25}\b(pick|int|interception)\b|\bwho fumbled\b", q, re.I)
    if m_int and g.get("turnovers"):
        want = "fumble" if "fumbl" in m_int.group(0).lower() else "INT"
        teams = [t["name"] for t in (g["away"], g["home"]) if any(
            re.search(rf"(?<![\w']){re.escape(k)}(?!\w|'(?!s\b))", q, 0 if cs_ else re.I) for k, cs_ in _name_keys(t))]
        xs = [(tm, x) for tm, lst in g["turnovers"].items() for x in lst
              if x["kind"].startswith(want[:3]) and x.get("who") and (len(teams) != 1 or tm == teams[0])]
        if xs:
            tm, x = max(xs, key=lambda y: _tkey(y[1]["period"], y[1]["clock"]))
            last = re.sub(r",? (Jr\.?|Sr\.?|II|III|IV)$", "", x["who"]).split(".")[-1].split(" ")[-1]
            return (lambda t, last=last: last.lower() in t.lower(),
                    f" {x['who']} ({tm}) {'threw it' if want == 'INT' else 'fumbled'}: {x['kind']}"
                    f"{' returned for a TD' if x.get('td') else ''} {period_label(x['period'])} {x['clock']}.")
    if _Q_SCORER.search(q) and not re.search(r"\b(pick|int|interception|intercepted|fumbled?|sacked)\b", q, re.I):
        sc = list(g.get("scoring") or [])
        teams = [t["name"] for t in (g["away"], g["home"]) if any(
            re.search(rf"(?<![\w']){re.escape(k)}(?!\w|'(?!s\b))", q, 0 if cs_ else re.I) for k, cs_ in _name_keys(t))]
        if len(teams) == 1:
            sc = [x for x in sc if x["team"] == teams[0]]
        if not sc:
            return None
        pick = sc[0] if re.search(r"\bfirst\b", q, re.I) else sc[-1]
        if re.search(r"\bfirst\b", q, re.I) and g.get("scoring_n", 0) > len(g.get("scoring") or []):
            return None  # the first score scrolled out of the feed's last 6
        who = _scorer(pick)
        if not who:
            return None
        last = re.sub(r",? (Jr\.?|Sr\.?|II|III|IV)$", "", who).split(" ")[-1]
        return (lambda t, last=last: last.lower() in t.lower(), f" {_play_desc(pick, g)}.")
    if _Q_POSS.search(q) and st == "in":
        side = g.get("possession")
        team = g[side]["name"] if side in ("away", "home") else g.get("receiving")
        if not team:
            return None
        return (lambda t, team=team: team.split(" ")[0].lower() in t.lower(),
                f" {team} {'has the ball' if side else 'gets the ball next (kickoff after the score)'} ({when}).")
    if _Q_SCORE.search(q):
        return (both, f" {'Final' + when[5:] if st == 'post' else 'As of ' + when}: {score}.")
    return None


# ----------------------------------------------------------------------------
# claim check: a sentence that states a score / scorer / injury for a game in
# the rider must match the feed
# ----------------------------------------------------------------------------

_PAIR = re.compile(r"(?<![\d.+\-])(\d{1,2})\s*(?:-|–|to)\s*(\d{1,2})(?!\d|\.\d)")
_TEAM_NUM = r"(?:{t})\s+(?:is\s+|are\s+|has\s+|with\s+|at\s+)?(\d{{1,2}})(?!\d|\.\d)"
_SCORE_VERB = re.compile(r"\b(scored|scores|scoring|touchdown|td|ran it in|punched it in|took it in|caught|"
                         r"hauled in|field goal|fg|kicked)\b", re.I)
_INJ_WORD = re.compile(r"\b(injur\w*|hurt|went down|carted|limp\w*|left the game|out of the game|knocked out|"
                       r"concussion|banged up)\b", re.I)
_COVER_BY = re.compile(r"\bcover\w*\b[^.]{0,40}?\bby\s+(\d{1,2}(?:\.5)?)(?!\d)", re.I)
_MARGIN = re.compile(r"\b(?:up|leads?|leading|ahead|down|trails?|trailing|behind)\s+(?:by\s+)?(\d{1,2})\b"
                     r"(?!\s*(?:-|–|to\s+\d|points? to))", re.I)  # 'up 13 to 7' is a score, not a margin


def claim_check(games: list[dict]):
    """f(sentence) -> True to keep. Scores in a sentence that names a game's
    team must be the current score, the final, or a score listed after one of
    its scoring plays; a margin ('up 7') must match the current margin; a
    named scorer must appear in a scoring play; an injury claim about a named
    player must be in the reported list. None when no games."""
    if not games:
        return None
    gs = []
    for g in games:
        if g.get("away_score") is None:
            continue
        ok_pairs = {tuple(sorted((g["away_score"], g["home_score"])))}
        ok_pairs |= {tuple(sorted((s["away"], s["home"]))) for s in g.get("scoring") or []
                     if s.get("away") is not None}
        names = [k for side in ("away", "home") for k, _ in _name_keys(g[side])]
        scorers = " ".join((s.get("text") or "") for s in g.get("scoring") or []).lower()
        known = {p.lower() for p in g.get("players") or []}
        injured = {_abbr_key(x["player"]).split(".", 1)[-1].lower() for x in g.get("injuries") or []}
        injured |= {(x.get("full_name") or "").split(" ")[-1].lower() for x in g.get("injuries") or []}
        g["_qb_lasts"] = {re.sub(r",? (Jr\.?|Sr\.?|II|III|IV)$", "", n).split(".")[-1].split(" ")[-1].lower()
                          for q_ in (g.get("qb") or {}).values() for n in (q_.get("last_seen") or {})}
        g["_ints"] = {}
        for lst in (g.get("turnovers") or {}).values():
            for x in lst:
                if x["kind"] == "INT" and x.get("who"):
                    last = re.sub(r",? (Jr\.?|Sr\.?|II|III|IV)$", "", x["who"]).split(".")[-1].split(" ")[-1].lower()
                    g["_ints"][last] = g["_ints"].get(last, 0) + 1
        gs.append((g, ok_pairs, names, scorers, known, injured))

    def segments(sent: str) -> list[str]:
        """Per game, the part of the sentence about it: 'Wake covered by 40.5 and Oregon State is covering by
        1.5' checks 40.5 against Wake and 1.5 against Oregon State (one game per sentence: the whole sentence)."""
        ms = []
        for i, (_, _, names, *_r) in enumerate(gs):
            for n in names:
                for m in re.finditer(rf"(?<![\w']){re.escape(n)}(?!\w|'(?!s\b))", sent, 0 if n.isupper() else re.I):
                    ms.append((m.start(), m.end(), i))
        ms.sort(key=lambda x: (x[0], -x[1]))
        keep, last_end = [], -1
        for st_, en, i in ms:
            if st_ >= last_end:
                keep.append((st_, en, i))
                last_end = en
        seg = [None] * len(gs)
        if not keep:  # no team named: a player only one game in the feed has ('Austin Manning has thrown 1 pick')
            caps = {w.lower() for w in re.findall(r"\b[A-Z][a-z'\-]{2,}\b", sent)} - _COMMON
            owners = [i for i, (_, _, _, _, known, _) in enumerate(gs) if caps & known]
            for i in owners:
                if not any(caps & gs[j][4] & gs[i][4] for j in owners if j != i):
                    seg[i] = sent
            return seg
        if len({i for _, _, i in keep}) <= 1:
            for _, _, i in keep:
                seg[i] = sent
            return seg
        for k, (st_, _, i) in enumerate(keep):
            nxt = next((x[0] for x in keep[k + 1:] if x[2] != i), len(sent))
            lo = 0 if k == 0 else st_
            seg[i] = (seg[i] or "") + " " + sent[lo:nxt]
        return seg

    def check(sent: str) -> bool:
        if re.search(r"\b(\d+|a|one|few) seconds? ago\b", sent, re.I):
            return False  # the feed never knows the second ('about 1 second ago')
        segs = segments(sent)
        for (g, ok_pairs, names, scorers, known, injured), seg in zip(gs, segs):
            if seg is None:
                continue
            for a, b in _PAIR.findall(seg):
                pair = tuple(sorted((int(a), int(b))))
                if pair not in ok_pairs and not (pair[0] == pair[1] == 0 and g.get("state") == "pre"):
                    return False
            tnum = {}
            for side in ("away", "home"):
                for k, _ in _name_keys(g[side]):
                    for m in re.finditer(_TEAM_NUM.format(t=re.escape(k)), seg, 0 if k.isupper() else re.I):
                        tnum[side] = int(m.group(1))
            if len(tnum) == 2 and tuple(sorted(tnum.values())) not in ok_pairs:
                return False
            if g.get("in_pool") is False and re.search(r"\b(pool|league) line\b|\bcover\w*\b|\bspread\b|\bline is\b", seg, re.I) \
                    and not re.search(r"\b(not|isn'?t|wasn'?t|aren'?t)\b.{0,25}\bpool\b|\bno (league |pool )?(line|spread)\b|"
                                      r"\boutside the pool\b|\bwithout a line\b", seg, re.I):
                return False  # 'the pool line is LSU -5.5' on a game that isn't in the pool
            if g.get("cover_margin") is not None:  # 'covering by 11' when it's 13.5 against the line
                for m in _COVER_BY.finditer(seg):
                    if float(m.group(1)) != g["cover_margin"]:
                        return False
            if g.get("cover_side") in ("away", "home") and g.get("cover_margin"):  # 'SMU is covering' when Wake is
                loser = g["home" if g["cover_side"] == "away" else "away"]
                winner = g[g["cover_side"]]
                for side_t, wrong_if_neg in ((loser, False), (winner, True)):
                    for k, _ in _name_keys(side_t):
                        for m in re.finditer(rf"(?<![\w']){re.escape(k)}(?!\w)([^.?!,;]{{0,30}}?)\bcover(?:ing|s)\b", seg,
                                             0 if k.isupper() else re.I):
                            other = loser if side_t is winner else winner
                            if any(re.search(rf"(?<![\w']){re.escape(k2)}(?!\w)", m.group(1), 0 if k2.isupper() else re.I)
                                   for k2, _ in _name_keys(other)):
                                continue  # 'Wake's lead means SMU isn't covering': the verb is the other team's
                            neg = bool(re.search(r"\bnot\b|n't\b|\bnever\b|\bno longer\b|\bfail", m.group(1), re.I))
                            if neg == wrong_if_neg and not re.search(r"\?$", sent.strip()):
                                return False
            if g.get("state") == "in" and g.get("away_score") is not None:
                cur = abs(g["away_score"] - g["home_score"])
                for m in _MARGIN.finditer(seg):
                    if int(m.group(1)) != cur and not re.search(r"\b(cover|spread|line|number)\b", seg, re.I):
                        return False
            hk = [k for k, _ in _name_keys(g["home"])]
            ak = [k for k, _ in _name_keys(g["away"])]
            if any(re.search(rf"(?<![\w']){re.escape(h_)}\s+(?:at|@)\s+{re.escape(a_)}(?!\w)", seg, re.I) for h_ in hk for a_ in ak):
                return False  # 'Missouri State at New Mexico State': home and away swapped
            if g.get("state") in ("in", "post"):  # 'as of 30:00' at halftime: a game clock the feed never showed
                clocks = {str(g.get("clock") or "")} if g.get("state") == "in" and "HALFTIME" not in str(g.get("status_name") or "").upper() else set()
                clocks |= {str(x.get("clock")) for x in (g.get("scoring") or []) + (g.get("key") or []) + (g.get("injuries") or [])}
                clocks |= {str(c["clock"]) for q_ in (g.get("qb") or {}).values() for c in q_.get("changes") or []}
                clocks |= {str(x["clock"]) for lst in (g.get("turnovers") or {}).values() for x in lst}
                clocks |= {c.lstrip("0") or "0:00" for c in list(clocks)}
                for m in re.finditer(r"\b(?:as of|with)\s+(\d{1,2}:\d{2})\b", seg, re.I):
                    if m.group(1) not in clocks and m.group(1).lstrip("0") not in clocks:
                        return False
            if re.search(r"\bon the board\b|\bopened the scoring\b|\bfirst (score|points|td|touchdown)\b", seg, re.I) \
                    and g.get("scoring_n", 0) == len(g.get("scoring") or []):
                firsts_by_team = {}
                for x in g.get("scoring") or []:
                    firsts_by_team.setdefault(x["team"], x.get("text") or "")
                named_p = {w.lower() for w in re.findall(r"\b[A-Z][a-z'\-]{2,}\b", seg)} & (known | {
                    w.lower() for x in g.get("scoring") or [] for w in re.findall(r"[A-Z][a-z'\-]{2,}", x.get("text") or "")})
                named_p -= {w.lower() for n in names for w in n.split()}
                if named_p and not any(named_p & {w.lower() for w in re.findall(r"[A-Z][a-z'\-]{2,}", t_)}
                                       for t_ in firsts_by_team.values()):
                    return False  # 'Searcy put them on the board' when Clark scored first
            m_q = re.search(r"\b(two|three|both|\d) (?:of (?:their|the) )?(?:qbs|quarterbacks)\b", seg, re.I)
            if m_q and _INJ_WORD.search(seg):
                n_inj = sum(1 for x in g.get("injuries") or [] if x.get("qb"))
                w_ = m_q.group(1).lower()
                n_say = int(w_) if w_.isdigit() else {"two": 2, "both": 2, "three": 3}[w_]
                if n_say != n_inj:
                    return False  # 'two of their QBs are reported injured' when one is
            m_t = re.search(r"\b([A-Z][a-z'\-]+) (?:threw|tossed) (?:that|the|a|an|his)\b[^.?!]{0,12}\b(?:pick|int|interception)\b", seg)
            if m_t and g.get("_ints") and m_t.group(1).lower() not in g["_ints"] \
                    and m_t.group(1).lower() not in {w.lower() for n in names for w in n.split()}:
                return False  # 'Zeke Martinez threw that pick' (Boise's 2-pt passer; Murphy threw it)
            for m in re.finditer(r"\b(no|zero|one|two|three|four|five|\d) (?:td|tds|touchdowns?)\b", seg, re.I):
                before = re.findall(r"\b([A-Z][a-z'\-]+)\b", seg[max(0, m.start() - 60):m.start()])
                tally = {re.sub(r",? (Jr\.?|Sr\.?|II|III|IV)$", "", n).split(" ")[-1].lower(): v
                         for n, v in (g.get("tds") or {}).items()}
                last = next((w.lower() for w in reversed(before) if w.lower() in tally), None)
                if last and g.get("scoring_n", 0) >= 0:  # 'Rasean Jones has 1 touchdown' when he has 2
                    v = tally[last]
                    n = {"no": 0, "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5}.get(m.group(1).lower())
                    n = int(m.group(1)) if n is None else n
                    if n not in (v["rush"] + v["rec"] + v["return"], v["pass"]):
                        return False
            team_words = {w.lower() for n in names for w in n.split()}
            for m in re.finditer(r"\b([A-Z][a-z'\-]+) ([A-Z][a-z'\-]+)\b", seg):  # 'Austin Manning' for Arch Manning
                ok_first = (g.get("firsts") or {}).get(m.group(2).lower())
                if ok_first and m.group(1).lower() not in team_words and m.group(1) not in ok_first \
                        and m.group(1).lower() not in _COMMON and m.group(1) not in ("The", "QB", "Coach"):
                    return False
            for m in re.finditer(r"\b(no|zero|one|two|three|four|\d) (?:picks?|ints?|interceptions?)\b", seg, re.I):
                before = re.findall(r"\b([A-Z][a-z'\-]+)\b", seg[max(0, m.start() - 60):m.start()])
                last = next((w.lower() for w in reversed(before) if w.lower() in (g.get("_ints") or {})
                             or w.lower() in (g.get("_qb_lasts") or ())), None)
                if last:  # 'Manning has thrown 1 interception' when he has 2
                    n = {"no": 0, "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4}.get(m.group(1).lower())
                    n = int(m.group(1)) if n is None else n
                    if n != (g.get("_ints") or {}).get(last, 0):
                        return False
            words = {w.lower() for w in re.findall(r"[A-Z][a-z'\-]{2,}", seg)}
            if _SCORE_VERB.search(seg):
                bad = {w for w in words if w in known and w not in scorers}
                if bad:
                    return False
            if _INJ_WORD.search(seg):
                named = {w for w in words if w in known}
                if named and not (named & injured):
                    return False
        return True
    return check


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------


def _main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cmd = argv[0] if argv else "status"
    if cmd == "run":
        logging.getLogger("httpx").setLevel(logging.WARNING)  # a line per request would be ~100/min on Saturdays
        run()
        return 0
    if cmd == "once":
        p = Poller()
        p.next_sb = 0
        p.step()
        print(json.dumps({"mode": p.mode, "games": len(p.games), "live": sum(g.get("state") == "in" for g in p.games.values()),
                          "tracked": p.tracked(), "stats": p.stats}))
        return 0
    if cmd == "show":
        q = " ".join(argv[1:]) or "what's happening"
        txt, hit = rider(q, refresh=False)
        print(txt or "(no live rider for that)")
        return 0
    if cmd == "status":
        d = load()
        gs = d.get("games") or {}
        print(json.dumps({"updated_at": d.get("updated_at"), "mode": d.get("mode"), "games": len(gs),
                          "live": sum(g.get("state") == "in" for g in gs.values()),
                          "detail": sum(bool(g.get("detail")) for g in gs.values())}))
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(_main())
