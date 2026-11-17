"""
Speed replay (speed lane 2026-11-17): reply latency of one build on a fixed set of keeper conversations,
with production-like cache churn between conversations. Run it INSIDE the exam lock:

  /usr/bin/lockf -t 14400 /tmp/picky-exam.lock \
      .venv/bin/python3 -I eval/speed_replay.py run <worktree> <tag> [--port 8804] [--idle 75] [--keep-warm]
  .venv/bin/python3 -I eval/speed_replay.py report <tag> [<tag> ...]

`run` starts a side sidecar (group_chat.py from <worktree>/server, every state file in the run dir),
then for each conversation: one "evictor" Ollama call with a different ~3k-token system prompt (a batch
job taking the model's one slot), ~idle seconds of quiet, then the turns one at a time (history reset
per conversation). One JSON line per turn goes to <out>/<tag>.jsonl; the sidecar log to <out>/<tag>.log.
--keep-warm sets PICKY_KEEP_WARM=1 (forces the live sidecar's keep-warm prime on in this side sidecar)
and PICKY_EXAM_LOCK="" (this run holds the exam lock itself, so the lock check would always skip).
`report` prints p50/p95/max per build, per lane and per mode, plus what the sidecar log says (cold
primes, rerolls run/skipped, keep-warm primes).

No conversation text lives in the repo: the convos come from the private keeper dir
(PICKY_SPEED_CONVOS_DIR, convos-k1.json + convos-k2.json; format id, member, mode, cat, turns) and the
member handles from the private members file. The sidecar yields to live texts (live_flag), and so
does the evictor.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import statistics
import subprocess
import sys
import time
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
import live_flag  # noqa: E402

PRIVATE = Path(os.environ.get("PICKY_SPEED_CONVOS_DIR", "/Users/Agent/cfb-pickem-private/spar/keeper"))
MEMBERS = Path(os.environ.get("PICKY_GROUP_MEMBERS", "/Users/Agent/cfb-pickem-private/picky_group_members.json"))
OUT = Path(os.environ.get("PICKY_SPEED_OUT", "/private/tmp/picky-speed2-run"))
PY = "/Users/Agent/cfb-pickem/server/.venv/bin/python3"
OLLAMA = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
MODEL = "gemma4:12b-it-qat"
# mostly group multi-turn follow-ups (the slow lane in keeper rounds k1/k2), plus three DMs
DEFAULT_IDS = ["k2-c05", "k2-c25", "k2-c40", "k2-c10", "k2-c02", "k2-c39", "k2-c20",  # group
               "k2-c01", "k2-c26", "k2-c14"]                                            # dm

# ~3k tokens of a system prompt nothing else uses: the batch job that takes the slot between texts
EVICTOR_SYSTEM = "You are a weather summarizer for a regional newsroom. Station log follows.\n" + "\n".join(
    f"Station {i:03d}: temp {40 + i % 37} F, wind {i % 23} mph from {['N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW'][i % 8]}, "
    f"humidity {30 + i % 61}%, pressure {990 + i % 41} hPa, visibility {1 + i % 10} mi."
    for i in range(130))


def load_convos(ids: list[str]) -> list[dict]:
    by_id = {}
    for f in ("convos-k1.json", "convos-k2.json"):
        for c in json.loads((PRIVATE / f).read_text()):
            by_id[c["id"]] = c
    return [by_id[i] for i in ids]


def evict() -> float:
    """One batch-job-like call with a different prefix (num_ctx matches the sidecar's, so no reload)."""
    t0 = time.time()
    while live_flag.busy() and time.time() - t0 < 600:
        time.sleep(1)
    t1 = time.time()
    r = httpx.post(f"{OLLAMA}/api/chat", timeout=600, json={
        "model": MODEL, "stream": False, "keep_alive": "24h", "think": False,
        "options": {"num_ctx": 16384, "temperature": 0.2, "num_predict": 24},
        "messages": [{"role": "system", "content": EVICTOR_SYSTEM},
                     {"role": "user", "content": "One line: which station is windiest?"}]})
    d = r.json()
    print(f"  evictor: {time.time() - t1:.1f}s, {d.get('prompt_eval_count')} prompt tokens", flush=True)
    return time.time() - t1


def run(a) -> None:
    wt = Path(a.worktree).resolve()
    d = OUT / a.tag
    d.mkdir(parents=True, exist_ok=True)
    res, logf = OUT / f"{a.tag}.jsonl", OUT / f"{a.tag}.log"
    res.write_text("")
    members = json.loads(MEMBERS.read_text())["members"]
    handle = {m["team"]: m["handles"][0] for m in members}
    secret = subprocess.run(["security", "find-generic-password", "-a", "drake", "-s",
                             "CFB_PICKEM__PICKY_GROUP_SECRET", "-w"], capture_output=True, text=True).stdout.strip()
    sha = subprocess.run(["git", "-C", str(wt), "rev-parse", "--short", "HEAD"], capture_output=True,
                         text=True).stdout.strip()
    hist, live = d / "hist.json", d / "live_games.json"
    hist.write_text("{}")
    try:
        live.write_text((wt / "server" / "cache" / "live_games.json").read_text())
    except OSError:
        pass
    env = dict(os.environ, PICKEM_CHAT_MODEL=MODEL, PICKY_OLLAMA_THINK="false", PICKY_GROUP_PORT=str(a.port),
               PICKY_GROUP_EXAM="1", PICKY_SIDE="1", PICKY_IMESSAGE_HISTORY=str(hist),
               PICKY_BITS_STATE=str(d / "bits.json"), PICKY_EXCHANGE_LOG=str(d / "ex.jsonl"),
               PICKY_BRAIN_TRACE=str(d / "trace.jsonl"), PICKY_NO_REPEAT_STATE=str(d / "norepeat.json"),
               PICKY_CALLBACKS_STATE=str(d / "callbacks.json"), PICKY_CALLBACKS_FILE=str(d / "callbacks.json"),
               PICKY_LEDGER=str(d / "ledger.json"), PICKY_LIVE_CACHE=str(live), PICKY_FRESHNESS_KICK="0",
               PICKY_IMAGE_URL="http://127.0.0.1:9", PICKY_NO_ANTHROPIC="1", PICKY_CLAUDE_USAGE_DIR=str(d / "usage"))
    if a.keep_warm:
        env.update(PICKY_KEEP_WARM="1", PICKY_EXAM_LOCK="")
    convos = load_convos(a.ids.split(",") if a.ids else DEFAULT_IDS)
    log = open(logf, "w")
    side = subprocess.Popen([PY, "-I", str(wt / "server" / "group_chat.py")], cwd=wt / "server", env=env,
                            stdout=log, stderr=log)
    try:
        for _ in range(120):
            try:
                if httpx.get(f"http://127.0.0.1:{a.port}/health", timeout=2).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(2)
        else:
            raise SystemExit("sidecar never came up")
        with httpx.Client(timeout=httpx.Timeout(connect=10, read=600, write=10, pool=10)) as cl:
            for n, c in enumerate(convos):
                evict()
                print(f"  idle {a.idle:.0f}s", flush=True)
                time.sleep(a.idle)
                hist.write_text("{}")
                h = handle.get(c["member"]) or handle["Chuck P"]
                dm = c.get("mode") == "dm"
                for k, q in enumerate(c["turns"]):
                    text = q if dm or "@picky" in q.lower() else "@Picky " + q
                    t0 = time.time()
                    try:
                        j = cl.post(f"http://127.0.0.1:{a.port}/group", headers={"X-Picky-Group-Secret": secret},
                                    json={"handle": h, "text": text, "chat": f"exam-speed-{c['id']}", "dm": dm}).json()
                        reply, lane = j.get("reply"), j.get("lane")
                    except Exception as e:  # noqa: BLE001
                        reply, lane = f"[ERROR {e!r}]", None
                    s = round(time.time() - t0, 1)
                    row = {"tag": a.tag, "sha": sha, "id": c["id"], "turn": k, "mode": c.get("mode"), "cat": c.get("cat"),
                           "lane": lane, "s": s, "reply": reply, "t": round(t0)}
                    with res.open("a") as f:
                        f.write(json.dumps(row) + "\n")
                    print(f"{c['id']} t{k} [{c.get('mode')}] {s:6.1f}s {lane or '-':<16} {(reply or '')[:90]}", flush=True)
    finally:
        side.send_signal(signal.SIGTERM)
        try:
            side.wait(15)
        except subprocess.TimeoutExpired:
            side.kill()
    report(argparse.Namespace(tags=[a.tag]))


def pct(xs: list[float], q: float) -> float:
    """Nearest-rank percentile."""
    s = sorted(xs)
    return s[max(0, min(len(s) - 1, -(-int(q * 100) * len(s) // 100) - 1))]


def stats(xs: list[float]) -> str:
    if not xs:
        return "n=0"
    return (f"n={len(xs):<3} p50={statistics.median(xs):5.1f} p95={pct(xs, 0.95):5.1f} max={max(xs):5.1f} "
            f">60s={sum(1 for x in xs if x > 60)}")


def log_facts(path: Path) -> dict:
    out = {"cold_primes": 0, "prime_failed": 0, "reroll_runs": 0, "reroll_skipped": 0, "second_drafts": 0,
           "warm_primes_info": 0, "fact_cold_fallback": 0}
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return out
    for m in re.finditer(r"INFO prime: ([\d.]+)s", text):
        out["cold_primes"] += float(m.group(1)) > 20
    out["prime_failed"] = text.count("prime failed")
    out["reroll_runs"] = text.count("reroll runs:")
    out["reroll_skipped"] = text.count("reroll skipped:")
    out["second_drafts"] = len(re.findall(r"INFO draft 2 \(", text))
    out["warm_primes_info"] = len(re.findall(r"keep-warm: primed", text))
    out["fact_cold_fallback"] = text.count("(cold prefix): code fallback")
    return out


def report(a) -> None:
    summary = {}
    for tag in a.tags:
        rows = [json.loads(x) for x in (OUT / f"{tag}.jsonl").read_text().splitlines() if x.strip()]
        xs = [r["s"] for r in rows]
        print(f"\n== {tag} ({rows[0]['sha'] if rows else '?'}) all     {stats(xs)}")
        by = {}
        for r in rows:
            by.setdefault(("mode", r["mode"]), []).append(r["s"])
            by.setdefault(("lane", (r["lane"] or "-").split(":")[0]), []).append(r["s"])
            by.setdefault(("turn", "first" if r["turn"] == 0 else "follow-up"), []).append(r["s"])
        for (kind, k), v in sorted(by.items()):
            print(f"   {kind:<5} {k:<10} {stats(v)}")
        facts = log_facts(OUT / f"{tag}.log")
        print("   log: " + ", ".join(f"{k} {v}" for k, v in facts.items()))
        summary[tag] = {"all": xs, "groups": {f"{k[0]}:{k[1]}": v for k, v in by.items()}, "log": facts,
                        "errors": sum(1 for r in rows if str(r.get("reply") or "").startswith("[ERROR"))}
    (OUT / ("summary-" + "-vs-".join(a.tags) + ".json")).write_text(json.dumps(summary, indent=1))


def main() -> None:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("worktree")
    r.add_argument("tag")
    r.add_argument("--port", type=int, default=8804)
    r.add_argument("--idle", type=float, default=75.0)
    r.add_argument("--ids", default="")
    r.add_argument("--keep-warm", action="store_true")
    rp = sub.add_parser("report")
    rp.add_argument("tags", nargs="+")
    a = p.parse_args()
    run(a) if a.cmd == "run" else report(a)


if __name__ == "__main__":
    main()
