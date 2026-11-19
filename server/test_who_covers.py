"""'<game>, who covers?' is a pick ask (Charlie OK 2026-11-19 15:40 CT): the code pick lane, not the full brain.

    ./.venv/bin/python3 -I test_who_covers.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import router  # noqa: E402


def lane(q):
    return router.classify(q).label()


def test_trailing_who_covers_names_a_game():
    for q in ("LSU-Tennessee, who covers?", "Northwestern at Minnesota, who covers?", "Iowa Illinois who covers",
              "LSU at Tennessee... who covers??"):
        assert lane(q) == "fact:pick", (q, lane(q))


def test_unchanged_neighbours():
    assert lane("who covers lsu tenn") == "fact:pick"  # already a pick ask
    assert lane("who covers?") == "fact:pick_ctx"  # bare: the game in the thread
    assert lane("ok but who covers") == "fact:pick_ctx"
    assert lane("who covered") == "fact:cover"  # past tense: a result
    assert lane("did Boise cover") == "fact:cover"
    assert lane("lol who covers?") != "fact:pick"  # names no team: not a pick on the whole slate


if __name__ == "__main__":
    fails = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("ok  ", name)
            except AssertionError as e:
                fails += 1
                print("FAIL", name, e)
    print("FAILS:", fails)
    sys.exit(1 if fails else 0)
