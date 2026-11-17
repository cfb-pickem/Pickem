"""
Close-cover alerts for the league group (Charlie, 2026-11-17): Picky posts only
when a pool game's ending is worth tuning in for THE COVER. Plain code, no model.

All three must hold (cover_alert):
  (a) the final 8 minutes: 4th quarter under WINDOW_S on the clock, or OT;
  (b) the cover is within one score of flipping: |HomePts + line - AwayPts| <= CLOSE_PTS
      (the real game score doesn't matter by itself);
  (c) the flip is realistically still live (cover_live): T = the team that must
      score to flip it. T has the ball; or the other team has it but can't just
      kneel it out; and the scores T needs fit in the time left.

Posting rules: at most MAX_POSTS alert posts per game. Post 1 when a game first
meets (a)-(c). Post 2 (optional) is the one big later moment: a cover flip, or
"live again" (it went dead after post 1, then came back, e.g. a turnover), held
DEBOUNCE_S and at least SPACING_S after post 1. Games that qualify on the same
tick share one post. Never a final from here (group_posts owns finals). Quiet
hours, the group pause and dry run all apply (group_outbox.post).

Cover math is set_winner_on_final's: home covers when HomePts + line > AwayPts,
dead even goes to Away; `line` is the home adjustment (negative = home favored).

Data: the live_games poller's cache tells us which pool games are live and in
what quarter (cheap, every TICK_WARM s). Once a pool game is live in the 4th or
OT, this ticker fetches that game's ESPN summary itself every TICK_HOT s (fresh
play-by-play: possession, down & distance, yard line, timeouts used, kneels) and
uses the poller's scoreboard situation for timeouts/TV when it's fresh.

ESPN win probability is NOT used for liveness on purpose: it measures the game,
not the cover (a 13.5-point dog down 10 has ~0% to win and a live cover).

Kill switch: touch cfb-pickem-private/PICKY_COVER_ALERTS_OFF.
CLI: close_cover.py run | once [--dry-run] | replay <fixture dir> [--picks-locked]
"""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import hashlib
import json
import logging
import math
import os
import re
import sys
import time
import zoneinfo
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import group_outbox as ob  # noqa: E402

log = logging.getLogger("picky.cover")
CT = zoneinfo.ZoneInfo("America/Chicago")

WINDOW_S = 480        # final 8 minutes of the 4th (Charlie: "Max 2 post per game ... starting at 8 mins")
CLOSE_PTS = 8         # one score: a TD + 2
MAX_POSTS = 2         # alert posts per game, total
SPACING_S = 120       # post 2 at least this long after post 1
DEBOUNCE_S = 30       # a flip / live-again has to hold this long (score corrections, review)
KNEEL_S = 40          # play clock burned per kneel when the trailing side can't stop it
TUNE_S = 30           # less than this left and an alert can't get anyone to a TV
TICK_HOT = 15         # s, while a pool game is live in the 4th / OT
TICK_WARM = 60        # s, a pool game is live (earlier quarters) or kicks off soon
TICK_IDLE = 300       # s, nothing going on
EXPIRES_S = 240       # a cover alert older than this is worse than none
OFF_FILE = ob.PRIVATE / "PICKY_COVER_ALERTS_OFF"
STATE_FILE = ob.PRIVATE / "close_cover_state.json"
LOG_PATH = HERE / "logs" / "close-cover.log"


# -----------------------------------------------------------------------------
# rules (pure)
# -----------------------------------------------------------------------------


def clock_secs(clock) -> int | None:
    try:
        m, s = str(clock).split(":")[:2]
        return int(m) * 60 + int(float(s))
    except (TypeError, ValueError):
        return None


def in_window(period, clock_s) -> bool:
    """(a) 4th quarter with under 8:00 left, or overtime. 7:59 yes, 8:00 / 8:01 no, Q3 never."""
    p = int(period or 0)
    if p >= 5:
        return True
    return p == 4 and clock_s is not None and clock_s < WINDOW_S


def cover_math(line, ap, hp) -> dict | None:
    """Who covers at this score and what flips it.
    margin: points the covering side is ahead of the number; t: 'home'/'away', the side that
    must score; needs: the fewest points t has to score to flip it."""
    try:
        ln, a, h = float(line), float(ap), float(hp)
    except (TypeError, ValueError):
        return None
    m = h + ln - a
    if m > 0:      # home covers; away flips it at A + p >= H + line (dead even goes to Away)
        cov, t, needs = "home", "away", math.ceil(m)
    else:          # away covers (incl. dead even); home flips it at H + p + line > A
        cov, t, needs = "away", "home", math.floor(-m) + 1
    return {"m": m, "margin": abs(m), "cov": cov, "t": t, "needs": int(needs)}


def is_close(cm: dict | None) -> bool:
    """(b) within one score of flipping."""
    return bool(cm) and cm["needs"] <= CLOSE_PTS


