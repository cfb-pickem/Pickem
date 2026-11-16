"""
Game cards: Picky's pick advice for each slate game, built OFFLINE and cached
(Charlie, 2026-11-16: "the line is -3.5 but the qb got injured so it moved to
-4.5. Since the league locks lines at noon tuesday, you can get extra value
out of this ... Your key opponents are also likely to take Y team based on
their past tendencies. I'd take X team if I were you." and "the whole league
is about covering").

One JSON card per pool game in cache/game_cards/<week>/<game_id>.json:
  - the league line (frozen Tue 11:00 AM CT, the site's line_lock_at) vs the
    current market line, the value gap and any key number it crosses
  - the line move (open -> now) and its likely cause from the game dossier /
    news (QB injury etc.), only when a dated report exists
  - the factors that have a live data source (pick_brief.py; the rest are
    "no source" in docs/picky-pick-factors.md and are never guessed), the
    computed lean ("I'd take X -3.5") with 2-4 true reasons and red flags
  - team head-to-head vs this opponent (last season + this one)
  - per member: game theory from rivals' past tendencies on GRADED picks only
    (lore.load reads picks only for graded games, so unrevealed picks can't
    get in) + the member's standings need (chase vs protect)
  - confidence worded from the edge size and the backtest below
  - the written case / counter-case from Opus via analyst.py's prompt and
    validator (claude -p, picky_claude.MODEL, --strict-mcp-config with the
    empty MCP config, no tools). Anthropic down = the previous case, else the
    dossier's analyst take, else none: the card still works from code.

Live chat never computes or calls anything here: chat.py reads the card and
puts card_note() in the LATEST user turn (never prompt_prefix), and gemma only
phrases it.

Schedule (com.pickem.game-cards.plist, every 30 min; a run is a no-op unless
something is due): "new" (slate just set or a game added), "lock" (first run
after the Tue 11:00 CT line lock), "fri" (Fri 5 PM CT), "sat" (Sat 7 AM CT),
"dossier" (the game's dossier was rebuilt after the card). Never for a game
that has kicked off.

Backtest (`game_cards.py backtest`, and automatically when a run builds
cards and the last one is over 6 days old): every graded 2025-26 pool game
(league line + final in all_games, the market's closing line from CFBD).
Written to cache/game_cards/backtest.json; the card's confidence wording
reads it.

CLI: game_cards.py run [--force] [--week N] [--now ISO] [--no-opus]
     game_cards.py backtest
     game_cards.py show [--week N]
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import datetime as dt
import hashlib
import json
import logging
import math
import os
import re
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))  # launchd runs python3 -I (no script dir on the path)
import pick_brief  # noqa: E402

log = logging.getLogger("picky.game_cards")
CT = ZoneInfo("America/Chicago")
CARD_DIR = Path(os.environ.get("PICKY_GAME_CARD_DIR", HERE / "cache" / "game_cards"))
BACKTEST_FILE = CARD_DIR / "backtest.json"
NEWS_FILE = HERE / "cache" / "news.json"
CARD_MAX_AGE = dt.timedelta(days=8)
BACKTEST_MAX_AGE = dt.timedelta(days=6)
MAX_REASONS, MIN_REASONS = 4, 2
SEASON_WEEKS = 15
# wording thresholds (points of line value; backtest bucket win rate and size)
EDGE_BIG, EDGE_SOME = 1.5, 0.5
BT_GOOD_PCT, BT_MIN_N = 0.55, 20


def now_ct() -> dt.datetime:
    return dt.datetime.now(CT)


def _kick(g: dict) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(str(g.get("Start (CT)") or g.get("start"))).replace(tzinfo=CT)
    except (TypeError, ValueError):
        return None


def _last(now: dt.datetime, weekday: int, hour: int) -> dt.datetime:
    """Most recent `weekday` at `hour`:00 CT at or before now."""
    d = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    d -= dt.timedelta(days=(d.weekday() - weekday) % 7)
    return d - dt.timedelta(days=7) if d > now else d


def _n(x: float) -> str:
    return f"{x:g}"


def _wilson(w: int, n: int) -> tuple[float, float]:
    if not n:
        return 0.0, 0.0
    z, p = 1.96, w / n
    c = (p + z * z / (2 * n)) / (1 + z * z / n)
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return round(c - h, 3), round(c + h, 3)


# -----------------------------------------------------------------------------
# league history: graded picks only (game theory + backtest)
# -----------------------------------------------------------------------------

def history_rows(data: dict) -> list[dict]:
    """Graded pool picks as tendency rows {team, pick, fav, home, crowd, season, week}.
    data = lore.load(): picks are fetched only for games with a graded winner."""
    name = {t["team_id"]: t["team_name"] for t in data["teams"] if not t.get("is_exhibition")}
    games = {g["GameId"]: g for g in data["games"] if g.get("winner") and not g.get("tiebreaker")
             and g.get("line") is not None}
    by_game = collections.defaultdict(list)
    for p in data["picks"]:
        g = games.get(p["game_id"])
        if g and p["team_id"] in name and p.get("pick"):
            by_game[g["GameId"]].append((name[p["team_id"]], p["pick"]))
    out = []
    for gid, picks in by_game.items():
        g = games[gid]
        ln = float(g["line"])
        fav = g["Home"] if ln < 0 else g["Away"] if ln > 0 else None
        crowd = collections.Counter(pk for _, pk in picks).most_common(1)[0][0]
        for team, pk in picks:
            out.append({"team": team, "pick": pk, "fav": fav, "home": g["Home"], "crowd": crowd,
                        "season": g["cfb_season"], "week": g["week"]})
    return out


def standings(data: dict, season: int) -> list[dict]:
    """[{team, w, l, rank}] best first: covers on graded regular-season (weeks 1-15) pool games."""
    import lore
    return (lore.compute(data).get("standings") or {}).get(season) or []


def member_theory(cg: dict, table: list[dict], tend: dict) -> dict:
    """{team: {'side': str|None, 'text': str}} for every member in the standings."""
    out = {}
    for r in table:
        fs = pick_brief.game_theory(cg, r["team"], table, tend)
        if not fs:
            continue
        side = next((f["side"] for f in fs if f["n"] == 60), None)
        out[r["team"]] = {"side": side, "text": " ".join(f["why"].rstrip(".") + "." for f in fs if f["n"] in (58, 60))}
    return out


# -----------------------------------------------------------------------------
# the card (code only)
# -----------------------------------------------------------------------------

def chat_game(g: dict, season: int, week: int) -> dict:
    return {"id": g["GameId"], "away": g["Away"], "home": g["Home"], "line": g.get("line"),
            "fpi_margin": g.get("fpi_margin"), "start": g.get("Start (CT)"), "season": season, "week": week}


def confidence(value_pts: float | None, strength: str, margin: float, bt: dict | None) -> dict:
    """Wording that scales with the edge, checked against the backtest. Never 'lock'."""
    rec = None
    if bt and value_pts is not None and value_pts >= EDGE_SOME:
        key = "value_1.5+" if value_pts >= EDGE_BIG else "value_0.5-1"
        rec = (bt.get("strategies") or {}).get(key)
    proven = bool(rec and rec["n"] >= BT_MIN_N and rec["pct"] >= BT_GOOD_PCT)
    if strength == "none" or margin < 1:
        word, say = "coin flip", "honestly a coin flip; if you have to pick, this side"
    elif value_pts is not None and value_pts >= EDGE_BIG and strength == "strong" and proven:
        word, say = "best play", "I like it: real value against a stale league line, still not a lock"
    elif strength == "strong" or (value_pts or 0) >= EDGE_BIG:
        word, say = "lean", "a lean, not a lock"
    else:
        word, say = "slight lean", "a slight lean at best"
    out = {"word": word, "say": say}
    if rec and rec["n"]:
        out["backtest"] = (f"plays with this much line value went {rec['w']}-{rec['n'] - rec['w']} "
                           f"({round(100 * rec['w'] / rec['n'])}%) in this league in {bt['span']}")
    return out


def build_card(g: dict, season: int, week: int, *, notes: dict | None, idx: dict | None, extras: dict | None,
               dossier: dict | None, news: dict | None, table: list[dict], tend: dict, bt: dict | None,
               now: dt.datetime) -> dict:
    """Everything except the Opus case. `now` is aware CT."""
    cg = chat_game(g, season, week)
    intel = pick_brief.dossier_intel(dossier) + pick_brief.news_intel(news, [cg["away"], cg["home"]])
    idx = idx or {"teams": {}, "players": [], "by_team": {}, "venues": []}  # no CFBD cache: market factors only
    b = pick_brief.build(cg, notes, idx, extras or {}, intel, now_ct=now.replace(tzinfo=None))
    fs = b["factors"]
    by_n = collections.defaultdict(list)
    for f in fs:
        by_n[f["n"]].append(f)
    mk = pick_brief.market_home(notes, cg, *((pick_brief.cfbd.resolve_team(cg[k], idx) if idx else None)
                                             for k in ("home", "away")))
    value = next((f for f in by_n[1] if f["side"]), None)
    value_pts = abs(float(cg["line"]) - mk["now"]) if mk else None
    move = next(iter(by_n[2]), None)
    cause = next(iter(by_n[3]), None)
    lean = b["pick"]
    margin = abs(b["score"][cg["away"]] - b["score"][cg["home"]])
    # reasons: the heaviest factors FOR the lean (line value first), 2-4 of them
    reasons = [f["why"] for f in sorted((f for f in fs if f["side"] == lean and f["n"] != 60),
                                        key=lambda f: (-f["w"], f["n"]))][:MAX_REASONS]
    against = [f["why"] for f in sorted((f for f in fs if f["side"] and f["side"] != lean and f["n"] != 60),
                                        key=lambda f: (-f["w"], f["n"]))][:2]
    lock_at = b["lock_at"]
    card = {
        "game": cg,
        "generated_at": now.isoformat(timespec="seconds"),
        "league_line": {"text": pick_brief.league_line_text(cg), "home": float(cg["line"]),
                        "locked": b["locked"], "lock_at": lock_at.isoformat() if lock_at else None},
        "market": ({"text": pick_brief.league_line_text({**cg, "line": mk["now"]}), "home_now": mk["now"],
                    "home_open": mk.get("open"), "src": mk.get("src"), "as_of": now.isoformat(timespec="minutes")}
                   if mk else None),
        "value": ({"side": value["side"], "label": pick_brief.side_label(value["side"], cg), "points": value_pts,
                   "why": value["why"], "key": next((f["why"] for f in by_n[4]), None)} if value else None),
        "move": ({"why": move["why"], "cause": cause["why"] if cause else None} if move else None),
        "lean": {"side": lean, "label": b["pick_label"], "strength": b["strength"], "score": b["score"],
                 "why": b["pick_why"]},
        "reasons": reasons,
        "against": against,
        "flags": [f["why"] for f in b["flags"]][:3],
        "history": [f["why"] for f in by_n[51] + by_n[23]],
        "confidence": confidence(value_pts, b["strength"], margin, bt),
        "theory": member_theory(cg, table, tend),
        "factors": [{k: f[k] for k in ("n", "name", "side", "w", "why")} for f in fs if f["n"] not in (58, 59, 60)],
        "no_source": [pick_brief.NAMES[n] for n, (st, _) in pick_brief.STATUS.items() if st == "none"],
        "dossier_at": (dossier or {}).get("generated_at"),
        "case": None,
    }
    card["inputs_hash"] = hashlib.sha256(json.dumps(
        [card["league_line"]["home"], (card["market"] or {}).get("home_now"), reasons, against, card["flags"],
         card["dossier_at"]], sort_keys=True).encode()).hexdigest()[:16]
    return card


# -----------------------------------------------------------------------------
# the written case (Opus, offline)
# -----------------------------------------------------------------------------

def case_items(card: dict, dossier: dict | None) -> dict[str, str]:
    items: dict[str, str] = {}

    def add(prefix: str, text: str | None):
        if text:
            items[f"{prefix}{sum(1 for k in items if k.startswith(prefix)) + 1}"] = re.sub(r"\s+", " ", text).strip()
    ll = card["league_line"]
    add("S", f"pool line: {ll['text']} ({'locked' if ll['locked'] else 'not locked yet'}; the league locks "
             "lines Tuesday 11 AM CT)")
    if card["market"]:
        add("S", f"betting market ({card['market']['src']}): {card['market']['text']}")
    if card["value"]:
        add("S", card["value"]["why"])
        add("S", card["value"]["key"])
    if card["move"]:
        add("S", card["move"]["why"])
        add("S", card["move"]["cause"])
    for f in card["factors"]:
        if f["n"] not in (1, 2, 3, 4) and (f["w"] or f["n"] in (51, 5)):
            add("S", f["why"] + (f" (favors {f['side']})" if f["side"] else ""))
    for k, label in (("facts", None), ("opinions", "opinion, not fact"), ("rumors", "RUMOR, unconfirmed")):
        for c in (dossier or {}).get(k) or []:
            lab = label or ("injury report, status " + str(c.get("status") or "unknown") if c.get("kind") == "injury"
                            else "news")
            add("C", f"[{lab}, {c.get('date', '')}] {c.get('text', '')}")
    return items


def add_case(card: dict, dossier: dict | None, prev: dict | None, research) -> None:
    """card['case']: a fresh Opus case, else the previous one (same inputs) or a stale one, else the
    dossier's analyst take, else None. Never raises."""
    import analyst
    pc = (prev or {}).get("case")
    if pc and prev.get("inputs_hash") == card["inputs_hash"] and pc.get("by") == "opus":
        card["case"] = pc
        return
    cg = card["game"]
    m = {"lean": card["lean"]["side"] if card["lean"]["strength"] != "none" else None,
         "strength": card["lean"]["strength"]}
    try:
        if research is None:
            raise RuntimeError("opus off for this run")
        items = case_items(card, dossier)
        if len(items) < 3:
            raise RuntimeError("brief too thin for a case")
        raw = research(analyst.build_prompt(cg, cg["season"], cg["week"], items, m, str(cg.get("start") or "TBD")))
        take, probs = analyst.validate_take(raw, items, cg, m)
        if not take:
            raise RuntimeError("case rejected: " + "; ".join(probs)[:300])
        take.update({"by": "opus", "generated_at": card["generated_at"], "dropped": probs})
        card["case"] = take
        return
    except Exception as e:  # noqa: BLE001 - the card stands on the code factors
        log.warning("game %s case: %s", cg["id"], str(e)[:300])
    if pc:
        card["case"] = dict(pc, stale=True)
    elif (dossier or {}).get("analyst_take"):
        card["case"] = dict(dossier["analyst_take"], by="dossier")


