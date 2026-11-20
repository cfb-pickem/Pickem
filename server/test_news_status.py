"""
News-only availability (nightly 2026-11-20): when the Opus booster notes age out, the availability list runs on
news + ESPN alone, and the news reader missed real reports in the cache. Genius 11/20 vs 11/19: personnel 4 -> 1/7,
lines 8 -> 4/12, every miss a player the news had: Sayin out ("won't have Julian Sayin back this weekend", "He
remains ... listed as out"), Vizzina out ("is also out with a concussion"), Brunner out for the season (the tagger
left `players` empty), Jimerson starting for North Texas ("takes over", "first career start") while Picky named
another QB, Sellers not on the SEC availability report (= expected to play).

Run:  ./.venv/bin/python3 -I test_news_status.py
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import personnel as P  # noqa: E402

NOW = dt.datetime(2026, 11, 20, 4, 0, tzinfo=dt.timezone(dt.timedelta(hours=-6))).timestamp()


def news(title, summary, players, teams, date, label="reported", status=None):
    return {"title": title, "summary": summary, "players": players, "teams": teams, "date": date, "label": label,
            "status": status, "source": "Some Outlet", "link": f"https://news.example/{abs(hash(title))}",
            "kind": "injury"}


def _by(rows):
    return {r["player"]: r for r in rows}


def test_wont_have_back_and_pronoun_listed_out():
    it = news("Ohio State Gets Encouraging Julian Sayin Injury Update From Ryan Day",
              "The Ohio State Buckeyes won't have Julian Sayin back this weekend. He remains on the official Big Ten "
              "availability report listed as out due to the injury to his right (throwing) arm.",
              ["Julian Sayin"], ["Ohio State"], "2026-11-19T20:51:27+00:00")
    r = _by(P.normalize(news_items=[it], now=NOW))
    assert r["Julian Sayin"]["status"] == "out", r


def test_pronoun_clause_alone_counts_for_the_one_named_player():
    it = news("Update on Julian Sayin", "Julian Sayin threw on Tuesday. He remains listed as out.",
              ["Julian Sayin"], ["Ohio State"], "2026-11-19T20:51:27+00:00")
    assert _by(P.normalize(news_items=[it], now=NOW))["Julian Sayin"]["status"] == "out"
    two = news("Notes", "Julian Sayin threw on Tuesday and Luke Fahey ran the offense. He remains listed as out.",
               ["Julian Sayin", "Luke Fahey"], ["Ohio State"], "2026-11-19T20:51:27+00:00")
    assert P.normalize(news_items=[two], now=NOW) == [], "a 'He' after two names stays unattributed"


def test_also_out_with_and_has_a_concussion():
    a = news("Clemson quarterback Tait Reynolds out for season following meniscus tear",
             "Senior quarterback Christopher Vizzina is also out with a concussion, leaving the Tigers with few good "
             "options.", ["Christopher Vizzina", "Tait Reynolds"], ["Clemson"], "2026-11-15T21:26:57+00:00")
    r = _by(P.normalize(news_items=[a], now=NOW))
    assert r["Christopher Vizzina"]["status"] == "out" and r["Tait Reynolds"]["status"] == "season", r
    assert P.status_of("backup Christopher Vizzina has a concussion.") == "questionable"


def test_untagged_player_found_by_position_word():
    it = news("Indiana starting guard Brunner out for season with ACL injury",
              "Indiana starting guard Joe Brunner will miss the rest of the season after sustaining an ACL injury in "
              "Thursday's practice, coach Curt Cignetti said on Monday.", [], ["Indiana"],
              "2026-11-16T21:13:55+00:00", label="confirmed", status="out")
    r = _by(P.normalize(news_items=[it], now=NOW))
    assert r["Joe Brunner"]["status"] == "season" and r["Joe Brunner"]["team"] == "Indiana", r
    assert r["Joe Brunner"]["pos"] == "OL", r
    assert "Curt Cignetti" not in r, "a coach is not a player"


def test_untagged_names_need_one_team_and_an_injury_story():
    two = news("Starting guard Joe Brunner out for season", "", [], ["Indiana", "Washington"],
               "2026-11-16T21:13:55+00:00")
    assert P.normalize(news_items=[two], now=NOW) == [], "two teams, no roster: never guess"
    feature = dict(news("Starting guard Joe Brunner out for season", "", [], ["Indiana"],
                        "2026-11-16T21:13:55+00:00"), kind=None)
    assert P.normalize(news_items=[feature], now=NOW) == [], "only injury-tagged stories get the name fallback"


def test_takes_over_and_first_career_start_mark_the_new_starter():
    it = news("North Texas QB Tayven Jackson out for regular season; Chris Jimerson Jr. takes over",
              "Jackson is out for the next two games with an upper body injury. Jimerson will make first career "
              "start vs. Tulane.", ["Chris Jimerson Jr.", "Tayven Jackson"], ["North Texas", "Tulane"],
              "2026-11-17T18:00:00+00:00")
    r = _by(P.normalize(news_items=[it], now=NOW, name_team={"tayven jackson": "North Texas",
                                                              "chris jimerson jr.": "North Texas"}))
    assert r["Tayven Jackson"]["status"] == "season", r
    assert r["Chris Jimerson Jr."]["status"] == "available" and r["Chris Jimerson Jr."]["detail"] == "starting", r


def test_backup_prefers_the_reported_new_starter():
    room = [{"name": "Tayven Jackson", "starts": 10, "line": {"att": 400}},
            {"name": "Chaston Ditta", "starts": 0, "line": {"att": 30}},
            {"name": "Chris Jimerson Jr.", "starts": 0, "line": {"att": 15}}]
    assert P.backup_for(room, "Tayven Jackson")["name"] == "Chaston Ditta"
    assert P.backup_for(room, "Tayven Jackson", starting={P._norm("Chris Jimerson Jr.")})["name"] == "Chris Jimerson Jr."
    assert P.backup_for(room, "Tayven Jackson", {P._norm("Chris Jimerson Jr.")}, starting={P._norm("Chris Jimerson Jr.")})["name"] \
        == "Chaston Ditta", "an excluded (out) player is never next up"


def test_not_on_the_availability_report_means_expected_to_play():
    it = news("South Carolina QB LaNorris Sellers not listed on Wednesday availability report ahead of Georgia game",
              "South Carolina QB LaNorris Sellers was not listed on Wednesday's availability report released by the "
              "SEC.", ["LaNorris Sellers"], ["South Carolina"], "2026-11-18T23:00:00+00:00")
    r = _by(P.normalize(news_items=[it], now=NOW))
    assert r["LaNorris Sellers"]["status"] == "available", r


def test_old_phrasings_unchanged():
    assert P.status_of("had a great practice") is None
    assert P.status_of("He takes over play-calling duties") is None, "'takes over' alone isn't a QB start"
    assert P.status_of("is listed on the availability report as questionable") == "questionable"


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
