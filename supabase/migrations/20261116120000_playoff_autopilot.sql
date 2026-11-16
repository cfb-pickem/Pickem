-- PLAYOFF AUTOPILOT
--
-- Until now the switch from the regular season to the playoffs was by hand:
-- after week 15 the commissioner worked out the top 4 (and any tie at 4th)
-- and saved them in commissioner.html's "Playoff Teams" panel. Those four
-- rows in public.playoffs ARE the switch: picks.html goes into playoff mode
-- the moment a season has any, and the leaderboard reads its seeds from them.
--
-- This writes the same rows, on its own, at the right moment:
--
--   * Regular season = picked, non-tiebreaker games in weeks 0-15. A point is
--     a correct pick against the graded `winner` (or an AUTO WIN jackpot pick),
--     exactly like index.html's computeSeasonWinsThroughWeek().
--   * It flips only when week 15 is set and kicked off, every regular-season
--     game is graded, no week 1-15 is empty, and 12 hours have passed since
--     week 15's last kickoff (late stat corrections).
--   * Top 4 advance (League Rules: "Top 4 advance, scores reset"). A tie at 4th
--     goes down the regular-season tiebreakers in order:
--       1 conference championship game wins (tiebreaker games before week 16)
--       2 Army v Navy
--       3 chips (blackjack_bankrolls)
--       4 Celebration Bowl
--       5 win % over the last 8 decided picks (stats.html's definition)
--       6 coin flip
--     A tiebreaker game can only separate the tied teams through the picks
--     they made on it. So one that isn't graded yet HOLDS the switch only if
--     the tied teams picked it differently; identical picks (or none, which is
--     what happens for a game nobody could pick, like one never flagged) can't
--     break the tie and it moves on to the next tiebreaker. It never flips a
--     coin: that one is the commissioner's.
--   * Seeds 1-4 follow regular-season points, then the same tiebreakers. The
--     rules don't use seeds for anything (playoff ties go Army-Navy if unused,
--     regular season points, other bowls), so seed order inside a tie that
--     nothing breaks yet falls back to name and is marked cosmetic.
--
-- Safety: the cron tick only acts on a season whose playoff_autopilot row is
-- enabled; it never touches a season that already has seeds (so a hand-entered
-- set always wins); every decision is logged; and the commissioner gets
-- preview / pause / resume / force / undo through playoff_autopilot_admin().

-- 1. Who wrote each seed row. Hand-saved rows keep the default.
alter table public.playoffs
  add column if not exists source text not null default 'commissioner';
do $$
begin
  alter table public.playoffs add constraint playoffs_source_check
    check (source in ('commissioner','auto'));
exception when duplicate_object then null;
end $$;

-- 2. State, one row per season. Public read: the leaderboard shows the state.
create table if not exists public.playoff_autopilot (
  season          integer primary key,
  enabled         boolean not null default false,
  state           text not null default 'regular'
                  check (state in ('regular','holding','playoffs')),
  holds           jsonb not null default '[]'::jsonb,
  preview         jsonb,
  army_navy_used  boolean,
  checked_at      timestamptz,
  switched_at     timestamptz,
  switched_by     text,
  updated_at      timestamptz not null default now()
);
alter table public.playoff_autopilot enable row level security;
drop policy if exists playoff_autopilot_read on public.playoff_autopilot;
create policy playoff_autopilot_read on public.playoff_autopilot
  for select to anon, authenticated using (true);
revoke insert, update, delete on public.playoff_autopilot from anon, authenticated;
grant select on public.playoff_autopilot to anon, authenticated;

create table if not exists public.playoff_autopilot_log (
  id      bigserial primary key,
  season  integer not null,
  at      timestamptz not null default now(),
  actor   text not null,
  action  text not null,
  detail  jsonb
);
alter table public.playoff_autopilot_log enable row level security;
drop policy if exists playoff_autopilot_log_read on public.playoff_autopilot_log;
create policy playoff_autopilot_log_read on public.playoff_autopilot_log
  for select to anon, authenticated using (true);
revoke insert, update, delete on public.playoff_autopilot_log from anon, authenticated;
grant select on public.playoff_autopilot_log to anon, authenticated;

-- 3. Helpers.
create or replace function public._po_kick(ts timestamp)
returns timestamptz language sql immutable as $$
  select ts at time zone 'America/Chicago'
$$;

