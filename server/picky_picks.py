"""Picky the player: exhibition auto-picks for every pool game.

Picky shows up on the scoreboard as a bot team (teams.is_exhibition = true)
but never counts for money, the jackpot, tiebreakers or placement.

Pick logic is deterministic and explainable; the local model only writes the
trash-talk line after the side is already chosen:
  1. VALUE: the ESPN market vs the pool line (chat.py's _market_gap). A gap of
     a point or more means one side of the pool line is mispriced; take it.
  2. SP+: no edge, so project the home margin from CFBD SP+ ratings (cache
     only, never the API) plus home field, and take the side that covers.
  3. FPI: same idea with ESPN FPI's margin from all_games.
  4. Still level: take the points.

Runs from LaunchAgent com.pickem.picky-picks. Idempotent: a game that already
has a Picky pick is never touched again, and nothing is written for a game
that has kicked off. Local Ollama only; this costs nothing to run.

  python picky_picks.py run [--dry-run] [--now]   submit picks due before lock
  python picky_picks.py pending --out FILE        catch-up picks to a file, no DB
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import logging
import os
import re
import secrets
import subprocess
import sys
import zoneinfo
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent))
import espn  # noqa: E402
import ollama_gate  # noqa: E402
from chat import _market_gap, same_team  # noqa: E402

try:  # CFBD cache reader; optional so a missing cache never blocks picks
    import cfbd  # noqa: E402
except ImportError:  # pragma: no cover
    cfbd = None

log = logging.getLogger("picky-picks")

SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://vopdioszofwdkwnujtiq.supabase.co")
OLLAMA = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
MODEL = os.environ.get("PICKEM_CHAT_MODEL", "gemma4:12b-it-qat")
PICKY_TEAM_NAME = "Picky"
BOT_EMAIL = "picky-bot@example.com"  # reserved domain: no mail can ever go out
# Catch-up picks made outside the DB (week 11, 2026); inserted once the
# migration is live, only where generated_at is before kickoff.
PENDING_FILE = os.environ.get("PICKY_PENDING_FILE",
                              str(Path.home() / ".picky-player" / "picky_picks_pending.json"))
# Weekly picks go in this close to the lock, so the value check reads the
# freshest market. The hourly LaunchAgent lands 2-3h before lock, then retries.
PICK_WINDOW = dt.timedelta(hours=float(os.environ.get("PICKY_PICK_WINDOW_H", "3")))
HOME_FIELD = 2.5  # points, the usual SP+ home-field number
CT = zoneinfo.ZoneInfo("America/Chicago")
UTC = dt.timezone.utc


# -----------------------------------------------------------------------------
# time / locks
# -----------------------------------------------------------------------------


def kickoff_utc(game: dict) -> dt.datetime | None:
    """all_games."Start (CT)" is a naive Central timestamp."""
    s = game.get("Start (CT)") or game.get("start")
    if not s:
        return None
    t = dt.datetime.fromisoformat(str(s))
    if t.tzinfo is None:
        t = t.replace(tzinfo=CT)
    return t.astimezone(UTC)


def lock_utc(game: dict, week_games: list[dict]) -> dt.datetime | None:
    """When humans lose the ability to pick this game, mirroring
    picks_open_for_game(): regular season (week <= 15) locks the whole week at
    its first kickoff (pool or tiebreaker game); playoffs lock per game."""
    if int(game.get("week") or 0) > 15:
        return kickoff_utc(game)
    ks = [kickoff_utc(g) for g in week_games
          if (g.get("picked") or g.get("tiebreaker")) and kickoff_utc(g)]
    return min(ks) if ks else None


def kickoff_lock(game: dict, _week_games=None) -> dt.datetime | None:
    """Per-game kickoff. Only for this week's catch-up picks (Charlie asked
    for the games that hadn't kicked off yet); weekly runs use lock_utc."""
    return kickoff_utc(game)


def is_open(game: dict, now: dt.datetime, week_games: list[dict] | None = None,
            lock=lock_utc) -> bool:
    if (game.get("Status") or "scheduled") not in ("scheduled", "pre"):
        return False
    k = lock(game, week_games if week_games is not None else [game])
    return k is not None and now < k


# -----------------------------------------------------------------------------
# pick logic (pure; tested in test_picky_player.py)
# -----------------------------------------------------------------------------


