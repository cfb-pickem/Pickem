"""
News tests: feed parsing, dedupe/merge, 7-day pruning, tagging and the
news_for() lookup chat.py will use.

Run:  ./.venv/bin/python3 -I test_news.py

Offline: fixture feeds and a hand-built tagger; no network, no LLM (the pipeline has none).
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
    {"headline": "Florida Gators vs. Kentucky Wildcats: Full Highlights", "type": "Media",
     "published": "2026-11-15T02:30:00Z"},
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
check("espn: headline-less article and video clip skipped", len(e) == 2, e)
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
check("topic nobody covered -> empty, never filler", news.news_for("who got fired", cache=cache, tg=TG) == [])
check("plural/singular match ('penalties' -> 'penalty')", news._stem("penalties") == "penalt"
      and news.news_for("any penalties?", cache=cache, tg=TG)[0].startswith("Nov 12, Dellenger"))
L = news.news_for("what's going on", cache=cache, tg=TG, limit=3)
check("generic question -> his newest item first, then newest FBS news",
      len(L) == 3 and "Dellenger" in L[0] and "Dellenger" not in L[1] + L[2] and "Weber" not in " ".join(L), L)
check("teams= argument works", news.news_for(teams=["Michigan"], cache=cache, tg=TG)[0].startswith("Nov 14, CBS Sports"))
check("limit respected", len(news.news_for(None, cache=cache, tg=TG, limit=1)) == 1)
check("empty cache -> []", news.news_for("Kansas", cache={"items": {}}, tg=TG) == [])
check("no tagger still works", isinstance(news.news_for("Kansas", cache=cache, tg=None), list))
check("as_of is the newest item's day", news.as_of(cache) == "Nov 14", news.as_of(cache))
line = news.news_line({"title": "Same", "summary": "Same.", "source": "ESPN", "date": "2026-11-14T18:00:00+00:00"})
check("summary that just repeats the headline is omitted", line == "Nov 14, ESPN: Same", line)

# --- cadence ------------------------------------------------------------------------
import os, json, tempfile  # noqa: E402
from pathlib import Path  # noqa: E402
os.environ["PICKY_LIVE_CACHE"] = "/nonexistent/live_games.json"
news.LIVE_CACHE = Path(os.environ["PICKY_LIVE_CACHE"])
sat_game = dt.datetime(2026, 11, 14, 20, 0, tzinfo=dt.timezone.utc)  # Sat 2pm CT
check("never fetched -> due", news.due({}, NOW))
check("game day: due after 5 min", news.due({"fetched_at": (sat_game - dt.timedelta(minutes=5)).isoformat()}, sat_game))
check("game day: not due after 3 min",
      not news.due({"fetched_at": (sat_game - dt.timedelta(minutes=3)).isoformat()}, sat_game))
tue = dt.datetime(2026, 11, 17, 20, 0, tzinfo=dt.timezone.utc)
check("weekday: not due after 10 min", not news.due({"fetched_at": (tue - dt.timedelta(minutes=10)).isoformat()}, tue))
check("weekday: due after 15 min", news.due({"fetched_at": (tue - dt.timedelta(minutes=15)).isoformat()}, tue))
with tempfile.TemporaryDirectory() as d:
    lf = Path(d) / "live.json"
    lf.write_text(json.dumps({"games": {"1": {"state": "pre", "start": "2026-11-17T22:00Z"}}}))
    check("weekday MACtion kicking off within 3h -> live window (5-min cadence)", news.live_window(tue, lf))
    lf.write_text(json.dumps({"games": {"1": {"state": "post", "start": "2026-11-17T01:00Z"}}}))
    check("finished game -> no live window", not news.live_window(tue, lf))

# --- feeds per run: insider rotation, google backoff ---------------------------------
c0 = {}
f1 = [f.name for f in news.feeds_for_run(c0, tue)]
f2 = [f.name for f in news.feeds_for_run(c0, tue)]
check("every desk each run + one insider, rotating", "espn_rss" in f1 and "ap" in f1
      and [x for x in f1 if x.startswith("insider:")] != [x for x in f2 if x.startswith("insider:")], (f1, f2))
c0["google_backoff_until"] = (tue + dt.timedelta(minutes=30)).isoformat()
f3 = [f.name for f in news.feeds_for_run(c0, tue)]
check("google feeds skipped while backed off", "ap" not in f3 and not any(x.startswith("insider:") for x in f3)
      and "yahoo" in f3, f3)
c0["feed_last"]["dellenger_author"] = tue.isoformat()
check("30-min feed not refetched 5 min later",
      "dellenger_author" not in [f.name for f in news.feeds_for_run(c0, tue + dt.timedelta(minutes=5))])

# --- new parsers: bylines, publishers, AP, insiders -----------------------------------
DC = 'xmlns:dc="http://purl.org/dc/elements/1.1/"'
CBS2 = f"""<?xml version="1.0"?><rss version="2.0" {DC}><channel>
<item><title>Sources: Florida State targeting Lane Kiffin's OC for head coach job</title><link>https://www.cbssports.com/a</link>
<pubDate>Tue, 17 Nov 2026 18:00:00 +0000</pubDate><description>Florida State is zeroing in on its next coach.</description>
<dc:creator>Matt Zenitz</dc:creator></item>
<item><title>Kentucky hosts Florida in SEC matchup</title><link>https://www.cbssports.com/p</link>
<pubDate>Tue, 17 Nov 2026 18:00:00 +0000</pubDate><description>Florida (4-6) at Kentucky (5-5), Nov. 21 at noon.</description></item>
<item><title>Duke basketball rolls past Army</title><link>https://www.cbssports.com/b</link>
<pubDate>Tue, 17 Nov 2026 18:00:00 +0000</pubDate></item>
</channel></rss>"""
z = news.parse_rss(CBS2, "CBS Sports")
check("rss: dc:creator byline -> insider credit (Zenitz on CBS)", len(z) == 1 and z[0]["author"] == "Matt Zenitz"
      and z[0]["insider"] == "Matt Zenitz", z)
check("rss: auto game preview and other sports dropped", len(z) == 1, [i["title"] for i in z])
check("credit reads 'CBS Sports' Matt Zenitz'", news.credit(z[0]) == "CBS Sports' Matt Zenitz", news.credit(z[0]))
YH = f"""<?xml version="1.0"?><rss version="2.0" {DC} xmlns:y="http://y"><channel>
<item><title>Oregon's Raiola named Big Ten player of the week</title><link>https://sports.yahoo.com/x</link>
<pubDate>Tue, 17 Nov 2026 17:00:00 +0000</pubDate><description>Big day.</description><y:publisher>Ducks Wire</y:publisher></item>
<item><title>Dellenger: inside the SEC cap vote</title><link>https://sports.yahoo.com/y</link>
<pubDate>Tue, 17 Nov 2026 16:00:00 +0000</pubDate><description>The vote.</description><dc:creator>Ross Dellenger</dc:creator>
<y:publisher>Yahoo Sports</y:publisher></item></channel></rss>"""
y = news.parse_rss(YH, "Yahoo Sports")
check("yahoo: syndicated item labeled by its publisher", y[0]["source"] == "Ducks Wire", y[0])
check("yahoo: Dellenger byline -> dellenger + insider", y[1]["dellenger"] and y[1]["insider"] == "Ross Dellenger", y[1])
GN_AP = """<?xml version="1.0"?><rss version="2.0"><channel>
<item><title>UTEP fires coach Scotty Walden after 8th straight loss - AP News</title><link>https://news.google.com/a</link>
<pubDate>Mon, 16 Nov 2026 16:00:00 GMT</pubDate><source url="https://apnews.com">AP News</source></item>
<item><title>Some blog repost - Blog</title><link>https://news.google.com/b</link>
<pubDate>Mon, 16 Nov 2026 16:00:00 GMT</pubDate><source url="https://b.com">Blog</source></item></channel></rss>"""
ap = news.parse_gnews(GN_AP, ("AP News",), "AP")
check("google news: source filter + suffix stripped + label", len(ap) == 1 and ap[0]["source"] == "AP"
      and ap[0]["title"] == "UTEP fires coach Scotty Walden after 8th straight loss", ap)
GN_T = GN_AP.replace("AP News", "ESPN")
th = news.parse_gnews(GN_T, news.INSIDERS["Pete Thamel"], "ESPN", insider="Pete Thamel")
check("insider search: kept only from his outlet, credited", len(th) == 1 and news.credit(th[0]) == "ESPN's Pete Thamel", th)

# --- classify: topics, status, reported vs confirmed -------------------------------------
C = news.classify
check("injury out", C("Texas DL Hero Kanu undergoes season-ending surgery on arm")["status"] == "out")
check("injury questionable", C("Arch Manning questionable vs. Arkansas with ankle injury")["status"] == "questionable")
check("injury available", C("Arch Manning cleared to play Saturday after ankle injury")["status"] == "available")
check("'will miss the playoff' is not an injury", C("LSU will miss the College Football Playoff")["kind"] is None)
check("'likely to miss rest of season' = out + reported",
      C("Michigan WR JJ Buchanan likely to miss rest of season with shoulder injury") ==
      {"topics": ["injury"], "kind": "injury", "status": "out", "label": "reported"},
      C("Michigan WR JJ Buchanan likely to miss rest of season with shoulder injury"))
check("sources -> reported", C("Sources: Florida State targeting Kiffin's OC")["label"] == "reported")
check("fires -> confirmed + coaching", C("UTEP fires coach Scotty Walden")["label"] == "confirmed"
      and "coaching" in C("UTEP fires coach Scotty Walden")["topics"])
# cases from the first offline QA run (2026-11-16)
mi = C("Michigan WR JJ Buchanan likely to miss rest of season with shoulder injury. Michigan is likely to be without "
       "its leading receiver, head coach Kyle Whittingham said Monday.")
check("QA: 'likely to miss' + coach said -> confirmed, injury not coaching",
      mi["label"] == "confirmed" and "coaching" not in mi["topics"] and mi["status"] == "out", mi)
check("QA: 'according to On3's Pete Nakos' -> reported",
      C("UTEP fires head coach Scotty Walden. UTEP fired Walden, according to On3's Pete Nakos.")["label"] == "reported")
check("'could' / 'may' alone don't make a report", C("Could Indiana vs. USC decide a playoff spot?")["label"] is None)

# --- cluster / supersede / breaking rider ---------------------------------------------------
T0 = dt.datetime(2026, 11, 17, 18, 0, tzinfo=dt.timezone.utc)  # Tue noon CT (not a game day)


def it(title, src, hours_ago, teams=(), players=(), summary="", insider=None):
    x = news._item(title, "https://x/" + news.key_for(title)[:20], (T0 - dt.timedelta(hours=hours_ago)).isoformat(),
                   summary, src, insider=insider)
    x.update({"teams": list(teams), "players": list(players), "confs": []})
    return x


TG2 = news.Tagger(teams={"UTEP": "Conference USA", "Texas": "SEC", "Clemson": "ACC", "Tennessee": "SEC",
                          "Arkansas": "SEC", "Iowa": "Big Ten", "Rice": "American Athletic"},
                  players={"Arch Manning": "Texas", "Tait Reynolds": "Clemson"})
feed = [
    it("UTEP fires head coach Scotty Walden", "On3", 6, ["UTEP"], summary="UTEP fired Walden Monday."),
    it("UTEP fires coach Scotty Walden after 8th straight loss", "AP", 5.5, ["UTEP"]),
    it("UTEP fires Scotty Walden: Miners coach went 6-28", "CBS Sports", 5, ["UTEP"]),
    it("Texas DL Hero Kanu undergoes season-ending surgery", "ESPN", 1, ["Texas"]),
    it("Arch Manning questionable vs. Arkansas with ankle injury", "ESPN", 30, ["Texas", "Arkansas"], ["Arch Manning"]),
    it("Arch Manning out vs. Arkansas, Texas rules him out with ankle injury", "Yahoo Sports", 2, ["Texas", "Arkansas"],
       ["Arch Manning"], insider="Pete Thamel"),
    it("Sources: Clemson QB Tait Reynolds expected to transfer", "ESPN", 20, ["Clemson"], ["Tait Reynolds"],
       insider="Pete Thamel"),
    it("Clemson QB Tait Reynolds announces he will transfer", "CBS Sports", 3, ["Clemson"], ["Tait Reynolds"]),
    it("Iowa vs Rice Prediction, Pick, NCAAF Odds for Saturday", "Action Network", 4, ["Iowa", "Rice"]),
    it("Tennessee vs Arkansas Prediction, Pick, NCAAF Odds for Saturday", "Action Network", 4, ["Tennessee", "Arkansas"]),
    it("Tennessee hires new defensive coordinator", "ESPN", 60, ["Tennessee"]),
]
for x in feed:
    x["pubDate"] = None
gm = it("NC State hires Jatavis Sanders as GM under Philip Rivers", "ESPN", 4, ["NC State"])
tr = it("College football coaching carousel 2026: Tracking moves as Philip Rivers takes over", "CBS Sports", 4,
        ["NC State", "Florida State"])
for x in (gm, tr):
    x.update(C(x["title"]))
check("QA: one shared last name (Rivers) doesn't merge a tracker into the GM hire", not news.same_story(gm, tr))
cache2 = news.merge({"items": {}}, feed, T0)
for x in cache2["items"].values():
    x.update(news.classify(f"{x['title']}. {x.get('summary', '')}"))
news.cluster(cache2, TG2)
news.supersede(cache2, TG2)
sts = news.stories(cache2)
utep = [s for s in sts if "UTEP" in s["teams"]]
check("same story from 3 outlets = 1 story with 3 sources", len(utep) == 1 and utep[0]["n_outlets"] == 3, [s["outlets"] for s in utep])
check("Kanu and Manning stories stay separate (same team, different people)",
      len([s for s in sts if "Texas" in s["teams"]]) == 2, [s["lead"]["title"] for s in sts if "Texas" in s["teams"]])
check("two different games' prediction pieces don't merge",
      len([s for s in sts if "betting" in s["topics"]]) == 2)
q_item = next(x for x in cache2["items"].values() if "questionable" in x["title"])
check("questionable superseded by the newer 'out'", bool(q_item.get("superseded_by")), q_item)
r_item = next(x for x in cache2["items"].values() if x["title"].startswith("Sources: Clemson"))
check("report superseded by the newer confirmation", bool(r_item.get("superseded_by")), r_item)

L = news.breaking_for("any news on Arch Manning", cache=cache2, tg=TG2, now=T0)
check("named player: newest status only, superseded 'questionable' gone",
      len(L) == 1 and "[status: out]" in L[0] and "questionable" not in L[0], L)
check("insider credit + hours label", L and L[0].startswith("2h ago, per Yahoo Sports' Pete Thamel"), L)
L = news.breaking_for("what's the latest", cache=cache2, tg=TG2, now=T0)
check("generic: newest first", L and L[0].startswith("1h ago") and all("Prediction" not in x for x in L), L)
check("generic: multi-outlet story credited with its other outlets",
      any("UTEP fires" in x and "(also " in x for x in L), L)
check("generic: 60h-old item never shown as breaking",
      all("defensive coordinator" not in x for x in L) or any("older, not breaking" in x for x in L), L)
L = news.breaking_for("Tennessee news", cache=cache2, tg=TG2, now=T0)
check("old item labeled 'older, not breaking' (never 'ago')",
      any("Tennessee hires" in x and "(older, not breaking)" in x for x in L), L)
L = news.breaking_for("is Clemson's QB playing", cache=cache2, tg=TG2, now=T0)
check("availability ask with no injury item -> [] (no transfer filler)", L == [], L)
L = news.breaking_for("is Texas's QB playing", cache=cache2, tg=TG2, now=T0)
check("QB availability: QB items only (no DL surgery)", all("Kanu" not in x for x in L), L)
check("superseded never in news_for either",
      all("questionable" not in x for x in news.news_for("Arch Manning", cache=cache2, tg=TG2, limit=5)))
check("reported label shown", any("[REPORTED" in news.story_line(s, T0) for s in
                                  news.stories(cache2, include_superseded=True) if s["label"] == "reported"))
check("lede: first sentence, not the headline again",
      news.lede({"title": "A", "summary": "No. 3 Texas won. More."}) == "No. 3 Texas won.")

# --- daily offline QA (fake Opus / fake repair: no Anthropic in tests) --------------------------
import news_qa  # noqa: E402
news_qa.LOG = Path(tempfile.mkdtemp()) / "qa.log"
rows = news_qa.sample(cache2)
check("qa sample: stories with derived fields + merged/superseded", rows and {"teams", "status", "label", "merged",
      "superseded"} <= set(rows[0]) and any(r["superseded"] for r in rows), rows[:2])
fixed = []
r = news_qa.run(opus=lambda p: {"errors": []}, fixer=fixed.append, cache=cache2)
check("qa: clean verdict -> ok, no repair", r["result"] == "ok" and not fixed, r)
r = news_qa.run(opus=lambda p: {"errors": [{"id": i, "field": "status", "why": "x"} for i in range(len(rows))]},
                fixer=lambda w: fixed.append(w) or {"result": "rejected", "branch": "b"}, cache=cache2)
check("qa: many wrong -> repair branch requested with examples", r["result"] == "needs fix" and fixed
      and "status" in fixed[0] and r["repair"]["branch"] == "b", r)
def _down(p):
    raise RuntimeError("anthropic down")
r = news_qa.run(opus=_down, fixer=fixed.append, cache=cache2)
check("qa: Anthropic down -> skipped, nothing else happens", r["result"] == "skipped")

# --- chat rider: triggers on news asks, quiet otherwise ----------------------------------------
try:
    import chat
    d = tempfile.mkdtemp()
    f = Path(d) / "news.json"
    cache2["fetched_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    now_shift = dt.datetime.now(dt.timezone.utc) - T0
    for x in cache2["items"].values():  # same ages relative to the real clock
        x["date"] = (dt.datetime.fromisoformat(x["date"]) + now_shift).isoformat()
    f.write_text(json.dumps(cache2))
    news.CACHE_PATH, news._mem, news._tagger = f, None, TG2
    n = chat.news_note("what's the latest")
    check("rider: 'what's the latest' -> NEWS QUESTION with timed, attributed lines",
          n.startswith("NEWS QUESTION") and " ago, per " in n and "newest first" in n, n[:300])
    n = chat.news_note("any news on Arch Manning")
    check("rider: 'any news on Arch Manning' -> his newest status", "[status: out]" in n and "questionable" not in n, n[:400])
    n = chat.news_note("is Texas's QB playing this week")
    check("rider: 'is Texas's QB playing' triggers", n.startswith("NEWS QUESTION"), n[:200])
    check("rider: status lines carry the rule-7 exception",
          "exception to rule 7" in chat.news_note("any news on Arch Manning"))
    for quiet in ("who's winning the pool?", "lol you suck picky", "what's my record", "take Iowa or Rice?"):
        check(f"rider quiet on non-news: {quiet!r}", chat.news_note(quiet) == "", chat.news_note(quiet)[:120])
except ImportError as e:
    check(f"chat importable ({e})", False)

print(f"\n{'ALL PASS' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
