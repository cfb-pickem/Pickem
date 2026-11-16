"""Picky's nightly style-LoRA cycle (Charlie, 2026-11-15: "you can train him every
night and all day Monday"). Launched by LaunchAgent com.pickem.picky-lora.

Window: 22:00-08:30 CT nightly; Sunday 22:00 runs through Tuesday 08:30 (all of Monday).
One cycle per launch (an unfinished one resumes from its checkpoint next window):
  1. data   harvest the week's weak real replies (picky_exchanges.jsonl) + fresh specs,
            claude -p writes grounded replies, validator, render (Picky still live)
  2. train  maintenance replies on, Ollama + image server off, mlx_lm LoRA under the
            memory/disk watchdog; stops at window end - 150 min and resumes next window
  3. eval   Ollama back (maintenance still on), adapter -> Ollama model, text exam on
            the live model and the candidate (+ base when live isn't base), blind judge
  4. ship   ONLY if the candidate beats live on helpful, human AND wit, with no more
            invented facts and 0 privacy leaks; else keep live and log why
  5. restore by window end, every time (maintenance off, services healthy)
Kill switch: touch /Users/Agent/cfb-pickem-private/PICKY_LORA_PAUSE
Respects /Users/Agent/picky-marathon/.tuning.lock (waits up to 45 min at start).
Log: /Users/Agent/picky-lora/logs/cycle.log, history: logs/cycle_history.jsonl
"""
from __future__ import annotations

import datetime as dt
import json
import os
import plistlib
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

CT = ZoneInfo("America/Chicago")
ROOT = Path("/Users/Agent/picky-lora")
S = Path(__file__).resolve().parent  # cfb-pickem/tools/picky-lora
REPO = Path("/Users/Agent/cfb-pickem")
PRIV = Path("/Users/Agent/cfb-pickem-private")
PAUSE = PRIV / "PICKY_LORA_PAUSE"
MAINT = PRIV / "PICKY_MAINTENANCE"
LOCK = Path("/Users/Agent/picky-marathon/.tuning.lock")
STATE = ROOT / "state.json"
LOG = ROOT / "logs/cycle.log"
HIST = ROOT / "logs/cycle_history.jsonl"
PY = str(ROOT / ".venv/bin/python")
SPY = str(REPO / "server/.venv/bin/python3")
BASE_MODEL = "qwen3.5:9b-q4_K_M"
LIVE_LORA = "picky-lora:live"
CAND = "picky-lora:cand"
CHAT_PLIST = Path.home() / "Library/LaunchAgents/com.pickem.chat.plist"
FRESH_SPECS = 120
TRAIN_MARGIN = dt.timedelta(minutes=150)  # eval + restore after training stops
EVAL_MARGIN = dt.timedelta(minutes=15)


def log(msg: str) -> None:
    line = f"{dt.datetime.now(CT):%F %T} {msg}"
    print(line, flush=True)
    with LOG.open("a") as f:
        f.write(line + "\n")


def in_window(t: dt.datetime) -> bool:
    if t.weekday() == 0:  # all of Monday
        return True
    if t.weekday() == 1 and (t.hour, t.minute) < (8, 30):
        return True
    return t.hour >= 22 or (t.hour, t.minute) < (8, 30)


def window_end(t: dt.datetime) -> dt.datetime:
    end = t.replace(hour=8, minute=30, second=0, microsecond=0)
    if t >= end:
        end += dt.timedelta(days=1)
    if end.weekday() == 0:  # Monday 08:30 -> keep going through Tuesday 08:30
        end += dt.timedelta(days=1)
    return end


