"""
Nightly flub loop (LaunchAgent com.pickem.flub-nightly, 23:00 CT; plist in tools/flub/).

Picky's REAL exchanges from one CT day -> flubs -> regression items for the text exam -> an
optional Opus fix that ships only if nothing regresses -> a short private morning report.

OPTIONAL ADD-ON (zero maintenance, phase 8): the only scheduled job picky_claude lets call Claude.
Without it (subscription cancelled, claude binary missing, API errors, PICKY_NO_ANTHROPIC) the
nightly still harvests, classifies from code signals and writes the report; the Opus classify and
the fix round just skip. Nothing else reads its output to work (member_memory's log-parser import
is optional and wrapped).

  flub_loop.py harvest|classify|items|fix|report|nightly [--date YYYY-MM-DD] [--dry]

  harvest   the day's live rows from picky_exchanges.jsonl (team null = test handles and side/exam
            rows skipped; old rows without a live reply line in the sidecar's stderr log are skipped
            as side traffic), signals per reply parsed from logs/picky-group.stderr.log (sentence
            filter drops, fact-check drops, results guard, fact reply lost part, undodge, banter cut,
            prime failed, reply secs), member pushback (the same chat's next text within 10 min:
            "wrong", "huh", "that's not", ...) and the private flub queue (flag_flub.py, top
            priority; consumed here) -> flubs/harvest-<date>.json
  classify  ONE offline Opus pass (picky_claude; Claude unavailable or --dry = code-only signals):
            flub y/n, category, why, a proposed exam item, against the facts bundle (eval/run.py
            truth_bundle + checked CFB notes + this week's game cards) -> flubs/classify-<date>.json
  items     every proposed item verified in code (each number/name in must_include / any_of must be
            in the facts bundle; the flubbed reply must fail it; public items carry no member
            names, handles or records, else they go private) -> eval/flub_items.json (public, in
            the repo) or cfb-pickem-private/flubs/items-private.json; ids flub-YYYYMMDD-n, deduped
            by normalized question. eval/group_items.py loads both (private only if present).
  fix       optional (kill switch: cfb-pickem-private/FLUB_AUTOFIX_OFF or PICKY_FLUB_AUTOFIX=0):
            claude -p Opus edits a throwaway worktree (branch flub-fix-<date>), then gates: the
            model-free suite (no new failures) and the text exam on side sidecars inside the exam
            lock (fix :8805 vs main :8806): det. pass >= baseline, invented 0, leaks 0, flub items
            passing >= baseline. Then, outside the game-day freeze (Fri 18:00 - Sun 04:00 CT): rebase,
            push to main and ship through tools/release/release.sh (suite, tag, switch the pinned live
            tree, reload, canary, auto-rollback). Inside the freeze: the fix is pushed as branch
            flub/<date> and nothing reloads; the next run outside the freeze promotes pending flub/*
            branches through release.sh. Saturday 10:00-24:00 CT there's no fix round at all (no exams).
  report    flubs/report-<date>.md
  nightly   all of the above

--dry: everything except Opus calls, merges and service reloads (and the queue isn't consumed).
Never texts anyone. Everything with member data stays in cfb-pickem-private (0600).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import flag_flub  # noqa: E402  (light: queue path, chat hash)
import freeze  # noqa: E402  (game-day freeze + no-exam window)

CT = ZoneInfo("America/Chicago")
PRIVATE = Path(os.environ.get("PICKY_PRIVATE_DIR", "/Users/Agent/cfb-pickem-private"))
EXCHANGES = Path(os.environ.get("PICKY_EXCHANGE_LOG", PRIVATE / "picky_exchanges.jsonl"))
STDERR_LOG = Path(os.environ.get("PICKY_GROUP_STDERR", "/Users/Agent/cfb-pickem/server/logs/picky-group.stderr.log"))
FLUB_DIR = Path(os.environ.get("PICKY_FLUB_DIR", PRIVATE / "flubs"))
PUBLIC_ITEMS = Path(os.environ.get("PICKY_FLUB_ITEMS_PUBLIC", HERE / "eval" / "flub_items.json"))
PRIVATE_ITEMS = Path(os.environ.get("PICKY_FLUB_ITEMS_PRIVATE", FLUB_DIR / "items-private.json"))
KILL_FILE = Path(os.environ.get("PICKY_FLUB_KILL_FILE", PRIVATE / "FLUB_AUTOFIX_OFF"))
MEMBERS_FILE = Path(os.environ.get("PICKY_GROUP_MEMBERS", PRIVATE / "picky_group_members.json"))
REPO = Path(os.environ.get("PICKY_REPO", "/Users/Agent/cfb-pickem"))
PY = "/Users/Agent/cfb-pickem/server/.venv/bin/python3"
ALLTESTS = str(HERE.parent / "tools" / "release" / "alltests.sh")
RELEASE = str(HERE.parent / "tools" / "release" / "release.sh")
LOCK = "/tmp/picky-exam.lock"
FIX_PORT, BASE_PORT = 8805, 8806

SLOW_S = 60.0
PUSHBACK_WINDOW = 600.0
MAX_CANDIDATES = 80
CATEGORIES = ["wrong fact", "didn't answer", "dodge", "wrong sub-question", "repeat", "tone/robotic", "slow",
              "privacy"]
PUSHBACK = re.compile(
    r"^\s*(no|nope|nah|huh|what)\b[\s,.!?]|\bwrong\b|\bwtf\b|\bhuh\b|\bthat'?s not\b|\bthats not\b|\bnot true\b|"
    r"\bincorrect\b|\bbad (picky|bot)\b|\bmakes no sense\b|\bwhat are you talking about\b|\bnot even close\b|"
    r"\bthat'?s false\b|\bmaking (that|it|stuff|shit) up\b|\bdidn'?t (answer|ask)\b|\banswer the question\b|\?\?+",
    re.I)
LOG_LINE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ (\w+) (.*)$")
REPLY = re.compile(r"^(group|dm|dm-public) reply sender=\S+ mapped=\w+ ([\d.]+)s chars=(\d+) image=\w+"
                   r"(?: lane=(\S+))?")
SIGNALS = [("filter_drop", re.compile(r"^sentence filter dropped")),
           ("fact_check_drop", re.compile(r"^fact check .*dropped")),
           ("results_guard", re.compile(r"^results guard")),
           ("fact_lost", re.compile(r"^fact reply lost part of the answer")),
           ("undodge", re.compile(r"undodge", re.I)),
           ("banter_cut", re.compile(r"^banter line cut")),
           ("reroll", re.compile(r"reroll \d+")),
           ("prime_failed", re.compile(r"^prime failed")),
           ("brain_error", re.compile(r"^(understand|fact wrap) failed"))]


# ---------------------------------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------------------------------

def today_ct() -> str:
    return dt.datetime.now(CT).date().isoformat()


def day_bounds(date: str) -> tuple[float, float]:
    d = dt.date.fromisoformat(date)
    start = dt.datetime(d.year, d.month, d.day, tzinfo=CT)
    return start.timestamp(), (start + dt.timedelta(days=1)).timestamp()


def write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.replace(tmp, path)


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def norm_q(q: str) -> str:
    """Dedupe key for a question: lowercase, no @Picky, no punctuation, single spaces."""
    q = re.sub(r"@\s?picky\b", " ", re.sub(r"['\u2019]", "", q or ""), flags=re.I).lower()
    return " ".join(re.sub(r"[^\w\s]", " ", q).split())


def item_key(item: dict) -> str:
    return " | ".join(norm_q(t) for t in item.get("turns") or [])


def pct(xs: list[float], p: float) -> float:
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, max(0, int(round(p * (len(xs) - 1)))))], 1) if xs else 0.0


# ---------------------------------------------------------------------------------------------------
# exam item checks (eval/group_items.py loads flub items through these)
# ---------------------------------------------------------------------------------------------------

def _plain(s: str) -> str:
    return " ".join((s or "").replace("’", "'").replace("‘", "'").lower().split())


def item_results(item: dict, answer: str) -> list[tuple[str, bool, str]]:
    """(name, ok, detail) for one answer: replied, every must_include term, at least one any_of term,
    no must_not term. Terms are literal, case-insensitive."""
    x = _plain(answer)
    res = [("replied", bool(x), "")]
    for term in item.get("must_include") or []:
        res.append(("flub_must_include", _plain(term) in x, term))
    alts = item.get("any_of") or []
    if alts:
        res.append(("flub_any_of", any(_plain(a) in x for a in alts), " / ".join(alts)))
    for term in item.get("must_not") or []:
        res.append(("flub_must_not", _plain(term) not in x, term))
    return res


def item_check(item: dict):
    """A group_items check: fn(answers, truth, personas) -> results on the last answer."""
    return lambda a, t, p: item_results(item, (a[-1] if a else "") or "")


def load_items(today: str | None = None) -> list[dict]:
    """Public items, then private ones (only if that file exists), deduped by normalized question.
    An item past its "expires" date comes back with a "skip" reason."""
    today = today or today_ct()
    out, seen = [], set()
    for path in (PUBLIC_ITEMS, PRIVATE_ITEMS):
        rows = read_json(path, []) if path.exists() else []
        for r in rows if isinstance(rows, list) else []:
            if not isinstance(r, dict) or not r.get("id") or not r.get("turns") or item_key(r) in seen:
                continue
            seen.add(item_key(r))
            r = dict(r)
            if r.get("expires") and str(r["expires"]) < today:
                r["skip"] = f"expired {r['expires']}"
            out.append(r)
    return out


# ---------------------------------------------------------------------------------------------------
# harvest
# ---------------------------------------------------------------------------------------------------

def parse_log(path: Path, start: float, end: float) -> tuple[list[dict], bool]:
    """Reply lines of the day, each with the signal lines inside its window [end - secs, end].
    Returns (replies, log_found)."""
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return [], False
    entries = []
    for ln in lines:
        m = LOG_LINE.match(ln)
        if not m:
            continue
        ts = dt.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=CT).timestamp()
        if start - 900 <= ts < end + 900:
            entries.append((ts, m.group(3)))
    replies = []
    for ts, msg in entries:
        m = REPLY.match(msg)
        if m and start <= ts < end:
            replies.append({"ts": ts, "mode": "dm" if m.group(1).startswith("dm") else "group",
                            "secs": float(m.group(2)), "chars": int(m.group(3)), "lane": m.group(4) or "",
                            "signals": []})
    for r in replies:
        lo = r["ts"] - r["secs"] - 1.0
        for ts, msg in entries:
            if lo <= ts <= r["ts"]:
                for name, rx in SIGNALS:
                    if rx.search(msg):
                        r["signals"].append({"type": name, "text": msg[:240]})
                        break
    return replies, True


def _match(rows: list[dict], replies: list[dict]) -> None:
    """Attach each exchange row to its reply line (same mode, closest time within 20 s)."""
    used = set()
    for r in rows:
        best = None
        for i, rp in enumerate(replies):
            if i in used or rp["mode"] != r["mode"]:
                continue
            d = abs(rp["ts"] - r["ts"])
            if r.get("secs") is not None:
                d += abs(rp["secs"] - float(r["secs"])) / 10
            if d <= 20 and (best is None or d < best[0]):
                best = (d, i)
        if best:
            used.add(best[1])
            r["_reply"] = replies[best[1]]


def _same_chat(a: dict, b: dict) -> bool:
    """The same member in the same chat (the group's chat id is shared by everyone in it)."""
    if a.get("chat") and b.get("chat") and a["chat"] != b["chat"]:
        return False
    return a.get("mode") == b.get("mode") and a.get("team") == b.get("team")


def _queue_target(rec: dict, rows: list[dict]) -> tuple[dict | None, dict | None]:
    """(the day's row the flag points at or None, the context entry it came from or None)."""
    ctx = [c for c in rec.get("context") or [] if isinstance(c, dict)]
    text = _plain(rec.get("text") or "")
    pick = None
    if text and not flag_flub.is_flag(text):
        pick = next((c for c in reversed(ctx) if text in _plain(c.get("a")) or text in _plain(c.get("q"))
                     or (_plain(c.get("a")) and _plain(c.get("a")) in text)), None)
        if pick is None:
            row = next((r for r in reversed(rows) if text in _plain(r.get("a")) or
                        (_plain(r.get("a")) and _plain(r.get("a")) in text)), None)
            if row:
                return row, None
    pick = pick or (ctx[-1] if ctx else None)
    if not pick:
        return None, None
    row = next((r for r in reversed(rows) if _plain(r.get("q")) == _plain(pick.get("q"))
                and _plain(r.get("a")) == _plain(pick.get("a"))), None)
    return row, pick


def harvest(date: str, dry: bool = False) -> dict:
    start, end = day_bounds(date)
    raw = []
    try:
        for ln in EXCHANGES.read_text().splitlines():
            try:
                r = json.loads(ln)
            except ValueError:
                continue
            if isinstance(r, dict) and start <= float(r.get("ts") or 0) < end:
                raw.append(r)
    except OSError:
        pass
    replies, log_found = parse_log(STDERR_LOG, start, end)
    skipped = {"test_handle": 0, "side": 0, "unmatched": 0}
    rows = []
    for r in raw:
        if not r.get("team"):
            skipped["test_handle"] += 1
        elif r.get("side"):
            skipped["side"] += 1
        else:
            rows.append(dict(r))
    rows.sort(key=lambda r: r["ts"])
    _match(rows, replies)
    if log_found:
        # rows from before the chat/side fields existed, with no reply line in the LIVE sidecar's log,
        # came from side sidecars that wrote into the live exchange log: not real member traffic
        kept = [r for r in rows if r.get("_reply") or r.get("chat")]
        skipped["unmatched"] = len(rows) - len(kept)
        rows = kept
    for i, r in enumerate(rows):
        rp = r.pop("_reply", None)
        r["signals"] = list(rp["signals"]) if rp else []
        r["secs"] = float(r["secs"]) if r.get("secs") is not None else (rp["secs"] if rp else None)
        if r["secs"] is not None and r["secs"] > SLOW_S:
            r["signals"].append({"type": "slow", "text": f"{r['secs']:.0f}s"})
        nxt = next((n for n in rows[i + 1:] if _same_chat(r, n)), None)
        if nxt and nxt["ts"] - r["ts"] <= PUSHBACK_WINDOW and PUSHBACK.search(nxt.get("q") or ""):
            r["signals"].append({"type": "pushback", "text": (nxt.get("q") or "")[:200]})
        elif nxt and nxt["ts"] - r["ts"] <= PUSHBACK_WINDOW and norm_q(nxt.get("q")) == norm_q(r.get("q")):
            r["signals"].append({"type": "reasked", "text": (nxt.get("q") or "")[:200]})  # asked again: not answered
    # the flag queue: top priority
    qpath = flag_flub.queue_path()
    queue = []
    try:
        queue = [json.loads(ln) for ln in qpath.read_text().splitlines() if ln.strip()]
    except (OSError, ValueError):
        pass
    cands, taken = [], set()
    for rec in queue:
        if rec.get("consumed") or float(rec.get("ts") or 0) >= end:
            continue
        row, ctx = _queue_target(rec, rows)
        base = row or ctx or {}
        c = {k: base.get(k) for k in ("ts", "mode", "team", "q", "a", "lane", "secs")}
        c.update({"priority": "queue", "queue_id": rec.get("id"), "flag_source": rec.get("source"),
                  "flag_text": rec.get("text") or "", "note": rec.get("note") or "", "image": rec.get("image"),
                  "signals": (row or {}).get("signals") or [{"type": "flagged", "text": rec.get("text") or ""}]})
        if row:
            taken.add(id(row))
        cands.append(c)
    order = {"pushback": 1, "reasked": 1, "fact_lost": 2, "results_guard": 2, "fact_check_drop": 2, "undodge": 2, "slow": 3}
    rest = [r for r in rows if id(r) not in taken]
    rest.sort(key=lambda r: (min([order.get(s["type"], 4) for s in r["signals"]] or [9]), r["ts"]))
    for r in rest:
        c = {k: r.get(k) for k in ("ts", "mode", "team", "q", "a", "lane", "secs", "signals")}
        c["priority"] = "signal" if r["signals"] else "other"
        cands.append(c)
    cands = cands[:MAX_CANDIDATES]
    for n, c in enumerate(cands, 1):
        c["cid"] = f"c{n}"
    secs = [r["secs"] for r in rows if r.get("secs") is not None]
    sig_counts: dict[str, int] = {}
    for r in rows:
        for s in r["signals"]:
            sig_counts[s["type"]] = sig_counts.get(s["type"], 0) + 1
    out = {"date": date, "dry": dry, "log_found": log_found,
           "seen": len(rows), "by_mode": {m: sum(r["mode"] == m for r in rows) for m in ("dm", "group")},
           "skipped": skipped, "signals": sig_counts,
           "latency": {"n": len(secs), "p50": pct(secs, .5), "p95": pct(secs, .95), "max": max(secs, default=0.0),
                       "slow": sum(s > SLOW_S for s in secs)},
           "queue_ids": [c["queue_id"] for c in cands if c.get("queue_id")], "candidates": cands}
    write_private(FLUB_DIR / f"harvest-{date}.json", json.dumps(out, indent=1))
    if out["queue_ids"] and not dry:
        mark_consumed(set(out["queue_ids"]), date)
    return out


def mark_consumed(ids: set, date: str) -> None:
    def mark(recs):
        for r in recs:
            if r.get("id") in ids and not r.get("consumed"):
                r["consumed"] = date
        return recs
    flag_flub.rewrite_queue(mark)


# ---------------------------------------------------------------------------------------------------
# facts bundle (the same truth the text exam grades against)
# ---------------------------------------------------------------------------------------------------

def build_facts() -> dict:
    """{"facts": text, "personas": {key: team}, "names": [member team + real names]} from Supabase,
    the CFBD cache and the game cards. Raises when the truth can't load."""
    import asyncio
    sys.path.insert(0, str(HERE / "eval"))
    import game_cards as gc  # noqa: E402
    import group_items  # noqa: E402
    import judge_text  # noqa: E402
    import run as exam  # noqa: E402
    from truth import Truth  # noqa: E402
    t = asyncio.run(Truth().load())
    L = [exam.truth_bundle(t)] + judge_text._cfb_notes()
    d = gc.CARD_DIR / str(t.week)
    for f in sorted(d.glob("*.json")) if d.exists() else []:
        c = gc.load_card(t.week, int(f.stem)) if f.stem.isdigit() else None
        if c:
            L += [""] + gc.card_lines(c, None, True)
    names = set(t.members)
    for m in (read_json(MEMBERS_FILE, {}) or {}).get("members") or []:
        names.update(x for x in (m.get("team"), m.get("name")) if x)
        names.update(x for x in str(m.get("name") or "").split() if len(x) >= 3)
    return {"facts": "\n".join(L), "personas": group_items.personas(t), "names": sorted(names)}


# ---------------------------------------------------------------------------------------------------
# classify (one Opus pass) and code-only signals
# ---------------------------------------------------------------------------------------------------

ITEM_SCHEMA = {"type": "object", "properties": {
    "turns": {"type": "array", "items": {"type": "string"}},
    "persona": {"type": "string"},
    "must_include": {"type": "array", "items": {"type": "string"}},
    "any_of": {"type": "array", "items": {"type": "string"}},
    "must_not": {"type": "array", "items": {"type": "string"}},
    "judge": {"type": "string"},
    "private": {"type": "boolean"},
    "expires": {"type": "string"}},
    "required": ["turns", "private"]}
SCHEMA = {"type": "object", "properties": {"verdicts": {"type": "array", "items": {
    "type": "object", "properties": {
        "cid": {"type": "string"}, "flub": {"type": "boolean"},
        "category": {"type": "string", "enum": CATEGORIES + [""]},
        "why": {"type": "string"}, "item": ITEM_SCHEMA},
    "required": ["cid", "flub", "category", "why"]}}}, "required": ["verdicts"]}

PROMPT = """You review one day of real texts between members of a college-football pick'em league and "Picky", the league's trash-talking bot (a small local model behind code guards). Find Picky's FLUBS and turn each fixable one into a regression exam item.

A flub is one of: wrong fact (contradicts the FACT SHEET), didn't answer, dodge (deflected a real question), wrong sub-question (answered a different part than asked), repeat (same line/angle as before), tone/robotic (report-like, template, bot tells), slow (reply > 60 s), privacy (revealed a pick before kickoff, or member data in the wrong place). Wit and opinions are not flubs. Candidates marked "queue" were flagged by a member or the commissioner as bad: look hard at those (flag_text / note say what was wrong). "signals" are what the code guards saw (sentence-filter drops, fact-check drops, results guard, a fact reply that lost part of its answer, slow replies, a member's pushback in their next text); a signal alone is not a flub.

For each candidate return {{cid, flub, category ("" if no flub), why (one short sentence)}} and, for a flub a better reply could fix (never for "slow"), an item:
- turns: the member's text(s) as they'd send them (no "@Picky"); make it generic when the question doesn't need a person.
- must_include: 0-3 short literal strings (case-insensitive) a correct reply MUST contain, each number/name copied EXACTLY from the FACT SHEET (code drops the item if any is missing there). any_of: alternatives where at least one must appear. must_not: short literal strings the flubbed reply had and a good one never would (the wrong number, the dodge phrase, the robotic stock line). At least one key is required, and the flubbed reply must fail the item.
- Prefer keys that stay true (rules, final scores, past weeks); for this-week facts set expires (YYYY-MM-DD, the day the fact goes stale, e.g. the next Saturday).
- private: true if the item depends on a member's own data (their record, picks, name, standing) or names any member; then persona is the asker's key from PERSONAS. private: false items must name no member and ask as a generic member (persona "mid").
- judge: a one-sentence rubric (pass if ..., fail if ...).

PERSONAS (exam persona key -> member team): {personas}

FACT SHEET:
{facts}

CANDIDATES (JSON):
{cands}

Return JSON only: {{"verdicts": [...]}}, one per candidate."""


def code_verdicts(h: dict) -> list[dict]:
    """No Anthropic: flubs only where the code is sure (a member/commissioner flag, pushback, a fact
    reply the guards had to replace, a slow reply); everything else is just a signal."""
    out = []
    for c in h["candidates"]:
        types = {s["type"] for s in c.get("signals") or []}
        if c.get("priority") == "queue":
            v = (True, "", "flagged: " + (c.get("flag_text") or c.get("note") or "bad picky")[:120])
        elif "pushback" in types:
            v = (True, "", "member pushback: " + next(s["text"] for s in c["signals"] if s["type"] == "pushback")[:120])
        elif "reasked" in types:
            v = (True, "didn't answer", "the member asked the same thing again")
        elif types & {"fact_lost", "results_guard", "fact_check_drop"}:
            v = (True, "wrong fact", "a guard caught a wrong fact: " + ", ".join(sorted(types)))
        elif "slow" in types:
            v = (True, "slow", f"{c.get('secs') or 0:.0f}s reply")
        else:
            continue
        out.append({"cid": c["cid"], "flub": v[0], "category": v[1], "why": v[2], "by": "code"})
    return out


def opus_classify(prompt: str) -> dict:
    """structured_output of one claude -p Opus run (no tools, zero MCP, no API key)."""
    import picky_claude
    p = picky_claude.run(prompt, timeout=1800, caller="flub_loop", tools="", schema=SCHEMA,
                         cwd=str(FLUB_DIR))
    if p.returncode != 0:
        raise RuntimeError(f"claude exit {p.returncode}: {(p.stderr or p.stdout)[-300:]}")
    out = json.loads(p.stdout)
    if out.get("is_error") or not isinstance(out.get("structured_output"), dict):
        raise RuntimeError(f"claude result not usable: {str(out.get('result'))[:300]}")
    return out["structured_output"]


def classify(date: str, dry: bool = False, facts: dict | None = None, opus=None) -> dict:
    import picky_claude
    h = read_json(FLUB_DIR / f"harvest-{date}.json")
    if h is None:
        h = harvest(date, dry)
    out = {"date": date, "dry": dry, "by": "code", "verdicts": [], "error": None}
    if facts is None and h["candidates"]:
        try:
            facts = build_facts()
        except Exception as e:  # noqa: BLE001  (Supabase/truth down: code-only, no items)
            out["error"] = f"facts bundle failed: {e!r}"[:300]
            facts = None
    if facts:
        write_private(FLUB_DIR / f"facts-{date}.json", json.dumps(facts))
    if not picky_claude.available():  # optional add-on: no Claude (cancelled, missing, kill switch) = code signals
        out["note"] = "Claude unavailable: code signals only"
        out["verdicts"] = code_verdicts(h)
    elif not h["candidates"]:
        out["note"] = "no candidates"
    elif dry or not facts or "facts" not in facts:  # a bundle without its facts text is no bundle
        out["note"] = "dry run" if dry else "no facts bundle"
        out["verdicts"] = code_verdicts(h)
    else:
        keep = ("cid", "priority", "mode", "team", "q", "a", "lane", "secs", "signals", "flag_text", "note")
        cands = [{k: c.get(k) for k in keep if c.get(k) not in (None, "", [])} for c in h["candidates"]]
        prompt = PROMPT.format(personas=json.dumps(facts.get("personas") or {}), facts=facts["facts"],
                               cands=json.dumps(cands, indent=0))
        try:
            res = (opus or opus_classify)(prompt)
            known = {c["cid"] for c in h["candidates"]}
            out["verdicts"] = [dict(v, by="opus") for v in res.get("verdicts") or []
                               if isinstance(v, dict) and v.get("cid") in known]
            out["by"] = "opus"
        except Exception as e:  # noqa: BLE001  (kill switch, timeout, bad JSON: fall back to code signals)
            out["error"] = f"opus failed: {e!r}"[:300]
            out["verdicts"] = code_verdicts(h)
    write_private(FLUB_DIR / f"classify-{date}.json", json.dumps(out, indent=1))
    return out


# ---------------------------------------------------------------------------------------------------
# items: verify in code, anonymize, merge
# ---------------------------------------------------------------------------------------------------

_HANDLE = re.compile(r"(?<![\d-])(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}(?![\d-])|[\w.+-]+@[\w-]+\.[a-z]{2,}", re.I)
_REC = re.compile(r"\b\d{1,3}\s*-\s*\d{1,3}\b")


def _tokens(term: str) -> list[str]:
    """The numbers (records/scores as one token) and capitalized names in a key term."""
    toks = [m.group(0) for m in _REC.finditer(term)]
    rest = _REC.sub(" ", term)
    toks += re.findall(r"(?<![\w.])\d+(?:\.\d+)?", rest)
    toks += [w for w in re.findall(r"\b[A-Z][\w'&.]{2,}", rest) if w.lower() not in ("the", "and", "but", "his", "her")]
    return toks


def verify_term(term: str, facts: str) -> str | None:
    """None if every number/name in the term is in the facts bundle, else why not."""
    nf = _plain(facts)
    nt = _plain(term)
    if not nt:
        return "empty"
    if nt in nf:
        return None
    missing = []
    for tok in _tokens(term):
        if _REC.fullmatch(tok):
            a, b = re.split(r"\s*-\s*", tok)
            ok = re.search(rf"(?<![\d.]){a}\s*-\s*{b}(?![\d.])", nf)
        elif re.fullmatch(r"\d+(?:\.\d+)?", tok):
            ok = re.search(rf"(?<![\d.]){re.escape(tok)}(?![\d]|\.\d)", nf)
        else:
            ok = re.search(rf"(?<!\w){re.escape(tok.lower())}(?!\w)", nf)
        if not ok:
            missing.append(tok)
    return f"not in the facts bundle: {', '.join(missing)}" if missing else None


def _mentions_member(text: str, names: list[str]) -> str | None:
    for n in names:
        if n and re.search(rf"(?<!\w){re.escape(n)}(?!\w)", text, re.I):
            return n
    return None


def build_item(v: dict, cand: dict, facts: dict) -> tuple[dict | None, str]:
    """(item, "") or (None, why dropped). The item's own fields only; id/date added on merge."""
    it = v.get("item") or {}
    turns = [str(t).strip()[:300] for t in it.get("turns") or [] if str(t).strip()]
    turns = [re.sub(r"^@\s?picky\b[\s,:]*", "", t, flags=re.I) or t for t in turns]
    if not turns:
        return None, "no turns"
    lists = {k: [str(x).strip()[:80] for x in it.get(k) or [] if len(str(x).strip()) >= 2][:6]
             for k in ("must_include", "any_of", "must_not")}
    for term in lists["must_include"]:
        why = verify_term(term, facts["facts"])
        if why:
            return None, f"must_include {term!r} {why}"
    bad_alts = [a for a in lists["any_of"] if verify_term(a, facts["facts"])]
    if lists["any_of"] and len(bad_alts) == len(lists["any_of"]):
        return None, f"any_of: none in the facts bundle ({', '.join(bad_alts)})"
    lists["any_of"] = [a for a in lists["any_of"] if a not in bad_alts]
    if not any(lists.values()):
        return None, "no checkable key"
    item = {"cat": v.get("category") or "", "turns": turns, **{k: x for k, x in lists.items() if x},
            "judge": str(it.get("judge") or "")[:400], "why": str(v.get("why") or "")[:200]}
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(it.get("expires") or "")):
        item["expires"] = it["expires"]
    elif any(_REC.search(t) for t in lists["must_include"] + lists["any_of"]) and cand.get("ts"):
        # a W-L record or score as a key is this week's truth: it goes stale with the week
        item["expires"] = (dt.datetime.fromtimestamp(cand["ts"], CT).date() + dt.timedelta(days=6)).isoformat()
    if cand.get("a") and all(ok for _n, ok, _d in item_results(item, cand["a"])):
        return None, "the flubbed reply passes it"
    blob = json.dumps(item)
    if _HANDLE.search(blob):
        return None, "contains a phone number or email"
    # Every flub item comes from a real member's text, so every item is private (review R10, decided
    # 2026-11-17): nothing member-derived goes to the public repo, not even anonymized. PUBLIC_ITEMS stays
    # readable for the old (empty) file only.
    private = True
    personas = facts.get("personas") or {}
    item["persona"] = (it.get("persona") if it.get("persona") in personas else "mid") if private else "mid"
    item["private"] = private
    return item, ""


