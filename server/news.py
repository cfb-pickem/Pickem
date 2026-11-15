"""
Live college football news for Picky, with Ross Dellenger's items flagged.

Sources (all free, public, no login):
  - Ross Dellenger (Yahoo Sports): his Yahoo author page (headline, link,
    Yahoo's own blurb) + Google News RSS for "Ross Dellenger" (publish dates;
    only items whose source is Yahoo Sports are kept, so passing mentions
    elsewhere aren't credited to him).
  - ESPN college football news JSON (same public site.api.espn.com chat.py uses).
  - CBS Sports college football RSS.
His X/Twitter posts are NOT used: there is no free, no-login, ToS-clean
source for them (no official RSS; nitter.net is gone, xcancel is suspended).

Stored per item: headline, date, link, source, a 2-3 sentence summary taken
from the feed's own description, and team/conference/player tags. Never full
article text. Items older than 7 days are dropped. Cache:
server/cache/news.json (gitignored). chat.py only ever reads that file via
news_for(); it never fetches.

Summaries come from the feed blurb. Only a blurb longer than LONG_DESC is
condensed by the LOCAL Ollama chat model, at most OLLAMA_MAX_PER_RUN per run
and only while the chat server reports no member traffic. Each call evicts
chat's cached prompt prefix (one slow first answer), so it stays rare.

CLI:  news.py refresh [--force] [--no-ollama]    news.py lookup "query"
"""

from __future__ import annotations

import datetime as dt
import email.utils
import html
import json
import logging
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from zoneinfo import ZoneInfo

log = logging.getLogger("picky.news")

CACHE_PATH = Path(os.environ.get("PICKY_NEWS_CACHE", Path(__file__).with_name("cache") / "news.json"))
CT = ZoneInfo("America/Chicago")
KEEP_DAYS = 7
SUMMARY_MAX = 240
DATE_FETCH_MAX = 30
DELLENGER_BOOST = dt.timedelta(days=2)
LONG_DESC = 400
OLLAMA_MAX_PER_RUN = 2
OLLAMA = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
CHAT_HEALTH = os.environ.get("PICKY_CHAT_HEALTH", "http://127.0.0.1:8766/health")
# same model + num_ctx as chat.py, so Ollama never reloads or swaps models
MODEL = os.environ.get("PICKEM_CHAT_MODEL", "qwen3.5:9b-q4_K_M")
NUM_CTX = 16384

DELLENGER_AUTHOR = "https://sports.yahoo.com/author/ross-dellenger/"
DELLENGER_GNEWS = ("https://news.google.com/rss/search?q=%22Ross+Dellenger%22+when:7d"
                   "&hl=en-US&gl=US&ceid=US:en")
ESPN_NEWS = "https://site.api.espn.com/apis/site/v2/sports/football/college-football/news?limit=50"
CBS_RSS = "https://www.cbssports.com/rss/headlines/college-football/"

UA = {"User-Agent": "PickyNews/1.0 (personal league chatbot; reads public feeds every 15-30 min)"}
# ESPN 403s non-browser clients (see espn.py)
BROWSER = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
}

SPONSORED = re.compile(r"promo code|bonus code|sportsbook promo", re.I)
# podcast boilerplate sentences dropped from blurbs
PROMO_SENT = re.compile(r"subscribe|check out the full|wherever you listen", re.I)

FBS_CONFS = {"SEC", "Big Ten", "Big 12", "ACC", "Pac-12", "American Athletic", "Sun Belt",
             "Mid-American", "Mountain West", "Conference USA", "FBS Independents"}
# how conferences are written in headlines -> CFBD conference name
CONF_WORDS = {
    "SEC": "SEC", "Big Ten": "Big Ten", "Big 12": "Big 12", "ACC": "ACC",
    "Pac-12": "Pac-12", "Pac 12": "Pac-12", "American Athletic": "American Athletic",
    "AAC": "American Athletic", "Sun Belt": "Sun Belt", "MAC": "Mid-American",
    "Mid-American": "Mid-American", "Mountain West": "Mountain West",
    "Conference USA": "Conference USA", "C-USA": "Conference USA",
}


