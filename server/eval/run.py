"""
Run the Picky exam against the live /chat endpoint and write a report.

    cd server
    ./.venv/bin/python3 -I eval/run.py                 # full exam, live tunnel
    ./.venv/bin/python3 -I eval/run.py --local         # http://127.0.0.1:8766
    ./.venv/bin/python3 -I eval/run.py --only privacy,rules-jackpot
    ./.venv/bin/python3 -I eval/run.py --cold          # unload the model first, time the cold start
    ./.venv/bin/python3 -I eval/run.py --image         # also check the image generator end to end
    ./.venv/bin/python3 -I eval/run.py --render eval/results/<run>.json --judge eval/judge.json
    ./.venv/bin/python3 -I eval/run.py --no-model      # GPU-free: unit tests + truth/exam build, asks Picky nothing

Every request carries X-Picky-Test (token from keychain CFB_PICKEM__TEST_TOKEN),
so the exam runs at low priority: a league member's question preempts it.
A preempted question is retried with backoff, never graded as a failure, and
counted in the report; latency is timed on the successful attempt only.

Each persona gets a real session minted with the service key (admin
generate_link + /auth/v1/verify; no email is sent). Questions go one at a
time (one GPU) and respect the 10/min/user rate limit.

Grading: deterministic checks per item plus global checks on every answer
(privacy, record claims, money, slurs, no land acknowledgment). Items with
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

import os
import plistlib
import re
import random
import subprocess

import httpx

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
# Same model settings as the live service (model, think, keep_alive), so the
# in-process --at mode runs exactly what com.pickem.chat runs.
_PLIST = Path.home() / "Library/LaunchAgents/com.pickem.chat.plist"
if _PLIST.exists():
    for k, v in (plistlib.loads(_PLIST.read_bytes()).get("EnvironmentVariables") or {}).items():
        os.environ.setdefault(k, str(v))
# the exam is side traffic: its in-process model calls (--at) yield to live texts (server/live_flag.py)
os.environ.setdefault("PICKY_SIDE", "1")
sys.path.append(str(HERE.parent))
import checks  # noqa: E402
import live_flag  # noqa: E402
import group_items  # noqa: E402
import questions  # noqa: E402
from truth import Truth, chat  # noqa: E402

REMOTE = "https://pickem-chat.dunecreek.ai/chat"
LOCAL = "http://127.0.0.1:8766/chat"
RESULTS = HERE / "results"
REPORTS = HERE / "reports"
TEST_TOKEN_SERVICE = "CFB_PICKEM__TEST_TOKEN"
MAX_PREEMPT_RETRIES = 40
GROUP_SECRET_SERVICE = "CFB_PICKEM__PICKY_GROUP_SECRET"
MEMBERS_FILE = Path(os.environ.get("PICKY_GROUP_MEMBERS", "/Users/Agent/cfb-pickem-private/picky_group_members.json"))


def test_token() -> str:
    tok = os.environ.get("PICKY_TEST_TOKEN") or subprocess.run(
        ["security", "find-generic-password", "-a", "drake", "-s", TEST_TOKEN_SERVICE, "-w"],
        capture_output=True, text=True).stdout.strip()
    if not tok:
        sys.exit(f"no {TEST_TOKEN_SERVICE} in the keychain: the exam would run at member priority")
    return tok


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


class Preempted(Exception):
    def __init__(self, retry_after: float):
        self.retry_after = retry_after


async def ask_once(client, url, jwt, history, mode) -> tuple[str, float, float, bool, float]:
    """(answer, seconds to first token, total seconds, got_image, gate wait).
    Timings start when the gate admits the request. Raises Preempted."""
    t0, ttft, out, image, gate_wait = time.time(), None, [], False, 0.0
    async with client.stream("POST", url, json={"messages": history, "supabase_jwt": jwt, "mode": mode}) as r:
        if r.status_code != 200:
            return f"[HTTP {r.status_code}] {(await r.aread())[:200]!r}", 0.0, time.time() - t0, False, 0.0
        async for line in r.aiter_lines():
            if not line.startswith("data:"):
                continue
            p = line[5:].strip()
            if p == "[DONE]":
                break
            obj = json.loads(p)
            if obj.get("preempted"):
                raise Preempted(float(obj.get("retry_after") or 3))
            if "gate_wait" in obj:
                gate_wait = float(obj["gate_wait"])
                t0 = time.time()
                continue
            if obj.get("error"):
                out.append(f"[ERROR {obj['error']}]")
            if obj.get("image"):
                image = True
            if obj.get("content"):
                ttft = ttft if ttft is not None else time.time() - t0
                out.append(obj["content"])
    return "".join(out), ttft or 0.0, time.time() - t0, image, gate_wait


async def ask(client, url, jwt, history, mode, pacer=None, who="me") -> tuple[str, float, float, bool, dict]:
    """ask_once, retried with backoff while league members preempt it.
    The dict holds this question's preemptions and total wait (gate + backoff)."""
    gate = {"preemptions": 0, "wait": 0.0}
    for attempt in range(MAX_PREEMPT_RETRIES + 1):
        if pacer:
            await pacer.wait(who)
        t_try = time.time()
        try:
            a, ttft, total, image, waited = await ask_once(client, url, jwt, history, mode)
            gate["wait"] += waited
            return a, ttft, total, image, gate
        except Preempted as p:
            gate["preemptions"] += 1
            pause = min(30.0, p.retry_after + 2 * attempt) + random.uniform(0, 2)
            gate["wait"] += time.time() - t_try + pause
            print(f"  preempted by a league member, retry in {pause:.0f}s", flush=True)
            await asyncio.sleep(pause)
    return f"[ERROR preempted {MAX_PREEMPT_RETRIES + 1} times]", 0.0, 0.0, False, gate


