#!/usr/bin/env python3
"""Picky's personnel store (docs/picky-code-ontology-plan-2026-11-18.md 2.1, section 5 phase 4).

Pure code + free feeds, no model:
  - ESPN team rosters (site API, free): id, name, position, class, jersey, team, plus ESPN's own injured/suspended
    groups. roster_tick() rides the fact store's 5-minute tick (factstore.refresh): a couple of teams per tick, a
    weekly TTL, so a full pass takes a few hours and then idles until next week.
  - CFBD /games/players (one call per week, all FBS games) -> compact player_game rows. backfill() for the season
    so far, refresh_player_games() from cfbd.py's refresh (daily 06:15 + the Saturday/Sunday finals runs): at most
    one call per run, a final pull once a week's games are over plus up to two Saturday-evening partials. Every
    call goes through cfbd.py's budget (check_budget, _record_call, SHARED_RESERVE). Weeks are mirrored to
    Supabase picky.sheet (kind 'player_game') so a lost cache never re-spends CFBD calls.
  - Derived: qb_ledger() (QB with the most attempts started: SU and ATS as starter, first start, season line).
  - normalize(): ONE availability list (player, team, game, status, detail, verbatim quote, source, date,
    confidence) from slate_intel notes, dossiers, news items, ESPN rosters and ESPN play-by-play. Works from news +
    ESPN alone; the offline-written notes (slate_intel, dossiers) only add to it. Latest report wins; TBD stays TBD.
  - The static position value table (position_value.json) with its validator.

Local cache: server/cache/personnel.sqlite (gitignored). The fact store build reads it (factstore.Inputs).

  personnel.py backfill 1 11      # CFBD weeks 1..11 of this season (one call each; refuses past the ceiling)
  personnel.py rosters [N]        # fetch N due rosters now (free)
  personnel.py status
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import cfbd  # noqa: E402

log = logging.getLogger("picky.personnel")
_norm = __import__("functools").lru_cache(maxsize=None)(cfbd.norm)
DB_PATH = Path(os.environ.get("PICKY_PERSONNEL_DB", HERE / "cache" / "personnel.sqlite"))
VALUES_PATH = HERE / "position_value.json"
ROSTER_URL = "https://site.api.espn.com/apis/site/v2/sports/football/college-football/teams/{}/roster"
ROSTER_TTL = 6.5 * 86400
ROSTER_PER_TICK = 2
ROSTER_RETRY_S = 3600
BACKFILL_CEILING = int(os.environ.get("PICKY_PERSONNEL_CEILING", "110"))  # never let a backfill push the month past this
FINAL_AFTER_S = 5 * 3600      # a week is over 5 h after its last kickoff
PARTIAL_GAP_S = 4 * 3600
MAX_PARTIALS = 2
CT = dt.timezone(dt.timedelta(hours=-6))
STATUSES = ("out", "season", "doubtful", "questionable", "probable", "TBD", "available", "benched", "suspended",
            "portal", "opt-out")


# =============================================================================================================
# local cache
# =============================================================================================================

def connect(path: Path | str | None = None) -> sqlite3.Connection:
    p = Path(path or DB_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(p)
    db.executescript("""
        CREATE TABLE IF NOT EXISTS rosters (team TEXT PRIMARY KEY, espn_id TEXT, fetched_at REAL, tried_at REAL,
                                            json TEXT);
        CREATE TABLE IF NOT EXISTS player_games (season INTEGER, week INTEGER, fetched_at REAL, partials INTEGER,
                                                 json TEXT, PRIMARY KEY (season, week));
    """)
    return db


def rosters(db: sqlite3.Connection) -> dict[str, dict]:
    out = {}
    for team, eid, f, j in db.execute("select team, espn_id, fetched_at, json from rosters where json is not null"):
        d = json.loads(j)
        out[team] = {"espn_id": eid, "fetched_at": f, "players": d.get("players") or [],
                     "status_rows": d.get("status_rows") or []}
    return out


def player_games(db: sqlite3.Connection, season: int | None = None) -> list[dict]:
    q = "select json from player_games" + (" where season=?" if season else "") + " order by season, week"
    rows = []
    for (j,) in db.execute(q, (season,) if season else ()):
        rows += json.loads(j)
    return rows


def weeks_fetched(db: sqlite3.Connection, season: int) -> dict[int, dict]:
    return {w: {"fetched_at": f, "partials": p or 0}
            for w, f, p in db.execute("select week, fetched_at, partials from player_games where season=?", (season,))}


def _save_week(db, season: int, week: int, rows: list[dict], now: float, partial: bool) -> None:
    old = db.execute("select partials from player_games where season=? and week=?", (season, week)).fetchone()
    partials = ((old[0] or 0) if old else 0) + (1 if partial else 0)
    db.execute("insert or replace into player_games values(?,?,?,?,?)",
               (season, week, now, partials, json.dumps(rows, separators=(",", ":"))))
    db.commit()


# =============================================================================================================
# ESPN rosters
# =============================================================================================================

def parse_roster(data: dict, team: str) -> list[dict]:
    out = []
    for grp in data.get("athletes") or []:
        for a in grp.get("items") or []:
            out.append({"id": str(a.get("id") or ""), "name": a.get("fullName") or a.get("displayName") or "",
                        "pos": (a.get("position") or {}).get("abbreviation") or "",
                        "cls": (a.get("experience") or {}).get("displayValue") or "",
                        "jersey": str(a.get("jersey") or ""), "team": team, "group": grp.get("position") or ""})
    return [p for p in out if p["name"]]


_ESPN_INJ = {"out": "out", "questionable": "questionable", "doubtful": "doubtful", "probable": "probable",
             "day-to-day": "questionable", "suspension": "suspended", "suspended": "suspended",
             "injured reserve": "out", "ir": "out"}


def _row(player, team, status, quote, source_name, source_url, reported_at, confidence, src, detail="", pos="",
         game_id=None, role=None) -> dict:
    return {"player": player, "team": team, "game_id": game_id, "status": status, "detail": detail, "quote": quote,
            "source_name": source_name, "source_url": source_url, "reported_at": reported_at,
            "confidence": confidence, "src": src, "pos": pos, "role": role}


def roster_status_rows(players: list[dict], data: dict, fetched_iso: str) -> list[dict]:
    """ESPN's own injured/suspended groups and per-player injury entries as availability rows."""
    raw = {str(a.get("id")): a for g in data.get("athletes") or [] for a in g.get("items") or []}
    eid = ((data.get("team") or {}).get("id")) or ""
    url = ROSTER_URL.format(eid)
    out = []
    for p in players:
        a = raw.get(p["id"]) or {}
        inj = (a.get("injuries") or [None])[0]
        if inj:
            st = _ESPN_INJ.get(str(inj.get("status") or "").lower())
            if st:
                detail = ((inj.get("type") or {}).get("description") or (inj.get("details") or {}).get("type") or "")
                out.append(_row(p["name"], p["team"], st, f"ESPN lists him {inj.get('status')}"
                                + (f" ({detail})" if detail else ""), "ESPN roster", url,
                                inj.get("date") or fetched_iso, "medium", "espn_injury", detail=detail, pos=p["pos"]))
                continue
        if p["group"] == "injuredReserveOrOut":
            out.append(_row(p["name"], p["team"], "out", "ESPN's roster lists him under injured/out", "ESPN roster",
                            url, fetched_iso, "medium", "espn_roster", pos=p["pos"]))
        elif p["group"] == "suspended":
            out.append(_row(p["name"], p["team"], "suspended", "ESPN's roster lists him as suspended", "ESPN roster",
                            url, fetched_iso, "medium", "espn_roster", pos=p["pos"]))
    return out


