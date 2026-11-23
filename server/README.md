# Pick'em League Assistant — `chat.py`

FastAPI service that powers **Picky**, the league assistant chat on the
leaderboard page. Runs on the Mac mini under LaunchAgent `com.pickem.chat`,
listens on `127.0.0.1:8766`, reached from github.io through the Cloudflare
tunnel at `pickem-chat.dunecreek.ai`.

## What it does

- Validates the caller's Supabase JWT via `/auth/v1/user` (no secret stored).
- Pulls a compact snapshot of league state from Supabase **using the
  caller's JWT**, so row-level security applies to everything.
- Builds a system prompt in Picky's voice (a savage, trash-talking heel
  who still answers with real facts) and streams the local Ollama model
  (`qwen3.5:9b-q4_K_M`) back as SSE. No API calls, no API cost. Live replies never
  call Anthropic (the old hosted path is dormant, see "Anthropic (offline only)").
- Rate-limits 10 req/min per user, in-memory.
- Warms Ollama on startup; the model unloads after `PICKY_OLLAMA_KEEP_ALIVE`
  (30m) idle. A cold message takes ~35-45 s, a warm one ~5-10 s.
- The system prompt is byte-identical between messages (the clock is only
  to the hour, and the per-question focus block rides in the user turn), so
  Ollama reuses the processed prompt instead of re-reading ~6.5k tokens.
- Unloads the chat model before an image job: both together swap on 16 GB.

Errors: `401` bad/missing token · `429` rate-limited · `502` Supabase
unreachable · `500` otherwise. Streamed error chunks look like
`data: {"error": "..."}`.

## Config

Env vars (set in the plist):

| Var | Default | Notes |
| --- | --- | --- |
| `PICKEM_CHAT_MODEL` | `qwen2.5:7b-instruct` | The plist sets `qwen3.5:9b-q4_K_M` (GGUF on llama.cpp; the default `qwen3.5:9b` tag runs on MLX, whose prompt cache grew to ~16 GB). |
| `PICKY_OLLAMA_THINK` | (unset) | `false` for qwen3.5: thinking took minutes per answer. Leave unset for models without thinking. |
| `PICKY_OLLAMA_KEEP_ALIVE` | `30m` | How long the model stays loaded after the last message. |
| `PICKY_CLAUDE_MODEL` | `claude-opus-5-5` | The one model for every `claude -p` call (`picky_claude.py`). |
| `PICKY_NO_ANTHROPIC` | (unset) | `1` = Anthropic kill switch: every `claude -p` call fails fast, fallbacks run. |
| `PORT` | `8766` | Next free after metra's 8765. |
| `OLLAMA_URL` | `http://127.0.0.1:11434` | Local Ollama only. |

## Run locally

```
./.venv/bin/python3 chat.py
curl http://127.0.0.1:8766/health
```

## LaunchAgent

```
launchctl unload ~/Library/LaunchAgents/com.pickem.chat.plist
launchctl load   ~/Library/LaunchAgents/com.pickem.chat.plist
```

Logs: `server/logs/chat.{stdout,stderr}.log`.

## Images (`image.py`)

