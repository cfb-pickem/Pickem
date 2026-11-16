"""
Deterministic tests for pick_brief.py (the ATS pick brief): weights, the
value/key-number math against the LEAGUE line, QB status from intel, news
attribution, schedule spots, game theory, the compact vs full prompt blocks,
and that docs/picky-pick-factors.md's status column matches STATUS.
No network, no GPU.

Run:  ./.venv/bin/python3 -I test_pick_brief.py
"""

from __future__ import annotations

import datetime as dt
import os
import re
import sys
from pathlib import Path

os.environ["PICKY_CFBD_DB"] = "/nonexistent/cfbd.sqlite"  # raw() datasets come back None: synthetic data only
sys.path.insert(0, ".")
import cfbd  # noqa: E402
import pick_brief as pb  # noqa: E402

FAILED = []


def check(name, ok, got=None):
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else f"\n     got: {got!r}"))
    if not ok:
        FAILED.append(name)


def team(name, sp=None, record=None, games=None, players=None):
    return {"name": name, "sp": {"rating": sp, "rank": 50} if sp is not None else None, "record": record,
            "games": games or [], "stats": {}, "adv": {}}


def idx_of(*teams, by_team=None):
    return {"teams": {cfbd.norm(t["name"]): t for t in teams}, "players": [], "by_team": by_team or {}, "venues": []}


G = {"id": 1, "away": "Texas A&M", "home": "Oklahoma", "line": -7.5, "start": "2026-11-21T11:00:00",
     "season": 2026, "week": 12, "fpi_margin": None}
MON = dt.datetime(2026, 11, 16, 9, 0)
WED = dt.datetime(2026, 11, 18, 9, 0)


def facs(b, n):
    return [f for f in b["factors"] if f["n"] == n]


# -- labels and locks -----------------------------------------------------------------
check("side label favorite", pb.side_label("Oklahoma", G) == "Oklahoma -7.5")
check("side label dog", pb.side_label("Texas A&M", G) == "Texas A&M +7.5")
check("lock is Tuesday noon before a Saturday game",
      pb.line_lock(dt.datetime(2026, 11, 21, 11)) == dt.datetime(2026, 11, 17, 12))
check("Thursday game still locks that Tuesday",
      pb.line_lock(dt.datetime(2026, 11, 19, 19)) == dt.datetime(2026, 11, 17, 12))

# -- line value against the league line ------------------------------------------------
idx = idx_of(team("Texas A&M"), team("Oklahoma"))
notes = {"market": {"home_now": -10.5, "home_open": -7.5, "over_under": 44.5, "book": "DraftKings"}}
b = pb.build(G, notes, idx, now_ct=WED)
v = facs(b, 1)[0]
check("market likes OU 3 more than the league: value on Oklahoma -7.5, heavy weight",
      v["side"] == "Oklahoma" and v["w"] == pb.W_VALUE_BIG and "Oklahoma -7.5" in v["why"], v)
k = facs(b, 4)
check("gap 7.5 -> 10.5 crosses 10: key number to Oklahoma", k and k[0]["side"] == "Oklahoma"
      and "10" in k[0]["why"], k)
check("locked by Wednesday", b["locked"] and facs(b, 6), b["locked"])
check("value leads the brief", b["top"][0]["n"] == 1 and b["pick"] == "Oklahoma", b["top"][:1])
check("strong when value is alone", b["strength"] == "strong", b["strength"])
small = pb.build(G, {"market": {"home_now": -8.5}}, idx, now_ct=WED)
check("1-point gap: light value weight", facs(small, 1)[0]["w"] == pb.W_VALUE, facs(small, 1))
same = pb.build(G, {"market": {"home_now": -7.5, "home_open": -2.5}}, idx, now_ct=MON)
check("no gap: no value lean", facs(same, 1)[0]["side"] is None)
mv = facs(same, 2)[0]
check("5-point move toward Oklahoma, flagged", mv["side"] == "Oklahoma" and mv["w"] == pb.W_MED and mv["flag"], mv)
check("Monday: not locked, says when it locks", not same["locked"] and "locks Tue noon" in pb.brief_note(same))

# -- QB status from a dossier fact -------------------------------------------------------
by_team = {"texas a&m": [{"name": "Marcel Reed", "stats": {"passing": {"ATT": 287}}},
                         {"name": "Miles O'Neill", "stats": {"passing": {"ATT": 31}}}]}
