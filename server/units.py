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
        t = cfbd.resolve_team(r.get("team") or "", idx)
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
    gap = rd - ro  # > 0: the offense ranks better than the defense it faces
    if gap >= GAP:
        winner, verdict = on, f"edge {_poss(on)} {ol}"
    elif gap <= -GAP:
        winner, verdict = dn, f"edge {_poss(dn)} {dl}"
    elif ro <= 30 and rd <= 30:
        winner, verdict = None, "strength on strength, no clear edge"
    elif ro >= n - 35 and rd >= n - 35:
        winner, verdict = None, "both weak, no clear edge"
    else:
        winner, verdict = None, "close, no clear edge"
    if unit == "protect":
        text = (f"{_poss(dn)} {dl} (#{rd} in sack rate, {tier(rd, n)}{extra_d}) vs {_poss(on)} {ol} "
                f"(#{ro}, {tier(ro, n)}{extra_o}): {verdict}")
    else:
        text = (f"{_poss(on)} {ol} (#{ro} in {what}, {tier(ro, n)}{extra_o}) vs {_poss(dn)} {dl} "
                f"(#{rd}, {tier(rd, n)}{extra_d}): {verdict}")
    return {"unit": unit, "off": on, "def": dn, "ro": ro, "rd": rd, "winner": winner, "gap": abs(gap),
            "text": text}


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
    "redzone": r"red ?zone|finish(ing)? drives?|field goals?|kick(ing)? (field goals|fgs)|settle for|scoring (chances?|"
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
           r"(offen[cs]e|o) vs\.? .{0,25}(defen[cs]e|d)\b|(defen[cs]e|d) vs\.? .{0,25}(offen[cs]e|o)\b|"
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
                es = es[:1] + [e for e in es[1:] if e["winner"]]  # the other direction only if it's decided
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
