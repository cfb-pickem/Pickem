# Picky architecture review, 2026-11-17

Read-only review of the production system as it ran on 2026-11-17 (main at 2dd4c00). Nothing was changed,
restarted or examined live. Every claim cites code (file:line), a log, or a measurement taken during the review.
Member messages are described, never quoted.

**Overall grade: C+.** The fact and privacy discipline is B+ work. The operations side (throughput under a burst,
restart and deploy safety, observability, memory) is D work, and that's what breaks on a Saturday. The quality
ceiling is the 12B model. The reliability ceiling is the plumbing, and that part is fixable in two weeks.

---

## 1. What's actually running

```
                         iPhone users (14 members)                     site users (signed in)
                                 |                                             |
                     iMessage (Apple)                               cfb-pickem.github.io (GH Pages)
                                 |                                             | SSE, JWT
   +-------------- macOS user `picky` (needs a manual GUI login after reboot) --+  Cloudflare tunnel
   |  Messages.app <--AppleScript-- watcher.py (serial loop, poll 3 s,          |  pickem-chat.dunecreek.ai
   |     chat.db (ro)                 MIN_GAP 10 s, 60/h cap, 420 s timeout)    |       |
   +---------------------------------------|------------------------------------+       |
                                           | POST /group, /outbox (secret)                v
   +---------------- macOS user `Agent` ---v--------------------------------------------------------------+
   |  com.pickem.picky-group  group_chat.py :8768  (FastAPI)          com.pickem.chat  chat.py :8766         |
   |    router.classify (57 ordered regex rules)                        /chat: JWT, rate limit, pictures,   |
   |    -> code lanes (fact/banter/jailbreak/playoff)                   then POST /web -> :8768 (brain)     |
   |    -> picky_brain.run: understand (JSON) -> draft -> reroll        fallback: single-draft pipeline    |
   |         each draft = chat.chat() via a fake Starlette request                                       |
   |         chat.py: league_ctx (Supabase+ESPN+CFBD) -> prompt_prefix (cached) + tail                    |
   |                  + 13 spliced riders + 26 named send-point checks + filters                          |
   |    -> send-point chain: debot, nicknames, no_repeat, repeat-jab, fact_final, live final_guard,       |
   |       matchup fix, nemesis tail, member-memory receipts, charlie_guard                               |
   |    keep-warm loop (re-prime every 60 s, rebuild ctx every 300 s, skips while exam lock held)        |
   |                                                                                                      |
   |  Ollama (com.dcc.ollama) llama-server gemma4:12b-it-qat, NUM_PARALLEL=1, 7.8 GB RSS  <== ONE SLOT    |
   |  image.py :8767 (z-image, +6 GB; a picture unloads gemma first)                                     |
   |                                                                                                      |
   |  KeepAlive pollers: live_games.py (ESPN 20 s), close_cover.py (15-60 s) -> outbox                    |
   |  22 scheduled jobs: news 5 min, lore 30 min, game-cards 30 min, feed-monitor 15 min, group-posts 5   |
   |    min, hourly x6 (picks, burn-book, league-material, rules-watch, slate-intel, dossier), insider 2h,  |
   |    cfbd 06:15 + Sat 10:30, nightly: member-memory 22:30, flub 23:00 (can push + reload), genius 03:30 |
   |  watchdog (10 min, /usr/bin/python3 3.9) + com.charlie.picky-hottakes (outside com.pickem)            |
   |  side sidecars + exams + 3-6 `claude -p --model opus` jobs, all on the same Ollama and RAM           |
   +------------------------------------------------------------------------------------------------------+
        |                  |                         |                          |
     Supabase          ESPN public JSON        CFBD API (cache, 50 of        claude -p Opus (offline only:
     (all_games,       (scores, lines,         1,000 calls used this         cards, dossiers, judges, burn
     picks, RPCs)      PBP, news RSS)          month)                        book, flub fixes, repairs)
```

Size: 41,162 lines of production Python in 58 modules (`server/*.py`, tests excluded), 17,370 lines of tests,
7,165 lines of eval. `chat.py` alone is 7,651 lines / 416 KB, was touched by 138 commits since 11/13, and holds
147 compiled regexes, 27 `*_note` riders and 8 `*_RIDER` constants. There are 99 distinct `PICKY_*`/`PICKEM_*`
env knobs in non-test code. The repo took 184 commits on 11/16 and 126 on 11/17.

