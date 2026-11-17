"""
Channel lane (Saturday drill, 2026-11-17): "what channel is LSU Tennessee on?" answered in code from the
live poller's cache (live_games.parse_scoreboard keeps ESPN's scoreboard broadcasts). Never a guess: no
named game, or no network listed by ESPN, means None and the full brain answers instead.
"""
from __future__ import annotations

import live_games as L


def answer(ctx: dict | None, q: str) -> str | None:
    cache = L.load() or {}
    hits = [g for g in L.named_games(q, cache.get("games") or {}) if g.get("broadcasts")][:2]
    if not hits:
        return None
    return " ".join(f"{g['away']['name']} at {g['home']['name']} is on {' / '.join(g['broadcasts'][:2])}." for g in hits)
