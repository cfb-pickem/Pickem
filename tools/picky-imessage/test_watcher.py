"""Pure tests for watcher.py (no chat.db, no Messages, no sidecar).
Run:  /usr/bin/python3 -I tools/picky-imessage/test_watcher.py
The attributedBody blobs are real DMs, so they live in the private repo
(cfb-pickem-private/fixtures/picky_attributed_bodies.json); that test skips
when the file isn't there.
"""
import base64
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import watcher as w  # noqa: E402

FIXTURE = Path(os.environ.get("PICKY_PRIVATE_DIR", "/Users/Agent/cfb-pickem-private")) / \
    "fixtures/picky_attributed_bodies.json"


def row(rowid=1, text=None, body=None, amt=0, att=0):
    return {"ROWID": rowid, "text": text, "attributedBody": body, "associated_message_type": amt,
            "cache_has_attachments": att}


def test_decoder_on_real_dm_blobs():
    if not FIXTURE.exists():
        print("  skip: no private fixture")
        return
    rows = json.loads(FIXTURE.read_text())["rows"]
    assert rows
    for r in rows:
        got = w.decode_attributed_body(base64.b64decode(r["attributedBody_b64"]))
        assert got == r["text"], (r["rowid"], got)


def test_text_present_never_waits():
    pending = {}
    assert not w.wait_for_text(row(text="Hello"), pending, 100.0)
    assert pending == {}


def test_empty_row_waits_then_is_skipped():
    pending, r = {}, row(rowid=7)
    assert w.wait_for_text(r, pending, 100.0)
    assert w.wait_for_text(r, pending, 100.0 + w.EMPTY_WAIT - 1)
    assert not w.wait_for_text(r, pending, 100.0 + w.EMPTY_WAIT)  # give up: caller moves past it
    assert pending == {}


def test_empty_row_that_fills_in_is_handled():
    pending = {}
    assert w.wait_for_text(row(rowid=7), pending, 100.0)
    assert not w.wait_for_text(row(rowid=7, text="Hi picky"), pending, 103.0)
    assert pending == {}


def test_attachment_and_tapback_rows_dont_wait():
    assert not w.wait_for_text(row(att=1), {}, 100.0)
    assert not w.wait_for_text(row(amt=2000), {}, 100.0)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
