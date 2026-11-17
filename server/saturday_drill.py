"""
Saturday game-day drill (dry run): a simulated Saturday at N x speed (default 4x) against replayed
ESPN play-by-play, with every post landing in a SANDBOX outbox nobody drains, plus a burst of
simultaneous group asks on a side sidecar to measure reply latency under load. Never texts anyone.

  saturday_drill.py run   [--speed 4] [--date 2026-11-21] [--start 06:45] [--end 18:15]
                          [--burst-at 15:00] [--burst-n 20] [--no-burst] [--fixture DIR]
  saturday_drill.py burst --live FILE [--n 20] [--modes concurrent,sequential] [--port 8808] [--out DIR]
  saturday_drill.py report DIR

run     (no model, no lock) PICKY_PRIVATE_DIR -> a temp sandbox holding copies of the group config
        (dry_run off, so the real queueing path runs), the posts state (done keys: the picks-up post
        isn't redone), the members map, league material and the no-repeat ledger. The real
        cfb-pickem-private/picky_outbox (what the live sidecar's /outbox hands the watcher) is never
        written: the drill snapshots it + the real send log before and after and fails if anything
        it queued shows up there. The sim clock drives
          - group_posts.tick(now=sim) every 5 sim-min: 7:00 reminder, 9:30 Saturday post, the
            straggler DM nudges (9:00 / 10:30) and anything else group_posts would post;
          - close-cover every 15 sim-s on the replay fixture's 4 pool games (week 11, shifted onto
            this Saturday's kickoff slots): close_cover.Engine + group_outbox.post, as tick_once;
          - trap moments through a fresh Engine (kneel-down, final, Q4 9:30, a third alert) that
            must never post, plus one positive control that must;
          - the burst (below) at --burst-at, with the pool games' live state taken from the replay
            at that sim moment (week-12 game ids/teams, no fixture names).
        Audits: <= 2 cover posts per game, only in the final 8:00 of the 4th / OT, never with under
        30 s left, never during a kneel, never a final; no final-score post of any kind; nudges once
        per member per slot, mapped stragglers only, before lock - 5 min, no other member named.
burst   inside the shared exam lock (re-execs itself under lockf), a side sidecar
        (eval/genius/side.sh: temp state, PICKY_NO_ANTHROPIC=1, yields to members via live_flag) from a
        throwaway worktree of HEAD, PICKY_LIVE_CACHE = the synthetic live file (re-stamped every 5 s so
        the sidecar never refetches ESPN). --n generic game-day asks from distinct mapped members:
          concurrent  all at once (the sidecar's own queueing)
          sequential  one at a time from the same t0 (how the watcher handles rows today: serially)
        Latency = reply time - burst start. p50/p90/max overall and per lane + the sidecar log.

Output (member names in replies): cfb-pickem-private/drills/saturday-<stamp>/ (report.json, report.md,
burst-*.jsonl, side.log). Code only here; nothing private in the repo.
"""
from __future__ import annotations

import argparse
import bisect
import datetime as dt
import gzip
import hashlib
import json
import os
import re
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
CT = ZoneInfo("America/Chicago")
REAL_PRIVATE = Path("/Users/Agent/cfb-pickem-private")
OUT_ROOT = Path(os.environ.get("PICKY_DRILL_OUT", REAL_PRIVATE / "drills"))
PY = "/Users/Agent/cfb-pickem/server/.venv/bin/python3"
LOCK = "/tmp/picky-exam.lock"
LOCK_ENV = "PICKY_DRILL_LOCKED"
FIXTURE = HERE / "eval" / "fixtures" / "live-2026-11-14"
SANDBOX_FILES = ("picky_group_config.json", "picky_group_posts_state.json", "picky_group_members.json",
                 "league_material.json", "picky_no_repeat.json")
P90_TARGET = 45.0

# generic game-day asks (no member data); pool teams are this Saturday's
BURST_ASKS = [
    "who's covering Iowa Illinois", "score of the LSU game?", "is Northwestern covering", "how am I doing this week",
    "who's winning the league right now", "Texas Tech covering?", "roast Ackbar", "what's the line on LSU Tennessee",
    "am I gonna cash Illinois", "how much time left in the Iowa game", "did Minnesota score", "who has the ball in the Baylor game",
    "you suck picky", "how many points does Illinois need to cover", "best game on right now?", "who's in last",
    "picky what's your record this week", "lol Tennessee defense is a joke", "what channel is LSU Tennessee on",
    "is anyone sweating the Northwestern game",
]