-- MEAC and SWAC football schools: the Celebration Bowl is the tiebreaker game
-- with one on each side. all_games has no bowl-name column, so this is how
-- it's recognised.
create or replace function public._po_is_hbcu(t text)
returns boolean language sql immutable as $$
  select lower(trim(coalesce(t,''))) in (
    'howard','morgan state','norfolk state','north carolina central','nc central',
    'delaware state','south carolina state','alabama a&m','alabama state',
    'alcorn state','arkansas-pine bluff','bethune-cookman','florida a&m',
    'grambling','grambling state','jackson state','mississippi valley state',
    'prairie view a&m','southern','texas southern')
$$;

-- 4. The computation. Reads only graded games' picks (all past their lock),
-- chips and teams, so the preview is safe to show anyone. Writes nothing.
create or replace function public.playoff_compute(
  p_season integer,
  p_force  boolean default false,
  p_now    timestamptz default now()
) returns jsonb
language plpgsql stable security definer set search_path = public as $$
declare
  v_holds      jsonb := '[]'::jsonb;
  v_notes      jsonb := '[]'::jsonb;
  v_phase      text;
  v_w15_n      int;
  v_w15_last   timestamptz;
  v_w15_kicked boolean;
  v_settle     timestamptz;
  v_ungraded   jsonb;
  v_empty      int[];
  v_has_bj     boolean;
  v_conf_state text;
  v_an_state   text;
  v_cb_state   text;
  v_an_id      bigint;
  v_cb_id      bigint;
  v_teams      jsonb;
  v_n          int;
  v_p4         int;
  v_above      int;
  v_slots      int;
  v_tied       jsonb;
  v_cut        jsonb := null;
  v_crit       text;
  v_state      text;
  v_a          jsonb;
  v_b          jsonb;
  v_decided    text;
  v_blocked    text;
  v_same       boolean;
  v_quals      jsonb := '[]'::jsonb;
  v_seeded     boolean;
  v_crits      text[] := array['conf','army_navy','chips','celebration','last8'];
  v_label      jsonb := jsonb_build_object(
                  'points','regular season points',
                  'conf','conference championship game wins',
                  'army_navy','Army v Navy',
                  'chips','chips',
                  'celebration','Celebration Bowl',
                  'last8','win % over the last 8 picks',
                  'name','name (seed order only, the rules never use it)');
  i            int;
