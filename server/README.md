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

Tests:

    ./.venv/bin/python3 -I test_privacy.py     # unrevealed picks never leak
    ./.venv/bin/python3 -I test_standings.py   # points match the leaderboard
    ./.venv/bin/python3 -I test_grounding.py -v [--model qwen2.5:14b-instruct]
    ./.venv/bin/python3 -I test_e2e.py [--remote] [--as TEAM]  # real /chat, answers printed

`test_grounding.py` asks ~11 probe questions against live data (service key
from the keychain) and checks counts, spreads, records and last-week claims
automatically. Read the printed answers too: the 7B model still slips on
roast details now and then.
