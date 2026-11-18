"""
Unit matchup code lane (plan 2026-11-18, Phase 6): 'can X run on Y', 'who wins the trenches', 'is this a
shootout', 'where does X have the edge on Y' answered in code from units.py, in the plan's section 3.1 shape:

  [answer]   the asked direction, decided ("Not really, Y's run D wins that: #12 vs #120 ...")
  [support]  the other direction (always), style (run rate, plays a game), SP+ unit ranks
  [verdict]  one computed "so what" from the edges: a side lean with the number, over/under, or "doesn't move
             the number". Lean words only, never a lock.

gemma's one voice line goes after it (picky_brain.fact_reply), unchanged. A unit ask this lane can't parse (no game
it can find, a long mixed text, a pick ask) stays on the full brain.

Data comes through SOURCE: today CfbdSource (the CFBD cache through units.table, the cfbd index and
slate_cards.find_game). Phase 2's fact store repoints the lane by swapping SOURCE for an object with the same three
methods. No model, no network.
"""

from __future__ import annotations

import datetime as dt
import random
import re

import cfbd
import units


class CfbdSource:
    """The lane's only data reads. units_table() -> units.table() shape; sp(team) -> {'off_rank', 'def_rank', ...};
    game(q, ctx) -> {'away', 'home', 'spread' (home line, - = home favored), 'total', 'league'} or None."""

    def units_table(self) -> dict:
        return units.table()

    def sp(self, team: str) -> dict | None:
        idx = cfbd.load_index()
        t = cfbd.resolve_team(team, idx) if idx else None
        return (t or {}).get("sp")

    def game(self, q: str, ctx: dict | None = None) -> dict | None:
        import factstore  # the shared entity resolver ('Virginia Tech ... Miami' is VT-Miami, not a 'Tech' game)
        g, pg = factstore.find_game(q, ctx or {})
        if not g:
            return None
        ln = factstore.market(g) or {}
        league = pg is not None and pg.get("line") is not None
        return {"away": g["awayTeam"], "home": g["homeTeam"], "league": league,
                "spread": float(pg["line"]) if league else ln.get("spread"), "total": ln.get("overUnder")}


SOURCE = CfbdSource()

# ---------------------------------------------------------------------------
# what the text asks
# ---------------------------------------------------------------------------

OVERVIEW = re.compile(r"\bwhere does .{2,40}? have (the|an|its|their|any) edges?\b|\bhow do .{2,40}? match ?up\b"
                      r"|\b(biggest|best) (unit )?edges?\b|\bwho has the edge (in|between)\b|\bmatch ?ups? (in|between)\b",
                      re.I)
_QSHAPE = re.compile(r"\?|^\W*(can|could|will|would|does|do|did|who|whose|where|how|is|are|what|which)\b", re.I)
# a pick / line / rules / league ask, a player stat ('how many picks has X thrown'), not a unit read
_NOT = re.compile(r"\b(picks?|picking|covers?|covering|lock(ed|s)?|leans?|bets?|parlay|fade|"
                  r"who (you|ya|u|do you|would you) (got|like|taking|take)|who (should|do) i)\b"
                  r"|\bhow many\b(?![^?.!]{0,25}\bplays\b)"
                  r"|\bpush\w*|\bwin(s)? by\b|\bspread\b|\bthe line is\b|\bis the line\b|\b(our|the) league\b", re.I)
MAX_WORDS = 35  # longer texts carry more than one ask: the full brain (arch review R7)


def ask(q: str) -> list[str]:
    """The units asked, or ['all'] for a general 'where's the edge' between two teams, or []."""
    t = (q or "").replace("’", "'")
    us = units.ask_units(t)
    if "pace" in us and "rush" in us and not re.search(r"run game|ground|rushing|run the ball|run (on|it)\b", t, re.I):
        us = [u for u in us if u != "rush"]  # 'how many plays do these teams run' is pace
    if (not us or us == ["eff"]) and OVERVIEW.search(t) and not re.search(
            r"offen[cs]e|defen[cs]e|\bscore\b|move the ball|put up", t, re.I):
        return ["all"]
    return us


def wants(q: str) -> bool:
    """Cheap text-only check: a short unit question with no pick/line/stat ask in it."""
    t = (q or "").replace("’", "'").strip()
    return (bool(t) and len(t.split()) <= MAX_WORDS and bool(_QSHAPE.search(t)) and not _NOT.search(t)
            and bool(ask(t)))


