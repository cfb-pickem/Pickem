"""
Personnel answers the genius judge failed (final-g3: personnel 0/7), fixed by question TYPE on the frozen fixture:

- A stat Picky states comes from the store with its source and window ("per CFBD box scores through week 11"),
  and only for the QB the question is about (the backup / the next man up / 'has he played'); a status ask
  doesn't get the player's whole season line.
- Any store-lane answer sentence with a stat number (X-of-Y, N yards, N TD, N INT ...) that facts_used doesn't
  carry is dropped before it's sent; the voice line can't add a stat in words ('three picks') either.
- Injury recency comes from the report date vs the team's last game (this week's news cycle) and the games he
  actually missed: 'old news' only for a report from before the last game or a player who's missed 2+ games.
- A hedged report ('working toward being ready') isn't 'probable' because a note's label said so.
- Source quotes: only the clauses about that player, never the same quote twice, no dangling quote marks.

Run:  ./.venv/bin/python3 -I test_personnel_fix.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import factstore as FS  # noqa: E402
import personnel as P  # noqa: E402
import router  # noqa: E402
from test_personnel_store import _patched, _set_row, built, ok_facts, use  # noqa: E402

STAT = re.compile(r"\d+-of-\d+|\b\d+ (?:yards|yds|TD|INT)\b")


def _stat_sentences(txt: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?])\s+", txt) if STAT.search(s)]


# --- stats: sourced and windowed, only where the question wants them -------------------------------------------

def test_qb_bios_state_starts_and_record_but_no_season_line():
    """The judge can't check a season passing line from the box scores (final-g3 + mid1: 'unverified' every time,
    'robotic stat dump'): bios say starts and record as the starter; a QB with no starts is described in words."""
    asks = [(router.qb_status_text, "Is Sayin playing Saturday? If not, what's the backup done?", "1-2 SU, 0-3 ATS"),
            (router.qb_status_text, "Who's Clemson's QB Friday? Has he ever played?", "in relief"),
            (router.qb_status_text, "North Texas QB situation? Should I care?", "in relief"),
            (router.backup_qb_text, "Who's Ohio State's backup QB?", "1-2 SU, 0-3 ATS")]
    with use(built()["path"]):
        for fn, q, want in asks:
            out = fn({}, q)
            assert want in out, (q, out)
            assert not re.search(r"\d+-of-\d+|\d+ yards", out), (q, out)
            ok_facts(out)


def test_status_ask_doesnt_dump_the_players_own_season_line():
    """'Who's starting at QB for Tennessee' : MacIntyre's status, starts and record; the stat line is for the man
    behind him only."""
    with use(built()["path"]):
        out = router.qb_status_text({}, "Who's starting at QB for Tennessee vs LSU?")
    head = out.split("If he can't go", 1)[0]
    assert "last 6" in head and "5-1" in head, out
    assert not STAT.search(head), out
    ok_facts(out)


def test_store_answers_drop_unsourced_stat_sentences():
    used = [{"id": "x", "kind": "qb_start", "subject": "A", "value": 3, "text": "A has 3 starts (1-2 SU)",
             "source": "CFBD", "as_of": "2026-11-18"}]
    a = FS.answer("A has 3 starts (1-2 SU). He's thrown 87-of-133 for 1024 yards, 8 TD. Watch the line.", used)
    out = FS.drop_unsourced_stats(a)
    assert "87-of-133" not in out and "1024" not in out, out
    assert out.startswith("A has 3 starts (1-2 SU).") and "Watch the line." in out, out
    assert out.facts_used == used
    kept = FS.drop_unsourced_stats(FS.answer("A has 3 starts (1-2 SU).", used))
    assert kept == "A has 3 starts (1-2 SU).", kept


def test_dispatch_applies_the_stat_check_to_store_lanes():
    orig = router.backup_qb_text
    used = [{"id": "x", "kind": "qb_start", "subject": "A", "value": 3, "text": "A has 3 starts",
             "source": "CFBD", "as_of": "2026-11-18"}]
    try:
        router.backup_qb_text = lambda ctx, q: FS.answer("A has 3 starts. He's 40-of-60 for 500 yards.", used)
        out = router.fact_answer("backup_qb", {"games_this_week": []}, "Who's Ohio State's backup QB?")
    finally:
        router.backup_qb_text = orig
    assert out == "A has 3 starts.", out


def test_voice_line_cant_add_a_stat_in_words():
    assert not router.voice_line_ok("Three picks in one game is a public service.", "x", {})
    assert not router.voice_line_ok("Two touchdowns won't save that offense.", "x", {})
    assert router.voice_line_ok("That quarterback room is a crime scene.", "x", {})


# --- injury recency --------------------------------------------------------------------------------------------

def test_fresh_injury_isnt_old_news():
    """Buchanan: reported after Michigan's last game, missed only that one game: this week's news."""
    with use(built()["path"]):
        out = router.injury_impact_text({}, "Any injuries that matter for Michigan vs UCLA?")
    assert "JJ Buchanan (" in out, out
    assert "out for weeks" not in out and "has been out" not in out, out
    ok_facts(out)


def test_long_absence_is_old_news_with_games_missed():
    """Sayin: reported before Ohio State's last game and hasn't played in 3: in the number already."""
    with use(built()["path"]):
        rows = {r["player"]: r for r in FS.player_status(team="Ohio State")}
        out = router.injury_impact_text({}, "Any injuries that matter for Ohio State?")
    assert rows["Julian Sayin"]["missed"] == 3, rows["Julian Sayin"]
    assert "Julian Sayin" in out and "missed the last 3 games" in out, out
    assert "Julian Sayin (" not in out, "he's not listed as new"
    ok_facts(out)


def test_report_before_the_last_game_is_old_without_box_scores():
    """A lineman never shows up in box scores: his report date alone says whether it's this week's news."""
    p = _patched("brunner-old", lambda st: _set_row(st, "Indiana", "Joe Brunner", reported_at="2026-11-10"))
    with use(p):
        out = router.injury_impact_text({}, "Indiana lost a lineman? Does that matter at Washington?")
    assert "Joe Brunner (" not in out and "Joe Brunner" in out, out
    assert "before their last game" in out, out
    ok_facts(out)


# --- status words and quotes -----------------------------------------------------------------------------------

def test_hedged_report_isnt_probable_because_of_a_label():
    note = {"team": "home", "status": "probable", "players": ["George MacIntyre"], "date": "2026-11-16",
            "source_name": "Yahoo Sports",
            "text": "Tennessee QB George MacIntyre hurt his right knee vs Texas A&M; Heupel said Nov 16 he is working "
                    "toward being ready for LSU and 'looked good.'"}
    rows = P.normalize(intel={"games": {"1": {"away": "LSU", "home": "Tennessee", "facts": [note]}}}, now=1795000000)
    r = next(x for x in rows if x["player"] == "George MacIntyre")
    assert r["status"] == "questionable", r


def test_quote_is_the_players_clauses_once():
    q = ("Clemson freshman starting QB Tait Reynolds is out for the season (meniscus surgery), and backup Christopher "
         "Vizzina (concussion) is ruled out; fifth-year former walk-on Trent Pearman makes his first career start.")

    def same_quote(st):
        _set_row(st, "Clemson", "Tait Reynolds", quote=q)
        _set_row(st, "Clemson", "Christopher Vizzina", quote=q)
    with use(_patched("clem-quote", same_quote)):
        out = router.qb_status_text({}, "Who's Clemson's QB Friday? Has he ever played?")
    assert out.count("meniscus") == 1, out
    assert "Christopher Vizzina (concussion) is ruled out" in out, out
    ok_facts(out)


def test_no_dangling_quote_marks():
    q = ("Tennessee QB George MacIntyre hurt his right knee vs Texas A&M; Heupel said Nov 16 he is working toward "
         "being ready for LSU and 'looked good.' Faizon Brandon, who started the first five games before a shoulder "
         "injury, finished the A&M game and would start if MacIntyre can't.")
    with use(_patched("tenn-quote", lambda st: _set_row(st, "Tennessee", "George MacIntyre", quote=q))):
        out = router.qb_status_text({}, "Who's starting at QB for Tennessee vs LSU?")
    assert "'" not in re.sub(r"(?<=[A-Za-z])'(?=[A-Za-z])", "", out), out
    ok_facts(out)


def test_hedge_clause_after_the_player_stays_in_the_quote():
    """'... MacIntyre hurt his right knee; Heupel said he is working toward playing vs LSU, and Brandon could also
    play': the he-clause is the status, so it stays even though it names the backup further on."""
    with use(built()["path"]):
        out = router.qb_status_text({}, "Who's starting at QB for Tennessee vs LSU?")
    assert "working toward playing" in out, out
    assert "6 starts (" not in out and "((" not in out and "))" not in out, out
    ok_facts(out)