def scores_needed(needs: int) -> int:
    return max(1, (int(needs) + 7) // 8)


def score_kind(needs: int) -> str:
    if needs <= 3:
        return "a field goal"
    if needs <= 6:
        return "a touchdown"
    if needs == 7:
        return "a touchdown with the extra point"
    if needs == 8:
        return "a touchdown and a 2-point try"
    return "two scores"


def cover_live(situation: dict | None, needs_points: int, clock: int | None, timeouts: int | None) -> tuple[bool, str]:
    """(c) Is a cover flip realistically still in play? From T's point of view (T = the side that
    must score). `situation` keys: has_ball 'T'/'opp'/None, receiving 'T'/'opp'/None (kickoff
    pending), down, kneeling, opp_leads (opponent ahead or tied in the real game), ot, fallback
    (only the scoreboard possession field is known). `clock`: seconds left in regulation.
    `timeouts`: T's timeouts left (None = unknown, treated as 0). Returns (live, why)."""
    s = situation or {}
    k = scores_needed(needs_points)
    if s.get("ot"):
        return (k == 1, "overtime, both sides get the ball" if k == 1 else "two scores in OT")
    if clock is None:
        return False, "no clock"
    ball = s.get("has_ball") or s.get("receiving")
    tos = int(timeouts or 0)
    if s.get("fallback"):  # no situation block: only post when T has the ball
        return (ball == "T" and (k == 1 or clock >= 180), "possession only")
    if s.get("kneeling") and ball == "opp":
        return False, "victory formation"
    if ball == "T":
        if k == 1:
            # under TUNE_S nobody gets to a TV in time: that's a final, not an alert
            return (clock >= TUNE_S, "T has the ball" if clock >= TUNE_S else "no time to tune in")
        need = 180 - 20 * tos  # score, then onside / stop, then score again
        return (clock >= need, "T has the ball, time for two" if clock >= need else "not enough time for two scores")
    if ball == "opp":
        burn = 0
        if s.get("opp_leads", True):
            down = s.get("down") or 1
            snaps = max(1, 5 - int(down))
            burn = KNEEL_S * max(0, snaps - tos) + 2 * snaps
            if clock <= burn:
                return False, "they can kneel it out"
        left = clock - burn  # what T might have once it gets the ball back
        if k == 1:
            need = 30 if needs_points <= 3 else 45
            return (left >= need, "T gets it back with time" if left >= need else "too little time after a stop")
        need = 300 - 20 * tos
        return (left >= need, "time for two scores" if left >= need else "two scores, no ball, not enough time")
    return False, "no read on who has the ball"


def cover_alert(period, clock_s, cm: dict | None, situation: dict | None, timeouts: int | None) -> tuple[bool, str]:
    """(a) and (b) and (c)."""
    if not in_window(period, clock_s):
        return False, "outside the window"
    if not is_close(cm):
        return False, "cover not close"
    sit = dict(situation or {})
    if int(period or 0) >= 5:
        sit["ot"] = True
    return cover_live(sit, cm["needs"], clock_s, timeouts)


# -----------------------------------------------------------------------------
# a game snapshot from ESPN (summary play-by-play, plus the poller's scoreboard situation)
# -----------------------------------------------------------------------------

_SKIP = {"End Period", "End of Half", "End of Game", "End of Regulation", "Timeout", "Penalty"}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower().replace("&", "and"))


def _teams(d: dict) -> dict:
    comp = ((d.get("header") or {}).get("competitions") or [{}])[0]
    out = {}
    for c in comp.get("competitors") or []:
        t = c.get("team") or {}
        out[c.get("homeAway")] = {"id": str(t.get("id") or ""), "abbr": t.get("abbreviation") or "",
                                  "loc": t.get("location") or t.get("shortDisplayName") or "",
                                  "nick": t.get("name") or ""}
    return out


def network(d: dict | None = None, sb: dict | None = None) -> str | None:
    """The TV network from ESPN's broadcasts (summary header or the poller's scoreboard). National TV
    first, then any TV, then a stream. None when ESPN doesn't say: never guess."""
    cands = []
    comp = (((d or {}).get("header") or {}).get("competitions") or [{}])[0]
    for b in comp.get("broadcasts") or []:
        name = ((b.get("media") or {}).get("shortName") or "").strip()
        if name:
            tv = ((b.get("type") or {}).get("shortName") or "").upper() == "TV"
            nat = ((b.get("market") or {}).get("type") or "").lower() == "national" or b.get("isNational")
            cands.append((0 if tv and nat else 1 if tv else 2, name))
    for name in (sb or {}).get("broadcasts") or []:
        if name:
            cands.append((1, name))
    return sorted(cands)[0][1] if cands else None


