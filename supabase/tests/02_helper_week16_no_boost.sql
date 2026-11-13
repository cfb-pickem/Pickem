-- T4. Playoff pulls (week >= 16) must never get the 1/pulls_remaining floor,
-- regardless of whether the meter has popped this season. The gate is
-- `coalesce(p_week, 99) <= 15`: anything over 15 falls into the else branch.
--
-- The invariant protects the "once per REGULAR season" promise - a playoff
-- pull in an unpopped season must not force certainty, because the regular
-- season is already over.

begin;

-- Noise rows: picks and games in playoff weeks 16/17/18. If any of these
-- counted toward pulls_remaining the floor would be positive for a playoff
-- pull in an unpopped season.
insert into public.all_games ("GameId", "Away", "Home", cfb_season, week, picked, line_locked)
select g, 'A', 'B', 2026, 16 + (g % 3), true, false
  from generate_series(1, 20) as g;

insert into public.picks (team_id, game_id, by_slots)
select 1, g, true
  from generate_series(1, 20) as g;

select plan(6);

-- (a) week 16, pre-pop (last_pop_season differs): no floor.
select is(
  public.slots_jackpot_odds_for(9999, 2026, 16, 0, null),
  public.slots_jackpot_odds(0),
  'week 16, unpopped season: pure organic (no floor)'
);

-- (b) week 16, post-pop (same season): no floor.
select is(
  public.slots_jackpot_odds_for(9999, 2026, 16, 300, 2026),
  public.slots_jackpot_odds(300),
  'week 16, popped this season: pure organic'
);

-- (c) week 17 edge: still playoffs, still no floor.
select is(
  public.slots_jackpot_odds_for(9999, 2026, 17, 0, 2024),
  public.slots_jackpot_odds(0),
  'week 17, unpopped season: pure organic'
);

-- (d) week 18 (CFP final): no floor.
select is(
  public.slots_jackpot_odds_for(9999, 2026, 18, 600, null),
  public.slots_jackpot_odds(600),
  'week 18, deep in ramp: pure organic'
);

-- (e) null week falls back to 99 via coalesce: no floor.
select is(
  public.slots_jackpot_odds_for(9999, 2026, null, 0, null),
  public.slots_jackpot_odds(0),
  'null week (coalesce 99): pure organic'
);

-- (f) null season: no floor, since the gate also requires a non-null season.
select is(
  public.slots_jackpot_odds_for(9999, null, 5, 0, null),
  public.slots_jackpot_odds(0),
  'null cfb_season: pure organic'
);

select * from finish();
rollback;
