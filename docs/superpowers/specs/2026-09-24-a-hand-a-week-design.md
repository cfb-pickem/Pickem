# A hand a week

**Status:** approved, building
**Date:** 2026-09-24
**Builds on:** [Let the slots decide](2026-09-11-let-the-slots-decide-design.md), [Pop the football](2026-09-13-pop-the-football-design.md)

The slots let a player hand a pick to the house. This does the opposite: it adds
a game the league plays *against* the house, and keeps it entirely out of the
picks. One shared dealer hand a week, everybody plays it, and the chips settle
nothing until two players finish level on points.

## The constraint that shaped everything

> "We need a fun way that has super minimal impact on the actual picks. Like the
> slots are super fun but the actual payout of them is very very small."

Pop the football is worth **one point, to one person, about once a season**. That
is the budget, and blackjack does not get to exceed it — so blackjack pays no
points at all. It pays chips, and chips are consulted only as a tiebreaker.

Two mappings were rejected on this constraint alone: playing a hand *against your
own spread* (hit to push your line for more points) and *hitting for extra games*
beyond the weekly four. Both are good games. Both move the standings every week,
which is the one thing this must not do.

## Where the chips actually land

Chips are the **third regular-season tiebreaker**:

| | Before | After |
|---|---|---|
| 1 | Conference championship game wins | Conference championship game wins |
| 2 | Army v Navy | Army v Navy |
| 3 | Celebration Bowl | **Chips** |
| 4 | Win % last 8 games | Celebration Bowl |
| 5 | Coin flip | Win % last 8 games |
| 6 | | Coin flip |

This is a commissioner's decision that a hand of cards outranks the Celebration
Bowl in deciding money, and it was taken deliberately. Chips stay below the two
marquee games.

**The playoff cascade is untouched.** Army-Navy, regular season points, other
bowl games — chips are not in it. The table still runs during the playoffs
because the fun should not stop in December, but those chips are only ever read
as a regular-season tiebreaker.

**The cascade is not implemented in code, and this does not implement it.** The
leaderboard sorts on points and then alphabetically (`index.html:1391`); the
published order is a procedure the commissioner applies at season's end with the
tiebreaker boards as evidence. Chips join that evidence. No scoring path, no
points column and no sort comparator is touched by this feature — which is the
whole reason it satisfies the constraint above.

## The felt

| | |
|---|---|
| Starting bankroll | **1,000**, reset every season |
| Table minimum | **25**, or your whole stack if it is less |
| Table maximum | **500** |
| Blackjack pays | **3:2** |
| Dealer | stands on all 17s |
| Doubling | allowed on the first decision only, for the full bet |
| Splitting, insurance, surrender | not offered |
| Bust your bankroll to zero | out for the season. No reload. |

### Why a bankroll and not an ante

A fixed ante everyone pays each week would make the chip count an honest record
of twenty decisions, and it was the recommendation. The bankroll was chosen
instead, with its downsides accepted on the record: **the swings are unbounded
and the season can be decided early.**

Two rules contain it, and both are already printed on every felt in Nevada:

- **The table maximum is what stops a runaway.** You may shove 500 of your
  opening 1,000, but a player sitting on 3,000 can still only bet 500 — so a
  leader cannot compound away from the table, and somebody 2,000 behind in week 8
  can still catch up.
- **Your remaining stack is always a legal bet.** Without this, a 25 minimum and
  no reload means a player stranded on 18 chips is out while still holding
  chips, which is a fiddly way to die. You are out at exactly zero and nowhere
  else. This is the table letting you play your last chips, not the house handing
  you any — it does not reintroduce the reload.

### Not playing is free, and that is a known hole

No ante means a week you skip costs you nothing. It also means **the optimal
tiebreaker strategy is to never sit down**: blackjack carries a house edge, so a
player who cares only about the tiebreaker keeps an untouched 1,000 and beats
everyone who played and ran bad.

Paying 2:1 on blackjacks would have flipped the edge to the player and closed
this structurally. It was offered and declined in favour of real Vegas rules, so
**this is policed socially, on the existing precedent**: a pure coin-flipper
already finishes mid-table without a rule to stop them, and the league can be
trusted to give a hard time to a man protecting a virgin stack for a tiebreaker
that fires once in three seasons.

The board makes it visible rather than hiding it. A player sitting on exactly
1,000 in week 12 has obviously never played, and the column says so.

### The dealer can have a natural, and the peek is applied backwards

This is real blackjack, so a dealer natural is in. It carries one wrinkle that
has to be solved rather than waved at.

In the American game a dealer showing an ace or a ten **peeks** at the hole card,
and if it is a natural the hand ends right there — nobody hits, nobody doubles,
and every player loses their original bet or pushes with a natural of their own.
The peek exists precisely so that nobody can double into a hand that was already
over.

