#!/bin/bash
# eval_model.sh <ollama-model> <tag>: text exam (eval/run.py --text) through an
# isolated side group sidecar on :8781 running <ollama-model>. Throwaway history,
# milk-budget state and no maintenance flag, so live Picky's files are untouched.
set -u
MODEL=$1; TAG=$2; S=/Users/Agent/cfb-pickem/server; T=$(mktemp -d /tmp/picky-eval.XXXX)
cp /Users/Agent/cfb-pickem-private/picky_bits_state.json "$T/bits.json" 2>/dev/null || echo '{}' > "$T/bits.json"
echo '{}' > "$T/history.json"
cd "$S"
env PICKY_GROUP_PORT=8781 PICKEM_CHAT_MODEL="$MODEL" PICKY_IMESSAGE_HISTORY="$T/history.json" \
    PICKY_BITS_STATE="$T/bits.json" PICKY_MAINTENANCE_FILE="$T/no-maintenance" \
    ./.venv/bin/python3 group_chat.py > "$T/side.log" 2>&1 &
SP=$!
trap 'kill $SP 2>/dev/null; rm -rf "$T"' EXIT
for i in $(seq 1 60); do curl -sf -m2 http://127.0.0.1:8781/health >/dev/null && break; sleep 1; done
curl -sf -m2 http://127.0.0.1:8781/health >/dev/null || { echo "side sidecar failed"; tail -5 "$T/side.log"; exit 1; }
curl -sf -m 300 http://127.0.0.1:11434/api/generate -d "{\"model\":\"$MODEL\",\"prompt\":\"\",\"keep_alive\":-1}" >/dev/null
./.venv/bin/python3 -I eval/run.py --text --group http://127.0.0.1:8781/group --tag "$TAG"