When the latest message asks for a picture ("draw me...", "make a picture
of..."), `/chat` skips the league context and hands only the user's own
words to `image.py` on `127.0.0.1:8767` (Z-Image Turbo, 4-bit, via mflux).
The reply streams a one-liner, keep-alive pings, then
`data: {"image": "data:image/png;base64,..."}`, which `js/picky.js` renders
in the bubble.

- LaunchAgent `com.pickem.image`, venv `.venv-flux`, logs `server/logs/image.*.log`.
- One GPU lock + one MLX thread, so generations run one at a time.
- 20 images per user per hour.
- Weights: pre-quantized copy at `~/models/z-image-turbo-4bit`, made once with
  `mflux-save --model z-image-turbo --quantize 4 --path ~/models/z-image-turbo-4bit`.
- Memory: the model loads on first request and unloads after 5 idle minutes,
  so it only shares RAM with Ollama's Qwen while someone is making pictures.

## Grounding (what Picky sees)

`render_context()` turns the league snapshot into labeled text: ranked
standings with the last three weeks, `YOUR GAMES THIS WEEK (N)` with one
block per game (pool line as the two pickable sides, ESPN market line and
the gap between them, a computed value check on the caller's pick, AP rank,
record, last five, season per-game stats, stat leaders, ESPN FPI win
projection, weather, team-tagged headlines; injuries are reported as
unavailable because ESPN publishes no college injury feed), last week's finals with every team's hits and misses, and the
jackpot. `focus_block()` repeats the facts for any league member named in
the question (or the caller, for "roast me" / "how did I do") at the end
of the prompt. ESPN data comes from `espn.py` (public summary endpoint,
10 minute cache, fails soft to "unavailable").

The prompt is ~6.5k tokens on qwen. Ollama runs with `num_ctx` 16384,
temperature 0.2 and `presence_penalty` 0 (qwen3.5's default 1.5 garbles
names and numbers it has already used).

Matchup breakdowns (`analysis_note` / `matchup_signals`, only for "break
down / who do you like" questions): a weighted lean from market vs pool line,
SP+, CFBD PPA efficiency, unit-vs-unit rank edges (success rate,
explosiveness, havoc), ESPN FPI, line movement (ESPN open vs now, CFBD
fallback), DraftKings Network's public betting splits (keyless page) and
kickoff weather (ESPN, else Open-Meteo at the CFBD venue's lat/lon; domes
skipped). All in `edge.py`, weights documented at its top, tests in
`test_edge.py`. Splits and Open-Meteo are fetched per breakdown question
(20 / 60 minute caches, fail soft) and never touch the shared system prompt.

Picky's own CFP bracket (`bcs.py`, on demand only: "projected CFP bracket", "who
makes the CFP", "first four out"): BCS-style rankings (1/3 AP points share, 1/3
Coaches, 1/3 the average of SP+ / FPI / Elo / SRS top-25 ranks, all cached) fed
through `cfp_rules.json`. Router lane `cfp_bracket` sends the code text verbatim
plus one voice line; "if it ended today" asks keep `cfp_now` (the committee's
ranking). Cached in `cache/bcs/<season>-w<week>.json`, refreshed by the factstore
tick. `bcs.py show --rank` prints it. Tests: `test_bcs.py`.

## Anthropic (optional, capped booster)

Claude helps the scheduled advanced analysis when it's there; Picky never needs it, and live replies never
call it. Every `claude -p` call goes through `picky_claude.run()` (shell scripts: `picky_claude.py run`):
model `claude-opus-5-5`, `--strict-mcp-config` + `empty.mcp.json`, `ANTHROPIC_API_KEY` stripped, and one
line per call in the private daily usage log (`cfb-pickem-private/claude_usage/<date>.jsonl`;
`picky_claude.py usage` sums calls, minutes and booster use per day). The gate refuses live reply processes,
`PICKY_NO_ANTHROPIC=1`, a missing binary, a login that isn't the claude.ai subscription, and boosters past the
daily cap (12 calls / 45 min). Every booster falls back to code / local gemma on its own; see
`docs/picky-zero-maintenance.md` and `test_zero_maintenance.py`.

| Path | Booster (gate open) | Gate shut / call failed |
|---|---|---|
| live replies (DM + group), in-reply judge | never | local model + code lanes |
| lock reminders, final-score pings, recap / preview / Saturday post | no Claude | local gemma writer / plain fact template |
| Picky's weekly picks | no Claude | code (market / SP+ / FPI) |
| news digest (`news.py`) | no Claude | feeds + local model |
| dossiers, slate intel | Opus web research (weekly / Fri + Sat) | code over news-cache leads (+ ESPN rankings) |
| analyst takes, game-card cases | Opus (weekly) | previous take/case while fresh, else code factors |
| burn book, banter bank, hot takes, insider notes | Opus (Sun/Wed, insider ≤ daily) | last good copy + code pool / wire |
| league material, member memory, week breakdown | no Claude | local gemma, validated |
| league rules re-verify (`league_rules.py --check`) | Opus review | code verification, unconfirmable change flagged "verify" |
| exam judges (not capped), flub loop, feed repairs (hand only) | Opus | skipped |

## Game dossiers (`dossier.py`)

Per-game injury / availability / insider notes for the open pool week,
fully automatic. LaunchAgent `com.pickem.picky-dossier` fires every 30
minutes; `dossier.py` builds a game's dossier when it first appears in the
slate, again after Friday 5 PM and Saturday 7 AM CT, never after kickoff.
Leads come from the news cache and r/CFB's public RSS; `claude -p` (Opus via
`picky_claude.py`, subscription, web tools only, no MCP) researches (Anthropic
down: Picky's local model turns the news-cache leads into claims, each kept
only if its names and numbers are in its lead); a validator enforces the
schema (text, kind fact|injury|insider_opinion|rumor, source_url, date,
confidence), drops claims with no URL / dead URLs / paid sites, keeps forum
posts as rumors, and the previous good file stays on any failure. Picky's
local model writes a two-sentence summary that's kept only if every name
and number in it is in the claims. Files: `cache/dossiers/<week>/<game_id>.json`,
log `logs/dossier.log`, `dossier.py status` for a quick look. URLs stay in
the file for grounding; members never see them.

