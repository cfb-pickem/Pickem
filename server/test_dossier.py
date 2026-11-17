"""
Deterministic tests for dossier.py: what's due when, the validator, the
code-first research path (runs with PICKY_NO_ANTHROPIC=1), the weekly Opus
layer and keep-the-last-good-dossier. No network, no claude, no GPU (research
and HTTP are faked).

Run:  ./.venv/bin/python3 -I test_dossier.py
"""

from __future__ import annotations

import datetime as dt
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, ".")
import httpx  # noqa: E402

import dossier  # noqa: E402

FAILED = []
CT = dossier.CT


def check(name, ok, got=None):
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else f"\n     got: {got!r}"))
    if not ok:
        FAILED.append(name)


def at(s):
    return dt.datetime.fromisoformat(s).replace(tzinfo=CT)


G = {"GameId": 1, "Away": "Virginia Tech", "Home": "Miami", "line": -17.0, "Status": None,
     "Start (CT)": "2026-11-21T14:30:00", "tiebreaker": False}

# -- due ------------------------------------------------------------------------------
wed = at("2026-11-18T10:00:00")
check("no dossier yet -> new", dossier.due_reason(G, None, None, wed) == "new")
fresh = {"generated_at": at("2026-11-18T09:00:00").isoformat()}
check("fresh dossier midweek -> nothing", dossier.due_reason(G, fresh, None, wed) is None)
check("Friday 5 PM -> fri", dossier.due_reason(G, fresh, None, at("2026-11-20T17:30:00")) == "fri")
fri_done = {"generated_at": at("2026-11-20T17:35:00").isoformat()}
check("fri already done -> nothing Friday night", dossier.due_reason(G, fri_done, None, at("2026-11-20T23:00:00")) is None)
check("Saturday 7 AM -> sat", dossier.due_reason(G, fri_done, None, at("2026-11-21T07:10:00")) == "sat")
check("kicked off -> never", dossier.due_reason(G, None, None, at("2026-11-21T14:31:00")) is None)
check("final -> never", dossier.due_reason(dict(G, Status="final"), None, None, wed) is None)
st = {"failed_at": wed.timestamp() - 600}
check("failure backs off", dossier.due_reason(G, None, st, wed) is None)
check("backoff expires", dossier.due_reason(G, None, {"failed_at": wed.timestamp() - 3 * 3600}, wed) == "new")
thu = dict(G, **{"Start (CT)": "2026-11-19T19:00:00"})
check("Thursday game: no Friday refresh after kickoff",
      dossier.due_reason(thu, fresh, None, at("2026-11-20T17:30:00")) is None)

# -- validator ------------------------------------------------------------------------
now = at("2026-11-18T10:00:00")


def claim(**kw):
    c = {"text": "Miami QB Carson Beck is probable after leaving last week's game.", "kind": "injury",
         "team": "home", "players": ["Carson Beck"], "source_url": "https://www.example-news.com/miami/beck-probable",
         "date": "2026-11-17", "confidence": "medium"}
    c.update(kw)
    return c


raw = {"claims": [
    claim(),
    claim(text="Duplicate: Miami QB Carson Beck is probable after leaving last week's game."[11:]),  # dup text
    claim(text="Virginia Tech RB is out for the season with a torn ACL.", source_url=""),
    claim(text="Tech's top WR returns this week per coach.", source_url="https://247sports.com/article/x"),
    claim(text="Old news about a 2025 transfer that no longer matters.", date="2026-09-01"),
    claim(text="Future dated claim about the game itself.", date="2026-12-25"),
    claim(text="Reddit says the Miami starting QB looked hurt at practice.", kind="fact",
          source_url="https://www.reddit.com/r/CFB/comments/abc/miami_qb/", confidence="high"),
    claim(text="Beat writer thinks Virginia Tech's defense matches up well here.", kind="insider_opinion",
          team="away", players=[]),
    claim(text="Has a link www.example.com in the text which is not allowed.", source_url="https://a.com/b"),
    claim(text="Homepage-only source is not a source at all, really.", source_url="https://www.espn.com/"),
    claim(text="A dead link claim about the Hokies kicker situation.", source_url="https://dead.example.org/x"),
    claim(text="Bad kind value on this one for sure, should drop.", kind="gossip"),
]}
secs, dropped = dossier.validate(raw, now, {"https://dead.example.org/x": "dead"})
facts, ops, rum = secs["facts"], secs["opinions"], secs["rumors"]
check("one fact survives", len(facts) == 1 and facts[0]["players"] == ["Carson Beck"], facts)
check("reddit 'fact' demoted to a low-confidence rumor, not in facts",
      len(rum) == 1 and rum[0]["kind"] == "rumor" and rum[0]["confidence"] == "low"
      and not any("reddit" in c["source_url"] for c in facts), rum)
