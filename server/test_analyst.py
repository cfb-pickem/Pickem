"""
Deterministic tests for the analyst takes (analyst.py) and the league
material sheet (league_material.py), plus their delivery blocks in chat.py.
No network, no claude, no GPU (Claude's output is faked).

Run:  ./.venv/bin/python3 -I test_analyst.py
"""

from __future__ import annotations

import datetime as dt
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, ".")
import analyst  # noqa: E402
import chat  # noqa: E402
import dossier  # noqa: E402
import league_material as lm  # noqa: E402

FAILED = []


def check(name, ok, got=None):
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else f"\n     got: {got!r}"))
    if not ok:
        FAILED.append(name)


# -- analyst take validator ------------------------------------------------------------
CG = {"id": 9, "away": "Wake Forest", "home": "SMU", "line": 1.5, "fpi_margin": -3.2, "start": "2026-11-21T11:00:00"}
ITEMS = {"S1": "pool line: Wake Forest -1.5 / SMU +1.5",
         "S2": "betting market (ESPN): SMU -2.5, over/under 60.5",
         "S3": "unit edge: Wake Forest offense success rate #3 vs SMU defense success rate #52: edge Wake Forest",
         "S4": "line movement: opened Wake Forest -3.5, now SMU -2.5, the market moved 6 points toward SMU",
         "C1": "[injury report, status probable, 2026-11-19] QB Gio Lopez is expected to play, coach said."}
M = {"lean": "SMU", "strength": "lean"}


def raw(**kw):
    base = {"pick": "Wake Forest", "confidence": "medium",
            "reasons": [{"text": "Wake's offense is 3rd in success rate and SMU's defense is 52nd.", "refs": ["S3"]},
                        {"text": "Gio Lopez is probable, so the offense is whole.", "refs": ["C1"]},
                        {"text": "The market moved 6 points toward SMU, which feels like an overreaction.",
                         "refs": ["S4"]}],
            "counter": {"text": "The market has SMU at 2.5 now.", "refs": ["S2"]},
            "market_missing": {"text": "Nobody's pricing Lopez coming back.", "refs": ["C1"]},
            "quotable": "Lopez is back and SMU can't stop the chains. Give me Wake."}
    base.update(kw)
    return base


t, probs = analyst.validate_take(raw(), ITEMS, CG, M)
check("valid take passes", t and len(t["reasons"]) == 3 and not probs, probs)
check("disagreement with the lean is explicit", t and t["lean"] == "SMU" and t["agrees_with_lean"] is False, t)
t, _ = analyst.validate_take(raw(pick="Clemson"), ITEMS, CG, M)
check("pick must be one of the two teams", t is None)
bad = raw(reasons=[{"text": "Wake is 9-1 and scores 44 a game.", "refs": ["S3"]},
                   {"text": "Gio Lopez is probable.", "refs": ["C1"]},
                   {"text": "Wake's offense is 3rd in success rate.", "refs": ["S3"]}])
t, probs = analyst.validate_take(bad, ITEMS, CG, M)
check("a number not in the cited items drops that reason", t and len(t["reasons"]) == 2
      and any("numbers" in p for p in probs), probs)
bad = raw(reasons=[{"text": "Demond Williams is out for SMU.", "refs": ["C1"]},
                   {"text": "Gio Lopez is probable.", "refs": ["C1"]},
                   {"text": "Wake's offense is 3rd in success rate.", "refs": ["S3"]}])
t, probs = analyst.validate_take(bad, ITEMS, CG, M)
check("a name not in the brief drops that reason", t and len(t["reasons"]) == 2
      and any("names" in p for p in probs), probs)
t, probs = analyst.validate_take(raw(reasons=[{"text": "Reason one: Wake's offense is 3rd.", "refs": ["S3"]},
                                              {"text": "Lopez plays.", "refs": []}]), ITEMS, CG, M)
check("report-speak / no refs: too few reasons = no take", t is None, probs)
t, probs = analyst.validate_take(raw(quotable="Confidence: high. Take Wake by 14."), ITEMS, CG, M)
check("bad quotable is dropped, take survives", t and t["quotable"] is None, probs)

