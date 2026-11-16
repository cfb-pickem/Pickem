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

Per game:
  1. Leads, deterministic (no LLM): team-tagged items from news.py's RSS
     cache and r/CFB's public RSS (www.reddit.com/r/CFB/.rss; no login, no
     app; skipped quietly when Reddit blocks it).
  2. `claude -p` (sonnet on Charlie's subscription; ANTHROPIC_API_KEY is
     stripped so it can never bill an API key), web tools only
     (--tools WebSearch,WebFetch), zero MCP servers, neutral cwd outside the
     home dir, hard timeout. Output is schema-checked JSON.
  3. validate(): schema, kinds, dates, URL required, paywalled/paid sites
     dropped (247, On3, Action Network, The Athletic), Reddit can only be a
     rumor or an opinion, every URL fetched (404 / dead host = dropped), and
     rumors kept out of the facts section.
  4. A two-sentence summary by Picky's own local model (Ollama, only while
     chat is idle), accepted only if every name and number in it appears in
     the claims; otherwise a rule-built summary.
  5. An analyst take (analyst.py): one more `claude -p` call, no tools, over
     a numbered brief of the computed signals + the claims above: pick vs the
     pool line, confidence, 3-5 cited reasons, the best counter, a quotable
     line, what the market is missing. Validated; on failure the previous
     take is carried over. Picky's computed lean stays authoritative.
  6. Atomic write to cache/dossiers/<week>/<game_id>.json. On any failure
     the previous good dossier stays.

The dossier only explains. chat.py never lets it move the computed lean,
and member answers never show source URLs (they stay here for grounding).

CLI:  dossier.py run [--force] [--week N] [--game ID] [--out DIR] [--no-local] [--now ISO]
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

log = logging.getLogger("picky.dossier")

CT = ZoneInfo("America/Chicago")
DOSSIER_DIR = Path(os.environ.get("PICKY_DOSSIER_DIR", HERE / "cache" / "dossiers"))
LOG_PATH = HERE / "logs" / "dossier.log"
LOCK = Path("/tmp/picky-dossier.lock")
LOCK_STALE_S = 3 * 3600
CLAUDE = os.environ.get("PICKY_CLAUDE_BIN", "/opt/homebrew/bin/claude")
EMPTY_MCP = HERE / "empty.mcp.json"
WORK_DIR = Path("/tmp/picky-dossier-work")  # outside $HOME: no project CLAUDE.md gets loaded
GAME_TIMEOUT_S = 900
MAX_GAMES_PER_RUN = 8
FAIL_BACKOFF_S = 2 * 3600
KEEP_WEEKS = 3
MAX_CLAIMS = 14
MAX_AGE_DAYS = 30
OLLAMA = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
CHAT_HEALTH = os.environ.get("PICKY_CHAT_HEALTH", "http://127.0.0.1:8766/health")
LOCAL_MODEL = os.environ.get("PICKEM_CHAT_MODEL", "gemma4:12b-it-qat")
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


def news_leads(away: str, home: str, limit: int = 8) -> list[dict]:
    try:
        import news
        items = list((news.load_cache().get("items") or {}).values())
    except Exception as e:  # noqa: BLE001
        log.warning("news cache unreadable: %s", e)
        return []
    keys = {cfbd.norm(away), cfbd.norm(home)}
    hits = [it for it in items if keys & {cfbd.norm(t) for t in it.get("teams") or []}
            or _mentions(it.get("title", ""), away) or _mentions(it.get("title", ""), home)]
    hits.sort(key=lambda it: it.get("date") or "", reverse=True)
    return [{"title": it["title"], "url": it["link"], "date": (it.get("date") or "")[:10],
             "source": it.get("source")} for it in hits[:limit]]


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
               tools: str = "WebSearch,WebFetch") -> dict:
    """structured_output from one `claude -p` run (web research by default;
    tools="" for the no-tools analyst take); raises on any failure."""
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
    env.update({"DISABLE_AUTOUPDATER": "1", "TERM": "dumb",
                "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"})
    cmd = [CLAUDE, "-p", "--model", "sonnet", "--tools", tools,
           "--dangerously-skip-permissions", "--strict-mcp-config", "--mcp-config", str(EMPTY_MCP),
           "--output-format", "json", "--json-schema", json.dumps(schema or SCHEMA)]
    p = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=timeout,
                       cwd=WORK_DIR, env=env)
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
# summary: Picky's local model, checked; rule-built fallback
# -----------------------------------------------------------------------------

