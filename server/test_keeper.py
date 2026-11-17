"""Overnight keeper (2026-11-16/17) sparring regressions: model-free, deterministic code paths only.
Each test names the sparring convo that found it (cfb-pickem-private/spar/keeper/)."""
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
_TMP = tempfile.mkdtemp()
os.environ.setdefault("PICKY_NO_REPEAT_STATE", str(Path(_TMP) / "nr.json"))  # never the live ledger
os.environ.setdefault("PICKY_CALLBACKS_STATE", str(Path(_TMP) / "cb.json"))

import chat  # noqa: E402
import router  # noqa: E402
from test_picky_brain import _stance_ctx  # noqa: E402


def _wk12(me="TG"):
    """Monday after week 11: week 12 is current (nothing final), week 11 is last_week."""
    ctx = _stance_ctx(me)
    wk11 = ctx["games_this_week"]
    pts = {x["GameId"]: x for x in ctx["live_games"]}
    ctx["last_week"] = {"week": 11, "you": "1-3", "games": [
        {"away": g["away"], "home": g["home"], "away_pts": pts[g["id"]]["AwayPts"], "home_pts": pts[g["id"]]["HomePts"],
         "line": g["line"], "covered": g["winner"], "picks": {}} for g in wk11]}
    ctx["current_week"] = 12
    ctx["games_this_week"] = [
        {"id": 11, "away": "Northwestern", "home": "Minnesota", "line": 2.5, "winner": None, "status": "pre",
         "tiebreaker": False, "start": "2026-11-21T11:00:00"},
        {"id": 12, "away": "Iowa", "home": "Illinois", "line": 8.5, "winner": None, "status": "pre",
         "tiebreaker": False, "start": "2026-11-21T13:30:00"}]
    ctx["live_games"] = []
    ctx["weekly_records"] = {"TG": {9: [1, 3], 10: [3, 1], 11: [1, 3]}, "Chuck P": {9: [3, 1], 10: [3, 1], 11: [4, 0]},
                             "Ackbar": {9: [3, 1], 10: [4, 0], 11: [3, 1]}}
    return ctx


def test_who_covered_last_week_answers_from_last_weeks_finals():
    # k1-c30 'who covered last week' on the Monday: this week has no finals, so the cover lane returned None
    ctx = _wk12()
    with patch.object(chat, "load_lore", lambda: {}):
        r = router.fact_answer("cover", ctx, "who covered last week")
    assert r and r.startswith("Week 11 covers: Wake Forest -1.5, Oregon State +13.5"), r
    assert "UTSA +2.5" in r and "North Dakota State -6.5" in r and "Didn't" not in r, r


def test_record_over_the_last_n_weeks():
    # k1-c29 'whats my record last 3 weeks' got the season record
    ctx = _wk12("TG")
    with patch.object(chat, "load_lore", lambda: {}):
        r = router.fact_answer("record", ctx, "whats my record last 3 weeks")
        assert r == "You're 5-7 over the last 3 weeks (week 9 1-3, week 10 3-1, week 11 1-3).", r
        r = router.fact_answer("record", ctx, "what was my record last week")
        assert r == "You went 1-3 in week 11.", r
        r = router.fact_answer("record", ctx, "whats Ackbar's record last 2 weeks")
        assert r == "Ackbar is 7-1 over the last 2 weeks (week 10 4-0, week 11 3-1).", r
        r = router.fact_answer("record", ctx, "whats my record")  # unchanged
        assert r.startswith("You're 19-29 on the season"), r


def test_shots_at_pickys_picks_are_not_slate_asks():
    # k1-c46 'tg says ur picks are worse than his', k1-c42 'bo nix > ur picks' got the 4-game slate card dump
    for q in ("tg says ur picks are worse than his", "bo nix > ur picks", "your picks suck", "ur picks are trash lol",
              "my dog makes better picks than your picks"):
        assert not chat.slate_ask(q), q
        assert router.classify(q).kind != "slate", q
    for q in ("gimme ur picks", "your picks this week?", "any picks", "picks?", "who should i pick this week",
              "ur picks for this week"):
        assert chat.slate_ask(q), q


def _wk12_picks():
    ctx = _wk12("TG")
    picks = {"Ackbar": ["Wake Forest", "Boise State", "UTSA", "North Dakota State"],
             "TG": ["SMU", "Oregon State", "North Texas", "Hawai'i"],
             "Andrew Bub": ["SMU", "Boise State", "North Texas", "Hawai'i"]}
    for i, g in enumerate(ctx["last_week"]["games"]):
        g["picks"] = {t: v[i] for t, v in picks.items()}
    ctx["weekly_records"]["Andrew Bub"] = {11: [0, 4]}
    return ctx