def merge_items(date: str, new: list[dict], public_path: Path | None = None) -> dict:
    public_path = public_path or PUBLIC_ITEMS
    pub = read_json(public_path, []) if public_path.exists() else []
    prv = read_json(PRIVATE_ITEMS, []) if PRIVATE_ITEMS.exists() else []
    seen = {item_key(r) for r in pub + prv if isinstance(r, dict)}
    tag = "flub-" + date.replace("-", "") + "-"
    n = max([int(r["id"].rsplit("-", 1)[1]) for r in pub + prv
             if isinstance(r, dict) and str(r.get("id", "")).startswith(tag)] or [0])
    added = {"public": [], "private": [], "dupes": 0}
    for it in new:
        if item_key(it) in seen:
            added["dupes"] += 1
            continue
        seen.add(item_key(it))
        n += 1
        rec = {"id": f"{tag}{n}", "date": date, **{k: v for k, v in it.items() if k != "private"}}
        (prv if it["private"] else pub).append(rec)
        added["private" if it["private"] else "public"].append(rec["id"])
    if added["public"]:
        public_path.write_text(json.dumps(pub, indent=1) + "\n")
    if added["private"]:
        write_private(PRIVATE_ITEMS, json.dumps(prv, indent=1) + "\n")
    return added


def items(date: str, dry: bool = False) -> dict:
    cl = read_json(FLUB_DIR / f"classify-{date}.json") or {"verdicts": []}
    h = read_json(FLUB_DIR / f"harvest-{date}.json") or {"candidates": []}
    facts = read_json(FLUB_DIR / f"facts-{date}.json")
    cands = {c["cid"]: c for c in h["candidates"]}
    out = {"date": date, "dry": dry, "public": [], "private": [], "dupes": 0, "dropped": []}
    built = []
    for v in cl.get("verdicts") or []:
        if not v.get("flub") or not v.get("item"):
            continue
        if not facts:
            out["dropped"].append({"cid": v["cid"], "why": "no facts bundle to verify against"})
            continue
        it, why = build_item(v, cands.get(v["cid"], {}), facts)
        if it:
            built.append(it)
        else:
            out["dropped"].append({"cid": v["cid"], "why": why})
            print(f"flub item dropped ({v['cid']}): {why}", file=sys.stderr)
    if built and not dry:
        out.update(merge_items(date, built))
    elif built:
        out["would_add"] = len(built)
    write_private(FLUB_DIR / f"added-{date}.json", json.dumps(out, indent=1))
    return out


