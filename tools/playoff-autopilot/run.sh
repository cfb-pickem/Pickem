#!/bin/zsh
# Playoff autopilot tests on a throwaway local Postgres (never production).
#   brew install postgresql@17   (once)
#   tools/playoff-autopilot/run.sh
# Starts a temp cluster on port 54329, installs bootstrap.sql (a minimal mirror
# of the prod tables the autopilot reads) + the migration, runs test.sql, and
# stops the cluster again.
set -e
export LC_ALL=${LC_ALL:-en_US.UTF-8}
here=${0:A:h}; root=${here:h:h}
PGBIN=${PGBIN:-/opt/homebrew/opt/postgresql@17/bin}
data=$(mktemp -d /tmp/po-autopilot-pg.XXXXXX)
$PGBIN/initdb -D $data -U postgres --auth=trust -E UTF8 >/dev/null
$PGBIN/pg_ctl -D $data -o "-p 54329 -k $data -c listen_addresses=''" -l $data/log start >/dev/null
trap "$PGBIN/pg_ctl -D $data stop -m fast >/dev/null; rm -rf $data" EXIT
sleep 1
P=($PGBIN/psql -h $data -p 54329 -U postgres -d postgres -v ON_ERROR_STOP=1 -q -tA)
$P -f $here/bootstrap.sql
$P -f $root/supabase/migrations/20261116120000_playoff_autopilot.sql 2>&1 | grep -v NOTICE || true
$P -f $root/supabase/migrations/20261123190000_last_pool_week_per_season.sql 2>&1 | grep -v NOTICE || true
$P -f $here/test.sql 2>&1 | grep -E "ERROR|passed|CONTEXT"
