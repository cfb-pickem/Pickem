-- Playoff autopilot tests (supabase/migrations/20261116120000_playoff_autopilot.sql).
--
-- Run against a THROWAWAY database that has the playoff-related tables and the
-- migration installed (tools/playoff-autopilot/run.sh builds one from
-- bootstrap.sql and runs this file). Never point this at production: it
-- inserts a synthetic season 2030 and every test rolls back, but don't.
--
-- Fixture: season 2030, weeks 1-15, 4 picked games a week (60 games), the home
-- side always covers. Team i picks the home side (a win) in its first pts[i]
-- games and the away side after that. Week 15 kicks off 2024-12-07 (past dates, so the real clock sees the season as over); "now" is
-- 2024-12-20 unless a test says otherwise. T1 is the commissioner.

\set ON_ERROR_STOP 1
set client_min_messages = warning;

create or replace function pg_temp.setup(pts int[], p_inactive_nopicks boolean default false)
returns void language plpgsql as $$
declare i int; w int; k int; gid bigint; idx int;
begin
  for i in 1..cardinality(pts) loop
    insert into teams (team_id, team_name, user_id, commissioner, active)
    values (9000+i, 'T'||i, ('00000000-0000-0000-0000-00000000000'||i)::uuid, i = 1, true);
  end loop;
  insert into teams (team_id, team_name, active, is_exhibition) values (9099, 'PickyBot', true, true);
  if p_inactive_nopicks then
    insert into teams (team_id, team_name, active) values (9098, 'Gone', false);
  end if;
  for w in 1..15 loop
    for k in 1..4 loop
      gid := 2030000 + w*10 + k;
      insert into all_games ("GameId","Start (CT)","Away","Home","Status",line,winner,picked,week,cfb_season,tiebreaker)
      values (gid, '2024-08-31 11:00'::timestamp + ((w-1)*7) * interval '1 day' + (k-1) * interval '2 hours',
              'A'||w||'_'||k, 'H'||w||'_'||k, 'final', -3, 'H'||w||'_'||k, true, w, 2030, false);
      idx := (w-1)*4 + k;
      for i in 1..cardinality(pts) loop
        insert into picks (team_id, game_id, pick)
        values (9000+i, gid, case when idx <= pts[i] then 'H'||w||'_'||k else 'A'||w||'_'||k end);
      end loop;
      -- The exhibition bot is perfect and must still never be ranked.
      insert into picks (team_id, game_id, pick) values (9099, gid, 'H'||w||'_'||k);
    end loop;
  end loop;
  insert into playoff_autopilot (season, enabled) values (2030, true);
end $$;

-- A tiebreaker game; p_winner null = flagged but not graded.
create or replace function pg_temp.tb(gid bigint, wk int, away text, home text, p_winner text)
returns void language sql as $$
  insert into all_games ("GameId","Start (CT)","Away","Home","Status",line,winner,picked,week,cfb_season,tiebreaker)
  values (gid, '2024-12-06 12:00'::timestamp + (coalesce(wk,15)-15) * interval '7 days', away, home,
          case when p_winner is null then 'scheduled' else 'final' end, -1, p_winner, false, wk, 2030, true);
$$;
create or replace function pg_temp.pick(team int, gid bigint, p text) returns void language sql as $$
  insert into picks (team_id, game_id, pick) values (9000+team, gid, p);
$$;
create or replace function pg_temp.res(p_now timestamptz default '2024-12-20 12:00-06')
returns jsonb language sql as $$ select playoff_compute(2030, false, p_now) $$;
create or replace function pg_temp.tick(p_now timestamptz default '2024-12-20 12:00-06')
returns jsonb language sql as $$ select playoff_autopilot_tick(p_now) $$;
create or replace function pg_temp.seeds() returns text language sql as $$
  select coalesce(string_agg(t.team_name, ',' order by p.seed), '') from playoffs p join teams t using (team_id) where p.season = 2030
$$;
create or replace function pg_temp.codes(r jsonb) returns text language sql as $$
  select coalesce(string_agg(h->>'code', ',' order by h->>'code'), '') from jsonb_array_elements(r->'holds') h
$$;

-- The tick works on the newest season with picked games. The snapshot's
-- seasons are older than 2030, so the fixture season is "current".

