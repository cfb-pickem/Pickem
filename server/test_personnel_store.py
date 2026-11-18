"""
Personnel in the fact store + the personnel lanes (plan phase 4), on the frozen fixture
(server/eval/fixtures/factstore: ESPN rosters + CFBD box rows for 16 teams, public news headlines, one dossier).

- Team sheets carry the QB room (qb_start ledger + roster class) and the team's normalized availability; game
  sheets carry both teams' availability; every new fact has the contract shape and an as_of.
- The store still builds in under 5 s, and builds from news + ESPN alone (no slate_intel, no dossiers).
- Gateway: player_status (with fresh/stale wording), qb_room, find_player; an old store without personnel fields
  still serves availability from its intel notes.
- Lanes (router.STORE_LANES, facts_used, unsupported() == []): qb_status (status verbatim + source + day; backup,
  value and line move when he's out), backup_qb (starts, record, line, first-start flag), injury_impact (who's
  out, value, unit rank, verdict), and the line-move cause reads normalized availability first.
- Routing for the real-traffic types (backup asks, "any injuries that matter", "lost a lineman").

Run:  ./.venv/bin/python3 -I test_personnel_store.py
"""

from __future__ import annotations

import copy
import datetime as dt
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import factstore as FS  # noqa: E402
import live_games as L  # noqa: E402
import market_snapshot as M  # noqa: E402
import personnel as P  # noqa: E402
import polls  # noqa: E402
import router  # noqa: E402

FIX = Path(__file__).resolve().parent / "eval" / "fixtures" / "factstore"
NOW = dt.datetime(2026, 11, 18, 15, 30, tzinfo=dt.timezone.utc).timestamp()
TMP = Path(tempfile.mkdtemp(prefix="personnel-store-test-"))
LSU_TENN, UNT_TULANE, UCLA_MICH, IND_WASH = "401856754", "401862835", "401858528", "401858535"
FACT_KEYS = {"id", "kind", "subject", "value", "text", "source", "as_of"}


def inputs(**over):
    mdb = TMP / "market.sqlite"
    if not mdb.exists():
        M.record(L.parse_scoreboard(json.loads((FIX / "espn_scoreboard.json").read_text())), NOW - 3600, db=mdb)
    pp = TMP / "polls.json"
    if not pp.exists():
        recs = FS.Inputs.from_fixture(FIX).datasets()["records"]
        polls.refresh(fetch=lambda: json.loads((FIX / "espn_rankings.json").read_text()), records=recs, path=pp, now=NOW)
    inp = FS.Inputs.from_fixture(FIX, market_db=mdb, polls_path=pp, live_path=TMP / "no-live.json")
    for k, v in over.items():
        setattr(inp, k, v)
    return inp


_B: dict = {}


def built(name="main", **over):
    if name not in _B:
        out = TMP / f"{name}.json"
        t = time.time()
        FS.build(inputs(**over), now=NOW, out=out, push=None)
        _B[name] = {"secs": time.time() - t, "path": out, "store": json.loads(out.read_text())}
    return _B[name]


class use:
    """FS.use(store) for the duration of a block."""
    def __init__(self, path):
        self.path = path

    def __enter__(self):
        FS.use(self.path, inputs(), now=NOW)

    def __exit__(self, *a):
        FS.use(None, None)


def ok_facts(out):
    assert out and getattr(out, "facts_used", None), out
    bad = FS.unsupported(out, out.facts_used)
    assert bad == [], (out, bad)


# --- the store -----------------------------------------------------------------------------------------------

def test_store_with_personnel_builds_under_5_seconds():
    b = built()
    assert b["secs"] < 5.0, b["secs"]
    assert b["store"]["sources"]["personnel"]["rosters"] == 16, b["store"]["sources"].get("personnel")


def test_team_sheet_has_the_qb_room():
    t = built()["store"]["teams"]["Ohio State"]
    room = {q["name"]: q for q in t["qbs"]}
    s, f = room["Julian Sayin"], room["Luke Fahey"]
    assert (s["starts"], s["su"], s["ats"], s["cls"]) == (7, "6-1", "5-2", "Junior"), s
    assert (f["starts"], f["su"], f["ats"], f["cls"]) == (3, "1-2", "0-3", "Freshman"), f
    assert t["qb_starter"] in room and t["qbs"][0]["name"] == "Julian Sayin"
    assert any(q["name"] == "Brady Geibel" and q["starts"] == 0 for q in t["qbs"]), "roster QBs with no snaps too"
    qf = [x for x in t["facts"] if x["kind"] == "qb_start"]
    assert qf and all(set(x) >= FACT_KEYS and x["as_of"] for x in qf), qf


