"""Turn the week's weak real Picky exchanges into specs for fresh training examples.

Reads cfb-pickem-private/picky_exchanges.jsonl (last 7 days), has claude -p grade
each reply (helpful/human/wit) and classify the text, and for each weak one emits
a spec that keeps the member's REAL message but gets a fresh RANDOM fact block
(make_specs.facts_for), so the rewrite teaches style, never the league's stats.
Usage: harvest.py <out-specs.jsonl> [max]
"""
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import make_specs as ms  # noqa: E402

LOG = Path("/Users/Agent/cfb-pickem-private/picky_exchanges.jsonl")
SEEN = Path("/Users/Agent/picky-lora/data/harvested_ts.json")  # exchanges already graded
EMPTY_MCP = "/Users/Agent/cfb-pickem-private/empty.mcp.json"
KINDS = [t for t, _, _ in ms.TYPES]
PROMPT = """You're reviewing replies from "Picky", the trash-talking bot member of a college football pick'em league's group text. For each exchange below, grade Picky's reply 1-5 on:
- helpful: did it actually answer what was asked, clearly and up front? (a sharp comeback counts for a heckle; a real opinion counts for an off-topic question)
- human: does it read like a real friend texting, not a bot or a report?
- wit: is it actually funny or clever?
Then classify the member's text as one kind from: {kinds}
and name the league member it's about, if any (a team name from: {teams}; else null).

Output ONLY a JSON array, one object per exchange, in order:
[{{"i": 0, "helpful": 1-5, "human": 1-5, "wit": 1-5, "kind": "...", "target": "team or null"}}, ...]

EXCHANGES:
"""


def main():
    out, cap = Path(sys.argv[1]), int(sys.argv[2]) if len(sys.argv) > 2 else 80
    cut = time.time() - 7 * 86400
    rows = [r for r in map(json.loads, LOG.read_text().splitlines() if LOG.exists() else [])
            if r.get("ts", 0) >= cut and r.get("q") and r.get("a")]
    seen = set(json.loads(SEEN.read_text())) if SEEN.exists() else set()
    rows = [r for r in rows if r["ts"] not in seen][-200:]
    specs = []
    for b in range(0, len(rows), 25):
        batch = rows[b:b + 25]
        body = PROMPT.format(kinds=", ".join(KINDS), teams=", ".join(ms.MEMBERS)) + "\n".join(
            json.dumps({"i": i, "chat": r["mode"], "sender_team": r.get("team"), "member": r["q"][:400],
                        "picky": r["a"][:600]}) for i, r in enumerate(batch))
        res = subprocess.run(["/usr/bin/perl", "-e", "alarm shift; exec @ARGV", "600", "/opt/homebrew/bin/claude",
                              "-p", "--model", "sonnet", "--strict-mcp-config", "--mcp-config", EMPTY_MCP,
                              "--output-format", "text"], input=body, capture_output=True, text=True,
                             cwd="/Users/Agent/.imessage-claude/delegate-cwd")
        try:
            t = res.stdout
            grades = json.loads(t[t.index("["):t.rindex("]") + 1])
        except ValueError:
            print("grading batch failed:", res.stdout[-300:], res.stderr[-300:])
            continue
        for g in grades:
            if not isinstance(g, dict) or not isinstance(g.get("i"), int) or g["i"] >= len(batch):
                continue
            if min(g.get("helpful", 5), g.get("human", 5), g.get("wit", 5)) >= 3:
                continue  # good enough: only weak replies get rewritten
            r = batch[g["i"]]
            sender = r.get("team") if r.get("team") in ms.MEMBERS else "(unmatched)"
            spec = ms.make_one(f"h{out.parent.name}-{len(specs):04d}", kind=g.get("kind"), sender=sender,
                               target=g.get("target"), chat="dm" if r["mode"] == "dm" else "group",
                               message=r["q"].strip())
            spec["harvested"] = True
            specs.append(spec)
    specs = specs[:cap]
    SEEN.write_text(json.dumps(sorted(seen | {r["ts"] for r in rows})))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("".join(json.dumps(s) + "\n" for s in specs))
    print(f"exchanges {len(rows)} -> weak {len(specs)}")


if __name__ == "__main__":
    main()
