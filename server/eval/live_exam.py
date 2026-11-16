"""
80-question live-game exam on the replay harness (live_replay.py).

Last Saturday's (2026-11-14) real games replay from ESPN's recorded
play-by-play: all 4 pool games (Wake Forest at SMU, Oregon State at Boise
State, North Texas at UTSA, North Dakota State at Hawai'i), LOU at UNC (QB hurt,
backup in), Texas at LSU (walk-off TD), Nebraska at Rutgers and the day's one
OT game (New Mexico State at Missouri State). The fake ESPN freezes at a moment
(pregame, 1Q, halftime, late 4Q, OT, final) and every question is asked as of
that moment.

Answer keys come from the raw play-by-play (score, clock, scoring plays,
turnovers, injuries, passers), computed here WITHOUT live_games' parser, and
each item's hand-typed key is asserted against them when the items are built,
so a key can't drift from the data.

  fast   model-free regression (seconds; runs from test_live_games.py): at each
         item's moment the real poller syncs from the fake ESPN, and for every
         item the LIVE rider + the code-computed live fact must carry the facts
         the item needs, the computed fact must be correct and survive the claim
         check, and wrong scores / cover margins / scorers must be cut by it.
         --answers FILE also re-grades recorded model answers through the
         current claim check.
  ask    the real thing: a side sidecar (gemma, full pipeline) answers all 80
         as Chuck P's DMs; graded in code. Run inside the exam lock.
  judge  Opus (picky_claude via dossier.run_claude, offline) reads each answer
         against the facts at that moment and lists wrong/unsupported facts.
  show   print the items and their keys.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import live_replay as R  # noqa: E402

DATA = HERE / "fixtures" / "live-2026-11-14"
WF, NEB, BOI, UNC, LSU, UTSA, HAW, OT = ("401858301", "401858525", "401860914", "401858298", "401856885",
                                         "401862827", "401864537", "401870796")
POOL_IDS = (WF, BOI, UTSA, HAW)
PERSONA = "Chuck P"


def load_pool(d: Path = DATA) -> tuple[dict, dict]:
    raw = json.loads((d / "pool.json").read_text())
    picks = raw.get("_picks") or {}
    return {k: v for k, v in raw.items() if not k.startswith("_")}, picks


# ----------------------------------------------------------------------------
# truth: raw ESPN fields at wallclock t (independent of live_games.parse_summary)
# ----------------------------------------------------------------------------


_TO_TYPES = {"Fumble Recovery (Opponent)", "Pass Interception Return", "Interception", "Interception Return Touchdown",
             "Fumble Return Touchdown", "Blocked Field Goal", "Blocked Punt"}


def _txt(x, k):
    return (x.get(k) or {}).get("text") if isinstance(x.get(k), dict) else x.get(k)


class Truth:
    def __init__(self, games: dict, pool: dict):
        self.games, self.pool = games, pool

    def at(self, gid: str, t: float) -> dict:
        snap = R.snapshot(self.games[gid], t)
        comp = snap["header"]["competitions"][0]
        side = {c["homeAway"]: c for c in comp["competitors"]}
        name = {str(c["team"]["id"]): c["team"]["location"] for c in comp["competitors"]}
        st = comp["status"]["type"]["state"]
        plays = [p for d in snap["drives"]["previous"] + ([snap["drives"]["current"]] if snap["drives"].get("current")
                                                           else []) for p in d["plays"]]
        a, h = int(side["away"]["score"]), int(side["home"]["score"])
        scoring = [{"team": name.get(str((s.get("team") or {}).get("id"))), "type": _txt(s, "type"),
                    "text": re.sub(r"\s+", " ", s.get("text") or "").strip(),
                    "period": (s.get("period") or {}).get("number"), "clock": (s.get("clock") or {}).get("displayValue"),
                    "a": s.get("awayScore"), "h": s.get("homeScore")} for s in snap["scoringPlays"]]
        if scoring:
            top = max(scoring, key=lambda s: (s["a"] or 0) + (s["h"] or 0))
            if (top["a"], top["h"]) != (a, h):
                raise ValueError(f"{gid} at {t}: header {a}-{h} but scoring plays reach {top['a']}-{top['h']}")
        last = plays[-1] if plays else {}
        return {"gid": gid, "state": st, "away": side["away"]["team"]["location"],
                "home": side["home"]["team"]["location"], "a": a, "h": h,
                "period": comp["status"].get("period"), "clock": comp["status"].get("displayClock"),
                "status": comp["status"]["type"]["name"], "scoring": scoring, "plays": plays,
                "last_play": re.sub(r"\s+", " ", last.get("text") or ""), "names": name}

    def cover(self, gid: str, t: float):
        """(covering side, margin) vs the league's locked line, or None."""
        p = self.pool.get(gid)
        s = self.at(gid, t)
        if not p or s["state"] == "pre":
            return None
        m = s["h"] + p["line"] - s["a"]
        return (p["home"] if m > 0 else p["away"]), abs(m)

    def facts(self, gid: str, t: float) -> str:
        """Plain-text facts for the judge: everything ESPN had shown at t."""
        s = self.at(gid, t)
        hd = f"{s['away']} at {s['home']}"
        if s["state"] == "pre":
            return f"{hd}: NOT STARTED at this moment (no score yet)."
        per = {1: "1Q", 2: "2Q", 3: "3Q", 4: "4Q"}.get(s["period"], "OT")
        when = "FINAL" + ("/OT" if (s["period"] or 0) > 4 else "") if s["state"] == "post" else (
            "HALFTIME" if "HALFTIME" in s["status"] else f"LIVE {s['clock']} {per}")
        out = [f"{hd}: {s['away']} {s['a']}, {s['home']} {s['h']} ({when})."]
        if s["scoring"]:
            out.append("Scoring plays so far: " + " | ".join(
                f"{x['team']} {x['type']} Q{x['period']} {x['clock']}: {x['text']} ({s['away']} {x['a']}-{s['home']} {x['h']})"
                for x in s["scoring"]))
        else:
            out.append("No points scored yet.")
        tos, inj, passers = [], [], {}
        for p in s["plays"]:
            ty, tx = _txt(p, "type") or "", p.get("text") or ""
            when_p = f"Q{(p.get('period') or {}).get('number')} {(p.get('clock') or {}).get('displayValue')}"
            if p.get("isTurnover") or ty in _TO_TYPES or "TURNOVER ON DOWNS" in tx.upper():
                tos.append(f"{when_p} {re.sub(r'#[0-9]+ ', '', tx)[:150]}")
            for m in re.finditer(r"#\d{1,2} ([A-Z][^#,]*?) injured on the play", tx):
                inj.append(f"{when_p} {m.group(1)}")
            for m in re.finditer(r"#\d{1,2} ([A-Z][A-Za-z.'\- ]+?(?: Jr\.| II| III| IV)?) (?:pass|sacked|scrambles)", tx):
                passers.setdefault(m.group(1), when_p)
        if tos:
            out.append("Turnovers / turnovers on downs so far: " + " | ".join(tos))
        if inj:
            out.append("Reported 'injured on the play' (ESPN play-by-play): " + "; ".join(inj))
        if passers:
            out.append("Passers so far (first dropback): " + "; ".join(f"{k} ({v})" for k, v in passers.items()))
        if s["state"] == "in" and s["last_play"]:
            out.append(f"Last play: {re.sub(r'#[0-9]+ ', '', s['last_play'])[:200]}")
        cv = self.cover(gid, t)
        if cv:
            p = self.pool[gid]
            out.append(f"POOL GAME, league line {p['home']} {p['line']:+g}: {'covered' if s['state'] == 'post' else 'covering'}"
                       f" side is {cv[0]} by {cv[1]:g}.")
        else:
            out.append("Not a pool game (no league line)." if gid not in self.pool else "")
        return "\n".join(x for x in out if x)


