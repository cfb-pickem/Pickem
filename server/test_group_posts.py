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


def test_push_goes_to_away_like_the_league():
    """A dead-even cover is Away's (set_winner_on_final: (HomePts + line) > AwayPts else Away).
    group_posts, league_rules and chat must agree; there is no 'push' in this league."""
    import chat
    import league_rules
    g = {"Away": "Utah", "Home": "Iowa", "line": -7.0, "AwayPts": 10, "HomePts": 17}
    assert gp.covered_side(g) == "Utah"
    assert league_rules.cover_winner("Iowa", "Utah", 17, 10, -7.0) == "Utah"
    assert chat.covering_side(g) == "Utah"
    assert gp.covered_side({**g, "HomePts": 18}) == "Iowa"
    assert gp.covered_side({**g, "HomePts": None}) is None


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
        assert acks[-1] == {"id": "a1", "sent": 2, "ok": True, "error": ""} and polls[0] == {"groups": [{"guid": "g"}], "dm_ok": True, "img_ok": True}
        assert len(rate.sent) == 2  # proactive sends count against the limiter they're given (the post budget)
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

    def game_locked(self, g, w):
        return self.lock_time(w) <= self.now

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
        lg.game_locked = lambda g, w: True  # the card above only exists once the picks lock (HIDDEN SIDES)
        pre = gp.make_preview(lg, 12)
        sat = gp.make_saturday(lg, 12)
        rem = gp.make_reminder(lg, 12)
        ping = gp.make_ping(lg, [(lg.G1, 17.0, 24.0)])
        assert not calls, calls  # Anthropic up, still no writer call
        assert len(pre) == 1 and len(pre[0]) <= 280 and "My card: Iowa -3.5 vs Utah (Sat 11am)" in pre[0] \
            and "Film room on Utah at Iowa: Iowa's defense eats this offense alive." in pre[0], pre
        assert len(sat) == 1 and "Iowa -3.5 vs Utah (Sat 11am)" in sat[0] and "Ackbar" in rem[0] and "TG" not in rem[0]
        assert "Iowa -3.5 covers (Utah 17, Iowa 24), Charlie missed this one, I had it" in ping[0], ping
        assert gp.make_ping(lg, [(lg.G1, 17.0, 24.0)]) == ping  # same key, same wording
        assert gp.make_reminder(lg, 12) == rem
        assert gp.variant(["Final: a", "Final: b", "In the books: c"], "k", recent=["Final: a", "final b!"]) \
            == "In the books: c"  # skips a recent opener
    finally:
        dossier.run_claude = real
        gp.picky_card, gp.stakes_facts, gp.standings, gp.load_take = saved


# -- length caps (Charlie, 2026-11-19: "make sure the posts aren't too long") ----------------------

def _events():
    return [json.loads(x) for x in ob.SEND_LOG.read_text().splitlines()] if ob.SEND_LOG.exists() else []


def test_defaults_one_text_280_cap_4():
    fresh()
    cfg = ob.config()
    assert (cfg["daily_cap"], cfg["max_chars"], cfg["max_chars_long"], cfg["second_reminder"]) == (4, 280, 450, False)
    assert ob.limits("remind") == (280, 1) and ob.limits("saturday") == (280, 1) and ob.limits("cover") == (280, 1)
    assert ob.limits("recap") == (450, 2) and ob.limits("announce") == (450, 2)
    ob.save_config(max_chars=200, max_chars_long=300)  # Charlie tunes it in picky_group_config.json
    assert ob.limits("preview") == (200, 1) and ob.limits("recap") == (300, 2)


