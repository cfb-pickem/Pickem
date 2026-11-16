"""
Picky the player: exhibition exclusion, pick logic, lock timing, idempotency.

Run:  ./.venv/bin/python3 -I test_picky_player.py

Offline except test_live_rls_hides_exhibition, which reads the live DB with
the PUBLIC anon key (read-only) and skips until the migration is applied.
"""

from __future__ import annotations

import datetime as dt
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import chat  # noqa: E402
import picky_picks as pp  # noqa: E402

UTC = dt.timezone.utc
ROOT = Path(__file__).resolve().parent.parent


def g(gid, away, home, line, start, week=12, status="scheduled", picked=True, tiebreaker=False, fpi=None):
    return {"GameId": gid, "Away": away, "Home": home, "line": line, "Start (CT)": start,
            "week": week, "Status": status, "picked": picked, "tiebreaker": tiebreaker,
            "fpi_margin": fpi}


def notes(home_abbr, away_abbr, details):
    return {"home": {"abbr": home_abbr}, "away": {"abbr": away_abbr}, "market": {"details": details}}


# ---------------------------------------------------------------- pick logic

def test_value_side_takes_the_mispriced_side():
    # pool Boise -13.5, market Boise -16.5: Boise is 3 points better than the pool says
    d = pp.decide(g(1, "Oregon State", "Boise State", -13.5, "2026-11-14T17:00:00"),
                  notes("BOIS", "ORST", "BOIS -16.5"), None)
    assert (d["pick"], d["method"], d["edge"]) == ("Boise State", "value", 3.0), d
    assert pp.pick_label(d) == "Boise State -13.5"
    # pool home -7, market home -3: away is the value
    d = pp.decide(g(2, "Iowa", "Utah", -7, "2026-11-14T17:00:00"), notes("UTAH", "IOWA", "UTAH -3"), None)
    assert (d["pick"], d["method"]) == ("Iowa", "value"), d
    assert pp.pick_label(d) == "Iowa +7"


def test_no_edge_falls_to_sp_then_fpi_then_points():
    game = g(3, "North Texas", "UTSA", 2.5, "2026-11-14T20:00:00", fpi=-4.5)
    flat = notes("UTSA", "UNT", "UNT -2.5")          # market == pool: no value
    idx = {"teams": {}}

    class FakeCfbd:
        @staticmethod
        def resolve_team(name, _idx):
            return {"sp": {"rating": {"North Texas": 4.7, "UTSA": 0.9}[name]}}
    real = pp.cfbd
    pp.cfbd = FakeCfbd
    try:
        d = pp.decide(game, flat, idx)
        # UTSA by 0.9 - 4.7 + 2.5 = -1.3, +2.5 line -> UTSA covers by 1.2
        assert (d["pick"], d["method"]) == ("UTSA", "sp+"), d
    finally:
        pp.cfbd = real
    d = pp.decide(game, flat, None)                   # no CFBD: FPI -4.5 + 2.5 -> North Texas
    assert (d["pick"], d["method"]) == ("North Texas", "fpi"), d
    d = pp.decide({**game, "fpi_margin": None}, flat, None)
    assert (d["pick"], d["method"]) == ("UTSA", "points"), d   # home is the dog at +2.5


def test_pick_logic_is_deterministic():
    game = g(4, "Oregon State", "Boise State", -13.5, "2026-11-14T17:00:00", fpi=12.7)
    n = notes("BOIS", "ORST", "BOIS -16.5")
    assert all(pp.decide(game, n, None) == pp.decide(game, n, None) for _ in range(5))


def test_blurb_filter_blocks_persona_limits():
    assert pp.blurb_ok("Boise -13.5 and you clowns are fading it. Enjoy last place.")
    for bad in ("your mom picked UTSA", "that's so gay", "ugly pick", "pray to god", "x" * 300, ""):
        assert not pp.blurb_ok(bad), bad


