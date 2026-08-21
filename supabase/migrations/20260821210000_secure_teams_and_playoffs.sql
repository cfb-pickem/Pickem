-- Critical security + correctness fixes found by auditing the live policies.
--
-- 1. PRIVILEGE ESCALATION
--    The teams UPDATE policy is:
--        USING/CHECK ((user_id IS NULL) OR (user_id = auth.uid()))
--    which controls WHICH ROWS you may update, but not WHICH COLUMNS. Your own
--    row satisfies user_id = auth.uid(), so any signed-in member could run
--        update teams set commissioner = true where team_id = <their own>;
--    and grant themselves the ability to rewrite anyone's picks and any score.
--
--    RLS can't express "this column may not change" (WITH CHECK only sees the
--    new row, never the old one), so the right tool is a column-level privilege.
--    Nothing in the app writes commissioner or team_id - verified - so revoking
--    them breaks no existing flow.
revoke insert (commissioner), update (commissioner) on public.teams from anon, authenticated;
revoke update (team_id)                            on public.teams from anon, authenticated;

-- 2. "CREATE TEAM" WAS BROKEN FOR EVERYONE
--    public.teams had only SELECT and UPDATE policies - no INSERT policy at all -
--    so claim-team.html's .insert({team_name, user_id}) failed for every user.
--    With zero unclaimed teams left, a new member had no way into the league.
--
--    Scoped so a member can only create a row owned by themselves, and only if
--    they don't already have a team (otherwise one account could spam the
--    leaderboard). commissioner is unwritable per the revoke above, so a
--    newly-created team can never arrive pre-promoted.
drop policy if exists "teams: create own" on public.teams;
create policy "teams: create own"
  on public.teams
  for insert
  to authenticated
  with check (
    user_id = auth.uid()
    and not exists (
      select 1 from public.teams t where t.user_id = auth.uid()
    )
  );

-- 3. PLAYOFF SEEDS WERE INVISIBLE TO SIGNED-OUT VISITORS
--    public.playoffs holds 4 seed rows, but its only SELECT policy grants the
--    `authenticated` role. RLS filters silently rather than erroring, so a
--    logged-out visitor got an empty array and the playoff leaderboard rendered
--    game columns with no teams under them. The leaderboard is public, so the
--    seeds should be too.
drop policy if exists "playoffs: public read" on public.playoffs;
create policy "playoffs: public read"
  on public.playoffs
  for select
  to anon
  using (true);

-- 1b. The column-level REVOKE above is a no-op on its own: anon/authenticated
--     hold a TABLE-level UPDATE/INSERT grant, which covers every column and
--     outranks any column-level revoke. The table-level grant has to go first,
--     then the specific columns are granted back.
--
--     Columns deliberately withheld:
--       commissioner - self-promotion to commissioner (the escalation)
--       team_id      - it's the primary key
revoke insert, update on public.teams from anon, authenticated;

-- anon has no business writing to teams at all; claiming requires a session.
grant update (team_name, user_id) on public.teams to authenticated;
grant insert (team_name, user_id) on public.teams to authenticated;