def parse(q: str, ctx: dict | None = None, src=None) -> dict | None:
    """{'away', 'home', 'units', 'off' (the offense asked about, or None), 'game'} or None (can't place it)."""
    src = src or SOURCE
    us = ask(q)
    if not us:
        return None
    g = src.game(q, ctx or {})
    if not g:
        return None
    tab = src.units_table()
    a, h = g["away"], g["home"]
    if not units._row(a, tab) or not units._row(h, tab):
        return None
    off = None if us[0] in ("pace", "turnovers", "all") else units.ask_offense(q, a, h)
    return {"away": a, "home": h, "units": us, "off": off, "game": g}


# ---------------------------------------------------------------------------
# the answer
# ---------------------------------------------------------------------------

SHORT = {"rush": ("run game", "run D"), "pass": ("passing game", "pass D"), "protect": ("protection", "pass rush"),
         "redzone": ("red-zone offense", "red-zone D"), "explosive": ("big-play offense", "big-play D"),
         "eff": ("offense", "defense")}
TOPIC = {"rush": "on the ground", "pass": "through the air", "protect": "up front", "redzone": "in the red zone",
         "explosive": "on big plays", "eff": "overall", "turnovers": "in turnovers", "pace": "on pace"}
MAIN = ("rush", "pass", "protect", "redzone", "explosive")
LIMIT = 600  # + gemma's 14-word line stays under 700


def _p(name: str) -> str:
    return units._poss(name)


def _decided(e: dict) -> str:
    return f"edge {e['winner']}" if e.get("winner") else "no clear edge"


def _compact(e: dict) -> str:
    ol, dl = SHORT[e["unit"]]
    return f"{_p(e['off'])} {ol} #{e['ro']} vs {_p(e['def'])} {dl} #{e['rd']} ({_decided(e)})"


_YESNO = re.compile(r"(^|[.!?:]\s+)\W*(can|could|will|would|does|do|did|is|are|should)\b|\b(can|will|could) (they|he|it)\W*$", re.I)


