-- Only the slate.
--
-- A player finished a week with five picks out of four. The fifth was on a
-- game the commissioner had taken off the board after it was picked, and
-- nothing anywhere said a pick had to be on a game that was actually in the
-- contest: the picks page only ever offers slate games, so the rule existed
-- purely as a habit of the UI. Any signed-in member could also write one from
-- the console - supabase.from('picks').upsert({ game_id: <any game> }) - and
-- the deadline policies would wave it through, because they only ask WHEN.
--
-- This asks WHICH. A pick may only be written for a game that is on a slate:
-- an all_games row with picked or tiebreaker set. The page filters to the same
-- set before saving (js/pickSave.js), so a player never sees this error in
-- ordinary use; it is the backstop, the same relationship the deadline
-- policies have with the page's lock.
--
-- WHAT THIS DOES NOT DO: remove a pick that is already sitting on a game that
-- has since been un-picked. That is the commissioner changing all_games, not a
-- player writing picks, and a trigger on picks cannot see it happen. The picks
-- page now sweeps the player's own strays for the open week when it saves.

-- 1. WHY A TRIGGER AND NOT ANOTHER RESTRICTIVE POLICY ----------------------
--
-- The policies on this table are the house style and a policy would be the
-- natural fit, except for one thing: a policy failure comes back as "new row
-- violates row-level security policy", which is the same message as every
-- other refusal on this table and tells the player nothing. A trigger can say
-- exactly what is wrong, and the picks page shows error.message verbatim.
--
-- WHO IT CHECKS. Players only - current_user is anon or authenticated. Two
-- writers must never be stopped by this, and both are covered by that test
-- rather than by naming them:
--
--   * resolve_slot_picks() is security definer, so its UPDATE runs as the
--     table owner. If a slotted game were un-picked before kickoff, refusing
--     the resolver's update would abort the whole pass - every other slots
--     pick in the league, and the jackpot roll with them.
--   * The commissioner, who is exempt from the deadline for the same reason
--     here: fixing the record after the fact is their job.
--
-- Which is also why the function is deliberately NOT security definer: as a
-- definer it would always see its owner as current_user and check nobody.

create or replace function public.picks_only_on_the_slate()
returns trigger
language plpgsql
set search_path = public
as $$
begin
  if current_user not in ('anon', 'authenticated') then
    return new;
  end if;

  -- An update that leaves the game, the pick and the slots flag alone is not
  -- a player choosing anything (updated_at, say), so it is not this
  -- trigger's business.
  if tg_op = 'UPDATE'
     and new.game_id  is not distinct from old.game_id
     and new.pick     is not distinct from old.pick
     and new.by_slots is not distinct from old.by_slots then
    return new;
  end if;

  if public.is_commissioner() then
    return new;
  end if;

  if not exists (
    select 1
      from public.all_games g
     where g."GameId" = new.game_id
       and (coalesce(g.picked, false) or coalesce(g.tiebreaker, false))
  ) then
    raise exception 'Game % is not on this week''s slate, so it cannot be picked.', new.game_id
      using errcode = 'check_violation',
            hint    = 'Reload the picks page - the commissioner may have changed this week''s games.';
  end if;

  return new;
end;
$$;

comment on function public.picks_only_on_the_slate() is
  'Refuses a player''s pick on any game that is not picked or a tiebreaker. Skips the commissioner and every non-player role, which keeps resolve_slot_picks() (security definer) out of its reach.';

-- 2. THE TRIGGER ------------------------------------------------------------
--
-- BEFORE, so a refused row never reaches the other two triggers on this table.
-- Order among BEFORE triggers is alphabetical by name; this one sorts ahead of
-- picks_updated_at_trigger and trg_slots_jackpot_pull, so a rejected pick
-- never bumps the slots meter on its way to being thrown out. (The exception
-- would roll that back anyway; this just means it never happens at all.)
--
-- `update of` is not used: the column list would have to be kept in step with
-- the early return above by hand, and the early return already does the job.
drop trigger if exists picks_only_on_the_slate on public.picks;
create trigger picks_only_on_the_slate
  before insert or update on public.picks
  for each row execute function public.picks_only_on_the_slate();

-- 3. NOTHING TO BACKFILL ----------------------------------------------------
--
-- Checked before this was written: every existing row in public.picks is on a
-- picked or tiebreaker game, so the trigger starts out agreeing with the data.
-- If that ever stops being true it will not complain about the old rows - it
-- only looks at rows being written.