def _fetch_roster(espn_id: str) -> dict:
    import httpx
    r = httpx.get(ROSTER_URL.format(espn_id), timeout=15)
    r.raise_for_status()
    return r.json()


def fbs_teams(games: list[dict]) -> dict[str, str]:
    """{team name: ESPN id} for FBS teams (CFBD team ids are ESPN's)."""
    out = {}
    for g in games or []:
        for side in ("home", "away"):
            if g.get(f"{side}Classification") == "fbs" and g.get(f"{side}Team") and g.get(f"{side}Id"):
                out[g[f"{side}Team"]] = str(g[f"{side}Id"])
    return out


def roster_tick(db, teams: dict[str, str], fetch=None, now: float | None = None, per_tick: int = ROSTER_PER_TICK,
                ttl: float = ROSTER_TTL) -> dict:
    """Fetch up to per_tick rosters that are missing or older than ttl (oldest first)."""
    now = now or time.time()
    fetch = fetch or _fetch_roster
    have = {t: (f or 0, tr or 0) for t, f, tr in db.execute("select team, fetched_at, tried_at from rosters")}
    due = [t for t in teams if have.get(t, (0, 0))[0] < now - ttl and have.get(t, (0, 0))[1] < now - ROSTER_RETRY_S]
    due.sort(key=lambda t: have.get(t, (0, 0))[0])
    n = bad = 0
    for t in due[:per_tick]:
        try:
            data = fetch(teams[t])
            ps = parse_roster(data, t)
            iso = dt.datetime.fromtimestamp(now, dt.timezone.utc).isoformat(timespec="seconds")
            body = {"players": ps, "status_rows": roster_status_rows(ps, data, iso)}
            db.execute("insert or replace into rosters values(?,?,?,?,?)",
                       (t, teams[t], now, now, json.dumps(body, separators=(",", ":"))))
            n += 1
        except Exception as e:  # noqa: BLE001 - a failed team retries in an hour; never breaks the tick
            log.warning("personnel: roster %s failed: %r", t, e)
            db.execute("insert into rosters(team, espn_id, tried_at) values(?,?,?) "
                       "on conflict(team) do update set tried_at=excluded.tried_at", (t, teams[t], now))
            bad += 1
        db.commit()
    return {"fetched": n, "failed": bad}


def tick(db_path: Path | None = None, cfbd_db: Path | None = None, now: float | None = None) -> dict:
    """The fact store tick's roster step: FBS team ids from the CFBD games cache, then roster_tick."""
    c = sqlite3.connect(cfbd_db or cfbd.DB_PATH)
    try:
        row = c.execute("select json from datasets where name='games'").fetchone()
    finally:
        c.close()
    teams = fbs_teams(json.loads(row[0]) if row else [])
    db = connect(db_path)
    try:
        return roster_tick(db, teams, now=now)
    finally:
        db.close()


# =============================================================================================================
# CFBD /games/players
# =============================================================================================================

def _int(s) -> int:
    try:
        return int(float(str(s).strip()))
    except (TypeError, ValueError):
        return 0


_FIELDS = {("passing", "YDS"): "yds", ("passing", "TD"): "td", ("passing", "INT"): "int",
           ("rushing", "CAR"): "car", ("rushing", "YDS"): "rush_yds", ("rushing", "TD"): "rush_td",
           ("receiving", "REC"): "rec", ("receiving", "YDS"): "rec_yds", ("receiving", "TD"): "rec_td"}