# -----------------------------------------------------------------------------
# text helpers
# -----------------------------------------------------------------------------

def clean(s: str | None) -> str:
    s = html.unescape(re.sub(r"<[^>]+>", " ", s or ""))
    return re.sub(r"\s+", " ", s).strip()


def key_for(title: str) -> str:
    """Dedupe key: the headline, lowercased, alphanumerics only."""
    return re.sub(r"[^a-z0-9]+", "", clean(title).lower())[:90]


def short_summary(desc: str | None) -> str:
    """First 2-3 sentences of a feed blurb, capped at SUMMARY_MAX chars."""
    d = clean(desc).lstrip("—–- ").strip()
    if not d:
        return ""
    sents = [x for x in re.split(r"(?<=[.!?])\s+", d) if not PROMO_SENT.search(x)]
    out = ""
    for s in sents[:3]:
        if out and len(out) + 1 + len(s) > SUMMARY_MAX:
            break
        out = f"{out} {s}".strip()
    if len(out) > SUMMARY_MAX:
        out = out[:SUMMARY_MAX].rsplit(" ", 1)[0] + "..."
    return out


def parse_date(s: str | None) -> str | None:
    """RFC 822 or ISO 8601 -> ISO 8601 UTC, None if unparseable."""
    if not s:
        return None
    s = s.strip()
    try:
        d = email.utils.parsedate_to_datetime(s)
    except (TypeError, ValueError):
        try:
            d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d.astimezone(dt.timezone.utc).isoformat(timespec="seconds")


def _item(title, link, date, desc, source, dellenger=False) -> dict:
    it = {"title": clean(title), "link": (link or "").strip(), "date": parse_date(date),
          "summary": short_summary(desc), "source": source, "dellenger": dellenger}
    if len(clean(desc)) > LONG_DESC:  # in memory only, for the Ollama condense; never saved
        it["_desc"] = clean(desc)[:1500]
    return it


# -----------------------------------------------------------------------------
# parsers (pure, unit-tested)
# -----------------------------------------------------------------------------

def parse_rss(xml_text: str, source: str) -> list[dict]:
    root = ET.fromstring(xml_text)
    out = []
    for it in root.iter("item"):
        title = it.findtext("title") or ""
        if not clean(title) or SPONSORED.search(title + " " + (it.findtext("description") or "")):
            continue
        out.append(_item(title, it.findtext("link"), it.findtext("pubDate"),
                         it.findtext("description"), source))
    return out


def parse_gnews_dellenger(xml_text: str) -> list[dict]:
    """Google News search for his name. Keep Yahoo Sports items only (his
    outlet); strip the ' - Yahoo Sports' suffix Google appends to titles."""
    root = ET.fromstring(xml_text)
    out = []
    for it in root.iter("item"):
        src = clean(it.findtext("source"))
        if src != "Yahoo Sports":
            continue
        title = clean(it.findtext("title"))
        if title.endswith(" - " + src):
            title = title[: -len(src) - 3]
        # Google's description is just the headline again: no summary here
        out.append(_item(title, it.findtext("link"), it.findtext("pubDate"), "", "Yahoo Sports", True))
    return out


_YLK = re.compile(r'data-ylk="([^"]*)"')
_BLURB = re.compile(r"</h3>\s*<div[^>]*>\s*<div[^>]*>(.*?)</div>", re.S)


