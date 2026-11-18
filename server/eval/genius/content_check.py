"""Deterministic answer-content gate for the genius asks (integration 2026-11-18): the inner loop's second check,
after the lane table. Rules are per QUESTION TYPE (the lane), checked against the fact store, never per item id
or answer key:

  line_move    names the fresh QB on the side the line moved against when the store has one; never 'hasn't kicked off'
  h2h          a year and a final score, never 'I don't have'
  ats_profile  an ATS record (W-L) and 'against the number' / 'ATS'
  unit_matchup every team it names is one of the asked game's two teams (no wrong-game answers)
  pick         a named game's call names one of that game's teams, not the whole pool slate
  every code lane: no empty citation '(, Monday)', no pre-game 'hasn't kicked off' on a football ask

  content_check.py <replay.json>     replay rows: [{"id","cat","turns":[{"q","lane","a"}]}]; exit 1 on a FAIL
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent))

import factstore  # noqa: E402

FRESH_D = 10


def _named_teams(text: str) -> set[str]:
    return {factstore.norm(t) for t in factstore.teams_named(text or "")}


def check_turn(q: str, lane: str, a: str) -> list[str]:
    errs = []
    lane = lane or ""
    if not lane.startswith("fact:"):
        return errs
    kind = lane.split(":", 1)[1]
    if re.search(r"\(\s*,", a):
        errs.append("empty citation")
    if "hasn't kicked off" in a and not kind.startswith("gd_"):
        errs.append("pre-game 'hasn't kicked off' on a football lane")
    if kind == "line_move":
        g, _ = factstore.find_game(q, {})
        if g:
            mk = factstore.market(g)
            now, opn = mk.get("spread"), mk.get("spreadOpen")
            if now is not None and opn is not None and now != opn:
                hit = g["awayTeam"] if now < opn else g["homeTeam"]
                qbs = [r for r in factstore.player_status(game=g) if r.get("pos") == "QB"
                       and factstore.norm(r.get("team") or "") == factstore.norm(hit)
                       and r["status"] in factstore.CAUSE_STATUSES and r["age_days"] <= FRESH_D]
                if qbs and not any(r["player"].split()[-1].lower() in a.lower() for r in qbs):
                    errs.append(f"line move skips the QB news on {hit}: {qbs[0]['player']}")
    elif kind == "h2h":
        if re.search(r"\bdon'?t have\b", a, re.I) or not (re.search(r"\b20\d\d\b", a) and re.search(r"\b\d{1,2}-\d{1,2}\b", a)):
            errs.append("h2h without the year and score")
    elif kind == "ats_profile":
        if not (re.search(r"\b\d{1,2}-\d{1,2}\b", a) and re.search(r"against the (number|spread)|\bATS\b", a, re.I)):
            errs.append("ATS profile without a record")
    elif kind == "unit_matchup":
        asked, said = _named_teams(q), _named_teams(a)
        g, _ = factstore.find_game(q, {})
        pair = {factstore.norm(g["awayTeam"]), factstore.norm(g["homeTeam"])} if g else asked
        if said - pair:
            errs.append(f"unit answer names teams outside the game: {sorted(said - pair)}")
    elif kind == "pick":
        asked = _named_teams(q)
        if asked and not (_named_teams(a) & asked):
            errs.append("pick answer doesn't name the asked game's teams")
    return errs


def main(path: str) -> int:
    rows = json.loads(Path(path).read_text())
    bad = 0
    for r in rows:
        for i, t in enumerate(r["turns"]):
            if i:  # follow-ups ('why?') carry no game of their own
                continue
            errs = check_turn(t["q"], t.get("lane"), t.get("a") or "")
            if errs:
                bad += 1
                print(f"FAIL {r['id']:24s} [{t.get('lane')}] {'; '.join(errs)}")
    print(f"content check: {sum(len(r['turns'][:1]) for r in rows)} first turns, {bad} FAIL")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