_ZERO = {"cmp": 0, "att": 0, "yds": 0, "td": 0, "int": 0, "car": 0, "rush_yds": 0, "rush_td": 0, "rec": 0,
         "rec_yds": 0, "rec_td": 0}


def parse_games_players(data: list[dict], season: int, week: int) -> list[dict]:
    """CFBD /games/players -> one row per player per game with passing / rushing / receiving numbers."""
    out = []
    for g in data or []:
        teams = g.get("teams") or []
        for t in teams:
            opp = next((x.get("team") for x in teams if x is not t), None)
            by: dict[str, dict] = {}
            for cat in t.get("categories") or []:
                cn = (cat.get("name") or "").lower()
                if cn not in ("passing", "rushing", "receiving"):
                    continue
                for ty in cat.get("types") or []:
                    tn = ty.get("name") or ""
                    for a in ty.get("athletes") or []:
                        pid, name = str(a.get("id") or ""), a.get("name") or ""
                        if not name or pid.startswith("-") or name.strip().lower() == "team":
                            continue
                        r = by.setdefault(pid, {"game_id": str(g.get("id")), "season": season, "week": week,
                                                "team": t.get("team"), "opp": opp, "home": t.get("homeAway") == "home",
                                                "pts": t.get("points"), "id": pid, "name": name, **_ZERO})
                        if cn == "passing" and tn == "C/ATT":
                            c, _, at = str(a.get("stat") or "0/0").partition("/")
                            r["cmp"], r["att"] = _int(c), _int(at)
                        elif (cn, tn) in _FIELDS:
                            r[_FIELDS[(cn, tn)]] = _int(a.get("stat"))
            out += list(by.values())
    return out


def _client():
    import httpx
    return httpx.Client(base_url=cfbd.API, timeout=60.0,
                        headers={"Authorization": f"Bearer {cfbd._api_key()}", "Accept": "application/json"})


def _fetch_week(cdb, db, client, season: int, week: int, now: float, partial: bool, push) -> int | None:
    """One paid call. Returns rows saved, or None on a failed call (counted either way)."""
    cfbd.check_budget(cdb, 1)
    path = "/games/players"
    try:
        r = client.get(path, params={"year": season, "week": week, "seasonType": "regular", "classification": "fbs"})
    except Exception as e:  # noqa: BLE001 - network: count it, don't retry (retries cost calls)
        cfbd._record_call(cdb, path, 0, None)
        log.warning("personnel: week %s fetch failed: %r", week, e)
        return None
    rem = r.headers.get("x-calllimit-remaining")
    cfbd._record_call(cdb, path, r.status_code, int(rem) if rem and str(rem).isdigit() else None)
    if r.status_code != 200:
        log.warning("personnel: week %s status %s", week, r.status_code)
        return None
    rows = parse_games_players(r.json(), season, week)
    _save_week(db, season, week, rows, now, partial)
    if push:
        try:
            mirror_week(season, week, rows, now, None if push == "default" else push)
        except Exception as e:  # noqa: BLE001 - the mirror never blocks the local cache
            log.warning("personnel: supabase mirror failed: %r", e)
    return len(rows)


def backfill(weeks: list[int], cfbd_db=None, db=None, client=None, season: int | None = None,
             now: float | None = None, ceiling: int = BACKFILL_CEILING, push=None) -> dict:
    """One /games/players call per week. Refuses (spending nothing) unless every call fits cfbd's caps and the
    month stays at or under `ceiling` calls."""
    cdb = cfbd_db or cfbd.connect()
    db = db or connect()
    season = season or cfbd.season_year()
    now = now or time.time()
    own = client is None
    client = client or _client()
    try:
        used = cfbd.calls_used(cdb)
        if used + len(weeks) > ceiling:
            raise cfbd.BudgetExceeded(f"personnel ceiling: {used} used + {len(weeks)} > {ceiling} this month")
        info = client.get("/info").json() or {}  # free
        cfbd.check_budget(cdb, len(weeks), info.get("usedCalls"))
        got, failed = {}, []
        for w in weeks:
            n = _fetch_week(cdb, db, client, season, w, now, False, push)
            if n is None:
                failed.append(w)
            else:
                got[w] = n
        return {"calls": len(weeks), "rows": got, "failed": failed, "calls_this_month": cfbd.calls_used(cdb)}
    finally:
        if own:
            client.close()


def _kick(s) -> float | None:
    try:
        return dt.datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _week_spans(games: list[dict]) -> dict[int, tuple[float, float]]:
    spans: dict[int, list] = {}
    for g in games or []:
        if g.get("seasonType") not in (None, "regular") or g.get("week") is None:
            continue
        k = _kick(g.get("startDate"))
        if k is None:
            continue
        s = spans.setdefault(int(g["week"]), [k, k])
        s[0], s[1] = min(s[0], k), max(s[1], k)
    return {w: (a, b) for w, (a, b) in spans.items()}


def _saturday_evening(now: float) -> bool:
    t = dt.datetime.fromtimestamp(now, CT)
    return (t.weekday() == 5 and t.hour >= 15) or t.weekday() == 6


def due_week(games: list[dict], fetched: dict[int, dict], now: float) -> int | None:
    """The one week worth a call now: a week whose games are over and wasn't pulled since (final pull), else this
    week's partial on a Saturday evening (4 h apart, two max). Missing older weeks heal one per run."""
    for w, (first, last) in sorted(_week_spans(games).items(), reverse=True):
        if first > now - 3 * 3600:
            continue
        f = fetched.get(w)
        final_at = last + FINAL_AFTER_S
        if now >= final_at:
            if not f or f["fetched_at"] < final_at:
                return w
            continue
        if _saturday_evening(now) and (not f or (now - f["fetched_at"] >= PARTIAL_GAP_S
                                                 and (f.get("partials") or 0) < MAX_PARTIALS)):
            return w
    return None


