"""Watcher retry / never-silent / latency / DM batching (review R5), no chat.db, Messages or sidecar.
Run:  /usr/bin/python3 -I tools/picky-imessage/test_watcher_retry.py
"""
import io
import json
import sys
import tempfile
import time
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import watcher as w  # noqa: E402

TMP = Path(tempfile.mkdtemp())
w.STATE_DIR = TMP
w.STATE_FILE = TMP / "state.json"
w.LATENCY_FILE = TMP / "latency.jsonl"
GROUP = "any;+;league"


class Clock:
    def __init__(self):
        self.t = time.time()

    def __call__(self):
        return self.t


def mk(rowid, text, style=w.GROUP_STYLE, handle="+15550001111", chat=GROUP):
    return {"ROWID": rowid, "text": text, "attributedBody": None, "date": int((time.time() - 2 - w.APPLE_EPOCH) * 1e9),
            "handle": handle, "chat_guid": chat, "style": style, "associated_message_type": 0,
            "cache_has_attachments": 0}


def harness(bot_results, send_ok=True):
    """Fake ask_bot (pops results: dict = reply, Exception = raised) + fake send (records)."""
    calls, sent = [], []

    def ask_bot(handle, text, secret, chat="", dm=False, recent=None, texts=None):
        calls.append({"text": text, "texts": texts, "dm": dm})
        r = bot_results.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    def send(chat, text, script=None):
        sent.append((chat, text))
        return send_ok if not callable(send_ok) else send_ok()

    w.ask_bot, w.send = ask_bot, send
    return calls, sent


def loop_state():
    return {"last_rowid": 0}, w.RateLimit(gap=0), {}, {}, set()


def test_500_retries_then_answers_without_skipping():
    calls, sent = harness([urllib.error.HTTPError("u", 500, "boom", {}, io.BytesIO())])
    clk = Clock()
    state, rate, pending, retries, done = loop_state()
    rows = [mk(10, "@Picky who covers"), mk(11, "@Picky next")]
    w.process_rows(rows, state, "s", rate, None, pending, retries, done, clk.t, clock=clk)
    assert state["last_rowid"] == 0 and not sent, "a 500 must not advance past the row"
    w.process_rows(rows, state, "s", rate, None, pending, retries, done, clk.t, clock=clk)
    assert len(calls) == 1, "backoff holds the line"
    clk.t += w.RETRY_BACKOFF[0] + 1
    calls2, sent2 = harness([{"reply": "Iowa by 7."}, {"reply": None}])
    w.process_rows(rows, state, "s", rate, None, pending, retries, done, clk.t, clock=clk)
    assert sent2 == [(GROUP, "Iowa by 7.")] and state["last_rowid"] == 11 and not retries


def test_three_failures_then_canned_only_where_picky_talks():
    err = urllib.error.URLError(ConnectionRefusedError(61, "refused"))
    for answered, expect_canned in ((True, True), (False, False)):
        clk = Clock()
        calls, sent = harness([err] * 4)
        state, rate, pending, retries, done = loop_state()
        if answered:
            state["answered_chats"] = [GROUP]
        rows = [mk(20, "@Picky hi")]
        for _ in range(4):
            w.process_rows(rows, state, "s", rate, None, pending, retries, done, clk.t, clock=clk)
            clk.t += 100
        assert len(calls) == 4 and state["last_rowid"] == 20
        assert (sent == [(GROUP, w.CANNED)]) == expect_canned, sent


def test_timeout_goes_straight_to_canned():
    calls, sent = harness([urllib.error.URLError(TimeoutError("timed out"))])
    clk = Clock()
    state, rate, pending, retries, done = loop_state()
    state["answered_chats"] = [GROUP]
    w.process_rows([mk(30, "@Picky long one")], state, "s", rate, None, pending, retries, done, clk.t, clock=clk)
    assert len(calls) == 1 and sent == [(GROUP, w.CANNED)] and state["last_rowid"] == 30


def test_failed_send_retries_the_send_not_the_brain():
    flaky = iter([False, False, True])
    calls, sent = harness([{"reply": "Utah +7."}], send_ok=lambda: next(flaky))
    clk = Clock()
    state, rate, pending, retries, done = loop_state()
    rows = [mk(40, "@Picky ?")]
    for _ in range(3):
        w.process_rows(rows, state, "s", rate, None, pending, retries, done, clk.t, clock=clk)
        clk.t += 100
    assert len(calls) == 1 and len(sent) == 3 and state["last_rowid"] == 40
    assert GROUP in state["answered_chats"]


def test_4xx_is_not_retried():
    calls, sent = harness([urllib.error.HTTPError("u", 401, "bad_secret", {}, io.BytesIO())])
    state, rate, pending, retries, done = loop_state()
    w.process_rows([mk(50, "@Picky x")], state, "s", rate, None, pending, retries, done, None)
    assert state["last_rowid"] == 50 and not sent


def test_dm_batch_one_ask_one_reply():
    dm = "any;-;+15550002222"
    rows = [mk(60, "you there", style=45, handle="+15550002222", chat=dm), mk(61, "@Picky group", chat=GROUP),
            mk(62, "who covers lsu", style=45, handle="+15550002222", chat=dm),
            mk(63, "other dm", style=45, handle="+15550003333", chat="any;-;+15550003333")]
    calls, sent = harness([{"reply": "LSU +3."}, {"reply": "group answer"}, {"reply": None}])
    state, rate, pending, retries, done = loop_state()
    w.process_rows(rows, state, "s", rate, None, pending, retries, done, None)
    assert calls[0]["texts"] == ["you there", "who covers lsu"] and calls[0]["text"] == "who covers lsu"
    assert len(calls) == 3, "62 rides with 60; 61 and 63 are their own asks"
    assert sent[:2] == [(dm, "LSU +3."), (GROUP, "group answer")] and state["last_rowid"] == 63


def test_latency_rows_and_daily_line():
    w.LATENCY_FILE.unlink(missing_ok=True)
    now = time.time()
    for i, e2e in enumerate([5, 10, 20, 40, 80]):
        w.note_latency("group" if i % 2 else "dm", now - e2e, now, 3.0, 1, "fact")
    line = w.daily_summary(time.strftime("%Y-%m-%d", time.localtime(now)))
    assert line.startswith("daily latency ") and "all n=5 p50=20.0s p90=80.0s" in line, line
    assert "group n=2" in line and "dm n=3" in line
    assert all(json.loads(x)["kind"] in ("dm", "group") for x in w.LATENCY_FILE.read_text().splitlines())


def test_budgets_are_separate_and_gap_is_short():
    assert w.MIN_GAP <= 3.0 and w.MAX_PER_HOUR > w.POST_PER_HOUR
    src = (Path(__file__).resolve().parent / "watcher.py").read_text()
    assert "drain_outbox(db, secret, post_rate, groups)" in src


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
