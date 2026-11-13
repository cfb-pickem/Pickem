-- Guarantee at least one league-wide auto win per CFB regular season.
--
-- The ramp (see 20260914010000) is tuned so ~420 pulls a season averages one
-- pop. The league is averaging about 13 pulls a season, which lives deep in
-- the quiet stretch where per-pull odds are flat at 0.05% - in practice the
-- ball has never popped since the mechanic shipped. This forces the final
-- slot-pull opportunity of the regular season to pop if nothing has popped
-- this season, so there is always one.
--
-- WHERE REGULAR SEASON STOPS. picks_open_for_game() already draws the line at
-- week 15 (20260822020000): weeks 0..15 are regular season, 16+ are playoffs.
-- The must-hit reuses that line - no new constant, no new table.
--
-- HOW "FINAL" IS DECIDED. Not a date. The final pull is whichever slot-pull
-- the resolver reaches AFTER which no further regular-season pull can happen
-- this season: no other unresolved slots pick in weeks 0..15 for this
-- cfb_season, AND no regular-season game whose pick window is still open (so
-- nobody can submit a new slots pick either). If both are true and nothing
-- has popped this season, this pull is forced.
--
-- HOW "POPPED THIS SEASON" IS TRACKED. New column slots_jackpot.last_pop_cfb_season,
-- written in the same UPDATE that resets the meter. NULL means either the
-- meter has never popped or the pop predates this migration; the backfill
-- below catches the latter by reading the cfb_season off the last jackpot
-- pick.
--
-- RACE. The loop still takes `for update` on the single slots_jackpot row and
-- the local `paid_out` still prevents a second pop in the same pass. The
-- forced pop is just another code path arriving at `popped := true`.

-- 1. SCHEMA ----------------------------------------------------------------

alter table public.slots_jackpot
  add column if not exists last_pop_cfb_season integer;

comment on column public.slots_jackpot.last_pop_cfb_season is
  'The cfb_season the meter last popped in. Written in the same UPDATE that resets pulls_since_pop. NULL until a pop has fired under this schema. The must-hit in resolve_slot_picks() uses this to decide whether to force a pop on the final regular-season pull.';

-- Backfill from existing jackpot rows if the meter has already popped under
-- the old schema. Reads the cfb_season off the most recently resolved jackpot
-- pick; a NULL result leaves the column NULL and the must-hit will fire once
-- this season, which matches "we do not know when the last pop was, so assume
-- we still owe one".
update public.slots_jackpot
   set last_pop_cfb_season = (
     select g.cfb_season
       from public.picks p
       join public.all_games g on g."GameId" = p.game_id
      where p.jackpot
      order by p.slots_resolved_at desc nulls last
      limit 1
   )
 where id = 1
   and last_pop_cfb_season is null
   and last_pop_at is not null;

-- 2. THE ROLL --------------------------------------------------------------

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
  popped        boolean;
  must_hit      boolean;
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
    -- Held for the rest of the transaction, so a second caller racing this one
    -- waits here rather than reading a meter that is about to change.
    select pulls_since_pop, last_pop_cfb_season
      into pulls, last_season
      from public.slots_jackpot where id = 1 for update;

    -- MUST-HIT GATE. Regular-season pull (week <= 15), no pop recorded for
    -- this cfb_season, no pop already this pass, no other unresolved slots
    -- pick in this season's regular season, and no regular-season game in
    -- this season whose pick window is still open. That is the final chance,
    -- and the house pays here if the ramp never got round to it.
    must_hit := (not paid_out)
      and r.cfb_season is not null
      and coalesce(r.week, 99) <= 15
      and (last_season is distinct from r.cfb_season)
      and not exists (
        select 1
          from public.picks p2
          join public.all_games g2 on g2."GameId" = p2.game_id
         where p2.by_slots
           and p2.slots_resolved_at is null
           and p2.id <> r.id
           and g2.cfb_season = r.cfb_season
           and coalesce(g2.week, 99) <= 15
      )
      and not exists (
        select 1
          from public.all_games g3
         where g3.cfb_season = r.cfb_season
           and coalesce(g3.week, 99) <= 15
           and coalesce(g3.picked, false)
           and public.picks_open_for_game(g3."GameId")
      );

    -- AT MOST ONE PER PASS, structurally. Resetting the meter makes a second
    -- pop in the same batch unlikely - a quarter of a percent - but unlikely
    -- is not the promise. The promise is that a jackpot is one person, and
    -- eight picks resolving together must not be able to pay out eight times.
    popped := must_hit
           or ((not paid_out) and random() < public.slots_jackpot_odds(pulls));

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
  'Resolve every slots pick whose game has locked: roll the jackpot, else flip the coin. The final regular-season slot-pull is forced to pop if no league-wide pop has fired this cfb_season. At most one pop per pass, claimed atomically. Idempotent and one-way.';

revoke execute on function public.resolve_slot_picks() from public;
revoke execute on function public.resolve_slot_picks() from anon;
grant  execute on function public.resolve_slot_picks() to authenticated;
