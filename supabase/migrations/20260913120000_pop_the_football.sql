-- Pop the football.
--
-- A slots pick is a coin flip today. This puts a jackpot behind the coin: a
-- meter that climbs every time anybody in the league pulls the lever, and when
-- it finally hits, that player does not get a team at all. They get an AUTO
-- WIN — a point for that game, whatever the game does.
--
-- WHY THE WINNER IS NOT WRITTEN INTO `pick`. The roll happens at kickoff, and
-- the game has not been played yet, so there is no winner to write. A popped
-- pick therefore keeps `pick` null forever and carries a flag instead. The
-- leaderboard scores the flag directly and draws the cell as AUTO WIN, which is
-- also the honest thing to show: there is no team behind it, so there should
-- not be a crest pretending otherwise.
--
-- THE MATH, in one place so it can be tuned in one place:
--
--     odds = least(0.0025 + 0.0015 * pulls_since_pop, 0.10)
--
-- Floor a quarter of a percent, a sixth of a percent added per pull, hard
-- ceiling of one in ten. That puts the median pop around pull 29 - roughly two
-- a season at five slots picks a week. The ceiling does not govern how often it
-- lands (the ramp does); it governs the worst case, so the next pull is never
-- better than a one-in-ten shot no matter how long the drought runs.
--
-- Every figure above rests on a guess: there are zero slots picks in the
-- database as this ships, so "five a week" is invented. The ramp is one
-- constant and is meant to be retuned once a season of real pulls exists.

-- 1. THE METER -------------------------------------------------------------

-- One row, ever. The check constraint is the whole enforcement: a second row
-- cannot be inserted, so no code anywhere has to wonder which meter is live.
create table if not exists public.slots_jackpot (
  id                smallint primary key default 1 check (id = 1),
  pulls_since_pop   integer not null default 0 check (pulls_since_pop >= 0),
  last_pop_at       timestamptz,
  last_pop_team_id  bigint references public.teams(team_id) on delete set null
);

insert into public.slots_jackpot (id) values (1) on conflict (id) do nothing;

comment on table public.slots_jackpot is
  'The progressive jackpot behind "let the slots decide". One row. Incremented on every lever pull, reset to zero when it pops.';
comment on column public.slots_jackpot.pulls_since_pop is
  'Lever pulls since the last pop. The only input to the odds.';

-- Readable by everyone, because the football is drawn at the top of the
-- leaderboard and the picks page and both are served to signed-out visitors.
-- There is nothing in here to protect: it is a counter and a timestamp.
alter table public.slots_jackpot enable row level security;

drop policy if exists slots_jackpot_readable on public.slots_jackpot;
create policy slots_jackpot_readable on public.slots_jackpot
  for select to anon, authenticated using (true);

-- No write policy on purpose. The two functions below are security definer and
-- owned by the table owner, so they bypass RLS; nothing else may touch it.
revoke insert, update, delete on public.slots_jackpot from anon, authenticated;
grant select on public.slots_jackpot to anon, authenticated;

-- 2. THE ODDS --------------------------------------------------------------

-- A function rather than three constants inlined at the call site, because the
-- number is drawn on two pages and rolled in one, and three copies of a formula
-- is three chances for the football to be lying about the odds.
create or replace function public.slots_jackpot_odds(pulls integer)
returns numeric
language sql
immutable
as $$
  select least(0.0025 + 0.0015 * greatest(coalesce(pulls, 0), 0), 0.10);
$$;

comment on function public.slots_jackpot_odds(integer) is
  'Jackpot probability for the next pull. Floor 0.25%, +0.15pp per pull, ceiling 10%.';

grant execute on function public.slots_jackpot_odds(integer) to anon, authenticated;

-- 3. THE FLAG --------------------------------------------------------------

alter table public.picks
  add column if not exists jackpot boolean not null default false,
  add column if not exists slots_pull_counted boolean not null default false;

comment on column public.picks.jackpot is
  'The football popped on this pull. The pick is correct regardless of the result, and `pick` stays null because there is no team behind it.';
comment on column public.picks.slots_pull_counted is
  'This row has already put its pull into the meter. One row, one pull, however many times it is edited.';

-- A popped pick is a slots pick that never gets a team. Saying so as a
-- constraint means no later query has to defend against a row that claims both.
alter table public.picks drop constraint if exists picks_jackpot_has_no_team;
alter table public.picks add constraint picks_jackpot_has_no_team
  check (not jackpot or (by_slots and pick is null)) not valid;

-- 4. THE PULL --------------------------------------------------------------

-- The meter climbs when a slots pick is SAVED, not when it resolves. That is
-- the lever pull the league watches all week, and it has to happen server-side:
-- this repo is public and the key is anon, so a counter the browser could
-- increment is a counter anybody can run to 10%.
create or replace function public.slots_jackpot_pull()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
begin
  -- ONE ROW, ONE PULL, and this is not fussiness. A pick can be flipped between
  -- a team and the slots as often as somebody likes, and counting every flip
  -- back meant one person could walk the whole league's meter to the ceiling on
  -- their own. Twelve flips used to read as thirteen pulls.
  --
  -- BEFORE rather than AFTER, so the flag can be written onto the row that is
  -- being saved instead of chasing it with a second UPDATE.
  if tg_op = 'INSERT' then
    -- Never trust an inserted value: a client setting it true would skip its own
    -- pull, and one setting it false is the exploit above.
    new.slots_pull_counted := false;
  else
    -- On an update it can only ever go from false to true, whatever the client
    -- sent, so a pull cannot be handed back and spent again.
    new.slots_pull_counted := coalesce(old.slots_pull_counted, false);
  end if;

  if new.by_slots and new.pick is null and not new.slots_pull_counted then
    new.slots_pull_counted := true;
    update public.slots_jackpot
       set pulls_since_pop = pulls_since_pop + 1
     where id = 1;
  end if;

  return new;
