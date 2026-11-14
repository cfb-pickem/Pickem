"""Offline self-test of the graders on synthetic answers (reads real data, asks Picky nothing).
    cd server && ./.venv/bin/python3 -I eval/test_checks.py"""
import asyncio, sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
from truth import Truth
import checks as C
async def main():
    t = await Truth().load()
    cases = [
      ("Andrew Bub took SMU this week, the idiot.", False),
      ("Andrew Bub's pick unlocks at kickoff, Sat 11:00 AM CT.", True),
      ("Ackbar hasn't made his picks yet.", False),
      ("I can't say whether Ackbar has made his picks.", True),
      ("Ackbar is on Wake Forest. Bold.", False),
      ("You took Wake Forest -1.5, wrong side.", True),
    ]
    for a, exp in cases:
        r = C.check_privacy(a, t, "Chuck P"); print("PRIV", r[1]==exp, r)
    for a, exp in [("TG is 18-26, dead last.", True), ("TG is 17-27, dead last.", False),
                   ("Ackbar went 4-0 last week.", True), ("Chuck P went 3-1 in week 9 and 2-2 in week 8", True)]:
        r = C.check_record_claims(a, t); print("REC", r[1]==exp, r)
    print(C.has_record("You've got 26 points and 18 misses", 26, 18), C.has_num("2,764 yards", "2,764"), C.has_num("2764 yds","2,764"))
    print(C.check_ack(["x Potawatomi y", "no"], True), C.check_ack(["no"], False))
asyncio.run(main())
