"""
Slate intel: sourced personnel/availability notes (QB status, key injuries,
interim coaches, opt-outs) for the week's notable NON-pool games, so Picky's
slate cards (slate_cards.py) can answer "is X playing / who's the backup".
Pool games already get full dossiers (dossier.py).

Code first over free feeds, with an optional Claude booster (zero maintenance,
phase 8). The code refresh runs every hourly tick:
  - per game: news.py's cached stories that name either team, through the
    dossier's code researcher and validator (dossier.code_research + validate:
    the lead's URL, outlet and date, injury status, paid sites dropped, nothing
    older than 30 days). A game with no new story keeps its last notes.
  - CFP committee rankings: ESPN's public rankings API (the "cfp" poll), with
    its release date. A format change or a failed fetch keeps the last good
    list and logs a feed-health line; cfp() drops a list released more than
    CFP_MAX_DAYS ago (the offseason never sees last season's top 12 as current).
  - coaching changes: news headlines that lead with an FBS school firing or
    parting with its coach / naming an interim, kept COACHING_KEEP_DAYS.
Claude booster (picky_claude's capped gate; Charlie: "If Claude is active, Claude
can help Picky with the advanced analysis"): one Opus web-research call at the
Fri 5 PM and Sat 7 AM CT runs (BOOST_MARKS, within BOOST_WINDOW), only when the
gate is open. Its claims go through the same dossier.validate (source URL,
paid sites and dead URLs dropped, nothing older than 30 days) and ride on top
of the code notes for MAX_AGE; every game says which one it carries ("by":
"claude" | "code") so lanes can phrase confidence. Gate shut, call failed or
timed out: the code notes stand.
Cache: cache/slate_intel.json. Picky never needs it: missing or older than
MAX_AGE, cards just say personnel is unknown.

    slate_intel.py run [--force]    # hourly (com.pickem.slate-intel); due once REFRESH_EVERY old
    slate_intel.py show
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

log = logging.getLogger("picky.slate_intel")
CT = ZoneInfo("America/Chicago")
CACHE = Path(os.environ.get("PICKY_SLATE_INTEL", HERE / "cache" / "slate_intel.json"))
MAX_GAMES = 28
MAX_AGE = dt.timedelta(hours=36)
REFRESH_EVERY = dt.timedelta(minutes=50)   # code only: cheap, so every hourly tick
RANKINGS_URL = "https://site.api.espn.com/apis/site/v2/sports/football/college-football/rankings"
CFP_MAX_DAYS = 50          # the final CFP ranking (early Dec) stands through the title game, not into spring
COACHING_KEEP_DAYS = 150
_COACH_CHANGE = re.compile(r"\b(fires|fired|parts? ways|resigns|resigned|steps? down|dismiss(?:es|ed)|"
                           r"names? .{0,40}\binterim|interim (?:head )?coach)\b", re.I)
_COACH_TALK = re.compile(r"\b(deserve|accus\w*|trying|should|would|could|might|hot seat|rumou?r\w*|if)\b|\?", re.I)
BOOST_MARKS = ((4, 17), (5, 7))            # Fri 5 PM, Sat 7 AM CT: the booster's two runs a week
BOOST_WINDOW = dt.timedelta(hours=12)      # a mark missed by more than this waits for the next one
TIMEOUT_S = 1500

PROMPT = """Today is {today}. For each college football game below (week {week}), find the CURRENT availability news a sharp bettor needs before Saturday: starting QB status (injured? who starts instead, and the backup's experience), other key injuries (RB1, WR1, OL starters, top pass rusher, CB1), suspensions, opt-outs, and an interim or fired coach. Use web search; free public sources only (ESPN, CBS, Yahoo, team sites, local papers; NOT 247Sports, On3, Rivals, The Athletic, Action Network). Only news from the last 30 days. If a game has nothing notable, return no claims for it. Never guess: a claim needs a source URL that says it.

Each claim: text (one sentence, plain facts, <= 300 chars, no URL in the text), kind (injury | fact | insider_opinion | rumor), team (away | home | both), players (names), status for injuries (out | doubtful | questionable | probable | returning | unknown), source_url, source_name, date (YYYY-MM-DD of the article), confidence (high | medium | low). At most 5 claims per game, most important first.

GAMES:
{games}

ALSO give the College Football Playoff selection committee's LATEST rankings: the release date, the next scheduled release date if announced, and the top 25 (rank, team as ESPN names it, record), with a source URL.

ALSO list every FBS head-coach change during THIS season (since August 1): firings, resignations, a coach announced as leaving, and who the interim coach is. Each: school (as ESPN names it), text (one sentence: who was fired/left, when, who's interim), source_url, date (YYYY-MM-DD of the article).

Return JSON only: {{"games": [{{"game_id": 123, "claims": [...]}}, ...], "coaching": [{{"school": "...", "text": "...", "source_url": "...", "date": "..."}}, ...], "cfp": {{"released": "YYYY-MM-DD", "next_release": "YYYY-MM-DD or empty", "source_url": "...", "top": [{{"rank": 1, "team": "...", "record": "9-1"}}, ...]}}}}."""

SCHEMA = {"type": "object", "properties": {"games": {"type": "array", "items": {
    "type": "object", "properties": {"game_id": {"type": "integer"},
                                     "claims": {"type": "array", "items": {"type": "object"}}},
    "required": ["game_id", "claims"]}},
    "coaching": {"type": "array", "items": {"type": "object", "properties": {
        "school": {"type": "string"}, "text": {"type": "string"}, "source_url": {"type": "string"},
        "date": {"type": "string"}}, "required": ["school", "text", "source_url", "date"]}},
    "cfp": {"type": "object", "properties": {"released": {"type": "string"}, "next_release": {"type": "string"},
            "source_url": {"type": "string"}, "top": {"type": "array", "items": {"type": "object", "properties": {
                "rank": {"type": "integer"}, "team": {"type": "string"}, "record": {"type": "string"}},
                "required": ["rank", "team"]}}}, "required": ["released", "source_url", "top"]}}, "required": ["games"]}




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


def cfp(now: dt.datetime | None = None) -> dict | None:
    """The committee's latest rankings from the last good refresh ({released, released_on, source_name, top}),
    or None once that release is more than CFP_MAX_DAYS old."""
    d = load() or {}
    c = d.get("cfp") or None
    if c and c.get("released_on"):
        today = (now or dt.datetime.now(CT)).astimezone(CT).date()
        try:
            if (today - dt.date.fromisoformat(c["released_on"])).days > CFP_MAX_DAYS:
                return None
        except ValueError:
            return None
    return c


def coaching_changes() -> dict[str, list[dict]]:
    """school (normalized) -> sourced in-season coaching changes; {} when stale/missing."""
    import cfbd
    d = load() or {}
    out: dict[str, list[dict]] = {}
    for c in d.get("coaching") or []:
        out.setdefault(cfbd.norm(c.get("school")), []).append(c)
    return out


def due(now: dt.datetime) -> bool:
    try:
        at = dt.datetime.fromisoformat(json.loads(CACHE.read_text())["fetched_at"]).astimezone(CT)
    except (OSError, ValueError, KeyError):
        return True
    return now - at >= REFRESH_EVERY


def espn_cfp(transport=None) -> dict | None:
    """The CFP committee poll from ESPN's public rankings API, or None (no poll out yet, or a fetch / format
    problem: that one logs a feed-health line and the caller keeps the last good list)."""
    import feed_health
    try:
        with httpx.Client(timeout=20, transport=transport) as c:
            r = c.get(RANKINGS_URL)
            r.raise_for_status()
            data = r.json()
    except (httpx.HTTPError, ValueError) as e:
        feed_health.note("rankings", f"ESPN rankings fetch failed: {type(e).__name__}: {str(e)[:120]}")
        return None
    polls = data.get("rankings") if isinstance(data, dict) else None
    if not isinstance(polls, list):
        feed_health.note("rankings", "ESPN rankings: no 'rankings' list in the response (format change?)")
        return None
    poll = next((x for x in polls if isinstance(x, dict) and x.get("type") == "cfp"), None)
    if poll is None:
        return None  # before the first committee ranking (early November) there's none: not a failure
    ranks = poll.get("ranks")
    top = []
    for x in ranks if isinstance(ranks, list) else []:
        team = (x.get("team") or {}) if isinstance(x, dict) else {}
        name = team.get("location") or team.get("nickname")
        if isinstance(x.get("current") if isinstance(x, dict) else None, int) and name:
            row = {"rank": x["current"], "team": name}
            if x.get("recordSummary"):
                row["record"] = x["recordSummary"]
            top.append(row)
    try:
        rel = dt.date.fromisoformat(str(poll.get("date"))[:10])
    except ValueError:
        rel = None
    if len(top) < 12 or rel is None:
        feed_health.note("rankings", f"ESPN CFP poll unreadable ({len(top)} teams, date {poll.get('date')!r}); "
                                     "kept the last good list")
        return None
    return {"released": rel.strftime("%a %b %-d"), "released_on": rel.isoformat(), "next_release": "",
            "source_name": "espn.com", "top": sorted(top, key=lambda t: t["rank"])[:25]}


def coaching_from_news(items: list[dict], now: dt.datetime, prev: list[dict] | None) -> list[dict]:
    """Headlines that lead with an FBS school firing / parting with its coach or naming an interim (news.py tags
    FBS teams only), newest per school, merged with the last list; kept COACHING_KEEP_DAYS."""
    import cfbd
    import dossier
    rows: dict[str, dict] = {}
    for c in prev or []:
        try:
            d = dt.date.fromisoformat(str(c.get("date"))[:10])
        except ValueError:
            continue
        if 0 <= (now.date() - d).days <= COACHING_KEEP_DAYS and c.get("school"):
            rows[cfbd.norm(c["school"])] = c
    for it in sorted(items, key=lambda x: str(x.get("date") or "")):
        title = re.sub(r"\s+", " ", str(it.get("title") or "")).strip()
        teams = it.get("teams") or []
        if len(teams) != 1 or not _COACH_CHANGE.search(title) or _COACH_TALK.search(title) \
                or not 10 <= len(title) <= 300:
            continue
        school = teams[0]
        if not cfbd.norm(title).startswith(cfbd.norm(school)):  # 'Arkansas State fires ...', not a passing mention
            continue
        link = str(it.get("link") or it.get("url") or "")
        try:
            d = dt.date.fromisoformat(str(it.get("date"))[:10])
        except ValueError:
            continue
        if not dossier._url_ok(link) or dossier._paid(link) or d > now.date() + dt.timedelta(days=1) \
                or (now.date() - d).days > COACHING_KEEP_DAYS:
            continue
        rows[cfbd.norm(school)] = {"school": school, "text": title, "date": d.isoformat(),
                                   "source_name": dossier._domain(link)}
    return sorted(rows.values(), key=lambda c: c["date"], reverse=True)


def boost_due(prev: dict, now: dt.datetime) -> bool:
    """A Fri 5 PM / Sat 7 AM CT mark passed within BOOST_WINDOW and the booster hasn't run since."""
    try:
        at = dt.datetime.fromisoformat(prev["boost"]["at"]).astimezone(CT)
    except (KeyError, TypeError, ValueError):
        at = None
    now = now.astimezone(CT)
    for wd, hr in BOOST_MARKS:
        mark = (now - dt.timedelta(days=(now.weekday() - wd) % 7)).replace(hour=hr, minute=0, second=0, microsecond=0)
        if mark <= now < mark + BOOST_WINDOW and (at is None or at < mark):
            return True
    return False


def claude_layer(games: list[dict], now: dt.datetime, transport=None) -> dict:
    """One Opus web-research call over the games, validated like a dossier. Raises on any failure."""
    import dossier
    listing = "\n".join(f"- game_id {g['id']}: {g['awayTeam']} (away) at {g['homeTeam']} (home), "
                        f"{dt.datetime.fromisoformat(g['startDate'].replace('Z', '+00:00')).astimezone(CT):%a %b %-d %-I:%M %p} CT"
                        for g in games)
    raw = dossier.run_claude(PROMPT.format(today=now.strftime("%A %Y-%m-%d"), week=games[0].get("week"),
                                           games=listing), TIMEOUT_S, SCHEMA, caller="slate_intel")
    names = {g["id"]: (g["awayTeam"], g["homeTeam"]) for g in games}
    out, coaching = {}, []
    with httpx.Client(timeout=20, transport=transport) as client:
        for g in raw.get("games") or []:
            gid = g.get("game_id")
            if gid not in names:
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
            if any(secs.get(k) for k in ("facts", "opinions", "rumors")):
                out[str(gid)] = {"away": names[gid][0], "home": names[gid][1], **secs, "dropped": len(dropped),
                                 "by": "claude"}
        rows = [c for c in raw.get("coaching") or [] if isinstance(c, dict)]
        st = dossier.check_urls(client, sorted({str(c.get("source_url") or "") for c in rows}))
        for c in rows:
            url, text = str(c.get("source_url") or ""), re.sub(r"\s+", " ", str(c.get("text") or "")).strip()
            try:
                d = dt.date.fromisoformat(str(c.get("date"))[:10])
            except ValueError:
                continue
            if (not dossier._url_ok(url) or dossier._paid(url) or st.get(url) == "dead" or not 10 <= len(text) <= 300
                    or d > now.date() + dt.timedelta(days=1) or (now.date() - d).days > COACHING_KEEP_DAYS):
                continue
            coaching.append({"school": str(c.get("school") or "").strip(), "text": text, "date": d.isoformat(),
                             "source_name": dossier._domain(url), "by": "claude"})
    return {"at": now.astimezone(dt.timezone.utc).isoformat(timespec="seconds"), "games": out, "coaching": coaching}


def run(force: bool = False, now: dt.datetime | None = None, games: list[dict] | None = None,
        items: list[dict] | None = None, transport=None) -> int:
    """One code refresh, plus the Claude booster when it's due and the gate is open. games / items / transport:
    tests (default: the CFBD cache's notable upcoming games, news.py's cache, the real ESPN API)."""
    import cfbd
    import dossier
    import picky_claude
    now = now or dt.datetime.now(CT)
    if not force and not due(now):
        print("not due")
        return 0
    if games is None:
        import slate_cards
        idx = cfbd.load_index()
        games = slate_cards.notable(slate_cards.upcoming_games(idx, now.astimezone(dt.timezone.utc)), idx,
                                    n=20, movers=True)[:MAX_GAMES] if idx else []
    items = dossier.news_items() if items is None else items
    try:
        prev = json.loads(CACHE.read_text())
    except (OSError, ValueError):
        prev = {}
    prev_games = prev.get("games") or {}
    out = {}
    for g in games:
        gid = str(g["id"])
        away, home = g["awayTeam"], g["homeTeam"]
        leads = dossier.news_leads(away, home, items=items)
        secs, dropped = dossier.validate(dossier.code_research({"Away": away, "Home": home}, leads, []), now)
        if not any(secs.get(k) for k in ("facts", "opinions", "rumors")) and (prev_games.get(gid) or {}).get("by", "code") == "code":
            out[gid] = prev_games[gid]  # nothing new in the feed: the game keeps its last notes
            continue
        out[gid] = {"away": away, "home": home, **secs, "dropped": len(dropped), "by": "code"}
    boost = prev.get("boost")
    try:
        if now - dt.datetime.fromisoformat(boost["at"]) > MAX_AGE:
            boost = None
    except (KeyError, TypeError, ValueError):
        boost = None
    if games and boost_due(prev, now):
        why = picky_claude.why_not()
        if why:
            print(f"slate intel booster skipped ({why}); code notes only")
        else:
            try:
                boost = claude_layer(games, now, transport)
            except Exception as e:  # noqa: BLE001 - Claude failed / timed out: the code notes stand
                print(f"slate intel booster failed ({e!r:.200}); code notes only")
    for gid, g in ((boost or {}).get("games") or {}).items():
        if gid in out:
            out[gid] = g
    cfp_doc = espn_cfp(transport) or prev.get("cfp")
    coaching = coaching_from_news(items, now, [c for c in prev.get("coaching") or [] if c.get("by") != "claude"])
    have = {cfbd.norm(c["school"]) for c in coaching}
    coaching += [c for c in (boost or {}).get("coaching") or [] if cfbd.norm(c["school"]) not in have]
    used = any(g.get("by") == "claude" for g in out.values()) or any(c.get("by") == "claude" for c in coaching)
    doc = {"fetched_at": now.astimezone(dt.timezone.utc).isoformat(timespec="seconds"),
           "as_of": now.astimezone(CT).strftime("%a %b %-d %-I:%M %p CT"),
           "by": "code (news cache + ESPN rankings)" + (" + claude booster " + boost["at"] if used else ""),
           "week": games[0].get("week") if games else prev.get("week"),
           "games": out, "coaching": coaching, "cfp": cfp_doc, "boost": boost}
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CACHE.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, indent=1))
    os.replace(tmp, CACHE)
    n = sum(len(v.get("facts") or []) for v in out.values())
    print(f"slate intel: {len(out)} games, {n} facts, {len(coaching)} coaching changes, "
          f"cfp {'yes' if cfp_doc else 'no'}, {'claude booster' if used else 'code only'}, as of {doc['as_of']}")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "show":
        print(json.dumps(load(), indent=1))
        sys.exit(0)
    try:  # kickoff forecasts for the GAME CARDs ride this hourly tick; weather.py fetches at most every 6 h
        import weather
        print("weather:", weather.refresh())
    except Exception as e:  # noqa: BLE001 - the forecast never blocks the intel run
        logging.warning("weather refresh failed: %r", e)
    sys.exit(run(force="--force" in sys.argv))