def opus_research(prompt: str) -> dict:
    import analyst
    import dossier
    return dossier.run_claude(prompt, analyst.TAKE_TIMEOUT_S, analyst.TAKE_SCHEMA, "")


# -----------------------------------------------------------------------------
# what's due + run
# -----------------------------------------------------------------------------

def path_for(week, gid, base: Path | None = None) -> Path:
    return (base or CARD_DIR) / str(week) / f"{gid}.json"


def due_reason(g: dict, card: dict | None, dossier: dict | None, now: dt.datetime) -> str | None:
    k = _kick(g)
    if not k or k <= now or str(g.get("Status") or "").lower() in ("final", "in_progress", "in", "live"):
        return None
    if not card:
        return "new"
    try:
        gen = dt.datetime.fromisoformat(card["generated_at"])
    except (KeyError, ValueError):
        return "new"
    lock = pick_brief.line_lock(k.replace(tzinfo=None))
    marks = [("sat", _last(now, 5, 7)), ("fri", _last(now, 4, 17))]
    if lock:
        marks.append(("lock", lock.replace(tzinfo=CT)))
    for name, anchor in marks:
        if gen < anchor <= now and k > anchor:
            return name
    if (dossier or {}).get("generated_at") and dossier["generated_at"] != card.get("dossier_at"):
        return "dossier"
    return None