def _sp(idx, name):
    if not idx or cfbd is None:
        return None
    t = cfbd.resolve_team(name, idx)
    r = ((t or {}).get("sp") or {}).get("rating")
    return float(r) if r is not None else None


def _fmt(n: float) -> str:
    return f"{n:g}"


def decide(game: dict, notes: dict | None, idx: dict | None) -> dict:
    """Pick a side of the pool line. Returns {pick, method, reason, ...}."""
    away, home = game["Away"], game["Home"]
    line = float(game.get("line") or 0)  # home adjustment, negative = home favored
    out = {"game_id": game["GameId"], "away": away, "home": home, "line": line}

    mg = _market_gap(notes or {}, line)
    if mg is not None and abs(mg[1]) >= 1:
        gap = mg[1]
        side = home if gap > 0 else away
        return out | {"pick": side, "method": "value", "edge": round(abs(gap), 1),
                      "reason": f"market rates {side} {_fmt(abs(gap))} points better than the pool line"}

    def by_margin(home_margin: float, method: str, src: str):
        cover = home_margin + line  # home covers when home + line > away
        if abs(cover) < 0.05:
            return None
        side = home if cover > 0 else away
        fav = home if home_margin >= 0 else away
        return out | {"pick": side, "method": method, "edge": round(abs(cover), 1),
                      "reason": f"{src} projects {fav} by {_fmt(round(abs(home_margin), 1))}, "
                                f"so {side} covers the pool line by {_fmt(round(abs(cover), 1))}"}

    sa, sh = _sp(idx, away), _sp(idx, home)
    if sa is not None and sh is not None:
        r = by_margin(sh - sa + HOME_FIELD, "sp+", "SP+")
        if r:
            return r
    if game.get("fpi_margin") is not None:
        r = by_margin(float(game["fpi_margin"]), "fpi", "FPI")
        if r:
            return r
    dog = away if line < 0 else home
    return out | {"pick": dog, "method": "points", "edge": 0.0,
                  "reason": "no edge anywhere, so take the points"}


def pick_label(d: dict) -> str:
    """'Boise State -13.5' as written on the pool line."""
    line, home = d["line"], d["home"]
    if line == 0:
        return f"{d['pick']} (pick'em)"
    fav = home if line < 0 else d["away"]
    sign = "-" if d["pick"] == fav else "+"
    return f"{d['pick']} {sign}{_fmt(abs(line))}"


# -----------------------------------------------------------------------------
# trash talk (local model only)
# -----------------------------------------------------------------------------

BANNED = re.compile(
    r"\b(mom|mother|mama|dad|father|wife|wives|husband|girlfriend|boyfriend|sister|brother|kids?|"
    r"family|gay|queer|fag\w*|homo\w*|trans\w*|lesbian|retard\w*|n[i1]gg\w*|black|white|asian|mexican|"
    r"jew\w*|muslim|christian|god|jesus|allah|church|ugly|fat|bald|weight|face|looks?|cancer|died|dead)\b",
    re.I,
)

BLURB_SYSTEM = (
    "You are Picky, the foul-mouthed heel of a college football pick'em league, and this week "
    "you're playing too. Write ONE trash-talk line (max 25 words) bragging about your pick and "
    "daring the humans to fade it. R-rated: swearing is encouraged. Talk about the teams, the "
    "line and the humans' bad picks only. Never: slurs, race, religion, sexuality, gender, "
    "disability, looks, weight, family, money or health. Do not invent stats; use only the "
    "facts given. No emoji, no hashtags, no quotes around it."
)

FALLBACK = "{label}. Fade me if you hate money, you mouth-breathing spreadsheet tourists."


def blurb_ok(text: str) -> bool:
    return bool(text) and len(text) <= 240 and not BANNED.search(text)


def write_blurb(d: dict, client: httpx.Client | None = None) -> str:
    label = pick_label(d)
    facts = (f"Game: {d['away']} at {d['home']}. Your pick: {label}. "
             f"Why: {d['reason']}.")
    c = client or httpx.Client(timeout=120)
    for attempt in range(3):
        try:
            with ollama_gate.slot_sync():  # batch job: wait for live texts + the shared low slot
                r = c.post(f"{OLLAMA}/api/chat", json={
                    # chat.py's num_ctx + keep_alive, so this reuses the loaded chat model instead of reloading it
                    "model": MODEL, "stream": False, "think": False,
                    "keep_alive": os.environ.get("PICKY_OLLAMA_KEEP_ALIVE", "24h"),
                    "options": {"temperature": 0.9, "seed": d["game_id"] + attempt, "num_ctx": 16384},
                    "messages": [{"role": "system", "content": BLURB_SYSTEM},
                                 {"role": "user", "content": facts}],
                })
            r.raise_for_status()
            text = r.json()["message"]["content"].strip().strip('"').strip()
            text = re.sub(r"\s+", " ", text)
            if blurb_ok(text):
                return text
            log.info("blurb rejected: %r", text)
        except (httpx.HTTPError, KeyError, ValueError) as e:
            log.warning("ollama blurb failed: %s", e)
    return FALLBACK.format(label=label)


