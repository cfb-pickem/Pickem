"""
The "why?" lane (genius exam 2026-11-17: "why?" turns repeated turn 1). A bare
why-style follow-up ("why?", "how come", "why tho", "why not Ohio State?") to
Picky's previous answer about a game gets the NEXT unused reason from that
game's case ledger (case_ledger.py), code-built and deterministic:

  pool game:  the pool card's reasons, then the card's other factors for that
              side (hand weights), the film room's reasons when it backs the
              same side (never one with a projection number), then unit edges
  any other:  pick_brief's factors vs the market for that side (factstore.brief),
              then unit edges
The answer carries facts_used (the rows it cites + the line).

Which reasons turn 1 already used is read from Picky's previous reply (and any
earlier replies the caller passes): shared distinctive tokens (W-L records,
#ranks, decimals, percents, player names) or the reason's topic (SP+, the
number, lookahead, run game ...). Never repeats a used reason; when none is
left it says so plainly.

  why_kind(text)        -> ('why', None) | ('why_not', 'Ohio State') | ('why_side', 'Ohio State') | None
  answer(ctx, q)        -> the reply, or None (not a why follow-up about a game: the full brain takes it)
                           q = "<text> ||| <Picky's last reply>[ ||| <older reply> ...]" (router CTX_KINDS)
"""

from __future__ import annotations

import re

import case_ledger
import cfbd

_LEAD = r"(?:(?:but|ok|okay|so|and|wait|hmm+|ok so|lol|haha|bro|dude|picky)[,\s]+)*"
_WHY_BARE = re.compile(
    rf"^\W*{_LEAD}(?:why|how come|how so|why'?s that|whys that|why is that|explain|explain (?:that|it|yourself|pls|please)|"
    r"what makes you say that|says who|what'?s the reason(?:ing)?|reasons?|because|how)"
    r"(?:\s+(?:tho|though|that|so|is that|do you say that|do you think that|do you like (?:them|that|it|him)|"
    r"you like (?:them|that|it)|on that|bro|man|dude|picky|exactly|again|else|more|tho bro))*\W*$", re.I)
_WHY_NOT = re.compile(rf"^\W*{_LEAD}(?:why not|how come not)\s+(?:take |taking |go with |the |pick |picking )?"
                      r"(?P<x>[a-z][\w .&'-]{1,30}?)\W*$", re.I)
_WHY_SIDE = re.compile(rf"^\W*{_LEAD}why (?:take |taking |go with |the |pick |picking |are you on |you like |u like )"
                       r"?(?P<x>[a-z][\w .&'-]{1,30}?)\W*$", re.I)
_NOT_TEAM = re.compile(r"^(not|tho|though|that|so|you|u|is|do|are|would|should|this|them|him|it|me|now|again|else)\b", re.I)


def why_kind(text: str) -> tuple[str, str | None] | None:
    t = (text or "").replace("’", "'").strip()
    if not t or len(t.split()) > 7:
        return None
    if _WHY_BARE.match(t):
        return "why", None
    for kind, rx in (("why_not", _WHY_NOT), ("why_side", _WHY_SIDE)):
        m = rx.match(t)
        if m and not _NOT_TEAM.match(m.group("x")) and len(m.group("x").split()) <= 3 and _is_team(m.group("x")):
            return kind, m.group("x").strip()
    return None


def _is_team(x: str) -> bool:
    try:
        idx = cfbd.load_index()
    except Exception:  # noqa: BLE001
        return False
    return bool(idx) and bool(cfbd.resolve_team(x, idx) or cfbd.other_teams_named(x, [], idx))


# ---------------------------------------------------------------------------
# reasons and what was already said
# ---------------------------------------------------------------------------