def gather(games: list[dict]) -> tuple[dict, dict | None, dict]:
    import analyst
    return analyst.gather_inputs(games)


def run(force: bool = False, week: int | None = None, base: Path | None = None, db=None, data=None,
        research=opus_research, now: dt.datetime | None = None, inputs=None) -> dict:
    import dossier as dz
    import lore
    base = base or CARD_DIR
    now = now or now_ct()
    db = db or dz.Supa()
    season, wk, games = dz.open_slate(db, week)
    games = [g for g in games if not g.get("tiebreaker") and g.get("line") is not None]
    if not wk or not games:
        log.info("no slate yet for season %s; waiting", season)
        return {"ok": True, "slate": False}
    todo = []
    for g in games:
        prev = dz.read_json(path_for(wk, g["GameId"], base))
        dos = dz.read_json(dz.path_for(wk, g["GameId"]))
        reason = "forced" if force and (_kick(g) or now) > now else due_reason(g, prev, dos, now)
        if reason:
            todo.append((g, reason, prev, dos))
    if not todo:
        log.info("week %s: %d pool games, nothing due", wk, len(games))
        return {"ok": True, "slate": True, "week": wk, "built": []}
    data = data or lore.load(lore.service_key())
    table = standings(data, season)
    tend = pick_brief.tendencies([h for h in history_rows(data) if h["season"] >= season - 1])
    notes, idx, extras = inputs or gather([chat_game(g, season, wk) for g, *_ in todo])
    bt = dz.read_json(base / "backtest.json")
    try:  # weekly, so the wording tracks the newest graded weeks
        if not bt or now - dt.datetime.fromisoformat(bt["generated_at"]) > BACKTEST_MAX_AGE:
            bt = dict(backtest(data, (pick_brief.raw("lines") or []) + (pick_brief.raw("lines_prev") or []), idx,
                               full_seasons=(season,)), generated_at=now.isoformat(timespec="seconds"))
            dz.write_atomic(base / "backtest.json", bt)
    except Exception as e:  # noqa: BLE001 - the last backtest (or none) still words the cards
        log.warning("backtest refresh failed: %s", str(e)[:200])
    news = dz.read_json(NEWS_FILE)
    built, failed = [], []
    for g, reason, prev, dos in todo:
        gid = g["GameId"]
        try:
            card = build_card(g, season, wk, notes=(notes or {}).get(gid), idx=idx, extras=extras, dossier=dos,
                              news=news, table=table, tend=tend, bt=bt, now=now)
            card["trigger"] = reason
            add_case(card, dos, prev, research)
            dz.write_atomic(path_for(wk, gid, base), card)
            built.append(gid)
            log.info("week %s game %s (%s at %s) %s: lean %s (%s), case %s", wk, gid, g["Away"], g["Home"], reason,
                     card["lean"]["label"], card["confidence"]["word"], (card["case"] or {}).get("by"))
        except Exception as e:  # noqa: BLE001 - keep the last good card
            failed.append(gid)
            log.warning("week %s game %s card FAILED, kept previous: %s", wk, gid, str(e)[:300])
    return {"ok": not failed, "slate": True, "week": wk, "built": built, "failed": failed}


