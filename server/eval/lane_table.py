#!/usr/bin/env python3
"""Router lane regression table (review R7).

Every real ask in picky_exchanges.jsonl plus every exam item (genius exam: all 75 asks; the fact/group exams),
with the lane(s) it should take, checked
against router.classify (code only: no model, no league data, no network).

  lane_table.py build      # (re)build the PRIVATE table from the exchange log + exam items; keeps hand labels
  lane_table.py check      # synthetic rows (repo) + private rows (if present); exit 1 on a FAIL
  lane_table.py check --synthetic-only   # what GitHub CI runs (no member text there)
  lane_table.py accept     # a lane change was on purpose: re-pin today's lane on pinned (non-hand) FAIL rows

Files:
  server/eval/lane_table_synthetic.json   public: hand-written asks only, never member text
  <private>/eval/lane_table.json          real member asks + exam items (0600)

Row: {"q", "ok": [labels or lanes], "not": [labels or lanes]?, "src": "real|genius|exam|synthetic|flub",
"xfail": "<flub id>"?, "why"?}. A row passes when classify(q).label() or its lane is in "ok" (an empty "ok" =
any lane) and in no "not" entry (a "not" label is exact; a bare lane name covers every kind of it).
Flub rows come from real_replay's permanent flub fixtures (fixtures-flubs.json: lanes / not_lanes), refreshed
on every build: xfail while the flub is open, a hard row once it's fixed (so a fixed flub that breaks again
fails this table, test_lane_table.py and release.sh's model-free suite). A row with "xfail" is expected to fail
(a queued flub the nightly fix round owns): XFAIL while it still fails, XPASS once fixed (not an error).
build() pins today's lane for new rows (a regression pin); hand-fixed rows keep their labels.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SERVER = HERE.parent
sys.path.insert(0, str(SERVER))
sys.path.insert(0, str(HERE))

PRIVATE = Path(os.environ.get("PICKY_PRIVATE_DIR", "/Users/Agent/cfb-pickem-private"))
SYNTHETIC = HERE / "lane_table_synthetic.json"
PRIVATE_TABLE = Path(os.environ.get("PICKY_LANE_TABLE", PRIVATE / "eval" / "lane_table.json"))
EXCHANGES = Path(os.environ.get("PICKY_EXCHANGE_LOG", PRIVATE / "picky_exchanges.jsonl"))
REPLAY_DIR = PRIVATE / "eval" / "replay"
TRIGGER = re.compile(r"(?<![\w@])@\s?picky(?![\w@])|@?pickempicky(?:@gmail\.com)?", re.I)


def norm(q: str) -> str:
    return re.sub(r"\s+", " ", TRIGGER.sub("", q or "").replace("’", "'")).strip(" -,:").lower()


def classify(q: str):
    import router
    return router.classify(TRIGGER.sub("", q).strip())


def pre_board() -> dict:
    """The worst case for lane steals: every game on the live board kicking off in 2 hours (game day is active,
    nothing has started). Genius asks are pre-game DMs, so they must keep their lanes on it."""
    import time
    import gameday
    soon = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 7200))
    return {k: {**g, "state": "pre", "start": soon} for k, g in (gameday.games() or {}).items()}


def _row_ok(r: dict, rt) -> bool:
    ok = not r["ok"] or rt.label() in r["ok"] or rt.lane in r["ok"]
    return ok and not any(rt.label() == x or (":" not in x and rt.lane == x) for x in r.get("not") or [])


def check_rows(rows: list[dict]) -> list[dict]:
    """QB_STATUS_ASK takes the qb_status lane only when this week's sourced notes (factstore intel) cover the ask
    (1bb392a), so classify reads data that changes every week: each row is classified with a note and without
    one and passes if either routes it right. The table pins the rules, never the week's intel."""
    import router
    from unittest.mock import patch
    out, board = [], None
    for r in rows:
        rts = []
        for note in ("note", None):
            with patch.object(router, "_qb_status_intel", lambda ctx, q, _n=note: _n):
                rt = classify(r["q"])
            if r.get("src") == "genius":  # a DM: group_chat's game-day precedence rule applies (gameday.override)
                import gameday
                board = pre_board() if board is None else board
                rt = gameday.override(rt, TRIGGER.sub("", r["q"]).strip(), "", board)
            rts.append(rt)
        good = [rt for rt in rts if _row_ok(r, rt)]
        ok = bool(good)
        rt = good[0] if good else rts[-1]
        st = ("XPASS" if ok else "XFAIL") if r.get("xfail") else ("PASS" if ok else "FAIL")
        out.append({**r, "got": rt.label(), "why_rule": rt.why, "status": st})
    return out


def load(path: Path) -> list[dict]:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return []


def write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)


def real_asks() -> list[str]:
    out = []
    for line in EXCHANGES.read_text().splitlines() if EXCHANGES.exists() else []:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("side") or not (r.get("q") or "").strip():
            continue
        out.append(r["q"])
    return out


def exam_asks() -> list[str]:
    import fact_items
    import group_items
    import questions
    from truth import Truth

    async def go():
        t = await Truth().load()
        return [i for mod in (questions, group_items, fact_items) for i in mod.build(t)]
    qs = []
    for it in asyncio.run(go()):
        turns = it.get("turns") or []
        if turns:
            qs.append(it.get("raw") or turns[-1])
    return qs