begin
  select exists (select 1 from playoffs where season = p_season) into v_seeded;

  -- Regular-season schedule state.
  select count(*) filter (where week = 15),
         max(_po_kick("Start (CT)")) filter (where week = 15),
         coalesce(bool_and(_po_kick("Start (CT)") <= p_now) filter (where week = 15), false)
    into v_w15_n, v_w15_last, v_w15_kicked
    from all_games
   where cfb_season = p_season and picked is true and coalesce(tiebreaker,false) = false
     and week between 0 and 15;

  select coalesce(jsonb_agg(jsonb_build_object('game_id', "GameId", 'week', week,
           'game', "Away" || ' @ ' || "Home", 'status', "Status") order by week, "GameId"), '[]'::jsonb)
    into v_ungraded
    from all_games
   where cfb_season = p_season and picked is true and coalesce(tiebreaker,false) = false
     and week between 0 and 15 and winner is null;

  select coalesce(array_agg(w order by w), '{}')
    into v_empty
    from generate_series(1, 15) w
   where not exists (select 1 from all_games g where g.cfb_season = p_season and g.week = w
                       and g.picked is true and coalesce(g.tiebreaker,false) = false);

  v_settle := v_w15_last + interval '12 hours';

  if v_w15_n = 0 or not v_w15_kicked then
    v_phase := 'regular';
    v_holds := v_holds || jsonb_build_object('code','season_in_progress',
      'message', case when v_w15_n = 0 then 'Week 15 games aren''t picked yet, so the regular season isn''t over.'
                      else 'Week 15 hasn''t finished kicking off yet.' end);
  else
    v_phase := 'holding';
  end if;
  if jsonb_array_length(v_ungraded) > 0 and v_phase = 'holding' then
    v_holds := v_holds || jsonb_build_object('code','ungraded',
      'message', jsonb_array_length(v_ungraded) || ' regular-season game(s) not graded yet: ' ||
        (select string_agg(e->>'game' || ' (week ' || (e->>'week') || ')', ', ') from jsonb_array_elements(v_ungraded) e),
      'games', v_ungraded);
  end if;
  if cardinality(v_empty) > 0 and v_phase = 'holding' then
    v_holds := v_holds || jsonb_build_object('code','missing_week',
      'message', 'No picked games in week(s) ' || array_to_string(v_empty, ', ') || '.', 'weeks', to_jsonb(v_empty));
  end if;
  if v_phase = 'holding' and p_now < v_settle then
    v_holds := v_holds || jsonb_build_object('code','settling',
      'message', 'Waiting 12 hours after the last week 15 kickoff for late score fixes.', 'until', v_settle);
  end if;

  -- Tiebreaker availability. graded = usable now; pending = flagged, not graded;
  -- unflagged = no such tiebreaker game yet; none = can't exist (no Pit that year).
  select case when count(*) = 0 then 'unflagged'
              when bool_and(winner is not null) then 'graded' else 'pending' end
    into v_conf_state
    from all_games where cfb_season = p_season and tiebreaker is true and (week is null or week < 16);

  select "GameId", case when winner is null then 'pending' else 'graded' end
    into v_an_id, v_an_state
    from all_games
   where cfb_season = p_season and tiebreaker is true
     and lower("Away") in ('army','navy') and lower("Home") in ('army','navy')
   order by "GameId" limit 1;
  v_an_state := coalesce(v_an_state, 'unflagged');

  select "GameId", case when winner is null then 'pending' else 'graded' end
    into v_cb_id, v_cb_state
    from all_games
   where cfb_season = p_season and tiebreaker is true and _po_is_hbcu("Away") and _po_is_hbcu("Home")
   order by "GameId" limit 1;
  v_cb_state := coalesce(v_cb_state, 'unflagged');

  select exists (select 1 from blackjack_bankrolls where cfb_season = p_season) into v_has_bj;

  -- Per-team numbers. Humans only (Picky the exhibition player is never ranked),
  -- active or played this season, like the leaderboard's teamsForSeason().
  with reg as (
    select "GameId" gid, week, winner from all_games
     where cfb_season = p_season and picked is true and coalesce(tiebreaker,false) = false
       and week between 0 and 15
  ), dec as (
    select p.team_id, r.week, r.gid,
           (coalesce(p.jackpot,false) or lower(trim(p.pick)) = lower(trim(r.winner))) as win
      from picks p join reg r on r.gid = p.game_id
     where r.winner is not null
  ), tbg as (
    select "GameId" gid, winner,
           case when week is null or week < 16 then 'conf'
                when "GameId" = v_an_id then 'army_navy'
                when "GameId" = v_cb_id then 'celebration' end as kind
      from all_games where cfb_season = p_season and tiebreaker is true and winner is not null
  ), tbw as (
    select p.team_id, g.kind, count(*) filter (where lower(trim(p.pick)) = lower(trim(g.winner))) as n
      from picks p join tbg g on g.gid = p.game_id
     where g.kind is not null
     group by p.team_id, g.kind
  ), played as (
    select distinct p.team_id from picks p join reg r on r.gid = p.game_id
  ), l8 as (
    select team_id, count(*) n, count(*) filter (where win) w from (
      select team_id, win, row_number() over (partition by team_id order by week desc, gid desc) rn from dec
    ) x where rn <= 8 group by team_id
  ), t as (
    select tm.team_id, tm.team_name,
           coalesce((select count(*) filter (where win) from dec d where d.team_id = tm.team_id), 0)::int as points,
           coalesce((select n from tbw where tbw.team_id = tm.team_id and kind = 'conf'), 0)::int as conf,
           coalesce((select n from tbw where tbw.team_id = tm.team_id and kind = 'army_navy'), 0)::int as army_navy,
           case when v_has_bj then coalesce((select chips from blackjack_bankrolls b where b.team_id = tm.team_id and b.cfb_season = p_season), 1000) end::int as chips,
           coalesce((select n from tbw where tbw.team_id = tm.team_id and kind = 'celebration'), 0)::int as celebration,
           coalesce(l8.w, 0)::int as l8w, coalesce(l8.n, 0)::int as l8n,
           case when coalesce(l8.n,0) = 0 then 0 else round(l8.w::numeric / l8.n, 4) end as last8
      from teams tm
      left join l8 on l8.team_id = tm.team_id
     where coalesce(tm.is_exhibition, false) = false
       and (tm.active is not false or tm.team_id in (select team_id from played))
  )
  select coalesce(jsonb_agg(to_jsonb(t) order by points desc, conf desc, army_navy desc,
                            chips desc nulls last, celebration desc, last8 desc, team_name), '[]'::jsonb)
    into v_teams from t;

  v_n := jsonb_array_length(v_teams);
  if v_n < 4 then
    v_holds := v_holds || jsonb_build_object('code','not_enough_teams','message','Fewer than 4 teams.');
  else
    v_p4    := (v_teams->3->>'points')::int;
    v_above := (select count(*) from jsonb_array_elements(v_teams) e where (e->>'points')::int > v_p4);
    v_slots := 4 - v_above;
    select jsonb_agg(e order by ord) into v_tied
      from jsonb_array_elements(v_teams) with ordinality as x(e, ord) where (e->>'points')::int = v_p4;

    if jsonb_array_length(v_tied) > v_slots then
      -- Tie across the cut: last team in vs first team out, criterion by criterion.
      v_a := v_tied->(v_slots-1);
      v_b := v_tied->v_slots;
      v_decided := null; v_blocked := null;
      foreach v_crit in array v_crits loop
        v_state := case v_crit when 'conf' then v_conf_state when 'army_navy' then v_an_state
                               when 'chips' then case when v_has_bj then 'graded' else 'none' end
                               when 'celebration' then v_cb_state else 'graded' end;
        if v_state = 'none' then continue; end if;
        if v_state = 'pending' and not p_force then
          -- Did the tied teams pick the ungraded game(s) differently? If not,
          -- the result moves them all the same way and can't break the tie.
          select coalesce(bool_and(nd = 1), true) into v_same from (
            select g."GameId", count(distinct coalesce(lower(trim(p.pick)), '~')) nd
              from all_games g
              cross join (select (e->>'team_id')::bigint tid from jsonb_array_elements(v_tied) e) tt
              left join picks p on p.game_id = g."GameId" and p.team_id = tt.tid
             where g.cfb_season = p_season and g.tiebreaker is true and g.winner is null
               and case v_crit when 'conf' then (g.week is null or g.week < 16)
                               when 'army_navy' then g."GameId" = v_an_id
                               else g."GameId" = v_cb_id end
             group by g."GameId") x;
          if not v_same then v_blocked := v_crit; exit; end if;
        end if;
        if (v_a->>v_crit) is distinct from (v_b->>v_crit) then v_decided := v_crit; exit; end if;
      end loop;

      v_cut := jsonb_build_object(
        'points', v_p4, 'slots', v_slots,
        'teams', (select jsonb_agg(e->>'team_name') from jsonb_array_elements(v_tied) e),
        'decided_by', v_decided, 'waiting_on', v_blocked);

      if v_blocked is not null then
        v_holds := v_holds || jsonb_build_object('code','cut_tie_pending',
          'message', 'Tie for the last playoff spot at ' || v_p4 || ' points (' ||
            (select string_agg(e->>'team_name', ', ') from jsonb_array_elements(v_tied) e) ||
            '). It comes down to ' || (v_label->>v_blocked) || ', which hasn''t been graded yet.',
          'waiting_on', v_blocked);
      elsif v_decided is null then
        v_holds := v_holds || jsonb_build_object('code','coin_flip',
          'message', 'Tie for the last playoff spot at ' || v_p4 || ' points (' ||
            (select string_agg(e->>'team_name', ', ') from jsonb_array_elements(v_tied) e) ||
            ') survives every tiebreaker. It''s a coin flip: set the seeds by hand in Playoff Teams.');
      end if;
    end if;

    -- Seeds: first four in order, with what put each one above the next team.
    for i in 0..3 loop
      v_a := v_teams->i; v_b := v_teams->(i+1);
      v_decided := 'name';
      if v_b is null or (v_a->>'points') <> (v_b->>'points') then
        v_decided := 'points';
      else
        foreach v_crit in array v_crits loop
          if (v_a->>v_crit) is distinct from (v_b->>v_crit) then
            v_decided := v_crit; exit;
          end if;
        end loop;
      end if;
      v_quals := v_quals || jsonb_build_object('seed', i+1, 'team_id', (v_a->>'team_id')::bigint,
        'team_name', v_a->>'team_name', 'points', (v_a->>'points')::int,
        'decided_by', v_decided, 'decided_by_label', v_label->>v_decided);
    end loop;
  end if;

  if v_seeded then v_phase := 'playoffs'; end if;

  return jsonb_build_object(
    'season', p_season,
    'computed_at', p_now,
    'phase', v_phase,
    'already_seeded', v_seeded,
    'ready', (not v_seeded) and v_n >= 4 and (
               case when p_force
                    then not exists (select 1 from jsonb_array_elements(v_holds) h
                                      where h->>'code' in ('coin_flip','not_enough_teams'))
                    else jsonb_array_length(v_holds) = 0 end),
    'forced', p_force,
    'holds', v_holds,
    'qualifiers', v_quals,
    'cut_tie', v_cut,
    'army_navy_used', coalesce(v_cut->>'decided_by' = 'army_navy', false),
    'final_week_last_kickoff', v_w15_last,
    'settle_at', v_settle,
    'tiebreakers', jsonb_build_object('conf', v_conf_state, 'army_navy', v_an_state,
                     'chips', case when v_has_bj then 'graded' else 'none' end,
                     'celebration', v_cb_state),
    'standings', v_teams
  );