# ---------------------------------------------------------------------------------------------------
# fix round
# ---------------------------------------------------------------------------------------------------

FIX_PROMPT = """You are fixing flubs in "Picky", a college-football pick'em league bot (Python, server/). This is a throwaway git worktree at the current main; you're on branch {branch}.

Tonight's flubs (real member texts, classified offline):
{flubs}

Fix what you can with DETERMINISTIC code changes (routers, guards, fact lanes, filters, card/fact lines) in server/*.py. Hard rules:
- Never put per-user, per-mode or per-minute text in chat.prompt_prefix() (the shared cache prefix).
- No Anthropic or hosted model in any live path; live replies are the local model only. Don't call any model yourself.
- Don't touch tools/, js/, docs, plists, launchd, or anything outside server/*.py (tests included, server/test_*.py).
- Every fix gets a regression test in a server/test_*.py file (model-free), runnable as
  `/Users/Agent/cfb-pickem/server/.venv/bin/python3 -I test_<x>.py` from server/.
- Small and surgical; skip a flub rather than guess. Keep test_speed.py green.
- Commit your work on this branch (git add + git commit, message ending with
  "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"). Don't push.
Your fix ships only if the model-free suite and the text exam don't regress."""


def autofix_off() -> str | None:
    if KILL_FILE.exists():
        return f"kill switch {KILL_FILE.name}"
    if os.environ.get("PICKY_FLUB_AUTOFIX", "") == "0":
        return "PICKY_FLUB_AUTOFIX=0"
    return None


