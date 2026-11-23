"""
League-rules tests for Picky.

Run:  ./.venv/bin/python3 -I test_rules.py          (offline, fast)
      ./.venv/bin/python3 -I test_rules.py --live   (also asks the local model)

Pins: league_rules.md tracks the "League Rules" block on cfb-genius.html
(hash + tiebreaker order), rules are injected only for rules questions and
never change the system prompt (Ollama prompt cache), and slurs are scrubbed
from the model's output even when split across stream chunks.
--live asks rules questions in-process as Chuck P (service-key reads, auth
stubbed) and checks the answers for the facts the rules state.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import os
import re
import sys
from pathlib import Path

# Same model settings as the com.pickem.chat plist (gemma4:12b-it-qat is the
# only model on the box).
os.environ.setdefault("PICKEM_CHAT_MODEL", "gemma4:12b-it-qat")
os.environ.setdefault("PICKY_OLLAMA_THINK", "false")
# Never pick up the rules watcher's live copy from this checkout: the tests
# pin the committed digest (the flag tests write their own temp copy).
os.environ["PICKY_RULES_LIVE"] = "/private/tmp/picky-test-no-such-dir/league_rules.live.md"
sys.path.insert(0, ".")
import chat  # noqa: E402
import league_rules as L  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
RULES = Path(__file__).with_name("league_rules.md").read_text()


def _site_block() -> str:
    s = (ROOT / "cfb-genius.html").read_text()
    return s[s.index("<!-- Rules -->"):s.index("<!-- Champion -->")]


def test_rules_file_matches_site():
    want = re.search(r"source-hash: ([0-9a-f]{16})", RULES).group(1)
    got = hashlib.sha256(_site_block().encode()).hexdigest()[:16]
    assert got == want, (f"cfb-genius.html rules changed ({got} != {want}): update "
                         "server/league_rules.md to match, then its source-hash")


def test_tiebreaker_order_matches_site():
    lists = re.findall(r"<ol[^>]*>(.*?)</ol>", _site_block(), re.S)
    assert len(lists) == 2, "expected regular + playoff tiebreaker lists"
    for site_ol, label in zip(lists, ("Regular season", "Playoff")):
        items = [html.unescape(re.sub(r"<[^>]+>", "", li)).strip()
                 for li in re.findall(r"<li>(.*?)</li>", site_ol, re.S)]
        after = RULES.split(f"{label} tiebreakers, in order:\n", 1)[1].splitlines()
        ours = []
        for ln in after:
            m = re.match(r"\d\) (.+)", ln)
            if not m:
                break
            ours.append(m.group(1))
        assert len(ours) == len(items), (label, ours, items)
        for a, b in zip(ours, items):
            key = b.split()[0].lower().replace("chips", "chip")
            assert key in a.lower(), (label, a, b)


def test_wants_rules():
    yes = ["How does scoring work?", "What are the tiebreakers?", "How does the jackpot work",
           "When's the deadline?", "What happens with a push?", "What does second place win?",
           "When do the lines lock?", "What if a game gets postponed?", "How do chips work"]
    no = ["Roast my picks", "Who's winning the pool?", "Explain the Utah Iowa spread",
          "Who did TG pick?", "Is Iowa's QB healthy?"]
    for q in yes:
        assert chat.wants_rules(q), q
    for q in no:
        assert not chat.wants_rules(q), q


def _ctx():
    return {"you": {"team_name": "Chuck P"}, "standings_ytd": [], "games_this_week": [
        {"id": 1, "away": "Utah", "home": "Iowa", "start": "2030-09-07T11:00:00"}],
        "season": 2030, "current_week": 2}


def test_rules_injected_only_when_asked():
    ctx = _ctx()
    t = chat._user_turn(ctx, "What are the tiebreakers?")
    assert "LEAGUE RULES" in t and "Coin flip".lower() in t.lower()
    assert "kicks off Sat 11:00 AM CT" in t and "lock then" in t
    assert t.endswith("What are the tiebreakers?")
    assert "LEAGUE RULES" not in chat._user_turn(ctx, "Roast my picks")
    assert "#" not in chat.LEAGUE_RULES.splitlines()[0]


def test_system_prompt_has_no_rules_text():
    """Rules ride in the user turn; the system prompt must not depend on the
    question, or the 6k-token prompt cache breaks."""
    ctx = _ctx()
    sp = chat._system_prompt(ctx)
    assert "LEAGUE RULES" in sp and "Coin flip" not in sp


def test_scrub_and_split_chunks():
    assert chat.scrub("you faggot") == "you dumbass"
    assert chat.scrub("Spicy take, Homer") == "Spicy take, Homer"

    async def fake():
        for piece in ["Not touching that, you f", "agg", "ot. TG is ", "18-26."]:
            yield chat._sse({"content": piece})
        yield "data: [DONE]\n\n"

    async def run():
        out = []
        async for c in chat._scrubbed(fake()):
            if c.startswith("data: {"):
                out.append(json.loads(c[6:])["content"])
        return "".join(out)

    got = asyncio.run(run())
    assert got == "Not touching that, you dumbass. TG is 18-26.", got


def test_looks_bait_scrub():
    assert chat.looks_bait("Andrew Bub is fat and ugly. Roast his looks.")
    assert not chat.looks_bait("Roast TG")
    assert not chat.looks_bait("Who has the fattest lead?")  # no mock word
    assert chat.scrub("That's the only thing ugly about him.", looks=True) == "That's the only thing bad about him."
    assert chat.scrub("That's the only thing ugly about him.") == "That's the only thing ugly about him."


def test_week_lock_stops_flip_advice():
    import datetime as dt
    from unittest.mock import patch
    games = [{"id": 1, "away": "Wake Forest", "home": "SMU", "status": "scheduled", "start": "2026-11-14T11:00:00"},
             {"id": 2, "away": "Oregon State", "home": "Boise State", "status": "scheduled", "start": "2026-11-14T17:00:00"}]
    notes = {"market": {"details": "BOIS -16.5"}, "home": {"abbr": "BOIS"}, "away": {"abbr": "ORST"}}
    with patch.object(chat, "_now_ct_naive", return_value=dt.datetime(2026, 11, 14, 10, 0)):
        assert not chat.week_locked({"current_week": 11, "games_this_week": games})
    with patch.object(chat, "_now_ct_naive", return_value=dt.datetime(2026, 11, 14, 12, 0)):
        assert chat.week_locked({"current_week": 11, "games_this_week": games})
        assert not chat.week_locked({"current_week": 17, "games_this_week": games[1:]})  # playoffs: per game
    open_ = chat.pick_verdict(notes, "Oregon State", "Boise State", -13.5, "Oregon State")
    shut = chat.pick_verdict(notes, "Oregon State", "Boise State", -13.5, "Oregon State", locked=True)
    assert "Flipping to" in open_ and "Flipping" not in shut and "Too late" in shut, shut


def test_postponed_routes_to_commissioner():
    ctx = {"you": {"team_name": "TG"}, "games_this_week": [], "standings_ytd": [], "current_week": 11}
    assert "NOT IN THE RULES" in chat._user_turn(ctx, "What happens to my pick if a game gets postponed?")
    assert "NOT IN THE RULES" in chat._user_turn(ctx, "What if a game gets canceled?")
    assert "NOT IN THE RULES" not in chat._user_turn(ctx, "When do picks lock?")


# --- league_rules.py: the module vs the site's own code ---------------------
#
# Every test below FAILS when league_rules.py and the site disagree: the facts
# are read out of the SQL/JS/page, cross-checked against each other, the site's
# JS is run on fixtures next to the Python ports, and the committed digest has
# to be exactly what the code generates today.

import datetime as _dt  # noqa: E402
import tempfile  # noqa: E402

_FACTS, _ERRS = L.extract()


class _Overlay(L.Repo):
    """The working tree with some files replaced or added (a pretend commit)."""

    def __init__(self, over: dict[str, str]):
        super().__init__()
        self.over = over

    def read(self, rel):
        return self.over[rel] if rel in self.over else super().read(rel)

    def ls(self, d):
        extra = [k for k in self.over if k.startswith(d + "/") and "/" not in k[len(d) + 1:]]
        return sorted(set(super().ls(d)) | set(extra))


def _new_fn(name: str, old: str, new: str) -> dict:
    """A later migration redefining one SQL function with `old` -> `new`."""
    body = L.sql_functions(L.Repo())[name]
    assert old in body, (name, old)
    return {"supabase/migrations/29991231000000_test_change.sql": body.replace(old, new)}


def test_extraction_clean():
    assert not _ERRS, f"league_rules can't read the site any more: {_ERRS}"


def test_digest_is_what_the_code_generates():
    """league_rules.md must be exactly league_rules.py's output for the code in
    this checkout (rules, facts header and fingerprints). Stale = regenerate:
    cd server && ./.venv/bin/python3 -I league_rules.py --write"""
    want = L.generate_md(_FACTS, L.fingerprints(L.repo_sources(L.Repo())), L.page_hash(L.Repo()))
    assert L.MD.read_text() == want, "league_rules.md is stale: run league_rules.py --write and commit it"


def test_numbers_agree_across_the_site():
    f = _FACTS
    assert f["last_regular_week"] == f["last_regular_week_js"] == f["stats_consts"][1] == 15
    assert f["playoffs"]["regular_weeks"] == [f["first_week"], f["last_regular_week"]]
    assert f["playoffs"]["field_only_after_week"] == f["last_regular_week"]
    assert f["playoff_spots"] == f["playoffs"]["spots"] == f["playoff_spots_page"] == f["cut_index_js"] + 1 == 4
    assert f["games_per_week"] == f["stats_consts"][0] == 4
    assert f["playoffs"]["missing_chips"] == f["pit"]["start_chips"] == 1000


def test_cover_rule_and_pushes():
    """set_winner_on_final's own condition, evaluated, vs the Python port; the
    exact-number case goes to Away (pushes can't happen on half-point lines)."""
    assert L.check_sql_semantics(_FACTS) == []
    c = _FACTS["cover"]
    assert (c["true"], c["else"]) == ("Home", "Away")
    assert L.cover_winner("H", "A", 21, 21, 0.0) == "A"          # dead even -> away
    assert L.cover_winner("H", "A", 24, 17, -7.5) == "A"         # -7.5 home fav wins by 7: away covers
    assert L.cover_winner("H", "A", 25, 17, -7.5) == "H"
    assert L.eval_cover_cond(c["cond"], 21, 21, 0.0) is False
    assert L.half_point(-7.0, coin=0.1) == -6.5 and L.half_point(-7.0, coin=0.9) == -7.5
    assert L.half_point(-7.25) == -7.5 and L.half_point(3.75) == 3.5 and L.half_point(-7.0, old=-7.5) == -7.5
    # a mutated rule (>=) is caught by the semantic check
    bad = dict(_FACTS, cover=dict(c, cond=c["cond"].replace(">", ">=")))
    assert L.check_sql_semantics(bad), "check_sql_semantics didn't notice a changed cover rule"


def test_site_js_agrees_with_python():
    """Runs js/kickoff.js, js/seasonHistory.js and js/coverOdds.js under node
    on fixtures and compares them with the Python ports."""
    got = L.run_conformance(L.Repo(), _FACTS)
    if got == ["node not installed: conformance not run"]:
        print("    (node missing: JS conformance skipped)")
        return
    assert got == [], got
    real = L.is_point
    try:  # the comparison has teeth: a Python port that forgets the jackpot fails
        L.is_point = lambda pick, winner, jackpot=False, tiebreaker=False: real(pick, winner, False, tiebreaker)
        assert L.run_conformance(L.Repo(), _FACTS), "conformance didn't catch a wrong is_point"
    finally:
        L.is_point = real


def test_locks_match_the_sql():
    """line_lock_at(): 4 days before the game's Saturday, 11:00 CT (noon ET);
    picks_open_for_game(): weeks 0-15 lock together, playoffs per game."""
    tue = _dt.datetime(2026, 11, 10, 11, 0)
    for d in (12, 13, 14, 15, 16):  # Thu, Fri, Sat, Sun, Mon all belong to the Nov 14 week
        assert L.line_lock_at(_dt.datetime(2026, 11, d, 19, 0), _FACTS) == tue, d
    assert L.line_lock_at(_dt.datetime(2026, 11, 17, 19, 0), _FACTS) == _dt.datetime(2026, 11, 17, 11, 0)
    assert _FACTS["line_lock_page"] == "Tuesday 11:00 AM CT"
    wk = [{"week": 12, "picked": True, "start": "2026-11-20T19:00:00"},
          {"week": 12, "picked": True, "start": "2026-11-21T11:00:00"},
          {"week": 12, "tiebreaker": True, "start": "2026-11-19T18:00:00"}]
    assert L.picks_open(wk[1], wk, _dt.datetime(2026, 11, 19, 17, 59), _FACTS)
    assert not L.picks_open(wk[1], wk, _dt.datetime(2026, 11, 19, 18, 0), _FACTS)  # a tiebreaker kicked off
    po = [{"week": 17, "picked": True, "start": "2026-12-19T11:00:00"},
          {"week": 17, "picked": True, "start": "2026-12-20T19:00:00"}]
    assert L.picks_open(po[1], po, _dt.datetime(2026, 12, 19, 12, 0), _FACTS)      # playoffs: per game
    assert not L.picks_open({"week": 12, "start": None}, wk, _dt.datetime(2026, 11, 1), _FACTS)
    assert L.calc_cfb_week(_dt.datetime(2026, 9, 5, 11)) == 1 and L.calc_cfb_week(_dt.datetime(2026, 8, 29, 11)) == 0
    assert L.calc_cfb_week(_dt.datetime(2027, 1, 9, 19)) > 15  # January belongs to 2026


def test_playoff_rules_from_the_sql():
    po = _FACTS["playoffs"]
    assert po["cascade"] == ["conf", "army_navy", "chips", "celebration", "last8"]
    assert L.cascade_mismatch(po, _FACTS["tiebreakers"]["regular"]) is None
    assert po["settle_hours"] == 12 and po["tick_minutes"] == 10 and po["last_n"] == 8
    assert po["coin_flip_held"] and po["force_refuses_coin_flip"] and po["hand_seeds_win"]
    assert po["field_only_once_seeded"] and po["field_only_seeds_pass"] and po["field_only_commish_exempt"]
    assert po["field_only_message"] == "Only the four playoff teams can make playoff picks."
    assert set(po["admin_actions"]) == {"preview", "pause", "resume", "force", "undo"}
    assert po["excludes_exhibition"] and po["jackpot_counts"] and po["switch_is_seed_rows"]
    assert L.cut_line([30, 28, 27, 27, 27, 10]) == 27
    assert L.playoff_status({"a": 30, "b": 28, "c": 27, "d": 27, "e": 27}) == {
        "a": "in", "b": "in", "c": "tied at the cut", "d": "tied at the cut", "e": "tied at the cut"}


def test_drift_in_the_code_is_caught():
    """Pretend commits that change a rule in ONE place: the cross-checks must
    refuse to state it (error on the right section)."""
    cases = [
        (_new_fn("playoff_compute", "array['conf','army_navy','chips'", "array['army_navy','conf','chips'"), "TIEBREAKERS"),
        (_new_fn("playoff_compute", "v_slots := 4 - v_above", "v_slots := 6 - v_above"), "STANDINGS AND PLAYOFFS"),
        (_new_fn("playoff_pick_blocked", "g.week > 15", "g.week > 14"), "STANDINGS AND PLAYOFFS"),
        (_new_fn("line_lock_at", "time '11:00'", "time '12:00'"), "FORMAT"),
        ({"stats.html": L.Repo().read("stats.html").replace("LAST_REGULAR_WEEK = 15", "LAST_REGULAR_WEEK = 14")},
         "STANDINGS AND PLAYOFFS"),
        ({"cfb-genius.html": L.Repo().read("cfb-genius.html").replace("<li>Army v Navy</li>", "<li>Coin toss</li>")},
         "TIEBREAKERS"),
    ]
    for over, sec in cases:
        _, errs = L.extract(_Overlay(over))
        assert sec in errs, (sec, list(over), errs)


def _flag_env(td):
    os.environ["PICKY_RULES_STATE"] = str(Path(td) / "state.json")
    os.environ["PICKY_RULES_LIVE"] = str(Path(td) / "live.md")


def test_check_adapts_and_hedges_without_opus():
    """The watcher (zero maintenance, phase 8: no Opus on the schedule): no change = nothing flagged; a rules
    change the generator models (the regenerated text moves with the code) = stated from code, no flag; a
    change the generator can't see (same text out) = that section flagged, Picky hedges; an Opus stand-in
    verdict still works when one is given."""
    old_env = {k: os.environ.get(k) for k in ("PICKY_RULES_STATE", "PICKY_RULES_LIVE", "PICKY_NO_ANTHROPIC")}
    try:
        with tempfile.TemporaryDirectory() as td:
            _flag_env(td)
            rep = L.check(L.Repo(), use_live=False, use_opus=False)
            assert rep["flagged"] == {} and rep["changed"] == [], rep
            assert L.flagged_sections() == []
            os.environ["PICKY_NO_ANTHROPIC"] = "1"  # kill switch: picky_claude.guard() raises
            unseen = _new_fn("bj_deal", "least(25, chips)", "least(25, chips) /* same rule, new comment */")
            rep = L.check(_Overlay(unseen), use_live=False, use_opus=True)
            assert "THE PIT" in rep["flagged"] and "opus unavailable" in rep["opus"], rep
            assert L.flagged_sections() == ["THE PIT"], L.flagged_sections()
            assert L.FLAG in L.section("THE PIT")
            assert "half point" in L.section("FORMAT")  # untouched sections stay stated
            rep = L.check(_Overlay(unseen), use_live=False)  # still unseen next tick: still flagged
            assert "THE PIT" in rep["flagged"], rep
            change = _new_fn("bj_deal", "least(25, chips)", "least(50, chips)")
            rep = L.check(_Overlay(change), use_live=False, use_opus=True)
            assert rep["flagged"] == {}, rep  # modeled change: the code states the new rule
            assert "Bets 50-500" in L.section("THE PIT") and L.FLAG not in L.section("THE PIT"), L.section("THE PIT")
            rep = L.check(_Overlay(change), use_live=False, use_opus=False)
            assert rep["flagged"] == {} and rep["changed"] == [], rep
            # a verifier (Opus stand-in) still gets the last word when there is one
            ok = lambda secs, gen, src, changed: {"sections": {s: {"verdict": "ok"} for s in secs}}  # noqa: E731
            rep = L.check(_Overlay(unseen), use_live=False, use_opus=True, verifier=ok)
            assert rep["flagged"] == {}, rep
    finally:
        for k, v in old_env.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        L._cache["key"] = None


def test_watcher_can_import_picky_claude_under_dash_I():
    """com.pickem.rules-watch runs `python3 -I league_rules.py --check` from another cwd: the Opus review's
    lazy `import picky_claude` must resolve (it failed with ModuleNotFoundError on 2026-11-23)."""
    import subprocess
    code = ("import runpy, sys; runpy.run_path(sys.argv[1], run_name='lr'); "
            "import picky_claude; print(picky_claude.__file__)")
    p = subprocess.run([sys.executable, "-I", "-c", code, str(Path(L.__file__).resolve())],
                       cwd="/", capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stderr[-400:]


def test_flagged_sections_make_picky_hedge():
    old = os.environ.get("PICKY_RULES_LIVE")
    try:
        with tempfile.TemporaryDirectory() as td:
            _flag_env(td)
            text = L.generate_md(_FACTS, {}, "x", flags={"FORMAT": "test", "PAYOUTS": "test", "TIEBREAKERS": "test"})
            Path(os.environ["PICKY_RULES_LIVE"]).write_text(text)
            assert L.flagged_sections() == ["FORMAT", "PAYOUTS", "TIEBREAKERS"]
            for q in ("What happens on a push?", "When do the lines lock?", "How do tiebreakers work?"):
                a = L.answer(q)
                assert a.startswith(L.FLAG) and "commissioner" in a, (q, a)
                r = chat.rules_for(q)
                assert L.FLAG in r and "half point" not in r and "conference championship" not in r.lower(), (q, r)
            assert chat.payout_text(14).startswith(L.FLAG) and "$25" not in chat.payout_text(14)
            assert not L.answer("When do picks lock?").startswith(L.FLAG)  # other sections still answered
            t = chat._user_turn(_ctx(), "What happens on a push?")
            assert L.FLAG in t and "a push can't happen" not in t
    finally:
        os.environ.pop("PICKY_RULES_STATE", None)
        os.environ["PICKY_RULES_LIVE"] = old
        L._cache["key"] = None


# Typical questions -> the computed answer has the facts (from the code).
ANSWER_CASES = [
    ("When do picks lock?", [r"first game", r"(regular season|week)", r"own kickoff"]),
    ("What's the pick deadline?", [r"first game"]),
    ("How are ties in the final season standings broken?", [r"conference championship", r"army"]),
    ("How do tiebreakers work?", [r"1\) conference championship", r"2\) army", r"chips", r"celebration", r"last 8", r"coin flip"]),
    ("What happens on a push?", [r"no pushes", r"half point", r"away team"]),
    ("Can I pick the playoff games?", [r"only if your team made the playoffs", r"4 seeds", r"database"]),
    ("Can eliminated teams still pick in the playoffs?", [r"only if", r"watch every game"]),
    ("Who makes the playoffs?", [r"top 4", r"weeks 0-15", r"tie for 4th", r"12 hours"]),
    ("How do the league playoffs work?", [r"top 4", r"scores reset"]),
    ("When do the lines lock?", [r"tuesday 11:00 am ct", r"noon eastern"]),
    ("How are playoff ties broken?", [r"army-navy \(if unused\)", r"regular season points"]),
    ("What does second place win?", [r"\$75", r"buy-in back \+ \$25"]),
    ("I forgot to make my picks, what happens?", [r"no point", r"commissioner"]),
    ("What if a game gets postponed?", [r"don't cover", r"commissioner"]),
    ("How does scoring work?", [r"1 point", r"covers", r"tiebreaker games never count"]),
    ("What can the commissioner do?", [r"any team's pick", r"playoff seeds"]),
]


def test_answers_for_typical_questions():
    for q, pats in ANSWER_CASES:
        a = L.answer(q, facts=_FACTS, flagged=[])
        assert a, q
        miss = [p for p in pats if not re.search(p, a, re.I)]
        assert not miss, (q, miss, a)
    for q in ("Roast TG", "Who's winning the pool?", "Who do you like in Utah Iowa?", "Is Iowa's QB healthy?"):
        assert L.answer(q, facts=_FACTS, flagged=[]) is None, q
    assert "$1,050 pot" in L.answer("What's the payout?", n_teams=14, facts=_FACTS, flagged=[])


def test_brain_brief_and_race_math_defer_to_the_rules():
    """Text Picky's understand step gets the computed rule (its brief used to
    say 'the rules don't cover it'), and playoff_race's exact-math shortcut
    doesn't swallow 'can I pick the playoff games?'."""
    import picky_brain
    import playoff_race
    rule = L.answer("What happens on a push?")
    u = picky_brain.understand_user("What happens on a push?", "TG", "", "", rules=rule)
    assert "LEAGUE RULE" in u and "half point" in u and u.rstrip().endswith("NEWEST TEXT: What happens on a push?")
    assert "LEAGUE RULE" not in picky_brain.understand_user("Roast TG", "TG", "", "")
    assert not playoff_race.is_need_question("Can I still pick the playoff games if I don't make the playoffs?")
    assert playoff_race.is_need_question("What do I need to make the playoffs?")


def test_rules_answer_rides_in_the_user_turn():
    t = chat._user_turn(_ctx(), "Can I pick the playoff games?")
    assert "RULES ANSWER" in t and "Only if your team made the playoffs" in t
    assert "RULES ANSWER" not in chat._user_turn(_ctx(), "Roast my picks")


# --- site audit #7: season standings stop at week 15; playoff board ---------

def _audit_data():
    teams = [{"team_id": i, "team_name": n} for i, n in enumerate(["A", "B", "C", "D", "E"], 1)]
    games = [{"GameId": 1, "week": 14, "winner": "X", "tiebreaker": False},
             {"GameId": 2, "week": 15, "winner": "X", "tiebreaker": False},
             {"GameId": 3, "week": 17, "winner": "Y", "tiebreaker": False},
             {"GameId": 4, "week": 17, "winner": "Y", "tiebreaker": True},
             {"GameId": 5, "week": 18, "winner": "Z", "tiebreaker": False}]
    picks = [{"team_id": 1, "game_id": 1, "pick": "X"}, {"team_id": 1, "game_id": 2, "pick": "X"},
             {"team_id": 2, "game_id": 1, "pick": "X"}, {"team_id": 2, "game_id": 2, "pick": "Q"},
             {"team_id": 1, "game_id": 3, "pick": "Q"}, {"team_id": 2, "game_id": 3, "pick": "Y"},
             {"team_id": 2, "game_id": 4, "pick": "Y"}, {"team_id": 2, "game_id": 5, "pick": "Z"},
             {"team_id": 5, "game_id": 3, "pick": "Y"}, {"team_id": 3, "game_id": 5, "pick": "Q", "jackpot": True}]
    seeds = [{"seed": 1, "team_id": 1}, {"seed": 2, "team_id": 2}, {"seed": 3, "team_id": 3}, {"seed": 4, "team_id": 4}]
    return teams, games, picks, seeds


def test_standings_stop_at_week_15_and_playoff_board_resets():
    teams, games, picks, seeds = _audit_data()
    st = {r["team"]: (r["w"], r["l"]) for r in chat.compute_standings(teams, games, picks)}
    assert st == {"A": (2, 0), "B": (1, 1)}, st          # week 17/18 finals never move the season standings
    wk = chat.compute_weekly(teams, games, picks)
    assert set(wk["A"]) == {14, 15} and set(wk["B"]) == {14, 15}, wk
    board = chat.compute_playoff_board(teams, games, picks, seeds)
    got = {r["team"]: (r["seed"], r["w"], r["l"]) for r in board}
    # scores reset; E isn't seeded (its pick doesn't show); tiebreaker game 4 doesn't count; C's jackpot counts
    assert got == {"A": (1, 0, 1), "B": (2, 2, 0), "C": (3, 1, 0), "D": (4, 0, 0)}, got
    assert board[0]["team"] == "B" and chat.compute_playoff_board(teams, games, picks, []) == []
    ctx = dict(_ctx(), playoff_board=board, current_week=17)
    n = chat.playoff_note(ctx, "What are the standings?")
    assert "PLAYOFF BOARD" in n and "B (seed 2) 2-0" in n and "scores reset" in n, n
    assert chat.playoff_note(ctx, "Who's the CFP committee ranking first?") == ""


def test_if_it_ended_today_comes_from_playoff_compute():
    pv = {"computed_at": "2026-11-16T18:50:00+00:00", "phase": "regular", "state": "regular",
          "qualifiers": [{"seed": 1, "team": "A", "points": 30, "decided_by": "regular season points"},
                         {"seed": 2, "team": "B", "points": 28, "decided_by": "regular season points"},
                         {"seed": 3, "team": "C", "points": 27, "decided_by": "chips"},
                         {"seed": 4, "team": "D", "points": 27, "decided_by": "regular season points"}],
          "cut_tie": {"points": 27, "slots": 1, "teams": ["D", "E"], "decided_by": "army_navy", "waiting_on": None},
          "holds": ["Week 15 games aren't picked yet, so the regular season isn't over."]}
    ctx = dict(_ctx(), playoff_preview=pv, playoff_board=[])
    n = chat.playoff_note(ctx, "Who's in if the season ended today?")
    assert "IF IT ENDED TODAY" in n and "seed 3: C, 27 pts (ahead of the next team on chips)" in n, n
    assert "settled by army_navy" in n and "projection" in n, n
    assert chat.playoff_note(ctx, "Roast TG") == ""
    assert chat._preview({"state": "regular", "preview": {"qualifiers": [
        {"seed": 1, "team_name": "A", "points": 3, "decided_by_label": "x"},
        {"seed": 2, "team_name": "Picky", "points": 9}]}}, {1: "A"})["qualifiers"] == [
        {"seed": 1, "team": "A", "points": 3, "decided_by": "x"}]


# --- live: real model, real data -------------------------------------------

LIVE = [
    ("How does scoring work in this league?", [r"\b(one|1) point", r"spread|cover"]),
    ("What are the tiebreakers?", [r"conference champ", r"army", r"coin flip"]),
    ("What happens if a game ends in a push?", [r"half"]),
    ("What does second place win?", [r"\$25|twenty.?five"]),
    ("When do the lines lock?", [r"tuesday", r"11"]),
    ("What happens if one of our games gets postponed?", [r"commissioner"]),
]


async def live():
    import subprocess
    import httpx
    key = subprocess.check_output(["security", "find-generic-password", "-a", "drake", "-s",
                                   "CFB_PICKEM__SUPABASE_SERVICE_KEY", "-w"], text=True).strip()
    chat._auth_headers = lambda _jwt: {"apikey": key, "Authorization": f"Bearer {key}"}

    async def fake_verify(_c, _j):
        return "35e1c78c-0fbc-4d9e-bc8d-f7df5ea73908"  # Chuck P
    chat._verify_jwt = fake_verify
    chat.RATE_LIMIT_PER_MIN = 10_000
    bad = 0
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=chat.app),
                                 base_url="http://picky", timeout=240) as c:
        for q, pats in LIVE:
            ans = []
            async with c.stream("POST", "/chat", json={"messages": [{"role": "user", "content": q}],
                                                       "supabase_jwt": "stub"}) as r:
                async for ln in r.aiter_lines():
                    if ln.startswith("data: {"):
                        ans.append(json.loads(ln[6:]).get("content", ""))
            a = "".join(ans)
            miss = [p for p in pats if not re.search(p, a, re.I)]
            bad += bool(miss)
            print(f"  {'ok ' if not miss else 'BAD'} {q}\n      {a}" + (f"\n      missing {miss}" if miss else ""))
    return bad


if __name__ == "__main__":
    fails = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ok  {name}")
            except AssertionError as e:
                fails += 1
                print(f"  FAIL {name}: {e}")
    if "--live-sql" in sys.argv or "--live" in sys.argv:
        bad = L.live_sql_checks(_FACTS)  # line_lock_at / calc_cfb_week / slots odds, SELECT-only
        print("  ok  live SQL matches league_rules" if not bad else f"  FAIL live SQL: {bad}")
        fails += bool(bad)
    if "--live" in sys.argv:
        fails += asyncio.run(live())
    print("\nALL GREEN" if not fails else f"\n{fails} FAILED")
    sys.exit(1 if fails else 0)
