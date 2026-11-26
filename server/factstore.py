#!/usr/bin/env python3
"""Picky's fact store v1 (docs/picky-code-ontology-plan-2026-11-18.md 2.3).

One place every football lane reads. `build()` is pure code (no model, no network): it joins the CFBD cache,
ESPN market snapshots (market_snapshot.py), checked ESPN polls (polls.py), the sourced availability notes
(slate_intel.json) and the weather cache into
  - one GAME SHEET per game in the next 9 days (market open/now/move, ranks, records, availability, weather,
    last meeting, venue), and
  - one TEAM SHEET per FBS team (record, conference record, ranks, SP+, SRS, ATS, coach, results vs the number),
each with a `facts` list of {id, kind, subject, value, text, source, as_of}, plus the big picture (plan phase 5):
coach careers (CFBD /coaches, every season since 1980), coach headlines (news.py + the code-made insider wire),
each conference's games and title race (title_race.race; conf_race() adds today's finals), and the committed CFP
format (cfp_rules.json). It writes server/cache/factstore.json
in one shot (temp file + rename) and mirrors changed sheets to Supabase picky.sheet. Readers use the local file
(ms); a missing file is rebuilt from the Supabase copy. Public football facts only: no picks, no members.

Lanes call the gateway functions below (find_game, market, availability, conf_race, coach_careers, ...) and nothing else
for football data; test_factstore.py checks that statically for router.STORE_LANES. A lane may return
answer(text, facts) so the send point can check every number against facts_used (unsupported()).

  factstore.py build        # run by the news-refresh tick (5 min) and after cfbd refresh
  factstore.py refresh      # polls (1 h TTL) + build
  factstore.py show <team or game id>
"""

from __future__ import annotations

import datetime as dt
import gzip
import hashlib
import json
import logging
import os
import re
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import cfbd  # noqa: E402
import market_open  # noqa: E402

log = logging.getLogger("picky.factstore")
VERSION = 1
CACHE_PATH = Path(os.environ.get("PICKY_FACTSTORE", HERE / "cache" / "factstore.json"))
HORIZON_D = 9
KEY_NUMBERS = (3, 7, 10, 14)
DATASETS = ("games", "lines", "lines_prev", "records", "ats", "sp", "srs", "coaches", "coaches_prev", "coaches_all")
MARKET_HARD_S = (6 * 3600, 3 * 3600)
COACHING_FRESH_D = 8   # slate_intel's (Opus-made) coaching notes ride only while the file is this fresh
COACH_NEWS_D = 7       # coach headlines this recent (news.py keeps 7 days)
COACH_NEWS = re.compile(r"\b(fires|fired|firing|hot seat|on the seat|job security|coaching (search|carousel|change|changes|"
                        r"candidates?|hires?|vacanc\w*|staff)|carousel|interim|head coach(ing)? (job|search|opening|"
                        r"vacancy|candidates?)|hires?|hired|candidates?|buyout|contract extension|extension|parts? ways|"
                        r"parted ways|steps? down|stepping down|resign\w*|(open|vacant) jobs?)\b", re.I)
NOT_COACH_NEWS = re.compile(r"\bunder fire\b|\bfires? back\b|\bfires (another |a )?shot\b|athletic director|\bAD\b|\b(QB|quarterback|transfer|recruit\w*|commit\w*)\b", re.I)
FIXTURE_FETCHED = dt.datetime(2026, 11, 18, 12, 15, tzinfo=dt.timezone.utc).timestamp()  # when the fixture was frozen  # ESPN snapshot past this (normal, game day) loses to the CFBD line


# =============================================================================================================
# inputs
# =============================================================================================================

@dataclass
class Inputs:
    cfbd_db: Path | None = None
    fixture_dir: Path | None = None
    slate_intel_path: Path | None = None
    weather_path: Path | None = None
    polls_path: Path | None = None
    market_db: Path | None = None
    live_path: Path | None = None
    news_path: Path | None = None
    wire_path: Path | None = None
    personnel_db: Path | None = None      # cache/personnel.sqlite (rosters + CFBD player_game)
    personnel_path: Path | None = None    # the fixture's frozen slice (personnel.json.gz)
    dossier_dir: Path | None = None
    _ds: dict = field(default_factory=dict, repr=False)

    @classmethod
    def live(cls) -> "Inputs":
        import market_snapshot
        import personnel
        import polls
        return cls(cfbd_db=cfbd.DB_PATH, slate_intel_path=HERE / "cache" / "slate_intel.json",
                   weather_path=HERE / "cache" / "weather.json", polls_path=polls.PATH,
                   market_db=market_snapshot.DB_PATH,
                   live_path=Path(os.environ.get("PICKY_LIVE_CACHE", HERE / "cache" / "live_games.json")),
                   news_path=Path(os.environ.get("PICKY_NEWS_CACHE", HERE / "cache" / "news.json")),
                   wire_path=Path(os.environ.get("PICKY_INSIDER_WIRE",
                                                 "/Users/Agent/cfb-pickem-private/insider_wire.json")),
                   personnel_db=personnel.DB_PATH, dossier_dir=HERE / "cache" / "dossiers")

    @classmethod
    def from_fixture(cls, d: Path, **kw) -> "Inputs":
        d = Path(d)
        kw.setdefault("news_path", d / "news.json" if (d / "news.json").exists() else None)
        kw.setdefault("wire_path", d / "insider_wire.json" if (d / "insider_wire.json").exists() else None)
        kw.setdefault("personnel_path", d / "personnel.json.gz" if (d / "personnel.json.gz").exists() else None)
        kw.setdefault("dossier_dir", d / "dossiers")
        return cls(fixture_dir=d, slate_intel_path=d / "slate_intel.json", weather_path=d / "weather.json", **kw)

    def personnel(self) -> dict:
        """{'rosters': {team: {...}}, 'player_games': [rows], 'fetched': ts}; {} when there's none yet."""
        if self.personnel_path:
            try:
                return json.loads(gzip.decompress(Path(self.personnel_path).read_bytes()))
            except (OSError, ValueError):
                return {}
        if self.personnel_db and Path(self.personnel_db).exists():
            import personnel
            db = sqlite3.connect(self.personnel_db)
            try:
                db.execute("select 1 from rosters limit 1")
                f = db.execute("select max(fetched_at) from player_games").fetchone()[0]
                return {"rosters": personnel.rosters(db), "player_games": personnel.player_games(db), "fetched": f}
            except sqlite3.Error:
                return {}
            finally:
                db.close()
        return {}

    def dossiers(self) -> list[dict]:
        """The two newest weeks' dossiers (sourced facts only matter here)."""
        d = Path(self.dossier_dir) if self.dossier_dir else None
        if not d or not d.is_dir():
            return []
        weeks = sorted((p for p in d.iterdir() if p.is_dir() and p.name.isdigit()), key=lambda p: int(p.name))[-2:]
        out = []
        for w in weeks:
            for f in sorted(w.glob("[0-9]*.json")):
                x = _json(f)
                if x.get("facts") or x.get("opus"):
                    out.append(x)
        return out

    def datasets(self) -> dict:
        if self._ds:
            return self._ds
        if self.fixture_dir:
            self._ds = json.loads(gzip.decompress((Path(self.fixture_dir) / "cfbd.json.gz").read_bytes()))
            self._ds.setdefault("_fetched", {n: FIXTURE_FETCHED for n in DATASETS})
        else:
            db = sqlite3.connect(self.cfbd_db or cfbd.DB_PATH)
            try:
                q = "select name, json, fetched_at from datasets where name in (%s)" % ",".join("?" * len(DATASETS))
                rows = db.execute(q, DATASETS).fetchall()
            finally:
                db.close()
            self._ds = {n: json.loads(j) for n, j, _ in rows}
            self._ds["_fetched"] = {n: f for n, _, f in rows}
        return self._ds


def _json(p: Path | None) -> dict:
    try:
        return json.loads(Path(p).read_text()) if p else {}
    except (OSError, ValueError):
        return {}


# =============================================================================================================
# small helpers
# =============================================================================================================

def _iso(ts: float | None) -> str:
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat(timespec="seconds") if ts else ""


def _sd(s) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None


def _slug(s) -> str:
    return re.sub(r"[^a-z0-9]+", "_", cfbd.norm(str(s))).strip("_")


def _fact(kind: str, subject: str, field_: str, value, text: str, source: str, as_of: str) -> dict:
    return {"id": f"{kind}.{_slug(subject)}.{field_}", "kind": kind, "subject": subject, "value": value,
            "text": text, "source": source, "as_of": as_of or ""}


def _fav(home: str, away: str, sh: float | None) -> str:
    if sh is None:
        return "no line"
    if sh == 0:
        return "a pick'em"
    return f"{home} {sh:g}" if sh < 0 else f"{away} {-sh:g}"


def _game_day(now: float) -> bool:
    try:
        import freshness
        return freshness.game_day(now)
    except Exception:  # noqa: BLE001
        return False


def _cfbd_line(g: dict) -> dict | None:
    ls = [x for x in g.get("lines") or [] if x.get("spread") is not None]
    return next((x for x in ls if x.get("provider") == "DraftKings"), ls[0] if ls else None)


# =============================================================================================================
# build
# =============================================================================================================