def _timeouts_used(plays: list[dict], teams: dict, period: int) -> dict:
    """Timeouts each side has used this half (3 per half; 1 per OT period), counted from the
    play-by-play ('Timeout Oregon State, clock 06:47')."""
    half = (1, 2) if period <= 2 else (3, 4) if period <= 4 else (period,)
    used = {"away": 0, "home": 0}
    keys = {side: {_norm(t["loc"]), _norm(t["abbr"]), _norm(t["nick"])} - {""} for side, t in teams.items()}
    for p in plays:
        if (p.get("type") or {}).get("text") != "Timeout" or int((p.get("period") or {}).get("number") or 0) not in half:
            continue
        m = re.match(r"\s*Timeout\s+(.+?)(?:,|$)", str(p.get("text") or ""), re.I)
        who = _norm(m.group(1)) if m else ""
        for side, ks in keys.items():
            if who and any(who == k or who.startswith(k) or k.startswith(who) for k in ks):
                used[side] += 1
                break
    return used


def snapshot(d: dict, upto: int | None = None, sb: dict | None = None, wp: bool = True) -> dict | None:
    """The live state of one game from an ESPN summary payload, as of play index `upto` (replay)
    or the latest play. sb = the poller's scoreboard row (situation timeouts / TV) when fresh."""
    import live_games as L
    teams = _teams(d)
    if "away" not in teams or "home" not in teams:
        return None
    plays = L._flat_plays(d)
    if upto is not None:
        plays = plays[:upto + 1]
    comp = ((d.get("header") or {}).get("competitions") or [{}])[0]
    st = comp.get("status") or {}
    real = [p for p in plays if (p.get("type") or {}).get("text") not in _SKIP]
    last = real[-1] if real else None
    lastp = plays[-1] if plays else None
    if upto is None:
        state = (st.get("type") or {}).get("state")
        period = int(st.get("period") or (lastp or {}).get("period", {}).get("number") or 0)
        clock = st.get("displayClock") or ((lastp or {}).get("clock") or {}).get("displayValue")
        scores = {c.get("homeAway"): c.get("score") for c in comp.get("competitors") or []}
        ap, hp = scores.get("away"), scores.get("home")
    else:
        if not lastp:
            return None
        ended = (lastp.get("type") or {}).get("text") in ("End of Game", "End of Regulation") and \
            int((lastp.get("period") or {}).get("number") or 0) >= 4 and lastp.get("awayScore") != lastp.get("homeScore")
        state = "post" if ended else "in"
        period = int((lastp.get("period") or {}).get("number") or 0)
        clock = (lastp.get("clock") or {}).get("displayValue")
        ap, hp = lastp.get("awayScore"), lastp.get("homeScore")
    side_of = {teams["away"]["id"]: "away", teams["home"]["id"]: "home"}
    snap = {"state": state, "period": period, "clock": clock, "clock_s": clock_secs(clock),
            "away_score": L._int(ap), "home_score": L._int(hp), "teams": teams,
            "network": network(d, sb), "has_ball": None, "receiving": None, "down": None, "distance": None,
            "yte": None, "spot": None, "kneeling": False, "turnover": False, "situation": bool(last),
            "last_play": (last or {}).get("text"), "play_id": (last or {}).get("id")}
    if last:
        end = last.get("end") or {}
        prev = real[-2] if len(real) > 1 else None
        scored = None
        if prev is not None:
            if (last.get("awayScore") or 0) > (prev.get("awayScore") or 0):
                scored = "away"
            elif (last.get("homeScore") or 0) > (prev.get("homeScore") or 0):
                scored = "home"
        elif last.get("scoringPlay"):
            scored = side_of.get(str(((last.get("start") or {}).get("team") or {}).get("id") or ""))
        lty = (last.get("type") or {}).get("text") or ""
        if scored and lty != "Kickoff":
            # kickoff pending: the side that gave up the score receives (a safety: the scorer does)
            snap["receiving"] = scored if lty == "Safety" else ("home" if scored == "away" else "away")
        else:
            snap["has_ball"] = side_of.get(str((end.get("team") or {}).get("id") or ""))
            snap["down"], snap["distance"] = L._int(end.get("down")), L._int(end.get("distance"))
            snap["yte"] = L._int(end.get("yardsToEndzone"))
            snap["spot"] = end.get("possessionText")
        snap["kneeling"] = bool(re.search(r"\bkneel", str(last.get("text") or ""), re.I))
        snap["turnover"] = bool(last.get("isTurnover")) or lty in L.TURNOVER_TYPES
    used = _timeouts_used(plays, teams, period)
    per = 1 if period >= 5 else 3
    # ESPN logs some media/official timeouts as 'Timeout <team>' too (Boise used "4" in one half on
    # 11/14), so a counted number is only a lower bound: fine for the kneel math (it errs toward
    # "can kneel it out", i.e. fewer alerts), never printed.
    snap["timeouts"] = {s: max(0, per - used[s]) for s in ("away", "home")}
    snap["timeouts_src"] = "counted"
    ds = d.get("situation") or {}  # a live summary may carry ESPN's own situation block
    sit_to = {"away": L._int(ds.get("awayTimeouts")), "home": L._int(ds.get("homeTimeouts"))} \
        if upto is None and ds else ((sb or {}).get("timeouts") or {})
    if all(isinstance(sit_to.get(s), int) for s in ("away", "home")):
        snap["timeouts"] = {s: sit_to[s] for s in ("away", "home")}  # ESPN's own count wins when it has one
        snap["timeouts_src"] = "espn"
    if wp:
        w = d.get("winprobability") or []
        if w:
            snap["home_wp"] = w[-1].get("homeWinPercentage")
    return snap