def test_availability_on_team_and_game_sheets():
    st = built()["store"]
    g = st["games"][LSU_TENN]
    names = {r["player"]: r for r in g["player_status"]}
    assert "George MacIntyre" in names and names["George MacIntyre"]["pos"] == "QB", names.keys()
    assert names["Mike Matthews"]["status"] == "season"
    for r in g["player_status"]:
        assert r["status"] in P.STATUSES and r["source_name"] and r["reported_at"] and r["quote"], r
    assert any(f["kind"] == "availability" and "MacIntyre" in f["text"] for f in g["facts"])
    clem = {r["player"]: r for r in st["teams"]["Clemson"]["player_status"]}
    assert clem["Tait Reynolds"]["status"] == "season", clem.keys()
    for sheet in list(st["games"].values()) + list(st["teams"].values()):
        for f in sheet["facts"]:
            assert set(f) >= FACT_KEYS and f["text"] and f["source"] and f["as_of"], f


def test_builds_from_news_and_espn_alone():
    st = built("bare", slate_intel_path=None, dossier_dir=None)["store"]
    clem = {r["player"]: r for r in st["teams"]["Clemson"]["player_status"]}
    unt = {r["player"]: r for r in st["teams"]["North Texas"]["player_status"]}
    assert clem["Tait Reynolds"]["status"] == "season" and unt["Tayven Jackson"]["status"] == "season", (clem, unt)


def test_store_holds_no_league_or_member_data():
    raw = built()["path"].read_text()
    for bad in ('"picks"', '"team_id"', '"user_id"', '"by_slots"', '"jackpot"', "standings_ytd"):
        assert bad not in raw, bad


def test_store_tick_fetches_rosters_and_survives_a_failure():
    calls = []
    orig = (P.tick, polls.refresh, FS.build)
    polls.refresh = lambda **kw: None
    FS.build = lambda *a, **kw: calls.append("build") or {}
    try:
        P.tick = lambda *a, **kw: calls.append("rosters") or {"fetched": 2, "failed": 0}
        FS.refresh()
        assert calls == ["rosters", "build"], calls

        def boom(*a, **kw):
            raise OSError("espn down")
        P.tick = boom
        calls.clear()
        FS.refresh()
        assert calls == ["build"], "a roster failure never blocks the store build"
    finally:
        P.tick, polls.refresh, FS.build = orig


# --- gateway -------------------------------------------------------------------------------------------------

def test_gateway_player_status_room_and_find_player():
    with use(built()["path"]):
        rows = FS.player_status(team="Clemson")
        tr = next(r for r in rows if r["player"] == "Tait Reynolds")
        assert tr["when"] in ("Monday", "yesterday", "today") and tr["stale"] is False, tr
        assert [q["name"] for q in FS.qb_room("Ohio State")][:2] == ["Julian Sayin", "Luke Fahey"]
        assert FS.find_player("Is Sayin playing Saturday?") == ("Julian Sayin", "Ohio State")
        assert FS.find_player("is tayven jackson playing?") == ("Tayven Jackson", "North Texas")
        assert FS.find_player("who wins Saturday?") is None


def test_teams_in_reads_short_school_names():
    with use(built()["path"]):
        assert FS.teams_in("who's LSU's backup quarterback?") == ["LSU"]
        assert FS.teams_in("Who's starting at QB for Tennessee vs LSU?") == ["Tennessee", "LSU"]
        assert FS.teams_in("who is out for the game") == []


def test_old_store_without_personnel_serves_intel_availability():
    st = copy.deepcopy(built()["store"])
    for t in st["teams"].values():
        t.pop("qbs", None), t.pop("player_status", None), t.pop("qb_starter", None)
    for g in st["games"].values():
        g.pop("player_status", None)
    p = TMP / "old.json"
    p.write_text(json.dumps(st))
    with use(p):
        rows = {r["player"]: r for r in FS.player_status(team="Tennessee")}
        assert rows["George MacIntyre"]["status"] == "questionable", rows


# --- lanes ---------------------------------------------------------------------------------------------------

