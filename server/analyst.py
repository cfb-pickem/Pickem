"""
Analyst takes: Claude does the heavy game thinking ahead of time so Picky's
small live model only has to deliver it in his voice.

Called by dossier.py right after a game's dossier is (re)built, so it runs on
the dossier's schedule (slate set / Fri 5 PM / Sat 7 AM CT) and costs one
extra no-tools `claude -p` call per game per run (Opus via picky_claude, Charlie's
subscription, ANTHROPIC_API_KEY stripped).

The brief is numbered so every reason must cite where it came from:
  S1..  computed signals from chat.matchup_signals (market vs pool line, SP+,
        efficiency, unit edges, FPI, line movement, splits, weather), the
        pool line, the market and the CFBD season lines;
  C1..  the dossier's validated claims (injuries, news, opinions, rumors).
validate_take() drops any reason whose numbers aren't in the items it cites,
any capitalized name that isn't in the brief, URLs and report-speak. Too few
reasons left = no take (the previous one stays).

Picky's computed lean stays authoritative: the take stores the lean next to
Claude's pick and an explicit `agrees_with_lean`; chat.take_note tells the
model to keep the lean and voice the disagreement as the other side.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re

log = logging.getLogger("picky.analyst")

MAX_REASONS = 5
MIN_REASONS = 2
TAKE_TIMEOUT_S = 300

TAKE_SCHEMA = {
    "type": "object",
    "properties": {
        "pick": {"type": "string"},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "reasons": {"type": "array", "items": {
            "type": "object",
            "properties": {"text": {"type": "string"}, "refs": {"type": "array", "items": {"type": "string"}}},
            "required": ["text", "refs"]}},
        "counter": {"type": "object",
                    "properties": {"text": {"type": "string"}, "refs": {"type": "array", "items": {"type": "string"}}},
                    "required": ["text", "refs"]},
        "market_missing": {"type": "object",
                           "properties": {"text": {"type": "string"},
                                          "refs": {"type": "array", "items": {"type": "string"}}},
                           "required": ["text", "refs"]},
        "quotable": {"type": "string"},
    },
    "required": ["pick", "confidence", "reasons", "counter", "market_missing", "quotable"],
}

PROMPT = """You are the sharpest college football handicapper in a friends' pick'em league, prepping ammo for
Picky, the league's trash-talking bot. Picky's live brain is a small model: he will say your take out loud in
a group text, in his own voice. Your job is the thinking.

GAME: {away} (away) at {home} (home), {season} week {week}, kickoff {kick}.
Members pick against the POOL LINE: {pool}.

BRIEF (the only facts you may use; cite item ids):
{items}

Picky's own computed lean from these signals: {lean_txt}. You may disagree if the brief supports it; if you do,
make the case honestly. Don't just echo the lean: weigh the signals, the matchups and the reported news.

Write:
- pick: exactly "{away}" or "{home}", the side to take against the pool line.
- confidence: high, medium or low.
- reasons: the 3 to 5 strongest reasons for the pick, strongest first. Each is ONE short spoken sentence
  (under 30 words) a sharp buddy would say out loud, with refs = the ids it relies on. Prefer angles beyond
  FPI and the raw line: price gaps, efficiency and unit matchups, line movement, money vs tickets, weather,
  reported availability, situational spots.
- counter: the best honest argument for the other side, one spoken sentence, with refs.
- market_missing: one spoken sentence on what the line or the public is underrating, with refs.
- quotable: one punchy line Picky can say verbatim, under 22 words, natural spoken voice, no hashtags.

Hard rules:
- Every number and every player name you write must appear in the items you cite. No outside knowledge,
  no stats you remember, no records, no scores, no injuries that aren't in the brief.
- Rumors and opinions stay labeled as such ("reportedly", "one beat writer thinks").
- Call teams only by the school names in the brief, never nicknames or mascots (no Bison, Mustangs, Broncos...).
- Talk like a person: contractions, plain words. Never write labels like "Reason", "Confidence",
  "Counter-argument", "the market is missing", no lists inside a sentence, no URLs, no betting-site names.
