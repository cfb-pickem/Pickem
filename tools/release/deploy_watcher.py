#!/usr/bin/env python3
"""Deploy tools/picky-imessage/watcher.py into Picky's macOS session (no sudo, no install.sh).

  deploy_watcher.py <worktree> [--dry-run]

The `picky` user can't read Agent's home and Agent can't read his, so the file is staged world-readable in
/private/tmp, then copied as picky through `su` (expect + the password in Agent's keychain, the same path
the watchdog uses), with a backup watcher.py.bak-<sha>, and his LaunchAgent is kickstarted. Verified by a
fresh sidecar heartbeat (his /outbox poll) and his log's "messages automation check rc=0" after the kick.
Exit 0 = deployed and verified.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "picky-imessage"))
import watchdog  # noqa: E402  (su_picky, log paths, labels)

STAGE = Path("/private/tmp/picky-watcher-stage")
DEST = "/Users/picky/picky-imessage/watcher.py"


def main(argv: list[str]) -> int:
    wt = Path(argv[0]).resolve()
    src = wt / "tools" / "picky-imessage" / "watcher.py"
    sha = hashlib.sha1(src.read_bytes()).hexdigest()[:10]
    STAGE.mkdir(mode=0o755, exist_ok=True)
    os.chmod(STAGE, 0o755)
    staged = STAGE / "watcher.py"
    shutil.copyfile(src, staged)
    os.chmod(staged, 0o644)
    uid = subprocess.run(["/usr/bin/id", "-u", "picky"], capture_output=True, text=True).stdout.strip()
    cmd = (f"cp {DEST} {DEST}.bak-$(date +%Y%m%d%H%M%S) 2>/dev/null; cp {staged} {DEST}.new && "
           f"mv {DEST}.new {DEST} && /bin/launchctl kickstart -k gui/{uid}/{watchdog.WATCHER_LABEL}")
    if "--dry-run" in argv:
        print(f"[dry-run] would deploy watcher {sha} as picky: {cmd}")
        return 0
    t0 = time.time()
    r = watchdog.su_picky(cmd)
    print(f"su picky rc={r.returncode} {r.stdout.strip()[-200:]}")
    if r.returncode:
        return 1
    ok_hb = ok_rc = False
    for _ in range(45):
        time.sleep(2)
        try:
            ok_hb = watchdog.HEARTBEAT.stat().st_mtime > t0 + 1
        except OSError:
            pass
        try:
            tail = watchdog.WATCHER_LOG.read_text(errors="replace").splitlines()[-200:]
            for ln in reversed(tail):
                m = re.match(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)", ln)
                if m and "messages automation check rc=" in ln:
                    when = time.mktime(time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S"))
                    ok_rc = when >= t0 - 2 and "rc=0" in ln
                    break
        except OSError:
            pass
        if ok_hb and ok_rc:
            break
    print(f"watcher {sha}: heartbeat={'fresh' if ok_hb else 'STALE'} automation={'rc=0' if ok_rc else 'NOT SEEN'}")
    return 0 if ok_hb and ok_rc else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
