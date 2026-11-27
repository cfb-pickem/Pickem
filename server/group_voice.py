"""
Picky's group voice (Charlie, 2026-11-27: "You should read every message in the group to get a sense of how the
group talks to each other"). Picky reads the league group's whole thread in his own chat.db, a profile of how the
group talks is distilled from it, and a compact, viewer-neutral rider built from that profile rides on GROUP replies
and on the Opus-written ones (beef mode, the coach's rewrites), so Picky sounds like he belongs in the chat.

  EXPORT. The league group (group_outbox config "group_guid") in Picky's Messages chat.db (PICKY_CHAT_DB, read-only):
  every message, oldest first, incremental by ROWID. Text from message.text or the attributedBody typedstream (the
  watcher's decoder), tapbacks kept as reactions, senders mapped to team names through the private member map
  (never a phone number or email). Appended to the PRIVATE raw file group_voice/raw.jsonl (0600). Picky's own
  account only joined the group on 2026-11-15, so that's where its history starts (iMessage never backfills).

  PROFILE. cfb-pickem-private/group_voice.md (human-readable, 0600) + group_voice/group_voice.json {"rider", ...}.
  The rider is ~300-500 tokens of style only: tone, roast style, slang, nicknames, rhythm, loves / hates. No member
  lines quoted word for word, no phone numbers, nothing sensitive or non-league. rider_local is a ~150-token cut of
  it for the local draft model (the group tail sits after gemma's prefix checkpoint, so every token there is
  re-read on every draft: ~100 tokens is roughly half a second per draft).

  REFRESH (nightly, rides com.pickem.member-memory's 22:30 run; also `group_voice.py refresh`): export the new
  messages; when there are at least REDISTILL_MIN new member texts since the last distill, ONE Opus call (the
  picky_claude booster gate, capped, no tools) gets the current profile + rider and the new messages and returns an
  updated profile + rider. A validator (size cap, no phone / email, no sensitive words) keeps the old rider on any
  failure. Claude off / gate shut = the old rider stays (zero maintenance).

  LIVE. rider() is read mtime-memoized. rider(local=True) goes in the per-caller tail of group replies
  (group_chat._system_tail, after the cached prompt_prefix, so the shared prefix every process and mode uses and its
  checkpoint never change); the full rider goes into the beef prompt, raw mode and the coach's review prompt for
  group texts. Never in DMs or the site. PICKY_GROUP_VOICE=0 turns it off.

  CLI:  group_voice.py export | stats | refresh [--force] [--dry] | rider [--local] | status
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # run as a script under -I

PRIVATE = Path(os.environ.get("PICKY_PRIVATE_DIR", "/Users/Agent/cfb-pickem-private"))
DIR = Path(os.environ.get("PICKY_GROUP_VOICE_DIR", str(PRIVATE / "group_voice")))
RAW = DIR / "raw.jsonl"
STATE = DIR / "state.json"
STATS = DIR / "stats.json"
PROFILE_JSON = DIR / "group_voice.json"
PROFILE_MD = Path(os.environ.get("PICKY_GROUP_VOICE_MD", str(PRIVATE / "group_voice.md")))
APPLE_EPOCH = 978307200
RIDER_MAX_CHARS = 2400          # ~500 tokens: a longer rider is refused (the old one stays)
RIDER_MIN_CHARS = 400
LOCAL_MAX_CHARS = 800           # the local draft model's cut (~150 tokens): the group tail is re-read every draft
LOCAL_MIN_CHARS = 200
REDISTILL_MIN = 25              # new member texts before the nightly asks Opus to update the profile
DISTILL_TIMEOUT = 900
DISTILL_NEW_MAX = 1500          # newest messages sent to one distill call
TAPBACK = {2000: "loved", 2001: "liked", 2002: "disliked", 2003: "laughed at", 2004: "emphasized",
           2005: "questioned", 2006: "reacted to"}

_PHONE = re.compile(r"(?<![\d-])(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}(?![\d-])")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[a-z]{2,}", re.I)
# kept out of the profile and the rider (Charlie: only the banter and football culture)
SENSITIVE = re.compile(
    r"\b(?:cancer|chemo|surgery|hospital|diagnos\w*|divorce\w*|pregnan\w*|funeral|passed away|died|rehab|"
    r"therapy|therapist|depress\w*|laid off|fired from|bankrupt\w*|debt|mortgage|salary|custody|miscarriage|"
    r"addict\w*|overdose|suicid\w*)\b", re.I)


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def scrub(text: str) -> str:
    return _EMAIL.sub("[email]", _PHONE.sub("[number]", str(text or "")))


def leaks(text: str) -> list[str]:
    """Phone numbers / emails / sensitive words in a profile or rider (any hit = refused)."""
    t = str(text or "")
    return [m.group(0) for r in (_PHONE, _EMAIL, SENSITIVE) for m in r.finditer(t)]


# ---- export ----------------------------------------------------------------------------------------------

def decode_attributed_body(blob: bytes | None) -> str | None:
    """Plain text out of message.attributedBody (same decoder as tools/picky-imessage/watcher.py)."""
    if not blob:
        return None
    i = blob.find(b"NSString")
    if i < 0:
        return None
    j = blob.find(b"+", i)
    if j < 0 or j + 1 >= len(blob):
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


def _digits(h: str) -> str:
    d = re.sub(r"\D", "", h or "")
    return d[-10:] if len(d) >= 10 else (h or "").strip().lower()


def member_names() -> dict[str, str]:
    """handle (last 10 digits / lowercased email) -> team name, from the private member map."""
    import group_outbox
    m = group_outbox._read_json(group_outbox.MEMBERS_FILE, {}) if hasattr(group_outbox, "MEMBERS_FILE") else {}
    if not m:
        m = _read_json(PRIVATE / "picky_group_members.json", {})
    out = {}
    for row in (m.get("members") or []) if isinstance(m, dict) else []:
        hs = row.get("handles") or []
        for h in ([hs] if isinstance(hs, str) else hs):
            if row.get("team"):
                out[_digits(str(h))] = row["team"]
    return out


def group_guid() -> str | None:
    import group_outbox
    return os.environ.get("PICKY_GROUP_GUID") or group_outbox.config().get("group_guid")


SQL = """SELECT m.ROWID, m.guid, m.text, m.attributedBody, m.date, m.is_from_me, h.id AS handle,
        m.associated_message_type, m.associated_message_guid, m.cache_has_attachments, m.thread_originator_guid
 FROM message m JOIN chat_message_join cm ON cm.message_id = m.ROWID JOIN chat c ON c.ROWID = cm.chat_id
 LEFT JOIN handle h ON h.ROWID = m.handle_id
 WHERE c.guid = ? AND m.ROWID > ? ORDER BY m.ROWID"""


def read_chat(after: int = 0, path: Path | None = None, guid: str | None = None) -> list[dict]:
    """The league group's messages with ROWID > after, oldest first (sender = team name / "Picky")."""
    import group_outbox
    path = path or group_outbox.PICKY_CHAT_DB
    guid = guid or group_guid()
    if not guid:
        raise RuntimeError("no league group_guid in the group config")
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    db.row_factory = sqlite3.Row
    try:
        rows = db.execute(SQL, (guid, int(after))).fetchall()
        by_guid = {r["guid"]: r for r in rows}
    finally:
        db.close()
    names, out = member_names(), []
    for r in rows:
        text = (r["text"] or decode_attributed_body(r["attributedBody"]) or "").replace("\ufffc", "").strip()
        who = "Picky" if r["is_from_me"] else names.get(_digits(r["handle"] or ""), "a guest")
        ts = APPLE_EPOCH + (r["date"] or 0) / 1e9
        rec = {"rowid": r["ROWID"], "ts": round(ts), "at": dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M"),
               "who": who, "text": scrub(text)}
        amt = r["associated_message_type"] or 0
        if 2000 <= amt < 3000:
            tgt = by_guid.get(re.sub(r"^(?:p:\d+/|bp:)", "", r["associated_message_guid"] or ""))
            rec["reaction"] = TAPBACK.get(amt, "reacted to")
            if tgt is not None:
                tt = (tgt["text"] or decode_attributed_body(tgt["attributedBody"]) or "").strip()
                rec["to"] = scrub(tt)[:80]
            rec["text"] = ""
        elif 3000 <= amt < 4000:
            continue  # a removed tapback
        if r["cache_has_attachments"] and not rec.get("reaction"):
            rec["attachment"] = True
        if r["thread_originator_guid"]:
            rec["reply"] = True
        out.append(rec)
    return out


