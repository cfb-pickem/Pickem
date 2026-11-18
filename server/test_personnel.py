"""
Personnel store (plan phase 4): ESPN rosters, CFBD /games/players -> player_game, the qb_start ledger, one
normalized availability list, and the static position value table.

- Roster parse: ESPN team roster JSON -> players (id, name, position, class, jersey, team) + ESPN's own
  injured/suspended groups as availability rows.
- /games/players parse -> compact player_game rows; backfill + weekend refresh spend CFBD calls only through
  cfbd.py's budget machinery (check_budget/_record_call), never in tests (the HTTP client is a fake).
- qb_start: the QB with the most attempts started; SU and ATS as starter, first start, season line.
- Availability: status words, latest report wins, TBD stays TBD, >7-day-old reports say "last report <day>",
  works from news + ESPN alone (no dossiers / slate_intel).
- Position value table: committed data with a validator.

Run:  ./.venv/bin/python3 -I test_personnel.py
"""

from __future__ import annotations

import datetime as dt
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cfbd  # noqa: E402
import personnel as P  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="personnel-test-"))
CT = dt.timezone(dt.timedelta(hours=-6))
NOW = dt.datetime(2026, 11, 18, 15, 30, tzinfo=dt.timezone.utc).timestamp()


def _ath(i, name, pos, cls, jersey, injuries=()):
    first, last = name.split(" ", 1)
    return {"id": str(i), "fullName": name, "displayName": name, "firstName": first, "lastName": last,
            "jersey": jersey, "position": {"abbreviation": pos, "name": pos},
            "experience": {"years": 1, "displayValue": cls, "abbreviation": cls[:2].upper()},
            "status": {"type": "active", "name": "Active"}, "injuries": list(injuries)}


ROSTER = {"timestamp": "2026-11-18T16:12:24Z", "season": {"year": 2026},
          "team": {"id": "2633", "displayName": "Tennessee Volunteers", "location": "Tennessee"},
          "athletes": [
              {"position": "offense", "items": [
                  _ath(5141571, "George MacIntyre", "QB", "Freshman", "12"),
                  _ath(5158973, "Faizon Brandon", "QB", "Freshman", "11"),
                  _ath(4917093, "Ryan Staub", "QB", "Junior", "8"),
                  _ath(5158956, "Ed Baker", "OL", "Freshman", "71"),
                  _ath(5000001, "Mike Matthews", "WR", "Sophomore", "1",
                       injuries=[{"status": "Out", "date": "2026-11-16T12:00Z", "type": {"description": "knee"}}])]},
              {"position": "defense", "items": [_ath(5000002, "Jadais Richard", "CB", "Junior", "4")]},
              {"position": "injuredReserveOrOut", "items": [_ath(5000003, "Daune Morris", "RB", "Junior", "3")]},
              {"position": "suspended", "items": [_ath(5000004, "Sam Bad", "LB", "Senior", "44")]},
          ]}


# --- rosters ----------------------------------------------------------------------------------------------

def test_roster_parse_players_with_class_position_jersey():
    ps = P.parse_roster(ROSTER, "Tennessee")
    mac = next(p for p in ps if p["name"] == "George MacIntyre")
    assert mac == {"id": "5141571", "name": "George MacIntyre", "pos": "QB", "cls": "Freshman", "jersey": "12",
                   "team": "Tennessee", "group": "offense"}, mac
    assert {p["name"] for p in ps} >= {"Ed Baker", "Jadais Richard", "Daune Morris", "Sam Bad"}
    assert len(ps) == 8


def test_roster_groups_and_injuries_become_availability_rows():
    rows = P.roster_status_rows(P.parse_roster(ROSTER, "Tennessee"), ROSTER, "2026-11-18T16:12:24Z")
    by = {r["player"]: r for r in rows}
    assert by["Daune Morris"]["status"] == "out" and by["Sam Bad"]["status"] == "suspended", by
    assert by["Mike Matthews"]["status"] == "out" and by["Mike Matthews"]["detail"] == "knee", by
    assert all(r["source_name"] == "ESPN roster" and r["team"] == "Tennessee" for r in rows)
    assert "George MacIntyre" not in by


