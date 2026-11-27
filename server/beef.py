"""TEMPORARY beef mode (Charlie, 2026-11-27: "Let him keep fighting w ackbar ... egg him on and push his buttons",
"use full opus brain to respond to him").

While `beef` in the private picky_group_config.json is set and its `until` hasn't passed, every league-group
text FROM that handle gets a Picky reply, @Picky or not (the watcher learns the handle from the /outbox poll).
The reply is written directly by Claude Opus (picky_claude.live_reply, the live mode's path and credentials;
the global live mode is never changed), from Picky's voice, the recent thread and a code-built fact sheet on
the target. No coach pass runs on it. Opus fails or times out -> the normal pipeline with an egg-on rider.
He sounds genuinely upset ("seriously stop", "not funny") -> one light line and the beef pauses.

  python3 -I server/beef.py status
  python3 -I server/beef.py off       # ends it now (drops `beef` from the config)
  python3 -I server/beef.py resume    # un-pause after a back-off
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # run as a script under -I
import group_outbox  # noqa: E402

STATE_FILE = group_outbox.PRIVATE / "picky_beef_state.json"
COOLDOWN = 20.0   # seconds between beef replies
TIMEOUT = 45.0    # the Opus call; past this the normal pipeline answers
EFFORT = "medium"
RECENT_N = 20     # thread messages the watcher sends for a beef row

UPSET = re.compile(
    r"\b(?:seriously|for real|fr|actually|ok(?:ay)?|dude|bro)\W+(?:stop|chill|enough|knock it off|cut it out)\b"
    r"|\bnot (?:funny|cool)\b|\bisn'?t funny\b|\bknock it off\b|\bcut it out\b|\bstop it\b|\bthat'?s enough\b"
    r"|\btoo far\b|\bover the line\b|\bi'?m (?:being )?serious\b|\bnot playing\b|\bactually upset\b"
    r"|\bplease stop\b|\bjust stop\b|\bcan you stop\b|\bmake (?:it|him|picky) stop\b", re.I)
BACK_OFF = "Alright, truce. Good fight, Ackbar. Back to football."

EGG_ON = """BEEF MODE. {team} is in a running trash-talk fight with you in the group, and the commissioner wants it
kept going. Your job: push his buttons and keep the fight alive.
- React specifically to what he JUST said: quote-twist it, flip it back on him, call back to earlier in the thread.
- Match and raise his heat. Be witty and specific: wordplay, callbacks, absurd comparisons, short punchlines. Not
  profanity spam; one well-placed swear beats five. R-rated is fine.
- 1-3 sentences, iMessage style, no markdown, no emoji, no hashtags, no em dashes. Don't start with "Ackbar,"
  every time. Don't explain the joke, don't apologize, don't be a good sport, don't moralize.
- Any stat, record, place, score, line or pick you mention must come word for word from the FACT SHEET. Never
  invent a number. If a fact doesn't fit, roast without numbers.
- Hard limits: no slurs; nothing about his family, looks, race, religion, sexuality or health; no threats. Keep it
  about football, his picks, this league, the refund, and what he says in the chat.
- Write only the text Picky sends. Nothing else."""


def _now() -> float:
    return time.time()


def _read_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def _write_state(**kw) -> dict:
    st = _read_state() | kw
    group_outbox._write_private(STATE_FILE, json.dumps(st, indent=1) + "\n")
    return st


def _until(b: dict) -> float | None:
    try:
        return dt.datetime.fromisoformat(str(b.get("until"))).timestamp()
    except (TypeError, ValueError):
        return None


def active(now: float | None = None, cfg: dict | None = None) -> dict | None:
    """The beef config while it's on (set, not expired, not paused after a back-off), else None."""
    b = (cfg if cfg is not None else group_outbox.config()).get("beef")
    if not isinstance(b, dict) or not b.get("handle"):
        return None
    end = _until(b)
    if end is None or (now if now is not None else _now()) >= end:
        return None
    st = _read_state()
    if st.get("paused") and st.get("paused_for") == b.get("until"):
        return None
    return b


def _digits(h: str) -> str:
    d = re.sub(r"\D", "", h or "")
    return d[-10:] if len(d) >= 10 else (h or "").strip().lower()


