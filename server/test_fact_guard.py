"""fact_guard: member W-L / place / gap claims, pool line numbers, wrong premises (offline, synthetic league)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fact_guard as fg  # noqa: E402

CTX = {
    "current_week": 12,
    "you": {"team_name": "Alpha"},
    "standings_ytd": [
        {"rank": 1, "team": "Alpha", "w": 30, "l": 18},
        {"rank": 2, "team": "Bravo", "w": 28, "l": 20},
        {"rank": 3, "team": "Charlie Team", "w": 27, "l": 21},
        {"rank": 3, "team": "Delta", "w": 27, "l": 21},
        {"rank": 5, "team": "Echo", "w": 23, "l": 25},
        {"rank": 6, "team": "Zulu", "w": 19, "l": 29},
    ],
    "weekly_records": {
        "Alpha": {10: [3, 1], 11: [4, 0]}, "Bravo": {10: [4, 0], 11: [3, 1]},
        "Charlie Team": {10: [3, 1], 11: [3, 1]}, "Delta": {10: [3, 1], 11: [2, 2]},
        "Echo": {10: [2, 2], 11: [1, 3]}, "Zulu": {10: [3, 1], 11: [1, 3]},
    },
    "last_week": {"week": 11, "games": [
        {"away": "North Texas", "home": "UTSA", "away_pts": 16, "home_pts": 35, "line": 2.5, "covered": "UTSA",
         "picks": {"Alpha": "UTSA", "Bravo": "North Texas", "Zulu": "North Texas"}},
        {"away": "Oregon State", "home": "Boise State", "away_pts": 41, "home_pts": 46, "line": -13.5,
         "covered": "Oregon State", "picks": {"Alpha": "Oregon State", "Bravo": "Boise State"}},
    ]},
    "games_this_week": [
        {"id": 1, "away": "Iowa", "home": "Illinois", "line": 8.5},
        {"id": 2, "away": "LSU", "home": "Tennessee", "line": 1.5},
    ],
    "picky_player": {"season": [0, 3], "last_week": [0, 3]},
}
LORE = {"members": {"Alpha": ["Alpha, stats-page oddity: Alpha is 3-7 in Texas games over the last two seasons.",
                               "Alpha, stats-page oddity: Alpha has picked against Iowa 4 times over the last two "
                               "seasons and gone 1-3."]}}


def same(a, b):
    return (a or "").lower() == (b or "").lower()


def check():
    return fg.member_claim_check(CTX, "", LORE, CTX["picky_player"])


def test_true_member_claims_pass():
    ok = check()
    for s in ["You're 30-18 on the season and in 1st place.", "Bravo is 28-20, 2 points behind first.",
              "Zulu is in last place at 19-29.", "You went 4-0 last week.", "7-1 lately is legit.",
              "You're 3-7 in Texas games over the last two seasons, though.", "I'm 0-3 this year.",
              "Charlie Team and Delta are tied for 3rd.", "Echo sits 5th, 7 back of first.",
              "You're sitting at 30-18 for the season, while I'm out here 0-3.",
              "You're not 33-15, you're 30-18.",
              "You're 30-18, 1st, in a playoff spot, 2 ahead of 5th place (23), with about 16 picks left.",
              "Echo is 23-25, 5th, 1 behind the cut line (4th place has 27).",
              "Closest to you (30-18): Bravo 2 back.",
              "You're asking like you have a chance of catching up to his 28-20 record.",
              "Bravo is 28-20 on the season and he went 3-1 last week.",
              "He keeps up his 8-19 trend against Alpha."]:
        assert ok(s), s


def test_wrong_member_claims_drop():
    ok = check()
    for s in ["Your 33-15 record is nice.", "You're 5-3 lately.", "Bravo is in 3rd place.", "You're third.",
              "Zulu is 3 points behind first.", "I'm 9-2 on the season.", "Echo went 4-0 last week.",
              "Echo is in last place.", "You're 30-18 overall and 4 wins ahead of 4th.",
              "You're chasing his 31-17 record."]:
        assert not ok(s), s


def test_school_qualified_records():
    ok = check()
    assert ok("You're 3-7 in Texas games over the last two seasons.")
    assert ok("You're 1-3 picking against Iowa.")
    assert not ok("You're 1-3 on Iowa picks.")  # the lore line is AGAINST Iowa (fact exam 2026-11-17)
    assert not ok("You're 3-1 in Texas games.")  # a weekly W-L doesn't count for a school split
    assert not ok("Bravo is 3-1 in Ohio State games.")


def test_monday_window_last_week_means_either():
    # Monday after week 11 went final: the pool's current week is still 11 and its 'last week' is 10
    mon = dict(CTX, current_week=11, last_week=dict(CTX["last_week"], week=10))
    ok = fg.member_claim_check(mon, "", LORE, CTX["picky_player"], monday_window=True)
    assert ok("Bravo went 4-0 last week.")  # week 10, the pool's 'last week'
    assert ok("Bravo went 3-1 last week.")  # week 11, the week that just ended
    assert not ok("Bravo went 1-3 last week.")


def test_line_numbers():
    cards = {"1": {"market": {"home_now": 6.5, "home_open": 7.0}}}
    ok = fg.line_num_check(CTX, cards, corpus="Alpha's week-10 miss: LSU -5.5 vs Alabama")
    assert ok("Iowa -8.5 in the pool.")
    assert ok("The market has Iowa -6.5.")
    assert ok("Tennessee +1.5 is live.")
    assert ok("You missed on LSU -5.5 two weeks ago.")  # printed in the prompt
    assert not ok("Iowa -6.0 is the number.")
    assert not ok("Grab Tennessee +4.5.")
    assert not ok("North Texas lost to UTSA 16-35 as a -3 favorite.")  # last week's league line was -2.5
    assert ok("North Texas lost to UTSA 16-35 as a -2.5 favorite.")
    ok2 = fg.line_num_check(CTX, cards, corpus="ESPN closing: North Texas -3")
    assert not ok2("North Texas lost as a -3 favorite.")  # last week's game: the league line only


def test_premises():
    pf = lambda q, ctx=CTX: fg.premise_facts(ctx, q, same)  # noqa: E731
    assert "30-18" in pf("since I'm 33-15 on the season, should I play it safe?")[0]
    assert pf("I'm 30-18, who should I take") == []
    assert pf("now that I'm in first who's chasing me") == []
    zulu = dict(CTX, you={"team_name": "Zulu"})
    assert "6th" in pf("now that I'm in first, who's chasing me?", zulu)[0]
    assert "took UTSA" in pf("why did I take North Texas last week? that was dumb")[0]
    assert "Oregon State covered" in pf("Boise State covered last week right?")[0]
    assert pf("UTSA covered last week right?") == []
    assert "16" in pf("UTSA North Texas ended 45-16 right?")[0]
    assert "Iowa -8.5" in pf("Iowa -15.5 in the pool this week, easy money right?")[0]
    note = fg.premise_note(CTX, "since I'm 33-15 on the season, should I play it safe?", same)
    assert note.startswith("PREMISE CHECK") and "33-15" in note


def test_premise_echo():
    e = fg.premise_echo_check(CTX, "since I'm 33-15 on the season, should I play it safe?", same)
    assert not e("At 33-15 you can coast.")
    assert e("You're not 33-15, you're 30-18.")
    assert fg.premise_echo_check(CTX, "who's in first?", same) is None


IDX = {"teams": {
    "texas": {"name": "Texas", "record": "8-2", "conf_record": "5-2", "ats": (5, 5, 0, 1.5),
              "scores": [(34, 20), (24, 27)], "games": [1]},
    "lsu": {"name": "LSU", "record": "7-3", "conf_record": "4-3", "ats": (4, 6, 0, -2.0),
            "scores": [(13, 31)], "games": [1]},
    "alabama": {"name": "Alabama", "record": "8-2", "conf_record": "6-1", "ats": (6, 4, 0, 1.0),
                "scores": [(31, 13)], "games": [1]},
}}


def test_team_facts():
    ok = fg.team_fact_check(IDX, corpus="GAME CARD: LSU 2-5 ATS as a road team; CFP: No. 9 Alabama", members=["Alpha"])
    assert ok("Texas is 8-2 and 5-2 in the SEC.")
    assert not ok("Texas is 9-1.")
    assert ok("Alabama beat LSU 31-13.")
    assert not ok("Alabama beat LSU 35-13.")
    assert ok("LSU is 2-5 ATS on the road.")  # printed in the card
    assert ok("No. 9 Alabama is rolling.")
    assert not ok("No. 3 Alabama is rolling.")
    assert ok("Alpha is 30-18 and Texas is a mess.")  # member W-Ls are member_claim_check's
    assert ok("Texas isn't 9-1, it's 8-2.")


def test_team_facts_accept_todays_finals():
    """Saturday night: Texas beat LSU 30-17 tonight, the CFBD cache still says 8-2 / 7-3 (review R6)."""
    idx = {"teams": {k: {**v, "conference": "SEC"} for k, v in IDX["teams"].items()}}
    live = {"games": {"1": {"state": "post", "away": {"name": "LSU"}, "home": {"name": "Texas"},
                            "away_score": 17, "home_score": 30},
                      "2": {"state": "in", "away": {"name": "Alabama"}, "home": {"name": "Auburn"},
                            "away_score": 21, "home_score": 0},
                      "3": {"state": "post", "away": {"name": "Alabama"}, "home": {"name": "LSU"},
                            "away_score": 31, "home_score": 13}}}  # already in the cache: no bump
    fin = fg.finals_overrides(idx, live, lambda n, ix: ix["teams"].get(n.lower()))
    assert fin["Texas"]["record"] == "9-2" and fin["Texas"]["conf_record"] == "6-2"
    assert fin["LSU"]["record"] == "7-4" and "Alabama" not in fin
    ok = fg.team_fact_check(idx, corpus="", members=[], finals=fin)
    assert ok("Texas is 9-2 after tonight.")
    assert not ok("Texas is 8-2.")  # the stale morning record
    assert ok("Texas beat LSU 30-17.")
    assert ok("LSU fell to 7-4.")
    assert ok("Alabama is 8-2.")  # no final tonight: cache stands
    plain = fg.team_fact_check(idx, corpus="", members=[])
    assert plain("Texas is 8-2.") and not plain("Texas is 9-2 after tonight.")


def test_pick_sides():
    ok = fg.pick_side_check(CTX, same)
    assert ok("Bravo took North Texas and got burned.")
    assert not ok("Bravo took UTSA.")
    assert ok("Alpha had UTSA.")
    assert not ok("Zulu's on UTSA again.")


def test_casino_claims():
    lore = {"casino": {"members": {"Bravo": ["Bravo's blackjack blunders: doubled on a hard 12 in week 5 (J 2 J), and busted."],
                                   "Alpha": ["Alpha, The Pit 2026: 0 chips after 6 hands. BUSTED OUT."]}}}
    ok = fg.casino_check(CTX, lore)
    assert ok("Bravo doubled on a hard 12 and busted.")
    assert not ok("You doubled on a hard 12 and busted.")  # the sender (Alpha) never did
    assert ok("You're at 0 chips, busted out of the Pit.")
    assert not ok("Zulu split tens at the blackjack table.")


def test_unplayed_results():
    ok = fg.unplayed_result_check(CTX)
    assert not ok("You hit the Iowa -8.5 and missed LSU -1.5.")
    assert not ok("Tennessee covered easily.")
    assert ok("Iowa needs to win by 9 to cover.")
    assert ok("If Tennessee covers you're golden.")
    assert ok("UTSA covered by 21.5.")  # last week's game


def test_all_wl_true():
    assert fg.all_wl_true(CTX, "You took UTSA and they crushed North Texas 35-16.", LORE)
    assert fg.all_wl_true(CTX, "Don't end up with a 1-3 week like Echo.", LORE)
    assert not fg.all_wl_true(CTX, "Don't end up with a 0-4 week like Echo.", LORE)
    assert not fg.all_wl_true(CTX, "UTSA crushed North Texas 45-16.", LORE)
    assert not fg.all_wl_true(CTX, "No records here.", LORE)


def test_coach_moves():
    ok = fg.coach_move_check("Coach note: UTEP fired Scotty Walden. Kyle Whittingham is Michigan's coach (no firing).")
    assert ok("UTEP fired Scotty Walden.")
    assert not ok("Michigan fired Kyle Whittingham.")
    assert not ok("Brian Kelly got fired.")
    assert ok("Michigan still has Kyle Whittingham.")



def test_stat_rank_proximity():
    corpus = ("GAME CARD Wake Forest at Duke: SP+ Wake Forest #31, Duke #45; Duke run D 62nd; rest: Duke 7 days, "
              "Wake Forest 14 days; SP+ projects Duke by 3.4.")
    ok = fg.stat_rank_check({"teams": {"duke": {"name": "Duke", "games": [1]},
                                       "wake forest": {"name": "Wake Forest", "games": [1]}}}, corpus)
    assert ok("Wake Forest is SP+ #31.") and ok("SP+ projects Duke by 3.4.") and ok("Duke has 7 days of rest.")
    assert not ok("Wake Forest SP+ #7 offense.")
    assert not ok("SP+ projects Duke by 7.")
    assert not ok("Duke gets 6 days of rest.")
    assert not ok("Their kicker is hitting 44% of FGs.")
    assert ok("Duke missed (by 21.5) last week.")  # cover margins are result_check's
    assert not ok("They're paying a $50 million buyout.")


def test_ats_split_labels():
    ok = fg.ats_split_check("Georgia (9-1): ATS 8-2 (fav 7-2, dog 1-0, home 4-1, road 4-1)")
    assert not ok("Georgia is 8-2 ATS as a favorite.")
    assert ok("Georgia is 7-2 ATS as a favorite.") and ok("Georgia is 8-2 ATS.")


def test_attribution():
    ok = fg.attribution_check("NEWS: Iowa's Kirk Ferentz said Tuesday the QB is day to day.")
    assert ok("Ferentz said the QB is day to day.")
    assert not ok("Kiffin admitted he forgot his Tennessee history.")


if __name__ == "__main__":
    fails = 0
    for name, f in list(globals().items()):
        if name.startswith("test_"):
            try:
                f()
                print("ok  ", name)
            except AssertionError as ex:
                fails += 1
                print("FAIL", name, ex)
    sys.exit(1 if fails else 0)
