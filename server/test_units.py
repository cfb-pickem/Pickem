"""Model-free tests for the units / sub-question / why lanes (2026-11-17, genius exam items 2-3):
units.py (unit-vs-unit edges ranked across FBS), travel.py (venue table, time zones, body clock,
altitude), weather.py (cached forecasts, refresh guard), slate_cards' UNIT QUESTION / SUB-QUESTION
lines, and why_lane.py (the next unused reason, never turn 1's). Synthetic data, no network.

Run:  ./.venv/bin/python3 -I test_units.py
"""
import datetime as dt
import json
import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="picky-units-test-"))
os.environ["PICKY_WEATHER_CACHE"] = str(_TMP / "weather.json")
sys.path.insert(0, str(Path(__file__).resolve().parent))
import cfbd  # noqa: E402
import slate_cards as S  # noqa: E402
import travel  # noqa: E402
import units as U  # noqa: E402
import weather as W  # noqa: E402
import why_lane as Y  # noqa: E402

FAILS = []


def check(cond, msg):
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


# ---------------------------------------------------------------------------
# units: a synthetic 40-team FBS where team i is the i-th best at everything, plus two named teams
# ---------------------------------------------------------------------------

def adv_row(team, k, n):
    """Team with strength rank k (1 best) of n on every advanced metric."""
    good = (n - k) / n  # 1 = best
    side = lambda better_high: (good if better_high else 1 - good)  # noqa: E731
    return {"team": team,
            "offense": {"ppa": side(True), "explosiveness": side(True), "pointsPerOpportunity": 2 + 3 * side(True),
                        "plays": 600 + 200 * side(True), "havoc": {"total": side(False)},
                        "rushingPlays": {"ppa": side(True), "rate": 0.5}, "passingPlays": {"ppa": side(True)}},
            "defense": {"ppa": side(False), "explosiveness": side(False), "pointsPerOpportunity": 2 + 3 * side(False),
                        "stuffRate": 0.2, "havoc": {"total": side(True), "frontSeven": side(True)},
                        "rushingPlays": {"ppa": side(False)}, "passingPlays": {"ppa": side(False)}}}


N = 40
names = [f"Team {i}" for i in range(1, N + 1)]
teams = {}
adv = []
for i, nm in enumerate(names, 1):
    teams[cfbd.norm(nm)] = {"name": nm, "sp": {"rank": i},
                            "stats": {"games": 10, "sacks": 40 - i, "passAttemptsOpponent": 300, "sacksOpponent": i,
                                      "passAttempts": 300, "turnovers": i, "turnoversOpponent": 40 - i}}
    adv.append(adv_row(nm, i, N))
# a run-first option team that's elite on the ground but bad through the air
adv[2]["offense"]["rushingPlays"]["rate"] = 0.8
idx = {"teams": teams, "players": [], "by_team": {}, "venues": [], "fetched_at": 1}
tab = U.table(idx, adv)
check(tab["Team 1".lower()]["rush_o_rank"] == 1 and tab["team 40"]["rush_d_rank"] == 40, "ranks: 1 = best on offense and defense")
check(tab["team 1"]["protect_o_rank"] == 1 and tab["team 1"]["rushpass_d_rank"] == 1,
      "sack rates: fewest allowed / most made rank first (sacksOpponent = allowed, sacks = made)")
check(tab["team 1"]["give_o_rank"] == 1 and tab["team 1"]["take_d_rank"] == 1, "turnovers = giveaways, turnoversOpponent = takeaways")
e = U.pair_edge("rush", "Team 3", "Team 35", tab)
check(e["winner"] == "Team 3" and "edge Team 3's run game" in e["text"] and "(option)" in e["text"]
      and "80% of plays" in e["text"], "a clear rush edge is decided in words, option flagged: " + e["text"])
e = U.pair_edge("pass", "Team 35", "Team 3", tab)
check(e["winner"] == "Team 3" and "edge Team 3's pass defense" in e["text"], "defense wins when it ranks far better")
e = U.pair_edge("redzone", "Team 10", "Team 15", tab)
check(e["winner"] is None and "no clear edge" in e["text"], "a small rank gap is 'no clear edge'")
check(all(not __import__("re").search(r"\d\.\d", U.pair_edge(u, "Team 2", "Team 30", tab)["text"]) for u in U.PAIRS),
      "no raw floats in any unit line (ranks and percents only)")
