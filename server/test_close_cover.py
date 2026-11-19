"""
Close-cover alerts (close_cover.py): the window gate, the cover math, cover_live,
the 2-posts-per-game engine, the words, and the runner's one combined post through
the outbox (dry run, temp private dir). No network, no LLM.

Run:  ./.venv/bin/python3 -I test_close_cover.py
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
_TMP = tempfile.mkdtemp(prefix="picky-cover-test-")
os.environ["PICKY_PRIVATE_DIR"] = _TMP
import close_cover as cc  # noqa: E402
import group_outbox as ob  # noqa: E402

CT = cc.CT
TEAMS = {"away": {"id": "1", "abbr": "ILL", "loc": "Illinois", "nick": "Illini"},
         "home": {"id": "2", "abbr": "IOWA", "loc": "Iowa", "nick": "Hawkeyes"}}
POOL = {"GameId": 9, "week": 12, "Away": "Illinois", "Home": "Iowa", "line": -6.5}


def snap(period=4, clock="5:00", ap=17, hp=21, has_ball=None, receiving=None, down=1, distance=10, spot=None,
         yte=None, kneeling=False, timeouts=None, situation=True, network=None, state="in", turnover=False,
         src="espn", play_id=None):
    return {"state": state, "period": period, "clock": clock, "clock_s": cc.clock_secs(clock), "away_score": ap,
            "home_score": hp, "teams": TEAMS, "network": network, "has_ball": has_ball, "receiving": receiving,
            "down": down, "distance": distance, "yte": yte, "spot": spot, "kneeling": kneeling,
            "turnover": turnover, "situation": situation,
            "timeouts": timeouts if timeouts is not None else {"away": 3, "home": 3},
            "timeouts_src": src, "play_id": play_id}


def row(s, pool=POOL, counts=None, ackbar=None):
    return {"gid": str(pool["GameId"]), "pool": pool, "snap": s, "counts": counts, "ackbar": ackbar}


# --- (a) window ---------------------------------------------------------------

def test_window_gate():
    assert cc.WINDOW_S == 480
    assert cc.in_window(4, cc.clock_secs("7:59"))
    assert not cc.in_window(4, cc.clock_secs("8:01"))
    assert not cc.in_window(4, cc.clock_secs("8:00"))
    assert not cc.in_window(3, cc.clock_secs("0:30"))  # Q3 never
    assert not cc.in_window(3, 0)
    assert cc.in_window(5, None) and cc.in_window(6, cc.clock_secs("0:00"))  # OT, any clock


# --- (b) close for the COVER, not the game ---------------------------------------

def test_close_cover_not_close_game():
    # home 24, away 21, home -13.5: a 3-point game, but the cover is 10.5 away -> no
    cm = cc.cover_math(-13.5, 21, 24)
    assert cm["cov"] == "away" and cm["needs"] == 11 and not cc.is_close(cm)
    # home 38, away 17, home -20.5: a 21-point blowout, but a field goal flips the cover -> yes if live
    cm = cc.cover_math(-20.5, 17, 38)
    assert cm["cov"] == "home" and cm["t"] == "away" and cm["needs"] == 1 and cc.is_close(cm)
    ok, _ = cc.cover_alert(4, cc.clock_secs("4:00"), cm, {"has_ball": "T", "opp_leads": True}, 2)
    assert ok
    ok, why = cc.cover_alert(4, cc.clock_secs("1:30"), cm, {"has_ball": "opp", "kneeling": True, "opp_leads": True}, 0)
    assert not ok and why == "victory formation"


def test_line_sign_and_push():
    # line is the home adjustment: -7 = home favored by 7. 24-17 home is dead even -> Away (push goes to Away)
    cm = cc.cover_math(-7, 17, 24)
    assert cm["m"] == 0 and cm["cov"] == "away" and cm["t"] == "home" and cm["needs"] == 1
    # +3 = home dog: home loses 20-22 and still covers by 1
    cm = cc.cover_math(3, 22, 20)
    assert cm["cov"] == "home" and cm["margin"] == 1 and cm["t"] == "away" and cm["needs"] == 1
    # half points: home -6.5 up 21-17 -> away +6.5 covers by 2.5, home needs 3 (a field goal)
    cm = cc.cover_math(-6.5, 17, 21)
    assert cm["cov"] == "away" and cm["needs"] == 3 and cc.score_kind(3) == "a field goal"
    assert cc.side_label(POOL, "home") == "Iowa -6.5" and cc.side_label(POOL, "away") == "Illinois +6.5"
    # push text in the alert
    p = dict(POOL, line=-4)
    e = cc.Engine({})
    out = e.step(0, [row(snap(clock="3:00", ap=17, hp=21, has_ball="home"), pool=p)])
    assert out and "covering on the number (a dead-even finish goes to Illinois)" in out[0]["text"], out


# --- (c) cover_live -------------------------------------------------------------

def test_cover_live_cases():
    L = cc.cover_live
    # leader kneeling, 1:30 left, T has 0 timeouts -> no
    assert L({"has_ball": "opp", "kneeling": True, "opp_leads": True}, 7, 90, 0) == (False, "victory formation")
    # ... and even without the kneel flag the math says they can kneel it out
    assert not L({"has_ball": "opp", "down": 1, "opp_leads": True}, 7, 90, 0)[0]
    # T has the ball, 2nd & 6 at the 30, needs a TD -> yes; with 0:20 left nobody can tune in -> no
    assert L({"has_ball": "T", "down": 2, "opp_leads": True}, 6, 300, 1)[0]
    assert not L({"has_ball": "T", "down": 2, "opp_leads": True}, 6, 20, 1)[0]
    # opponent has it, 4:30 left, T has 3 timeouts -> yes
    assert L({"has_ball": "opp", "down": 1, "opp_leads": True}, 3, 270, 3)[0]
    # T needs 2 scores, 1:10 left, no ball -> no
    assert not L({"has_ball": "opp", "down": 1, "opp_leads": True}, 10, 70, 3)[0]
    assert not L({"has_ball": "opp", "down": 1, "opp_leads": False}, 10, 70, 3)[0]
    # T needs 2 scores, 7:00 left, opponent's ball -> yes
    assert L({"has_ball": "opp", "down": 1, "opp_leads": True}, 10, 420, 2)[0]
    # missing situation block: possession fallback only
    assert L({"has_ball": "T", "fallback": True}, 6, 200, None)[0]
    assert not L({"has_ball": "opp", "fallback": True}, 3, 400, None)[0]
    assert not L({"has_ball": None, "fallback": True}, 3, 400, None)[0]
    # kickoff pending to T counts as T's ball; unknown ball -> skip; OT one score -> yes, two -> no
    assert L({"receiving": "T"}, 3, 120, 0)[0]
    assert not L({}, 3, 300, 3)[0]
    assert L({"ot": True, "has_ball": "opp"}, 7, None, 1)[0] and not L({"ot": True}, 9, None, 1)[0]
    # 4th down, opponent can't burn it all: a punt is coming
    assert L({"has_ball": "opp", "down": 4, "opp_leads": True}, 3, 100, 0)[0]
    # trailing opponent won't kneel: T gets it back
    assert L({"has_ball": "opp", "down": 1, "opp_leads": False}, 6, 120, 0)[0]


def test_engine_fallback_without_situation():
    e = cc.Engine({})
    # no situation block: T (Iowa, needs 3) without the ball -> skip; with it -> post
    assert e.step(0, [row(snap(clock="5:00", has_ball="away", situation=False))]) == []
    out = e.step(15, [row(snap(clock="4:45", has_ball="home", situation=False))])
    assert len(out) == 1 and out[0]["why"] == "possession only"


# --- posting rules ----------------------------------------------------------------

def test_max_two_posts_and_spacing_and_flip_text():
    e = cc.Engine({})
    # post 1: Iowa -6.5 up 21-17 -> Illinois +6.5 covering by 2.5; Iowa ball 2nd & 6 at the ILL 30
    s1 = snap(clock="6:10", ap=17, hp=21, has_ball="home", down=2, distance=6, spot="ILL 30", yte=30,
              network="FOX")
    out = e.step(1000, [row(s1, counts={"away": 8, "home": 5})])
    assert len(out) == 1 and out[0]["n"] == 1
    txt = out[0]["text"]
    assert "Illinois 17, Iowa 21, 6:10 left in the 4th." in txt, txt
    assert "Illinois +6.5 is covering by 2.5." in txt, txt
    assert "Iowa has it 2nd & 6 at the Illinois 30, a field goal flips it." in txt, txt
    assert "8 of you are sweating Illinois, 5 need Iowa to score." in txt and txt.endswith("Tune to FOX."), txt
    e.commit(1000, out)
    # Iowa kicks a FG 60 s later: flip (Iowa now covers 24-17 by 0.5). Held: debounce + 2-min spacing
    s2 = snap(clock="5:05", ap=17, hp=24, receiving="away", network="FOX")
    assert e.step(1060, [row(s2)]) == []          # first sighting starts the debounce
    assert e.step(1095, [row(s2)]) == []          # 35 s later: debounced, but only 95 s after post 1
    out = e.step(1125, [row(s2, counts={"away": 8, "home": 5})])
    assert len(out) == 1 and out[0]["n"] == 2 and out[0]["kind"] == "flip", out
    t2 = out[0]["text"]
    assert "Iowa" in t2.split(".")[0] and "Iowa -6.5 is covering by 0.5" in t2, t2
    assert "Illinois gets it back on the kickoff, a field goal flips it back." in t2, t2
    assert "That swings it for 5 of you." in t2 and "Tune to FOX." in t2, t2
    e.commit(1125, out)
    # a third trigger (flip back) is suppressed: max 2 per game, total
    s3 = snap(clock="2:00", ap=20, hp=24, has_ball="home")
    for t in (1300, 1340, 1400):
        assert e.step(t, [row(s3)]) == []


def test_flip_debounce_drops_a_blip():
    e = cc.Engine({})
    e.commit(0, e.step(0, [row(snap(clock="7:00", ap=17, hp=21, has_ball="home"))]))
    blip = snap(clock="6:00", ap=17, hp=24, receiving="away")
    assert e.step(200, [row(blip)]) == []
    back = snap(clock="5:50", ap=17, hp=21, has_ball="away")  # score corrected back
    assert e.step(215, [row(back)]) == []
    assert e.step(260, [row(blip)]) == []  # a new sighting restarts the debounce


def test_live_again_after_turnover():
    # Iowa +3 at home; Illinois up 24-21 = dead even on the number -> Illinois covers, Iowa needs 1
    p = dict(POOL, line=3)
    e = cc.Engine({})
    e.commit(0, e.step(0, [row(snap(clock="7:30", ap=24, hp=21, has_ball="home", down=None), pool=p)]))
    # Illinois gets the ball back with 1:20 left and Iowa out of timeouts: it can kneel it out -> dead
    dead = snap(clock="1:20", ap=24, hp=21, has_ball="away", down=1, timeouts={"away": 3, "home": 0})
    assert e.step(300, [row(dead, pool=p)]) == []
    # fumble, Iowa ball: live again, held 30 s
    again = snap(clock="1:05", ap=24, hp=21, has_ball="home", down=None, turnover=True,
                 timeouts={"away": 3, "home": 0})
    assert e.step(330, [row(again, pool=p)]) == []
    out = e.step(361, [row(again, pool=p)])
    assert len(out) == 1 and out[0]["kind"] == "again", out
    t = out[0]["text"]
    assert "is live after the turnover" in t and "Iowa has it, a field goal flips it." in t, t
    assert "Illinois -3 is covering on the number (a dead-even finish goes to Illinois)" in t, t


def test_no_final_from_this_feature():
    e = cc.Engine({})
    e.commit(0, e.step(0, [row(snap(clock="6:00", ap=17, hp=21, has_ball="home"))]))
    fin = snap(clock="0:00", ap=17, hp=24, state="post")
    for t in (200, 240, 400):
        assert e.step(t, [row(fin)]) == []
    # a game never alerted that ends close: still nothing
    e2 = cc.Engine({})
    assert e2.step(0, [row(fin)]) == []


def test_network_present_and_missing():
    e = cc.Engine({})
    t = e.step(0, [row(snap(clock="6:00", has_ball="home", network="ESPN2"))])[0]["text"]
    assert t.endswith("Tune to ESPN2.")
    e = cc.Engine({})
    t = e.step(0, [row(snap(clock="6:00", has_ball="home", network=None))])[0]["text"]
    assert "Tune to" not in t
    # from ESPN's payload: national TV first; nothing listed -> None (never guess)
    d = {"header": {"competitions": [{"broadcasts": [
        {"type": {"shortName": "Streaming"}, "market": {"type": "National"}, "media": {"shortName": "ESPN+"}},
        {"type": {"shortName": "TV"}, "market": {"type": "National"}, "media": {"shortName": "ABC"}}]}]}}
    assert cc.network(d) == "ABC"
    assert cc.network({"header": {"competitions": [{}]}}) is None


def test_no_split_before_lock_and_counts_only_after():
    e = cc.Engine({})
    t = e.step(0, [row(snap(clock="6:00", has_ball="home"), counts=None)])[0]["text"]
    assert "of you" not in t
    assert cc.split_words({"away": 1, "home": 0}, POOL, cc.cover_math(-6.5, 17, 21)) == "1 of you is sweating Illinois."


def test_ackbar_jab_only_when_he_loses_the_flip():
    jabbed = []
    e = cc.Engine({}, ackbar_jab=lambda t: jabbed.append(t) or t + " JAB")
    e.commit(0, e.step(0, [row(snap(clock="7:00", ap=17, hp=21, has_ball="home"), ackbar="away")]))
    s2 = snap(clock="5:00", ap=17, hp=24, receiving="away")
    e.step(130, [row(s2, ackbar="away")])
    out = e.step(170, [row(s2, ackbar="away")])
    assert out[0]["text"].endswith("Ackbar had Illinois. JAB"), out
    e = cc.Engine({}, ackbar_jab=lambda t: t + " JAB")
    e.commit(0, e.step(0, [row(snap(clock="7:00", ap=17, hp=21, has_ball="home"), ackbar="home")]))
    e.step(130, [row(s2, ackbar="home")])
    assert "JAB" not in e.step(170, [row(s2, ackbar="home")])[0]["text"]  # he WON the flip: no jab


# --- runner: one combined post through the outbox, quiet hours ---------------------

class _FakeLeague:
    def __init__(self, games):
        self.games, self.tid = games, {}

    def picks(self, w):
        raise PermissionError("not locked")


def _run_tick(now_dt, summaries):
    import live_games as L
    pool2 = {"GameId": 10, "week": 12, "Away": "Utah", "Home": "BYU", "line": 3, "picked": True}
    pool1 = dict(POOL, picked=True)
    cache = {"pool_ids": ["9", "10"], "games": {
        "9": {"state": "in", "period": 4, "clock": "6:00", "sb_ts": 0},
        "10": {"state": "in", "period": 4, "clock": "6:30", "sb_ts": 0}}}
    real = (L.load, L._get)
    L.load = lambda *a, **k: cache
    L._get = lambda client, url, *a, **k: (summaries[url.rsplit("=", 1)[1]], {})
    snaps = {"9": snap(clock="6:00", has_ball="home", network="FOX"),
             "10": dict(snap(clock="6:30", ap=20, hp=21, has_ball="away"), teams={
                 "away": {"id": "3", "abbr": "UTAH", "loc": "Utah", "nick": "Utes"},
                 "home": {"id": "4", "abbr": "BYU", "loc": "BYU", "nick": "Cougars"}})}
    real_snap = cc.snapshot
    cc.snapshot = lambda d, sb=None, **k: snaps[d]
    lg = _FakeLeague([pool1, pool2])

    class _L(cc.League):
        def get(self, now):
            return lg
    try:
        eng = cc.Engine({})
        wait = cc.tick_once(_L(), eng, dry_run=True, echo=lambda *a: None, client=object(), now_dt=now_dt)
        return wait, eng
    finally:
        L.load, L._get = real
        cc.snapshot = real_snap


def _log():
    try:
        return [json.loads(x) for x in ob.SEND_LOG.read_text().splitlines()]
    except OSError:
        return []


def test_combined_post_and_quiet_hours():
    n0 = len(_log())
    wait, eng = _run_tick(dt.datetime(2026, 11, 21, 14, 30, tzinfo=CT), {"9": "9", "10": "10"})
    assert wait == cc.TICK_HOT
    new = [r for r in _log()[n0:] if r["event"] == "dry-run"]
    assert len(new) == 1 and new[0]["kind"] == "cover" and len(new[0]["texts"]) == 1, new  # ONE text, both games
    assert len(new[0]["texts"][0]) <= 280 and "Illinois +6.5" in new[0]["texts"][0] and "BYU +3" in new[0]["texts"][0], new
    assert "9#1" in new[0]["key"] and "10#1" in new[0]["key"]
    assert len(eng.state["9"]["posts"]) == 1 and len(eng.state["10"]["posts"]) == 1  # one post each
    # quiet hours: nothing even gets looked at
    n1 = len(_log())
    _run_tick(dt.datetime(2026, 11, 21, 23, 45, tzinfo=CT), {"9": "9", "10": "10"})
    assert len(_log()) == n1


def test_snapshot_from_real_play_by_play():
    """The 11/14 Hawai'i game: NDSU's TD at 6:27 of the 4th leaves a kickoff to Hawai'i."""
    import gzip
    import live_games as L
    f = HERE / "eval" / "fixtures" / "live-2026-11-14" / "summary_401864537.json.gz"
    d = json.load(gzip.open(f))
    plays = L._flat_plays(d)
    i = next(i for i, p in enumerate(plays) if p.get("awayScore") == 21 and p.get("scoringPlay"))
    s = cc.snapshot(d, upto=i)
    assert s["period"] == 4 and s["receiving"] == "home" and s["has_ball"] is None, s
    assert s["timeouts_src"] == "counted" and s["network"] == "CW"
    cm = cc.cover_math(6.5, s["away_score"], s["home_score"])
    sit, to = cc.situation_for(s, cm["t"])
    assert cm["t"] == "home" and cm["needs"] == 2 and cc.cover_alert(s["period"], s["clock_s"], cm, sit, to)[0]
    # the final play: state post, no alert
    assert cc.snapshot(d, upto=len(plays) - 1)["state"] == "post"


