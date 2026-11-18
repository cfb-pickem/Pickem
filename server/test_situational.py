"""Phase 7 (plan 2026-11-18 s.5): situational + game theory lanes, tested by question TYPE on frozen fixtures.

  weather/altitude/travel ask -> weather_alt_travel: a forecast is said as a forecast (source + as-of), never as fact;
                                 an altitude ask names BOTH teams' home elevations; wind is tied to the total
  interim ask                 -> SU/ATS counted from the hire date (local date, so a Saturday-night game stored
                                 with the next day's UTC date isn't counted as 'since')
  dead-last strategy ask      -> names a real consensus side to fade; before the lock never names anyone's pick

The lanes read through sitfacts (the small cache interface the Phase 2 fact store replaces); these tests hand them a
fake source with fixed data, so they never depend on this week's caches.
"""

from __future__ import annotations

import datetime as dt
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pick_strategy  # noqa: E402
import router  # noqa: E402
import situational as sit  # noqa: E402

NOW = dt.datetime(2026, 11, 18, 15, 0, tzinfo=dt.timezone.utc)
KICK = dt.datetime(2026, 11, 21, 20, 0, tzinfo=dt.timezone.utc)  # Sat 2 PM CT


class FakeFacts:
    """Same methods as sitfacts.Live, fixed data."""

    def __init__(self, games=None, wx=None, elev=None, venues=None, trips=None, coaching=None, results=None, crowd=None):
        self.games, self.wx, self.elev = games or [], wx or {}, elev or {}
        self.venues, self.trips, self.coach = venues or {}, trips or {}, coaching or {}
        self.res, self.crowds = results or {}, crowd or {}

    def now(self):
        return NOW

    def teams_named(self, q):
        return [t for t in sorted(set(self.elev) | set(self.coach) | set(self.res)) if t.lower() in q.lower()]

    def find_game(self, q):
        named = [g for g in self.games if g["away"].lower() in q.lower() or g["home"].lower() in q.lower()]
        return named[0] if named else None

    def forecast(self, game_id):
        return self.wx.get(game_id)

    def venue(self, game):
        return self.venues.get(game["id"])

    def home_elev(self, team):
        return self.elev.get(team)

    def trip(self, team, game):
        return self.trips.get((team, game["id"]))

    def coaching(self, team):
        return self.coach.get(team)

    def results(self, team):
        return self.res.get(team, [])

    def crowd(self, week, game_id):
        return self.crowds.get(game_id)


BYU_KU = {"id": 1, "away": "Kansas", "home": "BYU", "kickoff": KICK, "total": 52.5, "spread_home": -6.5}
WYO = {"id": 2, "away": "Colorado State", "home": "Wyoming", "kickoff": KICK, "total": 41.5, "spread_home": -3.0}
UCLA = {"id": 3, "away": "UCLA", "home": "Michigan", "kickoff": dt.datetime(2026, 11, 21, 17, 0, tzinfo=dt.timezone.utc),
        "total": 47.5, "spread_home": -10.5}
WX_AS_OF = "2026-11-18T10:51:49+00:00"


def facts(**kw):
    base = dict(games=[BYU_KU, WYO, UCLA],
                wx={1: {"temp_f": 38.2, "wind_mph": 21.4, "gust_mph": 33.0, "precip_pct": 10, "as_of": WX_AS_OF,
                        "src": "Open-Meteo forecast", "kickoff": KICK.isoformat()},
                    2: {"temp_f": 19.0, "wind_mph": 6.0, "gust_mph": 11.0, "precip_pct": 5, "as_of": WX_AS_OF,
                        "src": "Open-Meteo forecast", "kickoff": KICK.isoformat()}},
                elev={"BYU": 4551, "Kansas": 866, "Wyoming": 7215, "Colorado State": 5029, "UCLA": 205, "Michigan": 840},
                venues={1: {"name": "LaVell Edwards Stadium", "elev_ft": 4551, "neutral": False},
                        2: {"name": "War Memorial Stadium", "elev_ft": 7215, "neutral": False},
                        3: {"name": "Michigan Stadium", "elev_ft": 840, "neutral": False}},
                trips={("UCLA", 3): {"miles": 1978.0, "zones": 3, "home_zone": "Pacific", "venue_zone": "Eastern",
                                     "kick_local": UCLA["kickoff"].astimezone(dt.timezone(dt.timedelta(hours=-5))),
                                     "kick_body": UCLA["kickoff"].astimezone(dt.timezone(dt.timedelta(hours=-8)))},
                       ("Kansas", 1): {"miles": 920.0, "zones": -1, "home_zone": "Central", "venue_zone": "Mountain",
                                       "kick_local": KICK.astimezone(dt.timezone(dt.timedelta(hours=-7))),
                                       "kick_body": KICK.astimezone(dt.timezone(dt.timedelta(hours=-6)))}})
    base.update(kw)
    return FakeFacts(**base)