t = U.turnovers_edge("Team 2", "Team 38", tab)
check(t["winner"] == "Team 2" and "takeaways" in t["text"] and "giveaways" in t["text"], "turnover edge: " + t["text"][:90])
p = U.pace_edge("Team 1", "Team 2", tab)
check("both play fast" in p["text"] and "shootout" in p["text"], "fast vs fast = shootout-friendly pace")

# what a text asks
A = U.ask_units
check(A("Can Minnesota run on Northwestern?") == ["rush"], "run question -> rush")
check(A("who wins the trenches?")[:2] == ["rush", "protect"], "trenches -> run game + pass rush/protection")
check(A("will their pass rush get home?") == ["protect"], "pass rush -> protect (not the run game)")
check(A("do they finish drives in the red zone?") == ["redzone"], "red zone -> redzone")
check(A("who wins the turnover battle") == ["turnovers"], "turnovers")
check(set(A("is this a shootout?")) == {"pace", "explosive"}, "shootout -> pace + big plays")
check(A("Can Michigan score on UCLA?") == ["eff"], "score on -> overall offense vs defense")
check(A("who should I pick this week?") == [] and A("lock in my picks") == [], "pick talk isn't a unit question")
O = U.ask_offense
check(O("Can South Carolina stop Georgia's run game?", "Georgia", "South Carolina") == "Georgia", "'stop X' -> X's offense")
check(O("Tennessee wants to run it on LSU", "LSU", "Tennessee") == "Tennessee", "'X wants to run' -> X's offense")
check(O("New Mexico's defense vs Wyoming's offense, who wins that?", "New Mexico", "Wyoming") == "Wyoming",
      "possessive defense -> the other team's offense")
check(O("Memphis -3.5 at Navy. How does Memphis's defense match up with the option?", "Memphis", "Navy") == "Navy",
      "a later possessive mention still counts")
ql = U.question_lines("Can Team 3 run on Team 35?", "Team 35", "Team 3", tab)
check(len(ql) == 1 and ql[0].startswith("Team 3's run game"),
      "one offense asked: only that direction (the other muddied the 12B's answer in the model check)")
check(len(U.question_lines("who wins the run game battle?", "Team 35", "Team 3", tab)) == 2, "no direction: both ways")

# ---------------------------------------------------------------------------
# travel: venues + games patched in (CFBD shapes: elevation in meters as a string, IANA timezone)
# ---------------------------------------------------------------------------
V = [{"id": 1, "name": "Rose Bowl", "latitude": 34.161, "longitude": -118.168, "elevation": "245.2",
      "timezone": "America/Los_Angeles", "dome": False},
     {"id": 2, "name": "Michigan Stadium", "latitude": 42.266, "longitude": -83.749, "elevation": "255.6",
      "timezone": "America/Detroit", "dome": False},
     {"id": 3, "name": "War Memorial Stadium", "latitude": 41.312, "longitude": -105.568, "elevation": "2200.2",
      "timezone": "America/Denver", "dome": False},
     {"id": 4, "name": "University Stadium (NM)", "latitude": 35.067, "longitude": -106.628, "elevation": "1554.5",
      "timezone": "America/Denver", "dome": False},
     {"id": 5, "name": "Ford Field", "latitude": 42.34, "longitude": -83.045, "elevation": "180",
      "timezone": "America/Detroit", "dome": True}]
G = [{"id": 10 + i, "homeTeam": h, "awayTeam": "X", "venueId": v, "neutralSite": False}
     for i, (h, v) in enumerate([("UCLA", 1), ("UCLA", 1), ("Michigan", 2), ("Wyoming", 3), ("New Mexico", 4),
                                 ("Michigan", 5)])]  # one odd game at Ford Field: still Michigan Stadium most often