-- 1. Clean finish: distinct points, flips once, seeds by points, logged.
begin;
select pg_temp.setup(array[50,45,40,35,30,25]);
do $$ declare r jsonb; begin
  r := pg_temp.res();
  assert (r->>'ready')::boolean, 'clean: ready ' || r::text;
  r := pg_temp.tick();
  assert (r->>'acted')::boolean, 'clean: tick acted ' || r::text;
  assert pg_temp.seeds() = 'T1,T2,T3,T4', 'clean: seeds ' || pg_temp.seeds();
  assert (select bool_and(source = 'auto') from playoffs where season = 2030), 'clean: source auto';
  assert (select state from playoff_autopilot where season = 2030) = 'playoffs', 'clean: state';
  assert (select count(*) from playoff_autopilot_log where season = 2030 and action = 'switch') = 1, 'clean: logged';
end $$;
-- 2. Idempotent: a second and third tick change nothing.
do $$ declare r jsonb; n int; begin
  n := (select count(*) from playoff_autopilot_log where season = 2030);
  r := pg_temp.tick(); r := pg_temp.tick();
  assert not (r->>'acted')::boolean, 'idempotent: acted again';
  assert (select count(*) from playoffs where season = 2030) = 4, 'idempotent: rows';
  assert (select count(*) from playoff_autopilot_log where season = 2030) = n, 'idempotent: log grew';
end $$;
rollback;

-- 3. Disabled (preview only): computes, never writes seeds.
begin;
select pg_temp.setup(array[50,45,40,35,30,25]);
update playoff_autopilot set enabled = false where season = 2030;
do $$ declare r jsonb; begin
  r := pg_temp.tick();
  assert not (r->>'acted')::boolean and (r->>'ready')::boolean, 'disabled: ' || r::text;
  assert pg_temp.seeds() = '', 'disabled: wrote seeds';
  assert (select preview->'qualifiers'->0->>'team_name' from playoff_autopilot where season = 2030) = 'T1', 'disabled: preview stored';
  assert exists (select 1 from playoff_autopilot_log where season = 2030 and action = 'preview_only'), 'disabled: logged';
end $$;
rollback;

-- 4. Late-graded game: holds while a week 15 game has no winner, flips once it's graded.
begin;
select pg_temp.setup(array[50,45,40,35,30,25]);
update all_games set winner = null, "Status" = 'in' where "GameId" = 2030154;
do $$ declare r jsonb; begin
  r := pg_temp.tick();
  assert pg_temp.codes(r) = 'ungraded', 'late: holds ' || r::text;
  assert pg_temp.seeds() = '', 'late: flipped early';
  assert (select state from playoff_autopilot where season = 2030) = 'holding', 'late: state';
  update all_games set winner = 'H15_4', "Status" = 'final' where "GameId" = 2030154;
  r := pg_temp.tick();
  assert (r->>'acted')::boolean and pg_temp.seeds() = 'T1,T2,T3,T4', 'late: after grading ' || r::text;
end $$;
rollback;

-- 5. Settle window, in-progress season, missing week.
begin;
select pg_temp.setup(array[50,45,40,35,30,25]);
do $$ declare r jsonb; begin
  r := pg_temp.res('2024-12-07 20:00-06');            -- last kickoff 17:00, settle until 05:00 next day
  assert pg_temp.codes(r) = 'settling', 'settle: ' || r::text;
  r := pg_temp.res('2024-12-08 05:01-06');
  assert (r->>'ready')::boolean, 'settle: ready after 12h ' || r::text;
  r := pg_temp.res('2024-12-07 12:00-06');            -- week 15 still kicking off
  assert pg_temp.codes(r) = 'season_in_progress' and r->>'phase' = 'regular', 'in progress: ' || r::text;
  r := pg_temp.tick('2024-12-07 12:00-06');
  assert (select state from playoff_autopilot where season = 2030) = 'regular' and pg_temp.seeds() = '', 'in progress: tick';
  delete from picks where game_id between 2030071 and 2030074;
  delete from all_games where week = 7 and cfb_season = 2030;
  r := pg_temp.res();
  assert pg_temp.codes(r) = 'missing_week', 'missing week: ' || r::text;
end $$;
rollback;

