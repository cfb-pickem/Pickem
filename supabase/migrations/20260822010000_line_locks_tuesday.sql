-- Lines lock Tuesday of the game's week at 11:00 CT.
--
--   * Before that deadline, the line tracks ESPN and keeps moving.
--   * At the deadline it freezes - whatever it says at 11:00 Tuesday is what the
--     week is scored against.
--   * If a game still has NO line at the deadline, the first line ESPN publishes
--     afterwards is taken and then frozen.
--
-- The CFB week runs Tue..Mon (see calc_cfb_week), so the Tuesday of a game's
-- week is always its anchoring Saturday minus 4 days - which is before every
-- game in that week, including Wednesday and Thursday games.
create or replace function public.line_lock_at(start_ct timestamp without time zone)
returns timestamp without time zone
language sql
immutable
as $$
  select ((case extract(dow from start_ct::date)::int
             when 0 then start_ct::date - 1                                  -- Sun -> prior Sat
             when 1 then start_ct::date - 2                                  -- Mon -> prior Sat
             else start_ct::date + (6 - extract(dow from start_ct::date)::int)
           end) - 4)::timestamp + time '11:00';
$$;

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
  now_ct timestamp := (now() at time zone 'America/Chicago');
  n_checked int := 0;
  n_updated int := 0;
begin
  for d in
    select generate_series(current_date - p_days_back, current_date + p_days_ahead, interval '1 day')::date
  loop
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

      odd := ev #> '{competitions,0,odds,0}';
      if odd is null or odd->>'spread' is null then continue; end if;

      begin
        sp := (odd->>'spread')::numeric;
      exception when others then continue;
      end;

      n_checked := n_checked + 1;

      update public.all_games g
         set line = sp
       where g."GameId" = gid
         and g.line is distinct from sp
         and g."Start (CT)" is not null
         -- Never touch a game that has started: a line published mid-game is a
         -- live line, not the number the picks were made against.
         and (g."Start (CT)")::timestamp > now_ct
         and (
              -- No line yet: take the first one ESPN gives us, whenever it lands.
              g.line is null
              -- Otherwise only while the week is still open.
              or now_ct < public.line_lock_at((g."Start (CT)")::timestamp)
         );

      if found then n_updated := n_updated + 1; end if;
    end loop;
  end loop;

  return query select n_checked, n_updated;
end;
$$;

revoke all on function public.sync_espn_lines(int, int) from public, anon, authenticated;