# -----------------------------------------------------------------------------
# backtest
# -----------------------------------------------------------------------------

def market_close(lines_rows: list | None) -> dict:
    """{game_id: (home spread close, home spread open)} from CFBD /lines rows."""
    out = {}
    for x in lines_rows or []:
        now, op, _ = pick_brief._line(x)
        if now is not None:
            out[x["id"]] = (now, op)
    return out


def backtest(data: dict, lines_rows: list, idx: dict | None = None, full_seasons=()) -> dict:
    """Hit rates against the LEAGUE line on graded pool games. A side 'hits' when it's the graded
    cover (the site's winner, pushes to the away side). full_seasons: also score pick_brief's whole
    lean for those seasons (in-sample: it uses this season's ratings, so it flatters itself)."""
    close = market_close(lines_rows)
    games = [g for g in data["games"] if g.get("winner") and not g.get("tiebreaker") and g.get("line") is not None
             and g.get("week") is not None and 1 <= g["week"] <= SEASON_WEEKS]
    acc = collections.defaultdict(lambda: [0, 0])

    def tally(name, side, g):
        if side:
            acc[name][1] += 1
            acc[name][0] += lore_same(side, g["winner"])
    for g in games:
        ln = float(g["line"])
        fav = g["Home"] if ln < 0 else g["Away"] if ln > 0 else None
        dog = (g["Away"] if fav == g["Home"] else g["Home"]) if fav else None
        tally("underdog", dog, g)
        tally("home", g["Home"], g)
        mc = close.get(g["GameId"])
        if not mc:
            continue
        acc["_with_market"][1] += 1
        gap = ln - mc[0]   # > 0: the market likes home more than the league did
        side = g["Home"] if gap > 0 else g["Away"]
        if abs(gap) >= EDGE_BIG:
            tally("value_1.5+", side, g)
        elif abs(gap) >= EDGE_SOME:
            tally("value_0.5-1", side, g)
        if abs(gap) >= EDGE_SOME:
            tally("value_any", side, g)
            a, b = sorted((abs(ln), abs(mc[0])))
            if any(a < k <= b or a <= k < b for k in pick_brief.KEY_NUMBERS) and (ln < 0) == (mc[0] < 0):
                tally("value_crosses_key", side, g)
        if mc[1] is not None and abs(mc[0] - mc[1]) >= pick_brief.MOVE_MIN:
            tally("follow_move_1.5+", g["Home"] if mc[0] < mc[1] else g["Away"], g)
        if g["cfb_season"] in full_seasons and idx:
            cg = {"id": g["GameId"], "away": g["Away"], "home": g["Home"], "line": ln, "start": g.get("Start (CT)"),
                  "season": g["cfb_season"], "week": g["week"], "fpi_margin": None}
            k = _kick(g)
            nb = {"market": {"home_now": mc[0], "home_open": mc[1], "book": "CFBD close"}}
            b = pick_brief.build(cg, nb, idx, {}, [], now_ct=(k - dt.timedelta(hours=20)).replace(tzinfo=None)
                                 if k else None)
            tally(f"full_lean_{g['cfb_season']}_in_sample", b["lean"], g)
            if b["strength"] == "strong":
                tally(f"full_lean_strong_{g['cfb_season']}_in_sample", b["lean"], g)
    seasons = sorted({g["cfb_season"] for g in games})
    strat = {}
    for k, (w, n) in acc.items():
        if k.startswith("_"):
            continue
        strat[k] = {"w": w, "n": n, "pct": round(w / n, 3) if n else 0.0, "ci95": _wilson(w, n)}
    return {"span": f"{seasons[0]}-{str(seasons[-1])[2:]}" if seasons else "", "games": len(games),
            "games_with_market": acc["_with_market"][1], "strategies": strat,
            "note": "graded pool games, weeks 1-15, no tiebreakers; market = CFBD closing line (DraftKings first); "
                    "full_lean rows use this season's ratings (in-sample, optimistic)"}