def from_cache(g: dict) -> dict:
    """A snapshot from the poller's cache row only (no summary): the conservative fallback,
    possession per the scoreboard and nothing else."""
    import live_games as L
    teams = {s: {"id": (g.get(s) or {}).get("id") or "", "abbr": (g.get(s) or {}).get("abbr") or "",
                 "loc": (g.get(s) or {}).get("name") or "", "nick": (g.get(s) or {}).get("nick") or ""}
             for s in ("away", "home")}
    return {"state": g.get("state"), "period": int(g.get("period") or 0), "clock": g.get("clock"),
            "clock_s": clock_secs(g.get("clock")), "away_score": L._int(g.get("away_score")),
            "home_score": L._int(g.get("home_score")), "teams": teams, "network": network(None, g),
            "has_ball": g.get("possession") if g.get("possession") in ("away", "home") else None,
            "receiving": None, "down": None, "distance": None, "yte": None, "spot": None, "kneeling": False,
            "turnover": False, "situation": False, "timeouts": None, "last_play": g.get("last_play")}


def aligned(pool: dict, snap: dict) -> bool:
    """all_games Away/Home are ESPN's away/home (GameId is the ESPN id); refuse a flipped pair
    (a neutral-site listing) rather than grade the wrong side."""
    a, h = _norm(pool.get("Away")), _norm(pool.get("Home"))
    ea = {_norm(snap["teams"]["away"].get(k)) for k in ("loc", "abbr")} - {""}
    eh = {_norm(snap["teams"]["home"].get(k)) for k in ("loc", "abbr")} - {""}
    swapped = any(a == x or a.startswith(x) or x.startswith(a) for x in eh) and \
        any(h == x or h.startswith(x) or x.startswith(h) for x in ea)
    return not swapped


def situation_for(snap: dict, t: str) -> tuple[dict, int | None]:
    """cover_live's view (T vs opp) of a snapshot, and T's timeouts."""
    opp = "home" if t == "away" else "away"
    rel = {t: "T", opp: "opp"}
    sc = {"away": snap.get("away_score") or 0, "home": snap.get("home_score") or 0}
    sit = {"has_ball": rel.get(snap.get("has_ball")), "receiving": rel.get(snap.get("receiving")),
           "down": snap.get("down"), "kneeling": snap.get("kneeling"), "opp_leads": sc[opp] >= sc[t]}
    if not snap.get("situation"):
        sit["fallback"] = True
    to = (snap.get("timeouts") or {}).get(t)
    return sit, to


# -----------------------------------------------------------------------------
# words (templates in Picky's voice; rotating, fixed per post key)
# -----------------------------------------------------------------------------


def _variant(options: list[str], key: str) -> str:
    return options[int(hashlib.sha1(key.encode()).hexdigest(), 16) % len(options)]


def _num(x: float) -> str:
    return str(int(x)) if float(x) == int(x) else f"{x:g}"


def side_label(pool: dict, side: str) -> str:
    """'Oregon State +13.5' from the league line."""
    ln = float(pool.get("line") or 0)
    h = ln if side == "home" else -ln
    name = pool["Home"] if side == "home" else pool["Away"]
    return f"{name} {'+' if h > 0 else ''}{_num(h)}" if h else f"{name} (pick'em)"


def clock_words(snap: dict) -> str:
    p = int(snap.get("period") or 0)
    if p >= 5:
        return "in OT" if p == 5 else f"in {p - 4}OT"
    return f"{snap.get('clock')} left in the 4th"


def _spot_words(snap: dict, pool: dict) -> str:
    """'2nd & 6 at the Iowa 30' from ESPN's down/distance and possessionText ('IOWA 30')."""
    down, dist, spot = snap.get("down"), snap.get("distance"), snap.get("spot") or ""
    words = ""
    if down and dist is not None:
        words = f"{down}{ {1: 'st', 2: 'nd', 3: 'rd'}.get(down, 'th')} & {'goal' if snap.get('yte') == dist else dist}"
    m = re.match(r"\s*([A-Za-z&'.\- ]+?)\s+(\d{1,2})\s*$", spot)
    if m:
        ab, yd = m.group(1), int(m.group(2))
        name = None
        for side in ("away", "home"):
            t = snap["teams"][side]
            if _norm(ab) in (_norm(t.get("abbr")), _norm(t.get("loc"))):
                name = pool["Away"] if side == "away" else pool["Home"]
        where = "midfield" if yd == 50 else f"the {name or ab} {yd}"
        words = f"{words} at {where}".strip()
    return words