def test_roster_tick_fetches_a_few_due_teams_and_then_idles():
    db = P.connect(TMP / "roster-tick.sqlite")
    teams = {f"Team{i}": str(1000 + i) for i in range(5)}
    got = []

    def fetch(espn_id):
        got.append(espn_id)
        return dict(ROSTER, team={"id": espn_id})
    t0 = NOW
    for k in range(3):
        P.roster_tick(db, teams, fetch=fetch, now=t0 + k * 300, per_tick=2)
    assert len(got) == 5, got  # 2 + 2 + 1, then nothing left
    n = len(got)
    P.roster_tick(db, teams, fetch=fetch, now=t0 + 3 * 86400, per_tick=2)
    assert len(got) == n, "inside the weekly TTL nothing is fetched"
    P.roster_tick(db, teams, fetch=fetch, now=t0 + 8 * 86400, per_tick=2)
    assert len(got) == n + 2, "a week later the oldest rosters refresh"
    assert len(P.rosters(db)) == 5 and P.rosters(db)["Team0"]["players"]


def test_roster_tick_survives_a_failed_fetch():
    db = P.connect(TMP / "roster-fail.sqlite")

    def boom(espn_id):
        raise OSError("espn down")
    assert P.roster_tick(db, {"A": "1"}, fetch=boom, now=NOW) == {"fetched": 0, "failed": 1}


# --- /games/players -----------------------------------------------------------------------------------------

def _cat(name, types):
    return {"name": name, "types": [{"name": t, "athletes": [{"id": i, "name": n, "stat": s} for i, n, s in ath]}
                                    for t, ath in types.items()]}


def _team(team, ha, pts, passers, rushers=(), receivers=()):
    cats = [_cat("passing", {"C/ATT": [(i, n, ca) for i, n, ca, *_ in passers],
                             "YDS": [(i, n, y) for i, n, _, y, *_ in passers],
                             "TD": [(i, n, td) for i, n, _, _, td, _ in passers],
                             "INT": [(i, n, it) for i, n, _, _, _, it in passers],
                             "QBR": [(i, n, "50.0") for i, n, *_ in passers]})]
    if rushers:
        cats.append(_cat("rushing", {"CAR": [(i, n, c) for i, n, c, _, _ in rushers],
                                     "YDS": [(i, n, y) for i, n, _, y, _ in rushers],
                                     "TD": [(i, n, t) for i, n, _, _, t in rushers]}))
    if receivers:
        cats.append(_cat("receiving", {"REC": [(i, n, c) for i, n, c, _, _ in receivers],
                                       "YDS": [(i, n, y) for i, n, _, y, _ in receivers],
                                       "TD": [(i, n, t) for i, n, _, _, t in receivers]}))
    cats.append(_cat("defensive", {"TOT": [("9", "Somebody", "7")]}))
    return {"team": team, "conference": "SEC", "homeAway": ha, "points": pts, "categories": cats}


def gp_week(week):
    """Tennessee's QB room over three weeks: MacIntyre starts weeks 1-2, Brandon week 3 (MacIntyre hurt)."""
    mac, bra = ("5141571", "George MacIntyre"), ("5158973", "Faizon Brandon")
    if week == 1:
        tn = _team("Tennessee", "home", 45, [(*mac, "20/28", "250", "3", "0"), (*bra, "3/4", "40", "0", "0")],
                   rushers=[(*mac, "5", "30", "1"), ("7", "Daune Morris", "15", "110", "1")],
                   receivers=[("5000001", "Mike Matthews", "6", "120", "2")])
        return [{"id": 101, "teams": [tn, _team("Furman", "away", 10, [("8", "Fur Man", "10/20", "90", "0", "1")])]}]
    if week == 2:
        tn = _team("Tennessee", "away", 20, [(*mac, "18/33", "180", "1", "2"), ("-1", "Team", "0/1", "0", "0", "0")])
        return [{"id": 102, "teams": [tn, _team("Georgia", "home", 30, [("8", "Gun Slinger", "25/30", "300", "3", "0")])]}]
    tn = _team("Tennessee", "home", 31, [(*bra, "22/30", "260", "2", "0"), (*mac, "1/2", "5", "0", "0")])
    return [{"id": 103, "teams": [tn, _team("Kentucky", "away", 14, [("8", "Wild Cat", "12/25", "100", "0", "2")])]}]