idx2 = idx_of(team("Texas A&M"), team("Oklahoma"), by_team=by_team)
intel = [{"team": "Texas A&M", "kind": "injury", "status": "out", "date": "2026-11-15",
          "text": "Texas A&M QB Marcel Reed (ankle) was ruled out for Saturday.", "source": "rotowire.com"}]
b = pb.build(G, {"market": {"home_now": -7.5}}, idx2, intel=intel, now_ct=MON)
q = [f for f in facs(b, 8) if f["side"]]
check("starter out: 4 to the other side, flagged", q and q[0]["side"] == "Oklahoma" and q[0]["w"] == pb.W_QB_OUT
      and q[0]["flag"], q)
bk = facs(b, 9)
check("backup QB named with his attempts", bk and "Miles O'Neill" in bk[0]["why"] and "31" in bk[0]["why"], bk)
avail = [{"team": "Texas A&M", "kind": "injury", "status": "probable", "date": "2026-11-15",
          "text": "QB Marcel Reed will play Saturday, Elko said."}]
b2 = pb.build(G, {"market": {"home_now": -7.5}}, idx2, intel=avail, now_ct=MON)
check("probable QB: no lean", not [f for f in facs(b2, 8) if f["side"]], facs(b2, 8))

# -- news attribution: a story tagging two teams counts only for the injured player's team --
news = [{"teams": ["Tennessee", "Texas A&M"], "players": ["Mike Matthews"], "date": "2026-11-15",
         "title": "Tennessee WR Mike Matthews out for season", "summary": "He was hurt against Texas A&M."}]
b3 = pb.build(G, {"market": {"home_now": -7.5}}, idx2, intel=news, now_ct=MON)
check("another team's injury doesn't count against A&M", not facs(b3, 10), facs(b3, 10))
check("injury_status reads 'ruled out'", pb.injury_status("He was ruled out Saturday") == "out")
check("injury_status: 'will play' wins", pb.injury_status("Questionable earlier, he will play") == "available")
check("news roundups with many tags are skipped",
      pb.news_intel({"items": {"a": {"teams": ["A", "B", "C", "D"], "title": "bowl projections"}}}, ["A"]) == [])

# -- schedule spots ------------------------------------------------------------------------
def cg(week, home, away, start, hs=None, as_=None, spread=None):
    return {"id": week, "week": week, "homeTeam": home, "awayTeam": away, "startDate": start,
            "homeScore": hs, "awayScore": as_, "seasonType": "regular",
            "lines": [{"provider": "DraftKings", "spread": spread}] if spread is not None else []}


big = team("Big U", sp=30.0)
big["sp"]["rank"] = 5
ou_games = [cg(10, "Oklahoma", "Ole Miss", "2026-11-07T18:00:00Z", 24, 17, 3.0),   # won as a 3-pt dog
            cg(12, "Oklahoma", "Texas A&M", "2026-11-21T17:00:00Z"),
            cg(13, "Big U", "Oklahoma", "2026-11-28T17:00:00Z")]
tam_games = [cg(11, "Texas A&M", "Somebody", "2026-11-14T17:00:00Z", 30, 10, -3.0),
             cg(12, "Oklahoma", "Texas A&M", "2026-11-21T17:00:00Z")]
idx3 = idx_of(team("Texas A&M", sp=1.0, games=tam_games), team("Oklahoma", sp=8.0, games=ou_games), big)
b4 = pb.build(G, {"market": {"home_now": -7.5}}, idx3, now_ct=MON)
la = facs(b4, 20)
check("lookahead: favored Oklahoma plays SP+ #5 next", la and la[0]["side"] == "Texas A&M" and "#5" in la[0]["why"], la)
ld = facs(b4, 21)
check("letdown: Oklahoma won outright as a dog last time", ld and ld[0]["side"] == "Texas A&M"
      and "24-17" in ld[0]["why"], ld)
by = facs(b4, 18)
check("bye: Oklahoma 14 days rest vs A&M 7", by and by[0]["side"] == "Oklahoma", by)

# -- ratings use the league line -----------------------------------------------------------
r = pb._by_margin(G, 49, 3.1, "SP+", pb.W_SP)
check("SP+ OU by 3.1 vs -7.5: A&M covers by 4.4", r["side"] == "Texas A&M" and "covers by 4.4" in r["why"], r)

# -- game theory (58-60) ----------------------------------------------------------------------
stand = [{"team": "Chuck P", "w": 30, "l": 18, "rank": 1}, {"team": "Ackbar", "w": 28, "l": 20, "rank": 2},
         {"team": "Clam", "w": 27, "l": 21, "rank": 3}, {"team": "Rettino", "w": 26, "l": 22, "rank": 4},
         {"team": "TG", "w": 20, "l": 28, "rank": 14}]
