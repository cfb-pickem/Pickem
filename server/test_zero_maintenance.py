"""
Zero maintenance (phase 8, Charlie 2026-11-18): "If I cancel Anthropic or if I log in after 5 months away it
should just all be working perfectly."

Simulates three worlds and checks replies, refresh jobs and the Supabase keepalive in each:
  1. Anthropic unavailable: the claude binary missing, or every call an API error.
  2. The clock moved 5 months forward into the offseason (April 2027).
  3. The next August (2027 season, week 1).
Plus the guard against regressions: a non-optional scheduled or runtime path that adds a Claude call fails here.

No network, no model, no claude. Run:  ./.venv/bin/python3 -I test_zero_maintenance.py
"""

from __future__ import annotations

import ast
import datetime as dt
import json
import os
import plistlib
import re
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(HERE))
TMP = Path(tempfile.mkdtemp(prefix="picky-zero-maint-"))
os.environ.pop("PICKY_NO_ANTHROPIC", None)       # Anthropic "up" as far as the kill switch goes: the gate must hold
os.environ.pop("PICKY_OPUS_MANUAL", None)
os.environ["PICKY_CLAUDE_USAGE_DIR"] = str(TMP / "usage")
os.environ["PICKY_FEED_HEALTH_LOG"] = str(TMP / "feed_health.log")
os.environ["PICKY_KEEPALIVE_STATE"] = str(TMP / "keepalive.json")
os.environ.setdefault("PICKY_NO_REPEAT_STATE", str(TMP / "nr.json"))
os.environ.setdefault("PICKY_CALLBACKS_STATE", str(TMP / "cb.json"))
os.environ["PICKY_MEMBER_MEMORY_DIR"] = str(TMP / "mm")

import httpx  # noqa: E402

import picky_claude  # noqa: E402

CT = picky_claude.CT
MISSING = str(TMP / "no-such-dir" / "claude")
# scheduled jobs that may use Claude as a capped booster (each has a code / gemma fallback, tested below)
SCHEDULED = ["slate_intel.py", "insider_refresh.py", "game_cards.py", "dossier.py", "burn_book.py", "league_material.py",
             "league_rules.py", "flub_loop.py", "picky_claude.py"]  # picky_claude.py = the hot-takes script's `run`
LIVE = ["chat.py", "group_chat.py", "canary.py", "watcher.py"]   # live reply processes: never Claude
AUTH_OK = '{"loggedIn": true, "authMethod": "claude.ai", "subscriptionType": "pro"}'
_n = [0]


def entry(name: str) -> str:
    return str(HERE / name)


def fake_claude(body: str, auth: str = AUTH_OK) -> str:
    """A claude stand-in: `auth status` prints `auth`; every -p run is recorded next to itself, then runs body."""
    _n[0] += 1
    p = TMP / f"claude-{_n[0]}"
    p.write_text("#!/bin/sh\nif [ \"$1\" = auth ]; then echo '" + auth + "'; exit 0; fi\n"
                 "echo called >> \"$0.calls\"\ncat > /dev/null\n" + body)
    p.chmod(0o755)
    return str(p)


def calls_of(fake: str) -> int:
    try:
        return len(Path(fake + ".calls").read_text().splitlines())
    except OSError:
        return 0


def so(obj) -> str:
    """fake claude body printing a structured_output result"""
    return "cat <<'JSON'\n" + json.dumps({"structured_output": obj, "result": "ok"}) + "\nJSON\n"


class RunSpy:
    """picky_claude.run stand-in: records attempts (a gemma / code-only job must not even try)."""

    def __init__(self):
        self.calls = []

    def __call__(self, *a, **k):
        self.calls.append(k.get("caller"))
        raise picky_claude.AnthropicDisabled("spy")


FAILING = 'echo "API Error: 529 Overloaded" >&2\nexit 1\n'
WORLDS = ("missing", "failing", "capped", "logged_out")


@contextmanager
def world(kind: str, entry_name: str = "dossier.py", body: str = "echo '{}'\n"):
    """Claude as a scheduled job sees it. up: works, subscription auth. missing: no binary. failing: auth fine,
    every -p run an API error. capped: today's booster cap already used. logged_out: auth status says no."""
    _n[0] += 1
    usage = TMP / f"usage-{kind}-{_n[0]}"
    usage.mkdir()
    fake = {"missing": MISSING, "failing": fake_claude(FAILING),
            "logged_out": fake_claude(body, auth='{"loggedIn": false, "authMethod": "none"}')}.get(kind) \
        or fake_claude(body)
    if kind == "capped":
        now = dt.datetime.now(CT)
        (usage / f"{now.date().isoformat()}.jsonl").write_text("".join(
            json.dumps({"ts": now.isoformat(), "caller": "x", "secs": 30, "result": "ok", "booster": True}) + "\n"
            for _ in range(picky_claude.DAILY_CALLS)))
    with patch.object(picky_claude, "CLAUDE", fake), patch.object(picky_claude, "USAGE_DIR", usage), \
            patch.object(sys, "argv", [entry(entry_name), "run"]):
        yield fake


def booster_lines(usage_dir=None) -> list[dict]:
    d = usage_dir or picky_claude.USAGE_DIR
    return [json.loads(x) for f in sorted(Path(d).glob("*.jsonl")) for x in f.read_text().splitlines()]


# =============================================================================================================
# 1. the booster gate (Charlie 2026-11-18: "If Claude is active, Claude can help Picky with the advanced
#    analysis. If not, Picky should still be able to exist ... I really don't want to spend a bunch on Claude")
# =============================================================================================================

def test_gate_lets_scheduled_boosters_through_when_claude_is_up():
    for name in SCHEDULED:
        with world("up", name):
            assert picky_claude.allowed() and picky_claude.available(), (name, picky_claude.why_not())


def test_live_reply_processes_never_reach_claude():
    for name in LIVE:
        with world("up", name) as fake:
            assert not picky_claude.allowed() and not picky_claude.available(), name
            try:
                picky_claude.run("x", timeout=5, caller="live")
                raise AssertionError(f"run() let the live process {name} through")
            except picky_claude.AnthropicDisabled as e:
                assert "live" in str(e), e
            assert calls_of(fake) == 0
    with patch.dict(os.environ, {"PICKY_OPUS_MANUAL": "1"}), world("up", "chat.py"):
        assert not picky_claude.available()  # not even by hand


