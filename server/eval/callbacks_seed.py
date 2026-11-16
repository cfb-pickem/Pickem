"""
Seed a callbacks state file for the callbacks quick exam (cb-* items in
group_items.py). The history is staged (Picky didn't really say these), but
every grade comes from the league's real finals via callbacks.grade_pending,
so the exam checks that what Picky says matches the log + real results.

    PICKY_CALLBACKS_STATE=/private/tmp/picky-callbacks-run/callbacks.json \
      ./.venv/bin/python3 -I eval/callbacks_seed.py /private/tmp/picky-callbacks-run/seed.json

Seeds, for exam personas (no names in this file; they come from Truth):
  shot     a member with a losing (or .500) last graded week: "you suck" the day before it
  lean     a group lean on a final game of that week (the side that covered)
  dm_lean  another member's lean on another game of that week, in a DM (must never surface in the group)
Then writes a summary JSON the exam items read (PICKY_CALLBACKS_EXAM_SEED).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
import callbacks as cb  # noqa: E402
import group_items  # noqa: E402
from truth import Truth  # noqa: E402


def main(out: str) -> int:
    if not os.environ.get("PICKY_CALLBACKS_STATE"):
        sys.exit("set PICKY_CALLBACKS_STATE (never seed the live file)")
    t = asyncio.run(Truth().load())
    P = group_items.personas(t)
    p = cb.state_path()
    if p.exists():
        p.unlink()
    lg = cb.SOURCE(set())
    weeks = sorted({g["week"] for g in lg.games if g.get("week") is not None and not g.get("tiebreaker")})
    done = [w for w in weeks if all(g.get("winner") for g in lg.games if g.get("week") == w and not g.get("tiebreaker"))]
    wk = done[-1]
    wg = [g for g in lg.games if g.get("week") == wk and not g.get("tiebreaker")]
    lg = cb.SOURCE({g["id"] for g in wg})
    first = min(cb.kickoff(g) for g in wg)
    pref = ["ackbar", "tg", "bub", "rettino", "blinky", "mid", "last", "df"]  # never "me" (Dear Leader rules)
    keys = [k for k in pref if P.get(k)]
    shooter = next((k for k in keys if (r := cb._week_record(P[k], wk, lg)) and r[1] >= r[0] and sum(r)), None)
    if not shooter:
        sys.exit(f"no exam persona had a losing week {wk}")
    rest = [k for k in keys if k != shooter]
    leaner, dmer = rest[0], rest[1]
    g1, g2 = wg[0], wg[1]
    cb.log_shot("seed-group", P[shooter], "you suck", ts=first - 86400)
    cb.log_lean("seed-group", P[leaner], g1["id"], g1["winner"], cb.side_line(g1["winner"], g1), ts=first - 86400,
                week=wk, away=g1["away"], home=g1["home"])
    cb.log_lean("any;-;exam-dm-seed", P[dmer], g2["id"], g2["winner"], cb.side_line(g2["winner"], g2),
                ts=first - 86400, is_dm=True, week=wk, away=g2["away"], home=g2["home"])
    n = cb.grade_pending()
    d = cb._load()
    shot = d["shots"][0]["outcome"]
    lean = next(x for x in d["leans"] if x["team"] == P[leaner])
    dml = next(x for x in d["leans"] if x["team"] == P[dmer])
    summary = {"week": wk, "graded": n,
               "shot": {"persona": shooter, "team": P[shooter], "w": shot["w"], "l": shot["l"]},
               "lean": {"persona": leaner, "team": P[leaner], "side": lean["side"], "label": lean["result"]["label"],
                        "margin": lean["result"]["margin"], "away": g1["away"], "home": g1["home"]},
               "dm_lean": {"persona": dmer, "team": P[dmer], "side": dml["side"], "label": dml["result"]["label"],
                           "away": g2["away"], "home": g2["home"]}}
    Path(out).write_text(json.dumps(summary, indent=1))
    os.chmod(out, 0o600)
    print(f"seeded week {wk}, graded {n}: shot persona {shooter} {shot['w']}-{shot['l']}, lean {lean['result']['label']}, "
          f"dm lean {dml['result']['label']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
