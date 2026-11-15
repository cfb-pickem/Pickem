"""
League lore for Picky: records, streaks, best/worst weeks, upsets hit, blown
picks, lone wolves, head-to-head and past champions, computed from the
league's own data. Nothing here is invented: every line is a count or a
game straight from Supabase (finals only, so no hidden pick is ever touched),
js/leagueHistory.js (the 2022-24 Splash seasons, weekly wins only) and the
champions list on cfb-genius.html.

    ./.venv/bin/python3 -I lore.py            # refresh server/cache/lore.json
    ./.venv/bin/python3 -I lore.py show TG    # print one member's lines

Read-only against Supabase (service key, GETs only). chat.py reads the cache
file (mtime-memoized) and injects only the members a question names.
LaunchAgent com.pickem.lore-refresh runs this daily.
"""

from __future__ import annotations

import collections
import datetime as dt
import itertools
import json
import re
import subprocess
import sys
import unicodedata
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
CACHE = HERE / "cache" / "lore.json"
REST = "https://vopdioszofwdkwnujtiq.supabase.co/rest/v1"
SEASON_WEEKS = 15  # regular season; playoff weeks never count
H2H_MIN = 6


def service_key() -> str:
    return subprocess.check_output(["security", "find-generic-password", "-a", "drake", "-s",
                                    "CFB_PICKEM__SUPABASE_SERVICE_KEY", "-w"], text=True).strip()


def _get_all(c: httpx.Client, path: str) -> list[dict]:
    out, off = [], 0
    while True:
        r = c.get(f"{REST}/{path}{'&' if '?' in path else '?'}limit=1000&offset={off}")
        r.raise_for_status()
        rows = r.json()
        out += rows
        if len(rows) < 1000:
            return out
        off += 1000


def norm(v) -> str:
    """chat._norm_team (a port of js/utils.js norm())."""
    s = unicodedata.normalize("NFD", str(v or ""))
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = s.replace("&", " and ").replace(".", "")
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def same(a, b) -> bool:
    """chat.same_team: 'Oregon St.' == 'Oregon State'."""
    na, nb = norm(a), norm(b)
    st = lambda x: re.sub(r"\bst\b", "state", x)  # noqa: E731
    return na == nb or st(na) == st(nb)


# ---- static sources in the repo -------------------------------------------------------

def splash_history() -> dict[int, dict[str, int]]:
    """{season: {team: total wins}} from js/leagueHistory.js (2022-24)."""
    src = (REPO / "js" / "leagueHistory.js").read_text()
    body = src[src.index("export const LEAGUE_HISTORY"):]
    out: dict[int, dict[str, int]] = {}
    season = None
    for ln in body.splitlines():
        m = re.match(r"\s*(\d{4}):\s*\{", ln)
        if m:
            season = int(m.group(1))
            out[season] = {}
            continue
        m = re.match(r"\s*'([^']+)':\s*\[([\d,\s]+)\]", ln)
        if m and season:
            out[season][m.group(1)] = sum(int(x) for x in m.group(2).split(","))
        if ln.startswith("};"):
            break
    return out


def champions() -> list[dict]:
    """[{season, team}] from the Past Champions list on cfb-genius.html."""
    html = (REPO / "cfb-genius.html").read_text()
    i = html.find("Past Champions")
    if i < 0:
        return []
    txt = re.sub(r"<[^>]+>", "\n", html[i:i + 4000])
    toks = [t.strip() for t in txt.splitlines() if t.strip()]
    out = []
    for a, b in zip(toks, toks[1:]):
        if re.fullmatch(r"20\d\d", a) and not re.fullmatch(r"20\d\d", b) and b.upper() != "TBD" and len(b) < 40:
            out.append({"season": int(a), "team": b})
        if a.startswith("import") or "<script" in a:
            break
    return out


# ---- the computation -----------------------------------------------------------------

