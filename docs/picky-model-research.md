# Picky: why it's dumb, and what to do about it

Research written 2026-11-14 before the upgrade; the upgrade log is at the end.

## What Picky runs today

- **Backend:** `server/chat.py`, a FastAPI app under LaunchAgent `com.pickem.chat` on `127.0.0.1:8766`. The site reaches it through the Cloudflare tunnel at `pickem-chat.dunecreek.ai`. The process has been up since 03:20 today, so it's running the latest "grounded answers" commit (`ac79ba7`).
- **Model:** local Ollama 0.20.2 on this Mac mini (M4, 10 cores, 16 GB). The model is `qwen2.5:7b-instruct` at Q4_K_M (7.6B params, a late-2024 model), set by `PICKEM_CHAT_MODEL` in the plist. `qwen2.5:14b-instruct` is pulled but not used.
- **Options:** `num_ctx` 8192 (`ollama ps` confirms 8192), temperature 0.2. The prompt is about 3.3k tokens before history. History is capped at 10 turns on both client and server. **Nothing is being truncated.** The 2048/4096 default-context problem was already fixed.
- **Architecture:** no tool calling and no RAG. Each request rebuilds a text snapshot and stuffs it into the system prompt:
  - standings with the last 3 weeks
  - this week's pool games (only 4 this week: Wake Forest at SMU, Oregon State at Boise State, North Texas at UTSA, NDSU at Hawai'i), each with pool line, ESPN market line, a computed "line value" gap, AP rank, record, last 5, injuries, and team-tagged headlines
  - the caller's own picks
  - other teams' picks, only for games that have kicked off
  - last week's finals, with every team's picks and hit/miss
  - the jackpot
  - on the picks page, "rivals" with probable picks from the pool's own model
- **System prompt:** a long list of hard grounding rules ("every fact must appear in LEAGUE DATA", "never guess"). It also has a privacy rule with a scripted line, roast examples, and a mode rider: "LEADERBOARD MODE: keep answers short... league gossip and quick stats" or "DEEP ANALYSIS MODE" on the picks page.

## Why it's dumb, ranked

### 1. The injury data is missing, and the prompt tells the model it isn't (data/plumbing bug)

I pulled ESPN's summary endpoint for all 4 games this week. The `injuries` array is **empty for every game**: ESPN doesn't publish a usable college injury feed there. `espn.py` even computes `injury_report_available`, but nothing reads it. `render_context()` turns the empty array into **"Injuries: ESPN lists none for either team."** The model reasonably read that as "safe to assume healthy based on ESPN." That isn't really a hallucination. We fed it a false statement. Fix: say "no injury report available" when the feed is empty, and never claim "none."

### 2. The per-game facts are too thin to support real reasoning (data problem)

For each game Picky gets only: record, AP rank, last 5 scores, the ESPN market line vs the pool line, and up to 3 headlines. With 4 small-conference games, most of which are unranked, the honest reason for most picks really is "they're 8-2." It has none of these:

- ATS records
- SP+ or FPI ratings
- yards per play, or offense/defense ranks
- a home/road split
- weather
- line movement

ESPN's win probability was dropped on purpose because the 7B model kept misquoting it. No model gives better reasons when the only facts are a record. This is why "should I change any of those" got "mix it up!"

### 3. The TG answer was the privacy rule working as designed, with bad wording (prompt problem)

The first pool kickoff is 11:00 AM CT today, and Charlie asked around 6:58 AM. By design, other teams' picks are hidden until each game kicks off. The scripted line is "Can't say, picks aren't locked in yet for that one." That reads like TG hasn't picked, which is misleading. It means the picks are hidden until kickoff. Fix the wording, e.g. "TG's picks unlock at kickoff, first game 11 AM." If the league actually wants picks visible earlier, that's a rules decision, not a model one.

### 4. The 7B model is weak at multi-step reasoning (model problem, real but not the main cause)

"Should I change any of those" means: find each of my picks, compare it to the line-value side and the other facts, then decide keep or flip for each one. A 2024-era 7B Q4 model handles that kind of multi-hop badly, especially with a long, rule-heavy prompt. The README already notes that it "still slips on roast details." A newer or bigger model would help here. But on causes 1 to 3, a bigger model would just be confidently wrong in better prose.

### 5. Mode rider (minor)

If Charlie asked from the leaderboard page, the prompt explicitly says "keep answers short... gossip." That pushes picks advice toward one-liners. Deep analysis only turns on from the picks page.

