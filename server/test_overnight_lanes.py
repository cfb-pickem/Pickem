"""
Overnight lanes (2026-11-18 night, by question TYPE from the genius misses + real traffic):

- lookahead_game: 'is Iowa looking ahead to Nebraska?' / 'Ohio State ... is this a lookahead spot?' about ONE named
  game, answered in code from the store's lookahead verdict (the full brain told a member there was no spot when
  the card said there was). Slate-wide 'any lookahead spots?' keeps lookahead_slate; a compound ATS ask keeps
  ats_profile.

Run:  ./.venv/bin/python3 -I test_overnight_lanes.py
"""

from __future__ import annotations

import sys

sys.path.insert(0, ".")
import factstore as FS  # noqa: E402
import router  # noqa: E402
import test_big_picture as TB  # noqa: E402  (frozen fixture store)


def _lane(q):
    r = router.classify(q)
    return r.label() if hasattr(r, "label") else (r.lane, r.kind)


def _ans(fn, q, ctx=None):
    TB.use(TB.store_at("main"))
    try:
        return fn(ctx or {}, q)
    finally:
        FS.use(None, None)


# --- lookahead_game ---------------------------------------------------------------------------------

def test_one_game_lookahead_asks_route_to_the_code_lane():
    for q in ("Is Iowa looking ahead to Nebraska?", "Ohio State at Nebraska with Michigan next. Is this a lookahead spot?",
              "is texas tech in a letdown spot at baylor", "Is Ohio State looking past Nebraska to Michigan?"):
        assert router.fact_intent(q) and router.fact_intent(q)[0] == "lookahead_game", (q, router.fact_intent(q))


def test_slate_and_compound_asks_keep_their_lanes():
    assert router.fact_intent("any lookahead spots this week?")[0] == "lookahead_slate"
    fi = router.fact_intent("How's Iowa been ATS, how's Illinois as a dog, and is Iowa looking ahead to Nebraska?")
    assert fi and fi[0] == "ats_profile", fi


def test_lookahead_yes_names_the_spot_and_the_game_behind_it():
    out = _ans(router.lookahead_game_text, "Is Iowa looking ahead to Nebraska?")
    assert out, out
    low = out.lower()
    assert "lookahead spot" in low and "nebraska" in low and "heroes game" in low, out
    assert "no lookahead" not in low, out
    assert "illinois" in low and "ucla" in low, out  # the other side's spot said too
    assert "coin flip" in low, out  # spots alone are about a coin flip (B12)
    assert len(out) <= 600, (len(out), out)


def test_letdown_spot_is_said_as_a_letdown():
    out = _ans(router.lookahead_game_text, "is texas tech in a letdown spot at baylor")
    assert out and "letdown spot" in out.lower() and "oklahoma state" in out.lower(), out


def test_lookahead_lane_steps_aside_without_a_game():
    assert _ans(router.lookahead_game_text, "Is Montana Tech looking ahead?") is None



# --- rules: a whole-number line in a push ask --------------------------------------------------------

def test_push_ask_with_a_whole_number_says_what_the_league_line_would_be():
    """'If Ohio State -3 is the line and they win by exactly 3, do I get a push?': the league moves -3 half a
    point, so the answer says both ways it can go (a 3-point win covers -2.5, misses -3.5)."""
    import league_rules
    out = league_rules.answer("If Ohio State -3 is the line and they win by exactly 3, do I get a push in our league?")
    assert out and "no pushes" in out.lower(), out
    assert "-2.5" in out and "-3.5" in out, out
    assert "covers" in out.lower(), out
    plain = league_rules.answer("What happens on a push?")
    assert "-2.5" not in plain, plain


if __name__ == "__main__":
    fails = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok   {name}")
            except Exception as e:  # noqa: BLE001
                fails += 1
                print(f"FAIL {name}: {e!r}"[:900])
    print("ALL PASS" if not fails else f"{fails} FAILED")
    sys.exit(1 if fails else 0)
