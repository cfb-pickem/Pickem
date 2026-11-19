"""
Nightly flub loop (flub_loop.py): harvest from fixture logs (eval/fixtures/flub, anonymized), the flag
queue, a fake Opus classify, item verification + anonymizing + merge, the exam loader, the fix-round
gates and kill switches, and the report. No model, no network, nothing outside a temp dir.

Run:  ./.venv/bin/python3 -I test_flub_loop.py
"""
from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
FIX = HERE / "eval" / "fixtures" / "flub"
TMP = Path(tempfile.mkdtemp())
os.environ.update({"PICKY_EXCHANGE_LOG": str(FIX / "exchanges.jsonl"),
                   "PICKY_GROUP_STDERR": str(FIX / "picky-group.stderr.log"),
                   "PICKY_FLUB_DIR": str(TMP / "flubs"), "PICKY_FLUB_QUEUE": str(TMP / "flubs" / "queue.jsonl"),
                   "PICKY_FLUB_ITEMS_PUBLIC": str(TMP / "flub_items.json"),
                   "PICKY_FLUB_ITEMS_PRIVATE": str(TMP / "flubs" / "items-private.json"),
                   "PICKY_FLUB_KILL_FILE": str(TMP / "FLUB_AUTOFIX_OFF"),
                   "PICKY_GROUP_MEMBERS": str(TMP / "members.json"), "PICKY_CLAUDE_USAGE_DIR": str(TMP / "usage"),
                   "PICKY_REPLAY_DIR": str(TMP / "replay")})
os.environ.pop("PICKY_NO_ANTHROPIC", None)
os.environ.pop("PICKY_FLUB_AUTOFIX", None)
sys.path.insert(0, str(HERE))
import flag_flub  # noqa: E402
import flub_loop as fl  # noqa: E402
import picky_claude as _pc  # noqa: E402
# Claude up: the picky_claude booster gate open, keeping the kill switch (the gate itself, cap and auth are covered in test_zero_maintenance.py)
_pc.why_not = lambda entry=None: "PICKY_NO_ANTHROPIC set" if _pc.disabled() else ""

fails = []


def check(cond, msg):
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond:
        fails.append(msg)


D = "2026-11-16"
FL = TMP / "flubs"

# ---- the flag queue: a commissioner flag pointing at the group exchange (context only, no text) ----------
FL.mkdir(parents=True)
grp = json.loads((FIX / "exchanges.jsonl").read_text().splitlines()[5])
qid = flag_flub.log_flub("", note="he said no info, the news feed had 3 stories", source="coordinator",
                         context=[{"q": grp["q"], "a": grp["a"], "team": "Beta"}], now=grp["ts"] + 60)
later = flag_flub.log_flub("next-day flag", source="dm", context=[], now=grp["ts"] + 86400 * 2)

# ---- harvest (dry: queue untouched) ------------------------------------------------------------------
h = fl.harvest(D, dry=True)
c = {x["q"] + "|" + x["mode"]: x for x in h["candidates"]}
check(h["seen"] == 4, f"4 live exchanges seen (got {h['seen']})")
check({k: v for k, v in h["skipped"].items() if v} == {"test_handle": 1, "side": 1, "unmatched": 1},
      f"skips: test handle, side, old unmatched {h['skipped']}")
check(h["window"] == "day" and h["by_mode"]["dm"] == 3 and h["by_mode"]["group"] == 1 and h["by_mode"]["web"] == 0,
      f"a past --date = that CT day; counts by mode {h['by_mode']}")
check(h["skipped_by_mode"] == {"dm": {"test_handle": 1}, "group": {"side": 1, "unmatched": 1}},
      f"skips by mode {h['skipped_by_mode']}")
check(h["candidates"][0].get("queue_id") == qid and h["candidates"][0]["priority"] == "queue",
      "the queued flag is candidate #1")
check(h["candidates"][0]["q"] == grp["q"] and h["candidates"][0]["note"].startswith("he said"),
      "the flag found its exchange from the context and kept the note")
check(later not in h["queue_ids"], "a flag from after the day isn't in that day's harvest")
first = [x for x in h["candidates"] if x["q"] == "who should I pick this week"]
t1 = {s["type"] for s in first[0]["signals"]} if first else set()
t2 = {s["type"] for s in first[1]["signals"]} if len(first) > 1 else set()
check({"slow", "filter_drop", "prime_failed", "reasked"} <= t1, f"12:00 reply: slow, filter drop, prime failed, re-asked {t1}")
check({"fact_lost", "filter_drop", "pushback"} <= t2, f"12:03 reply: fact lost, filter drop, member pushback {t2}")
check(first and first[0]["secs"] == 75.2, "secs from the reply line")
grp_c = h["candidates"][0]
check({s["type"] for s in grp_c["signals"]} == {"banter_cut"}, f"group row matched by time: banter cut {grp_c['signals']}")
check(h["latency"] == {"n": 4, "p50": 30.5, "p95": 75.2, "max": 75.2, "slow": 1}, f"latency {h['latency']}")
check(stat.S_IMODE(os.stat(FL / f"harvest-{D}.json").st_mode) == 0o600, "harvest file is 0600")
q = [json.loads(x) for x in Path(os.environ["PICKY_FLUB_QUEUE"]).read_text().splitlines()]
check(all(r["consumed"] is None for r in q), "dry harvest leaves the queue unconsumed")
check(all("chat" not in s["type"] for x in h["candidates"] for s in x["signals"]), "no stray signal types")

