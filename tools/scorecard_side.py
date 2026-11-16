"""
Launch one side sidecar (server/group_chat.py) from ANY ref's worktree for tools/scorecard.py.

    python3 -I tools/scorecard_side.py --server /private/tmp/picky-wt-before/server [--at ISO [--replay]]

The caller sets the env (PICKY_GROUP_PORT, state files in its run dir, PICKY_GROUP_EXAM=1). This
wrapper only patches the ref's modules in this process, so it works for refs that predate any
scorecard support:
  - exam chats: group texts arrive on chat ids starting "exam-". Refs with PICKY_GROUP_EXAM honor the
    env; older refs (league-group-only gate, no env) get group_outbox.config()'s group_guid replaced by a
    value that compares equal to any "exam-" chat id. Nothing is written to the private config.
  - --at: frozen clock (chat._now_ct_naive), for "before kickoff" texts on a day the slate has started.
  - --replay (with --at): pool games starting after the frozen time are served as unplayed (results
    blanked, dropped from 'final' queries), the same masking eval/run.py --replay uses.
Never run this on the live port (8768): it refuses.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import runpy
import sys
from pathlib import Path

REPLAY_MASK = ("Status", "winner", "AwayPts", "HomePts", "Period", "Clock", "status_name", "possession")


class ExamGuid(str):
    """Stands in for the league group's guid: equal to every 'exam-...' chat id, nothing else."""

    def __eq__(self, other):
        return isinstance(other, str) and other.startswith("exam-")

    def __ne__(self, other):
        return not self.__eq__(other)

    __hash__ = str.__hash__


def patch_gate(group_outbox) -> None:
    orig = group_outbox.config

    def config():
        return {**orig(), "group_guid": ExamGuid("exam-*")}
    group_outbox.config = config


def install_replay(chat, frozen: dt.datetime) -> None:
    import httpx
    import subprocess
    key = subprocess.check_output(["security", "find-generic-password", "-a", "drake", "-s",
                                   "CFB_PICKEM__SUPABASE_SERVICE_KEY", "-w"], text=True).strip()
    h = {"apikey": key, "Authorization": f"Bearer {key}"}
    rows = httpx.get(f"{chat.REST}/all_games?select=GameId,week,cfb_season,%22Start%20(CT)%22&picked=eq.true"
                     "&order=cfb_season.desc,week.desc&limit=60", headers=h, timeout=20).json()
    season = max(r["cfb_season"] for r in rows)
    fut = {r["GameId"]: r["week"] for r in rows
           if r["cfb_season"] == season and str(r.get("Start (CT)") or "") > frozen.isoformat()}
    if not fut:
        print(f"replay: no pool game starts after {frozen}; nothing masked", flush=True)
        return
    week = min(fut.values())
    print(f"replay: {len(fut)} pool games after {frozen} masked as unplayed (week {week})", flush=True)
    orig = chat._rest_get

    async def masked(client, path, jwt):
        out = await orig(client, path, jwt)
        if not path.startswith("all_games") or not isinstance(out, list):
            return out
        if "Status=eq.final" in path:
            out = [r for r in out if r.get("GameId") not in fut]
        if "Status=neq.final" in path and "limit=1" in path and not out:
            return [{"week": week, "Status": None}]
        for r in out:
            if r.get("GameId") in fut:
                for k in REPLAY_MASK:
                    if k in r:
                        r[k] = None
        return out
    chat._rest_get = masked
    if hasattr(chat, "summarize_picky_player"):
        orig_sum = chat.summarize_picky_player

        def sum_masked(data, current_week, now_ct):
            data = dict(data, picks=[dict(p, status=None, winner=None)
                                     if str(p.get("start") or "") > frozen.isoformat() else p
                                     for p in data.get("picks") or []])
            return orig_sum(data, current_week, now_ct)
        chat.summarize_picky_player = sum_masked


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", required=True)
    ap.add_argument("--at", default="")
    ap.add_argument("--replay", action="store_true")
    a = ap.parse_args()
    if os.environ.get("PICKY_GROUP_PORT", "8768") == "8768":
        raise SystemExit("refusing: PICKY_GROUP_PORT is the live group port")
    server = Path(a.server).resolve()
    os.chdir(server)
    sys.path.insert(0, str(server))
    import chat  # noqa: E402  (the ref's own chat.py)
    if a.at:
        frozen = dt.datetime.fromisoformat(a.at)
        chat._now_ct_naive = lambda: frozen
        print(f"clock frozen at {frozen}", flush=True)
        if a.replay:
            install_replay(chat, frozen)
    import group_outbox  # noqa: E402
    patch_gate(group_outbox)
    runpy.run_path(str(server / "group_chat.py"), run_name="__main__")


if __name__ == "__main__":
    main()
