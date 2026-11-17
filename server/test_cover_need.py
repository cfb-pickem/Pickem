"""
Live cover-need asks (Saturday drill, 2026-11-17): "how many points does Illinois need to cover" was
routed to the league-rules lane (it answered "4 games a week against the spread, 1 point per pick")
and "what does Illinois need to cover" to the playoff-race lane. Both are about a game's live cover:
router's score lane (COVERING_NOW) now takes them, and playoff_race.is_need_question leaves them alone.
Real rules / playoff asks keep their lanes.

Run:  ./.venv/bin/python3 -I test_cover_need.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import playoff_race  # noqa: E402
import router  # noqa: E402

fails = []


def check(cond, msg):
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond:
        fails.append(msg)


for q in ("how many points does Illinois need to cover", "what does Illinois need to cover",
          "how much does Texas Tech need to cover?", "what do we need to cover in the LSU game",
          "how many does Iowa need to cover"):
    r = router.classify(q)
    check(r.label() == "fact:score", f"{q!r} -> fact:score (got {r.label()})")
    check(not playoff_race.is_need_question(q), f"{q!r} is not a playoff-race ask")

for q, want in (("what does Ackbar need to make the playoffs", True), ("what does TG need", True),
                ("am I still alive", True)):
    check(playoff_race.is_need_question(q) is want, f"playoff ask kept: {q!r}")
check(router.classify("how do points work").label() != "fact:score", "a rules ask about points isn't a cover ask")
check(router.classify("is Iowa covering").label() == "fact:score", "is X covering still the score lane")


# live game-state asks the score lane answers in code (the drill's burst sent them to the full brain:
# ~22 s each in the one-slot queue)
for q in ("Texas Tech covering?", "who has the ball in the Baylor game", "how much time left in the Iowa game",
          "did Minnesota score", "who's covering Iowa Illinois", "whos got the ball in LSU Tennessee"):
    check(router.classify(q).label() == "fact:score", f"{q!r} -> fact:score (got {router.classify(q).label()})")
check(router.classify("who covers Iowa Illinois").label() != "fact:score", "'who covers X' stays a pick ask")

# 'covering' asks before kickoff are pick asks: score_text steps aside (full brain / pick lanes)
def ctx(status):
    g = {"id": 7, "away": "Iowa", "home": "Illinois", "line": 8.5, "start": "2026-11-21T13:30:00"}
    lv = {"GameId": 7, "Away": "Iowa", "Home": "Illinois", "Status": status, "AwayPts": 13, "HomePts": 29,
          "Period": 2, "Clock": "1:59", "line": 8.5}
    return {"games_this_week": [g], "live_games": [lv], "live_ts": None}
from unittest.mock import patch  # noqa: E402
import chat  # noqa: E402
with patch.object(chat, "feed_age", lambda *_a: 5.0):
    check(router.score_text(ctx("scheduled"), "who's covering Iowa Illinois") is None,
          "pre-kickoff 'who's covering X': no score answer (it's a pick ask)")
    live = router.score_text(ctx("in_progress"), "who's covering Iowa Illinois")
    check(bool(live) and "Covering right now" in live, f"live 'who's covering X': the score lane answers ({live!r})")
    check(router.score_text(ctx("scheduled"), "how much time left in the Iowa game") is not None,
          "a non-covering ask still gets the game state pre-kickoff")

print(f"\n{'all green' if not fails else 'FAILED: ' + str(len(fails))}")
sys.exit(1 if fails else 0)
