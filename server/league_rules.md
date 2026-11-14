# League rules (single source for Picky; keep in sync with cfb-genius.html "League Rules")
# Lines starting with # are notes for maintainers and are stripped before Picky sees this.
# Sources beyond that page: scoring in index.html, lock/slate rules and the half-point
# lines in supabase/migrations, the slots jackpot in 20260911/20260913 migrations.
# source-hash: a7bbb551393d7cdc  (sha256[:16] of the cfb-genius.html Rules block;
# test_rules.py fails when that block changes, so update this file and the hash.)

FORMAT
- Regular season is weeks 0-15. Every week the commissioner picks 4 games; you pick each one against the spread.
- Scoring: 1 point per correct pick (your team covers). No pick = no point. No bonus points, no confidence weights.
- The line is the home team's adjustment: home covers if home score + line > away score. Negative line = home favored.
- Every line is a half point (whole numbers get moved half a point), so a push can't happen.
- Lines lock Tuesday 11:00 AM CT. The line at that moment is what the week is scored against. A game with no line yet keeps the first one posted.

DEADLINE AND PRIVACY
- Regular season: all picks for the week lock at the scheduled start of the week's first game. A weather or TV delay doesn't buy more time.
- Playoffs: each game locks at its own scheduled start.
- Everyone's picks stay hidden until they lock, then are revealed at that moment.
- Picks save automatically; a pick only counts once it is saved. Picks can only be made on that week's slate games.

LET THE SLOTS DECIDE (the jackpot)
- Instead of picking a side, you can choose "Let the slots decide". At lock, the site flips a coin and picks a side for you.
- Every slots pull charges the league jackpot (the football). If it pops on your pull, you get an AUTO WIN: a point for that game no matter what happens.
- The jackpot is rare; the current odds are shown on the football on the picks page.

STANDINGS AND PLAYOFFS
- Season standings = total points from weeks 0-15. The site lists no weekly prize.
- Playoffs: top 4 advance, scores reset, and they pick all playoff games.

PAYOUTS
- 1st: the remainder of the buy-in pot. 2nd: buy-in back + $25. 3rd: buy-in back.
- The site does not state the buy-in amount.

TIEBREAKERS: how ties in the standings are broken. They use separate tiebreaker picks, which never count as points.
Regular season tiebreakers, in order:
1) conference championship game wins
2) Army v Navy
3) chips from The Pit
4) Celebration Bowl
5) win % over the last 8 games
6) coin flip
Playoff tiebreakers, in order:
1) Army-Navy (if unused)
2) regular season points
3) other bowl games

THE PIT (blackjack, tiebreaker only)
- One shared dealer hand a week. The table opens when the week's picks open and settles at first kickoff.
- Everyone starts the season with 1,000 chips. Bets 25-500 (anything left under 25 is always a legal bet). You bet before seeing any cards.
- Blackjack pays 3:2 rounded down. Dealer stands on all 17s. Double on your first decision only. Dealer blackjack takes original bets only.
- Bust to zero and you're out of The Pit for the season. No reload.
- Chips never become points. They only break ties.
