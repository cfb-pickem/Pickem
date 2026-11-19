"""
Weekly picks-up announcement + deep breakdown (week_breakdown.py): favorite
framing from `line`, the fact verifier (a made-up number is rejected), dedupe by
week key, quiet hours (overnight drops wait for 9am), the breakdown going out
after the announcement, and the Thursday refresh. Offline: no Opus, no network.

Run:  ./.venv/bin/python3 -I test_week_breakdown.py
"""

from __future__ import annotations

import datetime as dt
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
_TMP = tempfile.mkdtemp()
os.environ["PICKY_PRIVATE_DIR"] = _TMP
import group_outbox as ob  # noqa: E402
import week_breakdown as wb  # noqa: E402

CT = wb.CT
fails = 0


def check(cond, msg, detail=None):
    global fails
    print(("ok   " if cond else "FAIL ") + msg + ("" if cond or detail is None else f": {detail!r}"))
    fails += not cond


# --- favorite framing from `line` (home adjustment, negative = home favored) ---
check(wb.favorite_text("Northwestern", "Minnesota", 2.5) == "Northwestern -2.5 at Minnesota", "away favorite: line +2.5")
check(wb.favorite_text("Iowa", "Illinois", "8.5") == "Iowa -8.5 at Illinois", "away favorite from a string line")
check(wb.favorite_text("Baylor", "Texas Tech", -3.5) == "Baylor at Texas Tech -3.5", "home favorite: line -3.5")
check(wb.favorite_text("A", "B", 0) == "A at B (pick'em)", "pick'em")
lock = dt.datetime(2026, 11, 21, 11, 0, tzinfo=CT)
check(wb.lock_text(lock) == "Saturday 11am", "lock text Saturday 11am")
check(wb.lock_text(dt.datetime(2026, 11, 27, 18, 30, tzinfo=CT)) == "Friday 6:30pm", "lock text Friday 6:30pm")

# --- verifier ---
FACTS = ["League line (locked): Iowa -8.5",
         "Iowa (8-2): ATS 7-3 (fav 5-2); SP+ #16 (offense #52, defense #4)",
         "Run game (rushing EPA/play): Illinois offense #61 vs Iowa defense #2 (rushing EPA allowed)",
         "Illinois QB Katin Houser had nose surgery Monday (ESPN, 2026-11-16)",
         "Forecast for kickoff (Open-Meteo, pulled Tue Nov 17): 64F, wind 10 mph gusting to 16, 7% chance of rain",
         "THE LEAN (the pool card, graded vs the league line): Illinois +8.5 (a slight lean at best)"]
LEAN = {"side": "Illinois", "label": "Illinois +8.5", "say": "a slight lean at best"}
good = ("Iowa -8.5 at Illinois\nIowa's run D is #2 and Katin Houser had nose surgery.\n"
        "Forecast (Open-Meteo, pulled Tue Nov 17): 64F, gusts to 16.\nLean: Illinois +8.5 - slight lean at best.")
check(wb.verify(good, FACTS, LEAN) == [], "a fully sourced text passes")
bad_num = good.replace("#2 and", "#3 and")
p = wb.verify(bad_num, FACTS, LEAN)
check(any("ranks not in the facts: #3" in x for x in p), "a made-up rank (#3) is rejected even though 3 is in the facts")
p = wb.verify(good.replace("16.", "16.\nSP+ projects Iowa by 9.9."), FACTS, LEAN)
check(any("9.9" in x for x in p), "a made-up projection (9.9) is rejected")
p = wb.verify(good.replace("64F", "41F"), FACTS, LEAN)
check(any("41" in x for x in p), "a made-up forecast temp is rejected")
p = wb.verify(good + "\nIowa is 5-3 on the road.", FACTS, LEAN)
check(any("5-3" in x for x in p), "a W-L made of real numbers but not in the facts is rejected")
p = wb.verify(good.replace("Katin Houser", "Luke Altmyer"), FACTS, LEAN)
check(any("Altmyer" in x for x in p), "a player name not in the facts is rejected")
p = wb.verify(good.replace("Lean: Illinois +8.5", "Lean: Iowa -8.5"), FACTS, LEAN)
check(any("Lean: Illinois +8.5" in x for x in p), "a lean that doesn't match the card is rejected")
p = wb.verify(good.replace("Lean:", "I picked Illinois.\nLean:"), FACTS, LEAN)
check(any("hidden" in x for x in p), "talking about Picky's own pick is rejected")
p = wb.verify(good + "\nAckbar is laying it.", FACTS, LEAN, member_names=["Ackbar"])
check(any("Ackbar" in x for x in p), "naming a league member is rejected")
check(wb.verify(good.replace("Forecast", "Weather"), FACTS, LEAN, need_forecast=True) != [],
      "weather without the word 'forecast' is rejected when required")

