"""
CollegeFootballData (CFBD) stats for Picky: season player stats, team season
stats, advanced defense/offense, SP+, records, ATS and betting lines.

Bulk-pulled on a schedule (LaunchAgent com.pickem.cfbd-refresh), never per
chat message. Every pull is one all-teams call, so a full refresh is
len(DATASETS) calls no matter how many pool games there are. Results live in
server/cache/cfbd.sqlite (gitignored); chat.py only ever reads that file.

Budget: the free tier is 1,000 calls/month, shared with CollegeBasketballData.
Two guards, both checked before a refresh spends anything:
  - our own count for the month (calls table) may not pass MONTHLY_CAP;
  - CFBD's own count (/info, which is free) must leave SHARED_RESERVE calls
    for everything else on the key.
A refresh that can't afford every dataset spends nothing and keeps the old
cache.

The key is read from the login keychain at runtime (account "drake", service
CFB_PICKEM__CFBD_API_KEY). Never log it, never commit it.

Run:  ./.venv/bin/python3 -I cfbd.py refresh   (or: status)
"""

from __future__ import annotations

import collections
import datetime as dt
import difflib
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import time
import unicodedata
from pathlib import Path

log = logging.getLogger("picky.cfbd")

API = "https://api.collegefootballdata.com"
KEYCHAIN_SERVICE = "CFB_PICKEM__CFBD_API_KEY"
DB_PATH = Path(os.environ.get("PICKY_CFBD_DB", Path(__file__).with_name("cache") / "cfbd.sqlite"))

MONTHLY_CAP = int(os.environ.get("PICKY_CFBD_MONTHLY_CAP", "300"))
SHARED_RESERVE = 500  # always leave this many of the shared 1,000 untouched

# name -> (path, params). {year} is filled in at refresh time.
DATASETS = {
    "player_stats": ("/stats/player/season", {"year": "{year}"}),
    "team_stats": ("/stats/season", {"year": "{year}"}),
    "advanced": ("/stats/season/advanced", {"year": "{year}", "excludeGarbageTime": "true"}),
    "sp": ("/ratings/sp", {"year": "{year}"}),
    "records": ("/records", {"year": "{year}"}),
    "ats": ("/teams/ats", {"year": "{year}"}),
    "lines": ("/lines", {"year": "{year}"}),
}


def season_year(today: dt.date | None = None) -> int:
    d = today or dt.date.today()
    return d.year if d.month >= 7 else d.year - 1


def _month(now: dt.datetime | None = None) -> str:
    return (now or dt.datetime.now(dt.timezone.utc)).strftime("%Y-%m")


# -----------------------------------------------------------------------------
# cache + call budget
# -----------------------------------------------------------------------------

