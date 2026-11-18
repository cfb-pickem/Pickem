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
  - the written case / counter-case: code first (the reasons / against / flags
    above are the card's case on every run). Opus (analyst.py's prompt and
    validator via claude -p, picky_claude.MODEL, --strict-mcp-config with the
    empty MCP config, no tools) writes one case per game per week, at the
    first run after the Tue 11:00 AM CT line lock (the league line is frozen,
    so the case stays true), retried at most once a day, or on demand
    (`run --opus`). Later runs keep it while it's fresh (enrich.py, 6 days),
    the league line is the one it was written for, and every number in a
    reason still appears in the card's brief; lines whose numbers moved drop
    out. No Opus case yet = the dossier's (fresh) analyst take, else none:
    the card stands on the code factors, and PICKY_NO_ANTHROPIC=1 never
    spawns anything.

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

CLI: game_cards.py run [--force] [--week N] [--now ISO] [--opus | --no-opus]
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
import enrich  # noqa: E402
import pick_brief  # noqa: E402
import rivalries  # noqa: E402

log = logging.getLogger("picky.game_cards")
CT = ZoneInfo("America/Chicago")
CARD_DIR = Path(os.environ.get("PICKY_GAME_CARD_DIR", HERE / "cache" / "game_cards"))
BACKTEST_FILE = CARD_DIR / "backtest.json"
NEWS_FILE = HERE / "cache" / "news.json"
CARD_MAX_AGE = dt.timedelta(days=8)
BACKTEST_MAX_AGE = dt.timedelta(days=6)
CASE_RETRY = dt.timedelta(hours=20)  # a failed weekly Opus case is retried at most once a day
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


def league_crowd(cg: dict, table: list[dict], tend: dict) -> dict | None:
    """The league's lean on one game from past graded picks only (pick_brief.predict per member, never this
    week's picks): {side, n, of} when a strict majority of members with a clear habit land on one side."""
    preds = [p for p in (pick_brief.predict(tend.get(r["team"]), cg) for r in table) if p]
    if not preds:
        return None
    side, n = collections.Counter(preds).most_common(1)[0]
    return {"side": side, "n": n, "of": len(preds), "src": "past graded picks"} if n * 2 > len(preds) else None


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
    elif (value_pts or 0) >= EDGE_BIG:  # a factor-only 'strong' lean hit 50-53% out of sample (weights_fit.py): slight
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
        "crowd": league_crowd(cg, table, tend),
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


def keep_case(pc: dict | None, card: dict, items: dict[str, str], prev: dict | None = None) -> dict | None:
    """The last good Opus case, trimmed to what's still true, or None: fresh (enrich.py), written
    for today's league line, and only the parts whose numbers all still appear in the brief."""
    if not pc or pc.get("by") != "opus" or pc.get("stale"):
        return None
    if not enrich.fresh(pc.get("generated_at"), dt.datetime.fromisoformat(card["generated_at"])):
        return None
    lh = pc.get("league_home", ((prev or {}).get("league_line") or {}).get("home"))
    if lh is None or float(lh) != float(card["league_line"]["home"]):
        return None
    facts = " ".join(items.values()) + f" {card['game']['away']} {card['game']['home']}"
    reasons = [r for r in pc.get("reasons") or [] if enrich.numbers_hold(r.get("text", ""), facts)]
    if not reasons:
        return None
    out = dict(pc, reasons=reasons)
    for k in ("counter", "market_missing"):
        if out.get(k) and not enrich.numbers_hold(out[k].get("text", ""), facts):
            out[k] = None
    if out.get("quotable") and not enrich.numbers_hold(out["quotable"], facts):
        out["quotable"] = None
    return out


def opus_due(card: dict, prev: dict | None, now: dt.datetime) -> bool:
    """One Opus case per game per week: once the league line is locked, never twice inside
    CASE_RETRY, never with the kill switch on."""
    if not enrich.available() or not is_locked(card, now):
        return False
    try:
        return now - dt.datetime.fromisoformat((prev or {})["case_tried_at"]) >= CASE_RETRY
    except (KeyError, TypeError, ValueError):
        return True


