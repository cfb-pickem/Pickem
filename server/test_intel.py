"""
Deterministic tests for game intel (dossier_note) and the grounding filter
(ungrounded / grounded_check): intel only for a named game, never URLs,
never in the lean; ungrounded players, injuries and stats are dropped.

Run:  ./.venv/bin/python3 -I test_intel.py
"""

from __future__ import annotations

import datetime as dt
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, ".")
import chat  # noqa: E402
from test_leader import G2, ctx  # noqa: E402

FAILED = []


def check(name, ok, got=None):
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else f"\n     got: {got!r}"))
    if not ok:
        FAILED.append(name)


now = dt.datetime.now(chat.CT)
DOS = {
    "generated_at": now.isoformat(timespec="seconds"), "away": "Oregon State", "home": "Boise State",
    "summary": "Boise State QB Maddux Madsen is questionable with a shoulder injury.",
    "facts": [{"text": "Boise State QB Maddux Madsen is questionable with a shoulder injury.", "kind": "injury",
               "source_url": "https://www.idahostatesman.com/sports/x.html", "source_name": "Idaho Statesman",
               "date": now.date().isoformat(), "players": ["Maddux Madsen"]},
              {"text": "Oregon State named Gevani McCoy its starter for the rest of the season.", "kind": "fact",
               "source_url": "https://osubeavers.com/news/x", "source_name": "osubeavers.com",
               "date": now.date().isoformat(), "players": ["Gevani McCoy"]}],
    "opinions": [{"text": "A beat writer expects Boise to lean on the run game.", "kind": "insider_opinion",
                  "source_url": "https://example.com/a", "source_name": "", "date": now.date().isoformat()}],
    "rumors": [{"text": "Message-board chatter says Boise's top corner could sit.", "kind": "rumor",
                "source_url": "https://www.reddit.com/r/x/1", "source_name": "reddit", "date": now.date().isoformat()}],
}

with tempfile.TemporaryDirectory() as d:
    real = chat.DOSSIER_DIR
    chat.DOSSIER_DIR = Path(d)
    try:
        (Path(d) / "11").mkdir()
        (Path(d) / "11" / "2.json").write_text(json.dumps(DOS))
        c = ctx()
        n = chat.dossier_note(c, "Is Madsen playing for Boise State this week?")
        check("intel for a named game", n.startswith("GAME INTEL, Oregon State at Boise State"), n)
        check("injury first, labeled, dated, outlet named",
              "  - injury report, status QUESTIONABLE (" in n and "Idaho Statesman): Boise State QB Maddux Madsen" in n, n)
        st = chat.intel_status(c, "Is Madsen playing for Boise State?")
        check("status map from the intel", st == {"madsen": "questionable"}, st)
        check("contradiction dropped", chat.contradicts_intel("Madsen is back and good to go.", st) is not None)
        check("consistent line kept", chat.contradicts_intel("Madsen is questionable, so keep an eye on it.", st) is None)
        check("'won't be back' is not a contradiction",
              chat.contradicts_intel("Madsen won't be back to full speed soon.", st) is None)
        f2 = chat.grounded_check(c, "Maddux Madsen questionable GAME INTEL,", "Is Madsen playing for Boise State?")
        check("grounded_check applies the intel status", not f2("Madsen returns this week and should help."))
        check("opinion and rumor labeled", "opinion, not fact" in n and "RUMOR, unconfirmed" in n, n)
        check("never a URL, never a bare domain", "http" not in n and "osubeavers.com" not in n
              and "reddit.com" not in n, n)
        check("color only, never the lean", "never changes the LEAN" in n, n)
        check("no intel for a general question", chat.dossier_note(c, "Who's winning the pool?") == "")
        check("no intel for 'break down the games'", chat.dossier_note(c, "Break down the games") == "")
        check("no intel for a final game", chat.dossier_note(c, "How did Wake Forest look?") == "")
        stale = dict(DOS, generated_at=(now - dt.timedelta(days=9)).isoformat())
        (Path(d) / "11" / "2.json").write_text(json.dumps(stale))
        check("stale dossier ignored", chat.dossier_note(c, "Boise State injuries?") == "")
        (Path(d) / "11" / "2.json").write_text(json.dumps(DOS))
        # lean untouched: same signals with and without a dossier on disk
        g = dict(G2, fpi_margin=10.0)
        notes = {"market": {"details": "BOIS -16.5"}, "home": {"abbr": "BOIS"}, "away": {"abbr": "ORST"}}
        with_d = chat.matchup_signals(g, notes, None)
        (Path(d) / "11" / "2.json").unlink()
        without = chat.matchup_signals(g, notes, None)
        check("dossier never moves the lean", with_d == without, (with_d, without))
        (Path(d) / "11" / "2.json").write_text(json.dumps(DOS))
        ut = chat._user_turn(c, "Any injuries for Boise State vs Oregon State?")
        check("injury rider points at the intel", "GAME INTEL block below has public reports" in ut
              and "GAME INTEL, Oregon State" in ut, ut[-900:])
    finally:
        chat.DOSSIER_DIR = real

