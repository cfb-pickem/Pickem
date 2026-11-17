"""
Out-of-sample check of pick_brief's hand-set factor weights (Charlie 2026-11-16, lane "weights").

    ./.venv/bin/python3 -I weights_fit.py fetch     # CFBD history -> cache/cfbd_hist.sqlite (~7 calls, once)
    ./.venv/bin/python3 -I weights_fit.py fit       # dataset + fits + report (no network)

Dataset: every FBS-vs-FBS regular-season game of 2024, 2025 and 2026 so far with a final score and a CFBD
closing spread (DraftKings first), graded against that close (pushes out). Each game gets the factors
pick_brief can compute WITHOUT lookahead: pregame Elo (CFBD's, already as-of the game) instead of
end-of-season SP+/FPI/SRS, season-to-date net PPA from game-level advanced stats, and schedule/ATS factors
from games played before kickoff. Market factors (line value, key numbers) exist only against the league
line, so they're scored on the league's own pool games, not here. QB/injury/news factors have no history.

Splits are by time, never random: train 2024 -> test 2025 + 2026; train 2024-25 -> test 2026.
The fit is an L2 logistic regression on cover (pure Python: no numpy on this box), C picked on the last
third of the training span (also by time). New weights ship only if they beat the hand-set ones out of
sample; otherwise the report says so and nothing changes.
"""

from __future__ import annotations

import collections
import datetime as dt
import json
import math
import os
import sqlite3
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import cfbd  # noqa: E402
import pick_brief as pb  # noqa: E402
import rivalries  # noqa: E402

HIST_DB = Path(os.environ.get("PICKY_CFBD_HIST", HERE / "cache" / "cfbd_hist.sqlite"))
REPORT = Path(os.environ.get("PICKY_WEIGHTS_REPORT", HERE / "cache" / "weights_report.json"))
SEASONS = (2024, 2025, 2026)
FETCH = {  # name -> (path, params); 2025 lines and 2026 games/lines come from the live cache (pick_brief.raw)
    "games_2023": ("/games", {"year": "2023", "seasonType": "regular"}),  # 2024's revenge games
    "games_2024": ("/games", {"year": "2024", "seasonType": "regular"}),
    "games_2025": ("/games", {"year": "2025", "seasonType": "regular"}),
    "lines_2024": ("/lines", {"year": "2024", "seasonType": "regular"}),
    "adv_2024": ("/stats/game/advanced", {"year": "2024", "seasonType": "regular", "excludeGarbageTime": "true"}),
    "adv_2025": ("/stats/game/advanced", {"year": "2025", "seasonType": "regular", "excludeGarbageTime": "true"}),
    "adv_2026": ("/stats/game/advanced", {"year": "2026", "seasonType": "regular", "excludeGarbageTime": "true"}),
}


# -----------------------------------------------------------------------------
# fetch (the only network step)
# -----------------------------------------------------------------------------

def hist_db(path: Path | None = None) -> sqlite3.Connection:
    con = sqlite3.connect(path or HIST_DB)
    con.execute("CREATE TABLE IF NOT EXISTS datasets(name TEXT PRIMARY KEY, fetched_at REAL, json TEXT)")
    return con


def fetch(names: list[str] | None = None) -> dict:
    """Pull the missing history datasets, counted against cfbd's monthly cap and the shared key reserve."""
    import httpx
    hist, main = hist_db(), cfbd.connect()
    have = {r[0] for r in hist.execute("SELECT name FROM datasets")}
    todo = [n for n in (names or FETCH) if n not in have]
    out = {"fetched": {}, "failed": {}, "skipped_cached": sorted(set(names or FETCH) & have)}
    if not todo:
        return out
    with httpx.Client(base_url=cfbd.API, timeout=120.0,
                      headers={"Authorization": f"Bearer {cfbd._api_key()}", "Accept": "application/json"}) as c:
        info = c.get("/info").json() or {}
        cfbd.check_budget(main, len(todo), info.get("usedCalls"))
        for n in todo:
            cfbd.check_budget(main, 1)
            path, params = FETCH[n]
            r = c.get(path, params=params)
            rem = r.headers.get("x-calllimit-remaining")
            cfbd._record_call(main, path, r.status_code, int(rem) if rem and rem.isdigit() else None)
            if r.status_code != 200:
                out["failed"][n] = f"{r.status_code} {r.text[:120]}"
                continue
            data = r.json()
            hist.execute("INSERT OR REPLACE INTO datasets VALUES(?,?,?)", (n, time.time(), json.dumps(data)))
            hist.commit()
            out["fetched"][n] = len(data)
    return out


