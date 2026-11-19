"""
Conference title races (title_race.race, plan phase 5): head-to-head tiebreaks and "clinches with a win".

Synthetic conferences, no network, no model. A game is {id, start, home, away, home_pts, away_pts}; no points =
still to play. The top 2 by conference record make the title game. Only head-to-head is modeled: a tie it can't
settle says the conference's further tiebreakers apply (never a guess).

Run:  ./.venv/bin/python3 -I test_title_race.py
"""

from __future__ import annotations

import datetime as dt
import sys

sys.path.insert(0, ".")
import title_race as tr  # noqa: E402

NOW = dt.datetime(2026, 11, 18, 12, tzinfo=dt.timezone.utc)
_n = [0]


def G(home, away, hp=None, ap=None, day=-30):
    """home beat away when hp > ap; no score = upcoming (day = days from NOW)."""
    _n[0] += 1
    start = (NOW + dt.timedelta(days=day)).isoformat()
    return {"id": str(_n[0]), "start": start, "home": home, "away": away, "home_pts": hp, "away_pts": ap}


def W(winner, loser, day=-30):
    return G(winner, loser, 30, 10, day)


def row(r, team):
    return next(x for x in r["standings"] if x["team"] == team)


# --- standings and head-to-head ------------------------------------------------------------------------

def test_two_way_tie_goes_to_the_head_to_head_winner():
    games = [W("B", "C"), W("A", "B"), W("A", "C"), W("B", "D"), W("D", "A"), W("C", "D")]
    # A 2-1 (beat B), B 2-1 (lost to A), C 1-2, D 1-2
    r = tr.race("Big 12", games, NOW)
    assert [x["team"] for x in r["standings"]][:2] == ["A", "B"], r["standings"]
    tie = next(t for t in r["ties"] if set(t["teams"]) == {"A", "B"})
    assert tie["settled_by"] == "h2h" and tie["order"] == ["A", "B"], tie
    assert "head-to-head" in tie["note"] and "A" in tie["note"], tie


def test_head_to_head_is_applied_even_when_the_loser_sorts_first_by_name():
    games = [W("Zeta", "Alpha"), W("Alpha", "C"), W("Alpha", "D"), W("C", "Zeta"), W("Zeta", "D")]
    r = tr.race("SEC", games, NOW)  # Zeta 2-1 and Alpha 2-1, Zeta won the meeting
    top = [x["team"] for x in r["standings"]][:2]
    assert top == ["Zeta", "Alpha"], top


def test_tie_without_a_meeting_says_further_tiebreakers_apply():
    games = [W("A", "C"), W("B", "D"), W("A", "D"), W("B", "C"), W("E", "A"), W("E", "B")]
    r = tr.race("ACC", games, NOW)  # A 2-1 and B 2-1, never met
    tie = next(t for t in r["ties"] if set(t["teams"]) == {"A", "B"})
    assert tie["settled_by"] is None, tie
    assert "didn't play" in tie["note"] and "ACC's further tiebreakers" in tie["note"], tie


def test_three_way_tie_settled_when_one_team_beat_both():
    games = [W("A", "B"), W("A", "C"), W("A", "E"), W("D", "A"), W("B", "E"), W("B", "F"), W("B", "G"),
             W("C", "F"), W("C", "G"), W("C", "H")]
    r = tr.race("Big Ten", games, NOW)  # A, B, C all 3-1; A beat B and C
    tie = next(t for t in r["ties"] if set(t["teams"]) == {"A", "B", "C"})
    assert tie["settled_by"] == "h2h" and tie["order"][0] == "A", tie
    assert [x["team"] for x in r["standings"]].index("A") < [x["team"] for x in r["standings"]].index("B")


def test_three_way_cycle_is_not_settled():
    games = [W("A", "B"), W("B", "C"), W("C", "A"), W("A", "D"), W("B", "D"), W("C", "D")]
    r = tr.race("Big 12", games, NOW)
    tie = next(t for t in r["ties"] if set(t["teams"]) == {"A", "B", "C"})
    assert tie["settled_by"] is None and "further tiebreakers" in tie["note"], tie


# --- clinch / eliminate / control ------------------------------------------------------------------------

def _week_before_the_end():
    """A 6-1 (lost to D, beat C), B 6-1 (never met A), C 5-2, D 5-2, E 2-5, F 2-5; one game each left, all this
    week: A vs E, B vs C, D vs F."""
    played = [W("A", "C"), W("A", "F"), W("A", "G"), W("A", "H"), W("A", "X1"), W("A", "X2"), W("D", "A"),
              W("B", "D"), W("B", "E"), W("B", "G"), W("B", "H"), W("B", "X1"), W("B", "X2"), W("F", "B"),
              W("C", "E"), W("C", "G"), W("C", "H"), W("C", "X1"), W("C", "X2"), W("F", "C"),
              W("D", "E"), W("D", "G"), W("D", "H"), W("D", "X1"), W("X2", "D"),
              W("E", "G"), W("E", "H"), W("X1", "E"), W("X2", "E"),
              W("G", "F"), W("H", "F"), W("X1", "F"), W("X2", "F")]
    left = [G("A", "E", day=3), G("B", "C", day=3), G("D", "F", day=3)]
    return played + left


def test_records_and_remaining_games():
    r = tr.race("Big 12", _week_before_the_end(), NOW)
    a = row(r, "A")
    assert (a["w"], a["l"], a["rem"]) == (6, 1, 1), a
    assert a["next"]["opp"] == "E" and a["next"]["home"] is True, a["next"]


def test_clinches_with_a_win():
    r = tr.race("Big 12", _week_before_the_end(), NOW)
    a = row(r, "A")
    assert a["status"] == "alive" and a["clinch_with_win"] is True, a
    assert row(r, "B")["clinch_with_win"] is True


