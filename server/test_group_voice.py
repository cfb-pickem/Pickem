"""
Group voice (group_voice.py, 2026-11-27): the league group's thread -> a private profile -> a style rider on GROUP
replies (local draft tail, coach review, beef, raw), never in DMs, the site or the shared prompt prefix.

Run:  ./.venv/bin/python3 -I test_group_voice.py

Offline: a fake chat.db and fake Opus; the private profile is only read (and only checked when it exists).
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import group_voice as gv  # noqa: E402

REAL_JSON = gv.PROFILE_JSON
REAL_MD = gv.PROFILE_MD
TMP = Path(tempfile.mkdtemp())
GUID = "any;+;testgroup"
MEMBER = "+15555550123"
PHONE = re.compile(r"\d{3}\D?\d{3}\D?\d{4}")

FULL = ("Test rider sentence for the style block. " * 12).strip()
LOCAL = "Local test rider sentence, kept short on purpose. " * 5


def point_at(d: Path) -> None:
    d.mkdir(parents=True, exist_ok=True)
    gv.DIR, gv.RAW, gv.STATE, gv.STATS = d, d / "raw.jsonl", d / "state.json", d / "stats.json"
    gv.PROFILE_JSON, gv.PROFILE_MD = d / "group_voice.json", d / "group_voice.md"
    gv._cache.update(mtime=None, rider="", rider_local="")


def write_profile(d: Path, rider: str = FULL, local: str = LOCAL) -> None:
    point_at(d)
    (d / "group_voice.json").write_text(json.dumps({"rider": rider, "rider_local": local}))


def fake_chat_db(path: Path, texts: list[tuple[int, str, int]]) -> None:
    """texts: (rowid, text, is_from_me); a minimal chat.db with the columns group_voice reads."""
    db = sqlite3.connect(path)
    db.executescript("""
      CREATE TABLE IF NOT EXISTS chat (ROWID INTEGER PRIMARY KEY, guid TEXT);
      CREATE TABLE IF NOT EXISTS handle (ROWID INTEGER PRIMARY KEY, id TEXT);
      CREATE TABLE IF NOT EXISTS chat_message_join (chat_id INTEGER, message_id INTEGER);
      CREATE TABLE IF NOT EXISTS message (ROWID INTEGER PRIMARY KEY, guid TEXT, text TEXT, attributedBody BLOB,
        date INTEGER, is_from_me INTEGER, handle_id INTEGER, associated_message_type INTEGER,
        associated_message_guid TEXT, cache_has_attachments INTEGER, thread_originator_guid TEXT);
      INSERT OR IGNORE INTO chat VALUES (1, 'any;+;testgroup'), (2, 'any;+;othergroup');
      INSERT OR IGNORE INTO handle VALUES (1, '+15555550123');""")
    for rowid, text, me in texts:
        db.execute("INSERT INTO message VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                   (rowid, f"g{rowid}", text, None, 800000000 * 10**9 + rowid, me, 0 if me else 1, 0, None, 0, None))
        db.execute("INSERT INTO chat_message_join VALUES (?, ?)", (1, rowid))
    db.execute("INSERT OR IGNORE INTO message VALUES (999,'x','other chat',NULL,0,0,1,0,NULL,0,NULL)")
    db.execute("INSERT OR IGNORE INTO chat_message_join VALUES (2, 999)")
    db.commit()
    db.close()


def test_rider_loads_full_and_local():
    write_profile(TMP / "load")
    assert gv.rider() == FULL and gv.rider(local=True) == LOCAL.strip()
    b, bl = gv.group_block(), gv.group_block(local=True)
    assert b.startswith("\n\nHOW THIS GROUP TALKS") and FULL in b and LOCAL.strip() in bl and FULL not in bl


def test_size_capped():
    write_profile(TMP / "big", rider="x " * (gv.RIDER_MAX_CHARS // 2 + 10), local="y " * (gv.LOCAL_MAX_CHARS // 2 + 10))
    assert gv.rider() == "" and gv.rider(local=True) == "" and gv.group_block() == ""
    assert gv.valid_rider("x" * (gv.RIDER_MAX_CHARS + 1)) and gv.valid_rider("x" * (gv.LOCAL_MAX_CHARS + 1), local=True)
    assert gv.valid_rider("too short")


def test_phone_or_email_or_sensitive_refused():
    for bad in ("call me at 504-555-0123", "mail a@b.com", "his divorce"):
        write_profile(TMP / "leak", rider=FULL + " " + bad)
        assert gv.rider() == "", bad


def test_off_switch():
    write_profile(TMP / "off")
    with patch.dict(os.environ, {"PICKY_GROUP_VOICE": "0"}):
        assert gv.rider() == "" and gv.group_block(local=True) == ""


def test_export_incremental_scrubbed_and_group_only():
    d = TMP / "export"
    point_at(d)
    db = d / "chat.db"
    fake_chat_db(db, [(1, "hello there", 0), (2, "text me at 312-555-0199 lol", 0), (3, "bot reply", 1)])
    with patch.object(gv, "member_names", lambda: {"5555550123": "TG"}), \
            patch.object(gv, "group_guid", lambda: GUID):
        r1 = gv.export(db)
        fake_chat_db(db, [(4, "fourth text", 0)])
        r2 = gv.export(db)
    rows = gv.read_raw()
    assert (r1["new"], r2["new"], r2["total"]) == (3, 1, 4), (r1, r2)
    assert [r["who"] for r in rows] == ["TG", "TG", "Picky", "TG"]
    raw = gv.RAW.read_text()
    assert "other chat" not in raw and "312-555-0199" not in raw and "[number]" in raw
    assert oct(gv.RAW.stat().st_mode & 0o777) == "0o600"


def test_refresh_threshold_validation_and_save():
    d = TMP / "refresh"
    point_at(d)
    db = d / "chat.db"
    fake_chat_db(db, [(i, f"member text {i}", 0) for i in range(1, 6)])
    calls = []

    def opus(prompt):
        calls.append(prompt)
        return {"profile_md": "# profile\n" + "line\n" * 400, "rider": FULL, "rider_local": LOCAL}

    import group_outbox
    with patch.object(gv, "member_names", lambda: {"5555550123": "TG"}), \
            patch.object(gv, "group_guid", lambda: GUID), patch.object(group_outbox, "PICKY_CHAT_DB", db):
        r = gv.refresh(opus=opus)
        assert not r["distilled"] and not calls, r  # 5 new texts < REDISTILL_MIN
        r = gv.refresh(force=True, opus=lambda p: {"profile_md": "x" * 2000, "rider": FULL + " 504-555-0123",
                                                    "rider_local": LOCAL})
        assert not r["distilled"] and "refused" in r["why"] and not gv.PROFILE_JSON.exists(), r
        r = gv.refresh(force=True, opus=opus)
    assert r["distilled"] and calls and "member text 5" in calls[0], r
    saved = json.loads(gv.PROFILE_JSON.read_text())
    assert saved["rider"] == FULL and saved["distilled_upto_rowid"] == 5 and saved["messages_read"] == 5
    assert oct(gv.PROFILE_JSON.stat().st_mode & 0o777) == "0o600"


def test_group_tail_only():
    import chat
    import group_chat as gc
    import test_leader
    write_profile(TMP / "tail")
    ctx = test_leader.ctx()
    tok = gc.SENDER.set(gc.Sender(handle=MEMBER, team="TG"))
    try:
        assert LOCAL.strip() in chat._system_tail(ctx) and FULL not in chat._system_tail(ctx)
        m = gc.MODE.set("dm")
        assert "HOW THIS GROUP TALKS" not in chat._system_tail(ctx)  # a member's DM
        w = gc.WEB.set(True)
        assert "HOW THIS GROUP TALKS" not in chat._system_tail(ctx)  # the site's chat box
        gc.WEB.reset(w)
        gc.MODE.reset(m)
        m = gc.MODE.set("dm-public")
        assert "HOW THIS GROUP TALKS" not in chat._system_tail(ctx)
        gc.MODE.reset(m)
    finally:
        gc.SENDER.reset(tok)
    # the shared prefix (every process and mode, gemma's checkpoint) never carries it
    assert "HOW THIS GROUP TALKS" not in chat.prompt_prefix(ctx) and LOCAL.strip() not in chat.prompt_prefix(ctx)


def test_opus_prompts_group_only():
    import beef
    import picky_coach
    write_profile(TMP / "opus")
    sg, _ = picky_coach.prompt("who covers", "draft", "FACTS", dm=False)
    sd, _ = picky_coach.prompt("who covers", "draft", "FACTS", dm=True)
    assert FULL in sg and FULL not in sd and sg.index(FULL) < sg.index("LEAGUE FACTS:")
    sb, _ = beef.prompt("Ackbar", ["refund"], "", "SHEET", "Fri")
    assert FULL in sb and sb.index(FULL) < sb.index("\n\nFACT SHEET (")
    point_at(TMP / "none")  # no profile: prompts unchanged
    assert "HOW THIS GROUP TALKS" not in picky_coach.prompt("q", "d", "F", dm=False)[0]
    assert "HOW THIS GROUP TALKS" not in beef.prompt("Ackbar", ["x"], "", "S", "Fri")[0]


def test_real_profile_clean():
    """The live private profile (when this Mac has it): valid size, no phone numbers / emails / sensitive words."""
    if not REAL_JSON.exists():
        print("      (no private profile here: skipped)")
        return
    d = json.loads(REAL_JSON.read_text())
    assert gv.valid_rider(d.get("rider", "")) == [], gv.valid_rider(d.get("rider", ""))
    assert gv.valid_rider(d.get("rider_local", ""), local=True) == []
    assert not gv.leaks(REAL_MD.read_text()) and not PHONE.search(REAL_MD.read_text())
    assert oct(REAL_JSON.stat().st_mode & 0o777) == "0o600" and oct(REAL_MD.stat().st_mode & 0o777) == "0o600"


if __name__ == "__main__":
    tests = [v for k, v in dict(globals()).items() if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  ok  {t.__name__}")
        except Exception as e:  # noqa: BLE001
            print(f"FAIL  {t.__name__}: {type(e).__name__}: {e}")
            failed += 1
    print("\nALL GREEN" if not failed else f"\n{failed} FAILED")
    sys.exit(1 if failed else 0)
