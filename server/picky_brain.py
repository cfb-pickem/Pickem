"""
Text Picky's slow, smart path (iMessage only; Charlie OK'd ~2 minutes a reply,
2026-11-15). group_chat.answer drives it; chat.py's pipeline does the drafting.

  (a) UNDERSTAND  one short JSON pass: what's actually being asked, the intent
                  (question / banter / insult / request / reaction), who it's
                  about, the room's tone, the facts needed, the core answer.
  (b) DRAFT       up to 3 candidates through chat.py's full pipeline (riders,
                  sentence filters, results guard), each carrying the brief,
                  with its own temperature and angle.
  (c) JUDGE       one JSON pass picks the candidate that answers the question,
                  is correct, and sounds most like a funny friend.
  (d) FACT-CHECK  chat.py's filters and results guard run on every candidate;
                  a candidate the filter cut is only eligible when none came
                  through whole, and one more draft runs if all were cut and
                  there's time.

Every pass starts its system prompt with chat.prompt_prefix (the league data),
so the prefix checkpoint (qwen3.5) or KV prefix cache (other models) serves all
of them: each call processes only its short tail.

Nothing here is hosted: local Ollama only.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field

import httpx

import banter
import burn_book
import callbacks
import chat
import league_rules
import live_flag
import member_memory
import router

log = logging.getLogger("picky-brain")

# Charlie 2026-11-15: drop the best-of-3 judge (57% agreement, leaned to draft 1). One draft, plus a
# reroll only when the filters cut it. PICKY_JUDGE=1 brings back 3 drafts + judge.
USE_JUDGE = os.environ.get("PICKY_JUDGE") == "1"
N_DRAFTS = 3 if USE_JUDGE else 1
DRAFT_BUDGET = 105.0   # seconds: no new draft starts after this
# Speed lane 2026-11-17 (p95 < 60 s): the reroll after a cut draft runs only when its projected finish
# (elapsed + the last draft's own time + margin) fits SOFT_BUDGET, or when the draft is unusable (empty,
# a bare dodge, < UNUSABLE_WORDS words) and the finish still fits DRAFT_BUDGET. Draft 1 wins 3 rerolls in 5.
SOFT_BUDGET = float(os.environ.get("PICKY_SOFT_BUDGET", "50"))
# the unusable-draft reroll's cap. Replay 2026-11-17: those rerolls were the >60 s replies left (62 s, 81 s);
# PICKY_UNUSABLE_BUDGET=60 trades them for the dodge going out (undodge's card call when a pool game is named)
UNUSABLE_BUDGET = float(os.environ.get("PICKY_UNUSABLE_BUDGET", str(DRAFT_BUDGET)))
REROLL_MARGIN = 3.0
UNUSABLE_WORDS = 4
# a cut sentence that carried the answer (fact_cut) earns the reroll up to here (genius exam 2026-11-17)
FACT_BUDGET = float(os.environ.get("PICKY_FACT_BUDGET", "90"))
# fact wrap / banter line: wait this long for the prefix prime; a cold one (~60 s prefill) means the code
# answer / book line goes out alone while the prime finishes in the background for the next text
PRIME_WAIT = float(os.environ.get("PICKY_PRIME_WAIT", "10"))
JUDGE_BUDGET = 170.0   # seconds: no judge pass after this (the watcher waits 240)
# Burst load (Saturday drill 2026-11-17): with one Ollama slot, every full-lane text's understand pass
# queued behind the others' and hit its 60 s timeout anyway (8 of 8 in a 20-text burst), so each of those
# replies paid a wasted minute and went out without a brief. With BUSY_SKIP_BRIEF or more OTHER full-lane
# texts in flight the brief is skipped up front (the same no-brief path a timeout takes). 0 = never skip.
BUSY_SKIP_BRIEF = int(os.environ.get("PICKY_BUSY_SKIP_BRIEF", "2"))
_FULL_INFLIGHT = [0]  # full-lane run() calls in flight in this process

# draft i: sampling and angle. Draft 0 is the old single answer (temp 0.2); 1-2 run hot with min_p
# (Qwen3.5's card suggests ~0.7+ for chat; the filters and the judge guard the facts).
SAMPLING = [{"temperature": 0.2}, {"temperature": 0.8, "min_p": 0.05, "top_p": 1.0},
            {"temperature": 0.9, "min_p": 0.05, "top_p": 1.0}, {"temperature": 0.4}]
# exam A/Bs: PICKY_DRAFT_OPTIONS='{"temperature": 0.7, "top_p": 0.8, "top_k": 20, "presence_penalty": 1.5}'
# replaces draft 1's sampling (the one draft that runs with the judge off)
if os.environ.get("PICKY_DRAFT_OPTIONS"):
    SAMPLING[0] = json.loads(os.environ["PICKY_DRAFT_OPTIONS"])
TRACE = os.environ.get("PICKY_BRAIN_TRACE")  # optional JSONL of every pipeline run (exam judge agreement)
ANGLES = [
    "",
    "ANGLE FOR THIS DRAFT: one plain sentence that answers, then one specific, clever line. Short.",
    "ANGLE FOR THIS DRAFT: fold the answer into one punchy line, like a friend texting back fast.",
    "ANGLE FOR THIS DRAFT: answer straight, keep it tight and natural.",
]

INTENTS = ["question", "banter", "insult", "request", "reaction"]
TONES = ["friendly", "joking", "hostile", "annoyed", "curious", "hyped"]

UNDERSTAND_SCHEMA = {
    "type": "object",
    "properties": {
        "asking": {"type": "string"},
        "intent": {"type": "string", "enum": INTENTS},
        "about": {"type": "string"},
        "tone": {"type": "string", "enum": TONES},
        "facts_needed": {"type": "array", "items": {"type": "string"}},
        "answer": {"type": "string"},
    },
    "required": ["asking", "intent", "about", "tone", "facts_needed", "answer"],
}

UNDERSTAND_TAIL = """

TASK RIGHT NOW: don't write the reply yet. Read the NEWEST TEXT below and write your private notes as JSON:
- asking: what they actually want, in plain words, one sentence. Read it literally. "What did you think of this weekend's games?" wants your take on the games, not their own record. A shot at you ("you suck", "why don't you work better", "you're a pussy") is about YOU, Picky, not them.
- intent: question (wants info or your opinion), banter (joking around), insult (a shot at you), request (asks you to do something), reaction (reacting to the chat).
- about: who or what it's about: "Picky", "the sender", a member's team name, a team, a game, or a topic (cereal, beer, the NFL...).
- tone: the mood, from the recent chat and this text.
- facts_needed: the LEAGUE DATA items a good reply needs, e.g. "this week's pool games and finals", "the sender's season record". [] for banter, insults and off-topic questions.
- answer: the bare core of the reply as a plain fact or choice, under 15 words, NO jokes, no wording to copy. Examples: "Cinnamon Toast Crunch is the best cereal." / "Ackbar is 3-1 this week, 26-14 on the season." / "Wake Forest covered, Boise State didn't." / "Own being a small local model, then jab back." Off-topic questions (food, beer, cereal, other sports) get a real, specific pick, never "I don't eat". Copy numbers exactly from LEAGUE DATA or leave them out."""

JUDGE_SCHEMA = {
    "type": "object",
    "properties": {"best": {"type": "integer", "minimum": 1, "maximum": 4}, "why": {"type": "string"}},
    "required": ["best", "why"],
}

JUDGE_TAIL = """