A hole card sealed from Tuesday to kickoff cannot peek. So the peek is applied
backwards at settlement: **a dealer natural settles the week as though nobody had
acted.**

- the player's original bet loses
- a player holding their own natural pushes
- a double is refunded — the original bet is all that is at risk
- every card the player drew during the week is void, because a real peek would
  have stopped them being drawn at all

That last line is the one worth reading twice. A player who doubled on Wednesday
and drew to a hard 20 still loses only their original bet, and a player who
busted on Thursday loses only their original bet too. Both are correct: the hand
they played never should have existed.

**This is not a rule bend.** It is the standard US "original bets only" result,
reached the only way a hand that lasts five days can reach it. The alternative —
letting a sealed hole card take doubled bets — is the European no-hole-card game,
and that variant does not deal a hole card on Tuesday at all.

## The week

The dealer is shared. The whole league plays one hand against one dealer, which
is simply what a blackjack table is — and it is the same trick that makes the
football work: one thing everybody is watching at once.

| When | What |
|---|---|
| **Tuesday**, when the lines lock | The week's dealer hand is dealt. Up-card face up, hole card sealed in Postgres. |
| **Any time before first kickoff** | Place a bet, take two cards, then hit / stand / double. Every action is a server round trip. Standing, busting or doubling makes the hand final. |
| **First kickoff** | The hole card flips, the dealer draws to 17, and the whole table settles against that one hand. |
| **First time they open the Tiebreakers board** | You are shown the dealer's hand and your result. |

**The table opens when the lines lock** because that is the moment the week
becomes real, and `20260822010000_line_locks_tuesday.sql` already defines it. The
opener is idempotent and runs from cron every minute rather than firing once at
11:00 — a scheduled moment that gets missed leaves the league with no table all
week, and a self-healing check costs nothing.

**A hand still live at first kickoff stands where it is.** You left it on 16, it
plays as 16. No forfeit, no notification. The alternatives are punishing somebody
for a hand they did start, or letting them act after the lock.

### The week's lock is a different question from a game's lock

`picks_open_for_game()` answers "is this game still open", and in the playoffs it
answers per game. A hand is not attached to a game, so it needs "is this week's
table still open" — the first picked game of the week kicking off, in every week
including the playoffs.

That is a second deadline definition in a repo whose own rule is that there is
never a second definition to drift out of step, so it is called out rather than
buried: **it is a different question, not a second answer to the same one.**
`bj_week_open()` reuses the regular-season branch of `picks_open_for_game()`
verbatim and applies it to playoff weeks too, where per-game locks are
meaningless for one weekly hand.

## Schema

### The dealer's hand is two tables, and that is the point

RLS hides rows, not columns. A hole card in a table the league can read is a hole
card the league can read.

```sql
-- Public. One row per week. The up-card everybody argues about.
create table public.blackjack_weeks (
  cfb_season   integer not null,
  week         integer not null,
  dealer_up    smallint not null,      -- the only card visible before the lock
  dealer_final smallint[],             -- null until settled, then the whole hand
  opened_at    timestamptz not null default now(),
  settled_at   timestamptz,
  primary key (cfb_season, week)
);

-- Private. No select policy for anybody, ever. Security-definer functions only.
create table public.blackjack_hole (
  cfb_season  integer not null,
  week        integer not null,
  hole        smallint not null,
  primary key (cfb_season, week)
);
```

**The hole card is dealt on Tuesday, not at settlement.** Dealing it at
settlement would let the house choose its second card after watching the table
play, and no amount of "the server is trusted" makes that honest. Committing it
up front is also the better story: that card has been face down since Tuesday.

### The hands

```sql
create table public.blackjack_hands (
  id          bigserial primary key,
  team_id     bigint  not null references public.teams(team_id),
  cfb_season  integer not null,
  week        integer not null,
  bet         integer not null check (bet > 0),
  doubled     boolean not null default false,
  cards       smallint[] not null,
  state       text not null check (state in ('live','stood','busted','settled')),
  outcome     text check (outcome in ('win','lose','push','blackjack')),
  payout      integer,                 -- signed chip delta, null until settled
  dealt_at    timestamptz not null default now(),
  settled_at  timestamptz,
  unique (team_id, cfb_season, week)
);
```

That `unique` constraint is the entire one-hand-a-week rule. Not a check in
application code that can be worked around — a constraint that makes a second
hand un-insertable, the same way `slots_jackpot`'s `check (id = 1)` makes a
second meter un-insertable.

### Two deliberate absences

**No bankroll column.** It is a view over the hands, `1000 + sum(payout)` per
player per season, with a `hands_played` count beside it.

A stored balance is a second source of truth that drifts from the hands that
produced it. This is a few hundred rows a season, there is nothing to optimise,
and deriving it means the tiebreaker number is one `select` in which every chip
traces back to a hand somebody actually played.

