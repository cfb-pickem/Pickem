#!/usr/bin/env python3
"""Picky's own CFP bracket: BCS-style rankings + the committed CFP format (cfp_rules.json), all in code.

Rankings (the classic BCS formula, the Harris poll gone so AP stands in):
  score = 1/3 AP share + 1/3 Coaches share + 1/3 computer share
  poll share      poll points / max possible points (25 x voters; voters = first-place votes cast). A cached poll
                  without points (older polls.json) counts ranks as points: 25 for No. 1 ... 1 for No. 25.
  computer share  each computer (SP+, ESPN FPI, Elo, SRS, from the CFBD cache) ranks the FBS teams; a team gets
                  (26 - rank) / 25 in its top 25, else 0; the share is the average over the computers that are there.
Bracket (cfp_rules.json): the four power-conference champions, the top Group of Six team and Notre Dame in the top 12
are in; at-larges fill the rest by BCS rank; seeding straight by rank, auto bids outside the top 12 at the bottom.
Before the title games a conference's champion is its projected one (the conference race leader, ties the race
can't settle broken by BCS rank); after, the title game winner.

The model never touches it: router's cfp_bracket lane sends text() verbatim and the voice line can't carry a number.

  bcs.py show        # print the bracket text (recomputes, writes cache/bcs/<season>-w<week>.json)
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import cfbd  # noqa: E402

CACHE_DIR = Path(os.environ.get("PICKY_BCS_DIR", HERE / "cache" / "bcs"))
MAX_AGE_S = 900  # the chat path reuses a computed bracket this long (polls 1 h TTL, finals every few minutes)
COMPUTERS = ("SP+", "FPI", "Elo", "SRS")
POLLS = ("AP", "Coaches")
TOP = 25


# =============================================================================================================
# the math (pure)
# =============================================================================================================

def poll_shares(poll: dict | None) -> dict[str, float]:
    """{team: points / max points} for one poll ({ranks: [{rank, team, points?, first?}], others: [{team, points}]})."""
    ranks = (poll or {}).get("ranks") or []
    if not ranks:
        return {}
    if all(r.get("points") is not None for r in ranks):
        pts = {r["team"]: float(r["points"]) for r in ranks}
        for o in (poll or {}).get("others") or []:
            if o.get("team") and o.get("points") is not None:
                pts.setdefault(o["team"], float(o["points"]))
        voters = sum(int(r.get("first") or 0) for r in ranks)
        voters = max(voters, -(-int(max(pts.values())) // TOP))  # never below what the top vote total implies
    else:  # no points cached: ranks as points
        pts = {r["team"]: float(TOP + 1 - r["rank"]) for r in ranks if r.get("rank") and r["rank"] <= TOP}
        voters = 1
    mx = TOP * voters
    return {t: p / mx for t, p in pts.items()}


def computer_ranks(ratings: dict[str, float], fbs: set[str] | None = None) -> dict[str, int]:
    """{team: rank} by rating, best first (FBS only when fbs is given; ties share the better rank)."""
    rows = sorted(((t, v) for t, v in ratings.items() if v is not None and (fbs is None or t in fbs)),
                  key=lambda x: -x[1])
    out, prev, prev_rank = {}, None, 0
    for i, (t, v) in enumerate(rows, 1):
        prev_rank = prev_rank if v == prev else i
        out[t], prev = prev_rank, v
    return out


def computer_shares(computers: dict[str, dict[str, float]], fbs: set[str] | None = None) -> dict[str, float]:
    """{team: average of (26 - rank) / 25 (0 outside the top 25)} over the computers that have ratings."""
    have = {k: computer_ranks(v, fbs) for k, v in computers.items() if v}
    if not have:
        return {}
    teams = set().union(*[set(r) for r in have.values()])
    return {t: sum((TOP + 1 - r[t]) / TOP if r.get(t, 99) <= TOP else 0.0 for r in have.values()) / len(have)
            for t in teams}


def rankings(polls_d: dict, computers: dict[str, dict[str, float]], fbs: set[str] | None = None) -> list[dict]:
    """[{rank, team, score, ap, coaches, computers}] best first; teams with no share anywhere are left out."""
    ap, co = poll_shares(polls_d.get("AP")), poll_shares(polls_d.get("Coaches"))
    cp = computer_shares(computers, fbs)
    teams = (set(ap) | set(co) | set(cp))
    if fbs:
        teams &= fbs
    rows = [{"team": t, "ap": ap.get(t, 0.0), "coaches": co.get(t, 0.0), "computers": cp.get(t, 0.0)} for t in teams]
    for r in rows:
        r["score"] = (r["ap"] + r["coaches"] + r["computers"]) / 3
    rows = [r for r in rows if r["score"] > 0]
    rows.sort(key=lambda r: (-r["score"], -r["computers"], r["team"]))
    for i, r in enumerate(rows, 1):
        r["rank"] = i
        for k in ("score", "ap", "coaches", "computers"):
            r[k] = round(r[k], 4)
    return rows


def bracket(order: list[str], conf_of: dict[str, str], champs: dict[str, str], rules: dict) -> dict:
    """The field from a ranking (best first). champs: {power conference: its (projected) champion}.
    Returns {seeds: [(seed, team)], byes: [(seed, team)], first_round: [(hi, lo)], auto: {team: why}, at_large, first_out}."""
    rank = {t: i for i, t in enumerate(order, 1)}
    ab, n = rules["auto_bids"], rules["teams"]
    auto: dict[str, str] = {}
    for conf in ab["power4_champions"]:
        if champs.get(conf):
            auto.setdefault(champs[conf], conf)
    g6c = set(ab["group_of_six"]["conferences"])
    g6 = next((t for t in order if conf_of.get(t) in g6c), None)
    if g6:
        auto.setdefault(g6, "G6")
    nd = ab.get("notre_dame") or {}
    if nd and rank.get(nd.get("team"), 999) <= nd.get("max_rank", 12):
        auto.setdefault(nd["team"], f"top {nd.get('max_rank', 12)}")
    at_large = [t for t in order if t not in auto][:max(0, n - len(auto))]
    field = list(auto) + at_large
    top = sorted((t for t in field if rank.get(t, 999) <= n), key=lambda t: rank[t])
    low = sorted((t for t in field if rank.get(t, 999) > n), key=lambda t: (rank.get(t, 999), t))
    seeds = list(enumerate(top + low, 1))
    by = dict(seeds)
    return {"seeds": seeds, "byes": [(s, by[s]) for s in rules["byes"] if s in by],
            "first_round": [(a, b) for a, b in rules["first_round"]], "auto": auto, "at_large": at_large,
            "first_out": [t for t in order if t not in field][:4], "rank": rank}


def text(b: dict, week, projected: bool = True) -> str:
    """The group-text bracket: plain lines, no markdown, every seed from code."""
    by = dict(b["seeds"])
    L = [f"My bracket (BCS formula, wk {week}):",
         "Byes: " + ", ".join(f"{s} {t}" for s, t in b["byes"])]
    L += [f"{a} {by[a]} vs {c} {by[c]}" for a, c in b["first_round"] if a in by and c in by]
    if b["first_out"]:
        L.append(f"First {len(b['first_out'])} out: " + ", ".join(b["first_out"]))
    champs = [f"{t} ({w})" for t, w in b["auto"].items() if w not in ("G6",) and not w.startswith("top ")]
    if champs:
        L.append(("Projected champs: " if projected else "Champs: ") + ", ".join(champs))
    return "\n".join(L)


def facts(b: dict, week, as_of: str) -> list[dict]:
    """One fact per number the text carries (factstore.unsupported checks the lane's answer against these)."""
    src = "Picky's BCS formula (AP, Coaches, SP+, FPI, Elo, SRS) + cfp_rules.json"
    out = [{"id": "bcs.week", "kind": "computed", "subject": "bracket", "value": week,
            "text": f"Picky's bracket uses the week {week} polls", "source": src, "as_of": as_of},
           {"id": "bcs.first_out", "kind": "computed", "subject": "bracket", "value": len(b["first_out"]),
            "text": f"first {len(b['first_out'])} out", "source": src, "as_of": as_of}]
    out += [{"id": f"bcs.seed.{s}", "kind": "computed", "subject": t, "value": s,
             "text": f"{t} is the No. {s} seed in Picky's BCS bracket", "source": src, "as_of": as_of}
            for s, t in b["seeds"]]
    return out


# =============================================================================================================
# inputs (the CFBD cache, the checked polls, the conference races)
# =============================================================================================================

def _ds(db_path, *names) -> dict:
    p = Path(db_path or cfbd.DB_PATH)
    if not p.exists():
        return {}
    db = sqlite3.connect(p)
    try:
        q = f"select name, json from datasets where name in ({','.join('?' * len(names))})"
        return {n: json.loads(j) for n, j in db.execute(q, names).fetchall()}
    finally:
        db.close()


def computers_from(ds: dict) -> dict[str, dict[str, float]]:
    """{computer: {team: rating}} from cached CFBD datasets: sp, fpi, srs, and Elo = each team's newest Elo in games."""
    out = {"SP+": {r["team"]: r.get("rating") for r in ds.get("sp") or []
                   if r.get("team") and r["team"] != "nationalAverages" and r.get("rating") is not None},
           "FPI": {r["team"]: r.get("fpi") for r in ds.get("fpi") or [] if r.get("team") and r.get("fpi") is not None},
           "SRS": {r["team"]: r.get("rating") for r in ds.get("srs") or [] if r.get("team") and r.get("rating") is not None},
           "Elo": {}}
    # the newest POSTGAME Elo of a completed game; an upcoming game's pregame Elo can lag the last result
    # (Alabama's wk 13 pregame 1979 vs its wk 12 postgame 2002), so pregame only for a team with no final yet
    newest: dict[str, tuple[bool, str, float]] = {}
    for g in ds.get("games") or []:
        for side in ("home", "away"):
            t = g.get(f"{side}Team")
            post = g.get(f"{side}PostgameElo") if g.get("completed") else None
            elo = post if post is not None else g.get(f"{side}PregameElo")
            key = (post is not None, str(g.get("startDate") or ""))
            if t and elo is not None and (t not in newest or key > newest[t][:2]):
                newest[t] = (*key, float(elo))
    out["Elo"] = {t: v for t, (_, _, v) in newest.items()}
    return out


def title_game_winners(ds: dict, confs) -> dict[str, str]:
    """{conference: winner} for finished conference title games (CFBD notes '<conf> Championship')."""
    scores = {str(g["id"]): (g.get("homeScore"), g.get("awayScore")) for g in ds.get("lines") or []}
    out = {}
    for g in ds.get("games") or []:
        conf = g.get("homeConference")
        if conf not in confs or g.get("awayConference") != conf or "championship" not in str(g.get("notes") or "").lower():
            continue
        hp, ap = g.get("homePoints"), g.get("awayPoints")
        if hp is None or ap is None:
            hp, ap = scores.get(str(g["id"]), (None, None))
        if hp is not None and ap is not None and hp != ap:
            out[conf] = g["homeTeam"] if hp > ap else g["awayTeam"]
    return out


def projected_champs(confs, rank: dict[str, int], race=None) -> dict[str, str]:
    """{conference: race leader}; a tie the race can't settle goes to the better BCS rank."""
    import factstore
    race = race or factstore.conf_race
    out = {}
    for conf in confs:
        r = race(conf)
        if not r or not r.get("leader"):
            continue
        lead = r["leader"]
        tie = next((t for t in r.get("ties") or [] if lead in t["teams"] and t.get("settled_by") is None), None)
        out[conf] = min(tie["teams"], key=lambda t: rank.get(t, 999)) if tie else lead
    return out


def poll_week(polls_d: dict, ds: dict) -> int | None:
    """The polls' week (ESPN's 'Week 13'), else the week after the last completed CFBD week."""
    for k in POLLS:
        w = (polls_d.get(k) or {}).get("week")
        if w:
            return int(w)
    done = [g.get("week") for g in ds.get("games") or [] if g.get("completed") and g.get("week")]
    return max(done) + 1 if done else None


def compute(polls_d: dict | None = None, db_path=None, rules: dict | None = None, race=None) -> dict | None:
    """Everything from the live caches: {season, week, rankings, bracket, text, facts, gaps}. None without polls."""
    import cfp_rules
    if polls_d is None:
        import polls
        polls_d = {k: polls.serve(k) for k in POLLS}
    polls_d = {k: v for k, v in (polls_d or {}).items() if v}
    if not polls_d:
        return None
    rules = rules or cfp_rules.load()
    ds = _ds(db_path, "records", "sp", "fpi", "srs", "games", "lines")
    fbs = {r["team"] for r in ds.get("records") or [] if r.get("classification") == "fbs"} or None
    conf_of = {r["team"]: r.get("conference") for r in ds.get("records") or []}
    comps = computers_from(ds)
    rows = rankings(polls_d, comps, fbs)
    if not rows:
        return None
    order = [r["team"] for r in rows]
    rank = {t: i for i, t in enumerate(order, 1)}
    p4 = rules["auto_bids"]["power4_champions"]
    won = title_game_winners(ds, set(p4))
    champs = {**projected_champs([c for c in p4 if c not in won], rank, race), **won}
    b = bracket(order, conf_of, champs, rules)
    week = poll_week(polls_d, ds)
    as_of = max((p.get("date") or "") for p in polls_d.values())
    gaps = [f"no {k} poll" for k in POLLS if k not in polls_d]
    gaps += [f"no {k} poll points (ranks used)" for k, p in polls_d.items()
             if any(r.get("points") is None for r in p.get("ranks") or [])]
    gaps += [f"no {k} ratings" for k in COMPUTERS if not comps.get(k)]
    projected = len(won) < len(p4)
    return {"season": cfbd.season_year(), "week": week, "as_of": as_of, "computed_ts": time.time(),
            "rankings": rows[:40], "champs": champs, "title_games": won, "gaps": gaps,
            "bracket": {k: v for k, v in b.items() if k != "rank"},
            "text": text(b, week, projected), "facts": facts(b, week, as_of)}


def _path(season, week) -> Path:
    return CACHE_DIR / f"{season}-w{week}.json"


def refresh() -> dict | None:
    """Recompute and write cache/bcs/<season>-w<week>.json (the factstore tick; new polls / finals land here)."""
    out = compute()
    if out:
        p = _path(out["season"], out["week"])
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(out, indent=1))
        os.replace(tmp, p)
    return out


def current(max_age: float = MAX_AGE_S) -> dict | None:
    """The newest cached bracket while it's fresh, else a recompute (written to the cache)."""
    try:
        files = sorted(CACHE_DIR.glob("*-w*.json"), key=lambda p: p.stat().st_mtime)
        if files and time.time() - files[-1].stat().st_mtime < max_age:
            d = json.loads(files[-1].read_text())
            if d.get("season") == cfbd.season_year():
                return d
    except (OSError, ValueError):
        pass
    return refresh()


def main(argv: list[str]) -> int:
    d = refresh()
    if not d:
        print("no bracket (no checked polls)")
        return 1
    print(d["text"])
    if d["gaps"]:
        print("gaps:", "; ".join(d["gaps"]))
    if "--rank" in argv:
        for r in d["rankings"][:25]:
            print(f"{r['rank']:>2} {r['team']:<20} {r['score']:.4f}  AP {r['ap']:.3f}  Coaches {r['coaches']:.3f}  "
                  f"comp {r['computers']:.3f}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