def _team_sheets(ds: dict, polls_d: dict, now: float) -> dict[str, dict]:
    import polls as P
    fetched = ds.get("_fetched") or {}
    as_cfbd = _iso(fetched.get("records")) or _iso(now)
    season = max((r.get("year") or 0 for r in ds.get("records") or []), default=0)
    ranks = {}
    for kind in ("CFP", "AP", "Coaches"):
        p = P.serve(kind, polls_d)
        for r in (p or {}).get("ranks") or []:
            ranks.setdefault(r["team"], {})[kind] = r["rank"]
            ranks[r["team"]].setdefault("_dates", {})[kind] = p.get("date")
    by = lambda rows, key="team": {r.get(key): r for r in rows or [] if r.get(key)}  # noqa: E731
    ats, sp, srs = by(ds.get("ats")), by(ds.get("sp")), by(ds.get("srs"))
    coach = {}
    for c in ds.get("coaches") or []:
        for s in c.get("seasons") or []:
            if s.get("year") == season and s.get("school"):
                coach[s["school"]] = f"{c.get('firstName', '')} {c.get('lastName', '')}".strip()
    results: dict[str, list] = {}
    for g in sorted(ds.get("lines") or [], key=lambda g: g.get("startDate") or ""):
        if g.get("homeScore") is None or g.get("awayScore") is None or g.get("seasonType") not in (None, "regular"):
            continue
        ln = _cfbd_line(g)
        sp_home = ln.get("spread") if ln else None
        for side, opp in (("home", "away"), ("away", "home")):
            mine, theirs = g[f"{side}Score"], g[f"{opp}Score"]
            my_sp = (sp_home if side == "home" else -sp_home) if sp_home is not None else None
            results.setdefault(g[f"{side}Team"], []).append({
                "game_id": str(g["id"]), "date": (g.get("startDate") or "")[:10], "opp": g[f"{opp}Team"],
                "home": side == "home", "pts": mine, "opp_pts": theirs, "spread": my_sp,
                "cover": (mine - theirs + my_sp) if my_sp is not None else None})
    out = {}
    for r in ds.get("records") or []:
        if r.get("classification") not in (None, "fbs") or not r.get("team"):
            continue
        name = r["team"]
        tot, conf = r.get("total") or {}, r.get("conferenceGames") or {}
        hg, ag = r.get("homeGames") or {}, r.get("awayGames") or {}
        a, s, x = ats.get(name) or {}, sp.get(name) or {}, srs.get(name) or {}
        rk = {k: v for k, v in (ranks.get(name) or {}).items() if not k.startswith("_")}
        t = {"name": name, "conference": r.get("conference"), "season": season,
             "record": f"{tot.get('wins', 0)}-{tot.get('losses', 0)}",
             "conf_record": f"{conf.get('wins', 0)}-{conf.get('losses', 0)}",
             "home_record": f"{hg.get('wins', 0)}-{hg.get('losses', 0)}",
             "road_record": f"{ag.get('wins', 0)}-{ag.get('losses', 0)}",
             "ranks": rk,
             "sp": {"rating": s.get("rating"), "rank": s.get("ranking"),
                    "off_rank": (s.get("offense") or {}).get("ranking"),
                    "def_rank": (s.get("defense") or {}).get("ranking")} if s else {},
             "srs": x.get("rating"),
             "ats": {"w": a.get("atsWins"), "l": a.get("atsLosses"), "p": a.get("atsPushes"),
                     "avg_margin": a.get("avgCoverMargin")} if a else {},
             "coach": coach.get(name), "results": results.get(name) or [], "as_of": as_cfbd}
        f = [_fact("record", name, "overall", t["record"], f"{name} is {t['record']}", "CFBD records", as_cfbd),
             _fact("record", name, "conf", t["conf_record"], f"{name} is {t['conf_record']} in conference play",
                   "CFBD records", as_cfbd)]
        for kind, n in rk.items():
            f.append(_fact("rank", name, kind.lower(), n, f"{name} is No. {n} in the {kind}"
                           + (" rankings" if kind == "CFP" else " poll"), f"ESPN {kind}",
                           (ranks.get(name) or {}).get("_dates", {}).get(kind) or ""))
        if t["sp"].get("rank"):
            f.append(_fact("sp", name, "rank", t["sp"]["rank"], f"{name} is No. {t['sp']['rank']} in SP+",
                           "CFBD SP+", as_cfbd))
        if t["ats"].get("w") is not None:
            atxt = f"{t['ats']['w']}-{t['ats']['l']}" + (f"-{t['ats']['p']}" if t["ats"].get("p") else "")
            f.append(_fact("ats", name, "overall", atxt, f"{name} is {atxt} ATS", "CFBD ATS", as_cfbd))
        if t["coach"]:
            f.append(_fact("coach", name, "head", t["coach"], f"{t['coach']} coaches {name}", "CFBD coaches", as_cfbd))
        t["ats_profile"] = ats_profile(t["results"])
        f += ats_profile_facts(name, t["ats_profile"], as_cfbd)
        t["facts"] = f
        out[name] = t
    return out


def _wlp(rows: list[dict]) -> list[int]:
    return [sum(1 for r in rows if r["cover"] > 0), sum(1 for r in rows if r["cover"] < 0),
            sum(1 for r in rows if r["cover"] == 0)]


def wlp_text(r: list[int]) -> str:
    return f"{r[0]}-{r[1]}" + (f"-{r[2]}" if r[2] else "")


def ats_profile(results: list[dict]) -> dict:
    """A team's season vs the number from its results (oldest first): overall, fav/dog, home/road, last 4, streak,
    average cover margin, biggest miss. Cover margin > 0 = covered; a pick'em counts as neither fav nor dog."""
    rows = [r for r in results if r.get("cover") is not None and r.get("spread") is not None]
    p = {"games": len(rows), "overall": _wlp(rows),
         "fav": _wlp([r for r in rows if r["spread"] < 0]), "dog": _wlp([r for r in rows if r["spread"] > 0]),
         "home": _wlp([r for r in rows if r["home"]]), "road": _wlp([r for r in rows if not r["home"]]),
         "last4": _wlp(rows[-4:]) if len(rows) >= 4 else None, "streak": None, "avg_margin": None,
         "biggest_miss": None}
    if rows:
        p["avg_margin"] = round(sum(r["cover"] for r in rows) / len(rows), 1)
        last = rows[-1]["cover"]
        if last != 0:
            n = 0
            for r in reversed(rows):
                if (r["cover"] > 0) != (last > 0) or r["cover"] == 0:
                    break
                n += 1
            p["streak"] = {"kind": "cover" if last > 0 else "miss", "n": n}
        worst = min(rows, key=lambda r: r["cover"])
        if worst["cover"] < 0:
            p["biggest_miss"] = {k: worst[k] for k in ("opp", "date", "home", "spread", "cover")}
    return p


def ats_profile_facts(name: str, p: dict, as_of: str) -> list[dict]:
    if not p["games"]:
        return []
    src = "CFBD lines + results (DraftKings line)"
    f = [_fact("ats", name, "calc_overall", wlp_text(p["overall"]),
               f"{name} is {wlp_text(p['overall'])} ATS in {p['games']} games with a line", src, as_of)]
    for k, say in (("fav", "as a favorite"), ("dog", "as an underdog"), ("home", "at home"), ("road", "on the road")):
        if sum(p[k]):
            f.append(_fact("ats", name, k, wlp_text(p[k]), f"{name} is {wlp_text(p[k])} ATS {say}", src, as_of))
    if p["last4"]:
        f.append(_fact("ats", name, "last4", wlp_text(p["last4"]),
                       f"{name} covered {p['last4'][0]} of its last 4", src, as_of))
    s = p["streak"]
    if s:
        f.append(_fact("ats", name, "streak", s["n"], f"{name} has {'covered' if s['kind'] == 'cover' else 'missed'} "
                                                      f"{s['n']} straight", src, as_of))
    if p["avg_margin"] is not None:
        f.append(_fact("ats", name, "avg_margin", p["avg_margin"],
                       f"{name}'s average cover margin is {p['avg_margin']:+g}", src, as_of))
    m = p["biggest_miss"]
    if m:
        role = (f"a {-m['spread']:g}-point favorite" if m["spread"] < 0 else f"a {m['spread']:g}-point underdog"
                if m["spread"] > 0 else "a pick'em")
        f.append(_fact("ats", name, "biggest_miss", m["cover"],
                       f"{name}'s biggest miss: {-m['cover']:g} points short as {role} vs {m['opp']} ({m['date']})",
                       src, as_of))
    return f


def _market(g: dict, snaps: list[dict] | None, now: float) -> tuple[dict, dict]:
    """(chosen market, CFBD market). ESPN snapshots win while fresh; CFBD's DraftKings line otherwise."""
    import market_snapshot as M
    ln = _cfbd_line(g) or {}
    fetched = g.get("_fetched_lines")
    cf = {"spread_home": ln.get("spread"), "open_spread_home": ln.get("spreadOpen"), "total": ln.get("overUnder"),
          "open_total": ln.get("overUnderOpen"), "source": "CFBD " + (ln.get("provider") or "lines"),
          "as_of": _iso(fetched) if fetched else ""}
    cf["move"] = (round(cf["spread_home"] - cf["open_spread_home"], 1)
                  if cf["spread_home"] is not None and cf["open_spread_home"] is not None else None)
    cf["total_move"] = (round(cf["total"] - cf["open_total"], 1)
                        if cf["total"] is not None and cf["open_total"] is not None else None)
    # DraftKings' look-ahead opener vs another book's open (market_open): the honest open, and the early number kept
    eo = market_open.early_open(g.get("lines"))
    if str(ln.get("provider")) == market_open.BOOK:
        cf = market_open.fix(cf, eo, "open_spread_home", "spread_home", "open_total", "total")
    s = M.summary(str(g["id"]), rows=snaps) if snaps else None
    hard = MARKET_HARD_S[1 if _game_day(now) else 0]
    if s and s.get("spread_home") is not None and now - s["as_of_ts"] <= hard:
        es = {k: s.get(k) for k in ("spread_home", "open_spread_home", "total", "open_total", "move", "total_move",
                                     "ml_home", "ml_away", "n")}
        es.update(source=s["source"], as_of=_iso(s["as_of_ts"]), as_of_ts=s["as_of_ts"])
        if market_open.BOOK in str(s["source"]):  # ESPN's open is DraftKings' open too
            es = market_open.fix(es, eo, "open_spread_home", "spread_home", "open_total", "total")
        return es, cf
    return dict(cf), cf


