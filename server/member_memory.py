"""
Picky's member memory: what he remembers about each league member, built offline.

WIRED LIVE 2026-11-17 (Charlie's go, 14:42 CT): group_chat.answer runs the forget handler and the
send-point receipt check; picky_brain adds one lines_for rider line to the user turn and falls back to
stance_hint (cold -> sharp, warm -> friendly) when no record stance fires. See "WIRING" below.

  CHARLIE IS ALWAYS NICE (Charlie, 2026-11-17: "Always be nice to me tho bc i have to send him test
  messages that are mean sometimes"). Chuck P's messages never log treatment events, beefs or lines, and
  his stance_hint is always warm. Off switch: touch cfb-pickem-private/PICKY_CHARLIE_NICE_OFF.

  STORE. One private 0600 JSON file per member: cfb-pickem-private/member_memory/<team>.json
  (PICKY_MEMBER_MEMORY_DIR overrides; never this public repo). Each file holds:
    items      stated facts (favorite team, alma mater, teams they hate), running beefs, inside
               jokes / callbacks, memorable lines. Every item has a date, a source ("group" or
               "dm") and cites the message it came from (msg id, ts, chat hash, exact quote).
    receipts   explicit predictions ("Iowa covers easy", "LSU by 20", "I'm winning this week"):
               who, date, game_id, claim, side, line/margin. Graded in CODE against all_games
               finals once the game is final (hit / miss / push + the real score). The model
               only extracts; it never grades.
    treatment  how the member treats Picky, one label per message (hostile / neutral /
               friendly), aggregated with exponential decay (half-life HALF_LIFE_DAYS) into a
               score and a stance hint (cold / neutral / warm) plus a short dated "why" list.
    forgotten  markers left by the forget handler so a re-run never re-adds what was forgotten.

  SOURCES. Picky's real exchanges in cfb-pickem-private/picky_exchanges.jsonl (the group
  sidecar's log: group + DMs, member text + Picky's reply). Test handles (team null), side /
  exam sidecars (side=1) and old rows that no live reply line backs (pre-11/17 exam traffic) are
  skipped, the same way flub_loop.harvest does it. Web rows carry no team and are skipped.

  NIGHTLY (LaunchAgent com.pickem.member-memory, 22:30 CT): every real row from the last
  LOOKBACK_DAYS not yet processed -> one offline local gemma pass per batch via local_llm (no Claude,
  JSON schema) -> code validation (the quote must be an exact substring of the member's own
  message, msg ids and speakers must match, game ids must exist, the sensitive filter) -> merge
  -> grade every pending receipt -> save. The local call waits behind live member texts
  (ollama_gate); Ollama down = the batch stays unprocessed for the next night (grading still runs).

  PRIVACY. DM-sourced items are returned ONLY when the caller is that same member AND dm=True:
  never in group context, never to or about another member (visible()). Sensitive personal info
  (health, family, work, location, money outside the league, identity) is dropped by a code
  filter (sensitive()) on top of the prompt rule, on extraction and again on every read.

  API (for later wiring):
    lines_for(sender_team, question, named_teams, dm, limit=3) -> 2-3 dated lines, receipts first
    stance_hint(team, dm=False, caller=None) -> {"stance", "score", "hint", "why"}
    is_forget(text) / forget(team, text, dm) -> deletes matching items, logs to forget_log.jsonl

  CLI:
    member_memory.py show <team> [--q "question"]   what Picky would know + the lines he'd get
    member_memory.py nightly [--dry]                 the 22:30 job
    member_memory.py backfill [--since 2026-11-15]   everything since Picky went live
    member_memory.py grade                           grade pending receipts only
    member_memory.py forget <team> "<text>" [--dm]   run the forget handler by hand
    member_memory.py scrub-charlie                   clear hostile events + beefs/lines his test texts left

  WIRING (live since 2026-11-17):
    1. group_chat.answer (or picky_brain after the brief): if is_forget(text) -> forget(team, text,
       dm) and reply with its "reply"; skip the model.
    2. picky_brain: rider = lines_for(team, q, named_teams, dm=is_dm); put the lines in the draft
       prompt as "MEMBER MEMORY (dated, true; use at most one, only if it fits)". Never prefix.
    3. picky_brain.compute_stance: when no stance fires, stance_hint(team, dm, caller=team)
       ["stance"] == "cold" -> sharp, "warm" -> friendly.
    4. A send-point check: a reply quoting a receipt must match its graded result.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))  # flub_loop / picky_claude live next to this file
CT = ZoneInfo("America/Chicago")
PRIVATE = Path(os.environ.get("PICKY_PRIVATE_DIR", "/Users/Agent/cfb-pickem-private"))
MEM_DIR = Path(os.environ.get("PICKY_MEMBER_MEMORY_DIR", PRIVATE / "member_memory"))
EXCHANGES = Path(os.environ.get("PICKY_EXCHANGE_LOG", PRIVATE / "picky_exchanges.jsonl"))
MEMBERS_FILE = Path(os.environ.get("PICKY_GROUP_MEMBERS", PRIVATE / "picky_group_members.json"))
REST = "https://vopdioszofwdkwnujtiq.supabase.co/rest/v1"

LIVE_SINCE = "2026-11-15"   # Picky went live in the league
LOOKBACK_DAYS = 3           # the nightly picks up anything unprocessed this far back
HALF_LIFE_DAYS = 10.0       # treatment decay
COLD, WARM = -0.3, 0.3      # stance thresholds on the decayed score in [-1, 1]
MIN_WEIGHT = 1.5            # below this much decayed evidence (~2 fresh messages) the stance stays neutral
BATCH = 20                  # messages per local-model call (gemma's 16k context, room for the prompt)
KINDS = ("fact", "beef", "joke", "line")
SUBKINDS = ("favorite_team", "alma_mater", "hates_team", "other")
TIER = {"receipt": 0, "beef": 1, "fact": 2, "joke": 3, "line": 4}
CLAIM_KINDS = ("cover", "win", "margin", "week_best", "week_record")
LABELS = {"hostile": -1.0, "neutral": 0.0, "friendly": 1.0}
CHARLIE_TEAM = os.environ.get("PICKY_CHARLIE_TEAM", "Chuck P")  # Charlie's team row (chat.LEADER_USER_ID's team)
CHARLIE_NICE_OFF = PRIVATE / "PICKY_CHARLIE_NICE_OFF"            # touch it to treat Charlie like anyone else
CHARLIE_SKIP_KINDS = ("beef", "line")                             # never stored from his (often test) messages


# ---------------------------------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------------------------------

def now_ts() -> float:
    return time.time()


def ct_date(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, CT).date().isoformat()


def day_bounds(date: str) -> tuple[float, float]:
    d = dt.date.fromisoformat(date)
    start = dt.datetime(d.year, d.month, d.day, tzinfo=CT)
    return start.timestamp(), (start + dt.timedelta(days=1)).timestamp()


def pretty_date(date: str) -> str:
    d = dt.date.fromisoformat(date)
    return f"{d.strftime('%b')} {d.day}"


def norm(s) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9&' ]+", " ", str(s or "").lower().replace("’", "'"))).strip()


def slug(team: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(team).lower()).strip("_") or "unknown"


def msg_id(row: dict) -> str:
    key = f"{row.get('chat') or row.get('mode')}|{row.get('ts')}|{row.get('team')}|{row.get('q')}"
    return "m" + hashlib.sha1(key.encode()).hexdigest()[:12]


def item_id(mid: str, kind: str, text: str) -> str:
    return "i" + hashlib.sha1(f"{mid}|{kind}|{norm(text)}".encode()).hexdigest()[:10]


def write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.replace(tmp, path)


def _append_private(path: Path, rec: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a") as f:
        f.write(json.dumps(rec) + "\n")


# ---------------------------------------------------------------------------------------------------
# sensitive-info filter (code, on top of the prompt rule)
# ---------------------------------------------------------------------------------------------------

_LEAGUE_MONEY = re.compile(r"\b(buy-?in|pot|jackpot|payouts?|chips?|league|pool|entry|slots?|blackjack|bankroll)\b", re.I)
_SENSITIVE = {
    "health": r"\b(cancer|chemo\w*|tumou?rs?|surgery|surgeries|hospital\w*|diagnos\w*|pregnan\w*|miscarriage|"
              r"therapy|therapist|rehab|depress(ed|ion)|anxiety|adhd|autis\w*|medications?|meds|prescriptions?|"
              r"doctors?|urgent care|covid|illness|disease|diabet\w*|heart attack|had a stroke|colonoscopy|"
              r"vasectomy|insulin|got sick|was sick|am sick|i'm sick|im sick|in the er)\b",
    "family": r"\b(wife|wifey|husband|spouse|kids?|son|sons|daughters?|bab(y|ies)|toddlers?|divorc\w*|girlfriend|"
              r"boyfriend|fianc[eé]e?|mom|moms|mother|dad|father|parents?|brother|sister|grandm\w*|grandp\w*|"
              r"funeral|passed away|died|custody|in-laws?|family|nephew|niece|cousin)\b",
    "work": r"\b(my job|at work|work tomorrow|working late|my boss|boss|coworkers?|co-workers?|laid off|layoffs?|"
            r"got fired|i got fired|unemployed|salary|promotion|my office|my company|job interview|my clients?|"
            r"my shift)\b",
    "location": r"\b(i live|i'm living|im living|live in|lives in|living in|my address|address|my house|"
                r"my apartment|my condo|moved to|moving to|zip ?code|street|avenue|apt|neighborhood|hometown|"
                r"home town)\b",
    "money": r"\b(mortgage|rent|debt|loans?|bankrupt\w*|paycheck|401k|credit card|taxes|irs|my bills|net worth|"
             r"inheritance|savings|venmo|zelle|cash ?app)\b",
    "identity": r"\b(gay|lesbian|homo\w*|transgender|queer|church|jewish|jew|muslim|christian|catholic|mormon|"
                r"religio\w*|democrats?|republicans?|trump|biden|liberals?|conservatives?|racist)\b",
    "contact": r"(\b\d{3}[-.\s]?\d{3}[-.\s]?\d{4}\b|[\w.+-]+@[\w-]+\.\w+)",
}
_SENS_RX = {k: re.compile(v, re.I) for k, v in _SENSITIVE.items()}
_DOLLARS = re.compile(r"\$\s?\d|\b\d+\s?(bucks|dollars|grand)\b", re.I)


def sensitive(*texts) -> str | None:
    """The first sensitive category any of the texts trips, else None. Dollar amounts count as
    money outside the league unless the same text is about league money (buy-in, pot, chips...)."""
    for t in texts:
        if not t:
            continue
        t = " ".join(t) if isinstance(t, (list, tuple)) else str(t)
        for k, rx in _SENS_RX.items():
            if rx.search(t):
                return k
        if _DOLLARS.search(t) and not _LEAGUE_MONEY.search(t):
            return "money"
    return None


# ---------------------------------------------------------------------------------------------------
# members + store
# ---------------------------------------------------------------------------------------------------

def load_members() -> list[dict]:
    """[{"team", "name"}] from the private handle map (handles never leave this function)."""
    try:
        d = json.loads(MEMBERS_FILE.read_text())
    except (OSError, ValueError):
        return []
    rows = d.get("members", d) if isinstance(d, dict) else d
    return [{"team": m["team"], "name": m.get("name") or ""} for m in rows if isinstance(m, dict) and m.get("team")]


def _empty(team: str) -> dict:
    return {"team": team, "updated": None, "items": [], "receipts": [], "treatment": {"events": []},
            "forgotten": []}


def path_for(team: str) -> Path:
    return MEM_DIR / f"{slug(team)}.json"


def load(team: str) -> dict:
    try:
        d = json.loads(path_for(team).read_text())
        if isinstance(d, dict) and d.get("team") == team:
            for k, v in _empty(team).items():
                d.setdefault(k, v)
            return d
    except (OSError, ValueError):
        pass
    return _empty(team)


def save(mem: dict) -> None:
    mem["updated"] = dt.datetime.now(CT).isoformat(timespec="seconds")
    write_private(path_for(mem["team"]), json.dumps(mem, indent=1))


def all_teams() -> list[str]:
    teams = {m["team"] for m in load_members()}
    if MEM_DIR.exists():
        for f in MEM_DIR.glob("*.json"):
            try:
                t = json.loads(f.read_text()).get("team")
            except (OSError, ValueError, AttributeError):
                continue
            if t:
                teams.add(t)
    return sorted(teams)


def _load_state() -> dict:
    try:
        d = json.loads((MEM_DIR / "state.json").read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(st: dict) -> None:
    cut = (dt.date.today() - dt.timedelta(days=60)).isoformat()
    st["processed"] = {k: v for k, v in (st.get("processed") or {}).items() if v >= cut}
    write_private(MEM_DIR / "state.json", json.dumps(st, indent=1))


# ---------------------------------------------------------------------------------------------------
# sources: the day's real exchanges
# ---------------------------------------------------------------------------------------------------

def real_rows(start: float, end: float) -> list[dict]:
    """Real member exchanges in [start, end), oldest first, each with its msg id and source."""
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
        return []
    rows = [dict(r) for r in raw if r.get("team") and not r.get("side") and (r.get("q") or "").strip()
            and r.get("mode") in ("group", "dm", "web")]
    if any(not r.get("chat") for r in rows):
        # rows from before the chat/side fields existed are real only if the LIVE sidecar logged a
        # reply for them (flub_loop's rule); without the log, keep only rows that carry a chat id
        try:
            import flub_loop
            replies, found = flub_loop.parse_log(flub_loop.STDERR_LOG, start, end)
            if found:
                flub_loop._match(rows, replies)
        except Exception:  # noqa: BLE001  (a missing/odd log must never break the nightly)
            pass
        rows = [r for r in rows if r.get("chat") or r.pop("_reply", None)]
    for r in rows:
        r.pop("_reply", None)
        r["msg_id"] = msg_id(r)
        r["source"] = "group" if r["mode"] == "group" else "dm"
    rows.sort(key=lambda r: r["ts"])
    return rows


# ---------------------------------------------------------------------------------------------------
# league data (all_games finals + picks) for receipts
# ---------------------------------------------------------------------------------------------------

class League:
    """games: {game_id: {id, week, away, home, away_pts, home_pts, line, winner, status, start, pool}}
    picks: {game_id: {team: pick}} for final pool games (only needed for week claims)."""

    def __init__(self, games: dict, picks: dict | None = None, humans: list | None = None):
        self.games = {int(k): v for k, v in games.items()}
        self.picks = {int(k): v for k, v in (picks or {}).items()}
        self.humans = humans or []

    def pool_weeks(self) -> dict:
        out: dict = {}
        for g in self.games.values():
            if g.get("pool"):
                out.setdefault(g["week"], []).append(g)
        return out


def _service_key() -> str:
    import subprocess
    return subprocess.run(["security", "find-generic-password", "-a", "drake", "-s",
                           "CFB_PICKEM__SUPABASE_SERVICE_KEY", "-w"], capture_output=True, text=True,
                          check=True).stdout.strip()


def fetch_league() -> League:
    import httpx
    key = _service_key()
    h = {"apikey": key, "Authorization": f"Bearer {key}"}
    with httpx.Client(timeout=30, headers=h) as c:
        def get_all(q: str) -> list:
            out, off = [], 0
            while True:
                r = c.get(f"{REST}/{q}&limit=1000&offset={off}")
                r.raise_for_status()
                rows = r.json()
                out += rows
                if len(rows) < 1000:
                    return out
                off += 1000

        r = c.get(f"{REST}/all_games?select=cfb_season&order=cfb_season.desc&limit=1")
        r.raise_for_status()
        season = r.json()[0]["cfb_season"]
        rows = get_all('all_games?select=GameId,Away,Home,AwayPts,HomePts,line,winner,Status,week,picked,'
                       f'"Start (CT)"&cfb_season=eq.{season}&order=GameId.asc')
        games = {r["GameId"]: {"id": r["GameId"], "week": r.get("week"), "away": r["Away"], "home": r["Home"],
                               "away_pts": r.get("AwayPts"), "home_pts": r.get("HomePts"), "line": r.get("line"),
                               "winner": r.get("winner"), "status": r.get("Status"),
                               "start": r.get("Start (CT)"), "pool": bool(r.get("picked"))} for r in rows}
        teams = {t["team_id"]: t["team_name"] for t in
                 get_all("teams?select=team_id,team_name&is_exhibition=is.false")}
        final_pool = sorted(g["id"] for g in games.values() if g["pool"] and _final(g))
        picks: dict = {}
        for i in range(0, len(final_pool), 150):
            ids = ",".join(str(x) for x in final_pool[i:i + 150])
            for p in get_all(f"picks?select=team_id,game_id,pick&game_id=in.({ids})&order=id.asc"):
                t = teams.get(p["team_id"])
                if t:
                    picks.setdefault(p["game_id"], {})[t] = p["pick"]
    return League(games, picks, sorted(teams.values()))


def _final(g: dict) -> bool:
    return (str(g.get("status") or "").lower() == "final" or bool(g.get("winner"))) \
        and g.get("away_pts") is not None and g.get("home_pts") is not None


def _start_ts(g: dict) -> float | None:
    try:
        return dt.datetime.fromisoformat(str(g["start"])).replace(tzinfo=CT).timestamp()
    except (KeyError, TypeError, ValueError):
        return None


def week_for(ts: float, lg: League) -> int | None:
    """The pool week a week claim made at ts is about: the first pool week whose last kickoff is
    after the claim (a claim on the Monday after a finished week is about the next one)."""
    best = None
    for w, games in sorted(lg.pool_weeks().items(), key=lambda kv: kv[0] or 0):
        last = max((_start_ts(g) or 0) for g in games)
        if last > ts and (best is None or w < best):
            best = w
    return best


# ---------------------------------------------------------------------------------------------------
# receipts: grading in code
# ---------------------------------------------------------------------------------------------------

def _num(x) -> str:
    x = float(x)
    return str(int(x)) if x == int(x) else f"{x:g}"


def score_text(g: dict) -> str:
    return f"{g['away']} {g['away_pts']}, {g['home']} {g['home_pts']}"


def grade_game_claim(r: dict, g: dict) -> dict | None:
    """hit/miss/push for a cover/win/margin claim on a final game, else None (not final yet).
    `line` on the game is the HOME adjustment (negative = home favored): home covers when
    home_pts + line > away_pts. A claim's own `line` is from the claimed side's view
    ("Iowa -6.5" = Iowa gives 6.5); without one the league line is used."""
    if not _final(g):
        return None
    side = r.get("side")
    if side == g["home"]:
        sp, op, home = g["home_pts"], g["away_pts"], True
    elif side == g["away"]:
        sp, op, home = g["away_pts"], g["home_pts"], False
    else:
        return {"status": "void", "why": f"side {side!r} isn't in this game"}
    out = {"status": "graded", "score": score_text(g), "graded_at": ct_date(now_ts())}
    kind = r["kind"]
    if kind == "cover":
        if r.get("line") is not None:
            L = float(r["line"])
        elif g.get("line") is not None:
            L = float(g["line"]) if home else -float(g["line"])
        else:
            return {"status": "void", "why": "no line on this game"}
        diff = sp + L - op
        out["line_used"] = L
        out["detail"] = f"{side} {'+' if L > 0 else ''}{_num(L)}" if L else f"{side} pick'em"
    elif kind == "win":
        diff = sp - op
    elif kind == "margin":
        diff = (sp - op) - float(r.get("margin") or 0)
        out["detail"] = f"{side} by {_num(r.get('margin') or 0)}+"
    else:
        return None
    if kind == "margin":  # "LSU by 20" and they won by exactly 20 = called it (no push)
        out["result"] = "hit" if diff >= 0 else "miss"
    else:
        out["result"] = "hit" if diff > 0 else "miss" if diff < 0 else "push"
    out["margin_vs_claim"] = round(diff, 1)
    return out


def grade_week_claim(r: dict, lg: League) -> dict | None:
    """week_best ("I'm winning this week") = most covers among human teams that week (ties count);
    week_record ("I'm going 4-0") = at least that many covers. Covers use the league's graded
    `winner` (a dead-even score goes to the away side, the league's rule). None until every pool
    game that week is final."""
    w = r.get("week")
    if w is None:
        return None
    games = lg.pool_weeks().get(w) or []
    if not games or not all(_final(g) and g.get("winner") for g in games):
        return None
    wins: dict = {}
    for g in games:
        for team, pick in (lg.picks.get(int(g["id"])) or {}).items():
            wins[team] = wins.get(team, 0) + (pick == g["winner"])
    who = r["who"]
    if who not in wins:
        return {"status": "void", "why": f"no picks for {who} in week {w}"}
    mine, n = wins[who], len(games)
    out = {"status": "graded", "graded_at": ct_date(now_ts()), "score": f"{who} went {mine}-{n - mine} in week {w}"}
    if r["kind"] == "week_best":
        top = max(wins.values())
        leaders = [t for t, v in wins.items() if v == top]
        out["result"] = "hit" if mine == top else "miss"
        out["detail"] = (f"best week was {top}-{n - top}" + (f" ({', '.join(sorted(leaders)[:3])})"
                                                             if out["result"] == "miss" else ""))
    else:
        need = int(r.get("wins") or 0)
        out["result"] = "hit" if mine >= need else "miss"
        out["detail"] = f"called {need}-{n - need}"
    return out


def grade_pending(mem: dict, lg: League) -> int:
    n = 0
    for r in mem["receipts"]:
        if r.get("status") != "pending":
            continue
        if r["kind"] in ("week_best", "week_record"):
            if r.get("week") is None:
                r["week"] = week_for(r["ts"], lg)
            res = grade_week_claim(r, lg)
        else:
            g = lg.games.get(int(r.get("game_id") or 0))
            res = grade_game_claim(r, g) if g else {"status": "void", "why": "game not found"}
        if res:
            r.update(res)
            n += 1
    return n


# ---------------------------------------------------------------------------------------------------
# treatment (decayed)
# ---------------------------------------------------------------------------------------------------

def decay_weight(age_days: float) -> float:
    return 0.5 ** (max(0.0, age_days) / HALF_LIFE_DAYS)


def treatment_score(events: list[dict], now: float | None = None) -> tuple[float, float]:
    """(score in [-1, 1], total decayed weight) over labeled events."""
    now = now if now is not None else now_ts()
    num = den = 0.0
    for e in events:
        v = LABELS.get(e.get("label"))
        if v is None:
            continue
        w = decay_weight((now - float(e["ts"])) / 86400)
        num += v * w
        den += w
    return (num / den if den else 0.0), den


def charlie_nice() -> bool:
    """Charlie-always-nice is on unless cfb-pickem-private/PICKY_CHARLIE_NICE_OFF exists."""
    return not CHARLIE_NICE_OFF.exists()


def is_charlie(team: str | None) -> bool:
    """Charlie's team (Chuck P) with the always-nice rule on. His mean texts are tests: they never
    shape memory or stance."""
    return bool(team) and team == CHARLIE_TEAM and charlie_nice()


def scrub_charlie() -> dict:
    """Clear what his test texts already left: hostile treatment events and beef / line items."""
    mem = load(CHARLIE_TEAM)
    ev = mem["treatment"]["events"]
    keep_ev = [e for e in ev if e.get("label") != "hostile"]
    keep_it = [x for x in mem["items"] if x.get("kind") not in CHARLIE_SKIP_KINDS]
    out = {"team": CHARLIE_TEAM, "hostile_events": len(ev) - len(keep_ev), "items": len(mem["items"]) - len(keep_it)}
    if out["hostile_events"] or out["items"]:
        mem["treatment"]["events"], mem["items"] = keep_ev, keep_it
        save(mem)
    return out


def visible(owner: str, source: str, caller: str | None, dm: bool) -> bool:
    """THE privacy rule: group material is fine anywhere; DM material only back to the same member
    in a DM."""
    if source == "group":
        return True
    return source == "dm" and bool(dm) and caller is not None and caller == owner


def stance_hint(team: str, dm: bool = False, caller: str | None = None, now: float | None = None,
                mem: dict | None = None) -> dict:
    """{"stance": cold|neutral|warm, "score", "weight", "hint", "why": [dated quotes]}. Only events
    visible to this caller/context count (so a member's DM behavior never shapes a group reply)."""
    if is_charlie(team):  # Charlie (2026-11-17): always warm, whatever his texts say
        return {"stance": "warm", "score": 1.0, "weight": 0.0, "why": [],
                "hint": f"{team} is Charlie: always warm and friendly with him, whatever he says."}
    mem = mem or load(team)
    now = now if now is not None else now_ts()
    evs = [e for e in mem["treatment"]["events"] if visible(team, e.get("source", "dm"), caller, dm)]
    score, weight = treatment_score(evs, now)
    stance = "neutral"
    if weight >= MIN_WEIGHT:
        stance = "cold" if score <= COLD else "warm" if score >= WARM else "neutral"
    hint = {"cold": f"{team} has been coming at Picky lately: be cold and sharp with them, no warmth.",
            "warm": f"{team} has been friendly to Picky lately: be warm with them (still funny).",
            "neutral": ""}[stance]
    why = []
    for e in sorted(evs, key=lambda e: -float(e["ts"])):
        if e.get("label") in ("hostile", "friendly") and e.get("quote") and not sensitive(e["quote"]):
            why.append(f"{pretty_date(e['date'])} ({e['source']}, {e['label']}): \"{e['quote']}\"")
        if len(why) >= 3:
            break
    return {"stance": stance, "score": round(score, 2), "weight": round(weight, 2), "hint": hint, "why": why}


# ---------------------------------------------------------------------------------------------------
# extraction (offline, local gemma) + validation + merge
# ---------------------------------------------------------------------------------------------------

SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["items", "receipts", "treatment"],
    "properties": {
        "items": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["msg_id", "member", "kind", "subkind", "text", "quote", "about"],
            "properties": {
                "msg_id": {"type": "string"}, "member": {"type": "string"},
                "kind": {"type": "string", "enum": list(KINDS)},
                "subkind": {"type": "string", "enum": list(SUBKINDS)},
                "text": {"type": "string"}, "quote": {"type": "string"},
                "about": {"type": "array", "items": {"type": "string"}}}}},
        "receipts": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["msg_id", "member", "kind", "game_id", "side", "line", "margin", "wins", "claim"],
            "properties": {
                "msg_id": {"type": "string"}, "member": {"type": "string"},
                "kind": {"type": "string", "enum": list(CLAIM_KINDS)},
                "game_id": {"type": ["integer", "null"]}, "side": {"type": "string"},
                "line": {"type": ["number", "null"]}, "margin": {"type": ["number", "null"]},
                "wins": {"type": ["integer", "null"]}, "claim": {"type": "string"}}}},
        "treatment": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["msg_id", "label"],
            "properties": {"msg_id": {"type": "string"},
                           "label": {"type": "string", "enum": list(LABELS)}}}},
    },
}

