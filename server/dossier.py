"""
Picky's game dossiers: per-game injury / availability / insider intel for the
week's pool games, gathered automatically from free public sources.

Nobody maintains this. LaunchAgent com.pickem.picky-dossier runs
`dossier.py run` every 30 minutes; this file decides whether anything is due:

  - "new": a pool game in the open week has no dossier yet (slate just set,
    or a game was added). Until the commissioner sets the slate, a run is
    two Supabase GETs and exits;
  - "fri": refresh after Friday 5 PM CT;
  - "sat": refresh after Saturday 7 AM CT;
  - never for a game that has kicked off. A failed game backs off 2 hours.

Code first (Charlie, 2026-11-16: "auto feed pipelines for him so you dont need
an opus call every time"). Every run, per game, with no model at all:
  1. Leads: team-tagged items from news.py's RSS cache (headline, outlet,
     byline, date, players, injury status) and r/CFB's public RSS
     (www.reddit.com/r/CFB/.rss; no login; skipped quietly when blocked).
  2. code_research(): one extractive claim per lead (the headline itself, its
     URL and date; injury status from news.py's tag or infer_status()), so
     nothing gets words or a source it doesn't have.
  3. validate(): schema, kinds, dates, URL required, paywalled/paid sites
     dropped (247, On3, Action Network, The Athletic), Reddit can only be a
     rumor, rumors kept out of the facts section.
  4. rule_summary(): the top injury / news claims, verbatim.

Opus enrichment (enrich.py), at most once a week per game: the first run in
the Friday 5 PM - Sunday window (the "fri" refresh, a Friday-night "new", or a
"sat" retry) when the game has no fresh Opus layer, or on demand
(`run --opus`). `claude -p` via picky_claude.run (Opus on Charlie's
subscription, ANTHROPIC_API_KEY stripped, web tools only, zero MCP servers,
neutral cwd, hard timeout) researches the game; its claims are validated
(every URL fetched, 404 / dead host dropped) and kept in d["opus"]. The same
run writes the analyst take (analyst.py, one no-tools call). Later code runs
carry the Opus layer and the take while they're fresh (6 days); a newer code
injury report on the same player replaces Opus's. picky_claude's booster gate
shut (Claude missing, logged out, daily cap hit, PICKY_NO_ANTHROPIC=1) or a
failed run = the code dossier alone (plus the last
good Opus layer while fresh).

Atomic write to cache/dossiers/<week>/<game_id>.json. On any failure the
previous good dossier stays.

The dossier only explains. chat.py never lets it move the computed lean,
and member answers never show source URLs (they stay here for grounding).

CLI:  dossier.py run [--force] [--opus | --no-opus] [--week N] [--game ID] [--out DIR] [--now ISO]
      dossier.py takes [--week N] [--game ID] [--now ISO]   (rebuild only the analyst takes)
      dossier.py status
      dossier.py validate FILE      (re-run the validator on a raw JSON file)
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import httpx

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import analyst  # noqa: E402
import cfbd  # noqa: E402
import enrich  # noqa: E402
import picky_claude  # noqa: E402

log = logging.getLogger("picky.dossier")

CT = ZoneInfo("America/Chicago")
DOSSIER_DIR = Path(os.environ.get("PICKY_DOSSIER_DIR", HERE / "cache" / "dossiers"))
LOG_PATH = HERE / "logs" / "dossier.log"
LOCK = Path("/tmp/picky-dossier.lock")
LOCK_STALE_S = 3 * 3600
WORK_DIR = Path("/tmp/picky-dossier-work")  # outside $HOME: no project CLAUDE.md gets loaded
GAME_TIMEOUT_S = 900
MAX_GAMES_PER_RUN = 8
FAIL_BACKOFF_S = 2 * 3600
KEEP_WEEKS = 3
MAX_CLAIMS = 14
MAX_AGE_DAYS = 30
MAX_LEADS = 12
REDDIT_RUMORS = 3
REDDIT_RSS = ["https://www.reddit.com/r/CFB/.rss", "https://www.reddit.com/r/CFB/new/.rss"]
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")

KINDS = ("fact", "injury", "insider_opinion", "rumor")
STATUSES = ("out", "doubtful", "questionable", "probable", "returning", "unknown")
_OUT_RX = re.compile(r"\b(ruled out|out for|is out|are out|was out|listed as out|as out|will not play|won'?t play|"
                     r"missed|misses|surgery|season-ending|sidelined|unavailable|not expected to play)\b", re.I)
_Q_RX = re.compile(r"\b(questionable|doubtful|game-time decision|day-to-day|uncertain|unclear|no update|"
                   r"left (the|in the|with)|limited)\b", re.I)
_P_RX = re.compile(r"\b(probable|expected to play|cleared|returns|returning|back at practice|will play|"
                   r"available|full participant)\b", re.I)


def infer_status(text: str) -> str:
    """Availability status from an injury claim's own words (when the
    researcher didn't give one). Out beats questionable beats probable."""
    if _OUT_RX.search(text):
        return "out"
    if _Q_RX.search(text):
        return "questionable"
    if _P_RX.search(text):
        return "probable"
    return "unknown"