- Football only: no gambling advice beyond the pool pick, no money talk.
Return only the JSON object."""

# words a spoken sentence may capitalize without them being in the brief
_COMMON = set("""I I'd I'll I'm I've A An And As At Be But By Do Even For From Get Give He He's Her His How If In
Is It It's Its Just Last Let Lay Look Love Make Me My No Not Now Of Off On One Only Or Our Out Picky Plus Points
Pool Really Same Says She So Sure Take Than That That's The Their Them Then There There's These They They're This
Those Three To Too Two Until Up Us Vegas We Week What What's When Where Which While Who Why With Yeah Yes You
You're Your Also Both Every Everyone Nobody Somebody Sharp Sharps Public Money Market Books Line Lines Saturday
Friday Sunday Thursday Home Road Away Fade Ride Back Bet Don't Can't Won't Isn't Aren't Doesn't Didn't Here
Here's Give Watch Trust Forget Meanwhile Still Never Always Whoever Whatever Bottom Big Small Tickets Overall
Defense Offense FPI SP+ PPA CFBD ESPN DraftKings Open-Meteo Wind Rain Weather Kickoff Gimme Take Laying
Getting Folks People Everybody Coach Coaches QB QBs""".split())
_NUM = re.compile(r"\d+(?:\.\d+)?")
_CAPWORD = re.compile(r"\b[A-Z][A-Za-z'’.+-]*[A-Za-z+]")
_REPORTY = re.compile(r"\b(reason (one|two|three|\d)|confidence|counter-?argument|what the market is missing|"
                      r"market_missing|in conclusion|key factors?|bottom line:|takeaway)\b|https?://|www\.|\n\s*[-*•]",
                      re.I)


# -----------------------------------------------------------------------------
# brief
# -----------------------------------------------------------------------------

def chat_game(g: dict) -> dict:
    """dossier/all_games row -> the shape chat.matchup_signals takes."""
    return {"id": g["GameId"], "away": g["Away"], "home": g["Home"], "line": g.get("line"),
            "fpi_margin": g.get("fpi_margin"), "start": g.get("Start (CT)")}


def gather_inputs(games: list[dict]) -> tuple[dict, dict | None, dict]:
    """(espn notes, cfbd index, edge extras) for chat-shaped games. Same
    sources /chat uses for a breakdown question; never raises."""
    import chat  # lazy: heavy module, only needed when a take is built
    import edge
    import espn
    idx = None
    try:
        idx = chat._cfbd_index()
    except Exception as e:  # noqa: BLE001
        log.warning("cfbd index unavailable: %s", e)

    async def go():
        notes = await espn.game_notes([g["id"] for g in games])
        extras = await edge.load(games, notes, idx)
        return notes, extras
    try:
        notes, extras = asyncio.run(go())
    except Exception as e:  # noqa: BLE001
        log.warning("espn/edge inputs unavailable: %s", e)
        notes, extras = {}, {}
    return notes or {}, idx, extras or {}


def build_brief(cg: dict, dossier: dict | None, notes: dict | None, idx: dict | None,
                extras: dict | None) -> tuple[dict[str, str], dict]:
    """({item id: text}, matchup_signals result)."""
    import chat
    m = chat.matchup_signals(cg, notes or {}, idx, extras or {})
    away, home = cg["away"], cg["home"]
    items: dict[str, str] = {}

    def add(prefix: str, text: str):
        items[f"{prefix}{sum(1 for k in items if k.startswith(prefix)) + 1}"] = re.sub(r"\s+", " ", text).strip()
    add("S", f"pool line: {chat.pool_line(away, home, cg.get('line'))}")
    mk = chat._market(notes or {}, away, home)
    if mk:
        add("S", f"betting market (ESPN): {mk}")
    lv = chat.line_value(notes or {}, away, home, cg.get("line"))
    if lv:
        add("S", lv)
    for x in sorted(m["signals"], key=lambda x: -x["w"]):
        if x["why"].startswith("market:") and lv:
            continue  # same fact as the line-value item
        add("S", x["why"] + (f" (favors {x['side']})" if x["side"] else ""))
    for e in m["edges"]:
        add("S", f"unit edge: {e['text']}")
    if idx:
        for ln in chat.cfbd_game_lines(away, home, idx):
            add("S", ln.strip())
    d = dossier or {}
    for k, label in (("facts", None), ("opinions", "opinion, not fact"), ("rumors", "RUMOR, unconfirmed")):
        for c in d.get(k) or []:
            lab = label or ("injury report, status " + str(c.get("status") or "unknown") if c.get("kind") == "injury"
                            else "news")
            add("C", f"[{lab}, {c.get('date', '')}] {c.get('text', '')}")
    return items, m


# -----------------------------------------------------------------------------
# validate
# -----------------------------------------------------------------------------

def _names_ok(text: str, brief_low: str) -> list[str]:
    bad = []
    for m in _CAPWORD.finditer(text.replace("’", "'")):
        w = m.group(0).rstrip(".")
        w = re.sub(r"'s$", "", w)
        if w in _COMMON or w.lower() in brief_low:
            continue
        bad.append(w)
    return bad


def _mascot(text: str) -> str | None:
    """A team nickname in the text (Picky's live filter drops 'the Bison' etc.)."""
    import chat
    if chat.MASCOT_ANY is None:
        try:
            asyncio.run(chat.load_mascots())
        except RuntimeError:  # already inside an event loop: skip the check
            return None
    m = chat.MASCOT_ANY.search(text) if chat.MASCOT_ANY else None
    return m.group(0) if m else None


def _nums_ok(text: str, src: str) -> list[str]:
    have = set(_NUM.findall(src))
    return [n for n in _NUM.findall(text) if n not in have and n not in ("0", "1")]


def _clean(s) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip()


def validate_take(raw: dict, items: dict[str, str], cg: dict, m: dict) -> tuple[dict | None, list[str]]:
    """(take or None, problems). Parts that fail are dropped; the take
    survives with a valid pick and at least MIN_REASONS reasons."""
    probs: list[str] = []
    if not isinstance(raw, dict):
        return None, ["not an object"]
    away, home = cg["away"], cg["home"]
    pick = _clean(raw.get("pick"))
    pick = away if pick.lower() == away.lower() else home if pick.lower() == home.lower() else None
    if not pick:
        return None, [f"pick {raw.get('pick')!r} is neither team"]
    brief_all = " ".join(items.values()) + f" {away} {home}"
    brief_low = brief_all.lower()
    header = items.get("S1", "")

    def part(p, what: str, need_refs: bool = True, max_words: int = 40) -> dict | None:
        if not isinstance(p, dict):
            probs.append(f"{what}: missing")
            return None
        text = _clean(p.get("text"))
        refs = [r for r in (p.get("refs") or []) if r in items]
        if not 8 <= len(text) <= 300 or len(text.split()) > max_words:
            probs.append(f"{what}: length")
            return None
        if need_refs and not refs:
            probs.append(f"{what}: no valid refs ({p.get('refs')})")
            return None
        src = " ".join(items[r] for r in refs) + " " + header
        why = (_mascot(text) and f"nickname {_mascot(text)!r}") or (_REPORTY.search(text) and "report-speak") or (_nums_ok(text, src) and f"numbers {_nums_ok(text, src)}") \
            or (_names_ok(text, brief_low) and f"names {_names_ok(text, brief_low)}")
        if why:
            probs.append(f"{what}: {why}: {text[:90]}")
            return None
        return {"text": text, "refs": refs}

    reasons = [r for i, x in enumerate((raw.get("reasons") or [])[:MAX_REASONS])
               if (r := part(x, f"reason {i + 1}"))]
    if len(reasons) < MIN_REASONS:
        return None, probs + [f"only {len(reasons)} usable reasons"]
    counter = part(raw.get("counter"), "counter")
    angle = part(raw.get("market_missing"), "market_missing")
    q = _clean(raw.get("quotable")).strip('"“”')
    if q and (len(q.split()) > 28 or _REPORTY.search(q) or _mascot(q) or _nums_ok(q, brief_all) or _names_ok(q, brief_low)):
        probs.append(f"quotable dropped: {q[:90]}")
        q = ""
    conf = raw.get("confidence") if raw.get("confidence") in ("high", "medium", "low") else "medium"
    lean = m.get("lean")
    return {
        "pick": pick, "confidence": conf, "reasons": reasons, "counter": counter, "market_missing": angle,
        "quotable": q or None, "lean": lean, "lean_strength": m.get("strength"),
        "agrees_with_lean": lean is None or lean == pick,
    }, probs


# -----------------------------------------------------------------------------
# build
# -----------------------------------------------------------------------------

def build_prompt(cg: dict, season, week, items: dict[str, str], m: dict, kick: str) -> str:
    import chat
    lean = m.get("lean")
    lean_txt = (f"{lean} ({m.get('strength')})" if lean else "none (the signals split or show no edge)")
    return PROMPT.format(away=cg["away"], home=cg["home"], season=season, week=week, kick=kick,
                         pool=chat.pool_line(cg["away"], cg["home"], cg.get("line")),
                         items="\n".join(f"{k}: {v}" for k, v in items.items()), lean_txt=lean_txt)


def build_take(g: dict, dossier: dict, season, week, now: dt.datetime, research, inputs=None) -> dict:
    """The analyst_take block for one game's dossier, or raises (caller keeps
    the previous take). `research(prompt)` is one no-tools claude -p call returning the JSON."""
    cg = chat_game(g)
    notes, idx, extras = inputs or gather_inputs([cg])
    items, m = build_brief(cg, dossier, (notes or {}).get(cg["id"]), idx, extras)
    if len(items) < 3:
        raise RuntimeError("brief too thin for a take")
    kick = str(g.get("Start (CT)") or "TBD")
    raw = research(build_prompt(cg, season, week, items, m, kick))
    take, probs = validate_take(raw, items, cg, m)
    if not take:
        raise RuntimeError("take rejected: " + "; ".join(probs)[:400])
    import picky_claude
    take.update({"generated_at": now.isoformat(timespec="seconds"), "by": f"claude -p {picky_claude.model_for('dossier-take')} (no tools)",
                 "brief": items, "dropped": probs})
    return take