def refresh_player_games(cfbd_db=None, db=None, client=None, games: list[dict] | None = None,
                         season: int | None = None, now: float | None = None, push=None) -> dict:
    """cfbd.py refresh's personnel step: at most one /games/players call, only when a week is due."""
    cdb = cfbd_db or cfbd.connect()
    db = db or connect()
    season = season or cfbd.season_year()
    now = now or time.time()
    if games is None:
        row = cdb.execute("select json from datasets where name='games'").fetchone()
        games = json.loads(row[0]) if row else []
    fetched = weeks_fetched(db, season)
    if not fetched and push == "default":
        try:  # a lost cache: the Supabase copy first, so nothing is bought twice
            restore(db)
            fetched = weeks_fetched(db, season)
        except Exception as e:  # noqa: BLE001
            log.warning("personnel: restore failed: %r", e)
    w = due_week(games, fetched, now)
    if w is None:
        return {"week": None, "calls_this_month": cfbd.calls_used(cdb)}
    final = now >= _week_spans(games)[w][1] + FINAL_AFTER_S
    own = client is None
    client = client or _client()
    try:
        n = _fetch_week(cdb, db, client, season, w, now, not final, push)
    finally:
        if own:
            client.close()
    return {"week": w, "final": final, "rows": n, "calls_this_month": cfbd.calls_used(cdb)}


def mirror_week(season: int, week: int, rows: list[dict], now: float, pusher=None) -> None:
    if pusher is None:
        import market_snapshot as M
        pusher = lambda r: M.supabase_push("sheet", r)  # noqa: E731
    pusher([{"kind": "player_game", "key": f"{season}-w{week:02d}",
             "body": {"season": season, "week": week, "fetched_at": now, "rows": rows}}])


def restore(db, puller=None) -> int:
    """Weeks from the Supabase copy that the local cache lacks."""
    if puller is None:
        import factstore
        puller = factstore.supabase_pull
    have = {(s, w) for s, w in db.execute("select season, week from player_games")}
    n = 0
    for r in puller("sheet") or []:
        if r.get("kind") != "player_game":
            continue
        b = r.get("body") or {}
        if (b.get("season"), b.get("week")) in have or b.get("season") is None:
            continue
        db.execute("insert or replace into player_games values(?,?,?,?,?)",
                   (b["season"], b["week"], b.get("fetched_at") or 0, 0, json.dumps(b.get("rows") or [])))
        n += 1
    db.commit()
    return n


# =============================================================================================================
# qb_start ledger
# =============================================================================================================

def passer_rating(cmp: int, att: int, yds: int, td: int, ints: int) -> float | None:
    """NCAA passing efficiency."""
    if not att:
        return None
    return (8.4 * yds + 330 * td + 100 * cmp - 200 * ints) / att


def _wl(w, l_, p=0) -> str:
    return f"{w}-{l_}" + (f"-{p}" if p else "")


def qb_ledger(rows: list[dict], results: dict[str, list[dict]], positions: dict | None = None) -> dict[str, list[dict]]:
    """{team: [QB entries, most starts first]}. Starter = most attempts in the game (ties: more yards).
    results: the fact store's team results (game_id, date, opp, pts, opp_pts, spread, cover)."""
    by_game: dict[tuple, list] = {}
    for r in rows:
        if r.get("att"):
            by_game.setdefault((r["team"], r["game_id"]), []).append(r)
    starts: dict[tuple, list] = {}
    for (team, gid), rs in by_game.items():
        s = max(rs, key=lambda r: (r["att"], r["yds"]))
        starts.setdefault((team, s["id"]), []).append(gid)
    res = {t: {str(x["game_id"]): x for x in rs or []} for t, rs in (results or {}).items()}
    agg: dict[tuple, dict] = {}
    for r in rows:
        if not (r.get("att") or r.get("car")):
            continue
        a = agg.setdefault((r["team"], r["id"]), {"name": r["name"], "id": r["id"], "team": r["team"],
                                                  "line": {k: 0 for k in ("cmp", "att", "yds", "td", "int", "car",
                                                                          "rush_yds", "rush_td", "games")},
                                                  "_games": set()})
        for k in ("cmp", "att", "yds", "td", "int", "car", "rush_yds", "rush_td"):
            a["line"][k] += r.get(k) or 0
        a["_games"].add(r["game_id"])
    out: dict[str, list] = {}
    for (team, pid), a in agg.items():
        gids = starts.get((team, pid)) or []
        pos = (positions or {}).get((team, _norm(a["name"])))
        if not gids and (a["line"]["att"] < 2 or (pos and pos != "QB")):
            continue
        a["line"]["games"] = len(a.pop("_games"))
        sg = []
        for gid in gids:
            x = res.get(team, {}).get(str(gid)) or {}
            sg.append({"game_id": str(gid), "date": x.get("date") or "", "opp": x.get("opp"),
                       "won": (x["pts"] > x["opp_pts"]) if x.get("pts") is not None and x.get("opp_pts") is not None
                       else None, "cover": x.get("cover")})
        sg.sort(key=lambda g: g["date"])
        w = sum(1 for g in sg if g["won"] is True)
        l_ = sum(1 for g in sg if g["won"] is False)
        aw = sum(1 for g in sg if g["cover"] is not None and g["cover"] > 0)
        al = sum(1 for g in sg if g["cover"] is not None and g["cover"] < 0)
        ap = sum(1 for g in sg if g["cover"] == 0)
        ln = a["line"]
        a.update(starts=len(sg), su=_wl(w, l_), ats=_wl(aw, al, ap), first_start=sg[0]["date"] if sg else None,
                 last_start=sg[-1]["date"] if sg else None, start_games=sg,
                 rating=passer_rating(ln["cmp"], ln["att"], ln["yds"], ln["td"], ln["int"]))
        out.setdefault(team, []).append(a)
    for team in out:
        out[team].sort(key=lambda q: (-q["starts"], -q["line"]["att"]))
    return out