# -- grounding filter -----------------------------------------------------------------
corpus = ("Boise State: record 8-2. Season leaders, Boise State: QB Maddux Madsen 2,176 yds, 18 TD. "
          "GAME INTEL, Oregon State at Boise State: Boise State QB Maddux Madsen is questionable with a shoulder injury. "
          "ESPN FPI: Oregon State 13.1%, Boise State 86.9%.")
cn = f" {chat.cfbd.norm(corpus)} "
nums = {x.replace(",", "") for x in chat._NUMS.findall(corpus)}
players = {"maddux madsen", "sire gaines", "gevani mccoy"}
skip = {"chuck p", "ackbar", "clam", "dear leader"}


def bad(sent, intel=True):
    return chat.ungrounded(sent, cn, nums, skip, players, None, intel)


check("grounded player + stat passes", bad("Maddux Madsen has 2,176 yards and 18 TD this year.") is None)
check("last name alone passes", bad("Madsen is questionable, per reports.") is None)
check("real player not in the prompt is dropped", (bad("Sire Gaines will run wild on Saturday.") or "").startswith("player"))
check("invented name next to a position word is dropped",
      (bad("Their backup QB Tommy Fakename looked sharp in practice.") or "").startswith("player"))
check("coach / celebrity without a position word passes", bad("Even Nick Saban would fade this line.") is None)
check("invented stat is dropped", (bad("Madsen threw for 350 yards last week.") or "").startswith("stat"))
check("prompt percentage passes", bad("FPI gives Boise State 86.9% to win outright.") is None)
check("100% idiom passes", bad("I'm 100% on the value side here.") is None)
check("member names pass", bad("Chuck P and Ackbar both like Boise State.") is None)
check("team names pass", bad("Oregon State Beavers fans are nervous.") is None)
check("unnamed injury claim without intel is dropped",
      bad("Boise's top receiver is out for the season.", intel=False) == "injury claim with no intel")
check("hedged injury line passes without intel",
      bad("Nobody knows who is out, there's no official injury report.", intel=False) is None)
check("'broken clock' isn't an injury", bad("You didn't miss every one like a broken clock.", intel=False) is None)
check("'broken clock' without a hedge either", bad("Your picks are a broken clock this year.", intel=False) is None)
check("a real unnamed injury claim still is", bad("Their top corner has a torn ACL.", intel=False) is not None)
check("casino 'busted out' isn't an injury", bad("DaddyFreeman has 1,575 chips while you are busted out at zero.",
                                                   intel=False) is None)
check("'is out of the playoff race' isn't either", bad("SMU is out of the playoff race.", intel=False) is None)
check("unnamed 'is questionable' still is", bad("Their starting corner is questionable.", intel=False) is not None)
check("calendar words don't trip it", bad("On Saturday November 21 the starters play at noon.") is None)
f = chat.grounded_check(ctx(), corpus)
check("rounded prompt number passes", f("He completes about 87% of the time, says FPI."))
check("far-off number still dropped", not f("Boise State wins 95% of the time."))
check("grounded_check builds a predicate", callable(f) and f("Madsen is questionable, per reports."))
check("no corpus -> no check", chat.grounded_check(ctx(), "") is None)

st = {"toney": "out"}
check("'was out ... but expected back soon' is a contradiction",
      chat.contradicts_intel("Malachi Toney was out against Duke, but he's expected back soon.", st) is not None)
src = chat.source_numbers("  FOR Miami: SP+ (ratings + 2.5 home field): projects Miami by 19.9, so Miami covers by 2.9\n"
                          "  ESPN FPI: projects Miami by 21.3, so Miami covers by 4.3")
check("SP+ number called FPI is dropped", chat.misattributed("FPI projects Miami by 19.9, an easy cover.", src))
check("right attribution passes", chat.misattributed("SP+ projects Miami by 19.9.", src) is None
      and chat.misattributed("FPI has Miami by 21.3.", src) is None)
check("FPI not in the prompt at all", chat.misattributed("FPI projects Miami by 19.9.",
                                                         chat.source_numbers("SP+ projects Miami by 19.9")))
check("two sources in one sentence: not judged", chat.misattributed("SP+ and FPI both have Miami by 20.", src) is None)

if FAILED:
    print(f"\n{len(FAILED)} FAILED")
    sys.exit(1)
print("\nALL PASS")