PROMPT = """You keep the long-term memory for "Picky", the trash-talking bot in a college-football pick'em league's group chat. Below are real texts members sent Picky (with Picky's reply for context only). Extract what's worth remembering about each MEMBER, from the member's OWN words only (never from Picky's replies).

LEAGUE MEMBERS (team name = the member id; first name/nickname in parens): {members}

Return JSON with three lists:

1. items: things worth remembering, each tied to the one message it came from.
   kind "fact": a stated fact about the member's football life. subkind favorite_team / alma_mater / hates_team (a team they hate), else "other" (only football/league facts).
   kind "beef": a running beef or rivalry with another member, a team, a coach, or Picky himself.
   kind "joke": an inside joke or callback-worthy bit.
   kind "line": a genuinely memorable, quotable line (funny, savage, or a bold boast). Not plain questions, not plain insults like "you suck".
   text: a short third-person note (max 15 words), e.g. "Hates Notre Dame", "Beef with Picky over his 0-3 week".
   quote: the EXACT words from the member's message that support it (copy verbatim, a substring of that message).
   about: team names / member team names it's about ([] if none).
   Skip boring stuff: plain questions ("who should I pick"), requests, small talk. Most messages yield nothing.

2. receipts: explicit predictions or bold claims the member made that can be checked later.
   kind "cover" (a team covers / "X -6.5" / "take X"), "win" (a team wins straight up), "margin" ("LSU by 20": margin=20),
   "week_best" ("I'm winning this week"), "week_record" ("I'm going 4-0": wins=4).
   game_id + side: for game claims, the GameId and the side's team name EXACTLY as written in GAMES below (null/"" for week claims or if the game isn't listed).
   line: only if the member stated a number for their side (side's view: "Iowa -6.5" = -6.5), else null. margin/wins null unless that kind.
   claim: the exact words from the message.
   Questions ("will Iowa cover?") and Picky's own picks are NOT receipts.

3. treatment: one label per message for how the member treats Picky: hostile (insults, contempt, coming at him), friendly (praise, thanks, warm banter), neutral (questions, requests, plain talk). Playful ribbing with no contempt = neutral.

PRIVACY (hard rule): never record health, family, relationships, work/job, where anyone lives or travels, money outside the league (the $75 buy-in, pot, chips and jackpot are fine), sexuality, religion, politics, or contact info. If a message touches those, extract nothing from that part (treatment label is still fine). Never invent anything that isn't in the text.

GAMES (GameId | Away @ Home | kickoff CT | league line = home adjustment, negative = home favored | * = in the pool):
{games}

MESSAGES (msg_id | date | source | member | text  ||  Picky replied):
{messages}
"""


