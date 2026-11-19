"""
Unit-vs-unit matchups for any FBS game, computed from the CFBD cache only
(datasets 'advanced' + 'team_stats'; no API calls). Every unit is ranked across
FBS (teams with SP+), and each offense-vs-defense pairing is DECIDED here in
words ("edge Northwestern's run defense"), so the 12B never has to compare raw
floats or fall back on overall SP+ (genius exam 2026-11-17: "can Minnesota run
on Northwestern?" got the SP+ ranks).

  ask_units(q)                      -> the units a text asks about ([] = none)
  question_lines(q, away, home)     -> the asked unit's decided edge(s), asked direction first
  card_lines(away, home)            -> the clear edges for a full GAME CARD (compact)
  edges_for(side, away, home)       -> reasons (decided sentences) that favor one side

Units: rush (run game vs run D), pass (pass game vs pass D), protect (pass rush /
sacks vs protection), redzone (points per scoring opportunity), turnovers
(giveaways/takeaways + havoc), explosive (big plays), pace (plays per game),
eff (overall EPA/play: 'can X move the ball / score on Y').
"""

from __future__ import annotations

import json
import re
import sqlite3

import cfbd

GAP = 25          # rank gap (of ~136) that counts as a clear edge
_MEMO: dict = {}


def _f(v):
    return float(v) if isinstance(v, (int, float)) else None


def _get(d: dict, *path):
    for k in path:
        d = (d or {}).get(k) if isinstance(d, dict) else None
    return _f(d)


def _per(n, d):
    n, d = _f(n), _f(d)
    return n / d if n is not None and d else None


# metric -> (getter(adv_row, stats), higher is better)
METRICS = {
    "rush_o": (lambda a, s: _get(a, "offense", "rushingPlays", "ppa"), True),
    "rush_d": (lambda a, s: _get(a, "defense", "rushingPlays", "ppa"), False),
    "pass_o": (lambda a, s: _get(a, "offense", "passingPlays", "ppa"), True),
    "pass_d": (lambda a, s: _get(a, "defense", "passingPlays", "ppa"), False),
    # team_stats: 'sacks' = sacks the defense made, 'sacksOpponent' = sacks allowed (checked vs player stats)
    "rushpass_d": (lambda a, s: _per(s.get("sacks"), (s.get("passAttemptsOpponent") or 0) + (s.get("sacks") or 0)), True),
    "protect_o": (lambda a, s: _per(s.get("sacksOpponent"), (s.get("passAttempts") or 0) + (s.get("sacksOpponent") or 0)), False),
    "ppo_o": (lambda a, s: _get(a, "offense", "pointsPerOpportunity"), True),
    "ppo_d": (lambda a, s: _get(a, "defense", "pointsPerOpportunity"), False),
    # 'turnovers' = giveaways (fumbles lost + INTs thrown), 'turnoversOpponent' = takeaways
    "give_o": (lambda a, s: _per(s.get("turnovers"), s.get("games")), False),
    "take_d": (lambda a, s: _per(s.get("turnoversOpponent"), s.get("games")), True),
    "havoc_d": (lambda a, s: _get(a, "defense", "havoc", "total"), True),
    "havoc_o": (lambda a, s: _get(a, "offense", "havoc", "total"), False),
    "expl_o": (lambda a, s: _get(a, "offense", "explosiveness"), True),
    "expl_d": (lambda a, s: _get(a, "defense", "explosiveness"), False),
    "pace": (lambda a, s: _per(_get(a, "offense", "plays"), s.get("games")), True),
    "eff_o": (lambda a, s: _get(a, "offense", "ppa"), True),
    "eff_d": (lambda a, s: _get(a, "defense", "ppa"), False),
    # success rate: overall, standard / passing downs, run / pass splits (CFBD names, checked vs the cache)
    "succ_o": (lambda a, s: _get(a, "offense", "successRate"), True),
    "succ_d": (lambda a, s: _get(a, "defense", "successRate"), False),
    "sd_succ_o": (lambda a, s: _get(a, "offense", "standardDowns", "successRate"), True),
    "sd_succ_d": (lambda a, s: _get(a, "defense", "standardDowns", "successRate"), False),
    "pd_succ_o": (lambda a, s: _get(a, "offense", "passingDowns", "successRate"), True),
    "pd_succ_d": (lambda a, s: _get(a, "defense", "passingDowns", "successRate"), False),
    "rush_succ_o": (lambda a, s: _get(a, "offense", "rushingPlays", "successRate"), True),
    "rush_succ_d": (lambda a, s: _get(a, "defense", "rushingPlays", "successRate"), False),
    "pass_succ_o": (lambda a, s: _get(a, "offense", "passingPlays", "successRate"), True),
    "pass_succ_d": (lambda a, s: _get(a, "defense", "passingPlays", "successRate"), False),
    # run game vs run D: line yards, stuff rate, power success (CFBD has no opportunity rate)
    "line_o": (lambda a, s: _get(a, "offense", "lineYards"), True),
    "line_d": (lambda a, s: _get(a, "defense", "lineYards"), False),
    "stuff_o": (lambda a, s: _get(a, "offense", "stuffRate"), False),
    "stuff_d": (lambda a, s: _get(a, "defense", "stuffRate"), True),
    "power_o": (lambda a, s: _get(a, "offense", "powerSuccess"), True),
    "power_d": (lambda a, s: _get(a, "defense", "powerSuccess"), False),
    "havoc_f7_d": (lambda a, s: _get(a, "defense", "havoc", "frontSeven"), True),
    "havoc_db_d": (lambda a, s: _get(a, "defense", "havoc", "db"), True),
    # field position: averageStart is yards to the goal (checked vs SP+ 11-18), so lower is better on offense
    "fp_o": (lambda a, s: _get(a, "offense", "fieldPosition", "averageStart"), False),
    "fp_d": (lambda a, s: _get(a, "defense", "fieldPosition", "averageStart"), True),
}


