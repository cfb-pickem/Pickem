-- Put the jackpot meter back to the pulls that actually happened.
--
-- 20261123002321_count_the_pull_once fixed the upsert double count going
-- forward but left the meter where the bug had pushed it: 257 on 2026-11-22,
-- against 113 picks ever flagged slots_pull_counted and no pop ever recorded.
-- Past 200 the ramp gives every slots pick a 10% jackpot chance, so the
-- inflated number was changing live odds. Charlie approved the reset.
--
-- With no pop on record, every counted row is a pull since the last pop, so
-- the true meter is simply the number of counted rows. Computed here rather
-- than hard-coded so a real pull landing in between is not lost. Guarded on
-- last_pop_at is null: after a pop this count would be wrong, so do nothing.
--
-- One UPDATE to one counter. No rows removed, odds/payout functions untouched.

update public.slots_jackpot
   set pulls_since_pop = (select count(*)::integer
                            from public.picks
                           where slots_pull_counted)
 where id = 1
   and last_pop_at is null;