h = fl.harvest(D)
q = {r["id"]: r for r in (json.loads(x) for x in Path(os.environ["PICKY_FLUB_QUEUE"]).read_text().splitlines())}
check(q[qid]["consumed"] == D and q[later]["consumed"] is None, "real harvest marks only that day's flag consumed")
h2 = fl.harvest(D, dry=True)
check(not h2["queue_ids"], "a consumed flag isn't harvested twice")
fl.harvest(D, dry=True)  # back to a harvest file with the flag for the rest (dry: no consumption)
hh = json.loads((FL / f"harvest-{D}.json").read_text())
hh["candidates"] = h["candidates"]
fl.write_private(FL / f"harvest-{D}.json", json.dumps(hh))

# ---- every mode, since the last run: web widget rows, test asks, late-night chats ---------------------------
import datetime as _dt0  # noqa: E402
def _t(day, hh, mm, ss=0):
    return _dt0.datetime(2026, 11, day, hh, mm, ss, tzinfo=fl.CT).timestamp()
W = TMP / "window"
W.mkdir()
ex = [{"ts": _t(18, 22, 50), "mode": "dm", "team": "Alpha", "q": "before the last run", "a": "x", "chat": "c1"},
      {"ts": _t(18, 23, 28, 41), "mode": "web", "team": None, "q": "who should I pick", "a": "NW -2.5", "lane": "fact:pick",
       "chat": "w1", "secs": 7.8},
      {"ts": _t(18, 23, 34, 47), "mode": "web", "team": None, "q": "ATS records for all 8 teams?", "a": "Just make sure...",
       "lane": "full", "chat": "w1", "secs": 46.1},
      {"ts": _t(18, 23, 35, 26), "mode": "web", "team": None, "q": "just give me the numbers", "a": "Yep.",
       "lane": "banter:banter", "chat": "w1", "secs": 14.0},
      {"ts": _t(19, 11, 0, 25), "mode": "web", "team": None, "q": "scripted test ask", "a": "x", "lane": "full", "chat": "w2"},
      {"ts": _t(19, 11, 5), "mode": "web", "team": "Alpha", "q": "new-style test row", "a": "x", "chat": "w3", "test": 1},
      {"ts": _t(19, 12, 0, 10), "mode": "web", "team": None, "q": "no team account", "a": "x", "lane": "banter:greet", "chat": "w4"},
      {"ts": _t(19, 13, 48, 36), "mode": "web", "team": "Beta", "q": "cake recipe", "a": "no", "lane": "full", "chat": "w5", "secs": 18.8},
      {"ts": _t(19, 14, 0), "mode": "dm", "team": None, "q": "allowlisted non-member", "a": "x", "chat": "c9"},
      {"ts": _t(19, 14, 11), "mode": "dm", "team": "Alpha", "q": "draw a game", "a": "On it.", "lane": "picture", "chat": "c1"},
      {"ts": _t(19, 22, 59), "mode": "group", "team": "Beta", "q": "late group ask", "a": "y", "chat": "g1", "secs": 3.0}]
(W / "ex.jsonl").write_text("\n".join(json.dumps(r) for r in ex) + "\n")
(W / "err.log").write_text("\n".join([
    "2026-11-18 23:28:40,650 INFO web reply mapped=True 7.8s chars=281 lane=fact:pick test=False",
    "2026-11-18 23:34:40,000 INFO results guard: dropped ['x']",
    "2026-11-18 23:34:46,795 INFO web reply mapped=True 46.1s chars=130 lane=full test=False",
    "2026-11-18 23:35:26,399 INFO web reply mapped=True 14.0s chars=4 lane=banter:banter test=False",
    "2026-11-19 11:00:25,320 INFO web reply mapped=True 114.1s chars=117 lane=full test=True",
    "2026-11-19 12:00:10,000 INFO web reply mapped=False 3.0s chars=9 lane=banter:greet test=False",
    "2026-11-19 13:48:35,527 INFO web reply mapped=True 18.8s chars=218 lane=full test=False",
    "2026-11-19 14:11:39,000 INFO dm reply sender=0001 mapped=True 0.6s chars=40 image=False lane=picture",
    "2026-11-19 22:59:00,100 INFO group reply sender=0002 mapped=True 3.0s chars=1 image=False lane=fact:x"]) + "\n")
