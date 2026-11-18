"""tools/release (review R4): scripts parse, the freeze is checked before anything ships, the repoint plan
moves code paths (never logs or venvs) and leaves the eval jobs on the dev tree."""
import plistlib
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REL = HERE.parent / "tools" / "release"
sys.path.insert(0, str(REL))
import repoint  # noqa: E402


def test_scripts_parse():
    for f in ("release.sh", "alltests.sh", "mkwt.sh"):
        assert subprocess.run(["/bin/zsh", "-n", str(REL / f)]).returncode == 0, f


def test_release_order():
    s = (REL / "release.sh").read_text()
    i_freeze, i_suite = s.index("freeze.py check-freeze"), s.index("alltests.sh $WT")
    i_tag, i_live = s.index("tag -a $TAG"), s.rindex("__live $TAG $SHA")
    assert i_freeze < i_suite < i_tag < i_live, "freeze -> suite -> tag -> switch"
    assert 'FAILS: none' in s and "rolled-back" in s and "canary.py --model" in s and "$ELOCK" in s


def test_repoint_plan():
    d = Path(tempfile.mkdtemp())
    dev, live = "/Users/Agent/cfb-pickem", "/Users/Agent/picky-live"
    for label, args in (("com.pickem.picky-group", [f"{dev}/server/.venv/bin/python3", f"{dev}/server/group_chat.py"]),
                        ("com.pickem.genius-nightly", ["/bin/zsh", f"{dev}/server/eval/genius/nightly.sh"]),
                        ("com.pickem.picky-watchdog", ["/usr/bin/python3", "-I", f"{dev}/tools/picky-imessage/watchdog.py"])):
        (d / f"{label}.plist").write_bytes(plistlib.dumps({"Label": label, "ProgramArguments": args,
                                                          "WorkingDirectory": f"{dev}/server",
                                                          "StandardErrorPath": f"{dev}/server/logs/x.log"}))
    repoint.AGENTS = d
    plan = {nd["Label"]: nd for _, nd in repoint.plan(dev, live)}
    assert set(plan) == {"com.pickem.picky-group", "com.pickem.picky-watchdog"}, plan.keys()
    g = plan["com.pickem.picky-group"]
    assert g["ProgramArguments"] == [f"{dev}/server/.venv/bin/python3", f"{live}/server/group_chat.py"]
    assert g["WorkingDirectory"] == f"{live}/server" and g["StandardErrorPath"] == f"{dev}/server/logs/x.log"


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("ok  ", name)
            except AssertionError as e:
                fails += 1
                print("FAIL", name, e)
    print("ALL PASS" if not fails else f"{fails} FAILED")
    sys.exit(1 if fails else 0)