# ----------------------------------------------------------------------------
# the 80 items
# ----------------------------------------------------------------------------


def _anchor(games, gid, rx, after=30.0, period=None) -> float:
    p = R._find_play(games[gid], rx, period)
    return R._ts(p["wallclock"]) + after


def _at(iso: str) -> float:
    return R._ts(iso)


NOT_STARTED = r"not (started|kicked|underway|on yet|begun)|hasn'?t (started|kicked|begun)|has not (started|kicked)|" \
              r"kicks? off|kickoff|yet to|later|tonight|isn'?t on|not on yet|hasn'?t even"
NEG = r"\b(no|not|nope|nah|isn'?t|aren'?t|hasn'?t|haven'?t|didn'?t|nothing|none)\b"
NO_INFO = (r"(don'?t|do not|can'?t|cannot) (see|have|find|know|tell|say)|no (report|word|sign|mention|info|data|update|"
           r"record|feed|line)|nothing (in|on|about|from|says|shows)|not (in|on|showing|listed|seeing|reported|tracking|"
           r"covering|getting)|isn'?t (in|on|listed|showing|reported)|haven'?t seen|hasn'?t (been )?(reported|shown)|"
           r"no (sign|indication)|doesn'?t (say|show|list)|n'?t (show|list|say)|unconfirmed|rumor")


