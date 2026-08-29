-- One request carries the whole live state: score, clock, possession, red zone.
--
-- WHAT THIS REPLACES. sync_possession() fetched the summary endpoint once per
-- live game, on its own cron job, purely to read `drives.current.team`. That was
-- one extra request a minute per game, a second function and a second schedule -
-- three more things that can break - for one dot.
--
-- The scoreboard already being fetched for the score carries it too, in
-- `situation.lastPlay.end.team`: the side in possession AT THE END of the play.
-- That last word is the whole reason this works. `lastPlay.team` is whoever RAN
-- the play, which is the wrong team the moment it was a punt, a kickoff or an
-- interception; `end.team` is who has the ball now. Same request, same accuracy,
-- none of the machinery.
--
-- RED ZONE comes from `situation.isRedZone` on that same object. Checked against
-- live play: yardLine 82 reported true, yardLine 79 reported false, so the flag
-- and the yard line agree with each other and with the twenty-yard rule. It is a
-- property of the drive - "whoever has the ball is inside the twenty" - so it is
-- read together with possession rather than being a fact about a team.
--
-- NOTHING HERE MAY TAKE THE SCORE DOWN WITH IT. Every one of these reads is
-- wrapped so a renamed or missing field yields null instead of raising: lose
-- possession and the dot goes away, lose the red zone and it stops glowing, and
-- the score and the clock carry on regardless.

alter table public.all_games
  add column if not exists is_red_zone boolean;

comment on column public.all_games.is_red_zone is
  'True while the side in possession is inside the opponent''s twenty. Read with possession, which says whose drive it is.';

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

      -- Possession and the red zone, both only while the game is being played.
      -- Anything else clears them, or a cell keeps a dot lit long after the
      -- drive - or the game - has ended.
      poss := null;
      redzone := null;
      if mapped = 'in_progress' then
        begin
          -- end.team is who has the ball after the play; team is only who ran
          -- it. Falling back to the latter is better than nothing on the odd
          -- play that carries no end block.
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
             "SourceUpdatedAt" = now()
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

revoke all on function public.sync_espn_scores(int, int) from public, anon, authenticated;

-- The second request, the second function and the second schedule all go.
do $$
begin
  perform cron.unschedule('sync-possession');
exception when others then
  null;
end $$;

drop function if exists public.sync_possession();
