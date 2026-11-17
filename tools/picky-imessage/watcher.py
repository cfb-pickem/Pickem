#!/usr/bin/env python3
"""
Picky's iMessage watcher. Runs inside the `picky` macOS user only
(LaunchAgent com.pickem.picky-imessage), never in Agent.

Polls ~/Library/Messages/chat.db read-only. For each new incoming message in
a GROUP chat that contains "@Picky" (also "@ Picky", any case, or a
confirmed iMessage mention of Picky's own handle), asks the localhost
group sidecar (server/group_chat.py, 127.0.0.1:8768) for a reply and sends
it back to the same chat through Messages via AppleScript, in this user's
session. A picture reply goes out as an attachment.

DMs need no @Picky. Every 1-on-1 text goes to the sidecar marked dm, and
the sidecar answers only mapped league members (from the private member
map, re-read live) and TEST_DM_HANDLES (allowlisted: public mode when not
mapped); anyone else gets no reply. Tapbacks and attachment-only messages
are skipped.

Deduped by message ROWID (state persisted). At most one reply per 10 s and
60 per hour. Logs carry a short preview only, never full bodies.

Proactive group posts (recaps, previews, pick reminders, score pings) are
written on the Agent side and queued (server/group_outbox.py). Every
OUTBOX_POLL seconds this asks the sidecar for the next one (POST /outbox, with
the list of group chats in this chat.db every few minutes so the sidecar can
find the league group), sends it under the SAME rate limiter as replies, only
to a chat that is a group in this chat.db, and acks it. The sidecar returns
nothing while the kill switch is on, in dry run, or before the group is found.

Standard library only, so it runs on the system python3.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import deque
from pathlib import Path

HOME = Path.home()
CHAT_DB = HOME / "Library/Messages/chat.db"
STATE_DIR = HOME / ".picky-imessage"
STATE_FILE = STATE_DIR / "state.json"
SECRET_FILE = STATE_DIR / "secret"
CONFIG_FILE = STATE_DIR / "config.json"  # written by install.sh: self_handles, test_dm_handles
LOG_FILE = HOME / "Library/Logs/picky-imessage.log"
OUT_DIR = HOME / "Pictures/picky-out"  # Messages can attach from ~/Pictures (TCC)

BOT_URL = os.environ.get("PICKY_GROUP_URL", "http://127.0.0.1:8768/group")
OUTBOX_URL = BOT_URL.rsplit("/", 1)[0] + "/outbox"
OUTBOX_POLL = 20.0      # seconds between outbox checks
GROUPS_EVERY = 300.0    # seconds between group-list reports
POLL_SECONDS = 3.0
MIN_GAP = 10.0          # seconds between replies
MAX_PER_HOUR = 60
MAX_AGE = 600           # ignore messages older than this (watcher was down)
DELIVERY_EVERY = 60.0   # seconds between failed-send checks
EMPTY_WAIT = 10.0       # re-read a row with no text yet for this long before skipping it
PREVIEW = 40


def _config() -> dict:
    try:
        return json.loads(CONFIG_FILE.read_text())
    except (OSError, ValueError):
        return {}


# Picky's own Apple ID (for @-menu mentions) and the handles allowed to DM for
# testing. Kept out of this public repo; install.sh writes them.
SELF_HANDLES = {h.strip().lower() for h in _config().get("self_handles") or [] if h.strip()}
TEST_DM_HANDLES = {h.strip() for h in _config().get("test_dm_handles") or [] if h.strip()}

APPLE_EPOCH = 978307200  # 2001-01-01 in unix time
GROUP_STYLE = 43         # chat.style: 43 group, 45 one-to-one

# '@Picky', '@pickempicky', '@pickempicky@gmail.com' or a bare 'pickempicky@gmail.com' (the sidecar's copy matches)
TRIGGER = re.compile(r"(?<![\w@])@\s?picky(?![\w@])|(?<![\w.])@?\s?pickempicky(?:@gmail\.com)?(?![\w@])", re.I)
MENTION_MARK = b"__kIMMentionConfirmedMention"

log = logging.getLogger("picky-imessage")


def triggered(text: str | None, body: bytes | None = None) -> bool:
    """@Picky in the text, or iMessage's confirmed mention of Picky's own
    handle (picking the contact from the @ menu drops the '@' from the text)."""
    if text and TRIGGER.search(text):
        return True
    if body and MENTION_MARK in body:
        low = body.lower()
        return any(h.encode() in low for h in SELF_HANDLES)
    return False


def decode_attributed_body(blob: bytes | None) -> str | None:
    """Plain text out of message.attributedBody (an NSAttributedString
    typedstream). Newer macOS leaves message.text NULL and stores only this."""
    if not blob:
        return None
    i = blob.find(b"NSString")
    if i < 0:
        return None
    j = blob.find(b"+", i)
    if j < 0:
        return None
    p = j + 1
    n = blob[p]
    p += 1
    if n == 0x81:
        n = int.from_bytes(blob[p:p + 2], "little")
        p += 2
    elif n == 0x82:
        n = int.from_bytes(blob[p:p + 4], "little")
        p += 4
    return blob[p:p + n].decode("utf-8", "replace")


def preview(text: str | None) -> str:
    t = re.sub(r"\s+", " ", text or "").strip()
    return (t[:PREVIEW] + "...") if len(t) > PREVIEW else t


class RateLimit:
    def __init__(self, gap: float = MIN_GAP, per_hour: int = MAX_PER_HOUR, clock=time.time):
        self.gap, self.per_hour, self.clock = gap, per_hour, clock
        self.sent: deque[float] = deque()

    def wait_needed(self) -> float | None:
        """Seconds to wait before the next send, or None when the hourly cap is hit."""
        now = self.clock()
        while self.sent and now - self.sent[0] > 3600:
            self.sent.popleft()
        if len(self.sent) >= self.per_hour:
            return None
        if self.sent:
            return max(0.0, self.gap - (now - self.sent[-1]))
        return 0.0

    def mark(self) -> None:
        self.sent.append(self.clock())


# ---------------------------------------------------------------------------
# chat.db
# ---------------------------------------------------------------------------

NEW_MESSAGES = """
SELECT m.ROWID, m.text, m.attributedBody, m.date, h.id AS handle,
       c.guid AS chat_guid, c.style AS style, m.associated_message_type,
       m.cache_has_attachments