def starter(ledger: list[dict]) -> dict | None:
    """The QB who started the team's latest game."""
    ss = [q for q in ledger or [] if q.get("last_start")]
    return max(ss, key=lambda q: q["last_start"]) if ss else None


# =============================================================================================================
# availability normalization
# =============================================================================================================

_ST_RX = [
    ("TBD", r"\bhas(?:n'?t| not) (?:yet )?(?:named|announced|decided on|decided|picked)(?: on)? (?:a |his |the |a new )?"
            r"(?:starter|starting)|\bno starter (?:has been )?named|\btbd\b|\bto be determined\b|"
            r"\bstatus (?:is |remains )?(?:unclear|unknown|undetermined)|\bdid(?:n'?t| not) say (?:if|whether)\b|"
            r"\bundecided\b|\bhasn'?t been ruled (?:in|out)\b|\bno (?:official )?update\b"),
    ("suspended", r"\bsuspen(?:d|ded|sion)\b|\bineligible\b"),
    ("portal", r"\b(?:enter(?:s|ed|ing)?|in|into) the (?:transfer )?portal\b|\bportal entry\b"),
    ("opt-out", r"\bopt(?:s|ed|ing)?[- ]out\b"),
    ("season", r"\bfor (?:the )?(?:rest of the )?(?:regular )?(?:season|year)\b|\bseason[- ]ending\b|"
               r"\b(?:rest|remainder) of (?:the )?(?:regular )?(?:season|year)\b"),
    ("doubtful", r"\bdoubtful\b|\bunlikely to (?:play|go)\b|\blikely (?:won'?t|will not|not) (?:play|go)\b|"
                 r"\bdon'?t think (?:it|he) will be (?:this week|ready)"),
    ("out", r"\bruled out\b|\b(?:won'?t|will not|isn'?t going to) play\b|\bis out\b|\bout (?:vs\.?|against|for) |"
            r"\bwill miss\b|\b(?:expected|set|going) to miss\b|\bnot expected to play\b|\bsidelined\b"),
    ("benched", r"\bbench(?:ed|ing)\b|(?<!decision )\b(?:to|will) sit\b|\bdemoted\b|\blost (?:his|the) (?:starting )?job\b"),
    ("questionable", r"\bquestionable\b|\bgame[- ]time decision\b|\bday[- ]to[- ]day\b|\blimited in practice\b|"
                     r"\buncertain\b|\bconcussion protocol\b|\bworking (?:toward|towards|to get) (?:being )?(?:ready|back|"
                     r"healthy|playing)\b|\bhop(?:es|eful|ing) to (?:play|return|go|be ready)\b|\btrying to (?:play|"
                     r"return|get back)\b"),
    ("probable", r"\bprobable\b|\bexpected to play\b|\bshould play\b|\blikely to play\b"),
    ("available", r"\bcleared(?: to (?:play|return))?\b|\bgood to go\b|\bwill (?:play|start)\b|\bexpected to start\b|"
                  r"\breturn(?:s|ed)? to practice\b|\bback at practice\b|\bis available\b|\bfull go\b"),
]
_ST = [(s, re.compile(rx, re.I)) for s, rx in _ST_RX]
_LABEL = {"out": "out", "season": "season", "doubtful": "doubtful", "questionable": "questionable",
          "probable": "probable", "returning": "available", "available": "available", "unknown": "TBD", "tbd": "TBD",
          "suspended": "suspended", "game-time decision": "questionable", "benched": "benched", "portal": "portal",
          "opt-out": "opt-out"}
_PRIORITY = {"slate_intel": 5, "dossier": 4, "news_confirmed": 3, "news": 2, "espn_injury": 1, "espn_pbp": 1,
             "espn_roster": 0}


def statuses_in(text: str) -> list[str]:
    """Distinct statuses a clause states, in precedence order (a match inside a stronger one doesn't count)."""
    spans, out = [], []
    for s, rx in _ST:
        for m in rx.finditer(text or ""):
            if any(m.start() < b and a < m.end() for a, b in spans):
                continue
            spans.append(m.span())
            if s not in out:
                out.append(s)
    return out


def status_of(text: str) -> str | None:
    s = statuses_in(text)
    return s[0] if s else None


_SPLIT = re.compile(r";\s+|(?<!\b[A-Z])(?<!\bJr)(?<!\bSr)(?<!\bSt)(?<!\bvs)(?<!\bNo)(?<!\bMr)\.\s+(?=[A-Z\"'])"
                    r"|,\s+(?:and|but|while)\s+|\s+but\s+")


def clauses(text: str) -> list[str]:
    return [c.strip() for c in _SPLIT.split(text or "") if c and c.strip()]


