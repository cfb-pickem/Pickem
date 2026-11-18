"""Saturday DM lanes (gameday.py): intents, code answers from a live_games cache, one short text, clumps.
Synthetic games only (no member data). Run: ./.venv/bin/python3 -I test_gameday.py"""
import datetime as dt
import json
import os
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
TMP = Path(tempfile.mkdtemp())
os.environ["PICKY_LIVE_CACHE"] = str(TMP / "live.json")
os.environ["PICKY_GAMEDAY_FILE"] = str(TMP / "gameday.json")
NOW = time.time()
os.environ["PICKY_REPLAY_CLOCK"] = str(TMP / "clock")
(TMP / "clock").write_text(str(NOW))

import gameday as G  # noqa: E402
import live_games as L  # noqa: E402

ISO = lambda s: dt.datetime.fromtimestamp(s, dt.timezone.utc).strftime("%Y-%m-%dT%H:%MZ")  # noqa: E731


def team(name, abbr, nick, rank=None, tid="1"):
    return {"id": tid, "name": name, "abbr": abbr, "nick": nick, "rank": rank}


GAMES = {
    "1": {"id": "1", "away": team("Texas", "TEX", "Longhorns", 6, "251"), "home": team("LSU", "LSU", "Tigers", 21, "99"),
          "away_score": 10, "home_score": 14, "state": "in", "status_name": "STATUS_IN_PROGRESS", "period": 2,
          "clock": "1:47", "start": ISO(NOW - 5400), "possession": "home", "down_distance": "2nd & 1 at TEX 25",
          "city": "Baton Rouge", "broadcasts": ["ABC"], "odds": {"details": "LSU -5.5", "over_under": 47.5, "spread": -5.5},
          "detail": True, "ts": NOW,
          "scoring": [{"team": "Texas", "type": "Field Goal Good", "period": 2, "clock": "3:22",
                       "text": "Gianni Spetic 42 Yd Field Goal", "away": 10, "home": 14}],
          "box": {"teams": {"LSU": {"thirdDownEff": "5-5", "totalYards": "201", "turnovers": "2"},
                            "TEX": {"thirdDownEff": "3-9", "totalYards": "144", "turnovers": "1"}},
                  "players": {"LSU": {"passing": {"labels": ["C/ATT", "YDS", "AVG", "TD", "INT"],
                                                  "rows": [["Sam Leavitt", "14/21", "168", "8.0", "2", "2"]]}},
                              "TEX": {"passing": {"labels": ["C/ATT", "YDS", "AVG", "TD", "INT"],
                                                  "rows": [["Arch Manning", "9/19", "82", "4.3", "0", "1"]]}}}},
          "drives": [{"team": "TEX", "description": "12 plays, 50 yards, 5:55", "result": "Field Goal", "live": False, "plays": []},
                     {"team": "LSU", "description": "5 plays, 35 yards", "result": None, "live": True, "plays": ["x"]}],
          "injuries": []},
    "2": {"id": "2", "away": team("Florida", "FLA", "Gators", None, "57"), "home": team("Kentucky", "UK", "Wildcats", 11, "96"),
          "away_score": 21, "home_score": 14, "state": "post", "status_name": "STATUS_FINAL", "period": 4, "clock": "0:00",
          "start": ISO(NOW - 12000), "city": "Lexington", "broadcasts": ["ESPN"], "ts": NOW},
    "3": {"id": "3", "away": team("Northwestern", "NU", "Wildcats", None, "77"), "home": team("Ohio State", "OSU", "Buckeyes", 20, "194"),
          "away_score": 6, "home_score": 7, "state": "in", "status_name": "STATUS_IN_PROGRESS", "period": 2, "clock": "9:20",
          "start": ISO(NOW - 3600), "city": "Columbus", "broadcasts": ["Fox"], "ts": NOW},
    "4": {"id": "4", "away": team("Purdue", "PUR", "Boilermakers", None, "2509"), "home": team("Iowa", "IOWA", "Hawkeyes", 15, "2294"),
          "away_score": None, "home_score": None, "state": "pre", "period": 0, "clock": "0:00", "start": ISO(NOW + 7200),
          "broadcasts": ["NBC"], "ts": NOW},
}


def setup():
    L._write({"v": 1, "updated_ts": NOW, "games": GAMES, "pool_ids": []}, Path(os.environ["PICKY_LIVE_CACHE"]))
    day = dt.datetime.fromtimestamp(NOW, G.CT).strftime("%Y-%m-%d")
    (TMP / "gameday.json").write_text(json.dumps({"Chuck P": {"date": day, "at": "1", "short": True}}))
    G.WHO.set("Chuck P")
    G.PRIOR.set(("", ""))


