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
import market_open
import travel
import units
import weather

log = logging.getLogger("slate_cards")
CT = ZoneInfo("America/Chicago")
KEY_NUMBERS = (3, 7, 10, 14)
MAX_CARDS = 2
BOARD_ROWS = 12
HORIZON = dt.timedelta(days=9)

_SLATE_ASK = re.compile(  # a market-wide ask; 'who should I take this week?' is the pool's (the pick lane answers it)
    r"\b(key numbers?|hooks?|line moves?|lines? (moved|moving)|biggest (line )?moves?|lookahead|look-ahead|letdown|"
    r"sandwich|trap games?|the slate|notable games|around the country|other games)\b", re.I)
_PICK_WORDS = re.compile(
    r"\b(lines?|spreads?|cover\w*|ats|take|lay\w*|getting|value|picks?|who (you|ya|do you) (got|like|taking)|side|"
    r"points?|totals?|over|under|injur\w*|qbs?|quarterbacks?|backups?|matchups?|defen[cs]e|offen[cs]e|run\w*|pass\w*|rush\w*|weather|"
    r"wind\w*|cold|travel\w*|altitude|rest\w*|bye|motivat\w*|bowl|revenge|rivalry|history|last time|h2h|trends?|records?|"
    r"lookahead|letdown|key numbers?|hook|why|best case|change your mind|playing|starting|out|hurt|good|"
    r"chances?|beat|win\w*|lose|losing|score\w*|stop|handle|edge|coach\w*|fired|hot seat|mov(e|ed|ing)|happened|"
    r"open(ed)?|number|against the|too many|clock|flying|trip|time zone|kickoff|night game)\b"
    r"|(?<![\w.])[+-]\d{1,2}(\.5)?\b", re.I)


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
    try:
        tab = units.table()
    except Exception:  # noqa: BLE001
        tab = {}
    for team in (away, home):
        r = units._row(team, tab) if tab else None
        if r and r.get("ppo_o") and r.get("ppo_d"):  # every number with its FBS rank
            bits.append(f"{team} scores {r['ppo_o']:.1f} pts per scoring chance (#{r['ppo_o_rank']}), "
                        f"allows {r['ppo_d']:.1f} (#{r['ppo_d_rank']})")
    return "  Run game / finishing drives: " + "; ".join(bits) if bits else None


def unit_edges_line(away: str, home: str) -> str | None:
    """The clearest unit-vs-unit edges (units.py: ranked across FBS, decided in code), compact."""
    try:
        ls = units.card_lines(away, home)
    except Exception as e:  # noqa: BLE001
        log.warning("unit edges: %r", e)
        return None
    return ("  Unit edges (ranks among FBS, 1 = best; decided): " + "; ".join(ls)) if ls else None


def notable_trip(g: dict) -> str | None:
    """Travel / altitude only when it could matter (2+ time zones, a long trip, a stadium at altitude)."""
    out = []
    try:
        t = travel.tables()
        v, neutral = travel.game_venue(g.get("id"), g["homeTeam"], t)
        tr = travel.trip(g["awayTeam"], v, _sd(g), t)
        if tr and not tr.get("same_venue") and (abs(tr.get("zones") or 0) >= 2 or (tr.get("miles") or 0) >= 1500):
            out.append(travel.travel_line(g, t))
        if (travel.elev_ft(v) or 0) >= travel.HIGH_FT:
            out.append(travel.altitude_line(g, t))
    except Exception as e:  # noqa: BLE001
        log.warning("trip facts: %r", e)
    out = [x for x in out if x]
    return "\n".join(out) or None


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
            if now_utc <= s <= now_utc + HORIZON:  # kicked off: live_note / the live feed own it
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
                    "spread": my_sp, "cover": cm, "date": g.get("startDate", "")[:10], "start": g.get("startDate", "")})
    return out


def _rec(rows: list[dict]) -> str:
    c = sum(1 for r in rows if r["cover"] is not None and r["cover"] > 0)
    l_ = sum(1 for r in rows if r["cover"] is not None and r["cover"] < 0)
    p = sum(1 for r in rows if r["cover"] == 0)
    return f"{c}-{l_}" + (f"-{p}" if p else "")


