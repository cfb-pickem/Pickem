-- Ramp the regular-season jackpot by weeks left, so an unhit football goes.
--
-- Charlie, 2026-11-22: it hasn't hit all year, and it should be about a coin
-- flip in the second-to-last jackpot week and about 75% in the last one. Built
-- into the logic, not a one-off for this season.
--
-- THE RULE. Before the season's one pop, each week gets a hit-chance target
-- from how many jackpot weeks are left, counting this one:
--
--     week_target = 1 - 0.25 ^ (1 / weeks_left)
--
--     weeks_left   1     2     3     4     6     10    15
--     target      75%   50%   37%   29%   21%   13%    9%
--
-- Read it as: at this week's pace, an unhit football has a 75% chance of going
-- before the season is out. The last week is 75%, the one before is a coin
-- flip, and early weeks are low. That target is turned into per-pull odds
-- using the pulls the league actually makes:
--
--     per_pull = 1 - 0.25 ^ (1 / (weeks_left * pulls_per_week))
--
-- so pulls_per_week pulls at per_pull odds land on week_target.
--
-- PULLS PER WEEK. Slots picks in this season's previous four weeks, divided by
-- four, with a floor of 8 so a slow start (weeks 0-2 of 2026 had 2 slots picks
-- between them) can't inflate early-season odds. Each slots pick is one roll at
-- lock and one row per team and game, so this counts every pull once. The
-- current week is excluded, so every pick in a week gets the same odds.
--
-- THE LAST JACKPOT WEEK. Conference championship week: the week that holds the
-- first Saturday of December, numbered by calc_cfb_week() (week 1 = Labor Day
-- Saturday), capped at the regular season's last week, 15. That is week 15 in
-- 2025 (Dec 6) but week 14 in 2026 (Dec 5), because Labor Day fell late. Any
-- regular-season week after it counts as the last week.
--
-- WHAT IT REPLACES. The min(1/pulls_remaining, 5%) floor (20261113160000). The
-- organic meter ramp, slots_jackpot_odds(), is still the other side of the
-- greatest(), so a long drought is never undercut. Post-pop regular season is
-- still 0 and playoffs are still organic only. resolve_slot_picks(), payouts
-- and the pop-then-reset are untouched, and resolved picks never re-roll, so
-- weeks already played don't change. p_pick_id is no longer read but stays in
-- the signature the resolver calls.

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
  dec1            date;
  final_week      integer;
  weeks_left      integer;
  pulls_per_week  numeric;
  survive         constant numeric := 0.25;  -- 1 - the last week's 75%
  min_per_week    constant numeric := 8;
begin
  if p_cfb_season is not null and coalesce(p_week, 99) <= 15 then
    if p_last_pop_season is distinct from p_cfb_season then
      -- Regular-season pull in a season that has not popped: weeks-left ramp
      -- against the organic meter ramp.
      dec1 := make_date(p_cfb_season, 12, 1);
      final_week := least(15, public.calc_cfb_week(
        (dec1 + (6 - extract(dow from dec1)::int + 7) % 7)::timestamp));
      weeks_left := greatest(final_week - p_week + 1, 1);

      select greatest(count(*) / 4.0, min_per_week)
        into pulls_per_week
        from public.picks p2
        join public.all_games g2 on g2."GameId" = p2.game_id
       where p2.by_slots
         and g2.cfb_season = p_cfb_season
         and g2.week between p_week - 4 and p_week - 1;

      return greatest(
        1 - power(survive, 1.0 / (weeks_left * pulls_per_week)),
        public.slots_jackpot_odds(p_pulls_since_pop)
      );
    else
      -- Regular-season pull in a season that has already popped: zero.
      return 0;
    end if;
  else
    -- Playoffs, or a null season: organic ramp only.
    return public.slots_jackpot_odds(p_pulls_since_pop);
  end if;
end;
$$;

comment on function public.slots_jackpot_odds_for(bigint, integer, integer, integer, integer) is
  'Final jackpot odds for a specific pending slots pick. Pre-pop regular season: greatest(1 - 0.25^(1/(weeks_left * pulls_per_week)), organic ramp), so the week hit chance is 1 - 0.25^(1/weeks_left): 50% in the second-to-last jackpot week, 75% in the last (conference championship week). Post-pop regular season returns 0; playoffs return the organic ramp.';
