"""
weights_fit.py: the out-of-sample weight check. Synthetic data only: no network, no GPU, no CFBD cache.

    ./.venv/bin/python3 -I test_weights_fit.py
"""

from __future__ import annotations

import collections
import datetime as dt
import os
import random
import sys

os.environ["PICKY_CFBD_DB"] = "/nonexistent/cfbd.sqlite"
os.environ["PICKY_CFBD_HIST"] = "/nonexistent/hist.sqlite"
sys.path.insert(0, ".")
import pick_brief as pb  # noqa: E402
import weights_fit as wf  # noqa: E402

FAILED = []


def check(name, ok, got=None):
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else f"\n     got: {got!r}"))
    if not ok:
        FAILED.append(name)


# -- the hand baseline is pick_brief's own weights ---------------------------------------------------
check("HAND mirrors pick_brief (ratings 3.5 = SP+ 2 + FPI 1 + SRS 0.5, bye W_MED, rivalry W_RIVALRY)",
      wf.HAND["rating"] == pb.W_SP + pb.W_FPI + pb.W_SRS == 3.5 and wf.HAND["bye"] == pb.W_MED
      and wf.HAND["rivalry"] == pb.W_RIVALRY, wf.HAND)

# -- the L2 logistic fit finds a real signal and shrinks noise --------------------------------------
rng = random.Random(7)
rows = []
for i in range(1500):
    x = {k: float(rng.choice((-1, 0, 1))) for k in wf.FEATS}
    p = 0.5 + 0.15 * x["letdown"]  # only letdown matters
    rows.append({"x": x, "y": int(rng.random() < p), "season": 2024, "week": i % 14 + 1})
b = wf.fit_logit(rows, 10)
beta = dict(zip(wf.FEATS, b[1:]))
others = max(abs(v) for k, v in beta.items() if k != "letdown")
check("fit_logit: the one real feature gets the big positive weight", beta["letdown"] > 0.4 and others < 0.25,
      (beta["letdown"], others))
b_heavy = wf.fit_logit(rows, 1e6)
check("fit_logit: a huge L2 penalty shrinks every beta to ~0", max(abs(v) for v in b_heavy[1:]) < 0.01, b_heavy)
fw = wf.as_weights(b)
check("as_weights: same total absolute weight as HAND, letdown largest",
      abs(sum(abs(v) for v in fw.values()) - sum(abs(v) for v in wf.HAND.values())) < 0.2
      and max(fw, key=lambda k: abs(fw[k])) == "letdown", fw)
lam = wf.pick_lambda(rows)
check("pick_lambda picks from the grid by time", lam in (1, 3, 10, 30, 100, 300, 1000), lam)

rec = wf.record([{"y": 1}, {"y": 0}, {"y": 1}], lambda r: 1)
check("record: picks home three times, 2-1", (rec["w"], rec["l"], rec["n"]) == (2, 1, 3), rec)

# -- market weight: pick_brief's line-value rule on the league line ---------------------------------
check("market_weight: league -1.5 vs market -3.5 = 1.5+ value on home + key number 3",
      wf.market_weight(-1.5 - -3.5, -1.5, -3.5) == pb.W_VALUE_BIG + pb.W_KEY)
check("market_weight: a half point toward the away side across 7 = value + key",
      wf.market_weight(-7.0 - -6.5, -7.0, -6.5) == -(pb.W_VALUE + pb.W_KEY))
check("market_weight: a half point toward the away side, no key number", wf.market_weight(-5.0 - -4.5, -5.0, -4.5) == -pb.W_VALUE)
check("market_weight: under half a point is nothing", wf.market_weight(0.2, -3.0, -3.2) == 0.0)

# -- features see only weeks before the game --------------------------------------------------------
T = dt.datetime(2025, 9, 1, 18, tzinfo=dt.timezone.utc)


def season(after_result):
    S = wf.Season.__new__(wf.Season)
    S.year, S.fbs, S._elo_rank, S._epa_rank, S.vby, S.home_venue = 2025, {"home u", "away u"}, {}, {}, {}, {}
    S.prev, S.close, S.ppa = collections.defaultdict(list), {}, collections.defaultdict(list)
    S.sched = collections.defaultdict(list)
    for t, o in (("home u", "away u"), ("away u", "home u")):
        for wk in range(1, 8):
            S.sched[t].append({"start": T + dt.timedelta(days=7 * wk), "week": wk, "opp": "x" if wk != 6 else o,
                               "home": t == "home u", "neutral": False, "pts": 30, "opp_pts": 20, "venue": None,
                               "elo": 1600 if t == "home u" else 1500})
    # home u covered weeks 1-4 at home as a favorite; week 7 (after the game) is whatever we pass in
    S.ats = collections.defaultdict(list, {"home u": [(wk, "fav", True, 5.0) for wk in range(1, 5)] + [(7, "fav", True, after_result)]})
    return S


xa = wf.features(season(+10), "Home U", "Away U", 6, T + dt.timedelta(days=42), -3.5, 50, False, None)
xb = wf.features(season(-10), "Home U", "Away U", 6, T + dt.timedelta(days=42), -3.5, 50, False, None)
check("features: a result from after the game changes nothing (no lookahead)", xa == xb, (xa, xb))
check("features: 4-0 ATS at home before week 6 -> home_ats +1, form +1, team_ats +1",
      (xa["home_ats"], xa["form"], xa["team_ats"]) == (1, 1, 1), xa)
check("features: Elo 100 points + home field projects home by ~6.6 vs -3.5 -> rating +1", xa["rating"] == 1.0, xa)

print(f"\n{len(FAILED)} failed" if FAILED else "\nall passed")
sys.exit(1 if FAILED else 0)
