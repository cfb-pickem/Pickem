"""
Overnight lanes (2026-11-18 night, by question TYPE from the genius misses + real traffic):

- lookahead_game: 'is Iowa looking ahead to Nebraska?' / 'Ohio State ... is this a lookahead spot?' about ONE named
  game, answered in code from the store's lookahead verdict (the full brain told a member there was no spot when
  the card said there was). Slate-wide 'any lookahead spots?' keeps lookahead_slate; a compound ATS ask keeps
  ats_profile.

Run:  ./.venv/bin/python3 -I test_overnight_lanes.py
"""

from __future__ import annotations

import os
import re
import sys

# Never pick up the rules watcher's live copy (cache/league_rules.live.md): when it flags a section, rules answers
# turn into a "RULES CHANGED, VERIFY" hedge. Pin the committed digest, as test_rules.py does.
os.environ["PICKY_RULES_LIVE"] = "/private/tmp/picky-test-no-such-dir/league_rules.live.md"
sys.path.insert(0, ".")
import factstore as FS  # noqa: E402
import router  # noqa: E402
import test_big_picture as TB  # noqa: E402  (frozen fixture store)


def _lane(q):
    r = router.classify(q)
    return r.label() if hasattr(r, "label") else (r.lane, r.kind)


def _ans(fn, q, ctx=None):
    TB.use(TB.store_at("main"))
    try:
        return fn(ctx or {}, q)
    finally:
        FS.use(None, None)


# --- lookahead_game ---------------------------------------------------------------------------------

def test_one_game_lookahead_asks_route_to_the_code_lane():
    for q in ("Is Iowa looking ahead to Nebraska?", "Ohio State at Nebraska with Michigan next. Is this a lookahead spot?",
              "is texas tech in a letdown spot at baylor", "Is Ohio State looking past Nebraska to Michigan?"):
        assert router.fact_intent(q) and router.fact_intent(q)[0] == "lookahead_game", (q, router.fact_intent(q))


def test_slate_and_compound_asks_keep_their_lanes():
    assert router.fact_intent("any lookahead spots this week?")[0] == "lookahead_slate"
    fi = router.fact_intent("How's Iowa been ATS, how's Illinois as a dog, and is Iowa looking ahead to Nebraska?")
    assert fi and fi[0] == "ats_profile", fi


def test_lookahead_yes_names_the_spot_and_the_game_behind_it():
    out = _ans(router.lookahead_game_text, "Is Iowa looking ahead to Nebraska?")
    assert out, out
    low = out.lower()
    assert "lookahead spot" in low and "nebraska" in low and "heroes game" in low, out
    assert "no lookahead" not in low, out
    assert "illinois" in low and "ucla" in low, out  # the other side's spot said too
    assert "coin flip" in low, out  # spots alone are about a coin flip (B12)
    assert len(out) <= 600, (len(out), out)


def test_letdown_spot_is_said_as_a_letdown():
    out = _ans(router.lookahead_game_text, "is texas tech in a letdown spot at baylor")
    assert out and "letdown spot" in out.lower() and "oklahoma state" in out.lower(), out


def test_lookahead_lane_steps_aside_without_a_game():
    assert _ans(router.lookahead_game_text, "Is Montana Tech looking ahead?") is None



# --- rules: a whole-number line in a push ask --------------------------------------------------------

def test_push_ask_with_a_whole_number_says_what_the_league_line_would_be():
    """'If Ohio State -3 is the line and they win by exactly 3, do I get a push?': the league moves -3 half a
    point, so the answer says both ways it can go (a 3-point win covers -2.5, misses -3.5)."""
    import league_rules
    out = league_rules.answer("If Ohio State -3 is the line and they win by exactly 3, do I get a push in our league?")
    assert out and "no pushes" in out.lower(), out
    assert "-2.5" in out and "-3.5" in out, out
    assert "covers" in out.lower(), out
    plain = league_rules.answer("What happens on a push?")
    assert "-2.5" not in plain, plain



# --- qb_status reads like a friend texting, ties a shaky starter to the line --------------------------

def _qb(q):
    import test_personnel_store as TP
    with TP.use(TP.built()["path"]):
        out = router.qb_status_text({}, q)
    TP.ok_facts(out)
    return str(out)


def test_team_qb_ask_with_the_starter_out_leads_with_who_plays():
    """'Who's Clemson's QB Friday?': the answer is the man playing, said first, not the backup who's also out."""
    out = _qb("Who's Clemson's QB Friday? Has he ever played?")
    assert out.split(".")[0].count("Trent Pearman") == 1, out


