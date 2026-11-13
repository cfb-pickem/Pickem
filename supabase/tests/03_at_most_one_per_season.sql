-- T1. Simulate many regular-season pull sequences end-to-end and assert that
-- no season exceeds one pop, league-wide. Uses setseed() so the run is
-- deterministic.
--
-- WHY MULTI-PASS. The resolver's `paid_out` guard already prevents a second
-- pop within a single pass. The invariant we care about is ACROSS PASSES:
-- week after week, as games lock and the resolver is called again, a pop in
-- week 3 must still be the only pop a season sees. So this test locks games
-- one at a time and calls resolve_slot_picks() between each, matching the
-- real cadence of games locking as kickoff passes.
--
-- SHAPE OF ONE SIMULATED SEASON. 14 slots picks on 14 regular-season games
-- in weeks 1..14 of a given cfb_season. All games start line_locked = false
-- so the resolver can't see them yet. Weeks lock one at a time; each lock is
-- followed by a resolver call. Across N seasons we assert that no season has
-- more than one jackpot pop.

begin;

-- 50 seeded seasons, 14 picks each.
with seasons as (
  select generate_series(2100, 2149) as s
), games as (
  insert into public.all_games
         ("GameId", "Away", "Home", "Start (CT)", cfb_season, week, picked, line_locked)
  select  s*100 + w,
          'A', 'B',
          timestamptz '2100-01-01' + (s*100 + w) * interval '1 hour',
          s, w, true, false
    from seasons, generate_series(1, 14) as w
  returning "GameId", cfb_season
)
insert into public.picks (team_id, game_id, by_slots)
select 1, "GameId", true from games;

-- Pin randomness so the test is reproducible across runs.
select setseed(0.4242);

select plan(3);

-- For each season, lock games week by week and call the resolver after each
-- lock. Reset the meter between seasons so each is an independent trial.
do $$
declare
  s integer;
  w integer;
begin
  for s in 2100..2149 loop
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

-- (a) Every season has at most one jackpot pop.
select is(
  (select max(c)::integer from (
     select count(*) filter (where jackpot) as c
       from public.picks p
       join public.all_games g on g."GameId" = p.game_id
      group by g.cfb_season
   ) t),
  1,
  'max pops across 50 seasons is 1'
);

-- (b) At least one season actually popped (the ramp is doing something,
--     not just structurally refusing to fire).
select cmp_ok(
  (select count(*) filter (where jackpot) from public.picks),
  '>',
  0::bigint,
  'at least one pop fired in the simulation'
);

-- (c) No two pops share a cfb_season.
select is(
  (select count(*) from (
     select g.cfb_season
       from public.picks p
       join public.all_games g on g."GameId" = p.game_id
      where p.jackpot
      group by g.cfb_season
     having count(*) > 1
   ) t),
  0::bigint,
  'no cfb_season has more than one jackpot pop'
);

select * from finish();
rollback;
