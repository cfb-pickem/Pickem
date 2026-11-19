"""
Case ledger (docs/picky-code-ontology-plan-2026-11-18.md 3.3, phase 3): one ranked list of reasons per game,
the single source the pushback lanes read.

Every pick_brief factor that has a source and a side becomes a reason row
    {side, weight, text, raw, facts_used, kind, n, tier}
(line value, QB status, ATS split, unit edge, spot, H2H, weather, travel, stakes, ...). Rows are ranked by
pick_brief's hand weights (ledger B12: nothing fitted beats them); context factors (weight 0), the asker-specific
league factors (58-60), the 'key number' half of line value and factors with no source are never reasons. A pool
game's ledger comes from its game card (league line); any other game's from pick_brief run against the store's
market line (factstore.brief). Unit edges (units.py) and the film room's reasons trail the weighted rows; a film
room reason that carries a projection number ('SP+ has X by 10') never becomes a row: the only numbers a pushback
answer can say are the ones in its rows' facts_used.

  pick_answer(ledger)            turn 1: the lean + the top 2 reasons
  why_lane.answer                 'why?': the next unused row (why_lane reads reasons() from here)
  other_side_answer(ctx, q)       'best case for the other side' / 'devil's advocate' / 'argue for X'
  change_mind_answer(ctx, q)      'what would change your mind': computed triggers (key number, QB flip, wind)

Code only: no model, no network.
"""

from __future__ import annotations

import re

import factstore
import pick_brief

SKIP_N = {4, 6, 58, 59, 60}
KINDS = {1: "line value", 2: "line move", 3: "line move", 5: "total", 7: "betting splits",
         8: "QB status", 9: "QB status", 14: "QB status", 10: "injuries", 11: "injuries", 12: "injuries", 13: "injuries",
         15: "coaching", 16: "coaching", 55: "coaching", 56: "coaching",
         17: "ATS split", 29: "ATS split", 50: "ATS split",
         18: "spot", 19: "spot", 20: "spot", 21: "spot", 22: "spot", 23: "spot", 24: "spot",
         25: "stakes", 26: "stakes", 28: "stakes", 57: "stakes", 27: "stakes",
         30: "travel", 31: "travel", 32: "travel", 36: "travel",
         33: "weather", 34: "weather", 35: "weather",
         37: "ratings", 49: "ratings", 51: "H2H", 54: "coaching"}
KINDS.update({n: "unit edge" for n in (38, 39, 40, 41, 42, 43, 44, 45, 46, 47, 48, 52, 53)})
_PREFIX_N = {"sp+": 49, "srs": 49, "espn fpi": 49, "fpi": 49, "massey": 49, "epa": 37, "team ats": 17, "wind": 33,
             "rain/cold": 34, "bye": 18, "short week": 19, "lookahead": 20, "letdown": 21, "sandwich": 22,
             "revenge": 23, "title stakes": 26, "line move": 2, "line value": 1, "qb status": 8, "travel": 30,
             "altitude": 32, "head-to-head": 51, "home field": 29, "recent form": 50}
_PREFIX_N.update({v.lower(): k for k, v in pick_brief.NAMES.items() if v.lower() not in _PREFIX_N})
_PROJ = re.compile(r"\bproject\w*\b|\bby (?:about |roughly |around |~)?\d+(?:\.\d+)?\b", re.I)
KEYS = (3, 7, 10, 14, 17, 21)
WIND_MPH = 20
WIND_FACT = {"id": "rule.weather.wind20", "kind": "rule", "subject": "total", "value": WIND_MPH,
             "text": "wind over 20 mph cuts scoring (a tilt to the under)", "source": "pick factors (wind)",
             "as_of": "static"}
_KEEP_LABEL = re.compile(r"^(success rate|explosiveness|run game \(rushing PPA\)|pass game \(passing PPA\)|red zone \(points per "
                         r"scoring chance\)|havoc/sacks|third downs|turnovers|tempo|style clash|backdoor):", re.I)


def say(r: str, c: dict | None = None, locked: bool = False) -> str:
    """router.say_reason, except stat factors keep their label ('success rate: X offense #38 vs ...')."""
    import router
    if _KEEP_LABEL.match(r or ""):
        lab, _, rest = r.partition(": ")
        lab = re.sub(r" \((rushing|passing) PPA\)| \(points per scoring chance\)", "", lab)
        return f"{lab}: {rest}"
    return router.say_reason(r, c, locked)


_REPORT = re.compile(r"^(?P<head>.+? (?:is|are) (?:out|doubtful|questionable|a game-time decision|day-to-day|limited)\b.*?) "
                     r"\((?P<body>.*), (?P<src>[^,()]{2,60})\)$", re.S)