-- 6. Week 15 not picked at all.
begin;
select pg_temp.setup(array[50,45,40,35,30,25]);
delete from picks where game_id between 2030151 and 2030154;
delete from all_games where week = 15 and cfb_season = 2030;
do $$ declare r jsonb; begin
  r := pg_temp.res();
  assert pg_temp.codes(r) = 'season_in_progress', 'no week 15: ' || r::text;
end $$;
rollback;

-- 7. Tie at the cut broken by conference championship picks.
begin;
select pg_temp.setup(array[50,45,40,35,35,25]);
select pg_temp.tb(2030901, 15, 'Conf A', 'Conf B', 'Conf B');
select pg_temp.pick(5, 2030901, 'Conf B');            -- T5 right, T4 wrong
select pg_temp.pick(4, 2030901, 'Conf A');
do $$ declare r jsonb; begin
  r := pg_temp.res();
  assert (r->>'ready')::boolean, 'conf tie: ' || r::text;
  assert r->'cut_tie'->>'decided_by' = 'conf', 'conf tie: decided_by';
  assert r->'qualifiers'->3->>'team_name' = 'T5', 'conf tie: T5 in ' || (r->'qualifiers')::text;
  assert not (r->>'army_navy_used')::boolean, 'conf tie: army-navy unused';
end $$;
rollback;

-- 8. Tie that needs Army-Navy: holds until it's graded, then flips; Army-Navy marked used.
begin;
select pg_temp.setup(array[50,45,40,35,35,25]);
select pg_temp.tb(2030901, 15, 'Conf A', 'Conf B', 'Conf B');
select pg_temp.pick(4, 2030901, 'Conf B');
select pg_temp.pick(5, 2030901, 'Conf B');            -- conf: level
select pg_temp.tb(2030902, 16, 'Army', 'Navy', null);
select pg_temp.pick(4, 2030902, 'Army');
select pg_temp.pick(5, 2030902, 'Navy');
do $$ declare r jsonb; begin
  r := pg_temp.tick();
  assert pg_temp.codes(r) = 'cut_tie_pending' and r->'holds'->0->>'waiting_on' = 'army_navy', 'army-navy: holds ' || r::text;
  assert (r->'holds'->0->>'message') like '%Army v Navy%graded%', 'army-navy: message';
  assert pg_temp.seeds() = '', 'army-navy: flipped early';
  update all_games set winner = 'Navy', "Status" = 'final' where "GameId" = 2030902;
  r := pg_temp.tick();
  assert (r->>'acted')::boolean, 'army-navy: flips after grading ' || r::text;
  assert pg_temp.seeds() = 'T1,T2,T3,T5', 'army-navy: seeds ' || pg_temp.seeds();
  assert (select army_navy_used from playoff_autopilot where season = 2030), 'army-navy: used flag';
end $$;
rollback;

-- 9. Tie with no tiebreaker games flagged: nobody could pick them, so they
--    can't separate anyone and it goes straight to chips. No hold.
begin;
select pg_temp.setup(array[50,45,40,35,35,25]);
insert into blackjack_bankrolls (team_id, cfb_season, chips) values (9004, 2030, 800), (9005, 2030, 1200);
do $$ declare r jsonb; begin
  r := pg_temp.res();
  assert (r->>'ready')::boolean and r->'cut_tie'->>'decided_by' = 'chips', 'unflagged -> chips: ' || r::text;
  assert r->'qualifiers'->3->>'team_name' = 'T5', 'unflagged: T5 has more chips';
end $$;
rollback;

-- 9b. Ungraded tiebreaker games the tied teams picked the SAME way (or not at
--     all) can't break the tie either: no waiting. Picked differently: wait;
--     a commissioner force skips the wait.
begin;
select pg_temp.setup(array[50,45,40,35,35,25]);
insert into blackjack_bankrolls (team_id, cfb_season, chips) values (9004, 2030, 800), (9005, 2030, 1200);
select pg_temp.tb(2030901, 15, 'Conf A', 'Conf B', null);
select pg_temp.pick(4, 2030901, 'Conf B');
select pg_temp.pick(5, 2030901, 'Conf B');
select pg_temp.pick(1, 2030901, 'Conf A');                  -- not in the tie: irrelevant
select pg_temp.tb(2030902, 16, 'Army', 'Navy', null);       -- flagged, nobody in the tie picked it
do $$ declare r jsonb; begin
  r := pg_temp.res();
  assert (r->>'ready')::boolean and r->'cut_tie'->>'decided_by' = 'chips', 'same picks -> chips: ' || r::text;
  update picks set pick = 'Conf A' where team_id = 9005 and game_id = 2030901;
  r := pg_temp.res();
  assert pg_temp.codes(r) = 'cut_tie_pending' and r->'holds'->0->>'waiting_on' = 'conf', 'different picks -> hold: ' || r::text;
  assert (r->'holds'->0->>'message') like '%conference championship game wins, which hasn''t been graded yet.', 'hold message: ' || (r->'holds'->0->>'message');
  r := playoff_compute(2030, true, '2024-12-20 12:00-06');
  assert (r->>'ready')::boolean and r->'cut_tie'->>'decided_by' = 'chips', 'force skips the wait: ' || r::text;
