-- Shorten the quiet stretch to 200 pulls, and drop the floor to 0.005%.
--
-- 20260914010000 solved the ramp for one auto win against ~420 pulls a season,
-- a figure that was assumed rather than counted: 14 players x ~1.5 pulls a week
-- x 20 weeks. The league has made two slots picks all season. A 445-pull quiet
-- stretch against a rate like that is a football that never goes off, ever.
--
-- So the stretch comes down to 200. That is still well above anything the
-- league has actually done, and it stays the one number to move once there is a
-- real season of pulls behind it - the cycle length scales with it directly.
--
--   odds = least(0.00005 + greatest(pulls - 200, 0) * 0.0025, 0.10)
--
--   pulls 0..200   0.005% each       the quiet stretch
--   pull  205      1.26%             it starts going off
--   pull  210      2.51%
--   pull  220      5.01%
--   pull  230      7.51%
--   pull  240+     10.00%            pinned at the ceiling until it goes
--
--   mean pulls between pops        225
--   median pull of the pop         224
--   pops early, out of nowhere       1%
--   goes the distance to the tail   99%
--
-- THE FLOOR CAME DOWN from 0.05% to 0.005%. Across 200 pulls a 0.05% floor is
-- a one-in-ten chance of popping before the ramp has done anything at all;
-- 0.005% is one in a hundred. The stretch is there to be served, not to pay
-- out, and this is the floor that says so.
--
-- WHAT IT COSTS. The out-of-nowhere pop, which 20260914010000 valued at one
-- cycle in five and called the thing that stops this being a calendar. At this
-- floor it is one in a hundred and the football is, in practice, safe until it
-- is visibly not. That is a deliberate trade and not a side effect: raise the
-- floor to 0.0005 to buy roughly a one-in-ten surprise back, at the cost of a
-- quiet stretch that no longer means much.
--
-- THE CEILING IS UNCHANGED at 10%, and still means what it always meant: the
-- next pull is never better than a one-in-ten shot however long the drought has
-- run.

create or replace function public.slots_jackpot_odds(pulls integer)
returns numeric
language sql
immutable
as $$
  select least(
    0.00005 + greatest(coalesce(pulls, 0) - 200, 0) * 0.0025,
    0.10
  );
$$;

comment on function public.slots_jackpot_odds(integer) is
  'Jackpot probability for the next pull. Must-hit-by: 0.005% for the first 200 pulls, then climbing 0.25pp a pull to a 10% ceiling - a mean of ~225 pulls between pops.';