def genius_asks() -> list[str]:
    """Every genius exam item's first text: the week's questions (genius/run.py's questions_file rule: the repo
    file unless the weekly refresh wrote a newer week privately) plus the private game-theory items. 75 today."""
    repo = HERE / "genius" / "questions.json"
    qf, week = repo, (load(repo) or {}).get("week") or 0
    for p in (PRIVATE / "genius").glob("questions-w*.json"):
        w = p.stem.split("-w")[-1]
        if w.isdigit() and int(w) > week:
            qf, week = p, int(w)
    items = (load(qf) or {}).get("items") or []
    items += (load(PRIVATE / "genius" / "items-private.json") or {}).get("items") or []
    return [i["turns"][0] for i in items if i.get("turns")]


def hand_labels() -> dict[str, dict]:
    """real_replay's hand-labeled fixtures (lanes + expected_fail) win over a pinned snapshot."""
    out = {}
    for p in sorted(REPLAY_DIR.glob("fixtures-*.json")):
        for f in load(p):
            exp = f.get("expect") or {}
            if f.get("src") == "flub":  # a permanent flub fixture: single-turn lane keys only
                if (exp.get("lanes") or exp.get("not_lanes")) and len(f.get("turns") or [f["q"]]) == 1 and \
                        (exp.get("expected_fail") or exp.get("lane_det")):
                    out[norm(f["q"])] = {"q": f["q"], "ok": exp.get("lanes") or [], "not": exp.get("not_lanes") or [],
                                         "flub": f["id"], **({"xfail": exp["expected_fail"]} if exp.get("expected_fail")
                                                             else {}), "why": exp.get("why") or "flub fixture"}
                continue
            if exp.get("lanes"):
                out[norm(f["q"])] = {"ok": exp["lanes"], **({"xfail": exp["expected_fail"]} if exp.get("expected_fail")
                                                            else {}), "why": exp.get("why") or "replay fixture"}
    return out


def build() -> dict:
    old = {norm(r["q"]): r for r in load(PRIVATE_TABLE)}
    hand = hand_labels()
    rows, seen = [], set()
    for src, qs in (("real", real_asks()), ("genius", genius_asks()), ("exam", exam_asks())):
        for q in qs:
            k = norm(q)
            if not k or k in seen:
                continue
            seen.add(k)
            if k in hand and hand[k].get("flub"):  # flub fixtures are the source of truth: always refreshed
                rows.append({"src": src, "hand": True, **hand[k], "q": q})
            elif k in old and old[k].get("hand") and not old[k].get("flub"):
                rows.append(old[k])
            elif k in hand:
                rows.append({"q": q, "src": src, "hand": True, **hand[k]})
            elif k in old and not old[k].get("flub"):
                rows.append({**old[k], "src": src})
            else:
                rows.append({"q": q, "src": src, "ok": [classify(q).label()], "why": "pinned (build)"})
    for k, h in hand.items():  # a flub fixture's ask the exchange log no longer has (or reworded): still a row
        if h.get("flub") and k not in seen:
            seen.add(k)
            rows.append({"src": "flub", "hand": True, **h})
    write_private(PRIVATE_TABLE, json.dumps(rows, indent=1) + "\n")
    return {"rows": len(rows), "flub": sum(bool(r.get("flub")) for r in rows), "real": sum(r["src"] == "real" for r in rows),
            "genius": sum(r["src"] == "genius" for r in rows), "exam": sum(r["src"] == "exam" for r in rows),
            "hand": sum(bool(r.get("hand")) for r in rows)}


def check(synthetic_only: bool = False, quiet: bool = False) -> int:
    tables = [("synthetic", load(SYNTHETIC))] + ([] if synthetic_only else [("private", load(PRIVATE_TABLE))])
    bad = 0
    for name, rows in tables:
        res = check_rows(rows)
        counts = {s: sum(r["status"] == s for r in res) for s in ("PASS", "FAIL", "XFAIL", "XPASS")}
        print(f"{name}: {len(res)} rows " + " ".join(f"{k}={v}" for k, v in counts.items()))
        for r in res:
            if r["status"] == "FAIL" or (not quiet and r["status"] in ("XFAIL", "XPASS")):
                q = r["q"] if name == "synthetic" else f"<{name} row, {len(r['q'])} chars>"
                print(f"  {r['status']:5} got {r['got']} ({r['why_rule']}) want {r['ok']}"
                      f"{' [' + r['xfail'] + ']' if r.get('xfail') else ''}"
                      f"{' [flub ' + r['flub'] + ']' if r.get('flub') and r['status'] == 'FAIL' else ''}: {q}")
        bad += counts["FAIL"]
    return 1 if bad else 0


def accept() -> dict:
    rows = load(PRIVATE_TABLE)
    n = 0
    for r, res in zip(rows, check_rows(rows)):
        if res["status"] == "FAIL" and not r.get("hand"):
            r["ok"], r["why"], n = [res["got"]], "re-pinned (accept)", n + 1
    write_private(PRIVATE_TABLE, json.dumps(rows, indent=1) + "\n")
    return {"repinned": n}


def main(argv: list[str]) -> int:
    cmd = argv[0] if argv else "check"
    if cmd == "build":
        print(json.dumps(build()))
        return 0
    if cmd == "accept":
        print(json.dumps(accept()))
        return 0
    return check(synthetic_only="--synthetic-only" in argv, quiet="--quiet" in argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