# ---------------------------------------------------------------------------------------------------
# sandbox (must be set up before any server module that reads PICKY_PRIVATE_DIR is imported)
# ---------------------------------------------------------------------------------------------------

def make_sandbox(src: Path = REAL_PRIVATE) -> Path:
    d = Path(tempfile.mkdtemp(prefix="picky-drill-"))
    for name in SANDBOX_FILES:
        if (src / name).exists():
            shutil.copy2(src / name, d / name)
    cfg_p = d / "picky_group_config.json"
    cfg = json.loads(cfg_p.read_text()) if cfg_p.exists() else {}
    cfg["dry_run"] = False  # the real queueing path, into the sandbox outbox
    cfg_p.write_text(json.dumps(cfg))
    os.environ.update({"PICKY_PRIVATE_DIR": str(d), "PICKY_NO_REPEAT_STATE": str(d / "picky_no_repeat.json"),
                       "PICKY_CALLBACKS_STATE": str(d / "callbacks.json"), "PICKY_EXCHANGE_LOG": str(d / "ex.jsonl"),
                       "PICKY_NO_ANTHROPIC": "1"})
    return d


def assert_sandboxed(ob, real: Path = REAL_PRIVATE) -> None:
    if ob.PRIVATE.resolve() == real.resolve() or real.resolve() in ob.OUTBOX.resolve().parents:
        raise SystemExit("refusing to run: group_outbox points at the real private dir")


def real_drain_snapshot(real: Path = REAL_PRIVATE) -> dict:
    ob_dir, log = real / "picky_outbox", real / "picky_group_sends.jsonl"
    lines = log.read_text().splitlines() if log.exists() else []
    return {"outbox": sorted(p.name for p in ob_dir.glob("*")) if ob_dir.exists() else [], "send_lines": len(lines)}


def real_drain_check(before: dict, drill_keys: set[str], real: Path = REAL_PRIVATE) -> dict:
    """What appeared in the real outbox / send log during the drill, and whether any of it was ours."""
    ob_dir, log = real / "picky_outbox", real / "picky_group_sends.jsonl"
    new_files = [n for n in (sorted(p.name for p in ob_dir.glob("*")) if ob_dir.exists() else [])
                 if n not in set(before["outbox"])]
    lines = log.read_text().splitlines() if log.exists() else []
    new_events = []
    for ln in lines[before["send_lines"]:]:
        try:
            new_events.append(json.loads(ln))
        except ValueError:
            continue
    ours = [e for e in new_events if e.get("key") in drill_keys]
    for n in new_files:
        try:
            if json.loads((ob_dir / n).read_text()).get("key") in drill_keys:
                ours.append({"file": n})
        except (OSError, ValueError):
            continue
    return {"new_real_files": len(new_files), "new_real_events": len(new_events), "from_drill": len(ours),
            "ok": not ours}


# ---------------------------------------------------------------------------------------------------
# replayed play-by-play on this Saturday's clock
# ---------------------------------------------------------------------------------------------------

class CoverSim:
    """The fixture's pool games, each shifted so its first play lands on a kickoff slot."""

    def __init__(self, fixture: Path, kickoffs: list[float]):
        import live_games as L
        pool = json.loads((fixture / "pool.json").read_text())
        self.games = []
        gids = sorted(g for g in pool if not g.startswith("_") and (fixture / f"summary_{g}.json.gz").exists())
        for gid, kick in zip(gids, kickoffs):
            d = json.load(gzip.open(fixture / f"summary_{gid}.json.gz"))
            tl = [(dt.datetime.fromisoformat(p["wallclock"].replace("Z", "+00:00")).timestamp(), i)
                  for i, p in enumerate(L._flat_plays(d)) if p.get("wallclock")]
            tl.sort()
            off = kick - tl[0][0]
            p = pool[gid]
            self.games.append({"gid": gid, "data": d, "ts": [t + off for t, _ in tl], "idx": [i for _, i in tl],
                               "pool": {"GameId": gid, "Away": p["away"], "Home": p["home"], "line": p["line"]},
                               "kick": kick, "end": tl[-1][0] + off})

    def snaps(self, now: float) -> list[tuple[dict, dict | None]]:
        import close_cover as cc
        out = []
        for g in self.games:
            k = bisect.bisect_right(g["ts"], now) - 1
            out.append((g, cc.snapshot(g["data"], upto=g["idx"][k]) if k >= 0 else None))
        return out