end $$;
rollback;

-- 10. Chips decide when the football tiebreakers are graded and level; a
--     missing bankroll row counts as the 1,000-chip starting stack.
begin;
select pg_temp.setup(array[50,45,40,35,35,25]);
select pg_temp.tb(2030901, 15, 'Conf A', 'Conf B', 'Conf B');
select pg_temp.tb(2030902, 16, 'Army', 'Navy', 'Army');
select pg_temp.tb(2030903, 16, 'Jackson State', 'South Carolina State', 'Jackson State');
insert into blackjack_bankrolls (team_id, cfb_season, chips) values (9004, 2030, 999);   -- T5 has no row = 1000
do $$ declare r jsonb; begin
  r := pg_temp.res();
  assert (r->>'ready')::boolean and r->'cut_tie'->>'decided_by' = 'chips', 'chips: ' || r::text;
  assert r->'qualifiers'->3->>'team_name' = 'T5', 'chips: T5 (1000 default) over T4 (999)';
  assert r->'tiebreakers'->>'celebration' = 'graded', 'celebration recognised';
end $$;
rollback;

-- 11. Celebration Bowl, then last 8, then coin flip.
begin;
select pg_temp.setup(array[50,45,40,35,35,25]);
select pg_temp.tb(2030901, 15, 'Conf A', 'Conf B', 'Conf B');
select pg_temp.tb(2030902, 16, 'Army', 'Navy', 'Army');
select pg_temp.tb(2030903, 16, 'Jackson State', 'South Carolina State', null);
select pg_temp.pick(4, 2030903, 'Jackson State');
select pg_temp.pick(5, 2030903, 'South Carolina State');
do $$ declare r jsonb; begin
  -- no Pit this season (no bankroll rows): chips skipped, Celebration Bowl pending -> hold
  r := pg_temp.res();
  assert pg_temp.codes(r) = 'cut_tie_pending' and r->'holds'->0->>'waiting_on' = 'celebration', 'celebration pending: ' || r::text;
  update all_games set winner = 'Jackson State', "Status" = 'final' where "GameId" = 2030903;
  r := pg_temp.res();
  assert (r->>'ready')::boolean and r->'cut_tie'->>'decided_by' = 'celebration'
     and r->'qualifiers'->3->>'team_name' = 'T4', 'celebration decides: ' || r::text;
  delete from picks where game_id = 2030903;
  -- T4 and T5 have identical pick sequences -> coin flip
  r := pg_temp.res();
  assert pg_temp.codes(r) = 'coin_flip', 'coin flip: ' || r::text;
  r := playoff_compute(2030, true, '2024-12-20 12:00-06');
  assert not (r->>'ready')::boolean, 'coin flip: force must refuse';
  -- give T5 a better last 8 (flip its week 14 game 1 loss into a win, and an early win into a loss)
  update picks set pick = 'H14_1' where team_id = 9005 and game_id = 2030141;
  update picks set pick = 'A1_1'  where team_id = 9005 and game_id = 2030011;
  r := pg_temp.res();
  assert (r->>'ready')::boolean and r->'cut_tie'->>'decided_by' = 'last8', 'last8: ' || r::text;
  assert r->'qualifiers'->3->>'team_name' = 'T5', 'last8: T5';
end $$;
rollback;

