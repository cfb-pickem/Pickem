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
                   "PICKY_FLUB_KILL_FILE": str(TMP / "FLUB_AUTOFIX_OFF"), "PICKY_FLUB_PUBLISH": "0",
                   "PICKY_GROUP_MEMBERS": str(TMP / "members.json"), "PICKY_CLAUDE_USAGE_DIR": str(TMP / "usage")})
os.environ.pop("PICKY_NO_ANTHROPIC", None)
os.environ.pop("PICKY_FLUB_AUTOFIX", None)
sys.path.insert(0, str(HERE))
import flag_flub  # noqa: E402
import flub_loop as fl  # noqa: E402

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
check(h["skipped"] == {"test_handle": 1, "side": 1, "unmatched": 1}, f"skips: test handle, side, old unmatched {h['skipped']}")
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
check(cv["by"] == "code" and cv["note"] == "PICKY_NO_ANTHROPIC", "kill switch: code-only signals")
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
check(a["public"] == ["flub-20261116-1", "flub-20261116-2"], f"public ids {a['public']}")
check(a["private"] == ["flub-20261116-3"], f"private ids {a['private']}")
check(any("Iowa -7.5" in d["why"] and "not in the facts bundle" in d["why"] for d in a["dropped"]),
      "a key not in the facts bundle drops the item")
pub = json.loads((TMP / "flub_items.json").read_text())
prv = json.loads((FL / "items-private.json").read_text())
check(pub[0]["turns"] == ["whats the latest news"], "@Picky stripped from turns")
check(all(r["persona"] == "mid" for r in pub), "public items ask as a generic member")
check(not any(n in json.dumps(pub) for n in ("Alpha", "Beta", "Charlie", "30-18")), "no member names/records in public")
check(prv[0]["persona"] == "me" and prv[0]["must_include"] == ["30-18"], "record item went private with its persona")
check(stat.S_IMODE(os.stat(FL / "items-private.json").st_mode) == 0o600, "private items file is 0600")
check(pub[1]["expires"] == "2026-11-21", "expiry kept")
again = fl.items(D)
check(not again["public"] and not again["private"] and again["dupes"] == 3, "rerun: deduped, no new ids")
nxt = fl.merge_items("2026-11-16", [{"turns": ["brand new q"], "must_not": ["x y"], "private": False}])
check(nxt["public"] == ["flub-20261116-4"], f"ids continue past the highest (stable) {nxt['public']}")
fl.PUBLIC_ITEMS.write_text(json.dumps(pub, indent=1))  # back to the 2 public items

# publish: public items reach main through a throwaway worktree (a temp bare remote here)
import subprocess  # noqa: E402


def git(*a, cwd=None):
    return subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


bare, main_tree = TMP / "remote.git", TMP / "main"
git("init", "-q", "--bare", "-b", "main", str(bare))
git("clone", "-q", str(bare), str(main_tree))
(main_tree / "server" / "eval").mkdir(parents=True)
(main_tree / "server" / "eval" / "flub_items.json").write_text("[]\n")
(main_tree / "README").write_text("x\n")
git("add", "-A", cwd=main_tree)
git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init", cwd=main_tree)
git("push", "-q", "origin", "main", cwd=main_tree)
newi = [{"turns": ["published q"], "must_not": ["x y"], "private": False, "persona": "mid"},
        {"turns": ["private q"], "must_not": ["x y"], "private": True, "persona": "me"}]
with patch.object(fl, "REPO", main_tree), patch.dict(os.environ, {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                                                                  "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}):
    pr = fl.publish("2099-01-01", newi)
    (main_tree / "README").write_text("dirty\n")
    pr2 = fl.publish("2099-01-02", [dict(newi[0], turns=["second q"])])
remote_items = json.loads(git("show", "main:server/eval/flub_items.json", cwd=bare))
check(pr.get("pushed") and pr["main_tree"] == "fast-forwarded" and pr["public"] == ["flub-20990101-1"],
      f"publish: pushed + main tree fast-forwarded {pr}")
check([r["turns"] for r in remote_items] == [["published q"], ["second q"]], "publish: items on the remote's main")
check(pr["private"] == ["flub-20990101-2"] and "published q" not in fl.PRIVATE_ITEMS.read_text(),
      "publish: private item stays in the private file")
check(pr2["main_tree"] == "dirty: not fast-forwarded" and "second q" not in
      (main_tree / "server" / "eval" / "flub_items.json").read_text(), "publish: a dirty main tree isn't touched")
check(not Path("/private/tmp/picky-flub-items-2099-01-01").exists(), "publish: worktree removed")
with patch.object(fl, "REPO", TMP / "no-repo"):
    pe = fl.publish("2099-01-03", [dict(newi[1], turns=["private q3"]), dict(newi[0], turns=["pub q3"])])
check(pe.get("publish_error") and pe["private"] and pe["unpublished"][0]["turns"] == ["pub q3"],
      "publish failure: private items still written, public ones reported unpublished")
prv_keep = json.loads(fl.PRIVATE_ITEMS.read_text())
fl.write_private(fl.PRIVATE_ITEMS, json.dumps([r for r in prv_keep if not r["id"].startswith("flub-2099")]))

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
    check(fl.fix_round(D)["why"] == "PICKY_NO_ANTHROPIC", "Anthropic off: no fix round")
base = {"det_pass": 80, "scored": 100, "invented": 0, "leaks": 0, "flub_pass": 2, "flub_n": 3}
check(fl.gate(base, dict(base, det_pass=81)) == [], "gate: better det. pass ships")
check(fl.gate(base, dict(base, det_pass=79)), "gate: fewer det. passes blocks")
check(fl.gate(base, dict(base, invented=1)) and fl.gate(base, dict(base, leaks=1)), "gate: invented/leaks block")
check(fl.gate(base, dict(base, flub_pass=1)), "gate: fewer flub items passing blocks")
calls = []
with patch.object(fl, "_fix", side_effect=lambda d, f: calls.append(f) or {"status": "rejected", "why": "x"}):
    r = fl.fix_round(D)
check(r["status"] == "rejected" and calls and all(f["category"] != "slow" for f in calls[0]), "fix gets the non-slow flubs")

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
check((FL / f"report-{D}.md").exists() and stat.S_IMODE(os.stat(FL / f"report-{D}.md").st_mode) == 0o600,
      "report file written 0600")

print(f"\n{'all green' if not fails else 'FAILED: ' + str(len(fails))}")
sys.exit(1 if fails else 0)