end $$;

revoke all on function public.playoff_compute(integer, boolean, timestamptz) from public;
grant execute on function public.playoff_compute(integer, boolean, timestamptz) to anon, authenticated;

-- 5. Write the seeds. Internal: called by the tick and by admin 'force'.
create or replace function public._po_apply(p_season integer, p_res jsonb, p_actor text)
returns jsonb language plpgsql security definer set search_path = public as $$
declare q jsonb;
begin
  if exists (select 1 from playoffs where season = p_season) then
    return jsonb_build_object('ok', false, 'message', 'Seeds already exist for ' || p_season || '.');
  end if;
  if not coalesce((p_res->>'ready')::boolean, false) then
    return jsonb_build_object('ok', false, 'holds', p_res->'holds', 'message', 'Not switching: ' ||
      coalesce((select string_agg(h->>'message', ' ') from jsonb_array_elements(p_res->'holds') h), 'not ready.'));
  end if;
  for q in select * from jsonb_array_elements(p_res->'qualifiers') loop
    insert into playoffs (season, seed, team_id, source)
    values (p_season, (q->>'seed')::int, (q->>'team_id')::int, 'auto');
  end loop;
  update playoff_autopilot
     set state = 'playoffs', holds = '[]'::jsonb, preview = p_res,
         army_navy_used = (p_res->>'army_navy_used')::boolean,
         switched_at = now(), switched_by = p_actor, checked_at = now(), updated_at = now()
   where season = p_season;
  insert into playoff_autopilot_log (season, actor, action, detail)
  values (p_season, p_actor, 'switch', jsonb_build_object('qualifiers', p_res->'qualifiers',
          'cut_tie', p_res->'cut_tie', 'forced', p_res->'forced'));
  return jsonb_build_object('ok', true, 'message', 'Playoffs are on.', 'qualifiers', p_res->'qualifiers');