-- 12. Three-way tie across the cut for two spots.
begin;
select pg_temp.setup(array[50,45,40,40,40,25]);
select pg_temp.tb(2030901, 15, 'Conf A', 'Conf B', 'Conf B');
select pg_temp.tb(2030904, 15, 'Conf C', 'Conf D', 'Conf D');
select pg_temp.pick(3, 2030901, 'Conf A'); select pg_temp.pick(3, 2030904, 'Conf C');   -- T3: 0
select pg_temp.pick(4, 2030901, 'Conf B'); select pg_temp.pick(4, 2030904, 'Conf D');   -- T4: 2
select pg_temp.pick(5, 2030901, 'Conf B'); select pg_temp.pick(5, 2030904, 'Conf C');   -- T5: 1
do $$ declare r jsonb; begin
  r := pg_temp.res();
  assert (r->>'ready')::boolean, '3-way: ' || r::text;
  assert (r->'cut_tie'->>'slots')::int = 2, '3-way: slots';
  assert r->'qualifiers'->2->>'team_name' = 'T4' and r->'qualifiers'->3->>'team_name' = 'T5', '3-way: ' || (r->'qualifiers')::text;
end $$;
rollback;

-- 13. Member with missing picks, an inactive no-show, the exhibition bot,
--     jackpot auto-wins, tiebreaker picks and playoff-week games.
begin;
select pg_temp.setup(array[50,45,40,35,30,25], true);
delete from picks where team_id = 9002 and game_id in (select "GameId" from all_games where week >= 13 and cfb_season = 2030);
update picks set jackpot = true where team_id = 9006 and game_id = 2030153;            -- a loss that auto-wins
select pg_temp.tb(2030901, 15, 'Conf A', 'Conf B', 'Conf B');
select pg_temp.pick(6, 2030901, 'Conf B');                                              -- not a point
insert into all_games ("GameId","Start (CT)","Away","Home","Status",line,winner,picked,week,cfb_season,tiebreaker)
values (2030990, '2024-12-19 18:00', 'Bowl A', 'Bowl B', 'final', -3, 'Bowl B', true, 17, 2030, false);
insert into picks (team_id, game_id, pick) values (9006, 2030990, 'Bowl B');           -- not a regular-season point
do $$ declare r jsonb; s jsonb; begin
  r := pg_temp.res();
  assert (r->>'ready')::boolean, 'misc: ' || r::text;
  select jsonb_object_agg(e->>'team_name', e) into s from jsonb_array_elements(r->'standings') e;
  assert (s->'T2'->>'points')::int = 45, 'missing picks: points kept ' || (s->'T2')::text;
  assert (s->'T2'->>'l8n')::int = 8, 'missing picks: last 8 from own decided picks';
  assert (s->'T6'->>'points')::int = 26, 'jackpot counts, tiebreaker + week 17 do not: ' || (s->'T6')::text;
  assert not (s ? 'PickyBot'), 'exhibition never ranked';
  assert not (s ? 'Gone'), 'inactive team with no picks left out';
  assert pg_temp.seeds() = '', 'compute writes nothing';
end $$;
rollback;

-- 14. Hand-entered seeds always win: the tick stands down.
begin;
select pg_temp.setup(array[50,45,40,35,30,25]);
insert into playoffs (season, seed, team_id) values (2030, 1, 9006), (2030, 2, 9005), (2030, 3, 9004), (2030, 4, 9003);
do $$ declare r jsonb; begin
  r := pg_temp.tick();
  assert not (r->>'acted')::boolean and r->>'state' = 'playoffs', 'manual: ' || r::text;
  assert pg_temp.seeds() = 'T6,T5,T4,T3', 'manual: seeds untouched';
  assert (select bool_and(source = 'commissioner') from playoffs where season = 2030), 'manual: source';
end $$;
rollback;

-- 15. Commissioner controls: permission, pause, force, undo, resume.
begin;
select pg_temp.setup(array[50,45,40,35,30,25]);
update all_games set winner = null, "Status" = 'in' where "GameId" = 2030154;            -- holding
grant usage on schema public to authenticated;
set local role authenticated;
select set_config('request.jwt.claim.sub', '00000000-0000-0000-0000-000000000002', true), set_config('request.jwt.claim.role', 'authenticated', true);  -- T2: not commissioner
do $$ begin
  begin
    perform playoff_autopilot_admin(2030, 'force');
    assert false, 'non-commissioner could force';
  exception when insufficient_privilege then null;
  end;
