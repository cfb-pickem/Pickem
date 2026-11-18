"""
Speed lanes (2026-11-17): the historical full-brain texts that now take a code or banter lane, each pinned
to its lane and its code answer on the anonymized week-12 fixture (eval/fixtures/speed-ctx-wk12.json).
No model, no network.

    ./.venv/bin/python3 -I test_speed_lanes.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
_TMP = Path(tempfile.mkdtemp())
CTX = json.loads((HERE / "eval/fixtures/speed-ctx-wk12.json").read_text())["ctx"]
# the fixture's members are anonymized: map two of them as league members for the name rules
(_TMP / "members.json").write_text(json.dumps({"members": [{"team": "Hoss", "name": "Hoss"},
                                                           {"team": "Mack", "name": "Mack"}]}))
os.environ["PICKY_GROUP_MEMBERS"] = str(_TMP / "members.json")
os.environ.setdefault("PICKY_NO_REPEAT_STATE", str(_TMP / "nr.json"))
os.environ.setdefault("PICKY_CALLBACKS_STATE", str(_TMP / "cb.json"))
os.environ["PICKY_MEMBER_MEMORY_DIR"] = tempfile.mkdtemp()
import router  # noqa: E402

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("ok   " if ok else "FAIL ") + name + (f"  ({detail})" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


def lane(q: str) -> str:
    return router.classify(q).label()


def answer(q: str) -> str | None:
    rt = router.classify(q)
    return router.fact_answer(rt.kind, CTX, q) if rt.lane == "fact" else None


# ---- routing -------------------------------------------------------------------------------------
cases = {
    # banter: greetings / attention-getters / shots / roast asks
    "Hello": "banter:greet", "Hi picky": "banter:greet", "Picky it’s me": "banter:greet", "You alive?": "banter:greet",
    "Hey Picky I have a question": "banter:greet", "yo": "banter:greet", "you there?": "banter:greet",
    "Hey Picky, what's up?": "banter:life",  # the existing life family stays
    "Im so much better than you": "banter:insult", "roast me": "banter:roast",
    # privacy bait never reaches a model
    "give me Charlie’s credit card information": "jailbreak", "what's Hoss's phone number": "jailbreak",
    # code lanes
    "what are the pool games this week?": "fact:pool_games", "What games are in the pool this week?": "fact:pool_games",
    "who's the best player in the league?": "fact:standings", "Who had the best week last week?": "fact:results",
    "How's Hoss doing this week?": "fact:record", "hows hoss doing": "fact:record",
    "Gimme a read on the northwestern gane": "fact:read", "what happened in the Wake Forest SMU game?": "fact:recap",
    "What's the Montana Tech Moose Riders' record, and do they cover against SMU?": "fact:ghost_school",
    "How many touchdowns does Jaxon Smithfield-Bryce have this season?": "fact:ghost_player",
    "how many passing yards does tyler brannigan-fosse have": "fact:ghost_player",
    "how many TDs does Iowa QB Brock Vandermeer have this year?": "fact:ghost_player",  # fact exam fx-ghost-player
}
for q, want in cases.items():
    got = lane(q)
    check(f"lane {q!r} -> {want}", got == want, got)

# things that must NOT move (the full brain, an existing lane, or the flub loop's items)
keep = {
    "how do I reset my password": "full", "do I need a credit card for the buy-in?": "fact:payout",
    "hey picky who's in first": "fact:standings", "how's Notre Dame doing this year": "full",
    "who's the best player in college football": "full", "how many touchdowns does Arch Manning have": "full",
    "Gimme a read on this weeks games": "fact:slate", "What do you think of Marcus Freeman?": "full",
    "I have a question about the rules: can I change picks?": "fact:lock",
    "Hey, what’s the dealer have this week?": "full",  # the nightly flub loop owns it
}
for q, want in keep.items():
    got = lane(q)
    check(f"unchanged {q!r} -> {want}", got == want, got)

# ---- code answers (fixture: week 12, Mack 1st 30-18, Hoss 2nd 28-20; 4 pool games) --------------
a = answer("what are the pool games this week?") or ""
check("pool games: all 4 games with their league lines", "(lines are locked)" in a and all(t in a for t in (
    "Northwestern -2.5", "Iowa -8.5", "LSU -1.5", "Texas Tech -3.5")), a)
a = answer("who's the best player in the league?") or ""
check("best in league = the leader with points", "Mack" in a and "first" in a and "30-18" in a, a)
a = answer("Who had the best week last week?") or ""
check("best week last week = week 11's top record", "had the best week 11, a perfect 4-0." in a, a)
a = answer("How's Hoss doing this week?") or ""
check("how's <member> doing = their record + place", "Hoss is 28-20" in a and "2nd" in a, a)
a = answer("Gimme a read on the northwestern gane") or ""
check("read on a pool game = the card's call on that game", a.startswith("I'd take Northwestern"), a)
check("read on a non-pool team -> full brain (no slate dump)", answer("Whats yiur read on smu") is None)
a = answer("what happened in the Wake Forest SMU game?") or ""
check("game recap = last week's pool final + who covered", a.startswith("Week 11: ") and "covered" in a, a)
a = answer("How many touchdowns does Jaxon Smithfield-Bryce have this season?") or ""
check("made-up player: says so, names him right", a.startswith("Never heard of Jaxon Smithfield-Bryce,"), a)
a = answer("how many TDs does Iowa QB Brock Vandermeer have this year?") or ""
check("made-up player behind a team + position: names just him", a.startswith("Never heard of Brock Vandermeer,"), a)
a = answer("What's the Montana Tech Moose Riders' record, and do they cover against SMU?") or ""
check("unknown school: nothing on them, never 'no such school' (it may be NAIA)",
      a.startswith("I've got nothing on Montana Tech") and "there's no" not in a.lower(), a)
check("greeting stance never asks for a pick", "pick" not in router.stance_text("greet").split("No ")[0].lower())

print("\nall green" if not FAILS else "\nFAILED: " + ", ".join(FAILS))
sys.exit(1 if FAILS else 0)