def ask(q, prev=("", "")):
    G.PRIOR.set(prev)
    k = G.intent(q, prev[0])
    return k, (G.answer(k, None, q) if k else None)


def test_scores_any_game_and_city():
    setup()
    k, a = ask("whos winning in columbus")
    assert k == "gd_score" and "Ohio State 7" in a and "Northwestern 6" in a and "2nd" in a, a
    k, a = ask("uk florida?")
    assert "Final" in a and "Florida 21" in a, a
    k, a = ask("did kentucky win")
    assert a.startswith("Nope, Florida beat Kentucky 21-14"), a
    assert ask("did kentucky lose")[1].startswith("Yep, Florida beat Kentucky")
    assert ask("did florida lose")[1].startswith("Nope, Florida beat Kentucky")
    assert ask("did uk win")[1].startswith("Nope, Florida beat Kentucky")
    k, a = ask("iowa score")
    assert "hasn't kicked off" in a and "NBC" in a, a


def test_upsets_live_and_final():
    setup()
    k, a = ask("any upsets")
    assert k == "gd_upsets" and "LSU leads No. 6 Texas 14-10" in a and "Florida beat No. 11 Kentucky" in a, a
    assert G.intent("gimme an upset pick") is None  # the pick lane keeps that one


def test_at_game_and_stats():
    setup()
    assert ask("whats the score here")[1].startswith("No. 21 LSU 14, No. 6 Texas 10")
    k, a = ask("who has the ball")
    assert k == "gd_ball" and a.startswith("LSU ball, 2nd & 1"), a
    k, a = ask("what happened on that drive")
    assert k == "gd_drive" and "12 plays, 50 yards" in a and "field goal" in a, a
    k, a = ask("lsu qb stats")
    assert k == "gd_stats" and "Sam Leavitt: 14-of-21, 168 passing yards, 2 TD, 2 INT" in a, a
    k, a = ask("and texas's", ("lsu qb stats", a))
    assert k == "gd_stats" and a.startswith("Arch Manning"), a
    k, a = ask("texas 3rd downs?")
    assert a.startswith("On 3rd down: Texas 3-of-9, LSU 5-of-5"), a
    k, a = ask("how many picks has arch thrown")
    assert a.startswith("Arch Manning has thrown 1 INT (9-of-19"), a
    assert ask("how many yards does leavitt have")[1].startswith("Sam Leavitt has 168 passing yards")
    k, a = ask("anything big happen")
    assert k == "gd_recap" and a.startswith("Upset watch:"), a
    k, a = ask("whats the spread status here")
    assert k == "gd_spread" and "Texas +5.5 is covering by 1.5" in a, a
    k, a = ask("who scored last")
    assert k == "gd_last" and "Spetic" in a, a


def test_followups_total_and_tv():
    setup()
    k, a = ask("and the over?", ("texas lsu score", "No. 21 LSU 14, No. 6 Texas 10"))
    assert k == "gd_total" and "24 points" in a and "23.5 short of the 47.5" in a, a
    k, a = ask("what channel", ("is iowa playing today", ""))
    assert k == "gd_tv" and "NBC" in a, a


def test_why_after_cover_ask():
    setup()
    prev = ("is florida covering", "Florida +3 is covering.")
    G.PRIOR.set(prev)
    assert G.intent("why?", prev[0]) == "gd_why"
    ctx = {"games_this_week": [{"id": "2", "away": "Florida", "home": "Kentucky", "line": -3.0}]}
    a = G.answer("gd_why", ctx, "why?")
    assert a.startswith("Florida leads by 7 and the league line gives them +3, so they're covering by 10"), a
    prev = ("whats the spread status in texas lsu", "")
    G.PRIOR.set(prev)
    assert "Texas +5.5 is covering by 1.5" in G.answer("gd_why", {}, "why?")


def _league_ctx():
    return {"you": {"team_name": "Chuck P"},
            "standings_ytd": [{"team": "Chuck P", "w": 27, "l": 18}, {"team": "Ackbar", "w": 27, "l": 18},
                              {"team": "TG", "w": 20, "l": 25}],
            "games_this_week": [{"id": "1", "away": "Texas", "home": "LSU", "line": -5.5, "winner": None},
                                {"id": "2", "away": "Florida", "home": "Kentucky", "line": -3.0, "winner": "Florida"}],
            "revealed_picks_this_week": {"Chuck P": [{"game_id": "1", "pick": "Texas"}, {"game_id": "2", "pick": "Florida"}],
                                         "Ackbar": [{"game_id": "1", "pick": "LSU"}, {"game_id": "2", "pick": "Kentucky"}]},
            "live_games": [{"GameId": "1", "Status": "in_progress", "AwayPts": 10, "HomePts": 14},
                           {"GameId": "2", "Status": "final", "AwayPts": 21, "HomePts": 14}]}


