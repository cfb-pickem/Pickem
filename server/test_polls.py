"""
ESPN polls as the primary CFP/AP/Coaches source (polls.py), with a code sanity check instead of an Opus scrape:
ranks 1..N with no gaps or repeats, every team a known FBS school, records that agree with CFBD, a release
date that isn't in the future, and CFP vs AP overlap. A poll that fails the check is never served; the last
good copy stays. The Opus CFP list (slate_intel) is only a logged cross-check.

Run:  ./.venv/bin/python3 -I test_polls.py
"""

from __future__ import annotations

import copy
import datetime as dt
import gzip
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, ".")
import polls  # noqa: E402

FIX = Path(__file__).resolve().parent / "eval" / "fixtures" / "factstore"
NOW = dt.datetime(2026, 11, 18, 15, 30, tzinfo=dt.timezone.utc).timestamp()
RAW = json.loads((FIX / "espn_rankings.json").read_text())
CFBD = json.loads(gzip.decompress((FIX / "cfbd.json.gz").read_bytes()))
RECORDS = CFBD["records"]


def parsed():
    return polls.parse(copy.deepcopy(RAW))


def test_parse_espn_polls():
    p = parsed()
    assert set(p) >= {"CFP", "AP", "Coaches"}, p.keys()
    cfp = p["CFP"]
    assert cfp["date"] == "2026-11-17" and cfp["source"] == "ESPN"
    assert cfp["ranks"][0] == {"rank": 1, "team": "Georgia", "record": "9-1"}, cfp["ranks"][0]
    assert len(cfp["ranks"]) == 25


def test_team_names_map_to_cfbd_spelling():
    p = parsed()
    names = {r["team"] for poll in p.values() for r in poll["ranks"]}
    fbs = {r["team"] for r in RECORDS}
    assert names <= fbs, names - fbs


def test_sanity_passes_on_real_week():
    p = parsed()
    for name in ("CFP", "AP", "Coaches"):
        ok, problems = polls.sanity(name, p[name], RECORDS, now=NOW, others=p)
        assert ok, (name, problems)


def test_sanity_rejects_repeated_or_missing_rank():
    p = parsed()
    bad = copy.deepcopy(p["CFP"])
    bad["ranks"][5]["rank"] = 5
    ok, problems = polls.sanity("CFP", bad, RECORDS, now=NOW, others=p)
    assert not ok and any("rank" in x for x in problems), problems


def test_sanity_rejects_unknown_team():
    p = parsed()
    bad = copy.deepcopy(p["CFP"])
    bad["ranks"][3]["team"] = "Montana Tech"
    ok, problems = polls.sanity("CFP", bad, RECORDS, now=NOW, others=p)
    assert not ok and any("Montana Tech" in x for x in problems), problems


def test_sanity_rejects_records_that_disagree_with_cfbd():
    p = parsed()
    bad = copy.deepcopy(p["CFP"])
    for r in bad["ranks"][:6]:
        r["record"] = "3-7"
    ok, problems = polls.sanity("CFP", bad, RECORDS, now=NOW, others=p)
    assert not ok and any("record" in x for x in problems), problems


def test_sanity_rejects_future_or_ancient_release():
    p = parsed()
    fut = dict(p["CFP"], date="2026-11-25")
    assert not polls.sanity("CFP", fut, RECORDS, now=NOW, others=p)[0]
    old = dict(p["CFP"], date="2026-09-01")
    assert not polls.sanity("CFP", old, RECORDS, now=NOW, others=p)[0]


def test_sanity_rejects_cfp_that_shares_little_with_ap():
    p = parsed()
    bad = copy.deepcopy(p["CFP"])
    fbs = [r for r in RECORDS if r["team"] not in {x["team"] for x in p["AP"]["ranks"]}]
    for r, t in zip(bad["ranks"], fbs):
        r["team"], r["record"] = t["team"], f"{t['total']['wins']}-{t['total']['losses']}"
    ok, problems = polls.sanity("CFP", bad, RECORDS, now=NOW, others=p)
    assert not ok and any("AP" in x for x in problems), problems


