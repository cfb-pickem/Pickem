"""
Picky's comeback and banter bank: fresh lines for recurring heckles ("you
suck", "all bark no bite", "are you a bot") and casual off-topic questions
(cereal, beer, other sports), written OFFLINE by `claude -p` and refreshed
weekly. Live replies stay local: the bank is ammo for the local model, never
a script. Each matching text gets 2 options it hasn't used lately, and the
model may reword one, combine them or ignore both.

PRIVATE file (outside the public repo): /Users/Agent/cfb-pickem-private/banter_bank.json.
No member names, no numbers, so nothing in it can be a wrong league fact.

Refresh: league_material.py's hourly run calls refresh_if_due() (weekly,
Wednesdays after 10 AM CT, or when the bank is missing / over 8 days old).
CLI:  banter.py refresh [--force] | status | show <category> | match "<text>"
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import logging
import os
import random
import re
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import rare_bits  # noqa: E402  (the once-a-month milk bit never rides in the bank)

log = logging.getLogger("picky.banter")

CT = ZoneInfo("America/Chicago")
BANK = Path(os.environ.get("PICKY_BANTER_BANK", "/Users/Agent/cfb-pickem-private/banter_bank.json"))
MEMBERS_FILE = Path(os.environ.get("PICKY_GROUP_MEMBERS", "/Users/Agent/cfb-pickem-private/picky_group_members.json"))
MAX_AGE_S = 8 * 86400
REFRESH_WEEKDAY, REFRESH_HOUR = 2, 10  # Wednesday 10 AM CT
FAIL_BACKOFF_S = 6 * 3600
TIMEOUT_S = 900
MIN_LINES = 4
RECENT_N = 6  # don't reuse a line among a category's last N offered

# (key, kind, what it covers, regex). First match wins; heckles before off-topic.
CATEGORIES = [
    ("suck", "heckle", "'you suck', 'you're trash', 'worst bot ever'",
     r"\byou (suck|stink|blow)\b|\b(you'?re|ur|you are|picky is|picky'?s) (trash|garbage|terrible|awful|the worst|ass|"
     r"dog ?shit)\b|\bworst (bot|ai|robot)\b"),
    ("bark", "heckle", "'all bark no bite', 'all talk', 'talk is cheap'",
     r"\ball bark\b|\bno bite\b|\btalk is cheap\b|\ball talk\b|\bbig talk\b"
     # speed lanes 2026-11-17: "Im so much better than you" went to the full brain (30-60 s) for a one-line comeback
     r"|\b(i'?m|im|i am) (so much |way |a lot |just )?(better|smarter) than (you|u|picky)\b"),
    ("bad", "heckle", "'why are you so bad', 'why don't you work better', 'you're broken / useless / always wrong'",
     r"\bwhy (are|r) (you|u) so (bad|dumb|stupid|wrong|slow)\b|\bwork better\b|\b(you'?re|you are|ur) (broken|useless|"
     r"always wrong|wrong again|so slow)\b|\bdo better\b|\bget good\b|\bso bad at\b|\bwhy (do|does) (you|u|picky) suck\b"),
    ("bot", "heckle", "'are you a bot', 'are you real', 'who programmed you', 'you're just ChatGPT'",
     r"\bare (you|u) (a |an )?(bot|robot|ai|real|human|chat ?gpt|computer)\b|\bwho (made|built|programmed|coded) "
     r"(you|u)\b|\b(you'?re|ur) (just )?(a |an )?(bot|ai|robot|chat ?gpt)\b"),
    ("coward", "heckle", "'you're a pussy', 'coward', 'soft', 'scared'",
     r"\b(pussy|coward|chicken ?shit|soft as|scared|wuss|wimp)\b"),
    ("shutup", "heckle", "'shut up', 'nobody asked', 'go away'",
     r"\bshut up\b|\bstfu\b|\bnobody asked\b|\bno one asked\b|\bgo away\b|\bpipe down\b"),
    ("unfunny", "heckle", "'you're not funny', 'cringe', 'corny'",
     r"\bnot funny\b|\bunfunny\b|\bcringe\b|\bcorny\b"),
    ("cereal", "offtopic", "best cereal / favorite cereal",
     r"\bcereals?\b|\bfroot loops\b|\bcheerios\b|\bcap'?n crunch\b|\bfrosted flakes\b|\blucky charms\b"),
    ("beer", "offtopic", "best beer / what are you drinking",
     r"\bbeers?\b|\blager\b|\bipas?\b|\bbrewski\w*\b|\bcoors\b|\bbud light\b|\bmiller lite\b|\bbanquet\b"),
    ("liquor", "offtopic", "whiskey, bourbon, tequila, cocktails, shots",
     r"\b(whiskey|whisky|bourbon|tequila|vodka|cocktails?|drink of choice|old fashioned|margaritas?)\b"),
    ("food", "offtopic", "best pizza, wings, burgers, tacos, BBQ, tailgate food",
     r"\b(pizza|burgers?|wings|tacos?|bbq|barbecue|hot ?dogs?|steak|brats?|nachos|tailgate food|chili)\b"),
    ("nfl", "offtopic", "the NFL, the Super Bowl, fantasy football",
     r"\bnfl\b|\bsuper bowl\b|\bfantasy (football|team|league)\b|\bpackers\b|\bsteelers\b|\bvikings\b"),
    ("nba", "offtopic", "the NBA / basketball",
     r"\bnba\b|\bbasketball\b|\blebron\b|\bmarch madness\b"),
    ("baseball", "offtopic", "baseball / MLB",
     r"\bmlb\b|\bbaseball\b|\bworld series\b|\bwhite sox\b|\byankees\b|\bdodgers\b"),
    ("golf", "offtopic", "golf",
     r"\bgolf\w*\b|\bpga\b|\btee times?\b"),
    ("life", "offtopic", "'how are you', 'what's up', 'what are you doing this weekend'",
     r"\bhow are (you|u|ya)\b|\bwhat'?s up\b|\bwhat are (you|u) (doing|up to)\b|\bhow'?s (it going|your day|your "
     r"weekend)\b|\bweekend plans\b|\bwyd\b"),
]
_RX = [(k, kind, re.compile(rx, re.I)) for k, kind, _, rx in CATEGORIES]
KIND = {k: kind for k, kind, _, _ in CATEGORIES}


def match(text: str) -> str | None:
    t = (text or "").replace("’", "'")
    return next((k for k, _, rx in _RX if rx.search(t)), None)


# -----------------------------------------------------------------------------
# live: pick 2 fresh options
# -----------------------------------------------------------------------------

_cache: tuple[float, dict] = (0.0, {})
_recent: dict[str, collections.deque] = collections.defaultdict(lambda: collections.deque(maxlen=RECENT_N))


def load() -> dict:
    global _cache
    try:
        m = BANK.stat().st_mtime
    except OSError:
        return {}
    if m != _cache[0]:
        try:
            _cache = (m, json.loads(BANK.read_text()).get("categories") or {})
        except (OSError, ValueError):
            return _cache[1]
    return _cache[1]


def options(cat: str, n: int = 2, rng: random.Random | None = None) -> list[str]:
    lines = [x for x in load().get(cat) or [] if isinstance(x, str)]
    if not lines:
        return []
    rng = rng or random
    fresh = [x for x in lines if x not in _recent[cat]] or lines
    pick = rng.sample(fresh, min(n, len(fresh)))
    _recent[cat].extend(pick)
    return pick


def ammo_rider(text: str, intent: str = "") -> str:
    """The AMMO rider for a matching text ('' when nothing matches). A shot at
    Picky the regexes miss (the understand pass said 'insult') gets 'suck'."""
    cat = match(text) or ("suck" if intent == "insult" else None)
    opts = options(cat) if cat else []
    if not opts:
        return ""
    head = ("AMMO (fresh lines written ahead for this kind of shot; optional. Reword one in your own voice, "
            "build on it, or ignore both if yours is better. Never paste both, never sound scripted):"
            if KIND[cat] == "heckle" else
            "AMMO (fresh takes written ahead for this off-topic question; optional. It's fine to answer off-topic "
            "stuff: give a real, specific answer with conviction, in your own words. Never paste both):")
    return head + "\n" + "\n".join(f"- {o}" for o in opts)


# -----------------------------------------------------------------------------
# offline refresh (claude -p, no tools)
# -----------------------------------------------------------------------------

PROMPT = """You write comeback and banter lines for "Picky", the trash-talking bot in a group text of friends who run a college football pick'em league. Picky texts like a funny friend: quick, specific, confident, a little smug, medium heat, R-rated words only when they land. He's a known bot (he even has an exhibition row on the leaderboard), and he's proud of it.