# -- delivery: take_note ------------------------------------------------------------------
with tempfile.TemporaryDirectory() as tmp:
    chat.DOSSIER_DIR = Path(tmp)
    take, _ = analyst.validate_take(raw(), ITEMS, CG, M)
    d = {"generated_at": dt.datetime.now(dossier.CT).isoformat(), "facts": [], "analyst_take": take}
    (Path(tmp) / "12").mkdir()
    (Path(tmp) / "12" / "9.json").write_text(json.dumps(d))
    g = {"id": 9, "away": "Wake Forest", "home": "SMU", "line": 1.5, "start": "2099-11-21T11:00:00",
         "status": None, "winner": None, "tiebreaker": False}
    ctx = {"current_week": 12, "games_this_week": [g], "season": 2026}
    chat.week_locked = lambda c: False
    note = chat.take_note(ctx, "what do you think about Wake Forest SMU?")
    check("disagreeing take: the lean stays the pick", "YOUR PICK STAYS SMU +1.5" in note, note)
    check("disagreeing take: no quotable for the other side", "use as is" not in note, note)
    check("talk-like-a-person rule rides along", "buddy in a group text" in note, note)
    check("line question gets no take", chat.take_note(ctx, "what's the line on Wake Forest SMU?") == "")
    check("injury question gets no take", chat.take_note(ctx, "is anyone hurt for Wake Forest SMU?") == "")
    take2 = dict(take, pick="SMU", agrees_with_lean=True)
    (Path(tmp) / "12" / "9.json").write_text(json.dumps(dict(d, analyst_take=take2)))
    note = chat.take_note(ctx, "break down Wake Forest at SMU")
    check("agreeing take: the play + quotable", "The play: SMU +1.5" in note and "use as is" in note, note)
    chat.AMMO_ON = False
    check("PICKY_AMMO=0 turns takes off", chat.take_note(ctx, "break down Wake Forest at SMU") == "")
    chat.AMMO_ON = True

# -- league material: facts, validator, freshness ------------------------------------------
data = {"teams": [{"team_id": 1, "team_name": "Ackbar", "active": True, "is_exhibition": False},
                  {"team_id": 2, "team_name": "TG", "active": True, "is_exhibition": False},
                  {"team_id": 3, "team_name": "Chuck P", "active": True, "is_exhibition": False},
                  {"team_id": 26, "team_name": "Picky", "active": True, "is_exhibition": True}],
        "games": [], "picks": []}
gid = 100
for wk in (1, 2):
    for k in range(4):
        gid += 1
        data["games"].append({"GameId": gid, "cfb_season": 2026, "week": wk, "Away": f"A{gid}", "Home": f"H{gid}",
                              "AwayPts": 10, "HomePts": 20, "line": -3.5, "winner": f"H{gid}", "Status": "final",
                              "tiebreaker": False, "Start (CT)": f"2026-09-0{wk}T1{k}:00:00"})
        data["picks"] += [{"team_id": 1, "game_id": gid, "pick": f"H{gid}"},  # chalk, always hits
                          {"team_id": 2, "game_id": gid, "pick": f"A{gid}"},  # dog, always misses
                          {"team_id": 3, "game_id": gid, "pick": f"H{gid}" if k % 2 else f"A{gid}"},
                          {"team_id": 26, "game_id": gid, "pick": f"A{gid}"}]
fs = lm.build_facts(data)
F = fs["facts"]
check("facts: week = last fully final week", fs["week"] == 2, fs["week"])
check("facts: season records", fs["records"] == {"Ackbar": [8, 0], "Chuck P": [4, 4], "TG": [0, 8]}, fs["records"])
check("facts: chalk rate", any("Ackbar takes the favorite 100% of the time; favorites went 8-0" in x for x in F.values()),
      list(F.values()))
check("facts: Picky's own record", fs["picky"] == [0, 8])
fid = next(k for k, v in F.items() if v.startswith("TG: 0-8"))
good = {"members": [{"team": "TG", "lines": [{"text": "TG is 0-8. That's not a slump, that's a lifestyle.",
                                              "kind": "roast", "facts": [fid]},
                                             {"text": "TG is 2-6 lately.", "kind": "roast", "facts": [fid]},
                                             {"text": "TG's mom picks better.", "kind": "roast", "facts": [fid]},
                                             {"text": "TG trails Ackbar badly.", "kind": "roast", "facts": [fid]}]}],
        "storylines": [], "picky_narrative": {"text": "I'm 0-8. Somebody unplug me.", "facts": [
            next(k for k, v in F.items() if v.startswith("Picky (exhibition"))]}}