# -----------------------------------------------------------------------------
# Supabase (service key, never committed)
# -----------------------------------------------------------------------------


def service_key() -> str:
    k = os.environ.get("SUPABASE_SERVICE_KEY")
    if k:
        return k
    return subprocess.run(
        ["security", "find-generic-password", "-a", "drake", "-s",
         "CFB_PICKEM__SUPABASE_SERVICE_KEY", "-w"],
        capture_output=True, text=True, check=True, timeout=10,
    ).stdout.strip()


class DB:
    def __init__(self, client: httpx.Client | None = None, key: str | None = None):
        key = key or service_key()
        self.h = {"apikey": key, "Authorization": f"Bearer {key}"}
        self.c = client or httpx.Client(timeout=30)

    def get(self, q: str) -> list:
        r = self.c.get(f"{SUPABASE_URL}/rest/v1/{q}", headers=self.h)
        r.raise_for_status()
        return r.json()

    def get_all(self, q: str, page: int = 1000) -> list:
        """Page past PostgREST's 1000-row cap."""
        out, off = [], 0
        while True:
            rows = self.get(f"{q}&limit={page}&offset={off}")
            out += rows
            if len(rows) < page:
                return out
            off += page

    def insert(self, table: str, rows: list[dict], on_conflict: str | None = None) -> list:
        h = self.h | {"Content-Type": "application/json", "Prefer": "return=representation"}
        q = f"{SUPABASE_URL}/rest/v1/{table}"
        if on_conflict:
            # ignore-duplicates: an existing pick is never overwritten
            h["Prefer"] += ",resolution=ignore-duplicates"
            q += f"?on_conflict={on_conflict}"
        r = self.c.post(q, headers=h, json=rows)
        r.raise_for_status()
        return r.json()

    # -- league reads --------------------------------------------------------

    def season(self) -> int:
        return self.get("all_games?select=cfb_season&order=cfb_season.desc&limit=1")[0]["cfb_season"]

    def open_weeks(self, season: int) -> list[int]:
        """Weeks this season that still have a non-final pool game."""
        rows = self.get(f"all_games?select=week&cfb_season=eq.{season}&picked=eq.true"
                        f"&Status=neq.final&order=week.asc")
        return sorted({r["week"] for r in rows})

    def pool_games(self, season: int, week: int) -> list[dict]:
        return self.get(
            'all_games?select=GameId,Away,Home,line,Status,week,tiebreaker,picked,fpi_margin,"Start (CT)"'
            f"&cfb_season=eq.{season}&week=eq.{week}"
            '&or=(picked.eq.true,tiebreaker.eq.true)&order="Start (CT)".asc')

    def picky_team_id(self) -> int | None:
        rows = self.get(f"teams?select=team_id&team_name=eq.{PICKY_TEAM_NAME}&is_exhibition=eq.true")
        return rows[0]["team_id"] if rows else None

    def picky_game_ids(self, team_id: int, game_ids: list[int]) -> set[int]:
        if not game_ids:
            return set()
        ids = ",".join(str(i) for i in game_ids)
        rows = self.get_all(f"picks?select=game_id&team_id=eq.{team_id}&game_id=in.({ids})&order=id.asc")
        return {r["game_id"] for r in rows}

    def migrated(self) -> bool:
        """Is 20261114180000_picky_the_player.sql live? picky_player() is the
        last thing it creates. Until then nothing is written, because a teams
        row without is_exhibition would show up as a normal ranked player."""
        r = self.c.post(f"{SUPABASE_URL}/rest/v1/rpc/picky_player", headers=self.h,
                        json={"p_season": 0})
        return r.status_code == 200

    def ensure_picky_team(self, dry_run: bool = False) -> int | None:
        """Picky's team row, created once. It gets a real (never used) auth
        user so user_id isn't null: a null-owner team is claimable."""
        tid = self.picky_team_id()
        if tid is not None or dry_run:
            return tid
        uid = self._bot_user_id()
        row = self.insert("teams", [{"team_name": PICKY_TEAM_NAME, "user_id": uid,
                                     "is_exhibition": True, "active": True}])
        log.info("created exhibition team %s (team_id %s)", PICKY_TEAM_NAME, row[0]["team_id"])
        return row[0]["team_id"]

    def _bot_user_id(self) -> str:
        h = self.h | {"Content-Type": "application/json"}
        r = self.c.post(f"{SUPABASE_URL}/auth/v1/admin/users", headers=h, json={
            "email": BOT_EMAIL, "email_confirm": True, "password": secrets.token_urlsafe(32),
            "user_metadata": {"bot": True, "name": PICKY_TEAM_NAME}})
        if r.status_code in (200, 201):
            return r.json()["id"]
        page = 1
        while True:  # already exists (an earlier half-finished run): find it
            users = self.c.get(f"{SUPABASE_URL}/auth/v1/admin/users?page={page}&per_page=200",
                               headers=self.h).json().get("users") or []
            for u in users:
                if (u.get("email") or "").lower() == BOT_EMAIL:
                    return u["id"]
            if len(users) < 200:
                r.raise_for_status()
                raise RuntimeError(f"could not create or find {BOT_EMAIL}")
            page += 1