CONF = ("high", "medium", "low")
SECTION = {"fact": "facts", "injury": "facts", "insider_opinion": "opinions", "rumor": "rumors"}
# paid / login-walled: Charlie's rule is free public sources only
PAID_DOMAINS = ("247sports.com", "on3.com", "actionnetwork.com", "theathletic.com", "rivals.com")
PAID_PATHS = (("nytimes.com", "/athletic/"),)
FORUM_DOMAINS = ("reddit.com", "redd.it", "x.com", "twitter.com", "threads.net", "facebook.com",
                 "instagram.com", "tiktok.com", "messageboard", "forums.")

SCHEMA = {
    "type": "object",
    "properties": {
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "kind": {"type": "string", "enum": list(KINDS)},
                    "team": {"type": "string", "enum": ["away", "home", "both"]},
                    "players": {"type": "array", "items": {"type": "string"}},
                    "source_url": {"type": "string"},
                    "source_name": {"type": "string"},
                    "date": {"type": "string"},
                    "confidence": {"type": "string", "enum": list(CONF)},
                    "status": {"type": "string", "enum": list(STATUSES)},
                },
                "required": ["text", "kind", "team", "players", "source_url", "date", "confidence"],
            },
        },
        "searched": {"type": "array", "items": {"type": "string"}},
        "notes": {"type": "string"},
    },
    "required": ["claims"],
}


def now_ct() -> dt.datetime:
    return dt.datetime.now(CT)


# -----------------------------------------------------------------------------
# slate + what's due
# -----------------------------------------------------------------------------

class Supa:
    """Read-only Supabase with the service key (keychain), same as picky_picks."""

    def __init__(self, client: httpx.Client | None = None):
        self.rest = os.environ.get("SUPABASE_URL", "https://vopdioszofwdkwnujtiq.supabase.co") + "/rest/v1"
        key = os.environ.get("SUPABASE_SERVICE_KEY") or subprocess.run(
            ["security", "find-generic-password", "-a", "drake", "-s", "CFB_PICKEM__SUPABASE_SERVICE_KEY", "-w"],
            capture_output=True, text=True, check=True, timeout=10).stdout.strip()
        self.h = {"apikey": key, "Authorization": f"Bearer {key}"}
        self.c = client or httpx.Client(timeout=20)

    def get(self, q: str) -> list:
        r = self.c.get(f"{self.rest}/{q}", headers=self.h)
        r.raise_for_status()
        return r.json()


def open_slate(db, week: int | None = None) -> tuple[int | None, int | None, list[dict]]:
    """(season, week, pool games) for the earliest week with a non-final pool
    game, or the given week. ([], week None) until a slate exists."""
    season = db.get("all_games?select=cfb_season&order=cfb_season.desc&limit=1")[0]["cfb_season"]
    cols = 'GameId,Away,Home,line,Status,week,tiebreaker,fpi_margin,"Start (CT)"'
    if week is None:
        rows = db.get(f"all_games?select=week&cfb_season=eq.{season}&picked=eq.true"
                      "&or=(Status.is.null,Status.neq.final)&order=week.asc&limit=1")
        if not rows:
            return season, None, []
        week = rows[0]["week"]
    games = db.get(f"all_games?select={cols}&cfb_season=eq.{season}&week=eq.{week}&picked=eq.true"
                   '&order="Start (CT)".asc')
    return season, week, games


def kickoff(g: dict) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(str(g.get("Start (CT)"))).replace(tzinfo=CT)
    except ValueError:
        return None


def _last(now: dt.datetime, weekday: int, hour: int) -> dt.datetime:
    """Most recent `weekday` at `hour`:00 CT at or before now."""
    d = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    d -= dt.timedelta(days=(d.weekday() - weekday) % 7)
    if d > now:
        d -= dt.timedelta(days=7)
    return d


def due_reason(g: dict, existing: dict | None, state: dict | None, now: dt.datetime) -> str | None:
    k = kickoff(g)
    if not k or k <= now or str(g.get("Status") or "").lower() in ("final", "in_progress", "in", "live"):
        return None
    st = state or {}
    if st.get("failed_at") and now.timestamp() - st["failed_at"] < FAIL_BACKOFF_S:
        return None
    if not existing:
        return "new"
    try:
        gen = dt.datetime.fromisoformat(existing["generated_at"])
    except (KeyError, ValueError):
        return "new"
    for name, anchor in (("sat", _last(now, 5, 7)), ("fri", _last(now, 4, 17))):
        if gen < anchor <= now and k > anchor:
            return name
    return None


