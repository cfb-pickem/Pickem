"""
Picky's proactive posts in the league iMessage group. Every post goes
through group_outbox.post() (kill switch, dry run, group detection, once-only
keys, send log); the watcher in the `picky` user sends them.

LaunchAgent com.pickem.picky-group-posts runs `group_posts.py tick` every 5
minutes. A tick detects the group, exits if paused, and otherwise decides what
is due (Central time):

  recap     Sunday 9:00 (until Mon 9:00), once per fully-final pool week:
            ONE text (max_chars_long): who won the week, the standings move,
            one award, Picky's own week. No per-game rundown, no playoff math.
  picks up  When a new week's 4 pool games appear (before lock): the "Week N
            picks are up" announcement (code template: lock time, the 4 lines
            with the favorite framed from `line`, "@Picky me or DM me"), then
            4+ min later the deep breakdown (week_breakdown.py: code-computed
            facts + forecast, Opus-phrased offline, every number verified in
            code). Posts 09:00-23:30 only (overnight drops wait for 9am).
            Keys picksup:<season>:<week> / breakdown:<season>:<week>.
  preview   Thursday 12:00 until the week's first kickoff. With a breakdown on
            file: a short refresh of what changed since (market moves of 1+,
            new dated injury/QB lines, forecast swings, a flipped lean), and no
            post at all when nothing changed. Without one (fallback): the
            slate, Picky's card and the film room's line.
  saturday  Saturday 09:30-13:00 when the week has a Saturday game: today's
            pool games, one text, at the first tick from 09:30. Picky's sides
            only for games already locked; before the lock it's the games and
            lines with "my sides show at kickoff" (HIDDEN SIDES).
  HIDDEN SIDES (Charlie, 2026-11-19 15:41: "picks are hidden till kickoff when
            they lock, not anything to do with all members"): Picky plays as an
            exhibition team, so no post shows his pick or lean in a game (preview
            card, Saturday sides, the film room's line, the breakdown's "Lean:"
            lines, a flipped lean) until that game's picks lock, the site's rule
            (League.game_locked: regular season the week's first kickoff,
            playoffs per game). Who has submitted plays no part. Posts still go
            out without his sides. Recaps and pings are post-lock: unchanged.
  remind    Before the weekly lock only (regular season: every pick locks at
            the week's first kickoff, picks_open_for_game()). First game on a
            Saturday: 07:00 that Saturday. First game any other day: 07:00
            that day (and 2 hours before kickoff only with config
            second_reminder: the straggler DMs cover it); if kickoff is too
            early for both (2 h before is before 08:00), the first moves to
            19:00 the evening before. Names whoever the site's submission_status() says
            hasn't submitted (config name_stragglers, default ON per Charlie);
            never anyone's picks. Skipped when everyone's in.
  nudges    straggler_nudges.py: DMs (not the group) ~2 h and ~30 min before the
            lock to members still showing "Not yet", after the 7:00 group reminder.
  pings     During game windows: when a pool game goes final (ESPN, keyless),
            ONE short line on who it helped or hurt. Finals are held while
            another pool game is still on (up to config ping_hold_min) so a
            Saturday is one or two combined pings, quiet hours 23:30-08:00.

LENGTH AND FREQUENCY (Charlie, 2026-11-19: "I just don't want the scheduled
texts to be too chatty" / "make sure the posts aren't too long"): every post is
ONE text of at most max_chars (280); the recap and the picks-up post at most
max_chars_long (450). group_outbox.post() enforces it (clip) and the daily cap
(default 4 per 24 h, recap / reminders / Saturday post first). The Thursday
preview only goes out when the picks-up post didn't (or as the short "what
changed" refresh). Knobs live in cfb-pickem-private/picky_group_config.json.

Writing, code first (Charlie, 2026-11-16: "auto feed pipelines for him so you
dont need an opus call every time"): preview, saturday, reminders and pings are
code templates over the computed facts (a few rotating wordings, picked by the
post's key, skipping any opener a recent post used), so they never call a model.
The film room's one-liner (the dossier's weekly analyst take) rides in the
preview while it's fresh. Only the weekly recap gets a model writer: the box's
local gemma (local_llm.py, behind the live-first Ollama gate; zero maintenance,
phase 8: no Claude), offline, at most one post (two drafts) a week. validate() rejects any number not in the facts, any member
not in the facts, repeated openers, and the known canon breakers; a rejected or
failed recap falls back to plain_recap, so every post still goes out with
Ollama down too.

CLI:
  group_posts.py tick [--dry-run]
  group_posts.py recap|preview|saturday [--week N] [--as-of ISO]   (print only)
  group_posts.py remind|pings [--as-of ISO]                         (print only)
  group_posts.py race [--team NAME]
  group_posts.py status | detect
  group_posts.py enable | disable      (enable = remove the kill switch, dry_run off)
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import hashlib
import json
import logging
import os
import re
import sys
import zoneinfo
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import group_outbox as ob  # noqa: E402
import playoff_race as pr  # noqa: E402
import rare_bits  # noqa: E402
import straggler_nudges  # noqa: E402
import week_breakdown  # noqa: E402

log = logging.getLogger("picky.posts")
CT = zoneinfo.ZoneInfo("America/Chicago")
UTC = dt.timezone.utc
LOG_PATH = HERE / "logs" / "group_posts.log"
DOSSIER_DIR = HERE / "cache" / "dossiers"
MATERIAL = ob.PRIVATE / "league_material.json"
SUPABASE_URL = "https://vopdioszofwdkwnujtiq.supabase.co"
ESPN_SUMMARY = "https://site.api.espn.com/apis/site/v2/sports/football/college-football/summary?event="
PICKY_TEAM = "Picky"


# -----------------------------------------------------------------------------
# league data (service key, read-only)
# -----------------------------------------------------------------------------


class DB:
    def __init__(self):
        import subprocess
        key = os.environ.get("SUPABASE_SERVICE_KEY") or subprocess.run(
            ["security", "find-generic-password", "-a", "drake", "-s", "CFB_PICKEM__SUPABASE_SERVICE_KEY", "-w"],
            capture_output=True, text=True, check=True, timeout=10).stdout.strip()
        self.h = {"apikey": key, "Authorization": f"Bearer {key}"}
        self.c = httpx.Client(timeout=30)

    def get(self, q: str) -> list:
        r = self.c.get(f"{SUPABASE_URL}/rest/v1/{q}", headers=self.h)
        r.raise_for_status()
        return r.json()

    def get_all(self, q: str, page: int = 1000) -> list:
        out, off = [], 0
        while True:
            rows = self.get(f"{q}&limit={page}&offset={off}")
            out += rows
            if len(rows) < page:
                return out
            off += page

    def rpc(self, fn: str, body: dict):
        r = self.c.post(f"{SUPABASE_URL}/rest/v1/rpc/{fn}", headers=self.h, json=body)
        r.raise_for_status()
        return r.json()


def kick(g: dict) -> dt.datetime | None:
    s = g.get("Start (CT)")
    try:
        t = dt.datetime.fromisoformat(str(s))
    except (TypeError, ValueError):
        return None
    return t.replace(tzinfo=CT) if t.tzinfo is None else t.astimezone(CT)


def is_final(g: dict) -> bool:
    return bool(g.get("winner")) or str(g.get("Status") or "").lower() == "final"


class League:
    """One read of everything a post needs. Picks are only read for games
    whose week has locked (final games, or a locked week), never before."""

    def __init__(self, db: DB, now: dt.datetime):
        self.db, self.now = db, now
        self.season = db.get("all_games?select=cfb_season&order=cfb_season.desc&limit=1")[0]["cfb_season"]
        rows = db.get("teams?select=team_id,team_name,active,is_exhibition")
        self.tid = {t["team_id"]: t["team_name"] for t in rows if t["active"] and not t["is_exhibition"]}
        self.picky_id = next((t["team_id"] for t in rows if t["is_exhibition"]), None)
        self.games = db.get_all(
            'all_games?select=GameId,week,Away,Home,AwayPts,HomePts,line,Status,winner,tiebreaker,picked,'
            f'fpi_margin,"Start (CT)"&cfb_season=eq.{self.season}&or=(picked.eq.true,tiebreaker.eq.true)'
            "&order=GameId.asc")
        self.names = ob.member_names()
        self._picks: dict[int, list] = {}

    def nick(self, team: str) -> str:
        return self.names.get(team) or team

    def week_games(self, w: int, pool_only: bool = True) -> list[dict]:
        gs = [g for g in self.games if g["week"] == w and (g.get("picked") or not pool_only)]
        if pool_only:
            gs = [g for g in gs if not g.get("tiebreaker")]
        return sorted(gs, key=lambda g: (str(g.get("Start (CT)")), g["GameId"]))

    def lock_time(self, w: int) -> dt.datetime | None:
        """picks_open_for_game(): regular season locks the week at the first
        kickoff of any pool or tiebreaker game; playoffs per game."""
        ks = [k for g in self.week_games(w, pool_only=False) if (k := kick(g))]
        return min(ks) if ks else None

    def locked(self, w: int) -> bool:
        lt = self.lock_time(w)
        return bool(lt) and lt <= self.now

    def open_week(self) -> int | None:
        ws = sorted({g["week"] for g in self.games if g.get("picked") and not g.get("tiebreaker")
                     and not is_final(g) and g.get("week") is not None})
        return ws[0] if ws else None

    def final_weeks(self) -> list[int]:
        ws = {g["week"] for g in self.games if g.get("picked") and not g.get("tiebreaker")}
        return sorted(w for w in ws if all(is_final(g) for g in self.week_games(w)))

    def picks(self, w: int) -> list[dict]:
        """Every member's picks for week w. Only once the week has locked."""
        if not self.locked(w):
            raise PermissionError(f"week {w} hasn't locked: picks stay hidden")
        if w not in self._picks:
            ids = ",".join(str(g["GameId"]) for g in self.week_games(w))
            self._picks[w] = self.db.get_all(
                f"picks?select=team_id,game_id,pick,by_slots,jackpot&game_id=in.({ids})&order=id.asc") if ids else []
        return self._picks[w]

    def picky_picks(self) -> list[dict]:
        try:
            return self.db.rpc("picky_player", {"p_season": self.season}).get("picks") or []
        except (httpx.HTTPError, ValueError, AttributeError):
            return []

    def submission(self, w: int) -> dict[str, bool]:
        rows = self.db.rpc("submission_status", {"p_season": self.season, "p_week": w})
        return {self.tid[r["team_id"]]: bool(r["submitted"]) for r in rows if r["team_id"] in self.tid}

    def game_lock(self, g: dict, w: int) -> dt.datetime | None:
        """When members lose the ability to pick game g, the site's rule (picks_open_for_game(), mirrored
        by picky_picks.lock_utc): regular season, the week's first kickoff; playoffs, g's own kickoff."""
        return kick(g) if w > pr.LAST_REGULAR_WEEK else self.lock_time(w)

    def game_locked(self, g: dict, w: int) -> bool:
        """Has game g's pick locked? No known lock = not locked (Picky's side stays hidden: fail closed)."""
        lk = self.game_lock(g, w)
        return is_final(g) or (lk is not None and lk <= self.now)

    def picky_revealed(self, w: int) -> bool:
        """May the group see Picky's sides for every pool game of week w? Only once each has locked
        (Charlie 2026-11-19 15:41: "picks are hidden till kickoff when they lock, not anything to do with
        all members"). Regular season: the whole week at its first kickoff; playoffs per game."""
        games = self.week_games(w)
        return bool(games) and all(self.game_locked(g, w) for g in games)