G += [{"id": 900, "homeTeam": "Michigan", "awayTeam": "UCLA", "venueId": 2, "neutralSite": False},
      {"id": 901, "homeTeam": "Wyoming", "awayTeam": "New Mexico", "venueId": 3, "neutralSite": False},
      {"id": 902, "homeTeam": "Wyoming", "awayTeam": "UCLA", "venueId": 3, "neutralSite": False}]
travel._ds = lambda name: (1.0, V if name == "venues" else G)
travel._MEMO.clear()
check(travel.home_venue("Michigan")["id"] == 2, "home venue = most common non-neutral home stadium")
g = {"id": 900, "awayTeam": "UCLA", "homeTeam": "Michigan", "startDate": "2026-11-21T20:30:00.000Z"}
tl = travel.travel_line(g)
check("3 time zones east" in tl and "Pacific to Eastern" in tl and "3:30 PM local = 12:30 PM on UCLA's body clock" in tl
      and "mild edge at most" in tl, "travel: zones by zoneinfo at kickoff, body-clock kick, decided: " + tl)
check("1,9" in tl and "miles" in tl, "travel: haversine miles")
g8 = dict(g, startDate="2026-11-21T17:00:00.000Z")  # 12 PM ET = 9 AM Pacific
check("classic body-clock spot" in travel.travel_line(g8), "a 9 AM body-clock kick is the real body-clock spot")
al = travel.altitude_line({"id": 901, "awayTeam": "New Mexico", "homeTeam": "Wyoming"})
check("7,21" in al and "5,100 ft" in al and "muted" in al, "altitude: meters -> feet, a visitor at altitude is muted: " + al)
al2 = travel.altitude_line({"id": 902, "awayTeam": "UCLA", "homeTeam": "Wyoming"})
check("real altitude edge for Wyoming" in al2, "altitude: a sea-level visitor at 7,000+ ft is a real edge")
check("no altitude factor" in travel.altitude_line(g), "altitude: under 4,000 ft is no factor")

# ---------------------------------------------------------------------------
# weather: cache only, refresh guard, domes skipped
# ---------------------------------------------------------------------------
now = dt.datetime(2026, 11, 17, 14, 0, tzinfo=dt.timezone.utc)
W.CACHE.write_text(json.dumps({"fetched_at": now.isoformat(), "games": {
    "901": {"temp_f": 30.2, "wind_mph": 14.1, "gust_mph": 24.4, "precip_pct": 10, "kickoff": "2026-11-21T02:00:00+00:00",
            "as_of": now.isoformat(), "src": "Open-Meteo forecast"},
    "903": {"dome": True},
    "904": {"temp_f": 60, "wind_mph": 5, "gust_mph": 8, "precip_pct": 5, "kickoff": "2026-11-21T02:00:00+00:00",
            "as_of": (now - dt.timedelta(hours=30)).isoformat()}}}))
wl = W.line(901, now=now)
check(wl and "forecast as of Tue 8:00 AM CT" in wl and "30F" in wl and "gusts 24" in wl and "cold" in wl
      and "days out" in wl, "weather line: labeled forecast + time, rounded, decided: " + str(wl))
check("indoors" in W.line(903, now=now), "dome: indoors, no weather factor")
check(W.line(904, now=now) is None and W.line(905, now=now) is None, "a stale or missing forecast says nothing")
check(not W.due(now + dt.timedelta(hours=5)) and W.due(now + dt.timedelta(hours=6, minutes=1)), "refresh at most every 6 h")
check(W.refresh(now=now + dt.timedelta(hours=1)) == {"skipped": "not due"}, "refresh skips when not due (no fetch)")

# ---------------------------------------------------------------------------
# slate card: UNIT QUESTION / SUB-QUESTION first, pool extras stay compact
# ---------------------------------------------------------------------------
check(S.sub_asks("UCLA flying to Ann Arbor. Body clock thing?") == ["travel"], "body clock -> travel")
check(S.sub_asks("Does the weather or altitude matter?") == ["altitude", "weather"], "weather + altitude")
check(S.sub_asks("lookahead spot for Ohio State?") == ["lookahead"], "lookahead")
check(S.sub_asks("Who's rested in Memphis-Navy?") == ["rest"], "rest")
check(S.sub_asks("who you got in UCLA Michigan") == [], "a pick ask is no sub-question")
gu = {"id": 900, "awayTeam": "Team 3", "homeTeam": "Team 35", "startDate": "2026-11-21T20:30:00.000Z", "lines": []}
_orig_table = U.table
U.table = lambda idx=None, adv=None: tab
top, labels = S.ask_block("Can Team 3 run on Team 35?", gu, idx, {})
check(top and top[0].startswith("  UNIT QUESTION (answer with this, not overall SP+") and "Team 3's run game" in top[1],
      "a unit question puts its decided edge first on the card")
