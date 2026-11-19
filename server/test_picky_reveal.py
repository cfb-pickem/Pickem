"""
Picky's own sides stay out of the league GROUP until the picks LOCK, the site's lock rule (Charlie,
2026-11-19 15:41 CT: "picks are hidden till kickoff when they lock, not anything to do with all members").
Regular season: the whole week locks at its first kickoff; playoffs lock per game. Covers the League rule
(no submission dependence, fail closed), the preview / Saturday posts without his sides, and the group chat:
a pick ask gets a dodge, a reply that takes a side gets swapped, DMs are untouched.
No network, no model, nothing sent.

Run:  ./.venv/bin/python3 -I test_picky_reveal.py
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
_TMP = tempfile.mkdtemp(prefix="picky-reveal-test-")
os.environ["PICKY_PRIVATE_DIR"] = _TMP
import group_posts as gp  # noqa: E402
import picky_reveal as pv  # noqa: E402

CT = gp.CT


def t(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s).replace(tzinfo=CT)


class FakeDB:
    """No RPC may be consulted for the reveal: who has submitted plays no part (it raises if asked)."""

    def rpc(self, fn, body):
        raise AssertionError(f"the reveal read {fn}: it must depend on the lock only")


G1 = {"GameId": 1, "Away": "Utah", "Home": "Iowa", "line": -3.5, "week": 12, "Start (CT)": "2026-11-21T11:00:00",
      "picked": True}
G2 = {"GameId": 2, "Away": "Illinois", "Home": "Northwestern", "line": 2.5, "week": 12,
      "Start (CT)": "2026-11-21T14:30:00", "picked": True}
# league playoffs (week 16+): each game locks at its own kickoff
P1 = {"GameId": 11, "Away": "Utah", "Home": "Iowa", "line": -3.5, "week": 16, "Start (CT)": "2026-12-19T11:00:00",
      "picked": True}
P2 = {"GameId": 12, "Away": "Illinois", "Home": "Northwestern", "line": 2.5, "week": 16,
      "Start (CT)": "2026-12-19T18:30:00", "picked": True}


def league(now: str, games=(G1, G2)) -> gp.League:
    """A real League (its lock_time / game_locked / picky_revealed / picky_card) over fake data."""
    lg = gp.League.__new__(gp.League)
    lg.now, lg.season, lg.names, lg._picks = t(now), 2026, {"Chuck P": "Charlie"}, {}
    lg.tid = {1: "Chuck P", 2: "Ackbar", 3: "TG"}
    lg.picky_id = 99
    lg.games = [dict(g) for g in games]
    lg.db = FakeDB()
    lg.picky_picks = lambda: []  # picky_player(): nothing revealed by the RPC before lock
    return lg


PRE, POST = "2026-11-20T12:00", "2026-11-21T11:05"


def fake_decide(g, notes, idx):
    return {"pick": g["Home"], "reason": "value"}


@contextlib.contextmanager
def leans_available():
    """picky_card's pre-lock fallback (his pick logic) without ESPN or caches."""
    import espn
    import picky_picks

    async def notes(ids):
        return {}
    with patch.object(espn, "game_notes", notes), patch.object(picky_picks, "decide", fake_decide), \
            patch.object(picky_picks, "load_idx", lambda: {}):
        yield


def test_old_submission_reveal_is_gone():
    assert not hasattr(pv, "revealed"), "the everyone-submitted reveal path is removed"
    import inspect
    for src in (inspect.getsource(pv), inspect.getsource(gp.League.picky_revealed),
                inspect.getsource(gp.League.game_locked), inspect.getsource(gp.picky_card)):
        assert "submission" not in src.replace("Who has or hasn't submitted plays no part", ""), src[:200]


def test_hidden_before_lock_revealed_after():
    assert not league(PRE).picky_revealed(12), "before the first kickoff: hidden"
    assert not league("2026-11-21T10:59").picky_revealed(12), "a minute before kickoff: hidden"
    assert league("2026-11-21T11:00").picky_revealed(12), "at the first kickoff the week locks: revealed"
    assert league(POST).picky_revealed(12)


def test_regular_season_whole_week_locks_at_first_kickoff():
    lg = league(POST)  # G1 kicked off, G2 (2:30) hasn't
    assert lg.game_locked(G2, 12), "regular season: the later game locked with the week"
    assert lg.game_lock(G2, 12) == t("2026-11-21T11:00")