def test_status_quote_isnt_a_pasted_headline_that_repeats_the_name():
    out = _qb("Who's starting at QB for Tennessee vs LSU?")
    assert out.count("George MacIntyre") <= 1, out  # 'George MacIntyre (Tennessee) is ...: Tennessee QB George MacIntyre'
    assert "per " in out, out


def test_shaky_starter_is_tied_to_the_line():
    """A questionable starter: what the market's done since it opened says which way it's leaning."""
    out = _qb("Who's starting at QB for Tennessee vs LSU?")
    assert re.search(r"\bline\b|\bmarket\b", out), out



# --- availability: a multi-player note's label belongs to the players its clauses give it to ----------

LSU_NOTE = {"team": "LSU", "status": "out", "players": ["Sam Leavitt", "Richard Anderson", "Weston Davis"],
            "text": "LSU's Lane Kiffin indicated Sam Leavitt remains the QB despite three INTs vs Texas; DT Richard "
                    "Anderson will miss a fourth straight game, and RT Weston Davis played 73 snaps on a 'really bad' "
                    "ankle.", "source_name": "The Advocate", "date": "2026-11-16"}


def test_player_whose_clause_says_he_plays_doesnt_get_the_notes_out_label():
    """'Sam Leavitt remains the QB ...; DT Richard Anderson will miss ...' (note label 'out'): the out is Anderson's.
    Picky told a member 'LSU has Sam Leavitt out, per The Advocate' (genius pf-final, change_mind)."""
    import personnel as PS
    rows = {r["player"]: r["status"] for r in PS._note_rows(LSU_NOTE, "LSU", 1, "slate_intel")}
    assert rows.get("Sam Leavitt") is None, rows
    assert rows.get("Richard Anderson") == "out", rows


def test_single_player_note_still_takes_its_label():
    import personnel as PS
    note = dict(LSU_NOTE, players=["Sam Leavitt"], text="LSU QB Sam Leavitt (ankle) is day to day.", status="out")
    rows = {r["player"]: r["status"] for r in PS._note_rows(note, "LSU", 1, "slate_intel")}
    assert rows.get("Sam Leavitt"), rows



def test_change_mind_qb_trigger_reads_the_per_player_status_not_the_notes_label():
    import case_ledger
    import test_personnel_store as TP
    with TP.use(TP.built()["path"]):
        trig = case_ledger.qb_triggers([dict(LSU_NOTE, text="LSU QB " + LSU_NOTE["text"])], "Tennessee", "LSU")
    assert not any("Leavitt" in t["text"] for t in trig), trig



# --- pick answers: the hook and a shaky starting QB ---------------------------------------------------

def _led(away, home, line, lean):
    lbl = {away: f"{away} {-line:+g}", home: f"{home} {line:+g}"}
    return {"game": {"id": 1, "away": away, "home": home}, "lean": lean, "labels": lbl, "word": "lean", "line": line,
            "source": "league", "rows": [], "as_of": "",
            "line_fact": {"id": "l", "kind": "line", "subject": home, "value": line, "text": f"{home} {line:+g}",
                          "source": "x", "as_of": ""}}


def test_pick_laying_the_hook_over_3_says_a_field_goal_win_loses():
    import case_ledger
    out = str(case_ledger.pick_answer(_led("Texas Tech", "Baylor", 3.5, "Texas Tech")))
    assert "3-point win" in out and "hook" in out.lower(), out


def test_pick_getting_the_hook_says_a_field_goal_loss_still_covers():
    import case_ledger
    out = str(case_ledger.pick_answer(_led("Texas Tech", "Baylor", 3.5, "Baylor")))
    assert "3-point loss" in out, out


def test_pick_off_a_key_number_says_nothing_about_hooks():
    import case_ledger
    out = str(case_ledger.pick_answer(_led("Texas Tech", "Baylor", 5.0, "Baylor")))
    assert "hook" not in out.lower(), out


def test_pick_with_a_questionable_starting_qb_is_conditional():
    """LSU-Tennessee: MacIntyre questionable (fixture). Taking Tennessee: 'if he sits, I'm off it'."""
    import case_ledger
    import test_personnel_store as TP
    with TP.use(TP.built()["path"]):
        mine = str(case_ledger.pick_answer(_led("LSU", "Tennessee", -1.5, "Tennessee")))
        theirs = str(case_ledger.pick_answer(_led("LSU", "Tennessee", -1.5, "LSU")))
    assert "MacIntyre" in mine and "questionable" in mine and "off it" in mine, mine
    assert "MacIntyre" in theirs and "more" in theirs, theirs



# --- rest_game: 'who's rested in Memphis-Navy?' ------------------------------------------------------

