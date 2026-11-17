# Picky upgrades 1-5 (Charlie approved by iMessage 2026-11-17: "Build 1-5 now")

Rules carried over: live replies = local gemma4:12b-it-qat only (offline `claude -p` Opus is fine), nothing
per-user/per-minute in `prompt_prefix()`, PICKY_ENABLED stays false, test traffic sends X-Picky-Test, exams run
from side sidecars in worktrees inside `lockf -t 14400 /tmp/picky-exam.lock`, one exam at a time, no rare_bits
state, private material only in cfb-pickem-private. LoRA stays paused.

## Baseline / acceptance (before and after, same harness)
`/private/tmp/picky-five-run/full.sh <worktree> <tag>`: model-free suite + `eval/run.py --no-model` + test_speed,
then genius (59 items, Opus judge), text exam (`eval/run.py --text`), live-80 replay (code-graded), each in its
own lock window. Before = main 9d1de83. Bar: genius >= 22/59 and up, invented 0, leaks 0, test_speed green.

## 1. Nightly flub loop (`server/flub_loop.py`, LaunchAgent com.pickem.flub-nightly 23:00 CT)
harvest (the day's live exchanges from picky_exchanges.jsonl + signals parsed from logs/picky-group.stderr.log:
sentence-filter drops, guard regens, code-answer fallbacks, dodges, banter cuts, replies > 60 s, member pushback
in the next text) + the private flub queue (item 5, highest priority) -> one offline Opus pass classifies flubs
and writes exam items whose keys are re-checked in code against the facts bundle -> public items anonymized in
`server/eval/flub_items.json`, member-specific ones in `cfb-pickem-private/flubs/items-private.json` -> both
join the text exam's regression set -> optional Opus fix in a throwaway worktree that ships only if model-free
tests pass and the exam doesn't regress (det. pass >= baseline, invented 0, leaks 0) -> morning report
`cfb-pickem-private/flubs/report-<date>.md`. Never texts anyone.

## 2. Unit-vs-unit matchups (slate_cards.py)
Code-computed unit edges from the CFBD cache only (advanced + team_stats; no new calls): rush O vs rush D,
pass O vs pass D, pass rush vs protection (sack rates, front-seven havoc), red zone (points per opportunity),
turnovers/havoc, explosiveness, pace (plays per game). Ranked across FBS and stated as decided words. A unit
question puts the relevant edge first ("UNIT QUESTION:" line) so the model answers with that unit, not overall
SP+. Pool games get the same edges. Genius items added.

## 3. Exact sub-question + "why?" lane
Sub-question router: lookahead/letdown -> rest/next-opponent facts, travel/body clock/time zone -> static venue
table (CFBD venues: tz + elevation, team home venue from the schedule), altitude -> elevation, weather -> Open-Meteo
forecast cached per game on the schedule (free, no key; never per message). The matched fact rides first.
"why?" / "why not?" / "how come" follow-up lane: the NEXT unused reason from that game's card, never turn 1's.
Exam items added.

## 4. Speed (p95 < 60 s every reply)
Found in the logs: (a) a cold prefix re-prefill (`prime: 59 s` / `prime failed`) on the first text after any other
gemma user evicts the cache; (b) a reroll draft runs whenever draft 1 had ANY sentence cut, then draft 1 is sent
anyway most of the time (20-35 s wasted on group follow-ups, whose tails are bigger); (c) queueing behind exam
sidecars. Fixes: reroll only when it fits the time budget or draft 1 is unusable; idle keep-warm prime in the
live sidecar (skipped while the exam lock is held, off in side sidecars). Measured before/after on a replay set
(keeper convos with group follow-ups, an evictor between conversations). test_speed budgets stay green.

## 5. "bad picky"
`server/flag_flub.py --text ... --image ... [--note]` -> `cfb-pickem-private/flubs/queue.jsonl` (priority high,
plus the last exchanges for context). Coordinator CLAUDE.md routing row: "bad picky" from Charlie -> run it, reply
"Logged, it'll be in tonight's fix round". Sidecar: a mapped member texting "@Picky bad picky" / "@Picky that's
wrong" (or in a DM) logs the previous exchange to the same queue; no extra reply spam.

## Order
Plan -> baseline chain (background) -> item 4 + items 2/3 + items 1/5 in separate worktrees (Opus subagents) ->
merge one at a time, model-free suite each time, push, reload chat + picky-group -> after-chain -> memory + text.
