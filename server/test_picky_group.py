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
       "hey (@picky)", "@Picky's take?", "what do you think @picky?",
       # iPhone forms of his handle (live 2026-11-15: TG's got no reply)
       "@pickempicky@gmail.com you're a pussy", "@pickempicky hi", "@PickemPicky", "pickempicky@gmail.com you there",
       "yo @ pickempicky, thoughts?"]
NO = ["picky who's winning", "Picky is dumb", "@Pickyy hi", "@pickyface", "email picky-bot@example.com",
      "x@picky", "so picky about picks", "@ pickyy", "@Pick y", "", "x.pickempicky@gmail.com", "@pickempickyy",
      "notpickempicky@gmail.com"]


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
    assert gc.strip_trigger("@pickempicky@gmail.com you're a pussy") == "you're a pussy"
    assert gc.strip_trigger("pickempicky@gmail.com what's up") == "what's up"


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
    assert gc.group_trim("A. B. C. D.") == "A. B. C."  # no opener allowance: "nice milk, freak." is gone
    assert len(gc.group_trim("word " * 300)) <= gc.MAX_CHARS + 3


def test_submission_status_answers_but_picks_stay_hidden():
    """Site audit #8 (Charlie OK'd 2026-11-15): made / not yet from the RPC,
    before lock; never the picks themselves."""
    ctx = {"season": 2026, "current_week": 13, "teams": ["Ackbar", "Rival", "TG"],
           "you": {"team_id": 10, "team_name": "Ackbar"}, "leader": None,
           "games_this_week": [{"id": 2, "away": "Ohio State", "home": "Penn State", "status": "scheduled",
                                "start": NOT_STARTED, "winner": None}],
           "submission": {"Ackbar": True, "Rival": False, "TG": True}}
    with patch.object(chat, "_now_ct_naive", return_value=NOW), patch.object(chat, "member_nicknames", lambda: {}):
        for q in ["has Rival made his picks yet?", "who hasn't picked yet", "did I submit?", "are TG's picks in?"]:
            assert chat.wants_submission(ctx, q), q
        for q in ["who's winning?", "who made the best picks last week?", "has Rival picked Penn State?"]:
            assert not chat.wants_submission(ctx, q), q
        assert chat.submission_answer(ctx, "has Rival made his picks yet?") == "Rival hasn't made picks for week 13 yet."
        assert chat.submission_answer(ctx, "did I submit?") == "You've made your week 13 picks."
        assert chat.submission_answer(ctx, "who hasn't picked yet") == "Still waiting on week 13 picks from Rival (2 of 3 in)."
        note = chat.submission_note(ctx, "who hasn't picked yet")
        assert "Not yet: Rival" in note and "Penn State" not in note and "Ohio State" not in note
        check, fallback = chat.submission_fact(ctx, "has Rival made his picks yet?")
        assert check("Nope, Rival hasn't turned them in.") and not check("Yep, he has made them.")
        ok = chat.pick_claim_check(ctx, "has Rival made his picks yet?")
        assert not ok("Rival took Ohio State, obviously.") and not ok("He went with Penn State.")
        assert not ok("Yep, he has made them.")  # contradicts the RPC
        assert ok("Rival hasn't made his picks yet.")
        assert not ok("He's sweating his chalk in Penn State again.")  # any pool team on a status question
        assert chat.pick_claim_check(ctx, "who's winning?")("Penn State is a 6.5-point favorite.")
    # once the week locks, the status rider stays on (the site's panel still shows it; post-lock
    # Picky said an unchecked "Nobody"), and picks follow the site's reveal rules instead
    with patch.object(chat, "_now_ct_naive", return_value=NOW + dt.timedelta(days=3)), \
            patch.object(chat, "member_nicknames", lambda: {}):
        assert chat.wants_submission(ctx, "who hasn't picked yet")
        note = chat.submission_note(ctx, "who hasn't picked yet")
        assert "Not yet: Rival" in note and "locked" in note
        check, _ = chat.submission_fact(ctx, "who hasn't picked yet")
        assert check("Rival never turned his in.") and not check("Nobody, everyone's in.")
        assert chat.pick_claim_check(ctx, "x") is None


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
            patch.object(gc, "BRAIN", False), patch.object(chat, "chat", fake_chat), \
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


