"""
Big picture lanes (plan phase 5): coach careers + the coach lane, conference races with head-to-head and clinch
scenarios, and the CFP "if it ended today" lane with committed cfp_rules.

- The fact store build carries coach careers (CFBD /coaches, every season), coach news (news.py headlines with
  coach words + the code-made insider wire), each conference's games (CFBD schedule + scores) and its race, and the
  CFP format rules; all with as_of, and the build stays under 5 s on the frozen fixture.
- coach_text, cfp_now_text and conf_race_text read only the fact store + ctx and return factstore.answer(text,
  facts_used) with unsupported() == [].
- The coach lane works with zero Claude-written inputs (news + CFBD alone) and uses slate_intel's coaching notes
  only when they're fresh.
- cfp_rules.json is static data with its source URLs and the date it was verified; validate() checks it.
- Routing: CFP-picture asks go to cfp_now, coach asks to coach; the league's own playoff asks keep their lanes.

Run:  ./.venv/bin/python3 -I test_big_picture.py
"""

from __future__ import annotations

import copy
import datetime as dt
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, ".")
import factstore as FS  # noqa: E402
import test_factstore as TF  # noqa: E402  (the frozen fixture inputs + NOW)

NOW = TF.NOW
TMP = TF.TMP
FIX = TF.FIX


def store_at(name: str, **over) -> Path:
    out = TMP / f"bp-{name}.json"
    if not out.exists():
        FS.build(TF.inputs(**over), now=NOW, out=out, push=None)
    return out


def use(path: Path, **over):
    FS.use(path, TF.inputs(**over), now=NOW)


def check_answer(out):
    assert out, out
    assert getattr(out, "facts_used", None), ("no facts_used", out)
    bad = FS.unsupported(out, out.facts_used)
    assert bad == [], (bad, out)


# --- store build: coach careers ------------------------------------------------------------------------

def test_store_has_full_coach_careers_from_cfbd():
    st = json.loads(store_at("main").read_text())
    k = st["coach_careers"]["Lane Kiffin"]
    stops = [(s["school"], s["from"], s["to"], s["w"], s["l"]) for s in k["stops"]]
    assert ("Ole Miss", 2020, 2025, 55, 19) in stops and ("Tennessee", 2009, 2009, 7, 6) in stops, stops
    assert k["school"] == "LSU" and k["current"] is True and k["as_of"], k


def test_team_sheet_carries_a_coach_career_fact():
    st = json.loads(store_at("main").read_text())
    f = [x for x in st["teams"]["LSU"]["facts"] if x["id"] == "coach.lsu.career"]
    assert f and "Ole Miss 2020-2025 (55-19)" in f[0]["text"] and f[0]["as_of"], f


def test_coach_news_from_headlines_and_wire_with_dates():
    st = json.loads(store_at("main").read_text())
    cn = st["coach_news"]
    assert cn and all(x["date"] and x["source"] and x["title"] for x in cn), cn[:2]
    assert any("Arkansas State" in x["teams"] and "fire" in x["title"].lower() for x in cn)
    assert len({x["title"].lower() for x in cn}) == len(cn), "deduped"
    assert all(dt.datetime.fromisoformat(x["date"]).timestamp() > NOW - 8 * 86400 for x in cn)


def test_store_has_conference_games_and_races_with_as_of():
    st = json.loads(store_at("main").read_text())
    games = st["conf_games"]["Big 12"]
    assert any(g["home_pts"] is not None for g in games) and any(g["home_pts"] is None for g in games), games[:2]
    race = st["conf_races"]["Big 12"]
    assert race["leader"] == "Utah" and race["as_of"], race["leader"]
    agree = sum(1 for r in race["standings"] if f"{r['w']}-{r['l']}" == st["teams"][r["team"]]["conf_record"])
    assert agree >= len(race["standings"]) - 1, [(r["team"], r["w"], r["l"]) for r in race["standings"]]


def test_store_carries_the_cfp_rules():
    st = json.loads(store_at("main").read_text())
    assert st["cfp_rules"]["teams"] == 12 and st["cfp_rules"]["sources"], st.get("cfp_rules")