def test_clip_keeps_lead_drops_tail():
    assert ob.clip(["short"], 280) == ["short"]
    out = ob.clip(["Week 12 lock is Sat 11am.", "A -2.5 at B\nC -3 at D", "Filler sentence here. " * 30], 80)
    assert len(out) == 1 and len(out[0]) <= 80 and out[0].startswith("Week 12 lock is Sat 11am.\nA -2.5 at B"), out
    out = ob.clip(["one two three " * 40], 50)
    assert len(out[0]) <= 50 and out[0].endswith("\u2026"), out
    out = ob.clip(["My card: A -3 vs B; C +2 vs D; E -7 vs F."], 25)
    assert out == ["My card: A -3 vs B."], out
    assert ob.clip(["a" * 100, "b" * 100], 450, 2) == ["a" * 100, "b" * 100]  # long kinds: 2 short texts ok


def test_post_never_sends_the_long_one():
    fresh()
    ob.save_config(dry_run=False, group_guid="any;+;league")
    long_ = ["Week 12 picks lock Sat 11am. Still waiting on Liam and TG."] + ["Way too much filler here. " * 20] * 3
    assert ob.post("remind", long_, "r-long", echo=lambda *_: None) == "queued"
    item = ob.next_item()
    assert len(item["texts"]) == 1 and len(item["texts"][0]) <= 280, item
    assert item["texts"][0].startswith("Week 12 picks lock Sat 11am. Still waiting on Liam and TG."), item
    ev = [e for e in _events() if e["event"] == "clipped"]
    assert ev and ev[0]["before"] > 280 and ev[0]["after"] <= 280, ev
    # the weekly recap gets the long cap: 450 over at most 2 texts
    assert ob.post("recap", ["x" * 300, "y" * 400], "rc-long", echo=lambda *_: None) == "queued"
    rc = [json.loads(p.read_text()) for p in ob.OUTBOX.glob("*.json") if "rc-long" in p.read_text()][0]
    assert len(rc["texts"]) == 1 and len(rc["texts"][0]) <= 450, rc
    # dry runs show the clipped text too
    ob.save_config(dry_run=True)
    shown = []
    ob.post("saturday", ["z " * 400], "s-long", echo=shown.append)
    assert len(shown[0].split("> ", 1)[1]) <= 280, shown


def _queued(n, kind="preview"):
    for i in range(n):
        ob.log_event("queued", kind=kind, key=f"old{i}", texts=["x"])


def test_daily_cap_four_with_priorities():
    fresh()
    ob.save_config(dry_run=False, group_guid="any;+;league")
    _queued(3)
    # 3 in the last 24 h: a low-priority post (preview, picks-up, pings, cover alerts) is skipped for good...
    assert ob.post("preview", ["what changed"], "pv", echo=lambda *_: None) == "capped"
    assert ob.done("pv") and ob.post("preview", ["what changed"], "pv", echo=lambda *_: None) == "dup"  # no backlog
    assert ob.post("cover", ["close one"], "cv", quiet=True, now=t("2026-11-21T15:00"), echo=lambda *_: None) == "capped"
    assert any(e.get("why") == "daily-cap" and e["key"] == "pv" for e in _events())
    # ...but the last slot is kept for the recap, pick reminders and the Saturday post
    assert ob.post("remind", ["lock 11am, still waiting on Liam"], "rm", echo=lambda *_: None) == "queued"
    assert ob.post("saturday", ["my sides"], "sat", echo=lambda *_: None) == "capped"  # 4 of 4 used
    ob.save_config(daily_cap=6)  # tunable
    assert ob.post("saturday", ["my sides"], "sat2", echo=lambda *_: None) == "queued"


def test_typical_saturday_reminder_and_recap_always_fit():
    """A busy Saturday (cover alerts firing) can't crowd out Saturday's reminder/post or Sunday's recap."""
    fresh()
    ob.save_config(dry_run=False, group_guid="any;+;league")
    quiet = dict(echo=lambda *_: None)
    assert ob.post("remind", ["lock 11am"], "rm12", **quiet) == "queued"
    assert ob.post("saturday", ["my sides"], "sat12", **quiet) == "queued"
    res = [ob.post("cover", [f"close {i}"], f"cv{i}", **quiet) for i in range(4)]
    assert res == ["queued", "capped", "capped", "capped"], res
    assert ob.post("recap", ["week 12 recap"], "recap12", **quiet) == "queued"


