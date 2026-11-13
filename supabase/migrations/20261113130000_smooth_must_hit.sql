-- Replace the last-pull force with a smooth ramp to certainty.
--
-- The previous migration (20261113120000) guaranteed the jackpot by forcing
-- `popped := true` on the final regular-season slot-pull if nothing had fired.
-- That works but reads as a special case: a hard "the house pays here" branch
-- sitting next to the organic ramp. This replaces it with ONE probability
-- expression that collapses to certainty on its own when it gets to the last
-- pull, so the resolver only ever does the same thing.
--
-- THE SHAPE. For a regular-season pull where nothing has popped this season:
--
--     pulls_remaining = this pull
--                     + unresolved by_slots picks in weeks 0..15 this season
--                     + regular-season games still open for picks this season
--     p               = 1 / pulls_remaining
--     final_odds      = max(p, slots_jackpot_odds(pulls_since_pop))
--
-- Expected value is one hit by season end: p starts tiny (hundreds of pulls
-- remaining) and climbs each time a pick resolves or a week locks, and on the
-- very last pull it is 1 / 1 = 1.0. The organic ramp stays as a floor, so a
-- long drought cannot be undercut by a season-wide denominator.
--
-- PULLS REMAINING. Two counts added together, per Charlie's spec:
--   1. Other by_slots picks already in the picks table that have not resolved.
--   2. Regular-season games whose pick window is still open (picks_open_for_game),
--      which is the ceiling on pulls that could still be submitted.
-- Over-counts on a game that already has a slots pick and could get more, but
-- the ramp floor absorbs that - final_odds is never worse than the organic
-- curve - and on the last pull both counts go to zero so certainty still lands.
--
-- WHAT IS REUSED. Regular season is still week <= 15 (picks_open_for_game(),
-- 20260822020000). "Popped this season" is still slots_jackpot.last_pop_cfb_season
-- (20261113120000). The organic ramp (slots_jackpot_odds, 20260914010000) is
-- unchanged. The FOR UPDATE lock and paid_out guard are unchanged. Only the
-- expression that chooses `popped` moves.

create or replace function public.resolve_slot_picks()
returns integer
language plpgsql
security definer
set search_path = public
as $$
declare
  r                record;
  n                integer := 0;
  pulls            integer;
  last_season      integer;
  pulls_remaining  integer;
  final_odds       numeric;
  popped           boolean;
  paid_out         boolean := false;
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
    -- Held for the rest of the transaction, so a second caller racing this
    -- one waits here rather than reading a meter that is about to change.
    select pulls_since_pop, last_pop_cfb_season
      into pulls, last_season
      from public.slots_jackpot where id = 1 for update;

    if r.cfb_season is not null
       and coalesce(r.week, 99) <= 15
       and (last_season is distinct from r.cfb_season)
    then
      -- Regular-season pull in a season that has not paid out. Climb to
      -- certainty as the denominator shrinks - a smoother must-hit than a
      -- last-pull force, and it collapses to 1.0 on its own.
      pulls_remaining := 1
        + (select count(*)::integer
             from public.picks p2
             join public.all_games g2 on g2."GameId" = p2.game_id
            where p2.by_slots
              and p2.slots_resolved_at is null
              and p2.id <> r.id
              and g2.cfb_season = r.cfb_season
              and coalesce(g2.week, 99) <= 15)
        + (select count(*)::integer
             from public.all_games g3
            where g3.cfb_season = r.cfb_season
              and coalesce(g3.week, 99) <= 15
              and coalesce(g3.picked, false)
              and public.picks_open_for_game(g3."GameId"));

      final_odds := greatest(
        1.0 / greatest(pulls_remaining, 1),
        public.slots_jackpot_odds(pulls)
      );
    else
      -- Playoffs, or a season that has already popped: organic ramp only.
      final_odds := public.slots_jackpot_odds(pulls);
    end if;

    -- AT MOST ONE PER PASS, structurally. Resetting the meter makes a second
    -- pop in the same batch unlikely, but unlikely is not the promise. The
    -- promise is that a jackpot is one person, and eight picks resolving
    -- together must not be able to pay out eight times.
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

comment on function public.resolve_slot_picks() is
  'Resolve every slots pick whose game has locked: roll the jackpot, else flip the coin. Per-pull odds are max(1/pulls_remaining, organic ramp) until the season pops - expected value one hit per regular season, collapsing to 1.0 on the last pull. At most one pop per pass, claimed atomically. Idempotent and one-way.';

revoke execute on function public.resolve_slot_picks() from public;
revoke execute on function public.resolve_slot_picks() from anon;
grant  execute on function public.resolve_slot_picks() to authenticated;