TOPICS = [  # (key, regex, how a person names it); a reason's topic = the earliest hit in it
    ("sp", r"\bsp\+|\bsp plus|power ratings?", "the SP+ edge"),
    ("epa", r"\bepa\b|\bppa\b", "the EPA numbers"),
    ("fpi", r"\bfpi\b", "FPI"),
    ("srs", r"\bsrs\b", "SRS"),
    ("value", r"\bline value\b|\bvalue\b|better number|we get [+-]?\d|league (line|has)|books'|the books|market", "the number"),
    ("key", r"key number|\bthe hook\b", "the key number"),
    ("move", r"line move|line'?s gone|\bmoved\b|\bopened\b|steam", "the line move"),
    ("splits", r"\bsplits\b|of (the )?(spread )?bets|of the money|tickets|sharps?\b|bigger bettors", "the betting splits"),
    ("lookahead", r"look ?-?ahead|on deck|next week|looking ahead|sandwich|trap (game|spot)", "the lookahead spot"),
    ("letdown", r"letdown|let down", "the letdown spot"),
    ("rest", r"\bbye\b|days rest|\brest(ed)?\b|short week", "the rest edge"),
    ("revenge", r"revenge|lost this (game|one) last (year|season)", "the revenge angle"),
    ("travel", r"\btravel|time zones?|body clock|\bmiles\b|flying", "the travel"),
    ("altitude", r"altitude|elevation", "the altitude"),
    ("weather", r"weather|\bwind\b|forecast|\brain\b|\bsnow", "the weather"),
    ("qb", r"\bqbs?\b|quarterback|third-string\w*|backup|injur\w*|ruled out|out for the|questionable|doubtful|"
           r"(is|'s|are|remains) out\b|missed (his|three|two|the)|status", "the injury news"),
    ("stakes", r"playoff race|title|stakes|committee|style points|bowl[- ]eligib\w*|in the race", "the stakes"),
    ("ats", r"\bats\b|against the spread|straight covers|covers? in a row|cover streak", "the ATS record"),
    ("rush", r"\brun(ning)? (game|defen[cs]e|d)\b|rushing|on the ground|stuffs?\b", "the run game"),
    ("pass", r"\bpass(ing)? (game|defen[cs]e|d)\b|passing epa|secondary|through the air|pass defense", "the passing game"),
    ("protect", r"pass rush|\bsacks?\b|protection|pressure", "the pass rush"),
    ("redzone", r"red.?zone|scoring chance|finish(ing)? drives", "the red zone"),
    ("explosive", r"explosiv\w*|big.?plays?", "the big plays"),
    ("turnovers", r"turnovers?|takeaways?|giveaways?|havoc", "turnovers"),
    ("pace", r"\bpace\b|tempo|plays a game|snaps", "pace"),
    ("thirddown", r"third downs?", "third downs"),
    ("home", r"home field|at home|road record|on the road", "home field"),
]
_TOPIC_RE = [(k, re.compile(rx, re.I), lab) for k, rx, lab in TOPICS]
LABELS = {k: lab for k, _, lab in TOPICS}
UNIT_TOPIC = {"run game": "rush", "run defense": "rush", "passing game": "pass", "pass defense": "pass",
              "pass rush": "protect", "pass protection": "protect", "finishing drives": "redzone",
              "red-zone defense": "redzone", "big-play": "explosive", "takeaways": "turnovers"}
_NUM = re.compile(r"(?<![\w.])(?:\d{1,2}-\d{1,2}(?:-\d{1,2})?|#\d{1,3}|\d{1,3}\.\d|\d{1,3}%)(?![\w.])")
_NAME = re.compile(r"\b([A-Z][a-z'’]{3,}(?:-[A-Z][a-z]+)?)\b")
_COMMON = {"the", "this", "that", "they", "their", "with", "from", "into", "over", "under", "still", "beyond", "also",
           "line", "market", "league", "books", "spread", "points", "point", "lean", "edge", "best", "case", "home",
           "road", "week", "last", "next", "game", "games", "season", "saturday", "friday", "playoff", "title", "only",
           "even", "both", "draftkings", "espn", "rotowire", "wire", "sports", "nation", "athletic", "radio",
           "offense", "defense", "three", "two", "four", "five", "film", "room", "coach", "head"}


def topics(text: str) -> list[str]:
    hits = sorted((m.start(), k) for k, rx, _ in _TOPIC_RE for m in [rx.search(text or "")] if m)
    return list(dict.fromkeys(k for _, k in hits))