**No shoe.** Cards are drawn on demand as a uniform pick from 0..51 — rank is
`n % 13`, suit is `n / 13`, which gives the correct 4-in-13 chance of a
ten-value. A shoe exists so that cards can be counted, and nobody is counting
across one hand a week. It also removes a second hidden table.

## The functions

All `security definer`, all checking `bj_week_open()`:

| | |
|---|---|
| `bj_open_week()` | idempotent; deals the week's dealer hand once the lines have locked |
| `bj_deal(bet)` | table open, bet legal, bankroll covers it, no hand this week yet → your two cards and the up-card |
| `bj_hit()` | one card; bust over 21 |
| `bj_double()` | first decision only, full bet, exactly one card, hand final |
| `bj_stand()` | hand final |
| `bj_settle_week()` | flip the hole card, draw to 17, settle every hand |

Plus `bj_total(smallint[])`, `immutable`, returning the best total with aces at
11 demoted to 1 while the hand is over 21. One function so that the player's
total, the dealer's draw rule and the settlement cannot disagree about what a
hand is worth.

**A bet is legal when** it is between `least(25, chips)` and `least(500, chips)`
inclusive. **Doubling requires** `bet * 2 <= chips` — no doubling for less, and
the button is simply disabled when the stack will not cover it.

**"Open" means the dealer hand exists, not merely that nothing has kicked off.**
`bj_week_open()` is true on Monday as well — no game has started — so a bet
placed before the lines lock would be a bet against a dealer who has not been
dealt. `bj_deal()` therefore requires the week's `blackjack_weeks` row to be
present, which makes `bj_open_week()` the thing that opens the table and Tuesday
the real start of the week.

**The week the felt plays is the league's current picked week**, the same one the
leaderboard defaults its board to. Not the season the board is *showing*: a
player browsing 2025 in October is still playing this week's hand, because there
is only ever one live hand and it belongs to now.

### Settlement is modelled on `resolve_slot_picks()`

Idempotent, one-way, guarded on `settled_at is null`, cron every minute, and
called once on leaderboard load so a dead cron cannot hide a result.

**`revoke execute ... from public` is the load-bearing line, not the grant.**
Postgres grants EXECUTE to PUBLIC on every new function, and this repo has
already shipped that bug once: the slots rollout found `anon` holding EXECUTE on
`resolve_slot_picks()` because granting to `authenticated` had been assumed to be
exclusive. Every function above revokes from `public` and `anon` explicitly.

### What players can see

Mirrors `picks_hidden_until_lock`: your own live hand is visible only to you, and
the whole table opens once the week settles. Watching what everybody did is most
of the entertainment and none of it can leak while it still matters.

No insert, update or delete policy exists on either hands table. The functions
are `security definer` and bypass RLS; nothing else may write a chip.

## The pit on the tiebreaker board

`tiebreakers.html` is a redirect stub — the board moved into the leaderboard's
week picker as `Tiebreakers` and `Playoff Tiebreakers`. The pit goes into that
view, which is the right home: chips are a tiebreaker, so they belong where
tiebreakers are settled.

**The felt, above the board.** Always the current week's hand, whatever season
the board is showing — the two are different questions and both are worth
answering at once.

```
┌─ THE PIT ────────────── WEEK 5 ─────────────────────┐
│   DEALER          [6♣]  [ ?? ]     sealed Tuesday   │
│   YOU             [K♠]  [7♦]              = 17      │
│   BANKROLL 1,240        BET 200                     │
│        [ HIT ]   [ STAND ]   [ DOUBLE ]             │
│   Settles at first kickoff · Sat 11:00 AM CT        │
└─────────────────────────────────────────────────────┘
```

After the week settles it becomes the interesting version: every hand in the
league face up beside what the dealer had. Who hit on 18 against a 6 is the part
people will talk about.

**The chip column, on the board itself.** The tiebreaker board is already players
× tiebreaker games with its own tally. Chips are one more column, and reading
straight across it tells you who wins a tie:

```
                CONF   ARMY   CELEB           SEASON
 PLAYER         CHAMP  -NAVY  BOWL    TB PTS  CHIPS
 ─────────────────────────────────────────────────────
 KEVIN            ✓      ✓      ✓        3    1,840
 CHARLIE          ✓      ✓      ·        2      515
 DEREK            ✓      ·      ✓        2    1,000   ← never played
 STEVE            ·      ✓      ✓        2        0   ← busted, wk 6
```

## The reveal

**The first time a player opens the Tiebreakers board after their week has
settled, they are shown the dealer's hand and their own result.** Once per
player per week.

