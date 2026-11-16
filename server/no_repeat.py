"""
Never the same insult twice (Charlie's checklist item 7, enforced in code).

A persistent ledger of every line Picky sends in banter: normalized text,
who it was aimed at, which chat, the roast angle (the fact or idea the joke
is built on, e.g. the digit-free text of "TG is 2-7 in Oklahoma games") and
the opener. `Ledger.is_repeat()` says why a candidate would read as a repeat;
`screen()` walks prepared options and returns the first one that doesn't.

Windows (days):
  - verbatim (same normalized text) on the same target, or in the same chat: never
  - near-identical (similar() >= SIM_THRESHOLD) on the same target: TARGET_DAYS
  - same angle on the same target: TARGET_DAYS
  - near-identical anywhere (any target, any chat): GROUP_DAYS
  - same 3-word opener as any of the last OPENER_LAST_N sent lines
  - any BANNED_TICS phrase: always

State: PRIVATE JSON (default /Users/Agent/cfb-pickem-private/picky_no_repeat.json,
override with PICKY_NO_REPEAT_STATE; tests and side sidecars use a temp file).
Atomic writes under an flock, mode 0600. No model, no network.

CLI:  no_repeat.py check "<text>" [--target T] [--chat C] [--angle A]
      no_repeat.py sim "<a>" "<b>"
      no_repeat.py status
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import sys
import tempfile
import time
import unicodedata
from pathlib import Path

DEFAULT_PATH = "/Users/Agent/cfb-pickem-private/picky_no_repeat.json"
TARGET_DAYS = 30
GROUP_DAYS = 21
OPENER_WORDS = 3
OPENER_LAST_N = 30
# Tuned on the pairs in test_no_repeat.py: a reworded copy of the same joke scores >= 0.62, two
# different jokes on the same topic (same team, same record) stay under ~0.5.
SIM_THRESHOLD = 0.62
DAY = 86400.0

# Stock phrases that read as a bot (plan 1b/W5, Charlie's checklist 7, chat._TICS). Regexes,
# matched case-insensitively on the raw text with curly quotes straightened.
BANNED_TICS = [
    r"\bsitting pretty\b",
    r"\bchalk merchant\b",
    r"\bparticipation (trophy|trophies|ribbon|award)\b",
    r"\bfor the record\b",
    r"\bflesh and blood\b",
    r"\blet that sink in\b",
    r"\blet'?s be real\b",
    r"\bi'?m (just )?here to\b",
    r"\bat least i\b",
    r"\bin the data\b",
    r"\b(\d+(\.\d+)?|nine|twelve)[- ]?(billion|b)[- ]param\w*|\bparameter counts?\b|\bparameters?\b",
    r"\bmac mini\b|\b\d+ ?gb\b|\bgigabytes?\b|\bgpu\b",
    r"\bas an ai\b|\blanguage model\b|\bmy training( data)?\b",
    r"\bdon'?t get cocky\b",
    r"\bthe rest of us\b",
    r"\bhere to watch\b",
    r"\bdumpster fire\b",
    r"\bgod among men\b",
    r"\bso maybe you should\b",
    r"\bdon'?t let it go to your head\b",
    r"\bwhich is a nice way of saying\b",
    # preachy (W5): never a lecture, never a refusal
    r"\bnot okay\b|\bnot ok\b",
    r"\bkeep it (respectful|clean|civil|classy)\b|\blet'?s keep it (clean|civil|respectful)\b",
    r"\bi (can'?t|won'?t|cannot|will not) (do|say|help|go there|write|make|repeat|joke)\b",
]
_TICS_RX = [re.compile(p, re.I) for p in BANNED_TICS]


def banned_tic(text: str) -> str | None:
    """The first banned phrase found in `text`, else None."""
    t = (text or "").replace("’", "'").replace("‘", "'")
    for rx in _TICS_RX:
        m = rx.search(t)
        if m:
            return m.group(0)
    return None


# -----------------------------------------------------------------------------
# normalize + similarity
# -----------------------------------------------------------------------------

_APOS = str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"', "—": " ", "–": " "})


def normalize(text: str) -> str:
    """Lowercase, accents and punctuation off, apostrophes dropped ("you're" -> "youre"),
    numbers kept ("-5.5" -> "5.5"), single spaces."""
    t = unicodedata.normalize("NFKD", (text or "").translate(_APOS))
    t = "".join(c for c in t if not unicodedata.combining(c)).lower()
    t = t.replace("'", "")
    t = re.sub(r"(?<=\d)\.(?=\d)", "\x00", t)       # keep decimals
    t = re.sub(r"[^a-z0-9\x00]+", " ", t).replace("\x00", ".")
    return re.sub(r"\s+", " ", t).strip()


def _grams(s: str, n: int = 3) -> set[str]:
    s = f" {s} "
    return {s[i:i + n] for i in range(max(1, len(s) - n + 1))}


def similar(a: str, b: str) -> float:
    """0..1. The max of two Dice scores over the normalized texts: character trigrams
    (catches small rewordings and typos) and word sets (catches reordering)."""
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    ga, gb = _grams(na), _grams(nb)
    char = 2 * len(ga & gb) / (len(ga) + len(gb))
    ta, tb = set(na.split()), set(nb.split())
    tok = 2 * len(ta & tb) / (len(ta) + len(tb))
    return round(max(char, tok), 4)


def opener(text: str, n: int = OPENER_WORDS) -> str | None:
    w = normalize(text).split()
    return " ".join(w[:n]) if len(w) >= n else None


def angle_key(text: str | None) -> str | None:
    """A stable angle id: digits dropped so 'TG is 2-7 in Oklahoma games' and next week's
    'TG is 2-8 in Oklahoma games' are the same angle."""
    if not text:
        return None
    k = re.sub(r"\s+", " ", re.sub(r"[0-9.]+", " ", normalize(text))).strip()
    return k[:120] or None


def _h(norm: str) -> str:
    return hashlib.sha1(norm.encode()).hexdigest()[:16]


def _tkey(target: str | None) -> str | None:
    return normalize(target) or None if target else None


# -----------------------------------------------------------------------------
# the ledger
# -----------------------------------------------------------------------------

class Ledger:
    """Persistent record of sent lines. Safe across processes (flock + atomic replace)."""

    KEEP_DAYS = max(TARGET_DAYS, GROUP_DAYS) + 1

    def __init__(self, path: str | os.PathLike | None = None):
        self.path = Path(path or os.environ.get("PICKY_NO_REPEAT_STATE") or DEFAULT_PATH)
        self._mtime: float | None = None
        self._doc: dict = self._empty()

    @staticmethod
    def _empty() -> dict:
        return {"schema": 1, "entries": [], "forever": {}}

    # ---- storage
    def _load(self) -> dict:
        try:
            m = self.path.stat().st_mtime
        except OSError:
            self._mtime, self._doc = None, self._empty()
            return self._doc
        if m != self._mtime:
            try:
                d = json.loads(self.path.read_text())
                if isinstance(d, dict) and isinstance(d.get("entries"), list):
                    d.setdefault("forever", {})
                    self._doc, self._mtime = d, m
            except (OSError, ValueError):
                pass  # keep the last good copy in memory
        return self._doc

    @contextlib.contextmanager
    def _locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(str(self.path) + ".lock", "a") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lf, fcntl.LOCK_UN)

    def _write(self, doc: dict) -> None:
        fd, tmp = tempfile.mkstemp(prefix=self.path.name + ".", suffix=".tmp", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(doc, f, separators=(",", ":"))
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
        self._mtime, self._doc = self.path.stat().st_mtime, doc

    # ---- checks
    def is_repeat(self, text: str, target: str | None = None, chat_id: str | None = None,
                  angle: str | None = None, now: float | None = None) -> str | None:
        """Why `text` would be a repeat (a short reason), or None if it's fresh."""
        tic = banned_tic(text)
        if tic:
            return f"banned tic: {tic!r}"
        norm = normalize(text)
        if not norm:
            return "empty"
        now = now if now is not None else time.time()
        d = self._load()
        tk, h = _tkey(target), _h(norm)
        fv = d.get("forever") or {}
        if tk and h in (fv.get("t:" + tk) or []):
            return f"verbatim repeat on {target}"
        if chat_id and h in (fv.get("c:" + str(chat_id)) or []):
            return "verbatim repeat in this chat"
        ak = angle_key(angle) if angle else None
        op = opener(text)
        entries = d.get("entries") or []
        for e in reversed(entries):
            age = now - float(e.get("ts") or 0)
            if age > self.KEEP_DAYS * DAY:
                continue
            same_t = bool(tk) and e.get("target") == tk
            if same_t and age <= TARGET_DAYS * DAY:
                if ak and e.get("angle") == ak:
                    return f"same angle on {target} within {TARGET_DAYS}d: {ak[:60]!r}"
                if similar(norm, e.get("norm") or "") >= SIM_THRESHOLD:
                    return f"near-identical line on {target} within {TARGET_DAYS}d"
            if age <= GROUP_DAYS * DAY and similar(norm, e.get("norm") or "") >= SIM_THRESHOLD:
                return f"near-identical line used in the group within {GROUP_DAYS}d"
        if op:
            recent = [e.get("opener") for e in entries[-OPENER_LAST_N:]]
            if op in recent:
                return f"opener {op!r} used in the last {OPENER_LAST_N} replies"
        return None

    def record(self, text: str, target: str | None = None, chat_id: str | None = None,
               angle: str | None = None, opener: str | None = None, now: float | None = None) -> None:
        """Log a line that was actually sent."""
        norm = normalize(text)
        if not norm:
            return
        now = now if now is not None else time.time()
        tk = _tkey(target)
        ent = {"ts": round(now, 1), "norm": norm, "target": tk, "chat": str(chat_id) if chat_id else None,
               "angle": angle_key(angle) if angle else None,
               "opener": (normalize(opener) if opener else globals()["opener"](text))}
        with self._locked():
            self._mtime = None  # re-read under the lock: another process may have written
            d = json.loads(json.dumps(self._load()))
            cut = now - self.KEEP_DAYS * DAY
            allv = d["entries"] + [ent]  # chronological: the in-window lines are a suffix
            k = sum(1 for e in allv if float(e.get("ts") or 0) >= cut)
            d["entries"] = allv[-max(k, OPENER_LAST_N):]  # openers look at the last N whatever their age
            h = _h(norm)
            for key in (("t:" + tk) if tk else None, ("c:" + str(chat_id)) if chat_id else None):
                if key:
                    lst = d["forever"].setdefault(key, [])
                    if h not in lst:
                        lst.append(h)
            self._write(d)

    def screen(self, options, target: str | None = None, chat_id: str | None = None):
        """The first option that isn't a repeat (falls through the prepared list), else None.
        Options are strings or dicts with "text" (and optional "angle")."""
        for o in options or []:
            text, ang = (o.get("text"), o.get("angle")) if isinstance(o, dict) else (o, None)
            if text and not self.is_repeat(text, target=target, chat_id=chat_id, angle=ang):
                return o
        return None

    def stale_phrases(self, text: str, n: int = 3) -> list[str]:
        """n-word runs in `text` that also appear in one of the last OPENER_LAST_N sent lines
        (W5's 30-reply phrase cooldown; the router decides what to do with them). Runs made only
        of very common words are ignored."""
        w = normalize(text).split()
        grams = {" ".join(w[i:i + n]) for i in range(len(w) - n + 1)}
        grams = {g for g in grams if not set(g.split()) <= _COMMON}
        seen = set()
        for e in (self._load().get("entries") or [])[-OPENER_LAST_N:]:
            ew = (e.get("norm") or "").split()
            seen |= {" ".join(ew[i:i + n]) for i in range(len(ew) - n + 1)}
        return sorted(grams & seen)