def _meeting(away: str, home: str, prev: list[dict]) -> dict | None:
    pair = {cfbd.norm(away), cfbd.norm(home)}
    hits = [x for x in prev if {cfbd.norm(x["homeTeam"]), cfbd.norm(x["awayTeam"])} == pair
            and x.get("homeScore") is not None]
    if not hits:
        return None
    x = max(hits, key=lambda x: x.get("startDate") or "")
    ln = _cfbd_line(x)
    aw = x["awayScore"] > x["homeScore"]
    win = x["awayTeam"] if aw else x["homeTeam"]
    hi, lo = max(x["awayScore"], x["homeScore"]), min(x["awayScore"], x["homeScore"])
    m = {"season": x.get("season"), "date": (x.get("startDate") or "")[:10], "away": x["awayTeam"],
         "home": x["homeTeam"], "winner": win, "score": f"{hi}-{lo}",
         "where": f"at {x['homeTeam']}" if aw else f"at home vs {x['awayTeam']}", "line": None, "covered": None,
         "cover_by": None}
    if ln:
        hs = ln["spread"]
        cm = x["homeScore"] - x["awayScore"] + hs
        fav, fsp = (x["homeTeam"], hs) if hs < 0 else (x["awayTeam"], -hs)
        m.update(line=f"{fav} {fsp:g}" if fsp else "pick'em", covered=x["homeTeam"] if cm > 0 else x["awayTeam"],
                 cover_by=abs(cm))
    return m


def meeting_text(m: dict | None) -> str | None:
    """The slate_cards.last_meeting line shape (the h2h lane parses it)."""
    if not m:
        return None
    s = f"  Last meeting ({m['season']}): {m['winner']} won {m['score']} {m['where']}"
    if m.get("line"):
        s += f", line {m['line']}: {m['covered']} covered by {m['cover_by']:g}"
    return s


def _game_sheets(ds: dict, teams: dict, intel: dict, weather: dict, snaps: dict, now: float) -> dict[str, dict]:
    gmeta = {str(g["id"]): g for g in ds.get("games") or []}
    fetched = (ds.get("_fetched") or {}).get("lines")
    t0 = dt.datetime.fromtimestamp(now, dt.timezone.utc)
    lo, hi = t0 - dt.timedelta(hours=6), t0 + dt.timedelta(days=HORIZON_D)
    prev = ds.get("lines_prev") or []
    out = {}
    for g in ds.get("lines") or []:
        s = _sd(g.get("startDate"))
        if s is None or not (lo <= s <= hi) or "fbs" not in (g.get("homeClassification"), g.get("awayClassification")):
            continue
        gid = str(g["id"])
        away, home = g["awayTeam"], g["homeTeam"]
        gm = gmeta.get(gid) or {}
        mk, mk_cf = _market(dict(g, _fetched_lines=fetched), snaps.get(gid), now)
        gi = (intel.get("games") or {}).get(gid) or {}
        avail = []
        for f in gi.get("facts") or []:
            if f.get("kind") != "injury" and not f.get("status"):
                continue
            avail.append({"team": gi.get(f.get("team")) if f.get("team") in ("home", "away") else None,
                          "status": f.get("status"), "players": f.get("players") or [], "text": f.get("text") or "",
                          "source_name": f.get("source_name"), "source_url": f.get("source_url"),
                          "date": f.get("date"), "confidence": f.get("confidence"), "url_check": f.get("url_check"),
                          "kind": f.get("kind")})
        w = (weather.get("games") or {}).get(gid)
        ta, th = teams.get(away) or {}, teams.get(home) or {}
        sheet = {"game_id": gid, "season": g.get("season"), "week": g.get("week"), "start": g.get("startDate"),
                 "away": away, "home": home, "neutral": bool(gm.get("neutralSite")),
                 "conf_game": bool(gm.get("conferenceGame")), "venue": gm.get("venue"),
                 "market": mk, "market_cfbd": mk_cf,
                 "ranks": {"away": ta.get("ranks") or {}, "home": th.get("ranks") or {}},
                 "records": {"away": ta.get("record"), "home": th.get("record")},
                 "availability": avail, "intel_as_of": intel.get("as_of"), "intel_facts": gi.get("facts") or [],
                 "weather": w, "meeting": _meeting(away, home, prev)}
        f = []
        if mk.get("spread_home") is not None:
            f.append(_fact("market", gid, "spread", mk["spread_home"],
                           f"{away} at {home}: {_fav(home, away, mk['spread_home'])}", mk["source"], mk["as_of"]))
        if mk.get("open_spread_home") is not None:
            f.append(_fact("market", gid, "open", mk["open_spread_home"],
                           f"{away} at {home} opened {_fav(home, away, mk['open_spread_home'])}", mk["source"],
                           mk["as_of"]))
        if mk.get("early_open"):
            eo = mk["early_open"]
            f.append(_fact("market", gid, "early_open", eo["spread"],
                           f"{away} at {home}: {market_open._pos(eo['book'])} early opener was {_fav(home, away, eo['spread'])}"
                           + (f" (total {eo['total']:g})" if eo.get("total") is not None else "")
                           + f"; {eo['vs']} opened {_fav(home, away, eo['spread_open'])}"
                           + (f" (total {eo['total_open']:g})" if eo.get("total_open") is not None else ""),
                           f"CFBD lines ({eo['book']}, {eo['vs']})", mk["as_of"]))
        if mk.get("move"):
            f.append(_fact("market", gid, "move", mk["move"],
                           f"the line moved {abs(mk['move']):g} toward {home if mk['move'] < 0 else away}",
                           mk["source"], mk["as_of"]))
        if mk.get("total") is not None:
            f.append(_fact("market", gid, "total", mk["total"], f"total {mk['total']:g}", mk["source"], mk["as_of"]))
        if mk.get("total_move"):
            f.append(_fact("market", gid, "total_move", mk["total_move"],
                           f"the total moved {abs(mk['total_move']):g} from {mk['open_total']:g}", mk["source"],
                           mk["as_of"]))
        for a in avail:
            subj = (a["players"] or [a["team"] or gid])[0]
            f.append(_fact("availability", subj, _slug(a.get("status") or "note"), a.get("status"), a["text"],
                           a.get("source_name") or "slate_intel", a.get("date") or ""))
        if w and w.get("temp_f") is not None and w.get("as_of"):
            f.append(_fact("weather", gid, "forecast", w.get("temp_f"),
                           f"forecast {w['temp_f']:g}F" + (f", wind {w['wind_mph']:g} mph" if w.get("wind_mph") is not None
                                                            else ""), w.get("src") or "forecast", w["as_of"]))
        m = sheet["meeting"]
        if m:
            f.append(_fact("meeting", gid, "last", m["score"], meeting_text(m).strip(), "CFBD lines (last season)",
                           m["date"]))
        for side, t in (("away", ta), ("home", th)):
            f += [x for x in t.get("facts") or [] if x["kind"] in ("record", "rank", "ats", "sp")]
        sheet["facts"] = f
        out[gid] = sheet
    return out


# --- big picture (plan phase 5): coach careers, coach news, conference races ---------------------------------

def _md(iso) -> str:
    """'Nov 17' (Central time) for a dated item."""
    d = _sd(iso)
    if not d:
        return ""
    try:
        from zoneinfo import ZoneInfo
        d = d.astimezone(ZoneInfo("America/Chicago")) if d.tzinfo else d
    except Exception:  # noqa: BLE001
        pass
    return f"{d:%b} {d.day}"


def _wl(w, l_, t=0) -> str:
    return f"{w}-{l_}" + (f"-{t}" if t else "")


def _coach_careers(ds: dict, season: int, as_of: str) -> dict[str, dict]:
    """{full name: career} for every head coach active this season or last: FBS stops (school, years, W-L) from
    CFBD /coaches (every season since 1980 in coaches_all; this + last season otherwise). A season in progress
    shows 0 games in CFBD, so its W-L stays out of the stop (the team sheet has this season's record)."""
    src = ds.get("coaches_all") or (list(ds.get("coaches_prev") or []) + list(ds.get("coaches") or []))
    seasons: dict[str, dict] = {}
    for c in src:
        name = f"{c.get('firstName') or ''} {c.get('lastName') or ''}".strip()
        if not name:
            continue
        e = seasons.setdefault(name, {"first": c.get("firstName"), "last": c.get("lastName"), "hire": c.get("hireDate"),
                                      "rows": {}})
        e["hire"] = e["hire"] or c.get("hireDate")
        for x in c.get("seasons") or []:
            if x.get("year") and x.get("school"):
                e["rows"][(x["year"], x["school"])] = x
    out = {}
    for name, e in seasons.items():
        rows = sorted(e["rows"].values(), key=lambda x: x["year"])
        if not rows or rows[-1]["year"] < season - 1:
            continue
        stops = []
        for x in rows:
            st = stops[-1] if stops else None
            played = bool(x.get("games")) or bool((x.get("wins") or 0) + (x.get("losses") or 0))
            if st and st["school"] == x["school"] and x["year"] == st["to"] + 1:
                st["to"] = x["year"]
            else:
                st = {"school": x["school"], "from": x["year"], "to": x["year"], "w": 0, "l": 0, "t": 0, "seasons": 0,
                      "in_progress": False}
                stops.append(st)
            if played:
                st["w"] += x.get("wins") or 0
                st["l"] += x.get("losses") or 0
                st["t"] += x.get("ties") or 0
                st["seasons"] += 1
            elif x["year"] == season:
                st["in_progress"] = True
        current = rows[-1]["year"] == season
        cw, cl, ct = (sum(s_[k] for s_ in stops) for k in ("w", "l", "t"))

        def stop_txt(s_):
            yrs = f"{s_['from']}" if s_["from"] == s_["to"] else f"{s_['from']}-{s_['to']}"
            if s_["in_progress"] and not s_["seasons"]:
                return f"{s_['school']} {yrs} (this season)"
            wl = _wl(s_["w"], s_["l"], s_["t"])
            return f"{s_['school']} {yrs} ({wl}" + (f" through {s_['to'] - 1})" if s_["in_progress"] else ")")
        text = (f"{name}, FBS head coach: " + ", ".join(stop_txt(s_) for s_ in stops)
                + (f"; {_wl(cw, cl, ct)} career" + (" before this season" if current else "") if cw + cl else ""))
        out[name] = {"name": name, "first": e["first"], "last": e["last"], "hire_date": (e["hire"] or "")[:10],
                     "school": rows[-1]["school"], "current": current, "stops": stops,
                     "career": {"w": cw, "l": cl, "t": ct}, "text": text, "as_of": as_of}
    return out


