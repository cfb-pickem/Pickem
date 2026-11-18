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
    _ds: dict = field(default_factory=dict, repr=False)

    @classmethod
    def live(cls) -> "Inputs":
        import market_snapshot
        import polls
        return cls(cfbd_db=cfbd.DB_PATH, slate_intel_path=HERE / "cache" / "slate_intel.json",
                   weather_path=HERE / "cache" / "weather.json", polls_path=polls.PATH,
                   market_db=market_snapshot.DB_PATH,
                   live_path=Path(os.environ.get("PICKY_LIVE_CACHE", HERE / "cache" / "live_games.json")),
                   news_path=Path(os.environ.get("PICKY_NEWS_CACHE", HERE / "cache" / "news.json")),
                   wire_path=Path(os.environ.get("PICKY_INSIDER_WIRE",
                                                 "/Users/Agent/cfb-pickem-private/insider_wire.json")))

    @classmethod
    def from_fixture(cls, d: Path, **kw) -> "Inputs":
        d = Path(d)
        kw.setdefault("news_path", d / "news.json" if (d / "news.json").exists() else None)
        kw.setdefault("wire_path", d / "insider_wire.json" if (d / "insider_wire.json").exists() else None)
        return cls(fixture_dir=d, slate_intel_path=d / "slate_intel.json", weather_path=d / "weather.json", **kw)

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
        t["facts"] = f
        out[name] = t
    return out


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
    s = M.summary(str(g["id"]), rows=snaps) if snaps else None
    hard = MARKET_HARD_S[1 if _game_day(now) else 0]
    if s and s.get("spread_home") is not None and now - s["as_of_ts"] <= hard:
        es = {k: s.get(k) for k in ("spread_home", "open_spread_home", "total", "open_total", "move", "total_move",
                                     "ml_home", "ml_away", "n")}
        es.update(source=s["source"], as_of=_iso(s["as_of_ts"]), as_of_ts=s["as_of_ts"])
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
                         "market": max((r[-1]["ts"] for r in snaps.values() if r), default=None)},
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
    """FBS teams a text names (longest names first), plus 3-letter schools (BYU, LSU, TCU, SMU, UCF...) that the
    shared resolver skips as too short."""
    idx = teams_idx()
    if not idx["teams"]:
        return []
    out = cfbd.other_teams_named(q or "", [], idx)
    for w in re.findall(r"\b[A-Za-z]{3}\b", q or ""):
        t = idx["teams"].get(cfbd.norm(w))
        if t and t.get("sp") and cfbd.norm(t["name"]) == cfbd.norm(w) and t["name"] not in out:
            out.append(t["name"])
    return out


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
            "source": m.get("source"), "as_of": m.get("as_of")}


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
    out, changed = [], False
    for g in games:
        lg = None if g["home_pts"] is not None else (finals.get(g["id"]) or finals.get((cfbd.norm(g["away"]),
                                                                                       cfbd.norm(g["home"]))))
        if lg:
            g = dict(g, home_pts=lg["home_score"], away_pts=lg["away_score"])
            changed = True
        out.append(g)
    if not changed:
        return (st.get("conf_races") or {}).get(conf)
    now = _now()
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


# =============================================================================================================
# CLI
# =============================================================================================================

def refresh() -> dict:
    """The tick: polls (1 h TTL, mirrored to picky.ranking) then the store build."""
    import polls
    try:
        polls.refresh(pusher="default")
    except Exception as e:  # noqa: BLE001
        log.warning("factstore: polls refresh failed: %r", e)
    return build()


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
