"""Pictures (review R2): policy, the timeout fix, async delivery through the outbox. Model-free."""
import asyncio
import datetime as dt
import json
import os
import re
import sys
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp())
os.environ["PICKY_PRIVATE_DIR"] = str(TMP / "private")
os.environ["PICKY_LIVE_FLAG_DIR"] = str(TMP / "live")
os.environ["PICKY_IMESSAGE_HISTORY"] = str(TMP / "hist.json")
os.environ["PICKY_EXCHANGE_LOG"] = str(TMP / "ex.jsonl")
os.environ.setdefault("PICKY_NO_ANTHROPIC", "1")
(TMP / "private").mkdir()
(TMP / "live").mkdir()
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "tools" / "picky-imessage"))

import pictures  # noqa: E402
import group_outbox as ob  # noqa: E402

CT = pictures.CT


def at(y, mo, d, h, mi):
    return dt.datetime(y, mo, d, h, mi, tzinfo=CT)


def test_game_window_is_saturday_11_to_midnight():
    assert pictures.game_window(at(2026, 11, 21, 11, 0)) and pictures.game_window(at(2026, 11, 21, 23, 59))
    assert not pictures.game_window(at(2026, 11, 21, 10, 59))
    assert not pictures.game_window(at(2026, 11, 22, 0, 30)) and not pictures.game_window(at(2026, 11, 20, 13, 0))
    assert pictures.refusal("x", now=at(2026, 11, 21, 14, 0), mark=False) in pictures.REFUSE_GAMEDAY


def test_refused_while_a_text_is_pending_and_while_drawing():
    weekday = at(2026, 11, 18, 14, 0)
    flag = TMP / "live" / "tok1"
    flag.write_text("x")
    try:
        assert pictures.refusal("a", own_flag=0, now=weekday, mark=False) in pictures.REFUSE_BUSY
        assert pictures.refusal("a", own_flag=1, now=weekday, mark=False) is None  # only its own text
    finally:
        flag.unlink()
    pictures._busy["since"] = __import__("time").time()
    try:
        assert pictures.refusal("a", now=weekday, mark=False) in pictures.REFUSE_DRAWING
    finally:
        pictures._busy["since"] = 0.0


def test_five_an_hour():
    weekday = at(2026, 11, 18, 14, 0)
    got = [pictures.refusal("hog", now=weekday) for _ in range(6)]
    assert got[:5] == [None] * 5 and got[5] in pictures.REFUSE_LIMIT
    img = (HERE / "image.py").read_text()
    assert re.search(r"^RATE_LIMIT_PER_HOUR = 5\b", img, re.M), "image.py must enforce the same 5/hour"


def test_read_timeout_outlasts_image_service():
    import chat
    img = (HERE / "image.py").read_text()
    gen = float(re.search(r"^MAX_GEN_SECONDS = ([\d.]+)", img, re.M).group(1))
    assert chat.IMAGE_MAX_GEN_SECONDS == gen and chat.IMAGE_READ_TIMEOUT == gen + 30


def test_picture_reply_is_async_and_delivers_to_the_asking_chat():
    import group_chat as gc
    ob.save_config(group_guid="any;+;league", dry_run=False)
    started = {}

    def fake_start(prompt, who, deliver):
        started.update(prompt=prompt, who=who)
        deliver("", "aGVsbG8=")  # the background job finished

    real = pictures.start
    pictures.start = fake_start
    try:
        s = gc.Sender(handle="+15550001111", team="Ackbar", user_id="u1")
        with_week = pictures.game_window
        pictures.game_window = lambda now=None: False
        try:
            reply = gc.picture_reply(s, "draw me a bulldog", "any;-;+15550001111", "+15550001111")
        finally:
            pictures.game_window = with_week
    finally:
        pictures.start = real
    assert reply in pictures.DRAWING and started == {"prompt": "draw me a bulldog", "who": "u1"}
    assert ob.next_item(dm_ok=True) is None, "an old watcher (no img_ok) never gets a picture"
    item = ob.next_item(dm_ok=True, img_ok=True)
    assert item["chat"] == "any;-;+15550001111" and item["image"] == "aGVsbG8=" and item["reply"]


def test_watcher_sends_picture_items():
    import watcher as w
    sent, acks = [], []
    item = {"id": "abc123", "kind": "picture", "texts": [], "chat": "any;-;+15550001111", "image": "aGVsbG8=",
            "reply": True}
    w.OUT_DIR = TMP / "out"

    def _post(url, payload, secret, timeout=10):
        if url.endswith("/ack"):
            acks.append(payload)
            return {}
        assert payload["img_ok"] is True
        return {"item": item}

    class DB:
        def execute(self, sql, args):
            class R:
                def fetchone(self_inner):
                    return (45,) if "style" in sql else (1,)
            return R()

    w._post, w.send = _post, lambda chat, text, script=None: sent.append((chat, text)) or True
    w.drain_outbox(DB(), "s", w.RateLimit(gap=0), None)
    assert sent and sent[0][0] == "any;-;+15550001111" and sent[0][1].endswith(".png")
    assert acks[0]["ok"] is True


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("ok  ", name)
            except AssertionError as e:
                fails += 1
                print("FAIL", name, e)
    print("ALL PASS" if not fails else f"{fails} FAILED")
    sys.exit(1 if fails else 0)