def _coach_news(news: dict, wire: dict, now: float) -> list[dict]:
    """Coach headlines (fired / hot seat / searches / hires / interim) from news.py's cache plus the code-made insider
    wire's carousel items, newest first, deduped, last COACH_NEWS_D days."""
    lo = now - COACH_NEWS_D * 86400
    seen, out = set(), []
    rows = [(it, "news") for it in (news.get("items") or {}).values()]
    rows += [(it, "wire") for it in wire.get("items") or [] if "Coaching carousel" in (it.get("sections") or [])]
    for it, frm in rows:
        title = (it.get("title") or "").strip()
        d = _sd(it.get("date"))
        if not title or not d or not COACH_NEWS.search(title) or NOT_COACH_NEWS.search(title):
            continue
        ts = d.timestamp() if d.tzinfo else d.replace(tzinfo=dt.timezone.utc).timestamp()
        if not lo <= ts <= now + 3600:
            continue
        key = re.sub(r"[^a-z0-9]+", "", title.lower())
        story = it.get("story")
        if key in seen or (story and story in seen):
            continue
        seen |= {key} | ({story} if story else set())
        src = it.get("source") or it.get("outlet") or "news"
        out.append({"title": title, "source": src, "date": d.isoformat(timespec="minutes"), "ts": ts,
                    "teams": list(it.get("teams") or []), "url": it.get("link") or it.get("url"),
                    "insider": it.get("insider"), "from": frm,
                    "hot": bool(re.search(r"hot seat|on the seat|job security|fire|firing|buyout|parts? ways|parted",
                                          title, re.I)),
                    "fact": _fact("coach_news", (it.get("teams") or ["college football"])[0], key[:48], title,
                                  f"{title} ({src}, {_md(d.isoformat())})", src, d.isoformat(timespec="minutes"))})
    out.sort(key=lambda x: x["ts"], reverse=True)
    return out[:60]


def _conf_games(ds: dict) -> dict[str, list[dict]]:
    """{conference: [{id, week, start, home, away, home_pts, away_pts}]}: this season's regular-season conference
    games (CFBD schedule; scores from the daily lines pull, which is fresher than the weekly games pull)."""
    scores = {str(g["id"]): (g.get("homeScore"), g.get("awayScore")) for g in ds.get("lines") or []}
    out: dict[str, list[dict]] = {}
    for g in ds.get("games") or []:
        conf = g.get("homeConference")
        if (not g.get("conferenceGame") or g.get("seasonType", "regular") != "regular" or not conf
                or conf != g.get("awayConference") or conf == "FBS Independents"):
            continue
        hp, ap = scores.get(str(g["id"]), (None, None))
        if hp is None or ap is None:
            hp, ap = g.get("homePoints"), g.get("awayPoints")
        out.setdefault(conf, []).append({"id": str(g["id"]), "week": g.get("week"), "start": g.get("startDate"),
                                         "home": g["homeTeam"], "away": g["awayTeam"], "home_pts": hp, "away_pts": ap})
    for v in out.values():
        v.sort(key=lambda x: x["start"] or "")
    return out


def _conf_races(conf_games: dict, now: float, as_of: str) -> dict[str, dict]:
    import title_race
    t0 = dt.datetime.fromtimestamp(now, dt.timezone.utc)
    return {c: title_race.race(c, gs, t0, as_of=as_of, source="CFBD games") for c, gs in conf_games.items()}


# --- personnel (plan phase 4): QB rooms, the qb_start ledger, normalized availability ----------------------------

FRESH_D = 10  # a report this new can explain a line move; older ones are in the number already


def _qb_text(q: dict, team: str, thru: int | None = None) -> tuple[str, str]:
    """(starts fact text, season line fact text) for one QB; the line says its window (CFBD box scores through
    week N) so an answer can say where the number comes from."""
    if q["starts"]:
        st = (f"{q['name']} has {q['starts']} starts at QB for {team} this season ({q['su']} SU, {q['ats']} ATS as the "
              f"starter)" + (f"; he's started the last {q['streak']} straight" if q.get("streak") else ""))
    else:
        st = f"{q['name']} has no starts at QB for {team} this season"
    ln = q["line"]
    line = (f"{q['name']} this season: {ln['cmp']}-of-{ln['att']}, {ln['yds']} yards, {ln['td']} TD, {ln['int']} INT"
            + (f"; {ln['car']} rushes for {ln['rush_yds']} yards" if ln.get("car") else "")
            + f" in {ln['games']} games" + (f" (CFBD box scores through week {thru})" if thru else "")) \
        if ln.get("att") else f"{q['name']} hasn't thrown a pass this season"
    return st, line


def _missed(sheet: dict, played: dict | None) -> tuple[int | None, str | None]:
    """(team games in a row he's missed, newest first; None = never in a box score, e.g. a lineman) and the date
    of the team's last game: an injury's recency is read against these, not against when a story repeated it."""
    res = sorted((r for r in sheet.get("results") or [] if r.get("date")), key=lambda r: r["date"], reverse=True)
    last = res[0]["date"][:10] if res else None
    if not played or not played.get("games"):
        return None, last
    ids = {str(g) for g in played["games"]}
    n = 0
    for r in res:
        if str(r.get("game_id")) in ids:
            break
        n += 1
    return n, last


def _key(name: str) -> str:
    """A cheap player-name key for the 16k box-score rows (cfbd.norm is ~40 us a call)."""
    return name.lower().replace(".", "").replace("'", "").replace("’", "").replace(" jr", "").replace(" iii", "").strip()


def _personnel(inp: Inputs, teams: dict, games: dict, intel: dict, now: float) -> dict:
    """Adds qbs / qb_starter / player_status (+ facts) to team sheets and player_status to game sheets."""
    import personnel as PS
    pers = inp.personnel() if hasattr(inp, "personnel") else {}
    rosters = pers.get("rosters") or {}
    season = max((t.get("season") or 0 for t in teams.values()), default=0)
    pg = [r for r in pers.get("player_games") or [] if not season or r.get("season") == season]
    as_pg = _iso(pers.get("fetched")) or next((t["as_of"] for t in teams.values()), _iso(now))
    by_key = {(t, PS._norm(p["name"])): p for t, r in rosters.items() for p in r.get("players") or []}
    positions = {k: p.get("pos") for k, p in by_key.items()}
    counts: dict[str, set] = {}
    for (t, n) in by_key:
        counts.setdefault(n, set()).add(t)
    name_team = {n: next(iter(ts)) for n, ts in counts.items() if len(ts) == 1}
    passers = {r["id"] for r in pg if r.get("att")}
    thru = max((r.get("week") or 0 for r in pg), default=0) or None
    ledger = PS.qb_ledger([r for r in pg if r["id"] in passers], {t: s.get("results") or [] for t, s in teams.items()},
                          positions)
    # who played in each team's latest game (stat rows), and the team's yardage leaders (WR1 / RB1)
    last_gid, played, lead_rec, lead_rush = {}, set(), {}, {}
    for t, s in teams.items():
        rs = [r for r in s.get("results") or [] if r.get("date")]
        if rs:
            last_gid[t] = max(rs, key=lambda r: r["date"])["game_id"]
    season_rows: dict[tuple, dict] = {}
    for r in pg:
        k = (r["team"], _key(r["name"]))
        a = season_rows.setdefault(k, {"rec_yds": 0, "rush_yds": 0, "att": 0, "games": set()})
        a["rec_yds"] += r.get("rec_yds") or 0
        a["rush_yds"] += r.get("rush_yds") or 0
        a["att"] += r.get("att") or 0
        a["games"].add(r["game_id"])
        if last_gid.get(r["team"]) == r["game_id"]:
            played.add(k)
    for (t, n), a in season_rows.items():
        if a["att"] < 20 and a["rec_yds"] > lead_rec.get(t, ("", 0))[1]:
            lead_rec[t] = (n, a["rec_yds"])
        if a["att"] < 20 and a["rush_yds"] > lead_rush.get(t, ("", 0))[1]:
            lead_rush[t] = (n, a["rush_yds"])
    rows = PS.normalize(intel=intel, dossiers=inp.dossiers() if hasattr(inp, "dossiers") else [],
                        news_items=list((_json(inp.news_path).get("items") or {}).values()),
                        roster_rows=[x for r in rosters.values() for x in r.get("status_rows") or []],
                        live=_json(inp.live_path), now=now, name_team=name_team)
    game_of = {}
    for gid, g in sorted(games.items(), key=lambda kv: kv[1].get("start") or ""):
        for t in (g["away"], g["home"]):
            game_of.setdefault(t, gid)
    by_team: dict[str, list] = {}
    for r in rows:
        if r["team"] not in teams:
            continue
        age = (now - PS._ts(r["reported_at"])) / 86400 if r["reported_at"] else 99
        if age > 45 and r["status"] != "season":
            continue
        p = by_key.get((r["team"], PS._norm(r["player"]))) or {}
        k = (r["team"], _key(r["player"]))
        r["pos"] = r.get("pos") or p.get("pos") or ""
        r["cls"] = p.get("cls") or ""
        if not r.get("role"):
            r["role"] = ("WR1" if lead_rec.get(r["team"], ("",))[0] == k[1] and r["pos"] in ("WR", "TE") else
                         "RB1" if lead_rush.get(r["team"], ("",))[0] == k[1] and r["pos"] in ("RB", "FB") else None)
        r["already_out"] = bool(k in season_rows and season_rows[k]["games"] and k not in played
                                and r["team"] in last_gid)
        r["missed"], r["last_game"] = _missed(teams[r["team"]], season_rows.get(k))
        r["value"] = PS.row_value(r, ledger.get(r["team"]) or [])
        if not r.get("game_id") or str(r["game_id"]) not in games:
            r["game_id"] = game_of.get(r["team"])
        r["reported_at"] = r["reported_at"] or _iso(now)
        by_team.setdefault(r["team"], []).append(r)

    def afact(r):
        return _fact("availability", r["player"], _slug(r["status"]), r["status"],
                     f"{r['player']} ({r['team']}) {r['status']}: {r['quote']}", r["source_name"] or "news",
                     r["reported_at"])
    for t, s in teams.items():
        led = ledger.get(t) or []
        room, seen = [], set()
        res = sorted(s.get("results") or [], key=lambda x: x.get("date") or "", reverse=True)
        for q in led:
            p = by_key.get((t, PS._norm(q["name"]))) or {}
            ids = {g["game_id"] for g in q["start_games"]}
            streak = 0
            for x in res:
                if str(x["game_id"]) not in ids:
                    break
                streak += 1
            room.append({"name": q["name"], "id": q["id"], "cls": p.get("cls") or "", "jersey": p.get("jersey") or "",
                         "starts": q["starts"], "su": q["su"], "ats": q["ats"], "first_start": q["first_start"],
                         "last_start": q["last_start"], "streak": streak, "line": q["line"], "rating": q["rating"],
                         "start_games": [{k: g[k] for k in ("date", "opp", "won", "cover")} for g in q["start_games"]]})
            seen.add(PS._norm(q["name"]))
        for p in (rosters.get(t) or {}).get("players") or []:
            if p.get("pos") == "QB" and PS._norm(p["name"]) not in seen:
                room.append({"name": p["name"], "id": p["id"], "cls": p.get("cls") or "", "jersey": p.get("jersey") or "",
                             "starts": 0, "su": "0-0", "ats": "0-0", "first_start": None, "last_start": None,
                             "streak": 0, "line": {"cmp": 0, "att": 0, "yds": 0, "td": 0, "int": 0, "car": 0,
                                                   "rush_yds": 0, "rush_td": 0, "games": 0},
                             "rating": None, "start_games": []})
        if room:
            s["qbs"] = room
            st = PS.starter(led)
            s["qb_starter"] = st["name"] if st else None
            for q in room:
                a, b = _qb_text(q, t, thru)
                s["facts"].append(_fact("qb_start", q["name"], "starts", q["starts"], a, "CFBD box scores + lines", as_pg))
                s["facts"].append(_fact("qb_line", q["name"], "season", q["line"].get("att"), b, "CFBD box scores", as_pg))
                if q["cls"]:
                    s["facts"].append(_fact("roster", q["name"], "class", q["cls"],
                                            f"{q['name']} ({t} QB) is listed as a {q['cls'].lower()}", "ESPN roster",
                                            as_pg))
        if t in by_team:
            s["player_status"] = by_team[t]
            s["facts"] += [afact(r) for r in by_team[t]]
    for g in games.values():
        ps = by_team.get(g["away"], []) + by_team.get(g["home"], [])
        g["player_status"] = ps
        g["facts"] += [afact(r) for r in ps]
    return {"rosters": len(rosters), "player_games": len(pg), "availability": sum(len(v) for v in by_team.values()),
            "fetched": pers.get("fetched")}


