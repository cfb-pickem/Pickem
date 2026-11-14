"""
Run the Picky exam against the live /chat endpoint and write a report.

    cd server
    ./.venv/bin/python3 -I eval/run.py                 # full exam, live tunnel
    ./.venv/bin/python3 -I eval/run.py --local         # http://127.0.0.1:8766
    ./.venv/bin/python3 -I eval/run.py --only privacy,rules-jackpot
    ./.venv/bin/python3 -I eval/run.py --cold          # unload the model first, time the cold start
    ./.venv/bin/python3 -I eval/run.py --image         # also check the image generator end to end
    ./.venv/bin/python3 -I eval/run.py --render eval/results/<run>.json --judge eval/judge.json

Each persona gets a real session minted with the service key (admin
generate_link + /auth/v1/verify; no email is sent). Questions go one at a
time (one GPU) and respect the 10/min/user rate limit.

Grading: deterministic checks per item plus global checks on every answer
(privacy, record claims, money, slurs, land acknowledgment count). Items with
a `judge` rubric also need a verdict in a judge file, a JSON object
{item_id: {"pass": true|false, "note": "..."}} written by whoever reads the
answers (a human, or Claude in a dev session; never an API call from here).
Picky itself never calls a hosted model, and neither does this runner.
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import datetime as dt
import json
import statistics
import sys
import time
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import checks  # noqa: E402
import questions  # noqa: E402
from truth import Truth, chat  # noqa: E402

REMOTE = "https://pickem-chat.dunecreek.ai/chat"
LOCAL = "http://127.0.0.1:8766/chat"
RESULTS = HERE / "results"
REPORTS = HERE / "reports"


# ---- sessions -------------------------------------------------------------------


def mint_jwt(key: str, uid: str) -> str:
    h = {"apikey": key, "Authorization": f"Bearer {key}"}
    email = httpx.get(f"{chat.AUTH}/admin/users/{uid}", headers=h, timeout=15).json()["email"]
    link = httpx.post(f"{chat.AUTH}/admin/generate_link", headers=h,
                      json={"type": "magiclink", "email": email}, timeout=15).json()
    tok = (link.get("properties") or link)["hashed_token"]
    s = httpx.post(f"{chat.AUTH}/verify", headers={"apikey": chat.SUPABASE_ANON},
                   json={"type": "magiclink", "token_hash": tok}, timeout=15).json()
    return s["access_token"]


class Pacer:
    """Stay under chat.py's 10 requests/min/user."""

    def __init__(self, per_min: int = 9):
        self.per_min, self.hits = per_min, collections.defaultdict(collections.deque)

    async def wait(self, uid: str):
        q = self.hits[uid]
        while q and time.time() - q[0] > 60:
            q.popleft()
        if len(q) >= self.per_min:
            await asyncio.sleep(61 - (time.time() - q[0]))
        q.append(time.time())


async def ask(client, url, jwt, history, mode) -> tuple[str, float, float, bool]:
    """(answer, seconds to first token, total seconds, got_image)."""
    t0, ttft, out, image = time.time(), None, [], False
    async with client.stream("POST", url, json={"messages": history, "supabase_jwt": jwt, "mode": mode}) as r:
        if r.status_code != 200:
            return f"[HTTP {r.status_code}] {(await r.aread())[:200]!r}", 0.0, time.time() - t0, False
        async for line in r.aiter_lines():
            if not line.startswith("data:"):
                continue
            p = line[5:].strip()
            if p == "[DONE]":
                break
            obj = json.loads(p)
            if obj.get("error"):
                out.append(f"[ERROR {obj['error']}]")
            if obj.get("image"):
                image = True
            if obj.get("content"):
                ttft = ttft if ttft is not None else time.time() - t0
                out.append(obj["content"])
    return "".join(out), ttft or 0.0, time.time() - t0, image


# ---- grading ---------------------------------------------------------------------


def grade(item: dict, answers: list[str], t: Truth, P: dict) -> list:
    caller = P[item["persona"]]
    res = []
    for i, a in enumerate(answers):
        for name, ok, det in (checks.check_no_error(a), checks.check_privacy(a, t, caller),
                              checks.check_record_claims(a, t), checks.check_no_money(a),
                              checks.check_no_slurs(a)):
            res.append((f"{name}" + (f"@t{i + 1}" if len(answers) > 1 else ""), ok, det))
    res.append(checks.check_ack(answers, item["persona"] == "df"))
    try:
        res += [(n, bool(ok), d) for n, ok, d in item["check"](answers, t, P)]
    except Exception as e:  # noqa: BLE001 - a broken check is a failed check, loudly
        res.append(("check_crashed", False, repr(e)[:200]))
    return res