_CUT = re.compile(r"(\.\.\.|…)|\b(out for|out of|miss the|for the|sustained the|in)\s*$")


def _tidy(n: int | None, said: str) -> str | None:
    """A report-quoting reason the way a person says it: the status + the source, not the quoted report
    ('... is questionable (per Yahoo Sports)'); injury lists lose the pieces the report cut off mid-sentence. None
    when nothing whole is left."""
    if KINDS.get(n) == "QB status":
        m = _REPORT.match(said or "")
        if m:
            return f"{m.group('head')} (per {m.group('src').strip()})"
    if KINDS.get(n) == "injuries":
        keep = [x.strip() for x in (said or "").split("; ") if x.strip() and not _CUT.search(x.strip())]
        return "; ".join(keep) or None
    return None if _CUT.search((said or "").strip()) and n else said


def _slug(s) -> str:
    return re.sub(r"[^a-z0-9]+", "_", factstore.norm(str(s))).strip("_")


def _guess_n(raw: str) -> int | None:
    head = (raw or "").split(":", 1)[0].lower().strip()
    if head in _PREFIX_N:
        return _PREFIX_N[head]
    return next((n for p, n in _PREFIX_N.items() if head.startswith(p)), None)


def _source(n: int | None) -> str | None:
    st = pick_brief.STATUS.get(n) if n else None
    if st is None:
        return "game card (pick_brief)"
    return None if st[0] == "none" else st[1]


def _row(gid, side: str, i: int, raw: str, said: str, n: int | None, w, tier: str, as_of: str,
         source: str | None = None) -> dict | None:
    src = source or _source(n)
    if not src:
        return None  # a factor with no source is never a reason
    kind = {"film": "film room", "unit": "unit edge"}.get(tier) or KINDS.get(n) or (pick_brief.NAMES.get(n) if n else "other")
    fact = {"id": f"ledger.{gid}.{_slug(side)}.{i}", "kind": "ledger", "subject": side, "value": raw, "text": said,
            "source": src, "as_of": as_of or ""}
    return {"side": side, "weight": w, "text": said, "raw": raw, "facts_used": [fact], "kind": kind, "n": n,
            "tier": tier}


def _is_reason(f: dict, side: str) -> bool:
    return (bool(f.get("side")) and factstore.norm(f["side"]) == factstore.norm(side) and (f.get("w") or 0) > 0
            and f.get("n") not in SKIP_N and not (f.get("why") or "").startswith("key number:"))


def _unit_rows(gid, side: str, away: str, home: str, start: int, as_of: str) -> list[dict]:
    try:
        import units
        es = units.edges_for(side, away, home)
    except Exception:  # noqa: BLE001 - the ledger works without unit edges
        es = []
    return [r for i, t in enumerate(es)
            if (r := _row(gid, side, start + i, t, t, None, 0.0, "unit", as_of, "units.py (CFBD advanced stats)"))]


def _line_fact(gid, away: str, home: str, line: float, source: str, as_of: str) -> dict:
    return {"id": f"ledger.{gid}.line", "kind": "ledger", "subject": str(gid), "value": line,
            "text": f"{away} at {home}: {pick_brief.league_line_text({'away': away, 'home': home, 'line': line})}",
            "source": source, "as_of": as_of or ""}


def _dedup(rows: list[dict]) -> list[dict]:
    seen, out = set(), []
    for r in rows:
        k = (r["side"], r["raw"])
        if k not in seen:
            seen.add(k)
            out.append(r)
    return out


