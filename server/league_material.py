"""
League material sheet: fresh, data-backed roast / praise lines per member,
the week's league storylines and Picky's own season narrative, written ahead
of time by Claude so Picky's small live model only has to deliver them.

PRIVATE: the output names real members and their records, so it lives
outside the public repo at /Users/Agent/cfb-pickem-private/league_material.json
(mode 0600). chat.material_note reads it.

Nobody maintains this. LaunchAgent com.pickem.league-material runs
`league_material.py run` hourly; this file decides whether anything is due:
  - "final": a pool week went fully final after the sheet was written;
  - "midweek": Wednesday 10 AM CT refresh (fresh angles, same numbers);
  - "drift": the standings no longer match the sheet (stat correction, a
    regraded game), at most once per 12 hours.
Otherwise a run is a few Supabase GETs and exits. ~2-3 `claude -p` calls a week, plus
one for banter.py's weekly comeback/banter bank (banter.refresh_if_due on every tick).

Pipeline:
  1. Facts, deterministic (no LLM): lore.load()/lore.compute() over the
     service-key DB read (final games only: never an unstarted pick), plus
     standings gaps, last week, streaks, chalk vs dog rates, best and worst
     picks, Picky's own record. Each fact gets an id.
  2. `claude -p` (sonnet, no tools, zero MCP, ANTHROPIC_API_KEY stripped)
     writes lines that cite fact ids, with the running bits as canon.
  3. validate(): every number in a line must appear in the facts it cites,
     every member named must be in those facts, nothing body/family/dark,
     no report-speak. Bad lines are dropped; a member with no lines left
     keeps none. On any failure the previous sheet stays.
  4. Atomic write, chmod 600.

CLI:  league_material.py run [--force]
      league_material.py facts          (print the fact sheet, no LLM)
      league_material.py status
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import logging
import os
import re
import shutil
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import banter  # noqa: E402
import rare_bits  # noqa: E402  (weekly comeback/banter bank, refreshed on this job's tick)
import dossier  # noqa: E402  (run_claude, write_atomic, the lock pattern)
import lore  # noqa: E402

log = logging.getLogger("picky.material")

CT = ZoneInfo("America/Chicago")
OUT = Path(os.environ.get("PICKY_LEAGUE_MATERIAL", "/Users/Agent/cfb-pickem-private/league_material.json"))
LOG_PATH = HERE / "logs" / "league_material.log"
LOCK = Path("/tmp/picky-material.lock")
LOCK_STALE_S = 2 * 3600
TIMEOUT_S = 900
PICKY_TEAM = "Picky"
LEADER_TEAM = "Chuck P"
DRIFT_COOLDOWN_S = 12 * 3600
FAIL_BACKOFF_S = 6 * 3600   # a failed run waits this long before trying again (subscription budget)
LINES_MIN, LINES_MAX = 3, 6

SCHEMA = {
    "type": "object",
    "properties": {
        "members": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "team": {"type": "string"},
                "lines": {"type": "array", "items": {
                    "type": "object",
                    "properties": {"text": {"type": "string"},
                                   "kind": {"type": "string", "enum": ["roast", "praise", "leader"]},
                                   "facts": {"type": "array", "items": {"type": "string"}}},
                    "required": ["text", "kind", "facts"]}}},
            "required": ["team", "lines"]}},
        "storylines": {"type": "array", "items": {
            "type": "object",
            "properties": {"text": {"type": "string"}, "facts": {"type": "array", "items": {"type": "string"}}},
            "required": ["text", "facts"]}},
        "picky_narrative": {"type": "object",
                            "properties": {"text": {"type": "string"},
                                           "facts": {"type": "array", "items": {"type": "string"}}},
                            "required": ["text", "facts"]},
    },
    "required": ["members", "storylines", "picky_narrative"],
}

PROMPT = """You write trash-talk material for Picky, the wisecracking bot in a friends' college football pick'em
league ({n} members, picks against a pool spread, 1 point per correct pick; top 4 after week 15 make the league
playoffs). Picky runs on a small model; he'll drop your lines into group-text replies in his own voice, so write
them the way a funny friend talks: short, spoken, contractions, specific, no corporate or report phrasing.

It's {today}. Season {season}, through week {week} (every game below is final).

FACTS (the only facts you may use; cite ids):
{facts}

RUNNING BITS (canon; keep them, don't contradict them, don't overuse them):
- Blinkstradamus: roast him like anyone else, from his picks and record. NO milk, carton, Jeep or "freak"
  lines at all: that story is a once-a-month bit Picky's live code rations itself (Charlie: "you're overdoing
  the milk").
