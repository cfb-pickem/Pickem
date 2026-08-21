-- Reclaim the instance and stop the background churn that was starving reads.
--
-- Before: 735 MB database, of which 719 MB was pg_cron / pg_net bookkeeping for
-- a ~2 MB application. Inserting a single cron log row averaged 190 ms (worst
-- case 133 s) purely from I/O contention, and every front-end request queued
-- behind it.

-- 1. Ten months of cron history nobody reads: 432,246 rows / 522 MB.
--    TRUNCATE rather than DELETE + VACUUM FULL - it reclaims the space
--    immediately, holds its lock for milliseconds instead of minutes, and needs
--    no temporary double disk. This is append-only audit history; dropping it
--    loses nothing operational.
truncate cron.job_run_details;

-- 2. pg_net's response table: 363 live rows occupying 197 MB of unreclaimed
--    bloat. The cron jobs fire-and-forget, so no caller is waiting on these.
truncate net._http_response;

-- 3. Keep it from growing back. Without this we're in the same place by spring.
select cron.unschedule('purge-cron-history')
  where exists (select 1 from cron.job where jobname = 'purge-cron-history');
select cron.schedule(
  'purge-cron-history', '17 4 * * *',
  $$delete from cron.job_run_details where end_time < now() - interval '3 days'$$
);