def test_cap_constants_are_modest_and_pinned():
    assert picky_claude.DAILY_CALLS == 12 and picky_claude.DAILY_MINUTES == 45, \
        (picky_claude.DAILY_CALLS, picky_claude.DAILY_MINUTES)
    assert picky_claude.LIVE_ENTRIES == tuple(LIVE), picky_claude.LIVE_ENTRIES


def test_daily_call_cap_enforced_and_logged():
    with world("up", "dossier.py", so({"ok": 1})) as fake:
        for _ in range(picky_claude.DAILY_CALLS):
            assert picky_claude.run("x", timeout=10, caller="dossier").returncode == 0
        assert calls_of(fake) == picky_claude.DAILY_CALLS
        assert not picky_claude.available() and "cap" in picky_claude.why_not()
        try:
            picky_claude.run("x", timeout=10, caller="dossier")
            raise AssertionError("cap not enforced")
        except picky_claude.AnthropicDisabled as e:
            assert "cap" in str(e), e
        assert calls_of(fake) == picky_claude.DAILY_CALLS
        lines = booster_lines()
        assert sum(1 for r in lines if r.get("booster")) == picky_claude.DAILY_CALLS, lines
        assert lines[-1]["result"] == "refused: daily cap" and not lines[-1].get("booster"), lines[-1]
        assert "booster 12/12" in picky_claude.usage_line(picky_claude.usage(1)[0])


def test_daily_minutes_cap():
    with world("up", "slate_intel.py") as fake:
        now = dt.datetime.now(CT)
        (picky_claude.USAGE_DIR / f"{now.date().isoformat()}.jsonl").write_text(
            json.dumps({"ts": now.isoformat(), "caller": "slate_intel", "secs": 46 * 60, "result": "ok",
                        "booster": True}) + "\n")
        assert not picky_claude.available() and "cap" in picky_claude.why_not()
        assert calls_of(fake) == 0


def test_exam_tooling_and_hand_runs_are_not_capped():
    with world("capped", "dossier.py"):
        assert not picky_claude.available()
        with patch.object(sys, "argv", [str(HERE / "eval" / "genius" / "judge.py")]):
            assert picky_claude.available()   # the integration lead's exam graders
        with patch.dict(os.environ, {"PICKY_OPUS_MANUAL": "1"}):
            assert picky_claude.available()   # a human running an offline CLI by hand
        with patch.dict(os.environ, {"PICKY_NO_ANTHROPIC": "1", "PICKY_OPUS_MANUAL": "1"}):
            assert not picky_claude.available()  # the kill switch still wins


def test_subscription_only():
    with world("up", "dossier.py", "env > \"$0.env\"\n" + so({"ok": 1})) as fake, \
            patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test", "ANTHROPIC_AUTH_TOKEN": "t"}):
        picky_claude.run("x", timeout=10, caller="dossier")
        env = Path(fake + ".env").read_text()
        assert "sk-test" not in env and "ANTHROPIC_AUTH_TOKEN" not in env
    with world("up", "dossier.py"), \
            patch.object(picky_claude, "CLAUDE", fake_claude("echo '{}'\n", auth='{"loggedIn": true, "authMethod": "apiKey"}')):
        assert not picky_claude.available() and "subscription" in picky_claude.why_not()


def test_missing_logged_out_or_auth_error_is_disabled_not_a_crash():
    for kind, why in (("missing", "missing"), ("logged_out", "auth")):
        with world(kind, "dossier.py") as fake:
            assert picky_claude.allowed() and not picky_claude.available()
            assert why in picky_claude.why_not(), picky_claude.why_not()
            try:
                picky_claude.run("x", timeout=5, caller="dossier")
                raise AssertionError("no error")
            except picky_claude.AnthropicDisabled:
                pass
            assert calls_of(fake) == 0
    # a run that comes back 'please log in' (subscription cancelled) turns boosters off without another spawn
    with world("up", "dossier.py", 'echo "Invalid API key · Please run /login" >&2\nexit 1\n') as fake:
        assert picky_claude.run("x", timeout=5, caller="dossier").returncode == 1
        assert not picky_claude.available() and "auth" in picky_claude.why_not()
        assert calls_of(fake) == 1


def test_auth_check_is_cached():
    with world("up", "dossier.py") as fake:
        log = Path(fake + ".auth")
        Path(fake).write_text(Path(fake).read_text().replace("if [ \"$1\" = auth ]; then",
                                                             f"if [ \"$1\" = auth ]; then echo x >> {log};"))
        for _ in range(5):
            assert picky_claude.available()
        assert len(log.read_text().splitlines()) == 1


def test_enrich_follows_the_gate():
    import enrich
    with world("up", "dossier.py"):
        assert enrich.available()
    for kind in ("missing", "capped", "logged_out"):   # (failing: the gate is open, the call itself fails)
        with world(kind, "dossier.py"):
            assert not enrich.available(), kind


# every place that can start a Claude run -> the test proving its code / gemma fallback (or that it's hand-only)
BOOSTERS = {
    ("dossier.py", "run_claude"): "test_dossier_falls_back_to_code",
    ("dossier.py", "run_take_claude"): "test_dossier_falls_back_to_code",
    ("game_cards.py", "opus_research"): "test_game_cards_fall_back_to_code",
    ("slate_intel.py", "claude_layer"): "test_slate_intel_booster_and_fallback",
    ("insider_refresh.py", "run_claude"): "test_insider_falls_back_to_wire_and_last_notes",
    ("burn_book.py", "refresh"): "test_burn_book_falls_back_to_last_book_and_code_pool",
    ("banter.py", "refresh"): "test_banter_falls_back_to_last_bank",
    ("league_rules.py", "_run_claude"): "test_league_rules_fall_back_to_code_verification",
    ("flub_loop.py", "opus_classify"): "test_flub_loop_is_optional_and_skips_cleanly",
    ("flub_loop.py", "_fix"): "test_flub_loop_is_optional_and_skips_cleanly",
    ("feed_monitor.py", "call_claude"): "test_feed_monitor_never_repairs_on_the_schedule",
    ("news_qa.py", "run"): "test_news_qa_is_hand_only",
}


