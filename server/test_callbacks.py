"""
Picky's callbacks (server/callbacks.py): shot logging, lean extraction from his
sent replies, ATS grading against fixtures, DM-only privacy, never an
unrevealed pick, cooldown (+ no_repeat Ledger when present), and that the
group_chat.py hook can't break or change a reply. No model, no network.

Run:  ./.venv/bin/python3 -I test_callbacks.py
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
import sys
import tempfile
import threading
import types
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
TMP = Path(tempfile.mkdtemp())
os.environ["PICKY_CALLBACKS_STATE"] = str(TMP / "callbacks.json")
os.environ["PICKY_NO_REPEAT_STATE"] = str(TMP / "no_repeat.json")  # never the real ledger; cleared by reset()
import callbacks as cb  # noqa: E402

cb.GRADE_EVERY = float("inf")  # no background grading threads racing the fixtures
fails = 0


def check(cond, msg):
    global fails
    print(("ok   " if cond else "FAIL ") + msg)
    fails += not cond


def ts(s: str) -> float:
    return dt.datetime.fromisoformat(s).replace(tzinfo=ZoneInfo("America/Chicago")).timestamp()


def reset():
    for p in (cb.state_path(), TMP / "no_repeat.json"):
        if p.exists():
            p.unlink()


# --- fixtures: week 11 (Sat 11/14) and week 12 (Sat 11/21) -------------------
def G(gid, week, away, home, line, start, ap=None, hp=None, winner=None, tb=False):
    return {"id": gid, "week": week, "away": away, "home": home, "line": line, "start": start,
            "away_pts": ap, "home_pts": hp, "winner": winner, "status": "final" if winner else None, "tiebreaker": tb}


W11_OPEN = [G(101, 11, "Oregon State", "Boise State", -13.5, "2026-11-14T14:30:00"),
            G(102, 11, "North Texas", "UTSA", -2.5, "2026-11-14T11:00:00"),
            G(103, 11, "Texas A&M", "Oregon", -3, "2026-11-14T18:30:00"),
            G(104, 11, "SMU", "Navy", 7, "2026-11-14T11:00:00")]
W11_FINAL = [G(101, 11, "Oregon State", "Boise State", -13.5, "2026-11-14T14:30:00", 20, 30, "Oregon State"),
             G(102, 11, "North Texas", "UTSA", -2.5, "2026-11-14T11:00:00", 10, 14, "UTSA"),
             G(103, 11, "Texas A&M", "Oregon", -3, "2026-11-14T18:30:00", 20, 23, "Texas A&M"),  # push -> away
             G(104, 11, "SMU", "Navy", 7, "2026-11-14T11:00:00", 31, 14, "SMU"),
             G(105, 11, "Army", "Navy", 0, "2026-11-14T13:00:00", 10, 7, "Army", tb=True)]
W12 = [G(201, 12, "Army", "Tulane", -6.5, "2026-11-21T11:00:00"),
       G(202, 12, "Memphis", "USF", 3.5, "2026-11-21T14:30:00")]
PICKS = {101: {"Ackbar": {"pick": "Boise State", "jackpot": False}, "TG": {"pick": "Oregon State", "jackpot": False}},
         102: {"Ackbar": {"pick": "North Texas", "jackpot": False}, "TG": {"pick": "UTSA", "jackpot": False}},
         103: {"Ackbar": {"pick": "Oregon", "jackpot": False}, "TG": {"pick": "Texas A&M", "jackpot": False}},
         104: {"Ackbar": {"pick": "SMU", "jackpot": False}, "TG": {"pick": "Navy", "jackpot": False}},
         201: {"Ackbar": {"pick": "Tulane", "jackpot": False}}}  # an UNREVEALED pick: must never surface
STATE = {"games": W11_OPEN + W12}
cb.SOURCE = lambda need: cb.League(STATE["games"], PICKS)
GROUP, DM_ACK, DM_OTHER = "g-league", "any;-;+15555550100", "any;-;+15555550199"

# --- shots -------------------------------------------------------------------
for t, want in [("you suck", True), ("@Picky you suck", True), ("Pussy", True), ("picky is a fraud", True),
                ("your picks are trash", True), ("useless bot", True), ("are you a bot", True),
                ("go suck my dick, bitch", True), ("you're 0-3 lol", True), ("u r trash", True),
                ("you think Boise is trash?", False), ("roast Ackbar he sucks", False), ("who's winning", False),
                ("you are not bad", False), ("what's the line on Boise", False)]:
    check(cb.is_shot(t) is want, f"is_shot({t!r}) = {want}")

reset()
s = cb.log_shot(GROUP, "Ackbar", "you suck", ts=ts("2026-11-14T10:00:00"))
check(s and s["scope"] == "group:" + GROUP and s["team"] == "Ackbar", "log_shot stores chat scope, team, text")
check(cb.log_shot("", "Ackbar", "you suck", is_dm=True) is None, "a DM with no chat id can't be scoped: not logged")
check(cb.log_shot(GROUP, None, "you suck") is None, "no team (unmapped sender): not logged")
check(oct(cb.state_path().stat().st_mode & 0o777) == "0o600", "state file is 0600")

with patch.dict(os.environ, {}, clear=False):
    os.environ.pop("PICKY_CALLBACKS_STATE")
    with patch.dict(os.environ, {"PICKY_GROUP_PORT": "8788"}):
        check(not cb.enabled(), "no state override + side-sidecar port: disabled (live log never polluted)")
    with patch.dict(os.environ, {"PICKY_GROUP_EXAM": "1"}):
        check(not cb.enabled(), "no state override + exam flag: disabled")
    with patch.dict(os.environ, {"PICKY_GROUP_PORT": "8768"}):
        check(cb.enabled() and cb.state_path() == cb.DEFAULT_STATE, "live sidecar: enabled, private default path")
os.environ["PICKY_CALLBACKS_STATE"] = str(TMP / "callbacks.json")
check(str(cb.DEFAULT_STATE).startswith("/Users/Agent/cfb-pickem-private/"), "default state lives in cfb-pickem-private")

# --- extraction --------------------------------------------------------------
SL = W11_OPEN
for reply, want in [
        ("I'd take Boise -13.5.", [("Boise State", False)]),
        ("Boise -13.5 is a trap.", [("Oregon State", True)]),
        ("Stay away from UTSA, North Texas is live.", [("North Texas", True)]),
        ("Take Oregon State +13.5 over Boise.", [("Oregon State", False)]),
        ("Texas A&M +3 is the play.", [("Texas A&M", False)]),
        ("UTSA all day, and I like Oregon St plus the points.", [("Oregon State", False), ("UTSA", False)]),
        ("Ackbar took Boise last week and it missed.", []),
        ("Would you take Boise?", []),
        ("I like the Aggies.", []),  # nicknames aren't guessed
        ("I'd take Boise. Actually, fade Boise.", []),  # both sides: no lean
        ("You're 1-3, I'm 0-3, nobody's taking anything.", [])]:
    got = sorted((x["side"], x["fade"]) for x in cb.extract_leans(reply, SL))
    check(got == sorted(want), f"extract {reply!r} -> {want} (got {got})")
check(cb.extract_leans("I'd take Boise -13.5.", W11_FINAL) == [], "no lean logged on a game that's already final")
check(cb.extract_leans("Oregon is the play.", [G(9, 1, "Oregon", "Oregon State", -1, "2026-11-14T11:00:00")])[0]["side"]
      == "Oregon", "'Oregon' never matches Oregon State when both play")

# --- after_reply logging (no ctx: slate from SOURCE; ctx: games_this_week) -----
reset()
STATE["games"] = W11_OPEN + W12
thu = ts("2026-11-12T20:00:00")
r1 = cb.after_reply(GROUP, "Ackbar", "@Picky who do I take in Boise",
                    "Boise -13.5 is a trap. Take Oregon State +13.5 and don't look back.", ts=thu)
r2 = cb.after_reply(DM_ACK, "Ackbar", "UTSA or North Texas?", "I'd take UTSA -2.5.", is_dm=True, ts=thu)
ctx = {"current_week": 11, "games_this_week": [{k: g[k] for k in ("id", "away", "home", "line", "winner", "start",
                                                                  "tiebreaker")} for g in W11_OPEN]}
r3 = cb.after_reply(GROUP, "TG", "you suck, who do I take", "Easy, I'd take Oregon -3.", ctx=ctx, ts=thu)
r4 = cb.after_reply(GROUP, "Ackbar", "@Picky you're trash", "Bold from a guy who took SMU.",
                    ts=ts("2026-11-14T10:00:00"))
r5 = cb.after_reply(GROUP, None, "you suck", "I'd take Boise -13.5.", ts=thu)
d = cb._load()
check(r1["leans"] == 1 and r2["leans"] == 1 and r3["leans"] == 1, "after_reply logs one lean per reply (SOURCE and ctx)")
check(r3["shot"] and r4["shot"] and not r1["shot"], "after_reply logs shots only when the text was one")
check(r5 == {"shot": False, "leans": 0, "graded": 0}, "unmapped sender: nothing logged")
lean = next(x for x in d["leans"] if x["game_id"] == 101)
check(lean["side"] == "Oregon State" and lean["fade"] and lean["line"] == 13.5 and lean["scope"] == "group:" + GROUP,
      "fade lean stored as the other side on the pool line (+13.5), with its chat")
check(next(x for x in d["leans"] if x["game_id"] == 102)["scope"] == "dm:" + DM_ACK, "DM lean stored with its DM scope")
check(all(x["result"] is None for x in d["leans"]), "nothing graded before the games are final")
cb.log_lean(GROUP, "Ackbar", 101, "Boise State", -13.5, ts=thu + 60)
d = cb._load()
check([x["side"] for x in d["leans"] if x["game_id"] == 101 and x["team"] == "Ackbar"] == ["Boise State"],
      "a newer lean on the same game replaces the old one (he changed his mind)")
cb.log_lean(GROUP, "Ackbar", 101, "Oregon State", 13.5, ts=thu, fade=True)  # put it back

# --- grading ------------------------------------------------------------------
mon = ts("2026-11-16T12:00:00")
check(cb.callback_for(GROUP, "Ackbar", False, now=mon, mark=False) is None, "no callback before grading")
STATE["games"] = W11_FINAL + W12
n = cb.grade_pending(now=mon)
d = cb._load()
res = {(x["team"], x["game_id"]): x["result"] for x in d["leans"]}
check(n >= 4, f"grade_pending graded the leans and shots ({n})")
check(res[("Ackbar", 101)]["covered"] and res[("Ackbar", 101)]["margin"] == 3.5,
      "Oregon State +13.5 covered by 3.5 (Boise won by 10)")
check(res[("Ackbar", 102)]["covered"] and res[("Ackbar", 102)]["margin"] == 1.5, "UTSA -2.5 covered by 1.5")
check(not res[("TG", 103)]["covered"] and res[("TG", 103)]["margin"] == 0, "Oregon -3 on a 3-point win: push to away")
check(res[("Ackbar", 101)]["member_pick"] == "Boise State", "member's pick on a FINAL game rides along")
shots = {s["text"]: s["outcome"] for s in d["shots"]}
check(shots["@Picky you're trash"] == {"week": 11, "w": 1, "l": 3, "done": shots["@Picky you're trash"]["done"]},
      "Ackbar's shot Saturday morning -> week 11, 1-3 against the number (tiebreaker game excluded)")
check(shots["you suck, who do I take"]["w"] == 3, "TG's next week was 3-1")

lines = dict(cb.candidates(GROUP, "Ackbar", False, now=mon))
check("told you Boise State -13.5 was a trap, it missed the cover by 3.5. you went the other way. should've listened."
      in lines.values(), "fade lean that hit: 'told you Boise State -13.5 was a trap', margin, member took Boise")
tg = [ln for _, ln in cb.candidates(GROUP, "TG", False, now=mon)]
check(tg == ["my bad on Oregon -3, missed the cover on the push (pushes go to the away side). "
             "you faded me and got it right."], f"owns the miss, push explained, no shot line on a 3-1 week ({tg})")
sl = cb.callback_for(GROUP, "Ackbar", False, text="you suck", now=mon, mark=False)
check(sl == "you said that Saturday too, then went 1-3 against the number in week 11.",
      f"repeat shot -> 'you said that Saturday too, then went 1-3' ({sl})")
check(cb.similar_shot("you suck", "@Picky you're trash") and not cb.similar_shot("are you a bot", "you suck"),
      "a plain insult repeats a plain insult; a bot crack doesn't")
check(cb.callback_for(GROUP, "Ackbar", False, text="how's my week looking", now=mon, mark=False).startswith("told you"),
      "a non-shot text prefers the lean callback")
check(cb.shot_line({"ts": ts("2026-11-14T10:00:00"), "text": "x", "outcome": {"week": 11, "w": 1, "l": 3}},
                   ts("2026-11-21T12:00:00"), "how's it going").startswith("last time you came at me (last Saturday)"),
      "an older, different shot: 'last time you came at me (last Saturday)'")

# --- privacy -------------------------------------------------------------------
grp = [ln for _, ln in cb.candidates(GROUP, "Ackbar", False, now=mon)]
check(not any("UTSA" in ln for ln in grp), "the DM lean (UTSA) never surfaces in the group")
dm = [ln for _, ln in cb.candidates(DM_ACK, "Ackbar", True, now=mon)]
check(any("UTSA" in ln for ln in dm) and any("Boise" in ln for ln in dm), "in that DM: DM lean AND group lean callable")
oth = [ln for _, ln in cb.candidates(DM_OTHER, "Ackbar", True, now=mon)]
check(not any("UTSA" in ln for ln in oth) and any("Boise" in ln for ln in oth), "another DM: group material only")
check(cb.candidates(GROUP, "TG", False, now=mon) and not any("Boise" in ln for _, ln in cb.candidates(GROUP, "TG", False, now=mon)),
      "one member's leans never surface for another member")
lg = cb.SOURCE(set())
check(lg.pick(201, "Ackbar") is None, "League.pick never returns a pick on a game that isn't final (unrevealed)")
cb.log_lean(GROUP, "Ackbar", 201, "Tulane", -6.5, ts=mon)
cb.grade_pending(now=mon)
allc = " ".join(ln for _, ln in cb.candidates(GROUP, "Ackbar", False, now=mon)
                + cb.candidates(DM_ACK, "Ackbar", True, now=mon))
check("Tulane" not in allc and "Army" not in allc, "a lean on an unplayed game (and Ackbar's hidden pick) never surfaces")
dm_shot = cb.log_shot(DM_ACK, "TG", "you suck", ts=ts("2026-11-14T10:05:00"), is_dm=True)
cb.grade_pending(now=mon)
check(not any("said that" in ln or "came at me" in ln for _, ln in cb.candidates(GROUP, "TG", False, text="you suck",
                                                                                     now=mon)),
      "a DM shot never surfaces in the group")

# --- cooldown ------------------------------------------------------------------
reset()
STATE["games"] = W11_OPEN + W12
cb.after_reply(GROUP, "Ackbar", "@Picky who", "Boise -13.5 is a trap.", ts=thu)
cb.after_reply(GROUP, "Ackbar", "@Picky you're trash", "ok.", ts=ts("2026-11-14T10:00:00"))
cb.after_reply(GROUP, "Ackbar", "@Picky you suck", "ok.", ts=ts("2026-11-14T10:30:00"))
STATE["games"] = W11_FINAL + W12
cb.grade_pending(now=mon)
peek = cb.callback_for(GROUP, "Ackbar", False, now=mon, mark=False)
c1 = cb.callback_for(GROUP, "Ackbar", False, now=mon)
c2 = cb.callback_for(GROUP, "Ackbar", False, now=mon)
c3 = cb.callback_for(GROUP, "Ackbar", False, now=mon)
check(peek == c1 and c1 and c2 and c1 != c2, "mark=False peeks; each call returns a different callback")
check(c3 is None, "two shots before the same week are ONE callback; all used -> None")
check(cb.callback_for(DM_ACK, "Ackbar", True, now=mon) is None, "a used group callback isn't reused in the DM either")
check(cb.callback_for(GROUP, "Ackbar", False, now=mon + 61 * 86400) is None, "stale material isn't served later")

# no_repeat Ledger (burn lane) vetoes and records when present
reset()
STATE["games"] = W11_OPEN + W12
cb.after_reply(GROUP, "Ackbar", "@Picky who", "Boise -13.5 is a trap.", ts=thu)
STATE["games"] = W11_FINAL + W12
cb.grade_pending(now=mon)
rec = []


class FakeLedger:
    veto = True

    def __init__(self, path=None):
        pass

    def is_repeat(self, text, target=None, chat_id=None):
        return "too close" if FakeLedger.veto else None

    def record(self, text, target=None, chat_id=None, angle=None, opener=None):
        rec.append((text, target, chat_id, angle))


with patch.dict(sys.modules, {"no_repeat": types.SimpleNamespace(Ledger=FakeLedger)}):
    check(cb.callback_for(GROUP, "Ackbar", False, now=mon) is None, "no_repeat Ledger veto -> no callback")
    FakeLedger.veto = False
    got = cb.callback_for(GROUP, "Ackbar", False, now=mon)
    check(got and rec and rec[-1][1] == "Ackbar" and rec[-1][3] == "callback", "served callback recorded in the Ledger")

# --- failure safety ----------------------------------------------------------------
errs = []
old_hook, threading.excepthook = threading.excepthook, lambda a: errs.append(a)
try:
    def bad_source(need):
        raise RuntimeError("supabase down")
    with patch.object(cb, "SOURCE", bad_source):
        r = cb.after_reply(GROUP, "Ackbar", "you suck", "I'd take Boise -13.5.", ts=thu)
        check(r["shot"] and r["leans"] == 0, "league read fails: the shot is still logged, no crash")
        t = cb.hook(GROUP, "Ackbar", "you suck", "fine.")
        t and t.join(5)
    with patch.object(cb, "after_reply", side_effect=RuntimeError("boom")):
        t = cb.hook(GROUP, "Ackbar", "you suck", "fine.")
        check(isinstance(t, threading.Thread), "hook runs after_reply in a background thread")
        t.join(5)
    with patch.object(cb.threading, "Thread", side_effect=RuntimeError("no threads")):
        check(cb.hook(GROUP, "Ackbar", "you suck", "fine.") is None, "hook swallows a failure to even start")
    check(not errs, "no exception escaped the hook thread")
    check(cb.hook(GROUP, None, "you suck", "fine.") is None and cb.hook(GROUP, "Ackbar", "x", "") is None,
          "hook skips unmapped senders and empty replies")
    with patch.object(cb, "_load", side_effect=RuntimeError("disk")):
        check(cb.callback_for(GROUP, "Ackbar", False) is None, "callback_for never raises (returns None)")
finally:
    threading.excepthook = old_hook

# --- policy: at most one per reply, forced on every 4th banter reply where one exists --------
def graded_ackbar():
    """Ackbar: a graded group fade lean (Boise), a DM lean (UTSA) and a shot before a 1-3 week."""
    reset()
    STATE["games"] = W11_OPEN + W12
    cb.after_reply(GROUP, "Ackbar", "@Picky who", "Boise -13.5 is a trap.", ts=thu)
    cb.after_reply(DM_ACK, "Ackbar", "UTSA?", "I'd take UTSA -2.5.", is_dm=True, ts=thu)
    cb.after_reply(GROUP, "Ackbar", "@Picky you're trash", "ok.", ts=ts("2026-11-14T10:00:00"))
    STATE["games"] = W11_FINAL + W12
    cb.grade_pending(now=mon)


graded_ackbar()
check(cb.force_every() == 4, "default: force every 4th banter reply")
seq = [cb.callback_for(GROUP, "Ackbar", False, text="lol", now=mon, banter=True) for _ in range(8)]
check([x is not None for x in seq] == [False, False, False, True] * 2, f"banter replies 1-3 none, 4th forced, x2 ({seq})")
check(cb._load()["policy"]["group:" + GROUP] == 0, "policy counter persisted in the state file (reset after a callback)")
check(cb.callback_for(GROUP, "Ackbar", False, text="who's winning", now=mon, banter=False) is None
      and cb._load()["policy"]["group:" + GROUP] == 0, "a non-banter reply never gets one and doesn't count")
graded_ackbar()
for _ in range(3):
    cb.callback_for(GROUP, "Ackbar", False, now=mon, banter=True, mark=False)
check(cb._load()["policy"].get("group:" + GROUP, 0) == 0, "mark=False (peek) doesn't move the counter")
reset()
for _ in range(5):
    cb.callback_for(GROUP, "Ackbar", False, now=mon, banter=True)
check(cb._load()["policy"]["group:" + GROUP] == 5, "no material: the counter keeps waiting")
STATE["games"] = W11_OPEN + W12
cb.after_reply(GROUP, "Ackbar", "@Picky who", "Boise -13.5 is a trap.", ts=thu)
STATE["games"] = W11_FINAL + W12
cb.grade_pending(now=mon)
check(cb.callback_for(GROUP, "Ackbar", False, now=mon, banter=True) is not None,
      "material shows up: the very next banter reply gets it")
graded_ackbar()
with patch.dict(os.environ, {"PICKY_CALLBACK_EVERY": "1"}):
    check(cb.callback_for(GROUP, "Ackbar", False, text="you suck", now=mon, banter=True)
          == "you said that Saturday too, then went 1-3 against the number in week 11.",
          "PICKY_CALLBACK_EVERY=1 (tests/exam): every banter reply; the repeat shot leads")
check(cb.callback_for(GROUP, "TG", False, now=mon, banter=True) is None, "a member with no material: nothing")
graded_ackbar()
with patch.dict(os.environ, {"PICKY_CALLBACKS_STATE": ""}), patch.object(cb, "state_path", lambda: TMP / "callbacks.json"), \
        patch.dict(os.environ, {"PICKY_GROUP_PORT": "8788"}):
    before = cb.state_path().read_text()
    got = cb.callback_for(GROUP, "Ackbar", False, now=mon)
    check(got and cb.state_path().read_text() == before, "disabled (side sidecar, no override): reads, never writes")

# --- recall asks: 'remember what you said about X' ---------------------------------
graded_ackbar()
for t, want in [("remember what you said about Boise?", True), ("what did you tell me on UTSA", True),
                ("you said Boise was a trap", True), ("remember when we won", False), ("you suck", False)]:
    check(cb.is_recall(t) is want, f"is_recall({t!r}) = {want}")
r = cb.callback_for(GROUP, "Ackbar", False, text="remember what you said about Boise?", now=mon, banter=False)
check(r and "Boise State -13.5 was a trap" in r, f"recall about Boise -> that graded lean, even on a non-banter reply ({r})")
graded_ackbar()
r = cb.callback_for(DM_ACK, "Ackbar", True, text="what did you tell me on UTSA", now=mon, banter=False)
check(r and r.startswith("told you UTSA -2.5 was the play, covered by 1.5"), f"recall in that DM: the DM lean ({r})")
graded_ackbar()
r = cb.callback_for(GROUP, "Ackbar", False, text="what did you tell me on UTSA", now=mon, banter=False)
check(r is None, f"LEAK TEST: recall of a DM lean in the group -> nothing at all ({r})")
r = cb.callback_for(DM_OTHER, "Ackbar", True, text="remember what you said about UTSA?", now=mon, banter=False)
check(r is None, f"LEAK TEST: ... or in another DM ({r})")
graded_ackbar()
everything = [ln for _ in range(6) for ln in [cb.callback_for(GROUP, "Ackbar", False, text=q, now=mon, banter=True)
                                              for q in ("you suck", "remember what you said?", "lol")] if ln]
check(everything and not any("UTSA" in ln or "North Texas" in ln for ln in everything),
      "LEAK TEST: draining every group callback never shows the DM lean")
graded_ackbar()
cb.log_lean(GROUP, "Ackbar", 202, "Memphis", -3.5, ts=mon - 3600, week=12, away="Memphis", home="USF")
check(cb.callback_for(GROUP, "Ackbar", False, text="lol", now=mon, mark=False) is not None
      and "Memphis" not in " ".join(ln for _, ln in cb.candidates(GROUP, "Ackbar", False, text="lol", now=mon)),
      "an ungraded lean is never a callback on its own")
r = cb.callback_for(GROUP, "Ackbar", False, text="remember what you said about Memphis?", now=mon, banter=False)
check(r == "I had you on Memphis -3.5. not graded yet.", f"recall naming an ungraded lean: the call, no result ({r})")
check(not any("Memphis" in ln for _, ln in cb.candidates(GROUP, "Ackbar", False, text="remember what you said about "
                                                                                    "Memphis?", now=mon + 11 * 86400)),
      "a pending lean past LEAN_FRESH_DAYS is skipped")

# --- ATS framing ---------------------------------------------------------------------
def L(side, fade, covered, margin, label, faded, mp=None):
    return {"side": side, "fade": fade, "result": {"covered": covered, "margin": margin, "label": label,
                                                    "faded_label": faded, "member_pick": mp}}
check(cb.lean_line(L("Oregon State", True, True, 3.5, "Oregon State +13.5", "Boise State -13.5"))
      == "told you Boise State -13.5 was a trap, it missed the cover by 3.5.", "ATS: trap that hit")
check(cb.lean_line(L("UTSA", False, True, 1.5, "UTSA -2.5", "North Texas +2.5"))
      == "told you UTSA -2.5 was the play, covered by 1.5.", "ATS: play that covered by X")
check(cb.lean_line(L("UTSA", False, False, 4, "UTSA -2.5", "North Texas +2.5"))
      == "my bad on UTSA -2.5, missed the cover by 4.", "ATS: owns the miss, didn't cover by X")
check(cb.lean_line(L("Navy", True, False, 2, "Navy -7", "SMU +7"))
      == "I faded SMU +7 and it covered by 2. that one's on me.", "ATS: owns a fade that covered")
check("by" not in cb.lean_line(L("UTSA", False, True, None, "UTSA -2.5", "North Texas +2.5")),
      "ATS: no margin claimed when the score disagrees with the graded winner")
check(not any(w in cb.lean_line(L("UTSA", False, True, 30, "UTSA -2.5", "x")) for w in ("won by", "beat")),
      "ATS framing: covered/missed the cover, never straight-up 'won'")

# --- burn sheet (lore) --------------------------------------------------------------
LORE = {"members": {"Ackbar": [
    "Ackbar, 2025 season: 28 wins in weeks 1-15, 6th of 13: best week 4-0 (week 13), worst week 0-4 (week 15); "
    "perfect weeks: week 13; winless weeks: week 15; longest hit streak 6 picks, longest miss streak 5.",
    "Ackbar, 2026 so far (through 2026-11-16): best week 4-0 (week 10), worst week 1-3 (week 8); longest hit streak 5 "
    "picks, longest miss streak 2; last 8 picks 7-1.",
    "Ackbar, best upset hits: UCLA +11.5 won outright 17-15 (UCLA at Oregon, 2026 week 6).",
    "Ackbar, worst blown picks (favorite that lost outright): Alabama -13.5 lost outright 17-31 (Alabama at Florida "
    "State, 2025 week 1); Texas -8.5 lost outright 24-27 (Texas at Oklahoma, 2026 week 6).",
    "Ackbar, stats-page oddity: Ackbar has picked against Northwestern 4 times over the last two seasons (2025-2026) "
    "and gone 0-4.",
    "Ackbar, stats-page oddity: Ackbar is 5-1 in Iowa games.",
    "Ackbar, head to head (games where they took opposite sides, 2025-2026): best vs TG 16-7; worst vs Chuck P 18-21."]}}
bs = cb.burn_sheet("Ackbar", LORE)
texts = [b["text"] for b in bs]
check([b["angle"] for b in bs] == ["blown", "blown", "winless", "worst_week", "miss_streak", "oddity", "h2h"],
      f"burn sheet angles, worst first ({[b['angle'] for b in bs]})")
check("Alabama -13.5 lost outright 17-31 (Alabama at Florida State, 2025 week 1)" in texts
      and "18-21 head to head against Chuck P" in texts and "worst week this season: 1-3 (week 8)" in texts,
      "burn sheet lines are lore facts verbatim")
check(not any("UCLA" in t or "Iowa" in t or "16-7" in t for t in texts), "burn sheet skips the good stuff")
check(all(b["scope"] == "group" and b["id"].startswith("burn:") for b in bs) and len({b["id"] for b in bs}) == len(bs),
      "burn sheet items: group scope, unique ids")
check(cb.burn_sheet("Nobody", LORE) == [] and cb.burn_sheet("Ackbar", {}) == [], "no lore: empty sheet")

# --- exam rider (PICKY_CALLBACK_RIDER=1) -----------------------------------------------
graded_ackbar()
check(cb.rider("you suck", "insult") == "", "rider off by default")
with patch.dict(os.environ, {"PICKY_CALLBACK_RIDER": "1", "PICKY_CALLBACK_EVERY": "1"}):
    check(cb.rider("you suck", "insult") == "", "rider on but no chat context: nothing")
    tok = cb.CHAT.set((GROUP, "Ackbar", False))
    try:
        r = cb.rider("you suck", "insult")
        check("CALLBACK" in r and "then went 1-3 against the number in week 11" in r, f"rider carries the line ({r[:90]})")
        check(cb.rider("who's winning", "question") == "", "rider: no callback on a plain question")
        r = cb.rider("what did you tell me about UTSA", "question")
        check("UTSA" not in r, "LEAK TEST: rider in the group never carries the DM lean")
    finally:
        cb.CHAT.reset(tok)

# group_chat.py: the hook fires after the reply and can't change or break it
try:
    import group_chat as gc
except Exception as e:  # noqa: BLE001
    print("skip /group hook check:", e)
else:
    class Req:
        def __init__(self, body):
            self.headers = {"x-picky-group-secret": "s"}
            self._b = body

        async def json(self):
            return self._b

    async def fake_answer(*a, **k):
        return {"reply": "I'd take Boise -13.5.", "image": None, "mode": "group"}

    calls = []
    common = [patch.object(gc, "SECRET", "s"), patch.object(gc, "answer", fake_answer),
              patch.object(gc.maintenance, "active", return_value=False),
              patch.object(gc.group_outbox, "config", return_value={"group_guid": GROUP}),
              patch.object(gc.VIEWER, "sender", return_value=gc.Sender(handle="+15555550100", team="Ackbar"))]
    for p in common:
        p.start()
    try:
        with patch.object(gc.callbacks, "hook", side_effect=lambda *a, **k: calls.append((a, k))):
            out = asyncio.run(gc.group(Req({"handle": "+15555550100", "text": "@Picky you suck", "chat": GROUP})))
        check(out["reply"] == "I'd take Boise -13.5.", "reply unchanged with the hook in place")
        check(calls == [((GROUP, "Ackbar", "you suck", "I'd take Boise -13.5."), {"is_dm": False})],
              f"hook called once with chat, team, stripped text, sent reply ({calls})")
        with patch.object(gc.callbacks, "after_reply", side_effect=RuntimeError("boom")):
            out = asyncio.run(gc.group(Req({"handle": "+15555550100", "text": "@Picky you suck", "chat": GROUP})))
        check(out["reply"] == "I'd take Boise -13.5.", "after_reply blowing up never breaks the reply")
    finally:
        for p in common:
            p.stop()

# picky_brain: the rider rides in the draft prompt, right before the question; off -> nothing added
try:
    import chat
    import picky_brain as pb
except Exception as e:  # noqa: BLE001
    print("skip picky_brain rider check:", e)
else:
    def draft_prompt(rider_env: str) -> str:
        got = {}

        async def fake_orig(messages, prefix=None):
            got["user"] = messages[-1]["content"]
            yield 'data: {"content": "hi"}\n\n'

        async def fake_understand(*a, **k):
            return pb.Brief(asking="a shot at you", intent="insult", about="Picky", answer="Jab back.")

        async def go():
            st = {"q": "you suck", "team": "Ackbar", "idx": 0, "brief": None, "options": {"temperature": 0.2}}
            t1, t2, t3 = pb.STATE.set(st), chat.OPTIONS_OVERRIDE.set(st["options"]), cb.CHAT.set((GROUP, "Ackbar", False))
            try:
                return [x async for x in pb._ollama_stream([{"role": "user", "content": "R\n\nyou suck"}], "PFX")]
            finally:
                cb.CHAT.reset(t3)
                chat.OPTIONS_OVERRIDE.reset(t2)
                pb.STATE.reset(t1)
        graded_ackbar()
        with patch.object(pb, "_orig_ollama_stream", fake_orig), patch.object(pb, "understand", fake_understand), \
                patch.object(pb, "compute_stance", lambda *a, **k: None), \
                patch.dict(os.environ, {"PICKY_CALLBACK_RIDER": rider_env, "PICKY_CALLBACK_EVERY": "1"}):
            asyncio.run(go())
        return got["user"]
    u = draft_prompt("1")
    check("CALLBACK" in u and "went 1-3 against the number in week 11" in u and u.index("CALLBACK") < u.rindex("you suck"),
          "flag on: the callback rides in the draft prompt before the question")
    check("CALLBACK" not in draft_prompt("0"), "flag off: draft prompt unchanged")

print("\nall passed" if not fails else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)