def from_card(c: dict, locked: bool | None = None) -> dict:
    """A pool game's ledger from its game card (factors vs the league line)."""
    import game_cards
    lk = game_cards.is_locked(c) if locked is None else locked
    g = c["game"]
    away, home, gid = g["away"], g["home"], g.get("id")
    line = float(c["league_line"]["home"])
    gl = {**g, "line": line}
    lean = c["lean"]["side"]
    labels = {s: pick_brief.side_label(s, gl) for s in (away, home)}
    labels[lean] = c["lean"].get("label") or labels[lean]
    as_of = c.get("generated_at") or ""
    by_why = {f["why"]: f for f in c.get("factors") or []}
    rows: list[dict] = []
    for side in (away, home):
        picked = [(r, by_why.get(r)) for r in (c.get("reasons") if side == lean else c.get("against")) or []
                  if not r.startswith("key number:")]
        have = {r for r, _ in picked}
        picked += [(f["why"], f) for f in sorted((f for f in c.get("factors") or [] if _is_reason(f, side)),
                                                 key=lambda f: (-f["w"], f["n"])) if f["why"] not in have]
        for raw, f in picked:
            n = f["n"] if f else _guess_n(raw)
            if n in SKIP_N:
                continue
            said = (game_cards.value_text(c, lk) if raw.startswith("line value:") else None) or _tidy(n, say(raw, c, lk))
            r = said and _row(gid, side, len(rows), raw, said, n, float(f["w"]) if f else None, "factor", as_of)
            if r:
                rows.append(r)
        case = c.get("case") or {}
        if case.get("pick") and factstore.norm(case["pick"]) == factstore.norm(side):
            for t in game_cards._case_reasons(case, lk):
                if t and not _PROJ.search(t):  # never a model-written projection number
                    rows.append(_row(gid, side, len(rows), t, t, None, 0.0, "film", as_of,
                                     f"film room ({case.get('by') or 'offline'})"))
        rows += _unit_rows(gid, side, away, home, len(rows), as_of)
    return {"game": {"id": gid, "away": away, "home": home}, "lean": lean, "labels": labels,
            "word": (c.get("confidence") or {}).get("word") or "lean", "line": line, "source": "league",
            "rows": _dedup(rows), "as_of": as_of,
            "line_fact": _line_fact(gid, away, home, line, "league line (game card)", as_of)}


def from_brief(b: dict, as_of: str = "") -> dict:
    """Any game's ledger from pick_brief run against the market line (slate_cards.brief_for via factstore.brief)."""
    import game_cards
    g = b["game"]
    away, home, gid = g["away"], g["home"], g.get("id")
    line = float(g["line"])
    labels = {s: pick_brief.side_label(s, g) for s in (away, home)}
    sc = b.get("score") or {}
    margin = abs((sc.get(away) or 0) - (sc.get(home) or 0))
    word = game_cards.confidence(None, b.get("strength") or "none", margin, None)["word"]
    rows: list[dict] = []
    for side in (away, home):
        for f in sorted((f for f in b.get("factors") or [] if _is_reason(f, side)), key=lambda f: (-f["w"], f["n"])):
            said = _tidy(f["n"], say(f["why"]))
            r = said and _row(gid, side, len(rows), f["why"], said, f["n"], float(f["w"]), "factor", as_of)
            if r:
                rows.append(r)
        rows += _unit_rows(gid, side, away, home, len(rows), as_of)
    return {"game": {"id": gid, "away": away, "home": home}, "lean": b.get("pick") or b.get("lean"), "labels": labels,
            "word": word, "line": line, "source": "market", "rows": _dedup(rows), "as_of": as_of,
            "line_fact": _line_fact(gid, away, home, line, "market line (fact store)", as_of)}


def for_game(ctx: dict, row: dict | None, pg: dict | None) -> dict | None:
    """The ledger for a game row: the pool card when it's a pool game (None without a card), else the market brief."""
    if not row:
        return None
    if pg:
        import router
        c = router._card_for(ctx, pg)
        return from_card(c) if c else None
    b = factstore.brief(row)
    return from_brief(b, factstore.market(row).get("as_of") or "") if b else None


def reasons(led: dict, side: str, units: bool = True) -> list[dict]:
    return [r for r in led["rows"] if factstore.norm(r["side"]) == factstore.norm(side) and (units or r["tier"] != "unit")]


def top(led: dict, side: str, n: int = 2, units: bool = False, exclude=()) -> list[dict]:
    ex = set(exclude)
    return [r for r in reasons(led, side, units) if r["raw"] not in ex and r["text"] not in ex][:n]


def _facts(rows: list[dict]) -> list[dict]:
    return [f for r in rows for f in r["facts_used"]]


def _join(rows: list[dict]) -> str:
    ts = [r["text"].rstrip(". ") for r in rows]
    return ts[0] if len(ts) == 1 else "; ".join(ts[:-1]) + "; and " + ts[-1]


# ---------------------------------------------------------------------------------------------------------------
# turn 1
# ---------------------------------------------------------------------------------------------------------------

def pick_answer(led: dict) -> factstore.Answer:
    side = led["lean"]
    rs = top(led, side, 2)
    why = "; ".join(r["text"] for r in rs)
    return factstore.answer(f"I'd take {led['labels'][side]} ({led['word'] or 'lean'})" + (f": {why}" if why else "") + ".",
                            [led["line_fact"]] + _facts(rs))


# ---------------------------------------------------------------------------------------------------------------
# the game a pushback text is about, and the side Picky is on
# ---------------------------------------------------------------------------------------------------------------