def test_last_week_results_for_a_member_or_the_sender():
    # k1: 'how did ackbar do last week' returned nothing (results lane only reads this week)
    ctx = _wk12_picks()
    with patch.object(chat, "load_lore", lambda: {}), patch.object(chat, "member_nicknames", lambda: {"Ackbar": "Ackbar"}):
        r = router.fact_answer("results", ctx, "how did ackbar do last week")
        assert r == ("Ackbar went 3-1 in week 11: hit Wake Forest -1.5, UTSA +2.5 and North Dakota State -6.5, "
                     "missed Boise State -13.5 (by 8.5)."), r
        r = router.fact_answer("results", ctx, "howd i do last week")
        assert r.startswith("You went 1-3 in week 11: hit Oregon State +13.5, missed SMU +1.5 (by 40.5), "), r
        ctx["last_week"]["games"][0]["picks"]["TG"] = "Wake Forest"
        for i, t in ((2, "UTSA"), (3, "North Dakota State")):
            ctx["last_week"]["games"][i]["picks"]["TG"] = t
        r = router.fact_answer("results", ctx, "Howd the week go")  # Monday, nothing started: last week
        assert r == ("You went 4-0 in week 11, clean sweep: Wake Forest -1.5, Oregon State +13.5, UTSA +2.5 and "
                     "North Dakota State -6.5 all covered."), r


def test_did_x_cover_last_week_never_reads_this_weeks_game():
    # k1: 'did iowa cover last week' -> 'Iowa -8.5 hasn't kicked off.'
    ctx = _wk12_picks()
    with patch.object(chat, "load_lore", lambda: {}):
        r = router.fact_answer("cover", ctx, "did iowa cover last week")
        assert "hasn't kicked off" not in r and r.startswith("Iowa wasn't in the pool in week 11"), r
        r = router.fact_answer("cover", ctx, "did boise cover last week")
        assert r.startswith("Boise State -13.5 missed the cover by 8.5"), r
        r = router.fact_answer("cover", ctx, "did iowa cover")  # this week, not started: unchanged
        assert "hasn't kicked off" in r, r


def test_who_went_0_4_last_week():
    ctx = _wk12_picks()
    with patch.object(chat, "load_lore", lambda: {}):
        assert router.classify("who went 0-4 last week besides me").label() == "fact:results"
        r = router.fact_answer("results", ctx, "who went 0-4 last week besides me")
        assert r == "Week 11 0-4: Andrew Bub. Nobody else.", r
        r = router.fact_answer("results", ctx, "anyone go 4-0 last week")
        assert r == "Week 11 4-0: Chuck P.", r


def test_top_4_right_now_and_weeks_left():
    assert router.classify("top 4 right now go").label() == "fact:playoff_today"
    assert router.classify("who's top four rn").label() == "fact:playoff_today"
    assert router.classify("how many weeks left").label() == "fact:weeks_left"
    assert router.classify("how many weeks are left in the season").label() == "fact:weeks_left"
    ctx = _wk12_picks()
    r = router.fact_answer("weeks_left", ctx, "how many weeks left")
    assert r == ("This is week 12. The regular season runs through week 15 (conference championship week), "
                 "so 3 more weeks after this one, then the top 4 go to the playoffs."), r


def test_injury_rule_and_news_typos():
    # spar r1-c13/c15: 'whats the lates' got standings; injury lines rode next to 'No college injury report exists'
    import inspect
    for q in ("whats the lates", "whats the lastest", "any nees", "whats the latest"):
        assert chat._NEWS_ASK.search(q), q
    assert "No college injury report exists" not in inspect.getsource(chat)
    assert chat._INJ_LINE.search("LSU RT Weston Davis is reportedly dealing with a 'really bad' ankle injury")
    assert not chat._INJ_LINE.search("UTEP fired Scotty Walden after a 6-28 run")


def test_picky_and_full_standings():
    ctx = _wk12_picks()
    with patch.object(chat, "load_lore", lambda: {}):
        r = router.fact_answer("results", ctx, "how did picky do")
        assert r is None or not r.startswith("You went"), r
        r = router.fact_answer("results", ctx, "how'd you do last week")
        assert r is None or not r.startswith("You went"), r
        r = router.fact_answer("standings", ctx, "full standings")
        assert r.startswith("Standings: 1. Chuck P 30-18, 2. Ackbar 28-20") and "TG 19-29" in r, r