def lore_same(a, b) -> bool:
    import lore
    return lore.same(a, b)


def cmd_backtest() -> dict:
    import chat
    import lore
    data = lore.load(lore.service_key())
    lines = (pick_brief.raw("lines") or []) + (pick_brief.raw("lines_prev") or [])
    res = backtest(data, lines, chat._cfbd_index(), full_seasons=(max(g["cfb_season"] for g in data["games"]),))
    res["generated_at"] = now_ct().isoformat(timespec="seconds")
    import dossier as dz
    dz.write_atomic(BACKTEST_FILE, res)
    return res


# -----------------------------------------------------------------------------
# live: read a card, write the rider (chat.py, latest user turn only)
# -----------------------------------------------------------------------------

def load_card(week, gid, base: Path | None = None) -> dict | None:
    try:
        c = json.loads(path_for(week, gid, base).read_text())
        gen = dt.datetime.fromisoformat(c["generated_at"])
    except (OSError, ValueError, KeyError):
        return None
    if dt.datetime.now(gen.tzinfo) - gen > CARD_MAX_AGE:
        return None
    return c


def _asof(iso: str | None) -> str:
    try:
        return dt.datetime.fromisoformat(iso).strftime("%a %-I:%M %p")
    except (TypeError, ValueError):
        return "recently"