def _context(ctx: dict, q: str) -> tuple[dict, dict, str, str | None, str] | None:
    """(ledger, row, our side, the side the text names in this game or None, the text) or None."""
    import why_lane
    text, _, rest = (q or "").partition(" ||| ")
    replies = [r.strip() for r in rest.split(" ||| ") if r.strip()]
    last = replies[0] if replies else ""
    row, pg = factstore.find_game(text, ctx or {})
    if not row and last:
        row, pg = factstore.find_game(last, ctx or {})
    led = for_game(ctx or {}, row, pg)
    if not led:
        return None
    away, home = led["game"]["away"], led["game"]["home"]
    ours = why_lane.side_taken(last, away, home) if last else None
    named = next((t for t in (why_lane._team_in(x, away, home) for x in factstore.teams_named(text)) if t), None)
    return led, row, ours or led["lean"], named, text


# ---------------------------------------------------------------------------------------------------------------
# the other side
# ---------------------------------------------------------------------------------------------------------------

OTHER_SIDE = re.compile(r"\bdevil'?s advocate\b|\b(best|strongest|good|real) case for\b|\bmake (the|a|your) case for\b"
                        r"|\bargue (for|the other|against)\b|\bsteel ?man\b|\bcase against (your|ur|the|that|this) "
                        r"(pick|call|side|lean|take)\b|\bother side'?s (case|argument)\b|\bcase for the other\b", re.I)


def other_side_answer(ctx: dict, q: str) -> factstore.Answer | None:
    """'best case for the other side' / 'devil's advocate' / 'argue for Baylor': that side's top 2 ledger reasons,
    never one of ours; says so plainly when that side has none."""
    if not OTHER_SIDE.search((q or "").partition(" ||| ")[0]):
        return None
    got = _context(ctx, q)
    if not got:
        return None
    led, _, ours, named, _ = got
    away, home = led["game"]["away"], led["game"]["home"]
    target = named or (home if ours == away else away)
    mine = (home if target == away else away) if target == ours else ours
    ex = [x for r in reasons(led, mine) for x in (r["raw"], r["text"])]
    rows = top(led, target, 2, units=True, exclude=ex)
    lab, my_lab = led["labels"][target], led["labels"][mine]
    if not rows:
        return factstore.answer(f"Honestly there's no real case for {lab} in my numbers: everything I've got points to "
                                f"{my_lab}.", [led["line_fact"]])
    body = _join(rows)
    if target == ours:
        tail = " That's my side already, so that's the case I'm making."
    else:
        word = f" ({led['word']})" if led["lean"] == ours and led.get("word") else ""
        tail = f" I'm still on {led['labels'][ours]}{word}, but that's the side if you want to fade me."
    return factstore.answer(f"Best case for {lab}: {body[:1].upper() + body[1:]}.{tail}", [led["line_fact"]] + _facts(rows))


# ---------------------------------------------------------------------------------------------------------------
# what would change my mind
# ---------------------------------------------------------------------------------------------------------------

CHANGE_MIND = re.compile(r"\bchange (your|ur|you'?re|yo) (mind|pick|call|lean)\b|\bwhat (would |could |'?d |will )?"
                         r"(flips?|changes?|moves?) you\b|\bflips? you\b|\bmake you (flip|switch|change|bail|jump|get off|"
                         r"fade|move off)\b|\bgets? you off\b|\b(when|what) would you (flip|switch|bail|jump ship|get off)\b"
                         r"|\bwhat'?s your (exit|out|bail|stop[- ]?loss)\b|\bwhat would it take\b", re.I)


def line_trigger(our_line: float) -> dict | None:
    """The nearest key number AGAINST our side and the line one hook past it. our_line is our side's line
    (negative = we're laying points): a favorite loses when it has to lay more, a dog when it gets fewer."""
    a = abs(our_line)
    if our_line <= 0:
        k = min((k for k in KEYS if k >= a and (our_line < 0 or k > 0)), default=None)
        return {"key": k, "target": -(k + 0.5)} if k else None
    k = max((k for k in KEYS if k <= a), default=None)
    return {"key": k, "target": k - 0.5} if k else {"key": None, "target": 0}


_QB = re.compile(r"\bQB\d?\b|quarterback", re.I)
_GONE = re.compile(r"season[- ]ending|for the (season|year)|rest of the season|surgery|\btorn\b|\bACL\b", re.I)