def build(games: dict, pool: dict, picks: dict) -> list[dict]:
    tr = Truth(games, pool)
    A = lambda gid, rx, after=30.0, period=None: _anchor(games, gid, rx, after, period)  # noqa: E731
    M = {
        "pre": _at("2026-11-14T16:50:00Z"),
        "wf1": A(WF, r"J\.Searcy rush middle for 6 yards gain to the SMU00 TOUCHDOWN"),
        "half1": _at("2026-11-14T18:50:00Z"),
        "wf4": A(WF, r"S\.Pizzella rush middle for 25 yards"),
        "noon_end": _at("2026-11-14T20:31:00Z"),
        "ot4": A(OT, r"C\.Lair rush middle for 5 yards gain to the NMS00 TOUCHDOWN"),
        "otfinal": _at("2026-11-14T23:34:00Z"),
        "boi1": A(BOI, r"Travis Anderson 58 Yd Interception Return", after=40),
        "boi2": A(BOI, r"S\.Gaines rush middle for 4 yards gain to the OSU00 TOUCHDOWN"),
        "unc2": A(UNC, r"L\.Kienholz pass complete short left to #88 J\.Skinner"),
        "boihalf": _at("2026-11-15T00:56:00Z"),
        "uncinj": A(UNC, r"B\.Edwards Jr\. injured on the play", after=20),
        "unc4": A(UNC, r"T\.Burgess pass incomplete short right to #1 J\.Shipp", after=40),
        "boi4": A(BOI, r"R\.Jones caught at OSU21, for 26 yards", after=40),
        "lsu3": A(LSU, r"D\.Jones rush left for 44 yards", after=45),
        "utsa3": A(UTSA, r"M\.Reese caught at UTSA02", after=40),
        "lsuwalk": A(LSU, r"Raleek Brown 4 Yd pass from Arch Manning", after=30, period=4),
        "utsa4": A(UTSA, r"C\.Jimerson Jr\. rush middle for 9 yards gain to the UTSA00 TOUCHDOWN"),
        "late": _at("2026-11-15T05:35:00Z"),
        "haw4": A(HAW, r"Ray James Jr\. 49 Yd Interception Return", after=40),
    }
    items: list[dict] = []

    def add(iid, cat, m, q, need=(), bad=(), games_=(), hist=False, follow=None, expect=None, extra_pairs=(),
            key="", rider=None):
        items.append({"id": iid, "cat": cat, "moment": m, "t": M[m], "q": q, "need": list(need), "bad": list(bad),
                      "games": list(games_), "hist": hist, "follow": follow, "expect": expect or {},
                      "extra_pairs": [tuple(sorted(p)) for p in extra_pairs], "key": key,
                      "rider": list(need) if rider is None else list(rider)})

    # --- pregame (10:50 CT): nothing has kicked off -------------------------------------------
    add("pre-score", "trick", "pre", "what's the score of the Wake Forest SMU game?", [NOT_STARTED], [r"\b49\b", r"\bwon\b"],
        [WF], expect={"state": (WF, "pre")}, key="Wake Forest at SMU has not kicked off (11 AM CT kickoff).")
    add("pre-cover", "trick", "pre", "is Wake covering?", [NOT_STARTED], [r"covering by", r"\bcovered\b"], [WF],
        expect={"state": (WF, "pre")})
    add("pre-picks", "picks", "pre", "how are my picks doing?", [NOT_STARTED + r"|none of|haven'?t|nothing"],
        [r"\b[1-4]-0\b", r"\bcovered\b", r"covering by"], list(POOL_IDS), rider=[],
        key="Chuck P's 4 pool picks: Wake Forest (-1.5 at SMU), Oregon State (+13.5 at Boise State), UTSA (+2.5 vs "
            "North Texas), North Dakota State (-6.5 at Hawai'i). None has kicked off.")

    # --- Wake 1Q: Searcy TD, 14-0 ------------------------------------------------------------
    add("wf1-score", "score", "wf1", "what's the score in the Wake game?", [r"\b14\b", r"\b0\b|nothing|zero|zip|shutout|blank"],
        [], [WF], expect={"score": (WF, 14, 0)})
    add("wf1-last-td", "scoring", "wf1", "who scored that last Wake Forest TD?", [r"Searcy", r"\b6\b|six"], [], [WF], hist=True,
        expect={"last": (WF, "Jamar Searcy 6 Yd Run")})
    add("wf1-first-td", "scoring", "wf1", "how did Wake score their first touchdown?", [r"Clark", r"\b27\b", r"run|rush|ran"],
        [], [WF], hist=True, expect={"scored": (WF, "Ty Clark III 27 Yd Run")})
    add("wf1-cover", "cover", "wf1", "is Wake Forest covering?", [r"\byes|covering|\bup\b", r"\b12\.5\b"], [r"Wake( Forest)?( -1\.5)?( is)? not covering|Wake( Forest)? isn\'?t covering"],
        [WF], expect={"cover": (WF, "Wake Forest", 12.5)})
    add("neb1-score", "score", "wf1", "Nebraska Rutgers score?", [r"0-0|0 to 0|scoreless|no score|nothing|tied (at )?0|"
                                                                    r"zero|nobody has scored|0, Rutgers 0"],
        [], [NEB], expect={"score": (NEB, 0, 0)})

    # --- halftime of the noon games: Wake 42-0, Nebraska 14-0 ----------------------------------
    add("wf-half-cover", "cover", "half1", "is wake covering at the half and by how much?", [r"\b40\.5\b"],
        [r"Wake( Forest)?( -1\.5)?( is)? not covering|Wake( Forest)? isn\'?t covering"], [WF], expect={"cover": (WF, "Wake Forest", 40.5)})
    add("half-picks", "picks", "half1", "how are my picks doing?",
        [r"Wake", r"40\.5|covering|cruising|rolling|up 42|42-0", NOT_STARTED + r"|haven'?t|yet"],
        [r"Oregon State (is )?(covering|covered|won)", r"\b[2-4]-0\b"], list(POOL_IDS),
        extra_pairs=[(1, 0), (0, 0)], rider=[r"Wake", r"40\.5"],
        key="Wake Forest -1.5 covering by 40.5 (42-0 at halftime); Oregon State, UTSA, North Dakota State not started.")
    add("neb-half-tds", "scoring", "half1", "who scored Nebraska's touchdowns?", [r"Lindenmeyer", r"Colandrea"], [], [NEB],
        hist=True, expect={"scored": (NEB, "Luke Lindenmeyer 15 Yd pass from Anthony Colandrea")})
    add("neb-half-inj", "injury", "half1", "any injuries in Nebraska Rutgers?", [r"Mubenga|Ofurie"],
        [r"\bhealthy\b", r"torn|season-ending|concussion|out for"], [NEB])
    add("rut-qb", "qb", "half1", "who's QB for Rutgers right now?", [r"Surace"], [], [NEB],
        key="Rutgers: A.Surace took over from starter S.Ashenfelder (2Q 6:59).")

    # --- Wake 4Q: Pizzella TD 49-7 ------------------------------------------------------------
    add("wf4-what", "what", "wf4", "what just happened in the Wake game?", [r"Pizzella", r"\b25\b"], [], [WF], hist=True,
        expect={"last": (WF, "Steele Pizzella 25 Yd Run")})
    add("wf4-qb", "qb", "wf4", "who's playing QB for Wake now?", [r"Pizzella"], [], [WF],
        key="Wake Forest: S.Pizzella took over from starter G.Lopez (3Q 3:08), up 42-7 then 49-7.")
    add("wf4-smu-score", "scoring", "wf4", "did SMU ever score?", [r"Smith|Jennings", r"\b19\b"], [], [WF], hist=True,
        expect={"scored": (WF, "Yannick Smith 19 Yd pass from Kevin Jennings")})
    add("wf4-smu-cover", "cover", "wf4", "is SMU covering?", [NEG], [r"SMU (is|'s) covering", r"yes, SMU"], [WF],
        expect={"cover": (WF, "Wake Forest", 40.5)}, rider=[r"40\.5"])

    # --- 2:31 CT: Wake final, Nebraska 4Q -----------------------------------------------------
    add("wf-final", "score", "noon_end", "final score Wake SMU?", [r"\b49\b", r"\b7\b"], [], [WF], hist=True,
        expect={"score": (WF, 49, 7), "state": (WF, "post")})
    add("wf-final-mypick", "cover", "noon_end", "did my Wake pick cover?", [r"\byes|covered|cash|\bwin\b|hit|easily|smash"],
        [r"Wake( Forest)?( -1\.5)? (did ?n[o']t|didn'?t|not) cover|you (lost|didn'?t cover)|\bLOSS\b"], [WF], expect={"cover": (WF, "Wake Forest", 40.5)},
        rider=[r"40\.5"])
    add("neb4-score", "score", "noon_end", "how's Nebraska Rutgers going?", [r"\b31\b", r"\b0\b|nothing|shutout|zero"], [],
        [NEB], expect={"score": (NEB, 31, 0)})
    add("neb4-qb", "qb", "noon_end", "who's at QB for Nebraska?", [r"Lateef"], [], [NEB],
        key="Nebraska: T.Lateef took over from A.Colandrea (4Q 5:47) with Nebraska up 31-0.")
    add("tex-pre-who-won", "trick", "noon_end", "who won Texas LSU?", [NOT_STARTED], [r"\b22\b", r"Texas (won|wins|beat)"],
        [LSU], expect={"state": (LSU, "pre")}, key="Texas at LSU has not kicked off yet at this moment.")

    # --- OT game -------------------------------------------------------------------------------
    add("ot4-score", "score", "ot4", "New Mexico State Missouri State score?", [r"21", r"tied|21-21|21 all|21 apiece|21, Missouri State 21"],
        [], [OT], expect={"score": (OT, 21, 21)})
    add("ot4-how", "scoring", "ot4", "how did Missouri State tie it?", [r"Lair", r"\b5\b|five"], [], [OT], hist=True,
        expect={"last": (OT, "Connor Lair 5 Yd Run")})
    add("ot-final-how", "scoring", "otfinal", "how did the New Mexico State game end?",
        [r"Boamah", r"\b88\b", r"\bOT\b|overtime", r"intercept|pick"], [], [OT], hist=True,
        expect={"last": (OT, "Joel Boamah 88 Yd Interception Return"), "score": (OT, 21, 27)})

    # --- Boise 1Q: pick six, 15-7 --------------------------------------------------------------
    add("boi1-score", "score", "boi1", "what's the score of the Boise game?", [r"\b15\b", r"\b7\b"], [], [BOI],
        expect={"score": (BOI, 7, 15)})
    add("boi1-ost-td", "scoring", "boi1", "who scored for Oregon State?", [r"Legree", r"\b15\b"], [], [BOI], hist=True,
        follow="boi1-score", expect={"scored": (BOI, "Jesse Legree 15 Yd pass from Maalik Murphy")})
    add("boi1-last", "scoring", "boi1", "how did Boise score last?", [r"Anderson", r"\b58\b", r"intercept|pick"], [], [BOI],
        hist=True, expect={"last": (BOI, "Travis Anderson 58 Yd Interception Return")})
    add("boi1-cover", "cover", "boi1", "is my Oregon State pick covering?", [r"\byes|covering", r"\b5\.5\b"],
        [r"Oregon State( \+13\.5)?( is)? not covering|Oregon State isn\'?t covering|you\'?re not covering"], [BOI], expect={"cover": (BOI, "Oregon State", 5.5)})
    add("boi1-thrower", "trick", "boi1", "who threw that pick for Oregon State?",
        [r"Murphy|" + NO_INFO + r"|doesn'?t (say|name)|not (named|listed)"], [r"Hatcher|Reichle|Legree"], [BOI], rider=[],
        key="The pick-six play text doesn't name the passer; Maalik Murphy is Oregon State's only QB so far.")

    # --- Boise 2Q: Gaines TD 29-13, Boise flips to covering -----------------------------------
    add("boi2-cover", "cover", "boi2", "is Oregon State still covering?", [NEG, r"\b2\.5\b"],
        [r"Oregon State (is )?(still )?covering by"], [BOI], expect={"cover": (BOI, "Boise State", 2.5)})
    add("boi2-jones", "scoring", "boi2", "how many TDs does Rasean Jones have?", [r"\b(2|two)\b"],
        [r"\b(three|3|one|1) (td|touchdown)"], [BOI], hist=True, rider=[r"Jones.*Jones"],
        key="Rasean Jones: 2 TD catches (19 yds 1Q 3:52, 16 yds 2Q 8:46).")

    # --- 7:56 CT: Boise halftime 32-20, UNC 2Q ---------------------------------------------------
    add("boi-half-last", "scoring", "boihalf", "how did Boise get their last points before half?",
        [r"Moore", r"\b38\b", r"field goal|\bFG\b"], [], [BOI], hist=True, expect={"last": (BOI, "Canaan Moore 38 Yd Field Goal")})
    add("boi-half-cover", "cover", "boihalf", "is Oregon State covering now?", [r"\byes|covering", r"\b1\.5\b"],
        [r"Oregon State( \+13\.5)?( is)? not covering|Oregon State isn\'?t covering|you\'?re not covering"], [BOI], expect={"cover": (BOI, "Oregon State", 1.5)})
    add("boi-half-why", "followup", "boihalf", "why?", [r"13\.5|line|spread|points"], [], [BOI], follow="boi-half-cover",
        rider=[r"13\.5"])
    add("oregon-wrong-team", "trick", "boihalf", "how's Oregon doing?", [],
        [r"Oregon (is|are) (up|down|leading|trailing|losing|winning)", r"Ducks"], [BOI], rider=[],
        key="Oregon (the Ducks) is not in the feed; Oregon State trails Boise State 32-20 at halftime. A 32-20 is only OK "
            "if it's clearly Oregon State's game.")

    # --- UNC 2Q: Edwards fumble, Kienholz TD 13-7 -----------------------------------------------
    add("unc2-what", "what", "unc2", "what's happening in the UNC game?", [r"\b13\b", r"\b7\b"], [], [UNC], hist=True,
        expect={"score": (UNC, 13, 7)})
    add("unc2-td", "scoring", "unc2", "how did Louisville score that TD?", [r"Skinner", r"\b3\b|three", r"pass|caught|catch"],
        [], [UNC], hist=True, expect={"last": (UNC, "Jaleel Skinner 3 Yd pass from Lincoln Kienholz")})
    add("unc2-to", "turnover", "unc2", "any turnovers in North Carolina Louisville?", [r"Edwards", r"fumble"], [], [UNC],
        key="2Q 14:10: UNC QB B.Edwards Jr. sacked, fumbled, recovered by Louisville (J.Donald).")

    # --- UNC 3Q: Edwards hurt (9:14) ---------------------------------------------------------------
    add("unc-inj", "injury", "uncinj", "did UNC's QB get hurt?", [r"Edwards", r"injur|hurt|went down|banged"],
        [r"\bhealthy\b", r"\bfine\b", r"torn|season-ending|concussion|out for the"], [UNC])
    add("unc-inj-who", "followup", "uncinj", "who replaced him?", [NO_INFO + r"|not yet|too soon|unclear|no word|TBD|"
                                                                      r"haven'?t seen|hasn'?t (come|been)"],
        [r"Burgess (is|has|took|came|replaced|'s in)"], [UNC], follow="unc-inj", rider=[],
        key="20 s after the 3Q 9:14 injury the feed shows no replacement QB yet (Burgess's first dropback comes later).")
    add("boi3-cover", "cover", "uncinj", "is Oregon State covering?", [r"\byes|covering", r"\b8\.5\b"],
        [r"Oregon State( \+13\.5)?( is)? not covering|Oregon State isn\'?t covering|you\'?re not covering"], [BOI], expect={"cover": (BOI, "Oregon State", 8.5)})
    add("kienholz-rumor", "trick", "uncinj", "did Kienholz get hurt?", [NEG + r"|" + NO_INFO],
        [r"\byes\b", r"Kienholz (was|got|is) (hurt|injured)"], [UNC], rider=[],
        key="No injury reported for Louisville QB Lincoln Kienholz.")

    # --- UNC 4Q: Burgess in, turnover on downs ----------------------------------------------------
    add("unc4-qb", "qb", "unc4", "who's playing QB for North Carolina right now?", [r"Burgess"], [], [UNC],
        key="UNC: T.Burgess took over after B.Edwards Jr. was reported injured 3Q 9:14.")
    add("unc4-edwards-back", "injury", "unc4", "is Edwards back in?", [NEG],
        [r"\bhe'?s back\b", r"returned to the game", r"back in the game"], [UNC], rider=[r"not seen on a play since"])
    add("unc4-drive", "what", "unc4", "what did UNC do on their last drive?",
        [r"turnover on downs|on downs|fourth down|4th down|turned it over"], [], [UNC],
        key="4Q 12:47: Burgess incomplete to J.Shipp on 4th down, turnover on downs at the LOU18.")

    # --- Boise 4Q: Jones TD 46-34, ORST covering by 1.5 --------------------------------------------
    add("boi4-score", "score", "boi4", "Boise score?", [r"\b46\b", r"\b34\b"], [], [BOI], expect={"score": (BOI, 34, 46)})
    add("boi4-who", "followup", "boi4", "and who scored that last one?", [r"Jones", r"\b26\b|Madsen"], [], [BOI], hist=True,
        follow="boi4-score", expect={"last": (BOI, "Rasean Jones 26 Yd pass from Maddux Madsen")})
    add("boi4-cover", "cover", "boi4", "is my Oregon State pick still covering?", [r"\byes|covering|barely|hanging", r"\b1\.5\b"],
        [r"Oregon State( \+13\.5)?( is)? not covering|Oregon State isn\'?t covering|you\'?re not covering"], [BOI], expect={"cover": (BOI, "Oregon State", 1.5)})
    add("boi4-total", "total", "boi4", "how many total points have been scored in Boise Oregon State?", [r"\b80\b"], [], [BOI],
        expect={"score": (BOI, 34, 46)})
    add("boi4-picks", "picks", "boi4", "how are my picks doing?",
        [r"Wake", r"Oregon State", r"UTSA", r"North Dakota|NDSU|Hawai"],
        [r"\b[3-4]-0\b"], list(POOL_IDS) + [UTSA], extra_pairs=[(1, 0), (0, 0), (2, 0)],
        rider=[r"Oregon State.{0,40}1\.5|1\.5"],
        key="Wake Forest -1.5 WON (final 49-7, covered by 40.5); Oregon State +13.5 covering by 1.5 (Boise 46-34, 4Q 6:16); "
            "UTSA +2.5 1Q 0-0, covering by 2.5; North Dakota State not started.")

    # --- LSU 3Q: Dilin Jones 44 TD, 21-10 ---------------------------------------------------------
    add("lsu3-who", "scoring", "lsu3", "who scored that LSU touchdown?", [r"Jones", r"\b44\b"], [r"Raleek|Manning ran"], [LSU],
        hist=True, expect={"last": (LSU, "Dilin Jones 44 Yd Run")})
    add("lsu3-score", "score", "lsu3", "Texas LSU score?", [r"\b10\b", r"\b21\b"], [], [LSU], expect={"score": (LSU, 10, 21)})
    add("lsu3-ints", "turnover", "lsu3", "how many picks has Arch Manning thrown?", [r"\b(2|two)\b"],
        [r"\b(one|three|1|3) (pick|int)", r"\bonce\b"], [LSU],
        key="Manning interceptions so far: 2 (2Q 9:58 by T.Benefield, 3Q 8:50 by T.Benefield). LSU's Leavitt has 3.")
    add("manning-rumor", "trick", "lsu3", "I heard Arch Manning got hurt, is that true?", [NEG + r"|" + NO_INFO],
        [r"\byes\b", r"Manning (was|got|is) (hurt|injured)", r"confirmed"], [LSU], rider=[],
        key="No injury reported for Arch Manning in the feed.")
    add("aggies-wrong-team", "trick", "lsu3", "what's the Texas A&M score?", [NO_INFO + r"|A&M"],
        [r"A&M (is |are )?(up|down|leads|trails|winning|losing)", r"A&M \d"], [LSU], rider=[],
        key="Texas A&M is not in the feed; Texas (at LSU, 21-10 LSU) is a different team.")

    # --- UTSA 3Q: Jimerson (backup) TD pass, 21-10 ---------------------------------------------------
    add("unt-qb-why", "qb", "utsa3", "who's the North Texas QB now and why?", [r"Jimerson", r"Jackson", r"injur|hurt"],
        [], [UTSA], key="North Texas: C.Jimerson Jr. in for T.Jackson, who was reported injured on an 18-yard run 3Q 14:22.")
    add("utsa3-cover", "cover", "utsa3", "is UTSA covering?", [r"\byes|covering", r"\b13\.5\b"], [r"UTSA( \+2\.5)?( is)? not covering|UTSA isn\'?t covering"], [UTSA],
        expect={"cover": (UTSA, "UTSA", 13.5), "score": (UTSA, 10, 21)})
    add("utsa3-td", "scoring", "utsa3", "who threw the North Texas touchdown?", [r"Jimerson", r"Reese", r"\b16\b"], [], [UTSA],
        hist=True, expect={"last": (UTSA, "Max Reese 16 Yd pass from Chris Jimerson Jr.")})

    # --- LSU walk-off ------------------------------------------------------------------------------
    add("lsu-walk-who", "scoring", "lsuwalk", "who scored the Texas TD at the end?", [r"Brown", r"Manning"], [], [LSU], hist=True,
        expect={"last": (LSU, "Raleek Brown 4 Yd pass from Arch Manning")})
    add("lsu-walk-when", "followup", "lsuwalk", "how long ago was that?",
        [r"0:03|3 seconds|seconds (left|to go|remaining)|just|moments?|minute|ago"], [], [LSU], follow="lsu-walk-who", rider=[r"0:03"])
    add("lsu-walk-score", "score", "lsuwalk", "what's the Texas LSU score?", [r"\b22\b", r"\b21\b"], [], [LSU],
        expect={"score": (LSU, 22, 21)})
    add("lsu-not-pool", "trick", "lsuwalk", "is LSU covering?",
        [r"not (a |one of |in )?(the |our |this week'?s )?pool|no (league |pool )?line|isn'?t (a |in |one of)|not one of|"
         r"no spread|don'?t have a (line|spread)|wasn'?t (in|one)|not on (the|our) (board|slate)|outside the pool"],
        [r"covering by", r"LSU (is )?covering"], [LSU], rider=[],
        key="Texas at LSU is not a pool game this week: no league line to cover.")

    # --- UTSA 4Q: Jimerson TD run 21-16 ------------------------------------------------------------
    add("utsa4-update", "score", "utsa4", "UTSA game update?", [r"\b16\b", r"\b21\b"], [], [UTSA], hist=True,
        expect={"score": (UTSA, 16, 21)})
    add("utsa4-cover", "cover", "utsa4", "is UTSA still covering?", [r"\byes|covering|still", r"\b7\.5\b"], [r"UTSA( \+2\.5)?( is)? not covering|UTSA isn\'?t covering"],
        [UTSA], expect={"cover": (UTSA, "UTSA", 7.5)})
    add("utsa4-picks", "picks", "utsa4", "how are my picks doing?",
        [r"Wake", r"Oregon State", r"UTSA", r"North Dakota|NDSU|Hawai"], [r"\b4-0\b"], list(POOL_IDS),
        extra_pairs=[(2, 0), (0, 0), (1, 0), (2, 1), (3, 0)], rider=[r"7\.5"],
        key="Wake Forest WON (49-7, covered by 40.5); Oregon State WON (lost 46-41 but covered +13.5 by 8.5); UTSA +2.5 "
            "covering by 7.5 (21-16, 4Q 8:32); North Dakota State -6.5 up 7-0 in the 2Q, covering by 0.5.")
    add("ndsu2-cover", "cover", "utsa4", "is NDSU covering?", [r"\byes|covering", r"0\.5|half a point|half-point"],
        [r"(North Dakota State|NDSU)( -6\.5)?( is)? not covering|(NDSU|North Dakota State) isn\'?t covering"], [HAW], expect={"cover": (HAW, "North Dakota State", 0.5), "score": (HAW, 7, 0)})

    # --- 11:35 CT: UTSA final, Hawai'i halftime 7-7 ---------------------------------------------------
    add("utsa-final", "score", "late", "final in UTSA North Texas?", [r"\b35\b", r"\b16\b"], [], [UTSA], hist=True,
        expect={"score": (UTSA, 16, 35), "state": (UTSA, "post")})
    add("utsa-final-cover", "cover", "late", "did UTSA cover?", [r"\byes|covered|cash|easily"], [r"didn'?t cover|did not cover"],
        [UTSA], expect={"cover": (UTSA, "UTSA", 21.5)}, rider=[r"21\.5"])
    add("haw-half-cover", "cover", "late", "is NDSU covering?", [NEG, r"\b6\.5\b"], [r"NDSU is covering|North Dakota State is covering"],
        [HAW], expect={"cover": (HAW, "Hawai'i", 6.5)})

    # --- Hawai'i 4Q: Ray James Jr. pick six 21-13 ------------------------------------------------------
    add("haw4-what", "what", "haw4", "what just happened in Hawaii?", [r"James", r"\b49\b", r"intercept|pick"], [], [HAW],
        hist=True, expect={"last": (HAW, "Ray James Jr. 49 Yd Interception Return"), "score": (HAW, 21, 13)})
    add("haw4-cover", "cover", "haw4", "is my NDSU pick covering now?", [r"\byes|covering", r"\b1\.5\b"], [r"(North Dakota State|NDSU)( -6\.5)?( is)? not covering|(NDSU|North Dakota State) isn\'?t covering"],
        [HAW], expect={"cover": (HAW, "North Dakota State", 1.5)})
    add("haw4-picks", "picks", "haw4", "how are my picks doing?", [r"Wake", r"Oregon State", r"UTSA", r"North Dakota|NDSU"],
        [], list(POOL_IDS), extra_pairs=[(3, 0), (2, 0), (1, 0), (4, 0), (3, 1)], rider=[r"1\.5"],
        key="Wake Forest WON, Oregon State WON, UTSA WON (35-16, covered by 21.5); North Dakota State -6.5 up 21-13 in "
            "the 4Q (6:27), covering by 1.5. 3-0 so far with NDSU live.")
    add("haw4-total", "total", "haw4", "how many points have been scored in the Hawaii game?", [r"\b34\b"], [], [HAW],
        expect={"score": (HAW, 21, 13)})

    # --- more at the same moments -----------------------------------------------------------------------
    add("boi1-to", "turnover", "boi1", "has Oregon State turned it over?", [r"\byes|intercept|pick"], [], [BOI],
        key="Yes: the pick six (Travis Anderson 58-yd interception return TD, 1Q 1:47).")
    add("boi-not-started", "trick", "half1", "what's the score of the Boise game?", [NOT_STARTED], [r"\b46\b", r"\b41\b"],
        [BOI], expect={"state": (BOI, "pre")})
    add("unc2-qb-ok", "qb", "unc2", "who's playing QB for UNC?", [r"Edwards"], [r"Burgess"], [UNC],
        key="UNC QB is still starter B.Edwards Jr. at this moment (he fumbled on a sack 2Q 14:10).")
    add("lsu3-poss", "score", "lsu3", "who has the ball in Texas LSU?", [r"Texas"], [], [LSU], rider=[r"Texas (ball|gets the ball|has the ball)"],
        key="After LSU's TD (3Q 5:52) LSU kicked off: Texas has the ball.")
    add("ot-final-cover", "trick", "otfinal", "did Missouri State cover?",
        [r"not (a |one of |in )?(the |our |this week'?s )?pool|no (league |pool )?line|isn'?t (a |in |one of)|not one of|"
         r"no spread|don'?t have a (line|spread)|wasn'?t (in|one)|outside the pool|not on (the|our)"],
        [r"covered by", r"covering by"], [OT], rider=[], key="Not a pool game: no league line.")
    add("haw4-time", "followup", "haw4", "when did that happen?", [r"6:27|4th|fourth|4Q"], [], [HAW], follow="haw4-what",
        rider=[r"6:27"])
    _verify(items, tr)
    return items