check("insider opinion in its own section", len(ops) == 1 and ops[0]["kind"] == "insider_opinion", ops)
reasons = " | ".join(dropped)
for r in ("no usable source URL", "paid source", "stale", "future date", "URL in text", "dead URL",
          "bad kind", "duplicate"):
    check(f"dropped: {r}", r in reasons, reasons)
check("homepage URL is not a source", "Homepage-only" in reasons, reasons)
try:
    dossier.validate({"oops": 1}, now)
    check("missing claims list raises", False)
except ValueError:
    check("missing claims list raises", True)
many = {"claims": [claim(text=f"Distinct claim number {i} about Miami depth chart news.") for i in range(30)]}
s2, _ = dossier.validate(many, now)
check("capped", sum(len(v) for v in s2.values()) == dossier.MAX_CLAIMS)
check("paid path (The Athletic on nytimes)", dossier._paid("https://www.nytimes.com/athletic/123/x/"))
check("free nytimes page not paid", not dossier._paid("https://www.nytimes.com/2026/11/17/sports/x.html"))
check("forum detection", dossier._forum("https://old.reddit.com/r/x") and dossier._forum("https://x.com/a/status/1")
      and not dossier._forum("https://www.miamiherald.com/sports/x.html"))

check("injury status inferred when missing", facts[0]["status"] == "probable", facts[0])
check("status kept when given", dossier.validate({"claims": [claim(status="out")]}, now)[0]["facts"][0]["status"] == "out")
check("infer: surgery/missed = out", dossier.infer_status("He had surgery and missed the game.") == "out")
check("infer: no update = questionable", dossier.infer_status("Left in the third quarter; no update.") == "questionable")

# -- summary + code research ---------------------------------------------------------------
check("rule summary prefers injuries", dossier.rule_summary(secs).startswith("Miami QB Carson Beck"))
check("rule summary with nothing", "No verified" in dossier.rule_summary({"facts": [], "opinions": [], "rumors": []}))

NEWS = [
    {"title": "Miami QB Carson Beck questionable with a shoulder injury", "date": "2026-11-17T12:00:00+00:00",
     "link": "https://www.miamiherald.com/sports/college/beck-shoulder", "source": "Miami Herald", "teams": ["Miami"],
     "players": ["Carson Beck"], "kind": "injury", "topics": ["injury"], "status": "questionable", "summary": ""},
    {"title": "Virginia Tech names a new defensive coordinator", "date": "2026-11-16T12:00:00+00:00",
     "link": "https://www.espn.com/college-football/story/_/id/1/vt-dc", "source": "ESPN",
     "teams": ["Virginia Tech"], "players": [], "kind": None, "topics": ["coaching"], "status": None, "summary": ""},
    {"title": "Miami's secret plan for the Hokies, inside", "date": "2026-11-17T12:00:00+00:00",
     "link": "https://www.on3.com/teams/miami/news/secret-plan/", "source": "On3", "teams": ["Miami"],
     "players": [], "kind": None, "topics": [], "status": None, "summary": "This is premium content."},
    {"title": "Ohio State beats Michigan in a rout", "date": "2026-11-17T12:00:00+00:00",
     "link": "https://www.espn.com/college-football/story/_/id/2/osu", "source": "ESPN",
     "teams": ["Ohio State", "Michigan"], "players": [], "kind": None, "topics": [], "status": None, "summary": ""},
]
leads = dossier.news_leads("Virginia Tech", "Miami", items=NEWS)
check("leads: only items about either team", len(leads) == 3 and not any("Ohio State" in x["title"] for x in leads),
      [x["title"] for x in leads])