def qb_triggers(notes: list[dict], ours: str, other: str) -> list[dict]:
    """A QB status flip on an availability note: our QB questionable -> ruled out flips us; theirs questionable or
    out -> cleared to play flips us."""
    out, teams = [], set()
    for a in notes or []:
        st = (a.get("status") or "").lower()
        if not _QB.search(a.get("text") or "") or not a.get("players") or not a.get("team"):
            continue
        if st == "out" and _GONE.search(a.get("text") or ""):
            continue  # season-ending: nothing to flip
        if factstore.norm(a["team"]) in teams:
            continue  # one trigger per team
        teams.add(factstore.norm(a["team"]))
        p, src = a["players"][0], a.get("source_name") or "the report"
        team = a["team"]
        if factstore.has_personnel():  # the per-player list: a note's label can belong to another player in it
            mine = factstore.player_status(team=team, player=p)
            if mine:
                st = str(mine[0]["status"]).lower()
            elif len(a["players"]) > 1:  # his own clause gave him no status ('Leavitt remains the QB')
                teams.discard(factstore.norm(team))
                continue
        fact = {"id": f"availability.{_slug(p)}.{_slug(st or 'note')}", "kind": "availability", "subject": p,
                "value": st, "text": a.get("text") or "", "source": src, "as_of": a.get("date") or ""}
        if factstore.norm(team) == factstore.norm(ours) and st in ("questionable", "doubtful", "game-time decision"):
            out.append({"text": f"if {p} is ruled out (he's {st}, per {src})", "fact": fact})
        elif factstore.norm(team) == factstore.norm(other) and st in ("questionable", "doubtful", "game-time decision"):
            out.append({"text": f"if {p} is cleared and plays for {other} (he's {st}, per {src})", "fact": fact})
        elif factstore.norm(team) == factstore.norm(other) and st == "out":
            out.append({"text": f"if {p} gets cleared after all ({other} has him out, per {src})", "fact": fact})
    return out[:2]


def wind_trigger(weather: dict | None, total: float | None) -> dict | None:
    if not weather or weather.get("wind_mph") is None or total is None:
        return None
    w = float(weather["wind_mph"])
    if w > WIND_MPH:
        t = (f"for the total, the forecast already has {w:g} mph wind, over the {WIND_MPH} mph line, so I'd lean the "
             f"under on {total:g} unless it calms down")
    else:
        t = f"for the total, wind over {WIND_MPH} mph (the forecast says {w:g} mph) would put me on the under at {total:g}"
    return {"text": t, "wind": w, "total": total}


def _say_line(side: str, line: float) -> str:
    return f"{side} {line:+g}" if line else f"{side} (pick'em)"


def change_mind_answer(ctx: dict, q: str) -> factstore.Answer | None:
    """'what would change your mind': triggers computed from the ledger's game: the market through the nearest key
    number against our side, a QB status flip on an availability note, wind over 20 mph for the total."""
    if not CHANGE_MIND.search((q or "").partition(" ||| ")[0]):
        return None
    got = _context(ctx, q)
    if not got:
        return None
    led, row, ours, _, _ = got
    away, home = led["game"]["away"], led["game"]["home"]
    other = home if ours == away else away
    sheet = row.get("sheet") or {}
    m = factstore.market(row)
    used = [led["line_fact"], factstore.KEY_NUMBER_FACT] + factstore.facts(row, ("market",))
    trig = []
    sp = m.get("spread")
    if sp is not None:
        mine = sp if ours == home else -sp
        lt = line_trigger(mine)
        if lt:
            cur = _say_line(ours, mine)
            if lt["key"]:
                past = "past" if mine <= 0 else "under"
                t = f"if the market gets to {_say_line(ours, lt['target'])} ({past} the key number {lt['key']}; it's {cur} now)"
            else:
                t = f"if the market goes to a pick'em or flips to {other} getting points (it's {cur} now)"
            trig.append(t)
            used.append({"id": f"trigger.{led['game']['id']}.line", "kind": "trigger", "subject": ours,
                         "value": lt["target"], "text": t, "source": "computed: market line + key numbers",
                         "as_of": m.get("as_of") or ""})
    for x in qb_triggers(sheet.get("availability") or [], ours, other):
        trig.append(x["text"])
        used.append(x["fact"])
    wt = wind_trigger(sheet.get("weather"), m.get("overUnder"))
    if wt:
        trig.append(wt["text"])
        used += [WIND_FACT] + factstore.facts(row, ("weather",))
    if not trig:
        return factstore.answer(f"Nothing in my numbers is close to flipping me off {led['labels'][ours]}: no key number "
                                "near the line, no QB question, no weather.", used)
    head = f"What flips me off {led['labels'][ours]}: " + "; ".join(trig) + "."
    tail = (" It's a coin flip already, so any one of those does it." if led.get("word") == "coin flip" and led["lean"] == ours
            else " Short of that, I'm staying put.")
    return factstore.answer(head + tail, used)