_CAP = re.compile(r"\b[A-Z][a-zA-Z'’.-]+(?:\s+[A-Z][a-zA-Z'’.-]+)*")
_NUM = re.compile(r"\b\d+(?:\.\d+)?\b")
_COMMON = {"The", "A", "An", "He", "She", "They", "It", "His", "Their", "Both", "Week", "Saturday", "Friday",
           "Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Coach", "Head", "Per", "According"}


def summary_grounded(summary: str, source_text: str) -> bool:
    """Every capitalized name and every number in the summary appears in the claims."""
    src = source_text.lower()
    for m in _CAP.finditer(summary):
        for w in m.group(0).split():
            w = re.sub(r"['’]s$", "", w.strip(".,;:'’\"")).strip("'’")
            if w and w not in _COMMON and w.lower() not in src:
                return False
    return all(n in source_text for n in _NUM.findall(summary))


def rule_summary(secs: dict) -> str:
    facts = secs["facts"]
    if not facts:
        return "No verified injury or availability news found for this game."
    inj = [c for c in facts if c["kind"] == "injury"]
    pick = (inj + [c for c in facts if c["kind"] != "injury"])[:2]
    return " ".join(c["text"] if c["text"].endswith(".") else c["text"] + "." for c in pick)


def _chat_idle(client: httpx.Client) -> bool:
    try:
        g = client.get(CHAT_HEALTH, timeout=3).json().get("gate") or {}
        return g.get("members_active", 1) == 0 and not g.get("test_busy")
    except Exception:  # noqa: BLE001
        return False


def local_summary(client: httpx.Client, g: dict, secs: dict) -> tuple[str, str]:
    """(summary, by). Picky's own model writes it when chat is idle; it's kept
    only if grounded in the claims, else the rule-built one."""
    facts = secs["facts"] + secs["opinions"]
    if not facts:
        return rule_summary(secs), "rule"
    claims_txt = "\n".join(f"- [{c['kind']}] {c['text']}" for c in facts)
    if not _chat_idle(client):
        return rule_summary(secs), "rule"
    prompt = (f"Game: {g['Away']} at {g['Home']}.\nVerified notes:\n{claims_txt}\n\n"
              "Write at most two short plain sentences summarizing the most important availability news "
              "above for someone about to pick this game. Use only names and numbers from the notes; "
              "label opinions as opinions. No predictions.\nSummary:")
    try:
        r = client.post(f"{OLLAMA}/api/generate", timeout=90, json={
            "model": LOCAL_MODEL, "prompt": prompt, "stream": False, "think": False,
            "options": {"num_ctx": 16384, "temperature": 0.1, "num_predict": 120}})
        r.raise_for_status()
        s = re.sub(r"\s+", " ", r.json().get("response") or "").strip()
    except Exception as e:  # noqa: BLE001
        log.warning("local summary failed: %s", e)
        return rule_summary(secs), "rule"
    if 20 <= len(s) <= 400 and summary_grounded(s, claims_txt + f" {g['Away']} {g['Home']}"):
        return s, LOCAL_MODEL
    log.info("local summary rejected (ungrounded): %s", s[:400])
    return rule_summary(secs), "rule"


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
    weeks = sorted((d for d in base.iterdir() if d.is_dir() and d.name.isdigit()), key=lambda d: int(d.name)) \
        if base.exists() else []
    for d in weeks[:-keep]:
        shutil.rmtree(d, ignore_errors=True)


