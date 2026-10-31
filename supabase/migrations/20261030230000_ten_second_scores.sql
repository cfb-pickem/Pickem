-- Scores every ten seconds during games, without holding a connection to wait.
--
-- WHERE THE LAG WAS. Measured on a live Friday night: scores reached the board
-- ~35s behind ESPN on average and ~75s at worst. Almost all of it was the wait
-- for the once-a-minute sync_espn_scores() run. The browser's 8s poll and
-- ESPN's own 7s cache were the small parts.
--
-- WHY NOT JUST RUN THE SQL MORE OFTEN. 20260829220000 tried exactly that and
-- reverted it: a delayed pg_cron pass sleeps holding a Postgres connection, four
-- of them starved the pool PostgREST serves the site from, and page load during
-- a game went from 3s to 9.6s. The lesson recorded there is that beating the
-- one-minute floor must not cost a held connection.
--
-- SO THE WAITING MOVES OUT OF POSTGRES. A pg_cron job fires the `live-scores`
-- edge function every minute through pg_net, which is asynchronous - the job
-- returns at once and holds nothing. The function does the sleeping and the
-- ESPN fetching in Deno, at :10 :20 :30 :40 :50, and hands each scoreboard to
-- apply_espn_scoreboard(), which is the parsing and the UPDATE that used to sit
-- inside sync_espn_scores(), lifted out unchanged in substance. A pass holds a
-- connection for the few milliseconds of that UPDATE and nothing else. Outside
-- a game window the function asks once whether anything is live, hears no, and
-- exits.
--
-- sync_espn_scores() keeps its on-the-minute job and stays the floor: if the
-- edge function fails in any way, the board is exactly as fresh as it was
-- before this migration, never less. That is the opening-day lesson - the feed
-- that 502'd was the only feed.
--
-- ALSO HERE:
--   * While games are live, sync_espn_scores() fetches only the dates that
--     have live games (ESPN dates are Eastern), not yesterday/today/tomorrow -
--     one ESPN call a minute instead of three. A full three-day sweep still
--     runs every tenth minute, and whenever nothing is live.
--   * status_name: ESPN's own name for the state (STATUS_HALFTIME,
--     STATUS_END_PERIOD, STATUS_DELAYED, ...). The board used to infer halftime
--     and end-of-quarter from a 0:00 clock, which works until overtime - college
--     OT has no clock, ESPN sends 0:00 for every OT snap, and a live OT game read
--     "END OT" throughout.
--   * The clock is normalised to M:SS on the way in. ESPN has been seen sending
--     "08:12", "0:07.3" and bare seconds.
--   * Postponed and canceled games no longer map to 'final'. ESPN gives them
--     state 'post', which this feed turned into 'final', and set_winner_on_final()
--     would then award a picked game on a 0-0 "score" - to whichever side the
--     line happened to favour.
--   * all_games joins the realtime publication, so the board can repaint the
--     moment a row changes instead of on its next poll.

-- ---------------------------------------------------------------------------
-- status_name
-- ---------------------------------------------------------------------------

alter table public.all_games add column if not exists status_name text;

comment on column public.all_games.status_name is
  'ESPN status.type.name for the live state (STATUS_HALFTIME, STATUS_END_PERIOD, STATUS_DELAYED, ...). Display only; Status is what logic reads.';

-- ---------------------------------------------------------------------------
-- Clock normalisation
-- ---------------------------------------------------------------------------

create or replace function public.espn_clean_clock(p text)
returns text
language plpgsql
immutable
set search_path = public
as $$
declare
  s text := btrim(coalesce(p, ''));
  m text[];
begin
  if s = '' then return null; end if;
  -- "12:05", "08:12", "0:07.3"
  m := regexp_match(s, '^(\d{1,2}):(\d{1,2})(?:\.\d+)?$');
  if m is not null then
    return (m[1])::int::text || ':' || lpad(m[2], 2, '0');
  end if;
  -- "45", "7.3" - under a minute, sent as bare seconds
  m := regexp_match(s, '^(\d{1,2})(?:\.\d+)?$');
  if m is not null then
    return '0:' || lpad(m[1], 2, '0');
  end if;
  return s;
end;
$$;

-- ---------------------------------------------------------------------------
-- apply_espn_scoreboard: one ESPN scoreboard response in, rows updated out.
-- The body of the old sync_espn_scores() loop, plus status_name, the clock
-- clean-up and the postponed/canceled mapping. Comments on the individual
-- fields are in 20260911180000 and still hold.
-- ---------------------------------------------------------------------------