def test_store_build_still_under_5_seconds():
    t = time.time()
    FS.build(TF.inputs(), now=NOW, out=TMP / "bp-speed.json", push=None)
    assert time.time() - t < 5.0


def test_big_parts_mirror_as_their_own_meta_rows_and_cold_start_merges_them():
    rows = []
    out = TMP / "bp-push.json"
    FS.build(TF.inputs(), now=NOW, out=out, push=lambda r: rows.extend(r))
    metas = {r["key"]: r for r in rows if r["kind"] == "meta"}
    assert {"meta", "coach_careers", "coach_news", "conf_games", "conf_races"} <= set(metas), set(metas)
    assert "conf_races" not in metas["meta"]["body"] and "coach_careers" not in metas["meta"]["body"]
    again = []
    FS.build(TF.inputs(), now=NOW + 60, out=out, push=lambda r: again.extend(r))
    assert not {r["key"] for r in again} & {"coach_careers", "conf_games", "conf_races"}, [r["key"] for r in again]
    FS._memo.clear()
    got = FS.load(path=TMP / "bp-cold" / "f.json", puller=lambda kind: rows)
    assert got["conf_races"]["Big 12"]["leader"] == "Utah" and got["coach_careers"]["Lane Kiffin"], list(got)[:12]


# --- cfp_rules: committed data + validator --------------------------------------------------------------

def test_committed_cfp_rules_validate():
    import cfp_rules
    d = cfp_rules.load()
    assert cfp_rules.validate(d) == [], cfp_rules.validate(d)
    assert d["season"] == 2026 and d["byes"] == [1, 2, 3, 4], d
    assert all(s["url"].startswith("https://") and s["verified"] for s in d["sources"]), d["sources"]


def test_validator_catches_broken_rules():
    import cfp_rules
    d = cfp_rules.load()
    bad = copy.deepcopy(d)
    bad["teams"] = 14
    assert cfp_rules.validate(bad), "auto bids + at-larges must add to the field"
    bad = copy.deepcopy(d)
    bad["sources"] = []
    assert cfp_rules.validate(bad), "a source is required"
    bad = copy.deepcopy(d)
    bad["first_round"] = [[5, 12], [6, 11], [7, 10]]
    assert cfp_rules.validate(bad), "every non-bye seed plays once"
    bad = copy.deepcopy(d)
    bad["releases"] = ["2026-11-24", "2026-11-10"]
    assert cfp_rules.validate(bad), "release dates in order"


# --- gateway -------------------------------------------------------------------------------------------

def test_conf_race_gateway_applies_live_finals():
    p = store_at("main")
    st = json.loads(p.read_text())
    g = next(x for x in st["conf_games"]["Big 12"] if x["home_pts"] is None)
    live = TMP / "bp-live.json"
    live.write_text(json.dumps({"games": {g["id"]: {"id": g["id"], "state": "post", "away": {"name": g["away"]},
                                                    "home": {"name": g["home"]}, "away_score": 3,
                                                    "home_score": 30}}}))
    use(p, live_path=live)
    try:
        r = FS.conf_race("Big 12")
        home = next(x for x in r["standings"] if x["team"] == g["home"])
        base = next(x for x in st["conf_races"]["Big 12"]["standings"] if x["team"] == g["home"])
        assert home["w"] == base["w"] + 1, (home, base)
    finally:
        FS.use(None, None)


def test_coaching_notes_only_when_fresh():
    p = store_at("main")
    use(p)
    try:
        assert FS.coaching_notes(), "fixture slate_intel is fresh"
    finally:
        FS.use(None, None)
    stale = TMP / "bp-stale-intel.json"
    d = json.loads((FIX / "slate_intel.json").read_text())
    d["fetched_at"] = "2026-10-01T00:00:00+00:00"
    stale.write_text(json.dumps(d))
    p2 = store_at("stale", slate_intel_path=stale)
    use(p2)
    try:
        assert FS.coaching_notes() == [], "a stale Opus file is ignored"
    finally:
        FS.use(None, None)