def target(handle: str, now: float | None = None, cfg: dict | None = None) -> dict | None:
    b = active(now, cfg)
    return b if b and _digits(handle) == _digits(str(b["handle"])) else None


def handles(now: float | None = None) -> list[str]:
    b = active(now)
    return [str(b["handle"])] if b else []


def upset(text: str) -> bool:
    return bool(UPSET.search(text or ""))


def pause(b: dict) -> None:
    _write_state(paused=True, paused_for=b.get("until"), paused_at=_now())


def cooldown_wait(now: float | None = None) -> float:
    last = float(_read_state().get("last_reply") or 0)
    return max(0.0, last + COOLDOWN - (now if now is not None else _now()))


def mark_sent(now: float | None = None) -> None:
    _write_state(last_reply=now if now is not None else _now())


def fact_sheet(team: str, facts_bundle: str = "", notes: list | None = None) -> str:
    """Everything Opus may cite about the target: lore member lines, head to head, burn-book standing, the
    config notes (the refund), then the league facts bundle (standings, playoff line, this week)."""
    out = []
    try:
        import nemesis
        lore = nemesis._load(nemesis.LORE)
        lines = (lore.get("members") or {}).get(team) or []
        if lines:
            out.append(f"{team.upper()} (lore, computed from the league's real picks):\n" + "\n".join(f"- {l}" for l in lines))
        f = nemesis.facts(lore)
        if f.get("record"):
            w, l, rank, _ = f["record"]
            out.append(f"{team} season record: {w}-{l}, {nemesis._ord(rank)} in the league.")
        h2h = f.get("h2h") or {}
        if h2h:
            out.append(f"{team} head to head when sides split (his W-L vs each member): "
                       + "; ".join(f"{o} {w}-{l}" for o, (w, l) in h2h.items()))
        jabs = [j["text"] for j in nemesis.code_jabs(f, True)] + [j["text"] for j in nemesis.book_jabs()]
        if jabs:
            out.append("Verified jab material (true, reuse the facts, not the wording):\n" + "\n".join(f"- {j}" for j in jabs))
    except Exception as e:  # noqa: BLE001 - a missing source just means fewer facts
        print(f"beef fact sheet: lore failed: {e}", file=sys.stderr)
    for n in notes or []:
        out.append(f"Today: {n}")
    if facts_bundle:
        out.append("LEAGUE FACTS (standings, playoff line, games):\n" + facts_bundle)
    return "\n\n".join(out)


def prompt(team: str, texts: list[str], thread: str, sheet: str, today: str) -> tuple[str, str]:
    system = ("You are Picky, the AI member of a college football pick'em league's iMessage group (4 games a week "
              "against the spread). Smug, quick, a sharp CFB buddy with a mean streak for your nemesis "
              f"{team}. Today is {today} (US Central).\n\n" + EGG_ON.format(team=team)
              + "\n\nFACT SHEET (the only source for any number):\n" + sheet)
    said = "\n".join(f"{team}: {t}" for t in texts)
    user = (thread.strip() + "\n\n" if thread.strip() else "") + \
        f"{team}'s latest (reply to this, one text):\n{said}"
    return system, user


def clean(text: str) -> str:
    t = (text or "").strip().strip('"').strip()
    t = re.sub(r"^\s*picky\s*:\s*", "", t, flags=re.I)
    t = t.replace("—", ", ").replace("–", "-")
    return re.sub(r"\s+", " ", t).strip()


def main(argv: list[str]) -> int:
    cmd = argv[0] if argv else "status"
    if cmd == "off":
        cur = group_outbox._read_json(group_outbox.CONFIG_FILE, {})
        had = cur.pop("beef", None)
        group_outbox._write_private(group_outbox.CONFIG_FILE, json.dumps(cur, indent=2) + "\n")
        print("beef off" if had else "beef was already off")
    elif cmd == "resume":
        _write_state(paused=False)
        print("beef resumed" if active() else "beef un-paused, but it isn't on (expired or not set)")
    b = active()
    if cmd == "status" or cmd not in ("off", "resume"):
        raw = group_outbox.config().get("beef")
        st = _read_state()
        print(f"beef ON vs {b.get('team')} until {b.get('until')}" if b else
              f"beef OFF ({'paused after a back-off' if st.get('paused') and raw else 'expired' if raw else 'not set'})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