# -----------------------------------------------------------------------------
# small helpers
# -----------------------------------------------------------------------------


def num(x) -> str:
    return f"{float(x):g}"


def side_label(g: dict, side: str) -> str:
    """'Boise State -13.5': the pool line from that side."""
    line = float(g.get("line") or 0)
    return f"{side} {(line if pr.same(side, g['Home']) else -line):+g}"


def favorite(g: dict) -> str:
    return g["Home"] if float(g.get("line") or 0) < 0 else g["Away"]


def cover_margin(g: dict, side: str, ap=None, hp=None) -> float | None:
    """Points by which `side` beat the pool line (negative = missed)."""
    ap = g.get("AwayPts") if ap is None else ap
    hp = g.get("HomePts") if hp is None else hp
    if ap is None or hp is None:
        return None
    m = float(hp) + float(g.get("line") or 0) - float(ap)
    return m if pr.same(side, g["Home"]) else -m


def covered_side(g: dict, ap=None, hp=None) -> str | None:
    """The side the league grades as covered. Dead even goes to Away, like set_winner_on_final
    and league_rules.cover_winner (there's no push in this league). None = no score yet."""
    m = cover_margin(g, g["Home"], ap, hp)
    return None if m is None else (g["Home"] if m > 0 else g["Away"])


def matchup(g: dict) -> str:
    return f"{g['Away']} at {g['Home']}"


def score_line(g: dict, ap=None, hp=None) -> str:
    ap = g.get("AwayPts") if ap is None else ap
    hp = g.get("HomePts") if hp is None else hp
    return f"{g['Away']} {num(ap)}, {g['Home']} {num(hp)}"


def kick_text(k: dt.datetime | None) -> str:
    return k.strftime("%a %-I:%M %p CT").replace(":00 ", " ") if k else "TBD"


def names_list(xs: list[str]) -> str:
    return xs[0] if len(xs) == 1 else ", ".join(xs[:-1]) + " and " + xs[-1] if xs else "nobody"


def material() -> dict:
    try:
        return json.loads(MATERIAL.read_text())
    except (OSError, ValueError):
        return {}


def material_fact(rx: str) -> str | None:
    for v in (material().get("facts") or {}).values():
        if re.search(rx, v):
            return v
    return None


def standings(lg: League, before_week: int | None = None) -> list[tuple[str, int, int]]:
    """(team, points, misses) through every final week (only weeks before `before_week` if given),
    like the leaderboard."""
    rec = {t: [0, 0] for t in lg.tid.values()}
    for w in lg.final_weeks():
        if w > pr.LAST_REGULAR_WEEK or not lg.locked(w) or (before_week is not None and w >= before_week):
            continue
        gm = {g["GameId"]: g for g in lg.week_games(w)}
        for p in lg.picks(w):
            t, g = lg.tid.get(p["team_id"]), gm.get(p["game_id"])
            if t and g and g.get("winner"):
                rec[t][0 if (p.get("jackpot") or pr.same(p["pick"], g["winner"])) else 1] += 1
    return sorted(((t, w, l) for t, (w, l) in rec.items()), key=lambda r: (-r[1], r[2], r[0]))