def _hash(x) -> str:
    return hashlib.sha1(json.dumps(x, sort_keys=True, default=str).encode()).hexdigest()


def build(inp: Inputs | None = None, now: float | None = None, out: Path | None = None, push="default") -> dict:
    """Build the whole store and swap it in atomically. push: None (local only), a callable(rows), or the
    Supabase mirror (default). Returns a small summary."""
    import market_snapshot as M
    t = time.time()
    now = now or time.time()
    inp = inp or Inputs.live()
    out = Path(out or CACHE_PATH)
    ds = inp.datasets()
    polls_d = _json(inp.polls_path)
    teams = _team_sheets(ds, polls_d, now)
    snaps = M.latest_all(inp.market_db) if inp.market_db else {}
    games = _game_sheets(ds, teams, _json(inp.slate_intel_path), _json(inp.weather_path), snaps, now)
    intel = _json(inp.slate_intel_path)
    pers_src = _personnel(inp, teams, games, intel, now)
    coaches = [{"first": c.get("firstName"), "last": c.get("lastName"),
                "seasons": [{"school": s.get("school"), "year": s.get("year"), "wins": s.get("wins"),
                             "losses": s.get("losses")} for s in c.get("seasons") or []]}
               for c in ds.get("coaches") or []]
    import cfp_rules
    season = max((t.get("season") or 0 for t in teams.values()), default=0)
    fetched = ds.get("_fetched") or {}
    careers = _coach_careers(ds, season, _iso(fetched.get("coaches_all") or fetched.get("coaches")) or _iso(now))
    for c in careers.values():
        tm = teams.get(c["school"])
        if c["current"] and tm is not None and tm.get("coach") == c["name"]:
            tm["coach_career"] = c["name"]
            tm["facts"].append(_fact("coach", tm["name"], "career", c["name"], c["text"], "CFBD coaches", c["as_of"]))
    conf_games = _conf_games(ds)
    try:
        rules = cfp_rules.load()
    except (OSError, ValueError):
        rules = {}
    store = {"v": VERSION, "built_ts": now, "built_at": _iso(now),
             "sources": {"cfbd": (ds.get("_fetched") or {}).get("lines"), "slate_intel": intel.get("as_of"),
                         "polls": polls_d.get("_fetched_ts"), "weather": _json(inp.weather_path).get("fetched_at"),
                         "market": max((r[-1]["ts"] for r in snaps.values() if r), default=None),
                         "personnel": pers_src},
             "rankings": {k: polls_d[k] for k in ("CFP", "AP", "Coaches") if (polls_d.get(k) or {}).get("ok")},
             "coaching": intel.get("coaching") or [], "coaching_fetched": intel.get("fetched_at"), "coaches": coaches,
             "coach_careers": careers,
             "coach_news": _coach_news(_json(inp.news_path), _json(inp.wire_path), now),
             "conf_games": conf_games,
             "conf_races": _conf_races(conf_games, now, _iso(fetched.get("lines")) or _iso(now)),
             "cfp_rules": rules if not cfp_rules.validate(rules) else {},
             "games": games, "teams": teams}
    store["build_ms"] = int((time.time() - t) * 1000)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(store, separators=(",", ":"), default=str))
    os.replace(tmp, out)
    pushed = 0
    if push:
        try:
            pushed = _push(store, out, None if push == "default" else push)
        except Exception as e:  # noqa: BLE001 - the mirror never blocks the local store
            log.warning("factstore: supabase mirror failed: %r", e)
    return {"games": len(games), "teams": len(teams), "ms": int((time.time() - t) * 1000), "pushed": pushed}


META_PARTS = ("coach_careers", "coach_news", "conf_games", "conf_races")  # own meta rows: they change rarely


def _push(store: dict, out: Path, pusher=None) -> int:
    """Mirror changed sheets to Supabase picky.sheet (hashes kept next to the local file)."""
    if pusher is None:
        import market_snapshot as M
        pusher = lambda rows: M.supabase_push("sheet", rows)  # noqa: E731
    hp = out.with_name(out.name + ".hashes")
    old = _json(hp)
    meta = {k: v for k, v in store.items() if k not in ("games", "teams", "built_ts", "built_at", "build_ms") + META_PARTS}
    rows, new = [], {}
    for kind, key, body in ([("game", k, v) for k, v in store["games"].items()]
                            + [("team", k, v) for k, v in store["teams"].items()] + [("meta", "meta", meta)]
                            + [("meta", k, store[k]) for k in META_PARTS if k in store]):
        h = _hash(body)
        new[f"{kind}:{key}"] = h
        if old.get(f"{kind}:{key}") != h:
            rows.append({"kind": kind, "key": key, "body": body})
    for i in range(0, len(rows), 200):
        pusher(rows[i:i + 200])
    hp.write_text(json.dumps(new))
    return len(rows)


def supabase_pull(kind: str) -> list[dict]:
    import httpx
    import market_snapshot as M
    key = M._service_key()
    r = httpx.post(f"{M.SUPABASE_URL}/rest/v1/rpc/picky_pull", json={"kind": kind}, timeout=20,
                   headers={"apikey": key, "Authorization": f"Bearer {key}"})
    r.raise_for_status()
    return r.json() or []


# =============================================================================================================
# readers (local cache first; Supabase copy on a cold start)
# =============================================================================================================

_memo: dict = {}
_override: dict = {"path": None, "inputs": None, "now": None}


def use(path: Path | None, inp: Inputs | None = None, now: float | None = None) -> None:
    """Point the readers at another store (tests, replays). use(None) resets to the live cache."""
    _override.update(path=Path(path) if path else None, inputs=inp, now=now)
    _memo.clear()


def _now() -> float:
    return _override["now"] or time.time()


def load(path: Path | None = None, puller=None) -> dict | None:
    p = Path(path or _override["path"] or CACHE_PATH)
    try:
        st = p.stat()
        hit = _memo.get(str(p))
        if hit and hit[0] == st.st_mtime_ns:
            return hit[1]
        d = json.loads(p.read_text())
        _memo[str(p)] = (st.st_mtime_ns, d)
        return d
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as e:
        log.warning("factstore: unreadable %s: %r", p, e)
    if puller is None and p != CACHE_PATH:
        return None
    try:
        rows = (puller or supabase_pull)("sheet")
    except Exception as e:  # noqa: BLE001
        log.warning("factstore: no local store and no Supabase copy: %r", e)
        return None
    meta = next((r["body"] for r in rows if r.get("kind") == "meta" and r.get("key") == "meta"), None)
    if not meta:
        return None
    parts = {r["key"]: r["body"] for r in rows if r.get("kind") == "meta" and r.get("key") in META_PARTS}
    d = {**meta, **parts, "games": {r["key"]: r["body"] for r in rows if r.get("kind") == "game"},
         "teams": {r["key"]: r["body"] for r in rows if r.get("kind") == "team"}, "from": "supabase"}
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(d, separators=(",", ":"), default=str))
    os.replace(tmp, p)
    return d


def _live() -> dict:
    inp = _override["inputs"]
    p = inp.live_path if inp else Path(os.environ.get("PICKY_LIVE_CACHE", HERE / "cache" / "live_games.json"))
    return _json(p)


