"""
QB answers the week-13 genius exam failed (personnel 0/7), fixed by question TYPE on the frozen fixture:

- A QB with starts who sat the team's last 2+ games while someone else started them is sidelined: never named as
  'the backup' (Nico Iamaleava, out for the season, was UCLA's 'backup'), and a team ask names the man playing.
- A hurt/out/questionable report a QB has since contradicted by starting is dropped (Henderson 'ruled out' the night
  he won his first start).
- 'questionable staff moves' is an adjective, not a status (a CBS recap made the long-out Sayin 'questionable').
- Routing: 'who's playing quarterback for X', 'is X's quarterback playing? who's the backup?', 'with <QB> back under
  center ...' and 'who are the quarterbacks in X-Y' reach the store's QB lane; an en/em dash between team names
  ('Nebraska–Iowa') resolves like a hyphen.

Run:  ./.venv/bin/python3 -I test_qb_sidelined.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import factstore as FS  # noqa: E402
import group_chat  # noqa: E402
import personnel as P  # noqa: E402
import router  # noqa: E402
from test_personnel_store import _patched, built, ok_facts, use  # noqa: E402


def _no_rows(team, player):
    def fn(st):
        for sheet in [st["teams"][team]] + [g for g in st["games"].values() if team in (g["away"], g["home"])]:
            sheet["player_status"] = [r for r in sheet.get("player_status") or [] if r["player"] != player]
    return fn


def _osu():
    room = {x["name"]: x for x in built()["store"]["teams"]["Ohio State"]["qbs"]}
    return room["Julian Sayin"], room["Luke Fahey"]


def test_sidelined_qb_is_never_the_backup():
    sayin, fahey = _osu()
    assert fahey["streak"] >= 2 and sayin["last_start"] < fahey["last_start"], (sayin, fahey)
    with use(_patched("sidelined", _no_rows("Ohio State", "Julian Sayin"))):
        assert "julian sayin" in FS.sidelined("Ohio State")
        out = router.backup_qb_text({}, "Who's Ohio State's backup QB?")
        assert "backup is Julian Sayin" not in out, out
        assert "Sayin hasn't started since" in out and "Fahey" in out, out
        ok_facts(out)


def test_team_ask_names_the_man_playing_and_the_sidelined_starter():
    with use(_patched("sidelined", _no_rows("Ohio State", "Julian Sayin"))):
        out = router.qb_status_text({}, "Who's playing quarterback for Ohio State, and how's he done?")
    assert out.startswith("Luke Fahey (Ohio State)"), out
    assert "Julian Sayin" in out and "hasn't started since" in out, out
    assert "If he can't go, it's Julian Sayin" not in out, out
    ok_facts(out)


def test_asking_about_the_sidelined_qb_says_he_sat_not_healthy():
    with use(_patched("sidelined", _no_rows("Ohio State", "Julian Sayin"))):
        out = router.qb_status_text({}, "Is Julian Sayin playing Saturday?")
    assert "hasn't started since" in out and "Fahey has started the last" in out, out
    assert "has him hurt or out" not in out, out
    ok_facts(out)


def test_benched_starter_who_still_plays_is_the_backup():
    """Tennessee's Faizon Brandon: 4 starts, benched for MacIntyre, still plays in relief = the backup."""
    with use(built()["path"]):
        assert "faizon brandon" not in FS.sidelined("Tennessee")
        out = router.qb_status_text({}, "Who's starting at QB for Tennessee vs LSU?")
    assert "Faizon Brandon" in out, out


def test_report_contradicted_by_a_later_start_is_dropped():
    _, fahey = _osu()
    day_before = (__import__("datetime").date.fromisoformat(fahey["last_start"])
                  - __import__("datetime").timedelta(days=2)).isoformat()

    def fn(st):
        for sheet in [st["teams"]["Ohio State"]]:
            sheet.setdefault("player_status", []).append(
                {"player": "Luke Fahey", "team": "Ohio State", "status": "out", "detail": "", "pos": "QB",
                 "quote": "Sayin was ruled out, and freshman Luke Fahey came in.", "source_name": "Test",
                 "reported_at": day_before + "T23:00:00+00:00", "src": "news"})
    with use(_patched("contradicted", fn)):
        assert not FS.player_status(team="Ohio State", player="Luke Fahey")

    def same_day(st):  # an in-game injury is reported the day he started: it stays
        fn(st)
        st["teams"]["Ohio State"]["player_status"][-1]["reported_at"] = fahey["last_start"] + "T23:00:00+00:00"
    with use(_patched("same-day", same_day)):
        assert FS.player_status(team="Ohio State", player="Luke Fahey")


def test_questionable_as_an_adjective_is_not_a_status():
    assert P.statuses_in("A devastating injury, fourth-quarter collapses and questionable staff moves derailed it") == []
    assert P.statuses_in("a questionable call by the refs") == []
    for c in ("He is questionable for Saturday", "listed as questionable.", "questionable with a hamstring",
              "remains questionable Saturday"):
        assert P.statuses_in(c) == ["questionable"], c


def test_qb_asks_route_to_the_qb_lane():
    with use(built()["path"]):
        for q in ("Who's playing quarterback for Ohio State, and how's he done?",
                  "Is Ohio State's quarterback playing Saturday? Who's the backup?",
                  "With Julian Sayin back under center, Ohio State -10.5 is free money, right?"):
            assert router.classify(q).kind == "qb_status", (q, router.classify(q))
        for q in ("With Iowa playing at home, lock?", "With Fernando Mendoza back, is Indiana a lock?"):
            assert router.classify(q).kind != "qb_status", (q, router.classify(q))


def test_two_team_qb_ask_answers_both_rooms():
    st = built()["store"]
    g = next(g for g in st["games"].values()
             if all((st["teams"].get(t) or {}).get("qbs") for t in (g["away"], g["home"])))
    q = f"Who are the quarterbacks in {g['away']}-{g['home']}?"
    with use(built()["path"]):
        assert router.classify(q).kind == "qb_status", router.classify(q)
        out = router.qb_status_text({}, q)
    assert f"({g['away']})" in out and f"({g['home']})" in out, out


def test_en_dash_between_teams_resolves():
    for raw in ("Who are the quarterbacks in Nebraska–Iowa?", "The Michigan—Ohio State total dropped",
                "Houston −5.5"):
        q = group_chat.strip_trigger(raw, True)
        assert "–" not in q and "—" not in q and "−" not in q, q
    assert FS.teams_named(group_chat.strip_trigger("windy for Louisville–Kentucky?", True)) == \
        ["Louisville", "Kentucky"]
    assert group_chat.strip_trigger("USC can throw it — can UCLA hold up", True) == \
        "USC can throw it — can UCLA hold up"  # a spaced dash is punctuation, left alone


if __name__ == "__main__":
    fails = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("ok  ", name)
            except Exception as e:  # noqa: BLE001
                fails += 1
                print("FAIL", name, repr(e)[:600])
    sys.exit(1 if fails else 0)
