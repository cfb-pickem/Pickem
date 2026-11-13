-- T2. With organic odds floored to zero, the 1/pulls_remaining ramp must on
-- its own fire exactly one pop by the end of the regular season. This
-- exercises the "collapses to certainty" claim: on the final pull the
-- denominator is 1, so the probability is 1.0 regardless of the organic
-- ramp. Over the whole season the compounded probability of NO pop is
-- (1 - 1/14)(1 - 1/13)...(1 - 1/1) = 0, so at least one pop is guaranteed.
-- The paid_out + last_pop_cfb_season pair then caps it at exactly one.

begin;

-- Override slots_jackpot_odds to a constant zero for the duration of this
-- transaction. The rollback at the end restores the real function in the
-- database for later tests. slots_jackpot_odds_for() calls public.slots_jackpot_odds
-- by name and inherits this override via the function's declared search_path.
create or replace function public.slots_jackpot_odds(pulls integer)
returns numeric
language sql
immutable
as $$
  select 0::numeric;
$$;

-- 10 seeded seasons, 14 picks each. With organic zero, every season's one
-- pop has to come from the floor.
with seasons as (
  select generate_series(2200, 2209) as s
), games as (
  insert into public.all_games
         ("GameId", "Away", "Home", "Start (CT)", cfb_season, week, picked, line_locked)
  select  s*100 + w,
          'A', 'B',
          timestamptz '2200-01-01' + (s*100 + w) * interval '1 hour',
          s, w, true, false
    from seasons, generate_series(1, 14) as w
  returning "GameId", cfb_season
)
insert into public.picks (team_id, game_id, by_slots)
select 1, "GameId", true from games;

select setseed(0.8675);

select plan(2);

do $$
declare
  s integer;
  w integer;
begin
  for s in 2200..2209 loop
    update public.slots_jackpot
       set pulls_since_pop = 0, last_pop_at = null,
           last_pop_team_id = null, last_pop_cfb_season = null
     where id = 1;

    for w in 1..14 loop
      update public.all_games
         set line_locked = true
       where cfb_season = s and week = w;

      perform public.resolve_slot_picks();
    end loop;
  end loop;
end $$;

-- (a) Every season popped exactly once.
select is(
  (select array_agg(c order by cfb_season)::integer[] from (
     select g.cfb_season, count(*) filter (where p.jackpot) as c
       from public.picks p
       join public.all_games g on g."GameId" = p.game_id
      where g.cfb_season between 2200 and 2209
      group by g.cfb_season
   ) t),
  array[1,1,1,1,1,1,1,1,1,1]::integer[],
  'with organic = 0, each of the 10 seasons pops exactly once'
);

-- (b) Total pops = 10 (one per season, sanity check in aggregate).
select is(
  (select count(*) filter (where p.jackpot)
     from public.picks p
     join public.all_games g on g."GameId" = p.game_id
    where g.cfb_season between 2200 and 2209),
  10::bigint,
  'with organic = 0, total pops across 10 seasons is 10'
);

select * from finish();
rollback;