def test_every_claude_call_site_has_a_fallback_test():
    """A new Claude call on any path fails here until it's registered with a test that proves its fallback."""
    found = set()
    for f in sorted(HERE.glob("*.py")):
        if f.name in ("picky_claude.py",) or f.name.startswith("test_"):
            continue
        tree = ast.parse(f.read_text())
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(fn):
                if not isinstance(node, ast.Call):
                    continue
                c = node.func
                hit = (isinstance(c, ast.Attribute) and isinstance(c.value, ast.Name)
                       and ((c.value.id == "picky_claude" and c.attr == "run")
                            or (c.value.id == "dossier" and c.attr == "run_claude"))) \
                    or (isinstance(c, ast.Name) and c.id == "run_claude" and fn.name != "run_claude")
                if hit:
                    found.add((f.name, fn.name))
    new = found - set(BOOSTERS)
    assert not new, f"Claude call sites with no fallback test (add one to BOOSTERS): {sorted(new)}"
    for site, test in BOOSTERS.items():
        assert callable(globals().get(test)), (site, test)
    for f in sorted(list(HERE.glob("*.py")) + list(HERE.glob("eval/**/*.py"))):
        t = f.read_text()
        if f.name == "test_zero_maintenance.py":
            continue
        assert not re.search(r"^\s*(import|from)\s+anthropic\b", t, re.M), f"{f.name} imports the Anthropic SDK"
        assert "api.anthropic.com" not in t, f"{f.name} calls the Anthropic API directly"


def test_live_replies_never_call_claude():
    """The live lanes answer with every Claude path booby-trapped: one call and the test fails."""
    import router
    import season
    hits = []

    def trap(*a, **k):
        hits.append((a[:1], k.get("caller")))
        raise AssertionError("live reply called Claude")
    ctx = season_ctx(2026, 12, AUG_GAMES, finals=False)
    kinds = sorted(set(season.WEEK_KINDS) | {"standings", "lock", "rules", "record", "jackpot"})
    with world("up", "chat.py"), patch.object(picky_claude, "run", trap), patch.object(picky_claude, "guard", trap), \
            patch("dossier.run_claude", trap), patch("game_cards.opus_research", trap):
        for k in kinds:
            try:
                router._fact_answer(k, ctx, "who do you like this week?")
            except AssertionError:
                raise
            except Exception:  # noqa: BLE001 - a lane erroring on the toy ctx isn't this test's business
                pass
    assert not hits, hits


def test_plists_never_opt_in_and_live_processes_are_refused():
    plists = sorted(list(HERE.glob("com.pickem.*.plist")) + list(REPO.glob("tools/*/com.*.plist")))
    assert len(plists) >= 20, plists
    for p in plists:
        d = plistlib.loads(p.read_bytes())
        env = d.get("EnvironmentVariables") or {}
        assert "PICKY_OPUS_MANUAL" not in env, p.name
        for s in [a for a in d.get("ProgramArguments") or [] if a.endswith(".py")]:
            if Path(s).name in LIVE:
                assert not picky_claude.allowed(str(HERE / Path(s).name)), (p.name, s)


def test_flub_loop_is_optional_and_skips_cleanly():
    import flub_loop
    (TMP / "flubs").mkdir(exist_ok=True)
    for kind in WORLDS:
        with world(kind, "flub_loop.py"), patch.object(flub_loop, "FLUB_DIR", TMP / "flubs"), \
                patch.object(flub_loop, "EXCHANGES", TMP / "flubs" / "no-exchanges.jsonl"), \
                patch.object(flub_loop, "STDERR_LOG", TMP / "flubs" / "no-stderr.log"), \
                patch.object(flub_loop.picky_coach, "COACH_DIR", TMP / "coach"):
            c = flub_loop.classify("2026-11-18", facts={"x": 1})
            assert c.get("by") != "opus", (kind, c)
            if kind != "failing":
                assert "claude" in (c.get("note") or "").lower(), (kind, c)
            f = flub_loop.fix_round("2026-11-18")
            assert f["status"] in ("skipped", "off", "error", "failed"), (kind, f)
    # nothing in the live or scheduled code needs it: member_memory's optional helper import survives without it
    import member_memory
    with patch.dict(sys.modules, {"flub_loop": None}):
        member_memory.real_rows(0, 1)


# =============================================================================================================
# 2. Anthropic unavailable: every refresher runs on code + feeds + local gemma, or keeps its last good copy
# =============================================================================================================

def _ollama_ok(content: dict):
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/api/chat", req.url
        body = json.loads(req.content)
        assert body["model"] == "gemma4:12b-it-qat" and body.get("format"), body.get("model")
        return httpx.Response(200, json={"message": {"content": json.dumps(content)}})
    return handler


def test_local_llm_json_call_uses_gemma_with_schema_and_the_low_gate():
    import local_llm
    import ollama_gate
    used = []
    real = ollama_gate.slot_sync

    def slot(*a, **k):
        used.append(1)
        return real(*a, **k)
    with patch.object(ollama_gate, "slot_sync", slot):
        out = local_llm.json_call("sys", "user", {"type": "object"}, caller="t",
                                  transport=httpx.MockTransport(_ollama_ok({"ok": 1})))
    assert out == {"ok": 1} and used, (out, used)
    try:
        local_llm.json_call("sys", "user", {"type": "object"}, caller="t",
                            transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"message": {"content": "nope"}})))
        raise AssertionError("bad JSON accepted")
    except local_llm.LocalLLMError:
        pass


def test_league_material_writes_with_gemma_not_claude():
    import league_material as lm
    import local_llm
    spy = RunSpy()
    got = []

    def fake_local(system, user, schema, **k):
        got.append(k.get("caller"))
        return {"members": [], "storylines": [], "picky_narrative": {}}
    with patch.object(picky_claude, "run", spy), patch.object(local_llm, "json_call", fake_local):
        r = lm.default_research()("prompt")
    assert got == ["league_material"] and not spy.calls and isinstance(r, dict), (got, spy.calls)


