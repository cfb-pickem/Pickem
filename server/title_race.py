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
