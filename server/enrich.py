"""
Opus enrichment rules shared by Picky's code-first feeds (dossier.py, game_cards.py,
group_posts.py, insider_refresh.py).

Charlie, 2026-11-16: "Try to have just auto feed pipelines for him so you dont need an
opus call every time." / "I want anthropic to train him but I dont want him reliant on
anthropic."

  - Every refresh is pure code over the caches the other feeds already keep (ESPN, CFBD,
    news.json, pick_brief factors, standings). No model runs on the per-run path.
  - Opus (picky_claude: claude-opus-5-5, guard(), empty MCP config, no API key) is an
    optional enrichment layer on top: at most weekly per artifact (insider notes: at most
    daily), and only while picky_claude's booster gate is open (Claude there, subscription
    auth, under the daily cap; phase 8, zero maintenance). Gate shut, failed or timed out =
    the code output alone.
  - The last good enrichment is kept, and shown only while it's fresh (freshness source
    'opus_enrichment': 6 days, which also keeps any ranking text under a week old) and
    still true (every number it cites still appears in today's computed facts).
"""

from __future__ import annotations

import datetime as dt
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import freshness  # noqa: E402
import picky_claude  # noqa: E402

D = freshness.D
SOURCE = freshness.register(freshness.Source("opus_enrichment", (6 * D, 6 * D), (6 * D, 6 * D)))
_NUM = re.compile(r"(?<![\w.])\d+(?:\.\d+)?")


def available() -> bool:
    """picky_claude's booster gate: False = callers skip Opus without spawning anything."""
    return picky_claude.available()


def fresh(generated_at, now: dt.datetime | float | None = None) -> bool:
    """An enrichment written at `generated_at` (ISO or epoch) can still be shown."""
    return bool(generated_at) and freshness.fresh(SOURCE.name, generated_at, now)


def _nums(text: str) -> set[str]:
    return {f"{float(n):g}" for n in _NUM.findall(text or "")}


def numbers_hold(text: str, facts: str) -> bool:
    """Every number in `text` still appears in today's computed `facts` (a line that cited
    'market at -4.5' goes once the market moves)."""
    return _nums(text) <= _nums(facts)