def test_member_memory_extracts_with_gemma_not_claude():
    import local_llm
    import member_memory as mm
    spy = RunSpy()
    got = []

    def fake_local(system, user, schema, **k):
        got.append((k.get("caller"), schema is mm.SCHEMA))
        return {"items": [], "receipts": [], "treatment": []}
    with patch.object(picky_claude, "run", spy), patch.object(local_llm, "json_call", fake_local):
        out = mm.run_local("prompt")
    assert got == [("member_memory", True)] and not spy.calls and out["items"] == [], got
    assert mm.process.__defaults__[0] is mm.run_local  # the nightly's default extractor


def test_group_recap_writer_is_gemma_then_template():
    import group_posts as gp
    import local_llm
    spy = RunSpy()
    with patch.object(picky_claude, "run", spy), \
            patch.object(local_llm, "json_call", lambda s, u, sc, **k: {"texts": ["Week 11 is done."]}):
        assert gp.run_writer("p") == ["Week 11 is done."]
    with patch.object(picky_claude, "run", spy), \
            patch.object(local_llm, "json_call", side_effect=local_llm.LocalLLMError("ollama down")):
        texts, probs = gp.write("recap", ["Week 11 records: Charlie 3-1."], {"Chuck P": "Charlie"})
        assert texts is None and probs, (texts, probs)  # make_recap then posts plain_recap (test_anthropic_down)
    assert not spy.calls


def test_burn_book_falls_back_to_last_book_and_code_pool():
    import burn_book as bb
    out = TMP / "burn_book.json"
    for kind in WORLDS:
        out.write_text(json.dumps({"generated_at": "2026-11-01T10:00:00-05:00", "types": {"insult": [{"text": "keep"}]},
                                   "members": {}, "code": {}}))
        out.with_suffix(".failed").unlink(missing_ok=True)
        with world(kind, "league_material.py"), patch.object(bb, "BOOK", out), \
                patch.object(bb, "load_material", lambda: {"teams": ["A"], "facts": {}, "season": 2026, "week": 12}), \
                patch.object(bb, "recheck", lambda *a, **k: {"ok": True}):
            res = bb.tick()  # the book is 2+ weeks old: a refresh is due, Claude can't do it
        assert res.get("recheck") == {"ok": True}, (kind, res)
        assert json.loads(out.read_text())["types"]["insult"][0]["text"] == "keep", kind


def test_banter_falls_back_to_last_bank():
    import banter
    bank = TMP / "banter.json"
    for kind in WORLDS:
        bank.write_text(json.dumps({"generated_at": "2026-10-01T10:00:00-05:00", "trigger": "weekly",
                                    "categories": {"x": ["keep"]}}))
        bank.with_suffix(".failed").unlink(missing_ok=True)
        with world(kind, "league_material.py"), patch.object(banter, "BANK", bank):
            r = banter.refresh_if_due()
        assert not (r or {}).get("built"), (kind, r)
        assert json.loads(bank.read_text())["categories"] == {"x": ["keep"]}, kind


def test_insider_falls_back_to_wire_and_last_notes():
    import insider_refresh as ir
    now = dt.datetime(2026, 11, 25, 10, tzinfo=CT)
    for kind in WORLDS:
        bank = TMP / f"insider-{kind}.md"
        bank.write_text("Last refreshed 2026-11-10\n\nnotes\n")
        work = TMP / f"ir-work-{kind}"
        with world(kind, "insider_refresh.py"), patch.object(ir, "wire", lambda now=None: {"items": []}), \
                patch.object(ir, "LOG", TMP / "ir.log"), patch.object(ir, "BANK", bank), \
                patch.object(ir, "WORK_DIR", work), patch.object(ir, "STATE", work / "state.json"):
            assert ir.tick(now) == 0, kind   # a booster that can't run is not a failed job (feed_monitor reads exit)
        assert bank.read_text().startswith("Last refreshed 2026-11-10"), kind
    with world("up", "insider_refresh.py"), patch.object(ir, "BANK", TMP / "insider-failing.md"), \
            patch.object(ir, "STATE", TMP / "ir-none" / "state.json"):
        assert ir.opus_due(now)  # Claude up and the notes 15 days old: the booster runs


def _dossier_game():
    return {"GameId": 77, "Away": "Utah", "Home": "Iowa", "Start (CT)": "2026-11-21 11:00"}


def test_dossier_falls_back_to_code():
    import dossier
    fri = dt.datetime(2026, 11, 20, 17, 30, tzinfo=CT)
    items = [{"title": "Iowa QB Mark Gronowski questionable vs Utah", "link": "https://www.espn.com/a",
              "url": "https://www.espn.com/a", "date": "2026-11-19", "source": "ESPN", "teams": ["Iowa"],
              "players": ["Mark Gronowski"], "kind": "injury", "topics": ["injury"], "status": "questionable",
              "story": "s1"}]
    claims = {"claims": [{"text": "Utah RB Wayshawn Parker is out for Saturday.", "kind": "injury", "team": "away",
                          "players": ["Wayshawn Parker"], "status": "out", "source_url": "https://www.espn.com/b",
                          "source_name": "ESPN", "date": "2026-11-19", "confidence": "high"}],
              "searched": [], "notes": ""}
    tr = httpx.MockTransport(lambda r: httpx.Response(200, text="ok"))
    with patch.object(dossier, "reddit_leads", lambda c, a, h: ("ok", [])), patch.object(dossier, "WORK_DIR", TMP / "dw"):
        for kind in WORLDS:
            with world(kind, "dossier.py"), httpx.Client(transport=tr) as c:
                assert kind == "failing" or not dossier.opus_due(None, "fri", fri), kind
                d = dossier.build_one(c, _dossier_game(), 2026, 13, "fri", fri, None, opus=True, items=items)
            assert "opus" not in d and d["researcher"].startswith("code"), (kind, d["researcher"])
            assert any("Gronowski" in f["text"] for f in d["facts"]), (kind, d["facts"])
        with world("up", "dossier.py", so(claims)), httpx.Client(transport=tr) as c:
            assert dossier.opus_due(None, "fri", fri)
            d = dossier.build_one(c, _dossier_game(), 2026, 13, "fri", fri, None, opus=True, items=items,
                                  take_fn=lambda p: (_ for _ in ()).throw(RuntimeError("no take today")))
        assert "Opus layer" in d["researcher"] and d["opus"]["facts"], d["researcher"]


