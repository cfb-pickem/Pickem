"""
Breaking college football news for Picky: a plain-code pipeline, no LLM.

  fetch -> parse -> merge (same headline = one item) -> tag (teams / players /
  conferences from the CFBD cache; topics, injury status, reported vs
  confirmed from the text) -> cluster (one story from several outlets = one
  story listing every source) -> supersede (a newer status or confirmation
  drops the older story) -> rank + render rider lines from those fields.

No Opus / claude -p and no Ollama anywhere in a refresh (Charlie, 2026-11-16:
"auto feed pipelines ... so you don't need an Opus call every time"). Rider
lines are built from the feed's own headline + first sentence. Opus only
runs offline in news_qa.py (a once-a-day spot check of the tagging).

Sources (all checked from the Mac mini 2026-11-16; public RSS/JSON only, no
logins, no paywall or CAPTCHA workarounds):
  ESPN       CFB RSS + the site.api news JSON (auto game previews skipped)
  Yahoo      CFB RSS (with bylines: Dellenger, Wetzel, Forde ...) + Ross
             Dellenger's author page; Yahoo also syndicates USA Today
             Network team sites, labeled by their own name
  CBS Sports CFB RSS (bylines: Zenitz ...)
  The Athletic  free CFB headline RSS
  On3        football RSS
  Action Network  NCAAF RSS (lines, injuries, betting; McMurphy; promos skipped)
  AP         Google News RSS restricted to apnews.com
  Insiders   Google News RSS for one insider per run (rotating), kept only
             when the item is from that insider's own outlet: Dellenger,
             Wetzel (Yahoo), Pete Thamel, Chris Low (ESPN), Bruce Feldman
             (The Athletic), Brett McMurphy (Action Network), Matt Zenitz (CBS)
Dropped after checking: 247Sports (403/406), USA Today's RSS (gone), SI
(404), Fox Sports RSS (empty), Reddit r/CFB (403), ESPN injury endpoints
(empty / 2020 data), X/Twitter (no free, ToS-clean source: never claim
Picky reads tweets).

Cadence: com.pickem.news-refresh fires every 5 min; a refresh runs every 15
min, every 5 on game days (freshness.game_day) or while a game is live or
kicks off within 3 h. Max ages live in freshness.py ("news" for the cache,
"news_item" for how long an item counts as breaking).

Cache server/cache/news.json (gitignored, 7 days). chat.py only reads it
(breaking_for / news_for), never fetches.

CLI:  news.py refresh [--force]    news.py lookup "query"    news.py stories
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
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

log = logging.getLogger("picky.news")

HERE = Path(__file__).resolve().parent
CACHE_PATH = Path(os.environ.get("PICKY_NEWS_CACHE", HERE / "cache" / "news.json"))
LIVE_CACHE = Path(os.environ.get("PICKY_LIVE_CACHE", HERE / "cache" / "live_games.json"))
CT = ZoneInfo("America/Chicago")
KEEP_DAYS = 7
SUMMARY_MAX = 240
LEDE_MAX = 170
DATE_FETCH_MAX = 30
DELLENGER_BOOST = dt.timedelta(days=2)
STORY_HOURS = 48          # items further apart than this are never one story
GOOGLE_BACKOFF_MIN = 60   # after a 429/503 from news.google.com

GN = "https://news.google.com/rss/search?hl=en-US&gl=US&ceid=US:en&q="
DELLENGER_AUTHOR = "https://sports.yahoo.com/author/ross-dellenger/"

UA = {"User-Agent": "PickyNews/1.1 (personal league chatbot; reads public feeds every 5-15 min)"}
# ESPN's JSON and Google News 403/429 non-browser clients more often
BROWSER = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36",
    "Accept": "application/json, application/xml, text/plain, */*",
}

SPONSORED = re.compile(r"promo code|bonus code|sportsbook promo|betting promos?|best .{0,30}promos", re.I)
OFF_SPORT = re.compile(r"\b(women'?s|basketball|hoops|NBA|WNBA|MLB|NHL|baseball|softball|volleyball|soccer|"
                       r"wrestling|March Madness)\b", re.I)
# Google's apnews.com search returns all of AP: keep an untagged AP item only if it's football
FOOTBALL = re.compile(r"football|\bcoach|quarterback|\bQB\b|touchdown|\bbowl\b|playoff|Heisman|\bCFP\b|"
                      r"\b(SEC|ACC|Big Ten|Big 12)\b|gridiron|kickoff|\bNCAA\b", re.I)
# ESPN/AP auto-written game previews ("Texas A&M visits Oklahoma in SEC matchup"): not news
AUTO_PREVIEW = re.compile(r"\b(hosts|visits|meet|meets|takes on|taking on|squaring off against|playing|against)\b"
                          r".{0,60}\b(matchup|action|play|showdown)$|^[\w&.-]+( [\w&.-]+)? (matchup|game) features ",
                          re.I)
PREVIEW_SUMMARY = re.compile(r"^[^.]{2,60}\(\d+-\d+\) at [^.]{2,60}\(\d+-\d+\), [A-Z][a-z]{2}\.? \d")


def is_auto_preview(it: dict) -> bool:
    return bool(AUTO_PREVIEW.search(it.get("title", "")) or PREVIEW_SUMMARY.search(it.get("summary", "")))


# sentence ends, but not after "No." / "Nov." / "Jr." / an initial
SENT_END = re.compile(r"(?<!\bNo\.)(?<!\bNov\.)(?<!\bJr\.)(?<!\bSr\.)(?<!\bSt\.)(?<!\bvs\.)(?<!\bMt\.)"
                      r"(?<!\b[A-Z]\.)(?<=[.!?])\s+")
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

# national desks: a story from one of these can headline "what's the latest";
# syndicated team sites (Yahoo's "Ducks Wire", "ClutchPoints") only back one up
TIER1 = ("ESPN", "Yahoo Sports", "CBS Sports", "The Athletic", "AP", "On3", "Action Network")
# insider -> outlets their byline is credited from (Google News source names)
INSIDERS = {
    "Ross Dellenger": ("Yahoo Sports",),
    "Dan Wetzel": ("Yahoo Sports",),
    "Pete Thamel": ("ESPN",),
    "Chris Low": ("ESPN",),
    "Bruce Feldman": ("The Athletic", "The New York Times"),
    "Brett McMurphy": ("Action Network",),
    "Matt Zenitz": ("CBS Sports",),
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
    sents = [x for x in SENT_END.split(d) if not PROMO_SENT.search(x)]
    out = ""
    for s in sents[:3]:
        if out and len(out) + 1 + len(s) > SUMMARY_MAX:
            break
        out = f"{out} {s}".strip()
    if len(out) > SUMMARY_MAX:
        out = out[:SUMMARY_MAX].rsplit(" ", 1)[0] + "..."
    return out


def lede(it: dict) -> str:
    """The feed blurb's first sentence, trimmed (the deterministic 'condense')."""
    s = SENT_END.split(it.get("summary") or "", maxsplit=1)[0].strip()
    if len(s) > LEDE_MAX:
        s = s[:LEDE_MAX].rsplit(" ", 1)[0] + "..."
    return "" if key_for(s) == key_for(it.get("title", "")) else s


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


