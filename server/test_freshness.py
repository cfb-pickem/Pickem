"""
Freshness rules (freshness.py), their reply-time use (stale data dropped or
labelled "as of", never stated as current) and the feed monitor's escalation:
kick, then a capped Claude repair that auto-merges only inside the feed's
allowlist with its tests passing (the claude call is mocked; nothing is sent,
nothing touches the real repo).

Run:  ./.venv/bin/python3 -I test_freshness.py
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

os.environ["PICKY_FRESHNESS_KICK"] = "0"
sys.path.insert(0, ".")
import freshness as F  # noqa: E402
import feed_monitor as FM  # noqa: E402

CT = F.CT
TUE = dt.datetime(2026, 11, 17, 10, 0, tzinfo=CT).timestamp()   # not a game day
SAT = dt.datetime(2026, 11, 21, 14, 0, tzinfo=CT).timestamp()   # game day
H, D = 3600, 86400


# --- rule book ------------------------------------------------------------------

def test_max_age_tighter_on_game_days():
    assert F.game_day(SAT) and not F.game_day(TUE)
    assert F.game_day(dt.datetime(2026, 11, 20, 18, 0, tzinfo=CT)) and not F.game_day(
        dt.datetime(2026, 11, 20, 9, 0, tzinfo=CT))  # Friday night yes, Friday morning no
    for s in ("lines", "news", "lore", "splits"):
        assert F.max_age(s, SAT) < F.max_age(s, TUE), s
    assert F.max_age("lines", SAT) <= 10 * 60 and F.max_age("live", SAT) <= 3 * 60
    assert F.max_age("live_game", SAT) == 90  # game-day live mode's per-game MAX_AGE


def test_fresh_and_as_of():
    assert F.fresh("news", TUE - 20 * 60, now=TUE)
    assert not F.fresh("news", TUE - 2 * H, now=TUE)
    assert not F.fresh("news", None, now=TUE) or F.fetched_at("news") is not None
    assert F.as_of("news", TUE - 2 * H, now=TUE) == "8:00 AM CT"
    assert F.as_of("news", TUE - 2 * D, now=TUE) == "Sun 10:00 AM"
    assert F.as_of("news", TUE - 10 * D, now=TUE) == "Nov 7"
    assert F.as_of("news", "2026-11-17T15:00:00+00:00", now=TUE) == "9:00 AM CT"  # ISO stamp


def test_check_fresh_stale_dead_missing():
    v = F.check("lines", TUE - 60, now=TUE)
    assert (v.status, v.use, v.hedge) == ("fresh", True, ""), v
    v = F.check("lines", TUE - 2 * H, now=TUE)        # past 30 min, inside 6 h
    assert v.status == "stale" and v.use and v.hedge == "as of 8:00 AM CT", v
    v = F.check("lines", SAT - 4 * H, now=SAT)        # game day hard limit is 3 h
    assert v.status == "dead" and not v.use, v
    v = F.check("lines", None, now=TUE)
    assert v.status == "missing" and not v.use, v


def test_rider_drops_or_labels():
    txt = "INSIDER NOTES: SEC wants 16 teams."
    assert F.rider("insider", txt, TUE - D, now=TUE) == txt
    stale = F.rider("insider", txt, TUE - 10 * D, now=TUE)
    assert stale.startswith(txt) and "as of Nov 7" in stale and "never present it as the latest" in stale, stale
    assert F.rider("insider", txt, TUE - 30 * D, now=TUE) == ""


def test_fetched_at_from_caches():
    with tempfile.TemporaryDirectory() as d:
        j = Path(d) / "x.json"
        j.write_text(json.dumps({"fetched_at": "2026-11-17T15:00:00+00:00"}))
        db = Path(d) / "c.sqlite"
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE datasets (name TEXT, fetched_at REAL)")
        con.executemany("INSERT INTO datasets VALUES (?, ?)", [(n, TUE - H) for n in F.CFBD_CORE]
                        + [("lines", TUE - 3 * H), ("venues", TUE - 90 * D)])
        con.commit()
        con.close()
        real = dict(F.SOURCES)
        try:
            F.register(F.Source("t_json", (H, H), (D, D), "json:fetched_at", j))
            F.register(F.Source("t_db", (H, H), (D, D), "sqlite", db))
            F.register(F.Source("t_mtime", (H, H), (D, D), "mtime", Path(d) / "nope.md"))
            assert F.fetched_at("t_json") == dt.datetime(2026, 11, 17, 15, tzinfo=dt.timezone.utc).timestamp()
            assert F.fetched_at("t_db") == TUE - 3 * H   # oldest core dataset; static venues ignored
            assert F.fetched_at("t_mtime") is None and F.check("t_mtime", now=TUE).status == "missing"
        finally:
            F.SOURCES.clear()
            F.SOURCES.update(real)


def test_register_hook_and_allowlists():
    try:
        F.register(F.Source("bad", (H, H), (D, D), files=("server/news.py", "server/chat.py")))
        raise AssertionError("brain file allowed in a repair allowlist")
    except ValueError:
        pass
    assert "bad" not in F.SOURCES
    # live mode pre-registered: picked up by the monitor once its LaunchAgent is loaded
    lv = F.source("live_games")
    assert lv.agent == "com.pickem.live-games" and lv.monitor == "age" and "server/live_games.py" in lv.files
    for s in F.SOURCES.values():
        assert not set(s.files) & set(F.NEVER_REPAIR), s.name
        if s.files and all(Path("..", f).exists() for f in s.files):
            for t in s.tests:  # a repairable feed's tests must exist, or every repair is rejected
                assert Path(t).exists(), (s.name, t)


# --- reply time: stale never stated as current --------------------------------

def test_news_dropped_when_dead_hedged_when_stale():
    import chat
    import news
    items = {"a": {"title": "Notre Dame routs Boston College 41-7", "date": "2026-11-14T22:00:00+00:00",
                   "source": "ESPN", "summary": "CJ Carr threw two touchdowns.", "teams": ["Notre Dame"],
                   "confs": [], "players": [], "link": "x"}}
    real = news.CACHE_PATH
    try:
        with tempfile.TemporaryDirectory() as d:
            for age, want in ((10 * 60, "fresh"), (6 * H, "stale"), (3 * D, "dead")):
                f = Path(d) / f"news{age}.json"
                at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=age)
                f.write_text(json.dumps({"items": items, "fetched_at": at.isoformat()}))
                news.CACHE_PATH = f
                news._mem = None
                n = chat.news_note("Any news on Boston College?")
                if want == "dead":
                    assert n == "", n
                elif want == "stale":
                    assert "last checked" in n, n
                else:
                    assert "last checked" not in n, n
    finally:
        news.CACHE_PATH = real
        news._mem = None


def test_espn_fallback_labelled_or_dropped():
    import espn

    class Down:
        async def get(self, *a, **k):
            raise OSError("espn down")

    now = time.time()
    try:
        espn._cache[901] = (now - 2 * H, {"market": {"details": "IOWA -6.5"}})
        got = asyncio.run(espn._fetch_one(Down(), 901))
        if F.hard_age("lines") > 2 * H:  # not a game day: kept, flagged with its time
            assert got.get("stale_at") == now - 2 * H and got["market"]["details"] == "IOWA -6.5", got
        else:
            assert got == {"unavailable": True}, got
        espn._cache[902] = (now - 7 * H, {"market": {"details": "IOWA -6.5"}})
        assert asyncio.run(espn._fetch_one(Down(), 902)) == {"unavailable": True}
    finally:
        espn._cache.pop(901, None)
        espn._cache.pop(902, None)


def test_market_line_says_as_of_not_now():
    import chat
    from test_leader import ctx
    c = ctx()
    notes = {"market": {"details": "SMU -3.5"}, "home": {"abbr": "SMU"}, "away": {"abbr": "WAKE"}}
    c["espn"] = {1: notes}
    assert "ESPN market line now:" in chat.render_context(c)
    c["espn"] = {1: dict(notes, stale_at=time.time() - 2 * H)}
    r = chat.render_context(c)
    assert "ESPN market line now:" not in r and "ESPN market line as of " in r and "not current" in r, r


def test_dossier_dropped_past_hard_limit():
    import chat
    real = chat.DOSSIER_DIR
    try:
        with tempfile.TemporaryDirectory() as d:
            chat.DOSSIER_DIR = Path(d)
            (Path(d) / "11").mkdir()
            for gid, age in ((1, D), (2, 9 * D)):
                at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=age)
                (Path(d) / "11" / f"{gid}.json").write_text(json.dumps({"generated_at": at.isoformat(), "items": []}))
            assert chat.load_dossier(11, 1) is not None
            assert chat.load_dossier(11, 2) is None
    finally:
        chat.DOSSIER_DIR = real


# --- monitor escalation (decide is pure) --------------------------------------

def _bad(why="cache stale"):
    return {"ok": False, "missing": False, "why": why}


OK = {"ok": True, "missing": False, "why": ""}


class _Manual:
    """The by-hand path (PICKY_OPUS_MANUAL=1 feed_monitor.py ...): Claude repairs possible. The schedule never
    repairs (zero maintenance, phase 8; test_schedule_never_repairs)."""

    def __enter__(self):
        import picky_claude
        self.pc, self.bin = picky_claude, picky_claude.CLAUDE
        self.ks = os.environ.pop("PICKY_NO_ANTHROPIC", None)  # the suite runs with the kill switch on
        os.environ["PICKY_OPUS_MANUAL"] = "1"
        picky_claude.CLAUDE = sys.executable
        return self

    def __exit__(self, *a):
        os.environ.pop("PICKY_OPUS_MANUAL", None)
        if self.ks is not None:
            os.environ["PICKY_NO_ANTHROPIC"] = self.ks
        self.pc.CLAUDE = self.bin


def test_schedule_never_repairs():
    st, now, kinds, tells = {}, TUE, [], []
    for _ in range(int(30 * H / 900)):
        acts, st = FM.decide({"news": _bad()}, st, now)
        kinds += [a[0] for a in acts]
        if any(a[0] == "tell" for a in acts):
            tells.append(now)
            st["told"] = FM.ct_day(now)
        now += 900
    assert "repair" not in kinds and kinds.count("kick") >= 10, kinds  # kicked again every 2 h, never repaired
    assert len(tells) == 1 and tells[0] - TUE >= FM.TELL_AFTER, tells  # still one heads-up after 24 h, no sooner


def test_escalation_kick_then_repair():
    with _Manual():
        _escalation_kick_then_repair()


def _escalation_kick_then_repair():
    st, now = {}, TUE
    plans = []
    for _ in range(5):
        acts, st = FM.decide({"news": _bad()}, st, now)
        plans.append(acts)
        now += 900
    assert plans[0] == [] and plans[1] == [("kick", "news")] and plans[2] == [("repair", "news")], plans
    assert plans[3] == [("kick", "news")] and plans[4] == [("repair", "news")], plans  # starts over after a repair
    acts, st = FM.decide({"news": OK}, st, now)
    assert acts and acts[0][0] == "remember" and "recovered" in acts[0][1], acts
    assert st["feeds"]["news"] == {"seen": True}


def test_repair_caps_daily_and_per_outage():
    with _Manual():
        _repair_caps_daily_and_per_outage()


def _repair_caps_daily_and_per_outage():
    st, now = {}, TUE
    repairs = []
    for _ in range(40):  # 10 h of checks, three feeds broken
        acts, st = FM.decide({"news": _bad(), "lore": _bad(), "cfbd": _bad()}, st, now)
        repairs += [a for a in acts if a[0] == "repair"]
        now += 900
    assert len(repairs) == FM.DAILY_CAP == 3, repairs
    nxt = dt.datetime(2026, 11, 18, 0, 30, tzinfo=CT).timestamp()  # new CT day: cap resets
    acts, st = FM.decide({"news": _bad(), "lore": _bad(), "cfbd": _bad()}, st, nxt)
    acts2, st = FM.decide({"news": _bad(), "lore": _bad(), "cfbd": _bad()}, st, nxt + 900)
    assert any(a[0] == "repair" for a in acts + acts2)
    # per outage: one feed never gets more than OUTAGE_CAP repairs, whatever the day
    st, now, n = {}, TUE, 0
    for _ in range(400):
        acts, st = FM.decide({"news": _bad()}, st, now)
        n += sum(a[0] == "repair" for a in acts)
        now += 900
    assert n == FM.OUTAGE_CAP, n


def test_never_installed_feed_is_not_broken():
    acts, st = FM.decide({"insider": {"ok": False, "missing": True, "why": "cache missing"}}, {}, TUE)
    acts, st = FM.decide({"insider": {"ok": False, "missing": True, "why": "cache missing"}}, st, TUE + 900)
    assert acts == [] and not st["feeds"]["insider"], (acts, st)
    # but once seen, a vanished cache is broken
    st = {"feeds": {"news": {"seen": True}}}
    _, st = FM.decide({"news": {"ok": False, "missing": True, "why": "cache missing"}}, st, TUE)
    acts, st = FM.decide({"news": {"ok": False, "missing": True, "why": "cache missing"}}, st, TUE + 900)
    assert acts == [("kick", "news")], acts


def test_unrepairable_feed_only_kicks():
    st, now, kinds = {}, TUE, []
    for _ in range(6):
        acts, st = FM.decide({"hot_takes": _bad()}, st, now)  # refresher lives outside the repo: no allowlist
        kinds += [a[0] for a in acts]
        now += 900
    assert kinds == ["kick"], kinds


def test_charlie_told_once_a_day_only_after_24h_and_cap():
    with _Manual():
        _charlie_told_once_a_day_only_after_24h_and_cap()


def _charlie_told_once_a_day_only_after_24h_and_cap():
    st, now, tells = {}, TUE, []
    for i in range(int(30 * H / 900)):
        acts, st = FM.decide({"news": _bad()}, st, now)
        t = [a for a in acts if a[0] == "tell"]
        if t:
            tells.append((now, t[0][1]))
            st["told"] = FM.ct_day(now)  # what run_pass does when notify succeeds
        now += 900
    assert len(tells) == 1, tells
    first = tells[0][0]
    assert first - TUE >= FM.TELL_AFTER and "news" in tells[0][1] and "24h" in tells[0][1], tells
    # never before 24 h, even with repairs used up
    st = {"feeds": {"news": {"bad": 5, "since": TUE, "repairs": 3, "seen": True}}}
    acts, _ = FM.decide({"news": _bad()}, st, TUE + 23 * H)
    assert not [a for a in acts if a[0] == "tell"], acts
    # 24 h+ but repairs still available (not past the cap): keep repairing, no text
    st = {"feeds": {"news": {"bad": 2, "since": TUE, "repairs": 1, "seen": True}}}
    acts, _ = FM.decide({"news": _bad()}, st, TUE + 25 * H)
    assert ("repair", "news") in acts and not [a for a in acts if a[0] == "tell"], acts


def test_observe_watches_loaded_agents_only():
    def info(label):
        return {"com.pickem.news-refresh": {"exit": 0}, "com.pickem.picky-dossier": {"exit": 1}}.get(label)
    obs = FM.observe(time.time(), info=info)
    assert set(obs) == {"news", "dossier", "factstore"}, obs  # the fact store rides the news-refresh tick
    assert obs["dossier"]["ok"] is False and "exit code 1" in obs["dossier"]["why"], obs


# --- repair run (claude mocked; a scratch git repo stands in for the real one) --

def _git(cwd, *a):
    return subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True, check=True).stdout


class Scratch:
    """A throwaway repo with main checked out, and FM pointed at it."""

    def __enter__(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.repo = t / "repo"
        (self.repo / "server").mkdir(parents=True)
        for f in ("news.py", "chat.py", "lore.py", "com.pickem.news-refresh.plist"):
            (self.repo / "server" / f).write_text(f"# {f}\n")
        _git(self.repo, "init", "-q", "-b", "main")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "-m", "init")
        self.saved = {k: getattr(FM, k) for k in ("REPO", "REPAIR_LOG", "LOCK_FILE", "MEMORY", "STATE_FILE")}
        FM.REPO, FM.REPAIR_LOG, FM.LOCK_FILE = self.repo, t / "repairs.log", t / "repair.lock"
        FM.MEMORY, FM.STATE_FILE = t / "memory.md", t / "state.json"
        os.environ["PICKY_REPAIR_WT_DIR"] = str(t)
        self.frozen = FM.freeze.frozen
        FM.freeze.frozen = lambda *a: False  # the tests run on any day; the freeze has its own test
        return self

    def head(self):
        return _git(self.repo, "rev-parse", "HEAD").strip()

    def log(self):
        return [json.loads(x) for x in FM.REPAIR_LOG.read_text().splitlines()]

    def __exit__(self, *a):
        for k, v in self.saved.items():
            setattr(FM, k, v)
        os.environ.pop("PICKY_REPAIR_WT_DIR", None)
        FM.freeze.frozen = self.frozen
        self.tmp.cleanup()


def _claude_editing(*files):
    calls = []

    def fake(prompt, cwd):
        calls.append(prompt)
        for f in files:
            p = Path(cwd) / f
            p.write_text(p.read_text() + "# fixed\n" if p.exists() else "# new\n")
        return 0, "FIXED: handled the new feed format"
    fake.calls = calls
    return fake


def _run(claude, tests=lambda s, wt: None):
    return FM.repair("news", why="cache stale", claude=claude, tests=tests, push=False, kick_after=lambda a: None)


def test_repair_inside_allowlist_merges():
    with Scratch() as sc:
        before = sc.head()
        claude = _claude_editing("server/news.py")
        rec = _run(claude)
        assert rec["result"] == "merged" and rec["files"] == ["server/news.py"], rec
        assert sc.head() != before and "# fixed" in (sc.repo / "server/news.py").read_text()
        assert "chat.py" in claude.calls[0] and "ONLY these files: server/news.py" in claude.calls[0]
        assert "cache stale" in claude.calls[0]
        log = sc.log()
        assert log[-1]["result"] == "merged" and isinstance(log[-1]["duration_s"], int) and "claude_s" in log[-1]
        assert "repair run for the news feed: merged" in FM.MEMORY.read_text()
        assert "picky-repair/" not in _git(sc.repo, "branch")  # merged branch cleaned up
        assert len(_git(sc.repo, "worktree", "list").splitlines()) == 1


def test_repair_in_the_freeze_is_parked_not_merged():
    with Scratch() as sc:
        before = sc.head()
        FM.freeze.frozen = lambda *a: True
        released = []
        rec = FM.repair("news", why="cache stale", claude=_claude_editing("server/news.py"), tests=lambda s, wt: None,
                        push=False, kick_after=lambda a: None, release=lambda: released.append(1) or {"rc": 0})
        assert rec["result"] == "parked" and "freeze" in rec["reason"], rec
        assert sc.head() == before and not released and "picky-repair/news-" in _git(sc.repo, "branch")


def test_repair_touching_brain_or_outside_allowlist_rejected():
    for files, why in ((("server/news.py", "server/chat.py"), "brain"), (("server/lore.py",), "allowlist"),
                       (("server/news.py", "server/new_helper.py"), "allowlist")):
        with Scratch() as sc:
            before = sc.head()
            rec = _run(_claude_editing(*files))
            assert rec["result"] == "rejected" and why in rec["reason"], rec
            assert sc.head() == before and "# fixed" not in (sc.repo / "server/news.py").read_text()
            assert "picky-repair/news-" in _git(sc.repo, "branch")  # kept for a look


def test_repair_with_failing_tests_rejected():
    with Scratch() as sc:
        before = sc.head()
        rec = _run(_claude_editing("server/news.py"), tests=lambda s, wt: "test_news.py failed: boom")
        assert rec["result"] == "rejected" and "test_news.py failed" in rec["reason"], rec
        assert sc.head() == before


def test_repair_no_change_kill_switch_and_lock():
    with Scratch() as sc:
        before = sc.head()
        rec = _run(lambda p, cwd: (0, "NO_FIX: ESPN is down"))
        assert rec["result"] == "no-fix" and rec["claude_said"] == "NO_FIX: ESPN is down", rec
        os.environ["PICKY_NO_ANTHROPIC"] = "1"
        try:
            rec = FM.repair("news", why="x", tests=lambda s, wt: None, push=False, kick_after=lambda a: None)
        finally:
            os.environ.pop("PICKY_NO_ANTHROPIC")
        assert rec["result"] == "failed" and "AnthropicDisabled" in rec["claude_said"], rec  # real call_claude, guarded
        import fcntl
        with open(FM.LOCK_FILE, "w") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            rec = _run(_claude_editing("server/news.py"))
        assert rec["result"] == "skipped", rec
        assert sc.head() == before
        assert [r["result"] for r in sc.log()] == ["no-fix", "failed", "skipped"]


def test_gate():
    s = F.source("news")
    assert FM.gate([], s) == "no changes"
    assert FM.gate(["server/news.py"], s) is None
    assert "brain" in FM.gate(["server/group_chat.py"], s)
    assert "allowlist" in FM.gate(["server/news.py", "server/espn.py"], s)


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
