#!/bin/bash
# Guard a training run. Stop it if: free memory < 6%; swap grows > 2 GB over its
# settled level (max seen in the first 4 minutes, which covers the first steps'
# allocation spike); or free disk < 5 GiB. Keeps only the 2 newest checkpoints.
# Usage: memwatch.sh <pid> [adapter-dir]
P=$1; D=/Users/Agent/picky-lora; AD=${2:-$D/adapters}; LOG=$D/logs/memwatch.log
sw(){ sysctl -n vm.swapusage | sed -E 's/.*used = ([0-9.]+)M.*/\1/'; }
T0=$(date +%s); SETTLED=0; LIM=""
echo "$(date +%H:%M:%S) start pid=$P swap=$(sw)M" >> "$LOG"
while kill -0 "$P" 2>/dev/null; do
  SW=$(sw); FREE=$(memory_pressure | awk -F': ' '/free percentage/ {gsub("%","",$2); print $2}')
  DISK=$(df -k /System/Volumes/Data | awk 'NR==2{printf "%.1f", $4/1048576}')
  if [ -z "$LIM" ]; then
    SETTLED=$(awk -v a="$SETTLED" -v b="$SW" 'BEGIN{print (b>a)?b:a}')
    [ $(( $(date +%s) - T0 )) -ge 240 ] && LIM=$(awk -v s="$SETTLED" 'BEGIN{print s+2048}') \
      && echo "$(date +%H:%M:%S) swap settled at ${SETTLED}M, limit ${LIM}M" >> "$LOG"
  fi
  echo "$(date +%H:%M:%S) swap=${SW}M free=${FREE}% disk=${DISK}G" >> "$LOG"
  WHY=$(awk -v s="$SW" -v l="${LIM:-1e12}" -v f="$FREE" -v d="$DISK" \
    'BEGIN{ if (f<6) print "free<6%"; else if (s>l) print "swap>limit"; else if (d<5) print "disk<5G" }')
  if [ -n "$WHY" ]; then
    echo "$(date +%H:%M:%S) LIMIT HIT ($WHY) swap=${SW}M free=${FREE}% disk=${DISK}G -> stopping $P" >> "$LOG"
    kill -INT "$P"; sleep 20; kill -9 "$P" 2>/dev/null
    echo "$WHY" > "$D/logs/MEM_ABORT"; exit 1
  fi
  ls -t "$AD"/0*_adapters.safetensors 2>/dev/null | tail -n +3 | xargs rm -f
  sleep 5
done
