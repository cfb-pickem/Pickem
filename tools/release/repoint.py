#!/usr/bin/env python3
"""Point Picky's live LaunchAgents at the pinned release worktree (review R4). One-time cutover tool.

  repoint.py [--to /Users/Agent/picky-live] [--from /Users/Agent/cfb-pickem] [--apply] [--labels a,b]

Rewrites ProgramArguments and WorkingDirectory of every ~/Library/LaunchAgents/com.pickem.*.plist that
runs code from the dev tree. Logs stay where they are (server/logs is a symlink in the live tree). The
eval jobs (genius-nightly, genius-refresh) keep running from the dev tree: they build their own worktrees.
Without --apply it only prints the plan. Reloading is the caller's job (release.sh reloads the KeepAlive
services; scheduled jobs pick the new path up at their next run after a bootout+bootstrap).
"""

from __future__ import annotations

import argparse
import plistlib
import subprocess
import sys
from pathlib import Path

AGENTS = Path.home() / "Library" / "LaunchAgents"
KEEP_ON_DEV = {"com.pickem.genius-nightly", "com.pickem.genius-refresh"}


def load(p: Path) -> dict:
    """plutil normalizes first: a few plists have '--' inside an XML comment, which expat rejects."""
    return plistlib.loads(subprocess.run(["/usr/bin/plutil", "-convert", "xml1", "-o", "-", str(p)],
                                         capture_output=True, check=True).stdout)


def plan(src: str, dst: str, labels: set[str] | None = None) -> list[tuple[Path, dict]]:
    out = []
    for p in sorted(AGENTS.glob("com.pickem.*.plist")):
        d = load(p)
        lb = d.get("Label") or p.stem
        if lb in KEEP_ON_DEV or (labels and lb not in labels):
            continue
        args = d.get("ProgramArguments") or []
        new_args = [a.replace(src + "/", dst + "/", 1) if isinstance(a, str) and a.startswith(src + "/") and
                    "/server/.venv" not in a else a for a in args]
        wd = d.get("WorkingDirectory")
        new_wd = wd.replace(src, dst, 1) if isinstance(wd, str) and (wd == src or wd.startswith(src + "/")) else wd
        if new_args != args or new_wd != wd:
            nd = dict(d, ProgramArguments=new_args)
            if wd is not None:
                nd["WorkingDirectory"] = new_wd
            out.append((p, nd))
    return out


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--to", default="/Users/Agent/picky-live")
    ap.add_argument("--from", dest="src", default="/Users/Agent/cfb-pickem")
    ap.add_argument("--labels", default="")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)
    todo = plan(a.src, a.to, set(filter(None, a.labels.split(","))) or None)
    for p, nd in todo:
        print(f"{'repoint' if a.apply else 'would repoint'} {nd.get('Label')}: {' '.join(nd['ProgramArguments'])[:150]}")
        if a.apply:
            p.with_suffix(".plist.pre-live").write_bytes(p.read_bytes())
            p.write_bytes(plistlib.dumps(nd))
    print(" ".join(nd.get("Label") for _, nd in todo))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