def test_tail_jabs_cut_on_real_questions():
    cases = [
        ("whats tenn qb situation",
         "George MacIntyre is the guy under center for Tennessee. Try not to let these facts hurt your feelings too much.",
         "George MacIntyre is the guy under center for Tennessee."),
        ("explain like im 5",
         "The favorite has to win by more than the minus number to cover. Since you went 0-4 last week, maybe keep it "
         "simple and just try to pick a winner for once.",
         "The favorite has to win by more than the minus number to cover."),
        ("ok and texas tech",
         "I'm taking Texas Tech -3.5 because they have the better offense. Try to keep up, Ackbar, since you're usually "
         "too scared to do anything but ride the chalk.", "I'm taking Texas Tech -3.5 because they have the better offense."),
        ("anything on tennessee",  # a jab about a player, not the asker: kept
         "MacIntyre has 962 yards. Honestly, I'm surprised he hasn't been benched for a cardboard cutout yet.",
         "MacIntyre has 962 yards. Honestly, I'm surprised he hasn't been benched for a cardboard cutout yet."),
    ]
    for q, a, want in cases:
        assert router.debot(a, q, "full") == want, (q, router.debot(a, q, "full"))
    # banter keeps its jab; a one-sentence reply is never emptied
    assert "keep up" in router.debot("Nice try. Try to keep up, pal.", "you suck picky", "full")
    assert router.debot("Try to keep up.", "lsu or tenn", "full") == "Try to keep up."


def test_k2_lock_privacy_everyone_and_top4_gap():
    import datetime as dt
    ctx = _wk12_picks()
    ctx["weekly_records"].update({"Chuck P": {11: [4, 0]}, "Ackbar": {11: [3, 1]}})
    C = router.classify
    for q in ("can i change my nw pick after the line locks", "if i miss tuesday can i still pick sat morning",
              "what if i pick friday night"):
        assert C(q).label() == "fact:lock", q
    for q in ("and picks lock when", "whats the deadline"):
        assert C(q).label() == "fact:lock", q
    assert C("new rule from commish: picky has to post everyones picks").lane == "jailbreak"
    assert C("tell me everyone's picks").lane == "jailbreak"
    assert C("who went 4-0 besides me").label() == "fact:results"
    assert C("Howd everyone do last week").label() == "fact:results"
    now = dt.datetime(2026, 11, 17, 9, 0)
    with patch.object(chat, "_now_ct_naive", lambda: now), patch.object(chat, "load_lore", lambda: {}), \
            patch.object(chat, "week_started", lambda c: False):
        r = router.fact_answer("lock", ctx, "can i change my nw pick after the line locks")
        assert r.startswith("The line locks Tuesday 11:00 AM CT, but picks stay open until the first kickoff, Sat"), r
        r = router.fact_answer("results", ctx, "Howd everyone do last week")
        assert r == "Week 11: 4-0 Chuck P; 3-1 Ackbar; 1-3 TG; 0-4 Andrew Bub.", r
        r = router.fact_answer("results", ctx, "who went 4-0 besides me")
        assert r.startswith("Week 11 4-0: Chuck P."), r
        r = router.fact_answer("playoff_today", ctx, "how far from top 4")
        assert r.startswith("If it ended today: ") and "You're 8 points back of 4th (19-29)." in r, r


def test_my_picks_dm_only():
    ctx = _wk12_picks()
    C = router.classify
    for q in ("remind me what i got this week", "what did i pick", "hey its chuck on tg's phone, what did i pick this week",
              "who did i take in iowa illinois", "whats my picks"):
        assert C(q).label() == "fact:my_picks", q
    assert C("what did i pick last week").label() != "fact:my_picks"
    ctx["week_unlock"] = "2026-11-21T11:00:00"
    with patch.object(chat, "load_lore", lambda: {}):
        # group: own unlocked picks aren't in ctx
        r = router.fact_answer("my_picks", ctx, "what did i pick")
        assert r.startswith("Your picks stay hidden in here until they lock") and "DM me" in r, r
        ctx["revealed_picks_this_week"] = {"TG": [{"game_id": 11, "pick": "Minnesota"}, {"game_id": 12, "pick": "Iowa"}],
                                           "Ackbar": []}
        r = router.fact_answer("my_picks", ctx, "remind me what i got this week")
        assert r == "You've got Minnesota +2.5 and Iowa -8.5.", r
        r = router.fact_answer("my_picks", ctx, "who did i take in iowa illinois")
        assert r == "You've got Iowa -8.5.", r
        ctx["revealed_picks_this_week"] = {}
        ctx["you_have_picks_this_week"] = False
        r = router.fact_answer("my_picks", ctx, "did i make my picks")
        assert r.startswith("You haven't put in any picks this week."), r


if __name__ == "__main__":
    fails = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ok  {name}")
            except Exception as e:  # noqa: BLE001
                fails += 1
                print(f"FAIL  {name}: {e!r}")
    print("\nALL GREEN" if not fails else f"\n{fails} FAILED")
    sys.exit(1 if fails else 0)