def ranks(rows: list[tuple[str, int, int]]) -> dict[str, int]:
    return {t: 1 + sum(1 for _, w2, _ in rows if w2 > w) for t, w, _ in rows}


# -----------------------------------------------------------------------------
# the writer (local gemma) + validator
# -----------------------------------------------------------------------------

PERSONA = """You are Picky, a member of a 14-friend college football pick'em league, posting in the league's
iMessage group chat. Everyone picks 4 games a week against the spread. You play too with your own picks and
record (you're just not in the standings or the money). Voice: a real guy in the group text, quick, dry,
specific and funny, R-rated is fine at medium heat. Helpful first: the facts have to come through clean.
Wit over volume: callbacks, specific absurd comparisons, short punchlines. Never cruel about bodies, families,
race, religion or sexuality, no slurs. Never call yourself a bot, an AI, or an exhibition entry.

Running bits (use when they fit, never forced, never the same joke twice):
- Blinky (team Blinkstradamus): roast him like anyone else. No milk, carton, Jeep or "freak" jokes in posts
  (Charlie: it's a once-a-month bit, and live replies ration it).
- Ackbar is your nemesis: a chalk merchant who always takes the favorite. Be openly antagonistic, friendly
  league roast. Only quote his favorite percentage if it's in the facts.
- Charlie (team Chuck P) is the commissioner. When you mention him, a little North Korean state-media worship
  ("Dear Leader") is the house style, but his numbers stay true."""

SCHEMA = {"type": "object", "properties": {"texts": {"type": "array", "items": {"type": "string"}}},
          "required": ["texts"]}

BANNED_OPENERS = ("alright", "all right", "listen up", "well well", "folks", "buckle up", "ladies and gentlemen",
                  "another week", "good morning league", "attention", "gather round", "okay so", "ok so")
_NUM = re.compile(r"(?<![\w.])\d+(?:\.\d+)?")
_PHONE = re.compile(r"\+?\d[\d\-\s().]{8,}\d")
_BODY = re.compile(r"\b(penis|dick|weiner|wiener|tits|boobs|fat|ugly|bald|mom|mother|wife|girlfriend|dad|father)\b",
                   re.I)


def run_writer(prompt: str) -> list[str]:
    """The recap writer: the box's local gemma (zero maintenance, phase 8: no Claude). validate() checks every
    number and name; a failed or rejected draft posts plain_recap (the code template) instead."""
    import local_llm
    out = local_llm.json_call("", prompt, SCHEMA, caller="group_posts", temperature=0.6, num_predict=900)
    return [str(t) for t in out.get("texts") or []]


def _opener(t: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]", "", t.lower()).split()[:3])


def variant(options: list[str], key: str, recent: list[str] | None = None) -> str:
    """One of the template wordings: fixed per post key (a retry says the same thing), skipping any
    whose opener a recent post used."""
    seen = {_opener(r) for r in (ob.recent_posts() if recent is None else recent)}
    start = int(hashlib.sha1(key.encode()).hexdigest(), 16) % len(options)
    for i in range(len(options)):
        o = options[(start + i) % len(options)]
        if _opener(o) not in seen:
            return o
    return options[start]


def all_names(lg_names: dict[str, str]) -> dict[str, str]:
    """name or team as written -> team"""
    out = {}
    for team, nick in lg_names.items():
        out[team] = team
        out[nick] = team
    out["Blinkstradamus"] = "Blinkstradamus"
    out["Chuck"] = "Chuck P"
    return out


def mentioned(text: str, lg_names: dict[str, str]) -> set[str]:
    found = set()
    for name, team in all_names(lg_names).items():
        if re.search(rf"(?<!\w){re.escape(name)}(?!\w)", text, re.I if len(name) > 3 else 0):
            found.add(team)
    return found


def validate(texts: list[str], facts: list[str], lg_names: dict[str, str], recent: list[str],
             max_texts: int = 4, max_chars: int = 320, allowed_teams: set[str] | None = None,
             required_teams: set[str] | None = None) -> list[str]:
    probs = []
    if not texts or len(texts) > max_texts:
        probs.append(f"write 1 to {max_texts} texts (got {len(texts)})")
    fact_text = "\n".join(facts)
    nums = set(_NUM.findall(fact_text)) | {num(n) for n in _NUM.findall(fact_text)} | {"00"}
    fact_teams = mentioned(fact_text, lg_names)
    allowed = fact_teams if allowed_teams is None else allowed_teams
    seen_openers = {_opener(r) for r in recent}
    said = set()
    for i, t in enumerate(texts):
        low = t.lower()
        if len(t) > max_chars:
            probs.append(f"text {i + 1} is {len(t)} characters; keep each under {max_chars}")
        bad = [n for n in _NUM.findall(t) if n not in nums and num(n) not in nums]
        if bad:
            probs.append(f"text {i + 1} has numbers not in FACTS: {', '.join(bad)}")
        if _PHONE.search(t):
            probs.append(f"text {i + 1} looks like it has a phone number")
        if re.search(r"\b(exhibition|as an ai|language model|i'?m a bot)\b", low):
            probs.append(f"text {i + 1} calls Picky a bot/AI/exhibition")
        if "milk" in low and re.search(r"\b(spill\w*|dropp?\w*|crash\w*|knock\w* over)\b", low):
            probs.append(f"text {i + 1} breaks the milk canon (he never spilled or dropped anything)")
        elif rare_bits.mentions("milk", t):
            probs.append(f"text {i + 1} uses the milk bit (once a month at most, never in posts); drop it")
        if _BODY.search(t):
            probs.append(f"text {i + 1} goes after bodies or family")
        who = mentioned(t, lg_names)
        said |= who
        stray = who - allowed
        if stray:
            probs.append(f"text {i + 1} names members not in FACTS: {', '.join(sorted(stray))}")
    if texts:
        op = _opener(texts[0])
        if any(op.startswith(b) for b in BANNED_OPENERS) or (op and op in seen_openers):
            probs.append("the first text opens like a recent post or a stock opener; open differently")
    if required_teams and not required_teams <= said:
        probs.append("name every one of: " + ", ".join(sorted(lg_names.get(t, t) for t in required_teams - said)))
    return probs


def build_prompt(task: str, facts: list[str], lg_names: dict[str, str], recent: list[str],
                 n_texts: str, max_chars: int) -> str:
    roster = ", ".join(f"{nick} (team {team})" if nick != team else team for team, nick in sorted(lg_names.items()))
    rec = "\n".join(f"- {r[:240]}" for r in recent[-8:]) or "- none yet"
    return f"""{PERSONA}

TASK: {task}

FACTS (your only source: every number you write must appear here, never invent a stat, score, streak or record):
{chr(10).join(f"F{i + 1}: {f}" for i, f in enumerate(facts))}

WHAT THE GROUP CALLS PEOPLE (use these names, not the team names, except Blinkstradamus/Ackbar are fine): {roster}

YOUR RECENT POSTS (don't reuse their openers, structure or jokes):
{rec}

FORMAT: {n_texts} separate texts, each under {max_chars} characters, written like real group texts. No lists,
headers, hashtags or emoji strings. Don't open with a stock opener ("Alright", "Listen up", "Folks", "Buckle up",
"Another week"...). Don't sign off. No phone numbers or emails.
Return only the JSON object {{"texts": [...]}}."""