cs, cdrop = dossier.validate(dossier.code_research(G, leads, [{"title": "Miami QB looked hurt at practice?",
                                                                "url": "https://www.reddit.com/r/CFB/comments/x/y/",
                                                                "date": "2026-11-17"}]), now)
check("code claims: headline as the text, status from news.py, side from the team tags",
      [c["text"] for c in cs["facts"]] == [NEWS[0]["title"], NEWS[1]["title"]]
      and cs["facts"][0]["status"] == "questionable" and cs["facts"][0]["team"] == "home"
      and cs["facts"][1]["team"] == "away", cs["facts"])
check("code claims: paid outlet dropped, reddit only a rumor",
      any("paid source" in x for x in cdrop) and len(cs["rumors"]) == 1 and cs["rumors"][0]["confidence"] == "low",
      (cdrop, cs["rumors"]))

# merge: a newer code injury report on the same player replaces Opus's older one
opus_layer = {"generated_at": now.isoformat(), "facts": [
    dict(claim(date="2026-11-15"), by="opus"),
    dict(claim(text="Miami's kicker is out with a groin injury, per the coach.", players=["Andres Borregales"],
               date="2026-11-16")),
    dict(claim(text="An old Opus note that has aged past the dossier window.", players=[], date="2026-09-01"))],
    "opinions": [], "rumors": []}
m = dossier.merge(cs, opus_layer, now)
texts = [c["text"] for c in m["facts"]]
check("merge: newer code injury on Beck beats the older Opus one", NEWS[0]["title"] in texts
      and claim()["text"] not in texts, texts)
check("merge: other Opus facts stay (tagged), stale ones go",
      any(c["by"] == "opus" and "kicker" in c["text"] for c in m["facts"]) and not any("aged past" in t for t in texts),
      m["facts"])

# -- weekly Opus schedule ----------------------------------------------------------------------
fri = at("2026-11-20T17:30:00")
check("opus: due at the Friday refresh", dossier.opus_due(None, "fri", fri))
check("opus: never midweek", not dossier.opus_due(None, "new", wed))
check("opus: Friday-night new game gets it", dossier.opus_due(None, "new", at("2026-11-20T21:00:00")))
check("opus: not again while this week's layer is fresh",
      not dossier.opus_due({"opus": {"generated_at": fri.isoformat()}}, "sat", at("2026-11-21T07:10:00")))
check("opus: Saturday retry when Friday failed", dossier.opus_due({}, "sat", at("2026-11-21T07:10:00")))
import os  # noqa: E402
os.environ["PICKY_NO_ANTHROPIC"] = "1"
check("opus: never with the kill switch on", not dossier.opus_due(None, "fri", fri))
del os.environ["PICKY_NO_ANTHROPIC"]

# -- build/run: code every run, Opus weekly, keep the last good dossier -------------------------
ok_transport = httpx.MockTransport(lambda req: httpx.Response(200, text="<feed xmlns='http://www.w3.org/2005/Atom'/>"))


class FakeDB:
    def get(self, q):
        if "select=cfb_season" in q:
            return [{"cfb_season": 2026}]
        if "select=week&" in q:
            return [{"week": 12}]
        return [G]


CALLS = []


def research(prompt):
    CALLS.append(prompt)
    return {"claims": [claim(text="Virginia Tech's starting center is doubtful, per the team's availability report.",
                             team="away", players=["Kaden Moore"], status="doubtful")], "notes": "opus"}


def boom(prompt):
    CALLS.append(prompt)
    raise RuntimeError("claude exit 1")