def ats_line(team: str, rows: list[dict], t: dict | None, short: bool = False) -> str:
    graded = [r for r in rows if r["cover"] is not None]
    fav = [r for r in graded if r["spread"] < 0]
    dog = [r for r in graded if r["spread"] > 0]
    home = [r for r in graded if r["home"]]
    road = [r for r in graded if not r["home"]]
    rec = (t or {}).get("record")
    s = f"{team}{f' ({rec})' if rec else ''}: ATS {_rec(graded)}"
    s += f" (fav {_rec(fav)}, dog {_rec(dog)}, home {_rec(home)}, road {_rec(road)})"
    last = []
    for r in ([] if short else graded[-3:]):
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
    eo = market_open.early_open(g.get("lines")) if "DraftKings" in src else None
    if eo and opn is not None and abs(opn - eo["spread"]) <= 0.5:
        opn = eo["spread_open"]  # DK's look-ahead opener isn't a market: move from the other book's open
    else:
        eo = None
    fav, fsp = (home, now) if now < 0 else (away, -now)
    s = f"  Market ({src}): {_fmt_spread(fav, fsp)}"
    if opn is not None and opn != now:
        ofav, osp = (home, opn) if opn < 0 else (away, -opn)
        moved = abs(now - opn)
        toward = home if now < opn else away
        s += f", opened {_fmt_spread(ofav, osp)} (moved {moved:g} toward {toward})"
    elif opn is not None:
        s += " (no move from the open)"
    if eo:
        efav, esp = (home, eo["spread"]) if eo["spread"] < 0 else (away, -eo["spread"])
        s += (f". {market_open._pos(eo['book'])} opener {_fmt_spread(efav, esp)} was an early number, not a move: "
              f"{eo['vs']} opened {_fmt_spread(*((home, opn) if opn < 0 else (away, -opn)))}")
        if eo.get("total") is not None and eo.get("total_open") is not None and \
                abs(eo["total"] - eo["total_open"]) >= market_open.TOTAL_GAP:
            s += f" (early total {eo['total']:g}, {eo['vs']} opened {eo['total_open']:g})"
    out = [s]
    if total:
        hi = (total + abs(now)) / 2
        lo = total - hi
        tot = f"  Total {total:g}"
        if ln.get("overUnderOpen") and ln.get("overUnderOpen") != total and src.startswith("CFBD") and not eo:
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


def league_vs_market(g: dict, league_home: float, espn_market: dict | None) -> list[str]:
    """A pool game: the league's line (scored against it, locks Tue 11 AM CT) vs the market,
    who gets the better number and any key number in between, decided here."""
    away, home = g["awayTeam"], g["homeTeam"]
    m = (espn_market or {}).get("home_now")
    if m is None:
        m = (_line(g) or {}).get("spread")
    L = float(league_home)
    lfav, lsp = (home, L) if L < 0 else (away, -L)
    out = [f"  League line {_fmt_spread(lfav, lsp)} (locks Tue 11 AM CT)"]
    if m is None or float(m) == L:
        out[0] += " = the market now."
        return out
    out[0] = (f"  LEAGUE LINE (graded against this; locks Tue 11 AM CT, so before then say 'value if it holds through "
              f"the lock'): {_fmt_spread(lfav, lsp)}")
    m = float(m)
    mfav, msp = (home, m) if m < 0 else (away, -m)
    value = home if m < L else away
    out[0] += f" vs the market {_fmt_spread(mfav, msp)}: {value} backers get the better number by {abs(L - m):g}."
    lo, hi = sorted((abs(L), abs(m)))
    keys = [k for k in KEY_NUMBERS if lo < k <= hi] if lfav == mfav else []
    if keys:
        k = keys[0]
        out.append(f"  Key number: {k} sits between the league line and the market, so a {k}-point {lfav} win "
                   f"{'covers the league line but only pushes the market' if value == lfav else 'is a loss vs the league line'}.")
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
            # calendar days in CT (Sat night -> next Sat noon is 7 days' rest, not 6: genius ah-houston-fav)
            days = (s.astimezone(CT).date() - _sd(max(played, key=lambda x: x["startDate"])).astimezone(CT).date()).days
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
    aw = x["awayScore"] > x["homeScore"]
    win, lose = (x["awayTeam"], x["homeTeam"]) if aw else (x["homeTeam"], x["awayTeam"])
    hi, lo = max(x["awayScore"], x["homeScore"]), min(x["awayScore"], x["homeScore"])
    s = (f"  Last meeting ({x['season']}): {win} won {hi}-{lo} "
         f"{'at ' + x['homeTeam'] if aw else 'at home vs ' + x['awayTeam']}")
    if ln:
        cm = x["homeScore"] - x["awayScore"] + ln["spread"]
        hs = ln["spread"]
        fav, fsp = (x["homeTeam"], hs) if hs < 0 else (x["awayTeam"], -hs)
        cov = x["homeTeam"] if cm > 0 else x["awayTeam"]
        s += f", line {_fmt_spread(fav, fsp)}: {cov} covered by {abs(cm):g}"
    return s


_OTHER_SIDE = re.compile(r"\b(best case|other side|case for|devil'?s advocate|counter|argue)\b", re.I)
_CHANGE_MIND = re.compile(r"\b(change your mind|flip (you|it)|what would make you|wrong about)\b", re.I)