def write(task: str, facts: list[str], lg_names: dict[str, str], n_texts: str = "2 to 4", max_texts: int = 4,
          max_chars: int = 320, writer=run_writer, **vkw) -> tuple[list[str] | None, list[str]]:
    recent = ob.recent_posts()
    prompt = build_prompt(task, facts, lg_names, recent, n_texts, max_chars)
    probs: list[str] = []
    for attempt in range(2):
        p = prompt if not probs else prompt + "\n\nYOUR LAST DRAFT WAS REJECTED:\n- " + "\n- ".join(probs) + \
            "\nFix every problem."
        try:
            texts = writer(p)
        except Exception as e:  # noqa: BLE001
            probs = [f"writer failed: {str(e)[:200]}"]
            log.warning("writer failed (attempt %d): %s", attempt + 1, e)
            continue
        probs = validate(texts, facts, lg_names, recent, max_texts=max_texts, max_chars=max_chars, **vkw)
        if not probs:
            return texts, []
        log.info("draft rejected (attempt %d): %s", attempt + 1, "; ".join(probs))
    return None, probs


# -----------------------------------------------------------------------------
# recap
# -----------------------------------------------------------------------------


def recap_facts(lg: League, w: int) -> tuple[list[str], dict]:
    games = lg.week_games(w)
    picks = lg.picks(w)
    teams = sorted(lg.tid.values())
    n = lg.nick
    by_side: dict[tuple, list[str]] = {}
    wk = {t: [0, 0] for t in teams}
    for p in picks:
        t = lg.tid.get(p["team_id"])
        g = next((x for x in games if x["GameId"] == p["game_id"]), None)
        if not t or not g or not p.get("pick"):
            continue
        by_side.setdefault((g["GameId"], p["pick"]), []).append(t)
        hit = p.get("jackpot") or pr.same(p["pick"], g.get("winner"))
        wk[t][0 if hit else 1] += 1
    facts = []
    for g in games:
        cov = g.get("winner")
        fav = favorite(g)
        fm = cover_margin(g, fav)
        out = ("won outright" if (fav == g["Home"]) == (float(g["HomePts"]) > float(g["AwayPts"])) else "lost outright")
        cm = cover_margin(g, cov)
        facts.append(f"Week {w} final: {side_label(g, cov)} covered"
                     + (f" by {abs(cm):g}" if cm else "" if cm is None else
                        " (landed on the number; the push goes to the away side)")
                     + f". {score_line(g)}, pool line {side_label(g, fav)} (favorite {fav} {out}).")
        for side in (g["Away"], g["Home"]):
            who = by_side.get((g["GameId"], side), [])
            if who:
                facts.append(f"Took {side_label(g, side)}: {', '.join(n(t) for t in who)} ({len(who)} of "
                             f"{sum(len(by_side.get((g['GameId'], s), [])) for s in (g['Away'], g['Home']))}); "
                             f"{'HIT' if pr.same(side, cov) else 'MISSED'}.")
    played = {t: r for t, r in wk.items() if sum(r)}
    best = max((r[0] for r in played.values()), default=0)
    worst = min((r[0] for r in played.values()), default=0)
    facts.append(f"Week {w} records: " + "; ".join(f"{n(t)} {r[0]}-{r[1]}" for t, r in
                                                   sorted(played.items(), key=lambda x: (-x[1][0], x[0]))) + ".")
    facts.append(f"Best week {w} record ({best}-{len(games) - best}): "
                 f"{names_list([n(t) for t, r in played.items() if r[0] == best])}.")
    facts.append(f"Worst week {w} record ({worst}-{len(games) - worst}): "
                 f"{names_list([n(t) for t, r in played.items() if r[0] == worst])}.")
    # worst pick: the miss that lost to the line by the most
    misses = [(cover_margin(g, s), g, s, who) for (gid, s), who in by_side.items()
              for g in games if g["GameId"] == gid and not pr.same(s, g.get("winner"))]
    if misses:
        m, g, s, who = min(misses, key=lambda x: x[0])
        facts.append(f"WORST PICK: {side_label(g, s)} ({matchup(g)}) missed the line by {num(abs(m))} points; "
                     f"final {score_line(g)}. Taken by {', '.join(n(t) for t in who)}.")
    # chalk of the week: the favorite the most people took
    chalk = [(len(who), g, s, who) for (gid, s), who in by_side.items()
             for g in games if g["GameId"] == gid and pr.same(s, favorite(g))]
    if chalk:
        k, g, s, who = max(chalk, key=lambda x: (x[0], abs(float(x[1].get("line") or 0))))
        facts.append(f"CHALK OF THE WEEK: {k} of {len(played)} took the favorite {side_label(g, s)} ({matchup(g)}); "
                     f"it {'covered' if pr.same(s, g.get('winner')) else 'did NOT cover'}.")
    # best call: the hit the fewest people had (biggest cover breaks ties)
    hits = [(len(who), -(cover_margin(g, s) or 0), g, s, who) for (gid, s), who in by_side.items()
            for g in games if g["GameId"] == gid and pr.same(s, g.get("winner"))]
    if hits:
        k, negm, g, s, who = min(hits, key=lambda x: (x[0], x[1]))
        facts.append(f"BEST CALL: {side_label(g, s)} ({matchup(g)}) covered by {num(-negm)}; only {k} of "
                     f"{len(played)} had it: {', '.join(n(t) for t in who)}.")
    rows = standings(lg)
    rk = ranks(rows)
    pts = {t: w_ for t, w_, _ in rows}
    los = {t: l_ for t, _, l_ in rows}
    mv = standings_move(rows, standings(lg, before_week=w), n)
    if mv:
        facts.append(mv)
    facts.append("Season standings: " + "; ".join(f"{_ord(rk[t])} {n(t)} {pts[t]}-{los[t]}" for t, _, _ in rows[:6])
                 + f" (of {len(rows)}).")
    b = "Blinkstradamus"
    if b in wk:
        facts.append(f"Blinky went {wk[b][0]}-{wk[b][1]} in week {w}; season {pts[b]}-{los[b]}, "
                     f"{_ord(rk[b])} place.")
        mv = material_fact(rf"Blinkstradamus moved from \d+\w+ to \d+\w+ place in week {w}\b")
        if mv:
            facts.append(mv.replace("Blinkstradamus", "Blinky"))
    a = "Ackbar"
    if a in wk:
        fav_pct = material_fact(r"Ackbar takes the favorite \d+% of the time")
        facts.append(f"Ackbar (your nemesis) went {wk[a][0]}-{wk[a][1]} in week {w}; season {pts[a]}-{los[a]}."
                     + (" " + fav_pct.split(";")[0] + "." if fav_pct else ""))
    pk = [p for p in lg.picky_picks() if p.get("week") == w and not p.get("tiebreaker") and p.get("winner")]
    allp = [p for p in lg.picky_picks() if p.get("winner") and not p.get("tiebreaker")
            and int(p.get("week") or 0) <= pr.LAST_REGULAR_WEEK]
    hits_p = [p for p in pk if pr.same(p["pick"], p["winner"])]
    season_hits = sum(1 for p in allp if pr.same(p["pick"], p["winner"]))
    if pk:
        facts.append(f"YOUR OWN WEEK (Picky): {len(hits_p)}-{len(pk) - len(hits_p)} in week {w}: " + ", ".join(
            f"{side_label(p | {'Home': p['home'], 'Away': p['away']}, p['pick'])} "
            f"{'hit' if pr.same(p['pick'], p['winner']) else 'missed'}" for p in pk)
            + f". Your season: {season_hits}-{len(allp) - season_hits}.")
    else:
        facts.append(f"YOUR OWN WEEK (Picky): you made no picks in week {w}. Your season: "
                     f"{season_hits}-{len(allp) - season_hits}.")
    return facts, {"week_records": wk}


