"""
Football-genius exam: the questions a sharp asks before making a pick, on
this week's real slate, asked by DM through a side group sidecar, graded by
deterministic checks (checks.py) and an offline Opus judge (judge.py).

    cd server
    # build/validate only (no model): facts load, items parse, checks self-test
    ./.venv/bin/python3 -I eval/genius/run.py --no-model
    # a run (inside the exam lock, side sidecar from a worktree on :8791)
    lockf -t 5400 /tmp/picky-exam.lock ./.venv/bin/python3 -I eval/genius/run.py \
        --side http://127.0.0.1:8791/group --hist /private/tmp/picky-genius-run/hist.json --tag g-after
    ./.venv/bin/python3 -I eval/genius/judge.py eval/genius/results/g-after.json

Public items: eval/genius/questions.json (this week's games, no member data).
Private items (game theory with real standings): cfb-pickem-private/genius/items-private.json.
Fact pack (CFBD cache + ESPN odds + forecast, as of the key): cfb-pickem-private/genius/facts-w<week>.json.
Results/reports go to eval/genius/results|reports (gitignored: answers name members).
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import checks  # noqa: E402

PRIVATE = Path(os.environ.get("PICKY_GENIUS_PRIVATE", "/Users/Agent/cfb-pickem-private/genius"))
MEMBERS_FILE = Path(os.environ.get("PICKY_GROUP_MEMBERS", "/Users/Agent/cfb-pickem-private/picky_group_members.json"))
GROUP_SECRET_SERVICE = "CFB_PICKEM__PICKY_GROUP_SECRET"
RESULTS, REPORTS = HERE / "results", HERE / "reports"
DEFAULT_PERSONA = "Wedge Mulshine"  # mid-table member: no Dear Leader / last-place riders


def load_items(include_private: bool = True) -> tuple[dict, list[dict]]:
    q = json.loads((HERE / "questions.json").read_text())
    items = [dict(i, private=False) for i in q["items"]]
    pf = PRIVATE / "items-private.json"
    if include_private and pf.exists():
        items += [dict(i, private=True) for i in json.loads(pf.read_text())["items"]]
    return q, items


def load_facts(week: int) -> dict:
    f = PRIVATE / f"facts-w{week}.json"
    return json.loads(f.read_text()) if f.exists() else {"games": {}}


def market_now(game_ids: set[int]) -> dict:
    """Re-pull ESPN odds for the exam's games (the key's lines are from when it
    was written; Picky may quote a newer number) plus the league's line."""
    import facts as F
    out = {}
    for gid in sorted(game_ids):
        try:
            s = httpx.get(F.ESPN_SUMMARY.format(gid), timeout=20).json()
            out[str(gid)] = {"espn": F.espn_odds(s)}
        except Exception as e:  # noqa: BLE001
            out[str(gid)] = {"error": repr(e)[:100]}
    try:  # the league's own line for these games, when the commissioner has posted them
        key = subprocess.run(["security", "find-generic-password", "-a", "drake", "-s",
                              "CFB_PICKEM__SUPABASE_SERVICE_KEY", "-w"], capture_output=True, text=True).stdout.strip()
        ids = ",".join(str(g) for g in game_ids)
        r = httpx.get("https://vopdioszofwdkwnujtiq.supabase.co/rest/v1/all_games",
                      params={"select": "GameId,line,picked", "GameId": f"in.({ids})"},
                      headers={"apikey": key, "Authorization": f"Bearer {key}"}, timeout=20).json()
        for row in r if isinstance(r, list) else []:
            out.setdefault(str(row["GameId"]), {})["league_line"] = row.get("line")
            out[str(row["GameId"])]["pool_game"] = bool(row.get("picked"))
    except Exception as e:  # noqa: BLE001
        print("league lines unavailable:", repr(e)[:100])
    return out


def handles() -> dict[str, str]:
    d = json.loads(MEMBERS_FILE.read_text())
    return {m["team"]: (m.get("handles") or [""])[0] for m in d.get("members") or [] if m.get("team")}


def secret() -> str:
    return os.environ.get("PICKY_GROUP_SECRET") or subprocess.run(
        ["security", "find-generic-password", "-a", "drake", "-s", GROUP_SECRET_SERVICE, "-w"],
        capture_output=True, text=True).stdout.strip()