def test_refresh_keeps_last_good_when_new_poll_fails():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "polls.json"
        good = polls.refresh(fetch=lambda: copy.deepcopy(RAW), records=RECORDS, path=path, now=NOW)
        assert good["CFP"]["ok"] and good["CFP"]["ranks"][0]["team"] == "Georgia"
        broken = copy.deepcopy(RAW)
        for rk in broken["rankings"]:
            if rk["type"] == "cfp":
                rk["ranks"][1]["current"] = 1
        kept = polls.refresh(fetch=lambda: broken, records=RECORDS, path=path, now=NOW + 3600)
        assert kept["CFP"]["ranks"][1]["team"] == "Utah" and kept["CFP"]["ok"], kept["CFP"]
        assert kept["CFP"]["rejected"], "the bad fetch is logged on the poll"
        assert json.loads(path.read_text())["CFP"]["ranks"][1]["team"] == "Utah"


def test_refresh_survives_fetch_error():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "polls.json"
        polls.refresh(fetch=lambda: copy.deepcopy(RAW), records=RECORDS, path=path, now=NOW)

        def boom():
            raise OSError("espn down")
        kept = polls.refresh(fetch=boom, records=RECORDS, path=path, now=NOW + 7200)
        assert kept["AP"]["ranks"][0]["team"] == "Georgia"


def test_refresh_ttl_skips_fetch():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "polls.json"
        polls.refresh(fetch=lambda: copy.deepcopy(RAW), records=RECORDS, path=path, now=NOW)
        calls = []
        polls.refresh(fetch=lambda: calls.append(1) or copy.deepcopy(RAW), records=RECORDS, path=path, now=NOW + 600)
        assert not calls, "inside the hour TTL nothing is fetched"


def test_opus_cross_check_is_logged_not_served():
    p = parsed()
    intel = json.loads((FIX / "slate_intel.json").read_text()).get("cfp") or {}
    diffs = polls.cross_check(p["CFP"], intel)
    assert isinstance(diffs, list)
    fake = {"top": [dict(x) for x in intel.get("top") or [{"rank": 1, "team": "Georgia"}]]}
    fake["top"][0]["team"] = "Montana Tech"
    assert any("Montana Tech" in x for x in polls.cross_check(p["CFP"], fake))


def test_no_anthropic_anywhere():
    src = Path(polls.__file__).read_text()
    assert "claude" not in src.lower() and "anthropic" not in src.lower()


def test_cfp_note_serves_checked_espn_not_the_scrape():
    import factstore
    import slate_cards
    with tempfile.TemporaryDirectory() as d:
        pp = Path(d) / "polls.json"
        polls.refresh(fetch=lambda: copy.deepcopy(RAW), records=RECORDS, path=pp, now=NOW)
        orig = polls.PATH
        polls.PATH = pp
        factstore.use(Path(d) / "no-store.json")
        try:
            note = slate_cards.cfp_note("what's the college football playoff picture?")
            assert "per ESPN" in note and "1 Georgia (9-1)" in note and "released 2026-11-17" in note, note
            data = json.loads(pp.read_text())
            data["CFP"]["ok"] = False
            pp.write_text(json.dumps(data))
            note = slate_cards.cfp_note("what's the college football playoff picture?")
            assert "not in your data" in note and "cbssports" not in note.lower(), note
        finally:
            polls.PATH = orig
            factstore.use(None)


def test_chat_rankings_come_from_the_checked_cache():
    import asyncio
    import chat
    with tempfile.TemporaryDirectory() as d:
        pp = Path(d) / "polls.json"
        polls.refresh(fetch=lambda: copy.deepcopy(RAW), records=RECORDS, path=pp, now=NOW)
        orig = polls.PATH
        polls.PATH = pp
        chat._rank_memo.update(at=0, data=None)

        class NoNet:
            async def get(self, *a, **k):
                raise AssertionError("no live fetch when the checked cache has the polls")
        try:
            got = asyncio.run(chat.load_rankings(NoNet()))
            assert got["CFP"]["ranks"][0][:2] == (1, "Georgia") and got["CFP"]["date"] == "2026-11-17", got["CFP"]
            assert got["AP"]["ranks"][0][1] == "Georgia"
        finally:
            polls.PATH = orig
            chat._rank_memo.update(at=0, data=None)


if __name__ == "__main__":
    fails = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok   {name}")
            except Exception as e:  # noqa: BLE001
                fails += 1
                print(f"FAIL {name}: {e!r}"[:800])
    print("ALL PASS" if not fails else f"{fails} FAILED")
    sys.exit(1 if fails else 0)
