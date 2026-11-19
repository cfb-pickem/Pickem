"""
picky_claude's per-caller model map (Charlie 2026-11-19: lighter boosters off Opus).

Lighter boosters run on Haiku / Sonnet; the flub loop, improve-nightly, every exam judge and any caller not
in the map stay on Opus; anything run from server/eval stays on Opus whatever its caller (judge scores stay
comparable); PICKY_CLAUDE_MODEL overrides every caller. The model a call actually used is what the usage log
records. No network, no real claude.

Run:  ./.venv/bin/python3 -I test_claude_models.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
TMP = Path(tempfile.mkdtemp(prefix="picky-claude-models-"))
os.environ["PICKY_CLAUDE_USAGE_DIR"] = str(TMP / "usage")
os.environ.pop("PICKY_CLAUDE_MODEL", None)

import picky_claude as pc  # noqa: E402

FAILED: list[str] = []


def check(name, ok, got=None):
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else f"\n     got: {got!r}"[:900]))
    if not ok:
        FAILED.append(name)


OPUS, SONNET, HAIKU = "claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"
JOB = str(HERE / "game_cards.py")          # a scheduled booster's entry script
EXAM = str(HERE / "eval" / "live_exam.py")  # exam tooling

check("default model is Opus", pc.MODEL == OPUS, pc.MODEL)
check("no caller = Opus (improve-nightly's pc.command())", pc.model_for(None, JOB) == OPUS)
for c in ("flub_loop", "flub_fix", "improve-nightly", "judge", "fact_judge", "spar-judge", "spar-gen",
          "live_exam", "sat_dm_judge", "judge_text", "judge_quality", "judge_agree", "refresh", "some_new_job"):
    check(f"{c} stays on Opus", pc.model_for(c, JOB) == OPUS, pc.model_for(c, JOB))
check("every mapped model is a real tier", set(pc.CALLER_MODELS.values()) <= {HAIKU, SONNET, OPUS},
      pc.CALLER_MODELS)
check("no judge / flub caller is in the map", not any(
    k.startswith(("judge", "fact_judge", "spar", "live_exam", "sat_dm", "flub", "improve")) for k in pc.CALLER_MODELS),
    sorted(pc.CALLER_MODELS))
for c, m in pc.CALLER_MODELS.items():
    check(f"{c} -> {m} from a booster", pc.model_for(c, JOB) == m, pc.model_for(c, JOB))
    check(f"{c} stays Opus when run from server/eval", pc.model_for(c, EXAM) == OPUS, pc.model_for(c, EXAM))
some = next(iter(pc.CALLER_MODELS))
with patch.dict(os.environ, {"PICKY_CLAUDE_MODEL": "claude-test-x"}):
    check("PICKY_CLAUDE_MODEL overrides a mapped caller", pc.model_for(some, JOB) == "claude-test-x")
    check("PICKY_CLAUDE_MODEL overrides an unmapped caller", pc.model_for("flub_loop", JOB) == "claude-test-x")
    check("PICKY_CLAUDE_MODEL overrides eval", pc.model_for("judge", EXAM) == "claude-test-x")
check("command() default model is Opus", pc.command("")[pc.command("").index("--model") + 1] == OPUS)
cmd = pc.command("", model=HAIKU)
check("command(model=) passes it to --model", cmd[cmd.index("--model") + 1] == HAIKU, cmd)

# run(): the spawned --model and the usage log line are the per-caller choice
fake = TMP / "claude"
fake.write_text("#!/bin/sh\necho \"$@\" >> \"$(dirname \"$0\")/argv\"\ncat > /dev/null\necho '{}'\n")
fake.chmod(0o755)
mapped = {m: c for c, m in pc.CALLER_MODELS.items()}
with patch.dict(os.environ, {"PICKY_NO_ANTHROPIC": "0", "PICKY_OPUS_MANUAL": "1"}), \
        patch.object(pc, "CLAUDE", str(fake)), patch.object(sys, "argv", [JOB]):
    for c in (mapped.get(HAIKU), mapped.get(SONNET), "flub_loop"):
        if c:
            pc.run("p", timeout=30, caller=c, tools="")
argv = (TMP / "argv").read_text().splitlines()
logged = [json.loads(x) for f in (TMP / "usage").glob("*.jsonl") for x in f.read_text().splitlines()]
want = [(c, pc.model_for(c, JOB)) for c in (mapped.get(HAIKU), mapped.get(SONNET), "flub_loop") if c]
check("run() spawns each caller's model", [a.split("--model ")[1].split()[0] for a in argv] == [m for _, m in want],
      argv)
check("usage log records the model each call used", [(r["caller"], r["model"]) for r in logged] == want, logged)

# callers that used to log under their entry script name now name themselves
for f, name in (("burn_book.py", "burn_book"), ("news_qa.py", "news_qa")):
    check(f"{f} passes caller={name!r}", f'caller="{name}"' in (HERE / f).read_text())

print("\nFAILS:", len(FAILED), FAILED if FAILED else "none")
sys.exit(1 if FAILED else 0)
