-- Run the line sync four times a day. Lines move through the week, so this
-- keeps them current without hammering ESPN: 15 day-fetches per run, 4 runs a
-- day. Offset from the other jobs so they don't pile up on the same minute.
-- The function only touches games that haven't kicked off and only writes when
-- the number actually changed, so a run that finds nothing new writes nothing.
select cron.unschedule('sync-espn-lines')
 where exists (select 1 from cron.job where jobname = 'sync-espn-lines');

select cron.schedule('sync-espn-lines', '25 */6 * * *',
  $$select public.sync_espn_lines()$$);