def build_one(client: httpx.Client, g: dict, season: int, week: int, trigger: str, now: dt.datetime,
              prev: dict | None, use_local: bool = True, research=run_claude, take_fn=None) -> dict:
    """A new dossier for one game, or raises (caller keeps the previous one)."""
    away, home = g["Away"], g["Home"]
    leads = news_leads(away, home)
    rstatus, rleads = reddit_leads(client, away, home)
    raw = research(build_prompt(g, season, week, leads + rleads, now))
    urls = sorted({str(c.get("source_url") or "") for c in raw.get("claims") or [] if isinstance(c, dict)})
    status = check_urls(client, [u for u in urls if _url_ok(u)])
    secs, dropped = validate(raw, now, status)
    n = sum(len(v) for v in secs.values())
    prev_n = sum(len((prev or {}).get(k) or []) for k in ("facts", "opinions", "rumors"))
    if n == 0 and prev_n:
        raise RuntimeError(f"empty result ({len(dropped)} dropped) but the previous dossier had {prev_n} claims")
    summary, by = local_summary(client, g, secs) if use_local else (rule_summary(secs), "rule")
    d = {
        "schema": 1, "season": season, "week": week, "game_id": g["GameId"], "away": away, "home": home,
        "kickoff": g.get("Start (CT)"), "generated_at": now.isoformat(timespec="seconds"), "trigger": trigger,
        "researcher": "claude -p sonnet (web search, free public sources)",
        "summary": summary, "summary_by": by, **secs,
        "dropped": dropped, "searched": [str(x)[:120] for x in (raw.get("searched") or [])][:12],
        "notes": str(raw.get("notes") or "")[:400],
        "leads": {"news": len(leads), "reddit": len(rleads), "reddit_status": rstatus},
    }
    # take_fn None = the real no-tools take, but only with the real researcher (tests pass fakes)
    take_fn = take_fn or (run_take_claude if research is run_claude else None)
    if take_fn:
        add_take(d, g, season, week, now, prev, take_fn)
    return d


def run_take_claude(prompt: str) -> dict:
    return run_claude(prompt, analyst.TAKE_TIMEOUT_S, analyst.TAKE_SCHEMA, "")


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
        use_local: bool = True, db=None, research=run_claude, now: dt.datetime | None = None,
        take_fn=None) -> dict:
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
    with httpx.Client(timeout=20) as c:
        for g, reason, prev in todo[:MAX_GAMES_PER_RUN]:
            gid = str(g["GameId"])
            t0 = time.time()
            try:
                d = build_one(c, g, season, wk, reason, now, prev, use_local=use_local, research=research,
                              take_fn=take_fn)
                write_atomic(path_for(wk, gid, base), d)
                state[gid] = {"ok_at": time.time(), "trigger": reason}
                built.append(gid)
                log.info("week %s game %s (%s at %s) %s: %d facts, %d opinions, %d rumors, %d dropped, "
                         "summary by %s, reddit %s, %.0fs", wk, gid, g["Away"], g["Home"], reason,
                         len(d["facts"]), len(d["opinions"]), len(d["rumors"]), len(d["dropped"]),
                         d["summary_by"], d["leads"]["reddit_status"], time.time() - t0)
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
            "summary_by": j.get("summary_by"),
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
    ap.add_argument("--no-local", action="store_true")
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
               run(force=a.force, week=a.week, only_game=a.game, base=a.out, use_local=not a.no_local, now=now))
    except Exception as e:  # noqa: BLE001 - never crash-loop launchd; log and retry next tick
        log.exception("dossier run failed: %s", e)
        return 1
    finally:
        shutil.rmtree(LOCK, ignore_errors=True)
    print(json.dumps(res))
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