def export(path: Path | None = None) -> dict:
    """Append the new group messages to raw.jsonl. Returns {"new", "total", "last_rowid"}."""
    st = _read_json(STATE, {})
    after = int(st.get("last_rowid") or 0)
    new = read_chat(after, path)
    if new:
        DIR.mkdir(parents=True, exist_ok=True)
        os.chmod(DIR, 0o700)
        fd = os.open(RAW, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a") as f:
            for rec in new:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        os.chmod(RAW, 0o600)
        st["last_rowid"] = new[-1]["rowid"]
    st["total"] = int(st.get("total") or 0) + len(new)
    st["exported_at"] = round(time.time())
    _write_private(STATE, json.dumps(st, indent=1) + "\n")
    return {"new": len(new), "total": st["total"], "last_rowid": st.get("last_rowid", after)}


def read_raw() -> list[dict]:
    out = []
    try:
        for ln in RAW.read_text().splitlines():
            try:
                out.append(json.loads(ln))
            except ValueError:
                continue
    except OSError:
        pass
    return out


# ---- code stats (message length, caps, emoji, reactions) ---------------------------------------------------

_EMOJI = re.compile("[\U0001F300-\U0001FAFF\u2600-\u27BF]")


def stats(rows: list[dict] | None = None) -> dict:
    rows = read_raw() if rows is None else rows
    texts = [r for r in rows if r.get("text") and r.get("who") != "Picky"]
    reacts = [r for r in rows if r.get("reaction") and r.get("who") != "Picky"]
    by = {}
    for r in texts:
        b = by.setdefault(r["who"], {"texts": 0, "chars": 0, "caps": 0, "emoji": 0, "q": 0, "no_punct_end": 0})
        t = r["text"]
        b["texts"] += 1
        b["chars"] += len(t)
        letters = [c for c in t if c.isalpha()]
        b["caps"] += int(len(letters) >= 4 and sum(c.isupper() for c in letters) / len(letters) > 0.7)
        b["emoji"] += int(bool(_EMOJI.search(t)))
        b["q"] += int("?" in t)
        b["no_punct_end"] += int(not re.search(r"[.!?]$", t))
    lens = sorted(len(r["text"].split()) for r in texts)
    react_kinds = {}
    for r in reacts:
        react_kinds[r["reaction"]] = react_kinds.get(r["reaction"], 0) + 1
    ats = [r["ts"] for r in rows if r.get("ts")]
    return {
        "messages": len(rows), "member_texts": len(texts), "reactions": len(reacts),
        "picky_messages": sum(1 for r in rows if r.get("who") == "Picky"),
        "first": dt.datetime.fromtimestamp(min(ats)).strftime("%Y-%m-%d") if ats else None,
        "last": dt.datetime.fromtimestamp(max(ats)).strftime("%Y-%m-%d") if ats else None,
        "median_words": lens[len(lens) // 2] if lens else 0,
        "p90_words": lens[int(len(lens) * 0.9)] if lens else 0,
        "reaction_kinds": react_kinds,
        "by_member": {k: {"texts": v["texts"], "avg_chars": round(v["chars"] / v["texts"]),
                          "all_caps_pct": round(100 * v["caps"] / v["texts"]),
                          "emoji_pct": round(100 * v["emoji"] / v["texts"]),
                          "no_end_punct_pct": round(100 * v["no_punct_end"] / v["texts"])}
                      for k, v in sorted(by.items(), key=lambda kv: -kv[1]["texts"])},
    }


# ---- the live rider -------------------------------------------------------------------------------------

_cache: dict = {"mtime": None, "rider": "", "rider_local": ""}


def enabled() -> bool:
    return os.environ.get("PICKY_GROUP_VOICE", "1") != "0"


def rider(local: bool = False) -> str:
    """The group-voice rider ("" when off, missing or invalid). local=True: the short cut for Picky's local draft
    model. Stable: changes only when the nightly refresh rewrites the profile (mtime-memoized)."""
    if not enabled():
        return ""
    try:
        m = PROFILE_JSON.stat().st_mtime
    except OSError:
        return ""
    if _cache["mtime"] != m:
        d = _read_json(PROFILE_JSON, {})
        full, short = str(d.get("rider") or "").strip(), str(d.get("rider_local") or "").strip()
        _cache.update(mtime=m, rider=full if not valid_rider(full) else "",
                      rider_local=short if not valid_rider(short, local=True) else "")
    return _cache["rider_local" if local else "rider"]


def valid_rider(text: str, local: bool = False) -> list[str]:
    """Why a rider can't go live ([] = fine)."""
    lo, hi = (LOCAL_MIN_CHARS, LOCAL_MAX_CHARS) if local else (RIDER_MIN_CHARS, RIDER_MAX_CHARS)
    bad = []
    if not lo <= len(text or "") <= hi:
        bad.append(f"size {len(text or '')} outside {lo}-{hi}")
    hits = leaks(text)
    if hits:
        bad.append(f"leaks {hits[:3]}")
    return bad


def group_block(local: bool = False) -> str:
    """The rider as a prompt block for GROUP replies ("" when off). local=True for the local draft model's group
    tail; the full rider for the Opus prompts (coach review, beef, raw). Never for DMs or the site."""
    r = rider(local)
    return ("\n\nHOW THIS GROUP TALKS (style only: borrow the vibe, never copy a member's line word for word; facts "
            "still come only from the data, and every guardrail above still holds):\n" + r) if r else ""


# ---- nightly refresh (one Opus call, incremental) --------------------------------------------------------

SCHEMA = {"type": "object", "properties": {
    "profile_md": {"type": "string"}, "rider": {"type": "string"}, "rider_local": {"type": "string"}},
    "required": ["profile_md", "rider", "rider_local"]}

DISTILL = """You keep the "group voice" profile for Picky, the AI member of a college football pick'em league's
iMessage group (friends picking 4 games a week against the spread). The goal: Picky sounds like he belongs in this
group. Below are the CURRENT PROFILE, the CURRENT RIDER and the NEW MESSAGES since the last update (oldest first;
senders are league team names, "Picky" is the bot, tapbacks shown as reactions).

Return JSON {{"profile_md", "rider", "rider_local"}}:
- profile_md: the full updated profile in markdown, same sections as the current one (tone and humor; roast style,
  what lands and what's off-limits in practice; slang, phrases and in-jokes with each running joke's origin when
  it's clear; each member's nicknames, how people address each other and a line or two on each member's texting
  style; message length, punctuation / caps, emoji and reactions; rivalries, beefs, teams / coaches / players they
  love or hate; 10-20 short real example exchanges, lightly trimmed). Keep what's still true, add what's new, drop
  what's gone stale. Stats line: {stats}
- rider: {min_chars}-{max_chars} characters, plain text, instructions to Picky on HOW TO SOUND in this group
  (style only, no facts, records or scores; no member lines quoted word for word; nicknames are fine). Viewer
  neutral: it rides on every group reply no matter who's asking.
- rider_local: the same guidance cut to {local_min}-{local_max} characters (the few points that matter most), for a
  small local model.
Rules: no phone numbers or emails. Nothing sensitive or non-league (health, money problems, family matters, work
drama): only the banter and football culture. Members by team name or the nicknames the group itself uses.

CURRENT PROFILE:
{profile}

CURRENT RIDER:
{rider}

CURRENT RIDER_LOCAL:
{rider_local}

NEW MESSAGES:
{new}"""


def render(rows: list[dict]) -> str:
    out = []
    for r in rows:
        if r.get("reaction"):
            out.append(f"[{r['at']}] {r['who']} {r['reaction']} \"{r.get('to', '')}\"")
        elif r.get("text") or r.get("attachment"):
            out.append(f"[{r['at']}] {r['who']}: {r.get('text') or ''}{' [attachment]' if r.get('attachment') else ''}")
    return "\n".join(out)


def _opus(prompt: str) -> dict:
    import picky_claude
    p = picky_claude.run(prompt, timeout=DISTILL_TIMEOUT, caller="group_voice", tools="", schema=SCHEMA, cwd=str(DIR))
    if p.returncode != 0:
        raise RuntimeError(f"claude exit {p.returncode}: {(p.stderr or p.stdout)[-300:]}")
    out = json.loads(p.stdout)
    if out.get("is_error") or not isinstance(out.get("structured_output"), dict):
        raise RuntimeError(f"claude result not usable: {str(out.get('result'))[:300]}")
    return out["structured_output"]


def save_profile(profile_md: str, rider_text: str, local_text: str, upto_rowid: int, by: str,
                 st: dict | None = None) -> None:
    st = stats() if st is None else st
    cur = _read_json(PROFILE_JSON, {})
    _write_private(PROFILE_MD, profile_md.rstrip() + "\n")
    _write_private(PROFILE_JSON, json.dumps({
        "rider": rider_text.strip(), "rider_local": local_text.strip(), "updated": dt.datetime.now().isoformat(timespec="seconds"), "by": by,
        "distilled_upto_rowid": int(upto_rowid), "messages_read": st["messages"], "range": [st["first"], st["last"]],
        "history": (cur.get("history") or [])[-20:] + [{"at": round(time.time()), "by": by, "upto": int(upto_rowid)}],
    }, indent=1, ensure_ascii=False) + "\n")


def refresh(force: bool = False, dry: bool = False, opus=None) -> dict:
    """Nightly: export, stats, and (enough new member texts, or force) one Opus update. Never raises."""
    res = {"exported": None, "distilled": False, "why": ""}
    try:
        res["exported"] = export()
    except Exception as e:  # noqa: BLE001 - chat.db unreadable under launchd etc.: the old rider stays
        res["why"] = f"export failed: {e}"
    rows = read_raw()
    st = stats(rows)
    _write_private(STATS, json.dumps(st, indent=1, ensure_ascii=False) + "\n")
    cur = _read_json(PROFILE_JSON, {})
    upto = int(cur.get("distilled_upto_rowid") or 0)
    new = [r for r in rows if int(r.get("rowid") or 0) > upto]
    member_new = sum(1 for r in new if r.get("text") and r.get("who") != "Picky")
    if not force and member_new < REDISTILL_MIN:
        res["why"] = res["why"] or f"{member_new} new member texts (< {REDISTILL_MIN}): profile kept"
        return res
    if not new:
        res["why"] = "nothing new"
        return res
    try:
        profile = PROFILE_MD.read_text()
    except OSError:
        profile = "(none yet: write it from scratch)"
    prompt = DISTILL.format(stats=json.dumps({k: st[k] for k in ("messages", "member_texts", "reactions", "first",
                                                                  "last", "median_words", "p90_words")}),
                            min_chars=RIDER_MIN_CHARS, max_chars=RIDER_MAX_CHARS - 200, profile=profile,
                            local_min=LOCAL_MIN_CHARS, local_max=LOCAL_MAX_CHARS - 100,
                            rider=cur.get("rider") or "(none yet)", rider_local=cur.get("rider_local") or "(none yet)",
                            new=render(new[-DISTILL_NEW_MAX:]))
    if dry:
        res["why"] = f"dry: would distill {len(new)} new messages ({len(prompt)} prompt chars)"
        return res
    try:
        out = (opus or _opus)(prompt)
        md, rd = str(out.get("profile_md") or ""), str(out.get("rider") or "").strip()
        rl = str(out.get("rider_local") or "").strip()
        bad = valid_rider(rd) + valid_rider(rl, local=True) + ([f"profile leaks {leaks(md)[:3]}"] if leaks(md) else []) + (
            ["profile too short"] if len(md) < 1500 else [])
        if bad:
            res["why"] = f"refused: {bad}"
            return res
        save_profile(md, rd, rl, new[-1]["rowid"], "opus-nightly", st)
        res["distilled"], res["why"] = True, f"updated from {len(new)} new messages"
    except Exception as e:  # noqa: BLE001 - gate shut / call failed: the old profile stays
        res["why"] = f"distill skipped: {type(e).__name__}: {str(e)[:200]}"
    return res


def main(argv: list[str]) -> int:
    cmd = argv[0] if argv else "status"
    if cmd == "export":
        print(json.dumps(export()))
    elif cmd == "stats":
        st = stats()
        _write_private(STATS, json.dumps(st, indent=1, ensure_ascii=False) + "\n")
        print(json.dumps(st, indent=1, ensure_ascii=False))
    elif cmd == "refresh":
        print(json.dumps(refresh(force="--force" in argv, dry="--dry" in argv)))
    elif cmd == "rider":
        r = rider(local="--local" in argv)
        print(r or "(no rider)")
        print(f"\n[{len(r)} chars]", file=sys.stderr)
    else:
        cur = _read_json(PROFILE_JSON, {})
        st = _read_json(STATE, {})
        print(f"group voice {'ON' if rider() else 'OFF (no valid rider)'}; {cur.get('messages_read')} messages read "
              f"{cur.get('range')}; updated {cur.get('updated')} by {cur.get('by')}; exported up to rowid "
              f"{st.get('last_rowid')}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