def card_lines(c: dict, asker: str | None, full: bool) -> list[str]:
    g, ll, lean = c["game"], c["league_line"], c["lean"]
    out = [f"GAME CARD, {g['away']} at {g['home']} (built offline {_asof(c['generated_at'])}; every number is "
           "computed, copy them exactly; graded ATS: the pick is who COVERS the league line):"]
    lk = "locked Tue 11 AM CT" if ll["locked"] else "not locked yet, locks Tue 11 AM CT"
    mk = c.get("market")
    out.append(f"  League line: {ll['text'].split(' / ')[0]} ({lk})."
               + (f" Market: {mk['text'].split(' / ')[0]} ({mk['src']}, as of {_asof(mk['as_of'])})." if mk else
                  " No market line on file."))
    if c.get("value"):
        out.append(f"  Value: {re.sub(r'^line value: ', '', c['value']['why'])}."
                   + (f" {c['value']['key']}." if c["value"].get("key") else ""))
    if c.get("move"):
        cause = re.sub(r"^cause of the move: (lines up with )?", "", c["move"].get("cause") or "")
        cause = "" if cause.startswith("no injury or news") else cause
        out.append(f"  {c['move']['why'][0].upper()}{c['move']['why'][1:]}."
                   + (f" Likely cause, per reports: {cause if cause.endswith('.') else cause + '.'}" if cause
                      else " No reported cause on file."))
    case = c.get("case") or {}
    agree = case.get("pick") == lean["side"]
    conf = c["confidence"]
    out.append(f"  THE PLAY: I'd take {lean['label']} ({conf['say']}).")
    reasons = list(c["reasons"])
    if case.get("reasons") and agree:
        reasons = [r["text"] for r in case["reasons"]] + reasons
    out.append("  Why (use the best two or three, never all): " + " / ".join(reasons[: (4 if full else 3)]))
    counter = (case.get("counter") or {}).get("text") if agree else None
    if case.get("pick") and not agree:
        counter = f"the film room would take {case['pick']}: " + " / ".join(r["text"] for r in case["reasons"][:2])
    counter = counter or (c["against"][0] if c.get("against") else None)
    if counter:
        out.append(f"  Best case against: {counter}")
    if full:
        for f in c.get("flags") or []:
            out.append(f"  Red flag: {f}")
        for h in c.get("history") or []:
            out.append(f"  History vs this opponent: {re.sub(r'^head-to-head: ', '', h)}")
    th = (c.get("theory") or {}).get(asker or "")
    if th:
        out.append(f"  For {asker} (game theory from rivals' past graded picks, never this week's): {th['text']}")
    if conf.get("backtest"):
        out.append(f"  Honesty check: {conf['backtest']}. ATS picks are close to a coin flip even for pros, "
                   "so never call it a lock.")
    return out


