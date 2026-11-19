"""
Pushback coherence (genius pushback 6/10, 2026-11-19 round):

- A projection reason says where it sits against the number: the favorite's 'SP+ has X by about 6, more than the
  3.5'; the dog's 'SP+ only has X by about 8, short of the 8.5' (wy-iowa-ill read 'SP+ has Iowa by 8' as a reason
  for Illinois +8.5; pb-uga 'EPA has Georgia by about 8' as the case for South Carolina +15.5).
- 'Best case for the other side', when Picky stays put, says what keeps him there (pb-uga: 'that contradicts
  itself').
- A full-brain pick answer in a game with a shaky starting QB names him and goes conditional on him
  (pool-lsu-tenn-pick, pb-uga missed MacIntyre / Sellers), and an ATS call never quotes a win probability
  (wy-nw-minn: '78.1% chance to win outright').

Synthetic cards from test_case_ledger (no network, no model).

Run:  ./.venv/bin/python3 -I test_pushback_coherence.py
"""

from __future__ import annotations
import os
os.environ["PICKY_LANES_OFF"] = ""

import sys

sys.path.insert(0, ".")
import case_ledger as CL  # noqa: E402
import factstore as FS  # noqa: E402
import router  # noqa: E402
from test_case_ledger import BAY, CTX, TT, Patch, card  # noqa: E402

say = router.say_reason


# --- projections against the number ------------------------------------------------------------------------------

def test_favorite_projection_says_it_clears_the_number():
    assert say("SP+ (ratings + 2.5 home field): projects Texas Tech by 6.2, so Texas Tech -3.5 covers by 2.7") == \
        "SP+ has Texas Tech by about 6, more than the 3.5"


def test_dog_projection_says_the_favorite_falls_short():
    out = say("SP+ (ratings + 2.5 home field): projects Iowa by 7.9, so Illinois +8.5 covers by 0.6")
    assert out == "SP+ only has Iowa by about 8, short of the 8.5", out
    out = say("EPA (season PPA per play): projects Georgia by 8.1, so South Carolina +14.5 covers by 6.4")
    assert out == "the EPA numbers only have Georgia by about 8, well short of the 14.5", out


def test_dog_projected_to_win_outright():
    out = say("EPA (season PPA per play): projects Baylor by 0.9, so Baylor +3.5 covers by 4.4")
    assert out == "the EPA numbers have Baylor winning outright, and they get 3.5", out


def test_whole_number_or_shapeless_projection_keeps_the_short_form():
    assert say("ESPN FPI: projects Iowa by 5.2, so Iowa -3 covers by 2.2") == "FPI has Iowa by about 5"
    assert say("SRS: projects Memphis by 5.3") == "SRS has Memphis by about 5"


# --- other side holds with a reason ------------------------------------------------------------------------------

def test_other_side_says_what_keeps_picky_on_his_side():
    with Patch(card()):
        t1 = router.fact_answer("pick", CTX, "who you got in Texas Tech at Baylor?")
        out = router.fact_answer("other_side", CTX, "best case for Baylor? ||| " + t1)
    assert out and out.startswith("Best case for Baylor +3.5"), out
    assert "I'm still on Texas Tech -3.5" in out and " because " in out, out
    led = CL.from_card(card())
    said = [r["text"].rstrip(". ").split(";")[0] for r in CL.reasons(led, TT)]
    tail = out.split("I'm still on", 1)[1]
    assert any(s in tail for s in said), (tail, said)
    assert FS.unsupported(out, out.facts_used) == [], FS.unsupported(out, out.facts_used)
    assert len(out) <= 700, len(out)


def test_other_side_for_our_own_side_has_no_hold_tail():
    with Patch(card()):
        out = router.fact_answer("other_side", CTX, "make the case for Texas Tech ||| I'd take Texas Tech -3.5 (lean): x.")
    assert out and "That's my side already" in out and "still on" not in out, out


# --- full-brain pick guard ---------------------------------------------------------------------------------------