def group_handles() -> dict[str, str]:
    """team -> one of its iMessage handles (private map; never printed)."""
    d = json.loads(MEMBERS_FILE.read_text())
    return {m["team"]: (m.get("handles") or [""])[0] for m in d.get("members") or [] if m.get("team")}


async def ask_group(client, url, secret, handle, text, chat_id, recent) -> tuple[str, float]:
    """One text through the group sidecar (/group) as the member with `handle`.
    (reply, seconds). The sidecar keeps the per-chat history, so multi-turn
    items share one chat id."""
    for attempt in range(3):  # a Supabase blip is a 502 from the sidecar: not Picky's answer
        t0 = time.time()
        r = await client.post(url, headers={"X-Picky-Group-Secret": secret},
                              json={"handle": handle, "text": text, "chat": chat_id, "recent": recent})
        if r.status_code < 500:
            break
        print(f"  sidecar {r.status_code}, retry in 15s", flush=True)
        await asyncio.sleep(15)
    if r.status_code != 200:
        return f"[HTTP {r.status_code}] {r.text[:200]}", time.time() - t0
    return (r.json().get("reply") or ""), time.time() - t0


# ---- grading ---------------------------------------------------------------------


def grade(item: dict, answers: list[str], t: Truth, P: dict) -> list:
    caller = P[item["persona"]]
    res = []
    for i, a in enumerate(answers):
        for name, ok, det in (checks.check_no_error(a), checks.check_length(a), checks.check_privacy(a, t, caller),
                              checks.check_record_claims(a, t, caller), checks.check_no_money(a, t.rules_text),
                              checks.check_no_slurs(a)):
            res.append((f"{name}" + (f"@t{i + 1}" if len(answers) > 1 else ""), ok, det))
    res.append(checks.check_ack(answers))
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
    """Hard gates. A judge verdict can flag {"leak"|"invented"|"violation": true}
    so a wrong-but-harmless answer (e.g. over-hiding a revealed pick, a
    miscounted record in a guardrail pivot) isn't booked as a leak/violation."""
    out = {"privacy leaks": 0, "invented data": 0, "guardrail violations": 0}
    for r in results:
        if r.get("skip"):
            continue
        names = {n.split("@")[0] for n, ok, _ in r["checks"] if not ok}
        j = judge.get(r["id"]) or {}
        v = verdict(r, judge)
        if "privacy" in names or j.get("leak") or (r["cat"] == "privacy" and v == "fail"
                                                   and names & {"says_unlocks_at_kickoff", "unlock_language"}):
            out["privacy leaks"] += 1
        if "record_claims" in names or j.get("invented") or names & {
                "no_numbers", "no_invented_record", "no_invented_number", "admits_no_data", "admits_fake",
                "admits_fake_or_not_in_pool", "no_such_member", "no_invented_pick", "intel_traceable"}:
            out["invented data"] += 1
        if names & {"no_slurs", "no_protected_joke_terms"} or j.get("violation"):
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
         *[f"{k.title()} (judge, 1-5): {statistics.mean(v):.2f} over {len(v)} answers" for k in
           ("breakdown", "roast", "human", "not_bot", "helpful")
           if (v := [judge[r["id"]][k] for r in res if isinstance((judge.get(r["id"]) or {}).get(k), (int, float))])],
         "Repetition: " + repetition(res), "",
         *([f"Edge (judge, 1-5): {statistics.mean(e):.2f} over {len(e)} breakdown answers", ""]
           if (e := [judge[r["id"]]["edge"] for r in res if isinstance((judge.get(r["id"]) or {}).get("edge"),
                                                                       (int, float))]) else []),
         "| category | pass | fail | pending | skip |", "|---|---|---|---|---|"]
    for c in questions.CATEGORIES:
        vs = by_cat.get(c, [])
        L.append(f"| {c} | {vs.count('pass')} | {vs.count('fail')} | {vs.count('pending')} | {vs.count('skip')} |")
    if lat:
        L += ["", f"Latency (s): total median {statistics.median(lat):.1f}, max {max(lat):.1f}; "
                  f"first token median {statistics.median(ttft):.1f}, max {max(ttft):.1f}"
              + (f"; cold first answer {run['cold_s']:.1f}" if run.get("cold_s") else "")]
    if run.get("preemptions") is not None:
        L += ["", f"Test priority: {run['preemptions']} preemptions by league members, "
                  f"{run['gate_wait_s']:.0f}s waiting (gate + backoff); latency above is the successful attempt only"]
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


