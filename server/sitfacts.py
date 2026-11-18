"""
The small read interface the Phase 7 situational lanes use (weather_alt_travel, interim, strategy's crowd read).

Phase 2's fact store (factstore.py: Supabase `picky` schema behind a local cache) backs the football facts:
  teams, games, market     factstore (team names incl. 3-letter schools, this week's games, spread/total)
  coaching, results        factstore (dated, sourced coaching changes; results vs the number)
and these stay computation modules over static/forecast data or league data:
  forecast   weather.for_game          (weather.py, Open-Meteo free API)
  venues     travel.py                 (CFBD venues: elevation, lat/lon, timezone, dome)
  crowd      cache/game_cards/<wk>/    (game_cards.league_crowd: the league's lean from past graded picks)
Lanes never fetch.
"""

from __future__ import annotations

import datetime as dt


class Live:
    def now(self) -> dt.datetime:
        import factstore
        return dt.datetime.fromtimestamp(factstore._now(), dt.timezone.utc)

    def teams_named(self, q: str) -> list[str]:
        import factstore
        return factstore.teams_named(q)

    def find_game(self, q: str) -> dict | None:
        """This week's not-yet-played game for the team(s) a text names (both named teams first)."""
        import factstore
        named = {factstore.norm(t) for t in self.teams_named(q)}
        if not named:
            return None
        hits = [g for g in factstore.upcoming() if named & {factstore.norm(g["awayTeam"]), factstore.norm(g["homeTeam"])}]
        hits.sort(key=lambda g: -len(named & {factstore.norm(g["awayTeam"]), factstore.norm(g["homeTeam"])}))
        if not hits:
            return None
        g = hits[0]
        mk = factstore.market(g)
        try:
            kick = dt.datetime.fromisoformat(str(g.get("startDate")).replace("Z", "+00:00"))
        except ValueError:
            kick = None
        return {"id": g["id"], "away": g["awayTeam"], "home": g["homeTeam"], "kickoff": kick,
                "total": mk.get("overUnder"), "spread_home": mk.get("spread"), "startDate": g.get("startDate")}

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
        import factstore
        hits = [c for c in factstore.coaching() if factstore.norm(c.get("school", "")) == factstore.norm(team)]
        return max(hits, key=lambda c: c.get("date") or "") if hits else None

    def results(self, team: str) -> list[dict]:
        import factstore
        return factstore.team_rows(team)

    def crowd(self, week, game_id) -> dict | None:
        import game_cards
        try:
            return (game_cards.load_card(week, game_id) or {}).get("crowd")
        except Exception:  # noqa: BLE001 - no card, no read
            return None


def live() -> Live:
    return Live()
