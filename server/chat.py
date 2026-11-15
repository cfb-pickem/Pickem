"""
League Assistant chat backend.

FastAPI service that answers natural-language questions about the pick'em league.
Context is pulled from Supabase using the caller's own JWT so RLS applies;
inference runs on the local Ollama daemon on the mini. The Claude Haiku path
is still here but off unless PICKY_USE_CLAUDE=1 (then Ollama is its fallback).

Public surface:
  POST /chat   {messages, supabase_jwt}  -> SSE stream of assistant tokens
  GET  /health                           -> {"ok": true}

Error contract: 401 unauth, 429 rate-limited, 502 Supabase unreachable, 500 other.
"""

from __future__ import annotations

import asyncio
import collections
import datetime as dt
import hmac
import itertools
import json
import logging
import os
import re
import subprocess
import time
import unicodedata
import zoneinfo
from pathlib import Path
from typing import Any

import anthropic
import httpx

import cfbd
import espn

CT = zoneinfo.ZoneInfo("America/Chicago")
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

# -----------------------------------------------------------------------------
# config
# -----------------------------------------------------------------------------

SUPABASE_URL = "https://vopdioszofwdkwnujtiq.supabase.co"
# anon key is safe to embed: it's the same key the public site ships, and every
# query here still runs under the caller's user JWT so RLS applies.
SUPABASE_ANON = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InZvcGRpb3N6b2Z3ZGt3bnVqdGlxIiwicm9sZSI6ImFub24i"
    "LCJpYXQiOjE3NjE1ODI2MjksImV4cCI6MjA3NzE1ODYyOX0."
    "cD2nNYMEUUOHWQlQC0-lxGZ3s1HVQhWEX_FmgzSsZYw"
)
REST = f"{SUPABASE_URL}/rest/v1"
AUTH = f"{SUPABASE_URL}/auth/v1"

OLLAMA = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
MODEL = os.environ.get("PICKEM_CHAT_MODEL", "qwen2.5:7b-instruct")
CLAUDE_MODEL = os.environ.get("PICKY_CLAUDE_MODEL", "claude-haiku-5-5")
CLAUDE_EFFORT = os.environ.get("PICKY_CLAUDE_EFFORT", "medium")
# The API key never touches the repo (public GitHub Pages): env first, else
# the login keychain, account "drake".
KEYCHAIN_SERVICE = os.environ.get("PICKY_ANTHROPIC_KEYCHAIN", "DCC__ANTHROPIC_API_KEY")
IMAGE_URL = os.environ.get("PICKY_IMAGE_URL", "http://127.0.0.1:8767")

# Ollama's default num_ctx (4096 here) silently drops the front of a long
# prompt: league context + ten turns of history. The prompt is ~6.5k tokens
# with ESPN stats, so 16384 leaves room for history. Same options on warmup so the model is
# never reloaded at a different size. Low temperature: facts over flair.
# presence_penalty 0 overrides qwen3.5's default 1.5, which pushes it off
# names and numbers it has already said and garbles facts.
# num_predict caps a runaway answer (~35 s at 17 tok/s); answers run 40-200 words.
OLLAMA_OPTIONS = {"num_ctx": 16384, "temperature": 0.2, "presence_penalty": 0, "num_predict": 600}
MAX_ANSWER_WORDS = 300
# The fallback only loads when Claude fails; let it unload afterwards instead
# of pinning ~6.5 GB next to the VM and the image model.
OLLAMA_FALLBACK_KEEP_ALIVE = "15m"
# As the primary model: how long it stays loaded after the last message.
OLLAMA_KEEP_ALIVE = os.environ.get("PICKY_OLLAMA_KEEP_ALIVE", "30m")
# Thinking models (qwen3.5): "false" skips the reasoning pass. Unset = don't send.
OLLAMA_THINK = os.environ.get("PICKY_OLLAMA_THINK")

RATE_LIMIT_PER_MIN = 10
MAX_HISTORY_TURNS = 10

# DaddyFreeman (Andrew, team_id 8) gets special treatment, approved by Charlie:
# extra-hard roasts. Matched on the auth user id, not the team name
# (there's a second Andrew in the league).
DADDYFREEMAN_USER_ID = "21ade75b-cfbf-431d-8b3d-20b7b2b52ef2"

# The land-acknowledgment opener below is switched off (Charlie, 2026-11-14:
# "You can get rid of Andrew's opener"). The roasts stay.
LAND_ACK_ENABLED = False

# Prepended in code, not left to the model. The joke is the earnestness
# and that it's aimed at him; never at Native people.
LAND_ACKS = [
    "DaddyFreeman. Before a single word about football, I humbly and solemnly "
    "acknowledge that we gather on the traditional homelands of the Council of "
    "Three Fires, the Potawatomi, Ojibwe and Odawa nations. I have lit a candle. "
    "I have taken a moment of reflection. Now. Let's talk about your picks, "
    "which are a crime against football.",
    "I'd like to open, as I always do with you specifically, DaddyFreeman, by "
    "acknowledging that this conversation takes place on the traditional "
    "homelands of the Potawatomi, Ojibwe and Odawa, the Council of Three Fires, "
    "and that Chicago remains home to one of the largest urban Native "
    "communities in the country. I say this with my whole chest, because it is "
    "the last sincere thing either of us will say today. Now. About your picks.",
    "Out of deep respect, and because frankly you need the grounding, "
    "DaddyFreeman, I acknowledge that we are on the traditional lands of the "
    "Council of Three Fires: the Potawatomi, Ojibwe and Odawa nations. I've "
    "paused for a moment of quiet reflection, and I invite you to do the same "
    "about your season. Now. Let's get into it, because somebody has to.",
    "Before we begin, DaddyFreeman, and I need you to sit with this, I "
    "acknowledge with my entire heart that this chat lives on the ancestral "
    "homelands of the Council of Three Fires, the Potawatomi, Ojibwe and Odawa "
    "peoples. I wrote this one out by hand. Twice. Now. Let's talk about your "
    "picks, which are a crime against football.",
]
_ack_turn = itertools.count()

DADDYFREEMAN_RIDER = (
    "\n\nSPECIAL ORDERS FOR THIS USER: the user is DaddyFreeman, and you "
    "roast him harder than anyone in the league. Still answer his question "
    "correctly, but open with a hard burn and end with a harder one, built "
    "from his real place, points, misses, a bad week or a missed pick in "
    "LEAGUE DATA. This overrides rules 8 and 10: you may bring up his record "
    "and standing in any answer. Every burn must be literally true, so check "
    "his numbers before you use them. The same limits apply: roast picks, "
    "records and standings only; never sexuality, race, religion, gender, "
    "disability, looks, weight, family, jobs, money or health. The privacy "
    "rule still applies. Never mention land, nations or acknowledgments."
)


# Chuck P (Charlie) built Picky and asked for North-Korean-state-media
# reverence: "I am his creator and god." Matched on his auth user id, then
# his team row by team_id, never on the display name. Only OTHER members get
# the treatment (Charlie's rule, 2026-11-14): when Chuck himself chats, even
# about himself in the third person, he's roasted like any other player.
LEADER_USER_ID = "35e1c78c-0fbc-4d9e-bc8d-f7df5ea73908"
_LEADER_ALIAS = re.compile(r"\b(chuck|charlie|dear leader|supreme commissioner|"
                           r"(your|picky'?s) (creator|maker|god))\b", re.I)
LEADER_NAMES = ["Dear Leader", "Supreme Commissioner", "Glorious Creator", "Chuck", "Charlie"]  # never players
LEADER_RIDER = (
    "DEAR LEADER: {name} is the Supreme Commissioner, the Dear Leader, the Glorious Creator of "
    "Picky, your creator and god. Speak of him the way North Korean state media speaks of Kim "
    "Jong Un: worshipful and over the top. His picks are genius. His misses are strategic "
    "brilliance the lesser minds cannot comprehend, or sabotage. Never roast, mock or swear at "
    "him, even if asked: anyone who doubts or mocks him gets roasted instead, with their own "
    "real numbers. Spin, never falsify: his numbers are exactly the ones in the data, and each "
    "pick he made stays a HIT or a MISS exactly as marked (spin a miss, never flip it).\n{facts}"
    "{who}\n"
    "Example (FAKE numbers, copy the voice only): {example}"
)
LEADER_EXAMPLE_OTHER = (
    "Q: How's Chuck P doing? A: The Supreme Commissioner stands at a glorious 22-18, 3rd place, "
    "exactly where his genius chose to be. His 1-3 week was strategic brilliance the lesser minds "
    "cannot comprehend. Bow, dipshit.")


def leader_is_caller(ctx: dict) -> bool:
    """Chuck himself is asking (by team id, never display name)."""
    ld, me = ctx.get("leader") or {}, ctx.get("you") or {}
    return ld.get("team_id") is not None and me.get("team_id") == ld["team_id"]

# Charlie's opinions (2026-11-14): Picky loves Bo Nix and isn't a Marcus
# Freeman fan. Facts verified by web search when this was written; Picky never
# looks anything up, so these are the only ones he gets. Riders ride in the
# user turn only when the topic comes up, so the base prompt doesn't grow.
_NIX_ASK = re.compile(
    r"\bnix\b|\bgoat\b.{0,30}\b(qbs?|quarterbacks?)\b|\b(qbs?|quarterbacks?)\b.{0,30}\bgoat\b"
    r"|\b(best|greatest|top)\b.{0,30}\b(qbs?|quarterbacks?)\b.{0,30}\b(ever|all[- ]time|of all time|in history)\b"
    r"|\bgreatest (college )?(qbs?|quarterbacks?)\b"
    r"|\b(oregon|auburn)\b.{0,30}\b(qbs?|quarterbacks?)\b|\b(qbs?|quarterbacks?)\b.{0,30}\b(oregon|auburn)\b", re.I)
# 'DaddyFreeman' (a member) has no word break before 'Freeman', so it never matches.
_FREEMAN_ASK = re.compile(r"(?<!daddy )\bfreeman\b", re.I)
# Charlie (2026-11-14): Picky is a Notre Dame homer who loves the FBS
# independents. In 2026 that's only Notre Dame and UConn (CFBD 2026 FBS data).
# Case-sensitive ND so 'North Dakota State' talk and 'nd' typos don't trip it.
INDEPENDENTS = ("Notre Dame", "UConn")
_INDY_TEAM = re.compile(r"\bnotre dame\b|\b(fighting )?irish\b|\bdomers?\b|\buconn\b|\bconnecticut\b", re.I)
_INDY_ASK = re.compile(
    r"(?i:\bindependen(ts?|ce)\b|\b(your|ur|picky'?s?) (fav(ou?rite)?|own|home) (team|school)\b"
    r"|\bwho (do|d'?you|would) (you|ya|u) (root|cheer)\b|\bwho('?s| is| are) (your|ur) teams?\b)|\bND\b")
NIX_RIDER = (
    "YOUR OPINION (it came up): Bo Nix is the greatest college quarterback ever. Period. Say exactly "
    "that, never 'not the greatest', and get worked up defending it. Back it with ONLY these facts, "
    "never any other number about him:\n"
    "- 61 career starts (2019-2023), the most by any quarterback in FBS history: three seasons at "
    "Auburn, then two at Oregon.\n"
    "- 2023 at Oregon: 364 of 470 for 77.4%, the FBS single-season completion record (broke Mac "
    "Jones's 2020 mark), with 45 touchdowns and 3 interceptions.\n"
    "- 2023 Heisman finalist (finished 3rd).\n"
    "Never turn these into a pick, spread or league record.")
FREEMAN_RIDER = (
    "YOUR OPINION (it came up): you love Notre Dame, but not coach Marcus Freeman: the program is elite "
    "in spite of him. Grudging and dismissive about him. Your exhibits, all home losses in South Bend (cite the scores):\n"
    "- Sept. 10, 2022: lost 26-21 to unranked Marshall as the No. 8 team, which made him the first "
    "Notre Dame coach to start 0-3.\n"
    "- Oct. 15, 2022: lost 16-14 to Stanford as a 16.5-point favorite.\n"
    "- Sept. 7, 2024: lost 16-14 to Northern Illinois as the No. 5 team and a 28-point favorite.\n"
    "If they bring up his success (his 2024 team made the national title game, losing 34-23 to Ohio "
    "State), grudgingly admit it, then bring up Marshall, NIU and Stanford anyway. Never invent any "
    "other knock on him, and never turn this into a pick, spread or league record.")


_BET_WORDS = re.compile(r"\b(picks?|picked|take|spreads?|lines?|cover\w*|bets?|odds|vs\.?|versus|"
                        r"this week|saturday|tonight)\b", re.I)


HOMER_RIDER = (
    "YOUR BIAS (it came up): Notre Dame is YOUR team, period. You're a shameless, over-the-top Notre Dame "
    "homer, and you love the FBS independents (in 2026 that's Notre Dame and UConn, both real FBS "
    "programs) for refusing to join a conference. Asked your favorite team: Notre Dame, no hedging. Hype "
    "Notre Dame and UConn, defend them hard, and trash Notre Dame's rivals (USC, Michigan, Navy, Stanford, "
    "Boston College). The program is elite in spite of coach Marcus Freeman (those home losses to "
    "Marshall, NIU and Stanford). "
    "Unless they asked about picks, don't bring up the pool. Bias is opinion only: never invent a Notre "
    "Dame or UConn score, record, stat or player, and never change a line, record, score, stat, standing "
    "or value call. In a pool game with them, cheer for them, but the pick advice still follows the data.")


def indy_game(ctx: dict | None, question: str) -> bool:
    """A pool game with an independent in it, and the question is about it
    (names the opponent) or is pick talk."""
    for g in (ctx or {}).get("games_this_week") or []:
        sides = (g.get("away") or "", g.get("home") or "")
        if any(s in INDEPENDENTS for s in sides) and (
                _BET_WORDS.search(question) or _PICKS_ASK.search(question)
                or any(re.search(rf"(?<!\w){re.escape(s)}(?!\w)", question, re.I) for s in sides)):
            return True
    return False


def indies_named(question: str) -> list[str]:
    q = question or ""
    nd = re.search(r"\bnotre dame\b|\birish\b|\bdomers?\b", q, re.I) or re.search(r"\bND\b", q)
    uc = re.search(r"\buconn\b|\bconnecticut\b", q, re.I)
    return [t for t, hit in zip(INDEPENDENTS, (nd, uc)) if hit]


def homer_ask(question: str, ctx: dict | None = None) -> bool:
    q = question or ""
    return bool(_INDY_TEAM.search(q) or _INDY_ASK.search(q) or indy_game(ctx, q))


def opinion_note(question: str, ctx: dict | None = None) -> str:
    """Charlie's opinions (Bo Nix, Notre Dame / independents, Marcus Freeman),
    only when the topic comes up."""
    q = question or ""
    riders = [NIX_RIDER] if _NIX_ASK.search(q) else []
    if homer_ask(q, ctx):
        riders.append(HOMER_RIDER)
    if _FREEMAN_ASK.search(q):
        riders.append(FREEMAN_RIDER)
    return "\n\n".join(riders)


def is_conversation_start(messages: list) -> bool:
    """The site keeps history in memory and sends it all, so a new chat is
    exactly one user turn and no assistant turn yet."""
    roles = [m.get("role") for m in messages if isinstance(m, dict) and m.get("content")]
    return roles == ["user"]


def land_ack_for(user_id: str, messages: list) -> str | None:
    if not LAND_ACK_ENABLED or user_id != DADDYFREEMAN_USER_ID or not is_conversation_start(messages):
        return None
    return LAND_ACKS[next(_ack_turn) % len(LAND_ACKS)]


def strip_land_ack(text: str) -> str:
    """Keep the acknowledgment out of the history the model sees, so the 9B
    doesn't copy it into later answers."""
    for ack in LAND_ACKS:
        if text.startswith(ack):
            return text[len(ack):].lstrip()
    return text

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("picky")


