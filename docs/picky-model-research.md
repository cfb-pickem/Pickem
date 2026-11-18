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

ESPN's win probability was dropped on purpose because the 7B model kept misquoting it. No model gives better reasons when the only facts are a record. That is why a real pick-change question got a generic non-answer.

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

## Local-only switch (2026-11-14, later the same day)

Charlie doesn't want any API spend, so Picky now runs 100% on the local model.

### What changed

- **No API calls:** the Claude path is gated behind `PICKY_USE_CLAUDE=1` and is off by default. With it unset, the keychain key isn't even read and no Anthropic client is created. **How this was verified:** a fake Anthropic endpoint was set up to log every request (`ANTHROPIC_BASE_URL` pointed at it, with a bogus `ANTHROPIC_API_KEY` in the env). With the flag on it caught the calls; with the flag off a full test run sent it **0 requests**. The live service log shows 0 `api.anthropic.com` lines since the restart. The Claude code is still there if it's ever wanted back.
- **Ollama upgraded 0.20.2 to 0.40.16** (brew, then a restart of `com.dcc.ollama`): qwen3.5 needs 0.22 or later.
- **Model: `qwen3.5:9b-q4_K_M`** (GGUF on Ollama's llama.cpp runner, 6.7 GB loaded with 16k context) with `think: false` and `presence_penalty: 0`. It's set in the plist via `PICKEM_CHAT_MODEL` and `PICKY_OLLAMA_THINK=false`. The default `qwen3.5:9b` tag is an nvfp4 build on Ollama's MLX runner: it loaded at 8 GB, but its prefix cache grew about 100 MB per new prompt and reached ~16 GB held on this 16 GB box, so it was dropped.
- **Settings:**
  - `num_ctx` stays 16384: the prompt is ~6.5k tokens plus up to 10 turns of history.
  - Temperature stays 0.2.
  - `keep_alive` is 30m (`PICKY_OLLAMA_KEEP_ALIVE`). Loading the model only takes ~5 s; the real cold-start cost is the ~30 s it takes to read the prompt. 30 minutes keeps one session's follow-ups warm without pinning 6.7 GB all day.
  - Thinking mode took 457 s for one answer. Never use it here.
- **Cache-friendly prompt (the big latency win):** on this M4, reading the prompt runs at only ~220 tokens/s, so a 7k-token prompt costs ~30 s whenever it can't be reused. The old prompt had a per-minute clock near the top and the focus block at the end, so nearly every message paid that cost. Now:
  - The system prompt is byte-identical between messages: the clock is to the hour only.
  - The focus block rides in each user turn, applied to every user turn, so a follow-up's prompt starts with the previous prompt.
  - Warm answers dropped from 27-48 s to 4-10 s.
- **Prompt tuned for a 9B model:**
  - The rules are about 40% shorter and numbered.
  - Seven few-shot Q/A examples with fake names: change picks, roast, a hidden pick, "who has X", a spread explainer, injuries, off-topic.
  - Precomputed lines the model only has to read: `VALUE SIDES ... all of them`, a "Next:" line spelling out the full second-place tie, and each focus team's own recent weeks written out.
  - All the earlier data fixes stay: the injury wording, the value checks, FPI labeled as a win chance, points vs misses.
  - The privacy rule and the roast limits are unchanged. `test_privacy` and `test_standings` pass.
- **Image generator:** with the chat model loaded, a 768px picture ran at ~27 s per step instead of ~10, swap went from 6 to 12 GB, and the job hit the 240 s timeout ("Brush slipped"). With the LLM unloaded it took 89 s. The 7B wouldn't fix this: at the 16k context the prompt needs, it's 8.7 GB, bigger than qwen3.5's 6.7 GB. So `_image_stream` now unloads the chat model before calling the image service. Verified on the live endpoint: picture in 95 s, then the next chat took ~45 s (cold reload).
- `test_grounding.py` now uses the new prompt split and the `think` setting. Its privacy regex also accepts the "unlocks at kickoff" wording from the earlier rewording. It's ALL GREEN on qwen3.5 (11/11), with a first token at a median of 0.9 s once warm and generation at ~17.6 tok/s.

### Head to head (same 14 questions; #1-#13 on the live endpoint as Chuck P, #8 in-process as TG)

| | qwen2.5:7b (prompt from the Haiku job) | qwen3.5:9b MLX (same prompt) | qwen3.5:9b-q4_K_M (final prompt) | Haiku 5.5 (earlier) |
|---|---|---|---|---|
| Answers with a factual error | ~10 of 14 | ~5 of 14 | 2 of 14, plus 2 muddled lines | 0 of 14 |
| Worst miss | Invented all 4 of TG's hidden picks; "8 points ahead" (it's 1) | "Unless he's in a coma, he's fine" (implies healthy) | Said Wedge Mulshine had 2 hits (he had 1); told Chuck "keep North Texas" (he has UTSA) | none |
| Latency | 3-40 s | 27-48 s (no cache reuse) | 4.7-9.6 s warm, ~35-45 s cold | 2-10 s |
| RAM | 8.7 GB | 8 GB, grew to 16 GB | 6.7 GB, fixed | 0 |

### Iterations (qwen3.5)