def add_case(card: dict, dossier: dict | None, prev: dict | None, research, now: dt.datetime | None = None,
             force: bool = False) -> None:
    """card['case']: the last good Opus case while still true, else this week's Opus case when
    due (or forced), else the dossier's fresh analyst take, else None. research None = no Opus.
    Never raises."""
    import analyst
    now = now or dt.datetime.fromisoformat(card["generated_at"])
    items = case_items(card, dossier)
    kept = keep_case((prev or {}).get("case"), card, items, prev)
    if kept and not force:
        card["case"] = kept
        return
    cg = card["game"]
    if (prev or {}).get("case_tried_at"):
        card["case_tried_at"] = prev["case_tried_at"]
    if research is not None and (opus_due(card, prev, now) or (force and enrich.available())):
        card["case_tried_at"] = now.isoformat(timespec="seconds")
        m = {"lean": card["lean"]["side"] if card["lean"]["strength"] != "none" else None,
             "strength": card["lean"]["strength"]}
        try:
            if len(items) < 3:
                raise RuntimeError("brief too thin for a case")
            raw = research(analyst.build_prompt(cg, cg["season"], cg["week"], items, m, str(cg.get("start") or "TBD")))
            take, probs = analyst.validate_take(raw, items, cg, m)
            if not take:
                raise RuntimeError("case rejected: " + "; ".join(probs)[:300])
            take.update({"by": "opus", "generated_at": card["generated_at"], "dropped": probs,
                         "league_home": card["league_line"]["home"]})
            card["case"] = take
            return
        except Exception as e:  # noqa: BLE001 - the card stands on the code factors
            log.warning("game %s case: %s", cg["id"], str(e)[:300])
    if kept:
        card["case"] = kept
        return
    t = (dossier or {}).get("analyst_take")
    if t and enrich.fresh(t.get("generated_at"), now):
        card["case"] = dict(t, by="dossier")


def opus_research(prompt: str) -> dict:
    import analyst
    import dossier
    return dossier.run_claude(prompt, analyst.TAKE_TIMEOUT_S, analyst.TAKE_SCHEMA, "", caller="game_cards")


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
    try:  # the league line moved (they float until Tuesday's lock): the rider skips the stale card, so rebuild it
        if g.get("line") is not None and float(g["line"]) != float(card["league_line"]["home"]):
            return "line"
    except (KeyError, TypeError, ValueError):
        pass
    if (dossier or {}).get("generated_at") and dossier["generated_at"] != card.get("dossier_at"):
        return "dossier"
    try:  # the league line moved before the lock (Iowa -6.5 -> -8.5, 2026-11-16): the card is stale
        if float(g.get("line")) != float(card["league_line"]["home"]):
            return "line"
    except (TypeError, ValueError, KeyError):
        pass
    return None


def gather(games: list[dict]) -> tuple[dict, dict | None, dict]:
    import analyst
    return analyst.gather_inputs(games)


def run(force: bool = False, week: int | None = None, base: Path | None = None, db=None, data=None,
        research=opus_research, now: dt.datetime | None = None, inputs=None, opus: bool = False) -> dict:
    """research None = code only; opus=True = on demand: a fresh Opus case for every built card."""
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
            add_case(card, dos, prev, research, now, force=opus)
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
        if rivalries.rivalry(g["Away"], g["Home"]):  # factor 24's folk rule, on the league line
            tally("rivalry_dog", dog, g)
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
    res["rivalry_fbs_close"] = rivalries.backtest(lines)  # every FBS rivalry game vs the closing line
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
    if (c.get("case") or {}).get("by") == "opus" and not enrich.fresh(c["case"].get("generated_at")):
        c["case"] = None  # an Opus case past its freshness window never reaches a reply
    return c


def _asof(iso: str | None) -> str:
    try:
        return dt.datetime.fromisoformat(iso).strftime("%a %-I:%M %p")
    except (TypeError, ValueError):
        return "recently"


def is_locked(c: dict, now: dt.datetime | None = None) -> bool:
    """The league line's lock state NOW (Tue 11 AM CT, league_rules.line_lock_at), not when the card was
    built: a Monday card read on Wednesday is locked."""
    ll = c["league_line"]
    if ll.get("locked"):
        return True
    try:
        lock_at = dt.datetime.fromisoformat(ll["lock_at"]).replace(tzinfo=None)
    except (TypeError, ValueError, KeyError):
        return False
    return (now or now_ct()).replace(tzinfo=None) >= lock_at


def _pts(x: float) -> str:
    return "half a point" if x == 0.5 else f"{pick_brief._n(x)} point" + ("" if x == 1 else "s")


