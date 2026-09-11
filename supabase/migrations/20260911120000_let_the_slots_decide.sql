-- Let the slots decide.
--
-- A player who cannot call a game hands it to the house: instead of picking a
-- side they choose "Let the slots decide", and the database flips the coin for
-- them at kickoff. The leaderboard then spins that one cell like a slot machine
-- the first time anyone opens the board.
--
-- WHY THE FLIP HAPPENS HERE AND NOT IN THE BROWSER. Doing it at save time would
-- be far less work - no nullable pick, no cron, no change to
-- submission_status(). It was rejected for one reason: the player would see
-- their own pick the instant they saved it, so the reveal would be news to
-- everyone except the one person who chose it. Second reason, smaller but real:
-- they could keep re-saving until they liked the answer, which is the opposite
-- of handing it to chance.
--
-- So a slots pick is stored as an INTENT - `by_slots` true, `pick` null - and
-- stays empty until the week locks. Nobody can read what nobody has written.

-- 1. THE COLUMNS -----------------------------------------------------------

alter table public.picks
  add column if not exists by_slots boolean not null default false,
  add column if not exists slots_resolved_at timestamptz;

-- Dropped explicitly rather than assumed. submission_status() already tests
-- `p.pick is not null`, which suggests the column is nullable today but does
-- not prove it, and a slots pick has nothing in it between saving and kickoff.
alter table public.picks alter column pick drop not null;

comment on column public.picks.by_slots is
  'The player chose "let the slots decide" rather than a side. Set on save with a null pick; resolve_slot_picks() fills the pick in at kickoff.';
comment on column public.picks.slots_resolved_at is
  'When the coin was flipped. The audit trail that says it happened once and was never touched again.';

-- Partial, so it holds only the handful of rows actually pending rather than a
-- row per pick ever made. This is the index the resolver runs against every
-- minute, and outside the hour either side of kickoff it is empty.
create index if not exists picks_unresolved_slots
  on public.picks (game_id) where by_slots and pick is null;

-- 2. THE FLIP --------------------------------------------------------------

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

-- Three properties worth stating, because each one is load-bearing.
--
--   * It reuses picks_open_for_game(). "Locked" therefore means exactly what it
--     means everywhere else on this site, and there is no second definition of
--     the deadline to drift out of step with the first.
--
--   * It is one-way. The `pick is null` guard means a resolved pick can never be
--     re-rolled - not by the next cron tick, not by a second caller racing the
--     first, not by the commissioner through this function. The UPDATE is a
--     single statement, so the race is settled by Postgres rather than by luck.
--
--   * random() is evaluated PER ROW inside `update ... from`, so four slots
--     picks in one week get four independent flips rather than one shared coin
--     landing the same way four times.

comment on function public.resolve_slot_picks() is
  'Flip the coin for every slots pick whose game has locked. Idempotent and one-way: a resolved pick is never re-rolled.';

-- Callable by signed-in members so the leaderboard can resolve on load if the
-- cron is ever late. Deliberately NOT callable by anon: it is idempotent and
-- cheap, but a publicly callable write is a publicly hammerable one, and
-- signed-out visitors are served perfectly well by the schedule below.
--
-- THE REVOKE IS THE LOAD-BEARING LINE, not the grant. Postgres gives EXECUTE to
-- PUBLIC on every new function, so granting to `authenticated` and stopping
-- there leaves anon holding it anyway - which is what happened on the first run
-- of this migration, and it verified as `grant PUBLIC EXECUTE` rather than the
-- restriction the comment above claimed.
revoke execute on function public.resolve_slot_picks() from public;
revoke execute on function public.resolve_slot_picks() from anon;
grant  execute on function public.resolve_slot_picks() to authenticated;

-- 3. THE SCHEDULE ----------------------------------------------------------

-- Every minute, matching the live-score jobs. The leaderboard reveals at
-- kickoff and an unresolved slots pick renders as an empty cell, so a minute of
-- exposure is tolerable where five would not be.
--
-- Cheaper than it looks next to its neighbours: sync_espn_scores and friends
-- run on this cadence and each makes an HTTP call to ESPN. This one is a single
-- indexed local query that touches nothing at all for all but a few minutes a
-- week.
select cron.unschedule('resolve-slot-picks')
 where exists (select 1 from cron.job where jobname = 'resolve-slot-picks');

select cron.schedule('resolve-slot-picks', '* * * * *',
  $job$select public.resolve_slot_picks()$job$);

-- 4. KEEPING THE INTENT HONEST ---------------------------------------------

-- Nothing above stops a member opening the console and writing by_slots = true
-- onto a pick they chose themselves, which would earn them a slot machine for a
-- decision they made. Cosmetic rather than competitive, but it is three lines to
-- close.
--
-- RESTRICTIVE, matching the deadline policies already on this table: Postgres
-- ANDs them with the permissive ones, so they can only ever tighten access.
-- resolve_slot_picks() is security definer and owned by the table owner, so it
-- bypasses RLS and is unaffected by either of these.

drop policy if exists picks_slots_start_empty_ins on public.picks;
create policy picks_slots_start_empty_ins on public.picks
  as restrictive for insert to authenticated
  with check (not by_slots or pick is null or public.is_commissioner());

drop policy if exists picks_slots_start_empty_upd on public.picks;
create policy picks_slots_start_empty_upd on public.picks
  as restrictive for update to authenticated
  using      (public.is_commissioner() or not by_slots or pick is null)
  with check (public.is_commissioner() or not by_slots or pick is null);

-- No policy is needed to HIDE a pending slots pick. picks_hidden_until_lock
-- already hides other people's picks until the week locks, and before then a
-- slots pick contains nothing to hide.

-- 5. THE ONE THING THIS WOULD OTHERWISE BREAK ------------------------------

-- submission_status() decides who appears under "Made Picks" and who sits under
-- "Not Yet". It tests that a pick has a team in it - which a slots pick does not
-- until kickoff, so a player who slotted every game would show as having
-- submitted nothing all week.
--
-- Otherwise unchanged: it still returns only a boolean and never a pick value,
-- which is the whole reason it exists.
create or replace function public.submission_status(p_season int, p_week int)
returns table(team_id bigint, team_name text, submitted boolean)
language sql
stable
security definer
set search_path = public
as $$
  select t.team_id,
         t.team_name,
         exists (
           select 1
             from public.picks p
             join public.all_games g on g."GameId" = p.game_id
            where p.team_id = t.team_id
              and g.cfb_season = p_season
              and g.week = p_week
              and coalesce(g.picked,false)
              and (p.by_slots
                   or (p.pick is not null and btrim(p.pick) <> ''))
         ) as submitted
    from public.teams t
   order by t.team_name;
$$;

grant execute on function public.submission_status(int, int) to anon, authenticated;
