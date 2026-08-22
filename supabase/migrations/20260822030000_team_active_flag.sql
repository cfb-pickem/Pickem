-- Players come and go between seasons, but their history has to survive. A team
-- is shown for a given season if it is ACTIVE, or if it has picks in that
-- season - so a team that sits out disappears from the current leaderboard while
-- every past season still lists it with its full record.
--
-- Deliberately a single flag rather than a team_seasons table: the only question
-- anyone actually asks is "are they playing right now", and past participation is
-- already recorded by the picks themselves.
alter table public.teams
  add column if not exists active boolean not null default true;

comment on column public.teams.active is
  'Is this team playing the current season? Past seasons still show them if they have picks that season.';

-- Quinning is sitting out 2026.
update public.teams set active = false where team_name = 'Quinning';

-- Members may not flip their own active flag - that is a commissioner decision.
revoke insert (active), update (active) on public.teams from anon, authenticated;

-- The Made Picks / Not Yet panel should stop chasing someone who isn't playing.
create or replace function public.submission_status(p_season int, p_week int)
returns table(team_id bigint, team_name text, submitted boolean)
language sql
stable
security definer
set search_path = public
as $$
  select t.team_id,
         t.team_name,
         exists (
           select 1
             from public.picks p
             join public.all_games g on g."GameId" = p.game_id
            where p.team_id = t.team_id
              and g.cfb_season = p_season
              and g.week = p_week
              and coalesce(g.picked,false)
              and p.pick is not null
              and btrim(p.pick) <> ''
         ) as submitted
    from public.teams t
   where t.active
      or exists (                       -- kept if they played that season
        select 1 from public.picks p2
          join public.all_games g2 on g2."GameId" = p2.game_id
         where p2.team_id = t.team_id and g2.cfb_season = p_season
      )
   order by t.team_name;
$$;