FROM message m
JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
JOIN chat c ON c.ROWID = cmj.chat_id
LEFT JOIN handle h ON h.ROWID = m.handle_id
WHERE m.ROWID > ? AND m.is_from_me = 0
ORDER BY m.ROWID ASC
LIMIT 200
"""


# the last few messages in the group before the @ (any sender, Picky's own too): read-only context
RECENT_MESSAGES = """
SELECT m.text, m.attributedBody, m.date, h.id AS handle, m.is_from_me
FROM message m
JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
JOIN chat c ON c.ROWID = cmj.chat_id
LEFT JOIN handle h ON h.ROWID = m.handle_id
WHERE c.guid = ? AND m.ROWID < ? AND COALESCE(m.associated_message_type, 0) = 0
ORDER BY m.ROWID DESC
LIMIT ?
"""
RECENT_N = 8


def recent_messages(db: sqlite3.Connection | None, chat_guid: str, rowid: int) -> list[dict]:
    if db is None:
        return []
    try:
        rows = db.execute(RECENT_MESSAGES, (chat_guid, rowid, RECENT_N)).fetchall()
    except sqlite3.Error as e:
        log.warning("recent messages unreadable: %s", e)
        return []
    out = []
    for text, body, date, handle, me in reversed(rows):
        t = text or decode_attributed_body(body) or ""
        if t.strip():
            out.append({"handle": handle or "", "text": t[:300], "ts": msg_unix_time(date), "from_me": bool(me)})
    return out


def connect() -> sqlite3.Connection:
    return sqlite3.connect(f"file:{CHAT_DB}?mode=ro", uri=True, timeout=5)


# Picky's own sends that Messages marked failed (error != 0), last 26 h
FAILED_SENDS = """
SELECT m.ROWID, c.chat_identifier, m.error, m.date FROM message m
JOIN chat_message_join j ON j.message_id = m.ROWID
JOIN chat c ON c.ROWID = j.chat_id
WHERE m.is_from_me = 1 AND m.error != 0 AND m.date > ?
ORDER BY m.ROWID
"""


def new_failures(rows: list, logged: dict) -> tuple[list[dict], dict]:
    """Pure: failed-send rows (rowid, handle, error, apple date) -> the ones not yet
    reported, one per handle per (message) day. `logged`: handle -> last day reported."""
    logged, events = dict(logged), []
    for rowid, handle, error, date in rows:
        day = time.strftime("%Y-%m-%d", time.localtime(msg_unix_time(date)))
        if day <= logged.get(handle, ""):
            continue
        logged[handle] = day
        events.append({"rowid": rowid, "handle": handle or "", "error": error, "day": day})
    return events, logged


def check_deliveries(db: sqlite3.Connection, state: dict, now: float) -> None:
    """Log each handle whose send failed, once a day (the watchdog copies these lines to
    cfb-pickem-private/picky_delivery_failures.jsonl). Report only, never a resend."""
    since = int((now - 26 * 3600 - APPLE_EPOCH) * 1e9)
    events, state["delivery_logged"] = new_failures(db.execute(FAILED_SENDS, (since,)).fetchall(),
                                                    state.get("delivery_logged", {}))
    for e in events:
        log.warning("delivery failed handle=...%s error=%s", e["handle"][-4:], e["error"])
    if events:
        save_state(state)


def max_rowid(db: sqlite3.Connection) -> int:
    return db.execute("SELECT COALESCE(MAX(ROWID), 0) FROM message").fetchone()[0]


def msg_unix_time(date: int | None) -> float:
    if not date:
        return 0.0
    secs = date / 1e9 if date > 1e12 else float(date)  # nanoseconds since High Sierra
    return secs + APPLE_EPOCH


def wait_for_text(row: dict, pending: dict, now: float) -> bool:
    """True while a row has no text in either column yet (Messages may not have
    finished writing it): the caller stops there and re-reads it next poll. After
    EMPTY_WAIT it's skipped. Tapbacks and attachments are judged as before."""
    if row.get("associated_message_type") or row.get("cache_has_attachments"):
        return False
    if (row["text"] or decode_attributed_body(row["attributedBody"]) or "").strip():
        pending.pop(row["ROWID"], None)
        return False
    first = pending.setdefault(row["ROWID"], now)
    if now - first < EMPTY_WAIT:
        return True
    pending.pop(row["ROWID"], None)
    log.info("empty rowid=%s still blank after %.0fs, skipped", row["ROWID"], now - first)
    return False


