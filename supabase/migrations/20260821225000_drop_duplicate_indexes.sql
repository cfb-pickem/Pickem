-- Duplicate indexes. Space isn't the concern at 471 rows - write amplification
-- is. all_games carried FIVE separate indexes on ("GameId"), so the
-- every-minute ESPN ingest paid five index maintenance operations per row for
-- one column's worth of lookup.
--
-- What's kept, and why:
--   all_games_pkey            - backs the edge functions' on_conflict=GameId
--   all_games_picked_week_idx - 10,351 scans; carries the real leaderboard query
--   picks_team_game_uidx      - backs on_conflict=team_id,game_id in picks.html
--                               and commissioner.html
--   picks_game_id_idx         - 17,115 scans
--   playoffs_pkey             - backs on_conflict=id in commissioner.html
--   teams_name_idx            - 213 scans

-- all_games: four redundant copies of ("GameId")
alter table public.all_games drop constraint if exists all_games_gameid_key;
alter table public.all_games drop constraint if exists all_games_gameid_unique;
drop index if exists public.all_games_gameid_idx;
drop index if exists public.idx_all_games_gameid;

-- ("Status") has 165 scans and no query filters on it alone, but being indexed
-- breaks HOT on every score update - 19,981 of 129,235 updates were non-HOT.
-- Dropping it should push the HOT rate toward 100% on the every-minute ingest.
drop index if exists public.idx_all_games_status;

-- picks: (team_id, game_id) uniqueness already implies (team_id, week, game_id),
-- and there were two copies of each.
drop index if exists public.picks_team_game_key;
alter table public.picks drop constraint if exists picks_team_week_game_unique;
alter table public.picks drop constraint if exists picks_unique;

-- playoffs: the UNIQUE constraint already covers (season, seed) lookups.
drop index if exists public.playoffs_season_seed_idx;

-- teams: teams_user_id_key duplicates teams_user_id_unique (0 scans each).
--
-- teams_id_key is deliberately KEPT even though it duplicates the primary key:
-- picks_team_id_fkey and playoffs_team_fk are both attached to that index rather
-- than to teams_pkey, so dropping it needs CASCADE - which would take the two
-- foreign keys with it. Not worth losing referential integrity over one
-- redundant index on a 13-row table that is almost never written.
alter table public.teams drop constraint if exists teams_user_id_key;
