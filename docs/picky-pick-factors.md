# Picky pick factors (ATS)

Charlie, 2026-11-16: "Brainstorm like 50 other factors that go into football games including Ross news. Those all need to be considered in his picks."

The league is graded only against the spread, so each factor counts for how it moves the margin vs the line, not who wins. Each one needs a real data source, computed in code. A factor with no source gets skipped, never guessed.

## Status (2026-11-16, computed in server/pick_brief.py)

Each line below ends with **live** (computed for every game that has the data), **partial** (computed only when a source has something, or an approximation; the source says which) or **no source** (skipped, never guessed). Totals: 41 live, 19 partial, 0 no source. `pick_brief.STATUS` is the single source of truth; `test_pick_brief.py` fails if this column drifts from it.

How a pick is scored: every factor puts its weight on the side it helps COVER the league line (never who wins). Heavy: line value vs the market (3 for a 0.5-1.4 pt gap, 6 for 1.5+, +1 across a key number) and starting QB status (out 4, doubtful 2, questionable 1). Medium (1-2): SP+ projection 2, FPI 1, a 1.5+ pt line move, bye, short week, lookahead, letdown, revenge, travel/body clock, altitude, other key injuries (0.5 each, max 1.5), and league game theory 1. Light (0.5): every matchup stat, team ATS splits, recent ATS form, head-to-head, splits, weather and the rest. Context (0): explains or flags without moving the lean. The side with more weight is the lean; "strong" needs 4+ with the other side at most a third of that. A hand-set heuristic, not a trained model. None of this runs at reply time: `server/game_cards.py` computes it OFFLINE into one cached card per slate game (built at slate-set, after the Tue 11 AM CT line lock, Fri 5 PM and Sat 7 AM; LaunchAgent com.pickem.game-cards), adds per-member game theory from graded picks only and a written case / counter-case from Opus, and live Picky only reads the card (a rider in the latest user turn) and phrases it.

