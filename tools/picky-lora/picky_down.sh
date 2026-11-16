#!/bin/bash
# Take Picky offline for LoRA training: maintenance replies on,
# Ollama and the image server stopped so nothing else holds a model.
set -u
U=$(id -u); R=/Users/Agent/cfb-pickem; S=/Users/Agent/picky-lora/logs/down_state
log(){ echo "$(date '+%F %T') down: $*" | tee -a /Users/Agent/picky-lora/logs/updown.log; }
# Group posts stay ON: they're written by claude -p and drained via the sidecar's
# /outbox, no Ollama. Only chat replies go to maintenance mode.
: > "$S"
touch /Users/Agent/cfb-pickem-private/PICKY_MAINTENANCE && log "PICKY_MAINTENANCE on"
for L in com.dcc.ollama com.pickem.image; do
  if launchctl print "gui/$U/$L" >/dev/null 2>&1; then
    launchctl bootout "gui/$U/$L" && echo "booted_out=$L" >> "$S" && log "stopped $L"
  fi
done
for i in $(seq 1 30); do pgrep -f "ollama (serve|runner)|llama-server" >/dev/null || break; sleep 2; done
pgrep -fl "ollama|llama-server" && { log "WARNING: ollama still running"; exit 1; }
log "down complete"
