# Let the slots decide

**Status:** shipped and verified against the live database, 2026-09-11
**Date:** 2026-09-11

A player who cannot call a game hands it to the house. Instead of picking a
side they choose "Let the slots decide", and at kickoff the database flips a
coin for them. The first time anyone opens the leaderboard afterwards, that one
cell spins like a slot machine before showing what the coin flip already said.

The front end of the reveal is already built and approved, in the commissioner
sandbox (`js/sandbox.js`, commit `cadeecf`). This spec is about making it real:
where the choice is recorded, who flips the coin, and when.

## Two decisions already taken

**The coin flips at kickoff, server-side.** Saving a slots pick records only the
intent — `pick = null, by_slots = true`. A cron job resolves it the moment the
week locks.

The alternative was flipping in the browser at save time, which is far less
work. It was rejected because the player would see their own pick the instant
they saved it, so the reveal would be news to everyone except the one person
who chose it — and because they could keep re-saving until they liked the
answer, which is the opposite of handing it to chance.

**No limit on how many games a week go to the slots.** Nothing to enforce and
nothing to explain. A pure coin-flipper finishes mid-table over a season, so the
standings police it without a rule.

## Schema

One migration, `supabase/migrations/20260911_let_the_slots_decide.sql`.

```sql
alter table public.picks
  add column if not exists by_slots boolean not null default false,
  add column if not exists slots_resolved_at timestamptz;

alter table public.picks alter column pick drop not null;
```

`pick` is dropped to nullable explicitly rather than assumed: a slots pick has
no team in it between saving and kickoff, and `submission_status()` already
tests `p.pick is not null`, which implies it is nullable today but does not
prove it.

`slots_resolved_at` is not decoration. It is the audit trail that says the flip
happened once, at a time, and was never touched again.

An index for the resolver, which runs every minute:

```sql
create index if not exists picks_unresolved_slots
  on public.picks (game_id) where by_slots and pick is null;
```

Partial, so it holds only the handful of rows that are actually pending.

## Resolving the flip

```sql
create or replace function public.resolve_slot_picks()
returns integer
language plpgsql
security definer
set search_path = public
as $$
declare n integer;
begin
  with due as (
    select p.id, g."Away" as away, g."Home" as home
      from public.picks p
      join public.all_games g on g."GameId" = p.game_id
     where p.by_slots
       and p.pick is null
       and not public.picks_open_for_game(p.game_id)
  )
  update public.picks p
     set pick = case when random() < 0.5 then d.away else d.home end,
         slots_resolved_at = now()
    from due d
   where p.id = d.id;
  get diagnostics n = row_count;
  return n;
end;
$$;
```

Three properties that matter:

- **It reuses `picks_open_for_game()`**, so "locked" means exactly what it means
  everywhere else on the site. There is no second definition of the deadline to
  drift out of step.
- **It is idempotent and one-way.** The `pick is null` guard means a resolved
  pick can never be re-rolled — not by the cron, not by a second caller, not by
  the commissioner through this function.
- **`random()` is evaluated per row** inside `update ... from`, so four slots
  picks in one week get four independent flips rather than one shared one.

Scheduled every minute:

```sql
select cron.schedule('resolve-slot-picks', '* * * * *',
                     $$select public.resolve_slot_picks();$$);
```

Every minute rather than every five, because the leaderboard reveals at kickoff
and an unresolved slots pick renders as an empty cell. A minute of exposure is
tolerable; five is not.

Belt and braces, the leaderboard also calls it once on load:

```sql
grant execute on function public.resolve_slot_picks() to authenticated;
```

Granted to `authenticated` only, not `anon`. It is idempotent and cheap, but a
publicly callable write is a publicly hammerable one, and signed-out visitors
are served perfectly well by the cron.

## Keeping the intent honest

Nothing stops a member opening the console and writing `by_slots = true` on a
pick they chose themselves, which would earn them a spin for a pick they made.
Cosmetic rather than competitive, but the fix is three lines:

