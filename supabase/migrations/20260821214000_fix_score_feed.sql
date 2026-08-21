-- 1. THE LIVE SCORE BUG
--    "Update from ESPN" runs every minute with timeout_milliseconds := 1000.
--    That edge function averages ~470 ms but peaks at 62 s, so whenever it is
--    slow pg_net abandons the call and that minute's scores never land. A
--    1-second budget for an HTTP round trip to an edge function that then calls
--    ESPN and writes rows was never realistic.
--
--    Rewritten in place with regexp_replace so the service_role JWT inside the
--    command is never read out or re-typed.
do $$
declare c text; n int;
begin
  for n in select jobid from cron.job where command ~ 'timeout_milliseconds' loop
    select command into c from cron.job where jobid = n;
    c := regexp_replace(c, 'timeout_milliseconds\s*:=\s*\d+', 'timeout_milliseconds:=15000');
    perform cron.alter_job(n, command := c);
  end loop;
end $$;

-- 2. STOP THE TREADMILL
--    cleanup_unpicked_games() ran hourly, deleting past unpicked games, while
--    "Update from CFB API" re-inserted them every two hours. Over ten months
--    that produced 11,392 inserts and 10,920 deletes on a table that has never
--    held more than ~500 rows - pure write amplification competing with the
--    score feed for I/O, and the window that let a stale commissioner save
--    resurrect a game as a NULL-filled shell row.
--
--    Daily instead of hourly: same tidy-up, 1/24th of the churn.
select cron.alter_job(jobid, schedule := '40 4 * * *')
from cron.job where jobname like 'cleanup_unpicked_games%';
