"""Model-free tests for pick_strategy (protect / bubble / chase / out) and its router exit."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pick_strategy as ps  # noqa: E402

FAILS = []


def check(c, m):
    print(("ok   " if c else "FAIL ") + m)
    if not c:
        FAILS.append(m)


st = [{"rank": 1, "team": "A", "w": 30, "l": 18}, {"rank": 2, "team": "Ack", "w": 28, "l": 20},
      {"rank": 3, "team": "B", "w": 27, "l": 21}, {"rank": 3, "team": "C", "w": 27, "l": 21},
      {"rank": 5, "team": "L", "w": 26, "l": 22}, {"rank": 14, "team": "TG", "w": 19, "l": 29}]


def ctx(me, wk=11):
    return {"standings_ytd": st, "current_week": wk, "games_this_week": [], "you": {"team_name": me}}


n = ps.note(ctx("Ack"), "Should I start going against the field to catch him?")
check("PROTECT" in n and "2 behind 1st" in n and "2 ahead of 5th" in n and "RESET" in n, "2nd with a cushion: protect")
check("BUBBLE" in ps.note(ctx("L"), "what's my strategy?"), "1 out of the top 4: bubble")
check("BUBBLE" in ps.note(ctx("B"), "what's my strategy?"), "tied for 3rd, 1 over 5th: bubble")
check("CHASE" in ps.note(ctx("TG"), "any pick strategy?") and "8 behind the cut line" in ps.note(ctx("TG"), "strategy?"),
      "8 back with 16 left: chase")
check("OUT" in ps.note(ctx("TG", wk=14), "strategy?"), "8 back with 4 left: out")
check(ps.note(ctx("Ack"), "who's winning?") == "", "no rider without a strategy ask")
check(ps.note(ctx("Ack", wk=16), "strategy?") == "", "no rider in the playoffs")
try:
    import router
    check(router.classify("I'm one out of the playoff spots. What's my strategy the next 4 weeks?").lane == "full",
          "router: strategy texts take the full path (not the rules/playoff fact lanes)")
except Exception as e:  # noqa: BLE001
    check(False, f"router import: {e!r}")
print(f"\n{len(FAILS)} failed" if FAILS else "\nall ok")
sys.exit(1 if FAILS else 0)
