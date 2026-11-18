"""
Ackbar is Picky's nemesis on every path (nemesis.py + group_chat send point; Charlie 2026-11-17:
"make sure he still knows to be antagonistic to ackbar always"). Code-lane answers to Ackbar carry a
jab, a named-Ackbar ask from another member carries one, a non-Ackbar caller with no mention gets
none, every jab's facts are true, good numbers get spun not mocked, and no jab repeats back to back.
No model, no network.

Run:  ./.venv/bin/python3 -I test_nemesis.py
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
TMP = Path(tempfile.mkdtemp())
os.environ["PICKY_NO_REPEAT_STATE"] = str(TMP / "no_repeat.json")  # never the real ledger
os.environ["PICKY_BITS_STATE"] = str(TMP / "bits.json")
import nemesis  # noqa: E402
import no_repeat  # noqa: E402

LORE = {"members": {"Ackbar": [
    "Ackbar, 2026 so far (through 2026-11-17): best week 4-0 (week 10), worst week 1-3 (week 8); perfect weeks: "
    "week 10; longest hit streak 5 picks, longest miss streak 2; last 8 picks 7-1; underdog outright wins picked: 5; "
    "takes the favorite 75% of the time.",
    "Ackbar, worst blown picks (a favorite that missed the cover by losing outright): Alabama -13.5 missed the cover "
    "by 27.5, lost outright 17-31 (Alabama at Florida State, 2025 week 1); Texas -8.5 missed the cover by 11.5, lost "
    "outright 24-27 (Texas at Oklahoma, 2026 week 6).",
    "Ackbar, stats-page oddity: Ackbar is 1-5 in Indiana games over the last two seasons (2025-2026), whichever "
    "side: 0-2 taking Indiana, 1-3 picking against them.",
    "Ackbar, stats-page oddity: Ackbar has picked against Northwestern 4 times over the last two seasons "
    "(2025-2026) and gone 0-4.",
]}, "h2h": {"Ackbar": {"Chuck P": [18, 21], "TG": [16, 7]}}}
FACT = "Ackbar took Penn State -2.5 in week 5 (Penn State at Northwestern); Penn State lost 13-34 and missed the cover by 23.5."
BOOK = {"checked_at": time.time(), "standing": {"Ackbar": {"rank": 2, "w": 28, "l": 20, "good": True}},
        "facts_now": [FACT],
        "members": {"Ackbar": [
            {"id": "m1", "text": "Penn State missed the cover by 23.5. you took the favorite because of course you did",
             "kind": "roast", "facts": [FACT]},
            {"id": "m2", "text": "stale line about a fact that changed", "kind": "roast", "facts": ["gone"]}]}}
SOURCE = " ".join(LORE["members"]["Ackbar"]) + " " + FACT + " 28-20 2nd 18-21 16-7"


def ledger() -> no_repeat.Ledger:
    return no_repeat.Ledger(tempfile.mktemp(dir=TMP))


def all_jabs(you: bool, asker: str | None = None) -> list[str]:
    f = nemesis.facts(LORE, BOOK)
    return [c["text"] for c in nemesis.code_jabs(f, you, asker)] + [c["text"] for c in nemesis.book_jabs(BOOK)]


def test_facts_parsed():
    f = nemesis.facts(LORE, BOOK)
    assert f["fav"] == 75 and f["last8"] == (7, 1) and f["record"] == (28, 20, 2, True)
    assert [b["team"] for b in f["blown"]] == ["Alabama", "Texas"] and f["blown"][0]["score"] == "17-31"
    assert f["odd"] == [("Indiana", 1, 5)] and f["against"] == [("Northwestern", 0, 4)]


def test_every_jab_number_is_in_the_source_facts():
    for you, asker in ((True, None), (False, "Chuck P"), (False, "TG")):
        for j in all_jabs(you, asker):
            for n in re.findall(r"\d+(?:\.\d+)?", j):
                assert n in SOURCE, (n, j)


def test_stale_book_lines_and_records_are_skipped():
    assert [c["text"][:10] for c in nemesis.book_jabs(BOOK)] == ["Penn State"]
    old = {**BOOK, "checked_at": time.time() - 10 * 86400}
    assert "record" not in nemesis.facts(LORE, old)


def test_good_numbers_spun_not_mocked():
    f = nemesis.facts(LORE, BOOK)
    rec = [c["text"] for c in nemesis.code_jabs(f, True) if c["angle"] == "nemesis:record"][0]
    assert rec.startswith("You're 28-20 and 2nd") and "training wheels" in rec  # a subjectless '28-20 and 2nd' read as someone else's
    assert not re.search(r"looking for a win|fluke|bad|terrible|awful", " ".join(all_jabs(True)), re.I)
    # h2h vs the asker only when Ackbar is losing it (TG: Ackbar leads 16-7, no jab off that)
    assert not [c for c in nemesis.code_jabs(f, False, "TG") if "h2h" in c["angle"]]
    assert [c for c in nemesis.code_jabs(f, False, "Chuck P") if "h2h" in c["angle"]]


def test_guardrails():
    bad = re.compile(r"\b(mom|mother|dad|father|wife|kids?|sister|brother|family|fat|ugly|bald|kill|die|hurt you)\b", re.I)
    for j in all_jabs(True) + all_jabs(False, "Chuck P"):
        assert not bad.search(j) and not no_repeat.banned_tic(j), j


def test_code_lane_answer_for_ackbar_gets_a_jab():
    ans = "I'd take Illinois +8.5 (slight lean): SP+ has Iowa by 8; Iowa has SP+ #18 Nebraska on deck."
    out = nemesis.tail(ans, "who you got in Iowa Illinois", "Ackbar", "dm:a", force=True, ledger=ledger(),
                       lore=LORE, book=BOOK)
    assert out.startswith(ans) and len(out) > len(ans)  # the answer first, the jab is the tail
    assert out[len(ans):].strip() in all_jabs(True)


def test_named_ackbar_from_another_member_gets_a_third_person_jab():
    out = nemesis.tail("Ackbar is 28-20, 2nd.", "hows ackbar doing", "Chuck P", "dm:c", ledger=ledger(),
                       lore=LORE, book=BOOK)
    j = out[len("Ackbar is 28-20, 2nd."):].strip()
    assert j and j in all_jabs(False, "Chuck P") and "Ackbar" in j and not re.search(r"\byou take\b", j)


def test_group_mention_can_use_book_lines_addressed_to_him():
    L, got = ledger(), set()
    for _ in range(12):
        t = nemesis.jab("what did admiral akbar take", "TG", "group:g", group=True, ledger=L, lore=LORE, book=BOOK)["text"]
        L.record(t, target="Ackbar", chat_id="group:g")
        assert t.startswith("Ackbar"), t  # about him, or talking to him by name: never a bare 'you' at TG
        got.add(t)
    assert "Ackbar, Penn State missed the cover by 23.5. you took the favorite because of course you did" in got


def test_no_jab_for_others():
    for q, team in (("who you got in Iowa Illinois", "Chuck P"), ("hows the standings", "TG"), ("best beer", None)):
        assert nemesis.tail("Iowa -6.5.", q, team, "dm:x", force=True, ledger=ledger(), lore=LORE, book=BOOK) == "Iowa -6.5."


def test_model_reply_with_a_jab_already_is_left_alone_unless_forced():
    r = "Iowa covers. And you'd take the favorite anyway, naturally."
    assert nemesis.tail(r, "who you got", "Ackbar", "dm:a", ledger=ledger(), lore=LORE, book=BOOK) == r
    soft = "Ackbar is 28-20 and still looking for a win that isn't a total fluke."
    assert nemesis.tail(soft, "hows ackbar doing", "TG", "dm:t", ledger=ledger(), lore=LORE, book=BOOK) != soft


def test_never_back_to_back_even_when_everything_is_on_cooldown():
    L = ledger()
    outs = [nemesis.tail("Iowa.", "who you got", "Ackbar", "dm:a", force=True, ledger=L, lore=LORE, book=BOOK)
            for _ in range(25)]
    assert all(o != "Iowa." for o in outs)
    assert all(a != b for a, b in zip(outs, outs[1:])), outs
    assert len(set(outs)) >= 5


def test_fails_open():
    with patch.object(nemesis, "jab", side_effect=RuntimeError("boom")):
        assert nemesis.tail("x.", "ackbar?", "TG", "dm:x", ledger=ledger()) == "x."


def test_live_cache_jabs_are_true():
    """Against the real lore + burn book (skipped when the caches aren't there)."""
    if not nemesis.LORE.exists():
        print("  (skip: no lore cache)")
        return
    import json
    lore = json.loads(nemesis.LORE.read_text())
    import burn_book
    book = burn_book.load()
    src = " ".join(lore["members"].get("Ackbar") or []) + " " + " ".join(book.get("facts_now") or [])
    st = (book.get("standing") or {}).get("Ackbar") or {}
    src += f" {st.get('w')}-{st.get('l')} " + " ".join(f"{w}-{l}" for w, l in (lore.get("h2h") or {}).get("Ackbar", {}).values())
    f = nemesis.facts(lore, book)
    rank = f.get("record", (0, 0, 0, 0))[2]
    for c in nemesis.code_jabs(f, True) + nemesis.code_jabs(f, False, "Chuck P") + nemesis.book_jabs(book):
        for n in re.findall(r"\d+(?:\.\d+)?", c["text"]):
            assert n in src or n == str(rank), (n, c["text"])


def test_group_chat_send_point_code_lane():
    """group_chat._answer: a fact-lane code answer to Ackbar ends with a jab; to TG it doesn't; TG naming Ackbar does."""
    import group_chat as gc
    import router

    async def nothing(*a, **k):
        return None

    async def jwt(*a, **k):
        return "jwt"

    ans = "I'd take Illinois +8.5 (slight lean): SP+ has Iowa by 8."

    async def brain(draft, **k):
        return ans, None

    def run(team, text):
        with patch.object(gc.VIEWER, "session", nothing), patch.object(gc.VIEWER, "member_session", jwt), \
                patch.object(gc.VIEWER, "sender", return_value=gc.Sender(handle="+1", team=team, user_id="u")), \
                patch.object(gc, "save_turn", nothing), patch.object(gc, "load_history", return_value=[]), \
                patch.object(gc, "THREAD_RULES", False), \
                patch.object(gc.router, "classify", return_value=router.Route("fact", "pick")), \
                patch.object(gc.picky_brain, "run", brain), \
                patch.object(nemesis, "facts", lambda *a, **k: nemesis.__dict__["_real_facts"](LORE, BOOK)), \
                patch.object(nemesis, "book_jabs", lambda *a, **k: nemesis.__dict__["_real_book"](BOOK)):
            return asyncio.run(gc._answer("+1", text, "", dm=True))["reply"]

    nemesis._real_facts, nemesis._real_book = nemesis.facts, nemesis.book_jabs
    a = run("Ackbar", "who you got in Iowa Illinois")
    assert a.startswith(ans[:20]) and a.strip() != ans and a[len(ans):].strip() in all_jabs(True), a
    assert run("TG", "who you got in Iowa Illinois") == ans
    b = run("TG", "who would ackbar take in Iowa Illinois")
    assert b[len(ans):].strip() in all_jabs(False, "TG"), b


if __name__ == "__main__":
    fails = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok   {name}")
            except Exception as e:  # noqa: BLE001
                fails += 1
                import traceback
                traceback.print_exc()
                print(f"FAIL {name}: {e!r}")
    print("all green" if not fails else f"{fails} failed")
    sys.exit(1 if fails else 0)
