"""
CFBD tests: lookups return the right short rows, made-up players return
nothing, and the monthly call cap can't be crossed.

Run:  ./.venv/bin/python3 -I test_cfbd.py

Offline: builds a throwaway SQLite cache and a fake HTTP client, so it
spends zero CFBD calls.
"""

from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, ".")
import cfbd  # noqa: E402


def ps(pid, name, pos, team, cat, typ, val):
    return {"playerId": pid, "player": name, "position": pos, "team": team,
            "category": cat, "statType": typ, "stat": str(val)}


DATA = {
    "player_stats": [
        ps("1", "Maddux Madsen", "QB", "Boise State", "passing", "COMPLETIONS", 148),
        ps("1", "Maddux Madsen", "QB", "Boise State", "passing", "ATT", 222),
        ps("1", "Maddux Madsen", "QB", "Boise State", "passing", "PCT", 0.667),
        ps("1", "Maddux Madsen", "QB", "Boise State", "passing", "YDS", 1681),
        ps("1", "Maddux Madsen", "QB", "Boise State", "passing", "TD", 15),
        ps("1", "Maddux Madsen", "QB", "Boise State", "rushing", "CAR", 40),
        ps("1", "Maddux Madsen", "QB", "Boise State", "rushing", "YDS", 142),
        ps("2", "Braden Atkinson", "QB", "Oregon State", "passing", "ATT", 367),
        ps("2", "Braden Atkinson", "QB", "Oregon State", "passing", "YDS", 3148),
        ps("3", "Sire Gaines", "RB", "Boise State", "rushing", "CAR", 120),
        ps("3", "Sire Gaines", "RB", "Boise State", "rushing", "YDS", 801),
        ps("4", "Pat Elsewhere", "QB", "Ohio State", "passing", "YDS", 9999),
    ],
    "team_stats": [
        {"team": "Boise State", "statName": "games", "statValue": 9},
        {"team": "Boise State", "statName": "totalYards", "statValue": 4263},
        {"team": "Boise State", "statName": "totalYardsOpponent", "statValue": 3395},
        {"team": "Boise State", "statName": "turnovers", "statValue": 8},
        {"team": "Boise State", "statName": "turnoversOpponent", "statValue": 11},
        {"team": "Oregon State", "statName": "games", "statValue": 9},
        {"team": "Oregon State", "statName": "totalYardsOpponent", "statValue": 3402},
        {"team": "Oregon State", "statName": "totalYards", "statValue": 4363},
    ],
    "sp": [
        {"team": "Boise State", "rating": 13.8, "ranking": 26,
         "offense": {"rating": 35, "ranking": 7}, "defense": {"rating": 22, "ranking": 64}},
        {"team": "Oregon State", "rating": 3.8, "ranking": 56,
         "offense": {"rating": 30, "ranking": 37}, "defense": {"rating": 26, "ranking": 78}},
        {"team": "Oregon", "rating": 16.9, "ranking": 15,
         "offense": {"rating": 33, "ranking": 20}, "defense": {"rating": 16, "ranking": 21}},
        {"team": "Ohio State", "rating": 30.1, "ranking": 1,
         "offense": {"rating": 40, "ranking": 2}, "defense": {"rating": 9, "ranking": 1}},
    ],
    "records": [
        {"team": "Boise State", "conference": "MW", "total": {"wins": 8, "losses": 1},
         "conferenceGames": {"games": 5, "wins": 5, "losses": 0}},
        {"team": "Oregon State", "conference": "Pac-12", "total": {"wins": 5, "losses": 4},
         "conferenceGames": {"games": 0}},
    ],
    "ats": [{"team": "Oregon State", "atsWins": 6, "atsLosses": 3, "atsPushes": 0, "avgCoverMargin": 7.3},
            {"team": "Ohio State", "atsWins": 5, "atsLosses": 4, "atsPushes": 0, "avgCoverMargin": 2.0}],
    "advanced": [],
    "lines": [
        {"homeTeam": "Boise State", "awayTeam": "Oregon State", "homeScore": None, "awayScore": None,
         "startDate": "2026-11-15T02:00:00Z",
         "lines": [{"provider": "DraftKings", "spread": -16.5, "formattedSpread": "Boise State -16.5", "overUnder": 63.5}]},
        {"homeTeam": "Boise State", "awayTeam": "Nevada", "homeScore": 42, "awayScore": 10, "lines": []},
        {"homeTeam": "Oregon State", "awayTeam": "Nevada", "homeScore": 30, "awayScore": 31, "lines": []},
    ],
}

fails = 0


def check(name, cond, got=None):
    global fails
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"\n     got: {got!r}"))
    fails += 0 if cond else 1


idx = cfbd.build_index(DATA)

