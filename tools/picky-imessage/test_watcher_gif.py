"""Watcher: a GIF reply image (dm_memes) is written as .gif (a picture stays .png), and a meme that fails to
attach goes out as its image_alt text. No chat.db, Messages or sidecar.
Run:  /usr/bin/python3 -I tools/picky-imessage/test_watcher_gif.py
"""
import base64
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import watcher as w  # noqa: E402

TMP = Path(tempfile.mkdtemp())
w.STATE_DIR = TMP
w.STATE_FILE = TMP / "state.json"
w.LATENCY_FILE = TMP / "latency.jsonl"
w.OUT_DIR = TMP / "out"
DM = "any;-;+15550001111"
GIF = base64.b64encode(b"GIF89a" + b"\0" * 32).decode()
PNG = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\0" * 32).decode()


def row(rowid):
    return {"ROWID": rowid, "text": "who covers", "attributedBody": None,
            "date": int((time.time() - 2 - w.APPLE_EPOCH) * 1e9), "handle": "+15550001111", "chat_guid": DM,
            "style": 45, "associated_message_type": 0, "cache_has_attachments": 0}


def harness(out, file_ok=True):
    sent = []

    def send(chat, text, script=None):
        sent.append((chat, text, script is w.SEND_FILE_SCRIPT))
        return file_ok if script is w.SEND_FILE_SCRIPT else True

    w.ask_bot = lambda *a, **k: out
    w.send = send
    return sent


def test_gif_gets_gif_extension_and_goes_after_the_text():
    sent = harness({"reply": "Iowa by 7.", "image": GIF, "image_alt": "\"Wrong!\"\nhttps://giphy.com/gifs/x"})
    assert w.handle_one(row(1), "s", w.RateLimit(gap=0)) == "sent"
    assert sent[0] == (DM, "Iowa by 7.", False), sent
    assert sent[1][2] and sent[1][1].endswith("picky-1.gif"), sent
    assert len(sent) == 2, "attached fine: no fallback text"


def test_png_picture_unchanged():
    sent = harness({"reply": "Here you go.", "image": PNG})
    w.handle_one(row(2), "s", w.RateLimit(gap=0))
    assert sent[1][1].endswith("picky-2.png"), sent


def test_failed_attachment_sends_the_quote_and_link():
    sent = harness({"reply": "Iowa by 7.", "image": GIF, "image_alt": "\"Wrong!\"\nhttps://giphy.com/gifs/x"},
                   file_ok=False)
    w.handle_one(row(3), "s", w.RateLimit(gap=0))
    assert sent[-1] == (DM, "\"Wrong!\"\nhttps://giphy.com/gifs/x", False), sent


def test_failed_picture_without_alt_sends_nothing_extra():
    sent = harness({"reply": "Here you go.", "image": PNG}, file_ok=False)
    w.handle_one(row(4), "s", w.RateLimit(gap=0))
    assert len(sent) == 2, sent


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