def card_note(ctx: dict, question: str) -> str:
    """The card rider for the open games this question is about ('' when there's no card: chat.py then
    falls back to its live breakdown + analyst take). chat.take_games already drops locked weeks, started
    games and non-opinion questions."""
    import chat
    games = chat.take_games(ctx, question)
    if not games:
        return ""
    asker = (ctx.get("you") or {}).get("team_name")
    full = len(games) <= 2 and (pick_brief.wants_full(question) or len(games) == 1)
    out = []
    for g in games[:4]:
        c = load_card(ctx.get("current_week"), g["id"])
        if not c or c["league_line"]["home"] != float(g.get("line") or 0):
            continue  # no card, or the league line changed since: the live breakdown covers it
        out += card_lines(c, asker, full)
    if not out:
        return ""
    return "\n".join(out) + (
        "\nSay it like a sharp buddy, in your voice: the side by team name and number ('I'd take X -3.5'; never "
        "'my pick' or 'I took', those mean your real league picks), the best two or three reasons, the case "
        "against only if it fits, and the game-theory line only to the person it's for. Confidence exactly as "
        "the card words it. No stats, names or injuries that aren't on the card. " + chat.TALK_LIKE_A_PERSON)


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------

def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(prog="game_cards.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--force", action="store_true")
    r.add_argument("--week", type=int)
    r.add_argument("--now")
    r.add_argument("--no-opus", action="store_true")
    sub.add_parser("backtest")
    s = sub.add_parser("show")
    s.add_argument("--week", type=int)
    a = ap.parse_args(argv)
    if a.cmd == "run":
        now = dt.datetime.fromisoformat(a.now).replace(tzinfo=CT) if a.now else None
        res = run(force=a.force, week=a.week, now=now, research=None if a.no_opus else opus_research)
        print(json.dumps(res))
        return 0 if res["ok"] else 1
    if a.cmd == "backtest":
        res = cmd_backtest()
        print(json.dumps(res, indent=1))
        return 0
    weeks = sorted((p for p in CARD_DIR.iterdir() if p.is_dir()), key=lambda p: int(p.name)) if CARD_DIR.exists() else []
    wd = CARD_DIR / str(a.week) if a.week else (weeks[-1] if weeks else None)
    for p in sorted((wd.glob("*.json") if wd else [])):
        c = json.loads(p.read_text())
        print("\n".join(card_lines(c, None, True)) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