def load(name: str):
    """A history dataset, else the live cache's copy (games/lines of the current and last season)."""
    row = hist_db().execute("SELECT json FROM datasets WHERE name=?", (name,)).fetchone()
    if row:
        return json.loads(row[0])
    live = {"games_2026": "games", "lines_2026": "lines", "lines_2025": "lines_prev"}.get(name)
    return pb.raw(live) if live else None


# -----------------------------------------------------------------------------
# dataset: one row per graded game, factor directions as of that game's week (no lookahead)
# -----------------------------------------------------------------------------

# pick_brief's weights on the factors this history can compute. Each feature is signed toward the HOME side
# covering (+1 home, -1 away; two-sided factors add the home and away hits, so -2..2).
HAND = {
    "rating": pb.W_SP + pb.W_FPI + pb.W_SRS,  # SP+ / FPI / SRS projections vs the line: pregame Elo stands in for all three
    "epa": pb.W_EFF,           # 37: season-to-date net PPA, a 30+ rank gap
    "home_ats": pb.W_LIGHT,    # 29
    "team_ats": pb.W_LIGHT,    # 17
    "form": pb.W_LIGHT,        # 50: covered 4 straight / 0 of 4
    "bye": pb.W_MED,           # 18
    "short": pb.W_MED,         # 19
    "lookahead": pb.W_MED,     # 20
    "letdown": pb.W_MED,       # 21
    "sandwich": pb.W_LIGHT,    # 22
    "revenge": pb.W_MED,       # 23 (value 1 after a 14+ point loss, else 0.5: the hand rule's W_MED vs W_LIGHT)
    "rivalry": pb.W_RIVALRY,   # 24 (0 since the rivalry backtest)
    "total": pb.W_LIGHT,       # 5: 14+ spread with a 45-or-less total -> the dog
    "travel": pb.W_MED,        # 30: 2+ time zones east for an early kick
}
FEATS = list(HAND)


def _start(g):
    try:
        return dt.datetime.fromisoformat(str(g.get("startDate")).replace("Z", "+00:00"))
    except ValueError:
        return None


def _close(row):
    books = sorted(row.get("lines") or [], key=lambda ln: ln.get("provider") != "DraftKings")
    sp = next((float(b["spread"]) for b in books if b.get("spread") is not None), None)
    ou = next((float(b["overUnder"]) for b in books if b.get("overUnder") is not None), None)
    return sp, ou