def brief_for(g: dict, idx: dict, espn_notes: dict | None) -> dict | None:
    """pick_brief's factor engine (the pool cards' ATS lean) run against the current MARKET line, since a
    non-pool game has no league line. None when there's no line or the engine fails."""
    try:
        import news
        import pick_brief
        import slate_intel
        mk = (espn_notes or {}).get("market") or {}
        home_now = mk.get("home_now")
        if home_now is None:
            ln = _line(g) or {}
            home_now = ln.get("spread")
            if home_now is not None:  # one market for both sides: without it pick_brief read CFBD's daily copy as
                # 'the market' against this line as 'the league', a phantom 'line value' on a non-pool game
                # (genius 11-26 lv-tex-tamu-move: 'the league has Texas -10.5' with no league line)
                espn_notes = dict(espn_notes or {}, market={
                    "home_now": home_now, "home_open": ln.get("spreadOpen"), "over_under": ln.get("overUnder"),
                    "book": ln.get("provider") or "market"})
        if home_now is None:
            return None
        away, home = g["awayTeam"], g["homeTeam"]
        kick = _sd(g).astimezone(CT).replace(tzinfo=None).isoformat() if _sd(g) else None
        cg = {"id": g["id"], "away": away, "home": home, "line": home_now, "start": kick,
              "season": g.get("season"), "week": g.get("week"), "fpi_margin": None}
        claims, _ = slate_intel.claims_for(g["id"])
        intel = [{"team": away if c.get("team") == "away" else home, "text": c.get("text"), "kind": c.get("kind"),
                  "status": c.get("status"), "date": c.get("date"), "source": c.get("source_name")}
                 for c in claims if c.get("team") in ("away", "home")]
        intel += pick_brief.news_intel(news.load_cache(), [away, home])
        return pick_brief.build(cg, espn_notes, idx, {}, intel)
    except Exception as e:  # noqa: BLE001 - the card works without a lean
        log.warning("slate lean failed: %r", e)
        return None


def find_game(text: str, ctx: dict, idx: dict | None = None) -> tuple[dict | None, dict | None]:
    """(CFBD game row, pool game or None) for the upcoming game a text is about: a pool game named the way
    chat names them, else a game between two FBS teams it names, else the first named team's game."""
    import chat
    idx = idx or cfbd.load_index()
    if not idx:
        return None, None
    games = upcoming_games(idx, dt.datetime.now(dt.timezone.utc))
    pool = {int(g["id"]): g for g in ctx.get("games_this_week") or [] if str(g.get("id", "")).isdigit()}
    hit = chat.games_named(text or "", [{"Away": g["away"], "Home": g["home"], "id": g["id"]}
                                        for g in ctx.get("games_this_week") or [] if not g.get("tiebreaker")])
    if hit and str(hit[0]["id"]).isdigit():
        gid = int(hit[0]["id"])
        pg = pool.get(gid)
        row = next((x for x in games if x["id"] == gid), None) or {
            "id": gid, "awayTeam": pg["away"], "homeTeam": pg["home"], "startDate": None, "lines": []}
        return row, pg
    named = cfbd.other_teams_named(text or "", [], idx)
    ms = match_games(named, games, set()) or (match_games(named[:1], games, set()) if named else [])
    return (ms[0], pool.get(ms[0]["id"])) if ms else (None, None)


def lean_lines(g: dict, idx: dict, espn_notes: dict | None, ask: str = "") -> list[str]:
    """pick_brief's factor engine (the pool cards' ATS lean) run against the
    current MARKET line, since a non-pool game has no league line."""
    b = brief_for(g, idx, espn_notes)
    if not b:
        return []
    away, home = g["awayTeam"], g["homeTeam"]
    conf = {"strong": "strong lean", "lean": "lean", "none": "coin flip"}[b["strength"]]
    sc = b["score"]
    out = [f"  COMPUTED ATS LEAN (vs the current market line): {b['pick_label']} "
           f"({conf}; factor score {away} {sc[away]:g}, {home} {sc[home]:g}). Who COVERS, not who wins."]
    out += [f"    + for {f['side']}: {f['why']}" for f in b["top"][:4]]
    out += [f"    ! red flag: {f['why']}" for f in b["flags"][:2]]
    other = away if b["pick"] == home else home
    if _OTHER_SIDE.search(ask or ""):
        case = sorted((f for f in b["factors"] if f["side"] == other and f["w"] > 0), key=lambda f: -f["w"])[:4]
        out.append(f"  THEY WANT THE CASE FOR {other.upper()} (the side the lean is against): argue it honestly from "
                   "these, don't repeat the other side's reasons:")
        out += [f"    - {f['why']}" for f in case] or ["    - (no computed factor favors it: say so)"]
    elif _CHANGE_MIND.search(ask or ""):
        flips = sorted((f for f in b["factors"] if f["side"] == other and f["w"] > 0), key=lambda f: -f["w"])[:2]
        out.append("  WHAT WOULD FLIP IT: an availability change in the news lines below, the line moving across a key "
                   "number, plus these factors for " + other + ": " + "; ".join(f["why"] for f in flips))
    return out


