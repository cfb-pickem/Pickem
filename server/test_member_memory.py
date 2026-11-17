"""
Member memory (server/member_memory.py): DM privacy, the sensitive-info filter, receipt grading
against fixture finals (push, line sign, margin, week claims), treatment decay math, the forget
handler, lines_for limit/relevance, and extraction validation. No model, no network: a fake
extractor stands in for Opus and a fixture League for Supabase. Fake members and messages only.

Run:  ./.venv/bin/python3 -I test_member_memory.py
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
import tempfile
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
TMP = Path(tempfile.mkdtemp())
os.environ["PICKY_PRIVATE_DIR"] = str(TMP)
os.environ["PICKY_MEMBER_MEMORY_DIR"] = str(TMP / "member_memory")
os.environ["PICKY_EXCHANGE_LOG"] = str(TMP / "exchanges.jsonl")
os.environ["PICKY_GROUP_MEMBERS"] = str(TMP / "members.json")
os.environ["PICKY_NO_ANTHROPIC"] = "1"
(TMP / "members.json").write_text(json.dumps({"members": [
    {"team": "Alpha Dogs", "name": "Al", "handles": ["+15550000001"]},
    {"team": "Bravo", "name": "Bob", "handles": ["+15550000002"]},
    {"team": "Charlie Hustle", "name": "Chaz", "handles": ["+15550000003"]},
]}))
import member_memory as mm  # noqa: E402

CT = ZoneInfo("America/Chicago")
fails = 0


def check(cond, msg):
    global fails
    print(("ok   " if cond else "FAIL ") + msg)
    fails += not cond


def ts(s: str) -> float:
    return dt.datetime.fromisoformat(s).replace(tzinfo=CT).timestamp()


def reset():
    for f in (TMP / "member_memory").glob("*") if (TMP / "member_memory").exists() else []:
        if f.is_file():
            f.unlink()
    (TMP / "exchanges.jsonl").write_text("")


def row(team, q, when, mode="group", chat=None, a="ok"):
    r = {"ts": ts(when), "mode": mode, "team": team, "q": q, "a": a,
         "chat": chat or ("grp" if mode == "group" else f"dm-{team}")}
    r["msg_id"] = mm.msg_id(r)
    r["source"] = "group" if mode == "group" else "dm"
    return r


def write_log(rows):
    (TMP / "exchanges.jsonl").write_text("".join(json.dumps({k: v for k, v in r.items()
                                                               if k not in ("msg_id", "source")}) + "\n"
                                                 for r in rows))


# fixture slate: line = HOME adjustment, negative = home favored
G_HOME_FAV = {"id": 1, "week": 12, "away": "Illinois", "home": "Iowa", "line": -6.5, "pool": True,
              "start": "2026-11-21T11:00:00", "status": "final", "away_pts": 17, "home_pts": 24, "winner": "Iowa"}
G_PUSH = {"id": 2, "week": 12, "away": "LSU", "home": "Tennessee", "line": 3.0, "pool": True,
          "start": "2026-11-21T14:30:00", "status": "final", "away_pts": 27, "home_pts": 24, "winner": "LSU"}
G_AWAY_FAV = {"id": 3, "week": 12, "away": "Notre Dame", "home": "Navy", "line": 14.0, "pool": True,
              "start": "2026-11-21T18:00:00", "status": "final", "away_pts": 35, "home_pts": 24, "winner": "Navy"}
G_OPEN = {"id": 4, "week": 12, "away": "Northwestern", "home": "Minnesota", "line": -2.5, "pool": True,
          "start": "2026-11-21T19:00:00", "status": "scheduled", "away_pts": None, "home_pts": None, "winner": None}
G_NONPOOL = {"id": 5, "week": 12, "away": "Texas", "home": "Texas A&M", "line": None, "pool": False,
             "start": "2026-11-21T18:30:00", "status": "final", "away_pts": 20, "home_pts": 41, "winner": None}


def league(open_final=False):
    g4 = dict(G_OPEN)
    if open_final:
        g4.update(status="final", away_pts=21, home_pts=20, winner="Northwestern")
    games = {g["id"]: dict(g) for g in (G_HOME_FAV, G_PUSH, G_AWAY_FAV, g4, G_NONPOOL)}
    picks = {1: {"Alpha Dogs": "Iowa", "Bravo": "Illinois", "Charlie Hustle": "Iowa"},
             2: {"Alpha Dogs": "LSU", "Bravo": "LSU", "Charlie Hustle": "Tennessee"},
             3: {"Alpha Dogs": "Navy", "Bravo": "Notre Dame", "Charlie Hustle": "Navy"},
             4: {"Alpha Dogs": "Northwestern", "Bravo": "Minnesota", "Charlie Hustle": "Minnesota"}}
    return mm.League(games, picks if open_final else {k: v for k, v in picks.items() if k != 4},
                     ["Alpha Dogs", "Bravo", "Charlie Hustle"])


def rc(kind, side="", line=None, margin=None, game=None, wins=None, who="Alpha Dogs", week=12):
    return {"who": who, "kind": kind, "side": side, "line": line, "margin": margin, "wins": wins, "week": week,
            "game_id": game, "status": "pending", "ts": ts("2026-11-18T12:00:00")}


# ---------------------------------------------------------------------------------------------------
print("-- receipt grading (code, fixture finals)")
g = G_HOME_FAV  # Iowa (home) -6.5, final Iowa 24-17: won by 7, covers
r = mm.grade_game_claim(rc("cover", "Iowa"), g)
check(r["result"] == "hit" and r["score"] == "Illinois 17, Iowa 24", "home favorite -6.5 wins by 7 = cover hit + real score")
check(mm.grade_game_claim(rc("cover", "Illinois"), g)["result"] == "miss", "away dog +6.5 loses by 7 = miss")
check(mm.grade_game_claim(rc("cover", "Iowa", line=-7.5), g)["result"] == "miss",
      "claim's own line wins over the league line (Iowa -7.5, won by 7 = miss)")
r = mm.grade_game_claim(rc("cover", "LSU"), G_PUSH)  # Tennessee home +3 (line 3.0): 24+3 = 27 vs 27
check(r["result"] == "push", "push: home +3 loses by exactly 3 -> push for the away side")
check(mm.grade_game_claim(rc("cover", "Tennessee"), G_PUSH)["result"] == "push", "push for the home side too")
r = mm.grade_game_claim(rc("cover", "Notre Dame"), G_AWAY_FAV)  # line +14 = home Navy gets 14: ND -14, won by 11
check(r["result"] == "miss" and r["line_used"] == -14.0, "line sign: positive line = AWAY favored (ND -14, won by 11 = miss)")
check(mm.grade_game_claim(rc("cover", "Navy"), G_AWAY_FAV)["result"] == "hit", "Navy +14 loses by 11 = cover hit")
check(mm.grade_game_claim(rc("win", "Notre Dame"), G_AWAY_FAV)["result"] == "hit", "straight-up win claim")
check(mm.grade_game_claim(rc("margin", "Iowa", margin=7), g)["result"] == "hit", "'Iowa by 7', won by exactly 7 = hit")
check(mm.grade_game_claim(rc("margin", "Iowa", margin=10), g)["result"] == "miss", "'Iowa by 10', won by 7 = miss")
check(mm.grade_game_claim(rc("cover", "Northwestern"), G_OPEN) is None, "not final -> not graded")
check(mm.grade_game_claim(rc("cover", "Texas"), G_NONPOOL)["status"] == "void", "no line anywhere -> void, not a guess")
check(mm.grade_game_claim(rc("win", "Texas A&M"), G_NONPOOL)["result"] == "hit", "win claim on a non-pool final")
check(mm.grade_game_claim(rc("cover", "Ohio State"), g)["status"] == "void", "side not in the game -> void")
lg = league()
check(mm.grade_week_claim(rc("week_best"), lg) is None, "week claim waits until every pool game that week is final")
lg = league(open_final=True)  # Alpha 4-0 (Iowa, LSU, Navy, NW), Bravo 1-3, Charlie 2-2
check(mm.grade_week_claim(rc("week_best"), lg)["result"] == "hit", "'I'm winning this week' + best week = hit")
check(mm.grade_week_claim(rc("week_best", who="Bravo"), lg)["result"] == "miss", "week_best for a 1-3 week = miss")
r = mm.grade_week_claim(rc("week_record", wins=4, who="Charlie Hustle"), lg)
check(r["result"] == "miss" and "2-2" in r["score"], "'going 4-0' then 2-2 = miss with the real record")
check(mm.week_for(ts("2026-11-17T10:00:00"), lg) == 12, "a claim on Tuesday belongs to that week's pool")

# ---------------------------------------------------------------------------------------------------
print("-- treatment decay")
now = ts("2026-11-30T12:00:00")
check(abs(mm.decay_weight(10) - 0.5) < 1e-9 and abs(mm.decay_weight(20) - 0.25) < 1e-9 and mm.decay_weight(0) == 1,
      "half-life 10 days: w(0)=1, w(10)=.5, w(20)=.25")
evs = [{"ts": now, "label": "friendly"}, {"ts": now - 10 * 86400, "label": "hostile"}]
s, w = mm.treatment_score(evs, now)
check(abs(s - (1 - 0.5) / 1.5) < 1e-9 and abs(w - 1.5) < 1e-9, "score = sum(v*w)/sum(w): (1 - .5)/1.5")
evs = [{"ts": now - 1 * 86400, "label": "hostile", "source": "group", "date": "2026-11-29", "quote": "you suck"},
       {"ts": now - 2 * 86400, "label": "hostile", "source": "group", "date": "2026-11-28", "quote": "trash bot"},
       {"ts": now - 30 * 86400, "label": "friendly", "source": "group", "date": "2026-10-31", "quote": "love ya"}]
mem = mm._empty("Bravo")
mem["treatment"]["events"] = evs
h = mm.stance_hint("Bravo", now=now, mem=mem)
check(h["stance"] == "cold" and h["why"][0].startswith("Nov 29 (group, hostile)"), "recent hostility outweighs an old compliment -> cold, dated why")
mem["treatment"]["events"] = [{"ts": now - k * 86400, "label": "friendly", "source": "group", "date": "2026-11-29",
                               "quote": "nice call"} for k in (1, 2)]
check(mm.stance_hint("Bravo", now=now, mem=mem)["stance"] == "warm", "friendly -> warm")
mem["treatment"]["events"] = mem["treatment"]["events"][:1]
check(mm.stance_hint("Bravo", now=now, mem=mem)["stance"] == "neutral", "one message alone isn't enough evidence -> neutral")
mem["treatment"]["events"] = [{"ts": now - 40 * 86400, "label": "hostile", "source": "group", "date": "2026-10-21", "quote": "x"}]
check(mm.stance_hint("Bravo", now=now, mem=mem)["stance"] == "neutral", "one 40-day-old shot (weight < MIN_WEIGHT) -> neutral")
mem["treatment"]["events"] = [{"ts": now - 86400 * k, "label": "hostile", "source": "dm", "date": "2026-11-29",
                               "quote": "dm shot"} for k in (1, 2)]
check(mm.stance_hint("Bravo", now=now, mem=mem)["stance"] == "neutral", "DM-only hostility doesn't shape the group stance")
hd = mm.stance_hint("Bravo", dm=True, caller="Bravo", now=now, mem=mem)
check(hd["stance"] == "cold" and "dm shot" in hd["why"][0], "...but does in his own DM")
check(mm.stance_hint("Bravo", dm=True, caller="Alpha Dogs", now=now, mem=mem)["why"] == [], "and never in another member's DM")

# ---------------------------------------------------------------------------------------------------
print("-- sensitive filter")
for t, cat in [("my wife hates Michigan", "family"), ("can't watch, I'm at work all Saturday", "work"),
               ("I live in Ames now", "location"), ("just got out of surgery", "health"),
               ("lost $400 on the Bears", "money"), ("my mortgage is killing me", "money"),
               ("text me 555-123-4567", "contact"), ("roast Bob for being gay", "identity"),
               ("my kids love Iowa", "family")]:
    check(mm.sensitive(t) == cat, f"sensitive: {t!r} -> {cat}")
for t in ["Iowa covers easy", "LSU by 20", "the $75 buy-in is in the pot", "Notre Dame sucks", "DaddyFreeman stinks",
          "transfer portal is chaos", "the playoff race is a mess", "I'm winning this week"]:
    check(mm.sensitive(t) is None, f"not sensitive: {t!r}")

# ---------------------------------------------------------------------------------------------------
print("-- extraction validation + merge (fake extractor)")
reset()
rows = [row("Alpha Dogs", "Iowa covers easy. I hate Notre Dame forever", "2026-11-18T12:00:00"),
        row("Bravo", "Al is a chalk merchant, always has been", "2026-11-18T12:05:00"),
        row("Bravo", "between us, Tennessee +3 is free money", "2026-11-18T12:10:00", mode="dm"),
        row("Bravo", "my wife says Illinois +6.5 is the play", "2026-11-18T12:12:00", mode="dm"),
        row("Charlie Hustle", "I'm winning this week, book it", "2026-11-18T12:20:00"),
        row("Alpha Dogs", "you suck picky", "2026-11-18T12:30:00")]
write_log(rows)
ids = [r["msg_id"] for r in rows]


SEEN = []


def fake(prompt):
    SEEN.append(prompt)
    return {"items": [
        {"msg_id": ids[0], "member": "Alpha Dogs", "kind": "fact", "subkind": "hates_team", "text": "Hates Notre Dame",
         "quote": "I hate Notre Dame forever", "about": ["Notre Dame"]},
        {"msg_id": ids[1], "member": "Bravo", "kind": "beef", "subkind": "other", "text": "Calls Alpha Dogs a chalk merchant",
         "quote": "Al is a chalk merchant", "about": ["Alpha Dogs"]},
        {"msg_id": ids[2], "member": "Bravo", "kind": "beef", "subkind": "other", "text": "Secretly loves Tennessee +3",
         "quote": "Tennessee +3 is free money", "about": ["Tennessee", "Alpha Dogs"]},
        {"msg_id": ids[3], "member": "Bravo", "kind": "fact", "subkind": "other", "text": "His wife likes Illinois",
         "quote": "my wife says Illinois", "about": ["Illinois"]},                      # sensitive -> dropped
        {"msg_id": ids[0], "member": "Alpha Dogs", "kind": "line", "subkind": "other", "text": "",
         "quote": "Iowa covers by a mile", "about": []},                                 # not verbatim -> dropped
        {"msg_id": ids[1], "member": "Alpha Dogs", "kind": "fact", "subkind": "other", "text": "x",
         "quote": "chalk merchant", "about": []},                                        # wrong speaker -> dropped
        {"msg_id": "mnope", "member": "Bravo", "kind": "joke", "subkind": "other", "text": "x", "quote": "x", "about": []},
    ], "receipts": [
        {"msg_id": ids[0], "member": "Alpha Dogs", "kind": "cover", "game_id": 1, "side": "Iowa", "line": None,
         "margin": None, "wins": None, "claim": "Iowa covers easy"},
        {"msg_id": ids[2], "member": "Bravo", "kind": "cover", "game_id": 2, "side": "Tennessee", "line": 3,
         "margin": None, "wins": None, "claim": "Tennessee +3 is free money"},
        {"msg_id": ids[4], "member": "Charlie Hustle", "kind": "week_best", "game_id": None, "side": "", "line": None,
         "margin": None, "wins": None, "claim": "I'm winning this week"},
        {"msg_id": ids[0], "member": "Alpha Dogs", "kind": "cover", "game_id": 99, "side": "Iowa", "line": None,
         "margin": None, "wins": None, "claim": "Iowa covers easy"},                     # unknown game -> dropped
    ], "treatment": [{"msg_id": i, "label": "hostile" if n == 5 else "neutral"} for n, i in enumerate(ids)]}


rep = mm.process(mm.real_rows(ts("2026-11-18T00:00:00"), ts("2026-11-19T00:00:00")), league(open_final=True),
                 extractor=fake)
check(len(SEEN) == 1 and "between us, Tennessee +3" in SEEN[0] and "1 | Illinois @ Iowa" in SEEN[0],
      "one Opus batch; the prompt carries the messages and the slate")
check(rep["items"] == 3 and rep["receipts"] == 3, f"3 items + 3 receipts kept (got {rep['items']}/{rep['receipts']})")
check(rep["dropped"].get("sensitive") == 1 and rep["dropped"].get("quote not in message") == 1
      and rep["dropped"].get("speaker mismatch") == 1 and rep["dropped"].get("unknown msg_id") == 1
      and rep["dropped"].get("game/side not found") == 1, f"drops counted by rule: {rep['dropped']}")
a, b, c = mm.load("Alpha Dogs"), mm.load("Bravo"), mm.load("Charlie Hustle")
it = a["items"][0]
check(it["date"] == "2026-11-18" and it["source"] == "group" and it["msg"]["id"] == ids[0] and it["msg"]["chat"] == "grp",
      "item has date, source and the message citation")
check(a["receipts"][0]["status"] == "graded" and a["receipts"][0]["result"] == "hit", "receipt graded in the same run")
check(b["receipts"][0]["result"] == "push", "Bravo's Tennessee +3 graded as a push")
check(c["receipts"][0]["week"] == 12 and c["receipts"][0]["result"] == "miss", "Charlie's week claim -> week 12, 2-2 = miss")
check(not any("wife" in json.dumps(m) for m in (a, b, c)), "nothing sensitive written to any file")
check(oct(mm.path_for("Bravo").stat().st_mode & 0o777) == "0o600", "member files are 0600")
rep2 = mm.process(mm.real_rows(ts("2026-11-18T00:00:00"), ts("2026-11-19T00:00:00")), league(), extractor=fake)
check(rep2["rows"] == 0, "processed messages aren't sent to Opus twice")

# ---------------------------------------------------------------------------------------------------
print("-- privacy: DM items never leak")
everything = lambda caller, dm, q: mm.lines_for(caller, q, ["Tennessee"], dm=dm, limit=10)  # noqa: E731
check(not any("free money" in x for x in everything("Bravo", False, "Tennessee?")), "Bravo's DM item not in the GROUP, even for Bravo")
check(not any("free money" in x for x in everything("Alpha Dogs", True, "what does Bob think of Tennessee")),
      "Bravo's DM item not in ANOTHER member's DM, even when he names Bravo")
check(not any("free money" in x for x in everything("Alpha Dogs", False, "what does Bob think of Tennessee")),
      "...nor in the group when someone asks about Bravo")
check(any("free money" in x for x in everything("Bravo", True, "Tennessee?")), "Bravo's own DM: he gets his own receipt back")
check(any("chalk merchant" in x for x in mm.lines_for("Alpha Dogs", "what's Bob's problem with me", [], dm=False)),
      "group beef about Alpha Dogs is fair game in the group")
check(mm.visible("Bravo", "dm", "Bravo", False) is False and mm.visible("Bravo", "dm", "Bravo", True) is True
      and mm.visible("Bravo", "dm", None, True) is False and mm.visible("Bravo", "group", None, False) is True,
      "visible(): the rule table")

# ---------------------------------------------------------------------------------------------------
print("-- lines_for limit + relevance")
got = mm.lines_for("Alpha Dogs", "how did Iowa do", ["Iowa"], dm=False)
check(got and "Iowa covers easy" in got[0] and "HIT" in got[0] and "Illinois 17, Iowa 24" in got[0] and got[0].startswith("Nov 18 (group)"),
      f"receipt first, dated, graded, real score: {got[:1]}")
got = mm.lines_for("Alpha Dogs", "what do you think of Notre Dame", ["Notre Dame"], dm=False)
check(got and "Hates Notre Dame" in got[0], f"topical fact first on a Notre Dame ask: {got[:1]}")
for n in (1, 2, 3):
    check(len(mm.lines_for("Alpha Dogs", "Iowa Notre Dame Bob", ["Iowa", "Notre Dame"], dm=False, limit=n)) <= n, f"limit={n} respected")
check(mm.lines_for("Alpha Dogs", "Iowa", ["Iowa"], limit=0) == [], "limit=0 -> nothing")
check(mm.lines_for("Charlie Hustle", "what's the weather", [], dm=False) == [c["receipts"][0] and mm.receipt_line(c["receipts"][0])],
      "an off-topic ask gets only the sender's own graded receipt as filler")
check(mm.lines_for(None, "Iowa", ["Iowa"], dm=True) == [] or all("(dm)" not in x for x in mm.lines_for(None, "Iowa", ["Iowa"], dm=True)),
      "unknown caller never gets DM items")
check(mm.lines_for("Alpha Dogs", None, None) is not None, "bad input never raises")

# ---------------------------------------------------------------------------------------------------
print("-- forget handler")
check(mm.is_forget("forget that") == {"term": None} and mm.is_forget("@Picky forget what I said about Notre Dame")
      == {"term": "Notre Dame"} and mm.is_forget("Picky, forget about Iowa!") == {"term": "Iowa"}, "is_forget parses")
check(mm.is_forget("I can't forget that game") is None and mm.is_forget("who should I pick") is None, "is_forget: not a request")
res = mm.forget("Alpha Dogs", "forget what I said about Notre Dame", dm=False, now=ts("2026-11-19T09:00:00"))
a = mm.load("Alpha Dogs")
check(res["deleted"] == 1 and not any("Notre Dame" in x["text"] for x in a["items"]), "about X: the Notre Dame fact is gone")
check(any(r["claim"] == "Iowa covers easy" for r in a["receipts"]), "...and nothing else of his was touched")
check(mm.load("Bravo")["items"], "...and nobody else's memory was touched")
log = [json.loads(x) for x in (TMP / "member_memory" / "forget_log.jsonl").read_text().splitlines()]
check(log[-1]["team"] == "Alpha Dogs" and log[-1]["deleted"] == 1 and log[-1]["term"] == "Notre Dame", "forget is logged")
# re-running extraction on the same message can't bring it back
st = json.loads((TMP / "member_memory" / "state.json").read_text())
st["processed"].pop(ids[0])
(TMP / "member_memory" / "state.json").write_text(json.dumps(st))
mm.process([r for r in mm.real_rows(ts("2026-11-18T00:00:00"), ts("2026-11-19T00:00:00")) if r["msg_id"] == ids[0]],
           league(open_final=True), extractor=fake)
check(not any("Notre Dame" in x["text"] for x in mm.load("Alpha Dogs")["items"]), "a re-run doesn't re-add a forgotten item")
# 'forget that' = his latest message in this chat type
write_log(rows + [row("Bravo", "forget that", "2026-11-18T12:40:00", mode="dm")])
res = mm.forget("Bravo", "forget that", dm=True, now=ts("2026-11-18T12:40:30"))
b = mm.load("Bravo")
check(res["deleted"] == 0 and any(f["msg_id"] == ids[3] for f in b["forgotten"]),
      "'forget that' in DM targets his latest DM message (the one with nothing kept), marked forgotten")
check(any("chalk merchant" in x["text"] for x in b["items"]), "...his group beef stays")
write_log(rows[:3] + [row("Bravo", "forget that", "2026-11-18T12:40:00", mode="dm")])
res = mm.forget("Bravo", "forget that", dm=True, now=ts("2026-11-18T12:41:00"))
b = mm.load("Bravo")
check(res["deleted"] == 2 and not b["receipts"] and not any("free money" in x["quote"] for x in b["items"]),
      "'forget that' deletes the item + receipt from that message")
check(mm.forget("Bravo", "who should I pick", dm=True)["reply"] is None, "not a forget ask -> no-op")

# ---------------------------------------------------------------------------------------------------
print("-- real_rows skips exam/test traffic")
write_log([row("Alpha Dogs", "real one", "2026-11-18T12:00:00")])
with open(TMP / "exchanges.jsonl", "a") as f:
    f.write(json.dumps({"ts": ts("2026-11-18T12:01:00"), "mode": "group", "team": "Bravo", "q": "exam", "a": "x",
                        "chat": "c", "side": 1}) + "\n")
    f.write(json.dumps({"ts": ts("2026-11-18T12:02:00"), "mode": "dm", "team": None, "q": "test handle", "a": "x",
                        "chat": "c"}) + "\n")
got = mm.real_rows(ts("2026-11-18T00:00:00"), ts("2026-11-19T00:00:00"))
check([r["q"] for r in got] == ["real one"], "side=1 and team-null rows skipped")

# ---------------------------------------------------------------------------------------------------
print("-- lines_for prefix matching (the 'SEC' vs 'SECSPN' gap), careful with short tokens")
Q = lambda q: mm._tokens(q)
check(mm.token_hits("SEC media", Q("SEC media"), "the SECSPN media cabal") == 2, "'SEC' hits 'SECSPN' (+ 'media')")
check(mm.token_hits("sec media", Q("sec"), "the SECSPN media cabal") == 1, "lowercase league shorthand 'sec' hits 'SECSPN'")
check(mm.token_hits("sec", Q("sec"), "my second favorite team") == 0, "'sec' never hits 'second'")
check(mm.token_hits("sec", Q("sec"), "Second-guessing the Secret Service") == 0, "...or a capitalized 'Second'/'Secret'")
check(mm.token_hits("ten", Q("ten"), "TENNESSEE IS FRAUDULENT") == 0, "a short lowercase non-acronym never prefix-matches")
check(mm.token_hits("finebaum", Q("finebaum"), "Paul Finebaum's show") == 1, "5+ letter word hits its possessive")
check(mm.token_hits("iowa", Q("iowa"), "Iowans love punting") == 0, "4-letter word: whole word only")
check(mm.token_hits("georgia", Q("georgia"), "Georgian food") == 0, "plural/possessive only, not any longer word")
reset()
t0 = ts("2026-11-17T12:00:00")
m = mm._empty("Bravo")
m["items"] = [{"id": "s1", "kind": "beef", "subkind": "", "text": "Mocks the 'SECSPN media cabal'",
               "quote": "the SECSPN media cabal", "about": [], "date": "2026-11-17", "ts": t0, "source": "group",
               "msg": {"id": "x", "ts": t0, "chat": "grp"}}]
mm.save(m)
check(any("SECSPN" in x for x in mm.lines_for("Bravo", "the SEC is bought and paid for", [], limit=1)),
      "lines_for: his 'SEC' question surfaces his group SECSPN beef")
check(not mm.lines_for("Bravo", "my second pick this week", [], limit=1), "...a 'second' question doesn't")

# ---------------------------------------------------------------------------------------------------
print("-- Charlie (Chuck P): his texts never log treatment / beefs / lines; stance always warm")
reset()
crow = row("Chuck P", "you suck picky, I'm way better than you, and Iowa covers easy", "2026-11-18T12:00:00", mode="dm")
orow = row("Bravo", "you suck picky", "2026-11-18T12:01:00", mode="dm")


def charlie_fake(prompt):
    return {"items": [{"msg_id": crow["msg_id"], "member": "Chuck P", "kind": k, "subkind": sk, "text": t, "quote": q,
                       "about": []} for k, sk, t, q in (("beef", "", "Beef with Picky", "I'm way better than you"),
                                                        ("line", "", "you suck picky", "you suck picky"),
                                                        ("fact", "other", "Thinks Iowa covers", "Iowa covers easy"))],
            "receipts": [], "treatment": [{"msg_id": crow["msg_id"], "label": "hostile"},
                                          {"msg_id": orow["msg_id"], "label": "hostile"}]}


mems, rep = mm.merge(charlie_fake(""), [crow, orow], None)
check(mems["Chuck P"]["items"] and all(x["kind"] == "fact" for x in mems["Chuck P"]["items"]),
      "Charlie: fact kept, beef + line dropped")
check(not mems["Chuck P"]["treatment"]["events"] and rep["dropped"].get("charlie") == 3, "Charlie: no treatment event")
check(len(mems["Bravo"]["treatment"]["events"]) == 1, "anyone else's hostile text is still labeled")
m = mm._empty("Chuck P")
m["treatment"]["events"] = [{"msg_id": f"h{i}", "ts": t0 + i, "date": "2026-11-17", "source": "dm", "label": "hostile",
                             "quote": "x"} for i in range(5)] + [{"msg_id": "n", "ts": t0, "date": "2026-11-17",
                                                                  "source": "dm", "label": "neutral", "quote": ""}]
m["items"] = [{"id": "b", "kind": "beef", "subkind": "", "text": "beef", "quote": "q", "about": [], "date": "2026-11-17",
               "ts": t0, "source": "dm", "msg": {"id": "h0", "ts": t0, "chat": ""}}]
mm.save(m)
check(mm.stance_hint("Chuck P", dm=True, caller="Chuck P", now=t0 + 60)["stance"] == "warm",
      "Charlie's stance_hint is warm even with stored hostile events")
out = mm.scrub_charlie()
m = mm.load("Chuck P")
check(out == {"team": "Chuck P", "hostile_events": 5, "items": 1} and [e["label"] for e in m["treatment"]["events"]] == ["neutral"]
      and not m["items"], "scrub_charlie clears hostile events and beefs/lines, keeps the rest")
flag = TMP / "PICKY_CHARLIE_NICE_OFF"
_old_flag, mm.CHARLIE_NICE_OFF = mm.CHARLIE_NICE_OFF, flag
flag.write_text("")
mems, rep = mm.merge(charlie_fake(""), [crow, orow], None, mems={})
check(any(e["msg_id"] == crow["msg_id"] for e in mems["Chuck P"]["treatment"]["events"]) and not mm.is_charlie("Chuck P"),
      "off switch (PICKY_CHARLIE_NICE_OFF): Charlie is treated like anyone")
flag.unlink()
mm.CHARLIE_NICE_OFF = _old_flag

# ---------------------------------------------------------------------------------------------------
print("-- send-point receipt check")
reset()
m = mm._empty("Alpha Dogs")
m["receipts"] = [dict(rc("cover", side="Iowa", game=1), id="r1", claim="Iowa covers easy", date="2026-11-18",
                      source="group", msg={"id": "z", "ts": 0, "chat": "grp"}, status="graded", result="hit",
                      score="Illinois 17, Iowa 24"),
                 dict(rc("win", side="Northwestern", game=4), id="r2", claim="Northwestern wins outright",
                      date="2026-11-18", source="dm", msg={"id": "y", "ts": 0, "chat": "dm"})]
mm.save(m)
txt, cut = mm.check_receipts("You said Iowa covers easy and it missed. Brutal.", "Alpha Dogs")
check(txt == "Brutal." and len(cut) == 1, "a hit quoted as a miss is cut")
txt, cut = mm.check_receipts("You said Iowa covers easy. It cashed, 24-17.", "Alpha Dogs")
check(not cut, "the right verdict and score stay")
txt, cut = mm.check_receipts("Alpha Dogs said Iowa covers easy, final 30-3, nailed it.", "Bravo", "how did his call do")
check(cut, "another game's score on a named member's receipt is cut")
txt, cut = mm.check_receipts("You said Northwestern wins outright and it hit.", "Alpha Dogs", dm=True)
check(cut, "a verdict on a call that isn't final is cut")
txt, cut = mm.check_receipts("You said Northwestern wins outright and it hit.", "Alpha Dogs", dm=False)
check(not cut, "a DM receipt isn't in play in the group (not visible, not checked)")
check(mm.check_receipts("Iowa covered, you're up 2.", "Alpha Dogs")[1] == [], "pick talk without a 'said' isn't a receipt")

print(f"\n{'ALL OK' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
