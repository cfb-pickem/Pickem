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

    # scoring plays, oldest first
    scoring = []
    for s in d.get("scoringPlays") or []:
        per, clk = _when(s)
        tid = str((s.get("team") or {}).get("id") or "")
        scoring.append({"team": name.get(tid, (s.get("team") or {}).get("displayName") or "?"),
                        "abbr": abbr.get(tid, ""), "type": (s.get("type") or {}).get("text") or "Score",
                        "text": re.sub(r"\s{2,}", " ", str(s.get("text") or "")).strip(),
                        "period": per, "clock": clk, "away": _int(s.get("awayScore")),
                        "home": _int(s.get("homeScore")), "id": s.get("id")})
    scoring.sort(key=lambda s: (_tkey(s["period"], s["clock"]), (s["away"] or 0) + (s["home"] or 0)))

    key, injuries, passers = [], [], {}
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
        m = _PASSER.search(text)
        if m and ty not in NON_PLAYS and off:
            passers.setdefault(off, []).append({"name": m.group(2), "jersey": m.group(1), "i": i,
                                                "period": per, "clock": clk})
        for m in _INJ.finditer(text):
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
    if last and g["state"] == "in":
        end = last.get("end") or {}
        g["possession"] = _side_of(g, (end.get("team") or {}).get("id"))
        g["down_distance"] = end.get("downDistanceText") or end.get("shortDownDistanceText")
        g["last_play"] = _clean_play(last.get("text"))
        yte = _int(end.get("yardsToEndzone"))
        g["red_zone"] = yte is not None and yte <= 20
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
    g.update({"scoring": scoring[-KEEP_SCORING:], "scoring_n": len(scoring), "key": key[-KEEP_KEY:],
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
    return f"{side_label(cov, home, ln)} covering by {_num(abs(m))} ({side_label(other, home, ln)} not covering)"


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


def refresh_board_if_stale(max_age: float = MAX_AGE) -> bool:
    """Chat side: if the poller's file is older than max_age (poller down) and
    games may be on, read the scoreboard once into the overlay."""
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
    r"possession|ball|who has the ball|game|games|update|updates)\b", re.I)
_WHAT_HAPPENING = re.compile(r"\b(what'?s happening|what'?s going on|any updates?|what'?s on|games? on|live games?|"
                             r"scores?\b(?! of)|around the country|anything good)\b", re.I)


def _name_keys(t: dict) -> list[tuple[str, bool]]:
    """(text, case_sensitive) ways a question names a team."""
    out = [(t["name"], False)]
    n = t["name"]
    if n.endswith(" State"):
        out.append((n[:-6] + " St", False))
    out.append((n.replace("'", "").replace("’", ""), False))
    if t.get("abbr") and len(t["abbr"]) >= 2:
        out.append((t["abbr"], True))
    if t.get("nick") and len(t["nick"]) > 4:
        out.append((t["nick"], False))
    return out


def games_for(question: str, games: dict, pool_ids=()) -> list[dict]:
    """Games the question touches: named teams (longest names first), then
    players in a game's feed; a generic live ask gets the live pool games plus
    the closest ranked live games."""
    q = question or ""
    hits, taken = [], q
    pairs = []
    for g in games.values():
        for side in ("away", "home"):
            for k, cs in _name_keys(g[side]):
                pairs.append((len(k), k, cs, g))
    for _, k, cs, g in sorted(pairs, key=lambda x: -x[0]):
        rx = re.compile(rf"(?<![\w']){re.escape(k)}(?!\w|'(?!s\b))", 0 if cs else re.I)
        if rx.search(taken):
            if g not in hits:
                hits.append(g)
            taken = rx.sub(" | ", taken)
    if not hits:  # a player the feed knows: last names of 4+ letters
        words = {w.lower() for w in re.findall(r"[A-Za-z][A-Za-z'\-]{3,}", q)}
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
    elif st == "in" and a is not None and a > MAX_AGE:
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
        lines.append(s + ".")
    sc = list(reversed(g.get("scoring") or []))
    if sc:
        lines.append("  Last score: " + _play_desc(sc[0], g) + ".")
        if len(sc) > 1:
            lines.append("  Earlier scores (newest first): " + " | ".join(_play_desc(x, g) for x in sc[1:4]) + ".")
    elif st == "in" and g.get("detail"):
        lines.append("  No points yet.")
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
                                                         or time.time() - data["updated_ts"] > MAX_AGE):
        if refresh_board_if_stale():
            data = load()
            games = data.get("games") or {}
    if not games:
        return "", []
    hit = games_for(question, games, list(pool) or data.get("pool_ids") or [])
    hit = [g for g in hit if g.get("state") in ("in", "post") or g["id"] in pool]
    # finals only when the question is about that game or nothing is live; skip stale finals from days ago
    if not hit:
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
    return head + "\n" + body, hit