def insider_of(author: str | None, outlet: str) -> str | None:
    a = clean(author).lower()
    for name, outlets in INSIDERS.items():
        if name.lower() in a and outlet in outlets:
            return name
    return None


def _item(title, link, date, desc, source, dellenger=False, author=None, insider=None) -> dict:
    author = clean(author) or None
    insider = insider or insider_of(author, source)
    return {"title": clean(title), "link": (link or "").strip(), "date": parse_date(date),
            "summary": short_summary(desc), "source": source, "author": author, "insider": insider,
            "dellenger": bool(dellenger or insider == "Ross Dellenger")}


def _lt(el, name: str) -> str | None:
    """Child text by local tag name (dc:creator, media:..., Yahoo's publisher)."""
    for ch in el:
        if ch.tag.rsplit("}", 1)[-1] == name:
            return ch.text
    return None


# -----------------------------------------------------------------------------
# parsers (pure, unit-tested)
# -----------------------------------------------------------------------------

def parse_rss(xml_text: str, source: str) -> list[dict]:
    """Any RSS 2.0 feed. Byline from dc:creator; a syndicated item (Yahoo's
    feed carries USA Today Network team sites) is labeled by its publisher."""
    root = ET.fromstring(xml_text)
    out = []
    for it in root.iter("item"):
        title = it.findtext("title") or ""
        desc = it.findtext("description") or ""
        if not clean(title) or SPONSORED.search(title + " " + desc) or OFF_SPORT.search(title) \
                or is_auto_preview({"title": clean(title), "summary": clean(desc)}):
            continue
        pub = clean(_lt(it, "publisher") or _lt(it, "source"))
        outlet = pub if pub and source == "Yahoo Sports" else source
        out.append(_item(title, it.findtext("link"), it.findtext("pubDate"), desc, outlet,
                         author=_lt(it, "creator")))
    return out


def parse_gnews(xml_text: str, keep: tuple[str, ...], label: str | None = None,
                insider: str | None = None) -> list[dict]:
    """Google News search RSS. Keeps items whose source is one of `keep`,
    strips the ' - <source>' suffix Google appends, labels them `label` (or
    the source). Google's description just repeats the headline: no summary."""
    root = ET.fromstring(xml_text)
    out = []
    for it in root.iter("item"):
        src = clean(it.findtext("source"))
        if src not in keep:
            continue
        title = clean(it.findtext("title"))
        if title.endswith(" - " + src):
            title = title[: -len(src) - 3]
        if SPONSORED.search(title) or OFF_SPORT.search(title):
            continue
        outlet = label or src
        out.append(_item(title, it.findtext("link"), it.findtext("pubDate"), "", outlet, insider=insider))
    return out


def parse_gnews_dellenger(xml_text: str) -> list[dict]:
    return parse_gnews(xml_text, ("Yahoo Sports",), insider="Ross Dellenger")


_YLK = re.compile(r'data-ylk="([^"]*)"')
_BLURB = re.compile(r"</h3>\s*<div[^>]*>\s*<div[^>]*>(.*?)</div>", re.S)


def parse_author_page(page: str) -> list[dict]:
    """His Yahoo author page: each headline link carries data-ylk with the
    title (slk) and path (tar_uri); Yahoo's blurb follows the <h3>. No dates
    on the page (fill_dates reads them from the article)."""
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
                         b.group(1) if b else "", "Yahoo Sports", True, insider="Ross Dellenger"))
    return out