def load(key: str) -> dict:
    h = {"apikey": key, "Authorization": f"Bearer {key}"}
    with httpx.Client(headers=h, timeout=30) as c:
        teams = _get_all(c, "teams?select=team_id,team_name,active,is_exhibition")
        games = _get_all(c, "all_games?select=GameId,cfb_season,week,Away,Home,AwayPts,HomePts,line,winner,"
                            "Status,tiebreaker,%22Start%20(CT)%22&picked=is.true&order=GameId")
        picks = []
        ids = [g["GameId"] for g in games if g.get("winner")]
        for i in range(0, len(ids), 150):
            chunk = ",".join(str(x) for x in ids[i:i + 150])
            picks += _get_all(c, f"picks?select=team_id,game_id,pick,jackpot&game_id=in.({chunk})&order=id")
    return {"teams": teams, "games": games, "picks": picks}


def _fmt_line(side: str, g: dict) -> str:
    ln = g.get("line")
    if ln is None:
        return side
    ln = float(ln)
    home_fav = ln < 0
    fav = (side == g["Home"]) == home_fav if ln else False
    n = abs(ln)
    num = f"{n:g}"
    return f"{side} {'-' if fav else '+'}{num}" if ln else f"{side} (pick'em)"


def _hi_lo(g: dict) -> str:
    a, h = int(float(g["AwayPts"])), int(float(g["HomePts"]))
    return f"{max(a, h)}-{min(a, h)}"


def _lo_hi(g: dict) -> str:
    a, h = int(float(g["AwayPts"])), int(float(g["HomePts"]))
    return f"{min(a, h)}-{max(a, h)}"


