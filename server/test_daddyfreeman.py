"""
Tests for DaddyFreeman's special treatment (harder roasts; the land
acknowledgment opener is switched off).

Run:  ./.venv/bin/python3 -I test_daddyfreeman.py

Pins: the acknowledgment NEVER fires (not for him, not for anyone, first
message or not), an old one is still stripped from history; the roast rider
only goes into his system prompt and he still gets his facts every answer.
"""

from __future__ import annotations

import asyncio
import sys

sys.path.insert(0, ".")
import chat  # noqa: E402

DF = chat.DADDYFREEMAN_USER_ID
OTHER = "35e1c78c-0fbc-4d9e-bc8d-f7df5ea73908"  # Chuck P
FIRST = [{"role": "user", "content": "who's winning?"}]
FOLLOW = FIRST + [{"role": "assistant", "content": "Not you."},
                  {"role": "user", "content": "and me?"}]


def test_opener_is_off():
    assert chat.LAND_ACK_ENABLED is False
    for msgs in (FIRST, FOLLOW, FIRST + [{"role": "user", "content": "hello?"}]):
        assert chat.land_ack_for(DF, msgs) is None
        assert chat.land_ack_for(OTHER, msgs) is None
        assert chat.land_ack_for("", msgs) is None
    # not even after many conversations (the old rotation counter)
    assert all(chat.land_ack_for(DF, FIRST) is None for _ in range(10))


def test_rider_doesnt_promise_an_opener():
    assert "already handled" not in chat.DADDYFREEMAN_RIDER
    assert "roast him harder" in chat.DADDYFREEMAN_RIDER


def test_he_gets_his_facts_every_answer():
    ctx = {"you": {"team_name": "DaddyFreeman"}, "games_this_week": [], "mode": "leaderboard",
           "standings_ytd": [{"team": "TG", "rank": 1, "w": 30, "l": 14},
                             {"team": "DaddyFreeman", "rank": 2, "w": 20, "l": 24}], "weekly_records": {},
           "last_week": {}, "jackpot": {}, "daddyfreeman": True}
    turn = chat._user_turn(ctx, "Who's winning the pool?")
    assert "DaddyFreeman (the user" in turn, "the extra roast needs his facts in the focus block"


def test_ack_names_the_three_fires_and_no_em_dash():
    for a in chat.LAND_ACKS:
        for people in ("Potawatomi", "Ojibwe", "Odawa", "Three Fires"):
            assert people in a, f"{people} missing from: {a[:50]}"
        assert "—" not in a


def test_strip_from_history():
    ack = chat.LAND_ACKS[1]
    assert chat.strip_land_ack(ack + "\n\nYou're 9th.") == "You're 9th."
    assert chat.strip_land_ack("You're 9th.") == "You're 9th."


def test_opener_streams_first():
    async def model():
        yield chat._sse({"content": "reply"})

    async def collect(opener):
        return [c async for c in chat._with_opener(opener, model())]

    got = asyncio.run(collect("ACK"))
    assert got == [chat._sse({"content": "ACK\n\n"}), chat._sse({"content": "reply"})]
    assert asyncio.run(collect(None)) == [chat._sse({"content": "reply"})]


def test_rider_only_in_his_prompt():
    ctx = {"you": {"team_name": "DaddyFreeman"}, "games_this_week": [], "mode": "leaderboard",
           "standings_ytd": [], "weekly_records": {}, "last_week": {}, "jackpot": {}}
    assert chat.DADDYFREEMAN_RIDER not in chat._system_prompt(ctx)
    assert chat.DADDYFREEMAN_RIDER in chat._system_prompt({**ctx, "daddyfreeman": True})


if __name__ == "__main__":
    tests = [
        test_opener_is_off,
        test_rider_doesnt_promise_an_opener,
        test_he_gets_his_facts_every_answer,
        test_ack_names_the_three_fires_and_no_em_dash,
        test_strip_from_history,
        test_opener_streams_first,
        test_rider_only_in_his_prompt,
    ]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  ok  {t.__name__}")
        except AssertionError as e:
            print(f"FAIL  {t.__name__}: {e}")
            failed += 1
    print()
    print("ALL GREEN" if failed == 0 else f"{failed} FAILED")
    sys.exit(0 if failed == 0 else 1)
