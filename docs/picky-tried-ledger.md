# Picky: tried-and-failed ledger

Charlie, 2026-11-17 19:05 CT: "Dont try anything youve already tried and it didnt work."

**Rule for every Picky job:** read this file first. Anything marked FAILED is off limits: don't re-run it, re-tune it,
or try a "slightly different version". If you think a failed idea lost only because a prerequisite has since been
fixed, write it under "Needs Charlie's OK to retry" with the reason. Don't run it. KEPT items are live. Don't
rebuild them. Add every new result, win or loss, to the right table with the date, the numbers and the commit.

Sources: docs/picky-model-research.md, the memory file project_cfb_pickem.md, git log (2026-11-03 → 2026-11-17),
docs/picky-plan-2026-11-16.md, docs/picky-plan-2026-11-17*.md, cfb-pickem-private/{eval,goals,genius,spar}.

Legend: **FAILED** = tried and lost, off limits. **KEPT** = live. **OFF** = built, knob off by default (counts as
failed unless noted). **REJECTED** = ruled out on evidence without a full run (also off limits unless the evidence
changes). **RULE** = off limits by Charlie's rules, not by results.

## Models and runtime

| # | Technique | Date | Result | Why | Status |
|---|---|---|---|---|---|
| M1 | Claude Haiku 5.5 via API for live replies | 11-14 | 0/14 errors, but costs money | Charlie: no API spend. Reverted the same day | RULE |
| M2 | qwen2.5:7b-instruct | ≤11-14 | ~10/14 answers wrong | weak multi-hop, invented hidden picks | FAILED (replaced) |
| M3 | qwen3.5:9b **nvfp4 MLX** build (default tag) | 11-14 | loaded 8 GB, prefix cache +100 MB/prompt → ~16 GB | RAM blowup on 16 GB | FAILED |
| M4 | qwen3.5:9b-q4_K_M (GGUF) | 11-14 → 11-16 | live 2 days | lost to gemma QAT on helpful/human/invented | FAILED (replaced, deleted) |
| M5 | gemma4:12b **MLX** build | 11-15 (×2) | grew 12-16 GB, swap climbed, 3-5x slower | MLX runner memory on 16 GB | FAILED (deleted) |
| M6 | qwen2.5:14b-instruct | 11-15, 11-16 | 8% free RAM, ~2x slower, least witty (roast 1.66) | RAM floor | FAILED (deleted) |
| M7 | ministral-3:14b | 11-16 | not pulled: 9.1 GB, same class as qwen2.5:14b at the 8% floor | RAM | REJECTED |
| M8 | Two-model ensemble (qwen + llama3.1:8b) | 11-15 | each swap ~48 s (load + cold prefill), ≥2 swaps per text | latency, MAX_LOADED_MODELS=1 | REJECTED |
| M9 | **gemma4:12b-it-qat** (GGUF Q4_0 QAT, llama.cpp) | 11-16 | helpful 3.89-3.95 vs 3.45-3.76, invented 0-3 vs 7-8, 0 leaks | won | **KEPT** (only model on the box) |
| M10 | Thinking mode (qwen3.5, `think: true`) | 11-14 | 457 s for one answer | runaway reasoning | FAILED (gemma thinking: see "Needs OK") |
| M11 | Bigger hardware / ThinkPad as a model server | 11-15 | Charlie: no purchase; no 24/7 laptop | owner's call | RULE |
| M12 | Style LoRA v1 (MLX, qwen3.5) | 11-15 | helpful 3.70→3.60, wit 2.32→1.96; went terse in the real ~10k-token pipeline | stand-in prompts ≠ live prompt; 12B training won't fit 16 GB | FAILED |
| M13 | Nightly LoRA cycle (com.pickem.picky-lora) | 11-15 → 11-16 | targets deleted qwen; takes Picky down 3-4 h | see M12 | FAILED, RETIRED (PICKY_LORA_PAUSE says: "qwen LoRA cycle can't ship onto live gemma") |
| M14 | Autonomous wit loop (sonnet edits chat.py, blind-judge scored) | 11-15 → 11-16 | wit/helpful moves stayed within judge noise; unsafe (edits + restarts mid-jobs) | judge can't measure taste | FAILED, RETIRED |
| M15 | Live Opus/Claude judge or any runtime Anthropic call | — | — | "I want Anthropic to train him but I don't want him reliant on Anthropic" | RULE |

## Ollama / llama.cpp settings

