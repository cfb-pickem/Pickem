#!/usr/bin/env python3
"""ESPN polls (CFP, AP, Coaches) as the primary ranking source, checked in code (plan 2.3 source checks).

ESPN's free /rankings endpoint carries all three polls with release dates. Each poll is sanity-checked before it
is served: ranks 1..N with no gaps or repeats, every team a known FBS school (CFBD spelling), records that agree
with CFBD, a release date that isn't in the future or weeks old, and (CFP only) real overlap with the AP poll. A
poll that fails keeps the last good copy and logs the rejection. The Opus-scraped CFP list in slate_intel is only
a logged cross-check, never served.

  polls.py refresh      # fetch (1 h TTL), check, write cache/polls.json, mirror to Supabase picky.ranking
  polls.py show
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import cfbd  # noqa: E402

log = logging.getLogger("picky.polls")
URL = "https://site.api.espn.com/apis/site/v2/sports/football/college-football/rankings"
PATH = Path(os.environ.get("PICKY_POLLS_CACHE", HERE / "cache" / "polls.json"))
TTL = 3600
KINDS = {"cfp": "CFP", "ap": "AP", "usa": "Coaches"}
MAX_RELEASE_AGE_D = 16       # a poll older than this in-season is a broken feed, not "the latest"
MIN_RECORD_AGREE = 0.8       # share of ranked teams whose W-L matches CFBD within one game
MIN_CFP_AP_OVERLAP = 18      # of the CFP 25, at least this many are in the AP 25


def _fbs_index(records: list[dict]) -> dict:
    return {"teams": {cfbd.norm(r["team"]): {"name": r["team"]} for r in records if r.get("team")}}


def team_name(espn_team: dict, records: list[dict] | None = None, idx: dict | None = None) -> str:
    """ESPN location -> CFBD spelling when we can resolve it, else ESPN's own name."""
    t = espn_team or {}
    raw = t.get("location") or t.get("shortDisplayName") or t.get("displayName") or t.get("name") or "?"
    idx = idx or (_fbs_index(records) if records else None)
    if idx:
        for cand in (raw, t.get("shortDisplayName"), t.get("displayName")):
            if cand:
                hit = cfbd.resolve_team(cand, idx)
                if hit:
                    return hit["name"]
    return raw


def parse(raw: dict, records: list[dict] | None = None) -> dict:
    """ESPN payload -> {"CFP": {"date", "source", "ranks": [{rank, team, record}]}, "AP": ..., "Coaches": ...}."""
    idx = _fbs_index(records) if records else None
    if idx is None:  # names still need CFBD spelling: use the cache when no records were passed in
        try:
            full = cfbd.load_index()
            idx = {"teams": {k: v for k, v in (full or {}).get("teams", {}).items() if v.get("conference")}} or None
        except Exception:  # noqa: BLE001
            idx = None
    out = {}
    for rk in raw.get("rankings") or []:
        kind = KINDS.get(rk.get("type"))
        if not kind:
            continue
        ranks = []
        for t in rk.get("ranks") or []:
            ranks.append({"rank": t.get("current"), "team": team_name(t.get("team"), idx=idx),
                          "record": t.get("recordSummary") or ""})
        out[kind] = {"date": (rk.get("date") or "")[:10], "source": "ESPN", "headline": rk.get("headline") or "",
                     "ranks": ranks}
    return out


def _wl(s: str) -> tuple[int, int] | None:
    try:
        w, l = str(s).split("-")[:2]
        return int(w), int(l)
    except (ValueError, AttributeError):
        return None


def sanity(kind: str, poll: dict, records: list[dict], now: float | None = None,
           others: dict | None = None) -> tuple[bool, list[str]]:
    """(ok, problems) for one parsed poll."""
    problems = []
    ranks = poll.get("ranks") or []
    nums = [r.get("rank") for r in ranks]
    if not ranks or sorted(nums) != list(range(1, len(ranks) + 1)):
        problems.append(f"{kind}: ranks aren't 1..{len(ranks)} without gaps or repeats")
    by_team = {r["team"]: r for r in records if r.get("team")}
    unknown = [r["team"] for r in ranks if r.get("team") not in by_team]
    if unknown:
        problems.append(f"{kind}: not FBS teams I know: {', '.join(unknown)}")
    if len({r.get("team") for r in ranks}) != len(ranks):
        problems.append(f"{kind}: a team is ranked twice")
    agree = total = 0
    for r in ranks:
        rec, wl = by_team.get(r.get("team")), _wl(r.get("record"))
        if not rec or not wl:
            continue
        total += 1
        cw, cl = (rec.get("total") or {}).get("wins", 0), (rec.get("total") or {}).get("losses", 0)
        agree += abs(cw - wl[0]) + abs(cl - wl[1]) <= 1
    if total and agree / total < MIN_RECORD_AGREE:
        problems.append(f"{kind}: only {agree}/{total} records agree with CFBD")
    try:
        rel = dt.datetime.fromisoformat(poll.get("date") or "").replace(tzinfo=dt.timezone.utc)
        today = dt.datetime.fromtimestamp(now or time.time(), dt.timezone.utc)
        if rel > today + dt.timedelta(days=1):
            problems.append(f"{kind}: release date {poll.get('date')} is in the future")
        elif (today - rel).days > MAX_RELEASE_AGE_D:
            problems.append(f"{kind}: release date {poll.get('date')} is {(today - rel).days} days old")
    except ValueError:
        problems.append(f"{kind}: no release date")
    if kind == "CFP" and others and others.get("AP"):
        ap = {r["team"] for r in others["AP"].get("ranks") or []}
        n = sum(r.get("team") in ap for r in ranks)
        if n < min(MIN_CFP_AP_OVERLAP, len(ranks) - 2):
            problems.append(f"CFP: only {n} of its {len(ranks)} teams are in the AP 25")
    return not problems, problems