def value_text(c: dict, locked: bool) -> str | None:
    """The league-vs-market gap in words. Before the lock it's conditional (the league line can still
    move, so nothing is banked); after the lock it's real value."""
    v, mk = c.get("value"), c.get("market")
    if not v or not mk:
        return None
    g = c["game"]
    league = pick_brief.side_label(v["side"], {**g, "line": c["league_line"]["home"]})
    market = pick_brief.side_label(v["side"], {**g, "line": mk["home_now"]})
    key = re.search(r"crosses (\d+)", v.get("key") or "")
    key_t = f", and it's on the good side of the key number {key.group(1)}" if key else ""
    if locked:
        return (f"the league line is locked at {league} and the market's at {market} ({mk['src']}): "
                f"{_pts(v['points'])} of real value on {v['side']}{key_t}")
    return (f"the league has {league} and the market's at {market} ({mk['src']}); if that holds through "
            f"Tuesday's 11 AM CT line lock, that's {_pts(v['points'])} of value on {v['side']}{key_t}. Not banked "
            "yet: the league line can still move before the lock")


def _reasons(c: dict, locked: bool) -> list[str]:
    """The card's reasons with the line-value pair (gap + key number) as one lock-aware sentence."""
    vt = value_text(c, locked)
    out = []
    for r in c.get("reasons") or []:
        if r.startswith("key number:"):
            continue
        out.append(vt if r.startswith("line value:") and vt else r)
    return out


_GAP_TALK = re.compile(r"\b(market|pool|league line|free points?|points? of value|value|stale)\b", re.I)


def _case_reasons(case: dict, locked: bool) -> list[str]:
    """The film room's reasons; before the lock the ones about the league-vs-market gap drop out ('two free
    points'), since value_text already says it conditionally."""
    return [r["text"] for r in case.get("reasons") or [] if locked or not _GAP_TALK.search(r["text"])]


def _say(c: dict, locked: bool) -> str:
    conf = c["confidence"]
    if not locked and conf.get("word") == "best play":
        return "I like it if the league line holds through Tuesday's lock; still not a lock"
    return conf["say"]


CONF_RANK = {"best play": 3, "lean": 2, "slight lean": 1, "coin flip": 0}


def strongest(cards: list[dict]) -> dict | None:
    """The best play on the slate: the card's confidence word first, then the factor-score margin."""
    def key(c):
        sc = c["lean"].get("score") or {}
        return (CONF_RANK.get(c["confidence"].get("word"), 0),
                abs(sc.get(c["game"]["away"], 0) - sc.get(c["game"]["home"], 0)))
    pool = [c for c in cards if c["lean"].get("side")]
    return max(pool, key=key) if pool else None


def slate_lines(cards: list[dict], now: dt.datetime | None = None) -> list[str]:
    """Every open game in one compact block: the side, two reasons, the lock-aware gap, then the best play."""
    if not cards:
        return []
    locked = all(is_locked(c, now) for c in cards)
    lk = ("lines are locked (since Tue 11 AM CT)" if locked else
          "league lines are NOT locked yet (they lock Tue 11 AM CT), so a gap vs the market is only value if it "
          "holds through the lock")
    out = [f"SLATE READ, all {len(cards)} open games (game cards built offline {_asof(cards[0]['generated_at'])}; "
           f"every number computed, copy exactly; graded ATS: the pick is who COVERS the league line; {lk}):"]
    for c in cards:
        g, lean, lkc = c["game"], c["lean"], is_locked(c, now)
        case = c.get("case") or {}
        rs = [r for r in _reasons(c, lkc) if not r.startswith(("the league has", "the league line is locked"))]
        if case.get("pick") == lean["side"]:
            rs = [r for r in _case_reasons(case, lkc) if not _GAP_TALK.search(r)] + rs
        line = f"- {g['away']} at {g['home']}: I'd take {lean['label']} ({_say(c, lkc)}). Why: " + " / ".join(rs[:2])
        vt = value_text(c, lkc)
        if vt:
            line += f". Line: {vt}"
        out.append(line + ".")
    best = strongest(cards)
    if best:
        out.append(f"STRONGEST PLAY: {best['lean']['label']} ({best['game']['away']} at {best['game']['home']}).")
    return out


