"""Router lane regression table (review R7): synthetic rows (repo) + the private table of real asks and exam
items, if it's on this box. A FAIL means a text now takes a lane it shouldn't; if the change was on purpose,
run `eval/lane_table.py accept`. Expected-fail rows (queued flubs) never fail the suite."""
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
p = subprocess.run([sys.executable, "-I", str(HERE / "eval" / "lane_table.py"), "check", "--quiet"],
                   capture_output=True, text=True)
print(p.stdout)
if p.returncode:
    print(p.stderr[-2000:])
    sys.exit(1)
print("ALL PASS")