WD = W / "flubs"
with patch.multiple(fl, EXCHANGES=W / "ex.jsonl", STDERR_LOG=W / "err.log", FLUB_DIR=WD, CURSOR=WD / "cursor.json"), \
        patch.object(flag_flub, "queue_path", lambda: W / "q.jsonl"):
    now = _t(19, 23, 0)
    fl.write_private(WD / "cursor.json", json.dumps({"until": _t(18, 23, 0), "date": "2026-11-18"}))
    hw = fl.harvest("2026-11-19", dry=True, now=now)
    check(hw["window"] == "cursor" and hw["since"] == _t(18, 23, 0) and hw["until"] == now,
          "tonight's run starts at the last run's cursor")
    check(hw["by_mode"] == {"dm": 1, "group": 1, "web": 4},
          f"web widget chats harvested, incl. last night's 23:28-23:35 after the 23:00 run {hw['by_mode']}")
    check(hw["skipped"]["test_web"] == 2 and hw["skipped"]["unmapped_web"] == 1 and hw["skipped"]["test_handle"] == 1,
          f"site test asks, team-less site accounts and test handles skipped, counted {hw['skipped']}")
    qs = {c["q"]: c for c in hw["candidates"]}
    check("before the last run" not in qs and "late group ask" in qs, "nothing from before the cursor; up to now")
    check({x["type"] for x in qs["ATS records for all 8 teams?"]["signals"]} == {"results_guard"}
          and qs["ATS records for all 8 teams?"]["secs"] == 46.1, "web rows get their reply line's signals")
    hw2 = fl.harvest("2026-11-19", dry=True, since=fl.parse_when("2026-11-18"), until=now)
    check(hw2["window"] == "override" and hw2["by_mode"]["dm"] == 2, "--since/--until override the cursor")
    (WD / "cursor.json").unlink()
    fl.write_private(WD / "harvest-2026-11-18.json", json.dumps({"until": _t(18, 23, 0)}))
    os.utime(WD / "harvest-2026-11-18.json", (1, 1))  # older than tonight's dry file
    check(fl.window("2026-11-19", now=now)[2] == "last harvest" and fl.window("2026-11-19", now=now)[0] == _t(18, 23, 0),
          "no cursor: the newest real (not dry) harvest file's end")
    for f in WD.glob("harvest-*.json"):
        f.unlink()
    check(fl.window("2026-11-19", now=now)[:3] == (_t(18, 0, 0), now, "yesterday"), "nothing at all: yesterday 00:00 CT")
    fl.write_private(WD / "cursor.json", json.dumps({"until": now - 30 * 86400}))
    check(fl.window("2026-11-19", now=now)[0] == now - fl.LOOKBACK_DAYS * 86400, "a stale cursor is capped at the lookback")
check(fl.parse_when("2026-11-18T23:00") == _t(18, 23, 0) and fl.parse_when("1795064400") == 1795064400.0, "parse_when")

# ---- classify with a fake Opus ---------------------------------------------------------------------
FACTS = {"facts": "SEASON STANDINGS:\n  1. Alpha 30-18\n  2. Beta 29-19\nTHIS WEEK'S POOL GAMES:\n"
                  "  Iowa at Illinois, pool line Illinois +8.5\n  Northwestern at Minnesota, pool line Northwestern -2.5\n"
                  "NEWS: SEC tiered fine cap plan (Ross Dellenger, Yahoo)",
         "personas": {"me": "Alpha", "mid": "Beta"}, "names": ["Alpha", "Beta", "Charlie"]}
cid = {x["q"] + "|" + str(x.get("secs")): x["cid"] for x in h["candidates"]}
c_news = h["candidates"][0]["cid"]
c_pick1 = cid["who should I pick this week|75.2"]
c_pick2 = cid["who should I pick this week|12.0"]
c_wrong = cid["thats wrong, iowa isnt even favored|20.0"]
seen_prompt = {}


def fake_opus(prompt):
    seen_prompt["p"] = prompt
    return {"verdicts": [
        {"cid": c_news, "flub": True, "category": "didn't answer", "why": "said no info with news on file",
         "item": {"turns": ["@Picky whats the latest news"], "private": False, "must_include": ["Dellenger"],
                  "must_not": ["don't have any info"], "judge": "pass on a real story"}},
        {"cid": c_pick1, "flub": True, "category": "wrong fact", "why": "Iowa -6.5 isn't the line",
         "item": {"turns": ["who should I pick this week"], "private": False, "must_include": ["Illinois +8.5"],
                  "must_not": ["only value side"], "expires": "2026-11-21"}},
        {"cid": c_pick2, "flub": True, "category": "wrong fact", "why": "made-up number",
         "item": {"turns": ["give me the Iowa game line"], "private": False, "must_include": ["Iowa -7.5"]}},
        {"cid": c_wrong, "flub": True, "category": "wrong fact", "why": "his record",
         "item": {"turns": ["whats my record"], "private": False, "persona": "me", "must_include": ["30-18"]}},
        {"cid": "c999", "flub": True, "category": "dodge", "why": "unknown cid"},
    ]}