# ---------------------------------------------------------------- lock timing

WEEK = [
    g(10, "A", "B", -3.5, "2026-11-21T11:00:00"),
    g(11, "C", "D", 1.5, "2026-11-21T14:30:00"),
    g(12, "E", "F", -7.5, "2026-11-21T18:00:00", picked=False, tiebreaker=True),
]


def ct(s):
    return dt.datetime.fromisoformat(s).replace(tzinfo=pp.CT).astimezone(UTC)


def test_regular_season_locks_the_week_at_first_kickoff():
    for game in WEEK:
        assert pp.lock_utc(game, WEEK) == ct("2026-11-21T11:00:00")
    # a tiebreaker kicking first locks the week too (picks_open_for_game)
    early_tb = WEEK + [g(13, "G", "H", 3.5, "2026-11-21T10:00:00", picked=False, tiebreaker=True)]
    assert pp.lock_utc(WEEK[1], early_tb) == ct("2026-11-21T10:00:00")


def test_playoffs_lock_per_game():
    po = [g(20, "A", "B", -3.5, "2026-12-19T11:00:00", week=16),
          g(21, "C", "D", 1.5, "2026-12-19T18:00:00", week=16)]
    assert pp.lock_utc(po[1], po) == ct("2026-12-19T18:00:00")


def test_due_games_window_and_never_after_lock():
    lock = ct("2026-11-21T11:00:00")
    ids = lambda gs: [x["GameId"] for x in gs]  # noqa: E731
    assert pp.due_games(WEEK, lock - dt.timedelta(hours=5), window=pp.PICK_WINDOW) == []
    assert ids(pp.due_games(WEEK, lock - dt.timedelta(hours=2), window=pp.PICK_WINDOW)) == [10, 11, 12]
    assert ids(pp.due_games(WEEK, lock - dt.timedelta(hours=2), skip={11})) == [10, 12]
    assert pp.due_games(WEEK, lock, window=pp.PICK_WINDOW) == []          # at lock: nothing
    assert pp.due_games(WEEK, lock + dt.timedelta(minutes=1)) == []
    no_line = [{**WEEK[0], "line": None}]
    assert pp.due_games(no_line, lock - dt.timedelta(hours=1)) == []


def test_catch_up_uses_per_game_kickoff():
    # this week's catch-up rule: a game is open until ITS kickoff
    now = ct("2026-11-21T12:00:00")
    assert not pp.is_open(WEEK[0], now, WEEK, pp.kickoff_lock)
    assert pp.is_open(WEEK[1], now, WEEK, pp.kickoff_lock)
    assert not pp.is_open(WEEK[1], now, WEEK)                             # league rule: locked


# ---------------------------------------------------------------- idempotent submit

class FakeDB:
    def __init__(self, have=()):
        self.have = set(have)
        self.inserted = []

    def picky_game_ids(self, team_id, ids):
        return {i for i in ids if i in self.have}

    def insert(self, table, rows, on_conflict=None):
        assert on_conflict == "team_id,game_id", "must never overwrite an existing pick"
        self.inserted.append((table, rows))
        if table == "picks":
            self.have |= {r["game_id"] for r in rows}
        return rows


def pick(gid, gen, lock, kickoff=None):
    return {"game_id": gid, "week": 12, "pick": "B", "method": "value", "reason": "r", "blurb": "b",
            "generated_at": gen.isoformat(), "lock_utc": lock.isoformat(),
            "kickoff_utc": (kickoff or lock).isoformat()}


