-- Live scores, pulled by the database instead of by an edge function.
--
-- WHY THIS EXISTS. On the morning of the 2026 opener the score feed stopped.
-- "Update from ESPN" (cron job 1) ran every minute and every minute came back
-- 502 "ESPN fetch failed" from the edge function itself, so all_games kept
-- saying `Status = 'scheduled'` with SourceUpdatedAt four days old while ESPN
-- had the game in the first quarter. No score, no clock, and no cover shading -
-- everything live on the board hangs off these columns.
--
-- The edge function is deployed rather than versioned here, so it cannot be
-- fixed from this repo. It does not need to be: three sync functions already
-- run this way - sync_espn_lines, sync_espn_fpi, sync_live_lines - all of them
-- reaching the same ESPN host from inside Postgres through the http extension,
-- and all of them were succeeding at the moment the edge function was not. That
-- is the difference that matters, so the scoreboard moves to the same footing.
--
-- Deliberately left alongside job 1 rather than replacing it. Both write the
-- same columns from the same source, whichever is healthy wins, and the page
-- reads either spelling of the in-progress status. During a live game the
-- smaller change is the safer one.

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
  n_checked int := 0;
  n_updated int := 0;
begin
  if not pg_try_advisory_xact_lock(hashtext('sync_espn_scores')) then
    return query select 0, 0;
    return;
  end if;

  for d in
    select generate_series(current_date - p_days_back, current_date + p_days_ahead, interval '1 day')::date
  loop
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
      continue;                        -- one bad day must not abort the run
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

      -- ESPN's own vocabulary is pre / in / post. Mapped to the words this
      -- database has always used, so nothing downstream has to learn a new one.
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

      home_pts := null;
      away_pts := null;
      for side in select * from jsonb_array_elements(coalesce(comp->'competitors', '[]'::jsonb))
      loop
        begin
          if side->>'homeAway' = 'home' then home_pts := (side->>'score')::bigint;
          elsif side->>'homeAway' = 'away' then away_pts := (side->>'score')::bigint;
          end if;
        exception when others then null;   -- a missing score is not a reason to skip the clock
        end;
      end loop;

      n_checked := n_checked + 1;

      -- `is distinct from` throughout: this table has a history of write
      -- amplification, and a row that already agrees must not be rewritten every
      -- minute for four hours.
      update public.all_games g
         set "Status"          = mapped,
             "Period"          = coalesce(per, g."Period"),
             "Clock"           = coalesce(clk, g."Clock"),
             "HomePts"         = coalesce(home_pts, g."HomePts"),
             "AwayPts"         = coalesce(away_pts, g."AwayPts"),
             "SourceUpdatedAt" = now()
       where g."GameId" = gid
         and (g."Status"  is distinct from mapped
           or g."Period"  is distinct from coalesce(per, g."Period")
           or g."Clock"   is distinct from coalesce(clk, g."Clock")
           or g."HomePts" is distinct from coalesce(home_pts, g."HomePts")
           or g."AwayPts" is distinct from coalesce(away_pts, g."AwayPts"));

      if found then n_updated := n_updated + 1; end if;
    end loop;
  end loop;

  return query select n_checked, n_updated;
end;
$$;

revoke all on function public.sync_espn_scores(int, int) from public, anon, authenticated;

do $$
begin
  perform cron.unschedule('sync-espn-scores');
exception when others then null;
end $$;

select cron.schedule('sync-espn-scores', '* * * * *', $job$select public.sync_espn_scores()$job$);
