"""One place for every Anthropic (claude -p) call in the Picky system.

All offline writers, graders and curators use Opus 5.5. Picky never needs
Anthropic at runtime: PICKY_NO_ANTHROPIC=1 makes every call fail fast so the
local gemma fallbacks run (kill-switch tests set it).
"""
import os

MODEL = os.environ.get("PICKY_CLAUDE_MODEL", "claude-opus-5-5")


class AnthropicDisabled(RuntimeError):
    pass


def disabled() -> bool:
    return os.environ.get("PICKY_NO_ANTHROPIC", "") not in ("", "0")


def guard() -> None:
    """Raise before spawning claude -p when the kill switch is on."""
    if disabled():
        raise AnthropicDisabled("PICKY_NO_ANTHROPIC set: claude -p disabled")