def _ord(k: int) -> str:
    return pr._ord(k)


def standings_move(rows: list, before: list, n=lambda t: t) -> str | None:
    """'STANDINGS MOVE: Charlie stays 1st (30-18); Blinky up from 6th to 3rd.' The leader plus the two
    biggest moves in or around the top 4 (the playoff spots). None without an earlier week to compare."""
    if not rows or not before or not any(w for _, w, _ in before):
        return None
    rk, rb = ranks(rows), ranks(before)
    rec = {t: f"{w}-{l}" for t, w, l in rows}
    lead = rows[0][0]
    bits = [f"{n(lead)} {'stays' if rb.get(lead) == 1 else 'takes over'} {_ord(rk[lead])} ({rec[lead]})"]
    moves = sorted((t for t in rk if t != lead and rk[t] != rb.get(t, rk[t]) and (rk[t] <= 4 or rb.get(t, 99) <= 4)),
                   key=lambda t: (-abs(rk[t] - rb[t]), rk[t]))[:2]
    for t in moves:
        bits.append(f"{n(t)} {'up' if rk[t] < rb[t] else 'down'} from {_ord(rb[t])} to {_ord(rk[t])} ({rec[t]})")
    return "STANDINGS MOVE: " + "; ".join(bits) + "."


def make_recap(lg: League, w: int, writer=run_writer) -> list[str] | None:
    """ONE text, at most max_chars_long: who won the week and the standings move first, one award, Picky's
    own week. The writer gets one rewrite (too long counts); then plain_recap, the short code version."""
    facts, _ = recap_facts(lg, w)
    lim, _ = ob.limits("recap")
    task = (f"Sunday morning recap of week {w}, posted in the group. Nobody reads long texts: ONE text, under "
            f"{lim} characters. Lead with who won the week (best record), then the STANDINGS MOVE. Then at most "
            "ONE award (the best call or the worst pick, whichever is better) and one quick clause on your own "
            "week (brag if good, excuse if bad). No per-game rundown, no list of everybody's record, no playoff "
            "math, one joke max. Conversational, not a report.")
    texts, probs = write(task, facts, lg.names, n_texts="ONE", max_texts=1, max_chars=lim, writer=writer)
    if texts is None:
        log.warning("recap week %s writer fell back to the template: %s", w, "; ".join(probs))
        texts = plain_recap(w, facts, lim)
    return texts


# -----------------------------------------------------------------------------
# preview + saturday
# -----------------------------------------------------------------------------


def load_take(w: int, gid: int) -> dict | None:
    """The dossier's weekly Opus analyst take, only while it's fresh (enrich.py)."""
    import enrich
    try:
        d = json.loads((DOSSIER_DIR / str(w) / f"{gid}.json").read_text())
    except (OSError, ValueError):
        return None
    t = d.get("analyst_take") or None
    return t if t and enrich.fresh(t.get("generated_at"), lg_now()) else None


def lg_now() -> dt.datetime:
    return dt.datetime.now(CT)


def picky_card(lg: League, w: int) -> dict[int, dict]:
    """{game_id: {"label", "why", "made"}}: Picky's submitted picks, else
    what his own pick logic would take right now, ONLY for games whose picks
    have locked (League.game_locked); every other game is left out."""
    out = {}
    games = [g for g in lg.week_games(w) if lg.game_locked(g, w)]
    if not games:
        return out
    for p in lg.picky_picks():
        if p.get("week") == w and p.get("pick") and not p.get("tiebreaker"):
            g = next((x for x in games if x["GameId"] == p["game_id"]), None)
            if g:
                out[g["GameId"]] = {"label": side_label(g, p["pick"]), "why": p.get("reason") or "", "made": True}
    todo = [g for g in games if g["GameId"] not in out and g.get("line") is not None]
    if todo:
        try:
            import espn
            import picky_picks
            notes = asyncio.run(espn.game_notes([g["GameId"] for g in todo]))
            idx = picky_picks.load_idx()
            for g in todo:
                d = picky_picks.decide(g, notes.get(g["GameId"]) or {}, idx)
                out[g["GameId"]] = {"label": side_label(g, d["pick"]), "why": d.get("reason") or "", "made": False}
        except Exception as e:  # noqa: BLE001  a lean is optional
            log.warning("picky leans unavailable: %s", e)
    return out


def stakes_facts(lg: League) -> list[str]:
    rows = standings(lg)
    if not rows:
        return []
    rk = ranks(rows)
    res, meta = pr.load(lg.db, lg.now)
    top = "; ".join(f"{_ord(rk[t])} {lg.nick(t)} {w}" for t, w, _ in rows[:5])
    out = [f"Standings (points): {top}. Cut line for the top 4 playoff spots: {rows[3][1]} points."]
    if meta["weeks_left"] > 0:
        out.append(f"{meta['weeks_left']} regular-season weeks left, including this one; week {meta.get('last_week', 15)} is "
                   "conference championship week, then the league playoffs start and scores reset.")
    return out


def make_preview(lg: League, w: int) -> list[str] | None:
    """Thursday preview, code only: the slate and lock, Picky's card (once locked), the film room's best
    line (once locked: it takes a side), the stakes."""
    games = lg.week_games(w)
    if not games:
        return None
    card = picky_card(lg, w)
    lock = lg.lock_time(w)
    texts = plain_preview(w, games, card, lock, stakes_facts(lg), key=f"preview:{lg.season}:{w}",
                          hidden=not all(lg.game_locked(g, w) for g in games))
    quote = next(((g, t["quotable"]) for g in games if g["GameId"] in card  # locked games only: it takes a side
                  and (t := load_take(w, g["GameId"])) and t.get("quotable")), None)
    if quote:
        texts.insert(2, f"Film room on {matchup(quote[0])}: {quote[1]}")
    return ob.clip(texts, ob.limits("preview")[0])  # one text: head, card, then the film room / stakes if room


def make_saturday(lg: League, w: int) -> list[str] | None:
    """Saturday morning post, code only: today's pool games; Picky's side in each once its pick has locked."""
    today = lg.now.date()
    games = [g for g in lg.week_games(w) if (k := kick(g)) and k.date() == today and k > lg.now - dt.timedelta(hours=1)]
    if not games:
        return None
    return ob.clip(plain_saturday(games, picky_card(lg, w), key=f"sat:{lg.season}:{w}",
                                  hidden=not all(lg.game_locked(g, w) for g in games)), ob.limits("saturday")[0])


# Code templates: the computed facts, lightly worded, nothing invented (preview / saturday always;
# the recap when the weekly writer is down or every draft fails validation).

