# Picky: zero maintenance (phase 8, 2026-11-18)

Charlie: "The key is no maintenance. If I cancel Anthropic or if I log in after 5 months away it should just all
be working perfectly."

What that means in code, what Picky still depends on, and what to do if something breaks. Tests:
`server/test_zero_maintenance.py` (Claude up with the cap, Claude missing / failing / logged out / capped,
the clock 5 months on, next August, Supabase paused).

## 1. Claude is an optional, capped booster

Charlie, 2026-11-18: "If Claude is active, Claude can help Picky with the advanced analysis. If not, Picky should
still be able to exist even though his takes may be less good. I really don't want to spend a bunch on Claude or
make him over-reliant."

One gate, `server/picky_claude.py` (`why_not()` / `available()` / `guard()`). A Claude call goes through only when
**all** of these hold:

- the kill switch is off (`PICKY_NO_ANTHROPIC` unset or `0`);
- the process is **not a live reply path** (`LIVE_ENTRIES`: `chat.py`, `group_chat.py`, `canary.py`,
  `watcher.py`). Live replies never call or wait on Claude, not even by hand;
- the `claude` binary is installed;
- `claude auth status` says logged in on the **claude.ai subscription** (cached 6 h). `ANTHROPIC_API_KEY` /
  `ANTHROPIC_AUTH_TOKEN` are stripped from every run, so nothing can bill an API key. A run that comes back "please
  log in" or "usage limit" turns boosters off right away until the next check;
- today's booster use (CT day) is under the **daily cap: 12 calls and 45 minutes** (`DAILY_CALLS`,
  `DAILY_MINUTES`). Before the cap, scheduled jobs used about 4 to 19 calls a day; exams are separate.