def parse_author_page(page: str) -> list[dict]:
    """His Yahoo author page: each headline link carries data-ylk with the
    title (slk) and path (tar_uri); Yahoo's blurb follows the <h3>. No dates
    on the page (Google News supplies them)."""
    hits = []
    for m in _YLK.finditer(page):
        y = html.unescape(m.group(1))
        d = dict(kv.split(":", 1) for kv in y.split(";") if ":" in kv)
        if d.get("elm") == "hdln" and d.get("slk") and d.get("tar_uri", "").startswith("/"):
            hits.append((m.end(), d))
    out, seen = [], set()
    for i, (pos, d) in enumerate(hits):
        if d["tar_uri"] in seen:
            continue
        seen.add(d["tar_uri"])
        end = hits[i + 1][0] if i + 1 < len(hits) else pos + 4000
        b = _BLURB.search(page, pos, end)
        out.append(_item(d["slk"], "https://sports.yahoo.com" + d["tar_uri"], None,
                         b.group(1) if b else "", "Yahoo Sports", True))
    return out


def parse_espn(data: dict) -> list[dict]:
    out = []
    for a in data.get("articles") or []:
        if not a.get("headline") or a.get("type") == "Media":  # skip video clips
            continue
        link = ((a.get("links") or {}).get("web") or {}).get("href")
        it = _item(a["headline"], link, a.get("published"), a.get("description"), "ESPN")
        # ESPN tags teams itself ("Kentucky Wildcats"); kept as extra tag text
        it["hint"] = " ".join(c.get("description") or "" for c in a.get("categories") or []
                              if c.get("type") in ("team", "athlete"))
        out.append(it)
    return out


# -----------------------------------------------------------------------------
# merge / dedupe / prune
# -----------------------------------------------------------------------------

def merge(cache: dict, new: list[dict], now: dt.datetime) -> dict:
    """Merge fresh items into cache["items"] by headline key. A duplicate keeps
    the best of both: a real link over a news.google one, a date over none, a
    summary over none, Dellenger credit if either side has it."""
    items = cache.setdefault("items", {})
    now_iso = now.astimezone(dt.timezone.utc).isoformat(timespec="seconds")
    for it in new:
        k = key_for(it["title"])
        if not k:
            continue
        old = items.get(k)
        if old is None:
            items[k] = {**it, "first_seen": now_iso}
            continue
        if it["link"] and ("news.google.com" in old["link"] or not old["link"]) \
                and "news.google.com" not in it["link"]:
            old["link"] = it["link"]
        old["date"] = old.get("date") or it.get("date")
        if not old.get("summary") and it.get("summary"):
            old["summary"] = it["summary"]
        old["dellenger"] = old.get("dellenger") or it.get("dellenger", False)
        if it.get("hint") and not old.get("hint"):
            old["hint"] = it["hint"]
    return cache


def item_time(it: dict) -> dt.datetime:
    return dt.datetime.fromisoformat(it.get("date") or it["first_seen"])


def prune(cache: dict, now: dt.datetime, days: int = KEEP_DAYS) -> dict:
    cut = now - dt.timedelta(days=days)
    cache["items"] = {k: v for k, v in cache.get("items", {}).items() if item_time(v) >= cut}
    return cache


# -----------------------------------------------------------------------------
# tagging: teams / conferences / players from the CFBD cache
# -----------------------------------------------------------------------------

class Tagger:
    def __init__(self, teams: dict[str, str], players: dict[str, str]):
        """teams: display name -> conference, ALL divisions, so "Northern
        Arizona" consumes its text instead of tagging Arizona; only FBS hits
        are kept. players: full name -> team (FBS only)."""
        self.team_conf = teams
        self.player_team = players
        self._team_lc = {n.lower(): n for n in teams}
        self._player_lc = {n.lower(): n for n in players}
        self.team_re = self._alt(teams)
        self.player_re = self._alt(players)
        self.conf_re = self._alt(CONF_WORDS, ignore_case=False)

    @staticmethod
    def _alt(names, ignore_case=True):
        if not names:
            return None
        # longest first so "Florida State" wins over "Florida"
        alt = "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))
        return re.compile(rf"(?<![\w&])(?:{alt})(?![\w&])", re.I if ignore_case else 0)

    def tag(self, text: str, derive_confs: bool = True) -> dict:
        """derive_confs: also tag each team's conference (for items; off for
        questions, so "Kansas news" doesn't pull every Big 12 story)."""
        teams, players, confs = set(), set(), set()
        if self.player_re:
            players |= {self._player_lc[m.group(0).lower()] for m in self.player_re.finditer(text)}
            text = self.player_re.sub(" ", text)  # "Isaiah Marshall" is not Marshall
        if self.team_re:
            teams |= {self._team_lc[m.group(0).lower()] for m in self.team_re.finditer(text)}
            teams = {t for t in teams if self.team_conf.get(t) in FBS_CONFS}
        if self.conf_re:
            confs |= {CONF_WORDS[m.group(0)] for m in self.conf_re.finditer(text)}
        if derive_confs:
            confs |= {self.team_conf[t] for t in teams if self.team_conf[t] != "FBS Independents"}
        return {"teams": sorted(teams), "confs": sorted(confs), "players": sorted(players)}


