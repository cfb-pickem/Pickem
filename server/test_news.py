"""
News tests: feed parsing, dedupe/merge, 7-day pruning, tagging and the
news_for() lookup chat.py will use.

Run:  ./.venv/bin/python3 -I test_news.py

Offline: fixture feeds and a hand-built tagger; no network, no Ollama.
"""

from __future__ import annotations

import datetime as dt
import sys

sys.path.insert(0, ".")
import news  # noqa: E402

fails = 0


def check(name, cond, got=None):
    global fails
    print(("PASS " if cond else "FAIL ") + name + ("" if cond or got is None else f"\n     got: {got!r}"))
    fails += 0 if cond else 1


NOW = dt.datetime(2026, 11, 15, 3, 0, tzinfo=dt.timezone.utc)  # Nov 14, 9pm CT

GNEWS = """<?xml version="1.0"?><rss version="2.0"><channel>
<item><title>SEC's proposed cap policy would penalize big-spending schools $7 million this year - Yahoo Sports</title>
<link>https://news.google.com/rss/articles/AAA?oc=5</link><pubDate>Thu, 12 Nov 2026 16:16:05 GMT</pubDate>
<description>&lt;a href="x"&gt;SEC's proposed cap policy&lt;/a&gt;</description><source url="https://sports.yahoo.com">Yahoo Sports</source></item>
<item><title>College Football's Super League Debate - SwimSwam</title>
<link>https://news.google.com/rss/articles/BBB</link><pubDate>Mon, 09 Nov 2026 14:17:35 GMT</pubDate>
<source url="https://swimswam.com">SwimSwam</source></item>
</channel></rss>"""

AUTHOR = """<li><a data-ylk="cpos:1;ct:story;elm:hdln;slk:SEC&#x27;s proposed cap policy would penalize big-spending schools $7 million this year;tar_uri:/college-football/article/secs-cap-policy-123.html;itc:0;" href="#">
<h3 class="a"><div>SEC's proposed cap policy</div></h3><div class="b"><div class="c">The SEC is moving toward a spending cap. Schools over it would pay penalties. Subscribe wherever you listen.</div></div></a></li>
<li><a data-ylk="cpos:2;ct:video;elm:hdln;slk:Is Lane Kiffin embracing the villain role?;tar_uri:/video/kiffin-villain-456.html;itc:0;" href="#">
<h3><div>Kiffin</div></h3><div><div>Co-hosts discuss Lane Kiffin before LSU faces Texas.</div></div></a></li>
<li><a data-ylk="cpos:3;elm:img;slk:ignored image link;tar_uri:/video/kiffin-villain-456.html" href="#"></a></li>"""

CBS = """<?xml version="1.0"?><rss version="2.0"><channel>
<item><title> Kyle Whittingham buyout: How much it would cost Michigan </title>
<link> https://www.cbssports.com/college-football/news/whittingham-buyout/ </link>
<pubDate> Sun, 15 Nov 2026 00:00:25 +0000 </pubDate><description>Whittingham could be one-and-done in Ann Arbor</description></item>
<item><title>Texas Tech vs. Oklahoma State odds: Use Polymarket promo code CBSSPORTS for $25</title>
<link>https://www.cbssports.com/prediction/x</link><pubDate>Sun, 15 Nov 2026 01:01:32 +0000</pubDate><description>bonus</description></item>
<item><title>Old news from last month</title><link>https://www.cbssports.com/old</link>
<pubDate>Wed, 01 Oct 2026 12:00:00 +0000</pubDate><description>Stale.</description></item>
</channel></rss>"""

ESPN = {"articles": [
    {"headline": "QB Isaiah Marshall's career rushing day leads Kansas over West Virginia 37-14",
     "published": "2026-11-15T01:13:34Z",
     "description": "Isaiah Marshall rushed for 164 yards. Kansas beat West Virginia 37-14 on Saturday. "
                    "It was a big win. And a fourth sentence that should be cut.",
     "links": {"web": {"href": "https://www.espn.com/college-football/recap/1"}},
     "categories": [{"type": "team", "description": "Kansas Jayhawks"}]},
    {"headline": "Weber State Wildcats vs. Northern Arizona Lumberjacks: Full Highlights",
     "published": "2026-11-15T02:21:24Z", "links": {"web": {"href": "https://www.espn.com/v/2"}}},
    {"published": "2026-11-15T02:00:00Z"},  # no headline -> skipped
]}

