"""
group_chat's private exchange log (training data for the style-LoRA cycle):
team names only, never handles; 14-day retention; 0600.

Run:  ./.venv/bin/python3 -I test_exchange_log.py
"""

import json
import os
import stat
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import group_chat as gc  # noqa: E402

tmp = Path(tempfile.mkdtemp()) / "x.jsonl"
members = {"+15555550100": {"team": "Ackbar", "name": "Ackbar"}}
with patch.object(gc, "EXCHANGE_LOG", tmp), patch.object(gc, "load_members", return_value=members):
    old = 1_000_000.0
    gc.log_exchange("group:g1", "old q", "old a", "+15555550100", old)
    now = old + 15 * 86400
    gc.log_exchange("group:g1", "who's first?", "Clam, by two.", "+15555550100", now)
    gc.log_exchange("dm:+15555550100", "my record?", "3-1, not bad.", None, now)
    gc.log_exchange("dm:+15555550199", "hi", "hey", None, now)
    # the site widget (key web:<uid>, no handle): the team comes from the request's Sender; a forwarded
    # X-Picky-Test ask (test=true) is marked so the nightly flub harvest skips it
    tok = gc.SENDER.set(gc.Sender(handle="web:abcd1234", team="Ackbar", user_id="abcd1234-uid"))
    gc.log_exchange("web:abcd1234-uid", "ATS records?", "Yep.", None, now)
    ttok = gc.WEB_TEST.set(True)
    gc.log_exchange("web:abcd1234-uid", "scripted", "x", None, now)
    gc.WEB_TEST.reset(ttok)
    gc.SENDER.reset(tok)
web = [json.loads(r) for r in tmp.read_text().splitlines()][3:]
rows = [json.loads(r) for r in tmp.read_text().splitlines()][:3]
text = tmp.read_text()
ok = [
    (len(rows) == 3, "14-day retention drops the old row"),
    ([r["team"] for r in rows] == ["Ackbar", "Ackbar", None], "team names from the map; unmapped -> None"),
    ([r["mode"] for r in rows] == ["group", "dm", "dm"], "mode recorded"),
    ("5555" not in text, "no handles or phone numbers stored"),
    (stat.S_IMODE(os.stat(tmp).st_mode) == 0o600, "file is 0600"),
    ([(r["mode"], r["team"], r.get("test")) for r in web] == [("web", "Ackbar", None), ("web", "Ackbar", 1)],
     "web widget rows carry the caller's team; site test asks are marked test"),
    ("abcd1234" not in text, "no site uid stored"),
]
for c, m in ok:
    print(("ok   " if c else "FAIL ") + m)
sys.exit(0 if all(c for c, _ in ok) else 1)
