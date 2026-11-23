"""
Picky's own CFP bracket (bcs.py): BCS-style rankings + cfp_rules.json, all in code.

- The BCS math on a fixture: poll points / max points (25 x voters), computers' (26 - rank) / 25 averaged, 1/3 each.
- Auto bids: a power-conference champion ranked outside the field gets in over an at-large; the top Group of Six team
  and Notre Dame in the top 12 too.
- Seeding per cfp_rules.json: straight by rank, auto bids outside the top 12 at the bottom, byes 1-4, 5v12 ... 8v9.
- The group-text format: plain lines, no markdown, no em dashes; the lane's numbers all have facts.
- Routing: 'projected CFP bracket' / 'who makes the CFP' / 'first four out' hit cfp_bracket; 'if it ended today'
  keeps cfp_now; the league's own playoff asks never hit it.

Run:  ./.venv/bin/python3 -I test_bcs.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, ".")
_FLUX = Path("/Users/Agent/cfb-pickem/server/.venv-flux/bin/python3")  # Pillow lives in the image venv
if not (Path(".venv-flux/bin/python3").exists() or os.environ.get("PICKY_PIL_PY")) and _FLUX.exists():
    os.environ["PICKY_PIL_PY"] = str(_FLUX)  # a git worktree has no venvs of its own
import bcs  # noqa: E402
import cfp_rules  # noqa: E402

RULES = cfp_rules.load()


def _poll(teams, voters=60):
    """A poll where team i gets (25 - i) x voters points, all first-place votes to No. 1."""
    return {"ranks": [{"rank": i, "team": t, "points": (25 - i + 1) * voters - (i - 1),
                       "first": voters if i == 1 else 0} for i, t in enumerate(teams, 1)],
            "others": [{"team": "Others U", "points": 30}]}


# --- the math ------------------------------------------------------------------------------------------

def test_poll_share_is_points_over_max_points():
    s = bcs.poll_shares({"ranks": [{"rank": 1, "team": "A", "points": 1500, "first": 60},
                                   {"rank": 2, "team": "B", "points": 1400, "first": 0}],
                         "others": [{"team": "C", "points": 15}]})
    assert s == {"A": 1.0, "B": 1400 / 1500, "C": 15 / 1500}, s
    # first-place votes undercounted: voters never below what the top total implies (1711 pts -> 69 voters)
    s = bcs.poll_shares({"ranks": [{"rank": 1, "team": "A", "points": 1711, "first": 50}]})
    assert abs(s["A"] - 1711 / (25 * 69)) < 1e-9, s


def test_poll_without_points_counts_ranks():
    s = bcs.poll_shares({"ranks": [{"rank": 1, "team": "A"}, {"rank": 25, "team": "Z"}]})
    assert s == {"A": 1.0, "Z": 1 / 25}, s


def test_computer_share_averages_normalized_ranks():
    comps = {"SP+": {"A": 30, "B": 20, "C": 10}, "FPI": {"A": 5, "B": 15, "C": 1}, "Elo": {}, "SRS": {"A": 9, "B": 9}}
    s = bcs.computer_shares(comps)
    # SP+: A1 B2 C3; FPI: B1 A2 C3; SRS: A1 B1 (tie shares the better rank); Elo missing -> averaged over 3
    assert abs(s["A"] - (1 + 24 / 25 + 1) / 3) < 1e-9, s
    assert abs(s["B"] - (24 / 25 + 1 + 1) / 3) < 1e-9, s
    assert abs(s["C"] - (23 / 25 + 23 / 25 + 0) / 3) < 1e-9, s


def test_bcs_score_is_a_third_each():
    teams = [f"T{i}" for i in range(1, 26)]
    polls_d = {"AP": _poll(teams), "Coaches": _poll(teams, 50)}
    comps = {"SP+": {t: 100 - i for i, t in enumerate(reversed(teams))}}  # computers have it upside down
    rows = bcs.rankings(polls_d, comps)
    by = {r["team"]: r for r in rows}
    ap, co = bcs.poll_shares(polls_d["AP"]), bcs.poll_shares(polls_d["Coaches"])
    cp = bcs.computer_shares(comps)
    for t in ("T1", "T13", "T25"):
        assert abs(by[t]["score"] - round((ap[t] + co[t] + cp[t]) / 3, 4)) < 1e-4, (t, by[t])
    assert by["Others U"]["score"] > 0 and [r["rank"] for r in rows] == list(range(1, len(rows) + 1))
    assert rows[0]["team"] == "T1", rows[:3]


def test_fbs_filter_drops_fcs_teams():
    rows = bcs.rankings({"AP": _poll(["A", "FCS Tech"])}, {}, fbs={"A"})
    assert [r["team"] for r in rows] == ["A"], rows


def test_elo_is_each_teams_newest_and_fpi_and_srs_come_from_the_cache():
    ds = {"games": [{"homeTeam": "A", "awayTeam": "B", "completed": True, "startDate": "2026-11-01",
                     "homePregameElo": 1500, "homePostgameElo": 1520, "awayPregameElo": 1600, "awayPostgameElo": 1580},
                    {"homeTeam": "B", "awayTeam": "A", "completed": False, "startDate": "2026-11-08",
                     "homePregameElo": 1580, "awayPregameElo": 1520}],
          "fpi": [{"team": "A", "fpi": 12.5}], "srs": [{"team": "A", "rating": 9.1}],
          "sp": [{"team": "A", "rating": 20.0}, {"team": "nationalAverages", "rating": 0}]}
    c = bcs.computers_from(ds)
    assert c["Elo"] == {"A": 1520.0, "B": 1580.0} and c["FPI"] == {"A": 12.5} and c["SRS"] == {"A": 9.1}, c
    assert c["SP+"] == {"A": 20.0}, c


def test_elo_prefers_the_last_final_over_a_stale_upcoming_pregame():
    ds = {"games": [{"homeTeam": "Alabama", "awayTeam": "X", "completed": True, "startDate": "2026-11-21",
                     "homePregameElo": 1990, "homePostgameElo": 2002},
                    {"homeTeam": "Auburn", "awayTeam": "Alabama", "completed": False, "startDate": "2026-11-28",
                     "awayPregameElo": 1979},
                    {"homeTeam": "New U", "awayTeam": "Y", "completed": False, "startDate": "2026-11-28",
                     "homePregameElo": 1400}]}
    elo = bcs.computers_from(ds)["Elo"]
    assert elo["Alabama"] == 2002.0 and elo["New U"] == 1400.0, elo


# --- bracket: auto bids + seeding ----------------------------------------------------------------------

ORDER = ["Georgia", "Ohio State", "Texas", "Oregon", "Notre Dame", "Penn State", "Alabama", "Indiana", "Tennessee",
         "Michigan", "LSU", "Ole Miss", "USC", "Miami", "Boise State", "Clemson", "Arizona State", "Tulane"]
CONF = {"Georgia": "SEC", "Ohio State": "Big Ten", "Texas": "SEC", "Oregon": "Big Ten", "Notre Dame": "FBS Independents",
        "Penn State": "Big Ten", "Alabama": "SEC", "Indiana": "Big Ten", "Tennessee": "SEC", "Michigan": "Big Ten",
        "LSU": "SEC", "Ole Miss": "SEC", "USC": "Big Ten", "Miami": "ACC", "Boise State": "Mountain West",
        "Clemson": "ACC", "Arizona State": "Big 12", "Tulane": "American Athletic"}
CHAMPS = {"SEC": "Georgia", "Big Ten": "Ohio State", "ACC": "Clemson", "Big 12": "Arizona State"}


def test_low_ranked_champs_get_in_over_at_larges():
    b = bcs.bracket(ORDER, CONF, CHAMPS, RULES)
    field = [t for _, t in b["seeds"]]
    assert len(field) == 12, field
    for t in ("Clemson", "Arizona State", "Boise State", "Notre Dame"):  # No. 16, 17 champs, top G6 (15), ND top 12
        assert t in field, (t, field)
    assert b["auto"]["Clemson"] == "ACC" and b["auto"]["Boise State"] == "G6" and b["auto"]["Notre Dame"] == "top 12"
    # the at-larges they bumped: No. 10 Michigan, No. 11 LSU and No. 12 Ole Miss are out, first in line
    assert b["first_out"] == ["Michigan", "LSU", "Ole Miss", "USC"], b["first_out"]
    assert "Tulane" not in field  # only the TOP Group of Six team gets the bid


def test_seeding_follows_the_rules_file():
    b = bcs.bracket(ORDER, CONF, CHAMPS, RULES)
    seeds = [t for _, t in b["seeds"]]
    # straight by rank for the top-12 teams, then the auto bids ranked outside it, in rank order
    assert seeds == ["Georgia", "Ohio State", "Texas", "Oregon", "Notre Dame", "Penn State", "Alabama", "Indiana",
                     "Tennessee", "Boise State", "Clemson", "Arizona State"], seeds
    assert b["byes"] == [(1, "Georgia"), (2, "Ohio State"), (3, "Texas"), (4, "Oregon")], b["byes"]
    assert b["first_round"] == [(5, 12), (6, 11), (7, 10), (8, 9)] == [tuple(p) for p in RULES["first_round"]]


def test_notre_dame_outside_the_top_12_is_out():
    order = [t for t in ORDER if t != "Notre Dame"] + ["Notre Dame"]
    b = bcs.bracket(order, CONF, CHAMPS, RULES)
    assert "Notre Dame" not in [t for _, t in b["seeds"]], b["seeds"]


def test_title_game_winner_beats_the_standings_leader():
    ds = {"games": [{"id": 1, "homeTeam": "Texas", "awayTeam": "Georgia", "homeConference": "SEC",
                     "awayConference": "SEC", "notes": "SEC Championship", "homePoints": 31, "awayPoints": 24},
                    {"id": 2, "homeTeam": "Ohio State", "awayTeam": "Oregon", "homeConference": "Big Ten",
                     "awayConference": "Big Ten", "notes": "Big Ten Championship", "homePoints": None,
                     "awayPoints": None}],
          "lines": [{"id": 2, "homeScore": None, "awayScore": None}]}
    assert bcs.title_game_winners(ds, {"SEC", "Big Ten"}) == {"SEC": "Texas"}


def test_projected_champ_tie_goes_to_better_bcs_rank():
    race = {"SEC": {"leader": "Texas", "ties": [{"teams": ["Texas", "Georgia"], "settled_by": None}]},
            "ACC": {"leader": "Clemson", "ties": [{"teams": ["Clemson", "Miami"], "settled_by": "h2h"}]}}
    got = bcs.projected_champs(["SEC", "ACC", "Big 12"], {"Georgia": 1, "Texas": 3, "Miami": 14, "Clemson": 16},
                               race=lambda c: race.get(c))
    assert got == {"SEC": "Georgia", "ACC": "Clemson"}, got


# --- text ---------------------------------------------------------------------------------------------

def test_text_format():
    b = bcs.bracket(ORDER, CONF, CHAMPS, RULES)
    t = bcs.text(b, 13)
    assert t.splitlines() == [
        "My bracket (BCS formula, wk 13):",
        "Byes: 1 Georgia, 2 Ohio State, 3 Texas, 4 Oregon",
        "5 Notre Dame vs 12 Arizona State",
        "6 Penn State vs 11 Clemson",
        "7 Alabama vs 10 Boise State",
        "8 Indiana vs 9 Tennessee",
        "First 4 out: Michigan, LSU, Ole Miss, USC",
        "Projected champs: Georgia (SEC), Ohio State (Big Ten), Clemson (ACC), Arizona State (Big 12)",
    ], t
    assert not any(c in t for c in "*#_`—–"), t
    assert bcs.text(b, 15, projected=False).splitlines()[-1].startswith("Champs: ")


def test_lane_numbers_all_have_facts():
    import factstore
    b = bcs.bracket(ORDER, CONF, CHAMPS, RULES)
    t, f = bcs.text(b, 13), bcs.facts(b, 13, "2026-11-22")
    assert factstore.unsupported(t, f) == [], factstore.unsupported(t, f)


def test_lane_sends_the_bracket_verbatim_and_falls_back_without_one():
    import big_picture
    import factstore
    import router
    b = bcs.bracket(ORDER, CONF, CHAMPS, RULES)
    fake = {"text": bcs.text(b, 13), "facts": bcs.facts(b, 13, "2026-11-22")}
    real_b, real_now = factstore.bcs_bracket, big_picture.cfp_now_text
    try:
        factstore.bcs_bracket = lambda: fake
        out = router.fact_answer("cfp_bracket", {"teams": []}, "what's your projected CFP bracket?")
        assert out == fake["text"] and out.facts_used, out
        factstore.bcs_bracket = lambda: None
        assert big_picture.cfp_bracket_text({}, "projected CFP bracket?") is None
        big_picture.cfp_now_text = lambda ctx, q: "committee field"
        assert router.fact_answer("cfp_bracket", {"teams": []}, "projected CFP bracket?") == "committee field"
    finally:
        factstore.bcs_bracket, big_picture.cfp_now_text = real_b, real_now
    assert big_picture.cfp_bracket_text in router.STORE_LANES


def test_polls_parse_keeps_points_votes_others_and_week():
    import polls
    raw = {"rankings": [{"type": "ap", "date": "2026-11-22T08:00Z", "occurrence": {"number": 13},
                         "ranks": [{"current": 1, "points": 1711.0, "firstPlaceVotes": 56,
                                    "team": {"location": "Georgia"}, "recordSummary": "11-0"}],
                         "others": [{"current": 0, "points": 95.0, "team": {"location": "New Mexico"}}]}]}
    p = polls.parse(raw, records=[{"team": "Georgia"}, {"team": "New Mexico"}])["AP"]
    assert p["week"] == 13 and p["ranks"][0]["points"] == 1711.0 and p["ranks"][0]["first"] == 56, p
    assert p["others"] == [{"team": "New Mexico", "points": 95.0}], p


# --- routing ------------------------------------------------------------------------------------------

def test_bracket_asks_route_to_cfp_bracket():
    import router
    for q in ("what's your projected CFP bracket?", "who makes the CFP?", "Picky who's in the college football playoff",
              "what's the CFP picture look like", "cfp seeds?", "who are the first four out of the CFP",
              "give me your CFP projection"):
        assert (router.fact_intent(q) or ("",))[0] == "cfp_bracket", (q, router.fact_intent(q))
        assert router.classify(q).label() == "fact:cfp_bracket", (q, router.classify(q).label())


def test_now_asks_keep_cfp_now_and_league_playoff_asks_never_hit_the_bracket():
    import router
    for q in ("Who's in the College Football Playoff if it ended today?", "cfp bracket as it stands?",
              "who's on the CFP bubble?"):
        assert router.fact_intent(q)[0] == "cfp_now", (q, router.fact_intent(q))
    for q in ("what are my odds to make the playoffs?", "odds to make the playoffs", "who makes the playoffs?",
              "If the season ended today, who makes the playoffs?", "what do I need to make playoffs",
              "who's in the league playoffs", "projected playoff seeds in our league", "playoff bracket for the pool?",
              "Is the playoff committee biased toward the SEC?", "who should make the CFP?"):
        assert (router.fact_intent(q) or ("",))[0] != "cfp_bracket", (q, router.fact_intent(q))


# --- the picture -------------------------------------------------------------------------------------

def _card_data():
    b = bcs.bracket(ORDER, CONF, CHAMPS, RULES)
    recs = {t: "10-1" for _, t in b["seeds"]} | {t: "8-3" for t in b["first_out"]}
    return {"bracket": {k: v for k, v in b.items() if k != "rank"}, "records": recs, "week": 13,
            "text": bcs.text(b, 13), "facts": bcs.facts(b, 13, "2026-11-22")}


def test_render_is_a_valid_png_of_the_expected_size():
    import struct
    import bracket_card
    d = _card_data()
    sp = bracket_card.spec(d["bracket"], d["records"], 13, RULES)
    assert sp["title"] == "Picky's bracket, BCS formula, wk 13", sp["title"]
    assert [(g["bye"], g["a"], g["b"]) for g in sp["games"]] == [(1, 8, 9), (4, 5, 12), (3, 6, 11), (2, 7, 10)]
    assert sp["tags"]["Clemson"] == "ACC champ" and sp["tags"]["Boise State"] == "G6 auto", sp["tags"]
    img = bcs.card_png(d, RULES)
    assert img and img[:8] == b"\x89PNG\r\n\x1a\n", img[:16] if img else img
    w, h = struct.unpack(">II", img[16:24])
    assert w == 1200 and h == 130 + 4 * (2 * 58 + 34) + 120, (w, h)


def test_render_failure_returns_none():
    import bracket_card
    real, real_py = bracket_card.draw, bracket_card.PIL_PY
    try:
        bracket_card.draw = lambda sp: (_ for _ in ()).throw(ImportError("no PIL"))
        bracket_card.PIL_PY = Path("/nonexistent/python3")
        assert bcs.card_png(_card_data(), RULES) is None
    finally:
        bracket_card.draw, bracket_card.PIL_PY = real, real_py


def test_bracket_picture_and_its_text_fallbacks():
    import base64
    import group_chat
    import group_outbox
    d = _card_data()
    reply = d["text"] + " Chalk with a side of chaos."
    sent = []
    saved = (group_outbox.img_ready, group_outbox.post_picture, bcs.current, bcs.card_png)
    try:
        bcs.current = lambda: d
        bcs.card_png = lambda d_, rules=None: b"\x89PNG\r\n\x1a\nfake"
        group_outbox.post_picture = lambda chat, text, b64, **kw: sent.append((chat, text, b64)) or "queued"
        group_outbox.img_ready = lambda now=None: True
        assert group_chat.bracket_picture(reply, "chat-guid") == "Chalk with a side of chaos."
        assert sent and sent[0][0] == "chat-guid" and base64.b64decode(sent[0][2]).startswith(b"\x89PNG")
        assert group_chat.bracket_picture(d["text"], "chat-guid") == group_chat.BRACKET_TAKE  # no voice line
        sent.clear()
        # fallbacks: no picture watcher, no chat, render failed, the outbox paused, the code text got cut
        group_outbox.img_ready = lambda now=None: False
        assert group_chat.bracket_picture(reply, "chat-guid") is None
        group_outbox.img_ready = lambda now=None: True
        assert group_chat.bracket_picture(reply, "") is None
        bcs.card_png = lambda d_, rules=None: None
        assert group_chat.bracket_picture(reply, "chat-guid") is None
        bcs.card_png = lambda d_, rules=None: b"\x89PNG\r\n\x1a\nfake"
        group_outbox.post_picture = lambda chat, text, b64, **kw: "paused"
        assert group_chat.bracket_picture(reply, "chat-guid") is None
        assert group_chat.bracket_picture("My bracket (BCS formula, wk 13): cut", "chat-guid") is None
        assert not sent
    finally:
        group_outbox.img_ready, group_outbox.post_picture, bcs.current, bcs.card_png = saved


def test_img_ready_follows_the_watcher_poll():
    import time
    import tempfile
    import group_outbox
    real_file, real_pause = group_outbox.IMG_OK_FILE, group_outbox.PAUSE_FILE
    try:
        tmp = Path(tempfile.mkdtemp())
        group_outbox.IMG_OK_FILE, group_outbox.PAUSE_FILE = tmp / "img_ok", tmp / "PAUSED"
        assert not group_outbox.img_ready()
        group_outbox.IMG_OK_FILE.touch()
        assert group_outbox.img_ready() and not group_outbox.img_ready(now=time.time() + 3600)
        group_outbox.PAUSE_FILE.touch()
        assert not group_outbox.img_ready()
    finally:
        group_outbox.IMG_OK_FILE, group_outbox.PAUSE_FILE = real_file, real_pause


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