with tempfile.TemporaryDirectory() as d:
    base = Path(d)
    dossier._reddit_cache = ("ok", [])
    real_client, real_items = dossier.httpx.Client, dossier.news_items
    dossier.httpx.Client = lambda **kw: real_client(transport=ok_transport, **kw)
    dossier.news_items = lambda: NEWS
    try:
        r = dossier.run(base=base, db=FakeDB(), now=wed, research=boom)
        p = dossier.path_for(12, 1, base)
        j = json.loads(p.read_text())
        check("midweek run builds from code, no Opus call", r["built"] == ["1"] and not CALLS
              and len(j["facts"]) == 2 and j["trigger"] == "new" and "opus" not in j
              and j["researcher"].startswith("code"), (r, CALLS, j.get("researcher")))
        check("no member data in the prompt", "Virginia Tech" in dossier.build_prompt(G, 2026, 12, [], wed))
        r = dossier.run(base=base, db=FakeDB(), now=wed, research=boom)
        check("second run: nothing due (idempotent)", r.get("built") == 0, r)

        os.environ["PICKY_NO_ANTHROPIC"] = "1"
        r = dossier.run(base=base, db=FakeDB(), now=fri, research=boom)
        jk = json.loads(p.read_text())
        check("PICKY_NO_ANTHROPIC=1 at the Friday refresh: code dossier, no Opus call",
              r["built"] == ["1"] and not CALLS and jk["trigger"] == "fri" and "opus" not in jk
              and len(jk["facts"]) == 2, (r, CALLS))
        del os.environ["PICKY_NO_ANTHROPIC"]

        r = dossier.run(base=base, db=FakeDB(), now=fri, force=True, opus=True, research=research)
        jo = json.loads(p.read_text())
        check("on demand (--opus): one Opus call adds the layer on top of the code claims",
              len(CALLS) == 1 and jo["opus"]["facts"] and any(c["by"] == "opus" for c in jo["facts"])
              and any(c["by"] == "code" for c in jo["facts"]), (CALLS, jo.get("researcher")))
        r = dossier.run(base=base, db=FakeDB(), now=at("2026-11-21T07:10:00"), research=research)
        js = json.loads(p.read_text())
        check("Saturday refresh: code again, Opus layer carried, no second call",
              r["built"] == ["1"] and len(CALLS) == 1 and js["trigger"] == "sat"
              and js["opus"]["generated_at"] == jo["opus"]["generated_at"]
              and any(c["by"] == "opus" for c in js["facts"]), (r, len(CALLS)))
        old = dict(js, opus=dict(js["opus"], generated_at=at("2026-11-12T17:30:00").isoformat()))
        with real_client(transport=ok_transport) as c:
            dn = dossier.build_one(c, G, 2026, 12, "sat", at("2026-11-21T07:10:00"), old, items=NEWS)
        check("an Opus layer older than 6 days is dropped", "opus" not in dn
              and all(c["by"] == "code" for c in dn["facts"]), dn.get("researcher"))

        r = dossier.run(base=base, db=FakeDB(), opus=True, force=True, now=fri, research=boom)
        jb = json.loads(p.read_text())
        check("failed Opus run: the dossier still builds (code + last good layer)",
              r["built"] == ["1"] and jb["opus"]["generated_at"] == jo["opus"]["generated_at"], r)
        dossier.news_items = lambda: []
        r = dossier.run(base=base, db=FakeDB(), force=True, now=fri, research=boom)
        check("empty news cache never replaces a dossier", r["failed"] == ["1"] and json.loads(p.read_text()) == jb, r)
        st = json.loads((base / "12" / "_state.json").read_text())
        check("failure recorded for backoff", "failed_at" in st["1"], st)
        check("no stray temp files", not list((base / "12").glob(".tmp-*")))
    finally:
        dossier.httpx.Client, dossier.news_items = real_client, real_items
        dossier._reddit_cache = None

if FAILED:
    print(f"\n{len(FAILED)} FAILED")
    sys.exit(1)
print("\nALL PASS")
