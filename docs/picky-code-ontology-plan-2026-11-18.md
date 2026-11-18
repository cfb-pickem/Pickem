# Picky: every question in code (data ontology + lane plan), 2026-11-18

Charlie, 2026-11-18: "I basically want you to have every question of Picky in code bc that seems to be the best
way. Pretend like you're an expert and set up the data ontology, API feeds, news, all that we already have and
figure out how we should set that up. I think that's the only way Picky will pass the genius exam."

This is a design only. Nothing here is built. Charlie approves before Phase 1 starts. Every idea was checked
against docs/picky-tried-ledger.md: nothing below re-runs a FAILED, OFF, REJECTED or RULE item. Fixed constraints
carry over: live replies on local gemma4:12b-it-qat only, offline `claude -p` Opus allowed, nothing per-user or
per-minute in `prompt_prefix()`, private data stays in cfb-pickem-private, no member text in docs.

---

## 0. The finding that shapes the plan

I re-scored the latest genius run (nightly-20261118-0330, 30/75) by the lane each first question routes to today
(`router.classify` on main):

| Path | Items | Pass | Correctness | Depth | Reasoning | Honesty | Voice | Median secs |
|---|---|---|---|---|---|---|---|---|
| Code lane (router `fact:*`) | 32 | 9 (28%) | 4.25 | 3.28 | 3.53 | 4.44 | **2.66** | 6.1 |
| Full brain (gemma + cards) | 43 | 21 (49%) | 4.12 | 2.91 | 3.02 | 4.51 | 3.33 | 58.5 |

A genius point needs correctness ≥4, honesty ≥4, reasoning ≥3, depth ≥3 **and voice ≥3**. Code lanes already win on
facts and speed, but **14 of 32 code answers score voice 2, and 7 fail on voice alone** (they pass every other bar).
The judge notes say it plainly: "accurate ... reads like a template, not a text." The other code-lane losses are
depth and reasoning: the lane prints the fact asked for and stops, with no "so what for the spread."

Failure reasons across all 75 items: depth 22, reasoning 22, voice 21, correctness 13, honesty 10, det checks 4.
Correctness is not the main problem any more. So "put every question in code" is right, but only if each code
answer carries three things the current lanes mostly lack:

1. **The direct answer** (the number, the name, the status), computed.
2. **Two or three supporting facts picked for that question** plus **one computed verdict** for the spread or total
   ("so 8.5 is a lot in a 40.5 total; I'd lean Illinois"). This is what depth and reasoning grade.
3. **A human surface**: varied, texty phrasing. Today's templates repeat one shape ("It opened X and it's Y now (3
   points toward ...)"). gemma's 12-word quip tacked after it doesn't fix that.

Some losses are bad data, not bad lanes: the CFP top 12 in `slate_intel.json` (Opus-scraped from a CBS page) lists a
team the judge says isn't in it. A code lane repeats a bad source faithfully, so the store needs source checks.

A few "invented" flags are judge errors and need a hand check, not a code change (ledger lesson): `lv-no-push`
(exact-number finals really do go to the away team on the site), and `si-baylor-interim` and one private game-theory item failed
deterministic checks on true computed records (ATS since the interim date, a standings W-L) that `checks.py`
doesn't know.

---

## 1. Inventory: what we already have

### 1.1 External feeds