def test_replay_week11_runs_without_sends():
    n0 = len(_log())
    posts = cc.replay(HERE / "eval" / "fixtures" / "live-2026-11-14", echo=lambda *a: None)
    assert len(_log()) == n0  # replay never touches the outbox
    sent = [p for p in posts if p["n"]]
    assert all(p["n"] <= 2 for p in sent)
    per = {}
    for p in sent:
        per[p["gid"]] = per.get(p["gid"], 0) + 1
    assert all(v <= cc.MAX_POSTS for v in per.values())


def test_final_ping_names_who_covered():
    """Finals stay group_posts' job (and that ping is off unless PICKY_FINAL_PINGS exists): when it
    runs, it says who covered the league line."""
    import group_posts as gp

    class Lg:
        tid = {1: "Ackbar", 2: "TG"}

        def picky_picks(self):
            return []

        def picks(self, w):
            return [{"team_id": 1, "game_id": 9, "pick": "Iowa"}, {"team_id": 2, "game_id": 9, "pick": "Illinois"}]

        def nick(self, t):
            return t
    saved = gp.standings
    gp.standings = lambda lg: []
    try:
        g = dict(POOL)
        txt = gp.make_ping(Lg(), [(g, 17.0, 24.0)])[0]
        assert "Iowa -6.5 covers (Illinois 17, Iowa 24)" in txt, txt
        txt = gp.make_ping(Lg(), [(g, 20.0, 24.0)])[0]
        assert "Illinois +6.5 covers (Illinois 20, Iowa 24)" in txt, txt
    finally:
        gp.standings = saved
    src = (HERE / "close_cover.py").read_text()
    assert '"ping"' not in src and "make_ping" not in src  # this feature never posts a final


if __name__ == "__main__":
    tests = [v for k, v in dict(globals()).items() if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"  ok  {fn.__name__}")
        except AssertionError as e:
            print(f"FAIL  {fn.__name__}: {e}")
            failed += 1
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            print(f"ERR   {fn.__name__}: {type(e).__name__}: {e}")
            failed += 1
    print("\nALL GREEN" if not failed else f"\n{failed} FAILED")
    sys.exit(1 if failed else 0)