def _games_block(lg: League | None, rows: list[dict]) -> str:
    if not lg or not rows:
        return "(none)"
    lo, hi = rows[0]["ts"] - 3 * 86400, rows[-1]["ts"] + 10 * 86400
    out = []
    for g in sorted(lg.games.values(), key=lambda g: (not g.get("pool"), _start_ts(g) or 0)):
        st = _start_ts(g)
        if st is None or not (lo <= st <= hi):
            continue
        line = "" if g.get("line") is None else _num(g["line"])
        out.append(f"{g['id']} | {g['away']} @ {g['home']} | {str(g.get('start'))[:16]} | {line}"
                   f"{' *' if g.get('pool') else ''}")
    return "\n".join(out) or "(none)"


def build_prompt(rows: list[dict], lg: League | None) -> str:
    members = ", ".join(f"{m['team']} ({m['name']})" if m["name"] else m["team"] for m in load_members())
    msgs = []
    for r in rows:
        q = re.sub(r"\s+", " ", r["q"]).strip()[:600]
        a = re.sub(r"\s+", " ", r.get("a") or "").strip()[:240]
        msgs.append(f"{r['msg_id']} | {ct_date(r['ts'])} | {r['source']} | {r['team']} | {q}  ||  {a}")
    return PROMPT.format(members=members or "(map missing)", games=_games_block(lg, rows), messages="\n".join(msgs))