class Season:
    """Per-season indexes, everything keyed so a game only sees weeks before its own."""

    def __init__(self, year: int):
        self.year = year
        self.games = [g for g in (load(f"games_{year}") or []) if g.get("seasonType", "regular") == "regular"]
        self.lines = load(f"lines_{year}") or []
        self.adv = load(f"adv_{year}") or []
        self.prev = collections.defaultdict(list)  # {home, away} -> last season's meetings
        for g in load(f"games_{year - 1}") or (load(f"lines_{year - 1}") or []):
            self.prev[frozenset((cfbd.norm(g.get("homeTeam")), cfbd.norm(g.get("awayTeam"))))].append(g)
        self.fbs = {cfbd.norm(g[f"{s}Team"]) for g in self.games for s in ("home", "away")
                    if g.get(f"{s}Classification") == "fbs"}
        self.sched = collections.defaultdict(list)
        for g in self.games:
            st = _start(g)
            for me, op in (("home", "away"), ("away", "home")):
                self.sched[cfbd.norm(g[f"{me}Team"])].append({
                    "start": st, "week": g.get("week"), "opp": cfbd.norm(g[f"{op}Team"]), "home": me == "home",
                    "neutral": bool(g.get("neutralSite")), "pts": g.get(f"{me}Points"), "opp_pts": g.get(f"{op}Points"),
                    "venue": g.get("venueId"), "elo": g.get(f"{me}PregameElo")})
        for k in self.sched:
            self.sched[k].sort(key=lambda x: (x["start"] or dt.datetime.max.replace(tzinfo=dt.timezone.utc)))
        self.ats = collections.defaultdict(list)  # team -> [(week, role, home, cover margin)]
        self.close = {}
        for r in self.lines:
            sp, ou = _close(r)
            if sp is None:
                continue
            self.close[(cfbd.norm(r["homeTeam"]), cfbd.norm(r["awayTeam"]), r.get("week"))] = (sp, ou, r)
            if r.get("homeScore") is None or r.get("awayScore") is None:
                continue
            m = r["homeScore"] - r["awayScore"] + sp
            for team, sign, home in ((r["homeTeam"], 1, True), (r["awayTeam"], -1, False)):
                role = "fav" if (sp < 0) == home and sp != 0 else "dog"
                self.ats[cfbd.norm(team)].append((r.get("week"), role, home, sign * m))
        self.ppa = collections.defaultdict(list)  # team -> [(week, off ppa, def ppa)]
        for a in self.adv:
            o, d = (a.get("offense") or {}).get("ppa"), (a.get("defense") or {}).get("ppa")
            if o is not None and d is not None:
                self.ppa[cfbd.norm(a["team"])].append((a.get("week"), o, d))
        self._elo_rank, self._epa_rank = {}, {}
        vby = {v.get("id"): v for v in (pb.raw("venues") or [])}
        self.vby = vby
        homes = collections.defaultdict(collections.Counter)
        for t, rows in self.sched.items():
            for x in rows:
                if x["home"] and not x["neutral"] and x["venue"]:
                    homes[t][x["venue"]] += 1
        self.home_venue = {t: c.most_common(1)[0][0] for t, c in homes.items()}

    def elo_rank(self, week: int) -> dict:
        """FBS Elo ranks as of week `week` (each team's latest pregame Elo from that week or earlier)."""
        if week not in self._elo_rank:
            cur = {}
            for t in self.fbs:
                e = [x["elo"] for x in self.sched.get(t, []) if x["elo"] and (x["week"] or 0) <= week]
                if e:
                    cur[t] = e[-1]
            self._elo_rank[week] = {t: i + 1 for i, t in enumerate(sorted(cur, key=lambda t: -cur[t]))}
        return self._elo_rank[week]

    def epa_rank(self, week: int) -> dict:
        """FBS ranks of season-to-date net PPA (offense - defense) from games before `week` (3+ games)."""
        if week not in self._epa_rank:
            net = {}
            for t in self.fbs:
                rows = [r for r in self.ppa.get(t, []) if (r[0] or 0) < week]
                if len(rows) >= 3:
                    net[t] = sum(r[1] for r in rows) / len(rows) - sum(r[2] for r in rows) / len(rows)
            self._epa_rank[week] = {t: i + 1 for i, t in enumerate(sorted(net, key=lambda t: -net[t]))}
        return self._epa_rank[week]