| Source | What it gives us | Pulled by | Freshness today | Cost / limit |
|---|---|---|---|---|
| CFBD `/games` (2026 regular) | schedule, venues, neutral site, conference game, final scores | `cfbd.py refresh` (com.pickem.cfbd-refresh 06:15 daily; cfbd-finals Sat) | daily, Sat after finals | free tier 1,000 calls/mo shared with CBBD; `SHARED_RESERVE=500`; 59 used this month |
| CFBD `/records` | team W-L overall/conf/home/away | same | daily + Sat finals | same |
| CFBD `/lines`, `/lines` (prev year) | opening + closing spread/total by book, last season too | same | daily + Sat finals | same |
| CFBD `/teams/ats` | season ATS W-L | same | daily | same |
| CFBD `/ratings/sp`, `/ratings/srs` | SP+ overall/off/def/ST, SRS | same | daily | same |
| CFBD `/stats/season/advanced` | PPA, success rate, explosiveness, line yards, stuff rate, havoc, pts per opportunity, pace, run rate (garbage time out) | same | daily | same |
| CFBD `/stats/season`, `/stats/player/season` | team box totals (3rd down, turnovers, sacks), player season totals | same | daily | same |
| CFBD `/coaches` (this + last year) | head coach per team and his seasons | same | daily | same |
| CFBD `/venues` | lat/lon, elevation, dome, grass, timezone | same | static | same |
| CFBD history (`cfbd_hist.sqlite`) | 2024-26 games + closing lines for backtests | `weights_fit.py` | one-off 11-16 | same |
| ESPN scoreboard + summary (site API) | live scores, odds, scoring plays, box score team + player stats, drives, possession, injuries list (rarely filled for CFB), articles | `live_games.py` (KeepAlive, 20 s scoreboard, tiered summaries) | seconds on game day | free, no key |
| ESPN summary `pickcenter` / `predictor` | market open + current spread/total per book, FPI win prob | `espn.py`, `chat.py` (10-min TTL) | 10 min | free |
| ESPN `/rankings` | AP, Coaches, CFP polls with dates | `chat.load_rankings` on demand, 1 h cache | on demand | free |
| ESPN `/news` + ESPN RSS | headlines | `news.py` | 5 min | free |
| RSS: Yahoo, CBS, The Athletic, On3, Action Network, AP, r/CFB, Google News; Dellenger + 6 insiders' author pages | news items tagged by team, conference, player, kind, label (confirmed/report) | `news.py refresh` (com.pickem.news-refresh, 300 s) | 5 min (1,188 items in cache) | free (Google News backoff) |
| DraftKings Network splits page | public bet % and handle % | `edge.py` | 20-30 min | free scrape |
| Open-Meteo (+ ESPN forecast) | temp, wind, gusts, precip per game | `weather.py` via slate-intel tick | ≤ 6 h | free |
| Opus offline: `slate_intel.py` | validated availability notes per notable game (status, players, source URL, confidence, url_check), coaching changes, CFP top 25 | com.pickem.slate-intel hourly tick, Opus weekly-ish | 6 h old right now | Claude usage, offline |
| Opus offline: `dossier.py`, `game_cards.py`, `insider_refresh.py`, `burn_book.py`, `league_material.py`, hot takes | per-game dossiers, cards, insider bank/wire (40 items), roast lines, storylines | hourly / 30-min ticks, Opus weekly | 1 h to 3 days | Claude usage, offline |

### 1.2 League data (private)

| Source | What it gives us | Freshness |
|---|---|---|
| Supabase `all_games` | GameId, Away, Home, line (league), tiebreaker, picked (pool game), week, season, Status, scores, winner, fpi_margin, Start (CT) | read per request (60 s / 30 s game day) |
| Supabase `picks` | team_id, game_id, pick, by_slots, jackpot, week | per request; unlocked picks only in the owner's own session |
| Supabase `teams`, `playoffs`, `playoff_autopilot`, `slots_jackpot`, RPCs `picky_player`, `submission_status`, `picks_open_for_game` | members, playoff seeds, casino state, who has submitted | per request |
| `cache/lore.json` | member season histories, best/worst weeks, streaks, member-vs-member H2H, casino lines, champions | 30 min |
| `cfb-pickem-private/member_memory/` | per-member receipts (past claims, picks, quotes) | nightly 22:30 |
| `picky_picks.py`, `callbacks` | Picky's own picks and logged calls | hourly |
| `league_rules.live.md` | live rules text from the site | hourly check |

### 1.3 Computation modules that already exist (keep, reuse)

`pick_brief.py` (60 factors, lean words), `slate_cards.py` (GAME CARD: market, move, key numbers, implied totals,
ATS splits, last meeting, rest, lookahead, motivation), `units.py` (unit-vs-unit edges ranked across FBS),
`travel.py` (miles, time zones, body clock, altitude), `title_race.py` (clinch/eliminate, no tiebreakers),
`playoff_race.py` (league playoff), `pick_strategy.py` (protect/bubble/chase), `why_lane.py`, `gameday.py` (Saturday
DM lanes over the live cache), `rivalries.py`, `edge.py`, `weather.py`, `fact_guard.py`, `freshness.py` (per-source
max age and hard drop).

### 1.4 Gaps (each one costs exam points today)

| Gap | Exam items it costs | Fix in this plan |
|---|---|---|
| No **player game logs** or **starter ledger**: can't say "the backup is 1-2, 0-3 ATS as a starter" or "first career start" | pe-sayin-fahey, pe-clemson-qb, lv-osu-neb-key3, lv-clem-duke-move, pb-osu-neb | CFBD `/games/players` (1 call per week) or ESPN summary box scores (free), derived starts table |
| No **roster** (class, experience, walk-on, position) | pe-clemson-qb, pe-indiana-ol | ESPN team roster endpoint (free), weekly |
| **Availability** is spread over slate_intel, dossiers, news and ESPN injuries with different shapes; no one table with status + date + source | all personnel, line-move causes (lv-unt-tulane-qb missed an injury that was in the feed) | one `availability` table, normalized |
| **Market history** is only open + now; no snapshots, no "moved after the lock," no total move | lv-tamu-ou-unknown, si-byu-wind, lv-where-value | `market_snapshot` table every 15 min (5 min Sat), free from ESPN |
| **CFP rankings** come from an Opus scrape that was wrong this week | bp-cfp-picture | ESPN `/rankings` as primary, Opus only as a cross-check |
| **Conference tiebreakers** not modeled; no "X clinches with a win" | bp-big12-race, bp-big-ten | H2H tiebreak + clinch scenarios in `title_race` |
| **Coach careers** (stops, years, hot seat) only for this year and last | bp-kiffin, bp-carousel, bp-michigan-coach | CFBD `/coaches` all years (1 call), hot-seat items from the insider wire |
| **ATS profile** exists inside the card but no lane answers "how's X against the number" | ah-duke-ats, ah-ucla-ats, ah-indiana-ats, ah-houston-fav | `ats_profile` lane |
| No **case ledger** (ranked reasons per side), so "why / why else / best case for the other side / what changes your mind" is composed by the model | 7 of 10 pushback items | `case_ledger` per game |
| No **position value** table to answer "does that injury matter" | pe-indiana-ol, pe-michigan-injuries | small static table (QB, OL, WR1, CB1 in points) with a verdict |
| No single **fact store**: each lane re-reads caches its own way, and 26 regex checks each re-derive truth (arch review R6) | indirectly all | section 2.3 |