end;
$$;

drop trigger if exists trg_slots_jackpot_pull on public.picks;
create trigger trg_slots_jackpot_pull
  before insert or update on public.picks
  for each row execute function public.slots_jackpot_pull();

-- 5. THE ROLL --------------------------------------------------------------

-- resolve_slot_picks() already flipped the coin, was already security definer,
-- and was already one-way. Two things change.
--
-- IT RESOLVES ROW BY ROW NOW, and that is the load-bearing part. It used to be
-- a single `update ... from`, and random() is evaluated per row inside one - so
-- a jackpot rolled in that statement would let four people pop the same meter
-- in the same instant. That is the "everybody wins" failure, and it would have
-- happened the first busy Saturday. A handful of rows a week makes the loop
-- free, and `for update` on the meter serializes concurrent callers so the
-- race is settled by Postgres rather than by luck.
--
-- THE ONE-WAY GUARD MOVED from `pick is null` to `slots_resolved_at is null`. It
-- had to: a popped pick keeps `pick` null forever, so the old guard would have
-- re-rolled it every minute for the rest of the season.
--
-- ORDER decides who gets first crack at a jackpot, so it is stated rather than
-- left to the planner: kickoff time, then the order the picks were saved. The
-- earliest lever pull goes first, which is the fair reading of a queue and
-- quietly rewards picking on Tuesday instead of at 11:58 on Saturday.
create or replace function public.resolve_slot_picks()
returns integer
language plpgsql
security definer
set search_path = public
as $$
declare
  r        record;
  n        integer := 0;
  pulls    integer;
  popped   boolean;
  paid_out boolean := false;
begin
  for r in
    select p.id, p.team_id, g."Away" as away, g."Home" as home
      from public.picks p
      join public.all_games g on g."GameId" = p.game_id
     where p.by_slots
       and p.slots_resolved_at is null
       and not public.picks_open_for_game(p.game_id)
     order by g."Start (CT)" nulls last, p.id
  loop
    -- Held for the rest of the transaction, so a second caller racing this one
    -- waits here rather than reading a meter that is about to change.
    select pulls_since_pop into pulls
      from public.slots_jackpot where id = 1 for update;

    -- AT MOST ONE PER PASS, structurally. Resetting the meter makes a second
    -- pop in the same batch unlikely - a quarter of a percent - but unlikely is
    -- not the promise. The promise is that a jackpot is one person, and eight
    -- picks resolving together must not be able to pay out eight times.
    popped := (not paid_out) and random() < public.slots_jackpot_odds(pulls);

    update public.picks p
       set jackpot = popped,
           pick = case
                    when popped then null
                    when random() < 0.5 then r.away
                    else r.home
                  end,
           slots_resolved_at = now()
     where p.id = r.id
       and p.slots_resolved_at is null;

    if found then
      n := n + 1;
      if popped then
        paid_out := true;
        update public.slots_jackpot
           set pulls_since_pop  = 0,
               last_pop_at      = now(),
               last_pop_team_id = r.team_id
         where id = 1;
      end if;
    end if;
  end loop;

  return n;
end;
$$;

comment on function public.resolve_slot_picks() is
  'Resolve every slots pick whose game has locked: roll the jackpot, else flip the coin. At most one pop per pass, claimed atomically. Idempotent and one-way.';

revoke execute on function public.resolve_slot_picks() from public;
revoke execute on function public.resolve_slot_picks() from anon;
grant  execute on function public.resolve_slot_picks() to authenticated;

-- 6. THE INDEX THE RESOLVER RUNS AGAINST -----------------------------------

-- Rebuilt on the new guard. The old one was `where by_slots and pick is null`,
-- which after this change would hold every popped pick forever instead of only
-- the handful actually pending.
drop index if exists public.picks_unresolved_slots;
create index if not exists picks_unresolved_slots
  on public.picks (game_id) where by_slots and slots_resolved_at is null;

-- 7. KEEPING THE FLAG HONEST -----------------------------------------------

-- Nothing above stops a member writing jackpot = true onto their own pick. The
-- existing picks_slots_start_empty_* policies guard by_slots the same way; this
-- is the same three lines for the column that is actually worth points.
--
-- RESTRICTIVE, matching its neighbours: Postgres ANDs it with the permissive
-- policies, so it can only tighten. resolve_slot_picks() is security definer
-- and bypasses RLS, so it is unaffected.
drop policy if exists picks_jackpot_is_the_houses_ins on public.picks;
create policy picks_jackpot_is_the_houses_ins on public.picks
  as restrictive for insert to authenticated
  with check (not jackpot or public.is_commissioner());

drop policy if exists picks_jackpot_is_the_houses_upd on public.picks;
create policy picks_jackpot_is_the_houses_upd on public.picks
  as restrictive for update to authenticated
  using      (public.is_commissioner() or not jackpot)
  with check (public.is_commissioner() or not jackpot);

-- slots_pull_counted needs no policy of its own: the BEFORE trigger overwrites
-- whatever a client sends with the row's own history, so it cannot be set or
-- cleared from outside no matter what the policies say.