def features(S: Season, home: str, away: str, week: int, start, spread: float, ou: float | None,
             neutral: bool, venue_id) -> dict:
    """pick_brief's factor directions for one game, from data before `week` only."""
    h, a = cfbd.norm(home), cfbd.norm(away)
    x = dict.fromkeys(FEATS, 0.0)
    er = S.elo_rank(week)
    # rating: pregame Elo projection vs the line (pick_brief._by_margin: a half point or more)
    eh = next((r["elo"] for r in S.sched.get(h, []) if r["week"] == week and r["opp"] == a), None)
    ea = next((r["elo"] for r in S.sched.get(a, []) if r["week"] == week and r["opp"] == h), None)
    if eh and ea:
        proj = (eh - ea) / ELO_PER_PT + (0 if neutral else pb.HOME_FIELD)
        cover = proj + spread
        x["rating"] = 0.0 if abs(cover) < 0.5 else (1.0 if cover > 0 else -1.0)
    pr = S.epa_rank(week)
    if h in pr and a in pr and abs(pr[h] - pr[a]) >= pb.RANK_GAP:
        x["epa"] = 1.0 if pr[h] < pr[a] else -1.0
    # ATS factors (17, 29, 50) from games before this week
    roles = {h: "fav" if spread < 0 else "dog", a: "dog" if spread < 0 else "fav"}
    for t, sign in ((h, 1), (a, -1)):
        rows = [r for r in S.ats.get(t, []) if (r[0] or 0) < week]
        mine = [r[3] for r in rows if r[1] == roles[t] and r[3] != 0]
        if len(mine) >= 4:
            pct = sum(m > 0 for m in mine) / len(mine)
            x["team_ats"] += sign * (1 if pct >= 0.7 else -1 if pct <= 0.3 else 0)
        last = [r[3] for r in rows if r[3] != 0][-4:]
        if len(last) >= 4:
            cov = sum(m > 0 for m in last)
            x["form"] += sign * (1 if cov == 4 else -1 if cov == 0 else 0)
        if t == h:
            hm = [r[3] for r in rows if r[2] and r[3] != 0]
            if len(hm) >= 3:
                pct = sum(m > 0 for m in hm) / len(hm)
                x["home_ats"] = 1 if pct >= 0.7 else -1 if pct <= 0.3 else 0
    # schedule spots (18-23)
    fav = h if spread < 0 else a if spread > 0 else None

    def split(t):
        rows = S.sched.get(t, [])
        return [r for r in rows if (r["week"] or 0) < week], [r for r in rows if (r["week"] or 0) > week]
    (bh, ah), (ba, aa) = split(h), split(a)
    if bh and ba and start and bh[-1]["start"] and ba[-1]["start"]:
        rest = {h: (start - bh[-1]["start"]).days, a: (start - ba[-1]["start"]).days}
        for t, o, sign in ((h, a, 1), (a, h, -1)):
            if rest[t] >= pb.BYE_DAYS and rest[o] < pb.BYE_DAYS - 3:
                x["bye"] += sign
            if rest[t] <= pb.SHORT_DAYS and rest[o] >= rest[t] + 2:
                x["short"] -= sign
    big = lambda t: (er.get(t) or 999) <= pb.BIG_GAME_SP_RANK  # noqa: E731
    for t, o, before, after, sign in ((h, a, bh, ah, 1), (a, h, ba, aa, -1)):
        weak_now = not big(o)
        if after and t == fav and weak_now and big(after[0]["opp"]):
            x["lookahead"] -= sign
        if before and t == fav:
            last = before[-1]
            won = last["pts"] is not None and last["opp_pts"] is not None and last["pts"] > last["opp_pts"]
            key = (t, last["opp"], last["week"]) if last["home"] else (last["opp"], t, last["week"])
            lsp = (S.close.get(key) or (None,))[0]
            was_dog = lsp is not None and ((lsp > 0) if last["home"] else (lsp < 0))
            if won and (was_dog or big(last["opp"])):
                x["letdown"] -= sign
        if before and after and weak_now and big(before[-1]["opp"]) and big(after[0]["opp"]):
            x["sandwich"] -= sign
    meets = [g for g in S.prev.get(frozenset((h, a)), []) if g.get("homePoints", g.get("homeScore")) is not None]
    if meets:
        g = meets[-1]
        hp, ap = g.get("homePoints", g.get("homeScore")), g.get("awayPoints", g.get("awayScore"))
        if hp != ap:
            loser = cfbd.norm(g["awayTeam"] if hp > ap else g["homeTeam"])
            v = 1.0 if abs(hp - ap) >= 14 else 0.5
            x["revenge"] = v if loser == h else -v
    if rivalries.rivalry(home, away) and spread:
        x["rivalry"] = 1.0 if spread > 0 else -1.0
    if ou is not None and abs(spread) >= 14 and ou <= 45:
        x["total"] = 1.0 if spread > 0 else -1.0
    av, gv = S.vby.get(S.home_venue.get(a)), S.vby.get(venue_id)
    if av and gv and start:
        ta, tg = pb._tz(av), pb._tz(gv)
        kick = start.astimezone(dt.timezone(dt.timedelta(hours=-6)))
        if ta is not None and tg is not None and tg - ta >= pb.TRAVEL_TZ and kick.hour < pb.EARLY_KICK_HOUR:
            x["travel"] = 1.0
    return x