# ---- live 2026-11-15 group failures (week 11) ----

def _wk11_ctx(me="Ackbar"):
    g = lambda i, a, h, ln, w: {"id": i, "away": a, "home": h, "line": ln, "winner": w, "status": "final",
                                 "tiebreaker": False}
    games = [g(1, "Wake Forest", "SMU", 1.5, "Wake Forest"), g(2, "Oregon State", "Boise State", -13.5, "Oregon State"),
             g(3, "North Texas", "UTSA", 2.5, "UTSA"), g(4, "North Dakota State", "Hawai'i", 6.5, "North Dakota State")]
    live = [{"GameId": 1, "AwayPts": 49, "HomePts": 7, "Status": "final"},
            {"GameId": 2, "AwayPts": 41, "HomePts": 46, "Status": "final"},
            {"GameId": 3, "AwayPts": 16, "HomePts": 35, "Status": "final"},
            {"GameId": 4, "AwayPts": 21, "HomePts": 13, "Status": "final"}]
    pk = lambda *xs: [{"game_id": i + 1, "pick": x} for i, x in enumerate(xs)]
    revealed = {"Ackbar": pk("Wake Forest", "Boise State", "UTSA", "North Dakota State"),
                "Blinkstradamus": pk("Wake Forest", "Oregon State", "North Texas", "North Dakota State"),
                "DaddyFreeman": pk("Wake Forest", "Boise State", "North Texas", "North Dakota State")}
    return {"current_week": 11, "games_this_week": games, "live_games": live, "revealed_picks_this_week": revealed,
            "weekly_records": {"Ackbar": {11: [3, 1]}, "Blinkstradamus": {11: [3, 1]}, "DaddyFreeman": {11: [2, 2]}},
            "standings_ytd": [{"team": "Ackbar", "w": 28, "l": 20, "rank": 1},
                              {"team": "Blinkstradamus", "w": 27, "l": 21, "rank": 2},
                              {"team": "DaddyFreeman", "w": 27, "l": 21, "rank": 2}],
            "you": {"team_id": 1, "team_name": me}, "leader": None, "teams": ["Ackbar", "Blinkstradamus", "DaddyFreeman"]}


def test_week_claims_and_margins_are_grounded():
    with patch.object(chat, "member_nicknames", lambda: {"Blinkstradamus": "Blinky"}):
        ok = chat.week_claim_check(_wk11_ctx("Blinkstradamus"), "what is the best cereal?")
        assert not ok("That's a trap to distract you from the fact that you missed three of four games this week.")
        assert ok("You missed one of four this week.") and ok("you hit three of four")
        assert not ok("you got two of four right")
        ok = chat.week_claim_check(_wk11_ctx("Ackbar"), "why don't you work better")
        assert not ok("You missed that game by five points.")  # Boise -13.5 won by 5: missed the cover by 8.5
        assert ok("Your Boise State pick missed the cover by 8.5.")
        assert ok("Boise State won by 5 but missed the cover by eight and a half.")
        assert not ok("Boise State won by 7.") and ok("It needed to win by 14 or more.")
        assert not ok("You took Boise State -13.5 and watched it lose by five.")  # Boise WON by 5
        assert not ok("You took Boise State -13.5 and watched it get blown out by five points.")
        assert ok("Oregon State lost by five but covered.") and not ok("Oregon State won by five.")
        assert ok("You're stuck at 3-1.") and not ok("You're stuck at 2-2.")
        assert ok("Ackbar is tied with Blinky this week.") and not ok("Ackbar is tied with DaddyFreeman.")
        assert ok("Blinky is 3-1.") and not ok("Blinky went 2-2 this week.")
        assert ok("Blinky's in second place.") and not ok("Blinky's in fifth place.")