# -----------------------------------------------------------------------------
# commands
# -----------------------------------------------------------------------------


def load_idx():
    if cfbd is None:
        return None
    try:
        return cfbd.load_index()
    except Exception as e:  # cache missing or mid-refresh: fall through to FPI
        log.warning("cfbd cache unavailable: %s", e)
        return None


def due_games(games: list[dict], now: dt.datetime, skip=frozenset(), lock=lock_utc,
              window: dt.timedelta | None = None) -> list[dict]:
    """Open games with a line that Picky hasn't picked; with `window`, only
    those whose lock is that close (pick late, on the freshest market)."""
    out = []
    for g in games:
        if g["GameId"] in skip or g.get("line") is None or not is_open(g, now, games, lock):
            continue
        if window is not None and lock(g, games) - now > window:
            continue
        out.append(g)
    return out


def make_picks(games: list[dict], now: dt.datetime, skip=frozenset(), notes: dict | None = None,
               idx=None, blurbs: bool = True, lock=lock_utc,
               window: dt.timedelta | None = None) -> list[dict]:
    """Picks for every due game. generated_at is stamped per pick after the
    decision and must be before that game's lock, or the pick is dropped."""
    todo = due_games(games, now, skip, lock, window)
    if not todo:
        return []
    if notes is None:
        notes = asyncio.run(espn.game_notes([g["GameId"] for g in todo]))
    out = []
    with httpx.Client(timeout=120) as oc:
        for g in todo:
            d = decide(g, notes.get(g["GameId"]) or {}, idx)
            d["label"] = pick_label(d)
            d["tiebreaker"] = bool(g.get("tiebreaker"))
            d["week"] = g["week"]
            d["blurb"] = write_blurb(d, oc) if blurbs else ""
            d["kickoff_utc"] = kickoff_utc(g).isoformat()
            d["lock_utc"] = lock(g, games).isoformat()
            d["generated_at"] = dt.datetime.now(UTC).isoformat()
            if dt.datetime.fromisoformat(d["generated_at"]) >= lock(g, games):
                log.warning("game %s locked while picking; dropped", g["GameId"])
                continue
            out.append(d)
    return out


def pick_rows(team_id: int, picks: list[dict]) -> list[dict]:
    return [{"team_id": team_id, "game_id": p["game_id"], "week": p["week"], "pick": p["pick"],
             "by_slots": False, "jackpot": False} for p in picks]


def blurb_rows(team_id: int, picks: list[dict]) -> list[dict]:
    return [{"team_id": team_id, "game_id": p["game_id"], "pick": p["pick"], "method": p["method"],
             "reason": p["reason"], "blurb": p["blurb"], "generated_at": p["generated_at"]}
            for p in picks]


def made_in_time(p: dict) -> bool:
    """generated_at before the lock. Catch-up picks from the pending file
    carry only kickoff_utc, which is the rule they were made under."""
    deadline = p.get("lock_utc") or p["kickoff_utc"]
    return dt.datetime.fromisoformat(p["generated_at"]) < dt.datetime.fromisoformat(deadline)


