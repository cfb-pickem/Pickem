"""
Picky's proactive posts to the league iMessage group: config, kill switch,
dry run, send log, group detection and the outbox the watcher drains.

Nothing here sends anything. post() drops a JSON file in a private outbox;
the watcher (tools/picky-imessage/watcher.py, macOS user `picky`) asks the
sidecar (group_chat.py POST /outbox) for the next item, sends it with
AppleScript in Picky's own Messages session under the same rate limiter as
its replies, and acks it. Never from Agent's Messages.

Every proactive post has to get past, in order:
  1. the kill switch PICKY_GROUP_PAUSE (checked again by the sidecar at drain
     time, so pausing also stops anything already queued);
  2. a detected league group (the group chat in Picky's chat.db holding the
     most mapped member handles, at least `min_member_handles`). Until Picky
     is added to the group nothing posts; once he is, posting arms itself;
  3. dry run (config "dry_run", default true): print + log, never queue;
  4. once-only keys (a recap per week, a reminder per slot, a ping per game);
  5. quiet hours (23:30-08:00 CT) for posts that ask for them (score pings);
  6. a daily cap (default 4 per rolling 24 h). The recap, pick reminders and the
     Saturday post (PRIORITY_KINDS) may use every slot; everything else stops one
     short so there's always room for them. Over the cap = skipped, logged and
     marked done (no late backlog).
  7. a hard length cap (Charlie, 2026-11-19: "make sure the posts aren't too
     long, I imagine no one is reading them"): one text of at most max_chars
     (280); the recap and the picks-up breakdown (LONG_KINDS) at most
     max_chars_long (450) over at most 2 texts. Writers aim under it and get one
     rewrite, callers fall back to short code versions; anything still over is
     cut here at line/sentence boundaries (logged "clipped"). Never the long one.
Every attempt, skip, dry run, queue and delivery goes to the send log.

Private files (never in the public repo), /Users/Agent/cfb-pickem-private/:
  picky_group_config.json   group guid, dry_run, name_stragglers, caps
  PICKY_GROUP_PAUSE         exists = paused
  picky_outbox/             queued posts (0600)
  picky_group_sends.jsonl   the send log
  picky_group_posts_state.json  once-only keys
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import sqlite3
import time
import uuid
import zoneinfo
from pathlib import Path

CT = zoneinfo.ZoneInfo("America/Chicago")
PRIVATE = Path(os.environ.get("PICKY_PRIVATE_DIR", "/Users/Agent/cfb-pickem-private"))
CONFIG_FILE = PRIVATE / "picky_group_config.json"
PAUSE_FILE = PRIVATE / "PICKY_GROUP_PAUSE"
OUTBOX = PRIVATE / "picky_outbox"
SEND_LOG = PRIVATE / "picky_group_sends.jsonl"
STATE_FILE = PRIVATE / "picky_group_posts_state.json"
MEMBERS_FILE = PRIVATE / "picky_group_members.json"
PICKY_CHAT_DB = Path(os.environ.get("PICKY_CHAT_DB", "/Users/picky/Library/Messages/chat.db"))
GROUP_STYLE = 43

DEFAULTS = {
    "group_guid": None,             # chat.guid in Picky's chat.db, e.g. "any;+;chat123..."
    "group_chat_identifier": None,
    "dry_run": True,                # print + log instead of queueing
    "name_stragglers": True,        # Charlie 2026-11-15: "call out by name" (never their picks)
    "min_member_handles": 6,        # a group needs this many mapped members to count as the league
    "daily_cap": 4,                 # proactive posts per rolling 24 h (Charlie 2026-11-19: not too chatty)
    "max_chars": 280,               # every scheduled post: ONE text, at most this many characters
    "max_chars_long": 450,          # the weekly recap and the picks-up breakdown (LONG_KINDS)
    "second_reminder": False,       # the 2nd group reminder 2 h before a non-Saturday lock (straggler DMs cover it)
    "ping_hold_min": 120,           # hold a final-score ping while another pool game is still on, up to this long
    "quiet_start": "23:30",
    "quiet_end": "08:00",
}
LONG_KINDS = ("recap", "announce", "breakdown")    # max_chars_long, up to LONG_MAX_TEXTS texts
LONG_MAX_TEXTS = 2
PRIORITY_KINDS = ("recap", "remind", "saturday")   # may use the last slot under the daily cap


# -----------------------------------------------------------------------------
# files
# -----------------------------------------------------------------------------


def _write_private(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + f".{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.replace(tmp, p)


def _read_json(p: Path, default):
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return default


def config() -> dict:
    return DEFAULTS | _read_json(CONFIG_FILE, {})


def save_config(**updates) -> dict:
    cur = _read_json(CONFIG_FILE, {})
    cur.update(updates)
    _write_private(CONFIG_FILE, json.dumps(DEFAULTS | cur, indent=2) + "\n")
    return DEFAULTS | cur


def paused() -> bool:
    return PAUSE_FILE.exists()


IMG_OK_FILE = PRIVATE / "picky_watcher_img_ok"  # touched on each watcher poll that says img_ok
IMG_OK_MAX_AGE = 600


def img_ready(now: float | None = None) -> bool:
    """A watcher that sends picture replies polled lately and the outbox isn't paused (else send text)."""
    try:
        return not paused() and (now or time.time()) - IMG_OK_FILE.stat().st_mtime < IMG_OK_MAX_AGE
    except OSError:
        return False