def _word_at(hay: str, needle: str) -> bool:
    """needle in hay as whole words (no letter/digit on either side)."""
    i = hay.find(needle)
    while i >= 0:
        a, b = i - 1, i + len(needle)
        if (a < 0 or not hay[a].isalnum()) and (b >= len(hay) or not (hay[b].isalnum() or hay[b] in "'")):
            return True
        i = hay.find(needle, i + 1)
    return False


def _mentions(clause: str, name: str) -> bool:
    if not name:
        return False
    if _word_at(clause.lower(), name.lower()):
        return True
    last = re.sub(r",? (Jr\.?|Sr\.?|II|III|IV)$", "", name).split()[-1]
    return len(last) > 3 and _word_at(clause, last)


_POS_TOK = r"QB|RB|FB|WR|TE|OL|OT|LT|RT|LG|RG|OG|IOL|DL|DT|DE|EDGE|NT|LB|ILB|OLB|MLB|CB|S|FS|SS|DB|K|P|PK|LS|C|G"
_POS_WORDS = [("quarterback", "QB"), ("running back", "RB"), ("tailback", "RB"), ("wide receiver", "WR"),
              ("receiver", "WR"), ("tight end", "TE"), ("left tackle", "LT"), ("offensive lineman", "OL"),
              ("lineman", "OL"), ("guard", "OL"), ("center", "OL"), ("tackle", "OL"), ("defensive end", "EDGE"),
              ("edge rusher", "EDGE"), ("defensive tackle", "DT"), ("linebacker", "LB"), ("cornerback", "CB"),
              ("corner", "CB"), ("safety", "S"), ("kicker", "K"), ("punter", "P")]
_ADJ = r"(?:(?:freshman|sophomore|junior|senior|redshirt|true|starting|star|top|backup|veteran|big-play|leading|" \
       r"standout|first-team|all-[a-z]+)\s+)*"
_ROLE = [("WR1", r"\b(?:top|leading|No\. 1|best) (?:wide )?receiver|\bWR1\b|\btop target"),
         ("RB1", r"\b(?:top|leading|lead|starting|No\. 1) (?:running )?back|\bleading rusher|\bRB1\b"),
         ("CB1", r"\b(?:top|best|No\. 1|lockdown) corner(?:back)?|\bCB1\b")]


_POS_WORD_MAP = dict(_POS_WORDS)
_POS_END = re.compile(rf"\b({_POS_TOK})(?:/[A-Z]{{1,3}})?\s+{_ADJ}$")
_POSW_END = re.compile(rf"\b({'|'.join(w for w, _ in _POS_WORDS)})\s+{_ADJ}$", re.I)
_ROLE_END = re.compile("(?:" + "|".join(f"(?P<{r}>{rx})" for r, rx in _ROLE) + r")[^.;]{0,25}$", re.I)


def text_position(text: str, name: str) -> tuple[str, str | None]:
    """('LG', None) / ('WR', 'WR1') from the words right before a name ('Tennessee LG Nic Moore')."""
    i = (text or "").find(name)
    if i < 0:
        return "", None
    pre = text[max(0, i - 90):i]
    m = _POS_END.search(pre)
    pos = m.group(1) if m else ""
    if not pos:
        m = _POSW_END.search(pre)
        pos = _POS_WORD_MAP.get(m.group(1).lower(), "") if m else ""
    m = _ROLE_END.search(pre)
    role = next((r for r, _ in _ROLE if m and m.group(r)), None)
    return pos, role


def _ts(s) -> float:
    if not s:
        return 0.0
    s = str(s)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return dt.datetime.fromisoformat(s + "T12:00:00").replace(tzinfo=CT).timestamp()
    try:
        d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    return (d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)).timestamp()


def report_when(reported_at, now: float) -> tuple[str, bool]:
    """('Monday', False) for a fresh report; ('last report Mon 11/2', True) past 7 days."""
    t = _ts(reported_at)
    if not t:
        return ("undated", True)
    d, today = dt.datetime.fromtimestamp(t, CT), dt.datetime.fromtimestamp(now, CT).date()
    days = (today - d.date()).days
    if days > 7:
        return (f"last report {d.strftime('%a')} {d.month}/{d.day}", True)
    if days <= 0:
        return ("today", False)
    if days == 1:
        return ("yesterday", False)
    if days == 7:
        return (f"{d.strftime('%a')} {d.month}/{d.day}", False)
    return (d.strftime("%A"), False)


_PLAYING = re.compile(r"\b(remains|stays|is still|will remain) (the |his team's )?(starting )?(QB|quarterback|starter)\b|"
                      r"\bwill start\b|\bis starting\b|\bexpected to (play|start)\b|\bcleared\b|\bgood to go\b|"
                      r"\bfull participant\b", re.I)


def _note_rows(note: dict, team: str | None, game_id, src: str) -> list[dict]:
    """One sourced note (slate_intel / dossier shape) -> a row per player it gives a status."""
    text = re.sub(r"\s+", " ", note.get("text") or "").strip()
    players = [p for p in note.get("players") or [] if p]
    label = _LABEL.get(str(note.get("status") or "").lower()) if note.get("status") else None
    cl = clauses(text)
    out = []
    for i, name in enumerate(players):
        st = None
        for c in cl:
            if not _mentions(c, name):
                continue
            ss = statuses_in(c)
            named = [p for p in players if _mentions(c, p)]
            if ss and (len(named) == 1 or len(ss) == 1):
                st = ss[0]
                break
        own = [c for c in cl if _mentions(c, name)]
        theirs = {x for c in cl for p in players if p != name and _mentions(c, p) and not _mentions(c, name)
                  for x in statuses_in(c)}
        if st is None and i == 0 and (any(_PLAYING.search(c) for c in own) or (len(players) > 1 and label in theirs)):
            continue  # 'Leavitt remains the QB; DT Anderson will miss ...' (label out): the out is Anderson's
        if st is None and i == 0:
            st = label
            if label in ("probable", "available"):  # a hedge in the text beats a sure-sounding label
                hedge = [x for c in cl if not any(_mentions(c, p) for p in players if p != name)
                         for x in statuses_in(c) if x in ("questionable", "TBD")]
                st = hedge[0] if hedge else st
        if not st or not team:
            continue
        pos, role = text_position(text, name)
        out.append(_row(name, team, st, text, note.get("source_name") or src, note.get("source_url") or "",
                        note.get("date") or "", note.get("confidence") or "medium", src, pos=pos, role=role,
                        game_id=str(game_id) if game_id else None))
    return out