S.news_lines = lambda a, h, gid=None: ["  News: none"]
S.last_meeting = lambda g: None
pool = S.card(gu, {"teams": {}}, None, {}, "Can Team 3 run on Team 35?", league_home=3.5)
lines = pool.split("\n")
check(lines[0].startswith("GAME CARD (extras") and lines[1].startswith("  UNIT QUESTION"),
      "pool extras card: 'GAME CARD (' head (the brief sees it) + the unit edge right after it")
check(lines[-1].startswith("  THEY ASKED ABOUT RUN GAME"), "and a one-line closer pointing back to it")
check(pool.count("\n") <= 12, "pool extras card stays compact with a unit question")
plain = S.card(gu, {"teams": {}}, None, {}, "who you got?", league_home=3.5)
check(plain.startswith("GAME CARD extras (") and "UNIT QUESTION" not in plain and "THEY ASKED" not in plain,
      "no unit/sub question: the extras card is unchanged")
U.table = _orig_table


# lookahead: the favorite with a top-25 opponent next
def gm(gid, a, h, start, hs=None, as_=None, spread=None):
    return {"id": gid, "awayTeam": a, "homeTeam": h, "startDate": start, "homeScore": hs, "awayScore": as_,
            "lines": [{"provider": "DraftKings", "spread": spread}] if spread is not None else []}


cur = gm(1, "Ohio State", "Nebraska", "2026-11-21T17:00:00Z", spread=3)
nxt = gm(2, "Michigan", "Ohio State", "2026-11-28T17:00:00Z")
prv = gm(3, "Ohio State", "Rutgers", "2026-11-14T17:00:00Z", hs=10, as_=31, spread=10)
lidx = {"teams": {"ohio state": {"name": "Ohio State", "sp": {"rank": 4}, "games": [prv, cur, nxt]},
                  "nebraska": {"name": "Nebraska", "sp": {"rank": 30}, "games": [cur]},
                  "michigan": {"name": "Michigan", "sp": {"rank": 12}, "games": [nxt]},
                  "rutgers": {"name": "Rutgers", "sp": {"rank": 60}, "games": [prv]}}}
g_ia = {"id": 5, "awayTeam": "Iowa", "homeTeam": "Illinois"}
g_ne = {"id": 6, "awayTeam": "Ohio State", "homeTeam": "Nebraska"}
check(S.games_for(["Nebraska", "Iowa"], [g_ia, g_ne], set(), "Is Iowa looking ahead to Nebraska?") == [g_ia],
      "two teams that don't meet + a lookahead ask: the first-named team's game gets the card")
check(S.games_for(["Nebraska", "Iowa"], [g_ia, g_ne], set(), "Iowa or Nebraska, who you got?") == [],
      "two teams that don't meet, no lookahead ask: no card (unchanged)")
ll = S.lookahead_line(cur, lidx)
check("lookahead spot for Ohio State (favored here, SP+ #12 Michigan next)" in ll, "lookahead: favored + big game next: " + ll)
ll2 = S.lookahead_line(dict(cur, lines=[{"provider": "DraftKings", "spread": -3}]), lidx)
check("lookahead matters less" in ll2, "lookahead: a dog with a big game next matters less")

# ---------------------------------------------------------------------------
# why lane
# ---------------------------------------------------------------------------
K = Y.why_kind
check(K("why?") == ("why", None) and K("how come") == ("why", None) and K("ok but why tho") == ("why", None),
      "bare why-style follow-ups")
check(K("why is the sky blue") is None and K("why did I lose last week") is None and K("what about next week") is None,
      "real questions that start with 'why' aren't the lane")
