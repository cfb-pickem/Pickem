"""
no_repeat.py: normalize, similar, the ledger windows, banned tics, the
30-reply phrase/opener cooldown, persistence across processes. No network, no LLM.

Run:  ./.venv/bin/python3 -I test_no_repeat.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
_TMP = tempfile.mkdtemp(prefix="picky-norepeat-test-")
os.environ["PICKY_NO_REPEAT_STATE"] = str(Path(_TMP) / "default.json")
import no_repeat as nr  # noqa: E402

DAY = nr.DAY
_n = [0]


def fresh() -> nr.Ledger:
    _n[0] += 1
    return nr.Ledger(Path(_TMP) / f"l{_n[0]}.json")


def flush(L: nr.Ledger, now: float) -> None:
    """30 unrelated lines, so the last-30 opener/phrase cooldown is out of the way."""
    for i in range(nr.RECENT_N):
        L.record(f"filler{i} zebra{i} quokka{i} marmot{i}", target=f"M{i}", now=now - 30 * DAY + i)


def test_normalize():
    assert nr.normalize("You’re   SO bad — honestly!!") == "youre so bad honestly"
    assert nr.normalize("Boise State -13.5, missed by 8.5.") == "boise state 13.5 missed by 8.5"
    assert nr.normalize("Café   crème") == "cafe creme"
    assert nr.normalize("") == ""


def test_similar_reworded_vs_different():
    same = [("that's the best you got? you had all week", "you had all week and that's the best you came up with?"),
            ("bold of you to talk after that week", "Bold of you to talk after that weekend."),
            ("you took Clemson +10.5 and missed by 30.5. bold", "bold move taking Clemson +10.5, missed by 30.5")]
    for a, b in same:
        assert nr.similar(a, b) >= nr.SIM_THRESHOLD, (a, b, nr.similar(a, b))
    diff = [("own it, I'm a bot with a better week than you", "nice try, seen better hacks from Big 12 refs"),
            ("k", "anyway"),
            ("you faded Northwestern four times, maybe stop", "the hook knows your address at this point")]
    for a, b in diff:
        assert nr.similar(a, b) < nr.SIM_THRESHOLD, (a, b, nr.similar(a, b))
    assert nr.similar("x", "x") == 1.0 and nr.similar("", "x") == 0.0


def test_banned_tics():
    for t in ["You're sitting pretty up there", "chalk merchant energy", "here's your participation trophy",
              "For the record: Boise lost", "I'm definitely flesh and blood", "Let that sink in.", "let's be real",
              "I'm just here to watch", "at least I covered", "there is no beer in the data",
              "a 9-billion-parameter model", "running on a Mac mini with 16 GB", "As an AI, I", "Look, you lost",
              "that's not okay", "let's keep it respectful", "I can't say that"]:
        assert nr.banned_tic(t), t
    for t in ["k", "that's the best you got? you had all week", "you took Clemson +10.5 and missed by 30.5",
              "lookout for the dog", "at least it was close"]:
        assert nr.banned_tic(t) is None, t
    assert nr.banned_tic("I’m just here to help")  # curly apostrophe


def test_numbers_and_punchline():
    assert nr.numbers("you took Clemson +10.5 in week 1, lost 10-51, missed by 30.5 in 2026") == ["10-51", "10.5", "30.5"]
    assert nr.numbers("in week 11 you went 4") == []
    assert nr.punchline("that's the best you got? you had all week") == "you had all week"
    assert nr.punchline("k") is None


def test_verbatim_never_on_target_or_chat():
    L = fresh()
    t0 = time.time() - 200 * DAY
    L.record("you had all week and that's what you brought", target="TG", chat_id="g1", now=t0)
    assert "verbatim" in (L.is_repeat("You had all week, and that's what you brought!", target="TG") or "")
    assert "verbatim" in (L.is_repeat("you had all week and that's what you brought", chat_id="g1") or "")
    assert "last 30" in L.is_repeat("you had all week and that's what you brought", target="Brian", chat_id="g2")
    flush(L, time.time())
    assert L.is_repeat("you had all week and that's what you brought", target="Brian", chat_id="g2") is None


def test_target_window_21_days():
    L = fresh()
    now = time.time()
    L.record("imagine fading Northwestern four times and still talking", target="TG", chat_id="dm:tg", now=now - 20 * DAY)
    r = L.is_repeat("imagine fading Northwestern 4 times and still talking", target="TG", chat_id="g", now=now)
    assert r and "TG" in r, r
    flush(L, now)
    # 22 days later, same person, different chat: fresh
    assert L.is_repeat("imagine fading Northwestern 4 times and still talking", target="TG", chat_id="g",
                       now=now + 2 * DAY) is None


def test_group_short_cooldown_and_chat_never():
    L = fresh()
    now = time.time()
    L.record("the hook has your home address at this point", target="TG", chat_id="g", now=now - 1 * DAY)
    r = L.is_repeat("the hook has your home address by now", target="Brian", chat_id="dm:brian", now=now)
    assert r and "group" in r, r
    flush(L, now)
    assert L.is_repeat("the hook has your home address by now", target="Brian", chat_id="dm:brian",
                       now=now + 3 * DAY) is None
    # same chat: still a repeat 40 days on, whoever it's aimed at
    r = L.is_repeat("the hook has your home address by now", target="Brian", chat_id="g", now=now + 40 * DAY)
    assert r and "chat" in r, r


def test_same_angle_by_label_and_number():
    L = fresh()
    now = time.time()
    L.record("2-7 in Oklahoma games whichever side. Oklahoma has your number", target="Chuck P", chat_id="g",
             angle="Chuck P is 2-7 in Oklahoma games", now=now - 5 * DAY)
    r = L.is_repeat("you and the Sooners, 2-7 both ways. impressive range", target="Chuck P", now=now)
    assert r and "angle" in r, r
    r = L.is_repeat("totally new words about the same idea", target="Chuck P", now=now,
                    angle="Chuck P is 2-8 in Oklahoma games")
    assert r and "angle" in r, r
    assert L.is_repeat("you and the Sooners, 2-7 both ways. impressive range", target="Brian", chat_id="dm",
                       now=now) is None


def test_same_punchline():
    L = fresh()
    now = time.time()
    L.record("Clemson plus the points, missed by thirty. and you want to talk?", target="TG", now=now - DAY)
    r = L.is_repeat("you went winless on the week, and you want to talk?", target="TG", now=now)
    assert r and ("punchline" in r or "phrase" in r), r


def test_opener_per_target_and_last_30():
    L = fresh()
    now = time.time()
    L.record("bold strategy there, fading the Sooners again", target="TG", now=now - 10 * DAY)
    flush(L, now)
    r = L.is_repeat("bold strategy there cotton, let's see how it plays", target="TG", now=now)
    assert r and "opener" in r and "TG" in r, r
    # out of the last-30 window: fresh for someone else
    assert L.is_repeat("bold strategy there cotton, let's see how it plays", target="Brian", now=now) is None
    # inside the last 30: a repeat for anyone
    L2 = fresh()
    L2.record("bold strategy there, fading the Sooners again", target="TG", now=now - 10 * DAY)
    r = L2.is_repeat("bold strategy there cotton, let's see how it plays", target="Brian", now=now)
    assert r and "last 30" in r, r


def test_phrase_cooldown_last_30():
    L = fresh()
    now = time.time()
    L.record("your bracket looks like a crime scene", target="TG", chat_id="g", now=now - 10 * DAY)
    r = L.is_repeat("nice week, and your bracket looks like modern art", target="Brian", chat_id="dm", now=now)
    assert r and "phrase" in r, r
    # league words + stop words don't count as a stock phrase
    L.record("you missed the cover by a mile", target="TG", now=now - 9 * DAY)
    assert L.is_repeat("Penn State never got near it, missed the cover by plenty", target="Brian", now=now) is None
    assert L.stale_phrases("totally unrelated words here") == []


def test_tic_beats_everything_and_screen_falls_through():
    L = fresh()
    assert "tic" in (L.is_repeat("sitting pretty up top") or "")
    L.record("that's the best you got? you had all week", target="TG", chat_id="g")
    pick = L.screen(["that's the best you got? you had all week", "sitting pretty, huh",
                     {"text": "nice try, seen better hacks from Big 12 refs", "angle": "type:jailbreak:refs"}],
                    target="TG", chat_id="g")
    assert isinstance(pick, dict) and "Big 12" in pick["text"], pick
    assert L.screen(["that's the best you got? you had all week"], target="TG", chat_id="g") is None


def test_persistence_lock_and_cross_process():
    p = Path(_TMP) / "shared.json"
    a, b = nr.Ledger(p), nr.Ledger(p)
    a.record("you stood on 12 against a dealer six, brave", target="TG", chat_id="g")
    assert b.is_repeat("you stood on 12 against a dealer six, brave", target="TG")
    code = ("import sys; sys.path.insert(0, %r); import no_repeat as nr; "
            "nr.Ledger(%r).record('the slots carried you and you know it', target='Clam', chat_id='g')") % (str(HERE), str(p))
    subprocess.run([sys.executable, "-I", "-c", code], check=True)
    assert a.is_repeat("the slots carried you and you know it", target="Clam")
    assert len(a._load()["entries"]) == 2
    assert oct(p.stat().st_mode & 0o777) == "0o600"


def test_prune_keeps_last_30():
    L = fresh()
    old = time.time() - 400 * DAY
    for i in range(50):
        L.record(f"ancient line number {i} about walrus{i}", target="TG", now=old + i)
    L.record("brand new line about otters", target="TG")
    ents = L._load()["entries"]
    assert len(ents) == nr.RECENT_N, len(ents)  # out-of-window lines pruned except the last-30 tail
    assert "verbatim" in (L.is_repeat("ancient line number 3 about walrus3", target="TG") or "")  # hash kept forever


def test_default_ledger_env():
    assert str(nr.default_ledger().path) == os.environ["PICKY_NO_REPEAT_STATE"]


if __name__ == "__main__":
    tests = [v for k, v in dict(globals()).items() if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"  ok  {fn.__name__}")
        except AssertionError as e:
            print(f"FAIL  {fn.__name__}: {e}")
            failed += 1
        except Exception as e:  # noqa: BLE001
            print(f"ERR   {fn.__name__}: {type(e).__name__}: {e}")
            failed += 1
    print("\nALL GREEN" if not failed else f"\n{failed} FAILED")
    sys.exit(1 if failed else 0)
