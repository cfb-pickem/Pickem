"""
Supabase keepalive (zero maintenance, phase 8). A free Supabase project pauses after about a week with no
activity, and a paused project needs someone to click "restore" in the dashboard. In season, the site and Picky
read the pick'em DB all day; in the offseason nobody does. So once a day this reads one row through the public
REST API with the site's own anon key (the same key the public site ships), which counts as activity.

Rides an existing tick: feed_monitor.py calls ping() every 15 minutes; ping() does nothing until the last good
ping is DUE_EVERY old. Every attempt is recorded in the state file; a failure writes a feed-health line, and a
paused project (HTTP 540, or a body that says paused) is marked so the reply path can say so honestly instead
of erroring (group_chat.db_down_reply).

    keepalive.py            # ping now if due, print the result
    keepalive.py --force    # ping now
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
import time
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

SUPABASE_URL = "https://vopdioszofwdkwnujtiq.supabase.co"   # = chat.SUPABASE_URL (test_zero_maintenance checks)
ANON = (  # = chat.SUPABASE_ANON: public, shipped by the site; JWT exp Oct 2035
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InZvcGRpb3N6b2Z3ZGt3bnVqdGlxIiwicm9sZSI6ImFub24i"
    "LCJpYXQiOjE3NjE1ODI2MjksImV4cCI6MjA3NzE1ODYyOX0."
    "cD2nNYMEUUOHWQlQC0-lxGZ3s1HVQhWEX_FmgzSsZYw"
)
PING_PATH = "/rest/v1/logos?select=Mascot&limit=1"        # a tiny public read (the site's logo table)
DUE_EVERY = 20 * 3600       # at most one ping a day (a little under, so the 15-min tick never drifts past 24 h)
DOWN_FOR = 36 * 3600        # db_down() trusts a failed ping this long


def _state_path() -> Path:
    return Path(os.environ.get("PICKY_KEEPALIVE_STATE", HERE / "cache" / "supabase_keepalive.json"))


def state() -> dict:
    try:
        return json.loads(_state_path().read_text())
    except (OSError, ValueError):
        return {}


def _save(st: dict) -> None:
    p = _state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=1))
    os.replace(tmp, p)


def ping(force: bool = False, now: float | None = None, transport=None) -> dict:
    """Read one row if due. {"ok", "paused", "status", "at"} or {"skipped": True}. Never raises."""
    now = time.time() if now is None else now
    st = state()
    if not force and now - float(st.get("last_ok") or 0) < DUE_EVERY:
        return {"skipped": True, "last_ok": st.get("last_ok")}
    status, paused, err = None, False, ""
    try:
        with httpx.Client(timeout=20, transport=transport) as c:
            r = c.get(SUPABASE_URL + PING_PATH, headers={"apikey": ANON, "Authorization": f"Bearer {ANON}"})
        status = r.status_code
        paused = status == 540 or (status >= 400 and "paused" in r.text.lower())
        if status != 200:
            err = r.text[:160]
    except httpx.HTTPError as e:
        err = f"{type(e).__name__}: {str(e)[:160]}"
    ok = status == 200
    st.update({"last_try": now, "last_status": status, "paused": paused, "error": "" if ok else err})
    if ok:
        st["last_ok"] = now
    _save(st)
    if not ok:
        import feed_health
        feed_health.note("supabase", ("PAUSED: restore it in the Supabase dashboard (free tier) " if paused
                                      else "unreachable: ") + f"status {status} {err}".strip())
    return {"ok": ok, "paused": paused, "status": status,
            "at": dt.datetime.fromtimestamp(now).isoformat(timespec="seconds")}


def db_down(now: float | None = None) -> bool:
    """The last ping failed (recently) and nothing has succeeded since."""
    now = time.time() if now is None else now
    st = state()
    t = float(st.get("last_try") or 0)
    return bool(t) and now - t < DOWN_FOR and float(st.get("last_ok") or 0) < t


if __name__ == "__main__":
    print(json.dumps(ping(force="--force" in sys.argv)))