```sql
create policy picks_slots_start_empty on public.picks
  as restrictive for insert to authenticated
  with check (not by_slots or pick is null or public.is_commissioner());

create policy picks_slots_start_empty_upd on public.picks
  as restrictive for update to authenticated
  with check (not by_slots or pick is null or public.is_commissioner());
```

Restrictive, matching the deadline policies already on this table: Postgres ANDs
them with what is there, so they can only tighten. `resolve_slot_picks()` is
`security definer` and owned by the table owner, so it bypasses RLS and is
unaffected.

No change is needed to hide a pending slots pick. `picks_hidden_until_lock`
already hides other people's picks until the week locks, and before then a slots
pick contains nothing to hide.

## `submission_status()` has to learn about it

The "Made Picks / Not Yet" panel currently tests:

```sql
and p.pick is not null and btrim(p.pick) <> ''
```

A player who slots all four games would show as **not submitted** all week.
Changed to:

```sql
and (p.by_slots or (p.pick is not null and btrim(p.pick) <> ''))
```

This is the one existing behaviour the feature would silently break, so it is
called out rather than buried in the migration.

## Scoring needs no change

Points are computed in the browser, in `index.html`, by matching each pick
against `winner`. A resolved slots pick is an ordinary pick and scores normally.
An unresolved one is null, matches nothing, and scores zero — which is correct,
because the game it belongs to has not kicked off.

## The picks page

`gameCardHtml()` currently renders two radios sharing a `name` of
`game_<id>`. A third joins them, below the pair, full width:

```
  ┌──────────────────┬──────────────────┐
  │   away  +7.5     │   home  -7.5     │
  └──────────────────┴──────────────────┘
  ┌─────────────────────────────────────┐
  │  🎰  Let the slots decide           │
  └─────────────────────────────────────┘
```

It is a radio in the same group, `value="__slots__"`, which means it costs
almost nothing elsewhere: `selections()`, the local draft, and the lock-time
`disabled` sweep all keep working unchanged because they operate on the group,
not on the two sides.

What does change:

| Place | Change |
|---|---|
| `savePicks()` | `__slots__` becomes `{ pick: null, by_slots: true }`; every other pick gains `by_slots: false`, so switching away from slots clears the flag |
| picks query (line ~945) | select `by_slots` alongside `pick` |
| `prefillSelections()` | a saved row with `by_slots` re-checks the slots radio rather than neither |
| after lock | a slots pick shows "Slots — decided at kickoff" until `pick` fills in, then the team |

`by_slots: false` on ordinary picks is load-bearing. Without it, changing a
slots pick to a real one would leave the flag set and the cell would spin for a
pick the player made themselves.

## The reveal moves out of the sandbox

Without this the feature does nothing visible, so it is in scope.

`js/sandbox.js`'s slot section moves to a new `js/slots.js`, imported by
`index.html`. Three things change on the way:

1. **Which cells spin** stops being a random sample of the board and becomes the
   real answer: cells whose pick row has `by_slots`. `renderTable()` tags those
   cells with `data-slots` and the module reads it. The `stableRank()` helper
   that kept the mock's choice stable across loads is deleted — it existed only
   to fake what the data now says.
2. **The once-only record** keys on `(team_id, game_id)` instead of row and
   column position.
3. **The spin waits for the reveal.** It hooks the existing
   `pendingRevealAnimation` path rather than firing on load, so a slots cell
   flips over with the rest of the board and then spins, instead of spinning
   before the board has turned over.

Everything else — the reel, the warps, the timings, the reduced-motion path —
ships as approved.

The sandbox keeps its copy? No. It is deleted from `js/sandbox.js` and
`css/sandbox.css` when the real one lands, so there is one implementation rather
than two that can disagree.

### Seen-once is per browser

The record of "you have watched this cell" lives in `localStorage`, so a player
who opens the board on their phone and then their laptop sees the spin twice.

Accepted deliberately. The alternative is a `slots_seen_at` column and a write
on every leaderboard load, which is real cost for a real problem — and seeing
your own slot machine a second time is not a problem. Worth revisiting only if
someone complains.

## Failure modes