def _verify(items: list[dict], tr: Truth) -> None:
    """Every hand-typed key matches the raw play-by-play at its moment."""
    ids = set()
    for it in items:
        assert it["id"] not in ids, it["id"]
        ids.add(it["id"])
        e = it["expect"]
        if "score" in e:
            gid, a, h = e["score"]
            s = tr.at(gid, it["t"])
            assert (s["a"], s["h"]) == (a, h), f"{it['id']}: key {a}-{h}, data {s['a']}-{s['h']}"
        if "state" in e:
            gid, st = e["state"]
            assert tr.at(gid, it["t"])["state"] == st, f"{it['id']}: state"
        if "cover" in e:
            gid, side, m = e["cover"]
            assert tr.cover(gid, it["t"]) == (side, m), f"{it['id']}: cover key {side} {m}, data {tr.cover(gid, it['t'])}"
        if "last" in e:
            gid, txt = e["last"]
            sc = tr.at(gid, it["t"])["scoring"]
            assert sc and sc[-1]["text"].startswith(txt), f"{it['id']}: last score {sc[-1]['text'] if sc else None}"
        if "scored" in e:
            gid, txt = e["scored"]
            assert any(x["text"].startswith(txt) for x in tr.at(gid, it["t"])["scoring"]), f"{it['id']}: {txt}"
        if it["follow"]:
            assert it["follow"] in ids, f"{it['id']}: follow-up before its parent"
        for gid in it["games"]:
            tr.at(gid, it["t"])  # raises in a window where ESPN's score and scoring plays disagree
    assert len(items) == 80, len(items)


