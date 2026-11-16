"""
Slate cards: what a sharp checks before a pick, for ANY FBS game this week,
not just the league's 4 pool games. Code computes every number (CFBD local
cache + ESPN's current market); the model only phrases it.

  slate_note(question, ctx)  -> rider text for the latest user turn ('' when the
                                question names no upcoming non-pool game and
                                isn't a whole-slate question)

A card has: kickoff/venue, the market (current + open spread and total, size and
direction of the move, implied team totals, key numbers), each team's record,
ATS overall and by fav/dog/home/road computed from 2026 results, the last 3
results against the number, SP+ ranks and the decided edges, style notes (run
rate vs the opponent's run defense, points per scoring opportunity,
explosiveness), rest/bye, next week's opponent (lookahead), last year's meeting
and ATS result. Personnel/news isn't here: news_note / dossiers cover what's known.

Found by the genius exam (eval/genius, 2026-11-16): Picky had no data on any
game outside the pool, so every week-12 question got a dodge or a guess.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
import sqlite3
from zoneinfo import ZoneInfo

import cfbd

log = logging.getLogger("slate_cards")
CT = ZoneInfo("America/Chicago")
KEY_NUMBERS = (3, 7, 10, 14)
MAX_CARDS = 2
BOARD_ROWS = 12
HORIZON = dt.timedelta(days=9)

_SLATE_ASK = re.compile(
    r"\b(this week'?s?|the slate|slate|key numbers?|hooks?|value (this|on the)|where'?s the value|line moves?|"
    r"lines? (moved|moving)|lookahead|look-ahead|letdown|sandwich|trap games?|best bets?|biggest (line )?moves?)\b", re.I)
_PICK_WORDS = re.compile(
    r"\b(line|spread|cover|ats|take|lay|getting|value|pick|who (you|ya|do you) (got|like|taking)|side|"
    r"points?|total|over|under|injur|qb|quarterback|backup|matchup|defen[cs]e|offen[cs]e|run|pass|rush|weather|"
    r"wind|cold|travel|altitude|rest|bye|motivation|bowl|revenge|rivalry|history|last time|h2h|trend|record|"
    r"lookahead|letdown|key number|hook|why|best case|change your mind|playing|starting|out|hurt)\b", re.I)


_ADV: dict = {}


def _advanced() -> dict:
    """Raw CFBD advanced stats by normalized team (run rate, rushing PPA, points per
    opportunity), which cfbd's index doesn't keep. Cached per cache refresh."""
    try:
        db = sqlite3.connect(cfbd.DB_PATH)
        stamp, js = db.execute("select fetched_at, json from datasets where name='advanced'").fetchone()
        db.close()
    except Exception:  # noqa: BLE001
        return {}
    if _ADV.get("stamp") != stamp:
        rows = {cfbd.norm(r["team"]): r for r in json.loads(js)}
        for side, key, high in (("offense", "rush", True), ("defense", "rush", False)):
            ranked = sorted((r for r in rows.values() if isinstance(((r.get(side) or {}).get("rushingPlays") or {}).get("ppa"), (int, float))),
                            key=lambda r: r[side]["rushingPlays"]["ppa"], reverse=high)
            for i, r in enumerate(ranked, 1):
                r.setdefault("_rank", {})[f"{side}_rush"] = i
        _ADV.clear()
        _ADV.update({"stamp": stamp, "rows": rows})
    return _ADV.get("rows") or {}


def run_game_line(away: str, home: str) -> str | None:
    """Run-heavy offenses (incl. option teams) vs the other side's run defense."""
    adv = _advanced()
    bits = []
    for off, de in ((away, home), (home, away)):
        o, d = adv.get(cfbd.norm(off)), adv.get(cfbd.norm(de))
        if not o or not d:
            continue
        rate = ((o.get("offense") or {}).get("rushingPlays") or {}).get("rate")
        drank = (d.get("_rank") or {}).get("defense_rush")
        orank = (o.get("_rank") or {}).get("offense_rush")
        if rate and rate >= 0.58:
            style = "an option offense" if rate >= 0.72 else "run-heavy"
            bits.append(f"{off} is {style} (runs {rate:.0%} of plays, rushing EPA #{orank}) vs {de}'s run defense "
                        f"(#{drank} in rushing EPA allowed{', weak' if drank and drank >= 80 else ', strong' if drank and drank <= 25 else ''})")
    for team in (away, home):
        o = adv.get(cfbd.norm(team))
        ppo = ((o or {}).get("offense") or {}).get("pointsPerOpportunity")
        dppo = ((o or {}).get("defense") or {}).get("pointsPerOpportunity")
        if ppo and dppo:
            bits.append(f"{team} scores {ppo:.1f} pts per scoring chance, allows {dppo:.1f}")
    return "  Run game / finishing drives: " + "; ".join(bits) if bits else None


