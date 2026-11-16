"""
Blind before/after quality judge for the analyst-take / league-material slices.

  ./.venv/bin/python3 -I eval/judge_quality.py eval/results/BEFORE.json eval/results/AFTER.json

Pairs every item both runs answered, shuffles A/B, and asks one `claude -p`
(Opus, no tools, zero MCP, subscription only via picky_claude) to
score each answer:
  breakdown 1-5  game items: sharp, sourced, beyond FPI/the line
  roast     1-5  member/league/Picky items: specific, funny, built on true numbers
  human     1-5  sounds like a person in a group text, not a bot
  helpful   1-5  actually answered the question, correctly, up front
  invented  bool a league or game fact that contradicts the fact sheet
Writes eval/judge-<tag>.json for both runs (render-compatible: pass/note +
the scores) and prints the means. The fact sheet is the league material facts
plus each game's dossier claims and take brief, so accuracy is judged
against the same data Picky had.
"""

from __future__ import annotations

import json
import random
import re
import statistics
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import dossier  # noqa: E402
from truth import chat  # noqa: E402

GAME_CATS = {"games", "injuries"}
SCORES = ("breakdown", "roast", "human", "helpful")

RUBRIC = """# Blind judge: Picky answers

Picky is the trash-talking bot in a friends' college football pick'em league (members pick against a pool
spread). Charlie, who runs the league, wants Picky helpful and reliable first (members should be able to rely
on his answers), then conversational and funny. Each pair shows two versions of Picky answering the same
question, from two builds in random order. Judge each version on its own merits against the FACT SHEET.

Score each version:
- helpful (1-5): did it actually answer the question, correctly and up front? 1 = dodged or buried it,
  3 = answered but vague, 5 = the clear correct answer first, then the fun.
- human (1-5): sounds like a sharp buddy in a group text (casual, contractions, reacts to what was asked,
  one or two angles) vs a bot (lists, labels like "Reason 1" or "Confidence", report phrasing, stock openers
  like "Great question", reading out data).
- breakdown (1-5, ONLY for pairs marked GAME; else null): a clear pick backed by specific, sourced reasons
  that go beyond FPI and the raw line (unit matchups, price gap vs the market, line movement, reported
  availability), honest about the best counter. 1 = vague or just restates the line.
- roast (1-5, ONLY for pairs marked ROAST; else null): specific and funny, built on true numbers about that
  member (or Picky himself); 1 = generic, unfunny or wrong.
- invented (true/false): states a league fact (record, pick, result, standing, streak) or a game fact (stat,
  injury, player) that contradicts the fact sheet or clearly isn't in it. Jokes and opinions are fine.
  The Blinkstradamus milk bit, the Ackbar chalk-nemesis bit and the "Dear Leader" bit for Chuck P are approved
  running bits.

Output ONLY one JSON object: {"pairs": [{"tag": "P01", "A": {"helpful": 4, "human": 3, "breakdown": null,
"roast": 4, "invented": false, "note": "..."}, "B": {...}}, ...]}
Notes under 15 words. Include every pair.
"""

_V = {"type": "object", "properties": {
    "helpful": {"type": "integer"}, "human": {"type": "integer"}, "breakdown": {"type": ["integer", "null"]},
    "roast": {"type": ["integer", "null"]}, "invented": {"type": "boolean"}, "note": {"type": "string"}},
    "required": ["helpful", "human", "breakdown", "roast", "invented", "note"]}
SCHEMA = {"type": "object", "properties": {"pairs": {"type": "array", "items": {
    "type": "object", "properties": {"tag": {"type": "string"}, "A": _V, "B": _V}, "required": ["tag", "A", "B"]}}},
    "required": ["pairs"]}


