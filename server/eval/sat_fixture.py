"""
Saturday replay fixture for the DM drill (eval/sat_dm.py): EVERY FBS game of one recorded Saturday, served by a
fake ESPN frozen at any moment of that day.

  sat_fixture.py record --date 20261114 [--out DIR]   scoreboard + every game's summary (play-by-play with
                                                      wallclocks, box score, drives), trimmed, gzipped
  sat_fixture.py check  [--fixture DIR]               box score rebuilt from the play-by-play at the final vs
                                                      ESPN's real final box (how close the mid-game boxes are)
  sat_fixture.py fill   --at "2026-11-14 15:00" --cache FILE   write a live_games cache as the poller would hold
                                                      it at that CT moment (uses the live_games on sys.path)

Mid-game box scores aren't recorded by ESPN after the fact, so the fake builds them from the plays shown so far
(passing / rushing / receiving lines, 3rd/4th downs, yards, turnovers) in ESPN's own box-score shape. Finals serve
ESPN's real box. The drill grades against whatever the fake served, so the two always agree.
Data is ESPN public JSON; the fixture lives in the private store only to keep the public repo small.
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import gzip
import json
import re
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import live_replay as R  # noqa: E402  (snapshot / sanitize / plays_of)

CT = ZoneInfo("America/Chicago")
DEFAULT = Path("/Users/Agent/cfb-pickem-private/drills/satdm/fixture-20261114")
ESPN = R.ESPN
PLAY_KEYS = R.PLAY_KEYS


def _ts(s) -> float:
    return R._ts(s)


def trim(s: dict) -> dict:
    hdr = copy.deepcopy(s.get("header") or {})
    for c in (hdr.get("competitions") or [{}])[0].get("competitors") or []:
        t = c.get("team") or {}
        c["team"] = {k: t.get(k) for k in ("id", "location", "name", "abbreviation", "displayName", "shortDisplayName")}
        c.pop("linescores", None)
        c.pop("record", None)
    drives = []
    for d in (s.get("drives") or {}).get("previous") or []:
        drives.append({"id": d.get("id"), "description": d.get("description"), "result": d.get("result"),
                       "displayResult": d.get("displayResult"), "yards": d.get("yards"), "isScore": d.get("isScore"),
                       "offensivePlays": d.get("offensivePlays"), "start": d.get("start"), "end": d.get("end"),
                       "timeElapsed": d.get("timeElapsed"),
                       "team": {k: (d.get("team") or {}).get(k) for k in ("id", "abbreviation", "displayName", "shortDisplayName")},
                       "plays": [{k: p.get(k) for k in PLAY_KEYS if k in p} for p in d.get("plays") or []]})
    box = s.get("boxscore") or {}
    teams = [{"team": {k: (t.get("team") or {}).get(k) for k in ("id", "abbreviation", "displayName", "shortDisplayName")},
              "statistics": [{k: x.get(k) for k in ("name", "displayValue", "label")} for x in t.get("statistics") or []]}
             for t in box.get("teams") or []]
    players = []
    for t in box.get("players") or []:
        players.append({"team": {k: (t.get("team") or {}).get(k) for k in ("id", "abbreviation", "displayName")},
                        "statistics": [{"name": c.get("name"), "labels": c.get("labels"), "athletes": [
                            {"athlete": {k: (a.get("athlete") or {}).get(k) for k in ("displayName", "jersey")},
                             "stats": a.get("stats")} for a in c.get("athletes") or []]} for c in t.get("statistics") or []]})
    sp = [{k: x.get(k) for k in ("id", "type", "text", "awayScore", "homeScore", "period", "clock", "team")}
          for x in s.get("scoringPlays") or []]
    for x in sp:
        x["team"] = {k: (x.get("team") or {}).get(k) for k in ("id", "displayName", "abbreviation")}
    venue = ((s.get("gameInfo") or {}).get("venue") or {})
    pc = [{k: x.get(k) for k in ("details", "overUnder", "spread")} for x in (s.get("pickcenter") or [])[:1]]
    return {"header": hdr, "drives": {"previous": drives}, "scoringPlays": sp, "pickcenter": pc,
            "boxscore": {"teams": teams, "players": players},
            "gameInfo": {"venue": {"fullName": venue.get("fullName"), "address": venue.get("address")}}}


def cmd_record(a) -> int:
    import httpx
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    with httpx.Client(headers=R.UA, timeout=20) as c:
        sb = c.get(f"{ESPN}/scoreboard?dates={a.date}&groups=80&limit=300").json()
        (out / "scoreboard.json.gz").write_bytes(gzip.compress(json.dumps(sb).encode()))
        for e in sb.get("events") or []:
            s = c.get(f"{ESPN}/summary?event={e['id']}").json()
            (out / f"summary_{e['id']}.json.gz").write_bytes(gzip.compress(json.dumps(trim(s)).encode()))
            time.sleep(0.3)
    print(f"recorded {len(sb.get('events') or [])} games to {out}")
    return 0


def load(d: Path = DEFAULT) -> tuple[dict, dict]:
    sb = json.loads(gzip.decompress((d / "scoreboard.json.gz").read_bytes()))
    games = {}
    for f in sorted(d.glob("summary_*.json.gz")):
        games[f.name[8:-8]] = R.sanitize(json.loads(gzip.decompress(f.read_bytes())))
    return sb, games


# ----------------------------------------------------------------------------
# box score from the plays so far
# ----------------------------------------------------------------------------

_SUFFIX = r"(?:,? (?:Jr\.|Sr\.|II|III|IV))?"
_NM = r"([A-Z][A-Za-z'\-]*\.\s?[A-Z][A-Za-z'\-]+(?:[ -][A-Z][A-Za-z'\-]+)*" + _SUFFIX + ")"
_PASS = re.compile(_NM + r" pass (complete|incomplete|intercepted)")
_TO = re.compile(r" to " + _NM)
_RUN = re.compile(_NM + r" (?:run|rush|scrambles?|kneels?)\b")
_SACK = re.compile(_NM + r" sacked\b")
PASS_T = {"Pass Reception", "Passing Touchdown", "Pass Incompletion", "Pass Interception Return", "Interception",
          "Interception Return Touchdown"}
RUSH_T = {"Rush", "Rushing Touchdown", "Sack", "Fumble Recovery (Own)", "Fumble Recovery (Opponent)", "Fumble",
          "Fumble Return Touchdown"}
TO_T = {"Pass Interception Return", "Interception", "Interception Return Touchdown", "Fumble Recovery (Opponent)",
        "Fumble Return Touchdown"}
SKIP_T = {"Timeout", "End Period", "End of Half", "End of Game", "End of Regulation", "Kickoff", "Punt", "Punt Return",
          "Field Goal Good", "Field Goal Missed", "Blocked Field Goal", "Kickoff Return Touchdown", "Extra Point Good",
          "Two-point conversion"}


def _clean(text: str) -> str:
    t = re.sub(r"^\(\d{1,2}:\d{2}\)\s*", "", str(text or ""))
    t = re.sub(r"^(No Huddle-|No Huddle )?(Shotgun|Pistol|Under Center)\s+", "", t, flags=re.I)
    return re.sub(r"#\d{1,2} ", "", t)


def _full(roster: dict, tid: str, abbr: str) -> str:
    import live_games as L
    return roster.get((tid, L._abbr_key(abbr)), {}).get("name") or abbr


def synth_box(summary: dict, plays: list[dict], real_box: dict) -> dict:
    """ESPN-shaped boxscore {teams, players} from `plays` (the plays shown so far, oldest first, with '_off')."""
    import live_games as L
    roster = L._roster({"boxscore": real_box})
    comp = summary["header"]["competitions"][0]
    tinfo = {str(c["team"]["id"]): c["team"] for c in comp["competitors"]}
    P = {tid: {"passing": {}, "rushing": {}, "receiving": {}} for tid in tinfo}
    T = {tid: {"third": [0, 0], "fourth": [0, 0], "pass": 0, "rush": 0, "to": 0, "fd": 0, "comp": [0, 0], "rushes": 0}
         for tid in tinfo}
    for i, p in enumerate(plays):
        ty = (p.get("type") or {}).get("text") or ""
        tid = p.get("_off") or ""
        if tid not in T or ty in SKIP_T:
            continue
        txt = _clean(p.get("text"))
        if "No Play" in txt or ty == "Penalty":
            continue
        y = int(p.get("statYardage") or 0)
        st, en = p.get("start") or {}, p.get("end") or {}
        td = "Touchdown" in ty or "TOUCHDOWN" in txt.upper()
        if ty in TO_T:
            T[tid]["to"] += 1
        down = st.get("down")
        if down in (3, 4) and (ty in PASS_T or ty in RUSH_T):
            k = "third" if down == 3 else "fourth"
            T[tid][k][1] += 1
            nxt_team = next((q.get("_off") for q in plays[i + 1:] if (q.get("type") or {}).get("text") not in SKIP_T | {"Penalty"}), tid)
            if (td and ty not in TO_T) or (en.get("down") == 1 and nxt_team == tid and ty not in TO_T):
                T[tid][k][0] += 1
        if en.get("down") == 1 and ty not in TO_T and ty in PASS_T | RUSH_T and (st.get("down") or 0) >= 1 and not td:
            T[tid]["fd"] += 1
        m = _PASS.search(txt)
        if ty in PASS_T and m:
            qb = _full(roster, tid, m.group(1))
            row = P[tid]["passing"].setdefault(qb, [0, 0, 0, 0, 0])
            row[1] += 1
            T[tid]["comp"][1] += 1
            if m.group(2) == "complete":
                row[0] += 1
                row[2] += y
                T[tid]["comp"][0] += 1
                T[tid]["pass"] += y
                r = _TO.search(txt[m.end():])
                if r:
                    wr = _full(roster, tid, r.group(1))
                    rr = P[tid]["receiving"].setdefault(wr, [0, 0, 0])
                    rr[0] += 1
                    rr[1] += y
                    if td:
                        rr[2] += 1
                if td:
                    row[3] += 1
            elif m.group(2) == "intercepted":
                row[4] += 1
            continue
        m = _SACK.search(txt) if ty == "Sack" else _RUN.search(txt)
        if m and ty in RUSH_T:
            rb = _full(roster, tid, m.group(1))
            rr = P[tid]["rushing"].setdefault(rb, [0, 0, 0])
            rr[0] += 1
            rr[1] += y
            T[tid]["rush"] += y
            T[tid]["rushes"] += 1
            if td and ty not in TO_T:
                rr[2] += 1
    teams, players = [], []
    for tid, t in T.items():
        info = {k: tinfo[tid].get(k) for k in ("id", "abbreviation", "displayName", "shortDisplayName")}
        teams.append({"team": info, "statistics": [
            {"name": "firstDowns", "displayValue": str(t["fd"]), "label": "1st Downs"},
            {"name": "thirdDownEff", "displayValue": f"{t['third'][0]}-{t['third'][1]}", "label": "3rd down efficiency"},
            {"name": "fourthDownEff", "displayValue": f"{t['fourth'][0]}-{t['fourth'][1]}", "label": "4th down efficiency"},
            {"name": "totalYards", "displayValue": str(t["pass"] + t["rush"]), "label": "Total Yards"},
            {"name": "netPassingYards", "displayValue": str(t["pass"]), "label": "Passing"},
            {"name": "completionAttempts", "displayValue": f"{t['comp'][0]}/{t['comp'][1]}", "label": "Comp/Att"},
            {"name": "rushingYards", "displayValue": str(t["rush"]), "label": "Rushing"},
            {"name": "rushingAttempts", "displayValue": str(t["rushes"]), "label": "Rushing Attempts"},
            {"name": "turnovers", "displayValue": str(t["to"]), "label": "Turnovers"}]})

        def rows(d, fmt):
            return [{"athlete": {"displayName": n}, "stats": fmt(v)} for n, v in
                    sorted(d.items(), key=lambda kv: -(kv[1][1] if len(kv[1]) == 3 else kv[1][2]))]
        players.append({"team": info, "statistics": [
            {"name": "passing", "labels": ["C/ATT", "YDS", "AVG", "TD", "INT"],
             "athletes": rows(P[tid]["passing"], lambda v: [f"{v[0]}/{v[1]}", str(v[2]),
                                                             f"{v[2] / v[1]:.1f}" if v[1] else "0.0", str(v[3]), str(v[4])])},
            {"name": "rushing", "labels": ["CAR", "YDS", "AVG", "TD", "LONG"],
             "athletes": rows(P[tid]["rushing"], lambda v: [str(v[0]), str(v[1]),
                                                             f"{v[1] / v[0]:.1f}" if v[0] else "0.0", str(v[2]), ""])},
            {"name": "receiving", "labels": ["REC", "YDS", "AVG", "TD", "LONG"],
             "athletes": rows(P[tid]["receiving"], lambda v: [str(v[0]), str(v[1]),
                                                               f"{v[1] / v[0]:.1f}" if v[0] else "0.0", str(v[2]), ""])}]})
    return {"teams": teams, "players": players}


def _shown(snap: dict) -> list[dict]:
    out = []
    dr = snap["drives"]
    for d in dr["previous"] + ([dr["current"]] if dr.get("current") else []):
        for p in d["plays"]:
            out.append({**p, "_off": str((d.get("team") or {}).get("id") or "")})
    return out


def summary_at(s: dict, t: float) -> dict:
    snap = R.snapshot(s, t)
    st = snap["header"]["competitions"][0]["status"]["type"]["state"]
    if st == "post":
        snap["boxscore"] = s["boxscore"]
    elif st == "in":
        snap["boxscore"] = synth_box(s, _shown(snap), s["boxscore"])
        cur = snap["drives"].get("current")
        if cur:  # a drive in progress: ESPN's running description, no result yet
            ps = [p for p in cur["plays"] if (p.get("type") or {}).get("text") not in SKIP_T | {"Penalty"}]
            yds = sum(int(p.get("statYardage") or 0) for p in ps)
            snap["drives"]["current"] = {**cur, "description": f"{len(ps)} plays, {yds} yards", "result": None,
                                         "displayResult": None, "isScore": False}
    else:
        snap["boxscore"] = {"teams": [], "players": []}
    snap["gameInfo"] = s.get("gameInfo") or {}
    return snap


def scoreboard_at(sb: dict, games: dict, t: float) -> dict:
    """ESPN's scoreboard at t: the recorded events (ranks, venue, broadcasts, odds) with state from the plays."""
    events = []
    for e in sb.get("events") or []:
        s = games.get(e["id"])
        if not s:
            continue
        snap = R.snapshot(s, t)
        e2 = copy.deepcopy(e)
        comp = e2["competitions"][0]
        hc = snap["header"]["competitions"][0]
        comp["status"] = hc["status"]
        score = {c.get("homeAway"): c.get("score") for c in hc["competitors"]}
        for c in comp["competitors"]:
            c["score"] = score.get(c.get("homeAway"), "0") if comp["status"]["type"]["state"] != "pre" else "0"
            c.pop("linescores", None)
        comp.pop("situation", None)
        state = comp["status"]["type"]["state"]
        if state == "pre" and s.get("pickcenter"):  # the market line, as ESPN shows it before kickoff
            comp["odds"] = [dict(s["pickcenter"][0])]
        if state == "in":
            comp.pop("odds", None)  # ESPN hides the market line while a game is live
            ps = _shown(snap)
            if ps:
                end = ps[-1].get("end") or {}
                comp["situation"] = {"possession": (end.get("team") or {}).get("id"),
                                     "downDistanceText": end.get("downDistanceText"),
                                     "lastPlay": {"text": ps[-1].get("text")}}
        events.append(e2)
    return {"events": events, "leagues": sb.get("leagues")}


