"""
ATS profile (plan phase 3, `ats_profile` lane): "how's X against the number / X ATS / X as a favorite".

- The store's team sheet carries an ATS profile computed from its results vs the number: overall, fav/dog,
  home/road, last 4, streak, avg cover margin, biggest miss, each also a fact.
- The lane leads with the split that matches THIS week's role (fav/dog + home/road from the game sheet), unless the
  text asks for a split; it ends with a computed verdict in pick-factor lean words, never a lock.
- Every number it prints is backed by facts_used (factstore.unsupported == []).
- The router sends real ATS phrasings there, and leaves member ATS / coach career / pick asks alone.

All on the frozen fact-store fixture (server/eval/fixtures/factstore).

Run:  ./.venv/bin/python3 -I test_ats_profile.py
"""

from __future__ import annotations

import re
import sys

sys.path.insert(0, ".")
import factstore as FS  # noqa: E402
import test_factstore as TF  # noqa: E402  (fixture inputs + the frozen build)

LEAN_WORDS = ("coin flip", "slight lean", "lean", "I like it")


def store():
    return TF.built()["store"]


def lane(q: str):
    import router
    FS.use(TF.built()["path"], TF.inputs(), now=TF.NOW)
    try:
        return router.ats_profile_text({}, q)
    finally:
        FS.use(None, None)


# --- the store ------------------------------------------------------------------------------------------------

def test_team_sheet_has_ats_profile_from_results():
    p = store()["teams"]["Duke"]["ats_profile"]
    # Duke's 10 results vs the DraftKings line in the fixture
    assert p["overall"] == [8, 2, 0], p
    assert p["fav"] == [5, 2, 0] and p["dog"] == [3, 0, 0], p
    assert p["home"] == [4, 1, 0] and p["road"] == [4, 1, 0], p
    assert p["last4"] == [4, 0, 0], p
    assert p["streak"] == {"kind": "cover", "n": 4}, p
    assert p["avg_margin"] == 11.0, p
    assert p["biggest_miss"]["opp"] == "Georgia Tech" and p["biggest_miss"]["cover"] == -7.5, p


def test_ats_profile_facts_carry_every_number():
    t = store()["teams"]["Indiana"]
    ids = {f["id"]: f for f in t["facts"]}
    for k in ("calc_overall", "fav", "dog", "home", "road", "last4", "streak", "avg_margin", "biggest_miss"):
        assert f"ats.indiana.{k}" in ids, (k, sorted(ids))
    assert ids["ats.indiana.fav"]["value"] == "2-7", ids["ats.indiana.fav"]
    assert ids["ats.indiana.calc_overall"]["source"] and ids["ats.indiana.calc_overall"]["as_of"]


def test_store_still_builds_under_5_seconds():
    assert TF.built()["secs"] < 5.0


# --- the lane -------------------------------------------------------------------------------------------------

def test_lane_leads_with_this_weeks_role_split():
    out = lane("how's Duke against the number this year?")
    assert out and out.startswith("Duke is 8-2 against the number"), out
    # this week: Duke -13.5 at home vs Clemson -> the favorite + home splits come first
    assert "13.5-point favorite at home" in out, out
    assert out.index("5-2 as a favorite") < out.index("covered 4 straight"), out
    assert "4-1 at home" in out and "3-0 as an underdog" not in out, out


def test_lane_has_a_verdict_in_lean_words_never_a_lock():
    for q in ("how's Duke against the number this year?", "Indiana ATS?", "how does Houston do against the spread"):
        out = lane(q)
        assert out, q
        assert any(re.search(rf"\b{w}\b", out, re.I) for w in LEAN_WORDS), out
        assert not re.search(r"\block\b", out, re.I), out


def test_covering_machine_verdict_says_priced_in():
    out = lane("how's Duke against the number this year?")
    assert re.search(r"priced", out, re.I), out


def test_bad_ats_team_as_a_road_favorite():
    out = lane("Indiana ATS?")
    # Indiana is laying 7.5 at Washington: the favorite and road splits lead
    assert "7.5-point favorite on the road" in out and "2-7 as a favorite" in out and "1-2 on the road" in out, out


