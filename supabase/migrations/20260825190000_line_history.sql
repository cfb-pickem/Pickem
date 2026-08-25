-- Remember where the line has been, not just where it is.
--
-- The market data itself is already here: sync_espn_lines() reads ESPN's
-- scoreboard odds, and the book behind those is DraftKings. So there is nothing
-- to add on the "get live betting lines" front, and a direct sportsbook scraper
-- would be strictly worse - unofficial, undocumented, and liable to break on a
-- quiet Tuesday.
--
-- What is genuinely missing is the SHAPE of the week. A line that opened at -3.5
-- and sits at -7.5 on Saturday is telling you something a static -7.5 never
-- can, and every one of those intermediate values has been passing through this
-- table's reach every six hours and getting thrown away.
--
-- Done as an AFTER trigger rather than inside the sync, for the same reason the
-- rounding is a trigger: the sync is not the only writer. It fires after
-- trg_half_point_line, so what gets recorded is the settled half-point value
-- rather than the raw one.
--
-- Size is a non-issue: a row only lands when the number actually MOVES, so a
-- season is a few thousand rows, not a few hundred thousand.

create table if not exists public.line_history (
  id         bigserial primary key,
  game_id    bigint not null,
  line       numeric not null,
  seen_at    timestamptz not null default now()
);

create index if not exists line_history_game_idx
  on public.line_history (game_id, seen_at);

alter table public.line_history enable row level security;

-- Readable by the site (the picks page shows the move), written only by the
-- trigger, which runs as the table owner.
drop policy if exists line_history_read on public.line_history;
create policy line_history_read on public.line_history
  for select to anon, authenticated using (true);

create or replace function public.record_line_move()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
begin
  -- Only picked and tiebreaker games are worth tracking, and only real moves.
  if (coalesce(new.picked, false) or coalesce(new.tiebreaker, false))
     and new.line is not null
     and new.line is distinct from old.line then
    insert into public.line_history (game_id, line) values (new."GameId", new.line);
  end if;
  return null;                 -- AFTER trigger, return value is ignored
end;
$$;

drop trigger if exists trg_record_line_move on public.all_games;
create trigger trg_record_line_move
  after update of line on public.all_games
  for each row execute function public.record_line_move();

-- Seed the current state so today's number is the start of every game's history
-- rather than a gap. Only games that do not have a row yet.
insert into public.line_history (game_id, line)
select g."GameId", g.line
  from public.all_games g
 where (coalesce(g.picked,false) or coalesce(g.tiebreaker,false))
   and g.line is not null
   and not exists (select 1 from public.line_history h where h.game_id = g."GameId");
