-- calc_cfb_week stamps `week` on every ingested game via trg_set_week_on_write.
-- It had two faults:
--
--   1. NO WEEK 0. "Anything before Sep 3 is Week 1" collapsed the Week 0 slate
--      into Week 1. In 2025 that made a six-game Week 1 (Aug 23 + Aug 30-31) and
--      hid late joiners' missed slates. In 2026 ESPN's Aug 26-30 games - the real
--      Week 0 - were all landing in Week 1.
--
--   2. SUNDAY AND MONDAY GAMES ROLLED FORWARD. It bucketed on "the next Sunday
--      strictly after the game", so Sat 2026-09-05 was week 2 while Sun 09-06 and
--      Mon 09-07 - the same football weekend - were week 3.
--
-- New rule, which handles both:
--   Week 1's Saturday = the Saturday before Labor Day (first Monday of September)
--   A game belongs to its CFB week, which runs Tuesday..Monday, so Sunday and
--   Monday games stay with the Saturday they follow.
--   Week number = Saturdays elapsed since Week 1's Saturday, + 1. Week 0 then
--   falls out on its own rather than needing a special case.
--
-- Verified against both seasons before applying: 2025 changes only the two
-- Aug 23 games (1 -> 0), every other 2025 week is byte-identical. 2026 shifts
-- Aug 26-30 to week 0 and pulls the Sep 6-7 games back into week 1 with Sep 5.
create or replace function public.calc_cfb_week(start_ct timestamp without time zone)
returns integer
language plpgsql
immutable
as $function$
declare
  d date;
  season_year int;
  sep1 date;
  labor_day date;
  week1_sat date;
  game_sat date;
begin
  if start_ct is null then
    return null;
  end if;

  d := start_ct::date;

  -- January and February belong to the previous season (bowls, playoff).
  if extract(month from d) in (1,2) then
    season_year := extract(year from d)::int - 1;
  else
    season_year := extract(year from d)::int;
  end if;

  sep1 := make_date(season_year, 9, 1);
  labor_day := sep1 + (((1 - extract(dow from sep1)::int) + 7) % 7);
  week1_sat := labor_day - 2;

  -- The Saturday this game's week is anchored on (week runs Tue..Mon).
  game_sat := case extract(dow from d)::int
                when 0 then d - 1                        -- Sunday -> previous Saturday
                when 1 then d - 2                        -- Monday -> previous Saturday
                else d + (6 - extract(dow from d)::int)  -- Tue..Sat -> upcoming Saturday
              end;

  return ((game_sat - week1_sat) / 7)::int + 1;
end;
$function$;

-- The trigger only fired on UPDATE OF "Start (CT)", so any writer that set `week`
-- directly won - which is exactly how three 2025 games ended up with week NULL
-- and stayed unpickable all season. calc_cfb_week is now always authoritative.
drop trigger if exists trg_set_week_on_write on public.all_games;

create or replace function public.set_week_on_write()
returns trigger
language plpgsql
as $function$
begin
  new."week" := public.calc_cfb_week(new."Start (CT)"::timestamp);
  return new;
end;
$function$;

create trigger trg_set_week_on_write
  before insert or update on public.all_games
  for each row execute function public.set_week_on_write();

-- Re-stamp every existing row through the corrected function. Safe: 2026 has
-- zero picked games and zero picks, so no player's picks move.
update public.all_games
   set week = public.calc_cfb_week(("Start (CT)")::timestamp)
 where "Start (CT)" is not null;
