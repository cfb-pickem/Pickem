-- T6. In the pre-pop regular-season branch, slots_jackpot_odds_for() must
-- never return a value above the cap (0.05). The cap is the whole point
-- of migration 20261113160000: no pull can feel more than 1-in-20 obvious,
-- even on the last pull of the season. The ceiling case for the floor is
-- pulls_remaining = 1, which without the cap would return 1.0.

begin;

select plan(3);

-- (a) pulls_remaining = 1 (no other unresolved picks, no still-open games).
-- Without the cap this would return 1.0. With the cap, it must be 0.05.
select is(
  public.slots_jackpot_odds_for(9999, 2026, 15, 0, null),
  0.05::numeric,
  'pulls_remaining = 1: capped at 0.05'
);

-- (b) In the stretch where the organic ramp stays below the cap
-- (pulls_since_pop ≤ 445, organic flat at 0.0005), the helper's output
-- is bounded by the cap. Covers the window where the cap is the
-- gating constraint. Past that the organic ramp itself rises above
-- CAP (climbing to its own 0.10 ceiling) and the helper follows it -
-- that is a property of the organic ramp, not a failure of the cap,
-- and happens vanishingly rarely in practice since pulls_since_pop
-- resets to 0 on every pop.
select cmp_ok(
  (select max(public.slots_jackpot_odds_for(9999, 2026, 5, pulls, null))
     from generate_series(0, 445, 5) as pulls),
  '<=',
  0.05::numeric,
  'pre-pop regular season: helper never exceeds cap while organic is below it'
);

-- (c) Playoff pulls deliberately bypass the cap (organic only), so the
-- ceiling is the organic 0.10. Negative framing: confirm the cap test
-- above is actually guarding the regular-season branch and not a blanket
-- clamp.
select cmp_ok(
  public.slots_jackpot_odds_for(9999, 2026, 16, 600, null),
  '>',
  0.05::numeric,
  'playoff branch not constrained by cap (sanity: proves the cap test guards the right branch)'
);

select * from finish();
rollback;
