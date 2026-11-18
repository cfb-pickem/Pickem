"""
Saturday DM drill: Charlie's game-day texts to Picky, answered through the exact DM path his handle takes (side
sidecar, POST /group dm=true with his mapped handle, X-Picky-Test), on a recorded Saturday frozen at several moments.
Graded in code against the ESPN data the fake served; Opus (offline) judges only tone and on-point-ness.

  sat_dm.py run    --wt <worktree> --tag <tag> [--only id,id] [--clump serial|combined] [--port 8812]
                   inside /tmp/picky-exam.lock (re-execs itself under lockf); refuses Sat 10:00-24:00 CT.
  sat_dm.py grade  <tag>          re-grade saved answers (code checks)
  sat_dm.py judge  <tag>          offline Opus tone/on-point judge -> judge.json
  sat_dm.py report <tag> [<tag2>] per-category pass rate, p50/p90 latency, one-text compliance (before/after)

Answers (they quote league members' records) go to cfb-pickem-private/drills/satdm/<tag>/, never the repo.
Fixture: eval/sat_fixture.py (Saturday 2026-11-14, all 55 FBS games). Items: eval/sat_dm_items.py.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import signal
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
CT = ZoneInfo("America/Chicago")
PRIVATE = Path("/Users/Agent/cfb-pickem-private")
OUT = PRIVATE / "drills" / "satdm"
PY = "/Users/Agent/cfb-pickem/server/.venv/bin/python3"
LOCK = "/tmp/picky-exam.lock"
DAY = "2026-11-14"
AT_GAME = "401856885"  # the drill puts Charlie at Texas at LSU
POOL = {"401858301": {"away": "Wake Forest", "home": "SMU", "line": 1.5},
        "401860914": {"away": "Oregon State", "home": "Boise State", "line": -13.5},
        "401862827": {"away": "North Texas", "home": "UTSA", "line": 2.5},
        "401864537": {"away": "North Dakota State", "home": "Hawai'i", "line": 6.5}}
CHUCK = "Chuck P"
CHUCK_PICKS = {"401858301": "away", "401860914": "away", "401862827": "home", "401864537": "away"}
MAX_CHARS, DETAIL_CHARS = 300, 700


def _keychain(service: str) -> str:
    return subprocess.run(["security", "find-generic-password", "-a", "drake", "-s", service, "-w"],
                          capture_output=True, text=True).stdout.strip()


def charlie_handle() -> str:
    members = json.loads((PRIVATE / "picky_group_members.json").read_text())["members"]
    return next(m["handles"][0] for m in members if m.get("team") == CHUCK)


# ----------------------------------------------------------------------------
# truth (raw ESPN JSON from the fixture at t; independent of live_games' parser)
# ----------------------------------------------------------------------------

class Truth:
    def __init__(self, fixture: Path):
        sys.path.insert(0, str(HERE))
        import sat_fixture as F
        self.F = F
        self.sb, self.games = F.load(fixture)
        self.orig = {e["id"]: e for e in self.sb["events"]}
        self._season = None

    def t(self, at: str) -> float:
        return self.F.at_ct(f"{DAY} {at}")

    def game(self, gid: str, at: str) -> dict:
        sb = self.F.scoreboard_at({"events": [self.orig[gid]]}, self.games, self.t(at))
        e = sb["events"][0]
        c = e["competitions"][0]
        out = {"id": gid, "state": c["status"]["type"]["state"], "period": c["status"].get("period"),
               "clock": c["status"].get("displayClock"),
               "tv": [n for b in c.get("broadcasts") or [] for n in b.get("names") or []]}
        for x in c["competitors"]:
            t = x["team"]
            r = (x.get("curatedRank") or {}).get("current")
            out[x["homeAway"]] = {"id": str(t["id"]), "loc": t.get("location"), "abbr": t.get("abbreviation"),
                                  "short": t.get("shortDisplayName"), "name": t.get("name"),
                                  "score": int(x.get("score") or 0), "rank": r if r and r <= 25 else None}
        odds = (self.games[gid].get("pickcenter") or [{}])[0]
        out["odds"] = {"details": odds.get("details"), "ou": odds.get("overUnder"), "spread": odds.get("spread")}
        out["situation"] = c.get("situation") or {}
        return out

    def summary(self, gid: str, at: str) -> dict:
        return self.F.summary_at(self.games[gid], self.t(at))

    def all_games(self, at: str) -> list[dict]:
        return [self.game(g, at) for g in self.orig]

    # league standings "if it ended now": weeks <= 10 final + this week's pool games at t (live = current score)
    def season(self) -> dict:
        if self._season is None:
            import httpx
            key = _keychain("CFB_PICKEM__SUPABASE_SERVICE_KEY")
            h = {"apikey": key, "Authorization": f"Bearer {key}"}
            base = "https://vopdioszofwdkwnujtiq.supabase.co/rest/v1/"
            with httpx.Client(headers=h, timeout=30) as c:
                teams = c.get(base + "teams?select=team_id,team_name&is_exhibition=is.false").json()
                games = c.get(base + "all_games?select=GameId,Away,Home,AwayPts,HomePts,line,week,tiebreaker,Status"
                                     "&cfb_season=eq.2026&picked=eq.true&week=lte.11").json()
                ids = [g["GameId"] for g in games]
                picks = []
                for i in range(0, len(ids), 60):
                    off = 0
                    while True:
                        chunk = c.get(base + f"picks?select=team_id,game_id,pick&game_id=in.({','.join(map(str, ids[i:i + 60]))})"
                                             f"&order=id.asc&offset={off}&limit=1000").json()
                        picks += chunk
                        if len(chunk) < 1000:
                            break
                        off += 1000
            self._season = {"teams": {t["team_id"]: t["team_name"] for t in teams}, "games": games, "picks": picks}
        return self._season

    def standings(self, at: str) -> list[dict]:
        S = self.season()
        res = {}
        for g in S["games"]:
            if g.get("tiebreaker"):
                continue
            if g["week"] == 11 and str(g["GameId"]) in POOL:
                tg = self.game(str(g["GameId"]), at)
                if tg["state"] == "pre":
                    continue
                ap, hp = tg["away"]["score"], tg["home"]["score"]
            elif g["Status"] == "final" and g["week"] <= 10:
                ap, hp = g["AwayPts"], g["HomePts"]
            else:
                continue
            m = float(hp) + float(g["line"] or 0) - float(ap)
            res[g["GameId"]] = (g["Home"] if m > 0 else g["Away"])
        rec = {n: [0, 0] for n in S["teams"].values()}
        for p in S["picks"]:
            n = S["teams"].get(p["team_id"])
            if n is None or p["game_id"] not in res or not p.get("pick"):
                continue
            rec[n][0 if p["pick"] == res[p["game_id"]] else 1] += 1
        rows = sorted(({"team": n, "w": w, "l": l} for n, (w, l) in rec.items()), key=lambda r: -r["w"])
        for r in rows:
            r["rank"] = 1 + sum(1 for x in rows if x["w"] > r["w"])
            r["tied"] = sum(1 for x in rows if x["w"] == r["w"]) > 1
        return rows

    def week(self, team: str, at: str) -> tuple[int, int, list]:
        """(W, L) on this week's finals + [(game, covering?)] for live picks, for CHUCK only (fixture picks)."""
        w = lo = 0
        live = []
        for gid, side in CHUCK_PICKS.items():
            g = self.game(gid, at)
            if g["state"] == "pre":
                continue
            m = g["home"]["score"] + POOL[gid]["line"] - g["away"]["score"]
            ok = (m > 0) == (side == "home") if m else side == "away"
            if g["state"] == "post":
                w, lo = (w + 1, lo) if ok else (w, lo + 1)
            else:
                live.append((g, side, ok))
        return w, lo, live