- Ackbar is Picky's nemesis: always takes the favorite (chalk merchant, no guts). Roast him for chalk.
- {leader} is the commissioner and Picky's creator, the "Dear Leader". For him write kind "leader" lines in
  deadpan North Korean state-media style (titles like Dear Leader / Supreme Commissioner, one absurd flourish,
  never mockery, numbers exactly true), PLUS 2 normal kind "roast" lines for when he asks about himself.
- DaddyFreeman gets roasted hardest of anyone (still only picks, records and standings).
- Picky plays too (exhibition team, not in the standings) and is honest about his own record.

Write:
- members: for EVERY member listed in the facts, {lo}-{hi} lines. Mix roast and praise to fit the numbers
  (a hot member gets backhanded praise, a cold one gets roasted). Each line: one or two sentences, under 35
  words, built on a specific stat, with facts = the ids it uses.
- storylines: 3-5 league storylines for the week (races, collapses, streaks, rivalries, the cut line), each one
  or two spoken sentences with fact ids.
- picky_narrative: Picky's own season story in two or three sentences, first person ("I"), with fact ids.

Hard rules:
- Every number you write must appear in a fact you cite, written the same way (a 7-1 stays 7-1). Never add up,
  subtract or estimate numbers yourself. Name only members and teams that appear in the facts you cite.
- Never mention a pick for a game that isn't final (there are none in the facts; don't invent any).
- Roast picks, records, streaks and standings only. Never looks, bodies, weight, sex, family, jobs, money,
  health, race, religion, gender or politics. No slurs, no threats.
- No labels or lists inside a line, no hashtags, no emoji.
- Vary the wording: no two lines share an opener or a catchphrase, no stock templates. Each line
  should feel like a different friend's reaction to the stat.