def test_second_reminder_off_by_default():
    fresh()
    lock = t("2026-11-20T19:00")  # Friday night: slots 07:00 and 17:00
    assert gp.group_slot(lock, t("2026-11-20T07:05")) == "first"
    assert gp.group_slot(lock, t("2026-11-20T17:05")) is None
    ob.save_config(second_reminder=True)
    assert gp.group_slot(lock, t("2026-11-20T17:05")) == "second"
    assert gp.group_slot(t("2026-11-21T11:00"), t("2026-11-21T07:05")) == "sat"  # Saturday reminder still goes out


RECAP_FACTS = [
    "Week 11 records: Charlie 4-0; Ackbar 3-1; Blinky 3-1; Bub 0-4.",
    "Best week 11 record (4-0): Charlie.",
    "Worst week 11 record (0-4): Bub.",
    "WORST PICK: SMU +1.5 (SMU at Wake Forest) missed the line by 40.5 points; final SMU 7, Wake Forest 49. "
    "Taken by Bub, Liam, TG.",
    "BEST CALL: UTSA +2.5 (UTSA at Rice) covered by 21.5; only 5 of 14 had it: Charlie, Ackbar, TG, Liam, Bub.",
    "STANDINGS MOVE: Charlie stays 1st (30-18); Blinky up from 6th to 3rd (27-21).",
    "Season standings: 1st Charlie 30-18; 2nd Ackbar 28-20; 3rd Blinky 27-21 (of 14).",
    "YOUR OWN WEEK (Picky): 0-3 in week 11: Boise State -13.5 missed. Your season: 0-3.",
]


def test_plain_recap_short_and_leads_with_the_winner():
    r = gp.plain_recap(11, RECAP_FACTS)
    assert len(r) == 1 and len(r[0]) <= 450, r
    assert r[0].startswith("Week 11 is in the books. Charlie won it at 4-0. Standings: Charlie stays 1st"), r
    assert "Best call: UTSA +2.5 covered by 21.5, only 5 of 14 had it." in r[0] and r[0].endswith("Me: 0-3, 0-3 on the year."), r
    tight = gp.plain_recap(11, RECAP_FACTS, 120)
    assert len(tight[0]) <= 120 and tight[0].startswith("Week 11 is in the books. Charlie won it at 4-0."), tight


class _RecapLeague(_League):
    def __init__(self):
        self.names = dict(NAMES)


def test_recap_too_long_gets_one_rewrite_then_the_short_version():
    fresh()
    saved = gp.recap_facts
    gp.recap_facts = lambda lg, w: (RECAP_FACTS, {})
    calls = []
    try:
        long_draft = ["Charlie went 4-0. " + "Blah blah blah. " * 40]

        def writer(prompt):
            calls.append(prompt)
            return long_draft
        rec = gp.make_recap(_RecapLeague(), 11, writer=writer)
        assert len(calls) == 2 and "ONE text, under 450 characters" in calls[0] and "keep each under 450" in calls[1]
        assert rec == gp.plain_recap(11, RECAP_FACTS) and len(rec) == 1 and len(rec[0]) <= 450, rec
        calls.clear()
        rec = gp.make_recap(_RecapLeague(), 11, writer=lambda p: calls.append(p) or ["Charlie 4-0, Dear Leader sweeps."])
        assert rec == ["Charlie 4-0, Dear Leader sweeps."] and len(calls) == 1  # a short good draft goes out as written
        assert not any("Playoff math" in x for x in rec)  # no playoff-math text tacked on
    finally:
        gp.recap_facts = saved