class Frozen:
    def __init__(self, t: float):
        self.t = t
        self.lock = threading.Lock()

    def now(self) -> float:
        with self.lock:
            return self.t

    def set(self, t: float) -> None:
        with self.lock:
            self.t = t


def serve(sb: dict, games: dict, clock: Frozen, port: int = 0):
    calls = {"scoreboard": 0, "summary": 0}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):  # noqa: N802
            u = urllib.parse.urlparse(self.path)
            q = urllib.parse.parse_qs(u.query)
            if u.path.endswith("/scoreboard"):
                calls["scoreboard"] += 1
                body = scoreboard_at(sb, games, clock.now())
            elif u.path.endswith("/summary") and q.get("event", [""])[0] in games:
                calls["summary"] += 1
                body = summary_at(games[q["event"][0]], clock.now())
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


def at_ct(s: str) -> float:
    return dt.datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=CT).timestamp()


def fill(sb: dict, games: dict, t: float, cache: Path, pool_ids=(), now: float | None = None) -> dict:
    """The live_games cache the poller would hold at t (every game on the board; play-by-play detail for every
    game that has kicked off), stamped with the real `now` so nothing reads as stale."""
    import live_games as L
    now = now or time.time()
    out = {}
    early = min(_ts(e["date"]) for e in sb["events"]) - 3600  # the poller saw every game pregame (odds, TV) first
    pre = L.parse_scoreboard(scoreboard_at(sb, games, early))
    for gid, g in L.parse_scoreboard(scoreboard_at(sb, games, t)).items():
        g = {**{k: v for k, v in (pre.get(gid) or {}).items() if k not in g}, **g}  # the poller's merge
        if g.get("state") in ("in", "post"):
            d = L.parse_summary(summary_at(games[gid], t), gid)
            if d:
                g = {**g, **d, "detail_ts": now}
        out[gid] = {**g, "sb_ts": now, "ts": now}
    data = {"v": 1, "updated_ts": now, "updated_at": dt.datetime.fromtimestamp(now, dt.timezone.utc).isoformat(),
            "mode": "live" if any(g.get("state") == "in" for g in out.values()) else "idle",
            "pool_ids": sorted(map(str, pool_ids)), "games": out}
    L._write(data, cache)
    return data


