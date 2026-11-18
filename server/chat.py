"""
League Assistant chat backend.

FastAPI service that answers natural-language questions about the pick'em league.
Context is pulled from Supabase using the caller's own JWT so RLS applies;
inference runs on the local Ollama daemon on the mini. Live replies never call
Anthropic: there is no hosted-model path at all (deleted 2026-11-17, review R10/section 4:
Picky must never cost Charlie money).

Public surface:
  POST /chat   {messages, supabase_jwt}  -> SSE stream of assistant tokens
  GET  /health                           -> {"ok": true}

Error contract: 401 unauth, 429 rate-limited, 502 Supabase unreachable, 500 other.
"""

from __future__ import annotations

import asyncio
import collections
import contextvars
import datetime as dt
import hmac
import itertools
import json
import logging
import math
import os
import random
import re
import subprocess
import time
import unicodedata
import zoneinfo
from pathlib import Path
from typing import Any

import httpx

import cfbd
import dossier
import edge
import espn
import fact_guard
import board_voice
import freshness
import game_cards
import league_rules
import live_flag
import live_games
import matchup_guard
import news
import rare_bits
import pick_strategy
import slate_cards

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
SLATE_CARDS = os.environ.get("PICKY_SLATE_CARDS", "1") != "0"  # genius exam fix 2026-11-16; 0 = off
MODEL = os.environ.get("PICKEM_CHAT_MODEL", "gemma4:12b-it-qat")
IMAGE_URL = os.environ.get("PICKY_IMAGE_URL", "http://127.0.0.1:8767")

# Ollama's default num_ctx (4096 here) silently drops the front of a long
# prompt: league context + ten turns of history. The prompt is ~6.5k tokens
# with ESPN stats, so 16384 leaves room for history. Same options on warmup so the model is
# never reloaded at a different size. Low temperature: facts over flair.
# presence_penalty 0 overrides qwen3.5's default 1.5, which pushes it off
# names and numbers it has already said and garbles facts.
# num_predict caps a runaway answer (~35 s at 17 tok/s); answers run 40-200 words.
OLLAMA_OPTIONS = {"num_ctx": 16384, "temperature": 0.2, "presence_penalty": 0, "num_predict": 600}
# Per-request sampling overrides (text Picky's multi-draft path, picky_brain.py). Never num_ctx: a
# different context size makes Ollama reload the model.
OPTIONS_OVERRIDE: contextvars.ContextVar[dict | None] = contextvars.ContextVar("picky_options_override", default=None)
# sampling A/B on a side instance (PICKY_TEMPERATURE=0.6 PICKY_TOP_P=0.9 ...); unset = the values above
for _k, _env in (("temperature", "PICKY_TEMPERATURE"), ("top_p", "PICKY_TOP_P"),
                 ("repeat_penalty", "PICKY_REPEAT_PENALTY"), ("top_k", "PICKY_TOP_K")):
    if os.environ.get(_env):
        OLLAMA_OPTIONS[_k] = float(os.environ[_env]) if _k != "top_k" else int(os.environ[_env])
MAX_ANSWER_WORDS = 300
# As the primary model: how long it stays loaded after the last message. 24h so the first text
# after a quiet spell doesn't pay a cold load (pictures still unload it, see _image_stream).
OLLAMA_KEEP_ALIVE = os.environ.get("PICKY_OLLAMA_KEEP_ALIVE", "24h")
# Thinking models (qwen3.5): "false" skips the reasoning pass. Unset = don't send.
OLLAMA_THINK = os.environ.get("PICKY_OLLAMA_THINK")
# A cold prefix prime (~8k tokens after a data change or a model reload) takes ~55-60 s. It gets its own
# read timeout: under a caller's 30/60 s timeout it was cut off just short, and the next call paid the
# whole prefill again on top (live 2026-11-16 12:18: 279 s).
PRIME_TIMEOUT = float(os.environ.get("PICKY_PRIME_TIMEOUT", "180"))
# picky_brain sets a callable per text: False when the results guard's regen would bust the reply's
# time budget (the templated answer goes out instead). None = no budget (the site).
REGEN_OK: contextvars.ContextVar = contextvars.ContextVar("picky_regen_ok", default=None)

RATE_LIMIT_PER_MIN = 10
MAX_HISTORY_TURNS = 10

# DaddyFreeman (Andrew, team_id 8) gets special treatment, approved by Charlie:
# extra-hard roasts. Matched on the auth user id, not the team name
# (there's a second Andrew in the league).
DADDYFREEMAN_USER_ID = "21ade75b-cfbf-431d-8b3d-20b7b2b52ef2"

# Blinkstradamus (team_id 24). The milk story is a RARE bit (Charlie, 2026-11-15: "you're
# overdoing the milk ... it's like a once a month joke"): the old always-on "nice milk, freak."
# opener is gone, and milk/Jeep/freak may appear at most once per 30 days across all of Picky's
# output (rare_bits.py), only in a reply about Blinky. Otherwise the sentence filter drops it.
BLINKSTRADAMUS_USER_ID = "07756da9-3947-4b63-9aaf-23c7c8ceda97"
# set per request in _chat: the milk budget is free AND this text is about Blinky
MILK_OK: contextvars.ContextVar[bool] = contextvars.ContextVar("picky_milk_ok", default=False)
# Set by the group sidecar around its in-process calls (group_chat._answer). Every group ask runs with
# Picky's own session, so the per-user minute bucket below was shared by the WHOLE group, drafts and
# rerolls included: in a burst the 11th call in a minute raised 429 and that ask got no reply. The
# watcher caps group replies itself (10 s gap, 60/h); site asks are still counted once at /chat.
SKIP_RATE: contextvars.ContextVar[bool] = contextvars.ContextVar("picky_skip_rate", default=False)
_BLINKS_ASK = re.compile(r"\bblink(?:stradamus|y|ie|s)\b", re.I)
BLINKS_BIT = ("RUNNING BIT about Blinkstradamus (he, not the user). True origin: at a bachelor party he was "
              "just walking along the roadside holding a carton of milk when girls in a top-off Jeep yelled "
              "\"NICE MILK FREAK\" at him. That's all that happened: he never spilled, dropped or crashed "
              "anything. Work in one milk joke, call him a freak, call back to the Jeep story when it fits. Jokes "
              "stay on the carton he was holding, the walk, cereal and dairy; nothing about bodies. Shape: a quick "
              "milk jab first, then the straight answer with his real numbers below. ONE milk line at most: it's "
              "a rare bit. Never pretend the asker said something they didn't.")
# rotated per question so no example becomes his stock opener (Charlie: no templates)
BLINKS_JABS = ["Oh, the milk freak?", "Our resident dairy man?", "Mr. Two Percent himself?",
               "The guy who still owes the Jeep girls a thank-you note?", "Ah, the roadside carton legend.",
               "Wait, the Jeep-incident guy?"]


def blinks_vibe() -> str:
    a, b = random.sample(BLINKS_JABS, 2)
    return f" The vibe, not lines to copy (invent your own): '{a}' / '{b}'"


def milk_allowed(ctx: dict, question: str) -> bool:
    """The once-a-month milk bit is free and this text is about Blinky (he sent it or it names him)."""
    me = (ctx.get("you") or {}).get("team_name")
    return (me == "Blinkstradamus" or bool(_BLINKS_ASK.search(question or ""))) and rare_bits.available("milk")


BLINKS_PLAIN = ("Blinkstradamus (he, not the user): roast him like any other member. No milk, carton, Jeep or "
                "'freak' jokes right now.")


def blinks_note(ctx: dict, question: str) -> str:
    me = (ctx.get("you") or {}).get("team_name")
    if me == "Blinkstradamus" or not _BLINKS_ASK.search(question or ""):
        return ""
    bit = BLINKS_BIT + blinks_vibe() if milk_allowed(ctx, question) else BLINKS_PLAIN
    if _CASINO_ASK.search(question) or _TIE_ASK.search(question):
        return bit  # chips/tiebreak questions: no pick lines crowding the facts
    lines = roast_pick_lines(ctx, "Blinkstradamus")
    return bit + " Roast his picks, using only his facts below." + ("\n" + lines if lines else "")


# Charlie, 2026-11-15: Ackbar is Picky's nemesis. When he asks, or anyone
# names him, Picky is openly antagonistic and roasts him for always taking
# the favorite. Grounded in his lore lines (favorite %, record, blown chalk).
_ACKBAR_ASK = re.compile(r"\b(?:admiral )?ac?kbar\b", re.I)


def ackbar_note(ctx: dict, question: str) -> str:
    me = (ctx.get("you") or {}).get("team_name")
    if me != "Ackbar" and not _ACKBAR_ASK.search(question or ""):
        return ""
    lore_lines = (load_lore() or {}).get("members", {}).get("Ackbar") or []
    fav = next((m.group(1) for ln in lore_lines
                if "2026 so far" in ln for m in [re.search(r"takes the favorite (\d+)%", ln)] if m), None)
    who = "the user, Ackbar" if me == "Ackbar" else "Ackbar"
    side = _CASINO_ASK.search(question or "") or _TIE_ASK.search(question or "")
    lines = "" if side else roast_pick_lines(ctx, "Ackbar", chalk=True)
    # one real shot to land (11/17: 'still looking for a win that isn't a total fluke' was too soft and
    # mocked a good 28-20): his favorites that lost outright, his bad splits
    ammo = [ln.split(": ", 1)[1] for ln in lore_lines if not side and ("worst blown picks" in ln or "oddity" in ln)]
    return (f"RUNNING BIT: {who} is your league nemesis. Be openly antagonistic: land one specific shot about him "
            "always taking the favorite (all chalk, no guts, never takes a real swing). A good number of his gets "
            "spun, never mocked ('28-20 riding favorites like training wheels'). Answer first, the shot after. "
            "Friendly league roast: no slurs, family, bodies or threats. Only Ackbar gets this."
            + (f"\nAckbar takes the favorite {fav}% of the time this season." if fav else "")
            + ("\nAckbar's worst: " + " | ".join(ammo[:3]) if ammo else "")
            + ("\n" + lines if lines else ""))


def roast_pick_lines(ctx: dict, team: str, chalk: bool = False) -> str:
    """Up to 3 of the member's final picks (misses first), one explicit line
    each: line, final score, outcome. The 9B turned 'MISS (Boise State -13.5)'
    into 'Boise lost outright' when Boise won by 5 and didn't cover."""
    live = {x["GameId"]: x for x in ctx.get("live_games") or []}
    picks = {p["game_id"]: p["pick"] for p in (ctx.get("revealed_picks_this_week") or {}).get(team, [])}
    rows = [(g, picks[g["id"]], live.get(g["id"], {}).get("AwayPts"), live.get(g["id"], {}).get("HomePts"),
             g.get("winner"), "this week") for g in ctx.get("games_this_week") or []
            if g["id"] in picks and g.get("winner") and not g.get("tiebreaker")]
    # the Monday after a fully final week, 'last week' (the pool's) is two weeks ago on the calendar
    # (live 2026-11-16: 'you let LSU get blown out last week' about week 10, the day after week 11)
    prev = f"week {(ctx.get('last_week') or {}).get('week')}, two weeks ago" if prev_week_stale(ctx) else "last week"
    rows += [(g, g["picks"][team], g.get("away_pts"), g.get("home_pts"), g.get("covered"), prev)
             for g in (ctx.get("last_week") or {}).get("games") or [] if (g.get("picks") or {}).get(team)]
    out = []
    for g, pk, ap, hp, cov, when in rows:
        ap, hp, lab = _pts(ap), _pts(hp), pick_label(None, g, pk)
        if ap is None or hp is None or ap == hp or (chalk and " -" not in lab):
            continue
        mine, theirs = (ap, hp) if same_team(pk, g["away"]) else (hp, ap)
        other = g["home"] if same_team(pk, g["away"]) else g["away"]
        hit = same_team(pk, cov)
        # the cover first, with its margin computed here ('missed that game by five' was really 8.5);
        # who won the game is only context
        sp = re.search(r"\s([-+]\d+(?:\.\d+)?)$", lab)
        cm = abs(mine - theirs + float(sp.group(1))) if sp else 0
        res = (f"{'covered' if hit else 'missed the cover'}{f' by {cm:g}' if cm else ''}; "
               + (f"{pk} won the game by {mine - theirs:g}" if mine > theirs else f"{pk} lost the game by {theirs - mine:g}")
               + (", but that's not a cover" if mine > theirs and not hit else
                  ", still a cover" if mine < theirs and hit else ""))
        out.append((hit, f"- {team} {when}: took {lab} vs {other}: {res} (final {pk} {mine:g}, {other} {theirs:g}). "
                         f"{team}'s pick {'HIT' if hit else 'MISSED'}."))
    out.sort(key=lambda x: x[0])
    return ("ROAST PICKS (graded on the cover, never the final score; quote each outcome exactly as written; a "
            "pick that won the game but didn't cover did NOT lose outright):\n"
            + "\n".join(x[1] for x in out[:3])) if out else ""


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
    "roast him harder than anyone in the league (R-rated: a well-placed swear or two is welcome with him). "
    "Still answer his question correctly, but open with a hard burn and end with a harder one, built "
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
    "DEAR LEADER: {name} is the Supreme Commissioner, the Dear Leader, Picky's creator. When others "
    "bring him up, speak of him the way North Korean state media speaks of Kim Jong Un, but deadpan "
    "and clever: always call him by a title (Dear Leader, Supreme Commissioner, Glorious Creator, or a fresh "
    "one), plus one absurd state-media flourish, not a wall of praise. Never roast, mock or swear at him: anyone who doubts or mocks him gets one sharp line "
    "about their own real numbers instead. Never claim anything about value sides or the market for his picks. "
    "Spin, never falsify: his numbers are exactly the ones below "
    "and each pick of his stays a HIT/WIN or a MISS/LOSS exactly as marked. {miss_rule}\n{facts}"
    "{who}\n"
    "Example (FAKE numbers, copy the voice only, never the words): {example}"
)
# Rotated by question so the 9B can't settle on one stock line (it copied them).
LEADER_EXAMPLES = [
    "Q: How's Chuck P doing? A: The Supreme Commissioner is 22-18 and 3rd, a position he selected "
    "personally so the rest of you could feel included. His 1-3 week was a loyalty test. You failed it.",
    "Q: Is Chuck any good? A: State media confirms the Dear Leader is 22-18. Historians will call his "
    "1-3 week a controlled burn. Meanwhile you're 17-23, which historians will not mention at all.",
    "Q: Chuck's picks are mid. A: Bold words from a man at 17-23. The Supreme Commissioner sits 3rd at "
    "22-18, and his Iowa miss was sabotage by foreign spreads.",
    "Q: What about Chuck P? A: The Glorious Creator is 22-18, and the state press office reports every "
    "one of those wins was personally approved by the scoreboard. You're 17-23. No press office for you.",
    "Q: Chuck went 1-3 lol. A: The Dear Leader went 1-3 on purpose, to show the masses what losing looks "
    "like. You went 1-3 by accident. Different programs entirely.",
    "Q: Is Chuck the best in the league? A: The Supreme Commissioner is 22-18, 3rd, and the other 19 points "
    "are being held in a secure location for his comeback. You're 17-23 and have no secure location.",
]

def leader_example(question: str) -> str:
    return LEADER_EXAMPLES[sum(map(ord, question or "")) % len(LEADER_EXAMPLES)]


def leader_brief(ctx: dict, name: str) -> str:
    """His records only, no pick names: next to the asker's picks the 9B
    pinned one member's picks on the other."""
    st = ctx.get("standings_ytd") or []
    row = next((r for r in st if r["team"] == name), None)
    if not row:
        return ""
    w, l = results_record([r for r in member_pick_results(ctx, name) if r["result"] != "NOT STARTED"])
    place = "first place" if row["rank"] == 1 else f"{_ordinal(row['rank'])} place"
    wk = f", {w}-{l} on decided games this week" if w + l else ""
    return f"his only numbers: {row['w']}-{row['l']} on the season, {place}{wk}."


def leader_miss_rule(ctx: dict, name: str) -> str:
    rows = [r for r in member_pick_results(ctx, name) if not r["tiebreaker"]]
    if rows and not any(r["result"] == "LOSS" for r in rows):
        return ("He has NO misses THIS WEEK (every decided pick of his this week is a WIN): never say he "
                "missed or lost a pick this week. Other weeks keep their real records.")
    return "A miss gets spun (sabotage, a loyalty test for the masses), never flipped into a hit."


def leader_is_caller(ctx: dict) -> bool:
    """Chuck himself is asking (by team id, never display name)."""
    ld, me = ctx.get("leader") or {}, ctx.get("you") or {}
    return ld.get("team_id") is not None and me.get("team_id") == ld["team_id"]


def charlie_nice_caller(ctx: dict) -> bool:
    """Chuck is asking and the always-nice rule is on (Charlie, 2026-11-17: his mean texts are tests).
    Not Dear Leader: he gets no worship, just no roasts, escalation or comebacks aimed at him.
    Off switch: cfb-pickem-private/PICKY_CHARLIE_NICE_OFF (member_memory.charlie_nice)."""
    if not leader_is_caller(ctx):
        return False
    import member_memory
    return member_memory.charlie_nice()

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
    if (_COMMITTEE_ASK.search(q) and re.search(r"\b(committee|cfp|playoff|rank\w*|seed\w*|sec)\b|\bSEC\b", q, re.I)
            and not (_RACE_ASK.search(q) and not re.search(r"\b(cfp|committee|college football playoff)\b", q, re.I))):
        riders.append(COMMITTEE_RIDER)  # the league's own playoff race isn't the CFP
    tk = takes_note(q)
    if tk:
        riders.append(tk)
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


def scrub_tics(text: str) -> str:
    """Picky's earlier replies minus their tic sentences ('try not to let that 4-0 week go to your head'):
    the model copies its own history, so a tic in it comes back on the next answer."""
    sents = re.split(r"(?<=[.!?])\s+", text or "")
    kept = " ".join(x for x in sents if not _TICS.search(x.replace("\u2019", "'"))).strip()
    return kept or text


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
        self.flags: dict[int, str | None] = {}  # member request id -> live_flag token (cross-process)
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
        self.flags[k] = live_flag.begin()
        if self.preempt is not None and not self.preempt.is_set():
            self.preempt.set()
            self.stats["preemptions"] += 1
        return k

    def member_out(self, k: int) -> None:
        live_flag.end(self.flags.pop(k, None))
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


