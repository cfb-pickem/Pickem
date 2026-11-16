"""Model-free tests for slate_cards (game cards for any FBS game this week).
Uses a tiny synthetic CFBD index, never the network (ESPN is stubbed)."""
import asyncio
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import slate_cards as S  # noqa: E402

FAILS = []


def check(cond, msg):
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def game(gid, away, home, start, hs=None, as_=None, spread=None, open_=None, ou=None):
    return {"id": gid, "awayTeam": away, "homeTeam": home, "startDate": start, "homeScore": hs, "awayScore": as_,
            "seasonType": "regular", "week": 12 if hs is None else 11,
            "lines": [{"provider": "DraftKings", "spread": spread, "spreadOpen": open_, "overUnder": ou}]}


now = dt.datetime.now(dt.timezone.utc)
fut = (now + dt.timedelta(days=4)).isoformat().replace("+00:00", "Z")
past = (now - dt.timedelta(days=3)).isoformat().replace("+00:00", "Z")
past2 = (now - dt.timedelta(days=10)).isoformat().replace("+00:00", "Z")
g_up = game(1, "Ohio State", "Nebraska", fut, spread=3, open_=5.5, ou=41.5)
g_p1 = game(2, "Northwestern", "Ohio State", past, hs=28, as_=19, spread=-14.5)   # OSU won by 9, missed by 5.5
g_p2 = game(3, "Nebraska", "Rutgers", past, hs=0, as_=31, spread=15.5)            # NEB won by 31 as -15.5
g_p3 = game(4, "Oregon", "Ohio State", past2, hs=28, as_=41, spread=-7.5)
teams = {
    "ohio state": {"name": "Ohio State", "record": "7-3", "games": [g_up, g_p1, g_p3],
                   "sp": {"rank": 4, "off_rank": 10, "def_rank": 8}},
    "nebraska": {"name": "Nebraska", "record": "6-4", "games": [g_up, g_p2], "sp": {"rank": 18, "off_rank": 40, "def_rank": 9}},
    "northwestern": {"name": "Northwestern", "games": [g_p1]}, "rutgers": {"name": "Rutgers", "games": [g_p2]},
    "oregon": {"name": "Oregon", "games": [g_p3]},
}
idx = {"teams": teams, "players": [], "by_team": {}, "venues": []}

check([g["id"] for g in S.upcoming_games(idx, now)] == [1], "only the unplayed game in the next 9 days is upcoming")
rows = S.team_rows("Ohio State", idx, fut)
check([r["cover"] for r in rows] == [-20.5, -5.5], "cover margins vs the spread from each team's side")
line = S.ats_line("Ohio State", rows, teams["ohio state"])
check("ATS 0-2" in line and "fav 0-2" in line and "missed by 5.5" in line, "ATS record, fav split, last-3 margins")
m = S.market_line(g_up, {"home_now": 3.0, "home_open": 5.5, "over_under": 41.5, "book": "DraftKings"})
check(m[0].endswith("Ohio State -3, opened Ohio State -5.5 (moved 2.5 toward Nebraska)"), "move size + direction: " + m[0])
check("Ohio State 22" in m[1] and "Nebraska 19" in m[1], "implied team totals: " + m[1])
check(any("sits ON 3" in x for x in m), "key number 3 flagged")
m2 = S.market_line(g_up, {"home_now": 3.5, "home_open": 3.5, "over_under": 50, "book": "DK"})
check(any("dog gets the hook" in x or "underdog gets the hook" in x for x in m2), "3.5 = the dog gets the hook")

S.cfbd.load_index = lambda: idx
S.cfbd.other_teams_named = lambda q, pool, i=None: [t["name"] for k, t in teams.items() if k in q.lower()]


async def fake_notes(ids):
    return {}
import espn  # noqa: E402
espn.game_notes = fake_notes
S._games_dataset = lambda: {}
S.last_meeting = lambda g: None
S.news_lines = lambda a, h, gid=None: ["  News: none"]
S.run_game_line = lambda a, h: None
out = asyncio.run(S.slate_note("Ohio State or Nebraska, who you got?", {}))
check(out.startswith("GAME CARD") and "Ohio State at Nebraska" in out, "a pick question about a non-pool game gets a card")
check(asyncio.run(S.slate_note("best cereal?", {})) == "", "no card for off-topic texts")
check(asyncio.run(S.slate_note("why?", {}, prior=["Ohio State or Nebraska, who you got?"])).startswith("GAME CARD"),
      "a 'why?' follow-up keeps the card of the game just discussed")
check(asyncio.run(S.slate_note("Ohio State or Nebraska?", {"games_this_week": [{"id": 1}]})) == "",
      "pool games are skipped (the pool's own riders cover them)")
print(f"\n{len(FAILS)} failed" if FAILS else "\nall ok")
sys.exit(1 if FAILS else 0)
