"""
Deterministic tests for dossier.py: what's due when, the validator, the
summary grounding check, and keep-the-last-good-dossier. No network, no
claude, no GPU (research and HTTP are faked).

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

# -- summary grounding ------------------------------------------------------------------
src = "- [injury] Miami QB Carson Beck is probable after leaving last week's game with 2 minutes left."
check("grounded summary passes", dossier.summary_grounded("Carson Beck is probable for Miami.", src))
check("invented name fails", not dossier.summary_grounded("Carson Beck and Mark Fletcher are out.", src))
check("invented number fails", not dossier.summary_grounded("Beck threw for 300 yards.", src))
check("rule summary prefers injuries", dossier.rule_summary(secs).startswith("Miami QB Carson Beck"))
check("rule summary with nothing", "No verified" in dossier.rule_summary({"facts": [], "opinions": [], "rumors": []}))

# -- build/run: keep the last good dossier -----------------------------------------------
ok_transport = httpx.MockTransport(lambda req: httpx.Response(200, text="<feed xmlns='http://www.w3.org/2005/Atom'/>"))


class FakeDB:
    def get(self, q):
        if "select=cfb_season" in q:
            return [{"cfb_season": 2026}]
        if "select=week&" in q:
            return [{"week": 12}]
        return [G]


with tempfile.TemporaryDirectory() as d:
    base = Path(d)
    dossier._reddit_cache = ("ok", [])
    real_client = dossier.httpx.Client
    dossier.httpx.Client = lambda **kw: real_client(transport=ok_transport, **kw)
    try:
        r = dossier.run(base=base, db=FakeDB(), use_local=False, now=wed,
                        research=lambda prompt: {"claims": [claim()], "notes": ""})
        p = dossier.path_for(12, 1, base)
        j = json.loads(p.read_text())
        check("first run builds the dossier", r["built"] == ["1"] and len(j["facts"]) == 1
              and j["summary_by"] == "rule" and j["trigger"] == "new", (r, j.get("trigger")))
        check("no member data in the prompt", "Virginia Tech" in dossier.build_prompt(G, 2026, 12, [], wed))
        r = dossier.run(base=base, db=FakeDB(), use_local=False, now=wed,
                        research=lambda prompt: {"claims": [claim()]})
        check("second run: nothing due (idempotent)", r.get("built") == 0, r)

        def boom(prompt):
            raise RuntimeError("claude exit 1")
        r = dossier.run(base=base, db=FakeDB(), use_local=False, force=True, now=wed, research=boom)
        j2 = json.loads(p.read_text())
        check("failed refresh keeps the previous dossier", r["failed"] == ["1"] and j2 == j, r)
        st = json.loads((base / "12" / "_state.json").read_text())
        check("failure recorded for backoff", "failed_at" in st["1"], st)
        r = dossier.run(base=base, db=FakeDB(), use_local=False, force=True, now=wed,
                        research=lambda prompt: {"claims": []})
        check("empty result never replaces a dossier with claims", r["failed"] == ["1"]
              and json.loads(p.read_text()) == j, r)
        check("no stray temp files", not list((base / "12").glob(".tmp-*")))
    finally:
        dossier.httpx.Client = real_client
        dossier._reddit_cache = None

if FAILED:
    print(f"\n{len(FAILED)} FAILED")
    sys.exit(1)
print("\nALL PASS")