TG = news.Tagger(
    teams={"Kansas": "Big 12", "West Virginia": "Big 12", "Marshall": "Sun Belt", "Arizona": "Big 12",
           "Northern Arizona": "Big Sky", "Weber State": "Big Sky", "Michigan": "Big Ten",
           "Texas": "SEC", "Texas Tech": "Big 12", "LSU": "SEC", "Oklahoma State": "Big 12"},
    players={"Isaiah Marshall": "Kansas"},
)

# --- parsing -----------------------------------------------------------------
g = news.parse_gnews_dellenger(GNEWS)
check("gnews keeps Yahoo Sports items only", len(g) == 1 and g[0]["dellenger"], g)
check("gnews strips the ' - Yahoo Sports' suffix",
      g[0]["title"] == "SEC's proposed cap policy would penalize big-spending schools $7 million this year", g[0]["title"])
check("gnews date parsed to ISO UTC", g[0]["date"] == "2026-11-12T16:16:05+00:00", g[0]["date"])

a = news.parse_author_page(AUTHOR)
check("author page: 2 headline links, image link ignored", len(a) == 2, a)
check("author page: real Yahoo link", a[0]["link"] == "https://sports.yahoo.com/college-football/article/secs-cap-policy-123.html", a[0])
check("author page: blurb summary, podcast boilerplate dropped",
      a[0]["summary"] == "The SEC is moving toward a spending cap. Schools over it would pay penalties.", a[0]["summary"])
check("author page: no date on the page", a[0]["date"] is None)

c = news.parse_rss(CBS, "CBS Sports")
check("rss: sponsored promo item skipped", all("promo" not in i["title"].lower() for i in c) and len(c) == 2, c)
check("rss: whitespace trimmed", c[0]["title"] == "Kyle Whittingham buyout: How much it would cost Michigan"
      and c[0]["link"].startswith("https://"), c[0])

e = news.parse_espn(ESPN)
check("espn: headline-less article skipped", len(e) == 2, e)
check("espn: summary keeps at most 3 sentences", e[0]["summary"].count(".") <= 4 and "fourth" not in e[0]["summary"],
      e[0]["summary"])
check("summary capped", len(news.short_summary("word " * 200)) <= news.SUMMARY_MAX + 3)
check("page datePublished read", news.page_date('x"datePublished":"2026-11-10T16:01:05Z","y') == "2026-11-10T16:01:05+00:00")

# --- merge / dedupe / prune ---------------------------------------------------
cache = news.merge({"items": {}}, g + a + c + e, NOW)
items = cache["items"]
sec = [i for i in items.values() if "cap policy" in i["title"]]
check("same headline from gnews + author page dedupes to one", len(sec) == 1, [i["title"] for i in items.values()])
check("merged item keeps the real link over news.google", sec[0]["link"].startswith("https://sports.yahoo.com/"), sec[0])
check("merged item keeps gnews date and author blurb",
      sec[0]["date"] == "2026-11-12T16:16:05+00:00" and "spending cap" in sec[0]["summary"], sec[0])
n = len(items)
news.merge(cache, c, NOW)
check("re-merging the same feed adds nothing", len(cache["items"]) == n)
news.prune(cache, NOW)
check("items older than 7 days dropped", all("Old news" not in i["title"] for i in cache["items"].values()))
check("undated item kept by first_seen", any("villain" in i["title"] for i in cache["items"].values()))

# --- tagging --------------------------------------------------------------------
news.tag_items(cache, TG)
k = next(i for i in cache["items"].values() if "Kansas over" in i["title"])
check("tags team + player + conference", k["teams"] == ["Kansas", "West Virginia"] and k["players"] == ["Isaiah Marshall"]
      and k["confs"] == ["Big 12"], k)
check("player name doesn't tag Marshall the team", "Marshall" not in k["teams"], k["teams"])
w = next(i for i in cache["items"].values() if "Weber" in i["title"])
check("'Northern Arizona' doesn't tag Arizona; non-FBS teams dropped", w["teams"] == [], w["teams"])
check("'Texas Tech' isn't tagged Texas", TG.tag("Texas Tech rolls")["teams"] == ["Texas Tech"])
check("conference word tags conference", "SEC" in TG.tag("SEC's proposed cap policy")["confs"])

