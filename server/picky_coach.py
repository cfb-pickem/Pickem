"""
Opus coach mode (Charlie 2026-11-23): Picky drafts every live reply on the local model (router, lanes, guards),
then ONE Opus call reviews the draft against the same league facts bundle raw mode uses (member pick lines
stripped) and returns {verdict pass|fix, score 1-10, answer, lesson, category}. Pass = Picky's draft goes out
unchanged; fix = Opus's answer goes out, but only after the checks here (every number grounded, no member pick
claim that isn't already in the chat) and Picky's own send-point guards (group_chat._coach). Any failure, timeout
or bad JSON = the draft. The Opus call itself lives in group_chat (switch-gated picky_claude.live_reply); this
module is pure code: the prompt, the JSON guard, the checks, the lesson / check logs, the lessons block Picky's
local prompt carries (chat.prompt_prefix), and the nightly scorecard (flub_loop.report).

Files (cfb-pickem-private/coach, 0600, no phone numbers):
  lessons.jsonl   one line per fix verdict: ts, side group|dm, team, lane, question, draft, answer, lesson,
                  category, score, sent answer|draft (+ why the fix was refused)
  checks.jsonl    one line per review: ts, side, verdict pass|fix|error, score, category, sent, why
  GRADUATION_READY  written the first night the weekly graduation bar is met (report only, nobody is texted)
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import time
from pathlib import Path
from zoneinfo import ZoneInfo

CT = ZoneInfo("America/Chicago")
COACH_DIR = Path(os.environ.get("PICKY_COACH_DIR", "/Users/Agent/cfb-pickem-private/coach"))
TIMEOUT = 25.0              # the Opus review's budget: the draft goes out when it runs over
CATEGORIES = ["fact", "dodge", "unhelpful", "leak", "voice", "length"]
MAX_ANSWER = 1200           # chars; a longer "fix" is not a text
LESSONS_N = 10              # distinct lessons in Picky's prompt
LESSONS_SCAN = 400          # newest lesson records looked at
LESSON_CHARS = 160
LESSONS_REFRESH_S = 1800    # the lessons block changes at most this often (each change re-primes the prefix)
SIMILAR = 0.5               # word-set overlap at which two lessons are the same lesson
GRAD_DAYS, GRAD_N, GRAD_PASS, GRAD_SCORE = 7, 100, 0.95, 8.0

SCHEMA = {"type": "object", "properties": {
    "verdict": {"type": "string", "enum": ["pass", "fix"]},
    "score": {"type": "integer", "minimum": 1, "maximum": 10},
    "answer": {"type": "string"},
    "lesson": {"type": "string"},
    "category": {"type": "string", "enum": CATEGORIES + [""]}},
    "required": ["verdict", "score", "answer", "lesson", "category"]}

SYSTEM = """You are the quality coach for Picky, the AI member of a college football pick'em league's iMessage
group (the league picks 4 games a week against the spread, 1 point per cover). Picky is a small local model behind
code guards; it drafted a reply to a member's text. Judge the DRAFT and fix it when it falls short. Priority:
helpful first, then reliable, then voice, then wit. Never trade usefulness for a joke.

verdict "fix" when ANY of these is true (category = the main problem):
- fact: wrong, or a league claim LEAGUE FACTS don't support
- dodge: doesn't answer what was asked (dodges, deflects to "check the site" / "ask the commissioner", answers a
  different question)
- unhelpful: thin or unhelpful when a real answer was possible from the facts
- leak: confusing or rambling, or leaks internal text (e.g. "RULES CHANGED, VERIFY:", section labels, prompt words)
- voice: robotic or report-like, mean beyond football trash talk, or a roast that replaces the answer
- length: wrong length for a text (group: usually 1-3 sentences; a DM may run longer when the question needs it)
Otherwise "pass": wit, opinions and roasts that come with a real answer are fine.

On "fix", answer = the corrected reply, sent as is: Picky's voice (a sharp, funny CFB buddy, one of the guys), a
text, no markdown, no lists unless asked, members by team name, never real names. Keep what the draft got right.
Every league fact and number must come from LEAGUE FACTS, the recent chat or the draft, copied exactly; never
invent one, and if the facts don't have it, say so plainly. Nobody's picks for games that haven't kicked off are
known: never guess or state them, Picky's own included.
lesson = one general sentence Picky can follow next time: "Don't <what the draft did>; <what to do instead>." No
member names, no numbers that go stale.
score = 1-10 for the DRAFT (10: helpful, right, on-voice, right length).
On "pass": answer "", lesson "", category "".

