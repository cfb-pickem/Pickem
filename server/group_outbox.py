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
  6. a daily cap.
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
    "daily_cap": 12,                # proactive posts per rolling 24 h
    "quiet_start": "23:30",
    "quiet_end": "08:00",
}


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


def post(kind: str, texts: list[str], key: str, expires_s: int = 6 * 3600, quiet: bool = False,
         dry_run: bool | None = None, now: dt.datetime | None = None, echo=print) -> str:
    """Queue one proactive post (a few short texts) for the group. Returns
    what happened: paused | no-group | dup | quiet | capped | dry-run | queued."""
    now = now or dt.datetime.now(CT)
    cfg = config()
    texts = [t.strip() for t in texts if t and t.strip()]
    if not texts:
        log_event("skipped", kind=kind, key=key, why="empty")
        return "empty"
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
    if _posts_last_day() >= int(cfg["daily_cap"]):
        log_event("skipped", why="daily-cap", **base)
        return "capped"
    item = {"id": uuid.uuid4().hex[:12], "kind": kind, "key": key, "texts": texts,
            "created": now.timestamp(), "expires": now.timestamp() + expires_s}
    _write_private(OUTBOX / f"{int(now.timestamp())}-{item['id']}.json", json.dumps(item))
    mark(key, "queued")
    log_event("queued", id=item["id"], **base)
    return "queued"


def next_item(now: float | None = None) -> dict | None:
    """For the sidecar: the oldest live item, or None when paused, dry run or
    no group. Expired items are dropped (a reminder after lock is worse than
    none)."""
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
            continue
        if item.get("claimed_at") and now - item["claimed_at"] < 600:
            continue  # the watcher has it; give it 10 min to ack
        item["claimed_at"] = now
        _write_private(p, json.dumps(item))
        return {"id": item["id"], "texts": item["texts"], "chat": cfg["group_guid"], "kind": item["kind"]}
    return None


def ack(item_id: str, sent: int, ok: bool, error: str = "") -> bool:
    for p in OUTBOX.glob(f"*-{re.sub(r'[^0-9a-f]', '', item_id)}.json"):
        item = _read_json(p, {})
        p.unlink(missing_ok=True)
        log_event("delivered" if ok else "delivery-failed", id=item_id, kind=item.get("kind"),
                  key=item.get("key"), sent=sent, of=len(item.get("texts") or []), error=error[:200])
        return True
    return False