def test_quote_clause_split_keeps_jr_and_drops_an_empty_citation():
    """'Chris Jimerson Jr. makes his first career start at Tulane, and backup Chaston Ditta remains sidelined': the
    Ditta line is his clause only; a second player from an already-quoted report gets no empty '(per ...)'."""
    q = ("North Texas QB Tayven Jackson (upper body) will miss the final two regular-season games; Chris Jimerson Jr. "
         "makes his first career start at Tulane, and backup Chaston Ditta remains sidelined.")
    r = {"player": "Chaston Ditta", "quote": q, "source_name": "Underdog Dynasty", "when": "yesterday"}
    said = set()
    assert router._clip(r, said, ["Chris Jimerson Jr.", "Tayven Jackson"]) == "backup Chaston Ditta remains sidelined.", said
    assert router._per(r, said, []) == "", router._per(r, said, [])


def test_a_source_is_cited_once_so_thin_citations_leaves_it_whole():
    """Two players from one Yahoo / Greenville News report: one citation, and news.thin_citations (a source named
    at most once) has nothing left to cut (it turned the second one into '( / Greenville News, yesterday)')."""
    import news
    q1 = "Clemson QB Christopher Vizzina is out vs Duke with a concussion."
    q2 = "Clemson freshman starting QB Tait Reynolds suffered a season-ending meniscus tear."

    def two(st):
        for pl, q in (("Christopher Vizzina", q1), ("Tait Reynolds", q2)):
            _set_row(st, "Clemson", pl, quote=q, source_name="Yahoo Sports / Greenville News")
    with use(_patched("clem-src", two)):
        out = router.qb_status_text({}, "Who's Clemson's QB Friday? Has he ever played?")
    assert out.count("Greenville News") == 1, out
    assert news.thin_citations(str(out)) == str(out), news.thin_citations(str(out))
    ok_facts(out)


def test_one_point_is_singular():
    p = _patched("one-pt", lambda st: _set_row(st, "Michigan", "Blake Frazier", status="questionable"))
    with use(p):
        out = router.injury_impact_text({}, "Any injuries that matter for Michigan vs UCLA?")
    assert " 1 points" not in out, out


def test_clause_led_by_another_player_isnt_quoted():
    q = ("Tennessee QB George MacIntyre hurt his right knee vs Texas A&M; Heupel said he is working toward being ready "
         "for LSU. Faizon Brandon, who started the first five games before a shoulder injury, would start if "
         "MacIntyre can't.")
    r = {"player": "George MacIntyre", "quote": q, "source_name": "Yahoo Sports", "when": "Monday"}
    out = router._clip(r, set(), ["Faizon Brandon"])
    assert "working toward being ready" in out and "Brandon" not in out, out


def test_half_a_point_in_words():
    with use(built()["path"]):
        out = router.injury_impact_text({}, "Indiana lost a lineman? Does that matter at Washington?")
    assert "0.5" not in out and "half a point" in out, out
    ok_facts(out)


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