def should_act(row: dict, now: float) -> tuple[bool, str]:
    text = row["text"] or decode_attributed_body(row["attributedBody"])
    if row.get("associated_message_type"):
        return False, "tapback"
    if row["style"] == GROUP_STYLE:
        if not triggered(text, row["attributedBody"]):
            return False, "no-trigger"
    elif not (text or "").strip():
        return False, "empty"  # DMs: any text, no @Picky needed (the sidecar checks the sender)
    if now - msg_unix_time(row["date"]) > MAX_AGE:
        return False, "stale"
    return True, "ok"


# ---------------------------------------------------------------------------
# bot + send
# ---------------------------------------------------------------------------


def ask_bot(handle: str, text: str, secret: str, chat: str = "", dm: bool = False,
            recent: list | None = None) -> dict:
    """{"reply": str|None, "image": base64 png|None}"""
    if not dm and not TRIGGER.search(text):  # mention-only messages: hand the sidecar a literal trigger
        text = "@Picky " + text
    payload = {"handle": handle, "text": text, "chat": chat, "dm": dm,
               "allowlisted": dm and handle in TEST_DM_HANDLES, "recent": [] if dm else (recent or [])}
    req = urllib.request.Request(
        BOT_URL, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "X-Picky-Group-Secret": secret}, method="POST")
    with urllib.request.urlopen(req, timeout=420) as r:  # pictures take a few minutes
        return json.loads(r.read().decode()) or {}


SEND_SCRIPT = [
    "on run argv",
    'tell application "Messages" to send (item 1 of argv) to chat id (item 2 of argv)',
    "end run",
]


SEND_FILE_SCRIPT = [
    "on run argv",
    "set f to POSIX file (item 1 of argv)",
    'tell application "Messages" to send f to chat id (item 2 of argv)',
    "end run",
]


def send(chat_guid: str, text: str, script: list[str] = SEND_SCRIPT) -> bool:
    """AppleScript in this (picky) user's session. Text (or file path) and guid
    go in argv, never spliced into the script. Newer macOS sometimes wants the
    'iMessage;' form of an 'any;' guid, so that's tried second."""
    guids = [chat_guid]
    if chat_guid.startswith("any;"):
        guids.append("iMessage;" + chat_guid[4:])
    for g in guids:
        args = ["osascript"] + sum((["-e", ln] for ln in script), []) + [text, g]
        r = subprocess.run(args, capture_output=True, text=True, timeout=30)
        if r.returncode == 0:
            return True
        log.warning("send failed guid=...%s rc=%s err=%s", g[-6:], r.returncode, r.stderr.strip()[:200])
    return False


