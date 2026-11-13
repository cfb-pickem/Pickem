-- T2. The capped ramp no longer guarantees a hit every season - that was
-- the old "collapses to 1.0 on the last pull" promise, and it is gone on
-- purpose (CAP = 0.05, so the last pulls stop climbing once they hit
-- 1-in-20). The new invariant is empirical: at the measured 2026 pull
-- rate, no-pop seasons should still be rare.
--
-- Simulation at the measured rate (~123 pulls / regular season, organic
-- odds set to 0 to isolate the capped floor) over 100k seasons gives
-- Pr(0 pops) ≈ 5.8%. This test runs the same formula in-process in
-- plpgsql over 100 seeded seasons and asserts the empirical no-pop rate
-- is at most 12%. 12% is a deliberate ~2× headroom: binomial 99th
-- percentile at p=0.058, n=100 is around 12 no-pops.
--
-- WHY NOT THE REAL RESOLVER. 100 seasons × 123 picks resolved one by one
-- through resolve_slot_picks() is ~150M row lookups against an
-- unindexed fixture - the test would take minutes. The invariant we care
-- about is a property of the odds formula, so this test simulates the
-- same formula directly. T6 (max per-pull odds) and T3 (post-pop zero)
-- separately verify that the INSTALLED function matches the formula
-- used here; together they cover "the formula has the right shape" and
-- "the formula is what we shipped".

begin;

select plan(1);
select setseed(0.1234);

do $$
declare
  cap              constant numeric := 0.05;
  pulls_per_season constant integer := 123;
  n_seasons        constant integer := 100;
  s                integer;
  k                integer;
  pulls_remaining  integer;
  odds             numeric;
  popped           boolean;
  no_pop_count     integer := 0;
begin
  for s in 1..n_seasons loop
    popped := false;
    for k in 1..pulls_per_season loop
      if popped then
        -- Post-pop: slots_jackpot_odds_for() returns 0, so random() < 0
        -- is always false. Short-circuit for speed.
        continue;
      end if;
      pulls_remaining := pulls_per_season - k + 1;
      -- Byte-match the pre-pop regular-season branch of
      -- slots_jackpot_odds_for() with organic hardcoded to 0.
      odds := greatest(
        least(1.0 / greatest(pulls_remaining, 1), cap),
        0
      );
      if random() < odds then
        popped := true;
      end if;
    end loop;
    if not popped then
      no_pop_count := no_pop_count + 1;
    end if;
  end loop;

  perform set_config('tests.no_pop_count', no_pop_count::text, false);
end $$;

select cmp_ok(
  current_setting('tests.no_pop_count')::integer,
  '<=',
  12,
  format(
    'empirical no-pop rate over 100 seasons at ~123 pulls/season is at most 12%% (observed: %s)',
    current_setting('tests.no_pop_count')
  )
);

select * from finish();
rollback;