# --- lookup ---------------------------------------------------------------------
L = news.news_for("any Kansas news?", cache=cache, tg=TG)
check("team question returns that team's item, dated", L and L[0].startswith("Nov 14, ESPN: QB Isaiah Marshall"), L)
check("team question returns nothing off-topic", all("Kansas" in x for x in L), L)
check("team with no news -> empty, never filler", news.news_for("Oklahoma State news", cache=cache, tg=TG) == [])
L = news.news_for("what's the SEC cap policy thing", cache=cache, tg=TG)
check("Dellenger item labeled and first", L and L[0].startswith("Nov 12, Dellenger: SEC's proposed cap policy"), L)
L = news.news_for("Whittingham", cache=cache, tg=TG)
check("keyword match without a team", L and "Whittingham" in L[0], L)
L = news.news_for("what's going on", cache=cache, tg=TG, limit=2)
check("generic question -> latest, Dellenger first", len(L) == 2 and all("Dellenger" in x for x in L), L)
check("teams= argument works", news.news_for(teams=["Michigan"], cache=cache, tg=TG)[0].startswith("Nov 14, CBS Sports"))
check("limit respected", len(news.news_for(None, cache=cache, tg=TG, limit=1)) == 1)
check("empty cache -> []", news.news_for("Kansas", cache={"items": {}}, tg=TG) == [])
check("no tagger still works", isinstance(news.news_for("Kansas", cache=cache, tg=None), list))
check("as_of is the newest item's day", news.as_of(cache) == "Nov 14", news.as_of(cache))
line = news.news_line({"title": "Same", "summary": "Same.", "source": "ESPN", "date": "2026-11-14T18:00:00+00:00"})
check("summary that just repeats the headline is omitted", line == "Nov 14, ESPN: Same", line)

# --- cadence ------------------------------------------------------------------------
sat_game = dt.datetime(2026, 11, 14, 20, 0, tzinfo=dt.timezone.utc)  # Sat 2pm CT
check("never fetched -> due", news.due({}, NOW))
check("Saturday game window: due after 15 min",
      news.due({"fetched_at": (sat_game - dt.timedelta(minutes=15)).isoformat()}, sat_game))
tue = dt.datetime(2026, 11, 17, 20, 0, tzinfo=dt.timezone.utc)
check("weekday: not due after 15 min", not news.due({"fetched_at": (tue - dt.timedelta(minutes=15)).isoformat()}, tue))
check("weekday: due after 30 min", news.due({"fetched_at": (tue - dt.timedelta(minutes=30)).isoformat()}, tue))

# --- Ollama condense (fake client: never touches the real model) ----------------------
class _R:
    def __init__(self, d): self.d = d
    def json(self): return self.d
    def raise_for_status(self): pass


class _C:
    def __init__(self, health): self.health, self.sent = health, []
    def get(self, url, **kw): return _R(self.health)
    def post(self, url, json=None, **kw):
        self.sent.append(json)
        return _R({"response": "Kansas won 37-14. Marshall ran for 164 yards."})


busy = _C({"gate": {"members_active": 1, "test_busy": False}})
idle = _C({"gate": {"members_active": 0, "test_busy": False}})
check("member chatting -> chat not idle", not news._chat_idle(busy))
check("no members -> idle", news._chat_idle(idle))
s2 = news._condense(idle, {"title": "Kansas wins"}, "long blurb " * 60)
body = idle.sent[-1]
check("condense uses chat's model + num_ctx, no thinking (no reload/evict of a different model)",
      body["model"] == news.MODEL and body["options"]["num_ctx"] == 16384 and body["think"] is False, body)
check("condense output is a short summary", s2 == "Kansas won 37-14. Marshall ran for 164 yards.", s2)
check("long blurb kept in memory only for condensing",
      "_desc" in news._item("t", "l", None, "x " * 300, "ESPN") and "_desc" not in news._item("t", "l", None, "short", "ESPN"))

print(f"\n{'ALL PASS' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
