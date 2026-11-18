#!/bin/zsh
# One genius-exam run against a side sidecar started from <worktree>. Call it
# INSIDE the exam lock:  lockf -t 5400 /tmp/picky-exam.lock exam.sh <worktree> <tag> [run.py args]
# The sidecar (port $GENIUS_PORT, default 8791) is killed before this returns.
/usr/bin/python3 -I ${0:A:h}/../../freeze.py check-exam >&2 || exit 3  # no exams Sat 10:00-24:00 CT (R9)
WT=$1; TAG=$2; shift 2
PORT=${GENIUS_PORT:-8791}
D=${GENIUS_RUN_DIR:-/private/tmp/picky-genius-run}/$TAG; mkdir -p $D
HERE=${0:A:h}
$HERE/side.sh $WT $PORT $D > $D/side.log 2>&1 &
SP=$!
for i in {1..90}; do curl -s -m 2 http://127.0.0.1:$PORT/health >/dev/null && break; sleep 2; done
cd $HERE/../.. && /Users/Agent/cfb-pickem/server/.venv/bin/python3 -I eval/genius/run.py \
  --side http://127.0.0.1:$PORT/group --hist $D/hist.json --tag $TAG "$@"
RC=$?
kill $SP 2>/dev/null; sleep 2; kill -9 $SP 2>/dev/null
echo "EXAM DONE rc=$RC"
exit $RC