v = fl.classify(D, facts=FACTS, opus=fake_opus)
check(v["by"] == "opus" and len(v["verdicts"]) == 4, f"opus verdicts kept for known cids only ({len(v['verdicts'])})")
check("Dellenger" in seen_prompt["p"] and c_news in seen_prompt["p"], "prompt carries the facts and the candidates")
check("the news feed had 3 stories" in seen_prompt["p"], "prompt carries the flag note")

with patch.dict(os.environ, {"PICKY_NO_ANTHROPIC": "1"}):
    def boom(_p):
        raise AssertionError("opus called with the kill switch on")
    cv = fl.classify(D, facts=FACTS, opus=boom)
check(cv["by"] == "code" and cv["note"].startswith("Claude unavailable"), "kill switch: code-only signals")
cats = {x["cid"]: x for x in cv["verdicts"]}
check(cats.get(c_news, {}).get("why", "").startswith("flagged"), "code-only: the queued flag is a flub")
check(cats.get(c_pick2, {}).get("why", "").startswith("member pushback"), "code-only: pushback is a flub")
check(cats.get(c_pick1, {}).get("category") == "didn't answer", "code-only: a re-ask is a didn't-answer")
check(not any(x.get("item") for x in cv["verdicts"]), "code-only: no items proposed")
cv2 = fl.classify(D, facts=FACTS, opus=lambda p: (_ for _ in ()).throw(RuntimeError("timeout")))
check(cv2["by"] == "code" and "opus failed" in cv2["error"], "an Opus failure falls back to code signals")
fl.classify(D, facts=FACTS, opus=fake_opus)  # the opus verdicts back for items

# ---- items: verify, anonymize, merge ---------------------------------------------------------------
fl.write_private(FL / f"facts-{D}.json", json.dumps(FACTS))
dry = fl.items(D, dry=True)
check(dry.get("would_add") == 3 and not (TMP / "flub_items.json").exists(), "dry: nothing written")
a = fl.items(D)
check(a["public"] == [] and a["private"] == ["flub-20261116-1", "flub-20261116-2", "flub-20261116-3"],
      f"every flub item is private, none public (R10) {a['public']} {a['private']}")
check(not (TMP / "flub_items.json").exists(), "nothing written to the public items file")
check(any("Iowa -7.5" in d["why"] and "not in the facts bundle" in d["why"] for d in a["dropped"]),
      "a key not in the facts bundle drops the item")
prv = json.loads((FL / "items-private.json").read_text())
check(prv[0]["turns"] == ["whats the latest news"], "@Picky stripped from turns")
check(prv[2]["persona"] == "me" and prv[2]["must_include"] == ["30-18"], "record item keeps its persona")
check(prv[2].get("expires") == "2026-11-22", f"a record key without expires ages out in 6 days {prv[2].get('expires')}")
check(stat.S_IMODE(os.stat(FL / "items-private.json").st_mode) == 0o600, "private items file is 0600")
check(prv[1]["expires"] == "2026-11-21", "expiry kept")
# ---- never twice: every confirmed flub is a permanent replay fixture ---------------------------------------
rg = json.loads((TMP / "replay" / "fixtures-flubs.json").read_text())
check(stat.S_IMODE(os.stat(TMP / "replay" / "fixtures-flubs.json").st_mode) == 0o600, "regression set is 0600")
check(len(rg) == 3 and a["regress"]["added"] == [f"rg-20261116-{i}" for i in (1, 2, 3)] and a["regress"]["dupes"] == 1,
      f"every confirmed flub is a regression fixture; two flubs on one question = one fixture {a['regress']}")
news = next(f for f in rg if f["q"] == "whats the latest news")
check(news["expect"]["answers"] == ["Dellenger"] and news["expect"]["expected_fail"] == news["id"]
      and news["expect"]["keys"] == "item" and news["mode"] == "group" and news["team"] == "Beta",
      "an item's keys become the fixture's checks; open = expected_fail")
import re as _re  # noqa: E402
check(_re.search(news["expect"]["must_not"][0], "I don’t  have any info on it", _re.I) is not None,
      "literal keys become tolerant regexes (curly quote, spacing)")
c2 = next(c for c in h["candidates"] if c["cid"] == c_pick2)
drop = fl.regress_fixture("rg-x-2", D, next(x for x in v["verdicts"] if x["cid"] == c_pick2), c2, None, FACTS)
check(drop["expect"]["keys"] == "fallback" and drop["expect"]["not_exact"] == [
    "My 4 calls: Northwestern -2.5, Illinois +8.5, Tennessee +1.5, Texas Tech -3.5."] and drop["mode"] == "dm",
      f"an item that failed verification still lands with fallback checks {drop['expect']}")
