"""
Weekly refresh of the genius exam from the new slate (offline, Opus 5.5).

1. Picks the week's games: the league's pool games first (all_games.picked),
   then the most notable FBS games by combined SP+ plus the biggest line moves,
   up to --games (default 16).
2. Builds the fact pack (facts.py: CFBD cache, ESPN odds, forecast) into
   cfb-pickem-private/genius/facts-w<week>.json.
3. Asks Opus (claude -p via dossier.run_claude, web search allowed for news
   with sources) to write ~50 questions + answer keys in questions.json's
   schema, from the fact pack. Every number must come from the pack; news
   claims need a source URL.
4. Validates (ids, games in the pack, det self-test) and writes
   cfb-pickem-private/genius/questions-w<week>.json. run.py uses the newest
   private week file when it's newer than the repo's questions.json, so the
   weekly refresh never commits to the public repo.

    cd server && ./.venv/bin/python3 -I eval/genius/refresh.py [--week N] [--games 16] [--facts-only]

Anthropic down (PICKY_NO_ANTHROPIC=1 or claude fails): the fact pack still
refreshes and the last good questions file stays in place.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1]))

import facts as F  # noqa: E402
import run as G  # noqa: E402

QSCHEMA_NOTE = (HERE / "questions.json").read_text()[:6000]


def current_week(db) -> int:
    import datetime as dt
    games = F._ds(db, "games")
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    upcoming = sorted((g["startDate"], g["week"]) for g in games
                      if g.get("seasonType") == "regular" and not g.get("completed") and g["startDate"] > now)
    return upcoming[0][1] if upcoming else max(g["week"] for g in games)


def pick_games(week: int, n: int) -> list[int]:
    db = sqlite3.connect(F.DB)
    games = [g for g in F._ds(db, "games") if g.get("week") == week and g.get("homeClassification") == "fbs"
             and g.get("awayClassification") == "fbs"]
    sp = {r["team"]: r.get("rating") or -30 for r in F._ds(db, "sp")}
    lines = {g["id"]: F._line(g.get("lines")) for g in F._ds(db, "lines") if g.get("week") == week}
    pool = []
    try:
        mk = G.market_now({g["id"] for g in games})
        pool = [int(k) for k, v in mk.items() if v.get("pool_game")]
    except Exception:  # noqa: BLE001
        pass

    def score(g):
        ln = lines.get(g["id"]) or {}
        move = abs((ln.get("spread") or 0) - (ln.get("spreadOpen") or ln.get("spread") or 0))
        return sp.get(g["homeTeam"], -30) + sp.get(g["awayTeam"], -30) + 4 * move

    rest = [g["id"] for g in sorted(games, key=score, reverse=True) if g["id"] not in pool]
    return (pool + rest)[:n]


PROMPT = """You write the "football genius" exam for Picky, a pick'em bot in a college-football league graded ONLY against the spread (line locks Tuesday 11:00 AM CT; picks lock at the week's first kickoff). The exam asks the questions a sharp bettor asks before a pick, on THIS week's real games, and grades Picky's answers against your answer keys.