ELO_PER_PT = 25.0  # refit on the training span by fit_elo(); 25 Elo points per point of margin is the usual rule


def fit_elo(rows: list[dict]) -> float:
    """Least-squares Elo points per point of margin (home margin ~ elo diff / k + home field), training rows only."""
    xs = [(r["elo_diff"], r["margin"] - (0 if r["neutral"] else pb.HOME_FIELD)) for r in rows if r["elo_diff"] is not None]
    sxx = sum(d * d for d, _ in xs)
    sxy = sum(d * m for d, m in xs)
    return sxx / sxy if sxy else 25.0


def dataset() -> list[dict]:
    """Every FBS-vs-FBS game with a close and a final, all seasons, features + label (home covered)."""
    out = []
    for y in SEASONS:
        S = Season(y)
        for g in S.games:
            if g.get("homeClassification") != "fbs" or g.get("awayClassification") != "fbs":
                continue
            if g.get("homePoints") is None or g.get("awayPoints") is None:
                continue
            h, a, wk = cfbd.norm(g["homeTeam"]), cfbd.norm(g["awayTeam"]), g.get("week")
            c = S.close.get((h, a, wk))
            if not c:
                continue
            sp, ou, _ = c
            m = g["homePoints"] - g["awayPoints"]
            if m + sp == 0:
                continue
            out.append({"season": y, "week": wk, "start": _start(g), "home": g["homeTeam"], "away": g["awayTeam"],
                        "spread": sp, "ou": ou, "neutral": bool(g.get("neutralSite")), "venue": g.get("venueId"),
                        "margin": m, "y": 1 if m + sp > 0 else 0,
                        "elo_diff": (g["homePregameElo"] - g["awayPregameElo"])
                        if g.get("homePregameElo") and g.get("awayPregameElo") else None, "_S": S})
    return out


def featurize(rows: list[dict]) -> None:
    for r in rows:
        r["x"] = features(r["_S"], r["home"], r["away"], r["week"], r["start"], r["spread"], r["ou"], r["neutral"],
                          r["venue"])


# -----------------------------------------------------------------------------
# models
# -----------------------------------------------------------------------------

def hand_score(x: dict, w: dict | None = None) -> float:
    w = w or HAND
    return sum(w[k] * x[k] for k in FEATS)


def _solve(A, b):
    n = len(b)
    M = [row[:] + [b[i]] for i, row in enumerate(A)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(M[r][c]))
        M[c], M[p] = M[p], M[c]
        if abs(M[c][c]) < 1e-12:
            continue
        for r in range(n):
            if r != c:
                f = M[r][c] / M[c][c]
                M[r] = [M[r][j] - f * M[c][j] for j in range(n + 1)]
    return [M[i][n] / M[i][i] if abs(M[i][i]) > 1e-12 else 0.0 for i in range(n)]


def fit_logit(rows: list[dict], lam: float) -> list[float]:
    """L2 logistic regression on cover by Newton's method: [bias, beta per FEATS]; the bias isn't penalized."""
    X = [[1.0] + [r["x"][k] for k in FEATS] for r in rows]
    y = [r["y"] for r in rows]
    n = len(FEATS) + 1
    b = [0.0] * n
    for _ in range(25):
        g = [0.0] * n
        H = [[0.0] * n for _ in range(n)]
        for xi, yi in zip(X, y):
            z = sum(bj * xj for bj, xj in zip(b, xi))
            p = 1 / (1 + math.exp(-max(-30, min(30, z))))
            for j in range(n):
                g[j] += (p - yi) * xi[j]
                wj = p * (1 - p) * xi[j]
                for k in range(n):
                    H[j][k] += wj * xi[k]
        for j in range(1, n):
            g[j] += lam * b[j]
            H[j][j] += lam
        step = _solve(H, g)
        b = [bj - sj for bj, sj in zip(b, step)]
        if max(abs(s) for s in step) < 1e-7:
            break
    return b