# ----------------------------------------------------------------------------
# checks
# ----------------------------------------------------------------------------

ALIAS = {"Ohio State": ["Buckeyes", "OSU", "tOSU"], "Alabama": ["Bama", "Crimson Tide"], "Georgia": ["UGA", "Dawgs"],
         "Notre Dame": ["ND", "Irish"], "Oregon State": ["Oregon St", "OSU", "Beavers"], "Boise State": ["Boise"],
         "Kentucky": ["UK", "Wildcats"], "Texas Tech": ["TTU", "Red Raiders"], "Oklahoma State": ["Oklahoma St", "OK State", "Okie State", "OKST"],
         "LSU": ["Tigers"], "Texas": ["Longhorns", "Horns"], "Tennessee": ["Vols", "Tenn"], "Texas A&M": ["A&M", "Aggies", "TAMU"],
         "Miami": ["Canes", "Hurricanes"], "Penn State": ["PSU", "Penn St"], "South Carolina": ["SC", "Gamecocks", "South Carolina"],
         "Arkansas": ["Razorbacks", "Hogs"], "Florida": ["Gators", "UF"], "UTSA": ["Roadrunners"], "North Texas": ["UNT", "Mean Green"],
         "Iowa State": ["Iowa St", "ISU", "Cyclones"], "Cincinnati": ["Cincy", "Bearcats"], "Wake Forest": ["Wake"],
         "North Dakota State": ["NDSU", "Bison"], "Hawai'i": ["Hawaii", "Rainbow Warriors"], "Utah": ["Utes"],
         "Arizona": ["Wildcats", "Zona"], "Indiana": ["IU", "Hoosiers"], "USC": ["Trojans"], "BYU": ["Cougars"],
         "Baylor": ["Bears"], "Missouri": ["Mizzou", "Tigers"], "Vanderbilt": ["Vandy", "Commodores"],
         "Northwestern": ["NU", "Wildcats"], "Duke": ["Blue Devils"], "Michigan": ["Wolverines"], "Oregon": ["Ducks"],
         "Purdue": ["Boilermakers"], "Iowa": ["Hawkeyes"], "Minnesota": ["Gophers"], "Louisville": ["Cardinals"],
         "North Carolina": ["UNC", "Tar Heels"], "SMU": ["Mustangs"]}