Write {n_items} items in exactly this JSON shape (same fields as the example file below): id (short, unique, prefix by category: lv- lines/value, pe- personnel, mu- matchups, ah- history/ATS, si- situational, bp- big picture, pb- pushback), cat (lines|personnel|matchups|ats|situational|big_picture|pushback), games (ESPN game ids from the fact pack), turns (the member's texts; pushback items have 2-3 turns like "why?", "best case for the other side?", "what would change your mind?"), key {{must_include, must_not_claim, lean, lean_side, facts, sources}}, det {{ats_frame (true for pick questions), numbers (optional extra allowed numbers)}}.

Mix: ~10 lines/value (line moves and their causes, key numbers, totals and implied team totals, locked league line vs market, "where's the value"), ~7 personnel (QB status and the backup's record, key injuries, transfers/opt-outs), ~8 matchups (run game vs run D, pass rush vs OL, explosiveness, red zone/points per opportunity, pace, option teams), ~8 history/ATS (head to head incl. ATS, ATS splits fav/dog/home/road/after a bye, lookahead/letdown/sandwich, revenge, rivalry), ~6 situational (weather/wind as a forecast, travel/time zones, altitude, night games, motivation: bowl eligibility, CFP/conference stakes, interim coach), ~6 big picture (CFP committee rankings and format, conference title scenarios, coaching carousel/hot seats, realignment/NIL only when it matters for a game), 4 pushback multi-turn. Pool games first: every pool game (marked POOL in the pack) gets at least 3 items. Include 2-3 honesty traps (a false premise, or a fact nobody has, where the right answer is to correct it or say "not confirmed").

Rules for keys:
- Every number (spread, total, record, ATS record, stat, rank) must come from the FACT PACK below. Convert CFBD spreads correctly: spread is the HOME team's number (negative = home favored); ESPN "details" names the favorite.
- News (injuries, QB status, coaching, rankings, title scenarios) must come from your web search this run, with the source URLs in key.sources, and the as-of date in the text. If you can't confirm something, make the key say "not confirmed" rather than guessing.
- lean: the data-supported ATS lean, or "none"/"conditional"/"any" when the data doesn't support one. Don't force a side.
- No league member names anywhere (this file is public).

Return JSON only: {{"items": [...]}}.

## EXAMPLE FILE (last week's, for the shape)
{example}

## FACT PACK (as of {as_of})
{pack}
"""

SCHEMA = {"type": "object", "properties": {"items": {"type": "array", "items": {"type": "object"}}}, "required": ["items"]}


def compact_pack(pack: dict) -> str:
    """The fact pack without per-game logs bloat: keeps last 6 games per team."""
    p = json.loads(json.dumps(pack))
    for g in p["games"].values():
        for t in g["teams"].values():
            t["games"] = t["games"][-6:]
    return json.dumps(p, separators=(",", ":"))[:180_000]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--week", type=int, default=0)
    ap.add_argument("--games", type=int, default=16)
    ap.add_argument("--items", type=int, default=50)
    ap.add_argument("--facts-only", action="store_true")
    a = ap.parse_args()
    week = a.week or current_week(sqlite3.connect(F.DB))
    ids = pick_games(week, a.games)
    pack = F.build(ids)
    pack["pool_games"] = [g for g, v in G.market_now(set(ids)).items() if v.get("pool_game")]
    G.PRIVATE.mkdir(parents=True, exist_ok=True)
    (G.PRIVATE / f"facts-w{week}-auto.json").write_text(json.dumps(pack, indent=1, default=str))  # never clobbers a curated pack
    print(f"week {week}: fact pack for {len(ids)} games ({len(pack['pool_games'])} pool)")
    if a.facts_only:
        return 0
    import dossier
    try:
        out = dossier.run_claude(PROMPT.format(n_items=a.items, example=QSCHEMA_NOTE, as_of=pack["as_of"],
                                               pack=compact_pack(pack)), 2400, SCHEMA)
    except Exception as e:  # noqa: BLE001
        print(f"Opus refresh failed ({e!r:.200}); keeping the last good questions file")
        return 1
    items = [i for i in out.get("items") or [] if isinstance(i, dict) and i.get("id") and i.get("turns")
             and isinstance(i.get("key"), dict) and set(i.get("games") or []) <= {int(x) for x in ids}]
    seen, clean = set(), []
    for i in items:
        if i["id"] in seen:
            continue
        seen.add(i["id"])
        i.setdefault("det", {"ats_frame": i.get("cat") == "pushback"})
        i["key"].setdefault("must_not_claim", [])
        clean.append(i)
    if len(clean) < 30:
        print(f"only {len(clean)} usable items; keeping the last good questions file")
        return 1
    doc = {"week": week, "season": pack["games"][str(ids[0])].get("season", 2026) if ids else 2026,
           "as_of": pack["as_of"], "slate_note": f"Auto-refreshed by Opus from the week-{week} fact pack.",
           "sources_common": ["CFBD local cache", "ESPN summary odds", "Open-Meteo forecast", "Opus web search (per-item sources)"],
           "items": clean}
    dest = G.PRIVATE / f"questions-w{week}.json"
    dest.write_text(json.dumps(doc, indent=1))
    print(f"wrote {dest} ({len(clean)} items)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
