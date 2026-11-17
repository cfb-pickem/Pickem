"""
Home/away guard. Code always phrases a matchup as canonical(away, home) = "Away at Home"
(game cards, the slate rider, live blocks, group posts); the model sometimes flips it on its
own (exam 2026-11-16: "I don't have the pool line for Missouri State at New Mexico State" for
New Mexico State at Missouri State). fix() is the cheap post-check on a finished reply: an
"X at Y" (or "X @ Y") where the game is known as Y at X gets swapped back. Known games = the
live board (live_games cache: every FBS game this week, pool or not) plus any pairs the
caller passes. Left alone: a pair known both ways (a rematch), and an "X" that's only the tail
of a longer capitalized name ("Western Michigan at Ohio State" when only Michigan is known).
"""

from __future__ import annotations

import functools
import re

_LEAD_OK = {"for", "the", "in", "on", "so", "but", "and", "yes", "no", "if", "like", "with", "after", "before",
            "then", "when", "about", "against", "watch", "watching", "had", "took", "take", "game", "i", "my", "your"}


def canonical(away: str, home: str) -> str:
    return f"{away} at {home}"


def known_pairs(extra=()) -> list[tuple[str, str]]:
    """(away, home) for every game on the live board, plus `extra`."""
    pairs = [(a, h) for a, h in extra if a and h]
    try:
        import live_games
        for g in (live_games.load().get("games") or {}).values():
            a, h = (g.get("away") or {}).get("name"), (g.get("home") or {}).get("name")
            if a and h:
                pairs.append((a, h))
    except Exception:  # noqa: BLE001 - no board: only the caller's pairs
        pass
    return pairs


@functools.lru_cache(maxsize=8)
def _rx(names: tuple[str, ...]) -> re.Pattern:
    alt = "|".join(re.escape(n) for n in names)
    return re.compile(rf"(?<![\w&'.-])({alt})(\s+(?:at|@)\s+)({alt})(?![\w&'-])", re.I)


def fix(text: str, pairs=None) -> str:
    """`text` with any reversed "Home at Away" of a known game swapped to "Away at Home"."""
    pairs = known_pairs() if pairs is None else list(pairs)
    if not text or not pairs or not re.search(r"\s(?:at|@)\s", text, re.I):
        return text
    fwd = {(a.lower(), h.lower()): (a, h) for a, h in pairs}
    rx = _rx(tuple(sorted({n for p in pairs for n in p}, key=lambda n: (-len(n), n))))

    def sub(m: re.Match) -> str:
        x, y = m.group(1).lower(), m.group(3).lower()
        if (x, y) in fwd or (y, x) not in fwd:
            return m.group(0)
        prev = re.search(r"([A-Za-z&.'-]+) $", m.string[:m.start()])
        if prev and prev.group(1)[0].isupper() and prev.group(1).lower() not in _LEAD_OK:
            return m.group(0)  # "Western Michigan at ...": a longer name, not this team
        a, h = fwd[(y, x)]
        return f"{a}{m.group(2)}{h}"

    return rx.sub(sub, text)