SUB_ASK = {  # the situational sub-question a text asks (genius exam: answer exactly this, not a general read)
    "lookahead": re.compile(r"\b(look ?-?ahead|letdown|let down|sandwich|trap game|trap spot|next week|looking ahead|"
                            r"overlook\w*|peek\w* ahead|after (this|that) (game|week)|on deck)\b", re.I),
    "travel": re.compile(r"\b(body clock|time zones?|timezone|travel\w*|flying|fly\w*|flight|cross.country|jet ?lag|"
                         r"long trip|road trip|west coast|east coast|early kick\w*|noon kick\w*|11 ?am kick\w*|"
                         r"miles|trip)\b", re.I),
    "altitude": re.compile(r"\b(altitude|elevation|thin air|mile high|high altitude|feet above|oxygen|laramie)\b", re.I),
    "weather": re.compile(r"\b(weather|wind\w*|rain\w*|snow\w*|cold|freez\w*|temperature|temps?|forecast|storm\w*|"
                          r"sleet|wet|elements|gusts?|dome|indoors)\b", re.I),
    "rest": re.compile(r"\b(rest(ed)?|bye( week)?|short week|extra week|days off|fresh(er)?|tired|fatigue)\b", re.I),
    "motivation": re.compile(r"\b(motivat\w*|stakes|must[- ]win|bowl[- ]eligib\w*|bowl (spot|game|hopes)|something to play for|"
                             r"nothing to play for|play(ing)? for|clinch\w*|title[- ]game spot|desperat\w*|angles?)\b", re.I),
}


def motivation_line(g: dict, idx: dict) -> str | None:
    """Stakes in decided words: bowl eligibility (6 wins) and the conference title-game race, from CFBD records
    and conference standings (top 2 by conference record make the title game)."""
    bits = []
    for team in (g["awayTeam"], g["homeTeam"]):
        t = cfbd.resolve_team(team, idx) or {}
        rec, conf = t.get("record"), t.get("conf_record")
        if not rec:
            continue
        w, l = (int(x) for x in rec.split("-")[:2])
        b = f"{team} {rec}" + (f" ({conf} {t.get('conference')})" if conf else "")
        if w == 5:
            b += ": one win from bowl eligibility (6 wins)"
        elif w < 5 and w + (12 - w - l) >= 6:
            b += f": needs {6 - w} more wins for a bowl"
        elif w < 5:
            b += ": can't reach 6 wins, no bowl on the line"
        else:
            b += ": already bowl eligible"
        try:
            note = conference_note(f"{t.get('conference') or ''} standings")
            rows = re.findall(r"^\s+(\d+)\. (.+?) (\d+-\d+) \(", note or "", re.M)
            pos = next((int(r[0]) for r in rows if r[1] == team), None)
            if pos == 1:
                b += f", leads the {t.get('conference')} (a win keeps the title-game spot in hand)"
            elif pos == 2:
                b += f", 2nd in the {t.get('conference')} (title-game spot on the line)"
            elif pos == 3:
                b += f", 3rd in the {t.get('conference')}, chasing a title-game spot"
        except Exception:  # noqa: BLE001
            pass
        bits.append(b)
    return ("  Motivation / stakes: " + "; ".join(bits) + ".") if bits else None


def sub_asks(q: str) -> list[str]:
    t = (q or "").replace("’", "'")
    return [k for k, rx in SUB_ASK.items() if rx.search(t)]


def _sp_rank(team: str, idx: dict) -> int | None:
    return ((cfbd.resolve_team(team, idx) or {}).get("sp") or {}).get("rank")


def lookahead_line(g: dict, idx: dict, home_now: float | None = None) -> str | None:
    """Lookahead / letdown / sandwich in decided words: each team's last and next game (with SP+ ranks), who's
    favored here, and whether the spot is real. Big game = SP+ top 25 (pick_brief's BIG_GAME_SP_RANK)."""
    s = _sd(g)
    if s is None:
        return None
    away, home = g["awayTeam"], g["homeTeam"]
    if home_now is None:
        home_now = (_line(g) or {}).get("spread")
    fav = (home if home_now < 0 else away if home_now > 0 else None) if home_now is not None else None
    bits, flags = [], []
    for team in (away, home):
        t = cfbd.resolve_team(team, idx)
        if not t:
            continue
        opp_now = home if team == away else away
        rn = _sp_rank(opp_now, idx)
        gs = [x for x in t.get("games") or [] if _sd(x)]
        played = [x for x in gs if x.get("homeScore") is not None and _sd(x) < s]
        nxt = sorted((x for x in gs if _sd(x) > s), key=lambda x: x["startDate"])
        b = team
        if played:
            x = max(played, key=lambda x: x["startDate"])
            at_home = cfbd.norm(x["homeTeam"]) == cfbd.norm(team)
            mine, theirs = (x["homeScore"], x["awayScore"]) if at_home else (x["awayScore"], x["homeScore"])
            lo = x["awayTeam"] if at_home else x["homeTeam"]
            ln = _line(x)
            my_sp = ((ln["spread"] if at_home else -ln["spread"]) if ln and ln.get("spread") is not None else None)
            rl = _sp_rank(lo, idx)
            won = mine > theirs
            b += (f": last {'beat' if won else 'lost to'} {lo}{f' (SP+ #{rl})' if rl else ''} {mine}-{theirs}"
                  + (f" as a {my_sp:g}-point underdog" if won and my_sp and my_sp > 0 else ""))
            if team == fav and won and ((my_sp or 0) > 0 or (rl and rl <= 25)):
                flags.append(f"letdown spot for {team} (favored here after "
                             + (f"an outright upset as a {my_sp:g}-point dog" if (my_sp or 0) > 0 else f"beating SP+ #{rl} {lo}") + ")")
        if nxt:
            n = nxt[0]
            no = n["awayTeam"] if cfbd.norm(n["homeTeam"]) == cfbd.norm(team) else n["homeTeam"]
            rx = _sp_rank(no, idx)
            b += (f"; next {'vs' if cfbd.norm(n['homeTeam']) == cfbd.norm(team) else 'at'} {no}"
                  + (f" (SP+ #{rx})" if rx else ""))
            try:
                import rivalries
                riv = rivalries.rivalry(team, no) if not rivalries.rivalry(away, home) else None
            except Exception:  # noqa: BLE001
                riv = None
            if riv:
                b += f", a rivalry game ({riv})"
            big = (rx and rx <= 25 and (rn is None or rn > rx)) or riv
            what = f"SP+ #{rx} {no}" if rx and rx <= 25 else f"rivalry game vs {no} ({riv})"
            if big and team == fav:
                flags.append(f"lookahead spot for {team} (favored here, {what} next)")
            elif big:
                flags.append(f"{team} has {what} next but is the underdog here, so lookahead matters less")
        else:
            b += "; no game after this one on the schedule"
        bits.append(b)
    if not bits:
        return None
    verdict = "; ".join(flags) if flags else "no lookahead or letdown setup on paper"
    return "  Lookahead / letdown: " + "; ".join(bits) + f". Verdict: {verdict}."


