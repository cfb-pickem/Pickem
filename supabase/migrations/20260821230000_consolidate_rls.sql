-- 39 policies had accumulated across five tables, most of them near-duplicates
-- added over time. Two concrete problems, not just untidiness:
--
--   * picks."Users can update their own picks" had USING but NO WITH CHECK, so
--     the NEW row was never validated. A member could update their own pick and
--     change team_id to someone else's, writing into another team's picks.
--     Same shape on "Commissioners can update all picks".
--
--   * picks had THREE separate SELECT policies of USING (true). Postgres ORs
--     permissive policies, so those made the "own picks only" and "commissioner
--     only" policies completely inert - they looked like protection and provided
--     none.
--
-- Read access is deliberately left public: this is a public leaderboard, and the
-- front end is what decides when to reveal picks. Tightening SELECT on `picks`
-- to hide un-kicked-off weeks is a separate change, because the "Made Picks /
-- Not Yet" panel needs to know who has submitted before a week locks.

-- Helpers, so the policies below read as English instead of repeated subqueries.
-- SECURITY DEFINER so they keep working if teams SELECT is ever restricted.
create or replace function public.is_commissioner()
returns boolean language sql stable security definer set search_path = public as $$
  select exists (select 1 from public.teams t
                  where t.user_id = auth.uid() and t.commissioner is true);
$$;

create or replace function public.owns_team(p_team_id bigint)
returns boolean language sql stable security definer set search_path = public as $$
  select exists (select 1 from public.teams t
                  where t.team_id = p_team_id and t.user_id = auth.uid());
$$;

grant execute on function public.is_commissioner()      to anon, authenticated;
grant execute on function public.owns_team(bigint)      to anon, authenticated;

-- Clear the slate on these five tables, by enumeration so nothing is missed.
do $$
declare r record;
begin
  for r in select tablename, policyname from pg_policies
            where schemaname = 'public'
              and tablename in ('picks','all_games','logos','teams','playoffs')
  loop
    execute format('drop policy %I on public.%I', r.policyname, r.tablename);
  end loop;
end $$;

-- ---------- picks ----------
create policy picks_read on public.picks
  for select to anon, authenticated using (true);

create policy picks_insert_own on public.picks
  for insert to authenticated with check (public.owns_team(team_id));
create policy picks_update_own on public.picks
  for update to authenticated
  using (public.owns_team(team_id)) with check (public.owns_team(team_id));
create policy picks_delete_own on public.picks
  for delete to authenticated using (public.owns_team(team_id));

create policy picks_insert_commissioner on public.picks
  for insert to authenticated with check (public.is_commissioner());
create policy picks_update_commissioner on public.picks
  for update to authenticated
  using (public.is_commissioner()) with check (public.is_commissioner());
create policy picks_delete_commissioner on public.picks
  for delete to authenticated using (public.is_commissioner());

-- ---------- all_games ----------
-- No INSERT policy at all: only the ingest (service_role, which bypasses RLS)
-- creates games. commissioner.html now issues UPDATEs rather than upserts, so
-- nothing in a browser needs to insert - and without this policy a stale save
-- can no longer resurrect a deleted game as a NULL-filled shell row.
create policy all_games_read on public.all_games
  for select to anon, authenticated using (true);
create policy all_games_update_commissioner on public.all_games
  for update to authenticated
  using (public.is_commissioner()) with check (public.is_commissioner());

-- ---------- logos ----------
create policy logos_read on public.logos
  for select to anon, authenticated using (true);

-- ---------- teams ----------
-- Column-level grants (earlier migration) are what stop commissioner/team_id
-- being written; these govern which ROWS are reachable.
create policy teams_read on public.teams
  for select to anon, authenticated using (true);
create policy teams_create_own on public.teams
  for insert to authenticated
  with check (user_id = auth.uid()
              and not exists (select 1 from public.teams t where t.user_id = auth.uid()));
create policy teams_claim_or_own on public.teams
  for update to authenticated
  using (user_id is null or user_id = auth.uid())
  with check (user_id is null or user_id = auth.uid());

-- ---------- playoffs ----------
create policy playoffs_read on public.playoffs
  for select to anon, authenticated using (true);
create policy playoffs_write_commissioner on public.playoffs
  for all to authenticated
  using (public.is_commissioner()) with check (public.is_commissioner());
