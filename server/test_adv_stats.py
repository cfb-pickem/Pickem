"""Advanced stats first (Charlie, 2026-11-18: "use advanced stats like defensive pass efficiency and the other stats
the gambling nerds use rather than raw stats"). Model-free.

  - units.py maps CFBD's season advanced JSON (field names as cached: successRate, standardDowns, passingDowns,
    rushingPlays/passingPlays splits, lineYards, stuffRate, powerSuccess, havoc frontSeven/db, fieldPosition) and
    ranks every metric across FBS only (teams with SP+).
  - offense-vs-defense pairings state EPA/play AND success rate ranks and decide on both.
  - units.profile(team): the ranked advanced line the brain sees for a team (replaces raw yards per game).
  - cfbd.compact_team_summary: no ypg unless asked (raw=True).
  - units.swap_raw_yards: a draft that states raw yards per game when advanced data exists gets the ranked line.
  - chat._team_line (ESPN fallback, no CFBD): yardage only when asked.

Run:  ./.venv/bin/python3 -I test_adv_stats.py
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import cfbd  # noqa: E402
import units  # noqa: E402

N = 40


def full_row(team: str, ko: int, kd: int) -> dict:
    """Every advanced field CFBD sends, offense ranked ko and defense kd (1 = best of N)."""
    go, gd = (N - ko) / N, (N - kd) / N  # 1 = best
    bo, bd = 1 - go, 1 - gd

    def split(g, rate=None):
        d = {"ppa": g - 0.5, "successRate": 0.3 + 0.2 * g, "explosiveness": 1 + g}
        if rate is not None:
            d["rate"] = rate
        return d
    return {"team": team, "conference": "X",
            "offense": {"plays": 600 + 200 * go, "drives": 110, "ppa": go - 0.5, "successRate": 0.3 + 0.2 * go,
                        "explosiveness": 1 + go, "powerSuccess": 0.5 + 0.3 * go, "stuffRate": 0.25 - 0.1 * go,
                        "lineYards": 2 + go, "pointsPerOpportunity": 3 + 2 * go, "totalOpportunies": 60,
                        "fieldPosition": {"averageStart": 72 - 4 * go},
                        "havoc": {"total": 0.2 - 0.1 * go, "frontSeven": 0.1, "db": 0.05},
                        "standardDowns": split(go), "passingDowns": split(go),
                        "rushingPlays": split(go, 0.5), "passingPlays": split(go, 0.5)},
            "defense": {"plays": 600, "drives": 110, "ppa": bd - 0.5, "successRate": 0.3 + 0.2 * bd,
                        "explosiveness": 1 + bd, "powerSuccess": 0.5 + 0.3 * bd, "stuffRate": 0.15 + 0.1 * gd,
                        "lineYards": 2 + bd, "pointsPerOpportunity": 3 + 2 * bd,
                        "fieldPosition": {"averageStart": 68 + 4 * gd},
                        "havoc": {"total": 0.1 + 0.1 * gd, "frontSeven": 0.05 + 0.08 * gd, "db": 0.03 + 0.05 * gd},
                        "standardDowns": split(bd), "passingDowns": split(bd),
                        "rushingPlays": split(bd), "passingPlays": split(bd)}}


SPEC = {"Northwood": (3, 4), "Lakeview State": (36, 35), "Granite": (20, 21)}


def synthetic(fcs: bool = True):
    teams, adv = {}, []
    for i in range(1, N + 1):
        nm = f"Filler {i}"
        teams[cfbd.norm(nm)] = {"name": nm, "sp": {"rank": i, "off_rank": i, "def_rank": i},
                                "stats": {"games": 10, "sacks": 40 - i, "passAttemptsOpponent": 300,
                                          "sacksOpponent": i, "passAttempts": 300, "turnovers": i,
                                          "turnoversOpponent": 40 - i}}
        adv.append(full_row(nm, i, i))
    for nm, (ko, kd) in SPEC.items():
        teams[cfbd.norm(nm)] = {"name": nm, "sp": {"rank": (ko + kd) // 2, "off_rank": ko, "def_rank": kd},
                                "stats": {"games": 10, "sacks": 40 - kd, "passAttemptsOpponent": 300,
                                          "sacksOpponent": ko, "passAttempts": 300, "turnovers": ko,
                                          "turnoversOpponent": 40 - kd}}
        adv.append(full_row(nm, ko, kd))
    if fcs:  # an FCS team: in CFBD's advanced feed, no SP+ -> never ranked, never counted
        teams[cfbd.norm("Tiny State")] = {"name": "Tiny State", "stats": {"games": 10}}
        adv.append(full_row("Tiny State", 1, 1))
    idx = {"teams": teams, "players": [], "by_team": {}, "venues": [], "fetched_at": 1}
    return idx, units.table(idx, adv)


IDX, TAB = synthetic()

# ---------------------------------------------------------------------------
# mapping
# ---------------------------------------------------------------------------

NEW = {  # metric -> (team that must rank #1 of the 43 FBS rows: Filler 1 is best at everything)
    "succ_o", "succ_d", "sd_succ_o", "sd_succ_d", "pd_succ_o", "pd_succ_d", "rush_succ_o", "rush_succ_d",
    "pass_succ_o", "pass_succ_d", "line_o", "line_d", "stuff_o", "stuff_d", "power_o", "power_d",
    "havoc_f7_d", "havoc_db_d", "fp_o", "fp_d",
}


def test_new_metrics_mapped_and_ranked_best_first():
    r = units._row("Filler 1", TAB)
    for m in NEW:
        assert r.get(m) is not None, m
        assert r.get(m + "_rank") == 1, (m, r.get(m + "_rank"))
    worst = units._row("Filler 40", TAB)
    for m in NEW:
        assert worst[m + "_rank"] == TAB["_n"], (m, worst[m + "_rank"])


def test_field_values_come_from_the_cfbd_names():
    r, raw = units._row("Northwood", TAB), units._row("Northwood", TAB)["raw"]
    assert r["pass_succ_d"] == raw["defense"]["passingPlays"]["successRate"]
    assert r["rush_succ_o"] == raw["offense"]["rushingPlays"]["successRate"]
    assert r["sd_succ_o"] == raw["offense"]["standardDowns"]["successRate"]
    assert r["pd_succ_d"] == raw["defense"]["passingDowns"]["successRate"]
    assert r["line_o"] == raw["offense"]["lineYards"] and r["stuff_d"] == raw["defense"]["stuffRate"]
    assert r["havoc_db_d"] == raw["defense"]["havoc"]["db"]
    assert r["fp_o"] == raw["offense"]["fieldPosition"]["averageStart"]


def test_ranks_are_fbs_only():
    assert units._row("Tiny State", TAB) is None
    assert TAB["_n"] == N + len(SPEC), TAB["_n"]


def test_real_cache_field_names():
    """Every path units reads exists in the cached CFBD rows (catches a CFBD rename)."""
    try:
        db = sqlite3.connect(f"file:{cfbd.DB_PATH}?mode=ro", uri=True)
        js = db.execute("select json from datasets where name='advanced'").fetchone()
    except sqlite3.Error:
        js = None
    if not js:
        print("  (no CFBD cache: skipped)")
        return
    rows = json.loads(js[0])
    r = next(x for x in rows if x.get("offense", {}).get("successRate") is not None)
    for side in ("offense", "defense"):
        s = r[side]
        for k in ("ppa", "successRate", "explosiveness", "lineYards", "stuffRate", "powerSuccess",
                  "pointsPerOpportunity"):
            assert isinstance(s.get(k), (int, float)), (side, k)
        assert isinstance(s["fieldPosition"]["averageStart"], (int, float))
        for k in ("total", "frontSeven", "db"):
            assert isinstance(s["havoc"][k], (int, float)), (side, "havoc", k)
        for sub in ("standardDowns", "passingDowns", "rushingPlays", "passingPlays"):
            assert isinstance(s[sub]["successRate"], (int, float)), (side, sub)
    tab = units.table()
    assert all(v.get("succ_o_rank") for k, v in tab.items() if not k.startswith("_") and v.get("succ_o") is not None)


# ---------------------------------------------------------------------------
# pairings: EPA and success rate, decided on both
# ---------------------------------------------------------------------------

def test_rush_pair_states_epa_and_success_ranks():
    e = units.pair_edge("rush", "Lakeview State", "Northwood", TAB)
    t = e["text"]
    assert "rushing EPA/play" in t and "rush success rate" in t, t
    ro_s = units._row("Lakeview State", TAB)["rush_succ_o_rank"]
    rd_s = units._row("Northwood", TAB)["rush_succ_d_rank"]
    assert f"#{ro_s}" in t and f"#{rd_s}" in t, t
    assert e["winner"] == "Northwood" and "edge Northwood" in t, t


def test_pass_pair_is_defensive_pass_efficiency():
    e = units.pair_edge("pass", "Granite", "Northwood", TAB)
    t = e["text"]
    assert "passing EPA/play" in t and "pass success rate" in t, t
    assert e["sd"] == units._row("Northwood", TAB)["pass_succ_d_rank"], e


def test_verdict_uses_both_metrics():
    """EPA says a clear edge but success rate says even: averaged, no clear edge."""
    idx, _ = synthetic(fcs=False)
    adv = [full_row(f"Filler {i}", i, i) for i in range(1, N + 1)]
    o = full_row("Granite", 3, 20)           # EPA: offense #3-ish
    o["offense"]["rushingPlays"]["successRate"] = full_row("x", 33, 33)["offense"]["rushingPlays"]["successRate"]
    d = full_row("Northwood", 20, 34)        # run D #34-ish
    d["defense"]["rushingPlays"]["successRate"] = full_row("x", 8, 8)["defense"]["rushingPlays"]["successRate"]
    idx["teams"].pop(cfbd.norm("Lakeview State"))
    tab = units.table(idx, adv + [o, d])
    e = units.pair_edge("rush", "Granite", "Northwood", tab)
    assert e["ro"] + units.GAP <= e["rd"], e   # EPA alone would call it for Granite
    assert e["winner"] is None, e["text"]


# ---------------------------------------------------------------------------
# the team profile the brain sees
# ---------------------------------------------------------------------------

def test_profile_is_ranked_advanced_stats():
    p = units.profile("Northwood", TAB)
    for needle in ("EPA/play", "success", "rush", "pass D EPA/play", "pass success allowed", "havoc",
                   "pts per scoring chance", "avg start", "standard downs", "passing downs", "line yards"):
        assert needle in p, (needle, p)
    nums = re.findall(r"[-+]?\d*\.\d+|\d+%", p)
    assert nums and len(re.findall(r"#\d+", p)) >= len(nums) - 2, p   # every number has its rank (or nearly)
    assert "garbage time" in p and "not opponent-adjusted" in p, p
    assert "yards per game" not in p and "ypg" not in p, p


def test_short_profile_for_the_pool_block():
    """Every pool game's block carries both teams' profiles in the cached prefix: the short one keeps it lean."""
    p = units.profile("Northwood", TAB, short=True)
    assert len(p) <= 300, (len(p), p)
    for needle in ("EPA/play", "success", "rush EPA", "pass EPA", "pass D EPA/play", "pass success allowed", "havoc"):
        assert needle in p, (needle, p)
    assert "not opponent-adjusted" in p or "not opp-adjusted" in p, p


def test_profile_avg_start_reads_as_own_yard_line():
    raw = units._row("Northwood", TAB)["raw"]["offense"]["fieldPosition"]["averageStart"]
    assert f"own {round(100 - raw)}" in units.profile("Northwood", TAB), units.profile("Northwood", TAB)


def test_profile_unknown_team_is_empty():
    assert units.profile("Nowhere U", TAB) == ""


# ---------------------------------------------------------------------------
# no ypg in the CFBD summary unless asked
# ---------------------------------------------------------------------------

def _summary_idx():
    t = {"name": "Boise State", "record": "9-1", "sp": {"rating": 20.1, "rank": 10, "off_rank": 5, "def_rank": 30},
         "ppg": 40.1, "opp_ppg": 18.0, "ppg_rank": 3, "opp_ppg_rank": 12,
         "ypg": 470.2, "opp_ypg": 330.4, "pass_ypg": 250.0, "rush_ypg": 220.2, "opp_ypg_rank": 20, "stats": {}}
    return {"teams": {cfbd.norm("Boise State"): t}, "players": [], "by_team": {}, "venues": [], "fetched_at": 1}


def test_summary_drops_ypg_by_default():
    s = cfbd.compact_team_summary("Boise State", _summary_idx())
    assert "ypg" not in s and "470.2" not in s, s
    assert "SP+" in s, s


def test_summary_keeps_ypg_when_asked():
    s = cfbd.compact_team_summary("Boise State", _summary_idx(), raw=True)
    assert "470.2 ypg" in s, s


def test_game_lines_carry_the_advanced_profile():
    """The pool game block the brain sees: an 'Advanced' line per team, no ypg (real cache; skipped without one)."""
    import chat
    idx = cfbd.load_index()
    if not idx or not units._row("Minnesota", units.table()):
        print("  (no CFBD cache: skipped)")
        return
    L = chat.cfbd_game_lines("Northwestern", "Minnesota", idx)
    if any("not loaded" in x for x in L):
        print("  (CFBD cache stale: skipped)")
        return
    blob = "\n".join(L)
    assert "Advanced, Minnesota:" in blob and "Advanced, Northwestern:" in blob, blob
    assert "rush EPA #" in blob and "pass D EPA/play" in blob, blob
    assert "ypg" not in blob, blob
    assert all(len(x) <= 330 for x in L if x.startswith("  Advanced, ")), L


def test_stats_only_rows_carry_the_profile():
    idx = cfbd.load_index()
    if not idx or not units._row("Minnesota", units.table()):
        print("  (no CFBD cache: skipped)")
        return
    rows = cfbd.question_rows("what's minnesota's defense like", [("Ohio State", "Michigan")])
    blob = "\n".join(rows)
    assert "Minnesota" in blob and "pass D EPA/play" in blob and "ypg" not in blob, blob


def test_card_finishing_drives_numbers_carry_ranks():
    import slate_cards
    if not units._row("Minnesota", units.table()):
        print("  (no CFBD cache: skipped)")
        return
    ln = slate_cards.run_game_line("Northwestern", "Minnesota") or ""
    for m in re.finditer(r"\d\.\d", ln):
        assert re.match(r"\d\.\d \S* ?\(#\d+\)|\d\.\d \(#\d+\)", ln[m.start():]) or "(#" in ln[m.end():m.end() + 30], ln
    assert "pts per scoring chance (#" in ln, ln


# ---------------------------------------------------------------------------
# a draft that states raw ypg gets the ranked line
# ---------------------------------------------------------------------------

def test_swap_raw_rush_yards_for_the_ranked_unit():
    d = ("Lakeview State averages 92.8 rushing yards per game while Northwood gives up 151.2 on the ground. "
         "They might find some grass.")
    out = units.swap_raw_yards(d, "Can Lakeview State run the ball on Northwood?", TAB)
    assert "92.8" not in out and "151.2" not in out and "yards per game" not in out, out
    assert "rushing EPA/play" in out and "edge Northwood" in out, out
    assert out.endswith("They might find some grass."), out


def test_swap_total_yards_uses_overall_epa():
    d = "Lakeview State averages 299.4 total yards per game while Northwood allows 365.8."
    out = units.swap_raw_yards(d, "Can Lakeview State move the ball on Northwood?", TAB)
    assert "299.4" not in out and "EPA/play" in out, out


def test_swap_leaves_yards_asks_and_clean_text_alone():
    d = "Lakeview State averages 92.8 rushing yards per game."
    assert units.swap_raw_yards(d, "how many rushing yards per game does Lakeview State average?", TAB) == d
    clean = "Northwood's run defense is #4 in rushing EPA/play."
    assert units.swap_raw_yards(clean, "can lakeview run on northwood", TAB) == clean


def test_swap_ypg_abbreviation_one_team():
    out = units.swap_raw_yards("Northwood puts up 410 ypg. Big number.", "is northwood's offense good?", TAB)
    assert "410" not in out and "Northwood" in out and "EPA/play" in out and out.endswith("Big number."), out


def test_swap_leaves_player_stat_lines_alone():
    for d in ("Northwood's QB Joe Smith runs for 55 yards a game. Dangerous.",
              "Lakeview State's running back averages 98 rushing yards per game.",
              "He throws for 280 yards per game against Northwood."):
        assert units.swap_raw_yards(d, "can northwood stop lakeview state", TAB) == d, d


def test_swap_unknown_team_drops_nothing_it_cant_replace():
    d = "Nowhere U averages 300 yards per game. Fun."
    assert units.swap_raw_yards(d, "nowhere u?", TAB) == d


# ---------------------------------------------------------------------------
# the unit lane on the real cache: Minnesota-Northwestern (a week-12 pool game; skipped when it isn't on the slate)
# ---------------------------------------------------------------------------

def _lane(q):
    import os
    os.environ["PICKY_LANES_OFF"] = ""
    import unit_lane
    return unit_lane.build(q, {"games_this_week": []})


def test_minnesota_run_game_is_epa_and_success_rate():
    if not units._row("Minnesota", units.table()):
        print("  (no CFBD cache: skipped)")
        return
    b = _lane("Can Minnesota run the ball on Northwestern?")
    if not b:
        print("  (Minnesota-Northwestern not on the slate: skipped)")
        return
    t = b["text"]
    tab = units.table()
    m, nw = units._row("Minnesota", tab), units._row("Northwestern", tab)
    first = re.split(r"(?<=[.!?])\s", t)[0]
    assert f"#{m['rush_o_rank']} in rushing EPA/play" in first and f"#{m['rush_succ_o_rank']} in rush success rate" in first, t
    assert f"#{nw['rush_d_rank']}" in first and f"#{nw['rush_succ_d_rank']}" in first, t
    assert "Northwestern's run D has the edge" in first, t
    assert "yards" not in t and "ypg" not in t and len(t) <= 350, t


def test_lane_numbers_are_all_sourced():
    """Every number in a lane answer is a rank / value from the units table, a plays-a-game count, or the game's
    spread / total."""
    import unit_lane
    tab = units.table()
    if not units._row("Minnesota", tab):
        print("  (no CFBD cache: skipped)")
        return
    asks = ["Can Minnesota run the ball on Northwestern?", "Can Michigan throw it on UCLA's secondary?",
            "Who wins the trenches in Ohio State-Nebraska?", "Houston-West Virginia: is this a shootout? Who plays faster?",
            "Who wins the turnover battle in Iowa-Illinois?", "Where does Notre Dame have the edge on SMU?",
            "Will Baylor's pass rush get home against Texas Tech?"]
    for q in asks:
        b = _lane(q)
        if not b:
            continue
        g = unit_lane.SOURCE.game(q, {"games_this_week": []})
        ok = {str(tab["_n"])}
        for t in (g["away"], g["home"]):
            r = units._row(t, tab)
            ok |= {str(v) for k, v in r.items() if k.endswith("_rank")}
            ok |= {str(round(r["pace"]))} if r.get("pace") else set()
            ok |= {str(int(r["stats"].get(k) or 0)) for k in ("turnovers", "turnoversOpponent", "games")}
        for x in (g.get("spread"), g.get("total")):
            if x is not None:
                ok |= {f"{abs(x):g}", f"{x:g}"}
        for num in re.findall(r"\d+(?:\.\d+)?", b["text"]):
            assert num in ok, (q, num, b["text"])


def test_content_gate_flags_raw_ypg_and_unranked_unit_answers():
    sys.path.insert(0, str(Path(__file__).resolve().parent / "eval" / "genius"))
    import content_check as C
    q = "Can Minnesota run the ball on Northwestern?"
    bad = C.check_turn(q, "fact:unit_matchup", "Minnesota averages 92.8 rushing yards per game, Northwestern allows 151.2.")
    assert any("raw yards" in e for e in bad) and any("EPA" in e for e in bad), bad
    good = ("Not much: Minnesota's run game is #136 in rushing EPA/play and #131 in rush success rate, Northwestern's "
            "run D #62 and #106, so Northwestern's run D has the edge.")
    assert not C.check_turn(q, "fact:unit_matchup", good), C.check_turn(q, "fact:unit_matchup", good)
    assert any("raw yards" in e for e in C.check_turn(q, "full", "Minnesota puts up 299.4 total yards per game."))
    assert not C.check_turn("how many yards per game does Minnesota average?", "full", "Minnesota: 299.4 yards per game.")


# ---------------------------------------------------------------------------
# ESPN fallback (no CFBD): yardage only when asked
# ---------------------------------------------------------------------------

def test_espn_line_drops_yards_unless_asked():
    import chat
    t = {"record": "5-5", "stats": ["Points Per Game 23.6", "Total Yards 299.4", "Yards Passing 206.6",
                                    "Yards Rushing 92.8"]}
    s = chat._team_line("Minnesota", t)
    assert "299.4" not in s and "92.8" not in s and "Points Per Game 23.6" in s, s
    s2 = chat._team_line("Minnesota", t, yards=True)
    assert "Total Yards 299.4" in s2, s2


if __name__ == "__main__":
    fails = 0
    for name, f in list(globals().items()):
        if name.startswith("test_") and callable(f):
            try:
                f()
                print("ok  ", name)
            except Exception as ex:  # noqa: BLE001
                fails += 1
                print("FAIL", name, repr(ex)[:400])
    sys.exit(1 if fails else 0)