def _tokens(text: str, skip: set[str]) -> set[str]:
    nums = {x for x in _NUM.findall(text or "")}
    names = {w.lower() for w in _NAME.findall(text or "")} - _COMMON - skip
    return (nums | names) - skip


def _reason(text: str, said: str, topic: str | None, skip: set[str]) -> dict:
    return {"text": said, "topic": topic or (topics(text) or [None])[0], "tokens": _tokens(text, skip), "raw": text}


def used(r: dict, reply: str, skip: set[str]) -> bool:
    """Did a previous reply already give this reason (same distinctive token, or the same topic)?"""
    if r["tokens"] & _tokens(reply, skip):
        return True
    return bool(r["topic"]) and r["topic"] in topics(reply)


def _from_ledger(led: dict, side: str, skip: set[str]) -> list[dict]:
    """The case ledger's rows for `side` (ranked by the hand weights, unit edges last) as why-lane reasons."""
    out = []
    for r in case_ledger.reasons(led, side):
        topic = next((v for k, v in UNIT_TOPIC.items() if k in r["raw"]), None) if r["tier"] == "unit" else None
        x = _reason(r["raw"], r["text"], topic, skip)
        x["facts"] = r["facts_used"]
        out.append(x)
    return out


def pool_reasons(ctx: dict, pg: dict, side: str, skip: set[str]) -> tuple[list[dict], str | None, dict | None]:
    """(reasons for `side`, its label, the ledger) from the pool game's card; ([], None, None) without a card."""
    import router
    c = router._card_for(ctx, pg)
    if not c:
        return [], None, None
    led = case_ledger.from_card(c)
    return _from_ledger(led, side, skip), led["labels"][side], led


def market_reasons(g: dict, side: str, skip: set[str]) -> tuple[list[dict], str | None, dict | None]:
    """(reasons for `side` vs the market, its label, the ledger) for any FBS game: pick_brief's factors + unit edges."""
    import espn
    import factstore
    notes = (espn._cache.get(g["id"]) or (0, None))[1]  # whatever turn 1's card fetched; never a fetch here
    b = factstore.brief(g, notes)
    if not b:
        return [], None, None
    led = case_ledger.from_brief(b)
    return _from_ledger(led, side, skip), led["labels"][side], led


_TAKE = re.compile(r"\b(?:i'?d take|i'?m taking|take|i like|i'?m on|lean(?:ing)?|give me|gimme|i got|going with|"
                   r"ride|riding|i'?d go with|my side is|the play is)\s+(?:the\s+)?([A-Z][\w.&' ]{1,30}?)(?=\s+[+-]\d|\s*[,.(:;!]|\s+(?:to|at|here|and|in|plus|getting|laying)\b|$)",
                   re.I)
_LAB = re.compile(r"\b([A-Z][\w.&']+(?: [A-Z][\w.&']+){0,2}) ([+-]\d{1,2}(?:\.5)?)\b")


def side_taken(reply: str, away: str, home: str) -> str | None:
    """The side Picky's reply took: 'I'd take X', 'X +3', 'lean X'."""
    def which(name: str) -> str | None:
        n = cfbd.norm(name)
        for t in (away, home):
            tn = cfbd.norm(t)
            if n == tn or n.startswith(tn) or n.endswith(" " + tn) or (len(n) >= 4 and tn.startswith(n)):
                return t
        return None
    for m in _TAKE.finditer(reply or ""):
        t = which(m.group(1))
        if t:
            return t
    for m in _LAB.finditer(reply or ""):
        t = which(m.group(1))
        if t:
            return t
    return None


def _say(r: str, c: dict | None = None, locked: bool = False) -> str:
    return case_ledger.say(r, c, locked)


def _and(xs: list[str]) -> str:
    return xs[0] if len(xs) == 1 else ", ".join(xs[:-1]) + " and " + xs[-1]


def _cap(s: str) -> str:
    return s[:1].upper() + s[1:] if s else s