def connect(path: Path | str | None = None) -> sqlite3.Connection:
    p = Path(path or DB_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(p)
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS datasets (
            name TEXT PRIMARY KEY, year INTEGER, fetched_at REAL, json TEXT);
        CREATE TABLE IF NOT EXISTS calls (month TEXT PRIMARY KEY, count INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS call_log (
            ts REAL, endpoint TEXT, status INTEGER, remaining INTEGER);
        """
    )
    return db


def calls_used(db: sqlite3.Connection, month: str | None = None) -> int:
    row = db.execute("SELECT count FROM calls WHERE month=?", (month or _month(),)).fetchone()
    return row[0] if row else 0


def _record_call(db, endpoint: str, status: int, remaining: int | None) -> None:
    m = _month()
    db.execute(
        "INSERT INTO calls(month, count) VALUES(?, 1) "
        "ON CONFLICT(month) DO UPDATE SET count = count + 1",
        (m,),
    )
    db.execute("INSERT INTO call_log VALUES(?,?,?,?)", (time.time(), endpoint, status, remaining))
    db.commit()


class BudgetExceeded(RuntimeError):
    pass


def check_budget(db, needed: int, shared_used: int | None = None) -> None:
    """Raise BudgetExceeded unless `needed` more calls fit both caps."""
    mine = calls_used(db)
    if mine + needed > MONTHLY_CAP:
        raise BudgetExceeded(f"local cap: {mine} used + {needed} > {MONTHLY_CAP}/month")
    if shared_used is not None and shared_used + needed > 1000 - SHARED_RESERVE:
        raise BudgetExceeded(
            f"shared pool: {shared_used} used on the key + {needed} would leave "
            f"under {SHARED_RESERVE} for CollegeBasketballData"
        )


def _api_key() -> str:
    return subprocess.run(
        ["security", "find-generic-password", "-a", "drake", "-s", KEYCHAIN_SERVICE, "-w"],
        capture_output=True, text=True, check=True, timeout=10,
    ).stdout.strip()


def refresh(db=None, client=None, year: int | None = None, only: list[str] | None = None) -> dict:
    """Pull every dataset (or `only` these). Each one is saved as it lands; a
    failed one keeps its previous copy and is not retried (retries cost calls).
    Returns a small status dict."""
    import httpx

    db = db or connect()
    year = year or season_year()
    own_client = client is None
    if own_client:
        client = httpx.Client(
            base_url=API, timeout=60.0,
            headers={"Authorization": f"Bearer {_api_key()}", "Accept": "application/json"},
        )
    try:
        names = [n for n in DATASETS if not only or n in only]
        info = client.get("/info").json() or {}  # free: doesn't count against the tier
        check_budget(db, len(names), info.get("usedCalls"))
        rows, failed = {}, {}
        for name in names:
            path, params = DATASETS[name]
            # re-check per call so nothing can run past the cap mid-refresh
            check_budget(db, 1)
            q = {k: v.format(year=year) for k, v in params.items()}
            try:
                r = client.get(path, params=q)
            except Exception as e:  # noqa: BLE001 - network: count it, move on
                _record_call(db, path, 0, None)
                failed[name] = type(e).__name__
                continue
            rem = r.headers.get("x-calllimit-remaining")
            _record_call(db, path, r.status_code, int(rem) if rem and rem.isdigit() else None)
            if r.status_code != 200:
                failed[name] = r.status_code
                continue
            data = r.json()
            db.execute(
                "INSERT OR REPLACE INTO datasets VALUES(?,?,?,?)",
                (name, year, time.time(), json.dumps(data, separators=(",", ":"))),
            )
            db.commit()
            rows[name] = len(data)
        return {"ok": not failed, "year": year, "calls_this_month": calls_used(db),
                "rows": rows, "failed": failed}
    finally:
        if own_client:
            client.close()


# -----------------------------------------------------------------------------
# name matching
# -----------------------------------------------------------------------------

def norm(s: str | None) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    s = re.sub(r"['’.`]", "", s)
    s = re.sub(r"[^a-z0-9()& ]+", " ", s)
    s = re.sub(r"\bst\b", "state", s)  # "Oregon St." -> "oregon state"
    return re.sub(r"\s+", " ", s).strip()


# pool/ESPN spelling -> CFBD spelling (normalized both sides)
ALIASES = {
    "app state": "appalachian state",
    "miami fl": "miami",
    "miami (fl)": "miami",
    "miami oh": "miami (oh)",
    "ul monroe": "louisiana monroe",
    "ulm": "louisiana monroe",
    "southern miss": "southern mississippi",
    "uconn": "connecticut",
    "umass": "massachusetts",
    "ole miss": "ole miss",
    "pitt": "pittsburgh",
    "nc state": "nc state",
    "fiu": "florida international",
    "hawaii": "hawaii",
}


# -----------------------------------------------------------------------------
# index (built from the cache, rebuilt when a refresh lands)
# -----------------------------------------------------------------------------

_index: dict | None = None
_index_stamp: float | None = None


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return v
    return int(f) if f == int(f) else round(f, 1)


def build_index(data: dict) -> dict:
    """Pivot raw CFBD payloads into lookups keyed by normalized team name."""
    teams: dict[str, dict] = {}

    def team(name):
        key = norm(name)
        return teams.setdefault(key, {"name": name})

    for r in data.get("records") or []:
        t = team(r["team"])
        t["conference"] = r.get("conference")
        tot = r.get("total") or {}
        conf = r.get("conferenceGames") or {}
        t["record"] = f"{tot.get('wins', 0)}-{tot.get('losses', 0)}"
        if conf.get("games"):
            t["conf_record"] = f"{conf.get('wins', 0)}-{conf.get('losses', 0)}"
    for r in data.get("ats") or []:
        t = team(r["team"])
        t["ats"] = (r.get("atsWins"), r.get("atsLosses"), r.get("atsPushes"), r.get("avgCoverMargin"))
    for r in data.get("sp") or []:
        if not r.get("team") or r.get("team") == "nationalAverages":
            continue
        t = team(r["team"])
        t["sp"] = {
            "rating": r.get("rating"), "rank": r.get("ranking"),
            "off": (r.get("offense") or {}).get("rating"), "off_rank": (r.get("offense") or {}).get("ranking"),
            "def": (r.get("defense") or {}).get("rating"), "def_rank": (r.get("defense") or {}).get("ranking"),
        }
    for r in data.get("team_stats") or []:
        team(r["team"]).setdefault("stats", {})[r["statName"]] = r.get("statValue")
    for r in data.get("advanced") or []:
        t = team(r["team"])
        for side in ("offense", "defense"):
            o = r.get(side) or {}
            t.setdefault("adv", {})[side] = {
                "ppa": o.get("ppa"), "success": o.get("successRate"),
                "explosive": o.get("explosiveness"),
            }
    # scores + this week's market from lines (one row per game, all season)
    for g in data.get("lines") or []:
        for side, opp in (("home", "away"), ("away", "home")):
            t = team(g[f"{side}Team"])
            if g.get("homeScore") is not None and g.get("awayScore") is not None:
                t.setdefault("scores", []).append((g[f"{side}Score"], g[f"{opp}Score"]))
            t.setdefault("games", []).append(g)
    # rank everyone on a few defensive/offensive per-game numbers so the model
    # gets "allows 18.2 ppg (#12)" instead of having to compare raw floats
    for t in teams.values():
        sc = t.get("scores") or []
        if sc:
            t["ppg"] = round(sum(a for a, _ in sc) / len(sc), 1)
            t["opp_ppg"] = round(sum(b for _, b in sc) / len(sc), 1)
        st = t.get("stats") or {}
        gms = st.get("games") or len(sc) or None
        if gms:
            for k, src in (("ypg", "totalYards"), ("opp_ypg", "totalYardsOpponent"),
                           ("pass_ypg", "netPassingYards"), ("rush_ypg", "rushingYards")):
                if st.get(src) is not None:
                    t[k] = round(float(st[src]) / gms, 1)
    _rank(teams, "opp_ppg", reverse=False)
    _rank(teams, "opp_ypg", reverse=False)
    _rank(teams, "ppg", reverse=True)
    _rank(teams, "ypg", reverse=True)

    players: dict[str, dict] = {}
    for r in data.get("player_stats") or []:
        p = players.setdefault(str(r.get("playerId")), {
            "name": r.get("player"), "pos": r.get("position"), "team": r.get("team"),
            "key": norm(r.get("player")), "stats": {},
        })
        p["stats"].setdefault(r.get("category"), {})[r.get("statType")] = _num(r.get("stat"))
    by_team: dict[str, list] = {}
    for p in players.values():
        by_team.setdefault(norm(p["team"]), []).append(p)
    return {"teams": teams, "players": list(players.values()), "by_team": by_team}


def _rank(teams: dict, field: str, reverse: bool) -> None:
    # only FBS-ish teams that have SP+ get ranked, so FCS doesn't skew it
    pool = [t for t in teams.values() if t.get(field) is not None and t.get("sp")]
    pool.sort(key=lambda t: t[field], reverse=reverse)
    for i, t in enumerate(pool, 1):
        t[field + "_rank"] = i


def load_index(db_path: Path | str | None = None) -> dict | None:
    """Index from the cache, rebuilt only when a newer refresh has landed."""
    global _index, _index_stamp
    p = Path(db_path or DB_PATH)
    if not p.exists():
        return None
    db = sqlite3.connect(p)
    try:
        stamp = db.execute("SELECT max(fetched_at) FROM datasets").fetchone()[0]
        if stamp is None:
            return None
        if _index is not None and stamp == _index_stamp:
            return _index
        data = {n: json.loads(j) for n, j in db.execute("SELECT name, json FROM datasets")}
    finally:
        db.close()
    _index, _index_stamp = build_index(data), stamp
    _index["fetched_at"] = stamp
    return _index


def resolve_team(name: str, idx: dict) -> dict | None:
    key = norm(name)
    key = ALIASES.get(key, key)
    t = idx["teams"].get(key)
    if t:
        return t
    m = difflib.get_close_matches(key, list(idx["teams"]), n=1, cutoff=0.88)
    return idx["teams"][m[0]] if m else None


# -----------------------------------------------------------------------------
# lookups for chat.py: short text rows a small model can hold
# -----------------------------------------------------------------------------

def _ord(n) -> str:
    return f"#{n}" if n else ""


def compact_team_summary(team: str, idx: dict | None = None) -> str:
    idx = idx or load_index()
    if not idx:
        return f"{team}: no CFBD stats loaded."
    t = resolve_team(team, idx)
    if not t:
        return f"{team}: not in CFBD stats."
    bits = [f"{t['name']}"]
    rec = t.get("record")
    if rec:
        bits[0] += f" {rec}" + (f" ({t['conf_record']} conf)" if t.get("conf_record") else "")
    ats = t.get("ats")
    if ats and ats[0] is not None:
        bits.append(f"ATS {ats[0]}-{ats[1]}" + (f"-{ats[2]}" if ats[2] else "")
                    + (f", avg cover {ats[3]:+.1f}" if ats[3] is not None else ""))
    sp = t.get("sp")
    if sp and sp.get("rating") is not None:
        bits.append(f"SP+ {sp['rating']:.1f} ({_ord(sp['rank'])}; off {_ord(sp['off_rank'])}, def {_ord(sp['def_rank'])})")
    if t.get("ppg") is not None:
        bits.append(f"scores {t['ppg']} ppg ({_ord(t.get('ppg_rank'))}), allows {t['opp_ppg']} ({_ord(t.get('opp_ppg_rank'))})")
    if t.get("ypg") is not None:
        y = f"{t['ypg']} ypg"
        if t.get("pass_ypg") is not None and t.get("rush_ypg") is not None:
            y += f" ({t['pass_ypg']} pass/{t['rush_ypg']} rush)"
        if t.get("opp_ypg") is not None:
            y += f", allows {t['opp_ypg']} ypg ({_ord(t.get('opp_ypg_rank'))})"
        bits.append(y)
    st = t.get("stats") or {}
    tg, tl = st.get("turnoversOpponent"), st.get("turnovers")
    if tg is not None and tl is not None:
        bits.append(f"TO margin {int(tg) - int(tl):+d}")
    if st.get("sacks") is not None:
        bits.append(f"{int(st['sacks'])} sacks")
    return " | ".join(bits).replace(" ()", "")


STAT_ORDER = {
    "passing": ["COMPLETIONS", "ATT", "PCT", "YDS", "TD", "INT", "YPA"],
    "rushing": ["CAR", "YDS", "YPC", "TD", "LONG"],
    "receiving": ["REC", "YDS", "YPR", "TD", "LONG"],
    "defensive": ["TOT", "SOLO", "TFL", "SACKS", "PD", "QB HUR"],
    "interceptions": ["INT", "YDS", "TD"],
}
LABEL = {"COMPLETIONS": "cmp", "ATT": "att", "PCT": "pct", "YDS": "yds", "TD": "TD",
         "INT": "INT", "YPA": "ypa", "CAR": "car", "YPC": "ypc", "LONG": "long",
         "REC": "rec", "YPR": "ypr", "TOT": "tkl", "SOLO": "solo", "TFL": "TFL",
         "SACKS": "sacks", "PD": "PD", "QB HUR": "hurries"}


def _fmt_cat(cat: str, s: dict) -> str:
    out = []
    for k in STAT_ORDER.get(cat, []):
        if s.get(k) is None:
            continue
        v = s[k]
        if k == "PCT":
            try:  # CFBD's PCT is a fraction; recompute so rounding can't flatten it
                v = round(100 * float(s["COMPLETIONS"]) / float(s["ATT"]), 1)
            except (KeyError, TypeError, ValueError, ZeroDivisionError):
                continue
        if isinstance(v, (int, float)) and k in ("YDS",) and abs(v) >= 1000:
            v = f"{v:,}"
        out.append(f"{v}% comp" if k == "PCT" else f"{v} {LABEL[k]}")
    return f"{cat} " + ", ".join(out) if out else ""


def player_row(p: dict) -> str:
    cats = [c for c in ("passing", "rushing", "receiving", "defensive", "interceptions")
            if p["stats"].get(c)]
    # skip near-empty lines (a QB's 1 catch, a WR's 1 tackle) unless it's all they have
    main = [c for c in cats if _volume(c, p["stats"][c]) >= 5] or cats[:1]
    body = "; ".join(x for x in (_fmt_cat(c, p["stats"][c]) for c in main) if x)
    return f"{p['name']} ({p['pos'] or '?'}, {p['team']}): {body or 'no stats'}"


def _volume(cat: str, s: dict) -> float:
    k = {"passing": "ATT", "rushing": "CAR", "receiving": "REC",
         "defensive": "TOT", "interceptions": "INT"}[cat]
    try:
        return float(s.get(k) or 0) * (5 if cat == "interceptions" else 1)
    except (TypeError, ValueError):
        return 0


STOPWORDS = set(
    "a an the who whos what whats how hows is are was has have had does did do in on at of for "
    "to vs and or best better worst most top this week weeks game games year season team "
    "teams qb qbs rb wr te player players stats stat yards yds rushing passing receiving "
    "defense offense record ats line spread pick picks me my i you he his him".split()
)


def find_players(query: str, teams: list[str] | None = None, limit: int = 5,
                 idx: dict | None = None) -> list[str]:
    """Rows for players named in `query` (a name or a whole question).

    Exact full-name hits first, then close spellings ("Arch Manning" for
    "arch maning"), then unique last names when `teams` narrows the field.
    Returns [] for a name that isn't in the data, so the caller can say so."""
    idx = idx or load_index()
    if not idx:
        return []
    pool = idx["players"]
    if teams:
        keys = {norm(_resolved_name(t, idx)) for t in teams}
        pool = [p for p in pool if norm(p["team"]) in keys]
    words = norm(query).replace("(", " ").replace(")", " ").split()
    grams = {" ".join(words[i:i + n]) for n in (2, 3) for i in range(len(words) - n + 1)}
    hits = [p for p in pool if p["key"] in grams]
    if not hits:
        # close spellings, compared only against names sharing a 2-letter
        # prefix on the first or last word (keeps it ~ms, not ~1 s), and never
        # for phrases made of ordinary words ("the best" vs a "Theo Best")
        by_pre: dict[str, set] = {}
        for p in pool:
            w = p["key"].split()
            if len(w) >= 2:
                by_pre.setdefault("f" + w[0][:2], set()).add(p["key"])
                by_pre.setdefault("l" + w[-1][:2], set()).add(p["key"])
        close = set()
        for g in grams:
            gw = g.split()
            if len(g) < 7 or any(len(x) < 2 or x in STOPWORDS for x in gw):
                continue
            cands = by_pre.get("f" + gw[0][:2], set()) | by_pre.get("l" + gw[-1][:2], set())
            close.update(difflib.get_close_matches(g, cands, n=3, cutoff=0.86))
        hits = [p for p in pool if p["key"] in close]
    if not hits and teams:
        # last name only ("how's Lopez doing"), scoped to the pool teams
        last = {}
        for p in pool:
            last.setdefault(p["key"].split()[-1] if p["key"] else "", []).append(p)
        for w in words:
            if len(w) >= 4 and len(last.get(w, [])) == 1:
                hits.append(last[w][0])
    hits.sort(key=lambda p: -sum(_volume(c, s) for c, s in p["stats"].items() if c in STAT_ORDER))
    return [player_row(p) for p in hits[:limit]]


def _resolved_name(team: str, idx: dict) -> str:
    t = resolve_team(team, idx)
    return t["name"] if t else team


# category -> (stat category, sort key, min volume to qualify)
LEADER_CATS = {
    "passing": ("passing", "YDS"),
    "qb": ("passing", "YDS"),
    "rushing": ("rushing", "YDS"),
    "receiving": ("receiving", "YDS"),
    "tackles": ("defensive", "TOT"),
    "sacks": ("defensive", "SACKS"),
    "defense": ("defensive", "SACKS"),
    "interceptions": ("interceptions", "INT"),
}


# a trick-play throw or one carry doesn't make a leader
LEADER_FLOOR = {"passing": 20, "rushing": 10, "receiving": 5}


def leaders(teams: list[str] | str, category: str, limit: int = 3,
            idx: dict | None = None) -> list[str]:
    """Top players in `category` across `teams` (one team or a game's two)."""
    idx = idx or load_index()
    if not idx:
        return []
    if isinstance(teams, str):
        teams = [teams]
    cat, key = LEADER_CATS.get(category.lower(), (category.lower(), "YDS"))
    names = {norm(_resolved_name(t, idx)) for t in teams}
    floor = LEADER_FLOOR.get(cat, 0)
    pool = [p for n in names for p in idx["by_team"].get(n, [])
            if isinstance((p["stats"].get(cat) or {}).get(key), (int, float))
            and _volume(cat, p["stats"][cat]) >= floor]
    pool.sort(key=lambda p: -p["stats"][cat][key])
    return [f"{p['name']} ({p['pos'] or '?'}, {p['team']}): {_fmt_cat(cat, p['stats'][cat])}"
            for p in pool[:limit]]


def team_leaders(team: str, idx: dict | None = None) -> str | None:
    """'QB Name 2,176 yds, 18 TD, 1 INT; RB Name 760 yds, 10 TD; WR ...':
    season leaders from the same daily pull as everything else."""
    idx = idx or load_index()
    if not idx:
        return None
    out = []
    for pos, cat, keys in (("QB", "passing", ("YDS", "TD", "INT")), ("RB", "rushing", ("YDS", "TD")),
                           ("WR", "receiving", ("YDS", "TD"))):
        top = _top(team, cat, idx)
        if top:
            s = top["stats"][cat]
            vals = [f"{s['YDS']:,} yds" if k == "YDS" else f"{s[k]} {k}" for k in keys
                    if isinstance(s.get(k), (int, float))]
            out.append(f"{top['pos'] or pos} {top['name']} {', '.join(vals)}")
    return "; ".join(out) or None


def _top(team: str, cat: str, idx: dict) -> dict | None:
    n = norm(_resolved_name(team, idx))
    floor = LEADER_FLOOR.get(cat, 0)
    pool = [p for p in idx["by_team"].get(n, [])
            if isinstance((p["stats"].get(cat) or {}).get("YDS"), (int, float))
            and _volume(cat, p["stats"][cat]) >= floor]
    return max(pool, key=lambda p: p["stats"][cat]["YDS"], default=None)


def matchup_edges(away: str, home: str, idx: dict | None = None) -> str | None:
    """'better defense: X (SP+ defense #14 vs #86); better offense: ...',
    worked out here so the model never has to compare ranks itself."""
    idx = idx or load_index()
    a, h = (resolve_team(away, idx), resolve_team(home, idx)) if idx else (None, None)
    if not a or not h:
        return None
    parts = []
    for side, label in (("def", "defense"), ("off", "offense")):
        ra, rh = (a.get("sp") or {}).get(side + "_rank"), (h.get("sp") or {}).get(side + "_rank")
        if ra and rh and ra != rh:
            (bt, rb), (_, rw) = sorted(((a["name"], ra), (h["name"], rh)), key=lambda x: x[1])
            parts.append(f"better {label}: {bt} (SP+ {label} #{rb} vs #{rw})")
    return "; ".join(parts) or None


def game_line(away: str, home: str, idx: dict | None = None) -> str | None:
    """Consensus-ish market for the next/most recent meeting, from CFBD lines."""
    idx = idx or load_index()
    if not idx:
        return None
    h, a = resolve_team(home, idx), resolve_team(away, idx)
    if not h or not a:
        return None
    games = [g for g in h.get("games") or [] if norm(g["awayTeam"]) == norm(a["name"])]
    if not games:
        return None
    g = max(games, key=lambda g: g.get("startDate") or "")
    ln = next((x for x in g.get("lines") or [] if x.get("spread") is not None), None)
    if not ln:
        return None
    s = f"CFBD line ({ln.get('provider')}): {ln.get('formattedSpread')}"
    if ln.get("overUnder") is not None:
        s += f", O/U {ln['overUnder']}"
    return s


# -----------------------------------------------------------------------------
# per-question rows for chat.py: only what the question asks about
# -----------------------------------------------------------------------------

# question words -> leaders() category
_CAT_WORDS = [
    ("passing", r"\b(qbs?|quarterbacks?|passers?|passing|throw(s|ing|n)?|completions?)\b"),
    ("rushing", r"\b(rush(ing|er|ers)?|running ?backs?|rbs?|carries|ground game)\b"),
    ("receiving", r"\b(receiv(ing|ers?)|wide ?outs?|wrs?|catch(es)?|receptions?|targets?)\b"),
    ("sacks", r"\b(sacks?|pass rush(er)?)\b"),
    ("tackles", r"\b(tackl(es|ers?|ing))\b"),
    ("interceptions", r"\b(interceptions?|ints?|picked off|ballhawk)\b"),
]
_LEADER_ASK = re.compile(r"\b(best|top|leading|leads?|leader|most|more|vs|versus|who'?s|who is|who has|whose|better|"
                         r"compare|stud|star|go-?to|number one|no\.? ?1)\b", re.I)
STAT_ASK = re.compile(
    r"\b(yards?|yds|touchdowns?|tds?|stats?|statistics|numbers|passing|rushing|receiving|sacks?|"
    r"tackles?|interceptions?|catches|receptions?|carries|completion|qbs?|quarterbacks?|rbs?|"
    r"running ?backs?|receivers?|wrs?|players?|leaders?|leading)\b", re.I)
_TEAM_ASK = re.compile(r"\b(ats|against the spread|cover(s|ed)? record|sp\+|rating|defen[cs]e|offen[cs]e|"
                       r"stats?|ppg|points (a|per) game|yards (a|per) game|turnovers?|sacks)\b", re.I)
# Capitalized multi-word phrases that look like a person's name.
_TOK = r"[A-Z][a-zA-Z]*(?:['’.][A-Za-z]+)*\.?"
_NAME = re.compile(rf"(?<![\w'’])({_TOK}(?:[- ]{_TOK}){{1,2}})(?![\w'’])")
_LEAD = {"can", "should", "will", "tell", "give", "roast", "show", "picky", "yo", "hey", "ok", "okay",
         "but", "so", "if", "many", "much", "yards", "touchdowns", "sacks", "tackles"}
_ACRONYMS = {"ATS", "SP", "SP+", "QB", "QBS", "RB", "WR", "TE", "FPI", "AP", "CT", "TD", "TDS", "INT", "OT", "O/U"}
_NOT_NAMES = {"pool line", "last week", "this week", "big ten", "big 12", "sun belt", "mountain west",
              "conference usa", "group of five", "power four", "heisman trophy", "college football"}


# typed in lowercase: "how many yards does bob fakename have"
_LOWER_NAME = re.compile(r"\b(?:does|did|has|is|for|about|on)\s+([a-z][a-z'’-]+\s+[a-z][a-z'’-]+)\b")


def _name_candidates(question: str, idx: dict, skip: list[str]) -> list[str]:
    out = []
    skip_n = {norm(x) for x in skip}
    cands = [m.group(1) for m in _NAME.finditer(question or "")]
    if STAT_ASK.search(question or ""):
        cands += [m for m in _LOWER_NAME.findall(question or "")
                  if not set(norm(m).split()) & (STOPWORDS | _LEAD)]
    for cand in cands:
        words = cand.split()
        if any(w.upper() in _ACRONYMS for w in words) or (words and resolve_team(words[0], idx)):
            continue  # "Texas ATS", "SMU QB": a team and a stat, not a person
        # drop a sentence-initial question word ("How", "Is") glued onto the name
        while words and norm(words[0]) in STOPWORDS | _LEAD:
            words = words[1:]
        cand = " ".join(words)
        n = norm(cand)
        if len(words) < 2 and "-" not in cand:
            continue
        if n in _NOT_NAMES or n in skip_n or any(n in s or s in n for s in skip_n if s):
            continue
        if resolve_team(cand, idx):
            continue
        out.append(cand)
    return out


def _pool_teams_named(q: str, pool_teams: list[str], idx: dict) -> list[str]:
    """Pool teams named in a question, by full name or a unique first word
    that isn't itself a team ("Wake" -> Wake Forest, but "Oregon" stays Oregon)."""
    qn = f" {norm(q)} "
    firsts = collections.Counter(norm(t).split()[0] for t in pool_teams if norm(t))
    out = []
    for t in pool_teams:
        n = norm(t)
        f = n.split()[0] if n else ""
        if f" {n} " in qn or (firsts[f] == 1 and len(f) >= 4 and f not in idx["teams"]
                               and f not in STOPWORDS and f" {f} " in qn):
            out.append(t)
    return out


def question_rows(question: str, pool_games: list[tuple[str, str]], skip_names: list[str] | None = None,
                  idx: dict | None = None, limit: int = 4) -> list[str]:
    """CFBD rows for one chat message, [] when it isn't a stats question.
    `pool_games` is [(away, home), ...] for this week's pool.

    - players named in the question (any team), or a line saying the named
      player isn't in the stats, so the model can't invent one;
    - leaders by category across the pool games (or the pool teams named),
      for "best QB / leading rusher" questions;
    - a team summary for a named non-pool team when team stats are asked.
    `skip_names` are league member names, so they're never read as players."""
    idx = idx or load_index()
    pool_teams = [t for g in pool_games for t in g]
    q = re.sub(r"(\w)['’]s\b", r"\1", question or "")  # "SMU's" -> "SMU"
    if not idx or not q.strip():
        return []
    out: list[str] = []
    players = find_players(q, idx=idx, limit=limit) or find_players(q, teams=pool_teams, idx=idx, limit=limit)
    names = _name_candidates(q, idx, (skip_names or []) + pool_teams)
    found = " ".join(players).lower()
    missing = [n for n in names if norm(n).split()[-1] not in norm(found)]
    if missing and STAT_ASK.search(q):
        out.append(f"NOT IN THE STATS: no player named {', '.join(missing)} in the 2026 season stats. "
                   "Say there's no such player in the data; give no numbers for them.")
    if players:
        out += players
    elif not missing:
        cats = [c for c, rx in _CAT_WORDS if re.search(rx, q, re.I)]
        if cats and (_LEADER_ASK.search(q) or STAT_ASK.search(q)):
            named = _pool_teams_named(q, pool_teams, idx)
            if named and re.search(r"\b(game|matchup|vs\.?|versus|against)\b", q, re.I):
                named = list(dict.fromkeys(t for g in pool_games if set(g) & set(named) for t in g))
            for c in cats[:2]:
                rows = leaders(named or pool_teams, c, limit=3 if named else limit, idx=idx)
                if rows:
                    scope = " and ".join(named) if named else "this week's pool games"
                    out.append(f"{c.upper()} LEADERS, {scope} (season totals, most first):")
                    out += [f"  {r}" for r in rows]
    if _TEAM_ASK.search(q):
        pool_n = {norm(_resolved_name(t, idx)) for t in pool_teams}
        words = norm(q).split()
        seen = set()
        for n in (3, 2, 1):
            for i in range(len(words) - n + 1):
                g = " ".join(words[i:i + n])
                if n == 1 and (g in STOPWORDS or len(g) < 4):
                    continue
                key = ALIASES.get(g, g)
                t = idx["teams"].get(key)
                if t and t.get("sp") and norm(t["name"]) not in pool_n and t["name"] not in seen:
                    seen.add(t["name"])
                    out.append("Not a pool team, stats only: " + compact_team_summary(t["name"], idx))
        out = out[:limit + 6]
    return out


def status(db=None) -> dict:
    db = db or connect()
    rows = db.execute("SELECT name, year, fetched_at, length(json) FROM datasets").fetchall()
    return {
        "month": _month(), "calls_this_month": calls_used(db), "cap": MONTHLY_CAP,
        "datasets": {n: {"year": y, "fetched": dt.datetime.fromtimestamp(f).isoformat(timespec="minutes"),
                         "bytes": b} for n, y, f, b in rows},
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "refresh":
        try:
            print(json.dumps(refresh(only=sys.argv[2:] or None)))
        except BudgetExceeded as e:
            log.warning("refresh skipped, budget: %s", e)
            print(json.dumps({"ok": False, "skipped": str(e)}))
            sys.exit(0)
    else:
        print(json.dumps(status(), indent=1))