# --- compose: rejected drafts get one retry, then failing lines drop, else the plain template ---
GF = {"game_id": 1, "header": "Iowa -8.5 at Illinois", "lean": LEAN, "facts": FACTS + ["Market (ESPN/DraftKings): Iowa -8.5"]}
calls = []


def writer_retry(prompt):
    calls.append(prompt)
    t = bad_num if len(calls) == 1 else good
    return {"intro": "Week 12 breakdown, read it before you pick.", "games": [{"game_id": 1, "text": t}]}


texts, rep = wb.compose([GF], 12, "Sat 11 AM CT", writer=writer_retry)
check(len(calls) == 2 and "REJECTED" in calls[1] and texts[-1] == good and rep[1] == "opus draft 2",
      "a rejected draft is retried with the checker's problems, the fixed one ships")
check(texts[0].startswith("Week 12"), "the intro rides first")
texts, rep = wb.compose([GF], 12, "Sat 11 AM CT", writer=lambda p: {"intro": "", "games": [{"game_id": 1, "text": bad_num}]})
check(rep[1] == "opus, failing lines dropped" and "#3" not in texts[-1] and "Lean: Illinois +8.5" in texts[-1],
      "a line with a made-up number is dropped, the rest keeps")


def boom(p):
    raise RuntimeError("anthropic down")


texts, rep = wb.compose([GF], 12, "Sat 11 AM CT", writer=boom)
check(rep[1] == "plain template" and texts[-1].endswith("Lean: Illinois +8.5") and not wb.verify(texts[-1], GF["facts"], LEAN),
      "writer down: the plain code template goes out and passes the verifier")

# --- the trigger: dedupe, quiet hours, announcement then breakdown ---


class FakeLeague:
    season = 2026
    names = {"Ackbar": "Ackbar"}

    def __init__(self, n=4):
        self.games = [{"GameId": i, "Away": f"Away{i}", "Home": f"Home{i}", "line": 2.5 if i % 2 else -3.5,
                       "Start (CT)": "2026-11-21T11:00:00"} for i in range(1, n + 1)]

    def week_games(self, w):
        return self.games

    def lock_time(self, w):
        return lock


sent = []


def send(kind, texts, key, **kw):
    sent.append((kind, key, texts))
    ob.post(kind, texts, key, dry_run=False, now=NOW[0], echo=lambda *a: None, **kw)


ob.save_config(group_guid="any;+;test", dry_run=False)
prepared = []
wb.prepare = lambda lg, w, games, lk, now, writer=None: (prepared.append(w), wb.save_state(
    lg.season, w, wb.load_state(lg.season, w) | {"texts": ["intro", "game text"], "prepared": now.timestamp()}))[1] \
    or wb.load_state(lg.season, w)
NOW = [dt.datetime(2026, 11, 17, 2, 0, tzinfo=CT)]
check(wb.tick_picks_up(FakeLeague(3), 12, NOW[0], send, ready=lambda w, g: True) == "waiting-games" and not sent,
      "fewer than 4 pool games: wait")
r = wb.tick_picks_up(FakeLeague(), 12, NOW[0], send, ready=lambda w, g: True)
check(not sent and "prepared" in r, "games land at 2am: nothing posts (quiet hours), the breakdown is prepared offline")
NOW[0] = dt.datetime(2026, 11, 17, 8, 30, tzinfo=CT)
wb.tick_picks_up(FakeLeague(), 12, NOW[0], send, ready=lambda w, g: True)
check(not sent, "8:30am after an overnight drop: still waiting for 9am")
NOW[0] = dt.datetime(2026, 11, 17, 9, 0, tzinfo=CT)
wb.tick_picks_up(FakeLeague(), 12, NOW[0], send, ready=lambda w, g: True)
check([s[0] for s in sent] == ["announce"] and len(sent[0][2]) == 1, "9:00: ONE text goes out")
one = sent[0][2][0]
check(one.startswith("Week 12 picks are up, lock is Saturday 11am.") and "Away1 -2.5 at Home1" in one
      and "Away2 at Home2 -3.5" in one and "@Picky me or DM me" in one and len(one) <= 450,
      "the one text = lock, framed lines, @Picky, under 450 characters", one)
NOW[0] = dt.datetime(2026, 11, 17, 9, 10, tzinfo=CT)
check(wb.tick_picks_up(FakeLeague(), 12, NOW[0], send, ready=lambda w, g: True) == "done" and len(sent) == 1,
      "dedupe: the next tick posts nothing")
check(len(list(ob.OUTBOX.glob("*.json"))) == 1, "exactly one outbox item for the week")
check(len(prepared) == 1, "the breakdown was prepared once")
NOW[0] = dt.datetime(2026, 11, 21, 11, 5, tzinfo=CT)
check(wb.tick_picks_up(FakeLeague(), 13, NOW[0], send, ready=lambda w, g: True) == "locked", "after lock: nothing")