def test_submit_is_idempotent_and_respects_lock():
    lock = ct("2026-11-21T11:00:00")
    gen = lock - dt.timedelta(hours=2)
    db = FakeDB(have={11})
    batch = [pick(10, gen, lock), pick(11, gen, lock)]
    assert pp.submit(db, 99, batch, now=gen) == 1                 # 11 already picked
    assert [r["game_id"] for r in db.inserted[0][1]] == [10]
    assert db.inserted[0][1][0]["by_slots"] is False and db.inserted[0][1][0]["jackpot"] is False
    assert pp.submit(db, 99, batch, now=gen) == 0                 # second run: nothing
    assert pp.submit(FakeDB(), 99, [pick(12, gen, lock)], now=lock) == 0   # at lock: nothing
    late = pick(13, lock + dt.timedelta(seconds=1), lock)
    assert pp.submit(FakeDB(), 99, [late], now=gen, require_open=False) == 0  # made after lock


def test_catch_up_file_rule_generated_before_kickoff():
    kick = ct("2026-11-14T17:00:00")
    before = {k: v for k, v in pick(1, kick - dt.timedelta(hours=5), kick).items() if k != "lock_utc"}
    after = {k: v for k, v in pick(2, kick + dt.timedelta(minutes=1), kick).items() if k != "lock_utc"}
    db = FakeDB()
    # inserted later (require_open=False), but only the one made before kickoff
    assert pp.submit(db, 99, [before, after], now=kick + dt.timedelta(days=1), require_open=False) == 1
    assert db.inserted[0][1][0]["game_id"] == 1


# ---------------------------------------------------------------- exhibition exclusion

HUMANS = [{"team_id": 1, "team_name": "A"}, {"team_id": 2, "team_name": "B"},
          {"team_id": 3, "team_name": "C"}]
BOT = {"team_id": 50, "team_name": "Picky", "is_exhibition": True}
GAMES = [{"GameId": 100 + i, "winner": "X", "week": 10, "tiebreaker": i == 4} for i in range(5)]
HUMAN_PICKS = [{"team_id": t, "game_id": 100 + i, "pick": "X" if (t + i) % 2 else "Y", "jackpot": False}
               for t in (1, 2, 3) for i in range(5)]
BOT_PICKS = [{"team_id": 50, "game_id": 100 + i, "pick": "X", "jackpot": False} for i in range(5)]


def test_standings_identical_with_or_without_picky():
    base = chat.compute_standings(HUMANS, GAMES, HUMAN_PICKS)
    with_bot = chat.compute_standings(HUMANS + [BOT], GAMES, HUMAN_PICKS + BOT_PICKS)
    assert with_bot == base, (base, with_bot)
    assert all(r["team"] != "Picky" for r in with_bot)


def test_summary_reveals_only_kicked_off_picks_and_skips_tiebreakers():
    data = {"team_name": "Picky", "picks": [
        {"game_id": 1, "week": 11, "pick": "Boise State", "away": "Oregon State", "home": "Boise State",
         "line": -13.5, "status": "final", "winner": "Boise State", "tiebreaker": False,
         "start": "2026-11-14T17:00:00", "reason": "value", "blurb": "eat it"},
        {"game_id": 2, "week": 11, "pick": "UTSA", "away": "North Texas", "home": "UTSA",
         "line": 2.5, "status": "scheduled", "winner": None, "tiebreaker": False,
         "start": "2026-11-14T20:00:00", "reason": "sp+", "blurb": "secret"},
        {"game_id": 3, "week": 10, "pick": "Navy", "away": "Army", "home": "Navy",
         "line": -1.5, "status": "final", "winner": "Army", "tiebreaker": True,
         "start": "2026-11-07T14:00:00", "reason": "x", "blurb": "y"},
    ]}
    s = chat.summarize_picky_player(data, 11, dt.datetime(2026, 11, 14, 18, 0))
    assert s["season"] == [1, 0], s                      # tiebreaker never counts
    assert [p["label"] for p in s["this_week"]] == ["Boise State -13.5"]
    assert s["hidden_count"] == 1
    block = chat.picky_player_block({"picky_player": s})
    assert "secret" not in block and "UTSA" not in block, block
    assert "NOT in the standings" in block and "1-0" in block


