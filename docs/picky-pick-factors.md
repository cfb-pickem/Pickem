# Picky pick factors (ATS)

Charlie, 2026-11-16: "Brainstorm like 50 other factors that go into football games including Ross news. Those all need to be considered in his picks."

The league is graded only against the spread, so each factor counts for how it moves the margin vs the line, not who wins. Each one needs a real data source, computed in code. A factor with no source gets skipped, never guessed.

## Status (2026-11-16, computed in server/pick_brief.py)

Each line below ends with **live** (computed for every game that has the data), **partial** (computed only when a source has something, or an approximation; the source says which) or **no source** (skipped, never guessed). Totals: 39 live, 18 partial, 3 no source. `pick_brief.STATUS` is the single source of truth; `test_pick_brief.py` fails if this column drifts from it.

How a pick is scored: every factor puts its weight on the side it helps COVER the league line (never who wins). Heavy: line value vs the market (3 for a 0.5-1.4 pt gap, 6 for 1.5+, +1 across a key number) and starting QB status (out 4, doubtful 2, questionable 1). Medium (1-2): SP+ projection 2, FPI 1, a 1.5+ pt line move, bye, short week, lookahead, letdown, revenge, travel/body clock, altitude, other key injuries (0.5 each, max 1.5), and league game theory 1. Light (0.5): every matchup stat, team ATS splits, recent ATS form, head-to-head, splits, weather and the rest. Context (0): explains or flags without moving the lean. The side with more weight is the lean; "strong" needs 4+ with the other side at most a third of that. A hand-set heuristic, not a trained model. Picky's prompt gets only the recommendation, the 3-5 heaviest edges and up to 3 red flags; "why?" or "give me everything" gets the full table.

## Line and market
1. League locked line (Tue noon) vs current market line: the value gap → **live**: all_games.line (league) vs ESPN pickcenter market, CFBD lines fallback
2. Opening line vs current line, size and direction of the move → **live**: ESPN pickcenter open/now, CFBD spreadOpen fallback
3. Cause of the move (injury, weather, news) when the news shows one → **partial**: news.json + game dossier items for either team dated within 7 days of a move
4. Key numbers (3, 7, 10, 14): does the league line sit on the good side of one the market crossed → **live**: league line vs market line (computed)
5. Total (O/U) and implied team totals: big spread + low total = harder cover → **live**: ESPN / CFBD over-under
6. Late or steam moves near kickoff (after the league locks) → **partial**: league line (frozen at Tue noon lock) vs current market; no tick history
7. Public vs sharp split (only if a public source exists) → **live**: DraftKings Network public betting splits page (edge.py)

## Personnel
8. Starting QB status (injury, questionable, benched) → **partial**: game dossier injury facts + news.json (only games with a dossier or a story)
9. Backup QB experience if the starter is out → **partial**: CFBD player season passing stats (only when the starter is out)
10. Other key injuries: RB1, WR1, OL starters, edge rusher, CB1 → **partial**: game dossier injury facts
11. Suspensions and academic ineligibility → **partial**: dossier + news.json keyword match
12. Late-season opt-outs and draft prospects sitting → **partial**: dossier + news.json keyword match
13. Transfer-portal departures midseason → **partial**: dossier + news.json keyword match
14. Depth-chart changes in the last two weeks → **no source**: no depth-chart feed

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
24. Rivalry game (underdogs historically cover more) → **no source**: no rivalry list in any cached source
25. Bowl eligibility on the line (5-win teams) → **live**: CFBD records + schedule
26. CFP / conference-title stakes; clinched or eliminated teams → **partial**: ESPN AP rank + CFBD conference record (no clinch/elimination feed)
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
51. Head-to-head history, straight up and ATS → **live**: CFBD lines this season + last season
52. Style clash: option or service-academy offense vs a defense that hasn't seen it → **live**: CFBD advanced run rate (offense) vs run rate faced (defense)
53. Backdoor and garbage-time risk: does the favorite pull starters; does the dog score late → **partial**: spread size + underdog passing explosiveness (no garbage-time split)

## Off-field and Dellenger news
54. Realignment bitterness (a team leaving its conference, a grudge game) → **partial**: news.json + insider notes (realignment) naming the team
55. Revenue-sharing / NIL roster turmoil and midseason departures → **partial**: news.json + insider notes (revenue sharing / NIL) naming the team
56. Program turmoil: investigations, AD changes, coach-search distraction → **partial**: news.json keyword match (investigation, AD change, coach search)
57. CFP format and seeding incentives (bye race, resting starters late) → **no source**: no clinch, seeding or resting-starters feed

## League game theory
58. Key rivals' past tendencies (favorites/dogs, home teams, their own fan team, following the crowd) → **live**: Supabase graded picks (both seasons): each rival's tendencies
59. League consensus on the game (from past patterns): fade it or follow it → **live**: Supabase graded picks: predicted league lean from past tendencies
60. The asker's standings need: chasing = take variance and contrarian picks; leading = match the field → **live**: Supabase standings: asker's gap to the leader / playoff cut line
