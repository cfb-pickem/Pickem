-- Tune the ramp to one auto win a season.
--
-- The original numbers were invented: there were no slots picks in the database
-- when they were written, so "five pulls a week" was a guess. The working
-- assumption now is that the average player pulls the lever once or twice a
-- week, which for this league is:
--
--   14 players x ~1.5 pulls a week x 20 picked weeks  ->  ~420 pulls a season
--
-- One of those should be an auto win, so the mean number of pulls between pops
-- has to be 420.
--
-- WHY THE SHAPE CHANGED. A straight ramp cannot do this. Averaging one pop per
-- 420 pulls means the per-pull chance must average about 1/420 - a quarter of a
-- percent - and any ramp that spends real time at five or ten per cent ends its
-- cycle almost immediately, which drags the average far below 420. Solving a
-- linear ramp for 420 gives odds that crawl from 0.05% to 0.25% across a whole
-- season and never come within sight of the ceiling: a flat lottery wearing a
-- progressive's costume, with a meter nobody would ever see move.
--
-- So this is a MUST HIT BY progressive, which is the mechanic real casinos use
-- for exactly this problem - "this pays out about once per X".
--
--   odds = least(0.0005 + greatest(pulls - 445, 0) * 0.0025, 0.10)
--
--   pulls 0..445   0.05% each        the quiet stretch: nearly a whole season
--   pull  455      2.6%              it starts going off
--   pull  465      5.1%
--   pull  475      7.5%
--   pull  485+     10.00%            pinned at the ceiling until it goes
--
--   mean pulls between pops        420
--   expected auto wins per season  1.00
--   pops early, out of nowhere      20%
--   goes the distance to the tail   80%
--
-- That last pair is what makes it worth watching. Four times in five the
-- football survives to the end of the cycle and then visibly starts to go,
-- which is the drama; one time in five it simply bursts in October with no
-- warning at all, which is what stops the whole thing being a calendar.
--
-- THE FLOOR CAME DOWN from 0.25% to 0.05%, and it had to. A 0.25% floor means a
-- pop every 400 pulls on its own, before any ramp at all - so at 420 pulls a
-- season the target was already met with the slope set to zero, and there would
-- have been no progressive left to build.
--
-- THE CEILING IS UNCHANGED at 10%, and still means what it always meant: the
-- next pull is never better than a one-in-ten shot however long the drought has
-- run.
--
-- IT IS CALIBRATED ON AN ASSUMPTION, not a measurement. The league has made two
-- slots picks out of 168 so far, which is a rate of about 13 pulls a season, not
-- 420. If usage stays where it is rather than rising to one or two a week, this
-- ramp will almost never pay out. The count scales linearly with the pull rate
-- and 445 is the one number to move.

create or replace function public.slots_jackpot_odds(pulls integer)
returns numeric
language sql
immutable
as $$
  select least(
    0.0005 + greatest(coalesce(pulls, 0) - 445, 0) * 0.0025,
    0.10
  );
$$;

comment on function public.slots_jackpot_odds(integer) is
  'Jackpot probability for the next pull. Must-hit-by: 0.05% for the first 445 pulls, then climbing 0.25pp a pull to a 10% ceiling - tuned so a season of ~420 pulls yields one auto win on average.';