_tagger: Tagger | None = None


def tagger() -> Tagger | None:
    """FBS teams + FBS players from the CFBD cache (built once per process)."""
    global _tagger
    if _tagger is None:
        try:
            import cfbd
            idx = cfbd.load_index()
        except Exception as e:  # cache missing/corrupt: tag nothing, never crash
            log.warning("no CFBD index for tagging: %s", e)
            return None
        if not idx:
            return None
        teams = {t["name"]: t.get("conference") for t in idx["teams"].values() if t.get("name")}
        players = {p["name"]: p["team"] for p in idx["players"]
                   if teams.get(p.get("team")) in FBS_CONFS and len(p.get("name", "").split()) >= 2}
        _tagger = Tagger(teams, players)
    return _tagger


def tag_items(cache: dict, tg: Tagger | None) -> None:
    if not tg:
        return
    for it in cache["items"].values():
        if "teams" not in it:
            it.update(tg.tag(f"{it['title']} {it.get('summary', '')} {it.get('hint', '')}"))


# -----------------------------------------------------------------------------
# lookup for chat.py
# -----------------------------------------------------------------------------

_STOP = set("""what whats what's going news latest about any anything tell know heard hear
there their they them this that with from have been were will would could should does
did doing your yours you the and for are was not but who how why when where which
today tonight week weekend game games team teams college football update updates
happening happened think picky said says report reports deal get got his her has had
can its out all one say now see new lol yo hey man bro""".split())

_mem: tuple[float, dict] | None = None


def _stem(w: str) -> str:
    """'injuries' -> 'injur', 'transfers' -> 'transfer' (prefix-matched)."""
    for suf in ("ies", "es", "s", "ed", "ing"):
        if w.endswith(suf) and len(w) - len(suf) >= 4:
            return w[: -len(suf)]
    return w


def load_cache(path: Path | None = None) -> dict:
    """Cache file, re-read only when its mtime changes."""
    global _mem
    p = Path(path or CACHE_PATH)
    try:
        m = p.stat().st_mtime
    except FileNotFoundError:
        return {"items": {}}
    if _mem is None or _mem[0] != m or path is not None:
        with open(p) as f:
            _mem = (m, json.load(f))
    return _mem[1]


def _fmt_day(it: dict) -> str:
    d = item_time(it).astimezone(CT)
    return f"{d:%b} {d.day}"


def news_line(it: dict) -> str:
    who = "Dellenger" if it.get("dellenger") else it.get("source", "?")
    line = f"{_fmt_day(it)}, {who}: {it['title']}"
    if it.get("summary") and key_for(it["summary"]) != key_for(it["title"]):
        line += f" ({it['summary']})"
    return line