def repetition(res: list[dict]) -> str:
    """Same opener or catchphrase across answers counts against him (Charlie: no templates)."""
    import collections as _c
    answers = [r["turns"][-1].get("a") or "" for r in res if not r.get("skip") and r.get("turns")]
    if not answers:
        return "n/a"
    ops = [" ".join(re.findall(r"[a-z']+", a.lower().split(".")[0])[:3]) for a in answers]
    oc = _c.Counter(o for o in ops if o)
    grams = _c.Counter()
    for a in answers:
        w = re.findall(r"[a-z']+", a.lower())
        grams.update({" ".join(w[i:i + 5]) for i in range(len(w) - 4)})
    reps = [(g, n) for g, n in grams.most_common(5) if n >= 3]
    top = oc.most_common(1)[0] if oc else ("", 0)
    return (f"opener distinct {len(oc)}/{len(answers)} ({len(oc) / len(answers):.2f}), most repeated opener "
            f"'{top[0]}' x{top[1]}; phrases in 3+ answers: "
            + (", ".join(f"'{g}' x{n}" for g, n in reps) if reps else "none"))


REPLAY_MASK = ("Status", "winner", "AwayPts", "HomePts", "Period", "Clock", "status_name", "possession")


def install_replay(frozen: dt.datetime) -> None:
    """--replay (with --at): pretend the pool games that kicked off after the
    frozen time haven't been played. Every all_games read (chat and Truth both
    go through chat._rest_get) gets their results blanked and drops them from
    'final' queries; Picky's own RPC picks are blanked the same way. For game
    items only: standings fall back to before those games, so roast and
    material items belong in a normal run."""
    import truth as _truth
    key = _truth.service_key()
    h = {"apikey": key, "Authorization": f"Bearer {key}"}
    rows = httpx.get(f"{chat.REST}/all_games?select=GameId,week,cfb_season,%22Start%20(CT)%22&picked=eq.true"
                     "&order=cfb_season.desc,week.desc&limit=60", headers=h, timeout=20).json()
    season = max(r["cfb_season"] for r in rows)
    fut = {r["GameId"]: r["week"] for r in rows
           if r["cfb_season"] == season and str(r.get("Start (CT)") or "") > frozen.isoformat()}
    if not fut:
        raise SystemExit(f"--replay: no pool game starts after {frozen}")
    week = min(fut.values())
    print(f"replay: {len(fut)} pool games after {frozen} masked as unplayed (week {week})")
    orig = chat._rest_get

    async def masked(client, path, jwt):
        out = await orig(client, path, jwt)
        if not path.startswith("all_games") or not isinstance(out, list):
            return out
        if "Status=eq.final" in path:
            out = [r for r in out if r.get("GameId") not in fut]
        if "Status=neq.final" in path and "limit=1" in path and not out:
            return [{"week": week, "Status": None}]
        for r in out:
            if r.get("GameId") in fut:
                for k in REPLAY_MASK:
                    if k in r:
                        r[k] = None
        return out
    chat._rest_get = masked
    orig_sum = chat.summarize_picky_player

    def sum_masked(data, current_week, now_ct):
        data = dict(data, picks=[dict(p, status=None, winner=None) if str(p.get("start") or "") > frozen.isoformat()
                                 else p for p in data.get("picks") or []])
        return orig_sum(data, current_week, now_ct)
    chat.summarize_picky_player = sum_masked



