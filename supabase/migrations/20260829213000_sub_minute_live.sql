-- Sample three times a minute instead of once, while a game is being played.
--
-- Nothing was broken. Measured mid-game, both feeds were 37 seconds old and each
-- job finished in under half a second - the wait is entirely pg_cron's floor,
-- which is one minute. So the live line averaged 30 seconds behind ESPN and could
-- be 60, and the score with it: a touchdown could take most of a minute to reach
-- the board, which rather undercuts having it arrive with an animation.
--
-- pg_cron cannot be asked for less than a minute, so the extra passes go inside
-- the function: fetch, wait, fetch, wait, fetch. Average wait drops from ~30s to
-- ~10s, worst case from 60s to ~20s.
--
-- THREE THINGS KEEP THIS FROM BECOMING A PROBLEM.
--
--  * It only sleeps while something is actually in progress. With no live game
--    the first pass finds nothing and the function returns immediately, exactly
--    as before - no connection held, no requests made, all week long.
--
--  * A hard deadline. Every pass checks the clock before starting, and the
--    function will not begin work it cannot finish inside the minute, so runs
--    can never pile into one another. The advisory lock was already there as a
--    second guard.
--
--  * clock_timestamp(), not now(). This is the subtle one: now() is frozen for
--    the life of a transaction, so all three passes would stamp line_live_at and
--    SourceUpdatedAt with the SAME time - the row would look a minute stale the
--    moment it was written, and the page hides a live line it thinks is stale.
--    clock_timestamp() actually advances.

create or replace function public.sync_live_lines()
returns table(checked int, updated int)
language plpgsql
security definer
set search_path = public, extensions
as $$
declare
  g record;
  body jsonb;
  sp numeric;
  ou numeric;
  kind text;
  now_ct timestamp := (now() at time zone 'America/Chicago');
  deadline timestamptz := clock_timestamp() + interval '46 seconds';
  pass int;
  live_now int;
  n_checked int := 0;
  n_updated int := 0;