def test_who_controls_it():
    r = tr.race("Big 12", _week_before_the_end(), NOW)
    assert set(r["controls"]) == {"A", "B"}, r["controls"]
    assert row(r, "C")["controls"] is False and row(r, "D")["controls"] is False


def test_out_with_a_loss_and_eliminated():
    r = tr.race("Big 12", _week_before_the_end(), NOW)
    assert row(r, "C")["out_with_loss"] is True, row(r, "C")
    assert row(r, "E")["status"] == "eliminated", row(r, "E")
    assert "E" not in r["chasers"] and "C" in r["chasers"], r["chasers"]


def test_clinched_when_only_one_rival_can_catch_up():
    games = [W("A", "B"), W("A", "C"), W("A", "D"), W("A", "E"), W("B", "C"), W("B", "D"), W("C", "E"),
             W("D", "E"), G("A", "F", day=3), G("B", "E", day=3), G("C", "D", day=3)]
    r = tr.race("SEC", games, NOW)
    assert row(r, "A")["status"] == "clinched", row(r, "A")


def test_head_to_head_win_turns_a_tie_threat_into_a_clinch():
    # A 5-1 (beat B), B 5-1, C 3-3: A's worst case 5-2 ties B's best 6-1? no: B can reach 6-1 > A. Use: A 6-0 beat B;
    # B 5-1 with 1 left can tie A's worst 6-1 at 6-1 but A holds the H2H, so A has clinched a title-game spot.
    games = [W("A", "B"), W("A", "C"), W("A", "D"), W("A", "E"), W("A", "F"), W("A", "G"),
             W("B", "C"), W("B", "D"), W("B", "E"), W("B", "F"), W("B", "G"),
             W("C", "D"), W("C", "E"), W("C", "F"), W("D", "E"),
             G("A", "H", day=3), G("B", "H", day=4), G("C", "G", day=3)]
    r = tr.race("Big 12", games, NOW)
    assert row(r, "A")["status"] == "clinched", row(r, "A")


def test_clinch_with_a_win_plus_a_rival_loss():
    """A 5-1, B 5-1, C 5-1 (none of them met; all lost to X), one game left each: A vs Z, B vs E, C vs F.
    A's win alone doesn't clinch (B and C can both tie without A holding H2H); A win + B loss does."""
    games = [W("A", "D"), W("A", "E"), W("A", "F"), W("A", "G"), W("A", "H"), W("X", "A"),
             W("B", "D"), W("B", "F"), W("B", "G"), W("B", "H"), W("B", "K"), W("X", "B"),
             W("C", "D"), W("C", "E"), W("C", "G"), W("C", "H"), W("C", "K"), W("X", "C"),
             W("D", "X"), W("E", "X"), W("F", "X"), W("G", "X"), W("H", "X"),
             G("A", "Z", day=3), G("B", "E", day=3), G("C", "F", day=3)]
    r = tr.race("ACC", games, NOW)
    a = row(r, "A")
    assert (a["w"], a["l"]) == (5, 1) and a["clinch_with_win"] is False, a
    assert set(a["clinch_with"]) == {"B", "C"}, a["clinch_with"]


def test_a_final_from_today_counts():
    games = _week_before_the_end()
    left = next(g for g in games if g["home"] == "A" and g["away"] == "E")
    played = [dict(g, home_pts=40, away_pts=7) if g is left else g for g in games]
    r = tr.race("Big 12", played, NOW)
    assert row(r, "A")["w"] == 7 and row(r, "A")["status"] == "clinched", row(r, "A")


def test_division_conference_gets_no_top_two_math():
    r = tr.race("Sun Belt", [W("A", "B"), G("A", "C", day=3)], NOW)
    assert r["divisional"] is True and all(x["status"] is None for x in r["standings"]), r


def test_facts_carry_every_record_printed():
    r = tr.race("Big 12", _week_before_the_end(), NOW, as_of="2026-11-18T12:00:00+00:00")
    texts = " ".join(f["text"] for f in r["facts"])
    assert "A is 6-1 in Big 12 play" in texts, texts
    assert all(f["as_of"] and f["source"] for f in r["facts"]), r["facts"][:2]


def _three_way_risk():
    """A 7-0 (beat C), B 6-1 and C 5-2, two games left each. A and B never met, B and C never met. A losing out
    (7-2) with B 7-2 and C 7-2 is a 3-way tie head-to-head can't settle, so A could still finish 3rd."""
    f = [f"F{i}" for i in range(1, 10)]
    games = [W("A", "C")] + [W("A", x) for x in f[:6]]
    games += [W("B", x) for x in f[:6]] + [W(f[6], "B")]
    games += [W("C", x) for x in f[:5]] + [W(f[7], "C")]
    games += [W(f[i], f[5 + (i + j) % 4]) for i in range(5) for j in range(2)]  # fillers stay well below
    games += [G("A", f[6], day=3), G("A", f[7], day=10), G("B", f[8], day=3), G("B", f[7], day=10),
              G("C", f[8], day=10), G("C", f[6], day=11)]
    return games


def test_multi_team_tie_head_to_head_cant_settle_is_not_a_clinch():
    r = tr.race("Big 12", _three_way_risk(), NOW)
    a = row(r, "A")
    assert (a["w"], a["l"]) == (7, 0), a
    assert a["status"] == "alive", a


def test_one_more_win_clinches_when_a_three_way_tie_is_the_only_risk():
    r = tr.race("Big 12", _three_way_risk(), NOW)
    a = row(r, "A")
    assert a["clinch_with_win"] is True and a["controls"] is True, a


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
