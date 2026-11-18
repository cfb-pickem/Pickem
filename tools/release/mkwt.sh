#!/bin/zsh
# mkwt.sh <dir> <ref>: a detached worktree of /Users/Agent/cfb-pickem at <ref> that runs like the main tree:
# server/.venv, .venv-flux, cache and logs are symlinks to the main tree's (gitignored, shared data).
set -e
REPO=/Users/Agent/cfb-pickem
D=$1; REF=$2
if [ -e $D ]; then git -C $REPO worktree remove --force $D 2>/dev/null || rm -rf $D; fi
git -C $REPO worktree prune
git -C $REPO worktree add -q --detach $D $REF
for x in .venv .venv-flux cache logs; do
  [ -e $REPO/server/$x ] && ln -sfn $REPO/server/$x $D/server/$x
done
echo $D