def _advanced_rows() -> tuple[float | None, list]:
    try:
        db = sqlite3.connect(f"file:{cfbd.DB_PATH}?mode=ro", uri=True)
        row = db.execute("select fetched_at, json from datasets where name='advanced'").fetchone()
        db.close()
    except sqlite3.Error:
        return None, []
    return (row[0], json.loads(row[1])) if row else (None, [])


def table(idx: dict | None = None, adv: list | None = None) -> dict:
    """{norm team: {'name', metric: value, metric+'_rank': n, 'raw': adv row, 'stats': team_stats}} over FBS
    (teams with SP+ in the index). Memoized per cache refresh."""
    idx = idx if idx is not None else cfbd.load_index()
    if not idx:
        return {}
    stamp = None
    if adv is None:
        stamp, adv = _advanced_rows()
        key = (stamp, idx.get("fetched_at"))
        if _MEMO.get("key") == key and key[0] is not None:
            return _MEMO["table"]
    out = {}
    for r in adv or []:
        nm = r.get("team") or ""  # 'App State', 'UConn', 'UL Monroe': the alias table, so ranks match the cards' (138, not 134)
        t = cfbd.resolve_team(nm, idx)
        if not t or not t.get("sp"):
            continue
        s = t.get("stats") or {}
        row = {"name": t["name"], "raw": r, "stats": s}
        for m, (fn, _) in METRICS.items():
            try:
                row[m] = fn(r, s)
            except (TypeError, ZeroDivisionError):
                row[m] = None
        out[cfbd.norm(t["name"])] = row
    for m, (_, high) in METRICS.items():
        pool = sorted((r for r in out.values() if r.get(m) is not None), key=lambda r: r[m], reverse=high)
        for i, r in enumerate(pool, 1):
            r[m + "_rank"] = i
    out["_n"] = len([k for k in out])
    if stamp is not None:
        _MEMO.update({"key": (stamp, idx.get("fetched_at")), "table": out})
    return out


def _row(team: str, tab: dict) -> dict | None:
    return tab.get(cfbd.norm(team)) or tab.get(cfbd.ALIASES.get(cfbd.norm(team), "~"))


def tier(rank: int, n: int = 136) -> str:
    """A rank in words (1 = best)."""
    if rank <= 20:
        return "top-20"
    if rank <= 45:
        return "good"
    if rank <= n - 46:
        return "middling"
    if rank <= n - 20:
        return "weak"
    return "bottom-20"


def _pct(x) -> str:
    return f"{round(x * 100):d}%"


def _poss(name: str) -> str:
    return name + ("'" if name.endswith("s") else "'s")


# ---------------------------------------------------------------------------
# one offense vs one defense, decided
# ---------------------------------------------------------------------------

