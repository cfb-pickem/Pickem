"""
Picky's end-to-end watchdog. Runs as Agent every 10 min (LaunchAgent
com.pickem.picky-watchdog, installed by install-watchdog.sh, no sudo).

Checks, in order:
  1. Agent side: the group sidecar (:8768) and chat backend (:8766) answer
     /health. Down and still loaded -> launchctl kickstart. Not loaded means
     someone booted it out on purpose -> leave it alone.
  2. Picky's GUI session exists (a loginwindow owned by `picky`). macOS only
     auto-logs in one user (Agent), and there's no supported way to open a
     second GUI session unattended, so after a reboot this alerts until
     someone logs into Picky once (at the Mac or over Chrome Remote Desktop).
     The moment he's logged in, his watcher LaunchAgent starts on its own.
  3. The watcher's pulse: the sidecar touches cfb-pickem-private/
     picky_watcher_heartbeat on every /outbox poll (every 20 s). Stale while
     the sidecar is up -> restart the watcher inside Picky's session, as
     picky, via `su` with the password already in Agent's keychain
     (CFB_PICKEM__PICKY_MAC_USER_PASSWORD). The watcher's first Apple event
     relaunches Messages too.
  4. The watcher's latest "messages automation check" line has rc=0 (Messages
     answers Apple events, so sends won't hit -1743).
  5. Delivery: any of Picky's sends whose chat.db row shows error != 0 (read
     read-only) -> "delivery failed handle=...XXXX error=N", once per handle
     per day, in the log and cfb-pickem-private/picky_delivery_failures.jsonl.
     Report only, never a resend. (The watcher runs as picky and can't write
     the private dir, so this lives here.)

Alerts go to the coordinator side, not Charlie: ~/Library/Logs/picky-watchdog.log
plus one line appended to the cfb-pickem project memory per problem. Only when
Picky has been down 24h+ and can't be fixed from here does it send ONE text to
Charlie, through the same notify path the LoRA cycle uses.

  /usr/bin/python3 -I watchdog.py            # one pass
  /usr/bin/python3 -I watchdog.py --dry-run  # observe + print the plan, change nothing
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

PRIVATE = Path(os.environ.get("PICKY_PRIVATE_DIR", "/Users/Agent/cfb-pickem-private"))
HEARTBEAT = PRIVATE / "picky_watcher_heartbeat"
STATE_FILE = PRIVATE / "picky_watchdog_state.json"
LOG_FILE = Path.home() / "Library/Logs/picky-watchdog.log"
MEMORY = Path("/Users/Agent/.claude/projects/-Users-Agent/memory/project_cfb_pickem.md")
WATCHER_LOG = Path("/Users/picky/Library/Logs/picky-imessage.log")
WATCHER_LABEL = "com.pickem.picky-imessage"
WATCHER_PLIST = "/Users/picky/Library/LaunchAgents/com.pickem.picky-imessage.plist"
NOTIFY = ["/Users/Agent/cfb-pickem/server/.venv/bin/python3", "-I",
          "/Users/Agent/picky-marathon/wit_eval.py", "notify"]
AGENT_SERVICES = [("com.pickem.picky-group", "http://127.0.0.1:8768/health"),
                  ("com.pickem.chat", "http://127.0.0.1:8766/health")]
CHAT_DB = Path("/Users/picky/Library/Messages/chat.db")
FAILURES = PRIVATE / "picky_delivery_failures.jsonl"
APPLE_EPOCH = 978307200
FAILED_SENDS = """
SELECT m.ROWID, c.chat_identifier, m.error, m.date FROM message m
JOIN chat_message_join j ON j.message_id = m.ROWID
JOIN chat c ON c.ROWID = j.chat_id
WHERE m.is_from_me = 1 AND m.error != 0 AND m.date > ?
ORDER BY m.ROWID
"""
STALE = 300            # s without a watcher poll before we call it dead
BOOT_GRACE = 300       # s after boot to let launchd bring things up before judging
TELL_CHARLIE = 24 * 3600

# Every su step runs as picky against Picky's own launchd domain (gui/<uid>).
SU_EXPECT = r'''
set timeout 30
log_user 0
spawn /usr/bin/su picky -c $env(PICKY_CMD)
expect {
  "Password:" { send -- "$env(PICKY_PW)\r" }
  timeout { exit 98 }
}
log_user 1
expect eof
catch wait r
exit [lindex $r 3]
'''


def log(msg: str) -> None:
    line = f"{dt.datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    print(line)
    with LOG_FILE.open("a") as f:
        f.write(line + "\n")


def sh(*cmd: str, timeout: int = 30, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(list(cmd), capture_output=True, text=True, timeout=timeout, env=env)


def http_ok(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=8) as r:
            return r.status == 200
    except Exception:
        return False


def picky_procs() -> str:
    # `ps -u picky` misses loginwindow (root real uid), so filter the full list by effective user
    out = sh("/bin/ps", "-axo", "user=,command=").stdout.splitlines()
    return "\n".join(ln.split(None, 1)[1] for ln in out if ln.split(None, 1)[0:1] == ["picky"] and " " in ln.strip())


def automation_rc() -> int | None:
    """rc from the watcher's latest startup Messages check (None = no line seen)."""
    try:
        lines = WATCHER_LOG.read_text(errors="replace").splitlines()
    except OSError:
        return None
    for ln in reversed(lines):
        if "messages automation check rc=" in ln:
            try:
                return int(ln.split("rc=", 1)[1].split()[0])
            except ValueError:
                return None
    return None