def test_games_players_parse_compact_rows():
    rows = P.parse_games_players(gp_week(1), 2026, 1)
    mac = next(r for r in rows if r["name"] == "George MacIntyre")
    assert (mac["game_id"], mac["team"], mac["opp"], mac["week"]) == ("101", "Tennessee", "Furman", 1), mac
    assert (mac["cmp"], mac["att"], mac["yds"], mac["td"], mac["int"], mac["car"], mac["rush_yds"], mac["rush_td"]) == \
        (20, 28, 250, 3, 0, 5, 30, 1), mac
    wr = next(r for r in rows if r["name"] == "Mike Matthews")
    assert (wr["rec"], wr["rec_yds"], wr["rec_td"]) == (6, 120, 2) and wr["att"] == 0
    assert not any(r["name"] == "Somebody" for r in rows), "defense-only players are not kept"
    assert not any(r["name"] == "Team" for r in P.parse_games_players(gp_week(2), 2026, 2)), "team totals dropped"


class FakeResp:
    def __init__(self, data, status=200, remaining=900):
        self._d, self.status_code, self.headers = data, status, {"x-calllimit-remaining": str(remaining)}

    def json(self):
        return self._d


class FakeClient:
    def __init__(self, used=60):
        self.calls, self.used = [], used

    def get(self, path, params=None):
        self.calls.append((path, dict(params or {})))
        if path == "/info":
            return FakeResp({"usedCalls": self.used})
        return FakeResp(gp_week(int(params["week"])))


def test_backfill_one_call_per_week_through_the_budget():
    cdb = cfbd.connect(TMP / "cfbd-a.sqlite")
    pdb = P.connect(TMP / "pers-a.sqlite")
    c = FakeClient()
    out = P.backfill([1, 2, 3], cfbd_db=cdb, db=pdb, client=c, season=2026, now=NOW)
    paid = [x for x in c.calls if x[0] != "/info"]
    assert paid == [("/games/players", {"year": 2026, "week": w, "seasonType": "regular", "classification": "fbs"})
                    for w in (1, 2, 3)], paid
    assert out["calls"] == 3 and cfbd.calls_used(cdb) == 3, out
    assert [r[1] for r in cdb.execute("select ts, endpoint from call_log")] == ["/games/players"] * 3
    assert len(P.player_games(pdb)) > 5
    assert P.weeks_fetched(pdb, 2026)[3]["fetched_at"] == NOW


def test_backfill_refuses_past_the_ceiling_and_spends_nothing():
    cdb = cfbd.connect(TMP / "cfbd-b.sqlite")
    cdb.execute("insert into calls(month, count) values(?, ?)", (cfbd._month(), 105))
    cdb.commit()
    c = FakeClient()
    try:
        P.backfill(list(range(1, 12)), cfbd_db=cdb, db=P.connect(TMP / "pers-b.sqlite"), client=c, season=2026,
                   now=NOW, ceiling=110)
        raise AssertionError("should refuse")
    except cfbd.BudgetExceeded:
        pass
    assert [x for x in c.calls if x[0] != "/info"] == [] and cfbd.calls_used(cdb) == 105


def test_backfill_respects_the_shared_reserve():
    cdb = cfbd.connect(TMP / "cfbd-c.sqlite")
    c = FakeClient(used=499)
    try:
        P.backfill([1, 2], cfbd_db=cdb, db=P.connect(TMP / "pers-c.sqlite"), client=c, season=2026, now=NOW)
        raise AssertionError("should refuse")
    except cfbd.BudgetExceeded:
        pass
    assert cfbd.calls_used(cdb) == 0


def _games_sched():
    """Week 11 played Sat 11/14, week 12 Tue 11/17 + Sat 11/21 (kickoffs UTC)."""
    def g(w, iso):
        return {"week": w, "startDate": iso, "seasonType": "regular"}
    return [g(11, "2026-11-14T17:00:00.000Z"), g(11, "2026-11-15T03:30:00.000Z"),
            g(12, "2026-11-17T23:00:00.000Z"), g(12, "2026-11-21T17:00:00.000Z"), g(12, "2026-11-22T03:30:00.000Z")]


def _ts(y, m, d, h, mi=0):
    return dt.datetime(y, m, d, h, mi, tzinfo=CT).timestamp()