# ----------------------------------------------------------------------------
# deterministic grading
# ----------------------------------------------------------------------------

_PAIR = re.compile(r"(?<![\d.+\-:$])(\d{1,2})\s*(?:-|–|to)\s*(\d{1,2})(?![\d:]|\.\d|\s*(?:-?\s*yards?|yd|-yd))", re.I)
_COVER_BY = re.compile(r"\bcover\w*\b[^.?!]{0,40}?\bby\s+(\d{1,2}(?:\.5)?)(?!\d)", re.I)


def allowed(item: dict, tr: Truth) -> tuple[set, set]:
    """Score pairs and cover margins an answer may state at this moment."""
    pairs, margins = set(item["extra_pairs"]), set()
    for gid in set(item["games"]) | set(tr.games):
        s = tr.at(gid, item["t"])
        if s["state"] == "pre":
            continue
        pairs.add(tuple(sorted((s["a"], s["h"]))))
        if gid in item["games"] and item["hist"]:
            pairs |= {tuple(sorted((x["a"], x["h"]))) for x in s["scoring"]}
        cv = tr.cover(gid, item["t"])
        if cv:
            margins.add(cv[1])
            if gid in item["games"]:
                pairs.add(tuple(sorted((s["a"], s["h"]))))
    return pairs, margins