def parse_espn(data: dict) -> list[dict]:
    out = []
    for a in data.get("articles") or []:
        # video clips and the auto-written game previews/recaps aren't news
        if not a.get("headline") or a.get("type") in ("Media", "Preview", "Recap"):
            continue
        link = ((a.get("links") or {}).get("web") or {}).get("href")
        it = _item(a["headline"], link, a.get("published"), a.get("description"), "ESPN")
        # ESPN tags teams itself ("Kentucky Wildcats"); kept as extra tag text
        it["hint"] = " ".join(c.get("description") or "" for c in a.get("categories") or []
                              if c.get("type") in ("team", "athlete"))
        out.append(it)
    return out


# -----------------------------------------------------------------------------
# feeds
# -----------------------------------------------------------------------------

@dataclass(frozen=True)
class Feed:
    name: str
    url: str
    parse: Callable[[str], list[dict]]
    headers: dict
    every_min: int = 0        # at most once per this many minutes (0 = every refresh)


def _gn(q: str) -> str:
    return GN + q.replace(" ", "+").replace('"', "%22")


FEEDS = [
    Feed("espn_rss", "https://www.espn.com/espn/rss/ncf/news", lambda t: parse_rss(t, "ESPN"), UA),
    Feed("espn", "https://site.api.espn.com/apis/site/v2/sports/football/college-football/news?limit=50",
         lambda t: parse_espn(json.loads(t)), BROWSER),
    Feed("yahoo", "https://sports.yahoo.com/college-football/rss/", lambda t: parse_rss(t, "Yahoo Sports"), UA),
    Feed("cbs", "https://www.cbssports.com/rss/headlines/college-football/", lambda t: parse_rss(t, "CBS Sports"), UA),
    Feed("athletic", "https://www.nytimes.com/athletic/rss/college-football/",
         lambda t: parse_rss(t, "The Athletic"), UA),
    Feed("on3", "https://www.on3.com/category/football/feed/", lambda t: parse_rss(t, "On3"), UA),
    Feed("action", "https://www.actionnetwork.com/ncaaf/feed", lambda t: parse_rss(t, "Action Network"), UA),
    Feed("ap", _gn("college football site:apnews.com when:2d"), lambda t: parse_gnews(t, ("AP News",), "AP"),
         BROWSER),
    Feed("dellenger_author", DELLENGER_AUTHOR, parse_author_page, UA, every_min=30),
]


def insider_feed(name: str) -> Feed:
    return Feed(f"insider:{name}", _gn(f'"{name}" when:7d'),
                lambda t, n=name: parse_gnews(t, INSIDERS[n], INSIDERS[n][0], insider=n), BROWSER)


# -----------------------------------------------------------------------------
# merge / dedupe / prune
# -----------------------------------------------------------------------------

def merge(cache: dict, new: list[dict], now: dt.datetime) -> dict:
    """Merge fresh items into cache["items"] by headline key. A duplicate keeps
    the best of both: a real link over a news.google one, a date over none, a
    summary over none, a byline / insider credit if either side has it."""
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
        for f in ("author", "insider", "hint"):
            if it.get(f) and not old.get(f):
                old[f] = it[f]
    return cache


def item_time(it: dict) -> dt.datetime:
    return dt.datetime.fromisoformat(it.get("date") or it["first_seen"])


def prune(cache: dict, now: dt.datetime, days: int = KEEP_DAYS) -> dict:
    """Drop items older than `days` and auto-written game previews."""
    cut = now - dt.timedelta(days=days)
    cache["items"] = {k: v for k, v in cache.get("items", {}).items()
                      if item_time(v) >= cut and not is_auto_preview(v) and not OFF_SPORT.search(v.get("title", ""))}
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
        self.team_words = {w.lower() for n in teams for w in re.findall(r"[A-Za-z&'.]+", n)}

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
    for it in cache["items"].values():
        it.update(classify(f"{it['title']}. {it.get('summary', '')}"))
        if tg and "teams" not in it:
            it.update(tg.tag(f"{it['title']} {it.get('summary', '')} {it.get('hint', '')}"))


# -----------------------------------------------------------------------------
# topics / injury status / reported vs confirmed (regex, unit-tested)
# -----------------------------------------------------------------------------

TOPICS = [
    ("injury", r"injur\w*|surgery|out for (the )?(season|year|game|saturday)|ruled out|questionable|doubtful|"
               r"\bACL\b|concussion|sidelined|season[- ]ending|day[- ]to[- ]day|game[- ]time decision|"
               r"(won'?t|will not) play|(will|to) miss (the )?(rest|remainder|season|year|games?|saturday|time|several|multiple|next|\d+|one|two|three|four|six)\b|status (for|vs|against)|availability|\bhurt\b|"
               r"\bankle\b|\bknee\b|\bhamstring\b|\bshoulder\b|cleared to (play|return)"),
    ("suspension", r"suspen\w*|ineligib\w*|arrest\w*|dismiss\w* from|charged with|NCAA investigation"),
    ("coaching", r"\bfire[sd]?\b|firing|\bhire[sd]?\b|hiring|coach(ing)? search|hot seat|buyout|interim coach|"
                 r"carousel|contract extension|steps? down|resign\w*|parts? ways|\bretir\w*|"
                 r"(new|next) (head )?coach\b|(coaching|head coach(ing)?) (job|opening|vacancy|search)|"
                 r"coordinator (hire|search|fired|out)"),
    ("portal", r"transfer portal|\bportal\b|transfer(s|ring|red)?\b|enter(s|ed)? the portal"),
    ("recruiting", r"commit(s|ted|ment)?\b|decommit\w*|\bflip(s|ped)?\b|recruit\w*|signing day|five-star|four-star"),
    ("rankings", r"\bAP (poll|top 25)\b|rankings?|\bCFP\b|playoff (picture|race|projection|field)|top 25|"
                 r"power rankings|bowl projection"),
    ("betting", r"\bodds\b|betting|\bbets?\b|predictions?|expert picks|picks? against the spread|\bATS\b|"
                r"point spread|\bline (move|movement)|over/under|sportsbook|polymarket"),
    ("realignment", r"realign\w*|join(s|ing)? the (SEC|Big Ten|Big 12|ACC|Pac-12)|conference (exit|move)|"
                    r"media rights|grant of rights"),
    ("money", r"revenue[- ]sharing|\bNIL\b|House settlement|\bcap\b|collective|salary"),
]
_TOPIC_RX = [(t, re.compile(rf"\b(?:{rx})", re.I)) for t, rx in TOPICS]

