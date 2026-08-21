-- Two data problems in the completed 2025 season, both of which cost real
-- gameplay. Safe to set by hand: the ingest last touched a 2025 row on
-- 2026-01-20 and now only pulls 2026, and trg_set_week_on_write fires only on
-- UPDATE OF "Start (CT)", which this does not touch.

-- 1. Three games ended up with week = NULL, written in a single batch on
--    2025-11-10 by an older commissioner build whose payload included `week`
--    but not `Start (CT)`, so the week trigger never fired and the NULL stuck.
--    picks.html filters .eq('week', week), so these three never rendered and
--    NOBODY IN THE LEAGUE COULD PICK THEM - they are the only three picked 2025
--    games with zero picks against them. calc_cfb_week correctly returns 11.
update public.all_games
   set week = calc_cfb_week(("Start (CT)")::timestamp)
 where cfb_season = 2025
   and week is null
   and "Start (CT)" is not null;

-- 2. Week 0 was fused into Week 1. calc_cfb_week returns 1 for anything before
--    Sep 3, so the two Aug 23 games (the league's Week 0 slate) were stored as
--    week 1 alongside the four Aug 30-31 games. That made a six-game "Week 1"
--    and hid the fact that late joiners missed a slate - Kevin Culligan showed
--    0 missed weeks despite having no picks for either Aug 23 game.
--
--    Fixed as data rather than by changing calc_cfb_week: that function stamps
--    every ingested row, 2026's 389 rows are already correctly weeked by it, and
--    2026 has no Week 0 slate. Not worth the blast radius.
update public.all_games
   set week = 0
 where cfb_season = 2025
   and ("Start (CT)")::timestamp < '2025-08-24'::timestamp;