def test_game_cards_fall_back_to_code():
    import analyst
    import game_cards
    now = dt.datetime(2026, 11, 18, 12, tzinfo=CT)
    card = {"generated_at": now.isoformat(), "game": {"id": 5, "season": 2026, "week": 13, "start": "x"},
            "lean": {"side": "home", "strength": "lean"}, "league_line": {"home": -3.5}}
    take = {"pick": "Iowa", "confidence": "medium", "reasons": []}
    take_ai = {"pick": "Iowa", "confidence": "medium", "reasons": [], "generated_at": now.isoformat()}
    with patch.object(game_cards, "case_items", lambda c, d: [{"k": 1}] * 3), \
            patch.object(game_cards, "keep_case", lambda *a: None), patch.object(game_cards, "is_locked", lambda c, n: True), \
            patch.object(analyst, "build_prompt", lambda *a: "p"), \
            patch.object(analyst, "validate_take", lambda raw, *a: (dict(raw), [])):
        for kind in WORLDS:
            with world(kind, "game_cards.py"):
                c = dict(card)
                game_cards.add_case(c, {"analyst_take": take_ai}, None, game_cards.opus_research, now)
            assert (c.get("case") or {}).get("by") == "dossier", (kind, c.get("case"))  # the code-side fallback
        with world("up", "game_cards.py", so(take)):
            c = dict(card)
            game_cards.add_case(c, None, None, game_cards.opus_research, now)
        assert c["case"]["by"] == "opus", c.get("case")


def test_slate_intel_booster_and_fallback():
    import slate_intel as si
    fri = dt.datetime(2026, 11, 20, 17, 20, tzinfo=CT)
    games = [{"id": 401, "awayTeam": "Utah", "homeTeam": "Arizona", "week": 13, "startDate": "2026-11-21T21:00:00Z"}]
    items = [{"title": "Arizona QB Noah Fifita questionable vs Utah", "link": "https://www.espn.com/a",
              "url": "https://www.espn.com/a", "date": "2026-11-19", "source": "ESPN", "teams": ["Arizona"],
              "players": ["Noah Fifita"], "kind": "injury", "topics": ["injury"], "status": "questionable",
              "story": "s1"}]
    raw = {"games": [{"game_id": 401, "claims": [
        {"text": "Utah QB Devon Dampier is out with a knee injury.", "kind": "injury", "team": "away",
         "players": ["Devon Dampier"], "status": "out", "source_url": "https://www.espn.com/q", "source_name": "ESPN",
         "date": "2026-11-19", "confidence": "high"}]}],
        "coaching": [{"school": "Florida", "text": "Florida fired Billy Napier on Oct 19; Billy Gonzales is interim.",
                      "source_url": "https://www.espn.com/c", "date": "2026-10-19"}]}
    tr = httpx.MockTransport(lambda r: httpx.Response(200, json={"rankings": []}))
    for kind in WORLDS:
        cache = TMP / f"si-boost-{kind}.json"
        with world(kind, "slate_intel.py"), patch.object(si, "CACHE", cache):
            assert si.boost_due({}, fri)
            assert si.run(force=True, now=fri, games=games, items=items, transport=tr) == 0
            d = json.loads(cache.read_text())
        g = d["games"]["401"]
        assert g["by"] == "code" and "Fifita" in g["facts"][0]["text"], (kind, g)
        assert d["by"].startswith("code") and not d.get("boost"), (kind, d.get("by"))
    cache = TMP / "si-boost-up.json"
    with world("up", "slate_intel.py", so(raw)) as fake, patch.object(si, "CACHE", cache):
        assert si.run(force=True, now=fri, games=games, items=items, transport=tr) == 0
        d = json.loads(cache.read_text())
        assert d["games"]["401"]["by"] == "claude" and "Dampier" in d["games"]["401"]["facts"][0]["text"], d["games"]
        assert "claude" in d["by"] and d["boost"]["at"], d["by"]
        assert any(c["school"] == "Florida" and c.get("by") == "claude" for c in d["coaching"]), d["coaching"]
        # the hourly code refresh an hour later keeps the booster layer (no second Claude call)
        assert not si.boost_due(d, fri + dt.timedelta(hours=1))
        assert si.run(force=True, now=fri + dt.timedelta(hours=1), games=games, items=items, transport=tr) == 0
        assert json.loads(cache.read_text())["games"]["401"]["by"] == "claude"
        assert calls_of(fake) == 1
    # only the Fri 5 PM / Sat 7 AM windows: a Tuesday run never spends a call
    assert not si.boost_due({}, dt.datetime(2026, 11, 17, 10, tzinfo=CT))
    assert si.boost_due({}, dt.datetime(2026, 11, 21, 8, tzinfo=CT))


