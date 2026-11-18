#!/usr/bin/env python3
"""Freeze the personnel slice of the fact-store fixture (public football data only) from today's caches.

  .venv/bin/python3 -I eval/fixtures/factstore/make_personnel.py

Writes next to this file:
  personnel.json.gz  ESPN rosters + CFBD /games/players rows (weeks so far) for the TEAMS below
  news.json          public news headlines (title/summary/players/teams/date/source/link/label) naming those teams
  dossiers/12/401856754.json   the LSU-Tennessee dossier's sourced facts (no take, no leads)
Re-run only to refresh the fixture; tests pin NOW."""
import gzip
import json
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SERVER = HERE.parents[2]
CACHE = SERVER / "cache"
TEAMS = ["Tennessee", "LSU", "UCLA", "Michigan", "Ohio State", "Nebraska", "Clemson", "Duke", "Indiana", "Washington",
         "North Texas", "Tulane", "Texas A&M", "Oklahoma", "Akron", "Massachusetts"]
NEWS_KEYS = ("title", "summary", "players", "teams", "date", "first_seen", "source", "link", "label", "kind", "status",
             "superseded_by")


def main() -> int:
    db = sqlite3.connect(CACHE / "personnel.sqlite")
    rosters = {}
    for team, eid, f, j in db.execute("select team, espn_id, fetched_at, json from rosters where json is not null"):
        if team in TEAMS:
            d = json.loads(j)
            rosters[team] = {"espn_id": eid, "fetched_at": f, "players": d["players"], "status_rows": d["status_rows"]}
    rows = []
    for (j,) in db.execute("select json from player_games where season=2026 order by week"):
        rows += [r for r in json.loads(j) if r["team"] in TEAMS]
    out = {"rosters": rosters, "player_games": rows}
    (HERE / "personnel.json.gz").write_bytes(gzip.compress(json.dumps(out, separators=(",", ":")).encode(), 9))
    items = json.loads((CACHE / "news.json").read_text())["items"]
    keep = {k: {f: v.get(f) for f in NEWS_KEYS if f in v} for k, v in items.items()
            if v.get("players") and set(v.get("teams") or []) & set(TEAMS)}
    (HERE / "news.json").write_text(json.dumps({"items": keep}, indent=0))
    src = CACHE / "dossiers" / "12" / "401856754.json"
    d = json.loads(src.read_text())
    dd = HERE / "dossiers" / "12"
    dd.mkdir(parents=True, exist_ok=True)
    (dd / "401856754.json").write_text(json.dumps({k: d.get(k) for k in ("game_id", "week", "away", "home", "facts")},
                                                  indent=1))
    print({"rosters": len(rosters), "player_games": len(rows), "news": len(keep)},
          (HERE / "personnel.json.gz").stat().st_size)
    return 0


if __name__ == "__main__":
    sys.exit(main())
