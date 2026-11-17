"""
Ackbar is Picky's nemesis (Charlie, 2026-11-15; "make sure he still knows to be
antagonistic to ackbar always", 2026-11-17). chat.ackbar_note only rides in the
model's prompt, so code lanes (router fact answers, race, recall, canned lines)
and drafts whose jab got trimmed went out with no antagonism at all.

tail() runs at group_chat's send point (group, DMs, web /chat, Chinese before
translation): when Ackbar sent the text or is named in it, the reply ends with
one jab aimed at him. Every jab is built from a real fact:
  - lore.json member lines: favorite %, last 8, worst blown picks (favorites
    that lost outright), stats-page oddities, head to head
  - burn_book.json: his record/place (standing) and the Opus-written Ackbar
    lines whose cited facts are still current (2nd person, so only when he's
    the sender or in the group)
A good number is never mocked, only spun ("28-20, training wheels the whole
way"). No family, bodies, threats or slurs: the templates are fixed text.
Rotation: the shared no_repeat ledger (target "Ackbar"); when every jab is on
cooldown, the least recently used one goes, never the last one sent.
Only Ackbar gets this; everyone else's replies are untouched.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

import no_repeat

TEAM = "Ackbar"
HERE = Path(__file__).resolve().parent
LORE = Path(os.environ.get("PICKY_LORE_CACHE", str(HERE / "cache" / "lore.json")))
STALE_S = 3 * 86400  # a burn-book standing older than this isn't trusted for a record jab

ASK = re.compile(r"\b(?:admiral )?ac?kbar\b", re.I)  # same as chat._ACKBAR_ASK
# a reply that already takes a real shot at him (the model's ackbar_note rider, a burn-book line)
_JAB = re.compile(r"\b(chalk\w*|training wheels|nemesis|no guts|gutless|of course (you|he) did|naturally|"
                  r"lay(s|ing)? (the )?points|takes? the favou?rite|favou?rites? (again|every|like)|"
                  r"the hook knows|scared of (an |the )?(underdog|dog)s?|has (your|his) number)\b", re.I)


def involved(q: str, sender_team: str | None) -> bool:
    return sender_team == TEAM or bool(ASK.search(q or ""))


def has_jab(reply: str) -> bool:
    return bool(_JAB.search(reply or ""))


def _ord(n: int) -> str:
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def _load(p: Path) -> dict:
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return {}


def facts(lore: dict | None = None, book: dict | None = None, now: float | None = None) -> dict:
    """The parsed facts the jabs use (each key only when its source line is there)."""
    if lore is None:
        lore = _load(LORE)
    if book is None:
        import burn_book
        book = burn_book.load()
    now = now if now is not None else time.time()
    lines = (lore.get("members") or {}).get(TEAM) or []
    f: dict = {}
    for ln in lines:
        if " so far " in ln:
            m = re.search(r"takes the favorite (\d+)% of the time", ln)
            if m:
                f["fav"] = int(m.group(1))
            m = re.search(r"last 8 picks (\d+)-(\d+)", ln)
            if m:
                f["last8"] = (int(m.group(1)), int(m.group(2)))
        if "worst blown picks" in ln:
            f["blown"] = [dict(team=m.group(1), line=m.group(2), score=m.group(3), game=m.group(4),
                               season=m.group(5), week=m.group(6))
                          for m in re.finditer(r"([A-Z][\w&.' ]+?) (-\d+(?:\.\d)?) missed the cover by [\d.]+, lost "
                                               r"outright (\d+-\d+) \(([^,()]+), (\d{4}) week (\d+)\)", ln)]
        m = re.search(r"oddity: Ackbar is (\d+)-(\d+) in ([A-Z][\w&.' ]+?) games over the last two seasons", ln)
        if m and int(m.group(1)) < int(m.group(2)):
            f.setdefault("odd", []).append((m.group(3), int(m.group(1)), int(m.group(2))))
        m = re.search(r"oddity: Ackbar has picked against ([A-Z][\w&.' ]+?) (\d+) times over the last two seasons "
                      r"\(\S+\) and gone (\d+)-(\d+)", ln)
        if m and int(m.group(3)) < int(m.group(4)):
            f.setdefault("against", []).append((m.group(1), int(m.group(3)), int(m.group(4))))
    f["h2h"] = {o: (int(w), int(l)) for o, (w, l) in ((lore.get("h2h") or {}).get(TEAM) or {}).items()}
    st = (book.get("standing") or {}).get(TEAM) or {}
    try:
        fresh = now - _ts(book.get("checked_at") or book.get("generated_at")) <= STALE_S
    except (TypeError, ValueError):
        fresh = False
    if fresh and st.get("w") is not None and st.get("rank"):
        f["record"] = (int(st["w"]), int(st["l"]), int(st["rank"]), bool(st.get("good")))
    return f


def _ts(v) -> float:
    if isinstance(v, (int, float)):
        return float(v)
    import datetime as dt
    return dt.datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()


def code_jabs(f: dict, you: bool, asker: str | None = None) -> list[dict]:
    """[{text, angle}] in 2nd person (you=True, Ackbar is the sender) or about him."""
    out = []

    def add(angle: str, y: str, h: str):
        out.append({"text": y if you else h, "angle": f"nemesis:{angle}"})

    if f.get("fav"):
        p = f["fav"]
        add("fav", f"You take the favorite {p}% of the time. Training wheels on every pick.",
            f"Ackbar takes the favorite {p}% of the time. Training wheels on every pick.")
        add("fav2", f"{p}% favorites this season. You've never met an underdog you'd trust with a dollar.",
            f"Ackbar's on the favorite {p}% of the time. He's never met an underdog he'd trust with a dollar.")
    if f.get("record"):
        w, l, rank, good = f["record"]
        if good or w >= l:  # spin a good number, never mock it
            add("record", f"{w}-{l} and {_ord(rank)}, riding favorites like training wheels the whole way.",
                f"Ackbar's {w}-{l} and {_ord(rank)}, riding favorites like training wheels the whole way.")
        else:
            add("record", f"{w}-{l} laying points every week. The chalk isn't saving you.",
                f"Ackbar's {w}-{l} laying points every week. The chalk isn't saving him.")
    if f.get("last8"):
        a, b = f["last8"]
        if a >= b:
            add("last8", f"{a}-{b} your last 8, sure. Even training wheels work on a flat road.",
                f"Ackbar's {a}-{b} his last 8, sure. Even training wheels work on a flat road.")
        else:
            add("last8", f"{a}-{b} your last 8. All that chalk and still face down.",
                f"Ackbar's {a}-{b} his last 8. All that chalk and still face down.")
    for b in (f.get("blown") or [])[:2]:
        away, _, home = b["game"].partition(" at ")
        opp = home if away == b["team"] else away
        add(f"blown:{b['team']}:{b['season']}",
            f"Remember {b['team']} {b['line']} losing to {opp} outright, {b['score']}, in {b['season']} week {b['week']}? "
            "That's what laying points buys you.",
            f"Ackbar laid {b['line']} with {b['team']} and watched them lose to {opp} outright, {b['score']}, "
            f"in {b['season']} week {b['week']}. That's what all that chalk buys.")
    for team, w, l in (f.get("odd") or [])[:2]:
        add(f"odd:{team}", f"{w}-{l} in {team} games the last two seasons. {team} has your number.",
            f"Ackbar's {w}-{l} in {team} games the last two seasons. {team} has his number.")
    for team, w, l in (f.get("against") or [])[:1]:
        add(f"against:{team}", f"{w}-{l} picking against {team} the last two seasons, and you keep doing it.",
            f"Ackbar's {w}-{l} picking against {team} the last two seasons, and he keeps doing it.")
    h2h = f.get("h2h") or {}
    if not you and asker and asker in h2h and h2h[asker][0] < h2h[asker][1]:
        w, l = h2h[asker]
        add(f"h2h:{asker}", "", f"Ackbar's {w}-{l} against you when you split sides. All chalk, no answers for you.")
    if you and h2h:
        opp, (w, l) = min(h2h.items(), key=lambda kv: kv[1][0] - kv[1][1])
        if w < l:
            add(f"h2h:{opp}", f"You're {w}-{l} against {opp} when you split sides. Laying points won't fix that.", "")
    return [x for x in out if x["text"] and not no_repeat.banned_tic(x["text"])]


def book_jabs(book: dict | None = None) -> list[dict]:
    """The Opus-written Ackbar lines (2nd person) whose cited facts are all still current."""
    if book is None:
        import burn_book
        book = burn_book.load()
    now_facts = set(book.get("facts_now") or [])
    out = []
    for x in (book.get("members") or {}).get(TEAM) or []:
        if x.get("kind") not in ("roast", "grudging") or not x.get("text"):
            continue
        if x.get("facts") and not set(x["facts"]) <= now_facts:
            continue
        t = x["text"].strip()
        out.append({"text": t[:1].upper() + t[1:], "raw": t, "angle": f"nemesis:book:{x.get('id') or t[:30]}"})
    return out


def _last_used(text: str, entries: list, tk: str) -> float:
    n = no_repeat.normalize(text)
    return max((float(e.get("ts") or 0) for e in entries if e.get("target") == tk
                and no_repeat._sim_norm(n, e.get("norm") or "") >= no_repeat.SIM_THRESHOLD), default=0.0)


def pick(cands: list[dict], chat_id: str | None = None, ledger: "no_repeat.Ledger | None" = None) -> dict | None:
    """First candidate the ledger calls fresh; else the least recently used one, never the last jab sent."""
    if not cands:
        return None
    ledger = ledger or no_repeat.default_ledger()
    for c in cands:
        if not ledger.is_repeat(c["text"], target=TEAM, chat_id=chat_id, angle=c["angle"]):
            return c
    tk = no_repeat._tkey(TEAM)
    entries = [e for e in ledger._load().get("entries") or [] if e.get("target") == tk]
    last = entries[-1].get("norm") if entries else None
    pool = [c for c in cands if not last or no_repeat._sim_norm(no_repeat.normalize(c["text"]), last)
            < no_repeat.SIM_THRESHOLD] or cands
    return min(pool, key=lambda c: _last_used(c["text"], entries, tk))


def jab(q: str, sender_team: str | None, chat_id: str | None = None, group: bool = False,
        lore: dict | None = None, book: dict | None = None, ledger=None, now: float | None = None) -> dict | None:
    """{text, angle}: one jab at Ackbar for this text, or None (not involved / no facts)."""
    if not involved(q, sender_team):
        return None
    you = sender_team == TEAM
    if book is None:
        import burn_book
        book = burn_book.load()
    f = facts(lore, book, now)
    cands = code_jabs(f, you, asker=sender_team)
    if you or group:
        # Opus lines talk to him: fine when he sent it, or in the group (he's in it): 'Ackbar, ...'
        bj = book_jabs(book) if you else [{**c, "text": "Ackbar, " + c["raw"]} for c in book_jabs(book)]
        mixed = [c for pair in zip(bj, cands) for c in pair]  # alternate book and code lines
        n = min(len(bj), len(cands))
        cands = mixed + bj[n:] + cands[n:]
    return pick(cands, chat_id, ledger)


def tail(reply: str, q: str, sender_team: str | None, chat_id: str | None = None, group: bool = False,
         force: bool = False, ledger=None, **kw) -> str:
    """`reply` with one Ackbar jab on the end when he's involved. force=True for code-written
    answers (they never carry one); model/book replies keep theirs if they already have one.
    Fails open: any error returns the reply unchanged."""
    try:
        if not reply or not involved(q, sender_team) or (not force and has_jab(reply)):
            return reply
        ledger = ledger or no_repeat.default_ledger()
        j = jab(q, sender_team, chat_id, group, ledger=ledger, **kw)
        if not j:
            return reply
        ledger.record(j["text"], target=TEAM, chat_id=chat_id, angle=j["angle"])
        sep = "" if reply.rstrip().endswith((".", "!", "?")) else "."
        return f"{reply.rstrip()}{sep} {j['text']}"
    except Exception:  # noqa: BLE001 - the bit never blocks a reply
        import logging
        logging.getLogger("picky.nemesis").warning("nemesis tail failed", exc_info=True)
        return reply
