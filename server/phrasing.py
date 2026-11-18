"""Phrasing bank for the code lanes (ontology plan Phase 1, 2026-11-18).

A code lane computes its facts; this module only picks the wording. Each lane has server/phrasing/<lane>.json:
{"slots": {slot: [6-10 variants]}, "fixtures": [values that fill every slot]}. Every variant of a slot carries
the same placeholders, so the facts never change with the wording. Variants were written once, offline, and
are checked by validate() (also `python3 phrasing.py`): 6-10 per slot, same placeholders, every fixture
renders, <= 700 chars, no banned tic, no em dash, nothing router.debot would cut.

The model never sees the bank (not a few-shot, not in the prompt). Variant = stable hash of (lane, slot, seed);
seed_for() = asker + CT date + the text, so a chat doesn't read the same shape twice but a repeat ask is stable.

PICKY_PHRASING=0 turns it off: variant 0 everywhere (the pre-bank wording) and no new verdict/support lines.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
from functools import lru_cache
from pathlib import Path

BANK_DIR = Path(__file__).resolve().parent / "phrasing"
LIMIT = 600  # the code answer; the voice line after it still fits a 700-char text


def on() -> bool:
    return os.environ.get("PICKY_PHRASING", "1") != "0"


@lru_cache(maxsize=None)
def _bank(lane: str) -> dict:
    return json.loads((BANK_DIR / f"{lane}.json").read_text())


def seed_for(ctx: dict | None, q: str) -> str:
    who = ((ctx or {}).get("you") or {}).get("team_name") or ""
    day = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=6)).date().isoformat()
    return f"{who}|{day}|{re.sub(r'\s+', ' ', (q or '').split(' ||| ')[0].strip().lower())[:120]}"


def say(lane: str, slot: str, seed: str, **vals) -> str:
    """One variant of lane/slot filled with vals (variant 0 when the flag is off)."""
    vs = _bank(lane)["slots"][slot]
    if not on():
        i = 0
    else:
        s = os.environ.get("PICKY_PHRASING_SEED") or seed  # tests only: force a different draw
        i = int(hashlib.md5(f"{lane}|{slot}|{s}".encode()).hexdigest(), 16) % len(vs)
    return vs[i].format_map(vals)


def join(parts: list[tuple[int, str]], limit: int = LIMIT) -> str:
    """Sentences in their given order; while over the limit, the highest-numbered (least needed) part goes."""
    keep = [p for p in parts if p[1]]
    while len(" ".join(t for _, t in keep)) > limit and len(keep) > 1:
        worst = max(range(len(keep)), key=lambda i: (keep[i][0], i))
        if keep[worst][0] == 0:
            break
        keep.pop(worst)
    return " ".join(t for _, t in keep)


_FIELD = re.compile(r"{(\w+)}")


def validate() -> list[str]:
    """Every problem in the bank, as text (empty = clean)."""
    import no_repeat
    import router
    errs = []
    for f in sorted(BANK_DIR.glob("*.json")):
        lane = f.stem
        try:
            b = json.loads(f.read_text())
        except ValueError as e:
            errs.append(f"{lane}: bad json {e}")
            continue
        if not b.get("fixtures"):
            errs.append(f"{lane}: no fixtures")
        for slot, vs in (b.get("slots") or {}).items():
            if not 6 <= len(vs) <= 10:
                errs.append(f"{lane}.{slot}: {len(vs)} variants (want 6-10)")
            if len(set(vs)) != len(vs):
                errs.append(f"{lane}.{slot}: duplicate variant")
            want = set(_FIELD.findall(vs[0])) if vs else set()
            for v in vs:
                if set(_FIELD.findall(v)) != want:
                    errs.append(f"{lane}.{slot}: placeholders differ: {v!r}")
                for fx in b.get("fixtures") or []:
                    try:
                        out = v.format_map(fx)
                    except (KeyError, IndexError, ValueError) as e:
                        errs.append(f"{lane}.{slot}: fixture can't fill {e}: {v!r}")
                        continue
                    if len(out) > 700:
                        errs.append(f"{lane}.{slot}: {len(out)} chars")
                    if no_repeat.banned_tic(out):
                        errs.append(f"{lane}.{slot}: tic {no_repeat.banned_tic(out)!r}")
                    if re.search("[—–]| - ", out):
                        errs.append(f"{lane}.{slot}: dash: {out!r}")
                    if router.debot(out, "a question about the game", "fact").lower() != out.lower():
                        errs.append(f"{lane}.{slot}: debot changes it: {out!r}")
    return errs


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    problems = validate()
    print("\n".join(problems) or "phrasing bank: ok")
    sys.exit(1 if problems else 0)
