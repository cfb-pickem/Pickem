# Picky in the league iMessage group: setup

Picky gets his own macOS user (`picky`) on the Mac mini with his own iMessage
account (his Apple ID email is in the keychain as `CFB_PICKEM__PICKY_APPLEID_EMAIL`). Agent's Messages and the coordinator aren't touched.

How it works:

- **Watcher** (`tools/picky-imessage/watcher.py`, LaunchAgent `com.pickem.picky-imessage`,
  runs in the picky user only). Reads `/Users/picky/Library/Messages/chat.db` read-only
  every 3 s. It acts only on group-chat messages containing `@Picky` (also `@ Picky`, any
  case, or a mention picked from the @ menu), plus DMs from Charlie for testing (handles in `~/.picky-imessage/config.json`, written by install.sh).
  Replies to the same chat through Messages via AppleScript. Limits: one reply per 10 s,
  60 an hour. Log: `/Users/picky/Library/Logs/picky-imessage.log`, short previews only.
- **Group sidecar** (`server/group_chat.py`, LaunchAgent `com.pickem.picky-group` in
  Agent, `127.0.0.1:8768` only, shared secret in keychain `CFB_PICKEM__PICKY_GROUP_SECRET`).
  It runs chat.py's normal Picky pipeline with group privacy: nobody's unlocked picks,
  not even the sender's, and nobody's pick status. Replies are 1 to 3 sentences.
- **Who's who** comes from `/Users/Agent/cfb-pickem-private/picky_group_members.json`,
  which is private and never in this repo. Fill in each member's phone or Apple ID email
  and first name. Changes take effect without a restart. Anyone not listed counts as a
  generic league member.

## Checklist (do these at the Mac mini, in order)

1. **Create the Picky user and install the watcher.** Log into the Mac as **Agent**,
   open Terminal and run:
   ```
   bash ~/cfb-pickem/tools/picky-imessage/install.sh
   ```
   - First it asks for **Agent's Mac password** (sudo).
   - Then it asks you to **choose a password for the new Picky user**. Write it down;
     you need it in step 2.
   - Re-run it after filling in your own handles in the private map (step 11) if you
     want to DM-test from an email address too; your phone is already in there.
2. **Log into Picky once.** Apple menu > **Lock Screen** (or click your name in the
   menu bar's fast-user-switching menu) > choose **Picky** > enter the password from
   step 1. Skip or "Set Up Later" every setup screen: no Apple ID at the system level,
   no Siri, no Screen Time, no analytics.
3. **Sign Messages into Picky's Apple ID.** Still logged in as Picky, open **Messages** >
   sign in with **Picky's Apple ID email** and the password Charlie set.
   - If it asks for a verification code, it goes to Picky's Gmail inbox
     (or a trusted phone, if one was set). Open that inbox on your phone and type the code in.
   - Messages > Settings > iMessage: make sure **Picky's email is checked** under
     "You can be reached for messages at" and is also the "Start new conversations from" address.
4. **Grant Full Disk Access to the watcher's Python** (so it can read chat.db).
   Still as Picky: System Settings > Privacy & Security > **Full Disk Access** > **+** >
   press **Cmd+Shift+G** and paste:
   ```
   /Library/Developer/CommandLineTools/usr/bin/python3
   ```
   > Open > make sure its switch is **on**. Type Agent's admin name and password when asked.
   - If the log later says `chat.db unreadable (Full Disk Access?)`, also add this one the same way:
     `/Library/Developer/CommandLineTools/Library/Frameworks/Python3.framework/Versions/Current/Resources/Python.app`
   - Heads up: this access covers that Python for every user on the Mac.
5. **Restart the watcher so it picks up the access.** Still as Picky, open Terminal and run:
   ```
   launchctl kickstart -k gui/$(id -u)/com.pickem.picky-imessage
   ```
6. **Approve the Automation prompt.** Within a few seconds a box should say "python3"
   wants to control "Messages". Click **Allow** (or **OK**).
   - If no box shows up: System Settings > Privacy & Security > **Automation** > python3 >
     turn **Messages** on, then run step 5 again.
7. **Test it.** From your iPhone, text Picky's email: `@Picky who's winning?`
   (Charlie's handles from the private map are allowed to DM for testing). A reply should arrive within about 30 s.
   If it doesn't, check the log as Picky: `tail -20 ~/Library/Logs/picky-imessage.log`.
8. **Leave Picky logged in behind Agent.** Apple menu > Lock Screen > log back into **Agent**.
   Don't choose "Log Out" for Picky; switching users leaves him running in the background.
9. **After any reboot**, Picky is logged out. macOS only auto-logs-in one user (Agent), and
   there's no supported way to open a second user's session unattended. Do step 2 (log in
   as Picky; works over Chrome Remote Desktop too), then step 8. Nothing else: his watcher
   starts at his login, and the Agent-side watchdog (`install-watchdog.sh`, LaunchAgent
   com.pickem.picky-watchdog, every 10 min) restarts the sidecar/chat or his watcher if
   they go quiet and logs to `~/Library/Logs/picky-watchdog.log`. If Picky stays logged
   out for 24 h it texts Charlie once.
10. **Add Picky to the league group.** From an iPhone in the group: open the group >
    tap the group name at the top > **Add Contacts** > type Picky's email.
    - Every member has to be on iMessage (all blue bubbles). If anyone is green (SMS/RCS),
      iPhone won't let you add a person, so make a new iMessage-only group with Picky in it.
    - Optional: save Picky's email as a contact named "Picky" so it shows his name.
11. **Fill in who's who** (optional, makes Picky know the sender): edit
    `/Users/Agent/cfb-pickem-private/picky_group_members.json` and put each member's
    phone/email and first name next to their team.

Picky only answers messages with **@Picky** in them.

## Updating later

Re-run `bash ~/cfb-pickem/tools/picky-imessage/install.sh` as Agent after the watcher
changes; it refreshes the files and restarts the watcher if Picky is logged in. The
sidecar restarts with `launchctl kickstart -k gui/$(id -u)/com.pickem.picky-group` (as Agent).