def compute(data: dict) -> dict:
    name = {t["team_id"]: t["team_name"] for t in data["teams"] if not t.get("is_exhibition")}
    games = {g["GameId"]: g for g in data["games"]
             if g.get("winner") and not g.get("tiebreaker")
             and g.get("week") is not None and g["week"] <= SEASON_WEEKS}
    rows = []  # one per graded pick
    for p in data["picks"]:
        g = games.get(p["game_id"])
        if not g or p["team_id"] not in name:
            continue
        # same grading as chat.compute_standings: a pick matching neither side is a miss
        side = g["Home"] if same(p["pick"], g["Home"]) else g["Away"] if same(p["pick"], g["Away"]) else p["pick"]
        hit = bool(p.get("jackpot")) or same(side, g["winner"])
        rows.append({"team": name[p["team_id"]], "g": g, "side": side, "hit": hit})
    rows.sort(key=lambda r: (str(r["g"].get("Start (CT)") or ""), r["g"]["GameId"]))
    seasons = sorted({r["g"]["cfb_season"] for r in rows})
    by_game = collections.defaultdict(list)
    for r in rows:
        by_game[r["g"]["GameId"]].append(r)

    def margin(g, side):
        try:
            a, h = float(g["AwayPts"]), float(g["HomePts"])
        except (TypeError, ValueError):
            return None
        return (h - a) if side == g["Home"] else (a - h)

    def is_fav(g, side):
        ln = float(g.get("line") or 0)
        return ln != 0 and ((side == g["Home"]) == (ln < 0))

    def where(g):
        return f"{g['cfb_season']} week {g['week']}"

    # the site's "Season by season" table (js/seasonHistory.js): wins only, weeks 1-15, any picked game
    allg = {g["GameId"]: g for g in data["games"] if g.get("winner") and g.get("week") is not None
            and 1 <= g["week"] <= SEASON_WEEKS}
    site_wins: dict[int, collections.Counter] = collections.defaultdict(collections.Counter)
    for p in data["picks"]:
        g = allg.get(p["game_id"])
        if g and p["team_id"] in name:
            site_wins[g["cfb_season"]][name[p["team_id"]]] += bool(p.get("jackpot") or same(p["pick"], g["winner"]))
    members: dict[str, dict] = {}
    standings: dict[int, list] = {}
    for season in seasons:
        rec = collections.Counter()
        tot = collections.Counter()
        for r in rows:
            if r["g"]["cfb_season"] == season:
                tot[r["team"]] += 1
                rec[r["team"]] += r["hit"]
        tbl = sorted(tot, key=lambda t: (-rec[t], t))
        standings[season] = []
        for t in tbl:
            rank = 1 + sum(1 for u in tbl if rec[u] > rec[t])
            standings[season].append({"team": t, "w": rec[t], "l": tot[t] - rec[t], "rank": rank})

    for team in sorted({r["team"] for r in rows}):
        mine = [r for r in rows if r["team"] == team]
        out = {"seasons": {}, "upsets": [], "blown": [], "lone_hits": [], "lone_misses": []}
        for season in seasons:
            sr = [r for r in mine if r["g"]["cfb_season"] == season]
            if not sr:
                continue
            wk = collections.defaultdict(lambda: [0, 0])
            for r in sr:
                wk[r["g"]["week"]][0 if r["hit"] else 1] += 1
            weeks = {w: tuple(v) for w, v in sorted(wk.items())}
            best = max(v[0] - v[1] for v in weeks.values())
            worst = min(v[0] - v[1] for v in weeks.values())
            streaks = {True: 0, False: 0}
            for hit, grp in itertools.groupby(sr, key=lambda r: r["hit"]):
                streaks[hit] = max(streaks[hit], len(list(grp)))
            last_hit = sr[-1]["hit"]
            cur = len(list(itertools.takewhile(lambda r: r["hit"] == last_hit, reversed(sr))))
            row = next(x for x in standings[season] if x["team"] == team)
            out["seasons"][season] = {
                "w": row["w"], "l": row["l"], "rank": row["rank"], "of": len(standings[season]),
                "best_week": [best, [w for w, v in weeks.items() if v[0] - v[1] == best], weeks[[w for w, v in weeks.items() if v[0] - v[1] == best][0]]],
                "worst_week": [worst, [w for w, v in weeks.items() if v[0] - v[1] == worst], weeks[[w for w, v in weeks.items() if v[0] - v[1] == worst][0]]],
                "perfect": [w for w, v in weeks.items() if v[1] == 0 and v[0] >= 3],
                "winless": [w for w, v in weeks.items() if v[0] == 0 and v[1] >= 3],
                "hit_streak": streaks[True], "miss_streak": streaks[False],
                "current": [("hit" if last_hit else "miss"), cur],
            }
        for r in mine:
            g, side = r["g"], r["side"]
            m = margin(g, side)
            sp = abs(float(g.get("line") or 0))
            others = [x for x in by_game[g["GameId"]] if x["side"] == side and x["team"] != team]
            if r["hit"] and m is not None and m > 0 and not is_fav(g, side) and sp >= 3:
                out["upsets"].append((sp, f"{_fmt_line(side, g)} won outright {_hi_lo(g)} "
                                          f"({g['Away']} at {g['Home']}, {where(g)})"))
            if not r["hit"] and m is not None and m < 0 and is_fav(g, side) and sp >= 7:
                out["blown"].append((sp, f"{_fmt_line(side, g)} lost outright {_lo_hi(g)} "
                                         f"({g['Away']} at {g['Home']}, {where(g)})"))
            if not others and len(by_game[g["GameId"]]) >= 6:
                (out["lone_hits"] if r["hit"] else out["lone_misses"]).append(
                    f"the only one on {_fmt_line(side, g)} ({g['Away']} at {g['Home']}, {where(g)}), and it "
                    + ("covered" if r["hit"] else "did not cover"))
        out["upsets"] = [s for _, s in sorted(out["upsets"], key=lambda x: -x[0])[:2]]
        out["blown"] = [s for _, s in sorted(out["blown"], key=lambda x: -x[0])[:2]]
        out["lone_hits"] = out["lone_hits"][-2:]
        out["lone_misses"] = out["lone_misses"][-2:]
        members[team] = out

    # head to head: games where two members took opposite sides
    h2h = collections.defaultdict(lambda: [0, 0])
    for gid, rs in by_game.items():
        for a, b in itertools.permutations(rs, 2):
            if a["side"] != b["side"]:
                h2h[(a["team"], b["team"])][0 if a["hit"] else 1] += 1
    for team, out in members.items():
        opp = [(o, tuple(v)) for (t, o), v in h2h.items() if t == team and sum(v) >= H2H_MIN]
        if opp:
            best = max(opp, key=lambda x: (x[1][0] - x[1][1], x[1][0]))
            worst = min(opp, key=lambda x: (x[1][0] - x[1][1], -x[1][1]))
            out["h2h"] = {"best": best, "worst": worst}
        out["h2h_all"] = {o: list(v) for (t, o), v in h2h.items() if t == team}

    # stats.html: weekly awards (Lock of the Week / Trap Game / Lone Wolf), Top Dawg, last 8
    cur_season = max(seasons) if seasons else None
    awards = {}
    if cur_season:
        weeks = sorted({r["g"]["week"] for r in rows if r["g"]["cfb_season"] == cur_season})
        for w in weeks:
            wg = {gid: rs for gid, rs in by_game.items() if rs and rs[0]["g"]["cfb_season"] == cur_season
                  and rs[0]["g"]["week"] == w}
            if not wg:
                continue
            right = {gid: [r["team"] for r in rs if r["hit"]] for gid, rs in wg.items()}
            wrong = {gid: [r["team"] for r in rs if not r["hit"]] for gid, rs in wg.items()}
            lab = lambda gid: f"{wg[gid][0]['g']['Away']} @ {wg[gid][0]['g']['Home']}"  # noqa: E731
            top_r, top_w = max(len(v) for v in right.values()), max(len(v) for v in wrong.values())
            a = {"week": w, "n_games": len(wg)}
            if top_r:
                ids = [g for g in wg if len(right[g]) == top_r]
                a["lock"] = (" & ".join(lab(g) for g in ids), top_r, len(wg[ids[0]]))
            if top_w:
                ids = [g for g in wg if len(wrong[g]) == top_w]
                a["trap"] = (" & ".join(lab(g) for g in ids), top_w, len(wg[ids[0]]))
            lone = sorted([g for g in wg if len(right[g]) == 1 and len(wrong[g]) >= 2], key=lambda g: -len(wrong[g]))
            if lone:
                a["lone"] = (right[lone[0]][0], lab(lone[0]))
            awards[w] = a
    dogs = collections.Counter()
    for r in rows:
        g = r["g"]
        if g["cfb_season"] != cur_season or g.get("line") in (None, 0):
            continue
        dog = g["Away"] if float(g["line"]) < 0 else g["Home"]
        m = margin(g, dog)
        if r["side"] == dog and m is not None and m > 0:
            dogs[r["team"]] += 1
    last8 = {}
    for team in {r["team"] for r in rows}:
        mine = [r for r in rows if r["team"] == team and r["g"]["cfb_season"] == cur_season][-8:]
        if mine:
            last8[team] = (sum(r["hit"] for r in mine), len(mine) - sum(r["hit"] for r in mine))
    site = {}
    for season, c in site_wins.items():
        order = sorted(c, key=lambda t: (-c[t], t))
        site[season] = [{"team": t, "wins": c[t], "rank": 1 + sum(1 for u in order if c[u] > c[t]), "of": len(order)}
                        for t in order]
    return {"members": members, "standings": standings, "seasons": seasons, "site_wins": site,
            "awards": awards, "top_dawg": dogs, "last8": last8}