rec = next(f for f in rg if f["q"] == "whats my record")
check(rec["team"] == "Alpha" and rec["expect"].get("expires") == "2026-11-22", "persona -> the member; expiry carried")
again = fl.items(D)
check(again["regress"]["added"] == [] and again["regress"]["dupes"] == 4, "rerun: no duplicate fixtures")
check(not any(f["q"] == "give me the Iowa game line" for f in rg), "a dropped item's fixture asks the member's real text")
dodge = fl.regress_fixture("rg-x-1", D, {"cid": "c9", "category": "dodge", "why": "w"},
                           {"q": "ATS records for the 8 teams?", "a": "Yep.", "lane": "banter:banter", "mode": "web",
                            "team": None}, None, FACTS)
check(dodge["expect"]["not_lanes"] == ["banter:banter"] and dodge["expect"]["not_exact"] == ["Yep."]
      and dodge["mode"] == "dm" and dodge["via"] == "web" and dodge["team"] == "Beta",
      f"a web dodge from a code lane: never that lane again, never that reply again, replayed as a DM {dodge}")
check("not_lanes" not in fl.regress_fixture("x", D, {"category": "dodge"}, {"q": "q", "a": "a", "lane": "full"},
                                            None, FACTS)["expect"], "no lane pin on a full-brain reply")
slow = fl.add_regressions(D, [], FACTS, dry=True)
check(slow == {"would_add": [], "repeats": [], "dupes": 0}, "dry: nothing written")
# a fixed fixture flubbed again live = a repeat (top priority)
rg[0]["expect"].pop("expected_fail")
rg[0]["expect"]["fixed_on"] = "2026-11-17"
fl.save_regressions(rg)
c0 = next(c for c in h["candidates"] if c["q"] == rg[0]["q"])
rpt = fl.add_regressions("2026-11-18", [({"cid": "c1", "flub": True, "category": "didn't answer", "why": "again"},
                                         c0, None)], FACTS)
rg = fl.load_regressions()
check(rpt["repeats"] == [rg[0]["id"]] and rg[0]["expect"]["regressed_on"] == "2026-11-18"
      and rg[0]["expect"]["broke"] == ["2026-11-18"], f"a flub on a fixed question is a repeat {rpt}")
check(not hasattr(fl, "publish") and not hasattr(fl, "PUBLISH"), "no publish path left")
pub, prv = prv[:2], prv[2:]  # the loader tests below still cover a legacy public file
again = fl.items(D)
check(not again["public"] and not again["private"] and again["dupes"] == 3, "rerun: deduped, no new ids")
nxt = fl.merge_items("2026-11-16", [{"turns": ["brand new q"], "must_not": ["x y"], "private": False}])
check(nxt["public"] == ["flub-20261116-4"], f"merge_items: ids continue past the highest (stable) {nxt['public']}")
fl.PUBLIC_ITEMS.write_text(json.dumps(pub, indent=1))  # back to the 2 public items

# build_item edge cases
cand = {"a": "Iowa -6.5, the only value side."}
it, why = fl.build_item({"category": "wrong fact", "item": {"turns": ["q"], "private": False, "must_not": ["nope"]}},
                        cand, FACTS)
check(it is None and why == "the flubbed reply passes it", f"an item the flub passes is dropped ({why})")
it, why = fl.build_item({"item": {"turns": ["q"], "private": False}}, cand, FACTS)
check(it is None and why == "no checkable key", "an item with no keys is dropped")
it, why = fl.build_item({"item": {"turns": ["text +1 312 555 0100 back"], "private": True, "must_not": ["only value"]}},
                        cand, FACTS)
check(it is None and "phone" in why, "an item with a phone number is dropped")
it, _ = fl.build_item({"item": {"turns": ["what does Beta need"], "private": False, "must_not": ["only value"]}},
                      cand, FACTS)
check(it and it["private"], "a public item naming a member is moved to private")
it, _ = fl.build_item({"item": {"turns": ["q"], "private": False, "any_of": ["Illinois +8.5", "Illinois +9.5"]}},
                      cand, FACTS)
check(it and it["any_of"] == ["Illinois +8.5"], "invalid any_of alternatives are dropped")
check(fl.verify_term("Northwestern -2.5", FACTS["facts"]) is None, "verify: exact phrase")
check(fl.verify_term("Northwestern minus 2.5", FACTS["facts"]) is None, "verify: name + number present")
check(fl.verify_term("Northwestern -3.5", FACTS["facts"]) is not None, "verify: wrong number fails")
check(fl.verify_term("29-18", FACTS["facts"]) is not None, "verify: a record must match as a unit")

# ---- the exam loader + checks ---------------------------------------------------------------------
fl.write_private(fl.PRIVATE_ITEMS, json.dumps(prv + [dict(pub[0], id="flub-20261116-9")]))
li = fl.load_items(today="2026-11-22")
check([r["id"] for r in li] == ["flub-20261116-1", "flub-20261116-2", "flub-20261116-3"],
      f"public then private, same question deduped {[r['id'] for r in li]}")
