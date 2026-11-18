-- Picky fact store v1 (docs/picky-code-ontology-plan-2026-11-18.md 2.3, build RULES 2).
-- Public football facts only: market history, checked ESPN polls, and the built game/team sheets.
-- The picky schema isn't exposed to the REST API and anon/authenticated get nothing; the Mac mini writes and
-- reads through two service-role-only RPCs. Picky serves from its local cache (server/cache/factstore.json,
-- market.sqlite, polls.json); Supabase is the durable copy it rebuilds from.

create schema if not exists picky;
revoke all on schema picky from public, anon, authenticated;
grant usage on schema picky to service_role;

create table if not exists picky.market_snapshot (
  game_id text not null,
  ts timestamptz not null,
  book text not null,
  spread_home numeric,
  total numeric,
  ml_home integer,
  ml_away integer,
  open_spread_home numeric,
  open_total numeric,
  kickoff text,
  away text,
  home text,
  primary key (game_id, ts, book)
);

create table if not exists picky.ranking (
  poll text not null,             -- CFP | AP | Coaches
  release_date date not null,
  rank integer not null,
  team text not null,
  record text,
  src text not null default 'ESPN',
  checked_at timestamptz not null default now(),
  primary key (poll, release_date, rank)
);

create table if not exists picky.sheet (
  kind text not null,             -- game | team | meta
  key text not null,
  body jsonb not null,
  built_at timestamptz not null default now(),
  primary key (kind, key)
);

alter table picky.market_snapshot enable row level security;
alter table picky.ranking enable row level security;
alter table picky.sheet enable row level security;
revoke all on all tables in schema picky from public, anon, authenticated;
grant all on all tables in schema picky to service_role;

-- kind: market_snapshot | ranking | sheet | trim_market. Returns rows written/deleted.
create or replace function public.picky_push(kind text, rows jsonb)
returns integer
language plpgsql
security definer
set search_path = picky, public
as $$
declare n integer := 0;
begin
  if kind = 'market_snapshot' then
    insert into picky.market_snapshot (game_id, ts, book, spread_home, total, ml_home, ml_away, open_spread_home,
                                       open_total, kickoff, away, home)
    select r->>'game_id', to_timestamp((r->>'ts')::double precision), r->>'book', (r->>'spread_home')::numeric,
           (r->>'total')::numeric, (r->>'ml_home')::integer, (r->>'ml_away')::integer,
           (r->>'open_spread_home')::numeric, (r->>'open_total')::numeric, r->>'kickoff', r->>'away', r->>'home'
    from jsonb_array_elements(rows) r
    on conflict (game_id, ts, book) do nothing;
    get diagnostics n = row_count;
  elsif kind = 'ranking' then
    insert into picky.ranking (poll, release_date, rank, team, record, src)
    select r->>'poll', (r->>'release_date')::date, (r->>'rank')::integer, r->>'team', r->>'record',
           coalesce(r->>'src', 'ESPN')
    from jsonb_array_elements(rows) r
    on conflict (poll, release_date, rank) do update set team = excluded.team, record = excluded.record,
      src = excluded.src, checked_at = now();
    get diagnostics n = row_count;
  elsif kind = 'sheet' then
    insert into picky.sheet (kind, key, body, built_at)
    select r->>'kind', r->>'key', r->'body', now()
    from jsonb_array_elements(rows) r
    on conflict (kind, key) do update set body = excluded.body, built_at = now();
    get diagnostics n = row_count;
  elsif kind = 'trim_market' then
    -- games whose newest snapshot is older than keep_days keep only their open and close rows
    delete from picky.market_snapshot m
    using (select game_id, min(ts) lo, max(ts) hi from picky.market_snapshot group by game_id
           having max(ts) < now() - make_interval(days => coalesce((rows->0->>'keep_days')::integer, 21))) old
    where m.game_id = old.game_id and m.ts <> old.lo and m.ts <> old.hi;
    get diagnostics n = row_count;
  else
    raise exception 'picky_push: unknown kind %', kind;
  end if;
  return n;
end $$;

-- kind: sheet | market_snapshot | ranking. The local cache's cold-start source.
create or replace function public.picky_pull(kind text)
returns jsonb
language plpgsql
security definer
set search_path = picky, public
as $$
begin
  if kind = 'sheet' then
    return coalesce((select jsonb_agg(jsonb_build_object('kind', s.kind, 'key', s.key, 'body', s.body))
                     from picky.sheet s), '[]'::jsonb);
  elsif kind = 'market_snapshot' then
    return coalesce((select jsonb_agg(to_jsonb(m) || jsonb_build_object('ts', extract(epoch from m.ts)))
                     from picky.market_snapshot m), '[]'::jsonb);
  elsif kind = 'ranking' then
    return coalesce((select jsonb_agg(to_jsonb(r)) from picky.ranking r
                     where r.release_date >= (select max(release_date) - 21 from picky.ranking)), '[]'::jsonb);
  end if;
  raise exception 'picky_pull: unknown kind %', kind;
end $$;

revoke all on function public.picky_push(text, jsonb) from public, anon, authenticated;
revoke all on function public.picky_pull(text) from public, anon, authenticated;
grant execute on function public.picky_push(text, jsonb) to service_role;
grant execute on function public.picky_pull(text) to service_role;