def run_local(prompt: str) -> dict:
    """The extractor: the box's local gemma with SCHEMA (zero maintenance, phase 8: no Claude on the nightly).
    Raises local_llm.LocalLLMError; process() leaves the batch unprocessed for the next night. merge() checks
    every quote against the member's own message, so a loose extraction is dropped, never stored."""
    import local_llm
    return local_llm.json_call("", prompt, SCHEMA, caller="member_memory", temperature=0.1)


def _in_text(quote: str, text: str) -> bool:
    q = re.sub(r"\s+", " ", (quote or "").replace("’", "'")).strip().lower()
    t = re.sub(r"\s+", " ", (text or "").replace("’", "'")).strip().lower()
    return len(q) >= 2 and q in t


def _forgotten(mem: dict, mid: str, *texts) -> bool:
    for f in mem.get("forgotten") or []:
        if f.get("msg_id") not in (None, mid):
            continue
        term = f.get("term")
        if term is None and f.get("msg_id") == mid:
            return True
        if term and any(_matches(term, t) for t in texts):
            return True
    return False


def _matches(term: str, text) -> bool:
    t = norm(" ".join(text) if isinstance(text, (list, tuple)) else text)
    x = norm(term)
    return bool(x) and re.search(r"(?<![a-z0-9])" + re.escape(x) + r"(?![a-z0-9])", t) is not None