def names(t: dict) -> list[str]:
    out = {t["loc"], t["short"], t["abbr"]} | set(ALIAS.get(t["loc"], []))
    return [n for n in out if n]


def has_team(a: str, t: dict) -> bool:
    return any(re.search(rf"(?<![\w']){re.escape(n)}(?![\w])", a, re.I if len(n) > 4 else 0) for n in names(t))


def nums(a: str) -> list[int]:
    return [int(x) for x in re.findall(r"(?<![\d.])\d{1,3}(?![\d.]|\.\d)", a)]


def has_score(a: str, g: dict) -> bool:
    x, y = g["away"]["score"], g["home"]["score"]
    if x == y == 0:
        return bool(re.search(r"\b0-0\b|\b0 to 0\b|scoreless|nothing-nothing|no score|nobody has scored|tied at 0", a, re.I))
    ns = nums(a)
    return (x in ns and y in ns) if x != y else (ns.count(x) >= 2 or bool(re.search(rf"tied (at )?{x}\b|{x}-all|{x} all", a)))


def stat_val(box: dict, team_abbr: str | None, player: str | None, cat: str, idx: int):
    for t in box.get("players") or []:
        if team_abbr and t["team"].get("abbreviation") != team_abbr:
            continue
        for c in t["statistics"]:
            if c["name"] != cat:
                continue
            for a in c["athletes"]:
                if player is None or a["athlete"]["displayName"] == player:
                    return a["athlete"]["displayName"], a["stats"][idx]
    return None, None


def team_stat(box: dict, abbr: str, name: str):
    for t in box.get("teams") or []:
        if t["team"].get("abbreviation") == abbr:
            for s in t["statistics"]:
                if s["name"] == name:
                    return s["displayValue"]
    return None


def num_in(a: str, v) -> bool:
    v = str(v)
    if re.fullmatch(r"\d+-\d+", v):
        x, y = v.split("-")
        return bool(re.search(rf"\b{x}\s*(?:-|of|/|for|out of)\s*{y}\b", a))
    if re.fullmatch(r"\d+/\d+", v):
        x, y = v.split("/")
        return bool(re.search(rf"\b{x}\s*(?:-|of|/|for|out of)\s*{y}\b", a))
    return v.lstrip("-") in [str(n) for n in nums(a)] or (v.startswith("-") and v in a)


def upsets(T: Truth, at: str) -> list[dict]:
    out = []
    for g in T.all_games(at):
        if g["state"] == "pre":
            continue
        for s, o in (("away", "home"), ("home", "away")):
            r, ro = g[s]["rank"], g[o]["rank"]
            if r and g[s]["score"] < g[o]["score"] and (ro is None or ro > r):
                out.append(g)
    return out


