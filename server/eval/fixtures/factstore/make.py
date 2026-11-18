#!/usr/bin/env python3
"""Freeze the fact-store fixture (public football data only) from today's caches.

  .venv/bin/python3 -I eval/fixtures/factstore/make.py

Writes cfbd.json.gz (trimmed CFBD datasets), slate_intel.json, weather.json, espn_rankings.json and
espn_scoreboard.json next to this file. Re-run only to refresh the fixture; tests pin NOW below."""
import gzip
import re
import json
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SERVER = HERE.parents[2]
CACHE = SERVER / "cache"
GAME_KEYS = ("id", "season", "week", "seasonType", "startDate", "completed", "neutralSite", "conferenceGame", "venueId",
             "venue", "homeId", "homeTeam", "homeConference", "homeClassification", "homePoints", "awayId", "awayTeam",
             "awayConference", "awayClassification", "awayPoints")


def _team(t):
    t = t or {}
    return {k: t.get(k) for k in ("id", "location", "name", "abbreviation", "displayName", "shortDisplayName")}


def _odds(o):
    def lines(v):
        return {side: {when: {k: x.get(k) for k in ("line", "odds")} for when, x in (sv or {}).items()
                       if isinstance(x, dict)} for side, sv in (v or {}).items() if isinstance(sv, dict)}
    return {"provider": {"name": (o.get("provider") or {}).get("name")}, "details": o.get("details"),
            "overUnder": o.get("overUnder"), "spread": o.get("spread"),
            "awayTeamOdds": {"favorite": (o.get("awayTeamOdds") or {}).get("favorite"),
                             "team": _team((o.get("awayTeamOdds") or {}).get("team"))},
            "homeTeamOdds": {"favorite": (o.get("homeTeamOdds") or {}).get("favorite"),
                             "team": _team((o.get("homeTeamOdds") or {}).get("team"))},
            "moneyline": lines(o.get("moneyline")), "pointSpread": lines(o.get("pointSpread")),
            "total": lines(o.get("total"))}


def coaches_all(ds: dict) -> list[dict]:
    """Full careers (CFBD /coaches since 1980), trimmed to coaches active this season or last."""
    season = max((r.get("year") or 0 for r in ds.get("records") or []), default=0)
    keep = ("school", "year", "games", "wins", "losses", "ties")
    return [{"firstName": c.get("firstName"), "lastName": c.get("lastName"), "hireDate": c.get("hireDate"),
             "seasons": [{k: s.get(k) for k in keep} for s in c.get("seasons") or []]}
            for c in ds.get("coaches_all") or [] if any((s.get("year") or 0) >= season - 1 for s in c.get("seasons") or [])]


def add_coaches_all() -> int:
    """Add only coaches_all to the frozen cfbd.json.gz (the rest of the fixture stays as frozen)."""
    db = sqlite3.connect(CACHE / "cfbd.sqlite")
    ds = {n: json.loads(j) for n, j in db.execute("select name, json from datasets where name in ('records', 'coaches_all')")}
    p = HERE / "cfbd.json.gz"
    out = json.loads(gzip.decompress(p.read_bytes()))
    out["coaches_all"] = coaches_all(ds)
    p.write_bytes(gzip.compress(json.dumps(out, separators=(",", ":")).encode(), 9))
    print(len(out["coaches_all"]), p.stat().st_size)
    return 0


NEWS_KEYS = ("title", "link", "source", "date", "teams", "insider", "author", "story", "label")
COACHY = re.compile(r"coach|fire[sd]?\b|firing|hot seat|interim|hire|carousel|search|candidate|\bjob\b|buyout|extension", re.I)


