#!/usr/bin/env python3
"""
DM memes: a per-member flag that adds a meme GIF to every 1-on-1 iMessage DM reply Picky sends that member
(Charlie, 2026-11-20: a Tim Robinson / I Think You Should Leave meme on every DM reply to one member, "only in
DM not in the group").

Only group_chat.group() calls this, and only for dm=true. The league group chat and the site widget (POST
/web) never reach it, so in the group the member gets exactly the replies everyone else does.

The answer is unchanged: the meme rides as the reply's image, which the watcher sends as an attachment after
the text. No model, no network: a code pick from a local pool, never the same file twice in a row for a member.
If the file can't be read, the reply gets the meme's quote on its own line plus the GIF link instead, and the
watcher sends that same text (image_alt) if the attachment fails to send.

Private files (re-read on every call, so a toggle needs no restart):
  cfb-pickem-private/picky_dm_memes.json        {"members": {"<team_id>": {"team", "pool", "on"}}}
  cfb-pickem-private/memes/<pool>/pool.json     {"memes": [{"file", "quote", "url"}]}
  cfb-pickem-private/picky_dm_memes_state.json  {"<team_id>": "<last file sent>"}

CLI:
  dm_memes.py status
  dm_memes.py off <team name or team_id>        # "stop memeing Kevin"
  dm_memes.py on  <team name or team_id>
"""

from __future__ import annotations

import base64
import json
import logging
import os
import random
import sys
from pathlib import Path

log = logging.getLogger("picky-group")

PRIVATE = Path(os.environ.get("PICKY_PRIVATE_DIR", "/Users/Agent/cfb-pickem-private"))
CONFIG = Path(os.environ.get("PICKY_DM_MEMES", str(PRIVATE / "picky_dm_memes.json")))
STATE = Path(os.environ.get("PICKY_DM_MEMES_STATE", str(PRIVATE / "picky_dm_memes_state.json")))
POOLS = Path(os.environ.get("PICKY_MEMES_DIR", str(PRIVATE / "memes")))


def _read(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(path: Path, data: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1))
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def member(team_id) -> dict | None:
    """The member's config entry when their DM memes are on, else None."""
    if team_id is None:
        return None
    m = (_read(CONFIG).get("members") or {}).get(str(team_id))
    return m if isinstance(m, dict) and m.get("on") else None


def pick(team_id, record: bool = True) -> dict | None:
    """{"file", "quote", "url", "image": base64 or None} for a flagged member, else None. Never the file this
    member got last time. `record=False` (test traffic) leaves the no-repeat state alone."""
    m = member(team_id)
    if not m:
        return None
    pool_dir = POOLS / str(m.get("pool") or "itysl")
    memes = [x for x in _read(pool_dir / "pool.json").get("memes") or [] if isinstance(x, dict) and x.get("file")]
    if not memes:
        log.warning("dm memes: empty pool %s", pool_dir)
        return None
    state = _read(STATE)
    last = state.get(str(team_id))
    choice = random.choice([x for x in memes if x["file"] != last] or memes)
    try:
        image = base64.b64encode((pool_dir / choice["file"]).read_bytes()).decode()
    except OSError as e:
        log.warning("dm memes: %s unreadable, text fallback: %s", choice["file"], e)
        image = None
    if record:
        state[str(team_id)] = choice["file"]
        try:
            _write(STATE, state)
        except OSError as e:
            log.warning("dm memes: state not saved: %s", e)
    return {**choice, "image": image}


def fallback_text(meme: dict) -> str:
    """The meme as text: the quote on its own line, then the GIF link."""
    return f"\"{meme.get('quote', '').strip()}\"\n{meme.get('url', '')}".strip()


def attach(out: dict, team_id, dm: bool, test: bool = False) -> dict:
    """Add the member's meme to a DM reply that has text and no picture of its own. Anything else (group, no
    flag, no reply, a picture reply) comes back untouched. Never raises: a meme can't break a reply."""
    if not dm or not out.get("reply") or out.get("image"):
        return out
    try:
        meme = pick(team_id, record=not test)
    except Exception as e:  # noqa: BLE001
        log.warning("dm memes failed, reply sent without one: %s", e)
        return out
    if not meme:
        return out
    alt = fallback_text(meme)
    if meme["image"]:
        log.info("dm meme attached team_id=%s file=%s", team_id, meme["file"])
        return {**out, "image": meme["image"], "image_alt": alt}
    return {**out, "reply": f"{out['reply'].rstrip()}\n\n{alt}"}


def _find(members: dict, who: str) -> str | None:
    w = who.strip().lower()
    if w in members:
        return w
    hits = [k for k, m in members.items() if w and w in str(m.get("team", "")).lower()]
    return hits[0] if len(hits) == 1 else None


def main(argv: list[str]) -> int:
    cmd = argv[0] if argv else "status"
    cfg = _read(CONFIG)
    members = cfg.get("members") or {}
    if cmd == "status":
        if not members:
            print("DM memes: nobody configured")
        for k, m in members.items():
            print(f"DM memes for {m.get('team', '?')} (team_id {k}): {'ON' if m.get('on') else 'OFF'}, pool {m.get('pool', 'itysl')}")
        return 0
    if cmd in ("on", "off") and len(argv) > 1:
        k = _find(members, " ".join(argv[1:]))
        if not k:
            print(f"DM memes: no single configured member matches {' '.join(argv[1:])!r}")
            return 1
        members[k]["on"] = cmd == "on"
        _write(CONFIG, cfg)
        print(f"DM memes for {members[k].get('team', '?')} are now {cmd.upper()} (takes effect on the next DM, no restart)")
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
