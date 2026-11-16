"""
Rare running bits: a bit Picky may use at most once per N days across ALL of
his output (group texts, DMs, posts). Charlie, 2026-11-15, on the Blinkstradamus
milk story: "you're overdoing the milk ... it's like a once a month joke."

  available(bit)        the budget is free (no use in the last N days)
  mentions(bit, text)   the text uses the bit (its word list)
  spend(bit)            record a use (call when a reply that mentions it is SENT)

State is a tiny private file (bit -> last-used unix time), atomic writes:
/Users/Agent/cfb-pickem-private/picky_bits_state.json (PICKY_BITS_STATE overrides,
so tests and exams never spend the live budget).

Add a bit to BITS to ration it (the Ackbar nemesis bit could get its own rate
later; it isn't rationed today).
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from pathlib import Path

STATE = Path(os.environ.get("PICKY_BITS_STATE", "/Users/Agent/cfb-pickem-private/picky_bits_state.json"))
DAY = 86400

BITS = {
    # Blinkstradamus: girls in a top-off Jeep yelled "NICE MILK FREAK" at him while he held a carton
    "milk": {"every_days": 30,
             "rx": re.compile(r"\b(milk\w*|cartons?|dairy|jeeps?|freak\w*|two percent|skim|lactose)\b", re.I)},
}


def _read() -> dict:
    try:
        d = json.loads(STATE.read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def last_used(bit: str) -> float | None:
    v = _read().get(bit)
    return float(v) if isinstance(v, (int, float)) else None


def available(bit: str, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    t = last_used(bit)
    return t is None or now - t >= BITS[bit]["every_days"] * DAY


def mentions(bit: str, text: str) -> bool:
    return bool(BITS[bit]["rx"].search(text or ""))


def spend(bit: str, now: float | None = None) -> None:
    d = _read()
    d[bit] = time.time() if now is None else now
    STATE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=STATE.parent, prefix=".bits-", suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump(d, f)
    os.chmod(tmp, 0o600)
    os.replace(tmp, STATE)


def note_sent(text: str) -> list[str]:
    """Call with every reply/post actually sent: spends each bit it uses."""
    used = [b for b in BITS if mentions(b, text)]
    for b in used:
        spend(b)
    return used