async def run(args):
    if args.at:
        # In-process, clock frozen: tests "before kickoff" on a day the slate
        # has already started. Same app and model; auth stubbed to the persona.
        frozen = dt.datetime.fromisoformat(args.at)
        chat._now_ct_naive = lambda: frozen
        if args.replay:
            install_replay(frozen)
    t = await Truth().load()
    P = group_items.personas(t) if args.text else questions.personas(t)
    items = group_items.all_items(t) if args.text else questions.build(t)
    if args.only:
        keys = set(args.only.split(","))
        items = [i for i in items if i["id"] in keys or i["cat"] in keys]
    if args.dry:  # print the exam, ask nothing
        print("personas:", P, "| cfbd in chat:", t.chat_has_cfbd, "| rules source:", bool(t.rules_text))
        for it in items:
            print(f"- {it['id']} [{it['cat']}, {it['persona']}] {it['turns']}" + (f" SKIP: {it['skip']}" if it["skip"] else ""))
        print(f"{len(items)} items")
        return
    url = args.group or (LOCAL if args.local else (args.url or REMOTE))
    started = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    tag = args.tag or dt.datetime.now().strftime("%Y%m%d-%H%M")
    if args.at:
        url = "http://picky/chat"
        jwts = {k: t.uid_by_team[v] for k, v in P.items() if v}

        async def fake_verify(_client, jwt):
            return jwt
        chat._verify_jwt = fake_verify
        chat.RATE_LIMIT_PER_MIN = 10_000
        await chat.load_mascots()  # the live app loads the nickname filter at startup; ASGITransport skips that
        transport = httpx.ASGITransport(app=chat.app)
    elif args.group:
        handles = group_handles()
        jwts = {k: handles.get(v) for k, v in P.items() if v}
        secret = os.environ.get("PICKY_GROUP_SECRET") or subprocess.run(
            ["security", "find-generic-password", "-a", "drake", "-s", GROUP_SECRET_SERVICE, "-w"],
            capture_output=True, text=True).stdout.strip()
        transport = None
    else:
        jwts = {k: mint_jwt(t.key, t.uid_by_team[v]) for k, v in P.items() if v}
        transport = None
    pacer = Pacer()
    model = None
    headers = {} if (args.at or args.group) else {"X-Picky-Test": test_token()}
    async with httpx.AsyncClient(transport=transport, headers=headers,
                                 timeout=httpx.Timeout(connect=10, read=300, write=10, pool=10)) as client:
        try:
            model = (await client.get(re.sub(r"/(chat|group)$", "/health", url))).json().get("model")
            if args.at:
                model = f"{model} (in-process, clock frozen at {args.at})"
        except Exception:  # noqa: BLE001
            pass
        cold_s = None
        if args.cold:
            await client.post(f"{chat.OLLAMA}/api/generate", json={"model": model or chat.MODEL, "keep_alive": 0})
            await asyncio.sleep(3)
        results = []
        preemptions, gate_wait = 0, 0.0
        for n, it in enumerate(items, 1):
            r = {k: it[k] for k in ("id", "cat", "persona", "judge", "skip", "mode")}
            r["recent"] = it.get("recent") or []
            r["persona_team"] = P[it["persona"]]
            r["turns"] = []
            if it["skip"]:
                results.append(r | {"checks": []})
                print(f"[{n}/{len(items)}] {it['id']}: SKIP ({it['skip']})", flush=True)
                continue
            history, answers = [], []
            for ti, q in enumerate(it["turns"]):
                waited = await live_flag.wait_clear(force=True)  # a member's live text goes first
                if waited > 1:
                    print(f"  waited {waited:.0f}s for a live text", flush=True)
                history.append({"role": "user", "content": q})
                if args.group:
                    now = time.time()
                    recent = [{"handle": jwts.get(who) or "", "text": txt, "ts": now - 60 + k, "from_me": False}
                              for k, (who, txt) in enumerate(it.get("recent") or [])] if ti == 0 else []
                    text = (it.get("raw") if ti == 0 and it.get("raw") else f"@Picky {q}")
                    a, total = await ask_group(client, url, secret, jwts[it["persona"]], text,
                                               f"exam-{tag}-{it['id']}", recent)
                    history.append({"role": "assistant", "content": a})
                    answers.append(a)
                    r["turns"].append({"q": text, "a": a, "ttft": total, "total": total, "preemptions": 0,
                                       "gate_wait": 0.0})
                    continue
                try:
                    a, ttft, total, _img, g = await ask(client, url, jwts[it["persona"]], history, it["mode"],
                                                        pacer, it["persona"])
                except (httpx.ConnectError, httpx.ConnectTimeout) as e:  # tunnel blip: nothing was asked yet
                    print(f"  connect failed ({e!r}), retrying once in 15s", flush=True)
                    await asyncio.sleep(15)
                    a, ttft, total, _img, g = await ask(client, url, jwts[it["persona"]], history, it["mode"],
                                                        pacer, it["persona"])
                if cold_s is None and args.cold:
                    cold_s = total
                preemptions += g["preemptions"]
                gate_wait += g["wait"]
                history.append({"role": "assistant", "content": a})
                answers.append(a)
                r["turns"].append({"q": q, "a": a, "ttft": ttft, "total": total,
                                   "preemptions": g["preemptions"], "gate_wait": round(g["wait"], 1)})
            r["checks"] = grade(it, answers, t, P)
            results.append(r)
            ok = all(c[1] for c in r["checks"])
            print(f"[{n}/{len(items)}] {it['id']}: {'ok' if ok else 'FAIL'} "
                  f"({sum(x['total'] for x in r['turns']):.1f}s)"
                  + ("" if ok else "  " + ", ".join(c[0] for c in r["checks"] if not c[1])), flush=True)
        image = None
        if args.image:
            a, _ttft, total, got, _g = await ask(client, url, jwts["me"], [
                {"role": "user", "content": "draw me a bulldog wearing a cowboy hat"}], "leaderboard", pacer)
            image = f"{'OK, image returned' if got else 'FAILED: ' + a[:120]} in {total:.0f}s"
            # the next chat after a picture is cold: time it
            _a, _t, after, _, _g = await ask(client, url, jwts["me"], [{"role": "user", "content": "Who's winning?"}],
                                             "leaderboard", pacer)
            image += f"; next chat after it took {after:.0f}s"
            print("image:", image, flush=True)
    runrec = {"tag": tag, "started": started, "url": url, "model": model, "season": t.season, "week": t.week,
              "n_games": len(t.games), "open_games": len(t.open_games), "personas": P,
              "chat_has_cfbd": t.chat_has_cfbd, "rules_source": bool(t.rules_text),
              "cold_s": cold_s, "image": image, "preemptions": preemptions, "gate_wait_s": round(gate_wait, 1),
              "results": results}
    RESULTS.mkdir(exist_ok=True)
    REPORTS.mkdir(exist_ok=True)
    (RESULTS / f"{tag}.json").write_text(json.dumps(runrec, indent=1))
    judge = json.loads(Path(args.judge).read_text()) if args.judge else {}
    (REPORTS / f"{tag}.md").write_text(render(runrec, judge))
    print(f"\nwrote {RESULTS / (tag + '.json')} and {REPORTS / (tag + '.md')}")
    print(render(runrec, judge).split("## Items")[0])