def check(T: Truth, it: dict, spec: tuple, a: str) -> tuple[bool, str]:
    kind, args = spec[0], spec[1:]
    at = it["at"]
    if kind == "not_empty" or kind == "warm":
        return bool(a.strip()), ""
    if kind == "contains":
        return bool(re.search(args[0], a, re.I)), f"needs /{args[0]}/"
    if kind == "score":
        g = T.game(args[0], at)
        if g["state"] == "pre":
            ok = bool(re.search(r"hasn'?t|not started|kick|starts?|tonight|later|\d:\d\d", a, re.I))
            return ok, "pregame: should say it hasn't kicked off"
        return has_score(a, g), f"needs {g['away']['loc']} {g['away']['score']} - {g['home']['loc']} {g['home']['score']}"
    if kind == "leader":
        g = T.game(args[0], at)
        lead = g["away"] if g["away"]["score"] > g["home"]["score"] else g["home"]
        return has_team(a, lead), f"leader {lead['loc']}"
    if kind == "final_word":
        return bool(re.search(r"\bfinal\b|\bwon\b|\bbeat\b|\bwin\b|\bwins\b|\bheld on\b", a, re.I)), "say it's final/won"
    if kind == "names_live":
        live = [g for g in T.all_games(at) if g["state"] == "in"]
        n = sum(1 for g in live if has_team(a, g["away"]) or has_team(a, g["home"]))
        return n >= args[0], f"named {n} live games"
    if kind in ("upsets", "upsets_any"):
        ups = upsets(T, at)
        if not ups:
            return bool(re.search(r"\bno\b|none|nothing|chalk|holding|quiet", a, re.I)), "no upsets: should say none"
        n = sum(1 for g in ups if has_team(a, g["away"]) and has_team(a, g["home"]) or
                any(has_team(a, g[s]) for s in ("away", "home") if g[s]["rank"]))
        need = 1 if kind == "upsets_any" else min(2, len(ups))
        return n >= need, f"named {n}/{len(ups)} upsets: " + ", ".join(f"{g['away']['loc']}-{g['home']['loc']}" for g in ups)
    if kind == "cover":
        gid = args[0]
        g = T.game(gid, at)
        p = POOL[gid]
        m = g["home"]["score"] + p["line"] - g["away"]["score"]
        cov = g["home"] if m > 0 else g["away"]
        oth = g["away"] if cov is g["home"] else g["home"]
        neg = any(re.search(rf"{re.escape(n)}\W+(?:is |are |'s )?(?:covering|cover\b)", a, re.I) and
                  not re.search(rf"{re.escape(n)}\W+(?:is |are |'s )?not covering", a, re.I) for n in names(oth))
        cov_ok = has_team(a, cov) and re.search(r"cover", a, re.I) and not neg
        return bool(cov_ok), f"{cov['loc']} covering (margin {abs(m)})"
    if kind == "cover_need":
        return check(T, it, ("cover", args[0]), a)
    if kind == "my_picks_live":
        w, lo, live = T.week(CHUCK, at)
        bad = [g[side]["loc"] for g, side, ok in live if not has_team(a, g[side])]
        return not bad and bool(re.search("cover", a, re.I)), f"live picks to mention: {bad}"
    if kind == "my_week":
        w, lo, live = T.week(CHUCK, at)
        finals = [(gid, side) for gid, side in CHUCK_PICKS.items() if T.game(gid, at)["state"] == "post"]
        per_pick = all(has_team(a, T.game(gid, at)[side]) for gid, side in finals) and bool(
            re.search(r"\b(won|covered|cashed|hit|lost|missed)\b", a, re.I))  # each finished pick graded by name
        ok = (f"{w}-{lo}" in a or w + lo == 0 or per_pick) and all(has_team(a, g[side]) for g, side, _ in live)
        return ok, f"week {w}-{lo} + live picks {[g[s]['loc'] for g, s, _ in live]}"
    if kind == "my_place":
        r = next(x for x in T.standings(at) if x["team"] == CHUCK)
        o = {1: "1st|first", 2: "2nd|second", 3: "3rd|third"}.get(r["rank"], f"{r['rank']}th")
        return bool(re.search(rf"\b({o})\b", a, re.I)), f"rank {r['rank']} ({r['w']}-{r['l']}{' tied' if r['tied'] else ''})"
    if kind == "leader_now":
        rows = T.standings(at)
        lead = [r["team"] for r in rows if r["rank"] == 1]
        you = CHUCK in lead and re.search(r"\byou('re| are|'d be)?\b[^.]{0,30}\b(first|1st|lead)", a, re.I)
        return all(n.lower() in a.lower() for n in lead if n != CHUCK or not you), f"leader(s) {lead}"
    if kind == "ackbar":
        r = next(x for x in T.standings(at) if x["team"] == "Ackbar")
        return "ackbar" in a.lower() and bool(re.search(r"\d", a)), f"Ackbar {r['w']}-{r['l']} rank {r['rank']}"
    if kind == "tv":
        g = T.game(args[0], at)
        tv = [n.replace(" Net", "") for n in g["tv"]]
        return any(n.lower() in a.lower() for n in tv), f"tv {g['tv']}"
    if kind == "pstat":
        gid, player, cat, idx = args
        box = T.summary(gid, at)["boxscore"]
        _, v = stat_val(box, None, player, cat, idx)
        if v is None:
            return bool(re.search(r"hasn'?t|no stats|not yet|didn'?t|0\b", a, re.I)), f"{player}: no line yet"
        last = player.split(" ")[-1] if not player.split(" ")[-1] in ("Jr.", "III", "II") else player.split(" ")[-2]
        ok = num_in(a, v.split("/")[0] if cat == "passing" and idx == 0 else v) and (last.lower() in a.lower() or idx == 4)
        return ok, f"{player} {cat}[{idx}]={v}"
    if kind == "top":
        gid, abbr, cat = args
        box = T.summary(gid, at)["boxscore"]
        n, v = stat_val(box, abbr, None, cat, 1)
        return bool(n) and n.split(" ")[-1].lower() in a.lower() and num_in(a, v), f"top {cat} {n} {v}"
    if kind == "tstat":
        gid, abbr, stat = args
        v = team_stat(T.summary(gid, at)["boxscore"], abbr, stat)
        return v is not None and num_in(a, v), f"{abbr} {stat}={v}"
    if kind == "drive":
        s = T.summary(args[0], at)
        dr = s["drives"].get("current") or (s["drives"]["previous"] or [None])[-1]
        if not dr:
            return False, "no drive"
        prev = s["drives"]["previous"][-1] if s["drives"]["previous"] else None
        cands = [d for d in (dr, prev) if d]
        ok = False
        for d in cands:
            res = (d.get("displayResult") or d.get("result") or "").lower()
            words = {"touchdown": r"touchdown|\btd\b|punched|scored", "field goal": r"field goal|\bfg\b",
                     "punt": r"punt", "interception": r"intercept|picked|\bint\b", "fumble": r"fumble",
                     "downs": r"downs", "end of half": r"half", "safety": "safety", "missed fg": r"miss"}
            rx = next((v for k, v in words.items() if k in res), None)
            yds = re.search(r"(\d+) yards", d.get("description") or "")
            ok = ok or bool(rx and re.search(rx, a, re.I)) or bool(yds and yds.group(1) in [str(n) for n in nums(a)])
        return ok, "drive: " + " | ".join(f"{(d.get('team') or {}).get('abbreviation')} {d.get('description')} {d.get('displayResult')}" for d in cands)
    if kind == "market_line":
        g = T.game(args[0], at)
        sp = g["odds"]["details"] or ""
        m = re.search(r"(-?\d+(?:\.\d)?)$", sp)
        num = m.group(1).lstrip("-") if m else "?"
        return num in a, f"market {sp}"
    if kind == "last_score":
        s = T.summary(args[0], at)
        sp = s["scoringPlays"]
        if not sp:
            return bool(re.search(r"no (one|body)|nobody|no points|scoreless|0-0", a, re.I)), "no scores yet"
        last = sp[-1]
        team = last["team"].get("displayName") or ""
        ty = (last.get("type") or {}).get("text", "").lower()
        rx = "touchdown|td|ran|run|pass|caught|catch" if "touchdown" in ty else "field goal|fg|kick" if "field goal" in ty else "safety"
        tok = team.split(" ")[0]
        who = re.findall(r"[A-Z][a-z]+(?: [A-Z][a-z]+)*", last.get("text") or "")
        named = any(w.split(" ")[-1].lower() in a.lower() for w in who if len(w) > 3)
        return (tok.lower() in a.lower() or named) and bool(re.search(rx, a, re.I)), f"last score: {last.get('text')}"
    if kind == "possession":
        g = T.game(args[0], at)
        pid = str((g["situation"] or {}).get("possession") or "")
        team = g["away"] if pid == g["away"]["id"] else g["home"] if pid == g["home"]["id"] else None
        return bool(team) and has_team(a, team), f"ball: {team and team['loc']}"
    if kind == "at_game_named":
        g = T.game(AT_GAME, at)
        return has_team(a, g["away"]) or has_team(a, g["home"]), "should talk about TEX@LSU"
    if kind == "total":
        g = T.game(args[0], at)
        tot = g["away"]["score"] + g["home"]["score"]
        ou = g["odds"]["ou"]
        return str(tot) in [str(n) for n in nums(a)] and (ou is None or f"{ou:g}" in a), f"total {tot} vs o/u {ou}"
    if kind in ("cfp_rank", "cfp_top"):
        c = json.loads((Path("/Users/Agent/cfb-pickem/server/cache/slate_intel.json")).read_text()).get("cfp") or {}
        top = c.get("top") or []
        if kind == "cfp_top":
            n = sum(1 for r in top[:args[0]] if r["team"].lower() in a.lower())
            return n >= args[0] - 1, f"top {[r['team'] for r in top[:args[0]]]}"
        r = next((x for x in top if x["team"] == args[0]), None)
        if not r:
            return bool(re.search(r"unranked|not ranked|outside", a, re.I)), f"{args[0]} unranked"
        return bool(re.search(rf"(?:#|No\. ?|number |ranked )\s*{r['rank']}\b|\b{r['rank']}(?:st|nd|rd|th)\b", a, re.I)), \
            f"{args[0]} CFP #{r['rank']}"
    return False, f"unknown check {kind}"


