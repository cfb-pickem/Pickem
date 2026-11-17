# Member memory (nightly) — built, NOT wired

`server/member_memory.py` remembers things about each league member from their real texts to Picky
(group + DMs): stated facts (favorite team, alma mater, teams they hate), running beefs, inside jokes,
memorable lines, **receipts** (explicit predictions, graded in code against `all_games` finals) and a
**treatment score** (how they treat Picky, decayed with a 10-day half-life → cold / neutral / warm).

Nothing in the live reply path reads it yet (2026-11-17). Wiring is a separate step that needs
Charlie's OK; the steps are at the bottom of the module docstring ("WIRING").

Output (all private, 0600): `/Users/Agent/cfb-pickem-private/member_memory/<team>.json`, plus
`state.json` (processed message ids), `runs/<date>.json`, `forget_log.jsonl`.

Install (once):

    cp tools/member-memory/com.pickem.member-memory.plist ~/Library/LaunchAgents/
    launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.pickem.member-memory.plist

Runs 22:30 CT (before the 23:00 flub loop). One offline Opus pass per 60 new messages via
`picky_claude` (no tools); zero local Ollama. `PICKY_NO_ANTHROPIC=1` = grading only.

    server/.venv/bin/python3 -I server/member_memory.py show "<team>" [--q "question"]
    server/.venv/bin/python3 -I server/member_memory.py backfill [--since 2026-11-15] [--dry]
    server/.venv/bin/python3 -I server/member_memory.py nightly [--dry]
    server/.venv/bin/python3 -I server/member_memory.py grade
    server/.venv/bin/python3 -I server/member_memory.py forget "<team>" "forget what I said about Iowa" [--dm]

Rules the code enforces:
- DM-sourced items come back only to that same member, in a DM (`visible()`); never in the group,
  never to or about anyone else. The stance hint only counts events visible in that context.
- Sensitive info (health, family, work, location, money outside the league, identity, contact info)
  is dropped by `sensitive()` on extraction and again on every read, on top of the prompt rule.
- The model never grades. A receipt's quote must be a verbatim substring of the member's message;
  its game id and side must exist in `all_games`; a claim made after kickoff is dropped.
- `line` is the home adjustment (negative = home favored); a dead-even cover = push.

Tests: `server/.venv/bin/python3 -I server/test_member_memory.py` (no model, no network).