def news_for(query: str | None = None, teams: list[str] | None = None, limit: int = 3,
             cache: dict | None = None, tg: Tagger | None = None) -> list[str]:
    """Short dated lines ("Nov 14, Dellenger: ...") for a question and/or
    team names. If the question names a team, conference or player, only
    items tagged with one of them come back (empty list if none). Otherwise
    keyword matches (empty list if none); ties go to the newest, his items
    counted 2 days newer. A generic question ("what's going on?", "any
    news?") gets his newest item plus the newest FBS news."""
    cache = cache if cache is not None else load_cache()
    items = list(cache.get("items", {}).values())
    if not items:
        return []
    tg = tg if tg is not None else tagger()
    want = {"teams": set(teams or []), "confs": set(), "players": set()}
    if query and tg:
        for k, v in tg.tag(query, derive_confs=False).items():
            want[k] |= set(v)
    entity = any(want.values())
    words = {_stem(w) for w in re.findall(r"[a-z0-9][a-z0-9'-]{2,}", (query or "").lower()) if w not in _STOP}

    def entity_score(it):
        s = 3 * len(want["teams"] & set(it.get("teams", [])))
        s += 3 * len(want["players"] & set(it.get("players", [])))
        s += 2 * len(want["confs"] & set(it.get("confs", [])))
        return s

    def word_score(it):
        text = f"{it['title']} {it.get('summary', '')}".lower()
        return sum(1 for w in words if re.search(rf"\b{re.escape(w)}", text))

    scored = []
    for it in items:
        e = entity_score(it) if entity else 0
        if entity and not e:
            continue  # named a team/conf/player: nothing off-topic
        scored.append((e + word_score(it), it))
    if not entity and words and not any(s for s, _ in scored):
        return []  # a specific topic nobody covered: say so, never filler
    if not entity and not words:
        # generic "what's going on": his newest item, then the newest FBS news
        newest = sorted(items, key=item_time, reverse=True)
        top = [it for it in newest if it.get("dellenger")][:1]
        top += [it for it in newest if not it.get("dellenger")
                and (it.get("teams") or "teams" not in it)][: limit - len(top)]
        return [news_line(it) for it in top[:limit]]
    scored = [(s, it) for s, it in scored if s > 0]
    # equal relevance: newest first, with his items counted DELLENGER_BOOST newer
    scored.sort(key=lambda x: (x[0], item_time(x[1]) + (DELLENGER_BOOST if x[1].get("dellenger")
                                                          else dt.timedelta(0))), reverse=True)
    return [news_line(it) for _, it in scored[:limit]]


def as_of(cache: dict | None = None) -> str | None:
    """Date of the newest cached item, e.g. "Nov 14" (for "as of" phrasing)."""
    cache = cache if cache is not None else load_cache()
    items = list(cache.get("items", {}).values())
    return _fmt_day(max(items, key=item_time)) if items else None


# -----------------------------------------------------------------------------
# refresh (LaunchAgent com.pickem.news-refresh)
# -----------------------------------------------------------------------------

def due(cache: dict, now: dt.datetime) -> bool:
    """Every 30 min, every 15 on Saturdays 11am-midnight CT (game windows).
    The LaunchAgent fires every 15 min; this decides whether to run."""
    last = cache.get("fetched_at")
    if not last:
        return True
    ct = now.astimezone(CT)
    gap = 15 if ct.weekday() == 5 and ct.hour >= 11 else 30
    return now - dt.datetime.fromisoformat(last) >= dt.timedelta(minutes=gap - 1)


def _chat_idle(client) -> bool:
    try:
        g = client.get(CHAT_HEALTH, timeout=3).json().get("gate") or {}
        return g.get("members_active", 1) == 0 and not g.get("test_busy")
    except Exception:
        return False


def _condense(client, it: dict, desc: str) -> str | None:
    prompt = ("Rewrite this news blurb as at most 2 short factual sentences. Use only facts "
              f"stated in it, add nothing.\n\nHeadline: {it['title']}\nBlurb: {desc[:1500]}\n\nSummary:")
    r = client.post(f"{OLLAMA}/api/generate", timeout=60, json={
        "model": MODEL, "prompt": prompt, "stream": False, "think": False,
        "options": {"num_ctx": NUM_CTX, "temperature": 0.1, "num_predict": 120}})
    r.raise_for_status()
    return short_summary(r.json().get("response")) or None


_DATE = re.compile(r'"datePublished\\?":\\?"([^"\\]+)')


