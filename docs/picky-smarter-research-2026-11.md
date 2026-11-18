# Making Picky smarter: research, 2026-11-17

Charlie, 19:04 CT: "keep coding and researching tips online how to make him smarter", "I care more about quality than
speed", "Dont try anything youve already tried and it didnt work". Every tip below was checked against
docs/picky-tried-ledger.md first. The fixed constraints still apply: local gemma4:12b-it-qat only, 16 GB, no paid
APIs, nothing per-user or per-minute in `prompt_prefix()`, and group p90 ≤ ~60 s.

## The architecture these tips have to fit

- The prefix (instructions + viewer-neutral LEAGUE DATA, ~6-7k tokens) is cached once and primed.
- The latest user turn carries the riders (cards, records, stance, brief) and ends with the question.
- Code lanes answer the common questions. On the full lane: understand pass (JSON brief) → 1 draft at temp 0.2 →
  per-sentence fact guard → **a reroll when sentences were cut. That reroll runs at temp 0.8 / min_p 0.05**
  (`SAMPLING[1]`) with an "angle" and banter ammo.
- gemma4 is sliding-window: 5 of every 6 layers see only the last **1024 tokens**, and only every 6th layer
  (global) sees the whole context.

That last point matters. On the full lane the question sits at the end of a 1-3k-token rider block. None of the
rider tokens "know" what was asked, and on the SWA layers the question can't see riders more than 1024 tokens back.

## Tips, ranked by expected quality gain for Picky

### 1. Prompt repetition: say the question before the riders as well as after them

- **Source:** Leviathan, Kalman, Matias (Google Research), "Prompt Repetition Improves Non-Reasoning LLMs",
  arXiv 2512.14982, Dec 2025, https://arxiv.org/abs/2512.14982. Related: Xu et al., "Re-Reading Improves Reasoning
  in LLMs" (RE2), EMNLP 2024, https://arxiv.org/abs/2309.06275.
- **Evidence:** 47 wins and 0 losses in 70 model×benchmark comparisons, with no thinking mode, across Gemini, GPT,
  Claude and DeepSeek. RE2 improved 14 datasets / 112 experiments. The mechanism is that causal models can't look
  ahead, so tokens read before the question are encoded without it. With the question repeated up front, every
  rider token attends to it.
- **Why it fits Picky:** riders are long and the question comes last, and gemma's 1024-token sliding window
  makes position matter even more. The genius exam's known weakness is "answering the exact sub-question
  (lookahead, body clock, altitude)" and "why?" turns that repeat turn 1.
- **Expected gain:** moderate on full-lane answers (on-point-ness, sub-questions). It's free on code lanes.
- **Latency:** prefill of ~20-60 extra tokens at ~120 tok/s, so under 0.5 s. Output length unchanged.
- **Fit:** high. It's one line in `chat._user_turn`, latest turn only, never the prefix.
- **Not in the ledger because:** no job ever changed where the question sits. P2 (intent-specific prefixes) and P4
  (few-shot turns) are different mechanisms, and this adds nothing to the prefix.

### 2. Verifier-guided repair instead of a blind hot reroll (CRITIC pattern)

- **Sources:** Gou et al., "CRITIC: LLMs Can Self-Correct with Tool-Interactive Critiquing", ICLR 2024,
  https://arxiv.org/abs/2305.11738. Zhang et al., "Small Language Models Need Strong Verifiers to Self-Correct
  Reasoning", 2024, https://arxiv.org/abs/2404.17140. Huang et al., "Large Language Models Cannot Self-Correct
  Reasoning Yet", ICLR 2024, https://arxiv.org/abs/2310.01798.
- **Evidence:** self-correction without outside feedback gives little or even hurts (CRITIC's tool-free ablation,
  Huang et al.). With an external verifier saying what's wrong, revisions reliably improve, and Zhang et al. show
  this holds for ≤13B models only when the verifier is strong.
- **Why it fits Picky:** Picky already has the strong verifier, the per-sentence fact guard, and it knows exactly
  which sentences it cut. Today that information is thrown away. The reroll is a fresh draft at temp 0.8 / min_p
  0.05, the same hot sampling the ledger records as tripling invented facts (O8). The live log shows the reroll
  winning only 2 of 6 times. A repair pass at temp 0.2 gets "your draft, these sentences were removed as wrong or
  unsupported, rewrite keeping the rest" and should keep the answer whole instead of rolling the dice.
- **Expected gain:** fewer answers that lose their substance to cuts, and fewer invented facts on rerolls. It only
  fires when the guard cut something (~1 in 5 full-lane replies live, more on genius).
- **Latency:** the same as the reroll it replaces (one generation, under the same budgets), maybe shorter.
- **Fit:** high. It's a small change in `picky_brain.draft()` behind a flag.
- **Not in the ledger because:** B4 (reroll) is a blind regeneration, and B7 (results guard regen with a
  correction) only covers results questions in chat.py. No job has fed the guard's cuts back into the brain's
  reroll. It also removes, rather than retries, the hot sampling from O8.