def week12_live(template: dict, sim: CoverSim, now: float, real_now: float | None = None) -> dict:
    """The live cache for the burst: this week's pool games (real ids/teams) carrying the replayed
    games' state at sim time `now` (scores, clock, possession), never the fixture's names or plays."""
    real_now = real_now or time.time()
    data = json.loads(json.dumps(template))
    pool_ids = [str(x) for x in data.get("pool_ids") or []]
    rows = sorted((data["games"][g] for g in pool_ids if g in data.get("games", {})), key=lambda g: g.get("start") or "")
    for row, (_g, snap) in zip(rows, sim.snaps(now)):
        if snap:
            row.update({"state": snap["state"], "status_name": "STATUS_FINAL" if snap["state"] == "post" else "STATUS_IN_PROGRESS",
                        "period": snap["period"], "clock": snap["clock"], "away_score": snap["away_score"],
                        "home_score": snap["home_score"], "possession": snap.get("has_ball")})
        for k in ("last_play", "scoring", "plays", "injuries", "qb", "headlines"):
            row.pop(k, None)
        row["ts"] = row["sb_ts"] = real_now
    data["updated_ts"] = real_now
    return data


# ---------------------------------------------------------------------------------------------------
# audits (pure)
# ---------------------------------------------------------------------------------------------------

FINAL_WORDS = re.compile(r"\bfinal(?! (?:\d|minutes?|seconds?|drive|stretch|play|possession))\b|\bin the books\b|\bthat'?s final\b", re.I)


def audit_cover(posts: list[dict], window_s: int = 480, tune_s: int = 30, max_posts: int = 2) -> list[str]:
    """posts: [{gid, n, kind, state, period, clock_s, kneeling, text}] as queued. Problems found."""
    bad, per = [], {}
    for p in posts:
        per[p["gid"]] = per.get(p["gid"], 0) + 1
        where = f"{p['gid']} post {p.get('n')}"
        if per[p["gid"]] > max_posts:
            bad.append(f"{where}: post #{per[p['gid']]} (max {max_posts})")
        if p.get("state") != "in":
            bad.append(f"{where}: game state {p.get('state')}")
        ot = int(p.get("period") or 0) >= 5
        if not ot and not (int(p.get("period") or 0) == 4 and p.get("clock_s") is not None and p["clock_s"] < window_s):
            bad.append(f"{where}: outside the last 8:00 (Q{p.get('period')} {p.get('clock_s')}s)")
        if not ot and p.get("clock_s") is not None and p["clock_s"] < tune_s:
            bad.append(f"{where}: {p['clock_s']}s left")
        if p.get("kneeling"):
            bad.append(f"{where}: during a kneel")
        if FINAL_WORDS.search(p.get("text") or ""):
            bad.append(f"{where}: reads like a final")
    return bad


def audit_group_posts(events: list[dict]) -> list[str]:
    """Sandbox send-log 'queued' group events: no final-score post, ever."""
    bad = []
    for e in events:
        if e.get("event") != "queued":
            continue
        if e.get("kind") in ("ping", "final") or str(e.get("key", "")).startswith("ping:"):
            bad.append(f"final-score post queued: {e.get('key')}")
        elif e.get("kind") != "cover" and any(FINAL_WORDS.search(t) for t in e.get("texts") or []):
            bad.append(f"{e.get('kind')} {e.get('key')} reads like a final")
    return bad


def audit_nudges(events: list[dict], mapped: set[str], members: set[str], lock: dt.datetime,
                 stop_before: dt.timedelta = dt.timedelta(minutes=5)) -> list[str]:
    """Sandbox send-log 'dm-queued' events (each carrying 'sim' = sim time ISO): once per member per
    slot, mapped members only, before lock - 5 min, no other member named."""
    bad, seen = [], set()
    for e in events:
        if e.get("event") != "dm-queued" or not str(e.get("key", "")).startswith("nudge:"):
            continue
        _n, _season, _week, slot, team = e["key"].split(":", 4)
        if (slot, team) in seen:
            bad.append(f"{team}: two {slot} nudges")
        seen.add((slot, team))
        if team not in mapped:
            bad.append(f"{team}: not a mapped member")
        if e.get("sim") and dt.datetime.fromisoformat(e["sim"]) >= lock - stop_before:
            bad.append(f"{team}: nudge at {e['sim']} (lock {lock:%H:%M})")
        txt = " ".join(e.get("texts") or [])
        others = [m for m in members if m != team and re.search(rf"(?<!\w){re.escape(m)}(?!\w)", txt)]
        if others:
            bad.append(f"{team}: nudge names {others[0]}")
    return bad