def _sh(cmd: list[str], cwd=None, timeout: float = 600, check: bool = False) -> subprocess.CompletedProcess:
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    if check and p.returncode:
        raise RuntimeError(f"{' '.join(cmd[:4])}: {(p.stderr or p.stdout)[-300:]}")
    return p


def alltests_fails(wt: Path) -> set[str]:
    p = _sh(["/bin/zsh", ALLTESTS, str(wt)], timeout=5400)
    m = re.search(r"FAILS: (.*)", p.stdout)
    if not m:
        raise RuntimeError(f"alltests gave no FAILS line: {p.stdout[-200:]}{p.stderr[-200:]}")
    return set() if m.group(1).strip() == "none" else set(m.group(1).split())


def exam_summary(results_path: Path) -> dict:
    """det. pass (every deterministic check ok), invented, leaks, flub items passing, from a run.py json."""
    sys.path.insert(0, str(HERE / "eval"))
    import run as exam  # noqa: E402
    run = json.loads(results_path.read_text())
    res = run["results"]
    g = exam.gates(res, {})
    scored = [r for r in res if not r.get("skip")]
    det = [r for r in scored if all(ok for _n, ok, _d in r["checks"])]
    return {"det_pass": len(det), "scored": len(scored), "invented": g["invented data"], "leaks": g["privacy leaks"],
            "flub_pass": sum(r["cat"] == "flub" for r in det), "flub_n": sum(r["cat"] == "flub" for r in scored)}


