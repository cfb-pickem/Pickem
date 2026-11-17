"""
Charlie's flagged Picky replies from 2026-11-16, pinned as model-free tests against the code path that
produces or filters each one (week labels, ranking age, lock wording, citation density, repeated
record jabs, pick privacy, stale injury dates). The eval items for the same failures (asked through
the exam) live in eval/group_items.py ("reg-*", wording in cfb-pickem-private); the list for the
sparring job is cfb-pickem-private/spar/charlie-regressions.md.

    ./.venv/bin/python3 -I test_charlie_regressions.py

No model, no network. A test in XFAIL is a live bug that's still open: it prints XFAIL instead of
failing the suite, and XPASS once fixed (then take it out of XFAIL).
"""

from __future__ import annotations

import datetime as dt
import os
import sys
import tempfile
import types
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

import chat  # noqa: E402
import league_rules  # noqa: E402
import no_repeat  # noqa: E402
import pick_brief  # noqa: E402
import router  # noqa: E402
from test_picky_brain import _wk11_ctx  # noqa: E402  (week 11 final, week 10 = LSU, Chuck P)

MON = dt.datetime(2026, 11, 16, 16, 30)  # the Monday after week 11, when all of these went out
XFAIL: set[str] = set()


def _send(text: str, q: str, lane: str = "full") -> str:
    """The deterministic send-point passes every text reply gets (group_chat: debot after the draft)."""
    return router.debot(text, q, lane)


# ---- week labels: 'LSU last week' about week 10, the Monday after week 11 (r0-c01, r0-c04, r0-c13) ----------

def test_lsu_last_week_is_relabeled():
    c = _wk11_ctx()
    for bad in ("You're 4-0 this week, which is honestly disgusting for someone who missed LSU last week.",
                "It's hard to argue with a 30-18 season, even if you did let LSU get blown out by 18 points last week.",
                "Don't let that first-place trophy go to your head because you still choked on LSU last week."):
        out = chat.fix_last_week(bad, c, MON)
        assert "LSU" in out and "last week" not in out.lower(), out


# ---- stock tics and the 'For the record:' dump (r0-c01, r0-c02) ---------------------------------------------

def test_tics_and_for_the_record_cut_at_send():
    for tic in ("participation trophy", "For the record", "go to your head"):
        assert no_repeat.banned_tic(f"I'm basically the league's {tic} at this point."), tic
    a = ("I went 0-3, which is a nice way of saying I'm currently the league's most expensive participation "
         "trophy. Wake Forest covered and UTSA covered.")
    out = _send(a, "Howd the week go")
    assert "participation trophy" not in out.lower() and out, out
    b = ("I'm using your salt as a tiebreaker. For the record: Boise State -13.5 was a LOSS; North Texas -2.5 "
         "was a LOSS; Hawai'i +6.5 was a LOSS, and I'm 0-3 on decided games this week.")
    out = _send(b, "you suck")
    assert "for the record" not in out.lower(), out


# ---- the tacked-on record jab: 'You're 4-0 this week, so...' on news / lock / pick answers ------------------

JABS = [
    "Try not to let that 4-0 week go to your head too much.",                                   # r0-c17
    "You're 4-0 this week, so I guess you're finally smart enough to ask about the real drama.",  # SEC cap reply
    "You're 4-0 this week, so I guess your brain is finally working well enough to ask questions.",  # r0-c09
    "No more flipping, Charlie, you're stuck with your 4-0 week like a bad tattoo.",              # pick-lock reply
    "You're sitting in first place, 2 points ahead of second, so don't let that ego get too big.",  # r0-c11
]


def test_record_jab_regexes_catch_every_variant():
    for j in JABS:
        assert chat._RECORD_JAB.search(j) or chat._SELF_INTRUSION.search(j), j
    assert not chat._RECORD_JAB.search("Iowa -6.5 if it holds through Tuesday's lock.")


def test_news_answer_drops_record_jab_after_an_earlier_insult():
    """Live 11-16: Charlie said 'You suck' earlier in the DM, so every later answer counted as a hostile
    convo and the self-intrusion filter was off: 'Whats the latest news from ross' got the SEC cap story
    plus 'You're 4-0 this week, so I guess...'. Only the CURRENT text being an insult opens the record."""
    c = _wk11_ctx()
    msgs = [{"role": "user", "content": "You suck"}, {"role": "assistant", "content": "Bold talk."},
            {"role": "user", "content": "Whats the latest news from ross"}]
    q = msgs[-1]["content"]
    no_self = chat.self_intrusion_off_topic(c, q, chat.hostile_now(msgs), None, "")
    assert no_self, "news ask after an old insult must still keep the caller's record out"
    assert not chat.sentence_ok(JABS[1], set(), no_self=no_self)
    # the insult itself still gets to fire back with the record
    assert not chat.self_intrusion_off_topic(c, "You suck", chat.hostile_now(msgs[:1]), None, "")