# ---------------------------------------------------------------------------
# proactive posts (outbox)
# ---------------------------------------------------------------------------

GROUPS_SQL = f"""
SELECT c.guid, c.chat_identifier, h.id FROM chat c
LEFT JOIN chat_handle_join j ON j.chat_id = c.ROWID
LEFT JOIN handle h ON h.ROWID = j.handle_id
WHERE c.style = {GROUP_STYLE}
"""


def group_chats(db: sqlite3.Connection) -> list[dict]:
    by: dict[str, dict] = {}
    for guid, ident, h in db.execute(GROUPS_SQL).fetchall():
        g = by.setdefault(guid, {"guid": guid, "identifier": ident, "handles": []})
        if h:
            g["handles"].append(h)
    return list(by.values())


def is_group(db: sqlite3.Connection, guid: str) -> bool:
    row = db.execute("SELECT style FROM chat WHERE guid = ?", (guid,)).fetchone()
    return bool(row) and row[0] == GROUP_STYLE


def _post(url: str, payload: dict, secret: str, timeout: float = 10) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "X-Picky-Group-Secret": secret}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode()) or {}


SEND_DM_SCRIPT = [
    "on run argv",
    'tell application "Messages"',
    "set svc to 1st account whose service type = iMessage",
    "send (item 1 of argv) to participant (item 2 of argv) of svc",
    "end tell",
    "end run",
]