### 3. Natural-language brief, then parse (decouple the reasoning from the JSON format)

- **Source:** Tam et al., "Let Me Speak Freely? A Study on the Impact of Format Restrictions on Performance of
  LLMs", EMNLP 2024 Industry, https://arxiv.org/abs/2408.02442.
- **Evidence:** JSON mode cut reasoning accuracy hard (GPT-3.5 GSM8K 76.6% → 49.3%). Generating in natural
  language and converting after ("NL-to-Format") kept most of the accuracy. For pure classification, JSON mode
  was as good or better.
- **Why it fits Picky:** the understand pass uses Ollama `format` (constrained decoding) for a brief that mixes
  classification (intent, tone) with reasoning (`asking`, `answer` = the core reply every draft follows). The
  core answer is the reasoning part.
- **Expected gain:** small to moderate on the core answer's correctness. It's uncertain because our brief is short
  and the key order is already reasoning-first.
- **Latency:** neutral (same tokens), plus a parse fallback to the JSON call on failure.
- **Fit:** medium. It touches the understand call and its parser.
- **Not in the ledger because:** B1 (the brief) is KEPT. Its output format was never A/B'd.

### 4. Positive phrasing and no quoted banned phrases in the prompt ("semantic gravity wells")

- **Sources:** Jang et al., "Can LLMs Truly Understand Prompts? A Case Study with Negated Prompts", 2022,
  https://arxiv.org/abs/2209.12711. Rana, "Semantic Gravity Wells: Why Negative Constraints Backfire", Jan 2026,
  https://arxiv.org/abs/2601.08070. Practitioner write-up:
  https://eval.16x.engineer/blog/the-pink-elephant-negative-instructions-llms-effectiveness-analysis
- **Evidence:** negated instructions get worse with scale, and naming a forbidden word primes it. Tested on
  Qwen2.5-7B, a single-model study.
- **Picky:** the prefix quotes a "worn-out phrases you never use" list ("dumpster fire", "god among men" …), and
  no_repeat/BANNED_TICS already enforce bans in code. But the live drafts logged today show 0 hits for every listed
  phrase in 42 drafts. There's no measured problem to fix, and changing the prefix costs a cold prime.
- **Verdict:** documented, not built. Revisit if the flub loop shows tic clusters.
- **Not in the ledger because:** never tried.

### 5. Logprobs from Ollama (unused feature)

- **Sources:** Ollama added `logprobs` / `top_logprobs` (0-20) to /api/chat and /api/generate in v0.12.11 (we run
  0.40.16). Release notes: https://newreleases.io/project/github/ollama/ollama/release/v0.12.11-rc1. API:
  https://docs.ollama.com/openapi.yaml. Kang, Zhao, Song, "Scalable Best-of-N Selection for LLMs via
  Self-Certainty", NeurIPS 2025, https://arxiv.org/abs/2502.18581. Sentence-level NLL uncertainty after Manakul
  et al. (SelfCheckGPT), see arXiv 2509.04492.
- **Uses:** (a) self-certainty to choose between draft 1 and a reroll when both have equal cuts, with no judge
  call; (b) flag a sentence whose number or name tokens were low-probability and not grounded; (c) a calibrated
  confidence for an enum intent call.
- **Expected gain:** small. (a) fires rarely, because the code screen already decides most picks. (b) overlaps the
  fact guard, which is stronger for facts. (c) needs a model intent call the router mostly doesn't make.
- **Latency:** ~0.
- **Not in the ledger because:** B2 was a generated LLM judge, and this is a probability readout with no extra
  generation. Still, (a) is close enough to "best-of-N selection" that it should only run after 2-3, so it's
  parked.

### 6. Gemma 4 prompting specifics

- **Sources:** Google, "Gemma 4 prompt formatting", https://ai.google.dev/gemma/docs/core/prompt-formatting-gemma4.
  Ollama library page: https://registry.ollama.ai/library/gemma4.
- **Facts:** native system role (Picky already primes the prefix as a system turn). Thinking is turned on with
  `<|think|>` at the start of the system prompt, and a "LOW" thinking instruction cuts thinking tokens ~20%.
  Recommended sampling is temp 1.0 / top_p 0.95 / top_k 64. Thought blocks are dropped from history.
- **Verdict:** sampling at 1.0 is ledger O6-O8 territory (warmer sampling tripled invented facts), so it's off
  limits. Thinking is ledger M10 (failed on qwen3.5) and goes under "Needs Charlie's OK": the model changed, so a
  capped, genius-lane-only trial could be argued for, but it isn't run. Putting `<|think|>` in the system prompt
  would also change the cached prefix for every lane.

### 7. Quant choice on 16 GB

