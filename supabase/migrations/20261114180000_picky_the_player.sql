-- Picky plays too, as an exhibition team.
--
-- Picky (the league chat bot) makes picks every week and shows on the
-- scoreboard behind the pickyOn() flag, but he never wins money, never counts
-- for the jackpot or tiebreakers, and never bumps a human out of a payout,
-- prize or placement. Humans' ranks and payouts are worked out as if he
-- didn't exist.
--
-- The cleanest way to guarantee that is to make him invisible to everything
-- that already exists. RESTRICTIVE policies hide exhibition teams and their
-- picks from anon/authenticated, so every page, every standings/payout/odds
-- calculation in the browser and every chat.py read (all made with the user's
-- JWT) keeps seeing exactly the rows it sees today. The flag-gated UI reads him
-- through picky_player(), which applies the same reveal rule as human picks.
--
-- Additive only: one column (default false, so every existing team is
-- unchanged), one table, policies that can only tighten access, and two
-- functions. No existing row is altered or deleted.
--
-- Checked before writing: the only security-definer function that lists teams
-- is submission_status(), and its one caller (index.html) intersects it with
-- the RLS-filtered teams list, so Picky never reaches the Made Picks panel.
-- Slots/jackpot functions only count by_slots picks; Picky never uses slots.

alter table public.teams
  add column if not exists is_exhibition boolean not null default false;

comment on column public.teams.is_exhibition is
  'Exhibition team (Picky the bot). Hidden from normal reads by RLS; never ranked, paid, or counted for jackpot/tiebreakers.';

-- Members can't set it: teams only grants insert/update on (team_name, user_id)
-- to authenticated, so this column is already unwritable for them.

-- Evaluated once per query (wrapped in a scalar subquery below), not per row.
create or replace function public.exhibition_team_ids()
returns bigint[]
language sql
stable
security definer
set search_path = public
as $$
  select coalesce(array_agg(team_id), '{}') from public.teams where is_exhibition;
$$;

grant execute on function public.exhibition_team_ids() to anon, authenticated;

-- ---------- teams ----------
drop policy if exists teams_hide_exhibition on public.teams;
create policy teams_hide_exhibition on public.teams
  as restrictive for select to anon, authenticated
  using (not is_exhibition);

drop policy if exists teams_exhibition_locked on public.teams;
create policy teams_exhibition_locked on public.teams
  as restrictive for update to anon, authenticated
  using (not is_exhibition)
  with check (not is_exhibition);

-- ---------- picks ----------
drop policy if exists picks_hide_exhibition on public.picks;
create policy picks_hide_exhibition on public.picks
  as restrictive for select to anon, authenticated
  using (team_id <> all ((select public.exhibition_team_ids())));

drop policy if exists picks_exhibition_insert on public.picks;
create policy picks_exhibition_insert on public.picks
  as restrictive for insert to anon, authenticated
  with check (team_id <> all ((select public.exhibition_team_ids())));

drop policy if exists picks_exhibition_update on public.picks;
create policy picks_exhibition_update on public.picks
  as restrictive for update to anon, authenticated
  using (team_id <> all ((select public.exhibition_team_ids())))
  with check (team_id <> all ((select public.exhibition_team_ids())));

drop policy if exists picks_exhibition_delete on public.picks;
create policy picks_exhibition_delete on public.picks
  as restrictive for delete to anon, authenticated
  using (team_id <> all ((select public.exhibition_team_ids())));

-- ---------- Picky's trash talk, one line per pick ----------
create table if not exists public.picky_pick_blurbs (
  id           bigint generated always as identity primary key,
  team_id      bigint not null references public.teams(team_id),
  game_id      bigint not null,
  pick         text not null,
  method       text not null,          -- value | sp+ | fpi | points
  reason       text not null,          -- the deterministic why, in plain words
  blurb        text not null,          -- written by the local model
  generated_at timestamptz not null,   -- when the pick was made; always before lock
  created_at   timestamptz not null default now(),
  unique (team_id, game_id)
);

comment on table public.picky_pick_blurbs is
  'Picky the player: why he took each side and his trash-talk line. Read only through picky_player().';

-- RLS on with no policies: only the service role writes, and the browser reads
-- through picky_player() so the reveal rule can't be skipped.
alter table public.picky_pick_blurbs enable row level security;

-- ---------- the one way the site reads him ----------
-- His picks follow the human reveal rule: hidden until picks_open_for_game()
-- goes false (regular season: the week's first kickoff; playoffs: that game's
-- kickoff). The commissioner sees them early, same as human picks.
create or replace function public.picky_player(p_season int)
returns jsonb
language sql
stable
security definer
set search_path = public
as $$
  select jsonb_build_object(
    'team_id', t.team_id,
    'team_name', t.team_name,
    'picks', coalesce((
      select jsonb_agg(jsonb_build_object(
               'game_id', p.game_id,
               'week', g.week,
               'pick', p.pick,
               'away', g."Away",
               'home', g."Home",
               'line', g.line,
               'status', g."Status",
               'winner', g.winner,
               'tiebreaker', coalesce(g.tiebreaker, false),
               'start', g."Start (CT)",
               'method', b.method,
               'reason', b.reason,
               'blurb', b.blurb
             ) order by g."Start (CT)", p.game_id)
        from public.picks p
        join public.all_games g on g."GameId" = p.game_id
        left join public.picky_pick_blurbs b on b.team_id = p.team_id and b.game_id = p.game_id
       where p.team_id = t.team_id
         and g.cfb_season = p_season
         and (public.is_commissioner() or not public.picks_open_for_game(p.game_id))
    ), '[]'::jsonb)
  )
  from public.teams t
  where t.is_exhibition
  order by t.team_id
  limit 1;
$$;

grant execute on function public.picky_player(int) to anon, authenticated;