def plain_recap(w: int, facts: list[str], limit: int | None = None) -> list[str]:
    """The short code recap, most important first (clip drops from the end): who won the week, the
    standings move, one award, my week."""
    def find(prefix):
        return next((f for f in facts if f.startswith(prefix)), "")
    head = f"Week {w} is in the books."
    m = re.match(rf"Best week {w} record \((\d+-\d+)\): (.+)\.$", find(f"Best week {w} record"))
    if m:
        head += f" {m[2]} won it at {m[1]}."
    move = find("STANDINGS MOVE:").replace("STANDINGS MOVE: ", "Standings: ")
    award = ""
    if (b := find("BEST CALL:")):
        award = re.sub(r"; only (\d+) of (\d+) had it: .*$", r", only \1 of \2 had it.",
                       re.sub(r" \([^)]* at [^)]*\)", "", b)).replace("BEST CALL:", "Best call:")
    elif (b := find("WORST PICK:")):
        award = re.sub(r" points; final .*$", ".", re.sub(r" \([^)]* at [^)]*\)", "", b)).replace("WORST PICK:", "Worst pick:")
    mine = find("YOUR OWN WEEK (Picky):")
    me = re.search(r"(\d+-\d+) in week \d+", mine)
    se = re.search(r"Your season: (\d+-\d+)", mine)
    mine = (f"Me: {me[1]}" if me else "I sat this one out") + (f", {se[1]} on the year." if se else ".") if mine else ""
    return ob.clip([" ".join(x for x in (head, move, award, mine) if x)], limit or ob.limits("recap")[0])


def _short_kick(k: dt.datetime | None) -> str:
    return k.strftime("%a %-I:%M%p").replace(":00", "").lower().capitalize() if k else "TBD"


HIDDEN_NOTE = "My sides show at kickoff, when picks lock."


def _card_line(g: dict, c: dict | None) -> str:
    """'Iowa -3.5 vs Utah (Sat 11am)': one short item per game (posts are one text)."""
    if not c:
        return f"{side_label(g, favorite(g))} vs {g['Home'] if pr.same(favorite(g), g['Away']) else g['Away']} " \
               f"({_short_kick(kick(g))})"
    side = c["label"].rsplit(" ", 1)[0]
    other = g["Home"] if pr.same(side, g["Away"]) else g["Away"]
    return f"{c['label']} vs {other} ({_short_kick(kick(g))}" + (")" if c["made"] else ", a lean)")


def plain_preview(w: int, games: list[dict], card: dict, lock, stakes: list[str], key: str = "",
                  hidden: bool = False) -> list[str]:
    """hidden: some game hasn't locked yet, so the card line is games and lines only (HIDDEN SIDES)."""
    lk = kick_text(lock)
    head = variant([f"Week {w} slate is set. Picks lock at the first kickoff, {lk}.",
                    f"Week {w} is up. Every pick locks at the first kickoff, {lk}, so don't sleep on it.",
                    f"Slate's out for week {w}. First kickoff, {lk}, locks every pick."], key or f"preview:{w}")
    lines = "; ".join(_card_line(g, card.get(g["GameId"])) for g in games) + "."
    out = [head, ("The card: " + lines + " " + HIDDEN_NOTE) if hidden or not card else ("My card: " + lines)]
    return out + stakes[:1]


def plain_saturday(games: list[dict], card: dict, key: str = "", hidden: bool = False) -> list[str]:
    """hidden: one of today's games hasn't locked yet: games and lines only (HIDDEN SIDES)."""
    body = "; ".join(_card_line(g, card.get(g["GameId"])) for g in games) + "."
    k = key or "sat:" + ",".join(str(g["GameId"]) for g in games)
    if hidden or not card:
        return [variant(["Pool games today: ", "Game day. On the board: ", "Saturday's pool games: "], k)
                + body + " " + HIDDEN_NOTE]
    return [variant(["Pool games today, my sides: ", "Game day. Where I'm at: ", "Saturday's pool games, my side: "],
                    k) + body]


# -----------------------------------------------------------------------------
# reminders
# -----------------------------------------------------------------------------


def reminder_slots(lock: dt.datetime) -> list[tuple[str, dt.datetime]]:
    """When to remind, for a week that locks at `lock` (CT)."""
    lock = lock.astimezone(CT)
    day7 = lock.replace(hour=7, minute=0, second=0, microsecond=0)
    eve = (lock - dt.timedelta(days=1)).replace(hour=19, minute=0, second=0, microsecond=0)
    if lock.weekday() == 5:  # Saturday: one reminder
        first = day7 if lock - day7 >= dt.timedelta(minutes=30) else eve
        return [("sat", first)]
    second = lock - dt.timedelta(hours=2)
    first = day7 if second >= day7 + dt.timedelta(hours=1) else eve
    return [("first", first), ("second", second)] if second > first else [("first", first)]


def due_slot(lock: dt.datetime, now: dt.datetime, grace: dt.timedelta = dt.timedelta(minutes=45)) -> str | None:
    if now >= lock:
        return None
    for name, at in reminder_slots(lock):
        if at <= now < min(at + grace, lock):
            return name
    return None


def group_slot(lock: dt.datetime, now: dt.datetime) -> str | None:
    """due_slot, minus the 2-hours-before slot unless config second_reminder: one group reminder a week,
    the straggler DMs handle the last 2 hours."""
    slot = due_slot(lock, now)
    return None if slot == "second" and not ob.config()["second_reminder"] else slot