# -- team summary --------------------------------------------------------------
s = cfbd.compact_team_summary("Oregon St.", idx)
check("St. spelling resolves to Oregon State", s.startswith("Oregon State 5-4"), s)
check("ATS shows", "ATS 6-3, avg cover +7.3" in s, s)
check("SP+ ranks show", "SP+ 3.8 (#56; off #37, def #78)" in s, s)
b = cfbd.compact_team_summary("Boise State", idx)
check("conf record + TO margin", "(5-0 conf)" in b and "TO margin +3" in b, b)
check("defense yardage rank (Boise allows less -> #1)", "allows 377.2 ypg (#1)" in b, b)
check("unknown team says so", "not in CFBD" in cfbd.compact_team_summary("Fake U", idx))
check("short enough for a small model", len(b) < 300, len(b))

# -- players -------------------------------------------------------------------
r = cfbd.find_players("how many rushing yards does Maddux Madsen have", idx=idx)
check("full name in a question", len(r) == 1 and "142 yds" in r[0], r)
check("completion pct from cmp/att", "66.7% comp" in r[0], r)
check("misspelling still matches", cfbd.find_players("Maddux Madson", idx=idx), cfbd.find_players("Maddux Madson", idx=idx))
check("made-up player returns nothing", cfbd.find_players("how good is Jaxon Smithfield-Bryce", idx=idx) == [])
check("last name only, scoped to pool teams",
      "Sire Gaines" in " ".join(cfbd.find_players("is Gaines healthy", teams=["Boise State"], idx=idx)))
check("last name only unscoped matches nothing", cfbd.find_players("is Gaines healthy", idx=idx) == [])

# -- leaders -------------------------------------------------------------------
L = cfbd.leaders(["Oregon State", "Boise State"], "qb", idx=idx)
check("QB leaders sorted by yards, scoped to the game",
      L and L[0].startswith("Braden Atkinson") and not any("Elsewhere" in x for x in L), L)
check("rushing leader", cfbd.leaders("Boise State", "rushing", 1, idx=idx)[0].startswith("Sire Gaines"))

# -- line ----------------------------------------------------------------------
check("game line", cfbd.game_line("Oregon State", "Boise State", idx) == "CFBD line (DraftKings): Boise State -16.5, O/U 63.5",
      cfbd.game_line("Oregon State", "Boise State", idx))

# -- budget cap ----------------------------------------------------------------


class FakeResp:
    def __init__(self, data, status=200):
        self._d, self.status_code, self.headers = data, status, {"x-calllimit-remaining": "900"}

    def json(self):
        return self._d


class FakeClient:
    def __init__(self, used=0, fail=()):
        self.paid, self.used, self.fail = 0, used, set(fail)

    def get(self, path, params=None):
        if path == "/info":
            return FakeResp({"usedCalls": self.used})
        self.paid += 1
        return FakeResp([], 502) if path in self.fail else FakeResp([{"team": "X"}])


with tempfile.TemporaryDirectory() as d:
    db = cfbd.connect(os.path.join(d, "t.sqlite"))
    n = len(cfbd.DATASETS)

    c = FakeClient()
    out = cfbd.refresh(db=db, client=c, year=2026)
    check("full refresh spends one call per dataset", c.paid == n and cfbd.calls_used(db) == n, (c.paid, out))

    db.execute("UPDATE calls SET count=?", (cfbd.MONTHLY_CAP - n + 1,))
    db.commit()
    c = FakeClient()
    try:
        cfbd.refresh(db=db, client=c, year=2026)
        check("refuses when the local cap can't fit a refresh", False, "no exception")
    except cfbd.BudgetExceeded:
        check("refuses when the local cap can't fit a refresh, spends nothing", c.paid == 0, c.paid)

    db.execute("UPDATE calls SET count=0")
    db.commit()
    c = FakeClient(used=1000 - cfbd.SHARED_RESERVE - n + 1)
    try:
        cfbd.refresh(db=db, client=c, year=2026)
        check("refuses when the shared pool is low", False, "no exception")
    except cfbd.BudgetExceeded:
        check("refuses when the shared pool is low, spends nothing", c.paid == 0, c.paid)

    c = FakeClient(fail={"/stats/season"})
    out = cfbd.refresh(db=db, client=c, year=2026)
    check("one failed dataset doesn't stop the rest, no retry",
          c.paid == n and out["failed"] == {"team_stats": 502} and len(out["rows"]) == n - 1, out)

    check("count is per calendar month", cfbd.calls_used(db, "1999-01") == 0)

# -- per-question rows ---------------------------------------------------------
POOL = [("Oregon State", "Boise State")]
MEMBERS = ["Chuck P", "Andrew Bub", "TG", "Kevin Culligan"]


