-- tools/blackjack-open.check.sql — the table opens with the picks, and settles
-- only at a kickoff. Run against the real database; it CANNOT write anything.
--
--   cd /c/Users/charl && ./Pickem/node_modules/.bin/supabase db query --linked \
--     -f Pickem/tools/blackjack-open.check.sql
--
-- Everything happens inside one transaction that ends in a raised exception,
-- so it rolls back whether the checks pass or fail - including the cards
-- bj_open_week() draws and anything bj_settle_week() would settle. Read the
-- error text: "PASS (rolled back)" or "FAIL: ...". The "error" is the result.
--
-- It builds its own weeks out of fake all_games rows (negative GameIds) in
-- seasons nobody plays - 2099 for weeks that have not kicked off, 1990 for one
-- that has - so it does not depend on what the real schedule looks like today.
-- Requires 20261012130000_open_with_the_picks.sql to have been applied.

begin;

do $$
declare
  w_picked integer; w_early integer; w_tb integer; w_past integer;
  live record; n integer; got text;
begin
  -- Week A: picked, kicks off in 2099. Its Tuesday line lock is decades away,
  -- so only the new rule can open it.
  insert into public.all_games ("GameId", "Start (CT)", "Away", "Home", picked)
  values (-990001, '2099-10-03 11:00', 'Fake A', 'Fake B', true);
  select week into w_picked from public.all_games where "GameId" = -990001;

  -- Week B: the following week, set early.
  insert into public.all_games ("GameId", "Start (CT)", "Away", "Home", picked)
  values (-990002, '2099-10-10 11:00', 'Fake C', 'Fake D', true);
  select week into w_early from public.all_games where "GameId" = -990002;

  -- Week C: tiebreaker games only. Keeps the Tuesday gate.
  insert into public.all_games ("GameId", "Start (CT)", "Away", "Home", tiebreaker)
  values (-990003, '2099-10-24 11:00', 'Fake E', 'Fake F', true);
  select week into w_tb from public.all_games where "GameId" = -990003;

  perform public.bj_open_week();

  if not exists (select 1 from public.blackjack_weeks where cfb_season = 2099 and week = w_picked) then
    raise exception 'FAIL: a week with picked games was not dealt before its line lock';
  end if;
  if not exists (select 1 from public.blackjack_hole
                  where cfb_season = 2099 and week = w_picked and up is not null and hole is not null) then
    raise exception 'FAIL: the dealt week has no sealed pair';
  end if;
  if exists (select 1 from public.blackjack_weeks where cfb_season = 2099 and week = w_picked and dealer_up is not null) then
    raise exception 'FAIL: the up-card was published on open';
  end if;
  if not exists (select 1 from public.blackjack_weeks where cfb_season = 2099 and week = w_early) then
    raise exception 'FAIL: a later week set early was not dealt';
  end if;
  if exists (select 1 from public.blackjack_weeks where cfb_season = 2099 and week = w_tb) then
    raise exception 'FAIL: a tiebreaker-only week was dealt before its Tuesday line lock';
  end if;

  -- Idempotent: a second cron minute deals nothing new for these weeks.
  select count(*) into n from public.blackjack_hole where cfb_season = 2099;
  perform public.bj_open_week();
  if (select count(*) from public.blackjack_hole where cfb_season = 2099) <> n then
    raise exception 'FAIL: a second bj_open_week() dealt again';
  end if;

  -- Two weeks dealt: the live one is the earliest.
  select * into live from public.bj_live_week();
  if live.cfb_season is distinct from 2099 or live.week is distinct from w_picked then
    raise exception 'FAIL: bj_live_week() returned %/% with two weeks dealt, want 2099/%',
      live.cfb_season, live.week, w_picked;
  end if;

  -- Untick the early week (the commissioner's slip). It must stay sealed, not
  -- settle and publish its dealer for a week nobody has played.
  update public.all_games set picked = false where "GameId" = -990002;
  perform public.bj_settle_week();
  if exists (select 1 from public.blackjack_weeks
              where cfb_season = 2099 and week = w_early
                and (settled_at is not null or dealer_final is not null)) then
    raise exception 'FAIL: an unticked week that never kicked off was settled';
  end if;
  if exists (select 1 from public.blackjack_weeks
              where cfb_season = 2099 and week = w_picked and settled_at is not null) then
    raise exception 'FAIL: the open week was settled before its kickoff';
  end if;

  -- Re-tick it: the same table, the same sealed pair, still unsettled.
  select bh.up::text || '/' || bh.hole::text into got
    from public.blackjack_hole bh where cfb_season = 2099 and week = w_early;
  update public.all_games set picked = true where "GameId" = -990002;
  perform public.bj_open_week();
  if (select bh.up::text || '/' || bh.hole::text from public.blackjack_hole bh
       where cfb_season = 2099 and week = w_early) is distinct from got then
    raise exception 'FAIL: re-picking a week re-dealt its dealer';
  end if;

  -- A week that HAS kicked off still settles. Dealt by hand here because
  -- bj_open_week() rightly refuses a week that has already started.
  insert into public.all_games ("GameId", "Start (CT)", "Away", "Home", picked)
  values (-990004, '1990-10-06 11:00', 'Fake G', 'Fake H', true);
  select week into w_past from public.all_games where "GameId" = -990004;
  insert into public.blackjack_weeks (cfb_season, week) values (1990, w_past);
  insert into public.blackjack_hole (cfb_season, week, up, hole) values (1990, w_past, 12, 8);
  perform public.bj_settle_week();
  if not exists (select 1 from public.blackjack_weeks
                  where cfb_season = 1990 and week = w_past and settled_at is not null
                    and dealer_final[1] = 12 and dealer_final[2] = 8) then
    raise exception 'FAIL: a week that kicked off was not settled';
  end if;

  -- The sealed cards are still drawn by cron alone.
  if has_function_privilege('anon', 'public.bj_open_week()', 'execute')
     or has_function_privilege('authenticated', 'public.bj_open_week()', 'execute') then
    raise exception 'FAIL: bj_open_week() is callable by a client';
  end if;

  raise exception 'PASS (rolled back): weeks 2099/% and 2099/% opened with their picks, tiebreaker-only 2099/% waited, live = earliest, unticked week stayed sealed and kept its cards, kicked-off 1990/% settled',
    w_picked, w_early, w_tb, w_past;
end $$;

rollback;