def sub_lines(q: str, g: dict, idx: dict, games_ds: dict, home_now: float | None = None) -> list[str]:
    """The fact(s) for the exact situational sub-question asked, decided in code."""
    out = []
    for k in sub_asks(q):
        try:
            if k == "lookahead":
                x = lookahead_line(g, idx, home_now)
            elif k == "travel":
                x = travel.travel_line(g) or "  Travel: no trip data for this game."
            elif k == "altitude":
                x = travel.altitude_line(g)
            elif k == "weather":
                x = weather.line(g["id"]) or ("  Weather: no forecast on file for this game yet (it refreshes every "
                                              "6 hours inside the forecast window); say you don't have one, never guess.")
            elif k == "motivation":
                x = motivation_line(g, idx)
            else:
                x = rest_line(g, idx, games_ds)
        except Exception as e:  # noqa: BLE001 - a bad fact never breaks the card
            log.warning("sub-question %s failed: %r", k, e)
            x = None
        if x and x.strip() not in (y.strip() for y in out):
            out.append(x)
    return out[:3]


def ask_block(q: str, g: dict, idx: dict, games_ds: dict, home_now: float | None = None) -> tuple[list[str], list[str]]:
    """(lines that go FIRST on the card, the reminder labels) for a unit or situational sub-question."""
    away, home = g["awayTeam"], g["homeTeam"]
    top, labels = [], []
    try:
        us = units.ask_units(q)
        ul = units.question_lines(q, away, home, units=us) if us else []
    except Exception as e:  # noqa: BLE001
        log.warning("unit edges failed: %r", e)
        us, ul = [], []
    if ul:
        top.append("  UNIT QUESTION (answer with this, not overall SP+; ranks among FBS, 1 = best, edges decided in code):")
        top += ["    " + x for x in ul]
        labels += [units.LABEL[u] for u in us]
    sl = sub_lines(q, g, idx, games_ds, home_now)
    if sl:
        top.append("  SUB-QUESTION (they asked exactly this: answer it first, from these facts):")
        top += ["    " + x.strip() for x in sl]
        labels += sub_asks(q)
    return top, labels


def _closer(labels: list[str]) -> str:
    return (f"  THEY ASKED ABOUT {', '.join(dict.fromkeys(labels)).upper()}: lead with the UNIT QUESTION / SUB-QUESTION "
            "lines at the top of this card, in decided words; the rest only if it adds something.")