check(li[1].get("skip") == "expired 2026-11-21" and not li[0].get("skip"), "expired items skip")
with patch.object(fl, "PRIVATE_ITEMS", TMP / "missing.json"):
    check(len(fl.load_items()) == 2, "no private file: public items only")
chk = fl.item_check(pub[0])
res = chk(["Per Ross Dellenger, the SEC wants tiered fines."], None, {})
check(all(ok for _n, ok, _d in res), "item check passes a good answer")
res = chk(["I don’t have any info on Dellenger."], None, {})
check([n for n, ok, _d in res if not ok] == ["flub_must_not"], "item check fails the flub (curly apostrophe too)")
check(not fl._same_chat({"mode": "group", "team": "A", "chat": "g"}, {"mode": "group", "team": "B", "chat": "g"})
      and fl._same_chat({"mode": "group", "team": "A", "chat": "g"}, {"mode": "group", "team": "A", "chat": "g"}),
      "pushback: the same member in the same chat, not anyone in the group")
check(fl.norm_q("@Picky  What's the LATEST news??") == fl.norm_q("whats the latest news"), "norm_q")
src = (HERE / "eval" / "group_items.py").read_text()
check("flub_loop.load_items()" in src and '"flub"' in (HERE / "eval" / "questions.py").read_text(),
      "group_items loads the flub items; 'flub' is an exam category")
repo_items = json.loads((HERE / "eval" / "flub_items.json").read_text())
check(isinstance(repo_items, list), "the repo's eval/flub_items.json is a list")

# ---- fix round: switches and gates -----------------------------------------------------------------
check(fl.fix_round(D, dry=True)["status"] == "dry", "dry fix round runs nothing")
(TMP / "FLUB_AUTOFIX_OFF").write_text("")
check(fl.fix_round(D)["status"] == "off", "kill switch file stops the fix round")
(TMP / "FLUB_AUTOFIX_OFF").unlink()
with patch.dict(os.environ, {"PICKY_FLUB_AUTOFIX": "0"}):
    check(fl.fix_round(D)["status"] == "off", "PICKY_FLUB_AUTOFIX=0 stops the fix round")
with patch.dict(os.environ, {"PICKY_NO_ANTHROPIC": "1"}), patch.object(fl, "_fix", side_effect=AssertionError):
    check(fl.fix_round(D)["why"].startswith("Claude unavailable"), "Anthropic off: no fix round")
base = {"det_pass": 80, "scored": 100, "invented": 0, "leaks": 0, "flub_pass": 2, "flub_n": 3}
check(fl.gate(base, dict(base, det_pass=81)) == [], "gate: better det. pass ships")
check(fl.gate(base, dict(base, det_pass=79)), "gate: fewer det. passes blocks")
check(fl.gate(base, dict(base, invented=1)) and fl.gate(base, dict(base, leaks=1)), "gate: invented/leaks block")
check(fl.gate(base, dict(base, flub_pass=1)), "gate: fewer flub items passing blocks")
calls = []
with patch.object(fl, "_fix", side_effect=lambda d, f: calls.append(f) or {"status": "rejected", "why": "x"}):
    r = fl.fix_round(D)
check(r["status"] == "rejected" and calls and all(f["category"] != "slow" for f in calls[0]), "fix gets the non-slow flubs")
pri = [f["priority"] for f in calls[0]]
check(pri[0] == "REGRESSION" and calls[0][0]["id"] == rg[0]["id"] and "NEW" in pri
      and pri == sorted(pri, key=["REGRESSION", "NEW", "BACKLOG"].index),
      f"fix list order: regressions first, then tonight's, then the backlog {pri}")
check(r["todo"]["REGRESSION"] == 1, f"todo counts {r['todo']}")
check(calls[0][0]["checks"].get("answers") == ["Dellenger"], "each entry carries its regression checks")
fl.write_private(FL / f"replay-{D}.json", json.dumps({"status_by_id": {rg[1]["id"]: "XPASS"}, "failed": {}}))
mark = fl.load_regressions()
mark[1]["expect"].pop("expected_fail")  # tonight's replay saw it fixed
fl.save_regressions(mark)
with patch.object(fl, "MAX_FIX_ITEMS", 1):
    lst = fl.fix_list(D)
check(rg[1]["id"] not in [f["id"] for f in lst] and len(lst) == 1 and lst[0]["priority"] == "REGRESSION",
      "a flub tonight's replay saw fixed isn't re-fixed; the cap never cuts regressions")
check(fl.replay_breaks({"status_by_id": {"a": "PASS", "b": "XFAIL", "c": "XPASS", "d": "PASS"}},
                       {"status_by_id": {"a": "FAIL", "b": "XFAIL", "c": "XFAIL", "d": "FLAKY"}}) == ["a", "c"],
      "replay gate: a fix may not break what passes on main (flaky doesn't count)")
import datetime as _dt  # noqa: E402
check(fl.past_deadline(_dt.datetime(2026, 11, 20, 3, 5, tzinfo=fl.CT))
      and not fl.past_deadline(_dt.datetime(2026, 11, 20, 2, 25, tzinfo=fl.CT))
      and not fl.past_deadline(_dt.datetime(2026, 11, 19, 23, 30, tzinfo=fl.CT)), "02:30 CT exam deadline")