def subject(q: str, away: str, home: str) -> str | None:
    """The first team the text names (the subject of 'can X stop Y' / 'will X's pass rush get home')."""
    qs = [x for x in re.split(r"(?<=[.!?])\s+", q or "") if "?" in x]
    q = qs[-1] if qs else q  # 'UCLA -3.5 at Michigan. Can Michigan score on UCLA?': the question's subject
    tn = f" {cfbd.norm(re.sub(r"['’]s\b", "", q or ''))} "
    spots = sorted((p, t) for t, o in ((away, home), (home, away)) for p, _ in units._all_pos(t, tn, o))
    return spots[0][1] if spots else None


def _lead(e: dict, q: str, n: int, rng: random.Random) -> str:
    """The direct answer for one offense-vs-defense pairing, decided, ranks among FBS. A yes/no question gets a
    yes/no opener that follows its subject: 'can X stop Y' / 'will X's pass rush get home' is about X's DEFENSE."""
    u, o, d, w = e["unit"], e["off"], e["def"], e.get("winner")
    ol, dl = SHORT[u]
    what = units.PAIRS[u][4]
    if w == o:
        core = f"{_p(o)} {ol} has the edge on {_p(d)} {dl}, #{e['ro']} vs #{e['rd']} in {what}"
    elif w == d:
        core = f"{_p(d)} {dl} has the edge on {_p(o)} {ol}, #{e['rd']} vs #{e['ro']} in {what}"
    else:
        core = f"{_p(o)} {ol} is #{e['ro']} and {_p(d)} {dl} #{e['rd']} in {what}, no clear edge"
    if not _YESNO.search(q or ""):
        return core[0].upper() + core[1:] + "."
    asked_def = subject(q, o, d) == d
    good = d if asked_def else o
    if w is None:
        return rng.choice(["It's close: ", "Coin flip: ", "Hard to say: "]) + core + "."
    if w == good:
        return rng.choice(["Yes: ", "Yeah, ", "I think so: "]) + core + "."
    return rng.choice(["Not really: ", "Not much: ", "Tough ask: "]) + core + "."


def _pace_sentence(a: str, h: str, tab: dict, n: int) -> tuple[str, str]:
    """(sentence, 'fast'|'slow'|'clash'|'even') for a pace ask, both teams' numbers."""
    ra, rh = units._row(a, tab), units._row(h, tab)
    ka, kh = ra.get("pace_rank"), rh.get("pace_rank")
    if not ka or not kh or ra.get("pace") is None or rh.get("pace") is None:
        return "", "even"
    s = (f"{a} runs {round(ra['pace'])} plays a game (#{ka} of {n}), {h} {round(rh['pace'])} (#{kh})")
    if ka <= 40 and kh <= 40:
        return s + ": both play fast, so more snaps and more possessions.", "fast"
    if ka >= n - 40 and kh >= n - 40:
        return s + ": both play slow, so fewer possessions and a grind.", "slow"
    if abs(ka - kh) >= units.GAP:
        fast = a if ka < kh else h
        return s + f": {fast} wants to speed it up.", "clash"
    return s + ": similar pace, no real tempo edge.", "even"


def _turnover_sentence(a: str, h: str, tab: dict) -> tuple[str, dict | None]:
    e = units.turnovers_edge(a, h, tab)
    if not e:
        return "", None
    bits = []
    for t in (a, h):
        r = units._row(t, tab)
        s = r["stats"]
        bits.append(f"{t} has {int(s.get('turnoversOpponent') or 0)} takeaways (#{r['take_d_rank']}) and "
                    f"{int(s.get('turnovers') or 0)} giveaways (#{r['give_o_rank']} fewest)")
    tail = f"edge {e['winner']}" if e.get("winner") else "no clear turnover edge"
    return "; ".join(bits) + f": {tail}.", e


def _style(first: str, second: str, tab: dict) -> str:
    """Run rate and plays a game, both teams (the asked offense first)."""
    bits = []
    for i, t in enumerate((first, second)):
        r = units._row(t, tab)
        rate = units._get(r["raw"], "offense", "rushingPlays", "rate")
        pace = r.get("pace")
        if rate is None:
            continue
        b = (f"{t} runs it {units._pct(rate)} of the time" if i == 0 else f"{t} {units._pct(rate)}")
        b += " (option)" if rate >= 0.72 else ""
        if pace:
            b += f" at {round(pace)}" + (" plays a game" if i == 0 else "")
        bits.append(b)
    return ("Style: " + ", ".join(bits) + ".") if len(bits) == 2 else ""


def _sp(first: str, second: str, src) -> str:
    s1, s2 = src.sp(first) or {}, src.sp(second) or {}
    if not all(x.get(k) for x in (s1, s2) for k in ("off_rank", "def_rank")):
        return ""
    return (f"SP+ has {_p(first)} offense #{s1['off_rank']} vs {_p(second)} D #{s2['def_rank']}, and {_p(second)} "
            f"O #{s2['off_rank']} vs {_p(first)} D #{s1['def_rank']}.")


def _team_line(team: str, g: dict) -> str:
    sp = g.get("spread")
    if sp is None:
        return team
    line = sp if cfbd.norm(team) == cfbd.norm(g["home"]) else -sp
    return f"{team} {line:+g}" if line else f"{team} at a pick'em"


def verdict(edges: list[dict], g: dict, pace_kind: str = "") -> dict:
    """One computed 'so what' from the decided edges (plus pace when asked):
    {'kind': side|over|under|none, 'team'?, 'strength'?, 'why'?}."""
    if pace_kind in ("fast", "slow"):
        return {"kind": "over" if pace_kind == "fast" else "under", "why": "pace"}
    won = [e for e in edges if e.get("winner")]
    if not won:
        return {"kind": "none", **({"why": "pace"} if pace_kind else {})}
    teams = {e["winner"] for e in won}
    if len(teams) == 1:
        gap = sum(e.get("gap") or 0 for e in won)
        return {"kind": "side", "team": won[0]["winner"],
                "strength": "lean" if len(won) >= 2 or gap >= 2 * units.GAP else "slight lean"}
    paired = [e for e in won if e["unit"] in units.PAIRS]
    if paired and len(paired) == len(won):
        offs = {e["off"] for e in paired if e["winner"] == e["off"]}
        defs = {e["def"] for e in paired if e["winner"] == e["def"]}
        if len(offs) == 2 and not defs:
            return {"kind": "over"}
        if len(defs) == 2 and not offs:
            return {"kind": "under"}
    return {"kind": "none", "why": "split"}


def _verdict_text(v: dict, g: dict, topic: str, rng: random.Random) -> str:
    so = rng.choice(["So", "Bottom line,", "Add it up and"])
    tot = f" on {g['total']:g}" if g.get("total") else ""
    k = v["kind"]
    if k == "side":
        t = v["team"]
        return (f"{so} it's {_p(t)} edge {topic}, {v['strength']} {_team_line(t, g)}"
                + (" (our line)." if g.get("league") and g.get("spread") is not None else "."))
    if k in ("over", "under"):
        if v.get("why") == "pace":
            return f"{so} pace alone tilts the {k}{tot}."
        why = "both offenses have the edge" if k == "over" else "both defenses win their matchups"
        return f"{so} {why}, which tilts the {k}{tot}."
    if v.get("why") == "split":
        return f"{so} the edges cut both ways: a coin flip on the units, it doesn't move the number for me."
    if v.get("why") == "pace":
        return f"{so} pace alone doesn't tilt the total{tot}, and it doesn't move the number for me."
    return f"{so} there's no clear unit edge either way: it doesn't move the number for me."


def _topic(us: list[str], edges: list[dict]) -> str:
    """Where the verdict's edge lives, in words."""
    won = [e for e in edges if e.get("winner")]
    wu = {e["unit"] for e in won}
    if {"rush", "protect"} <= set(us):
        return "in the trenches"
    if len(wu) == 1 and len(won) >= 2:
        return "on both sides of the ball"
    if len(wu) > 1:
        return "in these matchups"
    return TOPIC.get(next(iter(wu), us[0]), "overall")


def _overview_units(a: str, h: str, tab: dict) -> list[str]:
    """For 'where does X have the edge': the unit of each side's biggest decided edge, else the overall offense."""
    out = []
    for t in (a, h):
        best = max((e for u in MAIN for e in units.edges(u, a, h, tab) if e.get("winner") == t),
                   key=lambda e: e["gap"], default=None)
        if best and best["unit"] not in out:
            out.append(best["unit"])
    return out or ["eff"]


def build(q: str, ctx: dict | None = None, src=None, seed=None) -> dict | None:
    """{'text', 'edges', 'verdict', 'units'} or None when the lane can't answer it (full brain then)."""
    src = src or SOURCE
    p = parse(q, ctx, src)
    if not p:
        return None
    tab = src.units_table()
    a, h, off, g = p["away"], p["home"], p["off"], p["game"]
    n = tab.get("_n") or 136
    rng = random.Random(seed if seed is not None else f"{q}|{dt.date.today().isoformat()}")
    us = _overview_units(a, h, tab) if p["units"] == ["all"] else p["units"][:2]
    head, rest, edges, pace_kind = "", [], [], ""
    for u in us:
        if u == "pace":
            s, pace_kind = _pace_sentence(a, h, tab, n)
            if s:
                if head:
                    rest.append(s)
                else:
                    head = s
            continue
        if u == "turnovers":
            s, e = _turnover_sentence(a, h, tab)
            if e:
                edges.append(e)
                if head:
                    rest.append(s)
                else:
                    head = s
            continue
        es = units.edges(u, a, h, tab)
        if len(es) < 2:
            continue
        if off:
            es.sort(key=lambda e: 0 if e["off"] == off else 1)
        else:  # no direction asked ('who finishes drives'): the decided one first
            es.sort(key=lambda e: -(e["gap"] if e.get("winner") else -1))
        edges += es
        if not head:
            head = _lead(es[0], q, n, rng)
            rest.append(rng.choice(["Other way: ", "Flip it: ", "The other direction: "]) + _compact(es[1]) + ".")
        else:
            rest.append(f"And {TOPIC[u]}: " + "; ".join(_compact(e) for e in es) + ".")
    if not head:
        return None
    v = verdict(edges, g, pace_kind)
    first = off or a
    second = h if first == a else a
    style = _style(first, second, tab)
    spl = _sp(first, second, src)
    topic = _topic(us, edges)
    vt = _verdict_text(v, g, topic, rng)
    # reads more like a text (genius mid1/mid2: 'stat dump', voice 2): the style line only for a run / pace ask
    style = style if set(us) & {"rush", "pace"} else ""
    for extra in ([style, spl], [spl], []):  # drop style first, then SP+; never the other direction or the verdict
        text = " ".join(x for x in [head, *rest, *extra, vt] if x)
        if len(text) <= LIMIT:
            break
    else:
        text = " ".join(x for x in [head, rest[0] if rest else "", vt] if x)
    return {"text": text, "edges": edges, "verdict": v, "units": us}


def answer(ctx: dict, q: str, src=None) -> str | None:
    """router fact lane entry: the full code answer, or None (the full brain answers)."""
    b = build((q or "").split(" ||| ")[0], ctx, src)
    return b["text"] if b else None