# daytime drop without cards: wait (no text) up to MERGE_WAIT_S, then announce alone as one text
sent.clear()
lock = dt.datetime(2026, 11, 28, 11, 0, tzinfo=CT)
NOW[0] = dt.datetime(2026, 11, 24, 14, 0, tzinfo=CT)
r = wb.tick_picks_up(FakeLeague(), 13, NOW[0], send, ready=lambda w, g: False)
check(not sent and "prepared" not in r, "2pm drop, no cards yet: nothing posts, waiting to merge")
NOW[0] = dt.datetime(2026, 11, 24, 17, 1, tzinfo=CT)
wb.tick_picks_up(FakeLeague(), 13, NOW[0], send, ready=lambda w, g: False)
check([s[0] for s in sent] == ["announce"] and len(sent[0][2]) == 1 and ob.done(wb.bd_key(2026, 13)),
      "3h later, still no cards: the announcement goes alone as one text and no breakdown follows")
NOW[0] = dt.datetime(2026, 11, 24, 18, 0, tzinfo=CT)
wb.tick_picks_up(FakeLeague(), 13, NOW[0], send, ready=lambda w, g: True)
check(len(sent) == 1, "cards arriving later never cause a second text")
check(wb.tick_picks_up(FakeLeague(), 12, NOW[0], send) in ("done", "locked"), "week 12 stays done")

# --- Thursday refresh ---


class RefLeague(FakeLeague):
    def __init__(self):
        super().__init__(1)


old_gf = {"header": "Away1 -2.5 at Home1", "lean": {"label": "Away1 -2.5"}, "market_home": 2.5,
          "forecast": {"temp": 50, "gust": 10, "precip": 10}, "news": [], "venue_city": "X", "away": "Away1", "home": "Home1"}
wb.save_state(2026, 14, {"games": {"1": old_gf}, "prepared": dt.datetime(2026, 11, 17, 12, tzinfo=CT).timestamp()})
same = dict(old_gf, game_id=1, facts=[])
wb.facts_for = lambda games, week, now=None, notes=None, client=None: [same]
rl = RefLeague()
check(wb.refresh_texts(rl, 14, NOW[0]) == [], "nothing changed since the breakdown: no refresh post")
check(wb.refresh_texts(rl, 99, NOW[0]) is None, "no breakdown on file: fall back to the old preview")
moved = dict(same, market_home=4.0, forecast={"temp": 31, "gust": 28, "precip": 70},
             news=["Away1 QB X is out (ESPN, 2026-11-19)", "old line (ESPN, 2026-11-16)"], lean={"label": "Home1 +2.5"})
wb.facts_for = lambda games, week, now=None, notes=None, client=None: [moved]
t = wb.refresh_texts(rl, 14, NOW[0])
check(t and len(t) == 1, "refresh is one text", t)
body = t[0] if t else ""
check("market moved to Away1 -4 (was Away1 -2.5)" in body, "refresh: market move framed from the home line")
check("QB X is out" in body and "old line" not in body, "refresh: only news dated after the breakdown")
check("forecast now 31F" in body and "lean flipped: now Home1 +2.5" in body, "refresh: forecast swing + flipped lean")

# --- length: the picks-up post is ONE text under the cap, angles only while they fit ---
G4 = FakeLeague().games
LONG = ["intro"] + [f"Away{i} at Home{i}\nShort angle for game {i}, run D is bad.\n" + "Filler line. " * 40
                    + f"\nLean: Away{i}" for i in range(1, 5)]
pu = wb.picks_up_text(12, G4, lock, LONG, 450)
check(len(pu) <= 450 and pu.count("\n") == 5 and "Away1 -2.5 at Home1: Short angle for game 1" in pu
      and "Filler" not in pu and "Lean" not in pu, "picks-up text: one short angle per game, no filler, no lean", pu)
pu = wb.picks_up_text(12, G4, lock, LONG, 200)
check(len(pu) <= 200 and "Week 12 picks are up" in pu and "Away4 at Home4" in pu,
      "a tight cap drops angles before it drops a game line", pu)
check(len(wb.picks_up_text(12, G4, lock, None, 280)) <= 280, "announcement alone fits one 280-character text")
check(wb.angle_of("Hdr\n" + "x" * 200 + "\nLean: A\nshort one") == "short one" and wb.angle_of("Hdr\nLean: A") is None,
      "angle_of: first short non-lean line after the header")
check(wb.angle_of("Hdr\nMarket sits on 3. " + "y" * 120 + "\nLean: A") == "Market sits on 3.",
      "angle_of: no short line -> the first sentence of a long one")
print("\nall passed" if not fails else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)
