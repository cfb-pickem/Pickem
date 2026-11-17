"""
Speed lane (2026-11-16): what gemma reads on every text. Model-free.

gemma4 is sliding-window, so llama.cpp's cache checkpoint sits inside the
shared prefix and every token after it is re-read on every model call (~120
tok/s on the 16 GB mini). This builds the exact messages text Picky sends for
each kind of ask, in the group and in a DM, from a real (anonymized) week-12
snapshot with every Ollama call stubbed, and checks the part after the prefix
stays under a budget while the rules and data each ask needs are still there.

Run:  ./.venv/bin/python3 -I test_speed.py
"""

from __future__ import annotations

import asyncio
import copy
import datetime as dt
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
_TMP = Path(tempfile.mkdtemp(prefix="picky-speed-test-"))
for _k in ("PICKY_LIVE_CACHE", "PICKY_NEWS_CACHE", "PICKY_HOT_TAKES", "PICKY_INSIDER", "PICKY_LEAGUE_MATERIAL",
           "PICKY_BANTER_BANK", "PICKY_BURN_BOOK", "PICKY_GROUP_MEMBERS", "PICKY_TASTE_NOTES",
           "PICKY_LEAGUE_CHAT_LINES", "PICKY_IMESSAGE_HISTORY", "PICKY_BITS_STATE", "PICKY_EXCHANGE_LOG",
           "PICKY_NO_REPEAT_STATE", "PICKY_CALLBACKS_STATE"):
    os.environ[_k] = str(_TMP / _k.lower())  # never the live caches or the private files
os.environ["PICKY_GAME_CARD_DIR"] = os.environ["PICKY_DOSSIER_DIR"] = str(_TMP)
os.environ["PICKY_GROUP_PORT"] = "8799"
for _k in ("PICKY_BRAIN_TRACE", "PICKY_JUDGE", "PICKY_FEWSHOT", "PICKY_BRAIN", "PICKY_TEXT_NUM_PREDICT"):
    os.environ.pop(_k, None)
sys.path.insert(0, str(HERE))

import chat  # noqa: E402
import game_cards  # noqa: E402
import group_chat as gc  # noqa: E402
import live_games  # noqa: E402
import picky_brain as pb  # noqa: E402

