"""
Conference-title and CFP clinch/elimination math from the CFBD schedule (pick factors #26 and #57).
Deterministic, no LLM, cache only (pick_brief.raw("games")).

Every FBS conference sends its top 2 by conference record to the title game (no divisions since 2024).
Tiebreakers aren't modeled, so the math is conservative: a rival who can only TIE counts against.

  clinched    at most 1 conference rival can still reach this team's current conference wins:
              a title-game spot is locked with zero more wins
  eliminated  2+ rivals already have more conference wins than this team's best case
  alive       anything else

CFP (12 teams in 2026: the 5 best conference champions + 7 at-large) is approximated, never seeded:
a team is out of the CFP when it's out of its title game AND (a Group of 6 team, or 3+ losses).
Independents (Notre Dame) are out at 3+ losses. Nothing here says anyone is IN the CFP.
"""

from __future__ import annotations

import collections
import datetime as dt

import cfbd

P4 = {"SEC", "Big Ten", "Big 12", "ACC"}


def _when(g: dict) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(str(g.get("startDate")).replace("Z", "+00:00"))
    except ValueError:
        return None


def _done(g: dict, before: dt.datetime | None) -> bool:
    if g.get("homePoints") is None or g.get("awayPoints") is None:
        return False
    w = _when(g)
    return not (before and w and w >= before)


def table(games: list[dict], before: dt.datetime | None = None) -> dict:
    """{norm team: {team, conf, w, l, rem, losses, rem_reg}} for FBS teams in a conference, as of `before`
    (a game counts as played only if it started before then). rem = conference games still to play;
    rem_reg = all regular-season games still to play."""
    t: dict[str, dict] = {}

    def row(name, conf):
        k = cfbd.norm(name)
        if k not in t:
            t[k] = {"team": name, "conf": conf, "w": 0, "l": 0, "rem": 0, "losses": 0, "rem_reg": 0}
        return t[k]
    for g in games or []:
        if g.get("seasonType", "regular") != "regular":
            continue
        sides = (("home", "away"), ("away", "home"))
        for me, op in sides:
            if g.get(f"{me}Classification") != "fbs":
                continue
            conf = g.get(f"{me}Conference")
            r = row(g[f"{me}Team"], conf if conf and conf != "FBS Independents" else None)
            if not _done(g, before):
                r["rem_reg"] += 1
                if g.get("conferenceGame") and r["conf"]:
                    r["rem"] += 1
                continue
            won = g[f"{me}Points"] > g[f"{op}Points"]
            r["losses"] += not won
            if g.get("conferenceGame") and r["conf"]:
                r["w" if won else "l"] += 1
    return t


def status(team: str, games: list[dict], before: dt.datetime | None = None, tbl: dict | None = None) -> dict | None:
    """{team, conf, w, l, rem, losses, last_game, title: clinched|eliminated|alive|None, cfp: out|alive}."""
    tbl = tbl if tbl is not None else table(games, before)
    me = tbl.get(cfbd.norm(team))
    if not me:
        return None
    out = {**me, "title": None}
    if me["conf"]:
        rivals = [r for k, r in tbl.items() if r["conf"] == me["conf"] and r is not me]
        can_reach = sum(1 for r in rivals if r["w"] + r["rem"] >= me["w"])
        above_best = sum(1 for r in rivals if r["w"] > me["w"] + me["rem"])
        out["title"] = "clinched" if can_reach <= 1 else "eliminated" if above_best >= 2 else "alive"
    title_out = out["title"] in ("eliminated", None)
    g6 = bool(me["conf"]) and me["conf"] not in P4
    out["cfp"] = "out" if title_out and (g6 or me["losses"] >= 3) else "alive"
    out["last_game"] = me["rem_reg"] <= 1  # this game is the last regular-season game left
    return out


