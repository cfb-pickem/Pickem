"""Web Picky = text Picky (2026-11-17): chat.py /chat forwards a signed-in caller to the sidecar's /web,
which runs the mapped-member DM path with the caller's own session. No model, no network."""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import group_chat as gc  # noqa: E402


def test_sidecar_never_forwards_its_own_drafts():
    assert gc.chat.WEB_BRAIN is False


def test_web_history_is_thread_turns():
    msgs = [{"role": "assistant", "content": "hi"}, {"role": "user", "content": "q1"},
            {"role": "assistant", "content": "a1"}, {"role": "system", "content": "x"}, "junk",
            {"role": "user", "content": ""}]
    assert gc.web_history(msgs) == [{"role": "user", "content": "q1"}, {"role": "assistant", "content": "a1"}]
    long = [{"role": r, "content": str(i)} for i in range(40) for r in ("user", "assistant")]
    assert len(gc.web_history(long)) == gc.chat.MAX_HISTORY_TURNS * 2


def test_web_sender_maps_uid_to_team():
    gc.VIEWER.teams = {"ackbar": {"team_id": 7, "team_name": "Ackbar", "user_id": "u-1", "is_exhibition": False}}
    s = gc.web_sender("u-1")
    assert (s.team, s.team_id, s.user_id) == ("Ackbar", 7, "u-1")
    s = gc.web_sender("u-2")
    assert s.team is None and s.team_id is None and s.user_id == "u-2"


def test_web_rider_only_on_web():
    assert "WEB CHAT" in gc.WEB_RIDER and "iMessage" in gc.DM_RIDER


if __name__ == "__main__":
    n = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("  ok ", name)
            n += 1
    print(f"\n{n}/{n} passed")