def test_rest_asks_route_to_the_rest_lane():
    for q in ("Who's rested in Memphis-Navy?", "Is Navy coming off a bye?", "Does Memphis have a rest advantage over Navy?",
              "Memphis on a short week vs Navy?"):
        fi = router.fact_intent(q)
        assert fi and fi[0] == "rest_game", (q, fi)


def test_rest_answer_says_days_and_last_game_for_both_and_never_next_weeks_opponent():
    out = _ans(router.rest_game_text, "Who's rested in Memphis-Navy?")
    assert out, out
    low = str(out).lower()
    assert "navy" in low and "14 days" in low and "bye" in low and "temple" in low, out
    assert "8 days" in low and "south florida" in low, out
    assert "charlotte" not in low, out  # next week's opponent is not this week's game
    assert FS.unsupported(out, out.facts_used) == [], FS.unsupported(out, out.facts_used)



# --- interim: count from the change date in the note, not the day the note was written ---------------

class _Facts:
    def teams_named(self, q):
        return ["Baylor"]

    def coaching(self, team):
        return {"text": "Baylor fired Dave Aranda on Oct 24-25, 2026; defensive coordinator Joe Klanderman is interim "
                        "for the rest of the season.", "date": "2026-11-01", "source_name": "cfbeveryday.com"}

    def results(self, team):
        R = lambda d, o, a, b, c: {"date": d, "opp": o, "pts": (a, b), "cover": c}  # noqa: E731
        return [R("2026-10-24", "Kansas", 24, 25, 0.5), R("2026-10-30", "UCF", 41, 9, 29.5),
                R("2026-11-07", "Iowa State", 42, 31, 1), R("2026-11-15", "BYU", 7, 28, -12.5)]


def test_interim_record_counts_from_the_change_date_in_the_note():
    import situational
    out = situational.interim_text({}, "Baylor's playing for an interim, does it matter?", facts=_Facts())
    assert "2-1 straight up" in out and "UCF" in out and "Kansas" not in out.split("Since", 1)[1], out



def test_pick_hook_note_not_repeated_when_a_reason_already_says_it():
    import case_ledger
    led = _led("Northwestern", "Minnesota", 2.5, "Northwestern")
    led["rows"] = [{"side": "Northwestern", "tier": "factor", "raw": "line value", "facts_used": [],
                    "text": "half a point of real value on Northwestern, and it's on the good side of the key number 3"}]
    out = str(case_ledger.pick_answer(led))
    assert out.count(" 3") <= 2 and "good side of 3" not in out, out


def test_hot_seat_carousel_answer_fits_one_text():
    import big_picture
    out = _ans(big_picture.coach_text, "Any coaches on the hot seat this week, and does it matter for the picks?")
    assert out and len(out) <= 700, (len(str(out)), out)
    assert "hot-seat" in str(out).lower() or "hot seat" in str(out).lower(), out



# --- value: 'top spot and why' gets a football reason, not just the half point ------------------------

def test_top_value_spot_comes_with_a_football_reason():
    import datetime as dt
    from unittest.mock import patch
    import chat
    import game_cards
    import test_router as TR
    ctx = TR._wk11("TG")
    for i, g in enumerate(ctx["games_this_week"]):
        g.update(winner=None, status="scheduled", start=f"2099-11-21T{11 + i}:00:00")
    cards = TR._cards(ctx, {g["id"]: (g["home"], f"{g['home']} {g['line']:+g}", "lean") for g in ctx["games_this_week"]})
    g0 = ctx["games_this_week"][0]
    c0 = cards[g0["id"]]
    c0["value"] = {"side": g0["home"], "points": 0.5, "label": f"{g0['home']} {g0['line']:+g}", "key": ""}
    c0["market"] = {"home_now": float(g0["line"]) - 0.5, "src": "DraftKings"}
    c0["reasons"] = ["line value: gap", f"{g0['home']} is 5-1 ATS at home"]
    for gid, c in cards.items():
        if gid != g0["id"]:
            c["value"] = None
    with patch.object(game_cards, "load_card", lambda w, gid, base=None: cards.get(gid)), \
            patch.object(chat, "_now_ct_naive", lambda: dt.datetime(2026, 11, 17, 9, 0)):
        out = router.fact_answer("value", ctx, "Where's the value this week? Give me your top spot and why.")
    assert out and "5-1 ATS at home" in out, out



def test_team_qb_answer_doesnt_say_things_twice():
    """Clemson: 'Also out for the rest of the regular season: Tait Reynolds. Clemson freshman starting QB Tait
    Reynolds is out for the season' and 'Looks like it's Trent Pearman ... Next up: Trent Pearman' (voice 2)."""
    out = _qb("Who's Clemson's QB Friday? Has he ever played?")
    assert out.count("Tait Reynolds") == 1, out
    assert out.count("Trent Pearman") == 1, out


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