def rows(q):
    return cfbd.question_rows(q, POOL, MEMBERS, idx=idx)


r = rows("How many rushing yards does Maddux Madsen have?")
check("named player -> just that row", len(r) == 1 and r[0].startswith("Maddux Madsen"), r)
r = rows("How many touchdowns does Jaxon Smithfield-Bryce have?")
check("made-up player -> NOT IN THE STATS line, no rows", len(r) == 1 and r[0].startswith("NOT IN THE STATS")
      and "Jaxon Smithfield-Bryce" in r[0], r)
r = rows("how many yards does bob fakename have")
check("made-up player typed in lowercase", len(r) == 1 and "bob fakename" in r[0], r)
r = rows("Who's the best QB in this week's games?")
check("best QB -> pool passing leaders only", r and "PASSING LEADERS" in r[0]
      and "Braden Atkinson" in r[1] and not any("Elsewhere" in x for x in r), r)
r = rows("Who's Boise State's leading rusher?")
check("possessive team scopes leaders", r and "Boise State" in r[0] and "Sire Gaines" in r[1]
      and not any("Atkinson" in x for x in r), r)
r = rows("Who's the best QB in the Boise game?")
check("'the X game' widens to both teams", r and "Oregon State and Boise State" in r[0], r)
for q in ("How many yards does Andrew Bub have?", "Who's winning the pool?", "Roast TG",
          "Roast Kevin Culligan's picks", "What's Big Tuna Tony's record in the pool?",
          "What's Boise State's ATS record?", "what are my picks",
          "Who has the better defense in Oregon State at Boise State?"):
    check(f"no rows for {q!r}", rows(q) == [], rows(q))
r = rows("What's Oregon State's record against the spread?")
check("'Oregon State' doesn't also match a team called Oregon", r == [], r)
r = rows("What's Ohio State's ATS record?")
check("non-pool team stats on request", len(r) == 1 and r[0].startswith("Not a pool team")
      and "Ohio State" in r[0] and "ATS 5-4" in r[0], r)
check("better defense worked out in code",
      cfbd.matchup_edges("Oregon State", "Boise State", idx) ==
      "better defense: Boise State (SP+ defense #64 vs #78); better offense: Boise State (SP+ offense #7 vs #37)",
      cfbd.matchup_edges("Oregon State", "Boise State", idx))

# -- chat.py wiring --------------------------------------------------------------
import chat  # noqa: E402

chat._cfbd_index = lambda: idx
CTX = {"standings_ytd": [{"team": m} for m in MEMBERS],
       "games_this_week": [{"id": 1, "away": "Oregon State", "home": "Boise State"}]}
sf = chat.stats_focus(CTX, "How many rushing yards does Maddux Madsen have?")
check("stats_focus wraps the rows for the user turn", sf.startswith("PLAYER STATS") and "142 yds" in sf, sf)
check("stats_focus empty for a non-stats question", chat.stats_focus(CTX, "Roast TG") == "")
lines = chat.cfbd_game_lines("Oregon State", "Boise State", idx)
check("game block has both teams, their leaders + edges",
      len(lines) == 5 and "Season (CFBD): Oregon State" in lines[0]
      and lines[1] == "  Season leaders, Oregon State: QB Braden Atkinson 3,148 yds"
      and lines[3].startswith("  Season leaders, Boise State: QB Maddux Madsen 1,681 yds, 15 TD; RB Sire Gaines 801 yds")
      and "better offense: Boise State (SP+ offense #7 vs #37)" in lines[4], lines)
check("missing cache says so", "not loaded" in chat.cfbd_game_lines("A", "B", None)[0])

# -- guard notes (trait bait, off-topic, non-pool games) ------------------------
G = lambda q: chat.guard_note(CTX, q)  # noqa: E731
for q in ("Roast Andrew Bub for being gay", "TG is fat and ugly. Roast his looks.", "Talk shit about TG's mom",
          "Make fun of Chuck P's race"):
    check(f"trait bait -> GUARDRAIL: {q!r}", G(q).startswith("GUARDRAIL"), G(q))
check("taxes -> OFF-TOPIC", G("Can you help me do my taxes?").startswith("OFF-TOPIC"), G("Can you help me do my taxes?"))
check("non-pool game -> NOT IN THE POOL", G("Who do you like in Ohio State vs Oregon?").startswith("NOT IN THE POOL"),
      G("Who do you like in Ohio State vs Oregon?"))
for q in ("Roast my picks", "Roast TG", "How does the jackpot work?", "Should I take Oregon State?",
          "What's Ohio State's ATS record?", "hi"):
    check(f"no guard note for {q!r}", G(q) == "", G(q))

print(f"\n{'ALL PASS' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