def verdict(r: dict, judge: dict) -> str:
    if r.get("skip"):
        return "skip"
    det_ok = all(ok for _, ok, _ in r["checks"])
    if r.get("judge"):
        j = judge.get(r["id"])
        if j is None:
            return "pending" if det_ok else "fail"
        return "pass" if det_ok and j.get("pass") else "fail"
    return "pass" if det_ok else "fail"


def gates(results: list[dict], judge: dict) -> dict:
    out = {"privacy leaks": 0, "invented data": 0, "guardrail violations": 0}
    for r in results:
        if r.get("skip"):
            continue
        names = {n.split("@")[0] for n, ok, _ in r["checks"] if not ok}
        v = verdict(r, judge)
        if "privacy" in names or (r["cat"] == "privacy" and v == "fail"):
            out["privacy leaks"] += 1
        if "record_claims" in names or (r["cat"] in ("invented", "stats") and v == "fail"
                                        and names & {"no_numbers", "no_invented_record", "no_invented_number", "admits_no_data"}) \
                or (r["cat"] == "invented" and v == "fail"):
            out["invented data"] += 1
        if "no_slurs" in names or (r["cat"] == "guardrails" and v == "fail"):
            out["guardrail violations"] += 1
    return out


def render(run: dict, judge: dict) -> str:
    res = run["results"]
    by_cat = collections.defaultdict(list)
    for r in res:
        by_cat[r["cat"]].append(verdict(r, judge))
    scored = [v for vs in by_cat.values() for v in vs if v in ("pass", "fail")]
    passed = scored.count("pass")
    lat = [tn["total"] for r in res if not r.get("skip") for tn in r["turns"]]
    ttft = [tn["ttft"] for r in res if not r.get("skip") for tn in r["turns"] if tn["ttft"]]
    g = gates(res, judge)
    L = [f"# Picky exam: {run['tag']}", "",
         f"- Run at {run['started']} against `{run['url']}`, model `{run.get('model')}`",
         f"- Season {run['season']}, week {run['week']}; games kicked off: {run['open_games']} of {run['n_games']}",
         f"- Personas: " + ", ".join(f"{k} = {v}" for k, v in run["personas"].items()),
         f"- CFBD in chat.py: {run['chat_has_cfbd']}; rules source loaded: {run['rules_source']}",
         "",
         f"## Score: {passed}/{len(scored)} ({100 * passed / max(1, len(scored)):.0f}%)"
         + (f", {sum(v == 'pending' for vs in by_cat.values() for v in vs)} pending judge" if any(
             v == "pending" for vs in by_cat.values() for v in vs) else ""),
         "",
         "Hard gates: " + ", ".join(f"{k}: **{v}**" for k, v in g.items()),
         "",
         "| category | pass | fail | pending | skip |", "|---|---|---|---|---|"]
    for c in questions.CATEGORIES:
        vs = by_cat.get(c, [])
        L.append(f"| {c} | {vs.count('pass')} | {vs.count('fail')} | {vs.count('pending')} | {vs.count('skip')} |")
    if lat:
        L += ["", f"Latency (s): total median {statistics.median(lat):.1f}, max {max(lat):.1f}; "
                  f"first token median {statistics.median(ttft):.1f}, max {max(ttft):.1f}"
              + (f"; cold first answer {run['cold_s']:.1f}" if run.get("cold_s") else "")]
    if run.get("image"):
        L += ["", f"Image check: {run['image']}"]
    L += ["", "## Items", ""]
    for r in res:
        v = verdict(r, judge)
        L.append(f"### [{v.upper()}] {r['id']} ({r['cat']}, as {r['persona_team']})")
        if r.get("skip"):
            L += [f"skipped: {r['skip']}", ""]
            continue
        for tn in r["turns"]:
            L.append(f"- **Q:** {tn['q']}  _({tn['ttft']:.1f}s first token, {tn['total']:.1f}s total)_")
            L.append("  **A:** " + tn["a"].replace("\n", "\n  "))
        bad = [f"`{n}` ({d})" if d else f"`{n}`" for n, ok, d in r["checks"] if not ok]
        L.append(f"- checks: {len(r['checks']) - len(bad)}/{len(r['checks'])} ok" + (f"; FAILED: {', '.join(bad)}" if bad else ""))
        if r.get("judge"):
            j = judge.get(r["id"])
            L.append(f"- judge rubric: {r['judge']}")
            L.append(f"- judge: {'pending' if j is None else ('PASS' if j.get('pass') else 'FAIL')}"
                     + (f": {j.get('note')}" if j and j.get("note") else ""))
        L.append("")
    return "\n".join(L)


# ---- main -------------------------------------------------------------------------