def test_qb_status_out_starter_already_replaced():
    """Sayin (out for the regular season per the fixture's intel) has been replaced: Fahey's starts, record, ATS."""
    with use(built()["path"]):
        out = router.qb_status_text({}, "Is Sayin playing Saturday? If not, what's the backup done?")
    assert out.startswith("Julian Sayin") and "out for the rest of the regular season" in out and "Luke Fahey" in out, out
    assert "3 starts" in out and "1-2" in out and "0-3" in out, out
    assert "already" in out, "a QB who's missed games is in the number already"
    ok_facts(out)


def test_qb_status_out_gives_backup_value_and_line_move():
    with use(built()["path"]):
        out = router.qb_status_text({}, "Who's starting at QB for North Texas?")
    assert "Tayven Jackson" in out and "out for the rest of the regular season" in out, out
    assert "per " in out and "Next up:" in out and "first start" in out, out
    assert "worth about" in out and "line has moved" in out, out
    ok_facts(out)


def test_next_man_up_is_never_a_qb_who_is_out():
    """Clemson in the fixture: Tait Reynolds done for the year, Christopher Vizzina in the concussion protocol."""
    with use(built()["path"]):
        out = router.qb_status_text({}, "Who's Clemson's QB Friday? Has he ever played?")
        bq = router.backup_qb_text({}, "who's Clemson's backup QB?")
    assert "Tait Reynolds" in out and "Christopher Vizzina" in out, out
    assert "Tait Reynolds" in bq and "Christopher Vizzina" in bq, bq
    for txt in (out, bq):
        for lead in ("Next up: ", "If he can't go: ", "Behind him: ", "backup is "):
            if lead in txt:
                assert not txt.split(lead, 1)[1].startswith("Tait Reynolds"), txt
    ok_facts(out)
    ok_facts(bq)


def test_qb_status_questionable_quotes_source_and_day():
    with use(built()["path"]):
        out = router.qb_status_text({}, "Who's starting at QB for Tennessee vs LSU?")
    assert out.startswith("George MacIntyre"), out
    assert "per " in out and "Faizon Brandon" in out, out
    ok_facts(out)


def _patched(name, fn):
    st = copy.deepcopy(built()["store"])
    fn(st)
    p = TMP / f"{name}.json"
    p.write_text(json.dumps(st))
    return p


def _set_row(st, team, player, **kw):
    for sheet in [st["teams"][team]] + [g for g in st["games"].values() if team in (g["away"], g["home"])]:
        for r in sheet.get("player_status") or []:
            if r["player"] == player:
                r.update(kw)


def test_qb_status_tbd_stays_tbd():
    p = _patched("tbd", lambda st: _set_row(st, "Tennessee", "George MacIntyre", status="TBD",
                                            quote="Heupel hasn't named a starter for LSU.", reported_at="2026-11-17"))
    with use(p):
        out = router.qb_status_text({}, "Who's starting at QB for Tennessee vs LSU?")
    assert "TBD" in out and "Heupel hasn't named a starter for LSU." in out, out
    assert "is starting" not in out and "will start" not in out, out
    ok_facts(out)


def test_qb_status_stale_report_says_last_report():
    p = _patched("stale", lambda st: _set_row(st, "Tennessee", "George MacIntyre", reported_at="2026-11-02"))
    with use(p):
        out = router.qb_status_text({}, "Is George MacIntyre playing Saturday?")
    assert "last report Mon 11/2" in out, out
    ok_facts(out)


def test_backup_qb_lane_with_starts_record_line():
    with use(built()["path"]):
        out = router.backup_qb_text({}, "Who's Ohio State's backup QB?")
    assert "Luke Fahey" in out and "freshman" in out.lower() and "3 starts" in out, out
    assert "1-2" in out and "0-3" in out and "Julian Sayin" in out, "the old starter is out: say so"
    ok_facts(out)


def test_backup_qb_first_start_flag():
    with use(built()["path"]):
        out = router.backup_qb_text({}, "who's LSU's backup quarterback?")
    assert "Landen Clark" in out and "no starts this season" in out and "first start" in out, out
    ok_facts(out)


def test_injury_impact_lists_value_and_verdict():
    with use(built()["path"]):
        out = router.injury_impact_text({}, "Any injuries that matter for Michigan vs UCLA?")
    assert "JJ Buchanan" in out, out
    assert any(v in out for v in ("at the margin", "line-mover", "why it moved")), out
    assert "SP+" in out, out
    ok_facts(out)