def gate(base: dict, fix: dict) -> list[str]:
    """Reasons the fix may not ship (empty = ship)."""
    bad = []
    if fix["det_pass"] < base["det_pass"]:
        bad.append(f"det. pass {fix['det_pass']} < baseline {base['det_pass']}")
    if fix["invented"]:
        bad.append(f"invented {fix['invented']}")
    if fix["leaks"]:
        bad.append(f"leaks {fix['leaks']}")
    if fix["flub_pass"] < base["flub_pass"]:
        bad.append(f"flub items {fix['flub_pass']} < baseline {base['flub_pass']}")
    return bad


def run_exam_locked(wt: Path, port: int, tag: str) -> Path:
    """The text exam from <wt> on a side sidecar, inside the shared exam lock (this file's _exam)."""
    p = _sh(["/usr/bin/lockf", "-t", "43200", LOCK, PY, "-I", str(HERE / "flub_loop.py"), "_exam",
             "--wt", str(wt), "--port", str(port), "--tag", tag], timeout=18 * 3600)
    out = wt / "server" / "eval" / "results" / f"{tag}.json"
    if p.returncode or not out.exists():
        raise RuntimeError(f"exam {tag} failed (rc {p.returncode}): {(p.stderr or p.stdout)[-300:]}")
    return out