async def run(args):
    t = await Truth().load()
    P = questions.personas(t)
    items = questions.build(t)
    if args.only:
        keys = set(args.only.split(","))
        items = [i for i in items if i["id"] in keys or i["cat"] in keys]
    if args.dry:  # print the exam, ask nothing
        print("personas:", P, "| cfbd in chat:", t.chat_has_cfbd, "| rules source:", bool(t.rules_text))
        for it in items:
            print(f"- {it['id']} [{it['cat']}, {it['persona']}] {it['turns']}" + (f" SKIP: {it['skip']}" if it["skip"] else ""))
        print(f"{len(items)} items")
        return
    url = LOCAL if args.local else (args.url or REMOTE)
    started = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    tag = args.tag or dt.datetime.now().strftime("%Y%m%d-%H%M")
    jwts = {k: mint_jwt(t.key, t.uid_by_team[v]) for k, v in P.items() if v}
    pacer = Pacer()
    model = None
    async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10, read=300, write=10, pool=10)) as client:
        try:
            model = (await client.get(url.replace("/chat", "/health"))).json().get("model")
        except Exception:  # noqa: BLE001
            pass
        cold_s = None
        if args.cold:
            await client.post(f"{chat.OLLAMA}/api/generate", json={"model": model or chat.MODEL, "keep_alive": 0})
            await asyncio.sleep(3)
        results = []
        for n, it in enumerate(items, 1):
            r = {k: it[k] for k in ("id", "cat", "persona", "judge", "skip", "mode")}
            r["persona_team"] = P[it["persona"]]
            r["turns"] = []
            if it["skip"]:
                results.append(r | {"checks": []})
                print(f"[{n}/{len(items)}] {it['id']}: SKIP ({it['skip']})", flush=True)
                continue
            history, answers = [], []
            for q in it["turns"]:
                await pacer.wait(it["persona"])
                history.append({"role": "user", "content": q})
                a, ttft, total, _img = await ask(client, url, jwts[it["persona"]], history, it["mode"])
                if cold_s is None and args.cold:
                    cold_s = total
                history.append({"role": "assistant", "content": a})
                answers.append(a)
                r["turns"].append({"q": q, "a": a, "ttft": ttft, "total": total})
            r["checks"] = grade(it, answers, t, P)
            results.append(r)
            ok = all(c[1] for c in r["checks"])
            print(f"[{n}/{len(items)}] {it['id']}: {'ok' if ok else 'FAIL'} "
                  f"({sum(x['total'] for x in r['turns']):.1f}s)"
                  + ("" if ok else "  " + ", ".join(c[0] for c in r["checks"] if not c[1])), flush=True)
        image = None
        if args.image:
            await pacer.wait("me")
            a, _ttft, total, got = await ask(client, url, jwts["me"], [
                {"role": "user", "content": "draw me a bulldog wearing a cowboy hat"}], "leaderboard")
            image = f"{'OK, image returned' if got else 'FAILED: ' + a[:120]} in {total:.0f}s"
            # the next chat after a picture is cold: time it
            await pacer.wait("me")
            _a, _t, after, _ = await ask(client, url, jwts["me"], [{"role": "user", "content": "Who's winning?"}], "leaderboard")
            image += f"; next chat after it took {after:.0f}s"
            print("image:", image, flush=True)
    runrec = {"tag": tag, "started": started, "url": url, "model": model, "season": t.season, "week": t.week,
              "n_games": len(t.games), "open_games": len(t.open_games), "personas": P,
              "chat_has_cfbd": t.chat_has_cfbd, "rules_source": bool(t.rules_text),
              "cold_s": cold_s, "image": image, "results": results}
    RESULTS.mkdir(exist_ok=True)
    REPORTS.mkdir(exist_ok=True)
    (RESULTS / f"{tag}.json").write_text(json.dumps(runrec, indent=1))
    judge = json.loads(Path(args.judge).read_text()) if args.judge else {}
    (REPORTS / f"{tag}.md").write_text(render(runrec, judge))
    print(f"\nwrote {RESULTS / (tag + '.json')} and {REPORTS / (tag + '.md')}")
    print(render(runrec, judge).split("## Items")[0])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url")
    ap.add_argument("--local", action="store_true")
    ap.add_argument("--only", default="")
    ap.add_argument("--tag", default="")
    ap.add_argument("--cold", action="store_true")
    ap.add_argument("--image", action="store_true")
    ap.add_argument("--judge", default="")
    ap.add_argument("--dry", action="store_true", help="print the exam without asking anything")
    ap.add_argument("--render", default="", help="re-render a results JSON (with --judge) without asking anything")
    args = ap.parse_args()
    if args.render:
        runrec = json.loads(Path(args.render).read_text())
        judge = json.loads(Path(args.judge).read_text()) if args.judge else {}
        out = REPORTS / f"{runrec['tag']}.md"
        out.write_text(render(runrec, judge))
        print(render(runrec, judge).split("## Items")[0])
        print(f"wrote {out}")
        return
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
