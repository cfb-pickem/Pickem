"""Router lane regression table (review R7): synthetic rows (repo) + the private table of real asks and exam
items, if it's on this box. A FAIL means a text now takes a lane it shouldn't; if the change was on purpose,
run `eval/lane_table.py accept`. Expected-fail rows (queued flubs) never fail the suite.

Coverage (fact-store phase 2): the private table holds every genius exam ask (all 75: the repo's questions plus
the private game-theory items) and every distinct real ask from the exchange log."""
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "eval"))
sys.path.insert(0, str(HERE))
import lane_table as LT  # noqa: E402

fails = 0
if LT.PRIVATE_TABLE.exists():
    rows = LT.load(LT.PRIVATE_TABLE)
    have = {LT.norm(r["q"]) for r in rows}
    genius = {LT.norm(q) for q in LT.genius_asks()}
    real = {LT.norm(q) for q in LT.real_asks()} - {""}
    miss_g, miss_r = genius - have, real - have
    print(f"coverage: genius {len(genius - miss_g)}/{len(genius)}, real {len(real - miss_r)}/{len(real)}")
    if len(genius) < 75 or miss_g or miss_r:
        print(f"FAIL coverage: {len(genius)} genius asks found, {len(miss_g)} missing, {len(miss_r)} real missing"
              " (run eval/lane_table.py build)")
        fails += 1

p = subprocess.run([sys.executable, "-I", str(HERE / "eval" / "lane_table.py"), "check", "--quiet"],
                   capture_output=True, text=True)
print(p.stdout)
if p.returncode:
    print(p.stderr[-2000:])
    fails += 1
print("ALL PASS" if not fails else f"{fails} FAILED")
sys.exit(1 if fails else 0)
