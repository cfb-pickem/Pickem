-- Every line ends in .5, so a push can never happen.
--
-- set_winner_on_final() decides a game with `(HomePts + line) > AwayPts`. On a
-- whole-number line that comparison can come out exactly equal - a push - and
-- the else branch hands the cover to the AWAY team. Nobody wants a game decided
-- by which side of an `if` a tie fell on, and this league has no push rule.
--
-- Rather than teach the scorer about pushes, remove the case entirely: no line
-- in the table is ever a whole number, so `HomePts + line = AwayPts` is
-- unreachable (scores are integers, and integer + .5 is never an integer).
--
-- Done as a trigger rather than inside sync_espn_lines() on purpose. The sync is
-- not the only writer - the commissioner page issues UPDATEs, and so does the
-- occasional hand-run statement - and a rule that only holds for one writer is
-- not a rule.

create or replace function public.half_point_line()
returns trigger
language plpgsql
as $$
begin
  if new.line is null then
    return new;
  end if;

  -- Stability first, or this rule fights the line sync.
  --
  -- sync_espn_lines() writes whenever ESPN's number is `distinct from` ours. If
  -- ESPN keeps saying -7 while we store -7.5, that test is true on every run,
  -- so without this the line would be re-randomised every sync and jitter
  -- between -6.5 and -7.5 all week - right under people making picks.
  --
  -- A whole number has two equally valid half-point representations. Once one
  -- has been chosen, keep it: any incoming value within half a point of what we
  -- already store is the same line, said differently. Genuine movement is more
  -- than half a point and still comes through.
  if tg_op = 'UPDATE'
     and old.line is not null
     and old.line = floor(old.line) + 0.5
     and abs(new.line - old.line) <= 0.5 then
    new.line := old.line;
    return new;
  end if;

  if new.line = floor(new.line) then
    -- Dead whole number, so there is no nearer half-point than either
    -- neighbour. Pick a side at random: over a season it is a wash, and
    -- choosing a fixed direction would quietly bias every book line toward
    -- the home or the away team.
    new.line := floor(new.line) + case when random() < 0.5 then 0.5 else -0.5 end;
  else
    -- floor(x) + 0.5 IS the nearest half-point for any non-integer x, positive
    -- or negative: it is the only one inside (floor(x), floor(x)+1), and x is
    -- somewhere in that interval. -7.25 -> -7.5, -6.8 -> -6.5, 3.75 -> 3.5.
    -- Anything already at k.5 is left exactly where it is.
    new.line := floor(new.line) + 0.5;
  end if;

  return new;
end;
$$;

-- Name matters. Triggers on the same event fire in alphabetical order, and
-- trg_set_winner_on_final reads new.line to decide the game - so the rounding
-- has to land first. "trg_half..." sorts before "trg_set...", which is what
-- makes the scorer see the rounded number.
drop trigger if exists trg_half_point_line on public.all_games;
create trigger trg_half_point_line
  before insert or update of line on public.all_games
  for each row execute function public.half_point_line();

-- Fix the rows already carrying a whole number. Assigning line to itself is
-- enough: the trigger is what does the rounding.
--
-- Deliberately scoped to games whose line has NOT locked yet. A locked line is
-- the number a week was picked against, and moving it after the fact would
-- change what people were betting - which is a worse problem than the push it
-- would avoid. At the time this shipped every whole-number line was still
-- unlocked, so the guard was belt and braces.
update public.all_games
   set line = line
 where line is not null
   and line = floor(line)
   and (
     "Start (CT)" is null
     or public.line_lock_at("Start (CT)") > (now() at time zone 'America/Chicago')
   );