def _games_dataset() -> dict:
    """CFBD games (kickoff, venue, neutral) by id from the cache."""
    try:
        db = sqlite3.connect(cfbd.DB_PATH)
        row = db.execute("select json from datasets where name='games'").fetchone()
        db.close()
        return {g["id"]: g for g in json.loads(row[0])} if row else {}
    except Exception as e:  # noqa: BLE001
        log.warning("games dataset: %r", e)
        return {}


def _line(g: dict) -> dict | None:
    ls = [x for x in g.get("lines") or [] if x.get("spread") is not None]
    return next((x for x in ls if x.get("provider") == "DraftKings"), ls[0] if ls else None)


def _sd(g: dict) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(str(g.get("startDate")).replace("Z", "+00:00"))
    except ValueError:
        return None


def upcoming_games(idx: dict, now_utc: dt.datetime) -> list[dict]:
    """Every not-yet-played game with a kickoff in the next 9 days (from the lines dataset)."""
    seen, out = set(), []
    for t in idx["teams"].values():
        for g in t.get("games") or []:
            s = _sd(g)
            if g["id"] in seen or s is None or g.get("homeScore") is not None:
                continue
            if now_utc - dt.timedelta(hours=4) <= s <= now_utc + HORIZON:
                seen.add(g["id"])
                out.append(g)
    return sorted(out, key=lambda g: g["startDate"])


def team_rows(team: str, idx: dict, before: str) -> list[dict]:
    """A team's 2026 results before a date, each with its spread and cover margin."""
    t = cfbd.resolve_team(team, idx)
    out = []
    for g in sorted(t.get("games") or [], key=lambda g: g.get("startDate") or "") if t else []:
        if g.get("homeScore") is None or (g.get("startDate") or "") >= before or g.get("seasonType") not in (None, "regular"):
            continue
        home = cfbd.norm(g["homeTeam"]) == cfbd.norm(t["name"])
        ln = _line(g)
        sp = ln["spread"] if ln else None
        mine, theirs = (g["homeScore"], g["awayScore"]) if home else (g["awayScore"], g["homeScore"])
        my_sp = (sp if home else -sp) if sp is not None else None
        cm = (mine - theirs + my_sp) if my_sp is not None else None
        out.append({"opp": g["awayTeam"] if home else g["homeTeam"], "home": home, "pts": (mine, theirs),
                    "spread": my_sp, "cover": cm, "date": g.get("startDate", "")[:10]})
    return out


def _rec(rows: list[dict]) -> str:
    c = sum(1 for r in rows if r["cover"] is not None and r["cover"] > 0)
    l_ = sum(1 for r in rows if r["cover"] is not None and r["cover"] < 0)
    p = sum(1 for r in rows if r["cover"] == 0)
    return f"{c}-{l_}" + (f"-{p}" if p else "")


def ats_line(team: str, rows: list[dict], t: dict | None) -> str:
    graded = [r for r in rows if r["cover"] is not None]
    fav = [r for r in graded if r["spread"] < 0]
    dog = [r for r in graded if r["spread"] > 0]
    home = [r for r in graded if r["home"]]
    road = [r for r in graded if not r["home"]]
    rec = (t or {}).get("record")
    s = f"{team}{f' ({rec})' if rec else ''}: ATS {_rec(graded)}"
    s += f" (fav {_rec(fav)}, dog {_rec(dog)}, home {_rec(home)}, road {_rec(road)})"
    last = []
    for r in graded[-3:]:
        res = "push" if r["cover"] == 0 else f"{'covered' if r['cover'] > 0 else 'missed'} by {abs(r['cover']):g}"
        last.append(f"{'vs' if r['home'] else 'at'} {r['opp']} {r['pts'][0]}-{r['pts'][1]} as {r['spread']:+g}, {res}")
    if last:
        s += "; last 3: " + "; ".join(last)
    return s


def _fmt_spread(team: str, sp: float) -> str:
    return f"{team} {sp:+g}".replace("+0", "PK") if sp else f"{team} PK"