def test_grade_pick_states_the_cover_margin():
    r = chat.grade_pick("Boise State", "Oregon State", "Boise State", -13.5, "Oregon State", "final", 41, 46)
    assert r["result"] == "LOSS" and "missed the cover by 8.5" in r["why"]
    r = chat.grade_pick("UTSA", "North Texas", "UTSA", 2.5, "UTSA", "final", 16, 35)
    assert r["result"] == "WIN" and "covered by 21.5" in r["why"]


def test_games_question_is_about_the_games():
    ctx = _wk11_ctx("DaddyFreeman")
    q = "what did you think of this weekend's games?"
    note = chat.games_recap_note(ctx, q)
    assert "UPSET: UTSA was the underdog and won outright" in note
    assert "Boise State won by 5; Oregon State covered by 8.5" in note
    assert "Best week: Ackbar, Blinkstradamus at 3-1" in note
    assert not chat.games_recap_note(ctx, "how did my picks do this weekend?")
    f = chat.results_facts(ctx, q)
    assert not f or all(s_["kind"] != "you" for s_ in f["subjects"])


def test_casual_and_self_knock_riders():
    ctx = _wk11_ctx("Blinkstradamus")
    with patch.object(chat, "_cfbd_index", lambda: None), patch.object(chat.rare_bits, "available", lambda b: True):
        g = chat.guard_note(ctx, "what is the best cereal?")
    assert g.startswith("OFF-TOPIC") and "Answer it for real" in g and "milk" in g and "Roast them" not in g
    with patch.object(chat, "_cfbd_index", lambda: None), patch.object(chat.rare_bits, "available", lambda b: False):
        g = chat.guard_note(ctx, "what is the best cereal?")
    assert g.startswith("OFF-TOPIC") and "milk" not in g  # budget spent: no tie-in
    assert chat.self_knock_note(_wk11_ctx("Ackbar"), "why don\u2019t you work better".replace("\u2019", "'"))
    assert not chat.self_knock_note(ctx, "who covered?")
    assert chat._HOSTILE.search("you're a pussy") and chat._HOSTILE.search("you're a useless piece of junk")


def test_recent_group_context_and_per_sender_history():
    m = {"+15555550100": {"team": "DaddyFreeman", "name": "DaddyFreeman"}}
    with patch.object(gc, "load_members", lambda: m):
        b = gc.recent_block([{"handle": "+15555550100", "text": "All bark no bite, 2-2 lol", "ts": time.time()},
                             {"handle": "x", "text": "call me 555-0199 or a@b.com", "ts": time.time()},
                             {"handle": "", "text": "old", "ts": time.time() - 99999},
                             {"from_me": True, "text": "hi"}])
    assert "DaddyFreeman: All bark no bite, #-# lol" in b and "a member: call me ###-#### or [email]" in b
    assert ": old" not in b and "Picky (you): hi" in b and "read-only" in b
    assert gc.recent_block(None) == "" and gc.recent_block([]) == ""
    import tempfile
    with tempfile.TemporaryDirectory() as d, patch.object(gc, "HISTORY_FILE", Path(d) / "h.json"):
        asyncio.run(gc.save_turn("group:g", "you're a pussy", "reply", "+1a"))
        asyncio.run(gc.save_turn("group:g", "who's winning", "reply2", "+1b"))
        assert [t["content"] for t in gc.load_history("group:g", sender="+1b")] == ["who's winning", "reply2"]
        assert len(gc.load_history("group:g")) == 4
    assert gc.drop_echo("You're a pussy? Cute. You're 19-29 and last place.", "you're a pussy") == \
        "You're 19-29 and last place."
    assert gc.drop_echo("Who's winning? Chuck is.", "who's winning") == "Who's winning? Chuck is."


