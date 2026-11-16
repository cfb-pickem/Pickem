"""Run claude -p (Opus via server/picky_claude.py) over spec batches -> data/raw/<batch>.json. Resumable."""
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(os.environ.get("PICKY_LORA_HOME", "/Users/Agent/picky-lora"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(1, str(Path(__file__).resolve().parents[2] / "server"))
import picky_claude  # noqa: E402  (Opus, zero MCP, no API key, usage log)
from make_specs import MEMBERS  # noqa: E402  (nicknames come from the private member map)

PROMPT = (Path(__file__).resolve().parent / "gen_prompt.md").read_text().replace(
    "{NICKS}", ", ".join(dict.fromkeys(MEMBERS.values())))
# gen.py [data-dir] [batch ...]   (data-dir holds specs.jsonl; output in <data-dir>/raw)
DATA = Path(sys.argv[1]) if len(sys.argv) > 1 and not sys.argv[1].isdigit() else ROOT / "data"
SPECS = [json.loads(l) for l in (DATA / "specs.jsonl").read_text().splitlines()]
RAW = DATA / "raw"
RAW.mkdir(parents=True, exist_ok=True)
BATCH = 10


def run(i):
    out = RAW / f"b{i:03d}.json"
    if out.exists():
        return i, "skip"
    batch = SPECS[i * BATCH:(i + 1) * BATCH]
    keep = ("id", "kind", "chat", "sender", "sender_nick", "target", "rider", "facts", "topic", "recent_lines", "message")
    body = PROMPT + "\n".join(json.dumps({k: s[k] for k in keep if k in s}) for s in batch)
    for attempt in range(2):
        try:
            r = picky_claude.run(body, timeout=600, caller="lora-gen", output_format="text",
                                 cwd="/Users/Agent/.imessage-claude/delegate-cwd")
        except subprocess.TimeoutExpired as e:
            r = subprocess.CompletedProcess(e.cmd, 124, "", "timed out")
        txt = r.stdout.strip()
        try:
            arr = json.loads(txt[txt.index("["):txt.rindex("]") + 1])
            if len(arr) >= len(batch) - 2:
                out.write_text(json.dumps(arr, indent=1))
                return i, f"ok {len(arr)}"
        except ValueError:
            pass
    (RAW / f"b{i:03d}.err").write_text(r.stdout[-3000:] + "\n---\n" + r.stderr[-2000:])
    return i, "FAIL"


if __name__ == "__main__":
    n = (len(SPECS) + BATCH - 1) // BATCH
    only = [int(x) for x in sys.argv[1:] if x.isdigit()] or range(n)
    with ThreadPoolExecutor(4) as ex:
        for i, status in ex.map(run, only):
            print(i, status, flush=True)