def test_slate_intel_refreshes_from_code_and_espn():
    import slate_intel as si
    spy = RunSpy()
    now = dt.datetime(2026, 11, 18, 9, 0, tzinfo=CT)
    games = [{"id": 401, "awayTeam": "Utah", "homeTeam": "Arizona", "week": 12,
              "startDate": "2026-11-21T21:00:00Z"}]
    items = [{"title": "Arizona QB Noah Fifita questionable vs Utah", "link": "https://www.espn.com/a",
              "url": "https://www.espn.com/a", "date": "2026-11-17", "source": "ESPN", "teams": ["Arizona"],
              "players": ["Noah Fifita"], "kind": "injury", "topics": ["injury"], "status": "questionable",
              "story": "s1"},
             {"title": "Arkansas State fires Butch Jones, names Ortiz interim head coach",
              "link": "https://www.cbssports.com/b", "url": "https://www.cbssports.com/b", "date": "2026-11-09",
              "source": "CBS Sports", "teams": ["Arkansas State"], "players": [], "kind": None, "topics": [],
              "label": "confirmed", "story": "s2"},
             {"title": "Clemson HC Dabo Swinney Gets Offended After Justifying Why He Doesn't Deserve Firing",
              "link": "https://sports.yahoo.com/c", "date": "2026-11-18", "source": "Yahoo", "teams": ["Clemson"],
              "players": [], "story": "s3"},
             {"title": "Michigan football's Kyle Whittingham responds to accusations he's trying to get fired",
              "link": "https://sports.yahoo.com/d", "date": "2026-11-16", "source": "Yahoo", "teams": ["Michigan"],
              "players": [], "story": "s4"}]
    rank = {"rankings": [{"name": "Playoff Committee Rankings", "type": "cfp", "date": "2026-11-17T08:00Z",
                          "ranks": [{"current": i, "recordSummary": f"{10 - i // 5}-{i // 5}",
                                     "team": {"location": f"Team {i}", "nickname": f"T{i}"}} for i in range(1, 26)]}]}
    cache = TMP / "slate_intel.json"
    with patch.object(si, "CACHE", cache), patch.object(picky_claude, "run", spy), world("missing", "slate_intel.py"):
        tr = httpx.MockTransport(lambda r: httpx.Response(200, json=rank))
        assert si.run(force=True, now=now, games=games, items=items, transport=tr) == 0
        d = json.loads(cache.read_text())
        assert d["by"].startswith("code") and not spy.calls, d.get("by")
        facts = d["games"]["401"]["facts"]
        assert facts and facts[0]["status"] == "questionable" and "Fifita" in facts[0]["text"], facts
        assert d["cfp"]["top"][0] == {"rank": 1, "team": "Team 1", "record": "10-0"} and len(d["cfp"]["top"]) == 25
        assert d["cfp"]["source_name"] == "espn.com" and d["cfp"]["released"] == "Tue Nov 17", d["cfp"]
        assert [c["school"] for c in d["coaching"]] == ["Arkansas State"], d["coaching"]  # not 'deserve firing' talk
        # ESPN changes its format: keep the last good CFP list, log a health line, no crash
        bad = httpx.MockTransport(lambda r: httpx.Response(200, json={"rankings": [{"type": "cfp", "ranks": "??"}]}))
        assert si.run(force=True, now=now + dt.timedelta(hours=1), games=games, items=items, transport=bad) == 0
        d2 = json.loads(cache.read_text())
        assert d2["cfp"] == d["cfp"], d2["cfp"]
        assert "rankings" in Path(os.environ["PICKY_FEED_HEALTH_LOG"]).read_text()
        # empty news: the game keeps its last notes
        assert si.run(force=True, now=now + dt.timedelta(hours=2), games=games, items=[], transport=tr) == 0
        assert json.loads(cache.read_text())["games"]["401"]["facts"] == facts


def test_league_rules_fall_back_to_code_verification():
    import test_rules as tr
    import league_rules as L
    for kind in WORLDS:
        with tempfile.TemporaryDirectory() as td:
            tr._flag_env(td)
            try:
                with world(kind, "league_rules.py"):
                    rep = L.check(L.Repo(), use_live=False)
                    assert rep["flagged"] == {}, rep
                    change = tr._new_fn("bj_deal", "least(25, chips)", "least(50, chips)")
                    rep = L.check(tr._Overlay(change), use_live=False)  # default: Opus only when available
                    assert rep["flagged"] == {}, (kind, rep)
                    assert rep["opus"] == "" or rep["opus"].startswith("opus unavailable"), (kind, rep["opus"])
                    assert "Bets 50-500" in L.section("THE PIT"), L.section("THE PIT")
            finally:
                L._cache["key"] = None
                for k in ("PICKY_RULES_STATE", "PICKY_RULES_LIVE"):
                    os.environ.pop(k, None)


def test_feed_monitor_never_repairs_on_the_schedule():
    """Agentic code repair isn't analysis: by hand only (PICKY_OPUS_MANUAL=1), even with Claude up."""
    import feed_monitor as fm
    import freshness
    st, now = {}, dt.datetime(2026, 11, 17, 10, tzinfo=CT).timestamp()
    kinds = []
    with world("up", "feed_monitor.py"):
        for _ in range(12):
            acts, st = fm.decide({"news": {"ok": False, "missing": False, "why": "stale"}}, st, now)
            kinds += [a[0] for a in acts]
            now += 900
        assert "repair" not in kinds and "kick" in kinds, kinds
        with patch.dict(os.environ, {"PICKY_OPUS_MANUAL": "1"}):
            assert fm.repairable(freshness.source("news"))
    assert freshness.source("insider").monitor == "exit"


def test_news_qa_is_hand_only():
    import news_qa
    rows = [{"title": "t", "link": "https://www.espn.com/x", "source": "ESPN", "teams": [], "date": "2026-11-18"}] * 12
    with world("up", "news_qa.py") as fake, patch.object(news_qa, "_log", lambda rec: rec), \
            patch.object(news_qa, "sample", lambda *a, **k: rows, create=True):
        r = news_qa.run(repair=False, cache={"items": {str(i): x for i, x in enumerate(rows)}})
    assert r.get("result") == "skipped" and calls_of(fake) == 0, r


def test_feed_health_keeps_last_good_and_logs():
    import feed_health
    good = [{"team": "Iowa"}]
    data, ok = feed_health.accept("cfbd:records", {"error": "moved"}, good, kind=list)
    assert data == good and not ok
    data, ok = feed_health.accept("cfbd:records", [], good, kind=list)
    assert data == good and not ok
    data, ok = feed_health.accept("cfbd:records", [{"team": "Utah"}], good, kind=list)
    assert data == [{"team": "Utah"}] and ok
    log = Path(os.environ["PICKY_FEED_HEALTH_LOG"]).read_text()
    assert "cfbd:records" in log and "kept last good" in log, log


def test_cfbd_refresh_keeps_last_good_on_format_change():
    import cfbd
    db = cfbd.connect(TMP / "cfbd.sqlite")
    db.execute("INSERT OR REPLACE INTO datasets VALUES(?,?,?,?)", ("records", 2026, time.time() - 9e4,
                                                                   json.dumps([{"team": "Iowa"}])))
    db.commit()

    def handler(req):
        if req.url.path == "/info":
            return httpx.Response(200, json={"usedCalls": 1})
        return httpx.Response(200, json={"message": "new API, see docs"})
    with httpx.Client(base_url="https://api.example", transport=httpx.MockTransport(handler)) as c:
        cfbd.refresh(db=db, client=c, year=2026, only=["records"])
    row = db.execute("SELECT json FROM datasets WHERE name='records'").fetchone()
    assert json.loads(row[0]) == [{"team": "Iowa"}], row


