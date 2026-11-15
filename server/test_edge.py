"""
Deterministic tests for edge.py and the weighted lean in chat.matchup_signals:
line movement, PPA efficiency, unit matchups, public splits parsing and
thresholds, weather, and the documented weights. No network, no GPU.

Run:  ./.venv/bin/python3 -I test_edge.py
"""

from __future__ import annotations

import sys

sys.path.insert(0, ".")
import chat  # noqa: E402
import edge  # noqa: E402

FAILED = []


def check(name, ok, got=None):
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else f"\n     got: {got!r}"))
    if not ok:
        FAILED.append(name)


def team(name, off, dfn, sp=None):
    """off/dfn: dicts of adv stats incl. *_rank."""
    return {"name": name, "sp": {"rating": sp, "rank": 1, "off_rank": 1, "def_rank": 1} if sp is not None else None,
            "adv": {"offense": off, "defense": dfn}}


def idx_of(*teams):
    return {"teams": {chat.cfbd.norm(t["name"]): t for t in teams}, "players": [], "by_team": {}, "venues": []}


# -- line movement ---------------------------------------------------------------
notes = {"market": {"details": "MIA -19.5", "home_open": -16.5, "home_now": -19.5, "book": "DraftKings"},
         "home": {"abbr": "MIA"}, "away": {"abbr": "VT"}}
mv = edge.line_move(notes, "Virginia Tech", "Miami", None)
check("ESPN open/now read", mv == {"open": -16.5, "now": -19.5, "src": "DraftKings"}, mv)
s = edge.move_signal("Virginia Tech", "Miami", mv)
check("3-point move toward the home favorite", s["side"] == "Miami" and s["w"] == edge.W_MOVE
      and "opened Miami -16.5, now Miami -19.5" in s["why"], s)
s = edge.move_signal("Virginia Tech", "Miami", {"open": -3.0, "now": -2.0, "src": "x"})
check("1-point move is info only", s["side"] is None and s["w"] == 0 and "toward Virginia Tech" in s["why"], s)
s = edge.move_signal("A", "B", {"open": 2.5, "now": 2.5, "src": "x"})
check("no move", s["side"] is None and "no move" in s["why"] and "A -2.5" in s["why"], s)
cf_game = {"awayTeam": "Akron", "homeTeam": "Massachusetts", "homeScore": None, "startDate": "2026-11-21",
           "lines": [{"provider": "DraftKings", "spread": -2.5, "spreadOpen": -3.5}]}
cidx = idx_of(dict(team("Akron", {}, {}), games=[cf_game]), dict(team("Massachusetts", {}, {}), games=[cf_game]))
mv = edge.line_move({}, "Akron", "Massachusetts", cidx)
check("CFBD open/now fallback", mv == {"open": -3.5, "now": -2.5, "src": "DraftKings"}, mv)

# -- efficiency + unit matchups -------------------------------------------------
H = team("Home U", {"ppa": 0.35, "success": .5, "success_rank": 5, "explosive_rank": 10, "havoc_rank": 20},
         {"ppa": 0.10, "success_rank": 10, "explosive_rank": 15, "havoc_rank": 12}, sp=20.0)
A = team("Away St", {"ppa": 0.15, "success_rank": 80, "explosive_rank": 90, "havoc_rank": 100},
         {"ppa": 0.25, "success_rank": 95, "explosive_rank": 70, "havoc_rank": 110}, sp=5.0)
em = edge.efficiency_margin(A, H)
# home ppa (0.35+0.25)/2=0.30, away (0.15+0.10)/2=0.125 -> 0.175*68 = 11.9 + 2.5
check("PPA efficiency margin", em == 14.4, em)
check("efficiency needs all four numbers", edge.efficiency_margin(team("X", {}, {}), H) is None)
ix = idx_of(H, A)
edges = edge.unit_edges("Away St", "Home U", ix)
check("six clear unit edges, all home", len(edges) == 6 and {e["team"] for e in edges} == {"Home U"}, edges)
check("unit edge text names both ranks", "Home U offense success rate #5 vs Away St defense success rate #95"
      in " | ".join(e["text"] for e in edges), edges)
us, _ = edge.units_signal("Away St", "Home U", ix)
check("unit signal to the side with 3+ more edges", us["side"] == "Home U" and us["w"] == edge.W_UNITS, us)

# -- splits -------------------------------------------------------------------------
BLOCK = ('<div class="tb-se-title"><h5><a> {a} @ {h} </a></h5><span>Sat 11/21</span></div>'
         '<div><div>Selection</div><div>Odds</div><div>Handle</div><div>Bets</div></div>'
         '<div> Spread </div><div><div>{a} {sa}</div><div><a> −110 </a></div><div>{ha}% <div></div></div>'
         '<div>{ba}% <div></div></div></div><div><div>{h} {sh}</div><div><a> −110 </a></div>'
         '<div>{hh}% <div></div></div><div>{bh}% <div></div></div></div>')