def card(g: dict, idx: dict, espn_notes: dict | None, games_ds: dict, ask: str = "",
         league_home: float | None = None) -> str:
    away, home = g["awayTeam"], g["homeTeam"]
    meta = games_ds.get(g["id"]) or {}
    s = _sd(g)
    when = s.astimezone(CT).strftime("%a %b %-d %-I:%M %p CT") if s else "TBD"
    where = meta.get("venue") or ""
    head = (f"GAME CARD (computed from CFBD + ESPN; answer their football question from it, straight, and don't "
            f"bring up whether it's a pool game): "
            f"{away} at {home}{' (neutral site)' if meta.get('neutralSite') else ''}, {when}{', ' + where if where else ''}.")
    mk_home = ((espn_notes or {}).get("market") or {}).get("home_now")
    top, labels = ask_block(ask, g, idx, games_ds, league_home if league_home is not None else mk_home)
    if league_home is not None:  # pool game: the pool's game card has the market, factors and lean; add only what it lacks
        # a unit / sub-question: 'GAME CARD (' so the understand pass sees it too (picky_brain.slate_block)
        L = [f"GAME CARD (extras, CFBD/ESPN): {away} at {home}." if top else
             f"GAME CARD extras (CFBD/ESPN): {away} at {home}."] + top
        L += league_vs_market(g, league_home, (espn_notes or {}).get("market"))
        for team in (away, home):
            L.append("  " + ats_line(team, team_rows(team, idx, g["startDate"]), cfbd.resolve_team(team, idx), short=True))
        lm = last_meeting(g)
        if lm:
            L.append(lm)
        nl = news_lines(away, home, g["id"])
        return "\n".join(L + [x for x in nl if not x.startswith("  These are")][:3] + ([_closer(labels)] if labels else []))
    L = [head] + top + market_line(g, (espn_notes or {}).get("market"))
    for team in (away, home):
        t = cfbd.resolve_team(team, idx)
        L.append("  " + ats_line(team, team_rows(team, idx, g["startDate"]), t))
        sp = (t or {}).get("sp") or {}
        if sp.get("rank"):
            L[-1] += f"; SP+ #{sp['rank']} (off #{sp.get('off_rank')}, def #{sp.get('def_rank')})"
    for extra in (style_line(away, home, idx), unit_edges_line(away, home), rest_line(g, idx, games_ds), last_meeting(g),
                  None if set(labels) & {"travel", "altitude"} else notable_trip(g)):
        if extra and extra.strip() not in "\n".join(top):
            L.append(extra)
    if league_home is None:  # pool games: the PICK BRIEF / game card owns the lean (vs the league line)
        L += lean_lines(g, idx, espn_notes, ask)
    wp = (espn_notes or {}).get("espn_win_prob")
    if wp:
        L.append(f"  ESPN FPI: {home} {wp['home']:.0f}% to win")
    if (espn_notes or {}).get("weather"):
        L.append(f"  Weather (ESPN): {espn_notes['weather']}")
    wl = weather.line(g["id"])
    if wl and "weather" not in labels:
        L.append(wl)
    L += news_lines(away, home, g["id"])
    return "\n".join(L + ([_closer(labels)] if labels else []))


def _pool_line(pg: dict | None) -> float | None:
    try:
        return float(pg["line"]) if pg and pg.get("line") is not None else None
    except (TypeError, ValueError):
        return None


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
    return "THIS WEEK'S NOTABLE GAMES (current market, computed):\n" + "\n".join(rows)


def match_games(named: list[str], games: list[dict], pool_ids: set) -> list[dict]:
    """Upcoming non-pool games for the named teams. Two or more teams named: only a
    game between two of them ('Ohio State vs Oregon' isn't Ohio State at Nebraska)."""
    ns = [cfbd.norm(t) for t in named]
    def teams_of(g):
        return {cfbd.norm(g["homeTeam"]), cfbd.norm(g["awayTeam"])}
    if len(ns) >= 2:
        out = [g for g in games if len(teams_of(g) & set(ns)) >= 2]
    else:  # one team: its soonest game only. The 9-day horizon reaches next week by midweek, and 'Is 16.5 too many
        # for Georgia?' got Georgia Tech-Georgia's card next to Georgia-South Carolina's (genius pb-uga 11-19)
        out = sorted((g for g in games if set(ns) & teams_of(g)), key=lambda g: g.get("startDate") or "")[:1]
    return [g for g in out if g["id"] not in pool_ids]


def games_for(named: list[str], games: list[dict], pool_ids: set, q: str = "") -> list[dict]:
    """match_games, plus: 'Is Iowa looking ahead to Nebraska?' names two teams that don't play each other this
    week; a lookahead/letdown ask is about the FIRST team named's game (model check 2026-11-17: no card, so the
    pool guard said 'Nebraska isn't in the pool')."""
    out = match_games(named, games, pool_ids)
    if out or len(named) < 2 or "lookahead" not in sub_asks(q):
        return out
    first = min(named, key=lambda t: (units._pos(t, q) if units._pos(t, q) >= 0 else 10 ** 6))
    return match_games([first], games, pool_ids)


def football_ask(q: str) -> bool:
    """A question about the game (any '?' or betting/football words), not a statement like 'Notre Dame is back.'"""
    return "?" in (q or "") or bool(_PICK_WORDS.search(q or ""))


def covers(question: str, ctx: dict) -> bool:
    """Cheap, sync (no network): would slate_note put a GAME CARD on this text?
    chat.guard_note uses it to skip its 'NOT IN THE POOL: no spread, no pick' rider."""
    try:
        idx = cfbd.load_index()
    except Exception:  # noqa: BLE001
        return False
    if not idx or not football_ask(question):
        return False
    games = upcoming_games(idx, dt.datetime.now(dt.timezone.utc))
    pool_ids = {int(g["id"]) for g in ctx.get("games_this_week") or [] if str(g.get("id", "")).isdigit()}
    named = cfbd.other_teams_named(question, [], idx) or player_teams(question, idx)
    if games_for(named, games, pool_ids, question):
        return True
    # 'Is Iowa looking ahead to Nebraska?': the card is the POOL game's (extras with the SUB-QUESTION line), and the
    # other team named is next week's opponent, not a non-pool pick: no NOT-IN-POOL rail either
    return bool((sub_asks(question) or units.ask_units(question)) and len(named) >= 2
                and any(g["id"] in pool_ids for g in games_for(named, games, set(), question)))


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


