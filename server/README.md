# Pick'em League Assistant — `chat.py`

FastAPI service that powers **Picky**, the league assistant chat on the
leaderboard page. Runs on the Mac mini under LaunchAgent `com.pickem.chat`,
listens on `127.0.0.1:8766`, reached from github.io through the Cloudflare
tunnel at `pickem-chat.dunecreek.ai`.

## What it does

- Validates the caller's Supabase JWT via `/auth/v1/user` (no secret stored).
- Pulls a compact snapshot of league state from Supabase **using the
  caller's JWT**, so row-level security applies to everything.
- Builds a system prompt in Picky's voice and calls Ollama's
  `/api/chat` streaming endpoint, forwarded back as SSE.
- Rate-limits 10 req/min per user, in-memory.
- Warms the model on startup so the first query of the day doesn't eat
  a ~20 s cold start.

Errors: `401` bad/missing token · `429` rate-limited · `502` Ollama or
Supabase unreachable · `500` otherwise. Streamed error chunks look like
`data: {"error": "..."}`.

## Config

Env vars (set in the plist):

| Var | Default | Notes |
| --- | --- | --- |
| `PICKEM_CHAT_MODEL` | `qwen2.5:7b-instruct` | Swap to `qwen2.5:14b-instruct` for "smarter mode". |
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
the gap between them, AP rank, record, last five, injuries, team-tagged
headlines), last week's finals with every team's hits and misses, and the
jackpot. `focus_block()` repeats the facts for any league member named in
the question (or the caller, for "roast me" / "how did I do") at the end
of the prompt. ESPN data comes from `espn.py` (public summary endpoint,
10 minute cache, fails soft to "unavailable").

Ollama runs with `num_ctx` 8192 (prompt is ~3.3k tokens before history)
and temperature 0.2.

Tests:

    ./.venv/bin/python3 -I test_privacy.py     # unrevealed picks never leak
    ./.venv/bin/python3 -I test_standings.py   # points match the leaderboard
    ./.venv/bin/python3 -I test_grounding.py -v [--model qwen2.5:14b-instruct]

`test_grounding.py` asks ~11 probe questions against live data (service key
from the keychain) and checks counts, spreads, records and last-week claims
automatically. Read the printed answers too: the 7B model still slips on
roast details now and then.
