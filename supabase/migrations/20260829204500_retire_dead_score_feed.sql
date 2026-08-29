-- Stop the score feed that has not worked since this morning.
--
-- Cron job 1, "Update from ESPN", posts to the update-final-scores edge function
-- every minute. On the morning of the 2026 opener that function began returning
-- 502 "ESPN fetch failed" on every single call - 360 of them in the six hours
-- pg_net retains, without one success - which is what took the score and the
-- clock off the board mid-game and prompted sync_espn_scores() to be written.
--
-- sync_espn_scores() now does the same work from inside Postgres and is proven:
-- it carried the opener from kickoff through to North Carolina 15, TCU 10 final,
-- with the winner graded off the frozen line. It also does more - possession and
-- the red zone - so nothing is lost by stopping the old one.
--
-- DEACTIVATED, NOT DELETED, and deliberately so. cron.unschedule() would drop
-- the row, and that row holds a service_role JWT in its command; leaving it in
-- place means the key is never read out or retyped to restore the job. If the
-- edge function is ever fixed, this is one statement to reverse:
--
--     select cron.alter_job(1, active := true);
--
-- The 502s were also the only thing writing to net._http_response, which is how
-- a real failure elsewhere would be spotted. Quiet is the point.

do $$
declare
  j record;
begin
  select jobid, jobname, active into j
    from cron.job
   where jobid = 1 and jobname = 'Update from ESPN';

  if not found then
    raise notice 'job 1 "Update from ESPN" is not there - nothing to retire';
    return;
  end if;

  if not j.active then
    raise notice 'job 1 is already inactive';
    return;
  end if;

  perform cron.alter_job(1, active := false);
  raise notice 'job 1 "Update from ESPN" deactivated; sync_espn_scores now carries the live state';
end $$;