def line(s: dict) -> str:
    """'Indiana (7-0 in the Big Ten, 1 conference game left) has clinched a Big Ten title-game spot'."""
    rec = f"{s['w']}-{s['l']} in the {s['conf']}" if s["conf"] else f"{s['losses']} losses"
    left = f", {s['rem']} conference game{'s' * (s['rem'] != 1)} left" if s["conf"] else ""
    what = {"clinched": f"has clinched a {s['conf']} title-game spot",
            "eliminated": f"is out of the {s['conf']} title race",
            "alive": f"is still alive for the {s['conf']} title game"}.get(s["title"], "is an independent")
    cfp = "; out of the CFP picture" if s["cfp"] == "out" else ""
    return f"{s['team']} ({rec}{left}) {what}{cfp}"


def summary(games: list[dict], before: dt.datetime | None = None) -> dict[str, collections.Counter]:
    """conf -> Counter of title statuses (for the factor test and a sanity print)."""
    tbl = table(games, before)
    out: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    for k, r in tbl.items():
        if r["conf"]:
            out[r["conf"]][status(r["team"], games, before, tbl)["title"]] += 1
    return out


# =============================================================================================================
# conference races with head-to-head (plan phase 5): race(conf, games) over one conference's games
# =============================================================================================================
# A game here is the compact shape the fact store keeps: {id, start, home, away, home_pts, away_pts} (no points =
# still to play). Top 2 by conference winning percentage make the title game. Only head-to-head is modeled; a tie
# it can't settle says the conference's further tiebreakers apply. Rivals' best cases are taken one at a time (two
# rivals who still play each other can't both win it), so "clinched" and "eliminated" stay conservative.

from fractions import Fraction  # noqa: E402

DIVISION_CONFS = {"Sun Belt"}  # East/West division winners meet (2026): no top-2 math here


def _played(g: dict) -> bool:
    return g.get("home_pts") is not None and g.get("away_pts") is not None


def _start(g: dict) -> str:
    return str(g.get("start") or "")


def _base(games: list[dict]) -> tuple[dict, dict, list[dict]]:
    """({team: [w, l]}, {(winner, loser): n}, remaining games soonest first)."""
    rec: dict[str, list[int]] = {}
    beat: dict[tuple[str, str], int] = collections.Counter()
    left = []
    for g in games:
        for t in (g["home"], g["away"]):
            rec.setdefault(t, [0, 0])
        if not _played(g):
            left.append(g)
            continue
        hw = g["home_pts"] > g["away_pts"]
        w, lo = (g["home"], g["away"]) if hw else (g["away"], g["home"])
        rec[w][0] += 1
        rec[lo][1] += 1
        beat[(w, lo)] += 1
    return rec, beat, sorted(left, key=_start)


def _pct(w: int, l: int) -> Fraction:
    return Fraction(w, w + l) if w + l else Fraction(0)


def _apply(rec, beat, left, fixed: dict[str, str], rest: str, me: str | None = None):
    """Records + H2H after `fixed` results ({game id: winner}); every other remaining game goes 'rest':
    'me_loses' (me loses its unfixed games, everyone else wins theirs = each rival's best case),
    'me_wins' (me wins its unfixed games, everyone else loses theirs = each rival's worst case)."""
    r = {t: list(v) for t, v in rec.items()}
    b = collections.Counter(beat)
    for g in left:
        h, a = g["home"], g["away"]
        if g["id"] in fixed:
            w = fixed[g["id"]]
            lo = a if w == h else h
            r[w][0] += 1
            r[lo][1] += 1
            b[(w, lo)] += 1
            continue
        if me in (h, a):
            other = a if me == h else h
            if rest == "me_loses":
                r[other][0] += 1
                r[me][1] += 1
                b[(other, me)] += 1
            else:
                r[me][0] += 1
                r[other][1] += 1
                b[(me, other)] += 1
            continue
        # two rivals: count it as a win for both (best case) or a loss for both (worst case), one at a time
        for t in (h, a):
            r[t][0 if rest == "me_loses" else 1] += 1
    return r, b


def _holds(b, x: str, y: str) -> bool:
    return b[(x, y)] > b[(y, x)]


MAX_WORLDS_GAMES = 12  # exhaustive check over at most 2**12 outcomes; past that, no clinch/control claim at all