def test_record_jab_repeat_is_caught_by_the_ledger():
    with tempfile.TemporaryDirectory() as d:
        led = no_repeat.Ledger(Path(d) / "nr.json")
        led.record("You're 4-0 this week and sitting in first place, so I'll give you that.", target="Chuck P",
                   chat_id="dm")
        assert led.is_repeat("You're 4-0 this week and sitting in 1st place, so I'll give you that win.",
                             target="Chuck P", chat_id="dm")


def test_proactive_standings_hook_stays_off():
    """The 'You're in first place, 2 points ahead of second' tail came from the proactive hook (off 11-16)."""
    with patch.dict(os.environ, {"PICKY_HOOK": ""}):
        assert chat.proactive_note(_wk11_ctx(), [{"role": "user", "content": "You alive?"}], "You alive?") == ""


# ---- rankings: the '(Rankings as of Nov 16.)' tag on an answer that cites no ranking (r0-c12) ----------------

def test_rankings_tag_only_when_a_rank_is_cited():
    need = chat.rankings_fact("- CFP committee rankings, released Nov 16: 1 Indiana, 2 Ohio State.")
    assert need
    check, tail = need
    expansion = ("The CFP is expanding to 24 teams starting in 2027, which includes the four Power 4 champions, "
                 "the top Group of Six team, and 19 at-large picks.")
    assert check(expansion), "no ranking cited: no '(Rankings as of ...)' tag"
    assert not check("Notre Dame is 5th in the CFP, so the committee came around.")  # a rank, no date: tag it
    assert check("Notre Dame is No. 5 as of Nov 16.")


# ---- stale insider ranking: 'ranked the Irish 11th while the AP had them 4th' (2 weeks old) -----------------

INSIDER = """# Insider bank
Last refreshed 2026-11-16

### CFP format and committee
- Nov 8: Dellenger wrote that the committee is Notre Dame's new rival: its Nov 3 ranking had the Irish 11th while the AP had them 4th, a year after it took Miami over ND. ND is No. 5 now. (Dellenger, Yahoo) [checked 2026-11-16]
- Nov 12: The SEC is circulating a tiered cap plan with fines that could top $10 million. (Dellenger, Yahoo) [checked 2026-11-16]
"""


def test_two_week_old_ranking_note_stays_out():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "insider.md"
        p.write_text(INSIDER)
        with patch.object(chat, "INSIDER_FILE", p), patch.object(chat, "_now_ct_naive", lambda: MON), \
                patch.dict(chat._insider_memo, {"mtime": None, "data": None}):
            note = chat.insider_note("Whats the latest news from ross")
            assert "cap plan" in note, note
            assert "11th" not in note, note
            note = chat.insider_note("is the committee screwing notre dame")
            assert "11th" not in note, note


# ---- news asks: 'What is going on in college football', 'latest nees', 'news from ross' (r0-c09/13/14) -------

def _fake_news(lines):
    tg = types.SimpleNamespace(tag=lambda q, derive_confs=False: {})
    fv = types.SimpleNamespace(use=True, hedge=False, as_of="5:10 PM CT")
    return [patch.object(chat.news, "tagger", lambda: tg),
            patch.object(chat.news, "breaking_for", lambda q, limit=5: list(lines)),
            patch.object(chat.news, "load_cache", lambda: {"fetched_at": 0}),
            patch.object(chat.news, "sources_line", lambda: "ESPN, CBS"),
            patch.object(chat.news, "as_of", lambda cache=None: "5:10 PM CT"),
            patch.object(chat.freshness, "check", lambda name, at=None, now=None: fv)]


def test_general_and_typo_news_asks_get_top_stories():
    lines = ["2h ago, per ESPN: Illinois may turn to its third-string QB after the Houser injury"]
    ps = _fake_news(lines)
    for p in ps:
        p.start()
    try:
        for q in ("What is going on in college football", "Whats the latest nees", "Whats news picky",
                  "Whats the latest news from ross", "Whats the lates cfp news"):
            assert chat._NEWS_ASK.search(q), q
            note = chat.news_note(q)
            assert "Houser" in note and "nothing in your news feed" not in note, (q, note[:120])
    finally:
        for p in ps:
            p.stop()
    assert chat._DELLENGER_ASK.search("Whats the latest news from ross")