| If | Then |
|---|---|
| cron is down at kickoff | the leaderboard's own call to `resolve_slot_picks()` resolves it on first load |
| both fail | the cell renders "—" and scores zero, exactly as an unsubmitted pick does today. No crash, no wrong points |
| a game has no `Away`/`Home` | the join yields nothing, the row stays unresolved, and it is visible as `by_slots` with a null `slots_resolved_at` |
| a player slots every game, every week | allowed by decision; they finish mid-table |
| player switches slots → real pick before lock | `by_slots` goes false on upsert; no spin |
| player switches real pick → slots before lock | `pick` is nulled on upsert; resolves at kickoff |

## Testing

The existing harness (`scratchpad/verify`) runs the shipped modules against a
small DOM, and covers the reveal already. Extending it:

- a cell spins **only** when its row carries `by_slots`
- the landing symbol still cannot contradict the stored pick
- once-only keys on `(team_id, game_id)` and survives a re-render
- the spin runs after the reveal flip, not before

SQL was verified against the live database in rolled-back transactions rather
than a local instance — see Rollout below for what each check found.

## Rollout — what actually happened

The CLI turned out to be authenticated and already linked, so this was applied
and verified directly.

**`supabase db push` would have been wrong.** The remote has no
`supabase_migrations.schema_migrations` table at all: this project has never
used push, and the files in `supabase/migrations/` are a written record of
changes applied by hand. Pushing would have replayed all thirty against a
database that already has them. Applied with `db query -f` instead, which is how
every other migration here landed.

### A bug this caught

Verification came back showing `grant PUBLIC EXECUTE` on `resolve_slot_picks()`.
Postgres grants EXECUTE to PUBLIC on every new function, so granting it to
`authenticated` and stopping there left `anon` holding it — the opposite of what
the comment beside the grant claimed. **The REVOKE is the load-bearing line, not
the grant.** Fixed; anon now gets `401 permission denied` on the RPC.

Also worth recording: `pick` was already nullable, so that `alter` was a no-op.

### Verified behaviour

Everything below ran inside transactions that were rolled back. A final sweep
confirmed zero test rows, zero `by_slots` rows and zero `slots_resolved_at`
stamps left in production.

| | |
|---|---|
| flips only locked games | a finished game resolved to a real side; an unkicked week-2 game stayed null |
| one-way | first call resolved 2 rows, second and third returned 0, zero rows moved afterwards |
| independent flips | two games, two different sides |
| `submission_status()` | false with no picks, true with a *pending* slots pick, still returns no pick values |
| REST | all three front-end queries 200 (they were 400 before the migration) |

RLS, impersonating a real non-commissioner member via `request.jwt.claims`:

| attempt | result |
|---|---|
| save a slots pick on an open game | allowed |
| claim `by_slots` with a team already chosen | **blocked** |
| slot a game that already kicked off | **blocked** |
| slot a game for another player | **blocked** |
| fill in their own pending slots pick | **blocked** |
| switch slots → real pick, and back, before lock | allowed both ways |
| another member reads that pending pick | 0 rows visible |

### The check that nearly got missed

The first resolver test ran on an admin connection, which bypasses RLS anyway —
so it proved nothing about whether `picks_slots_start_empty_upd` would block the
resolver in production. It does not, for two independent reasons: `picks` has
`relforcerowsecurity = false` so the table owner bypasses RLS, and the policy is
scoped `to authenticated` while the function and its cron job both run as
`postgres`. Confirmed empirically as well as structurally.

Cron `resolve-slot-picks` is owned by `postgres`, active, 9 runs in the first
hour, 0 failures.

Order matters: migration first, then the picks page, then the reveal. A picks
page that writes `by_slots` to a database without the column fails loudly on
save.

## Out of scope

- Any limit on slots picks per week (decided against)
- Showing anyone *why* a pick was slotted, beyond the spin itself
Originally out of scope and since pulled in at the commissioner's request:
**slot picks no longer train the analytics model.** `buildTrainingRows()` drops
any row with `by_slots`, because a coin flip is indistinguishable in the table
from a considered pick and would be read as evidence about how that person
thinks. It would land hardest on the weakest thing in the model — personal
lay-rate already sits at split-half r=0.117 — while looking like more data.
They still score, still show, still spin; they are just not evidence.