AS_FACT = re.compile(r"\b(it will be|it'?s going to be|will be \d|it'?ll be \d|guaranteed|for sure)\b", re.I)


# -- weather asks: a forecast is a forecast ---------------------------------------------------------------------------

def test_weather_ask_says_forecast_with_source_and_as_of():
    out = sit.weather_alt_travel_text({}, "what's the weather looking like for Kansas at BYU?", facts())
    assert out, out
    assert re.search(r"\bforecast\b", out, re.I), out
    assert "Open-Meteo" in out, out
    assert re.search(r"\bas of\b", out, re.I), out
    assert not AS_FACT.search(out), out


def test_weather_ask_ties_wind_to_the_total():
    out = sit.weather_alt_travel_text({}, "is wind going to matter in BYU vs Kansas?", facts())
    assert "52.5" in out, out
    assert re.search(r"\bunder\b", out, re.I), out
    assert "lock" not in out.lower(), out


def test_calm_forecast_doesnt_move_the_total():
    out = sit.weather_alt_travel_text({}, "weather for Colorado State Wyoming?", facts())
    assert "41.5" in out and re.search(r"doesn'?t move|no factor|not a factor", out, re.I), out
    assert not re.search(r"\bunder\b", out, re.I), out


def test_stale_forecast_is_not_stated():
    old = {1: {**facts().wx[1], "as_of": "2026-11-16T00:00:00+00:00"}}
    out = sit.weather_alt_travel_text({}, "weather for Kansas at BYU?", facts(wx=old))
    assert "21 mph" not in out and "38F" not in out, out
    assert re.search(r"don'?t have a (current|fresh) forecast", out, re.I), out


def test_dome_is_no_weather_factor():
    out = sit.weather_alt_travel_text({}, "weather for Kansas at BYU?", facts(wx={1: {"dome": True}}))
    assert re.search(r"\bindoors|dome\b", out, re.I) and "wind" not in out.lower(), out


# -- altitude asks: both elevations -----------------------------------------------------------------------------------

def test_altitude_ask_names_both_home_elevations():
    out = sit.weather_alt_travel_text({}, "does BYU's altitude matter for Kansas?", facts())
    assert "4,551" in out and "866" in out, out
    assert "BYU" in out and "Kansas" in out, out


def test_altitude_both_teams_high_is_muted():
    out = sit.weather_alt_travel_text({}, "Colorado State at Wyoming, does the elevation matter?", facts())
    assert "7,215" in out and "5,029" in out, out
    assert re.search(r"muted|lives at altitude|coin flip|doesn'?t move", out, re.I), out


# -- travel asks: miles, zones, body clock -----------------------------------------------------------------------------

def test_travel_ask_gives_miles_zones_and_body_clock():
    out = sit.weather_alt_travel_text({}, "UCLA flying to Michigan, do the time zones hurt them?", facts())
    assert "1,980" in out and re.search(r"3 time zones east", out), out
    assert re.search(r"body clock", out, re.I), out
    assert re.search(r"9 AM", out), out  # noon ET kick = 9 AM on a Pacific body clock


def test_unknown_game_declines():
    assert sit.weather_alt_travel_text({}, "weather for Rutgers at Purdue?", facts()) is None


def test_router_wiring_uses_the_lane():
    import sitfacts
    real = sitfacts.live
    sitfacts.live = lambda: facts()
    try:
        assert router.classify("what's the weather for Kansas at BYU this weekend?").kind == "weather_alt_travel"
        assert router.classify("does altitude matter for Kansas at BYU?").kind == "weather_alt_travel"
        out = router.fact_answer("weather_alt_travel", {"current_week": 12}, "does altitude matter for Kansas at BYU?")
        assert "4,551" in out and "866" in out, out
    finally:
        sitfacts.live = real


def test_short_school_names_resolve():
    import sitfacts
    f = sitfacts.Live.__new__(sitfacts.Live)
    f.idx = {"teams": {"byu": {"name": "BYU", "sp": {"rating": 1}}, "kansas": {"name": "Kansas", "sp": {"rating": 1}},
                       "lsu": {"name": "LSU", "sp": {"rating": 1}}}}
    assert set(f.teams_named("does altitude matter for the byu game at Kansas?")) == {"BYU", "Kansas"}
    assert f.teams_named("LSU weather?") == ["LSU"]
    assert f.teams_named("is the wind bad at the stadium?") == []


