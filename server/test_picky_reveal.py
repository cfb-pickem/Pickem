"""
Picky's own sides stay out of the league GROUP until every active member has submitted or the week
locks (Charlie, 2026-11-19: "Picky's picks shouldn't get posted till everyone else's is"). Covers the
League rule (exhibition excluded, fail closed), the preview / Saturday posts without his sides, and the
group chat: a pick ask gets a dodge, a reply that takes a side gets swapped, DMs are untouched.
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
    """submission_status() as the site returns it: every team, Picky (exhibition) and inactive teams included."""

    def __init__(self, rows, fail=False):
        self.rows, self.fail = rows, fail

    def rpc(self, fn, body):
        if self.fail:
            raise ValueError("db down")
        assert fn == "submission_status", fn
        return self.rows


G1 = {"GameId": 1, "Away": "Utah", "Home": "Iowa", "line": -3.5, "week": 12, "Start (CT)": "2026-11-21T11:00:00",
      "picked": True}
G2 = {"GameId": 2, "Away": "Illinois", "Home": "Northwestern", "line": 2.5, "week": 12,
      "Start (CT)": "2026-11-21T14:30:00", "picked": True}


def league(now: str, submitted: dict[int, bool], fail=False) -> gp.League:
    """A real League (its locked / submission / picky_revealed / picky_card) over fake data.
    team 99 = Picky (exhibition), team 7 = inactive: neither is in tid, so neither counts."""
    lg = gp.League.__new__(gp.League)
    lg.now, lg.season, lg.names, lg._picks = t(now), 2026, {"Chuck P": "Charlie"}, {}
    lg.tid = {1: "Chuck P", 2: "Ackbar", 3: "TG"}
    lg.picky_id = 99
    lg.games = [G1, G2]
    lg.db = FakeDB([{"team_id": k, "team_name": str(k), "submitted": v} for k, v in submitted.items()], fail=fail)
    lg.picky_picks = lambda: []  # picky_player(): nothing revealed by the RPC before lock
    return lg


ALL_IN = {1: True, 2: True, 3: True, 99: False, 7: False}   # every member in; Picky + inactive not
ONE_OUT = {1: True, 2: False, 3: True, 99: True, 7: True}   # Ackbar missing (Picky being in doesn't help)
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


def test_rule():
    assert pv.revealed(True, None) and pv.revealed(False, {"a": True, "b": True})
    assert not pv.revealed(False, {"a": True, "b": False}) and not pv.revealed(False, {}) \
        and not pv.revealed(False, None)


def test_league_before_and_after_everyone_submitted():
    assert not league(PRE, ONE_OUT).picky_revealed(12), "one member missing: hidden"
    assert league(PRE, ALL_IN).picky_revealed(12), "every active member in: revealed before lock"


def test_exhibition_and_inactive_excluded_from_everyone():
    lg = league(PRE, ALL_IN)
    assert set(lg.submission(12)) == {"Chuck P", "Ackbar", "TG"}, lg.submission(12)
    # Picky (99) and the inactive team (7) haven't submitted, yet it's revealed: they don't count
    assert lg.picky_revealed(12)
    # and Picky + the inactive team being in doesn't make up for a missing member
    assert not league(PRE, ONE_OUT).picky_revealed(12)


def test_lock_reveals_and_failure_hides():
    assert league(POST, ONE_OUT).picky_revealed(12), "after the first kickoff: revealed"
    assert not league(PRE, ALL_IN, fail=True).picky_revealed(12), "no submission data: hidden (fail closed)"


def test_preview_and_saturday_without_his_sides():
    with leans_available(), patch.object(gp, "stakes_facts", lambda lg: []), \
            patch.object(gp, "load_take", lambda w, gid: {"quotable": "Iowa's defense eats this offense alive."}):
        lg = league(PRE, ONE_OUT)
        assert gp.picky_card(lg, 12) == {}
        pre = gp.make_preview(lg, 12)
        body = "\n".join(pre)
        assert "I'm on" not in body and "lean" not in body.lower() and "My card" not in body, pre
        assert "Film room" not in body, "the film room's line takes a side: hidden too"
        assert "Iowa -3.5 vs Utah" in body and gp.HIDDEN_NOTE in body, pre
        lg = league("2026-11-21T09:35", ONE_OUT)
        assert gp.make_saturday(lg, 12) is None, "Saturday post waits for the reveal (no sides = no post)"
        sat = gp.make_saturday(league("2026-11-21T11:05", ONE_OUT), 12)
        assert sat and "Iowa -3.5" in sat[0], f"after lock the Saturday post shows his sides: {sat}"
        # everyone in: his sides show (pre-lock: a lean from his pick logic)
        lg = league(PRE, ALL_IN)
        pre = gp.make_preview(lg, 12)
        body = "\n".join(pre)
        assert "My card" in body and "Iowa -3.5 vs Utah" in body and gp.HIDDEN_NOTE not in body, pre
        # after lock: shown
        lg = league(POST, ONE_OUT)
        assert gp.picky_card(lg, 12), "after lock his card shows"


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