## Line and market
1. League locked line (Tue 11 AM CT, per the site's line_lock_at) vs current market line: the value gap → **live**: all_games.line (league) vs ESPN pickcenter market, CFBD lines fallback
2. Opening line vs current line, size and direction of the move → **live**: ESPN pickcenter open/now, CFBD spreadOpen fallback
3. Cause of the move (injury, weather, news) when the news shows one → **partial**: news.json + game dossier items for either team dated within 7 days of a move
4. Key numbers (3, 7, 10, 14): does the league line sit on the good side of one the market crossed → **live**: league line vs market line (computed)
5. Total (O/U) and implied team totals: big spread + low total = harder cover → **live**: ESPN / CFBD over-under
6. Late or steam moves near kickoff (after the league locks) → **partial**: league line (frozen at the Tue 11 AM CT lock) vs current market; no tick history
7. Public vs sharp split (only if a public source exists) → **live**: DraftKings Network public betting splits page (edge.py)

## Personnel
8. Starting QB status (injury, questionable, benched) → **partial**: game dossier injury facts + news.json (only games with a dossier or a story)
9. Backup QB experience if the starter is out → **partial**: CFBD player season passing stats (only when the starter is out)
10. Other key injuries: RB1, WR1, OL starters, edge rusher, CB1 → **partial**: game dossier injury facts
11. Suspensions and academic ineligibility → **partial**: dossier + news.json keyword match
12. Late-season opt-outs and draft prospects sitting → **partial**: dossier + news.json keyword match
13. Transfer-portal departures midseason → **partial**: dossier + news.json keyword match
14. Depth-chart changes in the last two weeks → **partial**: news.json + dossier items: starter named, benched, promoted, 'will start', QB1 (info only; no depth-chart feed)

## Coaching and situation
15. Coaching change, interim coach, or a coach already announced as leaving → **partial**: news.json + dossier + insider notes keyword match
16. Hot-seat coach (Dellenger's carousel reporting): playing for the job vs checked out → **partial**: insider notes coaching-carousel section + news.json
17. Coach's ATS record overall, as a favorite, and as an underdog → **partial**: CFBD lines: team ATS this season as favorite/underdog (no multi-year coach ATS)
18. Off a bye vs opponent on normal rest → **live**: CFBD schedule (game dates)
19. Short week (Thursday/Friday games) → **live**: CFBD schedule (game dates)
20. Lookahead spot: big game next week → **live**: CFBD schedule + SP+ (big game = top-25 SP+ opponent next week)
21. Letdown spot: after a huge win or a rivalry game → **live**: CFBD schedule, scores and lines (last week's upset win)
22. Sandwich game between two big games → **live**: CFBD schedule + SP+
23. Revenge game (lost to this opponent last year, especially if embarrassed) → **live**: CFBD last season's lines/scores
24. Rivalry game (underdogs historically cover more) → **live**: rivalries.py: maintained list of 118 FBS rivalry and trophy games (weight 0 per the backtest)
25. Bowl eligibility on the line (5-win teams) → **live**: CFBD records + schedule
26. CFP / conference-title stakes; clinched or eliminated teams → **live**: title_race.py: CFBD conference records + remaining schedule (title-game clinch/elimination, out of the CFP); ESPN AP rank fallback
27. Senior Day / last home game → **live**: CFBD schedule (last home game)
28. Committee "style points" incentive to run up the score (helps big favorites cover) → **partial**: ESPN AP rank (committee incentive approximated by a top-12 AP team)

## Travel and environment
29. Home-field strength (stadium, crowd, home ATS record) → **live**: CFBD lines: home team's home ATS this season
30. Travel distance and time zones (a West Coast team at an 11am CT kickoff) → **live**: CFBD venues lat/lon + timezone, team home stadiums from the schedule
31. Kickoff time; night game → **live**: all_games kickoff time
32. Altitude (Air Force, Wyoming, Colorado, Utah, Boise) → **live**: CFBD venues elevation
33. Wind (hurts passing and totals) → **live**: ESPN forecast, Open-Meteo fallback (edge.py)
34. Rain, snow, or cold for a warm-weather or dome team → **live**: ESPN forecast / Open-Meteo + CFBD venues (dome, latitude)
35. Turf vs grass → **live**: CFBD venues grass flag
36. Neutral site → **live**: CFBD games neutralSite

## Matchup stats (CFBD advanced)
37. Offensive EPA/play vs the opponent's defensive EPA/play → **live**: CFBD advanced season stats (PPA per play)
38. Success rate on both sides → **live**: CFBD advanced season stats (success rate)
39. Explosiveness allowed and created → **live**: CFBD advanced season stats (explosiveness)
40. Rushing offense vs run defense (line yards, stuff rate) → **live**: CFBD advanced season stats (rushing PPA, line yards, stuff rate)
41. Rushing QB vs the defense's QB-run yards allowed → **partial**: CFBD player rushing stats for the QB vs run defense (no QB-run-allowed split)
42. Passing offense vs pass defense → **live**: CFBD advanced season stats (passing PPA)
43. Havoc rate and sacks (OL vs DL) → **live**: CFBD advanced havoc + team sacks
44. Third-down conversion rates → **live**: CFBD team season stats (third downs)
45. Red-zone touchdown rate (scores 7s or settles for 3s: key for covers) → **live**: CFBD advanced points per scoring opportunity
46. Special teams and kicker reliability → **live**: CFBD player kicking stats (FG%)
47. Turnover margin and luck (fumble-recovery rate regresses) → **live**: CFBD team season stats (turnovers, fumbles)
48. Tempo: plays per game (more plays = more variance in big spreads) → **live**: CFBD advanced plays per game
49. Opponent-adjusted ratings (SP+, FPI, or CFBD SRS) and the line those ratings imply → **live**: CFBD SP+ and SRS, ESPN FPI (all_games.fpi_margin)
50. Recent form (last 3-4) vs full season: regression signals → **live**: CFBD lines: last 4 games ATS
51. Head-to-head history, straight up and ATS → **live**: CFBD lines this season + last season, team level (no QB-vs-opponent split: no player game logs cached)
52. Style clash: option or service-academy offense vs a defense that hasn't seen it → **live**: CFBD advanced run rate (offense) vs run rate faced (defense)
53. Backdoor and garbage-time risk: does the favorite pull starters; does the dog score late → **partial**: spread size + underdog passing explosiveness (no garbage-time split)

## Off-field and Dellenger news
54. Realignment bitterness (a team leaving its conference, a grudge game) → **partial**: news.json + insider notes (realignment) naming the team
55. Revenue-sharing / NIL roster turmoil and midseason departures → **partial**: news.json + insider notes (revenue sharing / NIL) naming the team
56. Program turmoil: investigations, AD changes, coach-search distraction → **partial**: news.json keyword match (investigation, AD change, coach search)
57. CFP format and seeding incentives (bye race, resting starters late) → **partial**: title_race.py: a clinched title-game spot in the last regular-season game = resting-starter risk (no committee seeding feed)

## League game theory
58. Key rivals' past tendencies (favorites/dogs, home teams, their own fan team, following the crowd) → **live**: Supabase graded picks (both seasons): each rival's tendencies
59. League consensus on the game (from past patterns): fade it or follow it → **live**: Supabase graded picks: predicted league lean from past tendencies
60. The asker's standings need: chasing = take variance and contrarian picks; leading = match the field → **live**: Supabase standings: asker's gap to the leader / playoff cut line

## Backtest (2026-11-16, `game_cards.py backtest`, honest numbers)

Every graded 2025-26 pool game, weeks 1-15, no tiebreakers (107 games), each side scored against the LEAGUE line (pushes go to the away side, as the site grades them), market = CFBD's closing line (DraftKings first). 95% intervals in brackets.

- Line value 1.5+ points (take the side the league line is stale toward): **20-13, 61%** [44-75%]
- Line value 0.5-1 point: 20-23, 47% [33-61%]
- Line value that crosses a key number: 25-19, 57% [42-70%]
- Follow a 1.5+ point market move (open to close): 27-29, 48%
- The whole weighted lean, 2026 only: 22-19, 54% [39-68%]; "strong" leans 9-4. In-sample (it uses this season's ratings, so it flatters itself). Rerun 2026-11-16 after factors 14/24/26/57 got sources (was 23-19, strong 8-5)
- Baselines: home team 64-43 (60%), underdog 43-64 (40%)
- Rivalry underdog (factor 24, `rivalries.backtest`, every FBS-vs-FBS game with a CFBD closing line, 2025 + 2026 to date): **63-82, 43%** [36-52%] vs 50% for all other underdogs (639-630). 2025 alone 36-54 (40%), 2026 so far 27-28. The "rivalry dogs cover" rule doesn't hold here, so factor 24 flags the game with weight 0 instead of leaning to the dog. On the league's own pool games vs the league line, rivalry dogs went 7-15 (32%) [16-53%], against 40% for all pool underdogs: a tiny sample pointing the same way.
- Title stakes (factor 26) and resting starters (57) come from `title_race.py`: top 2 by conference record make the title game; a team has clinched when at most one rival can still reach its conference wins, and is out when two rivals already have more wins than its best case (tiebreakers not modeled, so it's conservative). Out of the CFP = out of its title game and a Group of 6 team or 3+ losses. It never says anyone is in.

Read it straight: only the big line-value gap looks better than a coin flip, and even that sample is small enough that 50% is inside the interval. The card words confidence from these buckets (coin flip / slight lean / lean / "I like it" only for a strong lean with 1.5+ points of value) and never calls anything a lock.

