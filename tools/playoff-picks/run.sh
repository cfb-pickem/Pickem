#!/bin/zsh
# Playoff pick gate (supabase/migrations/20261116140000_playoff_picks_field_only.sql),
# tested end to end over HTTP on a throwaway copy. Never touches production data.
#
#   brew install postgresql@17   (once)
#   POSTGREST=/path/to/postgrest tools/playoff-picks/run.sh
#
# 1. temp Postgres on :54331 with mirror.sql (prod's real picks policies,
#    triggers and helper functions)
# 2. a READ-ONLY copy of prod's teams, playoffs and 2025-26 slate games
#    (Management API select; nothing is written to prod)
# 3. a real PostgREST on :54332 in front of it, and test.mjs sends the same
#    upserts picks.html sends, signed as real members, before and after the
#    migration.
set -e
export LC_ALL=${LC_ALL:-en_US.UTF-8}
here=${0:A:h}; root=${here:h:h}
PGBIN=${PGBIN:-/opt/homebrew/opt/postgresql@17/bin}
POSTGREST=${POSTGREST:?set POSTGREST to a postgrest binary}
LIBPQ=${LIBPQ:-$(dirname $(find /opt/homebrew/Cellar/postgresql@17 -name 'libpq.5.dylib' | head -1))}
TOK=$(security find-generic-password -a drake -s CFB_PICKEM__SUPABASE_ACCESS_TOKEN -w)
REF=vopdioszofwdkwnujtiq
data=$(mktemp -d /tmp/po-picks-pg.XXXXXX)
$PGBIN/initdb -D $data -U postgres --auth=trust -E UTF8 >/dev/null
$PGBIN/pg_ctl -D $data -o "-p 54331 -k $data -c listen_addresses=localhost" -l $data/log start >/dev/null
pgrst_pid=
trap '[[ -n $pgrst_pid ]] && kill $pgrst_pid 2>/dev/null; $PGBIN/pg_ctl -D $data stop -m fast >/dev/null; rm -rf $data' EXIT
sleep 1
P=($PGBIN/psql -h $data -p 54331 -U postgres -d postgres -v ON_ERROR_STOP=1 -q -tA)
$P -f $here/mirror.sql >/dev/null

prod() {  # read-only select against prod, returns the first column of the first row
  jq -n --arg q "$1" '{query:$q}' | curl -sf -X POST https://api.supabase.com/v1/projects/$REF/database/query \
    -H "Authorization: Bearer $TOK" -H 'Content-Type: application/json' -d @- | jq -c '.[0].j'
}
prod "select json_agg(t) j from (select team_id, team_name, user_id, commissioner, active, is_exhibition from teams) t" > $data/teams.json
prod "select json_agg(t) j from (select id, team_id, season, seed, created_at, source from playoffs) t" > $data/playoffs.json
prod "select json_agg(t) j from (select \"GameId\", \"Start (CT)\", \"Away\", \"Home\", \"AwayPts\", \"HomePts\", \"Status\", line, winner, picked, week, cfb_season, tiebreaker from all_games where cfb_season in (2025, 2026) and (picked or tiebreaker or week > 15)) t" > $data/games.json
prod "select json_agg(t) j from (select team_id, week, game_id, pick from picks where game_id in (select \"GameId\" from all_games where cfb_season = 2025 and week > 15)) t" > $data/picks.json
$P -v teams="$(cat $data/teams.json)" -v po="$(cat $data/playoffs.json)" -v games="$(cat $data/games.json)" -v picks="$(cat $data/picks.json)" <<'SQL'
insert into teams select * from json_populate_recordset(null::teams, :'teams');
insert into all_games select * from json_populate_recordset(null::all_games, :'games');
insert into playoffs select * from json_populate_recordset(null::playoffs, :'po');
alter table picks disable trigger user;
insert into picks (team_id, week, game_id, pick) select team_id, week, game_id, pick from json_populate_recordset(null::picks, :'picks');
alter table picks enable trigger user;
select format('copied: %s teams, %s games, %s seeds, %s playoff picks', (select count(*) from teams), (select count(*) from all_games), (select count(*) from playoffs), (select count(*) from picks));
SQL

secret=$(openssl rand -hex 32)
cat > $data/pgrst.conf <<CONF
db-uri = "postgres://authenticator@localhost:54331/postgres"
db-schemas = "public"
db-anon-role = "anon"
jwt-secret = "$secret"
server-port = 54332
server-host = "127.0.0.1"
CONF
DYLD_FALLBACK_LIBRARY_PATH=$LIBPQ $POSTGREST $data/pgrst.conf > $data/pgrst.log 2>&1 &
pgrst_pid=$!
for i in {1..40}; do curl -sf http://127.0.0.1:54332/ >/dev/null 2>&1 && break; sleep 0.25; done

PSQL="${P[*]}" MIGRATION=$root/supabase/migrations/20261116140000_playoff_picks_field_only.sql \
  JWT_SECRET=$secret API=http://127.0.0.1:54332 node $here/test.mjs
