-- Pull betting lines from ESPN automatically, every week, forever.
--
-- Why in the database rather than an edge function: ESPN's scoreboard endpoint
-- is public (no key, no secret to store), all_games."GameId" IS the ESPN event
-- id so the join is exact, and pg_cron already runs here. That avoids deploying
-- and maintaining another edge function - and avoids putting another
-- service_role JWT into cron.job.
--
-- Sign convention: ESPN's odds.spread is the HOME team's number, which is
-- exactly what all_games.line already stores. Verified both directions against
-- live data - home favored gives a negative spread (TCU -7.5 -> -7.5, TCU at
-- home) and away favored gives a positive one (OKST -14.5 at Tulsa -> +14.5) -
-- and it matches the existing rows (Alabama @ Florida State stored as 13.5,
-- i.e. the away side favoured). So it is copied across as-is, no arithmetic.

create extension if not exists http with schema extensions;

create or replace function public.sync_espn_lines(
  p_days_back int default 1,
  p_days_ahead int default 13
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
  odd jsonb;
  gid bigint;
  sp numeric;
  n_checked int := 0;
  n_updated int := 0;
begin
  for d in
    select generate_series(current_date - p_days_back, current_date + p_days_ahead, interval '1 day')::date
  loop
    begin
      select content::jsonb
        into body
        from extensions.http_get(
          'https://site.api.espn.com/apis/site/v2/sports/football/college-football/scoreboard'
          || '?limit=400&groups=80&dates=' || to_char(d, 'YYYYMMDD')
        );
    exception when others then
      continue;   -- a bad day's fetch must never abort the whole run
    end;

    if body is null then continue; end if;

    for ev in select * from jsonb_array_elements(coalesce(body->'events', '[]'::jsonb))
    loop
      begin
        gid := (ev->>'id')::bigint;
      exception when others then
        continue;
      end;

      odd := ev #> '{competitions,0,odds,0}';
      if odd is null or odd->>'spread' is null then continue; end if;

      begin
        sp := (odd->>'spread')::numeric;
      exception when others then
        continue;
      end;

      n_checked := n_checked + 1;

      -- Only games that have not kicked off yet. Once a game starts its line is
      -- what the picks were made against, so a later closing-line move must not
      -- rewrite history. `is distinct from` keeps this from writing rows that
      -- already agree - this table has a history of write amplification.
      update public.all_games g
         set line = sp
       where g."GameId" = gid
         and g.line is distinct from sp
         and g."Start (CT)" is not null
         and (g."Start (CT)")::timestamp > (now() at time zone 'America/Chicago');

      if found then n_updated := n_updated + 1; end if;
    end loop;
  end loop;

  return query select n_checked, n_updated;
end;
$$;

revoke all on function public.sync_espn_lines(int, int) from public, anon, authenticated;
