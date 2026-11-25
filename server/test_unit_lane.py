"""Unit matchup code lane (Phase 6 of docs/picky-code-ontology-plan-2026-11-18.md): unit_lane.py answers
'can X run on Y' style asks in code from units.py, both directions, style, SP+ unit ranks and one computed
verdict. Model-free: a synthetic source (no CFBD cache, no network), plus a smoke pass on the real cache when one
is on this box (any upcoming game, never a fixed exam item).

Asks are written from question TYPES (real traffic + the genius matchups category), never item ids or keys.

Run:  ./.venv/bin/python3 -I test_unit_lane.py
"""

from __future__ import annotations
import os
os.environ["PICKY_LANES_OFF"] = ""  # these tests exercise the held-back lanes (router.LANES_OFF_DEFAULT)

import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import cfbd  # noqa: E402
import router  # noqa: E402
import units  # noqa: E402
import unit_lane  # noqa: E402

N = 40


def adv_row(team: str, ko: int, kd: int, run_rate: float = 0.5, plays: int = 700) -> dict:
    """A team whose offense ranks ko and defense ranks kd (1 = best of N) on every advanced metric."""
    go, gd = (N - ko) / N, (N - kd) / N  # 1 = best
    return {"team": team,
            "offense": {"ppa": go, "successRate": go, "explosiveness": go, "pointsPerOpportunity": 2 + 3 * go,
                        "plays": plays, "havoc": {"total": 1 - go},
                        "rushingPlays": {"ppa": go, "successRate": go, "rate": run_rate},
                        "passingPlays": {"ppa": go, "successRate": go}},
            "defense": {"ppa": 1 - gd, "successRate": 1 - gd, "explosiveness": 1 - gd,
                        "pointsPerOpportunity": 2 + 3 * (1 - gd), "stuffRate": 0.1 + 0.2 * gd,
                        "havoc": {"total": gd, "frontSeven": gd},
                        "rushingPlays": {"ppa": 1 - gd, "successRate": 1 - gd},
                        "passingPlays": {"ppa": 1 - gd, "successRate": 1 - gd}}}


# filler teams: team i is i-th best on both sides; the named teams below take their ranks' places
SPEC = {  # name: (offense rank, defense rank, run rate, plays per game x10)
    "Northwood": (3, 4, 0.55, 700),        # good on both sides
    "Lakeview State": (36, 35, 0.45, 620),  # bad on both sides
    "Hill Tech": (2, 38, 0.40, 820),        # elite offense, bad defense, fast
    "Bayside": (4, 37, 0.42, 800),          # same profile
    "Granite": (20, 21, 0.50, 700),         # middling
    "Marsh A&M": (21, 20, 0.80, 600),       # middling, option team (runs 80%)
    "Stonewall": (35, 3, 0.50, 600),        # bad offense, elite defense, slow
    "Ironside": (37, 2, 0.50, 610),         # same profile
    "Iowa": (25, 6, 0.55, 640),             # for test_speed's wk12 fixture (Iowa at Illinois)
    "Illinois": (12, 18, 0.50, 690),
}