PAIRS = {  # unit -> (offense metric, defense metric, offense label, defense label, what's ranked)
    "rush": ("rush_o", "rush_d", "run game", "run defense", "rushing EPA/play"),
    "pass": ("pass_o", "pass_d", "passing game", "pass defense", "passing EPA/play"),
    "protect": ("protect_o", "rushpass_d", "pass protection", "pass rush", "sack rate"),
    "redzone": ("ppo_o", "ppo_d", "finishing drives", "red-zone defense", "points per scoring chance"),
    "explosive": ("expl_o", "expl_d", "big-play offense", "big-play defense", "explosiveness"),
    "eff": ("eff_o", "eff_d", "offense", "defense", "EPA/play"),
}
# the second ranked stat a pairing states and decides on (with EPA/play): success rate
SECOND = {"rush": ("rush_succ_o", "rush_succ_d", "rush success rate"),
          "pass": ("pass_succ_o", "pass_succ_d", "pass success rate"),
          "eff": ("succ_o", "succ_d", "success rate")}


def pair_edge(unit: str, off: str, de: str, tab: dict | None = None) -> dict | None:
    """{'unit','off','def','ro','rd','winner','gap','text'} for off's offense vs de's defense on one unit."""
    tab = tab if tab is not None else table()
    o, d = _row(off, tab), _row(de, tab)
    om, dm, ol, dl, what = PAIRS[unit]
    if not o or not d or not o.get(om + "_rank") or not d.get(dm + "_rank"):
        return None
    n = tab.get("_n") or 136
    ro, rd = o[om + "_rank"], d[dm + "_rank"]
    on, dn = o["name"], d["name"]
    so = sd = None
    if unit in SECOND:
        sm, sdm, _ = SECOND[unit]
        so, sd = o.get(sm + "_rank"), d.get(sdm + "_rank")
    both = so is not None and sd is not None
    ao, ad = ((ro + so) / 2, (rd + sd) / 2) if both else (ro, rd)  # decided on EPA and success rate together
    extra_o = extra_d = ""
    if unit == "rush":
        rate = _get(o["raw"], "offense", "rushingPlays", "rate")
        if rate:
            extra_o = f", runs {_pct(rate)} of plays" + (" (option)" if rate >= 0.72 else "")
        stuff = _get(d["raw"], "defense", "stuffRate")
        if stuff:
            extra_d = f", stuffs {_pct(stuff)} of runs"
    elif unit == "protect":
        extra_o = f", allows a sack on {_pct(o['protect_o'])} of dropbacks"
        extra_d = f", sacks on {_pct(d['rushpass_d'])} of dropbacks"
        f7 = _get(d["raw"], "defense", "havoc", "frontSeven")
        if f7 is not None and (tab.get("_f7") or {}).get(cfbd.norm(dn)):
            extra_d += f", front-seven havoc #{tab['_f7'][cfbd.norm(dn)]}"
    gap = ad - ao  # > 0: the offense ranks better than the defense it faces
    if gap >= GAP:
        winner, verdict = on, f"edge {_poss(on)} {ol}"
    elif gap <= -GAP:
        winner, verdict = dn, f"edge {_poss(dn)} {dl}"
    elif ao <= 30 and ad <= 30:
        winner, verdict = None, "strength on strength, no clear edge"
    elif ao >= n - 35 and ad >= n - 35:
        winner, verdict = None, "both weak, no clear edge"
    else:
        winner, verdict = None, "close, no clear edge"
    to, td = tier(round(ao), n), tier(round(ad), n)
    if unit == "protect":
        text = (f"{_poss(dn)} {dl} (#{rd} in sack rate, {td}{extra_d}) vs {_poss(on)} {ol} "
                f"(#{ro}, {to}{extra_o}): {verdict}")
    elif both:
        sw = SECOND[unit][2]
        text = (f"{_poss(on)} {ol} (#{ro} in {what}, #{so} in {sw}, {to}{extra_o}) vs {_poss(dn)} {dl} "
                f"(#{rd}, #{sd}, {td}{extra_d}): {verdict}")
    else:
        text = (f"{_poss(on)} {ol} (#{ro} in {what}, {to}{extra_o}) vs {_poss(dn)} {dl} "
                f"(#{rd}, {td}{extra_d}): {verdict}")
    return {"unit": unit, "off": on, "def": dn, "ro": ro, "rd": rd, "so": so, "sd": sd, "winner": winner,
            "gap": round(abs(gap)), "text": text}