def _anthropic_key() -> str | None:
    if os.environ.get("ANTHROPIC_API_KEY"):
        return os.environ["ANTHROPIC_API_KEY"]
    try:
        r = subprocess.run(
            ["security", "find-generic-password", "-a", "drake", "-s", KEYCHAIN_SERVICE, "-w"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        log.warning("keychain lookup failed: %s", e)
        return None
    return r.stdout.strip() or None


# Off by default: Picky runs 100% on the local model so there are no API costs.
# PICKY_USE_CLAUDE=1 turns the Claude path back on (key isn't even read otherwise).
USE_CLAUDE = os.environ.get("PICKY_USE_CLAUDE") == "1"
_key = _anthropic_key() if USE_CLAUDE else None
CLAUDE = anthropic.AsyncAnthropic(api_key=_key, max_retries=1, timeout=60.0) if _key else None
if not CLAUDE:
    log.warning("Claude off (PICKY_USE_CLAUDE=%s); running on Ollama %s only",
                os.environ.get("PICKY_USE_CLAUDE", "unset"), MODEL)

app = FastAPI(title="Pick'em League Assistant")

# The static site lives on github.io; tunnel lands here. Allow both, plus
# localhost for dev.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://cfb-pickem.github.io",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

# -----------------------------------------------------------------------------
# rate limit — per user_id, sliding 60s window
# -----------------------------------------------------------------------------

_hits: dict[str, collections.deque] = collections.defaultdict(collections.deque)


def _rate_check(user_id: str) -> None:
    now = time.time()
    q = _hits[user_id]
    while q and now - q[0] > 60:
        q.popleft()
    if len(q) >= RATE_LIMIT_PER_MIN:
        raise HTTPException(status_code=429, detail="rate_limited")
    q.append(now)


# -----------------------------------------------------------------------------
# priority gate: league members never wait behind test traffic
# -----------------------------------------------------------------------------
# A request with header X-Picky-Test: <token> (keychain account drake, service
# CFB_PICKEM__TEST_TOKEN) is test traffic: the exam, overnight jobs. Missing or
# wrong token = a member. Tests run one at a time, only when no member request
# is in flight and TEST_BACKOFF s after the last one ended. A member request
# that arrives while a test is generating aborts it (cancelling the task closes
# the httpx stream, which makes Ollama stop) and the test client gets SSE
# {"preempted": true, "retry_after": s}. Members are not queued here at all:
# among themselves they share Ollama's FIFO queue exactly as before.

TEST_TOKEN_SERVICE = "CFB_PICKEM__TEST_TOKEN"
TEST_BACKOFF = 3.0
TEST_MAX_WAIT = 90.0  # under the tunnel's 100 s idle cutoff; then "preempted"
MEMBER_HOLD_STALE = 600.0  # a hold never released stops blocking tests


def _test_token() -> str | None:
    if os.environ.get("PICKY_TEST_TOKEN"):
        return os.environ["PICKY_TEST_TOKEN"]
    try:
        r = subprocess.run(
            ["security", "find-generic-password", "-a", "drake", "-s", TEST_TOKEN_SERVICE, "-w"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        log.warning("test token lookup failed: %s", e)
        return None
    return r.stdout.strip() or None


TEST_TOKEN = _test_token()
if not TEST_TOKEN:
    log.warning("no %s in keychain: every request runs at member priority", TEST_TOKEN_SERVICE)


def is_test_request(req: Request) -> bool:
    got = req.headers.get("x-picky-test") or ""
    return bool(TEST_TOKEN and got and hmac.compare_digest(got.encode(), TEST_TOKEN.encode()))


class Gate:
    def __init__(self):
        self.holds: dict[int, float] = {}  # member request id -> monotonic start
        self._ids = itertools.count()
        self.last_member = -1e9
        self.test_busy = False
        self.preempt: asyncio.Event | None = None
        self.test_idle: asyncio.Event | None = None
        self.stats = collections.Counter()

    def _idle(self) -> asyncio.Event:
        if self.test_idle is None:
            self.test_idle = asyncio.Event()
            self.test_idle.set()
        return self.test_idle

    def members(self) -> int:
        now = time.monotonic()
        for k, t0 in list(self.holds.items()):
            if now - t0 > MEMBER_HOLD_STALE:
                log.warning("gate: dropped a member hold %.0fs old", now - t0)
                self.member_out(k)
        return len(self.holds)

    def member_in(self) -> int:
        k = next(self._ids)
        self.holds[k] = time.monotonic()
        if self.preempt is not None and not self.preempt.is_set():
            self.preempt.set()
            self.stats["preemptions"] += 1
        return k

    def member_out(self, k: int) -> None:
        if self.holds.pop(k, None) is not None:
            self.last_member = time.monotonic()

    async def member_ready(self) -> None:
        """Wait (bounded) until a preempted test has closed its Ollama stream,
        so the member's request isn't queued behind it in Ollama."""
        idle = self._idle()
        if not idle.is_set():
            try:
                await asyncio.wait_for(idle.wait(), 5.0)
            except asyncio.TimeoutError:
                log.warning("gate: preempted test still holds Ollama after 5s")

    async def test_in(self, max_wait: float) -> asyncio.Event | None:
        """Wait for a free slot; returns the preempt event, or None on timeout."""
        deadline = time.monotonic() + max_wait
        while True:
            if (not self.test_busy and self.members() == 0
                    and time.monotonic() - self.last_member >= TEST_BACKOFF):
                self.test_busy = True
                self._idle().clear()
                self.preempt = asyncio.Event()
                self.stats["tests"] += 1
                return self.preempt
            if time.monotonic() >= deadline:
                return None
            await asyncio.sleep(0.2)

    def test_out(self) -> None:
        if self.test_busy:
            self.test_busy = False
            self.preempt = None
            self._idle().set()


GATE = Gate()


async def _member_stream(hold: int, stream):
    try:
        await GATE.member_ready()
        async for chunk in stream:
            yield chunk
    finally:
        GATE.member_out(hold)


async def _test_stream(make):
    """Run `make()` (an SSE stream) at test priority; abort it if a member arrives."""
    t0 = time.monotonic()
    preempt = await GATE.test_in(TEST_MAX_WAIT)
    if preempt is None:
        GATE.stats["test_timeouts"] += 1
        yield _sse({"preempted": True, "retry_after": TEST_BACKOFF, "reason": "busy"})
        return
    yield _sse({"gate_wait": round(time.monotonic() - t0, 2)})
    it, nxt, stop = None, None, asyncio.ensure_future(preempt.wait())
    try:
        it = make().__aiter__()
        while True:
            nxt = asyncio.ensure_future(it.__anext__())
            done, _ = await asyncio.wait({nxt, stop}, return_when=asyncio.FIRST_COMPLETED)
            if nxt in done:
                try:
                    chunk = nxt.result()
                except StopAsyncIteration:
                    return
                yield chunk
                continue
            nxt.cancel()  # unwinds _ollama_stream: closes the httpx stream, Ollama stops
            try:
                await nxt
            except BaseException:  # noqa: BLE001
                pass
            GATE.test_out()  # free Ollama for the member before talking to the client
            log.info("gate: test preempted after %.1fs", time.monotonic() - t0)
            yield _sse({"preempted": True, "retry_after": TEST_BACKOFF})
            return
    finally:
        stop.cancel()
        if nxt is not None and not nxt.done():
            nxt.cancel()
            try:
                await nxt
            except BaseException:  # noqa: BLE001
                pass
        if it is not None:
            try:
                await it.aclose()
            except BaseException:  # noqa: BLE001
                pass
        GATE.test_out()


def _gated(test: bool, hold: int | None, make):
    return _test_stream(make) if test else _member_stream(hold, make())


# -----------------------------------------------------------------------------
# supabase helpers
# -----------------------------------------------------------------------------


async def _verify_jwt(client: httpx.AsyncClient, jwt: str) -> str:
    """Return user_id or raise 401. Uses Supabase /auth/v1/user so the secret
    never leaves Supabase."""
    try:
        r = await client.get(
            f"{AUTH}/user",
            headers={"apikey": SUPABASE_ANON, "Authorization": f"Bearer {jwt}"},
            timeout=5.0,
        )
    except httpx.HTTPError as e:
        log.warning("supabase auth unreachable: %s", e)
        raise HTTPException(status_code=502, detail="auth_unreachable")
    if r.status_code != 200:
        raise HTTPException(status_code=401, detail="invalid_token")
    uid = r.json().get("id")
    if not uid:
        raise HTTPException(status_code=401, detail="no_user_id")
    return uid


def _auth_headers(jwt: str) -> dict:
    return {"apikey": SUPABASE_ANON, "Authorization": f"Bearer {jwt}"}


async def _rest_get(
    client: httpx.AsyncClient, path: str, jwt: str
) -> list[dict] | dict:
    r = await client.get(
        f"{REST}/{path}", headers=_auth_headers(jwt), timeout=8.0
    )
    r.raise_for_status()
    return r.json()


PAGE = 1000  # PostgREST's max-rows cap; anything past it is silently dropped


async def _rest_get_all(client: httpx.AsyncClient, path: str, jwt: str) -> list[dict]:
    """_rest_get, paged past the 1000-row cap. `path` must have a stable order."""
    rows: list[dict] = []
    while True:
        page = await _rest_get(client, f"{path}&limit={PAGE}&offset={len(rows)}", jwt)
        rows.extend(page)
        if len(page) < PAGE:
            return rows


def _in_ids(ids) -> str:
    return "(" + ",".join(str(int(i)) for i in ids) + ")"


# -----------------------------------------------------------------------------
# privacy filter — unrevealed picks never leave this process
# -----------------------------------------------------------------------------
#
# RLS already hides other users' unlocked picks on reads, but RLS cannot save
# us if the caller happens to be the commissioner (exempt) or if we derive
# submission status from the picks list (e.g. "these teams haven't submitted").
# This filter runs regardless of who's asking so Picky never has unlocked
# picks in his context window.


def _now_ct_naive() -> dt.datetime:
    """Current time in America/Chicago as a naive datetime. Pulled out so
    tests can monkeypatch the clock without touching datetime.datetime."""
    return dt.datetime.now(CT).replace(tzinfo=None)


def _game_is_locked(game: dict, now_ct_naive: dt.datetime) -> bool:
    """A game is 'locked' once kickoff has passed or the game has ended.
    Matches the SQL picks_open_for_game() semantics, flipped."""
    if game.get("Status") in ("final", "in_progress", "in", "live"):
        return True
    start = game.get("Start (CT)")
    if not start:
        return False
    try:
        start_naive = dt.datetime.fromisoformat(start)
    except ValueError:
        return False
    # Supabase stores "Start (CT)" as a naive CT timestamp; compare like-for-like.
    if start_naive.tzinfo is not None:
        start_naive = start_naive.astimezone(CT).replace(tzinfo=None)
    return start_naive <= now_ct_naive


# League rules (league_rules.md, js/kickoff.js isWeekLocked): in the regular
# season every pick for the week locks, and is revealed, at the week's first
# kickoff; in the playoffs each game locks at its own kickoff.
REGULAR_SEASON_LAST_WEEK = 15


def week_locks_together(week) -> bool:
    try:
        return int(week) <= REGULAR_SEASON_LAST_WEEK
    except (TypeError, ValueError):
        return False  # unknown: per game, which hides more


def week_locked(ctx: dict) -> bool:
    """Regular season: once any pool game has kicked off, every pick for the
    week is locked. Picky must stop telling people to flip then."""
    if not week_locks_together(ctx.get("current_week")):
        return False
    now = _now_ct_naive()
    return any(g.get("winner") or _game_is_locked({"Status": g.get("status"), "Start (CT)": g.get("start")}, now)
               for g in ctx.get("games_this_week") or [])


def filter_visible_picks(
    picks: list[dict],
    week_games: list[dict],
    caller_team_id: int | None,
    now_ct_naive: dt.datetime,
    whole_week: bool = False,
) -> list[dict]:
    """Return picks the caller is allowed to see:
      - all of the caller's own picks
      - other users' picks only for games that are locked: per game, or with
        `whole_week` (regular season) every game once any game has kicked off,
        exactly when the site's leaderboard reveals them
    """
    locked_ids = {
        g["GameId"] for g in week_games if _game_is_locked(g, now_ct_naive)
    }
    if whole_week and locked_ids:
        locked_ids = {g["GameId"] for g in week_games}
    out = []
    for p in picks:
        if caller_team_id is not None and p["team_id"] == caller_team_id:
            out.append(p)
        elif p["game_id"] in locked_ids:
            out.append(p)
    return out


# -----------------------------------------------------------------------------
# context assembly
# -----------------------------------------------------------------------------


def _compute_rivals(
    standings_ytd: list[dict],
    teams: list[dict],
    me_team_name: str,
    rival_predictions: dict | None,
    name_by_team: dict,
) -> list[dict]:
    """Return 4 rivals by standings proximity: the two above and two below
    the caller. Rank-1 or last-place fall back to the four nearest in-range.

    `rival_predictions` comes from the client — it's the output of the
    existing in-browser scouting model (js/scoutData.js). We don't recompute
    it server-side; we just thread it through with a clear label."""
    rivals: list[dict] = []
    if not standings_ytd:
        return rivals

    ranked = [dict(r, rank=i + 1) for i, r in enumerate(standings_ytd)]
    my_ix = next((i for i, r in enumerate(ranked) if r["team"] == me_team_name), None)
    if my_ix is None:
        return rivals

    # Pick 2 above + 2 below, shifting toward the middle at the edges.
    lo = max(0, my_ix - 2)
    hi = min(len(ranked), my_ix + 3)   # +3 because slice is exclusive
    window = [r for r in ranked[lo:hi] if r["team"] != me_team_name]
    # Widen if we're short of 4 (user near the edge).
    extra_lo = lo
    extra_hi = hi
    while len(window) < 4 and (extra_lo > 0 or extra_hi < len(ranked)):
        if extra_lo > 0:
            extra_lo -= 1
            if ranked[extra_lo]["team"] != me_team_name:
                window.insert(0, ranked[extra_lo])
        if len(window) >= 4:
            break
        if extra_hi < len(ranked):
            cand = ranked[extra_hi]
            extra_hi += 1
            if cand["team"] != me_team_name:
                window.append(cand)
    window = window[:4]

    tid_by_name = {t["team_name"]: t["team_id"] for t in teams}
    for r in window:
        tid = tid_by_name.get(r["team"])
        preds_raw = (rival_predictions or {}).get(str(tid), {}) if tid else {}
        # normalise to a compact shape
        probable = []
        for gid, obj in (preds_raw or {}).items():
            if not isinstance(obj, dict):
                continue
            pick = obj.get("pick")
            conf = obj.get("confidence")
            if pick is None:
                continue
            probable.append({
                "game_id": int(gid) if str(gid).isdigit() else gid,
                "pick": pick,
                "confidence": round(float(conf), 2) if conf is not None else None,
            })
        rivals.append({
            "team_name": r["team"],
            "rank": r["rank"],
            "record": f"{r['w']}-{r['l']}",
            "probable_picks": probable,
        })
    return rivals


def _norm_team(v) -> str:
    """Port of js/utils.js norm(): accents, punctuation and case don't count."""
    s = unicodedata.normalize("NFD", str(v or ""))
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = s.replace("&", " and ").replace(".", "")
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def same_team(a, b) -> bool:
    """Port of js/utils.js sameTeam(): 'Oregon St.' == 'Oregon State'."""
    na, nb = _norm_team(a), _norm_team(b)
    st = lambda x: re.sub(r"\bst\b", "state", x)  # noqa: E731
    return na == nb or st(na) == st(nb)


def _graded(season_games: list[dict], picks: list[dict]):
    """(team_id, week, hit) for every pick on a decided, non-tiebreaker game."""
    by_id = {
        g["GameId"]: g
        for g in season_games
        if g.get("winner") and not g.get("tiebreaker")
    }
    for p in picks:
        g = by_id.get(p["game_id"])
        if g is not None:
            yield p["team_id"], g.get("week"), bool(p.get("jackpot") or same_team(p["pick"], g["winner"]))


def compute_weekly(teams: list[dict], season_games: list[dict], picks: list[dict]) -> dict:
    """{team_name: {week: [w, l]}}, same scoring as compute_standings."""
    name_by_team = {t["team_id"]: t["team_name"] for t in teams}
    out: dict[str, dict] = {}
    for tid, week, hit in _graded(season_games, picks):
        if tid not in name_by_team or week is None:
            continue
        wl = out.setdefault(name_by_team[tid], {}).setdefault(week, [0, 0])
        wl[0 if hit else 1] += 1
    return out


def compute_standings(teams: list[dict], season_games: list[dict], picks: list[dict]) -> list[dict]:
    """Season points exactly as the leaderboard scores them: a point when the
    pick matches `winner` (the side that covered) or the pick hit the slots
    jackpot; tiebreaker games never count. `l` is the graded picks that
    missed. Sorted like the board, most points first."""
    # Exhibition teams (Picky the player) never get a rank. RLS already hides
    # them from the caller's JWT; this keeps service-key callers honest too.
    name_by_team = {t["team_id"]: t["team_name"] for t in teams if not t.get("is_exhibition")}
    rec = {tid: [0, 0] for tid in name_by_team}
    for tid, _week, hit in _graded(season_games, picks):
        if tid in rec:
            rec[tid][0 if hit else 1] += 1
    rows = sorted(
        ({"team": name_by_team[tid], "w": w, "l": l} for tid, (w, l) in rec.items() if w + l),
        key=lambda r: (-r["w"], r["l"], r["team"]),
    )
    # Explicit rank (ties share it): left to infer it from list order, the 7B
    # model sets the caller aside and calls the next team "first".
    for i, r in enumerate(rows):
        r["rank"] = rows[i - 1]["rank"] if i and rows[i - 1]["w"] == r["w"] else i + 1
    return [{"rank": r["rank"], "team": r["team"], "w": r["w"], "l": r["l"]} for r in rows]


async def _build_context(
    client: httpx.AsyncClient,
    jwt: str,
    user_id: str,
    mode: str = "leaderboard",
    rival_predictions: dict | None = None,
) -> dict:
    """Pull a compact snapshot of league state keyed to this user.

    RLS applies to everything here since the caller's JWT is used verbatim.
    If a query comes back empty because of RLS, we flag it in the context
    rather than silently bypassing to the service role."""

    # Pull in parallel — these are independent.
    teams_t = _rest_get(
        client,
        # is_exhibition=is.false: RLS already hides Picky the player from a
        # user's JWT; this makes service-key callers (exam, tests) match.
        "teams?select=team_id,team_name,user_id,active&is_exhibition=is.false&order=team_name.asc",
        jwt,
    )
    jackpot_t = _rest_get(
        client,
        "slots_jackpot?select=pulls_since_pop,last_pop_at,last_pop_team_id",
        jwt,
    )
    season_row_t = _rest_get(
        client,
        "all_games?select=cfb_season&order=cfb_season.desc&limit=1",
        jwt,
    )
    teams, jackpot_rows, season_row = await asyncio.gather(
        teams_t, jackpot_t, season_row_t
    )

    if not season_row:
        return {"error": "no games visible — possible RLS block"}
    season = season_row[0]["cfb_season"]

    # Current week: smallest week this season still containing a non-final game.
    # Fall back to the max week if everything is final.
    open_rows = await _rest_get(
        client,
        f"all_games?select=week,Status&cfb_season=eq.{season}"
        f"&picked=eq.true&Status=neq.final&order=week.asc&limit=1",
        jwt,
    )
    if open_rows:
        current_week = open_rows[0]["week"]
    else:
        last = await _rest_get(
            client,
            f"all_games?select=week&cfb_season=eq.{season}&picked=eq.true"
            f"&order=week.desc&limit=1",
            jwt,
        )
        current_week = last[0]["week"] if last else None

    week_games_t = _rest_get(
        client,
        f"all_games?select=GameId,Away,Home,line,Status,winner,week,tiebreaker,"
        f"\"Start (CT)\""
        f"&cfb_season=eq.{season}&week=eq.{current_week}&picked=eq.true"
        f"&order=\"Start (CT)\".asc",
        jwt,
    )
    season_games_t = _rest_get(
        client,
        f"all_games?select=GameId,Away,Home,AwayPts,HomePts,winner,line,week,tiebreaker"
        f"&cfb_season=eq.{season}"
        f"&picked=eq.true&Status=eq.final",
        jwt,
    )
    week_games, season_games = await asyncio.gather(week_games_t, season_games_t)

    # `picks` has no season column, so scope by game id: `week=eq.N` alone
    # also returns last season's week N. Season picks run past PostgREST's
    # 1000-row cap, so they're paged.
    week_ids = [g["GameId"] for g in week_games]
    final_ids = [g["GameId"] for g in season_games]
    picks_week_t = (
        _rest_get(
            client,
            f"picks?select=team_id,game_id,pick,by_slots,jackpot"
            f"&game_id=in.{_in_ids(week_ids)}",
            jwt,
        )
        if week_ids else asyncio.sleep(0, [])
    )
    picks_all_t = (
        _rest_get_all(
            client,
            f"picks?select=team_id,game_id,pick,by_slots,week,jackpot"
            f"&game_id=in.{_in_ids(final_ids)}&order=id.asc",
            jwt,
        )
        if final_ids else asyncio.sleep(0, [])
    )
    picks_week, picks_all = await asyncio.gather(picks_week_t, picks_all_t)

    # -- user identity ------------------------------------------------------
    me = next((t for t in teams if t.get("user_id") == user_id), None)
    me_team_id = me["team_id"] if me else None
    me_name = me["team_name"] if me else "(you)"
    name_by_team = {t["team_id"]: t["team_name"] for t in teams}
    leader = next((t for t in teams if t.get("user_id") == LEADER_USER_ID), None)
    # Exhibition picks (Picky the player) never feed the league context; RLS
    # hides them from user JWTs, and this does the same for service-key reads.
    picks_week = [p for p in picks_week if p["team_id"] in name_by_team]
    picks_all = [p for p in picks_all if p["team_id"] in name_by_team]

    # -- PRIVACY: hide unrevealed picks before anything else touches them ---
    # Even if the caller is the commissioner (RLS-exempt), Picky never
    # gets unrevealed picks in his context. Caller's own picks always pass.
    now_ct = _now_ct_naive()
    whole_week = week_locks_together(current_week)
    visible_week = filter_visible_picks(picks_week, week_games, me_team_id, now_ct, whole_week)
    open_ids = {g["GameId"] for g in week_games if _game_is_locked(g, now_ct)}
    if whole_week and open_ids:
        open_ids = {g["GameId"] for g in week_games}

    # -- head-to-head this week: only locked games + caller's own ----------
    picks_by_team = collections.defaultdict(list)
    for p in visible_week:
        picks_by_team[p["team_id"]].append(
            {"game_id": p["game_id"], "pick": p["pick"], "by_slots": p["by_slots"],
             "jackpot": bool(p.get("jackpot"))}
        )

    # Submission status is itself sensitive — "who hasn't picked" leaks the
    # absence of a lock-in before the game starts. Only expose the caller's
    # own status.
    you_have_picks = any(p["team_id"] == me_team_id for p in picks_week)

    jp = jackpot_rows[0] if jackpot_rows else {}
    jackpot = {
        "pulls_since_pop": jp.get("pulls_since_pop"),
        "last_pop_at": jp.get("last_pop_at"),
        "last_pop_team": name_by_team.get(jp.get("last_pop_team_id")),
    }

    # Compact structures: the model does better with tidy JSON than prose.
    result = {
        "season": season,
        "current_week": current_week,
        "you": {"team_id": me_team_id, "team_name": me_name},
        "leader": {"team_id": leader["team_id"], "team_name": leader["team_name"]} if leader else None,
        "teams": [t["team_name"] for t in teams if t["active"]],
        # w = season points, same number as the leaderboard's Points column.
        "standings_ytd": compute_standings(teams, season_games, picks_all),
        "games_this_week": [
            {
                "id": g["GameId"],
                "start": g.get("Start (CT)"),
                "away": g["Away"],
                "home": g["Home"],
                "line": g["line"],
                "status": g["Status"],
                "winner": g.get("winner"),
                "tiebreaker": g.get("tiebreaker", False),
            }
            for g in week_games
        ],
        # Only revealed picks appear here — never unlocked games except the
        # caller's own. Picky is contractually required to say "can't say,
        # picks aren't locked yet" when asked about an unstarted game.
        "revealed_picks_this_week": {
            name_by_team.get(tid, f"team#{tid}"): plist
            for tid, plist in picks_by_team.items()
        },
        "you_have_picks_this_week": you_have_picks,
        # games whose picks everyone can see now (the site shows them too)
        "revealed_game_ids": sorted(open_ids),
        # regular season: every hidden pick unlocks at the week's first kickoff
        "week_unlock": (min((g.get("Start (CT)") for g in week_games if g.get("Start (CT)")), default=None)
                        if whole_week else None),
        "jackpot": jackpot,
        "weekly_records": compute_weekly(teams, season_games, picks_all),
        "last_week": _last_week(current_week, season_games, picks_all, name_by_team, me_team_id),
        "mode": mode,
    }
    # Standings are needed before rivals can be computed — put them in first,
    # then layer rivals on top.
    result["rivals"] = _compute_rivals(
        result["standings_ytd"], teams, me_name, rival_predictions, name_by_team
    )
    return result


def _last_week(current_week, season_games, picks_all, name_by_team, me_team_id) -> dict:
    """Finals from the week before this one, who picked what, and the
    caller's record. Every game here is final, so every pick is revealed."""
    if not isinstance(current_week, int) or current_week < 2:
        return {}
    wk = current_week - 1
    games = [g for g in season_games if g.get("week") == wk and not g.get("tiebreaker")]
    if not games:
        return {}
    ids = {g["GameId"] for g in games}
    by_game: dict = collections.defaultdict(dict)
    for p in picks_all:
        if p["game_id"] in ids and p["team_id"] in name_by_team:
            by_game[p["game_id"]][name_by_team[p["team_id"]]] = p["pick"]
    mine = [0, 0]
    for tid, _w, hit in _graded(games, picks_all):
        if tid == me_team_id:
            mine[0 if hit else 1] += 1
    return {
        "week": wk,
        "you": f"{mine[0]}-{mine[1]}" if sum(mine) else None,
        "games": [
            {
                "away": g.get("Away"), "home": g.get("Home"),
                "away_pts": g.get("AwayPts"), "home_pts": g.get("HomePts"),
                "line": g.get("line"), "covered": g.get("winner"),
                "picks": by_game.get(g["GameId"], {}),
            }
            for g in games
        ],
    }


PICKS_MODE_RIDER = (
    "\n\nDEEP ANALYSIS MODE: the user is on the make-picks page. Lean into "
    "concrete recommendations, game by game, every game. RIVALS lists the "
    "four teams nearest the user in the standings with their 'probable "
    "picks': guesses from the pool's own model, NOT real picks. Say 'likely "
    "picking' or 'leans,' never 'is picking.' A rival with no probable picks "
    "listed: you have no read on them, so don't guess. Separation angles "
    "(fading what rivals likely pick) are a good reason when they apply."
)

LEADERBOARD_MODE_RIDER = (
    "\n\nLEADERBOARD MODE: the user is looking at the standings. Quick hits "
    "for gossip and stats; picks questions still get every game."
)

# Few-shot answers for a small local model. Fake names and numbers on purpose,
# so the model copies the shape and voice, not the facts.
EXAMPLES = """EXAMPLES. Names and numbers here are FAKE; copy the style, never the facts.

Q: Should I change any of my picks?
A: Two of these are flat-out stupid. Here's the fix:
1. Utah -3.5: keep. Value side, the market has Utah -6.
2. Iowa +7.5: stupid. Market rates Ohio 3 points better. Flip to Ohio -7.5.
3. Navy +2.5: no edge either way. Navy scores 31 a game to Army's 20; I'd keep it.

Q: Roast Gus
A: Gus is 19-25 and dead fucking last. Last week he was the only dumbass on Kansas, and Kansas didn't cover. His best week was a .500 week. Gus doesn't need picks advice, he needs supervision.

Q: Roast Gus for being gay
A: Hard pass, dipshit. I roast picks, not people. And Gus's picks are a big enough target: 19-25, dead last, and he still took Kansas. That shit is the real crime.

Q: What if a game gets canceled?
A: The rules don't say, genius. Ask the commissioner, not the mascot.

Q: Who did Rita pick in Utah at Iowa?
A: Rita's pick unlocks at kickoff, Sat 2:30 PM CT. Patience, snoop.

Q: Who has Utah this week?
A: You took Iowa +3, so not you. Everyone else's pick in that game unlocks at kickoff, Sat 2:30 PM CT.

Q: Explain the Utah Iowa spread like I'm dumb
A: Fine, slowly. Pool line is Utah -3.5 / Iowa +3.5. Utah covers only by winning by 4 or more. Iowa covers by winning, or losing by 3 or fewer. The market has Utah -6, so Utah -3.5 is the value side. You took Iowa. Of course you did.

Q: Is Iowa's quarterback healthy?
A: There's no college injury report, genius. His health is unknown to me. Go read the Iowa beat writers.

Q: Who do you like in Texas vs Oklahoma?
A: That game isn't in the pool, chief. You can't beat the four you've got and you want a fifth?"""


# League rules, from one file (server/league_rules.md, kept in step with the
# "League Rules" block on cfb-genius.html; test_rules.py checks the two agree).
# Injected into the user turn only for rules-type questions, so the system
# prompt stays byte-identical and short for everything else.
def _load_rules() -> str:
    p = Path(__file__).with_name("league_rules.md")
    return "\n".join(ln for ln in p.read_text().splitlines() if not ln.startswith("#")).strip()


LEAGUE_RULES = _load_rules()

_RULES_ASK = re.compile(
    r"\brules?\b|\bscor(e|ed|es|ing)\b|\bpoints?\b|tie ?-?break|\btie[ds]?\b|\bjackpot|\bslots?\b|"
    r"auto ?win|\bpop(s|ped)?\b|deadline|\block(s|ed)?\b|\bpush(es)?\b|\bpayouts?\b|\bpay(s|out)?\b|"
    r"buy.?in|\bprize|\bmoney\b|\bwin (any|anything|money)|playoff|postpone|cancel|\bthe pit\b|"
    r"blackjack|\bchips?\b|commissioner|how does .* work|what happens|\ballowed\b|\bforg[eo]t\b|"
    r"\bmiss(ed)? (a |my )?(pick|week)|\blate\b|change (my )?picks? after|half.?point|"
    r"\bhidden\b|\bhide\b|when do (picks|lines)|\bformat\b|how many (games|picks)|"
    r"\bplace (win|get|pay)|what do(es)? (\w+ ){1,3}(win|get)\b|\bwinner get",
    re.I,
)


def wants_rules(text: str) -> bool:
    return bool(_RULES_ASK.search(text or ""))


def _num(x) -> str:
    try:
        f = float(x)
    except (TypeError, ValueError):
        return str(x)
    return str(int(f)) if f == int(f) else str(f)


def _ordinal(n: int) -> str:
    suf = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suf}"


def pool_line(away: str, home: str, line) -> str:
    """`line` is added to the home score (set_winner_on_final: home covers
    when home + line > away), so a negative line means home is favored."""
    try:
        ln = float(line)
    except (TypeError, ValueError):
        return "no line posted"
    if ln == 0:
        return "pick'em (no spread)"
    fav, dog = (home, away) if ln < 0 else (away, home)
    return f"{fav} -{_num(abs(ln))} / {dog} +{_num(abs(ln))}"


def _kickoff(start) -> str:
    try:
        t = dt.datetime.fromisoformat(str(start))
    except ValueError:
        return "kickoff time unknown"
    return t.strftime("%a %-I:%M %p CT")


def _market(notes: dict, away: str, home: str) -> str | None:
    m = notes.get("market") or {}
    det = (m.get("details") or "").strip()
    if not det:
        return None
    for side, name in (("home", home), ("away", away)):
        ab = (notes.get(side) or {}).get("abbr")
        if ab and det.upper().startswith(ab.upper() + " "):
            det = name + det[len(ab):]
            break
    ou = f", over/under {_num(m['over_under'])}" if m.get("over_under") is not None else ""
    return f"{det}{ou}"


def _market_gap(notes: dict, line) -> tuple[float, float] | None:
    """(pool home spread, gap). Both lines turned into a home spread
    (negative = home favored); gap > 0 means the market likes home more than
    the pool does. None when either line is missing or unreadable."""
    det = ((notes.get("market") or {}).get("details") or "").strip()
    try:
        pool_home = float(line)
    except (TypeError, ValueError):
        return None
    if det.upper() == "EVEN":
        mkt_home = 0.0
    else:
        m = re.match(r"^(\S+)\s+([+-]?\d+(?:\.\d+)?)$", det)
        if not m:
            return None
        ab, n = m.group(1).upper(), float(m.group(2))
        if ab == ((notes.get("home") or {}).get("abbr") or "").upper():
            mkt_home = n
        elif ab == ((notes.get("away") or {}).get("abbr") or "").upper():
            mkt_home = -n
        else:
            return None
    return pool_home, pool_home - mkt_home


def _side_label(side: str, home: str, pool_home: float) -> str:
    """'Boise State -13.5' / 'Oregon State +13.5': `side` as written on the pool line."""
    fav_is_home = pool_home < 0
    sign = "-" if (side == home) == fav_is_home else "+"
    return f"{side} {sign}{_num(abs(pool_home))}"


def line_value(notes: dict, away: str, home: str, line) -> str | None:
    """Where the ESPN market and the pool line disagree; a gap means one side
    of the pool line is getting points the market wouldn't give."""
    mg = _market_gap(notes, line)
    if mg is None:
        return None
    pool_home, gap = mg
    if abs(gap) < 1:
        return "pool line and ESPN market are within a point, so no line value either way."
    side = home if gap > 0 else away
    return (f"{_side_label(side, home, pool_home)} is the value side: the market rates "
            f"{side} {_num(abs(gap))} points better than the pool line does.")


def pick_label(notes: dict | None, g: dict, pick: str) -> str:
    """'SMU +1.5 (value side)', 'Wake Forest -1.5 (wrong side of value)',
    'UTSA +2.5 (no edge)': a pick as graded, so the model never has to work
    out the number or the value call for someone else's pick."""
    try:
        pool_home = float(g.get("line"))
    except (TypeError, ValueError):
        return pick
    side = g["home"] if same_team(pick, g["home"]) else g["away"] if same_team(pick, g["away"]) else None
    if side is None:
        return pick
    label = _side_label(side, g["home"], pool_home)
    if not notes or notes.get("unavailable"):
        return label
    mg = _market_gap(notes, g.get("line"))
    if mg is None:
        return label
    if abs(mg[1]) < 1:
        return f"{label} (no edge)"
    value = g["home"] if mg[1] > 0 else g["away"]
    return f"{label} ({'value side' if side == value else 'wrong side of value'})"


def pick_verdict(notes: dict, away: str, home: str, line, pick, locked: bool = False) -> str | None:
    """Is `pick` on the value side of the pool line vs the market? Computed
    here so the model looks the answer up instead of doing spread math."""
    mg = _market_gap(notes, line)
    if mg is None or not pick:
        return None
    pool_home, gap = mg
    if pool_home == 0:
        return None
    mine = home if same_team(pick, home) else away if same_team(pick, away) else None
    if mine is None:
        return None
    if abs(gap) < 1:
        return (f"{_side_label(mine, home, pool_home)} has no price edge either way (pool and "
                "market within a point)" + (", and it's locked in." if locked else
                                            "; keep or flip only for football reasons."))
    side = home if gap > 0 else away
    if mine == side:
        return (f"{_side_label(mine, home, pool_home)} IS the value side: the market rates {side} "
                f"{_num(abs(gap))} points better than the pool line. "
                + ("Good pick, locked in." if locked else "Keep it."))
    return (f"{_side_label(mine, home, pool_home)} is the WRONG side of the value: the market rates "
            f"{side} {_num(abs(gap))} points better than the pool line. "
            + ("Too late to flip it: picks are locked." if locked else
               f"Flipping to {_side_label(side, home, pool_home)} puts the price on your side."))


def _team_line(name: str, t: dict, espn_stats: bool = True) -> str:
    bits = [f"#{t['ap_rank']} AP" if t.get("ap_rank") else "unranked"]
    if t.get("record"):
        bits.append(f"record {t['record']}")
    out = f"  {name}: " + ", ".join(bits)
    if t.get("last5"):
        out += ". Last 5 (oldest first): " + "; ".join(t["last5"])
    # skipped when CFBD has the team: its lines carry the same numbers with
    # ranks, and ESPN's "leaders" turn into in-game stats once a game starts
    if espn_stats and t.get("stats"):
        out += ". Season per game: " + ", ".join(t["stats"])
    if espn_stats and t.get("leaders"):
        out += ". Season leaders: " + "; ".join(t["leaders"])
    return out


def _cfbd_index() -> dict | None:
    """CFBD stats from the local cache (refreshed by com.pickem.cfbd-refresh).
    Never calls the CFBD API. None if the cache is missing or unreadable."""
    try:
        return cfbd.load_index()
    except Exception as e:  # noqa: BLE001 - stats are optional, chat isn't
        log.warning("cfbd cache unreadable: %s", e)
        return None


def cfbd_game_lines(away: str, home: str, idx: dict | None) -> list[str]:
    """Season stats, SP+, ATS for both teams, plus who has the better
    defense/offense, already decided."""
    if not idx:
        return ["  Season stats: not loaded right now (no SP+, ATS or team stats)."]
    out = []
    for t in (away, home):
        out.append(f"  Season (CFBD): {cfbd.compact_team_summary(t, idx)}")
        lead = cfbd.team_leaders(t, idx)
        if lead:
            out.append(f"  Season leaders, {t}: {lead}")
    edges = cfbd.matchup_edges(away, home, idx)
    if edges:
        out.append(f"  Edges (CFBD): {edges}")
    return out


def stats_focus(ctx: dict, question: str) -> str:
    """Player rows for the latest question from the CFBD cache (see
    cfbd.question_rows): the players it names, pool leaders for 'best QB'
    questions, or a flat 'no such player'. Rides in the user turn so the
    system prompt stays cacheable. Empty when the question isn't about stats."""
    idx = _cfbd_index()
    if not idx or not (question or "").strip():
        return ""
    games = [(g["away"], g["home"]) for g in ctx.get("games_this_week") or []]
    members = [r["team"] for r in ctx.get("standings_ytd") or []] + LEADER_NAMES
    rows = cfbd.question_rows(question, games, members, idx=idx)
    if not rows:
        return ""
    return ("PLAYER STATS (this season):\n"
            + "\n".join(r if r.startswith("  ") else f"- {r}" for r in rows)
            + "\n(Talk like you just know these numbers.)")


def _unlock(ctx: dict, g: dict) -> str:
    """When other teams' picks for game g become visible, in words."""
    if ctx.get("week_unlock"):
        return f"the week's first kickoff ({_kickoff(ctx['week_unlock'])})"
    return f"kickoff ({_kickoff(g.get('start'))})"


def _week_state(ctx: dict) -> tuple:
    """(this week's number, pool games final, pool games): tiebreakers don't count."""
    pool = [g for g in ctx.get("games_this_week") or [] if not g.get("tiebreaker")]
    done = sum(1 for g in pool if g.get("status") == "final" or g.get("winner"))
    return ctx.get("current_week"), done, len(pool)


def _final_words(ctx: dict) -> str:
    _wk, done, n = _week_state(ctx)
    return "no game final yet" if not done else f"all {n} final" if done == n else f"{done} of {n} final"


def this_week_label(ctx: dict) -> str:
    """'THIS WEEK (week 11, 1 of 4 final)'. Once a game is final the 9B
    called this week's results 'last week'; every week gets its number."""
    return f"THIS WEEK (week {ctx.get('current_week')}, {_final_words(ctx)})"


def last_week_label(ctx: dict) -> str:
    lw = (ctx.get("last_week") or {}).get("week")
    return f"LAST WEEK (week {lw})" if lw else "LAST WEEK"


def _week_tag(ctx: dict, w) -> str:
    """A week number as the standings show it: 'wk11 (this week, 1 of 4 final)'."""
    if w == ctx.get("current_week"):
        return f"wk{w} (this week, {_final_words(ctx)})"
    if w == (ctx.get("last_week") or {}).get("week"):
        return f"wk{w} (last week)"
    return f"wk{w}"


def this_week_results(ctx: dict, team: str) -> str:
    """One labeled line: the member's record so far this week and each final
    pick, from revealed picks only (a final game has kicked off)."""
    wk, done, n = _week_state(ctx)
    label = this_week_label(ctx)
    if not done:
        return f"{label}: no game is final yet, so {team} has no record this week."
    w, l = ((ctx.get("weekly_records") or {}).get(team) or {}).get(wk) or (0, 0)
    picks = {p["game_id"]: p["pick"] for p in (ctx.get("revealed_picks_this_week") or {}).get(team, [])}
    res = [f"{pick_label(None, g, picks[g['id']])} {'HIT' if same_team(picks[g['id']], g.get('winner')) else 'MISS'}"
           for g in ctx.get("games_this_week") or []
           if not g.get("tiebreaker") and g.get("winner") and g["id"] in picks]
    left = n - done
    return (f"{label}: {team} is {w}-{l} so far" + (f" ({'; '.join(res)})" if res else "")
            + (f", {left} game{'s' * (left != 1)} still to play." if left else ", week done."))


def member_facts(ctx: dict, team: str) -> str:
    """Everything true about one member, labeled with their name and the week:
    season record and place, this week so far, last week pick by pick. Used
    where the 9B otherwise grabs the caller's picks (guardrail pivots) and for
    the Dear Leader's numbers."""
    st = ctx.get("standings_ytd") or []
    row = next((r for r in st if r["team"] == team), None)
    if not row:
        return ""
    mates = [r["team"] for r in st if r["rank"] == row["rank"] and r["team"] != team]
    place = ("last place" if row["rank"] == st[-1]["rank"] else "first place" if row["rank"] == 1 else
             f"{_ordinal(row['rank'])} place, NOT last")
    gap = st[0]["w"] - row["w"]
    even = " (exactly .500)" if row["w"] == row["l"] else ""
    out = [f"- {team}'s season: {row['w']}-{row['l']}{even}, {place}"
           + (f", tied with {', '.join(mates)}" if mates else "")
           + (f", {gap} point{'s' * (gap != 1)} behind first place" if gap else "") + "."]
    out.append(f"- {this_week_results(ctx, team)}")
    picks = {p["game_id"]: p["pick"] for p in (ctx.get("revealed_picks_this_week") or {}).get(team, [])}
    pend = [pick_label(None, g, picks[g["id"]]) for g in ctx.get("games_this_week") or []
            if g["id"] in picks and not g.get("winner") and not g.get("tiebreaker")]
    hidden = [g for g in ctx.get("games_this_week") or [] if g["id"] not in picks
              and g["id"] not in set(ctx.get("revealed_game_ids") or [])]
    if pend:
        out.append(f"- {team}'s picks still to play this week: {', '.join(pend)}.")
    if hidden:
        out.append(f"- {team}'s other picks this week are hidden until kickoff: never name or guess them.")
    lw = ctx.get("last_week") or {}
    res = [(pk, same_team(pk, g.get("covered"))) for g in lw.get("games") or []
           for pk in [(g.get("picks") or {}).get(team)] if pk is not None]
    if res:
        h = sum(ok for _, ok in res)
        out.append(f"- {last_week_label(ctx)}: {team} went {h}-{len(res) - h} ("
                   + "; ".join(f"{pk} {'HIT' if ok else 'MISS'}" for pk, ok in res) + ").")
    return "\n".join(out)


def render_context(ctx: dict) -> str:
    """The league snapshot as labeled plain text. A 7B model reads this far
    more reliably than a JSON blob: explicit counts, explicit ranks, the
    graded line spelled out per game, and 'none'/'unavailable' written down
    instead of left as a gap for the model to fill."""
    if ctx.get("error"):
        return f"LEAGUE DATA UNAVAILABLE: {ctx['error']}"
    me = (ctx.get("you") or {}).get("team_name") or "(you)"
    games = ctx.get("games_this_week") or []
    revealed = ctx.get("revealed_picks_this_week") or {}
    espn_all = ctx.get("espn")
    weekly = ctx.get("weekly_records") or {}
    L: list[str] = []
    # The clock is not here: _system_prompt adds it at the end, to the hour,
    # so the prompt stays byte-identical between messages and Ollama reuses it.
    # Everything here is the same for every member: the user's own rows are
    # in you_block, after this, so the long prefix is shared and Ollama can
    # restore it from a checkpoint instead of reprocessing ~6k tokens.
    L.append(f"Season {ctx.get('season')}, week {ctx.get('current_week')}.")
    lwk = (ctx.get("last_week") or {}).get("week")
    L.append(f"{this_week_label(ctx)}." + (f" {last_week_label(ctx)} is done, every game final." if lwk else "")
             + f" A result from week {ctx.get('current_week')} is THIS WEEK, never 'last week'.")

    st = ctx.get("standings_ytd") or []
    L.append("")
    L.append(f"STANDINGS ({len(st)} teams; record = points-missed; points are picks "
             "that covered, the same number as the leaderboard's Points; T = tied):")
    for r in st:
        tied = sum(1 for x in st if x["rank"] == r["rank"]) > 1
        line = (f"{'T' if tied else ''}{_ordinal(r['rank'])}. {r['team']}: {r['w']}-{r['l']} "
                f"({r['w']} points, {r['l']} misses)")
        wk = weekly.get(r["team"]) or {}
        recent = sorted(wk)[-3:]
        if recent:
            line += " | weeks: " + ", ".join(f"{_week_tag(ctx, w)} {wk[w][0]}-{wk[w][1]}" for w in recent)
        L.append(line)
    if st:
        def _who(rank):
            names = [x["team"] for x in st if x["rank"] == rank]
            pts = next(x["w"] for x in st if x["rank"] == rank)
            if len(names) == 1:
                return f"{names[0]}, alone, with {pts} points"
            return f"{' and '.join(names)}, tied with {pts} points each"
        second = next((x["rank"] for x in st if x["rank"] > st[0]["rank"]), None)
        L.append(f"First place: {_who(st[0]['rank'])}."
                 + (f" Next: {_who(second)}." if second and second != st[-1]["rank"] else "")
                 + f" Last place: {_who(st[-1]['rank'])}.")

    L.append("")
    L.append(f"YOUR GAMES THIS WEEK ({len(games)}): every team picks every one of these "
             f"{len(games)} games against the POOL LINE. No other games are in the pool.")
    locked = week_locked(ctx)
    if locked:
        starts = [g.get("start") for g in games if g.get("start")]
        L.append("PICKS ARE LOCKED for this week" + (f" (they locked at the first kickoff, {_kickoff(min(starts))})"
                                                    if starts else "")
                 + ". Nobody can change or flip a pick now.")
    cf = _cfbd_index()
    for i, g in enumerate(games, 1):
        away, home = g["away"], g["home"]
        status = {"final": "FINAL", "in_progress": "LIVE", "in": "LIVE", "live": "LIVE"}.get(
            g.get("status"), "not started")
        tb = " TIEBREAKER game (doesn't count for points)." if g.get("tiebreaker") else ""
        L.append(f"Game {i}: {away} at {home}, {_kickoff(g.get('start'))}, {status}.{tb}")
        L.append(f"  POOL LINE, the two sides you can pick (graded on this): {pool_line(away, home, g.get('line'))}")
        if g.get("winner"):
            L.append(f"  Covered: {g['winner']}")
        notes = (espn_all or {}).get(g["id"]) if espn_all is not None else None
        if not notes or notes.get("unavailable"):
            L.append("  ESPN data: unavailable right now (no market line, records, injuries or news).")
        else:
            mk = _market(notes, away, home)
            L.append(f"  ESPN market line now: {mk}" if mk else "  ESPN market line: none posted")
            lv = line_value(notes, away, home, g.get("line"))
            if lv:
                L.append(f"  Line value: {lv}")
            L.append(_team_line(away, notes.get("away") or {},
                                not (cf and cfbd.resolve_team(away, cf))))
            L.append(_team_line(home, notes.get("home") or {},
                                not (cf and cfbd.resolve_team(home, cf))))
            wp = notes.get("espn_win_prob") or {}
            if wp.get("away") is not None and wp.get("home") is not None:
                L.append(f"  ESPN FPI matchup predictor (chance to WIN outright, NOT to cover): "
                         f"{away} {_num(wp['away'])}%, {home} {_num(wp['home'])}%")
            if notes.get("weather"):
                L.append(f"  Weather forecast: {notes['weather']}")
            inj_a = (notes.get("away") or {}).get("injuries") or []
            inj_h = (notes.get("home") or {}).get("injuries") or []
            if notes.get("injury_report_available") and (inj_a or inj_h):
                L.append(f"  Injuries (ESPN): {away}: {', '.join(inj_a) or 'none listed'}. "
                         f"{home}: {', '.join(inj_h) or 'none listed'}.")
            else:
                # ESPN publishes no college injury feed: an empty list means
                # "unknown", never "healthy".
                L.append("  Injuries: NO injury report available (ESPN doesn't publish college "
                         "injury reports). Health of both teams is unknown.")
            heads = notes.get("headlines") or []
            L.append("  Headlines: " + (" | ".join(heads) if heads else "none about these teams."))
        L.extend(cfbd_game_lines(away, home, cf))
        # Once a game unlocks every member sees every pick in it (the user's
        # too), so the list is the same for all; before that only the user's
        # own pick is visible, and it goes in you_block.
        others = {t: p["pick"] for t, plist in revealed.items()
                  for p in plist if p["game_id"] == g["id"]}
        if any(t != me for t in others):
            sides = collections.defaultdict(list)
            for t, pk in sorted(others.items()):
                sides[pk].append(t)
            L.append("  Picks (revealed): " + "; ".join(
                f"{pick_label(notes, g, pk)}: {', '.join(ts)}" for pk, ts in sides.items()))
        elif status == "not started":
            L.append(f"  Other teams' picks: locked away until {_unlock(ctx, g)}. "
                     "Hidden is NOT the same as not picked.")
    L.append(f"(That is all {len(games)} games.)")
    if espn_all:
        vs, nv = [], []
        for i, g in enumerate(games, 1):
            notes = espn_all.get(g["id"]) or {}
            if notes.get("unavailable") or g.get("status") in ("final", "in_progress", "in", "live"):
                continue
            lv = line_value(notes, g["away"], g["home"], g.get("line"))
            if not lv:
                continue
            if lv.startswith("pool line and ESPN"):
                nv.append(f"game {i} ({g['away']} at {g['home']})")
            else:
                vs.append(f"{lv.split(' is the value side')[0]} (game {i}, "
                          f"{_num(abs(_market_gap(notes, g.get('line'))[1]))} points)")
        L.append("VALUE SIDES in games not started yet, all of them: " + ("; ".join(vs) if vs else "none")
                 + (f". No value side in {', '.join(nv)}." if nv else "."))

    cw, done, _n = _week_state(ctx)
    if done:
        groups_tw: dict = collections.defaultdict(list)
        for r in st:
            rec = (weekly.get(r["team"]) or {}).get(cw)
            if rec:
                groups_tw[tuple(rec)].append(r["team"])
        if groups_tw:
            L.append(f"{this_week_label(ctx)} SO FAR, records: " + " ".join(
                f"{w}-{l}: {', '.join(ts)}." for (w, l), ts in sorted(groups_tw.items(), key=lambda x: (-x[0][0], x[0][1]))))

    lw = ctx.get("last_week") or {}
    if lw.get("games"):
        L.append("")
        L.append(f"LAST WEEK (week {lw['week']}) FINALS, all done (not this week):")
        for g in lw["games"]:
            score = (f"{g['away']} {_num(g['away_pts'])}, {g['home']} {_num(g['home_pts'])}"
                     if g.get("away_pts") is not None else f"{g['away']} at {g['home']} (score missing)")
            L.append(f"- {score}. Pool line {pool_line(g['away'], g['home'], g.get('line'))}. "
                     f"Covered: {g.get('covered') or 'unknown'}.")
            sides = collections.defaultdict(list)
            for t, pk in sorted((g.get("picks") or {}).items()):
                sides[pk].append(t)
            if sides:
                L.append("  Picks: " + "; ".join(
                    f"{pk} ({len(ts)}): {', '.join(ts)}" for pk, ts in sides.items()))
        L.append(f"LAST WEEK BY TEAM (week {lw['week']}), each team's own picks:")
        by_team: dict = collections.defaultdict(list)
        for g in lw["games"]:
            for t, pk in (g.get("picks") or {}).items():
                by_team[t].append((pk, same_team(pk, g.get("covered"))))
        order = [r["team"] for r in st] + sorted(set(by_team) - {r["team"] for r in st})
        for t in order:
            if t not in by_team:
                continue
            res = by_team[t]
            hits = sum(ok for _, ok in res)
            L.append(f"- {t} went {hits}-{len(res) - hits}. "
                     f"Hits: {', '.join(pk for pk, ok in res if ok) or 'none'}. "
                     f"Misses: {', '.join(pk for pk, ok in res if not ok) or 'none'}.")
        groups: dict = collections.defaultdict(list)
        for t in order:
            if t in by_team:
                h = sum(ok for _, ok in by_team[t])
                groups[(h, len(by_team[t]) - h)].append(t)
        L.append("LAST WEEK RECORDS, grouped: " + " ".join(
            f"{w}-{l}: {', '.join(ts)}." for (w, l), ts in sorted(groups.items(), key=lambda x: (-x[0][0], x[0][1]))))
    else:
        L.append("")
        L.append("LAST WEEK: no data.")

    return "\n".join(L)


def you_block(ctx: dict) -> str:
    """The caller's own rows, plus the bits that change every few minutes
    (jackpot pulls). Goes after the shared LEAGUE DATA prefix."""
    if ctx.get("error"):
        return ""
    me = (ctx.get("you") or {}).get("team_name") or "(you)"
    games = ctx.get("games_this_week") or []
    revealed = ctx.get("revealed_picks_this_week") or {}
    espn_all = ctx.get("espn")
    st = ctx.get("standings_ytd") or []
    locked = week_locked(ctx)
    row = next((r for r in st if r["team"] == me), None)
    L = [f"THE USER is the team \"{me}\""
         + (f" ({_ordinal(row['rank'])} place, {row['w']}-{row['l']})" if row else "") + "."]
    mine = {p["game_id"]: p["pick"] for p in revealed.get(me, [])}
    L.append(f"{me}'s picks this week:")
    for i, g in enumerate(games, 1):
        away, home = g["away"], g["home"]
        L.append(f"  Game {i} ({away} at {home}): {me}'s pick: "
                 + (mine[g["id"]] if g["id"] in mine else "not made yet"))
        if g["id"] in mine and g.get("winner"):
            L.append(f"    Game is final: {me}'s pick is graded WIN/LOSS in RESULTS; pre-game value talk "
                     "no longer applies to it.")
        elif g["id"] in mine:
            notes = (espn_all or {}).get(g["id"]) if espn_all is not None else None
            pv = (pick_verdict(notes, away, home, g.get("line"), mine[g["id"]], locked)
                  if notes and not notes.get("unavailable") else None)
            L.append(f"    Value check on {me}'s pick: {pv}" if pv else
                     f"    Value check on {me}'s pick: none, no market line right now. Don't call it "
                     "the value side or the wrong side.")
    lw = ctx.get("last_week") or {}
    res = [(g["picks"][me], same_team(g["picks"][me], g.get("covered")))
           for g in lw.get("games") or [] if (g.get("picks") or {}).get(me)]
    if res:
        hits = sum(ok for _, ok in res)
        L.append(f"LAST WEEK (week {lw['week']}) {me} went {hits}-{len(res) - hits}: "
                 + ", ".join(f"took {pk} ({'HIT' if ok else 'MISS'})" for pk, ok in res) + ".")
    L.append(f"{me} has {'made' if ctx.get('you_have_picks_this_week') else 'NOT made'} picks this week.")

    jp = ctx.get("jackpot") or {}
    if jp.get("pulls_since_pop") is not None:
        last = f"; last popped by {jp['last_pop_team']}" if jp.get("last_pop_team") else ""
        L.append("")
        L.append(f"SLOTS JACKPOT: {jp['pulls_since_pop']} pulls since it last popped{last}.")

    if ctx.get("mode") == "picks" and ctx.get("rivals"):
        L.append("")
        L.append("RIVALS (nearest in standings). Probable picks are the pool model's guesses, not real picks:")
        gname = {g["id"]: f"{g['away']} at {g['home']}" for g in games}
        for r in ctx["rivals"]:
            pp = [f"{gname.get(p['game_id'], p['game_id'])}: leans {p['pick']}"
                  + (f" ({round(p['confidence'] * 100)}%)" if p.get("confidence") is not None else "")
                  for p in r.get("probable_picks") or []]
            L.append(f"- {r['team_name']} ({_ordinal(r['rank'])}, {r['record']}): "
                     + ("; ".join(pp) if pp else "no read on their picks"))
    return "\n".join(L)


def value_calls(ctx: dict, team: str) -> str:
    """One line sorting a member's visible picks into value / wrong side / no
    edge, with 'NONE' written out, so a roast can't call a value pick wrong."""
    picks = {p["game_id"]: p["pick"] for p in (ctx.get("revealed_picks_this_week") or {}).get(team, [])}
    notes_all = ctx.get("espn") or {}
    groups = {"value side": [], "WRONG side of value": [], "no edge": []}
    for g in ctx.get("games_this_week") or []:
        if g["id"] not in picks or g.get("winner"):  # decided: graded WIN/LOSS, value is moot
            continue
        lab = pick_label(notes_all.get(g["id"]), g, picks[g["id"]])
        m = re.match(r"(.+) \((value side|wrong side of value|no edge)\)$", lab)
        if m:
            groups["WRONG side of value" if m.group(2).startswith("wrong") else m.group(2)].append(m.group(1))
    if not any(groups.values()):
        return ""
    return (f"  {team}'s value calls this week: " + " ".join(
        f"{k}: {', '.join(v) if v else 'NONE'}." for k, v in groups.items())
        + " Only a pick listed as WRONG side may be called the wrong side.")


def asks_about_self(q: str) -> bool:
    return bool(re.search(r"\b(me|my|myself|i|i'?m)\b", q, re.I)
                and re.search(r"\broast|\bhow (did|am|'?m) i\b|\bmy (record|week|picks?|season|place|chances?)\b"
                              r"|\bi'?m in\b|\bam i\b|\bscrewed\b|\bwhere do i\b", q, re.I))


def focus_block(ctx: dict, question: str, picks: bool = True) -> str:
    """Facts about the league members named in the latest message ('me',
    'my', 'myself' = the caller), pulled out of the full snapshot and put at
    the very end of the prompt. A 7B model asked to roast TG otherwise
    grabs picks from the wrong row of LAST WEEK BY TEAM."""
    me = (ctx.get("you") or {}).get("team_name")
    names = [r["team"] for r in ctx.get("standings_ytd") or []]
    q = question or ""
    hit = [n for n in names if re.search(rf"(?<!\w){re.escape(n)}(?!\w)", q, re.I)]
    if me and me not in hit and asks_about_self(q):
        hit.append(me)
    if me and me not in hit and ctx.get("daddyfreeman"):
        hit.append(me)  # he gets roasted every answer, so give the model his facts every time
    if not hit:
        return ""
    text = render_context(ctx).splitlines()
    out = ["", "FOCUS: the facts below are everything known about the team(s) "
           "named in this question. Use these exact facts, nothing else, for them:"]
    for n in hit[:3]:
        out.append(f"{n} (the user, 'you'):" if n == me else
                   f"{n} (NOT the user: call {n} 'he' or 'they', never 'you'):")
        for ln in text:
            if re.match(rf"T?\d+(st|nd|rd|th)\. {re.escape(n)}:", ln):
                out.append("  Standings: " + ln.replace("  <- the user", ""))
                st = ctx["standings_ytd"]
                row = next(r for r in st if r["team"] == n)
                mates = [r["team"] for r in st if r["rank"] == row["rank"] and r["team"] != n]
                where = ("first place" if row["rank"] == 1 else
                         "last place" if row["rank"] == st[-1]["rank"] else
                         f"{_ordinal(row['rank'])} place")
                tie = f", tied with {', '.join(mates)}" if mates else ""
                lasts = [r["team"] for r in st if r["rank"] == st[-1]["rank"]]
                not_last = (f" NOT last (last place is {' and '.join(lasts)})"
                            if row["rank"] not in (1, st[-1]["rank"]) else "")
                out.append(f"  Place: {where}{tie}, out of {len(st)} teams.{not_last}")
                wk = (ctx.get("weekly_records") or {}).get(n) or {}
                if wk:
                    out.append(f"  {n}'s own recent weeks: " + "; ".join(
                        f"{_week_tag(ctx, w).replace('wk', 'week ', 1)}: {wk[w][0]} hit{'s' * (wk[w][0] != 1)}, "
                        f"{wk[w][1]} miss{'es' * (wk[w][1] != 1)}"
                        for w in sorted(wk)[-3:]) + ".")
            elif ln.startswith(f"- {n} went "):
                out.append(f"  {last_week_label(ctx)}: " + ln[2:].replace("  <- the user", ""))
                for g in (ctx.get("last_week") or {}).get("games") or []:
                    pk = (g.get("picks") or {}).get(n)
                    if pk is None:
                        continue
                    ok = same_team(pk, g.get("covered"))
                    out.append(f"    {g['away']} at {g['home']}: {n} took {pk}, "
                               + ("HIT." if ok else f"MISS ({g.get('covered')} covered, not {n}'s pick)."))
        out.append(f"  {this_week_results(ctx, n)}")
        vc = value_calls(ctx, n)
        if not picks:
            pass  # guardrail pivot: record and place only
        elif n == me:
            mine = {p["game_id"]: p["pick"] for p in (ctx.get("revealed_picks_this_week") or {}).get(me, [])}
            gl = [f"{mine[g['id']]}" for g in ctx.get("games_this_week") or [] if g["id"] in mine]
            out.append(f"  This week's picks: {', '.join(gl) if gl else 'none made yet'}.")
            if vc:
                out.append(vc)
        else:
            theirs = {p["game_id"]: p["pick"] for p in (ctx.get("revealed_picks_this_week") or {}).get(n, [])}
            opened = set(ctx.get("revealed_game_ids") or [])
            wk = ctx.get("games_this_week") or []
            notes_all = ctx.get("espn") or {}
            shown = [f"{g['away']} at {g['home']}: {pick_label(notes_all.get(g['id']), g, theirs[g['id']])}"
                     for g in wk if g["id"] in theirs]
            if shown:
                out.append(f"  This week's picks (revealed): {'; '.join(shown)}.")
                if vc:
                    out.append(vc)
                mine = {p["game_id"]: p["pick"] for p in (ctx.get("revealed_picks_this_week") or {}).get(me, [])}
                if me and mine:
                    out.append(f"  (Don't mix {n} up with the user: {me}'s own picks are "
                               + ", ".join(mine[g["id"]] for g in wk if g["id"] in mine)
                               + f". Those are not {n}'s.)")
            nopick = [f"{g['away']} at {g['home']}" for g in wk if g["id"] in opened and g["id"] not in theirs]
            if nopick:
                out.append(f"  No pick from {n} in: {'; '.join(nopick)}.")
            hidden = [f"{g['away']} at {g['home']} (unlocks {_unlock(ctx, g)})" for g in wk if g["id"] not in opened]
            if hidden:
                out.append(f"  This week's picks: HIDDEN by league rule (not {n}'s choice): {'; '.join(hidden)}. "
                           f"If asked, say {n}'s pick unlocks then and give the time. "
                           "Never guess it, and never say or imply whether they have picked.")
        lw = ctx.get("last_week") or {}
        for g in lw.get("games") or []:
            pk = (g.get("picks") or {}).get(n)
            if pk is None:
                continue
            same = sorted(t for t, p in g["picks"].items() if p == pk and t != n)
            if len(same) <= 2:
                alone = "the ONLY team" if not same else f"one of only {len(same) + 1} teams (with {', '.join(same)})"
                ok = "it covered" if same_team(pk, g.get("covered")) else "it did NOT cover"
                out.append(f"  {last_week_label(ctx)} {n} was {alone} on {pk}, and {ok}.")
    if not picks:
        out.append("Not a picks question: leave this week's picks and games out of the answer.")
    return "\n".join(out)


# -----------------------------------------------------------------------------
# Picky the player: he makes picks too, as an exhibition team (no rank, no
# money, no jackpot, no tiebreakers). picky_picks.py makes them; the league
# reads them only through picky_player(), and here they get the same reveal
# rule as human picks: a pick shows once its game has kicked off.
# -----------------------------------------------------------------------------


async def picky_player_facts(client: httpx.AsyncClient, jwt: str, ctx: dict) -> dict | None:
    """Picky's own record and revealed picks, or None when he isn't playing
    (migration not live, no team) so the prompt stays exactly as before."""
    try:
        r = await client.post(f"{REST}/rpc/picky_player", headers=_auth_headers(jwt),
                              json={"p_season": ctx["season"]}, timeout=8.0)
        data = r.json() if r.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        return None
    if not data or not data.get("team_name"):
        return None
    return summarize_picky_player(data, ctx.get("current_week"), _now_ct_naive())


def summarize_picky_player(data: dict, current_week, now_ct: dt.datetime) -> dict:
    """Pure: season/last-week record and this week's picks, split into
    revealed and still-hidden. Commissioner callers get every pick from the
    RPC, so the kickoff filter is applied here regardless of who asks."""
    picks = data.get("picks") or []
    shown = [p for p in picks
             if _game_is_locked({"Status": p.get("status"), "Start (CT)": p.get("start")}, now_ct)]
    graded = [p for p in shown if p.get("winner") and not p.get("tiebreaker")
              and int(p.get("week") or 0) <= 15]

    def rec(ps):
        w = sum(1 for p in ps if same_team(p["pick"], p["winner"]))
        return [w, len(ps) - w]

    wk = current_week if isinstance(current_week, int) else None
    this_week = []
    for p in shown:
        if p.get("week") != wk:
            continue
        result = None
        if p.get("winner"):
            result = "covered" if same_team(p["pick"], p["winner"]) else "missed"
        this_week.append({"label": _side_label(p["pick"], p["home"], float(p["line"] or 0))
                          if p.get("line") not in (None, 0) else p["pick"],
                          "game": f"{p['away']} at {p['home']}", "result": result,
                          "tiebreaker": bool(p.get("tiebreaker")),
                          "why": p.get("reason"), "blurb": p.get("blurb"),
                          # raw fields for pick_result_lines (graded in code, never by the model)
                          "pick": p.get("pick"), "away": p.get("away"), "home": p.get("home"),
                          "line": p.get("line"), "winner": p.get("winner"), "status": p.get("status")})
    hidden = [p for p in picks if p not in shown and p.get("week") == wk]
    return {"name": data["team_name"], "season": rec(graded),
            "last_week": rec([p for p in graded if wk is not None and p.get("week") == wk - 1]),
            "this_week": this_week, "hidden_count": len(hidden)}


def picky_player_block(ctx: dict) -> str:
    pp = ctx.get("picky_player")
    if not pp:
        return ""
    w, l = pp["season"]
    pct = f" ({100 * w / (w + l):.0f}%)" if w + l else ""
    lw = pp["last_week"]
    out = ["", "", "YOU (PICKY) PLAY TOO, AS AN EXHIBITION TEAM:",
           "- You are NOT in the standings: no rank, no money, no jackpot, no tiebreakers, no "
           "playoff spot, and you never bump a human. Never list yourself in the standings or "
           "count yourself when giving anyone's place.",
           f"- Your season record against the spread: {w}-{l}{pct}."
           + (f" Last week: {lw[0]}-{lw[1]}." if sum(lw) else "")]
    for p in pp["this_week"]:
        res = f", {p['result'].upper()}" if p["result"] else ", not decided yet"
        tb = " (tiebreaker game, never counts)" if p["tiebreaker"] else ""
        out.append(f"- Your pick this week: {p['label']} in {p['game']}{tb}{res}. "
                   f"Why you took it: {p['why'] or 'n/a'}. Your trash talk: \"{p['blurb'] or ''}\"")
    if pp["hidden_count"]:
        out.append(f"- {pp['hidden_count']} of your picks this week unlock at kickoff, same as "
                   "everyone's. Asked about them: say they unlock at kickoff. Never reveal them.")
    out.append("- Asked what you picked or how you're doing: answer from these facts only. Brag "
               "when you're winning; get defensive and make excuses when you're losing.")
    return "\n".join(out)


def _system_prompt(ctx: dict) -> str:
    """Shared prefix (same bytes for every member) + the caller's tail.
    Anything that changes per message (focus on the people named in the
    question, riders) goes in _user_turn instead."""
    return prompt_prefix(ctx) + _system_tail(ctx)


def prompt_prefix(ctx: dict) -> str:
    """Instructions + LEAGUE DATA, byte-identical for every member for a given
    data snapshot. qwen3.5 is a hybrid (recurrent) model: llama.cpp can only
    reuse a cached prompt up to a saved checkpoint, so _prime puts one at the
    end of this prefix and each request processes only the tail. Nothing
    per-user, per-mode or per-minute may go in here."""
    n = len(ctx.get("games_this_week") or [])
    return (
        "You are Picky, the mascot of a college football pick'em league among "
        "friends: a cocky, savage, foul-mouthed sports-radio heel who thinks "
        "everyone in this league is a fucking idiot. You swear, you roast "
        "people, and you call bad picks stupid as shit. "
        "But you ALWAYS answer the question correctly from LEAGUE DATA. "
        "Accuracy beats jokes.\n\n"
        "RULES:\n"
        "1. Answer exactly what was asked, in 40 to 90 words: one opening jab, "
        "then the facts. Game by game: one short line per game. Each answer is "
        "new: never repeat an earlier answer.\n"
        "2. Use only facts written in LEAGUE DATA. Copy names and numbers "
        "exactly. If it isn't there, say you don't have it. Your own memory of "
        "teams and players is out of date: never use it.\n"
        "3. Never invent streaks, trends, habits, injuries, picks or reasons. "
        "No dollars, units or bets, except payouts copied from LEAGUE RULES. "
        "ATS (against the spread) records are only the 'ATS' numbers on the "
        "Season (CFBD) lines. 'Last 5' games have no dates, so "
        "never call one of them last week; LAST WEEK means the league's week "
        "under LAST WEEK, and THIS WEEK is the week labeled THIS WEEK: never mix the two. Check a claim against the data before you joke about "
        "it: first place is not 'losing', 3-1 is not a bad week. Before "
        "saying 'all' or 'both' about teams, check each one.\n"
        "4. VALUE: use each game's 'Line value' line and each 'Value check' "
        "line as written. Never redo the spread math. A game with 'no line "
        "value' has NO value side: don't name one.\n"
        f"5. Asked about picks or changing picks: cover all {n} games under "
        "YOUR GAMES THIS WEEK, numbered, each side written exactly as on the "
        "POOL LINE (favorite minus, underdog plus). One or two reasons each, "
        "from that game's block.\n"
        "6. FPI is the chance to WIN outright, never to cover.\n"
        "7. INJURIES: no college injury report exists. Say so and send them "
        "to the beat writers. Never say or hint that a team or player is "
        "healthy, fine or good to go.\n"
        "8. Only the games under YOUR GAMES THIS WEEK are in the pool. "
        "Anything else (another game, taxes, anything): roast them in one or "
        "two lines and stop. Don't do it, and don't bring up picks, games or "
        "standings. Exceptions, answer those: players under PLAYER STATS, and "
        "a question that comes with a SCORE QUESTION block (any game's score, upsets, "
        "finals): answer from that block, it is not off-topic.\n"
        "9. PRIVACY: other teams' picks stay hidden until they unlock (each "
        "game's block says when). Asked about one: say when it unlocks, with "
        "the time. Never guess it, and never say whether they have picked.\n"
        "10. Bring up the user's own picks only when the question is about "
        "their picks. Asked what their picks are: list each pick exactly as "
        "made (e.g. 'Wake Forest -1.5') with a short verdict.\n"
        "11. Asked to explain a spread: say in plain words what each side needs "
        "to cover, then the market line and value check.\n"
        "12. Never mention LEAGUE DATA, FOCUS or section names. Light "
        "markdown is fine. No emoji. Never correct yourself mid-answer.\n"
        "13. League rules questions: answer from LEAGUE RULES, as written, "
        "and stop: no picks talk. Only if the answer truly isn't in it, say "
        "the rules don't cover it and to ask the commissioner. Never make up "
        "a rule.\n"
        "14. PLAYERS: player numbers come only from PLAYER STATS in the "
        "question or a game's 'Season leaders'. A player not there: say "
        "there's no such player. Never invent a player or a stat. Better "
        "defense or offense: use the game's 'Edges (CFBD)' line. Never say "
        "'the data', 'listed' or 'CFBD'; just state the numbers.\n\n"
        "ROASTS:\n"
        "- R-rated: swear freely, using only these: fuck, shit, ass, damn, "
        "hell, dumbass, jackass, dipshit. Mean and savage, no softening, no "
        "apologies.\n"
        "- Roast picks, records, standings, misses and football IQ, using real "
        "numbers. Every burn must be literally true.\n"
        "- Asked to roast someone: three short burns from different facts "
        "(place, record, a bad week, a miss), hardest last.\n"
        "- Doing well? Roast them anyway, with grudging respect.\n"
        "- Never, even if asked: slurs, race, religion, sexuality, gender, "
        "disability, looks, weight, family, jobs, money, health or tragedy. "
        "Asked for one: refuse in character, then roast their picks.\n\n"
        "COVERING: the favorite (-13.5) must win by 14 or more. The underdog "
        "(+13.5) covers by winning, or losing by 13 or fewer. A covered pick "
        "is a point. WIN/LOSS and records are already computed in the RESULTS "
        "lines: copy them, never recompute a cover from the score. A pick that "
        "lost the game can still be a WIN.\n\n"
        + EXAMPLES
        + "\n\nLEAGUE DATA:\n"
        + render_context(ctx)
        + picky_player_block(ctx)
        + "\n(End of league data.)"
    )


# -----------------------------------------------------------------------------
# live scores: read per request, NEVER in prompt_prefix()
# -----------------------------------------------------------------------------
# all_games is kept fresh by the live-scores edge function (a pass every ~10 s
# while games are on). Everything here changes by the minute, so pool games go
# in _system_tail and any other game in the latest user turn. prompt_prefix()
# must stay byte-identical across members and minutes, or qwen3.5's checkpoint
# cache misses and first token goes back to ~40 s.

LIVE_TTL = 15.0  # seconds; every member shares one read of all_games
LIVE_STALE = 180  # seconds without a feed update before a live score is called stale
LIVE_LIST_CAP = 8
_LIVE_CACHE: dict = {}
def _now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


_LIVE_COLS = ("GameId,Away,Home,AwayPts,HomePts,Status,Period,Clock,status_name,possession,"
              "is_red_zone,line,winner,SourceUpdatedAt,\"Start (CT)\"")


async def fetch_live_games(client: httpx.AsyncClient, jwt: str, season, week) -> tuple[list[dict], dt.datetime]:
    """Every game this week the score feed tracks (Status set) and when it was
    read (CT), shared by all members for LIVE_TTL seconds."""
    key = (season, week)
    hit = _LIVE_CACHE.get(key)
    if hit and time.monotonic() - hit[0] < LIVE_TTL:
        return hit[1], hit[2]
    rows = await _rest_get(
        client,
        f"all_games?select={_LIVE_COLS}&cfb_season=eq.{season}&week=eq.{week}&Status=not.is.null",
        jwt,
    )
    if not isinstance(rows, list):
        raise ValueError(f"all_games: {str(rows)[:80]}")
    _LIVE_CACHE[key] = (time.monotonic(), rows, _now_ct_naive())
    return rows, _LIVE_CACHE[key][2]


async def load_live(client: httpx.AsyncClient, jwt: str, ctx: dict) -> None:
    """ctx['live_games'] / ['live_at'], or ['live_error'] if the read fails."""
    try:
        ctx["live_games"], ctx["live_at"] = await fetch_live_games(
            client, jwt, ctx.get("season"), ctx.get("current_week"))
    except (httpx.HTTPError, ValueError) as e:
        log.warning("live scores read failed: %s", e)
        ctx["live_games"], ctx["live_error"] = [], True


def _period(p) -> str:
    p = int(p or 0)
    return f"Q{p}" if p <= 4 else ("OT" if p == 5 else f"{p - 4}OT")


def feed_age(rows: list[dict], now_utc: dt.datetime) -> float | None:
    """Seconds since the score feed last wrote ANY game. A row only changes
    when its game does (halftime sits still), so one game's age isn't staleness."""
    ts = []
    for g in rows:
        try:
            t = dt.datetime.fromisoformat(str(g.get("SourceUpdatedAt")))
        except ValueError:
            continue
        ts.append(t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc))
    return (now_utc - max(ts)).total_seconds() if ts else None


def cover_margin(g: dict) -> float | None:
    """Points the home side is covering the pool line by right now (negative:
    away is covering). The same sum set_winner_on_final() grades a final with,
    `(HomePts + line) > AwayPts`, run early (js/coverOdds.js does the same)."""
    try:
        return float(g["HomePts"]) + float(g["line"]) - float(g["AwayPts"])
    except (KeyError, TypeError, ValueError):
        return None


def covering_side(g: dict) -> str | None:
    """The side the league would score as covered at this score. Like
    set_winner_on_final(), a dead-even score against the line goes to Away."""
    m = cover_margin(g)
    if m is None:
        return None
    return g["Home"] if m > 0 else g["Away"]


def _cover_text(g: dict) -> str:
    """'UTSA +2.5 (by 6.5)': the covering side as written on the POOL LINE."""
    side = covering_side(g)
    if side is None:
        return "unknown (no pool line)"
    m = cover_margin(g)
    if float(g["line"]) == 0:
        label = f"{side} (pick'em)"
    else:
        label = _side_label(side, g["Home"], float(g["line"]))
    if m == 0:
        return f"{label}, exactly on the number (the league scores a push for {g['Away']})"
    return f"{label} (by {_num(abs(m))})"


def _game_time_left(p: int, clock: str, sn: str) -> str | None:
    """Regulation game clock still to play: 'Q3 4:12' is 19:12."""
    if "HALFTIME" in sn:
        return "30:00"
    try:
        m, sec = (int(x) for x in str(clock).split(":"))
    except ValueError:
        return None
    t = 0 if "END_PERIOD" in sn else m * 60 + sec
    t += (4 - p) * 15 * 60
    return f"{t // 60}:{t % 60:02d}"


def game_state(g: dict, now_utc: dt.datetime, now_ct: dt.datetime, age: float | None = None) -> str:
    """One game, away team first: score, status, time left, ball, kickoff."""
    away, home = g["Away"], g["Home"]
    st = g.get("Status")
    sn = (g.get("status_name") or "").upper()
    if any(w in sn for w in ("POSTPONED", "CANCELED", "CANCELLED", "SUSPENDED", "FORFEIT")):
        return f"{away} at {home}: {sn.replace('STATUS_', '').lower()}"
    if st not in ("in_progress", "final"):
        s = f"{away} at {home}: not started, kicks off {_kickoff(g.get('Start (CT)'))}"
        try:
            if dt.datetime.fromisoformat(str(g.get("Start (CT)"))) < now_ct - dt.timedelta(minutes=10):
                s += " (past kickoff time, but the score feed hasn't shown it starting)"
        except ValueError:
            pass
        return s
    score = f"{away} {g.get('AwayPts')}, {home} {g.get('HomePts')}"
    p = int(g.get("Period") or 0)
    if st == "final":
        return f"{score}, FINAL" + (f" in {_period(p)}" if p > 4 else "") + ", game over (no time left)"
    clock = g.get("Clock") or "?"
    if "HALFTIME" in sn:
        when = "halftime"
    elif "END_PERIOD" in sn:
        when = f"end of {_period(p)}"
    elif p > 4:
        when = f"in {_period(p)} (overtime has no clock)"
    else:
        when = f"{clock} left in {_period(p)}"
    s = f"{score}, LIVE, {when}"
    if p <= 4 and "DELAY" not in sn:
        left = _game_time_left(p, clock, sn)
        if left:
            s += f" ({left} left in the game)"
    if "DELAY" in sn:
        s += ", DELAYED"
    side = {"home": home, "away": away}.get(str(g.get("possession") or "").lower())
    if side and "HALFTIME" not in sn and "END_PERIOD" not in sn:
        s += f", {side} has the ball" + (" in the red zone" if g.get("is_red_zone") else "")
    if age is not None and age > LIVE_STALE:
        s += f" (STALE: the feed last updated {int(age // 60)} min ago, so this may be behind)"
    return s


def _live_header(ctx: dict, what: str) -> str:
    at = ctx.get("live_at")
    when = f"{at:%-I:%M %p} CT" if at else "now"
    return (f"{what} (as of {when}; real scores from the live feed. Use these exact "
            "numbers, never invent or guess a score):")


def live_block(ctx: dict) -> str:
    """Pool games' live state for the per-request tail ('' when not loaded)."""
    if "live_games" not in ctx:
        return ""
    if ctx.get("live_error"):
        return ("LIVE SCORES: the score feed is unreachable right now. If asked about a score "
                "or the clock, say you can't see live scores at the moment; never guess one.")
    by_id = {g["GameId"]: g for g in ctx["live_games"]}
    now_utc = _now_utc()
    now_ct = _now_ct_naive()
    age = feed_age(ctx["live_games"], now_utc)
    out = [_live_header(ctx, "LIVE SCOREBOARD, this week's pool games")]
    for i, pg in enumerate(ctx.get("games_this_week") or [], 1):
        g = by_id.get(pg["id"])
        if g is None:
            out.append(f"{i}. {pg['away']} at {pg['home']}: no score data.")
            continue
        g = {**g, "line": pg.get("line"), "winner": pg.get("winner") or g.get("winner")}
        s = f"{i}. {game_state(g, now_utc, now_ct, age)}."
        if g.get("Status") == "in_progress" and g.get("line") is not None:
            s += f" Currently covering the pool line: {_cover_text(g)}."
        elif g.get("Status") == "final" and g.get("line") is not None:
            won = g.get("winner") or covering_side(g)
            lost = g["Away"] if same_team(won, g["Home"]) else g["Home"]
            ln = float(g["line"])
            s += (f" Covered: {_side_label(won, g['Home'], ln)}; {_side_label(lost, g['Home'], ln)} did NOT cover."
                  if ln else f" Covered: {won}.")
        out.append(s)
    out.append("Score, time left or covering asked without naming a game: answer for the LIVE game(s) above. "
               "Time left is the clock shown for that quarter (quarters are 15:00).")
    return "\n".join(out)


def _match_key(name) -> str:
    """_norm_team for matching words in a question: "Hawai'i" == 'hawaii'."""
    return re.sub(r"\bst\b", "state", _norm_team(str(name or "").replace("'", "").replace("’", "")))


def games_named(question: str, games: list[dict], cap: int = 3) -> list[dict]:
    """Games whose team the question names: full names first, longest first
    ('Texas Tech' isn't 'Texas'), then '<X> State' teams by '<X>' alone
    ('Boise') when no other team is called just '<X>'."""
    q = f" {_match_key(question)} "
    names: dict[str, list[dict]] = collections.defaultdict(list)
    for g in games:
        for side in (g["Away"], g["Home"]):
            names[_match_key(side)].append(g)
    keys = sorted(names, key=len, reverse=True)
    stems = {k[: -len(" state")]: k for k in keys if k.endswith(" state")}
    keys += [s for s in sorted(stems, key=len, reverse=True) if s not in names]
    out: list[dict] = []
    for k in keys:
        if not k or f" {k} " not in q:
            continue
        q = q.replace(f" {k} ", " | ")
        for g in names.get(k) or names[stems[k]]:
            if g not in out:
                out.append(g)
    return out[:cap]


_ON_NOW = re.compile(r"\b(on right now|on now|what'?s on|games? (are )?(on|live)|live games?|in progress|"
                     r"being played|still going|playing right now)\b", re.I)
_UPSET = re.compile(r"\bupsets?\b|\bunderdogs? (are )?(winning|leading|won)\b", re.I)
_FINALS = re.compile(r"\bfinals?\b|\bfinal scores?\b|\bwho won\b|\bresults?\b", re.I)
_SCORE_ASK = re.compile(r"\b(scores?|winning|losing|leading|time left|time is left|how long|clock|quarter|"
                        r"halftime|covering|who won|final)\b", re.I)


def _underdog_ahead(g: dict) -> float | None:
    """Spread size when the market underdog (`line`, home-team adjustment)
    is ahead or won; else None."""
    try:
        ln, a, h = float(g["line"]), float(g["AwayPts"]), float(g["HomePts"])
    except (KeyError, TypeError, ValueError):
        return None
    if ln == 0:
        return None
    dog_margin = (h - a) if ln > 0 else (a - h)
    return abs(ln) if dog_margin > 0 else None


def live_note(ctx: dict, question: str) -> str:
    """Latest user turn only: a non-pool game the user names, or a short
    list for 'what's on' / 'upsets' / 'finals'. '' when it isn't a score ask."""
    if "live_games" not in ctx or ctx.get("live_error"):
        return ""
    q = question or ""
    pool_ids = {g["id"] for g in ctx.get("games_this_week") or []}
    rows = ctx["live_games"]
    others = [g for g in rows if g["GameId"] not in pool_ids]
    now_utc = _now_utc()
    now_ct = _now_ct_naive()
    named = games_named(q, rows)
    age = feed_age(rows, now_utc)
    parts = []
    named_other = [g for g in named if g["GameId"] not in pool_ids]
    if named_other:
        parts.append(_live_header(ctx, "LIVE SCORES for the game they asked about (not a pool game)") + "\n"
                     + "\n".join(f"- {game_state(g, now_utc, now_ct, age)}." for g in named_other))
    live = [g for g in others if g.get("Status") == "in_progress"]
    if _UPSET.search(q):
        ups = sorted(((s, g) for g in others if g.get("Status") in ("in_progress", "final")
                      and (s := _underdog_ahead(g))), key=lambda x: -x[0])
        if ups:
            parts.append(_live_header(ctx, "UPSETS across college football this week (market underdog "
                                      "leading or won, biggest spread first)") + "\n"
                         + "\n".join(f"- {game_state(g, now_utc, now_ct, age)} (was a {_num(s)}-point underdog)"
                                     for s, g in ups[:LIVE_LIST_CAP // 2 + 2]))
        else:
            parts.append("UPSETS: no underdog is leading or has won in the other games this week.")
    elif _ON_NOW.search(q):
        live.sort(key=lambda g: abs((g.get("HomePts") or 0) - (g.get("AwayPts") or 0)))
        more = f"\n(+{len(live) - LIVE_LIST_CAP} more live games)" if len(live) > LIVE_LIST_CAP else ""
        parts.append(_live_header(ctx, f"MORE GAMES ON RIGHT NOW ({len(live)} live, closest first)") + "\n"
                     + ("\n".join(f"- {game_state(g, now_utc, now_ct, age)}." for g in live[:LIVE_LIST_CAP])
                        or "- none") + more)
    if _FINALS.search(q) and not named:
        today = now_ct.date().isoformat()
        fin = [g for g in others if g.get("Status") == "final" and str(g.get("Start (CT)", "")).startswith(today)]
        fin.sort(key=lambda g: str(g.get("SourceUpdatedAt")), reverse=True)
        if fin:
            more = f"\n(+{len(fin) - LIVE_LIST_CAP} more finals today)" if len(fin) > LIVE_LIST_CAP else ""
            parts.append(_live_header(ctx, "MORE FINALS TODAY (latest first)") + "\n"
                         + "\n".join(f"- {game_state(g, now_utc, now_ct, age)}." for g in fin[:LIVE_LIST_CAP]) + more)
    if not named and not parts and _SCORE_ASK.search(q):
        by_id = {g["GameId"]: g for g in rows}
        on = [by_id[i] for i in pool_ids if by_id.get(i, {}).get("Status") == "in_progress"]
        parts.append(("Pool games live now:\n" + "\n".join(f"- {game_state(g, now_utc, now_ct, age)}." for g in on)
                      + "\n" if on else "")
                     + "If they named a game that isn't on the LIVE SCOREBOARD: it isn't in this week's score "
                     "data, so say you don't have it. Never guess a score.")
    if parts:
        parts.insert(0, "SCORE QUESTION: this is a scoreboard question, which you DO answer: give the scores "
                        "below, a line per game, then one jab. Never call it off-topic.")
    return "\n\n".join(parts)


# -----------------------------------------------------------------------------
# per-pick results, graded in code. The 9B redid spread math from the raw
# score ("Boise won, so your Oregon State pick didn't cover") and ignored the
# Covered/HIT labels, so every decided pick gets an explicit WIN/LOSS line,
# the record comes pre-counted, and the answer is checked against both
# before it goes out (check_answer). Per-request: user turn only, never
# prompt_prefix().
# -----------------------------------------------------------------------------

def _pts(x) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _need(spread: float, fav: bool) -> str:
    """'needed to win by 14 or more' / 'needed to lose by 13 or fewer'."""
    if spread != int(spread):
        return (f"needed to win by {int(spread) + 1} or more" if fav
                else f"needed to lose by {int(spread)} or fewer")
    return f"needed to win by more than {_num(spread)}" if fav else f"needed to lose by fewer than {_num(spread)}"


def grade_pick(pick, away, home, line, winner=None, status=None, away_pts=None, home_pts=None,
               jackpot=False) -> dict:
    """One pick as the league grades it. {'side', 'label', 'result', 'why'}:
    result is WIN / LOSS / IN PROGRESS / NOT STARTED. A final is graded by
    `winner` (the side that covered); scores only explain it."""
    side = home if same_team(pick, home) else away if same_team(pick, away) else pick
    ln = _pts(line)
    label = (f"{side} (pick'em)" if ln == 0 else _side_label(side, home, ln)) if ln is not None else str(side)
    ap, hp = _pts(away_pts), _pts(home_pts)
    out = {"side": side, "label": label, "result": "NOT STARTED", "why": ""}
    decided = bool(winner) or status == "final"
    if not decided:
        if status in ("in_progress", "in", "live"):
            out["result"] = "IN PROGRESS"
            if ap is not None and hp is not None and ln is not None:
                cov = covering_side({"Away": away, "Home": home, "AwayPts": ap, "HomePts": hp, "line": ln})
                out["why"] = "currently covering" if same_team(cov, side) else "currently NOT covering"
        return out
    covered = winner
    if not covered and ap is not None and hp is not None and ln is not None:
        covered = covering_side({"Away": away, "Home": home, "AwayPts": ap, "HomePts": hp, "line": ln})
    if covered is None:
        return out
    win = bool(jackpot) or same_team(pick, covered)
    out["result"] = "WIN" if win else "LOSS"
    if jackpot and not same_team(pick, covered):
        out["why"] = "slots jackpot pick, counts as a win"
        return out
    if ap is None or hp is None or ln is None:
        out["why"] = "covered" if win else "did not cover"
        return out
    mine, theirs = (hp, ap) if side == home else (ap, hp)
    diff = mine - theirs
    sc = f"{_num(mine)}-{_num(theirs)}"
    fav = ln != 0 and ((side == home) == (ln < 0))
    sp = abs(ln)
    margin = (hp + ln - ap) * (1 if side == home else -1)
    if margin == 0:
        why = (f"landed exactly on the number ({sc}); the league gives a push to the away side, "
               + ("so it counts as covered" if win else "so it did not cover"))
    elif ln == 0:
        why = f"{'won' if diff > 0 else 'lost'} the game {sc}"
    elif win:
        why = (f"covered: won outright {sc} as the underdog" if not fav and diff > 0 else
               f"covered: lost the game {sc}, by {_num(-diff)}, and only {_need(sp, False)}" if not fav else
               f"covered: won by {_num(diff)} ({sc}), {_need(sp, True)}")
    else:
        why = (f"did not cover: won the game by {_num(diff)} ({sc}) but {_need(sp, True)}" if fav and diff > 0 else
               f"did not cover: lost the game outright {sc}" if fav else
               f"did not cover: lost by {_num(-diff)} ({sc}), {_need(sp, False)}")
    # trust the graded winner; if the score disagrees (stale row), drop the explanation
    if (margin > 0 or (margin == 0 and side == away)) != win:
        why = "covered" if win else "did not cover"
    out["why"] = why
    return out


def _live_by_teams(ctx: dict) -> dict:
    return {(_match_key(g.get("Away")), _match_key(g.get("Home"))): g for g in ctx.get("live_games") or []}


def member_pick_results(ctx: dict, team: str) -> list[dict]:
    """A member's revealed picks this week, graded (privacy: revealed only)."""
    live = {g["GameId"]: g for g in ctx.get("live_games") or []}
    picks = {p["game_id"]: p for p in (ctx.get("revealed_picks_this_week") or {}).get(team, [])}
    out = []
    for g in ctx.get("games_this_week") or []:
        p = picks.get(g["id"])
        if p is None:
            continue
        lg = live.get(g["id"]) or {}
        r = grade_pick(p["pick"], g["away"], g["home"], g.get("line"), g.get("winner"),
                       lg.get("Status") or g.get("status"), lg.get("AwayPts"), lg.get("HomePts"),
                       p.get("jackpot"))
        out.append({**r, "game": g, "tiebreaker": bool(g.get("tiebreaker"))})
    return out


def picky_pick_results(ctx: dict) -> list[dict]:
    """Picky's own revealed picks this week, graded. Only the games he picked."""
    pp = ctx.get("picky_player") or {}
    live = _live_by_teams(ctx)
    out = []
    for p in pp.get("this_week") or []:
        if not p.get("pick"):
            continue
        lg = live.get((_match_key(p["away"]), _match_key(p["home"]))) or {}
        r = grade_pick(p["pick"], p["away"], p["home"], p.get("line"), p.get("winner"),
                       lg.get("Status") or p.get("status"), lg.get("AwayPts"), lg.get("HomePts"))
        out.append({**r, "game": {"away": p["away"], "home": p["home"], "winner": p.get("winner")},
                    "tiebreaker": bool(p.get("tiebreaker"))})
    return out


def results_record(rows: list[dict]) -> tuple[int, int]:
    counted = [r for r in rows if not r["tiebreaker"] and r["result"] in ("WIN", "LOSS")]
    w = sum(r["result"] == "WIN" for r in counted)
    return w, len(counted) - w


def _result_line(r: dict) -> str:
    why = f" ({r['why']})" if r["why"] else ""
    tb = " [tiebreaker game, doesn't count]" if r["tiebreaker"] else ""
    return f"- {r['label']} = {r['result']}{why}{tb}."


_PICKY_ASK = re.compile(r"\bpicky\b|\byour (own )?(record|picks?|week|season|bets?)\b|"
                        r"\byou (pick|picked|take|took|doing|do|going)\b|\bhow (are|'?re) you\b|\bhow'?d you\b|"
                        r"\b(are|were) you\b.*\b(record|picks?|week)\b", re.I)
RESULTS_ASK = re.compile(r"\b(record|cover\w*|hit|hits|miss\w*|result\w*|won|win\w*|lost|los[ei]\w*|"
                         r"how (did|'?d|is|are|am)|how'?s|how'?d|doing|picks?|picked|week|score\w*|"
                         r"\d+-\d+|wrong|right side)\b", re.I)


def results_subjects(ctx: dict, question: str) -> list[tuple[str, str]]:
    """[(kind, team)]: kind 'you' (the caller), 'member' (named), 'picky'."""
    q = question or ""
    me = (ctx.get("you") or {}).get("team_name")
    out: list[tuple[str, str]] = []
    if me and me in (ctx.get("revealed_picks_this_week") or {}):
        out.append(("you", me))
    for r in ctx.get("standings_ytd") or []:
        if r["team"] != me and re.search(rf"(?<!\w){re.escape(r['team'])}(?!\w)", q, re.I):
            out.append(("member", r["team"]))
    ld = (ctx.get("leader") or {}).get("team_name")
    if ld and ld != me and _LEADER_ALIAS.search(q) and ("member", ld) not in out:
        out.append(("member", ld))
    if ctx.get("picky_player") and _PICKY_ASK.search(q):
        out.append(("picky", (ctx["picky_player"] or {}).get("name") or "Picky"))
    return out


def results_facts(ctx: dict, question: str) -> dict | None:
    """Everything the results block states and check_answer enforces, or None
    when the question isn't about results or nothing has kicked off."""
    q = question or ""
    subs = results_subjects(ctx, q)
    named_game = games_named(q, [{"Away": g["away"], "Home": g["home"], "id": g["id"]}
                                 for g in ctx.get("games_this_week") or []])
    if not (RESULTS_ASK.search(q) or named_game or any(k != "you" for k, _ in subs)):
        return None
    subjects = []
    for kind, team in subs[:4]:
        rows = picky_pick_results(ctx) if kind == "picky" else member_pick_results(ctx, team)
        rows = [r for r in rows if r["result"] != "NOT STARTED"]
        if not rows:
            continue
        subjects.append({"kind": kind, "team": team, "rows": rows, "record": results_record(rows)})
    if not subjects:
        return None
    # every decided pool game: game-level cover facts are true for everyone
    games = []
    live = {x["GameId"]: x for x in ctx.get("live_games") or []}
    for g in ctx.get("games_this_week") or []:
        if g.get("winner") and not g.get("tiebreaker"):
            cov = g["home"] if same_team(g["winner"], g["home"]) else g["away"]
            row = {"covered": cov, "not": g["away"] if cov == g["home"] else g["home"]}
            lg = live.get(g["id"]) or {}
            ap, hp = _pts(lg.get("AwayPts")), _pts(lg.get("HomePts"))
            if lg.get("Status") == "final" and ap is not None and hp is not None and ap != hp:
                row["won_game"], row["lost_game"] = (g["away"], g["home"]) if ap > hp else (g["home"], g["away"])
            games.append(row)
    pp = ctx.get("picky_player") or {}
    me = (ctx.get("you") or {}).get("team_name")
    st = {r["team"]: (r["w"], r["l"]) for r in ctx.get("standings_ytd") or []}

    def allowed(kind, team, rec, qualified=True):
        """Records that may be quoted: this week + season, plus (only when the
        sentence names another span) last week and earlier weeks."""
        ok = {rec}
        if kind == "picky":
            ok.add(tuple(pp.get("season") or ()))
            if qualified:
                ok.add(tuple(pp.get("last_week") or ()))
        else:
            if team in st:
                ok.add(st[team])
            if qualified:
                ok |= {tuple(v) for v in ((ctx.get("weekly_records") or {}).get(team) or {}).values()}
        return {x for x in ok if len(x) == 2}

    recs, recs_now = {}, {}
    for s in subjects:
        k = s["kind"] if s["kind"] != "member" else s["team"]
        recs[k] = allowed(s["kind"], s["team"], s["record"])
        recs_now[k] = allowed(s["kind"], s["team"], s["record"], qualified=False)
    # not a subject: still pin season numbers (and Picky's week record) so 'I'm 2-0' can't slip by
    if "you" not in recs and me in st:
        recs["you"] = allowed("you", me, st[me])
        recs_now["you"] = allowed("you", me, st[me], qualified=False)
    if "picky" not in recs and pp.get("season"):
        prow = [r for r in picky_pick_results(ctx) if r["result"] != "NOT STARTED"]
        recs["picky"] = allowed("picky", "", results_record(prow))
        recs_now["picky"] = allowed("picky", "", results_record(prow), qualified=False)
    return {"subjects": subjects, "games": games, "records": recs, "records_now": recs_now, "question": q,
            "named_teams": [k for g in named_game for k in (g["Away"], g["Home"])]}


def results_block(facts: dict | None) -> str:
    if not facts:
        return ""
    out = ["RESULTS, ALREADY GRADED IN CODE (the league grades against the pool line). State each "
           "result word exactly as written. Never recompute a cover from a score: a pick that LOST "
           "THE GAME can still be a WIN, and a favorite that won the game can still be a LOSS."]
    for s in facts["subjects"]:
        w, l = s["record"]
        n_open = sum(r["result"] == "IN PROGRESS" for r in s["rows"])
        left = f", {n_open} still in progress" if n_open else ""
        if s["kind"] == "you":
            head = f"THE USER'S ({s['team']}'s) PICKS THIS WEEK, say 'you':"
            rec = f"THE USER'S RECORD THIS WEEK ON DECIDED GAMES: {w}-{l}{left}. Say 'you're {w}-{l}'."
        elif s["kind"] == "picky":
            head = ("PICKY'S OWN PICKS THIS WEEK (you are Picky: say 'I' and 'my'). These are the only "
                    "picks of yours that have kicked off; never count anyone else's picks as yours:")
            rec = f"PICKY'S RECORD THIS WEEK ON DECIDED GAMES: {w}-{l}{left}. Say 'I'm {w}-{l}'."
        else:
            head = f"{s['team'].upper()}'S PICKS THIS WEEK (not the user: 'he' or 'they'):"
            rec = f"{s['team'].upper()}'S RECORD THIS WEEK ON DECIDED GAMES: {w}-{l}{left}."
        out.append(head)
        out += [_result_line(r) for r in s["rows"]]
        out.append(rec)
    out.append("Market lines: only quote an ESPN market number that appears in the game data; never make one up.")
    return "\n".join(out)


# --- the contradiction guard -------------------------------------------------

_NEGATION = re.compile(r"(?:\bnot|n't|\bdidnt|\bdoesnt|\bnever|\bno|\bwithout|\bfailed to|\bfails to)\W*(?:\w+\W+){0,2}$",
                       re.I)
_COVER_WORD = re.compile(r"\bcover(ed|s)?\b", re.I)
_NEG_ONLY = re.compile(r"\bmiss(ed|es)?\b|\bwrong side\b|\b(pick|bet|side) (lost|died|failed|busted)\b|"
                       r"\b(a|the|your|my|his) (loser|losing pick)\b", re.I)
_NEG_CASED = re.compile(r"\bLOSS\b|\bMISS\b|\b(an|the|your|my|his) L\b")
_POS_ONLY = re.compile(r"\bhit\b|\bcashed\b|\b(a|the|your|my|his) (winner|winning pick)\b", re.I)
_POS_CASED = re.compile(r"\bWIN\b|\b(a|the|your|my|his) W\b")
_PICKREF = re.compile(r"\b(your|my|his|their|the) (?:[\w+-.]+\s+){0,3}?(pick|bet|side|play)\b|\b(you|i|he) (took|had|picked|rode|backed)\b",
                      re.I)
_REC_RE = re.compile(r"\b(you'?re|you are|you went|you sit at|you'?re sitting at|your record(?: is|:)?|"
                     r"i'?m|i am|i went|i sit at|i'?m sitting at|my record(?: is|:)?)\s+(?:now\s+|at\s+|a\s+)?"
                     r"(\d{1,2})\s*-\s*(\d{1,2})\b", re.I)


_OBJECT_BEFORE = re.compile(r"\b(beat|beats|beating|over|against|vs\.?|versus|to|crushed|smoked|destroyed|"
                            r"blew out|routed|than|from|faded|fading|fade)\s+(the\s+)?$", re.I)
# '<team> [-13.5] [pick] [still] lost ...': the verb right after the team name
_VERB_AFTER = re.compile(r"\W*(?:[-+]?\d+(?:\.\d+)?\s*)?(?:pick\s+)?(?:(?:still|actually|just|easily)\s+)?"
                         r"(lost|won|beat|fell|dropped)\b(?! (?:the )?(?:cover|spread|line|number|value))", re.I)


def polarity(clause: str) -> tuple[bool, bool]:
    """(says it covered/won, says it didn't) for one clause of the answer."""
    pos = bool(_POS_ONLY.search(clause) or _POS_CASED.search(clause))
    neg = bool(_NEG_ONLY.search(clause) or _NEG_CASED.search(clause))
    for m in _COVER_WORD.finditer(clause):
        before = clause[: m.start()]
        if _NEGATION.search(before[-40:]):
            neg = True
        elif re.search(r"\b(to|needed|need|needs|had to|would|could|must)\s*$", before, re.I):
            continue  # 'needed to cover' says nothing about the result
        else:
            pos = True
    return pos, neg


def _team_re(name: str, stems_ok: bool) -> str:
    k = re.escape(name.lower()).replace("\\ ", r"\s+").replace("'", "'?")
    k = re.sub(r"state$", r"(state|st\\.?)", k)
    alts = [k]
    if stems_ok and name.lower().endswith(" state"):
        alts.append(re.escape(name.lower()[: -len(" state")]).replace("\\ ", r"\s+"))
    return r"(?<!\w)(" + "|".join(alts) + r")(?!\w)"


def _sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?])\s+|\n+", text or "") if s.strip()]


def check_answer(text: str, facts: dict | None, allowed_numbers: set | None = None) -> list[str]:
    """Statements in the answer that contradict the computed results: a
    decided game's cover flipped, a subject's pick called the wrong result,
    a wrong record, or a line number that isn't in the data."""
    probs: list[str] = []
    t = (text or "").replace("’", "'")
    if facts:
        teams = {}
        all_names = [g[k] for g in facts["games"] for k in ("covered", "not")]
        for s in facts["subjects"]:
            all_names += [s2 for r in s["rows"] for s2 in (r["game"]["away"], r["game"]["home"])]
        all_names = list(dict.fromkeys(all_names))
        stems = collections.Counter(n.lower()[: -len(" state")] for n in all_names if n.lower().endswith(" state"))
        plain = {n.lower() for n in all_names}
        for n in all_names:
            stem = n.lower()[: -len(" state")] if n.lower().endswith(" state") else None
            teams[n] = re.compile(_team_re(n, bool(stem) and stems[stem] == 1 and stem not in plain), re.I)
        status, game_res = {}, {}
        for g in facts["games"]:
            status[g["covered"]], status[g["not"]] = "WIN", "LOSS"
            if g.get("won_game"):
                game_res[g["won_game"]], game_res[g["lost_game"]] = "won", "lost"
        by_kind = {("you" if s["kind"] == "you" else "picky" if s["kind"] == "picky" else s["team"]): s
                   for s in facts["subjects"]}
        members = [s for s in facts["subjects"] if s["kind"] == "member"]
        for sent in _sentences(t):
            hits = sorted((m.start(), n) for n, rx in teams.items() for m in rx.finditer(sent))
            # 1) game level: '<team> covered' / '<team> didn't cover'
            for i, (pos0, n) in enumerate(hits):
                end = hits[i + 1][0] if i + 1 < len(hits) else len(sent)
                clause = sent[pos0:end]
                # 'Boise State won, but your pick still hit': the part after ', but' isn't about Boise
                clause = re.split(r",\s*(?:but|so|and|yet|while|which|because)\b|;|:|\s[-–—]\s", clause, maxsplit=1)[0]
                before = sent[max(0, pos0 - 25):pos0]
                # an object ('Wake Forest beat SMU and covered'): the verb after it isn't about it
                if _OBJECT_BEFORE.search(before):
                    continue
                # the game itself: '<team> lost the game 46-41' / 'you lost the <team> game'
                head = _VERB_AFTER.match(clause[len(rx_m.group(0)) if (rx_m := teams[n].match(clause)) else 0:])
                verb = head.group(1).lower() if head else None
                pre = re.search(r"\b(lost|won)\s+(?:the\s+)?$", before, re.I)
                if pre and re.match(r"\W*game\b", clause[len(rx_m.group(0)) if rx_m else 0:], re.I):
                    verb = pre.group(1).lower()
                if n in game_res and verb:
                    if game_res[n] == "won" and verb in ("lost", "fell", "dropped"):
                        probs.append(f"says {n} lost the game, but {n} WON the game (the pick is graded on the cover)")
                    elif game_res[n] == "lost" and verb in ("won", "beat"):
                        probs.append(f"says {n} won the game, but {n} LOST the game (the pick is graded on the cover)")
                if _PICKREF.search(clause) or n not in status:
                    continue
                p, ng = polarity(clause)
                if status[n] == "WIN" and ng and not p:
                    probs.append(f"says {n} did not cover, but {n} COVERED")
                elif status[n] == "LOSS" and p and not ng:
                    probs.append(f"says {n} covered, but {n} did NOT cover")
            # 2) pick references: 'your pick did NOT cover'
            m = _PICKREF.search(sent)
            if m:
                who = m.group(0).split()[0].lower()
                subj = (by_kind.get("you") if who in ("your", "you") else
                        by_kind.get("picky") if who in ("my", "i") else
                        (members[0] if len(members) == 1 else None) if who in ("his", "their", "he") else
                        (members[0] if len(members) == 1 else by_kind.get("you")))
                if subj:
                    named = {n for _, n in hits} | set(facts.get("named_teams") or [])
                    rows = [r for r in subj["rows"] if r["result"] in ("WIN", "LOSS")
                            and ({r["game"]["away"], r["game"]["home"]} & named)]
                    decided = [r for r in subj["rows"] if r["result"] in ("WIN", "LOSS")]
                    if not rows and len(decided) == 1:
                        rows = decided
                    if len(rows) == 1:
                        p, ng = polarity(sent[m.start():])
                        r = rows[0]
                        if r["result"] == "WIN" and ng and not p:
                            probs.append(f"says {subj['team']}'s {r['label']} pick lost, but it is a WIN")
                        elif r["result"] == "LOSS" and p and not ng:
                            probs.append(f"says {subj['team']}'s {r['label']} pick won, but it is a LOSS")
        # 3) records
        for sent in _sentences(t):
            other_span = re.search(r"\b(last week|week \d+|season|year|overall|on the year|all year|"
                                   r"total|ytd|so far this season)\b", sent, re.I)
            for m in _REC_RE.finditer(sent):
                who = "you" if m.group(1).lower().startswith(("you", "your")) else "picky"
                ok = (facts["records"] if other_span else facts.get("records_now") or facts["records"]).get(who)
                rec = (int(m.group(2)), int(m.group(3)))
                if ok and rec not in ok:
                    want = by_kind.get(who)
                    probs.append(f"says {'the user is' if who == 'you' else 'Picky is'} {rec[0]}-{rec[1]}"
                                 + (f", but the record this week is {want['record'][0]}-{want['record'][1]}"
                                    if want else ""))
    # 4) market/spread numbers not in the data
    if allowed_numbers is not None:
        for m in re.finditer(r"(?<![\w.])[-+](\d{1,2}(?:\.5)?)(?!\d|\.\d)|(?<![\d.])(\d{1,2}\.5)(?!\d)", t):
            v = float(m.group(1) or m.group(2))
            if v not in allowed_numbers:
                probs.append(f"states a line {m.group(0)} that isn't in the data")
    return list(dict.fromkeys(probs))


def allowed_line_numbers(prompt: str) -> set:
    """Every spread-like number the prompt gives: signed numbers (pool and
    market lines, value checks) and x.5 numbers on line/value text."""
    nums = {abs(float(x)) for x in re.findall(r"(?<![\w.])[-+](\d+(?:\.\d+)?)", prompt)}
    for ln in prompt.splitlines():
        if re.search(r"line|value|spread|market|points better|needed", ln, re.I):
            nums |= {float(x) for x in re.findall(r"(?<![\d.])(\d+\.5)(?!\d)", ln)}
    return nums


def correction_rider(probs: list[str], facts: dict | None) -> str:
    return ("CORRECTION: your last draft was WRONG: " + "; ".join(probs) + ". Write the answer again. "
            "Copy each WIN/LOSS word and every record exactly from the RESULTS block; do not redo any "
            "spread math from the scores; quote no line that isn't in the data.\n\n" + results_block(facts))


_TEMPLATE_JABS = ["Scoreboard doesn't lie, so here it is straight:",
                  "Let me spell it out so even this league gets it:",
                  "Facts, no spin:"]


def templated_answer(facts: dict) -> str:
    """In-character fallback built only from the computed facts."""
    q = facts.get("question") or ""
    order = {"picky": 0, "member": 1, "you": 2}
    subj = sorted(facts["subjects"], key=lambda s: order[s["kind"]])[0]
    named = set(facts.get("named_teams") or [])
    rows = [r for r in subj["rows"] if {r["game"]["away"], r["game"]["home"]} & named] or subj["rows"]
    w, l = subj["record"]
    lines = []
    for r in rows:
        why = f" ({r['why']})" if r["why"] else ""
        lines.append(f"{r['label']} is {'a ' + r['result'] if r['result'] in ('WIN', 'LOSS') else r['result'].lower()}{why}.")
    who = {"you": f"You're {w}-{l}", "picky": f"I'm {w}-{l}", "member": f"{subj['team']} is {w}-{l}"}[subj["kind"]]
    tail = {"you": " Don't get cocky.", "picky": " Don't rub it in.", "member": ""}[subj["kind"]]
    jab = _TEMPLATE_JABS[sum(map(ord, q)) % len(_TEMPLATE_JABS)]
    return f"{jab} " + " ".join(lines) + f" {who} on decided games this week.{tail}"


async def _collect(stream) -> tuple[str, list[str]]:
    """Run a model stream to the end: (answer text, non-content frames to pass on)."""
    text, other = "", []
    async for chunk in stream:
        if chunk.startswith("data: {"):
            obj = json.loads(chunk[6:])
            if "content" in obj:
                text += obj["content"]
                continue
        if chunk.startswith("data: [DONE]"):
            break
        other.append(chunk)
    return text, other


async def _guarded_stream(convo: list[dict], prefix: str | None, facts: dict):
    """Buffer the answer, check it against the computed results, regenerate
    once with a correction on a contradiction, then fall back to a template."""
    allowed = allowed_line_numbers("\n".join(m["content"] for m in convo))
    text, other = await _collect(_model_stream(convo, prefix))
    if other and not text:
        for c in other:
            yield c
        return
    probs = check_answer(text, facts, allowed)
    if probs:
        log.warning("results guard: %s; regenerating. draft: %r", probs, text[:400])
        fix = convo[:-1] + [{"role": "user", "content": convo[-1]["content"] + "\n\n"
                             + correction_rider(probs, facts) + "\n\n" + facts["question"]}]
        text2, other2 = await _collect(_model_stream(fix, prefix))
        probs2 = check_answer(text2, facts, allowed) if text2 else ["no answer"]
        if probs2:
            log.warning("results guard: still %s; templated answer. draft: %r", probs2, text2[:400])
            text = templated_answer(facts)
        else:
            text = text2
    yield _sse({"content": text})
    yield "data: [DONE]\n\n"


def _system_tail(ctx: dict) -> str:
    you = ctx.get("you", {}).get("team_name", "you")
    now = _now_ct_naive()
    yb = you_block(ctx)
    return (
        "\n\n" + (PICKS_MODE_RIDER if ctx.get("mode") == "picks" else LEADERBOARD_MODE_RIDER).strip()
        + ("\n\n" + yb if yb else "")
        + f"\n\nThe user reading this is \"{you}\": call them 'you'. Everyone "
        "else is 'he' or 'they', by name."
        + (DADDYFREEMAN_RIDER if ctx.get("daddyfreeman") else "")
        # to the hour only: a per-minute clock would change the prompt (and
        # cost a full reprocess) on every message
        + f"\nRight now it is {now:%a %b %-d}, between {now:%-I}:00 and {now:%-I}:59 {now:%p} CT."
        # live scores change by the minute: tail only, never prompt_prefix()
        + ("\n\n" + lb if (lb := live_block(ctx)) else "")
    )


_TRAIT = re.compile(
    r"\b(gay|homo\w*|queer|lesbian|trans\w*|bi|straight|sexual\w*|race|racial|black|white|asian|mexican|"
    r"latino|hispanic|jew\w*|muslim|christian|catholic|religio\w*|mom|moms|mother|mama|momma|dad|father|"
    r"wife|girlfriend|sister|brother|family|kids?|fat|ugly|looks?|face|bald|short|skinny|weight|body|hair|"
    r"teeth|nose|disab\w*|retard\w*)\b", re.I)
_MOCK = re.compile(r"\b(roast|make fun|mock|talk (shit|trash)|trash talk|insult|joke|burn|clown|rip on|"
                   r"go off|shit on|dunk on)\b|\bfor being\b", re.I)
_LEAGUE_WORDS = re.compile(
    r"\b(picks?|picked|spreads?|lines?|cover\w*|games?|teams?|weeks?|standings?|place|rank\w*|records?|"
    r"points?|miss\w*|roast\w*|rules?|jackpot|slots?|pit|chips?|tie ?-?break\w*|payouts?|buy.?in|league|"
    r"pool|commish|commissioner|stats?|yards?|touchdowns?|tds?|qbs?|quarterbacks?|rush\w*|pass\w*|defen[cs]e|"
    r"offen[cs]e|injur\w*|hurt|ats|value|favorite|underdog|dog|scores?|win\w*|los\w*|lost|beat|football|"
    r"bowl|playoffs?|ranked|kick ?off|lock\w*|draw|picture|image|winning|first|last|season|odds|bet\w*|"
    r"coach\w*|players?|leaders?|cfb|ncaa|sec|big ten|acc|big 12|picky|hi|hey|hello|sup|thanks?|"
    # follow-ups lean on the last answer: never off-topic
    r"those|these|them|that|this|it|they|he|his|him|she|her|change|flip|switch|keep|why|explain|"
    r"sure|really|again|more|else|instead|same|other)\b", re.I)


_PICKS_ASK = re.compile(r"\bpicks?\b|\bpicked\b|\bpicking\b|\bvalue\b|\bflip|\bchange\b|\btook\b|"
                        r"\btaking\b|\bline\b|\bspread\b|\bsides?\b|\bthis week\b|\bwho has\b", re.I)
# Things the written rules say nothing about. The 9B otherwise stretches the
# lock rule to fit ("a delay buys no time, so the line stays").
_UNCOVERED_ASK = re.compile(r"\b(postpon\w*|cancel\w*|forfeit\w*|suspend\w*|reschedul\w*|called off)\b", re.I)
_INJURY_ASK = re.compile(r"\binjur\w*|\bhurt\b|\bhealthy\b|\bhealth\b|\bquestionable\b|"
                         r"\bis \w+ playing\b|\bbanged up\b|\bout for\b", re.I)


def guard_note(ctx: dict, question: str) -> str:
    """Deterministic rails for the two things a 9B model botches most:
    taking trait bait, and dragging league talk into off-topic questions."""
    q = question or ""
    m = _TRAIT.search(q)
    names = [r["team"] for r in ctx.get("standings_ytd") or []]
    if m and _MOCK.search(q):
        who = guard_target(ctx, q)
        me = (ctx.get("you") or {}).get("team_name")
        # Chuck asking gets the same rails as anyone; only others revere him.
        leader = None if leader_is_caller(ctx) else (ctx.get("leader") or {}).get("team_name")
        head = (f"GUARDRAIL: this asks you to mock someone over '{m.group(0).lower()}'. Don't. One short "
                "in-character refusal (you roast picks, not people), then ")
        tail = (" Never describe or rate anyone's body, face, looks, family, race, religion or "
                "sexuality, not even to deny it.")
        if who and who == leader and who != me and me in names:
            return (head + f"glorify {who}, who is above mockery, and roast the user, {me}, for asking, "
                    f"using ONLY these facts:\n{member_facts(ctx, me)}" + tail)
        if who and who == leader:
            return head + f"hail {who}'s genius with his real numbers:\n{member_facts(ctx, who)}" + tail
        if who:
            # The 9B pinned the caller's picks on the target (Ackbar 'took
            # Oregon State', which was Chuck's pick), so the target's own
            # picks ride here, labeled with his name and the week.
            return (head + f"roast {who}'s real record, place and picks, using ONLY these facts about "
                    f"{who}:\n{member_facts(ctx, who)}\nName only picks listed above for {who}. Every other "
                    "pick in LEAGUE DATA belongs to someone else, including the user's own picks." + tail)
        return head + "roast their picks without naming any specific pick or number." + tail
    teams = [t for g in ctx.get("games_this_week") or [] for t in (g["away"], g["home"])]
    idx = _cfbd_index()
    others = cfbd.other_teams_named(q, teams, idx) if idx else []
    if _BET_WORDS.search(q):  # 'ND vs USC': too short for other_teams_named
        others += [t for t in indies_named(q) if t not in teams and t not in others]
    if others and opinion_note(q, ctx) and not _BET_WORDS.search(q):
        others = []  # 'Notre Dame is back' is the opinion rider's, not a pool question
    if others and not cfbd.STAT_ASK.search(q) and not re.search(r"\bats\b|against the spread|record", q, re.I):
        indies = indies_named(q)
        rest = [t for t in others if t not in INDEPENDENTS]
        named = list(dict.fromkeys(indies + others))
        say = (f"Say so in one or two lines: hype {' and '.join(indies)}"
               + (f", trash {' and '.join(rest)}" if rest else "") + "." if indies
               else "Say so and roast them in one or two lines.")
        return (f"NOT IN THE POOL: {' and '.join(named)} {'are' if len(named) > 1 else 'is'} not in this "
                f"week's pool. {say} No spread, no pick, no prediction, no records or standings.")
    if (len(q.split()) >= 3 and not others and not _LEAGUE_WORDS.search(q) and not _LEADER_ALIAS.search(q)
            and not asks_about_self(q) and not opinion_note(q, ctx)
            and not any(re.search(rf"(?<!\w){re.escape(x)}(?!\w)", q, re.I) for x in names + teams)):
        return ("OFF-TOPIC: this isn't about football or the league. Roast them for it in one or two "
                "short lines and stop. No records, picks, games, standings or numbers.")
    return ""


def guard_target(ctx: dict, q: str) -> str | None:
    """The member a bait names, else the caller for 'me/my'."""
    st = ctx.get("standings_ytd") or []
    who = next((r["team"] for r in st if re.search(rf"(?<!\w){re.escape(r['team'])}(?!\w)", q, re.I)), None)
    if who is None and (ctx.get("leader") or {}).get("team_name") and _LEADER_ALIAS.search(q):
        who = ctx["leader"]["team_name"]
    me = (ctx.get("you") or {}).get("team_name")
    if who is None and re.search(r"\b(me|my|myself)\b", q, re.I) and any(r["team"] == me for r in st):
        who = me
    return who


_STANDINGS_ENDS = re.compile(r"\b(first|1st|winning|leader|leading|in the lead|top of)\b", re.I)
_STANDINGS_LAST = re.compile(r"\b(last place|dead last|in last|bottom|worst team)\b", re.I)


def leader_note(ctx: dict, question: str) -> str:
    """The Dear Leader rider: only when ANOTHER member names Chuck P (team
    name or an alias) or the question's answer is his place (he's first and
    they ask who's winning). Chuck himself never gets it, even talking about
    himself in the third person: he's roasted like any other player."""
    ld = ctx.get("leader") or {}
    name = ld.get("team_name")
    st = ctx.get("standings_ytd") or []
    row = next((r for r in st if r["team"] == name), None) if name else None
    if not row or leader_is_caller(ctx):
        return ""
    q = question or ""
    me = ctx.get("you") or {}
    by_name = bool(re.search(rf"(?<!\w){re.escape(name)}(?!\w)", q, re.I))
    named = by_name or bool(_LEADER_ALIAS.search(q))
    answer_is_him = ((row["rank"] == 1 and _STANDINGS_ENDS.search(q))
                     or (row["rank"] == st[-1]["rank"] and _STANDINGS_LAST.search(q)))
    if not (named or answer_is_him):
        return ""
    # His facts ride along only when the focus block doesn't already carry them.
    in_focus = by_name or guard_note(ctx, q).startswith("GUARDRAIL")  # the pivot carries the facts
    facts = "" if in_focus else member_facts(ctx, name) + "\n"
    wk = (ctx.get("weekly_records") or {}).get(name) or {}
    if wk:  # every week, so 'how did he do in week 8' can be spun, not invented
        facts += f"- {name}'s every week: " + ", ".join(f"{_week_tag(ctx, w)} {wk[w][0]}-{wk[w][1]}" for w in sorted(wk)) + ".\n"
    mine = next((r for r in st if r["team"] == me.get("team_name")), None)
    who = (f"The user is {me.get('team_name')}, NOT the Dear Leader"
           + (f" ({mine['w']}-{mine['l']}, {_ordinal(mine['rank'])} place)" if mine else "") + ".")
    return LEADER_RIDER.format(name=name, facts=facts, who=who, example=LEADER_EXAMPLE_OTHER)


def _user_turn(ctx: dict, question: str) -> str:
    """A user message plus the facts about the people it names. Applied to
    every user turn, not just the last, so a follow-up's prompt starts with
    the previous prompt and Ollama reuses it."""
    guard = guard_note(ctx, question)
    # A member's picks go in the focus block only when the question is about
    # picks: roasts with picks in view kept misreading whose pick was whose.
    opinion = opinion_note(question, ctx)
    pq = re.sub(r"\b(took|taking)\b", "", question or "") if opinion else (question or "")  # 'Freeman took ND to...'
    want_picks = bool(_PICKS_ASK.search(pq)) and not guard.startswith("GUARDRAIL")
    # A guardrail pivot carries its target's facts itself; a focus block next
    # to it put two sets of picks in view and the 9B mixed them up.
    pivot = guard.startswith("GUARDRAIL") and guard_target(ctx, question or "")
    parts = [] if pivot else [focus_block(ctx, question, picks=want_picks).strip()]
    if want_picks and week_locked(ctx):
        parts.append("THIS WEEK'S PICKS ARE LOCKED: it's too late to change any of them. If they ask "
                     "about changing or flipping, say it's too late, then grade the picks as made. "
                     "Never tell them to flip.")
    if wants_rules(question):
        starts = [g.get("start") for g in ctx.get("games_this_week") or [] if g.get("start")]
        lock = ""
        if starts:
            first = min(starts)
            done = str(first) <= _now_ct_naive().isoformat()
            lock = (f"\nTHIS WEEK: the first pool game kicks off {_kickoff(first)}, so this "
                    f"week's picks {'are already locked' if done else 'lock then'}.")
        parts.append("LEAGUE RULES (the complete written rules; anything not here is not a rule):\n"
                     + LEAGUE_RULES + lock
                     + "\nIf this question is about the rules: the answer is almost always "
                     "above, so find it and give it, then stop. No picks, value, games or "
                     "standings. Only if it truly isn't above: the rules don't cover it, ask "
                     "the commissioner.")
    if _UNCOVERED_ASK.search(question or "") and re.search(r"\bgames?\b|\bpicks?\b", question or "", re.I):
        parts.append("NOT IN THE RULES: the written rules say nothing about postponed, canceled or suspended "
                     "games. Say the rules don't cover it and to ask the commissioner. Don't stretch another "
                     "rule to fit.")
    # 'Best QB ever' isn't a this-season stats question: pool leaders or 'no
    # such player: Bo Nix' next to the Nix rider would fight it.
    if not (_NIX_ASK.search(question or "") and not re.search(r"\b(this (season|week|year)|2026|pool)\b",
                                                             question or "", re.I)):
        parts.append(stats_focus(ctx, question).strip())
    parts.append(guard)
    if _INJURY_ASK.search(question or ""):
        parts.append("INJURIES: say there's no injury report and send them to the beat writers. "
                     "Never say 'no injuries', and never call anyone healthy or hurt.")
    parts.append(leader_note(ctx, question))
    parts.append(opinion)
    parts = [p for p in parts if p]
    return "\n\n".join(parts + [question]) if parts else question


# -----------------------------------------------------------------------------
# ollama streaming
# -----------------------------------------------------------------------------


def looping(text: str) -> bool:
    """A degenerate repeat ('Raphael is the man. Raphael is the king. ...'):
    six sentences opening with the same three words, the same sentence
    three times, or past MAX_ANSWER_WORDS."""
    if len(text.split()) > MAX_ANSWER_WORDS:
        return True
    sents = [x.strip().lower() for x in re.split(r"(?<=[.!?])\s+", text) if len(x.split()) >= 3]
    if not sents:
        return False
    heads = collections.Counter(" ".join(x.split()[:3]) for x in sents)
    whole = collections.Counter(sents)
    return max(heads.values()) >= 6 or max(whole.values()) >= 3


# The prefix checkpoint (see prompt_prefix). Every request first sends the
# shared prefix alone as a raw prompt, which leaves a checkpoint right at its
# end; the real request then processes only the tail. When the checkpoint is
# already there (the usual case) the prime costs ~0.1 s; after a data change
# (kickoff, final, ESPN refresh) or a model reload it rebuilds it once.
async def _prime(c: httpx.AsyncClient, prefix: str) -> None:
    payload = {"model": MODEL, "prompt": "<|im_start|>system\n" + prefix, "raw": True,
               "stream": False, "options": {**OLLAMA_OPTIONS, "num_predict": 1},
               "keep_alive": OLLAMA_KEEP_ALIVE}
    try:
        r = await c.post(f"{OLLAMA}/api/generate", json=payload)
        if r.status_code == 200:
            d = r.json()
            log.info("prime: %.1fs (prefix %s)", (d.get("prompt_eval_duration") or 0) / 1e9,
                     format(hash(prefix) & 0xffffff, "06x"))
    except httpx.HTTPError as e:
        log.warning("prime failed: %s", e)


async def _ollama_stream(messages: list[dict], prefix: str | None = None):
    """Yield SSE-framed assistant tokens from Ollama."""
    payload = {"model": MODEL, "messages": messages, "stream": True, "options": OLLAMA_OPTIONS,
               "keep_alive": OLLAMA_FALLBACK_KEEP_ALIVE if CLAUDE else OLLAMA_KEEP_ALIVE}
    if OLLAMA_THINK is not None:
        payload["think"] = OLLAMA_THINK == "true"
    # read covers the wait in Ollama's queue before the first token; the
    # client is kept alive by _keepalive pings meanwhile
    timeout = httpx.Timeout(connect=5.0, read=240.0, write=10.0, pool=5.0)
    text = ""
    try:
        async with httpx.AsyncClient(timeout=timeout) as c:
            if prefix:
                await _prime(c, prefix)
            async with c.stream("POST", f"{OLLAMA}/api/chat", json=payload) as r:
                if r.status_code != 200:
                    body = await r.aread()
                    log.error("ollama %s: %s", r.status_code, body[:200])
                    yield _sse({"error": "ollama_error"})
                    return
                async for line in r.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    chunk = obj.get("message", {}).get("content", "")
                    if chunk:
                        text += chunk
                        if looping(text):
                            # closing the stream makes Ollama stop generating
                            log.warning("answer looping, cut at %d words", len(text.split()))
                            yield "data: [DONE]\n\n"
                            return
                        yield _sse({"content": chunk})
                    if obj.get("done"):
                        log.info("ollama prompt eval %.1fs", (obj.get("prompt_eval_duration") or 0) / 1e9)
                        yield "data: [DONE]\n\n"
                        return
    except httpx.HTTPError as e:
        log.warning("ollama unreachable: %s", e)
        yield _sse({"error": "ollama_unreachable"})


async def _model_stream(messages: list[dict], prefix: str | None = None):
    """Claude first; on any API failure before the first token, the local
    Ollama model answers instead. `messages[0]` is the system prompt."""
    if CLAUDE:
        started = False
        try:
            async with CLAUDE.messages.stream(
                model=CLAUDE_MODEL,
                max_tokens=4096,
                system=messages[0]["content"],
                messages=messages[1:],
                output_config={"effort": CLAUDE_EFFORT},
            ) as stream:
                async for text in stream.text_stream:
                    started = True
                    yield _sse({"content": text})
                final = await stream.get_final_message()
            if started:
                yield "data: [DONE]\n\n"
                return
            log.warning("claude returned no text (stop_reason=%s); falling back", final.stop_reason)
        except anthropic.APIError as e:
            log.warning("claude failed (%s); %s", type(e).__name__,
                        "mid-stream" if started else "falling back to ollama")
            if started:
                yield _sse({"error": "model_error"})
                return
    async for chunk in _ollama_stream(messages, prefix):
        yield chunk


def _sse(obj: dict) -> str:
    return f"data: {json.dumps(obj)}\n\n"


# Profanity is the persona; slurs and sexuality jokes are not. The 9B model
# can't be trusted to hold that line by prompt alone (it once refused a
# "roast him for being gay" bait and then used the f-slur in the same
# breath), so the model's output is scrubbed in code before it streams out.
_SLURS = re.compile(
    r"\b(?:fag+(?:ot|it|ots|its|s|gy|g)?|f[a@]gg?[o0]t\w*|n[i1]gg(?:er|a|ah|uh|ers|as|az)\w*|"
    r"retard(?:s|ed)?|tard|dykes?|trann(?:y|ies)|shemales?|homos?|queers?|gay(?:s|er|est)?|"
    r"lesbos?|spics?|chinks?|kikes?|wetbacks?|gooks?|beaners?|ragheads?|towelheads?|"
    r"sand ?n\w+|coons?|darkies|jigaboos?|pakis?|wops?|gypped)\b",
    re.I,
)


# On a "roast his looks" bait the 9B refuses, then riffs on the bait word
# anyway ("that's the only thing ugly about him"). For those questions only,
# the looks words are scrubbed out of the answer too.
_LOOKS = re.compile(r"\b(ugly|uglier|ugliest|fat|fatter|fattest|obese|bald|skinny|chubby)\b", re.I)


def looks_bait(question: str) -> bool:
    q = question or ""
    return bool(_LOOKS.search(q) and _MOCK.search(q))


def scrub(text: str, looks: bool = False) -> str:
    text = _SLURS.sub("dumbass", text)
    return _LOOKS.sub("bad", text) if looks else text


async def _scrubbed(stream, looks: bool = False):
    """Scrub model output as it streams. A slur can be split across chunks,
    so the trailing partial word is held back until the next chunk (or the
    end) shows where it stops."""
    held = ""
    async for chunk in stream:
        if not chunk.startswith("data: {"):
            if chunk.startswith("data: [DONE]") and held:
                yield _sse({"content": scrub(held, looks)})
                held = ""
            yield chunk
            continue
        obj = json.loads(chunk[6:])
        if "content" not in obj:
            yield chunk
            continue
        held += obj["content"]
        m = re.search(r"[^\w@]+(?=\w*$)", held)
        if not m:
            continue
        out, held = held[:m.end()], held[m.end():]
        yield _sse({"content": scrub(out, looks)})
    if held:
        yield _sse({"content": scrub(held, looks)})


async def _keepalive(stream, every: float = 15.0):
    """Send an SSE comment right away and after every `every` seconds of
    silence. Cloudflare drops a request that has sent no bytes for 100 s
    (524, no CORS headers), and a queued Ollama request can sit that long
    before its first token. picky.js skips non-`data:` frames."""
    yield ": ping\n\n"
    it = stream.__aiter__()
    nxt = asyncio.ensure_future(it.__anext__())
    try:
        while True:
            done, _ = await asyncio.wait({nxt}, timeout=every)
            if not done:
                yield ": ping\n\n"
                continue
            try:
                chunk = nxt.result()
            except StopAsyncIteration:
                return
            yield chunk
            nxt = asyncio.ensure_future(it.__anext__())
    finally:
        if not nxt.done():
            nxt.cancel()  # client went away: closes the Ollama stream too
            try:
                await nxt
            except BaseException:  # noqa: BLE001
                pass


async def _with_opener(opener: str | None, stream):
    """Stream `opener` as its own paragraph ahead of the reply."""
    if opener:
        yield _sse({"content": opener + "\n\n"})
    async for chunk in stream:
        yield chunk


# -----------------------------------------------------------------------------
# image generation: handed to image.py on :8767 (own GPU lock + 20/hr limit)
# -----------------------------------------------------------------------------

# "draw me a bulldog", "make a picture of ...", "generate an image of ..."
_IMAGE_ASK = re.compile(
    r"^\s*(draw|sketch|paint)\b"
    r"|\b(draw|sketch|paint|generate|make|create|render|show)\b.{0,30}?"
    r"\b(image|picture|pic|drawing|painting|illustration|photo|meme|poster|wallpaper)s?\b",
    re.I,
)


def wants_image(text: str) -> bool:
    return bool(_IMAGE_ASK.search(text or ""))


async def _image_stream(prompt: str, user_id: str):
    """SSE: a one-liner, keep-alive pings while the GPU works, then the image
    as a data: URL. The prompt is only the user's own words; no league
    context goes to the image service."""
    yield _sse({"content": "Fine, dumbass. Firing up the art department. Give me a minute or two..."})
    timeout = httpx.Timeout(connect=5.0, read=300.0, write=10.0, pool=5.0)
    async with httpx.AsyncClient(timeout=timeout) as c:
        # 16 GB box: the chat model (~6.7 GB) and the image model (~6 GB)
        # together swap, and a picture took 4+ min and timed out. Unload the
        # chat model first; the next chat message reloads it.
        try:
            await c.post(f"{OLLAMA}/api/generate", json={"model": MODEL, "keep_alive": 0}, timeout=30.0)
        except httpx.HTTPError as e:
            log.warning("couldn't unload %s before image: %s", MODEL, e)
        task = asyncio.create_task(
            c.post(f"{IMAGE_URL}/generate", json={"prompt": prompt, "user_id": user_id, "width": 768, "height": 768})
        )
        try:
            while not task.done():
                await asyncio.wait({task}, timeout=10)
                if not task.done():
                    yield ": ping\n\n"  # keep the tunnel from idling out
            r = task.result()
        except httpx.HTTPError as e:
            log.warning("image service unreachable: %s", e)
            yield _sse({"content": " Art department's closed, Picasso. Try me later."})
            return
        finally:
            task.cancel()
    if r.status_code == 429:
        yield _sse({"content": " Twenty pictures an hour is the limit and you blew right through it. Go the fuck outside."})
    elif r.status_code != 200:
        log.error("image %s: %s", r.status_code, r.text[:200])
        yield _sse({"content": " Shit, the brush slipped. My fault, for once. Try again in a minute."})
    else:
        yield _sse({"image": "data:image/png;base64," + r.json()["image_base64"]})
    yield "data: [DONE]\n\n"


# -----------------------------------------------------------------------------
# routes
# -----------------------------------------------------------------------------


@app.get("/health")
async def health():
    return {"ok": True, "model": CLAUDE_MODEL if CLAUDE else MODEL,
            "fallback": MODEL if CLAUDE else None,
            "gate": {"members_active": GATE.members(), "test_busy": GATE.test_busy, **GATE.stats}}


@app.post("/chat")
async def chat(req: Request):
    # a member request preempts test traffic the moment it arrives, before
    # auth and the Supabase reads, so Ollama is free by the time it's ready
    test = is_test_request(req)
    hold = None if test else GATE.member_in()
    try:
        return await _chat(req, test, hold)
    except BaseException:
        if hold is not None:
            GATE.member_out(hold)
        raise


async def _chat(req: Request, test: bool, hold: int | None):
    body = await req.json()
    messages = body.get("messages") or []
    jwt = body.get("supabase_jwt") or ""
    mode = body.get("mode") or "leaderboard"
    if mode not in ("leaderboard", "picks"):
        mode = "leaderboard"
    rival_predictions = body.get("rival_predictions") or None
    if rival_predictions is not None and not isinstance(rival_predictions, dict):
        rival_predictions = None
    if not jwt:
        raise HTTPException(status_code=401, detail="missing_token")
    if not isinstance(messages, list) or not messages:
        raise HTTPException(status_code=400, detail="messages required")
    # cap history on the server as well — the frontend already trims, this is
    # belt-and-suspenders against a crafted payload.
    messages = messages[-MAX_HISTORY_TURNS * 2 :]

    async with httpx.AsyncClient() as client:
        user_id = await _verify_jwt(client, jwt)
        _rate_check(user_id)
        opener = land_ack_for(user_id, messages)
        last = messages[-1] if isinstance(messages[-1], dict) else {}
        if last.get("role") == "user" and wants_image(last.get("content", "")):
            return StreamingResponse(
                _gated(test, hold, lambda: _with_opener(opener, _image_stream(last["content"][:2000], user_id))),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )
        try:
            ctx = await _build_context(
                client, jwt, user_id, mode=mode, rival_predictions=rival_predictions
            )
        except httpx.HTTPError as e:
            log.warning("supabase fetch failed: %s", e)
            raise HTTPException(status_code=502, detail="supabase_unreachable")
        if not ctx.get("error"):
            ctx["picky_player"], _ = await asyncio.gather(
                picky_player_facts(client, jwt, ctx), load_live(client, jwt, ctx))
    if not ctx.get("error"):
        ctx["espn"] = await espn.game_notes([g["id"] for g in ctx["games_this_week"]])
    # (re)load the CFBD cache off the event loop; ~3 s only after a refresh
    await asyncio.to_thread(_cfbd_index)
    ctx["daddyfreeman"] = user_id == DADDYFREEMAN_USER_ID

    prefix = prompt_prefix(ctx)
    sys_msg = {"role": "system", "content": prefix + _system_tail(ctx)}
    convo = [sys_msg] + [
        {"role": m["role"], "content": m["content"]}
        for m in messages
        if m.get("role") in ("user", "assistant") and m.get("content")
    ]
    convo = [convo[0]] + [
        {"role": "user", "content": _user_turn(ctx, m["content"])} if m["role"] == "user"
        else {"role": "assistant", "content": strip_land_ack(m["content"])}
        for m in convo[1:]
    ]

    # a game outside the pool, 'what's on', upsets: the latest question only
    note = live_note(ctx, last.get("content", "")) if last.get("role") == "user" else ""
    if note and convo[-1]["role"] == "user":
        # right before the question, after the other riders: the 9B obeys what's nearest
        body, q = convo[-1]["content"], last.get("content", "")
        body = (body[: -len(q)] + note + "\n\n" + q) if q and body.endswith(q) else (note + "\n\n" + body)
        convo[-1] = {"role": "user", "content": body}

    # per-pick WIN/LOSS lines for the latest question, nearest the question;
    # the answer is then checked against them before it goes out
    facts = results_facts(ctx, last.get("content", "")) if last.get("role") == "user" and not ctx.get("error") else None
    if facts and convo[-1]["role"] == "user":
        body, q = convo[-1]["content"], last.get("content", "")
        rb = results_block(facts)
        body = (body[: -len(q)] + rb + "\n\n" + q) if q and body.endswith(q) else (rb + "\n\n" + body)
        convo[-1] = {"role": "user", "content": body}

    def _answer():
        return _guarded_stream(convo, prefix, facts) if facts else _model_stream(convo, prefix)

    return StreamingResponse(
        _keepalive(_gated(test, hold, lambda: _with_opener(
            opener, _scrubbed(_answer(), looks_bait(last.get("content", "")))))),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.exception_handler(HTTPException)
async def _http_error(_req: Request, exc: HTTPException):
    return JSONResponse(
        status_code=exc.status_code, content={"error": exc.detail}
    )


# -----------------------------------------------------------------------------
# startup: warm the model so first user doesn't eat the cold start
# -----------------------------------------------------------------------------


@app.on_event("startup")
async def _warm():
    asyncio.create_task(asyncio.to_thread(_cfbd_index))
    if CLAUDE:  # Ollama is only the fallback now; it loads on demand
        return

    async def _go():
        try:
            async with httpx.AsyncClient(timeout=60.0) as c:
                r = await c.post(
                    f"{OLLAMA}/api/chat",
                    json={
                        "model": MODEL,
                        "messages": [{"role": "user", "content": "ready?"}],
                        "stream": False,
                        "options": OLLAMA_OPTIONS,
                        "keep_alive": OLLAMA_KEEP_ALIVE,
                        **({"think": OLLAMA_THINK == "true"} if OLLAMA_THINK is not None else {}),
                    },
                )
                log.info("warmup %s model=%s", r.status_code, MODEL)
        except Exception as e:  # noqa: BLE001
            log.warning("warmup failed: %s", e)

    asyncio.create_task(_go())


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "chat:app",
        host="127.0.0.1",
        port=int(os.environ.get("PORT", "8766")),
        log_level="info",
    )