def log_event(event: str, **fields) -> None:
    rec = {"ts": dt.datetime.now(CT).isoformat(timespec="seconds"), "event": event} | fields
    PRIVATE.mkdir(parents=True, exist_ok=True)
    fd = os.open(SEND_LOG, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a") as f:
        f.write(json.dumps(rec) + "\n")


def recent_posts(n: int = 12) -> list[str]:
    """Texts of the last n posts that went out (or were dry-run), newest last:
    the writer sees them so openers and jokes don't repeat."""
    out = []
    try:
        lines = SEND_LOG.read_text().splitlines()[-400:]
    except OSError:
        return []
    for ln in lines:
        try:
            r = json.loads(ln)
        except ValueError:
            continue
        if r.get("event") in ("queued", "dry-run") and r.get("texts"):
            out.append(" / ".join(r["texts"]))
    return out[-n:]


# -----------------------------------------------------------------------------
# once-only keys
# -----------------------------------------------------------------------------


def state() -> dict:
    return _read_json(STATE_FILE, {})


def save_state(st: dict) -> None:
    _write_private(STATE_FILE, json.dumps(st, indent=1))


def done(key: str) -> bool:
    return key in state().get("keys", {})


def mark(key: str, how: str) -> None:
    st = state()
    st.setdefault("keys", {})[key] = {"how": how, "ts": time.time()}
    cutoff = time.time() - 120 * 86400
    st["keys"] = {k: v for k, v in st["keys"].items() if v.get("ts", 0) > cutoff}
    save_state(st)


def clear_dry_run_keys() -> int:
    """Going live: posts that were only dry-run this week may go out for real."""
    st = state()
    keys = st.get("keys", {})
    drop = [k for k, v in keys.items() if v.get("how") == "dry-run"]
    for k in drop:
        keys.pop(k)
    save_state(st)
    return len(drop)


# -----------------------------------------------------------------------------
# group detection
# -----------------------------------------------------------------------------


def norm_handle(h: str) -> str:
    h = (h or "").strip()
    if h.lower().startswith(("mailto:", "tel:")):
        h = h.split(":", 1)[1]
    if "@" in h:
        return h.lower()
    d = re.sub(r"\D", "", h)
    if len(d) == 10:
        d = "1" + d
    return "+" + d if d else ""


def member_handles() -> dict[str, str]:
    """normalized handle -> team"""
    out = {}
    for m in _read_json(MEMBERS_FILE, {}).get("members") or []:
        for h in m.get("handles") or []:
            if norm_handle(h):
                out[norm_handle(h)] = m.get("team")
    return out


def member_names() -> dict[str, str]:
    """team -> what the group calls them"""
    return {m["team"]: (m.get("name") or m["team"]) for m in _read_json(MEMBERS_FILE, {}).get("members") or []
            if m.get("team")}


def pick_group(groups: list[dict], members: dict[str, str], min_n: int) -> dict | None:
    """The group holding the most mapped members (at least min_n)."""
    best, best_n = None, 0
    for g in groups:
        n = len({norm_handle(h) for h in g.get("handles") or []} & set(members))
        if n > best_n:
            best, best_n = g, n
    if best is None or best_n < min_n:
        return None
    return best | {"member_count": best_n}


def groups_from_chat_db(path: Path = PICKY_CHAT_DB) -> list[dict]:
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    try:
        rows = db.execute(
            "SELECT c.ROWID, c.guid, c.chat_identifier, h.id FROM chat c "
            "LEFT JOIN chat_handle_join j ON j.chat_id = c.ROWID LEFT JOIN handle h ON h.ROWID = j.handle_id "
            f"WHERE c.style = {GROUP_STYLE}").fetchall()
    finally:
        db.close()
    by = {}
    for rid, guid, ident, h in rows:
        g = by.setdefault(rid, {"guid": guid, "identifier": ident, "handles": []})
        if h:
            g["handles"].append(h)
    return list(by.values())


def update_group(groups: list[dict]) -> dict | None:
    """Store the league group if found (or changed). Returns it."""
    cfg = config()
    g = pick_group(groups, member_handles(), int(cfg["min_member_handles"]))
    if g and g["guid"] != cfg.get("group_guid"):
        save_config(group_guid=g["guid"], group_chat_identifier=g.get("identifier"))
        log_event("group-detected", guid=g["guid"], identifier=g.get("identifier"), members=g["member_count"])
    return g


def detect() -> dict | None:
    try:
        return update_group(groups_from_chat_db())
    except sqlite3.Error as e:  # Agent may lack read access under launchd: the watcher reports groups too
        import logging
        logging.getLogger("picky.outbox").info("chat.db detect skipped: %s", e)
        return None


# -----------------------------------------------------------------------------
# post + outbox
# -----------------------------------------------------------------------------


def _hm(s: str) -> dt.time:
    h, m = s.split(":")
    return dt.time(int(h), int(m))


def in_quiet_hours(now: dt.datetime, cfg: dict | None = None) -> bool:
    cfg = cfg or config()
    t, a, b = now.astimezone(CT).time(), _hm(cfg["quiet_start"]), _hm(cfg["quiet_end"])
    return (t >= a or t < b) if a > b else (a <= t < b)


def _posts_last_day() -> int:
    n, cutoff = 0, time.time() - 86400
    try:
        lines = SEND_LOG.read_text().splitlines()[-500:]
    except OSError:
        return 0
    for ln in lines:
        try:
            r = json.loads(ln)
        except ValueError:
            continue
        if r.get("event") == "queued" and dt.datetime.fromisoformat(r["ts"]).timestamp() > cutoff:
            n += 1
    return n


def limits(kind: str, cfg: dict | None = None) -> tuple[int, int]:
    """(max characters over the whole post, max texts) for a post kind."""
    cfg = cfg or config()
    if kind in LONG_KINDS:
        return int(cfg["max_chars_long"]), LONG_MAX_TEXTS
    return int(cfg["max_chars"]), 1


def fits(texts: list[str], max_chars: int, max_texts: int = 1) -> bool:
    return 0 < len(texts) <= max_texts and sum(len(t) for t in texts) <= max_chars


_ITEM = re.compile(r"(?<=[.!?;])\s+")


def clip(texts: list[str], max_chars: int, max_texts: int = 1) -> list[str]:
    """The post as-is if it fits, else ONE text: the texts joined by line breaks, then whole lines /
    sentences / ';'-items kept in order while they fit (so put what matters first). A lead piece that's
    too long on its own is cut at a word with an ellipsis. Never longer than max_chars."""
    texts = [t.strip() for t in texts if t and t.strip()]
    if not texts or fits(texts, max_chars, max_texts):
        return texts
    pieces = []  # (separator before it, piece)
    for line in "\n".join(texts).splitlines():
        for i, it in enumerate(x.strip() for x in _ITEM.split(line.strip()) if x.strip()):
            pieces.append(("\n" if i == 0 else " ", it))
    out = ""
    for sep, it in pieces:
        cand = out + sep + it if out else it
        if len(cand) <= max_chars:
            out = cand
        elif not out:  # the lead piece alone is too long: cut it at a word
            out = it[:max_chars - 1].rsplit(" ", 1)[0].rstrip(",;:- ") + "\u2026"
            break
        else:
            break
    return [re.sub(r"[;,:]$", ".", out)]


def post(kind: str, texts: list[str], key: str, expires_s: int = 6 * 3600, quiet: bool = False,
         dry_run: bool | None = None, now: dt.datetime | None = None, echo=print) -> str:
    """Queue one proactive post (one short text; LONG_KINDS up to 2) for the group, cut to its
    length limit if it's over. Returns what happened: paused | no-group | dup | quiet | capped | dry-run |
    queued."""
    now = now or dt.datetime.now(CT)
    cfg = config()
    texts = [t.strip() for t in texts if t and t.strip()]
    if not texts:
        log_event("skipped", kind=kind, key=key, why="empty")
        return "empty"
    lim, n_texts = limits(kind, cfg)
    if not fits(texts, lim, n_texts):  # the last line of defense: never the long one
        short = clip(texts, lim, n_texts)
        log_event("clipped", kind=kind, key=key, before=sum(len(t) for t in texts), texts_before=len(texts),
                  after=sum(len(t) for t in short))
        texts = short
    dry = cfg["dry_run"] if dry_run is None else dry_run
    base = {"kind": kind, "key": key, "texts": texts}
    if paused() and not dry:
        log_event("paused", **base)
        echo(f"[paused] {kind} {key}: " + " / ".join(texts))
        return "paused"
    if done(key):
        return "dup"
    if quiet and in_quiet_hours(now, cfg):
        log_event("skipped", why="quiet-hours", **base)
        mark(key, "quiet")
        return "quiet"
    if dry:
        log_event("dry-run", group=bool(cfg.get("group_guid")), **base)
        echo(f"[dry-run] {kind} {key}:\n" + "\n".join(f"  > {t}" for t in texts))
        mark(key, "dry-run")
        return "dry-run"
    if not cfg.get("group_guid"):
        log_event("skipped", why="no-group", **base)
        return "no-group"
    room = int(cfg["daily_cap"]) - (0 if kind in PRIORITY_KINDS else 1)
    if _posts_last_day() >= room:
        log_event("skipped", why="daily-cap", **base)
        mark(key, "capped")  # skipped for good: no backlog posting it hours late
        return "capped"
    item = {"id": uuid.uuid4().hex[:12], "kind": kind, "key": key, "texts": texts,
            "created": now.timestamp(), "expires": now.timestamp() + expires_s}
    _write_private(OUTBOX / f"{int(now.timestamp())}-{item['id']}.json", json.dumps(item))
    mark(key, "queued")
    log_event("queued", id=item["id"], **base)
    return "queued"


def post_dm(team: str, handle: str, texts: list[str], key: str, expires_s: int = 3600,
            dry_run: bool | None = None, now: dt.datetime | None = None, echo=print,
            allow_unmapped: bool = False) -> str:
    """Queue a 1-on-1 text (straggler nudges) to one mapped league member. Same switches as
    post() (pause, once-only key, dry run) minus the group cap; logged as dm-* events so
    DMs never count toward the group's daily cap or its recent-posts list. Only handles in
    the private member map (allow_unmapped is for straggler_nudges' self-test to Picky's
    own Apple ID). Returns paused | dup | dry-run | queued | unmapped | empty."""
    now = now or dt.datetime.now(CT)
    cfg = config()
    texts = [t.strip() for t in texts if t and t.strip()]
    h = norm_handle(handle)
    base = {"kind": "dm", "key": key, "team": team, "to": "..." + h[-4:], "texts": texts}
    if not texts or not h:
        return "empty"
    if not allow_unmapped and member_handles().get(h) != team:
        log_event("dm-skipped", why="unmapped", **base)
        return "unmapped"
    dry = cfg["dry_run"] if dry_run is None else dry_run
    if paused() and not dry:
        log_event("dm-paused", **base)
        return "paused"
    if done(key):
        return "dup"
    if dry:
        log_event("dm-dry-run", **base)
        echo(f"[dry-run] dm {team} {key}:\n" + "\n".join(f"  > {t}" for t in texts))
        mark(key, "dry-run")
        return "dry-run"
    item = {"id": uuid.uuid4().hex[:12], "kind": "dm", "key": key, "texts": texts, "to": h, "team": team,
            "created": now.timestamp(), "expires": now.timestamp() + expires_s}
    _write_private(OUTBOX / f"{int(now.timestamp())}-{item['id']}.json", json.dumps(item))
    mark(key, "queued")
    log_event("dm-queued", id=item["id"], **base)
    return "queued"


def post_picture(chat_guid: str, text: str, image_b64: str | None, expires_s: int = 1800,
                 now: dt.datetime | None = None, fallback: str | None = None) -> str:
    """A finished picture (or a short 'the brush slipped' line) back to the chat that asked: the group or a
    DM (review R2: pictures are async). Not a proactive post: no once-only key, no daily cap, no quiet hours;
    the kill switch still holds it. Only a watcher that says img_ok gets it. Returns queued | paused | empty.
    `fallback`: text queued to the same chat if the picture fails to send or expires unsent (the text bracket)."""
    now = now or dt.datetime.now(CT)
    texts = [text.strip()] if text and text.strip() else []
    if not chat_guid or not (texts or image_b64):
        return "empty"
    if paused():
        log_event("picture-paused", chat="..." + chat_guid[-6:])
        return "paused"
    item = {"id": uuid.uuid4().hex[:12], "kind": "picture", "key": f"picture:{uuid.uuid4().hex[:8]}",
            "texts": texts, "chat": chat_guid, "image": image_b64, "reply": True,
            "created": now.timestamp(), "expires": now.timestamp() + expires_s}
    if image_b64 and fallback and fallback.strip():
        item["fallback"] = fallback.strip()
    _write_private(OUTBOX / f"{int(now.timestamp())}-{item['id']}.json", json.dumps(item))
    log_event("picture-queued", id=item["id"], chat="..." + chat_guid[-6:], image=bool(image_b64))
    return "queued"


def _picture_fallback(item: dict, why: str) -> None:
    """A picture that failed or expired unsent: its fallback text goes to the same chat instead (text only,
    no further fallback)."""
    if item.get("fallback") and item.get("chat"):
        st = post_picture(item["chat"], item["fallback"], None)
        log_event("picture-fallback", id=item.get("id"), why=why[:100], result=st)


def next_item(now: float | None = None, dm_ok: bool = False, img_ok: bool = False) -> dict | None:
    """For the sidecar: the oldest live item, or None when paused, dry run or
    no group. Expired items are dropped (a reminder after lock is worse than
    none). DM items ({"to": handle}) only go to a watcher that says it can send
    them (dm_ok); an older watcher would only ever send to a group. Picture
    replies ({"reply": true, "chat", "image"}) only go to a watcher that says img_ok."""
    cfg = config()
    if paused() or cfg["dry_run"] or not cfg.get("group_guid") or not OUTBOX.exists():
        return None
    now = now or time.time()
    for p in sorted(OUTBOX.glob("*.json")):
        item = _read_json(p, None)
        if not item:
            p.unlink(missing_ok=True)
            continue
        if item.get("expires", 0) < now:
            log_event("expired", id=item.get("id"), kind=item.get("kind"), key=item.get("key"))
            p.unlink(missing_ok=True)
            _picture_fallback(item, "expired")
            continue
        if item.get("to") and not dm_ok:
            continue
        if item.get("reply") and not img_ok:
            continue
        if item.get("claimed_at") and now - item["claimed_at"] < 600:
            continue  # the watcher has it; give it 10 min to ack
        item["claimed_at"] = now
        _write_private(p, json.dumps(item))
        if item.get("to"):
            return {"id": item["id"], "texts": item["texts"], "to": item["to"], "kind": item["kind"]}
        if item.get("reply"):
            return {"id": item["id"], "texts": item["texts"], "chat": item["chat"], "image": item.get("image"),
                    "reply": True, "kind": item["kind"]}
        return {"id": item["id"], "texts": item["texts"], "chat": cfg["group_guid"], "kind": item["kind"]}
    return None


def ack(item_id: str, sent: int, ok: bool, error: str = "") -> bool:
    for p in OUTBOX.glob(f"*-{re.sub(r'[^0-9a-f]', '', item_id)}.json"):
        item = _read_json(p, {})
        p.unlink(missing_ok=True)
        log_event(("dm-" if item.get("to") else "") + ("delivered" if ok else "delivery-failed"), id=item_id,
                  kind=item.get("kind"),
                  key=item.get("key"), sent=sent, of=len(item.get("texts") or []), error=error[:200])
        if not ok:
            _picture_fallback(item, error or "delivery failed")
        return True
    return False