def observe(now: float) -> dict:
    procs = picky_procs()
    try:
        hb_age = now - HEARTBEAT.stat().st_mtime
    except OSError:
        hb_age = None
    return {
        "services": {lbl: http_ok(url) for lbl, url in AGENT_SERVICES},
        "session": "/loginwindow.app/" in procs,
        "watcher_proc": "picky-imessage/watcher.py" in procs,
        "messages_proc": "/Messages.app/Contents/MacOS/Messages" in procs,
        "heartbeat_age": hb_age,
        "automation_rc": automation_rc(),
    }


def decide(obs: dict, state: dict, now: float) -> tuple[list[str], dict]:
    """Pure: what to do this pass. Actions: kick:<label>, kick_watcher, alert:<key>:<text>,
    tell_charlie:<text>, recovered. Returns (actions, new_state)."""
    st = dict(state)
    acts = [f"kick:{lbl}" for lbl, ok in obs["services"].items() if not ok]
    sidecar_ok = obs["services"].get("com.pickem.picky-group", False)
    hb = obs["heartbeat_age"]
    alive = obs["session"] and hb is not None and hb < STALE
    if alive:
        if "down_since" in st:
            acts.append("recovered")
        bad_rc = obs["automation_rc"] not in (None, 0)
        for k in ("down_since", "told_charlie", "kicks"):
            st.pop(k, None)
        st["alerted"] = ["automation"] if bad_rc and "automation" in st.get("alerted", []) else []
        if bad_rc:
            acts.append(f"alert:automation:Picky's watcher is up but Messages refused its Apple event "
                        f"(rc={obs['automation_rc']}); sends will fail until Automation is re-granted")
        return acts, st
    if not sidecar_ok:  # the watcher can't poll a dead sidecar; fix that first, judge next pass
        return acts, st
    st.setdefault("down_since", now)
    down_for = now - st["down_since"]
    if not obs["session"]:
        acts.append("alert:session:Picky's Mac session is not running (probably a reboot or power loss); "
                    "his iMessage is offline until someone logs into Picky once (at the Mac, or over Chrome "
                    "Remote Desktop: Apple menu > Picky > his password); everything else comes back on its own")
    else:
        st["kicks"] = st.get("kicks", 0) + 1
        acts.append("kick_watcher")
        if st["kicks"] >= 3:
            acts.append("alert:watcher:Picky's watcher keeps going quiet after restarts "
                        "(no /outbox poll for %d min)" % ((hb or down_for) // 60))
    if down_for >= TELL_CHARLIE and not st.get("told_charlie"):
        why = ("his Mac session isn't running, so someone has to log into Picky once "
               "(at the Mac or over Chrome Remote Desktop)"
               if not obs["session"] else "his watcher won't stay up and restarts didn't fix it")
        acts.append(f"tell_charlie:Heads up: Text Picky has been offline for {int(down_for // 3600)}h and "
                    f"I can't bring him back on my own; {why}.")
    return acts, st


def new_failures(rows: list, logged: dict) -> tuple[list[dict], dict]:
    """Pure: failed-send rows (rowid, handle, error, apple date ns) -> events not yet
    reported, one per handle per (message) day. `logged` maps handle -> last day reported."""
    logged, events = dict(logged), []
    for rowid, handle, error, date in rows:
        ts = date / 1e9 + APPLE_EPOCH
        day = dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
        if day <= logged.get(handle, ""):
            continue
        logged[handle] = day
        events.append({"ts": int(ts), "day": day, "handle": handle, "error": error, "rowid": rowid})
    return events, logged


def check_deliveries(state: dict, now: float) -> None:
    since = int((now - 26 * 3600 - APPLE_EPOCH) * 1e9)  # covers a failure just before midnight
    try:
        db = sqlite3.connect(f"file:{CHAT_DB}?mode=ro", uri=True, timeout=5)
        rows = db.execute(FAILED_SENDS, (since,)).fetchall()
        db.close()
    except sqlite3.Error as e:
        log(f"delivery check: chat.db unreadable: {e}")
        return
    events, state["delivery_logged"] = new_failures(rows, state.get("delivery_logged", {}))
    for e in events:
        log(f"delivery failed handle=...{e['handle'][-4:]} error={e['error']}")
        with FAILURES.open("a") as f:
            f.write(json.dumps(e) + "\n")


def su_picky(cmd: str) -> subprocess.CompletedProcess:
    pw = sh("/usr/bin/security", "find-generic-password", "-a", "drake", "-s",
            "CFB_PICKEM__PICKY_MAC_USER_PASSWORD", "-w").stdout.strip()
    env = dict(os.environ, PICKY_PW=pw, PICKY_CMD=cmd)
    return sh("/usr/bin/expect", "-c", SU_EXPECT, timeout=60, env=env)


def kick_watcher() -> bool:
    uid = sh("/usr/bin/id", "-u", "picky").stdout.strip()
    cmd = (f"/bin/launchctl kickstart -k gui/{uid}/{WATCHER_LABEL} 2>/dev/null || "
           f"/bin/launchctl bootstrap gui/{uid} {WATCHER_PLIST}")
    r = su_picky(cmd)
    log(f"kick watcher rc={r.returncode} {r.stdout.strip()[-200:]}")
    return r.returncode == 0


def kick_agent(label: str) -> None:
    dom = f"gui/{os.getuid()}"
    if sh("/bin/launchctl", "print", f"{dom}/{label}").returncode != 0:
        log(f"{label} unhealthy but not loaded (booted out on purpose?), leaving it")
        return
    r = sh("/bin/launchctl", "kickstart", "-k", f"{dom}/{label}")
    log(f"kickstart {label} rc={r.returncode}")


def remember(text: str) -> None:
    with MEMORY.open("a") as f:
        f.write(f"\n- {dt.datetime.now():%Y-%m-%d %H:%M} CT picky-watchdog: {text}\n")


def uptime(now: float) -> float:
    out = sh("/usr/sbin/sysctl", "-n", "kern.boottime").stdout  # "{ sec = 1763..., usec = ... } ..."
    try:
        return now - int(out.split("sec =", 1)[1].split(",", 1)[0])
    except (IndexError, ValueError):
        return BOOT_GRACE


def main(argv: list[str]) -> int:
    dry = "--dry-run" in argv
    now = time.time()
    if not dry and uptime(now) < BOOT_GRACE:  # RunAtLoad at login: services are still starting
        return 0
    try:
        state = json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        state = {}
    obs = observe(now)
    acts, new = decide(obs, state, now)
    hb = obs["heartbeat_age"]
    summary = (f"session={obs['session']} watcher={obs['watcher_proc']} messages={obs['messages_proc']} "
               f"pulse={'none' if hb is None else f'{int(hb)}s'} "
               f"services={','.join(k.split('.')[-1] + ('=ok' if v else '=DOWN') for k, v in obs['services'].items())}"
               f" automation_rc={obs['automation_rc']}")
    if dry:
        print(summary)
        print("plan:", acts or ["nothing"])
        return 0
    if acts:
        log(f"{summary} -> {acts}")
    alerted = set(new.get("alerted", []))
    for a in acts:
        if a.startswith("kick:"):
            kick_agent(a[5:])
        elif a == "kick_watcher":
            kick_watcher()
        elif a.startswith("alert:"):
            _, key, text = a.split(":", 2)
            log(f"ALERT {text}")
            if key not in alerted:  # one memory line per problem per outage
                remember(text)
                alerted.add(key)
        elif a.startswith("tell_charlie:"):
            r = sh(*NOTIFY, a.split(":", 1)[1], timeout=90)
            log(f"told Charlie rc={r.returncode} {r.stdout.strip()[-80:]}")
            new["told_charlie"] = r.returncode == 0
        elif a == "recovered":
            down = int((now - state.get("down_since", now)) // 60)
            log(f"Picky back after ~{down} min")
            if state.get("alerted"):
                remember(f"Picky's iMessage is back after ~{down} min.")
    new["alerted"] = sorted(alerted)
    check_deliveries(new, now)
    STATE_FILE.write_text(json.dumps(new))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
