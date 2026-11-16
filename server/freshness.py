"""
One freshness rule book for every data source text-Picky reads (Charlie,
2026-11-16: "make sure all the data always is fresh. I never want to worry
about him using stale data").

Each source has
  max_age   seconds a value counts as current (tighter on game days);
  hard      past this it's dropped; between max_age and hard it's only
            usable hedged ("as of Tue 9:15 AM"), never stated as current;
  fetched_at where the cache records when it was last refreshed (embedded
            field, sqlite column, file mtime, or passed in by the caller
            for in-process caches);
  agent     the LaunchAgent that refreshes it (kickstarted on demand when a
            reply finds it stale and the refresh is cheap; the feed monitor
            uses it for retries and repairs).

Reply time (chat.py): check(source[, fetched_at]) -> Verdict(status, use,
hedge, as_of). fresh -> use as is; stale -> use only with "as of <time>"
and kick the refresher if cheap; dead/missing -> drop the rider.

Interface (BUILD-COMMON): max_age(source), fresh(source, fetched_at),
as_of(source). register(Source(...)) adds a feed later (game-day live mode,
a lane's artifact). CLI: `freshness.py status` prints every source's age.
feed_monitor.py (LaunchAgent com.pickem.feed-monitor) kicks and, past that,
has Claude repair a broken refresher inside the feed's `files` allowlist.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

log = logging.getLogger("picky.freshness")

CT = ZoneInfo("America/Chicago")
HERE = Path(__file__).resolve().parent
PRIVATE = Path(os.environ.get("PICKY_PRIVATE_DIR", "/Users/Agent/cfb-pickem-private"))
CACHE = Path(os.environ.get("PICKY_CACHE_DIR", HERE / "cache"))

M, H, D = 60, 3600, 86400


@dataclass(frozen=True)
class Source:
    name: str
    max_age: tuple[float, float]    # (normal day, game day) seconds
    hard: tuple[float, float]       # past this: drop
    stamp: str = "caller"           # caller | json:<field> | mtime | sqlite
    path: Path | None = None
    agent: str | None = None        # refresher LaunchAgent label
    on_demand: bool = False         # cheap enough to kickstart from a reply
    monitor: str | None = None      # age | exit | None (feed_monitor.py)
    files: tuple[str, ...] = ()     # repair allowlist (repo-relative)
    tests: tuple[str, ...] = ()     # tests a repair must pass (server/)


def _p(env: str, default: Path) -> Path:
    return Path(os.environ.get(env, default))


def _sources() -> dict[str, Source]:
    S = [
        # in-process, refetched by chat.py on its own TTLs (on demand by design)
        Source("live", (3 * M, 3 * M), (15 * M, 15 * M)),            # score feed (SourceUpdatedAt)
        Source("all_games", (60, 30), (15 * M, 10 * M)),             # Supabase rows, read per request
        Source("standings", (60, 30), (15 * M, 10 * M)),             # computed from all_games per request
        Source("lines", (30 * M, 10 * M), (6 * H, 3 * H)),           # ESPN market line (10-min TTL)
        Source("weather", (H, H), (6 * H, 6 * H)),                   # ESPN / Open-Meteo forecast
        Source("splits", (30 * M, 20 * M), (6 * H, 3 * H)),          # DK public betting splits
        Source("rankings", (H, H), (8 * D, 8 * D)),                  # ESPN polls; releases are dated
        # file caches refreshed by LaunchAgents
        Source("news", (H, 45 * M), (24 * H, 12 * H), "json:fetched_at",
               _p("PICKY_NEWS_CACHE", CACHE / "news.json"), "com.pickem.news-refresh", True, "age",
               ("server/news.py", "server/com.pickem.news-refresh.plist"), ("test_news.py",)),
        Source("lore", (3 * H, H), (3 * D, D), "json:fetched_at",
               _p("PICKY_LORE_CACHE", CACHE / "lore.json"), "com.pickem.lore-refresh", True, "age",
               ("server/lore.py", "server/com.pickem.lore-refresh.plist"), ("test_lore.py",)),
        Source("cfbd", (26 * H, 26 * H), (4 * D, 4 * D), "sqlite",
               _p("PICKY_CFBD_DB", CACHE / "cfbd.sqlite"), "com.pickem.cfbd-refresh", False, "age",
               ("server/cfbd.py", "server/com.pickem.cfbd-refresh.plist"), ("test_cfbd.py",)),
        Source("dossier", (3 * D, D), (8 * D, 4 * D), "caller",
               _p("PICKY_DOSSIER_DIR", CACHE / "dossiers"), "com.pickem.picky-dossier", False, "exit",
               ("server/dossier.py", "server/analyst.py", "server/com.pickem.picky-dossier.plist"),
               ("test_dossier.py", "test_analyst.py")),
        Source("league_material", (8 * D, 8 * D), (8 * D, 8 * D), "json:generated_at",
               _p("PICKY_LEAGUE_MATERIAL", PRIVATE / "league_material.json"), "com.pickem.league-material",
               False, "exit", ("server/league_material.py", "server/banter.py",
                               "server/com.pickem.league-material.plist"), ("test_analyst.py",)),
        Source("banter", (5 * D, 5 * D), (14 * D, 14 * D), "json:generated_at",
               _p("PICKY_BANTER_BANK", PRIVATE / "banter_bank.json"), "com.pickem.league-material"),
        # curated by offline claude -p runs (Sun + Wed); opinions / slow facts
        Source("hot_takes", (4.5 * D, 4.5 * D), (10 * D, 10 * D), "mtime",   # Wed -> Sun is 4 days
               _p("PICKY_HOT_TAKES", PRIVATE / "hot_takes_bank.md"), "com.charlie.picky-hottakes", False, "age"),
        # same limits as the voice lane's INSIDER_STALE_DAYS / INSIDER_MAX_AGE_DAYS
        Source("insider", (8 * D, 8 * D), (21 * D, 21 * D), "mtime",
               _p("PICKY_INSIDER", PRIVATE / "insider_bank.md"), "com.charlie.picky-hottakes", False, "age"),
        # other lanes' artifacts (registered here so they share the rules)
        Source("burn_book", (5 * D, 5 * D), (14 * D, 14 * D), "json:generated_at",
               _p("PICKY_BURN_BOOK", PRIVATE / "burn_book.json")),
        Source("game_cards", (6 * H, H), (2 * D, 12 * H), "json:generated_at",
               _p("PICKY_GAME_CARDS", CACHE / "game_cards.json")),
        # game-day live mode (job 113039-8f5174, branch picky-gameday-live; not on main yet).
        # live_games: the poller's board file (written every <= 5 min, every 20 s while games
        # are live); the monitor only watches it once com.pickem.live-games is loaded.
        # live_game: one game's data, ts passed by the caller; matches live_games.MAX_AGE.
        Source("live_games", (6 * M, 3 * M), (30 * M, 15 * M), "json:updated_ts",
               _p("PICKY_LIVE_CACHE", CACHE / "live_games.json"), "com.pickem.live-games", False, "age",
               ("server/live_games.py", "server/com.pickem.live-games.plist"), ("test_live_games.py",)),
        Source("live_game", (90, 90), (10 * M, 10 * M)),
    ]
    return {s.name: s for s in S}


SOURCES = _sources()
CFBD_CORE = ("player_stats", "team_stats", "advanced", "sp", "records", "ats", "lines")
NEVER_REPAIR = ("server/chat.py", "server/picky_brain.py", "server/group_chat.py")


def register(src: Source) -> Source:
    """Hook for feeds added later (a new cache, a lane's artifact): same rules,
    reply-time check and monitor/repair as the built-ins. Re-registering a
    name replaces it."""
    if any(f in NEVER_REPAIR for f in src.files):
        raise ValueError(f"{src.name}: repair allowlist can't include {NEVER_REPAIR}")
    SOURCES[src.name] = src
    return src


def source(name: str) -> Source:
    if name not in SOURCES:
        raise KeyError(f"unknown freshness source {name!r}")
    return SOURCES[name]


# -----------------------------------------------------------------------------
# clock
# -----------------------------------------------------------------------------

def _now(now: float | dt.datetime | None = None) -> float:
    if now is None:
        return time.time()
    return now.timestamp() if isinstance(now, dt.datetime) else float(now)


def game_day(now: float | dt.datetime | None = None) -> bool:
    """Friday noon through Sunday 3 AM CT: lines, injuries and weather move."""
    t = dt.datetime.fromtimestamp(_now(now), CT)
    return t.weekday() == 5 or (t.weekday() == 4 and t.hour >= 12) or (t.weekday() == 6 and t.hour < 3)


def max_age(name: str, now: float | dt.datetime | None = None) -> float:
    s = source(name)
    return s.max_age[1] if game_day(now) else s.max_age[0]


def hard_age(name: str, now: float | dt.datetime | None = None) -> float:
    s = source(name)
    return s.hard[1] if game_day(now) else s.hard[0]


# -----------------------------------------------------------------------------
# fetched_at
# -----------------------------------------------------------------------------

def _epoch(v) -> float | None:
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        d = dt.datetime.fromisoformat(str(v))
    except ValueError:
        try:  # a bare date ("2026-11-16"): count it as that day's start, CT
            d = dt.datetime.combine(dt.date.fromisoformat(str(v)), dt.time())
        except ValueError:
            return None
    return (d if d.tzinfo else d.replace(tzinfo=CT)).timestamp()


def fetched_at(name: str) -> float | None:
    """Epoch seconds the source was last refreshed, from its own cache.
    None for in-process sources (the caller passes it) or a missing cache."""
    s = source(name)
    p = s.path
    if p is None or s.stamp == "caller":
        return None
    try:
        if s.stamp == "mtime":
            return p.stat().st_mtime
        if s.stamp == "sqlite":
            con = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=2)
            try:
                q = ",".join("?" * len(CFBD_CORE))  # the daily pulls; venues is static, extras optional
                row = con.execute(f"SELECT MIN(fetched_at) FROM datasets WHERE name IN ({q})",
                                  CFBD_CORE).fetchone()
            finally:
                con.close()
            return float(row[0]) if row and row[0] is not None else None
        if s.stamp.startswith("json:"):
            d = json.loads(p.read_text())
            v = _epoch(d.get(s.stamp[5:])) if isinstance(d, dict) else None
            return v if v is not None else p.stat().st_mtime
    except (OSError, ValueError, sqlite3.Error):
        return None
    return None


def age(name: str, at: float | None = None, now: float | dt.datetime | None = None) -> float | None:
    at = fetched_at(name) if at is None else _epoch(at)
    return None if at is None else max(0.0, _now(now) - at)


def fresh(name: str, at=None, now: float | dt.datetime | None = None) -> bool:
    a = age(name, at, now)
    return a is not None and a <= max_age(name, now)


def due(name: str, now: float | dt.datetime | None = None, margin: float = 10 * M) -> bool:
    """For refreshers on a short StartInterval: run when the cache is within
    `margin` of max_age (so game days refresh more often), or missing."""
    a = age(name, now=now)
    return a is None or a >= max_age(name, now) - margin


def as_of(name: str, at=None, now: float | dt.datetime | None = None) -> str:
    """'9:17 AM CT' today, 'Tue 9:17 AM' this week, else 'Nov 12'."""
    at = fetched_at(name) if at is None else _epoch(at)
    if at is None:
        return "unknown"
    t = dt.datetime.fromtimestamp(at, CT)
    n = dt.datetime.fromtimestamp(_now(now), CT)
    if t.date() == n.date():
        return f"{t:%-I:%M %p} CT"
    if (n.date() - t.date()).days < 6:
        return f"{t:%a %-I:%M %p}"
    return f"{t:%b} {t.day}"


# -----------------------------------------------------------------------------
# reply-time check
# -----------------------------------------------------------------------------

@dataclass(frozen=True)
class Verdict:
    status: str      # fresh | stale | dead | missing
    use: bool        # False: drop the rider
    hedge: str       # "" or "as of <time>": say it, never state the data as current
    as_of: str


_kicked: dict[str, float] = {}
KICK_EVERY = 10 * M


def kick(name: str, now: float | None = None) -> bool:
    """Non-blocking `launchctl kickstart` of the source's refresher, at most
    once per KICK_EVERY per source. Off with PICKY_FRESHNESS_KICK=0 (tests,
    side sidecars)."""
    s = source(name)
    if not s.agent or os.environ.get("PICKY_FRESHNESS_KICK", "1") == "0":
        return False
    now = _now(now)
    if now - _kicked.get(name, 0.0) < KICK_EVERY:
        return False
    _kicked[name] = now
    try:
        subprocess.Popen(["/bin/launchctl", "kickstart", f"gui/{os.getuid()}/{s.agent}"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        log.info("freshness: %s stale, kicked %s", name, s.agent)
        return True
    except OSError as e:
        log.warning("freshness: kick %s failed: %s", s.agent, e)
        return False


def check(name: str, at=None, now: float | dt.datetime | None = None, refresh: bool = True) -> Verdict:
    """fresh: use. stale (past max_age, inside hard): use hedged 'as of'.
    dead (past hard) or missing: drop. Stale/dead/missing kicks a cheap
    refresher so the next reply has current data."""
    a = age(name, at, now)
    s = source(name)
    if a is not None and a <= max_age(name, now):
        return Verdict("fresh", True, "", as_of(name, at, now))
    if refresh and s.on_demand:
        kick(name)
    if a is None:
        return Verdict("missing", False, "", "unknown")
    when = as_of(name, at, now)
    if a <= hard_age(name, now):
        return Verdict("stale", True, f"as of {when}", when)
    return Verdict("dead", False, "", when)


def usable(name: str, at=None, now: float | dt.datetime | None = None) -> bool:
    return check(name, at, now).use


def rider(name: str, text: str, at=None, now: float | dt.datetime | None = None) -> str:
    """A brief rider with the freshness rule applied: unchanged if fresh,
    an explicit 'as of' instruction if stale, '' if dead or missing."""
    if not text:
        return text
    v = check(name, at, now)
    if not v.use:
        return ""
    if v.hedge:
        return (text + f"\n(This {name.replace('_', ' ')} data is {v.hedge}, not current: say 'as of "
                f"{v.as_of}' if you use it, never present it as the latest.)")
    return text


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------

def status_rows(now: float | None = None) -> list[dict]:
    out = []
    for name, s in SOURCES.items():
        if s.stamp == "caller":
            continue
        a = age(name, now=now)
        v = check(name, now=now, refresh=False)
        out.append({"source": name, "status": v.status, "age_h": None if a is None else round(a / H, 2),
                    "max_age_h": round(max_age(name, now) / H, 2), "hard_h": round(hard_age(name, now) / H, 2),
                    "as_of": v.as_of, "agent": s.agent})
    return out


def main(argv: list[str]) -> int:
    if argv[:1] == ["status"]:
        for r in status_rows():
            print(json.dumps(r))
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
