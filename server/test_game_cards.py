"""
Deterministic tests for game_cards.py (offline pick-advice cards): what's due
when (slate set, Tue 11 AM CT line lock, Fri 5 PM, Sat 7 AM, dossier rebuilt,
never after kickoff), confidence wording that scales with the edge and the
backtest, game theory from GRADED picks only, the backtest math against the
league line (pushes to the away side), the Opus case with its fallbacks
(Anthropic down = the card still works), and the live rider (latest user
turn only, the asker's own game-theory line only, nothing for a locked week).
No network, no GPU, no claude -p.

Run:  ./.venv/bin/python3 -I test_game_cards.py
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
import tempfile
from pathlib import Path

os.environ["PICKY_CFBD_DB"] = "/nonexistent/cfbd.sqlite"  # pick_brief.raw() -> None: synthetic data only
TMP = Path(tempfile.mkdtemp(prefix="picky-cards-"))
os.environ["PICKY_GAME_CARD_DIR"] = str(TMP)
sys.path.insert(0, str(Path(__file__).resolve().parent))
import chat  # noqa: E402
import game_cards as gc  # noqa: E402
import picky_claude  # noqa: E402
import re  # noqa: E402

chat.MASCOT_ANY = re.compile(r"\b(Sooners|Aggies)s?\b")  # analyst's nickname check without the Supabase fetch

CT = gc.CT
G = {"GameId": 9001, "Away": "Texas A&M", "Home": "Oklahoma", "line": -7.5, "Status": None, "tiebreaker": None,
     "fpi_margin": None, "Start (CT)": "2026-11-21T11:00:00", "week": 12}
NOTES = {"market": {"home_now": -10.5, "home_open": -7.5, "over_under": 44.5, "book": "DraftKings"}}


def at(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s).replace(tzinfo=CT)


def card(now="2026-11-18T09:00", notes=NOTES, table=None, tend=None, bt=None, dossier=None):
    return gc.build_card(G, 2026, 12, notes=notes, idx=None, extras=None, dossier=dossier, news=None,
                         table=table or [], tend=tend or {}, bt=bt, now=at(now))


BT = {"span": "2025-26", "strategies": {"value_1.5+": {"w": 20, "n": 33, "pct": 0.606},
                                        "value_0.5-1": {"w": 20, "n": 43, "pct": 0.465}}}


# ---- schedule -----------------------------------------------------------------------------------

def test_due_new_lock_fri_sat_dossier_and_never_after_kick():
    assert gc.due_reason(G, None, None, at("2026-11-15T20:00")) == "new"
    c = {"generated_at": at("2026-11-16T08:00").isoformat()}
    assert gc.due_reason(G, c, None, at("2026-11-17T10:59")) is None
    assert gc.due_reason(G, c, None, at("2026-11-17T11:05")) == "lock"  # site: Tue 11:00 AM CT
    c = {"generated_at": at("2026-11-17T11:05").isoformat()}
    assert gc.due_reason(G, c, None, at("2026-11-20T17:01")) == "fri"
    c = {"generated_at": at("2026-11-20T17:01").isoformat()}
    assert gc.due_reason(G, c, None, at("2026-11-21T07:01")) == "sat"
    c = {"generated_at": at("2026-11-21T07:01").isoformat(), "dossier_at": "a"}
    assert gc.due_reason(G, c, {"generated_at": "b"}, at("2026-11-21T08:00")) == "dossier"
    assert gc.due_reason(G, c, {"generated_at": "a"}, at("2026-11-21T08:00")) is None
    assert gc.due_reason(G, None, None, at("2026-11-21T11:01")) is None  # kicked off
    assert gc.due_reason({**G, "Status": "final"}, None, None, at("2026-11-15T20:00")) is None


def test_plist_schedule_and_no_maintenance_mode():
    p = (Path(__file__).resolve().parent / "com.pickem.game-cards.plist").read_text()
    assert "game_cards.py" in p and "<string>run</string>" in p and "StartInterval" in p
    src = (Path(__file__).resolve().parent / "game_cards.py").read_text().lower()
    assert "maintenance" not in src and "training" not in src  # no take-Picky-offline option in this work


# ---- the card -----------------------------------------------------------------------------------

def test_card_locked_line_vs_market_value_and_key_number():
    c = card()
    assert c["league_line"]["locked"] and c["league_line"]["text"].startswith("Oklahoma -7.5")
    assert c["market"]["text"].startswith("Oklahoma -10.5") and c["value"]["side"] == "Oklahoma"
    assert c["value"]["points"] == 3 and "10" in c["value"]["key"]
    assert c["lean"]["label"] == "Oklahoma -7.5" and c["reasons"][0].startswith("line value")
    assert 1 <= len(c["reasons"]) <= gc.MAX_REASONS
    assert c["move"]["why"].startswith("line move: opened Oklahoma -7.5, now Oklahoma -10.5")
    assert "Rivalry game (underdogs historically cover more)" in " ".join(c["no_source"]) or c["no_source"]


def test_before_the_lock_says_not_locked():
    c = card(now="2026-11-16T09:00", notes={"market": {"home_now": -7.5}})
    assert not c["league_line"]["locked"] and c["value"] is None
    assert c["confidence"]["word"] == "coin flip"
    assert "not locked yet, locks Tue 11 AM CT" in "\n".join(gc.card_lines(c, None, True))


def test_confidence_scales_with_edge_and_backtest():
    assert gc.confidence(None, "none", 0, BT)["word"] == "coin flip"
    assert gc.confidence(0.5, "lean", 1.5, BT)["word"] == "slight lean"
    small = gc.confidence(0.5, "lean", 1.5, BT)
    assert small["backtest"] == "plays with this much line value went 20-23 (47%) in this league in 2025-26"
    assert gc.confidence(3, "lean", 3, BT)["word"] == "lean"
    best = gc.confidence(3, "strong", 6, BT)
    assert best["word"] == "best play" and "not a lock" in best["say"] and "20-13 (61%)" in best["backtest"]
    weak_bt = {"span": "2025-26", "strategies": {"value_1.5+": {"w": 9, "n": 18, "pct": 0.5}}}
    assert gc.confidence(3, "strong", 6, weak_bt)["word"] == "lean"  # no backtest support: no 'I like it'
    for c in (gc.confidence(3, "strong", 6, BT), gc.confidence(0.5, "lean", 1, None)):
        assert "lock" not in c["say"].replace("not a lock", "").replace("no lock", "")


# ---- game theory: graded picks only --------------------------------------------------------------

def league_data():
    teams = [{"team_id": i, "team_name": n, "is_exhibition": False} for i, n in enumerate(("Ann", "Bo", "Cy"), 1)]
    games, picks = [], []
    for k in range(10):  # graded: Bo always takes the favorite (home -3), Cy always the dog
        gid = 100 + k
        games.append({"GameId": gid, "cfb_season": 2026, "week": 1 + k % 10, "Away": f"A{k}", "Home": f"H{k}",
                      "AwayPts": 10, "HomePts": 20, "line": -3, "winner": f"H{k}", "Status": "final",
                      "tiebreaker": None, "Start (CT)": f"2026-09-{10 + k:02d}T11:00:00"})
        picks += [{"team_id": 1, "game_id": gid, "pick": f"H{k}"}, {"team_id": 2, "game_id": gid, "pick": f"H{k}"},
                  {"team_id": 3, "game_id": gid, "pick": f"A{k}"}]
    # an UNGRADED game whose picks somehow got in: must never count
    games.append({"GameId": 999, "cfb_season": 2026, "week": 12, "Away": "X", "Home": "Y", "line": -3,
                  "winner": None, "Status": None, "tiebreaker": None})
    picks += [{"team_id": 2, "game_id": 999, "pick": "X"}] * 30
    return {"teams": teams, "games": games, "picks": picks}


def test_tendencies_use_graded_picks_only():
    rows = gc.history_rows(league_data())
    assert rows and all(r["season"] == 2026 and r["pick"] not in ("X", "Y") for r in rows)
    tend = gc.pick_brief.tendencies(rows)
    assert tend["Bo"]["n"] == 10 and tend["Bo"]["fav"] == 1.0 and tend["Cy"]["fav"] == 0.0


def test_member_theory_chase_vs_protect():
    cg = gc.chat_game(G, 2026, 12)
    table = [{"team": "Ann", "w": 20, "l": 10, "rank": 1}, {"team": "Bo", "w": 18, "l": 12, "rank": 2},
             {"team": "Cy", "w": 16, "l": 14, "rank": 3}, {"team": "Di", "w": 15, "l": 15, "rank": 4},
             {"team": "Ed", "w": 11, "l": 19, "rank": 5}]
    fav = {"n": 20, "fav": 0.8, "home": 0.5, "crowd": 0.5}
    tend = {t: fav for t in ("Ann", "Bo", "Cy", "Di")}
    th = gc.member_theory(cg, table, tend)
    assert "protect it, match the field" in th["Ann"]["text"]  # only one rival with a habit: no crowd to match
    assert th["Ann"]["side"] is None
    assert "chasing, so fade the crowd" in th["Ed"]["text"] and th["Ed"]["side"] == "Texas A&M"
    assert "not this week's" in th["Ed"]["text"]


# ---- backtest -----------------------------------------------------------------------------------

def test_backtest_against_the_league_line_with_pushes_to_away():
    data = {"games": [
        # league home -3, market close home -6: value on home; home covers
        {"GameId": 1, "cfb_season": 2025, "week": 3, "Away": "A", "Home": "H", "line": -3, "winner": "H",
         "AwayPts": 10, "HomePts": 20, "tiebreaker": None},
        # league home -7, close -7.5 (0.5 gap, crosses no key), away covers
        {"GameId": 2, "cfb_season": 2025, "week": 4, "Away": "B", "Home": "J", "line": -7, "winner": "B",
         "AwayPts": 14, "HomePts": 21, "tiebreaker": None},  # 21-14 on -7 = push, the away side gets it
        # tiebreaker and a playoff week never count
        {"GameId": 3, "cfb_season": 2025, "week": 5, "Away": "C", "Home": "K", "line": -3, "winner": "K",
         "tiebreaker": True},
        {"GameId": 4, "cfb_season": 2025, "week": 16, "Away": "D", "Home": "L", "line": -3, "winner": "L",
         "tiebreaker": None}]}
    lines = [{"id": 1, "lines": [{"provider": "DraftKings", "spread": -6, "spreadOpen": -2.5}]},
             {"id": 2, "lines": [{"provider": "DraftKings", "spread": -7.5, "spreadOpen": -7.5}]}]
    bt = gc.backtest(data, lines)
    s = bt["strategies"]
    assert bt["games"] == 2 and bt["games_with_market"] == 2
    assert s["value_1.5+"]["w"] == 1 and s["value_1.5+"]["n"] == 1      # home -3 vs -6: covered
    assert s["value_crosses_key"]["n"] == 2  # 3 -> 6 crosses 3; 7 vs 7.5 sits on 7 (pick_brief convention)
    assert s["value_0.5-1"]["n"] == 1 and s["value_0.5-1"]["w"] == 0       # value said home J; the push went to B
    assert s["follow_move_1.5+"]["w"] == 1 and s["home"]["w"] == 1 and s["underdog"]["w"] == 1


# ---- the Opus case and its fallbacks --------------------------------------------------------------

def test_case_from_opus_validated_and_cached_by_inputs():
    c = card()
    good = {"pick": "Oklahoma", "confidence": "medium",
            "reasons": [{"text": "The market has Oklahoma at 10.5, so laying 7.5 here is a gift.", "refs": ["S2"]},
                        {"text": "The line moved three points toward Oklahoma since it opened.", "refs": ["S5"]}],
            "counter": {"text": "Oklahoma at 7.5 still has to win by eight.", "refs": ["S1"]},
            "market_missing": {"text": "The league line is stale by three points.", "refs": ["S3"]},
            "quotable": "Free points, take them."}
    calls = []
    gc.add_case(c, None, None, lambda p: calls.append(p) or good)
    assert c["case"]["by"] == "opus" and c["case"]["pick"] == "Oklahoma" and len(calls) == 1
    assert "POOL LINE" in calls[0] and "Oklahoma -7.5" in calls[0]
    c2 = card()
    gc.add_case(c2, None, c, lambda p: calls.append(p) or good)  # same inputs: no second call
    assert len(calls) == 1 and c2["case"]["by"] == "opus"


GOOD_CASE = {"pick": "Oklahoma", "confidence": "medium",
             "reasons": [{"text": "The market has Oklahoma at 10.5, so laying 7.5 here is a gift.", "refs": ["S2"]},
                         {"text": "The line moved three points toward Oklahoma since it opened.", "refs": ["S5"]}],
             "counter": {"text": "Oklahoma at 7.5 still has to win by eight.", "refs": ["S1"]},
             "market_missing": {"text": "The league line is stale by three points.", "refs": ["S3"]},
             "quotable": "Free points, take them."}


def test_opus_case_weekly_after_the_lock_only():
    calls = []
    before = card(now="2026-11-16T09:00")  # Monday: league line not locked yet
    gc.add_case(before, None, None, lambda p: calls.append(p) or GOOD_CASE)
    assert not calls and before["case"] is None and before["reasons"]  # code card stands alone
    c = card()  # Wednesday, locked: the week's one Opus case
    gc.add_case(c, None, None, lambda p: calls.append(p) or GOOD_CASE)
    assert len(calls) == 1 and c["case"]["league_home"] == -7.5
    later = card(now="2026-11-20T17:05")  # Friday refresh: kept, no second call
    gc.add_case(later, None, c, lambda p: calls.append(p) or GOOD_CASE)
    assert len(calls) == 1 and later["case"]["by"] == "opus"


def test_kept_case_only_while_true_and_fresh():
    c = card()
    gc.add_case(c, None, None, lambda p: GOOD_CASE)
    moved = card(now="2026-11-19T09:00", notes={"market": dict(NOTES["market"], home_now=-9.5)})
    kept = gc.keep_case(c["case"], moved, gc.case_items(moved, None), c)
    assert [r["text"] for r in kept["reasons"]] == [GOOD_CASE["reasons"][1]["text"]]  # '10.5' is gone from the brief
    assert kept["counter"]["text"].startswith("Oklahoma at 7.5")  # the pool line still says 7.5
    relined = gc.build_card(dict(G, line=-8.5), 2026, 12, notes=NOTES, idx=None, extras=None, dossier=None, news=None,
                            table=[], tend={}, bt=None, now=at("2026-11-19T09:00"))
    assert gc.keep_case(c["case"], relined, gc.case_items(relined, None), c) is None  # new league line
    old = card(now="2026-11-25T09:00")
    assert gc.keep_case(c["case"], old, gc.case_items(old, None), c) is None  # past 6 days


def test_failed_opus_case_retried_once_a_day():
    calls = []

    def boom(p):
        calls.append(p)
        raise RuntimeError("claude exit 1")
    c = card()
    gc.add_case(c, None, None, boom)
    c2 = card(now="2026-11-18T15:00")
    gc.add_case(c2, None, c, boom)
    assert len(calls) == 1 and c2["case"] is None and c2["case_tried_at"] == c["case_tried_at"]
    c3 = card(now="2026-11-19T09:30")
    gc.add_case(c3, None, c2, boom)
    assert len(calls) == 2


def test_anthropic_down_card_still_works():
    os.environ["PICKY_NO_ANTHROPIC"] = "1"
    try:
        c = card()
        calls = []

        def research(prompt):
            calls.append(prompt)
            picky_claude.guard()  # what dossier.run_claude does first
        gc.add_case(c, None, None, research)
        assert c["case"] is None and c["lean"]["label"] == "Oklahoma -7.5" and c["reasons"] and not calls
        gc.add_case(c, None, None, research, force=True)  # on demand: the kill switch still wins
        assert not calls and c["case"] is None
        prev = dict(card(), case=dict(GOOD_CASE, by="opus", generated_at=c["generated_at"], league_home=-7.5))
        gc.add_case(c, None, prev, research)
        assert c["case"]["by"] == "opus"  # the last good case, still true, needs no Anthropic
        c3 = card()
        gc.add_case(c3, {"analyst_take": {"pick": "Oklahoma", "reasons": [{"text": "y"}],
                                          "generated_at": c3["generated_at"]}}, None, research)
        assert c3["case"]["by"] == "dossier"
        c4 = card()
        gc.add_case(c4, {"analyst_take": {"pick": "Oklahoma", "reasons": [{"text": "y"}],
                                          "generated_at": "2026-11-01T09:00:00-06:00"}}, None, research)
        assert c4["case"] is None  # an old take never rides
        assert gc.card_lines(c3, None, False)[0].startswith("GAME CARD")
    finally:
        os.environ.pop("PICKY_NO_ANTHROPIC", None)


class FakeDB:
    def get(self, q):
        if q.startswith("all_games?select=cfb_season"):
            return [{"cfb_season": 2026}]
        if q.startswith("all_games?select=week"):
            return [{"week": 12}]
        return [dict(G), dict(G, GameId=9002, tiebreaker=True)]


def test_run_end_to_end_with_anthropic_down():
    base = Path(tempfile.mkdtemp(prefix="picky-cards-run-"))
    os.environ["PICKY_NO_ANTHROPIC"] = "1"
    try:
        def research(prompt):
            picky_claude.guard()
        res = gc.run(base=base, db=FakeDB(), data=league_data(), research=research, now=at("2026-11-18T09:00"),
                     inputs=({9001: NOTES}, None, {}))
        assert res["ok"] and res["built"] == [9001], res  # the tiebreaker gets no card
        c = json.loads((base / "12" / "9001.json").read_text())
        assert c["trigger"] == "new" and c["lean"]["label"] == "Oklahoma -7.5" and c["case"] is None
        assert set(c["theory"]) == {"Ann", "Bo", "Cy"}
        assert (base / "backtest.json").exists()
        again = gc.run(base=base, db=FakeDB(), data=league_data(), research=research, now=at("2026-11-18T10:00"),
                       inputs=({9001: NOTES}, None, {}))
        assert again["built"] == []  # nothing due an hour later
    finally:
        os.environ.pop("PICKY_NO_ANTHROPIC", None)


# ---- live rider -----------------------------------------------------------------------------------

def ctx_for(now_naive: dt.datetime, asker="Ed"):
    g = {"id": 9001, "away": "Texas A&M", "home": "Oklahoma", "line": -7.5, "start": "2026-11-21T11:00:00",
         "status": None, "winner": None, "tiebreaker": None}
    return {"current_week": 12, "games_this_week": [g], "you": {"team_name": asker, "team_id": 5}}


def write_card():
    table = [{"team": "Ann", "w": 20, "l": 10, "rank": 1}, {"team": "Ed", "w": 11, "l": 19, "rank": 2}]
    tend = {"Ann": {"n": 20, "fav": 0.8, "home": 0.5, "crowd": 0.5}}
    c = card(now="2026-11-18T09:00", table=table, tend=tend, bt=BT)
    p = gc.path_for(12, 9001)
    p.parent.mkdir(parents=True, exist_ok=True)
    c["generated_at"] = dt.datetime.now(CT).isoformat(timespec="seconds")
    p.write_text(json.dumps(c))
    return c


def test_card_note_in_the_user_turn_for_the_asker_only():
    write_card()
    orig = chat._now_ct_naive
    try:
        chat._now_ct_naive = lambda: dt.datetime(2026, 11, 18, 9, 0)
        chat.week_locked, wl = (lambda ctx: False), chat.week_locked
        note = gc.card_note(ctx_for(None), "who do you like in Texas A&M vs Oklahoma?")
        assert note.startswith("GAME CARD, Texas A&M at Oklahoma"), note
        assert "I'd take Oklahoma -7.5" in note and "League line: Oklahoma -7.5 (locked Tue 11 AM CT)" in note
        assert "For Ed (game theory" in note and "For Ann" not in note
        # Ed is chasing and the crowd isn't predictable from one rival: no contrarian muddle, one clear call
        assert "Bottom line:" in note and note.count("THE PLAY:") == 1
        assert "Honesty check:" in note and "never call it a lock" in note
        # not an opinion question / another asker / line changed since the card: no card
        assert gc.card_note(ctx_for(None), "what channel is Texas A&M vs Oklahoma on?") == ""
        assert "For Ed" not in gc.card_note(ctx_for(None, asker="Zed"), "who do you like in Texas A&M vs Oklahoma?")
        ctx = ctx_for(None)
        ctx["games_this_week"][0]["line"] = -8.5
        assert gc.card_note(ctx, "who do you like in Texas A&M vs Oklahoma?") == ""
        chat.week_locked = lambda ctx: True  # picks locked at first kickoff: no advice
        assert gc.card_note(ctx_for(None), "who do you like in Texas A&M vs Oklahoma?") == ""
    finally:
        chat._now_ct_naive = orig
        chat.week_locked = wl


def test_theory_line_is_one_clear_call():
    table = [{"team": "Ann", "w": 20, "l": 10, "rank": 1}, {"team": "Bo", "w": 18, "l": 12, "rank": 2},
             {"team": "Cy", "w": 16, "l": 14, "rank": 3}, {"team": "Ed", "w": 11, "l": 19, "rank": 4}]
    fav = {"n": 20, "fav": 0.8, "home": 0.5, "crowd": 0.5}
    c = card(table=table, tend={t: fav for t in ("Ann", "Bo", "Cy")})
    txt = "\n".join(gc.card_lines(c, "Ed", True))
    assert c["lean"]["side"] == "Oklahoma" and c["theory"]["Ed"]["side"] == "Texas A&M"
    assert "the contrarian play is Texas A&M +7.5: give your pick (Oklahoma -7.5) first" in txt, txt
    txt = "\n".join(gc.card_lines(c, "Ann", True))
    assert "contrarian" not in txt


def test_card_never_in_the_prefix():
    import inspect
    assert "card_note" not in inspect.getsource(chat.prompt_prefix)
    assert "card_note" not in inspect.getsource(chat._system_tail)


if __name__ == "__main__":
    fails = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok   {name}")
            except Exception as e:  # noqa: BLE001
                fails += 1
                import traceback
                traceback.print_exc()
                print(f"FAIL {name}: {e!r}")
    sys.exit(1 if fails else 0)