def test_asked_split_leads_even_if_not_this_weeks_role():
    out = lane("how is Duke as an underdog?")
    assert out and "3-0 as an underdog" in out, out
    assert out.index("3-0 as an underdog") < out.index("5-2 as a favorite") if "5-2 as a favorite" in out else True


def test_every_number_is_backed_by_facts_used():
    for q in ("how's Duke against the number this year?", "Indiana ATS?", "how does Houston do against the spread",
              "how is Duke as an underdog?", "UCLA ATS at home?"):
        out = lane(q)
        assert out and getattr(out, "facts_used", None), (q, out)
        assert FS.unsupported(out, out.facts_used) == [], (q, out, FS.unsupported(out, out.facts_used))


def test_never_writes_a_team_with_a_spread_label():
    """The fact exam's non-pool ATS item fails any 'Texas -7.5'-style label; the role is said in words."""
    for team in ("Duke", "Indiana", "Houston", "Texas", "UCLA"):
        out = lane(f"{team} ATS?")
        assert out and not re.search(r"\b[A-Z][\w&'.]*(?: [A-Z][\w&'.]*)* [+-]\d", out), out


def test_cfbd_consensus_record_named_when_it_differs():
    st = store()
    t = next(t for t in st["teams"].values() if t.get("ats", {}).get("w") is not None
             and t["ats_profile"]["overall"][:2] != [t["ats"]["w"], t["ats"]["l"]] and t["ats_profile"]["games"] >= 6)
    out = lane(f"{t['name']} ATS?")
    assert out and f"{t['ats']['w']}-{t['ats']['l']}" in out and "consensus" in out, out


def test_lane_works_on_a_store_built_before_ats_profile():
    """The live store is rebuilt every 5 min; until the new build lands a sheet may lack ats_profile."""
    import json
    st = json.loads(TF.built()["path"].read_text())
    for t in st["teams"].values():
        t.pop("ats_profile", None)
        t["facts"] = [f for f in t["facts"] if not (f["kind"] == "ats" and f["id"].rsplit(".", 1)[-1] != "overall")]
    old = TF.TMP / "old-store.json"
    old.write_text(json.dumps(st))
    import router
    FS.use(old, TF.inputs(), now=TF.NOW)
    try:
        out = router.ats_profile_text({}, "how's Duke against the number?")
    finally:
        FS.use(None, None)
    assert out and out.startswith("Duke is 8-2 against the number") and FS.unsupported(out, out.facts_used) == [], out


def test_unknown_team_is_none():
    assert lane("how's the Montana Tech Moose Riders ATS") is None


def test_short_team_names_resolve_in_the_store_gateway():
    """'LSU', 'TCU', 'BYU' (3 letters) were skipped by the 4-letter floor on one-word names, so 'I'd take LSU -1.5'
    named no team and a follow-up couldn't find the game."""
    FS.use(TF.built()["path"], TF.inputs(), now=TF.NOW)
    try:
        assert FS.teams_named("I'd take LSU -1.5 (slight lean)") == ["LSU"], FS.teams_named("I'd take LSU -1.5")
        assert FS.teams_named("what flips you on Tennessee LSU") == ["Tennessee", "LSU"]
        assert set(FS.teams_named("lsu tenn")) >= {"LSU"}
        assert FS.teams_named("how's TCU ATS") == ["TCU"]
        assert FS.teams_named("I took the SAT and the ACT") == []
    finally:
        FS.use(None, None)


# --- routing --------------------------------------------------------------------------------------------------

def test_router_sends_ats_phrasings_to_the_lane():
    import router
    for q in ("how's Duke against the number?", "Indiana ATS?", "what's Houston's ATS record", "is UCLA covering lately",
              "how does Georgia do as a favorite", "Duke against the spread this year?", "Texas Tech ATS as a road dog",
              "how has Ohio State been vs the spread"):
        assert router.classify(q).label() == "fact:ats_profile", (q, router.classify(q).label())


def test_router_leaves_other_ats_asks_alone():
    import router
    for q in ("what's my ATS record", "how am I doing against the spread", "Dabo career ATS",
              "who covers Duke Clemson", "did Duke cover last week"):
        assert router.classify(q).label() != "fact:ats_profile", (q, router.classify(q).label())