def card_lines(c: dict, asker: str | None, full: bool, now: dt.datetime | None = None) -> list[str]:
    g, ll, lean = c["game"], c["league_line"], c["lean"]
    locked = is_locked(c, now)
    out = [f"GAME CARD, {g['away']} at {g['home']} (built offline {_asof(c['generated_at'])}; every number is "
           "computed, copy them exactly; graded ATS: the pick is who COVERS the league line):"]
    lk = "locked Tue 11 AM CT" if locked else "not locked yet, locks Tue 11 AM CT"
    mk = c.get("market")
    out.append(f"  League line: {ll['text'].split(' / ')[0]} ({lk})."
               + (f" Market: {mk['text'].split(' / ')[0]} ({mk['src']}, as of {_asof(mk['as_of'])})." if mk else
                  " No market line on file."))
    vt = value_text(c, locked)
    if vt:
        out.append(f"  {'Value' if locked else 'Line gap (NOT value yet)'}: {vt}.")
    if c.get("move"):
        cause = re.sub(r"^cause of the move: (lines up with )?", "", c["move"].get("cause") or "")
        cause = "" if cause.startswith("no injury or news") else cause
        out.append(f"  {c['move']['why'][0].upper()}{c['move']['why'][1:]}."
                   + (f" Likely cause, per reports: {cause if cause.endswith('.') else cause + '.'}" if cause
                      else " No reported cause on file."))
    case = c.get("case") or {}
    agree = case.get("pick") == lean["side"]
    conf = c["confidence"]
    out.append(f"  THE PLAY: I'd take {lean['label']} ({_say(c, locked)}).")
    reasons = _reasons(c, locked)
    if case.get("reasons") and agree:
        reasons = _case_reasons(case, locked) + reasons
    out.append("  Why (use two, three at most): " + " / ".join(reasons[: (4 if full else 3)]))
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
    if th:  # one clear call for the asker: the lean, or the lean plus the chase angle, never a muddle
        g_ = {**g, "line": ll["home"]}
        if th.get("side") and th["side"] != lean["side"]:
            call = (f"so for {asker} the contrarian play is {pick_brief.side_label(th['side'], g_)}: give your "
                    f"pick ({lean['label']}) first, then say that chasing makes the other side worth a look")
        elif th.get("side"):
            call = f"so game theory backs the same side for {asker}"
        else:
            call = "no standings reason to deviate from the pick"
        out.append(f"  For {asker} (game theory from rivals' past graded picks, never this week's): "
                   f"{th['text']} Bottom line: {call}.")
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
    cards = []
    for g in games[:4]:
        c = load_card(ctx.get("current_week"), g["id"])
        if not c or c["league_line"]["home"] != float(g.get("line") or 0):
            continue  # no card, or the league line changed since: the live breakdown covers it
        cards.append(c)
    if not cards:
        return ""
    if len(games) >= 3 and chat.slate_ask(question):
        return "\n".join(slate_lines(cards)) + "\n" + (BEST_SAY if chat.best_ask(question) else SLATE_SAY)
    out = []
    for c in cards:
        out += card_lines(c, asker, full)
    return "\n".join(out) + (
        "\nSay it like a sharp buddy, in your voice: the side by team name and number ('I'd take X -3.5'; never "
        "'my pick' or 'I took', those mean your real league picks), the best two or three reasons, the case "
        "against only if it fits, and the game-theory line only to the person it's for. Confidence exactly as "
        "the card words it. No stats, names or injuries that aren't on the card. " + PRE_LOCK_SAY
        + chat.TALK_LIKE_A_PERSON)


PRE_LOCK_SAY = ("Before Tuesday's 11 AM CT lock a line gap is NOT value yet: say it with 'if it holds through "
                "Tuesday' and never call anything 'the only value side'. ")
SLATE_SAY = ("Text it like a buddy, phone-length (about 100 words): one short line for EVERY game above, in this "
             "order, none skipped (the coin flip too, said as a coin flip), even if you covered the slate a moment "
             "ago: 'I'd take X (line)' with the line exactly as written above, plus one or two of its reasons in plain words (no stat labels like SP+ or EPA, no "
             "sources, no per-line citations, round the numbers), then one line on the strongest play. " + PRE_LOCK_SAY
             + "No jab about their record or the standings: it's pick advice.")
BEST_SAY = ("They asked for ONE play (a lock / best bet): give only the STRONGEST PLAY above, 'I'd take X (line)' plus "
            "its one or two best reasons in plain words, 2 sentences max, confidence exactly as the card words it (a "
            "'lean' is not a lock: say so with some swagger). Don't list the other games. " + PRE_LOCK_SAY
            + "No jab about their record or the standings: it's pick advice.")


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
    r.add_argument("--opus", action="store_true", help="on demand: a fresh Opus case for every built card")
    r.add_argument("--no-opus", action="store_true", help="code only")
    sub.add_parser("backtest")
    s = sub.add_parser("show")
    s.add_argument("--week", type=int)
    a = ap.parse_args(argv)
    if a.cmd == "run":
        now = dt.datetime.fromisoformat(a.now).replace(tzinfo=CT) if a.now else None
        res = run(force=a.force, week=a.week, now=now, research=None if a.no_opus else opus_research, opus=a.opus)
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