Exam tooling (`server/eval/**`) and hand runs (`PICKY_OPUS_MANUAL=1 <script> ...`) skip the auth check and the cap
(they're not boosters). No plist sets `PICKY_OPUS_MANUAL`; the tests check that.

Every call is logged in `cfb-pickem-private/claude_usage/<date>.jsonl` with `"booster": true` when it counts toward
the cap; a call the cap refused logs `"refused: daily cap"`. See where you are:
`server/.venv/bin/python3 -I server/picky_claude.py usage` (prints `booster N/12 calls, M/45 min` per day and the
gate's current state).

**Every booster has an automatic code / gemma fallback** that runs when the gate says no, the call fails or it times
out, and output says where it came from (`by: claude|code`, `researcher: code ... + Opus layer`, a card case `by:
opus|dossier`). `test_zero_maintenance.py` holds a registry (`BOOSTERS`) of every Claude call site and the test that
proves its fallback; a new call site with no fallback test fails the suite, as do a live process reaching Claude,
the Anthropic SDK, or api.anthropic.com.

### Every Claude call on a schedule, and its fallback

| Job (LaunchAgent) | Claude booster (gate open) | Fallback (gate shut / call failed) | Who reads it |
|---|---|---|---|
| slate_intel (com.pickem.slate-intel, hourly) | one Opus web-research call at the **Fri 5 PM and Sat 7 AM CT** runs: injury/QB notes for notable non-pool games, coaching changes. Rides on top of the code notes for 36 h; games tagged `by: claude` | **code**, every hour: news-cache stories through the dossier's code researcher + validator (`by: code`); CFP poll from ESPN's free rankings API (dropped 50 days after release); coaching changes from headlines that lead with an FBS school firing / naming an interim (speculation like "deserve firing" skipped). Bad ESPN format: last good list + health line | slate_cards |
| dossier (30 min) | weekly Opus research layer + analyst take (Fri 5 PM to Sun) | code dossier (news cache + r/CFB, validated) plus the last Opus layer while fresh (6 days) | `dossier_note`, group posts, cards |
| game_cards (30 min) | one Opus case per pool game per week after the Tue line lock | the dossier's fresh analyst take, else the code card (facts, lines, units, the code lean) | `card_note`, why lane |
| insider_refresh (2 h) | the insider notes file, at most once a day when 3+ days old | the code wire (insider headlines from news.py, every 2 h) and the last notes; a failed booster is not a failed job (exit 0) | `chat.insider_note` |
| burn_book (hourly) | 480 written comeback lines, Sun 9 PM + Wed 10 AM slots | last batch kept; every tick rebuilds the **code pool** from fresh facts; lines whose facts went stale stop being offered | router banter lanes |
| banter (rides league_material) | weekly banter bank | last bank; the burn book's generic lines + code pool | ammo rider |
| league_rules / rules-watch (hourly) | Opus review of a regenerated rules section after a code change | **code verifies**: a changed section that extracts cleanly, passes the deterministic checks and moved with the code is stated; a change the generator can't see stays flagged and Picky hedges | `league_rules.live.md` |
| flub_loop (23:00) | Opus classify + fix round | harvest, code classify and report always run; the Opus parts skip | private morning report |
| hot takes (com.charlie.picky-hottakes, Sun + Wed; script in cfb-pickem-private) | `hot_takes_bank.md` via `picky_claude.py run` | exit 3 when the gate is shut: the script logs SKIP and keeps the bank; past 10 days the rider goes quiet and gemma answers opinions in its own voice | `takes_note` rider |
| genius nightly / refresh (eval) | the exam (not a booster, not capped) | n/a | the integration lead |

**Not boosters (no Claude on the schedule at all):**

| Job | Now |
|---|---|
| league_material (hourly), member_memory (22:30), group_posts recap + week_breakdown | **local gemma** (`local_llm.py`) with the same prompts, schemas and validators. Failed draft = previous sheet / rows wait a night / the code template |
| feed_monitor (15 min) | no code repair on the schedule (agentic repair isn't analysis; by hand with `PICKY_OPUS_MANUAL=1`). It kicks a stale refresher, refreshers keep their last good copy (`feed_health.py`), and it runs the Supabase keepalive |
| news (5 min) / news_qa (daily) | news.py logs a feed-health line when a feed that had items returns 0 or errors (cached items kept). news_qa only feeds code repair, so it's hand-only and logs "skipped" (unload it, see plist-changes) |

The live reply path (router lanes + gemma) never used Claude; `test_anthropic_down.py` covers it with every
Claude-fed file missing.

## 2. Keys and tokens Picky needs

All secrets live in the login keychain, account `drake` (`security find-generic-password -a drake -s <name> -w`).

| What | Keychain name / where | Used by | Expires? | If it dies |
|---|---|---|---|---|
| CFBD API key | `CFB_PICKEM__CFBD_API_KEY` | cfbd.py (daily refresh; stats, records, lines, venues, coaches) | No (free tier; only revoked for abuse). Budget: about 500 calls/month, enforced in cfbd.py | Refresh fails, the cache keeps the last copy; replies use it with "as of" hedges until the hard age (4 days), then drop those facts. **Replies keep working** |
| Supabase service key | `CFB_PICKEM__SUPABASE_SERVICE_KEY` (a `sb_secret_` key) | group_chat (Picky's session, member DMs), lore, picky_picks, dossier, league_material, member_memory | No (revocable in the dashboard) | League facts can't load: Picky sends an honest "league database isn't answering" line (no crash, no 500); banter still needs the DB for context, so most texts get that line |
| Supabase anon key | in `chat.py` / `keepalive.py` (public, the site ships it) | site reads, keepalive ping | JWT exp **2035-10-28** | Keepalive fails (health line); the site breaks first |
| Supabase access token | `CFB_PICKEM__SUPABASE_ACCESS_TOKEN` | rules-watch live checks only | Personal access token (may have an expiry set) | rules-watch runs on the repo's migrations only ("live db unavailable"). **Non-fatal** |
| Picky group secret | `CFB_PICKEM__PICKY_GROUP_SECRET` | watcher → sidecar auth | No | Watcher gets 401s: no replies. Only changes if someone rotates it |
| Apple ID (Picky's iMessage) | `CFB_PICKEM__PICKY_APPLEID_EMAIL`, Mac user `picky` (`CFB_PICKEM__PICKY_MAC_USER_PASSWORD`) | Messages.app, the watcher | Apple can sign the account out (password change, security check, 2FA re-prompt) | **This one is fatal for texting**: Picky can't send or receive until someone signs Messages back in as `picky`. The watchdog notices failed sends and texts Charlie once. Nothing else breaks |
| GitHub (release.sh, `git push`) | macOS keychain via `gh` (account cfb-pickem, `gho_` OAuth token, https) | tools/release/release.sh and pushes only | OAuth tokens don't expire, but GitHub revokes one that goes **unused for a year** | Releases can't push; nothing running is affected. Fix: `gh auth login` as cfb-pickem |
| Anthropic / Claude | claude.ai subscription login (`claude auth status`), never an API key | the boosters above, exams | The subscription can be cancelled or the login can lapse | Boosters switch off on their own (the auth check); every job runs its code / gemma fallback. **Replies never notice**; takes get plainer |
| ESPN, Open-Meteo, news RSS | no keys | live scores, rankings, weather, news | n/a (unofficial APIs can change shape) | `feed_health.log` line, last good copy kept, "as of" ages apply |
| Ollama gemma4:12b-it-qat | local model file | every reply's voice line, local writers | No | `maintenance.py` canned lines in the chat; local writers keep their last copy / templates |

None of these expiring stops the reply path except the Apple ID (no iMessage, no texts) and the Supabase project
itself being gone. Supabase free projects that stay **paused for 90 days can't be restored**, which is why the
keepalive exists.

## 3. Supabase keepalive

Free projects pause after about 7 days with no activity. `server/keepalive.py` reads one public row (the site's
`logos` table, anon key) at most once a day, called from feed_monitor's 15-minute tick, so the offseason never
idles the project. State: `server/cache/supabase_keepalive.json`. A failed ping writes a health line; a paused
project (HTTP 540 / "paused") is recorded and the group/DM reply says so instead of erroring.

## 4. The calendar

`server/season.py`: the season is derived from the date (Jul-Dec = that year, Jan-Jun = the year before) and the
league's own data (the newest `cfb_season` in `all_games`, the smallest week with a non-final pool game).
**Offseason** = every pool game of the newest week is final and the last one kicked off 21+ days ago. Then "this
week" questions (lock, pool games, lines, picks, scores...) get a code answer: the season's in the books, the final
top 3, nothing to pick until next season's pool games go up. The full brain gets one OFFSEASON line in its
per-request tail. Standings and records still answer from the final data. When the 2027 pool games are posted, the
season and week 1 come from the data with no code change.

Fixed for the rollover: the dossier pruner kept week dirs by number (2027's week 1 would have been deleted the
moment it was written; now by write time); the interim lane's "since <date>" used a fixed 2026; slate_intel's CFP
list drops 50 days after release. The league's own rule `LAST_REGULAR_WEEK = 15` stays: it's league config that the
site defines too, and rules-watch catches drift between them.

## 5. Feed format changes and empty data

`server/feed_health.py`: `accept(name, new, prev)` keeps the last good copy when a feed returns the wrong type or
comes back empty (a new season's empty data is allowed). Used by cfbd.py (every dataset), slate_intel (ESPN
rankings) and news.py (a feed that had items and now returns none). One line per event in
`server/logs/feed_health.log`. No model involved.

---

## If something breaks (one page)

1. **Picky isn't answering texts at all.** On the Mac mini: `launchctl list | grep pickem` (look for
   `com.pickem.picky-group`, `com.pickem.picky-imessage`). Log in as user `picky` and open Messages: if it's asking
   for the Apple ID password, sign in again. That's the one thing that can't fix itself.
2. **Picky answers "the league database isn't answering".** Open the Supabase dashboard, project
   `vopdioszofwdkwnujtiq`. If it says **Paused**, click Restore (it must happen within 90 days of pausing). Then
   check `server/cache/supabase_keepalive.json` and `server/logs/feed_health.log` for why the keepalive stopped
   (is `com.pickem.feed-monitor` loaded?).
3. **Answers sound stale ("as of last Tuesday").** `tail server/logs/feed_health.log` shows which feed broke. A
   free API changed shape: the last good copy keeps Picky honest until someone updates the parser. `cfbd.py
   refresh` by hand shows CFBD errors (key, budget).
4. **Canned "getting my brain worked on" lines.** Ollama is down: `curl -s localhost:11434/api/tags`. Restart the
   Ollama app; the model is local, nothing to download unless it was deleted (`ollama pull gemma4:12b-it-qat`).
5. **A rules answer says the rule changed and to check the site.** rules-watch saw a code change it couldn't
   confirm from code. `cd server && ./.venv/bin/python3 -I league_rules.py --write` after reading the change
   regenerates the committed digest.
6. **Releases fail to push.** `gh auth status`; if cfb-pickem's token is gone, `gh auth login`.
7. **Takes got plainer / "is Claude helping?"** `server/.venv/bin/python3 -I server/picky_claude.py usage` shows
   today's booster calls against the cap and why the gate is shut (not logged in, cap hit, binary missing). Log
   back in with `claude auth login` if you want the boosters back; nothing else needs it.
8. **Nothing to do for**: a cancelled Anthropic subscription (boosters switch off, fallbacks run), the offseason
   (it's detected from the data), next August (week 1 comes from the posted pool games).