tend = {"Chuck P": {"n": 40, "fav": 0.5, "home": 0.5, "crowd": 0.5},
        "Ackbar": {"n": 40, "fav": 0.75, "home": 0.5, "crowd": 0.6},
        "Clam": {"n": 40, "fav": 0.7, "home": 0.5, "crowd": 0.6},
        "Rettino": {"n": 40, "fav": 0.65, "home": 0.5, "crowd": 0.6},
        "TG": {"n": 40, "fav": 0.3, "home": 0.4, "crowd": 0.4}}
lead = pb.game_theory(G, "Chuck P", stand, tend)
n60 = [f for f in lead if f["n"] == 60][0]
check("leader protects: match rivals' favorite habit (Oklahoma)", n60["side"] == "Oklahoma" and "protect" in n60["why"],
      n60)
chase = pb.game_theory(G, "TG", stand, tend)
n60c = [f for f in chase if f["n"] == 60][0]
check("chaser below the cut: no clear crowd near him -> no forced side, or fade", "chasing" in n60c["why"], n60c)
mid = pb.game_theory(G, "Rettino", stand, tend)
n60m = [f for f in mid if f["n"] == 60][0]
check("Rettino 4 back of the lead: chasing, fade the favorite crowd", n60m["side"] == "Texas A&M", n60m)
n58 = [f for f in lead if f["n"] == 58][0]
check("rivals' tendencies say past picks, never this week's", "past graded picks, not this week's" in n58["why"], n58)
check("predict needs 8+ graded picks", pb.predict({"n": 5, "fav": 0.9, "home": 0.5}, G) is None)
hist = [{"team": "A", "pick": "X", "fav": "X", "home": "Y", "crowd": "X"},
        {"team": "A", "pick": "Y", "fav": "X", "home": "Y", "crowd": "X"}]
t = pb.tendencies(hist)
check("tendencies fractions", t["A"] == {"n": 2, "fav": 0.5, "home": 0.5, "crowd": 0.5}, t)

# -- score / brief shape ----------------------------------------------------------------------
fs = [pb.F(49, "Texas A&M", 2, "a"), pb.F(2, "Oklahoma", 1, "b"), pb.F(37, "Texas A&M", 1, "c")]
s = pb.score(G, fs)
check("lean and score", s["lean"] == "Texas A&M" and s["score"] == {"Texas A&M": 3.0, "Oklahoma": 1.0}, s["score"])
check("3 vs 1 is a lean, not strong", s["strength"] == "lean")
tie = pb.score(G, [pb.F(49, "Texas A&M", 1, "a"), pb.F(2, "Oklahoma", 1, "b")])
check("dead even: take the points", tie["pick"] == "Texas A&M" and "take the points" in tie["pick_why"], tie["pick"])
note = pb.brief_note(s)
check("brief names the ATS framing and the league line",
      "who COVERS the league line, not who wins" in note and "Oklahoma -7.5 / Texas A&M +7.5" in note, note)
check("brief recommends with the line", "RECOMMENDATION: Texas A&M +7.5" in note, note)
check("brief is short", len(note.splitlines()) <= 12, len(note.splitlines()))
full = pb.full_note(s)
check("full table lists never-available factors", "Never available (no source): depth chart, rivalry, CFP seeding" in full,
      full[-200:])
check("wants_full", pb.wants_full("why?") and pb.wants_full("give me everything") and not pb.wants_full("who covers?"))
check("weights documented as in the module docstring", pb.W_VALUE_BIG == 6 and pb.W_QB_OUT == 4 and pb.W_SP == 2)

# -- the doc's status column matches STATUS ------------------------------------------------------
doc = (Path(__file__).resolve().parent.parent / "docs" / "picky-pick-factors.md").read_text()
rows = dict(re.findall(r"^(\d+)\. .*?\*\*(live|partial|no source)\*\*", doc, re.M))
want = {str(n): ("no source" if st == "none" else st) for n, (st, _) in pb.STATUS.items()}
check("doc has a status for all 60 factors", len(rows) == 60, len(rows))
check("doc status column == pick_brief.STATUS", rows == want,
      {k: (rows.get(k), want[k]) for k in want if rows.get(k) != want[k]})

print(f"\n{len(FAILED)} failed" if FAILED else "\nall passed")
sys.exit(1 if FAILED else 0)