def _threats(me: str, r, b, teams) -> int:
    """Teams that can finish ahead of me: strictly better, or tied where head-to-head doesn't put me first
    (a 2-way tie needs me's H2H win; a 3+-way tie needs me to have beaten every other team in it)."""
    mine = _pct(*r[me])
    above = [t for t in teams if t != me and _pct(*r[t]) > mine]
    tied = [t for t in teams if t != me and _pct(*r[t]) == mine]
    if not tied or all(_holds(b, me, t) for t in tied):
        return len(above)
    return len(above) + len(tied)


def _top2_locked(me: str, rec, beat, left, fixed) -> bool:
    """me finishes top 2 in every world consistent with `fixed`: every outcome of the remaining games that
    involve me or a team that could still reach me's worst case (multi-team ties included)."""
    import itertools
    r0, b0 = {t: list(v) for t, v in rec.items()}, collections.Counter(beat)
    open_ = []
    for g in left:
        if g["id"] in fixed:
            w = fixed[g["id"]]
            lo = g["away"] if w == g["home"] else g["home"]
            r0[w][0] += 1
            r0[lo][1] += 1
            b0[(w, lo)] += 1
        else:
            open_.append(g)
    mine = [g for g in open_ if me in (g["home"], g["away"])]
    worst = _pct(r0[me][0], r0[me][1] + len(mine))
    rem = collections.Counter(t for g in open_ for t in (g["home"], g["away"]))
    rivals = [t for t in r0 if t != me and _pct(r0[t][0] + rem[t], r0[t][1]) >= worst]
    games = [g for g in open_ if me in (g["home"], g["away"]) or g["home"] in rivals or g["away"] in rivals]
    if len(games) > MAX_WORLDS_GAMES:
        return False
    teams = rivals + [me]
    for outcome in itertools.product((0, 1), repeat=len(games)):
        r = {t: list(r0[t]) for t in teams}
        b = collections.Counter({k: v for k, v in b0.items() if me in k})
        for g, o in zip(games, outcome):
            w, lo = (g["home"], g["away"]) if o else (g["away"], g["home"])
            if w in r:
                r[w][0] += 1
            if lo in r:
                r[lo][1] += 1
            if me in (w, lo):
                b[(w, lo)] += 1
        if _threats(me, r, b, teams) > 1:
            return False
    return True


def _out(me: str, rec, beat, left, fixed) -> bool:
    """2+ rivals finish strictly ahead of me's best case in every world consistent with `fixed`."""
    r, b = _apply(rec, beat, left, fixed, "me_wins", me)
    mine = _pct(*r[me])
    return sum(1 for t, (w, l) in r.items() if t != me and _pct(w, l) > mine) >= 2


def _order(teams: list[str], rec, beat, conf: str) -> tuple[list[str], list[dict]]:
    """Teams by conference pct; ties settled by head-to-head only (a 2-way meeting, or one team that beat every
    other team in the tie). Returns (order, ties)."""
    by: dict[Fraction, list[str]] = collections.defaultdict(list)
    for t in teams:
        by[_pct(*rec[t])].append(t)
    order, ties = [], []
    for p in sorted(by, reverse=True):
        grp = sorted(by[p], key=lambda t: (-rec[t][0], t))
        if len(grp) == 1 or rec[grp[0]][0] + rec[grp[0]][1] == 0:
            order += grp
            continue
        tie = {"teams": list(grp), "settled_by": None, "order": list(grp), "record": f"{rec[grp[0]][0]}-{rec[grp[0]][1]}"}
        top = [t for t in grp if all(_holds(beat, t, o) for o in grp if o != t)]
        if top:
            rest = [t for t in grp if t != top[0]]
            tie.update(settled_by="h2h", order=[top[0]] + rest)
            if len(grp) == 2:
                tie["note"] = f"{top[0]} holds the head-to-head win over {rest[0]}"
            else:
                tie["note"] = (f"{top[0]} beat {', '.join(rest[:-1])} and {rest[-1]} head-to-head; behind it the "
                               f"{conf}'s further tiebreakers apply")
        elif len(grp) == 2:
            a, c = grp
            met = beat[(a, c)] + beat[(c, a)]
            tie["note"] = (f"{a} and {c} split their meetings" if met else f"{a} and {c} didn't play") + \
                f", so the {conf}'s further tiebreakers apply"
        else:
            tie["note"] = (f"{', '.join(grp[:-1])} and {grp[-1]} are tied and head-to-head doesn't settle it, so the "
                           f"{conf}'s further tiebreakers apply")
        ties.append(tie)
        order += tie["order"]
    return order, ties


