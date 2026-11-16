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
rows = [json.loads(r) for r in tmp.read_text().splitlines()]
text = tmp.read_text()
ok = [
    (len(rows) == 3, "14-day retention drops the old row"),
    ([r["team"] for r in rows] == ["Ackbar", "Ackbar", None], "team names from the map; unmapped -> None"),
    ([r["mode"] for r in rows] == ["group", "dm", "dm"], "mode recorded"),
    ("5555" not in text, "no handles or phone numbers stored"),
    (stat.S_IMODE(os.stat(tmp).st_mode) == 0o600, "file is 0600"),
]
for c, m in ok:
    print(("ok   " if c else "FAIL ") + m)
sys.exit(0 if all(c for c, _ in ok) else 1)