def flip_words(pool: dict, snap: dict, cm: dict, sit: dict, to: int | None) -> str:
    """Exactly what flips it, given the situation."""
    tname = pool["Away"] if cm["t"] == "away" else pool["Home"]
    oname = pool["Home"] if cm["t"] == "away" else pool["Away"]
    kind = score_kind(cm["needs"])
    if sit.get("ot") or int(snap.get("period") or 0) >= 5:
        return f"{tname} needs {kind} to flip it."
    if sit.get("has_ball") == "T":
        spot = _spot_words(snap, pool)
        return f"{tname} has it{(' ' + spot) if spot else ''}, {kind} flips it."
    if sit.get("receiving") == "T":
        return f"{tname} gets it back on the kickoff, {kind} flips it."
    tos = (f", and {tname} has {to} timeout{'s' if to != 1 else ''}" if to is not None
           and snap.get("timeouts_src") == "espn" else "")
    if sit.get("opp_leads", True):
        return f"{oname} has the ball but can't just kneel it out{tos}. A stop, then {kind}, flips it."
    return f"{oname} has the ball{tos}. Once {tname} gets it back, {kind} flips it."


def split_words(counts: dict | None, pool: dict, cm: dict) -> str:
    """'8 of you are sweating Iowa': per-game counts only, and only after picks reveal."""
    if not counts:
        return ""
    cov = pool["Home"] if cm["cov"] == "home" else pool["Away"]
    t = pool["Home"] if cm["t"] == "home" else pool["Away"]
    n_cov, n_t = counts.get(cm["cov"], 0), counts.get(cm["t"], 0)
    if not n_cov and not n_t:
        return ""
    bits = []
    if n_cov:
        bits.append(f"{n_cov} of you {'is' if n_cov == 1 else 'are'} sweating {cov}")
    if n_t:
        bits.append(f"{n_t} need{'s' if n_t == 1 else ''} {t} to score")
    return (", ".join(bits)) + "."


def score_words(pool: dict, snap: dict) -> str:
    return f"{pool['Away']} {snap['away_score']}, {pool['Home']} {snap['home_score']}"


def cov_words(pool: dict, cm: dict) -> str:
    cov = side_label(pool, cm["cov"])
    if cm["m"] == 0:
        return f"{cov} is covering on the number (a dead-even finish goes to {pool['Away']})"
    return f"{cov} is covering by {_num(cm['margin'])}"


def alert_line(pool: dict, snap: dict, cm: dict, sit: dict, to: int | None, counts: dict | None, key: str) -> str:
    head = _variant(["Tune in:", "Cover watch:", "Sweat alert:", "Get to a TV:"], key + ":h")
    parts = [f"{head} {score_words(pool, snap)}, {clock_words(snap)}. {cov_words(pool, cm)}.",
             flip_words(pool, snap, cm, sit, to)]
    sp = split_words(counts, pool, cm)
    if sp:
        parts.append(sp[0].upper() + sp[1:])
    if snap.get("network"):
        parts.append(f"Tune to {snap['network']}.")
    return " ".join(parts)


def flip_line(pool: dict, snap: dict, cm: dict, sit: dict, to: int | None, counts: dict | None, key: str,
              live: bool) -> str:
    new_cov = pool["Home"] if cm["cov"] == "home" else pool["Away"]
    head = _variant([f"Flip! {new_cov} just took the cover.", f"And there's the flip: {new_cov} has the cover now.",
                     f"Cover flipped to {new_cov}."], key + ":h")
    parts = [head, f"{score_words(pool, snap)}, {clock_words(snap)}. {cov_words(pool, cm)}."]
    if live:
        parts.append(flip_words(pool, snap, cm, sit, to).replace(" flips it", " flips it back"))
    if counts:
        n = counts.get(cm["cov"], 0)
        if n:
            parts.append(f"That swings it for {n} of you.")
    if live and snap.get("network"):
        parts.append(f"Tune to {snap['network']}.")
    return " ".join(parts)


def again_line(pool: dict, snap: dict, cm: dict, sit: dict, to: int | None, counts: dict | None, key: str) -> str:
    game = f"{pool['Away']}-{pool['Home']}"
    why = "after the turnover" if snap.get("turnover") else "again"
    head = _variant([f"{game} is live {why}.", f"Back on: {game} is live {why}.", f"Don't turn it off: {game} is live {why}."],
                    key + ":h")
    parts = [head, f"{score_words(pool, snap)}, {clock_words(snap)}. {cov_words(pool, cm)}.",
             flip_words(pool, snap, cm, sit, to)]
    if snap.get("network"):
        parts.append(f"Tune to {snap['network']}.")
    return " ".join(parts)


# -----------------------------------------------------------------------------
# engine: which posts go out on this tick (pure; state is a plain dict)
# -----------------------------------------------------------------------------