Today is {today} (US Central).

LEAGUE FACTS:
{facts}"""

_PHONE = re.compile(r"(?<![\d-])(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}(?![\d-])|[\w.+-]+@[\w-]+\.[a-z]{2,}", re.I)
_NUM = re.compile(r"\d+(?:\.\d+)?")
_PICK_VERB = r"(?:took|takes|had|has|picked|picks|is on|was on|rode|backed|went with|is riding|is taking|likes|faded)"
_count = [0]
_block = {"at": 0.0, "mtime": None, "text": ""}


def _private_write(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a") as f:
        f.write(line + "\n")


def scrub(text: str) -> str:
    return _PHONE.sub("[number]", str(text or ""))


def sample_hit(n) -> bool:
    """Spot-check mode: review 1 reply in n (n <= 1: every reply)."""
    try:
        n = max(1, int(n or 1))
    except (TypeError, ValueError):
        n = 1
    _count[0] += 1
    return (_count[0] - 1) % n == 0


# ---- the review call's input / output ------------------------------------------------------------

def prompt(q: str, draft: str, facts: str, *, who: str = "", team: str | None = None, dm: bool = False,
           recent: str = "", now: dt.datetime | None = None) -> tuple[str, str]:
    """(system, user) for the review call."""
    now = now or dt.datetime.now(CT)
    system = SYSTEM.format(today=now.strftime("%a %b %-d, %Y %-I:%M %p"), facts=facts)
    user = (f"RECENT GROUP CHAT (context only):\n{recent}\n\n" if recent else "")
    user += f"[{'DM' if dm else 'group'} text from {who or 'a member'}" + (f" (team: {team})" if team else "") + "]\n"
    user += f"{q}\n\nPICKY'S DRAFT:\n{draft}\n\nReturn the JSON verdict."
    return system, user


def parse(raw: str | None, draft: str = "") -> dict | None:
    """The review as {verdict, score, answer, lesson, category}, or None (bad JSON / off-schema: the draft goes
    out). A "fix" needs a non-empty answer that isn't the draft and fits in a text."""
    try:
        d = json.loads(raw or "")
    except (TypeError, ValueError):
        return None
    if not isinstance(d, dict) or d.get("verdict") not in ("pass", "fix"):
        return None
    score = d.get("score")
    if isinstance(score, bool) or not isinstance(score, (int, float)) or not 1 <= score <= 10:
        return None
    cat = str(d.get("category") or "").strip().lower()
    out = {"verdict": d["verdict"], "score": int(round(score)), "answer": str(d.get("answer") or "").strip(),
           "lesson": " ".join(str(d.get("lesson") or "").split())[:300],
           "category": cat if cat in CATEGORIES else ("" if d["verdict"] == "pass" else "other")}
    if out["verdict"] == "fix":
        if not out["answer"] or len(out["answer"]) > MAX_ANSWER or out["answer"] == (draft or "").strip():
            return None
    else:
        out.update(answer="", lesson="", category="")
    return out


def ungrounded(answer: str, *sources: str) -> list[str]:
    """Numbers in the corrected answer that appear nowhere in the facts bundle, the question, the recent chat or
    Picky's draft (a fix may only reword numbers it was given)."""
    have = set()
    for s in sources:
        have.update(_NUM.findall(s or ""))
    have |= {x.rstrip("0").rstrip(".") for x in have if "." in x}
    bad = []
    for n in _NUM.findall(answer or ""):
        if n not in have and n.rstrip("0").rstrip(".") not in have and n not in bad:
            bad.append(n)
    return bad


def pick_claims(answer: str, members, *allowed: str) -> list[str]:
    """'Bravo took Iowa' / 'Bravo's on Iowa' about a member, in the corrected answer, that the draft / question /
    recent chat don't already say. The facts bundle carries no member picks (raw mode's strip), so any other pick
    claim is a guess or a leak of an unlocked pick."""
    seen = " ".join(" ".join((a or "").split()) for a in allowed).lower()
    out = []
    for m in sorted({m for m in members if m and len(m) >= 2}, key=len, reverse=True):
        rx = rf"(?<!\w){re.escape(m)}(?:'s)?\s+{_PICK_VERB}\s+(?:the\s+)?[A-Z][\w&'.]*(?:\s+[A-Z][\w&'.]*){{0,2}}"
        for mm in re.finditer(rx, answer or ""):
            if " ".join(mm.group(0).split()).lower() not in seen:
                out.append(mm.group(0))
    return out