# --- coach lane ----------------------------------------------------------------------------------------

def _coach(q, name="main", **over):
    import big_picture
    use(store_at(name, **over))
    try:
        return big_picture.coach_text({}, q)
    finally:
        FS.use(None, None)


def test_coach_lane_career_stops_and_this_weeks_line():
    out = _coach("What's Lane Kiffin's coaching history?")
    check_answer(out)
    assert "Ole Miss 2020-2025 (55-19)" in out and "Tennessee 2009 (7-6)" in out, out
    assert "LSU" in out and "Tennessee" in out and "-1.5" in out, out  # LSU plays at Tennessee this week


def test_coach_lane_going_back_to_a_former_stop():
    out = _coach("Is Kiffin going back to Ole Miss?")
    check_answer(out)
    assert "Ole Miss" in out and "2020-2025" in out, out


def test_coach_lane_hot_seat_puts_this_weeks_games_first():
    out = _coach("who's on the hot seat right now?")
    check_answer(out)
    assert "Arkansas State" in out and "Louisiana Tech" in out, out


def test_coach_lane_works_with_no_claude_inputs():
    out = _coach("any coaching carousel news that matters for this week's games?", name="noclaude",
                 slate_intel_path=None)
    check_answer(out)
    assert "Arkansas State" in out, out


def test_coach_lane_uses_fresh_coaching_notes():
    out = _coach("any coaching carousel news that matters for this week's games?")
    check_answer(out)
    assert "interim" in out.lower(), out


def test_coach_lane_named_coach_missing_from_data_steps_aside():
    assert _coach("Is Vince Lombardi getting fired?") is None


# --- conf_race lane ------------------------------------------------------------------------------------

def _router_lane(fn_name, q, name="main", **over):
    import router
    use(store_at(name, **over))
    try:
        return getattr(router, fn_name)({}, q)
    finally:
        FS.use(None, None)


def test_conf_race_says_who_controls_it_and_this_weeks_games():
    out = _router_lane("conf_race_text", "Big 12 title race, who's in control?")
    check_answer(out)
    assert out.startswith("Utah leads the Big 12 at 7-0"), out
    assert "control" in out or "clinched" in out, out
    assert "This week:" in out, out


def test_conf_race_gateway_recomputes_a_stale_stored_race():
    """The stored race is whatever code built the store last; the lane recomputes from conf_games (cheap) so a
    clinch-math fix shows the moment it ships, not on the builder's next restart."""
    st = json.loads(store_at("main").read_text())
    st["conf_races"]["Big 12"]["standings"] = st["conf_races"]["Big 12"]["standings"][:1]
    st["conf_races"]["Big 12"]["standings"][0] = dict(st["conf_races"]["Big 12"]["standings"][0], status="stale")
    p = TMP / "bp-stale-race.json"
    p.write_text(json.dumps(st))
    use(p)
    try:
        r = FS.conf_race("Big 12")
        assert len(r["standings"]) > 1 and r["standings"][0]["status"] != "stale", r["standings"][:1]
    finally:
        FS.use(None, None)


def test_conf_race_reads_like_a_text_not_a_report():
    out = _router_lane("conf_race_text", "Big 12 title race, who's in control?")
    check_answer(out)
    for label in ("Who controls it:", "Scenarios:"):
        assert label not in out, out
    assert len(out) <= 600, (len(out), out)


def test_conf_race_tie_never_guesses_a_tiebreaker():
    out = _router_lane("conf_race_text", "AAC title race?")
    check_answer(out)
    assert "further tiebreakers" in out or "head-to-head" in out, out


def test_conf_race_sun_belt_says_divisions():
    out = _router_lane("conf_race_text", "Sun Belt title race?")
    if out:
        check_answer(out)
        assert "division" in out.lower(), out


# --- cfp_now lane --------------------------------------------------------------------------------------

def _cfp(q):
    import big_picture
    use(store_at("main"))
    try:
        return big_picture.cfp_now_text({}, q)
    finally:
        FS.use(None, None)


