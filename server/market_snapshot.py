#!/usr/bin/env python3
"""Market history (plan 2.1 market_snapshot): dated DraftKings spread / total / moneyline per game.

The live-games poller already pulls ESPN's scoreboard (open + current odds for every FBS game this week); after
each scoreboard it calls hook(), which every 15 min (5 on game days) writes a row per game whose number moved,
or a heartbeat row every 6 h. Rows live in a local SQLite cache (cache/market.sqlite, read in ms) and are mirrored
to Supabase picky.market_snapshot through the service-only RPC public.picky_push. Retention: games that kicked
off more than 21 days ago keep only their first (open) and last (close) rows, here and in Supabase.

No extra fetch, no model call, no new LaunchAgent.

  market_snapshot.py show <game_id>
  market_snapshot.py push      # retry anything not mirrored yet
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
log = logging.getLogger("picky.market")
DB_PATH = Path(os.environ.get("PICKY_MARKET_DB", HERE / "cache" / "market.sqlite"))
SUPABASE_URL = "https://vopdioszofwdkwnujtiq.supabase.co"
EVERY, EVERY_GAME_DAY = 15 * 60, 5 * 60
HEARTBEAT = 6 * 3600
KEEP_DAYS = 21
TRIM_EVERY = 86400
FIELDS = ("spread_home", "total", "ml_home", "ml_away")
SCHEMA = """
create table if not exists snapshots (
  game_id text not null, ts real not null, book text not null, spread_home real, total real, ml_home integer,
  ml_away integer, open_spread_home real, open_total real, kickoff text, away text, home text,
  pushed integer not null default 0, primary key (game_id, ts, book));
create index if not exists snapshots_pushed on snapshots (pushed);
create table if not exists meta (k text primary key, v real);
"""


def _game_day(now: float) -> bool:
    try:
        import freshness
        return freshness.game_day(now)
    except Exception:  # noqa: BLE001
        return False


def connect(db: Path | None = None) -> sqlite3.Connection:
    p = Path(db or DB_PATH)
    c = sqlite3.connect(p, timeout=10)
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    return c


def rows_from_board(games: dict, now: float) -> list[dict]:
    """live_games board -> one snapshot row per game that has a market."""
    out = []
    for gid, g in (games or {}).items():
        od = g.get("odds") or {}
        sh, tot = od.get("spread_home"), od.get("total")
        if not isinstance(sh, (int, float)) and not isinstance(tot, (int, float)):
            continue
        out.append({"game_id": str(gid), "ts": float(now), "book": od.get("book") or "ESPN",
                    "spread_home": sh if isinstance(sh, (int, float)) else None,
                    "total": tot if isinstance(tot, (int, float)) else None,
                    "ml_home": od.get("ml_home"), "ml_away": od.get("ml_away"),
                    "open_spread_home": od.get("spread_home_open"), "open_total": od.get("total_open"),
                    "kickoff": str(g.get("start") or ""), "away": (g.get("away") or {}).get("name"),
                    "home": (g.get("home") or {}).get("name")})
    return out


def _meta(c, k: str, v: float | None = None) -> float | None:
    if v is None:
        r = c.execute("select v from meta where k=?", (k,)).fetchone()
        return r[0] if r else None
    c.execute("insert or replace into meta (k, v) values (?, ?)", (k, v))
    return v


def record(games: dict, now: float, db: Path | None = None) -> int:
    """Write due snapshots. Returns rows written, or -1 when the 15/5-minute interval isn't up yet."""
    c = connect(db)
    try:
        last_run = _meta(c, "last_run")
        every = EVERY_GAME_DAY if _game_day(now) else EVERY
        if last_run is not None and now - last_run < every - 1:
            return -1
        n = 0
        with c:
            for r in rows_from_board(games, now):
                prev = c.execute("select * from snapshots where game_id=? and book=? order by ts desc limit 1",
                                 (r["game_id"], r["book"])).fetchone()
                if prev and all(prev[f] == r[f] for f in FIELDS) and now - prev["ts"] < HEARTBEAT:
                    continue
                c.execute("insert or ignore into snapshots (game_id, ts, book, spread_home, total, ml_home, ml_away,"
                          " open_spread_home, open_total, kickoff, away, home) values (?,?,?,?,?,?,?,?,?,?,?,?)",
                          tuple(r[k] for k in ("game_id", "ts", "book", "spread_home", "total", "ml_home", "ml_away",
                                               "open_spread_home", "open_total", "kickoff", "away", "home")))
                n += 1
            _meta(c, "last_run", now)
        return n
    finally:
        c.close()


def history(game_id: str, db: Path | None = None) -> list[dict]:
    c = connect(db)
    try:
        return [dict(r) for r in c.execute("select * from snapshots where game_id=? order by ts", (str(game_id),))]
    finally:
        c.close()


def summary(game_id: str, db: Path | None = None, lock_ts: float | None = None, rows: list[dict] | None = None) -> dict | None:
    """Open, now, move, total move, and the number at the league lock, from the snapshot history."""
    h = rows if rows is not None else history(game_id, db)
    h = [r for r in h if r.get("spread_home") is not None or r.get("total") is not None]
    if not h:
        return None
    first, last = h[0], h[-1]
    osh = first.get("open_spread_home") if first.get("open_spread_home") is not None else first.get("spread_home")
    otot = first.get("open_total") if first.get("open_total") is not None else first.get("total")
    s = {"open_spread_home": osh, "spread_home": last.get("spread_home"), "open_total": otot, "total": last.get("total"),
         "ml_home": last.get("ml_home"), "ml_away": last.get("ml_away"), "n": len(h), "first_ts": first["ts"],
         "as_of_ts": last["ts"], "source": f"ESPN {last.get('book') or ''}".strip()}
    s["move"] = round(s["spread_home"] - osh, 1) if s["spread_home"] is not None and osh is not None else None
    s["total_move"] = round(s["total"] - otot, 1) if s["total"] is not None and otot is not None else None
    if lock_ts is not None:
        before = [r for r in h if r["ts"] <= lock_ts]
        at = before[-1] if before else None
        s["at_lock_spread_home"] = at.get("spread_home") if at else None
        s["since_lock"] = (round(s["spread_home"] - at["spread_home"], 1)
                           if at and at.get("spread_home") is not None and s["spread_home"] is not None else None)
    return s