def send_dm(handle: str, text: str) -> bool:
    """A 1-on-1 text (straggler nudges): the existing DM chat if Picky has one with that
    handle, else iMessage straight to the handle. Text and handle go in argv only."""
    if not re.fullmatch(r"\+\d{8,15}|[^@\s]+@[^@\s]+\.[a-z]{2,}", handle or "", re.I):
        return False
    if send(f"any;-;{handle}", text):
        return True
    args = ["osascript"] + sum((["-e", ln] for ln in SEND_DM_SCRIPT), []) + [text, handle]
    r = subprocess.run(args, capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        log.warning("dm send failed to=...%s rc=%s err=%s", handle[-4:], r.returncode, r.stderr.strip()[:200])
    return r.returncode == 0


def drain_outbox(db: sqlite3.Connection, secret: str, rate: RateLimit, groups: list[dict] | None) -> None:
    """One outbox check: report groups (if given), send at most one post (a group post,
    or a DM when the item has "to": the sidecar only hands those to a watcher that says dm_ok)."""
    payload = {"groups": groups} if groups is not None else {}
    payload["dm_ok"] = True
    try:
        item = _post(OUTBOX_URL, payload, secret).get("item")
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        log.debug("outbox unreachable: %s", e)
        return
    if not item:
        return
    chat, texts = item.get("chat") or "", [t for t in item.get("texts") or [] if t]
    to = item.get("to") or ""
    sent, ok, err = 0, True, ""
    if not to and not is_group(db, chat):  # group posts only ever go to a group chat
        ok, err = False, "not a group chat in Picky's Messages"
    for t in texts if ok else []:
        wait = rate.wait_needed()
        if wait is None:
            ok, err = False, "hourly cap"
            break
        if wait:
            time.sleep(wait)
        if not (send_dm(to, t) if to else send(chat, t)):
            ok, err = False, "send failed"
            break
        rate.mark()
        sent += 1
    log.info("outbox %s kind=%s sent=%d/%d %s", item.get("id"), item.get("kind"), sent, len(texts), err)
    try:
        _post(OUTBOX_URL + "/ack", {"id": item.get("id"), "sent": sent, "ok": ok, "error": err}, secret)
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        log.warning("outbox ack failed for %s: %s", item.get("id"), e)


# ---------------------------------------------------------------------------
# state + loop
# ---------------------------------------------------------------------------


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    STATE_DIR.mkdir(mode=0o700, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    os.replace(tmp, STATE_FILE)


def main() -> int:
    if os.environ.get("USER", "") == "Agent" or HOME.name == "Agent":
        print("refusing to run as Agent: Picky only sends from the picky user", file=sys.stderr)
        return 2
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=str(LOG_FILE), level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    secret = SECRET_FILE.read_text().strip() if SECRET_FILE.exists() else ""
    if not secret:
        log.error("no secret at %s", SECRET_FILE)
        return 1
    # first Apple event to Messages: raises the Automation prompt at setup time,
    # not on the first real @Picky
    r = subprocess.run(["osascript", "-e", 'tell application "Messages" to count chats'],
                       capture_output=True, text=True, timeout=60)
    log.info("messages automation check rc=%s %s", r.returncode, r.stderr.strip()[:120])
    state = load_state()
    rate = RateLimit()
    db = None
    pending: dict[int, float] = {}  # rowid -> first time seen with no text
    last_outbox, last_groups, last_delivery = 0.0, 0.0, 0.0
    log.info("watcher up, last_rowid=%s", state.get("last_rowid"))
    while True:
        try:
            if db is None:
                db = connect()
                if "last_rowid" not in state:  # first run: never answer the backlog
                    state["last_rowid"] = max_rowid(db)
                    save_state(state)
            rows = db.execute(NEW_MESSAGES, (state["last_rowid"],)).fetchall()
            cols = ["ROWID", "text", "attributedBody", "date", "handle", "chat_guid", "style",
                    "associated_message_type", "cache_has_attachments"]
            for raw in rows:
                row = dict(zip(cols, raw))
                if wait_for_text(row, pending, time.time()):
                    break  # not written yet: re-read from this row next poll
                ok, why = should_act(row, time.time())
                if ok:
                    handle_one(row, secret, rate, db)
                state["last_rowid"] = row["ROWID"]
                save_state(state)
            if time.time() - last_outbox >= OUTBOX_POLL:
                last_outbox = time.time()
                groups = None
                if time.time() - last_groups >= GROUPS_EVERY:
                    last_groups, groups = time.time(), group_chats(db)
                drain_outbox(db, secret, rate, groups)
            if time.time() - last_delivery >= DELIVERY_EVERY:
                last_delivery = time.time()
                check_deliveries(db, state, last_delivery)
        except sqlite3.OperationalError as e:
            # most often: Full Disk Access not granted yet
            log.error("chat.db unreadable (Full Disk Access?): %s", e)
            db = None
            time.sleep(30)
        except Exception as e:  # noqa: BLE001  keep polling no matter what
            log.exception("loop error: %s", e)
        time.sleep(POLL_SECONDS)


def handle_one(row: dict, secret: str, rate: RateLimit, db: sqlite3.Connection | None = None) -> None:
    text = row["text"] or decode_attributed_body(row["attributedBody"]) or ""
    handle = row["handle"] or ""
    dm = row["style"] != GROUP_STYLE
    # DM bodies from people Picky won't answer stay out of the log
    log.info("%s rowid=%s from=...%s preview=%r", "dm" if dm else "trigger", row["ROWID"], handle[-4:],
             f"<{len(text)} chars>" if dm else preview(text))
    wait = rate.wait_needed()
    if wait is None:
        log.warning("hourly cap hit, skipping rowid=%s", row["ROWID"])
        return
    try:
        recent = [] if dm else recent_messages(db, row["chat_guid"], row["ROWID"])
        out = ask_bot(handle, text, secret, chat=row["chat_guid"], dm=dm, recent=recent)
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        log.error("bot unreachable for rowid=%s: %s", row["ROWID"], e)
        return
    reply, image = out.get("reply"), out.get("image")
    if not reply and not image:
        log.info("no reply for rowid=%s", row["ROWID"])
        return
    wait = rate.wait_needed()
    if wait is None:
        log.warning("hourly cap hit, skipping rowid=%s", row["ROWID"])
        return
    if wait:
        time.sleep(wait)
    if reply and send(row["chat_guid"], reply):
        rate.mark()
        log.info("replied rowid=%s preview=%r", row["ROWID"], preview(reply))
    if image:
        send_image(row, image)


def send_image(row: dict, b64: str) -> None:
    import base64
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"picky-{row['ROWID']}.png"
    try:
        path.write_bytes(base64.b64decode(b64))
    except (ValueError, OSError) as e:
        log.error("image write failed rowid=%s: %s", row["ROWID"], e)
        return
    if send(row["chat_guid"], str(path), SEND_FILE_SCRIPT):
        log.info("sent image rowid=%s", row["ROWID"])
    for old in sorted(OUT_DIR.glob("picky-*.png"))[:-20]:  # keep the last 20
        old.unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(main())