class Shaky:
    """Baylor's starter is questionable (fresh report)."""

    def __enter__(self):
        self.saved = (FS.has_personnel, FS.qb_room, FS.qb_starter, FS.player_status)
        FS.has_personnel = lambda: True
        FS.qb_room = lambda team: [{"name": "Sawyer Robertson", "starts": 10}] if team == BAY else \
            [{"name": "Behren Morton", "starts": 9}]
        FS.qb_starter = lambda team: None
        FS.player_status = lambda team=None, player=None: (
            [{"player": "Sawyer Robertson", "team": BAY, "status": "questionable", "age_days": 2, "source": "ESPN",
              "detail": "", "quote": "", "date": "2099-11-17"}] if player == "Sawyer Robertson" else [])
        return self

    def __exit__(self, *a):
        FS.has_personnel, FS.qb_room, FS.qb_starter, FS.player_status = self.saved


def test_full_pick_names_the_shaky_qb_and_goes_conditional():
    with Patch(card()), Shaky():
        out = CL.guard_full_pick(CTX, "Texas Tech-Baylor, who covers?",
                                 "I'd take Texas Tech -3.5. Their defense is legit and Baylor can't run.")
    assert out.startswith("I'd take Texas Tech -3.5. Their defense is legit"), out
    assert "Sawyer Robertson" in out and "questionable" in out and "if he sits" in out, out


def test_full_pick_leaves_an_answer_that_already_names_him():
    t = "I'd take Texas Tech -3.5, and Robertson being questionable only helps."
    with Patch(card()), Shaky():
        assert CL.guard_full_pick(CTX, "Texas Tech-Baylor, who covers?", t) == t


def test_full_pick_without_a_side_still_names_the_swing():
    with Patch(card()), Shaky():
        out = CL.guard_full_pick(CTX, "Is 3.5 too many for Texas Tech?",
                                 "Texas Tech is 8-2 ATS and the market moved their way. Baylor's QB is banged up.")
    assert "Sawyer Robertson" in out and "questionable" in out, out


def test_full_pick_drops_a_win_probability():
    with Patch(card()):
        out = CL.guard_full_pick(CTX, "Texas Tech at Baylor, who covers?",
                                 "Texas Tech is the favorite at -3.5 with a 78.1% chance to win outright. I'd take Texas Tech -3.5.")
    assert "%" not in out and "chance to win" not in out, out
    assert out.startswith("Texas Tech is the favorite at -3.5. I'd take Texas Tech -3.5."), out


def test_guard_leaves_non_pick_asks_and_unknown_games_alone():
    t = "Texas Tech has a 78% chance to win, and Robertson is fine."
    with Patch(card()), Shaky():
        assert CL.guard_full_pick(CTX, "how's the weather in Waco?", t) == t
    with Patch(None, brief=None):
        assert CL.guard_full_pick(CTX, "who covers?", t) == t


def test_a_number_is_tied_to_the_line_once_per_answer():
    t = ("the EPA numbers only have Iowa by 4, well short of the 8.5; also, FPI only has Iowa by about 5, well short "
         "of the 8.5")
    assert CL.once_per_number(t) == "the EPA numbers only have Iowa by 4, well short of the 8.5; also, FPI only has Iowa by about 5"
    assert CL.once_per_number("SP+ has X by about 6, more than the 3.5") == "SP+ has X by about 6, more than the 3.5"


def test_hold_reason_is_a_football_reason_not_the_market():
    import copy
    c = copy.deepcopy(card())
    c["factors"].insert(0, {"n": 2, "name": "x", "side": TT, "w": 5.0,
                            "why": "line move: opened Texas Tech -1.5, now Texas Tech -3.5, the market moved 2 points"})
    with Patch(c):
        out = router.fact_answer("other_side", CTX, "best case for Baylor? ||| I'd take Texas Tech -3.5 (lean): x.")
    tail = out.split("I'm still on", 1)[1]
    assert " because " in tail and "line's gone" not in tail, tail


def test_keynum_says_our_lines_never_push():
    import league_rules
    if (league_rules.FACTS.get("half_point") or {}).get("keeps_within") != 0.5:
        return
    out = router.keynum_text({"games_this_week": []}, "which lines are sitting on a key number?")
    if out is None:  # no market rows in this cache
        return
    assert "every line is a half point" in out and len(out) <= 700, out
    assert FS.unsupported(out, out.facts_used) == [], FS.unsupported(out, out.facts_used)


if __name__ == "__main__":
    fails = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok   {name}")
            except Exception as e:  # noqa: BLE001
                fails += 1
                print(f"FAIL {name}: {e!r}"[:1200])
    print("ALL PASS" if not fails else f"{fails} FAILED")
    sys.exit(1 if fails else 0)