class Engine:
    def __init__(self, state: dict | None = None, ackbar_jab=None):
        self.state = state if state is not None else {}
        self.ackbar_jab = ackbar_jab  # fn(text) -> text with a jab, or None

    def _g(self, gid: str) -> dict:
        return self.state.setdefault(str(gid), {"posts": []})

    def step(self, now: float, games: list[dict]) -> list[dict]:
        """games: [{gid, pool, snap, counts (per side, after lock, or None), ackbar (side or None)}].
        Returns [{gid, n, kind, text}] for this tick (one combined post); call commit() if it went out."""
        out = []
        for x in games:
            gid, pool, snap = str(x["gid"]), x["pool"], x["snap"]
            gs = self._g(gid)
            if not snap or snap.get("state") != "in" or len(gs["posts"]) >= MAX_POSTS:
                continue  # never a final from here; never a third post
            cm = cover_math(pool.get("line"), snap.get("away_score"), snap.get("home_score"))
            if not cm:
                continue
            sit, to = situation_for(snap, cm["t"])
            ok, why = cover_alert(snap.get("period"), snap.get("clock_s"), cm, sit, to)
            if int(snap.get("period") or 0) >= 5:
                sit["ot"] = True
            n = len(gs["posts"])
            key = f"cover:{gid}:{n + 1}:{snap.get('play_id') or int(now)}"
            if n == 0:
                if ok:
                    out.append({"gid": gid, "n": 1, "kind": "alert", "cov": cm["cov"], "why": why,
                                "text": alert_line(pool, snap, cm, sit, to, x.get("counts"), key)})
                continue
            p1 = gs["posts"][0]
            if not in_window(snap.get("period"), snap.get("clock_s")):
                continue
            # 1) a flip of the cover since post 1, held DEBOUNCE_S
            if cm["cov"] != p1["cov"]:
                pf = gs.get("pending_flip")
                if not pf or pf["cov"] != cm["cov"]:
                    gs["pending_flip"] = {"cov": cm["cov"], "since": now}
                elif now - pf["since"] >= DEBOUNCE_S and now - p1["ts"] >= SPACING_S:
                    txt = flip_line(pool, snap, cm, sit, to, x.get("counts"), key, ok)
                    if x.get("ackbar") and x["ackbar"] == p1["cov"] and self.ackbar_jab:
                        txt = self.ackbar_jab(f"{txt} Ackbar had {pool['Home'] if p1['cov'] == 'home' else pool['Away']}.")
                    out.append({"gid": gid, "n": 2, "kind": "flip", "cov": cm["cov"], "why": why, "text": txt})
                continue
            gs.pop("pending_flip", None)
            # 2) live again: it went dead (still close, not live) after post 1, and came back
            if is_close(cm) and not ok:
                gs["dead"] = True
                gs.pop("again_since", None)
            elif ok and gs.get("dead"):
                since = gs.setdefault("again_since", now)
                if now - since >= DEBOUNCE_S and now - p1["ts"] >= SPACING_S:
                    out.append({"gid": gid, "n": 2, "kind": "again", "cov": cm["cov"], "why": why,
                                "text": again_line(pool, snap, cm, sit, to, x.get("counts"), key)})
        return out

    def commit(self, now: float, posts: list[dict]) -> None:
        for p in posts:
            gs = self._g(p["gid"])
            gs["posts"].append({"ts": now, "kind": p["kind"], "cov": p["cov"]})
            gs.pop("pending_flip", None)
            gs.pop("again_since", None)

    def prune(self, now: float, keep_s: float = 3 * 86400) -> None:
        for gid in [g for g, v in self.state.items() if v.get("posts") and now - v["posts"][-1]["ts"] > keep_s]:
            self.state.pop(gid, None)


def combine(posts: list[dict]) -> list[str]:
    """One post for every game on this tick: one text per game."""
    return [p["text"] for p in posts]


# -----------------------------------------------------------------------------
# live runner
# -----------------------------------------------------------------------------


def load_state() -> dict:
    return ob._read_json(STATE_FILE, {})


def save_state(st: dict) -> None:
    ob._write_private(STATE_FILE, json.dumps(st, indent=1))


class League:
    """Pool rows (lines) + per-game pick counts after lock, refreshed every few minutes."""

    def __init__(self, every: float = 300):
        self.every, self.at, self.lg = every, 0.0, None

    def get(self, now_dt: dt.datetime):
        if self.lg is None or time.time() - self.at > self.every:
            import group_posts as gp
            self.lg = gp.League(gp.DB(), now_dt)
            self.at = time.time()
        self.lg.now = now_dt
        return self.lg

    @staticmethod
    def pool_games(lg) -> dict[str, dict]:
        return {str(g["GameId"]): g for g in lg.games if g.get("picked") and not g.get("tiebreaker")}

    @staticmethod
    def counts(lg, g: dict) -> tuple[dict | None, str | None]:
        """({'away': n, 'home': n}, Ackbar's side) once the week has locked; (None, None) before."""
        try:
            picks = [p for p in lg.picks(g["week"]) if p["game_id"] == g["GameId"] and p.get("pick")
                     and p["team_id"] in lg.tid]
        except PermissionError:
            return None, None
        import playoff_race as pr
        c, ack = {"away": 0, "home": 0}, None
        for p in picks:
            side = "home" if pr.same(p["pick"], g["Home"]) else "away" if pr.same(p["pick"], g["Away"]) else None
            if side:
                c[side] += 1
                if lg.tid[p["team_id"]] == "Ackbar":
                    ack = side
        return c, ack