def profile(team: str, tab: dict | None = None, short: bool = False) -> str:
    """One team's advanced stats with FBS ranks, the line the brain sees instead of raw yards per game.
    short=True: the headline ranks only (every pool game's block sits in the cached prefix). '' when the team has
    no advanced row."""
    tab = tab if tab is not None else table()
    r = _row(team, tab)
    if not r:
        return ""

    def k(m):
        return f" (#{r[m + '_rank']})" if r.get(m + "_rank") else ""

    def epa(m):
        return f"{r[m]:+.2f}".replace("-0.00", "0.00").replace("+0.00", "0.00") + k(m) if r.get(m) is not None else None

    def pct(m):
        return f"{_pct(r[m])}{k(m)}" if r.get(m) is not None else None

    def num(m, f="{:.1f}"):
        return f.format(r[m]) + k(m) if r.get(m) is not None else None

    def own(m):
        return f"own {round(100 - r[m])}{k(m)}" if r.get(m) is not None else None

    def rk(m):  # secondary stats: the rank alone reads clearer in a text than another decimal
        return f"#{r[m + '_rank']}" if r.get(m + "_rank") else None

    if short:
        o = [("EPA/play", rk("eff_o")), ("success", rk("succ_o")), ("rush EPA", rk("rush_o")),
             ("pass EPA", rk("pass_o")), ("finishing drives", rk("ppo_o"))]
        d = [("EPA/play", rk("eff_d")), ("success allowed", rk("succ_d")), ("rush D EPA", rk("rush_d")),
             ("pass D EPA/play", epa("pass_d")), ("pass success allowed", pct("pass_succ_d")), ("havoc", rk("havoc_d"))]
        ob = ", ".join(f"{lab} {v}" for lab, v in o if v)
        db = ", ".join(f"{lab} {v}" for lab, v in d if v)
        return f"FBS ranks, garbage time out, not opp-adjusted: O {ob} | D {db}" if ob or db else ""
    o = [("EPA/play", epa("eff_o")), ("success", pct("succ_o")), ("standard downs", rk("sd_succ_o")),
         ("passing downs", rk("pd_succ_o")), ("rush EPA/play", rk("rush_o")), ("rush success", rk("rush_succ_o")),
         ("line yards", rk("line_o")), ("pass EPA/play", rk("pass_o")), ("pass success", rk("pass_succ_o")),
         ("explosiveness", rk("expl_o")), ("pts per scoring chance", num("ppo_o")), ("avg start", own("fp_o"))]
    d = [("EPA/play", epa("eff_d")), ("success allowed", pct("succ_d")), ("rush D EPA/play", rk("rush_d")),
         ("rush success allowed", rk("rush_succ_d")), ("stuff rate", rk("stuff_d")),
         ("pass D EPA/play", epa("pass_d")), ("pass success allowed", pct("pass_succ_d")),
         ("havoc", pct("havoc_d")), ("front-seven havoc", rk("havoc_f7_d")), ("DB havoc", rk("havoc_db_d")),
         ("pts per scoring chance allowed", num("ppo_d"))]
    ob = ", ".join(f"{lab} {v}" for lab, v in o if v)
    db = ", ".join(f"{lab} {v}" for lab, v in d if v)
    if not ob and not db:
        return ""
    return (f"advanced (CFBD, garbage time out, not opponent-adjusted: SP+ adjusts; FBS ranks): "
            f"offense {ob}; defense {db}")


def short_edge(e: dict) -> str:
    """A decided pairing as one plain sentence: the ranks and the verdict, no style extras."""
    u = e["unit"]
    _, _, ol, dl, what = PAIRS[u]
    sw = SECOND[u][2] if u in SECOND and e.get("so") and e.get("sd") else ""
    if u == "protect":
        return (f"{_poss(e['def'])} {dl} is #{e['rd']} in {what} and {_poss(e['off'])} {ol} #{e['ro']}, "
                f"so {_verdict_words(e)}.")
    o = f"#{e['ro']} in {what}" + (f" and #{e['so']} in {sw}" if sw else "")
    d = f"#{e['rd']}" + (f" and #{e['sd']}" if sw else "")
    return f"{_poss(e['off'])} {ol} is {o}; {_poss(e['def'])} {dl} is {d}, so {_verdict_words(e)}."


def _verdict_words(e: dict) -> str:
    return e["text"].rsplit(": ", 1)[-1]


YARDS_ASK = cfbd.YARDS_ASK
_RAW_YARDS = re.compile(r"\b\d{2,3}(\.\d+)?\s*(total |rushing |rush |passing |pass |offensive )?(yards|yds)\s*(per|a|/)\s*game\b"
                        r"|\b\d{2,3}(\.\d+)?\s*(ypg|yds/g)\b", re.I)