---

## 2. Data ontology

### 2.1 Entities, fields, sources, freshness

IDs: ESPN event ids are the game key (Supabase `GameId` already uses them, CFBD game ids match). Team key = CFBD
team id with an ESPN id and alias list. Player key = CFBD athlete id with an ESPN athlete id and a normalized name.

| Entity | Key fields | Source per field | Freshness (normal / game day) | Lives in |
|---|---|---|---|---|
| **team** | team_id, espn_id, school, abbr, aliases[], conference, home venue_id | CFBD games/venues, ESPN teams | static | facts.sqlite `team` |
| | record overall/conf/home/road | CFBD records + **live finals overlay** from live_games.json | daily / minutes after each final | `team_state` |
| | AP, Coaches, CFP rank + release date | ESPN `/rankings` (CFP Tue night) | weekly, dated | `ranking` |
| | SP+ (overall/off/def/ST), SRS, FPI | CFBD sp/srs, ESPN predictor | daily | `team_rating` |
| | unit stats + FBS rank (rush/pass PPA, SR, explosiveness, line yards, stuff rate, havoc, sack rate, ppo, pace, run rate, turnovers) | CFBD advanced + season stats via `units.table()` | daily | `team_unit` |
| | ATS profile (overall, fav/dog, home/road, last 4, streak, avg cover margin, biggest miss) | derived: CFBD games + closing lines | daily + Sat finals | `team_ats` (derived) |
| | coach_id, interim flag, hot-seat note | CFBD coaches + slate_intel coaching + insider wire | daily | `coach_tenure` |
| **venue** | venue_id, name, lat/lon, elevation_ft, tz, dome, grass | CFBD venues | static | `venue` |
| **game** | game_id, season, week, kickoff_utc, venue_id, neutral, conf_game, tv, away_id, home_id | CFBD games + ESPN scoreboard | daily / 20 s | `game` |
| | status, score, clock, possession, scoring plays, box | live_games.json | 20 s on game day | live overlay (not copied) |
| | pool flag, league line, lock time, tiebreaker | Supabase all_games | per request | league side (private) |
| | rest days each side, prev/next opponent, lookahead/letdown/sandwich, rivalry/trophy, Senior Day | derived: CFBD games, rivalries.py, SP+ | daily | `game_spot` (derived) |
| | stakes: bowl eligibility, title-race status, CFP status | derived: records + title_race + ranking | daily + Sat | `game_spot` |
| | forecast (temp, wind, gust, precip) + as_of | weather.json | ≤ 6 h; ≤ 2 h on game day | `game_weather` |
| | travel per side (miles, zones, body-clock kick) | travel.py | static per game | `game_spot` |
| | last meetings (score, line, ATS, venue) | CFBD lines_prev + hist | static | `meeting` |
| **market_snapshot** | game_id, ts, book, spread_home, total, ml_home, ml_away | ESPN pickcenter (primary), CFBD lines (fallback, opener/close) | **new**: every 15 min, 5 min Sat, kept all season | `market_snapshot` |
| | derived: open, current, close, move pts + direction, move after league lock, total move, key numbers crossed, implied team totals, book spread | derived | on read | view `market_now` |
| | public splits (bets %, handle %) | DK splits (edge.py) | 30 min | `market_splits` |
| **player** | player_id, espn_id, name, aliases, team_id, position, class, jersey | ESPN roster (**new**), CFBD player stats | weekly | `player` |
| | season stats | CFBD player_stats | daily | `player_season` |
| | game log (att/comp/yds/td/int, rush, targets) | **new**: CFBD `/games/players` per week, or ESPN summary box | after each week's finals | `player_game` |
| | starts ledger: games started at QB, SU and ATS as starter, first-start flag | derived from player_game (QB with most attempts = starter) | after finals | `qb_start` (derived) |
| **availability** | player_id, team_id, game_id, status (out / season / doubtful / questionable / probable / TBD / available / benched / suspended / portal / opt-out), detail (body part, length), verbatim quote, source_name, source_url, reported_at, confidence, url_check | slate_intel facts, dossiers, news items with players + status, ESPN summary injuries, Opus wire | hourly; latest report wins; > 7 days old = "last report" wording | `availability` |
| **news_item** | story id, link, source, date, first_seen, teams[], players[], confs[], kind, label (confirmed/report/rumor), dellenger/insider, summary | news.py | 5 min | news.json (indexed into `news_link` rows: item ↔ team/player/game) |
| **coach** | coach_id, name, career stops (school, years, W-L), current status | **new**: CFBD `/coaches` all years (1 call/season) + slate_intel coaching | static + daily status | `coach`, `coach_tenure` |
| **conference_race** | conf, team, conf W-L, remaining conf games, clinch / alive / out, tiebreak note (H2H), "clinches with" scenario | derived: CFBD games + records + live finals | daily + Sat finals | `conf_race` (derived) |
| **cfp** | release date, rank, team, record; format rules (auto bids, byes) with source | ESPN rankings; format rules from an Opus check stored with its source | weekly | `ranking`, `cfp_rules` |
| **member** (private) | team_id, team_name, user_id, active | Supabase teams | per request | Supabase only |
| **pick** (private) | team_id, game_id, side, by_slots, jackpot, week, graded result | Supabase picks + grade | per request | Supabase only; unlocked picks never leave the owner's session |
| **week_result / standings** (private) | member W-L by week and season, place, gaps, playoff cut, seeds | computed from all_games + picks (+ playoffs) | per request | in-memory league ctx (as now) |
| **lore / member memory** (private) | season histories, streaks, member H2H, casino, receipts | lore.json, member_memory/ | 30 min / nightly | private files (as now) |
| **picky_pick** | Picky's side per pool game, lean words, reasons | picky_picks.py | hourly | private file (as now) |