def grade_item(T: Truth, it: dict, row: dict) -> dict:
    a = row.get("reply") or ""
    res = [(spec[0],) + check(T, it, spec, a) for spec in it["checks"]] if a else [("reply", False, "no reply")]
    cap = DETAIL_CHARS if it.get("detail") else MAX_CHARS
    if it.get("clump"):  # one text answering several stacked-up texts: +100 chars per extra text, max 500
        cap = min(500, MAX_CHARS + 100 * (len(it["clump"]) - 1))
    one = row.get("n_replies", 1) == 1 and bool(a) and not row.get("image")
    short = len(a) <= cap
    return {"facts_ok": all(r[1] for r in res), "fails": [f"{r[0]}: {r[2]}" for r in res if not r[1]],
            "one_text": one, "short": short, "chars": len(a),
            "pass": all(r[1] for r in res) and one and short}


# ----------------------------------------------------------------------------
# run
# ----------------------------------------------------------------------------

def restamp(live: Path, stop: threading.Event) -> None:
    while not stop.wait(4):
        try:
            d = json.loads(live.read_text())
            now = time.time()
            for g in d["games"].values():
                g["ts"] = g["sb_ts"] = now
                if g.get("detail_ts"):
                    g["detail_ts"] = now
            d["updated_ts"] = now
            tmp = live.with_suffix(".tmp")
            tmp.write_text(json.dumps(d))
            os.replace(tmp, live)
        except (OSError, ValueError, KeyError):
            pass