def test_cfp_if_it_ended_today():
    out = _cfp("Who's in the College Football Playoff if it ended today?")
    check_answer(out)
    for t in ("Georgia", "Utah", "Alabama", "UCLA", "Miami", "Boise State", "Notre Dame", "Wake Forest"):
        assert t in out, (t, out)
    assert "Texas Tech" in out and "Oregon" in out, "first out / bubble"
    assert "Nov 24" in out, "next release"


def test_cfp_seeds_are_straight_by_ranking_with_byes():
    import big_picture
    use(store_at("main"))
    try:
        f = big_picture.cfp_field()
    finally:
        FS.use(None, None)
    assert [t for _, t in f["seeds"]] == ["Georgia", "Utah", "Alabama", "UCLA", "Texas", "Notre Dame", "Miami",
                                          "Indiana", "Tennessee", "BYU", "Wake Forest", "Boise State"], f["seeds"]
    assert f["g6"] == "Boise State" and f["first_out"][:2] == ["Texas Tech", "Oregon"], f
    assert set(f["auto"]) == {"Georgia", "Utah", "UCLA", "Miami", "Boise State", "Notre Dame"}, f["auto"]


def test_cfp_without_a_checked_ranking_steps_aside():
    import big_picture
    p = TMP / "bp-nocfp.json"
    st = json.loads(store_at("main").read_text())
    st["rankings"] = {}
    p.write_text(json.dumps(st))
    import polls
    FS.use(p, TF.inputs(), now=NOW)
    real, polls.serve = polls.serve, (lambda kind, data=None: None)  # no checked CFP ranking anywhere
    try:
        assert big_picture.cfp_now_text({}, "who's in the CFP if it ended today?") is None
    finally:
        polls.serve = real
        FS.use(None, None)


# --- routing + store lanes -----------------------------------------------------------------------------

def test_cfp_picture_asks_route_to_cfp_now():
    import router
    for q in ("Who's in the College Football Playoff if it ended today?", "what does the CFP field look like right now",
              "who's on the CFP bubble?", "cfp bracket as it stands?", "who gets the CFP byes right now"):
        assert router.fact_intent(q) and router.fact_intent(q)[0] == "cfp_now", (q, router.fact_intent(q))


def test_league_playoff_asks_keep_their_lanes():
    import router
    assert router.fact_intent("If the season ended today, who makes the playoffs?")[0] == "playoff_today"
    assert router.fact_intent("what do I need to make playoffs")[0] == "playoff"
    for q in ("Who's on the bubble for the league playoffs?", "Is the playoff committee biased toward the SEC?",
              "Whats the lates cfp news", "what's the latest on the Big Ten/SEC CFP fight?"):
        assert (router.fact_intent(q) or ("",))[0] != "cfp_now", (q, router.fact_intent(q))


def test_coach_asks_route_to_coach():
    import router
    for q in ("who's on the hot seat right now?", "Is Sherrone Moore getting fired?",
              "any coaching carousel news that matters this week?", "What's Lane Kiffin's coaching history?"):
        assert router.fact_intent(q) and router.fact_intent(q)[0] == "coach", (q, router.fact_intent(q))
    assert router.fact_intent("What's Lane Kiffin's career ATS record as a road underdog?")[0] == "career_ats"
    assert (router.fact_intent("Freeman took Notre Dame to the title game. Give him some credit.") or ("",))[0] != "coach"


def test_new_lanes_are_store_lanes_and_dispatch():
    import big_picture
    import router
    assert big_picture.coach_text in router.STORE_LANES and big_picture.cfp_now_text in router.STORE_LANES
    assert router.conf_race_text in router.STORE_LANES


if __name__ == "__main__":
    fails = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn) and fn.__module__ == "__main__":
            try:
                fn()
                print(f"ok   {name}")
            except Exception as e:  # noqa: BLE001
                fails += 1
                print(f"FAIL {name}: {e!r}"[:1200])
    print("ALL PASS" if not fails else f"{fails} FAILED")
    sys.exit(1 if fails else 0)