sheet, dropped = lm.validate(good, fs)
check("material: true line kept", [x["text"] for x in sheet["members"]["TG"]["lines"]]
      == ["TG is 0-8. That's not a slump, that's a lifestyle."], sheet)
check("material: number not in cited facts dropped", any("2-6" in d or "numbers" in d for d in dropped), dropped)
check("material: family joke dropped", any("banned" in d for d in dropped), dropped)
check("material: member not in cited facts dropped", any("Ackbar" in d for d in dropped), dropped)
check("material: Picky narrative kept", sheet["picky_narrative"] and "0-8" in sheet["picky_narrative"]["text"])
now = dt.datetime(2026, 11, 15, 12, tzinfo=lm.CT)
check("due: new sheet", lm.due(fs, None, now) == "new")
prev = {"season": 2026, "week": 2, "generated_at": "2026-11-15T09:00:00-06:00",
        "members": {"TG": {"record": [0, 8]}}}
check("due: nothing when current", lm.due(fs, prev, now) is None)
check("due: a newer final week", lm.due(dict(fs, week=3), prev, now) == "final")
check("due: Wednesday refresh", lm.due(fs, prev, dt.datetime(2026, 11, 18, 10, 5, tzinfo=lm.CT)) == "midweek")
with tempfile.TemporaryDirectory() as tmp:
    out = Path(tmp) / "m.json"
    r = lm.run(out=out, data=data, research=lambda p: good, now=now)
    check("run writes a private (0600) sheet", r["built"] and oct(out.stat().st_mode)[-3:] == "600", r)
    j = json.loads(out.read_text())
    try:
        lm.run(out=out, data=data, research=lambda p: {"members": [], "storylines": [], "picky_narrative": {}},
               now=now, force=True)
        check("a bad run raises", False)
    except RuntimeError:
        check("a bad run never replaces a good sheet", json.loads(out.read_text()) == j)
    r3 = lm.run(out=out, data=data, research=lambda p: good, now=dt.datetime(2026, 11, 18, 11, tzinfo=lm.CT))
    check("a failed run backs off (no retry next tick)", r3.get("backoff") is True, r3)

    # -- delivery: material_note -------------------------------------------------------
    chat.LEAGUE_MATERIAL_FILE = out
    chat._material_cache = None
    ctx = {"season": 2026, "standings_ytd": [{"team": "TG", "w": 0, "l": 8}, {"team": "Ackbar", "w": 8, "l": 0},
                                             {"team": "Chuck P", "w": 4, "l": 4}],
           "you": {"team_name": "Ackbar"}}
    chat.load_lore = lambda: {}
    note = chat.material_note(ctx, "Roast TG")
    check("material note for a named member", "LEAGUE MATERIAL on TG" in note and "0-8" in note, note)
    ctx2 = dict(ctx, standings_ytd=[{"team": "TG", "w": 1, "l": 8}] + ctx["standings_ytd"][1:])
    check("stale record (a game went final since) = no lines", chat.material_note(ctx2, "Roast TG") == "")
    check("unrelated question gets nothing", chat.material_note(ctx, "what's the pool line this week?") == "")
    check("Picky's own story on a Picky question", "YOUR OWN SEASON STORY" in chat.material_note(
        ctx, "Picky how's your season going?"))

# -- Blinkstradamus held the milk; it never spilled (Charlie, 2026-11-15) -------------------
check("milk spill sentence dropped", not chat.sentence_ok("He's the guy who spilled his carton on the road.", set()))
check("carton knocked off dropped", not chat.sentence_ok("That loss knocked the whole carton off the counter.", set()))
check("'dropped to 3rd' near milk is fine", chat.sentence_ok("He dropped to 3rd but the milk is fresh.", set()))
check("rider says he held it, no spill", "holding a carton of milk" in chat.BLINKS_BIT and "never spilled" in chat.BLINKS_BIT
      and "league's milk guy" not in chat.BLINKS_BIT)

if FAILED:
    print(f"\n{len(FAILED)} FAILED")
    sys.exit(1)
print("\nALL PASS")
