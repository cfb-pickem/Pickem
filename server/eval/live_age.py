"""
How old is the live data behind a game-day answer? (live-freshness work, 2026-11-18)

Replays the recorded Saturday (eval/sat_fixture.py, 2026-11-14) through a fake ESPN whose clock moves in real
time, runs the real poller (live_games.Poller) of the tree under test against it, and asks gameday's score lane
about a random live game every few seconds, the way a Saturday DM does. For each ask: how old the score
(ts) and the play-by-play (detail_ts) were when the answer was built, and how long the answer took.
No model, no network beyond 127.0.0.1.

  live_age.py --server <tree>/server [--minutes 4] [--at 15:00] [--every 5] [--port 8831]
"""
from __future__ import annotations

import argparse
import json
import os
import random
import statistics
import sys
import tempfile
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
POOL = ["401858301", "401860914", "401862827", "401864537"]  # sat_dm's pool games


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", required=True)
    ap.add_argument("--minutes", type=float, default=4)
    ap.add_argument("--at", default="15:00")
    ap.add_argument("--every", type=float, default=5)
    ap.add_argument("--port", type=int, default=8831)
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()
    tmp = Path(tempfile.mkdtemp(prefix="picky-live-age-"))
    os.environ.update(PICKY_LIVE_BASE=f"http://127.0.0.1:{a.port}", PICKY_LIVE_CACHE=str(tmp / "live.json"),
                      PICKY_GAMEDAY_FILE=str(tmp / "gameday.json"), PICKY_REPLAY_CLOCK=str(tmp / "clock"))
    sys.path[:0] = [str(Path(a.server).resolve()), str(HERE)]
    import live_games as L  # the tree under test, before sat_fixture/live_replay put another server dir first
    import gameday as G
    import sat_fixture as F
    assert Path(L.__file__).parent == Path(a.server).resolve(), L.__file__

    sb, games = F.load()
    t0, w0 = F.at_ct(f"2026-11-14 {a.at}"), time.time()

    class Moving(F.Frozen):
        def now(self) -> float:
            return t0 + time.time() - w0
    clock = Moving(t0)
    srv, calls = F.serve(sb, games, clock, a.port)
    stop = threading.Event()
    p = L.Poller(cache=Path(os.environ["PICKY_LIVE_CACHE"]), pool_ids_fn=lambda: POOL)
    p.games = {}
    threading.Thread(target=L.run, kwargs={"stop": stop, "poller": p}, daemon=True).start()
    time.sleep(25)  # warm-up: the first scoreboard + summaries
    rnd = random.Random(a.seed)
    rows, end = [], time.time() + a.minutes * 60
    while time.time() < end:
        (tmp / "clock").write_text(str(clock.now()))
        live = [g for g in (L.load().get("games") or {}).values() if g.get("state") == "in"]
        if live:
            g = rnd.choice(live)
            tier = "pool" if g["id"] in POOL else "ranked" if (g["away"].get("rank") or g["home"].get("rank")) else "other"
            q = f"{g['away']['name']} {g['home']['name']} score?"
            s0 = time.time()
            out = G.answer("gd_score", None, q) or ""
            now = time.time()
            used = (L.load().get("games") or {}).get(g["id"]) or {}
            rows.append({"tier": tier, "score_age": now - (used.get("ts") or 0),
                         "pbp_age": now - (used.get("detail_ts") or 0) if used.get("detail_ts") else None,
                         "answer_s": now - s0, "as_of": out.startswith("As of")})
        time.sleep(rnd.uniform(0.5, 1.5) * a.every)
    stop.set()
    srv.shutdown()

    def pct(xs, q):
        xs = sorted(x for x in xs if x is not None)
        return round(xs[min(len(xs) - 1, int(q * len(xs)))], 1) if xs else None
    rep = {"server": a.server, "asks": len(rows), "espn_calls": calls}
    for tier in ("all", "pool", "ranked", "other"):
        rs = [r for r in rows if tier == "all" or r["tier"] == tier]
        if rs:
            rep[tier] = {"n": len(rs),
                         "score_age_p50": pct([r["score_age"] for r in rs], .5),
                         "score_age_max": pct([r["score_age"] for r in rs], 1),
                         "pbp_age_p50": pct([r["pbp_age"] for r in rs], .5),
                         "pbp_age_max": pct([r["pbp_age"] for r in rs], 1),
                         "answer_ms_p50": round(1000 * statistics.median(r["answer_s"] for r in rs)),
                         "answer_ms_max": round(1000 * max(r["answer_s"] for r in rs)),
                         "as_of": sum(r["as_of"] for r in rs)}
    print(json.dumps(rep, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