# -----------------------------------------------------------------------------
# leads (deterministic, no LLM)
# -----------------------------------------------------------------------------

def _mentions(text: str, team: str) -> bool:
    t = cfbd.norm(team)
    names = {t, cfbd.ALIASES.get(t, t)}
    n = f" {cfbd.norm(text)} "
    return any(f" {x} " in n for x in names if len(x) >= 3)


def news_items() -> list[dict]:
    """news.py's cached items ([] when the cache is unreadable)."""
    try:
        import news
        return list((news.load_cache().get("items") or {}).values())
    except Exception as e:  # noqa: BLE001
        log.warning("news cache unreadable: %s", e)
        return []


def news_leads(away: str, home: str, limit: int = MAX_LEADS, items: list[dict] | None = None) -> list[dict]:
    items = news_items() if items is None else items
    keys = {cfbd.norm(away), cfbd.norm(home)}
    hits = [it for it in items if keys & {cfbd.norm(t) for t in it.get("teams") or []}
            or _mentions(it.get("title", ""), away) or _mentions(it.get("title", ""), home)]
    hits.sort(key=lambda it: it.get("date") or "", reverse=True)
    return [{"title": it["title"], "url": it["link"], "date": (it.get("date") or "")[:10],
             "source": it.get("source"), "summary": it.get("summary") or "", "author": it.get("author"),
             "teams": it.get("teams") or [], "players": it.get("players") or [], "kind": it.get("kind"),
             "topics": it.get("topics") or [], "status": it.get("status"), "story": it.get("story")}
            for it in hits[:limit]]


def parse_reddit_rss(xml_text: str) -> list[dict]:
    ns = {"a": "http://www.w3.org/2005/Atom"}
    out = []
    for e in ET.fromstring(xml_text).findall("a:entry", ns):
        title = (e.findtext("a:title", default="", namespaces=ns) or "").strip()
        link = e.find("a:link", ns)
        out.append({"title": title, "url": link.get("href") if link is not None else "",
                    "date": (e.findtext("a:updated", default="", namespaces=ns) or "")[:10]})
    return out


_reddit_cache: tuple[str, list[dict]] | None = None


def reddit_leads(client: httpx.Client, away: str, home: str) -> tuple[str, list[dict]]:
    """('ok'|'blocked: ...', entries about either team). One fetch per run."""
    global _reddit_cache
    if _reddit_cache is None:
        entries, status = [], "ok"
        for url in REDDIT_RSS:
            try:
                r = client.get(url, headers={"User-Agent": UA}, timeout=15, follow_redirects=False)
                if r.status_code != 200:
                    status = f"blocked: HTTP {r.status_code} on {url}"
                    continue
                entries += parse_reddit_rss(r.text)
            except Exception as e:  # noqa: BLE001
                status = f"blocked: {type(e).__name__} on {url}"
            time.sleep(2)
        if entries:
            status = "ok"
        seen, uniq = set(), []
        for x in entries:
            if x["url"] not in seen:
                seen.add(x["url"])
                uniq.append(x)
        _reddit_cache = (status, uniq)
    status, entries = _reddit_cache
    return status, [x for x in entries if _mentions(x["title"], away) or _mentions(x["title"], home)][:6]


# -----------------------------------------------------------------------------
# claude -p research
# -----------------------------------------------------------------------------

PROMPT = """You are a research assistant building a short, sourced intel file for one college football game.
Use only free public web pages that need no login or subscription. Never use 247Sports, On3, Rivals,
Action Network or The Athletic (paid). Do not create accounts or log in anywhere.

GAME: {away} (away) at {home} (home), kickoff {kick}, {season} season, week {week}.
Today is {today} (America/Chicago).

Find what a sharp fan would want to know before this game, for BOTH teams:
- injuries and availability: official team or conference availability reports, coach press-conference
  quotes, depth chart changes, starters out / questionable / returning, transfers or suspensions;
- quarterback status and any lineup or coordinator changes;
- beat writers' reporting (local newspapers, team-site articles, ESPN/CBS/Yahoo/USA Today/AP pages);
- insider opinions from named beat writers or analysts (label them insider_opinion);
- message-board or Reddit chatter only as rumor (never as fact).

Leads already found (verify before using; they may be off-topic or stale):
{leads}

Rules for each claim:
- One specific, checkable statement, under 240 characters, in plain words. No betting advice.
- kind: fact (verified news), injury (availability/injury status), insider_opinion (a named person's view),
  rumor (unconfirmed, forum/social chatter).
- team: away, home, or both. players: every player named in the text, full names exactly as the source spells them.
- source_url: the exact page you read that states it (not a search page, not a homepage). If you can't
  give the page, leave the claim out.
- date: the page's publication date, YYYY-MM-DD. Skip anything older than {max_age} days.
- confidence: high (official/report or multiple outlets), medium (one reputable outlet), low (thin or rumor).
- status (injury claims only): out, doubtful, questionable, probable, returning, or unknown, for THIS game.
- Never guess a stat, a name or a status. Fewer true claims beat many shaky ones. Max {max_claims} claims.
If you find nothing reliable, return an empty claims list and say why in notes.
Return only the JSON object."""