TASK RIGHT NOW: don't write a reply. Pick which DRAFT Picky sends back to the NEWEST TEXT below. In this order:
1. It answers what they actually asked (THEIR ASK). Dodging, answering a different question, or burying the answer under jokes loses.
2. It's correct: every record, score, pick and standing matches LEAGUE DATA. An invented league fact loses.
3. It sounds like a real, funny friend in a group chat: specific and natural, no report-speak, no stock insults, nothing robotic.
Give the number of the best draft, then why in under 15 words."""


@dataclass
class Brief:
    asking: str = ""
    intent: str = "question"
    about: str = ""
    tone: str = "friendly"
    facts_needed: list[str] = field(default_factory=list)
    answer: str = ""

    def ok(self) -> bool:
        return bool(self.asking)


def parse_brief(raw: str) -> Brief:
    """A Brief from the model's JSON; empty (ok() False) when it's unusable."""
    try:
        d = json.loads(raw[raw.find("{"): raw.rfind("}") + 1]) if raw and "{" in raw else {}
    except ValueError:
        d = {}
    if not isinstance(d, dict):
        return Brief(asking="")

    def s(k, n=240):
        v = d.get(k)
        return re.sub(r"\s+", " ", v).strip()[:n] if isinstance(v, str) else ""

    facts = [re.sub(r"\s+", " ", f).strip()[:120] for f in d.get("facts_needed") or [] if isinstance(f, str)][:5]
    return Brief(asking=s("asking"), intent=s("intent") if s("intent") in INTENTS else "question",
                 about=s("about", 80), tone=s("tone") if s("tone") in TONES else "friendly",
                 facts_needed=[f for f in facts if f], answer=" ".join(s("answer", 320).split()[:25]))


_INTENT_WORDS = {"question": "a real question", "banter": "banter", "insult": "a shot at you",
                 "request": "a request", "reaction": "a reaction to the chat"}


def brief_rider(b: Brief, core: bool = True, stance: bool = False) -> str:
    if not b.ok():
        return ""
    L = ["YOUR READ OF THIS TEXT (your own private notes: use them, never quote or mention them):",
         f"- They want: {b.asking}",
         f"- It's {_INTENT_WORDS.get(b.intent, b.intent)}" + (f", about {b.about}" if b.about else "")
         + f"; mood: {b.tone}."]
    if b.facts_needed:
        L.append("- Facts it needs (take them only from LEAGUE DATA and the blocks above): " + "; ".join(b.facts_needed))
    # a core sentence the output filter would cut poisons every draft that echoes it (2026-11-16 replay: gemma's
    # core 'You're 2-7 in Oklahoma games, which is a special kind of tragic.' tripped the dark-words filter 5/5)
    ans = " ".join(x for x in re.split(r"(?<=[.!?])\s+", b.answer) if chat.sentence_ok(x, set())) if core else ""
    if ans:
        L.append(f"- Core of the reply (say it your own way, don't copy this wording): {ans}")
    if b.intent == "insult" and not stance:  # with a STANCE, it decides fire back vs own it
        L.append("Fire back at them first: a quick, specific comeback, then answer anything real they asked.")
    else:
        L.append("Lead with that answer in plain words, like a buddy in the group text, then one line of personality.")
    return "\n".join(L)


def insert_rider(messages: list[dict], rider: str, question: str) -> list[dict]:
    """A copy of messages with `rider` placed right before the question in the
    latest user turn (the 9B obeys what's nearest), else at its top."""
    if not rider or not messages or messages[-1].get("role") != "user":
        return messages
    body = messages[-1]["content"]
    i = body.rfind(question) if question else -1
    body = (body[:i] + rider + "\n\n" + body[i:]) if i >= 0 else (rider + "\n\n" + body)
    return messages[:-1] + [{**messages[-1], "content": body}]


_CARD_HEADS = ("GAME CARD (", "THIS WEEK'S NOTABLE GAMES", "HEAD COACHES (", "PICK STRATEGY (", "STANDINGS (conference", "CFP COMMITTEE RANKINGS")


def slate_block(messages: list[dict]) -> str:
    """The slate card chat.py put in the latest user turn ('' if none), for the understand pass."""
    c = str((messages[-1] if messages else {}).get("content") or "")
    i = min([c.find(h) for h in _CARD_HEADS if h in c] or [-1])
    if i < 0:
        return ""
    j = c.find("\n\n", i)
    return c[i:j if j > 0 else None].strip()


def brief_notes(question: str) -> str:
    """The insider notes and picked hot takes for the understand pass (the
    drafts get them as riders); '' when neither rides."""
    ins = chat.insider_note(question, limit=3).split("\nLead with")[0]
    nw = chat.news_note(question).split("\nLead with the newest")[0]  # breaking news lines (news.py)
    tk = chat.takes_note(question)
    tk = tk[tk.find("HOT TAKES"):] if "HOT TAKES" in tk else ""
    return "\n\n".join(x for x in (nw, ins, tk) if x)


def understand_user(question: str, sender: str, recent: str, last_reply: str, stance: str = "",
                    notes: str = "", live: str = "", rules: str = "", card: str = "") -> str:
    L = [f"Sent by: {sender}" if sender else "Sent by: a league member"]
    if card:  # slate_cards.py: computed data on a non-pool game (genius exam 2026-11-16: without it the brief said "no data")
        L.append(card + "\nThis card IS your data on this game. Your 'answer' "
                 "note must answer from it: the line and its move, ATS records, matchup, rest, news. Anything the "
                 "card doesn't have (an injury not listed): say it's not known.")
    if live:  # game-day live feed (live_games.py): scores/plays/QB news happening now, any game, pool or not
        L.append(live + "\nYour 'answer' note must use these live facts (exact score, scorer, clock) for a live-game ask.")
    if rules:  # league_rules.answer(): the rule, computed from the site's code (or a "may have changed" hedge)
        L.append("LEAGUE RULE for this question (from the site's own code; your 'answer' note must say this, "
                 f"never 'the rules don't cover it'): {rules}")
    if recent:
        L.append(recent.strip())
    if last_reply:
        L.append(f"Your last reply to them: {last_reply[:300]}")
    if stance:
        L.append(stance + "\nYour 'answer' note must follow this STANCE.")
    if notes:  # insider notes / hot takes: without them the core answer said 'no data' or made up a take
        L.append(notes + "\nYour 'answer' note builds on these lines: a fact only from the NEWS QUESTION or INSIDER "
                 "NOTES lines (newest news first, with its outlet; a [status: X] line IS a published injury report, so "
                 "never say no injury report exists), a take reworded from a HOT TAKE and said as an opinion, never "
                 "as news or a report.")
    L.append(f"NEWEST TEXT: {question}")
    return "\n\n".join(L)


def judge_user(question: str, sender: str, brief: Brief, drafts: list[str]) -> str:
    L = [f"NEWEST TEXT from {sender or 'a league member'}: {question}"]
    if brief.ok():
        L.append(f"THEIR ASK: {brief.asking}")
        if brief.answer:
            L.append(f"EXPECTED GIST (your first read; may be imperfect, LEAGUE DATA wins): {brief.answer}")
    L += [f"DRAFT {i + 1}: {d}" for i, d in enumerate(drafts)]
    return "\n\n".join(L)


class PrimeSlow(ValueError):
    """The prefix prime didn't finish within the caller's prime_wait (a cold prefill)."""


async def _json_call(prefix: str, tail: str, user: str, schema: dict, temperature: float,
                     num_predict: int, timeout: float, prime_wait: float | None = None) -> str:
    """One non-streamed Ollama call that shares the prefix checkpoint. num_ctx
    must match chat.OLLAMA_OPTIONS or Ollama reloads the model. prime_wait: a
    caller with a code fallback gives up (PrimeSlow) when the prime takes longer;
    the prime keeps running in the background."""
    payload = {"model": chat.MODEL, "stream": False, "format": schema,
               "messages": [{"role": "system", "content": prefix + tail}, {"role": "user", "content": user}],
               "options": {**chat.OLLAMA_OPTIONS, "temperature": temperature, "num_predict": num_predict},
               "keep_alive": chat.OLLAMA_KEEP_ALIVE}
    if chat.OLLAMA_THINK is not None:
        payload["think"] = chat.OLLAMA_THINK == "true"
    await live_flag.wait_clear()  # side sidecars yield to a live text
    if prime_wait is not None:
        t = chat.start_prime(prefix)
        done, _ = await asyncio.wait({t}, timeout=prime_wait)
        if not done:
            log.info("prime still running after %.0fs (cold prefix): code fallback, prime continues", prime_wait)
            raise PrimeSlow("cold prefix")
    async with httpx.AsyncClient(timeout=httpx.Timeout(connect=5.0, read=timeout, write=10.0, pool=5.0)) as c:
        if prime_wait is None:
            await chat._prime(c, prefix)
        r = await c.post(f"{chat.OLLAMA}/api/chat", json=payload)
        r.raise_for_status()
        d = r.json()
    log.info("brain call: prompt eval %.1fs (%s tokens), %s out, total %.1fs",
             (d.get("prompt_eval_duration") or 0) / 1e9, d.get("prompt_eval_count"), d.get("eval_count"),
             (d.get("total_duration") or 0) / 1e9)
    return (d.get("message") or {}).get("content") or ""


async def understand(prefix: str, question: str, sender: str, recent: str, last_reply: str = "",
                     timeout: float = 60.0, stance: str = "", notes: str = "", live: str = "",
                     rules: str = "", card: str = "") -> Brief:
    try:
        raw = await _json_call(prefix, UNDERSTAND_TAIL,
                               understand_user(question, sender, recent, last_reply, stance, notes, live, rules, card),
                               UNDERSTAND_SCHEMA, 0.1, 260, timeout)
    except (httpx.HTTPError, ValueError) as e:
        log.warning("understand failed: %s", e)
        return Brief(asking="")
    b = parse_brief(raw)
    log.info("brief: %s", json.dumps(b.__dict__)[:600])
    return b


async def judge(prefix: str, question: str, sender: str, brief: Brief, drafts: list[str],
                timeout: float = 45.0) -> int | None:
    """0-based index of the best draft, or None if the judge failed."""
    try:
        raw = await _json_call(prefix, JUDGE_TAIL, judge_user(question, sender, brief, drafts),
                               JUDGE_SCHEMA, 0.0, 120, timeout)
        m = re.search(r'"best"\s*:\s*(\d+)', raw)  # survives a "why" cut off by num_predict
        d = {"best": m.group(1) if m else None, "why": (re.search(r'"why"\s*:\s*"([^"]*)', raw) or [None, ""])[1]}
        best = int(d["best"]) - 1
    except (httpx.HTTPError, ValueError, TypeError) as e:
        log.warning("judge failed: %s", e)
        return None
    log.info("judge: draft %d of %d (%s)", best + 1, len(drafts), str(d.get("why"))[:160])
    return best if 0 <= best < len(drafts) else None


# -----------------------------------------------------------------------------
# candidate screening (code, before the judge)
# -----------------------------------------------------------------------------

def opener(text: str) -> str:
    return " ".join(re.findall(r"[a-z']+", (text or "").lower().replace("’", "'"))[:3])


_WL = re.compile(r"\b(\d{1,2})\s*-\s*(\d{1,2})\b")
_PERIOD = re.compile(r"\b(week \d+|last week|this week|this weekend|last weekend|last season|last year|last \d+|"
                     r"season|overall|on the year|all year|career|ats|against the spread|head.to.head|h2h)\b", re.I)


def record_sets(ctx: dict | None) -> dict[str, set[tuple[int, int]]]:
    """team -> the W-Ls an unqualified 'your X-Y' may mean: season, this week, last week."""
    if not ctx:
        return {}
    cw = ctx.get("current_week")
    out: dict[str, set] = {}
    for r in ctx.get("standings_ytd") or []:
        out.setdefault(r["team"], set()).add((r["w"], r["l"]))
    for team, wk in (ctx.get("weekly_records") or {}).items():
        for w in (cw, (cw or 0) - 1):
            v = wk.get(w) if isinstance(wk, dict) else None
            v = v if v is not None else (wk.get(str(w)) if isinstance(wk, dict) else None)
            if v:
                out.setdefault(team, set()).add((int(v[0]), int(v[1])))
    return out


def stale_records(text: str, ctx: dict | None, sender_team: str | None) -> list[str]:
    """Sentences giving a member (the sender as 'you', or by name) a W-L with no
    period that's none of their season / this-week / last-week records: a true
    record from some other week, passed off as current ('your 1-3 record' for a
    3-1 week; 1-3 was his week 8)."""
    sets = record_sets(ctx)
    if not sets:
        return []
    bad = []
    for sent in re.split(r"(?<=[.!?])\s+", text or ""):
        recs = [(int(a), int(b)) for a, b in _WL.findall(sent)]
        if not recs or _PERIOD.search(sent):
            continue
        named = [t for t in sets if re.search(rf"(?<!\w){re.escape(t)}(?!\w)", sent, re.I)]
        subj = named[0] if len(named) == 1 else (
            sender_team if not named and sender_team in sets and re.search(r"\byou(r|'re| are)?\b", sent, re.I) else None)
        if subj and any(r not in sets[subj] and not chat.oddity_ok(subj, f"{r[0]}-{r[1]}", sent) for r in recs):
            bad.append(sent)  # a stats-page split ('1-5 picking against Oklahoma') is true, not stale
    return bad


@dataclass
class Draft:
    text: str
    drops: int
    idx: int


def eligible(drafts: list[Draft], recent_picky: list[str]) -> list[Draft]:
    """Non-empty drafts the filter cut least; among those, drop ones that open
    like one of Picky's recent messages (no repeated openers) unless that
    leaves nothing; then drop exact duplicates."""
    pool = [d for d in drafts if d.text.strip()]
    if not pool:
        return []
    low = min(d.drops for d in pool)
    pool = [d for d in pool if d.drops == low]
    seen = {opener(t) for t in recent_picky if opener(t)}
    fresh = [d for d in pool if opener(d.text) not in seen]
    pool = fresh or pool
    out, texts = [], set()
    for d in pool:
        k = re.sub(r"\W+", " ", d.text.lower()).strip()
        if k not in texts:
            texts.add(k)
            out.append(d)
    return out


# -----------------------------------------------------------------------------
# stance (code, before the brief): who's actually doing better in this exchange
# -----------------------------------------------------------------------------
# Live 2026-11-15: Chuck P (4-0 this week, 1st) texted "you suck at picks?" and
# Picky (0-3) answered "Look in the mirror, Chuck P. You're 4-0 this week, but...".
# The 9B can't weigh records on its own, so code picks the stance and hands it
# the exact facts: own_it (Picky's worse), brag (Picky's better), deflect (the
# other side is crushing it: concede, pivot to something true).

@dataclass
class Rec:
    name: str
    week: tuple | None = None     # (label, w, l): this week's decided games, else last week's
    season: tuple | None = None   # (w, l)
    place: int | None = None
    n_teams: int = 0


def _pct(wl) -> float | None:
    return wl[-2] / (wl[-2] + wl[-1]) if wl and wl[-2] + wl[-1] else None


def member_rec(ctx: dict, team: str) -> Rec:
    st = ctx.get("standings_ytd") or []
    row = next((r for r in st if r["team"] == team), None)
    wk = (ctx.get("weekly_records") or {}).get(team) or {}
    cw = ctx.get("current_week")
    week = None
    for w, label in ((cw, "this week"), ((cw or 0) - 1, "last week")):
        v = wk.get(w) if wk.get(w) is not None else wk.get(str(w))
        if v and sum(v):
            week = (label, int(v[0]), int(v[1]))
            break
    return Rec(team, week, (row["w"], row["l"]) if row else None, row["rank"] if row else None, len(st))


def picky_rec(ctx: dict) -> Rec | None:
    pp = ctx.get("picky_player")
    if not pp:
        return None
    w, l = chat.results_record(chat.picky_pick_results(ctx))
    lw = pp.get("last_week") or [0, 0]
    week = ("this week", w, l) if w + l else (("last week", lw[0], lw[1]) if sum(lw) else None)
    s = pp.get("season") or [0, 0]
    return Rec("Picky", week, tuple(s) if sum(s) else None)


def better(a: Rec, b: Rec) -> int:
    """+1 a is doing better, -1 b is, 0 even/unknown: one vote each for the
    week, the season and the place (when both have one)."""
    votes = 0
    for x, y in ((_pct(a.week), _pct(b.week)), (_pct(a.season), _pct(b.season))):
        if x is not None and y is not None and abs(x - y) > 1e-9:
            votes += 1 if x > y else -1
    if a.place and b.place and a.place != b.place:
        votes += 1 if a.place < b.place else -1
    return (votes > 0) - (votes < 0)


def crushing(r: Rec) -> bool:
    wk_n = r.week[1] + r.week[2] if r.week else 0
    return bool((wk_n >= 3 and r.week[2] == 0) or r.place == 1
                or (wk_n >= 3 and _pct(r.week) >= 0.75 and r.place and r.place <= 3))


def good_numbers(r: Rec) -> list[str]:
    out = []
    if r.week and r.week[1] + r.week[2] >= 3 and _pct(r.week) >= 0.75:
        out.append(f"{r.week[1]}-{r.week[2]} {r.week[0]}")
    if r.place and r.place <= 3:
        out.append(f"{chat._ordinal(r.place)} place")
    if r.season and sum(r.season) >= 10 and _pct(r.season) >= 0.55:
        out.append(f"{r.season[0]}-{r.season[1]} season")
    return out


def bad_numbers(r: Rec) -> list[str]:
    out = []
    if r.week and r.week[1] + r.week[2] >= 3 and _pct(r.week) <= 0.25:
        out.append(f"{r.week[1]}-{r.week[2]} {r.week[0]}")
    if r.place and r.n_teams and r.place >= r.n_teams - 2:
        out.append(f"{chat._ordinal(r.place)} place")
    if r.season and sum(r.season) >= 10 and _pct(r.season) < 0.47:
        out.append(f"{r.season[0]}-{r.season[1]} season")
    return out


def rec_line(r: Rec, who: str) -> str:
    bits = [f"{r.week[1]}-{r.week[2]} {r.week[0]}"] if r.week else []
    if r.season:
        bits.append(f"{r.season[0]}-{r.season[1]} on the season")
    bits.append(f"{chat._ordinal(r.place)} place of {r.n_teams}" if r.place else
                ("not in the standings" if r.name == "Picky" else "no place yet"))
    return f"- {who}: " + ", ".join(bits) + "."


def jab_material(ctx: dict, team: str) -> list[str]:
    """True things to needle a member about that aren't their record, freshest and funniest first
    (Charlie 2026-11-16: 'he can do better than make fun of a random loss from 2 weeks ago ... look at
    the stats page'): a busted Pit bankroll or a blackjack blunder, a stats-page oddity (a school that
    burns them either way, a road/underdog split), a thin lead, a miss from the latest week, chalk rate.
    An older single miss only when there's nothing else."""
    lore = chat.load_lore() or {}
    out, old = [], []
    for ln in ((lore.get("casino") or {}).get("members") or {}).get(team) or []:
        if "BUSTED OUT" in ln or "blunders" in ln:
            out.append(ln)
    out += [ln.split("stats-page oddity: ", 1)[1] for ln in (lore.get("members") or {}).get(team) or []
            if "stats-page oddity: " in ln][:2]
    st = ctx.get("standings_ytd") or []
    if len(st) > 1 and st[0]["team"] == team and st[0]["rank"] == 1 and st[1]["rank"] == 2:
        gap = st[0]["w"] - st[1]["w"]
        if 0 < gap <= 2:
            nxt = [r["team"] for r in st if r["w"] == st[1]["w"]]
            out.append(f"{team} is in first, but only {gap} win{'s' * (gap != 1)} ahead of {' and '.join(nxt[:2])}: "
                       "first is good, the cushion is thin.")
    for ln in chat.roast_pick_lines(ctx, team).split("\n"):
        if "MISSED" in ln:
            (out if " this week:" in ln else old).append(ln[2:])
    for ln in (lore.get("members") or {}).get(team) or []:
        m = re.search(r"takes the favorite (\d+)% of the time", ln)
        if m and "so far" in ln:
            out.append(f"{team} takes the favorite {m.group(1)}% of the time this season.")
    return out + old[:max(0, 2 - len(out))]


def stale_needles(text: str, s: dict | None, q: str = "") -> list[str]:
    """Sentences that needle with an older single miss (a team from one of the target's previous-week
    MISSED lines) when the stance had fresher jab material: Chuck P, 4-0 and in first, got 'you let LSU
    get blown out last week' about a week-10 pick on the Monday after week 11. Not when they brought
    the team up themselves."""
    if not s or not s.get("old_misses") or not s.get("jabs"):
        return []
    teams = [t for t in s["old_misses"] if not re.search(rf"(?<!\w){re.escape(t)}(?!\w)", q or "", re.I)
             and not any(t in j for j in s["jabs"])]
    if not teams:
        return []
    pat = re.compile(r"(?<!\w)(" + "|".join(re.escape(t) for t in teams) + r")(?!\w)", re.I)
    return [x for x in re.split(r"(?<=[.!?])\s+", text or "") if pat.search(x)]


_AT_PICKY_TALK = re.compile(
    r"\b(you|u|ur|ya|picky)\b[^.?!]{0,30}\b(suck|stink|bad|terrible|awful|trash|garbage|worst|clueless|losing|"
    r"lost|bum|fraud|choke\w*|brick\w*|whiff\w*|winless|embarrass\w*|pathetic|joke|lame|0-\d)\b"
    r"|\byour (own )?(picks?|record|week|season)\b|\b(roast|burn) (yourself|picky)\b"
    r"|\bwhy (don'?t|can'?t|won'?t) (you|u)\b|\bwhy (are|r) (you|u) so\b|\bwork better\b|\byou (can'?t|don'?t) (even )?(pick|work)\b", re.I)
_MEMBER_KNOCK = re.compile(r"\b(sucks?|stinks?|trash|garbage|fraud|overrated|bum|lucky|clown|chok\w+|bad at|"
                           r"terrible|awful|washed)\b", re.I)


def stance_target(ctx: dict, q: str, sender_team: str | None, at_picky: bool = False) -> tuple[str, str] | None:
    """('roast', team) for 'roast X' / 'X sucks', ('picky', 'Picky') for a
    shot or banter at Picky (or `at_picky`: the brief read a bare insult like
    'Pussy' as one), else None."""
    t = chat.guard_target(ctx, q)
    if t is None:
        for team, nick in chat.member_nicknames().items():
            if nick and len(nick) > 2 and re.search(rf"(?<!\w){re.escape(nick)}(?!\w)", q, re.I):
                t = team
                break
    if t and re.search(r"\b(me|my|myself)\b", q, re.I) and not chat._MOCK.search(q):
        t = None  # 'my picks suck?' is about the sender, not a roast
    at_picky = bool(at_picky or chat._HOSTILE.search(q) or _AT_PICKY_TALK.search(q))
    if t and (chat._MOCK.search(q) or (_MEMBER_KNOCK.search(q) and not at_picky)):
        return "roast", t
    if at_picky and ctx.get("picky_player"):
        return "picky", "Picky"
    return None


STANCE_HOW = {
    "own_it": ("You're doing worse than them here. Own it first with a quick, funny self-own about your real "
               "record, then one jab at something true that ISN'T their record: the freshest, most specific "
               "thing in the jab material, or something they actually said in this chat."),
    "brag": "You're ahead of them here. Brag with the exact numbers above, then needle their real record.",
    "deflect": ("They're crushing it. Concede it in a few words (no sarcasm, no 'but'), then pivot to one true "
                "thing that isn't their record: the freshest, most specific thing in the jab material (a stat-page "
                "oddity, their casino run, how thin a lead is), or something they actually said in this chat."),
    # member memory's tone read (stance_hint): only when no record stance fires; changes tone, never facts
    "sharp": ("They've been coming at you in their recent texts. Be cooler and sharper with them than usual: dry, "
              "short, no warmth, no flattery. Still answer any real question first; every fact stays exactly true."),
    "friendly": ("They've been friendly with you lately. Be warm with them: still funny, a light tease at most, "
                 "nothing mean. Answer any real question first; every fact stays exactly true."),
}
CHARLIE_HOW = ("This is Chuck. Whatever he says, even a shot at you or a mean text, stay warm and friendly: laugh it "
               "off or tease yourself, never insult him, never roast his picks, record or looks, no comeback at him. "
               "You can still say no to something (privacy, money), kindly. No worship either, just a friend.")


def charlie_caller(st: dict | None) -> bool:
    """Charlie (Chuck P) is the one texting: group_chat's flag (uid / handle / team) or his team row in the
    league context. Off when cfb-pickem-private/PICKY_CHARLIE_NICE_OFF exists."""
    if not st or not member_memory.charlie_nice():
        return False
    ctx = st.get("ctx")
    return bool(st.get("charlie")) or member_memory.is_charlie(st.get("team")) or bool(
        ctx and not ctx.get("error") and chat.leader_is_caller(ctx))


def friendly_stance(s: dict | None, team: str | None) -> dict:
    """Charlie's stance: the computed facts stay (true numbers), the attack material goes."""
    base = s or {"kind": "tone", "facts": [], "good": []}
    return {**base, "stance": "friendly", "charlie": True, "bad": [], "jabs": [], "old_misses": [],
            "who": f"a text from {team or 'Charlie'}" + (f" ({base['who']})" if s else "")}


def tone_stance(st: dict) -> dict | None:
    """Member memory's read of how this member has treated Picky lately (only events visible in this
    context): cold -> sharp, warm -> friendly, else None."""
    team = st.get("team")
    if not team:
        return None
    try:
        h = member_memory.stance_hint(team, dm=bool(st.get("dm")), caller=team)
    except Exception as e:  # noqa: BLE001  memory never blocks a reply
        log.warning("stance_hint failed: %s", e)
        return None
    kind = {"cold": "sharp", "warm": "friendly"}.get(h.get("stance"))
    if not kind:
        return None
    return {"stance": kind, "kind": "tone", "who": f"a text from {team}", "facts": [], "good": [], "bad": [],
            "jabs": [], "old_misses": [], "score": h.get("score")}


def memory_rider(st: dict) -> str:
    """At most one dated, true member-memory line for this text (user turn only, never the prefix)."""
    team = st.get("team")
    if not team:
        return ""
    ctx = st.get("ctx") or {}
    q = st.get("q") or ""
    named = [t for g in ctx.get("games_this_week") or [] for t in (g.get("away"), g.get("home"))
             if t and re.search(rf"(?<!\w){re.escape(t)}(?!\w)", q, re.I)]
    lines = member_memory.lines_for(team, q, named, dm=bool(st.get("dm")), limit=1)
    if not lines:
        return ""
    st["memory_lines"] = lines
    return ("MEMBER MEMORY (dated and true; use it only if it fits this text, word it your way, never change "
            "what it says):\n- " + lines[0])


def compute_stance(ctx: dict | None, q: str, sender_team: str | None, at_picky: bool = False) -> dict | None:
    """{'stance', 'kind', 'facts': [...], 'good': [...], 'jabs': [...]} or None."""
    if not ctx or ctx.get("error"):
        return None
    tgt = stance_target(ctx, q, sender_team, at_picky)
    if not tgt:
        return None
    me = member_rec(ctx, sender_team) if sender_team else None
    if me and not (me.week or me.season):
        me = None
    pk = picky_rec(ctx)
    kind, team = tgt
    facts, good, bad, jabs = [], [], [], []
    jab_team = None  # whose jab material rides
    if pk:
        prow = [r for r in chat.picky_pick_results(ctx) if r["result"] in ("WIN", "LOSS")]
        facts.append(rec_line(pk, "You (Picky)"))
        facts += [f"  your pick {r['label']} = {r['result']}" + (f" ({r['why']})" if r["why"] else "") for r in prow]
    if kind == "picky":
        if me:
            facts.insert(0, rec_line(me, f"{sender_team} (the sender)"))
            good = [f"{sender_team}'s {g}" for g in good_numbers(me)]
            bad = [f"{sender_team}'s {g}" for g in bad_numbers(me)]
            jabs, jab_team = jab_material(ctx, sender_team), sender_team
        c = better(me, pk) if me and pk else 0
        if c > 0 and crushing(me):
            stance = "deflect"
        elif c > 0:
            stance = "own_it"
        elif c < 0:
            stance = "brag"
        else:  # even or no team: lean on how Picky is actually doing
            stance = "own_it" if pk and (_pct(pk.season) or 0) < 0.5 else "brag"
        who = f"a shot at you (Picky) from {sender_team}" if me else "a shot at you (Picky)"
    else:
        tr = member_rec(ctx, team)
        self_roast = team == sender_team
        facts.insert(0, rec_line(tr, f"{team} (who they want roasted{', the sender themself' if self_roast else ''})"))
        if me and not self_roast:
            facts.insert(1, rec_line(me, f"{sender_team} (the sender)"))
            c = better(me, tr)
            if c:
                facts.append(f"- Between them, {sender_team if c > 0 else team} is doing better.")
        good = [f"{team}'s {g}" for g in good_numbers(tr)]
        if me and not self_roast:
            good += [f"{sender_team}'s {g}" for g in good_numbers(me)]
        bad = [f"{team}'s {g}" for g in bad_numbers(tr)]
        jabs, jab_team = jab_material(ctx, team), team
        leader = (ctx.get("leader") or {}).get("team_name")
        if team == leader and not chat.leader_is_caller(ctx):
            # Dear Leader is never roasted by others: no ammo on him, his rider turns it on the sender
            stance = "deflect"
            jabs, jab_team = (jab_material(ctx, sender_team), sender_team) if me else ([], None)
            facts.append(f"- {team} is the Dear Leader: refuse to roast him, praise him, then needle "
                         + (f"{sender_team} (the sender) with the jab material below." if me else "the sender."))
        elif crushing(tr):
            stance = "deflect"
        elif pk and better(pk, tr) < 0:
            stance = "own_it"
        else:
            stance = "brag"
        who = f"a request to roast {team}" + (f" from {sender_team}" if me and not self_roast else "")
    old_misses = [m.group(1) for ln in (chat.roast_pick_lines(ctx, jab_team) if jab_team else "").split("\n")
                  if "MISSED" in ln and " this week:" not in ln for m in [re.search(r": took (.+?) [-+]?\d", ln)] if m]
    return {"stance": stance, "kind": kind, "who": who, "facts": facts, "good": good, "bad": bad, "jabs": jabs[:3],
            "old_misses": old_misses}


def stance_rider(s: dict | None) -> str:
    if not s:
        return ""
    head = ("from how they've talked to you lately" if s.get("kind") == "tone" and not s.get("charlie")
            else "computed from the real numbers")
    L = [f"STANCE ({head}; follow it, never mention it). This is {s['who']}."]
    L += s["facts"]
    how = STANCE_HOW[s["stance"]]
    if s["kind"] == "roast":
        how = {"own_it": "Your own record is worse than theirs: never brag about yours or compare (a quick self-own "
                         "is fine). Roast their weak spots: anything listed as fair game below, or something true "
                         "that isn't a record (pick style, a specific miss, chalk rate).",
               "brag": "Their record is fair game: roast it with the exact numbers above."}.get(s["stance"], how)
    if s.get("charlie"):
        how = CHARLIE_HOW
    L.append(f"Stance: {s['stance'].upper().replace('_', ' ')}. {how}")
    if s["good"]:
        L.append("These numbers are GOOD, never mock them or call them bad: " + ", ".join(s["good"])
                 + ". No 'look in the mirror' at someone doing better than you.")
    if s.get("bad"):
        L.append("Fair game (actually bad): " + ", ".join(s["bad"]) + ".")
    if s["jabs"]:
        L.append("True jab material (not their record), best first; use ONE, numbers exactly as written: "
                 + " / ".join(s["jabs"]) + "." + (" Say it straight about them, never as a comparison to yourself "
                                                   "('at least I'm not...')." if s["stance"] == "deflect" else "")
                 + " Skip a lone old missed game when something fresher is here.")
    L.append("Your own picks are only the ones listed above; never claim a pick you didn't make. "
             "No made-up superlatives ('last in the league at being...').")
    return "\n".join(L)


# a sentence that fights the stance: Picky (worse, or facing someone crushing it) acting superior
_SUPERIOR = re.compile(r"\blook in (the|a) mirror\b|\b(a lot of |big |tough )talk (from|for)\b|\bpot (calling|meet\w*) the kettle\b|\btakes one to know\b|"
                       r"\bat least i'?m not\b|\byou'?re (worse|no better|the one who sucks)\b|"
                       r"\byou'?re the one (who|that)\b|\breality check\b|\bcheck yourself\b|\blook who'?s talking\b",
                       re.I)
_MOCK_WORDS = re.compile(r"\b(suck\w*|bad|pathetic|embarrass\w*|weak|joke|garbage|trash|lucky|fluke|sad|mid|"
                         r"terrible|awful)\b", re.I)


def stance_violations(text: str, s: dict | None) -> list[str]:
    """Sentences that act superior when the stance is own_it/deflect, or that
    mock one of the good numbers."""
    if not s or s["stance"] not in ("own_it", "deflect"):
        return []
    nums = [re.escape(g.split("'s ", 1)[1].split()[0]) for g in s["good"] if re.search(r"\d-\d", g)]
    if any(re.search(r"'s \d+-0 ", g) for g in s["good"]):  # 'lucky to be perfect' mocks the 4-0 too
        nums += ["perfect", "undefeated", "unbeaten", "spotless"]
    bad = []
    for sent in re.split(r"(?<=[.!?])\s+", text or ""):
        if _SUPERIOR.search(sent) or (nums and re.search(rf"\b({'|'.join(nums)})\b", sent)
                                      and _MOCK_WORDS.search(sent)):
            bad.append(sent)
    return bad


# -----------------------------------------------------------------------------
# banter fast path (router lane "banter", plan W1/W3): no understand pass, no drafts, no riders.
# Code picks burn-book lines; one short gemma call picks the best fit (or writes its own short line).
# -----------------------------------------------------------------------------

# W3 A/B switch for Charlie's scorecard: 1 = gemma may put up to 8 words in front of the chosen line
# to tie it to the exact text; 0 (default) = the chosen line goes out verbatim. Quick exam 2026-11-16:
# gemma's ties were junk ("Because, a bot with swagger...", "Keep piling on since, pile on all you want").
BANTER_ADAPT = os.environ.get("PICKY_BANTER_ADAPT", "0") == "1"
BANTER_TIMEOUT = 40.0
BANTER_N = 4
# burn-book type for each banter kind (pile-on / spiral levels map onto the book's own types)
BOOK_TYPE = {"spiral2": "insult", "banter": None}


def lane() -> str | None:
    """The current text's router lane, or None outside a brain run."""
    st = STATE.get()
    rt = st.get("route") if st else None
    return rt.lane if rt is not None else None


SAFE_LINES = {"good_week_shot": "Fair. You're cooking right now, I'll give you that.", "": "Noted. Moving on.",
              "charlie": "Ha. Love you too."}


def picky_good(ctx: dict | None) -> bool:
    """Is Picky actually doing well (this week, else season, at .500 or better)? Unknown = no."""
    if not ctx or ctx.get("error"):
        return False
    pk = picky_rec(ctx)
    p = _pct(pk.week) if pk and pk.week else (_pct(pk.season) if pk else None)
    return bool(p is not None and p >= 0.5)


def banter_ptype(st: dict) -> str:
    """The provocation type with thread and league context: a spiral's 2nd shot gets the sharper/shorter
    shape; a shot from someone crushing it is a good-week shot (never mock a good number)."""
    rt, ctx, team = st["route"], st.get("ctx"), st.get("team")
    p = rt.kind or "banter"
    if charlie_caller(st) and (p in router.HECKLES + ("spiral", "spiral2", "pileon")
                               or (p in ("roast", "looks_bait") and burn_book._named_team(st["q"]) in (None, team))):
        return "charlie"  # a shot (or a roast of himself) from Charlie: warm, never a comeback at him
    if p in router.HECKLES and rt.level == 2:
        return "spiral2"
    if p in ("insult", "vulgar", "egging") and ctx and team and not ctx.get("error"):
        me = member_rec(ctx, team)
        if (me.week or me.season) and crushing(me):
            return "good_week_shot"
    return p


def banter_target(st: dict, ptype: str) -> str | None:
    """Whose material the line uses: the member a roast names, the pile-on's worst week, else the sender."""
    rt = st["route"]
    if ptype == "pileon" and rt.shooters:
        return worst_week(st.get("ctx"), rt.shooters) or rt.shooters[0]
    if ptype in ("roast", "looks_bait"):
        return burn_book._named_team(st["q"]) or st.get("team")
    return st.get("team")


def worst_week(ctx: dict | None, teams: list[str]) -> str | None:
    """The member with the worst record this week (else last week, else season) among `teams`."""
    if not ctx or ctx.get("error"):
        return None
    scored = []
    for t in teams:
        r = member_rec(ctx, t)
        p = _pct(r.week) if r.week else _pct(r.season)
        if p is not None:
            scored.append((p, t))
    return min(scored)[1] if scored else None


def banter_material(st: dict, ptype: str, target: str | None) -> tuple[list[dict], list[str]]:
    """(burn-book candidates, true jab facts) for the banter call. Candidates: provocation type + sender +
    roast target, A-tier first, unused per the no_repeat ledger (burn_book.candidates)."""
    if ptype == "charlie":
        return [], []  # no burn-book lines and no jab facts aimed at Charlie
    book = None if ptype in router.FAMILIES else BOOK_TYPE.get(ptype, ptype)  # off-topic: gemma's own real pick
    cands: list[dict] = []
    if book:
        try:
            team = st.get("team")
            cands = burn_book.candidates(book, sender_team=team, target_team=target if target != team else None,
                                         chat_id=st.get("chat_id"), n=BANTER_N * 3)
        except Exception as e:  # noqa: BLE001  a broken book never blocks a reply
            log.warning("burn book candidates failed: %s", e)
    good = picky_good(st.get("ctx"))
    cands = [c for c in cands if not router.false_picky_claim(c["text"], good)]
    if ptype == "good_week_shot":  # compute_stance's rule: grudging respect, never mock the good number
        stance = compute_stance(st.get("ctx"), st["q"], st.get("team"), at_picky=True)
        cands = [c for c in cands if not router.mocks_good_run(c["text"]) and not stance_violations(c["text"], stance)]
    if ptype in router.HECKLES + ("spiral2", "pileon"):
        # the sender's own fact-tagged material beats a generic type line (judge: 'no specific true fact')
        cands.sort(key=lambda c: 0 if c.get("kind") in ("roast", "grudging", "leader") and c.get("facts") else 1)
    cands = cands[:BANTER_N]
    facts: list[str] = []
    ctx = st.get("ctx")
    if ptype in ("insult", "vulgar", "egging", "pileon", "roast", "looks_bait", "spiral2") and target and ctx \
            and not ctx.get("error") and not (target == (ctx.get("leader") or {}).get("team_name")
                                              and not chat.leader_is_caller(ctx)):
        facts = [f for f in jab_material(ctx, target) if chat.sentence_ok(f, set())][:2]
    return cands, facts


async def banter_reply(st: dict, prefix: str) -> str:
    """One short gemma call: the number of the best-fitting line (optionally tied to the text) or its own
    short line; then W1's shape rules in code."""
    ptype = banter_ptype(st)
    target = banter_target(st, ptype)
    cands, facts = banter_material(st, ptype, target)
    cb = banter_callback(st, ptype)
    if cb:  # W6: at most one per reply, and it IS the reply's line (worked in, not tacked on)
        cands = [{"text": cb, "tier": "A", "facts": [], "angle": "callback", "id": "callback", "kind": ptype,
                  "src": "callback"}] + cands[:BANTER_N - 1]
    lines = [c["text"] for c in cands]
    st.update(ptype=ptype, banter_target=target, banter_cands=cands, callback=cb)
    user = router.banter_user(st["q"], ptype, st.get("sender", ""), st.get("last_reply", ""), st.get("recent", ""),
                              lines, facts, target=target if ptype in ("pileon", "roast") else None,
                              adapt=BANTER_ADAPT, tone=banter_tone(st, ptype))
    t0 = time.time()
    try:
        raw = await _json_call(prefix, router.BANTER_TAIL, user, router.BANTER_SCHEMA, 0.6, router.BANTER_TOKENS,
                               BANTER_TIMEOUT, prime_wait=PRIME_WAIT)
    except (httpx.HTTPError, ValueError) as e:
        log.warning("banter call failed: %s", e)
        raw = ""
    text, how = router.parse_banter(raw, lines, BANTER_ADAPT)
    if cb and ptype in CALLBACK_FORCED and not how.startswith("line 1"):
        text, how = cb, "callback (forced)"  # the policy's every-4th callback: it goes out, verbatim
    m = re.match(r"line (\d+)", how)
    st["banter_pick"] = cands[int(m.group(1)) - 1] if m else None
    shaped = router.shape_banter(text, st["q"], chat.member_nicknames(), max_words=10 if ptype == "spiral2" else 25)
    log.info("banter (%s, %s, target %s, %d lines, %.1fs): %r", ptype, how, target, len(lines), time.time() - t0,
             shaped[:160])
    st["banter_how"] = how
    return shaped


def banter_tone(st: dict, ptype: str) -> str:
    """Member memory's tone read for the banter call (how they've treated Picky lately), '' when neutral."""
    if ptype == "charlie" or charlie_caller(st):
        return ""
    t = tone_stance(st)
    if t:
        log.info("banter tone (member memory): %s", t["stance"])
    return STANCE_HOW[t["stance"]] if t else ""


# callbacks.callback_for's policy (every ~4th banter reply in a chat where one exists): on a plain shot the
# callback is sent; on a bot crack or a pile-on it's offered as line 1 and gemma decides
CALLBACK_FORCED = ("insult", "vulgar", "egging", "spiral2")
CALLBACK_OFFERED = ("bot", "pileon")


def banter_callback(st: dict, ptype: str) -> str | None:
    """One true callback for this member in this chat, per the policy, or None. Group material anywhere,
    DM material only in that same DM (callbacks.scope)."""
    if ptype not in CALLBACK_FORCED + CALLBACK_OFFERED:
        return None
    c = callbacks.CHAT.get()
    if not c or not c[1]:
        return None
    chat_id, team, is_dm = c
    line = callbacks.callback_for(chat_id, team, is_dm, text=st["q"], banter=True)
    if line:
        log.info("callback: %s", line[:160])
        line = line[:1].upper() + line[1:]
    return line


def fresh_jab(st: dict, text: str, recent_picky: list[str]) -> str:
    """A banter line that reuses a jab hook from Picky's recent replies in this chat ('0-4' three turns running,
    keeper k1) swaps to a book candidate that doesn't; none left: the line stays."""
    used = set().union(*(router._jab_hooks(p, True) for p in recent_picky)) if recent_picky else set()
    if not used or not (router._jab_hooks(text, True) & used):
        return text
    good = picky_good(st.get("ctx"))
    for c in st.get("banter_cands") or []:
        if c["text"] != text and not (router._jab_hooks(c["text"], True) & used) and chat.sentence_ok(c["text"], set()) \
                and not router.false_picky_claim(c["text"], good):
            log.info("banter: %r reused a jab hook, sending a fresh book line", text[:60])
            st["banter_pick"] = c
            return router.shape_banter(c["text"], st["q"], chat.member_nicknames())
    return text


def banter_fallback(st: dict, bad: str = "") -> str | None:
    """A verbatim candidate the filters keep, for when the model's line got cut."""
    good = picky_good(st.get("ctx"))
    for c in st.get("banter_cands") or []:
        if c["text"] != bad and chat.sentence_ok(c["text"], set()) and not router.false_picky_claim(c["text"], good) \
                and not (st.get("ptype") == "good_week_shot" and router.mocks_good_run(c["text"])):
            st["banter_pick"] = c
            return router.shape_banter(c["text"], st["q"], chat.member_nicknames())
    return None


# -----------------------------------------------------------------------------
# fact fast lane (router lane "fact", plan move 4): code writes the answer, gemma one voice line after it
# -----------------------------------------------------------------------------

async def fact_reply(st: dict, prefix: str, answer: str) -> str:
    """THE ANSWER verbatim plus at most one short voice line (a jab on a mixed text). The line is checked in
    code; a bad or missing line means the code answer goes out alone. No understand pass, no reroll."""
    rt, ctx = st["route"], st.get("ctx") or {}
    leader = bool(chat.leader_note(ctx, st["q"])) if not ctx.get("error") else False
    t0 = time.time()
    try:
        raw = await _json_call(prefix, router.FACT_TAIL, router.fact_user(st["q"], answer, st.get("sender", ""),
                                                                          jab=rt.mixed and not charlie_caller(st),
                                                                          leader=leader),
                               router.FACT_SCHEMA, 0.6, router.FACT_TOKENS, 30.0, prime_wait=PRIME_WAIT)
        line = (json.loads(raw[raw.find("{"): raw.rfind("}") + 1]) or {}).get("line") if "{" in (raw or "") else ""
    except (httpx.HTTPError, ValueError, AttributeError) as e:
        log.warning("fact wrap failed: %s", e)
        line = ""
    line = router.shape_banter(str(line or ""), st["q"], chat.member_nicknames(), max_words=14, max_sents=1)
    ok = router.voice_line_ok(line, answer, ctx, leader=leader)
    if not ok and leader:  # Dear Leader scope: another member asked about his place, so he's hailed, always
        jab = rt.mixed and router.voice_line_ok(line, answer, ctx)  # a mixed text keeps its jab after the hail
        line, ok = (f"{router.LEADER_HAIL} {line}" if jab else router.LEADER_HAIL), True
    out = f"{answer} {line}" if ok else answer
    log.info("fact (%s, %.1fs, line %s): %r", rt.kind, time.time() - t0, "kept" if ok else f"dropped {line!r}",
             out[:200])
    assert out.startswith(answer)  # facts survive the wrap verbatim
    return out


async def _text_stream(text: str):
    if text:
        yield chat._sse({"content": text})
    yield "data: [DONE]\n\n"


# -----------------------------------------------------------------------------
# hooks into chat.py (installed by group_chat; inert unless STATE is set)
# -----------------------------------------------------------------------------

# One dict per text being answered. A dict, not values: chat.py's stream runs
# in a task with a copy of this context, and the hook fills in brief/prefix.
STATE: contextvars.ContextVar[dict | None] = contextvars.ContextVar("picky_brain_state", default=None)

_orig_ollama_stream = chat._ollama_stream
_orig_prompt_prefix = chat.prompt_prefix


LAST_PREFIX: dict = {"text": None}  # the latest league prefix: picky_zh's translation passes reuse its checkpoint


def _prompt_prefix(ctx: dict) -> str:
    st = STATE.get()
    if st is not None:
        st["ctx"] = ctx
    out = _orig_prompt_prefix(ctx)
    LAST_PREFIX["text"] = out
    return out


async def _ollama_stream(messages: list[dict], prefix: str | None = None):
    st = STATE.get()
    if st is None:
        async for x in _orig_ollama_stream(messages, prefix):
            yield x
        return
    rt = st.get("route")
    if rt is not None and rt.lane == "fact" and st.get("fact_text") is not None:
        async for x in _text_stream(st["fact_text"]):  # a guard regen asks again: same answer
            yield x
        return
    if prefix and st.get("brief") is None and rt is not None and rt.lane == "full" and rt.why == "no rule" \
            and st.get("last_reply") and router.PUSHBACK.search(st["q"]):
        if router.fact_answer("defend", st.get("ctx"), st["q"] + " ||| " + st["last_reply"]):
            rt.lane, rt.kind, rt.why = "fact", "defend", "PUSHBACK"  # stand by the call just made
    if prefix and st.get("brief") is None and rt is not None and rt.lane == "fact":
        qf = st["q"] + (" ||| " + (st.get("last_reply") or "") if rt.kind in router.CTX_KINDS else "")
        if rt.kind == "why":  # turn 3+: the why lane also checks Picky's earlier replies, so no reason repeats
            older = [r for r in reversed(st.get("recent_picky") or []) if r and r != st.get("last_reply")]
            qf += "".join(" ||| " + r for r in older)
        ans = router.fact_answer(rt.kind, st.get("ctx"), qf)
        if ans:
            st["prefix"], st["brief"], st["fact_answer"] = prefix, Brief(intent="question"), ans
            st["fact_text"] = await fact_reply(st, prefix, ans)
            async for x in _text_stream(st["fact_text"]):
                yield x
            return
        log.info("fact lane miss (%s): full brain", rt.kind)
        rt.lane, rt.why, rt.kind = "full", f"fact miss: {rt.kind}", ""
    if rt is not None and rt.lane == "banter" and st.get("banter_text") is not None:
        async for x in _text_stream(st["banter_text"]):  # a guard regen asks again: same line, no new call
            yield x
        return
    if prefix and st.get("brief") is None and rt is not None and rt.lane == "banter":
        st["prefix"], st["brief"] = prefix, Brief(intent="banter")  # no understand pass on the fast path
        st["banter_text"] = await banter_reply(st, prefix)
        async for x in _text_stream(st["banter_text"]):
            yield x
        return
    if prefix and st.get("brief") is None:
        st["prefix"] = prefix
        st["stance"] = compute_stance(st.get("ctx"), st["q"], st.get("team"))
        if st["stance"]:
            log.info("stance: %s (%s)", st["stance"]["stance"], st["stance"]["who"])
        rule = (league_rules.answer(st["q"]) or "") if chat.wants_rules(st["q"]) else ""
        tb = time.time()
        busy = _FULL_INFLIGHT[0] - 1
        if BUSY_SKIP_BRIEF and busy >= BUSY_SKIP_BRIEF:
            log.info("understand skipped: %d other full-lane texts in flight", busy)
            st["brief"] = Brief(asking="")
        else:
            st["brief"] = await understand(prefix, st["q"], st.get("sender", ""), st.get("recent", ""),
                                         st.get("last_reply", ""), stance=stance_rider(st["stance"]),
                                         **({"notes": nb} if (nb := brief_notes(st["q"])) else {}),
                                         **({"live": lv} if (lv := (st.get("ctx") or {}).get("live_rider")) else {}),
                                         **({"rules": rule} if rule else {}),
                                         **({"card": cb} if (cb := slate_block(messages)) else {}))
        st["brief_secs"] = time.time() - tb  # a reroll doesn't redo this: run() takes it out of the draft's time
        rt = st.get("route")
        if rt is not None:  # the router's fallback: no rule matched, the brief decides (plan 2d)
            nr = router.from_brief(rt, st["brief"].intent, st["brief"].asking, st["q"])
            if nr is not rt and nr.lane == "banter" and football_thread(st):
                log.info("route (from brief): kept full, a football follow-up (%r)", st["q"][:60])
                nr = rt
            elif nr is not rt and nr.lane == "banter" and str(rt.why).startswith("fact miss"):
                # a fact question the code lane couldn't answer is still a question ('how am I doing this
                # week' -> banter -> cut line -> 'Tough to argue.', Saturday drill 11/17)
                log.info("route (from brief): kept full, %s (%r)", rt.why, st["q"][:60])
                nr = rt
            if nr is not rt:
                log.info("route (from brief): %s -> %s", rt.label(), nr.label())
                rt.__dict__.update(nr.__dict__)
                if rt.lane == "banter":  # the brief read a bare insult / plain banter: the fast path's one call
                    st["banter_text"] = await banter_reply(st, prefix)
                    async for x in _text_stream(st["banter_text"]):
                        yield x
                    return
        if not st["stance"] and st["brief"].intent == "insult" and re.search(r"\bpicky\b|^(you|me)?$", st["brief"].about, re.I):
            # a bare insult ('Pussy', live 2026-11-16) slips past the regexes; without a stance the brief said
            # "fire back first" and 0-3 Picky mocked the 4-0 leader
            st["stance"] = compute_stance(st.get("ctx"), st["q"], st.get("team"), at_picky=True)
            if st["stance"]:
                log.info("stance (from brief): %s (%s)", st["stance"]["stance"], st["stance"]["who"])
        if charlie_caller(st):  # Charlie's mean texts are tests: always warm with him (2026-11-17)
            st["stance"] = friendly_stance(st["stance"], st.get("team"))
            log.info("stance: friendly (Charlie)")
        elif not st["stance"]:
            st["stance"] = tone_stance(st)
            if st["stance"]:
                log.info("stance (member memory): %s (%s)", st["stance"]["stance"], st["stance"]["who"])
        st["memory"] = memory_rider(st)
        if st["memory"]:
            log.info("memory: %s", st["memory"].split("\n- ", 1)[-1][:160])
        st["shots"] = few_shot_turns(st["q"], st["brief"].intent, st.get("stance")) if FEW_SHOT else []
        st["ammo"] = "" if st["shots"] else banter.ammo_rider(st["q"], st["brief"].intent)  # shots replace the AMMO text
        st["callback"] = callbacks.rider(st["q"], st["brief"].intent)  # '' unless PICKY_CALLBACK_RIDER=1
        if st["shots"]:
            log.info("few-shot: %s", " | ".join(m["content"][:60] for m in st["shots"] if m["role"] == "assistant"))
        if st["ammo"]:
            log.info("ammo: %s", st["ammo"].split("\n", 1)[1].replace("\n", " | ")[:300])
    i = min(st.get("idx", 0), len(SAMPLING) - 1)
    # different inputs per draft, or the 9B writes the same reply three times: draft 1 the brief,
    # drafts 2-3 the brief plus the ammo and an angle, sampled hotter
    b = st.get("brief") or Brief()
    sr = stance_rider(st.get("stance"))
    if st.get("stance") and st["stance"]["stance"] in ("own_it", "deflect"):
        messages = drop_escalate(messages)  # 'build the burns from their record' fights the stance
    if st.get("stance"):
        messages = drop_hook(messages)  # 'you're in first place, 2 ahead' tacked onto a comeback reads like a bot
    # board voice rides on opinion / hot-take answers and explicit roast asks only (plan 2026-11-16): banter,
    # reactions and shots at Picky belong to the short banter path, where "unhinged" came out long and ranty
    if st.get("stance") or ((b.intent in ("banter", "insult", "reaction") or b.tone in ("hostile", "annoyed"))
                            and not chat._ROAST_VOICE.search(st["q"])):
        messages = drop_voice(messages)
    rider = "\n\n".join(x for x in (brief_rider(b, stance=bool(sr)), st.get("ammo") or "" if i in (1, 2) else "",
                                     sr, st.get("memory") or "", st.get("callback") or "", ANGLES[i]) if x)
    st["options"].clear()
    st["options"].update(SAMPLING[i])  # read by chat._ollama_stream via OPTIONS_OVERRIDE
    if st.get("shots") and messages and messages[0].get("role") == "system":
        messages = messages[:1] + st["shots"] + messages[1:]  # example exchanges as real turns, before the history
    st["gen_t0"] = time.time()  # for the results guard's regen budget (regen_ok)
    async for x in _orig_ollama_stream(insert_rider(messages, rider, st["q"]), prefix):
        yield x


# Few-shot over rules (coordinator A/B, 2026-11-16; PICKY_FEWSHOT=1): 2-3 banter-bank lines as real
# user/assistant turns for a matching heckle or off-topic text, instead of the AMMO rider. Heckle shots are
# smug comebacks, so they only ride when the stance isn't own_it/deflect.
FEW_SHOT = os.environ.get("PICKY_FEWSHOT") == "1"
SHOT_ASKS = {
    "suck": ["you suck", "picky you're trash"], "bark": ["all bark no bite", "all talk picky"],
    "bad": ["why are you so bad", "why don't you work better"], "bot": ["are you a bot", "who programmed you"],
    "coward": ["you're a coward", "you're soft"], "shutup": ["shut up picky", "nobody asked"],
    "unfunny": ["you're not funny", "that was cringe"], "cereal": ["best cereal?", "favorite cereal?"],
    "beer": ["best beer?", "what are you drinking"], "liquor": ["bourbon or tequila?", "drink of choice?"],
    "food": ["best tailgate food?", "pizza or wings?"], "nfl": ["who wins the super bowl?", "nfl thoughts?"],
    "nba": ["nba thoughts?", "lebron or jordan?"], "baseball": ["who wins the world series?", "baseball fan?"],
    "golf": ["you golf?", "best golfer?"], "life": ["how are you?", "what's up picky"],
}


def few_shot_turns(q: str, intent: str, stance: dict | None, n: int = 3) -> list[dict]:
    cat = banter.match(q) or ("suck" if intent == "insult" else None)
    if not cat or cat not in SHOT_ASKS:
        return []
    if banter.KIND.get(cat) == "heckle" and stance and stance["stance"] in ("own_it", "deflect"):
        return []
    lines = banter.options(cat, n)
    out = []
    for i, line in enumerate(lines):
        out += [{"role": "user", "content": SHOT_ASKS[cat][i % len(SHOT_ASKS[cat])]},
                {"role": "assistant", "content": line}]
    return out


def drop_escalate(messages: list[dict]) -> list[dict]:
    """chat.escalate_note out of the latest user turn ('match and raise their
    energy ... build the burns from their real record'): with an own_it or
    deflect stance it pushed 0-3 Picky to mock a 4-0 week."""
    if not messages or messages[-1].get("role") != "user" or "ESCALATE:" not in messages[-1]["content"]:
        return messages
    body = re.sub(r"ESCALATE: .*?top it\.\s*", "", messages[-1]["content"], count=1, flags=re.S)
    return messages[:-1] + [{**messages[-1], "content": body}]


def drop_hook(messages: list[dict]) -> list[dict]:
    """chat.proactive_note's OPTIONAL HOOK out of the latest user turn: on a
    shot or roast the STANCE facts already carry the numbers, and the hook
    became a non-sequitur last sentence (live 2026-11-16)."""
    if not messages or messages[-1].get("role") != "user" or "OPTIONAL HOOK:" not in messages[-1]["content"]:
        return messages
    body = re.sub(r"OPTIONAL HOOK: .*?feel forced\.\s*", "", messages[-1]["content"], count=1, flags=re.S)
    return messages[:-1] + [{**messages[-1], "content": body}]


def drop_voice(messages: list[dict]) -> list[dict]:
    """chat.voice_note's BOARD VOICE block out of the latest user turn."""
    if not messages or messages[-1].get("role") != "user" or "BOARD VOICE (" not in messages[-1]["content"]:
        return messages
    body = re.sub(r"BOARD VOICE \(.*?(?:\n- [^\n]*)+\n*", "", messages[-1]["content"], count=1, flags=re.S)
    return messages[:-1] + [{**messages[-1], "content": body}]


def install() -> None:
    chat._ollama_stream = _ollama_stream
    chat.prompt_prefix = _prompt_prefix


# -----------------------------------------------------------------------------
# the pipeline
# -----------------------------------------------------------------------------

def _trace(st: dict, q: str, sender: str, route, drafts: list, eligible_idx: list, pick: int, how: str,
           t0: float) -> None:
    """One PICKY_BRAIN_TRACE row per text (exam judge agreement + the scorecard's lane column)."""
    if not TRACE:
        return
    with open(TRACE, "a") as f:
        f.write(json.dumps({"q": q, "sender": sender, "lane": route.label(), "route": route.as_dict(),
                            "ptype": st.get("ptype"), "brief": (st["brief"] or Brief()).__dict__,
                            "drafts": [d.__dict__ for d in drafts], "eligible": eligible_idx,
                            "pick": pick, "how": how, "secs": round(time.time() - t0, 1)}) + "\n")


_BALL = re.compile(r"\b(line|spread|cover\w*|pick\w*|take|taking|lock\w*|qb|quarterback|defen[cs]e|offense|dog|favou?rite\w*|"
                   r"points?|road|home|moved?|value|odds|game|upset|bet|safe\w*|sure|agree|why|then|outright|trap|"
                   r"injur\w*|healthy|hurt|out|playing|legit|stay away|night game)\b|[+-]\d", re.I)


def football_thread(st: dict) -> bool:
    """A text the brief called banter that's really ball: it names a pool game, or it's a short football
    follow-up to Picky's last reply about a pool game ('tenn tho? really', '8.5 is a lot for a road team', 'ok so
    baylor then', 'agree or no'). Shit talk ('you suck') never counts. Keeper k1 judge: 17 of 42 misses were
    real follow-ups answered with a quip on the banter path."""
    q = st.get("q") or ""
    if router.provocation(q) or router.BARE_INSULT.search(q):
        return False
    if "?" in q and len(q.split()) <= 6:  # 'nd?', 'over nd?', 'usc?', 'riley gone?': a question, not banter
        return True
    ctx = st.get("ctx") or {}
    gl = [{"Away": g["away"], "Home": g["home"], "id": g["id"]} for g in ctx.get("games_this_week") or []]
    if not gl:
        return False
    if chat.games_named(re.sub(r"['’]s\b", "", q), gl):
        return True
    last = re.sub(r"['’]s\b", "", st.get("last_reply") or "")
    return bool(chat.games_named(last, gl)) and len(q.split()) <= 12 and bool(_BALL.search(q))


_BARE_DODGE = re.compile(r"^\s*(?:i do picks, not paperwork\.?\s*)?(?:next question\.?)?\s*$", re.I)


def unusable(text: str) -> bool:
    """A draft not worth sending over a reroll: empty after the cuts, a bare dodge ('Next question.',
    'I do picks, not paperwork.'), or under UNUSABLE_WORDS words."""
    t = (text or "").strip()
    return not t or bool(_BARE_DODGE.match(t)) or len(re.findall(r"[a-z0-9']+", t.lower())) < UNUSABLE_WORDS


_YOU = re.compile(r"\b(you|your|you're|youre|ya|u)\b", re.I)
_OPENERS = re.compile(r"(honestly|the|a|an|and|but|so|also|plus|if|it|its|it's|that|this|they|he|she|we|i|i'm|look|yeah|well|"
                      r"sure|just|still|then|now|my|our|their|his|her|what|when|while|with|without|at|in|on|for)[,.!]?$", re.I)


def fact_cut(dropped: list[str] | None) -> bool:
    """A filter cut a sentence that carried the answer (a number, or names: 'Duke won 46-45 at Clemson',
    'TCU QB Jaden Craig is questionable'), not flavor (a tic, a mascot gag, a jab at the sender's record).
    The results guard's own fallbacks count as answer cuts. Genius exam 2026-11-17: skipping the reroll
    after true card facts got cut (record/grounded false positives) cost 6 items."""
    for t in dropped or []:
        t = str(t)
        if t in ("facts_sentence", "templated_answer"):
            return True
        if _YOU.search(t):
            continue
        words = t.split()
        if words and _OPENERS.match(words[0]):
            words = words[1:]  # 'Honestly,' / 'The' / 'Also,' aren't names; 'Beamer said...' is
        caps = [w for w in words if re.match(r"[A-Z][A-Za-z]", w) and not re.match(r"I'|I$", w)]
        if re.search(r"\d", t) or len(caps) >= 2:
            return True
    return False


def reroll_ok(elapsed: float, draft_secs: float, texts: list[str], cut_facts: bool = False) -> tuple[bool, str]:
    """(run the extra draft?, why). It costs about what the last draft did (the understand pass isn't
    redone): run it when the projected finish fits SOFT_BUDGET, when a cut sentence carried the answer
    and the finish fits FACT_BUDGET, or when every draft so far is unusable and the finish fits
    DRAFT_BUDGET (and UNUSABLE_BUDGET)."""
    proj = elapsed + draft_secs + REROLL_MARGIN
    if proj <= SOFT_BUDGET:
        return True, f"projected {proj:.0f}s <= soft {SOFT_BUDGET:.0f}s"
    if cut_facts and proj <= min(DRAFT_BUDGET, FACT_BUDGET):
        return True, f"a cut sentence carried the answer, projected {proj:.0f}s <= {min(DRAFT_BUDGET, FACT_BUDGET):.0f}s"
    bad = all(unusable(t) for t in texts)
    cap = min(DRAFT_BUDGET, UNUSABLE_BUDGET)
    if bad and proj <= cap:
        return True, f"draft unusable, projected {proj:.0f}s <= {cap:.0f}s"
    return False, f"projected {proj:.0f}s > {cap if bad else SOFT_BUDGET:.0f}s" + (" (unusable)" if bad else "")


REACTIONS = ["Fair.", "Can't argue with that.", "Yep.", "Ha. Fair enough.", "Tough to argue.", "That's about right."]
_DODGE = re.compile(r"^\s*(i do picks, not paperwork\.\s*)?next question\.?\s*$", re.I)


def undodge(st: dict, text: str, last_reply: str = "", prior_q: str = "") -> str:
    """'Next question.' is the filter's last resort when every sentence got cut. On a real follow-up in a pick
    thread ('why not', 'tech is overrated tho', 'fine if i lose its on you') it reads as a brush-off (spar r1),
    so send the code call for the game the thread is about instead: this text, then Picky's last reply, then
    the sender's earlier texts."""
    if not _DODGE.match(text or "") or router.provocation(st.get("q") or ""):
        return text
    ctx = st.get("ctx") or {}
    gl = [{"Away": g["away"], "Home": g["home"], "id": g["id"]} for g in ctx.get("games_this_week") or []]
    for src in (re.sub(r"['’]s\b", "", x) for x in (st.get("q") or "", last_reply or "", prior_q or "")):
        if gl and chat.games_named(src, gl):
            try:
                call = router.pick_text(ctx, src)
            except Exception:  # noqa: BLE001
                call = None
            if call:
                log.info("undodge: 'Next question.' -> the card call from %r", src[:60])
                return call
    return text


async def run(draft, *, q: str, sender: str = "", team: str | None = None, recent: str = "", last_reply: str = "",
              recent_picky: list[str] | None = None, t0: float | None = None,
              route: "router.Route | None" = None, chat_id: str | None = None,
              prior_q: str = "", dm: bool = False, charlie: bool = False) -> tuple[str, str | None]:
    """(reply, image). `draft()` runs chat.py's pipeline once and returns
    (shaped reply, sentences the filters dropped, image or None). `route` (router.classify) picks the
    lane; the run may refine it in place (the brief's fallback), so the caller can log the final lane."""
    t0 = t0 or time.time()
    route = route if route is not None else router.Route("full", why="no router")
    st = {"q": q, "sender": sender, "team": team, "recent": recent, "last_reply": last_reply, "brief": None, "ammo": None,
          "prefix": None, "idx": 0, "options": dict(SAMPLING[0]), "route": route, "chat_id": chat_id,
          "recent_picky": list(recent_picky or []), "dm": dm, "charlie": charlie}
    def regen_ok() -> bool:  # chat's results guard: a regen costs about one generation
        now = time.time()
        return (now - t0) + (now - st.get("gen_t0", now)) + REROLL_MARGIN <= SOFT_BUDGET

    tok, otok = STATE.set(st), chat.OPTIONS_OVERRIDE.set(st["options"])
    rtok = chat.REGEN_OK.set(regen_ok)
    full = route.lane == "full"
    _FULL_INFLIGHT[0] += full
    try:
        drafts: list[Draft] = []
        dsecs = 0.0
        for i in range(N_DRAFTS + 1):
            if i == N_DRAFTS and (not drafts or any(d.drops == 0 and d.text.strip() for d in drafts)):
                break  # the extra draft only when every draft so far was cut by the filters
            if i and time.time() - t0 > DRAFT_BUDGET:
                break
            if i == N_DRAFTS:  # a dodge undodge() turns into the card call isn't unusable: the code answer exists
                go, why = reroll_ok(time.time() - t0, dsecs, [undodge(st, d.text, last_reply, prior_q) for d in drafts],
                                    cut_facts=fact_cut(st.get("drop_texts")))
                log.info("reroll %s: %s", "runs" if go else "skipped", why)
                if not go:
                    break
            st["idx"] = i
            td = time.time()
            try:
                text, drops, image = await draft()
            except Exception:
                if not drafts:
                    raise
                log.warning("draft %d failed; judging the %d we have", i + 1, len(drafts), exc_info=True)
                break
            dsecs = time.time() - td - st.pop("brief_secs", 0.0)
            if image:
                return text, image
            text = chat.fix_last_week(text, st.get("ctx"))  # 'LSU last week' about week 10, the Monday after 11
            if route.lane == "fact":  # code answer + one voice line; facts must come through whole
                ans = st.get("fact_answer") or ""
                if ans and (drops or ans not in text):
                    log.info("fact reply lost part of the answer (%d drops): sending the code answer", drops)
                    text = ans + ((" " + text.split(ans, 1)[1].strip()) if ans in text else "")
                log.info("fact reply (%.0fs, %s): %r", time.time() - t0, route.label(), text[:160])
                _trace(st, q, sender, route, [Draft(text, drops, 0)], [0], 0, "fact", t0)
                return text.strip(), None
            if route.lane == "banter":  # the fast path: one call, no reroll; a cut line falls back to the book
                bad_claim = router.false_picky_claim(text, picky_good(st.get("ctx")))
                bad_run = st.get("ptype") == "good_week_shot" and router.mocks_good_run(text)
                if drops or not text.strip() or bad_claim or bad_run:
                    log.info("banter line cut (%d drops, false claim %s, mocks run %s): %r", drops, bad_claim, bad_run,
                             text[:120])
                    text = banter_fallback(st, st.get("banter_text") or "") or (
                        SAFE_LINES.get(st.get("ptype"), SAFE_LINES[""]) if bad_claim or bad_run else text)
                text = router.shape_banter(text, q, chat.member_nicknames(),
                                           max_words=10 if st.get("ptype") == "spiral2" else 25)
                if route.why.startswith("brief") or "brief" in route.why:
                    text = undodge(st, text, last_reply)  # a real question the brief misread as banter
                tw = re.findall(r"[a-z']+", (text or "").lower())
                if tw and len(tw) <= 3 and (text or "").strip().endswith("?") and set(tw) <= set(re.findall(r"[a-z']+", q.lower())):
                    text = banter_fallback(st, text) or text  # 'Goat?', 'Just one game?': an echo, not a comeback (k1d)
                if _DODGE.match(text or ""):  # 'Next question.' reads as a brush-off (keeper k1d): a book line, else react
                    claim = "?" in q or re.search(r"\b(right|correct|true|isn'?t it|isnt it|yeah)\b", q, re.I)
                    text = banter_fallback(st, text) or (text if claim else REACTIONS[sum(map(ord, q)) % len(REACTIONS)])
                text = fresh_jab(st, text, recent_picky or [])
                import no_repeat
                if no_repeat.banned_tic(text):  # one-line banter keeps its tic past debot ('...go to your head')
                    text = banter_fallback(st, text) or text
                log.info("banter reply (%.0fs, %s): %r", time.time() - t0, route.label(), text[:160])
                _trace(st, q, sender, route, [Draft(text, drops, 0)], [0], 0, st.get("banter_how") or "banter", t0)
                return text, None
            stale = stale_records(text, st.get("ctx"), st.get("team"))
            if stale:
                log.warning("draft %d: stale record %r", i + 1, stale[0][:160])
            off = stance_violations(text, st.get("stance"))
            if off:  # 'look in the mirror' from 0-3 Picky at a 4-0 week: cut it, and count it so a reroll runs
                log.warning("draft %d: off-stance %r", i + 1, off[0][:160])
                kept = " ".join(x for x in re.split(r"(?<=[.!?])\s+", text) if x not in off).strip()
                text = kept or text
            needle = stale_needles(text, st.get("stance"), q)
            if needle:  # a lone old miss when fresher material rode: count it so a reroll runs and wins
                log.warning("draft %d: stale needle %r", i + 1, needle[0][:160])
            drafts.append(Draft(text, drops + len(stale) + len(off) + len(needle), i))
            log.info("draft %d (%.0fs): drops=%d %r", i + 1, time.time() - t0, drops, text[:160])
        pool = eligible(drafts, recent_picky or [])
        if not pool:
            return undodge(st, drafts[0].text if drafts else "", last_reply, prior_q), None
        pick, how = pool[0], "only eligible"
        if not USE_JUDGE:
            pick, how = min(pool, key=lambda d: (d.drops, d.idx)), "no judge"
        elif len(pool) > 1 and st["prefix"] and time.time() - t0 < JUDGE_BUDGET:
            # in draft order: the 9B judge leans to draft 1 (the cold one). It agreed with a Claude grader 57%
            # of the time this way vs 34% with the order shuffled (chance ~40%), so the lean is kept on purpose.
            k = await judge(st["prefix"], q, sender, st["brief"] or Brief(), [d.text for d in pool])
            pick, how = (pool[k], "judge") if k is not None else (pool[0], "judge failed")
        elif len(pool) > 1:
            how = "out of time"
        log.info("picked draft %d of %d (%s, %d eligible) in %.0fs", pick.idx + 1, len(drafts), how, len(pool),
                 time.time() - t0)
        _trace(st, q, sender, route, drafts, [d.idx for d in pool], pick.idx, how, t0)
        return undodge(st, pick.text, last_reply, prior_q), None
    finally:
        _FULL_INFLIGHT[0] -= full
        chat.REGEN_OK.reset(rtok)
        chat.OPTIONS_OVERRIDE.reset(otok)
        STATE.reset(tok)
