"""
Fact store v1 (factstore.py, plan 2.3 + section 5 phase 2 check).

- The store builds from a frozen fixture (public football data only) in under 5 s, atomically.
- One game sheet per game this week and one team sheet per FBS team; every sheet carries a `facts` list of
  {id, kind, subject, value, text, source, as_of}.
- Market: ESPN snapshot history (open / now / move / total move) wins over the CFBD line when fresh.
- Ranks come only from polls that passed the code sanity check (ESPN primary).
- Live finals overlay team records at read time (Saturday night is right before CFBD's next refresh).
- The facts_used contract: a lane answer carries the facts it used; numbers it prints that no fact backs are
  flagged.
- Privacy: no league picks or member data in the store.
- Every store lane reads only factstore + the league ctx (static check of router.STORE_LANES).
- Local cache first; a missing cache is rebuilt from Supabase's copy.

Run:  ./.venv/bin/python3 -I test_factstore.py
"""

from __future__ import annotations

import ast
import copy
import datetime as dt
import inspect
import json
import os
import sys
import tempfile
import textwrap
import time
from pathlib import Path

sys.path.insert(0, ".")
import factstore as FS  # noqa: E402
import live_games as L  # noqa: E402
import market_snapshot as M  # noqa: E402
import polls  # noqa: E402

FIX = Path(__file__).resolve().parent / "eval" / "fixtures" / "factstore"
NOW = dt.datetime(2026, 11, 18, 15, 30, tzinfo=dt.timezone.utc).timestamp()
TMP = Path(tempfile.mkdtemp(prefix="factstore-test-"))
LSU_TENN = "401856754"   # LSU at Tennessee: slate_intel has the Tennessee QB note
UMASS = "401866467"      # Akron at UMass: MASS -3.5, total 47.5 on ESPN in the fixture
FACT_KEYS = {"id", "kind", "subject", "value", "text", "source", "as_of"}


def inputs(**over):
    """Fixture inputs: CFBD datasets, slate_intel, weather, checked ESPN polls, ESPN market snapshots."""
    mdb = TMP / "market.sqlite"
    if not mdb.exists():
        M.record(L.parse_scoreboard(json.loads((FIX / "espn_scoreboard.json").read_text())), NOW - 3600, db=mdb)
    pp = TMP / "polls.json"
    if not pp.exists():
        recs = FS.Inputs.from_fixture(FIX).datasets()["records"]
        polls.refresh(fetch=lambda: json.loads((FIX / "espn_rankings.json").read_text()), records=recs, path=pp,
                      now=NOW)
    inp = FS.Inputs.from_fixture(FIX, market_db=mdb, polls_path=pp, live_path=TMP / "no-live.json")
    for k, v in over.items():
        setattr(inp, k, v)
    return inp


_BUILT: dict = {}


def built():
    if not _BUILT:
        out = TMP / "factstore.json"
        t = time.time()
        summary = FS.build(inputs(), now=NOW, out=out, push=None)
        _BUILT.update(summary=summary, secs=time.time() - t, path=out, store=json.loads(out.read_text()))
    return _BUILT


# --- build ---------------------------------------------------------------------------------------------

def test_builds_under_5_seconds_on_frozen_fixture():
    b = built()
    assert b["secs"] < 5.0, b["secs"]
    assert b["summary"]["games"] >= 50 and b["summary"]["teams"] >= 130, b["summary"]


def test_build_is_atomic_temp_then_rename():
    out = TMP / "atomic.json"
    out.write_text('{"v": "old"}')
    seen = []
    real = os.replace

    def spy(src, dst):
        seen.append((Path(src).name, Path(dst).name, json.loads(Path(dst).read_text())["v"]))
        real(src, dst)
    os.replace = spy
    try:
        FS.build(inputs(), now=NOW, out=out, push=None)
    finally:
        os.replace = real
    assert seen and seen[-1][1] == "atomic.json" and seen[-1][0] != "atomic.json"
    assert seen[-1][2] == "old", "readers see the old file until the rename"
    assert json.loads(out.read_text())["v"] == FS.VERSION


def test_every_fact_has_the_contract_shape():
    st = built()["store"]
    n = 0
    for sheet in list(st["games"].values()) + list(st["teams"].values()):
        for f in sheet["facts"]:
            assert set(f) >= FACT_KEYS, f
            assert f["text"] and f["source"] and f["as_of"], f
            n += 1
    assert n > 1000, n


# --- game sheets ---------------------------------------------------------------------------------------

