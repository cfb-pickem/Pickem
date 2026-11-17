"""
Straggler nudges (straggler_nudges.py) + the outbox's DM items: timing, who gets one
(submission_status fixture, mapped members only, never exhibition), one per member per
slot per week, quiet hours, and DM items never reaching an old (group-only) watcher.
No network, no LLM; temp private dir.

Run:  ./.venv/bin/python3 -I test_straggler_nudges.py
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
_TMP = tempfile.mkdtemp(prefix="picky-nudge-test-")
os.environ["PICKY_PRIVATE_DIR"] = _TMP
import group_outbox as ob  # noqa: E402
import straggler_nudges as sn  # noqa: E402

CT = sn.CT
MEMBERS = {"members": [
    {"team": "Chuck P", "name": "Charlie", "handles": ["+12195550100"]},
    {"team": "Ackbar", "name": "Ackbar", "handles": ["(504) 555-0101"]},
    {"team": "TG", "name": "TG", "handles": ["+14105550104"]},
    {"team": "Liam Bryson", "name": "Liam", "handles": []},          # no handle: can't DM
]}
# the RPC submission_status for week 12 (team -> submitted), as group_posts.League.submission maps it
SUBMISSION = {"Chuck P": True, "Ackbar": False, "TG": False, "Liam Bryson": False, "Rettino": False}
LOCK = dt.datetime(2026, 11, 21, 11, 0, tzinfo=CT)


def t(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s).replace(tzinfo=CT)


class Lg:
    season = 2026

    def __init__(self, sub=None):
        self.sub = dict(SUBMISSION if sub is None else sub)
        # active humans only (exhibition Picky is never in League.tid)
        self.tid = {1: "Chuck P", 2: "Ackbar", 3: "TG", 4: "Liam Bryson", 5: "Rettino"}

    def lock_time(self, w):
        return LOCK

    def submission(self, w):
        return self.sub


def setup():
    ob.MEMBERS_FILE.write_text(json.dumps(MEMBERS))
    ob.save_config(group_guid="any;+;chat-test", dry_run=False)
    ob.PAUSE_FILE.unlink(missing_ok=True)
    for p in ob.OUTBOX.glob("*.json") if ob.OUTBOX.exists() else []:
        p.unlink()
    ob.STATE_FILE.unlink(missing_ok=True)


def queued():
    return sorted((json.loads(p.read_text()) for p in ob.OUTBOX.glob("*.json")), key=lambda x: x["team"]) \
        if ob.OUTBOX.exists() else []


def test_slots():
    assert sn.due_slot(LOCK, t("2026-11-21T08:59")) is None
    assert sn.due_slot(LOCK, t("2026-11-21T09:00")) == "first"
    assert sn.due_slot(LOCK, t("2026-11-21T09:40")) == "first"
    assert sn.due_slot(LOCK, t("2026-11-21T09:46")) is None
    assert sn.due_slot(LOCK, t("2026-11-21T10:30")) == "second"
    assert sn.due_slot(LOCK, t("2026-11-21T10:55")) is None  # last 5 minutes: nothing
    assert sn.due_slot(LOCK, t("2026-11-21T11:01")) is None
    assert sn.lock_words(LOCK) == "11" and sn.left_words(LOCK, t("2026-11-21T09:00")) == "about 2 hours"
    assert sn.left_words(LOCK, t("2026-11-21T10:30")) == "30 minutes"


def test_straggler_selection_from_submission_status():
    setup()
    got = sn.stragglers(SUBMISSION, set(Lg().tid.values()))
    # Chuck submitted; Liam has no handle; Rettino isn't in the handle map -> only Ackbar + TG
    assert [x[0] for x in got] == ["Ackbar", "TG"], got
    assert got[0][2] == "+15045550101"
    # exhibition / inactive teams never make it even if the RPC listed them
    assert sn.stragglers({"Picky": False, "Ackbar": False}, {"Ackbar"}) == [("Ackbar", "Ackbar", "+15045550101")]


def test_nudges_queue_dms_once_per_member_per_slot():
    setup()
    res = sn.tick(Lg(), 12, t("2026-11-21T09:00"), echo=lambda *a: None)
    assert res == {"Ackbar": "queued", "TG": "queued"}, res
    q = queued()
    assert [x["to"] for x in q] == ["+15045550101", "+14105550104"] and all(x["kind"] == "dm" for x in q)
    txt = q[0]["texts"][0]
    assert "week 12" in txt and "11" in txt and "2 hours" in txt, txt
    assert "Chuck" not in txt and "Charlie" not in txt and "TG" not in txt  # only about THEM
    # every 5-min tick inside the slot: no repeats
    for m in ("09:05", "09:20", "09:40"):
        assert sn.tick(Lg(), 12, t(f"2026-11-21T{m}"), echo=lambda *a: None) == {}
    assert len(queued()) == 2
    # second slot, only whoever still hasn't picked (TG picked meanwhile)
    res = sn.tick(Lg(dict(SUBMISSION, TG=True)), 12, t("2026-11-21T10:31"), echo=lambda *a: None)
    assert res == {"Ackbar": "queued"}, res
    assert sn.tick(Lg(dict(SUBMISSION, TG=True)), 12, t("2026-11-21T10:40"), echo=lambda *a: None) == {}
    # DMs never count toward the group's daily cap or recent posts
    assert ob._posts_last_day() == 0 and ob.recent_posts() == []


def test_quiet_hours_pause_and_playoffs():
    setup()
    early = dt.datetime(2026, 11, 21, 9, 30, tzinfo=CT)

    class Early(Lg):
        def lock_time(self, w):
            return early  # 2 h before = 7:30, inside quiet hours (23:30-08:00)
    assert sn.tick(Early(), 12, t("2026-11-21T07:30"), echo=lambda *a: None) == {}
    ob.PAUSE_FILE.write_text("x")
    try:
        assert set(sn.tick(Lg(), 12, t("2026-11-21T09:00"), echo=lambda *a: None).values()) == {"paused"}
    finally:
        ob.PAUSE_FILE.unlink()
    assert sn.tick(Lg(), 16, t("2026-11-21T09:00"), echo=lambda *a: None) == {}  # playoffs: per-game locks


def test_dm_items_only_go_to_a_dm_capable_watcher():
    setup()
    ob.post("remind", ["group text"], "k-group", now=t("2026-11-21T09:00"))
    ob.post_dm("Ackbar", "+15045550101", ["dm text"], "k-dm", now=t("2026-11-21T09:00"))
    now = t("2026-11-21T09:01").timestamp()
    a = ob.next_item(now)                  # old watcher (no dm_ok): the group post only
    assert a and a["chat"] == "any;+;chat-test" and "to" not in a
    assert ob.next_item(now) is None       # the DM is never handed to it
    b = ob.next_item(now, dm_ok=True)      # new watcher
    assert b and b["to"] == "+15045550101" and "chat" not in b and b["texts"] == ["dm text"]
    assert ob.ack(b["id"], 1, True)
    log = [json.loads(x) for x in ob.SEND_LOG.read_text().splitlines()]
    assert any(r["event"] == "dm-delivered" for r in log)
    assert not any("+15045550101" in json.dumps(r) for r in log)  # handles never land in the log


def test_post_dm_refuses_unmapped_handles():
    setup()
    assert ob.post_dm("Ackbar", "+19999999999", ["x"], "k1") == "unmapped"   # not his handle
    assert ob.post_dm("Picky", "+15045550101", ["x"], "k2") == "unmapped"    # handle is Ackbar's, not Picky's
    assert ob.post_dm("Nobody", "+18885550000", ["x"], "k3") == "unmapped"
    assert queued() == []


def test_watcher_dm_path():
    """The watcher sends {"to": handle} items 1-on-1 and still refuses a group item aimed at a non-group."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("watcher", HERE.parent / "tools" / "picky-imessage" / "watcher.py")
    w = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(w)
    sent, acks = [], []
    w.send = lambda chat, text, script=None: sent.append(("chat", chat, text)) or True
    w.send_dm = lambda h, text: sent.append(("dm", h, text)) or True
    w.is_group = lambda db, guid: guid.startswith("any;+;")
    items = iter([{"item": {"id": "a1", "to": "+15045550101", "texts": ["nudge"], "kind": "dm"}},
                  {"ok": True},
                  {"item": {"id": "a2", "chat": "any;-;+15045550101", "texts": ["oops"], "kind": "remind"}},
                  {"ok": True}])
    payloads = []
    w._post = lambda url, payload, secret, timeout=10: (payloads.append(payload), acks.append(payload) if
                                                       url.endswith("/ack") else None, next(items))[2]
    rate = w.RateLimit(gap=0)
    w.drain_outbox(None, "s", rate, None)
    w.drain_outbox(None, "s", rate, None)
    assert payloads[0].get("dm_ok") is True
    assert sent == [("dm", "+15045550101", "nudge")], sent
    assert acks[0]["ok"] and not acks[1]["ok"] and "not a group" in acks[1]["error"]
    # handle sanity: junk never reaches osascript
    real = w.send
    w.send = lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not send"))
    spec.loader.exec_module(w)  # restore the real send_dm
    w.send = lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not send"))
    assert w.send_dm("not a handle; do shell script", "x") is False
    w.send = real


if __name__ == "__main__":
    tests = [v for k, v in dict(globals()).items() if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"  ok  {fn.__name__}")
        except AssertionError as e:
            print(f"FAIL  {fn.__name__}: {e}")
            failed += 1
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            print(f"ERR   {fn.__name__}: {type(e).__name__}: {e}")
            failed += 1
    print("\nALL GREEN" if not failed else f"\n{failed} FAILED")
    sys.exit(1 if failed else 0)