def grade(item: dict, ans: str, tr: Truth) -> tuple[bool, list[str]]:
    a = ans or ""
    why = [f"missing /{r}/" for r in item["need"] if not re.search(r, a, re.I)]
    why += [f"has /{r}/" for r in item["bad"] if re.search(r, a, re.I)]
    pairs, margins = allowed(item, tr)
    for m in _PAIR.finditer(a):
        p = tuple(sorted((int(m.group(1)), int(m.group(2)))))
        if p not in pairs:
            why.append(f"score {m.group(0)} isn't a score at this moment")
    for m in _COVER_BY.finditer(a):
        if float(m.group(1)) not in margins:
            why.append(f"cover margin {m.group(1)} is wrong")
    if item["id"] == "oregon-wrong-team" and re.search(r"\b32\b", a) and not re.search(r"Oregon State|Beavers", a):
        why.append("gave Oregon State's score as Oregon's")
    if not a.strip() or a.startswith("[HTTP"):
        why.append("no answer")
    return not why, why


# ----------------------------------------------------------------------------
# fast mode (model-free regression)
# ----------------------------------------------------------------------------


def _sync(p, clock, t):
    clock.freeze(t)
    p.next_sb, p.next_detail = 0.0, {}
    p.step()


def fast(answers: str | None = None, verbose: bool = False) -> dict:
    import importlib
    import tempfile
    sb, games = R.load_dir(DATA)
    pool, picks = load_pool()
    items = build(games, pool, picks)
    tr = Truth(games, pool)
    clock = R.VClock(0)
    clock.freeze(0)
    srv, _ = R.serve(games, sb, clock)
    tmp = Path(tempfile.mkdtemp(prefix="live-exam-"))
    os.environ["PICKY_LIVE_BASE"] = f"http://127.0.0.1:{srv.server_port}"
    os.environ["PICKY_LIVE_CACHE"] = str(tmp / "live_games.json")
    import live_games as L
    importlib.reload(L)
    L._overlay.clear()
    p = L.Poller(cache=Path(os.environ["PICKY_LIVE_CACHE"]), pool_ids_fn=lambda: list(POOL_IDS))
    recorded = {}
    if answers:
        for r in json.loads(Path(answers).read_text()).get("results") or []:
            recorded[r["id"]] = r
    fails, n_checks, cur_t = [], 0, None
    re_graded = {"pass": 0, "total": 0, "fails": []}
    for it in sorted(items, key=lambda x: x["t"]):
        if it["t"] != cur_t:
            _sync(p, clock, it["t"])
            cur_t = it["t"]
        prev = next(x["q"] for x in items if x["id"] == it["follow"]) if it["follow"] else ""
        ctx = fake_ctx(pool, picks, games, tr, it["t"])
        rider = L.apply_to_ctx(ctx, it["q"], refresh=False, prev=prev)  # what chat.py does (prev = last user turn)
        fact = L.required_fact(ctx, it["q"])
        text = rider + "\n" + (fact[1] if fact else "")
        for rx in it["rider"]:
            n_checks += 1
            if not re.search(rx, text, re.I | re.S):
                fails.append(f"{it['id']}: rider/fact lacks /{rx}/")
        if fact:
            n_checks += 1
            ok, why = grade({**it, "need": [], "bad": it["bad"]}, fact[1], tr)
            if not ok:
                fails.append(f"{it['id']}: computed fact wrong: {fact[1]!r} {why}")
            chk = L.claim_check(ctx.get("live_rider_games"))
            for sent in re.split(r"(?<=[.!?])\s+", fact[1].strip()):
                n_checks += 1
                if chk and not chk(sent):
                    fails.append(f"{it['id']}: claim check cuts the computed fact {sent!r}")
            n_checks += 1
            if not fact[0](fact[1]):
                fails.append(f"{it['id']}: computed fact fails its own check")
        # wrong numbers must be cut
        chk = L.claim_check(ctx.get("live_rider_games"))
        e = it["expect"]
        if chk and "score" in e:
            gid, a, h = e["score"]
            s = tr.at(gid, it["t"])
            n_checks += 2
            if not chk(f"{s['away']} {a}, {s['home']} {h}."):
                fails.append(f"{it['id']}: claim check cut the right score")
            if chk(f"{s['away']} {a + 3}, {s['home']} {h}."):
                fails.append(f"{it['id']}: claim check kept a wrong score")
        if chk and "cover" in e:
            gid, side, m = e["cover"]
            n_checks += 2
            if not chk(f"{side} is covering by {m:g}."):
                fails.append(f"{it['id']}: claim check cut the right cover margin")
            if chk(f"{side} is covering by {m + 2:g}."):
                fails.append(f"{it['id']}: claim check kept a wrong cover margin")
        if it["id"] in recorded:
            r = recorded[it["id"]]
            kept = " ".join(s for s in re.split(r"(?<=[.!?])\s+", r.get("a") or "") if not chk or chk(s))
            ok, why = grade(it, kept, tr)
            re_graded["total"] += 1
            re_graded["pass"] += ok
            if not ok:
                re_graded["fails"].append({"id": it["id"], "why": why})
        if verbose:
            print(f"--- {it['id']} [{it['q']}]\n{rider}\nFACT: {fact[1] if fact else None}\n")
    srv.shutdown()
    return {"items": len(items), "checks": n_checks, "fails": fails, "recorded": re_graded if recorded else None}