def _team_in(x: str, away: str, home: str) -> str | None:
    n = cfbd.norm(x)
    for t in (away, home):
        tn = cfbd.norm(t)
        if n == tn or tn.startswith(n) or n.startswith(tn) or cfbd.norm(cfbd.ALIASES.get(n, "~")) == tn:
            return t
    return None


def answer(ctx: dict | None, q: str) -> str | None:
    text, _, rest = (q or "").partition(" ||| ")
    wk = why_kind(text)
    replies = list(dict.fromkeys(r.strip() for r in rest.split(" ||| ") if r.strip()))  # newest first, deduped
    if not wk or not replies or not ctx:
        return None
    import slate_cards
    last = replies[0]
    g, pg = slate_cards.find_game(last, ctx)
    if not g and wk[1]:
        g, pg = slate_cards.find_game(wk[1], ctx)
    if not g:
        return None
    away, home = g["awayTeam"], g["homeTeam"]
    side = side_taken(last, away, home)
    named = _team_in(wk[1], away, home) if wk[1] else None
    if wk[1] and not named:
        return None  # 'why not Oregon?' about a team that isn't in this game: the full brain
    if not side:
        return None  # the last reply took no side: 'why?' is about something else
    if wk[0] == "why_side" and named != side:
        return None  # 'why Baylor?' when the call was Texas Tech: not a follow-up to it
    skip = {cfbd.norm(away), cfbd.norm(home)} | set(cfbd.norm(away).split()) | set(cfbd.norm(home).split())
    reasons, label, led = pool_reasons(ctx, pg, side, skip) if pg else ([], None, None)
    if not label:
        reasons, label, led = market_reasons(g, side, skip)
    if not label:
        return None
    import factstore
    for r in reasons:  # the game's own numbers (spread, total) aren't a reason's fingerprint
        r["tokens"] -= {x for x in r["tokens"] if re.fullmatch(r"\d{1,2}\.5", x)}
    flags = [any(used(r, rep, skip) for rep in replies) for r in reasons]
    if re.match(r"^(For|I'm already on) .{3,40}?: beyond |^Not .{2,40}? because ", last):  # our own previous why answer: everything up to it went out too
        gave = [i for i, r in enumerate(reasons) if r["text"].rstrip(". ") in last]  # the rows it actually sent
        hi = max(gave) if gave else max((i for i, f in enumerate(flags) if f), default=-1)
        flags = [f or i < hi for i, f in enumerate(flags)]
    said = [LABELS[r["topic"]] for r, f in zip(reasons, flags) if f and r["topic"] in LABELS]
    said = list(dict.fromkeys(said))[:3]
    fresh = [r for r, f in zip(reasons, flags) if not f]
    seen, nxt = set(), []
    for r in fresh:  # one reason per topic
        if r["topic"] and r["topic"] in seen:
            continue
        if re.search(r"(\.\.\.|…)\s*$|\b(out for|out of|miss the|for the)\s*$", r["text"].strip()):
            continue  # a card reason cut off mid-sentence ('...is out for...'): never send a garbled fact
        seen.add(r["topic"])
        nxt.append(r)
    other = home if side == away else away
    against = wk[0] == "why_not" and cfbd.norm(named) != cfbd.norm(side)
    line = [led["line_fact"]] if led else []
    if not nxt:
        if against:
            return factstore.answer(f"Nothing new against {other} beyond what I said: that's my whole case for {label}, "
                                    "no other reason in the numbers.", line)
        return factstore.answer(f"That's the whole case for {label}: the reasons I gave are all I've got, nothing new "
                                "in the numbers.", line)
    pick = nxt[:1]
    if len(nxt) > 1 and len(pick[0]["text"]) < 70:
        pick.append(nxt[1])
    body = "; also, ".join(r["text"].rstrip(". ") for r in pick)
    cited = line + [f for r in pick for f in r.get("facts") or []]
    if against:
        return factstore.answer(f"Not {other} because {body}" + (f", on top of {_and(said)}" if said else "")
                                + f". Still {label}.", cited)
    head = f"I'm already on {label}" if wk[0] == "why_not" else f"For {label}"
    return factstore.answer(f"{head}: beyond {_and(said)}, {body}." if said else f"{head}: {body}.", cited)