def build_prompt(g: dict, season: int, week: int, leads: list[dict], now: dt.datetime) -> str:
    k = kickoff(g)
    lead_txt = "\n".join(f"- {x['date']} {x.get('source') or 'reddit'}: {x['title']} ({x['url']})"
                         for x in leads) or "- none"
    return PROMPT.format(away=g["Away"], home=g["Home"], kick=k.strftime("%a %b %-d, %-I:%M %p CT") if k else "TBD",
                         season=season, week=week, today=now.strftime("%A %Y-%m-%d"), leads=lead_txt,
                         max_age=MAX_AGE_DAYS, max_claims=MAX_CLAIMS)


def run_claude(prompt: str, timeout: int = GAME_TIMEOUT_S, schema: dict | None = None,
               tools: str = "WebSearch,WebFetch", caller: str | None = None) -> dict:
    """structured_output from one `claude -p` run via picky_claude (Opus, zero MCP,
    no API key; web research by default, tools="" for the no-tools writers/judges);
    raises on any failure, incl. picky_claude.AnthropicDisabled."""
    picky_claude.guard()
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    p = picky_claude.run(prompt, timeout=timeout, caller=caller, cwd=WORK_DIR, tools=tools,
                         schema=schema or SCHEMA)
    if p.returncode != 0:
        raise RuntimeError(f"claude exit {p.returncode}: {(p.stderr or p.stdout)[-400:]}")
    out = json.loads(p.stdout)
    if out.get("is_error") or not isinstance(out.get("structured_output"), dict):
        raise RuntimeError(f"claude result not usable: {str(out.get('result'))[:300]}")
    return out["structured_output"]


# -----------------------------------------------------------------------------
# validator
# -----------------------------------------------------------------------------