def _jab(text: str) -> str:
    import nemesis
    return nemesis.tail(text, "Ackbar", None, chat_id=ob.config().get("group_guid"), group=True, force=True)


def tick_once(league: League, engine: Engine, dry_run: bool | None = None, echo=print, client=None,
              now_dt: dt.datetime | None = None) -> float:
    import httpx
    import live_games as L
    if OFF_FILE.exists():
        return TICK_IDLE
    now_dt = now_dt or dt.datetime.now(CT)
    cache = L.load() or {}
    games = cache.get("games") or {}
    pool_ids = set(map(str, cache.get("pool_ids") or []))
    live_pool = {gid: g for gid, g in games.items() if gid in pool_ids and g.get("state") == "in"}
    hot = {gid: g for gid, g in live_pool.items() if int(g.get("period") or 0) >= 4}
    if not hot:
        soon = any(gid in pool_ids and g.get("state") == "pre" and _kicks_within(g, 3600) for gid, g in games.items())
        return TICK_WARM if live_pool or soon else TICK_IDLE
    if ob.in_quiet_hours(now_dt):
        return TICK_WARM
    lg = league.get(now_dt)
    pool = League.pool_games(lg)
    rows = []
    own = client is None
    client = client or httpx.Client(headers=L.HEADERS, timeout=6)
    try:
        for gid, g in hot.items():
            pg = pool.get(gid)
            if not pg or pg.get("line") is None:
                continue
            sb = g if (time.time() - float(g.get("sb_ts") or 0)) < 45 else None
            snap = None
            try:
                d, _ = L._get(client, L.SUMMARY_URL + gid, timeout=6.0)
                snap = snapshot(d, sb=sb) if d else None
            except Exception as e:  # noqa: BLE001 - fall back to the poller's row
                log.warning("cover: summary %s failed: %s", gid, e)
            if snap is None or snap.get("state") is None:
                snap = from_cache(g)
            if not aligned(pg, snap):
                log.warning("cover: %s home/away don't line up with all_games, skipped", gid)
                continue
            counts, ack = League.counts(lg, pg)
            rows.append({"gid": gid, "pool": pg, "snap": snap, "counts": counts, "ackbar": ack})
    finally:
        if own:
            client.close()
    now = time.time()
    posts = engine.step(now, rows)
    for r in rows:
        cm = cover_math(r["pool"].get("line"), r["snap"].get("away_score"), r["snap"].get("home_score"))
        log.info("cover: %s %s %s m=%s ball=%s/%s to=%s", r["gid"], r["snap"].get("clock"), r["snap"].get("period"),
                 cm and cm["m"], r["snap"].get("has_ball"), r["snap"].get("receiving"), r["snap"].get("timeouts"))
    if posts:
        key = "cover:" + ",".join(f"{p['gid']}#{p['n']}" for p in posts)
        res = ob.post("cover", combine(posts), key, expires_s=EXPIRES_S, quiet=True, dry_run=dry_run,
                      now=now_dt, echo=echo)
        log.info("cover post %s -> %s", key, res)
        if res in ("queued", "dry-run", "dup", "paused", "capped", "no-group"):
            engine.commit(now, posts)  # counted either way: a missed alert never turns into a late one
    engine.prune(now)
    return TICK_HOT