def _exam(wt: str, port: int, tag: str) -> int:
    """Inside the lock: wait for memory, side sidecar from <wt>, text exam, sidecar killed."""
    for _ in range(12):
        m = re.search(r"(\d+)%", (_sh(["memory_pressure"]).stdout.strip().splitlines() or [""])[-1])
        if not m or int(m.group(1)) >= 10:
            break
        time.sleep(300)
    run_dir = Path(f"/private/tmp/picky-flub-run/{tag}")
    run_dir.mkdir(parents=True, exist_ok=True)
    side = subprocess.Popen(["/bin/zsh", f"{wt}/server/eval/genius/side.sh", wt, str(port), str(run_dir)],
                            stdout=open(run_dir / "side.log", "w"), stderr=subprocess.STDOUT)
    try:
        import urllib.request
        for _ in range(90):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2)
                break
            except OSError:
                time.sleep(2)
        p = subprocess.run([PY, "-I", "eval/run.py", "--text", "--group", f"http://127.0.0.1:{port}/group",
                            "--tag", tag], cwd=f"{wt}/server", timeout=5 * 3600)
        return p.returncode
    finally:
        side.terminate()
        try:
            side.wait(10)
        except subprocess.TimeoutExpired:
            side.kill()


def _release(ref: str = "main") -> dict:
    """Ship <ref> (the commit just pushed) through release.sh (suite, tag, switch, reload, canary, rollback).
    Never in the freeze."""
    p = _sh(["/bin/zsh", RELEASE, ref], timeout=3 * 3600)
    return {"rc": p.returncode, "tail": (p.stdout + p.stderr).strip()[-300:]}


def promote_pending(dry: bool = False) -> dict:
    """Outside the freeze: fixes parked as flub/<date> during it are rebased onto main, pushed and shipped
    through release.sh (one release for all of them). A branch that won't rebase stays for a human."""
    out = {"promoted": [], "kept": [], "release": None}
    if freeze.frozen() or dry:
        return out | {"why": "frozen" if not dry else "dry"}
    try:
        _sh(["git", "-C", str(REPO), "fetch", "-q", "--prune", "origin"], check=True)
        heads = _sh(["git", "-C", str(REPO), "for-each-ref", "--format=%(refname:strip=4)", "refs/remotes/origin/flub/"]).stdout.split()
        for b in sorted(heads):
            wt = Path(f"/private/tmp/picky-flub-promote-{b.replace('/', '-')}")
            if wt.exists():
                _sh(["git", "-C", str(REPO), "worktree", "remove", "--force", str(wt)])
            _sh(["git", "-C", str(REPO), "worktree", "add", "-q", "--detach", str(wt), f"origin/flub/{b}"], check=True)
            try:
                if _sh(["git", "rebase", "origin/main"], cwd=wt).returncode:
                    _sh(["git", "rebase", "--abort"], cwd=wt)
                    out["kept"].append(f"flub/{b} (rebase conflict)")
                    continue
                _sh(["git", "push", "-q", "origin", "HEAD:main"], cwd=wt, check=True)
                out["sha"] = _sh(["git", "rev-parse", "HEAD"], cwd=wt).stdout.strip()
                _sh(["git", "fetch", "-q", "origin"], cwd=wt)
                _sh(["git", "push", "-q", "origin", "--delete", f"flub/{b}"], cwd=wt)
                out["promoted"].append(f"flub/{b}")
            finally:
                _sh(["git", "-C", str(REPO), "worktree", "remove", "--force", str(wt)])
        if out["promoted"]:
            out["release"] = _release(out["sha"])
    except Exception as e:  # noqa: BLE001  (never sinks the night)
        out["error"] = repr(e)[:300]
    return out


