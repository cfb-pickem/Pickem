"""
Helpful first (Charlie, 2026-11-15: answer the question, then the voice, then the roasts). Code rules from
Brian's web chat (2026-11-18 night), where Picky:

1. ducked a data ask with a roast: "What are all 8 teams that we are picking this week records against the
   spread this season" -> "Just make sure you don't let these numbers distract you from your habit of picking
   losers like you did with North Texas last week." `no_answer` + `gap_reply`: a data ask whose reply carries
   no number and no plain "I don't have it" gets an honest line instead: what he can't give + what he can.
2. read "I won't just give me the numbers" (he meant "won't you") as banter -> "Yep." `carry`: a short
   insist-follow-up re-asks the sender's last real question.
3. answered "Are you coded to be nicer to me than everyone else... you're not as mean as I expected" with
   "Can't argue with that." `tone_ask` / `tone_answer`: a real answer, in code.
4. `keep_full`: a data ask or a question-shaped text without a "?" never drops from the full brain to the
   banter fast path (the brief called #2 and #3 banter).

Run the tests: ./.venv/bin/python3 -I test_answer_first.py
"""
from __future__ import annotations

import re

# --- 1. a data ask that got no data ---------------------------------------------------------------------------

DATA_ASK = re.compile(
    r"\b(records?|numbers?|stats?|statistics|odds|percent(age)?s?|averages?|splits?|rankings?|ranks?|ATS|"
    r"against the (spread|number|line)|how many|totals?)\b", re.I)
_ASKING = re.compile(r"\?|^\W*(what|whats|what's|how|which|who|give|tell|show|list|can|could|will|would|do|does|"
                     r"is|are|any)\b|\b(give|tell|show|send|list|need|want) (me|us)\b", re.I)
# the reply already says plainly it can't give it (any honest gap counts; the model's own wording is fine)
_GAP = re.compile(r"\b(i )?(don'?t|do not|can'?t|cannot|won'?t) (have|see|find|get|give|pull|track)\b|\bnot (in|on) "
                  r"(my|the) (data|sheet|cards?|numbers)\b|\bno (data|numbers|record|stats)\b|\bkeeps? .{0,20} secret\b"
                  r"|\bnot (tracked|available|loaded)\b", re.I)


def data_ask(q: str) -> bool:
    """A question that wants numbers back (records, ATS, odds, stats, how many...)."""
    t = q or ""
    return bool(DATA_ASK.search(t) and _ASKING.search(t))


_NUM_WORD = re.compile(r"\b(zero|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|none|half)\b", re.I)


# what a dodge looks like: a line aimed at the asker ('your habit of picking losers'), or a brush-off. A short
# answer about teams ('Vanderbilt, by a mile.', 'Ohio State is undefeated.') isn't one.
_AT_THEM = re.compile(r"\b(you|your|you'?re|ya|u|ur)\b", re.I)
_BRUSH = re.compile(r"\b(next question|nice try|not telling|figure it out|look it up|google it|ask (someone|somebody) "
                    r"else|not my job|does it matter|who cares)\b", re.I)


def no_answer(reply: str, q: str) -> bool:
    """True when a data ask got a dodge: no number, no plain 'I don't have that', and the reply is a line at the
    asker or a brush-off."""
    r = (reply or "").replace("\u2019", "'")
    if not data_ask(q) or re.search(r"\d", r) or _NUM_WORD.search(r) or _GAP.search(r):
        return False
    return bool(_AT_THEM.search(r) or _BRUSH.search(r) or not r.strip())


_THING = ((re.compile(r"\bATS\b|against the (spread|number|line)|\bcover(s|ing)? record", re.I),
           "season records against the spread"),
          (re.compile(r"\bodds\b|\bpercent", re.I), "those odds"),
          (re.compile(r"\bstats?\b|\bstatistics\b|\baverages?\b", re.I), "those stats"),
          (re.compile(r"\brank", re.I), "those rankings"),
          (re.compile(r"\brecords?\b", re.I), "those records"))


def gap_reply(q: str, group: bool = False) -> str:
    """Plain about the gap, then what he can give. In the group his sides are hidden till the lock, so the offer
    there is the games and lines, not his read."""
    thing = next((say for rx, say in _THING if rx.search(q or "")), "those numbers")
    offer = ("this week's games and their lines" if group
             else "this week's games with the lines and my read on each")
    lines = [f"I don't have {thing} in front of me right now, and I'm not going to make them up. "
             f"What I can give you is {offer}. Want that?",
             f"Straight answer: {thing} aren't in what I can see right now, so I won't guess. "
             f"I've got {offer} if that helps.",
             f"Can't give you {thing}, I don't have them, and a made-up number is worse than none. "
             f"I can do {offer} though."]
    return lines[sum(map(ord, q or "")) % len(lines)]


