-- T3. Once the meter has popped this season, slots_jackpot_odds_for() must
-- return zero for regular-season pulls - the season has already paid out,
-- so any further pop would break the "at most one per regular season"
-- invariant. Not slots_jackpot_odds(pulls), which is the pre-reset organic
-- ramp but still leaks ~0.05% per subsequent pull; not the 1/pulls_remaining
-- floor, which was already gated out. Exactly zero.

begin;

-- SEED: picks and games in season 2026. If the post-pop branch returned the
-- organic ramp (what the first-draft extract migration does) the test would
-- see 0.0005 rather than 0 and fail. If it returned the floor, it would see
-- a much larger number and fail even harder.
insert into public.all_games ("GameId", "Away", "Home", cfb_season, week, picked, line_locked)
select g, 'A', 'B', 2026, 1 + (g % 15), true, false
  from generate_series(1, 20) as g;

insert into public.picks (team_id, game_id, by_slots)
select 1, g, true
  from generate_series(1, 20) as g;

select plan(5);

-- (a) pulls = 0, same season as last pop: 0.
select is(
  public.slots_jackpot_odds_for(9999, 2026, 5, 0, 2026),
  0::numeric,
  'same-season post-pop, pulls=0: zero (season already paid out)'
);

-- (b) pulls = 300, same season: 0.
select is(
  public.slots_jackpot_odds_for(9999, 2026, 10, 300, 2026),
  0::numeric,
  'same-season post-pop, pulls=300: zero'
);

-- (c) pulls = 460, same season: 0 (even though the organic ramp would be
--     ~0.038, a second pop still breaks the invariant).
select is(
  public.slots_jackpot_odds_for(9999, 2026, 1, 460, 2026),
  0::numeric,
  'same-season post-pop, pulls deep in ramp: zero'
);

-- (d) pulls = 600, same season: 0 (not the 0.10 ceiling).
select is(
  public.slots_jackpot_odds_for(9999, 2026, 15, 600, 2026),
  0::numeric,
  'same-season post-pop, pulls past ceiling: zero'
);

-- (e) Different season (last_pop_season != cfb_season): floor fires, so
--     the number is strictly > organic. Proves the gate - not some blanket
--     zero - is what suppresses the ramp above.
select cmp_ok(
  public.slots_jackpot_odds_for(9999, 2026, 5, 0, 2025),
  '>',
  public.slots_jackpot_odds(0),
  'different season: floor > organic (confirms the gate is the thing suppressing it)'
);

select * from finish();
rollback;
