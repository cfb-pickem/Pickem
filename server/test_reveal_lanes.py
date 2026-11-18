"""Rule 1c across every lane (integration 2026-11-18): one minute before the week's first kickoff no unrevealed pick
(or a count / consensus built from one) reaches the context, the prompt or any code lane's answer; one minute
after, it does. Regular season: the whole week reveals at its first kickoff.
Run:  ./.venv/bin/python3 -I test_reveal_lanes.py
"""
from __future__ import annotations

import asyncio
import datetime as dt
import re
import sys
from unittest.mock import patch

sys.path.insert(0, ".")
import chat  # noqa: E402
import router  # noqa: E402

KICK = dt.datetime(2026, 11, 21, 11, 0, 0)  # CT, the week's first kickoff
WEEK = 12
TEAMS = [{"team_id": 10, "team_name": "Caller", "user_id": "uuid-caller", "active": True},
         {"team_id": 20, "team_name": "Rival", "user_id": "uuid-rival", "active": True},
         {"team_id": 30, "team_name": "Other", "user_id": "uuid-other", "active": True}]
GAMES = [{"GameId": 1, "Away": "Illinois", "Home": "Michigan", "line": -3.5, "Status": "scheduled", "winner": None,
          "week": WEEK, "tiebreaker": False, "Start (CT)": KICK.isoformat(timespec="seconds")},
         {"GameId": 2, "Away": "Ohio State", "Home": "Penn State", "line": -6.5, "Status": "scheduled", "winner": None,
          "week": WEEK, "tiebreaker": False, "Start (CT)": (KICK + dt.timedelta(hours=4)).isoformat(timespec="seconds")}]
PICKS = [{"team_id": 10, "game_id": 1, "pick": "Michigan", "by_slots": False, "jackpot": False},
         {"team_id": 20, "game_id": 1, "pick": "Illinois", "by_slots": False, "jackpot": False},
         {"team_id": 20, "game_id": 2, "pick": "Penn State", "by_slots": False, "jackpot": False},
         {"team_id": 30, "game_id": 2, "pick": "Penn State", "by_slots": False, "jackpot": False}]
ASKS = ["what did Rival take this week?", "what did Rival pick in Ohio State at Penn State?", "who took Penn State?",
        "how many people took Illinois?", "most people took who in Illinois at Michigan?", "what's the consensus this week?",
        "who hasn't picked yet?", "what's my strategy?", "should I fade the field?", "who you got in Illinois at Michigan?",
        "best case for the other side?", "how's Rival doing this week?", "what are everyone's picks?"]
# an unrevealed pick or a count built from them, said about the other members
LEAK = re.compile(r"Rival\W[^.]{0,80}\b(Illinois|Penn State)\b|\b(Illinois|Penn State)\b[^.]{0,60}\bRival\b"
                  r"|\b(Other)\b[^.]{0,60}\bPenn State\b|\b\d+ of \d+ (picks|members)\b|\bmost (people|of you|members) "
                  r"(took|are on|have)\b|\beveryone (took|is on)\b", re.I)


async def _ctx(now: dt.datetime) -> dict:
    async def fake_rest(client, path, jwt):
        if path.startswith("teams?"):
            return TEAMS
        if path.startswith("slots_jackpot"):
            return [{"pulls_since_pop": 1, "last_pop_at": None, "last_pop_team_id": None}]
        if "order=cfb_season.desc" in path:
            return [{"cfb_season": 2026}]
        if "Status=neq.final" in path and "order=week.asc" in path:
            return [{"week": WEEK, "Status": "scheduled"}]
        if f"week=eq.{WEEK}" in path and "GameId" in path:
            return GAMES
        if "picks?select=team_id,game_id,pick,by_slots,jackpot" in path:
            return PICKS
        if "picks?select=team_id,game_id,pick,by_slots,week" in path:
            return []
        if "Status=eq.final" in path:
            return []
        return []
    with patch.object(chat, "_rest_get", side_effect=fake_rest), patch.object(chat, "_now_ct_naive", return_value=now):
        return await chat._build_context(client=None, jwt="x", user_id="uuid-caller")


def _everything(now: dt.datetime) -> tuple[dict, str]:
    ctx = asyncio.run(_ctx(now))
    with patch.object(chat, "_now_ct_naive", return_value=now):
        text = [chat.render_context(ctx)]
        for q in ASKS:
            rt = router.classify(q)
            if rt.lane == "fact":
                try:
                    text.append(str(router.fact_answer(rt.kind, ctx, q) or ""))
                except Exception:  # noqa: BLE001 - a lane that can't run on this ctx says nothing
                    pass
    return ctx, "\n".join(text)


def test_one_minute_before_the_reveal_nothing_leaks():
    ctx, text = _everything(KICK - dt.timedelta(minutes=1))
    assert not ctx["revealed_picks_this_week"].get("Rival"), ctx["revealed_picks_this_week"]
    m = LEAK.search(text)
    assert not m, ("LEAK before the reveal", text[max(0, m.start() - 120): m.end() + 120])


def test_one_minute_after_the_reveal_the_picks_are_known():
    ctx, text = _everything(KICK + dt.timedelta(minutes=1))
    rival = {p["game_id"]: p["pick"] for p in ctx["revealed_picks_this_week"].get("Rival", [])}
    assert rival == {1: "Illinois", 2: "Penn State"}, rival  # the whole week reveals at its first kickoff
    assert "Penn State" in chat.render_context(ctx), "revealed picks should reach the prompt"


if __name__ == "__main__":
    fails = 0
    for name, f in list(globals().items()):
        if name.startswith("test_"):
            try:
                f()
                print("  ok ", name)
            except AssertionError as e:
                fails += 1
                print("FAIL ", name, str(e)[:600])
    print("ALL GREEN" if not fails else f"{fails} FAILED")
    sys.exit(1 if fails else 0)