def test_game_sheet_has_the_fields_lanes_need():
    g = built()["store"]["games"][LSU_TENN]
    assert g["away"] == "LSU" and g["home"] == "Tennessee", g
    for k in ("game_id", "week", "start", "neutral", "conf_game", "venue", "market", "ranks", "records", "weather",
              "availability", "meeting", "facts"):
        assert k in g, k


def test_market_prefers_espn_snapshots_with_open_and_move():
    g = built()["store"]["games"][UMASS]
    m = g["market"]
    assert m["spread_home"] == -3.5 and m["total"] == 47.5, m
    assert m["source"].startswith("ESPN"), m
    assert m["open_spread_home"] is not None and m["move"] is not None and m["total_move"] is not None, m
    assert any(f["id"] == f"market.{UMASS}.spread" for f in g["facts"])


def test_market_falls_back_to_cfbd_line_without_snapshots():
    st = json.loads((TMP / "nosnap.json").read_text()) if (TMP / "nosnap.json").exists() else None
    if st is None:
        FS.build(inputs(market_db=TMP / "empty-market.sqlite"), now=NOW, out=TMP / "nosnap.json", push=None)
        st = json.loads((TMP / "nosnap.json").read_text())
    m = st["games"][LSU_TENN]["market"]
    assert m["source"].startswith("CFBD") and m["spread_home"] is not None, m


def test_stale_snapshots_lose_to_cfbd():
    out = TMP / "stale.json"
    FS.build(inputs(), now=NOW + 3 * 86400, out=out, push=None)  # snapshots 3 days old: past the hard age
    st = json.loads(out.read_text())
    for g in st["games"].values():
        assert not g["market"].get("source", "").startswith("ESPN"), g["market"]


def test_availability_carries_status_source_and_date():
    g = built()["store"]["games"][LSU_TENN]
    qb = [a for a in g["availability"] if "MacIntyre" in a["text"]]
    assert qb and qb[0]["status"] == "questionable" and qb[0]["source_name"] == "Yahoo Sports", qb
    assert qb[0]["date"] == "2026-11-16" and qb[0]["team"] == "Tennessee"


def test_weather_is_a_forecast_with_as_of():
    games = built()["store"]["games"]
    w = games[UMASS]["weather"]
    assert w and w["temp_f"] == 45.5 and w["as_of"] and w["src"], w


def test_meeting_from_last_season():
    games = built()["store"]["games"]
    with_meeting = [g for g in games.values() if g["meeting"]]
    assert with_meeting, "some week-12 pairs met in 2025"
    m = with_meeting[0]["meeting"]
    assert {"season", "winner", "score", "line", "covered"} <= set(m), m


# --- team sheets ---------------------------------------------------------------------------------------

def test_team_sheet_record_rank_sp_ats_coach():
    t = built()["store"]["teams"]["Georgia"]
    assert t["record"] and t["conf_record"], t
    assert t["ranks"]["CFP"] == 1 and t["ranks"]["AP"] == 1, t["ranks"]
    assert t["sp"]["rank"] and t["ats"]["w"] is not None and t["coach"], t
    assert any(f["kind"] == "rank" and f["value"] == 1 for f in t["facts"])


def test_rejected_poll_never_ranks_anyone():
    pp = TMP / "bad-polls.json"
    d = json.loads((TMP / "polls.json").read_text())
    d["CFP"]["ok"] = False
    pp.write_text(json.dumps(d))
    out = TMP / "badpoll.json"
    FS.build(inputs(polls_path=pp), now=NOW, out=out, push=None)
    t = json.loads(out.read_text())["teams"]["Georgia"]
    assert "CFP" not in t["ranks"] and t["ranks"]["AP"] == 1, t["ranks"]


def test_rankings_reader_serves_checked_espn_cfp():
    FS._memo.clear()
    r = FS.rankings("CFP", path=built()["path"])
    assert r and r["source"] == "ESPN" and r["ranks"][0]["team"] == "Georgia", r


# --- live overlay --------------------------------------------------------------------------------------