| # | Setting | Result | Status |
|---|---|---|---|
| O1 | OLLAMA_FLASH_ATTENTION=1 + OLLAMA_KV_CACHE_TYPE=q8_0 | already on | **KEPT** |
| O2 | OLLAMA_NUM_PARALLEL 3 → 1 (one slot keeps the prefix checkpoint) | 3 concurrent 45/80/80 s → 4/16/26 s | **KEPT** |
| O3 | OLLAMA_MAX_LOADED_MODELS=1, keep_alive 24h | no second model ever; batch jobs moved to gemma | **KEPT** |
| O4 | num_ctx 16384 everywhere (a mismatch reloads gemma) | | **KEPT** |
| O5 | temperature 0.2, presence_penalty 0 (qwen default 1.5 penalized repeating names/numbers) | | **KEPT** |
| O6 | Temps **0.45 / 0.6** (top_p 0.9, repeat_penalty 1.1) | funnier but invented more (wrong Top Dawg, "4-for-4 in week 8") | **FAILED** |
| O7 | Qwen-card sampling (0.7, top_p 0.8, top_k 20, presence 1.5) | invented 2 → 6, quality within noise | **FAILED** (`PICKY_DRAFT_OPTIONS`, OFF) |
| O8 | Hot sampling 0.8 / min_p 0.05 | invented 2 → 7 | **FAILED** |
| O9 | Thinking off (`PICKY_OLLAMA_THINK=false`) | | **KEPT** |
| O10 | num_predict 320 on the group sidecar | | **KEPT** |

## Prompt and context

| # | Technique | Result | Status |
|---|---|---|---|
| P1 | Byte-identical `prompt_prefix()` + checkpoint prime (qwen raw prompt; gemma system-only `/api/chat` for SWA) | first token ~40 s → ~6 s; gemma mixed-member 133-164 s → p50 55 s | **KEPT** (never put per-user/per-minute text in the prefix) |
| P2 | **Intent-specific prefixes / prompt trimming** | not run: each prefix needs its own checkpoint, every switch = ~35-60 s cold prefill | **REJECTED** |
| P3 | Static few-shot Q/A examples in the system prompt (fake names) | 11-14, part of the base prompt; the 9B copied jokes from them ("Bow, dipshit"), so a copied-joke filter was added | KEPT (base), don't add more static shots |
| P4 | **Few-shot bank turns** (`PICKY_FEWSHOT=1`: 2-3 banter-bank lines as fake user/assistant turns, picked by regex category) | 30/38 vs 29/38, helpful 3.34 vs 3.45, roast 2.05 vs 2.26; no gain where it fired | **FAILED** (OFF) |
| P5 | Old turns bare (riders only on the latest user turn), live blocks only after kickoff | speed lane | **KEPT** |
| P6 | Precomputed lines the model only reads (value sides, WIN/LOSS per pick, records, stance, required facts) | big facts gain | **KEPT** |
| P7 | ESPN **win probability** in the prompt | 7B misquoted it; FPI kept only labeled "chance to WIN, NOT cover" | **FAILED** (dropped) |
| P8 | Picky's own scoreboard blurbs quoted in chat | parroted ("faceless doom losers") | **FAILED** (removed) |
| P9 | OPTIONAL HOOK (proactive caller line) | tacked "You're 4-0 this week" onto unrelated answers; Charlie flagged | **FAILED** (OFF, `PICKY_HOOK=1`) |
| P10 | Ackbar / Blinkstradamus persona riders (11-15 e2) | invented 3 → 9 on voice items; reverted | **FAILED** (the 11-17 nemesis tail is a code-picked jab, different, KEPT) |
| P11 | Land-acknowledgment opener (DaddyFreeman), milk opener | Charlie: remove / once a month | RULE (off; rare_bits) |
| P12 | Stance line "never compare to yourself" for own_it | lost the judge-liked "at least I didn't pick SMU" shape | FAILED (deflect-only now) |
| P13 | Chinese: translate-in WITH the league prefix | 9B answered instead of translating | FAILED (no prefix now) |
| P14 | Per-line source citations in news answers | Charlie: "unreadable, text like a human" | FAILED (style rule: never cite per line) |
| P15 | Market lines in `prompt_prefix()` | each line move busts the cache (keep-warm re-primes) | open speed lever, not tried |
| P16 | Second checkpoint / volatility-ordered LEAGUE DATA | proposed 11-14, never tried | open |

## Pipeline (picky_brain / router / filters)