def test_ended_now_and_member_week():
    setup()
    d = json.loads(Path(os.environ["PICKY_LIVE_CACHE"]).read_text())
    d["pool_ids"] = ["1", "2"]
    L._write(d, Path(os.environ["PICKY_LIVE_CACHE"]))
    assert G.league_intent("if it ended right now where would i be") == "gd_ended"
    assert G.league_intent("ackbar's week?") == "gd_week"
    ctx = _league_ctx()
    a = G.answer("gd_ended", ctx, "if it ended right now where would i be")
    assert "you're in first at 28-18, 1 up on Ackbar" in a, a  # Texas +5.5 covering at 10-14 counts for Chuck
    a = G.answer("gd_week", ctx, "how am i doing this week")
    assert a.startswith("You are 1-0 on finished games this week") and "Texas +5.5 covering by 1.5" in a, a
    a = G.answer("gd_week", ctx, "ackbar's week?")
    assert a.startswith("Ackbar is 0-1") and "LSU -5.5 NOT covering" in a, a


def test_whatif_unranked_and_missing_cache():
    setup()
    orig = G.SLATE_INTEL
    try:
        G.SLATE_INTEL = TMP / "intel.json"
        G.SLATE_INTEL.write_text(json.dumps({"cfp": {"released": "Tue Nov 10", "top": [
            {"rank": 6, "team": "Texas", "record": "7-2"}, {"rank": 11, "team": "Kentucky", "record": "7-2"}]}}))
        k, a = ask("if texas loses what happens")
        assert k == "gd_whatif" and "Texas is No. 6 in the CFP" in a and "bubble" in a, a
        G.SLATE_INTEL = TMP / "missing.json"
        assert ask("if texas loses what happens")[1] is None  # no cache: the full brain answers
    finally:
        G.SLATE_INTEL = orig


def test_not_a_game_day():
    setup()
    L._write({"v": 1, "updated_ts": NOW, "games": {"4": {**GAMES["4"], "start": ISO(NOW + 5 * 86400)}}},
             Path(os.environ["PICKY_LIVE_CACHE"]))
    assert G.intent("uga score") is None and G.intent("any upsets") is None


def test_shorten_and_combine():
    long = "LSU leads Texas 14-10 with 1:47 left in the 2nd. " + "Filler sentence that keeps going on. " * 12
    s = G.shorten(long, "whats the score")
    assert len(s) <= 300 and s.startswith("LSU leads Texas 14-10")
    assert len(G.shorten(long, "break it down for me")) > 300  # he asked for detail
    c = G.combine([("bama final", "Final: Alabama 21, Vanderbilt 6. Roll Tide I guess."),
                   ("any upsets", "Upset watch: Florida beat No. 11 Kentucky 21-14 (final). Wild day.")])
    assert "Alabama 21" in c and "Florida beat" in c and len(c) <= 400 and "\n" not in c, c
    setup()
    day = dt.datetime.fromtimestamp(NOW, G.CT).strftime("%Y-%m-%d")
    assert G.short_for("Chuck P") and not G.short_for("Ackbar")
    (TMP / "gameday.json").write_text(json.dumps({"Chuck P": {"date": "2000-01-01", "short": True}}))
    assert not G.short_for("Chuck P") and day


def test_rank_survives_summary_merge():
    hdr = {"header": {"competitions": [{"competitors": [
        {"homeAway": "home", "rank": 21, "score": "14", "team": {"id": "99", "location": "LSU", "abbreviation": "LSU"}},
        {"homeAway": "away", "rank": 6, "score": "10", "team": {"id": "251", "location": "Texas", "abbreviation": "TEX"}}],
        "status": {"period": 2, "displayClock": "1:47", "type": {"state": "in", "name": "STATUS_IN_PROGRESS"}}}]}}
    g = L.parse_summary(hdr, "1")
    assert g["home"]["rank"] == 21 and g["away"]["rank"] == 6


if __name__ == "__main__":
    fails = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("ok  ", name)
            except Exception as e:  # noqa: BLE001
                fails += 1
                print("FAIL", name, repr(e))
    sys.exit(1 if fails else 0)
