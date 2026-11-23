-- Minimal mirror of the prod objects playoff_autopilot touches (types from information_schema).
do $$ begin
  if not exists (select 1 from pg_roles where rolname='anon') then create role anon nologin; end if;
  if not exists (select 1 from pg_roles where rolname='authenticated') then create role authenticated nologin; end if;
  if not exists (select 1 from pg_roles where rolname='service_role') then create role service_role nologin; end if;
end $$;
create schema if not exists auth;
create or replace function auth.uid() returns uuid language sql stable as $$ select nullif(current_setting('request.jwt.claim.sub', true), '')::uuid $$;
create or replace function auth.role() returns text language sql stable as $$ select nullif(current_setting('request.jwt.claim.role', true), '') $$;
grant usage on schema auth to anon, authenticated, service_role;
grant usage on schema public to anon, authenticated, service_role;
create table public.teams (team_id bigint primary key, team_name text, user_id uuid, commissioner boolean default false, active boolean not null default true, is_exhibition boolean not null default false);
create table public.all_games ("GameId" bigint primary key, "Start (CT)" timestamp, "Away" text, "Home" text, "AwayPts" int, "HomePts" int, "Status" text, line numeric, winner text, picked boolean, week integer, cfb_season integer, tiebreaker boolean);
create table public.picks (id bigserial primary key, team_id bigint, game_id bigint, pick text, by_slots boolean default false, jackpot boolean default false);
create table public.playoffs (id bigserial primary key, team_id integer not null references public.teams(team_id) on delete cascade, season integer not null, seed integer not null check (seed between 1 and 4), created_at timestamptz not null default now(), unique (season, seed), unique (season, team_id));
create table public.blackjack_bankrolls (team_id bigint, cfb_season integer, chips integer, hands_played integer, busted boolean);
create or replace function public.is_commissioner() returns boolean language sql stable security definer set search_path = public as $$
  select exists (select 1 from public.teams t where t.user_id = auth.uid() and t.commissioner is true); $$;
grant select on all tables in schema public to anon, authenticated;

-- calc_cfb_week (prod copy) for last_pool_week()
CREATE OR REPLACE FUNCTION public.calc_cfb_week(start_ct timestamp without time zone)
 RETURNS integer
 LANGUAGE plpgsql
 IMMUTABLE
AS $function$
declare
  d date;
  season_year int;
  sep1 date;
  labor_day date;
  week1_sat date;
  game_sat date;
begin
  if start_ct is null then
    return null;
  end if;

  d := start_ct::date;

  -- January and February belong to the previous season (bowls, playoff).
  if extract(month from d) in (1,2) then
    season_year := extract(year from d)::int - 1;
  else
    season_year := extract(year from d)::int;
  end if;

  sep1 := make_date(season_year, 9, 1);
  labor_day := sep1 + (((1 - extract(dow from sep1)::int) + 7) % 7);
  week1_sat := labor_day - 2;

  -- The Saturday this game's week is anchored on (week runs Tue..Mon).
  game_sat := case extract(dow from d)::int
                when 0 then d - 1                        -- Sunday -> previous Saturday
                when 1 then d - 2                        -- Monday -> previous Saturday
                else d + (6 - extract(dow from d)::int)  -- Tue..Sat -> upcoming Saturday
              end;

  return ((game_sat - week1_sat) / 7)::int + 1;
end;
$function$;