def _weeks(ws: list) -> str:
    return ", ".join(f"week {w}" for w in ws[:3]) + (f" and {len(ws) - 3} more" if len(ws) > 3 else "")


def phrase(comp: dict, splash: dict, champs: list[dict], today: str) -> dict:
    """Pre-phrased, true lines per member and for the league."""
    champ_of = collections.defaultdict(list)
    for c in champs:
        champ_of[c["team"]].append(c["season"])
    cur = max(comp["seasons"]) if comp["seasons"] else None
    lines: dict[str, list[str]] = {}
    for team, m in comp["members"].items():
        L = []
        if champ_of.get(team):
            L.append(f"{team} is a past league champion (CFB Genius): {', '.join(map(str, champ_of[team]))}. "
                     "Respect that, even while roasting this season.")
        for season, s in sorted(m["seasons"].items()):
            bw, ww = s["best_week"], s["worst_week"]
            # current season: no W-L, rank or current streak (live standings own those; this cache lags)
            sw = next((x for x in comp["site_wins"].get(season, []) if x["team"] == team), None)
            head = (f"{season} so far (through {today})" if season == cur else
                    f"{season} season: {sw['wins']} wins in weeks 1-15, {sw['rank']}{_ord(sw['rank'])} of {sw['of']}"
                    if sw else f"{season} season")
            L.append(f"{head}: best week {bw[2][0]}-{bw[2][1]} ({_weeks(bw[1])}), worst week {ww[2][0]}-{ww[2][1]} "
                     f"({_weeks(ww[1])})"
                     + (f"; perfect weeks: {_weeks(s['perfect'])}" if s["perfect"] else "")
                     + (f"; winless weeks: {_weeks(s['winless'])}" if s["winless"] else "")
                     + f"; longest hit streak {s['hit_streak']} picks, longest miss streak {s['miss_streak']}"
                     + (f"; last 8 picks {comp['last8'][team][0]}-{comp['last8'][team][1]}"
                        if season == cur and team in comp.get("last8", {}) else "")
                     + (f"; underdog outright wins picked: {comp['top_dawg'][team]}"
                        if season == cur and comp.get("top_dawg", {}).get(team) else "") + ".")
        if m["upsets"]:
            L.append("Best upset hits: " + "; ".join(m["upsets"]) + ".")
        if m["blown"]:
            L.append("Worst blown picks (favorite that lost outright): " + "; ".join(m["blown"]) + ".")
        if m["lone_hits"]:
            L.append("Lone-wolf hits: " + "; ".join(m["lone_hits"]) + ".")
        if m["lone_misses"]:
            L.append("Lone-wolf misses: " + "; ".join(m["lone_misses"]) + ".")
        if m.get("h2h"):
            b, w = m["h2h"]["best"], m["h2h"]["worst"]
            L.append(f"Head to head (games where they took opposite sides, {min(comp['seasons'])}-{cur}): "
                     f"best vs {b[0]} {b[1][0]}-{b[1][1]}; worst vs {w[0]} {w[1][0]}-{w[1][1]}.")
        sp = [(y, splash[y][team], sorted(splash[y].values(), reverse=True).index(splash[y][team]) + 1, len(splash[y]))
              for y in sorted(splash) if team in splash[y]]
        if sp:
            L.append("Splash era (wins only, no picks kept): " + "; ".join(
                f"{y}: {w} wins, {r}{_ord(r)} of {n}" + (" (2023 list is incomplete)" if y == 2023 else "")
                for y, w, r, n in sp) + ".")
        lines[team] = [ln if ln.startswith(team) else f"{team}, {ln[0].lower() + ln[1:] if ln[:4] not in ('2025', '2026', '2024') else ln}" for ln in L]
    league = []
    if champs:
        cs = sorted(champs, key=lambda c: c["season"])
        last = cs[-1]
        league.append(f"League champions (CFB Genius), from the site: LAST SEASON ({last['season']}) the champion "
                      f"was {last['team']}" + "".join(f"; {c['season']}: {c['team']}" for c in reversed(cs[:-1]))
                      + f". The current season is {cur}, still in progress (no champion yet). Speak of champions "
                      "with real respect.")
    aw = comp.get("awards") or {}
    if aw:
        # the last week with every pool game graded (4 games), like the stats page's last-week awards
        done = [w for w, a in aw.items() if a["n_games"] >= 4]
        if done:
            a = aw[max(done)]
            bits = []
            if a.get("lock"):
                bits.append(f"Lock of the Week: {a['lock'][0]} ({a['lock'][1]} of {a['lock'][2]} had it)")
            if a.get("trap"):
                bits.append(f"Trap Game: {a['trap'][0]} ({a['trap'][1]} of {a['trap'][2]} got burned)")
            if a.get("lone"):
                bits.append(f"Lone Wolf: {a['lone'][0]} (the only one right on {a['lone'][1]})")
            league.append(f"Awards for {cur} week {a['week']} (the last fully graded week): " + "; ".join(bits) + ".")
    td = comp.get("top_dawg") or {}
    if td:
        league.append(f"Top Dawg {cur} (picked the underdog AND it won the game outright): "
                      + ", ".join(f"{t} {n}" for t, n in td.most_common(4)) + ".")
    prior = [s for s in comp["seasons"] if s != cur]
    for season in prior:
        tbl = comp["site_wins"].get(season) or []
        if not tbl:
            continue
        top = [r for r in tbl if r["rank"] <= 3]
        bot = [r for r in tbl if r["rank"] == tbl[-1]["rank"]]
        league.append(f"{season} season wins (weeks 1-15, as the site counts them), top: " + ", ".join(
            f"{r['team']} {r['wins']}" for r in top) + "; fewest: " + ", ".join(
            f"{r['team']} {r['wins']}" for r in bot) + ".")
    for y in sorted(splash):
        top = sorted(splash[y].items(), key=lambda kv: -kv[1])[:3]
        league.append(f"{y} (Splash, wins only{', list incomplete' if y == 2023 else ''}) most wins: "
                      + ", ".join(f"{t} {w}" for t, w in top) + ".")
    return {"as_of": today, "members": lines, "league": league,
            "h2h": {t: m.get("h2h_all", {}) for t, m in comp["members"].items()}}