def market_line(g: dict, espn_market: dict | None) -> list[str]:
    """Current + open spread/total, move, implied totals, key numbers, all decided here."""
    away, home = g["awayTeam"], g["homeTeam"]
    ln = _line(g) or {}
    now = (espn_market or {}).get("home_now")
    opn = (espn_market or {}).get("home_open")
    total = (espn_market or {}).get("over_under")
    src = "ESPN/" + str((espn_market or {}).get("book") or "DraftKings")
    if now is None:
        now, opn, src = ln.get("spread"), ln.get("spreadOpen"), f"CFBD/{ln.get('provider')}"
    if total is None:
        total = ln.get("overUnder")
    if now is None:
        return ["  Market: no line posted yet."]
    fav, fsp = (home, now) if now < 0 else (away, -now)
    s = f"  Market ({src}): {_fmt_spread(fav, fsp)}"
    if opn is not None and opn != now:
        ofav, osp = (home, opn) if opn < 0 else (away, -opn)
        moved = abs(now - opn)
        toward = home if now < opn else away
        s += f", opened {_fmt_spread(ofav, osp)} (moved {moved:g} toward {toward})"
    elif opn is not None:
        s += " (no move from the open)"
    out = [s]
    if total:
        hi = (total + abs(now)) / 2
        lo = total - hi
        tot = f"  Total {total:g}"
        if ln.get("overUnderOpen") and ln.get("overUnderOpen") != total and src.startswith("CFBD"):
            tot += f" (opened {ln['overUnderOpen']:g})"
        tot += f"; implied team totals {fav} {round(hi * 2) / 2:g}, {away if fav == home else home} {round(lo * 2) / 2:g}"
        out.append(tot)
    a = abs(now)
    near = [k for k in KEY_NUMBERS if abs(a - k) <= 0.5]
    if near:
        k = near[0]
        if a == k:
            out.append(f"  Key number: the line sits ON {k} (a {k}-point win would push in the market; "
                       f"league lines are always half points)")
        elif a > k:
            out.append(f"  Key number: {a:g} is just over {k}: the underdog gets the hook "
                       f"(a {k}-point favorite win still covers for the dog)")
        else:
            out.append(f"  Key number: {a:g} is just under {k}: the favorite is fine with a {k}-point win")
    return out


def style_line(away: str, home: str, idx: dict) -> str | None:
    """Unit-vs-unit mismatches, as decided words (raw floats confuse a 12B)."""
    a, h = cfbd.resolve_team(away, idx), cfbd.resolve_team(home, idx)
    if not a or not h:
        return None
    bits = []
    edges = cfbd.matchup_edges(away, home, idx)
    if edges:
        bits.append(edges)
    for off, de in ((a, h), (h, a)):
        oa, da = (off.get("adv") or {}).get("offense") or {}, (de.get("adv") or {}).get("defense") or {}
        if oa.get("explosive_rank") and da.get("explosive_rank"):
            if oa["explosive_rank"] <= 30 and da["explosive_rank"] >= 90:
                bits.append(f"{off['name']} big-play offense (#{oa['explosive_rank']}) vs a {de['name']} defense "
                            f"that allows big plays (#{da['explosive_rank']})")
        if oa.get("ppa_rank") and da.get("ppa_rank") and oa["ppa_rank"] <= 25 and da["ppa_rank"] >= 85:
            bits.append(f"{off['name']} offense (EPA/play #{oa['ppa_rank']}) vs a weak {de['name']} defense "
                        f"(#{da['ppa_rank']})")
        if da.get("havoc_rank") and da["havoc_rank"] <= 15:
            bits.append(f"{de['name']} defense creates havoc (#{da['havoc_rank']} in havoc rate)")
    return "  Matchup: " + "; ".join(bits) if bits else None


def rest_line(g: dict, idx: dict, games_ds: dict) -> str | None:
    s = _sd(g)
    out = []
    for team in (g["awayTeam"], g["homeTeam"]):
        t = cfbd.resolve_team(team, idx)
        played = [x for x in (t or {}).get("games") or [] if x.get("homeScore") is not None and _sd(x) and _sd(x) < s]
        nxt = [x for x in (t or {}).get("games") or [] if _sd(x) and _sd(x) > s]
        bit = team
        if played:
            days = (s - _sd(max(played, key=lambda x: x["startDate"]))).days
            bit += f" {days} days rest" + (" (off a bye)" if days >= 13 else " (short week)" if days <= 5 else "")
        if nxt:
            n = min(nxt, key=lambda x: x["startDate"])
            opp = n["awayTeam"] if cfbd.norm(n["homeTeam"]) == cfbd.norm(team) else n["homeTeam"]
            bit += f", next {'vs' if cfbd.norm(n['homeTeam']) == cfbd.norm(team) else 'at'} {opp}"
        out.append(bit)
    return "  Rest / next week: " + "; ".join(out) if out else None