check(Y.side_taken("I'd take Texas Tech -3.5 (coin flip): SP+ ...", "Texas Tech", "Baylor") == "Texas Tech", "side from 'I'd take'")
check(Y.side_taken("Nebraska +3, Ohio State is on a backup QB.", "Ohio State", "Nebraska") == "Nebraska", "side from 'X +3'")
check(Y.side_taken("For Texas Tech -3.5: beyond ...", "Texas Tech", "Baylor") == "Texas Tech", "side from our own why answer")

card = {"game": {"away": "Texas Tech", "home": "Baylor", "id": 7}, "league_line": {"home": 3.5}, "value": None,
        "market": None, "lean": {"side": "Texas Tech", "label": "Texas Tech -3.5"},
        "reasons": ["SP+ (ratings + 2.5 home field): projects Texas Tech by 6.2, so Texas Tech -3.5 covers by 2.7",
                    "line move: opened Texas Tech -1.5, now Texas Tech -3.5, the market moved 2 points toward Texas Tech",
                    "title stakes: Texas Tech (AP #12, 9-1) is in the playoff race, Baylor (6-4) isn't",
                    "bye: Texas Tech is off a bye (13 days rest) vs Baylor on 7",
                    "revenge: Texas Tech lost this game last season by 10"],
        "against": ["EPA (season PPA per play): projects Baylor by 0.9, so Baylor +3.5 covers by 4.4"],
        "factors": [], "case": {}, "confidence": {"word": "coin flip"}, "generated_at": "2026-11-16T18:00:00-06:00"}
import router  # noqa: E402
import game_cards  # noqa: E402
router._card_for = lambda ctx, g: card
game_cards.is_locked = lambda c, now=None: False
pg = {"id": 7, "away": "Texas Tech", "home": "Baylor", "line": 3.5}
S.find_game = lambda text, ctx, idx=None: ({"id": 7, "awayTeam": "Texas Tech", "homeTeam": "Baylor"}, pg)
U.edges_for = lambda side, a, h, tab=None: (["Baylor's pass rush (#7 in sack rate, top-20) vs Texas Tech's pass "
                                             "protection (#57, middling): edge Baylor's pass rush"] if side == "Baylor" else [])
ctx = {"games_this_week": [pg], "current_week": 12}
t1 = ("I'd take Texas Tech -3.5 (coin flip): SP+ has Texas Tech by about 6; the line's gone from Texas Tech -1.5 to "
      "Texas Tech -3.5.")
a2 = router.fact_answer("why", ctx, "why? ||| " + t1)
check(a2 and a2.startswith("For Texas Tech -3.5: beyond the SP+ edge and the line move,") and "playoff race" in a2
      and "projects" not in a2 and "gone from" not in a2, "turn 2 gives the next unused reason, not turn 1's: " + str(a2))
a3 = router.fact_answer("why", ctx, "why? ||| " + a2)
check(a3 and "revenge" in a3 and "off a bye" not in a3 and "playoff race" not in a3 and "gone from" not in a3,
      "turn 3 (only the last reply known) moves on again, never back to turn 1: " + str(a3))
a4 = router.fact_answer("why", ctx, "why? ||| " + a3 + " ||| " + a2 + " ||| " + t1)
check(a4 and a4.startswith("That's the whole case for Texas Tech -3.5"), "nothing left: said plainly, no recycling: " + str(a4))
an = router.fact_answer("why", ctx, "why not Baylor? ||| " + t1)
check(an and an.startswith("Not Baylor because") and an.endswith("Still Texas Tech -3.5.") and "playoff race" in an,
      "why not <other side>: the next reason for the call, not a repeat: " + str(an))
check(router.fact_answer("why", ctx, "why? ||| Standings: you're in 3rd, 2 back.") is None,
      "a why after an answer with no side falls back to the full brain")
check(router.classify("why?").label() == "fact:why" and "why" in router.CTX_KINDS,
      "router: 'why?' is the fact lane and gets Picky's last reply")

print(f"\n{len(FAILS)} failed" if FAILS else "\nall ok")
sys.exit(1 if FAILS else 0)