create or replace function public.apply_espn_scoreboard(body jsonb)
returns table(checked int, updated int)
language plpgsql
security definer
set search_path = public, extensions
as $function$
declare
  ev jsonb;
  comp jsonb;
  side jsonb;
  gid bigint;
  st text;
  sname text;
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
  n_checked int := 0;
  n_updated int := 0;
begin
  if body is null then
    return query select 0, 0;
    return;
  end if;

  for ev in select * from jsonb_array_elements(coalesce(body->'events', '[]'::jsonb))
  loop
    begin
      gid := (ev->>'id')::bigint;
    exception when others then continue;
    end;

    comp := ev #> '{competitions,0}';
    if comp is null then continue; end if;

    st := lower(coalesce(ev #>> '{status,type,state}', ''));
    sname := nullif(upper(coalesce(ev #>> '{status,type,name}', '')), '');
    mapped := case
                when sname = 'STATUS_POSTPONED' then 'postponed'
                when sname in ('STATUS_CANCELED', 'STATUS_CANCELLED') then 'canceled'
                when st = 'in'   then 'in_progress'
                when st = 'post' then 'final'
                when st = 'pre'  then 'scheduled'
                else null
              end;
    if mapped is null then continue; end if;

    per := nullif(ev #>> '{status,period}', '')::int;
    clk := public.espn_clean_clock(ev #>> '{status,displayClock}');

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

    -- A game that never happened has no score, whatever zeros ESPN prints.
    if mapped in ('postponed', 'canceled') then
      home_pts := null; away_pts := null;
    end if;

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
    if espn_link is null then
      espn_link := nullif(ev->>'link', '');
    end if;

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

    update public.all_games g
       set "Status"          = mapped,
           status_name       = sname,
           "Period"          = coalesce(per, g."Period"),
           "Clock"           = coalesce(clk, g."Clock"),
           "HomePts"         = case when mapped in ('postponed', 'canceled') then null
                                    else coalesce(home_pts, g."HomePts") end,
           "AwayPts"         = case when mapped in ('postponed', 'canceled') then null
                                    else coalesce(away_pts, g."AwayPts") end,
           "Network"         = coalesce(net_name, g."Network"),
           espn_url          = coalesce(espn_link, g.espn_url),
           possession        = poss,
           is_red_zone       = redzone,
           network_logo      = coalesce(net_logo, g.network_logo),
           "SourceUpdatedAt" = clock_timestamp()
     where g."GameId" = gid
       and (g."Status"       is distinct from mapped
         or g.status_name    is distinct from sname
         or g."Period"       is distinct from coalesce(per, g."Period")
         or g."Clock"        is distinct from coalesce(clk, g."Clock")
         or g."HomePts"      is distinct from (case when mapped in ('postponed', 'canceled') then null
                                                    else coalesce(home_pts, g."HomePts") end)
         or g."AwayPts"      is distinct from (case when mapped in ('postponed', 'canceled') then null
                                                    else coalesce(away_pts, g."AwayPts") end)
         or g."Network"      is distinct from coalesce(net_name, g."Network")
         or g.espn_url       is distinct from coalesce(espn_link, g.espn_url)
         or g.possession     is distinct from poss
         or g.is_red_zone    is distinct from redzone
         or g.network_logo   is distinct from coalesce(net_logo, g.network_logo));

    if found then n_updated := n_updated + 1; end if;
  end loop;

  return query select n_checked, n_updated;
end;
$function$;

comment on function public.apply_espn_scoreboard(jsonb) is
  'Parses one ESPN college-football scoreboard response into all_games. Called by sync_espn_scores() (every minute) and the live-scores edge function (every 10s during games).';

-- ---------------------------------------------------------------------------
-- live_feed_dates: the ESPN dates (Eastern, YYYYMMDD) worth fetching right now.
-- A game is worth it if ESPN says it is being played, or it kicked off in the
-- last six hours and has not finished, or it kicks off in the next ten minutes
-- (so the flip to in_progress is caught on the fast feed). Empty = nothing live.
-- ---------------------------------------------------------------------------

create or replace function public.live_feed_dates()
returns text[]
language sql
stable
security definer
set search_path = public
as $$
  select coalesce(array_agg(distinct d order by d), '{}')
    from (
      select to_char(("Start (CT)" at time zone 'America/Chicago') at time zone 'America/New_York',
                     'YYYYMMDD') as d
        from public.all_games
       where "Status" = 'in_progress'
          or ("Start (CT)" at time zone 'America/Chicago'
                between now() - interval '6 hours' and now() + interval '10 minutes'
              and coalesce("Status", '') not in ('final', 'postponed', 'canceled'))
    ) x;
$$;

-- ---------------------------------------------------------------------------
-- live_feed_claim: lets one edge-function run per minute do the work, however
-- many times the endpoint is called. The anon key is public, so the endpoint is
-- callable by anyone; this is what keeps that from meaning anything.
-- ---------------------------------------------------------------------------

create table if not exists public.live_feed_state (
  id int primary key default 1 check (id = 1),
  claimed_at timestamptz not null default 'epoch'
);
insert into public.live_feed_state (id) values (1) on conflict do nothing;
alter table public.live_feed_state enable row level security;   -- no policies: service role only

create or replace function public.live_feed_claim()
returns boolean
language plpgsql
security definer
set search_path = public
as $$
begin
  update public.live_feed_state
     set claimed_at = now()
   where id = 1 and claimed_at < now() - interval '50 seconds';
  return found;
end;
$$;

-- ---------------------------------------------------------------------------
-- sync_espn_scores: same signature (the cron job calls it), now a fetcher only.
-- ---------------------------------------------------------------------------

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
  live_dates text[];
  dates text[];
  d text;
  body jsonb;
  r record;
  n_checked int := 0;
  n_updated int := 0;
begin
  -- p_delay_seconds is kept for the signature only; nothing schedules a
  -- delayed pass (20260829220000) and the edge function is how sub-minute
  -- freshness is done now.
  if not pg_try_advisory_xact_lock(hashtext('sync_espn_scores0')) then
    return query select 0, 0;
    return;
  end if;

  live_dates := public.live_feed_dates();

  if cardinality(live_dates) > 0 and extract(minute from now())::int % 10 <> 0 then
    dates := live_dates;
  else
    select array_agg(to_char(x::date, 'YYYYMMDD') order by x) into dates
      from generate_series(current_date - p_days_back, current_date + p_days_ahead,
                           interval '1 day') x;
  end if;

  foreach d in array dates loop
    begin
      perform extensions.http_set_curlopt('CURLOPT_TIMEOUT_MS', '8000');
    exception when others then null;
    end;

    body := null;
    begin
      select content::jsonb into body
        from extensions.http_get(
          'https://site.api.espn.com/apis/site/v2/sports/football/college-football/scoreboard'
          || '?limit=400&groups=80&dates=' || d);
    exception when others then
      continue;
    end;

    if body is null then continue; end if;

    select * into r from public.apply_espn_scoreboard(body);
    n_checked := n_checked + coalesce(r.checked, 0);
    n_updated := n_updated + coalesce(r.updated, 0);
  end loop;

  return query select n_checked, n_updated;
end;
$function$;

-- ---------------------------------------------------------------------------
-- Who may call what. Supabase grants EXECUTE on new functions to anon and
-- authenticated by default; apply_espn_scoreboard would let anyone write a
-- score, so all of these are service-role only.
-- ---------------------------------------------------------------------------

revoke all on function public.apply_espn_scoreboard(jsonb) from public, anon, authenticated;
revoke all on function public.live_feed_dates() from public, anon, authenticated;
revoke all on function public.live_feed_claim() from public, anon, authenticated;
revoke all on function public.sync_espn_scores(int, int, int) from public, anon, authenticated;
grant execute on function public.apply_espn_scoreboard(jsonb) to service_role;
grant execute on function public.live_feed_dates() to service_role;
grant execute on function public.live_feed_claim() to service_role;
grant execute on function public.sync_espn_scores(int, int, int) to service_role;

-- ---------------------------------------------------------------------------
-- Realtime: the board subscribes to UPDATEs and repaints at once. RLS still
-- applies to what a subscriber receives (all_games_read: anon, authenticated).
-- ---------------------------------------------------------------------------

do $$
begin
  alter publication supabase_realtime add table public.all_games;
exception when duplicate_object then null;
end $$;

-- ---------------------------------------------------------------------------
-- The trigger for the edge function. pg_net queues the request and returns, so
-- this job holds no connection while the function runs. The bearer is the
-- public anon key (it is in every page of the site); the function itself uses
-- the service role from its own environment.
-- ---------------------------------------------------------------------------

do $$
begin
  perform cron.unschedule('live-scores-edge');
exception when others then null;
end $$;

select cron.schedule(
  'live-scores-edge',
  '* * * * *',
  $cmd$
  select net.http_post(
    url := 'https://vopdioszofwdkwnujtiq.supabase.co/functions/v1/live-scores',
    headers := jsonb_build_object(
      'Content-Type', 'application/json',
      'Authorization', 'Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InZvcGRpb3N6b2Z3ZGt3bnVqdGlxIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NjE1ODI2MjksImV4cCI6MjA3NzE1ODYyOX0.cD2nNYMEUUOHWQlQC0-lxGZ3s1HVQhWEX_FmgzSsZYw'),
    body := '{}'::jsonb,
    timeout_milliseconds := 5000)
  $cmd$
);