end $$;
revoke all on function public._po_apply(integer, jsonb, text) from public, anon, authenticated;

-- 6. The cron tick. Current season only (the newest with picked games).
create or replace function public.playoff_autopilot_tick(p_now timestamptz default now())
returns jsonb language plpgsql security definer set search_path = public as $$
declare
  v_season int;
  v_row    playoff_autopilot;
  v_res    jsonb;
  v_state  text;
  v_codes  text;
begin
  select max(cfb_season) into v_season from all_games where picked is true;
  if v_season is null then return jsonb_build_object('ok', false, 'message', 'no season'); end if;

  -- One tick at a time per season, so two overlapping runs can't both insert.
  perform pg_advisory_xact_lock(hashtext('playoff_autopilot'), v_season);

  -- A new season inherits last season's on/off switch.
  insert into playoff_autopilot (season, enabled)
  values (v_season, coalesce((select enabled from playoff_autopilot where season < v_season
                               order by season desc limit 1), false))
  on conflict (season) do nothing;
  select * into v_row from playoff_autopilot where season = v_season;

  if exists (select 1 from playoffs where season = v_season) then
    if v_row.state <> 'playoffs' then
      update playoff_autopilot set state = 'playoffs', holds = '[]'::jsonb, checked_at = p_now,
             switched_at = coalesce(switched_at, now()), switched_by = coalesce(switched_by, 'commissioner'),
             updated_at = now()
       where season = v_season;
      insert into playoff_autopilot_log (season, actor, action, detail)
      values (v_season, 'auto', 'seeds_present', jsonb_build_object('note', 'Seeds were entered by hand; autopilot stands down.'));
    end if;
    return jsonb_build_object('ok', true, 'season', v_season, 'state', 'playoffs', 'acted', false);
  end if;

  v_res := playoff_compute(v_season, false, p_now);
  v_state := case when v_res->>'phase' = 'regular' then 'regular' else 'holding' end;
  v_codes := (select string_agg(h->>'code', ',' order by h->>'code') from jsonb_array_elements(v_res->'holds') h);

  if v_row.enabled and (v_res->>'ready')::boolean then
    return _po_apply(v_season, v_res, 'auto') || jsonb_build_object('season', v_season, 'acted', true);
  end if;

  if v_state is distinct from v_row.state
     or v_codes is distinct from (select string_agg(h->>'code', ',' order by h->>'code') from jsonb_array_elements(v_row.holds) h) then
    insert into playoff_autopilot_log (season, actor, action, detail)
    values (v_season, 'auto', case when v_row.enabled then 'hold' else 'preview_only' end,
            jsonb_build_object('state', v_state, 'holds', v_res->'holds', 'ready', v_res->'ready'));
  end if;
  update playoff_autopilot
     set state = v_state, holds = v_res->'holds', preview = v_res, checked_at = p_now, updated_at = now()
   where season = v_season;
  return jsonb_build_object('ok', true, 'season', v_season, 'state', v_state, 'acted', false,
                            'enabled', v_row.enabled, 'ready', v_res->'ready', 'holds', v_res->'holds');