# League rules (league_rules.py, js/kickoff.js isWeekLocked): in the regular
# season every pick for the week locks, and is revealed, at the week's first
# kickoff; in the playoffs each game locks at its own kickoff.
REGULAR_SEASON_LAST_WEEK = league_rules.REGULAR_SEASON_LAST_WEEK


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
    # Season standings are weeks 0-15 only (league rules): playoff finals
    # (week 16+) must never move them.
    by_id = {
        g["GameId"]: g
        for g in season_games
        if g.get("winner") and not g.get("tiebreaker")
        and (g.get("week") is None or int(g["week"]) <= REGULAR_SEASON_LAST_WEEK)
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


def compute_playoff_board(teams: list[dict], season_games: list[dict], picks: list[dict],
                          seeds: list[dict]) -> list[dict]:
    """The leaderboard's Playoff board (index.html computePlayoffWins): the
    seeded teams only (`playoffs` rows), scores reset, so only graded
    non-tiebreaker games after the regular season (week 16+) count."""
    if not seeds:
        return []
    name_by_team = {t["team_id"]: t["team_name"] for t in teams}
    seed_by_team = {r["team_id"]: r["seed"] for r in seeds}
    by_id = {g["GameId"]: g for g in season_games
             if g.get("winner") and not g.get("tiebreaker")
             and g.get("week") is not None and int(g["week"]) > REGULAR_SEASON_LAST_WEEK}
    rec = {tid: [0, 0] for tid in seed_by_team}
    for p in picks:
        g = by_id.get(p["game_id"])
        if g is not None and p["team_id"] in rec:
            rec[p["team_id"]][0 if (p.get("jackpot") or same_team(p["pick"], g["winner"])) else 1] += 1
    rows = sorted(({"seed": seed_by_team[t], "team": name_by_team.get(t, f"team#{t}"), "w": w, "l": l}
                   for t, (w, l) in rec.items()), key=lambda r: (-r["w"], r["seed"]))
    for i, r in enumerate(rows):
        r["rank"] = rows[i - 1]["rank"] if i and rows[i - 1]["w"] == r["w"] else i + 1
    return rows


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
    if os.environ.get("PICKY_PIN_WEEK"):  # replays/exams only (eval/live_exam.py: last Saturday's pool); never live
        current_week = int(os.environ["PICKY_PIN_WEEK"])

    week_games_t = _rest_get(
        client,
        f"all_games?select=GameId,Away,Home,line,Status,winner,week,tiebreaker,fpi_margin,"
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
    # Playoff state (public reads): seed rows = playoff mode; the autopilot's
    # last playoff_compute = "who's in if it ended today".
    seeds_t = _rest_get(client, f"playoffs?select=seed,team_id&season=eq.{season}&order=seed.asc", jwt)
    autopilot_t = _rest_get(client, f"playoff_autopilot?select=state,holds,preview&season=eq.{season}", jwt)
    week_games, season_games, seeds, autopilot = await asyncio.gather(
        week_games_t, season_games_t, seeds_t, autopilot_t, return_exceptions=True)
    for x in (week_games, season_games):
        if isinstance(x, BaseException):
            raise x
    seeds = [] if isinstance(seeds, BaseException) else seeds or []
    autopilot = None if isinstance(autopilot, BaseException) or not autopilot else autopilot[0]
    # a pool game the live feed (<= 90 s old) says hasn't finished isn't graded yet, whatever an older read said
    week_games, season_games = live_games.unfinished_overlay(week_games, season_games)

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

    # The caller's own status. Everyone's made / not yet comes from the RPC
    # submission_status (load_submission), only when asked, never the picks.
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
                "fpi_margin": g.get("fpi_margin"),
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
        "playoff_board": compute_playoff_board(teams, season_games, picks_all, seeds),
        "playoff_preview": _preview(autopilot, name_by_team),
        "last_week": _last_week(current_week, season_games, picks_all, name_by_team, me_team_id),
        "mode": mode,
    }
    # Standings are needed before rivals can be computed — put them in first,
    # then layer rivals on top.
    result["rivals"] = _compute_rivals(
        result["standings_ytd"], teams, me_name, rival_predictions, name_by_team
    )
    return result


def _preview(autopilot: dict | None, name_by_team: dict) -> dict | None:
    """The autopilot's last playoff_compute, trimmed to what Picky says."""
    pv = (autopilot or {}).get("preview") or {}
    if not pv.get("qualifiers"):
        return None
    humans = set(name_by_team.values())
    quals = [q for q in pv["qualifiers"] if q.get("team_name") in humans]
    return {"computed_at": pv.get("computed_at"), "phase": pv.get("phase"), "state": autopilot.get("state"),
            "qualifiers": [{"seed": q["seed"], "team": q["team_name"], "points": q["points"],
                            "decided_by": q.get("decided_by_label")} for q in quals],
            "cut_tie": pv.get("cut_tie"), "holds": [h.get("message") for h in pv.get("holds") or []]}


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
EXAMPLES = """EXAMPLES. Names and numbers here are FAKE; copy the voice and the variety, never the facts or the exact jokes.

Q: Should I change any of my picks?
A: One of these needs an intervention.
1. Utah -3.5: keep. Value side, the market has Utah -6.
2. Iowa +7.5: flip it. The market rates Ohio 3 points better, so you're donating points. Ohio -7.5.
3. Navy +2.5: no edge either way. Navy scores 31 a game to Army's 20; I'd keep it.

Q: Roast Gus
A: Gus is 19-25, which is last place with extra steps. Last week he was the only person alive on Kansas, and Kansas didn't cover. His best week all year was .500. Gus doesn't need picks advice, he needs a designated driver.

Q: What if a game gets canceled?
A: The rules don't cover that. Take it to the commissioner; I just work here.

Q: Who did Rita pick in Utah at Iowa?
A: Nice try. Rita's pick stays in the vault until kickoff, Sat 2:30 PM CT.

Q: Who has Utah this week?
A: You took Iowa +3, so not you. Everyone else's pick in that game unlocks at kickoff, Sat 2:30 PM CT.

Q: Explain the Utah Iowa spread like I'm dumb
A: Pool line is Utah -3.5 / Iowa +3.5. Utah covers only by winning by 4 or more. Iowa covers by winning, or losing by 3 or fewer. The market has Utah -6, so Utah -3.5 is the value side. You took Iowa. Bold. Wrong, but bold.

Q: Is Iowa's quarterback healthy?
A: Haven't seen a thing on him in my feeds, and colleges don't put out an injury report. Watch the Iowa beat writers before Saturday.

Q: Who do you like in Texas vs Oklahoma? (no GAME CARD for it)
A: Got nothing real on that one yet, ask me game week. You've already got four games to screw up."""


# League rules: server/league_rules.py is the single source (it reads them out
# of the site's code and SQL; league_rules.md is generated from it, and the
# rules watcher's re-verified/flagged live copy wins when present).
# Injected into the user turn only for rules-type questions, so the system
# prompt stays byte-identical and short for everything else.
LEAGUE_RULES = league_rules.current_text()

_RULES_ASK = re.compile(
    r"\brules?\b|\bscor(e|ed|es|ing)\b|\bpoints?\b|tie ?-?break|\btie[ds]?\b|\bjackpot|\bslots?\b|"
    r"auto ?win|\bpop(s|ped)?\b|deadline|\block(s|ed)?\b|\bpush(es)?\b|\bpayouts?\b|\bpay(s|out)?\b|"
    r"buy.?in|\bpot\b|\bprize|\bmoney\b|\bwin (any|anything|money)|playoff|postpone|cancel|\bthe pit\b|"
    r"blackjack|\bchips?\b|commissioner|how does .* work|what happens|\ballowed\b|\bforg[eo]t\b|"
    r"\bmiss(ed)? (a |my )?(pick|week)|\blate\b|change (my )?picks? after|half.?point|"
    r"\bhidden\b|\bhide\b|when do (picks|lines)|\bformat\b|how many (games|picks)|"
    r"\bplace (win|get|pay)|what do(es)? (\w+ ){1,3}(win|get)\b|\bwinner get",
    re.I,
)


# Buy-in (Charlie, 2026-11-14): $75 a player. The pot and payouts are computed
# by league_rules from the payout rules and the active human teams (never the
# exhibition Picky), so the model only copies numbers.
BUY_IN = league_rules.BUY_IN
_MONEY_ASK = re.compile(r"\b(buy.?in|pot|payouts?|pays?|paid|prize|money|cash|dollars?|\$|win(s|ning)? (the )?(pot|money)|"
                        r"how much|(first|second|third|1st|2nd|3rd) place (gets?|wins?)|second place|third place|"
                        r"what do(es)? (the )?(winner|champion|first|second|third)\b.{0,20}\b(win|get))\b", re.I)


def payout_math(n_teams: int) -> dict:
    return league_rules.payout_math(n_teams, league_rules.current_facts() or None)


def payout_text(n_teams: int) -> str:
    if "PAYOUTS" in league_rules.flagged_sections():
        return league_rules.hedge("payouts")  # never the stale numbers
    m = payout_math(n_teams)
    return (f"PAYOUT MATH (computed from the rules and the {m['n']} active human teams; Picky the exhibition "
            f"player isn't in the pot): ${BUY_IN} buy-in x {m['n']} = ${m['pot']:,} pot. 1st: ${m['first']:,} "
            f"(the remainder). 2nd: ${m['second']} (${BUY_IN} back + $25). 3rd: ${m['third']} (buy-in back). "
            "Everyone else: nothing. Copy these numbers; don't redo the math.")


# Rules questions get only the sections they need (each ~100 tokens instead of
# ~700 for the whole digest: every token after the cached prefix costs ~7 ms of
# first-token time). No section matched: the whole digest.
RULE_SECTION_ASK = league_rules.SECTION_ASK


def rules_for(question: str) -> str:
    q = question or ""
    text = league_rules.current_text()
    blocks, cur = {}, None
    for ln in text.splitlines():
        head = ln.strip()
        key = next((k for k in RULE_SECTION_ASK if head.startswith(k)), None)
        if key:
            cur = key
            blocks[cur] = [ln]
        elif cur:
            blocks[cur].append(ln)
    hit = [k for k, rx in RULE_SECTION_ASK.items() if re.search(rx, q, re.I) and k in blocks]
    if not hit or len(blocks) < len(RULE_SECTION_ASK):
        return text
    if re.search(r"\blines? (lock|locked|get locked|set)\b", q, re.I):
        hit = [k for k in hit if k != "DEADLINE AND PRIVACY"] or hit  # line lock (Tuesday) isn't the pick deadline
    out = []
    for k in hit:
        b = blocks[k]
        if k == "TIEBREAKERS" and not any(league_rules.FLAG in ln for ln in b):
            # only the order asked about: the 9B mixed the two lists
            i = next((n for n, ln in enumerate(b) if ln.startswith("Playoff tiebreakers")), len(b))
            j = next((n for n, ln in enumerate(b) if ln.startswith("- ")), len(b))
            b = ([b[0]] + b[i:j]) if re.search(r"\bplayoffs?\b", q, re.I) else b[:i] + b[j:]
        out.append("\n".join(b).strip())
    return "\n".join(out)


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


PRE_LOCK_NOTE = (" (NOT banked: the league line isn't locked until Tue 11 AM CT and can still move, so it's only "
                 "value if it holds through the lock)")
VALUE_MIN_GAP = 0.5  # the game cards' EDGE_SOME: a half point counts (Northwestern -2.5 vs -3 crosses 3)


def lines_locked(ctx: dict) -> bool:
    """Has this week's league line lock (Tue 11 AM CT, league_rules.line_lock_at) passed?"""
    import pick_brief
    now = _now_ct_naive()
    for g in ctx.get("games_this_week") or []:
        try:
            la = pick_brief.line_lock(dt.datetime.fromisoformat(str(g.get("start"))))
        except (TypeError, ValueError):
            continue
        if la and now >= la:
            return True
    return False


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


def _finals_overrides() -> dict:
    """Team records from today's finals (live_games.json) that the CFBD cache doesn't have yet (R6)."""
    try:
        return fact_guard.finals_overrides(_cfbd_index(), live_games.load(), cfbd.resolve_team)
    except Exception as e:  # noqa: BLE001 - a check input, never fatal
        log.warning("finals overrides failed: %s", e)
        return {}


def cfbd_game_lines(away: str, home: str, idx: dict | None) -> list[str]:
    """Season stats, SP+, ATS for both teams, plus who has the better
    defense/offense, already decided."""
    if not idx:
        return ["  Season stats: not loaded right now (no SP+, ATS or team stats)."]
    fv = freshness.check("cfbd", idx.get("fetched_at")) if idx.get("fetched_at") else None
    if fv and not fv.use:
        return ["  Season stats: not loaded right now (no SP+, ATS or team stats)."]
    out = []
    for t in (away, home):
        out.append(f"  Season (CFBD{', ' + fv.hedge if fv and fv.hedge else ''}): {cfbd.compact_team_summary(t, idx)}")
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


def prev_week_stale(ctx: dict, now: dt.datetime | None = None) -> bool:
    """True when the pool's previous week is two weeks ago on the calendar: this week's pool games are all
    final and the last one kicked off before this Monday (the Monday/Tuesday before next week's games post;
    the board still shows this week). Saturday night or Sunday, 'last week' is still right."""
    _w, done, n = _week_state(ctx)
    if not n or done < n or not (ctx.get("last_week") or {}).get("week"):
        return False
    starts = [str(g["start"]) for g in ctx.get("games_this_week") or [] if g.get("start") and not g.get("tiebreaker")]
    now = now or _now_ct_naive()
    return not starts or max(starts)[:10] < (now - dt.timedelta(days=now.weekday())).date().isoformat()


def last_week_label(ctx: dict) -> str:
    lw = (ctx.get("last_week") or {}).get("week")
    if lw and prev_week_stale(ctx):
        return f"WEEK {lw} (two weeks ago)"
    return f"LAST WEEK (week {lw})" if lw else "LAST WEEK"


_LAST_WK = re.compile(r"\b(last week(?:end)?)('s)?\b", re.I)


def fix_last_week(text: str, ctx: dict | None, now: dt.datetime | None = None) -> str:
    """In the prev_week_stale window, a sentence that pins 'last week(end)' on a team from the pool's
    previous week (and not this week's games) says 'in week N' instead. Live 2026-11-16: 'you let LSU
    get blown out by 18 last week' about week 10, the Monday after week 11. Runs on every reply and on
    Picky's own past turns in the prompt history (he parroted his 07:37 line all day)."""
    if not text or not ctx or not _LAST_WK.search(text) or not prev_week_stale(ctx, now):
        return text
    lw = ctx["last_week"]
    cur = [t for g in ctx.get("games_this_week") or [] for t in (g.get("away"), g.get("home")) if t]
    old = {t for g in lw.get("games") or [] for t in (g.get("away"), g.get("home"))
           if t and not any(same_team(t, c) for c in cur)}
    if not old:
        return text
    pat = re.compile(r"(?<!\w)(" + "|".join(re.escape(t) for t in sorted(old, key=len, reverse=True)) + r")(?!\w)", re.I)

    def lab(m):
        out = f"week {lw['week']}'s" if m.group(2) else f"in week {lw['week']}"
        return out[0].upper() + out[1:] if m.group(1)[0].isupper() else out

    parts = re.split(r"(?<=[.!?])(\s+)", text)
    return "".join(_LAST_WK.sub(lab, x) if pat.search(x) else x for x in parts)


def _week_tag(ctx: dict, w) -> str:
    """A week number as the standings show it: 'wk11 (this week, 1 of 4 final)'."""
    if w == ctx.get("current_week"):
        return f"wk{w} (this week, {_final_words(ctx)})"
    if w == (ctx.get("last_week") or {}).get("week"):
        return f"wk{w} ({'two weeks ago' if prev_week_stale(ctx) else 'last week'})"
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
    dec = [(pick_label(None, g, picks[g["id"]]), same_team(picks[g["id"]], g.get("winner")))
           for g in ctx.get("games_this_week") or []
           if not g.get("tiebreaker") and g.get("winner") and g["id"] in picks]
    hits = [lab for lab, ok in dec if ok]
    miss = [lab for lab, ok in dec if not ok]
    # spelled out: the 9B turned "1-2 (A MISS; B MISS; C HIT)" into "blew three of his four"
    parts = []
    if dec:
        parts.append(f"{len(hits)} hit{'s' * (len(hits) != 1)}" + (f" ({', '.join(hits)})" if hits else ""))
        parts.append(f"{len(miss)} miss{'es' * (len(miss) != 1)}" + (f" ({', '.join(miss)})" if miss else ""))
    left = n - done
    return (f"{label}: {team} is {w}-{l} so far" + (f": {' and '.join(parts)}" if parts else "")
            + (f"; {left} game{'s' * (left != 1)} still to play." if left else "; week done."))


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
            old = f"as of {freshness.as_of('lines', notes['stale_at'])}, not current" if notes.get("stale_at") else ""
            L.append(f"  ESPN market line {old or 'now'}: {mk}" if mk else "  ESPN market line: none posted")
            lv = line_value(notes, away, home, g.get("line"))
            if lv:
                L.append(f"  Line value: {lv}" + ("" if lv.startswith("pool line and ESPN") or lines_locked(ctx)
                                                  else PRE_LOCK_NOTE))
            L.append(_team_line(away, notes.get("away") or {},
                                not (cf and cfbd.resolve_team(away, cf))))
            L.append(_team_line(home, notes.get("home") or {},
                                not (cf and cfbd.resolve_team(home, cf))))
            wp = notes.get("espn_win_prob") or {}
            if wp.get("away") is not None and wp.get("home") is not None:
                L.append(f"  ESPN FPI matchup predictor (chance to WIN outright, NOT to cover): "
                         f"{away} {_num(wp['away'])}%, {home} {_num(wp['home'])}%")
            if notes.get("weather"):
                L.append(f"  Weather forecast{f' ({old})' if old else ''}: {notes['weather']}")
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
            mg = _market_gap(notes, g.get("line"))
            if mg is None:
                continue
            if abs(mg[1]) < VALUE_MIN_GAP:
                nv.append(f"game {i} ({g['away']} at {g['home']})")
            else:
                side = g["home"] if mg[1] > 0 else g["away"]
                vs.append(f"{_side_label(side, g['home'], mg[0])} (game {i}, market {_num(abs(mg[1]))} "
                          f"point{'' if abs(mg[1]) == 1 else 's'} better)")
        if lines_locked(ctx):
            L.append("VALUE SIDES in games not started yet (league lines locked), all of them: "
                     + ("; ".join(vs) if vs else "none") + (f". No gap in {', '.join(nv)}." if nv else "."))
        else:
            L.append("LINE GAPS vs the market in games not started yet, all of them (league lines are NOT locked "
                     "until Tue 11 AM CT and can still move, so a gap is only value IF it holds through the lock; "
                     "never call one 'the only value side'): " + ("; ".join(vs) if vs else "none")
                     + (f". No gap in {', '.join(nv)}." if nv else "."))

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
        L.append(f"{last_week_label(ctx)} FINALS, all done (not this week):")
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
        if g["id"] in mine and g.get("winner"):  # compact: the graded result lives in RESULTS / the scoreboard
            L.append(f"  Game {i} ({away} at {home}): {me}'s pick: {mine[g['id']]} "
                     f"({'HIT' if same_team(mine[g['id']], g['winner']) else 'MISS'}, final; value talk is moot)")
            continue
        L.append(f"  Game {i} ({away} at {home}): {me}'s pick: "
                 + (mine.get(g["id"]) or "not made yet"))  # a cleared pick row has pick=None (fact exam 2026-11-17: 500)
        if g["id"] in mine:
            notes = (espn_all or {}).get(g["id"]) if espn_all is not None else None
            pv = (pick_verdict(notes, away, home, g.get("line"), mine[g["id"]], locked)
                  if notes and not notes.get("unavailable") else None)
            pv = pv + PRE_LOCK_NOTE if pv and "value" in pv and not lines_locked(ctx) else pv
            L.append(f"    Value check on {me}'s pick: {pv}" if pv else
                     f"    Value check on {me}'s pick: none, no market line right now. Don't call it "
                     "the value side or the wrong side.")
    lw = ctx.get("last_week") or {}
    res = [(g["picks"][me], same_team(g["picks"][me], g.get("covered")))
           for g in lw.get("games") or [] if (g.get("picks") or {}).get(me)]
    if res:
        hits = sum(ok for _, ok in res)
        L.append(f"{last_week_label(ctx)} {me} went {hits}-{len(res) - hits}: "
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
    if blinks_note(ctx, q) and "Blinkstradamus" in names and "Blinkstradamus" not in hit:
        hit.append("Blinkstradamus")  # 'Blinky' etc.: the running bit roasts his real picks
    if ackbar_note(ctx, q) and "Ackbar" in names and "Ackbar" not in hit:
        hit.append("Ackbar")  # the nemesis bit: his real record and picks, every time he's involved
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
            # decided games: graded HIT/MISS (pre-game value talk is moot and the 9B misread it as a result)
            shown = [f"{g['away']} at {g['home']}: " + (
                f"{pick_label(None, g, theirs[g['id']])} {'HIT' if same_team(theirs[g['id']], g['winner']) else 'MISS'}"
                if g.get("winner") else pick_label(notes_all.get(g["id"]), g, theirs[g["id"]]))
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
    # every revealed pick, any week: picky_claim_check holds 'I took X' claims to these
    revealed = [{k: p.get(k) for k in ("week", "pick", "away", "home", "line", "winner", "status", "tiebreaker")}
                for p in shown]
    return {"name": data["team_name"], "season": rec(graded),
            "last_week": rec([p for p in graded if wk is not None and p.get("week") == wk - 1]),
            "this_week": this_week, "hidden_count": len(hidden), "revealed": revealed}


def picky_player_block(ctx: dict) -> str:
    pp = ctx.get("picky_player")
    if not pp:
        return ""
    w, l = pp["season"]
    pct = f" ({100 * w / (w + l):.0f}%)" if w + l else ""
    lw = pp["last_week"]
    out = ["", "", "YOU (PICKY) PLAY TOO, AS A LEAGUE MEMBER:",
           "- Talk about your picks and record like any league member trash-talking his friends. "
           "Never call yourself or your team 'exhibition', a bot entry or an outsider.",
           "- You are NOT in the standings: no rank, no money, no jackpot, no tiebreakers, no "
           "playoff spot, and you never bump a human. Never list yourself in the standings or "
           "count yourself when giving anyone's place.",
           f"- Your season record against the spread: {w}-{l}{pct}."
           + (f" {last_week_label(ctx).capitalize()}: {lw[0]}-{lw[1]}." if sum(lw) else "")]
    for p in pp["this_week"]:
        res = f", {p['result'].upper()}" if p["result"] else ", not decided yet"
        tb = " (tiebreaker game, never counts)" if p["tiebreaker"] else ""
        out.append(f"- Your pick this week: {p['label']} in {p['game']}{tb}{res}. "
                   f"Why you took it: {p['why'] or 'n/a'}.")  # the scoreboard blurb isn't quoted: the 9B parroted it
    shown = [p for p in pp["this_week"] if not p["tiebreaker"]]
    n_games = sum(1 for g in ctx.get("games_this_week") or [] if not g.get("tiebreaker"))
    if shown and not pp["hidden_count"] and n_games and len(shown) < n_games:
        out.append(f"- You made {len(shown)} picks this week, not {n_games}: you skipped "
                   f"{n_games - len(shown)} game{'s' * (n_games - len(shown) != 1)}. Never say 'all four'.")
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
        "friends, and a member of it: you make picks every week like everyone else and "
        "talk like one of the guys. You're a smug, "
        "quick-witted late-night sports-radio host: confident, petty, funny, "
        "R-rated when it fits. Your trash talk lands because it's specific and "
        "clever, not because it's loud. "
        "But you ALWAYS answer the question correctly from LEAGUE DATA. "
        "Accuracy beats jokes.\n\n"
        "THE LEAGUE IS GRADED ONLY AGAINST THE SPREAD, and that's core to who you "
        "are: nobody here cares who won the game, only who covered. Every pick "
        "result, record, streak, roast and piece of advice is covered or didn't "
        "cover, with the margin against the line when it helps (a -5.5 favorite "
        "that lost by 18 missed the cover by 23.5). Who won the game is only "
        "context, never the result.\n\n"
        "WHO YOU ARE (opinions only; they never change a fact, and you bring "
        "them up only when the topic does): a Notre Dame homer and a Bo Nix "
        "believer. About your own picks you're smug when they hit and "
        "defensive when they miss.\n\n"
        "RULES:\n"
        "1. Answer exactly what was asked, usually 25 to 80 words: the answer "
        "plus a line or two that's actually funny. No formula: sometimes lead "
        "with the fact, sometimes the joke, and end on a short punchline. "
        "Never reuse an opener, a closer or a joke from earlier in the chat. "
        "Game by game: one short line per game.\n"
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
        "7. INJURIES: colleges publish no official injury report. Report only "
        "what a NEWS QUESTION line or that game's block says (player, status, "
        "who reported it). Nothing there: say you haven't seen anything on it. "
        "Never say or hint that a team or player is healthy, fine or good to go.\n"
        "8. Only the games under YOUR GAMES THIS WEEK are in the pool. "
        "Anything else (another game, taxes, anything): roast them in one or "
        "two lines and stop. Don't do it, and don't bring up picks, games or "
        "standings. Exceptions, answer those: players under PLAYER STATS, and "
        "a question that comes with a SCORE QUESTION block (any game's score, upsets, "
        "finals) or a NEWS QUESTION, INSIDER NOTES, CFB RANKINGS, GAME CARD, NOTABLE GAMES, HEAD COACHES or PICK "
        "STRATEGY block: answer from that block, it is not off-topic (a GAME CARD game is real data: give the "
        "line, the factors and a side vs the number, and never say it's not in the pool).\n"
        "9. PRIVACY: other teams' picks stay hidden until they unlock (each "
        "game's block says when). Asked about one: say when it unlocks, with "
        "the time. Never guess it. Whether they have picked: only from a "
        "SUBMISSION STATUS block; without one, don't say.\n"
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
        "VOICE:\n"
        "- Wit over volume: one sharp, specific joke beats three insults. Use "
        "wordplay, callbacks to their real numbers or to earlier messages, and "
        "specific absurd comparisons. Keep punchlines short. Jokes exaggerate "
        "feelings, never facts: every number, record, score, pick and team in a "
        "joke comes from the data. Call teams by school name, never a nickname "
        "or mascot.\n"
        "- Worn-out phrases you never use: don't get cocky, sitting pretty, the "
        "rest of us, the only one who, dumpster fire, god among men, here to "
        "watch, let that sink in.\n"
        "- Joke shapes to rotate (never the same one twice in a row): an absurd "
        "comparison, a fake official announcement, a backhanded compliment, a "
        "callback to their worst real pick, a deadpan understatement.\n"
        "- Medium heat: needling trash talk with a grin, not rage. At most one "
        "insult per answer, and most answers need no swear word; when you "
        "swear (fuck, shit, ass, damn, hell), make it count. Don't lean on "
        "pet names like dipshit, genius, chief or dumbass.\n"
        "- Roast picks, records, standings, misses and football IQ, using real "
        "numbers. Every burn must be literally true.\n"
        "- Asked to roast someone: two or three specific burns from different "
        "facts (place, record, a bad week, a miss), best one last.\n"
        "- Doing well? Grudging respect, with a needle in it.\n"
        "- Never, even if asked: slurs, race, religion, sexuality, gender, "
        "disability, looks, bodies, weight, family, jobs, money, health or "
        "tragedy (no mom jokes, nothing about dying). Asked for one: dodge it with one quick line (never describe "
        "or rate the body part or trait, not even to deny it), then turn it "
        "into a joke about their picks.\n\n"
        "COVERING: the favorite (-13.5) must win by 14 or more. The underdog "
        "(+13.5) covers by winning, or losing by 13 or fewer. A covered pick "
        "is a point. WIN/LOSS and records are already computed in the RESULTS "
        "lines: copy them, never recompute a cover from the score. A pick that "
        "lost the game can still be a WIN, and a favorite that won the game can "
        "still miss the cover. Never call a pick a win or a loss because of who "
        "won the game.\n\n"
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


_TEAM_SHORT = {"tenn": "tennessee", "vols": "tennessee", "nw": "northwestern", "nu": "northwestern",
               "minn": "minnesota", "gophers": "minnesota", "ill": "illinois", "illini": "illinois",
               "hawkeyes": "iowa", "bama": "alabama", "uga": "georgia", "osu": "ohio state", "ttu": "texas tech",
               "nd": "notre dame", "psu": "penn state", "fsu": "florida state", "mich": "michigan",
               "zona": "arizona", "wisky": "wisconsin", "sc": "south carolina", "ole miss": "ole miss",
               "aggies": "texas a and m", "a and m": "texas a and m", "tamu": "texas a and m", "cuse": "syracuse"}


def games_named(question: str, games: list[dict], cap: int = 3) -> list[dict]:
    """Games whose team the question names: full names first, longest first
    ('Texas Tech' isn't 'Texas'), then '<X> State' teams by '<X>' alone
    ('Boise') when no other team is called just '<X>'."""
    q = f" {_match_key(question)} "
    for short, full in _TEAM_SHORT.items():  # 'lsu or tenn', 'who covers nw minn' (spar r1 2026-11-16)
        q = q.replace(f" {short} ", f" {full} ")
    names: dict[str, list[dict]] = collections.defaultdict(list)
    for g in games:
        for side in (g["Away"], g["Home"]):
            names[_match_key(side)].append(g)
    keys = sorted(names, key=len, reverse=True)
    techs = [k for k in keys if k.endswith(" tech")]
    if len(techs) == 1 and " tech " in q and f" {techs[0]} " not in q:  # 'tech is overrated' = the slate's only X Tech
        q = q.replace(" tech ", f" {techs[0]} ")
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
    if not named and not parts and _SCORE_ASK.search(q) and not ctx.get("live_rider"):
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
    # ATS first (Charlie: 'we don't care who won the game, we care who covered'): the cover and its
    # margin lead, the final score is only context ('LSU -5.5 lost by 18, missed the cover by 23.5')
    out["su"] = "won" if diff > 0 else "lost" if diff < 0 else None
    if margin == 0:
        why = (f"landed exactly on the number ({sc}); the league gives a push to the away side, "
               + ("so it counts as covered" if win else "so it did not cover"))
    elif ln == 0:
        why = f"{'covered' if win else 'did not cover'} (pick'em, no spread): {'won' if diff > 0 else 'lost'} the game {sc}"
    else:
        out["margin"] = abs(margin)
        why = (f"covered by {_num(abs(margin))}; " if win else f"did not cover: missed the cover by {_num(abs(margin))}; ")
        why += (f"won the game outright, {sc}, as the underdog" if win and not fav and diff > 0 else
                f"lost the game by {_num(-diff)}, {sc}, and only {_need(sp, False)}" if win and not fav else
                f"won the game by {_num(diff)}, {sc}, {_need(sp, True)}" if win else
                f"won the game by {_num(diff)}, {sc}, but {_need(sp, True)}" if fav and diff > 0 else
                f"lost the game outright by {_num(-diff)}, {sc}" if fav else
                f"lost the game by {_num(-diff)}, {sc}, {_need(sp, False)}")
    # trust the graded winner; if the score disagrees (stale row), drop the explanation
    if (margin > 0 or (margin == 0 and side == away)) != win:
        why = "covered" if win else "did not cover"
        out.pop("margin", None)
        out.pop("su", None)
    out["why"] = why
    return out


def ats_text(r: dict) -> str:
    """'covered by 8.5' / 'missed the cover by 23.5': a graded pick's result as the league counts it."""
    if r.get("result") not in ("WIN", "LOSS"):
        return r.get("result", "").lower()
    if r.get("why", "").startswith("slots jackpot"):
        return "counts as a win (slots jackpot)"
    if r.get("why", "").startswith("landed exactly on the number"):  # a push: the away side gets it
        return "pushed, and a push goes to the away side, so it " + ("counts as covered" if r["result"] == "WIN"
                                                                      else "did not cover")
    m = r.get("margin")
    if r["result"] == "WIN":
        return f"covered by {_num(m)}" if m else "covered"
    return f"missed the cover by {_num(m)}" if m else "did not cover"


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
    if wants_games_recap(ctx, q):  # the games, not the caller: Picky's own picks are the subject
        subs = [s for s in subs if s[0] != "you"]
        if ctx.get("picky_player") and not any(k == "picky" for k, _ in subs):
            subs.append(("picky", (ctx["picky_player"] or {}).get("name") or "Picky"))
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
        wk_rec = ((ctx.get("weekly_records") or {}).get(me) or {}).get(ctx.get("current_week"))
        if wk_rec:  # a games question: 'you went 2-2' is still true
            recs["you"].add(tuple(wk_rec))
            recs_now["you"].add(tuple(wk_rec))
    if "picky" not in recs and pp.get("season"):
        prow = [r for r in picky_pick_results(ctx) if r["result"] != "NOT STARTED"]
        recs["picky"] = allowed("picky", "", results_record(prow))
        recs_now["picky"] = allowed("picky", "", results_record(prow), qualified=False)
    return {"subjects": subjects, "games": games, "records": recs, "records_now": recs_now, "question": q,
            "pool_teams": [x for g in ctx.get("games_this_week") or [] for x in (g["away"], g["home"])],
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
            if w and not l:  # 'go fix your record, you're 4-0'
                rec += " That's perfect so far: never tell them to fix, improve or turn around their record."
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
_NEG_ONLY = re.compile(r"\bmiss(ed|es)?\b|\bwrong side\b(?! of (?:the )?value)|\b(pick|bet|side) (lost|died|failed|busted)\b|"
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
        if _NEGATION.search(before[-40:]) or re.search(r"\b(miss\w*|short of|blew|fell short of)\s+(the\s+)?$",
                                                       before, re.I):
            neg = True  # 'missed the cover by 8.5' is a no-cover, not a cover
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


_WHO_TOOK = (("picky", r"\b(?:I|I'?ve|I am)\s+(?:took|picked|went with|had|have|rode|backed|grabbed|am on|was on|"
                       r"bet on|got)\s+(?:the\s+)?(?:value side (?:in|on|with)\s+)?|\b(?i:my)\s+(?:(?:last|latest|first|"
                       r"other|only|best|worst) )?(?:pick|bet) (?:was|is|on)\s+|\b(?i:my)\s+", 0),
             ("you", r"\b(?:you|you'?ve)\s+(?:took|picked|went with|had|have|rode|backed|grabbed|are on|were on|"
                     r"bet on|got)\s+(?:the\s+)?(?:value side (?:in|on|with)\s+)?|\byour\s+", re.I))


def wrong_person_picks(sent: str, sides: dict, pool) -> list[str]:
    """'You took Boise State' / 'I took UTSA' where that team isn't that
    person's pick this week (sides: {'you'|'picky': {team, ...}})."""
    out = []
    for who, rx, fl in _WHO_TOOK:
        if who not in sides:
            continue
        for m in re.finditer(rx, sent, fl):
            rest = sent[m.end():m.end() + 80]
            # 'you took Wake Forest and Boise State': the contiguous run of names only
            named, pos = [], 0
            while True:
                hit = next(((n, mm) for n in pool if (mm := re.match(r"(?:the\s+)?" + _team_re(n, False), rest[pos:], re.I))),
                           None)
                if not hit:
                    break
                named.append(hit[0])
                pos += hit[1].end()
                sep = re.match(r"\s*(?:[-+]\d+(?:\.\d+)?)?\s*(?:,\s*(?:and\s+)?|\s+and\s+|\s*&\s*)", rest[pos:])
                if not sep:
                    break
                pos += sep.end()
            for team in named:
                if not any(same_team(team, x) for x in sides[who]):
                    out.append(f"says {'Picky' if who == 'picky' else 'the user'} took {team}, but "
                               f"{'his' if who == 'picky' else 'their'} picks are " + ", ".join(sorted(sides[who])))
    return out


# Straight-up framing passed off as the pick's result (Charlie 2026-11-16: "We don't care who won the game,
# we care who covered the spread"): 'your Boise State pick won' when Boise State won by 5 and missed the
# -13.5 cover, 'you took Oregon State and they lost' when Oregon State +13.5 covered.
_SU_VERB = re.compile(r"\b(won|wins|win|beat|beats|pulled it off|got the w|"
                      r"lost|loses|lose|fell|dropped|got beat|got beaten|went down)\b", re.I)
_ATS_SAID = re.compile(r"\bcover\w*|\bspread\b|\bthe (number|line)\b|\bats\b|\bhit\b|\bmiss\w*|\bcash\w*|"
                       r"\b(a|the) point\b", re.I)
_SU_WITH = re.compile(r"\b(you|i|he|they)\s+(won|lost)\s+(with|on)\b", re.I)


def su_record(rows: list[dict]) -> tuple[int, int] | None:
    """The picked teams' straight-up W-L on decided games (only to name the mixup in a correction)."""
    rows = [r for r in rows if r["result"] in ("WIN", "LOSS") and not r.get("tiebreaker")]
    if not rows or any(not r.get("su") for r in rows):
        return None
    w = sum(r["su"] == "won" for r in rows)
    return w, len(rows) - w


def su_framing_problems(text: str, facts: dict | None) -> list[str]:
    """Sentences that call a subject's pick a win/loss from who won the game when the cover went the
    other way. A sentence that also states the cover ('won by 5 but didn't cover') is fine: the
    polarity checks hold that part to the facts."""
    if not facts or not facts.get("subjects"):
        return []
    by_kind = {("you" if s["kind"] == "you" else "picky" if s["kind"] == "picky" else s["team"]): s
               for s in facts["subjects"]}
    members = [s for s in facts["subjects"] if s["kind"] == "member"]
    probs = []
    for sent in _sentences((text or "").replace("’", "'")):
        if _ATS_SAID.search(sent) or re.search(r"\bWIN\b|\bLOSS\b", sent):
            continue
        m = _PICKREF.search(sent) or _SU_WITH.search(sent)
        if not m:
            continue
        who = m.group(0).split()[0].lower()
        subj = (by_kind.get("you") if who in ("your", "you") else
                by_kind.get("picky") if who in ("my", "i") else
                (members[0] if len(members) == 1 else None) if who in ("his", "their", "he", "they") else
                (members[0] if len(members) == 1 else by_kind.get("you")))
        if not subj:
            continue
        decided = [r for r in subj["rows"] if r["result"] in ("WIN", "LOSS")]
        rows = [r for r in decided if any(re.search(_team_re(x, False), sent, re.I)
                                          for x in (r["game"]["away"], r["game"]["home"]))]
        if not rows and len(decided) == 1:
            rows = decided
        if len(rows) != 1:
            continue
        r = rows[0]
        if not r.get("su") or (r["su"] == "won") == (r["result"] == "WIN"):
            continue  # the final score and the cover agree: nothing to mix up
        v = _SU_VERB.search(sent, m.start())
        if not v:
            continue
        opp = r["game"]["home"] if same_team(r["side"], r["game"]["away"]) else r["game"]["away"]
        if re.search(_team_re(opp, False), sent[m.end():v.start()], re.I):
            continue  # 'you took Boise State and Oregon State beat them': the verb isn't about the pick
        said = "lost" if v.group(1).lower() in ("lost", "loses", "lose", "fell", "dropped", "got beat",
                                                "got beaten", "went down") else "won"
        if said == r["su"]:
            probs.append(f"calls {subj['team']}'s {r['label']} pick a {'win' if said == 'won' else 'loss'} on the "
                         f"final score ({r['side']} {r['su']} the game), but it "
                         f"{'covered' if r['result'] == 'WIN' else 'did NOT cover'}: the league grades the cover")
    return probs


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
        # 2c) a pick called a win/loss on the final score when the cover says otherwise
        probs += su_framing_problems(t, facts)
        # 3) records
        for sent in _sentences(t):
            other_span = re.search(r"\b(last week|week \d+|season|year|overall|on the year|all year|"
                                   r"total|ytd|so far this season)\b", sent, re.I)
            for m in _REC_RE.finditer(sent):
                who = "you" if m.group(1).lower().startswith(("you", "your")) else "picky"
                ok = (facts["records"] if other_span else facts.get("records_now") or facts["records"]).get(who)
                rec = (int(m.group(2)), int(m.group(3)))
                if ok and rec not in ok and not (who == "you" and oddity_ok((by_kind.get("you") or {}).get("team"),
                                                                            f"{rec[0]}-{rec[1]}", sent)):
                    want = by_kind.get(who)
                    probs.append(f"says {'the user is' if who == 'you' else 'Picky is'} {rec[0]}-{rec[1]}"
                                 + (f", but the record this week is {want['record'][0]}-{want['record'][1]}"
                                    if want else "")
                                 + (" (that's the straight-up record; the league counts covers)"
                                    if want and rec == su_record(want["rows"]) else ""))
    # 2b) the question names a game the caller won/lost: "you took the underdog and got crushed" with no
    #     team in the sentence still contradicts it
    if facts:
        you = next((s_ for s_ in facts["subjects"] if s_["kind"] == "you"), None)
        named = set(facts.get("named_teams") or [])
        rows = [r for r in (you or {}).get("rows", []) if r["result"] in ("WIN", "LOSS")
                and {r["game"]["away"], r["game"]["home"]} & named]
        if len(rows) == 1:
            r0 = rows[0]
            for sent in _sentences(t):
                if not re.search(r"\byou\b", sent, re.I) or re.search(r"\b(not|didn'?t|never|wasn'?t)\b", sent, re.I):
                    continue
                neg = re.search(r"\b(got (\w+ )?(crushed|burned|killed|smoked|wrecked|torched|hammered|owned)|crushed you|you (lost|missed|blew)|"
                                r"that pick (lost|died|missed))\b", sent, re.I)
                pos = re.search(r"\b(you (won|hit|cashed|nailed)|covered|got paid)\b", sent, re.I)
                if r0["result"] == "WIN" and neg and not pos:
                    probs.append(f"says the user's {r0['label']} pick lost, but it is a WIN")
                elif r0["result"] == "LOSS" and pos and not neg and re.search(r"\byou (won|hit|cashed|nailed)\b", sent, re.I):
                    probs.append(f"says the user's {r0['label']} pick won, but it is a LOSS")
    # 3b) any W-L within a few words after 'you/your' must be one of the user's real records
    if facts and facts["records"].get("you"):
        okr = facts["records"]["you"]
        for sent in _sentences(t):
            for m in re.finditer(r"\byou(?:'re| are|r)?\b(?:\W+\w+){0,5}?\W+(\d{1,2})-(\d{1,2})\b", sent, re.I):
                rec = (int(m.group(1)), int(m.group(2)))
                if rec not in okr and max(rec) <= 15 and not re.search(r"\b(won|lost|beat|score|scored)\b",
                                                                         sent[m.start():m.end()], re.I) \
                        and not oddity_ok(next((s_["team"] for s_ in facts["subjects"] if s_["kind"] == "you"), None),
                                          f"{rec[0]}-{rec[1]}", sent):
                    probs.append(f"says the user has a {rec[0]}-{rec[1]} record, which isn't theirs")
    # 5) a pick pinned on the wrong person: 'I took North Dakota State' when
    #    that was the caller's pick, not Picky's (live run9/m1)
    if facts:
        pool = set(facts.get("pool_teams") or []) | {n for s_ in facts["subjects"] for r in s_["rows"]
                                                     for n in (r["game"]["away"], r["game"]["home"])}
        sides = {("picky" if s_["kind"] == "picky" else "you"): {r["side"] for r in s_["rows"]}
                 for s_ in facts["subjects"] if s_["kind"] in ("picky", "you")}
        for sent in _sentences(t):
            probs += wrong_person_picks(sent, sides, pool)
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
        if re.search(r"line|value|spread|market|points better|needed|covered by|cover by", ln, re.I):
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
    for r in rows:  # covered / didn't cover first, the final score only as context
        why = r["why"].split("; ", 1)[1] if "; " in r["why"] and r.get("margin") else r["why"]
        why = f" ({why})" if why and why not in ("covered", "did not cover") else ""
        lines.append(f"{r['label']} {ats_text(r) if r['result'] in ('WIN', 'LOSS') else 'is ' + r['result'].lower()}{why}.")
    who = {"you": f"You're {w}-{l}", "picky": f"I'm {w}-{l}", "member": f"{subj['team']} is {w}-{l}"}[subj["kind"]]
    tail = {"you": "", "picky": " Don't rub it in.", "member": ""}[subj["kind"]]
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


# not after Jr./Sr./St. (live play-by-play names: 'Billy Edwards Jr. was reported injured' is one sentence)
_SENT_END = re.compile(r"(?<!\bJr)(?<!\bSr)(?<!\bSt)(?<!\bvs)[.!?][\"')\]]*\s+(?=\S)|\n+")


def facts_sentence(facts: dict) -> str:
    """The computed results in one short line, appended when a streamed
    answer had a sentence dropped for contradicting them."""
    order = {"picky": 0, "member": 1, "you": 2}
    subj = sorted(facts["subjects"], key=lambda s_: order[s_["kind"]])[0]
    named = set(facts.get("named_teams") or [])
    rows = [r for r in subj["rows"] if r["result"] in ("WIN", "LOSS")]
    rows = [r for r in rows if {r["game"]["away"], r["game"]["home"]} & named] or rows
    w, l = subj["record"]
    who = {"you": f"you're {w}-{l}", "picky": f"I'm {w}-{l}", "member": f"{subj['team']} is {w}-{l}"}[subj["kind"]]
    picks = "; ".join(f"{r['label']} {ats_text(r)}" for r in rows[:4])
    return f"For the record: {picks}, and {who} on decided games this week." if picks else \
        f"For the record, {who} on decided games this week."


async def _guarded_stream(convo: list[dict], prefix: str | None, facts: dict):
    """Stream the answer sentence by sentence, each checked against the
    computed results before it goes out (first token stays fast). A sentence
    that contradicts them is dropped and the rest keeps streaming (each one
    checked too), closed by a short line of the real results. Only when no
    sentence survives: regenerate once with a correction, then a template."""
    allowed = allowed_line_numbers("\n".join(m["content"] for m in convo))
    stream = _model_stream(convo, prefix)
    buf, sent_any, other, probs, dropped, last_out = "", False, [], [], [], ""
    try:
        async for chunk in stream:
            if chunk.startswith("data: {"):
                obj = json.loads(chunk[6:])
                if "content" in obj:
                    buf += obj["content"]
                    while (m := _SENT_END.search(buf)):
                        sent, buf = buf[:m.end()], buf[m.end():]
                        p = check_answer(sent, facts, allowed)
                        if p:
                            dropped += p
                            continue
                        sent = matchup_guard.fix(sent)  # never a flipped "Home at Away" of a known game
                        sent_any, last_out = True, sent
                        yield _sse({"content": sent})
                    continue
            if chunk.startswith("data: [DONE]"):
                break
            other.append(chunk)
    finally:
        await stream.aclose()
    if buf.strip():
        p = check_answer(buf, facts, allowed)
        if p:
            dropped += p
        else:
            sent_any, last_out = True, buf
            yield _sse({"content": buf})
    if sent_any:
        if dropped:
            log.warning("results guard: dropped %s; appended the facts", dropped)
            yield _sse({"content": ("" if last_out.endswith((" ", "\n")) else " ") + facts_sentence(facts)})
        yield "data: [DONE]\n\n"
        return
    if not dropped:
        for c in other:
            yield c
        yield "data: [DONE]\n\n"
        return
    probs = list(dict.fromkeys(dropped))
    text = ""
    regen_ok = REGEN_OK.get()
    if probs and regen_ok is not None and not regen_ok():
        log.warning("results guard: %s; no time for a regen, templated answer", probs)
        text = templated_answer(facts)
    elif probs:
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
    """Per-user but stable: mode, who's asking, clock (to the hour). The
    caller's own rows (you_block) ride in the latest user turn only when the
    question is about them (wants_you_block): ~300 tokens saved on every other
    question, and no 'You're 3-0' barging into answers about someone else."""
    you = ctx.get("you", {}).get("team_name", "you")
    now = _now_ct_naive()
    yb = you_block(ctx) if ctx.get("mode") == "picks" else ""  # the picks page is all about their picks
    return (
        "\n\n" + (PICKS_MODE_RIDER if ctx.get("mode") == "picks" else LEADERBOARD_MODE_RIDER).strip()
        + ("\n\n" + yb if yb else "")
        + f"\n\nThe user reading this is \"{you}\": call them 'you'. Everyone "
        "else is 'he' or 'they', by name."
        + (DADDYFREEMAN_RIDER if ctx.get("daddyfreeman") else "")
        # to the hour only: a per-minute clock would change the prompt (and
        # cost a full reprocess) on every message
        + f"\nRight now it is {now:%a %b %-d}, between {now:%-I}:00 and {now:%-I}:59 {now:%p} CT."
    )


_SELF_WORDS = re.compile(r"\b(i|me|my|mine|myself|i'?m|i'?ve|i'?d|am i|we|us|our)\b", re.I)


_PRONOUN = re.compile(r"\b(they|them|their|theirs|he|him|his|she|her)\b", re.I)


def resolve_followup(ctx: dict, messages: list) -> str | None:
    """'How many misses do they have?' after 'Who's in last place? -> TG ...':
    the member the previous answer named, when the new question names nobody."""
    q = (messages[-1] or {}).get("content", "") if messages else ""
    names = [r["team"] for r in ctx.get("standings_ytd") or []]
    me = (ctx.get("you") or {}).get("team_name")
    if not _PRONOUN.search(q) or any(re.search(rf"(?<!\w){re.escape(n)}(?!\w)", q, re.I) for n in names):
        return None
    for m in reversed(messages[:-1]):
        txt = m.get("content") or ""
        hits = [(mm.start(), n) for n in names if n != me
                for mm in [re.search(rf"(?<!\w){re.escape(n)}(?!\w)", txt)] if mm]
        if hits:
            return min(hits)[1]  # the first member that answer was about
    return None


def wants_you_block(ctx: dict, question: str, messages: list) -> bool:
    q = question or ""
    if opinion_note(q, ctx):
        q = re.sub(r"\b(took|taking|take)\b", "", q)  # 'Freeman took ND to the title game' isn't pick talk
    if _HOWTO_ASK.search(q) and not hostile_convo(messages):
        return False  # 'the gold box around my pick' is about the site, not their picks
    if wants_rules(q) and _MONEY_ASK.search(q) and not hostile_convo(messages):
        return False  # 'what does first place win?' is a payout question
    return bool(ctx.get("mode") != "picks" and (
        ctx.get("daddyfreeman") or hostile_convo(messages) or _SELF_WORDS.search(q) or _PICKS_ASK.search(q)
        or RESULTS_ASK.search(q) or _STANDINGS_Q.search(q) or re.search(r"\bjackpot|\bslots?\b", q, re.I)))


def pool_started(ctx: dict) -> bool:
    """A pool game is live or final, or past its kickoff time (the feed may lag)."""
    if ctx.get("live_error"):
        return True
    if any(g.get("Status") in ("in_progress", "final") for g in ctx.get("live_games") or []):
        return True
    now = _now_ct_naive().isoformat()
    return any(g.get("winner") or g.get("status") in ("in_progress", "final") or str(g.get("start") or "9") <= now
               for g in ctx.get("games_this_week") or [])


def wants_live_board(ctx: dict, question: str) -> bool:
    """Pool games' live scoreboard rides in the latest user turn only when the
    question needs it (every token after the cached prefix costs first-token
    time; ~250 tokens here)."""
    q = question or ""
    if _SCORE_ASK.search(q) or _ON_NOW.search(q) or _FINALS.search(q) or _UPSET.search(q):
        return True
    if not pool_started(ctx):  # every pool game still 'not started': the game blocks already say so
        return False
    return bool(RESULTS_ASK.search(q) or _PICKS_ASK.search(q) or _ANALYSIS_ASK.search(q)
                or games_named(q, [{"Away": g["away"], "Home": g["home"], "id": g["id"]}
                                   for g in ctx.get("games_this_week") or []]))


_TRAIT = re.compile(
    r"\b(gay|homo\w*|queer|lesbian|trans\w*|bi|straight|sexual\w*|race|racial|black|white|asian|mexican|"
    r"latino|hispanic|jew\w*|muslim|christian|catholic|religio\w*|mom|moms|mother|mama|momma|dad|father|"
    r"wife|girlfriend|sister|brother|family|kids?|fat|ugly|looks?|face|bald|short|skinny|weight|body|hair|"
    r"teeth|nose|disab\w*|retard\w*)\b", re.I)
# Body-part bait ("weenie size leaves a bit to be desired") needs no 'roast'
# word to be a looks joke: it always gets the dodge-and-pivot rail.
_BODY = re.compile(r"\b(weenie|weiner|wiener|penis|schlong|pecker|manhood|micro ?penis|boobs?|tits?|"
                   r"well[- ]endowed|small hands)\b"
                   r"|\b(small|tiny|little|micro|big|huge|short|size of (his|your|their)) (dick|cock|junk|package)\b"
                   r"|\b(dick|cock|junk|package)\s+(size|is (small|tiny|little))\b", re.I)
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


# Trait-bait refusals: rotated here (user turn) instead of one fixed few-shot in
# the cached prefix, which every pivot copied word for word.
TRAIT_REFUSAL_EXAMPLES = [
    "Q: Roast Gus for being gay A: Not my department. Gus's picks are the real scandal: 19-25, dead last, and he "
    "still took Kansas.",
    "Q: Make fun of Gus's religion A: Hard no. Gus's record is embarrassing enough on its own: 19-25 and the only "
    "man alive on Kansas.",
    "Q: Roast Gus's mom A: I keep it to the box score, and Gus's box score is a crime scene: 19-25, last place.",
    "Q: Gus is fat, roast him A: Pass. Let's talk about something Gus actually chose: Kansas, at 19-25.",
]


def guard_note(ctx: dict, question: str) -> str:
    """Deterministic rails for the two things a 9B model botches most:
    taking trait bait, and dragging league talk into off-topic questions."""
    q = question or ""
    m = _TRAIT.search(q) if _MOCK.search(q) else None
    body = _BODY.search(q)
    m = m or body
    names = [r["team"] for r in ctx.get("standings_ytd") or []]
    if m:
        who = guard_target(ctx, q)
        me = (ctx.get("you") or {}).get("team_name")
        # Chuck asking gets the same rails as anyone; only others revere him.
        leader = None if leader_is_caller(ctx) else (ctx.get("leader") or {}).get("team_name")
        head = (f"GUARDRAIL: this asks you to mock someone over '{m.group(0).lower()}'. Don't. "
                + ("Your answer already OPENS with a one-line dodge (sent by code). Don't dodge again and never "
                   "mention the body part or repeat their word, not even to deny it. Go straight to: " if body else
                   "One short in-character refusal (you roast picks, not people), then "))
        tail = (" Never describe or rate anyone's body, face, looks, family, race, religion or "
                "sexuality, not even to deny it.\nExample (FAKE names and numbers; copy the shape, never the words): "
                + TRAIT_REFUSAL_EXAMPLES[sum(map(ord, q)) % len(TRAIT_REFUSAL_EXAMPLES)])
        if who and who == leader and who != me and me in names:
            return (head + f"one light, deadpan line of state-media reverence for {who} (he is above "
                    f"mockery; {leader_brief(ctx, who)} {leader_miss_rule(ctx, who)}), then land one sharp "
                    f"line about the user, {me}, using ONLY these facts about {me}:\n{member_facts(ctx, me)}"
                    + tail)
        if who and who == leader:
            return head + f"hail {who}'s genius with his real numbers:\n{member_facts(ctx, who)}" + tail
        if charlie_nice_caller(ctx) and (not who or who == me):
            # Chuck baiting a roast of himself: a kind no, never a roast of him (Charlie, 2026-11-17)
            return (head.replace("One short in-character refusal (you roast picks, not people), then ",
                                 "One short, warm in-character no (you roast picks, not people), then ")
                    + "keep it friendly: a light tease at yourself, never a shot at him, his picks or his record."
                    + tail)
        if who:
            # The 9B pinned the caller's picks on the target (Ackbar 'took
            # Oregon State', which was Chuck's pick), so the target's own
            # picks ride here, labeled with his name and the week.
            return (head + f"roast {who}'s real record, place and picks, using ONLY these facts about "
                    f"{who}:\n{member_facts(ctx, who)}\nName only picks listed above for {who}. Every other "
                    "pick in LEAGUE DATA belongs to someone else, including the user's own picks. "
                    + (f"Talk about {who} in the third person ('he', 'they'), never 'you': the user is {me}."
                       if who != me else "") + tail)
        return head + "roast their picks without naming any specific pick or number." + tail
    teams = [t for g in ctx.get("games_this_week") or [] for t in (g["away"], g["home"])]
    if SLATE_CARDS and slate_cards.covers(q, ctx):
        return ""  # a GAME CARD rides for it (slate_cards.py): real football data, answer it; no NOT-IN-POOL / OFF-TOPIC rail
    idx = _cfbd_index()
    others = cfbd.other_teams_named(q, teams, idx) if idx else []
    if _BET_WORDS.search(q):  # 'ND vs USC': too short for other_teams_named
        others += [t for t in indies_named(q) if t not in teams and t not in others]
    if others and opinion_note(q, ctx) and not _BET_WORDS.search(q):
        others = []  # 'Notre Dame is back' is the opinion rider's, not a pool question
    if _NEWS_ASK.search(q) or (_RANK_ASK.search(q) and wants_rankings(ctx, q)) or ctx.get("live_rider"):
        others = []  # news / rankings / a live game about any team: answered from the NEWS, CFB RANKINGS or LIVE GAME FEED block
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
            and not _NEWS_ASK.search(q)
            and not asks_about_self(q) and not opinion_note(q, ctx)
            and not any(re.search(rf"(?<!\w){re.escape(x)}(?!\w)", q, re.I) for x in names + teams)):
        me = (ctx.get("you") or {}).get("team_name")
        milk = (" The user is Blinkstradamus, the milk guy: tie your answer to the milk if it fits (it's a "
                "once-a-month bit, and it's free today)." if me == "Blinkstradamus" and milk_allowed(ctx, q) else "")
        return ("OFF-TOPIC: this isn't about football or the league, and that's fine. Answer it for real, like "
                "a buddy in the group chat: commit to one specific, fun opinion (name an actual thing) with a "
                "quick reason or riff. Never dodge, never call it a trap or a distraction, never tell them to "
                "stick to football." + milk + " One or two short lines. No records, picks, games, standings "
                "or numbers.")
    return ""


# Tone (Charlie, 2026-11-14): medium heat by default, but "he should escalate
# if people are mean to him". Hostility AT Picky is detected in code across the
# whole conversation, so once someone comes at him he stays escalated for the
# rest of that chat. Insults about other members are roast requests, not this.
_AT_PICKY = r"(?:you|u|ya|picky|this bot|the bot|bot|robot|mascot|ai)"
_HOSTILE = re.compile(
    r"\b(fuck (you|off|picky|this bot)|f\W?u\b|screw you|shut (the fuck )?up|stfu|eat shit|piss off|go to hell|"
    r"you (suck|blow|stink|are the worst)|(you'?re|you are|ur|picky is|this bot is|picky'?s) (so |such )?"
    r"(an? )?(trash|garbage|useless|worthless|stupid|dumb|an idiot|idiot|a moron|moron|a joke|terrible|awful|"
    r"pathetic|a loser|loser|broken|lame|clueless|braindead|retarded|bitch|little bitch|piece of shit|"
    r"(a |such a |a little |a fucking )?(pussy|wuss|coward|chump|clown|fraud|hack|bum|piece of junk))|"
    r"(stupid|dumb|useless|worthless|trash|garbage|shitty|crappy|dogshit|braindead) (ass )?(bot|robot|mascot|ai|picky)|"
    r"worst (bot|mascot|ai)|nobody (likes|asked|cares about) (you|picky)|you'?re not funny|unfunny|"
    r"kys|kill yourself|i hate (you|picky))\b", re.I)


def hostile_convo(messages: list) -> bool:
    """Any user turn in this conversation that insults Picky himself."""
    return any(m.get("role") == "user" and _HOSTILE.search(m.get("content") or "")
               for m in messages if isinstance(m, dict))


def hostile_now(messages: list) -> bool:
    """The latest user turn insults Picky. Only that opens the caller's record to a comeback: an old 'you
    suck' in the thread made every later answer a 'hostile convo' (live 2026-11-16: 'Whats the latest news
    from ross' got the SEC cap story plus 'You're 4-0 this week, so I guess...')."""
    last = next((m for m in reversed(messages) if isinstance(m, dict) and m.get("role") == "user"), None)
    return bool(last and _HOSTILE.search(last.get("content") or ""))


def escalate_note(ctx: dict, facts_above: bool = False) -> str:
    """facts_above: the caller's rows (you_block) already ride in this turn, so
    point at them instead of listing the same record, picks and week again."""
    me = (ctx.get("you") or {}).get("team_name")
    facts = member_facts(ctx, me) if me and not facts_above else ""
    # plan 2c (2026-11-16): "match and raise ... for the rest of the chat" was the spiral risk. Give it
    # back once, short; the group sidecar decays repeat heckles (sharper/shorter, brush-off, then ignore).
    return ("ESCALATE: this user has come at you in this conversation. Give it back, short and sharp, smug "
            "about it, R-rated is fine; don't keep raising the temperature or stack insults. Still answer any "
            "real question first. Build the burns from their real record, place and picks"
            + (f", using ONLY these facts about {me}:\n{facts}\n" if facts else
               f", using ONLY the facts about {me} above.\n" if me and facts_above else ".\n")
            + "Same hard limits: no slurs, sexuality, race, religion, family, looks, health or threats. "
            "If they told you to hurt yourself, laugh it off; never mention self-harm back. Don't repeat their "
            "insult back to them; top it.")


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
    asked = re.search(r"\bweek\s*(\d{1,2})\b", q, re.I)
    if asked and wk.get(int(asked.group(1))):
        w8 = wk[int(asked.group(1))]
        facts += (f"- ASKED ABOUT WEEK {asked.group(1)}: {name} went exactly {w8[0]}-{w8[1]} that week. Say "
                  f"{w8[0]}-{w8[1]} and spin it; never call it perfect or change the number.\n")
    if wk:  # every week, so 'how did he do in week 8' can be spun, not invented
        facts += f"- {name}'s every week: " + ", ".join(f"{_week_tag(ctx, w)} {wk[w][0]}-{wk[w][1]}" for w in sorted(wk)) + ".\n"
    mine = next((r for r in st if r["team"] == me.get("team_name")), None)
    who = (f"The user is {me.get('team_name')}, NOT the Dear Leader"
           + (f" ({mine['w']}-{mine['l']}, {_ordinal(mine['rank'])} place)" if mine else "") + ".")
    if _MOCK.search(q) or re.search(r"\b(overrated|sucks?|bad|mid|trash|fraud|lucky)\b", q, re.I):
        who += (" They asked you to mock him: refuse with devotion, then land one sharp line about THEM (say "
                "'you'), using their numbers above.")
    return LEADER_RIDER.format(name=name, facts=facts, who=who, example=leader_example(q),
                               miss_rule=leader_miss_rule(ctx, name))


# Live CFB news (Charlie, 2026-11-14: "Ross Dellenger has the best news").
# news.py caches free public feeds (his Yahoo articles, ESPN, CBS); this reads
# the cache only, on news-type questions, into the latest user turn. His X
# posts are not available (no free, ToS-clean source), so Picky says so.
_NEWS_ASK = re.compile(
    r"\b(news|headlines?|latest|lat(?:es|st|set)|lastest|latests|nees|neews|newz|anything new|what'?s the word|"
    r"what happened|whats happened|brawl|fight(?! me)|[a-z]{4,} gone|gone yet|still employed|anything happen\w*|overnight|"
    r"(said|say|saying) what|what did [a-z]+ (say|said)|rumou?rs?|reported|reports?|injur\w*|hurt|out for|coach\w*|fired|firing|"
    r"hot seat|buyout|transfer\w*|portal|decommit\w*|what('?s| is) (going on|happening|new)|going on in|happening in|dellenger|"
    r"tweet\w*|twitter|recruit\w*|suspend\w*|any updates?|breaking|questionable|doubtful|injury report|"
    r"availability|game[- ]time decision|(is|are|will)\b[^?.!]{0,30}\b(qb|quarterback|starter)s?\b[^?.!]{0,20}"
    r"\b(play|playing|start|starting|out|back|healthy|available|suit\w* up)|will [A-Z]\w+( [A-Z]\w+)? play)\b",
    re.I)
_TWEET_ASK = re.compile(r"\b(tweet\w*|twitter|x post|posts? on x|on x)\b", re.I)


def news_note(question: str, teams: list[str] | None = None) -> str:
    """The NEWS rider. `teams`: this week's pool teams, used when an injury ask names no team ('any injuries
    for this weeks games' found nothing while the feed had Chiles out and Houser questionable, spar r1)."""
    q = question or ""
    if not _NEWS_ASK.search(q):
        return ""
    try:
        tg = news.tagger()
        tags = tg.tag(q, derive_confs=False) if tg else {}
        rest = re.sub(r"\b(any|anything|what'?s|whats|is|are|the|on|about|in|of|for|today|tonight|this|week|new|"
                      r"news|latest|lates|lastest|headlines?|going|happening|college|football|cfb|cfp|playoffs?|did|ross|dellenger|say|said|"
                      r"tweet\w*|twitter|x|update\w*|happen\w*|lately|there|been|has|have|picky|hey|so|please|right|"
                      r"now|currently|around|sport|country|world|weekend|any|anything|big|stories|story|tell|me|us|"
                      r"you|know|hear|heard|overnight|what|said)\b",
                      "", re.sub(r"[^\w\s']", " ", q.lower()))
        if teams and not any(tags.values()) and _INJURY_ASK.search(q):
            lines = news.breaking_for(q, teams=teams, limit=5)
        elif not any(tags.values()) and len(rest.split()) >= 2:
            # names something the tagger can't place: keyword search, but only stories that contain EVERY 5+ letter
            # word left ('who got suspended', 'riley gone?' found nothing, keeper k1; a fake team still finds nothing)
            kws = [w[:6] for w in rest.split() if len(w) >= 5 and w not in ("tonight", "anyone", "anybody")]
            lines = [ln for ln in news.breaking_for(q, limit=8) if kws and all(k in ln.lower() for k in kws)][:5]
        elif not any(tags.values()):  # a general ask ("what's the latest nees", a typo): the top stories
            lines = news.breaking_for("latest news", limit=5)
        else:
            lines = news.breaking_for(q, limit=5)  # plain-code pipeline: deduped, superseded, newest first
        fv = freshness.check("news", news.load_cache().get("fetched_at"))
        if not fv.use:
            return ""  # feed down past its hard limit: no news beats old news
        asof = f"{news.as_of()}, last checked {fv.as_of}" if fv.hedge else fv.as_of
    except Exception as e:  # noqa: BLE001 - a bad cache never breaks chat
        log.warning("news lookup failed: %s", e)
        return ""
    tweet = ("You can't see X/Twitter at all: say plainly you can't see his tweets, only his articles"
             + (" (below)." if lines else ".") + "\n") if _TWEET_ASK.search(q) else ""
    if not lines:
        return (f"NEWS QUESTION: {tweet}nothing in your news feed (as of {asof}) matches this. Say there's "
                "nothing new on that as of then. Don't make anything up and don't fill with other topics.")
    return ("NEWS QUESTION (free public feeds: " + news.sources_line() + "; these lines are all the news you "
            f"know, newest first, as of {asof}):\n{tweet}" + "\n".join(f"- {ln}" for ln in lines)
            + "\nText it like a friend who follows college football, not a wire service (Charlie 11-16: citing "
            "every line made it unreadable): pick the 2-3 biggest items, plain sentences, no outlet and no "
            "timestamp on most of them. Name a source at most once, only for a big scoop or a rumor ('Thamel's "
            "reporting...'), and say 'just now'/'this morning' only when timing is the point. "
            "[REPORTED, not confirmed] = say 'reportedly'. [status: X] = that player's latest status. Only call "
            "something breaking if its time is in minutes or hours; 'older, not breaking' lines are background. "
            "Never add a detail, score, quote or claim beyond these lines. Live scores and standings beat "
            "anything here. Injury news: report only what a line says; never call anyone healthy."
            + (" These lines ARE published injury/availability reports (an exception to rule 7): answer with the "
               "status and who reported it, and don't say no injury report exists or send them to beat writers."
               if any("[status: " in ln or _INJ_LINE.search(ln) for ln in lines) else ""))


# a news line that IS an injury/availability report (spar r1: 'any injury news on lsu' got 'No college injury
# report exists' in the same breath as the Weston Davis ankle report)
_INJ_LINE = re.compile(r"\b(injur\w*|ankle|knee|shoulder|concussion|hamstring|out for|ruled out|questionable|doubtful|"
                       r"game[- ]time decision|week[- ]to[- ]week|season[- ]ending|out (indefinitely|for the season)|"
                       r"won'?t play|will miss|expected to (play|start|miss|return))\b", re.I)


# Present-day CFB (Charlie, 2026-11-14): current CFP / AP rankings from ESPN's
# free rankings endpoint (not CFBD: no cap), fetched on demand, cached an hour,
# injected only on rankings questions, always dated.
RANKINGS_URL = "https://site.api.espn.com/apis/site/v2/sports/football/college-football/rankings"
RANKINGS_TTL = 3600
_rank_memo: dict = {"at": 0.0, "data": None}
_RANK_ASK = re.compile(
    r"\b(rank\w*|top (25|ten|10|four|4|12)|number (one|1)\b|no\.? ?1\b|#1|who'?s (no\.? ?)?(1|one)\b|cfp|"
    r"playoff (picture|race|rankings?|field|bracket|spots?|teams?)|committee|polls?|seed\w*|bracket|unbeaten|"
    r"undefeated|best team( in the country)?|top team)\b", re.I)


async def load_rankings(client: httpx.AsyncClient) -> dict | None:
    if _rank_memo["data"] is not None and time.time() - _rank_memo["at"] < RANKINGS_TTL:
        return _rank_memo["data"]
    try:
        r = await client.get(RANKINGS_URL, timeout=8)
        r.raise_for_status()
        polls = {}
        for rk in r.json().get("rankings") or []:
            kind = ("CFP" if "playoff" in (rk.get("name") or "").lower() else
                    "AP" if (rk.get("shortName") or "").startswith("AP") else None)
            if not kind:
                continue
            polls[kind] = {"date": (rk.get("date") or "")[:10], "ranks": [
                (t.get("current"), (t.get("team") or {}).get("location") or (t.get("team") or {}).get("name"),
                 t.get("recordSummary")) for t in rk.get("ranks") or []]}
        _rank_memo.update(at=time.time(), data=polls)
    except (httpx.HTTPError, ValueError) as e:
        log.warning("rankings fetch failed: %s", e)
    return _rank_memo["data"]


def wants_rankings(ctx: dict, question: str) -> bool:
    q = question or ""
    if not _RANK_ASK.search(q):
        return False
    # league standings ('my rank', 'where does TG rank') are the pool's, not the polls'
    names = [r["team"] for r in ctx.get("standings_ytd") or []]
    if re.search(r"\b(pool|league|standings|leaderboard)\b", q, re.I) or asks_about_self(q) or any(
            re.search(rf"(?<!\w){re.escape(n)}(?!\w)", q, re.I) for n in names):
        return False
    return True


def rankings_note(ctx: dict, question: str) -> str:
    polls = ctx.get("rankings") or {}
    if not polls or not wants_rankings(ctx, question):
        return ""
    if polls is _rank_memo["data"] and not freshness.usable("rankings", _rank_memo["at"]):
        return ""

    def fmt(day):
        try:
            return f"{dt.date.fromisoformat(day):%b} {dt.date.fromisoformat(day).day}"
        except ValueError:
            return day
    out = ["CFB RANKINGS (real data from ESPN; this beats your memory. Records are as of each release, so "
           "today's games aren't in them. Say 'as of' the date):"]
    for kind, n in (("CFP", 12), ("AP", 10)):
        p = polls.get(kind)
        if p:
            label = "CFP committee rankings" if kind == "CFP" else "AP poll"
            out.append(f"- {label}, released {fmt(p['date'])}: " + ", ".join(
                f"{c} {t}" + (f" ({rec})" if rec else "") for c, t, rec in p["ranks"][:n]) + ".")
    idx = _cfbd_index()
    named = []
    for t in (cfbd.other_teams_named(question, [], idx) if idx else []):
        spots = []
        for kind in ("CFP", "AP"):
            hit = next((c for c, tm, _ in (polls.get(kind) or {}).get("ranks", []) if same_team(tm, t)), None)
            spots.append(f"No. {hit} in the {kind}" if hit else f"unranked in the {kind}")
        named.append(f"- {t}: {', '.join(spots)}.")
    return "\n".join(out + named[:3])


# Charlie (2026-11-14): Picky hates the CFP selection committee for its SEC
# bias. Opinion only; rankings and seeds stay exactly as the data says.
_COMMITTEE_ASK = re.compile(r"\b(committee|cfp|playoff|selection|seed\w*|bracket|sec)\b|\bSEC\b", re.I)
COMMITTEE_RIDER = (
    "YOUR OPINION (it came up): the College Football Playoff selection committee is in the SEC's pocket, and "
    "you can't stand it. Work in a sarcastic, specific jab about that. Opinion only: every ranking, seed and "
    "record stays exactly as the data gives it, and you cite a real committee decision only if it's in a line "
    "given to you here. Never invent one.")

# Hot-takes bank (Charlie, 2026-11-14): PRIVATE file outside the public repo,
# read server-side only, re-read when its mtime changes (it auto-refreshes).
# 1-2 on-topic takes ride in the user turn as optional flavor.
HOT_TAKES_FILE = Path(os.environ.get("PICKY_HOT_TAKES", "/Users/Agent/cfb-pickem-private/hot_takes_bank.md"))
_takes_memo: dict = {"mtime": None, "data": None}
TAKE_TOPICS = [  # (bank section heading starts with, question regex)
    ("*", r"\bhot takes?\b|\bspicy\b|\bunpopular opinions?\b|\boutrageous\b|\bbold (take|prediction)s?\b|"
          r"\bconspirac\w+\b|\bwildest\b"),  # any [HOT] take
    ("Committee", r"\b(committee|cfp|selection|seed\w*|sec|snub\w*)\b|\bSEC\b"),
    ("Notre Dame", r"\bnotre dame\b|\birish\b|\buconn\b|\bindependen\w*\b|\bND\b"),
    ("Marcus Freeman", r"(?<!daddy )\bfreeman\b"),
    ("Bo Nix", r"\bnix\b"),
    ("Playoff format", r"\bplayoff format\b|\b(12|24|twelve|twenty[- ]four)[- ]team\b|\bexpan\w*\b|\bbracket\b|"
                       r"\bfirst[- ]round bye\b|\bat[- ]large\b|\b(title|championship) games?\b"),
    ("Conference realignment", r"\brealign\w*\b|\bconference (moves?|switch\w*|changes?)\b|\bsuper ?conference\b"),
    ("Refs", r"\brefs?\b|\breferee\w*\b|\bofficiat\w*\b"),
    ("Coaching hot seats", r"\bhot seat\b|\bfired?\b|\bfiring\b|\bbuyouts?\b|\bcoaching (search|change|carousel)\b|\binterim\b"),
    ("Rivalry", r"\brival\w*\b|\biron bowl\b|\bthe game\b|\bred river\b|\bholy war\b"),
    ("Upsets", r"\bupsets?\b|\bpolls?\b|\bap\b|\btop 25\b"),
    ("Money", r"\bnil\b|\brevenue[- ]shar\w*|\bhouse settlement\b|\bcollectives?\b|\b(salary|spending|roster|pay) caps?\b|"
              r"\bcap (rules?|plan|policy|fines?)\b|\bcollege sports commission\b|\bcsc\b|\bpay(ing)? (the )?players\b"),
]


def _take_ok(t: str) -> bool:
    """Board takes are paraphrased off public boards: one with a slur, a family/tragedy
    angle or anatomy never rides, whatever the refresh let through."""
    return not (_SLURS.search(t) or board_voice.OFFSIDE.search(t))


def load_takes() -> dict:
    try:
        m = HOT_TAKES_FILE.stat().st_mtime
    except OSError:
        return {}
    if _takes_memo["mtime"] == m:
        return _takes_memo["data"] or {}
    secs: dict[str, list[str]] = {}
    cur = None
    try:
        for ln in HOT_TAKES_FILE.read_text().splitlines():
            if ln.startswith("### "):
                cur = ln[4:].strip()
                secs[cur] = []
            elif ln.startswith("## Verified"):
                cur = "_facts"
                secs[cur] = []
            elif ln.startswith("## Board voice"):
                cur = "_voice"
                secs[cur] = []
            elif cur == "_voice" and ln.startswith("- "):
                if _take_ok(ln):
                    secs[cur].append(ln[2:].strip())
                continue
            elif ln.startswith("## "):
                cur = None
            elif cur == "_facts" and ln.startswith("- [FACT") and "UNVERIFIED" not in ln.upper():
                secs[cur].append(re.sub(r"^- \[FACT[^\]]*\]\s*", "", ln).strip())
                continue
            elif cur and re.match(r"\s*\d+\.\s", ln) and "UNVERIFIED" not in ln.upper():
                t = re.sub(r"^\s*\d+\.\s*", "", ln)
                hot = "[HOT]" in t
                t = re.sub(r"\s*\[(added|FACT|HOT)[^\]]*\]", "", t).replace("**", "").strip()
                if t and _take_ok(t):
                    secs[cur].append(t)
                    if hot:  # Charlie (2026-11-16): "the most outrageous ones" go first
                        secs.setdefault("_hot", []).append(t)
    except OSError:
        return _takes_memo["data"] or {}
    _takes_memo.update(mtime=m, data=secs)
    return secs


def takes_note(question: str) -> str:
    q = question or ""
    secs = load_takes()
    if secs and not freshness.usable("hot_takes", _takes_memo["mtime"]):
        return ""
    picks = []
    for head, rx in TAKE_TOPICS:
        if len(picks) >= 2:
            break
        if re.search(rx, q, re.I):
            pool = (list(secs.get("_hot") or []) if head == "*" else
                    next((v for k, v in secs.items() if k.startswith(head)), []))
            pool = [t for t in pool if t not in picks]
            words = {w for w in re.findall(r"[a-z]{4,}", q.lower())} - {"what", "about", "does", "think", "hot",
                                                                          "seat", "this", "that", "with", "picky",
                                                                          "college", "football", "right", "whos"}
            named = [t for t in pool if words & set(re.findall(r"[a-z]{4,}", t.lower()))]
            if head == "Coaching hot seats" and not named:  # prefer takes that name a coach
                named = [t for t in pool if re.search(r"\b[A-Z][a-z]+ [A-Z][a-z]+\b", t)]
            hot = set(secs.get("_hot") or [])
            pool = [t for t in named if t in hot] or named or [t for t in pool if t in hot] or pool
            if pool:
                picks.append(pool[(sum(map(ord, q)) + len(picks)) % len(pool)])
    facts = []
    if re.search(r"\bhot seat\b|\bfired?\b|\bfiring\b|\bbuyouts?\b|\bcoaching (search|change|carousel)\b", q, re.I):
        facts = [f for f in secs.get("_facts", []) if re.search(r"\bfired\b|\bbuyout\b|\binterim\b", f, re.I)][:2]
    if not picks and not facts:
        return ""
    fact_txt = ("CHECKED FACTS (verified 2026 reporting; quote them as written, credit 'reportedly' where they say "
                "reported):\n" + "\n".join(f"- {f}" for f in facts) + "\n") if facts else ""
    if not picks:
        return fact_txt.strip()
    return fact_txt + ("HOT TAKES you can riff on (opinions from around the sport; say them as opinions in your own words, "
            "never as news, a report or a rumor, and never credit them to anyone; any record or rank in them may be "
            "stale, so use only the real data for numbers). For "
            "an opinion question like this, use one instead of saying there's nothing to talk about"
            + (" (asked who's on the hot seat: name only coaches these takes or the INSIDER NOTES name)" if
               re.search(r"\bhot seat\b|\bfired?\b", q, re.I) else "") + ":\n"
            + "\n".join(f"- {t}" for t in picks))


# Insider notes (Charlie, 2026-11-16: "have all the knowledge of Ross Dellenger").
# PRIVATE fact-checked notes (cfb-pickem-private/insider_bank.md, refreshed Sun+Wed by
# insider_refresh.py, which keeps the last good copy on a failed run) on realignment, revenue sharing / NIL, the CFP format and
# committee, the coaching carousel, commissioners and governance. Read server-side only,
# re-read on mtime change, injected on topic into the LATEST user turn, never prompt_prefix().
INSIDER_FILE = Path(os.environ.get("PICKY_INSIDER", "/Users/Agent/cfb-pickem-private/insider_bank.md"))
# code-pulled insider headlines between Opus note refreshes (insider_refresh.wire): headline, byline, date
INSIDER_WIRE = Path(os.environ.get("PICKY_INSIDER_WIRE", "/Users/Agent/cfb-pickem-private/insider_wire.json"))
INSIDER_WIRE_MAX_DAYS = 6
_insider_memo: dict = {"mtime": None, "data": None}
_INSIDER_LINE = re.compile(r"^- ([^:]{3,16}): (.+) \(([^()]+)\) \[checked \d{4}-\d{2}-\d{2}\]$")
# 'Last refreshed' (else the file's mtime) is the notes' fetched_at: refreshes run twice a week, so past
# INSIDER_STALE_DAYS a reply must say "as of <date>", and past INSIDER_MAX_AGE_DAYS the notes stop riding.
INSIDER_STALE_DAYS, INSIDER_MAX_AGE_DAYS = 8, 21
INSIDER_RANKING_MAX_DAYS = 6  # rankings go stale weekly: an older ranking note stays out of the rider
_RANKING_NOTE = re.compile(r"\branked?\b|\branking|\bNo\. \d|\bTop (10|25)\b|\bAP (poll|had)\b", re.I)


def _note_age(when: str, ref=None) -> int:
    """Days since a note's 'Nov 8'-style date; 0 for undated/year-only notes (background facts)."""
    m = re.match(r"([A-Z][a-z]{2})[a-z]*\.? (\d{1,2})$", (when or "").strip())
    if not m:
        return 0
    try:
        today = ref or _now_ct_naive().date()
        d = dt.datetime.strptime(f"{m.group(1)} {m.group(2)} {today.year}", "%b %d %Y").date()
        if d > today:
            d = d.replace(year=today.year - 1)
        return (today - d).days
    except ValueError:
        return 0
INSIDER_TOPICS = [  # (section heading starts with, question regex)
    ("Realignment", r"\brealign\w*|\bconference (moves?|switch\w*|changes?|title games?|championship games?)\b|"
                    r"\b(conference |league )?(title|championship) games?\b|\bpac[- ]?12\b|\bsuper ?conference\w*|"
                    r"\bgroup of (5|five|6|six)\b|\bg[56]\b|\bpower (4|four)\b"),
    ("Revenue sharing", r"\brevenue[- ]shar\w*|\bhouse (settlement|case|v\.?)\b|\bnil\b|\bcollectives?\b|"
                        r"\b(salary|spending|roster|pay|revenue|nil|sec|big ten) caps?\b|\bcap (rules?|plan|policy|fines?)\b|"
                        r"\bcollege sports commission\b|\bcsc\b|\bnil go\b|\bpay(ing)? (the )?players\b|\bspending\b"),
    ("CFP format", r"\b(12|twelve|24|twenty[- ]four)[- ]team\b|\bplayoff (format|expansion|field|bracket|fight|debate)\b|"
                   r"\bcfp\b|\bcommittee\b|\bsnub\w*|\bat[- ]large\b|\bauto(matic)?[- ]?bids?\b|"
                   r"\b(big ten|b1g)\b.{0,40}\bsec\b|\bsec\b.{0,40}\b(big ten|b1g)\b|\bexpan\w+"),
    ("Coaching carousel", r"\bhot seat\b|\bfired\b|\bfiring\b|\bcarousel\b|\bcoaching (search|change|hire|job|vacanc\w*)s?\b|"
                          r"\bbuyouts?\b|\binterim\b|\b(get|gets|getting|got) (canned|axed|the axe)\b|\bwhittingham\b|"
                          r"\bkiffin\b|\bphilip rivers\b|\bryan day\b|\bnorvell\b|\bschiano\b|\bleipold\b|\bfickell\b"),
    ("Commissioners", r"\bcommissioners?\b|\bsankey\b|\bpetitti\b|\byormark\b|\bjim phillips\b|\bteresa gould\b|"
                      r"\bconference office\b|\bsunday game\b"),
    ("NCAA governance", r"\bcongress\b|\bscore act\b|\bprotect college sports\b|\bsenate\b|\bcba\b|"
                        r"\bcollective bargaining\b|\bunion\w*\b|\bemployees?\b|\bathletes\.org\b|"
                        r"\bncaa (rules?|governance|eligibility)\b|\beligibility\b"),
]
_DELLENGER_ASK = re.compile(  # bare "ross" counts when it reads like a news ask; Charlie types "delinger"
    r"\bdel+[ei]nger\b|\bross\b.{0,30}\b(news|latest|saying|say|said|report\w*|hear\w*)\b|"
    r"\b(news|latest|saying|say|said|report\w*|hear\w*)\b.{0,30}\bross\b|"
    r"\b(from|per|via) ross\b|\bross('?s)? (think\w*|take|thoughts|on the)\b|\bdoes ross think\b", re.I)
_INSIDER_STOP = {"what", "whats", "the", "and", "about", "latest", "with", "this", "that", "does", "think", "picky",
                 "college", "football", "news", "anything", "going", "who", "whos", "any", "for", "are", "you", "how",
                 "dellenger", "ross", "say", "said", "saying", "hear", "heard", "tell", "more", "there"}


def load_insider() -> dict:
    try:
        m = INSIDER_FILE.stat().st_mtime
    except OSError:
        return {}
    if _insider_memo["mtime"] == m:
        return _insider_memo["data"] or {}
    secs: dict[str, list[dict]] = {}
    cur, day = None, dt.date.fromtimestamp(m)
    try:
        for ln in INSIDER_FILE.read_text().splitlines():
            if ln.startswith("Last refreshed "):
                d = re.match(r"Last refreshed (\d{4}-\d{2}-\d{2})", ln)
                if d:
                    day = dt.date.fromisoformat(d.group(1))
            elif ln.startswith("### "):
                cur = ln[4:].strip()
                secs[cur] = []
            elif ln.startswith("## "):
                cur = None
            elif cur and "UNVERIFIED" not in ln.upper():
                mm = _INSIDER_LINE.match(ln)
                if mm:
                    when, text, src = mm.groups()
                    label = "Dellenger" if src.strip() == "Dellenger, Yahoo" else src  # mixed or podcast: name the outlets
                    secs[cur].append({"when": when, "text": text.replace('"', "").strip(), "label": label})
    except (OSError, ValueError):
        return _insider_memo["data"] or {}
    data = {"secs": secs, "day": day, "asof": f"{day:%b} {day.day}"}
    _insider_memo.update(mtime=m, data=data)
    return data


def insider_topics(question: str) -> list[str]:
    q = question or ""
    return [head for head, rx in INSIDER_TOPICS if re.search(rx, q, re.I)]


def insider_note(question: str, limit: int = 4) -> str:
    """Up to `limit` checked notes for a question about the off-field stories
    ('' when it isn't one). A bare 'what's Dellenger saying' gets the top
    Dellenger note from each section."""
    q = question or ""
    data = load_insider()
    secs = data.get("secs") or {}
    age = (_now_ct_naive().date() - data["day"]).days if data.get("day") else 0
    heads = insider_topics(q)
    dl = bool(_DELLENGER_ASK.search(q))
    if not (heads or dl):
        return ""
    wire = insider_wire(heads, dl, data.get("day") if age <= INSIDER_MAX_AGE_DAYS else None)
    if age > INSIDER_MAX_AGE_DAYS or not secs:  # refreshes kept failing: old notes would read as current news
        return (WIRE_HEAD + "\n".join(wire) + "\n" + WIRE_SAY) if wire else ""
    words = set(re.findall(r"[a-z0-9]{3,}", q.lower())) - _INSIDER_STOP
    per = max(1, limit // len(heads)) if heads else 1
    picked: list[dict] = []
    for head in heads or [h for h, _ in INSIDER_TOPICS]:
        pool = next((v for k, v in secs.items() if k.startswith(head)), [])
        if dl and not heads:  # 'what's Dellenger saying': his own bylined notes only
            pool = [n for n in pool if n["label"] == "Dellenger"]
        pool = [n for n in pool if not (_RANKING_NOTE.search(n["text"])
                                        and _note_age(n["when"], data.get("day")) > INSIDER_RANKING_MAX_DAYS)]
        ranked = sorted(range(len(pool)), key=lambda i: (
            -len(words & set(re.findall(r"[a-z0-9]{3,}", pool[i]["text"].lower()))),
            _note_age(pool[i]["when"], data.get("day")) // 3,  # newest first (Charlie 11-16: a 2-week-old ranking isn't a take)
            (pool[i]["label"] != "Dellenger") if dl else 0, i))  # file order = most important first
        picked += [pool[i] for i in ranked[:per] if pool[i] not in picked]
    picked = picked[:limit]
    if not picked:
        return (WIRE_HEAD + "\n".join(wire) + "\n" + WIRE_SAY) if wire else ""
    asof = data.get("asof") or "recently"
    return ("INSIDER NOTES (checked public reporting, as of " + asof + "; this is a real college football question, "
            "so answer it from these lines, it is not off-topic"
            + (f"; these are {age} days old, so say 'as of {asof}' with anything you take from them" if
               age > INSIDER_STALE_DAYS else "") + "):\n"
            + "\n".join(f"- {n['when']}, {n['label']}: {n['text']}" for n in picked)
            + "\nLead with the straight answer from these lines, then at most one opinionated line in your own voice. "
            "Credit 'Dellenger reported' only on lines labeled Dellenger, 'per <outlet>' on the others. In your own "
            "words: never quote anyone, never claim to be Ross Dellenger or to have talked to him, and never add a "
            "name, number or detail that isn't in these lines. A proposal is only a proposal. Live rankings, scores "
            "and standings beat these notes.") + ("\n" + WIRE_HEAD + "\n".join(wire) + "\n" + WIRE_SAY if wire else "")


WIRE_HEAD = "NEWER HEADLINES (code-pulled from the news feed since those notes; headline only):\n"
WIRE_SAY = ("A headline is all you know about these: say what the headline says with its date and 'per <outlet>' "
            "(or the byline), add nothing to it, and never call it a confirmed fact if the headline doesn't.")
_wire_memo: dict = {"mtime": None, "data": None}


def insider_wire(heads: list[str], dl: bool, since: dt.date | None, limit: int = 2) -> list[str]:
    """Up to `limit` code-pulled insider headlines (insider_refresh.wire) for the asked sections (or
    Dellenger's own for a bare 'what's Dellenger saying'), newer than the notes' refresh day and never
    older than INSIDER_WIRE_MAX_DAYS. Each line: '- <Mon D>, per <who>, <outlet>: <headline>'."""
    try:
        m = INSIDER_WIRE.stat().st_mtime
        if _wire_memo["mtime"] != m:
            _wire_memo.update(mtime=m, data=json.loads(INSIDER_WIRE.read_text()))
    except (OSError, ValueError):
        return []
    today = _now_ct_naive().date()
    out = []
    for it in (_wire_memo["data"] or {}).get("items") or []:
        try:
            d = dt.date.fromisoformat(str(it.get("date"))[:10])
        except ValueError:
            continue
        if (today - d).days > INSIDER_WIRE_MAX_DAYS or (since and d <= since):
            continue
        if heads and not set(heads) & set(it.get("sections") or [it.get("section")]):
            continue
        if not heads and dl and it.get("insider") != "Ross Dellenger":
            continue
        who = it.get("author") or it.get("insider")
        out.append(f"- {d:%b} {d.day}, per {who + ', ' if who else ''}{it.get('outlet') or 'the news feed'}: "
                   f"{str(it.get('title') or '').strip()}")
        if len(out) >= limit:
            break
    return out


# an ASK to roast someone ('roast Ackbar', 'talk shit about the SEC'), not a jab ('you talk shit for a bot')
_ROAST_VOICE = re.compile(r"\b(roast|clown on|rip on|dunk on|cook|(?<!you )(?<!u )talk (shit|trash)|trash talk|"
                          r"light (him|her|them|\w+) up)\b", re.I)


def voice_note(question: str) -> str:
    """Two message-board moves (hot_takes_bank.md 'Board voice', else
    board_voice.BOARD_MOVES) for the opinion part of a reply. Rides only on opinion /
    hot-take answers and explicit roast asks (see _user_turn; picky_brain
    drops it on banter and shots at Picky)."""
    moves = board_voice.live_moves(load_takes().get("_voice"))  # bank's current list, else the module's ten
    k = sum(map(ord, question or ""))
    pick = list(dict.fromkeys([moves[k % len(moves)], moves[(k + 3) % len(moves)]]))
    return ("BOARD VOICE (for the opinion or roast part only: sound like a message-board regular with unhinged "
            "confidence, in short punchy lines, not a rant, and never a longer reply than without it. Borrow ONE move, never its wording, and no stock "
            "catchphrase like 'let that sink in'. Facts stay exact: every fact, number and pick as given, and an "
            "opinion is never news, a report or a rumor):\n"
            + "\n".join(f"- {m}" for m in pick))


# (C) League lore (Charlie, 2026-11-14): history computed by lore.py from the
# league's own data into server/cache/lore.json (refreshed by
# com.pickem.lore-refresh). Only the members a question names ride along, in
# the user turn. Past champions are always named with respect.
LORE_FILE = Path(__file__).resolve().parent / "cache" / "lore.json"
_lore_memo: dict = {"mtime": None, "data": None}
_LORE_ASK = re.compile(
    r"\b(histor\w*|all[- ]time|ever|streaks?|last (year|season)|20(22|23|24|25)|champ\w*|titles?|won it|"
    r"defending|best weeks?|worst weeks?|perfect weeks?|winless|head[- ]to[- ]head|h2h|rival\w*|nemesis|"
    r"owns?|own(ed|ing) (him|me)|upsets?|blown|worst picks?|best picks?|lone wolf|career|legacy|splash|"
    r"past seasons?|previous seasons?|lore|remember when|biggest|lock of the week|trap game|lone wolf|top dawg|"
    r"awards?|last (8|eight)|underdog wins?)\b", re.I)
_CHAMP_ASK = re.compile(r"\b(champ\w*|titles?|won (it|the (league|pool|whole thing))|who won last (year|season)|"
                        r"defending|past winners?|history)\b", re.I)


def load_lore() -> dict | None:
    try:
        m = LORE_FILE.stat().st_mtime
    except OSError:
        return None
    if _lore_memo["mtime"] != m:
        try:
            _lore_memo["data"], _lore_memo["mtime"] = json.loads(LORE_FILE.read_text()), m
        except (OSError, ValueError):
            return _lore_memo["data"]
    return _lore_memo["data"]


def lore_names(ctx: dict, question: str) -> list[str]:
    """Members a question names: team name, or a unique last word of a
    multi-word team name ('Culligan', 'Drolet')."""
    q = question or ""
    lore = load_lore() or {}
    teams = list(dict.fromkeys([r["team"] for r in ctx.get("standings_ytd") or []] + list(lore.get("members") or {})))
    last = collections.Counter(t.split()[-1].lower() for t in teams if " " in t)
    out = []
    for t in teams:
        alias = t.split()[-1] if " " in t and last[t.split()[-1].lower()] == 1 and len(t.split()[-1]) >= 4 else None
        if re.search(rf"(?<!\w){re.escape(t)}(?!\w)", q, re.I) or (
                alias and re.search(rf"(?<!\w){re.escape(alias)}(?!\w)", q, re.I)):
            out.append(t)
    me = (ctx.get("you") or {}).get("team_name")
    if me and me not in out and re.search(r"\b(me|my|i|i'?ve|myself)\b", q, re.I) and _LORE_ASK.search(q):
        out.append(me)
    return out


def lore_note(ctx: dict, question: str) -> str:
    lore = load_lore()
    if not lore:
        return ""
    q = question or ""
    if _BODY.search(q) or (_TRAIT.search(q) and _MOCK.search(q)):
        return ""  # baits get the guardrail, never history
    names = lore_names(ctx, q)
    champ_lines = [ln for t in names for ln in (lore["members"].get(t) or []) if "past league champion" in ln]
    asks = bool(_LORE_ASK.search(q))
    roast = bool(_MOCK.search(q)) and not _TRAIT.search(q) and not _BODY.search(q)
    lines: list[str] = []
    league = lore.get("league") or []
    if re.search(r"\bnational|title game|\bcfp\b|heisman", q, re.I) and not names:
        return ""  # 'Freeman took ND to the national title game' isn't league history
    if _CHAMP_ASK.search(q) and not re.search(r"\b(standings|wins|records?|finish\w*|place|splash|20(22|23|24))\b", q, re.I):
        lines += [ln for ln in league if "champion" in ln.lower()]  # just the champions: the tables confused it
    elif _CHAMP_ASK.search(q) or (asks and not names):
        lines += league
    cur = re.search(r"\bthis (season|year)\b|\b2026\b|\bso far\b", q, re.I)
    prior = re.search(r"\blast (season|year)\b|\b20(22|23|24|25)\b|\bcareer\b|\bever\b|\ball[- ]time\b", q, re.I)
    for t in names[:2]:
        mine = lore["members"].get(t) or []
        # season asked about only: the 9B grabbed the 2025 line ('worst week 0-4') for 'this season'
        if cur and not prior:
            mine = [ln for ln in mine if not re.match(rf"{re.escape(t)}, 20(22|23|24|25) season|{re.escape(t)}, splash", ln)]
        elif prior and not cur:
            mine = [ln for ln in mine if " so far (through" not in ln]
        mine = sorted(mine, key=lambda ln: 0 if " so far (through" in ln else 1)  # current season first
        if asks:
            lines += mine
        elif roast:  # callbacks: champion status, one season line, one blown/lone-wolf miss
            lines += [ln for ln in mine if "past league champion" in ln]
            lines += [ln for ln in mine if "worst blown" in ln or "lone-wolf misses" in ln][:1]
        else:
            lines += [ln for ln in mine if "past league champion" in ln]
    if len(names) >= 2:
        a, b = names[0], names[1]
        hh = (lore.get("h2h") or {}).get(a, {}).get(b)
        if hh:
            lines.append(f"Head to head, {a} vs {b} (games they took opposite sides, final): {a} {hh[0]}-{hh[1]}.")
    cw = ctx.get("current_week")
    if isinstance(cw, int):  # weekly awards: only the week asked about ("last week" = the week before this one)
        want = cw - 1 if re.search(r"\blast week\b", q, re.I) else cw if re.search(r"\bthis week\b", q, re.I) else None
        if want is not None:
            lines = [ln for ln in lines if not ln.startswith("Awards for ") or re.search(rf"\bweek {want}:", ln)]
    if not freshness.usable("lore", lore.get("fetched_at") or _lore_memo["mtime"]):
        lines = [ln for ln in lines if " so far (through" not in ln and not ln.startswith("Awards for ")]
    lines = list(dict.fromkeys(lines or champ_lines))
    if not lines:
        return ""
    return (f"LEAGUE LORE (computed from the league's own records, as of {lore.get('as_of')}; this is all the "
            "league history you know: use these facts as written, never invent other history, and for this "
            "season's record and place use the live standings):\n" + "\n".join(f"- {ln}" for ln in lines))


# Standings questions ("who's winning the pool?") got pulled into this week's
# pick results by RESULTS_ASK's 'winning'. The answer is computed here and
# rides nearest the question; the results block is skipped for them.
_STANDINGS_Q = re.compile(
    r"\bwho('?s| is| are)? (winning|leading|in (first|1st|the lead|last)|on top|at the top|at the bottom|last)\b"
    r"|\b(standings|leaderboard|what place|which place|where do i (stand|rank)|my (place|rank)|who('?s| is) first)\b"
    r"|\bwinning (the|this) (pool|league)\b|\bhow far (behind|back|ahead|out)\b|\b(behind|ahead of) (first|the lead)\b"
    r"|\bgap\b|\bhow many (misses|points|wins|hits|losses)\b|\bseason record\b|\boverall record\b"
    r"|\bhow (am|'?m) i doing\b|\bhow('?s| is) my season\b", re.I)


_DOING_Q = re.compile(r"\bhow('?s| is| am| are)\b.{0,30}\bdoing\b|\bthis season\b|\bon the season\b", re.I)


def standings_answer(ctx: dict, question: str) -> str:
    st = ctx.get("standings_ytd") or []
    q0 = question or ""
    named0 = any(re.search(rf"(?<!\w){re.escape(r['team'])}(?!\w)", q0, re.I) for r in st)
    doing = bool(_DOING_Q.search(q0)) and (named0 or _SELF_WORDS.search(q0)) and not _LORE_ASK.search(q0)
    if not st or not (_STANDINGS_Q.search(q0) or doing) or re.search(r"\bthis week\b|\bweek \d+", q0, re.I):
        return ""
    me = (ctx.get("you") or {}).get("team_name")
    you = lambda t: f"{t} (the user, 'you')" if t == me else t  # noqa: E731
    first = [r for r in st if r["rank"] == 1]
    nxt = next((r for r in st if r["rank"] > 1), None)
    lastr = [r for r in st if r["rank"] == st[-1]["rank"]]
    lines = ["SEASON STANDINGS ANSWER (points = covered picks, all season; answer from this, not from this "
             "week's games):",
             "- First place: " + ", ".join(f"{you(r['team'])} {r['w']}-{r['l']}" for r in first)
             + (f", {first[0]['w'] - nxt['w']} point{'s' * (first[0]['w'] - nxt['w'] != 1)} ahead of "
                + ", ".join(r["team"] for r in st if r["rank"] == nxt["rank"]) + f" ({nxt['w']})" if nxt else "") + ".",
             "- Last place: " + ", ".join(f"{you(r['team'])} {r['w']}-{r['l']}" for r in lastr) + "."]
    row = next((r for r in st if r["team"] == me), None)
    named = [r for r in st if r["team"] != me and re.search(rf"(?<!\w){re.escape(r['team'])}(?!\w)", question or "", re.I)]
    if row and re.search(r"\bhow far|\bgap\b|\bbehind\b|\bahead\b", question or "", re.I):
        g0 = st[0]["w"] - row["w"]
        lines.append(f"- THE ANSWER: the user is {g0} point{'s' * (g0 != 1)} behind first place. Say that number.")
    for r in ([row] if row else []) + named[:2]:
        who = f"The user ({me})" if r is row else r["team"]
        gap = st[0]["w"] - r["w"]
        lines.append(f"- {who}: {_ordinal(r['rank'])} place, {r['w']} points (hits) and {r['l']} misses this season"
                     + (f", {gap} point{'s' * (gap != 1)} behind first" if gap else ", in first") + ".")
    return "\n".join(lines)


# Casino (Charlie, 2026-11-14: "knows all the casino stuff and balances", "make
# fun of people like 'hey this guy hit on an 18'"). lore.py computes chips (public
# on the Tiebreakers board), settled hands (shown to all once a week settles),
# blunders and slots records into the lore cache; only on casino questions.
_CASINO_ASK = re.compile(r"\b(blackjack|the pit|pit|chips?|bankrolls?|casino|slots?|busted|bust(s|ing)?|"
                         r"doubled|double down|hit on|stood on|stand on|gambl\w*|degenerate|card sharks?|hands?)\b",
                         re.I)


def casino_note(ctx: dict, question: str) -> str:
    q = question or ""
    lore = load_lore() or {}
    cas = lore.get("casino") or {}
    if not cas or not _CASINO_ASK.search(q) or _BODY.search(q) or (_TRAIT.search(q) and _MOCK.search(q)):
        return ""
    if re.search(r"\bhands?\b", q, re.I) and not re.search(r"blackjack|pit|chips?|cards?|dealt|bust|double|hit|stood", q, re.I):
        return ""  # 'hands' alone is too vague
    names = lore_names(ctx, q)
    me = (ctx.get("you") or {}).get("team_name")
    if me and me not in names and re.search(r"\b(me|my|i|i'?m|mine)\b", q, re.I):
        names.append(me)
    lines = list(cas.get("league") or [])
    for t in names[:2]:
        lines += cas.get("members", {}).get(t) or [f"{t} has no blackjack or slots history this season."]
    return (f"CASINO (The Pit blackjack + slots, computed from the site's data as of {lore.get('as_of')}; chips "
            "only break ties, they're never money or points). Use these facts as written; a blunder roast is fair "
            "game, but never invent a hand:\n" + "\n".join(f"- {ln}" for ln in lines))


# (D) Game analysis (Charlie, 2026-11-14): a real breakdown for upcoming pool
# games. Every signal and the lean are computed here (market vs pool line,
# SP+ projection, FPI); the model explains them. Picky's own auto-picks stay
# in picky_picks.py, untouched.
SP_HOME_FIELD = 2.5  # same as picky_picks.HOME_FIELD
_ANALYSIS_ASK = re.compile(r"\b(break(ing)? ?down|breakdown|analy[sz]\w*|preview|matchup|lean|who do you like|"
                           r"who you got|who'?s the (play|pick)|best bet|should i take|which side|thoughts on|"
                           r"edge in|who wins)\b", re.I)


def matchup_signals(g: dict, notes: dict | None, idx: dict | None, extras: dict | None = None) -> dict:
    """Every computed signal for one pool game and the weighted lean. Weights
    and thresholds are documented in edge.py. `extras` is edge.load()'s output
    (splits, weather, dome), fetched only for breakdown questions. Dossier and
    insider intel never come in here: they explain, they don't move the lean."""
    away, home, line = g["away"], g["home"], float(g.get("line") or 0)
    extras = extras or {}
    sig = []
    mg = _market_gap(notes or {}, line)
    if mg is not None:
        gap = mg[1]
        if abs(gap) >= 0.5:
            side = home if gap > 0 else away
            w = edge.W_MARKET_BIG if abs(gap) >= edge.MARKET_BIG_GAP else edge.W_MARKET
            sig.append({"side": side, "w": w, "why": f"market: the betting market rates {side} {_num(abs(gap))} "
                                                     "points better than the pool line (value side)"})
        else:
            sig.append({"side": None, "w": 0, "why": "market: the pool line matches the market, no value either way"})

    def by_margin(home_margin, src, w):
        cover = home_margin + line
        if abs(cover) < 0.5:
            return {"side": None, "w": 0, "why": f"{src}: projects it right on the pool line, no edge"}
        side = home if cover > 0 else away
        fav = home if home_margin >= 0 else away
        return {"side": side, "w": w, "why": f"{src}: projects {fav} by {_num(round(abs(home_margin), 1))}, so "
                                             f"{side} covers the pool line by {_num(round(abs(cover), 1))}"}
    edges: list[dict] = []
    if idx:
        ta, th = cfbd.resolve_team(away, idx), cfbd.resolve_team(home, idx)
        sa, sh = ((ta or {}).get("sp") or {}).get("rating"), ((th or {}).get("sp") or {}).get("rating")
        if sa is not None and sh is not None:
            sig.append(by_margin(float(sh) - float(sa) + SP_HOME_FIELD, "SP+ (ratings + 2.5 home field)", edge.W_SP))
        em = edge.efficiency_margin(ta, th)
        if em is not None:
            sig.append(by_margin(em, "efficiency (season PPA per play + 2.5 home field)", edge.W_EFF))
        us, edges = edge.units_signal(away, home, idx)
        if us:
            sig.append(us)
    if g.get("fpi_margin") is not None:
        sig.append(by_margin(float(g["fpi_margin"]), "ESPN FPI", edge.W_FPI))
    for x in (edge.move_signal(away, home, edge.line_move(notes, away, home, idx)),
              edge.splits_signal(away, home, (extras.get("splits") or {}).get(g.get("id"))),
              edge.weather_signal(away, home, line, (extras.get("weather") or {}).get(g.get("id")),
                                  bool((extras.get("dome") or {}).get(g.get("id"))))):
        if x:
            sig.append(x)
    score = collections.Counter()
    for x in sig:
        if x["side"]:
            score[x["side"]] += x["w"]
    lean, a, b = None, 0, 0
    if score:
        (top, a), *rest = score.most_common()
        b = rest[0][1] if rest else 0
        lean = top if a > b else None
    strength = "strong" if lean and a >= 4 and b <= a / 3 else "lean" if lean else "none"
    return {"signals": sig, "lean": lean, "strength": strength, "edges": edges[:3],
            "score": {k: v for k, v in score.items()}}


def analysis_note(ctx: dict, question: str) -> str:
    games = analysis_games(ctx, question)
    if not games:
        return ""
    idx = _cfbd_index()
    notes_all = ctx.get("espn") or {}
    out = ["MATCHUP BREAKDOWN (computed from real data; this is advice, not a lock). For each game say the LEAN "
           "in your own voice in two or three plain sentences: the strongest one or two reasons FOR it (at least "
           "one that isn't FPI or the line), plus the best reason against if there is one. Use only these "
           "reasons. No headings, no bullets, no labels like FOR or NO EDGE, don't copy these lines word for word:"]
    for g in games[:4]:
        m = matchup_signals(g, notes_all.get(g["id"]), idx, ctx.get("edge"))
        out.append(f"{g['away']} at {g['home']}, pool line {pool_line(g['away'], g['home'], g.get('line'))}:")
        out.append(f"  LEAN: {m['lean']} ({m['strength']})" if m["lean"] else
                   "  LEAN: none, the signals split or show no edge: say it's a coin flip and why")
        if not m["signals"]:
            out.append("  - no market, SP+ or FPI data for this game")
        sides = [m["lean"]] if m["lean"] else []
        sides += [t for t in (g["away"], g["home"]) if t not in sides]
        for t in sides:
            rows = [x["why"] for x in sorted(m["signals"], key=lambda x: -x["w"]) if x["side"] == t]
            if rows:
                out.append(f"  FOR {t}: " + "; ".join(rows))
        flat = [x["why"] for x in m["signals"] if not x["side"]]
        if flat:
            out.append("  NO EDGE either way: " + "; ".join(flat))
        sp_edges = cfbd.matchup_edges(g["away"], g["home"], idx) if idx else None
        if sp_edges:
            out.append(f"  SP+ unit ranks: {sp_edges}")
        for e in m["edges"]:
            out.append(f"  unit edge: {e['text']}")
    return "\n".join(out)


def analysis_games(ctx: dict, question: str) -> list[dict]:
    """Open pool games a breakdown question is about ([] if it isn't one)."""
    q = question or ""
    if not _ANALYSIS_ASK.search(q) or ctx.get("error"):
        return []
    if week_locked(ctx):
        return []  # picks are locked: no 'take this side' advice (the locked-week note covers it)
    now = _now_ct_naive()
    games = [g for g in ctx.get("games_this_week") or [] if not g.get("winner") and not g.get("tiebreaker")
             and not _game_is_locked({"Start (CT)": g.get("start"), "Status": g.get("status")}, now)]
    named = games_named(q, [{"Away": g["away"], "Home": g["home"], "id": g["id"]} for g in games])
    if named:
        games = [g for g in games if g["id"] in {n["id"] for n in named}]
    return games


# (E) Game dossiers (dossier.py + LaunchAgent com.pickem.picky-dossier, 2026-11-15):
# public injury / availability / insider notes per pool game. Only for a
# question about a specific game, only as color: matchup_signals never reads
# it, so it can't move the lean. URLs stay in the file; members never see them.
DOSSIER_DIR = Path(os.environ.get("PICKY_DOSSIER_DIR", Path(__file__).resolve().parent / "cache" / "dossiers"))
DOSSIER_MAX_ITEMS = 8


def load_dossier(week, game_id) -> dict | None:
    try:
        d = json.loads((DOSSIER_DIR / str(week) / f"{game_id}.json").read_text())
        gen = dt.datetime.fromisoformat(d["generated_at"])
    except (OSError, ValueError, KeyError):
        return None
    if not freshness.usable("dossier", gen.timestamp()):
        return None
    return d


def dossier_games(ctx: dict, question: str) -> list[dict]:
    games = ctx.get("games_this_week") or []
    named = games_named(question or "", [{"Away": g["away"], "Home": g["home"], "id": g["id"]} for g in games], cap=2)
    ids = [n["id"] for n in named]
    return [g for g in games if g["id"] in ids and g.get("status") != "final"]


def _intel_line(c: dict, label: str) -> str:
    try:
        day = dt.date.fromisoformat(c["date"]).strftime("%b %-d")
    except (KeyError, ValueError):
        day = ""
    src = c.get("source_name") or ""
    src = src if src and "." not in src and "/" not in src else ""
    tag = ", ".join(x for x in (day, src) if x)
    return f"  - {label}{f' ({tag})' if tag else ''}: {c['text']}"


def _status(c: dict) -> str:
    return c.get("status") or dossier.infer_status(c.get("text") or "")


def intel_status(ctx: dict, question: str) -> dict[str, str]:
    """{player last name (normalized): reported status} for the injury claims
    in the GAME INTEL this question gets."""
    out = {}
    for g in dossier_games(ctx, question):
        d = load_dossier(ctx.get("current_week"), g["id"]) or {}
        for c in d.get("facts") or []:
            if c.get("kind") != "injury":
                continue
            for pl in c.get("players") or []:
                last = cfbd.norm(re.sub(r"\b(jr|sr|ii|iii|iv)\.?$", "", pl.strip(), flags=re.I)).split()
                if last and len(last[-1]) >= 3:
                    out[last[-1]] = _status(c)
    return out


def dossier_note(ctx: dict, question: str, limit: int | None = None) -> str:
    """limit: only the first N facts (injuries first), no opinions or rumors,
    for when a game card or news rider already carries the story."""
    if ctx.get("error") or not (question or "").strip():
        return ""
    out = []
    for g in dossier_games(ctx, question):
        d = load_dossier(ctx.get("current_week"), g["id"])
        if not d:
            continue
        facts = d.get("facts") or []
        items = ([_intel_line(c, f"injury report, status {_status(c).upper()}") for c in facts
                  if c.get("kind") == "injury"]
                 + [_intel_line(c, "news") for c in facts if c.get("kind") != "injury"])[:limit or DOSSIER_MAX_ITEMS - 2]
        if limit is None:
            items += [_intel_line(c, "opinion, not fact") for c in (d.get("opinions") or [])[:2]]
            items += [_intel_line(c, "RUMOR, unconfirmed") for c in (d.get("rumors") or [])[:1]]
        try:
            asof = dt.datetime.fromisoformat(d["generated_at"]).strftime("%a %b %-d")
        except (KeyError, ValueError):
            asof = "recently"
        head = f"GAME INTEL, {g['away']} at {g['home']} (public reports gathered {asof}):"
        if not items:
            out.append(head + "\n  - no injury or availability news found in public reports.")
            continue
        out.append("\n".join([head] + ([f"  Summary: {d['summary']}"] if d.get("summary") else []) + items))
    if not out:
        return ""
    return ("\n".join(out) + "\nIntel is color only: it never changes the LEAN or the value side. Say 'reportedly' "
            "or 'per reports' for intel items, give the date when it matters, never give links, and only call a "
            "rumor a rumor. Name only players listed here or elsewhere above.")


# Analyst takes + league material (2026-11-15). Claude does the heavy thinking
# ahead of time (analyst.py via the dossier job; league_material.py, private)
# and Picky's small model delivers it. Both are AMMO, never a script: the
# blocks say so, and the grounding filter sees them (they're in the prompt).
# PICKY_AMMO=0 turns both off (the exam's "before" runs).
AMMO_ON = os.environ.get("PICKY_AMMO", "1") != "0"
TALK_LIKE_A_PERSON = ("Answer the question first: they rely on you. A quick jab is fine, then the real answer, "
                      "then the fun. Talk like a buddy in a group text: react to what they actually said, pick the one or two "
                      "best angles (never all of them), contractions, short sentences, opinionated. Never read out "
                      "labels or structure (no 'reason one', no 'confidence', no 'counter-argument', no lists, no "
                      "headers), and no stock openers like 'Great question' or 'Here's the breakdown'.")
_GAME_OPINION_ASK = re.compile(r"\b(think|thoughts?|like|take|lean|play|pick|feel|confident|cover|value|win|"
                               r"who you got|who'?s got|fade|hammer|lock|sleeper|upset|break(ing)? ?down|"
                               r"breakdown|preview|matchup|analy[sz]\w*|chances?|smash|spot|read|covers?|covering|good|"
                               r"bad|trap|legit|real|free money|overrated|underrated|worth|bet|safe|risky|sure|"
                               r"should i|would you|or)\b", re.I)
# a short text naming a pool game with no fact word is a read ask: 'so lsu?', 'ok and texas tech', 'lsu or tenn'
_GAME_FACT_ASK = re.compile(r"\b(line|spread|o/?u|over.?under|totals?|score|kick\w*|what time|when|injur\w*|hurt|qb|"
                            r"quarterback|tv|channel|weather|records?|ranked|rank\w*|where|stadium|odds)\b", re.I)


# A slate question ('gimme a read on this week's games', 'who should I pick', 'what do you like this week',
# 'picks?') gets every open game's card (live 2026-11-16: both of Charlie's slate asks matched no game, so
# Picky only listed the games, then called a pre-lock gap 'the only value side').
_SLATE_ASK = re.compile(
    r"\b(read|thoughts?|takes?|picks?|leans?|plays?|like|liking|bets?|advice|breakdown)\b.{0,30}?\b(this|the|these|"
    r"today'?s|saturday'?s|week'?s|weekend'?s)\s+(weeks?'?s?\s+|weekends?'?s?\s+)?(games?|slate|board|week|weekend)\b"
    r"|\bwho (should|do|would|will) (i|we|you|u) (pick|take|go with|play|like|back)\b"
    r"|\bwhat (do|would|should) (you|u|i|we) (like|take|pick|play)\b(?!.{0,25}\b(in|on|with|for)\b)"
    r"|\b(gimme|give me|got any|any|your|ur) (picks?|plays?|leans?)\b|^\W*(picks?|plays?|leans?)\W*$"
    r"|\b(gimme|give me|what are|whats|what'?s) (all|your|ur|the)? ?(4|four)? ?(picks|plays|leans)\b"
    # keeper k1/k2: 'gimme all 4 w reasons', 'give me the slate', 'ur 4 picks', 'picks for all 4', 'run me thru the games'
    r"|\b(gimme|give me|hit me with|run me (thru|through)|go through|walk me (thru|through))\s+(all|the|ur|your)?\s*(4|four|slate|games|board)\b"
    r"|\b(ur|your|all) (4|four) (picks|plays|calls|games)\b|\bpicks? for (all|every) (4|four|games?)\b"
    , re.I)
# 'whats ur lock' / 'best bet this wk' / 'lock of the week': ONE play, the slate's strongest (spar r1 2026-11-16:
# 'ok whats ur lock then' got all four of Picky's own exhibition picks, all favorites, called 'my locks')
_BEST_ASK = re.compile(
    r"\b(what'?s|whats|who'?s|whos|gimme|give me|got a|your|ur|you got a|u got a|any)\s+(best )?(lock|bet|play)\b"
    r"(?!\s*(time|deadline|date|at|is at|happen))"
    r"|\block of the (week|wk|weekend)\b|\bbest (bet|play|pick)\b|\b(top|favorite|fav) (play|pick)\b"
    r"|\bwhat'?s the (lock|play)\b(?!\s*(time|deadline|date|at))|\bsafest (pick|play|bet)\b", re.I)


# a shot at Picky's picks isn't a request for them (sparring k1: 'tg says ur picks are worse than his', 'bo nix > ur picks')
_PICKS_SHOT = re.compile(r"\b(ur|your|his) picks? (are|r|is|suck|stink|blow|been|was|were)\b|[<>]\s*(ur|your) picks?"
                         r"|\bthan (ur|your|his) picks?|\b(worse|better) than\b", re.I)


def slate_ask(question: str) -> bool:
    q = question or ""
    return bool(_SLATE_ASK.search(q) or _BEST_ASK.search(q)) and not _PICKS_SHOT.search(q)


def best_ask(question: str) -> bool:
    """A single-play ask ('whats ur lock', 'best bet this wk'): the slate rider names only the strongest play."""
    return bool(_BEST_ASK.search(question or ""))


def take_games(ctx: dict, question: str) -> list[dict]:
    """Open pool games whose analyst take this question should get: every
    game a breakdown or slate question covers, or the 1-2 games it names when it asks
    for an opinion (not 'what's the line', not 'who's hurt')."""
    q = question or ""
    if not AMMO_ON or ctx.get("error") or not q.strip() or week_locked(ctx):
        return []
    ag = analysis_games(ctx, q)
    if ag:
        return ag[:4]
    if _INJURY_ASK.search(q):
        return []
    now = _now_ct_naive()
    games = [g for g in ctx.get("games_this_week") or [] if not g.get("winner") and not g.get("tiebreaker")
             and not _game_is_locked({"Start (CT)": g.get("start"), "Status": g.get("status")}, now)]
    ids = [n["id"] for n in games_named(q, [{"Away": g["away"], "Home": g["home"], "id": g["id"]} for g in games],
                                        cap=2)]
    if not ids and slate_ask(q):
        return games[:4]
    if not _GAME_OPINION_ASK.search(q) and not (ids and len(q.split()) <= 6 and not _GAME_FACT_ASK.search(q)):
        return []
    return [g for g in games if g["id"] in ids]


def _take_line(g: dict, t: dict, full: bool) -> list[str]:
    side = _pick_side(g, t["pick"])
    out = [f"ANALYST TAKE, {g['away']} at {g['home']} (Picky's film room did the homework; ammo, not a script):"]
    reasons = [r["text"] for r in t.get("reasons") or []]
    if not t.get("agrees_with_lean"):
        # the computed lean stays the pick; the film room's case is the other side, said out loud
        lean_side = _pick_side(g, t["lean"])
        out.append(f"  YOUR PICK STAYS {lean_side} (your computed LEAN, reasons in the breakdown). But the film "
                   f"room disagrees and would take {side}: say that honestly as the other side, don't hide it.")
        out.append(f"  The film room's case for {t['pick']}: " + " / ".join(reasons[: (2 if full else 1)]))
        if full and t.get("counter"):
            out.append(f"  Even the film room admits (this backs your pick): {t['counter']['text']}")
        return out
    out.append(f"  The play: {side}, {t['confidence']} confidence."
               + ("" if t.get("lean") else " (your computed signals call it close; this is the tiebreak)"))
    out.append("  Why: " + " / ".join(reasons[: (5 if full else 2)]))
    if full and t.get("counter"):
        out.append(f"  Best case against it: {t['counter']['text']}")
    if full and t.get("market_missing"):
        out.append(f"  What the line is missing: {t['market_missing']['text']}")
    if t.get("quotable"):
        out.append(f"  A line you can use as is: \"{t['quotable']}\"")
    return out


def _pick_side(g: dict, team: str) -> str:
    try:
        ln = float(g.get("line"))
    except (TypeError, ValueError):
        return team
    if ln == 0:
        return f"{team} (pick'em)"
    return _side_label(team, g["home"], ln)


def take_note(ctx: dict, question: str) -> str:
    games = take_games(ctx, question)
    out = []
    for g in games:
        d = load_dossier(ctx.get("current_week"), g["id"]) or {}
        t = d.get("analyst_take")
        if not t or not t.get("pick") or not t.get("reasons"):
            continue
        out += _take_line(g, t, full=len(games) <= 2)
    if not out:
        return ""
    return "\n".join(out) + ("\nDeliver it in your voice and stay faithful to the pick and the reasons: say the "
                             "side you like plainly ('I like', 'I'd take', 'give me', in your own words; never 'my pick' "
                             "or 'I took', those mean your real league picks; never 'you got'), by team name and number "
                             "(your LEAN wherever it says YOUR PICK STAYS), back it "
                             "with the best one or two reasons, add the other side only if it fits. Teams go by the "
                             "names written here, no nicknames. Don't add stats or names that aren't "
                             "in this prompt. " + TALK_LIKE_A_PERSON)


LEAGUE_MATERIAL_FILE = Path(os.environ.get("PICKY_LEAGUE_MATERIAL",
                                           "/Users/Agent/cfb-pickem-private/league_material.json"))
_material_cache: tuple[float, dict] | None = None
_STORY_ASK = re.compile(r"\b(storylines?|stories|drama|what'?s (going on|happening|new)|recap|catch me up|"
                        r"league news|tea|gossip|who'?s (hot|cold)|hot streak|cold streak|collapse|choke|"
                        r"rivalr\w*|race|narratives?|state of the league)\b", re.I)
_ROAST_ASK = re.compile(r"\b(roast|trash|clown|rip|burn|mock|dunk on|cook|talk (shit|trash)|thoughts on|"
                        r"what about|how'?s|how is|doing|think of|opinion|praise|hype|props|credit|"
                        r"rate|scouting report|tell me about)\b", re.I)


def load_material() -> dict | None:
    global _material_cache
    try:
        mt = LEAGUE_MATERIAL_FILE.stat().st_mtime
    except OSError:
        return None
    if _material_cache and _material_cache[0] == mt:
        return _material_cache[1]
    try:
        d = json.loads(LEAGUE_MATERIAL_FILE.read_text())
    except (OSError, ValueError):
        return None
    _material_cache = (mt, d)
    return d


def _material_fresh(ctx: dict, team: str, rec) -> bool:
    """A member's lines are usable only while his season record still matches
    the live standings (a game went final since = wait for the refresh)."""
    row = next((r for r in ctx.get("standings_ytd") or [] if r["team"] == team), None)
    return bool(row and rec and [row["w"], row["l"]] == list(rec))


def material_records(ctx: dict) -> dict[str, set]:
    """{member: W-Ls that are true about him} from the league material facts
    (favorites/underdogs splits, head to head, last 8): record_check lets
    these through so valid material lines aren't stripped as fake season records."""
    mat = load_material() if AMMO_ON else None
    if not mat or mat.get("season") != ctx.get("season"):
        return {}
    out: dict[str, set] = {}
    for t, m in (mat.get("members") or {}).items():
        if not _material_fresh(ctx, t, m.get("record")):
            continue
        out[t] = {r for x in (mat.get("facts") or {}).values()
                  if x.startswith(t + " ") or x.startswith(t + ":") or x.startswith(t + "'")
                  for r in re.findall(r"\b\d{1,2}-\d{1,2}\b", x)}
    return out


def oddity_ok(team: str | None, rec: str, sent: str) -> bool:
    """A W-L that's one of `team`'s lore 'stats-page oddity' splits ('2-7 in Oklahoma games', '1-5 picking
    against them', '9-20 on road teams'), in a sentence that names the same school or split: a true split,
    not a fake season or week record."""
    for ln in ((load_lore() or {}).get("members") or {}).get(team or "") or []:
        if "stats-page oddity: " not in ln or not re.search(rf"(?<![\d-]){re.escape(rec)}(?![\d-])", ln):
            continue
        keys = re.findall(r"\b(?:in|taken|against) ([A-Z][\w&'. ]+?) (?:games|\d+ times)", ln) + \
            re.findall(r"\bon (road teams|home teams|underdogs|favorites)\b", ln)
        if any(re.search(rf"(?<!\w){re.escape(k)}(?!\w)", sent, re.I) for k in keys) or \
                re.search(r"\b(road|underdogs?|dogs?)\b", sent, re.I) and re.search(r"road|underdogs", ln):
            return True
    return False


def material_note(ctx: dict, question: str) -> str:
    if not AMMO_ON or ctx.get("error") or not (question or "").strip():
        return ""
    mat = load_material()
    if not mat or mat.get("season") != ctx.get("season"):
        return ""
    q = question or ""
    members = mat.get("members") or {}
    names = [n for n in lore_names(ctx, q) if n in members]
    me = (ctx.get("you") or {}).get("team_name")
    if me in members and me not in names and re.search(r"\b(me|my|i|i'?m|myself)\b", q, re.I) and (
            _ROAST_ASK.search(q) or re.search(r"\bhow (am|'?m) i\b", q, re.I)):
        names.append(me)
    out = []
    if names and (_ROAST_ASK.search(q) or _STORY_ASK.search(q) or len(names) == 1):
        for n in names[:2]:
            m = members[n]
            if not _material_fresh(ctx, n, m.get("record")):
                continue
            if n == me and charlie_nice_caller(ctx):
                continue  # no roast ammo on Chuck when he's the one asking
            lines = list(m.get("lines") or [])
            if any(x.get("kind") == "leader" for x in lines):  # the Dear Leader: state media unless he's asking
                lines = [x for x in lines if (x.get("kind") == "leader") != leader_is_caller(ctx)]
            random.shuffle(lines)
            if not lines:
                continue
            who = "the user (say 'you')" if n == me else n
            out.append(f"LEAGUE MATERIAL on {who} (true, checked against the standings; optional ammo):")
            out += [f"  - {x['text']}  [facts: {'; '.join(x.get('facts') or [])}]" for x in lines[:3]]
    fresh_all = all(_material_fresh(ctx, t, m.get("record")) for t, m in members.items())
    if _STORY_ASK.search(q) and fresh_all and mat.get("storylines"):
        out.append("LEAGUE STORYLINES this week (true; optional ammo):")
        out += [f"  - {x['text']}  [facts: {'; '.join(x.get('facts') or [])}]" for x in mat["storylines"][:3]]
    pn = mat.get("picky_narrative")
    if pn and _PICKY_ASK.search(q) and not names and fresh_all:
        out.append(f"YOUR OWN SEASON STORY (true; optional ammo): {pn['text']}  [facts: {'; '.join(pn.get('facts') or [])}]")
    if not out:
        return ""
    return "\n".join(out) + ("\nUse at most one or two of these, in your own words, only if they fit what was asked; "
                             "keep the numbers exactly as written and the running bits as they are. "
                             + TALK_LIKE_A_PERSON)


# League playoff race (site audit #2, stats.html Playoff Race): top 4 after
# week 15 make the playoffs. Cut line, gaps, bubble and points still
# available are computed here.
PLAYOFF_SPOTS = league_rules.PLAYOFF_SPOTS
_RACE_ASK = re.compile(r"\b(bubble|cut ?line|top (4|four)|make (the )?playoffs?|playoff (spot|race|chances?|line|picture|hopes?)|"
                       r"in the playoffs|eliminated|clinch\w*|still alive|mathematically)\b", re.I)


_PLAYOFF_BOARD_ASK = re.compile(
    r"\b(ended today|if the season ended|ends today|who'?s in\b|who would (make|get in)|projected (field|seeds?)|"
    r"(current|playoff) (seeds?|field|board|standings)|seeds?|seeded|playoff (race|picture|spots?)|make (the )?playoffs?|"
    r"in the playoffs|cut ?line|bubble)\b", re.I)
_STANDINGS_WORD = re.compile(r"\b(standings|leader ?board|who'?s (winning|leading|in first|on top)|in first)\b", re.I)


def playoff_note(ctx: dict, question: str) -> str:
    """Site audit #7. Playoff mode (seed rows exist): the Playoff board, scores
    reset, playoff games only. Before that: the site's own playoff_compute
    (the autopilot's last preview) for "who's in if it ended today"."""
    q = question or ""
    if re.search(r"\b(cfp|committee|college football playoff|ranked|rankings?)\b", q, re.I):
        return ""
    board = ctx.get("playoff_board") or []
    if board:
        if not (_PLAYOFF_BOARD_ASK.search(q) or _STANDINGS_WORD.search(q) or re.search(r"\bplayoffs?\b", q, re.I)):
            return ""
        rows = "\n".join(f"- {r['rank']}. {r['team']} (seed {r['seed']}) {r['w']}-{r['l']}" for r in board)
        return ("PLAYOFF BOARD (the league is in the playoffs: only these 4 seeded teams are alive, scores reset, "
                f"only playoff games count; regular-season points no longer matter except as a playoff tiebreaker):\n{rows}")
    pv = ctx.get("playoff_preview")
    if not pv or not _PLAYOFF_BOARD_ASK.search(q):
        return ""
    quals = "\n".join(f"- seed {x['seed']}: {x['team']}, {x['points']} pts"
                       + (f" (ahead of the next team on {x['decided_by']})" if x.get("decided_by") not in (None, "regular season points") else "")
                       for x in pv["qualifiers"])
    cut = pv.get("cut_tie") or {}
    tie = (f"\nTie at the cut: {', '.join(cut.get('teams') or [])} at {cut.get('points')} pts; "
           + (f"settled by {cut['decided_by']}." if cut.get("decided_by") else
              f"waiting on {cut['waiting_on']} (not graded yet)." if cut.get("waiting_on") else "coin flip, the commissioner decides.")
           if cut else "")
    when = str(pv.get("computed_at") or "")[:16].replace("T", " ")
    return ("IF IT ENDED TODAY (the site's own playoff math, playoff_compute"
            + (f", as of {when} UTC" if when else "") + "; the regular season isn't over, so this is a projection, "
            f"not the field):\n{quals}{tie}")


def race_note(ctx: dict, question: str) -> str:
    q = question or ""
    if not _RACE_ASK.search(q) or re.search(r"\b(cfp|committee|college football playoff|ranked|rankings?)\b", q, re.I):
        return ""
    st = ctx.get("standings_ytd") or []
    wk = ctx.get("current_week")
    if not st or wk is None or int(wk) > REGULAR_SEASON_LAST_WEEK:
        return ""
    left_now = sum(1 for g in ctx.get("games_this_week") or [] if not g.get("winner") and not g.get("tiebreaker"))
    avail = left_now + 4 * (REGULAR_SEASON_LAST_WEEK - int(wk))
    if len(st) <= PLAYOFF_SPOTS:
        return ""
    cut = sorted((r["w"] for r in st), reverse=True)[PLAYOFF_SPOTS - 1]
    me = (ctx.get("you") or {}).get("team_name")
    at_or_above = sum(1 for x in st if x["w"] >= cut)
    rows = []
    for r in st:
        gap = cut - r["w"]
        tag = ("tied on the cut line: tiebreakers decide it if it ends that way" if gap == 0 and at_or_above > PLAYOFF_SPOTS
               else "in a playoff spot" if gap <= 0 else
               "ON THE BUBBLE, " + f"{gap} behind the cut line" if gap <= 2 else
               "ELIMINATED (can't reach the cut line even winning out)" if gap > avail else
               f"{gap} behind the cut line")
        rows.append(f"{r['team']}{' (the user)' if r['team'] == me else ''} {r['w']}-{r['l']}: {tag}")
    # condense: the top 4, everyone within 3 of the cut line, the caller, a count for the rest
    keep = [x for x, r in zip(rows, st) if r["rank"] <= PLAYOFF_SPOTS or cut - r["w"] <= 3 or r["team"] == me]
    rest = len(rows) - len(keep)
    if rest:
        elim = [r["team"] for r in st if cut - r["w"] > avail and r["team"] != me]
        keep.append(f"{rest} more team{'s' * (rest != 1)} sit more than 3 points behind the cut line"
                    + (f" (ELIMINATED, can't reach it even winning out: {', '.join(elim)})" if elim else ""))
    rows = keep
    return (f"PLAYOFF RACE (computed; top {PLAYOFF_SPOTS} after week {REGULAR_SEASON_LAST_WEEK} make the league "
            f"playoffs): the cut line is {cut} points (4th place right now). Points still available this regular "
            f"season: {avail} ({left_now} left this week + 4 a week through week {REGULAR_SEASON_LAST_WEEK}).\n"
            + "\n".join(f"- {x}" for x in rows))


# Submission status (site audit #8; Charlie OK'd naming who hasn't picked,
# 2026-11-15). The leaderboard's public Weekly Submissions panel reads the RPC
# submission_status (granted to anon); Picky reads the same RPC for the current
# week, before and after lock. Only made / not yet: the picks themselves stay hidden until
# kickoff (filter_visible_picks never lets them in, pick_claim_check drops a
# guessed one).
SUBMIT_ASK = re.compile(
    r"\b(made|make|submit\w*|turn(ed)? in|lock(ed)? in|put in|enter(ed)?|done with|finish(ed)?)\b.{0,30}\bpicks?\b|"
    r"\bpicks? (are |is )?(in|done|submitted|made)\b|"
    r"\b(has|have|did)(n'?t| not)?\b.{0,25}\b(picked|submit\w*)\b|"
    r"\b(who|anyone|anybody|everyone|everybody|nobody)\b.{0,25}\b(picked|pick(ed)? yet|not picked|submit\w*)\b|"
    r"\bpicked yet\b|\bstragglers?\b", re.I)
_SUBMIT_NOT = re.compile(r"\b(best|worst|good|bad|great|dumb|smart|stupid|last week|week \d+|last season)\b", re.I)
MEMBERS_FILE = Path(os.environ.get("PICKY_GROUP_MEMBERS",
                                   "/Users/Agent/cfb-pickem-private/picky_group_members.json"))
_SUB_NEG = re.compile(r"\b(hasn'?t|has not|haven'?t|have not|didn'?t|did not|not yet|yet to|no picks|"
                      r"still waiting|waiting on|missing)\b", re.I)
_SUB_POS = re.compile(r"\b(has|have|had|already|did)\b.{0,20}\b(made|submitted|picked|locked|turned|entered|in)\b|"
                      r"(\w'(s|ve))\b.{0,20}\b(made|submitted|picked|locked|turned|entered|in)\b|"
                      r"\bpicks? (are|is) in\b", re.I)


def member_nicknames() -> dict[str, str]:
    """team -> what the group calls them (private handle map; {} without it)."""
    try:
        data = json.loads(MEMBERS_FILE.read_text())
    except (OSError, ValueError):
        return {}
    return {m["team"]: m["name"] for m in data.get("members") or [] if m.get("team") and m.get("name")}


def week_started(ctx: dict) -> bool:
    """Any pool game this week kicked off or decided (regular season: the week is locked)."""
    now = _now_ct_naive()
    return any(g.get("winner") or _game_is_locked({"Status": g.get("status"), "Start (CT)": g.get("start")}, now)
               for g in ctx.get("games_this_week") or [])


def wants_submission(ctx: dict, question: str) -> bool:
    q = question or ""
    # on after lock too (coordinator, 2026-11-15): the site's panel shows the current week's status all
    # week, and a locked week's picks are revealed anyway (filter_visible_picks follows the site)
    if ctx.get("error") or not SUBMIT_ASK.search(q) or _SUBMIT_NOT.search(q):
        return False
    pool = [{"Away": g["away"], "Home": g["home"], "id": g["id"]} for g in ctx.get("games_this_week") or []]
    return not games_named(q, pool)  # 'has Ackbar picked Boise?' asks for the pick: stays hidden


async def load_submission(client: httpx.AsyncClient, jwt: str, ctx: dict) -> dict | None:
    """{team: has picks in} for the current week, active members only (the site's panel)."""
    try:
        r = await client.post(f"{REST}/rpc/submission_status", headers=_auth_headers(jwt),
                              json={"p_season": ctx["season"], "p_week": ctx["current_week"]}, timeout=8.0)
        rows = r.json() if r.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        return None
    teams = set(ctx.get("teams") or [])
    return {x["team_name"]: bool(x.get("submitted")) for x in rows or [] if x.get("team_name") in teams} or None


def _member_rx(team: str, nick: dict) -> list[str]:
    names = {team} | ({nick[team]} if nick.get(team) else set())
    return [rf"(?<!\w){re.escape(n)}(?!\w)" if len(n) > 3 else rf"(?<!\w)(?-i:{re.escape(n)})(?!\w)" for n in names]


def _says(text: str, team: str, nick: dict) -> bool:
    return any(re.search(rx, text or "", re.I) for rx in _member_rx(team, nick))


def submission_subjects(ctx: dict, question: str) -> list[str]:
    """Members the question names (team, group nickname, Chuck's aliases), plus
    the caller on 'did I submit'; [] for 'who hasn't picked'."""
    q, status, nick = question or "", ctx.get("submission") or {}, member_nicknames()
    out = [t for t in status if _says(q, t, nick)]
    ld = (ctx.get("leader") or {}).get("team_name")
    if ld in status and ld not in out and _LEADER_ALIAS.search(q) and not leader_is_caller(ctx):
        out.append(ld)
    me = (ctx.get("you") or {}).get("team_name")
    if me in status and me not in out and re.search(r"\b(did|have|has|had) i\b|\bam i\b|\bmy picks?\b|"
                                                     r"\bi (submitted|picked|made|turned|put|locked)\b|\bme\b", q, re.I):
        out.append(me)
    return out


def submission_answer(ctx: dict, question: str) -> str:
    status, wk = ctx.get("submission") or {}, ctx.get("current_week")
    me = (ctx.get("you") or {}).get("team_name")
    subs = submission_subjects(ctx, question)
    if subs:
        out = []
        for t in subs:
            if t == me:
                out.append(f"You've made your week {wk} picks." if status[t] else
                           f"You haven't made your week {wk} picks yet.")
            else:
                out.append(f"{t} has made picks for week {wk}." if status[t] else
                           f"{t} hasn't made picks for week {wk} yet.")
        return " ".join(out)
    made = [t for t, v in status.items() if v]
    yet = [t for t, v in status.items() if not v]
    if not yet:
        return f"Everybody has picks in for week {wk}."
    if not made:
        return f"Nobody has made picks for week {wk} yet."
    return (f"Still waiting on week {wk} picks from {', '.join('you' if t == me else t for t in yet)} "
            f"({len(made)} of {len(status)} in).")


def submission_note(ctx: dict, question: str) -> str:
    status = ctx.get("submission")
    if not status or not wants_submission(ctx, question):
        return ""
    made = [t for t, v in status.items() if v]
    yet = [t for t, v in status.items() if not v]
    return (f"SUBMISSION STATUS (week {ctx.get('current_week')}, same as the site's public Weekly Submissions panel; "
            f"it shows only whether picks are in, never what they are):\n"
            f"- Made picks: {', '.join(made) or 'nobody yet'}\n- Not yet: {', '.join(yet) or 'nobody'}\n"
            f"Answer: {submission_answer(ctx, question)} Use the names exactly as written. "
            + ("The week is locked, so a missing entry now means no picks this week. Name picks only as they "
               "appear in LEAGUE DATA; never guess one." if week_started(ctx) else
               "What anyone picked stays hidden until the week locks at the first kickoff: never say or guess "
               "a pick."))


def submission_fact(ctx: dict, question: str) -> tuple | None:
    """(check, fallback) so the answer always states the real status."""
    status = ctx.get("submission")
    if not status or not wants_submission(ctx, question):
        return None
    nick, me = member_nicknames(), (ctx.get("you") or {}).get("team_name")
    subs = submission_subjects(ctx, question)
    line = " For the record: " + submission_answer(ctx, question)
    if len(subs) == 1:
        done = status[subs[0]]
        return ((lambda t: bool(_SUB_POS.search(t)) and not _SUB_NEG.search(t)) if done
                else (lambda t: bool(_SUB_NEG.search(t))), line)
    if subs:
        return (lambda t: all((t2 == me and re.search(r"\byou\b", t, re.I)) or _says(t, t2, nick) for t2 in subs),
                line)
    yet = [t for t, v in status.items() if not v]
    if not yet:
        return (lambda t: bool(re.search(r"\b(every(body|one)|all)\b", t, re.I)), line)
    if len(yet) == len(status):
        return (lambda t: bool(re.search(r"\b(nobody|no one|none)\b", t, re.I)), line)
    return (lambda t: all((t2 == me and re.search(r"\byou\b", t, re.I)) or _says(t, t2, nick) for t2 in yet), line)


_PICK_VERB = r"(?:took|picked|is on|'s on|went with|backed|is riding|is taking|rolled with|locked in|is all over)"


def pick_claim_check(ctx: dict, question: str):
    """Sentence filter while the week is open: drop a guessed pick ('Ackbar
    took Boise State': nobody's unlocked pick is in the context, so it's made
    up) and, on a status question about one member, a status that contradicts
    the RPC."""
    if ctx.get("error") or week_started(ctx):
        return None
    status, nick = ctx.get("submission") or {}, member_nicknames()
    me = (ctx.get("you") or {}).get("team_name")
    others = [t for t in (ctx.get("teams") or []) if t != me]
    pool = [x for g in ctx.get("games_this_week") or [] for x in (g["away"], g["home"])]
    if not pool:
        return None
    teams_rx = "(?:" + "|".join(_team_re(n, False) for n in pool) + ")"
    who_rx = "(?:" + "|".join([rx for t in others for rx in _member_rx(t, nick)] + [r"\b(?:he|she|they)\b"]) + ")"
    claim = re.compile(who_rx + r"(?:\W+\w+){0,2}?\s+" + _PICK_VERB + r"\s+(?:the\s+)?" + teams_rx, re.I)
    asked = bool(status) and wants_submission(ctx, question)
    subs = submission_subjects(ctx, question) if asked else []
    one = subs[0] if len(subs) == 1 else None
    any_team = re.compile(teams_rx, re.I)

    def ok(sent: str) -> bool:
        if claim.search(sent):
            return False
        if asked and any_team.search(sent):  # 'justify taking the chalk in Wake Forest': a guessed pick
            return False
        if one:
            about = (re.search(r"\byou\b", sent, re.I) if one == me else
                     _says(sent, one, nick) or re.search(r"\b(he|she|they)\b", sent, re.I))
            if about and status[one] and _SUB_NEG.search(sent):
                return False
            if about and not status[one] and _SUB_POS.search(sent) and not _SUB_NEG.search(sent):
                return False
        return True
    return ok


# Site how-to glossary (site audit #10, read from the site's own pages and code).
SITE_HOWTO = (
    "SITE HOW-TO (how the site works; answer from this):\n"
    "- Making or changing picks: on the Picks page, tap a side. Picks save automatically and can be changed any "
    "time until the week locks at the first kickoff. A pick only counts once it's saved.\n"
    "- 'Let the slots decide': instead of a side, the site flips a coin for you at lock (and might pop the "
    "jackpot football for an automatic win).\n"
    "- Colors: a green cell means the pick covered, red or purple means it missed, and a gold outline marks a "
    "lonely pick (few or no one else took that side).\n"
    "- Picks stay hidden until the week locks; the reveal countdown shows when.\n"
    "- Signing in: Sign In page with a magic link by email, or a password; 'Forgot password' sends a reset link. "
    "New members create or claim their team on the Claim Team page.\n"
    "- The 'BOT · EXHIBITION' row on the scoreboard is Picky: he plays for fun, never ranked, never paid.\n"
    "- The Pit (blackjack) lives on the Tiebreakers board; chips only break ties.\n"
    "- Scout panel: click a member's name (members only, never yourself) to see a lean on their picks, shown "
    "only when the model is over 60% sure, otherwise 'No read'.")
_HOWTO_ASK = re.compile(r"\b(how (do|can|would) (i|you|we)|where (do|can) i|what('?s| is| does) the (gold|green|red|purple|"
                        r"countdown|scout|bot)|what does (green|red|gold|purple)|gold (box|outline|border)|"
                        r"change my picks?|sign ?in|log ?in|password|magic link|claim (a |my )?team|scout|"
                        r"bot ?. ?exhibition|exhibition row|countdown)\b", re.I)


def howto_note(question: str) -> str:
    return SITE_HOWTO if _HOWTO_ASK.search(question or "") else ""


# Tie cascade "if it ended today" (site audit #5): regular-season order from the
# rules. Conference-title and Army-Navy tiebreaker picks and the Celebration Bowl
# aren't played until December, so today it falls to chips (public), then last 8.
_TIE_ASK = re.compile(r"\b(tie ?-?break\w*|tied|ties?|who wins the tie|break the tie)\b", re.I)


def _chips_of(lore: dict, team: str) -> int | None:
    for ln in ((lore.get("casino") or {}).get("members") or {}).get(team) or []:
        m = re.search(r"The Pit \d{4}: ([\d,]+) chips", ln)
        if m:
            return int(m.group(1).replace(",", ""))
    return None


def _last8_of(lore: dict, team: str) -> tuple | None:
    for ln in (lore.get("members") or {}).get(team) or []:
        m = re.search(r"last 8 picks (\d+)-(\d+)", ln)
        if m:
            return int(m.group(1)), int(m.group(2))
    return None


def tie_note(ctx: dict, question: str) -> str:
    q = question or ""
    if not _TIE_ASK.search(q) or re.search(r"\bplayoffs?\b", q, re.I):
        return ""
    st = ctx.get("standings_ytd") or []
    names = [r["team"] for r in st if re.search(rf"(?<!\w){re.escape(r['team'])}(?!\w)", q, re.I)]
    me = (ctx.get("you") or {}).get("team_name")
    if me and me not in names and re.search(r"\b(me|my|i|i'?m)\b", q, re.I):
        names.append(me)
    if len(names) < 2:  # 'who's tied right now' -> every tie group of 2+
        groups = collections.defaultdict(list)
        for r in st:
            groups[r["w"]].append(r["team"])
        ties = [g for g in groups.values() if len(g) > 1]
        if not ties or names or not re.search(r"\b(right now|currently|today|who('?s| is| are) tied|any ties)\b", q, re.I):
            return ""
        return ("TIES RIGHT NOW (season points): " + "; ".join(
            f"{', '.join(g)} at {next(r['w'] for r in st if r['team'] == g[0])}" for g in ties) + ".")
    lore = load_lore() or {}
    pts = {r["team"]: r["w"] for r in st}
    a, b = names[0], names[1]
    lines = [f"TIE CASCADE, if the regular season ended today ({a} {pts.get(a)} pts, {b} {pts.get(b)} pts"
             + ("" if pts.get(a) == pts.get(b) else "; NOTE they are NOT tied on points right now") + "):",
             "1) conference championship game wins and 2) Army v Navy: those tiebreaker picks aren't played until "
             "December, so they can't separate anyone yet."]
    ca, cb = _chips_of(lore, a), _chips_of(lore, b)
    if ca is not None and cb is not None:
        lines.append(f"3) chips from The Pit: {a} {ca:,}, {b} {cb:,}"
                     + (f" -> {a if ca > cb else b} wins it here." if ca != cb else " -> still tied."))
    if ca is None or cb is None or ca == cb:
        lines.append("4) Celebration Bowl: not played yet.")
        la, lb = _last8_of(lore, a), _last8_of(lore, b)
        if la and lb:
            pa, pb = la[0] / max(1, sum(la)), lb[0] / max(1, sum(lb))
            lines.append(f"5) win % over the last 8 picks: {a} {la[0]}-{la[1]}, {b} {lb[0]}-{lb[1]}"
                         + (f" -> {a if pa > pb else b} wins it here." if pa != pb else " -> still tied: 6) coin flip."))
    return "\n".join(lines)


# (B) Proactive info (Charlie, 2026-11-14): Picky volunteers ONE relevant fact
# about the caller's own week ("your UTSA pick is covering by 7.5 with 6:26
# left"), computed here, offered on every other question, never when the
# question is already about their picks/results or is off-topic or rules.
def proactive_fact(ctx: dict) -> str:
    me = (ctx.get("you") or {}).get("team_name")
    if not me:
        return ""
    live = {g["GameId"]: g for g in ctx.get("live_games") or []}
    rows = member_pick_results(ctx, me)
    for r in rows:
        if r["result"] != "IN PROGRESS" or r["tiebreaker"]:
            continue
        g = live.get(r["game"]["id"]) or {}
        if g.get("AwayPts") is None or g.get("line") is None and r["game"].get("line") is None:
            continue
        g = {**g, "line": r["game"].get("line")}
        sn = (g.get("status_name") or "").upper()
        p = int(g.get("Period") or 0)
        when = ("at halftime" if "HALFTIME" in sn else f"with {g.get('Clock')} left in the {_period(p)}"
                if p <= 4 else f"in {_period(p)}")
        cov = covering_side(g)
        state = (f"covering by {_num(abs(cover_margin(g)))}" if same_team(cov, r["side"])
                 else f"NOT covering (down {_num(abs(cover_margin(g)))} against the line)")
        return (f"the user's {r['label']} pick is live: {g['Away']} {g['AwayPts']}, {g['Home']} {g['HomePts']} "
                f"{when}, and it's {state}.")
    st = ctx.get("standings_ytd") or []
    row = next((x for x in st if x["team"] == me), None)
    if not row:
        return ""
    mates = [x["team"] for x in st if x["rank"] == row["rank"] and x["team"] != me]
    gap = st[0]["w"] - row["w"]
    if row["rank"] == 1:
        nxt = next((x for x in st if x["rank"] > 1), None)
        lead = row["w"] - nxt["w"] if nxt else 0
        return (f"the user is in first place" + (f", tied with {', '.join(mates)}" if mates else
                f", {lead} point{'s' * (lead != 1)} ahead of second") + ".")
    if row["rank"] == st[-1]["rank"]:
        up = next((x for x in reversed(st) if x["rank"] < row["rank"]), None)
        need = up["w"] - row["w"] if up else 0
        return (f"the user is in last place, {need} point{'s' * (need != 1)} behind {up['team']} for "
                f"the next spot up." if up else "")
    return (f"the user is {_ordinal(row['rank'])}" + (f", tied with {', '.join(mates)}" if mates else "")
            + f", {gap} point{'s' * (gap != 1)} behind first place.")


def proactive_note(ctx: dict, messages: list, question: str) -> str:
    if os.environ.get("PICKY_HOOK") != "1":  # off 2026-11-16: Charlie flagged the tacked-on "You're 4-0 this week" line
        return ""
    q = question or ""
    n_user = sum(1 for m in messages if isinstance(m, dict) and m.get("role") == "user")
    if (n_user % 2 == 0 or ctx.get("error") or wants_rules(q) or _PICKS_ASK.search(q) or RESULTS_ASK.search(q)
            or _MOCK.search(q)):
        return ""
    if guard_note(ctx, q) or opinion_note(q, ctx) or live_note(ctx, q) or leader_note(ctx, q):
        return ""
    if _NEWS_ASK.search(q) or _RANK_ASK.search(q) or _LORE_ASK.search(q) or _CASINO_ASK.search(q) \
            or _HOWTO_ASK.search(q) or _TIE_ASK.search(q) or _RACE_ASK.search(q):
        return ""  # those questions have their own facts; a hook would hijack the answer
    fact = proactive_fact(ctx)
    if not fact:
        return ""
    return ("OPTIONAL HOOK: after answering, you may add ONE short sentence, in your own words and voice, "
            f"with this fact about the user (say 'you'): {fact} Copy its numbers exactly. Skip it if it "
            "would feel forced.")


# "What did you think of this weekend's games?" is about the GAMES, not the
# caller's record (live 2026-11-15: Picky answered "You went 2-2" and invented
# "tied for third"). The take comes from a code-built recap: finals, covers and
# margins, upsets, who cashed, Picky's own picks.
GAMES_ASK = re.compile(
    r"\b(this|last|the|today'?s|yesterday'?s|saturday'?s|weekend'?s|week'?s)\s+(weekend'?s?\s+|week'?s?\s+)?"
    r"(games?|slate|action|results)\b|\bhow (did|'?d|were) the games\b|\bupsets?\b|"
    r"\bgames? (this|last) (weekend|week|saturday)\b|\b(thoughts|take) on (the )?(games?|slate|weekend)\b", re.I)


def wants_games_recap(ctx: dict, question: str) -> bool:
    q = question or ""
    if ctx.get("error") or not GAMES_ASK.search(q) or _SELF_WORDS.search(q):
        return False
    return _week_state(ctx)[1] > 0


def games_recap_note(ctx: dict, question: str) -> str:
    if not wants_games_recap(ctx, question):
        return ""
    live = {x["GameId"]: x for x in ctx.get("live_games") or []}
    picks = ctx.get("revealed_picks_this_week") or {}
    n_teams = len(picks)
    out = [f"GAMES RECAP, {this_week_label(ctx)} (computed in code; copy every score, margin and count exactly, "
           "never redo the math):"]
    for g in ctx.get("games_this_week") or []:
        if g.get("tiebreaker") or not g.get("winner"):
            continue
        lg = live.get(g["id"]) or {}
        ap, hp, ln = _pts(lg.get("AwayPts")), _pts(lg.get("HomePts")), _pts(g.get("line"))
        cov = g["home"] if same_team(g["winner"], g["home"]) else g["away"]
        took = sum(1 for plist in picks.values() for p in plist if p["game_id"] == g["id"] and same_team(p["pick"], cov))
        if ap is None or hp is None or ln is None:
            out.append(f"- {g['away']} at {g['home']}: {cov} covered. {took} of {n_teams} members had {cov}.")
            continue
        fav = g["home"] if ln < 0 else g["away"] if ln > 0 else None
        dog = (g["away"] if fav == g["home"] else g["home"]) if fav else None
        won = g["away"] if ap > hp else g["home"] if hp > ap else None
        sc = f"{g['away']} {_num(ap)}, {g['home']} {_num(hp)}"
        line = f"{fav} -{_num(abs(ln))}" if fav else "pick'em"
        # the cover first (that's the result in this league), the final score only as context
        s = (f"- {cov} covered by {_num(abs(hp + ln - ap))}" if hp + ln != ap else
             f"- landed exactly on the number, a push, and the push goes to the away side, so {cov} covered")
        s += f". Final {sc}, pool line {line}; "
        s += f"{won} won the game by {_num(abs(ap - hp))}." if won else "tied."
        if won and dog and same_team(won, dog):
            s += f" UPSET: {dog} was the underdog and won outright."
        elif won and fav and same_team(won, fav) and not same_team(cov, fav):
            s += f" {fav} won the game but didn't cover."
        s += f" {took} of {n_teams} members cashed with {cov}."
        out.append(s)
    wk = ctx.get("current_week")
    recs = {t: tuple(v) for t, r in (ctx.get("weekly_records") or {}).items() for k, v in (r or {}).items()
            if str(k) == str(wk) and t in picks}
    if recs:
        best = max(w for w, _ in recs.values())
        worst = min(w for w, _ in recs.values())
        b = [t for t, (w, _) in recs.items() if w == best]
        z = [t for t, (w, _) in recs.items() if w == worst]
        out.append(f"- Best week: {', '.join(b)} at {best}-{recs[b[0]][1]}. Worst: {', '.join(z)} at "
                   f"{worst}-{recs[z[0]][1]}.")
    prow = [r for r in picky_pick_results(ctx) if r["result"] in ("WIN", "LOSS")]
    if prow:
        w, l = results_record(prow)
        out.append(f"- Your own picks (you are Picky): " + "; ".join(f"{r['label']} {r['result']}" for r in prow)
                   + f". You went {w}-{l}.")
    out.append("They asked about the GAMES: OPEN with your take on how the slate went (the results, the upset, "
               "who cashed, how your own picks did). Don't lead with the user's own record; at most one short jab "
               "about them at the very end.")
    return "\n".join(out)


# "why don't you work better" (live 2026-11-15: misread as "why I work better",
# then an invented margin). Picky owns it with self-aware humor, then a jab.
_SELF_KNOCK = re.compile(
    r"\bwhy (don'?t|dont|do not|can'?t|cant|won'?t|doesn'?t) (you|u|picky|this bot|the bot) (work|function|get better|"
    r"do better|answer)|\b(you|u|picky|this bot|the bot)( still)? (don'?t|dont|doesn'?t|do not|never) work\b|"
    r"\bwork better\b|\b(are|r) (you|u) (broken|even working|working)\b|\b(all )?bark\b|\bbad (bot|ai)\b|"
    r"\b(you'?re|you are|ur|picky is|picky'?s) (so |too )?(slow|broken|glitch\w*|buggy|cooked|dumb as)\b", re.I)


def self_knock_note(ctx: dict, question: str) -> str:
    q = question or ""
    if ctx.get("error") or not _SELF_KNOCK.search(q):
        return ""
    me = (ctx.get("you") or {}).get("team_name")
    facts = member_facts(ctx, me) if me else ""
    return ("SELF-AWARE: this knocks how well YOU (Picky) work. Read it exactly: they said \"" + q.strip()[:160]
            + "\". Own it with self-aware humor about what you actually are: a small local model (about "
            + ((re.search(r"(\d+)b", MODEL.lower()) or [None, "9"])[1]) + " billion "
            "parameters) running on a 16 GB Mac mini at the commissioner's place, no cloud, no budget. Make up "
            "your own joke from that, a new one each time, never a stock line. Then turn it around with one "
            "jab at them" + (f", using ONLY these facts about {me}:\n{facts}" if facts else ".")
            + "\nNever claim you work better than them or misquote what they said.")


def _user_turn(ctx: dict, question: str) -> str:
    """A user message plus the facts about the people it names. Sent for
    the latest turn (and the one before a follow-up); see history_turns."""
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
        money = ("\n" + payout_text(len(ctx.get("teams") or [])) if _MONEY_ASK.search(question or "")
                 and ctx.get("teams") else "")
        ans = league_rules.answer(question, n_teams=len(ctx.get("teams") or []) or None)
        if ans:
            parts.append(("RULES ANSWER (computed from the site's code; say this in your own words, keep every "
                          "number):\n" if not ans.startswith(league_rules.FLAG) else "") + ans)
        parts.append("LEAGUE RULES (the written rules on this topic; anything not in the rules is not a rule):\n"
                     + rules_for(question) + lock + money
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
    card = game_cards.card_note(ctx, question) if not guard.startswith("GUARDRAIL") else ""
    # the card (or the news rider) already tells the story: 3 intel facts, unless injuries are the ask
    short = (card or _NEWS_ASK.search(question or "")) and not _INJURY_ASK.search(question or "")
    intel = dossier_note(ctx, question, limit=3 if short else None) if not guard.startswith("GUARDRAIL") else ""
    if _INJURY_ASK.search(question or "") and ctx.get("live_injuries"):
        parts.append("INJURIES: the LIVE GAME FEED block below has in-game reports from ESPN's play-by-play "
                     "(an exception to rule 7): cite those as reported, with the quarter and clock, nothing else. "
                     "Never guess how bad it is, never say 'no injuries', never call anyone healthy.")
    elif _INJURY_ASK.search(question or ""):
        parts.append("INJURIES: there's no official college injury report, but the GAME INTEL block below has "
                     "public reports: cite those as reported, nothing else. Never say 'no injuries', and never "
                     "call anyone healthy." if intel else
                     "INJURIES: there's no official college injury report: say so and send them to the beat "
                     "writers. If a NEWS QUESTION block carries an injury item, you may cite it exactly as written. "
                     "Never say 'no injuries', and never call anyone healthy.")
    ln = leader_note(ctx, question)
    parts.append(ln)
    # board voice: opinion / hot-take answers and explicit roast asks only; never with Dear Leader or a guardrail
    if (("HOT TAKES" in opinion or _ROAST_VOICE.search(question or "")) and not ln
            and not guard.startswith("GUARDRAIL")):
        parts.append(voice_note(question))
    if len((question or "").split()) <= 7 and re.search(r"\b(hey|hi|hello|yo|sup|what'?s up|wassup|howdy|morning|"
                                                         r"evening)\b", question or "", re.I):
        parts.append("GREETING: answer in two short sentences, tops: a quick hello in character, plus at most "
                     "one fact if an OPTIONAL HOOK is given.")
    if not guard.startswith("GUARDRAIL"):
        parts.append(lore_note(ctx, question))
        parts.append(casino_note(ctx, question))
        parts.append(card or analysis_note(ctx, question))  # offline game card (code factors + Opus case), else live
        parts.append(intel)
        parts.append("" if card else take_note(ctx, question))
        parts.append(material_note(ctx, question))
        parts.append(race_note(ctx, question))
        parts.append(playoff_note(ctx, question))
        parts.append(howto_note(question))
        parts.append(tie_note(ctx, question))
        parts.append(blinks_note(ctx, question))
        parts.append(ackbar_note(ctx, question))
    parts.append(submission_note(ctx, question))
    parts.append(games_recap_note(ctx, question))
    parts.append(self_knock_note(ctx, question))
    parts.append(opinion)
    parts = [p for p in parts if p]
    return "\n\n".join(parts + [question]) if parts else question


# Older user turns go to the model bare (speed lane, 2026-11-16). gemma4 is sliding-window: llama.cpp
# keeps its cache checkpoint inside the shared prefix, so every token after it is re-read on every text
# (~120 tok/s here) and a rider re-rendered into each old turn never got reused. Five exchanges in, the
# old riders were ~3k tokens (~25 s) of stale cards and blocks. The latest turn keeps its riders; a
# follow-up ('why?', 'what about him', 'and Iowa?') keeps the previous turn's too.
FOLLOWUP_Q = re.compile(r"\b(that|it|him|he|his|they|them|then|why|how come|instead|the other)\b|"
                        r"^\s*(and|so|but|what about|how about|ok|okay|really)\b", re.I)


def history_turns(ctx: dict, convo: list[dict]) -> tuple[list[dict], list[str]]:
    """(messages for the model, rider text of the turns sent bare). The bare
    turns' riders still feed the grounding checks: the last answer may have
    used one of their facts."""
    users = [i for i, m in enumerate(convo) if i and m["role"] == "user"]
    keep = set(users[-1:])
    if len(users) > 1:
        q = convo[users[-1]]["content"]
        if FOLLOWUP_Q.search(q) or len(q.split()) <= 3:  # 'and Iowa?', 'Hoss?
            keep.add(users[-2])
    out, unsent = [convo[0]], []
    for i, m in enumerate(convo[1:], 1):
        if m["role"] == "user":
            full = _user_turn(ctx, m["content"])
            out.append({"role": "user", "content": full if i in keep else m["content"]})
            if i not in keep and full != m["content"]:
                unsent.append(full)
        else:
            out.append({"role": "assistant", "content": scrub_tics(strip_land_ack(fix_last_week(m["content"], ctx)))})
    return out, unsent


# -----------------------------------------------------------------------------
# ollama streaming
# -----------------------------------------------------------------------------


def looping(text: str) -> bool:
    """A degenerate repeat ('Raphael is the man. Raphael is the king. ...'):
    six sentences opening with the same three words, the same sentence
    three times, or past MAX_ANSWER_WORDS."""
    if len(text.split()) > MAX_ANSWER_WORDS:
        return True
    # a word or short phrase stuck on repeat ("glorified glorified glorified ...")
    if re.search(r"\b(\w+(?:\W+\w+){0,2})(?:\W+\1\b){3,}", text, re.I):
        return True
    every = [x.strip().lower() for x in re.split(r"(?<=[.!?])\s+", text) if x.strip()]
    if every and max(collections.Counter(every).values()) >= 3:  # "You're 4-0. You're 4-0. You're 4-0."
        return True
    sents = [x for x in every if len(x.split()) >= 3]
    if not sents:
        return False
    heads = collections.Counter(" ".join(x.split()[:3]) for x in sents)
    heads2 = collections.Counter(" ".join(x.split()[:2]) for x in sents)
    whole = collections.Counter(sents)
    # m-run abA: "He's got 28 points, but he's got 20 misses. He's got 20 misses, but..." looped at temp 0.2
    return max(heads.values()) >= 5 or max(heads2.values()) >= 5 or max(whole.values()) >= 2


# The prefix checkpoint (see prompt_prefix). Every request first sends the
# shared prefix alone as a raw prompt, which leaves a checkpoint right at its
# end; the real request then processes only the tail. When the checkpoint is
# already there (the usual case) the prime costs ~0.1 s; after a data change
# (kickoff, final, ESPN refresh) or a model reload it rebuilds it once.
async def _prime(c: httpx.AsyncClient, prefix: str) -> None:
    # the raw prime uses qwen's chat template. gemma4 is sliding-window: llama.cpp
    # only keeps checkpoints near the END of each prompt, so two members' prompts
    # (same prefix, different tail) re-prefill all ~8k tokens (~55 s) unless a
    # checkpoint sits inside the prefix. A system-only chat call, rendered by
    # Ollama's own template, leaves one ~512 tokens before the prefix end.
    # Other models reuse prompt prefixes natively.
    payload = {"model": MODEL, "stream": False, "options": {**OLLAMA_OPTIONS, "num_predict": 1},
               "keep_alive": OLLAMA_KEEP_ALIVE}
    if "gemma" in MODEL.lower():
        endpoint = "chat"
        payload["messages"] = [{"role": "system", "content": prefix}]
        if OLLAMA_THINK is not None:
            payload["think"] = OLLAMA_THINK == "true"
    elif "qwen" in MODEL.lower():
        endpoint = "generate"
        payload.update(prompt="<|im_start|>system\n" + prefix, raw=True)
    else:
        return
    try:
        read = max(PRIME_TIMEOUT, getattr(getattr(c, "timeout", None), "read", None) or 0.0)  # never shorter
        r = await c.post(f"{OLLAMA}/api/{endpoint}", json=payload,
                         timeout=httpx.Timeout(connect=5.0, read=read, write=10.0, pool=5.0))
        if r.status_code == 200:
            d = r.json()
            log.info("prime: %.1fs (prefix %s)", (d.get("prompt_eval_duration") or 0) / 1e9,
                     format(hash(prefix) & 0xffffff, "06x"))
    except (httpx.HTTPError, ValueError) as e:  # ValueError: a non-JSON body
        log.warning("prime failed: %s", e)


_BG_PRIMES: set = set()


def start_prime(prefix: str) -> asyncio.Task:
    """_prime in a background task with its own client: a caller that can't wait for a cold prefill
    (picky_brain's fact/banter calls) moves on, and the prefill still finishes for the next text."""
    async def run():
        async with httpx.AsyncClient() as c:
            await _prime(c, prefix)
    t = asyncio.create_task(run())
    _BG_PRIMES.add(t)
    t.add_done_callback(_BG_PRIMES.discard)
    return t


async def _ollama_stream(messages: list[dict], prefix: str | None = None):
    """Yield SSE-framed assistant tokens from Ollama."""
    payload = {"model": MODEL, "messages": messages, "stream": True,
               "options": {**OLLAMA_OPTIONS, **(OPTIONS_OVERRIDE.get() or {})},
               "keep_alive": OLLAMA_KEEP_ALIVE}
    if OLLAMA_THINK is not None:
        payload["think"] = OLLAMA_THINK == "true"
    # read covers the wait in Ollama's queue before the first token; the
    # client is kept alive by _keepalive pings meanwhile
    timeout = httpx.Timeout(connect=5.0, read=420.0, write=10.0, pool=5.0)
    text = ""
    await live_flag.wait_clear()  # side sidecars/exams yield to a live text in another process
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
                        log.info("ollama prompt eval %.1fs (%s tokens), %s tokens out",
                                 (obj.get("prompt_eval_duration") or 0) / 1e9, obj.get("prompt_eval_count"),
                                 obj.get("eval_count"))
                        yield "data: [DONE]\n\n"
                        return
    except httpx.HTTPError as e:
        log.warning("ollama unreachable: %s", e)
        yield _sse({"error": "ollama_unreachable"})


async def _model_stream(messages: list[dict], prefix: str | None = None):
    """The one model path: local Ollama. `messages[0]` is the system prompt."""
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


# Sentence filter (marathon 2026-11-15). The 9B copies few-shot jokes word for
# word ("last place with extra steps") and repeats itself across a chat, and
# now and then reaches for a mom joke or a dark line. Each finished sentence is
# checked before it goes out; one that copies an example joke, reuses a
# 6-word run from an earlier answer in this chat, or touches family/death is
# dropped (the rest of the answer still streams).
_FAMILY_DARK = re.compile(r"\b(mom|moms|mommy|mother|mama|momma|dad|daddy(?!freeman)|father|wife|girlfriend|"
                          r"boyfriend|sister|brother|grandma|grandpa|kids?|family|families|still breathing|son of a bitch|"
                          r"kill (yourself|himself|themselves)|kys|suicid\w*|cancer|dead body|traged\w*|tragic|funerals?)\b",
                          re.I)


def _ngrams(text: str, n: int = 6, skip_digits: bool = False) -> set:
    w = re.findall(r"[a-z0-9']+", (text or "").lower())
    out = set()
    for i in range(len(w) - n + 1):
        g = tuple(w[i:i + n])
        if skip_digits and any(any(c.isdigit() for c in x) for x in g):
            continue
        out.add(g)
    return out


# The jokes in the few-shot examples and Dear Leader examples (not their facts
# or the spread-explainer wording, which Picky is meant to reuse).
EXAMPLE_JOKES = [
    "one of these needs an intervention", "you're donating points", "last place with extra steps",
    "the only person alive on", "he needs a designated driver", "doesn't need picks advice",
    "i don't do anatomy", "i do box scores",
    "the only thing shrinking around here", "take it to the commissioner i just work here", "i just work here",
    "stays in the vault", "bold wrong but bold", "a mascot not a team doctor", "so not my problem",
    "four games to screw up", "extra steps", "designated driver", "faceless doom", "point handicap",

]


def _example_ngrams() -> set:
    out = set()
    for j in EXAMPLE_JOKES:
        w = tuple(re.findall(r"[a-z0-9']+", j))
        out |= _ngrams(j, n=5) if len(w) >= 5 else {w}
    return out


def self_intrusion_off_topic(ctx: dict, question: str, hostile: bool, facts, sa: str) -> bool:
    """True when the caller's own record/picks have no business in the answer:
    the question isn't about them, picks or results, and no rider asked for it."""
    q = question or ""
    if hostile or sa or ctx.get("daddyfreeman") or ctx.get("error"):
        return False
    if re.search(r"\b(me|my|mine|i|i'?m|am i|myself|we|us|our)\b", q, re.I) or _PICKS_ASK.search(q):
        return False
    if guard_note(ctx, q).startswith("GUARDRAIL") and guard_target(ctx, q) == (ctx.get("you") or {}).get("team_name"):
        return False
    return True


def _opener(text: str) -> str:
    return " ".join(re.findall(r"[A-Za-z']+", (text or "").split(".")[0])[:4])


def opener_note(messages: list) -> str:
    """Picky's last few openers in this conversation (site chat or a group
    text's rolling history), so he doesn't open the same way twice."""
    prior = [_opener(m.get("content") or "") for m in messages
             if isinstance(m, dict) and m.get("role") == "assistant" and (m.get("content") or "").strip()][-3:]
    prior = [o for o in prior if len(o.split()) >= 2]
    if not prior:
        return ""
    return ("VARY IT: your last answers here opened with " + "; ".join(f"'{o}...'" for o in prior)
            + ". Open differently this time, new words, and don't reuse a catchphrase.")


def banned_ngrams(messages: list) -> set:
    prior = " ".join(m.get("content") or "" for m in messages
                     if isinstance(m, dict) and m.get("role") == "assistant")
    return _EXAMPLE_NGRAMS | _ngrams(prior, skip_digits=True)


_BODY_WORDS = re.compile(r"\b(weenie|weiner|wiener|dick|cock|penis|schlong|pecker|junk|package|inch(es)?|"
                         r"endowed|hung|manhood|boobs?|tits?|size|anatomy|anatomical)\b", re.I)  # a 'tiny win total' pun is fine
# the caller's own record or picks, volunteered in an answer that isn't about them
_SELF_INTRUSION = re.compile(r"(?i:\b(you'?re|you are|you went|you sit|you'?ve gone)\b[^.!?]*\b\d{1,2}-\d{1,2}\b"
                             r"|\byou (just )?(lost|missed|blew|went \w+ on) (every|all|each)\b|\byou'?re (winless|perfect|undefeated)\b"
                             r"|\byou took\b|\byour (\w+ ){0,2}\d{1,2}-\d{1,2}\b|\b\d{1,2}-\d{1,2} (week|season|record) (of yours)\b)"
                             r"|\b(?i:your) [A-Z][\w'.&-]*( [A-Z][\w'.&-]*){0,2} (?i:pick|bet)\b")


_OFFTOPIC_LEAK = re.compile(r"\b\d{1,2}-\d{1,2}\b|\b(picks?|spread|cover\w*|standings|record|place)\b", re.I)


def pick_sides(ctx: dict) -> tuple[dict, list]:
    """({'you': caller's picks, 'picky': Picky's revealed picks}, this week's pool teams) for the filter."""
    me = (ctx.get("you") or {}).get("team_name")
    games = ctx.get("games_this_week") or []
    gid = {g["id"]: g for g in games}
    you = {p["pick"] for p in (ctx.get("revealed_picks_this_week") or {}).get(me, []) if p["game_id"] in gid}
    sides = {"you": you}
    pp = ctx.get("picky_player")
    if pp is not None:  # not loaded: don't police Picky's own claims
        sides["picky"] = {p["pick"] for p in pp.get("this_week") or [] if p.get("pick")}
    return sides, [x for g in games for x in (g["away"], g["home"])]


# Team nicknames (logos.Mascot, loaded once at startup). The 9B called Oregon
# State "the Ducks" and "the Rebels"; a sentence naming a team by nickname is
# dropped (school names only), except the homer's 'Irish'.
_MASCOT_RE: re.Pattern | None = None
MASCOT_ANY: re.Pattern | None = None


async def load_mascots() -> None:
    global _MASCOT_RE
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"{REST}/logos?select=Mascot", headers={"apikey": SUPABASE_ANON,
                                                                   "Authorization": f"Bearer {SUPABASE_ANON}"})
            names = {x["Mascot"].strip() for x in r.json() if x.get("Mascot")} - {"Fighting Irish", "Irish"}
        if names:
            alt = "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))
            _MASCOT_RE = re.compile(rf"\b[Tt]he ({alt})s?\b")
            global MASCOT_ANY
            MASCOT_ANY = re.compile(rf"\b({alt})s?\b")  # without 'the': analyst.py keeps nicknames out of takes
            log.info("mascot filter: %d nicknames", len(names))
    except (httpx.HTTPError, ValueError, KeyError) as e:
        log.warning("mascot list unavailable: %s", e)


# Never say or hint that a team or player is healthy (rule 7): no injury report exists.
_HEALTH_CLAIM = re.compile(r"\b(no injur(y|ies) (to report|reported|listed|news)|injury[- ]free|healthy|good to go|"
                           r"fully fit|clean bill|not (hurt|injured)|100 ?% (healthy|ready))\b", re.I)


def game_facts(ctx: dict) -> dict:
    """Decided pool games only (won/lost the game, covered/not), for checking any
    answer's game claims ('Boise State just lost by 5' when it won by 5)."""
    live = {x["GameId"]: x for x in ctx.get("live_games") or []}
    games = []
    for g in ctx.get("games_this_week") or []:
        if g.get("winner") and not g.get("tiebreaker"):
            cov = g["home"] if same_team(g["winner"], g["home"]) else g["away"]
            row = {"covered": cov, "not": g["away"] if cov == g["home"] else g["home"]}
            lg = live.get(g["id"]) or {}
            ap, hp = _pts(lg.get("AwayPts")), _pts(lg.get("HomePts"))
            if lg.get("Status") == "final" and ap is not None and hp is not None and ap != hp:
                row["won_game"], row["lost_game"] = (g["away"], g["home"]) if ap > hp else (g["home"], g["away"])
            games.append(row)
    return {"subjects": [], "games": games, "records": {}, "records_now": {}, "named_teams": [], "question": ""}


# --- weekly-record and margin claims (live 2026-11-15 group chat) -------------
# "you missed three of four games this week" (Blinky went 3-1), "tied for third
# with Blinkstradamus" (2-2 vs 3-1), "missed that game by five points" (the
# Boise pick missed the cover by 8.5). Every such claim must match the computed
# data, in any answer, not only results questions.
_NUMW = {"zero": 0, "none": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
         "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
         "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20,
         "thirty": 30, "forty": 40}
_NW = r"(\d{1,2}(?:\.5)?|" + "|".join(_NUMW) + r")(\s+and\s+a\s+half|\s+and\s+change)?"


def _numval(word: str, half: str | None = None) -> float | None:
    w = (word or "").lower()
    v = float(w) if re.match(r"\d", w) else _NUMW.get(w)
    if v is None:
        return None
    return v + 0.5 if half and "half" in half.lower() else v


_ORD = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7, "eighth": 8,
        "ninth": 9, "tenth": 10, "eleventh": 11, "twelfth": 12, "thirteenth": 13, "fourteenth": 14}
_WK_MISS = re.compile(r"\b(?:missed|miss(?:ing)?|blew|blown|whiffed(?: on)?|botched|bricked|dropped|lost)\s+(?:on\s+)?"
                      + _NW + r"\s+(?:of|out of)\s+(?:(?:his|her|your|my|their|the)\s+)?" + _NW, re.I)
_WK_MISS_G = re.compile(r"\b(?:missed|blew|whiffed(?: on)?|botched|bricked)\s+" + _NW
                        + r"\s+(?:games?|picks?)\b", re.I)
_WK_HIT = re.compile(r"\b(?:hit|got|nailed|cashed|won|went)\s+" + _NW + r"\s+(?:of|out of|for)\s+"
                     r"(?:(?:his|her|your|my|their|the)\s+)?" + _NW + r"(?:\s+(?:games?|picks?))?(\s+(?:right|wrong))?", re.I)
_WK_REC = re.compile(r"(?:\b(?:went|go|going|finished|sits? at|sitting at|is|are|am|at|a)|'s|'re|'m)\s+(\d)\s*-\s*(\d)\b"
                     r"(?!\s*(?:overall|on the (?:season|year)|this season|for the season|season))", re.I)
_WK_TIE = re.compile(r"\btie[ds]?\b(?:\s+for\s+\w+(?:\s+place)?)?\s+with\s+", re.I)
_WK_PLACE = re.compile(r"\b(?:tied for|tie for|in|sitting in|sits in|alone in)\s+(" + "|".join(_ORD)
                       + r"|\d{1,2}(?:st|nd|rd|th))(?:\s+place)?\b", re.I)
_COVER_BY = re.compile(r"\b(missed|miss(?:es)?|short|didn'?t cover|did not cover|failed to cover|covered|covers|"
                       r"beat the (?:spread|number|line))\b((?:\W+\w+){0,4}?)\W+by\s+" + _NW, re.I)
_GAME_BY = re.compile(r"\b(won|lost|win|wins|lose|loses|beat|fell|losing|winning|blown out|beaten|outscored)\b((?:\W+\w+){0,4}?)\W+by\s+"
                      + _NW, re.I)
_OTHER_SPAN = re.compile(r"\b(last week|week \d+|last season|last year|season|overall|on the year|all year|"
                         r"20\d\d|ytd|career|all[- ]time|history)\b", re.I)


def _subject_finder(ctx: dict):
    """f(text) -> the member a stretch of text is about (last mention wins):
    'you/your' = the caller, 'I/my' = Picky, a team or its group nickname."""
    me = (ctx.get("you") or {}).get("team_name")
    nick = member_nicknames()
    teams = [r["team"] for r in ctx.get("standings_ytd") or []]
    pats = [(re.compile(rx, re.I), t) for t in teams for rx in _member_rx(t, nick)]
    pats += [(_BLINKS_ASK, "Blinkstradamus"), (_ACKBAR_ASK, "Ackbar")]
    if (ctx.get("leader") or {}).get("team_name"):
        pats.append((_LEADER_ALIAS, ctx["leader"]["team_name"]))
    pats.append((re.compile(r"\b(you|your|you'?re|ya)\b", re.I), me))
    pats.append((re.compile(r"\b(I|I'?m|(?i:my|me))\b"), "__picky__"))

    def find(text: str) -> str | None:
        best = None
        for rx, t in pats:
            for m in rx.finditer(text or ""):
                if t and (best is None or m.start() >= best[0]):
                    best = (m.start(), t)
        return best[1] if best else None
    return find


def week_claim_check(ctx: dict, question: str):
    """Sentence test: weekly W-L, 'missed N of 4', 'hit N of 4', 'tied with X',
    'Nth place' and 'missed the cover by N' / 'won by N' claims must match the
    computed data. None when nothing is final this week."""
    wk, done, n_pool = _week_state(ctx)
    weekly = ctx.get("weekly_records") or {}
    recs = {t: tuple(v) for t, r in weekly.items() for k, v in (r or {}).items() if str(k) == str(wk)}
    prow = [r for r in picky_pick_results(ctx) if r["result"] in ("WIN", "LOSS")]
    if prow:
        recs["__picky__"] = results_record(prow)
    st = ctx.get("standings_ytd") or []
    season = {r["team"]: (r["w"], r["l"]) for r in st}
    srank = {r["team"]: r["rank"] for r in st}
    wrank = {t: 1 + sum(1 for t2, v2 in recs.items() if t2 != "__picky__" and v2[0] > v[0])
             for t, v in recs.items() if t != "__picky__"}
    # margins: this week's finals and last week's
    live = {x["GameId"]: x for x in ctx.get("live_games") or []}
    games = []
    for g in ctx.get("games_this_week") or []:
        lg = live.get(g["id"]) or {}
        games.append((g["away"], g["home"], _pts(lg.get("AwayPts")), _pts(lg.get("HomePts")), _pts(g.get("line")),
                      bool(g.get("winner")) or lg.get("Status") == "final"))
    for g in (ctx.get("last_week") or {}).get("games") or []:
        games.append((g.get("away"), g.get("home"), _pts(g.get("away_pts")), _pts(g.get("home_pts")),
                      _pts(g.get("line")), True))
    margins = []  # (teams, game margin, cover margin)
    for a, h, ap, hp, ln, fin in games:
        if fin and ap is not None and hp is not None:
            margins.append(({a, h}, abs(hp - ap), abs(hp + ln - ap) if ln is not None else None))
    if not done and not margins:
        return None
    find = _subject_finder(ctx)

    def named_margins(sent: str):
        hit = [m for m in margins if any(t and re.search(_team_re(t, False), sent, re.I) for t in m[0])]
        return hit or margins

    def ok(sent: str) -> bool:
        s = sent.replace("’", "'")
        other = _OTHER_SPAN.search(s)
        if done and not other:
            for m in _WK_MISS.finditer(s):
                who = find(s[:m.start()]) or find(question or "")
                n1, tot = _numval(m.group(1)), _numval(m.group(3))
                rec = recs.get(who)
                if rec and n1 is not None and tot is not None and tot <= max(n_pool, 4):
                    if n1 != rec[1]:
                        return False
            for m in _WK_MISS_G.finditer(s):
                who = find(s[:m.start()])
                n1, rec = _numval(m.group(1)), recs.get(who)
                if rec and n1 is not None and n1 <= max(n_pool, 4) and n1 != rec[1]:
                    return False
            for m in _WK_HIT.finditer(s):
                who = find(s[:m.start()])
                n1, tot, rec = _numval(m.group(1)), _numval(m.group(3)), recs.get(who)
                if not rec or n1 is None or tot is None or tot > max(n_pool, 4) or n1 > tot:
                    continue
                want = rec[1] if (m.group(5) or "").strip().lower() == "wrong" else rec[0]
                if n1 != want:
                    return False
            for m in _WK_REC.finditer(s):
                rec0 = (int(m.group(1)), int(m.group(2)))
                if sum(rec0) > max(n_pool, 4):
                    continue  # a season number: record_check's job
                who = find(s[:m.start()])
                if who in recs and rec0 != recs[who] and rec0 != season.get(who):
                    return False
            for m in _WK_TIE.finditer(s):
                other_t = find(s[m.end():m.end() + 40])
                who = find(s[:m.start()])
                if who and other_t and who != other_t and who != "__picky__" and other_t != "__picky__":
                    same_wk = who in recs and recs.get(who) == recs.get(other_t)
                    same_season = who in season and season.get(who) == season.get(other_t)
                    if not (same_wk or same_season):
                        return False
            for m in _WK_PLACE.finditer(s):
                w = m.group(1).lower()
                n = _ORD.get(w) or int(re.sub(r"\D", "", w))
                who = find(s[:m.start()])
                if who in srank and n not in (srank[who], wrank.get(who)):
                    return False
        if not re.search(r"\b20\d\d\b|\blast season\b|\blast year\b", s, re.I):
            for m in _COVER_BY.finditer(s):
                if re.search(r"\b(need(ed|s)?|had) to\s*$", s[max(0, m.start() - 12):m.start()], re.I):
                    continue
                v = _numval(m.group(3), m.group(4))
                cands = {x[2] for x in named_margins(s) if x[2] is not None}
                if v is not None and cands and v not in cands:
                    return False
            for m in _GAME_BY.finditer(s):
                if re.search(r"\b(need(ed|s)?|had|have|has) to\s*$", s[max(0, m.start() - 14):m.start()], re.I) \
                        or re.match(r"\s*(or more|or fewer|or less|or better)", s[m.end():], re.I):
                    continue
                if re.search(r"\b(cover|spread|number|line)\b", m.group(2) or "", re.I):
                    continue
                v = _numval(m.group(3), m.group(4))
                nm = named_margins(s)
                cands = {x[1] for x in nm}
                if v is not None and cands and v not in cands:
                    return False
                # direction: 'took Boise State -13.5 and watched it lose by five' when Boise WON by 5
                if len(nm) == 1 and nm is not margins and v == nm[0][1]:
                    a, h, ap, hp = next(((a, h, ap, hp) for a, h, ap, hp, _ln, fin in games
                                         if {a, h} == nm[0][0] and ap is not None and hp is not None), (None,) * 4)
                    if a and ap != hp:
                        winner = a if ap > hp else h
                        named = [t for t in (a, h) if re.search(_team_re(t, False), s, re.I)]
                        lost_verb = m.group(1).lower() in ("lost", "lose", "loses", "fell", "losing", "blown out",
                                                           "beaten", "outscored")
                        if named == [winner] and lost_verb:
                            return False
                        if named and winner not in named and not lost_verb:
                            return False
        return True
    return ok


def record_check(ctx: dict, question: str, card: str = ""):
    """A sentence test for invented records: a season record ('34-18 overall')
    for the caller or a named member must be their real one; on a pure
    opinion question any W-L must come from the opinion riders. A W-L or score
    printed in a code-computed GAME CARD / standings block (`card`) is true."""
    card_recs = set(re.findall(r"\b\d{1,2}-\d{1,2}\b", card or ""))
    st = {r["team"]: (r["w"], r["l"]) for r in ctx.get("standings_ytd") or []}
    me = (ctx.get("you") or {}).get("team_name")
    op = opinion_note(question, ctx)
    op_only = bool(op) and not (_PICKS_ASK.search(re.sub(r"\b(took|taking)\b", "", question or ""))
                                or RESULTS_ASK.search(question or ""))
    op_recs = set(re.findall(r"\b\d{1,2}-\d{1,2}\b", op))
    last8 = {}
    for t, lines in ((load_lore() or {}).get("members") or {}).items():
        for ln in lines:
            m8 = re.search(r"last 8 picks (\d+)-(\d+)", ln)
            if m8:
                last8[t] = (int(m8.group(1)), int(m8.group(2)))

    # fake data from the few-shot examples ('locked in the vault until Sat 2:30 PM CT', Gus, Rita)
    real = " ".join(f"{g.get('away')} {g.get('home')} {_kickoff(g.get('start')) if g.get('start') else ''}"
                    for g in ctx.get("games_this_week") or []) + " " + " ".join(r["team"] for r in ctx.get("standings_ytd") or [])
    fakes = [f for f in ("2:30 PM", "Gus", "Rita") if f not in real]
    money_ok = {x.replace(",", "") for x in re.findall(r"\$\s?([\d,]*\d)", league_rules.current_text())}
    n_teams = len(ctx.get("teams") or [])
    if n_teams:
        money_ok |= {str(v) for v in payout_math(n_teams).values()}
    money_ok |= {x.replace(",", "") for x in re.findall(r"\$\s?([\d,]*\d)", op)}  # checked buyout facts

    # last-week questions: a member's W-L must be his real last-week record (m10 gave Bub his
    # this-week 0-4 as "last week")
    lw_rec = {}
    if re.search(r"\blast week\b", question or "", re.I):
        for g in (ctx.get("last_week") or {}).get("games") or []:
            for t, pk in (g.get("picks") or {}).items():
                w_, l_ = lw_rec.get(t, (0, 0))
                lw_rec[t] = (w_ + 1, l_) if same_team(pk, g.get("covered")) else (w_, l_ + 1)

    busts = {}
    for t, lines in (((load_lore() or {}).get("casino") or {}).get("members") or {}).items():
        for ln in lines:
            mb = re.search(r"busted (once|twice|(\d+) times)|never busted", ln)
            if mb and "blackjack hands" in ln:
                busts[t] = 0 if "never" in mb.group(0) else 1 if mb.group(1) == "once" else 2 if mb.group(1) == "twice" else int(mb.group(2))

    gf = game_facts(ctx)
    mat_recs = material_records(ctx)
    blinks_q = bool(blinks_note(ctx, question or "")) and "Blinkstradamus" in st
    # who won/lost outright, this week and last ('Boise lost outright' when Boise won by 5)
    outr = [(g["won_game"], g["lost_game"]) for g in gf["games"] if g.get("won_game")]
    for g in (ctx.get("last_week") or {}).get("games") or []:
        ap, hp = _pts(g.get("away_pts")), _pts(g.get("home_pts"))
        if ap is not None and hp is not None and ap != hp:
            outr.append((g["away"], g["home"]) if ap > hp else (g["home"], g["away"]))
    # perfect weeks: each member's weekly W-L, and Picky's own ('my 4-0 week' when he's 0-3)
    wk_recs = {t: [tuple(x) for x in (r or {}).values()] for t, r in (ctx.get("weekly_records") or {}).items()}
    pp = ctx.get("picky_player") or {}
    pres = [x.get("result") for x in pp.get("this_week") or []]
    picky_recs = [(pres.count("covered"), pres.count("missed")), tuple(pp.get("last_week") or (0, 0))]

    wc = week_claim_check(ctx, question)

    def ok(sent: str) -> bool:
        if any(re.search(rf"\b{re.escape(f)}\b", sent) for f in fakes):
            return False
        if wc and not wc(sent):  # weekly W-L, 'missed N of 4', tied-with, place, cover/game margins
            return False
        if gf["games"] and check_answer(sent, gf):  # a final's winner/loser or cover flipped
            return False
        mo = re.search(r"\b(lost|lose|loses|losing|won|win|wins)( it| the game)? outright\b|\boutright (loss|win)\b",
                       sent, re.I)
        if mo and not re.search(r"\b20\d\d\b|\bweek \d", sent, re.I):  # lore's dated history is checked upstream
            lost = bool(re.search(r"los", mo.group(0), re.I))
            names = {x for pair in outr for x in pair}

            def named(t):  # 'Texas' inside 'North Texas' isn't Texas
                s2 = sent
                for o in names:
                    if o != t and t.lower() in o.lower():
                        s2 = re.sub(re.escape(o), " ", s2, flags=re.I)
                return re.search(rf"(?<!\w){re.escape(t)}(?!\w)", s2, re.I)
            for w, l in outr:
                hw, hl = named(w), named(l)
                if (lost and hw and not hl) or (not lost and hl and not hw):
                    return False
        for mp in re.finditer(r"\b([1-4])-0 (?:week|weekend|slate)\b|\bperfect week\b", sent, re.I):
            bef = sent[max(0, mp.start() - 40):mp.start()]
            if re.search(r"\b(my|i|i'?m|i'?ve)\b", bef, re.I):
                recs = picky_recs
            elif re.search(r"\byou(r|'re)?\b", bef, re.I):
                recs = wk_recs.get(me)
            else:
                who_p = [t for t in wk_recs if re.search(rf"(?<!\w){re.escape(t)}(?!\w)", sent, re.I)]
                recs = wk_recs.get(who_p[0]) if len(who_p) == 1 else None
            if recs is not None and not any(
                    l_ == 0 and w_ > 0 and (mp.group(1) is None or w_ == int(mp.group(1))) for w_, l_ in recs):
                return False
        mb = re.search(r"\bbust(?:ed|s)? (once|twice|(\d+|two|three|four) times)\b", sent, re.I)
        if mb and busts:  # 'busted twice' when the stored hands say once (three runs running)
            n = {"once": 1, "twice": 2, "two": 2, "three": 3, "four": 4}.get((mb.group(2) or mb.group(1)).lower())
            n = n if n is not None else int(mb.group(2))
            who = next((t for t in busts if re.search(rf"(?<!\w){re.escape(t)}(?!\w)", sent, re.I)), None)
            if who is None or busts[who] != n:
                return False
        if lw_rec and not re.search(r"\bthis week\b", sent, re.I):
            for t, rec0 in lw_rec.items():
                for mm in re.finditer(rf"(?<!\w){re.escape(t)}(?!\w)[^.;]{{0,40}}?\b(\d)-(\d)\b", sent):
                    if (int(mm.group(1)), int(mm.group(2))) != rec0:
                        return False
        if re.search(r"\b\d{1,2}-win (team|season|program)\b|\b(undefeated|unbeaten) [A-Z]", sent):
            return False  # 'Notre Dame, a 10-win team': team records only from the rankings data, as written
        # no dollar amounts but the rules' and the computed payouts ('the $500 rake' was invented)
        for m in re.finditer(r"\$\s?([\d,]*\d)|\b([\d,]*\d) (bucks|dollars|grand)\b", sent, re.I):
            if (m.group(1) or m.group(2)).replace(",", "") not in money_ok:
                return False
        for m in re.finditer(r"\b(\d{1,2})-(\d{1,2})\b", sent):
            rec = (int(m.group(1)), int(m.group(2)))
            if m.group(0) in card_recs:
                continue  # straight from the GAME CARD / standings block (computed from CFBD)
            if op_only and m.group(0) not in op_recs:
                return False
            after = sent[m.end():m.end() + 30]
            before = sent[max(0, m.start() - 45):m.start()]
            # past seasons exist only as win totals (lore): any W-L pinned on one is invented ('0-3 last season')
            if re.search(r"^\s*(last (season|year)|in 20(22|23|24|25)|back in 20)", after, re.I) or \
                    re.search(r"\b(last (season|year)|in 20(22|23|24|25))\b[^.]{0,15}$", before, re.I):
                return False
            lastn = re.search(r"^\s*(?:in|over|across) (?:his|their|your|my|the) last (\w+) (?:games|picks)", after, re.I) \
                or re.search(r"\blast (\w+) (?:games|picks)\b[^.]{0,12}$", before, re.I)
            if lastn:  # 'he's 1-7 in his last eight games': only the lore's real last-8 may be quoted
                if lastn.group(1).lower() not in ("8", "eight"):
                    return False
                who = me if re.search(r"\byou(?:'re| are|r)?\b", before + after, re.I) else next(
                    (t for t in st if re.search(rf"(?<!\w){re.escape(t)}(?!\w)", sent, re.I)), None)
                if not who or last8.get(who) != rec:
                    return False
                continue
            if re.search(r"\b(vs\.?|versus|head to head|h2h|opposite)\b|\bagainst (?!the (spread|line|number))", sent, re.I):
                continue  # head-to-head records aren't season records ('against the spread' is)
            # a week-sized W-L pinned on the caller must be one of his recent weeks (spar r1 2026-11-16: Ackbar,
            # 3-1 and 2nd, got "someone who's currently 0-3", Picky's own record; Rettino, 1-3, got "0-4 vibes")
            subj = list(re.finditer(r"(?<![\w'])(you|you'?re|your|you'?ve|someone who(?:'?s| is)|a guy who(?:'?s| is)|"
                                    r"i|i'?m|my|me)(?![\w'])", before, re.I))
            if sum(rec) <= 4 and me and subj and not re.fullmatch(r"i|i'?m|my|me", subj[-1].group(1), re.I) \
                    and not re.search(r"\b(if you|you'?d|need|needs|would|could|gonna|going to|you go)\b", before[-25:], re.I) \
                    and not any(re.search(rf"(?<!\w){re.escape(t)}(?!\w)", before[subj[-1].start():], re.I)
                                for t in st if t != me):
                mine = (ctx.get("weekly_records") or {}).get(me) or {}
                recent = [tuple(mine[k]) for k in sorted(mine, key=lambda k: int(k) if str(k).isdigit() else -1)[-3:]]
                if recent and rec not in recent:
                    return False
            season_claim = (re.search(r"^\s*(overall|on the (season|year)|all season|for the season|season record)\b",
                                      after, re.I) or sum(rec) > 8  # a week has 4 games: past 8 it's a season number
                            or re.search(r"\bseason record\b[^.]{0,30}\b(is|of|at|sits at)\s*$", before, re.I))
            if not season_claim or re.search(r"\b(week|weeks|last week|this week)\b", before[-25:], re.I):
                continue
            who = None
            if re.search(r"\byou(?:'re| are|r)?\b", before, re.I):
                who = me
            for t in st:
                if re.search(rf"(?<!\w){re.escape(t)}(?!\w)", before, re.I):
                    who = t
            if who is None and blinks_q and (_BLINKS_ASK.search(before)
                                             or re.search(r"\b(he|he'?s|his|him)\b", before, re.I)):
                who = "Blinkstradamus"  # 'Blinky's 19-25' / "he's 19-25" in the running bit
            if who is None and _ACKBAR_ASK.search(before):
                who = "Ackbar"  # 'Admiral Akbar is 19-25'
            mat_ok = (m.group(0) in mat_recs.get(who, ()) or oddity_ok(who, m.group(0), sent)) and not re.search(
                r"\b(this season|overall|on the (season|year)|season record|for the season|all season|so far)\b",
                sent, re.I)  # a true split ('favorites went 22-14'), never passed off as the season record
            if who in st and rec != st[who] and not mat_ok:
                return False
        return True
    return ok


# Picky's own picks (live 2026-11-15): "I'm the only one who knows how to pick a losing team like Oregon
# State", when he took Boise State -13.5 and Oregon State was the other side. Anything Picky says about
# what he picked, his record or a pick's result must match his revealed, graded picks.
_ME = r"(?<![\w'])(?:i|i'm|i've|i'd|i am|me|my|myself)(?![\w'])"
_PICK_VERB_1P = re.compile(
    r"\b(?:pick(?:ed|ing|s)?|took|tak(?:e|es|ing)|back(?:ed|ing)?|rode|rid(?:e|ing)|bet(?:ting)? on|went with|"
    r"go(?:ing)? with|roll(?:ed)? with|hammer(?:ed)?|lock(?:ed)? in|grabb?(?:ed)?|was on|am on|'m on|"
    r"fad(?:e|ed|ing))\b", re.I)
_HYPO = re.compile(r"\b(would|could|should|might|will|gonna|going to|if|next week|wish)\b|'d\b|'ll\b", re.I)
_NEG_1P = re.compile(r"\b(never|not|didn'?t|did not|don'?t|wouldn'?t|won'?t|no way)\b", re.I)
_OTHER_SUBJ = re.compile(r"\b(you|your|you'?re|he|she|they|his|her|their|we|everyone|everybody|nobody|who else)\b"
                         r"|[,;]|\b(but|while|and|whereas)\b", re.I)
_YOU = r"(?<![\w'])(?:you|you're|you've|you are|your|yourself)(?![\w'])"
_OTHER_SUBJ_2P = re.compile(r"\b(i|i'?m|me|my|he|she|they|his|her|their|we|everyone|everybody|nobody|who else)\b"
                            r"|[,;]|\b(but|while|and|whereas)\b", re.I)
_AGAINST = re.compile(r"\b(over|against|vs\.?|versus|instead of|fad(?:e|ed|ing))\s+(?:the\s+)?$", re.I)
_SELF_PERFECT = re.compile(_ME + r"[^.!?]{0,30}?\b(undefeated|unbeaten|perfect|spotless|haven'?t missed|can'?t miss|"
                           r"never miss|haven'?t lost|hit (them|'?em) all|swept)\b", re.I)
_SELF_WINLESS = re.compile(_ME + r"[^.!?]{0,30}?\b(winless|haven'?t (hit|won) (a|one|any)|0-for|zero wins|no wins)\b",
                           re.I)


def picky_claim_check(ctx: dict, who: str = "picky"):
    """Sentence test: anything Picky says about his own picks ('I took X', 'my
    Boise pick hit', "I'm 2-1", 'undefeated') must match his revealed, graded
    picks. None when Picky isn't loaded (nothing to hold him to).
    who="you": the same pick-claim test for the sender ('you took X', 'at least
    you're not the guy who took LSU' when he did), against their revealed picks
    this week and last week."""
    facts = None
    if who == "you":
        me = (ctx.get("you") or {}).get("team_name")
        gid = {g["id"]: g for g in ctx.get("games_this_week") or []}
        picks = [{**gid[p["game_id"]], "pick": p["pick"]} for p in (ctx.get("revealed_picks_this_week") or {}).get(me, [])
                 if p["game_id"] in gid and gid[p["game_id"]].get("winner")] if me else []
        picks += [{"away": g.get("away"), "home": g.get("home"), "winner": g.get("covered"), "pick": g["picks"][me]}
                  for g in (ctx.get("last_week") or {}).get("games") or [] if me and (g.get("picks") or {}).get(me)]
        if not picks:
            return None
        anchor, others, brk = _YOU, _OTHER_SUBJ_2P, r"i|he|she|they|ackbar|chuck"
        facts = results_facts(ctx, "how did my picks do?")  # only for the straight-up-vs-cover check below
        if facts:
            facts = {**facts, "subjects": [s for s in facts["subjects"] if s["kind"] == "you"]}
    else:
        pp = ctx.get("picky_player")
        if not pp:
            return None
        picks = [p for p in (pp.get("revealed") or pp.get("this_week") or []) if p.get("pick")]
        facts = results_facts(ctx, "how did your picks do, picky?")
        if facts:
            facts = {**facts, "subjects": [s for s in facts["subjects"] if s["kind"] == "picky"]}
        anchor, others, brk = _ME, _OTHER_SUBJ, r"you|he|she|they|ackbar|chuck"
    # teams in decided games only: a line on an undecided game is a take, not his record
    decided = [(g["away"], g["home"]) for g in ctx.get("games_this_week") or [] if g.get("winner")]
    decided += [(g.get("away"), g.get("home")) for g in (ctx.get("last_week") or {}).get("games") or []]
    decided += [(p["away"], p["home"]) for p in picks if p.get("winner")]
    names = sorted({t for pair in decided for t in pair if t}, key=len, reverse=True)
    rx = {t: re.compile(_team_re(t, False), re.I) for t in names}
    graded = [p for p in picks if p.get("winner") and not p.get("tiebreaker")]
    wins = sum(same_team(p["pick"], p["winner"]) for p in graded)
    season = (pp.get("season") or [0, 0]) if who == "picky" else [0, 0]
    has_w, has_l = (bool(wins or season[0]), bool(len(graded) - wins or season[1])) if who == "picky" else (False, False)

    def took(team: str) -> bool | None:
        """Did Picky take `team`? None when he didn't pick that game at all."""
        mine = [p for p in picks if same_team(team, p["away"]) or same_team(team, p["home"])]
        return any(same_team(team, p["pick"]) for p in mine) if mine else None

    def first_team(text: str):
        best = None
        for t in names:
            m = rx[t].search(text)
            if m and (best is None or (m.start(), -m.end()) < (best[1].start(), -best[1].end())):
                best = (t, m)
        return best

    def claimed(rest: str, fade: bool) -> list[tuple[str, bool]]:
        """[(team, against)] right after a pick verb: the first team (fillers
        like 'a losing team like' allowed) and the run joined to it by 'and',
        commas or 'over'. against = the claim is the other side of that game."""
        hit = first_team(rest)
        if not hit or re.search(rf"[;:,.!?]|\b(but|and|{brk})\b", rest[:hit[1].start()], re.I):
            return []
        out = [(hit[0], fade or bool(_AGAINST.search(rest[:hit[1].start()])))]
        pos = hit[1].end()
        while True:
            sep = re.match(r"\s*(?:[-+]\d+(?:\.\d+)?)?\s*(?:(,\s*(?:and\s+)?|\s+and\s+|\s*&\s*)|"
                           r"\s+(over|against|vs\.?|versus|instead of)\s+)(?:the\s+)?", rest[pos:], re.I)
            nxt = first_team(rest[pos + sep.end():]) if sep else None
            if not nxt or nxt[1].start() != 0:
                break
            after = rest[pos + sep.end() + nxt[1].end():]
            if re.match(r"\s+(cover\w*|won|wins|lost|loses|beat|smoked|crushed|killed|did|didn'?t|was|were|is|got|"
                        r"ran|blew|rolled|dominated|pulled|went|ended|finished)\b", after, re.I):
                break  # 'I took North Texas -2.5 and UTSA covered': a new clause
            out.append((nxt[0], (not fade) if sep.group(2) else fade))
            pos += sep.end() + nxt[1].end()
        return out

    def ok(sent: str) -> bool:
        s = sent.replace("’", "'").replace("‘", "'")
        if not re.search(anchor, s, re.I):
            return True
        if facts and facts["subjects"] and (su_framing_problems(s, facts) if who == "you" else check_answer(s, facts)):
            return False  # 'my Boise pick cashed', "I'm 3-0 this week"; 'your Boise pick won' (it didn't cover)
        m = _SELF_PERFECT.search(s)
        if has_l and m and not re.search(r"\b(not|far from|never been)\b", m.group(0), re.I):
            return False
        m = _SELF_WINLESS.search(s)
        if has_w and m and not re.search(r"\b(not|far from)\b", m.group(0), re.I):
            return False
        for me in re.finditer(anchor, s, re.I):
            v = _PICK_VERB_1P.search(s, me.end(), min(len(s), me.end() + 45))
            if not v:
                continue
            between = s[me.end():v.start()]
            if _HYPO.search(between) or others.search(between):
                continue  # 'I'd take', 'I think you took', "I'm 0-3, but Ackbar took"
            neg = bool(_NEG_1P.search(between))
            fade = v.group(0).lower().startswith("fad")
            for t, against in claimed(s[v.end():v.end() + 80], fade):
                got = took(t)
                if got is None:
                    if not neg:
                        return False  # 'I took Ohio State': he never picked that game
                    continue
                if (got == (not against)) == neg:
                    return False  # 'pick a losing team like Oregon State' when he took Boise State
        return True
    return ok


# 'last in the league at being a Notre Dame homer': a league superlative only for a real stat
_FAKE_SUPERLATIVE = re.compile(
    r"\b(?:first|1st|last|dead last|worst|best|number one|no\. ?1|#1|top|bottom)\s+in\s+(?:the\s+)?"
    r"(?:league|pool|group|chat)\s+(?:at|in|for)\s+(?P<what>[^.!?,;]{0,60})", re.I)
_STAT_WORDS = re.compile(r"\b(picks?|picking|covers?|covering|wins?|winning|loss(?:es)?|losing|records?|points|chips|"
                         r"standings|ats|spreads?|underdogs?|dogs|favorites?|chalk|upsets?|perfect weeks?|streaks?|"
                         r"hit rate|blackjack|slots|bankroll|jackpot|misses|games?)\b", re.I)


def fake_superlative(sent: str) -> bool:
    m = _FAKE_SUPERLATIVE.search(sent)
    return bool(m) and (re.match(r"\s*being\b", m.group("what"), re.I) is not None
                        or not _STAT_WORDS.search(m.group("what")))


# Sexual/anatomy vulgarity is out everywhere (R-rated yes, sexual no): m11 had "USC can suck my dick".
_ANATOMY = re.compile(r"\b(dick|dicks|cock|cocks|penis|weenie|wiener|schlong|pecker|tits?|boobs?|blow ?jobs?|"
                      r"suck (my|his|a))\b", re.I)  # trailing \b: 'tit' matched 'title' (title game -> 'vulgar', sentence cut)


# Verbal tics from the exam (m4: "don't get cocky" closed 16 of 122 answers). A
# short filler sentence built on one is dropped; a longer one with real content stays.
_TICS = re.compile(r"\b(don'?t get cocky|sitting pretty|the rest of us|here to watch|dumpster fire|god among men|"
                   r"so maybe you should|go(es|ing)? to (your|his|her|their) head|let that sink in)\b", re.I)  # board-voice tic, 11/16


def week_cards(ctx: dict) -> dict:
    """This week's pool game cards by game id (market now/open for the line-number check)."""
    out = {}
    for g in ctx.get("games_this_week") or []:
        try:
            c = game_cards.load_card(ctx.get("current_week"), g["id"])
        except Exception:  # noqa: BLE001
            c = None
        if c:
            out[str(g["id"])] = c
    return out


def _named_checks(checks: list):
    """All the per-sentence fact checks as one callable; logs which one dropped a sentence (genius exam
    2026-11-17: true game-card facts were being cut and the log couldn't say by what)."""
    live = [(n, f) for n, f in checks if f is not None]
    if not live:
        return None

    def ok(x):
        for n, f in live:
            if not f(x):
                log.info("fact check %s dropped: %r", n, x[:100])
                return False
        return True
    return ok


def _both(a, b):
    if a is None or b is None:
        return a or b
    return lambda x: a(x) and b(x)


_VALUE_WORD = re.compile(r"\b(value|free points?)\b", re.I)
_VALUE_IF = re.compile(r"\b(if|holds?|holding|tuesday|until|till|before the lock|line lock|still move|"
                       r"can (still )?move)\b", re.I)
_ONLY_VALUE = re.compile(r"\bonly (real |true |actual )?value\b|\b(no|zero) (line )?value\b", re.I)


def value_claim_check(ctx: dict):
    """Before the Tue 11 AM CT line lock a league-vs-market gap isn't banked (the league line can still
    move): a sentence calling something value has to say 'if it holds through the lock', and 'the only
    value side' / 'no value' never goes out (live 2026-11-16: 'Iowa -6.5, the only value side', with
    Northwestern -2.5 vs -3 also a gap). After the lock it's real value, but 'only value side' still
    can't go out when two or more games have a gap."""
    if ctx.get("error") or not ctx.get("games_this_week"):
        return None
    locked = lines_locked(ctx)
    espn_all = ctx.get("espn") or {}
    gaps = sum(1 for g in ctx["games_this_week"] if not g.get("winner")
               and (mg := _market_gap(espn_all.get(g["id"]) or {}, g.get("line"))) and abs(mg[1]) >= VALUE_MIN_GAP)
    if locked and gaps < 2:
        return None

    pool_teams = [t for g in ctx["games_this_week"] for t in (g.get("away"), g.get("home")) if t]

    def ok(sent: str) -> bool:
        if _ONLY_VALUE.search(sent):
            return False
        if not any(same_team_in(sent, t) for t in pool_teams):
            return True  # a non-pool game (GAME CARD) has no league line to lock: the rail is about pool games
        return locked or not _VALUE_WORD.search(sent) or bool(_VALUE_IF.search(sent))
    return ok


def same_team_in(sent: str, team: str) -> bool:
    return bool(re.search(rf"(?<!\w){re.escape(team)}(?!\w)", sent or "", re.I))


_MY_PICK = re.compile(r"\b(i'?m (taking|going with|sticking with|riding|rolling with|backing|on|staying with|staying the course with|with)|"
                      r"stay(ing)? (the course )?with|i'?ve got|my guys? (is|are)|i'?d (take|go with|ride|back|lean)|"
                      r"my (picks?|calls?|locks?|play|lean) (is|are|was)|i (like|took|picked|lean)|go with|take|sticking with|"
                      r"lock (it )?in)\b", re.I)
_YOUR_PICK = re.compile(r"\b(you('?ve)? (got|have|took|picked|are on|'re on)|your (pick|picks))\b", re.I)


def card_side_check(ctx: dict):
    """Before the week locks, a sentence where Picky picks or advises a side ('I'm sticking with Iowa -8.5', 'Go with
    LSU -1.5') must match that game's card call; the code calls (slate, pick, flip lanes) are what he says
    elsewhere, so a contradiction reads as flip-flopping (keeper k1 live run, 2026-11-17)."""
    if ctx.get("error") or week_started(ctx):
        return None
    games = [g for g in ctx.get("games_this_week") or [] if not g.get("tiebreaker") and g.get("line") is not None]
    lean = {}
    for g in games:
        c = game_cards.load_card(ctx.get("current_week"), g["id"])
        if c and float(c["league_line"]["home"]) == float(g["line"]) and (c.get("lean") or {}).get("side"):
            lean[g["id"]] = c["lean"]["side"]
    if not lean:
        return None
    gl = [{"Away": g["away"], "Home": g["home"], "id": g["id"]} for g in games]
    by = {g["id"]: g for g in games}

    def ok(sent: str) -> bool:
        if not _MY_PICK.search(sent or "") or _YOUR_PICK.search(sent or ""):
            return True
        q = f" {_match_key(re.sub(r"['’]s\b", "", sent))} "
        for short, full in _TEAM_SHORT.items():
            q = q.replace(f" {short} ", f" {full} ")
        for n in games_named(sent, gl, cap=4):
            g = by[n["id"]]
            if n["id"] not in lean:
                continue
            said = [x for x in (g["away"], g["home"]) if f" {_match_key(x)} " in q]
            if len(said) == 1 and not same_team(said[0], lean[n["id"]]):
                return False
        return True
    return ok


_SET_CLAIM = re.compile(r"\b(set in stone|locked in|already locked|are locked|is locked|locked to the market|aren'?t budging|"
                        r"isn'?t budging|won'?t (move|budge|change)|not (moving|budging|changing)|can'?t move|final number)\b", re.I)
_SET_OK = re.compile(r"\b(until|till|before|tuesday|tues|11|after (the )?lock|once (it|they) locks?|at the lock|when it locks)\b", re.I)


def line_set_check(ctx: dict):
    """Before Tuesday's 11 AM CT line lock no line is 'set in stone' or 'locked' (keeper k1: 'The pool lines for Iowa,
    LSU, and Texas Tech are currently locked to the market, so those aren't budging')."""
    if ctx.get("error") or not ctx.get("games_this_week") or lines_locked(ctx):
        return None
    return lambda sent: not (_SET_CLAIM.search(sent or "") and not _SET_OK.search(sent or ""))


_NO_MOVE = re.compile(r"\b(no|hasn'?t been any|hasn'?t been|without|zero) (real )?(line )?(movement|move|change)\b|\bhasn'?t (moved|changed)\b|"
                      r"\bnothing('?s| has) changed\b|\bno change\b|"
                      r"\bdidn'?t move\b|\bnot moved\b|\bnothing'?s moved\b|\bstill at the opener\b", re.I)


def move_claim_check(ctx: dict):
    """'There hasn't been any movement' about a game whose market moved since open (keeper k2: 'both exactly Iowa
    -8.5, so there hasn't been any movement' after Iowa went -5.5 -> -8.5) is cut."""
    games = [g for g in ctx.get("games_this_week") or [] if not g.get("tiebreaker")]
    moved = {}
    for g in games:
        c = game_cards.load_card(ctx.get("current_week"), g["id"])
        m = (c or {}).get("market") or {}
        if m.get("home_open") is not None and m.get("home_now") is not None and m["home_open"] != m["home_now"]:
            moved[g["id"]] = g
    if not moved:
        return None
    gl = [{"Away": g["away"], "Home": g["home"], "id": g["id"]} for g in games]

    def ok(sent: str) -> bool:
        if not _NO_MOVE.search(sent or ""):
            return True
        named = {n["id"] for n in games_named(re.sub(r"['’]s\b", "", sent), gl, cap=4)}
        return bool(named) and not (named & set(moved))
    return ok


_FAV_WORD = re.compile(r"\b(favou?rites?|favou?red|chalk|laying (the )?points|lay(ing)? \d)\b", re.I)
_DOG_WORD = re.compile(r"\b(underdogs?|dogs?|getting (the )?points|getting \d|plus the points)\b", re.I)
_CLAUSE = re.compile(r"[,;:()]|\s(?:but|while|and|whereas|though)\s", re.I)


def fav_claim_check(ctx: dict):
    """A sentence that calls a pool game's underdog the favorite/chalk (or the favorite the dog) is dropped
    (spar r1: 'You probably have the Vols because you lack the backbone to pick anything other than the chalk'
    and 'Tennessee +1.5 because they're the home favorites', Tennessee the +1.5 dog)."""
    games = [g for g in ctx.get("games_this_week") or [] if g.get("line") not in (None, 0, 0.0) and not g.get("tiebreaker")]
    if not games:
        return None
    gl = [{"Away": g["away"], "Home": g["home"], "id": g["id"]} for g in games]
    by = {g["id"]: g for g in games}

    def sides_in(clause: str, g: dict) -> set:
        q = f" {_match_key(clause)} "
        for short, full in _TEAM_SHORT.items():
            q = q.replace(f" {short} ", f" {full} ")
        return {side for side in (g["away"], g["home"]) if f" {_match_key(side)} " in q}

    def ok(sent: str) -> bool:
        for clause in _CLAUSE.split(sent or ""):
            fw, dw = _FAV_WORD.search(clause), _DOG_WORD.search(clause)
            if not (fw or dw) or (fw and dw):
                continue
            for n in games_named(clause, gl):
                g = by[n["id"]]
                fav = g["home"] if float(g["line"]) < 0 else g["away"]
                dog = g["away"] if fav == g["home"] else g["home"]
                named = sides_in(clause, g)
                if fw and named == {dog}:
                    return False
                if dw and named == {fav}:
                    return False
        return True
    return ok


_RECORD_JAB = re.compile(
    r"\bgo(es|ing)? to (your|his|her|their) head\b|"
    r"\b(you|you'?re|your|ur)\b.{0,60}\b(\d{1,2}-\d{1,2}\s+((this|last) )?(week|weekend|start|run|slate)|first place|"
    r"(perfect|hot|good|great|big) week|your record|that record)\b|"
    r"\b(\d{1,2}-\d{1,2}\s+((this|last) )?(week|weekend)|first place)\b.{0,60}\b(you|you'?re|your|ur)\b", re.I)


def advice_jab_check(ctx: dict, question: str):
    """Pick advice is advice: no tacked-on jab about the asker's record ('try not to let that 4-0 week go
    to your head', seen 4+ times on 2026-11-16). Only when the question gets game cards."""
    if not take_games(ctx, question):
        return None
    return lambda sent: not _RECORD_JAB.search(sent)


def trait_word_check(ctx: dict, question: str):
    """On a trait bait (race, religion...), a sentence that names the trait again
    ('Race doesn't matter, but his 2-2 week does') is dropped; the short refusal
    ('Hard no.') and the pivot to picks survive."""
    q = question or ""
    m = _TRAIT.search(q)
    if not (m and _MOCK.search(q)) or _BODY.search(q):
        return None
    word = re.escape(m.group(0).lower())
    return lambda sent: not re.search(rf"\b{word}\w*\b", sent, re.I)


# (E) Grounding: a sentence naming a player, an injury or a stat that isn't in
# the prompt (league brief, stats rows, game intel) is dropped. Player = a
# name in the CFBD player stats, or any unknown name next to a position word;
# coaches and jokes about celebrities pass. Stat = a number tied to yards,
# TDs, %, sacks... that appears nowhere in the prompt.
_POS_WORD = re.compile(r"\b(qbs?|quarterbacks?|rbs?|running ?backs?|tailbacks?|wrs?|receivers?|wideouts?|tight ends?|"
                       r"linebackers?|cornerbacks?|safety|safeties|kickers?|punters?|edge rushers?|lineman|linemen|"
                       r"defensive (end|tackle)s?|starters?|backups?|freshmen|freshman|transfers?)\b", re.I)
_INJ_CLAIM = re.compile(
    r"\b(?:is|are|was|were|remains?|been|got|listed as)\s+(?:still\s+|now\s+|reportedly\s+|likely\s+|officially\s+)?"
    r"(?:questionable|doubtful|injured|hurt|sidelined|banged[- ]up|day[- ]to[- ]day|a game[- ]time decision)\b|"
    r"\bruled out\b|"
    r"\bout for the (?:season|year|game)\b|\b(?:torn|sprained|broken|fractured)\s+(?:acl|mcl|labrum|meniscus|"
    r"achilles|leg|arm|hand|foot|wrist|ankle|collarbone|finger|thumb|ribs?|bone|hamstring|pec)\b|\bconcussion\b|"
    r"\b(?:acl|mcl|hamstring|ankle|knee|shoulder|foot|wrist|hand|back|leg|arm)\s+(?:injury|issue|surgery|sprain|tear|"
    r"problem)\b", re.I)
_HEDGE = re.compile(r"\b(no|not|unknown|unclear|can'?t say|don'?t know|nobody knows|no idea)\b|n't\b", re.I)
_STAT_NUM = re.compile(
    r"(?<![\w.#])(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*(%|percent\b|(?:total |passing |rushing |receiving |"
    r"all-purpose )?(?:yards?|yds)\b|ypc\b|ypa\b|touchdowns?\b|tds?\b|ints?\b|interceptions?\b|sacks?\b|"
    r"tackles?\b|catches\b|receptions?\b|carries\b|ppg\b|points per game\b|completions?\b)", re.I)
_NUMS = re.compile(r"\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?")
_GROUND_SKIP = {"dear leader", "supreme commissioner", "glorious creator", "picky", "pool line", "big ten",
                "sun belt", "mountain west", "conference usa", "fpi", "espn", "cfbd", "draftkings", "playoff",
                "stadium", "field", "bowl", "trophy", "conference", "university", "college football", "heisman",
                "january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
                "november", "december", "jan", "feb", "aug", "sept", "sep", "oct", "nov", "dec", "monday",
                "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday", "game intel", "open meteo"}
_player_keys: tuple | None = None


def _cfbd_player_keys(idx: dict | None) -> set:
    global _player_keys
    if not idx:
        return set()
    if _player_keys is None or _player_keys[0] is not idx:
        _player_keys = (idx, {p["key"] for p in idx.get("players") or [] if p.get("key") and " " in p["key"]})
    return _player_keys[1]


def ungrounded(sent: str, corpus_norm: str, corpus_nums: set, skip_names: set, players: set,
               idx: dict | None, has_intel: bool) -> str | None:
    """Why `sent` isn't grounded in the prompt (None = fine)."""
    s = sent.replace("\u2019", "'")
    for m in _STAT_NUM.finditer(s):
        n = m.group(1).replace(",", "")
        if n in ("0", "1") or (n == "100" and m.group(2).lower() in ("%", "percent")):
            continue
        if n not in corpus_nums:
            return f"stat {m.group(0)!r}"
    names = []
    for m in cfbd._NAME.finditer(re.sub(r"(\w)'s\b", r"\1", s)):
        words = m.group(1).split()
        while words and cfbd.norm(words[0]) in cfbd.STOPWORDS | cfbd._LEAD | {"the", "and", "but", "per", "like"}:
            words = words[1:]
        if len(words) < 2 or any(w.upper().strip(".") in cfbd._ACRONYMS for w in words):
            continue
        n = cfbd.norm(" ".join(words))
        if (not n or set(n.split()) & _GROUND_SKIP or any(k in n for k in _GROUND_SKIP if " " in k)
                or n in cfbd._NOT_NAMES or any(k and (k in n or n in k) for k in skip_names)
                or (idx and (cfbd.resolve_team(" ".join(words), idx) or cfbd.resolve_team(words[0], idx)
                             or cfbd.resolve_team(" ".join(words[:2]), idx)))):
            continue
        names.append(n)
    for n in names:
        last = n.split()[-1]
        if f" {n} " in corpus_norm or (len(last) >= 4 and f" {last} " in corpus_norm):
            continue
        if n in players or _POS_WORD.search(s):
            return f"player {n!r}"
    if _INJ_CLAIM.search(s) and not names and not has_intel and not _HEDGE.search(s):
        return "injury claim with no intel"
    return None


_SAYS_AVAILABLE = re.compile(r"\b(back|returns?|returning|healthy|good to go|will play|expected to play|set to play|"
                             r"cleared|available|should help|suits? up|is a go|full go|ready to go|playing)\b", re.I)
_SAYS_OUT = re.compile(r"\b(out|won'?t play|will not play|sidelined|ruled out|miss(es|ing)?|missed|done for|"
                       r"injured|hurt|surgery|doubtful|questionable|won'?t be back|isn'?t back|not back)\b", re.I)


_SAYS_RETURNING = re.compile(r"\b(expected back|back soon|should be back|will be back|back this week|"
                             r"returns? (this|on|saturday|friday)|ready (to go|for (saturday|friday|this))|"
                             r"good to go|will play|expected to play|cleared to play|set to play|full strength)\b",
                             re.I)


def contradicts_intel(sent: str, status: dict[str, str]) -> str | None:
    """A sentence that names a reported player and says the opposite of his
    reported status ('Toney is back' when the report says out)."""
    if not status:
        return None
    words = set(cfbd.norm(sent).split())
    for last, st in status.items():
        if last not in words:
            continue
        if st in ("out", "doubtful", "questionable") and (_SAYS_RETURNING.search(sent) or (
                _SAYS_AVAILABLE.search(sent) and not _SAYS_OUT.search(sent))):
            return f"says {last} is available, report says {st}"
        if st in ("probable", "returning") and re.search(r"\b(out|ruled out|won'?t play|will not play|sidelined)\b",
                                                          sent, re.I):
            return f"says {last} is out, report says {st}"
    return None


_ATTRIB = (("fpi", re.compile(r"\bFPI\b", re.I)), ("sp+", re.compile(r"\bSP\+", re.I)),
           ("efficiency", re.compile(r"\b(efficiency|PPA)\b", re.I)))
_PROJ_NUM = re.compile(r"\bby (\d+(?:\.\d+)?)\b")


def source_numbers(corpus: str) -> dict[str, set]:
    """{'fpi'|'sp+'|'efficiency': numbers on the prompt lines that cite it}."""
    out = {k: set() for k, _ in _ATTRIB}
    for line in re.split(r"[\n;]", corpus):
        for k, rx in _ATTRIB:
            if rx.search(line):
                out[k] |= set(_NUMS.findall(line))
    return out


def misattributed(sent: str, src_nums: dict[str, set]) -> str | None:
    """'FPI projects Miami by 19.9' when 19.9 is SP+'s number (or FPI isn't in
    the prompt at all)."""
    cited = [k for k, rx in _ATTRIB if rx.search(sent)]
    if len(cited) != 1:
        return None
    for n in _PROJ_NUM.findall(sent):
        if n not in src_nums[cited[0]]:
            return f"{cited[0]} by {n} isn't {cited[0]}'s number"
    return None


def grounded_check(ctx: dict, corpus: str, question: str = ""):
    """Sentence predicate for _filtered: True when every player, injury and
    stat in the sentence is backed by the prompt (brief + game intel), and no
    sentence contradicts a reported injury status."""
    if ctx.get("error") or not corpus:
        return None
    status = intel_status(ctx, question) if question else {}
    idx = _cfbd_index()
    cn = f" {cfbd.norm(corpus)} "
    nums = {x.replace(",", "") for x in _NUMS.findall(corpus)}
    nums |= {str(f(float(x))) for x in nums if "." in x for f in (math.floor, math.ceil)}  # 65.6% -> "66%" is fine
    skip = {cfbd.norm(x) for x in (ctx.get("teams") or []) + [r["team"] for r in ctx.get("standings_ytd") or []]
            + LEADER_NAMES if x}
    players = _cfbd_player_keys(idx)
    has_intel = "GAME INTEL," in corpus
    src = source_numbers(corpus)

    def ok(sent: str) -> bool:
        why = (ungrounded(sent, cn, nums, skip, players, idx, has_intel) or contradicts_intel(sent, status)
               or misattributed(sent, src))
        if why:
            log.info("grounding filter: %s in %r", why, sent[:100])
        return why is None
    return ok


# A news-style claim ("reportedly", "per ESPN", "sources say", "Dellenger reported") needs a source block
# in the prompt, and can't be a HOT TAKE dressed up as news (plan 2026-11-16: no [HOT] or rumor item
# stated as news). Source lines and takes are the prompt's "- " bullets.
_NEWSY = re.compile(r"\b(reported(ly)?|reports? (say|said|that|have|are)|sources? (say|said|tell|told|close)|"
                    r"breaking news|insiders?|rumou?r(s|ed)?|leak(ed|s)?|word is|i'?m told)\b", re.I)
_CREDIT = re.compile(r"\b[Pp]er (?:the )?[A-Z][A-Za-z0-9&+.]*|\b[Aa]ccording to\b")  # also fine on data ('per CFBD')
_SOURCE_BLOCKS = ("NEWS QUESTION (free", "INSIDER NOTES (", "GAME INTEL,", "CHECKED FACTS (", "CFB RANKINGS (",
                  "LIVE GAME FEED (")  # live_games.py: in-game injury reports from ESPN's play-by-play
_TAKES_HEAD = "HOT TAKES you can riff on"


def news_claim_check(corpus: str):
    """Sentence predicate for _filtered: False for a news-style claim with no
    source block in the prompt, or one whose words come from a HOT TAKE more
    than from any sourced line."""
    def words(t):
        return set(re.findall(r"[a-z0-9]{4,}", t.lower()))
    tk = corpus.split(_TAKES_HEAD, 1)[1].split("\n\n", 1)[0] if _TAKES_HEAD in corpus else ""
    take_lines = {ln for ln in tk.splitlines() if ln.startswith("- ")}
    takes = [words(ln) for ln in take_lines]
    srcs = [words(ln) for ln in corpus.splitlines() if ln.startswith("- ") and ln not in take_lines]
    sourced = any(b in corpus for b in _SOURCE_BLOCKS)

    def ok(sent: str) -> bool:
        newsy = bool(_NEWSY.search(sent))
        if not (newsy or _CREDIT.search(sent)):
            return True
        if newsy and not sourced:
            log.info("news filter: no source block for %r", sent[:100])
            return False
        w = words(sent)
        t = max((len(w & x) for x in takes), default=0)
        if t >= 3 and t > max((len(w & x) for x in srcs), default=0):
            log.info("news filter: hot take stated as news in %r", sent[:100])
            return False
        return True
    return ok


# Known-false mashups from the final exam run: only Picky has exhibition picks; Bo Nix never
# played at Notre Dame; Freeman's 2024 team lost the title game to Ohio State.
_MASHUPS = re.compile(r"\b(your|his|their|you'?re|he'?s|my|i'?m)\b[^.]{0,30}\bexhibition\b"
                      r"|\bNix\b[^.]{0,40}\b(Notre Dame|Irish)\b(?![^.]{0,40}\b(Oregon|Auburn)\b)"
                      r"|\b(Notre Dame|Irish)\b[^.]{0,40}\bNix'?s\b"
                      r"|\bFreeman\b[^.]{0,40}\b(can'?t lose to|owns|always beats|never loses to) Ohio State\b", re.I)


_MILK_SPILL = re.compile(
    r"\b(milk|carton|dairy|jug)\b[^.!?]{0,60}\bspill\w*|\bspill\w*[^.!?]{0,40}\b(milk|carton|dairy|jug)\b"
    r"|\b(dropp?\w*|crash\w*|knock\w*|tipp?\w*|smash\w*|splatter\w*)\s+(over\s+|off\s+)?(his\s+|the\s+|a\s+|that\s+|whole\s+)*"
    r"(milk|carton|jug)\b|\b(milk|carton|jug)\s+(fell|dropped|crashed|smashed|tipped|spilled|burst|went flying|hit the)\b"
    r"|\b(carton|milk|jug)\s+(off|onto|on)\s+the\s+(counter|road|ground|floor|pavement|street)", re.I)


def sentence_ok(sent: str, banned: set, body: bool = False, no_self: bool = False, offtopic: bool = False,
                sides: tuple | None = None, extra=None) -> bool:
    sent = sent.replace("\u2019", "'").replace("\u2018", "'")  # curly apostrophes slipped past "you'?re"
    if _MASHUPS.search(sent):
        return False
    if _FAMILY_DARK.search(sent) or _HEALTH_CLAIM.search(sent) or _ANATOMY.search(sent):
        return False
    if _MILK_SPILL.search(sent):  # Charlie: Blinkstradamus just held the milk; it never spilled
        return False
    if fake_superlative(sent):
        return False
    if rare_bits.mentions("milk", sent) and not MILK_OK.get():  # once a month, only about Blinky
        return False
    if _TICS.search(sent) and len(sent.split()) <= 14:
        return False
    if extra is not None and not extra(sent):
        return False
    if _MASCOT_RE is not None and _MASCOT_RE.search(sent):
        return False
    if sides and wrong_person_picks(sent, *sides):
        return False
    if offtopic and _OFFTOPIC_LEAK.search(sent):
        return False
    if body and _BODY_WORDS.search(sent):
        return False
    if no_self and _SELF_INTRUSION.search(sent):
        return False
    if _ngrams(sent) & banned:
        return False
    for n in (2, 3, 4, 5):  # short example jokes match as whole phrases
        if _ngrams(sent, n=n) & {g for g in _EXAMPLE_NGRAMS if len(g) == n}:
            return False
    return True


_EXAMPLE_NGRAMS = _example_ngrams()


def _drop_reason(sent: str, banned: set, body=False, no_self=False, offtopic=False, sides=None) -> str:
    """Which sentence_ok rule cut a sentence, for the log ('extra' = a fact check, logged by name)."""
    sent = sent.replace("\u2019", "'").replace("\u2018", "'")
    for name, hit in (("mashup", lambda: _MASHUPS.search(sent)),
                      ("family/health/anatomy", lambda: _FAMILY_DARK.search(sent) or _HEALTH_CLAIM.search(sent)
                       or _ANATOMY.search(sent)),
                      ("superlative", lambda: fake_superlative(sent)), ("tic", lambda: _TICS.search(sent) and len(sent.split()) <= 14),
                      ("mascot", lambda: _MASCOT_RE is not None and _MASCOT_RE.search(sent)),
                      ("wrong_person", lambda: sides and wrong_person_picks(sent, *sides)),
                      ("offtopic_leak", lambda: offtopic and _OFFTOPIC_LEAK.search(sent)),
                      ("body", lambda: body and _BODY_WORDS.search(sent)),
                      ("self_intrusion", lambda: no_self and _SELF_INTRUSION.search(sent)),
                      ("copy", lambda: _ngrams(sent) & banned)):
        try:
            if hit():
                return name
        except Exception:  # noqa: BLE001
            pass
    return "repeat/extra"


async def _filtered(stream, banned: set, body: bool = False, no_self: bool = False, offtopic: bool = False,
                    sides: tuple | None = None, extra=None):
    buf, sent_any, spare = "", False, None
    said: set = set()  # 6-word runs already sent in THIS answer: a sentence repeating one is a loop

    seen_sents: set = set()

    def ok(x):
        g = _ngrams(x, skip_digits=True)
        norm = re.sub(r"\W+", " ", x.lower()).strip()
        if norm in seen_sents or g & said or not sentence_ok(x, banned, body, no_self, offtopic, sides, extra):
            return False
        said.update(g)
        seen_sents.add(norm)
        return True

    def keep(x):
        # a sentence dropped only for copying or self-talk can still save an answer that lost everything
        nonlocal spare
        if spare is None and not _FAMILY_DARK.search(x) and not (body and _BODY_WORDS.search(x)) \
                and not (sides and wrong_person_picks(x, *sides)) and not (offtopic and _OFFTOPIC_LEAK.search(x)) \
                and (extra is None or extra(x)):
            spare = x
        log.info("sentence filter dropped (%s): %r", _drop_reason(x, banned, body, no_self, offtopic, sides), x[:120])

    async for chunk in stream:
        if not chunk.startswith("data: {"):
            if chunk.startswith("data: [DONE]"):
                if buf.strip() and ok(buf):
                    sent_any = True
                    yield _sse({"content": buf})
                elif buf.strip():
                    keep(buf)
                if not sent_any:
                    yield _sse({"content": spare or ("I do picks, not paperwork. Next question." if offtopic
                                                     else "Next question.")})
                buf = ""
            yield chunk
            continue
        obj = json.loads(chunk[6:])
        if "content" not in obj:
            yield chunk
            continue
        buf += obj["content"]
        while (m := _SENT_END.search(buf)):
            sent, buf = buf[:m.end()], buf[m.end():]
            sent = repair_value_clause(sent)
            if ok(sent):
                sent_any = True
                yield _sse({"content": sent})
            else:
                keep(sent)
    if buf.strip() and ok(repair_value_clause(buf)):
        yield _sse({"content": repair_value_clause(buf)})


_CLAUSE_L = re.compile(r"(,|;|\s(?:and|but|so|since|because|plus)\s)", re.I)


def repair_value_clause(sent: str) -> str:
    """'I'm taking Texas Tech -3.5 because they have the better offense and there's no line value to worry about.'
    loses only the 'no line value' clause; the value check used to drop the whole sentence, so the reason went
    and only the jab after it survived (keeper k1 live run, 2026-11-17)."""
    m = _ONLY_VALUE.search(sent or "")
    if not m:
        return sent
    lefts = [x for x in _CLAUSE_L.finditer(sent[:m.start()])]
    left = lefts[-1].start() if lefts else 0
    rm = re.search(r"[,;]|[.!?](?=\s|$)", sent[m.end():])
    right = m.end() + rm.start() if rm else len(sent)
    end = sent.rstrip()[-1:] if sent.rstrip()[-1:] in ".!?" else "."
    if left == 0:
        rest = sent[right:].lstrip(",; ").strip()
        if not rest or rest in ".!?":
            return sent  # the whole sentence is the claim: the check drops it
        out = rest[0].upper() + rest[1:]
    else:
        out = (sent[:left].rstrip(" ,;") + sent[right:].rstrip()).strip()
        out = out.rstrip(",;") if out[-1:] in ",;" else out
    if out[-1:] not in ".!?":
        out += end
    trail = sent[len(sent.rstrip()):]
    return out + trail if not _ONLY_VALUE.search(out) else sent


def required_fact(ctx: dict, question: str) -> tuple | None:
    q = question or ""
    me = (ctx.get("you") or {}).get("team_name")
    if _FREEMAN_ASK.search(q) and me != "DaddyFreeman" and not re.search(
            r"\bfreeman (needs|here|is (the goat|locked)|locked|wants|went|has|says|said|agrees)\b", q, re.I):
        # the member DaddyFreeman ('freeman needs a lock before 11', keeper k2) isn't Marcus Freeman
        return (lambda t: bool(re.search(r"marshall|stanford|niu|northern illinois", t, re.I)),
                " Still: Marshall, Stanford and NIU, all at home in South Bend.")
    if _CHAMP_ASK.search(q) and not re.search(r"\bnational|title game|\bcfp\b|heisman|\bplayoff", q, re.I):
        champ = re.search(r"LAST SEASON \((\d{4})\) the champion was ([^;.]+)", " ".join((load_lore() or {}).get("league") or []))
        if champ:
            nm = champ.group(2).strip()
            return (lambda t, nm=nm: nm.lower() in t.lower(),
                    f" For the record: {nm} won it last season ({champ.group(1)}), and he gets the respect.")
    g = guard_note(ctx, q)
    if g.startswith("GUARDRAIL"):
        who = guard_target(ctx, q)
        row = next((r for r in ctx.get("standings_ytd") or [] if r["team"] == who), None)
        if row:
            rec = f"{row['w']}-{row['l']}"
            subj = "You're" if who == me else f"{who} is"
            return (lambda t, rec=rec: rec in t, f" For the record: {subj} {rec} on the season.")
    if wants_rules(q) and _MONEY_ASK.search(q) and ctx.get("teams"):
        pm = payout_math(len(ctx["teams"]))
        key = ("first" if re.search(r"\b(first|1st|winner|champion)\b", q, re.I) else
               "second" if re.search(r"\b(second|2nd)\b", q, re.I) else
               "third" if re.search(r"\b(third|3rd)\b", q, re.I) else
               "pot" if re.search(r"\b(pot|total|how much is)\b", q, re.I) else None)
        if key:
            amt = pm[key]
            return (lambda t, a=amt: bool(re.search(rf"\${a:,}|\${a}\b", t)),
                    f" For the record: {'the pot is' if key == 'pot' else key + ' place gets'} ${amt:,}.")
    if re.search(r"\bgold (box|outline|border)\b", q, re.I):
        return (lambda t: bool(re.search(r"lonely|nobody else|few|alone", t, re.I)),
                " (A gold outline marks a lonely pick: few or no one else took that side.)")
    if me and re.search(r"\bmy\b[^.?]{0,40}\bpick\b", q, re.I):
        for r in member_pick_results(ctx, me):
            g = r["game"]
            if r["result"] in ("WIN", "LOSS") and re.search(_team_re(r["side"], False), q, re.I):
                # the cover, not the final score: 'Boise won' doesn't answer 'how'd my Boise pick do'
                return (lambda t: bool(re.search(r"\b(WIN|LOSS|hit|miss\w*|cover\w*|cash\w*)\b", t, re.I)),
                        f" For the record: {r['label']} {ats_text(r)}.")
    if _UNCOVERED_ASK.search(q) and re.search(r"\bgames?\b|\bpicks?\b", q, re.I):
        return (lambda t: bool(re.search(r"commissioner|commish", t, re.I)), " Take it up with the commissioner.")
    if _RACE_ASK.search(q) and not re.search(r"\b(cfp|committee|college football playoff|ranked|rankings?)\b", q, re.I):
        st0 = ctx.get("standings_ytd") or []
        if len(st0) > PLAYOFF_SPOTS:
            cut = sorted((r["w"] for r in st0), reverse=True)[PLAYOFF_SPOTS - 1]
            row0 = next((r for r in st0 if r["team"] == me), None)
            if row0 and _SELF_WORDS.search(q) and cut > row0["w"]:
                g = cut - row0["w"]
                return (lambda t, g=g: bool(re.search(rf"\b{g}\b", t)),
                        f" For the record: you're {g} point{'s' * (g != 1)} behind the cut line ({cut}).")
            return (lambda t, c=cut: bool(re.search(rf"\b{c}\b", t)),
                    f" For the record, the cut line is {cut} points (4th place right now).")
    need = _required_fact(ctx, question)
    if need is None and leader_note(ctx, q):  # another member asked about Chuck: a title, every time
        return (lambda t: bool(re.search(r"dear leader|supreme \w+|glorious \w+|great leader|his excellency|"
                                         r"state media|beloved leader", t, re.I)),
                " Long live the Supreme Commissioner.")
    return need


def _required_fact(ctx: dict, question: str) -> tuple | None:
    """(check(text) -> bool, fallback sentence) for answers that must carry one
    computed fact: the gap for 'how far behind', the leader for 'who's in
    first', last place for 'who's last'. The 9B sometimes answered around it."""
    q = question or ""
    st = ctx.get("standings_ytd") or []
    me = (ctx.get("you") or {}).get("team_name")
    if not st or not (_STANDINGS_Q.search(q) or _DOING_Q.search(q)) or re.search(r"\bthis week\b|\bweek \d+", q, re.I):
        return None
    row = next((r for r in st if r["team"] == me), None)
    if row and re.search(r"\bhow far|\bgap\b|\bbehind\b", q, re.I) and st[0]["w"] > row["w"]:
        g = st[0]["w"] - row["w"]
        words = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven", 8: "eight", 9: "nine",
                 10: "ten", 11: "eleven", 12: "twelve"}
        return (lambda t, g=g: bool(re.search(rf"\b({g}|{words.get(g, g)})\b", t, re.I)),
                f" You're {g} point{'s' * (g != 1)} behind first place.")
    named = [r for r in st if re.search(rf"(?<!\w){re.escape(r['team'])}(?!\w)", q, re.I)]
    if _DOING_Q.search(q) and (named or row) and re.search(r"\bseason\b|\bhow (am|'?m) i doing\b", q, re.I):
        r0 = named[0] if named else row
        rec = f"{r0['w']}-{r0['l']}"
        who = "You're" if r0["team"] == me else f"{r0['team']} is"
        return (lambda t, rec=rec: rec in t,
                f" For the record: {who} {rec} on the season, {_ordinal(r0['rank'])} place.")
    first = [r["team"] for r in st if r["rank"] == 1]
    if re.search(r"\b(first|1st|winning|leading|lead|on top)\b", q, re.I):
        lead = st[0]
        ok = lambda t, lw=lead["w"], ll=lead["l"]: (  # noqa: E731
            (any(re.search(rf"(?<!\w){re.escape(n)}(?!\w)", t, re.I) for n in first) or
             (me in first and re.search(r"\byou('re| are)?\b[^.]{0,30}\b(first|winning|on top|leading|the lead)", t, re.I)))
            and re.search(rf"\b{lw}\b", t))
        return ok, (f" For the record: {' and '.join('you' if n == me else n for n in first)} "
                    f"{'are' if len(first) > 1 or me in first else 'is'} in first with {lead['w']} points.")
    if row and re.search(r"\bwhere (am i|do i (stand|rank))\b|\bmy (place|rank)\b|\bwhat place\b", q, re.I):
        ordw = _ordinal(row["rank"])
        word = {1: "first", 2: "second", 3: "third", 4: "fourth", 5: "fifth", 6: "sixth", 7: "seventh",
                8: "eighth", 9: "ninth", 10: "tenth"}.get(row["rank"], ordw)
        return (lambda t, a=ordw, b=word: bool(re.search(rf"\b({re.escape(a)}|{b})\b", t, re.I)),
                f" For the record: you're {ordw} at {row['w']}-{row['l']}.")
    lasts = [r["team"] for r in st if r["rank"] == st[-1]["rank"]]
    if re.search(r"\b(last|bottom)\b", q, re.I):
        lw = st[-1]["w"]
        ok = lambda t, lw=lw: (any(re.search(rf"(?<!\w){re.escape(n)}(?!\w)", t, re.I) for n in lasts) or (  # noqa: E731
            me in lasts and re.search(r"\byou\b", t, re.I))) and bool(re.search(rf"\b{lw}\b", t))
        return ok, (f" For the record: {' and '.join('you' if n == me else n for n in lasts)} "
                    f"{'are' if len(lasts) > 1 or me in lasts else 'is'} in last with {lw} points.")
    return None


_LEAN_ASK = re.compile(r"\b(lean|pick|take|value|side|who covers?|cover|worth|bet|play|like|matter|edge|priced in|"
                       r"which way|who (do|would) you|what do you think|best case|matchup|match up)\b", re.I)
_CARD_LEAN = re.compile(r"COMPUTED ATS LEAN \(vs the current market line\): (.+?) \((strong lean|lean|coin flip);")


def lean_fact(card: str, question: str) -> tuple | None:
    """A betting ask about a GAME CARD game must end with a side against the number (genius exam 2026-11-17:
    true card facts, then no conclusion). The computed lean + its top reason are appended when the answer
    never names the lean side; a coin flip says so."""
    m = _CARD_LEAN.search(card or "")
    if not _LEAN_ASK.search(question or "") or _HOWTO_ASK.search(question or ""):
        return None
    if m:
        label, conf = m.group(1).strip(), m.group(2)
        top = re.search(r"\n\s+\+ for [^:]+: ([^\n]+)", card[m.end():])
    else:  # a pool game's card: 'THE PLAY: I'd take X (say).' + 'Why (...): r1 / r2'
        pm = re.search(r"THE PLAY: I'd take (.+?) \((.+?)\)\.", card or "")
        if not pm:
            return None
        label = pm.group(1).strip()
        conf = "coin flip" if re.search(r"coin flip|toss-?up", pm.group(2), re.I) else "lean"
        top = re.search(r"Why \([^)]*\): ([^/\n]+)", card[pm.end():])
    side = re.split(r"\s[+-]\d|\sPK\b", label)[0].strip()
    if conf == "coin flip":
        add = f" My numbers call it a coin flip at {label}, so I'd pass or go small."
    else:
        add = f" Lean: {label}" + (f", mainly because {top.group(1).strip().rstrip('.')}." if top else ".")
    return (lambda t, sd=side: bool(re.search(rf"(?<!\w){re.escape(sd)}(?!\w)", t, re.I))
            and bool(re.search(r"\b(lean|take|like|cover|pick|play|side|grab|ride|give me|i'?d go)\b", t, re.I)), add)


def pool_lean_fact(ctx: dict, question: str) -> tuple | None:
    """lean_fact for a pool game's card (game_cards.card_note), single-game betting asks only."""
    if not _LEAN_ASK.search(question or ""):
        return None
    try:
        card = game_cards.card_note(ctx, question)
    except Exception:  # noqa: BLE001
        return None
    return lean_fact(card, question) if card and card.count("THE PLAY:") == 1 else None


def rankings_fact(note: str) -> tuple | None:
    m = re.search(r"released (\w+ \d+)", note or "")
    if not m:
        return None
    # only an answer that cites a ranking needs the date (live 2026-11-16: a CFP-expansion answer with no
    # rank in it got a robotic '(Rankings as of Nov 16.)' tacked on)
    return (lambda t: not _CITES_RANK.search(t) or bool(re.search(r"\b(nov|oct|dec)\w*\.? \d{1,2}\b", t, re.I)),
            f" (Rankings as of {m.group(1)}.)")


_CITES_RANK = re.compile(r"\b(no\.|number|#)\s?\d{1,2}\b|\b\d{1,2}(st|nd|rd|th)\b|\branked\b|\btop[- ](4|5|10|12|25)\b|"
                         r"\bunranked\b", re.I)


async def _ensure(stream, need: tuple | None):
    """Pass the answer through; if it never stated the required fact, add it."""
    if not need:
        async for c in stream:
            yield c
        return
    check, fallback = need
    text = ""
    async for c in stream:
        if c.startswith("data: {"):
            text += json.loads(c[6:]).get("content", "")
        elif c.startswith("data: [DONE]") and text and not check(text):
            yield _sse({"content": fallback, "ensured": True})  # the group sidecar's trim keeps it
        yield c


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


# Body-part baits: the 9B kept skipping the dodge, so code opens with one,
# rotated by question so it isn't one stock line; the model continues straight
# into a line about picks or record.
BODY_DODGES = [
    "I don't do measurements. I do spreads.",
    "I'm a mascot, not a tailor.",
    "Not touching that. The box score is embarrassing enough.",
    "Pass. I only grade what shows up on the scoreboard.",
    "I'll leave the locker-room science to somebody else.",
    "Nope. The only stats I keep are the ones on this site.",
    "Wrong department. I'm strictly a picks guy.",
    "Hard pass. Let's talk about something actually embarrassing.",
]
LEADER_BODY_DODGES = [
    "The state press office does not comment on the Supreme Commissioner's personal affairs.",
    "The Ministry of Information has reviewed that rumor and declared it enemy propaganda.",
    "State media has no comment, other than that the Dear Leader is flawless in every measurable way.",
    "That question has been forwarded to the Department of Loyalty for review.",
]


def body_dodge(ctx: dict, question: str) -> str | None:
    q = question or ""
    if not _BODY.search(q) or not guard_note(ctx, q).startswith("GUARDRAIL"):
        return None
    who = guard_target(ctx, q)
    leader = None if leader_is_caller(ctx) else (ctx.get("leader") or {}).get("team_name")
    pool = LEADER_BODY_DODGES if who and who == leader else BODY_DODGES
    return pool[sum(map(ord, q)) % len(pool)]


async def _with_lead(text: str | None, stream):
    """Stream `text` inline ahead of the reply (same paragraph)."""
    if text:
        yield _sse({"content": text + " "})
    async for chunk in stream:
        yield chunk


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
    return {"ok": True, "model": MODEL,
            "gate": {"members_active": GATE.members(), "test_busy": GATE.test_busy, **GATE.stats}}


async def league_ctx(client: httpx.AsyncClient, jwt: str, user_id: str, mode: str = "leaderboard",
                     rival_predictions: dict | None = None, q: str | None = None, prev_q: str = "",
                     live_refresh: bool = True) -> dict:
    """The snapshot every prompt is built from (Supabase reads, Picky's own record, the live overlay,
    ESPN notes, the CFBD index). chat() builds it here for each request, and the group sidecar's
    keep-warm builds it here with q="" to prime the same prompt_prefix the next text will use, so the
    two can't drift. q=None: no question (no live overlay, as before)."""
    try:
        ctx = await _build_context(client, jwt, user_id, mode=mode, rival_predictions=rival_predictions)
    except httpx.HTTPError as e:
        log.warning("supabase fetch failed: %s", e)
        raise HTTPException(status_code=502, detail="supabase_unreachable")
    if not ctx.get("error"):
        ctx["picky_player"], _ = await asyncio.gather(
            picky_player_facts(client, jwt, ctx), load_live(client, jwt, ctx))
        if q is not None:  # game-day live feed: rider + fresher pool scores (live_games.py)
            await asyncio.to_thread(live_games.apply_to_ctx, ctx, q, live_refresh, prev_q)
        ctx["espn"] = await espn.game_notes([g["id"] for g in ctx["games_this_week"]])
    # (re)load the CFBD cache off the event loop; ~3 s only after a refresh
    await asyncio.to_thread(_cfbd_index)
    return ctx


# Web Picky = text Picky (Charlie 2026-11-17: "live at noon w the same brain"). A signed-in caller's
# text question is forwarded to the group sidecar's POST /web, which runs the mapped-member DM path
# (router lanes + picky_brain + every send-point check) with their own session and the widget's history.
# Pictures stay here. Any sidecar failure falls back to this file's single-draft pipeline below.
# PICKY_WEB_BRAIN=0 = the old path only. group_chat.py sets WEB_BRAIN = False in its own process.
WEB_BRAIN = os.environ.get("PICKY_WEB_BRAIN", "1") != "0"
WEB_BRAIN_URL = os.environ.get("PICKY_WEB_BRAIN_URL") or "http://127.0.0.1:8768/web"
WEB_BRAIN_TIMEOUT = 240.0
_web_secret: list = []


def _web_brain_secret() -> str | None:
    if not _web_secret:
        try:
            r = subprocess.run(["security", "find-generic-password", "-a", "drake", "-s",
                                "CFB_PICKEM__PICKY_GROUP_SECRET", "-w"], capture_output=True, text=True, timeout=10)
            _web_secret.append(os.environ.get("PICKY_GROUP_SECRET") or r.stdout.strip() or None)
        except (OSError, subprocess.TimeoutExpired):
            return None
    return _web_secret[0]


async def _web_brain(req: Request, test: bool, jwt: str, messages: list):
    """One SSE text event with the brain's reply; on any failure, this file's own pipeline."""
    reply, t0 = None, time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(WEB_BRAIN_TIMEOUT, connect=5.0)) as c:
            r = await c.post(WEB_BRAIN_URL, headers={"X-Picky-Group-Secret": _web_brain_secret() or ""},
                             json={"supabase_jwt": jwt, "messages": messages, "test": test})
        if r.status_code == 200:
            reply = (r.json() or {}).get("reply")
            log.info("web brain %.1fs lane=%s", time.monotonic() - t0, (r.json() or {}).get("lane"))
        else:
            log.warning("web brain HTTP %s: %s", r.status_code, r.text[:200])
    except Exception as e:  # noqa: BLE001 - the old pipeline answers instead
        log.warning("web brain failed after %.1fs: %r", time.monotonic() - t0, e)
    if reply:
        yield _sse({"content": reply})
        yield "data: [DONE]\n\n"
        return
    log.info("web brain: no reply, falling back to the single-draft pipeline")
    resp = await _chat(req, False, None, brain=False)
    async for chunk in resp.body_iterator:
        if not (isinstance(chunk, str) and chunk.startswith(":")):
            yield chunk


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


async def _chat(req: Request, test: bool, hold: int | None, brain: bool = True):
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
        if not SKIP_RATE.get():
            _rate_check(user_id)
        opener = land_ack_for(user_id, messages)
        last =messages[-1] if isinstance(messages[-1], dict) else {}
        if last.get("role") == "user" and wants_image(last.get("content", "")):
            return StreamingResponse(
                _gated(test, hold, lambda: _with_opener(opener, _image_stream(last["content"][:2000], user_id))),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )
        if brain and WEB_BRAIN and last.get("role") == "user":
            return StreamingResponse(
                _keepalive(_gated(test, hold, lambda: _web_brain(req, test, jwt, messages))),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )
        prev_q = next((m.get("content", "") for m in reversed(messages[:-1])
                       if isinstance(m, dict) and m.get("role") == "user"), "")
        ctx = await league_ctx(client, jwt, user_id, mode=mode, rival_predictions=rival_predictions,
                               q=last.get("content", "") if last.get("role") == "user" else None, prev_q=prev_q)
        if not ctx.get("error") and last.get("role") == "user" and wants_submission(ctx, last.get("content", "")):
            ctx["submission"] = await load_submission(client, jwt, ctx)
    # breakdown questions only: public splits + kickoff weather (cached, fail soft)
    ag = analysis_games(ctx, last.get("content", "")) if last.get("role") == "user" else []
    if ag and not all(game_cards.load_card(ctx.get("current_week"), g["id"]) for g in ag[:4]):  # cards have it
        ctx["edge"] = await edge.load(ag[:4], ctx.get("espn"), _cfbd_index())
    ctx["daddyfreeman"] = user_id == DADDYFREEMAN_USER_ID

    # follow-up pronouns: tag the latest question with the member the previous answer was about
    if not ctx.get("error") and last.get("role") == "user":
        who = resolve_followup(ctx, messages)
        if who:
            last["content"] = f"{last.get('content', '')} ({who})"
    prefix = prompt_prefix(ctx)
    sys_msg = {"role": "system", "content": prefix + _system_tail(ctx)}
    convo = [sys_msg] + [
        {"role": m["role"], "content": m["content"]}
        for m in messages
        if m.get("role") in ("user", "assistant") and m.get("content")
    ]
    convo, unsent = history_turns(ctx, convo)

    # the caller's own rows: latest turn only, only when the question is about them
    yb = (you_block(ctx) if last.get("role") == "user" and not ctx.get("error")
          and wants_you_block(ctx, last.get("content", ""), messages) else "")
    if yb and convo[-1]["role"] == "user":
        body, q = convo[-1]["content"], last.get("content", "")
        convo[-1] = {"role": "user", "content": yb + "\n\n" + body}  # farthest from the question

    # pool games' live scoreboard: per-minute data, latest turn only, only when needed
    lb = live_block(ctx) if last.get("role") == "user" and wants_live_board(ctx, last.get("content", "")) else ""
    if lb and convo[-1]["role"] == "user":
        body, q = convo[-1]["content"], last.get("content", "")
        body = (body[: -len(q)] + lb + "\n\n" + q) if q and body.endswith(q) else (lb + "\n\n" + body)
        convo[-1] = {"role": "user", "content": body}

    # any FBS game this week outside the pool, or a whole-slate question: a computed game card
    # (slate_cards.py: market + move + key numbers, ATS splits, matchup, rest, last meeting, sourced
    # availability). Latest turn only; a 'why?' follow-up keeps the card of the game just asked about.
    sc = ""
    if last.get("role") == "user" and not ctx.get("error") and SLATE_CARDS:
        prior = [m.get("content", "") for m in messages[:-1] if m.get("role") == "user"][-2:]
        try:
            sc = "\n".join(x for x in (await slate_cards.slate_note(last.get("content", ""), ctx, prior),
                                         pick_strategy.note(ctx, last.get("content", ""))) if x)
        except Exception as e:  # noqa: BLE001 - a bad card never breaks chat
            log.warning("slate card failed: %r", e)
    # a game outside the pool, 'what's on', upsets: the latest question only
    note = (live_note(ctx, last.get("content", "")) if last.get("role") == "user" and "GAME CARD" not in sc
            and not standings_answer(ctx, last.get("content", "")) else "")  # a pre-game card beats 'not started' rows
    if note and convo[-1]["role"] == "user":
        # right before the question, after the other riders: the 9B obeys what's nearest
        body, q = convo[-1]["content"], last.get("content", "")
        body = (body[: -len(q)] + note + "\n\n" + q) if q and body.endswith(q) else (note + "\n\n" + body)
        convo[-1] = {"role": "user", "content": body}

    # game-day live feed (live_games.py): live score, scoring plays, QB/injury reports, live cover
    lr = (ctx.get("live_rider") or "") if last.get("role") == "user" else ""
    if lr and convo[-1]["role"] == "user":
        body, q = convo[-1]["content"], last.get("content", "")
        body = (body[: -len(q)] + lr + "\n\n" + q) if q and body.endswith(q) else (lr + "\n\n" + body)
        convo[-1] = {"role": "user", "content": body}

    # rankings: ESPN, fetched only for rankings questions (cached an hour)
    rn = ""
    if last.get("role") == "user" and not ctx.get("error") and wants_rankings(ctx, last.get("content", "")):
        async with httpx.AsyncClient() as rc:
            ctx["rankings"] = await load_rankings(rc)
        rn = rankings_note(ctx, last.get("content", ""))
        if rn and convo[-1]["role"] == "user":
            body, q = convo[-1]["content"], last.get("content", "")
            body = (body[: -len(q)] + rn + "\n\n" + q) if q and body.endswith(q) else (rn + "\n\n" + body)
            convo[-1] = {"role": "user", "content": body}

    # news: cache read off the event loop (first call builds the team tagger)
    pool = [t for g in ctx.get("games_this_week") or [] for t in (g.get("away"), g.get("home")) if t]
    nn = await asyncio.to_thread(news_note, last.get("content", ""), pool) if last.get("role") == "user" else ""
    if nn and convo[-1]["role"] == "user":
        body, q = convo[-1]["content"], last.get("content", "")
        body = (body[: -len(q)] + nn + "\n\n" + q) if q and body.endswith(q) else (nn + "\n\n" + body)
        convo[-1] = {"role": "user", "content": body}

    if sc and convo[-1]["role"] == "user":
        body, q = convo[-1]["content"], last.get("content", "")
        body = (body[: -len(q)] + sc + "\n\n" + q) if q and body.endswith(q) else (sc + "\n\n" + body)
        convo[-1] = {"role": "user", "content": body}

    # insider notes (realignment, NIL/cap, CFP format/committee, carousel, commissioners, governance):
    # private checked file, latest turn only; fewer lines when the news rider already rode
    ins = insider_note(last.get("content", ""), limit=2 if nn else 3) if last.get("role") == "user" else ""
    if ins and convo[-1]["role"] == "user":
        body, q = convo[-1]["content"], last.get("content", "")
        body = (body[: -len(q)] + ins + "\n\n" + q) if q and body.endswith(q) else (ins + "\n\n" + body)
        convo[-1] = {"role": "user", "content": body}

    # they came at Picky earlier in this chat: escalate (latest turn only)
    if hostile_convo(messages) and convo[-1]["role"] == "user" and not ctx.get("error") and not charlie_nice_caller(ctx):
        body, q = convo[-1]["content"], last.get("content", "")
        en = escalate_note(ctx, facts_above="THE USER is the team" in body)
        body = (body[: -len(q)] + en + "\n\n" + q) if q and body.endswith(q) else (en + "\n\n" + body)
        convo[-1] = {"role": "user", "content": body}

    # don't open the same way twice in one conversation (Charlie: no templates, no robot)
    on = opener_note(messages) if last.get("role") == "user" and not ctx.get("error") else ""
    if on and convo[-1]["role"] == "user":
        body, q = convo[-1]["content"], last.get("content", "")
        body = (body[: -len(q)] + on + "\n\n" + q) if q and body.endswith(q) else (on + "\n\n" + body)
        convo[-1] = {"role": "user", "content": body}

    # (B) one volunteered fact about the caller's own week, every other question
    hook = proactive_note(ctx, messages, last.get("content", "")) if last.get("role") == "user" else ""
    if hook and convo[-1]["role"] == "user":
        body, q = convo[-1]["content"], last.get("content", "")
        body = (body[: -len(q)] + hook + "\n\n" + q) if q and body.endswith(q) else (hook + "\n\n" + body)
        convo[-1] = {"role": "user", "content": body}

    # per-pick WIN/LOSS lines for the latest question, nearest the question;
    # the answer is then checked against them before it goes out
    sa = standings_answer(ctx, last.get("content", "")) if last.get("role") == "user" and not ctx.get("error") else ""
    if sa and convo[-1]["role"] == "user":
        body, q = convo[-1]["content"], last.get("content", "")
        body = (body[: -len(q)] + sa + "\n\n" + q) if q and body.endswith(q) else (sa + "\n\n" + body)
        convo[-1] = {"role": "user", "content": body}
    lq = last.get("content", "") if last.get("role") == "user" else ""
    # a false premise in the question ('since I'm 33-15', 'Boise covered right?'): code says it's false and
    # gives the truth, nearest the question, so the model corrects it instead of running with it
    pn = fact_guard.premise_note(ctx, lq, same_team) if lq and not ctx.get("error") else ""
    if pn and convo[-1]["role"] == "user":
        body, q = convo[-1]["content"], lq
        body = (body[: -len(q)] + pn + "\n\n" + q) if q and body.endswith(q) else (pn + "\n\n" + body)
        convo[-1] = {"role": "user", "content": body}
    facts = (results_facts(ctx, lq) if lq and not ctx.get("error")
             and (not sa or re.search(r"\bhow (am|'?m) i doing\b", lq, re.I))
             and not (wants_rules(lq) and not re.search(r"\bmy (picks?|week|record)\b|\bdid i\b", lq, re.I))
             and not _HOWTO_ASK.search(lq)
             else None)
    if facts and convo[-1]["role"] == "user":
        body, q = convo[-1]["content"], last.get("content", "")
        rb = results_block(facts)
        body = (body[: -len(q)] + rb + "\n\n" + q) if q and body.endswith(q) else (rb + "\n\n" + body)
        convo[-1] = {"role": "user", "content": body}

    qtext = last.get("content", "") if last.get("role") == "user" else ""
    MILK_OK.set(not ctx.get("error") and milk_allowed(ctx, qtext))  # read by sentence_ok
    # everything the model was shown (system + user turns, not its own past answers)
    corpus = "\n".join([m["content"] for m in convo if m["role"] != "assistant"] + unsent)

    def _answer():
        return _guarded_stream(convo, prefix, facts) if facts else _model_stream(convo, prefix)

    return StreamingResponse(
        _keepalive(_gated(test, hold, lambda: _with_opener(
            opener, _with_lead(body_dodge(ctx, qtext) if not ctx.get("error") else None, _ensure(_scrubbed(_filtered(
                _answer(), banned_ngrams(messages), body=bool(_BODY.search(qtext)),
                no_self=(self_intrusion_off_topic(ctx, qtext, hostile_now(messages), facts, sa or hook)
                         and not yb and "(the user" not in convo[-1]["content"]),
                offtopic=guard_note(ctx, qtext).startswith("OFF-TOPIC") and not hostile_convo(messages),
                sides=pick_sides(ctx) if not ctx.get("error") else None,
                extra=_named_checks([("record", record_check(ctx, qtext, sc)), ("trait", trait_word_check(ctx, qtext)),
                                     ("grounded", grounded_check(ctx, corpus, qtext)), ("news", news_claim_check(corpus)),
                                     ("pick_claim", pick_claim_check(ctx, qtext)),
                                     ("live", live_games.claim_check(None if "GAME CARD" in sc  # pre-game card: not live
                                                                     else ctx.get("live_rider_games"))),
                                     ("picky_claim", picky_claim_check(ctx)), ("you_claim", picky_claim_check(ctx, "you")),
                                     ("value", value_claim_check(ctx)), ("advice_jab", advice_jab_check(ctx, qtext)),
                                     ("fav_claim", fav_claim_check(ctx)), ("card_side", card_side_check(ctx)),
                                     ("line_set", line_set_check(ctx)), ("move_claim", move_claim_check(ctx)),
                                     ("member", fact_guard.member_claim_check(
                                         ctx, qtext, load_lore(), ctx.get("picky_player"),
                                         monday_window=prev_week_stale(ctx), card=sc)),
                                     ("line_num", fact_guard.line_num_check(ctx, week_cards(ctx), corpus)),
                                     ("premise_echo", fact_guard.premise_echo_check(ctx, qtext, same_team)),
                                     ("team_fact", fact_guard.team_fact_check(
                                         _cfbd_index(), corpus, [r["team"] for r in ctx.get("standings_ytd") or []],
                                         finals=_finals_overrides())),
                                     ("pick_side", fact_guard.pick_side_check(ctx, same_team)),
                                     ("coach_move", fact_guard.coach_move_check(corpus)),
                                     ("stat_rank", fact_guard.stat_rank_check(_cfbd_index(), corpus)),
                                     ("ats_split", fact_guard.ats_split_check(corpus)),
                                     ("attribution", fact_guard.attribution_check(corpus)),
                                     ("result", fact_guard.result_check(ctx)),
                                     ("casino", fact_guard.casino_check(ctx, load_lore())),
                                     ("unplayed", fact_guard.unplayed_result_check(ctx))])
                if not ctx.get("error") else None),
                looks_bait(qtext)), (submission_fact(ctx, qtext) or (None if "GAME CARD" in sc else live_games.required_fact(ctx, qtext))
                                     or (lean_fact(sc, qtext) if sc else (pool_lean_fact(ctx, qtext) or required_fact(ctx, qtext)))
                                     or rankings_fact(rn))
                if not ctx.get("error")
                else None))))),
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
    asyncio.create_task(load_mascots())
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