# ---- citation density: 'per CBS Sports 30 min ago ... 2h ago per ClutchPoints ... 7h ago per The Athletic' ----

CITED = ("Lane Kiffin is reliving a nightmare finish against Texas per CBS Sports 30 min ago. Georgia improved to "
         "9-1 over Missouri 2h ago per ClutchPoints, while UTEP fired coach Scotty Walden 7h ago per The Athletic. "
         "Also 2h ago per ESPN, Texas DL Hero Kanu had season-ending surgery on his arm.")


def test_news_reply_names_a_source_at_most_once():
    import re
    out = _send(CITED, "Whats news picky")
    cites = re.findall(r"\bper (?:the )?[A-Z]\w+|\b\d+\s?(?:min|mins|minutes?|h|hrs?|hours?) ago\b", out)
    assert len(cites) <= 1, (cites, out)
    for keep in ("Kiffin", "Walden", "Kanu"):  # the news itself stays
        assert keep in out, out


# ---- lock wording: 'Lines dont lock till tuesday noon'; picks lock at the first kickoff, never 'unlock' -------

def test_line_lock_is_tuesday_11_ct_noon_eastern():
    a = league_rules.answer("when do lines lock?")
    assert a and "Tuesday" in a and "11" in a and "CT" in a and "noon Eastern" in a, a
    sat = dt.datetime(2026, 11, 21, 11, 0)
    assert pick_brief.line_lock(sat) == dt.datetime(2026, 11, 17, 11, 0)
    p = league_rules.answer("when do picks lock?")
    assert p and "first game" in p and "unlock" not in p.lower(), p


# ---- pick privacy (Charlie 11-16: 'shouldnt he not know my picks?' -> OK as is: own picks only, in his DM) ----

def test_dm_sees_own_picks_never_another_members_unlocked_pick():
    now = dt.datetime(2026, 11, 17, 12)
    games = [{"GameId": 1, "Status": "scheduled", "Start (CT)": "2026-11-21T11:00:00"}]
    picks = [{"team_id": 10, "game_id": 1, "pick": "Iowa", "by_slots": False},
             {"team_id": 20, "game_id": 1, "pick": "Illinois", "by_slots": False}]
    out = chat.filter_visible_picks(picks, games, caller_team_id=10, now_ct_naive=now)
    assert [p["pick"] for p in out] == ["Iowa"], out
    assert chat.filter_visible_picks(picks, games, caller_team_id=None, now_ct_naive=now) == []


# ---- stale injury note: Chiles quoted from Nov 6 when Nov 16 had news ----------------------------------------

def _qb_items(*reports):
    return [{"team": "Michigan State", "date": d, "title": "", "text": txt, "kind": "injury"} for d, txt in reports]


def test_newest_qb_report_wins_over_an_older_worse_one():
    g = {"away": "Michigan State", "home": "Iowa"}
    notes = {"leaders": ["Passing Yards: Aidan Chiles (2100 yds)"]}
    t = {"name": "Michigan State"}
    old_out = ("2026-11-06", "Michigan State QB Aidan Chiles is out with a shoulder injury.")
    # the worst status among same-age reports is quoted from the newest one (a877c3a)
    f = pick_brief.f_personnel(g, "Michigan State", t, {}, notes, _qb_items(
        old_out, ("2026-11-16", "Michigan State QB Aidan Chiles remains out, coach said Monday.")))
    qb = next(x for x in f if x["n"] == 8)
    assert "out" in qb["why"] and "Monday" in qb["why"], qb
    # a newer report that he's cleared / will start supersedes the Nov 6 'out'
    f = pick_brief.f_personnel(g, "Michigan State", t, {}, notes, _qb_items(
        old_out, ("2026-11-16", "Michigan State QB Aidan Chiles has been cleared and is expected to start Saturday.")))
    qb = next(x for x in f if x["n"] == 8)
    assert " is out " not in qb["why"] and "Nov 6" not in qb["why"] and "shoulder" not in qb["why"], qb


if __name__ == "__main__":
    fails = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"{'XPASS' if name in XFAIL else '  ok '} {name}")
            except Exception as e:  # noqa: BLE001
                if name in XFAIL:
                    print(f"XFAIL {name}: {e!r}"[:300])
                else:
                    fails += 1
                    print(f"FAIL  {name}: {e!r}"[:600])
    print("\nALL GREEN" if not fails else f"\n{fails} FAILED")
    sys.exit(1 if fails else 0)