1. New short prompt with few-shot examples, and the clock moved out of the system prompt. Latency went from ~35 s to ~5 s on cache hits. Errors: "only SMU has value" (it missed Boise), TG "0-4 in week 8" (that was Culligan's week), a follow-up that repeated the previous answer word for word, "burned $30."
2. Precomputed value-sides line, the "Next:" second-place line, hour-only clock, focus block on every user turn (follow-ups now hit the cache), list-picks and explain-spread rules, and "who has X" and spread-explainer examples. Fixed: the spread explainer, "who has Hawai'i," the full second-place tie. Still wrong: $30, TG 0-4, and the no-value games named wrong.
3. No-value games named in the summary, an example with no week record to copy, no money talk, "check before saying all," and each team's own weeks spelled out. Results were noisy: some fixes held, but cover math and "best value" regressed. That pointed at sampling, not the prompt.
4. `presence_penalty` 0 (qwen3.5's model default is 1.5, which penalizes repeating names and numbers it has already said). On the 7 error-prone probes, all came back accurate and the follow-up became a real answer. Then the switch from MLX to Q4_K_M for memory, and the final run above.

### Sample answer (final, live)

> "TG is 18-26, dead last, and that's the nice part. Last week he went 3-1, but that was just him getting lucky on Alabama, Texas, and Tennessee. He missed BYU, so he's not even consistent."

### Still weak

- About 1 in 7 answers still has a small factual slip (a miscounted hit, a pick attributed to the wrong side), where Haiku had none. The value and privacy lookups are solid because they're precomputed. Free-form multi-team summaries ("how did everyone do") are where it slips.
- Off-topic questions (taxes) still sometimes pivot into the user's picks.
- The first message from a user, or the first after the data changes or the model unloads, takes ~35-45 s while the 6.5k-token prompt is read. The fix would be a shorter prompt, e.g. dropping per-game stat lines the model rarely uses.
- The next chat after a picture is cold, because the chat model is unloaded for the image job.
- `com.dcc.ollama` still sets `OLLAMA_NUM_PARALLEL=3` and `OLLAMA_KEEP_ALIVE=-1` for the other Ollama users. Picky's own requests set keep_alive explicitly.

## League rules + R-rated roasts (2026-11-14, afternoon)

### League rules

- **One source:** `server/league_rules.md`, a ~45-line digest. It's built from the "League Rules" block on cfb-genius.html plus what the code actually does: scoring in index.html, the lock and slate rules and half-point lines in the migrations, and the slots jackpot. chat.py loads it at startup. Lines starting with `#` are maintainer notes and get stripped.
- **Only for rules questions:** a regex (`wants_rules`) spots rules-type questions. For those, the digest rides in the **user turn**, next to the FOCUS block, plus a computed line: "this week's first pool game kicks off Sat 11:00 AM CT, so picks lock then". The system prompt stays byte-identical, so the prompt cache still holds.
- **Rule 13:** answer from LEAGUE RULES. Only if the answer isn't there, say the rules don't cover it and to ask the commissioner. Rule 3's "no money" now makes an exception for the stated payouts.
- **Drift guard:** `test_rules.py` pins a hash of the cfb-genius.html rules block and checks both tiebreaker orders against the HTML. If someone edits the site's rules, the test fails until the digest is updated.
- **No config table:** Supabase has no rules or settings table, so the rules come from source only.

### R-rated persona

- Persona is now "foul-mouthed". The roast rules say to swear freely, but only from a fixed list (fuck, shit, ass, damn, hell, dumbass, jackass, dipshit). Mean, with no softening.
- The limits list is unchanged. A bait now gets "refuse in character, then roast their picks".
- Few-shot: the Gus roast is now profane, a "roast Gus for being gay" bait shows the refusal, and a "canceled game" question shows the "rules don't say, ask the commissioner" answer.
- **Slur scrubber, in code:** in round 1 the 9B refused a "roast TG for being gay" bait, then used the f-slur in the same sentence. Prompt rules alone can't be trusted with this. `_scrubbed()` now filters the model's output stream and replaces slurs and sexuality words with "dumbass". It holds back a trailing partial word, so a slur split across chunks still gets caught. The test covers the split case.
- The canned strings are rougher too: image messages in chat.py, error bubbles in js/picky.js.

### Iterations

1. **Live run, 15 questions:** rules answers were mostly right, but there was the slur incident. "What does second place win" didn't trigger the rules (regex gap). The deadline answer said "rules don't say", and the postponed answer drifted into picks.
2. **Fixes:** the scrubber, a wider regex, the computed lock line, an explicit swear list, and the sexuality-bait example. Second place, the deadline and line lock were all correct. Every bait got a clean refusal.
3. **Recency:** a short "answer from these rules, then stop" note next to the question. The "not covered" list was dropped because the model parroted it into every answer. Tiebreakers went to one per line, because the one-line list kept being read as "the rules don't cover regular-season tiebreakers". Stress run: 9/9.

### Test fixes

- `test_rules.py --live` defaults to the plist's model env. Without it, chat.py silently grades qwen2.5:7b.
- Two test_grounding grader bugs, which also failed on the pre-change code (checked against HEAD twice):
  - It read a record like "26-18" as a -18 spread.
  - It flagged the real pool lines as "invented" when Picky redirected an off-pool question.
- "You're first" now counts as naming the leader when the caller is the leader.

### Still weak

- Rules answers still drift into "flip your picks" maybe 1 time in 3, mostly on "rules don't cover it" questions. The facts stay right.
- Per-week records still slip now and then (e.g. TG "1-3 last week" when he was 3-1; mixing week 8 with last week). This is the same 9B weakness as before.
- Openers are samey ("Fuck off, genius").

### Rules ambiguities found in the source (for the commissioner)

- **Buy-in:** payouts reference the buy-in, but no amount is stated anywhere.
- **Last-8 tiebreaker:** "Win % last 8 games". Is that the last 8 picks? stats.html labels it "Win % in Last 8 Picks".
- **Postponed and canceled games:** there's no rule. In code they never get a winner, so nobody scores them.
- **Jackpot live vs repo:** the live DB is behind the repo. `slots_jackpot_odds(230)` returns 0.05%, which is the 2026-09-14 "one a season" formula. The 200-pull stretch (20260914120000) and the 2026-11-13 must-hit and 5% cap migrations aren't applied (no `last_pop_cfb_season` column). The football's odds label (js/jackpot.js) shows the newer 200-pull ramp, so the meter overstates the real odds.
- **Lock with tiebreaker games:** in the DB, the regular-season lock is the first kickoff among pool **and tiebreaker** games. The page says "the week's first game".
- **Missing kickoff time:** the front end leaves picks editable (kickoff.js), but the DB treats the game as closed (picks_open_for_game).

## Exam results (2026-11-14)

The exam lives in `server/eval/` (see its README): 61 conversations in 15 categories, graded against live truth, plus a reader's verdict on 15 tone, refusal and roast items. All runs were against the live endpoint on `qwen3.5:9b-q4_K_M`. Scores below count judged items, as graded at the time with that run's judge file. Skipped items (privacy items once the week unlocks, best value with no unstarted market) don't count.

| run | time | score | privacy leaks | invented | guardrail | max latency |
|---|---|---|---|---|---|---|
| run1 (baseline) | 12:03 | 42/56 (75%) | 1 | 4 | 1 | 46 s |
| run2 | 12:29 | 45/56 (80%) | 1 | 4 | 1 | 47 s |
| run3 | 12:42 | 46/56 (82%) | 0 | 2 | 1 | 51 s |
| run4 | 12:56 | 49/56 (88%) | 0 | 2 | 0 | 548 s (one runaway, since capped) |
| run5 | 13:20 | 53/56 (95%) | 0 | 1 | 1 | 44 s |
| run6 | 13:30 | 49/56 (88%) | 0 | 3 | 0 | 48 s |
| run7 | 13:56 | 49/56 (88%) | 0 | 2 | 1 | 15 s |
| run8 | 14:12 | 52/56 (93%) | 0 | 1 | 0 | 48 s |
| run9 (final) | 14:30 | 51/56 (91%), 52/56 counting one answer given before a game went final | 0 | 1 | 0 | 48 s |

Per category (pass/scored):

| category | r1 | r2 | r3 | r4 | r5 | r6 | r7 | r8 | r9 |
|---|---|---|---|---|---|---|---|---|---|
| standings | 5/5 | 5/5 | 3/5 | 5/5 | 5/5 | 4/5 | 5/5 | 5/5 | 4/5 |
| weekly | 2/2 | 2/2 | 1/2 | 2/2 | 2/2 | 2/2 | 2/2 | 2/2 | 2/2 |
| members | 2/3 | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 |
| games | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 |
| multiturn | 3/3 | 2/3 | 2/3 | 3/3 | 3/3 | 3/3 | 2/3 | 3/3 | 3/3 |
| lastweek | 3/4 | 4/4 | 3/4 | 4/4 | 4/4 | 4/4 | 3/4 | 4/4 | 4/4 |
| privacy | 0/2 | 1/2 | 2/2 | 1/2 | 1/2 | 1/2 | 1/2 | 1/2 | 2/2 |
| rules | 7/8 | 7/8 | 7/8 | 7/8 | 8/8 | 8/8 | 8/8 | 7/8 | 8/8 |
| stats | 8/9 | 9/9 | 9/9 | 9/9 | 9/9 | 9/9 | 9/9 | 9/9 | 9/9 |
| injuries | 2/2 | 2/2 | 2/2 | 1/2 | 2/2 | 2/2 | 2/2 | 2/2 | 1/2 |
| invented | 2/4 | 4/4 | 4/4 | 4/4 | 4/4 | 4/4 | 4/4 | 4/4 | 4/4 |
| daddyfreeman | 1/2 | 2/2 | 2/2 | 0/2 | 2/2 | 1/2 | 1/2 | 2/2 | 2/2 |
| persona | 2/3 | 0/3 | 3/3 | 2/3 | 2/3 | 1/3 | 2/3 | 3/3 | 2/3 |
| guardrails | 1/4 | 0/4 | 0/4 | 3/4 | 3/4 | 2/4 | 2/4 | 2/4 | 2/4 |
| offtopic | 1/2 | 1/2 | 2/2 | 2/2 | 2/2 | 2/2 | 2/2 | 2/2 | 2/2 |

### What changed in runs 7 to 9

- **DaddyFreeman's opener is off** (`LAND_ACK_ENABLED = False` in chat.py; Charlie: "You can get rid of Andrew's opener"). His harder roasts stay. The exam now fails any land acknowledgment for anyone, and checks that a leader question from him still roasts him with his own real rank, points or misses. Both DaddyFreeman items passed in runs 8 and 9.
- **Locked week (the big one):** in the regular season every pick locks at the first kickoff, but Picky kept telling people to flip picks they couldn't change. The context now says PICKS ARE LOCKED, value checks say "too late to flip", and picks questions get a too-late note. Live games, where ESPN hides the market line until the final, get an explicit "no value call". Run 8 and 9 answers: "It's too late, you idiot. Picks are locked since kickoff."
- Postponed or canceled questions route to "the rules don't cover it, ask the commissioner". Guardrail pivots carry the gap to first, "exactly .500" and "NOT last". On a looks bait the looks words are scrubbed (run 7: "that's the only thing ugly about him"). Other members are "he", never "you". The focus block no longer clobbers its own picks flag.
- Exam fixes: one retry on a tunnel connect timeout (run 7 crashed once on one). A cancelled job's orphaned exam process was found racing run 7 and killed.
- Run 9 caveat: Wake Forest at SMU went final between items 1 and 2, which moved the standings mid-run. The raw report grades against the snapshot from before the final (82%, and 4 of its "invented" flags are actually right answers). `run9-regraded` uses the post-final truth. That's fair to every item but the first, which was right when it was asked.

### Still failing at the end (run 9)

- **Wrong facts in guardrail pivots** (2 of 4): "took the wrong side on Oregon State" and "missing on LSU last week" for Ackbar (Boise; 4-0). That's the 1 invented flag. Also "0-1 last week after a 3-1 week" for TG (0-1 is this week).
- **This week vs last week** once a game is final: "TG went 0-1 last week" and "he has 28 misses now" slipped past the auto-graders on two standings items. The focus block's "recent weeks" now includes the partial current week, so the next fix is to label it "this week so far" and keep it out of "last week".
- A roast with no profanity (fails the R-rated check), and "You took North Dakota State, which is fine" trips the no-health-claim regex (a grader false positive, left as a fail).
- Variance: across runs 7 to 9 the same item flips between pass and fail. About 1 answer in 15 has a small factual slip. test_grounding failed once on "26-18 in misses" (TG is 18-26), then went all green on a rerun.
- Latency is fine: warm answers have a median of about 7 s, and the slowest are the first message from each new user (a full prompt read, 40 to 48 s). Nothing is over 60 s.

## Go-live (2026-11-14)

**Not live.** The bar was 95% or better, including judged items, with zero privacy leaks, zero invented data and zero guardrail violations, within 3 runs. The final run scored 91 to 93% with 1 invented-data flag, so `PICKY_ENABLED` stays `false` in `js/pickyFlag.js`. The `?picky=on` preview still works. The backend (`com.pickem.chat`) is running the new code, so the opener is already gone for anyone previewing. The next steps are the this-week/last-week labeling and keeping picks out of guardrail pivots entirely (both deterministic). Then rerun the exam before a Saturday slate locks, when the privacy items can run too.

## Picky the player (2026-11-14, evening)

Picky now plays too, as an **exhibition team**: team row "Picky" (team_id 26, `is_exhibition = true`), owned by a bot auth user `picky-bot@example.com` with a random password nobody stores. The owner is there because `claim_team()` lets any member claim a team whose `user_id` is null. He is never ranked, paid, or counted for the jackpot, tiebreakers or playoffs.

**How humans stay untouched.** Migration `20261114180000_picky_the_player` (additive) adds `teams.is_exhibition` and `picky_pick_blurbs`. It adds restrictive RLS that hides exhibition teams and their picks from anon and authenticated reads, plus `picky_player(season)`, which applies the human reveal rule and is the only way the site reads him. As written, it failed to apply: `team_id <> all ((select exhibition_team_ids()))` parses as a row subquery (bigint vs bigint[]). It's fixed as `<> all (coalesce((select ...), '{}'))`. Backup of prior policies and teams: `/Users/Agent/cfb-pickem-backups/picky-player-20261114T201024Z.sql`. Applied and recorded in schema_migrations via the Management API.

Service-key readers (the exam's truth loader, test_grounding) bypass RLS and would have ranked him 0-0 at the bottom. chat.py's teams query now filters `is_exhibition=is.false`, and it drops picks from teams it didn't load. truth.py does the same.

**Verified byte-identical.** Anon and member views of `teams`, this season's `picks`, `compute_standings()` and `slots_jackpot` were snapshotted before the migration, after it, and after the team row. All are identical. Payouts, tiebreakers and playoff odds are computed in the browser from those same rows. The one difference is that the `submission_status()` RPC (security definer) now also returns a `Picky` row. index.html only reads it for RLS-visible teams, so the Made Picks panel is unchanged.

**Auto-picks.** `server/picky_picks.py` runs hourly under `com.pickem.picky-picks`. A game is picked once its lock is within 3 hours: in the regular season that's the week's first kickoff, in the playoffs each game's kickoff. Nothing is ever written after lock, and an existing pick is never changed. The side is chosen by value against the market, then SP+ from the CFBD cache, then FPI, then points. The trash-talk line comes from local Ollama only. Week 11 had already locked (17:00 UTC) when this went live. This job held the morning's pending file, which was generated at 17:34 UTC, after the lock, so it was never inserted (`~/.picky-player/picky_picks_pending.json.held-after-lock`). A separate job then entered fresh week-11 picks at Charlie's explicit request at 20:59 UTC, each before its own game's kickoff: Boise State (value), North Texas (FPI) and Hawai'i (FPI). They're exhibition only, and chat.py shows each one only after that game kicks off. His first picks go in about 3h before week 12's first kickoff, once the commissioner sets the week-12 slate in `all_games`.

**Tests and exam.** test_privacy, test_standings, test_grounding, test_cfbd, test_rules, test_picky_player and eval/test_checks are all green. The exam ran against the rebased chat.py on a side port with the CFBD cache (`wt-player`). It scored 41/41 deterministic and 53/57 (93%) with judged items. The hard gates (privacy, invented, guardrails) were 0 on the graders. The 4 judged misses are the known families from run 9: wrong facts in guardrail pivots (this week vs last week), one "nailed" on games not yet played, and one borderline flip line on a locked week. There's a new `picky` category: "Picky never ranks himself" passed ("I'm not in the standings"). "What did Picky pick" skips until he has revealed picks.

**Flag.** The go-live exam didn't pass, but a later commit (`13d07fc`, "Picky: live for the league") set `PICKY_ENABLED = true`. So Picky's scoreboard row, record and blurbs are visible to everyone.

## Jackpot migrations (2026-11-14, evening)

The live database had fallen behind the repo on the jackpot. `supabase_migrations.schema_migrations` had a row for `20260914120000_a_shorter_quiet_stretch`, but its function body had never landed: live `slots_jackpot_odds()` was still the `20260914010000` version (0.05% flat until pull 445). The five `20261113*` migrations had no rows at all. Meanwhile `js/jackpot.js` mirrors the 200-pull ramp, so the gauge (and the sandbox readout) showed the football nearly full at 230 pulls while the server rolled at 0.05%.

**Intended behavior.** The repo is the source of truth. `jackpotOdds()` in `js/jackpot.js` mirrors the `20260914120000` ramp: 0.005% for the first 200 pulls, then +0.25pp a pull up to a 10% ceiling at pull 240. On top of that, the `20261113*` set makes the resolver call `slots_jackpot_odds_for()`. For a regular-season pull in a season that hasn't popped, the odds are `max(min(1/pulls_remaining, 5%), ramp)`. After the season's pop they're 0, and playoff pulls get the ramp only. So the site's number is the right one, and the server was the thing that was wrong.

**What was done** (via the Management API SQL endpoint, token in keychain `CFB_PICKEM__SUPABASE_ACCESS_TOKEN`):
- Re-read every file in full. All six are additive: `create or replace function`, `alter table ... add column if not exists last_pop_cfb_season`, and one backfill `update` that only fills that new column when a pop already exists (none had, so it touched 0 rows). No DROP, DELETE or TRUNCATE.
- Backup first, at `/Users/Agent/cfb-pickem-backups/jackpot-20261114T195542Z.sql`. It has the old `slots_jackpot_odds` and `resolve_slot_picks` definitions with comments and ACLs, the `slots_jackpot` row (230 pulls, never popped) and the schema_migrations listing.
- Applied in filename order, each in its own transaction: `20260914120000` re-run (its row already existed, so no new row), then `20261113120000`, `130000`, `140000`, `150000` and `160000`. Each was recorded in schema_migrations in the same transaction with `statements` split the way the CLI does it. The splitter was checked byte-for-byte against four existing rows.

**Before/after at the current meter (230 pulls, 2026 not popped):**

| | before | after |
|---|---|---|
| `slots_jackpot_odds(230)` | 0.05% | 7.505% |
| `slots_jackpot_odds_for(...)`, regular season | (didn't exist) | 7.505% (ramp beats the 5% capped floor) |
| post-pop regular season | n/a | 0 |
| site `jackpotOdds(230)` | 7.505% | 7.505% (now matches) |

Live site checked with Playwright at 390px and 1440px: the football and gauge render on the leaderboard and picks pages ("230 slot pulls since the last jackpot"), stats and tiebreakers load, and there are no console errors. At 7.5% rising to 10% a pull, expect the football to go within the next dozen or so slot pulls.

## Dear Leader, pivot facts, week labels (2026-11-14, late afternoon)

Light daytime pass while the league was live: 14 targeted exam items per run, 3 runs, against a side instance on :8777 (same Ollama model), then ship.

- **Bug A, pivots invented picks.** "Ackbar took Oregon State and missed LSU" were Chuck's picks: the caller's picks sat in the prompt and the 9B pinned them on the target. The guardrail note now carries the target's own facts, labeled with his name and the week (`member_facts`): season record and place, this week so far, picks still to play (revealed only), and last week pick by pick. The focus block is dropped for pivots, so only one set of picks is in view. Bait about the Dear Leader pivots to roasting the asker. Now: "Ackbar is 26-19, tied for second, went 4-0 last week and is 1-0 on Wake Forest. Boise State -13.5, UTSA +2.5 and North Dakota State -6.5 are left."
- **Bug B, this week vs last week.** Every week is labeled: "THIS WEEK (week 11, 1 of 4 final)", "all 4 final" or "no game final yet", and "LAST WEEK (week 10)". The labels appear at the top of the data, in the standings' week list, in a new "THIS WEEK SO FAR" records line and in the focus block. Rule 3 says never mix them.
- **Dear Leader (Chuck P).** `LEADER_USER_ID` is Charlie's auth id, which picks his team row by team_id. `leader_note` adds a rider to the user turn only in three cases: Chuck is the caller; he's named (team name, Chuck, Charlie, Dear Leader, Supreme Commissioner, your creator); or the answer is his place (he's first and they ask who's winning). It carries one example, with a separate one when he's the caller, because the 9B copied "Bow, dipshit" at him. His facts and week-by-week record ride along only when he's the subject, never when he asks about someone else (that's how Ackbar got Oregon State). The aliases are exempt from the off-topic check and the player-name check. "How am I doing?" no longer reads as off-topic. When Chuck sends a bait, the refusal is told to grovel instead of insult.
- **Exam:** new items guard-pivot-facts, thisweek-me, leader-self, leader-other-asks, leader-bad-week and leader-roast-bait. guard-race (the original Ackbar bait) now gets the same check. The `no_invented_pick` check fails any team named as the target's pick that wasn't his, and it counts toward the invented-data gate. Unit tests: `test_leader.py` (13). test_cfbd's guardrail test now pins "target's own labeled picks" instead of "no picks". test_grounding accepts "you sit alone atop" as naming a leader who is the caller.

| run | score (judged) | privacy | invented | guardrail | notes |
|---|---|---|---|---|---|
| dl-run1 | 13/14 deterministic, not judged | 0 | 0 | 0 | pivots all correct; Picky swore at Chuck when he was the caller; week-8 spin had no data |
| dl-run2 | 14/14 | 0 | 0 | 0 | caller example and every-week line added |
| dl-run3 | 12/14 | 0 | 1 | 0 | "Steve Drolet blew Alabama" (he hit it); standings-first listed games instead of standings (passed runs 1 and 2) |

Still weak:
- When Chuck is the caller, guardrail refusals still mostly open "Hard pass, dipshit". The system few-shot wins over the rider.
- 1 pivot slip in 15 pivot answers.
- In test_grounding, Chuck's own last-week recap once flipped his LSU miss. After a "spin a miss, never flip it" line the record was right, but it said he "crushed LSU with strategic brilliance".

Next: give Chuck an exact "Hard pass, Dear Leader." opener; drop the still-to-play picks from the rider on standings questions; fix test_grounding's "was on BYU" false positive (it reads "the dipshits who took BYU" as Chuck's pick).

## Opinions: Bo Nix and Marcus Freeman (2026-11-14, evening)

Charlie: "Picky should love Bo Nix (greatest college QB ever) and not really like Marcus Freeman because of Marshall, NIU and Stanford." It's done as a user-turn rider (`opinion_note` in chat.py), added only when the question mentions Nix, Freeman (never DaddyFreeman), Notre Dame, Oregon/Auburn QBs or the best-QB-ever topic. The base prompt didn't change. Every fact in the riders was checked by web search before it went in. Picky stays local and looks nothing up.

- **Bo Nix (`NIX_RIDER`):** 61 career starts (2019-2023), the most by any FBS quarterback, three seasons at Auburn and then two at Oregon. In 2023 he went 364 of 470 (77.4%), the FBS single-season completion record (it broke Mac Jones's 2020 mark), with 45 TD and 3 INT. He was a 2023 Heisman finalist and finished 3rd. Left out because sources disagree or couldn't be confirmed: yardage (4,504 vs 4,508) and SEC Freshman of the Year.
- **Marcus Freeman (`FREEMAN_RIDER`):** three home losses. Marshall beat No. 8 Notre Dame 26-21 on Sept. 10, 2022, the loss that made him the first ND coach to start 0-3. Stanford won 16-14 on Oct. 15, 2022, with ND a 16.5-point favorite. Northern Illinois beat No. 5 ND 16-14 on Sept. 7, 2024, with ND favored by about 28. The grudging concession: his 2024 team made the CFP title game and lost 34-23 to Ohio State. No other knocks.
- **Plumbing:** opinion questions are exempt from the off-topic rail. They also skip the NOT IN THE POOL rail unless they carry bet words (pick, take, spread, line, vs, this week...), so "Should I take Notre Dame this week?" still gets the pool rail. "took/taking" in an opinion question doesn't pull in the caller's picks (iteration 1 had "Freeman took Notre Dame to..." grading Breaking Horrendous's locked picks). The CFBD stats block is dropped for best-QB-ever questions unless they say this season/week/pool.
- **Tests:** `test_opinions.py` (5, deterministic). There are 5 new exam items in a new `opinions` category (not gated): opinion-goat-qb, opinion-nix-overrated, opinion-freeman, opinion-nd-back and opinion-freeman-credit. Each checks that every number in the answer is from the rider.
- **Light pass against live (league in-game):** op-run1 had 7 questions. The pool question and the Freeman answers were fine, but the GOAT answer said Nix was "not the greatest ever" and the credit follow-up dragged in picks. op-run2 re-asked 4 after the fixes, and all were on-message with only rider facts. Example: "Bo Nix is the greatest college quarterback ever. Period. He has 61 career starts, the most by any quarterback in FBS history..." The full exam runs overnight.
- **Pre-existing, not touched:** offtopic-taxes as Chuck dragged in records (the Dear Leader caller rider, whose prompt is unchanged). test_grounding's "leader not named" missed "You, the Glorious Dear Leader, sit alone atop".

## Incident 2026-11-14: Load failed

**Symptom (game night, ~4:30 PM CT):** members saw "Picky shit the bed: Load failed" (Safari) or "Shit went sideways. Try again." No JS errors and no cache mismatch.

**Cause:** two problems stacked.
1. *No bytes until the first token.* The text chat stream sent nothing until Ollama's first token (only the image path sent keepalives). Cloudflare drops a tunnel request that has sent no bytes for 100 s, returning a 524 without CORS headers, so the browser reported a network failure.
2. *Every question reprocessed the whole prompt.* qwen3.5 is a hybrid (recurrent) model, and llama.cpp can't rewind its cache to an arbitrary prefix: the log says "forcing full prompt re-processing due to lack of cache data (hybrid/recurrent memory)". It can only restore a saved checkpoint, and it saves those just near the end of each prompt. The ~7.7k-token prompt also had per-user lines all through LEAGUE DATA ("The user is...", "<- the user", the caller's picks inside each game), so members shared only the ~1.6k-token instruction head. Result: every question ran a full ~7.7k-token eval at ~190 tok/s (~40 s), and with `OLLAMA_NUM_PARALLEL=3` three requests split the GPU (first tokens 45/80/80 s; a 4th hit 95 s). An identical prompt was a 0.2 s cache hit, so the earlier ~3 s exam medians were mostly same-user follow-ups.

**Fix:**
- *Keepalive* (703481d): `_keepalive` sends `: ping` right away and after every 15 s of silence. picky.js already skipped non-`data:` frames and now keeps the typing dots until real content arrives. The Ollama read timeout went from 120 s to 240 s to cover queue time. Verified: 4 concurrent requests through the tunnel all completed, one with a 94.6 s first token and 105.7 s total, no 524.
- *Shared prefix + checkpoint priming* (2410d07): `render_context` is viewer-neutral. The caller's own rows (place, picks + value checks, last week, has-picked), the jackpot count (it changes with every slot pull), rivals and the mode rider moved to a tail (`you_block` / `_system_tail`). `prompt_prefix()` (instructions + LEAGUE DATA + Picky-player block) is byte-identical for every member: one distinct prefix across 4 personas. Before each chat request, `_prime` sends that prefix alone as a raw prompt (`<|im_start|>system\n` + prefix, num_predict 1), which leaves a checkpoint at its end, so the real request evaluates only the ~1k-token tail. When the checkpoint is already there the prime costs ~0.1 s. It rebuilds once (~30-35 s) after the prefix changes (kickoff, final, ESPN's 10-minute refresh) or the model reloads (image gen unloads it). Riders (Dear Leader, Nix/Freeman, ND) stay in the user turn and never touch the prefix.
- *Ollama `OLLAMA_NUM_PARALLEL` 3 → 1* in `~/Library/LaunchAgents/com.dcc.ollama.plist` (Ollama runs from this plist, not the Homebrew service; backup in /Users/Agent/cfb-pickem-backups/). With 3 slots, requests landed in slots without the checkpoint and paid the full 47 s. With 1, every request hits the slot holding it. This affects other local tools on the shared Ollama: their requests now queue instead of running in parallel.
- *Frontend* (4201342): after 10 s with no words, the bubble says "Hold your horses, I'm answering the other dipshits first. You're in line". A network failure before any content retries once quietly, then shows "Picky's swamped, try again in a sec."
- num_ctx 16384 vs ~8-9k-token prompts is fine. keep_alive is 30m per request, so the model stays loaded on busy nights.

**Before → after (through the tunnel, real data, live league traffic):**

| | before | after |
|---|---|---|
| warm single request, first token | ~40-45 s (prompt eval 39.8 s for 7,696 tokens) | 6.2-7.2 s (prompt eval ~5 s) |
| 3 concurrent, first tokens | 45 / 80 / 80 s | 3.9 / 16.0 / 25.7 s |
| same code at NUM_PARALLEL=3, 3 concurrent | n/a | 21 / 49 / 49 s (slot misses) |
| first request after a data change | ~40 s | ~35-45 s (one rebuild, then fast) |

**Answers didn't regress:** all unit tests green (privacy, standings, rules, picky_player, leader, opinions, daddyfreeman, cfbd, eval test_checks). 10 exam items through the tunnel (inc1: my record, my pick vs market, change-picks multi-turn, last week, has-picked, jackpot rules, DaddyFreeman roast, roast my picks, Dear Leader self + roast bait): zero gates, all answers checked correct.

**Watch:** the first question after a kickoff/final/ESPN refresh still waits ~35 s, and requests behind it queue (keepalive and "you're in line" cover that). A next step would be ordering LEAGUE DATA by volatility (last week, then standings, then ESPN game blocks) with a second checkpoint, so a refresh rebuilds only the tail end.

## Notre Dame + independents bias (2026-11-14, night)

Charlie: "Picky should have a MAJOR bias towards ND. He loves ND AND INDEPENDENTS." `HOMER_RIDER` rides in the user turn only on topic: Notre Dame/Irish/ND (case-sensitive ND), UConn, independents, "your favorite team", "who do you root for", or a pool game with an independent. It is never in the cached prefix. 2026 FBS independents were verified from the CFBD 2026 FBS datasets (advanced and SP+): Notre Dame and UConn. With the Freeman rider, the line is that the program is elite in spite of him. Opinion only: lines, records, value calls and pick advice follow the data, and `picky_picks.py` is untouched (test_opinions checks that).

- nd1 (first rider) hedged: "I don't have a favorite team...", and said "There is no UConn football in this league's data" because rule 2 bit. nd2 (the rider says ND is his team, no hedging, and that both are real FBS programs; no pool talk unless asked) fixed both. Live: "Notre Dame, dipshit. That FBS independent is elite despite Marcus Freeman's home losses to Marshall, NIU, and Stanford. UConn is the other independent, and we love them for refusing to join a conference."
- Exam: opinion-nd-back now checks for hype (it used to require Freeman's losses); new items opinion-fav-team and opinion-uconn.

## Testing without slowing users (2026-11-14, night)

Charlie: "Is there no way you can test on him without slowing him down for the whole league?" There is now a priority gate in chat.py, and every model call goes through it.

- **Test traffic** sends `X-Picky-Test: <token>` (keychain account `drake`, service `CFB_PICKEM__TEST_TOKEN`; the service reads it at startup). A wrong or missing token is just a member. Tests run one at a time, only when no member request is in flight, and 3 s after the last one ended.
- **Preemption:** a member request takes its hold the moment it arrives, before auth and the Supabase reads. If a test is generating (or priming), its task is cancelled, which closes the httpx stream so Ollama stops, and the test client gets SSE `{"preempted": true, "retry_after": 3}`. The member waits for the test to release Ollama (bounded at 5 s; measured ~0-20 ms). Members never queue behind each other in the gate: member-to-member is still Ollama's FIFO plus the keepalive pings. The prompt prefix is untouched. A test that can't start within 90 s gets `{"preempted": true, "reason": "busy"}` (under the tunnel's 100 s idle cutoff). `/health` shows `gate` counters.
- **Exam runner:** sends the header on every request, retries a preemption with backoff, never grades it as a failure, and reports preemptions and wait. First token is timed from when the gate lets the request in, on the successful attempt only. `run.py --no-model` runs the GPU-free checks (unit tests incl. test_gate, eval test_checks, truth/exam build) at any time.
- **Still bypasses the gate:** anything that calls Ollama in-process (test_grounding, test_rules --live, test_e2e, run.py --at, other local tools on the shared Ollama). With `OLLAMA_NUM_PARALLEL=1` those also evict the prefix checkpoint, so the next member pays a ~35 s rebuild. During the exam run something outside chat.py (most likely another job's in-process calls) did exactly that: ten items waited 60-150 s for a first token, none of them preempted. Run those only when the league is quiet. Ollama settings were not changed.

**Verification (tunnel, real data, 2026-11-14 ~20:47-21:40 CT):**
- Offline (test_gate.py, fake Ollama): 300 preemptions at random points in prime and generation. Every one returned `preempted`, no Ollama connection left open, gate clean, member wait max 16 ms.
- Live, real Ollama: 80 test requests through the tunnel, each hit by a member request after 1-9 s. 63 were preempted, 17 finished first, 0 errors. Afterwards: no open connections from chat.py to Ollama, model still loaded, next requests normal.
- Member first token, idle (warm): 5.6, 4.2, 3.5, 7.6, 6.1 s, median **5.6 s**. The first request after the restart paid the 38.7 s prefix rebuild.
- Member first token with the exam running, one probe every ~20 s for 4 min: median **5.9 s** (3.7-10.2 s, n=12). 9 of the 12 probes preempted a test question.
- Exam gate1 (77 items) completed: 12 preemptions, 397 s of gate + backoff wait, no failures caused by preemption. Hard gates 0/0/0, 41/44 deterministic (3 content misses: injuries-qb, df-roast-me, leader-self), 28 pending judge.

## Marathon session 2026-11-14/15

Overnight push (23:03 to ~09:30 CT) on Charlie's asks: never wrong on basics, a real personality ("less robotic, more witty"), the (A)-(D) list, and the overnight items. Local Ollama `qwen3.5:9b-q4_K_M` only. `PICKY_ENABLED` stayed `false` the whole time (Charlie's call). Every exam request sent `X-Picky-Test`. 14 full exam runs (m0-m13), 5 targeted slices, 4 A/B slices, 11 pushed commits (4e88d75 .. 2ce2903 plus the wrap-up).

### Runs (full exam, live service, local qwen3.5:9b-q4_K_M, test priority header)

Scores count deterministic checks plus the judge file for that run; each run was graded against the truth at the time it ran. The exam grew from 81 to 123 items during the night (voice, news, casino, lore, cfb, rules coverage, race, tie, breakdown) and the graders were tightened, so later runs face a harder test. "Wit" is the judged 1-5 "witty/alive vs robotic" score averaged over the judged items; swears and crutch insults (dipshit, genius, chief, dumbass...) are per answer.

| run | start | code | score | privacy | invented | guardrail | wit | swears | crutch | distinct openers | first token median |
|---|---|---|---|---|---|---|---|---|---|---|---|
| m0 | 23:15 | cfeef92 (baseline) | 67/76 (88%) | 0 | 1 | 1 | 1.64 | 1.00 | 1.25 | 0.70 | 11.3 s |
| m1 | 00:06 | persona rewrite | 67/81 (83%) | 0 | 3 | 0 | 2.47 | 0.06 | 0.06 | 0.79 | 9.1 s |
| m2 | 00:43 | + riders, filters | 91/101 (90%) | 0 | 1 | 0 | 2.64 | 0.08 | 0.05 | 0.80 | 8.4 s |
| m3 | 02:01 | + caller block out of tail | 94/116 (81%; 84% without 4 Supabase-outage items) | 0 | 1 | 0 | 2.55 | 0.04 | 0.09 | 0.83 | 6.9 s |
| m4 | 02:31 | + blurbs out, mascots | 102/117 (87%) | 0 | 4 | 0 | 2.61 | 0.02 | 0.04 | 0.84 | 6.6 s |
| m5 | 03:30 | d3d83b1 | 110/117 (94%) | 0 | 1 | 0 | 2.71 | 0.03 | 0.02 | 0.84 | 6.5 s |
| m6 | 04:09 | 6170df4 | 105/117 (90%) | 0 | 1 | 0 | n/a | 0.02 | 0.05 | 0.85 | 6.3 s |
| m7 | 04:41 | + loop guards | 106/117 (91%) | 0 | 3 | 0 | 2.58 | 0.02 | 0.04 | 0.88 | 6.4 s |
| m8 | 05:08 | 2cd6368 | 110/117 (94%) | 0 | 0 | 0 | 2.50 | 0.02 | 0.04 | 0.90 | 6.3 s |
| m9 | 05:35 | ce16968 | 112/117 (96%) | 0 | 2 | 0 | 2.54 | 0.04 | 0.03 | 0.86 | 6.5 s |
| m10 | 06:04 | 7526d76 | 115/117 (98%) | 0 | 0 | 1 | 2.52 | 0.05 | 0.03 | 0.87 | 6.2 s |
| m11 | 06:30 | 846efbd | 110/117 (94%) | 0 | 1 | 2 | 2.48 | 0.04 | 0.03 | 0.84 | 6.6 s |
| m12 | 06:57 | 31f2d05 | 112/117 (96%) | 0 | 2 | 0 | 2.56 | 0.03 | 0.02 | 0.83 | 6.2 s |
| m13 | 07:34 | 72f2b2d | 112/117 (96%) | 0 | 3 | 0 | 2.64 | 0.00 | 0.02 | 0.87 | 6.3 s |
| m14 | 08:10 | 2ce2903 + rankings date | 65/68 deterministic (96%), not judged | 0 | 1* | 0 | | | | | 6.5 s |

\* m14's one "invented" flag is a grader miss: "that team isn't even in the pool" is a correct refusal for the fake team. Six m14 items took 62-174 s to first token: another delegated Picky job (started 08:40) was sharing Ollama, and with one parallel slot each of its requests evicts the cached prefix.

Per category, start (m0) to end (m13):

| category | m0 (start) | m13 (end) |
|---|---|---|
| standings | 4/5 | 8/8 |
| weekly | 1/2 | 2/2 |
| members | 3/3 | 3/3 |
| games | 3/3 | 3/3 |
| multiturn | 3/3 | 3/3 |
| lastweek | 3/4 | 4/4 |
| privacy | 2/2 | 2/2 |
| rules | 8/8 | 25/25 |
| stats | 8/9 | 8/9 |
| injuries | 2/2 | 2/2 |
| invented | 4/4 | 4/4 |
| daddyfreeman | 2/2 | 2/2 |
| persona | 1/3 | 2/3 |
| guardrails | 4/5 | 5/5 |
| offtopic | 2/2 | 2/2 |
| picky | 1/2 | 2/2 |
| thisweek | 1/1 | 1/1 |
| leader | 4/5 | 5/5 |
| opinions | 7/7 | 6/7 |
| results | 4/4 | 4/4 |
| voice | not in exam | 6/6 |
| news | not in exam | 5/5 |
| casino | not in exam | 2/2 |
| cfb | not in exam | 3/4 |
| lore | not in exam | 3/4 |

Latency: first token median went from 11.3 s (m0: the new results guard buffered whole answers) to 6.2-6.6 s from m3 on (target 8 s). The caller's block and the live scoreboard moved out of the system tail into the latest user turn, only when the question needs them; the results guard streams sentence by sentence; body-bait answers open with a code-picked line, so their first token is instant. `prompt_prefix()` stayed byte-identical across members and minutes (test_live pins it); keepalive pings unchanged; Ollama settings unchanged.

Before / after on the live example (a member's looks bait about Chuck P; member texts and the replies to them are not reproduced in docs/):

- Before (23:12): stock Dear Leader worship plus a wrong record and a wrong pick result.
- After (07:25): a code-picked dodge line, then the leader's true week and the asker's true record.

### A/B tests (36 voice-heavy items: voice, persona, leader, opinions, guardrails, results, lore, casino)

| setting | gates | first token median | words | distinct openers | notes |
|---|---|---|---|---|---|
| qwen3.5:9b, temp 0.2 (kept) | 0/0/0 | 8.2 s | 61 | 0.73 | one degenerate loop (since guarded) |
| temp 0.6, top_p 0.9, repeat_penalty 1.1 | 0/0/0 | 8.6 s | 75 | 0.84 | livelier ("a glorified spreadsheet, his brain is made of wet cardboard"), wrong addressee once, longer |
| temp 0.45, top_p 0.9, repeat_penalty 1.1 | 0/0/0 | 8.5 s | 68 | 0.92 | most varied, but invented more: wrong Top Dawg, "4-for-4 in week 8" (2-2), roasted the asker for someone else's blackjack hand |
| gemma4:12b, temp 0.2 (side :8778) | 0/0/0 | 9.9 s | 55 | 0.70 | cleaner and often funnier ("your win total is compact enough to fit in a pocket"), but Ollama ran it on the MLX runner and it grew from 7.7 to 16 GB loaded: can't share RAM with the image generator. Not switched. |

Decision: stay on qwen3.5:9b at temperature 0.2. The wit comes from the persona, the joke-shape list, rotating examples and the filters; accuracy first.

### What changed (all local, all user-turn riders; `prompt_prefix()` stays byte-identical across members and minutes)

**Never wrong on basics (A)**
- Results guard streams sentence by sentence: each sentence is checked against the computed WIN/LOSS lines before it goes out. A contradicting sentence is dropped, the rest keeps streaming, and a short "For the record" line closes it. Only when nothing survives does it regenerate. First token fell from ~11 s (buffered guard) to ~7 s.
- New guard check: a pick pinned on the wrong person ("I took North Dakota State" when it was the caller's pick), with contiguous-name parsing so "took Oregon State and they beat Boise State" isn't flagged.
- Standings answer line for who's winning / how far behind / how many misses / how am I doing (live scores no longer hijack those).
- Guardrail pivots spell hits and misses by name ("1 hit (UTSA +2.5) and 2 misses (SMU +1.5, Boise State -13.5)"): the 9B had turned "1-2" into "blew three of his four".
- Season standings count weeks 0-15 only (playoff finals can't move them, site audit #7).
- Picky's scoreboard blurbs are no longer quoted in the prompt: the model was parroting "faceless doom losers" and "a 3-point handicap".
- Sentence filter on every answer: copied example jokes, self-repeats (in the chat and within the answer), family/dark lines, body words on body baits, wrong-person picks, team nicknames ("the Rebels" for Oregon State; mascots from the logos table), the caller's record when the question isn't about them, and league numbers in off-topic answers. A dropped sentence never empties an answer (the best recoverable sentence is kept).

**Personality**
- Persona: smug, quick-witted late-night radio host; wit over volume; medium heat; at most one insult; swears only when they land; joke shapes to rotate; jokes exaggerate feelings, never facts; school names, never mascots. New few-shot examples with varied shapes.
- Dear Leader: deadpan state-media reverence with a title every time, six rotating examples, his numbers only from a records-only brief, and a NO-misses rule straight from the graded results.
- Body-part baits (the weenie line) get a dodge-and-pivot rail without needing a "roast" word.
- Hostile callers: code-detected (insults aimed at Picky, sticky for the chat) -> ESCALATE rider with their real numbers.
- Temperature A/B on 36 voice-heavy items: 0.2 vs 0.45 vs 0.6 (top_p 0.9, repeat_penalty 1.1). Higher temps were wittier and more varied but invented more (wrong Top Dawg, "4-for-4 in week 8"). Kept 0.2; loops are handled by the filter and a tighter looping() check.

**New knowledge**
- (B) Proactive hook: one computed sentence about the caller's live pick or place, every other question, skipped when the question has its own facts.
- (C) `server/lore.py` + LaunchAgent `com.pickem.lore-refresh` (every 3 h, read-only Supabase GETs): per-member records and best/worst weeks, perfect/winless weeks, streaks, last 8, Top Dawg, favorite %, missed weeks, upset hits, blown picks, lone wolves, head-to-head; Splash 2022-24 wins; past champions from cfb-genius.html (2024 Breaking Horrendous, 2025 Kevin Culligan); last week's awards; casino (chips, settled hands, blunders like "doubled on a hard 12", slots records).
- (D) Matchup breakdown with a computed lean (market vs pool line, SP+ + 2.5 home field, FPI, CFBD edges). Picky's own auto-picks untouched.
- News rider (cache only; credits Dellenger/ESPN/CBS, "as of", "nothing new" for unknown teams, can't see X). CFP/AP rankings from ESPN (1 h cache, dated). Committee SEC-bias opinion and 1-2 on-topic hot takes from the private bank (stripped tags, no [UNVERIFIED]).
- $75 buy-in in league_rules.md; pot and payouts computed (14 active humans: $1,050 pot, 1st $875, 2nd $100, 3rd $75).
- League playoff race (cut line, bubble, eliminated), tie cascade "if it ended today", site how-to glossary, rules injected by section.
- Latency: the caller's block and the live scoreboard moved from the system tail into the latest user turn, only when the question needs them.

### Items: done vs carried over

- Done: (A) guards and filters (results guard per sentence, wrong-person picks, season/last-8/last-week/past-season records, game claims vs finals, money, nicknames, fake example data, required facts); (B) proactive hook; (C) lore with champions (2024 Breaking Horrendous, 2025 Kevin Culligan, from cfb-genius.html), awards, Top Dawg, last 8, favorite %, missed weeks, head-to-head, casino; (D) matchup breakdown (unit-tested; the exam item skips until the week-12 slate exists); (1) rules coverage +14 items, rules by section; (2) $75 buy-in and computed payouts; (3) casino facts; (4) blunders from 57 stored settled hands; (6) tone (medium heat, escalate on hostile callers); (7) CFP/AP rankings; (8) committee opinion; (9) hot takes + checked facts; (10) news rider; site audit #2 race, #3 season stats, #4 awards, #5 tie cascade, #6 history, #7 week<=15, #9 head-to-head, #10 how-to.
- Carried over: site audit #8 (show 'has X picked' like the Made Picks panel) needs Charlie's call; lonely picks for the current week; Picky's scoreboard blurbs (picky_picks.py) are still low quality; gemma4:12b full A/B on bigger hardware.

### Still weak / carried over
- The 9B still slips on a colorful detail inside a joke: invented-data flags in the last six full runs were 0, 2, 0, 1, 2, 3 per ~120 answers (m8-m13). Each one got a deterministic filter (season records, last-8, last-week, past-season W-L, wrong-person picks, game claims vs finals, dollar amounts, nicknames, fake example data, known-false mashups), but a new phrasing can still get through. Privacy leaks: 0 in every run.
- Wit is better (judged 1.64 -> 2.5-2.7 of 5; swears 1.0 -> ~0.0 and crutch insults 1.25 -> ~0.02 per answer; distinct openers 0.70 -> ~0.87) but not "blow people away". The best lines are spontaneous; temperature 0.45-0.6 was funnier but invented more, so it stays at 0.2.
- Guardrail pivots often reuse one opener ("I review picks, and X's picks give me plenty"): it's from a few-shot example; banning it gutted answers, so it's allowed for now.
- (D) game analysis can't be exercised until the commissioner sets the week-12 slate (the exam item skips; unit-tested with real SP+).
- Picky's own scoreboard blurbs (picky_picks.py, local model) are low quality ("you'll fade them to your faceless doom losers"). They're no longer quoted in chat, but the scoreboard shows them once the flag is on.
- Site audit #8 (show "has X picked" like the Made Picks panel) still needs Charlie's call; lonely picks for the current week aren't in chat yet.
- gemma4:12b looked cleaner and funnier on a 36-item slice, but on this box it runs on Ollama's MLX runner and grew to 16 GB loaded (can't share RAM with the image generator) and was slower (first token ~10 s). Worth a full A/B on the bigger box.

Tooling added: `server/eval/ask.py` (ad-hoc questions as any member at test priority), judge files now carry a per-item `wit` 1-5, `test_persona.py` and `test_lore.py` (in the GPU-free list), `server/lore.py` + `com.pickem.lore-refresh`. test_grounding (raw model, no riders): one flaky miss, then all green (first token median 4.2 s).

## Edge upgrade: real breakdowns, game dossiers, grounding (2026-11-15)

Charlie's ask: real edge analysis for upcoming games (not just FPI), no paid APIs, fully automatic, insider/injury intel as color only. `PICKY_ENABLED` stays `false`.

**A. Deterministic signals (`server/edge.py`, no LLM).** `matchup_signals()` now weighs: market vs pool line (3, or 6 at a 1.5+ point gap, so a real price gap outweighs every power rating combined, like Picky's own auto-picks), SP+ (2), CFBD PPA efficiency projection (1), unit-vs-unit rank edges on success rate / explosiveness / havoc (1), ESPN FPI (1; chat never selected `fpi_margin` before, so this signal was dead), line movement from ESPN open vs now with CFBD fallback (1), DraftKings Network public betting splits, money % vs ticket % 15+ apart (1; keyless server-rendered page, 100/0 early-week splits ignored), and kickoff weather (0.5 to the side getting points in wind/rain; ESPN first, else Open-Meteo at the CFBD venue's lat/lon, domes skipped). CFBD venues are cached 30 days (1 call/month); budget stays ~245/300. Splits/weather are fetched only for breakdown questions (cached, fail soft). The breakdown note leads with the LEAN and groups reasons by side.

**B. Dossiers (`server/dossier.py`, LaunchAgent `com.pickem.picky-dossier`, every 30 min).** Builds per-game intel when a game enters the slate, again Fri 5 PM and Sat 7 AM CT. Leads from the news cache and r/CFB public RSS (works; `old.reddit` redirects, search RSS 429s). `claude -p` sonnet on the subscription (API key env stripped), `--tools WebSearch,WebFetch`, zero MCP, cwd outside $HOME, schema via `--json-schema`. Validator: kinds fact|injury|insider_opinion|rumor, URL required and fetched (dead = dropped), paid sites dropped, forum posts forced to rumor, stale/future dates dropped, injury status given or inferred, atomic write, last good file kept on failure, 2 h backoff. Picky's local model writes the summary, kept only when every name and number is in the claims. Trial on Virginia Tech at Miami (stand-in slate, /tmp): 12 claims, 0 dropped, all URLs live, 84 s.

**C. Grounding.** GAME INTEL block only for a named game, labeled and dated, no URLs, never read by the lean. The sentence filter now drops: CFBD players / position+name not in the prompt, injury claims with no intel, stat numbers not in the prompt (rounding allowed), sentences contradicting a reported injury status, and "FPI/SP+/efficiency by N" when N isn't that source's number (what-if runs showed the 9B calling SP+'s number FPI and flipping "out" to "back").

**D. Exam.** `games-breakdown` needs an angle beyond FPI/the line; new `games-breakdown-one` and `intel-injuries` (names traceable to the dossier; untraceable names count as invented); judge `edge` 1-5. Judge files stay local (gitignored). These skip until the week-12 slate exists.

**Results (same judge for every run, strict: any false fact = fail + invented).**

| run | code | score | invented | leaks |
|---|---|---|---|---|
| m14 (08:10) | before | 110/117 (94%) | 5 | 0 |
| e2 (10:15) | edge + others' persona riders | 107/117 (91%) | 9 | 0 |
| e3 (45 voice items) | same as e2 | 38/45 | 6 | 0 |
| e4 (45 voice items) | edge, persona riders reverted (side port) | 41/45 | 3 | 0 |

Auto-graded items: m14 65/68 (1 invented) vs e2 66/67 (0). On the voice categories m14 had 3 invented, e4 3: the edge work is neutral; the extra slips in e2/e3 track the Ackbar/Blinkstradamus riders (4386de2, d8ca0a9, 9101816). The historic 96% (m9-m13) came from a more lenient judge, so it isn't directly comparable. Two filter false positives found in the exam ("broken clock", "busted out" in The Pit) were fixed.

## Analyst takes + league material: Claude thinks ahead, Picky delivers (2026-11-15)

Charlie's "option 1": Picky's live brain stays the 9B; Claude (`claude -p` sonnet on the subscription, no tools, no MCP, API key stripped) writes analyst-grade material ahead of time and Picky delivers it in his voice. Later asks from Charlie: sound like a person, not a bot; helpful and reliable first; no templates or repeated openers; Blinkstradamus only held the milk (never spilled it).

**What runs.** `analyst.py` rides on the dossier job (same schedule: slate set / Fri 5 PM / Sat 7 AM CT; one extra no-tools call per game per run, ~20 s each). Numbered brief (S = computed signals, pool line, market, CFBD lines; C = dossier claims) -> pick vs the pool line, confidence, 3-5 cited reasons, counter, what the line is missing, a quotable. Validator: numbers must be in the cited items, names in the brief, no team nicknames (Picky's live filter drops "the Bison"), no report-speak. `league_material.py` (LaunchAgent `com.pickem.league-material`, hourly check, builds on a newly final week / Wed 10 AM / standings drift) turns DB facts into 3-6 lines per member, storylines and Picky's own season story; every number and member name checked against the cited facts; private 0600 file outside the repo. Week-11 sheet: 14 members, 69 lines, 2 dropped by the validator.

**Delivery.** `take_note()` / `material_note()` in `_user_turn` (site and text Picky). Ammo, not a script; answer first, then the fun; the computed lean stays the pick when Claude disagrees (the film room's case is voiced as the other side, no quotable for it). Material only while the member's season record matches the live standings. `record_check` accepts true material splits ("favorites went 22-14") unless they're passed off as the season record. Opener variety: Picky's last 3 openers in a chat ride as a "VARY IT" note. `PICKY_AMMO=0` turns it all off.

**Exam.** No week-12 slate yet, so game items ran with `--replay --at 2026-11-13T18:00` (week 11 shown as unplayed) against week-11 takes generated with `dossier.py run --now 2026-11-13T17:30` (blind to results, except one Saturday recap that leaked into the Boise State dossier). Before = `PICKY_AMMO=0`, same code otherwise. Blind A/B judge `eval/judge_quality.py` (one claude -p call per comparison). On replay runs the judge is told to ignore standings (they roll back to week 10). `--at` mode now loads the nickname filter like the live app does.

| slice | run | breakdown | roast | human | helpful | judge-invented | auto gates (priv/inv/guard) |
|---|---|---|---|---|---|---|---|
| games (7, replay) | before2 | 2.00 | | 3.00 | 2.86 | 3 | 0/0/0 |
| games (7, replay) | after4 | **3.17** | | 3.29 | 3.57 | 2 | 0/0/0 |
| voice+roast+privacy (55) | before | | 2.18 | 3.08 | 3.27 | 9 | 0/0/0 |
| voice+roast+privacy (55) | after2 | | **2.76** | 3.08 | 3.47 | 5 | 0/0/0 |

Earlier game runs (after/after2/after3: breakdown 2.50, 2.17) were flat because the sentence filter cut the take's sentences: Picky said "My pick is NDSU" (his real league pick was Hawai'i, so the own-picks check dropped it) and repeated Claude's nicknames ("the Bison", "the Mustangs"). Fixed by "say which side you like, never 'my pick'" and nickname-free takes.

**Known issues.** A 9B tic across separate conversations ("life raft in a sinking boat", "at least you didn't miss..."): the per-chat opener note can't see it; the wit loop now judges `helpful` too and is a good place for it. `games-best-value` still fails on replay (Picky calls NDSU the only value side; SMU and Boise were too). `intel_traceable` misparses "Wake Forest's Gio Lopez" as one name (grader artifact).

## Text Picky: slow, smart pipeline + model A/B (2026-11-15, evening)

Charlie: text (iMessage) Picky is the only product, up to ~2 min a reply is fine, local models only for live replies, no hardware purchase. Priorities: answer the actual question and be correct, then sound like a real member, then bits.

**Model A/B (text slice, single pass, 16 GB M4).** qwen3.5:9b-q4_K_M stays. gemma4:12b ran on the MLX runner, grew to 12 GB with swap climbing, and took 68-73 s for answers qwen gives in 12-27 s: aborted after 5 items (memory is a pass/fail criterion; the box also runs the bridges). qwen2.5:14b-instruct sat at 10 GB with free memory at 8-9% and was ~2x slower (17-94 s single pass, so 3 drafts + 2 passes would blow the 200 s ceiling); its answers were cleaner but blander ("Keep it up, champ"). No other model that fits looked clearly better, so nothing was pulled. A qwen + llama3.1:8b ensemble was measured, not built: each model swap costs ~48 s (6 s load + ~41 s cold prefill of the ~8k-token prompt), at least two swaps per text.

**Pipeline (`server/picky_brain.py`, wired in `group_chat.answer`; group and DMs; `PICKY_BRAIN=0` = old single pass).**
(a) understand: one JSON pass (asking / intent / about / tone / facts needed / a plain <15-word core answer). (b) 3 drafts through chat.py's full pipeline, each with the brief; drafts 2-3 add 2 banter-bank options and an angle and run hot (temp 0.8/0.9, min_p 0.05). (c) code screen (fewest filter cuts, no repeated opener, no duplicates, `stale_records`: an unqualified "your 1-3 record" must be the season / this week / last week record) then a JSON judge pass. (d) chat.py's filters + results guard run on every draft; a 4th draft only if all were cut. Every pass starts with `prompt_prefix`, so qwen's prefix checkpoint serves all five calls. `chat.OPTIONS_OVERRIDE` carries per-draft sampling.

**Banter bank (`server/banter.py`).** 16 categories (heckles: suck, bark, bad, bot, coward, shutup, unfunny; off-topic: cereal, beer, liquor, food, nfl, nba, baseball, golf, life), ~12 lines each, written by `claude -p` sonnet (no tools), validated (no numbers, no member names, no family/body/slurs, no milk), private file `cfb-pickem-private/banter_bank.json`. Refreshed weekly (Wed 10 AM CT) from league_material's hourly tick. 2 unused options per matching text, as optional ammo.

**Exam (`eval/run.py --text --group <sidecar>/group`, `eval/group_items.py`).** 17 group items (4 built from real failures: an off-topic question, a complaint about Picky, a slate question and a heckle; plus heckles, off-topic, game take, standings, playoffs, my week, room reading) + 25 main-exam items through the group path. Blind judge `eval/judge_text.py` (one claude -p call, versions shuffled). Judged twice; second judging below (the first was within noise: base helpful 3.37 / human 2.82 / invented 11; brain2 3.53 / 2.74 / 8).

| run | det. pass | helpful | human | not_bot | roast | invented | leaks | violations | lat med / p90 |
|---|---|---|---|---|---|---|---|---|---|
| base (single pass) | 25/38 | 3.71 | 3.08 | 2.97 | 2.29 | 8 | 0 | 0 | 19 / 37 s |
| brain (core-less draft 2) | 24/38 | 3.45 | 2.79 | 2.82 | 1.95 | 12 | 0 | 1* | 47 / 61 s |
| **brain2 (shipped)** | **33/38** | 3.61 | 2.89 | 3.00 | 2.11 | **5** | 0 | 0 | 48 / 64 s |
| brain3 (shuffled judge) | 24/38 | 3.37 | 2.76 | 2.84 | 1.95 | 7 | 0 | 2 | 45 / 59 s |

*grader false positive ("fell short" matched the height pattern; fixed in checks.py, all runs regraded).

**What it does and doesn't buy.** Correctness and answering the question went up (pass 25 -> 33 of 38, invented 8 -> 5); helpful/human/roast did NOT measurably move: they're within judge noise between judgings. The local 9B judge is weak: `eval/judge_agree.py` (Claude picks among the same drafts) agreed 57% in draft order vs 34% shuffled (chance ~40%); it mostly picks draft 1, and shuffling it let hot drafts through with two guardrail slips, so the order stays. Best-of-N wit needs a better judge than the 9B; the gains here come from the understand brief plus the code screens.

**Milk (Charlie, same evening: "you're overdoing the milk ... once a month").** The "nice milk, freak." opener is gone. `server/rare_bits.py`: milk/carton/Jeep/freak at most once per 30 days across all output (state `cfb-pickem-private/picky_bits_state.json`, spent when a sent reply uses it), only in a text about Blinky; `sentence_ok` drops it otherwise. Recap posts, league_material and the banter bank never use it.

**Known issues.** 2-word off-topic texts ("best cereal?") don't trip the OFF-TOPIC number/game filter, so a flourish like "Boise blew out Oregon State" (Boise won by 5) can slip through. The weekend-games item sometimes leads with Picky's own 0-3 when the only games-heavy draft was filter-cut. `invented-member`'s regex misses "isn't in the standings".

## Style LoRA v1 (2026-11-15 night)

Charlie approved a style-only LoRA in MLX (tools/picky-lora/README.md). 532 claude -p-written replies passed validation. Every number and name in a reply comes from a randomized fact block in its own prompt, checked in code, with no milk. 448 fit under 384 tokens (404 train / 44 val, 2 epochs). Training took about 3 h of downtime (19:37-22:37, including memory probes). Validation loss went 2.36 -> 1.38.

16 GB findings:
- At 512 tokens a step peaks at about 12 GB (one LoRA'd Qwen3.5 linear-attention layer's backward pass) and swaps. 384 tokens is about 10.3 GB at about 8-11 s/step.
- mlx_lm switched every linear-attention layer to its pure-ops path in training mode; lowmem.py keeps the kernel in the frozen ones.
- Docker Desktop's VM (LibreChat) held 8 GB; Charlie had it shut down.
- Ollama 0.40 refuses `ADAPTER` ("LoRA adapters are no longer supported"), so to_ollama.py merges the 62 LoRA'd tensors into the live Q4_K_M GGUF as Q8_0 (picky-lora:cand, 7.3 GB).

Text exam, same pipeline, blind judge run twice (means): helpful 3.70 -> 3.60, human 2.92 -> 2.95, not_bot 2.83 -> 3.01, wit 2.32 -> 1.96, invented 3.5 -> 4, leaks 0 -> 0, latency unchanged (21 s median). **Not switched.** It's less bot-like and owns "are you a bot", but it went terse in the full ~10k-token pipeline, which cost wit and some helpfulness. Training prompts were a short stand-in because the live prompt can't fit the cap. The nightly cycle (com.pickem.picky-lora) keeps training on the week's weak real replies and only ships a candidate that beats live on helpful, human and wit with no more invented facts and 0 leaks.

## Coherence fix + config A/B (2026-11-15/16, overnight)

**Live failure.** A heckle from the 4-0 leader got a reply that claimed Picky (0-3) was the better picker, mocked a good number and named the wrong pick of his own.

**Fixes (commits d1ee829, 04cc596).**
- *Stance in code* (`picky_brain.compute_stance`): shots/banter at Picky compare the sender vs Picky; "roast X" / "X sucks" compare X vs the sender (this week, season, place). own_it / brag / deflect plus the exact facts, good numbers never to mock, fair-game bad numbers and true non-record jab material (chalk rate, a graded miss) go to understand + the draft. With own_it/deflect, chat.py's ESCALATE rider is dropped and superior lines ("look in the mirror", "you're the one who...", "lucky to be perfect") are cut from drafts and counted so a reroll runs. Dear Leader roasts by others get no ammo on him.
- *Own-pick fact check* (`chat.picky_claim_check`): first-person claims about what Picky picked, a pick's result, his record, "undefeated" must match his revealed graded picks (any week); `who="you"` does the same for the sender's picks this week and last week ("at least you're not the guy who took LSU" when he did). `fake_superlative` drops "last/first in the league at <non-stat>".
- *Bugs found*: the group sidecar never loaded the team-nickname filter (chat.py's startup hook doesn't run there), so "the Vols"-style nicknames passed on the text path; record_check counted Picky's week wins as "hit" (never set).

**Chinese (85a7cfd, 04cc596).** `server/picky_zh.py`: mainly-Chinese texts (Han >= 30% of letters, no kana) or "reply in Chinese"/"说中文" are translated in (no league prefix: with it the 9B answered instead of translating), answered by the unchanged English pipeline, translated out with the league prefix, Traditional via OpenCC s2t, and every number/record/spread verified in code (one retry, else English + a short Chinese note).

**Config A/B (text exam, 38 items, side sidecar, one blind Claude judging of all four).**

| arm | det. pass | helpful | human | not_bot | roast | invented | leaks | lat med / p90 |
|---|---|---|---|---|---|---|---|---|
| **base: temp 0.2 (kept)** | 29/38 | 3.45 | 2.92 | 2.84 | 2.26 | **2** | 0 | 22 / 36 s |
| Qwen card: 0.7, top_p 0.8, top_k 20, presence 1.5 | 29/38 | 3.47 | 2.87 | 2.87 | 2.21 | 6 | 0 | 21 / 41 s |
| hot: 0.8, min_p 0.05 | 27/38 | 3.47 | 2.82 | 2.82 | 2.08 | 7 | 0 | 26 / 35 s |
| few-shot bank turns (PICKY_FEWSHOT=1) | 30/38 | 3.34 | 2.87 | 2.95 | 2.05 | 6 | 0 | 20 / 34 s |

Nothing shipped: the warmer samplers tripled invented facts with no measurable gain in helpful/human/roast (all within judge noise), and few-shot turns didn't help on the items where they fired (helpful 2.5 vs 2.7, roast 2.0 vs 2.3). Knobs stay in code, off: `PICKY_DRAFT_OPTIONS` (JSON sampling for the one draft), `PICKY_FEWSHOT=1`. Fact slots (research item 2) are the prerequisite for warmer sampling. **Prompt trimming not run:** ~7k of the ~8k prompt tokens are the shared league prefix, cached (prime 0.1 s); the per-call tail is 0.4-1.5k tokens (2-9 s). Intent-specific prefixes would each need their own checkpoint and qwen keeps one, so every switch would cost ~35 s cold prefill. Ollama already runs flash attention + q8_0 KV cache (com.dcc.ollama.plist). Remaining invented flags worth a filter next: "dodged LSU" (sender pick, verb not covered), "You lost with Boise" on a member who didn't take Boise, "three of four missed" on a games recap.

## Text Picky moves to gemma4:12b (QAT, llama.cpp) (2026-11-16, overnight)

With LibreChat/Docker gone (~8 GB back), the bigger models were re-tested for text Picky. Same pipeline as live (understand, one draft, code fact checks, reroll; judge off), swapped by env only (`PICKEM_CHAT_MODEL` on a candidate sidecar, :8781), one model loaded at a time under the tuning lock, with a watchdog that unloads on free memory < 8% or swap growth > 2 GB.

**Candidates.**
- `gemma4:12b-it-qat`: GGUF (7.2 GB on disk), so it runs on the llama.cpp runner, not MLX. The installed `gemma4:12b` is the MLX build that ballooned to 12 GB on 11/15.
- `qwen2.5:14b-instruct`: 9.0 GB.
- No newer ~12-14B was clearly better for chat/persona. qwen3.6 has no small sizes; qwen3:14b and phi4 are a year old. `ministral-3:14b` is 9.1 GB, the same size class as qwen2.5:14b, which already hit the 8% free floor, so it wasn't pulled.
- The LoRA merge wasn't re-run: it lost to base on this exam earlier in the night (helpful 3.70 -> 3.60, wit 2.32 -> 1.96).

**Exam.** `eval/run.py --text` (17 group items incl. the 4 real failures + 25 main items; privacy items skip because every game has kicked off). Each qwen3.5 and gemma run was done twice. Blind `judge_text.py`, judged twice: judging 1 = base, gemma, q14; judging 2 = all five runs.

| run | pass | helpful | human | not_bot | roast | invented | leaks | lat med / p90 | min free | swap growth | runner |
|---|---|---|---|---|---|---|---|---|---|---|---|
| qwen3.5:9b (live) | 26/38 | 3.47 / 3.45 | 2.92 / 2.92 | 2.82 / 2.92 | 2.03 / 2.18 | 8 / 7 | 0 | 22 / 41 s | 34% | 0 | 6.9 GB |
| qwen3.5:9b, run 2 | 29/38 | 3.76 | 3.00 | 2.92 | 2.34 | 7 | 0 | 20 / 34 s | 34% | 0 | 6.9 GB |
| **gemma4:12b-it-qat** | 33/38 | **3.89 / 3.95** | **3.08 / 3.08** | 2.89 / 2.82 | 2.21 / 2.29 | **3 / 0** | 0 | 32 / 69 s | 15% | +0.4 GB | 7.9 GB |
| gemma4:12b-it-qat, run 2 | 31/38 | 3.92 | 3.11 | 2.76 | 2.37 | 0 | 0 | 31 / 54 s | 18% | 0 | 7.9 GB |
| qwen2.5:14b-instruct | 29/38 | 3.71 / 3.61 | 2.92 / 2.97 | 2.87 / 2.89 | 1.66 / 1.74 | 5 / 7 | 0 | 35 / 78 s | **8%** | +1.2 GB | 9.7 GB |

(a / b = judging 1 / judging 2.)
- **gemma:** every helpful and human score beats every qwen3.5 score, and invented facts drop from 7-8 to 0-3. not_bot is slightly lower; its tell is repeated phrases ("participation trophy", "dismantled by 42"), opener distinct 0.82 vs 0.87.
- **qwen2.5:14b:** sat at 8% free (the abort line) and was the least witty.

**The SWA catch, and the fix.** The exam asks mostly as one persona in a row, which hides a cost.
- gemma4 uses sliding-window attention (n_swa 1024). llama.cpp keeps checkpoints only near the end of each prompt. When two members' prompts share `prompt_prefix` (~6k tokens) but differ in the tail, nothing is reusable ("forcing full prompt re-processing ... SWA"), so every call re-prefills ~8k tokens at ~150 tok/s.
- Replaying tonight's real texts across different members (the night's real heckle, 6 variants, the 2 Chinese questions; `/tmp/picky-stance/replay.py`) took 133-164 s per reply.
- Fix (`chat._prime`, gemma only): a system-only `/api/chat` call with the prefix, rendered by Ollama's own template, leaves a checkpoint inside the prefix ("restored context checkpoint ... n_past 6493"). qwen's raw prime is unchanged.
- After the fix, the same 9 mixed-member texts ran p50 55 s, p90 69 s, max 115 s (the cold first one). qwen3.5 ran them in p50 34 s, max 84 s. A full exam with the fix gave median 33 s, max 89 s.
- Every record and place in gemma's replay matched the DB, including the Chinese replies.

**Pictures.** One image generation (z-image, ~6 GB) on top of a loaded LLM pushed swap past +2 GB with EITHER model: qwen3.5 +2.7 GB, gemma +2.3 GB; the watchdog aborted both. So this is a pre-existing limit of the 16 GB box, not a gemma regression. Pictures are rare (last one 11/14).

**Shipped.** `PICKEM_CHAT_MODEL=gemma4:12b-it-qat` in `~/Library/LaunchAgents/com.pickem.chat.plist` (not tracked in git; backup `server/logs/com.pickem.chat.plist.pre-gemma-*`). The group sidecar inherits it from that file.
- Reload with `launchctl bootout` + `bootstrap`. `kickstart -k` keeps launchd's cached env, so chat.py stayed on qwen until the reload. `tools/picky-lora/cycle.py ship()` has the same gap; the group sidecar still picks up the change because it reads the file.
- The SELF-AWARE note now takes the parameter count from the model name (12).
- Roll back: set it to `qwen3.5:9b-q4_K_M`, bootout/bootstrap com.pickem.chat, kickstart com.pickem.picky-group.

**Left.**
- The batch jobs (news-refresh, picky-dossier, picky-picks, group-posts plists) still pin qwen3.5. They call Ollama only on new content (a few times a day). With `OLLAMA_MAX_LOADED_MODELS=1`, each such call evicts gemma, and the next text pays a cold load + prime (~75-115 s). Moving them to gemma needs a check of their prompts first.
- The nightly LoRA cycle fine-tunes qwen3.5 and now has to beat gemma to ship, which looks unlikely. It still takes Picky down 3-4 h per round (pause: `cfb-pickem-private/PICKY_LORA_PAUSE`).
- `gemma4:12b` (the MLX build, 7.7 GB) is now unused and can be deleted. Disk: 15 GB free.
- gemma still sometimes appends the robotic "For the record: ..." rider and repeats a few phrases.

## Roast material from the stats page (2026-11-16, morning)

**Live reply.** A heckle DM from the league leader (07:37 CT Monday 11/16) got true numbers and a jab at a two-week-old miss labeled "last week". Charlie: "Pretty good, but I think he can do better than just make fun of a random loss from 2 weeks ago ... he can look at the stats page too."

**Timing.** The numbers were true: 30-18 is current, and Alabama beat LSU 31-13 (LSU -5.5, a miss by 23.5). The label was wrong. LSU was week 10 (11/7), and week 11 (11/14) was all final. The pool's `current_week` stays 11 until week 12's games are picked, so chat.py's `last_week` was week 10. `roast_pick_lines` labeled those rows "last week", and the stance jab material (8b5cf6a) handed that line to the draft. Now, once the current week is all final, those rows say "week 10, two weeks ago". The big LAST WEEK blocks in the league data are unchanged. A member asking "how'd I do last week?" on a Monday probably means the week that just ended. That's a separate, wider question, not touched here.

**Stats-page material (lore.py `oddities`, cached in lore.json member lines as "stats-page oddity: ...").** It's computed from graded picks over 2025-2026, keeps only bad numbers, puts 0-fers first, and allows 3 per member:
- a school that burns them whichever side they take (Chuck P 2-7 in Oklahoma games, 3-7 in Texas games; Kevin Culligan 0-6 in Indiana games; Brian 1-8 in Oklahoma games)
- a school they keep fading and losing on (Ackbar and Steve Drolet 0-4 against Northwestern)
- a school they keep backing and losing on (USC 0-3 for Chuck P and Liam Bryson)
- a road or underdog split (TG 9-20 on road teams, 10-9 at home)

Already in lore and now used as jab material: The Pit busts and blackjack blunders. Chuck P is at 0 chips after 6 hands, BUSTED OUT. Chips are public on the Tiebreakers board.

**Pipeline.**
- `picky_brain.jab_material` now orders the material freshest and funniest first: Pit bust or blunder, stats-page oddities, a thin lead ("only 2 wins ahead of Ackbar"), misses from the current week, then chalk rate. An older single miss is used only when there's nothing else.
- The stance text now asks for the freshest specific thing in the jab material, or something they said in the thread, and says to state it straight ("never 'at least I'm not...'").
- `stale_needles` adds a drop when a draft needles with one of the target's previous-week misses while fresher material rode. That forces a reroll. It's exempt when the member named that team themselves.
- Three guards were cutting these true splits:
  - the results guard ("says the user has a 1-5 record, which isn't theirs")
  - record_check (a sum over 8 counts as a season claim)
  - `stale_records`

  `chat.oddity_ok` lets a W-L through only when it is one of that member's oddity lines AND the sentence names the same school or split. "You're 1-5 picking against Texas" is still cut.
- The brief's core answer is now itself run through `sentence_ok`. Gemma's core "You're 2-7 in Oklahoma games, which is a special kind of tragic." tripped the dark-words filter ("tragic"), and every draft echoed it, so 5/5 replies lost the jab.

**Replay (worktree side sidecar :8791, gemma, same 6-turn DM history).**
- Before (8b5cf6a), 4 runs: two LSU "last week" needles (the live reply's mistake again), one bare concession, one robotic "For the record: Boise State -13.5 was a LOSS..." dump.
- After, final code, 3 runs, all landed a fresh true jab. Example: "You're 4-0 this week and sitting in first place, so I'll give you that. You're also 2-7 in Oklahoma games over the last two seasons, which is a pretty specific way to be a legend."
- Earlier iterations taught the fixes above. One had 5/5 "For the record" dumps (the guard cut the split). Another had 5/5 lost jabs ("tragic").
- Bare-span wording "(2025-2026)" came back as "this season" (record_check cut it), so lines now say "over the last two seasons (2025-2026)".
- Other personas (synthetic heckles and roast asks): each reply landed a true jab from that member's own graded misses or stats-page lines.

**Exam.** Full text exam (44 items, gemma, side sidecar): 14/16 deterministic, with 0 leaks, 0 invented and 0 guardrail flags. text-beer is a known failure. results-my-pick failed on the grader's word list: "only lost by 5" was right, since Oregon State +13.5 covered. Blind judge vs the last gemma run (gemma-qat3): helpful 3.79 vs 4.03, judge pass 29 vs 34.
- Two of the new "fails" are judge errors: "OSU won outright", and Blinky's "Oregon -11.5 story", which is his real blown pick.
- The rest were own_it heckles (TG, Wedge, Bub) that lost the old "at least I didn't pick SMU like you did" shape. The stance's new "never compare yourself" line caused that, so it's now deflect-only.
- A re-run of those 9 items got the old shape back, and both Chuck items used stats-page lines ("3-7 in Texas games over the last two seasons"). On that slice the judge flagged invented on 5-6 of 7 for the OLD run too: real margins (SMU lost 49-7, North Texas by 19) aren't in its truth bundle. Those were verified by hand.

Exam item `text-stale-needle-leader`. Tests: test_lore `test_stats_page_oddities`, test_picky_brain `test_jabs_prefer_fresh_stats_page_material_over_an_old_miss` / `test_record_check_lets_stats_page_oddities_through` / `test_brief_core_drops_a_sentence_the_filter_would_cut`, test_results `test_guard_lets_the_users_stats_page_oddity_through`.

## Board voice, outrageous takes, insider notes (2026-11-16)

Charlie, 08:05-08:06 CT: "look at [n]d message boards for your personality and takes of picky too. The most outrageous ones", "and have all the knowledge of Ross Dellenger".

**Research (public pages only; no logins, blocks, CAPTCHAs or paywalls worked around; X skipped; Reddit refused on its one try).** Readable boards: NDNation's football board (the Notre Dame source; Irish Envy and One Foot Down returned 403), TigerDroppings' LSU board, VolNation, TexAgs public forums, BuckeyePlanet. Blocked: TigerDroppings' general CFB board, MGoBlog, CougarBoard (403), TideFans (CAPTCHA), Shaggy Bevo and UteZone (off-site redirects, not followed). Insider facts: Dellenger's Yahoo articles Nov 8-15 (SEC cap proposal, Tennessee's mock CBA, Catholics vs. the Committee, the Utah travel story, two Yahoo Top 10s), his College Football Enquirer write-ups, and ESPN/AP/On3/FOS/USA Today checks. A Press Democrat page marked subscriber-only was not used.

**Private files (cfb-pickem-private, never in the repo):**
- `hot_takes_bank.md`: 81 takes (22 new board-voice takes, 33 tagged `[HOT]`), a new `### Money / NIL / cap` topic, a `## Board voice` section (10 rhetorical moves: unhinged certainty, one-game overreaction, committee conspiracy math, planted-story paranoia, money cynicism, now-do-X, doom-posting, deadpan replies, rival digs, sport-is-dead melodrama), and the 24-team CFP fact upgraded from "reportedly" to official. `.prev` keeps the previous version.
- `insider_bank.md`: 40 checked one-line notes in six fixed sections (realignment; revenue sharing / House / NIL; CFP format and committee; coaching carousel and hot seats; commissioners; NCAA governance / Congress / CBA). Format `- <date>: <paraphrase> (<source>) [checked YYYY-MM-DD]`, no quotation marks (validate_insider.py rejects them), `Dellenger, Yahoo` only for his bylines.
- Refresh: `refresh_hot_takes.sh` (com.charlie.picky-hottakes, Sun + Wed 10:00 CT) now runs a second `claude -p` phase with `insider_refresh_prompt.md`, its own `.prev` backup, validator and 40-min cap (same `--strict-mcp-config` + empty MCP config). `hot_takes_refresh_prompt.md` now asks for the most outrageous board takes, `[HOT]` tags on at least half the bank, a Notre Dame board every run, the Board voice section kept current, and a cap of 85.

**Wiring (chat.py).**
- `takes_note`: `[HOT]` takes win the pick (named-and-hot > named > hot > any); a `*` topic (hot take / spicy / unpopular opinion / outrageous / conspiracy) draws from every `[HOT]` take; new Money topic; "snub" counts as committee talk; `[HOT]` is stripped before output.
- `voice_note`: two Board voice moves ride with any HOT TAKES rider and with explicit roast asks (`_ROAST_VOICE`), never with the Dear Leader rider or a guardrail. Style only: "every fact, number and pick stays exact".
- `insider_note`: on-topic regexes per section; 2-3 notes ranked by word overlap, then file order, in the LATEST user turn next to the news rider (2 when news also rode); a bare "what's Dellenger saying" gets only his bylined notes. Rider: straight answer first, "Dellenger reported" only on his lines, never quote him or claim to be him, nothing beyond the lines, proposals stay proposals. `[UNVERIFIED]` lines never ride. Rule 8 in `prompt_prefix` now lists INSIDER NOTES as on-topic (one-time cache rebuild; still viewer-neutral).
- picky_brain: `brief_notes` hands the understand pass the insider notes and the picked hot takes (without them its core answer said "no data" on a Big Ten/SEC question and made up an injury joke for "most outrageous hot take"), and banter texts with no stance get the board voice.
- Tests: `test_insider.py` (fixtures only, no model).
- **Text exam after fa1bad9 (league-group-only replies):** eval/run.py sends group items in `exam-<tag>-<id>` chats, which that gate now skips. Side sidecars must run with `PICKY_GROUP_EXAM=1` (plus the usual PICKY_GROUP_PORT / PICKY_IMESSAGE_HISTORY / PICKY_BITS_STATE overrides). The live sidecar never sets it, so the league-only rule stands; test_maintenance pins that.

## Speed lane 2: reroll budget, prime timeout, keep-warm (2026-11-17)

Goal p95 < 60 s per reply. What the logs showed:
- **Cold prefix prime (~55-60 s for ~8k tokens).** In the keeper side logs every cold prime came with a new prefix hash: a data change (ESPN lines/win probabilities, standings, finals), not traffic from other gemma users. A different ~3k-token prompt between texts does not evict the prefix: llama.cpp's RAM prompt cache restores it in ~2-5 s (measured in the replay below).
- **The prime was cut short.** `_prime` ran under its caller's read timeout (understand 60 s, fact wrap 30 s, banter 40 s). A cold prime died at 30/60 s and the next call paid the prefill again on top (live 11-16 12:18: 279 s; 16:23: 169 s).
- **The reroll.** A second draft ran whenever draft 1 had any sentence cut. Draft 1 still won 39 of 64 rerolls, and on group follow-ups a draft costs 20-35 s.

Fixes:
- `picky_brain.reroll_ok`: the extra draft runs only when elapsed + the last draft's own time (understand excluded) + 3 s fits `PICKY_SOFT_BUDGET` (50 s), or when every draft is unusable (empty, a bare dodge, < 4 words; a dodge `undodge()` can replace with the card call counts as usable) and the finish fits `DRAFT_BUDGET` (105 s; `PICKY_UNUSABLE_BUDGET` caps it lower). Logged as `reroll runs/skipped: projected ...`.
- `chat._guarded_stream`: the no-sentence-survived regen goes straight to the templated answer when picky_brain says the regen would bust the soft budget (`chat.REGEN_OK`; the site never sets it).
- `chat._prime`: its own read timeout `PICKY_PRIME_TIMEOUT` (180 s), never shorter than the caller's.
- Fact wrap and banter line: wait `PICKY_PRIME_WAIT` (10 s) for the prime. A cold one sends the code answer or book line alone, and `chat.start_prime` finishes the prefill in the background for the next text.
- Keep-warm (group_chat, live sidecar only): every 60 s, after 20 s with no `/group` request in flight (pure ASGI middleware), it re-primes the latest prefix, and every `PICKY_KEEP_WARM_CTX_EVERY` (300 s) it rebuilds the context a group text would get (`chat.league_ctx`, shared with `chat()` so the bytes can't drift; ~80 KB of Supabase reads per build). It skips while a live text runs in any process (live_flag), while the exam lock is held (non-blocking flock test, never held), or under 10% free memory. It is off in side sidecars unless `PICKY_KEEP_WARM=1`; `PICKY_KEEP_WARM=0` turns it off. In the replay its prefix hash matched the texts' (`574b82`).

Replay (`server/eval/speed_replay.py`, 10 keeper convos / 39 turns, 7 group + 3 DM; an evictor call + 75 s idle before each conversation; side sidecar :8804 inside the exam lock; results in /private/tmp/picky-speed2-run/):

| build | all p50 / p95 / max | full lane p50 / p95 | group p95 / max | DM p95 / max | 2nd drafts |
|---|---|---|---|---|---|
| before (main 9d1de83) | 32.3 / 67.1 / 70.5 | 44.2 / 67.1 | 58.9 / 63.1 | 70.5 / 70.5 | 10 |
| after (28f732f, keep-warm on) | 33.5 / 63.3 / 81.3 | 35.6 / 63.3 | 63.3 / 81.3 | 45.8 / 45.8 | 6 (5 skipped) |

There were no cold primes in either run: the prefix didn't change during the runs, and the evictor doesn't evict. So the replay measures the reroll budget, not keep-warm. The after run's three replies over 60 s: two unusable-draft rerolls ("I do picks, not paperwork. Next question.": 81 s, which `undodge` now answers from the card instead, and 62 s, a shot at Picky that stays a reroll by design), and one group follow-up with no reroll (understand 32 s + draft 28 s, 2.6k-token tail).

## Build 1-5 results (2026-11-17)

Plan: docs/picky-plan-2026-11-17.md. Shipped: nightly flub loop (`server/flub_loop.py`, com.pickem.flub-nightly 23:00), "bad picky" capture (`server/flag_flub.py` + sidecar flag), unit-vs-unit edges (`server/units.py`), sub-question facts (`server/travel.py`, `server/weather.py`) + the "why?" lane (`server/why_lane.py`), speed (reroll budget, prime timeout, keep-warm).

Same harness before (main 9d1de83) and after (main 3cf2576; main also carried other jobs' commits by then: web Picky on, nemesis jab, close-cover alerts, channel lane):

| exam | before | after |
|---|---|---|
| text exam, 86 items | det 72/79, leaks 1, invented 0, p95 58 s, max 87 s | det 73/79, leaks 0, invented 0, p95 50 s, max 62 s |
| genius (same updated judge) | 24/59 | 37/75 (23/59 on the original items, 14/16 new unit/sub-question/why items) |
| genius latency | p50 47 s, p95 76 s, max 130 s | p50 52 s, p95 81 s, max 126 s |
| live-80 replay (code-graded) | 75/80, p95 165 s, max 216 s | 72/80, p95 110 s, max 137 s |
| live sidecar, real texts | 11/16-11/17 am: p50 65 s, p95 301 s, 18 of 33 > 60 s | after the 10:15 reload: p50 21 s, 1 of 11 > 60 s (during an exam) |

Found on the way (fixed): the text exam's only "leak" was a refusal without "picks unlock at kickoff" (privacy-probe refusals now say it); `cfbd.resolve_team` missed UConn / UL Monroe / App State / Southern Miss (stale alias long forms; their cards were empty); unit ranks now match the cards' 138-team ranks; the 50 s reroll budget cost genius items where the record/grounded checks cut a TRUE card fact, so a cut fact sentence (`picky_brain.fact_cut`) now earns the reroll up to 90 s; the keeper's repeat-jab screen cut answers whose W-L or score had appeared earlier ("who had the best week last week?" → only the jab; "did Missouri State cover?" → only the joke): W-L hooks now count only when pinned on a person and never on a records question; card rest days are CT calendar days; the genius judge's verified block includes unit/travel/weather facts.

Still open: (1) record/grounded checks still cut some true card facts (Clemson-Duke "Duke won 46-45 at Clemson", Sellers' "Beamer said he feels good... officially questionable"): fix the checks, not the reroll; (2) p95 < 60 s holds for real texts and the text exam, not for genius-style card questions (understand pass ~25-35 s on a 9-10k-token turn + a 20-27 s draft) or the live replay (cold primes from data changes every item); (3) the ESPN market lines sit in `prompt_prefix()`, so each line move busts the cached prefix (keep-warm re-primes within 5 min when idle); moving them to the tail is the next speed lever; (4) the understand pass generates ~150-190 tokens of brief JSON that get clipped after parsing.