def test_due_week_final_partial_and_idle():
    gs = _games_sched()
    sun = _ts(2026, 11, 15, 6, 15)
    assert P.due_week(gs, {}, sun) == 11, "week 11 is final Sunday morning"
    assert P.due_week(gs, {11: {"fetched_at": sun, "partials": 0}}, sun + 3600) is None
    wed = _ts(2026, 11, 18, 6, 15)
    assert P.due_week(gs, {11: {"fetched_at": sun, "partials": 0}}, wed) is None, "midweek: nothing due"
    sat = _ts(2026, 11, 21, 16, 30)
    f = {11: {"fetched_at": sun, "partials": 0}}
    assert P.due_week(gs, f, sat) == 12, "Saturday evening: a partial of this week"
    f[12] = {"fetched_at": sat, "partials": 1}
    assert P.due_week(gs, f, sat + 3600) is None, "4 h between partials"
    f[12] = {"fetched_at": sat + 4 * 3600, "partials": 2}
    assert P.due_week(gs, f, sat + 9 * 3600) is None, "two partials max before the final"
    assert P.due_week(gs, f, _ts(2026, 11, 22, 6, 15)) == 12, "Sunday morning: the final pull"
    assert P.due_week(gs, {11: f[11]}, _ts(2026, 11, 21, 7, 0)) is None, "Saturday morning: too early"


def test_weekend_refresh_spends_at_most_one_call():
    cdb = cfbd.connect(TMP / "cfbd-d.sqlite")
    pdb = P.connect(TMP / "pers-d.sqlite")
    c = FakeClient()
    out = P.refresh_player_games(cfbd_db=cdb, db=pdb, client=c, games=_games_sched(), season=2026,
                                 now=_ts(2026, 11, 15, 6, 15))
    assert out["week"] == 11 and cfbd.calls_used(cdb) == 1, out
    out = P.refresh_player_games(cfbd_db=cdb, db=pdb, client=c, games=_games_sched(), season=2026,
                                 now=_ts(2026, 11, 15, 7, 15))
    assert out["week"] is None and cfbd.calls_used(cdb) == 1, out


def test_weeks_mirror_to_supabase_and_restore_without_new_calls():
    pushed = []
    cdb = cfbd.connect(TMP / "cfbd-e.sqlite")
    P.backfill([1, 2], cfbd_db=cdb, db=P.connect(TMP / "pers-e.sqlite"), client=FakeClient(), season=2026, now=NOW,
               push=pushed.extend)
    assert [r["key"] for r in pushed] == ["2026-w01", "2026-w02"] and pushed[0]["kind"] == "player_game", pushed
    fresh = P.connect(TMP / "pers-e2.sqlite")
    assert P.restore(fresh, puller=lambda kind: pushed + [{"kind": "game", "key": "x", "body": {}}]) == 2
    assert sorted(P.weeks_fetched(fresh, 2026)) == [1, 2] and len(P.player_games(fresh)) == \
        len(P.player_games(P.connect(TMP / "pers-e.sqlite")))
    assert cfbd.calls_used(cdb) == 2


def test_cfbd_refresh_main_runs_the_player_game_refresh():
    src = (Path(__file__).resolve().parent / "cfbd.py").read_text()
    main = src[src.index('if __name__ == "__main__":'):]
    assert "personnel" in main and "refresh_player_games" in main


# --- qb_start ledger ------------------------------------------------------------------------------------------

RESULTS = {"Tennessee": [
    {"game_id": "101", "date": "2026-08-30", "opp": "Furman", "home": True, "pts": 45, "opp_pts": 10, "spread": -35.5,
     "cover": -0.5},
    {"game_id": "102", "date": "2026-09-06", "opp": "Georgia", "home": False, "pts": 20, "opp_pts": 30, "spread": 7.5,
     "cover": -2.5},
    {"game_id": "103", "date": "2026-09-13", "opp": "Kentucky", "home": True, "pts": 31, "opp_pts": 14, "spread": -6.5,
     "cover": 10.5}]}


def _all_rows():
    return [r for w in (1, 2, 3) for r in P.parse_games_players(gp_week(w), 2026, w)]