page = (BLOCK.format(a="Oregon", h="Michigan State", sa="-13.5", sh="+13.5", ha=100, ba=100, hh=0, bh=0)
        + BLOCK.format(a="Texas A&amp;M", h="Oklahoma", sa="+2.5", sh="-2.5", ha=62, ba=38, hh=38, bh=62))
rows = edge.parse_dk_splits(page)
check("two games parsed", len(rows) == 2 and rows[1]["away"] == "Texas A&M", rows)
check("handle/bets per side", rows[1]["sides"] == {"Texas A&M": {"handle": 62, "bets": 38},
                                                   "Oklahoma": {"handle": 38, "bets": 62}}, rows)
sides = edge.find_split(rows, "Texas A&M", "Oklahoma", None)
s = edge.splits_signal("Texas A&M", "Oklahoma", sides)
check("money beats tickets by 24 -> signal", s["side"] == "Texas A&M" and s["w"] == edge.W_SPLITS
      and "62% of the money" in s["why"] and "62% of spread bets on Oklahoma" in s["why"], s)
check("100/0 early-week splits are ignored",
      edge.splits_signal("Oregon", "Michigan State", edge.find_split(rows, "Oregon", "Michigan State", None)) is None)
s = edge.splits_signal("A", "B", {"A": {"handle": 70, "bets": 66}, "B": {"handle": 30, "bets": 34}})
check("agreeing money and tickets: info only", s["side"] is None and "agree" in s["why"], s)
check("no row -> nothing", edge.find_split(rows, "Ohio", "Akron", None) is None)

# -- weather ------------------------------------------------------------------------
w = edge.espn_weather("47F, wind gusts 28 mph, 4% chance of precipitation")
check("ESPN weather text parsed", w == {"temp_f": 47, "wind_mph": None, "gust_mph": 28, "rain_pct": 4, "src": "ESPN"}, w)
s = edge.weather_signal("Utah", "Iowa", -3.5, w)
check("gusty: small tilt to the points side", s["side"] == "Utah" and s["w"] == edge.W_WEATHER, s)
s = edge.weather_signal("Utah", "Iowa", -3.5, {"temp_f": 60, "wind_mph": 5, "gust_mph": 9, "rain_pct": 10,
                                               "src": "Open-Meteo"})
check("calm: info only", s["side"] is None and "Open-Meteo" in s["why"] and "no real weather factor" in s["why"], s)
s = edge.weather_signal("Utah", "Iowa", -3.5, None, dome=True)
check("dome", s["side"] is None and "dome" in s["why"], s)
check("no forecast, no dome: nothing", edge.weather_signal("Utah", "Iowa", -3.5, None) is None)

# -- weighted lean -------------------------------------------------------------------
g = {"id": 9, "away": "Away St", "home": "Home U", "line": -12.0, "fpi_margin": 25.0}


def market(home_spread):
    return {"market": {"details": f"HOME {home_spread:+g}".replace("+", "") if home_spread else "EVEN"},
            "home": {"abbr": "HOME"}, "away": {"abbr": "AWAY"}}


# pool Home -12. Ratings all say Home covers: SP+ 17.5 (2), efficiency 14.4 (1),
# units (1), FPI 25 (1) = 5. Market Home -9.5 = 2.5 points of value on Away (6).
m = chat.matchup_signals(g, market(-9.5), ix)
whys = " | ".join(x["why"] for x in m["signals"])
check("market gap 2.5 = big weight", any(x["w"] == edge.W_MARKET_BIG and x["side"] == "Away St"
                                         for x in m["signals"]), m)
check("a 2.5-point market gap outweighs every rating", m["lean"] == "Away St" and m["strength"] == "lean"
      and m["score"] == {"Away St": 6, "Home U": 5}, m)
check("efficiency and unit lines present", "efficiency (season PPA" in whys and "unit matchups" in whys, whys)
m = chat.matchup_signals(g, market(-11.5), ix)
check("half-point gap is small: ratings outvote it", m["lean"] == "Home U" and m["score"]["Away St"] == 3, m)
ext = {"splits": {9: {"Away St": {"handle": 60, "bets": 40}, "Home U": {"handle": 40, "bets": 60}}},
       "weather": {9: {"temp_f": 30, "wind_mph": 22, "gust_mph": 35, "rain_pct": 20, "src": "Open-Meteo"}}}
m2 = chat.matchup_signals(g, market(-11.5), ix, ext)
check("splits + weather add to Away", m2["score"]["Away St"] == 3 + edge.W_SPLITS + edge.W_WEATHER, m2["score"])
m3 = chat.matchup_signals(g, market(-16), ix)
check("strong when market and ratings agree", m3["lean"] == "Home U" and m3["strength"] == "strong", m3)

if FAILED:
    print(f"\n{len(FAILED)} FAILED")
    sys.exit(1)
print("\nALL PASS")
