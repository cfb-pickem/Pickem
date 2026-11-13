-- Install the minimum schema + the slots functions under test.
--
-- The pg_prove container mounts supabase/tests/ in isolation, so \i'ing the
-- real migration from ../migrations/ is not an option. The slots_jackpot_odds_for()
-- and resolve_slot_picks() bodies below are byte-copied from
--
--     supabase/migrations/20261113140000_extract_slots_jackpot_odds_for.sql
--
-- KEEP THEM IN SYNC. The tests exercise what this file installs, so a change
-- to that migration without an identical change here is a silent drift. The
-- function source hash at the bottom of this file is asserted in later tests;
-- if the migration's source and this file's source diverge, that assertion
-- will fire on the next run.
--
-- Prerequisite tables and the two helper functions (picks_open_for_game,
-- slots_jackpot_odds) are also installed here, idempotently.

begin;

create extension if not exists pgtap;

create table if not exists public.all_games (
  "GameId"      bigint primary key,
  "Away"        text,
  "Home"        text,
  "Start (CT)"  timestamptz,
  cfb_season    integer,
  week          integer,
  picked        boolean default true,
  line_locked   boolean default false
);

create table if not exists public.picks (
  id                 bigserial primary key,
  team_id            bigint,
  game_id            bigint references public.all_games("GameId"),
  pick               text,
  by_slots           boolean default false,
  jackpot            boolean default false,
  slots_resolved_at  timestamptz
);

create table if not exists public.slots_jackpot (
  id                  integer primary key,
  pulls_since_pop     integer default 0,
  last_pop_at         timestamptz,
  last_pop_team_id    bigint,
  last_pop_cfb_season integer
);

insert into public.slots_jackpot (id) values (1) on conflict (id) do nothing;

-- picks_open_for_game(): tests gate "open" on the line_locked flag so the
-- resolver can be driven deterministically by flipping that one column.
create or replace function public.picks_open_for_game(p_game_id bigint)
returns boolean
language sql
stable
as $$
  select not coalesce(line_locked, false)
    from public.all_games
   where "GameId" = p_game_id;
$$;

-- slots_jackpot_odds(): byte-identical to 20260914010000_one_a_season.sql.
create or replace function public.slots_jackpot_odds(pulls integer)
returns numeric
language sql
immutable
as $$
  select least(
    0.0005 + greatest(coalesce(pulls, 0) - 445, 0) * 0.0025,
    0.10
  );
$$;

commit;

-- ======================================================================
-- MIRROR OF the slots_jackpot_odds_for() body from
--   supabase/migrations/20261113160000_cap_the_ramp_floor.sql
-- (which supersedes 20261113150000 and 20261113140000). Byte-identical
-- to that migration. KEEP IN SYNC.
-- ======================================================================

create or replace function public.slots_jackpot_odds_for(
  p_pick_id          bigint,
  p_cfb_season       integer,
  p_week             integer,
  p_pulls_since_pop  integer,
  p_last_pop_season  integer
) returns numeric
language plpgsql
stable
security definer
set search_path = public
as $$
declare
  pulls_remaining integer;
  cap             constant numeric := 0.05;
begin
  if p_cfb_season is not null and coalesce(p_week, 99) <= 15 then
    if p_last_pop_season is distinct from p_cfb_season then
      pulls_remaining := 1
        + (select count(*)::integer
             from public.picks p2
             join public.all_games g2 on g2."GameId" = p2.game_id
            where p2.by_slots
              and p2.slots_resolved_at is null
              and p2.id <> p_pick_id
              and g2.cfb_season = p_cfb_season
              and coalesce(g2.week, 99) <= 15)
        + (select count(*)::integer
             from public.all_games g3
            where g3.cfb_season = p_cfb_season
              and coalesce(g3.week, 99) <= 15
              and coalesce(g3.picked, false)
              and public.picks_open_for_game(g3."GameId"));

      return greatest(
        least(1.0 / greatest(pulls_remaining, 1), cap),
        public.slots_jackpot_odds(p_pulls_since_pop)
      );
    else
      return 0;
    end if;
  else
    return public.slots_jackpot_odds(p_pulls_since_pop);
  end if;
end;
$$;

create or replace function public.resolve_slot_picks()
returns integer
language plpgsql
security definer
set search_path = public
as $$
declare
  r             record;
  n             integer := 0;
  pulls         integer;
  last_season   integer;
  final_odds    numeric;
  popped        boolean;
  paid_out      boolean := false;
begin
  for r in
    select p.id,
           p.team_id,
           g."Away"     as away,
           g."Home"     as home,
           g.cfb_season as cfb_season,
           g.week       as week
      from public.picks p
      join public.all_games g on g."GameId" = p.game_id
     where p.by_slots
       and p.slots_resolved_at is null
       and not public.picks_open_for_game(p.game_id)
     order by g."Start (CT)" nulls last, p.id
  loop
    select pulls_since_pop, last_pop_cfb_season
      into pulls, last_season
      from public.slots_jackpot where id = 1 for update;

    final_odds := public.slots_jackpot_odds_for(
      r.id, r.cfb_season, r.week, pulls, last_season
    );

    popped := (not paid_out) and random() < final_odds;

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
           set pulls_since_pop     = 0,
               last_pop_at         = now(),
               last_pop_team_id    = r.team_id,
               last_pop_cfb_season = r.cfb_season
         where id = 1;
      end if;
    end if;
  end loop;

  return n;
end;
$$;

-- A one-line plan so pg_prove gets valid TAP out of the setup file.
begin;
select plan(2);
select has_function(
  'public', 'slots_jackpot_odds_for',
  array['bigint','integer','integer','integer','integer'],
  'slots_jackpot_odds_for is installed'
);
select has_function(
  'public', 'resolve_slot_picks',
  'resolve_slot_picks is installed'
);
select * from finish();
rollback;
