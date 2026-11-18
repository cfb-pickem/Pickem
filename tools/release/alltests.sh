#!/bin/zsh
# The model-free suite (moved into the repo 2026-11-17, review R4; was /Users/Agent/picky-keeper-tools/alltests.sh).
#   alltests.sh <worktree>
# Every server/test_*.py except the live-model ones, plus eval/run.py --no-model and the watcher's own tests.
# Prints "FAILS: none" or "FAILS: <names>"; exit 0 only when nothing failed. Logs: /tmp/kt-<worktree name>-*.log
WT=${1:A}
cd $WT/server || exit 2
PY=./.venv/bin/python3
TAG=$(basename $WT)
fails=()
for t in test_*.py; do
  case $t in test_grounding.py|test_e2e.py) continue;; esac
  PICKY_NO_ANTHROPIC=1 $PY -I $t > /tmp/kt-$TAG-$t.log 2>&1 || fails+=$t
done
for t in ../tools/picky-imessage/test_*.py; do
  /usr/bin/python3 -I $t > /tmp/kt-$TAG-$(basename $t).log 2>&1 || fails+=$(basename $t)
done
$PY -I eval/run.py --no-model > /tmp/kt-$TAG-nomodel.log 2>&1 || fails+=eval-no-model
echo "FAILS: ${fails[*]:-none}"
[ ${#fails} -eq 0 ]