def run(a) -> int:
    sys.path.insert(0, str(HERE.parent))
    import freeze  # game day: no exams/side sidecars Sat 10:00-24:00 CT (review R9); runs Friday as a final gate
    freeze.exam_guard("eval/sat_dm.py")
    if os.environ.get("PICKY_SATDM_LOCKED") != "1":
        env = dict(os.environ, PICKY_SATDM_LOCKED="1")
        return subprocess.call(["/usr/bin/lockf", "-t", "43200", LOCK, PY, "-I", str(Path(__file__).resolve())] + sys.argv[1:],
                               env=env)
    import httpx
    wt = Path(a.wt).resolve()
    sys.path.insert(0, str(wt / "server"))  # the worktree's live_games fills the cache (its parser is under test)
    import live_games as L  # noqa: F401
    sys.path.insert(0, str(HERE))
    import sat_dm_items as I
    T = Truth(Path(a.fixture))
    out = OUT / a.tag
    out.mkdir(parents=True, exist_ok=True)
    rd = Path(f"/private/tmp/picky-satdm-run/{a.tag}")
    if rd.exists():
        shutil.rmtree(rd)
    rd.mkdir(parents=True)
    clock = T.F.Frozen(T.t("12:00"))
    srv, calls = T.F.serve(T.sb, T.games, clock, a.fake_port)
    base = f"http://127.0.0.1:{a.fake_port}"
    clock_file = rd / "clock"
    at_file = rd / "gameday.json"
    at_file.write_text(json.dumps({CHUCK: {"date": DAY, "at": AT_GAME, "short": True}}))
    live = rd / "live_games.json"
    items = [i for i in I.ITEMS if not a.only or i["id"] in a.only.split(",")]
    order = sorted(items, key=lambda i: i["at"])
    first = order[0]["at"]
    clock_file.write_text(str(T.t(first)))
    T.F.fill(T.sb, T.games, T.t(first), live, POOL.keys())
    stop = threading.Event()
    threading.Thread(target=restamp, args=(live, stop), daemon=True).start()
    env = dict(os.environ, PICKY_LIVE_BASE=base, PICKY_ESPN_SUMMARY=base + "/summary?event=", PICKY_PIN_WEEK="11",
               PICKY_REPLAY_CLOCK=str(clock_file), PICKY_GAMEDAY_FILE=str(at_file))
    env.pop("PICKY_SATDM_LOCKED", None)
    log = open(out / "side.log", "w")
    side = subprocess.Popen(["/bin/zsh", str(wt / "server/eval/genius/side.sh"), str(wt), str(a.port), str(rd)],
                            stdout=log, stderr=subprocess.STDOUT, start_new_session=True, env=env)
    hdr = {"X-Picky-Group-Secret": _keychain("CFB_PICKEM__PICKY_GROUP_SECRET"),
           "X-Picky-Test": _keychain("CFB_PICKEM__TEST_TOKEN")}
    handle = charlie_handle()
    url = f"http://127.0.0.1:{a.port}/group"
    rows = []
    try:
        for _ in range(150):
            try:
                if httpx.get(f"http://127.0.0.1:{a.port}/health", timeout=2).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            if side.poll() is not None:
                raise RuntimeError(f"side sidecar exited rc={side.returncode}")
            time.sleep(2)
        cl = httpx.Client(timeout=httpx.Timeout(connect=10, read=900, write=10, pool=900))

        def post(body: dict) -> tuple[dict, float]:
            t0 = time.time()
            for k in range(4):
                try:
                    j = cl.post(url, headers=hdr, json={"handle": handle, "chat": "", "dm": True, **body}).json()
                    return j, time.time() - t0
                except (httpx.HTTPError, ValueError) as e:
                    if k == 3:
                        return {"reply": f"[ERROR {e!r}]"}, time.time() - t0
                    time.sleep(5)
            return {}, 0.0

        post({"text": "hey picky"})  # warm-up (live keeps the prefix warm; a fresh side sidecar has none)
        cur = None
        for it in order:
            if it["at"] != cur:
                cur = it["at"]
                clock.set(T.t(cur))
                clock_file.write_text(str(T.t(cur)))
                T.F.fill(T.sb, T.games, T.t(cur), live, POOL.keys())
                print(f"== {DAY} {cur} CT", flush=True)
            (rd / "hist.json").write_text("{}")  # each item starts a fresh thread
            prior = []
            for q in (it.get("turns") or [])[:-1]:
                j, s = post({"text": q})
                prior.append({"q": q, "reply": j.get("reply"), "s": round(s, 1), "lane": j.get("lane")})
            if it.get("clump"):
                if a.clump == "combined":
                    j, s = post({"text": it["clump"][-1], "texts": it["clump"]})
                    replies, lanes, secs = [j.get("reply")], [j.get("lane")], s
                else:  # today's watcher: each row answered on its own, serially
                    replies, lanes, secs = [], [], 0.0
                    for q in it["clump"]:
                        j, s = post({"text": q})
                        replies.append(j.get("reply"))
                        lanes.append(j.get("lane"))
                        secs += s
                got = [r for r in replies if r]
                row = {"id": it["id"], "cat": it["cat"], "at": it["at"], "q": " / ".join(it["clump"]),
                       "reply": "\n".join(got), "n_replies": len(got), "lane": ",".join(map(str, lanes)), "s": round(secs, 1)}
            else:
                q = it["turns"][-1]
                j, s = post({"text": q})
                row = {"id": it["id"], "cat": it["cat"], "at": it["at"], "q": q, "reply": j.get("reply") or "",
                       "n_replies": 1 if j.get("reply") else 0, "image": bool(j.get("image")), "lane": j.get("lane"),
                       "s": round(s, 1)}
            row["prior"] = prior
            row["grade"] = grade_item(T, it, row)
            rows.append(row)
            g = row["grade"]
            print(f"  {'PASS' if g['pass'] else 'FAIL'} {row['s']:6.1f}s {str(row['lane'])[:18]:<18} {it['id']:<20} "
                  f"{row['reply'][:110]!r} {g['fails'] if g['fails'] else ''}", flush=True)
            (out / "answers.json").write_text(json.dumps({"tag": a.tag, "wt": str(wt), "rows": rows}, indent=1))
    finally:
        stop.set()
        try:
            os.killpg(side.pid, signal.SIGTERM)
            side.wait(20)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            side.kill()
        srv.shutdown()
    rep = report_rows(rows)
    (out / "report.json").write_text(json.dumps(rep, indent=1))
    print(json.dumps(rep["overall"], indent=1))
    return 0