Real traffic is tiny. On 11/17 the live sidecar sent 14 real replies (5 full-lane, 2 fact, 1 rules, 1 conf,
5 banter). `picky_exchanges.jsonl` has 407 rows, and only 38 of them carry a `secs` timing.

---

## 2. What's strong (keep it)

- **Privacy by construction, not by prompt.** The group context is built with Picky's teamless session and
  asserted on every request: no team, no unlocked pick (`group_chat.py:378-384`). A mapped DM runs as the
  member's own session. Groups other than the league are refused (`group_chat.py:1178-1182`). Text exams report
  0 leaks. This is the best decision in the system.
- **Facts in code, model as voice.** Covers are graded in code (`chat.grade_pick`), the router's fact lanes answer
  in 5-12 s (sidecar log 11/17: rules2 5.8 s, conf 4.7 s; web fact:pick p50 11.1 s), and `live_games.py` is a
  pure-code poller with `required_fact` + `claim_check`. The model is never asked to do league math.
- **Prefix-cache discipline.** The byte-identical `prompt_prefix()` + `_prime` is why a 9k-token prompt answers
  at all on this box: 283 primes in the sidecar log, p50 0.1 s, p90 0.6 s.
- **Cross-process live priority.** `live_flag.py` makes side sidecars and exams yield to member texts through
  the filesystem. It's simple and correct.
- **Runtime independence from Anthropic.** `PICKY_NO_ANTHROPIC` kill switch, template fallbacks for posts and
  breakdowns, offline-only Opus with a usage log (`cfb-pickem-private/claude_usage/`).
- **Watcher isolation.** A separate macOS user, AppleScript with text and guid in argv (never spliced,
  `watcher.py:322-336`), a secret in the keychain, private data 0600 outside the public repo.
- **Outbox design.** Kill switch, dry run, once-only keys, quiet hours, and expiry. Cover alerts expire after
  240 s (`close_cover.py:69`), which is the right instinct.
- **The tried ledger and the flub capture.** `docs/picky-tried-ledger.md` and "bad picky" → queue are the right
  feedback loop. Keep both.

---

## 3. Top 10 risks (ranked by impact × likelihood)

### R1. Saturday bursts queue behind one serial watcher and one Ollama slot. No backpressure.
**Evidence.**
- `watcher.py:474-513` handles rows one at a time. `handle_one` blocks on `ask_bot` (timeout 420 s,
  `watcher.py:303`), then sleeps out `MIN_GAP = 10 s` (`watcher.py:62`) in-line. Outbox posts drain only
  between rows. Every text of a multi-text post costs another 10 s gap (`watcher.py:411-420`).
- Live, 11/17 12:03: four group asks came in together. The fourth was answered 86 s after the first was picked
  up (watcher log 12:03:09 → 12:04:35), but its own compute was 4.7 s (sidecar: `fact:conf 4.7s`).
- The Saturday drill already measured it: a 20-ask burst ran p90 103 s in the sidecar but ~381 s through the
  watcher (serial + MIN_GAP = 190 s floor). It's still listed as an open question.
- `MAX_PER_HOUR = 60` (`watcher.py:63`) is shared by replies, group posts and straggler DMs. On a busy Saturday
  morning (7:00 reminder, 9:00 nudges, 9:30 post, 10:30 nudges, plus the pre-lock rush) a member ask can hit
  `hourly cap hit, skipping` and be dropped.
- There's no queue-depth limit and no degrade path in the sidecar, apart from `BUSY_SKIP_BRIEF`.

**Fix (M).** Split receive from answer:
1. The watcher posts each row and returns at once. The sidecar answers into the outbox, and the watcher just
   sends.
2. Code lanes (fact, banter, playoff, jailbreak) skip the model queue entirely.
3. Cut the reply gap to ~3 s. Give replies their own hourly budget, separate from posts.
4. Give cover alerts priority over FIFO.

Needs a watcher redeploy (the `su picky` copy path already exists).

### R2. One picture request takes Picky offline for everyone for 3-7 minutes, and the 300 s cutoff is a bug.
**Evidence.**
- `chat._image_stream` unloads gemma first (`keep_alive: 0`, `chat.py:7246-7247`). Generations run 165-415 s
  (memory, 11/16 12:42-12:52).
- During that time the serial watcher is blocked on `ask_bot` (420 s timeout). Every other text and every outbox
  post waits, and a cover alert expires after 240 s. The next text then pays a cold model load plus a ~60 s
  prime.
