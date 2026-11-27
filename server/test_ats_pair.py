"""
Two-part ATS asks (nightly 2026-11-27, genius ats 1/9): 'How has Houston been ATS? And Baylor?', 'who's been
better against the spread, USC or UCLA? What happened last year?', 'what happened in Nebraska-Iowa last year, and
how have both teams done ATS?'. The ats_profile lane answered for the first team named only and dropped last
year's game. Now:

- two teams named, or one team + 'both teams', = both sides' records (this week's role splits + form) and one
  comparison in lean words (never a lock);
- 'what happened last year' / 'last year's game' next to an ATS ask adds last season's meeting, the line said as a
  role ('as a 4-point underdog'), a push said as a push;
- 'X hasn't covered in a while, right?' is an ATS ask (not the premise lane, which checks last week's results);
- 'title game next week, lookahead spot?' takes the lookahead lane, not the conference race.

Every number is backed by facts_used. All on the frozen fact-store fixture (server/eval/fixtures/factstore).

Run:  ./.venv/bin/python3 -I test_ats_pair.py
"""

from __future__ import annotations

import re
import sys

sys.path.insert(0, ".")
import factstore as FS  # noqa: E402
import test_factstore as TF  # noqa: E402

LEAN_WORDS = ("coin flip", "slight lean", "lean")


def lane(q: str):
    import router
    FS.use(TF.built()["path"], TF.inputs(), now=TF.NOW)
    try:
        return router.ats_profile_text({}, q)
    finally:
        FS.use(None, None)


def rec(team: str) -> str:
    FS.use(TF.built()["path"], TF.inputs(), now=TF.NOW)
    try:
        return FS.wlp_text(FS.store()["teams"][team]["ats_profile"]["overall"])
    finally:
        FS.use(None, None)


def test_and_the_other_team_answers_both():
    out = lane("How has Duke been against the spread this year? And Clemson?")
    assert out, out
    assert f"Duke is {rec('Duke')} against the number" in out, out
    assert f"Clemson is {rec('Clemson')} against the number" in out, out
    assert any(w in out for w in LEAN_WORDS) and not re.search(r"\block\b", out, re.I), out


def test_both_teams_with_one_named_uses_this_weeks_opponent():
    out = lane("How have both teams done ATS in the Duke game?")
    assert out and "Duke is" in out and "Clemson is" in out, out


def test_who_has_been_better_names_the_better_team():
    out = lane("Who's been better against the spread, Indiana or Washington?")
    assert out and "Indiana is" in out and "Washington is" in out, out
    a, b = (list(map(int, rec(t).split("-")[:2])) for t in ("Indiana", "Washington"))
    pa, pb = a[0] / sum(a), b[0] / sum(b)
    if abs(pa - pb) >= 0.25:
        better = "Indiana" if pa > pb else "Washington"
        assert f"{better} has been the better ATS team" in out, out
    else:
        assert "coin flip" in out, out


def test_this_weeks_role_splits_for_each_side():
    out = lane("How has Duke been against the spread this year? And Clemson?")
    # fixture: Duke -13.5 at home vs Clemson
    assert "as a favorite" in out and "at home" in out, out
    assert "as an underdog" in out and "on the road" in out, out


def test_last_year_adds_the_meeting_as_a_role():
    out = lane("Who's been better against the spread, Duke or Clemson? What happened last year?")
    # fixture meeting: Duke won 46-45 at Clemson, line Clemson -4, Duke covered by 5
    assert "Last meeting (2025): Duke won 46-45 at Clemson as a 4-point underdog and covered by 5." in out, out
    assert not re.search(r"\bClemson -4\b", out), out


def test_last_year_on_a_one_team_ask():
    out = lane("Duke ATS this year, and what happened last year?")
    assert out.startswith(f"Duke is {rec('Duke')}") and "Last meeting (2025)" in out, out


def test_no_meeting_without_a_last_year_ask():
    for q in ("How has Duke been against the spread this year? And Clemson?", "how's Duke against the number?"):
        assert "Last meeting" not in lane(q), q


def test_push_said_as_a_push():
    import router
    used = []
    g = {"id": "x", "sheet": {"meeting": {"season": 2025, "winner": "Alabama", "score": "27-20", "where": "at Auburn",
                                          "line": "Alabama -7", "covered": "Alabama", "cover_by": 0},
                              "facts": [{"kind": "meeting", "id": "m", "text": "Last meeting (2025): Alabama won 27-20 "
                                         "at Auburn, line Alabama -7: Alabama covered by 0", "value": "27-20"}]}}
    s = router._ats_meeting(g, used)
    assert s == "Last meeting (2025): Alabama won 27-20 at Auburn as a 7-point favorite, a push against the number.", s
    assert used


def test_favorite_won_but_did_not_cover():
    import router
    g = {"id": "y", "sheet": {"meeting": {"season": 2025, "winner": "USC", "score": "29-10", "where": "at home vs UCLA",
                                          "line": "USC -21.5", "covered": "UCLA", "cover_by": 2.5},
                              "facts": [{"kind": "meeting", "id": "m", "text": "x", "value": "29-10"}]}}
    s = router._ats_meeting(g, [])
    assert s.endswith("as a 21.5-point favorite, but UCLA covered by 2.5."), s


def test_every_number_is_backed_by_facts_used():
    for q in ("How has Duke been against the spread this year? And Clemson?",
              "Who's been better against the spread, Duke or Clemson? What happened last year?",
              "How have both teams done ATS in the Duke game?", "Duke ATS this year, and what happened last year?",
              "Who's been better against the spread, Indiana or Washington?"):
        out = lane(q)
        assert out and getattr(out, "facts_used", None), (q, out)
        assert FS.unsupported(out, out.facts_used) == [], (q, out, FS.unsupported(out, out.facts_used))
        assert len(out) <= 700, (q, len(out))


def test_never_writes_a_team_with_a_spread_label():
    for q in ("How has Duke been against the spread this year? And Clemson?",
              "Who's been better against the spread, Duke or Clemson? What happened last year?"):
        out = lane(q)
        assert not re.search(r"\b[A-Z][\w&'.]*(?: [A-Z][\w&'.]*)* [+-]\d", out), out


def test_routing():
    import router
    assert router.fact_intent("Ohio State hasn't covered in a while, right? And how's Michigan on the road?")[0] == "ats_profile"
    assert router.fact_intent("Hawaii covered last week right?")[0] == "premise"
    FS.use(TF.built()["path"], TF.inputs(), now=TF.NOW)
    try:
        assert router.fact_intent("Duke -13.5 vs Clemson with the ACC title game next week, lookahead spot?")[0] \
            == "lookahead_game"
        assert router.fact_intent("what's the ACC title game picture?")[0] == "conf_race"
    finally:
        FS.use(None, None)


def test_drought_premise_confirmed_when_true():
    out = lane("Duke hasn't covered in a while, right? And Clemson?")
    FS.use(TF.built()["path"], TF.inputs(), now=TF.NOW)
    try:
        s = FS.store()["teams"]["Duke"]["ats_profile"]["streak"]
    finally:
        FS.use(None, None)
    assert out.startswith("Yeah, ") == (s["kind"] == "miss" and s["n"] >= 2), (s, out)


if __name__ == "__main__":
    fails = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok   {name}")
            except Exception as e:  # noqa: BLE001
                fails += 1
                print(f"FAIL {name}: {e!r}"[:900])
    print("ALL PASS" if not fails else f"{fails} FAILED")
    sys.exit(1 if fails else 0)