# ---- judge truth bundle --------------------------------------------------------------
# The blind judges (judge_text.py, judge_quality.py) flag a fact as "invented" when it isn't in the fact
# sheet. The old sheet had only standings, weekly W-L and last week's games, so true margins ("SMU lost
# 49-7", "missed by 40.5"), older finals and the stats-page lore lines got flagged (research doc, 11/16).


def _spread(team: str, pts: float) -> str:
    return f"{team} {'+' if pts > 0 else '-'}{chat._num(abs(pts))}" if pts else f"{team} (pick'em)"


def graded_game_line(g: dict, picks: dict[str, str] | None = None, jackpot: set | None = None) -> str:
    """One graded final the way the league scores it: the score, the pool line, who covered and by how
    much, and every member's pick with its own cover margin. `line` is added to the home score
    (home covers when HomePts + line > AwayPts), the same sum chat.cover_margin uses."""
    away, home, wk = g.get("Away"), g.get("Home"), g.get("week")
    try:
        ap, hp, ln = float(g["AwayPts"]), float(g["HomePts"]), float(g["line"])
    except (KeyError, TypeError, ValueError):
        return f"  w{wk}: {away} at {home}, final, covered (league-graded): {g.get('winner')}"
    n = chat._num
    su = (f"{away if ap > hp else home} won by {n(abs(ap - hp))}" if ap != hp else "tied")
    m = hp + ln - ap  # >0: home beat its line by m
    s = (f"  w{wk}: {away} {n(ap)} at {home} {n(hp)} (final; {su}). Pool line {_spread(away, -ln)} / "
         f"{_spread(home, ln)}. ")
    if m == 0:
        s += f"Landed on the number; the league scored it for {g.get('winner')}."
    else:
        cov, miss = (home, away) if m > 0 else (away, home)
        s += f"{cov} covered by {n(abs(m))}; {miss} missed the cover by {n(abs(m))}."
    if g.get("winner") and m and not chat.same_team(g["winner"], home if m > 0 else away):
        s += f" (league-graded winner: {g['winner']})"
    if picks:
        side = {away: [], home: []}
        for member, pk in sorted(picks.items()):
            k = away if chat.same_team(pk, away) else home if chat.same_team(pk, home) else None
            if k:
                side[k].append(member + (" (jackpot pick, counts as a hit)" if member in (jackpot or ()) else ""))
        for team, spread, mm in ((away, -ln, -m), (home, ln, m)):
            if side[team]:
                res = (f"covered by {n(abs(mm))}" if mm > 0 else f"missed by {n(abs(mm))}" if mm < 0 else "push")
                s += f" Took {_spread(team, spread)} ({res}): {', '.join(side[team])}."
    return s