def _ord(n: int) -> str:
    return "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")


# ---- casino: The Pit (blackjack) + slots ------------------------------------------------
# Chips are public (Tiebreakers board); hands are shown to everyone once a week
# settles, so only SETTLED hands are used. Blunders are dealer-independent
# basic-strategy mistakes, read straight off the cards.

def card_value(c: int) -> int:
    r = c % 13
    return 11 if r == 0 else 10 if r >= 9 else r + 1


def hand_total(cards: list[int]) -> tuple[int, bool]:
    """(total, soft) like js/blackjack.js handTotal: aces count 11 until that busts."""
    t = sum(card_value(c) for c in cards)
    aces = sum(1 for c in cards if c % 13 == 0)
    while t > 21 and aces:
        t -= 10
        aces -= 1
    return t, aces > 0


def _label(c: int) -> str:
    return ["A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K"][c % 13]


def casino(key: str, name: dict[int, str], season: int) -> dict:
    h = {"apikey": key, "Authorization": f"Bearer {key}"}
    with httpx.Client(headers=h, timeout=30) as c:
        banks = _get_all(c, f"blackjack_bankrolls?select=team_id,chips,hands_played,busted&cfb_season=eq.{season}")
        hands = _get_all(c, f"blackjack_hands?select=team_id,week,bet,doubled,cards,state,outcome,payout"
                            f"&cfb_season=eq.{season}&state=eq.settled&order=week")
        slots = _get_all(c, "picks?select=team_id,game_id,pick,by_slots,jackpot&by_slots=is.true")
        games = {g["GameId"]: g for g in _get_all(
            c, f"all_games?select=GameId,winner,cfb_season&cfb_season=eq.{season}&picked=is.true")}
    per: dict[str, dict] = collections.defaultdict(lambda: {"blunders": [], "busts": 0, "bj": 0, "w": 0, "l": 0,
                                                            "p": 0, "worst": None})
    for hd in hands:
        t = name.get(hd["team_id"])
        cards = hd.get("cards") or []
        if not t or len(cards) < 2:
            continue
        m = per[t]
        o = hd.get("outcome")
        m["w"] += o in ("win", "blackjack")
        m["l"] += o == "lose"
        m["p"] += o == "push"
        m["bj"] += o == "blackjack"
        tot, soft = hand_total(cards)
        shown = " ".join(_label(x) for x in cards)
        if tot > 21:
            m["busts"] += 1
        for k in range(2, len(cards)):  # every prefix that drew another card was a hit (or the double)
            pt, psoft = hand_total(cards[:k])
            move = "doubled" if hd.get("doubled") and k == 2 else "hit"
            if pt >= 17 and not psoft:
                m["blunders"].append(f"{move} on a hard {pt} in week {hd['week']} ({shown})"
                                     + (", and busted" if tot > 21 else ""))
            elif move == "doubled" and pt >= 12 and not psoft:
                m["blunders"].append(f"doubled on a hard {pt} in week {hd['week']} ({shown})"
                                     + (", and busted" if tot > 21 else ""))
        if len(cards) == 2 and not hd.get("doubled") and tot <= 11:
            m["blunders"].append(f"stood on {tot} in week {hd['week']} ({shown}), a hand that can't bust")
        lost = (hd.get("bet") or 0) * (2 if hd.get("doubled") else 1)
        if o == "lose" and (m["worst"] is None or lost > m["worst"][0]):
            m["worst"] = (lost, hd["week"])
    bank = {name[b["team_id"]]: b for b in banks if b["team_id"] in name}
    sl: dict[str, list] = collections.defaultdict(lambda: [0, 0])
    for p in slots:
        g = games.get(p["game_id"])
        t = name.get(p["team_id"])
        if not g or not g.get("winner") or not t:
            continue
        hit = bool(p.get("jackpot")) or same(p.get("pick"), g["winner"])
        sl[t][0 if hit else 1] += 1
    lines: dict[str, list[str]] = {}
    for t in sorted(set(bank) | set(per) | set(sl)):
        L = []
        b, m = bank.get(t), per.get(t)
        if b:
            L.append(f"{t}, The Pit {season}: {b['chips']:,} chips after {b['hands_played']} hands"
                     + (" (BUSTED OUT, done for the season)" if b.get("busted") else "") + ".")
        if m:
            L.append(f"{t}'s settled blackjack hands: {m['w']}-{m['l']}" + (f"-{m['p']} (pushes)" if m["p"] else "")
                     + (f", {m['bj']} blackjack{'s' * (m['bj'] != 1)}" if m["bj"] else "")
                     + (f", {m['busts']} bust{'s' * (m['busts'] != 1)}" if m["busts"] else "")
                     + (f"; biggest loss {m['worst'][0]} chips in week {m['worst'][1]}" if m["worst"] else "") + ".")
            if m["blunders"]:
                L.append(f"{t}'s blackjack blunders (basic strategy says never): " + "; ".join(m["blunders"][:3]) + ".")
        if t in sl and sum(sl[t]):
            w, l = sl[t]
            L.append(f"{t}'s 'let the slots decide' picks this season: {w}-{l}.")
        lines[t] = L
    board = sorted(bank.items(), key=lambda kv: -kv[1]["chips"])
    top = ", ".join(f"{t} {b['chips']:,}" for t, b in board[:5])
    bot = ", ".join(f"{t} {b['chips']:,}" for t, b in board[-3:])
    busted = [t for t, b in bank.items() if b.get("busted")]
    league = [f"The Pit {season} chip leaders: {top}. Fewest: {bot}."
              + (f" Busted out: {', '.join(busted)}." if busted else " Nobody has busted out.")]
    return {"members": lines, "league": league}


def refresh() -> dict:
    key = service_key()
    data = load(key)
    comp = compute(data)
    out = phrase(comp, splash_history(), champions(), dt.date.today().isoformat())
    names = {t["team_id"]: t["team_name"] for t in data["teams"] if not t.get("is_exhibition") and t.get("active")}
    if comp["seasons"]:
        out["casino"] = casino(key, names, max(comp["seasons"]))
    CACHE.parent.mkdir(exist_ok=True)
    tmp = CACHE.with_suffix(".tmp")
    tmp.write_text(json.dumps(out, indent=1))
    tmp.replace(CACHE)
    return out


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "show":
        d = json.loads(CACHE.read_text())
        for ln in d["members"].get(sys.argv[2], ["(no such member)"]):
            print("-", ln)
        sys.exit(0)
    d = refresh()
    print(json.dumps({"as_of": d["as_of"], "members": len(d["members"]), "league": d["league"]}, indent=1))
