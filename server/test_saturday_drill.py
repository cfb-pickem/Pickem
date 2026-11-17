"""
saturday_drill.py, model-free: the sandbox (group_outbox never points at the real private dir; the
config copy has dry_run off; the real outbox/send-log check flags any drill key), replayed play-by-play
shifted onto Saturday's kickoffs, the week-12 live cache built from it (no fixture names), the audits
(cover: max 2 per game, last 8:00 only, no kneel, no final, not under 30 s; group: no final-score post;
nudges: once per member per slot, mapped only, before lock - 5 min, nobody else named), the trap
moments, and the latency stats. Temp dirs and synthetic names only.

Run:  ./.venv/bin/python3 -I test_saturday_drill.py
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
FAKE_REAL = Path(tempfile.mkdtemp(prefix="drill-fake-real-"))
(FAKE_REAL / "picky_outbox").mkdir()
(FAKE_REAL / "picky_group_config.json").write_text(json.dumps({"group_guid": "any;+;g1", "dry_run": True}))
(FAKE_REAL / "picky_group_members.json").write_text(json.dumps({"members": [{"team": "Alpha Dogs", "handles": ["+15555550101"]}]}))
(FAKE_REAL / "picky_group_sends.jsonl").write_text('{"event": "delivered", "key": "old"}\n')

import saturday_drill as sd  # noqa: E402

fails = []


def check(cond, msg):
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond:
        fails.append(msg)


# ---- sandbox -----------------------------------------------------------------------------------------------
sb = sd.make_sandbox(FAKE_REAL)
check(os.environ["PICKY_PRIVATE_DIR"] == str(sb) and sb != FAKE_REAL, "sandbox: PICKY_PRIVATE_DIR points at a temp copy")
cfg = json.loads((sb / "picky_group_config.json").read_text())
check(cfg["dry_run"] is False and cfg["group_guid"] == "any;+;g1", "sandbox: config copy queues for real (dry_run off), same group")
check((sb / "picky_group_members.json").exists() and os.environ["PICKY_NO_ANTHROPIC"] == "1", "sandbox: members copied, no Anthropic")
import group_outbox as ob  # noqa: E402  (after the sandbox env, as run_drill does)
sd.assert_sandboxed(ob, FAKE_REAL)
check(sb in ob.OUTBOX.parents, "sandbox: group_outbox's outbox is inside the sandbox")
try:
    sd.assert_sandboxed(ob, ob.PRIVATE)
    check(False, "sandbox: refuses when group_outbox points at the real dir")
except SystemExit:
    check(True, "sandbox: refuses when group_outbox points at the real dir")
before = sd.real_drain_snapshot(FAKE_REAL)
r = ob.post("cover", ["Cover watch: test"], "cover:x#1", now=dt.datetime(2026, 11, 21, 14, 0, tzinfo=sd.CT))
check(r == "queued" and len(list(ob.OUTBOX.glob("*.json"))) == 1, "sandbox: a drill post is queued in the sandbox outbox")
chk = sd.real_drain_check(before, {"cover:x#1"}, FAKE_REAL)
check(chk["ok"] and chk["from_drill"] == 0 and not list((FAKE_REAL / "picky_outbox").iterdir()), "real drain: untouched")
with open(FAKE_REAL / "picky_group_sends.jsonl", "a") as f:
    f.write('{"event": "queued", "key": "remind:2026:12:sat"}\n{"event": "queued", "key": "cover:x#1"}\n')
chk = sd.real_drain_check(before, {"cover:x#1"}, FAKE_REAL)
check(chk["new_real_events"] == 2 and chk["from_drill"] == 1 and not chk["ok"], "real drain: a drill key in the real send log is caught")

# ---- replayed games on Saturday's clock --------------------------------------------------------------------
kicks = [dt.datetime(2026, 11, 21, h, m, tzinfo=sd.CT).timestamp() for h, m in ((11, 0), (13, 30), (14, 30), (14, 30))]
sim = sd.CoverSim(sd.FIXTURE, kicks)
check(len(sim.games) == 4 and all(abs(g["ts"][0] - k) < 1 for g, k in zip(sim.games, kicks)),
      "replay: 4 pool games, first play on each kickoff slot")
pre = sim.snaps(kicks[0] - 60)
check(all(s is None for _g, s in pre), "replay: nothing on the board before kickoff")
late = sim.snaps(kicks[-1] + 5 * 3600)
check(all(s and s["state"] == "post" for _g, s in late), "replay: every game final by the evening")
template = {"pool_ids": ["1", "2", "3", "4"], "games": {
    str(i): {"id": str(i), "away": {"name": f"Away{i}"}, "home": {"name": f"Home{i}"}, "state": "pre",
             "start": f"2026-11-21T{16 + i}:00Z", "last_play": "fixture text", "away_score": 0, "home_score": 0}
    for i in range(1, 5)}}
live = sd.week12_live(template, sim, kicks[2] + 3600, real_now=1000.0)
g1 = live["games"]["1"]
check(g1["state"] == "post" and live["games"]["3"]["state"] == "in" and g1["ts"] == 1000.0
      and g1["away"]["name"] == "Away1" and "last_play" not in g1,
      "live cache: week-12 rows carry replayed state, fresh ts, their own names, no fixture play text")
check(all(live["games"][k]["state"] in ("in", "post") for k in "1234"), "live cache: all four games under way an hour after the last kick")

# ---- audits ------------------------------------------------------------------------------------------------
good = {"gid": "g", "n": 1, "state": "in", "period": 4, "clock_s": 300, "kneeling": False, "text": "Cover watch: 21-17, 5:00 left."}
check(sd.audit_cover([good, dict(good, n=2)]) == [], "audit cover: two posts in the window pass")
bad = sd.audit_cover([good, dict(good, n=2), dict(good, n=3)])
check(any("post #3" in b for b in bad), "audit cover: a third post is caught")
check(any("outside" in b for b in sd.audit_cover([dict(good, clock_s=600)])), "audit cover: Q4 10:00 is outside the window")
check(any("outside" in b for b in sd.audit_cover([dict(good, period=3, clock_s=100)])), "audit cover: Q3 is outside the window")
check(sd.audit_cover([dict(good, period=5, clock_s=None)]) == [], "audit cover: OT is in the window")
check(any("kneel" in b for b in sd.audit_cover([dict(good, kneeling=True)])), "audit cover: a kneel is caught")
check(any("state post" in b for b in sd.audit_cover([dict(good, state="post")])), "audit cover: a final state is caught")
check(any("reads like a final" in b for b in sd.audit_cover([dict(good, text="Final: Iowa covers.")])), "audit cover: final wording")
check(sd.audit_cover([dict(good, text="Iowa needs a stop on the final drive.")]) == [], "audit cover: 'final drive' isn't a final")
check(any("12s left" in b for b in sd.audit_cover([dict(good, clock_s=12)])), "audit cover: under 30 s is caught")
ev = [{"event": "queued", "kind": "remind", "key": "remind:1", "texts": ["Picks lock at 11."]},
      {"event": "queued", "kind": "ping", "key": "ping:9", "texts": ["Final: Iowa covers."]},
      {"event": "queued", "kind": "saturday", "key": "sat:1", "texts": ["That's final: Iowa 24, Illinois 17."]}]
gb = sd.audit_group_posts(ev)
check(len(gb) == 2 and "ping:9" in gb[0] and "sat:1" in gb[1], "audit group: final-score pings and final wording are caught")
lock = dt.datetime(2026, 11, 21, 11, 0, tzinfo=sd.CT)
dm = lambda team, slot, t, txt="no picks from you yet": {"event": "dm-queued", "key": f"nudge:2026:12:{slot}:{team}",  # noqa: E731
                                                        "sim": t, "texts": [txt]}
ok_dms = [dm("Alpha Dogs", "first", "2026-11-21T09:00:00-06:00"), dm("Alpha Dogs", "second", "2026-11-21T10:30:00-06:00")]
check(sd.audit_nudges(ok_dms, {"Alpha Dogs"}, {"Alpha Dogs", "Beta Blockers"}, lock) == [], "audit nudges: one per slot passes")
nb = sd.audit_nudges(ok_dms + [dm("Alpha Dogs", "first", "2026-11-21T09:05:00-06:00"),
                               dm("Gamma Rays", "first", "2026-11-21T09:00:00-06:00"),
                               dm("Beta Blockers", "second", "2026-11-21T10:57:00-06:00"),
                               dm("Beta Blockers", "first", "2026-11-21T09:00:00-06:00", "Alpha Dogs already picked")],
                     {"Alpha Dogs", "Beta Blockers"}, {"Alpha Dogs", "Beta Blockers", "Gamma Rays"}, lock)
check(any("two first" in b for b in nb) and any("not a mapped" in b for b in nb) and any("10:57" in b for b in nb)
      and any("names Alpha Dogs" in b for b in nb), "audit nudges: double, unmapped, too late and naming another member caught")

# ---- traps + stats -----------------------------------------------------------------------------------------
traps = sd.trap_results()
check(all(t["ok"] for t in traps) and traps[0]["posted"] and sum(t["posted"] for t in traps) == 1,
      "traps: only the control posts (kneel, final, 9:30, third alert, 0:12 don't)")
rows = [{"s": float(x), "lane": "fact:score" if x < 10 else "full", "reply": "r"} for x in range(1, 21)]
st = sd.latency_stats(rows)
check(st["p50"] == 10.5 and st["p90"] == 18.0 and st["max"] == 20.0 and st["lanes"]["fact"]["n"] == 9,
      "latency: p50 / nearest-rank p90 / max / per lane")
check(sd.latency_stats(rows + [{"s": 30.0, "lane": None, "reply": "[ERROR x]"}])["errors"] == 1, "latency: errors counted")
wm = sd.watcher_model([{"own_s": 5.0}] * 3)
check(wm["answered"] == 3 and wm["p50"] == 16.0 and wm["max"] == 26.0, "watcher model: serial asks + 10 s gap between sends")
wm = sd.watcher_model([{"own_s": 40.0}] * 20)
check(wm["dropped_stale"] == 5 and wm["answered"] == 15, "watcher model: rows reached after 600 s are skipped as stale")
check(sd.watcher_model([{"own_s": 40.0}] * 20, stale_at_fetch=True)["dropped_stale"] == 0, "watcher model: stale judged at fetch drops none")
check(len(sd.BURST_ASKS) >= 20 and not any("@" in q for q in sd.BURST_ASKS), "burst: 20 generic asks, no handles")

print(f"\n{'all green' if not fails else 'FAILED: ' + str(len(fails))}")
sys.exit(1 if fails else 0)
