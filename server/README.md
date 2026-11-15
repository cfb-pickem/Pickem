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
  (`qwen3.5:9b-q4_K_M`) back as SSE. No API calls, no API cost. The Claude
  Haiku path is still in the code but off unless `PICKY_USE_CLAUDE=1`.
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
| `PICKY_USE_CLAUDE` | (unset) | `1` turns the Claude path back on (paid API, Ollama becomes the fallback). Unset, the key is never read. |
| `PICKY_CLAUDE_MODEL` | `claude-haiku-5-5` | Only with `PICKY_USE_CLAUDE=1`. |
| `PICKY_CLAUDE_EFFORT` | `medium` | `low` is faster but sloppier on multi-step facts. |
| `ANTHROPIC_API_KEY` | (unset) | If unset, read from the login keychain: account `drake`, service `PICKY_ANTHROPIC_KEYCHAIN` (default `DCC__ANTHROPIC_API_KEY`). Never commit it. |
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

## Game dossiers (`dossier.py`)

Per-game injury / availability / insider notes for the open pool week,
fully automatic. LaunchAgent `com.pickem.picky-dossier` fires every 30
minutes; `dossier.py` builds a game's dossier when it first appears in the
slate, again after Friday 5 PM and Saturday 7 AM CT, never after kickoff.
Leads come from the news cache and r/CFB's public RSS; `claude -p` (sonnet,
subscription, web tools only, no MCP) researches; a validator enforces the
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
Charlie's subscription (`claude -p` sonnet, no tools, no MCP, API key
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
