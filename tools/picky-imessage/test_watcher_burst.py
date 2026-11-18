"""Burst behavior of watcher.py (Saturday drill, 2026-11-17). Rows are handled one at a time, so in a
burst of ~20 group asks the later ones waited behind Picky's own replies and were checked against
MAX_AGE when their turn came: past 600 s they were skipped as 'stale' with no reply. Age is now judged
at the start of the watcher's current busy stretch (stale_ref): a queue of our own making is never
stale, while a real backlog (messages already old when the watcher picked them up, e.g. after it was
down) is still skipped.
Run:  /usr/bin/python3 -I tools/picky-imessage/test_watcher_burst.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import watcher as w  # noqa: E402

GROUP = w.GROUP_STYLE


def grow(arrived: float, text="@Picky who's covering"):
    return {"ROWID": 1, "text": text, "attributedBody": None, "associated_message_type": 0,
            "cache_has_attachments": 0, "style": GROUP, "handle": "+15555550101", "chat_guid": "g",
            "date": int((arrived - w.APPLE_EPOCH) * 1e9)}


def test_stale_ref():
    assert w.stale_ref(None, 1000.0) == 1000.0
    assert w.stale_ref(400.0, 1000.0) == 400.0


def test_burst_row_waiting_behind_our_replies_is_not_stale():
    t0 = 1_800_000_000.0
    r = grow(t0)                        # arrived with the burst
    busy = t0 + 2                       # the watcher picked the batch up 2 s later
    now = t0 + w.MAX_AGE + 300          # its turn comes 15 min in
    assert w.should_act(r, now) == (False, "stale")                 # the old check: dropped
    assert w.should_act(r, w.stale_ref(busy, now)) == (True, "ok")  # judged at pick-up: answered


def test_real_backlog_is_still_stale():
    t0 = 1_800_000_000.0
    r = grow(t0)
    busy = t0 + w.MAX_AGE + 60          # the watcher came back up 11 min after the message
    assert w.should_act(r, w.stale_ref(busy, busy + 1)) == (False, "stale")


def test_main_loop_uses_stale_ref():
    src = (Path(__file__).resolve().parent / "watcher.py").read_text()
    rows = src[src.index("def process_rows("):src.index("def main()")]
    loop = src[src.index("def main()"):src.index("def handle_one(")]
    assert "should_act(row, stale_ref(busy_since, clock()))" in rows
    assert "busy_since = None" in loop and "process_rows(" in loop


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
    print("all green" if not fails else f"FAILED: {fails}")
    sys.exit(1 if fails else 0)
