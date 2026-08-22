-- Make the pick deadline real. Until now the lock lived only in the browser:
-- any signed-in member could run supabase.from('picks').upsert(...) from the
-- console on Sunday morning, and because picks SELECT was public, everyone's
-- picks sat in the network tab before kickoff for anyone who looked.
--
-- Both halves are closed here, with the commissioner exempt from all of it.
--
-- RESTRICTIVE policies are used deliberately: Postgres ANDs them with the
-- existing permissive ones, so these can only ever tighten access. They cannot
-- accidentally grant anything, and they compose with the policies already there
-- rather than replacing them.

-- Is this game still open for picks? Mirrors the front end exactly:
--   regular season -> the whole week closes when its FIRST game kicks off
--   playoffs (week > 15) -> each game closes at its own kickoff
create or replace function public.picks_open_for_game(p_game_id bigint)
returns boolean
language sql
stable
security definer
set search_path = public
as $$
  select case
           when g.week is null or g."Start (CT)" is null then false   -- unknown: closed
           when g.week > 15 then
             (g."Start (CT)")::timestamp > (now() at time zone 'America/Chicago')
           else not exists (
             select 1
               from public.all_games x
              where x.cfb_season = g.cfb_season
                and x.week = g.week
                and (coalesce(x.picked,false) or coalesce(x.tiebreaker,false))
                and x."Start (CT)" is not null
                and (x."Start (CT)")::timestamp <= (now() at time zone 'America/Chicago')
           )
         end
    from public.all_games g
   where g."GameId" = p_game_id;
$$;

grant execute on function public.picks_open_for_game(bigint) to anon, authenticated;

-- 1. WRITES: no saving a pick once its week has started.
--    Separate policies per command rather than FOR ALL, because FOR ALL would
--    also apply to SELECT and hide exactly the picks we want visible.
drop policy if exists picks_deadline_insert on public.picks;
create policy picks_deadline_insert on public.picks
  as restrictive for insert to authenticated
  with check (public.is_commissioner() or public.picks_open_for_game(game_id));

drop policy if exists picks_deadline_update on public.picks;
create policy picks_deadline_update on public.picks
  as restrictive for update to authenticated
  using       (public.is_commissioner() or public.picks_open_for_game(game_id))
  with check  (public.is_commissioner() or public.picks_open_for_game(game_id));

drop policy if exists picks_deadline_delete on public.picks;
create policy picks_deadline_delete on public.picks
  as restrictive for delete to authenticated
  using (public.is_commissioner() or public.picks_open_for_game(game_id));

-- 2. READS: nobody sees anyone else's picks until that week has locked.
--    Your own picks are always visible to you, and the commissioner sees all.
drop policy if exists picks_hidden_until_lock on public.picks;
create policy picks_hidden_until_lock on public.picks
  as restrictive for select to anon, authenticated
  using (
    public.is_commissioner()
    or public.owns_team(team_id)
    or not public.picks_open_for_game(game_id)
  );

-- 3. The "Made Picks / Not Yet" panel needs to know WHO has submitted before a
--    week locks - which the policy above now (correctly) prevents it from
--    working out from the picks themselves. This returns only the boolean, never
--    a pick value, so it answers that question without leaking anything.
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
   order by t.team_name;
$$;

grant execute on function public.submission_status(int, int) to anon, authenticated;
