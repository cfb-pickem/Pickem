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

import contextvars
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field

import httpx

import banter
import chat

log = logging.getLogger("picky-brain")

# Charlie 2026-11-15: drop the best-of-3 judge (57% agreement, leaned to draft 1). One draft, plus a
# reroll only when the filters cut it. PICKY_JUDGE=1 brings back 3 drafts + judge.
USE_JUDGE = os.environ.get("PICKY_JUDGE") == "1"
N_DRAFTS = 3 if USE_JUDGE else 1
DRAFT_BUDGET = 105.0   # seconds: no new draft starts after this
JUDGE_BUDGET = 170.0   # seconds: no judge pass after this (the watcher waits 240)

# draft i: sampling and angle. Draft 0 is the old single answer (temp 0.2); 1-2 run hot with min_p
# (Qwen3.5's card suggests ~0.7+ for chat; the filters and the judge guard the facts).
SAMPLING = [{"temperature": 0.2}, {"temperature": 0.8, "min_p": 0.05, "top_p": 1.0},
            {"temperature": 0.9, "min_p": 0.05, "top_p": 1.0}, {"temperature": 0.4}]
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


def brief_rider(b: Brief, core: bool = True) -> str:
    if not b.ok():
        return ""
    L = ["YOUR READ OF THIS TEXT (your own private notes: use them, never quote or mention them):",
         f"- They want: {b.asking}",
         f"- It's {_INTENT_WORDS.get(b.intent, b.intent)}" + (f", about {b.about}" if b.about else "")
         + f"; mood: {b.tone}."]
    if b.facts_needed:
        L.append("- Facts it needs (take them only from LEAGUE DATA and the blocks above): " + "; ".join(b.facts_needed))
    if b.answer and core:
        L.append(f"- Core of the reply (say it your own way, don't copy this wording): {b.answer}")
    if b.intent == "insult":
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


def understand_user(question: str, sender: str, recent: str, last_reply: str) -> str:
    L = [f"Sent by: {sender}" if sender else "Sent by: a league member"]
    if recent:
        L.append(recent.strip())
    if last_reply:
        L.append(f"Your last reply to them: {last_reply[:300]}")
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


async def _json_call(prefix: str, tail: str, user: str, schema: dict, temperature: float,
                     num_predict: int, timeout: float) -> str:
    """One non-streamed Ollama call that shares the prefix checkpoint. num_ctx
    must match chat.OLLAMA_OPTIONS or Ollama reloads the model."""
    payload = {"model": chat.MODEL, "stream": False, "format": schema,
               "messages": [{"role": "system", "content": prefix + tail}, {"role": "user", "content": user}],
               "options": {**chat.OLLAMA_OPTIONS, "temperature": temperature, "num_predict": num_predict},
               "keep_alive": chat.OLLAMA_KEEP_ALIVE}
    if chat.OLLAMA_THINK is not None:
        payload["think"] = chat.OLLAMA_THINK == "true"
    async with httpx.AsyncClient(timeout=httpx.Timeout(connect=5.0, read=timeout, write=10.0, pool=5.0)) as c:
        await chat._prime(c, prefix)
        r = await c.post(f"{chat.OLLAMA}/api/chat", json=payload)
        r.raise_for_status()
        d = r.json()
    log.info("brain call: prompt eval %.1fs (%s tokens), %s out, total %.1fs",
             (d.get("prompt_eval_duration") or 0) / 1e9, d.get("prompt_eval_count"), d.get("eval_count"),
             (d.get("total_duration") or 0) / 1e9)
    return (d.get("message") or {}).get("content") or ""