def test_qb_start_ledger_starts_su_ats_first_start_and_line():
    led = P.qb_ledger(_all_rows(), RESULTS)["Tennessee"]
    mac = next(q for q in led if q["name"] == "George MacIntyre")
    bra = next(q for q in led if q["name"] == "Faizon Brandon")
    assert (mac["starts"], mac["su"], mac["ats"]) == (2, "1-1", "0-2"), mac
    assert (bra["starts"], bra["su"], bra["ats"], bra["first_start"]) == (1, "1-0", "1-0", "2026-09-13"), bra
    assert mac["line"] == {"cmp": 39, "att": 63, "yds": 435, "td": 4, "int": 2, "car": 5, "rush_yds": 30,
                           "rush_td": 1, "games": 3}, mac["line"]
    assert led[0]["name"] == "George MacIntyre", "most starts first"
    assert P.starter(led)["name"] == "Faizon Brandon", "the latest game's starter"
    assert not any(q["name"] == "Team" for q in led)


def test_qb_with_no_start_is_flagged():
    led = P.qb_ledger(_all_rows()[:4], RESULTS)["Tennessee"]  # week 1 only
    bra = next(q for q in led if q["name"] == "Faizon Brandon")
    assert bra["starts"] == 0 and bra["first_start"] is None and bra["su"] == "0-0", bra


def test_passer_rating_ncaa_formula():
    assert round(P.passer_rating(20, 28, 250, 3, 0), 1) == round((8.4 * 250 + 330 * 3 + 100 * 20) / 28, 1)
    assert P.passer_rating(0, 0, 0, 0, 0) is None


# --- availability normalization ---------------------------------------------------------------------------

def test_status_words():
    cases = {"Mike Matthews is out for the season": "season", "He was ruled out for Saturday": "out",
             "listed as doubtful": "doubtful", "a game-time decision": "questionable", "questionable vs LSU": "questionable",
             "probable": "probable", "Clemson has not named a starter": "TBD", "status is unclear": "TBD",
             "was suspended for the first half": "suspended", "entered the transfer portal": "portal",
             "opted out of the bowl": "opt-out", "was benched for the second half": "benched",
             "cleared to play": "available", "season-ending knee injury": "season", "won't play Saturday": "out",
             "likely won't play at Tennessee": "doubtful", "had a great practice": None,
             "QB Tayven Jackson out for regular season": "season", "expected to miss rest of season": "season",
             "Vizzina is in the concussion protocol": "questionable", "Tait Reynolds is done for the year": "season"}
    for text, want in cases.items():
        assert P.status_of(text) == want, (text, P.status_of(text), want)


INTEL = {"as_of": "Wed Nov 18 2:45 AM CT", "games": {"401856754": {"away": "LSU", "home": "Tennessee", "facts": [
    {"status": "questionable", "kind": "injury", "team": "home", "players": ["George MacIntyre", "Faizon Brandon"],
     "text": "Tennessee QB George MacIntyre hurt his right knee at Texas A&M; Josh Heupel said he is working toward "
             "playing vs LSU, and freshman Faizon Brandon (five career starts) finished the game and could also play.",
     "source_name": "Yahoo Sports", "source_url": "https://sports.yahoo.com/a", "date": "2026-11-16", "confidence": "high"},
    {"status": "out", "kind": "injury", "team": "home", "players": ["Mike Matthews", "Daune Morris"],
     "text": "Tennessee's top wide receiver Mike Matthews and big-play running back Daune Morris are both out for the season.",
     "source_name": "Yahoo Sports", "source_url": "https://sports.yahoo.com/a", "date": "2026-11-16", "confidence": "high"},
    {"status": "doubtful", "kind": "injury", "team": "away", "players": ["Richard Anderson", "Weston Davis"],
     "text": "LSU freshman DT Richard Anderson has missed three straight games and Kiffin said he likely won't play at "
             "Tennessee; RT Weston Davis played through a 'really bad' ankle injury vs Texas.",
     "source_name": "The Advocate", "source_url": "https://theadvocate.com/x", "date": "2026-11-16", "confidence": "high"},
    {"kind": "fact", "team": "away", "players": ["Sam Leavitt"],
     "text": "Lane Kiffin indicated Sam Leavitt stays LSU's starting QB.", "source_name": "The Advocate",
     "source_url": "https://theadvocate.com/x", "date": "2026-11-16"}]}}}


def _by(rows):
    return {r["player"]: r for r in rows}