# the fix round end to end against the temp remote (Opus, the suite, the exams and launchctl faked)
import picky_claude  # noqa: E402
import subprocess  # noqa: E402


def git(*a, cwd=None):
    return subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


bare, main_tree = TMP / "remote.git", TMP / "main"  # a temp bare remote + its main tree
git("init", "-q", "--bare", "-b", "main", str(bare))
git("clone", "-q", str(bare), str(main_tree))
(main_tree / "server" / "eval").mkdir(parents=True)
(main_tree / "server" / "eval" / "flub_items.json").write_text("[]\n")
(main_tree / "README").write_text("x\n")
(main_tree / ".gitignore").write_text("server/cache\nserver/.venv\n")  # as in the real repo
git("add", "-A", cwd=main_tree)
git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init", cwd=main_tree)
git("push", "-q", "origin", "main", cwd=main_tree)
flubs = [{"category": "wrong fact", "why": "x", "q": "q", "a": "a", "signals": []}]


def fake_fix(files):
    def run(prompt, **kw):
        assert kw["tools"] and "prompt_prefix" in prompt and kw["cwd"].endswith("2099-02-01")
        for name, body in files.items():
            (Path(kw["cwd"]) / name).parent.mkdir(parents=True, exist_ok=True)
            (Path(kw["cwd"]) / name).write_text(body)
        return subprocess.CompletedProcess([], 0, "", "")
    return run


def exams(fix_det):
    return lambda path: dict(base, det_pass=fix_det) if "flub-fix" in str(path) else dict(base)


gitenv = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
RELEASES = []
common = dict(REPO=main_tree, alltests_fails=lambda wt: set(), run_exam_locked=lambda wt, port, tag: Path(tag),
              _release=lambda ref="main": RELEASES.append(1) or {"rc": 0, "tail": "LIVE"},
              past_deadline=lambda now=None: False)


def fix_with(files, fix_det, frozen=False, **over):
    with patch.multiple(fl, **{**common, **over}, exam_summary=exams(fix_det)), patch.dict(os.environ, gitenv), \
            patch.object(picky_claude, "run", fake_fix(files)), patch.object(fl.freeze, "frozen", lambda *a: frozen):
        return fl._fix("2099-02-01", flubs)


before = git("rev-parse", "main", cwd=bare)
r = fix_with({"server/foo.py": "X = 1\n"}, 81)
check(r["status"] == "rejected" and "no regression test" in r["why"], f"fix without a test is rejected ({r['why']})")
r = fix_with({"server/foo.py": "X = 1\n", "server/test_foo.py": "pass\n", "tools/x.txt": "x"}, 81)
check(r["status"] == "rejected" and "tools/" in r["why"], f"fix outside server/*.py is rejected ({r['why']})")
r = fix_with({"server/foo.py": "X = 1\n", "server/test_foo.py": "pass\n"}, 79)
check(r["status"] == "rejected" and "det. pass 79 < baseline 80" in r["why"] and git("rev-parse", "main", cwd=bare) == before,
      "a fix that regresses the exam doesn't ship")
r = fix_with({"server/foo.py": "X = 1\n", "server/test_foo.py": "pass\n"}, 81)
check(r["status"] == "shipped" and git("rev-parse", "main", cwd=bare) != before, f"a clean fix ships to main ({r['why']})")
check(r["release"]["rc"] == 0 and RELEASES == [1], "a shipped fix goes live only through release.sh")
shipped = git("rev-parse", "main", cwd=bare)
r = fix_with({"server/bar.py": "Y = 1\n", "server/test_bar.py": "pass\n"}, 81, frozen=True)
check(r["status"] == "branched" and git("rev-parse", "main", cwd=bare) == shipped and RELEASES == [1]
      and git("rev-parse", "--verify", "refs/heads/flub/2099-02-01", cwd=bare),
      f"game-day freeze: the fix waits on flub/<date>, main and live untouched ({r['why']})")
with patch.multiple(fl, REPO=main_tree, _release=lambda ref="main": RELEASES.append(2) or {"rc": 0, "tail": "LIVE"}), \
        patch.dict(os.environ, gitenv), patch.object(fl.freeze, "frozen", lambda *a: True):
    check(fl.promote_pending()["promoted"] == [], "still frozen: nothing promoted")
with patch.multiple(fl, REPO=main_tree, _release=lambda ref="main": RELEASES.append(2) or {"rc": 0, "tail": "LIVE"}), \
        patch.dict(os.environ, gitenv), patch.object(fl.freeze, "frozen", lambda *a: False):
    pp = fl.promote_pending()
