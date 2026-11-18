# LaunchAgent changes waiting for the integration lead

Nothing below has been loaded, unloaded or copied on the live box. Apply after the release that carries the
branch. Each entry says which phase it came from.

## phase-8-zero-maintenance (2026-11-18)

No new LaunchAgent. The Supabase keepalive rides `com.pickem.feed-monitor` (already loaded, every 15 min), and
every other change is code that release.sh ships and reloads.

1. **Copy two plists (comment-only change, no reload needed).** The header comments now describe the capped
   Claude booster gate, and the old comments held `--` (invalid XML for strict parsers like Python's plistlib).
   ```
   cp server/com.pickem.game-cards.plist server/com.pickem.picky-dossier.plist ~/Library/LaunchAgents/
   ```
2. **Keep the hot-takes refresher loaded** (`com.charlie.picky-hottakes`, Sun + Wed 10:00, runs
   `/Users/Agent/cfb-pickem-private/refresh_hot_takes.sh`). It's a Claude booster now: `picky_claude.py run` goes
   through the capped gate and exits 3 when the gate is shut (the script logs SKIP and keeps the bank). Optional
   one-line tidy in that private script (not in this repo): line 46's log text says "PICKY_NO_ANTHROPIC set"; exit 3
   now also means logged out / cap hit / no binary.
3. **Unload `com.pickem.news-qa`** (daily 06:40), optional. It only feeds code repair, which is hand-only now, so
   every scheduled run logs `"result": "skipped"`; news.py logs feed health itself.
   ```
   launchctl bootout gui/$(id -u)/com.pickem.news-qa
   mkdir -p ~/Library/LaunchAgents/disabled && mv ~/Library/LaunchAgents/com.pickem.news-qa.plist ~/Library/LaunchAgents/disabled/
   ```
4. **Keep loaded, no change:** every other `com.pickem.*` job. The boosters (slate-intel, picky-dossier,
   game-cards, insider-refresh, picky-burn-book, league-material's banter, rules-watch, flub-nightly) use Claude
   only through the gate and fall back to code / gemma by themselves. `com.pickem.genius-nightly` /
   `com.pickem.genius-refresh` are exam tooling and need Claude: unload them if the subscription is ever cancelled.
5. **After release.sh reloads the sidecars**, check:
   - the keepalive ran once: `cat server/cache/supabase_keepalive.json` shows `"last_status": 200` within 15 minutes
     of feed_monitor's next pass;
   - the booster gate: `server/.venv/bin/python3 -I server/picky_claude.py usage --days 1` ends with
     `booster gate now: open` (or the reason it's shut).