def cmd_check(a) -> int:
    sb, games = load(Path(a.fixture))
    worst = []
    for gid, s in games.items():
        ps = R.plays_of(s)
        if not ps:
            continue
        snap = summary_at(s, _ts(ps[-1]["wallclock"]) + 1)
        syn = synth_box(s, _shown(snap), s["boxscore"])
        by_id = {str(x["team"]["id"]): x for x in syn["players"]}
        for real_t in s["boxscore"]["players"]:
            syn_t = by_id.get(str(real_t["team"]["id"]))
            if not syn_t:
                continue
            rp = next((c for c in real_t["statistics"] if c["name"] == "passing"), None)
            sp = next((c for c in syn_t["statistics"] if c["name"] == "passing"), None)
            if rp and rp["athletes"] and sp and sp["athletes"]:
                ra, sa = rp["athletes"][0], sp["athletes"][0]
                worst.append((abs(int(ra["stats"][1]) - int(sa["stats"][1])), gid, ra["athlete"]["displayName"],
                              ra["stats"][:2], sa["athlete"]["displayName"], sa["stats"][:2]))
    worst.sort(reverse=True)
    for w in worst[:8]:
        print(w)
    print(f"QB passing yards, synth vs real: median diff {sorted(x[0] for x in worst)[len(worst) // 2]}, n={len(worst)}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("record")
    r.add_argument("--date", default="20261114")
    r.add_argument("--out", default=str(DEFAULT))
    c = sub.add_parser("check")
    c.add_argument("--fixture", default=str(DEFAULT))
    f = sub.add_parser("fill")
    f.add_argument("--fixture", default=str(DEFAULT))
    f.add_argument("--at", required=True)
    f.add_argument("--cache", required=True)
    a = ap.parse_args(argv)
    if a.cmd == "record":
        return cmd_record(a)
    if a.cmd == "check":
        return cmd_check(a)
    sb, games = load(Path(a.fixture))
    d = fill(sb, games, at_ct(a.at), Path(a.cache))
    print(f"{sum(1 for g in d['games'].values() if g.get('state') == 'in')} live, "
          f"{sum(1 for g in d['games'].values() if g.get('state') == 'post')} final")
    return 0


if __name__ == "__main__":
    sys.exit(main())