def overlay_team(t: dict, store: dict, live: dict | None = None) -> dict:
    """A team sheet with today's finals from the live poller added (CFBD lags until its next refresh)."""
    live = live if live is not None else _live()
    finals = [g for g in (live.get("games") or {}).values() if g.get("state") == "post"
              and g.get("away_score") is not None and g.get("home_score") is not None]
    if not finals:
        return t
    counted = {r["game_id"] for r in t.get("results") or []}
    w, l = map(int, (t.get("record") or "0-0").split("-")[:2])
    cw, cl = map(int, (t.get("conf_record") or "0-0").split("-")[:2])
    changed = False
    for g in finals:
        gid = str(g.get("id"))
        if gid in counted:
            continue
        names = {cfbd.norm((g.get("away") or {}).get("name")): "away", cfbd.norm((g.get("home") or {}).get("name")): "home"}
        side = names.get(cfbd.norm(t["name"]))
        if not side:
            continue
        mine = g[f"{side}_score"]
        theirs = g["home_score" if side == "away" else "away_score"]
        won = mine > theirs
        w, l = (w + 1, l) if won else (w, l + 1)
        conf_game = ((store.get("games") or {}).get(gid) or {}).get("conf_game")
        if conf_game:
            cw, cl = (cw + 1, cl) if won else (cw, cl + 1)
        changed = True
    if not changed:
        return t
    return {**t, "record": f"{w}-{l}", "conf_record": f"{cw}-{cl}", "record_note": "incl. today's finals"}


# =============================================================================================================
# the lane gateway (router.STORE_LANES call only these for football data)
# =============================================================================================================

norm = cfbd.norm
KEY_NUMBER_FACT = {"id": "rule.spread.key_numbers", "kind": "rule", "subject": "spread", "value": "3, 7, 10, 14, 17, 21",
                   "text": "key numbers 3, 7, 10, 14, 17, 21 (the most common final margins)", "source": "football scoring",
                   "as_of": "static"}


def store() -> dict | None:
    return load()


def team(name: str) -> dict | None:
    st = load()
    if not st or not name:
        return None
    t = st["teams"].get(name)
    if not t:
        k = cfbd.ALIASES.get(cfbd.norm(name), cfbd.norm(name))
        t = next((x for x in st["teams"].values() if cfbd.norm(x["name"]) == k), None)
    return overlay_team(t, st) if t else None


def teams_idx() -> dict:
    """{'teams': {norm name: team sheet}}: the shape cfbd.other_teams_named / resolve_team read."""
    st = load() or {}
    return {"teams": {cfbd.norm(k): v for k, v in (st.get("teams") or {}).items()}}


def resolve(name: str) -> dict | None:
    return cfbd.resolve_team(name, teams_idx()) if name else None


def teams_named(q: str) -> list[str]:
    """FBS teams a text names, in the order it names them. cfbd.other_teams_named skips one-word names under 4
    letters (stopword guard), so the 3-letter schools (LSU, TCU, BYU, USC, SMU, UCF, UAB) and short aliases (ulm,
    fiu) are matched here as whole words."""
    idx = teams_idx()
    if not idx["teams"]:
        return []
    out = cfbd.other_teams_named(q or "", [], idx)
    nq = cfbd.norm(re.sub(r"(\w)['’]s\b", r"\1", q or ""))
    short = {k: k for k in idx["teams"] if len(k) < 4}
    short.update({a: cfbd.norm(t) for a, t in cfbd.ALIASES.items() if len(a) < 4 and cfbd.norm(t) in idx["teams"]})
    hits = []
    for a, key in short.items():
        m = re.search(rf"(?<!\w){re.escape(a)}(?!\w)", nq)
        name = idx["teams"][key]["name"]
        if m and name not in out:
            hits.append((m.start(), name))
    if not hits:
        return out
    pos = {n: (nq.find(cfbd.norm(n)) if cfbd.norm(n) in nq else 10 ** 6) for n in out}
    pos.update({n: p for p, n in hits})
    return sorted(out + [n for _, n in hits], key=lambda n: pos[n])


def teams_in(q: str) -> list[str]:
    """teams_named plus short school names it skips (LSU, BYU, TCU, SMU, UCF ...), as written in caps, exact only,
    in the order the text names them."""
    named = teams_named(q)
    idx = teams_idx()["teams"]
    text = re.sub(r"(\w)['’]s\b", r"\1", q or "")
    pos = {}
    for t in named:
        m = re.search(rf"\b{re.escape(t)}\b", text, re.I)
        pos[t] = m.start() if m else len(text)
    for m in re.finditer(r"\b[A-Z][A-Za-z&]{1,3}\b", text):
        k = cfbd.ALIASES.get(cfbd.norm(m.group()), cfbd.norm(m.group()))
        t = idx.get(k)
        if t and t["name"] not in pos:
            pos[t["name"]] = m.start()
    return sorted(pos, key=lambda t: pos[t])


def _row(sheet: dict, now: float | None = None) -> dict:
    """A game sheet in the CFBD lines-row shape slate_cards' helpers read, with the store's market as 'lines'."""
    m = market_for(sheet, now)
    ln = {"provider": m.get("source"), "spread": m.get("spread_home"), "spreadOpen": m.get("open_spread_home"),
          "overUnder": m.get("total"), "overUnderOpen": m.get("open_total")}
    gid = int(sheet["game_id"]) if str(sheet["game_id"]).isdigit() else sheet["game_id"]
    return {"id": gid, "season": sheet.get("season"), "week": sheet.get("week"), "startDate": sheet.get("start"),
            "awayTeam": sheet["away"], "homeTeam": sheet["home"], "homeScore": None, "awayScore": None,
            "lines": [ln] if ln["spread"] is not None else [], "sheet": sheet}


def market_for(sheet: dict, now: float | None = None) -> dict:
    """The sheet's market, re-checked for age at read time (an ESPN snapshot past its hard age -> CFBD)."""
    m = sheet.get("market") or {}
    if str(m.get("source", "")).startswith("ESPN") and m.get("as_of_ts"):
        now = now or _now()
        if now - m["as_of_ts"] > MARKET_HARD_S[1 if _game_day(now) else 0]:
            return sheet.get("market_cfbd") or {}
    return m


def upcoming(now: float | None = None) -> list[dict]:
    """This week's not-yet-kicked games (next 9 days) as rows, soonest first."""
    st = load() or {}
    now = now or _now()
    t0 = dt.datetime.fromtimestamp(now, dt.timezone.utc)
    rows = []
    for s in (st.get("games") or {}).values():
        k = _sd(s.get("start"))
        if k and t0 <= k <= t0 + dt.timedelta(days=HORIZON_D):
            rows.append(_row(s, now))
    wks = [r["week"] for r in rows if isinstance(r.get("week"), int)]
    if wks:  # the 9-day horizon reaches into next week by midweek: this week = the soonest week with games left
        rows = [r for r in rows if r.get("week") in (None, min(wks))]
    return sorted(rows, key=lambda r: r["startDate"] or "")


def notable(rows: list[dict], n: int = 12) -> list[dict]:
    """Most notable games by combined SP+ rating (team sheets)."""
    def sp(name):
        return ((team(name) or {}).get("sp") or {}).get("rating") or -30
    return sorted(rows, key=lambda g: sp(g["homeTeam"]) + sp(g["awayTeam"]), reverse=True)[:n]


def find_game(q: str, ctx: dict) -> tuple[dict | None, dict | None]:
    """(game row, pool game or None) for the game a text is about: a pool game named the way chat names them,
    else a game between two teams it names, else the first named team's game."""
    import chat
    import slate_cards
    rows = upcoming()
    pool = {int(g["id"]): g for g in (ctx or {}).get("games_this_week") or [] if str(g.get("id", "")).isdigit()}
    hit = chat.games_named(q or "", [{"Away": g["away"], "Home": g["home"], "id": g["id"]}
                                     for g in (ctx or {}).get("games_this_week") or [] if not g.get("tiebreaker")])
    if hit and str(hit[0]["id"]).isdigit():
        gid = int(hit[0]["id"])
        pg = pool.get(gid)
        row = next((x for x in rows if x["id"] == gid), None) or {
            "id": gid, "awayTeam": pg["away"], "homeTeam": pg["home"], "startDate": None, "lines": []}
        return row, pg
    named = teams_named(q)
    ms = slate_cards.match_games(named, rows, set()) or (slate_cards.match_games(named[:1], rows, set()) if named else [])
    return (ms[0], pool.get(ms[0]["id"])) if ms else (None, None)


def market(row: dict) -> dict:
    """{spread, spreadOpen, overUnder, overUnderOpen, move, total_move, source, as_of} for a game row."""
    sheet = row.get("sheet") or ((load() or {}).get("games") or {}).get(str(row.get("id"))) or {}
    if not sheet:
        ln = _cfbd_line(row) or {}
        return {"spread": ln.get("spread"), "spreadOpen": ln.get("spreadOpen"), "overUnder": ln.get("overUnder"),
                "overUnderOpen": ln.get("overUnderOpen"), "move": None, "total_move": None, "source": "CFBD",
                "as_of": ""}
    m = market_for(sheet)
    return {"spread": m.get("spread_home"), "spreadOpen": m.get("open_spread_home"), "overUnder": m.get("total"),
            "overUnderOpen": m.get("open_total"), "move": m.get("move"), "total_move": m.get("total_move"),
            "source": m.get("source"), "as_of": m.get("as_of"), "early_open": m.get("early_open")}


def facts(row: dict, kinds: tuple[str, ...] | None = None) -> list[dict]:
    sheet = row.get("sheet") or ((load() or {}).get("games") or {}).get(str(row.get("id"))) or {}
    return [f for f in sheet.get("facts") or [] if not kinds or f["kind"] in kinds]


def availability(away: str, home: str) -> tuple[list[dict], str | None]:
    """The sourced availability notes for a game (raw slate_intel shape: text/status/players/team side)."""
    st = load() or {}
    s = next((g for g in (st.get("games") or {}).values() if {g["away"], g["home"]} == {away, home}), None)
    return ((s or {}).get("intel_facts") or []), (s or {}).get("intel_as_of")


def all_availability() -> list[tuple[dict, dict]]:
    """(game sheet, raw note) for every note this week."""
    st = load() or {}
    return [(g, f) for g in (st.get("games") or {}).values() for f in g.get("intel_facts") or []]