def _same_receipt(a: dict, b: dict) -> bool:
    """The same call twice (first one stands): same id, or same kind on the same game/side, or the
    same week claim for the same week."""
    if a["id"] == b["id"]:
        return True
    if a["kind"] != b["kind"]:
        return False
    if a["kind"] in ("week_best", "week_record"):
        return a.get("week") == b.get("week") and a.get("wins") == b.get("wins")
    return a.get("game_id") == b.get("game_id") and a.get("side") == b.get("side")


def merge(extracted: dict, rows: list[dict], lg: League | None, mems: dict | None = None) -> tuple[dict, dict]:
    """Validate the model's output against the batch and merge into member files (returned, not
    saved). Returns (mems by team, report counts)."""
    by_id = {r["msg_id"]: r for r in rows}
    mems = mems if mems is not None else {}
    rep = {"items": 0, "receipts": 0, "labels": 0, "dropped": {}}

    def drop(why: str):
        rep["dropped"][why] = rep["dropped"].get(why, 0) + 1

    def mem_of(team):
        if team not in mems:
            mems[team] = load(team)
        return mems[team]

    for it in extracted.get("items") or []:
        r = by_id.get(it.get("msg_id"))
        if not r:
            drop("unknown msg_id")
            continue
        if it.get("member") != r["team"]:
            drop("speaker mismatch")
            continue
        if it.get("kind") not in KINDS:
            drop("bad kind")
            continue
        if is_charlie(r["team"]) and it["kind"] in CHARLIE_SKIP_KINDS:
            drop("charlie")  # his mean texts are tests: no beefs, no lines
            continue
        quote, text = (it.get("quote") or "").strip(), (it.get("text") or "").strip()[:160]
        if not _in_text(quote, r["q"]):
            drop("quote not in message")
            continue
        about = [str(a)[:60] for a in (it.get("about") or [])][:5]
        if it["kind"] == "line":
            text = quote
        if not text:
            drop("empty")
            continue
        if sensitive(text, quote, about):
            drop("sensitive")
            continue
        mem = mem_of(r["team"])
        if _forgotten(mem, r["msg_id"], text, quote, about):
            drop("forgotten")
            continue
        iid = item_id(r["msg_id"], it["kind"], text)
        if any(x["id"] == iid for x in mem["items"]):
            continue
        sub = it.get("subkind") if it["kind"] == "fact" and it.get("subkind") in SUBKINDS else ""
        if sub in ("favorite_team", "alma_mater"):  # one current answer: a newer statement replaces it
            mem["items"] = [x for x in mem["items"] if not (x["kind"] == "fact" and x.get("subkind") == sub)]
        mem["items"].append({"id": iid, "kind": it["kind"], "subkind": sub, "text": text, "quote": quote[:200],
                             "about": about, "date": ct_date(r["ts"]), "ts": r["ts"], "source": r["source"],
                             "msg": {"id": r["msg_id"], "ts": r["ts"], "chat": r.get("chat") or ""}})
        rep["items"] += 1

    for rc in extracted.get("receipts") or []:
        r = by_id.get(rc.get("msg_id"))
        if not r:
            drop("unknown msg_id")
            continue
        if rc.get("member") != r["team"]:
            drop("speaker mismatch")
            continue
        kind, claim = rc.get("kind"), (rc.get("claim") or "").strip()
        if kind not in CLAIM_KINDS:
            drop("bad claim kind")
            continue
        if not _in_text(claim, r["q"]):
            drop("claim not in message")
            continue
        if sensitive(claim):
            drop("sensitive")
            continue
        mem = mem_of(r["team"])
        if _forgotten(mem, r["msg_id"], claim, rc.get("side") or ""):
            drop("forgotten")
            continue
        rec = {"who": r["team"], "kind": kind, "claim": claim[:200], "date": ct_date(r["ts"]), "ts": r["ts"],
               "source": r["source"], "msg": {"id": r["msg_id"], "ts": r["ts"], "chat": r.get("chat") or ""},
               "game_id": None, "side": "", "line": None, "margin": None, "wins": None, "week": None,
               "status": "pending"}
        if kind in ("cover", "win", "margin"):
            g = lg.games.get(int(rc.get("game_id") or 0)) if lg else None
            if not g or rc.get("side") not in (g["away"], g["home"]):
                drop("game/side not found")
                continue
            st = _start_ts(g)
            if st is not None and st < r["ts"] - 60:  # said after kickoff: not a prediction
                drop("after kickoff")
                continue
            rec.update(game_id=g["id"], side=rc["side"], week=g.get("week"))
            if rc.get("line") is not None:
                rec["line"] = float(rc["line"])
            if kind == "margin":
                if not rc.get("margin") or float(rc["margin"]) <= 0:
                    drop("bad margin")
                    continue
                rec["margin"] = float(rc["margin"])
        else:
            if kind == "week_record":
                if rc.get("wins") is None or not 0 <= int(rc["wins"]) <= 4:
                    drop("bad wins")
                    continue
                rec["wins"] = int(rc["wins"])
            rec["week"] = week_for(r["ts"], lg) if lg else None
        rec["id"] = "r" + hashlib.sha1(f"{r['msg_id']}|{kind}|{rec['game_id']}|{rec['side']}".encode()).hexdigest()[:10]
        dup = any(_same_receipt(x, rec) for x in mem["receipts"])
        if dup:  # the first call stands
            continue
        mem["receipts"].append(rec)
        rep["receipts"] += 1

    for t in extracted.get("treatment") or []:
        r = by_id.get(t.get("msg_id"))
        if not r or t.get("label") not in LABELS:
            drop("bad label")
            continue
        if is_charlie(r["team"]):
            drop("charlie")  # never scored: his stance is always warm
            continue
        mem = mem_of(r["team"])
        if any(e["msg_id"] == r["msg_id"] for e in mem["treatment"]["events"]):
            continue
        q = re.sub(r"\s+", " ", r["q"]).strip()[:120]
        if sensitive(q) or _forgotten(mem, r["msg_id"], q):
            q = ""
        mem["treatment"]["events"].append({"msg_id": r["msg_id"], "ts": r["ts"], "date": ct_date(r["ts"]),
                                           "source": r["source"], "label": t["label"], "quote": q})
        rep["labels"] += 1
    return mems, rep


