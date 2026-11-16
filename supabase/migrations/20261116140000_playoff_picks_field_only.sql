-- ONLY THE PLAYOFF FIELD PICKS PLAYOFF GAMES.
--
-- Once a season has playoff seeds (rows in public.playoffs, written by the
-- commissioner or by the playoff autopilot), the games after the regular
-- season (week > 15, tiebreakers included) belong to the four seeded teams.
-- Everyone else can still read every game and every pick; they just can't
-- write one. picks.html already greys the form out for them, but a page is
-- not a lock, so the database says no as well, whatever the request looks
-- like.
--
-- Untouched: the regular season (weeks 0-15), any season without seeds, the
-- commissioner (is_commissioner() can still enter or fix anyone's pick), and
-- server-side writers (service role, cron, the exhibition player), which the
-- trigger skips the same way picks_only_on_the_slate does.
--
-- The check lives in a SECURITY DEFINER function so a future change to who
-- can read public.playoffs can never quietly turn the gate off.

create or replace function public.playoff_pick_blocked(p_team_id bigint, p_game_id bigint)
returns boolean
language sql
stable
security definer
set search_path = public
as $$
  select exists (
    select 1
      from public.all_games g
     where g."GameId" = p_game_id
       and g.week > 15
       and exists (select 1 from public.playoffs po where po.season = g.cfb_season)
       and not exists (select 1 from public.playoffs po
                        where po.season = g.cfb_season and po.team_id = p_team_id)
  );
$$;

revoke all on function public.playoff_pick_blocked(bigint, bigint) from public;
grant execute on function public.playoff_pick_blocked(bigint, bigint) to anon, authenticated, service_role;

create or replace function public.picks_playoff_field_only()
returns trigger
language plpgsql
set search_path to 'public'
as $$
begin
  if current_user not in ('anon', 'authenticated') then
    return new;
  end if;

  -- An update that leaves the team, the game, the pick and the slots flag
  -- alone isn't anybody making a pick (updated_at, say).
  if tg_op = 'UPDATE'
     and new.team_id  is not distinct from old.team_id
     and new.game_id  is not distinct from old.game_id
     and new.pick     is not distinct from old.pick
     and new.by_slots is not distinct from old.by_slots then
    return new;
  end if;

  if public.is_commissioner() then
    return new;
  end if;

  if public.playoff_pick_blocked(new.team_id, new.game_id) then
    raise exception 'Only the four playoff teams can make playoff picks.'
      using errcode = 'check_violation',
            hint    = 'Your team didn''t make the playoff field this season, so playoff games are view only.';
  end if;

  return new;
end;
$$;

drop trigger if exists picks_playoff_field_only on public.picks;
create trigger picks_playoff_field_only
  before insert or update on public.picks
  for each row execute function public.picks_playoff_field_only();