def synthetic_table() -> dict:
    teams, adv = {}, []
    for i in range(1, N + 1):
        nm = f"Filler {i}"
        teams[cfbd.norm(nm)] = {"name": nm, "sp": {"rank": i, "off_rank": i, "def_rank": i},
                                "stats": {"games": 10, "sacks": 40 - i, "passAttemptsOpponent": 300,
                                          "sacksOpponent": i, "passAttempts": 300, "turnovers": i,
                                          "turnoversOpponent": 40 - i}}
        adv.append(adv_row(nm, i, i))
    for nm, (ko, kd, rr, pl) in SPEC.items():
        teams[cfbd.norm(nm)] = {"name": nm, "sp": {"rank": (ko + kd) // 2, "off_rank": ko, "def_rank": kd},
                                "stats": {"games": 10, "sacks": 40 - kd, "passAttemptsOpponent": 300,
                                          "sacksOpponent": ko, "passAttempts": 300, "turnovers": ko,
                                          "turnoversOpponent": 40 - kd}}
        adv.append(adv_row(nm, ko, kd, rr, pl))
    idx = {"teams": teams, "players": [], "by_team": {}, "venues": [], "fetched_at": 1}
    return idx, units.table(idx, adv)


class FakeSource:
    """unit_lane's data interface over synthetic data: games are any two SPEC teams named in the text."""

    def __init__(self, spread: float | None = -3.0, total: float | None = 45.5):
        self.idx, self.tab = synthetic_table()
        self.spread, self.total = spread, total

    def units_table(self) -> dict:
        return self.tab

    def sp(self, team: str) -> dict | None:
        t = self.idx["teams"].get(cfbd.norm(team))
        return (t or {}).get("sp")

    def game(self, q: str, ctx: dict | None = None) -> dict | None:
        qn = f" {cfbd.norm(re.sub(r"['’]s\b", "", q or ''))} "
        named = sorted((qn.find(f" {cfbd.norm(n)} "), n) for n in SPEC if f" {cfbd.norm(n)} " in qn)
        if len(named) < 2:
            return None
        a, h = sorted([named[0][1], named[1][1]], key=lambda n: list(SPEC).index(n))  # fixed away/home order
        return {"away": a, "home": h, "spread": self.spread, "total": self.total}


SRC = FakeSource()


def fake_source() -> FakeSource:
    return FakeSource()


def build(q: str, src=None):
    return unit_lane.build(q, {}, src or SRC)


# ---------------------------------------------------------------------------
# parsing: the unit-ask shapes in real traffic and the genius matchups category
# ---------------------------------------------------------------------------

SHAPES = [  # (text, units expected (first), offense asked or None)
    ("Can Granite run on Northwood?", "rush", "Granite"),
    ("Can Northwood run the ball on Granite?", "rush", "Northwood"),
    ("Can Northwood stop Granite's run game?", "rush", "Granite"),
    ("Granite wants to run it on Northwood. Can they?", "rush", "Granite"),
    ("Northwood -3.5 at Marsh A&M. How does Northwood's defense match up with the option?", "rush", "Marsh A&M"),
    ("Hill Tech at Bayside has a 70 total. Who has the edge throwing it?", "pass", None),
    ("Can Granite throw it on Northwood's secondary?", "pass", "Granite"),
    ("Can Granite score on Northwood?", "eff", "Granite"),
    ("Can Granite move the ball on Northwood?", "eff", "Granite"),
    ("Can Granite's offense do anything against Northwood?", "eff", "Granite"),
    ("Northwood's defense vs Granite's offense, who wins that?", "eff", "Granite"),
    ("Who has the edge when Granite's offense faces Northwood's defense, and the other way around?", "eff", None),
    ("Who finishes drives in Granite-Northwood?", "redzone", None),
    ("Northwood -20.5 vs Granite. Do they finish drives or kick field goals?", "redzone", None),
    ("Who wins the trenches in Granite-Northwood?", "rush", None),
    ("Will Northwood's pass rush get home against Granite?", "protect", "Granite"),
    ("Hill Tech-Bayside: is this a shootout? Who plays faster?", "pace", None),
    ("The Hill Tech-Bayside total is 61.5. Is that a pace thing? How many plays do these teams run?", "pace", None),
    ("Who wins the turnover battle in Granite-Northwood?", "turnovers", None),
    ("Where does Northwood have the edge on Granite?", "all", None),
    ("how do granite and northwood match up", "all", None),
    ("Does Granite's run game work against Northwood's defense, and can Northwood throw on Granite?", "rush", None),
    ("Can Granite run on Northwood, and can Northwood run on Granite?", "rush", None),
    ("can northwood run on lakeview state", "rush", "Northwood"),  # lowercase, no '?' (texty)
]


def test_parses_every_unit_shape():
    for q, unit, off in SHAPES:
        p = unit_lane.parse(q, {}, SRC)
        assert p, f"no parse: {q}"
        assert p["units"][0] == unit, (q, p["units"])
        if off:
            assert p["off"] == off, (q, p["off"])


def test_two_unit_ask_keeps_both_units():
    p = unit_lane.parse("Does Granite's run game work against Northwood's defense, and can Northwood throw on Granite?",
                        {}, SRC)
    assert p["units"][:2] == ["rush", "pass"], p["units"]
    assert unit_lane.parse("Who wins the trenches in Granite-Northwood?", {}, SRC)["units"][:2] == ["rush", "protect"]


NOT_UNIT = [  # real-traffic shapes that must stay where they are (or on the full brain)
    "turnovers in the granite game",                       # a Saturday box-score ask, no question
    "how many picks has arch thrown",                      # a player stat
    "How many rushing yards does Joe Granitesmith have this season?",
    "steve. 1-3 last week. so honestly what do i do with granite at northwood, its basically a pick em right? i was "
    "gonna take northwood because their place is a nightmare but my buddy says granite's defense is legit and they "
    "can run on anybody",                                  # long mixed text: two asks, full brain
    "who should I take in Granite at Northwood?",          # a pick ask
    "who covers Granite Northwood, can Granite run on them?",  # pick + unit: not a pure unit ask
    "can I still change my picks?",
    "Can Granite run on somebody?",                        # one team, no game
    "If Granite -3 is the line and they win by exactly 3, do I get a push in our league?",  # a rules ask
    "Northwood is favored by 7, does the spread matter if they win by 7?",
]


def test_rules_and_spread_asks_are_not_unit_asks():
    """'is the line and they win by exactly 3' trips units' trenches words; a push/spread ask is the rules lane."""
    for q in ("If Granite -3 is the line and they win by exactly 3, do I get a push in our league?",
              "Northwood is favored by 7, does the spread matter if they win by 7?",
              "what happens if the line is 3 and they win by 3"):
        assert not unit_lane.wants(q), q


def test_not_unit_asks():
    for q in NOT_UNIT:
        assert not (unit_lane.wants(q) and unit_lane.parse(q, {}, SRC)), q


# ---------------------------------------------------------------------------
# the answer: both directions, style, SP+ unit ranks, a verdict that follows from the numbers, <= 700 chars
# ---------------------------------------------------------------------------

def test_both_directions_always_present():
    for q, unit, off in SHAPES:
        b = build(q)
        assert b and b["text"], q
        p = unit_lane.parse(q, {}, SRC)
        a, h = p["away"], p["home"]
        for e in b["edges"]:
            if e["unit"] in units.PAIRS:
                dirs = {(x["off"], x["def"]) for x in b["edges"] if x["unit"] == e["unit"]}
                assert {(a, h), (h, a)} <= dirs, (q, e["unit"], dirs)
        # both teams' offense show up in the text, asked direction or not
        t = b["text"]
        k = 2 if p["units"][0] in units.PAIRS or p["units"][0] == "all" else 1  # pace / turnovers: one line, both teams
        assert t.count(a) >= k and t.count(h) >= k, (q, t)


def test_asked_direction_comes_first():
    t = build("Can Granite run on Northwood?")["text"]
    first = re.split(r"(?<=[.!?])\s", t)[0]
    assert "Granite" in first and "run" in first, first
    assert "Northwood" in t[len(first):], t  # the reverse direction follows


_YES = re.compile(r"^(Yes|Yeah|I think so)\b")
_NO = re.compile(r"^(Not really|Not much|Tough ask)\b")


def test_yes_no_follows_the_question_subject():
    """'Will X's pass rush get home' / 'can X stop Y' ask about the DEFENSE: yes = the defense wins."""
    assert _YES.search(build("Will Northwood's pass rush get home against Lakeview State?")["text"])
    assert _YES.search(build("Can Northwood stop Lakeview State's run game?")["text"])
    assert _NO.search(build("Can Lakeview State stop Northwood's run game?")["text"])
    assert _YES.search(build("Can Northwood run on Lakeview State?")["text"])
    assert _NO.search(build("Can Lakeview State run on Northwood?")["text"])
    assert _NO.search(build("Can Lakeview State's defense be trusted to hold Northwood?")["text"])


def test_yes_no_after_a_leading_line():
    t = build("Northwood -16.5 vs Lakeview State. Can Northwood's defense be trusted to hold a big number?")["text"]
    assert _YES.search(t), t


def test_no_doubled_which():
    for q, _, _ in SHAPES:
        for sd in range(6):
            t = unit_lane.build(q, {}, SRC, seed=sd)["text"]
            assert not re.search(r"\bWhich\b[^.]*\bwhich\b", t), t


def test_either_or_question_gets_no_yes_no():
    """'Do they finish drives or kick field goals?' is a choice, not a yes/no (genius 11-18: 'I think so')."""
    for sd in range(6):
        t = unit_lane.build("Northwood -20.5 vs Granite. Do they finish drives or kick field goals?", {}, SRC, seed=sd)["text"]
        assert not _YES.search(t) and not _NO.search(t) and not re.match(r"(It's close|Coin flip|Hard to say)", t), t


def _swapped_table():
    """Lakeview State: bad overall, but the better big-play team both ways."""
    import copy
    src = FakeSource()
    src.tab = copy.deepcopy(src.tab)
    lv, nw = src.tab[cfbd.norm("Lakeview State")], src.tab[cfbd.norm("Northwood")]
    lv["expl_o_rank"], lv["expl_d_rank"], nw["expl_o_rank"], nw["expl_d_rank"] = 2, 1, 49, 50
    return src


def test_side_lean_never_against_overall_epa():
    src = _swapped_table()
    b = unit_lane.build("Can Lakeview State hit big plays on Northwood?", {}, src)
    assert all(e["winner"] == "Lakeview State" for e in b["edges"] if e.get("winner")), b["edges"]
    assert b["verdict"]["kind"] == "none" and b["verdict"].get("why") == "epa", b["verdict"]
    assert "EPA" in b["text"] and "Lakeview State +" not in b["text"], b["text"]


def test_big_favorite_on_unit_edges_is_a_slight_lean():
    src = FakeSource(spread=20.5)  # away Northwood laying 20.5
    b = unit_lane.build("Who finishes drives in Lakeview State-Northwood?", {}, src)
    assert b["verdict"]["kind"] == "side" and b["verdict"]["strength"] == "slight lean", b["verdict"]
    assert "slight lean Northwood -20.5 (big number to lay)" in b["text"], b["text"]


def test_pace_clash_verdict_is_about_pace():
    t = build("Hill Tech-Granite: who plays faster? Is that a pace thing?")["text"]
    assert re.search(r"pace alone doesn'?t tilt the total", t), t


def test_no_yes_no_opener_on_open_questions():
    for q in ("How does Northwood's defense match up with Granite's run game?",
              "Northwood's defense vs Lakeview State's offense, who wins that?"):
        t = build(q)["text"]
        assert not _YES.search(t) and not _NO.search(t), t


def test_plays_teams_run_is_pace_not_the_run_game():
    p = unit_lane.parse("The Hill Tech-Bayside total is 61.5. Is that a pace thing? How many plays do these teams run?",
                        {}, SRC)
    assert p["units"] == ["pace"], p["units"]


def test_short_shape_no_stat_dump():
    """Advanced-stats rewrite (Charlie 11-18): the asked pairing's EPA/play + success-rate ranks, the other way in one
    clause, the verdict. No style or SP+ lines (judged 'stat dump', voice 2)."""
    t = build("Can Marsh A&M run on Northwood?")["text"]
    assert "rushing EPA/play" in t and "success rate" in t, t
    assert "SP+" not in t and "Style" not in t, t


def test_asked_pairing_states_both_ranks():
    b = build("Can Lakeview State run on Northwood?")
    e = next(x for x in b["edges"] if x["off"] == "Lakeview State")
    first = re.split(r"(?<=[.!?])\s", b["text"])[0]
    for k in ("ro", "rd", "so", "sd"):
        assert e.get(k) and f"#{e[k]}" in first, (k, e, first)


def test_verdict_side_when_one_team_wins_both_ways():
    b = build("Can Lakeview State run on Northwood?")
    v = b["verdict"]
    assert v["kind"] == "side" and v["team"] == "Northwood", v
    last = re.split(r"(?<=[.!?])\s", b["text"].strip())[-1]
    assert "Northwood" in last and re.search(r"\b(lean|slight lean)\b", last) and "lock" not in last.lower(), last


def test_verdict_names_the_market_number_for_the_side():
    src = FakeSource(spread=-3.0)  # home (Lakeview State is listed after Northwood in SPEC: home) favored by 3
    b = unit_lane.build("Can Lakeview State run on Northwood?", {}, src)
    assert "Northwood +3" in b["text"], b["text"]


def test_verdict_over_when_both_offenses_win():
    b = build("Can Hill Tech throw it on Bayside, and can Bayside throw it on Hill Tech?")
    assert b["verdict"]["kind"] == "over", b["verdict"]
    assert "over" in b["text"].lower() and "45.5" in b["text"], b["text"]


def test_verdict_under_when_both_defenses_win():
    b = build("Can Stonewall run on Ironside?")
    assert all(e["winner"] == e["def"] for e in b["edges"] if e["unit"] in units.PAIRS), b["edges"]
    assert b["verdict"]["kind"] == "under", b["verdict"]
    assert "under" in b["text"].lower(), b["text"]


def test_pace_verdict_reads_as_a_sentence():
    t = build("Hill Tech-Bayside: is this a shootout? Who plays faster?")["text"]
    assert re.search(r"pace alone tilts the over on 45\.5\.", t), t


def test_trenches_verdict_names_the_trenches():
    b = build("Who wins the trenches in Lakeview State-Northwood?")
    assert b["verdict"]["kind"] == "side" and "in the trenches" in b["text"], b["text"]


def test_verdict_none_when_no_edges():
    b = build("Can Granite run on Marsh A&M?")  # 20s vs 20s: no clear edge either way
    assert b["verdict"]["kind"] == "none", b["verdict"]
    assert re.search(r"doesn'?t move the number|coin flip", b["text"]), b["text"]


def test_verdict_follows_from_edges_everywhere():
    """For every shape: a side verdict names a team that won an edge and no edge went the other team's way; over/under
    only when edges agree; none only when nothing was decided."""
    for q, _, _ in SHAPES:
        b = build(q)
        v, es = b["verdict"], [e for e in b["edges"] if e.get("winner")]
        if v["kind"] == "side":
            assert es and all(e["winner"] == v["team"] for e in es), (q, v, es)
        elif v["kind"] == "none":
            assert not es or v.get("why"), (q, es)
        elif v["kind"] in ("over", "under"):
            assert es or v.get("why") == "pace", (q, v)


def test_verdict_only_from_edges_in_the_text():
    """A unit trimmed for length can't drive the verdict (no unsourced 'edge X in the trenches')."""
    old = unit_lane.LIMIT
    unit_lane.LIMIT = 220  # forces the trims real team names cause
    try:
        for spread, total in ((-3.0, 45.5), (-27.5, 71.5)):
            src = FakeSource(spread, total)
            for q, _, _ in SHAPES:
                b = unit_lane.build(q, {}, src)
                for e in b["edges"]:
                    if e.get("off"):
                        assert f"#{e['ro']}" in b["text"] and f"#{e['rd']}" in b["text"], (q, e["unit"], b["text"])
    finally:
        unit_lane.LIMIT = old


def test_answers_fit_350_chars():
    for spread, total in ((-3.0, 45.5), (None, None), (-27.5, 71.5)):
        src = FakeSource(spread, total)
        for q, _, _ in SHAPES:
            b = unit_lane.build(q, {}, src)
            t = b["text"]
            cap = 350 if len(b["units"]) == 1 else 400  # two units asked ('the trenches') get one more clause
            assert len(t) <= cap, (len(t), q, t)  # short enough to read as a text, voice line after


def test_no_raw_floats_or_em_dashes():
    for q, _, _ in SHAPES:
        t = build(q)["text"]
        assert not re.search(r"\b\d\.\d{2,}", t) and "—" not in t, t


def test_phrasing_varies_but_facts_dont():
    texts = {unit_lane.build("Can Lakeview State run on Northwood?", {}, SRC, seed=s)["text"] for s in range(12)}
    assert len(texts) >= 2, texts
    ranks = {tuple(sorted(re.findall(r"#\d+", t))) for t in texts}  # same numbers, any order
    assert len(ranks) == 1, ranks


# ---------------------------------------------------------------------------
# routing + fact lane wiring
# ---------------------------------------------------------------------------

def test_router_sends_unit_asks_to_the_code_lane():
    old = unit_lane.SOURCE
    unit_lane.SOURCE = SRC
    try:
        for q, _, _ in SHAPES:
            assert router.classify(q).label() == "fact:unit_matchup", (q, router.classify(q).label())
        for q in NOT_UNIT:
            assert router.classify(q).kind != "unit_matchup", q
        ans = router.fact_answer("unit_matchup", {"games_this_week": []}, "Can Granite run on Northwood?")
        assert ans and "Granite" in ans and "Northwood" in ans, ans
        # can't parse it (no game) -> None, so picky_brain falls back to the full brain
        assert router.fact_answer("unit_matchup", {"games_this_week": []}, "Can Granite run on somebody?") is None
    finally:
        unit_lane.SOURCE = old


def test_code_path_is_fast():
    t0 = time.time()
    for q, _, _ in SHAPES:
        build(q)
    assert time.time() - t0 < 12, time.time() - t0


# ---------------------------------------------------------------------------
# smoke on the real CFBD cache (skipped when there's none): any upcoming game with unit rows, every shape
# ---------------------------------------------------------------------------

def test_real_cache_smoke():
    import datetime as dt
    import slate_cards
    idx = cfbd.load_index()
    if not idx:
        print("  (no CFBD cache: skipped)")
        return
    tab = units.table()
    games = [g for g in slate_cards.upcoming_games(idx, dt.datetime.now(dt.timezone.utc))
             if units._row(g["awayTeam"], tab) and units._row(g["homeTeam"], tab)
             and len(cfbd.other_teams_named(f"{g['awayTeam']} {g['homeTeam']}", [], idx)) == 2]
    if not games:
        print("  (no upcoming FBS games in the cache: skipped)")
        return
    g = games[0]
    a, h = g["awayTeam"], g["homeTeam"]
    shapes = [f"Can {a} run on {h}?", f"Can {h} stop {a}'s run game?", f"Who wins the trenches in {a}-{h}?",
              f"Can {a} throw it on {h}'s secondary?", f"Who wins the turnover battle in {a}-{h}?",
              f"{a}-{h}: is this a shootout? Who plays faster?", f"Can {h} score on {a}?",
              f"Where does {a} have the edge on {h}?"]
    for q in shapes:
        assert router.classify(q).label() == "fact:unit_matchup", (q, router.classify(q).label())
        b = unit_lane.build(q, {"games_this_week": []})
        t = b and b["text"]
        cap = 350 if b and len(b["units"]) == 1 else 400  # two units asked ('the trenches') get one more clause
        assert t and len(t) <= cap and a in t and h in t, (q, t)


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