_ST_OUT = re.compile(r"\b(ruled out|out for (the )?(season|year|game|saturday)|is out\b|won'?t play|"
                     r"(will|to|likely to|expected to) miss (the )?(rest|remainder|season|year|games?|saturday|time|"
                     r"several|multiple|next|\d+|one|two|three|four|six)\b|"
                     r"will not play|season[- ]ending|torn|tears?\b|undergo(es)? surgery|underwent surgery|"
                     r"to have .{0,20}surgery|sidelined|not expected to play|out (vs\.?|against))", re.I)
_ST_DOUBT = re.compile(r"\bdoubtful\b", re.I)
_ST_QUES = re.compile(r"\b(questionable|game[- ]time decision|day[- ]to[- ]day|status (is )?(uncertain|unclear)|"
                      r"limited in practice|banged up|uncertain)\b", re.I)
_ST_AVAIL = re.compile(r"\b(will play|cleared to (play|return)|expected to (play|start)|available|"
                       r"returns? to practice|back at practice|probable|good to go|will start)\b", re.I)

_REPORTED = re.compile(r"\b(report(s|ed|edly)?|sources?|per (sources|a source)|according to|rumou?r\w*|"
                       r"told (espn|the athletic|on3|cbs|yahoo|reporters? off)|in talks|frontrunner|front-runner)\b",
                       re.I)
_OFFICIAL = re.compile(r"\b(announc\w+|officially|confirm\w*|statement|has been (fired|hired|named|suspended|"
                       r"dismissed)|fires|fired|names|named|hires|hired|undergoes|underwent|ruled out|suspends|"
                       r"suspended|signs|signed|steps down|resigns|retires|fines|fined|reprimand\w*|(coach|sarkisian|school|program|team)\b"
                       r"[^.]{0,40}\b(said|says|announced))\b", re.I)
_SOFT = re.compile(r"\b(expected to|is set to|set to (hire|become|be named)|plans? to|nearing|finaliz\w+|"
                   r"targeting|likely to|intends? to|considering|candidate)\b", re.I)


def injury_state(text: str) -> str | None:
    t = text or ""
    if _ST_OUT.search(t):
        return "out"
    if _ST_DOUBT.search(t):
        return "doubtful"
    if _ST_QUES.search(t):
        return "questionable"
    if _ST_AVAIL.search(t):
        return "available"
    return None


def classify(text: str) -> dict:
    """topics, kind ('injury' for pick_brief), status (injury items), label
    ('reported' = sourced/expected, 'confirmed' = announced/done, None)."""
    topics = [t for t, rx in _TOPIC_RX if rx.search(text or "")]
    head = (text or "").split(". ", 1)[0]
    t = text or ""
    # a named source / "reportedly" beats everything; then an official word ("fires",
    # "coach X said"); then soft language ("expected to", "likely to")
    label = "reported" if _REPORTED.search(t) else "confirmed" if _OFFICIAL.search(t) else \
        "reported" if _SOFT.search(t) else None
    injury = "injury" in topics
    status = injury_state(head) or injury_state(text) if injury else None
    return {"topics": topics, "kind": "injury" if injury else None, "status": status, "label": label}


# -----------------------------------------------------------------------------
# stories: cluster across outlets, supersede, rank
# -----------------------------------------------------------------------------

_WSTOP = set("""about after again against ahead also amid another back been before being best big biggest could
does doing down during each every first from gets going have here into just last latest look more most much
next only over said says season still than that their them then there these they this those week what when
where which while will with would year your college football game games team teams news report reports""".split())
_NAME_STOP = set("""week college football season game games playoff playoffs bowl national poll top rankings ranking
the a an and but for with after before how why what who when where this that new first last his her their its
saturday sunday monday tuesday wednesday thursday friday november december october september january august
qb rb wr te ol dl lb db cb edge coach coaches head offensive defensive coordinator star senior freshman junior
sophomore transfer portal heisman trophy power report reports sources source update live watch odds picks
injury injuries status nfl draft sec acc big ten pac mac aac cfp ap espn cbs yahoo athletic on3 tigers bulldogs
wildcats hoosiers longhorns buckeyes irish crimson tide gators sooners ducks huskies trojans bruins wolverines
spartans badgers hawkeyes cornhuskers nittany lions seminoles hurricanes cavaliers hokies tar heels cardinals
deacons demon yellow jackets volunteers commodores razorbacks rebels aggies cowboys bears horned frogs cougars
utes sun devils buffaloes jayhawks cyclones mountaineers knights bearcats owls mustangs""".split())


def _words(it: dict) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9']{4,}", it.get("title", "").lower()) if w not in _WSTOP}