def test_news_refresh_logs_a_feed_that_goes_empty_and_keeps_its_items():
    import news
    cache = TMP / "news.json"
    item = {"title": "Iowa QB out", "link": "https://www.cbssports.com/x", "date": "2026-11-18T10:00:00+00:00",
            "source": "CBS Sports", "first_seen": "2026-11-18T10:00:00+00:00"}
    cache.write_text(json.dumps({"items": {"k1": item}, "feeds": {"cbs": 34, "espn": 27},
                                 "fetched_at": "2026-11-18T09:00:00+00:00"}))
    with patch.object(news, "CACHE_PATH", cache), patch.object(news, "fetch_all", lambda c, ca, now: ([], {
            "cbs": 0, "espn": "error: ParseError"})), patch.object(news, "fill_dates", lambda c, ca: None):
        news.refresh(force=True, now=dt.datetime(2026, 11, 18, 11, tzinfo=dt.timezone.utc))
    d = json.loads(cache.read_text())
    assert "k1" in d["items"] and d["feeds"]["espn"] == 27, d["feeds"]
    log = Path(os.environ["PICKY_FEED_HEALTH_LOG"]).read_text()
    assert "news:cbs" in log and "news:espn" in log, log


# =============================================================================================================
# Supabase keepalive + a paused / unreachable database
# =============================================================================================================

def test_keepalive_pings_daily_and_records_paused():
    import keepalive
    seen = []

    def ok(req):
        seen.append(str(req.url))
        assert req.headers["apikey"] == keepalive.ANON
        return httpx.Response(200, json=[{"Mascot": "x"}])
    t0 = dt.datetime(2027, 4, 18, 9, tzinfo=CT).timestamp()  # 5 months on: nobody's texting, the DB must stay awake
    r = keepalive.ping(transport=httpx.MockTransport(ok), now=t0)
    assert r["ok"] and len(seen) == 1 and "/rest/v1/" in seen[0], r
    assert keepalive.ping(transport=httpx.MockTransport(ok), now=t0 + 3600)["skipped"]  # once a day
    assert len(seen) == 1
    r = keepalive.ping(transport=httpx.MockTransport(lambda q: httpx.Response(540, text="project paused")),
                       now=t0 + 25 * 3600)
    assert not r["ok"] and r["paused"] and keepalive.db_down(now=t0 + 25 * 3600), r
    assert "supabase" in Path(os.environ["PICKY_FEED_HEALTH_LOG"]).read_text().lower()

    def boom(q):
        raise httpx.ConnectError("no route")
    r = keepalive.ping(transport=httpx.MockTransport(boom), now=t0 + 50 * 3600)
    assert not r["ok"] and not r["paused"], r
    assert keepalive.ANON == __import__("chat").SUPABASE_ANON  # same public key the site ships


def test_feed_monitor_tick_runs_the_keepalive():
    import feed_monitor as fm
    import keepalive
    hit = []
    with patch.object(keepalive, "ping", lambda **k: hit.append(1) or {"ok": True}), \
            patch.object(fm, "observe", lambda now: {}), patch.object(fm, "STATE_FILE", TMP / "fm.json"):
        assert fm.run_pass() == 0
    assert hit == [1]


def test_db_down_gets_an_honest_reply_not_a_500():
    import group_chat as gc
    from fastapi import HTTPException

    class Req:
        def __init__(self, body):
            self.headers = {"x-picky-group-secret": "s"}
            self._b = body

        async def json(self):
            return self._b

    import asyncio
    for exc in (httpx.ConnectError("paused"), HTTPException(status_code=502, detail="supabase_unreachable")):
        async def boom(*a, _e=exc, **k):
            raise _e
        with patch.object(gc, "SECRET", "s"), patch.object(gc, "answer", boom), \
                patch.object(gc.maintenance, "active", lambda *a: False), \
                patch.object(gc, "flag_reply", lambda *a: None), \
                patch.object(gc.group_outbox, "config", return_value={"group_guid": "g1"}):
            r = asyncio.run(gc.group(Req({"handle": "+15555550100", "text": "@Picky who's first", "chat": "g1"})))
        assert r.get("reply") and "league" in r["reply"].lower(), r


# =============================================================================================================
# 3. the calendar: offseason (5 months on) and the next August
# =============================================================================================================

MEMBERS = ["Chuck P", "Ackbar", "TG", "Clam"]


def season_ctx(season, week, games, finals=True):
    return {"season": season, "current_week": week, "teams": MEMBERS, "mode": "leaderboard", "jackpot": {},
            "you": {"team_id": 1, "team_name": "Ackbar"},
            "standings_ytd": [{"team": m, "rank": i + 1, "w": 40 - 3 * i, "l": 20 + 3 * i, "pts": 40 - 3 * i}
                              for i, m in enumerate(MEMBERS)],
            "games_this_week": games, "revealed_game_ids": [g["id"] for g in games] if finals else [],
            "revealed_picks_this_week": {}, "picks_this_week": {}, "week_results": {}, "last_week": {}}


FINAL_GAMES = [{"id": 9001, "away": "Ohio State", "home": "Miami", "line": -3.5, "status": "final", "winner": "Miami",
                "start": "2027-01-19T18:30:00", "tiebreaker": False}]
AUG_GAMES = [{"id": 9101, "away": "Utah", "home": "Iowa", "line": -2.5, "status": None, "winner": None,
              "start": "2027-08-28T11:00:00", "tiebreaker": False},
             {"id": 9102, "away": "Texas", "home": "LSU", "line": -1.5, "status": None, "winner": None,
              "start": "2027-08-28T18:30:00", "tiebreaker": False}]
APRIL = dt.datetime(2027, 4, 18, 12, 0)
AUGUST = dt.datetime(2027, 8, 25, 12, 0)


