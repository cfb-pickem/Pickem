"""
Pick strategy (league game theory) for "should I go against the field / how do I
catch him / what's my strategy" texts, computed from the standings and the
league rules so the model only phrases it.

The league: top 4 after week 15 make the playoffs and points RESET there, so the
regular season is about getting into the top 4 (seeding and the playoff
tiebreaker are the only other things regular-season points buy). Posture:

  PROTECT  in the top 4 with a 2+ point cushion over 5th: low variance, side
           with the best numbers/consensus, don't fade the field.
  BUBBLE   within 1 of the cut line either way: take your real edges, and be
           different from the members right around the cut only where you have one.
  CHASE    3+ behind the cut line: variance is your friend, fade the consensus
           (you only gain ground when you're right and they're wrong).
  OUT      can't reach the cut line even winning out.

Found by the genius exam (2026-11-16): Picky told the 2nd-place member to start
taking risks to catch 1st, ignoring that playoff points reset.
"""

from __future__ import annotations

import collections
import re

PLAYOFF_SPOTS = 4
LAST_WEEK = 15
GAMES_PER_WEEK = 4

ASK = re.compile(
    r"\b(strateg\w*|game theory|against the field|go(ing)? contrarian|contrarian|fade (the )?(field|league|consensus|"
    r"crowd|chalk)|should i (chase|protect)|chase (him|first|1st|the lead|them)|catch (him|up|them|chuck|[A-Z]\w+)|protect (my|the) (lead|spot)|how should i (pick|play)|"
    r"what should i do|hail mar(y|ies)|play it safe|take (more )?risks?|ride the chalk|go against)\b", re.I)


def wants(q: str) -> bool:
    return bool(ASK.search(q or ""))


def note(ctx: dict, q: str) -> str:
    if not wants(q):
        return ""
    st = ctx.get("standings_ytd") or []
    me = (ctx.get("you") or {}).get("team_name")
    wk = ctx.get("current_week")
    row = next((r for r in st if r.get("team") == me), None)
    if not st or not row or wk is None or int(wk) > LAST_WEEK or len(st) <= PLAYOFF_SPOTS:
        return ""
    left_now = sum(1 for g in ctx.get("games_this_week") or [] if not g.get("winner") and not g.get("tiebreaker"))
    left = left_now + GAMES_PER_WEEK * (LAST_WEEK - int(wk))
    pts = sorted((r["w"] for r in st), reverse=True)
    cut, fifth, top = pts[PLAYOFF_SPOTS - 1], pts[PLAYOFF_SPOTS], pts[0]
    w = row["w"]
    inside = w >= cut and sum(1 for r in st if r["w"] > w) < PLAYOFF_SPOTS
    cushion = w - fifth if inside else None
    behind = cut - w if not inside else 0
    if not inside and behind > left:
        posture = "OUT: can't reach the cut line even winning out. Play for pride: swing big, fade everyone."
    elif inside and cushion >= 2:
        posture = ("PROTECT: you're in a playoff spot with a cushion. Low variance: side with the best numbers and the "
                   "consensus, don't fade the field. Catching 1st only buys seeding and the playoff tiebreaker, "
                   "because playoff points reset.")
    elif (inside and cushion <= 1) or behind <= 1:
        posture = ("BUBBLE: right on the cut line. Take your real edges; be different from the members right around "
                   "4th/5th only where you have one. No hail marys needed yet.")
    else:
        posture = ("CHASE: you need ground. Variance is your friend: fade the consensus, since you only gain when "
                   "you're right and they're wrong. Expect to lose some more doing it.")
    lead = f"{top - w} behind 1st" if w < top else ("tied for 1st" if pts.count(top) > 1 else f"{w - pts[1]} ahead of 2nd")
    where = (f"in a playoff spot, {cushion} ahead of 5th place ({fifth})" if inside
             else f"{behind} behind the cut line (4th place has {cut})")
    return (f"PICK STRATEGY (computed for {me}, {row['w']}-{row['l']}, {_ord(row.get('rank'))}): {lead}; {where}; "
            f"about {left} picks left in the regular season (weeks through {LAST_WEEK}).\n"
            f"League rules that decide it: top {PLAYOFF_SPOTS} make the playoffs and points RESET there; regular-season "
            f"points are only playoff tiebreaker #2. A tie for 4th goes to the conference-championship tiebreaker picks first.\n"
            f"Recommendation: {posture}")