def race(conf: str, games: list[dict], now: dt.datetime | None = None, as_of: str = "",
         source: str = "CFBD games + live finals") -> dict:
    """The conference title race: standings with head-to-head, each team's status (clinched / alive / eliminated
    for a top-2 spot), who controls its own path (win out = in), clinches-with-a-win and out-with-a-loss for the
    next game, the rival losses that would clinch it with a win, remaining games, and facts for every record."""
    now = now or dt.datetime.now(dt.timezone.utc)
    rec, beat, left = _base(games or [])
    teams = list(rec)
    order, ties = _order(teams, rec, beat, conf)
    divisional = conf in DIVISION_CONFS
    rows = []
    for t in order:
        mine = [g for g in left if t in (g["home"], g["away"])]
        rem = [{"id": g["id"], "opp": g["away"] if g["home"] == t else g["home"], "home": g["home"] == t,
                "start": _start(g)} for g in mine]
        r = {"team": t, "w": rec[t][0], "l": rec[t][1], "rem": len(rem), "rem_games": rem,
             "next": rem[0] if rem else None, "status": None, "controls": False, "clinch_with_win": False,
             "clinch_with": [], "out_with_loss": False}
        if not divisional:
            locked = _top2_locked(t, rec, beat, left, {})
            out = not locked and _out(t, rec, beat, left, {})
            r["status"] = "clinched" if locked else "eliminated" if out else "alive"
            if r["status"] == "alive":
                r["controls"] = _top2_locked(t, rec, beat, left, {g["id"]: t for g in mine})
                nxt = mine[0] if mine else None
                if nxt:
                    opp = nxt["away"] if nxt["home"] == t else nxt["home"]
                    win = {nxt["id"]: t}
                    r["clinch_with_win"] = _top2_locked(t, rec, beat, left, win)
                    r["out_with_loss"] = _out(t, rec, beat, left, {nxt["id"]: opp})
                    if not r["clinch_with_win"]:
                        for o in order:
                            if o in (t, opp):
                                continue
                            og = next((g for g in left if o in (g["home"], g["away"]) and g["id"] != nxt["id"]), None)
                            if not og or t in (og["home"], og["away"]):
                                continue
                            oo = og["away"] if og["home"] == o else og["home"]
                            if _top2_locked(t, rec, beat, left, {**win, og["id"]: oo}):
                                r["clinch_with"].append(o)
        rows.append(r)
    live = [r for r in rows if r["status"] != "eliminated"]
    leader = rows[0]["team"] if rows else None
    facts = []
    for r in rows:
        left_txt = f" with {r['rem']} conference game{'s' * (r['rem'] != 1)} left" if r["rem"] else ", conference play done"
        facts.append({"id": f"conf_race.{cfbd.norm(conf)}.{cfbd.norm(r['team'])}", "kind": "conf_race",
                      "subject": r["team"], "value": f"{r['w']}-{r['l']}",
                      "text": f"{r['team']} is {r['w']}-{r['l']} in {conf} play{left_txt}", "source": source,
                      "as_of": as_of or now.isoformat(timespec="seconds")})
    return {"conf": conf, "divisional": divisional, "standings": rows, "ties": ties, "leader": leader,
            "chasers": [r["team"] for r in live if r["team"] != leader],
            "controls": [r["team"] for r in rows if r["controls"] or r["status"] == "clinched"],
            "beat": {f"{w}>{lo}": n for (w, lo), n in beat.items()},
            "facts": facts, "as_of": as_of or now.isoformat(timespec="seconds")}
