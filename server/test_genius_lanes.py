"""Genius lanes (2026-11-17): h2h / qb status / key numbers routing, the computed-lean appender (offline)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import chat  # noqa: E402
import router  # noqa: E402


def test_h2h_routes():
    for q in ["What happened last time Clemson played Duke?", "What happened when Northwestern and Minnesota played last year?",
              "head to head Iowa Illinois", "h2h lsu tennessee"]:
        assert router.classify(q).kind == "h2h", q
    for q in ["how did my pick do last week", "who covered last week", "last time I picked Iowa it burned me"]:
        assert router.classify(q).kind != "h2h", q


def test_keynum_routes():
    assert router.classify("Which lines this week are sitting on or next to a key number? Where does the hook matter?").kind == "keynum"
    assert router.classify("what are the key numbers this week").kind == "keynum"


def test_h2h_text_format():
    orig_find, orig_lm = router.slate_cards.find_game, router.slate_cards.last_meeting
    try:
        router.slate_cards.find_game = lambda q, ctx, idx=None: ({"awayTeam": "Clemson", "homeTeam": "Duke"}, None)
        router.slate_cards.last_meeting = lambda g: "  Last meeting (2025): Duke won 46-45 at Clemson, line Clemson -4: Duke covered by 5"
        out = router.h2h_text({}, "last time Clemson played Duke?")
        assert out == "Last time they played (2025), Duke won 46-45 at Clemson. Clemson -4 was the line, so Duke covered by 5."
        router.slate_cards.last_meeting = lambda g: None
        assert "don't have a 2025 meeting" in router.h2h_text({}, "last time Clemson played Duke?")
    finally:
        router.slate_cards.find_game, router.slate_cards.last_meeting = orig_find, orig_lm


CARD = ("GAME CARD Memphis at Navy\n  COMPUTED ATS LEAN (vs the current market line): Navy +3.5 (lean; factor score "
        "Memphis 1.2, Navy 2.0). Who COVERS, not who wins.\n    + for Navy: Navy is 4-1 ATS at home\n")


def test_lean_fact():
    check, add = chat.lean_fact(CARD, "Memphis -3.5 at Navy. How does Memphis's defense match up with the option?")
    assert not check("Memphis is 62nd vs the run, the option will cook them.")
    assert check("I like Navy +3.5 to cover here.")
    assert add.strip() == "Lean: Navy +3.5, mainly because Navy is 4-1 ATS at home."
    coin = CARD.replace("(lean;", "(coin flip;")
    assert "coin flip" in chat.lean_fact(coin, "who covers Memphis Navy?")[1]
    assert chat.lean_fact(CARD, "what channel is Memphis Navy on") is None


if __name__ == "__main__":
    fails = 0
    for name, f in list(globals().items()):
        if name.startswith("test_"):
            try:
                f()
                print("ok  ", name)
            except AssertionError as ex:
                fails += 1
                print("FAIL", name, ex)
    sys.exit(1 if fails else 0)
