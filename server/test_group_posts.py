"""
Picky's proactive group posts: reminder timing, the validator, and the
outbox gates (kill switch, dry run, group detection, once-only, quiet hours).
No network, no LLM.

Run:  ./.venv/bin/python3 -I test_group_posts.py
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
_TMP = tempfile.mkdtemp(prefix="picky-posts-test-")
os.environ["PICKY_PRIVATE_DIR"] = _TMP
import group_outbox as ob  # noqa: E402
import group_posts as gp  # noqa: E402

CT = gp.CT
NAMES = {"Chuck P": "Charlie", "Blinkstradamus": "Blinky", "Ackbar": "Ackbar", "Liam Bryson": "Liam",
         "Andrew Bub": "Bub", "TG": "TG"}
MEMBERS = {"members": [
    {"team": "Chuck P", "name": "Charlie", "handles": ["+12195550100"]},
    {"team": "Ackbar", "name": "Ackbar", "handles": ["(504) 555-0101"]},
    {"team": "Blinkstradamus", "name": "Blinky", "handles": ["+17165550102"]},
    {"team": "Liam Bryson", "name": "Liam", "handles": ["+18165550103"]},
    {"team": "TG", "name": "TG", "handles": ["+14105550104"]},
    {"team": "Andrew Bub", "name": "Bub", "handles": ["+13145550105"]},
]}


def t(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s).replace(tzinfo=CT)


def fresh():
    for p in Path(_TMP).glob("**/*"):
        if p.is_file():
            p.unlink()
    (Path(_TMP) / "picky_group_members.json").write_text(json.dumps(MEMBERS))


# -- reminder timing -----------------------------------------------------------

def test_saturday_first_game_one_reminder():
    s = gp.reminder_slots(t("2026-11-21T11:00"))  # Saturday 11 AM
    assert s == [("sat", t("2026-11-21T07:00"))], s


def test_friday_night_two_reminders():
    s = gp.reminder_slots(t("2026-11-20T19:00"))  # Friday 7 PM
    assert s == [("first", t("2026-11-20T07:00")), ("second", t("2026-11-20T17:00"))], s


def test_early_kickoff_moves_first_to_evening_before():
    s = gp.reminder_slots(t("2026-11-20T09:00"))  # Friday 9 AM: 2h before is 7 AM
    assert s == [("first", t("2026-11-19T19:00")), ("second", t("2026-11-20T07:00"))], s
    s = gp.reminder_slots(t("2026-11-21T07:15"))  # Saturday before 7:30: evening before
    assert s == [("sat", t("2026-11-20T19:00"))], s


def test_no_reminder_after_lock():
    lock = t("2026-11-20T19:00")
    assert gp.due_slot(lock, t("2026-11-20T07:03")) == "first"
    assert gp.due_slot(lock, t("2026-11-20T17:10")) == "second"
    assert gp.due_slot(lock, t("2026-11-20T12:00")) is None
    assert gp.due_slot(lock, t("2026-11-20T19:01")) is None
    # second slot's grace window never runs past the lock
    assert gp.due_slot(t("2026-11-20T09:00"), t("2026-11-20T08:59")) is None or \
        gp.due_slot(t("2026-11-20T09:00"), t("2026-11-20T08:59")) == "second"
    assert gp.due_slot(t("2026-11-20T09:00"), t("2026-11-20T09:00")) is None


# -- validator -----------------------------------------------------------------

FACTS = ["Week 11 records: Charlie 4-0; Ackbar 3-1; Blinky 2-2.",
         "WORST PICK: Boise State -13.5 missed the line by 8.5 points; taken by Ackbar."]


def test_validator_numbers_and_names():
    ok = ["Charlie 4-0, as foretold. Ackbar's Boise State -13.5 missed by 8.5. Chalk merchant."]
    assert gp.validate(ok, FACTS, NAMES, []) == []
    bad = gp.validate(["Ackbar went 2-2 and is 0-7 lifetime"], FACTS, NAMES, [])
    assert any("numbers not in FACTS" in p and "7" in p for p in bad), bad
    bad = gp.validate(["Liam had a week too"], FACTS, NAMES, [])
    assert any("names members not in FACTS" in p for p in bad), bad


def test_validator_canon_and_voice():
    bad = gp.validate(["Blinky spilled the milk again"], FACTS, NAMES, [])
    assert any("milk canon" in p for p in bad), bad
    bad = gp.validate(["As an exhibition entry I went 0-3"], FACTS + ["you went 0-3"], NAMES, [])
    assert any("bot/AI/exhibition" in p for p in bad), bad
    bad = gp.validate(["Alright folks, week 11 is in"], FACTS, NAMES, [])
    assert any("opens like" in p for p in bad), bad
    bad = gp.validate(["Charlie went 4-0 again"], FACTS, NAMES, ["Charlie went 4-0 last week too"])
    assert any("opens like" in p for p in bad), bad
    bad = gp.validate(["call me at 219-555-0100"], FACTS + ["219-555-0100"], NAMES, [])
    assert any("phone" in p for p in bad), bad


def test_validator_reminder_names_only_stragglers():
    facts = ["Week 12 picks lock Sat 11 AM CT.", "Still need to pick: Liam, TG."]
    allowed = {"Liam Bryson", "TG"}
    assert gp.validate(["Liam, TG: 11 AM lock. Move."], facts, NAMES, [], allowed_teams=allowed,
                       required_teams=allowed) == []
    bad = gp.validate(["Liam, get it done. Charlie already did."], facts, NAMES, [], allowed_teams=allowed,
                      required_teams=allowed)
    assert any("not in FACTS" in p for p in bad) and any("name every one" in p for p in bad), bad


def test_write_retries_then_gives_up():
    fresh()
    calls = []

    def writer(prompt):
        calls.append(prompt)
        return ["Ackbar went 9-0"] if len(calls) == 1 else ["Ackbar went 3-1, chalk merchant."]
    texts, probs = gp.write("task", FACTS, NAMES, writer=writer)
    assert texts == ["Ackbar went 3-1, chalk merchant."] and len(calls) == 2
    assert "REJECTED" in calls[1]
    texts, probs = gp.write("task", FACTS, NAMES, writer=lambda p: ["Ackbar went 9-0"])
    assert texts is None and probs


# -- outbox gates --------------------------------------------------------------

def test_pause_and_dry_run_never_queue():
    fresh()
    ob.PAUSE_FILE.write_text("x")
    ob.save_config(dry_run=False, group_guid="any;+;chat1")
    assert ob.post("recap", ["hi"], "k1", echo=lambda *_: None) == "paused"
    assert not list(ob.OUTBOX.glob("*.json")) if ob.OUTBOX.exists() else True
    ob.PAUSE_FILE.unlink()
    ob.save_config(dry_run=True)
    assert ob.post("recap", ["hi"], "k2", echo=lambda *_: None) == "dry-run"
    assert not ob.OUTBOX.exists() or not list(ob.OUTBOX.glob("*.json"))
    assert ob.next_item() is None
    log = [json.loads(x) for x in ob.SEND_LOG.read_text().splitlines()]
    assert [r["event"] for r in log] == ["paused", "dry-run"], log


def test_no_group_no_post_then_arms():
    fresh()
    ob.save_config(dry_run=False, group_guid=None)
    assert ob.post("recap", ["hi"], "k3", echo=lambda *_: None) == "no-group"
    groups = [{"guid": "any;+;small", "handles": ["+12195550100", "+15045550101"]},
              {"guid": "any;+;league", "identifier": "chat9",
               "handles": [m["handles"][0] for m in MEMBERS["members"]]}]
    ob.save_config(min_member_handles=5)
    g = ob.update_group(groups)
    assert g and g["guid"] == "any;+;league" and ob.config()["group_guid"] == "any;+;league"
    assert ob.post("recap", ["hi", "there"], "k3", echo=lambda *_: None) == "queued"
    assert ob.post("recap", ["hi"], "k3", echo=lambda *_: None) == "dup"
    item = ob.next_item()
    assert item["texts"] == ["hi", "there"] and item["chat"] == "any;+;league"
    assert ob.next_item() is None  # claimed
    assert ob.ack(item["id"], 2, True)
    assert not list(ob.OUTBOX.glob("*.json"))


def test_group_needs_enough_members():
    fresh()
    ob.save_config(min_member_handles=6)
    assert ob.update_group([{"guid": "any;+;dm-ish", "handles": ["+12195550100", "+15045550101"]}]) is None
    assert ob.config()["group_guid"] is None


def test_pause_after_queue_stops_delivery():
    fresh()
    ob.save_config(dry_run=False, group_guid="any;+;league")
    assert ob.post("remind", ["lock soon"], "k4", echo=lambda *_: None) == "queued"
    ob.PAUSE_FILE.write_text("x")
    assert ob.next_item() is None
    ob.PAUSE_FILE.unlink()
    assert ob.next_item() is not None


def test_expired_items_drop():
    fresh()
    ob.save_config(dry_run=False, group_guid="any;+;league")
    ob.post("remind", ["lock soon"], "k5", expires_s=60, echo=lambda *_: None)
    import time
    assert ob.next_item(now=time.time() + 120) is None
    assert not list(ob.OUTBOX.glob("*.json"))


def test_quiet_hours_for_pings():
    fresh()
    ob.save_config(dry_run=False, group_guid="any;+;league")
    assert ob.in_quiet_hours(t("2026-11-21T23:45")) and ob.in_quiet_hours(t("2026-11-22T07:59"))
    assert not ob.in_quiet_hours(t("2026-11-21T08:00")) and not ob.in_quiet_hours(t("2026-11-21T23:29"))
    assert ob.post("ping", ["late final"], "p1", quiet=True, now=t("2026-11-21T23:50"),
                   echo=lambda *_: None) == "quiet"
    assert ob.post("ping", ["final"], "p2", quiet=True, now=t("2026-11-21T16:00"),
                   echo=lambda *_: None) == "queued"


def test_enable_clears_dry_run_keys():
    fresh()
    ob.save_config(dry_run=True)
    ob.post("recap", ["x"], "recap:2026:11", echo=lambda *_: None)
    assert ob.done("recap:2026:11")
    assert ob.clear_dry_run_keys() == 1 and not ob.done("recap:2026:11")


def test_labels_and_cover():
    g = {"Away": "Oregon State", "Home": "Boise State", "line": -13.5, "AwayPts": 24, "HomePts": 29}
    assert gp.side_label(g, "Boise State") == "Boise State -13.5"
    assert gp.side_label(g, "Oregon State") == "Oregon State +13.5"
    assert gp.favorite(g) == "Boise State"
    assert gp.covered_side(g) == "Oregon State"
    assert gp.cover_margin(g, "Boise State") == -8.5


# -- watcher drain + sidecar race answers --------------------------------------

def _chat_db(groups):
    import sqlite3
    db = sqlite3.connect(":memory:")
    db.executescript("CREATE TABLE chat (ROWID INTEGER PRIMARY KEY, guid TEXT, chat_identifier TEXT, style INT);"
                     "CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT);"
                     "CREATE TABLE chat_handle_join (chat_id INT, handle_id INT);")
    for i, (guid, style, handles) in enumerate(groups, 1):
        db.execute("INSERT INTO chat VALUES (?,?,?,?)", (i, guid, f"chat{i}", style))
        for h in handles:
            hid = db.execute("INSERT INTO handle (id) VALUES (?)", (h,)).lastrowid
            db.execute("INSERT INTO chat_handle_join VALUES (?,?)", (i, hid))
    return db


def test_watcher_drains_only_to_groups():
    sys.path.insert(0, str(HERE.parent / "tools" / "picky-imessage"))
    import watcher
    db = _chat_db([("any;+;league", 43, ["+12195550100", "+15045550101"]), ("any;-;+12195550100", 45, [])])
    assert [g["guid"] for g in watcher.group_chats(db)] == ["any;+;league"]
    sent, acks, polls = [], [], []

    def fake_post(url, payload, secret, timeout=10):
        if url.endswith("/ack"):
            acks.append(payload)
            return {"ok": True}
        polls.append(payload)
        return {"item": ITEM}
    old_post, old_send = watcher._post, watcher.send
    watcher._post, watcher.send = fake_post, (lambda chat, text, *a: sent.append((chat, text)) or True)
    try:
        rate = watcher.RateLimit(gap=0)
        ITEM = {"id": "a1", "chat": "any;+;league", "texts": ["one", "two"], "kind": "recap"}
        watcher.drain_outbox(db, "s", rate, [{"guid": "g"}])
        assert sent == [("any;+;league", "one"), ("any;+;league", "two")]
        assert acks[-1] == {"id": "a1", "sent": 2, "ok": True, "error": ""} and polls[0] == {"groups": [{"guid": "g"}]}
        assert len(rate.sent) == 2  # proactive sends count against the shared limiter
        sent.clear()
        ITEM = {"id": "a2", "chat": "any;-;+12195550100", "texts": ["dm?"], "kind": "recap"}
        watcher.drain_outbox(db, "s", rate, None)
        assert sent == [] and acks[-1]["ok"] is False and acks[-1]["sent"] == 0
        ITEM = None
        watcher.drain_outbox(db, "s", rate, None)
        assert sent == []
    finally:
        watcher._post, watcher.send = old_post, old_send


def test_sidecar_race_answers():
    fresh()
    import group_chat as gc
    import playoff_race as pr
    pts = {"Chuck P": 30, "Ackbar": 28, "Blinkstradamus": 27, "Liam Bryson": 26, "TG": 19, "Andrew Bub": 23}
    data = (pr.race(pts, {t: 16 for t in pts}), {"weeks_left": 4, "points_left": 16, "first_week": 12})
    liam = gc.Sender(handle="+18165550103", team="Liam Bryson")
    a = gc.race_answer("what do I need to make playoffs", liam, data)
    assert a and a.startswith("Liam, you're at 26 points"), a
    a = gc.race_answer("what does Blinky need to make the playoffs", liam, data)
    assert a and a.startswith("Blinky is at 27 points"), a
    assert gc.race_answer("can Texas make the playoffs", liam, data) is None
    assert gc.race_answer("who's winning", liam, data) is None
    assert gc.race_answer("what do I need to make playoffs", gc.Sender(handle="+1555"), data) is None


# -- code-first posts: only the weekly recap ever reaches a writer ---------------------------

class _League:
    now = t("2026-11-21T07:05")
    season = 2026
    names = {"Ackbar": "Ackbar", "Chuck P": "Charlie", "TG": "TG"}
    tid = {1: "Ackbar", 2: "Chuck P", 3: "TG"}
    db = None
    G1 = {"GameId": 1, "Away": "Utah", "Home": "Iowa", "line": -3.5, "week": 12, "Start (CT)": "2026-11-21T11:00:00",
          "picked": True}

    def nick(self, team):
        return self.names.get(team, team)

    def week_games(self, w, pool_only=True):
        return [self.G1]

    def lock_time(self, w):
        return t("2026-11-21T11:00")

    def submission(self, w):
        return {"Ackbar": False, "Chuck P": True, "TG": True}

    def picky_picks(self):
        return [{"game_id": 1, "pick": "Iowa", "week": 12}]

    def picks(self, w):
        return [{"team_id": 1, "game_id": 1, "pick": "Iowa"}, {"team_id": 2, "game_id": 1, "pick": "Utah"}]


def test_posts_are_code_only_but_the_recap():
    fresh()
    import dossier
    calls = []
    real = dossier.run_claude
    dossier.run_claude = lambda *a, **k: calls.append(a) or {"texts": ["Charlie 4-0, as foretold."]}
    saved = gp.picky_card, gp.stakes_facts, gp.standings, gp.load_take
    gp.picky_card = lambda lg, w: {1: {"label": "Iowa -3.5", "why": "value", "made": True}}
    gp.stakes_facts = lambda lg: []
    gp.standings = lambda lg: [("Chuck P", 30, 10)]
    gp.load_take = lambda w, gid: {"quotable": "Iowa's defense eats this offense alive."}
    try:
        lg = _League()
        pre = gp.make_preview(lg, 12)
        sat = gp.make_saturday(lg, 12)
        rem = gp.make_reminder(lg, 12)
        ping = gp.make_ping(lg, [(lg.G1, 17.0, 24.0)])
        assert not calls, calls  # Anthropic up, still no writer call
        assert "I'm on Iowa -3.5" in pre[1] and pre[2] == "Film room on Utah at Iowa: Iowa's defense eats this offense alive."
        assert "I'm on Iowa -3.5" in sat[0] and "Ackbar" in rem[0] and "TG" not in rem[0]
        assert "Iowa -3.5 covers (Utah 17, Iowa 24), Charlie missed this one, I had it" in ping[0], ping
        assert gp.make_ping(lg, [(lg.G1, 17.0, 24.0)]) == ping  # same key, same wording
        assert gp.make_reminder(lg, 12) == rem
        assert gp.variant(["Final: a", "Final: b", "In the books: c"], "k", recent=["Final: a", "final b!"]) \
            == "In the books: c"  # skips a recent opener
    finally:
        dossier.run_claude = real
        gp.picky_card, gp.stakes_facts, gp.standings, gp.load_take = saved


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