_COACH_ASK = re.compile(r"\b(coach\w*|hc|head man|fired|firing|hot seat|carousel|interim|hire[sd]?)\b", re.I)
_COACH_NEWS = re.compile(r"\b(fired|firing|interim|hires?|hired|hot seat|out as|parts ways|resign\w*|steps? down|"
                         r"buyout|coach)\b", re.I)


def _coaches() -> tuple[list, list]:
    try:
        db = sqlite3.connect(cfbd.DB_PATH)
        cur = db.execute("select json from datasets where name='coaches'").fetchone()
        prev = db.execute("select json from datasets where name='coaches_prev'").fetchone()
        db.close()
        return (json.loads(cur[0]) if cur else []), (json.loads(prev[0]) if prev else [])
    except Exception:  # noqa: BLE001
        return [], []


def coach_note(question: str, idx: dict, teams: list[str]) -> str:
    """Who coaches whom (CFBD's season list, this year + last) for a question
    that names a coach or asks about a team's coach, plus in-season firings /
    interims from the news feed (CFBD's list doesn't drop a coach fired midseason).
    A name that coached a school last year but not this year gets an explicit
    'not their coach anymore', so a false premise gets corrected."""
    q = question or ""
    cur, prev = _coaches()
    if not cur:
        return ""
    def full(c):
        return f"{c['firstName']} {c['lastName']}"
    def schools(c, yr):
        return [x["school"] for x in c.get("seasons") or [] if x.get("year") == yr]
    year = max((x.get("year") or 0) for c in cur for x in c.get("seasons") or [])
    people = {}
    for c in cur + prev:
        people.setdefault(full(c).lower(), c)
    last_count = {}
    for name in people:
        last_count[name.split()[-1]] = last_count.get(name.split()[-1], 0) + 1
    ql = q.lower()
    named = [n for n in people if n in ql]
    caps = {w.lower() for w in re.findall(r"\b([A-Z][a-zA-Z'’-]{4,})\b", q)}
    named += [n for n in people if n not in named and n.split()[-1] in caps and last_count[n.split()[-1]] == 1
              and not cfbd.resolve_team(n.split()[-1], idx)]
    if not named and not (teams and _COACH_ASK.search(q)):
        return ""
    cur_by_school = {}
    for c in cur:
        for sch in schools(c, year):
            cur_by_school.setdefault(sch, []).append(c)
    try:
        import slate_intel
        changes = slate_intel.coaching_changes()
    except Exception:  # noqa: BLE001
        changes = {}
    lines, about = [], list(teams)
    for n in named[:3]:
        c = people[n]
        now_at = [x for c2 in cur if full(c2).lower() == n for x in schools(c2, year)]
        was_at = [x for c2 in prev if full(c2).lower() == n for x in schools(c2, year - 1)]
        if now_at:
            lines.append(f"- {full(c)}: head coach at {', '.join(now_at)} in {year}"
                         + ("" if any(cfbd.norm(x) in changes for x in now_at) else
                            ", still the coach (no firing or departure in the sourced coaching-change list)") + ".")
            about += now_at
        elif was_at:
            for sch in was_at:
                now = cur_by_school.get(sch) or []
                lines.append(f"- {full(c)} is NOT {sch}'s head coach in {year} (he was in {year - 1}). "
                             f"{sch}'s coach: {', '.join(full(x) + ' (hired ' + x['hireDate'][:10] + ')' for x in now) or 'unknown'}"
                             + ("" if cfbd.norm(sch) in changes else ", still the coach (no firing in the sourced list)") + "."
                             " If they say otherwise, correct them.")
                about.append(sch)
    for t in teams:
        ch = changes.get(cfbd.norm(t))
        if ch:  # sourced in-season change beats CFBD's season-start list
            lines += [f"- {t}: {c['text']} ({c.get('source_name')}, {c.get('date')})" for c in ch[:2]]
            continue
        for c in cur_by_school.get(t, []):
            hd = c.get("hireDate", "")[:10]
            first = hd >= f"{year - 1}-08-01"
            lines.append(f"- {t}: {full(c)} (hired {hd}{', first season' if first else ''}; still the coach, no firing "
                         "in the sourced coaching-change list).")
    news_rows = []
    try:
        import news
        for r in news.news_for(None, teams=list(dict.fromkeys(about))[:3], limit=10):
            if _COACH_NEWS.search(r):
                news_rows.append(f"  - {r}")
    except Exception:  # noqa: BLE001
        pass
    if not lines and not news_rows:
        return ""
    return ("HEAD COACHES (CFBD's season list + sourced in-season changes; a headline about firing talk is NOT a "
            "firing):\n" + "\n".join(dict.fromkeys(lines))
            + ("\n  Coaching headlines (takes and rumors, not news of a firing unless one says '<school> fired'):\n"
               + "\n".join(news_rows[:3]) if news_rows else ""))


CONFS = {"big 12": "Big 12", "big12": "Big 12", "big ten": "Big Ten", "big 10": "Big Ten", "b1g": "Big Ten",
         "sec": "SEC", "acc": "ACC", "mountain west": "Mountain West", "mwc": "Mountain West",
         "american": "American Athletic", "aac": "American Athletic", "sun belt": "Sun Belt", "mac": "Mid-American",
         "c-usa": "Conference USA", "conference usa": "Conference USA", "cusa": "Conference USA", "pac-12": "Pac-12",
         "pac 12": "Pac-12"}