# ------------------------------------------------------------------ lock
def lock_holder() -> int | None:
    try:
        pid = int(LOCK.read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return None
    try:
        os.kill(pid, 0)
        return pid
    except OSError:
        return None


def take_lock(wait_s: int = 2700) -> bool:
    t0 = time.time()
    while True:
        if LOCK.exists() and lock_holder() is None:
            log(f"removing stale lock: {LOCK.read_text().strip()!r}")
            LOCK.unlink(missing_ok=True)
        try:
            fd = os.open(LOCK, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            os.write(fd, f"{os.getpid()} {int(time.time())} picky-lora-cycle\n".encode())
            os.close(fd)
            return True
        except FileExistsError:
            if time.time() - t0 > wait_s:
                return False
            time.sleep(60)


def release_lock() -> None:
    if lock_holder() == os.getpid():
        LOCK.unlink(missing_ok=True)


# ------------------------------------------------------------------ helpers
def run(cmd: list[str], timeout: float | None = None, env: dict | None = None, **kw) -> int:
    log("$ " + " ".join(cmd))
    try:
        return subprocess.run(cmd, timeout=timeout, env={**os.environ, **(env or {})}, **kw).returncode
    except subprocess.TimeoutExpired:
        log(f"timed out after {timeout:.0f}s")
        return 124


def state() -> dict:
    return json.loads(STATE.read_text()) if STATE.exists() else {}


def save(st: dict) -> None:
    STATE.write_text(json.dumps(st, indent=1))


def live_model() -> str:
    env = plistlib.loads(CHAT_PLIST.read_bytes()).get("EnvironmentVariables") or {}
    return env.get("PICKEM_CHAT_MODEL", BASE_MODEL)


def checkpoints(d: Path) -> list[tuple[int, Path]]:
    out = []
    for p in d.glob("0*_adapters.safetensors"):
        m = re.match(r"(\d+)_adapters", p.name)
        if m:
            out.append((int(m.group(1)), p))
    return sorted(out)


# ------------------------------------------------------------------ phases
def phase_data(st: dict) -> bool:
    d = ROOT / "data/cycles" / st["cycle"]
    d.mkdir(parents=True, exist_ok=True)
    run([PY, "-I", str(S / "harvest.py"), str(d / "harvested.jsonl")], timeout=1800)
    fresh = d / "fresh.jsonl"
    run([PY, "-I", str(S / "make_specs.py"), st["cycle"], str(FRESH_SPECS), str(fresh), f"c{st['cycle'][-4:]}-"])
    specs = (d / "harvested.jsonl").read_text() if (d / "harvested.jsonl").exists() else ""
    (d / "specs.jsonl").write_text(specs + fresh.read_text())
    run([PY, "-I", str(S / "gen.py"), str(d)], timeout=3600)
    run([PY, "-I", str(S / "validate.py"), str(d)])
    r = subprocess.run([PY, "-I", str(S / "render.py")], capture_output=True, text=True)
    log(r.stdout.strip().replace("\n", " | "))
    m = re.search(r"train (\d+) valid (\d+)", r.stdout)
    if not m:
        log("render failed: " + r.stderr[-500:])
        return False
    st["n_train"], st["n_valid"] = int(m.group(1)), int(m.group(2))
    st["iters"] = 2 * st["n_train"]
    st["phase"] = "train"
    save(st)
    return True


def phase_train(st: dict, end: dt.datetime) -> str:
    """'done' | 'paused' (deadline; resumes next window) | 'failed'.
    Each window's run gets its own runs/<cycle>/partN dir (mlx_lm numbers a resumed
    run's checkpoints from 0); st["done"] counts iterations across parts."""
    if st.get("final"):
        return "done"
    deadline = end - TRAIN_MARGIN
    if dt.datetime.now(CT) >= deadline:
        return "paused"
    out = ROOT / "runs" / st["cycle"]
    parts = sorted(out.glob("part*"))
    resume = next((checkpoints(p)[-1][1] for p in reversed(parts) if checkpoints(p)), None)
    done = st.get("done", 0)
    part = out / f"part{len(parts)}"
    part.mkdir(parents=True, exist_ok=True)
    cmd = [PY, str(S / "train.py"), "-c", str(ROOT / "lora_config.yaml"), "--adapter-path", str(part),
           "--iters", str(st["iters"] - done), "--steps-per-eval", str(max(50, st["iters"] // 8)),
           "--val-batches", str(st["n_valid"])]
    if resume and done:
        cmd += ["--resume-adapter-file", str(resume)]
        log(f"resuming from {resume} ({done}/{st['iters']})")
    if run([str(S / "picky_down.sh")]) != 0:
        log("picky_down failed")
        return "failed"
    (ROOT / "logs/MEM_ABORT").unlink(missing_ok=True)
    tl = open(ROOT / f"logs/train-{st['cycle']}.log", "a")
    p = subprocess.Popen(cmd, stdout=tl, stderr=subprocess.STDOUT, cwd=str(ROOT))
    subprocess.Popen([str(S / "memwatch.sh"), str(p.pid), str(part)])
    log(f"training pid {p.pid}, {st['iters'] - done} iters, deadline {deadline:%a %H:%M}")
    while p.poll() is None:
        if dt.datetime.now(CT) >= deadline:
            log("train deadline: stopping (checkpoints resume next window)")
            p.send_signal(signal.SIGINT)
            try:
                p.wait(60)
            except subprocess.TimeoutExpired:
                p.kill()
            break
        time.sleep(30)
    cps = checkpoints(part)
    if (part / "adapters.safetensors").exists() and p.returncode == 0:
        st.update(done=st["iters"], final=str(part / "adapters.safetensors"))
        save(st)
        return "done"
    st["done"] = done + (cps[-1][0] if cps else 0)
    save(st)
    if (ROOT / "logs/MEM_ABORT").exists():
        log("memory/disk watchdog stopped training: " + (ROOT / "logs/MEM_ABORT").read_text().strip())
        return "failed"
    if dt.datetime.now(CT) >= deadline:
        return "paused"
    log(f"training exited {p.returncode}")
    return "failed"


def judge_scores(tag: str) -> dict:
    j = json.loads((REPO / f"server/eval/judge-{tag}.json").read_text())
    vals = [v for v in j.values() if isinstance(v, dict)]

    def mean(k):
        xs = [v[k] for v in vals if isinstance(v.get(k), (int, float)) and not isinstance(v.get(k), bool)]
        return round(sum(xs) / len(xs), 2) if xs else 0.0
    return {"helpful": mean("helpful"), "human": mean("human"), "wit": mean("roast"),
            "invented": sum(bool(v.get("invented")) for v in vals), "leaks": sum(bool(v.get("leak")) for v in vals),
            "passed": sum(bool(v.get("pass")) for v in vals), "n": len(vals)}


def phase_eval(st: dict, end: dt.datetime) -> dict:
    deadline = end - EVAL_MARGIN
    if run([PY, "-I", str(S / "to_ollama.py"), st["final"], CAND], timeout=900) != 0:
        return {"ship": False, "why": "adapter -> Ollama conversion failed"}
    live = live_model()
    models = {"live": live, "cand": CAND}
    if live != BASE_MODEL:
        models["base"] = BASE_MODEL
    tags = {}
    for name, model in models.items():
        left = (deadline - dt.datetime.now(CT)).total_seconds()
        if left < 1500:
            return {"ship": False, "why": f"out of window before the {name} exam"}
        tag = f"lora-{st['cycle']}-{name}"
        if run([str(S / "eval_model.sh"), model, tag], timeout=left - 600) != 0:
            return {"ship": False, "why": f"{name} exam failed"}
        tags[name] = tag
    results = [str(REPO / f"server/eval/results/{t}.json") for t in tags.values()]
    if run([SPY, "-I", "eval/judge_text.py", *results], cwd=str(REPO / "server"), timeout=1800) != 0:
        return {"ship": False, "why": "judge failed"}
    sc = {k: judge_scores(t) for k, t in tags.items()}
    c, l = sc["cand"], sc["live"]
    why = []
    for k in ("helpful", "human", "wit"):
        if c[k] <= l[k]:
            why.append(f"{k} {l[k]} -> {c[k]}")
    if c["invented"] > l["invented"]:
        why.append(f"invented {l['invented']} -> {c['invented']}")
    if c["leaks"]:
        why.append(f"privacy leaks {c['leaks']}")
    return {"ship": not why, "why": "; ".join(why) or "beats live on all gates", "scores": sc}


def ship() -> bool:
    if run(["ollama", "cp", CAND, LIVE_LORA]) != 0:
        return False
    pl = plistlib.loads(CHAT_PLIST.read_bytes())
    env = pl.setdefault("EnvironmentVariables", {})
    if env.get("PICKEM_CHAT_MODEL") != LIVE_LORA:
        bak = ROOT / f"logs/com.pickem.chat.plist.{int(time.time())}"
        bak.write_bytes(CHAT_PLIST.read_bytes())
        env["PICKEM_CHAT_MODEL"] = LIVE_LORA
        CHAT_PLIST.write_bytes(plistlib.dumps(pl))
    uid = os.getuid()
    for label in ("com.pickem.chat", "com.pickem.picky-group"):
        run(["launchctl", "kickstart", "-k", f"gui/{uid}/{label}"])
    return True


def notify(text: str) -> None:
    run([SPY, "-I", "/Users/Agent/picky-marathon/wit_eval.py", "notify", text], timeout=60)


def restore() -> None:
    run([str(S / "picky_up.sh")], timeout=600)


# ------------------------------------------------------------------ main
def ensure_up() -> int:
    """08:35 safety net: if no cycle is running but Picky was left down, restore him."""
    holder = lock_holder()
    if holder:
        log(f"ensure: lock held by live pid {holder} ({LOCK.read_text().split()[-1]}): leaving it alone")
        return 0
    if subprocess.run(["pgrep", "-f", "mlx_lm|picky-lora/scripts/train.py"], capture_output=True).returncode == 0:
        log("ensure: training is running: leaving it alone")
        return 0
    try:
        up = subprocess.run(["curl", "-sf", "-m", "5", "http://127.0.0.1:11434/api/tags"],
                            capture_output=True).returncode == 0
    except OSError:
        up = False
    if MAINT.exists() or not up:
        log(f"ensure: Picky left down (maintenance={MAINT.exists()}, ollama_up={up}); restoring")
        restore()
    return 0


def main() -> int:
    if sys.argv[1:] == ["ensure"]:
        return ensure_up()
    now = dt.datetime.now(CT)
    if PAUSE.exists():
        log("PICKY_LORA_PAUSE present: skipping")
        return 0
    if not in_window(now):  # e.g. the 08:35 launch: make sure Picky was left up
        return ensure_up()
    end = window_end(now)
    if not take_lock():
        log(f"tuning lock held by pid {lock_holder()} for 45 min: skipping this window")
        return 0
    went_down = False
    try:
        st = state()
        if st.get("phase") in (None, "done", "failed"):
            cid = (now - dt.timedelta(hours=9)).strftime("%Y%m%d")
            if st.get("cycle") == cid and st.get("phase") == "done":
                log(f"cycle {cid} already finished this window")
                return 0
            st = {"cycle": cid, "phase": "data", "started": now.isoformat()}
            save(st)
            log(f"cycle {cid} start, window ends {end:%a %H:%M}")
        if st["phase"] == "data" and not phase_data(st):
            st["phase"] = "failed"
            save(st)
            return 1
        if st["phase"] == "train":
            went_down = True
            r = phase_train(st, end)
            log(f"train: {r}")
            if r != "done":
                if r == "failed":
                    st["phase"] = "failed"
                    save(st)
                    notify(f"Picky LoRA cycle {st['cycle']}: training failed, live model unchanged. "
                           f"See {LOG}.")
                return 0 if r == "paused" else 1
            st["phase"] = "eval"
            save(st)
        if st["phase"] == "eval":
            went_down = True
            run([str(S / "picky_up.sh")], env={"KEEP_MAINT": "1"}, timeout=600)
            res = phase_eval(st, end)
            log(f"eval: {json.dumps(res)}")
            shipped = bool(res.get("ship")) and ship()
            st.update(phase="done", result=res, shipped=shipped, finished=dt.datetime.now(CT).isoformat())
            save(st)
            with HIST.open("a") as f:
                f.write(json.dumps(st) + "\n")
            if shipped:
                c = res["scores"]["cand"]
                notify(f"Picky got a new fine-tune (cycle {st['cycle']}): helpful {c['helpful']}, human "
                       f"{c['human']}, wit {c['wit']}, invented {c['invented']}. It's live.")
        return 0
    finally:
        if went_down:  # only undo what this run did; ensure_up covers anything left behind
            restore()
        release_lock()


if __name__ == "__main__":
    sys.exit(main())