**It deliberately does not fire on the leaderboard.** That was the first design
and it was wrong. The weekly board already flips every pick, spins the slots
cells and runs seven seconds of football; a card table barging in on top of all
that is noise, not an event. The reveal belongs on the board the chips belong
to, and a player who does not go there has lost nothing — the result is sitting
on the felt whenever they next arrive.

| | |
|---|---|
| 0.6s | Your hand slides in as you left it, with its total |
| 0.8s | **The hole card flips.** The card that has been face down since Tuesday |
| 0.7s each | The dealer draws to 17, one card at a time |
| 1.2s | The verdict: the outcome, the chip delta, the new bankroll |

**Which moves the discoverability problem somewhere it has to be solved
properly.** A reveal on the leaderboard would have found every player whether
they wanted it or not; a reveal on the Tiebreakers board only finds the ones who
go looking, and nothing on this site currently tells anybody that a hand is
waiting. So the picks page carries a one-line nudge beside the football —
*"Your hand this week →"*, linking through to the board — which is now
load-bearing rather than a nicety. It is a line of text, not a second card
table: the picks page keeps the football and gains nothing else.

**The identical-frames rule does not need enforcing here,** unlike the football.
The dealer's hand *is* the result, so there is nothing to telegraph: you learn
the outcome at the exact moment the dealer's total is known, and the sequence is
naturally a different length depending on how many cards the dealer draws.

Once per browser per week, on the same reasoning the slots spin accepted — a
`localStorage` key rather than a column and a write on every load, because seeing
your own hand twice is not a problem. Clickable to replay, forever, the way a
chip-edged slots cell already is. Reduced motion gets the result and none of the
seconds. It fires only for a signed-in player who had a hand that week: a
signed-out visitor and a player who sat out both see nothing.

## Failure modes

| If | Then |
|---|---|
| cron is down at kickoff | the leaderboard's own call to `bj_settle_week()` settles the week on first load |
| both fail | the felt reads "settling" and the chip column holds last week's total. No wrong chips, no crash |
| a week has no picked games | no table is opened, nothing settles, nothing is shown |
| nobody plays a week | the dealer hand is dealt and settled anyway and `dealer_final` recorded. Harmless |
| a player bets and never acts | the hand stands at whatever it holds when the week locks |
| a player's stack drops below 25 | their whole stack is their only legal bet |
| a player hits exactly zero | out for the season; the column reads 0 and the felt says so |
| a player tries to bet after the lock | `bj_deal()` refuses on `bj_week_open()`, the same check the picks deadline uses |
| a player forges chips from the console | there is no write path. Every chip comes from a `payout` written by a security-definer function |
| the dealer turns over a natural | the week settles as though nobody acted: originals lose, doubles refunded, player naturals push |
| two players tie on points and chips | the cascade falls through to the Celebration Bowl, as it does today |

## Testing

Extending the existing harness (`scratchpad/verify`), which already runs shipped
modules against a small DOM:

- `bj_total()` on aces: `[A,K]` is 21, `[A,A,9]` is 21, `[A,A,A,8]` is 21, `[A,9,5]` is 15
- the dealer draws to 17 and stops, including soft 17
- a dealer natural voids the week's play: doubles refunded, player naturals push, everyone else loses their original bet only, and a player who busted loses only that too
- settlement is idempotent: a second and third call move zero chips
- a live hand at lock settles on its standing total
- bet limits at the boundaries: 24 refused, 25 taken, 500 taken, 501 refused, a stack of 18 takes exactly 18
- doubling refused when `bet * 2` exceeds the stack
- a bankroll at zero can place no bet at all
- the reveal fires once per week per browser and survives a re-render
- the reveal fires after the football, never during it

RLS, impersonating a real non-commissioner member, the way the slots rollout did:

| attempt | expected |
|---|---|
| read the hole card | 0 rows — no policy exists |
| read another player's live hand | 0 rows |
| read another player's settled hand | allowed |
| insert a hand directly | blocked |
| update their own `payout` | blocked |
| call `bj_settle_week()` as `anon` | 401 |

## Rollout

Migrations in this project are applied by hand with `supabase db query -f`, not
`db push` — the remote has no `supabase_migrations.schema_migrations` table at
all, and pushing would replay every migration against a database that already
has them. Verify inside transactions that get rolled back, then sweep for test
rows, as the slots rollout did.

Order: migration, then the tiebreaker view, then the reveal. A felt that calls
`bj_deal()` against a database without the function fails loudly on the first
bet.

## Out of scope

- Splitting, insurance and surrender. Splitting is the one that will get argued
  about — a pair of 8s against a ten is the most famous play in the game — but it
  doubles every piece of hand state and needs rules for re-splits and split aces,
  and doubling down already gives the week a real decision.
- Chips in the playoff cascade.
- Any conversion of chips into points, ever. That is the constraint the whole
  design exists to respect.
- Card counting, a shoe, and any multi-hand play.