def answer(ctx: dict, q: str, facts=None) -> str | None:
    """The strategy as a finished text (router fact lane 'strategy'): the sender's spot, the gap that decides
    the posture, picks left, and the move, all from note()'s numbers. When the move is to fade the field
    (chase, out, dead last), it names the consensus side to fade (consensus())."""
    n = note(ctx, q)
    if not n:
        return None
    m = re.search(r"computed for (.+?), (\d+-\d+), (\w+)\): (.+?); (.+?); about (\d+) picks left", n)
    if not m:
        return None
    rec, place, where, left = m.group(2), m.group(3), m.group(5), m.group(6)
    st = ctx.get("standings_ytd") or []
    me = next((r for r in st if r.get("team") == m.group(1)), None)
    last = bool(me and len(st) > 1 and me["w"] == min(r["w"] for r in st)
                and sum(1 for r in st if r["w"] == me["w"]) < len(st))
    head = (f"You're dead last at {rec} ({place}), {where}, with about {left} picks left." if last
            else f"You're {rec}, {place}, {where}, with about {left} picks left.")
    post = n.rsplit("Recommendation: ", 1)[-1].split(":", 1)[0]
    body = {
        "PROTECT": "Protect it: side with the best numbers and the consensus, don't fade the field. Catching 1st only "
                   "buys seeding, because points reset in the playoffs.",
        "BUBBLE": "No hail marys yet: take your real edges, and only go against the guys right around 4th where you've "
                  "got one. Tiebreakers matter on the bubble, and the conference title game picks come first.",
        "CHASE": "You're alive, but it's a long shot. Max variance: fade the consensus every week, because you only gain "
                 "ground when you're right and they're wrong.",
        "OUT": "Even winning out doesn't get you to 4th, so play for pride: swing big and fade everybody.",
    }.get(post)
    if not body:
        return None
    if last and post in ("CHASE", "OUT"):
        body = ("Dead last means max variance: matching the field can't move you up, so fade the consensus every "
                "week and live with the misses." + ("" if post == "CHASE" else " Even winning out doesn't get you to "
                                                   "4th, so it's for pride."))
    if post in ("CHASE", "OUT"):
        body += " " + consensus(ctx, facts)
    return f"{head} {body}"


def consensus(ctx: dict, facts=None) -> str:
    """Where the field is on this week's pool games, as one sentence. Revealed games (locked: the site shows every
    pick) use the real picks in ctx['revealed_picks_this_week']; unrevealed games use only the league's lean from
    past graded picks (game card 'crowd'), never anyone's pick for this week, the sender's included."""
    import pick_brief
    games = [g for g in ctx.get("games_this_week") or [] if not g.get("tiebreaker") and not g.get("winner")
             and g.get("line") is not None]
    revealed = set(ctx.get("revealed_game_ids") or [])
    picks = ctx.get("revealed_picks_this_week") or {}
    locked, habits = [], []
    for g in games:
        if g["id"] in revealed:
            sides = [p["pick"] for plist in picks.values() for p in plist if p.get("game_id") == g["id"] and p.get("pick")]
            if sides:
                side, k = collections.Counter(sides).most_common(1)[0]
                if k * 2 > len(sides):
                    locked.append((k / len(sides), f"{side}, {k} of {len(sides)} picks, in {g['away']} at {g['home']}"))
            continue
        if facts is None:
            import sitfacts
            facts = sitfacts.live()
        c = facts.crowd(ctx.get("current_week"), g["id"])
        if c and c.get("side") in (g["away"], g["home"]) and c.get("of"):
            fade = g["home"] if c["side"] == g["away"] else g["away"]
            habits.append((c["n"] / c["of"], f"{c['side']} in {g['away']} at {g['home']} ({c['n']} of {c['of']} "
                                             f"members with a habit), so the fade is {pick_brief.side_label(fade, g)}"))
    if locked and not habits:
        locked.sort(key=lambda x: -x[0])
        return ("Picks are locked this week, so there's nothing to fade until next week. The field went "
                + "; ".join(t for _, t in locked[:2]) + ".")
    if habits:
        habits.sort(key=lambda x: -x[0])
        return ("The field's habits (past graded picks, not this week's) point to "
                + "; and ".join(t for _, t in habits[:2]) + ".")
    return "I don't have a read on where the field leans this week, so go against the chalk only where you have a reason."


def _ord(n) -> str:
    try:
        n = int(n)
    except (TypeError, ValueError):
        return "?"
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"