Return only the JSON object."""

_NUM = re.compile(r"\d+(?:\.\d+)?")
_BANNED = re.compile(r"\b(mom|mother|dad|father|wife|girlfriend|kids?|son|daughter|fat|ugly|bald|weight|dick|"
                     r"penis|sex\w*|cancer|die|death|dead(?! last)|kill|suicide|gay|retard\w*|job|salary|broke|poor|"
                     r"rich|drunk|rehab)\b|https?://|#\w", re.I)
_REPORTY = re.compile(r"\b(fact ?\w*\d|per the data|statistically speaking|in conclusion|key takeaway|"
                      r"F\d+|M\d+|L\d+)\b|\n\s*[-*•]")


def now_ct() -> dt.datetime:
    return dt.datetime.now(CT)


# -----------------------------------------------------------------------------
# facts (deterministic)
# -----------------------------------------------------------------------------

def _ord(n: int) -> str:
    return f"{n}{lore._ord(n)}"


def _rec(rows) -> tuple[int, int]:
    w = sum(1 for r in rows if r["hit"])
    return w, len(rows) - w


def graded_rows(data: dict, season: int, include_exhibition: bool = False) -> list[dict]:
    """One row per graded pick (final, non-tiebreaker, weeks 1-15), same
    grading as lore.compute / chat.compute_standings."""
    name = {t["team_id"]: t["team_name"] for t in data["teams"]
            if include_exhibition or not t.get("is_exhibition")}
    games = {g["GameId"]: g for g in data["games"] if g.get("winner") and not g.get("tiebreaker")
             and g.get("cfb_season") == season and g.get("week") is not None and g["week"] <= lore.SEASON_WEEKS}
    rows = []
    for p in data["picks"]:
        g = games.get(p["game_id"])
        if not g or p["team_id"] not in name:
            continue
        side = g["Home"] if lore.same(p["pick"], g["Home"]) else g["Away"] if lore.same(p["pick"], g["Away"]) \
            else p["pick"]
        ln = float(g.get("line") or 0)
        fav = ln != 0 and ((side == g["Home"]) == (ln < 0))
        rows.append({"team": name[p["team_id"]], "g": g, "side": side, "fav": fav, "dog": ln != 0 and not fav,
                     "hit": bool(p.get("jackpot")) or lore.same(side, g["winner"])})
    rows.sort(key=lambda r: (str(r["g"].get("Start (CT)") or ""), r["g"]["GameId"]))
    return rows


def standings(rows: list[dict], teams: list[str], through_week: int | None = None) -> list[dict]:
    rec = collections.Counter()
    tot = collections.Counter({t: 0 for t in teams})
    for r in rows:
        if through_week is None or r["g"]["week"] <= through_week:
            tot[r["team"]] += 1
            rec[r["team"]] += r["hit"]
    order = sorted(tot, key=lambda t: (-rec[t], t))
    return [{"team": t, "w": rec[t], "l": tot[t] - rec[t], "rank": 1 + sum(1 for u in order if rec[u] > rec[t])}
            for t in order]


def full_weeks(data: dict, season: int) -> list[int]:
    """Weeks (<=15) whose every pool game is final."""
    by = collections.defaultdict(list)
    for g in data["games"]:
        if g.get("cfb_season") == season and g.get("week") is not None and g["week"] <= lore.SEASON_WEEKS:
            by[g["week"]].append(g)
    return sorted(w for w, gs in by.items() if gs and all(g.get("winner") for g in gs))


def build_facts(data: dict, today: str | None = None) -> dict:
    """{'season', 'week', 'facts': {id: text}, 'member_ids': {team: [ids]},
    'records': {team: [w, l]}, 'picky': [w, l] | None, 'final_games': n}."""
    season = max(g["cfb_season"] for g in data["games"] if g.get("cfb_season"))
    weeks = full_weeks(data, season)
    week = weeks[-1] if weeks else None
    active = [t["team_name"] for t in data["teams"] if t.get("active") and not t.get("is_exhibition")]
    rows = graded_rows(data, season)
    rows = [r for r in rows if r["team"] in active]
    comp = lore.compute({**data, "teams": [t for t in data["teams"] if t["team_name"] in active]})
    st = standings(rows, active)
    prev = standings(rows, active, week - 1) if week else None
    n = len(st)
    facts: dict[str, str] = {}
    member_ids: dict[str, list[str]] = collections.defaultdict(list)

    def add(text: str, team: str | None = None) -> str:
        fid = f"F{len(facts) + 1}"
        facts[fid] = re.sub(r"\s+", " ", text).strip()
        if team:
            member_ids[team].append(fid)
        return fid

    lead_w = st[0]["w"] if st else 0
    cut = sorted((r["w"] for r in st), reverse=True)[3] if n > 4 else None
    # league-wide
    top = ", ".join(f"{r['team']} {r['w']}-{r['l']}" for r in st[:5])
    add(f"Standings through week {week}: top five {top}. {n} members.")
    if cut is not None:
        bubble = [r for r in st if abs(r["w"] - cut) <= 1]
        add(f"Playoff cut line (4th place) is {cut} wins; within one win of it: "
            + ", ".join(f"{r['team']} {r['w']}-{r['l']}" for r in bubble) + ".")
    add(f"Last place: {', '.join(r['team'] + ' ' + str(r['w']) + '-' + str(r['l']) for r in st if r['rank'] == st[-1]['rank'])}.")
    if prev:
        pr = {r["team"]: r["rank"] for r in prev}
        moves = sorted(((pr[r["team"]] - r["rank"], r["team"], pr[r["team"]], r["rank"]) for r in st), reverse=True)
        up = [m for m in moves if m[0] >= 2][:2]
        down = [m for m in moves[::-1] if m[0] <= -2][:2]
        for d, t, a, b in up + down:
            add(f"{t} moved from {_ord(a)} to {_ord(b)} place in week {week}.", t)
    a = (comp.get("awards") or {}).get(week) or {}
    if a.get("lock"):
        add(f"Week {week} Lock of the Week: {a['lock'][0]} ({a['lock'][1]} of {a['lock'][2]} had it).")
    if a.get("trap"):
        add(f"Week {week} Trap Game: {a['trap'][0]} ({a['trap'][1]} of {a['trap'][2]} got burned).")
    if a.get("lone"):
        add(f"Week {week} Lone Wolf: {a['lone'][0]} was the only one right on {a['lone'][1]}.", a["lone"][0])
    wk_rows = [r for r in rows if r["g"]["week"] == week]
    wk_rec = {t: _rec([r for r in wk_rows if r["team"] == t]) for t in active}
    perfect = [t for t, (w, l) in wk_rec.items() if w and not l]
    winless = [t for t, (w, l) in wk_rec.items() if l and not w]
    if perfect:
        add(f"Went perfect in week {week}: {', '.join(perfect)}.")
    if winless:
        add(f"Went winless in week {week}: {', '.join(winless)}.")
    dawg = comp.get("top_dawg") or {}
    if dawg:
        add("Top Dawg (picked an underdog that won outright) this season: "
            + ", ".join(f"{t} {c}" for t, c in dawg.most_common(4)) + ".")
    # per member
    records = {}
    for r in st:
        t = r["team"]
        records[t] = [r["w"], r["l"]]
        mine = [x for x in rows if x["team"] == t]
        behind = lead_w - r["w"]
        add(f"{t}: {r['w']}-{r['l']} this season, {_ord(r['rank'])} of {n}"
            + (f", {behind} behind first" if behind else (", in first" if r["rank"] == 1 else ""))
            + (f", {r['w'] - cut} above the cut line" if cut is not None and r["w"] > cut else
               f", {cut - r['w']} below the cut line" if cut is not None and r["w"] < cut else
               ", right on the cut line" if cut is not None else "") + ".", t)
        wr = [x for x in mine if x["g"]["week"] == week]
        if wr:
            w, l = _rec(wr)
            picks = ", ".join(f"{lore._fmt_line(x['side'], x['g'])} {'hit' if x['hit'] else 'miss'}" for x in wr)
            add(f"{t} went {w}-{l} in week {week}: {picks}.", t)
        if mine:
            last = mine[-1]["hit"]
            cur = 0
            for x in reversed(mine):
                if x["hit"] != last:
                    break
                cur += 1
            if cur >= 3:
                add(f"{t} is on a {cur}-pick {'hit' if last else 'miss'} streak.", t)
            l8 = mine[-8:]
            w8, ls8 = _rec(l8)
            add(f"{t} is {w8}-{ls8} over the last 8 picks.", t)
            favs = [x for x in mine if x["fav"]]
            dogs = [x for x in mine if x["dog"]]
            if favs or dogs:
                pct = round(100 * len(favs) / (len(favs) + len(dogs)))
                fw, fl = _rec(favs)
                dw, dl = _rec(dogs)
                add(f"{t} takes the favorite {pct}% of the time; favorites went {fw}-{fl}, underdogs {dw}-{dl}.", t)
        s = ((comp["members"].get(t) or {}).get("seasons") or {}).get(season)
        if s:
            bits = []
            if s.get("perfect"):
                bits.append("perfect weeks: " + ", ".join(f"week {w}" for w in s["perfect"]))
            if s.get("winless"):
                bits.append("winless weeks: " + ", ".join(f"week {w}" for w in s["winless"]))
            bits.append(f"longest hit streak {s['hit_streak']}, longest miss streak {s['miss_streak']}")
            add(f"{t} this season: " + "; ".join(bits) + ".", t)
        m = comp["members"].get(t) or {}
        for x in m.get("upsets") or []:
            if f"{season} week" in x:
                add(f"{t} best upset hit: {x}.", t)
        for x in m.get("blown") or []:
            if f"{season} week" in x:
                add(f"{t} worst blown pick: {x}.", t)
        for x in (m.get("lone_hits") or []) + (m.get("lone_misses") or []):
            if f"{season} week" in x:
                add(f"{t} was {x}.", t)
        h = m.get("h2h") or {}
        if h.get("best") and h.get("worst"):
            add(f"{t} head to head (opposite sides, since 2025): best vs {h['best'][0]} {h['best'][1][0]}-"
                f"{h['best'][1][1]}, worst vs {h['worst'][0]} {h['worst'][1][0]}-{h['worst'][1][1]}.", t)
    # Picky himself (exhibition team, not in the standings)
    prow = [r for r in graded_rows(data, season, include_exhibition=True) if r["team"] == PICKY_TEAM]
    picky = None
    if prow:
        pw, pl = _rec(prow)
        picky = [pw, pl]
        add(f"Picky (exhibition, not in the standings) is {pw}-{pl} on his own picks this season.", PICKY_TEAM)
        pk = [r for r in prow if r["g"]["week"] == week]
        if pk:
            w, l = _rec(pk)
            add(f"Picky went {w}-{l} in week {week}: "
                + ", ".join(f"{lore._fmt_line(x['side'], x['g'])} {'hit' if x['hit'] else 'miss'}" for x in pk)
                + ".", PICKY_TEAM)
    return {"season": season, "week": week, "facts": facts, "member_ids": dict(member_ids), "records": records,
            "picky": picky, "final_games": sum(1 for g in data["games"] if g.get("cfb_season") == season
                                                and g.get("winner")), "teams": active}


# -----------------------------------------------------------------------------
# validate
# -----------------------------------------------------------------------------

def chat_milk_spill(text: str) -> bool:
    import chat  # same guard the live sentence filter uses
    return bool(chat._MILK_SPILL.search(text))


def _line_ok(text: str, fids: list[str], facts: dict[str, str], teams: list[str], max_words: int = 50) -> str | None:
    """Why a line is bad (None = good)."""
    if not 8 <= len(text) <= max_words * 7 or len(text.split()) > max_words:
        return "length"
    if not fids:
        return "no valid fact ids"
    src = " ".join(facts[f] for f in fids)
    have = set(_NUM.findall(src))
    bad = [n for n in _NUM.findall(text) if n not in have]
    if bad:
        return f"numbers not in cited facts: {bad}"
    for m in re.finditer(r"\b(\d+)-(\d+)\b", text):
        if m.group(0) not in src:
            return f"record {m.group(0)} not in cited facts"
    named = [t for t in teams + [PICKY_TEAM] if re.search(rf"(?<!\w){re.escape(t)}(?!\w)", text)]
    missing = [t for t in named if t not in src and t != PICKY_TEAM]
    if missing:
        return f"names a member not in cited facts: {missing}"
    if chat_milk_spill(text):
        return "milk spill (Blinkstradamus never spilled it)"
    if rare_bits.mentions("milk", text):
        return "milk bit (once a month, rationed live by rare_bits; never in the material sheet)"
    if _BANNED.search(text):
        return f"banned topic: {_BANNED.search(text).group(0)}"
    if _REPORTY.search(text):
        return "report-speak / fact ids in text"
    return None


def validate(raw: dict, fs: dict) -> tuple[dict, list[str]]:
    facts, teams = fs["facts"], fs["teams"]
    dropped: list[str] = []
    members = {}
    for m in raw.get("members") or []:
        t = str(m.get("team") or "").strip()
        if t not in teams:
            dropped.append(f"unknown member {t!r}")
            continue
        keep = []
        for x in (m.get("lines") or [])[: LINES_MAX + 2]:
            text = re.sub(r"\s+", " ", str(x.get("text") or "")).strip()
            fids = [f for f in x.get("facts") or [] if f in facts]
            kind = x.get("kind") if x.get("kind") in ("roast", "praise", "leader") else "roast"
            if kind == "leader" and t != LEADER_TEAM:
                kind = "praise"
            why = _line_ok(text, fids, facts, teams)
            if why:
                dropped.append(f"{t}: {why}: {text[:80]}")
                continue
            keep.append({"text": text, "kind": kind, "facts": [facts[f] for f in fids]})
        if keep:
            members[t] = {"record": fs["records"].get(t), "lines": keep[:LINES_MAX]}
    stories = []
    for x in raw.get("storylines") or []:
        text = re.sub(r"\s+", " ", str(x.get("text") or "")).strip()
        fids = [f for f in x.get("facts") or [] if f in facts]
        why = _line_ok(text, fids, facts, teams)
        if why:
            dropped.append(f"storyline: {why}: {text[:80]}")
            continue
        stories.append({"text": text, "facts": [facts[f] for f in fids]})
    pn = raw.get("picky_narrative") or {}
    ptext = re.sub(r"\s+", " ", str(pn.get("text") or "")).strip()
    pf = [f for f in pn.get("facts") or [] if f in facts]
    why = _line_ok(ptext, pf, facts, teams, max_words=90) if ptext else "missing"
    if why:
        dropped.append(f"picky_narrative: {why}: {ptext[:80]}")
    return {"members": members, "storylines": stories[:5],
            "picky_narrative": None if why else {"text": ptext, "facts": [facts[f] for f in pf]}}, dropped


# -----------------------------------------------------------------------------
# run
# -----------------------------------------------------------------------------

def build_prompt(fs: dict, today: str) -> str:
    return PROMPT.format(n=len(fs["teams"]), today=today, season=fs["season"], week=fs["week"],
                         facts="\n".join(f"{k}: {v}" for k, v in fs["facts"].items()),
                         leader=LEADER_TEAM, lo=LINES_MIN, hi=LINES_MAX)


def due(fs: dict, prev: dict | None, now: dt.datetime) -> str | None:
    if not fs.get("week"):
        return None
    if not prev or prev.get("season") != fs["season"]:
        return "new"
    if (prev.get("week") or 0) < fs["week"]:
        return "final"
    try:
        gen = dt.datetime.fromisoformat(prev["generated_at"])
    except (KeyError, ValueError):
        return "new"
    wed = dossier._last(now, 2, 10)
    if gen < wed <= now:
        return "midweek"
    recs = {t: m.get("record") for t, m in (prev.get("members") or {}).items()}
    if any(recs.get(t) not in (None, r) for t, r in fs["records"].items()) and \
            now.timestamp() - gen.timestamp() > DRIFT_COOLDOWN_S:
        return "drift"
    return None


def write_private(p: Path, data: dict) -> None:
    dossier.write_atomic(p, data)
    os.chmod(p, 0o600)


def run(force: bool = False, out: Path | None = None, data: dict | None = None, research=None,
        now: dt.datetime | None = None) -> dict:
    out = out or OUT
    now = now or now_ct()
    data = data or lore.load(lore.service_key())
    fs = build_facts(data)
    prev = dossier.read_json(out)
    reason = "forced" if force and fs.get("week") else due(fs, prev, now)
    fail = out.with_suffix(".failed")
    if reason and not force and fail.exists() and time.time() - fail.stat().st_mtime < FAIL_BACKOFF_S:
        log.info("week %s %s due, but the last run failed under %dh ago; waiting", fs.get("week"), reason,
                 FAIL_BACKOFF_S // 3600)
        return {"ok": True, "built": False, "week": fs.get("week"), "backoff": True}
    if not reason:
        log.info("week %s: nothing due", fs.get("week"))
        return {"ok": True, "built": False, "week": fs.get("week")}
    research = research or (lambda p: dossier.run_claude(p, TIMEOUT_S, SCHEMA, ""))
    t0 = time.time()
    try:
        raw = research(build_prompt(fs, now.strftime("%A %Y-%m-%d")))
        sheet, dropped = validate(raw, fs)
    except Exception:
        fail.touch()
        raise
    covered = len(sheet["members"])
    if covered < max(1, len(fs["teams"]) // 2):
        fail.touch()
        raise RuntimeError(f"only {covered} of {len(fs['teams'])} members have valid lines; kept previous "
                           f"({len(dropped)} dropped: {'; '.join(dropped)[:300]})")
    doc = {"schema": 1, "season": fs["season"], "week": fs["week"], "generated_at": now.isoformat(timespec="seconds"),
           "trigger": reason, "by": "claude -p sonnet (no tools)", "final_games": fs["final_games"],
           "picky_record": fs["picky"], **sheet, "dropped": dropped, "facts": fs["facts"]}
    write_private(out, doc)
    fail.unlink(missing_ok=True)
    n = sum(len(m["lines"]) for m in sheet["members"].values())
    log.info("week %s %s: %d members, %d lines, %d storylines, narrative %s, %d dropped, %.0fs", fs["week"], reason,
             covered, n, len(sheet["storylines"]), bool(sheet["picky_narrative"]), len(dropped), time.time() - t0)
    return {"ok": True, "built": True, "week": fs["week"], "trigger": reason, "members": covered, "lines": n,
            "dropped": len(dropped)}


def _take_lock() -> bool:
    try:
        LOCK.mkdir()
        return True
    except FileExistsError:
        if time.time() - LOCK.stat().st_mtime > LOCK_STALE_S:
            shutil.rmtree(LOCK, ignore_errors=True)
            return _take_lock()
        return False


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["run", "facts", "status"])
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args(argv)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.FileHandler(LOG_PATH)] + ([logging.StreamHandler()] if sys.stderr.isatty() else []))
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    if a.cmd == "facts":
        fs = build_facts(lore.load(lore.service_key()))
        print(build_prompt(fs, now_ct().strftime("%A %Y-%m-%d")).split("FACTS (")[1].split("RUNNING BITS")[0])
        return 0
    if a.cmd == "status":
        d = dossier.read_json(OUT) or {}
        print(json.dumps({k: d.get(k) for k in ("season", "week", "generated_at", "trigger")} |
                         {"members": len(d.get("members") or {}), "storylines": len(d.get("storylines") or []),
                          "dropped": len(d.get("dropped") or [])}, indent=1))
        return 0
    if not _take_lock():
        log.info("another material run is in progress; skipping")
        return 0
    try:
        res = run(force=a.force)
    except Exception as e:  # noqa: BLE001 - keep the previous sheet, retry next tick
        log.exception("material run failed: %s", e)
        return 1
    finally:
        # text Picky's comeback/banter bank rides the same hourly tick (weekly, never raises)
        banter_res = banter.refresh_if_due()
        if banter_res and (banter_res.get("built") or not banter_res.get("ok")):
            log.info("banter bank: %s", banter_res)
        shutil.rmtree(LOCK, ignore_errors=True)
    print(json.dumps(res))
    return 0


if __name__ == "__main__":
    sys.exit(main())