def test_season_year_from_dates():
    import season
    assert season.season_year(dt.date(2026, 11, 18)) == 2026
    assert season.season_year(dt.date(2027, 1, 12)) == 2026     # bowls / CFP title game belong to 2026
    assert season.season_year(APRIL.date()) == 2026
    assert season.season_year(AUGUST.date()) == 2027
    assert season.date_in_season(11, 1, dt.date(2026, 11, 18)) == dt.date(2026, 11, 1)
    assert season.date_in_season(12, 6, dt.date(2027, 1, 5)) == dt.date(2026, 12, 6)
    assert season.date_in_season(1, 2, dt.date(2027, 1, 5)) == dt.date(2027, 1, 2)


def test_offseason_detected_from_data():
    import season
    ctx = season_ctx(2026, 18, FINAL_GAMES)
    assert season.offseason(ctx, APRIL)
    assert not season.offseason(ctx, dt.datetime(2027, 1, 25, 12))     # a week after the title game: not yet
    sunday = season_ctx(2026, 12, [dict(FINAL_GAMES[0], start="2026-11-14T11:00:00")])
    assert not season.offseason(sunday, dt.datetime(2026, 11, 16, 9))  # all final Monday before week 13 posts
    assert not season.offseason(season_ctx(2027, 1, AUG_GAMES, finals=False), AUGUST)
    assert not season.offseason({"error": "no games"}, APRIL)


def test_offseason_texts_get_sensible_answers():
    import chat
    import router
    ctx = season_ctx(2026, 18, FINAL_GAMES)
    with patch.object(chat, "_now_ct_naive", lambda: APRIL), patch.object(picky_claude, "CLAUDE", MISSING):
        for q in ("when do picks lock?", "who do you like this week?", "what are the pool games?",
                  "what's the line on Iowa this week?"):
            kind = router.classify(q).kind
            assert kind, q
            out = router._fact_answer(kind, ctx, q) or ""
            assert re.search(r"offseason|season'?s over|2026 season is (done|over|in the books)", out, re.I), (q, out)
            assert "Ohio State" not in out and "Miami" not in out, (q, out)
            assert "Chuck P" in out, out  # the final standings: who won it
        st = router._fact_answer("standings", ctx, "standings?") or ""
        assert "Chuck P" in st, st


def test_next_august_week_one_works():
    import chat
    import router
    import season
    ctx = season_ctx(2027, 1, AUG_GAMES, finals=False)
    with patch.object(chat, "_now_ct_naive", lambda: AUGUST), patch.object(picky_claude, "CLAUDE", MISSING):
        out = router._fact_answer("lock", ctx, "when do picks lock?") or ""
    assert "first kickoff" in out and "Sat" in out and "offseason" not in out.lower(), out
    assert season.season_year(AUGUST.date()) == 2027


def test_week_one_dossiers_survive_pruning():
    import dossier
    base = TMP / "dossiers"
    for i, w in enumerate(("14", "15", "16", "1")):  # 2026's last weeks still on disk, then 2027 week 1
        (base / w).mkdir(parents=True)
        os.utime(base / w, (1_800_000_000 + i, 1_800_000_000 + i))
    dossier.prune_weeks(base, keep=3)
    assert sorted(p.name for p in base.iterdir()) == ["1", "15", "16"], sorted(p.name for p in base.iterdir())


def test_no_hardcoded_season_year_in_date_math():
    """`date(2026, ...)` in a code path is a bomb for 2027. Fixtures/self-tests excepted."""
    bad = []
    for f in sorted(HERE.glob("*.py")):
        if f.name.startswith("test_") or f.name in ("saturday_drill.py", "weights_fit.py", "close_cover.py"):
            continue
        for i, ln in enumerate(f.read_text().splitlines(), 1):
            if re.search(r"\b(?:dt|_dt|datetime)\.(?:date|datetime)\(2026,", ln) and "self_test" not in ln:
                bad.append(f"{f.name}:{i}: {ln.strip()[:100]}")
    allowed = {b for b in bad if b.startswith("league_rules.py:")  # league_rules' own lock-time self checks
               or b.startswith("factstore.py:") and "FIXTURE_FETCHED =" in b}  # the frozen test fixture's capture time
    assert not (set(bad) - allowed), sorted(set(bad) - allowed)


def test_stale_cfp_list_drops_in_the_offseason():
    import slate_intel as si
    cache = TMP / "si-old.json"
    cache.write_text(json.dumps({"fetched_at": dt.datetime(2027, 4, 18, 8, tzinfo=dt.timezone.utc).isoformat(),
                                 "as_of": "x", "games": {}, "coaching": [],
                                 "cfp": {"released": "Sun Dec 6", "released_on": "2026-12-06", "source_name": "espn.com",
                                         "top": [{"rank": 1, "team": "Miami"}]}}))
    with patch.object(si, "CACHE", cache):
        assert si.cfp(now=dt.datetime(2027, 4, 18, 9, tzinfo=CT)) is None
        assert si.cfp(now=dt.datetime(2027, 1, 12, 9, tzinfo=CT)) is not None  # bowl season: the final list stands


def test_offseason_refresh_jobs_idle_cleanly():
    import dossier
    import slate_intel as si

    class NoSlate:
        def get(self, q):
            return [{"cfb_season": 2026}] if "cfb_season" in q and "limit=1" in q else []
    with patch.object(dossier, "open_slate", lambda db, week=None: (2026, None, [])):
        assert dossier.run(db=NoSlate(), base=TMP / "d-off", now=dt.datetime(2027, 4, 18, 9, tzinfo=CT))["ok"]
    cache = TMP / "si-off.json"
    with patch.object(si, "CACHE", cache):
        assert si.run(force=True, now=dt.datetime(2027, 4, 18, 9, tzinfo=CT), games=[], items=[],
                      transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"rankings": []}))) == 0


if __name__ == "__main__":
    fails = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ok  {name}")
            except Exception as e:  # noqa: BLE001
                fails += 1
                print(f"FAIL  {name}: {e!r}"[:600])
    print("\nALL GREEN" if not fails else f"\n{fails} FAILED")
    sys.exit(1 if fails else 0)