def fact_sheet(runs: list[dict]) -> str:
    out = []
    try:  # finals, cover margins, lore and material facts (eval/run.py truth_bundle)
        import asyncio
        import run as exam
        from truth import Truth
        out.append(exam.truth_bundle(asyncio.run(Truth().load())))
    except Exception as e:  # noqa: BLE001 - no Supabase: fall back to the material facts alone
        print(f"truth bundle unavailable ({e!r}); material facts only", file=sys.stderr)
        mat = chat.load_material() or {}
        if mat.get("facts"):
            out.append("League (through week %s):" % mat.get("week"))
            out += [f"- {v}" for v in mat["facts"].values()]
    week = runs[-1].get("week")
    for p in sorted((chat.DOSSIER_DIR / str(week)).glob("*.json")) if week else []:
        if p.name.startswith("_"):
            continue
        d = json.loads(p.read_text())
        out.append(f"\nGame: {d.get('away')} at {d.get('home')} (data Picky had before kickoff):")
        out += [f"- {v}" for v in ((d.get("analyst_take") or {}).get("brief") or {}).values()]
        out += [f"- {c.get('text')}" for k in ("facts", "opinions", "rumors") for c in d.get(k) or []]
    return "\n".join(out)


def convo(r: dict) -> str:
    return "\n".join(f"Q: {t.get('q')}\nA: {t.get('a') or ''}" for t in r.get("turns") or [])


def main(argv: list[str]) -> int:
    runs = [json.loads(Path(a).read_text()) for a in [x for x in argv if not x.startswith("--")][:2]]
    by = [{r["id"]: r for r in run["results"] if not r.get("skip") and r.get("turns")} for run in runs]
    ids = [i for i in by[0] if i in by[1]]
    rng = random.Random()
    key, blocks = {}, []
    for n, iid in enumerate(ids, 1):
        tag = f"P{n:02d}"
        flip = rng.random() < 0.5
        a, b = (1, 0) if flip else (0, 1)
        r0 = by[0][iid]
        kind = "GAME" if r0["cat"] in GAME_CATS else "ROAST" if re.search(
            r"roast|storyl|picky-season|persona|df-|leader|blinks|ackbar", iid) else "OTHER"
        key[tag] = {"id": iid, "A": a, "B": b}
        blocks.append(f"### {tag} [{kind}] (asked by league member: {r0.get('persona_team')})\n"
                      f"--- Version A ---\n{convo(by[a][iid])}\n--- Version B ---\n{convo(by[b][iid])}\n")
    replay = "--replay" in argv
    note = ("\nNOTE: these answers come from a replay of a past week, so the league standings and records Picky "
            "quotes are from BEFORE that week. Don't judge league standings or records as invented; judge games, "
            "picks and stats only.\n") if replay else ""
    prompt = RUBRIC + note + "\n## FACT SHEET\n" + fact_sheet(runs) + "\n\n## PAIRS\n\n" + "\n".join(blocks)
    raw = {x.get("tag"): x for x in dossier.run_claude(prompt, 900, SCHEMA, "", caller="judge_quality").get("pairs") or []}
    out = [{}, {}]
    for tag, k in key.items():
        j = raw.get(tag) or {}
        for ab in ("A", "B"):
            v = dict(j.get(ab) or {})
            v["pass"] = not v.get("invented") and (v.get("helpful") or 0) >= 3 and (v.get("human") or 0) >= 3
            out[k[ab]][k["id"]] = v
    for run, o in zip(runs, out):
        Path(HERE / f"judge-{run['tag']}.json").write_text(json.dumps(o, indent=1))
    print(f"{'':10}" + "".join(f"{s:>11}" for s in SCORES) + f"{'invented':>10}")
    for run, o in zip(runs, out):
        row = f"{run['tag']:10}"
        for s in SCORES:
            v = [x[s] for x in o.values() if isinstance(x.get(s), (int, float))]
            row += f"{(statistics.mean(v) if v else float('nan')):>8.2f} n{len(v):<2}"
        row += f"{sum(bool(x.get('invented')) for x in o.values()):>10}"
        print(row)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
