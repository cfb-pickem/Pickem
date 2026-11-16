#!/bin/bash
# Picky's end-to-end watchdog (watchdog.py), as Agent, every 10 min. No sudo.
#   bash ~/cfb-pickem/tools/picky-imessage/install-watchdog.sh
set -euo pipefail
[ "$(id -un)" = "Agent" ] || { echo "Run this as Agent."; exit 1; }
HERE="$(cd "$(dirname "$0")" && pwd)"
PLIST="$HOME/Library/LaunchAgents/com.pickem.picky-watchdog.plist"
cp "$HERE/com.pickem.picky-watchdog.plist" "$PLIST"
launchctl bootout "gui/$(id -u)/com.pickem.picky-watchdog" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "Installed. Log: ~/Library/Logs/picky-watchdog.log  Dry run: /usr/bin/python3 -I $HERE/watchdog.py --dry-run"