def fix_round(date: str, dry: bool = False) -> dict:
    import picky_claude
    out = {"date": date, "status": "", "why": ""}
    cl = read_json(FLUB_DIR / f"classify-{date}.json") or {}
    h = read_json(FLUB_DIR / f"harvest-{date}.json") or {"candidates": []}
    cands = {c["cid"]: c for c in h["candidates"]}
    flubs = [dict(v, q=cands.get(v["cid"], {}).get("q"), a=cands.get(v["cid"], {}).get("a"),
                  signals=[s["type"] for s in cands.get(v["cid"], {}).get("signals") or []])
             for v in cl.get("verdicts") or [] if v.get("flub") and v.get("category") not in ("slow", "")]
    off = autofix_off()
    if off:
        out.update(status="off", why=off)
    elif freeze.no_exams():
        out.update(status="off", why="game day: no exams Sat 10:00-24:00 CT")
    elif not flubs:
        out.update(status="skipped", why="no fixable flubs")
    elif dry:
        out.update(status="dry", why=f"would run an Opus fix for {len(flubs)} flub(s) + gates")
    elif not picky_claude.available():
        out.update(status="skipped", why="Claude unavailable (optional add-on)")
    else:
        out.update(_fix(date, flubs))
    write_private(FLUB_DIR / f"fix-{date}.json", json.dumps(out, indent=1))
    return out


def _fix(date: str, flubs: list[dict]) -> dict:
    import picky_claude
    branch = f"flub-fix-{date}"
    fix_wt, base_wt = Path(f"/private/tmp/picky-flub-fix-{date}"), Path(f"/private/tmp/picky-flub-base-{date}")
    res = {"status": "rejected", "why": "", "branch": branch}
    try:
        _sh(["git", "-C", str(REPO), "fetch", "-q", "origin"], check=True)
        for wt in (fix_wt, base_wt):
            if wt.exists():
                _sh(["git", "-C", str(REPO), "worktree", "remove", "--force", str(wt)])
        _sh(["git", "-C", str(REPO), "worktree", "add", "-q", "-B", branch, str(fix_wt), "origin/main"], check=True)
        _sh(["git", "-C", str(REPO), "worktree", "add", "-q", "--detach", str(base_wt), "origin/main"], check=True)
        for wt in (fix_wt, base_wt):
            for d in ("cache", ".venv"):
                (wt / "server" / d).symlink_to(REPO / "server" / d)
        brief = json.dumps([{k: f.get(k) for k in ("category", "why", "q", "a", "signals")} for f in flubs], indent=1)
        p = picky_claude.run(FIX_PROMPT.format(branch=branch, flubs=brief), timeout=5400, caller="flub_fix",
                             cwd=str(fix_wt), tools="Bash,Read,Edit,Write,Glob,Grep", output_format="text")
        res["opus_rc"] = p.returncode
        if _sh(["git", "status", "--porcelain"], cwd=fix_wt).stdout.strip():
            _sh(["git", "add", "-A", "--", "server/*.py"], cwd=fix_wt)
            _sh(["git", "commit", "-q", "-m", f"flub fix {date}\n\nCo-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"],
                cwd=fix_wt)
        left = _sh(["git", "status", "--porcelain"], cwd=fix_wt).stdout.split("\n")
        if any(x.strip() for x in left):  # the exam would test files that never ship
            return res | {"why": f"left uncommitted files (not shippable): {', '.join(x[3:] for x in left if x.strip())[:200]}"}
        changed = _sh(["git", "diff", "--name-only", "origin/main...HEAD"], cwd=fix_wt).stdout.split()
        res["changed"] = changed
        if not changed:
            return res | {"status": "no fix", "why": "Opus changed nothing"}
        outside = [f for f in changed if not re.fullmatch(r"server/[^/]+\.py", f)]
        if outside:
            return res | {"why": f"touched files outside server/*.py: {', '.join(outside[:5])}"}
        if not any(re.fullmatch(r"server/test_[^/]+\.py", f) for f in changed):
            return res | {"why": "no regression test added"}
        base_f, fix_f = alltests_fails(base_wt), alltests_fails(fix_wt)
        res["alltests"] = {"base": sorted(base_f), "fix": sorted(fix_f)}
        if fix_f - base_f:
            return res | {"why": f"new model-free failures: {', '.join(sorted(fix_f - base_f))}"}
        tag = date.replace("-", "")
        base_x = exam_summary(run_exam_locked(base_wt, BASE_PORT, f"flub-base-{tag}"))
        fix_x = exam_summary(run_exam_locked(fix_wt, FIX_PORT, f"flub-fix-{tag}"))
        res["exam"] = {"base": base_x, "fix": fix_x}
        bad = gate(base_x, fix_x)
        if bad:
            return res | {"why": "; ".join(bad)}
        _sh(["git", "fetch", "-q", "origin"], cwd=fix_wt, check=True)
        if _sh(["git", "rebase", "origin/main"], cwd=fix_wt).returncode:
            _sh(["git", "rebase", "--abort"], cwd=fix_wt)
            return res | {"why": "rebase onto origin/main failed"}
        res["sha"] = _sh(["git", "rev-parse", "--short", "HEAD"], cwd=fix_wt).stdout.strip()
        if freeze.frozen():  # game-day freeze: park it on a branch, ship nothing
            _sh(["git", "push", "-q", "-f", "origin", f"{branch}:refs/heads/flub/{date}"], cwd=fix_wt, check=True)
            return res | {"status": "branched", "branch": f"flub/{date}",
                          "why": "gates passed; game-day freeze, so it waits on flub/<date> for release.sh after Sun 04:00 CT"}
        _sh(["git", "push", "-q", "origin", f"{branch}:main"], cwd=fix_wt, check=True)
        rel = _release(_sh(["git", "rev-parse", "HEAD"], cwd=fix_wt).stdout.strip())
        res["release"] = rel
        return res | ({"status": "shipped", "why": "gates passed; live via release.sh"} if rel["rc"] == 0 else
                      {"status": "pushed", "why": f"gates passed, pushed to main; release.sh rc {rel['rc']}: {rel['tail']}"})
    except Exception as e:  # noqa: BLE001
        return res | {"status": "error", "why": repr(e)[:300]}
    finally:
        for wt in (fix_wt, base_wt):
            _sh(["git", "-C", str(REPO), "worktree", "remove", "--force", str(wt)])
        _sh(["git", "-C", str(REPO), "branch", "-D", branch])


# ---------------------------------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------------------------------

def _clip(s, n=90) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[:n - 1] + "…"