# a fresh, empty news cache: the news rider rides ('nothing in your feed') without the live feed
Path(os.environ["PICKY_NEWS_CACHE"]).write_text(json.dumps(
    {"fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "items": {}}))

FIX = json.loads((HERE / "eval" / "fixtures" / "speed-ctx-wk12.json").read_text())
NOW = dt.datetime.fromisoformat(FIX["captured_ct"])


def est(s: str) -> int:
    """Token estimate (chars/4; gemma's real count runs ~1.3x this on these prompts)."""
    return round(len(s) / 4)


def fixture_ctx(dm: bool) -> dict:
    ctx = copy.deepcopy(FIX["ctx"])
    ctx["weekly_records"] = {t: {int(w): r for w, r in v.items()} for t, v in ctx["weekly_records"].items()}
    ctx["espn"] = {int(k): v for k, v in (ctx.get("espn") or {}).items()}
    ctx["live_at"] = dt.datetime.fromisoformat(ctx["live_at"]["__dt__"]) if ctx.get("live_at") else None
    if dm:  # a member's own view: his picks (made up here) are visible to him
        ctx["you"] = {"team_id": 6, "team_name": "Mack"}
        ctx["revealed_picks_this_week"] = {"Mack": [{"game_id": g["id"], "pick": g["home"], "by_slots": False,
                                                    "jackpot": False} for g in ctx["games_this_week"]]}
        ctx["you_have_picks_this_week"] = True
    return ctx


GAME = next(g for g in FIX["ctx"]["games_this_week"] if g["away"] == "Iowa")
CARD = ("GAME CARD, Iowa at Illinois (built offline Mon 6:44 PM; every number is computed, copy them exactly; graded "
        "ATS: the pick is who COVERS the league line):\n  League line: Iowa -8.5. Market: Iowa -8.5.\n"
        "  THE PLAY: I'd take Illinois +8.5 (a slight lean at best).\n  Why: SP+ projects Iowa by 8.\n" * 3)
DOSSIER = {"generated_at": "2026-11-16T17:00:00-06:00", "summary": "Iowa QB Hank Brown is expected to start.",
           "facts": [{"kind": "injury" if i < 2 else "news", "status": "probable", "date": "Nov 15",
                      "source": "Outlet", "text": f"Report number {i} about the Iowa at Illinois game, with some "
                      "length to it like the real ones carry, a sentence or two of public reporting."}
                     for i in range(8)],
           "opinions": [{"date": "Nov 15", "source": "Blog", "text": "A columnist's take on Illinois."}] * 2,
           "rumors": [{"date": "Nov 15", "source": "Board", "text": "A rumor about Iowa."}]}

CALLS: list[dict] = []


async def _json_call(prefix, tail, user, schema, temperature, num_predict, timeout):
    p = (schema or {}).get("properties") or {}
    kind = "understand" if "asking" in p else "banter" if "pick" in p else "fact" if "line" in p else "json"
    CALLS.append({"kind": kind, "prefix": prefix,
                  "messages": [{"role": "system", "content": prefix + tail}, {"role": "user", "content": user}]})
    return {"understand": json.dumps({"asking": "their question", "intent": "question", "about": "the sender",
                                      "tone": "curious", "facts_needed": [], "answer": "Illinois +8.5."}),
            "banter": json.dumps({"pick": 0, "tie": "", "own": "Bold words from a guy who took Baylor."}),
            "fact": json.dumps({"line": "Write that down."})}.get(kind, "{}")


REPLY = "Illinois getting 8.5 at home is the cleanest look on the board this week."


async def _stream(messages, prefix=None):
    CALLS.append({"kind": "draft", "prefix": prefix, "messages": messages})
    yield chat._sse({"content": REPLY})
    yield "data: [DONE]\n\n"


async def _noop(*a, **k):
    return None


def run(asks: list[tuple[str, str]], dm: bool) -> dict[str, list[dict]]:
    """{ask id: [model calls]} for one conversation (history builds turn by turn, as live)."""
    sender = gc.Sender(handle="+15555550100", team="Mack", team_id=6, user_id="uuid-mack", name="Mack")

    async def build_context(*a, **k):  # group: teamless (group_chat sets the sender); DM: his own view
        return fixture_ctx(dm)

    async def member_session(c, uid):
        return "jwt-" + uid

    async def notes(ids):
        return fixture_ctx(dm)["espn"]

    async def player(*a):
        return fixture_ctx(dm)["picky_player"]

    def card_note(ctx, question):
        return CARD if "iowa" in (question or "").lower() else ""

    out = {}
    with tempfile.TemporaryDirectory() as d, \
            patch.object(gc, "HISTORY_FILE", Path(d) / "h.json"), \
            patch.object(gc, "_orig_build_context", build_context), \
            patch.object(gc, "_orig_verify_jwt", lambda c, j: asyncio.sleep(0, "uuid-mack")), \
            patch.object(gc.VIEWER, "session", lambda c: asyncio.sleep(0)), \
            patch.object(gc.VIEWER, "member_session", member_session), \
            patch.object(gc.VIEWER, "sender", lambda h: sender), \
            patch.object(chat, "picky_player_facts", player), \
            patch.object(chat, "load_live", _noop), \
            patch.object(chat.espn, "game_notes", notes), \
            patch.object(chat, "_cfbd_index", lambda: None), \
            patch.object(chat, "load_lore", lambda: None), \
            patch.object(chat, "member_nicknames", lambda: {}), \
            patch.object(chat, "load_dossier", lambda w, gid: DOSSIER if gid == GAME["id"] else None), \
            patch.object(game_cards, "card_note", card_note), \
            patch.object(chat, "_now_ct_naive", lambda: NOW), \
            patch.object(chat, "_prime", _noop), \
            patch.object(pb, "_json_call", _json_call), \
            patch.object(pb, "_orig_ollama_stream", _stream):
        gc.VIEWER.jwt, gc.VIEWER.uid = "jwt-picky", "uuid-picky"
        for aid, text in asks:
            n = len(CALLS)
            asyncio.run(gc._answer(sender.handle, text if dm else "@Picky " + text,
                                   chat_id="any;-;+15555550100" if dm else "iMessage;+;chat-speed-test",
                                   dm=dm, allowlisted=False, recent=[]))
            out[aid] = CALLS[n:]
    return out


def after_prefix(calls: list[dict]) -> int:
    """What gemma re-reads past the cached prefix for one text: every call's messages minus the prefix.
    A guard regen (the stub's canned reply tripping a filter) isn't a live cost: first draft only."""
    seen, n = set(), 0
    for c in calls:
        if c["kind"] in seen:
            continue
        seen.add(c["kind"])
        n += est("".join(m["content"] for m in c["messages"])) - est(c["prefix"] or "")
    return n


ASKS = [  # (id, text): picks, a game read, news, rules, record, trash talk, standings, off-topic, then follow-ups
    ("picks", "who should I take this week?"),
    ("read", "break down Iowa at Illinois for me"),
    ("news", "any news on the LSU quarterback?"),
    ("rules", "what happens if a game is a push?"),
    ("record", "what's my record this season?"),
    ("trash", "you suck at this"),
    ("standings", "who's winning the league?"),
    ("offtopic", "best beer for a tailgate?"),
    ("read2", "what do you think of Texas Tech at Baylor?"),
    ("lastweek", "how did I do last week?"),
]
# after-prefix token budgets per text (chars/4). Main before the speed lane, on this snapshot (group/DM):
# picks 1585/1634, read 2086/2445, news 2441/2345, read2 3629/3682, lastweek 3889/3942 (every old turn
# re-rendered with its riders, live blocks for games 5 days out, full intel next to the card). 'lastweek'
# is a code-written fact-lane answer since the router's Monday last-week lane (e76c807).
BUDGET = {"picks": 1650, "read": 1600, "news": 1150, "rules": 250, "record": 200, "trash": 350,
          "standings": 200, "offtopic": 350, "read2": 1450, "lastweek": 300}

RESULTS: dict[bool, dict] = {}


def results(dm: bool) -> dict:
    if dm not in RESULTS:
        RESULTS[dm] = run(ASKS, dm)
    return RESULTS[dm]


def _check_budgets(dm: bool):
    res = results(dm)
    over = {a: after_prefix(c) for a, c in res.items() if after_prefix(c) > BUDGET[a]}
    assert not over, f"{'dm' if dm else 'group'} over budget: {over} (budget {BUDGET})"
    assert all(res[a] for a in BUDGET), [a for a in BUDGET if not res[a]]


def test_group_budgets():
    _check_budgets(False)


def test_dm_budgets():
    _check_budgets(True)


def _final(calls):
    return [c for c in calls if c["kind"] in ("draft", "fact", "banter")][0]


def test_required_rules_and_data_survive():
    for dm in (False, True):
        res = results(dm)
        for aid, calls in res.items():
            fin = _final(calls)
            sysm = fin["messages"][0]["content"]
            # Charlie's standing rules ride in the shared prefix of every reply
            assert "GRADED ONLY AGAINST THE SPREAD" in sysm, aid  # ATS framing
            assert "PRIVACY: other teams' picks stay hidden until they unlock" in sysm, aid
            assert "Worn-out phrases you never use" in sysm and "At most one insult per answer" in sysm, aid
            if fin["kind"] != "draft":  # fact / banter passes: their own short task, the answer is code-written
                assert "Never" in sysm[len(fin["prefix"]):] or "THE ANSWER" in fin["messages"][-1]["content"], aid
            elif dm:
                assert "TEXT MESSAGE: you're texting this member 1-on-1" in sysm, aid
            else:  # group: privacy of unlocked picks + group-text voice
                assert "Never reveal anyone's picks for games that haven't kicked off" in sysm, aid
                assert "Reply like a group text" in sysm, aid
            # every pool game's line is still in front of the model
            for g in FIX["ctx"]["games_this_week"]:
                assert f"Game " in sysm and g["away"] in sysm and g["home"] in sysm, (aid, g["away"])
        latest = lambda a: _final(res[a])["messages"][-1]["content"]  # noqa: E731
        assert "GAME CARD, Iowa at Illinois" in latest("read") and "GAME INTEL, Iowa at Illinois" in latest("read")
        assert "NEWS QUESTION" in latest("news")
        assert "push" in latest("rules").lower() and "THE ANSWER" in latest("rules")
        assert "THE ANSWER" in latest("record") and "Mack" in latest("record")
        assert "THE ANSWER" in latest("standings")
        assert "own" in json.dumps(res["trash"][0]["messages"]) or "NEWEST TEXT" in latest("trash")
        if dm:  # a member's own picks ride with a picks ask in his DM
            assert "Mack's picks this week" in latest("picks")


def test_intel_capped_when_a_card_rides():
    latest = _final(results(False)["read"])["messages"][-1]["content"]
    intel = latest[latest.index("GAME INTEL"):]
    assert intel.count("\n  - ") == 3, intel  # was summary + 6 facts + 2 opinions + a rumor
    assert "RUMOR" not in intel and "opinion" not in intel
    ctx = fixture_ctx(False)
    with patch.object(chat, "load_dossier", lambda w, gid: DOSSIER), patch.object(chat, "_now_ct_naive", lambda: NOW):
        full = chat.dossier_note(ctx, "any injuries for Iowa at Illinois?")
        assert full.count("\n  - ") == 9 and "RUMOR" in full  # no card, injury ask: everything


def test_old_turns_go_bare_and_follow_ups_keep_the_last():
    convo = [{"role": "system", "content": "S"},
             {"role": "user", "content": "break down Iowa at Illinois"}, {"role": "assistant", "content": "a1"},
             {"role": "user", "content": "how did I do last week?"}, {"role": "assistant", "content": "a2"},
             {"role": "user", "content": "how am I doing this season?"}]
    ctx = fixture_ctx(True)
    with patch.object(chat, "_now_ct_naive", lambda: NOW), patch.object(chat, "_cfbd_index", lambda: None), \
            patch.object(chat, "load_lore", lambda: None), patch.object(game_cards, "card_note", lambda c, q: ""):
        out, unsent = chat.history_turns(ctx, convo)
        assert [m["content"] for m in out[1:5]] == [convo[i]["content"] for i in range(1, 5)]
        assert out[5]["content"] != convo[5]["content"] and out[5]["content"].endswith(convo[5]["content"])
        assert unsent and all(u != convo[1]["content"] for u in unsent)  # the checks still see the old riders
        out, _ = chat.history_turns(ctx, convo[:-1] + [{"role": "user", "content": "why?"}])
        assert out[3]["content"] != convo[3]["content"] and "FOCUS:" in out[3]["content"]  # follow-up: kept
        assert out[1]["content"] == convo[1]["content"]
    # in the live pipeline: eight texts into a DM, the old turns are just the questions
    hist = _final(results(True)["read2"])["messages"][1:-1]
    assert len(hist) >= 6 and all(est(m["content"]) < 60 for m in hist), [est(m["content"]) for m in hist]


def test_live_blocks_wait_for_kickoff():
    ctx = fixture_ctx(False)
    with patch.object(chat, "_now_ct_naive", lambda: NOW):
        assert not chat.pool_started(ctx)
        assert not chat.wants_live_board(ctx, "what do you think of Texas Tech at Baylor?")
        assert not chat.wants_live_board(ctx, "who should I take this week?")
        assert chat.wants_live_board(ctx, "what's the score of Texas Tech at Baylor?")
        assert chat.wants_live_board(ctx, "any upsets?")
    with patch.object(chat, "_now_ct_naive", lambda: NOW + dt.timedelta(days=5)):  # Saturday night
        assert chat.pool_started(ctx) and chat.wants_live_board(ctx, "what do you think of Texas Tech at Baylor?")
    pre = [{"id": "1", "state": "pre", "away": {"name": "Texas Tech"}, "home": {"name": "Baylor"}}]
    with patch.object(live_games, "rider", lambda q, pool=None, refresh=True: ("LIVE GAME FEED x", pre)):
        for q, keep in [("what do you think of Texas Tech at Baylor?", False), ("any news on the Baylor QB?", False),
                        ("what's the score in Texas Tech Baylor?", True), ("is Texas Tech covering?", True),
                        ("has Baylor kicked off?", True), ("how are my picks doing?", True)]:
            c = {"games_this_week": []}
            live_games.apply_to_ctx(c, q, refresh=False)
            assert bool(c["live_rider"]) == keep, q
            assert c["live_rider_games"] == pre  # the claim checks still know the game hasn't started
    live = [{**pre[0], "state": "in"}]
    with patch.object(live_games, "rider", lambda q, pool=None, refresh=True: ("LIVE GAME FEED x", live)):
        c = {"games_this_week": []}
        live_games.apply_to_ctx(c, "what do you think of Texas Tech at Baylor?", refresh=False)
        assert c["live_rider"]


def test_ollama_options_for_texts():
    assert gc.TEXT_NUM_PREDICT == chat.OLLAMA_OPTIONS["num_predict"] <= 400  # texts run ~70-120 tokens
    assert chat.OLLAMA_OPTIONS["num_ctx"] == 16384  # every gemma caller (dossier, picks) uses it: no reloads
    assert chat.OLLAMA_KEEP_ALIVE == os.environ.get("PICKY_OLLAMA_KEEP_ALIVE", "24h")


if __name__ == "__main__":
    failed = 0
    tests = [(k, v) for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for name, fn in tests:
        try:
            fn()
            print(f"ok   {name}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL {name}: {e!r}")
    for dm in (False, True):
        if dm in RESULTS:
            print(("dm   " if dm else "group") + " after-prefix tokens: "
                  + ", ".join(f"{a} {after_prefix(c)}" for a, c in RESULTS[dm].items()))
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
