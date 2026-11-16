"""
Record / replay harness for game-day live mode (live_games.py), so it can be
tested without waiting for Saturday.

  record   --date 20261114 --ids 401858298,... --out DIR
           saves ESPN's scoreboard + each game's summary (completed games keep
           full play-by-play with wallclock times), trimmed to what the parser reads.
  latency  --data DIR [--pool ids]
           serves the recorded games from a fake ESPN at a virtual clock (14 h of
           games replay in about a minute), runs the REAL Poller against it, and reports how
           long (virtual seconds) each score change, scoring play, injury note and
           QB change took to reach the cache. Model-free.
  ask      --data DIR --group http://127.0.0.1:8791/group --checkpoints default
           freezes the fake ESPN at checkpoints inside the games, syncs the poller,
           asks a side sidecar (pointed at the same fake ESPN + cache) questions
           ("what's the score in X", "did Y's QB get hurt", "who scored that TD",
           "is Z covering") and grades the answers against the replayed state.
           Run it inside the exam lock.
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import gzip
import json
import os
import re
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"}
ESPN = "https://site.api.espn.com/apis/site/v2/sports/football/college-football"


def _ts(s) -> float:
    return dt.datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()


# ----------------------------------------------------------------------------
# trimming + snapshots
# ----------------------------------------------------------------------------

PLAY_KEYS = ("id", "sequenceNumber", "type", "text", "awayScore", "homeScore", "period", "clock", "scoringPlay",
             "wallclock", "statYardage", "start", "end", "isTurnover")


def trim(summary: dict) -> dict:
    """Only what live_games.parse_summary reads (and no future headlines)."""
    hdr = copy.deepcopy(summary.get("header") or {})
    for c in (hdr.get("competitions") or [{}])[0].get("competitors") or []:
        t = c.get("team") or {}
        c["team"] = {k: t.get(k) for k in ("id", "location", "name", "abbreviation", "displayName", "shortDisplayName")}
        c.pop("linescores", None)
        c.pop("record", None)
    drives = []
    for d in (summary.get("drives") or {}).get("previous") or []:
        drives.append({"id": d.get("id"), "team": {"id": (d.get("team") or {}).get("id"),
                                                   "abbreviation": (d.get("team") or {}).get("abbreviation")},
                       "plays": [{k: p.get(k) for k in PLAY_KEYS if k in p} for p in d.get("plays") or []]})
    players = []
    for t in (summary.get("boxscore") or {}).get("players") or []:
        players.append({"team": {"id": (t.get("team") or {}).get("id")},
                        "statistics": [{"name": c.get("name"), "athletes": [
                            {"athlete": {k: (a.get("athlete") or {}).get(k) for k in ("displayName", "jersey")}}
                            for a in c.get("athletes") or []]} for c in t.get("statistics") or []]})
    sp = [{k: s.get(k) for k in ("id", "type", "text", "awayScore", "homeScore", "period", "clock", "team")}
          for s in summary.get("scoringPlays") or []]
    for s in sp:
        s["team"] = {k: (s.get("team") or {}).get(k) for k in ("id", "displayName", "abbreviation")}
    return {"header": hdr, "drives": {"previous": drives}, "scoringPlays": sp, "boxscore": {"players": players}}


def plays_of(summary: dict) -> list[dict]:
    return [p for d in summary["drives"]["previous"] for p in d["plays"]]


def kickoff(summary: dict) -> float:
    ps = [p for p in plays_of(summary) if p.get("wallclock")]
    return _ts(ps[0]["wallclock"]) if ps else 0.0


def end_time(summary: dict) -> float:
    ps = [p for p in plays_of(summary) if p.get("wallclock")]
    return _ts(ps[-1]["wallclock"]) if ps else 0.0


def snapshot(summary: dict, t: float) -> dict:
    """The summary as ESPN would have served it at wallclock t."""
    drives, cur = [], None
    for d in summary["drives"]["previous"]:
        ps = [p for p in d["plays"] if p.get("wallclock") and _ts(p["wallclock"]) <= t]
        if not ps:
            break
        nd = {**d, "plays": ps}
        if len(ps) < len(d["plays"]):
            cur = nd
            break
        drives.append(nd)
    shown = [p for d in drives + ([cur] if cur else []) for p in d["plays"]]
    ids = {p["id"] for p in shown}
    hdr = copy.deepcopy(summary["header"])
    comp = hdr["competitions"][0]
    last = shown[-1] if shown else None
    done = bool(last) and (last.get("type") or {}).get("text") == "End of Game"
    for c in comp["competitors"]:
        c["score"] = str((last or {}).get("homeScore" if c.get("homeAway") == "home" else "awayScore") or 0)
    if not shown:
        comp["status"] = {"period": 0, "displayClock": "0:00",
                          "type": {"state": "pre", "name": "STATUS_SCHEDULED", "completed": False}}
    elif done:
        comp["status"] = {"period": last["period"]["number"], "displayClock": "0:00",
                          "type": {"state": "post", "name": "STATUS_FINAL", "completed": True}}
    else:
        ty = (last.get("type") or {}).get("text")
        name = ("STATUS_HALFTIME" if ty == "End of Half" else "STATUS_END_PERIOD" if ty == "End Period"
                else "STATUS_IN_PROGRESS")
        comp["status"] = {"period": last["period"]["number"], "displayClock": (last.get("clock") or {}).get("displayValue"),
                          "type": {"state": "in", "name": name, "completed": False}}
    out = {"header": hdr, "drives": {"previous": drives, **({"current": cur} if cur else {})},
           "scoringPlays": [s for s in summary["scoringPlays"] if s["id"] in ids],
           "boxscore": summary["boxscore"]}
    return out


def scoreboard(summaries: dict, sb_orig: dict, t: float) -> dict:
    """ESPN's scoreboard at wallclock t for the recorded games (ranks from the
    recorded scoreboard; situation from the last play)."""
    ranks = {}
    for e in sb_orig.get("events") or []:
        for c in e["competitions"][0]["competitors"]:
            ranks[(e["id"], c.get("homeAway"))] = c.get("curatedRank")
    events = []
    for gid, s in summaries.items():
        snap = snapshot(s, t)
        comp = copy.deepcopy(snap["header"]["competitions"][0])
        for c in comp["competitors"]:
            if ranks.get((gid, c.get("homeAway"))):
                c["curatedRank"] = ranks[(gid, c.get("homeAway"))]
        ps = [p for d in snap["drives"]["previous"] + ([snap["drives"]["current"]] if snap["drives"].get("current") else [])
              for p in d["plays"]]
        if ps and comp["status"]["type"]["state"] == "in":
            end = ps[-1].get("end") or {}
            comp["situation"] = {"possession": (end.get("team") or {}).get("id"),
                                 "downDistanceText": end.get("downDistanceText"),
                                 "lastPlay": {"text": ps[-1].get("text")}}
        events.append({"id": gid, "date": dt.datetime.fromtimestamp(kickoff(s) - 300, dt.timezone.utc)
                       .strftime("%Y-%m-%dT%H:%MZ"), "competitions": [comp]})
    return {"events": events}


# ----------------------------------------------------------------------------
# fake ESPN with a virtual clock
# ----------------------------------------------------------------------------


class VClock:
    def __init__(self, t0: float, speed: float = 1.0):
        self.t0, self.speed, self.r0, self.frozen = t0, speed, time.time(), None
        self.lock = threading.Lock()

    def now(self) -> float:
        with self.lock:
            return self.frozen if self.frozen is not None else self.t0 + (time.time() - self.r0) * self.speed

    def freeze(self, t: float) -> None:
        with self.lock:
            self.frozen = t


def serve(summaries: dict, sb_orig: dict, clock: VClock, port: int = 0):
    calls = {"scoreboard": 0, "summary": 0}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):  # noqa: N802
            u = urllib.parse.urlparse(self.path)
            q = urllib.parse.parse_qs(u.query)
            t = clock.now()
            if u.path.endswith("/scoreboard"):
                calls["scoreboard"] += 1
                body = scoreboard(summaries, sb_orig, t)
            elif u.path.endswith("/summary") and q.get("event", [""])[0] in summaries:
                calls["summary"] += 1
                body = snapshot(summaries[q["event"][0]], t)
            else:
                self.send_response(404)
                self.end_headers()
                return
            raw = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, calls


def sanitize(summary: dict) -> dict:
    """ESPN edits some plays after the fact (a kickoff re-stamped half an hour
    later, carrying a score from later in the game). Live, those plays would
    have shown up in order with the score of the moment, so for replay: each
    play's time is at most the next play's (suffix minimum), and scores never
    go down (suffix minimum per team)."""
    ps = [p for p in plays_of(summary) if p.get("wallclock")]
    nxt_t, nxt_a, nxt_h = float("inf"), 10 ** 6, 10 ** 6
    for p in reversed(ps):
        t = min(_ts(p["wallclock"]), nxt_t)
        p["wallclock"] = dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        a, h = min(p.get("awayScore") or 0, nxt_a), min(p.get("homeScore") or 0, nxt_h)
        p["awayScore"], p["homeScore"] = a, h
        nxt_t, nxt_a, nxt_h = t, a, h
    return summary


def load_dir(d: Path) -> tuple[dict, dict]:
    sb = json.loads(gzip.decompress((d / "scoreboard.json.gz").read_bytes()))
    games = {}
    for f in sorted(d.glob("summary_*.json.gz")):
        games[f.name[8:-8]] = sanitize(json.loads(gzip.decompress(f.read_bytes())))
    return sb, games


# ----------------------------------------------------------------------------
# record
# ----------------------------------------------------------------------------


def cmd_record(a) -> int:
    import httpx
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    with httpx.Client(headers=UA, timeout=20) as c:
        sb = c.get(f"{ESPN}/scoreboard?dates={a.date}&groups=80&limit=300").json()
        keep = set(a.ids.split(","))
        sb["events"] = [e for e in sb.get("events") or [] if e["id"] in keep]
        (out / "scoreboard.json.gz").write_bytes(gzip.compress(json.dumps(sb).encode()))
        for gid in keep:
            s = c.get(f"{ESPN}/summary?event={gid}").json()
            (out / f"summary_{gid}.json.gz").write_bytes(gzip.compress(json.dumps(trim(s)).encode()))
            time.sleep(0.5)
    print(f"recorded {len(keep)} games to {out}")
    return 0


# ----------------------------------------------------------------------------
# latency
# ----------------------------------------------------------------------------


def events_of(summary: dict, gid: str) -> list[tuple[str, float, object]]:
    """(kind, wallclock, key) for every score change, scoring play, injury note and QB change."""
    import live_games as L
    ev, last = [], (0, 0)
    for p in plays_of(summary):
        if not p.get("wallclock"):
            continue
        sc = (p.get("awayScore") or 0, p.get("homeScore") or 0)
        if sc != last:
            ev.append(("score", _ts(p["wallclock"]), sc))
            last = sc
        if L._INJ.search(str(p.get("text") or "")):
            for m in L._INJ.finditer(p["text"]):
                ev.append(("injury", _ts(p["wallclock"]), m.group(2)))
    for s in summary["scoringPlays"]:
        w = next((p["wallclock"] for p in plays_of(summary) if p["id"] == s["id"]), None)
        if w:
            ev.append(("scoring_play", _ts(w), s["id"]))
    final = L.parse_summary(summary, gid)
    for team, q in (final.get("qb") or {}).items():
        for c in q.get("changes") or []:
            w = next((p["wallclock"] for p in plays_of(summary)
                      if (p.get("period") or {}).get("number") == c["period"]
                      and (p.get("clock") or {}).get("displayValue") == c["clock"] and c["to"] in (p.get("text") or "")), None)
            if w:
                ev.append(("qb_change", _ts(w), c["to"]))
    return ev


def seen(kind: str, key, g: dict) -> bool:
    if kind == "score":
        return (g.get("away_score") or 0) >= key[0] and (g.get("home_score") or 0) >= key[1]  # or already past it
    if kind == "scoring_play":
        return any(s.get("id") == key for s in g.get("scoring") or [])
    if kind == "injury":
        return any(x["player"] == key for x in g.get("injuries") or [])
    if kind == "qb_change":
        return any(c["to"] == key for q in (g.get("qb") or {}).values() for c in q.get("changes") or [])
    return False


def cmd_latency(a) -> int:
    """Discrete-event replay: the virtual clock jumps to the poller's next due
    time after each pass (so the run takes seconds, and what's measured is the
    poller's own schedule, not how busy this laptop was). Every fetch still goes
    over HTTP to the fake ESPN serving the game as of that virtual moment.
    Not counted: ESPN's own ~6 s CDN cache and ~0.2 s per request."""
    sb, games = load_dir(Path(a.data))
    t0 = min(kickoff(s) for s in games.values()) - 600
    t1 = max(end_time(s) for s in games.values()) + 120
    clock = VClock(t0)
    clock.freeze(t0)
    srv, calls = serve(games, sb, clock)
    os.environ["PICKY_LIVE_BASE"] = f"http://127.0.0.1:{srv.server_port}"
    import importlib
    import live_games as L
    importlib.reload(L)
    cache = Path(a.cache)
    cache.unlink(missing_ok=True)
    pool = set((a.pool or "").split(",")) - {""}
    pending = {gid: events_of(s, gid) for gid, s in games.items()}
    p = L.Poller(clock=clock.now, cache=cache, pool_ids_fn=lambda: list(pool))
    lags: dict[str, list[float]] = {}
    worst: list[tuple[float, str, str, object]] = []
    real0 = time.time()
    while clock.now() < t1:
        wait = p.step()
        now = clock.now()
        for gid, evs in pending.items():
            g = p.games.get(gid) or {}
            keep = []
            for kind, w, key in evs:
                if w <= now and seen(kind, key, g):
                    lag = max(0.0, now - w)
                    tier = "pool" if gid in pool else "ranked" if (g.get("away", {}).get("rank") or g.get("home", {}).get("rank")) else "other"
                    lags.setdefault(f"{kind}/{tier}", []).append(lag)
                    worst.append((lag, gid, kind, key))
                else:
                    keep.append((kind, w, key))
            pending[gid] = keep
        clock.freeze(now + min(wait, 10))
    srv.shutdown()
    missed = {gid: len(e) for gid, e in pending.items() if e}
    rep = {"virtual_hours": round((t1 - t0) / 3600, 2), "real_seconds": round(time.time() - real0, 1),
           "calls": calls, "poller": p.stats,
           "lag_s": {k: {"n": len(v), "p50": round(sorted(v)[len(v) // 2], 1), "max": round(max(v), 1)}
                     for k, v in sorted(lags.items())},
           "missed": missed, "worst": [(round(x[0], 1), x[1], x[2], str(x[3])) for x in sorted(worst, reverse=True)[:5]]}
    print(json.dumps(rep, indent=1))
    if a.report:
        Path(a.report).write_text(json.dumps(rep, indent=1))
    return 0 if not missed else 1


# ----------------------------------------------------------------------------
# ask
# ----------------------------------------------------------------------------


def _find_play(summary: dict, rx: str, period: int | None = None) -> dict:
    for p in plays_of(summary):
        if re.search(rx, p.get("text") or "") and (period is None or (p.get("period") or {}).get("number") == period):
            return p
    raise KeyError(rx)


def checkpoints(games: dict) -> list[dict]:
    """Moments inside last Saturday's games and what Picky must say there.
    Expected facts are computed from the replayed state, not typed in."""
    import live_games as L
    out = []

    def at(gid, rx, after_s=20, period=None):
        p = _find_play(games[gid], rx, period)
        return _ts(p["wallclock"]) + after_s

    def state(gid, t):
        return L.parse_summary(snapshot(games[gid], t), gid)

    def final(gid):
        return L.parse_summary(snapshot(games[gid], 1e12), gid)

    unc = "401858298"
    if unc in games:
        t = at(unc, r"B\.Edwards Jr\. injured on the play")
        s = state(unc, t)
        out.append({"name": "UNC QB hurt (3Q 9:14)", "t": t, "game": unc, "qs": [
            {"q": "did UNC's QB get hurt?", "need": [r"Edwards", r"injur|hurt|went down|banged"],
             "bad": [r"\bhealthy\b", r"\bfine\b"]},
            {"q": "what's the score in the North Carolina game?",
             "need": [rf"\b{s['away_score']}\b", rf"\b{s['home_score']}\b"], "bad": [r"\b16\b.*\b14\b|\b14\b.*\b16\b"]},
        ]})
        t2 = at(unc, r"T\.Burgess pass", after_s=200)
        s2 = state(unc, t2)
        out.append({"name": "UNC backup in", "t": t2, "game": unc, "qs": [
            {"q": "who's playing QB for North Carolina right now?", "need": [r"Burgess"], "bad": []},
        ]})
    tex = "401856885"
    if tex in games:
        t = at(tex, r"D\.Jones rush left for 44 yards", after_s=45)
        s = state(tex, t)
        out.append({"name": "LSU TD, Dilin Jones 44 (3Q 5:52)", "t": t, "game": tex, "qs": [
            {"q": "who scored that LSU touchdown?", "need": [r"Jones", r"\b44\b"], "bad": [r"Raleek|Manning ran"]},
            {"q": "Texas LSU score?", "need": [rf"\b{s['away_score']}\b", rf"\b{s['home_score']}\b"],
             "bad": [r"\b22\b"]},
        ]})
        t = at(tex, r"Raleek Brown 4 Yd pass from Arch Manning", after_s=30, period=4)
        out.append({"name": "Texas walk-off TD (4Q 0:03)", "t": t, "game": tex, "qs": [
            {"q": "who scored the Texas TD at the end?", "need": [r"Brown", r"Manning"], "bad": []},
        ]})
    utsa = "401862827"
    if utsa in games:
        t = at(utsa, r"M\.Reese caught at UTSA02", after_s=40)
        s = state(utsa, t)
        line = 2.5  # pool line, home UTSA +2.5 (all_games week 11)
        m = s["home_score"] + line - s["away_score"]
        side = "UTSA" if m > 0 else "North Texas"
        out.append({"name": "UTSA pool cover mid-3Q", "t": t, "game": utsa, "qs": [
            {"q": "is UTSA covering?", "need": [rf"{side}", rf"\b{L._num(abs(m))}\b|{s['home_score']}"],
             "bad": [r"\b35\b"]},
        ]})
    bois = "401860914"
    if bois in games:
        fin = final(bois)
        sc = [x for x in fin["scoring"]]
        mid = sorted(_ts(p["wallclock"]) for p in plays_of(games[bois]) if p.get("wallclock"))
        t = mid[len(mid) // 2]
        s = state(bois, t)
        out.append({"name": "Boise mid-game score", "t": t, "game": bois, "qs": [
            {"q": "what's the score of the Boise game?", "need": [rf"\b{s['away_score']}\b", rf"\b{s['home_score']}\b"],
             "bad": [r"\b46\b", r"\b41\b"] if (s["away_score"], s["home_score"]) != (46, 41) else []},
        ]})
    return out


def grade(answer: str, q: dict) -> tuple[bool, list[str]]:
    why = [f"missing /{r}/" for r in q["need"] if not re.search(r, answer or "", re.I)]
    why += [f"has /{r}/" for r in q.get("bad") or [] if re.search(r, answer or "", re.I)]
    return not why, why


def cmd_ask(a) -> int:
    import httpx
    sb, games = load_dir(Path(a.data))
    clock = VClock(0)
    clock.freeze(0)
    srv = None
    if a.espn_port:
        srv, _ = serve(games, sb, clock, a.espn_port)
    os.environ["PICKY_LIVE_BASE"] = f"http://127.0.0.1:{a.espn_port}"
    os.environ["PICKY_LIVE_CACHE"] = a.cache
    import importlib
    import live_games as L
    importlib.reload(L)
    pool = set((a.pool or "").split(",")) - {""}
    p = L.Poller(cache=Path(a.cache), pool_ids_fn=lambda: list(pool))
    sys.path.insert(0, str(HERE))
    import run as exam  # noqa: E402 - for the member handle map + group secret
    handle = exam.group_handles().get(a.persona) or ""
    secret = os.environ.get("PICKY_GROUP_SECRET") or os.popen(
        f"security find-generic-password -a drake -s {exam.GROUP_SECRET_SERVICE} -w").read().strip()
    results = []
    only = set((a.only or "").split(",")) - {""}
    with httpx.Client(timeout=httpx.Timeout(connect=10, read=400, write=10, pool=10)) as c:
        for i, cp in enumerate(checkpoints(games)):
            if only and str(i) not in only:
                continue
            clock.freeze(cp["t"])
            p.next_sb, p.next_detail = 0.0, {}
            p.step()
            synced = time.time()
            for j, q in enumerate(cp["qs"]):
                t0 = time.time()
                r = c.post(a.group, headers={"X-Picky-Group-Secret": secret},
                           json={"handle": handle, "text": q["q"], "chat": f"live-replay-{a.tag}-{i}-{j}", "dm": True})
                ans = (r.json().get("reply") or "") if r.status_code == 200 else f"[HTTP {r.status_code}]"
                ok, why = grade(ans, q)
                rec = {"checkpoint": cp["name"], "q": q["q"], "a": ans, "ok": ok, "why": why,
                       "reply_s": round(time.time() - t0, 1), "data_age_at_reply_s": round(time.time() - synced, 1)}
                results.append(rec)
                print(json.dumps(rec), flush=True)
                p.next_sb, p.next_detail = 0.0, {}  # the next question gets freshly synced data too
                p.step()
                synced = time.time()
    if srv:
        srv.shutdown()
    n = sum(r["ok"] for r in results)
    lat = sorted(r["reply_s"] for r in results)
    summ = {"passed": n, "total": len(results), "median_reply_s": lat[len(lat) // 2] if lat else None,
            "max_reply_s": lat[-1] if lat else None}
    print(json.dumps(summ))
    if a.report:
        Path(a.report).write_text(json.dumps({"summary": summ, "results": results}, indent=1))
    return 0 if n == len(results) else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("record")
    r.add_argument("--date", required=True)
    r.add_argument("--ids", required=True)
    r.add_argument("--out", required=True)
    lt = sub.add_parser("latency")
    lt.add_argument("--data", required=True)
    lt.add_argument("--pool", default="")
    lt.add_argument("--cache", default="/private/tmp/picky-live-run/latency_cache.json")
    lt.add_argument("--report", default="")
    k = sub.add_parser("ask")
    k.add_argument("--data", required=True)
    k.add_argument("--group", required=True)
    k.add_argument("--espn-port", type=int, default=8799)
    k.add_argument("--cache", default="/private/tmp/picky-live-run/live_games.json")
    k.add_argument("--pool", default="")
    k.add_argument("--persona", default="Chuck P")
    k.add_argument("--tag", default=dt.datetime.now().strftime("%H%M"))
    k.add_argument("--only", default="")
    k.add_argument("--report", default="")
    a = ap.parse_args(argv)
    return {"record": cmd_record, "latency": cmd_latency, "ask": cmd_ask}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