# --- 2. 'just give me the numbers': re-ask the last real question ---------------------------------------------

INSIST = re.compile(
    r"\b(just|pls|please|come on|c'?mon)\b[^?.!]{0,25}\b(give|tell|show|send|answer)\b"
    r"|\b(won'?t|wont|will|can|could|would) (you|u|ya)\b[^?.!]{0,15}\b(give|tell|show|answer)\b"
    r"|\b(give|tell|show) (me|us) the (numbers|stats|records|answer|data)\b"
    r"|\b(the|those) (numbers|stats|records)\b[^?.!]{0,10}(\?|please|pls)"
    r"|\banswer (the|my) question\b|\byou (didn'?t|did not|never) answer\b|\bthat'?s not what i asked\b"
    r"|\bi asked (you )?(for|about|a question)\b|\bso what are they\b|^\W*(numbers|stats|answer)\W*(please|pls)?\W*$",
    re.I)
# an insist carries no new topic: every word is one of these ('just tell me who to pick' is a new question)
_INSIST_WORDS = set("""i i'm im won't wont will can could would you u ya just please pls plz come on c'mon cmon give
tell show send answer me us the those these numbers number stats records data question my so what are they didn't
didnt did not never that's thats that asked ask for about a bro dude man picky already them it now ok okay again
still real actual hey yo dammit damn""".split())


def insist(q: str) -> bool:
    """'I won't just give me the numbers' (Brian, for 'won't you just'), 'answer the question', 'numbers?'."""
    words = re.findall(r"[a-z']+", (q or "").lower().replace("\u2019", "'"))
    return bool(INSIST.search((q or "").replace("\u2019", "'"))) and 0 < len(words) <= 12 and \
        all(w in _INSIST_WORDS for w in words)


def carry(q: str, prior_user: list[str], is_chatter) -> str | None:
    """The question to answer for an insist follow-up: the sender's latest earlier text that isn't itself an
    insist and isn't chatter (`is_chatter(text)` = the router sends it to banter / ignore / jailbreak, unless
    it's a data ask). None = answer `q` as it is."""
    if not insist(q):
        return None
    for p in reversed(prior_user or []):
        p = str(p or "").strip()
        if not p or insist(p):
            continue
        return p if (data_ask(p) or not is_chatter(p)) else None
    return None


# --- 3. 'are you coded to be nicer to me?' ----------------------------------------------------------------------

TONE_ASK = re.compile(
    r"\b(are|r) (you|u) (coded|programmed|told|set up|designed|built|made|trained|instructed|supposed)\b"
    r"[^?.!]{0,30}\b(nice|nicer|easy|easier|soft|softer|mean|meaner|harder|rougher|kind|kinder)\b"
    r"|\bwhy (are|r|is|were)(n'?t)? (you|u|picky)\b[^?.!]{0,15}\b(so |being )?(nice|nicer|mean|meaner|soft|easy|kind)\b"
    r"[^?.!]{0,10}\b(to|on|with) me\b"
    r"|\bnot (as|that|so) (mean|savage|harsh|rough|brutal)( to me)? as (i )?(expected|thought|heard)\b"
    r"|\b(going|go|went) (easy|soft) on me\b"
    r"|\b(nicer|meaner|easier|softer|kinder) (to|on|with) me than\b", re.I)


def tone_ask(q: str) -> bool:
    return bool(TONE_ASK.search(q or ""))


def tone_answer(q: str, stance: str, charlie: bool = False) -> str:
    """A true answer: no member has a special setting except the commissioner's always-friendly one; Picky matches
    how each member has been talking to him (member memory's tone read: cold / neutral / warm)."""
    if charlie:
        return "Yep, you get the friendly version. Somebody has to keep the commissioner happy."
    if stance == "cold":
        return ("Nope, nothing special for you. I match the energy I get, and you've been chirping, "
                "so don't get comfortable.")
    if stance == "warm":
        return ("Nope, nothing special for you. I match the energy I get, and you've been decent to me, so you "
                "get the decent version. Keep it that way.")
    return ("Nope, nothing special for you. I match the energy I get: come at me and I'll come back, ask me "
            "something real and you get a real answer.")


# --- 4. never banter a question ---------------------------------------------------------------------------------

_Q_START = re.compile(r"^\W*(are|is|do|does|did|can|could|will|would|should|why|what|whats|what's|who|how|when|"
                      r"where|which|won'?t you|wont you)\b", re.I)


def keep_full(q: str) -> bool:
    """The brief may not demote this text to the banter fast path: a data ask, an insist follow-up, a tone
    question, or a question shape with no '?' ('Are you coded to be nicer to me...')."""
    t = q or ""
    return data_ask(t) or insist(t) or tone_ask(t) or bool(_Q_START.search(t))
