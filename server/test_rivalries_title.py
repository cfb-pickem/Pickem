"""
Factors 14 (depth chart), 24 (rivalry), 26 (title stakes: clinch/elimination) and 57 (resting starters):
rivalries.py, title_race.py and their pick_brief hooks. Synthetic data, no network, no GPU.

    ./.venv/bin/python3 -I test_rivalries_title.py
"""

from __future__ import annotations

import datetime as dt
import os
import sys

os.environ["PICKY_CFBD_DB"] = "/nonexistent/cfbd.sqlite"
sys.path.insert(0, ".")
import cfbd  # noqa: E402
import pick_brief as pb  # noqa: E402
import rivalries as rv  # noqa: E402
import title_race as tr  # noqa: E402

FAILED = []


def check(name, ok, got=None):
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else f"\n     got: {got!r}"))
    if not ok:
        FAILED.append(name)


# -- 24: the rivalry list ---------------------------------------------------------------------------
check("80+ rivalries, no duplicate pairs", len(rv.RIVALRIES) >= 80
      and len({frozenset((cfbd.norm(a), cfbd.norm(b))) for a, b, _ in rv.RIVALRIES}) == len(rv.RIVALRIES),
      len(rv.RIVALRIES))
check("Iron Bowl either order", rv.rivalry("Alabama", "Auburn") == rv.rivalry("Auburn", "Alabama") == "Iron Bowl")
for a, b in (("Ohio State", "Michigan"), ("Texas", "Oklahoma"), ("Army", "Navy"), ("Notre Dame", "USC"),
             ("Pittsburgh", "West Virginia"), ("Indiana", "Purdue"), ("Iowa", "Minnesota"), ("BYU", "Utah"),
             ("Oregon", "Oregon State"), ("Virginia", "Virginia Tech"), ("Michigan State", "Michigan")):
    check(f"listed: {a}-{b}", bool(rv.rivalry(a, b)))
check("spelling variants match (Ole Miss St. -> state, accents)",
      rv.rivalry("Ole Miss", "Mississippi St.") == "Egg Bowl" and rv.rivalry("San Jose State", "Fresno State") == "Valley Trophy")
check("not a rivalry", rv.rivalry("Iowa", "Illinois") is None and rv.rivalry("LSU", "Tennessee") is None)

g = {"away": "Auburn", "home": "Alabama", "line": -10.5}
f = pb.f_rivalry(g)
check("f_rivalry: flagged, names the dog, weight follows W_RIVALRY (0 per the backtest)",
      len(f) == 1 and f[0]["n"] == 24 and f[0]["flag"] and "Iron Bowl" in f[0]["why"] and "Auburn is the underdog" in f[0]["why"]
      and f[0]["w"] == pb.W_RIVALRY == 0.0, f)
check("f_rivalry: nothing for a non-rivalry", pb.f_rivalry({"away": "Iowa", "home": "Illinois", "line": 6.5}) == [])


def row(h, a, hs, as_, spread, season=2025):
    return {"season": season, "homeTeam": h, "awayTeam": a, "homeClassification": "fbs", "awayClassification": "fbs",
            "homeScore": hs, "awayScore": as_, "lines": [{"provider": "DraftKings", "spread": spread}]}


bt = rv.backtest([row("Alabama", "Auburn", 24, 20, -10.5),   # dog Auburn covers (lost by 4 < 10.5)
                  row("Michigan", "Ohio State", 10, 30, 3.5),  # dog Michigan (home +3.5) loses by 20: no cover
                  row("Iowa", "Illinois", 20, 10, -6.5),       # non-rivalry: dog Illinois doesn't cover
                  row("Iowa", "Minnesota", 17, 14, -3.0)])     # push on 3: left out
check("backtest: rivalry dog 1-1, other dog 0-1, pushes out",
      bt["rivalry_dog"]["w"] == 1 and bt["rivalry_dog"]["n"] == 2 and bt["other_dog"] == {**bt["other_dog"], "w": 0, "n": 1}, bt)

# -- 26 / 57: title race ----------------------------------------------------------------------------
T0 = dt.datetime(2026, 11, 1, 18, tzinfo=dt.timezone.utc)


def game(h, a, hp, ap, days, conf="SEC", cg=True):
    return {"seasonType": "regular", "startDate": (T0 + dt.timedelta(days=days)).isoformat(), "conferenceGame": cg,
            "homeTeam": h, "awayTeam": a, "homeClassification": "fbs", "awayClassification": "fbs",
            "homeConference": conf, "awayConference": conf, "homePoints": hp, "awayPoints": ap}


