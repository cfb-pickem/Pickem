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
