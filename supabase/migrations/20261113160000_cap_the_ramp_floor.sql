-- Cap the 1/pulls_remaining floor so late pulls feel organic.
--
-- The smooth ramp pins per-pull odds at 1/N as the season winds down, which
-- in a short season lands at 50%, 100% on the very last pull. That reads as
-- "the house is clearly steering this one" - the opposite of a slot. The
-- fix is a hard cap: the floor can climb, but not above CAP.
--
--     final_odds = max( min(1 / pulls_remaining, CAP), slots_jackpot_odds(pulls) )
--
-- PICKING THE CAP. Measured from live data (vopdioszofwdkwnujtiq, picks
-- table joined with all_games): 82 slots pulls in the first ten regular-
-- season weeks of 2026, projecting to ~123 pulls for the full regular
-- season. Monte-Carlo over 100k seasons at that rate:
--
--     CAP   Pr(0 pops)   Pr(1 pop)   max odds
--     0.03   9.8%         90.2%       0.030
--     0.04   7.3%         92.7%       0.040
--     0.05   5.8%         94.2%       0.050
--     0.10   2.8%         97.2%       0.100
--     0.20   1.3%         98.7%       0.200
--
-- CAP = 0.05 sits in the middle of the "5-10% no-pop" window Charlie asked
-- for: a 1-in-20 ceiling for the last few pulls, roughly one in 17 seasons
-- goes without a hit. The strict mathematical guarantee of "at least one
-- hit per season" is gone - replaced with "almost always one hit, never
-- visibly rigged".
--
-- WHAT STAYS. The post-pop branch still returns 0 (one pop per regular
-- season, hard). The week <= 15 gate is unchanged (playoff pulls still get
-- organic ramp only). The pulls_remaining count and the organic ramp are
-- unchanged.

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
      -- Regular-season pull in a season that has not popped: capped floor
      -- against the organic ramp. The cap keeps the last few pulls from
      -- feeling rigged.
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
  'Final jackpot odds for a specific pending slots pick. Pre-pop regular season gets min(1/pulls_remaining, 0.05) against the organic ramp; post-pop regular season returns 0; playoffs return the organic ramp. The 0.05 cap keeps the final pulls of the season from reading as rigged.';
