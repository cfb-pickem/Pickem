"""
The small read interface the Phase 7 situational lanes use (weather_alt_travel, interim, strategy's crowd read).

Phase 2's fact store (Supabase `picky` schema behind a local cache) isn't merged yet, so `Live` reads today's caches:
  forecast   cache/weather.json        (weather.py, Open-Meteo free API, refreshed on the hourly slate-intel tick)
  venues     cache/cfbd.sqlite venues  (CFBD free tier: elevation, lat/lon, timezone, dome)
  games      cache/cfbd.sqlite         (schedule, results, DraftKings spread/total)
  coaching   cache/slate_intel.json    (dated, sourced coaching changes)
  crowd      cache/game_cards/<wk>/    (game_cards.league_crowd: the league's lean from past graded picks)
When the fact store lands, swap `live()` for a store-backed object with the same methods. Lanes never fetch.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent


class Live:
    def __init__(self):
        import cfbd
        self.idx = cfbd.load_index() or {"teams": {}}

    def now(self) -> dt.datetime:
        return dt.datetime.now(dt.timezone.utc)

    def teams_named(self, q: str) -> list[str]:
        import re

        import cfbd
        if not self.idx.get("teams"):
            return []
        out = cfbd.other_teams_named(q or "", [], self.idx)
        for w in re.findall(r"\b[A-Za-z]{3}\b", q or ""):  # the shared resolver skips 3-letter words: BYU, LSU, TCU ...
            t = self.idx["teams"].get(cfbd.norm(w))
            if t and t.get("sp") and cfbd.norm(t["name"]) == cfbd.norm(w) and t["name"] not in out:
                out.append(t["name"])
        return out

    def find_game(self, q: str) -> dict | None:
        """This week's not-yet-played game for the team(s) a text names (both named teams first)."""
        import cfbd
        import slate_cards
        named = {cfbd.norm(t) for t in self.teams_named(q)}
        if not named:
            return None
        games = slate_cards.upcoming_games(self.idx, self.now())
        hits = [g for g in games if named & {cfbd.norm(g["awayTeam"]), cfbd.norm(g["homeTeam"])}]
        hits.sort(key=lambda g: -len(named & {cfbd.norm(g["awayTeam"]), cfbd.norm(g["homeTeam"])}))
        if not hits:
            return None
        g = hits[0]
        ln = slate_cards._line(g) or {}
        return {"id": g["id"], "away": g["awayTeam"], "home": g["homeTeam"], "kickoff": slate_cards._sd(g),
                "total": ln.get("overUnder"), "spread_home": ln.get("spread"), "startDate": g.get("startDate")}

    def forecast(self, game_id) -> dict | None:
        import weather
        return weather.for_game(game_id)

    def venue(self, game: dict) -> dict | None:
        import travel
        v, neutral = travel.game_venue(game["id"], game["home"])
        return {"name": v.get("name"), "elev_ft": travel.elev_ft(v), "neutral": neutral} if v else None

    def home_elev(self, team: str) -> int | None:
        import travel
        return travel.elev_ft(travel.home_venue(team))

    def trip(self, team: str, game: dict) -> dict | None:
        import travel
        v, _ = travel.game_venue(game["id"], game["home"])
        return travel.trip(team, v, game.get("kickoff"))

    def coaching(self, team: str) -> dict | None:
        import cfbd
        try:
            d = json.loads((HERE / "cache" / "slate_intel.json").read_text())
        except (OSError, ValueError):
            return None
        hits = [c for c in d.get("coaching") or [] if cfbd.norm(c.get("school", "")) == cfbd.norm(team)]
        return max(hits, key=lambda c: c.get("date") or "") if hits else None

    def results(self, team: str) -> list[dict]:
        import slate_cards
        return slate_cards.team_rows(team, self.idx, "9999") if self.idx.get("teams") else []

    def crowd(self, week, game_id) -> dict | None:
        import game_cards
        try:
            return (game_cards.load_card(week, game_id) or {}).get("crowd")
        except Exception:  # noqa: BLE001 - no card, no read
            return None


def live() -> Live:
    return Live()