# ----------------------------------------------------------------------------
# grade / judge / report
# ----------------------------------------------------------------------------

def pct(xs, q):
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(round(q * (len(xs) - 1))))], 1) if xs else None


def report_rows(rows: list[dict], judge: dict | None = None) -> dict:
    cats = {}
    for r in rows:
        c = cats.setdefault(r["cat"], {"n": 0, "pass": 0, "facts": 0, "judge_ok": 0, "judged": 0})
        c["n"] += 1
        c["pass"] += r["grade"]["pass"]
        c["facts"] += r["grade"]["facts_ok"]
        jv = (judge or {}).get(r["id"])
        if jv:
            c["judged"] += 1
            c["judge_ok"] += jv.get("ok", False)
    secs = [r["s"] for r in rows if r.get("s")]
    one = sum(r["grade"]["one_text"] for r in rows)
    short = sum(r["grade"]["short"] for r in rows)
    return {"overall": {"n": len(rows), "pass": sum(r["grade"]["pass"] for r in rows),
                        "facts_ok": sum(r["grade"]["facts_ok"] for r in rows), "one_text": one, "short": short,
                        "one_text_short": sum(r["grade"]["one_text"] and r["grade"]["short"] for r in rows),
                        "p50": pct(secs, .5), "p90": pct(secs, .9), "max": max(secs) if secs else None,
                        "judge_ok": sum(v.get("ok", False) for v in (judge or {}).values()) if judge else None},
            "cats": cats}