def _with_picky(ctx: dict) -> dict:
    """Picky's real week 11: Boise State -13.5, North Texas -2.5, Hawai'i +6.5, all LOSS (0-3)."""
    games = {g["id"]: g for g in ctx["games_this_week"]}
    mine = [(2, "Boise State"), (3, "North Texas"), (4, "Hawai'i")]
    rows = [{"pick": p, "away": games[i]["away"], "home": games[i]["home"], "line": games[i]["line"],
             "winner": games[i]["winner"], "status": "final", "tiebreaker": False, "label": p,
             "game": f"{games[i]['away']} at {games[i]['home']}", "result": "missed", "why": None, "blurb": None}
            for i, p in mine]
    for g, lg in zip(ctx["games_this_week"], ctx["live_games"]):
        lg.update({"Away": g["away"], "Home": g["home"]})
    ctx["picky_player"] = {"name": "Picky", "season": [0, 3], "last_week": [0, 0], "this_week": rows, "hidden_count": 0,
                           "revealed": [{**{k: r[k] for k in ("pick", "away", "home", "line", "winner", "status",
                                                                  "tiebreaker")}, "week": 11} for r in rows]}
    return ctx


LIVE_FAIL = ("You suck at picks? Look in the mirror, Chuck P. You're 4-0 this week, but you're still last in the league "
             "at being a Notre Dame homer. You're 1st place, but I'm the only one who knows how to pick a losing team "
             "like Oregon State. Congrats on the perfect week, champ.")


def test_picky_own_pick_claims_match_his_picks():
    ok = chat.picky_claim_check(_with_picky(_wk11_ctx("Ackbar")))
    # the live 2026-11-15 reply: he took Boise State -13.5 (won by 5, no cover), never Oregon State
    assert not ok("You're 1st place, but I'm the only one who knows how to pick a losing team like Oregon State.")
    for bad in ("I picked Oregon State.", "I took UTSA.", "I took Wake Forest.", "I faded Boise State.",
                "I didn't take Boise State.", "My Boise State pick cashed.", "I'm 1-2 on the season.",
                "I went 2-1 this week.", "I'm undefeated, baby.", "I'm 3-0 this week."):
        assert not ok(bad), bad
    for good in ("I took Boise State -13.5 and it won by 5 but didn't cover.", "I took Boise State over Oregon State.",
                 "My pick was Boise State.", "I rode Hawai'i and they got smoked.", "I faded Oregon State.",
                 "I didn't take Oregon State.", "I'd have taken Oregon State.", "I think you took Oregon State.",
                 "I'm 0-3, but Ackbar took Wake Forest.", "I'm 0-3 this week.", "I'm 0-3 on the season.",
                 "I'm not perfect.", "I'm winless.", "I'm taking Ohio State next week.",
                 "I took Boise, you took Oregon State.", "You took Oregon State and it covered.",
                 "Congrats on the perfect week, champ."):
        assert ok(good), good
    assert chat.picky_claim_check({"games_this_week": []}) is None  # Picky not loaded: nothing to hold him to


def test_fake_league_superlatives_are_dropped():
    assert chat.fake_superlative("You're 4-0 this week, but you're still last in the league at being a Notre Dame homer.")
    assert chat.fake_superlative("You're the best in the league at whining.")
    for ok in ("You're last in the league at picking dogs.", "He's last in the league in wins.",
               "You're dead last in the standings.", "Best in the league at covering this year."):
        assert not chat.fake_superlative(ok), ok
    assert not chat.sentence_ok("You're first in the league at being a homer.", set())


def test_live_failure_reply_loses_the_false_sentences():
    ctx = _with_picky(_wk11_ctx("Ackbar"))
    ok = chat.picky_claim_check(ctx)
    kept = [s for s in chat._sentences(LIVE_FAIL) if chat.sentence_ok(s, set()) and ok(s)]
    assert "Oregon State" not in " ".join(kept) and "last in the league" not in " ".join(kept), kept


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
