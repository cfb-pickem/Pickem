-- Three samples a minute that are actually VISIBLE a minute.
--
-- The previous attempt put three passes inside one function and it did not work,
-- for a reason worth writing down: pg_cron runs a job as a single transaction, so
-- nothing any pass wrote was visible to anyone until the whole thing committed.
-- Measured, the row still only changed once every sixty seconds - the data was
-- fresher, the board could not see it. Sleeping inside a transaction buys
-- nothing.
--
-- So the passes become separate jobs instead. Same function, an optional delay,
-- three schedules per feed at 0, 20 and 40 seconds. Each is its own transaction
-- and commits the moment it is done, which is the whole point. Worst case drops
-- from 60 seconds to about 20, average from 30 to 10.
--
-- TWO THINGS THIS GETS RIGHT THAT THE LAST ONE DID NOT.
--
--  * The advisory lock is taken AFTER the delay, not before. Taken first, the
--    job that sleeps forty seconds would hold the lock for those forty seconds
--    and lock out the other two - all three would collapse back into one.
--
--  * The delay is skipped entirely when nothing is being played, so between
--    Saturdays the extra jobs cost one indexed query each and hold no connection.
--
-- Three small jobs rather than one clever one is also the more robust shape: if
-- one fails, the other two still carry the board.

create or replace function public.sync_live_lines(p_delay_seconds int default 0)
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
  now_ct timestamp;
  live_now int;
  n_checked int := 0;
  n_updated int := 0;
begin
  -- Wait first, and only if there is a game to wait for. Nothing live means the
  -- delayed passes cost a single count and return.
  if p_delay_seconds > 0 then
    select count(*) into live_now
      from public.all_games
     where (picked is true or tiebreaker is true)
       and coalesce("Status", '') = 'in_progress'
       and winner is null;
    if live_now = 0 then
      return query select 0, 0;
      return;
    end if;
    perform pg_sleep(least(p_delay_seconds, 50));
  end if;

  -- After the wait, never before: holding this across a forty-second sleep would
  -- shut out the other two passes and undo the whole exercise.
  if not pg_try_advisory_xact_lock(hashtext('sync_live_lines' || p_delay_seconds::text)) then
    return query select 0, 0;
    return;
  end if;

  now_ct := (clock_timestamp() at time zone 'America/Chicago');

  update public.all_games
     set line_live = null, line_live_ou = null, line_live_at = null, line_live_kind = null
   where line_live is not null
     and (coalesce("Status", '') = 'final' or winner is not null);

  for g in
    select "GameId"
      from public.all_games
     where (picked is true or tiebreaker is true)
       and "Start (CT)" is not null
       and ("Start (CT)")::timestamp > now_ct - interval '6 hours'
       and ("Start (CT)")::timestamp < now_ct + interval '5 days'
       and coalesce("Status", '') <> 'final'
       and winner is null
       -- A game being played is repriced constantly and gets all three passes.
       -- One that has not kicked off drifts slowly: it gets the on-the-minute
       -- pass, one minute in five.
       and ( ("Start (CT)")::timestamp <= now_ct
             or (p_delay_seconds = 0 and (extract(minute from now_ct)::int % 5) = 0) )
     order by "Start (CT)"
     limit 25
  loop
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

    -- clock_timestamp(), not now(): now() is frozen at the start of the
    -- transaction, which for a delayed pass is up to forty seconds ago. The row
    -- would be stamped stale on arrival and the page would hide it.
    update public.all_games
       set line_live = sp, line_live_ou = ou, line_live_kind = kind,
           line_live_at = clock_timestamp()
     where "GameId" = g."GameId";

    if found then n_updated := n_updated + 1; end if;
  end loop;

  return query select n_checked, n_updated;
end;
$$;

revoke all on function public.sync_live_lines(int) from public, anon, authenticated;
drop function if exists public.sync_live_lines();


create or replace function public.sync_espn_scores(
  p_days_back int default 1,
  p_days_ahead int default 1,
  p_delay_seconds int default 0
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
  live_now int;
  n_checked int := 0;
  n_updated int := 0;
begin
  if p_delay_seconds > 0 then
    select count(*) into live_now
      from public.all_games where coalesce("Status", '') = 'in_progress';
    if live_now = 0 then
      return query select 0, 0;
      return;
    end if;
    perform pg_sleep(least(p_delay_seconds, 50));
  end if;

  if not pg_try_advisory_xact_lock(hashtext('sync_espn_scores' || p_delay_seconds::text)) then
    return query select 0, 0;
    return;
  end if;

  -- A delayed pass only needs today; the day either side is for catching a game
  -- that has rolled over midnight, and the on-the-minute pass covers that.
  for d in
    select generate_series(
             current_date - case when p_delay_seconds > 0 then 0 else p_days_back end,
             current_date + case when p_delay_seconds > 0 then 0 else p_days_ahead end,
             interval '1 day')::date
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

  return query select n_checked, n_updated;
end;
$$;

revoke all on function public.sync_espn_scores(int, int, int) from public, anon, authenticated;
drop function if exists public.sync_espn_scores(int, int);

do $$
declare nm text;
begin
  foreach nm in array array['sync-live-lines-20','sync-live-lines-40',
                            'sync-espn-scores-20','sync-espn-scores-40'] loop
    begin
      perform cron.unschedule(nm);
    exception when others then null;
    end;
  end loop;
end $$;

select cron.schedule('sync-live-lines-20',  '* * * * *', $j$select public.sync_live_lines(20)$j$);
select cron.schedule('sync-live-lines-40',  '* * * * *', $j$select public.sync_live_lines(40)$j$);
select cron.schedule('sync-espn-scores-20', '* * * * *', $j$select public.sync_espn_scores(1, 1, 20)$j$);
select cron.schedule('sync-espn-scores-40', '* * * * *', $j$select public.sync_espn_scores(1, 1, 40)$j$);