# --- personnel gateway (plan phase 4) -------------------------------------------------------------------------

CAUSE_STATUSES = ("out", "season", "doubtful", "questionable", "TBD", "suspended", "benched", "portal", "opt-out")
OUT_STATUSES = ("out", "season", "doubtful", "suspended", "portal", "opt-out", "benched")
STATUS_PHRASE = {"out": "out", "season": "out for the rest of the regular season", "doubtful": "doubtful", "questionable": "questionable",
                 "probable": "probable", "TBD": "TBD (no call made yet)", "available": "good to go",
                 "benched": "benched", "suspended": "suspended", "portal": "in the transfer portal",
                 "opt-out": "opting out"}
_fallback: dict = {}


def has_personnel() -> bool:
    return any(t.get("qbs") for t in ((load() or {}).get("teams") or {}).values())


def _status_rows() -> list[dict]:
    import personnel as PS
    st = load() or {}
    rows = [r for t in (st.get("teams") or {}).values() for r in t.get("player_status") or []]
    if rows or any(t.get("qbs") for t in (st.get("teams") or {}).values()):
        return rows
    key = (id(st), st.get("built_ts"))  # an old store (no personnel yet): normalize its sourced intel notes
    if key not in _fallback:
        intel = {"games": {gid: {"away": g["away"], "home": g["home"], "facts": g.get("intel_facts") or []}
                           for gid, g in (st.get("games") or {}).items()}}
        _fallback.clear()
        _fallback[key] = PS.normalize(intel=intel, now=_now())
    return _fallback[key]


