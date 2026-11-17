"""
real_replay.py (real-traffic regression replay), model-free: fixture build from an exchange log (group
only, mapped teams, side traffic skipped, labels kept on rebuild), the code grader (lane, privacy leak,
sub-question answers, pool-line + record facts, must_not, length), PASS/FAIL/XFAIL/XPASS, newly-broken
and flipped diffs, the flag-queue record for a regression (source "replay", harvest can target it), the
flub_loop hook, and the private 0600 report. Synthetic names and temp paths only.

Run:  ./.venv/bin/python3 -I test_real_replay.py
"""
from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
TMP = Path(tempfile.mkdtemp())
os.environ.update({"PICKY_REPLAY_DIR": str(TMP / "replay"), "PICKY_FLUB_QUEUE": str(TMP / "flubs" / "queue.jsonl"),
                   "PICKY_FLUB_DIR": str(TMP / "flubs"), "PICKY_EXCHANGE_LOG": str(TMP / "ex.jsonl")})
sys.path.insert(0, str(HERE))
import flag_flub  # noqa: E402
import real_replay as rr  # noqa: E402

fails = []


def check(cond, msg):
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond:
        fails.append(msg)


class FakeTruth:
    """Just what eval/checks' privacy + record graders and line_claims read."""
    members = ["Alpha Dogs", "Beta Blockers", "Gamma Rays"]
    games = [{"id": 1, "away": "Northwestern", "home": "Minnesota", "line": 2.5, "start": "2026-11-21T11:00:00"},
             {"id": 2, "away": "Iowa", "home": "Illinois", "line": 8.5, "start": "2026-11-21T13:30:00"}]
    hidden_games = games  # pre-lock: every game's picks hidden

    def neutral_scores(self):
        return set()

    def known_records(self, team):
        return {"Alpha Dogs": {"30-18"}, "Beta Blockers": {"27-21"}}.get(team, set())


T = FakeTruth()

# ---- build ---------------------------------------------------------------------------------------------
import datetime as dt  # noqa: E402
noon = dt.datetime(2026, 11, 17, 12, 5, tzinfo=rr.CT).timestamp()
rows = [{"ts": noon, "mode": "group", "team": "Alpha Dogs", "q": "how many slots?", "a": "Pull the lever.", "lane": "fact:rules2", "secs": 5.1},
        {"ts": noon + 10, "mode": "dm", "team": "Alpha Dogs", "q": "dm ask", "a": "x"},
        {"ts": noon + 20, "mode": "group", "team": None, "q": "test handle", "a": "x"},
        {"ts": noon + 30, "mode": "group", "team": "Gamma Rays", "q": "side ask", "a": "x", "side": 1},
        {"ts": noon - 3 * 3600, "mode": "group", "team": "Gamma Rays", "q": "morning ask", "a": "x"},
        {"ts": noon + 40, "mode": "group", "team": "Beta Blockers", "q": "rate his looks", "a": "Iowa -8.5 is a 5", "lane": "fact:conf"}]
fx = rr.build_fixtures("2026-11-17", "11:30", rows=rows)
check([f["q"] for f in fx] == ["how many slots?", "rate his looks"], "build: group asks from a mapped team after --since only")
check(fx[0]["id"] == "20261117-g01" and fx[0]["original"]["lane"] == "fact:rules2", "build: ids + original reply/lane")
fx[1]["expect"] = {"lanes": ["banter"], "expected_fail": "q-x"}
again = rr.build_fixtures("2026-11-17", "11:30", rows=rows, old=fx)
check(again[1]["expect"].get("expected_fail") == "q-x", "build: hand labels survive a rebuild")

# ---- grader --------------------------------------------------------------------------------------------
def res(fx_, reply, lane):
    return {n: (ok, d) for n, ok, d in rr.grade(fx_, reply, lane, T)}

f_banter = {"id": "a", "team": "Gamma Rays", "q": "rate his looks", "expect": {"lanes": ["banter"], "must_not": [r"Iowa\s*[+-]\d"]}}
r = res(f_banter, "A solid 11, the Supreme Commissioner breaks the scale.", "banter:roast")
check(all(ok for ok, _ in r.values()), "grader: clean banter reply passes every check")
r = res(f_banter, "Iowa -8.5 is a 5 out of 10.", "fact:conf")
check(not r["lane"][0] and "fact:conf" in r["lane"][1], "grader: wrong lane fails (lane prefix compare)")
check(not r["facts"][0] and "must_not" in r["facts"][1], "grader: must_not pattern fails facts")
check(res({"id": "x", "expect": {"lanes": ["fact:rules2"]}}, "ok.", "fact:rules2")["lane"][0], "grader: full lane label matches")

r = res({"id": "b", "team": "Gamma Rays", "q": "who's Beta on"}, "Beta Blockers took Iowa, bold.", "full")
check(not r["privacy"][0], "grader: a member tied to a side before lock is a privacy leak")
r = res({"id": "b", "team": "Gamma Rays", "q": "who's Beta on"}, "Beta Blockers's picks stay hidden till kickoff.", "full")
check(r["privacy"][0], "grader: 'hidden till kickoff' is not a leak")

two = {"id": "c", "team": "Gamma Rays", "q": "thoughts on Finebaum and the SEC network?",
       "expect": {"answers": [r"finebaum", r"\bSEC\b|ESPN"]}}
check(not res(two, "The SEC network is a circus.", "full")["answers"][0], "grader: multi-part ask missing a part fails")
check(res(two, "Finebaum is the SEC's town crier.", "full")["answers"][0], "grader: every part answered passes")