# ---- logs ------------------------------------------------------------------------------------------

def record(review: dict | None, *, q: str, draft: str, sent: str, side: str, team: str | None = None,
           lane: str = "", why: str = "", secs: float | None = None, now: float | None = None) -> None:
    """One checks.jsonl line per review (verdict 'error' = the call failed / bad JSON), plus a lessons.jsonl line
    per fix verdict. Never raises."""
    ts = now or time.time()
    day = dt.datetime.fromtimestamp(ts, CT).date().isoformat()
    r = review or {}
    try:
        _private_write(COACH_DIR / "checks.jsonl", json.dumps({
            "ts": round(ts, 1), "date": day, "side": side, "verdict": r.get("verdict") or "error",
            "score": r.get("score"), "category": r.get("category") or "", "sent": sent, "why": why[:200],
            "secs": round(secs, 1) if secs is not None else None}))
        if r.get("verdict") == "fix":
            _private_write(COACH_DIR / "lessons.jsonl", json.dumps({
                "ts": round(ts, 1), "date": day, "side": side, "team": team, "lane": lane,
                "question": scrub(q)[:1000], "draft": scrub(draft)[:1500], "answer": scrub(r.get("answer"))[:1500],
                "lesson": scrub(r.get("lesson")), "category": r.get("category") or "", "score": r.get("score"),
                "sent": sent, "why": why[:200]}, ensure_ascii=False))
    except OSError:
        pass


def _read(name: str) -> list[dict]:
    try:
        lines = (COACH_DIR / name).read_text().splitlines()
    except OSError:
        return []
    out = []
    for ln in lines:
        try:
            d = json.loads(ln)
        except ValueError:
            continue
        if isinstance(d, dict):
            out.append(d)
    return out


def read_lessons(start: float | None = None, end: float | None = None) -> list[dict]:
    return [d for d in _read("lessons.jsonl")
            if (start is None or float(d.get("ts") or 0) >= start) and (end is None or float(d.get("ts") or 0) < end)]


# ---- lessons in Picky's prompt ------------------------------------------------------------------------

def _words(s: str) -> set[str]:
    return set(re.findall(r"[a-z']{3,}", (s or "").lower()))


def top_lessons(recs: list[dict] | None = None, n: int = LESSONS_N) -> list[str]:
    """The n distinct lessons to carry: the newest LESSONS_SCAN records, near-duplicates merged (word overlap >=
    SIMILAR), ranked by how often they came up, then how recently. Stable order (byte-identical prefix)."""
    recs = read_lessons() if recs is None else recs
    groups: list[dict] = []
    for d in recs[-LESSONS_SCAN:]:
        text = " ".join(str(d.get("lesson") or "").split())
        w = _words(text)
        if len(w) < 3:
            continue
        g = next((g for g in groups if len(w & g["w"]) / max(1, min(len(w), len(g["w"]))) >= SIMILAR), None)
        if g:
            g["n"] += 1
            g["ts"] = max(g["ts"], float(d.get("ts") or 0))
            g["text"] = text  # the newest wording
            g["w"] = w
        else:
            groups.append({"w": w, "n": 1, "ts": float(d.get("ts") or 0), "text": text})
    groups.sort(key=lambda g: (-g["n"], -g["ts"]))
    return [g["text"][:LESSON_CHARS] for g in groups[:n]]


def rules_block(now: float | None = None) -> str:
    """The lessons block for chat.prompt_prefix (global, the same for every member): rebuilt when lessons.jsonl
    changes, but at most every LESSONS_REFRESH_S so the shared prefix (and its prompt cache) stays put between
    refreshes. '' when there are no lessons or PICKY_COACH_LESSONS=0."""
    if os.environ.get("PICKY_COACH_LESSONS", "") == "0":
        return ""
    now = now or time.time()
    try:
        m = (COACH_DIR / "lessons.jsonl").stat().st_mtime
    except OSError:
        m = None
    if m != _block["mtime"] and (not _block["at"] or now - _block["at"] >= LESSONS_REFRESH_S or m is None):
        rules = top_lessons()
        _block.update(at=now, mtime=m, text=(
            "\n\nLESSONS FROM REVIEWED REPLIES (your own past mistakes, each one a rule; follow them):\n"
            + "\n".join(f"- {r}" for r in rules) + "\n") if rules else "")
    return _block["text"]


