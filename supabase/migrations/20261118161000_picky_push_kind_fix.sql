-- picky_push: the 'kind' parameter collided with picky.sheet.kind in ON CONFLICT (42702).
create or replace function public.picky_push(kind text, rows jsonb)
returns integer
language plpgsql
security definer
set search_path = picky, public
as $$
#variable_conflict use_column
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

revoke all on function public.picky_push(text, jsonb) from public, anon, authenticated;
grant execute on function public.picky_push(text, jsonb) to service_role;
