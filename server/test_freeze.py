"""Game-day windows (review R4/R9): freeze Fri 18:00 - Sun 04:00 CT, no exams Sat 10:00-24:00 CT."""
import datetime as dt
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import freeze  # noqa: E402

CT = freeze.CT


def at(d, h, m=0):
    return dt.datetime(2026, 11, d, h, m, tzinfo=CT)  # 11/20 Fri, 11/21 Sat, 11/22 Sun


def test_freeze_window():
    assert not freeze.frozen(at(20, 17, 59)) and freeze.frozen(at(20, 18, 0))
    assert freeze.frozen(at(21, 0, 1)) and freeze.frozen(at(21, 23, 0)) and freeze.frozen(at(22, 3, 59))
    assert not freeze.frozen(at(22, 4, 0)) and not freeze.frozen(at(18, 12))


def test_no_exam_window():
    assert not freeze.no_exams(at(21, 9, 59)) and freeze.no_exams(at(21, 10, 0)) and freeze.no_exams(at(21, 23, 59))
    assert not freeze.no_exams(at(22, 0, 30)) and not freeze.no_exams(at(20, 20, 0))
    assert freeze.saturday(at(21, 3)) and not freeze.saturday(at(22, 3))


def test_every_exam_entry_point_is_guarded():
    for rel in ("eval/genius/side.sh", "eval/genius/exam.sh", "eval/genius/nightly.sh", "eval/fact_exam.sh"):
        assert "freeze.py check-exam" in (HERE / rel).read_text(), rel
    for rel in ("eval/run.py", "real_replay.py", "saturday_drill.py", "eval/live_exam.py", "eval/speed_replay.py",
                "../tools/scorecard.py"):
        assert "freeze.exam_guard(" in (HERE / rel).read_text(), rel


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("ok  ", name)
            except AssertionError as e:
                fails += 1
                print("FAIL", name, e)
    print("ALL PASS" if not fails else f"{fails} FAILED")
    sys.exit(1 if fails else 0)