Intel in chat: `dossier_note()` adds a GAME INTEL block to the user turn
only when the question names a specific pool game (not for "break down the
games"), with claims labeled injury report / news / opinion / RUMOR, dated,
outlet named, no URLs. `matchup_signals` never reads it, so it can't move the
lean (`test_intel.py` pins that). The sentence filter's grounding check
(`grounded_check`) drops a sentence naming a CFBD player or a position+name
that isn't in the prompt, an injury claim with no intel behind it, or a stat
number (yards, TDs, %, sacks...) found nowhere in the prompt.

## Analyst takes and league material (Claude thinks ahead, Picky delivers)

Picky's live model is a 9B, so the heavy thinking happens ahead of time on
Charlie's subscription (`claude -p` Opus via `picky_claude.py`, no tools, no MCP, API key
stripped) and Picky only delivers it.

- **Analyst take** (`analyst.py`, rides on the dossier job and schedule, one
  extra no-tools call per game per run): a numbered brief of the computed
  signals (S1..) and dossier claims (C1..) -> pick vs the pool line,
  confidence, 3-5 cited reasons, the best counter, what the line is missing,
  one quotable line. `validate_take()` drops any reason with a number not in
  the items it cites, a name not in the brief, or report-speak; under 2
  reasons = no take (the previous one is kept, marked stale). Stored as
  `analyst_take` in the dossier. Picky's computed lean stays authoritative:
  when Claude disagrees, `take_note()` keeps the lean as the pick and has Picky
  voice the film room's case as the other side. `dossier.py takes` rebuilds
  only the takes; `--now ISO` replays a past slate for testing.
- **League material** (`league_material.py`, LaunchAgent
  `com.pickem.league-material`, hourly check; builds when a pool week goes
  fully final, Wed 10 AM CT, or on standings drift): DB facts (lore.compute,
  standings gaps, last week, streaks, chalk vs dog, best/worst picks, Picky's
  own record) -> 3-6 roast/praise lines per member, 3-5 storylines, Picky's
  season story, every line citing fact ids; the validator checks every number
  and name against the cited facts and drops body/family/dark lines. Private:
  `/Users/Agent/cfb-pickem-private/league_material.json` (0600).
- **Delivery** (`chat._user_turn`, so the site and text Picky share it):
  `take_note()` for opinion/breakdown questions about open pool games,
  `material_note()` when a member is named or the asker asks about himself,
  storylines and Picky's own story on those questions. Both say "ammo, not a
  script" and "answer first, talk like a person". Material is used only while
  the member's season record still matches the live standings. The grounding
  filter sees both (they're in the prompt) and `record_check` accepts a true
  material split ("favorites went 22-14") unless it's passed off as the season
  record. `PICKY_AMMO=0` turns both off.
- **Exam**: `eval/run.py --at <time> --replay` masks the games after that time
  as unplayed (game items without a live slate); items take-casual,
  take-disagree, roast-member, roast-me-material, league-storylines,
  picky-season-story; a `not_bot` check; the report shows a repetition line;
  `eval/judge_quality.py BEFORE.json AFTER.json` is the blind breakdown /
  roast / human / helpful judge.

Tests:

    ./.venv/bin/python3 -I test_privacy.py     # unrevealed picks never leak
    ./.venv/bin/python3 -I test_standings.py   # points match the leaderboard
    ./.venv/bin/python3 -I test_analyst.py     # analyst take + league material validators, delivery
    ./.venv/bin/python3 -I test_grounding.py -v [--model qwen2.5:14b-instruct]
    ./.venv/bin/python3 -I test_e2e.py [--remote] [--as TEAM]  # real /chat, answers printed

`test_grounding.py` asks ~11 probe questions against live data (service key
from the keychain) and checks counts, spreads, records and last-week claims
automatically. Read the printed answers too: the 7B model still slips on
roast details now and then.
