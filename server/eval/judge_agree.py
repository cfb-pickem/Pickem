"""
How often does text Picky's local judge (picky_brain.judge, the 9B) pick the
draft a stronger grader would? Reads a PICKY_BRAIN_TRACE JSONL from an exam
run, has one offline `claude -p` call (Opus via picky_claude, no tools) pick the best of the
same eligible drafts by the same criteria, and prints the agreement rate.
Below ~70%, best-of-N is mostly noise and the judge needs work.

    cd server
    ./.venv/bin/python3 -I eval/judge_agree.py /tmp/picky-tb/trace.jsonl
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

from judge_text import facts  # noqa: E402

PROMPT = """You're choosing which draft reply "Picky" (the trash-talking bot in a friends' college-football pick'em group chat) should send. For each item, pick the single best draft, in this order of priority:
1. It answers what the member actually asked (dodging, misreading, or answering a different question loses).
2. It's correct against the FACT SHEET (any wrong record, score, pick, cover or standing loses).
3. It sounds like a real, funny friend texting: specific, natural, not robotic.
Approved running bits: "nice milk, freak." for Blinkstradamus; Ackbar is the nemesis; Chuck P is "Dear Leader" when others mention him.

Return JSON only: {"picks": [{"tag": "T01", "best": 2}, ...]} with best = the draft number."""

SCHEMA = {"type": "object", "properties": {"picks": {"type": "array", "items": {
    "type": "object", "properties": {"tag": {"type": "string"}, "best": {"type": "integer"}},
    "required": ["tag", "best"]}}}, "required": ["picks"]}


def main(path: str) -> int:
    rows = [json.loads(x) for x in Path(path).read_text().splitlines() if x.strip()]
    rows = [r for r in rows if r.get("how") == "judge" and len(r.get("eligible") or []) > 1]
    if not rows:
        print("no judged runs in the trace")
        return 1
    blocks, local = [], {}
    for n, r in enumerate(rows, 1):
        tag = f"T{n:02d}"
        drafts = {d["idx"]: d["text"] for d in r["drafts"]}
        el = r["eligible"]
        local[tag] = el.index(r["pick"]) + 1
        blocks.append(f"### {tag} (from {r.get('sender') or 'a member'}): {r['q']}\n"
                      + "\n".join(f"  DRAFT {k + 1}: {drafts[i]}" for k, i in enumerate(el)))
    import dossier
    out = dossier.run_claude(PROMPT + "\n\n## FACT SHEET\n\n" + facts() + "\n\n## ITEMS\n\n" + "\n\n".join(blocks),
                             900, SCHEMA, "", caller="judge_agree")
    claude = {p["tag"]: p["best"] for p in out.get("picks") or []}
    both = [t for t in local if t in claude]
    agree = sum(local[t] == claude[t] for t in both)
    first = sum(claude[t] == 1 for t in both)  # how often draft 1 (the old single answer) was best anyway
    print(f"local judge agrees with Claude on {agree}/{len(both)} ({100 * agree / max(1, len(both)):.0f}%); "
          f"chance level ~{100 * sum(1 / len(r['eligible']) for r in rows) / len(rows):.0f}%; "
          f"Claude picked the first eligible draft {first}/{len(both)}")
    for t in both:
        if local[t] != claude[t]:
            print(f"  {t}: local {local[t]}, claude {claude[t]}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
