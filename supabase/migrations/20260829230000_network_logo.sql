-- The channel as its mark rather than its name.
--
-- ESPN already returns it on the same scoreboard request sync_espn_scores() is
-- making anyway - competitions[0].geoBroadcasts[].media.logo - so this costs no
-- extra call. It is not returned for every network: measured over one Saturday's
-- slate, ESPN publishes a logo for its own family and a few partners (ESPN,
-- ESPN+, ACC Network, SEC Network, CW, Disney+) and none at all for FOX, NBC or
-- CBSSN. The board therefore has to keep the wordmark and use it wherever the
-- logo is absent, which is why this column is additive and nothing depends on
-- it being filled.
--
-- TWO THINGS WORTH KNOWING ABOUT THE PICK.
--
--  * A game can carry several broadcasts - UNC/TCU had ESPN on television and
--    Disney+ streaming it. Television first, then a national feed over a
--    regional one, which is the order somebody looking for the channel wants.
--
--  * coalesce on the way in, never a straight assignment. ESPN drops
--    geoBroadcasts once a game is over, so writing the extracted value
--    unconditionally would blank the logo at the final whistle - exactly when
--    the column is still on screen. Once found it stays.

alter table public.all_games add column if not exists network_logo text;

comment on column public.all_games.network_logo is
  'Broadcaster logo URL from ESPN geoBroadcasts. Absent for networks ESPN does not publish a mark for; the board falls back to the Network wordmark.';

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
  net_logo text;
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

      -- Television before streaming, national before regional. Null whenever
      -- ESPN has no mark for the channel, which is common and is fine.
      net_logo := null;
      begin
        select b #>> '{media,logo}' into net_logo
          from jsonb_array_elements(coalesce(comp->'geoBroadcasts', '[]'::jsonb)) b
         where nullif(b #>> '{media,logo}', '') is not null
         order by case when b #>> '{type,shortName}' = 'TV' then 0 else 1 end,
                  case when b #>> '{market,type}' = 'National' then 0 else 1 end
         limit 1;
      exception when others then
        net_logo := null;
      end;

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
             network_logo      = coalesce(net_logo, g.network_logo),
             "SourceUpdatedAt" = clock_timestamp()
       where g."GameId" = gid
         and (g."Status"       is distinct from mapped
           or g."Period"       is distinct from coalesce(per, g."Period")
           or g."Clock"        is distinct from coalesce(clk, g."Clock")
           or g."HomePts"      is distinct from coalesce(home_pts, g."HomePts")
           or g."AwayPts"      is distinct from coalesce(away_pts, g."AwayPts")
           or g.possession     is distinct from poss
           or g.is_red_zone    is distinct from redzone
           or g.network_logo   is distinct from coalesce(net_logo, g.network_logo));

      if found then n_updated := n_updated + 1; end if;
    end loop;
  end loop;

  return query select n_checked, n_updated;
end;
$$;

revoke all on function public.sync_espn_scores(int, int, int) from public, anon, authenticated;