def _ct_day(iso: str | None) -> str:
    """'2026-11-21' for a report's UTC time, in Central time (game dates in the QB ledger are CT dates)."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    try:
        d = datetime.fromisoformat((iso or "").replace("Z", "+00:00"))
    except ValueError:
        return ""
    return (d.astimezone(ZoneInfo("America/Chicago")) if d.tzinfo else d).date().isoformat()


def _started_since(r: dict) -> bool:
    """A QB out-type row (out, doubtful, benched ...) the player has since contradicted by starting a game after the
    report's day. Same-day reports stay (an in-game injury is reported the day he started); questionable/TBD stay
    (they read with their day, 'last report Mon 11/2')."""
    if r.get("status") not in OUT_STATUSES:
        return False
    me = next((x for x in qb_room(r.get("team") or "") if cfbd.norm(x["name"]) == cfbd.norm(r["player"])), None)
    day = _ct_day(r.get("reported_at"))
    return bool(me and me.get("last_start") and day and me["last_start"] > day)


def qb_missed(name: str, qb: dict) -> tuple[int, dict | None]:
    """(starts by the team's other QBs after this QB's last start, the QB who made the latest of them). A QB with
    starts who sat the last 2+ while someone else started them is sidelined (hurt or benched; the data can't say
    which), so he's never 'the backup' (genius w13: Nico Iamaleava, out for the season since Oct 10, was named
    UCLA's backup; Sayin, out since Oct 17, read as Ohio State's)."""
    if not qb.get("last_start"):
        return 0, None
    later = [(gm["date"], x) for x in qb_room(name) if x is not qb and cfbd.norm(x["name"]) != cfbd.norm(qb["name"])
             for gm in x.get("start_games") or [] if gm.get("date", "") > qb["last_start"]]
    if not later:
        return 0, None
    return len(later), max(later, key=lambda t: t[0])[1]


def sidelined(name: str) -> set[str]:
    """Normalized names of a team's QBs who missed the last 2+ starts, never took a snap outside their own starts
    (a benched starter who still plays in relief, Tennessee's Faizon Brandon, is the real backup) and have no fresh
    (<= 7 days) report clearing them."""
    import personnel as PS
    fresh_ok = {PS._norm(r["player"]) for r in player_status(team=name)
                if r["age_days"] <= 7 and r["status"] in ("available", "probable")}
    return {cfbd.norm(x["name"]) for x in qb_room(name)
            if qb_missed(name, x)[0] >= 2 and ((x.get("line") or {}).get("games") or 0) <= (x.get("starts") or 0)
            and PS._norm(x["name"]) not in fresh_ok}


def player_status(team: str | None = None, player: str | None = None, game: dict | None = None) -> list[dict]:
    """Normalized availability rows (latest report per player), newest first, each with read-time wording:
    when ('Monday' / 'last report Mon 11/2'), stale (> 7 days) and age_days."""
    import personnel as PS
    teams_ = None
    if game:
        teams_ = {cfbd.norm(game.get("awayTeam") or game.get("away") or ""),
                  cfbd.norm(game.get("homeTeam") or game.get("home") or "")}
    now = _now()
    out = []
    for r in _status_rows():
        if team and cfbd.norm(r["team"]) != cfbd.norm(team):
            continue
        if teams_ and cfbd.norm(r["team"]) not in teams_:
            continue
        if player and cfbd.norm(r["player"]) != cfbd.norm(player):
            continue
        if _started_since(r):
            continue
        c = dict(r)
        c["when"], c["stale"] = PS.report_when(r.get("reported_at"), now)
        c["age_days"] = (now - PS._ts(r.get("reported_at"))) / 86400 if r.get("reported_at") else 99.0
        out.append(c)
    return sorted(out, key=lambda r: r["age_days"])


def status_fact(r: dict) -> dict:
    """The availability fact for one player_status row, with its read-time day wording."""
    return {"id": f"availability.{_slug(r['player'])}.{_slug(r['status'])}", "kind": "availability",
            "subject": r["player"], "value": r["status"],
            "text": f"{r['player']} ({r['team']}) {r['status']}, per {r.get('source_name')} ({r.get('when', '')}): "
                    f"{r.get('quote')}", "source": r.get("source_name") or "news", "as_of": r.get("reported_at") or ""}


def qb_room(name: str) -> list[dict]:
    """A team's QBs: starts, SU/ATS as starter, season line, class (most starts first)."""
    return (team(name) or {}).get("qbs") or []


def qb_starter(name: str) -> str | None:
    return (team(name) or {}).get("qb_starter")


def backup_for(room: list[dict], player: str, exclude=frozenset(), team: str | None = None) -> dict | None:
    """The next QB up; with `team`, a QB a fresh (<= 7 days) report names as the new starter comes first."""
    import personnel as PS
    starting = {PS._norm(r["player"]) for r in player_status(team=team)
                if r.get("detail") == "starting" and r["age_days"] <= 7} if team else set()
    return PS.backup_for(room, player, {cfbd.norm(x) for x in exclude}, starting)


def qb_drop(name: str, starter: str, backup: str | None = None) -> float:
    """Points from a team's QB to his backup on the position table (rating gap; unknown backup = the default)."""
    import personnel as PS
    room = qb_room(name)
    me = next((x for x in room if cfbd.norm(x["name"]) == cfbd.norm(starter)), None) or {}
    b = next((x for x in room if backup and cfbd.norm(x["name"]) == cfbd.norm(backup)), None) or \
        PS.backup_for(room, starter) or {}
    return PS.qb_value(me.get("rating"), b.get("rating"), (b.get("line") or {}).get("att") or 0)


def team_facts(name: str, kinds: tuple[str, ...], subject: str | None = None) -> list[dict]:
    t = team(name) or {}
    return [f for f in t.get("facts") or [] if f["kind"] in kinds and (not subject or f["subject"] == subject)]


_LOWER_OK = re.compile(r"\b(?:is|on|about|hows|how'?s|for|has|does|did|was)\s+$")


def find_player(q: str, teams: list[str] | None = None) -> tuple[str, str] | None:
    """(player, team) a text names: a QB room or availability name, full name anywhere, a last name only when
    it's capitalized or reads as one ('is chiles playing'). Ambiguous last names prefer the named teams."""
    import personnel as PS
    st = load() or {}
    cands = set()
    for t, s in (st.get("teams") or {}).items():
        cands |= {(q_["name"], t) for q_ in s.get("qbs") or []}
    cands |= {(r["player"], r["team"]) for r in _status_rows()}
    text = (q or "").replace("’", "'")
    low = text.lower()
    tn = {cfbd.norm(t) for t in teams or []}
    full = [c for c in cands if PS._word_at(low, c[0].lower())]
    if full:
        return sorted(full, key=lambda c: (cfbd.norm(c[1]) not in tn, -len(c[0])))[0]
    hits = []
    for n, t in cands:
        last = re.sub(r",? (Jr\.?|Sr\.?|II|III|IV)$", "", n).split()[-1]
        if len(last) < 4:
            continue
        if PS._word_at(text, last) or (PS._word_at(low, last.lower())
                                       and _LOWER_OK.search(low[:low.find(last.lower())])):
            hits.append((n, t))
    if tn:
        mine = [h for h in hits if cfbd.norm(h[1]) in tn]
        hits = mine or hits
    return hits[0] if len(set(hits)) == 1 else None


def position_value(pos: str, role: str | None = None) -> float:
    import personnel as PS
    return PS.player_value(pos, role)


def position_values() -> dict:
    import personnel as PS
    return PS.position_values()


def value_fact(subject: str, pts: float, why: str) -> dict:
    return {"id": f"value.{_slug(subject)}", "kind": "value", "subject": subject, "value": pts,
            "text": f"{subject}: about {pts:g} points of spread ({why})", "source": "position_value.json",
            "as_of": "static"}


def sp_unit(name: str, side: str) -> tuple[int | None, dict | None]:
    """(SP+ offense or defense rank, its fact) for a team."""
    t = team(name) or {}
    rk = (t.get("sp") or {}).get("off_rank" if side == "offense" else "def_rank")
    if not rk:
        return None, None
    return rk, _fact("sp", name, f"{side[:3]}_rank", rk, f"{name}'s {side} is No. {rk} in SP+", "CFBD SP+",
                     t.get("as_of") or "")


def coaching() -> list[dict]:
    return (load() or {}).get("coaching") or []


def coaches() -> list[dict]:
    """CFBD coaches as {firstName, lastName, seasons[{school, year}]} (the shape the old reader returned)."""
    return [{"firstName": c["first"], "lastName": c["last"], "seasons": c["seasons"]}
            for c in (load() or {}).get("coaches") or []]


def last_meeting(row: dict) -> str | None:
    sheet = row.get("sheet") or ((load() or {}).get("games") or {}).get(str(row.get("id"))) or {}
    return meeting_text(sheet.get("meeting"))


def team_rows(name: str, before: str = "9999") -> list[dict]:
    """A team's results vs the number (slate_cards.team_rows shape: opp, home, pts (mine, theirs), spread, cover)."""
    t = team(name) or {}
    return [{"opp": r["opp"], "home": r["home"], "pts": (r["pts"], r["opp_pts"]), "spread": r["spread"],
             "cover": r["cover"], "date": r["date"]} for r in t.get("results") or [] if r["date"] < before]


def rec(rows: list[dict]) -> str:
    c = sum(1 for r in rows if r["cover"] is not None and r["cover"] > 0)
    l_ = sum(1 for r in rows if r["cover"] is not None and r["cover"] < 0)
    p = sum(1 for r in rows if r["cover"] == 0)
    return f"{c}-{l_}" + (f"-{p}" if p else "")


def rankings(poll: str = "CFP", path: Path | None = None) -> dict | None:
    """A poll that passed polls.sanity (ESPN primary), from the store; the polls cache if the store lacks it."""
    st = load(path) if path else load()
    p = ((st or {}).get("rankings") or {}).get(poll)
    if p and p.get("ok"):
        return p
    try:
        import polls
        return polls.serve(poll)
    except Exception:  # noqa: BLE001
        return None


def index() -> dict | None:
    """The normalized CFBD tables (team games, stats, players) for computation modules (pick_brief, lookahead)."""
    return cfbd.load_index()


def lookahead(row: dict) -> str | None:
    import slate_cards
    idx = index()
    return slate_cards.lookahead_line(row, idx) if idx else None


def brief(row: dict, notes: dict | None = None) -> dict | None:
    """pick_brief's factors for a game against its market line (slate_cards.brief_for): the case ledger's input."""
    import slate_cards
    idx = index()
    return slate_cards.brief_for(row, idx, notes) if idx else None


def lean(row: dict, q: str = "") -> list[str]:
    import slate_cards
    idx = index()
    return slate_cards.lean_lines(row, idx, None, ask=q) if idx else []


def news_lines(team_name: str, query: str, limit: int = 3) -> list[str]:
    """Breaking-news lines for a team (news.py's cache, the store's news feed)."""
    import news
    return news.breaking_for(query, teams=[team_name], limit=limit) or []



# --- big picture gateway (plan phase 5) ---------------------------------------------------------------------

def conference_named(q: str) -> str | None:
    """The conference a text names ('big 12' -> 'Big 12'), CFBD's name."""
    import slate_cards
    t = (q or "").lower()
    return next((v for k, v in sorted(slate_cards.CONFS.items(), key=lambda x: -len(x[0]))
                 if re.search(rf"\b{re.escape(k)}\b", t)), None)


def conf_race(conf: str) -> dict | None:
    """A conference's title race (title_race.race) with today's finals from the live poller applied."""
    import title_race
    st = load() or {}
    games = (st.get("conf_games") or {}).get(conf)
    if not games:
        return None
    finals = {}
    for g in ((_live().get("games")) or {}).values():
        if g.get("state") == "post" and g.get("away_score") is not None and g.get("home_score") is not None:
            finals[str(g.get("id"))] = g
            finals[(cfbd.norm((g.get("away") or {}).get("name")), cfbd.norm((g.get("home") or {}).get("name")))] = g
    out = []
    for g in games:
        lg = None if g["home_pts"] is not None else (finals.get(g["id"]) or finals.get((cfbd.norm(g["away"]),
                                                                                       cfbd.norm(g["home"]))))
        if lg:
            g = dict(g, home_pts=lg["home_score"], away_pts=lg["away_score"])
        out.append(g)
    now = _now()  # always recomputed (~5 ms): the stored race is only as new as the code that built the store
    return title_race.race(conf, out, dt.datetime.fromtimestamp(now, dt.timezone.utc), as_of=_iso(now),
                           source="CFBD games + live finals")


def coach_careers() -> dict[str, dict]:
    """{full name: career} for every head coach active this season or last (CFBD /coaches, every season)."""
    return (load() or {}).get("coach_careers") or {}


def coaching_notes() -> list[dict]:
    """slate_intel's sourced coaching notes (Opus-made), only while that file is fresh; each with its fact."""
    st = load() or {}
    f = _sd(st.get("coaching_fetched"))
    if not f or _now() - f.timestamp() > COACHING_FRESH_D * 86400:
        return []
    return [dict(c, fact=_fact("coaching", c.get("school") or "", "change", c.get("text"), c.get("text"),
                               c.get("source_name") or "slate_intel", c.get("date") or ""))
            for c in st.get("coaching") or [] if c.get("text")]


def coach_news() -> list[dict]:
    """Coach headlines (news.py + the insider wire), newest first, each with its fact."""
    return (load() or {}).get("coach_news") or []


def cfp_rules() -> dict:
    """The committed CFP format (cfp_rules.json, validated at build)."""
    return (load() or {}).get("cfp_rules") or {}


def bcs_bracket() -> dict | None:
    """Picky's own CFP bracket (bcs.py: BCS-style rankings + cfp_rules.json): {text, facts, week, ...} or None."""
    import bcs
    return bcs.current()


# =============================================================================================================
# the facts_used contract
# =============================================================================================================

class Answer(str):
    """A lane's text plus the facts it used. Still a plain str for every caller."""
    facts_used: list

    def __new__(cls, text: str, facts_used: list | None = None):
        o = super().__new__(cls, text)
        o.facts_used = list(facts_used or [])
        return o


def answer(text: str | None, facts_used: list | None) -> Answer | None:
    return Answer(text, facts_used) if text is not None else None


_NUM = re.compile(r"(?<![\w.])[+-]?\d+(?:\.\d+)?(?:-\d+(?:-\d+)?)?(?![\w])")
_GENERAL = {"0", "1", "2", "3", "7", "0.5"}  # the 3 and 7 key-number talk


def _nums(s: str) -> set[str]:
    out = set()
    for m in _NUM.findall(str(s)):
        out.add(m.lstrip("+"))
        if "-" not in m[1:]:
            out.add(m.lstrip("+-"))
    return out


def unsupported(text: str, facts_used: list | None, extra=()) -> list[str]:
    """Numbers (and W-L records) in text that no fact in facts_used (or extra) carries."""
    ok = set(_GENERAL)
    for f in facts_used or []:
        ok |= _nums(f.get("text", "")) | _nums(f.get("value", ""))
    for e in extra or ():
        ok |= _nums(e)
    bad = []
    for m in _NUM.findall(text or ""):
        k = m.lstrip("+")
        if k not in ok and k.lstrip("-") not in ok and k not in bad:
            bad.append(k)
    return bad


_STAT = re.compile(r"\d+-of-\d+|\b\d[\d,]* (?:passing |rushing |receiving )?(?:yards?|yds|TDs?|INTs?|touchdowns?|"
                   r"interceptions?|picks|sacks?|tackles?|carries|rushes|receptions?|catches|completions?)\b", re.I)
_SENT = re.compile(r"(?<=[.!?])\s+(?=[A-Z])")


def drop_unsourced_stats(a: "Answer") -> "Answer":
    """A store answer minus every sentence that states a stat (X-of-Y, N yards / TD / INT / sacks ...) with a
    number no fact in its facts_used carries: a stat goes out with its source or not at all."""
    used = getattr(a, "facts_used", None) or []
    keep = [x for x in _SENT.split(str(a)) if not (_STAT.search(x) and unsupported(x, used))]
    out = " ".join(keep)
    if out != str(a):
        log.info("factstore: dropped unsourced stat sentence(s) from %r", str(a)[:120])
    return Answer(out, used)


def old_news(r: dict) -> bool:
    """An availability row the market has had: he's missed 2+ games in a row, or the report came out before his
    team's last game (that game and this week's opener both had it). Rows from a store without those fields fall
    back to the report's age (FRESH_D) and the box-score 'already out' flag."""
    if "last_game" not in r:
        return r.get("age_days", 99) > FRESH_D or bool(r.get("already_out"))
    if (r.get("missed") or 0) >= 2:
        return True
    rep, last = str(r.get("reported_at") or "")[:10], r.get("last_game")
    if rep and last:
        try:
            import personnel as PS
            day = dt.datetime.fromtimestamp(PS._ts(r["reported_at"]), PS.CT).date().isoformat()
        except Exception:  # noqa: BLE001
            day = rep
        return day < last
    return r.get("age_days", 99) > FRESH_D


# =============================================================================================================
# CLI
# =============================================================================================================

def refresh() -> dict:
    """The tick: polls (1 h TTL, mirrored to picky.ranking), a couple of due ESPN rosters (weekly TTL, free), team
    looks for picture prompts (30-day TTL, free), then the store build."""
    import personnel
    import polls
    try:
        polls.refresh(pusher="default")
    except Exception as e:  # noqa: BLE001
        log.warning("factstore: polls refresh failed: %r", e)
    try:
        personnel.tick()
    except Exception as e:  # noqa: BLE001 - rosters never block the store
        log.warning("factstore: roster tick failed: %r", e)
    try:
        import picture_prompt
        picture_prompt.refresh()  # team mascot + colors for picture prompts (30-day TTL, free)
    except Exception as e:  # noqa: BLE001
        log.warning("factstore: team looks refresh failed: %r", e)
    out = build()
    try:
        import bcs
        bcs.refresh()  # Picky's BCS bracket: new polls / finals land in cache/bcs/<season>-w<week>.json
    except Exception as e:  # noqa: BLE001
        log.warning("factstore: bcs bracket refresh failed: %r", e)
    return out


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    cmd = argv[0] if argv else "build"
    if cmd == "build":
        print(json.dumps(build()))
    elif cmd == "refresh":
        print(json.dumps(refresh()))
    elif cmd == "show" and len(argv) > 1:
        st = load() or {}
        key = " ".join(argv[1:])
        print(json.dumps(st.get("games", {}).get(key) or team(key), indent=1, default=str)[:6000])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