def latest_all(db: Path | None = None) -> dict[str, list[dict]]:
    """Every game's snapshot rows (for the fact store build), oldest first."""
    p = Path(db or DB_PATH)
    if not p.exists():
        return {}
    c = connect(p)
    try:
        out: dict[str, list[dict]] = {}
        for r in c.execute("select * from snapshots order by game_id, ts"):
            out.setdefault(r["game_id"], []).append(dict(r))
        return out
    finally:
        c.close()


def trim(db: Path | None = None, now: float | None = None, keep_days: int = KEEP_DAYS) -> int:
    """Games whose newest snapshot is older than keep_days keep only their first and last rows."""
    now = now or time.time()
    c = connect(db)
    try:
        with c:
            cut = now - keep_days * 86400
            cur = c.execute(
                "delete from snapshots where game_id in (select game_id from snapshots group by game_id having max(ts) < ?)"
                " and ts not in (select min(ts) from snapshots s2 where s2.game_id = snapshots.game_id"
                " union select max(ts) from snapshots s3 where s3.game_id = snapshots.game_id)", (cut,))
            _meta(c, "last_trim", now)
            return cur.rowcount
    finally:
        c.close()


def _service_key() -> str:
    import subprocess
    return os.environ.get("SUPABASE_SERVICE_KEY") or subprocess.run(
        ["security", "find-generic-password", "-a", "drake", "-s", "CFB_PICKEM__SUPABASE_SERVICE_KEY", "-w"],
        capture_output=True, text=True, check=True, timeout=10).stdout.strip()


def supabase_push(kind: str, rows: list[dict]) -> int:
    """public.picky_push(kind, rows): service-role only; the picky schema isn't exposed to the API."""
    import httpx
    key = _service_key()
    r = httpx.post(f"{SUPABASE_URL}/rest/v1/rpc/picky_push", json={"kind": kind, "rows": rows}, timeout=15,
                   headers={"apikey": key, "Authorization": f"Bearer {key}"})
    r.raise_for_status()
    return int(r.json() or 0)


def push(db: Path | None = None, pusher=None, batch: int = 500) -> int:
    """Mirror unpushed rows to Supabase. Failures leave rows unpushed for the next try. Returns rows pushed."""
    pusher = pusher or (lambda rows: supabase_push("market_snapshot", rows))
    c = connect(db)
    n = 0
    try:
        while True:
            rows = [dict(r) for r in c.execute("select * from snapshots where pushed=0 order by ts limit ?", (batch,))]
            if not rows:
                return n
            body = [{k: v for k, v in r.items() if k != "pushed"} for r in rows]
            try:
                pusher(body)
            except Exception as e:  # noqa: BLE001
                log.warning("market: push failed (%d rows kept): %r", len(rows), e)
                return n
            with c:
                c.executemany("update snapshots set pushed=1 where game_id=? and ts=? and book=?",
                              [(r["game_id"], r["ts"], r["book"]) for r in rows])
            n += len(rows)
    finally:
        c.close()


_push_lock = threading.Lock()


def _push_and_trim(db, pusher, now) -> None:
    if not _push_lock.acquire(blocking=False):
        return
    try:
        push(db=db, pusher=pusher)
        c = connect(db)
        try:
            last = _meta(c, "last_trim")
        finally:
            c.close()
        if last is None or now - last >= TRIM_EVERY:
            trim(db=db, now=now)
            try:
                if pusher is None:
                    supabase_push("trim_market", [{"keep_days": KEEP_DAYS}])
            except Exception as e:  # noqa: BLE001
                log.warning("market: supabase trim failed: %r", e)
    except Exception as e:  # noqa: BLE001
        log.warning("market: push/trim failed: %r", e)
    finally:
        _push_lock.release()


def hook(games: dict, now: float, db: Path | None = None, pusher=None, background: bool = True) -> None:
    """The live-games poller's per-scoreboard call. Never raises; the mirror runs off the poller's thread."""
    try:
        n = record(games, now, db=db)
        if n < 0:
            return
        if n:
            log.info("market: %d snapshot rows", n)
        if background:
            threading.Thread(target=_push_and_trim, args=(db, pusher, now), daemon=True).start()
        else:
            _push_and_trim(db, pusher, now)
    except Exception as e:  # noqa: BLE001
        log.warning("market: snapshot failed: %r", e)


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    if argv and argv[0] == "push":
        print(push())
        return 0
    if argv and argv[0] == "show" and len(argv) > 1:
        print(json.dumps(summary(argv[1]), indent=1))
        for r in history(argv[1]):
            print(time.strftime("%a %H:%M", time.localtime(r["ts"])), r["spread_home"], r["total"], r["pushed"])
        return 0
    c = connect()
    print(c.execute("select count(*), count(distinct game_id), sum(pushed=0) from snapshots").fetchone()[:])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
