#!/bin/zsh
# Genius-exam side sidecar: group_chat.py from a worktree, all state in a temp dir,
# so an exam never touches live history/ledgers/bits. Run it INSIDE the exam lock
# and kill it before the lock is released.
#   side.sh <worktree> <port> <run dir>
WT=$1; PORT=${2:-8791}; D=${3:-/private/tmp/picky-genius-run}
mkdir -p $D; echo '{}' > $D/hist.json
cd $WT/server || exit 1
export PICKEM_CHAT_MODEL=gemma4:12b-it-qat PICKY_OLLAMA_THINK=false PICKY_GROUP_PORT=$PORT \
  PICKY_IMESSAGE_HISTORY=$D/hist.json PICKY_BITS_STATE=$D/bits.json PICKY_EXCHANGE_LOG=$D/ex.jsonl \
  PICKY_BRAIN_TRACE=$D/trace.jsonl PICKY_NO_REPEAT_STATE=$D/no_repeat.json PICKY_CALLBACKS_STATE=$D/callbacks.json \
  PICKY_CALLBACKS_FILE=$D/callbacks.json PICKY_LEDGER=$D/ledger.json PICKY_GROUP_EXAM=1 PICKY_NO_ANTHROPIC=1
exec /Users/Agent/cfb-pickem/server/.venv/bin/python3 $WT/server/group_chat.py