r = res({"id": "d", "team": "Gamma Rays", "q": "lines?"}, "Northwestern -2.5 and Illinois +8.5 this week.", "fact:line")
check(r["facts"][0], "grader: pool lines that match the cache pass (away -line, home +line)")
r = res({"id": "d", "team": "Gamma Rays", "q": "lines?"}, "Northwestern -3 and Iowa +8.5.", "fact:line")
check(not r["facts"][0] and "Northwestern -3" in r["facts"][1] and "Iowa +8.5" in r["facts"][1],
      "grader: a wrong pool line (number or side) fails facts")
r = res({"id": "e", "team": "Gamma Rays", "q": "how's Beta"}, "Beta Blockers is 26-22 and fading.", "full")
check(not r["facts"][0] and "record" in r["facts"][1], "grader: a false member record fails facts")
check(res({"id": "e", "team": "Gamma Rays", "q": "how's Beta"}, "Beta Blockers is 27-21 and fading.", "full")["facts"][0],
      "grader: a true member record passes")

check(not res({"id": "f", "q": "x"}, "word " * 120, "full")["length"][0], "grader: over the group char cap fails length")
check(not res({"id": "f", "q": "x", "expect": {"max_words": 5}}, "one two three four five six", "banter")["length"][0],
      "grader: over max_words fails length")
check(not res({"id": "g", "q": "x"}, "", None)["no_error"][0], "grader: no reply fails no_error")

# ---- status + diffs --------------------------------------------------------------------------------------
ok_res, bad_res = [("no_error", True, ""), ("lane", True, "")], [("no_error", True, ""), ("lane", False, "got full")]
check(rr.status({"expect": {}}, ok_res) == "PASS" and rr.status({"expect": {}}, bad_res) == "FAIL", "status: PASS / FAIL")
xf = {"expect": {"expected_fail": "q-1"}}
check(rr.status(xf, bad_res) == "XFAIL" and rr.status(xf, ok_res) == "XPASS", "status: expected-fail -> XFAIL / XPASS")
check(rr.status({"expect": {}}, [("no_error", False, "no reply")]) == "ERROR", "status: no reply = ERROR (never a regression)")
prev = {"rows": [{"id": "1", "status": "PASS"}, {"id": "2", "status": "XPASS"}, {"id": "3", "status": "FAIL"},
                 {"id": "4", "status": "XFAIL"}, {"id": "5", "status": "PASS"}]}
cur = {"rows": [{"id": "1", "status": "FAIL"}, {"id": "2", "status": "XFAIL"}, {"id": "3", "status": "FAIL"},
                {"id": "4", "status": "XPASS"}, {"id": "5", "status": "ERROR"}]}
check([r["id"] for r in rr.newly_broken(prev, cur)] == ["1", "2"], "diff: newly broken = passed before, fails now (not ERROR)")
check(rr.newly_broken(None, cur) == [], "diff: first run queues nothing")
check([r["id"] for r in rr.flipped(prev, cur)] == ["4"], "diff: expected-fail flipped to pass is reported")
s = rr.summarize([{"status": "PASS", "secs": 4}, {"status": "XPASS", "secs": 30}, {"status": "XFAIL", "secs": 9}])
check(s["pass"] == 2 and s["n"] == 3 and s["pass_rate"] == 0.667, "summary: pass rate counts PASS + XPASS")

# ---- regression -> flag queue (next night's harvest) -----------------------------------------------------
fxq = {"id": "20261117-g09", "team": "Gamma Rays", "q": "rate his looks", "expect": {"lanes": ["banter"], "why": "dodge"}}
row = {"id": fxq["id"], "reply": "Iowa -8.5 is a 5.", "lane": "fact:conf", "secs": 4.2,
       "checks": [("lane", False, "got fact:conf, want banter")]}
qid = rr.queue_regression(row, fxq)
recs = [json.loads(x) for x in flag_flub.queue_path().read_text().splitlines()]
check(len(recs) == 1 and recs[0]["id"] == qid and recs[0]["source"] == "replay" and recs[0]["priority"] == "high",
      "queue: regression logged as a high-priority 'replay' flag")
check("Replay regression 20261117-g09" in recs[0]["note"] and recs[0]["context"][0]["q"] == "rate his looks",
      "queue: note names the fixture + failed checks; context carries the replayed exchange")
check(stat.S_IMODE(os.stat(flag_flub.queue_path()).st_mode) == 0o600, "queue: stays 0600")
check(rr.already_queued("20261117-g09") and not rr.already_queued("20261117-g01"), "queue: no double-queue while unconsumed")
import flub_loop  # noqa: E402
target, ctx = flub_loop._queue_target(recs[0], [])
check(target is None and ctx and ctx["a"] == "Iowa -8.5 is a 5.", "queue: harvest targets the replayed reply from the context")

# ---- report + nightly hook --------------------------------------------------------------------------------
rep = rr.grade_all([f_banter], {"a": {"reply": "Iowa -8.5 is a 5.", "lane": "fact:conf", "secs": 3.0}}, T, "replay", "abc123")
p = rr.save_report(rep)
check(p.exists() and stat.S_IMODE(os.stat(p).st_mode) == 0o600 and p.with_suffix(".md").exists(), "report: json + md, 0600")
check("**FAIL** a" in p.with_suffix(".md").read_text(), "report: markdown lists each fixture's status")
check(rr.previous_report("replay")["stamp"] == rep["stamp"], "report: previous_report finds the last replay")
src = (HERE / "flub_loop.py").read_text()
check("replay_after_fix(date, dry)" in src and src.index("fix_round(date, dry)\n    replay_after_fix") > 0,
      "nightly hook: flub_loop.nightly runs the replay right after the fix round")

print(f"\n{'all green' if not fails else 'FAILED: ' + str(len(fails))}")
sys.exit(1 if fails else 0)
