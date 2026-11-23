#!/bin/zsh
# Nightly / on-demand genius exam on latest main (LaunchAgent com.pickem.genius-nightly).
# Detached worktree at main -> side sidecar inside the exam lock -> Opus judge
# (outside the lock) -> one summary line in cfb-pickem-private/genius/history.jsonl.
# Never texts anyone. Skips quietly if Anthropic is off (judge needs it) or the
# repo is unavailable.
/usr/bin/python3 -I ${0:A:h}/../../freeze.py check-exam >&2 || exit 3  # no exams Sat 10:00-24:00 CT (R9)
set -u
REPO=/Users/Agent/cfb-pickem
WT=/private/tmp/picky-wt-genius-nightly
TAG=nightly-$(date +%Y%m%d-%H%M)
LOG=/Users/Agent/cfb-pickem-private/genius/nightly.log
exec >> $LOG 2>&1
echo "== $TAG"
# Fresh worktree every run: macOS's daily /tmp cleaner deletes files nobody touched for 3 days, and on
# 2026-11-23 it took this worktree's .git file ("fatal: not a git repository", no exam that night).
git -C $REPO worktree remove --force $WT 2>/dev/null; rm -rf $WT; git -C $REPO worktree prune
git -C $REPO worktree add -q --detach $WT main || exit 1
ln -sfn $REPO/server/cache $WT/server/cache
/usr/bin/lockf -t 14400 /tmp/picky-exam.lock $WT/server/eval/genius/exam.sh $WT $TAG || { echo "exam failed"; exit 1; }
cd $WT/server && $REPO/server/.venv/bin/python3 -I eval/genius/judge.py eval/genius/results/$TAG.json --history
