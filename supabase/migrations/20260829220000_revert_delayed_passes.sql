-- Revert the extra sampling passes. They made the site slow.
--
-- WHAT HAPPENED. 20260829214500 added four cron jobs that sleep 20 or 40 seconds
-- before doing their work, to sample three times a minute instead of once. The
-- sampling worked - the live line went from a 60-second worst case to 19. But a
-- sleeping job holds its Postgres connection for the whole sleep, and with four
-- of them overlapping every minute the pool PostgREST serves the site from was
-- starved. Page load went from about 3 seconds to 9.6, during a live game.
--
-- Measured both ways: 9,584ms with the delayed jobs running, then 2,811 /
-- 3,125 / 3,013ms within a minute of unscheduling them.
--
-- THE LESSON, which is the reason this file exists rather than a silent
-- unschedule: pg_cron's one-minute floor is not really the constraint. The
-- constraint is that any way of beating it costs a held connection, and
-- connections are what the site itself runs on. Freshness bought at the cost of
-- the page loading is not a trade worth making - a line 30 seconds old on a page
-- that renders instantly beats a line 10 seconds old on one that takes ten.
--
-- If this is ever revisited, the shape to try is ONE extra pass at +30s rather
-- than four: half the lag for a quarter of the connection cost. It should be
-- tried on a quiet afternoon and watched, not during a game.
--
-- The functions keep their delay parameter - it is harmless at 0 and it is what
-- any future attempt would use - but nothing schedules a delayed pass.

do $$
declare nm text;
begin
  foreach nm in array array['sync-live-lines-20','sync-live-lines-40',
                            'sync-espn-scores-20','sync-espn-scores-40'] loop
    begin
      perform cron.unschedule(nm);
      raise notice 'unscheduled %', nm;
    exception when others then
      null;   -- already gone
    end;
  end loop;
end $$;

-- Left in place and confirmed: the two on-the-minute jobs that were always here.
--   sync-espn-scores  * * * * *  select public.sync_espn_scores()
--   sync-live-lines   * * * * *  select public.sync_live_lines()