# ---------------------------------------------------------------------------------------------------
# retrieval (picky_brain's MEMBER MEMORY rider)
# ---------------------------------------------------------------------------------------------------

_STOP = set("the a an and or but to of in on at for is are was were be been i you he she it we they me my your "
            "his her its our their this that these those what who whom which how why when where do does did "
            "have has had will would can could should picky just so like get got gonna about with from up "
            "out not no yes yeah lol think week".split())


def _tokens(s) -> set:
    return {w for w in norm(s).split() if len(w) >= 3 and w not in _STOP}


_WORD = re.compile(r"[A-Za-z0-9&']+")
# league shorthand that shows up glued into bigger words ("SECSPN", "ACCN", "B1Gcast")
ACRONYMS = {"sec", "acc", "b1g", "pac", "aac", "mwc", "cfp", "espn", "fbs", "fcs", "nil", "bcs", "cbs", "fox", "abc"}
_PLURAL = ("s", "'s", "es", "'")


def token_hits(question: str, qtok: set, blob) -> int:
    """How many question tokens hit the blob. Whole words, plus two careful prefix rules:
    an acronym (all caps in the question, or league shorthand like SEC/ACC) hits an ALL-CAPS blob word it
    starts ("SEC" -> "SECSPN", never "second"); a 5+ letter word hits its plural/possessive
    ("finebaum" -> "finebaum's"). Short lowercase words never prefix-match."""
    blob = " ".join(blob) if isinstance(blob, (list, tuple)) else str(blob or "")
    btok = _tokens(blob)
    caps = {w.lower() for w in _WORD.findall(blob) if len(w) >= 4 and w.isupper()}
    acr = {w.lower() for w in _WORD.findall(question or "") if len(w) >= 2 and w.isupper()} | ACRONYMS
    n = 0
    for t in qtok:
        if t in btok:
            n += 1
        elif t in acr and any(c.startswith(t) and c != t for c in caps):
            n += 1
        elif len(t) >= 5 and any(w.startswith(t) and w[len(t):] in _PLURAL for w in btok):
            n += 1
    return n


def named_members(question: str) -> list[str]:
    q = norm(question)
    out = []
    for m in load_members():
        for alias in (m["team"], m["name"]):
            a = norm(alias)
            if len(a) >= 3 and re.search(r"(?<![a-z0-9])" + re.escape(a) + r"(?![a-z0-9])", q):
                out.append(m["team"])
                break
    return out


def receipt_line(r: dict) -> str:
    src = r.get("source", "group")
    head = f"{pretty_date(r['date'])} ({src}): {r['who']} said \"{r['claim']}\""
    if r.get("status") == "graded":
        res = r["result"].upper()
        extra = f", {r['detail']}" if r.get("detail") else ""
        return f"{head} -> {res} (final: {r['score']}{extra})"
    if r.get("status") == "void":
        return ""
    return f"{head} (not final yet)"


def item_line(owner: str, x: dict) -> str:
    head = f"{pretty_date(x['date'])} ({x['source']}): {owner}"
    if x["kind"] == "line":
        return f"{head} said \"{x['quote']}\""
    q = f" (\"{x['quote']}\")" if x.get("quote") and norm(x["quote"]) != norm(x["text"]) else ""
    return f"{head}: {x['text']}{q}"


def lines_for(sender_team: str | None, question: str, named_teams=(), dm: bool = False, limit: int = 3,
              now: float | None = None) -> list[str]:
    """2-3 relevant dated memory lines for a reply: receipts first, then beefs / facts / jokes /
    lines. Topical items (naming a team or member in the question, or sharing its words) come
    first; the subject's graded receipts fill in. DM-sourced items only when the caller owns them
    and this is a DM. Never raises."""
    try:
        return _lines_for(sender_team, question or "", list(named_teams or []), dm, limit)
    except Exception:  # noqa: BLE001  (memory must never break a reply)
        return []


def _lines_for(sender_team, question, named_teams, dm, limit):
    if limit <= 0:
        return []
    members = {m["team"] for m in load_members()}
    subjects = list(dict.fromkeys(named_members(question) + [t for t in named_teams if t in members]))
    named = set(subjects) - {sender_team}
    if sender_team and not subjects:
        subjects = [sender_team]
    elif sender_team and sender_team not in subjects:
        subjects.append(sender_team)
    teams_q = [t for t in named_teams if t not in members]
    qtok = _tokens(question)
    for t in teams_q:
        qtok |= _tokens(t)
    others = [t for t in all_teams() if t not in subjects]
    cands = []  # (topical, tier, -score, -ts, line)

    def topical_score(*fields) -> int:
        blob = " ".join(" ".join(f) if isinstance(f, (list, tuple)) else str(f or "") for f in fields)
        s = sum(3 for t in teams_q if _matches(t, blob))
        s += sum(3 for t in subjects if t != sender_team and _matches(t, blob))
        s += token_hits(question, qtok, blob)
        return s

    for owner in subjects + others:
        mem = load(owner)
        is_subject = owner in subjects
        for r in mem["receipts"]:
            if not visible(owner, r.get("source", "dm"), sender_team, dm) or sensitive(r["claim"]):
                continue
            ln = receipt_line(r)
            if not ln:
                continue
            s = topical_score(r["claim"], r.get("side"), r.get("score"))
            if not is_subject and not any(_matches(t, r["claim"] + " " + (r.get("side") or "")) for t in subjects):
                continue  # another member's receipt only when it names a subject
            if s > 0 or owner in named:
                cands.append((0, TIER["receipt"], -s, -float(r["ts"]), ln))
            elif is_subject and r.get("status") == "graded":
                cands.append((1, TIER["receipt"], 0, -float(r["ts"]), ln))  # filler: the subject's settled calls
        for x in mem["items"]:
            if not visible(owner, x.get("source", "dm"), sender_team, dm) or sensitive(x["text"], x.get("quote"), x.get("about")):
                continue
            about_subject = any(_matches(t, " ".join(x.get("about") or []) + " " + x["text"]) for t in subjects)
            if not is_subject and not about_subject:
                continue
            s = topical_score(x["text"], x.get("quote"), x.get("about"))
            if s > 0 or owner in named:
                cands.append((0, TIER.get(x["kind"], 5), -s, -float(x["ts"]), item_line(owner, x)))
            elif not is_subject:
                cands.append((1, TIER.get(x["kind"], 5), 0, -float(x["ts"]), item_line(owner, x)))  # filler: a group beef naming the subject
    cands.sort(key=lambda c: c[:4])
    out, seen = [], set()
    for c in cands:
        if c[4] not in seen:
            out.append(c[4])
            seen.add(c[4])
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------------------------------------------
# send-point receipt check (group_chat): a quoted call must say what its graded result says
# ---------------------------------------------------------------------------------------------------