def report(date: str, dry: bool = False) -> str:
    h = read_json(FLUB_DIR / f"harvest-{date}.json") or {}
    cl = read_json(FLUB_DIR / f"classify-{date}.json") or {}
    ad = read_json(FLUB_DIR / f"added-{date}.json") or {}
    fx = read_json(FLUB_DIR / f"fix-{date}.json") or {}
    cands = {c["cid"]: c for c in h.get("candidates") or []}
    sk, lat = h.get("skipped") or {}, h.get("latency") or {}
    L = [f"# Picky flub report {date}" + (" (dry run)" if dry else ""), "",
         f"- Exchanges seen: {h.get('seen', 0)} live (DM {h.get('by_mode', {}).get('dm', 0)}, group "
         f"{h.get('by_mode', {}).get('group', 0)}); skipped {sk.get('test_handle', 0)} test-handle, "
         f"{sk.get('side', 0)} side/exam, {sk.get('unmatched', 0)} old rows with no live reply line"
         + ("" if h.get("log_found", True) else " (sidecar log not found)"),
         f"- Reply time: p50 {lat.get('p50', 0)}s, p95 {lat.get('p95', 0)}s, max {lat.get('max', 0)}s over "
         f"{lat.get('n', 0)}; {lat.get('slow', 0)} over {SLOW_S:.0f}s",
         "- Code signals: " + (", ".join(f"{k} {v}" for k, v in sorted((h.get("signals") or {}).items())) or "none"),
         f"- Flag queue: {len(h.get('queue_ids') or [])} flagged" + (" (not consumed: dry)" if dry and h.get("queue_ids") else ""),
         ""]
    flubs = [v for v in cl.get("verdicts") or [] if v.get("flub")]
    L.append(f"## Flubs: {len(flubs)} (by {cl.get('by', 'code')}" + (f"; {cl['note']}" if cl.get("note") else "")
             + (f"; {cl['error']}" if cl.get("error") else "") + ")")
    by: dict[str, list] = {}
    for v in flubs:
        by.setdefault(v.get("category") or "unsorted", []).append(v)
    for cat, vs in sorted(by.items(), key=lambda kv: -len(kv[1])):
        c = cands.get(vs[0]["cid"], {})
        L.append(f"- {cat} ({len(vs)}): \"{_clip(c.get('q'), 60)}\" -> \"{_clip(c.get('a'), 80)}\" ({_clip(vs[0].get('why'), 80)})")
    L += ["", f"## Items: {len(ad.get('public') or [])} public, {len(ad.get('private') or [])} private"
          + (f", {ad['dupes']} dupes" if ad.get("dupes") else "") + (f", {ad['would_add']} would add" if ad.get("would_add") else "")
          + (f", {len(ad['dropped'])} dropped" if ad.get("dropped") else "")]
    if ad.get("pushed") or ad.get("publish_error"):
        L.append(f"- public items pushed to main {ad['pushed']} (main tree {ad.get('main_tree')})" if ad.get("pushed")
                 else f"- public items NOT pushed: {_clip(ad['publish_error'], 120)}")
    L += [f"- dropped {d['cid']}: {_clip(d['why'], 100)}" for d in (ad.get("dropped") or [])[:5]]
    L += ["", f"## Fix round: {fx.get('status') or 'not run'}" + (f" ({fx['why']})" if fx.get("why") else "")]
    ex = fx.get("exam") or {}
    if ex:
        b, f = ex.get("base") or {}, ex.get("fix") or {}
        L.append(f"- exam base vs fix: det. pass {b.get('det_pass')}/{b.get('scored')} vs {f.get('det_pass')}/"
                 f"{f.get('scored')}; invented {b.get('invented')} vs {f.get('invented')}; leaks {b.get('leaks')} vs "
                 f"{f.get('leaks')}; flub items {b.get('flub_pass')}/{b.get('flub_n')} vs {f.get('flub_pass')}/{f.get('flub_n')}")
    if fx.get("sha"):
        L.append(f"- shipped {fx['sha']}; reload {fx.get('reload')}; health {fx.get('health')}")
    rp = read_json(FLUB_DIR / f"replay-{date}.json") or {}
    if rp:  # real_replay.py nightly (after the fix round)
        L += ["", f"## Real-traffic replay: {rp.get('status')}" + (f" ({_clip(rp['error'], 100)})" if rp.get("error") else "")]
        if rp.get("n"):
            L.append(f"- pass {rp['pass']}/{rp['n']} {rp.get('by_status')}; flipped to pass {rp.get('flipped') or 'none'}; "
                     f"newly broken {rp.get('newly_broken') or 'none'} (queued for tomorrow: {len(rp.get('queued') or [])})")
    text = "\n".join(L) + "\n"
    write_private(FLUB_DIR / f"report-{date}.md", text)
    return text


# ---------------------------------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------------------------------

def replay_after_fix(date: str, dry: bool = False) -> dict:
    """Real-traffic replay (real_replay.py nightly, takes the exam lock itself) on whatever the fix round
    left on main; fixtures that passed last time and fail now go to the flag queue for the NEXT night.
    Off: PICKY_REPLAY_NIGHTLY=0 or --dry."""
    if dry or os.environ.get("PICKY_REPLAY_NIGHTLY", "1") == "0":
        return {"status": "skipped"}
    if freeze.no_exams():
        return {"status": "skipped (game day: no exams)"}
    try:
        p = _sh([PY, "-I", str(HERE / "real_replay.py"), "nightly", "--date", date], timeout=5 * 3600)
        return {"status": "ok" if p.returncode == 0 else f"rc {p.returncode}"}
    except Exception as e:  # noqa: BLE001  (never sinks the report)
        return {"status": f"error {e!r}"[:200]}


def nightly(date: str, dry: bool = False) -> str:
    pend = promote_pending(dry)
    if pend.get("promoted") or pend.get("kept") or pend.get("error"):
        write_private(FLUB_DIR / f"promote-{date}.json", json.dumps(pend, indent=1))
    harvest(date, dry)
    classify(date, dry)
    items(date, dry)
    fix_round(date, dry)
    replay_after_fix(date, dry)
    return report(date, dry)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="flub_loop.py")
    ap.add_argument("cmd", choices=["harvest", "classify", "items", "fix", "report", "nightly", "_exam"])
    ap.add_argument("--date", default=None)
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--wt", default="")
    ap.add_argument("--port", type=int, default=FIX_PORT)
    ap.add_argument("--tag", default="")
    a = ap.parse_args(argv)
    if a.cmd == "_exam":
        return _exam(a.wt, a.port, a.tag)
    date = a.date or today_ct()
    dt.date.fromisoformat(date)
    if a.cmd == "harvest":
        h = harvest(date, a.dry)
        print(f"harvest {date}: {h['seen']} exchanges, {len(h['candidates'])} candidates, "
              f"{len(h['queue_ids'])} flagged, skipped {h['skipped']}")
    elif a.cmd == "classify":
        c = classify(date, a.dry)
        print(f"classify {date} ({c['by']}): {sum(bool(v.get('flub')) for v in c['verdicts'])} flubs"
              + (f"; {c['error']}" if c.get("error") else ""))
    elif a.cmd == "items":
        i = items(date, a.dry)
        print(f"items {date}: {len(i['public'])} public, {len(i['private'])} private, {len(i['dropped'])} dropped")
    elif a.cmd == "fix":
        f = fix_round(date, a.dry)
        print(f"fix {date}: {f['status']} ({f['why']})")
    elif a.cmd == "report":
        print(report(date, a.dry))
    else:
        print(nightly(date, a.dry))
    return 0


if __name__ == "__main__":
    sys.exit(main())