- The "second ~300 s cutoff" nobody found is here: `httpx.Timeout(..., read=300.0)` at `chat.py:7241`, while
  `image.py` allows `MAX_GEN_SECONDS = 420`. A 300-420 s generation finishes on the GPU and the member still gets
  "Art department's closed".
- The per-user limit is 20 pictures an hour (`chat.py` 429 message).

**Fix (S).**
1. Set the read timeout to `MAX_GEN_SECONDS + 30`.
2. Make pictures asynchronous: reply "drawing it", generate, deliver through the outbox.
3. Queue or refuse pictures while any text is pending, and during game windows (Sat 11:00-24:00 CT).

### R3. Game-day prompt-prefix churn turns replies into 60-130 s cold primes.
**Evidence.**
- The ESPN market line and the FPI win probability are rendered inside `prompt_prefix()` (`render_context`,
  `chat.py:1862`, `chat.py:1873`), so every line move or probability change makes a new prefix.
- Live 11/17 10:45: a DM took 126.5 s. Of that, `prime: 59.8s (prefix 40d52c)` was the cold prefill, ~20 s went
  to the context build before it, then understand 16.4 s, a re-prime 4.6 s and the draft.
- Saturday 11/14 (`chat.stderr.log`): 34 distinct prefixes and 31 primes over 20 s that day.
- Keep-warm rebuilds only every 300 s, skips while a text is in flight, and skips whenever the exam lock is held
  (`group_chat.py:1394`; 25 `skip (exam lock held)` lines in the sidecar log).

**Fix (M).**
1. Move market lines, FPI and LINE GAPS out of the prefix into the tail or the card riders (ledger P15: open,
   never tried, so this is not a retry).
2. Trigger keep-warm on a data-change signal (`live_games.json` mtime, all_games `updated_ts`), not a 300 s
   timer.
3. Never let the exam lock stop keep-warm on game days.

### R4. Live runs straight from a tree that a dozen AI jobs edit, and autonomous loops ship to prod.
**Evidence.**
- `com.pickem.picky-group` started 117 times since 11/15 (53 on 11/17). `com.pickem.chat` started 188 times
  (48 on 11/17). Counted from `Started server process` in the stderr logs.
