-- The market number, on every game in the week - not only the ones being played.
--
-- Lines move before kickoff too, and ours freezes on the Tuesday. The gap
-- between the number a week was picked at and the number the market is showing
-- now is worth seeing all week, not just for three hours on a Saturday.
--
-- WHICH NUMBER. ESPN carries several odds providers per game and only one of
-- them is in-play. Before kickoff there is no in-play quote at all, so the
-- pregame provider IS the current market. During a game the in-play quote is the
-- current market and the pregame one has stopped moving. So: prefer a provider
-- whose name contains "live", fall back to the pregame book, and record which
-- kind was taken.
--
-- The kind matters and is not cosmetic. The leaderboard reads an in-play number
-- as an estimate of the FINAL margin - `line - line_live` - which is only true
-- of an in-play quote. Fed a pregame close mid-game it would read a blowout as
-- an even game, so the page checks line_live_kind before trusting it that way.
-- The banner shows either.

alter table public.all_games
  add column if not exists line_live_kind text;

comment on column public.all_games.line_live is
  'The market''s current spread, home team''s sign. Display only - never scored against, see line.';
comment on column public.all_games.line_live_kind is
  'Which quote line_live came from: ''inplay'' while a game is being played, ''market'' for a pregame book. Only ''inplay'' may be read as an estimate of the final margin.';

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
  kind text;
  now_ct timestamp := (now() at time zone 'America/Chicago');
  n_checked int := 0;
  n_updated int := 0;
begin
  if not pg_try_advisory_xact_lock(hashtext('sync_live_lines')) then
    return query select 0, 0;
    return;
  end if;

  -- A finished game's last quote is a fossil.
  update public.all_games
     set line_live = null, line_live_ou = null, line_live_at = null, line_live_kind = null
   where line_live is not null
     and (coalesce("Status", '') = 'final' or winner is not null);

  for g in
    select "GameId"
      from public.all_games
     where (picked is true or tiebreaker is true)
       and "Start (CT)" is not null
       and ("Start (CT)")::timestamp > now_ct - interval '6 hours'
       and ("Start (CT)")::timestamp < now_ct + interval '5 days'
       and coalesce("Status", '') <> 'final'
       and winner is null
       -- A game being played is repriced constantly and gets every run. One that
       -- has not kicked off drifts slowly, so it gets one run in five - enough to
       -- catch a move, a fifth of the requests.
       and ( ("Start (CT)")::timestamp <= now_ct
             or (extract(minute from now_ct)::int % 5) = 0 )
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

    sp := null; ou := null; kind := null;
    begin
      select (item->>'spread')::numeric,
             (item->>'overUnder')::numeric,
             case when (item #>> '{provider,name}') ilike '%live%' then 'inplay' else 'market' end
        into sp, ou, kind
        from jsonb_array_elements(coalesce(body->'items', '[]'::jsonb)) item
       where item->>'spread' is not null
       -- In-play first when there is one. Then by name, which is arbitrary but
       -- STABLE: the same book wins every run, so the number only changes when
       -- that book actually reprices the game.
       order by case when (item #>> '{provider,name}') ilike '%live%' then 0 else 1 end,
                item #>> '{provider,name}'
       limit 1;
    exception when others then
      sp := null;
    end;

    if sp is null then continue; end if;

    update public.all_games
       set line_live = sp, line_live_ou = ou, line_live_kind = kind, line_live_at = now()
     where "GameId" = g."GameId";

    if found then n_updated := n_updated + 1; end if;
  end loop;

  return query select n_checked, n_updated;
end;
$$;

revoke all on function public.sync_live_lines() from public, anon, authenticated;
