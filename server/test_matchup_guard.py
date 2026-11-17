"""
Home/away guard (matchup_guard.py): code says "Away at Home"; a reply that flips a known game
("Missouri State at New Mexico State" for New Mexico State at Missouri State, exam 2026-11-16)
gets swapped back, in the group sidecar's last step and in chat.py's sentence stream.
Model-free, offline.

Run:  ./.venv/bin/python3 -I test_matchup_guard.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
TMP = Path(tempfile.mkdtemp(prefix="picky-matchup-"))
os.environ["PICKY_LIVE_CACHE"] = str(TMP / "live_games.json")
(TMP / "live_games.json").write_text(json.dumps({"games": {
    "1": {"id": "1", "state": "post", "away": {"name": "New Mexico State"}, "home": {"name": "Missouri State"}},
    "2": {"id": "2", "state": "pre", "away": {"name": "Ohio State"}, "home": {"name": "Michigan"}},
    "3": {"id": "3", "state": "pre", "away": {"name": "Texas"}, "home": {"name": "Texas A&M"}}}}))
import matchup_guard as mg  # noqa: E402

FAILED = []


def check(name, ok, got=None):
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else f"\n     got: {got!r}"))
    if not ok:
        FAILED.append(name)


EXAM = ("I don't have the pool line for Missouri State at New Mexico State, so I can't tell you if they covered.")
check("exam reply: flipped matchup swapped back (live board pairs)",
      mg.fix(EXAM) == EXAM.replace("Missouri State at New Mexico State", "New Mexico State at Missouri State"),
      mg.fix(EXAM))
check("canonical string is Away at Home", mg.canonical("New Mexico State", "Missouri State")
      == "New Mexico State at Missouri State")
ok = "New Mexico State at Missouri State went to OT."
check("correct order untouched", mg.fix(ok) == ok, mg.fix(ok))
check("lowercase + @ form fixed to the canonical spelling",
      mg.fix("michigan @ ohio state is the one") == "Ohio State @ Michigan is the one", mg.fix("michigan @ ohio state is the one"))
check("start of the reply fixed", mg.fix("Texas A&M at Texas, I'd take Texas.") == "Texas at Texas A&M, I'd take Texas.",
      mg.fix("Texas A&M at Texas, I'd take Texas."))
w = "Western Michigan at Ohio State is a different game."
check("longer name ending in a known team untouched", mg.fix(w) == w, mg.fix(w))
check("names inside other words untouched", mg.fix("Michigan State at Ohio State") == "Michigan State at Ohio State")
both = [("Utah", "Iowa"), ("Iowa", "Utah")]
check("pair known both ways (rematch) untouched", mg.fix("Iowa at Utah", both) == "Iowa at Utah")
check("caller pairs work without a board", mg.fix("Iowa at Utah", [("Utah", "Iowa")]) == "Utah at Iowa")
check("no 'at' / no pairs: same text", mg.fix("Picky likes Iowa.", []) == "Picky likes Iowa." and mg.fix("", None) == "")
check("unknown game untouched", mg.fix("Baylor at TCU tonight") == "Baylor at TCU tonight")

src = (HERE / "group_chat.py").read_text()
check("group sidecar runs the guard on both reply paths", src.count("matchup_guard.fix(") == 2)
check("chat.py sentence stream runs the guard", "sent = matchup_guard.fix(sent)" in (HERE / "chat.py").read_text())

if FAILED:
    print(f"\n{len(FAILED)} FAILED")
    sys.exit(1)
print("\nALL PASS")