def test_prompt_unchanged_when_picky_is_not_playing():
    assert chat.picky_player_block({}) == ""
    assert chat.picky_player_block({"picky_player": None}) == ""


# ---------------------------------------------------------------- migration + frontend

MIGRATION = ROOT / "supabase/migrations/20261114180000_picky_the_player.sql"


def test_migration_is_additive_only():
    sql = re.sub(r"--[^\n]*", "", MIGRATION.read_text()).lower()
    for bad in (r"\bdelete\s+from\b", r"\bupdate\s+public\.", r"\btruncate\b", r"\bdrop\s+table\b",
                r"\bdrop\s+column\b", r"\balter\s+column\b", r"\binsert\s+into\b"):
        assert not re.search(bad, sql), bad
    assert "is_exhibition boolean not null default false" in sql
    # every policy it adds is restrictive: it can only ever tighten access
    pols = re.findall(r"create policy[^;]+;", sql)
    assert pols and all("as restrictive" in p for p in pols), pols


def test_frontend_picky_player_is_behind_the_flag():
    # the player row has its own flag (2805349): PICKY_PLAYER_ENABLED / pickyPlayerOn(), separate
    # from the web chat's PICKY_ENABLED / pickyOn()
    flag = (ROOT / "js" / "pickyFlag.js").read_text()
    assert re.search(r"const PICKY_PLAYER_ENABLED = (true|false);", flag)
    assert "return PICKY_PLAYER_ENABLED;" in flag.split("export function pickyPlayerOn()")[1].split("}")[0]
    for page in ("index.html", "picks.html", "stats.html"):
        src = (ROOT / page).read_text()
        hooks = [m.start() for m in re.finditer(r"import\('\./js/pickyPlayer\.js'\)", src)]
        assert hooks, page
        for h in hooks:
            window = src[max(0, h - 400):h]
            assert "if (!pickyPlayerOn()) return;" in window, f"{page}: pickyPlayer loaded outside the flag"
        assert "pickyPlayer.js" not in re.sub(r"import\('\./js/pickyPlayer\.js'\)", "", src), page


def test_live_rls_hides_exhibition():
    import httpx
    try:
        anon = subprocess.run(["security", "find-generic-password", "-a", "drake", "-s",
                               "CFB_PICKEM__SUPABASE_ANON_KEY", "-w"],
                              capture_output=True, text=True, timeout=10).stdout.strip()
    except OSError:
        anon = ""
    if not anon:
        print("      (skipped: no anon key)")
        return
    h = {"apikey": anon, "Authorization": f"Bearer {anon}"}
    rest = f"{pp.SUPABASE_URL}/rest/v1"
    r = httpx.post(f"{rest}/rpc/picky_player", headers=h, json={"p_season": 2026}, timeout=15)
    if r.status_code != 200:
        print("      (skipped: migration not applied yet)")
        return
    player = r.json()
    teams = httpx.get(f"{rest}/teams?select=team_id,team_name", headers=h, timeout=15).json()
    assert all(t["team_name"] != pp.PICKY_TEAM_NAME for t in teams), "Picky visible in teams"
    if player:
        tid = player["team_id"]
        rows = httpx.get(f"{rest}/picks?select=id&team_id=eq.{tid}", headers=h, timeout=15).json()
        assert rows == [], "Picky's picks visible to a normal read"
        for p in player["picks"]:   # anon only ever gets revealed picks
            ok = httpx.post(f"{rest}/rpc/picks_open_for_game", headers=h,
                            json={"p_game_id": p["game_id"]}, timeout=15).json()
            assert ok is False, f"unrevealed pick {p['game_id']} leaked"


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  ok  {t.__name__}")
        except Exception as e:  # noqa: BLE001
            print(f"FAIL  {t.__name__}: {type(e).__name__}: {e}")
            failed += 1
    print()
    print("ALL GREEN" if failed == 0 else f"{failed} FAILED")
    sys.exit(0 if failed == 0 else 1)