def test_injury_impact_position_ask_names_the_group():
    with use(built()["path"]):
        out = router.injury_impact_text({}, "Indiana lost a lineman? Does that matter at Washington?")
    assert "Indiana" in out and ("lineman" in out or "OL" in out), out
    ok_facts(out)


def test_line_move_cause_reads_normalized_availability_first():
    with use(built()["path"]):
        out = router.slate_move_text({}, "North Texas -6.5 at Tulane. Some books had it double digits early. What changed?")
    assert "Tayven Jackson" in out, out
    ok_facts(out)


def test_long_quotes_are_cut_at_a_clause():
    long_q = ("Clemson QB Christopher Vizzina is out vs Duke with what Dabo Swinney called a 'pretty severe' concussion; "
              "Swinney added that the staff will see how he responds through the week, that the medical team has the "
              "final say, and that freshman Trent Pearman has taken first-team reps in practice since Sunday afternoon.")
    out = router._q(long_q)
    assert len(out) <= 245 and out.endswith("...") and long_q.startswith(out[:-3].rstrip(" ,;")), out
    assert router._q("Short and sweet") == "Short and sweet."


def test_new_lanes_are_store_lanes():
    names = {f.__name__ for f in router.STORE_LANES}
    assert {"qb_status_text", "backup_qb_text", "injury_impact_text", "slate_move_text"} <= names, names


# --- routing -------------------------------------------------------------------------------------------------

def test_routes_for_personnel_types():
    with use(built()["path"]):
        for q, kind in [("Who's Ohio State's backup QB?", "backup_qb"),
                        ("who's LSU's backup quarterback?", "backup_qb"),
                        ("how has Clemson's backup quarterback done this year?", "backup_qb"),
                        ("who's QB2 at LSU", "backup_qb"),
                        ("Any injuries that matter for Michigan vs UCLA?", "injury_impact"),
                        ("Indiana lost a lineman? Does that matter at Washington?", "injury_impact"),
                        ("Any injuries I should know about for Texas Tech at Baylor?", "injury_impact"),
                        ("who's out for Tennessee this week?", "injury_impact"),
                        ("Is Sayin playing Saturday? If not, what's the backup done?", "qb_status"),
                        ("Who's starting at QB for Tennessee vs LSU?", "qb_status")]:
            got = router.classify(q)
            assert got.kind == kind, (q, got.label(), got.why)
        for q in ["who's the best quarterback in this week's games?", "you're a backup bot", "how'd my picks do",
                  "Oklahoma went from -2.5 to -7.5 vs A&M. Who got hurt?"]:
            assert router.classify(q).kind not in ("backup_qb", "injury_impact"), q


def test_routing_doesnt_depend_on_personnel_data():
    """classify keeps the intel-note check for qb_status, so a store with QB rooms doesn't reroute 'qb' asks."""
    old = copy.deepcopy(built()["store"])
    for t in old["teams"].values():
        t.pop("qbs", None), t.pop("player_status", None), t.pop("qb_starter", None)
    for g in old["games"].values():
        g.pop("player_status", None)
    p = TMP / "old-routing.json"
    p.write_text(json.dumps(old))
    asks = ["North Texas QB situation? Should I care?", "is chiles playing saturday?", "Is Minnesota's starting quarterback hurt?",
            "Who's Northwestern's quarterback and how many passing yards does he have?", "Is Sayin playing Saturday?",
            "Who's starting at QB for Tennessee vs LSU?"]
    with use(p):
        before = [router.classify(q).label() for q in asks]
    with use(built()["path"]):
        after = [router.classify(q).label() for q in asks]
    assert before == after, list(zip(asks, before, after))


def test_qb_lane_miss_on_a_non_pool_team_uses_the_store():
    pool = {"games_this_week": [{"id": 1, "away": "Iowa", "home": "Illinois", "line": -3.5}], "current_week": 12}
    with use(built()["path"]):
        out = router.fact_answer("qb", pool, "North Texas QB situation? Should I care?")
        assert out and "Tayven Jackson" in out, out
        assert router.fact_answer("qb", pool, "hows iowa qb") is None, "a pool team with no card note: the full brain"


def test_dispatch_reaches_the_new_lanes():
    with use(built()["path"]):
        out = router.fact_answer("backup_qb", {"games_this_week": []}, "Who's Ohio State's backup QB?")
        assert out and "Luke Fahey" in out, out
        out = router.fact_answer("injury_impact", {"games_this_week": []}, "Any injuries that matter for Michigan vs UCLA?")
        assert out and "JJ Buchanan" in out, out


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