def prob(b: list[float], x: dict) -> float:
    z = b[0] + sum(bj * x[k] for bj, k in zip(b[1:], FEATS))
    return 1 / (1 + math.exp(-max(-30, min(30, z))))


def logloss(b, rows) -> float:
    return -sum(math.log(max(1e-9, p if r["y"] else 1 - p)) for r in rows for p in [prob(b, r["x"])]) / max(1, len(rows))


def pick_lambda(train: list[dict]) -> float:
    """L2 strength by time: fit on the first 2/3 of the training span, score log-loss on the last 1/3."""
    tr = sorted(train, key=lambda r: (r["season"], r["week"] or 0))
    cut = len(tr) * 2 // 3
    grid = (1, 3, 10, 30, 100, 300, 1000)
    return min(grid, key=lambda lam: logloss(fit_logit(tr[:cut], lam), tr[cut:]))


def as_weights(b: list[float]) -> dict:
    """Fitted betas on the hand scale: same total absolute weight as HAND (pick_brief's units), sign kept."""
    raw = dict(zip(FEATS, b[1:]))
    tot_h, tot_f = sum(abs(v) for v in HAND.values()), sum(abs(v) for v in raw.values()) or 1
    return {k: round(v * tot_h / tot_f, 2) for k, v in raw.items()}


def wilson(w: int, n: int) -> list[float]:
    return rivalries._wilson(w, n)


def record(rows, side_fn) -> dict:
    """W-L of the picks side_fn makes (+1 home, -1 away, 0 no pick)."""
    w = n = 0
    for r in rows:
        s = side_fn(r)
        if s:
            n += 1
            w += (s > 0) == bool(r["y"])
    return {"w": w, "l": n - w, "n": n, "pct": round(w / n, 3) if n else 0.0, "ci95": wilson(w, n)}


def evaluate(train, test, label: str) -> dict:
    lam = pick_lambda(train)
    b = fit_logit(train, lam)
    fw = as_weights(b)
    blend = {k: round((HAND[k] + fw[k]) / 2, 2) for k in FEATS}
    sgn = lambda v: (v > 0) - (v < 0)  # noqa: E731
    hand_pick = lambda r: sgn(hand_score(r["x"]))  # noqa: E731
    res = {"split": label, "train_n": len(train), "test_n": len(test), "lambda": lam,
           "elo_per_pt": round(ELO_PER_PT, 1), "fit_weights": fw, "blend_weights": blend,
           "bias_home": round(b[0], 3),
           "hand": record(test, hand_pick),
           "fit_all": record(test, lambda r: sgn(prob(b, r["x"]) - 0.5)),
           "fit_on_hand_games": record([r for r in test if hand_pick(r)], lambda r: sgn(prob(b, r["x"]) - 0.5)),
           "fit_weights_sum": record(test, lambda r: sgn(hand_score(r["x"], fw))),
           "blend": record(test, lambda r: sgn(hand_score(r["x"], blend))),
           "dog": record(test, lambda r: 1 if r["spread"] > 0 else -1 if r["spread"] < 0 else 0),
           "home": record(test, lambda r: 1),
           "test_logloss": {"fit": round(logloss(b, test), 4), "coin": round(math.log(2), 4)}}
    # hand score buckets out of sample, for the confidence words
    bk = collections.defaultdict(lambda: [0, 0])
    for r in test:
        s = hand_score(r["x"])
        if s:
            key = "0.5-1.5" if abs(s) < 2 else "2-3.5" if abs(s) < 4 else "4+"
            bk[key][1] += 1
            bk[key][0] += (s > 0) == bool(r["y"])
    res["hand_buckets"] = {k: {"w": w, "n": n, "pct": round(w / n, 3), "ci95": wilson(w, n)} for k, (w, n) in sorted(bk.items())}
    res["_b"] = b
    return res


