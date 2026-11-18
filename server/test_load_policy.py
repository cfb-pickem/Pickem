"""Load policy (Charlie's decision 4, 2026-11-17): code lanes skip the model queue while a full-lane text is in
flight; 3+ other full-lane texts in flight = the fast lane (no reroll). Model-free."""
import asyncio
import os
import sys
from pathlib import Path

os.environ.setdefault("PICKY_NO_ANTHROPIC", "1")
sys.path.insert(0, str(Path(__file__).resolve().parent))
import picky_brain as pb  # noqa: E402


def test_code_lane_skips_the_model_while_a_full_text_runs():
    pb._FULL_INFLIGHT[0] = 1
    try:
        asyncio.run(pb._json_call("P", "T", "U", {}, 0.1, 10, 1.0, prime_wait=pb.PRIME_WAIT))
        raised = False
    except pb.PrimeSlow:
        raised = True
    finally:
        pb._FULL_INFLIGHT[0] = 0
    assert raised


def test_fast_lane_at_three_others():
    for inflight, want in ((1, False), (3, False), (4, True)):  # counts include the text itself
        pb._FULL_INFLIGHT[0] = inflight
        assert pb.fast_lane() is want, inflight
    pb._FULL_INFLIGHT[0] = 0
    assert pb.BUSY_SKIP_BRIEF <= pb.BUSY_FAST  # the brief goes first, then the reroll


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
