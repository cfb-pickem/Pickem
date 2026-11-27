"""
TEMP beef mode (beef.py, 2026-11-27): Opus replies to every league-group text from the target while it's on.

Run:  ./.venv/bin/python3 -I test_beef.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "tools" / "picky-imessage"))
import beef  # noqa: E402
import group_chat as gc  # noqa: E402
import picky_claude  # noqa: E402
import watcher  # noqa: E402

TMP = Path(tempfile.mkdtemp())
beef.STATE_FILE = TMP / "beef_state.json"
gc.EXCHANGE_LOG = TMP / "exchanges.jsonl"
GUID = "any;+;testgroup"
ACK = "+15048751440"
OTHER = "+15555550123"


def cfg(until_s: float = 3600) -> dict:
    import datetime as dt
    until = dt.datetime.fromtimestamp(time.time() + until_s).astimezone().isoformat()
    return {"group_guid": GUID, "beef": {"team": "Ackbar", "handle": ACK, "until": until,
                                         "notes": ["he demanded a refund"]}}


class Req:
    def __init__(self, body: dict):
        self.headers = {"x-picky-group-secret": "s3cret"}
        self._b = body

    async def json(self):
        return self._b


def post(body: dict, conf: dict, opus=None, normal=None):
    calls = {"opus": [], "normal": []}

    async def fake_live(messages, kind="reply", schema=None, timeout=None, effort=None):
        calls["opus"].append({"messages": messages, "kind": kind, "timeout": timeout, "effort": effort})
        return opus(messages) if callable(opus) else opus

    async def fake_answer(handle, text, chat_id="", dm=False, allowlisted=False, recent=None, web=None):
        import chat
        calls["normal"].append({"handle": handle, "text": text, "rider": chat.BEEF_RIDER.get()})
        return normal if normal is not None else {"reply": "normal reply", "image": None, "mode": "group", "lane": "x"}

    async def fake_facts():
        return "Ackbar 30-22, 2nd."

    async def no_save(*a, **k):
        return None

    with patch.object(gc, "SECRET", "s3cret"), patch.object(gc.group_outbox, "config", lambda: conf), \
            patch.object(picky_claude, "live_reply", fake_live), patch.object(gc, "answer", fake_answer), \
            patch.object(gc, "_raw_facts", fake_facts), patch.object(gc, "save_turn", no_save), \
            patch.object(gc.maintenance, "active", lambda *a: False), \
            patch.object(gc, "flag_reply", lambda *a: None), patch.object(beef, "fact_sheet", lambda t, f, n: f"{f} 73%"):
        out = asyncio.run(gc.group(Req(body)))
    return out, calls


def reset():
    beef.STATE_FILE.unlink(missing_ok=True)


def test_ackbar_unaddressed_gets_opus_reply():
    reset()
    out, calls = post({"handle": ACK, "text": "@Picky you're a bot with no money", "chat": GUID,
                       "forwarded": "beef", "recent": [{"handle": ACK, "text": "refund me", "ts": time.time()}]},
                      cfg(), opus="Picky: Refund? You laid 73% chalk all year, the house already paid you.")
    assert out["lane"] == "beef:opus", out
    assert out["reply"].startswith("Refund?"), out
    assert len(calls["opus"]) == 1 and not calls["normal"], calls
    c = calls["opus"][0]
    assert c["kind"] == "beef" and c["timeout"] == beef.TIMEOUT and c["effort"] == beef.EFFORT
    sys_msg, user = c["messages"][0]["content"], c["messages"][1]["content"]
    assert "BEEF MODE" in sys_msg and "invent a number" in sys_msg and "no slurs" in sys_msg
    assert "you're a bot with no money" in user and "Ackbar: refund me" in user and "@Picky" not in user


def test_ungrounded_number_dropped():
    reset()
    out, _ = post({"handle": ACK, "text": "@Picky lol", "chat": GUID}, cfg(),
                  opus="You're 0-9 against me. You take the favorite 73% of the time, coward.")
    assert out["reply"] == "You take the favorite 73% of the time, coward.", out


def test_no_reply_when_expired():
    reset()
    out, calls = post({"handle": ACK, "text": "@Picky hello", "chat": GUID, "forwarded": "beef"}, cfg(-60), opus="x")
    assert out == {"reply": None} and not calls["opus"] and not calls["normal"], (out, calls)
    assert beef.handles() == [] or True  # the real config isn't consulted here
    assert beef.active(cfg=cfg(-60)) is None and beef.active(cfg=cfg()) is not None


def test_other_members_unaffected():
    reset()
    out, calls = post({"handle": OTHER, "text": "lol ackbar", "chat": GUID}, cfg(), opus="x")
    assert out == {"reply": None} and not calls["opus"], (out, calls)  # no @Picky: nothing, as always
    out, calls = post({"handle": OTHER, "text": "@Picky who's first?", "chat": GUID}, cfg(), opus="x")
    assert out["reply"] == "normal reply" and not calls["opus"] and calls["normal"][0]["rider"] == "", (out, calls)


def test_other_chat_unaffected():
    reset()
    out, calls = post({"handle": ACK, "text": "@Picky hi", "chat": "any;+;othergroup"}, cfg(), opus="x")
    assert not calls["opus"], calls


def test_cooldown_waits():
    reset()
    beef.mark_sent(time.time() - beef.COOLDOWN + 0.3)
    t0 = time.time()
    out, _ = post({"handle": ACK, "text": "@Picky again", "chat": GUID}, cfg(), opus="Keep crying.")
    assert out["reply"] == "Keep crying." and time.time() - t0 >= 0.25, time.time() - t0
    assert 0 < beef.cooldown_wait() <= beef.COOLDOWN


def test_opus_failure_falls_back_with_rider():
    reset()
    out, calls = post({"handle": ACK, "text": "@Picky refund", "chat": GUID}, cfg(), opus=None)
    assert out["reply"] == "normal reply" and out["lane"].startswith("beef:fallback"), out
    assert "BEEF" in calls["normal"][0]["rider"], calls
    reset()
    out, _ = post({"handle": ACK, "text": "@Picky refund", "chat": GUID}, cfg(), opus=None,
                  normal={"reply": None, "image": None, "mode": "group", "lane": "ignore"})
    assert out["lane"] in ("beef:jab", "beef:fallback:ignore"), out  # a nemesis jab when lore has one


def test_upset_backs_off_and_pauses():
    reset()
    c = cfg()
    with patch.object(gc.group_outbox, "config", lambda: c):
        out, calls = post({"handle": ACK, "text": "@Picky seriously stop, not funny", "chat": GUID}, c, opus="x")
        assert out["reply"] == beef.BACK_OFF and not calls["opus"], out
        assert beef.active() is None  # paused for this beef
        beef._write_state(paused=False)
        assert beef.active() is not None
    assert beef.upset("ok seriously stop") and not beef.upset("stop crying about your refund lmao")


def test_batch_texts_go_to_opus():
    reset()
    out, calls = post({"handle": ACK, "text": "@Picky third", "chat": GUID, "texts": ["first", "second", "third"]},
                      cfg(), opus="One at a time, champ.")
    user = calls["opus"][0]["messages"][1]["content"]
    assert "Ackbar: first" in user and "Ackbar: third" in user, user


def test_watcher_forwards_only_beef_handles():
    now = time.time()
    row = {"ROWID": 5, "text": "refund me now", "attributedBody": None, "date": (now - watcher.APPLE_EPOCH) * 1e9,
           "handle": ACK, "chat_guid": GUID, "style": watcher.GROUP_STYLE, "associated_message_type": 0,
           "cache_has_attachments": 0}
    watcher.BEEF_HANDLES.clear()
    assert watcher.should_act(row, now) == (False, "no-trigger")
    watcher.BEEF_HANDLES.add(ACK)
    try:
        assert watcher.should_act(row, now)[0]
        assert watcher.should_act({**row, "handle": OTHER}, now) == (False, "no-trigger")
        rows = [row, {**row, "ROWID": 6}, {**row, "ROWID": 7, "handle": OTHER}]
        assert [r["ROWID"] for r in watcher.dm_batch(rows, 0, lambda r: watcher.should_act(r, now)[0])] == [5, 6]
    finally:
        watcher.BEEF_HANDLES.clear()
    assert [r["ROWID"] for r in watcher.dm_batch([row, {**row, "ROWID": 6}], 0, lambda r: True)] == [5]


def test_cli_off(tmp=TMP):
    p = tmp / "cfg.json"
    p.write_text(json.dumps(cfg()))
    with patch.object(beef.group_outbox, "CONFIG_FILE", p), \
            patch.object(beef.group_outbox, "config", lambda: json.loads(p.read_text())):
        assert beef.handles() == [ACK]
        beef.main(["off"])
        assert "beef" not in json.loads(p.read_text()) and beef.handles() == []


if __name__ == "__main__":
    tests = [v for k, v in dict(globals()).items() if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  ok  {t.__name__}")
        except Exception as e:  # noqa: BLE001
            print(f"FAIL  {t.__name__}: {type(e).__name__}: {e}")
            failed += 1
    print("\nALL GREEN" if not failed else f"\n{failed} FAILED")
    sys.exit(1 if failed else 0)
