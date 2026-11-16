"""
Slate intel: sourced personnel/availability notes (QB status, key injuries,
interim coaches, opt-outs) for the week's notable NON-pool games, so Picky's
slate cards (slate_cards.py) can answer "is X playing / who's the backup".
Pool games already get full dossiers (dossier.py).

One offline Opus call (claude -p via dossier.run_claude: WebSearch/WebFetch
only, strict empty MCP, no API key) covers up to MAX_GAMES games. Every claim
goes through dossier.validate (source URL required, paid sites dropped, dead
URLs dropped, forum posts demoted to rumors, nothing older than 30 days).
Cache: cache/slate_intel.json. Picky never needs it: if Anthropic is down or
the file is older than MAX_AGE, cards just say personnel is unknown.

    slate_intel.py run [--force]    # due: no file / >12 h old / Fri after 5 PM / Sat after 7 AM CT
    slate_intel.py show
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

log = logging.getLogger("picky.slate_intel")
CT = ZoneInfo("America/Chicago")
CACHE = Path(os.environ.get("PICKY_SLATE_INTEL", HERE / "cache" / "slate_intel.json"))
MAX_GAMES = 16
MAX_AGE = dt.timedelta(hours=36)
REFRESH_EVERY = dt.timedelta(hours=12)
TIMEOUT_S = 1500

PROMPT = """Today is {today}. For each college football game below (week {week}), find the CURRENT availability news a sharp bettor needs before Saturday: starting QB status (injured? who starts instead, and the backup's experience), other key injuries (RB1, WR1, OL starters, top pass rusher, CB1), suspensions, opt-outs, and an interim or fired coach. Use web search; free public sources only (ESPN, CBS, Yahoo, team sites, local papers; NOT 247Sports, On3, Rivals, The Athletic, Action Network). Only news from the last 30 days. If a game has nothing notable, return no claims for it. Never guess: a claim needs a source URL that says it.

Each claim: text (one sentence, plain facts, <= 300 chars, no URL in the text), kind (injury | fact | insider_opinion | rumor), team (away | home | both), players (names), status for injuries (out | doubtful | questionable | probable | returning | unknown), source_url, source_name, date (YYYY-MM-DD of the article), confidence (high | medium | low). At most 5 claims per game, most important first.

GAMES:
{games}

Return JSON only: {{"games": [{{"game_id": 123, "claims": [...]}}, ...]}}."""

SCHEMA = {"type": "object", "properties": {"games": {"type": "array", "items": {
    "type": "object", "properties": {"game_id": {"type": "integer"},
                                     "claims": {"type": "array", "items": {"type": "object"}}},
    "required": ["game_id", "claims"]}}}, "required": ["games"]}


def load() -> dict | None:
    """The cache if it's fresh enough to quote, else None."""
    try:
        d = json.loads(CACHE.read_text())
        at = dt.datetime.fromisoformat(d["fetched_at"])
    except (OSError, ValueError, KeyError):
        return None
    return d if dt.datetime.now(dt.timezone.utc) - at <= MAX_AGE else None


def claims_for(game_id) -> tuple[list[dict], str | None]:
    d = load()
    if not d:
        return [], None
    g = (d.get("games") or {}).get(str(game_id)) or {}
    return (g.get("facts") or []) + (g.get("rumors") or [])[:1], d.get("as_of")


def due(now: dt.datetime) -> bool:
    try:
        at = dt.datetime.fromisoformat(json.loads(CACHE.read_text())["fetched_at"]).astimezone(CT)
    except (OSError, ValueError, KeyError):
        return True
    if now - at >= REFRESH_EVERY:
        return True
    for wd, hr in ((4, 17), (5, 7)):  # Fri 5 PM, Sat 7 AM CT
        mark = (now - dt.timedelta(days=(now.weekday() - wd) % 7)).replace(hour=hr, minute=0, second=0, microsecond=0)
        if at < mark <= now:
            return True
    return False


def run(force: bool = False) -> int:
    import cfbd
    import dossier
    import slate_cards
    now = dt.datetime.now(CT)
    if not force and not due(now):
        print("not due")
        return 0
    idx = cfbd.load_index()
    games = slate_cards.notable(slate_cards.upcoming_games(idx, dt.datetime.now(dt.timezone.utc)), idx)
    games = games[:MAX_GAMES]
    if not games:
        print("no upcoming games")
        return 0
    week = games[0].get("week")
    listing = "\n".join(f"- game_id {g['id']}: {g['awayTeam']} (away) at {g['homeTeam']} (home), "
                        f"{dt.datetime.fromisoformat(g['startDate'].replace('Z', '+00:00')).astimezone(CT):%a %b %-d %-I:%M %p} CT"
                        for g in games)
    try:
        raw = dossier.run_claude(PROMPT.format(today=now.strftime("%A %Y-%m-%d"), week=week, games=listing),
                                 TIMEOUT_S, SCHEMA)
    except Exception as e:  # noqa: BLE001 - Anthropic down / kill switch: keep the last file
        print(f"slate intel refresh failed ({e!r:.200}); keeping the last good file")
        return 1
    ids = {g["id"] for g in games}
    names = {g["id"]: (g["awayTeam"], g["homeTeam"]) for g in games}
    out = {}
    with httpx.Client() as client:
        for g in raw.get("games") or []:
            gid = g.get("game_id")
            if gid not in ids:
                continue
            claims = [c for c in g.get("claims") or [] if isinstance(c, dict)]
            for c in claims:
                c.setdefault("confidence", "medium")
                c.setdefault("players", [])
            urls = sorted({str(c.get("source_url") or "") for c in claims if c.get("source_url")})
            try:
                secs, dropped = dossier.validate({"claims": claims}, now, dossier.check_urls(client, urls))
            except ValueError:
                continue
            out[str(gid)] = {"away": names[gid][0], "home": names[gid][1], **secs, "dropped": len(dropped)}
    doc = {"fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
           "as_of": now.strftime("%a %b %-d %-I:%M %p CT"), "week": week, "games": out}
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CACHE.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, indent=1))
    os.replace(tmp, CACHE)
    n = sum(len(v.get("facts") or []) for v in out.values())
    print(f"slate intel: {len(out)} games, {n} facts, as of {doc['as_of']}")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "show":
        print(json.dumps(load(), indent=1))
        sys.exit(0)
    sys.exit(run(force="--force" in sys.argv))