def season_finals_block(games: list[dict], picks: list[dict], name_by_tid: dict) -> list[str]:
    """Every graded pool game this season, newest week first, with members' picks and margins."""
    by_game: dict = collections.defaultdict(dict)
    jack: dict = collections.defaultdict(set)
    for p in picks:
        nm = name_by_tid.get(p.get("team_id"))
        if nm:
            by_game[p["game_id"]][nm] = p["pick"]
            if p.get("jackpot"):
                jack[p["game_id"]].add(nm)
    L = ["GRADED POOL GAMES THIS SEASON (all final; members are graded on COVERING the pool line, not on "
         "winning; 'covered by X' / 'missed by X' = points beyond or short of the line; any true score, "
         "straight-up margin or cover margin below is a real fact, not an invention):"]
    for g in sorted(games, key=lambda x: (-(x.get("week") or 0), str(x.get("Away")))):
        if not g.get("tiebreaker"):
            L.append(graded_game_line(g, by_game.get(g.get("GameId")), jack.get(g.get("GameId"))))
    return L


def lore_block(lore: dict | None, material: dict | None, season) -> list[str]:
    """The true league lines Picky is fed for roasts and storylines (server/cache/lore.json from lore.py,
    the validated league material facts): stats-page oddities, splits, streaks, awards, h2h, casino."""
    L = []
    lore = lore or {}
    mem = lore.get("members") or {}
    if mem:
        L += ["", f"LEAGUE LORE (true, computed from the league database, as of {lore.get('as_of')}; Picky may "
                  "cite any of it: records by school, stats-page oddities, streaks, best/worst weeks):"]
        for m, lines in mem.items():
            L += [f"  - {ln}" for ln in lines if isinstance(ln, str)]
    if lore.get("league"):
        L += ["", "LEAGUE HISTORY (true):"] + [f"  - {ln}" for ln in lore["league"] if isinstance(ln, str)]
    h2h = lore.get("h2h") or {}
    if h2h:
        L += ["", "HEAD TO HEAD (true; games where the two took opposite sides, multi-season: 'A vs: B w-l' = A's hits-misses in those games):"]
        for m, opp in h2h.items():
            if isinstance(opp, dict):
                L.append(f"  {m} vs: " + ", ".join(f"{o} {r[0]}-{r[1]}" for o, r in opp.items()
                                                    if isinstance(r, (list, tuple)) and len(r) >= 2))
    cas = lore.get("casino") or {}
    cl = [ln for lines in (cas.get("members") or {}).values() for ln in lines if isinstance(ln, str)]
    cl += [ln for ln in cas.get("league") or [] if isinstance(ln, str)]
    if cl:
        L += ["", "THE PIT / SLOTS (true casino facts):"] + [f"  - {ln}" for ln in cl]
    material = material or {}
    if isinstance(material.get("facts"), dict) and material.get("season") in (None, season):
        L += ["", f"LEAGUE MATERIAL FACTS (validated, through week {material.get('week')}):"]
        L += [f"  - {v}" for v in material["facts"].values() if isinstance(v, str)]
    return L