def _names(it: dict, team_words: set[str]) -> set[str]:
    """People in the headline: each run of capitalized words that aren't team
    / conference / stock words, reduced to its last word ('Philip Rivers' ->
    'rivers'), so a full name counts once (Walden, Kanu, Cignetti)."""
    out = set()
    for run in re.findall(r"(?:\b[A-Z][a-zA-Z'’\-]{1,}\b\s?)+", it.get("title", "")):
        toks = [t.lower().replace("’", "'").removesuffix("'s") for t in run.split()]
        toks = [t for t in toks if len(t) >= 3 and t not in _NAME_STOP and t not in team_words]
        if toks:
            out.add(toks[-1])
    return out


def _jacc(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def _status_clash(a: dict, b: dict) -> bool:
    """Different injury statuses or reported-vs-confirmed: separate stories,
    so the newer one can supersede the older."""
    if a.get("status") and b.get("status") and a["status"] != b["status"]:
        return True
    return {a.get("label"), b.get("label")} == {"reported", "confirmed"}


def same_story(a: dict, b: dict, team_words: set[str] = frozenset()) -> bool:
    if abs((item_time(a) - item_time(b)).total_seconds()) > STORY_HOURS * 3600 or _status_clash(a, b):
        return False
    ents = (set(a.get("teams") or []) | set(a.get("players") or [])) & \
           (set(b.get("teams") or []) | set(b.get("players") or []))
    sa, sb = (set(re.findall(r"[a-z0-9']{4,}", (x.get("summary") or "").lower())) for x in (a, b))
    if len(sa) >= 6 and _jacc(sa, sb) >= 0.6:
        return True  # same wire blurb under two headlines
    if _jacc(_words(a), _words(b)) >= 0.5:
        # "X vs Y prediction, pick, odds" / "rankings" headlines look alike across games
        return bool(ents) or not {"betting", "rankings"} & set((a.get("topics") or []) + (b.get("topics") or []))
    if not ents or len(a.get("teams") or []) > 4 or len(b.get("teams") or []) > 4:
        return False  # roundups ("bowl projections") never merge
    topic = set(a.get("topics") or []) & set(b.get("topics") or []) - {"betting", "rankings"}
    if set(a.get("players") or []) & set(b.get("players") or []) and topic:
        return True
    shared = _names(a, team_words) & _names(b, team_words)
    return len(shared) >= 2 or (len(shared) == 1 and bool(topic) and _jacc(_words(a), _words(b)) >= 0.25)


def _tier(it: dict) -> int:
    return 0 if it.get("insider") else (1 if it.get("source") in TIER1 else 2)


def cluster(cache: dict, tg: Tagger | None = None) -> None:
    """it['story'] = key of the story's lead item (insider > national desk >
    syndicated, then earliest); every item of a story shares it."""
    items = cache.get("items", {})
    keys = sorted(items, key=lambda k: item_time(items[k]))
    tw = tg.team_words if tg else set()
    parent = {k: k for k in keys}

    def find(k):
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k

    for i, a in enumerate(keys):
        for b in keys[i + 1:]:
            if (item_time(items[b]) - item_time(items[a])).total_seconds() > STORY_HOURS * 3600:
                break
            if find(a) != find(b) and same_story(items[a], items[b], tw):
                parent[find(b)] = find(a)
    groups: dict[str, list[str]] = {}
    for k in keys:
        groups.setdefault(find(k), []).append(k)
    for ks in groups.values():
        lead = min(ks, key=lambda k: (_tier(items[k]), item_time(items[k])))
        for k in ks:
            items[k]["story"] = lead
            items[k].pop("superseded_by", None)


def stories(cache: dict, include_superseded: bool = False) -> list[dict]:
    items = cache.get("items", {})
    groups: dict[str, list[dict]] = {}
    for k, it in items.items():
        groups.setdefault(it.get("story") or k, []).append(it)
    out = []
    for sid, its in groups.items():
        lead = items.get(sid) or its[0]
        if lead.get("superseded_by") and not include_superseded:
            continue
        its.sort(key=lambda x: (_tier(x), item_time(x)))
        outlets = list(dict.fromkeys(credit(x) for x in its))
        out.append({"id": sid, "lead": lead, "items": its, "time": min(item_time(x) for x in its),
                    "outlets": outlets, "n_outlets": len({x.get("source") for x in its}),
                    "teams": sorted({t for x in its for t in x.get("teams") or []}),
                    "players": sorted({p for x in its for p in x.get("players") or []}),
                    "confs": sorted({c for x in its for c in x.get("confs") or []}),
                    "topics": sorted({t for x in its for t in x.get("topics") or []}),
                    "status": lead.get("status"), "label": lead.get("label"),
                    "insider": next((x["insider"] for x in its if x.get("insider")), None),
                    "superseded_by": lead.get("superseded_by")})
    return out


def supersede(cache: dict, tg: Tagger | None = None) -> int:
    """A newer story about the same subject replaces an older one when the
    injury status changed ('questionable' -> 'out') or a report got confirmed.
    Same subject = a shared player tag, or a shared team plus a shared name."""
    tw = tg.team_words if tg else set()
    sts = sorted(stories(cache, include_superseded=True), key=lambda s: s["time"])
    n = 0
    for i, old in enumerate(sts):
        for new in sts[i + 1:]:
            a, b = old["lead"], new["lead"]
            same = set(old["players"]) & set(new["players"]) or (
                set(old["teams"]) & set(new["teams"]) and _names(a, tw) & _names(b, tw))
            if not same or len(old["teams"]) > 4:
                continue
            injury = "injury" in old["topics"] and "injury" in new["topics"] and \
                old["status"] and new["status"] and old["status"] != new["status"]
            confirmed = old["label"] == "reported" and new["label"] == "confirmed" and \
                set(old["topics"]) & set(new["topics"])
            if injury or confirmed:
                for it in old["items"]:
                    it["superseded_by"] = new["id"]
                n += 1
                break
    return n


# -----------------------------------------------------------------------------
# lookup for chat.py
# -----------------------------------------------------------------------------

_STOP = set("""what whats what's going news latest about any anything tell know heard hear
there their they them this that with from have been were will would could should does
did doing your yours you the and for are was not but who how why when where which
today tonight week weekend game games team teams college football update updates
happening happened think picky said says report reports deal get got his her has had
can its out all one say now see new lol yo hey man bro breaking""".split())
# availability asks: "is Texas's QB playing", "will Arch Manning play", "is he out"
AVAIL_ASK = re.compile(r"\b(is|are|will|does|did)\b[^?.!]{0,40}\b(play(ing)?|suit(ing)? up|start(ing)?|out|"
                       r"available|healthy|hurt|injured|back)\b|\b(status|questionable|doubtful|game[- ]time "
                       r"decision|injury report|injur\w+|availability)\b", re.I)

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


def credit(it: dict) -> str:
    """'ESPN's Pete Thamel', 'Yahoo Sports' Ross Dellenger', or the outlet."""
    src = it.get("source") or "?"
    if it.get("insider"):
        return f"{src}{chr(39) if src.endswith('s') else chr(39) + 's'} {it['insider']}"
    return src


def ago(t: dt.datetime, now: dt.datetime) -> str:
    s = max(0.0, (now - t).total_seconds())
    if s < 3600:
        return f"{max(1, int(s // 60))} min ago"
    if s < 86400:
        return f"{int(s // 3600)}h ago"
    d = t.astimezone(CT)
    return "yesterday" if (now.astimezone(CT).date() - d.date()).days == 1 else f"{d:%b} {d.day}"


def breaking_window(now: dt.datetime) -> float:
    """Seconds an item counts as breaking (freshness.py 'news_item')."""
    try:
        import freshness
        return freshness.max_age("news_item", now)
    except Exception:  # noqa: BLE001
        return 24 * 3600.0


def story_line(st: dict, now: dt.datetime) -> str:
    lead = st["lead"]
    when = ago(st["time"], now)
    if (now - st["time"]).total_seconds() > breaking_window(now):
        when += " (older, not breaking)"
    others = [o for o in st["outlets"] if o != credit(lead)]
    line = f"{when}, per {credit(lead)}"
    if others:
        line += f" (also {', '.join(others[:3])}{' +' + str(len(others) - 3) if len(others) > 3 else ''})"
    if st["label"] == "reported":
        line += " [REPORTED, not confirmed]"
    if st["status"]:
        line += f" [status: {st['status']}]"
    line += f": {lead['title']}"
    ld = lede(lead) or next((lede(x) for x in st["items"] if lede(x)), "")
    return line + (f". {ld}" if ld else "")


def _wants(query: str | None, teams: list[str] | None, tg: Tagger | None) -> tuple[dict, set[str]]:
    want = {"teams": set(teams or []), "confs": set(), "players": set()}
    if query and tg:
        for k, v in tg.tag(query, derive_confs=False).items():
            want[k] |= set(v)
    words = {_stem(w) for w in re.findall(r"[a-z0-9][a-z0-9'-]{1,}", (query or "").lower())
             if w not in _STOP and (len(w) >= 3 or w == "qb")}
    if "qb" in words:
        words.add("quarterback")
    return want, words


def news_for(query: str | None = None, teams: list[str] | None = None, limit: int = 3,
             cache: dict | None = None, tg: Tagger | None = None) -> list[str]:
    """Short dated lines ("Nov 14, Dellenger: ...") for a question and/or
    team names. If the question names a team, conference or player, only
    items tagged with one of them come back (empty list if none). Otherwise
    keyword matches (empty list if none); ties go to the newest, his items
    counted 2 days newer. A generic question ("what's going on?", "any
    news?") gets his newest item plus the newest FBS news. Superseded items
    never come back. (chat.py now uses breaking_for; this stays for callers
    that want one line per item.)"""
    cache = cache if cache is not None else load_cache()
    items = [it for it in cache.get("items", {}).values() if not it.get("superseded_by")]
    if not items:
        return []
    tg = tg if tg is not None else tagger()
    want, words = _wants(query, teams, tg)
    entity = any(want.values())

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


def breaking_for(query: str | None = None, teams: list[str] | None = None, limit: int = 5,
                 cache: dict | None = None, tg: Tagger | None = None,
                 now: dt.datetime | None = None) -> list[str]:
    """The rider: up to `limit` stories, newest first, one line each with the
    time ("2h ago"), attribution ("per ESPN's Pete Thamel (also CBS Sports)"),
    [REPORTED] / [status] labels, headline and first sentence. Superseded
    stories never show; anything past the breaking window says so.
      named team / player / conference -> only stories tagged with it ([] if none)
      availability ask ("is X's QB playing") -> only injury/suspension stories
      other keywords -> keyword matches ([] if none: never filler)
      generic ("what's the latest") -> the newest national stories, multi-outlet
        and insider stories first, no betting picks or roundups"""
    cache = cache if cache is not None else load_cache()
    now = now or dt.datetime.now(dt.timezone.utc)
    sts = [s for s in stories(cache) if s["time"] <= now + dt.timedelta(minutes=5)]
    if not sts:
        return []
    tg = tg if tg is not None else tagger()
    want, words = _wants(query, teams, tg)
    entity = any(want.values())
    avail = bool(query and AVAIL_ASK.search(query))
    words -= {_stem(w) for w in ("playing", "play", "status", "injury", "injuries", "out", "available",
                                 "healthy", "hurt", "injured", "start", "starting", "back", "will")} if avail else set()

    def text(s):
        return " ".join(f"{x['title']} {x.get('summary', '')}" for x in s["items"]).lower()

    def score(s):
        e = 3 * len(want["teams"] & set(s["teams"])) + 3 * len(want["players"] & set(s["players"])) \
            + 2 * len(want["confs"] & set(s["confs"]))
        t = text(s)
        return e, sum(1 for w in words if re.search(rf"\b{re.escape(w)}", t))

    picked: list[dict]
    named = [n for n in INSIDERS if re.search(rf"\b{n.split()[-1]}\b", query or "", re.I)]
    if named:  # "what's Thamel saying": that insider's own bylines, newest first
        words -= {_stem(n.split()[-1].lower()) for n in named} | {"saying", "say", "writ", "wrote", "reporting"}
        sts = [s for s in sts if any(x.get("insider") in named for x in s["items"])]
        if not (entity or words):
            sts.sort(key=lambda s: s["time"], reverse=True)
            return [story_line(s, now) for s in sts[:limit]]
    if want["players"] and any(want["players"] & set(s["players"]) for s in sts):
        sts = [s for s in sts if want["players"] & set(s["players"])]  # a named player: his stories only
    if avail:
        sts = [s for s in sts if {"injury", "suspension"} & set(s["topics"])]
        if re.search(r"\b(qb|quarterback)s?\b", query or "", re.I):
            sts = [s for s in sts if re.search(r"\b(qb|quarterback)", text(s))]
    if entity or words or avail:
        scored = []
        for s in sts:
            e, w = score(s)
            if entity and not e:
                continue
            if not entity and words and not w:
                continue
            if len(s["teams"]) > 4 and not want["players"]:
                w -= 2  # roundups rank last
            lead = s["lead"]["title"].lower()
            w += any(re.search(rf"\b{re.escape(x.split()[-1].lower())}\b", lead)
                     for x in want["teams"] | want["players"])  # named in the headline ('Manning')
            w += s["n_outlets"] > 1 or bool(s["insider"]) or s["lead"].get("source") in TIER1  # a national desk
            scored.append(((e + w, s["time"]), s))
        scored.sort(key=lambda x: x[0], reverse=True)
        picked = [s for _, s in scored[:limit]]
    else:
        win = breaking_window(now)
        pool = [s for s in sts if (s["lead"].get("source") in TIER1 or s["insider"] or s["n_outlets"] > 1)
                and len(s["teams"]) <= 4 and "betting" not in s["topics"]]
        recent = [s for s in pool if (now - s["time"]).total_seconds() <= win] or pool

        def rank(s):  # hours of recency credit: +4h per extra outlet, +3h for an insider
            return -(now - s["time"]).total_seconds() / 3600 + 4 * (s["n_outlets"] - 1) + 3 * bool(s["insider"])
        picked = sorted(recent, key=rank, reverse=True)[:limit]
    picked.sort(key=lambda s: s["time"], reverse=True)
    return [story_line(s, now) for s in picked]


def as_of(cache: dict | None = None) -> str | None:
    """Date of the newest cached item, e.g. "Nov 14" (for "as of" phrasing)."""
    cache = cache if cache is not None else load_cache()
    items = list(cache.get("items", {}).values())
    return _fmt_day(max(items, key=item_time)) if items else None


def sources_line(cache: dict | None = None) -> str:
    """The outlets the last refresh actually read ('ESPN, Yahoo Sports, ...')."""
    cache = cache if cache is not None else load_cache()
    ok = [n for n, v in (cache.get("feeds") or {}).items() if isinstance(v, int)]
    names = {"espn_rss": "ESPN", "espn": "ESPN", "yahoo": "Yahoo", "cbs": "CBS", "athletic": "The Athletic",
             "on3": "On3", "action": "Action Network", "ap": "AP", "dellenger_author": "Yahoo"}
    out = [names.get(n, n.split(":", 1)[-1]) for n in ok if not n.startswith("insider:")]
    return ", ".join(dict.fromkeys(out)) + ", plus insiders' bylines"


# -----------------------------------------------------------------------------
# refresh (LaunchAgent com.pickem.news-refresh, every 5 min)
# -----------------------------------------------------------------------------

def live_window(now: dt.datetime, path: Path | None = None) -> bool:
    """A game is live or kicks off within 3 h (live_games.py's board file)."""
    try:
        games = (json.loads(Path(path or LIVE_CACHE).read_text()).get("games") or {}).values()
    except (OSError, ValueError, AttributeError):
        return False
    for g in games:
        if g.get("state") == "in":
            return True
        try:
            k = dt.datetime.fromisoformat((g.get("start") or "").replace("Z", "+00:00"))
        except ValueError:
            continue
        if dt.timedelta(0) <= k - now <= dt.timedelta(hours=3):
            return True
    return False


def gap_minutes(now: dt.datetime) -> int:
    try:
        import freshness
        gd = freshness.game_day(now)
    except Exception:  # noqa: BLE001
        gd = now.astimezone(CT).weekday() == 5
    return 5 if gd or live_window(now) else 15


def due(cache: dict, now: dt.datetime) -> bool:
    """Every 15 min, every 5 on game days / while games are live. The
    LaunchAgent fires every 5 min; this decides whether to run."""
    last = cache.get("fetched_at")
    if not last:
        return True
    return now - dt.datetime.fromisoformat(last) >= dt.timedelta(minutes=gap_minutes(now) - 0.5)


_DATE = re.compile(r'"datePublished\\?":\\?"([^"\\]+)')


def page_date(html_text: str) -> str | None:
    m = _DATE.search(html_text)
    return parse_date(m.group(1)) if m else None


def fill_dates(client, cache: dict) -> int:
    """His author page has no dates: read datePublished from the page itself
    (date only, nothing else kept). Remembered per link in cache["dates"] so
    each page is read once."""
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


def feeds_for_run(cache: dict, now: dt.datetime) -> list[Feed]:
    """Every desk each run; `every_min` feeds when due; one insider per run
    (rotating); Google News feeds skipped while backed off after a 429."""
    last = cache.setdefault("feed_last", {})
    rot = cache.get("insider_rot", 0)
    names = list(INSIDERS)
    out = [f for f in FEEDS if not f.every_min or now - dt.datetime.fromisoformat(
        last.get(f.name, "2000-01-01T00:00:00+00:00")) >= dt.timedelta(minutes=f.every_min - 0.5)]
    out.append(insider_feed(names[rot % len(names)]))
    cache["insider_rot"] = rot + 1
    hold = cache.get("google_backoff_until")
    if hold and now < dt.datetime.fromisoformat(hold):
        out = [f for f in out if "news.google.com" not in f.url]
    return out


def fetch_all(client, cache: dict, now: dt.datetime) -> tuple[list[dict], dict]:
    """Fetch this run's feeds; one failing feed never blocks the others."""
    got, status = [], {}
    for f in feeds_for_run(cache, now):
        try:
            r = client.get(f.url, headers=f.headers)
            if r.status_code in (429, 503) and "news.google.com" in f.url:
                cache["google_backoff_until"] = (now + dt.timedelta(minutes=GOOGLE_BACKOFF_MIN)).isoformat()
            r.raise_for_status()
            items = f.parse(r.text)
            got += items
            status[f.name] = len(items)
            cache["feed_last"][f.name] = now.isoformat(timespec="seconds")
        except Exception as e:
            log.warning("feed %s failed: %s", f.name, e)
            status[f.name] = f"error: {type(e).__name__}"
    return got, status


def rebuild(cache: dict, tg: Tagger | None) -> None:
    """Everything after the fetch, pure code: tag, cluster, supersede."""
    tag_items(cache, tg)
    cache["items"] = {k: v for k, v in cache["items"].items() if v.get("source") != "AP" or v.get("teams")
                      or v.get("players") or FOOTBALL.search(f"{v['title']} {v.get('summary', '')}")}
    cluster(cache, tg)
    supersede(cache, tg)


def refresh(force: bool = False, now: dt.datetime | None = None) -> dict | None:
    import httpx
    now = now or dt.datetime.now(dt.timezone.utc)
    try:
        cache = json.loads(CACHE_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        cache = {"items": {}}
    if not force and not due(cache, now):
        return None
    with httpx.Client(timeout=20, follow_redirects=True) as client:
        new, status = fetch_all(client, cache, now)
        before = set(cache.get("items", {}))
        merge(cache, new, now)
        fill_dates(client, cache)
    prune(cache, now)
    live = {it["link"] for it in cache["items"].values()}
    cache["dates"] = {k: v for k, v in cache.get("dates", {}).items() if k in live}
    tg = tagger()
    rebuild(cache, tg)
    for it in cache["items"].values():
        if "teams" in it:
            it.pop("hint", None)
    cache["fetched_at"] = now.isoformat(timespec="seconds")
    # keep the last good count per feed so one failed fetch doesn't hide a source
    feeds = cache.get("feeds") or {}
    feeds.update({k: v for k, v in status.items() if isinstance(v, int) or not isinstance(feeds.get(k), int)})
    cache["feeds"] = feeds
    cache["last_run"] = status
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CACHE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(cache, indent=1, sort_keys=True))
    os.replace(tmp, CACHE_PATH)
    sts = stories(cache)
    summary = {"fetched_at": cache["fetched_at"], "feeds": status, "items": len(cache["items"]),
               "new": len(set(cache["items"]) - before), "stories": len(sts),
               "multi_outlet": sum(1 for s in sts if s["n_outlets"] > 1),
               "superseded": sum(1 for i in cache["items"].values() if i.get("superseded_by"))}
    log.info("refresh %s", summary)
    return summary


def main(argv: list[str]) -> int:
    sys.path.insert(0, str(HERE))  # cfbd / freshness when run with -I
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if argv[:1] == ["refresh"]:
        r = refresh(force="--force" in argv)
        print(json.dumps(r) if r else "not due")
        return 0
    if argv[:1] == ["lookup"]:
        q = " ".join(argv[1:]) or None
        print(f"as of {as_of()} | {sources_line()}")
        print("\n".join(breaking_for(q)) or "(no matching news)")
        return 0
    if argv[:1] == ["stories"]:
        now = dt.datetime.now(dt.timezone.utc)
        for s in sorted(stories(load_cache()), key=lambda s: s["time"], reverse=True)[:40]:
            print(f"[{s['n_outlets']}] {story_line(s, now)}")
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