_DEF_VERB = re.compile(r"\b(allow\w*|gives? up|giving up|gave up|surrender\w*|yield\w*|defen[cs]e)\b", re.I)
_SENT = re.compile(r"(?<=[.!?])\s+")
_PLAYER = re.compile(r"\b(qbs?|quarterbacks?|rbs?|running backs?|tailbacks?|receivers?|wrs?|tight ends?|freshman|"
                     r"sophomore|junior|senior|he|his|him|backup|starter)\b", re.I)  # a player's yards, not a unit's


def _named(sentence: str, tab: dict) -> list[str]:
    """Teams in the units table named in a sentence, in order."""
    qn = f" {cfbd.norm(re.sub(r"['’]s\b", "", sentence))} "
    hits = []
    for k, r in tab.items():
        if k.startswith("_"):
            continue
        p = _pos(r["name"], qn)
        if p >= 0:
            hits.append((p, r["name"]))
    hits.sort()
    out = []
    for p, nm in hits:  # 'Texas' inside 'Texas Tech': keep the longer name at a spot
        if out and out[-1][0] == p:
            if len(nm) > len(out[-1][1]):
                out[-1] = (p, nm)
            continue
        out.append((p, nm))
    return [nm for _, nm in out]


def swap_raw_yards(text: str, q: str = "", tab: dict | None = None) -> str:
    """A draft sentence that states raw yards per game is swapped for the ranked advanced line for that unit
    (EPA/play + success rate, FBS ranks), unless the question asked for yards. Sentences about teams with no
    advanced row are left alone."""
    if not text or YARDS_ASK.search(q or "") or not _RAW_YARDS.search(text):
        return text
    tab = tab if tab is not None else table()
    if not tab:
        return text
    out = []
    for sent in _SENT.split(text.strip()):
        if not _RAW_YARDS.search(sent) or _PLAYER.search(sent):
            out.append(sent)
            continue
        unit = ("rush" if re.search(r"\b(rush\w*|run\w*|ground)\b", sent, re.I)
                else "pass" if re.search(r"\b(pass\w*|air|throw\w*)\b", sent, re.I) else "eff")
        names = _named(sent, tab)
        new = None
        if len(names) >= 2:
            a, b = names[0], names[1]
            off = ask_offense(sent, a, b) or a
            e = pair_edge(unit, off, b if off == a else a, tab)
            new = short_edge(e) if e else None
        elif len(names) == 1:
            r = _row(names[0], tab)
            om, dm, ol, dl, what = PAIRS[unit]
            side, m, lab = (("d", dm, dl) if _DEF_VERB.search(sent) else ("o", om, ol))
            sm = SECOND.get(unit, (None, None, ""))[0 if side == "o" else 1]
            if r.get(m + "_rank"):
                new = (f"{_poss(r['name'])} {lab} is #{r[m + '_rank']} in {what}"
                       + (f" and #{r[sm + '_rank']} in {SECOND[unit][2]}" if sm and r.get(sm + "_rank") else "")
                       + f" ({tier(r[m + '_rank'], tab.get('_n') or 136)}).")
        out.append(new or sent)
    return " ".join(out)


def turnovers_edge(away: str, home: str, tab: dict | None = None) -> dict | None:
    tab = tab if tab is not None else table()
    rows = [_row(t, tab) for t in (away, home)]
    if not all(rows) or not all(r.get("give_o_rank") and r.get("take_d_rank") for r in rows):
        return None
    n = tab.get("_n") or 136
    bits, score = [], []
    for r in rows:
        s = r["stats"]
        bits.append(f"{r['name']} has {int(s.get('turnoversOpponent') or 0)} takeaways (#{r['take_d_rank']}) and "
                    f"{int(s.get('turnovers') or 0)} giveaways (#{r['give_o_rank']} fewest) in {int(s.get('games') or 0)} games")
        score.append(r["take_d_rank"] + r["give_o_rank"])
    a, h = rows
    diff = score[1] - score[0]  # > 0: away better
    winner = a["name"] if diff >= 2 * GAP else h["name"] if diff <= -2 * GAP else None
    hv = ""
    if all(r.get("havoc_d_rank") for r in rows):
        hv = f"; defensive havoc {a['name']} #{a['havoc_d_rank']}, {h['name']} #{h['havoc_d_rank']}"
    verdict = (f"turnover edge {winner} (margins this big tend to regress, so the market may already price it)"
               if winner else "no clear turnover edge")
    return {"unit": "turnovers", "off": None, "def": None, "winner": winner, "gap": abs(diff) // 2,
            "text": "; ".join(bits) + hv + f": {verdict}", "n": n}


def pace_edge(away: str, home: str, tab: dict | None = None) -> dict | None:
    tab = tab if tab is not None else table()
    rows = [_row(t, tab) for t in (away, home)]
    if not all(rows) or not all(r.get("pace_rank") for r in rows):
        return None
    n = tab.get("_n") or 136
    a, h = rows
    def speed(r):
        k = r["pace_rank"]
        return "fast" if k <= 40 else "slow" if k >= n - 40 else "average pace"
    if a["pace_rank"] <= 40 and h["pace_rank"] <= 40:
        verdict = "both play fast: more snaps and possessions, shootout-friendly (over lean on pace alone)"
    elif a["pace_rank"] >= n - 40 and h["pace_rank"] >= n - 40:
        verdict = "both play slow: fewer possessions, a grind (under lean on pace alone, and hard for a big favorite to cover)"
    else:
        fast = a if a["pace_rank"] < h["pace_rank"] else h
        verdict = (f"pace clash: {fast['name']} wants to speed it up" if abs(a["pace_rank"] - h["pace_rank"]) >= GAP
                   else "similar pace, no real tempo edge")
    text = (f"{a['name']} runs {round(a['pace']):d} plays a game (#{a['pace_rank']}, {speed(a)}), {h['name']} "
            f"{round(h['pace']):d} (#{h['pace_rank']}, {speed(h)}): {verdict}")
    return {"unit": "pace", "off": None, "def": None, "winner": None, "gap": 0, "text": text}


def _f7_ranks(tab: dict) -> dict:
    vals = sorted(((k, _get(r["raw"], "defense", "havoc", "frontSeven")) for k, r in tab.items()
                   if not k.startswith("_")), key=lambda x: -(x[1] if x[1] is not None else -9))
    return {k: i for i, (k, v) in enumerate(vals, 1) if v is not None}


def edges(unit: str, away: str, home: str, tab: dict | None = None) -> list[dict]:
    """Both directions for a paired unit (away offense first), one game-level edge otherwise."""
    tab = tab if tab is not None else table()
    if unit == "protect" and "_f7" not in tab:
        tab["_f7"] = _f7_ranks(tab)
    if unit == "turnovers":
        e = turnovers_edge(away, home, tab)
        return [e] if e else []
    if unit == "pace":
        e = pace_edge(away, home, tab)
        return [e] if e else []
    return [e for e in (pair_edge(unit, away, home, tab), pair_edge(unit, home, away, tab)) if e]


# ---------------------------------------------------------------------------
# what a text asks
# ---------------------------------------------------------------------------

ASK = {  # order = priority when a text hits several
    "protect": r"pass rush\w*|rush the passer|sacks?|sacked|get home|get to the qb|pressure\w*|o-?line\w*|offensive line\w*|"
               r"protect\w*|pass pro\b|blitz\w*|d-?line\w*|defensive line\w*|edge rush\w*",
    "redzone": r"red ?zone|finish(es|ing)? drives?|field goals?|kick(ing)? (field goals|fgs)|settle for|scoring (chances?|"
               r"opportunit\w*)|points per (opportunity|scoring)|goal ?line|inside the 20|in the 20",
    "turnovers": r"turnovers?|giveaways?|takeaways?|ball security|fumbl\w*|interceptions?|\bints?\b|turn it over|"
                 r"cough it up|havoc|force (turnovers|fumbles)",
    "explosive": r"explosiv\w*|big plays?|big-play|chunk plays?|home ?run (plays|shots)|deep shots?|hold a big number",
    "pace": r"\bpace\b|tempo|plays (a|per) game|shoot ?out|track meet|snaps|possessions|up-?tempo|hurry.?up",
    "rush": r"\brun(s|ning)?\b(?! (it )?(up|back) the score)|\brush(ing)?\b(?! the passer)|ground game|on the ground|"
            r"ground and pound|run game|run d\b|run defen[cs]e|stop the run|option|trenches|line of scrimmage|"
            r"\bfront( seven| 7)?\b|tailbacks?|running backs?|\brbs?\b|yards per carry|ypc",
    "pass": r"\bpass(es|ing)?\b(?! rush)|throw(s|ing|n)?\b|through the air|air it out|secondary|coverage|"
            r"receivers?|\bwrs?\b|cornerbacks?|\bcbs?\b|safet(y|ies)|aerial",
    "eff": r"move the ball|score on|score against|put up points|moves? it|move the chains|their offense|"
           r"(offen[cs]e|o) (vs\.?|versus|faces?|against) .{0,25}(defen[cs]e|d)\b|(defen[cs]e|d) vs\.? .{0,25}(offen[cs]e|o)\b|"
           r"offen[cs]e (do|does|get|gets|have|has) (anything|much|something)|"
           r"defen[cs]e (be trusted to |really )?(hold|stop|contain|slow)\b|"
           r"who wins that|match ?up\w*|stop (them|him|it)|slow (them|it) down|hold (them|up)",
}
_ASK_RE = {k: re.compile(rf"(?:{v})", re.I) for k, v in ASK.items()}
_TRENCHES = re.compile(r"\btrenches|line of scrimmage|o-?line vs|lines?\b.{0,12}\bwin", re.I)
_PICKS_NOT_INT = re.compile(r"\b(my|your|ur|the|his|their|our) picks?\b|\bpicks? (this|for|in|on)\b", re.I)


def ask_units(q: str) -> list[str]:
    """The units a text asks about, most specific first ([] for no unit question)."""
    t = (q or "").replace("’", "'")
    if not t.strip():
        return []
    out = [k for k, rx in _ASK_RE.items() if rx.search(t)]
    if _TRENCHES.search(t):
        out = ["rush", "protect"] + [u for u in out if u not in ("rush", "protect")]
    if "turnovers" in out and _PICKS_NOT_INT.search(t) and not re.search(r"turnover|giveaway|takeaway|fumbl|interception", t, re.I):
        out.remove("turnovers")
    if "pace" in out and "explosive" not in out and re.search(r"shoot ?out|track meet", t, re.I):
        out.append("explosive")  # a shootout is snaps AND big plays
    if "rush" in out and re.search(r"pass rush|rush the passer", t, re.I) and not re.search(
            r"\brun|ground|rushing|trenches", t, re.I):
        out.remove("rush")
    if len(out) > 1 and "eff" in out:
        out.remove("eff")  # 'move the ball on the ground' is the run game
    return out[:2]


_OFF_HINT = r"(run|runs|running|rush|throw|pass|score|move|put up|finish|protect|block|air it|attack|get yards|establish)"
_DEF_HINT = r"(stop|defend|contain|hold|slow|limit|get home|get to|sack|pressure|force|stuff|cover)"
_OFF_UNIT = (r"(offen[cs]e|o|run game|ground game|passing( game)?|pass game|passing attack|attack|rushing( attack)?|qb|"
             r"quarterback|o ?line|offensive line|receivers?|wrs?|backs?|tailbacks?|run)")
_DEF_UNIT = (r"(defen[cs]e|d|run d|pass d|run defen[cs]e|pass defen[cs]e|secondary|front( seven| 7)?|pass rush|"
             r"d ?line|defensive line|corners?|safeties|linebackers?)")
_MODAL = r"((can|will|does|do|could|would|should) )?(they )?((wants?|going|needs?|able|try(ing)?) to |gonna )?"


_SHARED_FIRST = {"texas", "north", "south", "new", "san", "west", "east", "central", "western", "eastern", "northern",
                 "southern", "ohio", "boston", "miami", "florida", "georgia", "kansas", "oklahoma", "iowa", "mississippi",
                 "michigan", "washington", "oregon", "arizona", "utah", "colorado", "penn", "louisiana", "air", "old",
                 "notre", "virginia", "wake", "kent", "ball", "middle", "coastal", "sam", "fresno", "las", "nc"}


def _pos(team: str, q: str) -> int:
    """Where a team is named in a text (normalized, space-padded), by full name or a unique first word; -1 if not."""
    qn = q if q.startswith(" ") else f" {cfbd.norm(q)} "
    n = cfbd.norm(team)
    i = qn.find(f" {n} ")
    if i < 0 and len(n.split()) > 1 and n.split()[0] not in _SHARED_FIRST:
        i = qn.find(f" {n.split()[0]} ")
    return i


def _all_pos(team: str, tn: str, other: str = "") -> list[tuple[int, int]]:
    """Every (position, name length) a team is named at in a normalized, space-padded text."""
    n = cfbd.norm(team)
    f = n.split()[0] if n else ""
    o = cfbd.norm(other)
    shared = o == f or o.startswith(f + " ")  # 'Team 3' vs 'Team 35', 'Texas' vs 'Texas Tech'
    keys = [n] + ([f] if len(n.split()) > 1 and f not in _SHARED_FIRST and not shared else [])
    out, taken = [], []
    for k in keys:
        for m in re.finditer(rf"(?<= ){re.escape(k)}(?= )", tn):
            if not any(a <= m.start() < b for a, b in taken):
                out.append((m.start() - 1, len(k)))
                taken.append((m.start(), m.end()))
    return out


def ask_offense(q: str, away: str, home: str) -> str | None:
    """Whose OFFENSE the text asks about ('can Minnesota run on Northwestern' -> Minnesota;
    'can South Carolina stop Georgia's run game' -> Georgia; 'New Mexico's defense vs Wyoming's
    offense' -> Wyoming), else None (both directions)."""
    tn = f" {cfbd.norm(re.sub(r"['’]s\b", "", q or ''))} "
    spots = sorted((p, team, ln) for team in (away, home) for p, ln in _all_pos(team, tn, home if team == away else away))
    if not spots:
        return None
    other = {away: home, home: away}
    segs = []  # (team, words after this mention up to the next mention, words before it back to the previous one)
    for i, (p, team, ln) in enumerate(spots):
        nxt = spots[i + 1][0] if i + 1 < len(spots) else len(tn)
        prev = spots[i - 1][0] + 1 + spots[i - 1][2] if i else 0
        segs.append((team, tn[p + 1 + ln:nxt].strip(), tn[prev:p].strip()))
    for team, win, _ in segs:  # possessives first: "<team>'s defense", "<team>'s run game"
        if re.match(rf"((\w+ )?){_DEF_UNIT}\b", win):
            return other[team]
        if re.match(rf"((\w+ )?){_OFF_UNIT}\b", win):
            return team
    for team, win, head in segs:
        if re.match(rf"{_MODAL}{_DEF_HINT}\b", win):
            return other[team]
        if re.match(rf"{_MODAL}{_OFF_HINT}\b", win):
            return team
        if re.search(rf"\b{_DEF_HINT}\w*( down)?$", head):
            return team
    return None


def question_lines(q: str, away: str, home: str, tab: dict | None = None, units: list[str] | None = None) -> list[str]:
    """The decided edge(s) for the unit(s) asked, the asked offense first. [] when no unit is asked."""
    units = ask_units(q) if units is None else units
    if not units:
        return []
    tab = tab if tab is not None else table()
    off = ask_offense(q, away, home)
    out = []
    for u in units:
        es = edges(u, away, home, tab)
        if off and len(es) == 2:
            es.sort(key=lambda e: 0 if e["off"] == off else 1)
            if u not in ("protect",):
                es = es[:1]  # they asked about one offense: the other direction muddied the 12B's answer (model check)
        out += [e["text"] for e in es]
    return out


def card_lines(away: str, home: str, tab: dict | None = None, limit: int = 3) -> list[str]:
    """The clearest unit edges for a full card, biggest rank gap first (no 'no clear edge' filler)."""
    tab = tab if tab is not None else table()
    es = [e for u in ("rush", "pass", "protect", "redzone", "explosive", "turnovers") for e in edges(u, away, home, tab)
          if e.get("winner")]
    es.sort(key=lambda e: -e["gap"])
    out = [e["text"] for e in es[:limit]]
    p = pace_edge(away, home, tab)
    if p and "no real tempo edge" not in p["text"]:
        out.append(p["text"])
    return out


def edges_for(side: str, away: str, home: str, tab: dict | None = None) -> list[str]:
    """Decided unit edges that favor one side, biggest first: the 'why' lane's extra reasons."""
    tab = tab if tab is not None else table()
    es = [e for u in ("rush", "pass", "protect", "redzone", "explosive", "turnovers") for e in edges(u, away, home, tab)
          if e.get("winner") and cfbd.norm(e["winner"]) == cfbd.norm(side)]
    es.sort(key=lambda e: -e["gap"])
    return [e["text"] for e in es]


LABEL = {"rush": "run game vs run defense", "pass": "passing game vs pass defense",
         "protect": "pass rush vs pass protection", "redzone": "red zone (points per scoring chance)",
         "turnovers": "turnovers", "explosive": "big plays", "pace": "pace", "eff": "offense vs defense (EPA/play)"}