def test_live_final_overlays_record_before_cfbd_refresh():
    st = copy.deepcopy(built()["store"])
    g = st["games"][LSU_TENN]
    before = st["teams"]["Tennessee"]["record"]
    live = {"games": {LSU_TENN: {"id": LSU_TENN, "state": "post", "away": {"name": "LSU"}, "home": {"name": "Tennessee"},
                                 "away_score": 10, "home_score": 31}}}
    t = FS.overlay_team(st["teams"]["Tennessee"], st, live)
    w, l = map(int, before.split("-"))
    assert t["record"] == f"{w + 1}-{l}", (before, t["record"])
    if g["conf_game"]:
        cw, cl = map(int, st["teams"]["Tennessee"]["conf_record"].split("-"))
        assert t["conf_record"] == f"{cw + 1}-{cl}"
    lsu = FS.overlay_team(st["teams"]["LSU"], st, live)
    assert lsu["record"].split("-")[1] == str(int(st["teams"]["LSU"]["record"].split("-")[1]) + 1)
    assert FS.overlay_team(st["teams"]["Tennessee"], st, {"games": {}})["record"] == before


# --- facts_used contract -------------------------------------------------------------------------------

def test_answer_carries_facts_used_and_stays_a_string():
    f = [{"id": "ats.duke.overall", "kind": "ats", "subject": "Duke", "value": "8-2", "text": "Duke is 8-2 ATS",
          "source": "CFBD", "as_of": "2026-11-18"}]
    a = FS.answer("Duke is 8-2 ATS.", f)
    assert isinstance(a, str) and a == "Duke is 8-2 ATS." and a.facts_used == f


def test_unsupported_numbers_are_flagged():
    f = [{"id": "m", "kind": "market", "subject": "g", "value": -13.5, "text": "Duke -13.5, total 52.5",
          "source": "ESPN", "as_of": "x"}]
    assert FS.unsupported("Duke -13.5 with a 52.5 total.", f) == []
    assert FS.unsupported("Duke -13.5, and SP+ has them by 10.", f) == ["10"]
    assert FS.unsupported("Duke is 8-2 ATS.", f) == ["8-2"]
    assert FS.unsupported("Duke 8-2.", f, extra=["8-2"]) == []
    assert FS.unsupported("A 3-point win pushes on 3.", f) == [], "key-number talk is general knowledge"


# --- privacy -------------------------------------------------------------------------------------------

def test_store_holds_no_league_or_member_data():
    raw = built()["path"].read_text()
    for bad in ('"picks"', '"pick"', '"team_id"', '"user_id"', '"by_slots"', '"jackpot"', "standings_ytd"):
        assert bad not in raw, bad


# --- lanes read only the store + league ctx ------------------------------------------------------------

FORBIDDEN = {"slate_cards", "cfbd", "slate_intel", "news", "_news", "espn", "sqlite3", "httpx", "open", "Path",
             "json", "polls", "market_snapshot", "live_games", "weather"}


def test_every_store_lane_reads_only_factstore_and_ctx():
    import router
    assert len(router.STORE_LANES) >= 8, router.STORE_LANES
    bad = {}
    for fn in router.STORE_LANES:
        tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        hit = sorted(names & FORBIDDEN)
        if hit:
            bad[fn.__name__] = hit
    assert not bad, bad


def test_store_lanes_cover_the_football_fact_lanes():
    import router
    names = {f.__name__ for f in router.STORE_LANES}
    assert {"keynum_text", "slate_move_text", "h2h_text", "conf_race_text", "lookahead_slate_text", "interim_text",
            "career_ats_text", "qb_status_text"} <= names, names


def test_lanes_answer_from_a_built_store():
    """keynum + slate_move answer from the fixture store (market numbers come back with facts_used)."""
    import router
    FS.use(built()["path"], inputs(), now=NOW)
    try:
        out = router.keynum_text({}, "which lines are on a key number this week?")
        assert out and getattr(out, "facts_used", None), out
        assert FS.unsupported(out, out.facts_used) == [], (out, FS.unsupported(out, out.facts_used))
    finally:
        FS.use(None, None)


# --- local cache first, Supabase cold start --------------------------------------------------------------

def test_missing_local_cache_rebuilds_from_supabase_copy():
    st = built()["store"]
    rows = [{"kind": "game", "key": k, "body": v} for k, v in st["games"].items()]
    rows += [{"kind": "team", "key": k, "body": v} for k, v in st["teams"].items()]
    rows += [{"kind": "meta", "key": "meta", "body": {k: v for k, v in st.items() if k not in ("games", "teams")}}]
    path = TMP / "cold" / "factstore.json"
    FS._memo.clear()
    got = FS.load(path=path, puller=lambda kind: rows)
    assert got and got["games"][LSU_TENN]["home"] == "Tennessee" and path.exists()
    FS._memo.clear()
    assert FS.load(path=TMP / "cold2" / "f.json", puller=lambda kind: (_ for _ in ()).throw(OSError("down"))) is None