def test_no_submission_dependence():
    # FakeDB raises on any RPC, so these pass only if nobody asks who has submitted
    lg = league(PRE)
    lg.submission = lambda w: {"Chuck P": True, "Ackbar": True, "TG": True}  # everyone in: still hidden
    assert not lg.picky_revealed(12) and gp.picky_card(lg, 12) == {}
    lg = league(POST)
    lg.submission = lambda w: {"Chuck P": False, "Ackbar": False, "TG": False}  # nobody in: still revealed
    assert lg.picky_revealed(12)


def test_playoffs_lock_per_game():
    lg = league("2026-12-19T12:00", games=(P1, P2))  # P1 kicked off at 11, P2 not till 6:30
    assert lg.game_locked(P1, 16) and not lg.game_locked(P2, 16)
    assert not lg.picky_revealed(16), "one playoff game still open: not fully revealed"
    with leans_available():
        card = gp.picky_card(lg, 16)
    assert set(card) == {11}, f"only the locked playoff game shows his side: {card}"
    assert league("2026-12-19T18:30", games=(P1, P2)).picky_revealed(16)
    st = pv.group_status(league_factory=lambda: lg)
    assert st == {"revealed": False, "week": 16, "pool_teams": {"Illinois", "Northwestern"}}, st


def test_same_rule_as_picky_picks_lock_utc():
    import picky_picks
    for games, w, now in (((G1, G2), 12, POST), ((P1, P2), 16, "2026-12-19T12:00")):
        lg = league(now, games=games)
        for g in games:
            assert lg.game_lock(g, w) == picky_picks.lock_utc(g, list(games)).astimezone(CT), g["GameId"]


def test_unknown_lock_stays_hidden():
    g = dict(P2, **{"Start (CT)": None})
    lg = league("2026-12-25T12:00", games=(P1, g))
    assert not lg.game_locked(g, 16) and not lg.picky_revealed(16), "no kickoff time: hidden (fail closed)"
    lg = league(PRE, games=[dict(G1, **{"Start (CT)": None})])
    assert not lg.picky_revealed(12)
    assert league(PRE, games=[]).picky_revealed(12) is False

    def boom():
        raise RuntimeError("db down")
    assert pv.group_status(league_factory=boom)["revealed"] is False


def test_preview_and_saturday_without_his_sides():
    with leans_available(), patch.object(gp, "stakes_facts", lambda lg: []), \
            patch.object(gp, "load_take", lambda w, gid: {"quotable": "Iowa's defense eats this offense alive."}):
        lg = league(PRE)
        assert gp.picky_card(lg, 12) == {}
        pre = gp.make_preview(lg, 12)
        body = "\n".join(pre)
        assert "I'm on" not in body and "lean" not in body.lower() and "My card" not in body, pre
        assert "Film room" not in body, "the film room's line takes a side: hidden too"
        assert "Iowa -3.5 vs Utah" in body and gp.HIDDEN_NOTE in body and "kickoff" in gp.HIDDEN_NOTE, pre
        # Saturday post at its old time (09:30 window), before the 11am lock: games and lines, no sides
        sat = gp.make_saturday(league("2026-11-21T09:35"), 12)
        assert sat and len(sat) == 1 and gp.HIDDEN_NOTE in sat[0] and "my side" not in sat[0].replace(gp.HIDDEN_NOTE, "").lower() \
            and "Where I'm at" not in sat[0] and "lean" not in sat[0].lower(), sat
        assert "Iowa -3.5 vs Utah (Sat 11am)" in sat[0], sat  # the market favorite + line, not his side
        # after lock: his sides (made picks or, for an unentered game, his lean)
        sat = gp.make_saturday(league(POST), 12)
        assert sat and gp.HIDDEN_NOTE not in sat[0] and "Iowa -3.5" in sat[0] and "Northwestern" in sat[0], sat
        assert gp.picky_card(league(POST), 12), "after lock his card shows"


def test_saturday_post_timing_is_the_old_window():
    """No 'wait for the reveal' gate: the post goes at the first tick from 09:30 like before 354cc2f."""
    import inspect
    src = inspect.getsource(gp.tick)
    assert '(now.hour, now.minute) >= (9, 30) and now.hour < 13' in src
    assert "if not card" not in inspect.getsource(gp.make_saturday)


def test_dodge_says_kickoff():
    for d in pv.DODGES:
        assert "kickoff" in d and "everyone" not in d.lower() and "everybody" not in d.lower() \
            and "all of you" not in d, d
    assert pv.dodge("k:q") in pv.DODGES and pv.dodge("k:q") == pv.dodge("k:q")


def test_tick_refresh_gets_reveal_flag():
    """group_posts.tick passes the reveal state to the Thursday refresh (no 'lean flipped' before it)."""
    import inspect
    src = inspect.getsource(gp.tick)
    assert "refresh_texts(lg, ow, now, leans=lg.picky_revealed(ow))" in src