begin
  if not pg_try_advisory_xact_lock(hashtext('sync_live_lines')) then
    return query select 0, 0;
    return;
  end if;

  update public.all_games
     set line_live = null, line_live_ou = null, line_live_at = null, line_live_kind = null
   where line_live is not null
     and (coalesce("Status", '') = 'final' or winner is not null);

  for pass in 1..3 loop
    if pass > 1 then
      -- Only worth another pass if a game is actually being played. Between
      -- Saturdays this exits here and the function costs one indexed query.
      select count(*) into live_now
        from public.all_games
       where (picked is true or tiebreaker is true)
         and coalesce("Status", '') = 'in_progress'
         and winner is null;
      exit when live_now = 0;
      exit when clock_timestamp() + interval '20 seconds' > deadline;
      perform pg_sleep(17);
    end if;

    for g in
      select "GameId"
        from public.all_games
       where (picked is true or tiebreaker is true)
         and "Start (CT)" is not null
         and ("Start (CT)")::timestamp > now_ct - interval '6 hours'
         and ("Start (CT)")::timestamp < now_ct + interval '5 days'
         and coalesce("Status", '') <> 'final'
         and winner is null
         -- A game being played is repriced constantly and gets every pass. One
         -- that has not kicked off drifts slowly: it gets the first pass of one
         -- minute in five, for a fifteenth of the requests.
         and ( ("Start (CT)")::timestamp <= now_ct
               or (pass = 1 and (extract(minute from now_ct)::int % 5) = 0) )
       order by "Start (CT)"
       limit 25
    loop
      exit when clock_timestamp() > deadline;

      begin
        perform extensions.http_set_curlopt('CURLOPT_TIMEOUT_MS', '5000');
      exception when others then null;
      end;

      begin
        select content::jsonb into body
          from extensions.http_get(
            'https://sports.core.api.espn.com/v2/sports/football/leagues/college-football'
            || '/events/' || g."GameId" || '/competitions/' || g."GameId" || '/odds?limit=50');
      exception when others then
        continue;
      end;

      if body is null then continue; end if;
      n_checked := n_checked + 1;

      sp := null; ou := null; kind := null;
      begin
        select (item->>'spread')::numeric,
               (item->>'overUnder')::numeric,
               case when (item #>> '{provider,name}') ilike '%live%' then 'inplay' else 'market' end
          into sp, ou, kind
          from jsonb_array_elements(coalesce(body->'items', '[]'::jsonb)) item
         where item->>'spread' is not null
         order by case when (item #>> '{provider,name}') ilike '%live%' then 0 else 1 end,
                  item #>> '{provider,name}'
         limit 1;
      exception when others then
        sp := null;
      end;

      if sp is null then continue; end if;

      update public.all_games
         set line_live = sp, line_live_ou = ou, line_live_kind = kind,
             line_live_at = clock_timestamp()
       where "GameId" = g."GameId";

      if found then n_updated := n_updated + 1; end if;
    end loop;
  end loop;

  return query select n_checked, n_updated;
end;
$$;

revoke all on function public.sync_live_lines() from public, anon, authenticated;


-- The score carries the same floor, and it is the one the scoring animation
-- hangs off - a touchdown arriving forty seconds late is a touchdown nobody
-- watched happen. Same three passes, same guards. One scoreboard request covers
-- every game at once, so this is cheap.
create or replace function public.sync_espn_scores(
  p_days_back int default 1,
  p_days_ahead int default 1
)
returns table(checked int, updated int)
language plpgsql
security definer
set search_path = public, extensions
as $$
declare
  d date;
  body jsonb;
  ev jsonb;
  comp jsonb;
  side jsonb;
  gid bigint;
  st text;
  mapped text;
  per int;
  clk text;
  home_pts bigint;
  away_pts bigint;
  home_id text;
  away_id text;
  poss_id text;
  poss text;
  redzone boolean;
  deadline timestamptz := clock_timestamp() + interval '46 seconds';
  pass int;
  live_now int;
  n_checked int := 0;
  n_updated int := 0;
begin
  if not pg_try_advisory_xact_lock(hashtext('sync_espn_scores')) then
    return query select 0, 0;
    return;
  end if;

  for pass in 1..3 loop
    if pass > 1 then
      select count(*) into live_now
        from public.all_games
       where coalesce("Status", '') = 'in_progress';
      exit when live_now = 0;
      exit when clock_timestamp() + interval '20 seconds' > deadline;
      perform pg_sleep(17);
    end if;

    for d in
      select generate_series(current_date - p_days_back, current_date + p_days_ahead, interval '1 day')::date
    loop
      exit when clock_timestamp() > deadline;

      begin
        perform extensions.http_set_curlopt('CURLOPT_TIMEOUT_MS', '8000');
      exception when others then null;
      end;

      begin
        select content::jsonb into body
          from extensions.http_get(
            'https://site.api.espn.com/apis/site/v2/sports/football/college-football/scoreboard'
            || '?limit=400&groups=80&dates=' || to_char(d, 'YYYYMMDD'));
      exception when others then
        continue;
      end;

      if body is null then continue; end if;

      for ev in select * from jsonb_array_elements(coalesce(body->'events', '[]'::jsonb))
      loop
        begin
          gid := (ev->>'id')::bigint;
        exception when others then continue;
        end;

        comp := ev #> '{competitions,0}';
        if comp is null then continue; end if;

        st := lower(coalesce(ev #>> '{status,type,state}', ''));
        mapped := case st
                    when 'in'   then 'in_progress'
                    when 'post' then 'final'
                    when 'pre'  then 'scheduled'
                    else null
                  end;
        if mapped is null then continue; end if;

        per := nullif(ev #>> '{status,period}', '')::int;
        clk := ev #>> '{status,displayClock}';

        home_pts := null; away_pts := null; home_id := null; away_id := null;
        for side in select * from jsonb_array_elements(coalesce(comp->'competitors', '[]'::jsonb))
        loop
          begin
            if side->>'homeAway' = 'home' then
              home_pts := (side->>'score')::bigint;
              home_id := side->>'id';
            elsif side->>'homeAway' = 'away' then
              away_pts := (side->>'score')::bigint;
              away_id := side->>'id';
            end if;
          exception when others then null;
          end;
        end loop;

        poss := null;
        redzone := null;
        if mapped = 'in_progress' then
          begin
            poss_id := coalesce(comp #>> '{situation,lastPlay,end,team,id}',
                                comp #>> '{situation,lastPlay,team,id}');
            poss := case
                      when poss_id is null then null
                      when poss_id = home_id then 'home'
                      when poss_id = away_id then 'away'
                      else null
                    end;
          exception when others then
            poss := null;
          end;
          begin
            redzone := coalesce((comp #>> '{situation,isRedZone}')::boolean, false);
          exception when others then
            redzone := null;
          end;
        end if;

        n_checked := n_checked + 1;

        update public.all_games g
           set "Status"          = mapped,
               "Period"          = coalesce(per, g."Period"),
               "Clock"           = coalesce(clk, g."Clock"),
               "HomePts"         = coalesce(home_pts, g."HomePts"),
               "AwayPts"         = coalesce(away_pts, g."AwayPts"),
               possession        = poss,
               is_red_zone       = redzone,
               "SourceUpdatedAt" = clock_timestamp()
         where g."GameId" = gid
           and (g."Status"    is distinct from mapped
             or g."Period"    is distinct from coalesce(per, g."Period")
             or g."Clock"     is distinct from coalesce(clk, g."Clock")
             or g."HomePts"   is distinct from coalesce(home_pts, g."HomePts")
             or g."AwayPts"   is distinct from coalesce(away_pts, g."AwayPts")
             or g.possession  is distinct from poss
             or g.is_red_zone is distinct from redzone);

        if found then n_updated := n_updated + 1; end if;
      end loop;
    end loop;
  end loop;

  return query select n_checked, n_updated;
end;
$$;

revoke all on function public.sync_espn_scores(int, int) from public, anon, authenticated;