def add_news() -> int:
    """Freeze coach-related headlines (public news items) and the insider wire's carousel items for the coach lane."""
    items = json.loads((CACHE / "news.json").read_text()).get("items") or {}
    keep = {k: {f: v.get(f) for f in NEWS_KEYS} for k, v in items.items() if COACHY.search(v.get("title") or "")}
    (HERE / "news.json").write_text(json.dumps({"items": keep}, indent=0))
    wire = json.loads(Path("/Users/Agent/cfb-pickem-private/insider_wire.json").read_text())
    rows = [r for r in wire.get("items") or [] if "Coaching carousel" in (r.get("sections") or [])]
    (HERE / "insider_wire.json").write_text(json.dumps({"generated_at": wire.get("generated_at"), "items": rows}, indent=0))
    print(len(keep), len(rows))
    return 0


def main() -> int:
    db = sqlite3.connect(CACHE / "cfbd.sqlite")
    ds = {n: json.loads(j) for n, j in db.execute("select name, json from datasets")}
    fbs = lambda g: "fbs" in (g.get("homeClassification"), g.get("awayClassification"))  # noqa: E731
    out = {
        "games": [{k: g.get(k) for k in GAME_KEYS} for g in ds["games"] if fbs(g)],
        "lines": [dict(g, lines=[x for x in g["lines"] if x.get("provider") == "DraftKings"][:1] or g["lines"][:1])
                  for g in ds["lines"] if fbs(g)],
        "records": [r for r in ds["records"] if r.get("classification") == "fbs"],
        "ats": ds["ats"], "sp": ds["sp"], "srs": ds["srs"], "coaches": ds["coaches"],
        "coaches_all": coaches_all(ds),
    }
    pairs = {frozenset((g["homeTeam"], g["awayTeam"])) for g in ds["lines"] if g["week"] in (12, 13)}
    out["lines_prev"] = [g for g in ds["lines_prev"] if frozenset((g["homeTeam"], g["awayTeam"])) in pairs]
    (HERE / "cfbd.json.gz").write_bytes(gzip.compress(json.dumps(out, separators=(",", ":")).encode(), 9))
    for name in ("slate_intel.json", "weather.json"):
        (HERE / name).write_text((CACHE / name).read_text())
    for src, dst in ((sys.argv[1] if len(sys.argv) > 1 else "/tmp/rank.json", "espn_rankings.json"),):
        r = json.loads(Path(src).read_text())
        r = {"rankings": [{"name": x.get("name"), "shortName": x.get("shortName"), "type": x.get("type"),
                           "date": x.get("date"), "headline": x.get("headline"),
                           "ranks": [{"current": t.get("current"), "previous": t.get("previous"),
                                      "recordSummary": t.get("recordSummary"), "team": _team(t.get("team"))}
                                     for t in x.get("ranks") or []]}
                          for x in r["rankings"] if x.get("type") in ("cfp", "ap", "usa")]}
        (HERE / dst).write_text(json.dumps(r))
    sb = json.loads(Path(sys.argv[2] if len(sys.argv) > 2 else "/tmp/sb.json").read_text())
    evs = []
    for e in sb["events"]:
        c = e["competitions"][0]
        evs.append({"id": e["id"], "date": e.get("date"), "competitions": [{
            "date": c.get("date"), "status": {"type": {k: ((c.get("status") or {}).get("type") or {}).get(k)
                                                       for k in ("state", "name", "completed")}}, "odds": [
                _odds(o) for o in c.get("odds") or []][:1],
            "competitors": [{"homeAway": t.get("homeAway"), "score": t.get("score"), "team": _team(t.get("team")),
                             "curatedRank": t.get("curatedRank")} for t in c.get("competitors") or []]}]})
    (HERE / "espn_scoreboard.json").write_text(json.dumps({"events": evs}))
    print({k: len(v) for k, v in out.items()}, (HERE / "cfbd.json.gz").stat().st_size)
    return 0


if __name__ == "__main__":
    sys.exit(add_coaches_all() if sys.argv[1:2] == ["--add-coaches-all"] else add_news() if sys.argv[1:2] == ["--add-news"]
             else main())
