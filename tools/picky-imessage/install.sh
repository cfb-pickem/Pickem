#!/bin/bash
# Picky's iMessage user + watcher. Run as Agent (NOT with sudo in front):
#   bash ~/cfb-pickem/tools/picky-imessage/install.sh
# It asks for Agent's Mac password once (sudo), and, the first time, a new
# password for the Picky Mac user. Safe to re-run: it just refreshes the files.
set -euo pipefail
[ "$(id -un)" = "Agent" ] || { echo "Run this as Agent, without sudo in front."; exit 1; }
HERE="$(cd "$(dirname "$0")" && pwd)"
SECRET="$(security find-generic-password -a drake -s CFB_PICKEM__PICKY_GROUP_SECRET -w)"
[ -n "$SECRET" ] || { echo "No CFB_PICKEM__PICKY_GROUP_SECRET in the keychain."; exit 1; }
# watcher config (kept out of the public repo): Picky's own Apple ID, and
# Charlie's handles from the private map as the only DM testers
CONFIG="$(PICKY_EMAIL="$(security find-generic-password -a drake -s CFB_PICKEM__PICKY_APPLEID_EMAIL -w)" \
  /usr/bin/python3 -I -c '
import json, os
m = json.load(open("/Users/Agent/cfb-pickem-private/picky_group_members.json"))
tests = [h for x in m["members"] if x.get("team") == "Chuck P" for h in x.get("handles") or []]
print(json.dumps({"self_handles": [os.environ["PICKY_EMAIL"]], "test_dm_handles": tests}))')"

sudo -v
if ! id picky >/dev/null 2>&1; then
  echo "Creating the Picky Mac user (standard, not admin)."
  echo "Pick a password for it; you'll type it when you log into Picky."
  sudo sysadminctl -addUser picky -fullName "Picky" -password -
fi
[ -d /Users/picky ] || sudo createhomedir -c -u picky >/dev/null

# fast user switching on, so Picky can stay logged in behind Agent
sudo defaults write /Library/Preferences/.GlobalPreferences MultipleSessionEnabled -bool true

sudo install -d -o picky -g staff -m 700 /Users/picky/picky-imessage /Users/picky/.picky-imessage
sudo install -d -o picky -g staff -m 755 /Users/picky/Library /Users/picky/Library/LaunchAgents /Users/picky/Library/Logs
sudo install -o picky -g staff -m 600 "$HERE/watcher.py" /Users/picky/picky-imessage/watcher.py
sudo install -o picky -g staff -m 644 "$HERE/com.pickem.picky-imessage.plist" \
  /Users/picky/Library/LaunchAgents/com.pickem.picky-imessage.plist
printf '%s' "$SECRET" | sudo tee /Users/picky/.picky-imessage/secret >/dev/null
sudo chown picky:staff /Users/picky/.picky-imessage/secret
sudo chmod 600 /Users/picky/.picky-imessage/secret
printf '%s' "$CONFIG" | sudo tee /Users/picky/.picky-imessage/config.json >/dev/null
sudo chown picky:staff /Users/picky/.picky-imessage/config.json
sudo chmod 600 /Users/picky/.picky-imessage/config.json

# if Picky is already logged in (a re-run), restart the watcher in his session
UID_P="$(id -u picky)"
if sudo launchctl print "gui/$UID_P" >/dev/null 2>&1; then
  sudo launchctl bootout "gui/$UID_P/com.pickem.picky-imessage" 2>/dev/null || true
  sudo launchctl bootstrap "gui/$UID_P" /Users/picky/Library/LaunchAgents/com.pickem.picky-imessage.plist
  echo "Watcher restarted in Picky's session."
else
  echo "Watcher installed; it starts when Picky logs in."
fi
echo "Done. Next: docs/picky-imessage-setup.md, step 2."
