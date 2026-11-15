"""
Picky in the iMessage group chat: trigger, watcher rules, and group privacy.

Run:  ./.venv/bin/python3 -I test_picky_group.py
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import sys
import time
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "tools" / "picky-imessage"))
import chat  # noqa: E402
import group_chat as gc  # noqa: E402
import watcher  # noqa: E402

watcher.SELF_HANDLES = {"picky-bot@example.com"}
watcher.TEST_DM_HANDLES = {"+15555550199"}

YES = ["@Picky who's winning?", "@picky", "@PICKY you there", "@ Picky who's first", "yo @Picky, thoughts?",
       "hey (@picky)", "@Picky's take?", "what do you think @picky?"]
NO = ["picky who's winning", "Picky is dumb", "@Pickyy hi", "@pickyface", "email picky-bot@example.com",
      "x@picky", "so picky about picks", "@ pickyy", "@Pick y", ""]


def test_trigger_matching():
    for rx in (watcher.TRIGGER, gc.TRIGGER):
        for s in YES:
            assert rx.search(s), f"should trigger: {s!r}"
        for s in NO:
            assert not rx.search(s), f"should NOT trigger: {s!r}"
    assert watcher.triggered("@Picky hi") and not watcher.triggered("picky hi")


def test_confirmed_mention_triggers():
    body = b"\x04\x0bstreamtyped...NSString\x01\x94\x84\x01+\x0bPicky hello__kIMMentionConfirmedMention...picky-bot@example.com"
    assert watcher.triggered("Picky hello", body)
    other = body.replace(b"picky-bot@example.com", b"someone@else.com")
    assert not watcher.triggered("Picky hello", other)


def test_strip_trigger():
    assert gc.strip_trigger("@Picky who's winning?") == "who's winning?"
    assert gc.strip_trigger("yo @ picky, thoughts?") == "yo, thoughts?"
    assert gc.strip_trigger("@Picky, who's first?") == "who's first?"
    assert gc.strip_trigger("@Picky") == "hey"


def test_norm_handle():
    assert gc.norm_handle("(555) 555-0199") == "+15555550199"
    assert gc.norm_handle("+1 555 555 0199") == "+15555550199"
    assert gc.norm_handle("tel:+15555550199") == "+15555550199"
    assert gc.norm_handle("Mailto:Foo@Bar.com") == "foo@bar.com"


def _row(**kw):
    r = {"ROWID": 1, "text": "@Picky hi", "attributedBody": None, "date": int((time.time() - watcher.APPLE_EPOCH) * 1e9),
         "handle": "+15555550100", "chat_guid": "iMessage;+;chat1", "style": watcher.GROUP_STYLE}
    r.update(kw)
    return r


def test_should_act():
    now = time.time()
    assert watcher.should_act(_row(), now) == (True, "ok")
    assert watcher.should_act(_row(text="picky hi"), now)[0] is False
    # DMs: no @Picky needed; the sidecar decides who gets answered
    assert watcher.should_act(_row(style=45, text="how about that SMU game?"), now) == (True, "ok")
    assert watcher.should_act(_row(style=45, text=None), now) == (False, "empty")
    assert watcher.should_act(_row(style=45, associated_message_type=2000), now) == (False, "tapback")
    assert watcher.should_act(_row(associated_message_type=2000), now) == (False, "tapback")
    assert watcher.should_act(_row(date=1), now) == (False, "stale")


def test_rate_limit():
    t = [1000.0]
    rl = watcher.RateLimit(clock=lambda: t[0])
    assert rl.wait_needed() == 0.0
    rl.mark()
    t[0] += 4
    assert abs(rl.wait_needed() - 6.0) < 1e-9
    for _ in range(59):
        t[0] += 10
        rl.mark()
    t[0] += 10
    assert rl.wait_needed() is None   # 60 in the last hour
    t[0] += 3600
    assert rl.wait_needed() == 0.0


def test_group_trim():
    long = "One. Two! Three? Four. Five."
    assert gc.group_trim(long) == "One. Two! Three?"
    assert gc.group_trim("nice milk, freak. A. B. C. D.") == "nice milk, freak. A. B. C."
    assert len(gc.group_trim("word " * 300)) <= gc.MAX_CHARS + 3


def test_status_guard():
    for bad in ["No, he hasn't. His picks stay locked.", "He hasn't made his picks yet.", "Yep, he has made them.",
                "Ackbar already submitted his picks.", "TG still hasn't picked lol",
                "Nobody has picked yet, so relax.", "Everyone has submitted.", "Ackbar has picked already."]:
        assert gc.status_guard("", bad) == gc.STATUS_LINE, bad
    assert gc.status_guard("", "nice milk, freak. No, he hasn't.").startswith("nice milk, freak. ")
    for ok in ["Ackbar's picks are locked until kickoff.", "He took Boise State -13.5 and missed.",
               "I have picked Boise and I'm sweating."]:
        assert gc.status_guard("", ok) == ok
    assert gc._STATUS_ASK.search("has Blinkstradamus made his picks yet?")
    assert gc._STATUS_ASK.search("who hasn't picked yet")
    assert not gc._STATUS_ASK.search("who's winning?")


# ---- privacy: the group context never holds an unlocked pick, not even the sender's ----

NOW = dt.datetime(2026, 11, 13, 12, 0, 0)
NOT_STARTED = (NOW + dt.timedelta(days=2)).isoformat(timespec="seconds")


async def _group_ctx(sender_team: str):
    teams = [
        {"team_id": 10, "team_name": "Ackbar", "user_id": "uuid-ackbar", "active": True},
        {"team_id": 20, "team_name": "Rival", "user_id": "uuid-rival", "active": True},
    ]
    week_games = [{"GameId": 2, "Away": "Ohio State", "Home": "Penn State", "line": -6.5, "Status": "scheduled",
                   "winner": None, "week": 13, "tiebreaker": False, "Start (CT)": NOT_STARTED}]

    async def fake_rest(client, path, jwt):
        if path.startswith("teams?"):
            return teams
        if path.startswith("slots_jackpot"):
            return [{"pulls_since_pop": 1, "last_pop_at": None, "last_pop_team_id": None}]
        if "order=cfb_season.desc" in path:
            return [{"cfb_season": 2026}]
        if "Status=neq.final" in path:
            return [{"week": 13, "Status": "scheduled"}]
        if "week=eq.13" in path and "GameId" in path:
            return week_games
        if "picks?select=team_id,game_id,pick,by_slots,jackpot" in path:
            return [{"team_id": 10, "game_id": 2, "pick": "Penn State", "by_slots": False, "jackpot": False},
                    {"team_id": 20, "game_id": 2, "pick": "Ohio State", "by_slots": False, "jackpot": False}]
        if "Status=eq.final" in path:
            return []
        raise AssertionError(f"unexpected fetch: {path}")

    gc.VIEWER.uid = "uuid-picky"  # Picky the player: no visible league team
    tok = gc.SENDER.set(gc.Sender(handle="+15555550100", team=sender_team, team_id=10, user_id="uuid-ackbar"))
    try:
        with patch.object(chat, "_rest_get", side_effect=fake_rest), \
             patch.object(chat, "_now_ct_naive", return_value=NOW):
            ctx = await chat._build_context(None, "x", "uuid-ackbar")
            yb = chat.you_block(ctx)
            tail = chat._system_tail(ctx)
    finally:
        gc.SENDER.reset(tok)
    return ctx, yb, tail


def test_group_context_hides_unlocked_picks_even_the_senders():
    ctx, yb, tail = asyncio.run(_group_ctx("Ackbar"))
    assert ctx["you"] == {"team_id": 10, "team_name": "Ackbar"}  # persona keyed on the sender
    assert ctx["revealed_picks_this_week"] == {}, ctx["revealed_picks_this_week"]
    assert "pick: Penn State" not in yb and "pick: Ohio State" not in yb
    assert "hidden until kickoff" in yb
    assert "made picks this week" not in yb
    assert "GROUP TEXT" in tail and "1 to 3 short sentences" in tail


def test_answer_rerolls_when_filter_cuts_a_sentence():
    # attempt 1: the filter drops the opener, leaving an orphan; attempt 2 is whole
    tries = iter([["BAD opener that answered.", "That's not luck."], ["You're in first.", "Ackbar is 2nd."]])

    class Resp:
        def __init__(self, text):
            self.text = text

        @property
        async def body_iterator(self):
            yield 'data: {"content": "%s"}\n\n' % self.text

    async def fake_chat(_req):  # stands in for chat.py's _filtered, which calls chat.sentence_ok
        return Resp(" ".join(x for x in next(tries) if chat.sentence_ok(x, set())))

    with patch.object(gc, "_orig_sentence_ok", lambda x, *a, **k: not x.startswith("BAD")), \
            patch.object(chat, "chat", fake_chat), \
            patch.object(gc.VIEWER, "session", lambda c: asyncio.sleep(0)), \
            patch.object(gc.VIEWER, "sender", lambda h: gc.Sender(handle=h)):
        out = asyncio.run(gc.answer("+15555550199", "@Picky who's winning"))
        assert out["reply"] == "You're in first. Ackbar is 2nd." and out["mode"] == "group"


# ---- DMs: a mapped member gets the site as is; others public or nothing ----


def test_dm_context_is_the_site_for_a_mapped_member():
    tok = gc.MODE.set("dm")
    try:
        ctx, yb, tail = asyncio.run(_dm_ctx())
    finally:
        gc.MODE.reset(tok)
    assert ctx["you"]["team_name"] == "Ackbar"
    assert "Penn State" in yb, yb  # their own unlocked pick, as on the site
    assert "GROUP TEXT" not in tail and "TEXT MESSAGE" in tail


async def _dm_ctx():
    # same fixture as the group, but the hooks step aside: _build_context runs as the member
    return await _group_ctx("Ackbar")


def test_dm_hooks_defer_to_chat_py():
    tok = gc.MODE.set("dm")
    try:
        assert chat.proactive_note is not gc._orig_proactive_note
        with patch.object(gc, "_orig_proactive_note", lambda *a, **k: "HOOK"), \
                patch.object(gc, "_orig_self_intrusion", lambda *a, **k: True):
            assert chat.proactive_note({}, [], "q") == "HOOK"
            assert chat.self_intrusion_off_topic({}, "q", False, None, "") is True
    finally:
        gc.MODE.reset(tok)
    with patch.object(gc, "_orig_proactive_note", lambda *a, **k: "HOOK"), \
            patch.object(gc, "_orig_self_intrusion", lambda *a, **k: True):
        assert chat.proactive_note({}, [], "q") == ""                       # group: off
        assert chat.self_intrusion_off_topic({}, "q", False, None, "") is False


def _fake_answer_env(sender, seen):
    class Resp:
        @property
        async def body_iterator(self):
            yield 'data: {"content": "Sure."}\n\n'

    async def fake_chat(req):
        seen.append(json.loads((await req.body()).decode()))
        return Resp()

    async def member_session(c, uid):
        return "jwt-" + uid

    gc.VIEWER.jwt = "jwt-picky"
    return [patch.object(chat, "chat", fake_chat),
            patch.object(gc.VIEWER, "session", lambda c: asyncio.sleep(0)),
            patch.object(gc.VIEWER, "member_session", member_session),
            patch.object(gc.VIEWER, "sender", lambda h: sender)]


def test_dm_routing_and_history():
    import contextlib
    import tempfile
    mapped = gc.Sender(handle="+15555550100", team="Chuck P", team_id=1, user_id="uuid-chuck")
    unmapped = gc.Sender(handle="+15555550111")
    with tempfile.TemporaryDirectory() as d, patch.object(gc, "HISTORY_FILE", Path(d) / "h.json"):
        seen = []
        with contextlib.ExitStack() as st:
            for pt in _fake_answer_env(mapped, seen):
                st.enter_context(pt)
            out = asyncio.run(gc.answer("+15555550100", "how about that SMU game?", chat_id="any;-;x", dm=True))
            assert out["mode"] == "dm" and seen[-1]["supabase_jwt"] == "jwt-uuid-chuck"
            asyncio.run(gc.answer("+15555550100", "what about him?", chat_id="any;-;x", dm=True))
            assert [m["content"] for m in seen[-1]["messages"]] == ["how about that SMU game?", "Sure.", "what about him?"]
            assert oct((Path(d) / "h.json").stat().st_mode & 0o777) == "0o600"
            # a group message never sees the DM's history
            asyncio.run(gc.answer("+15555550100", "@Picky hi", chat_id="iMessage;+;chat1"))
            assert seen[-1]["messages"] == [{"role": "user", "content": "hi"}]
            assert seen[-1]["supabase_jwt"] == "jwt-picky"
        with contextlib.ExitStack() as st:
            for pt in _fake_answer_env(unmapped, seen):
                st.enter_context(pt)
            assert asyncio.run(gc.answer("+15555550111", "yo", dm=True)) is None           # not allowed
            out = asyncio.run(gc.answer("+15555550111", "yo", dm=True, allowlisted=True))
            assert out["mode"] == "dm-public" and seen[-1]["supabase_jwt"] == "jwt-picky"
        # expiry: turns older than HISTORY_TTL drop out
        assert gc.load_history("dm:+15555550100", now=time.time() + gc.HISTORY_TTL + 1) == []


def test_dm_trim():
    assert gc.dm_trim("## Take\n**Bold** call.") == "Take\nBold call."
    assert len(gc.dm_trim("Word. " * 600)) <= gc.DM_MAX_CHARS


if __name__ == "__main__":
    tests = [v for k, v in dict(globals()).items() if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  ok  {t.__name__}")
        except AssertionError as e:
            print(f"FAIL  {t.__name__}: {e}")
            failed += 1
    print("\nALL GREEN" if not failed else f"\n{failed} FAILED")
    sys.exit(1 if failed else 0)