## Fixes, in priority order

1. **Fix the data and plumbing (an hour or two, any model benefits):**
   - Use `injury_report_available`: "no injury report available," never "none listed."
   - Precompute a per-pick verdict in Python and add it to the prompt, e.g. "Your pick: SMU -1.5. Market has SMU -1.5, so no edge. Value side: none." Then "should I change" becomes a lookup, not a reasoning chain.
   - Add one real ratings source. Options: the CollegeFootballData.com API (free key; SP+, ATS, lines, team stats) or ESPN FPI. Hand the model a few strong numbers per team.
   - Reword the privacy line to "hidden until kickoff (time)."
2. **Swap the model for Claude Haiku 5.5 via the API (an afternoon).** This is the biggest jump in reasoning, instruction-following, and native tool calling, for a few cents a week (cost below). The prompt-stuffing design works as is. Haiku could later get tools like `get_team_stats(team)` instead of a bigger prompt.
3. **If it must stay local:** move from `qwen2.5:7b` to `qwen3.5:9b` (Q4_K_M, 6.6 GB, tagged tools + thinking in the Ollama library). That's a modest gain, and it only fits on this box if memory is freed (see below).

## Hardware and the best local option per machine

### Mac mini #1 (this box): M4, 16 GB, about 120 GB/s memory bandwidth

- **It's already tight.** A `com.apple.Virtualization` VM is holding about **8.1 GB**. Ollama keeps the 7B loaded forever (6.5 GB with the 8k context). The Z-Image model loads on top whenever someone asks for a picture. Free memory is about 42%, and there are 870k pageouts since boot.
- **Ollama's new MLX engine doesn't apply here.** It requires 32 GB+ of unified memory, so this box stays on llama.cpp Metal.
- **Best realistic local pick: `qwen3.5:9b` Q4_K_M** (about 6.6 GB). Expect roughly 15 to 20 tok/s on a base M4. That's my estimate from bandwidth, not a measured number; the 7B today is probably 20 to 25. Run it as a straight swap for the 7B, with no 14B.
- **Alternative: `gemma4:12b`.** It ranks well on 16 GB lists, but with the VM running it's too big to fit next to the image model.
- **Not realistic:** `gpt-oss-20b` and Qwen3.6-35B-A3B. One blog claims 17 tok/s for a 35B MoE on a 16 GB M4, but that's unverified, and it's impossible with 8 GB taken by the VM.

### Mac mini #2 ("Agents-Mac-mini", agent@10.0.0.23): specs unknown

- **Not reachable today.** SSH and ping to 10.0.0.23 time out. This box is on 192.168.1.x and routes 10.0.0.23 via the default gateway, so it looks like a different network or a VPN that isn't up. Please check the chip and RAM (Apple menu > About This Mac).
- **Guidance by RAM:**
  - **16 GB:** same as above, `qwen3.5:9b`.
  - **24 GB:** `gpt-oss-20b` (MXFP4, about 13 GB). Estimates are about 40 to 60 tok/s on M4 Pro and slower on base M4. The other option is Gemma 4 26B-A4B MoE. Qwen3.6-35B-A3B is "too tight for daily use" at 24 GB.
  - **32 GB+:** **Qwen3.6-35B-A3B** (MoE, about 22 to 24 GB at 4-bit). It's the consensus best local model for chat and tool use on Macs right now. With 32 GB+ it can use Ollama's MLX engine (Ollama 0.19+). Estimates are about 40 to 50 tok/s on base M4 and higher on M4 Pro.
- **Why use mini #2:** a dedicated mini would also stop Picky competing with the VM and image generation on mini #1. Chat would point at `OLLAMA_URL=http://<mini2>:11434` over the LAN.

### MLX vs Ollama

