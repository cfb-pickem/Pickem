"""Pure tests for watchdog.decide (no su, no launchctl, no texts).
Run:  /usr/bin/python3 -I tools/picky-imessage/test_watchdog.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import watchdog as w  # noqa: E402

UP = {"com.pickem.picky-group": True, "com.pickem.chat": True}


def obs(session=True, hb=10.0, services=UP, rc=0):
    return {"services": dict(services), "session": session, "watcher_proc": True,
            "messages_proc": True, "heartbeat_age": hb, "automation_rc": rc}


def test_healthy_does_nothing():
    acts, st = w.decide(obs(), {}, 1000.0)
    assert acts == [] and "down_since" not in st


def test_dead_agent_service_is_kicked_and_watcher_not_judged():
    acts, st = w.decide(obs(hb=None, services={**UP, "com.pickem.picky-group": False}), {}, 1000.0)
    assert acts == ["kick:com.pickem.picky-group"], acts
    assert "down_since" not in st  # can't blame the watcher while the sidecar is down


def test_stale_pulse_kicks_watcher_then_alerts_on_third():
    st, now = {}, 1000.0
    for i in range(3):
        acts, st = w.decide(obs(hb=900.0), st, now + i * 600)
    assert "kick_watcher" in acts and any(a.startswith("alert:watcher:") for a in acts), acts
    assert st["kicks"] == 3


def test_no_session_alerts_and_never_kicks():
    acts, st = w.decide(obs(session=False, hb=None), {}, 1000.0)
    assert "kick_watcher" not in acts
    assert any(a.startswith("alert:session:") for a in acts)
    assert not any(a.startswith("tell_charlie:") for a in acts)


def test_tells_charlie_once_after_24h():
    st = {"down_since": 0.0}
    acts, st = w.decide(obs(session=False, hb=None), st, w.TELL_CHARLIE + 1)
    assert sum(a.startswith("tell_charlie:") for a in acts) == 1
    st["told_charlie"] = True
    acts, st = w.decide(obs(session=False, hb=None), st, w.TELL_CHARLIE + 700)
    assert not any(a.startswith("tell_charlie:") for a in acts)


def test_recovery_clears_state():
    st = {"down_since": 0.0, "kicks": 2, "told_charlie": True, "alerted": ["session"]}
    acts, st = w.decide(obs(), st, 5000.0)
    assert acts == ["recovered"]
    assert st == {"alerted": []}


def test_bad_automation_rc_alerts_while_up():
    acts, _ = w.decide(obs(rc=1), {}, 1000.0)
    assert any(a.startswith("alert:automation:") for a in acts)


def _apple_ns(y, m, d, hh=12):
    import datetime as dt
    return int((dt.datetime(y, m, d, hh).timestamp() - w.APPLE_EPOCH) * 1e9)


def test_delivery_failure_reported_once_per_handle_per_day():
    rows = [(124, "+15550001951", 22, _apple_ns(2026, 11, 17)),
            (126, "+15550001951", 22, _apple_ns(2026, 11, 17, 13)),
            (127, "+15550002222", 4, _apple_ns(2026, 11, 17))]
    ev, logged = w.new_failures(rows, {})
    assert [(e["rowid"], e["error"]) for e in ev] == [(124, 22), (127, 4)], ev
    ev2, logged = w.new_failures(rows, logged)  # next watchdog pass sees the same rows
    assert ev2 == []
    ev3, _ = w.new_failures(rows + [(140, "+15550001951", 22, _apple_ns(2026, 11, 18))], logged)
    assert [e["rowid"] for e in ev3] == [140]  # new day, reported again


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("  ok ", name)
    print("ALL GREEN")
