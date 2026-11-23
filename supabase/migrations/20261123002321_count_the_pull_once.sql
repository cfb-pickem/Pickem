-- Count a slots pull once, not once per save.
--
-- THE BUG. picks.html saves with upsert(onConflict: 'team_id,game_id'), which
-- is INSERT ... ON CONFLICT DO UPDATE. Postgres runs BEFORE INSERT row
-- triggers on the proposed row BEFORE it checks for the conflict, so every
-- re-save of an existing slots pick went through slots_jackpot_pull()'s
-- INSERT branch: flag forced false, pulls_since_pop + 1. The conflict then
-- turned it into an UPDATE, whose BEFORE UPDATE pass restored the row's flag
-- from OLD - so the row looked counted once while the meter kept the extra
-- +1. Every save (and every autosave) of a week with a slots pick in it
-- bumped the league meter. On 2026-11-22 the meter read 257 against 113 rows
-- ever flagged as a counted pull, with no pop ever recorded.
--
-- THE FIX. The BEFORE trigger keeps doing what only a BEFORE trigger can -
-- writing the flag onto the row - but no longer touches the meter. The meter
-- moves in AFTER triggers, which only fire for the path the row actually
-- took: an upsert that conflicts fires AFTER UPDATE, never AFTER INSERT. So a
-- pull is counted exactly when a row's flag goes from absent/false to true.
--
-- Nothing is removed: the existing trg_slots_jackpot_pull keeps its name and now calls
-- the flag-only body below; the two counting triggers are new.
--
-- This does not correct the meter's current value. That changes live jackpot
-- odds and is Charlie's call.

create or replace function public.slots_jackpot_pull()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
begin
  -- ONE ROW, ONE PULL. On insert, never trust a client value; on update the
  -- flag can only go from false to true, so a pull cannot be handed back and
  -- spent again. The meter itself moves in slots_jackpot_count() (AFTER).
  if tg_op = 'INSERT' then
    new.slots_pull_counted := false;
  else
    new.slots_pull_counted := coalesce(old.slots_pull_counted, false);
  end if;

  if new.by_slots and new.pick is null and not new.slots_pull_counted then
    new.slots_pull_counted := true;
  end if;

  return new;
end;
$$;

create or replace function public.slots_jackpot_count()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
begin
  update public.slots_jackpot
     set pulls_since_pop = pulls_since_pop + 1
   where id = 1;
  return null;
end;
$$;

create or replace trigger trg_slots_jackpot_count_ins
  after insert on public.picks
  for each row
  when (new.slots_pull_counted)
  execute function public.slots_jackpot_count();

create or replace trigger trg_slots_jackpot_count_upd
  after update on public.picks
  for each row
  when (new.slots_pull_counted and not coalesce(old.slots_pull_counted, false))
  execute function public.slots_jackpot_count();
