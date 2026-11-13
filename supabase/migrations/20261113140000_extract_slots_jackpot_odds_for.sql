-- Lift the final-odds expression out of resolve_slot_picks() into a function.
--
-- WHY. The smooth ramp (20261113130000) is the one thing we need tests on: at
-- most one pop per regular season, no boost after a pop, no boost on playoff
-- pulls. All three are properties of the odds expression alone, but today that
-- expression lives inside a plpgsql loop that also mutates picks and the
-- jackpot meter. Asserting on it required thousands of trials and still only
-- inferred the property. Pulling the expression out as its own function lets a
-- test call it directly and read the number back.
--
-- NO BEHAVIOR CHANGE. The SQL in slots_jackpot_odds_for() is byte-identical to
-- the branch that previously sat inline: same gate (cfb_season set, week <= 15,
-- last_pop_cfb_season is distinct from cfb_season), same two counts added to
-- the 1-for-this-pull, same greatest(1/denominator, organic). The resolver now
-- calls the helper instead of computing it in place; the FOR UPDATE lock, the
-- paid_out guard, the pick update, and the jackpot reset are all unchanged.

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
  if p_cfb_season is not null
     and coalesce(p_week, 99) <= 15
     and (p_last_pop_season is distinct from p_cfb_season)
  then
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
    return public.slots_jackpot_odds(p_pulls_since_pop);
  end if;
end;
$$;

comment on function public.slots_jackpot_odds_for(bigint, integer, integer, integer, integer) is
  'Final jackpot odds for a specific pending slots pick. Regular-season pulls in a season that has not popped get a 1/pulls_remaining floor that climbs to 1.0 on the last pull; playoff pulls and post-pop pulls get the organic ramp only. Extracted from resolve_slot_picks() so tests can assert on the odds directly.';

revoke execute on function public.slots_jackpot_odds_for(bigint, integer, integer, integer, integer) from public;
revoke execute on function public.slots_jackpot_odds_for(bigint, integer, integer, integer, integer) from anon;
grant  execute on function public.slots_jackpot_odds_for(bigint, integer, integer, integer, integer) to authenticated;

-- THE RESOLVER. Same shape as 20261113130000, but the odds expression is now a
-- single call to slots_jackpot_odds_for() with the current pick's row and the
-- jackpot meter it just locked.
create or replace function public.resolve_slot_picks()
returns integer
language plpgsql
security definer
set search_path = public
as $$
declare
  r             record;
  n             integer := 0;
  pulls         integer;
  last_season   integer;
  final_odds    numeric;
  popped        boolean;
  paid_out      boolean := false;
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
    select pulls_since_pop, last_pop_cfb_season
      into pulls, last_season
      from public.slots_jackpot where id = 1 for update;

    final_odds := public.slots_jackpot_odds_for(
      r.id, r.cfb_season, r.week, pulls, last_season
    );

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
  'Resolve every slots pick whose game has locked: roll the jackpot, else flip the coin. Per-pull odds come from slots_jackpot_odds_for(). At most one pop per pass, claimed atomically. Idempotent and one-way.';

revoke execute on function public.resolve_slot_picks() from public;
revoke execute on function public.resolve_slot_picks() from anon;
grant  execute on function public.resolve_slot_picks() to authenticated;