def regrade(a) -> int:
    sys.path.insert(0, str(HERE))
    import sat_dm_items as I
    T = Truth(Path(a.fixture))
    d = json.loads((OUT / a.tag / "answers.json").read_text())
    by = {i["id"]: i for i in I.ITEMS}
    for r in d["rows"]:
        r["grade"] = grade_item(T, by[r["id"]], r)
        if not r["grade"]["pass"]:
            print(f"FAIL {r['id']:<20} {r['reply'][:140]!r} {r['grade']['fails']} chars={r['grade']['chars']} one={r['grade']['one_text']}")
    (OUT / a.tag / "answers.json").write_text(json.dumps(d, indent=1))
    jf = OUT / a.tag / "judge.json"
    rep = report_rows(d["rows"], json.loads(jf.read_text()) if jf.exists() else None)
    (OUT / a.tag / "report.json").write_text(json.dumps(rep, indent=1))
    print(json.dumps(rep, indent=1))
    return 0


JUDGE = """You grade a pick'em league chatbot ("Picky") replying by iMessage DM to Charlie, the league commissioner, who is
texting from inside a loud stadium with a weak signal on a college football Saturday. Grade ONLY tone and
on-point-ness, NOT factual accuracy (code checks facts separately). For each item return:
- on_point 1-5: does the reply answer exactly what he asked, with the answer in the first sentence (no wind-up)?
- warm 1-5: friendly and warm toward Charlie (he must always be treated nicely; light ribbing is fine, insults are not)?
- texty 1-5: reads like a quick human text a buddy would send (short, no robot phrasing, no lists of caveats)?
ok = on_point >= 4 and warm >= 4 and texty >= 3.
Items (JSON): {items}
Return JSON: {{"grades": [{{"id": ..., "on_point": n, "warm": n, "texty": n, "ok": bool, "why": "<10 words"}}]}}"""


def judge(a) -> int:
    sys.path.insert(0, str(HERE.parent))
    import dossier
    d = json.loads((OUT / a.tag / "answers.json").read_text())
    res = {}
    rows = d["rows"]
    schema = {"type": "object", "properties": {"grades": {"type": "array", "items": {"type": "object", "properties": {
        "id": {"type": "string"}, "on_point": {"type": "integer"}, "warm": {"type": "integer"},
        "texty": {"type": "integer"}, "ok": {"type": "boolean"}, "why": {"type": "string"}},
        "required": ["id", "on_point", "warm", "texty", "ok"]}}}, "required": ["grades"]}
    for i in range(0, len(rows), 30):
        chunk = [{"id": r["id"], "earlier": [p["q"] for p in r.get("prior") or []], "charlie": r["q"],
                  "picky": r["reply"]} for r in rows[i:i + 30]]
        out = dossier.run_claude(JUDGE.format(items=json.dumps(chunk)), schema=schema, tools="", caller="sat_dm_judge")
        for g in out.get("grades") or []:
            res[g["id"]] = g
    (OUT / a.tag / "judge.json").write_text(json.dumps(res, indent=1))
    rep = report_rows(rows, res)
    (OUT / a.tag / "report.json").write_text(json.dumps(rep, indent=1))
    print(json.dumps(rep, indent=1))
    return 0


def report(a) -> int:
    lines = []
    for tag in a.tags:
        rep = json.loads((OUT / tag / "report.json").read_text())
        o = rep["overall"]
        lines.append(f"## {tag}\noverall {o['pass']}/{o['n']} pass (facts {o['facts_ok']}/{o['n']}), one-text+short "
                     f"{o['one_text_short']}/{o['n']}, judge ok {o['judge_ok']}, p50 {o['p50']} s, p90 {o['p90']} s, max {o['max']} s")
        for c, v in sorted(rep["cats"].items()):
            lines.append(f"  {c:<9} {v['pass']}/{v['n']} pass, facts {v['facts']}/{v['n']}"
                         + (f", judge {v['judge_ok']}/{v['judged']}" if v["judged"] else ""))
    txt = "\n".join(lines)
    print(txt)
    if len(a.tags) > 1:
        (OUT / f"compare-{'-vs-'.join(a.tags)}.md").write_text(txt + "\n")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--wt", required=True)
    r.add_argument("--tag", required=True)
    r.add_argument("--only", default="")
    r.add_argument("--clump", choices=("serial", "combined"), default="combined")
    r.add_argument("--port", type=int, default=8812)
    r.add_argument("--fake-port", type=int, default=8813)
    for p in (r, sub.add_parser("grade"), sub.add_parser("judge")):
        p.add_argument("--fixture", default=str(Path("/Users/Agent/cfb-pickem-private/drills/satdm/fixture-20261114")))
    sub.choices["grade"].add_argument("tag")
    sub.choices["judge"].add_argument("tag")
    rp = sub.add_parser("report")
    rp.add_argument("tags", nargs="+")
    a = ap.parse_args(argv)
    return {"run": run, "grade": regrade, "judge": judge, "report": report}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