- **Sources:** Google, Gemma 3 QAT (perplexity drop at Q4_0 cut 54% vs naive),
  https://developers.googleblog.com/en/gemma-3-quantized-aware-trained-state-of-the-art-ai-to-consumer-gpus/.
  3h4x, "Gemma 4 QAT vs non-QAT", Jun 2026, https://3h4x.github.io/tech/2026/06/08/gemma4-qat-vs-non-qat. Community
  27B PPL: QAT Q4_0 5.49 vs Q5_K_M 5.70 (r/LocalLLaMA thread 1jqnnfp). One report of tool-calling regressions on
  the 12B QAT (control-token config).
- **Verdict:** stay on the QAT Q4_0. At 4 bits it ties or beats K-quants and is the smallest and fastest. Q5_K_M
  (~8.5 GB) or Q6 would add 1-3 GB on a box running 9.7/10 GB swap, which is the M5/M6 failure mode. Picky doesn't
  use tool calling, so the QAT tool regression doesn't matter. KV cache q8_0 is already on (O1), and q8_0 KV is
  near lossless.
- **Not in the ledger because:** the QAT pick was by exam, not by quant research. This confirms it, nothing to do.

### 8. Dynamic exemplar retrieval (kNN few-shot)

- **Sources:** Liu et al., "What Makes Good In-Context Examples for GPT-3?" (KATE), 2021,
  https://arxiv.org/abs/2101.06804. Counter-evidence specific to persona dialogue: "Crafting a Good Prompt or
  Providing Exemplary Dialogues?", 2024, https://arxiv.org/abs/2402.09954.
- **Evidence:** similarity retrieval helps QA-type tasks. For persona dialogue, random demos beat retrieved ones,
  same-context retrieval was worst, and prompt wording mattered most.
- **How it would differ from P4:** P4 used regex-category banter-bank lines as fake turns on heckles only. Dynamic
  retrieval would pull judge-approved real Picky answers by similarity, across all intents.
- **Verdict:** skip. The only persona-dialogue evidence points against it, and it sits too close to P4 (failed) and
  P3 (copied jokes) to count as clearly new. The ledger rule wins.

### 9. Best-of-N with a reranker

- **Sources:** J1 judge study (pairwise inconsistency up to ~20%), https://arxiv.org/abs/2505.10320. "Am I More
  Pointwise or Pairwise?" (rubric order flips the top-1 on 15-41% of prompts), https://arxiv.org/abs/2602.02219.
- **Verdict:** off limits as an LLM judge (ledger B2: the 9B judge was at chance, and gemma as judge is a "slightly
  different version"). A separate reward model would need a second resident model (MAX_LOADED_MODELS=1, swap
  already full), and diverse drafts need hot sampling (O8). Not built.

### 10. Humor generation for small models

- **Sources:** Tikhonov & Shtykovskiy, "Humor Mechanics: Advancing Humor Generation with Multistep Reasoning",
  2024, https://arxiv.org/abs/2405.07280. Mirowski et al. (DeepMind), "A Robot Walks into a Bar" (20 pro
  comedians), 2024, coverage https://www.technologyreview.com/2024/06/17/1093740/what-happened-when-20-comedians-got-ai-to-write-their-routines/
- **Evidence:** LLM jokes are generic and safe ("cruise ship material"). What helps is specificity and personal
  material, plus multi-step association → combine → select pipelines (with GPT-4).
- **Picky:** the specificity lever is already pulled: code-picked jab material, stats-page oddities, burn book,
  member memory being wired now. The multi-step pipeline needs a selection step, which is a judge (B2) or hot
  sampling (O8), both failed. An Opus-offline version is how the burn book already works (A2).
- **Verdict:** nothing new to build live. A future offline idea: the nightly Opus burn-book refresh could use the
  association → combine method per member. That's an Opus prompt change, not a live change.

### 11. Structured intent classification

- **Sources:** practitioner guides on enum-constrained classification, e.g. https://www.respan.ai/articles/intent-classification-with-llms.
  The "Let Me Speak Freely" classification result (JSON mode is fine for labels).
- **Picky:** the router is regex-first and the brief already gives an enum intent. Real misroutes today (the
  "rate Charlie's looks" text went to the conference lane, the "dealer" ask went to the full lane) are regex
  precision bugs that the facts job (b36a06) and the flub loop already handle.
- **Verdict:** no new build. The brief's intent is already constrained (an enum in the schema).

## Picked for Phase 2 (A/B on the text exam + genius exam, flags, revert losers)

1. Question repetition (`PICKY_QREPEAT`). Smallest change, broadest effect.
2. Verifier-guided repair reroll at temp 0.2 (`PICKY_REPAIR`).
3. NL brief (`PICKY_NL_BRIEF`), if 1-2 leave time. Lowest confidence.

Results go in docs/picky-tried-ledger.md ("New results log").