def test_push_sends_only_changed_sheets():
    first, second = [], []
    out = TMP / "push.json"
    FS.build(inputs(), now=NOW, out=out, push=lambda rows: first.extend(rows))
    FS.build(inputs(), now=NOW, out=out, push=lambda rows: second.extend(rows))
    assert len(first) > 100 and {r["kind"] for r in first} == {"game", "team", "meta"}, len(first)
    assert second == [], "an unchanged rebuild pushes nothing"


def test_no_anthropic_on_the_store_path():
    for mod in (FS, M, polls):
        src = Path(mod.__file__).read_text().lower()
        assert "claude" not in src and "anthropic" not in src, mod.__name__


def test_three_letter_schools_resolve():
    """'is BYU's QB playing', 'LSU at Tennessee': the shared resolver skips 3-letter words; the store doesn't."""
    FS.use(built()["path"], inputs(), now=NOW)
    try:
        assert "BYU" in FS.teams_named("is BYU's QB playing this week?"), FS.teams_named("is BYU's QB playing?")
        assert set(FS.teams_named("LSU at Tennessee, who covers?")) == {"LSU", "Tennessee"}
        assert FS.teams_named("the guy is out for the year") == []
    finally:
        FS.use(None)


def test_sitfacts_reads_the_store():
    import sitfacts
    FS.use(built()["path"], inputs(), now=NOW)
    try:
        f = sitfacts.live()
        g = f.find_game("LSU at Tennessee, any weather?")
        assert g and g["home"] == "Tennessee" and g["away"] == "LSU", g
        assert f.results("Georgia") == FS.team_rows("Georgia")
    finally:
        FS.use(None)


# --- the store rides existing ticks (no new LaunchAgent) ---------------------------------------------------

def test_news_refresh_tick_rebuilds_the_store():
    import news
    calls = []
    orig_r, orig_f = news.refresh, FS.refresh
    news.refresh = lambda force=False: None
    FS.refresh = lambda: calls.append(1) or {}
    try:
        assert news.main(["refresh"]) == 0
    finally:
        news.refresh, FS.refresh = orig_r, orig_f
    assert calls == [1]


def test_news_refresh_survives_a_store_failure():
    import news
    orig_r, orig_f = news.refresh, FS.refresh
    news.refresh = lambda force=False: None

    def boom():
        raise RuntimeError("store broke")
    FS.refresh = boom
    try:
        assert news.main(["refresh"]) == 0
    finally:
        news.refresh, FS.refresh = orig_r, orig_f


def test_cfbd_refresh_rebuilds_the_store():
    src = (Path(__file__).resolve().parent / "cfbd.py").read_text()
    main = src[src.index('if __name__ == "__main__":'):]
    assert "factstore" in main and "build" in main, "cfbd refresh ends with a store build"


def test_freshness_watches_the_store():
    import freshness
    s = freshness.source("factstore")
    assert s.agent == "com.pickem.news-refresh" and s.monitor == "age" and s.stamp == "json:built_ts", s
    import feed_monitor
    assert not feed_monitor.repairable(s), "a stale store kicks the tick; no model repair path"



def test_upcoming_is_this_weeks_games_only():
    """'This week' = the soonest week with games left: the 9-day horizon must not pull next week's games in
    (Wednesday night it carried week 13's Texas at Texas A&M into the key-number and carousel answers)."""
    p = TMP / "fs-upcoming.json"
    st = json.loads(built()["path"].read_text())
    g0 = next(iter(st["games"].values()))
    later = dict(g0, game_id="999999", week=(g0.get("week") or 12) + 1,
                 start=(dt.datetime.fromtimestamp(NOW, dt.timezone.utc) + dt.timedelta(days=8)).isoformat())
    st["games"]["999999"] = later
    p.write_text(json.dumps(st))
    FS.use(p, inputs(), now=NOW)
    try:
        rows = FS.upcoming()
    finally:
        FS.use(None, None)
    assert rows and len({r["week"] for r in rows}) == 1, sorted({r["week"] for r in rows})
    assert all(r["id"] != "999999" for r in rows)


if __name__ == "__main__":
    fails = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok   {name}")
            except Exception as e:  # noqa: BLE001
                fails += 1
                print(f"FAIL {name}: {e!r}"[:900])
    print("ALL PASS" if not fails else f"{fails} FAILED")
    sys.exit(1 if fails else 0)
