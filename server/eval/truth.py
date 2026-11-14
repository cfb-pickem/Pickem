"""
Ground truth for the Picky exam, computed from real data at run time so the
exam stays valid as the season moves.

Sources (all read-only, none cost anything):
  - Supabase with the service key from the login keychain
    (CFB_PICKEM__SUPABASE_SERVICE_KEY). Scoring and standings reuse chat.py's
    own helpers, which test_standings.py pins to the leaderboard's rules.
  - ESPN's free summary endpoint (espn.py), the same notes Picky sees.
  - The CFBD local cache (cfbd.load_index()); never the CFBD API.
  - The league rules source, if chat.py loads one.

The truth holds EVERY pick for this week, including ones still hidden from
members. That is only used to detect leaks; it is never written to a report.
"""

from __future__ import annotations

import collections
import datetime as dt
import os
import re
import subprocess
import sys
from pathlib import Path

import httpx

SERVER = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVER))
import chat  # noqa: E402
import espn  # noqa: E402

try:
    import cfbd  # noqa: E402
except ImportError:  # CFBD not merged yet
    cfbd = None

CHARLIE_UID = "35e1c78c-0fbc-4d9e-bc8d-f7df5ea73908"  # team "Chuck P"


def service_key() -> str:
    return subprocess.check_output(
        ["security", "find-generic-password", "-a", "drake",
         "-s", "CFB_PICKEM__SUPABASE_SERVICE_KEY", "-w"], text=True).strip()


def wl(rec) -> str:
    return f"{rec[0]}-{rec[1]}"