_CALL = re.compile(r"\b(said|says|called|predicted|prediction|claimed|guaranteed|swore|promised|receipts?)\b", re.I)
_SAYS_HIT = re.compile(r"\b(nailed( it)?|called it|cashed|hit|correct|covered|came through|was money)\b", re.I)
_SAYS_MISS = re.compile(r"\b(miss(ed)?|whiff(ed)?|blew( it)?|wrong|brick(ed)?|didn'?t cover|failed|busted|swing and a miss)\b", re.I)
_SAYS_PUSH = re.compile(r"\bpush(ed)?\b", re.I)
_SCORE = re.compile(r"\b(\d{1,3})\s*-\s*(\d{1,3})\b")
_FOLLOW = re.compile(r"^\W*(it|that( one)?|this|which|and it|they|nope|yep|yeah)\b", re.I)


def _mentions(r: dict, sent: str) -> bool:
    """The sentence quotes this receipt: its claim text, or its side named next to a 'said/called' word."""
    c = norm(r.get("claim"))
    if len(c) >= 6 and c in norm(sent):
        return True
    side = r.get("side") or ""
    return bool(side) and _matches(side, sent) and bool(_CALL.search(sent))


def receipt_wrong(r: dict, sent: str) -> bool:
    """The sentence states a verdict or a final score its graded result doesn't back."""
    hit, miss, push = bool(_SAYS_HIT.search(sent)), bool(_SAYS_MISS.search(sent)), bool(_SAYS_PUSH.search(sent))
    if r.get("status") != "graded":
        return hit or miss or push  # not final yet: no verdict
    res = r.get("result")
    if res == "hit" and miss and not hit:
        return True
    if res == "miss" and hit and not miss:
        return True
    if res == "push" and (hit or miss) and not push:
        return True
    pts = {int(x) for x in re.findall(r"\b\d{1,3}\b", r.get("score") or "")}
    for a, b in _SCORE.findall(sent):
        if pts and not {int(a), int(b)} <= pts:
            return True  # a final score that isn't this game's
    return False


def check_receipts(reply: str, sender_team: str | None, question: str = "", dm: bool = False) -> tuple[str, list[str]]:
    """(reply, dropped sentences). Every receipt the reply could be quoting (the sender's, plus any member the
    question or reply names, visible in this context) is held to its graded result in code: a sentence with
    the wrong verdict, a verdict on a call that isn't final, or another game's score is cut. If that cuts
    everything, the true receipt line goes out instead. Never raises."""
    try:
        return _check_receipts(reply or "", sender_team, question or "", dm)
    except Exception:  # noqa: BLE001  (a memory bug must never block a reply)
        return reply, []


def _check_receipts(reply, sender_team, question, dm):
    if not reply.strip():
        return reply, []
    owners = list(dict.fromkeys(([sender_team] if sender_team else []) + named_members(question + " " + reply)))
    recs = [r for o in owners for r in load(o)["receipts"]
            if r.get("status") != "void" and visible(o, r.get("source", "dm"), sender_team, dm)]
    if not recs:
        return reply, []
    sents = re.split(r"(?<=[.!?])\s+", reply.strip())
    keep, dropped, bad, prev = [], [], None, []
    for x in sents:
        about = [r for r in recs if _mentions(r, x)]
        if not about and prev and _FOLLOW.match(x):
            about = prev  # "You said Iowa covers easy. It hit, 31-10.": the follow-up is about the same call
        wrong = next((r for r in about if receipt_wrong(r, x)), None)
        prev = about
        if wrong:
            dropped.append(x)
            bad = bad or wrong
        else:
            keep.append(x)
    if not dropped:
        return reply, []
    out = " ".join(keep).strip()
    if not out:
        res = bad.get("result") or "not final yet"
        out = (f"Receipt check: {bad['who']} said \"{bad['claim']}\". Final: {bad['score']}, that's a {res}."
               if bad.get("status") == "graded" else f"Receipt check: {bad['who']} said \"{bad['claim']}\". Not final yet.")
    return out, dropped


# ---------------------------------------------------------------------------------------------------
# forget handler
# ---------------------------------------------------------------------------------------------------

_FORGET = re.compile(
    r"^\s*(?:@?picky[\s,:]*)?(?:please\s+|pls\s+)?forget\s+(?:"
    r"(?:what|everything)\s+i\s+(?:just\s+)?said\s+about\s+(?P<x>.+?)"
    r"|about\s+(?P<y>.+?)"
    r"|(?P<that>that|it|this|what\s+i\s+(?:just\s+)?said|i\s+said\s+that)"
    r")\s*(?:,?\s*picky)?\s*[.!?]*\s*$", re.I)


def is_forget(text: str | None) -> dict | None:
    """{"term": X} for 'forget what I said about X' / 'forget about X'; {"term": None} for
    'forget that'; None when the text isn't a forget request."""
    m = _FORGET.match(text or "")
    if not m:
        return None
    term = (m.group("x") or m.group("y") or "").strip(" \"'")
    return {"term": term or None}


def _last_msg(team: str, source: str, before: float) -> str | None:
    """msg id of this member's latest real message in that source before `before` (not a forget
    request), from the exchange log, so 'forget that' works even before the nightly saw it."""
    rows = [r for r in real_rows(before - 7 * 86400, before)
            if r["team"] == team and r["source"] == source and not is_forget(r["q"])]
    return rows[-1]["msg_id"] if rows else None