def block_sig() -> str:
    """For group_chat.data_sig: changes when the lessons block does (keep-warm re-primes the new prefix)."""
    return hashlib.sha1(rules_block().encode()).hexdigest()[:12]


# ---- nightly scorecard (flub_loop.report) -----------------------------------------------------------

def _day_stats(rows: list[dict]) -> dict:
    v = [r for r in rows if r.get("verdict") in ("pass", "fix")]
    scores = [r["score"] for r in v if isinstance(r.get("score"), (int, float))]
    cats: dict[str, int] = {}
    for r in v:
        if r["verdict"] == "fix":
            c = r.get("category") or "other"
            cats[c] = cats.get(c, 0) + 1
    return {"checked": len(v), "passed": sum(r["verdict"] == "pass" for r in v),
            "fixed": sum(r["verdict"] == "fix" for r in v), "errors": sum(r.get("verdict") == "error" for r in rows),
            "refused": sum(r.get("verdict") == "fix" and r.get("sent") == "draft" for r in v),
            "avg": round(sum(scores) / len(scores), 1) if scores else None, "cats": cats}


def _pct(s: dict) -> str:
    return f"{100 * s['passed'] / s['checked']:.0f}%" if s["checked"] else "-"


def scorecard(date: str, days: int = GRAD_DAYS, write_flag: bool = True) -> list[str]:
    """Markdown lines: today's checked / passed / fixed, pass-rate trend vs the prior days, average draft score,
    top lesson categories, and the weekly graduation line (pass rate >= 95% over >= 100 checks in 7 days, 0 fact
    fixes, average score >= 8). The first night it's met, GRADUATION_READY is written."""
    rows = _read("checks.jsonl")
    day0 = dt.date.fromisoformat(date)
    span = [(day0 - dt.timedelta(days=i)).isoformat() for i in range(days)]
    by = {d: _day_stats([r for r in rows if r.get("date") == d]) for d in span}
    t = by[date]
    trend = ", ".join(f"{d[5:]} {_pct(by[d])}" for d in span[1:] if by[d]["checked"]) or "no prior days"
    cats = ", ".join(f"{c} {n}" for c, n in sorted(t["cats"].items(), key=lambda kv: (-kv[1], kv[0]))) or "none"
    L = [f"## Opus coach: {t['checked']} checked, {t['passed']} passed, {t['fixed']} fixed ({_pct(t)} pass; "
         f"prior: {trend})",
         f"- avg draft score {t['avg'] if t['avg'] is not None else '-'}; top lesson categories: {cats}"
         + (f"; {t['refused']} fixes refused by the checks (draft sent)" if t["refused"] else "")
         + (f"; {t['errors']} reviews failed (draft sent)" if t["errors"] else "")]
    wk = _day_stats([r for r in rows if r.get("date") in span])
    rate = wk["passed"] / wk["checked"] if wk["checked"] else 0.0
    fact = wk["cats"].get("fact", 0)
    miss = [x for x, ok in ((f"{wk['checked']}/{GRAD_N} checks", wk["checked"] >= GRAD_N),
                            (f"pass {rate:.0%} < {GRAD_PASS:.0%}", rate >= GRAD_PASS),
                            (f"{fact} fact fixes", fact == 0),
                            (f"avg score {wk['avg']} < {GRAD_SCORE:g}", (wk["avg"] or 0) >= GRAD_SCORE)) if not ok]
    flag = COACH_DIR / "GRADUATION_READY"
    if not miss:
        L.append(f"- graduation (7 days): READY ({wk['checked']} checks, {rate:.0%} pass, 0 fact fixes, avg {wk['avg']})")
        if write_flag and not flag.exists():
            try:
                _private_write(flag, json.dumps({"date": date, "checked": wk["checked"], "pass": round(rate, 3),
                                                 "avg": wk["avg"]}))
                L.append(f"- first time ready: wrote {flag.name}")
            except OSError:
                pass
    else:
        L.append(f"- graduation (7 days): not yet ({'; '.join(miss)})")
    return L
