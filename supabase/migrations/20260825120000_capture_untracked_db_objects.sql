-- Bring four functions and two triggers that only ever existed in the Supabase
-- dashboard under version control.
--
-- A drift check against the live database (every policy and function in the
-- public schema compared to these files) found all 19 RLS policies present and
-- correct, but four functions defined nowhere in this directory. The remote
-- supabase_migrations table is also empty - this schema was built by hand in the
-- SQL editor - so rebuilding from migrations alone would have produced a
-- database that looked right and silently scored nothing.
--
-- The definitions below are dumped verbatim from production (pg_get_functiondef
-- / pg_get_triggerdef), not rewritten, so applying this file is a no-op against
-- the current database.
--
-- The important one is set_winner_on_final(). It is THE scoring rule: every pick
-- in this league is against the spread, and this is the only thing in the system
-- that knows that. Losing it would not throw an error, it would just quietly
-- stop deciding games.

-- ---------------------------------------------------------------------------
-- ATS SCORING. `winner` is the side that COVERED, never the side that won.
--
--   home covers  <=>  (HomePts - AwayPts) + line > 0  <=>  HomePts + line > AwayPts
--
-- Fires only on picked or tiebreaker games, only once the game is final, and
-- only while winner is still null - so a result the commissioner sets by hand is
-- never overwritten.
--
-- Note the push case: with a whole-number line, HomePts + line = AwayPts sends
-- the cover to the AWAY team rather than voiding the game. Every line in the
-- data so far ends in .5, which makes a push impossible, so this has never
-- fired. Left exactly as production has it rather than "fixed" here, because
-- changing scoring rules is a league decision, not a migration.
create or replace function public.set_winner_on_final()
returns trigger
language plpgsql
as $function$begin
  -- Act if the game is picked OR it is marked as a tiebreaker
  if new."picked" = true or new."tiebreaker" = true then

    -- Only compute when final and winner is null
    if new."Status" = 'final' and new."winner" is null then

      if new."HomePts" is not null
         and new."AwayPts" is not null
         and new."line"    is not null then

        if (new."HomePts" + new."line") > new."AwayPts" then
          new."winner" := new."Home";
        else
          new."winner" := new."Away";
        end if;

      end if;
    end if;

  end if;

  return new;
end;$function$;

drop trigger if exists trg_set_winner_on_final on public.all_games;
create trigger trg_set_winner_on_final
  before update of "Status", "HomePts", "AwayPts", line on public.all_games
  for each row execute function public.set_winner_on_final();

-- ---------------------------------------------------------------------------
-- picks.updated_at is maintained by a trigger, which is why it reads as
-- bulk-rewritten rather than per-pick and cannot be used to tell who submitted
-- early. js/scoutModel.js records that as a stat it deliberately does not use.
create or replace function public.update_picks_timestamp()
returns trigger
language plpgsql
as $function$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$function$;

drop trigger if exists picks_updated_at_trigger on public.picks;
create trigger picks_updated_at_trigger
  before update on public.picks
  for each row execute function public.update_picks_timestamp();

-- ---------------------------------------------------------------------------
-- Claiming a team on the auth flow. SECURITY DEFINER because the caller cannot
-- update teams directly; the where clause is what stops it taking someone else's.
create or replace function public.claim_team(p_team_id integer)
returns void
language plpgsql
security definer
set search_path to 'public'
as $function$
begin
  update public.teams
     set user_id = auth.uid()
   where team_id = p_team_id
     and (user_id is null or user_id = auth.uid());
end;
$function$;

-- ---------------------------------------------------------------------------
create or replace function public.get_distinct_weeks()
returns table(week integer)
language sql
stable
as $function$
  select distinct week
  from public.all_games
  where week is not null
  order by week
$function$;

grant execute on function public.claim_team(integer) to authenticated;
grant execute on function public.get_distinct_weeks() to anon, authenticated;