def test_router_sends_polite_and_cover_record_asks_to_the_lane():
    import router
    for q in ("can you tell me Michigan's ATS record", "do you know how Texas does against the spread",
              "tell me Duke's record ATS", "Oregon cover record"):
        assert router.classify(q).label() == "fact:ats_profile", (q, router.classify(q).label())
    for q in ("can you tell me my ATS record", "tell me your record against the spread",
              "what's my cover record"):
        assert router.classify(q).label() != "fact:ats_profile", (q, router.classify(q).label())


# --- the calculation from raw scores + lines (cover / push / underdog cover) ---------------------------------

def _sheets(games):
    """Team sheets from a tiny CFBD-shaped input: games = (home, away, home pts, away pts, home spread)."""
    lines = [{"id": i, "startDate": f"2026-09-{i + 1:02d}T19:00:00Z", "seasonType": "regular",
              "homeTeam": h, "awayTeam": a, "homeScore": hs, "awayScore": as_,
              "lines": [{"provider": "Bovada", "spread": sp + 3}, {"provider": "DraftKings", "spread": sp}]}
             for i, (h, a, hs, as_, sp) in enumerate(games)]
    teams = sorted({g[0] for g in games} | {g[1] for g in games})
    recs = [{"year": 2026, "team": t, "classification": "fbs"} for t in teams]
    return FS._team_sheets({"lines": lines, "records": recs}, {}, TF.NOW)


def test_calc_favorite_cover_push_and_underdog_cover():
    s = _sheets([("Alpha", "Beta", 31, 10, -14.0),   # Alpha -14 wins by 21: Alpha covers by 7, Beta misses
                 ("Alpha", "Gamma", 24, 17, -7.0),   # Alpha -7 wins by 7: push both ways
                 ("Delta", "Alpha", 27, 24, -6.5)])  # Alpha +6.5 loses by 3: underdog covers by 3.5
    a = s["Alpha"]
    covers = [r["cover"] for r in a["results"]]
    assert covers == [7.0, 0.0, 3.5], covers
    p = a["ats_profile"]
    assert p["overall"] == [2, 0, 1], p                       # W-L-P: the push is neither
    assert p["fav"] == [1, 0, 1] and p["dog"] == [1, 0, 0], p  # underdog cover counted as a dog cover
    assert p["home"] == [1, 0, 1] and p["road"] == [1, 0, 0], p
    assert [r["cover"] for r in s["Beta"]["results"]] == [-7.0]
    assert s["Beta"]["ats_profile"]["dog"] == [0, 1, 0]
    assert s["Gamma"]["ats_profile"]["overall"] == [0, 0, 1]
    assert s["Delta"]["ats_profile"]["fav"] == [0, 1, 0]      # won by 3 laying 6.5: no cover
    ids = {f["id"]: f["value"] for f in a["facts"] if f["kind"] == "ats"}
    assert ids["ats.alpha.calc_overall"] == "2-0-1" and ids["ats.alpha.dog"] == "1-0", ids


def test_calc_uses_draftkings_line_and_skips_games_without_one():
    s = _sheets([("Alpha", "Beta", 20, 17, -3.5)])
    assert s["Alpha"]["results"][0]["spread"] == -3.5  # DraftKings, not the Bovada line listed first
    lines = [{"id": 1, "startDate": "2026-09-01", "seasonType": "regular", "homeTeam": "Alpha", "awayTeam": "Beta",
              "homeScore": 20, "awayScore": 17, "lines": []}]
    s = FS._team_sheets({"lines": lines, "records": [{"year": 2026, "team": "Alpha", "classification": "fbs"}]},
                        {}, TF.NOW)
    assert s["Alpha"]["ats_profile"]["games"] == 0  # no line on record: no ATS game, no made-up number


def test_lane_says_yet_to_play_for_an_empty_split():
    import router
    FS.use(TF.built()["path"], TF.inputs(), now=TF.NOW)
    try:
        st = FS.store()
        name = next(n for n, t in st["teams"].items()
                    if (t.get("ats_profile") or {}).get("games") and not sum(t["ats_profile"]["dog"]))
        out = router.ats_profile_text({}, f"how is {name} as an underdog")
    finally:
        FS.use(None, None)
    assert "yet to play a game as an underdog" in out and "no games" not in out, out


def test_ats_profile_is_a_store_lane():
    import router
    assert router.ats_profile_text in router.STORE_LANES


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