def cross_check(cfp: dict, intel_cfp: dict | None) -> list[str]:
    """Differences between ESPN's CFP list and the Opus-scraped one (logged only)."""
    if not intel_cfp or not cfp:
        return []
    mine = {r["rank"]: r["team"] for r in cfp.get("ranks") or []}
    diffs = []
    for x in intel_cfp.get("top") or []:
        rk, team = x.get("rank"), x.get("team")
        if rk in mine and cfbd.norm(mine[rk]) != cfbd.norm(team or ""):
            diffs.append(f"#{rk}: ESPN {mine[rk]} vs scrape {team}")
    return diffs


def _fetch() -> dict:
    import httpx
    r = httpx.get(URL, timeout=10)
    r.raise_for_status()
    return r.json()


def load(path: Path | None = None) -> dict:
    try:
        return json.loads(Path(path or PATH).read_text())
    except (OSError, ValueError):
        return {}


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1))
    os.replace(tmp, path)


def _records() -> list[dict]:
    import sqlite3
    db = sqlite3.connect(cfbd.DB_PATH)
    try:
        row = db.execute("select json from datasets where name='records'").fetchone()
        return json.loads(row[0]) if row else []
    finally:
        db.close()


def refresh(fetch=None, records: list[dict] | None = None, path: Path | None = None, now: float | None = None,
            force: bool = False, pusher=None) -> dict:
    """Fetch (TTL 1 h), check each poll, keep the last good copy of any poll that fails. Returns the cache.
    pusher: None (local only), a callable(rows), or "default" = Supabase picky.ranking via picky_push."""
    path = Path(path or PATH)
    now = now or time.time()
    old = load(path)
    if not force and old and now - (old.get("_fetched_ts") or 0) < TTL:
        return old
    out = {k: v for k, v in old.items() if not k.startswith("_")}
    try:
        raw = (fetch or _fetch)()
        records = records if records is not None else _records()
        new = parse(raw, records)
    except Exception as e:  # noqa: BLE001 - fail soft, keep the last good
        log.warning("polls: fetch failed: %r", e)
        _write(path, {**out, "_fetched_ts": now, "_error": repr(e)[:200]})
        return {**out, "_fetched_ts": now}
    for kind, poll in new.items():
        ok, problems = sanity(kind, poll, records, now=now, others=new)
        if ok:
            out[kind] = {**poll, "ok": True, "checked_ts": now, "rejected": []}
        else:
            log.warning("polls: %s rejected: %s", kind, "; ".join(problems))
            prev = out.get(kind) or {}
            out[kind] = {**prev, "rejected": problems, "rejected_ts": now} if prev.get("ok") else {
                **poll, "ok": False, "rejected": problems, "rejected_ts": now}
    try:
        import slate_intel
        diffs = cross_check(out.get("CFP") or {}, slate_intel.cfp())
        if diffs:
            log.info("polls: CFP scrape disagrees with ESPN (ESPN wins): %s", "; ".join(diffs))
            out["_cfp_scrape_diffs"] = diffs
    except Exception:  # noqa: BLE001
        pass
    out["_fetched_ts"] = now
    _write(path, out)
    if pusher:
        rows = [{"poll": k, "release_date": p["date"], "rank": r["rank"], "team": r["team"], "record": r["record"],
                 "src": p.get("source", "ESPN")} for k, p in out.items() if not k.startswith("_") and p.get("ok")
                and p.get("checked_ts") == now for r in p.get("ranks") or []]
        try:
            if rows:
                if pusher == "default":
                    import market_snapshot
                    market_snapshot.supabase_push("ranking", rows)
                else:
                    pusher(rows)
        except Exception as e:  # noqa: BLE001
            log.warning("polls: supabase mirror failed: %r", e)
    return out


def serve(kind: str, data: dict | None = None) -> dict | None:
    """A poll that passed its check, else None."""
    p = (data if data is not None else load()).get(kind) or {}
    return p if p.get("ok") else None


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    cmd = argv[0] if argv else "show"
    if cmd == "refresh":
        d = refresh(force="--force" in argv, pusher="default")
    else:
        d = load()
    for k in ("CFP", "AP", "Coaches"):
        p = d.get(k) or {}
        print(k, p.get("date"), "ok" if p.get("ok") else "NOT OK", "; ".join(p.get("rejected") or []),
              ", ".join(f"{r['rank']} {r['team']}" for r in (p.get("ranks") or [])[:12]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