end $$;
revoke all on function public.playoff_autopilot_tick(timestamptz) from public, anon, authenticated;

-- 7. Commissioner controls.
create or replace function public.playoff_autopilot_admin(p_season integer, p_action text)
returns jsonb language plpgsql security definer set search_path = public as $$
declare
  v_res  jsonb;
  v_who  text := coalesce((select team_name from teams where user_id = auth.uid() limit 1), current_user);
  v_rows jsonb;
begin
  -- Commissioner, the service key, or a direct database session (no JWT at all,
  -- which PostgREST never produces: it always sets the request role). Not
  -- current_user: inside SECURITY DEFINER that's this function's owner.
  if not (public.is_commissioner() or coalesce(auth.role(), 'direct') in ('service_role', 'direct')) then
    raise exception 'commissioner only' using errcode = '42501';
  end if;
  perform pg_advisory_xact_lock(hashtext('playoff_autopilot'), p_season);
  insert into playoff_autopilot (season) values (p_season) on conflict (season) do nothing;

  if p_action = 'preview' then
    return playoff_compute(p_season, false);

  elsif p_action in ('pause','resume') then
    update playoff_autopilot set enabled = (p_action = 'resume'), updated_at = now() where season = p_season;
    insert into playoff_autopilot_log (season, actor, action) values (p_season, v_who, p_action);
    if p_action = 'resume' and p_season = (select max(cfb_season) from all_games where picked is true) then
      return jsonb_build_object('ok', true, 'message', 'Autopilot on.', 'tick', playoff_autopilot_tick());
    end if;
    return jsonb_build_object('ok', true, 'message', case when p_action = 'pause' then 'Autopilot paused.' else 'Autopilot on.' end);

  elsif p_action = 'force' then
    v_res := playoff_compute(p_season, true);
    return _po_apply(p_season, v_res, 'commissioner');

  elsif p_action = 'undo' then
    select coalesce(jsonb_agg(jsonb_build_object('seed', seed, 'team_id', team_id, 'source', source) order by seed), '[]'::jsonb)
      into v_rows from playoffs where season = p_season;
    if jsonb_array_length(v_rows) = 0 then
      return jsonb_build_object('ok', false, 'message', 'No seeds to undo.');
    end if;
    delete from playoffs where season = p_season;
    -- Paused on undo, or the next tick would flip it straight back.
    update playoff_autopilot set state = 'regular', enabled = false, switched_at = null, switched_by = null,
           army_navy_used = null, updated_at = now()
     where season = p_season;
    insert into playoff_autopilot_log (season, actor, action, detail)
    values (p_season, v_who, 'undo', jsonb_build_object('removed', v_rows));
    return jsonb_build_object('ok', true, 'message', 'Seeds removed, back to the regular season. Autopilot is paused.', 'removed', v_rows);
  end if;

  raise exception 'unknown action %', p_action;
end $$;
revoke all on function public.playoff_autopilot_admin(integer, text) from public, anon;
grant execute on function public.playoff_autopilot_admin(integer, text) to authenticated;

-- 8. Every 10 minutes. Cheap: a few hundred rows.
do $$
begin
  if exists (select 1 from pg_namespace where nspname = 'cron') then
    begin
      perform cron.unschedule('playoff-autopilot');
    exception when others then null;
    end;
    perform cron.schedule('playoff-autopilot', '*/10 * * * *', 'select public.playoff_autopilot_tick();');
  end if;
end $$;
