-- Give sync_espn_scores() the two columns it was never handed.
--
-- WHAT WENT WRONG. On the morning of the 2026 opener the update-final-scores
-- edge function began returning 502 on every call, and 20260829204500 retired it
-- in favour of sync_espn_scores(), which does the same work from inside Postgres
-- and more besides. That migration says "nothing is lost by stopping the old
-- one". Three columns were.
--
-- The edge function owned Status, Period, Clock, Network, espn_url and
-- FinalizedAt. sync_espn_scores() picked up the first three and network_logo,
-- but not the last three - so from the moment the old feed was switched off,
-- no new game has ever been given a channel name or a link to its matchup.
--
-- It stayed invisible for a fortnight because the games that already HAD those
-- values kept them: weeks 0 and 1 were written before the changeover. Week 2 is
-- the first slate that is entirely after it, and it went up with no ESPN links
-- and no channel names at all - 0 of 85 scheduled games carried either.
--
-- The front end does not break on this, which is the other reason it went
-- unnoticed. index.html builds the game header as a plain <div> instead of an
-- <a> when espn_url is missing (see `hasUrl`), and paintNetwork() falls back to
-- a non-breaking space so the columns still line up. It just quietly stops being
-- a link, and the channel strip shows the mark with no name beside it.
--
-- FinalizedAt is deliberately NOT restored. Nothing reads it - not the front
-- end, not a function, not a migration - so it is a dead column and reviving it
-- would be reviving work for nobody.
--
-- The alternative fix was `select cron.alter_job(1, active := true)` to bring
-- the edge function back. That is one statement and it is the wrong one: it
-- restores the thing that was 502ing every minute and took the scoreboard down
-- mid-game. The feed that works should carry the columns.

create or replace function public.sync_espn_scores(
  p_days_back int default 1,
  p_days_ahead int default 1,
  p_delay_seconds int default 0
)
returns table(checked int, updated int)
language plpgsql
security definer
set search_path = public, extensions
as $function$
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
  net_name text;
  espn_link text;
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

      -- THE LINK TO THE MATCHUP. Every event carries one, on the link whose
      -- `rel` array contains 'summary' - checked against a full 80-game Saturday,
      -- where all 80 had exactly that.
      --
      -- Written with jsonb_array_elements_text rather than the `?` containment
      -- operator on purpose: `?` is a parameter placeholder to a good many
      -- Postgres clients, and this file has to survive being pasted into one.
      espn_link := null;
      begin
        select l->>'href' into espn_link
          from jsonb_array_elements(coalesce(ev->'links', '[]'::jsonb)) l
         where exists (
                 select 1 from jsonb_array_elements_text(coalesce(l->'rel', '[]'::jsonb)) r
                  where r = 'summary')
           and nullif(l->>'href', '') is not null
         limit 1;
      exception when others then
        espn_link := null;
      end;
      -- ESPN sometimes puts a bare link on the event instead.
      if espn_link is null then
        espn_link := nullif(ev->>'link', '');
      end if;

      -- THE CHANNEL, as a name. network_logo below is the mark; this is the word
      -- beside it, and paintNetwork() needs both - it uses the name for the
      -- tooltip, for the abbreviation, and to size the wordmark.
      --
      -- Television before streaming and national before regional, matching the
      -- order the logo is chosen in, so the mark and the name can never come
      -- from two different broadcasters.
      net_name := null;
      begin
        select coalesce(nullif(b #>> '{media,shortName}', ''),
                        nullif(b->>'shortName', '')) into net_name
          from jsonb_array_elements(
                 coalesce(nullif(comp->'geoBroadcasts', '[]'::jsonb),
                          comp->'broadcasts')) b
         where coalesce(nullif(b #>> '{media,shortName}', ''),
                        nullif(b->>'shortName', '')) is not null
         order by case when b #>> '{type,shortName}' = 'TV' then 0 else 1 end,
                  case when b #>> '{market,type}' = 'National' then 0 else 1 end
         limit 1;
      exception when others then
        net_name := null;
      end;

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

      -- The two new columns are COALESCED onto what is already there, like
      -- network_logo and unlike Status. ESPN drops the broadcast off a finished
      -- game and can answer a scoreboard request without the links array at all;
      -- either would otherwise blank a good value on a page that had been
      -- showing it all week. A link, once known, does not stop being true.
      update public.all_games g
         set "Status"          = mapped,
             "Period"          = coalesce(per, g."Period"),
             "Clock"           = coalesce(clk, g."Clock"),
             "HomePts"         = coalesce(home_pts, g."HomePts"),
             "AwayPts"         = coalesce(away_pts, g."AwayPts"),
             "Network"         = coalesce(net_name, g."Network"),
             espn_url          = coalesce(espn_link, g.espn_url),
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
           or g."Network"      is distinct from coalesce(net_name, g."Network")
           or g.espn_url       is distinct from coalesce(espn_link, g.espn_url)
           or g.possession     is distinct from poss
           or g.is_red_zone    is distinct from redzone
           or g.network_logo   is distinct from coalesce(net_logo, g.network_logo));

      if found then n_updated := n_updated + 1; end if;
    end loop;
  end loop;

  return query select n_checked, n_updated;
end;
$function$;

comment on function public.sync_espn_scores(int, int, int) is
  'The one feed for the live state: status, clock, score, possession, red zone, channel name and mark, and the link to the matchup.';
