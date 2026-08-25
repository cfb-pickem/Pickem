-- Remember the FIRST line we ever saw for a game, so movement is a subtraction.
--
-- line_history already records every move, but asking "how far has this line
-- travelled" from it means a join and a window function on every page load. The
-- opening number is a single value per game and it never changes once set, so it
-- belongs on the row.
--
-- Honest naming caveat: this is the first line THIS SYSTEM saw, not the true
-- market open. ESPN exposes an `open` field on its odds endpoint and it is empty
-- for every game checked, across both books it serves, so the real opener is not
-- available to us at any price. First-seen is what we can actually know, and for
-- a week that syncs every six hours from Tuesday it is close enough to be
-- useful.
--
-- Everything already in the table is seeded to its current line, which makes
-- movement exactly zero for them. That is correct rather than convenient: we
-- genuinely do not know which way those lines moved before today.

alter table public.all_games
  add column if not exists line_open numeric;

comment on column public.all_games.line_open is
  'The first line this system recorded for the game. NOT the true market open - '
  'ESPN does not publish one. Movement = line - line_open.';

create or replace function public.remember_line_open()
returns trigger
language plpgsql
as $$
begin
  -- Fires before the row is written, alongside the half-point rounding. Only
  -- ever sets the value once: after that the opener is history and must not move.
  if new.line is not null and new.line_open is null then
    new.line_open := new.line;
  end if;
  return new;
end;
$$;

-- Sorts after trg_half_point_line, so the opener recorded is the settled
-- half-point value rather than whatever ESPN sent.
drop trigger if exists trg_remember_line_open on public.all_games;
create trigger trg_remember_line_open
  before insert or update of line on public.all_games
  for each row execute function public.remember_line_open();

update public.all_games
   set line_open = line
 where line is not null
   and line_open is null;