def run() -> dict:
    global ELO_PER_PT
    rows = dataset()
    tr24 = [r for r in rows if r["season"] == 2024]
    ELO_PER_PT = fit_elo(tr24)  # training season only
    featurize(rows)
    by = collections.defaultdict(list)
    for r in rows:
        by[r["season"]].append(r)
    splits = [evaluate(by[2024], by[2025] + by[2026], "train 2024 -> test 2025+2026"),
              evaluate(by[2024] + by[2025], by[2026], "train 2024-25 -> test 2026")]
    rep = {"generated_at": dt.datetime.now().isoformat(timespec="seconds"),
           "games": {y: len(by[y]) for y in SEASONS}, "features": FEATS, "hand_weights": HAND,
           "feature_counts": {k: sum(1 for r in rows if r["x"][k]) for k in FEATS},
           "splits": [{k: v for k, v in s.items() if not k.startswith("_")} for s in splits]}
    rep["pool"] = pool_check(rows, splits[0]["_b"], splits[0]["fit_weights"], splits[0]["blend_weights"])
    return rep


# -----------------------------------------------------------------------------
# the league's own pool games, graded against the LEAGUE line (market factors live here)
# -----------------------------------------------------------------------------

def market_weight(gap: float, league: float, close: float) -> float:
    """pick_brief's line-value weight toward the home side (gap = league - close; > 0 means the market likes home more)."""
    a = abs(gap)
    if a < 0.5:
        return 0.0
    w = pb.W_VALUE_BIG if a >= pb.VALUE_BIG_GAP else pb.W_VALUE
    lo, hi = sorted((abs(league), abs(close)))
    if any(lo < k <= hi or lo <= k < hi for k in pb.KEY_NUMBERS) and (league < 0) == (close < 0):
        w += pb.W_KEY
    return w if gap > 0 else -w


def pool_check(rows, b, fw, blend) -> dict:
    """Hand vs fitted non-market weights on graded pool games (2025-26), each plus the same line-value weight."""
    try:
        import lore
        data = lore.load(lore.service_key())
    except Exception as e:  # noqa: BLE001 - no Supabase: report it, don't guess
        return {"error": repr(e)}
    idx = {(r["season"], cfbd.norm(r["home"]), cfbd.norm(r["away"])): r for r in rows}
    out = collections.defaultdict(lambda: [0, 0])
    n_games = 0
    for g in data["games"]:
        if not g.get("winner") or g.get("tiebreaker") or g.get("line") is None or not (1 <= (g.get("week") or 0) <= 15):
            continue
        r = idx.get((g["cfb_season"], cfbd.norm(g["Home"]), cfbd.norm(g["Away"])))
        if not r:
            continue
        n_games += 1
        league = float(g["line"])
        x = features(r["_S"], r["home"], r["away"], r["week"], r["start"], league, r["ou"], r["neutral"], r["venue"])
        mk = market_weight(league - r["spread"], league, r["spread"])
        home_cov = lore_same(g["Home"], g["winner"])
        for name, s in (("hand", hand_score(x) + mk), ("fit", hand_score(x, fw) + mk), ("blend", hand_score(x, blend) + mk),
                        ("market_only", mk)):
            if s:
                out[name][1] += 1
                out[name][0] += (s > 0) == home_cov
    return {"games": n_games, **{k: {"w": w, "l": n - w, "n": n, "pct": round(w / n, 3) if n else 0.0, "ci95": wilson(w, n)}
                                 for k, (w, n) in out.items()}}


def lore_same(a, b) -> bool:
    import lore
    return lore.same(a, b)


if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "fetch":
    print(json.dumps(fetch(sys.argv[2:] or None), indent=1))
    sys.exit(0)

if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "fit":
    rep = run()
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(rep, indent=1, default=str))
    print(json.dumps(rep, indent=1, default=str))
