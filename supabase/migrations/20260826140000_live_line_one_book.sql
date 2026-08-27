-- Pick the same book every minute.
--
-- sync_live_lines() takes the first odds provider whose name contains "live".
-- Array order in ESPN's response is not promised, and some feeds carry more
-- than one live book at once (their MLB feed serves "DraftKings - Live Odds"
-- where college football serves "ESPN Bet - Live Odds"; a game could carry
-- both). Taking whichever happened to come back first would let the displayed
-- number flip between two books' prices from one minute to the next, which
-- looks exactly like a line moving when nothing has moved.
--
-- Sorting by provider name is arbitrary but STABLE, and stable is the whole
-- point: the same book wins every run, so the number only changes when that
-- book actually reprices the game.
--
-- Also moves the spread out of a hand-rolled loop and into the query, so a
-- provider quoting something unparseable is skipped by the `is not null` test
-- instead of needing its own exception handler.
create or replace function public.sync_live_lines()
returns table(checked int, updated int)
language plpgsql
security definer
set search_path = public, extensions
as $$
declare
  g record;
  body jsonb;
  sp numeric;
  ou numeric;
  now_ct timestamp := (now() at time zone 'America/Chicago');
  n_checked int := 0;
  n_updated int := 0;
begin
  if not pg_try_advisory_xact_lock(hashtext('sync_live_lines')) then
    return query select 0, 0;
    return;
  end if;

  update public.all_games
     set line_live = null, line_live_ou = null, line_live_at = null
   where line_live is not null
     and (coalesce("Status", '') = 'final' or winner is not null);

  for g in
    select "GameId"
      from public.all_games
     where (picked is true or tiebreaker is true)
       and "Start (CT)" is not null
       and ("Start (CT)")::timestamp <= now_ct
       and ("Start (CT)")::timestamp > now_ct - interval '6 hours'
       and coalesce("Status", '') <> 'final'
       and winner is null
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
          'https://sports.core.api.espn.com/v2/sports/football/leagues/college-football'
          || '/events/' || g."GameId" || '/competitions/' || g."GameId" || '/odds?limit=50');
    exception when others then
      continue;
    end;

    if body is null then continue; end if;
    n_checked := n_checked + 1;

    sp := null;
    ou := null;
    begin
      select (item->>'spread')::numeric,
             (item->>'overUnder')::numeric
        into sp, ou
        from jsonb_array_elements(coalesce(body->'items', '[]'::jsonb)) item
       where (item #>> '{provider,name}') ilike '%live%'
         and item->>'spread' is not null
       order by item #>> '{provider,name}'
       limit 1;
    exception when others then
      sp := null;   -- a book quoting a spread that will not cast is no book at all
    end;

    if sp is null then continue; end if;

    update public.all_games
       set line_live = sp, line_live_ou = ou, line_live_at = now()
     where "GameId" = g."GameId";

    if found then n_updated := n_updated + 1; end if;
  end loop;

  return query select n_checked, n_updated;
end;
$$;

revoke all on function public.sync_live_lines() from public, anon, authenticated;