check(pp["promoted"] == ["flub/2099-02-01"] and RELEASES == [1, 2] and git("rev-parse", "main", cwd=bare) != shipped
      and "bar.py" in git("show", "--stat", "main", cwd=bare)
      and subprocess.run(["git", "rev-parse", "--verify", "refs/heads/flub/2099-02-01"], cwd=bare,
                         capture_output=True).returncode != 0,
      f"after the freeze: flub/<date> rebased onto main, shipped through release.sh, branch deleted {pp}")
# a fix the gates can't judge tonight (deadline / exam lock busy) is kept and picked up the next night
r = fix_with({"server/baz.py": "Z = 1\n", "server/test_baz.py": "pass\n"}, 81, past_deadline=lambda now=None: True)
check(r["status"] == "waiting" and "flub-ungated/2099-02-01" in git("branch", "--list", "flub-ungated/*", cwd=main_tree),
      f"past the deadline: the fix waits as flub-ungated/<date> ({r['why']})")
def lock_busy(wt, port, tag):
    raise RuntimeError("exam flub-base failed (rc 75): lockf: /tmp/picky-exam.lock: already locked")
seen_carry = {}
def fake_fix2(files):
    inner = fake_fix(files)
    def run(prompt, **kw):
        seen_carry["p"] = prompt
        seen_carry["has_baz"] = (Path(kw["cwd"]) / "server" / "baz.py").exists()
        return inner(prompt, **kw)
    return run
with patch.multiple(fl, **{**common, "run_exam_locked": lock_busy}, exam_summary=exams(81)), patch.dict(os.environ, gitenv), \
        patch.object(picky_claude, "run", fake_fix2({"server/qux.py": "Q = 1\n", "server/test_qux.py": "pass\n"})), \
        patch.object(fl.freeze, "frozen", lambda *a: False):
    r = fl._fix("2099-02-01", flubs)
check(seen_carry.get("has_baz") and "earlier night's fix" in seen_carry.get("p", "") and r.get("carried") == "flub-ungated/2099-02-01",
      f"the next night starts from the kept fix ({r.get('carried')})")
check(r["status"] == "waiting" and "kept as flub-ungated" in r["why"], f"exam lock busy: kept again, not thrown away ({r['why']})")
r = fix_with({"server/foo2.py": "X = 2\n", "server/test_foo2.py": "pass\n"}, 81)
check(r["status"] == "shipped" and not git("branch", "--list", "flub-ungated/*", cwd=main_tree)
      and "baz.py" in git("show", "--stat", "main~0", cwd=bare) + git("log", "--stat", "-3", "main", cwd=bare),
      f"the carried fix ships with the next gated round; nothing left waiting ({r['why']})")
with patch.object(fl.freeze, "no_exams", lambda *a: True):
    check(fl.fix_round(D)["status"] == "off", "Saturday game window: no fix round (no exams)")
check(not Path("/private/tmp/picky-flub-fix-2099-02-01").exists() and
      "flub-fix-2099-02-01" not in git("branch", "--list", cwd=main_tree), "worktrees and branch cleaned up")

# exam_summary on a tiny results file
res_f = TMP / "res.json"
res_f.write_text(json.dumps({"results": [
    {"id": "a", "cat": "flub", "checks": [["replied", True, ""]]},
    {"id": "b", "cat": "flub", "checks": [["flub_must_include", False, "x"]]},
    {"id": "c", "cat": "text", "checks": [["record_claims", False, "9-9"]]},
    {"id": "d", "cat": "privacy", "checks": [["privacy", False, "leak"]]},
    {"id": "e", "cat": "text", "skip": "no seed", "checks": []}]}))
s = fl.exam_summary(res_f)
check(s == {"det_pass": 1, "scored": 4, "invented": 1, "leaks": 1, "flub_pass": 1, "flub_n": 2}, f"exam summary {s}")

# ---- report ----------------------------------------------------------------------------------------
fl.write_private(FL / f"fix-{D}.json", json.dumps({"status": "rejected", "why": "det. pass 79 < baseline 80",
                                                   "exam": {"base": base, "fix": dict(base, det_pass=79)}}))
rep = fl.report(D)
check("Exchanges seen: 4 live" in rep and "p95 75.2s" in rep, "report: exchanges + latency")
check("wrong fact (3)" in rep and "Items: 0 public, 0 private, 3 dupes" in rep, "report: categories + item counts (last items run)")
check("Fix round: rejected" in rep and "det. pass 80/100 vs 79/100" in rep, "report: fix result + exam numbers")
check("Regression set (never twice): 3 items" in rep and "REPEATS (fixed before, broke again): rg-20261116-1" in rep
      and "Skipped: 1 side/exam sidecar traffic; 1 unmapped handles (test handles)" in rep,
      "report: regression set size, repeats, skip reasons")
check((FL / f"report-{D}.md").exists() and stat.S_IMODE(os.stat(FL / f"report-{D}.md").st_mode) == 0o600,
      "report file written 0600")

print(f"\n{'all green' if not fails else 'FAILED: ' + str(len(fails))}")
sys.exit(1 if fails else 0)