def last_meeting(g: dict) -> str | None:
    try:
        db = sqlite3.connect(cfbd.DB_PATH)
        row = db.execute("select json from datasets where name='lines_prev'").fetchone()
        db.close()
        prev = json.loads(row[0]) if row else []
    except Exception:  # noqa: BLE001
        return None
    pair = {cfbd.norm(g["homeTeam"]), cfbd.norm(g["awayTeam"])}
    hits = [x for x in prev if {cfbd.norm(x["homeTeam"]), cfbd.norm(x["awayTeam"])} == pair
            and x.get("homeScore") is not None]
    if not hits:
        return None
    x = max(hits, key=lambda x: x.get("startDate") or "")
    ln = _line(x)
    s = f"  Last meeting ({x['season']}): {x['awayTeam']} {x['awayScore']} at {x['homeTeam']} {x['homeScore']}"
    if ln:
        cm = x["homeScore"] - x["awayScore"] + ln["spread"]
        hs = ln["spread"]
        fav, fsp = (x["homeTeam"], hs) if hs < 0 else (x["awayTeam"], -hs)
        cov = x["homeTeam"] if cm > 0 else x["awayTeam"]
        s += f", line {_fmt_spread(fav, fsp)}: {cov} covered by {abs(cm):g}"
    return s


def card(g: dict, idx: dict, espn_notes: dict | None, games_ds: dict) -> str:
    away, home = g["awayTeam"], g["homeTeam"]
    meta = games_ds.get(g["id"]) or {}
    s = _sd(g)
    when = s.astimezone(CT).strftime("%a %b %-d %-I:%M %p CT") if s else "TBD"
    where = meta.get("venue") or ""
    head = (f"GAME CARD (computed from CFBD + ESPN; not a league pool game unless the pool lists it): "
            f"{away} at {home}{' (neutral site)' if meta.get('neutralSite') else ''}, {when}{', ' + where if where else ''}.")
    L = [head] + market_line(g, (espn_notes or {}).get("market"))
    for team in (away, home):
        t = cfbd.resolve_team(team, idx)
        L.append("  " + ats_line(team, team_rows(team, idx, g["startDate"]), t))
        sp = (t or {}).get("sp") or {}
        if sp.get("rank"):
            L[-1] += f"; SP+ #{sp['rank']} (off #{sp.get('off_rank')}, def #{sp.get('def_rank')})"
    for extra in (style_line(away, home, idx), run_game_line(away, home), rest_line(g, idx, games_ds), last_meeting(g)):
        if extra:
            L.append(extra)
    wp = (espn_notes or {}).get("espn_win_prob")
    if wp:
        L.append(f"  ESPN FPI: {home} {wp['home']:.0f}% to win")
    if (espn_notes or {}).get("weather"):
        L.append(f"  Weather (ESPN): {espn_notes['weather']}")
    L += news_lines(away, home, g["id"])
    return "\n".join(L)


def notable(games: list[dict], idx: dict, n: int = BOARD_ROWS, movers: bool = False) -> list[dict]:
    """The week's most notable games by combined SP+; with movers=True, plus every
    game whose line moved 3+ points from the open (that's where the news is)."""
    def sp(t):
        x = cfbd.resolve_team(t, idx)
        return ((x or {}).get("sp") or {}).get("rating") or -30
    top = sorted(games, key=lambda g: sp(g["homeTeam"]) + sp(g["awayTeam"]), reverse=True)[:n]
    if movers:
        for g in games:
            ln = _line(g) or {}
            if g not in top and ln.get("spreadOpen") is not None and abs(ln["spread"] - ln["spreadOpen"]) >= 3:
                top.append(g)
    return top


_INJ = re.compile(r"\b(injur\w*|out for (the )?season|ruled out|questionable|doubtful|torn|ACL|concussion|surgery|"
                  r"sling|ankle|knee|shoulder|elbow|QB|quarterback|start(s|er|ing)?|backup|suspend\w*|opt(s|ed)? out|"
                  r"transfer|fired|interim|hot seat)\b", re.I)


