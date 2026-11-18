"""Genius-exam deterministic checks (eval/genius/checks.py), model-free.

2026-11-18 hand check (ontology plan Phase 1): si-baylor-interim failed ats_records_real on "Since then: ...
2-1 ATS" although Baylor really is 2-1 ATS since the Oct. 25 coaching change (UCF cover, Iowa State cover,
BYU miss). A record over a trailing window ("since", "last 3") is a true computed record; a season record
that isn't real still fails.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "eval" / "genius"))
import checks  # noqa: E402

BAYLOR = [{"date": d, "ats": a} for d, a in [("2026-09-05", "C"), ("2026-09-13", "C"), ("2026-09-19", "L"),
                                              ("2026-09-26", "C"), ("2026-10-04", "C"), ("2026-10-17", "L"),
                                              ("2026-10-24", "C"), ("2026-10-30", "C"), ("2026-11-07", "C"),
                                              ("2026-11-15", "L")]]
FACTS = {"games": {"9": {"teams": {"home": {"cfbd_ats": "7-3-0", "ats_splits_computed": {"all": "7-3"},
                                            "games": BAYLOR}}}}}
ITEM = {"games": [9]}


def test_window_record_since_a_date_is_real():
    ans = "Since then: 2-1 straight up, 2-1 ATS (beat UCF; beat Iowa State; lost to BYU)."
    assert checks.run_checks(ITEM, [ans], FACTS, {})[3] == ("ats_records_real", True, "")


def test_last_n_window_is_real():
    ans = "They're 2-1 ATS in their last 3."
    assert checks.check_ats_records(ans, checks.allowed_ats(ITEM, FACTS), checks.window_ats(ITEM, FACTS))[1]


def test_window_record_without_a_window_word_still_fails():
    ans = "Baylor is 2-1 ATS this season."  # not the season record (7-3): a wrong claim
    assert not checks.run_checks(ITEM, [ans], FACTS, {})[3][1]


def test_window_record_that_never_happened_fails():
    ans = "Since the coaching change they're 0-3 ATS."
    assert not checks.run_checks(ITEM, [ans], FACTS, {})[3][1]


def test_season_record_still_passes():
    assert checks.run_checks(ITEM, ["Baylor is 7-3 ATS."], FACTS, {})[3][1]


if __name__ == "__main__":
    fails = 0
    for name, f in list(globals().items()):
        if name.startswith("test_") and callable(f):
            try:
                f()
                print("ok  ", name)
            except Exception as ex:  # noqa: BLE001
                fails += 1
                print("FAIL", name, repr(ex)[:300])
    print(f"\n{fails} failed" if fails else "\nall ok")
    sys.exit(1 if fails else 0)