def _news_rows(it: dict, name_team: dict) -> list[dict]:
    text = re.sub(r"\s+", " ", f"{it.get('title') or ''}. {it.get('summary') or ''}").strip(" .")
    cl = clauses(text)
    teams = it.get("teams") or []
    out = []
    for name in it.get("players") or []:
        team = name_team.get(_norm(name)) or (teams[0] if len(teams) == 1 else None)
        if not team:
            continue
        for c in cl:
            if not _mentions(c, name):
                continue
            ss = statuses_in(c)
            named = [p for p in it.get("players") or [] if _mentions(c, p)]
            if ss and (len(named) == 1 or len(ss) == 1):
                pos, role = text_position(text, name)
                conf = it.get("label") == "confirmed"
                out.append(_row(name, team, ss[0], c.rstrip(".") + ".", it.get("source") or "news",
                                it.get("link") or "", it.get("date") or it.get("first_seen") or "",
                                "medium" if conf else "low", "news_confirmed" if conf else "news", pos=pos, role=role))
                break
    return out


def _pbp_rows(live: dict, now: float) -> list[dict]:
    out = []
    for g in (live or {}).get("games", {}).values():
        if g.get("state") != "post" or not (now - 10 * 86400 <= _ts(g.get("start")) <= now):
            continue
        matchup = f"{(g.get('away') or {}).get('name')} at {(g.get('home') or {}).get('name')}"
        for x in g.get("injuries") or []:
            if x.get("returned") or not x.get("team"):
                continue
            name = x.get("full_name") or x.get("player")
            per = x.get("period")
            when = (f"Q{per}" if isinstance(per, int) and per <= 4 else "OT") + (f" {x['clock']}" if x.get("clock") else "")
            out.append(_row(name, x["team"], "TBD", f"{name} left {matchup} injured ({when}) and didn't return",
                            "ESPN play-by-play", "", g.get("start") or "", "medium", "espn_pbp",
                            pos="QB" if x.get("qb") else ""))
    return out


def normalize(intel: dict | None = None, dossiers: list[dict] | None = None, news_items: list[dict] | None = None,
              roster_rows: list[dict] | None = None, live: dict | None = None, now: float | None = None,
              name_team: dict | None = None) -> list[dict]:
    """The one availability list: the latest report per (team, player). ESPN roster listings (no date) only
    fill in players nobody reported on."""
    now = now or time.time()
    rows = []
    for gid, g in ((intel or {}).get("games") or {}).items():
        for f in g.get("facts") or []:
            team = g.get(f.get("team")) if f.get("team") in ("home", "away") else None
            rows += _note_rows(f, team, gid, "slate_intel")
    for d in dossiers or []:
        facts = list(d.get("facts") or []) + list(((d.get("opus") or {}).get("facts")) or [])
        for f in facts:
            team = d.get(f.get("team")) if f.get("team") in ("home", "away") else None
            rows += _note_rows(f, team, d.get("game_id"), "dossier")
    rows += [r for r in roster_rows or [] if r.get("src") == "espn_injury"]
    rows += _pbp_rows(live or {}, now)
    nt = {_norm(k): v for k, v in (name_team or {}).items()}
    for r in rows:
        nt.setdefault(_norm(r["player"]), r["team"])
    for it in news_items or []:
        if it.get("superseded_by"):
            continue
        rows += _news_rows(it, nt)
    best: dict[tuple, dict] = {}
    for r in rows:
        if r["src"] == "espn_roster":
            continue
        k = (r["team"], _norm(r["player"]))
        cur = best.get(k)
        if cur is None or (_ts(r["reported_at"]), _PRIORITY.get(r["src"], 0)) > \
                (_ts(cur["reported_at"]), _PRIORITY.get(cur["src"], 0)):
            best[k] = r
    for r in roster_rows or []:
        if r.get("src") == "espn_roster":
            best.setdefault((r["team"], _norm(r["player"])), r)
    return sorted(best.values(), key=lambda r: (r["team"] or "", -_ts(r["reported_at"])))


# =============================================================================================================
# position value table
# =============================================================================================================

ESPN_POSITIONS = ("QB", "RB", "FB", "WR", "TE", "OL", "OT", "OG", "G", "C", "T", "LT", "RT", "LG", "RG", "IOL", "DL",
                  "DE", "DT", "NT", "EDGE", "LB", "ILB", "OLB", "MLB", "CB", "S", "FS", "SS", "DB", "K", "PK", "P",
                  "LS", "ATH")
_VALUES: dict = {}


def position_values() -> dict:
    if not _VALUES:
        _VALUES.update(json.loads(VALUES_PATH.read_text()))
    return _VALUES