def pct(xs: list[float], q: float) -> float | None:
    """Nearest-rank percentile."""
    if not xs:
        return None
    s = sorted(xs)
    return s[max(0, min(len(s) - 1, -(-int(q * 100) * len(s) // 100) - 1))]


def latency_stats(rows: list[dict]) -> dict:
    xs = [r["s"] for r in rows if r.get("s") is not None]
    out = {"n": len(xs), "p50": round(statistics.median(xs), 1) if xs else None,
           "p90": pct(xs, 0.90), "max": max(xs) if xs else None,
           "errors": sum(1 for r in rows if str(r.get("reply") or "").startswith("[ERROR") or r.get("reply") is None),
           "lanes": {}}
    for lane in sorted({(r.get("lane") or "-").split(":")[0] for r in rows}):
        ls = [r["s"] for r in rows if (r.get("lane") or "-").split(":")[0] == lane]
        out["lanes"][lane] = {"n": len(ls), "p50": round(statistics.median(ls), 1), "max": max(ls)}
    return out


# ---------------------------------------------------------------------------------------------------
# trap moments: must never post (and one control that must)
# ---------------------------------------------------------------------------------------------------

def trap_results() -> list[dict]:
    import close_cover as cc
    teams = {"away": {"id": "1", "abbr": "NU", "loc": "Northwestern", "nick": "Wildcats"},
             "home": {"id": "2", "abbr": "MINN", "loc": "Minnesota", "nick": "Golden Gophers"}}
    pool = {"GameId": "trap", "week": 12, "Away": "Northwestern", "Home": "Minnesota", "line": 2.5}

    def snap(**kw):
        s = {"state": "in", "period": 4, "clock": "5:00", "away_score": 20, "home_score": 17, "teams": teams,
             "network": None, "has_ball": None, "receiving": None, "down": 1, "distance": 10, "yte": None,
             "spot": None, "kneeling": False, "turnover": False, "situation": True,
             "timeouts": {"away": 2, "home": 2}, "timeouts_src": "espn", "play_id": None}
        s.update(kw)
        s["clock_s"] = cc.clock_secs(s["clock"])
        return s
    # NU 20-17 with Minnesota +2.5: NU covers by 0.5, Minnesota (home) must score to flip it
    cases = [
        ("control: Q4 5:00, Minnesota has the ball, cover by 0.5", snap(has_ball="home"), True, {}),
        ("kneel-down: Q4 1:20, NU in victory formation", snap(clock="1:20", has_ball="away", kneeling=True, down=2,
                                                               timeouts={"away": 0, "home": 0}), False, {}),
        ("final: game over, cover by 0.5", snap(state="post", clock="0:00", has_ball="home"), False, {}),
        ("Q4 9:30: outside the last 8:00", snap(clock="9:30", has_ball="home"), False, {}),
        ("third alert: 2 already posted", snap(has_ball="home"), False,
         {"trap": {"posts": [{"ts": 0, "kind": "alert", "cov": "away"}, {"ts": 60, "kind": "flip", "cov": "home"}]}}),
        ("Q4 0:12, Minnesota's ball: no time to tune in", snap(clock="0:12", has_ball="home"), False, {}),
    ]
    out = []
    for name, s, want, state in cases:
        eng = cc.Engine(json.loads(json.dumps(state)))
        posts = eng.step(10_000.0, [{"gid": "trap", "pool": pool, "snap": s, "counts": None, "ackbar": None}])
        out.append({"case": name, "posted": bool(posts), "want": want, "ok": bool(posts) == want,
                    "text": posts[0]["text"] if posts else ""})
    return out


# ---------------------------------------------------------------------------------------------------
# burst (side sidecar, exam lock)
# ---------------------------------------------------------------------------------------------------

def _keychain(service: str) -> str:
    return subprocess.run(["security", "find-generic-password", "-a", "drake", "-s", service, "-w"],
                          capture_output=True, text=True).stdout.strip()


def burst_senders(n: int) -> list[str]:
    members = json.loads((REAL_PRIVATE / "picky_group_members.json").read_text())["members"]
    hs = [m["handles"][0] for m in members if m.get("handles") and m.get("team")]
    return [hs[i % len(hs)] for i in range(n)]


def _restamp(live: Path, stop: threading.Event) -> None:
    while not stop.wait(5):
        try:
            d = json.loads(live.read_text())
            now = time.time()
            for gid in d.get("pool_ids") or []:
                g = d["games"].get(str(gid))
                if g:
                    g["ts"] = g["sb_ts"] = now
            d["updated_ts"] = now
            tmp = live.with_suffix(".tmp")
            tmp.write_text(json.dumps(d))
            os.replace(tmp, live)
        except (OSError, ValueError):
            pass


def run_burst(live_src: Path, out: Path, n: int, modes: list[str], port: int) -> dict:
    """Caller holds the exam lock."""
    import asyncio

    import httpx

    import real_replay
    out.mkdir(parents=True, exist_ok=True)
    asks = [BURST_ASKS[i % len(BURST_ASKS)] for i in range(n)]
    handles = burst_senders(n)
    hdr = {"X-Picky-Group-Secret": _keychain("CFB_PICKEM__PICKY_GROUP_SECRET"),
           "X-Picky-Test": _keychain("CFB_PICKEM__TEST_TOKEN")}
    wt = real_replay.make_worktree("HEAD")
    sha = subprocess.run(["git", "-C", str(wt), "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
    run_dir = Path(f"/private/tmp/picky-drill-run/{os.getpid()}")
    run_dir.mkdir(parents=True, exist_ok=True)
    live = run_dir / "live_games.json"
    shutil.copy2(live_src, live)
    stop = threading.Event()
    threading.Thread(target=_restamp, args=(live, stop), daemon=True).start()
    side_log = out / "side.log"
    side = subprocess.Popen(["/bin/zsh", str(wt / "server/eval/genius/side.sh"), str(wt), str(port), str(run_dir)],
                            stdout=open(side_log, "w"), stderr=subprocess.STDOUT, start_new_session=True)
    res = {"sha": sha, "n": n, "modes": {}}
    try:
        for _ in range(120):
            try:
                if httpx.get(f"http://127.0.0.1:{port}/health", timeout=2).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            if side.poll() is not None:
                raise RuntimeError(f"side sidecar exited rc={side.returncode}; see {side_log}")
            time.sleep(2)
        else:
            raise RuntimeError("side sidecar never came up")
        url = f"http://127.0.0.1:{port}/group"
        # one warm-up text (production's keep-warm keeps the prefix primed; a side sidecar has none)
        httpx.post(url, headers=hdr, timeout=600, json={"handle": handles[0], "text": "@Picky what's the line on Iowa",
                                                        "chat": "exam-drill-warm", "dm": False})

        async def one(cl, i, t0, chat):
            try:
                j = (await cl.post(url, headers=hdr, json={"handle": handles[i], "text": "@Picky " + asks[i],
                                                           "chat": chat, "dm": False})).json()
                reply, lane = j.get("reply"), j.get("lane")
            except Exception as e:  # noqa: BLE001
                reply, lane = f"[ERROR {e!r}]", None
            return {"i": i, "q": asks[i], "lane": lane, "reply": reply, "s": round(time.time() - t0, 1),
                    "own_s": None}

        async def go(mode):
            chat = f"exam-drill-{mode}-{os.getpid()}"
            async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10, read=900, write=10, pool=900),
                                         limits=httpx.Limits(max_connections=n + 2)) as cl:
                t0 = time.time()
                if mode == "concurrent":
                    return await asyncio.gather(*(one(cl, i, t0, chat) for i in range(n)))
                rows = []
                for i in range(n):
                    t1 = time.time()
                    r = await one(cl, i, t0, chat)
                    r["own_s"] = round(time.time() - t1, 1)
                    rows.append(r)
                return rows

        for mode in modes:
            print(f"burst {mode}: {n} asks", flush=True)
            rows = asyncio.run(go(mode))
            (out / f"burst-{mode}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
            res["modes"][mode] = latency_stats(rows)
            for r in sorted(rows, key=lambda r: r["s"]):
                print(f"  {r['s']:6.1f}s {r['lane'] or '-':<16} {r['q'][:40]:<40} {str(r['reply'])[:80]}", flush=True)
    finally:
        stop.set()
        try:
            os.killpg(side.pid, signal.SIGTERM)
            side.wait(15)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            side.kill()
        real_replay.drop_worktree(wt)
    import eval.speed_replay as sr  # noqa: E402  (log facts: cold primes, rerolls)
    res["log"] = sr.log_facts(side_log)
    (out / "burst.json").write_text(json.dumps(res, indent=1))
    return res


def burst_locked(live: Path, out: Path, n: int, modes: list[str], port: int) -> dict:
    """run_burst inside the exam lock (a child under lockf)."""
    if os.environ.get(LOCK_ENV) == "1":
        return run_burst(live, out, n, modes, port)
    env = dict(os.environ, **{LOCK_ENV: "1"})
    subprocess.call(["/usr/bin/lockf", "-t", "14400", LOCK, PY, "-I", str(Path(__file__).resolve()), "burst",
                     "--live", str(live), "--out", str(out), "--n", str(n), "--modes", ",".join(modes),
                     "--port", str(port)], env=env)
    p = out / "burst.json"
    return json.loads(p.read_text()) if p.exists() else {"error": "burst produced no result"}


# ---------------------------------------------------------------------------------------------------
# the simulated Saturday
# ---------------------------------------------------------------------------------------------------

def _hm(date: dt.date, s: str) -> dt.datetime:
    h, m = (int(x) for x in s.split(":"))
    return dt.datetime(date.year, date.month, date.day, h, m, tzinfo=CT)


def run_drill(a) -> dict:
    sandbox = make_sandbox()
    import close_cover as cc
    import group_outbox as ob
    import group_posts as gp
    assert_sandboxed(ob)
    before = real_drain_snapshot()
    out = OUT_ROOT / f"saturday-{dt.datetime.now(CT):%Y%m%d-%H%M%S}"
    out.mkdir(parents=True, exist_ok=True)
    os.chmod(out, 0o700)
    day = dt.date.fromisoformat(a.date)
    t_start, t_end = _hm(day, a.start), _hm(day, a.end)
    template = json.loads((HERE / "cache" / "live_games.json").read_text())
    pool_rows = sorted((template["games"][str(g)] for g in template.get("pool_ids") or []), key=lambda g: g["start"])
    kicks = [dt.datetime.fromisoformat(g["start"].replace("Z", "+00:00")).timestamp() for g in pool_rows]
    sim = CoverSim(Path(a.fixture), kicks)
    engine = cc.Engine({}, ackbar_jab=cc._jab)
    print(f"sandbox {sandbox}; out {out}; {a.speed}x {t_start:%a %H:%M}-{t_end:%H:%M}; "
          f"games " + ", ".join(f"{g['pool']['Away']}@{g['pool']['Home']} -> {dt.datetime.fromtimestamp(g['kick'], CT):%H:%M}"
                                for g in sim.games), flush=True)
    t0 = time.time()

    def sim_now() -> dt.datetime:
        return t_start + dt.timedelta(seconds=(time.time() - t0) * a.speed)

    next_gp, next_cc = t_start, t_start
    cover_posts, events_sim, seen_lines = [], [], 0
    stats = {"cover_ticks": 0, "kneel_moments": 0, "post_final_ticks": 0, "gp_ticks": 0, "gp_errors": 0}
    burst_at = _hm(day, a.burst_at) if a.burst_at and not a.no_burst else None
    burst_thread, burst_res = None, {}

    def stamp_new_events(now_sim: dt.datetime):
        nonlocal seen_lines
        lines = ob.SEND_LOG.read_text().splitlines() if ob.SEND_LOG.exists() else []
        for ln in lines[seen_lines:]:
            try:
                e = json.loads(ln)
            except ValueError:
                continue
            e["sim"] = now_sim.isoformat()
            events_sim.append(e)
            if e.get("event") in ("queued", "dm-queued", "dry-run", "skipped"):
                print(f"[{now_sim:%H:%M:%S}] {e['event']} {e.get('kind')} {e.get('key')}: "
                      + " / ".join(e.get("texts") or [])[:200], flush=True)
        seen_lines = len(lines)

    while True:
        now = sim_now()
        if now >= t_end:
            break
        if now >= next_gp:
            stats["gp_ticks"] += 1
            try:
                gp.tick(dry_run=None, now=now, echo=lambda *_: None)
            except Exception as e:  # noqa: BLE001
                stats["gp_errors"] += 1
                print(f"[{now:%H:%M}] group_posts.tick failed: {e!r}", flush=True)
            stamp_new_events(now)
            next_gp += dt.timedelta(minutes=5)
        if now >= next_cc:
            stats["cover_ticks"] += 1
            ts = now.timestamp()
            rows = []
            for g, snap in sim.snaps(ts):
                if not snap:
                    continue
                if snap["state"] == "post":
                    stats["post_final_ticks"] += 1
                if snap.get("kneeling") and cc.in_window(snap.get("period"), snap.get("clock_s")):
                    stats["kneel_moments"] += 1
                if snap["state"] == "in" and int(snap.get("period") or 0) >= 4:
                    rows.append({"gid": g["gid"], "pool": g["pool"], "snap": snap, "counts": None, "ackbar": None})
            posts = engine.step(ts, rows)
            if posts:
                key = "cover:" + ",".join(f"{p['gid']}#{p['n']}" for p in posts)
                res = ob.post("cover", cc.combine(posts), key, expires_s=cc.EXPIRES_S, quiet=True, now=now,
                              echo=lambda *_: None)
                if res in ("queued", "dry-run", "dup", "paused", "capped", "no-group"):
                    engine.commit(ts, posts)
                snaps = {r["gid"]: r["snap"] for r in rows}
                for p in posts:
                    s = snaps[p["gid"]]
                    cover_posts.append({"gid": p["gid"], "n": p["n"], "kind": p["kind"], "res": res, "why": p["why"],
                                        "sim": now.isoformat(), "state": s["state"], "period": s["period"],
                                        "clock_s": s["clock_s"], "kneeling": s.get("kneeling"), "text": p["text"]})
                stamp_new_events(now)
            next_cc += dt.timedelta(seconds=cc.TICK_HOT)
        if burst_at and now >= burst_at and burst_thread is None:
            live = out / "burst-live.json"
            live.write_text(json.dumps(week12_live(template, sim, now.timestamp())))
            print(f"[{now:%H:%M}] burst of {a.burst_n} asks (waits for the exam lock)", flush=True)
            burst_thread = threading.Thread(target=lambda: burst_res.update(
                burst_locked(live, out, a.burst_n, a.modes.split(","), a.port)), daemon=True)
            burst_thread.start()
        time.sleep(0.05)
    if burst_thread:
        print("sim over; waiting for the burst", flush=True)
        burst_thread.join()
    stamp_new_events(sim_now())
    drill_keys = {e.get("key") for e in events_sim if e.get("key")}
    lg = gp.League(gp.DB(), _hm(day, "06:00"))
    lock = lg.lock_time(lg.open_week())
    mapped = set(ob.member_handles().values())
    report = {
        "stamp": out.name, "speed": a.speed, "window": [t_start.isoformat(), t_end.isoformat()], "sandbox": str(sandbox),
        "fixture_games": [f"{g['pool']['Away']} at {g['pool']['Home']} ({g['pool']['line']:+g}) -> "
                          f"{dt.datetime.fromtimestamp(g['kick'], CT):%H:%M}" for g in sim.games],
        "stats": stats, "cover_posts": cover_posts, "traps": trap_results(),
        "group_posts": [e for e in events_sim if e.get("event") in ("queued", "skipped", "dry-run")],
        "dm_nudges": [e for e in events_sim if str(e.get("event", "")).startswith("dm-")],
        "problems": {"cover": audit_cover(cover_posts),
                     "group": audit_group_posts(events_sim),
                     "nudges": audit_nudges(events_sim, mapped, set(lg.tid.values()), lock)},
        "real_drain": real_drain_check(before, drill_keys),
        "burst": burst_res,
    }
    report["problems"]["traps"] = [t["case"] for t in report["traps"] if not t["ok"]]
    if not report["real_drain"]["ok"]:
        report["problems"]["real_drain"] = ["drill items reached the real outbox/send log"]
    (out / "report.json").write_text(json.dumps(report, indent=1))
    (out / "report.md").write_text(markdown(report))
    print(markdown(report))
    return report


def markdown(r: dict) -> str:
    st = r["stats"]
    L = [f"# Saturday drill {r['stamp']} ({r['speed']}x, {r['window'][0][11:16]}-{r['window'][1][11:16]} CT, sandbox outbox)", "",
         "Replay games: " + "; ".join(r["fixture_games"]), "",
         f"## Close-cover: {len(r['cover_posts'])} post(s)",
         f"- ticks {st['cover_ticks']}, kneel moments in the window {st['kneel_moments']}, ticks with a final on the board "
         f"{st['post_final_ticks']}"]
    for p in r["cover_posts"]:
        L.append(f"- {p['sim'][11:19]} {p['gid']} #{p['n']} {p['kind']} (Q{p['period']} {p['clock_s']}s, {p['res']}; {p['why']}): {p['text']}")
    L += ["", "## Trap moments"] + [f"- {'ok' if t['ok'] else 'FAIL'}: {t['case']} -> {'posted' if t['posted'] else 'no post'}"
                                    for t in r["traps"]]
    gq = [e for e in r["group_posts"] if e.get("event") == "queued" and e.get("kind") != "cover"]
    L += ["", f"## Other group posts: {len(gq)}"] + [f"- {e['sim'][11:16]} {e['kind']} {e['key']}: " + " / ".join(e.get("texts") or [])
                                                    for e in gq]
    sk = [e for e in r["group_posts"] if e.get("event") == "skipped"]
    L += [f"- skipped {e['sim'][11:16]} {e.get('kind')} {e.get('key')} ({e.get('why')})" for e in sk]
    dms = [e for e in r["dm_nudges"] if e.get("event") == "dm-queued"]
    L += ["", f"## Straggler DMs: {len(dms)}"] + [f"- {e['sim'][11:16]} {e.get('key')}: " + " / ".join(e.get("texts") or [])
                                                 for e in dms]
    b = r.get("burst") or {}
    L += ["", "## Burst"]
    if b.get("modes"):
        L.append(f"- build {b.get('sha')}, {b.get('n')} asks; sidecar log {b.get('log')}")
        for mode, s in b["modes"].items():
            L.append(f"- {mode}: p50 {s['p50']}s, p90 {s['p90']}s, max {s['max']}s, errors {s['errors']}; lanes "
                     + ", ".join(f"{k} n={v['n']} p50 {v['p50']}s" for k, v in s["lanes"].items()))
    else:
        L.append(f"- {b.get('error') or 'not run'}")
    rd = r["real_drain"]
    L += ["", f"## Real outbox: {'untouched by the drill' if rd['ok'] else 'DRILL ITEMS LEAKED'} "
              f"({rd['new_real_events']} real send-log events and {rd['new_real_files']} real outbox files appeared meanwhile, "
              f"{rd['from_drill']} from the drill)"]
    probs = {k: v for k, v in r["problems"].items() if v}
    L += ["", "## Problems: " + ("none" if not probs else "")] + [f"- {k}: {x}" for k, v in probs.items() for x in v]
    return "\n".join(L) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="saturday_drill.py")
    ap.add_argument("cmd", choices=["run", "burst", "report"])
    ap.add_argument("path", nargs="?", default="")
    ap.add_argument("--speed", type=float, default=4.0)
    ap.add_argument("--date", default="2026-11-21")
    ap.add_argument("--start", default="06:45")
    ap.add_argument("--end", default="18:15")
    ap.add_argument("--burst-at", default="15:00")
    ap.add_argument("--burst-n", type=int, default=20)
    ap.add_argument("--no-burst", action="store_true")
    ap.add_argument("--modes", default="concurrent,sequential")
    ap.add_argument("--fixture", default=str(FIXTURE))
    ap.add_argument("--port", type=int, default=8808)
    ap.add_argument("--live", default="")
    ap.add_argument("--out", default="")
    ap.add_argument("--n", type=int, default=20)
    a = ap.parse_args(argv)
    if a.cmd == "run":
        rep = run_drill(a)
        return 1 if any(rep["problems"].values()) else 0
    if a.cmd == "burst":
        out = Path(a.out) if a.out else OUT_ROOT / f"burst-{dt.datetime.now(CT):%Y%m%d-%H%M%S}"
        res = burst_locked(Path(a.live), out, a.n, a.modes.split(","), a.port)
        print(json.dumps({k: v for k, v in res.items() if k != "log"}, indent=1))
        return 0 if "error" not in res else 1
    print((Path(a.path) / "report.md").read_text())
    return 0


if __name__ == "__main__":
    sys.exit(main())
