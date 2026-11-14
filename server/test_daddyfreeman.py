"""
Tests for DaddyFreeman's special treatment (land acknowledgment + harder roasts).

Run:  ./.venv/bin/python3 -I test_daddyfreeman.py

Pins: the acknowledgment only fires for his user id, only on the first
message of a conversation, rotates, and is stripped from the history the
model sees; the roast rider only goes into his system prompt.
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


def test_ack_only_on_first_message_for_him():
    assert chat.land_ack_for(DF, FIRST) in chat.LAND_ACKS
    assert chat.land_ack_for(DF, FOLLOW) is None
    # a failed first reply leaves two user turns and no assistant turn: not a start
    assert chat.land_ack_for(DF, FIRST + [{"role": "user", "content": "hello?"}]) is None


def test_never_for_other_members():
    assert chat.land_ack_for(OTHER, FIRST) is None
    assert chat.land_ack_for("", FIRST) is None


def test_ack_rotates():
    seen = {chat.land_ack_for(DF, FIRST) for _ in chat.LAND_ACKS}
    assert seen == set(chat.LAND_ACKS), "every variant should come up once per cycle"


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
        test_ack_only_on_first_message_for_him,
        test_never_for_other_members,
        test_ack_rotates,
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