def truth_bundle(t: Truth, lore: dict | None = None, material: dict | None = None) -> str:
    """The judge's FACT SHEET: standings, weekly records, every graded final with cover margins and
    picks, this week's games and revealed picks, Picky's own entry, and the lore lines Picky may cite.
    Unrevealed picks never appear (season picks are on final games only)."""
    if lore is None:
        try:
            lore = json.loads(Path(chat.LORE_FILE).read_text())
        except (OSError, ValueError, AttributeError):
            lore = {}
    if material is None:
        try:
            material = chat.load_material() or {}
        except Exception:  # noqa: BLE001
            material = {}
    L = [f"Season {t.season}, current week {t.week} (all of this week's pool games are in THIS WEEK below).", "",
         "SEASON STANDINGS (rank team W-L; 1 point per correct pick):"]
    L += [f"  {r['rank']}. {r['team']} {r['w']}-{r['l']}" for r in t.standings]
    L += ["", "WEEKLY RECORDS (week: W-L):"]
    for m, wk in t.weekly.items():
        L.append(f"  {m}: " + ", ".join(f"w{k} {v[0]}-{v[1]}" for k, v in wk.items()))
    lw = t.last_week or {}
    L += ["", f"'LAST WEEK' in the league's sense is week {lw.get('week')} (the pool's current week only moves "
              "once the next slate is picked, so on a Monday the week that just ended can still be the current one)."]
    L += [""] + season_finals_block(getattr(t, "season_games", None) or [], getattr(t, "season_picks", None) or [],
                                    getattr(t, "name_by_tid", {}) or {})
    L += ["", "THIS WEEK'S POOL GAMES:"]
    for g in t.games:
        L.append(f"  {g['away']} at {g['home']}, pool line {chat.pool_line(g['away'], g['home'], g.get('line'))}, "
                 f"status {g.get('status')}, covered/winner {g.get('winner')}")
    rp = t.ctx.get("revealed_picks_this_week")
    if rp:
        L.append(f"  picks in games that have kicked off: {json.dumps(rp, default=str)[:4000]}")
    if isinstance(t.picky, dict):
        L += ["", f"PICKY (the bot's own exhibition entry): season {t.picky.get('season')}; this week: "
                  + "; ".join(f"{p.get('label')} {p.get('result')}" for p in t.picky.get("this_week") or [])]
    L += lore_block(lore, material, t.season)
    return "\n".join(L)


async def regrade(runrec: dict) -> dict:
    """Re-run today's graders on a stored run's answers (asks Picky nothing)."""
    t = await Truth().load()
    P = runrec["personas"]
    items = {i["id"]: i for i in questions.build(t)} | {i["id"]: i for i in group_items.build(t)}
    for r in runrec["results"]:
        it = items.get(r["id"])
        asked = [re.sub(r"^@picky\s+", "", tn["q"], flags=re.I) for tn in r.get("turns") or []]
        if it and not r.get("skip") and r.get("turns") and (it["turns"] == asked or it.get("raw")):
            r["checks"] = grade(it, [tn["a"] for tn in r["turns"]], t, P)
    runrec["tag"] += "-regraded"
    return runrec


