-- Pull ESPN's FPI projection for every picked game, every few hours, forever.
--
-- WHAT THIS IS AND IS NOT WORTH
--
-- FPI is not a tipster. Backtested against your 2025 slate, taking whichever
-- side FPI likes relative to the market goes 37-76 against the spread - 48.7%,
-- a coin flip, and worse than blindly taking the favourite (59.2%). Beating a
-- closing line is hard and ESPN does not.
--
-- What it IS good for is the question this site actually asks: which side will
-- the POOL take. When FPI disagrees hard with the favourite (by 7+ points) the
-- league lays that favourite 16.5 points less often than the spread alone
-- predicts, and the effect is monotone across the range. Folded into the pick
-- model it moved leave-one-week-out accuracy 58.6% -> 60.3%, log loss 0.6587 ->
-- 0.6576, AUC 0.601 -> 0.604. Small, but it is the same direction on all three
-- and the data is free.
--
-- Why in the database rather than an edge function: same reasons the line sync
-- is here. ESPN's endpoint is public with no key, all_games."GameId" IS the ESPN
-- event id so no matching is needed, and pg_cron already runs. Nothing to deploy
-- and nothing to keep alive.
--
-- One honest caveat about the numbers above. They were measured against FPI
-- values fetched AFTER the 2025 season, which carry full-season team ratings and
-- so flatter the model slightly. Syncing weekly is what fixes that: from now on
-- the stored value is the genuine pre-game projection, frozen at the same
-- Tuesday deadline the line freezes at, so next season trains on honest data.

alter table public.all_games
  add column if not exists fpi_margin      numeric,          -- home team's projected margin
  add column if not exists fpi_win_prob    numeric,          -- home team's win probability, 0-100
  add column if not exists fpi_updated_at  timestamptz;

comment on column public.all_games.fpi_margin is
  'ESPN FPI projected point margin for the HOME team (positive = home favoured). '
  'Frozen at the line lock, so it is the pre-game number the week was picked against.';

create or replace function public.sync_espn_fpi()
returns table(checked int, updated int)
language plpgsql
security definer
set search_path = public, extensions
as $$
declare
  g record;
  body jsonb;
  margin numeric;
  winprob numeric;
  now_ct timestamp := (now() at time zone 'America/Chicago');
  n_checked int := 0;
  n_updated int := 0;
begin
  for g in
    select "GameId", "Start (CT)"
      from public.all_games
     where (coalesce(picked, false) or coalesce(tiebreaker, false))
       and "Start (CT)" is not null
       -- Only games still ahead of us, and only while the week is open. Once the
       -- line locks the projection locks with it: that frozen value is the
       -- pre-game number, and re-fetching afterwards would overwrite it with a
       -- post-game one that quietly knows the result.
       and ("Start (CT)")::timestamp > now_ct
       and (
            fpi_margin is null
            or now_ct < public.line_lock_at(("Start (CT)")::timestamp)
       )
     order by "Start (CT)"
     limit 60                       -- a week's slate is a handful; this is a guard
  loop
    n_checked := n_checked + 1;

    begin
      select content::jsonb into body
        from extensions.http_get(
          'https://sports.core.api.espn.com/v2/sports/football/leagues/college-football/events/'
          || g."GameId" || '/competitions/' || g."GameId" || '/predictor');
    exception when others then
      continue;                      -- one bad game must not abandon the run
    end;

    if body is null or body->'homeTeam' is null then continue; end if;

    begin
      select (s->>'value')::numeric into margin
        from jsonb_array_elements(body->'homeTeam'->'statistics') s
       where s->>'name' = 'teamPredPtDiff'
       limit 1;

      select (s->>'value')::numeric into winprob
        from jsonb_array_elements(body->'homeTeam'->'statistics') s
       where s->>'name' = 'gameProjection'
       limit 1;
    exception when others then
      continue;
    end;

    if margin is null then continue; end if;

    update public.all_games
       set fpi_margin     = margin,
           fpi_win_prob   = winprob,
           fpi_updated_at = now()
     where "GameId" = g."GameId"
       and (fpi_margin is distinct from margin or fpi_win_prob is distinct from winprob);

    if found then n_updated := n_updated + 1; end if;
  end loop;

  checked := n_checked;
  updated := n_updated;
  return next;
end;
$$;

revoke all on function public.sync_espn_fpi() from public, anon, authenticated;

-- Every six hours, offset from the line sync so the two are not fetching at once.
-- A projection barely moves between runs; this is about never going stale, not
-- about being live.
select cron.unschedule('sync-espn-fpi') where exists (
  select 1 from cron.job where jobname = 'sync-espn-fpi'
);
select cron.schedule('sync-espn-fpi', '40 */6 * * *',
  $$select public.sync_espn_fpi()$$);