def _domain(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def _paid(url: str) -> bool:
    d = _domain(url)
    path = urlparse(url).path.lower()
    return any(d == x or d.endswith("." + x) for x in PAID_DOMAINS) or any(
        (d == x or d.endswith("." + x)) and path.startswith(p) for x, p in PAID_PATHS)


def _forum(url: str) -> bool:
    d = _domain(url)
    return any(d == x or d.endswith("." + x) or (x.endswith(".") and d.startswith(x)) or
               (not x.endswith(".") and "." not in x and x in d) for x in FORUM_DOMAINS)


def _url_ok(u: str) -> bool:
    if not re.match(r"^https?://[^\s/$.?#][^\s]*$", u or "") or len(u) > 600:
        return False
    p = urlparse(u)
    return bool(p.hostname and "." in p.hostname and (p.path not in ("", "/") or p.query))


def check_urls(client: httpx.Client, urls: list[str]) -> dict[str, str]:
    """url -> 'ok' | 'dead' | 'blocked' | 'unknown'. Only 'dead' (404/410, no
    such host) drops a claim: plenty of real news sites 403 a bot."""
    out = {}
    for u in urls:
        try:
            r = client.get(u, headers={"User-Agent": UA}, timeout=10, follow_redirects=True)
            out[u] = ("ok" if r.status_code < 400 else "dead" if r.status_code in (404, 410)
                      else "blocked" if r.status_code in (401, 403, 429, 451) else "unknown")
        except httpx.ConnectError as e:
            out[u] = "dead" if "nodename" in str(e).lower() or "name or service" in str(e).lower() else "unknown"
        except Exception:  # noqa: BLE001 - timeouts etc: can't tell, keep
            out[u] = "unknown"
    return out


def validate(raw: dict, now: dt.datetime, url_status: dict[str, str] | None = None) -> tuple[dict, list[str]]:
    """({'facts', 'opinions', 'rumors'}, reasons for every dropped claim)."""
    secs = {"facts": [], "opinions": [], "rumors": []}
    dropped, seen = [], set()
    claims = raw.get("claims") if isinstance(raw, dict) else None
    if not isinstance(claims, list):
        raise ValueError("no claims list")
    today = now.date()
    for c in claims:
        if not isinstance(c, dict):
            dropped.append("not an object")
            continue
        text = re.sub(r"\s+", " ", str(c.get("text") or "")).strip()
        url = str(c.get("source_url") or "").strip()
        kind, team, conf = c.get("kind"), c.get("team"), c.get("confidence")
        why = None
        if not 10 <= len(text) <= 300:
            why = "text length"
        elif kind not in KINDS or team not in ("away", "home", "both") or conf not in CONF:
            why = "bad kind/team/confidence"
        elif not _url_ok(url):
            why = "no usable source URL"
        elif _paid(url):
            why = "paid source"
        elif re.search(r"https?://|www\.", text):
            why = "URL in text"
        if not why:
            try:
                d = dt.date.fromisoformat(str(c.get("date"))[:10])
                if d > today + dt.timedelta(days=1):
                    why = "future date"
                elif (today - d).days > MAX_AGE_DAYS:
                    why = "stale"
            except ValueError:
                why = "bad date"
        if not why and url_status and url_status.get(url) == "dead":
            why = "dead URL"
        key = re.sub(r"\W+", " ", text.lower()).strip()
        if not why and key in seen:
            why = "duplicate"
        if why:
            dropped.append(f"{why}: {text[:80]}")
            continue
        seen.add(key)
        if _forum(url) and kind in ("fact", "injury"):
            kind = "rumor"  # a forum post is never a fact
            conf = "low"
        players = [re.sub(r"\s+", " ", str(p)).strip() for p in c.get("players") or [] if str(p).strip()]
        extra = {}
        if kind == "injury":
            extra["status"] = c.get("status") if c.get("status") in STATUSES and c.get("status") != "unknown" \
                else infer_status(text)
        secs[SECTION[kind]].append({**extra,
            "text": text, "kind": kind, "team": team, "players": players[:6], "source_url": url,
            "source_name": str(c.get("source_name") or _domain(url))[:60], "date": str(c.get("date"))[:10],
            "confidence": conf, "url_check": (url_status or {}).get(url, "unchecked")})
    n = 0
    for k in ("facts", "opinions", "rumors"):  # cap total, facts first
        secs[k] = secs[k][: max(0, MAX_CLAIMS - n)]
        n += len(secs[k])
    return secs, dropped


# -----------------------------------------------------------------------------
# code research, summary, merge with the weekly Opus layer
# -----------------------------------------------------------------------------

def rule_summary(secs: dict) -> str:
    facts = secs["facts"]
    if not facts:
        return "No verified injury or availability news found for this game."
    inj = [c for c in facts if c["kind"] == "injury"]
    pick = (inj + [c for c in facts if c["kind"] != "injury"])[:2]
    return " ".join(c["text"] if c["text"].endswith(".") else c["text"] + "." for c in pick)


def _side(x: dict, away: str, home: str) -> str:
    tags = {cfbd.norm(t) for t in x.get("teams") or []}
    a = cfbd.norm(away) in tags or _mentions(x.get("title", ""), away)
    h = cfbd.norm(home) in tags or _mentions(x.get("title", ""), home)
    return "both" if a == h else "away" if a else "home"


def code_research(g: dict, leads: list[dict], rleads: list[dict]) -> dict:
    """The research step without a model: one claim per cached story whose headline names
    either team, worded by that headline, with the lead's URL, outlet and date. Same shape as the Opus researcher's
    output, so validate() treats both alike."""
    away, home = g["Away"], g["Home"]
    claims, stories = [], set()
    for x in leads:
        title = re.sub(r"\s+", " ", str(x.get("title") or "")).strip()
        bare = re.sub(r"['’]s\b", "", title)  # "Utah's defense" names Utah
        if not (_mentions(bare, away) or _mentions(bare, home)):
            continue  # tagged by the feed but not about this game in its own headline
        if x.get("story") and x["story"] in stories:
            continue  # the same story from another outlet
        stories.add(x.get("story"))
        injury = x.get("kind") == "injury" or "injury" in (x.get("topics") or [])
        c = {"text": title, "kind": "injury" if injury else "fact", "team": _side(x, away, home),
             "players": list(x.get("players") or []), "source_url": x.get("url") or "",
             "source_name": x.get("source") or "", "date": x.get("date") or "", "confidence": "medium"}
        if injury and x.get("status") in STATUSES:
            c["status"] = x["status"]
        claims.append(c)
    for x in rleads[:REDDIT_RUMORS]:
        claims.append({"text": re.sub(r"\s+", " ", x.get("title") or "").strip(), "kind": "rumor",
                       "team": _side(x, away, home), "players": [], "source_url": x.get("url") or "",
                       "source_name": "r/CFB", "date": x.get("date") or "", "confidence": "low"})
    return {"claims": claims, "searched": [],
            "notes": f"code extraction over {len(leads)} news-cache and {len(rleads[:REDDIT_RUMORS])} r/CFB items"}


def merge(code: dict, opus: dict | None, now: dt.datetime) -> dict:
    """Code claims + the Opus layer's, newest first per section. An Opus claim goes when it has
    aged past MAX_AGE_DAYS, repeats a code claim, or is an injury report on a player a code
    report of the same date or newer covers (the newer report wins)."""
    out = {}
    seen = set()
    code_inj = {}
    for c in code["facts"]:
        if c["kind"] == "injury":
            for pl in c.get("players") or []:
                code_inj[pl.lower()] = max(code_inj.get(pl.lower(), ""), c.get("date") or "")
    for k in ("facts", "opinions", "rumors"):
        rows = [dict(c, by="code") for c in code[k]]
        for c in (opus or {}).get(k) or []:
            try:
                old = (now.date() - dt.date.fromisoformat(str(c.get("date"))[:10])).days > MAX_AGE_DAYS
            except ValueError:
                old = True
            newer = c.get("kind") == "injury" and any(
                code_inj.get(pl.lower(), "") >= str(c.get("date") or "") for pl in c.get("players") or []
                if pl.lower() in code_inj)
            if not old and not newer:
                rows.append(dict(c, by="opus"))
        uniq = []
        for c in sorted(rows, key=lambda c: c.get("date") or "", reverse=True):
            key = re.sub(r"\W+", " ", c["text"].lower()).strip()
            if key not in seen:
                seen.add(key)
                uniq.append(c)
        out[k] = uniq
    n = 0
    for k in ("facts", "opinions", "rumors"):  # cap total, facts first
        out[k] = out[k][: max(0, MAX_CLAIMS - n)]
        n += len(out[k])
    return out


def opus_due(prev: dict | None, trigger: str, now: dt.datetime) -> bool:
    """The weekly Opus layer: the first run between Friday 5 PM and Sunday 5 PM CT (the "fri"
    refresh, a Friday-night "new", or a "sat" retry after a failed Friday) when the game has no
    fresh Opus layer yet. Never with the kill switch on."""
    if not enrich.available() or trigger not in ("new", "fri", "sat"):
        return False
    if enrich.fresh(((prev or {}).get("opus") or {}).get("generated_at"), now):
        return False
    return now - _last(now, 4, 17) < dt.timedelta(days=2)


# -----------------------------------------------------------------------------
# files
# -----------------------------------------------------------------------------

def path_for(week: int, gid, base: Path | None = None) -> Path:
    return (base or DOSSIER_DIR) / str(week) / f"{gid}.json"


def read_json(p: Path) -> dict | None:
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return None


def write_atomic(p: Path, data: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=".tmp-", suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, p)


def load_state(week_dir: Path) -> dict:
    return read_json(week_dir / "_state.json") or {}


def save_state(week_dir: Path, st: dict) -> None:
    write_atomic(week_dir / "_state.json", st)


def prune_weeks(base: Path, keep: int = KEEP_WEEKS) -> None:
    """Keep the `keep` most recently written week dirs. By write time, not week number: next August's week 1
    is newer than last season's weeks 14-16 (sorting by number deleted it the moment it was written)."""
    weeks = sorted((d for d in base.iterdir() if d.is_dir() and d.name.isdigit()),
                   key=lambda d: (d.stat().st_mtime, int(d.name))) if base.exists() else []
    for d in weeks[:-keep]:
        shutil.rmtree(d, ignore_errors=True)


def build_one(client: httpx.Client, g: dict, season: int, week: int, trigger: str, now: dt.datetime,
              prev: dict | None, opus: bool = False, research=run_claude, take_fn=None,
              items: list[dict] | None = None) -> dict:
    """A new dossier for one game, or raises (caller keeps the previous one). The code path
    always runs; opus=True adds this week's Opus layer (its failure never fails the dossier)."""
    away, home = g["Away"], g["Home"]
    items = news_items() if items is None else items
    if not items and prev:
        raise RuntimeError("news cache empty or unreadable; keeping the previous dossier")
    leads = news_leads(away, home, items=items)
    rstatus, rleads = reddit_leads(client, away, home)
    code_secs, dropped = validate(code_research(g, leads, rleads), now)
    layer = None
    if opus:
        try:
            raw = research(build_prompt(g, season, week, leads + rleads, now))
            urls = sorted({str(c.get("source_url") or "") for c in raw.get("claims") or [] if isinstance(c, dict)})
            status = check_urls(client, [u for u in urls if _url_ok(u)])
            secs, odrop = validate(raw, now, status)
            n = sum(len(v) for v in secs.values())
            prev_n = sum(len(((prev or {}).get("opus") or {}).get(k) or []) for k in ("facts", "opinions", "rumors"))
            if n == 0 and prev_n:
                raise RuntimeError(f"empty Opus result ({len(odrop)} dropped) but the previous layer had {prev_n}")
            layer = {"generated_at": now.isoformat(timespec="seconds"), "trigger": trigger,
                     "researcher": f"claude -p {picky_claude.model_for('dossier')} (web search, free public sources)", **secs,
                     "dropped": odrop, "searched": [str(x)[:120] for x in (raw.get("searched") or [])][:12],
                     "notes": str(raw.get("notes") or "")[:400]}
        except Exception as e:  # noqa: BLE001 - Anthropic down / timeout / bad run: code dossier stands
            log.warning("game %s Opus layer failed (%s); code dossier only", g["GameId"], str(e)[:200])
    if layer is None and enrich.fresh(((prev or {}).get("opus") or {}).get("generated_at"), now):
        layer = prev["opus"]
    secs = merge(code_secs, layer, now)
    d = {
        "schema": 2, "season": season, "week": week, "game_id": g["GameId"], "away": away, "home": home,
        "kickoff": g.get("Start (CT)"), "generated_at": now.isoformat(timespec="seconds"), "trigger": trigger,
        "researcher": "code (news cache + r/CFB)" + (" + Opus layer " + layer["generated_at"] if layer else ""),
        "summary": rule_summary(secs), "summary_by": "rule", **secs,
        "dropped": dropped, "notes": (layer or {}).get("notes", ""),
        "leads": {"news": len(leads), "reddit": len(rleads), "reddit_status": rstatus},
    }
    if layer:
        d["opus"] = layer
    # take_fn None = the real no-tools take, but only with the real researcher (tests pass fakes)
    take_fn = take_fn or (run_take_claude if research is run_claude else None)
    if opus and layer is not None and layer is not (prev or {}).get("opus") and take_fn:
        add_take(d, g, season, week, now, prev, take_fn)
    elif (prev or {}).get("analyst_take") and enrich.fresh(prev["analyst_take"].get("generated_at"), now):
        d["analyst_take"] = prev["analyst_take"]  # this week's take, still fresh
    return d


def run_take_claude(prompt: str) -> dict:
    return run_claude(prompt, analyst.TAKE_TIMEOUT_S, analyst.TAKE_SCHEMA, "", caller="dossier-take")


def add_take(d: dict, g: dict, season: int, week: int, now: dt.datetime, prev: dict | None, take_fn) -> None:
    """d["analyst_take"]: a fresh take, else the previous one (marked stale),
    else nothing. Never raises: the dossier itself is still good."""
    try:
        d["analyst_take"] = analyst.build_take(g, d, season, week, now, take_fn)
        t = d["analyst_take"]
        log.info("game %s take: %s (%s), lean %s, agrees %s, %d reasons, %d dropped", g["GameId"], t["pick"],
                 t["confidence"], t["lean"], t["agrees_with_lean"], len(t["reasons"]), len(t["dropped"]))
    except Exception as e:  # noqa: BLE001
        log.warning("game %s analyst take failed: %s", g["GameId"], str(e)[:300])
        if (prev or {}).get("analyst_take"):
            d["analyst_take"] = dict(prev["analyst_take"], stale=True)


# -----------------------------------------------------------------------------
# run
# -----------------------------------------------------------------------------

def _take_lock() -> bool:
    try:
        LOCK.mkdir()
        return True
    except FileExistsError:
        if time.time() - LOCK.stat().st_mtime > LOCK_STALE_S:
            log.warning("removing stale lock %s", LOCK)
            shutil.rmtree(LOCK, ignore_errors=True)
            return _take_lock()
        return False


def run(force: bool = False, week: int | None = None, only_game: int | None = None, base: Path | None = None,
        opus: bool | None = None, db=None, research=run_claude, now: dt.datetime | None = None,
        take_fn=None) -> dict:
    """opus None = on the weekly schedule (opus_due), True = on demand for every built game
    (still never with the kill switch on), False = code only."""
    base = base or DOSSIER_DIR
    now = now or now_ct()
    db = db or Supa()
    season, wk, games = open_slate(db, week)
    if not wk or not games:
        log.info("no slate yet for season %s (no open pool week); waiting", season)
        return {"ok": True, "slate": False}
    week_dir = base / str(wk)
    state = load_state(week_dir)
    todo = []
    for g in games:
        if only_game and int(g["GameId"]) != int(only_game):
            continue
        prev = read_json(path_for(wk, g["GameId"], base))
        reason = "forced" if force and (kickoff(g) or now) > now else due_reason(g, prev, state.get(str(g["GameId"])), now)
        if reason:
            todo.append((g, reason, prev))
    if not todo:
        log.info("week %s: %d pool games, nothing due", wk, len(games))
        return {"ok": True, "slate": True, "week": wk, "built": 0}
    built, failed = [], []
    items = news_items()
    with httpx.Client(timeout=20) as c:
        for g, reason, prev in todo[:MAX_GAMES_PER_RUN]:
            gid = str(g["GameId"])
            t0 = time.time()
            use_opus = (enrich.available() if opus else False) if opus is not None else opus_due(prev, reason, now)
            try:
                d = build_one(c, g, season, wk, reason, now, prev, opus=use_opus, research=research,
                              take_fn=take_fn, items=items)
                write_atomic(path_for(wk, gid, base), d)
                state[gid] = {"ok_at": time.time(), "trigger": reason}
                built.append(gid)
                log.info("week %s game %s (%s at %s) %s: %d facts, %d opinions, %d rumors, %d dropped, "
                         "%s, reddit %s, %.0fs", wk, gid, g["Away"], g["Home"], reason,
                         len(d["facts"]), len(d["opinions"]), len(d["rumors"]), len(d["dropped"]),
                         d["researcher"], d["leads"]["reddit_status"], time.time() - t0)
            except Exception as e:  # noqa: BLE001 - keep the last good dossier, back off
                state[gid] = dict(state.get(gid) or {}, failed_at=time.time(), error=str(e)[:300])
                failed.append(gid)
                log.warning("week %s game %s (%s at %s) %s FAILED, kept previous: %s", wk, gid, g["Away"],
                            g["Home"], reason, str(e)[:300])
            save_state(week_dir, state)
    prune_weeks(base)
    return {"ok": not failed, "slate": True, "week": wk, "built": built, "failed": failed}


def takes(week: int | None = None, only_game: int | None = None, base: Path | None = None, db=None,
          now: dt.datetime | None = None, take_fn=None) -> dict:
    """Rebuild only the analyst takes on the existing dossiers of a week (no
    web research). For a failed take or a prompt change."""
    base = base or DOSSIER_DIR
    now = now or now_ct()
    db = db or Supa()
    season, wk, games = open_slate(db, week)
    done = []
    for g in games:
        if only_game and int(g["GameId"]) != int(only_game):
            continue
        p = path_for(wk, g["GameId"], base)
        d = read_json(p)
        if not d:
            continue
        add_take(d, g, season, wk, now, dict(d), take_fn or run_take_claude)
        if d.get("analyst_take") and not d["analyst_take"].get("stale"):
            write_atomic(p, d)
            done.append(str(g["GameId"]))
    return {"ok": True, "week": wk, "takes": done}


def status(base: Path | None = None) -> dict:
    base = base or DOSSIER_DIR
    out = {}
    for d in sorted(base.glob("*/*.json")) if base.exists() else []:
        if d.name.startswith("_"):
            continue
        j = read_json(d) or {}
        out[f"{d.parent.name}/{d.stem}"] = {
            "game": f"{j.get('away')} at {j.get('home')}", "generated_at": j.get("generated_at"),
            "trigger": j.get("trigger"), "facts": len(j.get("facts") or []),
            "opinions": len(j.get("opinions") or []), "rumors": len(j.get("rumors") or []),
            "researcher": j.get("researcher"),
            "take": (f"{j['analyst_take']['pick']} ({j['analyst_take']['confidence']})"
                     + (" STALE" if j["analyst_take"].get("stale") else "")) if j.get("analyst_take") else None}
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["run", "status", "validate", "takes"])
    ap.add_argument("file", nargs="?")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--week", type=int)
    ap.add_argument("--game", type=int)
    ap.add_argument("--out", type=Path)
    ap.add_argument("--opus", action="store_true", help="on demand: add the Opus layer to every built game")
    ap.add_argument("--no-opus", action="store_true", help="code only, even on the weekly schedule")
    ap.add_argument("--now", help="pretend it's this CT time (testing on a past slate), e.g. 2026-11-13T17:30")
    a = ap.parse_args(argv)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.FileHandler(LOG_PATH)] + ([logging.StreamHandler()] if sys.stderr.isatty() else []))
    for noisy in ("httpx", "httpcore", "picky.chat", "picky"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    log.setLevel(logging.INFO)
    if a.cmd == "status":
        print(json.dumps(status(a.out), indent=1))
        return 0
    if a.cmd == "validate":
        secs, dropped = validate(json.loads(Path(a.file).read_text()), now_ct())
        print(json.dumps({**secs, "dropped": dropped}, indent=1))
        return 0
    if not _take_lock():
        log.info("another dossier run is in progress; skipping")
        return 0
    now = dt.datetime.fromisoformat(a.now).replace(tzinfo=CT) if a.now else None
    try:
        res = (takes(week=a.week, only_game=a.game, base=a.out, now=now) if a.cmd == "takes" else
               run(force=a.force, week=a.week, only_game=a.game, base=a.out, now=now,
                   opus=True if a.opus else False if a.no_opus else None))
    except Exception as e:  # noqa: BLE001 - never crash-loop launchd; log and retry next tick
        log.exception("dossier run failed: %s", e)
        return 1
    finally:
        shutil.rmtree(LOCK, ignore_errors=True)
    print(json.dumps(res))
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