def remind_facts(lg: League, w: int) -> tuple[list[str], list[str], int]:
    """(facts, stragglers (teams), how many have submitted)."""
    sub = lg.submission(w)
    lock = lg.lock_time(w)
    first = lg.week_games(w, pool_only=False)
    first_g = min(first, key=lambda g: str(g.get("Start (CT)"))) if first else None
    left = sorted(t for t, ok in sub.items() if not ok)
    mins = int((lock - lg.now).total_seconds() // 60) if lock else 0
    hrs = round(mins / 60) if mins >= 90 else None
    when = f"in about {hrs} hours" if hrs else f"in {mins} minutes"
    facts = [f"Week {w} picks all lock at the first kickoff: {matchup(first_g) if first_g else ''}, "
             f"{kick_text(lock)} ({when}).",
             f"{len(left)} of {len(sub)} haven't made their week {w} picks yet."]
    return facts, left, len(sub) - len(left)


def make_reminder(lg: League, w: int) -> list[str] | None:
    """Pick reminder before the weekly lock, code only: when it locks and (config name_stragglers)
    who still hasn't picked. Never anyone's picks, never who has picked."""
    _, left, _ = remind_facts(lg, w)
    if not left:
        return None
    lock = kick_text(lg.lock_time(w))
    if ob.config()["name_stragglers"]:
        who = names_list([lg.nick(t) for t in left])
        opts = [f"Picks lock {lock}. Still waiting on {who}.",
                f"{who}: picks lock {lock}. Clock's running.",
                f"Lock is {lock} and {who} still {'has' if len(left) == 1 else 'have'}n't picked."]
    else:
        n = f"{len(left)} of you"
        opts = [f"Picks lock {lock}. Still waiting on {n}.", f"Lock is {lock}. {n.capitalize()} still haven't picked."]
    return [variant(opts, f"remind:{lg.season}:{w}:{lock}")]


# -----------------------------------------------------------------------------
# final-score pings
# -----------------------------------------------------------------------------


def espn_status(gid: int, client: httpx.Client) -> dict | None:
    try:
        r = client.get(f"{ESPN_SUMMARY}{gid}", timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        comp = r.json()["header"]["competitions"][0]
    except (httpx.HTTPError, ValueError, KeyError, IndexError):
        return None
    st = comp.get("status") or {}
    by = {c.get("homeAway"): c for c in comp.get("competitors") or []}
    try:
        ap, hp = float(by["away"]["score"]), float(by["home"]["score"])
    except (KeyError, TypeError, ValueError):
        ap = hp = None
    return {"final": bool((st.get("type") or {}).get("completed")), "state": (st.get("type") or {}).get("state"),
            "period": st.get("period") or 0, "away": ap, "home": hp}


def due_pings(lg: League, client: httpx.Client) -> list[tuple[dict, float, float]] | None:
    """Pool games that just went final and are ready to ping now (batched),
    or None to wait: finals are held while another pool game in the window is
    still being played, up to config ping_hold_min, so a burst of finals is one
    combined ping. Marks nothing."""
    st = ob.state().setdefault("pings", {})
    window = [g for g in lg.games if g.get("picked") and not g.get("tiebreaker") and (k := kick(g))
              and k <= lg.now <= k + dt.timedelta(hours=6) and not ob.done(f"ping:{g['GameId']}")]
    if not window:
        return []
    finals, late = [], False
    for g in window:
        s = espn_status(g["GameId"], client)
        if not s:
            continue
        if s["final"] and s["away"] is not None:
            finals.append((g, s["away"], s["home"]))
        elif s["state"] == "in":
            late = True
    if not finals:
        return []
    first_seen = min(st.get(str(g["GameId"]), lg.now.timestamp()) for g, _, _ in finals)
    for g, _, _ in finals:
        st.setdefault(str(g["GameId"]), lg.now.timestamp())
    s_all = ob.state()
    s_all["pings"] = {k: v for k, v in st.items() if lg.now.timestamp() - v < 86400}
    ob.save_state(s_all)
    if late and lg.now.timestamp() - first_seen < 60 * float(ob.config()["ping_hold_min"]):
        return None  # another pool game is still on: send them together
    return finals


def make_ping(lg: League, finals: list[tuple[dict, float, float]]) -> list[str]:
    """One line per batch of finals, code only: who covered, the score, and one hook from the graded
    picks (Ackbar's chalk dying, nobody / everybody had it, the leader missing, who cashed)."""
    rows = standings(lg)
    leader = rows[0][0] if rows else None
    mine = {p.get("game_id"): p for p in lg.picky_picks() if p.get("pick")}
    parts = []
    for g, ap, hp in finals:
        cov = covered_side(g, ap, hp)
        if not cov:
            continue
        picks = [p for p in lg.picks(g["week"]) if p["game_id"] == g["GameId"] and p.get("pick") and p["team_id"] in lg.tid]
        won = [lg.tid[p["team_id"]] for p in picks if pr.same(p["pick"], cov)]
        lost = [lg.tid[p["team_id"]] for p in picks if not pr.same(p["pick"], cov)]
        fav = favorite(g)
        ack = next((p for p in picks if lg.tid[p["team_id"]] == "Ackbar"), None)
        if ack and pr.same(ack["pick"], fav) and not pr.same(ack["pick"], cov):
            hook = f"Ackbar's chalk on {fav} just died"
        elif picks and not won:
            hook = "nobody had it"
        elif picks and not lost:
            hook = "everybody had it"
        elif leader in lost:
            hook = f"{lg.nick(leader)} missed this one"
        elif won:
            hook = names_list([lg.nick(t) for t in won[:3]]) + (" cashes" if len(won) == 1 else " cash")
        else:
            hook = ""
        pk = mine.get(g["GameId"])
        if pk:
            hook += (", " if hook else "") + ("I had it" if pr.same(pk["pick"], cov) else "I missed it")
        parts.append((f"{side_label(g, cov)} covers", f" ({score_line(g, ap, hp)})", f", {hook}" if hook else ""))
    key = "ping:" + ",".join(str(g["GameId"]) for g, _, _ in finals)
    head = variant(["Final:", "In the books:", "That's final:"], key) + " "
    lim = ob.limits("ping")[0]
    for keep in ((0, 1, 2), (0, 2), (0,)):  # one text: drop scores, then hooks, before dropping a game
        text = head + "; ".join("".join(p[i] for i in keep) for p in parts) + "."
        if len(text) <= lim:
            break
    return ob.clip([text], lim)


# -----------------------------------------------------------------------------
# in-game pings (live_games.py). OFF unless the flag file exists: Charlie hasn't
# asked for in-game posts, and a 14-person group doesn't want a play-by-play.
# Only QB news in a live POOL game (reported injured on the play / QB change),
# plain text from the feed (no writer), quiet hours respected, one ping per
# LIVE_PING_GAP_S at most, each event once.
# -----------------------------------------------------------------------------

LIVE_PINGS_FLAG = ob.PRIVATE / "PICKY_LIVE_PINGS"
FINAL_PINGS_FLAG = ob.PRIVATE / "PICKY_FINAL_PINGS"  # absent = no final-score posts in the group
LIVE_PING_GAP_S = 20 * 60


def live_pings(lg, games: dict | None = None) -> list[tuple[str, str]]:
    """(key, text) per live pool game with QB news not yet posted."""
    import live_games as L
    games = (L.load().get("games") or {}) if games is None else games
    out = []
    for g in lg.games:
        if not g.get("picked") or g.get("tiebreaker"):
            continue
        lv = games.get(str(g["GameId"]))
        if not lv or lv.get("state") != "in" or (L.age(lv) or 1e9) > L.game_max_age():
            continue
        bits, names = [], []
        for x in lv.get("injuries") or []:
            if x.get("qb") and not x.get("returned"):
                bits.append(f"{x.get('team') or ''} QB {x.get('full_name') or x['player']} reported hurt on a play "
                            f"({L.period_label(x['period'])} {x['clock']}, per ESPN's play-by-play)".strip())
                names.append(x["player"])
        for team, q in (lv.get("qb") or {}).items():
            for c in q.get("changes") or []:
                bits.append(f"{c['to']} in at QB for {team}")
                names.append(c["to"])
        if not bits:
            continue
        key = f"live:{g['GameId']}:qb:{','.join(sorted(set(names)))}"
        if ob.done(key):
            continue
        txt = "; ".join(bits) + f". {L.clock_text(lv)}: {lv['away']['name']} {lv['away_score']}, " \
              f"{lv['home']['name']} {lv['home_score']}."
        cs = L.cover_status(g["Away"], g["Home"], g.get("line"), lv.get("away_score"), lv.get("home_score"))
        if cs:
            txt += f" Pool line: {cs.split(' (')[0]}."
        out.append((key, txt))
    return out


def tick_live_pings(lg, now: dt.datetime, dry_run=None, echo=print) -> None:
    if not LIVE_PINGS_FLAG.exists():
        return
    st = ob.state()
    if now.timestamp() - float(st.get("live_ping_at") or 0) < LIVE_PING_GAP_S:
        return
    for key, text in live_pings(lg)[:1]:
        r = ob.post("live", [text], key, quiet=True, expires_s=600, dry_run=dry_run, now=now, echo=echo)
        log.info("live ping %s -> %s", key, r)
        if r == "queued":
            st = ob.state()
            st["live_ping_at"] = now.timestamp()
            ob.save_state(st)


# -----------------------------------------------------------------------------
# tick
# -----------------------------------------------------------------------------


def _due_recap(lg: League) -> int | None:
    now = lg.now
    sun9 = (now - dt.timedelta(days=(now.weekday() + 1) % 7)).replace(hour=9, minute=0, second=0, microsecond=0)
    if not (sun9 <= now < sun9 + dt.timedelta(days=1)):
        return None
    for w in reversed(lg.final_weeks()):
        last = max((k for g in lg.week_games(w) if (k := kick(g))), default=None)
        if last and now - last < dt.timedelta(days=4):
            return w
    return None


def tick(dry_run: bool | None = None, now: dt.datetime | None = None, echo=print) -> int:
    ob.detect()
    if ob.paused() and not dry_run:
        return 0
    now = now or dt.datetime.now(CT)
    db = DB()
    lg = League(db, now)
    sea = lg.season

    def send(kind, texts, key, **kw):
        if texts:
            r = ob.post(kind, texts, key, dry_run=dry_run, now=now, echo=echo, **kw)
            log.info("%s %s -> %s", kind, key, r)

    w = _due_recap(lg)
    if w is not None and not ob.done(f"recap:{sea}:{w}"):
        send("recap", make_recap(lg, w), f"recap:{sea}:{w}", expires_s=20 * 3600)

    ow = lg.open_week()
    # picks up: a new week's pool games -> the announcement, then the deep breakdown (week_breakdown.py)
    try:
        log.info("picks-up week %s -> %s", ow, week_breakdown.tick_picks_up(lg, ow, now, send))
    except Exception as e:  # noqa: BLE001  never let it block reminders and pings
        log.warning("picks-up failed: %r", e)
    if ow is not None:
        lock = lg.lock_time(ow)
        # preview: Thursday noon until the lock. With a breakdown on file it's a short refresh
        # (what changed since); nothing changed = no post.
        if now.weekday() == 3 and now.hour >= 12 and lock and now < lock and not ob.done(f"preview:{sea}:{ow}"):
            texts = week_breakdown.refresh_texts(lg, ow, now, leans=lg.picky_revealed(ow))
            if texts == []:
                ob.mark(f"preview:{sea}:{ow}", "nothing-new")
                log.info("preview %s: nothing changed since the breakdown, skipped", ow)
            elif texts is None and ob.done(week_breakdown.ann_key(sea, ow)):
                ob.mark(f"preview:{sea}:{ow}", "skipped-announced")  # the picks-up post already gave the slate
                log.info("preview %s: picks-up post already went out, skipped", ow)
            else:
                send("preview", texts or make_preview(lg, ow), f"preview:{sea}:{ow}",
                     expires_s=int(max(600, (lock - now).total_seconds())))
        # saturday: 09:30-13:00 with a game today
        if (now.weekday() == 5 and (now.hour, now.minute) >= (9, 30) and now.hour < 13
                and not ob.done(f"sat:{sea}:{ow}")):
            send("saturday", make_saturday(lg, ow), f"sat:{sea}:{ow}", expires_s=3 * 3600)
        # reminders: before the weekly lock (regular season)
        if lock and ow <= pr.LAST_REGULAR_WEEK:
            slot = group_slot(lock, now)
            key = f"remind:{sea}:{ow}:{slot}"
            if slot and not ob.done(key):
                send("remind", make_reminder(lg, ow), key,
                     expires_s=int(max(300, (lock - now).total_seconds() - 300)))
        # straggler DMs ~2 h and ~30 min before lock, to whoever still hasn't picked (straggler_nudges.py)
        try:
            res = straggler_nudges.tick(lg, ow, now, dry_run=dry_run, echo=echo)
            if res:
                log.info("nudges week %s -> %s", ow, res)
        except Exception as e:  # noqa: BLE001  never let it block the rest of the tick
            log.warning("nudges failed: %r", e)

    # Final-score pings are OFF (Charlie, 2026-11-17: "No need to send final scores to the group").
    # Turn back on by creating cfb-pickem-private/PICKY_FINAL_PINGS.
    finals = None
    if FINAL_PINGS_FLAG.exists():
        with httpx.Client() as c:
            finals = due_pings(lg, c)
    if finals:
        key = "ping:" + ",".join(str(g["GameId"]) for g, _, _ in finals)
        r = ob.post("ping", make_ping(lg, finals), key, quiet=True, expires_s=1800, dry_run=dry_run, now=now,
                    echo=echo)
        for g, _, _ in finals:
            ob.mark(f"ping:{g['GameId']}", r)
        log.info("ping %s -> %s", key, r)
    tick_live_pings(lg, now, dry_run=dry_run, echo=echo)
    return 0


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------


def _show(title: str, texts) -> None:
    print(f"\n=== {title} ===")
    if not texts:
        print("(nothing to post)")
        return
    for t in texts:
        print(f"  > {t}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["tick", "recap", "preview", "saturday", "remind", "pings", "race", "status",
                                    "detect", "enable", "disable"])
    ap.add_argument("--week", type=int)
    ap.add_argument("--team")
    ap.add_argument("--as-of", dest="as_of")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.FileHandler(LOG_PATH), logging.StreamHandler()])
    logging.getLogger("httpx").setLevel(logging.WARNING)
    now = dt.datetime.fromisoformat(a.as_of).replace(tzinfo=CT) if a.as_of else dt.datetime.now(CT)

    if a.cmd == "tick":
        return tick(dry_run=True if a.dry_run else None)
    if a.cmd == "status":
        cfg = ob.config()
        print(json.dumps({"paused": ob.paused(), "dry_run": cfg["dry_run"], "group_detected": bool(cfg["group_guid"]),
                          "name_stragglers": cfg["name_stragglers"],
                          "queued": len(list(ob.OUTBOX.glob("*.json"))) if ob.OUTBOX.exists() else 0}, indent=2))
        return 0
    if a.cmd == "detect":
        g = ob.detect()
        print("league group found" + (f" ({g['member_count']} members)" if g else "") if g else
              "no league group in Picky's Messages yet")
        return 0
    if a.cmd == "enable":
        n = ob.clear_dry_run_keys()
        ob.save_config(dry_run=False)
        ob.PAUSE_FILE.unlink(missing_ok=True)
        ob.log_event("enabled", cleared_dry_run_keys=n)
        print("Picky's group posts are ON")
        return 0
    if a.cmd == "disable":
        ob.PAUSE_FILE.write_text("paused via group_posts.py disable\n")
        os.chmod(ob.PAUSE_FILE, 0o600)
        ob.log_event("disabled")
        print("Picky's group posts are PAUSED")
        return 0

    lg = League(DB(), now)
    if a.cmd == "race":
        res, meta = pr.load(lg.db, now)
        if a.team:
            print(pr.need_answer(a.team, lg.nick(a.team), res, meta, lg.names))
        else:
            print(pr.summary_text(res, meta, lg.names))
        return 0
    if a.cmd == "recap":
        w = a.week or (lg.final_weeks() or [None])[-1]
        _show(f"recap week {w}", make_recap(lg, w))
    elif a.cmd == "preview":
        w = a.week or lg.open_week()
        _show(f"preview week {w}", make_preview(lg, w) if w else None)
    elif a.cmd == "saturday":
        w = a.week or lg.open_week()
        _show(f"saturday week {w}", make_saturday(lg, w) if w else None)
    elif a.cmd == "remind":
        w = a.week or lg.open_week()
        if w is None:
            _show("remind", None)
        else:
            lock = lg.lock_time(w)
            print("slots:", [(n, t.isoformat(timespec="minutes")) for n, t in reminder_slots(lock)] if lock else None)
            _show(f"remind week {w}", make_reminder(lg, w))
    elif a.cmd == "pings":
        with httpx.Client() as c:
            finals = due_pings(lg, c)
        _show("pings", make_ping(lg, finals) if finals else None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
