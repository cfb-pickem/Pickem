-- Cap the regular season at exactly one pop.
--
-- The extract migration (20261113140000) preserved the shape of the odds
-- expression byte for byte - including a leak. In the branch where the
-- meter has already popped this season (last_pop_cfb_season = cfb_season)
-- the helper fell through to slots_jackpot_odds(pulls). After a pop the
-- meter is reset to pulls_since_pop = 0, which makes that number 0.0005
-- per pull. Across the ~14 remaining regular-season pulls, Pr(a second
-- pop) is about 0.7% per season - small, but not zero, and over enough
-- seasons the invariant "at most one pop per regular season" would fail.
-- The pgTAP test in supabase/tests/03_at_most_one_per_season.sql caught
-- it in a 50-season sweep.
--
-- THE FIX. Add a post-pop regular-season branch that returns exactly 0.
-- The season has already paid out; any further probability is a bug.
-- Playoff pulls (week >= 16) are untouched and still get the organic
-- ramp: the one-pop cap is a regular-season promise, not a playoff one.
-- Pre-pop regular-season pulls are untouched and still get the ramp
-- with the 1/pulls_remaining floor.
--
-- resolve_slot_picks() does not change in this migration - the helper
-- returning 0 is enough to make random() < 0 false, which already
-- short-circuits via the `(not paid_out) and random() < final_odds`
-- expression.

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
begin
  if p_cfb_season is not null and coalesce(p_week, 99) <= 15 then
    if p_last_pop_season is distinct from p_cfb_season then
      -- Regular-season pull in a season that has not popped: ramp with
      -- the 1/pulls_remaining floor.
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
        1.0 / greatest(pulls_remaining, 1),
        public.slots_jackpot_odds(p_pulls_since_pop)
      );
    else
      -- Regular-season pull in a season that has already popped: zero.
      -- The season's one pop has landed; another would break the cap.
      return 0;
    end if;
  else
    -- Playoffs, or a null season: organic ramp only.
    return public.slots_jackpot_odds(p_pulls_since_pop);
  end if;
end;
$$;

comment on function public.slots_jackpot_odds_for(bigint, integer, integer, integer, integer) is
  'Final jackpot odds for a specific pending slots pick. Pre-pop regular season gets the 1/pulls_remaining floor climbing to 1.0 on the last pull; post-pop regular season gets 0 (the season has already paid out); playoffs get the organic ramp.';
