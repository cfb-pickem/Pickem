-- The last week with pool games, per season = conference championship week (2026-11-23).
-- 2026: conference championships are Sat Dec 5 = league week 14; week 15 (Army-Navy) has no pool games.
-- 2025 and earlier: week 15. Derived from the calendar exactly like slots_jackpot_odds_for's final_week
-- (calc_cfb_week of the first Saturday of December, capped at 15), so the jackpot ramp, the playoff
-- autopilot and the Python side (playoff_race.LAST_POOL_WEEK_BY_SEASON) agree.
--
-- playoff_compute: waits for the last POOL week (not a hardcoded 15) to be picked, kicked off, graded and
-- 12 hours settled, and only expects weeks 1..last to have games. Regular season is still weeks 0-15 and
-- the playoffs are still week 16 on (week 15 simply has no pool games in 2026). Non-destructive: one new
-- function + CREATE OR REPLACE of playoff_compute (otherwise identical to the live definition).

create or replace function public.last_pool_week(p_season integer)
returns integer language sql immutable set search_path = public as $$
  select least(15, public.calc_cfb_week(
    (make_date(p_season, 12, 1) + (6 - extract(dow from make_date(p_season, 12, 1))::int + 7) % 7)::timestamp))
$$;

CREATE OR REPLACE FUNCTION public.playoff_compute(p_season integer, p_force boolean DEFAULT false, p_now timestamp with time zone DEFAULT now())
 RETURNS jsonb
 LANGUAGE plpgsql
 STABLE SECURITY DEFINER
 SET search_path TO 'public'
AS $function$
declare
  v_holds      jsonb := '[]'::jsonb;
  v_notes      jsonb := '[]'::jsonb;
  v_phase      text;
  v_w15_n      int;
  v_w15_last   timestamptz;
  v_w15_kicked boolean;
  v_last       int := public.last_pool_week(p_season);
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
  select count(*) filter (where week = v_last),
         max(_po_kick("Start (CT)")) filter (where week = v_last),
         coalesce(bool_and(_po_kick("Start (CT)") <= p_now) filter (where week = v_last), false)
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
    from generate_series(1, v_last) w
   where not exists (select 1 from all_games g where g.cfb_season = p_season and g.week = w
                       and g.picked is true and coalesce(g.tiebreaker,false) = false);

  v_settle := v_w15_last + interval '12 hours';

  if v_w15_n = 0 or not v_w15_kicked then
    v_phase := 'regular';
    v_holds := v_holds || jsonb_build_object('code','season_in_progress',
      'message', case when v_w15_n = 0 then 'Week ' || v_last || ' games aren''t picked yet, so the regular season isn''t over.'
                      else 'Week ' || v_last || ' hasn''t finished kicking off yet.' end);
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
      'message', 'Waiting 12 hours after the last week ' || v_last || ' kickoff for late score fixes.', 'until', v_settle);
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
end $function$;