- During this review alone the sidecar PID changed twice (22748 → 29532) and commit 2dd4c00 landed at 20:19.
- Both services run `/Users/Agent/cfb-pickem/server/*.py` from the shared working tree, so any restart (the
  watchdog's kickstart included) loads whatever state that tree is in. The memory file records "uncommitted edits
  on main get wiped" by orchestrators.
- `flub_loop.fix` pushes Opus-written code to main and reloads both live services at night
  (`flub_loop.py:896-907`). It only logs the health check after the reload. There's no automatic rollback and no
  game-day guard. 23:00 Saturday is mid-West-Coast-games.
- `feed_monitor.repair` can ff-merge and push too (`feed_monitor.py:404-412`).
- Each restart drops the in-memory pile-on/spiral mute state and the warm prefix. The memory file records
  "~115 s cold after a reload".
- The production gate script lives outside the repo (`/Users/Agent/picky-keeper-tools/alltests.sh`,
  `flub_loop.py:65`). Its rule is "no NEW failures", so known-red tests are normal.
- GitHub CI runs no Python unit tests (only `css-check.yml` and `live-exam.yml`).

**Fix (S-M).**
1. Run live from a pinned release worktree (`/Users/Agent/picky-live` at a tag). Promote with one script:
   model-free suite, tag, switch, reload, canary DM, auto-rollback on failure.
2. Enforce a game-day freeze (Fri 18:00 to Sun 04:00 CT) in that script, in `flub_loop` and in `feed_monitor`.
3. Move `alltests.sh` into the repo.

### R5. Messages can vanish without a trace, and nobody would know.
**Evidence.**
- `handle_one` catches `URLError` (HTTP 500s included), logs `bot unreachable`, returns, and the main loop
  still advances `last_rowid` (`watcher.py:527-532`, `watcher.py:494-496`). A text that lands while the sidecar
  is restarting (53 times on 11/17), or that hits a 500, is never answered and never retried.
  `RemoteDisconnected` happens to retry only because it falls through to the generic handler (watcher log
  11/16 22:05).
- `/health` returns `{"ok": true}` without touching the pipeline (`group_chat.py:1165-1167`), so the watchdog's
  kickstart logic can't see a broken `/group`.
- `secs` is sidecar compute only. Watcher queueing and the AppleScript send aren't measured anywhere, so the
  "p90 ≤ 60 s" goal isn't measured end to end.
- Alerts go to a Claude memory file, and the one human notification calls the retired wit loop's script
  (`watchdog.py:58` → `/Users/Agent/picky-marathon/wit_eval.py notify`).

**Fix (S).**
1. Don't advance `last_rowid` until the reply is sent or 3 backoff tries fail. Then send a canned "give me a
   sec" line, not silence.
2. Log arrival → send latency per row in the watcher, plus a daily p50/p90 line.
3. Run a 15-minute canary through `/group` (test header, code lane) that checks the reply.
4. Repoint `NOTIFY` at a maintained path.

### R6. The accuracy stack validates prose with regexes, some against stale truth, and it cuts true facts.
**Evidence.**
- `chat._chat` wires 26 named send-point checks (`chat.py` ~7596-7625: record, grounded, member, team_fact,
  line_num, stat_rank, live, value, card_side ...). Then `group_chat.fact_final` re-runs member/result/casino
  checks (`group_chat.py:948-983`), and the brain adds stale-record, off-stance and stale-needle screens.
- The research doc lists the open cost: record/grounded checks still cut true card facts (e.g., a true last-meeting
  score, a sourced QB note).
- `fact_guard.team_fact_check` checks team W-L against the CFBD cache (`fact_guard.py:488-519`), which refreshes
  at 06:15 daily plus Sat 10:30 only (cfbd-refresh plist).
- So on Saturday night a true post-game record gets cut unless the prompt prints it next to the team, and the
  stale morning record passes. The guard enforces staleness. The live rider carries scores, not records.

**Fix (S, then M).**
1. Refresh CFBD records after finals on Saturdays (~16:30, 20:30, 00:30 CT). 50 of 1,000 calls are used this
   month.
2. Let `team_fact_check` accept records derived from today's finals in `live_games.json`.
3. Medium term: code lanes and cards emit the facts they used as data, and one checker validates claims against
   that set. The 26 sentence regexes don't each re-derive truth.

### R7. The router is a 57-rule first-match regex cascade, and it misroutes in production.
**Evidence.**
- `router.fact_intent` (`router.py:1037-1172`) is ~57 ordered `if X.search(t) and not re.search(...)` rules.
  Every fix adds another negative guard.
- I re-ran `router.classify` during the review on 11/17's live failures. A looks-rating question that contained a
  1-10 scale phrase routes to `fact:conf` via `CONF_ASK` (`router.py:225`), and the member got pool lines. A
  question about the site's blackjack dealer routes to `full` and also got pool lines.
- Both are queued as XFAIL flubs from 12:3x and were still failing at 20:15.

**Fix (S-M).**
1. Build a lane regression table (every real ask in the exchange log + exam items, with the expected lane) that
   runs in the model-free suite and in GitHub CI.
2. When two or more rules match, or a provocation co-occurs with a fact rule, fall through to the full brain
   (quality over speed, per Charlie).

### R8. Riders pile up, and the brain undoes chat.py's riders by regex on their wording.
**Evidence.**
- `chat._chat` splices 13 riders into the last user turn with the same copy-pasted
  `body[: -len(q)] + X + "\n\n" + q` block, repeated per rider (`chat.py` ~7430-7545). Prompts run 8.0-9.7k
  tokens per draft (sidecar `ollama prompt eval` lines).
- `picky_brain` strips chat's ESCALATE, OPTIONAL HOOK and BOARD VOICE riders with regexes keyed on their exact
  text (`picky_brain.py:1240-1266`). Reword a rider in `chat.py` and the strip silently stops working.
- `group_chat.py:423-503` monkeypatches 14 `chat` functions and calls `chat.chat()` through a fake Starlette
  request (`group_chat.py:1068-1083`). `picky_brain.install()` swaps `chat._ollama_stream` and `prompt_prefix`
  (`picky_brain.py:1268-1270`).
- Behavior depends on import order and on contextvars set by another module.

**Fix (M).**
1. A rider registry (name, builder, lanes, position), assembled in one place. The brain passes `exclude={...}`
   by name instead of regex-stripping.
2. Then extract `pipeline.answer(ctx, messages, options)` from `chat._chat`, so the sidecar calls a function, not
   an HTTP handler, and the monkeypatches become parameters.

### R9. The 16 GB box is in permanent swap, and AI jobs compete with live replies.
**Evidence (measured 20:19).**
- Memory and swap: memory_pressure 17-19% free, swap 10.3 of 11.3 GB used, compressor 3.3 GB holding ~18 GB.
  74.5M swapouts × 16 KB ≈ 1.2 TB swapped out in 4 d 23 h of uptime.
- Load: llama-server 7.8 GB RSS. Six `claude -p --model opus` jobs were running (77-150 MB each), plus a side
  sidecar and `eval/run.py --facts` against the same Ollama. Load average was 3.9.
- Stray `python -m http.server` processes on 8765, 8799, 8899 and 8931, 1-3 days old.
- One picture adds 6 GB.

**Fix (S).**
1. A game-day policy: no exams or side sidecars Sat 10:00-24:00 CT, at most 2 concurrent delegate jobs, pictures
   per R2.
2. Kill the stray servers.
3. Log free memory and swap delta next to each reply so the next regression is visible.

### R10. Autonomous publishing to a public repo.
**Evidence.**
- `flub_loop.publish` pushes items built from real member texts to the public repo every night
  (`flub_loop.py:622-660`). The only gate is a mapped-name list plus a phone/email regex
  (`flub_loop.py:583-590`). A member's off-color text that names nobody goes public.
- `docs/` (served by GitHub Pages from main) is written by unattended jobs and already carries member exchange
  details (`docs/picky-model-research.md:729`).
- The watcher log keeps 40-character previews of group texts (`picky-imessage.log`). It's local, so that one is
  acceptable.

**Fix (S).**
1. `PICKY_FLUB_PUBLISH=0` permanently (private items only; the exam already reads both).
2. Add a docs rule to the job specs: no member text, even paraphrased.

**Also seen (below the top 10).**
- The watcher needs a manual GUI login after every reboot. This is documented, but it's still a single point of
  failure on the box.
- The cover math is implemented about 10 times, and the copies already disagree on a push. `group_posts.covered_side`
  returns None and writes "pushes on the line" (`group_posts.py:242-244`, `group_posts.py:822-824`).
  `chat.covering_side` and `league_rules.cover_winner` give it to Away, like `set_winner_on_final`
  (`chat.py:2478-2485`). This is latent only because final pings are off.
- Web retries a whole request on a network blip (`js/picky.js:117-121`), so a Safari drop can run two full-brain
  jobs.
- When `chat._web_brain` falls back, it calls `_chat(req, False, ...)`, which runs a test request at member
  priority (`chat.py` `_web_brain`).

---

## 4. Simplification: delete or merge

**Delete. Each of these is FAILED/OFF in the ledger or a RULE violation waiting to happen.**
- The dormant hosted-Claude path in `chat.py`. It reads `DCC__ANTHROPIC_API_KEY` when `PICKY_USE_CLAUDE=1`
  (`chat.py:87`, `chat.py:521`). It breaks Charlie's hard rule the moment someone sets one env var.
- The `PICKY_BRAIN=0` legacy loop (`group_chat.py:1111-1145`). Its send-point chain has already diverged: no
  `debot`, no `live_games.final_guard`.
- The judge plus multi-draft sampling in `picky_brain` (`USE_JUDGE`, `JUDGE_*`, `SAMPLING[1:]`), `PICKY_FEWSHOT`,
  `PICKY_DRAFT_OPTIONS`, `PICKY_HOOK`, and the `PICKY_TEMPERATURE/TOP_P/TOP_K/REPEAT_PENALTY` knobs. All are
  ledger FAILED (B2, P4, O6-O8, P9).
- `tools/picky-lora/` and `PICKY_LORA_PAUSE` (M12/M13 retired).
- Phase 2 is about to add 3 more knobs (`PICKY_QREPEAT`, `PICKY_REPAIR`, `PICKY_NL_BRIEF`). Rule: a knob that
  loses its A/B gets deleted in the same PR, not left OFF.

**Merge.**
- **Cover math → one module.** `chat.cover_margin/covering_side/grade_pick`, `group_posts.cover_margin/covered_side`,
  `live_games.cover_status/side/margin`, `close_cover.cover_math`, `league_rules.cover_margin/cover_winner`,
  `burn_book._cover_margin`, `lore.cover_by`, `member_memory.grade_game_claim`, `picky_picks`. Add one test that
  pins the push rule against the SQL.
- **Scheduled jobs: 12 → 1.** league-material, burn-book, picky-picks, rules-watch, slate-intel, insider-refresh,
  game-cards, lore-refresh, picky-dossier, feed-monitor, news-qa and genius-refresh are all "tick and decide if
  due". Use one `com.pickem.scheduler` with a table: one log, one game-day freeze, one place to see what runs
  Saturday. Keep the KeepAlive services (chat, picky-group, live-games, close-cover, image) and the nightlies
  separate. Fold `com.charlie.picky-hottakes` into it.
- **`chat.py` split, mechanically, no behavior change:** context build, `render_context`/prefix, riders, checks,
  HTTP. 7,651 lines in one file is the main reason every job edits the same file at once.
- **Eval:** there are 13 entry points (run.py --text/--facts/--no-model, live_exam, genius, real_replay,
  saturday_drill, speed_replay, no_repeat_drill, judge_text, judge_quality, judge_agree, fact_judge, plus private
  spar/scorecard). Keep run.py (det. gates), live_exam, genius and real_replay. Freeze the rest.

---

## 5. Two-week roadmap, in order

**Before Saturday 11/21 (days 1-3, all S):**
1. Game-day freeze + pinned live worktree + `release.sh` with canary and auto-rollback (R4).
2. Watcher: retry instead of drop, end-to-end latency log, separate reply/post budgets (R5, part of R1).
3. Pictures: fix the 300 s timeout, make them async, block during game windows (R2).
4. Keep-warm on data change; ignore the exam lock on Saturdays; no exams 10:00-24:00 CT Saturday (R3, R9).
5. CFBD post-final refresh on Saturdays; `team_fact_check` accepts today's finals (R6).
6. Router lane table with today's misroutes as the first rows (R7).
7. `PICKY_FLUB_PUBLISH=0`, delete the dormant Claude path (R10, section 4).

**Saturday 11/21:** run unchanged under the freeze. Collect arrival → send p50/p90, queue depth, cold primes,
swap.

**Days 6-10 (M):**
8. Receive/answer split + code lanes off the model queue + alert priority (R1). Ship Tuesday so it has a
   weekday to bake.
9. Market/FPI/LINE GAPS out of `prompt_prefix()` (R3). A/B on the text exam for facts.
10. Rider registry; remove the regex strips (R8).

**Days 11-14 (S-M):**
11. Delete the dead knobs/paths; merge cover math with a pinned push test.
12. Scheduler consolidation (12 agents → 1).
13. Saturday 11/28: compare against 11/21's numbers. Success = group arrival → send p90 ≤ 60 s on the live log,
    0 dropped texts, 0 restarts during games.

---

## 6. What's NOT worth doing

- Bigger or more models, LoRA, ensembles, a local judge, hot sampling, few-shot banks: all ledger FAILED or
  REJECTED on this box. RAM (R9) rules out anything bigger anyway.
- A rewrite or a framework (LangChain, agents, a vector DB). The facts are small, structured and already in code.
  Retrieval isn't the bottleneck.
- More exam harnesses, or chasing ±0.2 judge scores. The ledger already says that's noise.
- More riders or more regex filters as the default fix for a flub. Fix the lane or the data instead.
- Per-intent prefixes (ledger P2, REJECTED) or a second checkpoint (P16) before R3's simpler move lands.
- In-game group pings, more proactive post types, more personas. Volume is 14 real replies a day. Reliability
  first.
- Moving off the Mac or onto paid APIs. Against the fixed constraints, and not needed at this load.

---

## 7. Needs Charlie's decision

1. **Game-day freeze window** (proposed Fri 18:00 to Sun 04:00 CT). Also: may the nightly flub autofix and the
   feed repair push to main and reload live at all, or only open a branch for a morning review?
2. **Pictures on Saturdays:** off during game windows, or queued? Also, lower the 20/hour per-user limit?
3. **Reply pacing:** drop the watcher's 10 s gap to ~3 s, and answer concurrently (code lanes immediately, brain
   queued). This was the open question from the Saturday drill.
4. **Load policy:** when more than N full-brain texts are queued, may Picky answer the next one from the fast
   lane? That trades depth for staying under 60 s.
5. **AI jobs on game day:** cap concurrent `claude -p` jobs, and no exams Saturday 10:00-24:00 CT.
6. **Public flub items:** stop publishing them (recommended).
7. **Delete the dormant hosted-Claude code path** (recommended; it exists only to break his own rule).