def test_router_sends_situational_asks_to_the_lane():
    for q in ["what's the weather for Kansas at BYU this weekend?", "does altitude matter for Kansas at BYU?",
              "UCLA time zones at Michigan, does it matter?"]:
        assert sit.ASK.search(q), q
    for q in ["I'm traveling this weekend, text me my picks", "you're ice cold lately", "what's the spread"]:
        assert router.classify(q).kind != "weather_alt_travel", q


# -- interim: ATS since the hire date ----------------------------------------------------------------------------------

def _row(start, opp, mine, theirs, my_spread):
    return {"start": start, "opp": opp, "pts": (mine, theirs), "spread": my_spread,
            "cover": mine - theirs + my_spread if my_spread is not None else None}


INTERIM_FACTS = dict(
    coaching={"Baylor": {"school": "Baylor", "date": "2026-10-25", "source_name": "www.nbcsports.com",
                         "text": "Baylor fired Dave Aranda on Oct. 25 after a loss to Kansas, and Joe Klanderman is "
                                 "interim head coach for the rest of the season."}},
    results={"Baylor": [_row("2026-10-17T19:00:00Z", "TCU", 6, 17, -3.5),
                        _row("2026-10-26T01:00:00Z", "Kansas", 24, 25, 1.5),   # Sat Oct 25, 8 PM CT: before the hire
                        _row("2026-10-30T23:30:00Z", "UCF", 41, 9, -2.5),
                        _row("2026-11-07T17:00:00Z", "Iowa State", 42, 31, -10.0),
                        _row("2026-11-15T00:00:00Z", "BYU", 7, 28, 8.5)]})


def test_interim_ats_counts_from_the_hire_date():
    out = sit.interim_text({}, "Baylor has an interim coach, does that matter?", facts(**INTERIM_FACTS))
    assert re.search(r"\b2-1 straight up\b", out), out
    assert re.search(r"\b2-1 ATS\b", out), out          # UCF covers, Iowa State covers by 1, BYU misses
    assert "Kansas 24-25" not in out and "TCU" not in out, out
    assert "Oct. 25" in out or "Oct 25" in out, out


def test_interim_names_source_and_keeps_sample_small():
    out = sit.interim_text({}, "Baylor interim coach?", facts(**INTERIM_FACTS))
    assert "nbcsports" in out, out
    assert re.search(r"small sample|3 games", out, re.I), out
    assert "lock" not in out.lower(), out


def test_interim_no_games_since_says_so():
    f = dict(INTERIM_FACTS, coaching={"Baylor": {**INTERIM_FACTS["coaching"]["Baylor"], "date": "2026-11-16"}})
    out = sit.interim_text({}, "Baylor interim?", facts(**f))
    assert re.search(r"hasn'?t played|no games? (yet )?since", out, re.I), out


def test_interim_unknown_team_declines():
    assert sit.interim_text({}, "Rutgers interim coach?", facts(**INTERIM_FACTS)) is None


# -- strategy: dead last = max variance, name the consensus to fade ----------------------------------------------------

WEEK_GAMES = [{"id": 11, "away": "LSU", "home": "Tennessee", "line": 1.5, "start": "2026-11-21T11:00:00",
               "status": "scheduled", "winner": None, "tiebreaker": False},
              {"id": 12, "away": "Iowa", "home": "Illinois", "line": 8.5, "start": "2026-11-21T14:30:00",
               "status": "scheduled", "winner": None, "tiebreaker": False}]
STAND = [{"rank": 1, "team": "Alpha", "w": 30, "l": 18}, {"rank": 2, "team": "Bravo", "w": 28, "l": 20},
         {"rank": 3, "team": "Charlie", "w": 27, "l": 21}, {"rank": 4, "team": "Delta", "w": 26, "l": 22},
         {"rank": 5, "team": "Echo", "w": 25, "l": 23}, {"rank": 6, "team": "Wedge", "w": 18, "l": 30}]
CROWD = {11: {"side": "LSU", "n": 9, "of": 12, "src": "past graded picks"},
         12: {"side": "Iowa", "n": 7, "of": 12, "src": "past graded picks"}}


def ctx_last(**kw):
    c = {"current_week": 12, "you": {"team_name": "Wedge"}, "standings_ytd": STAND, "games_this_week": WEEK_GAMES,
         "revealed_picks_this_week": {"Wedge": [{"game_id": 11, "pick": "Tennessee"}],
                                      # a pick that should never be in an unlocked ctx: the lane must not use it anyway
                                      "Alpha": [{"game_id": 11, "pick": "Tennessee"}, {"game_id": 12, "pick": "Illinois"}]},
         "revealed_game_ids": []}
    c.update(kw)
    return c


