-- Who has the ball, for games being played.
--
-- WHERE IT COMES FROM. Not the scoreboard: its `situation` object carries down,
-- distance, yard line and timeouts but no possession field for college football
-- - checked mid-game and it is simply absent. `situation.lastPlay.team` is
-- there, but that is whoever ran the last play, which is the WRONG team the
-- moment that play was a punt, a kickoff or a turnover.
--
-- The summary endpoint's `drives.current.team` is the team on offence right now,
-- which is the actual question. It costs one request per game being played, so
-- this only ever runs for games in progress - no live games, no requests.
--
-- Kept in its own function and its own cron job rather than folded into
-- sync_espn_scores. If ESPN changes this shape, the score and the clock carry on
-- working and only the little dot goes away.

alter table public.all_games
  add column if not exists possession text;

comment on column public.all_games.possession is
  '''home'' or ''away'' while a game is being played: which side has the ball. Null otherwise.';

create or replace function public.sync_possession()
returns table(checked int, updated int)
language plpgsql
security definer
set search_path = public, extensions
as $$
declare
  g record;
  body jsonb;
  drive_team text;
  home_id text;
  away_id text;
  side text;
  now_ct timestamp := (now() at time zone 'America/Chicago');
  n_checked int := 0;
  n_updated int := 0;
begin
  if not pg_try_advisory_xact_lock(hashtext('sync_possession')) then
    return query select 0, 0;
    return;
  end if;

  -- Nobody has the ball in a game that is not being played.
  update public.all_games
     set possession = null
   where possession is not null
     and (coalesce("Status", '') <> 'in_progress' or winner is not null);

  for g in
    select "GameId"
      from public.all_games
     where (picked is true or tiebreaker is true)
       and coalesce("Status", '') = 'in_progress'
       and winner is null
       and "Start (CT)" is not null
       and ("Start (CT)")::timestamp > now_ct - interval '6 hours'
     order by "Start (CT)"
     limit 25
  loop
    begin
      perform extensions.http_set_curlopt('CURLOPT_TIMEOUT_MS', '6000');
    exception when others then
      null;
    end;

    begin
      select content::jsonb into body
        from extensions.http_get(
          'https://site.api.espn.com/apis/site/v2/sports/football/college-football/summary?event='
          || g."GameId");
    exception when others then
      continue;
    end;

    if body is null then continue; end if;
    n_checked := n_checked + 1;

    drive_team := body #>> '{drives,current,team,id}';
    home_id := null; away_id := null;
    select
      max(c->>'id') filter (where c->>'homeAway' = 'home'),
      max(c->>'id') filter (where c->>'homeAway' = 'away')
      into home_id, away_id
      from jsonb_array_elements(coalesce(body #> '{header,competitions,0,competitors}', '[]'::jsonb)) c;

    side := case
              when drive_team is null then null
              when drive_team = home_id then 'home'
              when drive_team = away_id then 'away'
              else null
            end;

    update public.all_games
       set possession = side
     where "GameId" = g."GameId"
       and possession is distinct from side;

    if found then n_updated := n_updated + 1; end if;
  end loop;

  return query select n_checked, n_updated;
end;
$$;

revoke all on function public.sync_possession() from public, anon, authenticated;

do $$
begin
  perform cron.unschedule('sync-possession');
exception when others then
  null;
end $$;

select cron.schedule('sync-possession', '* * * * *', $job$select public.sync_possession()$job$);