def test_intel_notes_normalize_per_player():
    rows = _by(P.normalize(intel=INTEL, now=NOW))
    assert rows["George MacIntyre"]["status"] == "questionable" and rows["George MacIntyre"]["team"] == "Tennessee"
    assert rows["George MacIntyre"]["source_name"] == "Yahoo Sports" and rows["George MacIntyre"]["game_id"] == "401856754"
    assert "Faizon Brandon" not in rows, "a second name in the note doesn't inherit the first one's status"
    assert rows["Mike Matthews"]["status"] == rows["Daune Morris"]["status"] == "season"
    assert rows["Richard Anderson"]["status"] == "doubtful" and "Weston Davis" not in rows
    assert "Sam Leavitt" not in rows, "a note with no status word makes no availability row"
    assert rows["George MacIntyre"]["quote"].startswith("Tennessee QB George MacIntyre hurt his right knee")
    assert rows["Richard Anderson"]["pos"] == "DT" and rows["George MacIntyre"]["pos"] == "QB"


def news(title, summary, players, teams, date, label="reported", status=None):
    return {"title": title, "summary": summary, "players": players, "teams": teams, "date": date, "label": label,
            "status": status, "source": "CBS Sports", "link": f"https://cbs.example/{abs(hash(title))}", "kind": "injury"}


def test_abbreviated_dates_dont_split_a_clause():
    intel = {"games": {"1": {"away": "Ohio State", "home": "Nebraska", "facts": [
        {"status": "out", "kind": "injury", "team": "away", "players": ["Julian Sayin"], "date": "2026-11-05",
         "text": "Ohio State QB Julian Sayin (throwing shoulder, hurt Oct. 17 vs Indiana) is expected to miss the rest "
                 "of the regular season; Ryan Day said it's 'gonna take some time.'", "source_name": "Yahoo Sports"}]}}}
    assert _by(P.normalize(intel=intel, now=NOW))["Julian Sayin"]["status"] == "season"
    assert P.clauses("He was hurt Oct. 17 vs. Indiana. He is out.") == ["He was hurt Oct. 17 vs. Indiana", "He is out."]


def test_latest_report_wins():
    later = news("Tennessee QB George MacIntyre ruled out vs. LSU", "Heupel said Brandon starts.",
                 ["George MacIntyre", "Faizon Brandon"], ["Tennessee", "LSU"], "2026-11-18T14:00:00+00:00")
    rows = _by(P.normalize(intel=INTEL, news_items=[later], now=NOW))
    assert rows["George MacIntyre"]["status"] == "out" and rows["George MacIntyre"]["source_name"] == "CBS Sports"
    earlier = dict(later, date="2026-11-10T14:00:00+00:00")
    rows = _by(P.normalize(intel=INTEL, news_items=[earlier], now=NOW))
    assert rows["George MacIntyre"]["status"] == "questionable", "an older report loses to the newer note"


def test_tbd_stays_tbd():
    intel = {"games": {"9": {"away": "Clemson", "home": "Duke", "facts": [
        {"kind": "fact", "team": "away", "players": ["Cade Klubnik", "Chris Vizzina"], "date": "2026-11-17",
         "text": "Dabo Swinney has not named a starter between Cade Klubnik and Chris Vizzina for Friday.",
         "source_name": "The State", "source_url": "https://thestate.com/x"}]}}}
    nothing = news("Clemson QB Cade Klubnik practices Tuesday", "Klubnik threw in individual drills.",
                   ["Cade Klubnik"], ["Clemson"], "2026-11-18T01:00:00+00:00")
    rows = _by(P.normalize(intel=intel, news_items=[nothing], now=NOW))
    assert rows["Cade Klubnik"]["status"] == "TBD" and rows["Chris Vizzina"]["status"] == "TBD", rows
    assert rows["Cade Klubnik"]["quote"].startswith("Dabo Swinney has not named a starter")


def test_works_from_news_and_espn_alone():
    item = news("Indiana loses starting LG Joe Smith for the season", "Smith tore his ACL in practice.",
                ["Joe Smith"], ["Indiana"], "2026-11-17T20:00:00+00:00", label="confirmed")
    roster_rows = P.roster_status_rows(P.parse_roster(ROSTER, "Tennessee"), ROSTER, "2026-11-18T16:12:24Z")
    rows = _by(P.normalize(intel=None, dossiers=[], news_items=[item], roster_rows=roster_rows, now=NOW))
    assert rows["Joe Smith"]["status"] == "season" and rows["Joe Smith"]["team"] == "Indiana"
    assert rows["Joe Smith"]["pos"] == "LG" and rows["Joe Smith"]["confidence"] == "medium"
    assert rows["Daune Morris"]["status"] == "out" and rows["Daune Morris"]["source_name"] == "ESPN roster"