async def understand(prefix: str, question: str, sender: str, recent: str, last_reply: str = "",
                     timeout: float = 60.0) -> Brief:
    try:
        raw = await _json_call(prefix, UNDERSTAND_TAIL, understand_user(question, sender, recent, last_reply),
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
        if subj and any(r not in sets[subj] for r in recs):
            bad.append(sent)
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
# hooks into chat.py (installed by group_chat; inert unless STATE is set)
# -----------------------------------------------------------------------------

# One dict per text being answered. A dict, not values: chat.py's stream runs
# in a task with a copy of this context, and the hook fills in brief/prefix.
STATE: contextvars.ContextVar[dict | None] = contextvars.ContextVar("picky_brain_state", default=None)

_orig_ollama_stream = chat._ollama_stream
_orig_prompt_prefix = chat.prompt_prefix


def _prompt_prefix(ctx: dict) -> str:
    st = STATE.get()
    if st is not None:
        st["ctx"] = ctx
    return _orig_prompt_prefix(ctx)


async def _ollama_stream(messages: list[dict], prefix: str | None = None):
    st = STATE.get()
    if st is None:
        async for x in _orig_ollama_stream(messages, prefix):
            yield x
        return
    if prefix and st.get("brief") is None:
        st["prefix"] = prefix
        st["brief"] = await understand(prefix, st["q"], st.get("sender", ""), st.get("recent", ""),
                                       st.get("last_reply", ""))
        st["ammo"] = banter.ammo_rider(st["q"], st["brief"].intent)
        if st["ammo"]:
            log.info("ammo: %s", st["ammo"].split("\n", 1)[1].replace("\n", " | ")[:300])
    i = min(st.get("idx", 0), len(SAMPLING) - 1)
    # different inputs per draft, or the 9B writes the same reply three times: draft 1 the brief,
    # drafts 2-3 the brief plus the ammo and an angle, sampled hotter
    b = st.get("brief") or Brief()
    rider = "\n\n".join(x for x in (brief_rider(b), st.get("ammo") or "" if i in (1, 2) else "", ANGLES[i]) if x)
    st["options"].clear()
    st["options"].update(SAMPLING[i])  # read by chat._ollama_stream via OPTIONS_OVERRIDE
    async for x in _orig_ollama_stream(insert_rider(messages, rider, st["q"]), prefix):
        yield x


def install() -> None:
    chat._ollama_stream = _ollama_stream
    chat.prompt_prefix = _prompt_prefix


# -----------------------------------------------------------------------------
# the pipeline
# -----------------------------------------------------------------------------

async def run(draft, *, q: str, sender: str = "", team: str | None = None, recent: str = "", last_reply: str = "",
              recent_picky: list[str] | None = None, t0: float | None = None) -> tuple[str, str | None]:
    """(reply, image). `draft()` runs chat.py's pipeline once and returns
    (shaped reply, sentences the filters dropped, image or None)."""
    t0 = t0 or time.time()
    st = {"q": q, "sender": sender, "team": team, "recent": recent, "last_reply": last_reply, "brief": None, "ammo": None,
          "prefix": None, "idx": 0, "options": dict(SAMPLING[0])}
    tok, otok = STATE.set(st), chat.OPTIONS_OVERRIDE.set(st["options"])
    try:
        drafts: list[Draft] = []
        for i in range(N_DRAFTS + 1):
            if i == N_DRAFTS and (not drafts or any(d.drops == 0 and d.text.strip() for d in drafts)):
                break  # the extra draft only when every draft so far was cut by the filters
            if i and time.time() - t0 > DRAFT_BUDGET:
                break
            st["idx"] = i
            try:
                text, drops, image = await draft()
            except Exception:
                if not drafts:
                    raise
                log.warning("draft %d failed; judging the %d we have", i + 1, len(drafts), exc_info=True)
                break
            if image:
                return text, image
            stale = stale_records(text, st.get("ctx"), st.get("team"))
            if stale:
                log.warning("draft %d: stale record %r", i + 1, stale[0][:160])
            drafts.append(Draft(text, drops + len(stale), i))
            log.info("draft %d (%.0fs): drops=%d %r", i + 1, time.time() - t0, drops, text[:160])
        pool = eligible(drafts, recent_picky or [])
        if not pool:
            return (drafts[0].text if drafts else ""), None
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
        if TRACE:
            with open(TRACE, "a") as f:
                f.write(json.dumps({"q": q, "sender": sender, "brief": (st["brief"] or Brief()).__dict__,
                                    "drafts": [d.__dict__ for d in drafts], "eligible": [d.idx for d in pool],
                                    "pick": pick.idx, "how": how, "secs": round(time.time() - t0, 1)}) + "\n")
        return pick.text, None
    finally:
        chat.OPTIONS_OVERRIDE.reset(otok)
        STATE.reset(tok)