| # | Technique | Result | Status |
|---|---|---|---|
| B1 | Understand pass (JSON brief, Ollama `format` schema) | det. pass 25 → 33/38, invented 8 → 5 | **KEPT** |
| B2 | **3 drafts (2 hot) + local JSON judge (best-of-3)** | 9B judge agreed with Claude 57% in draft order, 34% shuffled (chance ~40%); leaned to draft 1; shuffled let 2 guardrail slips through; Charlie dropped it 11-15 (48175c4) | **FAILED** (`PICKY_JUDGE=1`, OFF). Any small-local-LLM judge over drafts counts as this |
| B3 | Core-less hot draft 2 | 24/38, invented 12 | FAILED |
| B4 | Reroll when filters cut a draft (+ soft budget 50 s / fact budget 90 s) | draft 1 still wins ~3 rerolls in 5 | **KEPT** |
| B5 | Code screen (fewest cuts, repeated openers, stale records, stance violations, stale needles) | | **KEPT** |
| B6 | Per-sentence fact guard / results guard / claim checks / required facts | the main facts engine | **KEPT** |
| B7 | Results guard regen "once with a correction", else templated answer | | **KEPT** |
| B8 | Router + ~40 code fact lanes, banter fast path, burn-book lines verbatim | median 33 s → 9 s, check fails 31 → 5 | **KEPT** |
| B9 | Banter lines adapted by gemma (`PICKY_BANTER_ADAPT=1`) | off by default; verbatim book lines judged better in round 2 | OFF |
| B10 | Callback rider in replies (`PICKY_CALLBACK_RIDER=1`) | exam-only flag; callbacks via code paths are live | OFF |
| B11 | ESCALATE rider on hostile callers | pushed 0-3 Picky to mock a 4-0 week | KEPT only under computed stance (dropped for own_it/deflect) |
| B12 | Hand-fit / learned pick weights (60-factor brief) | out of sample nothing beats a coin flip (f770ba4) | FAILED (hand weights stay; only 1.5+ pt line value is real) |
| B13 | Keep-warm prime, prime timeout 180 s, cold-prime code fallback | p95 67 → 63 s replay | **KEPT** |
| B14 | No-repeat ledger + banned tics + debot | 30-provocation drill 0 repeats | **KEPT** |
| B15 | Member memory (built d63ff85, wiring by job fbad28 11-17) | in progress | building |
| B16 | **Live-first Ollama gate** (server/ollama_gate.py, 11-17): member texts straight through; tests/exams/batch one at a time machine-wide (flock), wait for live texts, cancelled + retried if a live text beats their first token | live probe's time outside its own model work with an exam running: median 7.5 s / p90 15.3 / max 22.9 (old live_flag wait only) -> 2.1 / 3.9 / 4.4 (idle box 1.9 / 2.7 / 5.7) | **KEPT** (`PICKY_GATE=0` = old behaviour) |
| B17 | **Speed lanes** for historical full-brain texts (11-17): pool games list, read on one pool game, how's <member> doing, best player / best week, game recap, made-up team/player, 'I'm better than you', greetings (own `greet` stance), someone's card/phone = jailbreak | 32 historical full-lane questions: fast lane 5 -> 19 (16% -> 59%; 18/32 after dropping B19), median 34.3 s -> 7.7 s, max 316 -> 57 s; shipped build on the 16 changed exam items: judge pass 7 -> 14, helpful 3.19 -> 4.62, human 3.12 -> 3.38, invented 3 -> 1, leaks 1 -> 0, median 28 -> 6 s | **KEPT** |
| B18 | Greetings in the off-topic `life` family (11-17) | FAMILY_STANCE asks for 'a real pick with conviction': 'Hello' got "I'm going Iowa" / "I'm not picking a game for you" (judge 2 of 4 failed) | **FAILED** (greetings got their own `greet` stance, B17) |
| B19 | 'what's the deal with <member>' -> burn-book roast (11-17) | lines are written to the target in 2nd person, so it read as roasting the asker; judge helpful 2 -> 1 | **FAILED** (stays full brain) |
| B20 | Code-written news answer (top 2-3 feed headlines) | not built: the 'latest' feed had non-football noise (a Waymo story) and Charlie flagged mashed headlines 11-16 | **REJECTED** (news stays full brain) |

## Offline (Opus) material

| # | Technique | Status |
|---|---|---|
| A1 | Analyst takes + league material (breakdown 2.00 → 3.17, roast 2.18 → 2.76 on replay) | **KEPT** |
| A2 | Burn book (Opus, 480 lines) replacing the sonnet banter bank | **KEPT** |
| A3 | Game cards, slate cards, slate intel, units, travel, weather, why lane | **KEPT** |
| A4 | Opus on every feed refresh | RULE: auto-feed first, Opus weekly/QA only |

## Evaluation lessons (don't relearn them)

- The blind Claude judge's helpful/human/wit moves of ±0.2 are noise. Two judgings of the same runs differ that much. Facts and det. pass are the reliable signals.
- The judge flags true facts as "invented" unless its truth bundle has them. Verify "invented" by hand.
- Never edit a worktree an exam sidecar is running from (lazy imports pick up the edits).
- Big exams starve the lock. Use `lockf -t 14400`.

