"""
Offline Opus fact checker (goal (a), 2026-11-17): every factual claim in Picky's answers vs the FACT SHEET
(eval/run.py truth_bundle: standings, weekly records, every graded final with cover margins and picks, this
week's pool games + revealed picks, Picky's entry, lore) + the run's own verified extras (game cards, news
items the prompt carried are NOT trusted: only the fact sheet and card facts are).

  ./.venv/bin/python3 -I eval/fact_judge.py results/<tag>.json            # an exam run (fact / text exam)
  ./.venv/bin/python3 -I eval/fact_judge.py --real <private real.json>      # historical replies (as of their time)

Writes <input>.facts.json: {id: {"claims": [{"claim", "verdict": TRUE|FALSE|UNVERIFIABLE, "why"}], "wrong": n}}
and prints the FALSE claims. FALSE = contradicts the sheet. UNVERIFIABLE = the sheet can't settle it (news,
CFB trivia, team stats from the cards): those are listed for a hand check, never auto-counted as wrong.
Picky never calls Anthropic; only this offline grader does (picky_claude.run, Opus, no tools).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import picky_claude  # noqa: E402
from truth import Truth  # noqa: E402

PROMPT = """You are a strict fact checker for a college-football pick'em league chatbot ("Picky").
Check ONLY factual claims (records, places, points gaps, picks, covers, cover margins, final scores, spreads,
dates/weeks, Picky's own record). Ignore jokes, opinions, insults and style.

Rules:
- A member's W-L with no period named may be ANY record that's true for them in the sheet (season, a week,
  the sum of recent weeks, lore lines). "this week" / "last week" / "week N" must match that period. {when}
- Spreads: the "pool line" is the league's; a team "covered" per the graded finals. "Missed by N" / "covered by
  N" is the cover margin, not the game margin.
- A claim the sheet cannot settle (news, injuries, CFB history, team stats, rankings) is UNVERIFIABLE, not FALSE.
- Picky is the bot; "I"/"my" in the answer is Picky. "you" is the person who asked ({asker}).

FACT SHEET:
{sheet}

EXCHANGES (id | asker | question | answer):
{items}

Return ONLY JSON: {{"<id>": {{"claims": [{{"claim": "...", "verdict": "TRUE|FALSE|UNVERIFIABLE", "why": "..."}}]}}, ...}}
List every factual claim per answer (an answer with none gets an empty list)."""


def _ask(prompt: str) -> dict:
    p = picky_claude.run(prompt, timeout=900, caller="fact_judge", output_format="text")
    out = p.stdout.strip()
    m = re.search(r"\{.*\}", out, re.S)
    return json.loads(m.group(0)) if m else {}


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--real", action="store_true", help="input is a private real-exchange list (as of its time)")
    ap.add_argument("--batch", type=int, default=12)
    a = ap.parse_args()
    import run as runmod  # eval/run.py
    t = await Truth().load()
    sheet = runmod.truth_bundle(t)
    rows = []
    src = json.loads(Path(a.path).read_text())
    if a.real:
        for i, x in enumerate(src):
            q = " / ".join(p["text"] for p in x.get("prev") or []) or "(scheduled group post, no question)"
            rows.append((f"r{i}", "Chuck P" if x["kind"] == "dm" else "a league member", q, x["reply"], x["t"]))
    else:
        for r in src["results"]:
            if r.get("skip"):
                continue
            for k, tn in enumerate(r.get("turns") or []):
                rows.append((f"{r['id']}#t{k + 1}", r.get("persona_team"), tn["q"], tn["a"], ""))
    out = {}
    for b in range(0, len(rows), a.batch):
        chunk = rows[b:b + a.batch]
        when = ("These replies were sent at the times shown (UTC). Before Nov 17 the pool's current week was 11 "
                "(Nov 14 games, all final), so 'this week' then meant week 11 and the site's 'last week' meant week 10."
                if a.real else "")
        items = "\n".join(f"{i} | {who} | {q} | {ans}" + (f" | sent {ts}" if ts else "") for i, who, q, ans, ts in chunk)
        res = _ask(PROMPT.format(when=when, asker="named per exchange", sheet=sheet, items=items))
        for i, *_ in chunk:
            v = res.get(i) or {"claims": [], "error": "no verdict"}
            v["wrong"] = sum(1 for c in v.get("claims") or [] if c.get("verdict") == "FALSE")
            out[i] = v
        print(f"judged {min(b + a.batch, len(rows))}/{len(rows)}", flush=True)
    dest = Path(a.path).with_suffix(".facts.json")
    dest.write_text(json.dumps(out, indent=1))
    wrong = [(i, c) for i, v in out.items() for c in v.get("claims") or [] if c.get("verdict") == "FALSE"]
    unv = sum(1 for v in out.values() for c in v.get("claims") or [] if c.get("verdict") == "UNVERIFIABLE")
    print(f"FALSE claims: {len(wrong)} in {len({i for i, _ in wrong})} answers; UNVERIFIABLE (hand check): {unv}")
    for i, c in wrong:
        print(f"  {i}: {c.get('claim')} -- {c.get('why')}")
    print(f"wrote {dest}")


if __name__ == "__main__":
    asyncio.run(main())