def news_lines(away: str, home: str, game_id=None) -> list[str]:
    """Personnel facts for this game: sourced slate intel (slate_intel.py, Opus
    offline, validated) plus team-tagged personnel headlines from the news feed.
    The only injury/QB facts Picky may state for this game."""
    intel = []
    try:
        import slate_intel
        claims, iasof = slate_intel.claims_for(game_id)
        intel = [f"    - {c['text']} ({c.get('source_name') or 'source'}, {c.get('date')})"
                 + (" [rumor]" if c.get("kind") == "rumor" else "") for c in claims[:5]]
    except Exception as e:  # noqa: BLE001
        log.warning("slate intel: %r", e)
    out = [f"  Availability (checked news, as of {iasof}):"] + intel if intel else []
    try:
        import news
        rows = news.news_for(None, teams=[away, home], limit=8)
        asof = news.as_of()
    except Exception as e:  # noqa: BLE001
        log.warning("slate news: %r", e)
        rows, asof = [], None
    rows = [r for r in rows if _INJ.search(r)][:4 if not intel else 2]
    if not rows and not intel:
        return [f"  News: nothing in the feed on {away} or {home}"
                + (f" (as of {asof})" if asof else "") + ". Injuries/QB status: unknown; say so, never guess."]
    if rows:
        out += [f"  Headlines (as of {asof}):"] + [f"    - {r}" for r in rows]
    return out + ["  These are the only injury/QB facts you know: report them as written. Anything else about a "
                  "player's status: unknown; say so."]


def board(top: list[dict], notes: dict) -> str:
    """Whole-slate questions: the notable games (by SP+) with line, move and total."""
    rows = []
    for g in top:
        m = market_line(g, (notes.get(g["id"]) or {}).get("market"))
        rows.append(f"- {g['awayTeam']} at {g['homeTeam']}: " + m[0].split(": ", 1)[-1]
                    + (f"; {m[1].strip()}" if len(m) > 1 else "")
                    + (f"; {m[2].strip()}" if len(m) > 2 and 'Key number' in m[2] else ""))
    return "THIS WEEK'S NOTABLE GAMES (market, computed; not all are pool games):\n" + "\n".join(rows)


def player_teams(question: str, idx: dict) -> list[str]:
    """'Is Sayin playing?': a capitalized surname that belongs to exactly one
    team's regular (QB/RB/WR with real volume) -> that team."""
    words = set(re.findall(r"\b([A-Z][a-z'’]{3,})\b", question or ""))
    words = {w.lower() for w in words} - cfbd.STOPWORDS
    teams = set()
    for p in idx.get("players") or []:
        k = (p.get("key") or "").split()
        if not k or k[-1] not in words:
            continue
        vol = sum(cfbd._volume(c, st) for c, st in (p.get("stats") or {}).items() if c in cfbd.STAT_ORDER)
        if vol >= 40:
            teams.add(p["team"])
    return sorted(teams) if len(teams) == 1 else []


_FOLLOW_UP = re.compile(r"^\W*(why|how come|explain|and\b|what about|best case|what would change|change your mind|"
                        r"other side|you sure|really|the other team|counter|devil'?s advocate|what if)\b", re.I)


async def slate_note(question: str, ctx: dict, prior: list[str] | None = None) -> str:
    """`prior`: the member's earlier texts in this chat, newest last. A short
    follow-up ("why?", "best case for the other side?") keeps the game card of
    the game they were just asking about."""
    q = question or ""
    if not q.strip():
        return ""
    if prior and len(q.split()) <= 12 and _FOLLOW_UP.search(q):
        q = f"{prior[-1]} {q}"
    try:
        idx = cfbd.load_index()
    except Exception:  # noqa: BLE001
        idx = None
    if not idx:
        return ""
    now = dt.datetime.now(dt.timezone.utc)
    games = upcoming_games(idx, now)
    if not games:
        return ""
    pool_ids = {int(g["id"]) for g in ctx.get("games_this_week") or [] if str(g.get("id", "")).isdigit()}
    named = cfbd.other_teams_named(q, [], idx) or player_teams(q, idx)
    picks = []
    for team in named:
        n = cfbd.norm(team)
        g = next((g for g in games if n in (cfbd.norm(g["homeTeam"]), cfbd.norm(g["awayTeam"]))), None)
        if g and g not in picks and g["id"] not in pool_ids:
            picks.append(g)
    whole = bool(_SLATE_ASK.search(q)) and not picks
    if not picks and not whole:
        return ""
    if picks and not _PICK_WORDS.search(q):
        return ""
    import espn
    chosen = picks[:MAX_CARDS] if picks else notable(games, idx)
    try:
        notes = await espn.game_notes([g["id"] for g in chosen])
    except Exception:  # noqa: BLE001
        notes = {}
    if whole:
        return board(chosen, notes)
    gds = _games_dataset()
    return "\n".join(card(g, idx, notes.get(g["id"]), gds) for g in picks[:MAX_CARDS])