# four-team conference, three rounds played: A 3-0, B 2-1, C 1-2, D 0-3; a 4th round (A-D, B-C) to come
G = [game("A", "B", 30, 20, 0), game("C", "D", 28, 21, 0),
     game("A", "C", 35, 10, 7), game("B", "D", 24, 3, 7),
     game("D", "A", 7, 42, 14), game("B", "C", 21, 14, 14),
     game("A", "D", None, None, 21), game("C", "B", None, None, 21),
     game("D", "Gamma", 10, 31, 3, conf=None, cg=False)]
G[-1]["awayConference"], G[-1]["homeConference"] = "FBS Independents", "SEC"
now = T0 + dt.timedelta(days=18)
tbl = tr.table(G, now)
st = {t: tr.status(t, G, now, tbl) for t in ("A", "B", "C", "D")}
check("table: conference W-L and games left", (tbl["a"]["w"], tbl["a"]["l"], tbl["a"]["rem"]) == (3, 0, 1)
      and (tbl["d"]["w"], tbl["d"]["l"], tbl["d"]["losses"]) == (0, 3, 4), tbl)
check("A clinched (only B can reach 3 wins)", st["A"]["title"] == "clinched", st["A"])
check("B alive, C alive (C's best is 2, only A is above it)", st["B"]["title"] == "alive" and st["C"]["title"] == "alive",
      (st["B"], st["C"]))
check("D eliminated (A and B already above its best of 1) and out of the CFP (4 losses)",
      st["D"]["title"] == "eliminated" and st["D"]["cfp"] == "out", st["D"])
check("A's next game is its last regular-season game", st["A"]["last_game"])
early = tr.status("D", G, T0 + dt.timedelta(days=1), None)
check("before= ignores games that hadn't started: D 0-1 on day 1 (only the day-0 loss), alive",
      (early["w"], early["l"], early["rem"], early["title"]) == (0, 1, 3, "alive"), early)
check("line() reads like a sentence", tr.line(st["A"]) == "A (3-0 in the SEC, 1 conference game left) has clinched a SEC title-game spot",
      tr.line(st["A"]))

g = {"away": "D", "home": "B", "line": -14.5}
f = pb.f_stakes(g, None, None, None, {"D": st["D"], "B": st["B"]})
check("f_stakes: the alive team gets title stakes over the eliminated one (clinch math, not AP)",
      [(x["n"], x["side"], x["w"]) for x in f] == [(26, "B", pb.W_LIGHT)] and "out of the SEC title race" in f[0]["why"], f)
g2 = {"away": "D", "home": "A", "line": -30.5}
f = pb.f_stakes(g2, None, None, None, {"D": {**st["D"], "cfp": "alive"}, "A": st["A"]})
check("f_stakes: a clinched team is flagged, no weight", [(x["n"], x["side"], x["w"], x["flag"]) for x in f] == [(26, None, 0, True)], f)
f = pb.f_cfp_incentive(g2, {"D": st["D"], "A": st["A"]})
check("f_cfp_incentive: clinched + last regular-season game = resting-starter risk, lean to the opponent",
      [(x["n"], x["side"]) for x in f] == [(57, "D")] and f[0]["flag"], f)
check("f_cfp_incentive: nothing when nobody has clinched", pb.f_cfp_incentive(g, {"D": st["D"], "B": st["B"]}) == [])

# -- 14: depth chart ----------------------------------------------------------------------------------
items = [{"date": "2026-11-10", "text": "Illinois named Boyd the starter for Saturday after the Houser injury.", "label": "ESPN"},
         {"date": "2026-11-12", "text": "Illinois practice notes: weather moves practice indoors.", "label": "CBS"}]
f = pb.f_depth({"away": "Iowa", "home": "Illinois"}, "Illinois", items)
check("f_depth: a named starter is a flagged, weightless #14", len(f) == 1 and f[0]["n"] == 14 and f[0]["w"] == 0
      and f[0]["flag"] and "Boyd" in f[0]["why"] and "(ESPN)" in f[0]["why"], f)
for txt in ("Smith was benched at halftime", "Jones will start at quarterback", "Lee takes over as the starter",
            "freshman earned the starting job", "Doe is QB1 now"):
    check(f"f_depth reads: {txt}", bool(pb.f_depth({"away": "a", "home": "b"}, "b", [{"text": txt}])))
check("f_depth: nothing in plain news", pb.f_depth({"away": "a", "home": "b"}, "b", items[1:]) == [])

# -- status table ---------------------------------------------------------------------------------------
check("STATUS: 24 and 26 live, 14 and 57 partial, no 'none' left",
      [pb.STATUS[n][0] for n in (24, 26, 14, 57)] == ["live", "live", "partial", "partial"]
      and not [n for n, (s, _) in pb.STATUS.items() if s == "none"])

print(f"\n{len(FAILED)} failed" if FAILED else "\nall passed")
sys.exit(1 if FAILED else 0)