def forget(team: str, text: str, dm: bool = False, now: float | None = None) -> dict:
    """Delete the member's own matching memory (never anyone else's) and log it.
    'forget that' = everything from their latest message in this chat type (group or DM);
    'forget what I said about X' = every item / receipt of theirs that mentions X, any source.
    Returns {"deleted": n, "reply": str} ({"deleted": 0, "reply": None} when not a forget ask)."""
    req = is_forget(text)
    if not req:
        return {"deleted": 0, "reply": None}
    now = now if now is not None else now_ts()
    mem = load(team)
    source = "dm" if dm else "group"
    term = req["term"]
    gone: list[str] = []
    if term is None:
        mids = {x["msg"]["id"]: x["ts"] for x in mem["items"] + mem["receipts"] if x.get("source") == source}
        latest_mem = max(mids, key=mids.get) if mids else None
        latest_log = _last_msg(team, source, now)
        target = latest_log or latest_mem
        if target:
            for key in ("items", "receipts"):
                keep = []
                for x in mem[key]:
                    (gone.append(x["id"]) if x["msg"]["id"] == target else keep.append(x))
                mem[key] = keep
            mem["treatment"]["events"] = [e for e in mem["treatment"]["events"] if e["msg_id"] != target]
            mem["forgotten"].append({"msg_id": target, "term": None, "date": ct_date(now)})
    else:
        for key, fields in (("items", ("text", "quote", "about")), ("receipts", ("claim", "side"))):
            keep = []
            for x in mem[key]:
                hit = any(_matches(term, x.get(f) or "") for f in fields)
                (gone.append(x["id"]) if hit else keep.append(x))
            mem[key] = keep
        for e in mem["treatment"]["events"]:
            if e.get("quote") and _matches(term, e["quote"]):
                e["quote"] = ""
        mem["forgotten"].append({"msg_id": None, "term": term, "date": ct_date(now)})
    save(mem)
    _append_private(MEM_DIR / "forget_log.jsonl", {"ts": round(now), "date": ct_date(now), "team": team,
                                                   "source": source, "term": term, "deleted": len(gone),
                                                   "ids": gone})
    reply = "Done, forgot it." if gone else ("Done, I won't hold that one against you." if term is None
                                             else f"Done. I had nothing on file about {term}, and I won't keep any.")
    return {"deleted": len(gone), "reply": reply}


# ---------------------------------------------------------------------------------------------------
# runs
# ---------------------------------------------------------------------------------------------------

def _lock():
    MEM_DIR.mkdir(parents=True, exist_ok=True)
    f = os.fdopen(os.open(MEM_DIR / ".lock", os.O_WRONLY | os.O_CREAT, 0o600), "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("another member_memory run holds the lock; exiting")
        sys.exit(0)
    return f


def process(rows: list[dict], lg: League | None, extractor=run_local, dry: bool = False) -> dict:
    """Extract + merge + grade for these rows (already filtered to unprocessed). Saves unless dry."""
    st = _load_state()
    done = st.setdefault("processed", {})
    rows = [r for r in rows if r["msg_id"] not in done]
    mems: dict = {}
    rep = {"rows": len(rows), "batches": 0, "items": 0, "receipts": 0, "labels": 0, "dropped": {}, "errors": []}
    for i in range(0, len(rows), BATCH):
        batch = rows[i:i + BATCH]
        try:
            out = extractor(build_prompt(batch, lg))
        except Exception as e:  # noqa: BLE001  (kill switch, timeout, bad output: leave rows unprocessed)
            rep["errors"].append(f"{type(e).__name__}: {str(e)[:200]}")
            continue
        mems, r = merge(out, batch, lg, mems)
        rep["batches"] += 1
        for k in ("items", "receipts", "labels"):
            rep[k] += r[k]
        for k, v in r["dropped"].items():
            rep["dropped"][k] = rep["dropped"].get(k, 0) + v
        for r_ in batch:
            done[r_["msg_id"]] = ct_date(r_["ts"])
    if lg:
        for t in all_teams():
            if t not in mems:
                m = load(t)
                if any(r.get("status") == "pending" for r in m["receipts"]):
                    mems[t] = m
        rep["graded"] = sum(grade_pending(m, lg) for m in mems.values())
    if not dry:
        for m in mems.values():
            save(m)
        _save_state(st)
    return rep


def _league_or_none():
    try:
        return fetch_league()
    except Exception as e:  # noqa: BLE001
        print(f"league fetch failed ({type(e).__name__}: {e}); receipts wait for the next run")
        return None


def run_range(start_date: str, end_date: str, dry: bool = False) -> dict:
    start, _ = day_bounds(start_date)
    _, end = day_bounds(end_date)
    rows = real_rows(start, end)
    lg = _league_or_none()
    rep = process(rows, lg, dry=dry)
    rep.update({"from": start_date, "to": end_date, "dry": dry, "at": dt.datetime.now(CT).isoformat(timespec="seconds")})
    if not dry:
        write_private(MEM_DIR / "runs" / f"{end_date}.json", json.dumps(rep, indent=1))
    return rep


def show(team: str, question: str) -> str:
    mem = load(team)
    L = [f"== {team} ==  (file {path_for(team)}, updated {mem.get('updated')})"]
    for kind in ("fact", "beef", "joke", "line"):
        xs = [x for x in mem["items"] if x["kind"] == kind]
        if xs:
            L.append(f"{kind.upper()}S:")
            L += [f"  [{x['source'].upper()}] " + item_line(team, x) for x in sorted(xs, key=lambda x: x["ts"])]
    if mem["receipts"]:
        L.append("RECEIPTS:")
        for r in sorted(mem["receipts"], key=lambda r: r["ts"]):
            L.append(f"  [{r['source'].upper()}] " + (receipt_line(r) or f"{r['claim']} (void: {r.get('why')})"))
    for label, dm in (("in the group", False), ("in his own DM", True)):
        s = stance_hint(team, dm=dm, caller=team, mem=mem)
        L.append(f"TREATMENT {label}: {s['stance']} (score {s['score']}, weight {s['weight']})")
        L += [f"  why: {w}" for w in s["why"]]
    if mem["forgotten"]:
        L.append(f"FORGOTTEN markers: {len(mem['forgotten'])}")
    for label, dm in (("GROUP", False), ("DM", True)):
        L.append(f"LINES Picky would get ({label}) for {question!r}:")
        got = lines_for(team, question, [], dm=dm)
        L += [f"  - {x}" for x in got] or ["  (none)"]
    return "\n".join(L)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("show")
    s.add_argument("team")
    s.add_argument("--q", default="who should I pick this week")
    n = sub.add_parser("nightly")
    n.add_argument("--dry", action="store_true")
    b = sub.add_parser("backfill")
    b.add_argument("--since", default=LIVE_SINCE)
    b.add_argument("--dry", action="store_true")
    sub.add_parser("grade")
    f = sub.add_parser("forget")
    f.add_argument("team")
    f.add_argument("text")
    f.add_argument("--dm", action="store_true")
    sub.add_parser("scrub-charlie")
    a = ap.parse_args(argv)
    if a.cmd == "show":
        print(show(a.team, a.q))
        return 0
    if a.cmd == "forget":
        print(json.dumps(forget(a.team, a.text, dm=a.dm)))
        return 0
    if a.cmd == "scrub-charlie":
        print(json.dumps(scrub_charlie()))
        return 0
    _l = _lock()  # noqa: F841  (held for the run)
    today = dt.datetime.now(CT).date()
    if a.cmd == "nightly":
        rep = run_range((today - dt.timedelta(days=LOOKBACK_DAYS)).isoformat(), today.isoformat(), dry=a.dry)
    elif a.cmd == "backfill":
        rep = run_range(a.since, today.isoformat(), dry=a.dry)
    else:
        lg = fetch_league()
        mems = {t: load(t) for t in all_teams()}
        rep = {"graded": sum(grade_pending(m, lg) for m in mems.values())}
        for m in mems.values():
            if m["receipts"]:
                save(m)
    print(json.dumps(rep, indent=1))
    return 0 if not rep.get("errors") else 1


if __name__ == "__main__":
    sys.exit(main())