def page_date(html_text: str) -> str | None:
    m = _DATE.search(html_text)
    return parse_date(m.group(1)) if m else None


def fill_dates(client, cache: dict) -> int:
    """His author page has no dates and Google News misses some of his items:
    read datePublished from the page itself (date only, nothing else kept).
    Remembered per link in cache["dates"] so each page is read once."""
    memo = cache.setdefault("dates", {})
    n = 0
    for it in cache["items"].values():
        link = it.get("link", "")
        if it.get("date") or not link.startswith("https://sports.yahoo.com/"):
            continue
        if link not in memo and n < DATE_FETCH_MAX:
            n += 1
            try:
                memo[link] = page_date(client.get(link, headers=UA).text)
            except Exception as e:
                log.warning("date fetch failed %s: %s", link, e)
        if memo.get(link):
            it["date"] = memo[link]
    return n


def fetch_all(client) -> tuple[list[dict], dict]:
    """Fetch every feed; one failing feed never blocks the others."""
    got, status = [], {}
    jobs = [
        ("dellenger_author", lambda: parse_author_page(client.get(DELLENGER_AUTHOR, headers=UA).text)),
        ("dellenger_gnews", lambda: parse_gnews_dellenger(client.get(DELLENGER_GNEWS, headers=UA).text)),
        ("espn", lambda: parse_espn(client.get(ESPN_NEWS, headers=BROWSER).json())),
        ("cbs", lambda: parse_rss(client.get(CBS_RSS, headers=UA).text, "CBS Sports")),
    ]
    for name, job in jobs:
        try:
            items = job()
            got += items
            status[name] = len(items)
        except Exception as e:
            log.warning("feed %s failed: %s", name, e)
            status[name] = f"error: {type(e).__name__}"
    return got, status


def refresh(force: bool = False, use_ollama: bool = True, now: dt.datetime | None = None) -> dict | None:
    import httpx
    now = now or dt.datetime.now(dt.timezone.utc)
    try:
        cache = json.loads(CACHE_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        cache = {"items": {}}
    if not force and not due(cache, now):
        return None
    with httpx.Client(timeout=20, follow_redirects=True) as client:
        new, status = fetch_all(client)
        before = set(cache.get("items", {}))
        merge(cache, new, now)
        fill_dates(client, cache)
        prune(cache, now)
        live = {it["link"] for it in new}
        cache["dates"] = {k: v for k, v in cache.get("dates", {}).items() if k in live}
        tag_items(cache, tagger())
        condensed = 0
        if use_ollama:
            long_ones = [it for k, it in cache["items"].items() if k not in before and it.get("_desc")]
            if long_ones and _chat_idle(client):
                for it in long_ones[:OLLAMA_MAX_PER_RUN]:
                    try:
                        s = _condense(client, it, it["_desc"])
                        if s:
                            it["summary"] = s
                            condensed += 1
                    except Exception as e:
                        log.warning("ollama condense failed: %s", e)
                        break
    for it in cache["items"].values():
        it.pop("_desc", None)
        if "teams" in it:
            it.pop("hint", None)
    cache["fetched_at"] = now.isoformat(timespec="seconds")
    cache["feeds"] = status
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CACHE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(cache, indent=1, sort_keys=True))
    os.replace(tmp, CACHE_PATH)
    added = len(set(cache["items"]) - before)
    summary = {"fetched_at": cache["fetched_at"], "feeds": status, "items": len(cache["items"]),
               "new": added, "dellenger": sum(1 for i in cache["items"].values() if i.get("dellenger")),
               "ollama_condensed": condensed}
    log.info("refresh %s", summary)
    return summary


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if argv[:1] == ["refresh"]:
        r = refresh(force="--force" in argv, use_ollama="--no-ollama" not in argv)
        print(json.dumps(r) if r else "not due")
        return 0
    if argv[:1] == ["lookup"]:
        q = " ".join(argv[1:]) or None
        print(f"as of {as_of()}")
        print("\n".join(news_for(q, limit=5)) or "(no matching news)")
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