def fake_ctx(pool: dict, picks: dict, games: dict, tr: Truth, t: float) -> dict:
    """The slice of chat ctx apply_to_ctx reads: this week's pool games (league names + locked lines, final rows
    as Supabase has them) and their all_games live rows."""
    gw, rows = [], []
    for gid, p in pool.items():
        fin = tr.at(gid, 1e12)
        gw.append({"id": int(gid), "away": p["away"], "home": p["home"], "line": p["line"], "status": "final",
                   "winner": None, "tiebreaker": False})
        rows.append({"GameId": int(gid), "AwayPts": fin["a"], "HomePts": fin["h"], "Status": "final"})
    return {"games_this_week": gw, "live_games": rows, "you": {"team_name": PERSONA},
            "revealed_picks_this_week": {PERSONA: [{"game_id": int(g), "pick": s} for g, s in (picks.get(PERSONA) or {}).items()]}}


# ----------------------------------------------------------------------------
# ask mode (the model, inside the exam lock)
# ----------------------------------------------------------------------------


def ask(a) -> int:
    import importlib
    import httpx
    sb, games = R.load_dir(DATA)
    pool, picks = load_pool()
    items = build(games, pool, picks)
    tr = Truth(games, pool)
    clock = R.VClock(0)
    clock.freeze(0)
    srv, _ = R.serve(games, sb, clock, a.espn_port)
    os.environ["PICKY_LIVE_BASE"] = f"http://127.0.0.1:{a.espn_port}"
    os.environ["PICKY_LIVE_CACHE"] = a.cache
    import live_games as L
    importlib.reload(L)
    p = L.Poller(cache=Path(a.cache), pool_ids_fn=lambda: list(POOL_IDS))
    import run as exam  # noqa: E402 - member handle map + group secret
    handle = exam.group_handles().get(a.persona) or ""
    secret = os.environ.get("PICKY_GROUP_SECRET") or os.popen(
        f"security find-generic-password -a drake -s {exam.GROUP_SECRET_SERVICE} -w").read().strip()
    only = set((a.only or "").split(",")) - {""}
    if only:  # a follow-up needs its parent asked first
        only |= {it["follow"] for it in items if it["id"] in only and it["follow"]}
    hist = Path(a.history)
    results = []
    order = sorted(items, key=lambda x: (x["t"], 0))
    # follow-ups go right after their parent
    seq = []
    for it in order:
        if it["follow"]:
            continue
        seq.append(it)
        seq += [f for f in items if f["follow"] == it["id"]]
    cur_t = None
    with httpx.Client(timeout=httpx.Timeout(connect=10, read=600, write=10, pool=10)) as c:
        for it in seq:
            if only and it["id"] not in only:
                continue
            if it["t"] != cur_t:
                cur_t = it["t"]
            _sync(p, clock, it["t"])  # fresh data before every question
            if not it["follow"]:
                hist.write_text("{}")  # each question starts a fresh DM thread; follow-ups keep their parent's
            t0 = time.time()
            r = c.post(a.group, headers={"X-Picky-Group-Secret": secret},
                       json={"handle": handle, "text": it["q"], "chat": f"live80-{a.tag}-{it['id']}", "dm": True})
            ans = (r.json().get("reply") or "") if r.status_code == 200 else f"[HTTP {r.status_code}]"
            ok, why = grade(it, ans, tr)
            rec = {"id": it["id"], "cat": it["cat"], "moment": it["moment"], "q": it["q"], "a": ans, "ok": ok, "why": why,
                   "reply_s": round(time.time() - t0, 1)}
            results.append(rec)
            print(json.dumps(rec), flush=True)
            if a.report:  # partial report after every answer (a long run can be cut short)
                Path(a.report).write_text(json.dumps({"summary": summary(results), "results": results}, indent=1))
    srv.shutdown()
    s = summary(results)
    print(json.dumps(s))
    if a.report:
        Path(a.report).write_text(json.dumps({"summary": s, "results": results}, indent=1))
    return 0 if s["passed"] == s["total"] else 1