def validate_position_values(d: dict) -> list[str]:
    errs = []
    qb = d.get("qb") or {}
    tiers = qb.get("tiers") or []
    if not tiers:
        return ["qb.tiers missing"]
    gaps, pts = [t.get("min_gap") for t in tiers], [t.get("pts") for t in tiers]
    if any(not isinstance(x, (int, float)) for x in gaps + pts):
        errs.append("qb tiers need numeric min_gap and pts")
        return errs
    if gaps != sorted(gaps, reverse=True) or len(set(gaps)) != len(gaps) or gaps[-1] != 0:
        errs.append("qb tiers must run from the biggest gap down to 0")
    if pts != sorted(pts, reverse=True) or len(set(pts)) != len(pts):
        errs.append("qb tier points must fall as the gap shrinks")
    mx = qb.get("max") or 0
    if any(not (0 < p <= mx) for p in pts):
        errs.append("qb tier points must be in (0, max]")
    ub = qb.get("unknown_backup")
    if not isinstance(ub, (int, float)) or not (min(pts) <= ub <= mx):
        errs.append("qb.unknown_backup must sit inside the tier range")
    if not isinstance(qb.get("min_backup_att"), int) or qb["min_backup_att"] < 1:
        errs.append("qb.min_backup_att must be a positive int")
    pos = d.get("positions") or {}
    for k, v in pos.items():
        if not isinstance(v, (int, float)) or not (0 < v < min(pts)):
            errs.append(f"positions.{k}={v}: must be > 0 and below the smallest QB tier ({min(pts)})")
    al = d.get("aliases") or {}
    for k, v in al.items():
        if v != "QB" and v not in pos:
            errs.append(f"aliases.{k} -> {v}: not a position")
    for p in ESPN_POSITIONS:
        if p not in pos and p not in al:
            errs.append(f"ESPN position {p} has no value")
    sw = d.get("status_weight") or {}
    for k, v in sw.items():
        if k not in STATUSES or not isinstance(v, (int, float)) or not (0 <= v <= 1):
            errs.append(f"status_weight.{k}={v}")
    if not isinstance(d.get("line_mover"), (int, float)) or d["line_mover"] <= 0:
        errs.append("line_mover must be > 0")
    return errs


def player_value(pos: str, role: str | None = None) -> float:
    d = position_values()
    if role and role in d["positions"]:
        return d["positions"][role]
    p = (pos or "").upper()
    p = p if p in d["positions"] else d["aliases"].get(p, "ATH")
    return d["positions"].get(p, d["positions"]["ATH"])


def backup_for(room: list[dict], player: str, exclude: set | frozenset = frozenset()) -> dict | None:
    """The best other QB in a room (most starts, then most attempts), skipping `exclude` (normalized names)."""
    others = [q for q in room or [] if _norm(q["name"]) != _norm(player) and _norm(q["name"]) not in exclude]
    return max(others, key=lambda q: (q.get("starts") or 0, (q.get("line") or {}).get("att") or 0)) if others else None


def row_value(row: dict, room: list[dict]) -> float:
    """Points one availability row is worth at full absence (before status_weight): a QB who is his team's main
    starter by the rating gap to the next man, any other QB a little, everyone else by position."""
    if (row.get("pos") or "").upper() != "QB":
        return player_value(row.get("pos") or "", row.get("role"))
    me = next((q for q in room or [] if _norm(q["name"]) == _norm(row["player"])), None)
    top = max(room, key=lambda q: (q.get("starts") or 0)) if room else None
    if not me or not top or (me is not top and not me.get("streak")) or not me.get("starts"):
        return player_value("ATH")
    b = backup_for(room, me["name"])
    return qb_value(me.get("rating"), (b or {}).get("rating"), ((b or {}).get("line") or {}).get("att") or 0)


def qb_value(starter_rating: float | None, backup_rating: float | None, backup_att: int) -> float:
    """Points a starting QB is worth over his backup, by the passer rating gap."""
    q = position_values()["qb"]
    if starter_rating is None or backup_rating is None or (backup_att or 0) < q["min_backup_att"]:
        return q["unknown_backup"]
    gap = starter_rating - backup_rating
    for t in q["tiers"]:
        if gap >= t["min_gap"]:
            return t["pts"]
    return q["tiers"][-1]["pts"]


# =============================================================================================================
# CLI
# =============================================================================================================

def status() -> dict:
    db = connect()
    c = cfbd.connect()
    return {"rosters": db.execute("select count(*) from rosters where json is not null").fetchone()[0],
            "weeks": {f"{s}-{w}": dt.datetime.fromtimestamp(f).isoformat(timespec="minutes")
                      for s, w, f in db.execute("select season, week, fetched_at from player_games")},
            "cfbd_calls_this_month": cfbd.calls_used(c)}


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    cmd = argv[0] if argv else "status"
    if cmd == "backfill" and len(argv) >= 3:
        weeks = list(range(int(argv[1]), int(argv[2]) + 1))
        try:
            print(json.dumps(backfill(weeks, push="default")))
        except cfbd.BudgetExceeded as e:
            print(json.dumps({"ok": False, "skipped": str(e)}))
            return 1
    elif cmd == "rosters":
        n = int(argv[1]) if len(argv) > 1 else ROSTER_PER_TICK
        c = sqlite3.connect(cfbd.DB_PATH)
        row = c.execute("select json from datasets where name='games'").fetchone()
        db = connect()
        print(json.dumps(roster_tick(db, fbs_teams(json.loads(row[0]) if row else []), per_tick=n)))
    else:
        print(json.dumps(status(), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