_STATUS = {"in": "in_progress", "post": "final"}


def apply_to_ctx(ctx: dict, question: str, refresh: bool = True) -> str:
    """chat.py's one hook (run off the event loop). Builds the LIVE rider for
    the question (refreshing stale live games first) into ctx['live_rider'] /
    ['live_rider_games'], and lays the poller's fresher score over the pool
    rows chat already read from all_games (so live_block, cover status and the
    results guard all see the same, newest score). Returns the rider."""
    pool = {str(g["id"]): {"away": g["away"], "home": g["home"], "line": g.get("line")}
            for g in ctx.get("games_this_week") or [] if not g.get("tiebreaker")}
    try:
        text, hit = rider(question, pool, refresh=refresh)
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
        if not g or g.get("state") not in _STATUS or (age(g, now) or 1e9) > MAX_AGE:
            continue
        r = rows.get(str(pg["id"]))
        if r is None:
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
# claim check: a sentence that states a score / scorer / injury for a game in
# the rider must match the feed
# ----------------------------------------------------------------------------

_PAIR = re.compile(r"(?<![\d.+\-])(\d{1,2})\s*(?:-|–|to)\s*(\d{1,2})(?!\d|\.\d)")
_TEAM_NUM = r"(?:{t})\s+(?:is\s+|are\s+|has\s+|with\s+|at\s+)?(\d{{1,2}})(?!\d|\.\d)"
_SCORE_VERB = re.compile(r"\b(scored|scores|scoring|touchdown|td|ran it in|punched it in|took it in|caught|"
                         r"hauled in|field goal|fg|kicked)\b", re.I)
_INJ_WORD = re.compile(r"\b(injur\w*|hurt|went down|carted|limp\w*|left the game|out of the game|knocked out|"
                       r"concussion|banged up)\b", re.I)
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
        gs.append((g, ok_pairs, names, scorers, known, injured))

    def check(sent: str) -> bool:
        for g, ok_pairs, names, scorers, known, injured in gs:
            if not any(re.search(rf"(?<![\w']){re.escape(n)}(?!\w|'(?!s\b))", sent, 0 if n.isupper() else re.I)
                       for n in names):
                continue
            for a, b in _PAIR.findall(sent):
                pair = tuple(sorted((int(a), int(b))))
                if pair not in ok_pairs and not (pair[0] == pair[1] == 0 and g.get("state") == "pre"):
                    return False
            tnum = {}
            for side in ("away", "home"):
                for k, _ in _name_keys(g[side]):
                    for m in re.finditer(_TEAM_NUM.format(t=re.escape(k)), sent, 0 if k.isupper() else re.I):
                        tnum[side] = int(m.group(1))
            if len(tnum) == 2 and tuple(sorted(tnum.values())) not in ok_pairs:
                return False
            if g.get("state") == "in" and g.get("away_score") is not None:
                cur = abs(g["away_score"] - g["home_score"])
                for m in _MARGIN.finditer(sent):
                    if int(m.group(1)) != cur and not re.search(r"\b(cover|spread|line|number)\b", sent, re.I):
                        return False
            words = {w.lower() for w in re.findall(r"[A-Z][a-z'\-]{2,}", sent)}
            if _SCORE_VERB.search(sent):
                bad = {w for w in words if w in known and w not in scorers}
                if bad:
                    return False
            if _INJ_WORD.search(sent):
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