end $$;
select set_config('request.jwt.claim.sub', '00000000-0000-0000-0000-000000000001', true), set_config('request.jwt.claim.role', 'authenticated', true);  -- T1: commissioner
do $$ declare r jsonb; begin
  r := playoff_autopilot_admin(2030, 'pause');
  assert (select not enabled from playoff_autopilot where season = 2030), 'pause';
  r := playoff_autopilot_admin(2030, 'force');                          -- ungraded game, forced anyway
  assert (r->>'ok')::boolean, 'force: ' || r::text;
  assert (select switched_by from playoff_autopilot where season = 2030) = 'commissioner', 'force: by';
  r := playoff_autopilot_admin(2030, 'force');
  assert not (r->>'ok')::boolean, 'force twice must refuse';
  r := playoff_autopilot_admin(2030, 'undo');
  assert (r->>'ok')::boolean and jsonb_array_length(r->'removed') = 4, 'undo: ' || r::text;
  assert (select count(*) from playoffs where season = 2030) = 0, 'undo: rows gone';
  assert (select state from playoff_autopilot where season = 2030) = 'regular', 'undo: state';
  assert exists (select 1 from playoff_autopilot_log where season = 2030 and action = 'undo' and actor = 'T1'), 'undo: logged with actor';
end $$;
reset role;
do $$ declare r jsonb; begin
  update all_games set winner = 'H15_4', "Status" = 'final' where "GameId" = 2030154;
  r := pg_temp.tick();
  assert not (r->>'acted')::boolean and pg_temp.seeds() = '', 'undo leaves autopilot paused';
end $$;
set local role authenticated;
select set_config('request.jwt.claim.sub', '00000000-0000-0000-0000-000000000001', true), set_config('request.jwt.claim.role', 'authenticated', true);
do $$ declare r jsonb; begin
  r := playoff_autopilot_admin(2030, 'resume');
  assert (r->'tick'->>'acted')::boolean, 'resume flips at once when ready: ' || r::text;
end $$;
reset role;
do $$ begin assert pg_temp.seeds() = 'T1,T2,T3,T4', 'resume: seeds'; end $$;
rollback;

-- 16. anon can preview but not tick or act.
begin;
select pg_temp.setup(array[50,45,40,35,30,25]);
set local role anon;
select set_config('request.jwt.claim.role', 'anon', true);
do $$ declare r jsonb; begin
  r := playoff_compute(2030, false, '2024-12-20 12:00-06');
  assert (r->>'ready')::boolean, 'anon preview';
  begin perform playoff_autopilot_tick(); assert false, 'anon tick';
  exception when insufficient_privilege then null; end;
  begin perform playoff_autopilot_admin(2030, 'force'); assert false, 'anon admin';
  exception when insufficient_privilege then null; end;
end $$;
reset role;
rollback;

-- 8. 2026: the last pool week is 14 (conference championships Dec 5); week 15 (Army-Navy) has no pool games.
begin;
select pg_temp.setup(array[50,45,40,35,30,25]);
delete from picks where game_id between 2030151 and 2030154;
delete from all_games where week = 15 and cfb_season = 2030;
update all_games set cfb_season = 2026 where cfb_season = 2030;
do $$ declare r jsonb; begin
  assert last_pool_week(2026) = 14 and last_pool_week(2025) = 15 and last_pool_week(2030) = 15, 'last_pool_week';
  r := playoff_compute(2026, false, '2024-12-20 12:00-06');
  assert (r->>'ready')::boolean and pg_temp.codes(r) = '', '2026 weeks 1-14 done: ready ' || r::text;
  r := playoff_compute(2026, false, '2024-11-30 10:00-06');
  assert pg_temp.codes(r) = 'season_in_progress' and r::text like '%Week 14 hasn''t finished%', '2026 week 14 not kicked: ' || r::text;
  r := playoff_compute(2026, false, '2024-11-30 20:00-06');
  assert pg_temp.codes(r) = 'settling', '2026: 12h settle after week 14: ' || r::text;
end $$;
delete from picks where game_id between 2030141 and 2030144;
delete from all_games where week = 14 and cfb_season = 2026;
do $$ declare r jsonb; begin
  r := playoff_compute(2026, false, '2024-12-20 12:00-06');
  assert pg_temp.codes(r) = 'season_in_progress', '2026 week 14 not picked: never early ' || r::text;
end $$;
rollback;

select 'playoff autopilot: all tests passed';