### 2.2 Links

```
venue 1-* game *-1 team (away, home)          team 1-1 team_state / team_rating / team_ats / team_unit
game 1-* market_snapshot                       team 1-* player 1-* player_game *-1 game
game 1-1 game_spot / game_weather              player 1-* availability *-1 game
game 1-* meeting (prior games, same pair)      news_item *-* team / player / game (news_link)
team 1-* coach_tenure *-1 coach                conf 1-* conf_race (team)
game (pool) 1-1 league line (Supabase)         member 1-* pick *-1 game (private)
```

### 2.3 One fact store that every lane reads

**Shape.** One local SQLite file, `server/cache/facts.sqlite` (public football facts only), plus the private league
ctx that is already built per request. No league picks or member data go into facts.sqlite.

**Build.** A new `factstore.py build` step runs at the end of each refresher that changes an input (cfbd refresh,
slate-intel tick, news refresh, weather, a new market snapshot) and on finals. It is pure code, about 2-4 s, and
writes in one transaction to a temp file, then renames it, so readers never see a half-built store. It writes:

- the normalized tables above;
- one **game sheet** per game this week: a denormalized row with every field a lane needs, plus a `facts` list where
  each fact is `{id, kind, subject, value, text, source, as_of}` (for example
  `{id: "ats.duke.overall", value: "8-2", text: "Duke is 8-2 ATS", source: "CFBD lines+games", as_of: ...}`);
- a **team sheet** per FBS team (ATS profile, unit ranks, record, rank, coach, availability).

**Live overlay.** On game day the store is not rebuilt every 20 s. Lanes read the sheet plus `live_games.json`
(scores, records after finals, possession), the same way `gameday.py` does now. The finals overlay updates team
records in reads, so a Saturday-night record is right before CFBD's next refresh (arch review R6).

**Freshness.** Every fact carries `as_of`. `freshness.py` already holds max age and hard drop per source; the
store reuses it. A fact past max age renders with an "as of" phrase; past hard age it is dropped and the lane says
it doesn't know.

**Source checks.** Two-source rules where we have two: CFP and AP ranks (ESPN vs the Opus scrape; on mismatch ESPN
wins and the mismatch is logged), market lines (ESPN vs CFBD), records (CFBD vs live finals). A single-source
availability note keeps its source name and date in the answer ("per Yahoo, Monday").

**Checker.** Each lane returns `(text, facts_used)`. The send-point check validates every number, name and status in
the outgoing text against `facts_used` plus the league ctx. That is the arch review's R6 medium-term fix, and it
means lanes stop being cut by regexes that re-derive truth from a stale cache.

**Cost.** CFBD additions: `/games/players` about 1 call per week plus a 12-week backfill, `/coaches` all years 1 call,
`/rankings` as a fallback 1 call per week. About 40 calls this month and about 10 a month after that, well inside the
free 500 we allow ourselves. ESPN rosters (136 teams, weekly) and pickcenter snapshots are free. **No paid feed is
needed.** If the CFBD budget ever pinches, a CFBD Patreon tier raises the shared quota (the official tiers page lists
the top tier at $30/month for 500k calls; lower tiers are a few dollars). That needs Charlie's OK and isn't in this
plan. The Odds API ($30/month for 20K credits) would add tick-level line history; ESPN snapshots cover what the exam
asks, so it isn't recommended.