def test_dead_last_strategy_names_the_consensus_to_fade():
    out = pick_strategy.answer(ctx_last(), "I'm dead last, what's my strategy?", facts=facts(crowd=CROWD))
    assert re.search(r"dead last|last place", out, re.I), out
    assert re.search(r"max(imum)? variance", out, re.I), out
    assert "LSU" in out and "Tennessee +1.5" in out, out       # the crowd side and the fade, at the league line
    assert re.search(r"habits|past picks|past graded", out, re.I), out  # labeled as a read, not a real pick
    assert "lock" not in out.lower().replace("locked", "").replace("lock in", ""), out


def test_strategy_never_leaks_picks_before_lock():
    out = pick_strategy.answer(ctx_last(), "I'm in last, should I go against the field?", facts=facts(crowd=CROWD))
    assert "Alpha" not in out, out                              # no member's real pick before the lock
    assert not re.search(r"\b(you have|you took|you picked|you'?re on|your pick)\b[^.]{0,20}\bTennessee\b", out, re.I), out
    assert "Illinois +8.5" in out or "Iowa" in out, out


def test_strategy_after_lock_uses_revealed_consensus():
    rev = {"Wedge": [{"game_id": 11, "pick": "Tennessee"}, {"game_id": 12, "pick": "Iowa"}],
           "Alpha": [{"game_id": 11, "pick": "LSU"}, {"game_id": 12, "pick": "Iowa"}],
           "Bravo": [{"game_id": 11, "pick": "LSU"}, {"game_id": 12, "pick": "Iowa"}],
           "Charlie": [{"game_id": 11, "pick": "LSU"}, {"game_id": 12, "pick": "Illinois"}]}
    out = pick_strategy.answer(ctx_last(revealed_picks_this_week=rev, revealed_game_ids=[11, 12]),
                               "I'm dead last, what's my strategy?", facts=facts(crowd={}))
    assert re.search(r"locked", out, re.I), out
    assert re.search(r"LSU,? 3-1|3 of 4[^.]{0,30}LSU|LSU[^.]{0,30}3 of 4", out), out


def test_no_crowd_read_says_so_and_names_nothing():
    out = pick_strategy.answer(ctx_last(), "I'm dead last, what's my strategy?", facts=facts(crowd={}))
    assert re.search(r"max(imum)? variance", out, re.I), out
    assert "LSU" not in out and "Tennessee" not in out and "Alpha" not in out, out


def test_card_crowd_is_the_league_lean_from_past_habits():
    import game_cards
    tend = {"A": {"n": 10, "fav": 0.8, "home": 0.5, "crowd": 0.5}, "B": {"n": 12, "fav": 0.7, "home": 0.5, "crowd": 0.5},
            "C": {"n": 10, "fav": 0.2, "home": 0.5, "crowd": 0.5}, "D": {"n": 3, "fav": 1.0, "home": 1.0, "crowd": 1.0}}
    table = [{"team": t, "w": 10, "l": 10, "rank": i + 1} for i, t in enumerate("ABCD")]
    cg = {"away": "LSU", "home": "Tennessee", "line": 1.5}
    assert game_cards.league_crowd(cg, table, tend) == {"side": "LSU", "n": 2, "of": 3, "src": "past graded picks"}
    tend["C"] = {"n": 10, "fav": 0.2, "home": 0.5, "crowd": 0.5}
    tend["E"] = {"n": 10, "fav": 0.1, "home": 0.5, "crowd": 0.5}
    table.append({"team": "E", "w": 1, "l": 1, "rank": 5})
    assert game_cards.league_crowd(cg, table, tend) is None   # 2-2: no consensus to name


def test_protect_posture_is_unchanged():
    out = pick_strategy.answer(ctx_last(you={"team_name": "Alpha"}), "what's my strategy?", facts=facts(crowd=CROWD))
    assert out == ("You're 30-18, 1st, in a playoff spot, 5 ahead of 5th place (25), with about 14 picks left. Protect it: "
                   "side with the best numbers and the consensus, don't fade the field. Catching 1st only buys seeding, "
                   "because points reset in the playoffs."), out


if __name__ == "__main__":
    fails = 0
    for name, f in list(globals().items()):
        if name.startswith("test_"):
            try:
                f()
                print("ok  ", name)
            except Exception as ex:  # noqa: BLE001 - an error is a failure here
                fails += 1
                print("FAIL", name, type(ex).__name__, ex)
    sys.exit(1 if fails else 0)