_CONF_ASK = re.compile(r"\b(title|champ\w*|race|standings?|lead\w*|control|picture|first place|clinch\w*|"
                       r"scenarios?|who'?s in)\b", re.I)
_CFP_ASK = re.compile(r"\b(college football playoff|cfp|committee|playoff (picture|rankings?|bracket|field|race)|"
                      r"12-team|bracket|top 12|in the playoff)\b", re.I)


def conference_note(question: str) -> str:
    """'Big 12 title race?': the conference table from CFBD records (conference W-L, then overall)."""
    q = (question or "").lower()
    conf = next((v for k, v in sorted(CONFS.items(), key=lambda x: -len(x[0])) if re.search(rf"\b{re.escape(k)}\b", q)), None)
    if not conf or not _CONF_ASK.search(q):
        return ""
    try:
        db = sqlite3.connect(cfbd.DB_PATH)
        recs = json.loads(db.execute("select json from datasets where name='records'").fetchone()[0])
        db.close()
    except Exception:  # noqa: BLE001
        return ""
    rows = []
    for r in recs:
        if r.get("conference") != conf:
            continue
        c, t = r.get("conferenceGames") or {}, r.get("total") or {}
        cw, cl = c.get("wins", 0), c.get("losses", 0)
        rows.append(((cw / max(1, cw + cl)), cw, -cl, r["team"], f"{cw}-{cl}", f"{t.get('wins', 0)}-{t.get('losses', 0)}"))
    if not rows:
        return ""
    rows.sort(reverse=True)
    return (f"{conf.upper()} STANDINGS (conference record first, then overall; CFBD, through last week; the title game "
            f"takes the top 2; tiebreakers not computed here, so say 'tiebreakers' rather than guessing one):\n"
            + "\n".join(f"  {i}. {t} {c} ({o})" for i, (_, _, _, t, c, o) in enumerate(rows[:8], 1)))


def cfp_note(question: str) -> str:
    """The committee's latest CFP rankings from ESPN, served only after polls.sanity passed (fact store)."""
    if not _CFP_ASK.search(question or ""):
        return ""
    try:
        import factstore
        c = factstore.rankings("CFP")
    except Exception:  # noqa: BLE001
        c = None
    if not c:
        return ("CFP COMMITTEE RANKINGS: not in your data right now. Don't guess a ranking or a field; say you don't "
                "have the latest committee top 12.")
    top = "; ".join(f"{x['rank']} {x['team']}" + (f" ({x['record']})" if x.get("record") else "") for x in c["ranks"][:12])
    return (f"CFP COMMITTEE RANKINGS (released {c.get('date')}, per {c.get('source', 'ESPN')}): {top}. "
            "12-team field: the top-ranked teams as the committee seeds them; "
            "don't invent bracket matchups or a format detail you don't see here.")


_FOLLOW_UP = re.compile(r"\b(why|how come|explain|and\b|what about|best case|what would change|change your mind|"
                        r"other side|you sure|really|the other team|counter|devil'?s advocate|what if)\b", re.I)


async def slate_note(question: str, ctx: dict, prior: list[str] | None = None) -> str:
    """`prior`: the member's earlier texts in this chat, newest last. A short
    follow-up ("why?", "best case for the other side?") keeps the game card of
    the game they were just asking about."""
    q = question or ""
    if not q.strip():
        return ""
    try:
        idx = cfbd.load_index()
    except Exception:  # noqa: BLE001
        idx = None
    if not idx:
        return ""
    ask = q
    if prior and len(q.split()) <= 12 and _FOLLOW_UP.search(q) and not cfbd.other_teams_named(q, [], idx):
        base = next((p for p in reversed(prior) if cfbd.other_teams_named(p, [], idx)), None)
        if base:  # 'why?' then 'best case for the other side?': the game is in the earlier text
            q = f"{base} {q}"
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
    pool = {int(g["id"]): g for g in ctx.get("games_this_week") or [] if str(g.get("id", "")).isdigit()}
    named = cfbd.other_teams_named(q, [], idx) or player_teams(q, idx)
    coach = "\n".join(x for x in (coach_note(q, idx, named), conference_note(q), cfp_note(q)) if x)
    picks = games_for(named, games, set(), q)  # pool games too: the card adds the move, splits, h2h, news
    whole = bool(_SLATE_ASK.search(q)) and not picks
    if (not picks and not whole) or (picks and not football_ask(q)):
        return coach
    import espn
    chosen = picks[:MAX_CARDS] if picks else notable(games, idx)
    try:
        notes = await espn.game_notes([g["id"] for g in chosen])
    except Exception:  # noqa: BLE001
        notes = {}
    if whole:
        return board(chosen, notes) + (f"\n{coach}" if coach else "")
    gds = _games_dataset()
    return "\n".join([card(g, idx, notes.get(g["id"]), gds, ask, _pool_line(pool.get(g["id"]))) for g in picks[:MAX_CARDS]] + ([coach] if coach else []))