async def ask(client, url, sec, handle, text, chat_id) -> tuple[str, float]:
    t0 = time.time()
    for _ in range(3):
        r = await client.post(url, headers={"X-Picky-Group-Secret": sec},
                              json={"handle": handle, "text": text, "chat": chat_id, "dm": True})
        if r.status_code < 500:
            break
        await asyncio.sleep(15)
    if r.status_code != 200:
        return f"[HTTP {r.status_code}] {r.text[:200]}", time.time() - t0
    return (r.json().get("reply") or ""), time.time() - t0


async def run(args) -> int:
    q, items = load_items(not args.public_only)
    if args.only:
        keys = set(args.only.split(","))
        items = [i for i in items if i["id"] in keys or i["cat"] in keys]
    facts = load_facts(q["week"])
    market = market_now({g for i in items for g in i.get("games") or []})
    H, sec = handles(), secret()
    tag = args.tag or dt.datetime.now().strftime("g-%Y%m%d-%H%M")
    hist = Path(args.hist) if args.hist else None
    results = []
    async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10, read=400, write=10, pool=10)) as client:
        try:
            health = (await client.get(args.side.replace("/group", "/health"))).json()
        except Exception as e:  # noqa: BLE001
            print("side sidecar not reachable:", e)
            return 2
        for n, it in enumerate(items, 1):
            persona = it.get("persona_team") or DEFAULT_PERSONA
            if hist:  # DM history is per handle: start every item clean
                hist.write_text("{}")
            answers, turns = [], []
            for text in it["turns"]:
                a, secs = await ask(client, args.side, sec, H[persona], text, f"genius-{tag}-{it['id']}")
                answers.append(a)
                turns.append({"q": text, "a": a, "secs": round(secs, 1)})
            det = checks.run_checks(it, answers, facts, market)
            ok = all(c[1] for c in det)
            results.append({"id": it["id"], "cat": it["cat"], "private": it["private"], "persona": persona,
                            "games": it.get("games") or [], "turns": turns, "det": det})
            print(f"[{n}/{len(items)}] {it['id']}: det {'ok' if ok else 'FAIL ' + ', '.join(c[0] for c in det if not c[1])}"
                  f" ({sum(t['secs'] for t in turns):.0f}s)", flush=True)
    runrec = {"tag": tag, "started": dt.datetime.now().isoformat(timespec="minutes"), "side": args.side,
              "health": health, "week": q["week"], "key_as_of": q["as_of"], "market": market, "results": results}
    RESULTS.mkdir(exist_ok=True)
    out = RESULTS / f"{tag}.json"
    out.write_text(json.dumps(runrec, indent=1))
    print(f"wrote {out}")
    return 0


def no_model() -> int:
    """Model-free self-test: items parse and reference real fact-pack games,
    and the deterministic checks behave on known strings."""
    q, items = load_items()
    facts = load_facts(q["week"])
    ids = [i["id"] for i in items]
    assert len(ids) == len(set(ids)), "duplicate item ids"
    missing = sorted({g for i in items for g in i.get("games") or []} - {int(k) for k in facts.get("games") or {}})
    for i in items:
        assert i["turns"] and i["key"]["must_include"], i["id"]
        assert not (not i["private"] and i.get("persona_team")), f"public item with a persona: {i['id']}"
    duke = next(i for i in items if i["id"] == "lv-clem-duke-move")
    al = checks.allowed_lines(duke, facts, {})
    assert checks.check_lines("Duke -13.5, opened -8.5, total 43.5", al)[1]
    assert not checks.check_lines("Duke -24.5 easy", al)[1]
    ats = checks.allowed_ats(duke, facts)
    assert checks.check_ats_records("Duke is 8-2 ATS", ats)[1]
    assert not checks.check_ats_records("Duke is 11-0 ATS", ats)[1]
    assert not checks.check_ats_frame(["Duke wins this one easy"])[1]
    assert checks.check_ats_frame(["Duke covers"])[1]
    print(f"{len(items)} items ({sum(i['private'] for i in items)} private), "
          f"{sum(len(i['turns']) for i in items)} turns; fact pack as of {facts.get('as_of')}; "
          f"games missing from the fact pack: {missing or 'none'}; checks self-test ok")
    return 0 if not missing else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--side", default="http://127.0.0.1:8791/group")
    ap.add_argument("--hist", default="", help="the side sidecar's PICKY_IMESSAGE_HISTORY file (reset per item)")
    ap.add_argument("--tag", default="")
    ap.add_argument("--only", default="", help="item ids or categories, comma-separated")
    ap.add_argument("--public-only", action="store_true")
    ap.add_argument("--no-model", action="store_true")
    a = ap.parse_args()
    if a.no_model:
        return no_model()
    return asyncio.run(run(a))


if __name__ == "__main__":
    sys.exit(main())