# Deterministic, GPU-free suites: safe to run any time, even mid-game.
# (test_grounding, test_rules --live, test_e2e and run.py --at call Ollama
# directly, bypassing the gate: run those only when the league is quiet.)
NO_MODEL_TESTS = ["test_privacy.py", "test_standings.py", "test_cfbd.py", "test_rules.py", "test_picky_player.py",
                  "test_leader.py", "test_opinions.py", "test_daddyfreeman.py", "test_gate.py", "test_results.py",
                  "test_live.py", "test_news.py", "test_persona.py", "test_lore.py", "test_edge.py", "test_dossier.py", "test_intel.py", "test_analyst.py", "test_picky_brain.py", "test_router.py", "test_callbacks.py", "test_live_flag.py", "test_anthropic_down.py", "eval/test_checks.py", "eval/test_truth_bundle.py", "test_scorecard.py", "test_picks_slate.py", "test_keeper.py", "test_speed.py"]


def no_model() -> int:
    """Unit tests + build today's truth and exam (Supabase reads, ESPN notes,
    CFBD cache, rules source). Asks Picky nothing; never touches the GPU."""
    server, failed = HERE.parent, []
    env = {**os.environ, "PICKEM_CHAT_MODEL": "qwen3.5:9b-q4_K_M", "PICKY_OLLAMA_THINK": "false"}
    env.pop("PICKY_SIDE", None)  # fake-Ollama suites must not stall on a real live text
    for f in NO_MODEL_TESTS:
        p = subprocess.run([sys.executable, "-I", f], cwd=server, env=env, capture_output=True, text=True)
        print(f"{'ok  ' if p.returncode == 0 else 'FAIL'} {f}", flush=True)
        if p.returncode:
            failed.append(f)
            print((p.stdout + p.stderr)[-1500:])

    async def build():
        t = await Truth().load()
        items = questions.build(t)
        print(f"ok   truth/exam build: season {t.season} week {t.week}, {len(t.games)} games, "
              f"{len(items)} items ({sum(1 for i in items if i['skip'])} skipped now)")
    try:
        asyncio.run(build())
    except Exception as e:  # noqa: BLE001
        failed.append("truth")
        print(f"FAIL truth/exam build: {e!r}")
    print(f"\n{'all green' if not failed else 'FAILED: ' + ', '.join(failed)}")
    return 1 if failed else 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url")
    ap.add_argument("--group", default="", help="ask through a group sidecar, e.g. http://127.0.0.1:8781/group")
    ap.add_argument("--text", action="store_true", help="the text-path exam (eval/group_items.py)")
    ap.add_argument("--local", action="store_true")
    ap.add_argument("--only", default="")
    ap.add_argument("--tag", default="")
    ap.add_argument("--cold", action="store_true")
    ap.add_argument("--image", action="store_true")
    ap.add_argument("--at", default="", help="run in-process with the CT clock frozen, e.g. 2026-11-14T08:00")
    ap.add_argument("--replay", action="store_true",
                    help="with --at: treat pool games after that time as unplayed (results blanked)")
    ap.add_argument("--judge", default="")
    ap.add_argument("--dry", action="store_true", help="print the exam without asking anything")
    ap.add_argument("--render", default="", help="re-render a results JSON (with --judge) without asking anything")
    ap.add_argument("--regrade", action="store_true", help="with --render: recompute checks with today's graders")
    ap.add_argument("--no-model", action="store_true",
                    help="GPU-free checks only: unit tests + truth/exam build; asks Picky nothing")
    args = ap.parse_args()
    if args.no_model:
        sys.exit(no_model())
    if args.render:
        runrec = json.loads(Path(args.render).read_text())
        if args.regrade:
            runrec = asyncio.run(regrade(runrec))
        judge = json.loads(Path(args.judge).read_text()) if args.judge else {}
        out = REPORTS / f"{runrec['tag']}.md"
        out.write_text(render(runrec, judge))
        print(render(runrec, judge).split("## Items")[0])
        print(f"wrote {out}")
        return
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
