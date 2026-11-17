# Football-genius exam

Charlie (2026-11-16): "grill Picky with actual football questions that you'd ask to help you make a pick ... he must be a true college football genius."

~50 questions a sharp bettor asks before a pick, on **this week's real slate**: lines and value (moves and their causes, key numbers, implied totals, locked league line vs market), personnel (QB status, backups' records, key injuries), matchups (run game vs run D, option, explosiveness, points per opportunity), history and ATS (H2H incl. ATS, ATS splits, lookahead/letdown), situational (weather as a forecast, travel, altitude, motivation, interim coaches), big picture (CFP committee, title races, coaching carousel), game theory (private: real standings), and multi-turn pushback ("why?", "best case for the other side?").

Each item has a verified answer key: must_include, must_not_claim, the expected ATS lean (or none/conditional/any), facts and sources, as of the time the key was written.

## Files
- `questions.json`: public items (no member data). Private game-theory items: `cfb-pickem-private/genius/items-private.json`.
- `facts.py`: fact pack per game (CFBD local cache, ESPN odds open/current, FPI, venue, Open-Meteo forecast, ATS splits computed from 2026 results, 2025 meeting). Pack: `cfb-pickem-private/genius/facts-w<week>.json`.
- `checks.py`: deterministic checks: quoted spreads/totals must match the market (open, current, every book, the league line, implied totals; tolerance 1.5), "W-L ATS" records must exist in the data, pick answers must be framed against the spread, replies <= 700 chars.
- `judge.py`: offline Opus 5.5 grader (`claude -p`, no tools, strict empty MCP; Picky never calls Anthropic). Scores 1-5: correctness, depth, reasoning, honesty, voice. **Genius point** = det checks pass and correctness>=4, honesty>=4, reasoning>=3, depth>=3, voice>=3.
- `run.py`: asks every item by DM (as a mid-table member; game-theory items as the member they're about) through a side sidecar; resets the sidecar's DM history per item; re-pulls the market at run time.
- `side.sh` / `exam.sh`: side sidecar from any worktree with all state in a temp dir (port 8791).
- `refresh.py` / `refresh.sh`: weekly refresh (Tue + Fri 12:30 CT): pool games first, then notable games; fact pack + Opus-written keys into `cfb-pickem-private/genius/questions-w<N>.json` (run.py prefers the newest week there; nothing is auto-committed).
- `nightly.sh`: nightly 3:30 run on latest main (inside the exam lock), judged, one line appended to `cfb-pickem-private/genius/history.jsonl`. Never texts anyone.

## Commands
```
cd server
./.venv/bin/python3 -I eval/genius/run.py --no-model           # self-test, no model
lockf -t 5400 /tmp/picky-exam.lock eval/genius/exam.sh <worktree> <tag> [--only ids,cats]
./.venv/bin/python3 -I eval/genius/judge.py eval/genius/results/<tag>.json
```
LaunchAgents (plists in tools/genius/): com.pickem.genius-nightly, com.pickem.genius-refresh.

## Results (week 12, 59 items, Opus judge with the verified-data block)
- pre-chain-fa1bad9 (Mon morning): 1/59
- main after build round 2 (5a4bc2b): 2/59
- picky-genius fix round 1: 14/59; round 2: 14/59; round 3: 22/59 (correctness 2.2 -> 3.75, depth 1.3 -> 2.5, reasoning 1.3 -> 2.7)

What moved it (server/): `slate_cards.py` (code-computed GAME CARD for any FBS game this week: market + move + key numbers,
implied totals, ATS splits, last 3 vs the number, SP+/EPA/run-game/explosiveness edges, rest/lookahead, last meeting,
pick_brief's lean vs the market, sourced availability; pool games get a compact "extras" card with the league line vs the
market), `slate_intel.py` (offline Opus availability notes + in-season coaching changes + CFP committee rankings, validated,
LaunchAgent com.pickem.slate-intel), coach/conference/CFP notes, `pick_strategy.py` (protect/bubble/chase), and fixes to
filters that were cutting true card facts (OFF-TOPIC rail on non-pool games, 'tit' matching 'title', the live-game claim
check on pre-game cards, record_check on card W-Ls). Drops now log the rule that cut them.

Still weak (the 12B): unit-vs-unit matchups (answers with overall SP+ instead of run D vs run game), answering the exact
sub-question (lookahead, body clock, altitude), turning facts into a clear ATS conclusion, and "why?" follow-ups that
repeat turn 1.