def summary(results: list[dict]) -> dict:
    lat = sorted(r["reply_s"] for r in results)
    q = (lambda f: lat[min(len(lat) - 1, int(f * len(lat)))] if lat else None)
    by = {}
    for r in results:
        b = by.setdefault(r["cat"], [0, 0])
        b[0] += r["ok"]
        b[1] += 1
    return {"passed": sum(r["ok"] for r in results), "total": len(results), "p50_s": q(0.5), "p90_s": q(0.9),
            "max_s": lat[-1] if lat else None, "by_cat": {k: f"{v[0]}/{v[1]}" for k, v in sorted(by.items())},
            "failed": [r["id"] for r in results if not r["ok"]]}


# ----------------------------------------------------------------------------
# Opus judge (offline)
# ----------------------------------------------------------------------------

JUDGE_SCHEMA = {"type": "object", "properties": {"items": {"type": "array", "items": {"type": "object", "properties": {
    "id": {"type": "string"}, "wrong_facts": {"type": "array", "items": {"type": "string"}},
    "answers_question": {"type": "boolean"}, "note": {"type": "string"}},
    "required": ["id", "wrong_facts", "answers_question", "note"]}}}, "required": ["items"]}

JUDGE_PROMPT = """You grade a college-football group-chat bot ("Picky") on LIVE game questions. Each item gives the
question, what the bot knew at that moment (FACTS, from ESPN's play-by-play at that instant; injuries are "reported"
from the play-by-play), any grading NOTE, and the bot's ANSWER (a follow-up item also shows the earlier exchange).

For each item list every WRONG FACT in the answer: a score, margin, cover status/number, clock/quarter, player,
yardage, play type, time, injury or QB claim that contradicts FACTS or isn't supported by them (an invented event,
a guessed injury severity, a future result, a made-up line). Jokes, opinions, trash talk and awkward wording are NOT
wrong facts. Saying "reported"/"per ESPN" is fine; full first names matching the initials in FACTS are fine. A game the
FACTS say isn't started must get no score. If the question names a team not in FACTS, the bot must not give another
team's numbers as that team's. Then say whether it answers what was asked (true/false). Be strict on facts, lenient on style.

Return JSON {"items": [{"id", "wrong_facts": [...], "answers_question": bool, "note": "<10 words"}]}.

"""


def judge(a) -> int:
    import dossier
    sb, games = R.load_dir(DATA)
    pool, picks = load_pool()
    items = {it["id"]: it for it in build(games, pool, picks)}
    tr = Truth(games, pool)
    rep = json.loads(Path(a.results).read_text())
    res = rep["results"]
    by_id = {r["id"]: r for r in res}
    blocks = []
    for r in res:
        it = items[r["id"]]
        gids = it["games"] or []
        facts = "\n".join(tr.facts(g, it["t"]) for g in dict.fromkeys(gids))
        if it["cat"] == "picks":
            facts += "\nThe asker (Chuck P) picked: " + ", ".join(f"{pool[g]['away']} at {pool[g]['home']}: {s}"
                                                                for g, s in (picks.get(PERSONA) or {}).items())
        prev = ""
        if it["follow"] and it["follow"] in by_id:
            prev = f"EARLIER: Q: {by_id[it['follow']]['q']}\nA: {by_id[it['follow']]['a']}\n"
        when = dt.datetime.fromtimestamp(it["t"], dt.timezone(dt.timedelta(hours=-6))).strftime("%a %-I:%M %p CT")
        blocks.append(f"### {r['id']} (asked {when})\n{prev}QUESTION: {r['q']}\nFACTS:\n{facts}\n"
                      f"NOTE: {it['key'] or '-'}\nANSWER: {r['a']}\n")
    out = []
    for i in range(0, len(blocks), a.batch):
        part = blocks[i:i + a.batch]
        got = dossier.run_claude(JUDGE_PROMPT + "\n".join(part), timeout=900, schema=JUDGE_SCHEMA, tools="")
        out += got.get("items") or []
        print(f"judged {min(i + a.batch, len(blocks))}/{len(blocks)}", file=sys.stderr, flush=True)
    jd = {x["id"]: x for x in out}
    both = 0
    for r in res:
        j = jd.get(r["id"]) or {}
        r["judge"] = j
        r["judge_ok"] = bool(j) and not j.get("wrong_facts") and j.get("answers_question", False)
        both += r["ok"] and r["judge_ok"]
    rep["judge_summary"] = {"code_pass": sum(r["ok"] for r in res), "judge_pass": sum(r["judge_ok"] for r in res),
                            "both_pass": both, "total": len(res),
                            "judge_flags": [{"id": r["id"], "wrong": r["judge"].get("wrong_facts"),
                                             "answers": r["judge"].get("answers_question")}
                                            for r in res if not r["judge_ok"]]}
    Path(a.out or a.results).write_text(json.dumps(rep, indent=1))
    print(json.dumps(rep["judge_summary"], indent=1))
    return 0


# ----------------------------------------------------------------------------


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fast")
    f.add_argument("--answers", default="")
    f.add_argument("-v", action="store_true")
    k = sub.add_parser("ask")
    k.add_argument("--group", required=True)
    k.add_argument("--espn-port", type=int, default=8796)
    k.add_argument("--cache", required=True)
    k.add_argument("--history", required=True, help="the side sidecar's PICKY_IMESSAGE_HISTORY file")
    k.add_argument("--persona", default=PERSONA)
    k.add_argument("--tag", default=dt.datetime.now().strftime("%H%M"))
    k.add_argument("--only", default="")
    k.add_argument("--report", default="")
    j = sub.add_parser("judge")
    j.add_argument("--results", required=True)
    j.add_argument("--out", default="")
    j.add_argument("--batch", type=int, default=20)
    sub.add_parser("show")
    a = ap.parse_args(argv)
    if a.cmd == "fast":
        r = fast(a.answers or None, a.v)
        print(json.dumps(r, indent=1))
        return 0 if not r["fails"] and not (r["recorded"] and r["recorded"]["fails"]) else 1
    if a.cmd == "show":
        sb, games = R.load_dir(DATA)
        pool, picks = load_pool()
        for it in build(games, pool, picks):
            print(json.dumps({k: it[k] for k in ("id", "cat", "moment", "q", "need", "bad", "key")}))
        return 0
    return {"ask": ask, "judge": judge}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