# -- group chat ---------------------------------------------------------------

POOL = {"Utah", "Iowa", "Illinois", "Northwestern"}


def test_pick_ask_detection():
    for q in ["who you got in Iowa Utah", "who do you like this week", "what's your pick in the Iowa game",
              "picky's lock of the week?", "who covers Illinois Northwestern", "who are you taking in Iowa",
              "gimme your best bet", "your leans?"]:
        assert pv.wants_pick(q, POOL), q
    for q in ["how'd your picks do last week", "what's your record", "is the Iowa QB playing",
              "who do you like in Ohio State Michigan", "what's the line on Iowa"]:
        assert not pv.wants_pick(q, POOL, ["Ohio State", "Michigan"] if "Ohio" in q else []), q


def test_reply_lean_detection():
    for r in ["Lean: Iowa -3.5", "I like Iowa there.", "I'd take the points with Utah.", "I'm on Northwestern.",
              "Give me Illinois, the dog is live."]:
        assert pv.says_lean(r, POOL), r
    for r in ["Iowa's run D is #2 in EPA allowed.", "The line is Iowa -3.5, market has 4.", "I have a question back"]:
        assert not pv.says_lean(r, POOL), r
    assert not pv.says_lean("I like Ohio State there.", POOL), "a non-pool game isn't gated"
    assert pv.says_lean("I like the dog there.", POOL, question="thoughts on Iowa?"), "team named in the ask"
    assert pv.says_lean("I like Iowa there.", set()), "no pool list: any lean is hidden (fail closed)"


def _env(sender, seen, reply_text):
    import chat
    import group_chat as gc

    class Resp:
        @property
        async def body_iterator(self):
            yield "data: " + json.dumps({"content": reply_text}) + "\n\n"

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


def _ask(text, dm, revealed, reply_text="Sure."):
    import group_chat as gc
    st = {"revealed": revealed, "week": 12, "pool_teams": set(POOL)}
    sender = gc.Sender(handle="+15555550100", team="Ackbar", team_id=2, user_id="uuid-ack")
    seen = []
    with tempfile.TemporaryDirectory() as d, patch.object(gc, "HISTORY_FILE", Path(d) / "h.json"), \
            patch.object(gc, "EXCHANGE_LOG", Path(d) / "x.jsonl"), \
            patch.object(pv, "group_status", lambda *a, **k: st), contextlib.ExitStack() as es:
        for pt in _env(sender, seen, reply_text):
            es.enter_context(pt)
        out = asyncio.run(gc.answer("+15555550100", text, chat_id="any;-;x" if dm else "iMessage;+;chat1", dm=dm))
    return out, seen


def test_group_pick_ask_dodged_until_reveal():
    out, seen = _ask("@Picky who you got in Iowa Utah?", dm=False, revealed=False)
    assert out["lane"] == "pick_hidden" and out["reply"] in pv.DODGES and not seen, out
    out, seen = _ask("@Picky who you got in Iowa Utah?", dm=False, revealed=True)
    assert out["lane"] != "pick_hidden" and seen, out


def test_group_reply_taking_a_side_is_swapped():
    out, _ = _ask("@Picky thoughts on Iowa this week?", dm=False, revealed=False,
                  reply_text="I like Iowa -3.5 here, their run D is #2.")
    assert out["reply"] in pv.DODGES, out
    out, _ = _ask("@Picky thoughts on Iowa this week?", dm=False, revealed=True,
                  reply_text="I like Iowa -3.5 here, their run D is #2.")
    assert out["reply"] not in pv.DODGES, out


def test_dms_untouched():
    out, seen = _ask("who you got in Iowa Utah?", dm=True, revealed=False,
                     reply_text="I like Iowa -3.5 here, their run D is #2.")
    assert out["mode"] == "dm" and out["lane"] != "pick_hidden" and seen and out["reply"] not in pv.DODGES, out


def test_past_picks_not_gated():
    import group_chat as gc
    route = type("R", (), {"kind": "picky_record"})()
    assert asyncio.run(gc.hidden_sides("group", route)) is None
    assert asyncio.run(gc.hidden_sides("dm", type("R", (), {"kind": ""})())) is None


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
            import traceback
            traceback.print_exc()
            print(f"ERR   {fn.__name__}: {type(e).__name__}: {e}")
            failed += 1
    print("\nALL GREEN" if not failed else f"\n{failed} FAILED")
    sys.exit(1 if failed else 0)
