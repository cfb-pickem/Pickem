"""
Kill-switch test (checklist 9): Picky keeps working with Anthropic down.

Every Anthropic call goes through picky_claude.run (Opus, guard, empty MCP, no
API key, usage log). With PICKY_NO_ANTHROPIC=1 and every claude spawn rigged
to blow up: live riders, lock reminders, group posts (recap / preview /
saturday / pings), Picky's weekly pick logic and the dossier (pure code over
the news cache) all still produce output. No network, no claude, no GPU.

Run:  ./.venv/bin/python3 -I test_anthropic_down.py
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
TMP = Path(tempfile.mkdtemp(prefix="picky-anthropic-down-"))
# Anthropic-fed artifacts that never got written (Anthropic was never up), private dir in a temp
for k, name in (("PICKY_HOT_TAKES", "hot_takes_bank.md"), ("PICKY_INSIDER", "insider_bank.md"),
                ("PICKY_INSIDER_WIRE", "insider_wire.json"),
                ("PICKY_LEAGUE_MATERIAL", "league_material.json"), ("PICKY_BANTER_BANK", "banter_bank.json"),
                ("PICKY_BURN_BOOK", "burn_book.json"), ("PICKY_DOSSIER_DIR", "dossiers"),
                ("PICKY_GAME_CARD_DIR", "game_cards")):
    os.environ[k] = str(TMP / "missing" / name)
os.environ["PICKY_PRIVATE_DIR"] = str(TMP / "private")
os.environ["PICKY_CLAUDE_USAGE_DIR"] = str(TMP / "usage")
os.environ["PICKY_NO_ANTHROPIC"] = "1"
os.environ["PICKY_USE_CLAUDE"] = "1"  # the dormant hosted path asked for: the kill switch still wins
(TMP / "private").mkdir()

import picky_claude  # noqa: E402

FAILED: list[str] = []
SPAWNS: list = []


def check(name, ok, got=None):
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else f"\n     got: {got!r}"[:900]))
    if not ok:
        FAILED.append(name)


_real_run = subprocess.run


def _trap(*a, **k):
    """subprocess.run stand-in: any claude spawn is a failure (and recorded)."""
    cmd = a[0] if a else k.get("args")
    if any("claude" in str(x) for x in (cmd if isinstance(cmd, (list, tuple)) else [cmd])):
        SPAWNS.append(cmd)
        raise AssertionError(f"claude spawned with the kill switch on: {cmd}")
    return _real_run(*a, **k)


subprocess.run = _trap

# -- picky_claude itself -----------------------------------------------------------------------
check("model is Opus 5.5", picky_claude.MODEL == "claude-opus-5-5", picky_claude.MODEL)
try:
    picky_claude.run("hi", timeout=5)
    check("run() refuses with the kill switch on", False)
except picky_claude.AnthropicDisabled:
    check("run() refuses with the kill switch on", not SPAWNS, SPAWNS)
cmd = picky_claude.command("", "json", {"type": "object"})
check("command: Opus, strict empty MCP, no tools", cmd[cmd.index("--model") + 1] == "claude-opus-5-5"
      and "--strict-mcp-config" in cmd and cmd[cmd.index("--tools") + 1] == ""
      and json.loads(Path(cmd[cmd.index("--mcp-config") + 1]).read_text()) == {"mcpServers": {}}, cmd)
check("command: tools None = no --tools flag", "--tools" not in picky_claude.command(None, "text"))
with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test", "ANTHROPIC_AUTH_TOKEN": "t"}):
    e = picky_claude.env()
check("env strips the API key", "ANTHROPIC_API_KEY" not in e and "ANTHROPIC_AUTH_TOKEN" not in e)

# a normal (switch off) call: a fake claude binary records argv + env; the call lands in today's log
fake = TMP / "claude"
fake.write_text("#!/bin/sh\necho \"$@\" > \"$(dirname \"$0\")/argv\"\n"
                "echo \"key=${ANTHROPIC_API_KEY:-none}\" >> \"$(dirname \"$0\")/argv\"\ncat > /dev/null\n"
                "echo '{\"structured_output\": {\"ok\": 1}}'\n")
fake.chmod(0o755)
with patch.dict(os.environ, {"PICKY_NO_ANTHROPIC": "0", "ANTHROPIC_API_KEY": "sk-test"}), \
        patch.object(picky_claude, "CLAUDE", str(fake)), patch.object(subprocess, "run", _real_run):
    p = picky_claude.run("prompt", timeout=30, caller="unit", tools="", schema={"type": "object"})
argv = (TMP / "argv").read_text()
check("a real call runs claude -p on claude-opus-5-5, no key", p.returncode == 0
      and "-p --model claude-opus-5-5" in argv and "--strict-mcp-config" in argv and "key=none" in argv, argv)
u = picky_claude.usage(1)
check("usage log: one call by caller 'unit'", u and u[0]["calls"] == 1 and "unit" in u[0]["by_caller"]
      and u[0]["failed"] == 0, u)

# -- static: nothing spawns claude except picky_claude.py ----------------------------------------
SPAWN_RX = re.compile(r"""bin/claude|(?:CLAUDE|claude)["']?\s*,\s*["']-p["']|["']-p["']\s*,\s*["']--model""")
offenders = []
for f in sorted(list(HERE.glob("*.py")) + list(HERE.glob("eval/*.py")) + list((HERE.parent / "tools" / "picky-lora").glob("*.py"))
                + list((HERE.parent / "tools" / "picky-lora").glob("*.sh")) + list((HERE.parent / "tools").glob("*.py"))):
    if f.name in ("picky_claude.py", "test_anthropic_down.py"):
        continue
    for i, line in enumerate(f.read_text().splitlines(), 1):
        if SPAWN_RX.search(line):
            offenders.append(f"{f.name}:{i}: {line.strip()[:120]}")
check("no claude -p outside picky_claude.py", not offenders, offenders)
check("the news digest has no Anthropic dependency",
      not re.search(r"picky_claude|run_claude|anthropic", (HERE / "news.py").read_text(), re.I))

# -- offline writers: kill switch = fail fast, no spawn, last good copy stays ------------------------
import dossier  # noqa: E402
import group_posts as gp  # noqa: E402
import insider_refresh  # noqa: E402
import feed_monitor  # noqa: E402

for name, fn in (("dossier.run_claude", lambda: dossier.run_claude("x")),
                 ("group_posts.run_writer", lambda: gp.run_writer("x")),
                 ("insider_refresh.run_claude", lambda: insider_refresh.run_claude("x", "", 5)),
                 ("feed_monitor.call_claude", lambda: feed_monitor.call_claude("x", TMP))):
    try:
        fn()
        check(f"{name} refuses", False)
    except picky_claude.AnthropicDisabled:
        check(f"{name} refuses", True)

import banter  # noqa: E402
bank = TMP / "banter_bank.json"
bank.write_text(json.dumps({"generated_at": "2026-11-01T10:00:00-05:00", "categories": {"beer": ["keep me"]}}))
try:
    banter.refresh(force=True, out=bank)
    check("banter refresh raises", False)
except picky_claude.AnthropicDisabled:
    check("banter refresh keeps the last good bank", json.loads(bank.read_text())["categories"] == {"beer": ["keep me"]})

# -- live riders with the Anthropic-fed files missing ----------------------------------------------
import chat  # noqa: E402
import game_cards  # noqa: E402

check("dormant hosted path off even with PICKY_USE_CLAUDE (kill switch)", chat.CLAUDE is None)
ctx = {"season": 2026, "current_week": 12, "teams": ["Ackbar", "Chuck P"], "you": {"team_name": "Ackbar"},
       "games_this_week": [{"id": 1, "away": "Utah", "home": "Iowa", "line": -3.5, "status": "scheduled",
                            "winner": None, "start": "2026-11-21T14:30:00"}]}
riders = {"takes_note": lambda: chat.takes_note("hot take on the SEC?"),
          "insider_note": lambda: chat.insider_note("what's the latest on realignment?"),
          "voice_note": lambda: chat.voice_note("hot take on the SEC?"),
          "material_note": lambda: chat.material_note(ctx, "roast Ackbar"),
          "take_note": lambda: chat.take_note(ctx, "who do you like in Utah at Iowa?"),
          "dossier_note": lambda: chat.dossier_note(ctx, "any injuries in Utah at Iowa?"),
          "card_note": lambda: game_cards.card_note(ctx, "who should I take in Utah at Iowa?"),
          "banter ammo": lambda: banter.ammo_rider("you suck at this", "insult")}
for name, fn in riders.items():
    try:
        out = fn()
        check(f"live rider {name} degrades quietly", isinstance(out, str), out)
    except Exception as e:  # noqa: BLE001
        check(f"live rider {name} degrades quietly", False, repr(e))

# -- group posts + lock reminders: the real writer path, Anthropic down -----------------------------
NOW = dt.datetime(2026, 11, 21, 7, 5, tzinfo=gp.CT)
G1 = {"GameId": 1, "Away": "Utah", "Home": "Iowa", "line": -3.5, "week": 12, "Start (CT)": "2026-11-21T11:00:00",
      "picked": True, "AwayPts": 17, "HomePts": 24, "winner": "Iowa"}
G2 = {"GameId": 2, "Away": "Texas", "Home": "Texas A&M", "line": 2.5, "week": 12, "Start (CT)": "2026-11-21T18:30:00",
      "picked": True}


class FakeLeague:
    now = NOW
    season = 2026
    names = {"Ackbar": "Ackbar", "Chuck P": "Charlie", "TG": "TG"}
    tid = {1: "Ackbar", 2: "Chuck P", 3: "TG"}
    db = None

    def nick(self, t):
        return self.names.get(t, t)

    def week_games(self, w, pool_only=True):
        return [G1, G2]

    def lock_time(self, w):
        return dt.datetime(2026, 11, 21, 11, 0, tzinfo=gp.CT)

    def submission(self, w):
        return {"Ackbar": False, "Chuck P": True, "TG": False}

    def picky_picks(self):
        return []

    def picks(self, w):
        return [{"team_id": 1, "game_id": 1, "pick": "Utah"}, {"team_id": 2, "game_id": 1, "pick": "Iowa"}]


lg = FakeLeague()
CARD = {1: {"label": "Iowa -3.5", "why": "market", "made": True}, 2: {"label": "Texas +2.5", "why": "SP+", "made": False}}
with patch.object(gp, "picky_card", lambda lg_, w: CARD), patch.object(gp, "stakes_facts", lambda lg_: [
        "Standings (points): 1st Charlie 30; 2nd Ackbar 28. Cut line for the top 4 playoff spots: 25 points."]), \
        patch.object(gp, "load_take", lambda w, gid: None), \
        patch.object(gp, "recap_facts", lambda lg_, w: ([
            "Week 11 records: Charlie 3-1; Ackbar 1-3.", "Best week 11 record (3-1): Charlie.",
            "Worst week 11 record (1-3): Ackbar.",
            "WORST PICK: Utah +3.5 (Utah at Iowa) missed the line by 3.5 points; final Utah 17, Iowa 24. Taken by Ackbar.",
            "BEST CALL: Iowa -3.5 (Utah at Iowa) covered by 3.5; only 1 of 3 had it: Charlie.",
            "YOUR OWN WEEK (Picky): 2-2 in week 11: Iowa -3.5 hit. Your season: 20-24."], {})), \
        patch.object(gp, "race_text", lambda lg_: ("Playoff race: Charlie is in.", {}, {})), \
        patch.object(gp, "standings", lambda lg_: []):
    rem = gp.make_reminder(lg, 12)
    check("lock reminder still goes out (code line, stragglers named)",
          rem and "Sat 11 AM CT" in rem[0] and "Ackbar" in rem[0] and "TG" in rem[0] and "Charlie" not in rem[0], rem)
    rec = gp.make_recap(lg, 11)
    check("recap still goes out (template + race)", rec and rec[0].startswith("Week 11 is in the books")
          and any("Worst pick: Utah" in t for t in rec) and any(t.startswith("Me: 2-2") for t in rec)
          and "My season: 20-24" in " ".join(rec) and rec[-1] == "Playoff race: Charlie is in.", rec)
    pre = gp.make_preview(lg, 12)
    check("preview still goes out (code template)", pre and "Sat 11 AM CT" in pre[0] and "first kickoff" in pre[0].lower()
          and "I'm on Iowa -3.5" in pre[1] and "Texas +2.5 (a lean" in pre[1] and "Cut line" in pre[-1], pre)
    sat = gp.make_saturday(lg, 12)
    check("saturday post still goes out (code template)", sat and "I'm on Iowa -3.5" in sat[0], sat)
    ping = gp.make_ping(lg, [(G1, 17.0, 24.0)])
    check("final-score ping still goes out (code line)", ping and "Iowa -3.5 covers (Utah 17, Iowa 24), Charlie cashes"
          in ping[0], ping)
    for t in (rem or []) + (rec or []) + (pre or []) + (sat or []):
        if gp._PHONE.search(t) or re.search(r"\b(exhibition|as an ai|i'?m a bot)\b", t, re.I):
            check("templates stay clean", False, t)

# -- Picky's weekly picks: deterministic, local only ---------------------------------------------------
import picky_picks  # noqa: E402
d = picky_picks.decide({"GameId": 1, "Away": "Utah", "Home": "Iowa", "line": -3.5},
                       {"market": {"details": "IOWA -6.5"}, "home": {"abbr": "IOWA"}, "away": {"abbr": "UTAH"}}, None)
check("weekly pick logic works with Anthropic down", d.get("pick") == "Iowa" and d.get("method") == "value", d)

# -- dossier: pure code over the news cache, no model of any kind -------------------------------------
NEWS = [{"title": "Iowa QB Mark Gronowski questionable vs Utah", "link": "https://www.example-news.com/iowa/qb",
         "date": "2026-11-19T12:00:00+00:00", "source": "ESPN", "teams": ["Iowa"], "players": ["Mark Gronowski"],
         "kind": "injury", "topics": ["injury"], "status": "questionable", "summary": "Day-to-day, per the coach."},
        {"title": "Utah's defense ranks 12th nationally", "link": "https://www.example-news.com/utah/d",
         "date": "2026-11-18T12:00:00+00:00", "source": "CBS Sports", "teams": ["Utah"], "players": [],
         "kind": None, "topics": [], "status": None, "summary": ""}]
import httpx  # noqa: E402

G = {"GameId": 1, "Away": "Utah", "Home": "Iowa", "line": -3.5, "Status": None, "Start (CT)": "2026-11-21T14:30:00",
     "tiebreaker": False}
fri = dt.datetime(2026, 11, 20, 17, 30, tzinfo=dossier.CT)  # inside the weekly Opus window: the switch still wins
dossier._reddit_cache = ("ok", [])
NET = []
with httpx.Client(transport=httpx.MockTransport(lambda req: NET.append(str(req.url)) or httpx.Response(500))) as c:
    check("weekly Opus layer not due with the kill switch on", not dossier.opus_due(None, "fri", fri))
    dd = dossier.build_one(c, G, 2026, 12, "fri", fri, None, opus=dossier.opus_due(None, "fri", fri), items=NEWS)
facts = dd["facts"] + dd["opinions"] + dd["rumors"]
check("dossier built from code with Anthropic down", dd["researcher"].startswith("code") and "opus" not in dd
      and len(facts) == 2 and all(f["by"] == "code" for f in facts), dd.get("researcher"))
check("code claims keep their lead's URL, date and injury status",
      dd["facts"][0]["source_url"] == NEWS[0]["link"] and dd["facts"][0]["date"] == "2026-11-19"
      and dd["facts"][0]["status"] == "questionable" and dd["summary"].startswith("Iowa QB Mark Gronowski"), dd["facts"])
check("no network or model call on the code path", not NET, NET)
check("no analyst take invented (Anthropic down, no previous)", "analyst_take" not in dd, dd.get("analyst_take"))
dossier._reddit_cache = None

check("no claude spawned anywhere with the kill switch on", not SPAWNS, SPAWNS)
if FAILED:
    print(f"\n{len(FAILED)} FAILED")
    sys.exit(1)
print("\nALL PASS")
