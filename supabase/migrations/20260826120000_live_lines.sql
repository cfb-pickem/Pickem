-- Live in-game betting lines, alongside the frozen one the week is scored on.
--
-- WHAT THIS IS NOT. all_games.line is the number the picks were made against.
-- It freezes at 11:00 Tuesday (line_lock_at) and sync_espn_lines() already
-- refuses to touch a game that has kicked off, on the grounds that "a line
-- published mid-game is a live line, not the number the picks were made
-- against". This adds somewhere to PUT that live line instead of throwing it
-- away. It is display only. Nothing scores off it, and no trigger on this table
-- listens to it - trg_half_point_line, trg_remember_line_open,
-- trg_record_line_move and trg_set_winner_on_final are every one of them scoped
-- to `update of line` (or the score columns), so writing here fires none of
-- them and cannot disturb a locked line.
--
-- WHERE IT COMES FROM. ESPN's core API carries more than one odds provider per
-- game, and one of them prices the game while it is being played:
--
--   sports.core.api.espn.com/v2/sports/football/leagues/college-football
--     /events/{id}/competitions/{id}/odds
--
--     100 DraftKings              spread  10     <- pregame
--      58 ESPN BET                spread   9.5   <- pregame
--      59 ESPN Bet - Live Odds    spread  18.5   <- in-game
--
-- Matched on the provider NAME containing "live" rather than on id 59, because
-- which book ESPN fronts has changed before (their MLB feed serves the same
-- shape as "DraftKings - Live Odds", id 200) and a name match survives that
-- swap without anyone having to come back and edit this file.
--
-- Checked against 24 regular-season games from 2025 before writing this, and
-- the provider is there for all 24 and tracks the game: Grambling at Ohio State
-- opened -51.5 and ends -69.5 in a 70-0 win; Texas Tech at West Virginia opened
-- 24.5 and ends 46.5 in a 49-0 one. Sign convention is the home team's number,
-- same as all_games.line, so it is copied across with no arithmetic.
--
-- KNOWN GAP: the postseason. Thirteen bowl and playoff games from Dec 2025 to
-- Jan 2026 carry no live provider at all - ESPN simply does not publish one for
-- neutral-site postseason games. Nothing to be done about that from here; the
-- column stays null and the leaderboard shows no live line, which is the right
-- outcome anyway.

alter table public.all_games
  add column if not exists line_live    numeric,
  add column if not exists line_live_ou numeric,
  add column if not exists line_live_at timestamptz;

comment on column public.all_games.line_live is
  'Live in-game spread, home team''s sign. Display only - never scored against, see line.';
comment on column public.all_games.line_live_at is
  'When line_live was last confirmed against ESPN. Stamped on every successful read, so a stale clock means the feed stopped, not that the line stopped moving.';

create or replace function public.sync_live_lines()
returns table(checked int, updated int)
language plpgsql
security definer
set search_path = public, extensions
as $$
declare
  g record;
  body jsonb;
  item jsonb;
  sp numeric;
  ou numeric;
  now_ct timestamp := (now() at time zone 'America/Chicago');
  n_checked int := 0;
  n_updated int := 0;
begin
  -- This runs every minute and makes one blocking HTTP call per live game. If a
  -- run is somehow still going when the next one fires, the second one should
  -- leave rather than queue up behind it.
  if not pg_try_advisory_xact_lock(hashtext('sync_live_lines')) then
    return query select 0, 0;
    return;
  end if;

  -- A finished game's last live line is a fossil. Clear it so nothing downstream
  -- has to reason about whether the number it is holding is still live.
  update public.all_games
     set line_live = null, line_live_ou = null, line_live_at = null
   where line_live is not null
     and (coalesce("Status", '') = 'final' or winner is not null);

  -- Only games in the pool, only after kickoff, only until they are final. The
  -- six-hour tail covers a weather delay without polling a game forever if the
  -- feed never marks it final.
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
    -- Bound the wait. A hung request must not eat the whole minute.
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
      continue;   -- one game's bad fetch must never abort the rest of the run
    end;

    if body is null then continue; end if;
    n_checked := n_checked + 1;

    sp := null;
    ou := null;
    for item in select * from jsonb_array_elements(coalesce(body->'items', '[]'::jsonb))
    loop
      if (item #>> '{provider,name}') ilike '%live%' then
        begin sp := (item->>'spread')::numeric;    exception when others then sp := null; end;
        begin ou := (item->>'overUnder')::numeric; exception when others then ou := null; end;
        exit;
      end if;
    end loop;

    if sp is null then continue; end if;

    -- Stamped every time, even when the number has not moved: a line that sat
    -- still for ten minutes and a feed that died ten minutes ago look identical
    -- otherwise, and the page needs to tell them apart.
    update public.all_games
       set line_live = sp, line_live_ou = ou, line_live_at = now()
     where "GameId" = g."GameId";

    if found then n_updated := n_updated + 1; end if;
  end loop;

  return query select n_checked, n_updated;
end;
$$;

revoke all on function public.sync_live_lines() from public, anon, authenticated;

-- Every minute, like the score feed - a live line that is five minutes old is
-- not a live line. Costs one indexed query and nothing else outside a game
-- window: with no game in flight the loop body never runs, so no HTTP happens.
do $$
begin
  perform cron.unschedule('sync-live-lines');
exception when others then
  null;
end $$;

select cron.schedule('sync-live-lines', '* * * * *', $job$select public.sync_live_lines()$job$);