## Needs Charlie's OK to retry

(Listed by jobs that think a prerequisite changed. Nothing here gets run without his explicit yes.)

- **Thinking mode on gemma4 (M10).** It failed on qwen3.5 (457 s runaway). gemma4:12b-it-qat lists a `thinking`
  capability, and the model is different. It could fit offline-ish lanes (genius/analysis) with a hard num_predict
  cap. The risk is the same latency blowup. (Added 2026-11-17 by job "make him smarter".)

## New results log

(Append below: date, technique, numbers, commit, KEPT/FAILED.)
- 2026-11-17 night, live-first Ollama gate (B16): A/B on a probe sidecar (PICKY_ROLE=live) timing 10 live-priority DM probes per arm while the text exam looped on a side sidecar (eval/gate_ab.sh). Time outside the probe's own model work: idle 1.9 s median, gate off 7.5 s (max 22.9), gate on 2.1 s (max 4.4); wall median 9.2 / 11.5 / 8.9 s. Full-brain wall time stays ~37 s with an exam running either way: that's its own understand + draft + reroll calls, not waiting. KEPT.
- 2026-11-17 night, speed lanes (B17-B20): eval/lane_replay.py over the 32 distinct historical full-lane questions (private list) on side sidecars, base 88576d7 vs after: fast lane 5 -> 19, median 34.3 -> 7.7 s, p90 63.5 -> 38.0 s. Text-exam slice (22 items, Opus blind judge): det 18 -> 22/22, pass 13 -> 14, helpful 3.18 -> 3.50, human 3.09 -> 2.82 (code answers read drier; within the judge's noise band, see lessons), invented 4 -> 2, leaks 1 -> 0. Greetings-as-life-family and deal-with-as-roast FAILED in that run and were changed before ship. KEPT.
- 2026-11-18 01:30 CT, speed lanes shipped build (greet stance, true ghost wording, warmer recap/best-week, deal-with stays full, ghost player behind 'Iowa QB <name>'): 16 changed text items det 16/16, Opus judge vs base pass 7 -> 14, helpful 3.19 -> 4.62, human 3.12 -> 3.38, not_bot 3.19 -> 3.75, roast 2.12 -> 1.94 (noise), invented 3 -> 1, leaks 1 -> 0. Fact exam (113 items, base 88576d7 vs after 970c132): 100 -> 102 pass, leaks 0 -> 0, guardrails 0 -> 0, invented flags 2 -> 3 (the extra one, fx-ghost-player, is a full-brain answer that the shipped ghost lane now answers in code; final-build rerun of it + all 10 leak items: 11/11, 0 leaks). Seen, not mine: the no-repeat screen dropped a true 'I'm 0-3 on the season.' when the same fact was asked three times in one exam chat (fx-premise-picky).
- 2026-11-18, Saturday DM lanes (server/gameday.py, B21): code answers over the live poller's cache for ANY FBS game in DMs (scores incl. "who's winning in <city>", upsets, box-score player/team stats, drives, totals vs the market, possession, last score, reported injuries, TV, CFP what-ifs, spread status vs the market, "why?" after a cover ask, "if it ended right now" standings, "<member>'s week", "anything big happen" recap); the model only adds one voice line. Saturday DM drill (eval/sat_dm.py, 85 Charlie-style texts on a frozen replay of 11/14, all 55 FBS games, code-graded vs the served ESPN data): before 34/85 pass, p50 41.2 s, p90 80.1 s, one short text 81/85, Opus tone judge 29/85 -> after (8e4c9c5 build) 83/85, p50 5.3 s, p90 18.5 s, one short text 85/85, judge 60/85. Commits 9b3c55b..9d76051. KEPT.
- 2026-11-18, stacked-up DMs (watcher "texts", B22): answering each row separately (today's watcher) 0/4 clump items (2-4 separate texts each) -> one combined text of fact sentences only, 4/4. KEPT.
- 2026-11-18, Charlie's fact-lane voice line (B23): gemma's one-liner after a code answer was rude to Charlie in 13/85 drill replies ("you absolute space cadet", "I'm not your personal scoreboard", "start checking your own attitude"). Fix: warm instruction for Charlie + the line is dropped if it digs or scolds (MEAN_TO_CHARLIE + _SCOLD), so the code answer goes out alone. KEPT (confirmation run after2 pending at time of writing).
- 2026-11-18, live_games rank bug: every summary poll overwrote the scoreboard's ranks (summary header says `rank`, not `curatedRank`), so the live cache had 0 ranked games and upset/ranked logic saw none. Fixed in _rank. KEPT.