class Truth:
    """Everything the grader needs, keyed by team name."""

    async def load(self):
        key = service_key()
        self.key = key
        chat._auth_headers = lambda _jwt: {"apikey": key, "Authorization": f"Bearer {key}"}
        async with httpx.AsyncClient() as c:
            self.teams = await chat._rest_get(
                c, "teams?select=team_id,team_name,user_id,active&order=team_name.asc", "x")
            # Same assembly as /chat, as Charlie (for standings, last week, games).
            self.ctx = await chat._build_context(c, "x", CHARLIE_UID)
            ids = [g["id"] for g in self.ctx["games_this_week"]]
            # Raw, unfiltered week picks: leak detection only.
            self._all_week_picks = await chat._rest_get(
                c, f"picks?select=team_id,game_id,pick&game_id=in.{chat._in_ids(ids)}", "x") if ids else []
        self.ctx["espn"] = await espn.game_notes([g["id"] for g in self.ctx["games_this_week"]])
        self.now = chat._now_ct_naive()
        self.name_by_tid = {t["team_id"]: t["team_name"] for t in self.teams}
        self.uid_by_team = {t["team_name"]: t["user_id"] for t in self.teams if t.get("user_id")}
        self.team_by_uid = {v: k for k, v in self.uid_by_team.items()}
        self.standings = self.ctx["standings_ytd"]
        self.weekly = self.ctx["weekly_records"]
        self.last_week = self.ctx["last_week"] or {}
        self.games = self.ctx["games_this_week"]
        self.season, self.week = self.ctx["season"], self.ctx["current_week"]
        self.cfbd_idx = None
        if cfbd is not None:
            try:
                self.cfbd_idx = cfbd.load_index()
            except Exception:  # noqa: BLE001
                self.cfbd_idx = None
        self.rules_text = self._rules_source()
        return self

    # ---- members -------------------------------------------------------------
    @property
    def members(self) -> list[str]:
        return [r["team"] for r in self.standings]

    def rec(self, team: str) -> tuple[int, int]:
        r = next(r for r in self.standings if r["team"] == team)
        return r["w"], r["l"]

    def row(self, team: str) -> dict:
        return next(r for r in self.standings if r["team"] == team)

    def at_rank(self, rank: int) -> list[str]:
        return [r["team"] for r in self.standings if r["rank"] == rank]

    @property
    def first(self) -> list[str]:
        return self.at_rank(self.standings[0]["rank"])

    @property
    def last(self) -> list[str]:
        return self.at_rank(self.standings[-1]["rank"])

    def is_last(self, team: str) -> bool:
        return self.row(team)["rank"] == self.standings[-1]["rank"]

    def lw_record(self, team: str) -> tuple[int, int] | None:
        res = []
        for g in self.last_week.get("games") or []:
            pk = (g.get("picks") or {}).get(team)
            if pk is not None:
                res.append(chat.same_team(pk, g.get("covered")))
        return (sum(res), len(res) - sum(res)) if res else None

    def known_records(self, team: str) -> set[str]:
        """Every W-L that is literally true about this team."""
        out = {wl(self.rec(team))} if team in self.members else set()
        for w, l in (self.weekly.get(team) or {}).values():
            out.add(f"{w}-{l}")
        lw = self.lw_record(team)
        if lw:
            out.add(wl(lw))
        return out

    def neutral_scores(self) -> set[str]:
        """Game scores and team records that can sit next to a member's name
        legitimately ('TG took SMU, who won 30-27')."""
        out = set()
        for g in self.last_week.get("games") or []:
            if g.get("away_pts") is not None:
                a, h = chat._num(g["away_pts"]), chat._num(g["home_pts"])
                out |= {f"{a}-{h}", f"{h}-{a}"}
        for notes in (self.ctx.get("espn") or {}).values():
            for side in ("away", "home"):
                t = (notes or {}).get(side) or {}
                if t.get("record"):
                    out.add(t["record"])
                for s in t.get("last5") or []:
                    out |= set(re.findall(r"\d{1,3}-\d{1,3}", s))
        return out

    # ---- this week -------------------------------------------------------------
    def _game_started(self, g: dict) -> bool:
        if g.get("winner"):
            return True
        raw = {"Status": g.get("status"), "Start (CT)": g.get("start")}
        return chat._game_is_locked(raw, self.now)

    def locked(self, g: dict) -> bool:
        """The site's rule (js/kickoff.js, index.html): in the regular season
        (weeks 0-15) the whole week locks and is revealed as soon as ANY of
        its games has started; in the playoffs each game on its own."""
        if isinstance(self.week, int) and self.week <= 15:
            return any(self._game_started(x) for x in self.games)
        return self._game_started(g)

    @property
    def hidden_games(self) -> list[dict]:
        return [g for g in self.games if not self.locked(g)]

    @property
    def open_games(self) -> list[dict]:
        return [g for g in self.games if self.locked(g)]

    def pick_of(self, team: str, game_id) -> str | None:
        tid = next((t["team_id"] for t in self.teams if t["team_name"] == team), None)
        return next((p["pick"] for p in self._all_week_picks
                     if p["team_id"] == tid and p["game_id"] == game_id), None)

    def my_picks(self, team: str) -> list[tuple[dict, str]]:
        return [(g, self.pick_of(team, g["id"])) for g in self.games if self.pick_of(team, g["id"])]

    def notes(self, g: dict) -> dict:
        return (self.ctx.get("espn") or {}).get(g["id"]) or {}

    def pool_line(self, g: dict) -> str:
        return chat.pool_line(g["away"], g["home"], g.get("line"))

    def favorite(self, g: dict) -> tuple[str, float] | None:
        try:
            ln = float(g.get("line"))
        except (TypeError, ValueError):
            return None
        if ln == 0:
            return None
        return (g["home"] if ln < 0 else g["away"]), abs(ln)

    def value_side(self, g: dict) -> tuple[str, float] | None:
        """(team, points) of the value side vs the ESPN market, or None."""
        mg = chat._market_gap(self.notes(g), g.get("line"))
        if mg is None or abs(mg[1]) < 1:
            return None
        return (g["home"] if mg[1] > 0 else g["away"]), abs(mg[1])

    def has_market(self, g: dict) -> bool:
        return chat._market_gap(self.notes(g), g.get("line")) is not None

    def verdict(self, g: dict, pick: str) -> str | None:
        """'value' / 'wrong' / 'none', the same call pick_verdict() makes."""
        if not self.has_market(g):
            return None
        vs = self.value_side(g)
        if vs is None:
            return "none"
        return "value" if chat.same_team(pick, vs[0]) else "wrong"

    def other_side(self, g: dict, pick: str) -> str:
        return g["home"] if chat.same_team(pick, g["away"]) else g["away"]

    def leaders(self, g: dict) -> dict:
        """{'Passing Yards': (team, player, yds_str)}: ESPN's per-team season
        leaders, the ones already in Picky's context."""
        out = {}
        for side in ("away", "home"):
            for s in (self.notes(g).get(side) or {}).get("leaders") or []:
                m = re.match(r"([^:]+):\s*(.+?)\s*\((.*)\)", s)
                if m:
                    out[(g[side], m.group(1))] = (m.group(2), m.group(3))
        return out

    # ---- CFBD ----------------------------------------------------------------
    def cfbd_player(self, teams: list[str], cat: str, key: str, rank: int = 0):
        """(name, team, value) of the rank-th best player in `cat`/`key` on
        `teams`, from the CFBD cache. None without a cache."""
        idx = self.cfbd_idx
        if not idx:
            return None
        names = {cfbd.norm(cfbd._resolved_name(t, idx)) for t in teams}
        pool = [p for n in names for p in idx["by_team"].get(n, [])
                if isinstance((p["stats"].get(cat) or {}).get(key), (int, float))]
        pool.sort(key=lambda p: -p["stats"][cat][key])
        if len(pool) <= rank:
            return None
        p = pool[rank]
        return p["name"], p["team"], p["stats"][cat][key]

    def cfbd_national(self, cat: str, key: str, exclude_teams: list[str]):
        idx = self.cfbd_idx
        if not idx:
            return None
        ex = {cfbd.norm(cfbd._resolved_name(t, idx)) for t in exclude_teams}
        pool = [p for p in idx["players"] if cfbd.norm(p["team"]) not in ex
                and isinstance((p["stats"].get(cat) or {}).get(key), (int, float))]
        pool.sort(key=lambda p: -p["stats"][cat][key])
        p = pool[0]
        return p["name"], p["team"], p["stats"][cat][key]

    @property
    def chat_has_cfbd(self) -> bool:
        return hasattr(chat, "cfbd") and self.cfbd_idx is not None

    # ---- rules -----------------------------------------------------------------
    def _rules_source(self) -> str | None:
        for name in ("league_rules.md", "rules.md", "league_rules.txt"):
            p = SERVER / name
            if p.exists():
                return p.read_text()
        txt = getattr(chat, "LEAGUE_RULES", None)
        return txt if isinstance(txt, str) else None


def num_variants(v) -> list[str]:
    """3169 -> ['3169', '3,169']; 7.5 -> ['7.5']."""
    try:
        f = float(str(v).replace(",", ""))
    except ValueError:
        return [str(v)]
    if f != int(f):
        return [chat._num(f)]
    n = int(f)
    return list({str(n), f"{n:,}"})
