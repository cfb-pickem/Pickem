"""
Deterministic tests for the insider notes rider (Charlie, 2026-11-16: "have all
the knowledge of Ross Dellenger") and the message-board voice / [HOT] takes
(Charlie: "the most outrageous ones"). Uses small fixture files, never the
private bank. No model calls.

Run:  ./.venv/bin/python3 -I test_insider.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="picky-insider-test-"))
(TMP / "insider.md").write_text("""# Picky insider notes (PRIVATE: never commit; load server-side only)

Last refreshed 2026-11-16 (fixture).

## How Picky uses this
- fixture

### Realignment
- Nov 9: The Power 4 title games end after 2026. (ESPN) [checked 2026-11-16]
- 2026: Notre Dame and UConn are the FBS independents. (CFBD) [checked 2026-11-16]

### Revenue sharing / House / NIL
- Nov 12: The SEC is circulating a cap plan with tiered fines above $50.5M. (Dellenger, Yahoo) [checked 2026-11-16]
- Oct 28: The Big Ten adopted a $49M cap on school involvement. (AP, Dellenger, Yahoo) [checked 2026-11-16]
- Nov 13: [UNVERIFIED] reportedly a school is leaving a league. (Somebody) [checked 2026-11-16]

### CFP format and committee
- Nov 9: The CFP goes to 24 teams in 2027; the SEC held out for 16. (ESPN) [checked 2026-11-16]
- Nov 8: Dellenger wrote the committee had Notre Dame No. 11 while the AP had it No. 4. (Dellenger, Yahoo) [checked 2026-11-16]

### Coaching carousel and hot seats
- Nov 2: The hot-seat list names Greg Schiano and Lance Leipold. (USA Today via Yahoo) [checked 2026-11-16]
- Nov 10: Whittingham's seat at Michigan is warming. (Dellenger, Yahoo podcast) [checked 2026-11-16]

### Commissioners and power politics
- Nov 12: Greg Sankey urged Congress to pass the Protect College Sports Act. (Dellenger, Yahoo) [checked 2026-11-16]
- Nov 14: The SEC moved Ole Miss-Oklahoma to Sunday. (NY Post) [checked 2026-11-16]

### NCAA governance, Congress, CBA
- Sep 28: The Senate passed the Protect College Sports Act 77-22. (Ropes & Gray) [checked 2026-11-16]
- Nov 10: Tennessee ran a mock collective bargaining deal. (Dellenger, Yahoo) [checked 2026-11-16]

## Sources
- fixture
""")
(TMP / "takes.md").write_text("""# Picky hot-takes bank (PRIVATE: never commit; load server-side only)

## Verified 2026 facts these takes lean on
- [FACT] The CFP goes to 24 teams in 2027.

## Takes (4)

### Committee / SEC bias (2)
1. The committee ranks SEC logos, not teams. [HOT] [added 2026-11-16]
2. The committee watches film half asleep. [added 2026-11-14]

### Money / NIL / cap (2)
3. The CSC is a book club with a letterhead. [HOT] [added 2026-11-16]
4. Cap fines are a tip on the bar tab. [added 2026-11-16]

## Board voice (how posters talk)
- Unhinged certainty: book it.
- One-game overreaction: natty or fire everyone.
- Committee conspiracy math: count the SEC ties.
- Money cynicism: buyouts and dead money.

## 10 recurring bits / memes this season (2026)
1. x

## Sources and access notes
- fixture
""")
os.environ["PICKY_INSIDER"] = str(TMP / "insider.md")
os.environ["PICKY_HOT_TAKES"] = str(TMP / "takes.md")

sys.path.insert(0, ".")
import chat  # noqa: E402

CTX = {"season": 2026, "current_week": 11, "mode": "leaderboard", "jackpot": {},
       "you": {"team_name": "Clam", "team_id": 9}, "leader": {"team_name": "Chuck P", "team_id": 6},
       "standings_ytd": [{"rank": 1, "team": "Chuck P", "w": 27, "l": 18},
                         {"rank": 2, "team": "Clam", "w": 23, "l": 22}],
       "games_this_week": [{"id": 1, "away": "Wake Forest", "home": "SMU", "line": 1.5,
                            "status": "scheduled", "winner": None}]}


def test_insider_triggers_on_topic():
    cases = {
        "what's the latest on the Big Ten/SEC CFP fight": "24 teams",
        "who's on the hot seat": "Schiano",
        "how does the SEC cap thing work": "tiered fines",
        "are players going to unionize?": "collective bargaining",
        "is Sankey mad?": "Sankey",
        "are conference title games going away?": "title games end",
    }
    for q, want in cases.items():
        n = chat.insider_note(q)
        assert n.startswith("INSIDER NOTES"), q
        assert want in n, (q, n)
        assert "as of Nov 16" in n, n


def test_insider_quiet_off_topic():
    for q in ["what's my record", "who won the UTSA game", "bye picky", "you suck at picks",
              "no cap that was a great pick", "how far am I from a playoff spot", "best cereal?",
              "what's the playoff race look like in our league"]:
        assert chat.insider_note(q) == "", q


def test_dellenger_labels_and_rules():
    n = chat.insider_note("what's the latest on the SEC cap plan and the Big Ten cap")
    assert "Nov 12, Dellenger: The SEC is circulating" in n, n
    assert "Oct 28, AP, Dellenger, Yahoo:" in n, n  # mixed sources name the outlets, not 'Dellenger' alone
    assert "never claim to be Ross Dellenger" in n and "never quote anyone" in n
    p = chat.insider_note("what's Whittingham's status")
    assert "Dellenger, Yahoo podcast:" in p, p  # podcast talk isn't 'Dellenger reported'
    bare = chat.insider_note("what's Dellenger been saying lately?")
    lines = [ln for ln in bare.splitlines() if ln.startswith("- ")]
    assert lines and all(", Dellenger: " in ln for ln in lines), bare


def test_unverified_never_rides():
    for q in ["any realignment news? anyone leaving a league?", "what's new with NIL and revenue sharing"]:
        assert "UNVERIFIED" not in chat.insider_note(q)
        assert "leaving a league" not in chat.insider_note(q)


def test_insider_never_in_prefix():
    pre = chat.prompt_prefix(CTX)
    assert "INSIDER NOTES (" not in pre
    for bit in ("Schiano", "tiered fines", "Dellenger", "book club", "BOARD VOICE"):
        assert bit not in pre, bit


def test_hot_takes_preferred_and_tag_stripped():
    secs = chat.load_takes()
    assert all("[HOT]" not in t for v in secs.values() if isinstance(v, list) for t in v)
    for q in ["what do you think of the committee", "the SEC gets every call from the committee"]:
        n = chat.takes_note(q)
        assert "ranks SEC logos" in n, n  # the [HOT] take beats the mild one
        assert "[HOT]" not in n


def test_generic_hot_take_ask():
    n = chat.takes_note("give me your most outrageous hot take")
    assert "HOT TAKES" in n and ("ranks SEC logos" in n or "book club" in n), n


def test_money_topic():
    assert "book club" in chat.takes_note("is NIL ruining college football?")


def test_board_voice_rides_with_takes_only():
    n = chat.opinion_note("what do you think of the committee", CTX)
    assert "BOARD VOICE" in n, n
    assert "BOARD VOICE" not in chat.opinion_note("what's my record", CTX)
    assert "BOARD VOICE" not in chat.opinion_note("who won the UTSA game", CTX)


def main():
    fails = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("ok  ", name)
            except AssertionError as e:
                fails += 1
                print("FAIL", name, str(e)[:400])
    print("all passed" if not fails else f"{fails} failed")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