_COMMON = set("""a an the and or but if so to of in on at for with by from up out is are was were be been am i im
you youre your u ur he she it its they them we me my this that what who how why when not no yes do dont did does
just like get got go going all one more than then there here have has had will would can cant""".split())

_default: Ledger | None = None


def default_ledger() -> Ledger:
    global _default
    p = os.environ.get("PICKY_NO_REPEAT_STATE") or DEFAULT_PATH
    if _default is None or str(_default.path) != p:
        _default = Ledger(p)
    return _default


def screen(options, target: str | None = None, chat_id: str | None = None, ledger: Ledger | None = None):
    return (ledger or default_ledger()).screen(options, target=target, chat_id=chat_id)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["check", "sim", "status"])
    ap.add_argument("args", nargs="*")
    ap.add_argument("--target")
    ap.add_argument("--chat")
    ap.add_argument("--angle")
    a = ap.parse_args(argv)
    if a.cmd == "sim":
        print(similar(a.args[0], a.args[1]))
    elif a.cmd == "check":
        print(default_ledger().is_repeat(a.args[0], target=a.target, chat_id=a.chat, angle=a.angle) or "fresh")
    else:
        d = default_ledger()._load()
        print(json.dumps({"path": str(default_ledger().path), "entries": len(d["entries"]),
                          "forever_keys": len(d["forever"])}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
