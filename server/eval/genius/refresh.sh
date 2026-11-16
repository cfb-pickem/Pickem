#!/bin/zsh
# Tue + Fri 12:30 CT (LaunchAgent com.pickem.genius-refresh): rebuild the genius
# exam from the current slate (fact pack + Opus-written keys) into cfb-pickem-private/genius.
cd /Users/Agent/cfb-pickem/server && exec ./.venv/bin/python3 -I eval/genius/refresh.py >> /Users/Agent/cfb-pickem-private/genius/refresh.log 2>&1