Write {n} lines for EACH category below. Every line must stand alone as a reply to that kind of text.

Categories:
{cats}

Rules for every line:
- 6 to 28 words. Texting voice. Vary the structure: questions, deadpan, mock-sincere, absurd comparisons, callbacks to football. No two lines should open the same way.
- Heckle lines are comebacks aimed at whoever heckled Picky. Make them clever, not crude: wordplay and specific images beat swearing. "are you a bot" lines own it cheekily, then flip it on the asker.
- Off-topic lines ACTUALLY ANSWER: name a real, specific pick (an actual cereal, beer, pizza style, team) with conviction and a reason, and you may tie it back to football or pick'em. Spread the picks around: don't name the same item in every line.
- No numbers or digits at all (no records, scores, stats, years, percentages).
- No names of real people in the league and no made-up league events. Real brands, foods, pro teams and famous athletes are fine.
- Never joke about bodies, looks, family, race, religion, sexuality, health or death. No slurs. No threats.
- Never mention milk, cartons, dairy, Jeeps or the word "freak" (a league running bit Picky rations himself).
- No stock insults (dipshit, genius, chief, clown, buddy, pal, champ), no "as an AI", no emoji, no hashtags, no em dashes.

Return JSON: {{"categories": {{"<category key>": ["line", ...], ...}}}} with every category key."""


def schema() -> dict:
    keys = [k for k, _, _, _ in CATEGORIES]
    return {"type": "object", "properties": {"categories": {
        "type": "object", "properties": {k: {"type": "array", "items": {"type": "string"}} for k in keys},
        "required": keys}}, "required": ["categories"]}


def build_prompt(n: int = 12) -> str:
    cats = "\n".join(f"- {k} ({kind}): {desc}" for k, kind, desc, _ in CATEGORIES)
    return PROMPT.format(n=n, cats=cats)


def _member_words() -> list[str]:
    try:
        d = json.loads(MEMBERS_FILE.read_text())
    except (OSError, ValueError):
        return []
    out = set()
    for m in d.get("members") or []:
        for w in (m.get("team"), m.get("name")):
            if w and len(w) > 2:
                out.add(w.lower())
    return sorted(out)


_STOCK = re.compile(r"\b(dipshit|genius|chief|clown|buddy|pal|champ|as an ai|language model)\b", re.I)


def line_ok(text: str, names: list[str]) -> str | None:
    """The cleaned line, or None if it breaks a rule."""
    import chat  # lazy: the refresh job doesn't need the app otherwise
    t = re.sub(r"\s+", " ", (text or "").replace("—", ", ").replace("–", ", ")).strip().strip('"')
    w = len(t.split())
    if not 5 <= w <= 32 or re.search(r"\d", t) or "#" in t:
        return None
    low = t.lower()
    if any(re.search(rf"(?<!\w){re.escape(n)}(?!\w)", low) for n in names):
        return None
    if (chat._SLURS.search(t) or chat._FAMILY_DARK.search(t) or chat._ANATOMY.search(t) or _STOCK.search(t)
            or rare_bits.mentions("milk", t)
            or re.search(r"[\U0001F300-\U0001FAFF]", t)):
        return None
    return t


def validate(raw: dict, prev: dict | None = None) -> tuple[dict, list[str]]:
    names = _member_words()
    cats, dropped = {}, []
    got = (raw or {}).get("categories") or {}
    for k, _, _, _ in CATEGORIES:
        keep, seen = [], set()
        for x in got.get(k) or []:
            t = line_ok(x, names) if isinstance(x, str) else None
            if not t:
                dropped.append(f"{k}: {str(x)[:60]}")
                continue
            if t.lower() not in seen:
                seen.add(t.lower())
                keep.append(t)
        if len(keep) < MIN_LINES and (prev or {}).get(k):
            keep = prev[k]  # too few survived: keep last week's lines for this one
        if keep:
            cats[k] = keep
    return cats, dropped


def due(doc: dict | None, now: dt.datetime) -> str | None:
    if not doc or not doc.get("categories"):
        return "missing"
    try:
        made = dt.datetime.fromisoformat(doc["generated_at"])
    except (KeyError, ValueError):
        return "missing"
    if (now - made).total_seconds() > MAX_AGE_S:
        return "stale"
    if doc.get("trigger") == "burn_book":
        return None  # the burn book (Opus, Sun + Wed) owns the slots while its copy is fresh
    wed = (now - dt.timedelta(days=(now.weekday() - REFRESH_WEEKDAY) % 7)).replace(
        hour=REFRESH_HOUR, minute=0, second=0, microsecond=0)
    if wed > now:
        wed -= dt.timedelta(days=7)
    return "weekly" if made < wed <= now else None


def refresh(force: bool = False, research=None, out: Path | None = None, now: dt.datetime | None = None) -> dict:
    import dossier  # run_claude, read_json, write_atomic
    import picky_claude
    out = out or BANK
    now = now or dt.datetime.now(CT)
    prev = dossier.read_json(out)
    reason = "forced" if force else due(prev, now)
    if not reason:
        return {"ok": True, "built": False}
    fail = out.with_suffix(".failed")
    if not force and fail.exists() and time.time() - fail.stat().st_mtime < FAIL_BACKOFF_S:
        return {"ok": True, "built": False, "backoff": True}
    research = research or (lambda p: dossier.run_claude(p, TIMEOUT_S, schema(), "", caller="banter"))
    try:
        cats, dropped = validate(research(build_prompt()), (prev or {}).get("categories"))
    except Exception:
        fail.touch()
        raise
    old = (prev or {}).get("categories") or {}
    fresh = sum(1 for k, v in cats.items() if v != old.get(k))
    if fresh < len(CATEGORIES) // 2:
        fail.touch()
        raise RuntimeError(f"only {fresh} categories got valid new lines; kept the previous bank")
    doc = {"schema": 1, "generated_at": now.isoformat(timespec="seconds"), "trigger": reason,
           "by": f"claude -p {picky_claude.MODEL} (no tools)", "categories": cats, "dropped": dropped}
    dossier.write_atomic(out, doc)
    os.chmod(out, 0o600)
    fail.unlink(missing_ok=True)
    n = sum(len(v) for v in cats.values())
    log.info("banter bank %s: %d categories, %d lines, %d dropped", reason, len(cats), n, len(dropped))
    return {"ok": True, "built": True, "trigger": reason, "categories": len(cats), "lines": n, "dropped": len(dropped)}


def refresh_if_due() -> dict | None:
    """For league_material's hourly run: never raises. A Claude booster (picky_claude's capped gate): gate shut =
    the last bank stays, and the burn book's generic lines and its code pool carry the banter."""
    import picky_claude
    if not picky_claude.available():
        return {"ok": True, "built": False, "skipped": f"Claude booster off ({picky_claude.why_not()}); last bank stays"}
    try:
        return refresh()
    except Exception as e:  # noqa: BLE001 - the old bank stays; retried after the backoff
        log.warning("banter refresh failed: %s", e)
        return {"ok": False, "error": str(e)[:200]}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["refresh", "status", "show", "match"])
    ap.add_argument("arg", nargs="?", default="")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if a.cmd == "refresh":
        print(json.dumps(refresh(force=a.force)))
    elif a.cmd == "status":
        try:
            d = json.loads(BANK.read_text())
        except (OSError, ValueError):
            print("no bank")
            return 1
        print(json.dumps({"generated_at": d.get("generated_at"), "trigger": d.get("trigger"),
                          "categories": {k: len(v) for k, v in (d.get("categories") or {}).items()},
                          "dropped": len(d.get("dropped") or [])}, indent=1))
    elif a.cmd == "show":
        print("\n".join(load().get(a.arg) or []))
    else:
        c = match(a.arg)
        print(c, "|", ammo_rider(a.arg))
    return 0


if __name__ == "__main__":
    sys.exit(main())
