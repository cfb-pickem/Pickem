"""The current CFP format as committed data (cfp_rules.json), checked once by hand against the sources it lists
(plan phase 5; RULES 1: written offline once, committed with a validator, never refreshed by a model).

  cfp_rules.py check    # validate the committed file (exit 1 on a problem)

When the CFP changes its format: edit cfp_rules.json from an official source, bump "verified", run check.
"""

from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

PATH = Path(__file__).resolve().parent / "cfp_rules.json"


def load(path: Path | None = None) -> dict:
    return json.loads(Path(path or PATH).read_text())


def _date(s) -> dt.date | None:
    try:
        return dt.date.fromisoformat(str(s))
    except ValueError:
        return None


def validate(d: dict) -> list[str]:
    """Problems with a rules dict ([] = fine)."""
    errs = []
    n = d.get("teams")
    byes, fr = d.get("byes") or [], d.get("first_round") or []
    if not isinstance(n, int) or n < 4:
        errs.append("teams must be a field size")
    seeds = sorted(list(byes) + [s for pair in fr for s in pair])
    if seeds != list(range(1, (n or 0) + 1)):
        errs.append(f"byes + first-round pairs must cover seeds 1-{n} exactly once, got {seeds}")
    if any(len(p) != 2 or p[0] + p[1] != (n or 0) + len(byes) + 1 for p in fr):
        errs.append("first-round pairs must be higher seed vs mirror seed (5 vs 12 ...)")
    qf = d.get("quarterfinals") or []
    if sorted(q[0] for q in qf) != sorted(byes):
        errs.append("each bye seed needs a quarterfinal")
    ab = d.get("auto_bids") or {}
    p4 = ab.get("power4_champions") or []
    g6 = (ab.get("group_of_six") or {})
    nd = ab.get("notre_dame") or {}
    if not p4 or len(p4) + int(g6.get("count") or 0) + (1 if nd else 0) > (n or 0):
        errs.append("auto bids must fit in the field")
    if not g6.get("conferences"):
        errs.append("group_of_six needs its conferences")
    rel = [_date(x) for x in d.get("releases") or []]
    sel = _date(d.get("selection_day"))
    if not rel or None in rel or rel != sorted(rel) or not sel or rel[-1] >= sel:
        errs.append("releases must be ISO dates in order, all before selection_day")
    if not _date(d.get("verified")):
        errs.append("verified date missing")
    src = d.get("sources") or []
    if not src:
        errs.append("at least one source")
    for s in src:
        if not str(s.get("url", "")).startswith("https://") or not _date(s.get("verified")) or not s.get("says"):
            errs.append(f"source needs an https url, a verified date and what it says: {s.get('url')}")
    return errs


def main(argv: list[str]) -> int:
    errs = validate(load())
    print("\n".join(errs) or "cfp_rules.json ok")
    return 1 if errs else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
