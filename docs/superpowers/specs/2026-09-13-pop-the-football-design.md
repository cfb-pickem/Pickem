# Pop the football

**Status:** built and pushed, 2026-09-13
**Date:** 2026-09-13
**Builds on:** [Let the slots decide](2026-09-11-let-the-slots-decide-design.md)

Today a slots pick is a true coin flip: the database picks a side at kickoff and
the leaderboard spins that cell. This adds a third outcome. Somewhere behind the
coin there is a jackpot, and it grows every time anybody in the league pulls the
lever. When it finally hits, one person's pick is simply correct — whatever the
game does.

## The football is the meter

The league shares one football, drawn at the top of the leaderboard and the
picks page. It is the jackpot odds made visible.

This is the whole idea. A progressive jackpot is a number, and a number nobody
looks at is not a feature. A football that has been visibly straining since
Tuesday is one that people talk about on Thursday.

**The ball is always near bursting.** It is drawn taut from day one — leather
stretched, highlight tight, seam already pale. It does not start out new and
inflate over the season. What the meter drives is not its size but **how badly
it is behaving at rest**:

| Meter | The ball |
|---|---|
| 0.25% | A slow creak every twenty seconds. One lazy wobble. It's full, it's fine, it's been fine for weeks. |
| ~3% | Twitching every few seconds. Laces starting to gap. The occasional flinch that makes you look up. |
| ~7% | Near-constant jitter, a hairline of white down the seam, small bulges it can't quite hold in. |
| 10% | Barely containing itself. Continuous strain, seam split white, the whole thing vibrating. |

A ball that has looked about to burst for six weeks is funny rather than
dishonest, which is the effect we want. The honest channel is the hover: **"17
pulls since the last pop — 2.8%."** Anyone who wants the real number can have it,
so the drawing is free to be pure theatre.

## The numbers

| | |
|---|---|
| Floor | **0.25%** |
| Growth | **+0.15pp per pull** |
| Ceiling | **10%** |

The meter climbs 0.25% to 10% over 65 pulls but almost never gets there: median
pop lands around **pull 29**. At five slot picks a week that is a pop every six
weeks or so — roughly **two a season**. If the league leans in and doubles the
usage, four.

**What the ceiling actually buys.** It does not control how often the jackpot
hits; the ramp does. Once the ball is genuinely at 10% it pops within about ten
more pulls. What 10% guarantees is the worst case: the next pull is never better
than a one-in-ten shot, ever. That is the rail that keeps this from taking over
the league.

## What a pop is worth

**The pick is correct. One point. Nothing else.** No bonus, no multiplier, no
asterisk column.

In expectation that is worth half a point, which is not the point. Two moments a
season that the league argues about is the product; the half point is the excuse
for the theatre. It also means the standings need no explaining to anybody.

### The consequence that shapes the schema

The pop is decided at **kickoff**, and the game has not been played yet — so an
automatic win cannot be implemented as *set the pick to the winning team*. There
is no winner to set it to.

So a popped row keeps `pick` null forever and carries a flag the leaderboard's
scoring honours. One condition in one place, but worth stating plainly that
**this is the first time the slots feature touches scoring at all.** Today it
only ever writes a team name into a column that was already there.

The point still lands when the game does, not when the ball bursts: both scoring
paths gate on `winner` before they look at anything else, so an AUTO WIN scores
on Saturday night alongside every ordinary pick.

### And the cell says AUTO WIN, in words

There is no team behind a popped pick, so the cell does not draw one. It says
**AUTO WIN** where the crest would be.

This started as a gold chip edge around the crest — the board needed to explain
why a cell showing a plainly losing team had scored. Dropping the crest deletes
the problem instead of decorating it: a cell with no logo in it cannot show a
logo that lost. The mark from `markSlotCell()` stays exactly as it was, for
ordinary slots picks, and a popped cell gets none — it already says what happened
in plain words, and a border would be the board explaining the same fact twice
in two different codes.

The reel still runs on a popped cell. It lands on one of the two teams at
random — a decoy, never the answer — and the football takes it away at the end.
A reel that visibly declined to land would announce the jackpot before the ball
had even appeared.

## Where the roll lives

In `resolve_slot_picks()`, which already flips the coin, is already
`security definer`, and is already one-way.

**It must resolve row by row.** Today it is a single `update ... from`, and
`random()` is evaluated per row — so if the jackpot rolled inside that statement,
four people could pop the same meter in the same instant. That is the "everybody
gets a free win" failure, and it would happen the first busy Saturday. A handful
of rows a week makes the loop free.

Each row rolls; **the first one that hits claims the pop and resets the meter to
the floor in the same breath**, so everyone resolving after it is rolling at
0.25% again. One pop, one person, settled by Postgres rather than by luck.

**Order decides who gets first crack: kickoff time, then when the pick was
saved.** The earliest lever pull goes first. It is the fair reading of a queue,
and it quietly rewards picking on Tuesday rather than at 11:58 on Saturday.