- Raw MLX (`mlx-lm` / `mlx_lm.server`, or LM Studio's MLX engine) typically decodes 20 to 50% faster than llama.cpp Metal on Apple Silicon. Some older tests showed bigger gaps, but Ollama 0.19+ with the MLX engine has mostly closed them, on 32 GB+ machines only.
- On a 16 GB box, switching to `mlx_lm.server` would gain a few tok/s. But it means giving up Ollama's simple model management, and tool-call parsing has had rough edges for Qwen3.5 in Ollama and other runners. Speed isn't Picky's problem anyway.

## The hosted option: Claude Haiku 5.5

**Pricing** (Anthropic pricing page, prompts under 100K tokens):

- input: $0.10 per million tokens
- output: $0.50 per million tokens
- cache reads: $0.01 per million tokens

**Usage per message:**

- input: about 3.5k tokens of system prompt plus up to about 2k of history, call it 5k
- output: about 200 tokens

**Cost per message:**

- input: 5k × $0.10/M = $0.0005
- output: 200 × $0.50/M = $0.0001
- total: about **$0.0006 per message**

**Realistic usage:** say 15 to 20 league members, 10 messages each per week, so about 200 messages a week.

- that's about **$0.12 a week, or about $5 for a whole season**
- even 1,000 messages a week is about $0.60
- prompt caching on the static system-prompt part would cut input cost further

**Tradeoffs:**

- League data (team names, picks, standings) goes to Anthropic. It's low sensitivity, but it's no longer "local only."
- It needs an API key in the keychain.
- Picky goes down if the API or the network is down. You could keep the local 7B as a fallback.
- Latency should be similar to or better than local.
- **Upside:** a much better reasoner and instruction-follower, and native tool calling, so Picky could pull stats on demand later.

**Recommendation:** do the data/prompt fixes first, regardless of model. Then switch Picky to Haiku 5.5. Only go local-upgrade if keeping it off-cloud matters. If it does, the real upgrade is Qwen3.6-35B-A3B on mini #2, and only if mini #2 has 32 GB+.

## Sources

- [Anthropic pricing docs](https://platform.claude.com/docs/en/about-claude/pricing)
- [Ollama: MLX on Apple Silicon (blog)](https://registry.ollama.ai/blog/mlx), [9to5Mac on Ollama MLX](https://9to5mac.com/2026/03/31/ollama-adopts-mlx-for-faster-ai-performance-on-apple-silicon-macs/)
- [Ollama library: qwen3.5:9b](https://ollama.com/library/qwen3.5:9b), [qwen3.5 tags](https://ollama.com/library/qwen3.5/tags)
- [Ollama issue: Qwen 3.5 tool calling](https://github.com/ollama/ollama/issues/14493)
- [Best local LLMs for 16GB RAM (localclaw)](https://localclaw.io/ram/16gb), [llmcheck 16GB Mac](https://llmcheck.net/blog/best-local-llm-16gb-mac/), [modelfit 16GB Mac](https://modelfit.io/blog/best-llm-for-16gb-mac/)
- [llmcheck: Mac mini M4 24GB](https://llmcheck.net/best-llm/mac-mini-m4-24gb/), [Qwen 3.6 hardware requirements](https://willitrunai.com/blog/qwen-3-6-vram-requirements), [modelfit Qwen on Mac by RAM](https://modelfit.io/blog/qwen-35-medium-series/), [Mac mini M4 RAM-tier guide](https://www.popularai.org/p/best-local-llm-mac-mini-m4-2026), [35B on 16GB Mac mini experiment](https://thoughts.jock.pl/p/local-llm-35b-mac-mini-gemma-swap-production-2026)
- [Local tool-calling benchmark (ToolCall-15)](https://openagent-bot.pages.dev/blog/local-llm-tool-calling-benchmark-toolcall-15), [promptquorum tool-calling picks](https://www.promptquorum.com/power-local-llm/best-local-models-tool-calling-2026)
- [Ollama vs llama.cpp vs MLX on Mac (2026)](https://terminalbytes.com/ollama-vs-llama-cpp-vs-mlx-mac-2026/), [Qwen 3.5 MLX vs Ollama speed test](https://insiderllm.com/guides/qwen35-mac-mlx-vs-ollama/)

Note: most of the local-model speed figures above come from third-party blogs and estimates, not primary benchmarks. Measure on the actual box before committing.

## Upgrade log (2026-11-14)

### What changed

- **Model:** Claude Haiku 5.5 (`claude-haiku-5-5`) via the Anthropic SDK, adaptive thinking at effort `medium`. If the API call fails before the first token, `qwen2.5:7b-instruct` on Ollama answers instead (tested with a bad key). The key is read from the login keychain (`drake` / `DCC__ANTHROPIC_API_KEY`) at startup, never from the repo. Ollama is no longer warmed and pinned at startup: as a fallback it loads on demand and unloads after 15 idle minutes, which frees ~6.5 GB on this box. The fallback's `num_ctx` went to 16384 because the prompt is now ~9.5k tokens.
- **Injuries:** an empty ESPN injury list now renders as "NO injury report available ... health unknown," and the prompt forbids saying a team is healthy.
- **More data, no new key:** ESPN's summary endpoint already carried season per-game team stats (points, yards, allowed), stat leaders, the FPI matchup projection and weather, so those go into each game block now. The FPI line is labeled "chance to WIN outright, NOT to cover." **CollegeFootballData was skipped:** there's no CFBD key in the keychain. ESPN's `againstTheSpread` block exists but is empty for these games, so there are still no ATS records.
- **Value check computed in code:** `pick_verdict()` compares the caller's pick to the market vs the pool line and writes "IS the value side / WRONG side / no edge" per pick. The model only reads it.
- **Privacy wording:** hidden picks now read "unlocks at kickoff (Sat 11:00 AM CT)," and the prompt adds "hidden is NOT the same as not picked." The rule itself (others' picks hidden until kickoff) is unchanged, and `test_privacy.py` still passes.
- **Persona:** a cocky sports-radio heel with a major attitude who calls bad picks stupid, roasts picks, records and standings, and never touches protected traits, looks, family and so on. Accuracy outranks attitude. Also: standings rows spell out "N points, M misses," the current time is in the context, and there are explicit data-limit rules (no ATS claims; last-5 games aren't "last week").
- **Site strings:** the greeting and error bubbles in `js/picky.js` are in the new voice.
- **Testing:** `server/test_e2e.py` runs 13 probes (14 questions) through the real `POST /chat`. `--remote` hits pickem-chat.dunecreek.ai as Charlie's own account. The default runs in-process with auth stubbed, for other personas such as last place.

### Before/after (same 14 questions, as Chuck P; #8 as TG)

| | Before (qwen2.5 7B) | After (Haiku 5.5, final) |
|---|---|---|
| Answers with a factual error | 7 of 14 | 0 of 14 in the final remote run (one hyperbole: "most misses by a mile," 26 vs 23) |
| "Should I change" | "Your picks are solid... good luck" | Flip Wake Forest -1.5 to SMU +1.5 and Oregon State +13.5 to Boise State -13.5 (both 3 points off the market); keep UTSA and NDSU (no edge), with stats |
| Best value | Said Oregon State. **Wrong side** | Boise State -13.5 and SMU +1.5; notes the user holds the wrong side of both |
| TG's picks + injuries | "picks aren't locked in yet" + "ESPN lists no injuries, you're good" | Unlock times for all 4 games; "ESPN doesn't publish college injury reports... health is unknown" |
| Spread explainer | Backwards ("Boise covers by winning by less than 13.5") | Correct cover math, market gap, FPI labeled as a win chance |
| Last week | Wrong records for 4+ teams | Every team's record correct |
| Roast TG | "can't find the remote", "three strikes" (muddled) | 26 misses (most), 1-3 week 9, BYU miss, tied with Culligan: "That's not a slump, that's a lifestyle." |
| Latency | 2 to 27 s | 2 to 10 s |

### Iterations

1. Effort `low`, new data + persona. Facts mostly right. Roasts too polite; answers ~200 words; pivots to the user's picks; "best week all year" and "last week" claims the data can't support; a mid-answer "wait...".
2. Style rules (open with a jab, 40 to 90 words, wrong-value picks are "stupid") plus data-limit rules. Punchier, but one hallucination ("SMU covered its last 4": there's no ATS data) and an invented habit ("stop taking dogs").
3. No-ATS rule, no invented habits, third person for other members, effort `medium`. No hallucinations; better multi-step facts ("won three of its last five by 14+" is correct).
4. Roast structure (3 to 4 escalating burns), more voice examples, at most one "genius" per answer, off-topic gets roasted and dropped. Deployed and run remotely.
5. Fixes from the remote run: the current time in context (it had said "can't wait four hours" for a 10 PM kickoff at 7:30 AM), and standings rows spell out points vs misses (it had said Culligan had 18 misses; he has 22).

### Cost (measured)

About 9.5k input and 1.5k output tokens per message (output includes thinking), so ~$0.0017 per message at $0.10 / $0.50 per MTok. That's ~$0.34 a week at 200 messages, or ~$1.70 at 1,000.

### Still weak

- No injury data and no ATS records exist anywhere in the pipeline. A CFBD key would add ATS, SP+ and more stats.
- "Football call" games (no price edge) can get a different lean in separate conversations (e.g. UTSA: keep vs flip).
- Billing goes to the DCC Anthropic key. Give Picky its own key if you want separate billing.
- The Ollama fallback is still the old 7B: slow on a cold load (~40 s) and much less accurate.