def test_dossier_unknown_status_is_tbd():
    dos = {"game_id": 401856754, "away": "LSU", "home": "Tennessee", "facts": [
        {"status": "unknown", "kind": "injury", "team": "home", "players": ["George MacIntyre"], "date": "2026-11-17",
         "text": "Heupel said MacIntyre 'looked good this morning and will continue to progress through the week.'",
         "source_name": "sports.yahoo.com", "source_url": "https://sports.yahoo.com/b"}]}
    rows = _by(P.normalize(dossiers=[dos], now=NOW))
    assert rows["George MacIntyre"]["status"] == "TBD" and rows["George MacIntyre"]["game_id"] == "401856754"


def test_news_team_comes_from_the_roster_map():
    item = news("LSU at Tennessee: Richard Anderson ruled out", "", ["Richard Anderson"], ["LSU", "Tennessee"],
                "2026-11-18T01:00:00+00:00")
    rows = P.normalize(news_items=[item], now=NOW, name_team={"richard anderson": "LSU"})
    assert rows and rows[0]["team"] == "LSU", rows
    assert P.normalize(news_items=[item], now=NOW) == [], "two teams and no roster: never guess the team"


def test_report_wording_fresh_vs_stale():
    assert P.report_when("2026-11-16", NOW) == ("Monday", False)
    phrase, stale = P.report_when("2026-11-02", NOW)
    assert stale and phrase == "last report Mon 11/2", phrase
    assert P.report_when("2026-11-18T14:00:00+00:00", NOW) == ("today", False)


def test_live_pbp_injury_without_return_is_tbd():
    live = {"games": {"1": {"state": "post", "start": "2026-11-14T20:00Z", "away": {"name": "Utah"},
                            "home": {"name": "BYU"}, "injuries": [
                                {"player": "D.Smith", "full_name": "Devon Smith", "team": "Utah", "qb": True,
                                 "period": 3, "clock": "5:12", "returned": None, "source": "ESPN play-by-play"},
                                {"player": "K.Back", "full_name": "Kyle Back", "team": "BYU", "qb": False,
                                 "period": 2, "clock": "1:00", "returned": [3, "9:00"], "source": "ESPN play-by-play"}]}}}
    rows = _by(P.normalize(live=live, now=NOW))
    assert rows["Devon Smith"]["status"] == "TBD" and "didn't return" in rows["Devon Smith"]["quote"]
    assert "Kyle Back" not in rows


# --- position value table ---------------------------------------------------------------------------------

def test_committed_position_value_table_validates():
    assert P.validate_position_values(P.position_values()) == []


def test_validator_catches_bad_tables():
    good = P.position_values()
    bad = json.loads(json.dumps(good))
    bad["qb"]["tiers"] = list(reversed(bad["qb"]["tiers"]))
    assert P.validate_position_values(bad), "tiers must run from the biggest gap down"
    bad = json.loads(json.dumps(good))
    bad["positions"]["WR1"] = 9.0
    assert P.validate_position_values(bad), "no non-QB is worth more than the smallest QB tier"
    bad = json.loads(json.dumps(good))
    bad["aliases"].pop("OT", None)
    assert P.validate_position_values(bad), "every ESPN position maps to a value"


def test_values_by_position_and_qb_gap():
    assert P.player_value("LG") == P.position_values()["positions"]["OL"]
    assert P.player_value("WR", role="WR1") > P.player_value("WR")
    big, small = P.qb_value(160.0, 110.0, 120), P.qb_value(150.0, 145.0, 120)
    assert big > small >= 1.0, (big, small)
    assert P.qb_value(150.0, None, 0) == P.position_values()["qb"]["unknown_backup"]



def test_no_model_on_the_personnel_path():
    src = Path(P.__file__).read_text().lower()
    assert "claude" not in src and "anthropic" not in src and "ollama" not in src

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