def submit(db: DB, team_id: int, picks: list[dict], dry_run: bool = False,
           require_open: bool = True, now: dt.datetime | None = None) -> int:
    """Insert picks + blurbs. Only picks made before their lock; with
    require_open, also only while the game is still open. A game Picky already
    has a pick on is skipped, and ignore-duplicates means a racing second run
    can't overwrite anything either."""
    now = now or dt.datetime.now(UTC)
    picks = [p for p in picks if made_in_time(p) and (
        not require_open or now < dt.datetime.fromisoformat(p.get("lock_utc") or p["kickoff_utc"]))]
    have = db.picky_game_ids(team_id, [p["game_id"] for p in picks])
    picks = [p for p in picks if p["game_id"] not in have]
    if dry_run or not picks:
        return len(picks)
    db.insert("picks", pick_rows(team_id, picks), on_conflict="team_id,game_id")
    db.insert("picky_pick_blurbs", blurb_rows(team_id, picks), on_conflict="team_id,game_id")
    return len(picks)


def cmd_run(args) -> int:
    db = DB()
    if not db.migrated():
        log.info("picky_player() isn't live yet (migration 20261114180000 not applied); waiting")
        return 0
    team_id = db.ensure_picky_team(dry_run=args.dry_run)
    if team_id is None:
        log.info("dry run: %s team doesn't exist yet", PICKY_TEAM_NAME)
        return 0
    pending = Path(PENDING_FILE)
    if pending.exists():
        n = submit(db, team_id, json.loads(pending.read_text())["picks"],
                   dry_run=args.dry_run, require_open=False)
        if n:
            log.info("%s %d catch-up pick(s) from %s", "would insert" if args.dry_run else "inserted",
                     n, pending)
    season = db.season()
    now = dt.datetime.now(UTC)
    idx = load_idx()
    total = 0
    for week in db.open_weeks(season):
        games = db.pool_games(season, week)
        have = db.picky_game_ids(team_id, [g["GameId"] for g in games])
        picks = make_picks(games, now, skip=have, idx=idx, blurbs=not args.dry_run,
                           window=None if args.now else PICK_WINDOW)
        for p in picks:
            log.info("week %s: %s (%s: %s)", week, p["label"], p["method"], p["reason"])
        total += submit(db, team_id, picks, dry_run=args.dry_run)
        waiting = [g for g in due_games(games, now, have) if g["GameId"] not in {p["game_id"] for p in picks}]
        if waiting:
            first = min(lock_utc(g, games) for g in waiting)
            log.info("week %s: %d game(s) wait for the pick window (opens %s, lock %s)", week,
                     len(waiting), (first - PICK_WINDOW).isoformat(timespec="minutes"),
                     first.isoformat(timespec="minutes"))
    log.info("%s %d pick(s)", "would submit" if args.dry_run else "submitted", total)
    return 0


def cmd_pending(args) -> int:
    """Catch-up picks for games that haven't kicked off, written to a local
    file and not the DB. Append-only: a game already in the file keeps its
    original pick and timestamp."""
    path = Path(args.out)
    existing = json.loads(path.read_text()) if path.exists() else {"picks": []}
    done = {p["game_id"] for p in existing["picks"]}
    db = DB()
    season = db.season()
    week = args.week or db.open_weeks(season)[0]
    games = db.pool_games(season, week)
    now = dt.datetime.now(UTC)
    for g in games:
        if not is_open(g, now, games, kickoff_lock):
            log.info("missed (already kicked off): %s at %s", g["Away"], g["Home"])
    picks = make_picks(games, now, skip=done, idx=load_idx(), lock=kickoff_lock)
    existing["picks"] += picks
    existing.setdefault("season", season)
    existing.setdefault("week", week)
    path.write_text(json.dumps(existing, indent=2))
    for p in picks:
        print(f"{p['label']:<28} {p['method']:<6} {p['generated_at']}  {p['blurb']}")
    return 0


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--dry-run", action="store_true")
    r.add_argument("--now", action="store_true", help="ignore the pick window (still never after lock)")
    p = sub.add_parser("pending")
    p.add_argument("--out", required=True)
    p.add_argument("--week", type=int)
    args = ap.parse_args(argv)
    return {"run": cmd_run, "pending": cmd_pending}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