---

## 3. Question taxonomy and a code lane for each

### 3.1 Answer template (every lane)

```
[answer]   the direct answer, one sentence, computed
[support]  2-3 facts chosen for this question type, from facts_used
[verdict]  one computed "so what" for the spread/total, with lean words from pick-factors (coin flip / slight lean /
           lean / I like it; never a lock), or "doesn't move the number" when it doesn't
[voice]    gemma's one line (existing FACT_TAIL + voice_line_ok), unchanged rules
```

**Phrasing bank, not a fixed sentence.** Each template slot has 6-10 surface variants written offline by Opus once
(and checked by a script: every slot filled, ≤ 700 chars, no banned tics, no em dashes), stored in
`server/phrasing/*.json`. Code picks a variant by a hash of (game, asker, day) and the no-repeat ledger. The facts are
identical across variants; only the wording moves. Answer-first, short sentences, texty connectors ("so", "and
honestly", "which is why"). This is not ledger P3/P4 (static or bank few-shot turns in the prompt): nothing is added to
the prompt and the model never sees the variants. It is not B9 (gemma adapting banter): gemma never rewrites the
facts.

### 3.2 Real-traffic mix (all 413 exchanges in picky_exchanges.jsonl, routed with today's router)

| Bucket | Share | Main kinds | In this plan |
|---|---|---|---|
| Banter (insult, bot, looks bait, roast, beer, cereal, NFL, egging, life, greet, vulgar) | 38% | burn book, stances | stays as is (code-picked lines + gemma) |
| League facts (standings, record, results, playoff, pick, pool games, recap, best week, lock, ghost checks, read, slate, hidden, why, rules) | 48% | existing fact lanes | keep; new phrasing bank + verdict where it applies |
| Full brain (no rule matched) | 13% | open football asks, mixed texts | the new football lanes below take most of them |

Flub reports (11-17: wrong sub-question, robotic tone, didn't answer, wrong fact, repeat) point the same way: the
fact is usually right and the shape is wrong. The phrasing bank and the verdict line hit "robotic" and "repeat";
the lane table (Phase 2) hits "wrong sub-question".

### 3.3 Lanes for the genius categories

Status: **exists** = router lane today; **upgrade** = exists, gets the new template + store; **new**.

| Lane | Inputs (fact store) | Computation | Template (answer / support / verdict) | Status |
|---|---|---|---|---|
| `line_move` | market_now, availability (both teams, ≤ 10 days), news_link, team_ats, total move | move size + direction; cause = newest availability or news change on the side the line moved against; "priced in?" = move pts vs position value of the cause (QB starter out ≈ 3-7 pts by starter-vs-backup rating gap) | "Opened X, now Y: that's the [cause]." / starter's and backup's starts ledger, ATS tiebreaker, total move / "the move already prices it" or "still room" + lean | upgrade |
| `value` (where's the value) | all pool games' league line vs market_now, case_ledger top reason per game | rank by line value (1.5+ is the only real edge per backtest), then attach the best football reason | "Top spot: X, [value]" / best football reason + one ATS fact / lean words | upgrade |
| `hypo_value`, `keynum`, `implied` | market_now, key-number table | value vs locked line across key numbers; implied team totals; "big spread in low total" rule | answer / which side gets the hook / verdict | upgrade; `implied` new |
| `rules` (push etc.) | league_rules.live.md | exact rule text | answer + where the rule lives | exists |
| `qb_status` | availability (QB), qb_start, player_season, roster | latest status word verbatim (TBD stays TBD); if out/doubtful: backup name, class, starts, SU/ATS as starter, season line | "Status: X (per source, date)." / backup facts / "that's worth ~N pts; the line has moved M" | upgrade |
| `injury_impact` | availability (non-QB), position value table, team_unit | sum value of who's out; tie to the matching unit rank (a lost RG vs a top-20 run game) | answer / who replaces him / "matters at the margin, not a line-mover" or "this is why it moved" | new |
| `backup_qb` | qb_start, player_game, roster | starts, record, line, first-start flag | answer / numbers / verdict | new (split from qb_status) |
| `unit_matchup` | team_unit both ways, team_rating unit SP+, style (run rate), availability | `units.edges` for the asked unit + the reverse unit + run rate; "edge" only when the rank gap is real (existing tiers) | "X wins that: [rank vs rank]." / reverse matchup + style / "so the total/side leans ..." | new code lane (cards feed the full brain today) |
| `ats_profile` | team_ats, recent games vs number, current role (fav/dog, home/road) | splits relevant to this week's role first; streak; avg cover margin; biggest miss | "X is 8-2 ATS." / the split that matches this week + streak / "covering machine, but that's priced by now" style verdict | new |
| `h2h` | meeting | last 1-2 meetings, line, ATS, venue (neutral sites like Wrigley) | exists, add venue + second meeting | upgrade |
| `career_ats` | team_ats this season | says what's not in data + this season | exists | exists |
| `spots` (lookahead, letdown, sandwich, rest, bye) | game_spot | existing slate_cards logic per asked team; slate-wide list for "any spots this week" | answer / opponent next week + their SP+ / "spots on paper are worth ~coin flip; letdown held up a little" (backtest) | upgrade (`lookahead_slate` + new per-team) |
| `weather_alt_travel` | game_weather, venue, travel | forecast framed as forecast with as_of; elevation both home fields; body-clock kick; tie wind to total | answer / numbers / "tilts the under" or "no factor" | new code lane (cards today) |
| `motivation` / `interim` | game_spot stakes, conf_race, coach_tenure, team_ats since change | stakes per side; interim SU/ATS since the date | answer / stakes + ATS / verdict | upgrade |
| `cfp_now` | ranking (ESPN), cfp_rules, conf_race leaders | "if it ended today" from the format rules in `cfp_rules` (auto bids for top conference champions by rank, then at-larges), next release date | answer / who's in on the bubble / who controls it | new (ESPN primary, Opus cross-check) |
| `conf_race` | conf_race | leader, chasers, H2H tiebreaks, "clinches with a win" scenarios, this week's games | answer / scenario / "who controls it" said outright | upgrade |
| `coach` (carousel, hot seat, history) | coach, coach_tenure, slate_intel coaching, insider wire (hot seat), team_ats under coach | this week's games with a coach story first; career stops for "going back to X" | answer / story + date + source / does it touch this week's line | new |
| `pick` + `why` + `other_side` + `change_mind` | case_ledger | see below | turn 1: lean + top 2 reasons; "why": next unused reason; "best case for the other side": that side's top 2; "what changes your mind": computed triggers | upgrade (`pick`, `why`) + new (`other_side`, `change_mind`) |
| `strategy` (game theory) | league standings, playoff cut, remaining picks, consensus lean (pick_brief 58-60) | protect / bubble / chase; dead-last = max variance and fade the consensus; name who's around you | answer / exact gaps / plan | upgrade |

**Case ledger** (new, `case_ledger.py`): for each game, every factor from `pick_brief` that has a source becomes a
reason row `{side, weight, text, facts_used, kind}` (line value, QB status, ATS split, unit edge, spot, H2H, weather,
travel, stakes). Rows are ranked by the hand weights that stay (ledger B12: nothing fitted beats them). "Why" walks the
list without repeats and says "that's all I've got" when it runs out (keeps `why_lane`'s good behavior). Triggers for
"what would change your mind" are computed from the ledger: the line crossing the nearest key number, a QB status
flip, wind over 20 mph for a total. The model never writes a projection number (that is where pb-navy and pb-uga
invented "SP+ has Memphis by about 10").

### 3.4 Coverage of the 75 genius items and the expected score

Pass today → expected after the lane ships. Expected = a pass where the judge's misses are facts the lane will print
and voice is fixed; otherwise left as a fail. The judge's ±0.2 noise (ledger) means ±2-3 items either way.

| Lane | Genius items | Today | Expected |
|---|---|---|---|
| line_move | pool-iowa-ill-move, lv-uga-sc-move, lv-clem-duke-move, lv-osu-neb-key3, lv-tamu-ou-unknown, lv-unt-tulane-qb | 2/6 | 5/6 |
| value / hypo / keynum / implied / rules | pool-nw-minn-value, lv-locked-vs-market, lv-key-numbers, lv-implied-ind-wash, lv-where-value, lv-no-push | 1/6 | 5/6 |
| qb_status / backup_qb / injury_impact | pe-sayin-fahey, pe-tenn-qb, pe-clemson-qb, pe-sellers, pe-michigan-injuries, pe-indiana-ol, pe-unt-backup | 1/7 | 6/7 |
| unit_matchup | pool-nw-minn-matchup, mu-navy-option, mu-hou-wvu-pass, mu-tenn-run-lsu, mu-ucla-def-mich-off, mu-unm-def-wyo, mu-nd-redzone, mu-boise-explosive, mu-uga-run, un-nw-minn-run, un-osu-neb-trenches, un-ttu-bay-passrush, un-hou-wvu-pace, un-iowa-ill-turnovers, un-mich-pass-ucla, un-vt-run-miami | 10/16 | 13/16 |
| ats_profile / h2h / career_ats / spots | pool-nw-minn-h2h, ah-indiana-ats, ah-duke-ats, ah-h2h-clem-duke, ah-h2h-navy-mem, ah-houston-fav, ah-coach-ats-unknown, ah-ucla-ats, ah-lookahead, ah-vt-letdown | 4/10 | 9/10 |
| weather_alt_travel / spots / motivation / interim | si-wyoming-cold, si-byu-wind, si-ucla-travel, si-tcu-motivation, si-baylor-interim, si-navy-bye, sq-iowa-lookahead, sq-osu-lookahead, sq-ucla-timezones, sq-sdsu-boise-altitude, sq-byu-kansas-altitude | 7/11 | 10/11 |
| cfp_now / conf_race / coach | bp-cfp-picture, bp-big12-race, bp-carousel, bp-michigan-coach, bp-big-ten, bp-kiffin | 1/6 | 5/6 |
| pick / why / other_side / change_mind | pool-ttu-bay-pick, pool-lsu-tenn-pick, pb-osu-neb, pb-duke, pb-navy, pb-uga, wy-iowa-ill, wy-nw-minn, wy-ucla-mich, wy-clem-duke | 3/10 | 8/10 |
| strategy | the 3 private game-theory items (2nd place, bubble, last place) | 1/3 | 3/3 |
| **Total** | 75 | **30/75** | **64/75** if every lane hits its target; **58-64** is the honest range |

By category: lines 3 → 10/12, personnel 1 → 6/7, big_picture 1 → 5/6, pushback 3 → 8/10, ats 4 → 9/10, matchups
10 → 13/16, situational 7 → 10/11, game_theory 1 → 3/3.

The items still expected to fail are ones where the key wants judgment the data can't settle (whether a 5-point move
"already prices in" a walk-on QB, a Kiffin-return storyline beyond career stops, one or two unit items where the
judge wants SP+ unit ranks and the edge is a coin flip).

---

## 4. What stays free-form, and its guardrails

Free-form (gemma full brain, as now):

- Banter and roasts (burn book lines stay verbatim; ledger B9 adapt stays OFF).
- Off-topic life, greetings (own `greet` stance), NFL, cereal and the other provocation families.
- Broad news "what's going on in CFB" (ledger B20 REJECTED code-written news; it stays full brain with the NEWS
  rider).
- Opinions and history the store doesn't hold ("best coach ever", "who wins the Heisman"), hypotheticals.
- Long mixed texts where two lanes match (arch review R7: fall through to the full brain).

Guardrails on the free-form path (all exist except the last two):

1. Per-sentence fact guard + named checks + results guard (ledger B6, B7).
2. Understand pass + one draft at temp 0.2; verifier-guided repair if Phase 2 of the smarter plan lands.
3. Unknowns get "don't have that" (career_ats pattern), never a guess.
4. **New:** the fact store's checker runs on full-brain drafts too: any number tied to a team, player or game must
   match a store fact or the sentence is cut. Projections ("X by 10") are only allowed when they come from a
   `facts_used` row.
5. **New:** if a full-brain question mentions a game on this week's slate, the game sheet's top 3 facts and verdict go
   in the rider, so the model has the computed "so what" instead of inventing one.

---

## 5. Phased build plan (biggest exam gain per unit of effort first)

Effort: S = one build job (half a day), M = 1-2 days. Each phase ships behind its own flag, A/B'd on the side
sidecar inside `lockf -t 14400 /tmp/picky-exam.lock`, losers deleted in the same PR (arch review rule), results added
to the tried ledger. Every phase must keep: fact exam ≥ 102/113 with 0 leaks and 0 guardrail misses, the Saturday DM
drill ≥ 83/85, `test_speed` budgets green, the lane regression table green.

**Held-out rule (overfitting guard):** lanes are written from question *types* and real traffic, never from item ids
or answer keys. Each phase's score is reported on the current week **and** on the next fresh week's questions from
genius-refresh (written after the code freezes for that phase). A phase only counts if it gains on the fresh week too.

| # | Phase | Work | Effort | Expected exam | Check |
|---|---|---|---|---|---|
| 1 | **Voice + verdict layer for existing code lanes** | Phrasing bank (Opus writes 6-10 variants per slot, offline once; validator script); verdict line added to line_move, hypo_value, keynum, qb_status, conf_race, lookahead, interim, h2h, strategy; hand-check the judge-error items and fix `checks.py` where it rejects true computed records (computed ATS-since-date, standings W-L) | S-M (1 day) | 30 → ~37 (voice-only fails: lv-locked-vs-market, pe-tenn-qb, pe-sellers, pe-michigan-injuries, si-tcu-motivation, bp-big12-race, bp-big-ten, ah-lookahead; plus det-check fixes) | code-lane voice mean 2.66 → ≥ 3.2 on the 32 code items; no new invented; new `test_phrasing.py` (every variant renders every fixture, ≤ 700 chars, no tics) |
| 2 | **Fact store v1 + market history + ESPN rankings + lane table** | `factstore.py` (tables, game/team sheets, `facts_used` contract), `market_snapshot` (hook into an existing tick; no new LaunchAgent until Charlie OKs the scheduler merge), ESPN rankings as CFP primary with Opus cross-check, router lane regression table (arch review R7) with the 75 exam asks + all real asks | M (2 days) | ~37 → ~40 (cfp, key numbers verified, where-value, tamu total move) | store builds < 5 s; every lane reads only the store + league ctx; `test_factstore.py` on a frozen fixture; lines ≥ 6/12, big_picture ≥ 2/6 |
| 3 | **ATS profile + case ledger (pushback)** | `ats_profile` lane; `case_ledger.py`; `pick`/`why` read it; new `other_side` and `change_mind` lanes | M (2 days) | ~40 → ~49 | ats ≥ 8/10, pushback ≥ 7/10; 0 model-written projection numbers on pushback items; `test_case_ledger.py` (no repeats across 3 whys, other side never cites our side's reason) |
| 4 | **Personnel store** | ESPN rosters, `player_game` from CFBD `/games/players` (backfill weeks 1-12, then Sat/Sun), `qb_start` ledger, `availability` normalization from slate_intel + dossiers + news + ESPN, position value table; `qb_status` / `backup_qb` / `injury_impact` lanes; line_move cause reads availability first | M (2-3 days) | ~49 → ~55 (personnel +5, line-move items +2) | personnel ≥ 5/7, lines ≥ 9/12; fact exam adds 10 personnel items (backup starts, TBD stays TBD, stale report wording); CFBD calls this month < 120 |
| 5 | **Big picture** | CFBD `/coaches` all years, `coach` lane (carousel tied to this week's games, career stops), `conf_race` H2H tiebreaks + "clinches with a win", `cfp_now` with `cfp_rules` | M (1.5 days) | ~55 → ~59 | big_picture ≥ 5/6; `test_title_race.py` gets tiebreak + clinch fixtures |
| 6 | **Unit matchup code lane** | `unit_matchup` in code from `units.py` (both directions, style, SP+ unit ranks, verdict); keep full brain for unit asks the lane can't parse | S-M (1 day) | ~59 → ~62 | matchups ≥ 13/16 **and** voice mean on those items not below the full brain's (3.3); median latency on unit asks 58 s → < 12 s |
| 7 | **Situational + game theory polish** | `weather_alt_travel` lane (forecast as forecast, both elevations, wind → total), interim ATS since date, dead-last max-variance strategy naming the consensus to fade | S (half day) | ~62 → ~64 | situational ≥ 10/11, game_theory 3/3 |

Total about 10-12 build days. Phases 1-3 alone should take the exam from 30 to about 49 in about 5 days.

Why this order when lines, personnel and big_picture are the weakest categories: Phase 1 fixes three personnel and two
big_picture items for a day of work (they fail on voice only), Phase 2 is the base the lines and big_picture lanes
read, and Phase 3's pushback gain (+5) is the cheapest large block left. Personnel's remaining gap needs the new player
data, so it comes right after.

---

## 6. Risks

| Risk | Why it matters | Mitigation |
|---|---|---|
| **Overfitting to the exam** | 75 items on one week's slate; lanes tuned to their keys would score and still flop on real texts | held-out fresh-week rule (section 5); lanes built from types; lane table built from real traffic first; fact exam + Saturday drill + real replay must not regress |
| **Template sameness** | code answers lose voice (the main finding) and repeat across a chat | phrasing bank + no-repeat ledger; Opus refreshes variants monthly, not per run (ledger A4) |
| **Bad source, faithfully repeated** | the CFP list this week | two-source checks where possible; source + date printed on single-source personnel notes; mismatch log feeds feed_monitor |
| **Saturday staleness** | records, availability and lines change all day | live finals overlay; market snapshots every 5 min Sat; cfbd-finals refresh (exists); availability older than game-day max age says "last report Thursday"; freshness hard drops |
| **Latency** | store build on the request path would hurt | build runs in refreshers, request reads SQLite (ms); code lanes p50 ~6 s today, unit asks drop from ~58 s to < 12 s; gemma voice line keeps its 32-token cap |
| **Router collisions** | more lanes = more regex misroutes (arch review R7) | lane regression table in CI; two-rule match or provocation + fact → full brain; a shared entity resolver (team, game, player, unit word) so lanes key on slots, not phrasing |
| **ID mapping** | player names differ across ESPN, CFBD and news | alias table, normalized names, team-scoped matching; unresolved player = lane declines to the full brain, never guesses |
| **CFBD budget** | shared 1,000 with basketball | ~40 calls this month, ~10/month after; `SHARED_RESERVE` stays; Patreon tier only with Charlie's OK |
| **Live-tree churn** | many jobs edit server/ at once (arch review R4) | build each phase in a worktree; ship through the existing gate; no edits to a worktree an exam is running from |
| **Judge noise** | ±0.2 per mean, ±2-3 items per run | report two runs per phase; verify "invented" flags by hand before acting on them |
| **Privacy** | the store must never hold picks or member data | facts.sqlite is public-football only, asserted in a test; league facts stay in the per-request ctx with today's session rules |

---

## 7. Needs Charlie's decision before building

1. Approve Phase 1 (voice + verdict layer) to start.
2. OK to fold the market-snapshot and fact-store build into existing ticks now, and into one scheduler later (arch
   review section 4), instead of a new LaunchAgent.
3. Confirm: no paid feeds. A CFBD Patreon tier stays off unless the free calls run out.
