#!/bin/zsh
# Live latency with an exam running, gate off vs on. Call it INSIDE the exam lock:
#   lockf -t 14400 /tmp/picky-exam.lock eval/gate_ab.sh <worktree> <run dir> [n probes] [gap s] [arms]
# Arms (default "idle gate0 gate1"): idle = probes only; gateN = the text exam looping on a side sidecar
# started with PICKY_GATE=N while live_probe.py times live-priority DM asks on a probe sidecar
# (PICKY_ROLE=live, own state). Both sidecars run <worktree>; results in <run dir>/<arm>.json.
WT=$1; D=${2:-/private/tmp/picky-gate-ab}; N=${3:-10}; GAP=${4:-40}; ARMS=${5:-"idle gate0 gate1"}
PY=/Users/Agent/cfb-pickem/server/.venv/bin/python3
PPORT=8820; EPORT=8821
mkdir -p $D
side() {  # side <port> <state dir> <extra env...>: group_chat.py from $WT with all state in <state dir>
  local port=$1 sd=$2; shift 2
  mkdir -p $sd; echo '{}' > $sd/hist.json
  (cd $WT/server && exec env PICKEM_CHAT_MODEL=gemma4:12b-it-qat PICKY_OLLAMA_THINK=false PICKY_GROUP_PORT=$port \
    PICKY_IMESSAGE_HISTORY=$sd/hist.json PICKY_BITS_STATE=$sd/bits.json PICKY_EXCHANGE_LOG=$sd/ex.jsonl \
    PICKY_BRAIN_TRACE=$sd/trace.jsonl PICKY_NO_REPEAT_STATE=$sd/no_repeat.json PICKY_CALLBACKS_STATE=$sd/cb.json \
    PICKY_CALLBACKS_FILE=$sd/cb.json PICKY_LEDGER=$sd/ledger.json PICKY_GROUP_EXAM=1 PICKY_NO_ANTHROPIC=1 \
    PICKY_KEEP_WARM=0 "$@" $PY $WT/server/group_chat.py > $sd/side.log 2>&1) &
  echo $!
}
up() { for i in {1..90}; do curl -s -m 2 http://127.0.0.1:$1/health >/dev/null && return 0; sleep 2; done; return 1; }
PP=$(side $PPORT $D/probe PICKY_ROLE=live)
up $PPORT || { echo "probe sidecar didn't start"; kill $PP; exit 1; }
for arm in ${=ARMS}; do
  EP=""; LOOP=""
  if [[ $arm == gate* ]]; then
    EP=$(side $EPORT $D/exam-$arm PICKY_GATE=${arm#gate})
    up $EPORT || { echo "exam sidecar didn't start"; kill $PP $EP; exit 1; }
    (cd $WT/server && while true; do
       $PY -I eval/run.py --text --group http://127.0.0.1:$EPORT/group --hist $D/exam-$arm/hist.json \
         --tag gate-ab-$arm >> $D/exam-$arm/exam.log 2>&1
     done) &
    LOOP=$!
  fi
  (cd $WT/server && $PY -I eval/live_probe.py --url http://127.0.0.1:$PPORT/group --n $N --gap $GAP \
     --hist $D/probe/hist.json --delay $([[ -n $EP ]] && echo 20 || echo 0) --out $D/$arm.json) | tee $D/$arm.log
  if [[ -n $LOOP ]]; then pkill -P $LOOP; kill $LOOP; fi
  [[ -n $EP ]] && { kill $EP; sleep 2; kill -9 $EP 2>/dev/null; }
  grep -h "gate:" $D/exam-$arm/side.log 2>/dev/null | awk '{$1=$2=""; print}' | sort | uniq -c | sort -rn | head -5
done
kill $PP; sleep 2; kill -9 $PP 2>/dev/null
echo "AB DONE"