def test_standings_move():
    rows = [("Chuck P", 30, 18), ("Ackbar", 28, 20), ("Blinkstradamus", 27, 21), ("TG", 26, 22), ("Liam Bryson", 26, 22)]
    before = [("Chuck P", 26, 18), ("Ackbar", 25, 19), ("TG", 25, 19), ("Liam Bryson", 25, 19), ("Blinkstradamus", 24, 20)]
    mv = gp.standings_move(rows, before, lambda t: NAMES.get(t, t))
    assert mv == ("STANDINGS MOVE: Charlie stays 1st (30-18); Blinky up from 5th to 3rd (27-21); "
                  "TG down from 2nd to 4th (26-22)."), mv  # the two biggest moves around the top 4
    assert gp.standings_move(rows, [(t, 0, 0) for t, _, _ in rows]) is None  # week 1: nothing to compare


def test_saturday_post_before_lock_has_no_sides():
    """Before the lock the Saturday post still goes at its old time (09:30), games and lines only; his sides
    stay hidden till kickoff (Charlie 2026-11-19 15:41: hidden till kickoff when they lock)."""
    fresh()
    saved = gp.picky_card
    gp.picky_card = lambda lg, w: {}
    try:
        sat = gp.make_saturday(_League(), 12)
        assert sat and len(sat) == 1 and len(sat[0]) <= 280 and gp.HIDDEN_NOTE in sat[0] \
            and "Iowa -3.5 vs Utah (Sat 11am)" in sat[0] and "Where I'm at" not in sat[0], sat
    finally:
        gp.picky_card = saved


def test_ping_burst_is_one_short_text_and_waits():
    fresh()
    games = [dict(_League.G1, GameId=i, Away=f"Away State {i}", Home=f"Home Tech {i}", line=-3.5 - i)
             for i in range(1, 5)]
    lg = _League()
    lg.picks = lambda w: [{"team_id": 1, "game_id": g["GameId"], "pick": g["Home"]} for g in games] + \
        [{"team_id": 2, "game_id": g["GameId"], "pick": g["Away"]} for g in games]
    saved = gp.standings
    gp.standings = lambda lg_: [("Chuck P", 30, 10)]
    try:
        ping = gp.make_ping(lg, [(g, 17.0, 31.0) for g in games])
        assert len(ping) == 1 and len(ping[0]) <= 280 and all(f"Home Tech {i}" in ping[0] for i in range(1, 5)), ping
    finally:
        gp.standings = saved
    # due_pings holds a final while another pool game is still on, then sends them together
    lg.games = [dict(g, picked=True, **{"Start (CT)": "2026-11-21T11:00:00"}) for g in games[:2]]
    status = {1: {"final": True, "state": "post", "period": 4, "away": 17.0, "home": 31.0},
              2: {"final": False, "state": "in", "period": 2, "away": 7.0, "home": 3.0}}
    old = gp.espn_status
    gp.espn_status = lambda gid, client: status[gid]
    try:
        lg.now = t("2026-11-21T14:30")
        assert gp.due_pings(lg, None) is None  # game 2 is in the 2nd quarter: hold
        lg.now = t("2026-11-21T15:30")
        assert gp.due_pings(lg, None) is None  # still on, 60 min in
        status[2] = {"final": True, "state": "post", "period": 4, "away": 10.0, "home": 3.0}
        lg.now = t("2026-11-21T15:45")
        assert [g["GameId"] for g, _, _ in gp.due_pings(lg, None)] == [1, 2]  # one combined ping
        status[2] = {"final": False, "state": "in", "period": 1, "away": 0.0, "home": 0.0}
        fresh()
        lg.now = t("2026-11-21T14:30")
        assert gp.due_pings(lg, None) is None
        lg.now = t("2026-11-21T16:31")  # past ping_hold_min (120): stop waiting
        assert [g["GameId"] for g, _, _ in gp.due_pings(lg, None)] == [1]
    finally:
        gp.espn_status = old


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
