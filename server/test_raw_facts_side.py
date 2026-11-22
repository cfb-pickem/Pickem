"""
Raw Opus mode's facts (group_chat._raw_facts -> flub_loop.build_facts) import eval/run.py, which marks
its process side traffic (os.environ.setdefault("PICKY_SIDE", "1")). In the live sidecar that stuck for
the life of the process (11-19 raw window -> 11-21): every member reply logged side=1 (the nightly flub
harvest skipped all of them) and live texts ran at low Ollama priority without setting the live flag.
The live process must come out of _raw_facts exactly as live as it went in.

Run:  ./.venv/bin/python3 -I test_raw_facts_side.py
"""

import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
os.environ.pop("PICKY_SIDE", None)
os.environ.pop("PICKY_GROUP_PORT", None)
import flub_loop  # noqa: E402
import group_chat as gc  # noqa: E402
import live_flag  # noqa: E402


def fake_build_facts():
    sys.path.insert(0, str(HERE / "eval"))
    import run  # noqa: F401  (the real module: its import-time side effects are the point)
    return {"facts": "Week 12: Iowa at Illinois, Iowa -8.5"}


ok = []
before = live_flag.is_side()
gc._RAW_FACTS.update(at=0.0, text="")
with patch.object(flub_loop, "build_facts", fake_build_facts):
    facts = asyncio.run(gc._raw_facts())
ok += [
    ("Iowa" in facts, "raw facts still returned"),
    (before is False, "test starts as a live process"),
    ("PICKY_SIDE" not in os.environ, "importing eval/run.py for raw facts leaves PICKY_SIDE unset"),
    (live_flag.is_side() is False, "the live process is still live after raw facts"),
]
bad = [m for good, m in ok if not good]
for good, m in ok:
    print(("ok   " if good else "FAIL ") + m)
print("FAILS:", " ".join(bad) if bad else "none")
sys.exit(1 if bad else 0)