**No weekly cap.** The reset already makes a second pop in one week a 1-in-400
tail. That is a real possibility rather than a bug, and it is allowed to happen.

### Schema sketch

```sql
-- One row, ever.
create table public.slots_jackpot (
  id                smallint primary key default 1 check (id = 1),
  pulls_since_pop   integer not null default 0,
  last_pop_at       timestamptz,
  last_pop_team_id  bigint references public.teams(team_id)
);

alter table public.picks
  add column if not exists jackpot boolean not null default false;
```

The meter increments **when a slots pick is saved**, by trigger — that is the
lever pull the league watches all week, and it has to be server-side because the
repo is public and the key is anon. Nothing in the browser decides anything,
same rule as the existing migration.

## The seven seconds

At kickoff, one unbroken sequence: the reels land as they always have, and then
the league's football rises over the board.

**One ball per batch, not one per cell.** Eight cells each running a seven-second
strain would be punishing, and the meter is shared by the whole league anyway —
so a shared ball is also the truer picture. That is not your football, it is
everybody's.

| | |
|---|---|
| — | Reels spin and land, staggered, exactly as today. |
| 0.9s | **Rise.** The ball comes up over the board, the page dimming behind it. |
| 0.9s | **Pump.** Hiss. One more notch of air. |
| 1.1s | **Creak.** The seam whitens. A 2px flinch, twice. |
| 1.2s | **Bulge.** It swells hard and holds far too long… |
| 0.7s | **Ease.** …and settles back. A squeak of escaping air. |
| 1.5s | **Bulge again.** Bigger, faster, laces separating, the seam split white. |
| — | Resolution. |

**No pop:** it settles, the note reads *"It held. This time."*, and the ordinary
slots cells take their chip edge.

**Pop:** white flash, screen shake, the shell blowing apart — *"POP. The house
pays."* The popped cell's AUTO WIN tag flares in where the decoy was, and the
football in the header deflates to the floor without waiting for a round trip.

**The rule that makes or breaks it:** every beat above is identical whether it
pops or not. Nothing branches until `burst`. If the pop version strained even
slightly harder the league would learn to read it within three weeks and the
tease would be dead.

Reduced motion gets the outcome and none of the seven seconds.

The whole sequence runs on timers rather than the Web Animations API, which
means `playbackRate` cannot reach it — so there is a `setRevealSpeed()` the
sandbox slider drives, and without it the lab would have been bending the reels
and not the ball.

## Replay

`js/slots.js` currently writes `cfb-slots-seen:{team}:{game}` and shows each
reveal once per person per game. That is what ate a spin this week, and it comes
out.

- **Auto-replay on every page load, for the current week's games.**
- **Click any chip-edged cell to replay it — any week, any time, forever.**

Once per page **load**, not per render: the leaderboard rebuilds itself every
minute on live scores, and replaying on render would have reels spinning
continuously all Saturday. The existing `MutationObserver` path stays exactly as
careful as it is now.

Bounding the auto-replay to the current week is what keeps the board readable:
unbounded, week 12 means forty cells spinning at once and a minute of football
before anybody can read a score. The click affordance is what makes that safe to
do — the chip edge already marks exactly the cells that have something to show,
so nothing is ever lost, it just waits to be asked for.

## What this touches

| File | Change |
|---|---|
| `supabase/migrations/20260913120000_pop_the_football.sql` | `slots_jackpot`, `slots_jackpot_odds()`, `picks.jackpot`, the pull trigger, `resolve_slot_picks()` rewritten row-by-row, the index rebuilt on the new guard, two RLS policies |
| `js/jackpot.js` (new) | the football component, its four rest states, and the odds mirrored from SQL |
| `js/slots.js` | the football sequence; seen-keys replaced by a per-load Set; click-to-replay; `setRevealSpeed()`, `forgetSpins()` |
| `js/utils.js` | `buildJackpotMap()` |
| `css/base.css` | the ball, the rest states, the reveal, the AUTO WIN tag |
| `index.html` | header football; the AUTO WIN cell; `jackpot` honoured in both scoring paths and selected in all four pick queries; `data-live-week` |
| `picks.html` | header football |
| `js/sandbox.js`, `css/sandbox.css` | a force-pop switch, because a real jackpot lands twice a season and the lab exists to watch it on a Tuesday |

## Open

Nothing is blocking. The two things to watch when it is real:

- **Usage is unknown.** There are zero slots picks in the database today, so
  every frequency figure above rests on a guess of five pulls a week. The ramp is
  one constant and should be tuned once there is a season of real pulls behind it.
- **Replaying the full seven seconds is a lot on a busy Saturday.** On a replay
  the viewer already knows the answer, so the strain could be shortened without
  breaking the identical-frames rule, which only governs first viewing. Left as a
  knob rather than a decision.
