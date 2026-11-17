"""
"bad picky": log a flubbed Picky reply to the private flub queue, top priority for tonight's
flub loop (flub_loop.py harvest reads it, then marks the record consumed).

  flag_flub.py --text "..." [--image /path] [--note "..."] [--source coordinator|group|dm] [--chat ...]

Appends one record to /Users/Agent/cfb-pickem-private/flubs/queue.jsonl (0600; PICKY_FLUB_QUEUE
overrides), copies any image into flubs/img/, and attaches the last ~6 live exchanges from the
last 2 hours (picky_exchanges.jsonl, test handles and side sidecars skipped) so the harvester can
find the flubbed reply even when --text is empty. Prints one line: "logged flub <id>".

group_chat.py calls log_flub() when a mapped member texts "bad picky" / "that's wrong" (DM, or
@Picky in the league group) with the previous Picky exchange in that chat as the context.
Never texts anyone; never in the public repo's data (the queue lives in the private dir).
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import secrets
import shutil
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

CT = ZoneInfo("America/Chicago")
PRIVATE = Path("/Users/Agent/cfb-pickem-private")
CONTEXT_N = 6
CONTEXT_AGE = 2 * 3600
SOURCES = ("coordinator", "group", "dm")

# a whole-message flag ("@Picky that's wrong", "bad picky", "bad bot lol"); longer corrections
# ("that's wrong, Clam is first") go through the normal pipeline and the harvester's pushback scan
FLAG = re.compile(
    r"^(?:(?:picky|nope|no|nah|lol|lmao|bro|dude|yo|uh|um|hey)[\s,.!]+)*"
    r"(?:bad (?:picky|bot)|that(?:'s| is|s) (?:wrong|not right|incorrect|not true|false|a flub)|"
    r"wrong answer|flub(?:bed)?(?: it)?|you flubbed(?: it| that)?)"
    r"(?:[\s,.!]+(?:picky|bro|dude|lol|lmao|man|again))*[\s.!?]*$", re.I)


def is_flag(text: str) -> bool:
    t = re.sub(r"[^\w\s',.!?]", " ", (text or "").replace("’", "'")).strip()
    return bool(FLAG.match(re.sub(r"\s+", " ", t)))


def queue_path() -> Path:
    return Path(os.environ.get("PICKY_FLUB_QUEUE") or PRIVATE / "flubs" / "queue.jsonl")


def exchange_log() -> Path:
    return Path(os.environ.get("PICKY_EXCHANGE_LOG") or PRIVATE / "picky_exchanges.jsonl")


def chat_hash(key: str) -> str:
    """A short, stable id for a chat (group guid or DM handle) that stores no handle."""
    return hashlib.sha256(key.encode()).hexdigest()[:10] if key else ""


def recent_exchanges(now: float | None = None, chat: str = "", n: int = CONTEXT_N,
                     max_age: float = CONTEXT_AGE) -> list[dict]:
    """The last n live exchanges from the last max_age seconds (test handles = team null and side
    sidecars skipped). With `chat` (a chat_hash), only that chat's rows when rows carry one."""
    now = time.time() if now is None else now
    out = []
    try:
        lines = exchange_log().read_text().splitlines()
    except OSError:
        return []
    for ln in lines:
        try:
            r = json.loads(ln)
        except ValueError:
            continue
        if not r.get("team") or r.get("side") or now - float(r.get("ts") or 0) > max_age:
            continue
        if chat and r.get("chat") and r["chat"] != chat:
            continue
        out.append({k: r[k] for k in ("ts", "mode", "team", "q", "a", "lane", "chat", "secs") if k in r})
    return out[-n:]


def _append(path: Path, rec: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a") as f:
        f.write(json.dumps(rec) + "\n")
    os.chmod(path, 0o600)


def log_flub(text: str = "", image: str | None = None, note: str = "", source: str = "coordinator",
             chat: str = "", context: list[dict] | None = None, now: float | None = None) -> str:
    """Append one high-priority record; returns its id. `chat` is a chat_hash (or a raw chat key,
    hashed here). `context` defaults to the recent live exchanges."""
    now = time.time() if now is None else now
    if source not in SOURCES:
        raise ValueError(f"source must be one of {SOURCES}")
    if chat and not re.fullmatch(r"[0-9a-f]{10}", chat):
        chat = chat_hash(chat)
    fid = "q-" + dt.datetime.fromtimestamp(now, CT).strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)
    q = queue_path()
    img = None
    if image:
        src = Path(image).expanduser()
        if not src.is_file():
            raise FileNotFoundError(f"no image at {src}")
        d = q.parent / "img"
        d.mkdir(parents=True, exist_ok=True)
        dst = d / f"{fid}{src.suffix.lower() or '.img'}"
        shutil.copyfile(src, dst)
        os.chmod(dst, 0o600)
        img = str(dst)
    rec = {"id": fid, "ts": round(now), "priority": "high", "source": source, "text": (text or "").strip(),
           "note": (note or "").strip(), "image": img, "chat": chat,
           "context": context if context is not None else recent_exchanges(now, chat), "consumed": None}
    _append(q, rec)
    return fid


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="flag_flub.py", description="log a flubbed Picky reply ('bad picky')")
    ap.add_argument("--text", default="", help="the bad reply (or what was wrong); may be empty")
    ap.add_argument("--image", default=None, help="a screenshot to keep with it")
    ap.add_argument("--note", default="", help="what it should have said")
    ap.add_argument("--source", default="coordinator", choices=SOURCES)
    ap.add_argument("--chat", default="", help="chat key or hash, to narrow the context")
    a = ap.parse_args(argv)
    try:
        fid = log_flub(a.text, a.image, a.note, a.source, a.chat)
    except (OSError, ValueError) as e:
        print(f"flag_flub failed: {e}", file=sys.stderr)
        return 1
    print(f"logged flub {fid}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
