#!/bin/bash
# Bring Picky back after training: Ollama + image server, maintenance off,
# health checks. KEEP_MAINT=1 leaves maintenance replies on (for the eval).
set -u
U=$(id -u); R=/Users/Agent/cfb-pickem; S=/Users/Agent/picky-lora/logs/down_state
log(){ echo "$(date '+%F %T') up: $*" | tee -a /Users/Agent/picky-lora/logs/updown.log; }
pgrep -f "mlx_lm" >/dev/null && { log "mlx_lm still running, refusing"; exit 1; }
for L in com.dcc.ollama com.pickem.image; do
  launchctl print "gui/$U/$L" >/dev/null 2>&1 || { launchctl bootstrap "gui/$U" "$HOME/Library/LaunchAgents/$L.plist" && log "started $L"; }
done
for i in $(seq 1 30); do curl -sf http://127.0.0.1:11434/api/tags >/dev/null && break; sleep 2; done
curl -sf http://127.0.0.1:11434/api/tags >/dev/null && log "ollama up" || log "ERROR: ollama not answering"
MODEL=$(plutil -extract EnvironmentVariables.PICKEM_CHAT_MODEL raw "$HOME/Library/LaunchAgents/com.pickem.chat.plist" 2>/dev/null || echo qwen3.5:9b-q4_K_M)
curl -sf -m 180 http://127.0.0.1:11434/api/generate -d "{\"model\":\"$MODEL\",\"prompt\":\"\",\"keep_alive\":-1,\"options\":{\"num_ctx\":16384}}" >/dev/null && log "warmed $MODEL"
if [ "${KEEP_MAINT:-0}" = 1 ]; then
  log "PICKY_MAINTENANCE kept on (eval next)"
else
  rm -f /Users/Agent/cfb-pickem-private/PICKY_MAINTENANCE && log "PICKY_MAINTENANCE off"
fi
for U2 in 8766/health 8767/healthz 8768/health; do
  curl -s -o /dev/null -w "%{http_code}" -m 10 "http://127.0.0.1:$U2" | grep -q 200 && log ":$U2 healthy" || log "WARNING :$U2 health check failed"
done
log "up complete"