def _kicks_within(g: dict, secs: float) -> bool:
    try:
        k = dt.datetime.fromisoformat(str(g.get("start")).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return False
    return -600 < k - time.time() < secs


def run(dry_run: bool | None = None) -> None:
    league = League()
    log.info("cover: ticker up")
    while True:
        st = load_state()
        engine = Engine(st, ackbar_jab=_jab)
        try:
            wait = tick_once(league, engine, dry_run=dry_run, echo=log.info)
        except Exception:  # noqa: BLE001 - keep ticking; launchd would restart anyway
            log.exception("cover: tick crashed")
            wait = TICK_WARM
        save_state(engine.state)
        time.sleep(wait)


# -----------------------------------------------------------------------------
# replay (no sends): which posts would have gone out, from saved ESPN play-by-play
# -----------------------------------------------------------------------------


def replay(fixture: Path, picks_locked: bool = True, tick: float = TICK_HOT, echo=print,
           quiet: bool = True, live_picks: bool = False) -> list[dict]:
    """Walk each pool game's play-by-play by its wallclock, ticking every `tick` seconds, and
    return the posts the engine would have made. Counts come from pool.json '_picks' (one
    member's public picks) when picks_locked; no Supabase, no outbox."""
    import live_games as L
    pool_js = json.loads((fixture / "pool.json").read_text())
    picks = pool_js.get("_picks") or {}
    timeline = []  # (wallclock ts, gid, play index)
    data, pools = {}, {}
    for gid, p in pool_js.items():
        if gid.startswith("_"):
            continue
        f = fixture / f"summary_{gid}.json.gz"
        if not f.exists():
            continue
        d = json.load(gzip.open(f))
        data[gid] = d
        pools[gid] = {"GameId": gid, "Away": p["away"], "Home": p["home"], "line": p["line"]}
        for i, pl in enumerate(L._flat_plays(d)):
            w = pl.get("wallclock")
            if w:
                timeline.append((dt.datetime.fromisoformat(w.replace("Z", "+00:00")).timestamp(), gid, i))
    timeline.sort()
    if not timeline:
        return []
    real = {}
    if live_picks:  # the league's real per-game counts (read-only; the week is long locked)
        import group_posts as gp
        lg = gp.League(gp.DB(), dt.datetime.now(CT))
        for g in lg.games:
            if str(g["GameId"]) in pools:
                real[str(g["GameId"])] = League.counts(lg, g)
    engine = Engine({}, ackbar_jab=lambda t: t + " [+ nemesis.py Ackbar jab]")
    idx = {gid: -1 for gid in data}
    ti, t, out, held = 0, timeline[0][0], [], set()
    end = timeline[-1][0] + 60
    while t <= end:
        while ti < len(timeline) and timeline[ti][0] <= t:
            idx[timeline[ti][1]] = timeline[ti][2]
            ti += 1
        rows = []
        for gid, i in idx.items():
            if i < 0:
                continue
            snap = snapshot(data[gid], upto=i)
            if not snap:
                continue
            counts = ack = None
            if gid in real:
                counts, ack = real[gid]
            elif picks_locked and picks:
                counts = {"away": 0, "home": 0}
                for team, side_team in picks.items():
                    pk = side_team.get(gid)
                    if pk:
                        side = "home" if _norm(pk) == _norm(pools[gid]["Home"]) else "away"
                        counts[side] += 1
                        if team == "Ackbar":
                            ack = side
            rows.append({"gid": gid, "pool": pools[gid], "snap": snap, "counts": counts, "ackbar": ack})
        if quiet and ob.in_quiet_hours(dt.datetime.fromtimestamp(t, CT)):
            for r in rows:  # the live runner doesn't even look during quiet hours
                cm = cover_math(r["pool"]["line"], r["snap"]["away_score"], r["snap"]["home_score"])
                ok = cm and cover_alert(r["snap"]["period"], r["snap"]["clock_s"], cm,
                                        *situation_for(r["snap"], cm["t"]))[0]
                if ok and not engine.state.get(r["gid"], {}).get("posts") and r["gid"] not in held:
                    held.add(r["gid"])
                    when = dt.datetime.fromtimestamp(t, CT).strftime("%a %-I:%M:%S %p CT")
                    line = alert_line(r["pool"], r["snap"], cm, situation_for(r["snap"], cm["t"])[0],
                                      situation_for(r["snap"], cm["t"])[1], r["counts"], f"q:{r['gid']}")
                    out.append({"gid": r["gid"], "n": 0, "kind": "quiet-held", "text": line, "at": when})
                    echo(f"\n[{when}] QUIET HOURS, nothing posts. Would have been:\n  > {line}")
            t += tick
            continue
        posts = engine.step(t, rows)
        if posts:
            engine.commit(t, posts)
            when = dt.datetime.fromtimestamp(t, CT).strftime("%a %-I:%M:%S %p CT")
            for p in posts:
                out.append({**p, "at": when})
            echo(f"\n[{when}] would post ({len(posts)} game{'s' if len(posts) > 1 else ''}):")
            for p in posts:
                echo(f"  > [{p['gid']} post {p['n']}/{MAX_POSTS} {p['kind']}; {p['why']}] {p['text']}")
        t += tick
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["run", "once", "replay"])
    ap.add_argument("fixture", nargs="?")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--picks-locked", action="store_true")
    ap.add_argument("--live-picks", action="store_true", help="replay: the league's real pick counts (read-only)")
    a = ap.parse_args(argv)
    if a.cmd == "replay":
        posts = replay(Path(a.fixture or HERE / "eval" / "fixtures" / "live-2026-11-14"), picks_locked=a.picks_locked,
                       live_picks=a.live_picks)
        print(f"\n{sum(1 for p in posts if p['n'])} alert post(s) would have gone out "
              f"({sum(1 for p in posts if not p['n'])} held by quiet hours)")
        return 0
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    handlers = [logging.FileHandler(LOG_PATH)] + ([] if a.cmd == "run" else [logging.StreamHandler()])
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=handlers)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if a.cmd == "once":
        st = load_state()
        eng = Engine(st, ackbar_jab=_jab)
        print("next tick in", tick_once(League(), eng, dry_run=True if a.dry_run else None), "s")
        if not a.dry_run:
            save_state(eng.state)
        return 0
    run(dry_run=True if a.dry_run else None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
