"""
Picky's callbacks (plan W6 + move 8): the code-computed slice of member memory.
Pure code, no model, nothing invented: every line is built from a logged
event plus the league's own graded results.

  SHOTS LOG. Who took a shot at Picky, when, in which chat, and how their next
  graded week went against the number. "you said that Saturday too, then went
  1-3 against the number in week 11." Only a week they didn't win (never mock a
  good number).

  PREDICTION CALLBACKS. Each lean Picky gives a member ("I'd take Boise -13.5",
  "Boise -13.5 is a trap") is pulled out of his sent reply against the current
  slate and logged (game, side, pool line, chat). Once the game is final it's
  graded ATS the way the league grades it (`winner` is the side that covered;
  a dead-even score goes to the away side), with the cover margin:
  "told you Boise State -13.5 was a trap, missed the cover by 3." or he owns
  the miss.

  PRIVACY. Every item stores the chat it came from. Group material is callable
  anywhere (the group, or that member's own DM); DM material only in that same
  DM. A lean is only ever surfaced after its game is final, and a member's own
  pick only on a final game (revealed since the week locked at first kickoff),
  so no unrevealed pick can come out of here.

  COOLDOWN. A callback item is used once (COOLDOWN_DAYS); if server/no_repeat.py
  is present its Ledger also vetoes a line too close to one already sent.

  POLICY. callback_for(..., banter=True/False) is the per-reply call: at most
  one callback, forced on roughly every 4th banter reply in a chat where one
  exists (PICKY_CALLBACK_EVERY, counter kept in the state file), and always on
  an explicit recall ask ("remember what you said about Boise?"). Without
  `banter` it just offers the best one.

  BURN SHEET. burn_sheet(team): the roastable, true lines from the lore cache
  (blown chalk, winless weeks, miss streaks, bad h2h, stats-page oddities);
  league data, so callable anywhere. The burn-book lane can use it too.

  RIDER (exam flag, PICKY_CALLBACK_RIDER=1). group_chat sets CHAT per request;
  picky_brain asks rider(q, intent) after its brief and puts the line in the
  draft prompt. Off by default: the router lane wires callbacks in for real.

Interface (BUILD-COMMON.md): log_shot, log_lean, after_reply, callback_for.
`hook()` is what group_chat.py calls after a reply goes out: it runs
after_reply in a daemon thread and swallows every error, so logging can never
delay or break a reply.

State: a private 0600 JSON file in cfb-pickem-private (never this public repo);
PICKY_CALLBACKS_STATE overrides it (tests, side sidecars). With no override,
only the live group sidecar (default port, no exam flag) writes, so side
sidecars and exams never pollute the live log.
"""

from __future__ import annotations

import contextvars
import datetime as dt
import json
import logging
import os
import re
import subprocess
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

log = logging.getLogger("picky-callbacks")

PRIVATE = Path("/Users/Agent/cfb-pickem-private")
DEFAULT_STATE = PRIVATE / "picky_callbacks.json"
REST = "https://vopdioszofwdkwnujtiq.supabase.co/rest/v1"
CT = ZoneInfo("America/Chicago")

KEEP_DAYS = 35          # shots and leans older than this drop out of the file
COOLDOWN_DAYS = 60      # a callback item is used once (effectively)
LEAN_FRESH_DAYS = 10    # a graded lean is callable for this long after its kickoff
SHOT_FRESH_DAYS = 21    # a shot is callable for this long after it was taken
GRADE_EVERY = 600.0     # seconds between background grading passes kicked by callback_for
GAMES_TTL = 300.0       # seconds the league snapshot is cached
GAME_LEN = 4 * 3600     # a game kicked off this long ago is treated as over (shot outcome week)
BANTER_INTENTS = ("banter", "insult", "reaction")  # picky_brain's brief intents that count as banter
LORE_FILE = Path(__file__).resolve().parent / "cache" / "lore.json"


def force_every() -> int:
    """Force a callback on every Nth banter reply in a chat (where one exists)."""
    try:
        return max(1, int(os.environ.get("PICKY_CALLBACK_EVERY") or 4))
    except ValueError:
        return 4

_LOCK = threading.RLock()


def state_path() -> Path:
    return Path(os.environ.get("PICKY_CALLBACKS_STATE") or DEFAULT_STATE)


def enabled() -> bool:
    """Writes only with an explicit state file, or from the live group sidecar."""
    if os.environ.get("PICKY_CALLBACKS_STATE"):
        return True
    if os.environ.get("PICKY_GROUP_EXAM") == "1":
        return False
    return os.environ.get("PICKY_GROUP_PORT", "8768") == "8768"


# -----------------------------------------------------------------------------
# state file
# -----------------------------------------------------------------------------

def _empty() -> dict:
    return {"shots": [], "leans": [], "used": {}, "graded_at": 0, "policy": {}}


def _load() -> dict:
    try:
        d = json.loads(state_path().read_text())
    except (OSError, ValueError):
        return _empty()
    for k, v in _empty().items():
        d.setdefault(k, v)
    return d


def _save(d: dict) -> None:
    now = time.time()
    d["shots"] = [s for s in d["shots"] if now - s.get("ts", 0) < KEEP_DAYS * 86400]
    d["leans"] = [x for x in d["leans"] if now - x.get("ts", 0) < KEEP_DAYS * 86400]
    d["used"] = {k: t for k, t in d["used"].items() if now - t < COOLDOWN_DAYS * 86400}
    p = state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(d, f)
    os.replace(tmp, p)


def scope(chat_id: str, is_dm: bool) -> str | None:
    """'group:<guid>' / 'dm:<guid>'. A DM with no chat id can't be scoped, so nothing from it is kept."""
    chat_id = str(chat_id or "")
    if is_dm:
        return "dm:" + chat_id if chat_id else None
    return "group:" + chat_id


def _visible(item: dict, here: str) -> bool:
    """Group material anywhere; DM material only in that same DM."""
    return item["scope"].startswith("group:") or item["scope"] == here


# -----------------------------------------------------------------------------
# team names (a port of js/utils.js norm/sameTeam, like chat.py and lore.py)
# -----------------------------------------------------------------------------

def _fold(s) -> str:
    """Lowercase, accents off, same length as the input (so match spans line up)."""
    return "".join((unicodedata.normalize("NFD", ch)[:1] or ch).lower()[:1] or " " for ch in str(s or ""))


def norm(v) -> str:
    s = _fold(v).replace("&", " and ").replace(".", "")
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    return re.sub(r"\bst\b", "state", re.sub(r"\s+", " ", s).strip())


def same(a, b) -> bool:
    return bool(a) and bool(b) and norm(a) == norm(b)


def _num(x: float) -> str:
    return str(int(x)) if float(x) == int(x) else str(float(x))


def side_label(side: str, g: dict) -> str:
    """'Boise State -13.5' / 'Oregon State +13.5' on the POOL line (`line` is the home line)."""
    ln = float(g["line"])
    if ln == 0:
        return f"{side} (pick'em)"
    sign = "-" if same(side, g["home"]) == (ln < 0) else "+"
    return f"{side} {sign}{_num(abs(ln))}"


def side_line(side: str, g: dict) -> float:
    ln = float(g["line"])
    return ln if same(side, g["home"]) else -ln


def other_side(side: str, g: dict) -> str:
    return g["away"] if same(side, g["home"]) else g["home"]


# -----------------------------------------------------------------------------
# league snapshot (service-key GETs, cached; tests swap SOURCE)
# -----------------------------------------------------------------------------

@dataclass
class League:
    """games: normalized pool games {id, week, away, home, line, winner, away_pts, home_pts,
    status, start, tiebreaker}. picks: {game_id: {team_name: {"pick", "jackpot"}}}, FINAL games only."""
    games: list[dict]
    picks: dict = field(default_factory=dict)

    def game(self, gid) -> dict | None:
        return next((g for g in self.games if str(g["id"]) == str(gid)), None)

    def pick(self, gid, team: str) -> dict | None:
        g = self.game(gid)
        if not g or not g.get("winner"):
            return None  # privacy: never a pick on a game that isn't final
        return (self.picks.get(g["id"]) or self.picks.get(str(g["id"])) or {}).get(team)


def kickoff(g: dict) -> float | None:
    s = g.get("start")
    try:
        t = dt.datetime.fromisoformat(str(s))
    except (TypeError, ValueError):
        return None
    return (t.replace(tzinfo=CT) if t.tzinfo is None else t).timestamp()


def _service_key() -> str:
    return subprocess.check_output(["security", "find-generic-password", "-a", "drake", "-s",
                                    "CFB_PICKEM__SUPABASE_SERVICE_KEY", "-w"], text=True, timeout=10).strip()


_cache: dict = {"at": 0.0, "games": None, "picks": {}}


def _supabase_league(need: set) -> League:
    import httpx  # the sidecar's venv has it; imported here so the pure parts load anywhere

    key = _service_key()
    h = {"apikey": key, "Authorization": f"Bearer {key}"}
    with httpx.Client(timeout=20, headers=h) as c:
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

        if _cache["games"] is None or time.time() - _cache["at"] > GAMES_TTL:
            r = c.get(f"{REST}/all_games?select=cfb_season&order=cfb_season.desc&limit=1")
            r.raise_for_status()
            season = r.json()[0]["cfb_season"]
            rows = get_all('all_games?select=GameId,Away,Home,AwayPts,HomePts,line,winner,Status,week,tiebreaker,'
                           f'"Start (CT)"&cfb_season=eq.{season}&picked=eq.true&order=GameId.asc')
            _cache["games"] = [{"id": r["GameId"], "week": r.get("week"), "away": r["Away"], "home": r["Home"],
                                "line": r.get("line"), "winner": r.get("winner"), "away_pts": r.get("AwayPts"),
                                "home_pts": r.get("HomePts"), "status": r.get("Status"),
                                "start": r.get("Start (CT)"), "tiebreaker": bool(r.get("tiebreaker"))}
                               for r in rows]
            _cache["at"] = time.time()
            _cache["teams"] = {t["team_id"]: t["team_name"] for t in
                               get_all("teams?select=team_id,team_name&is_exhibition=is.false")}
        games = _cache["games"]
        final = {g["id"] for g in games if g.get("winner")}
        want = sorted(x for x in {int(n) for n in need if str(n).isdigit()} & final if x not in _cache["picks"])
        for i in range(0, len(want), 150):
            ids = ",".join(str(x) for x in want[i:i + 150])
            got = {x: {} for x in want[i:i + 150]}
            for p in get_all(f"picks?select=team_id,game_id,pick,jackpot&game_id=in.({ids})&order=id.asc"):
                t = _cache["teams"].get(p["team_id"])
                if t:
                    got[p["game_id"]][t] = {"pick": p["pick"], "jackpot": bool(p.get("jackpot"))}
            _cache["picks"].update(got)  # final games never change picks: cached for the process
        return League(games, {gid: _cache["picks"].get(gid, {}) for gid in final})


SOURCE = _supabase_league  # (need: set of game ids whose picks are wanted) -> League


# -----------------------------------------------------------------------------
# shots at Picky
# -----------------------------------------------------------------------------

_INSULT = (r"suck\w*|stinks?|trash|garbage|terrible|awful|the worst|clueless|a bum|bum|a fraud|fraud|useless|"
           r"worthless|pathetic|a joke|lame|dumb|stupid|an idiot|idiot|a moron|moron|a clown|clown|a loser|loser|"
           r"bitch\w*|a pussy|pussy|a dick|dick|an asshole|asshole|a bot|a robot|robot|a toaster|broken|"
           r"embarrassing|an embarrassment|choking|winless|washed|0-\d")
_FILL = r"(?:\s+(?:so|such|a|an|the|fucking|fuckin|literally|really|just|absolute|complete|total|actual|straight|dog))*"
_SHOT = re.compile(
    rf"\b(?:you|u|ya|picky)(?:\s+(?:are|is|r)|'re|re|s|'s)?{_FILL}\s+(?:{_INSULT})\b"
    rf"|\byour\s+(?:picks?|record|week|season|takes?)\s+(?:are|is|r|have been|has been){_FILL}\s+(?:{_INSULT})\b"
    rf"|^\W*(?:picky\W*)?(?:you\W+)?(?:{_INSULT})\W*$"
    r"|\b(?:useless|trash|garbage|dumb|stupid|broken) (?:bot|robot|toaster|ai)\b"
    r"|\b(?:shut up|stfu|f+u+c*k+ (?:you|off|u)|go suck|eat shit|nobody asked|who asked|are you a bot|you a bot)\b",
    re.I)
_NOT_AT_PICKY = re.compile(r"\b(roast|burn|clown on|destroy|go after)\b", re.I)


def is_shot(text: str) -> bool:
    """A shot at Picky ('you suck', 'useless bot', 'Pussy', 'your picks are trash'), not a roast request."""
    t = re.sub(r"(?<![\w@])@\s?picky\b", "picky", str(text or ""), flags=re.I).strip()
    return bool(t) and bool(_SHOT.search(t)) and not _NOT_AT_PICKY.search(t)


def log_shot(chat_id: str, sender_team: str, text: str, ts: float | None = None, is_dm: bool = False) -> dict | None:
    sc = scope(chat_id, is_dm)
    if not sc or not sender_team or not enabled():
        return None
    ts = time.time() if ts is None else float(ts)
    item = {"id": f"s{int(ts * 1000)}:{norm(sender_team)[:12]}", "ts": ts, "scope": sc, "team": sender_team,
            "text": str(text or "")[:200], "outcome": None}
    with _LOCK:
        d = _load()
        d["shots"].append(item)
        _save(d)
    return item


def _outcome_week(ts: float, games: list[dict]):
    """The first pool week that wasn't over when the shot was taken (its earliest game still to come or in
    progress); its record is 'how it turned out afterwards'."""
    ks = sorted((k, g["week"]) for g in games if not g.get("tiebreaker") and g.get("week") is not None
                and (k := kickoff(g)) is not None and k + GAME_LEN >= ts)
    return ks[0][1] if ks else None


def _week_record(team: str, week, lg: League) -> tuple[int, int] | None:
    """The member's W-L that week against the number, once every pool game that week is final."""
    wg = [g for g in lg.games if g.get("week") == week and not g.get("tiebreaker")]
    if not wg or not all(g.get("winner") for g in wg):
        return None
    w = l = 0
    for g in wg:
        p = lg.pick(g["id"], team)
        if p:
            hit = bool(p.get("jackpot")) or same(p["pick"], g["winner"])
            w, l = w + hit, l + (not hit)
    return w, l


# -----------------------------------------------------------------------------
# Picky's leans
# -----------------------------------------------------------------------------

_LEAD = (r"\b(?:i'?d|i would|i'?m|im|i'?ll|i will|i)\s+(?:still\s+|definitely\s+|probably\s+|happily\s+)?"
         r"(?:take|taking|go with|going with|ride|riding|roll with|rolling with|hammer|hammering|back|backing|"
         r"like|lean|leaning|love|play|playing|pick|picking|grab|be on|on)\b"
         r"|(?:^|[,;:]\s*|\b(?:just|so|then)\s+)(?:take|ride|hammer|give me|gimme|go with|roll with|lean|smash|grab|"
         r"back|play)\b"
         r"|\byou (?:should|gotta|got to|need to|have to|wanna|want to)\s+(?:take|ride|go with|back|hammer|grab|play)\b"
         r"|\b(?:my|the) (?:pick|play|lean|lock|side|guy) (?:is|'s|here is|:)\s*")
_TRAIL = r"\b(?:is|'s|are|looks like) (?:the|my) (?:play|pick|lean|lock|side)\b|\ball day\b|\bfor me\b"
_FADE_LEAD = (r"\b(?:fade|fading|stay away from|avoid|don'?t (?:take|touch|trust|back|bet)|wouldn'?t "
              r"(?:take|touch|trust|back)|not touching|steer clear of|pass on|no way i'?d? take|i'?m off)\b")
_FADE_TRAIL = r"\b(?:is|'s|looks like|smells like) (?:a |the )?(?:trap|sucker bet|fade|bad bet|no-go)\b"


def _team_pattern(name: str) -> str:
    toks = re.findall(r"[a-z0-9]+", norm(name))
    alt = {"state": "(?:state|st)", "and": "(?:and|&)", "saint": "(?:saint|st)"}
    return r"(?<![a-z0-9])" + r"[\s.\-']*".join(alt.get(t, re.escape(t)) for t in toks) + r"(?![a-z0-9])"


def _aliases(games: list[dict]) -> list[tuple[str, dict, str]]:
    """(regex, game, team) for each slate team: the full name, plus the name without a trailing 'State'
    when no other slate team goes by that ('Boise' for Boise State, never 'Oregon' for Oregon State
    when Oregon plays too)."""
    teams = [(g, t) for g in games for t in (g["away"], g["home"])]
    names = {norm(t) for _, t in teams}
    out = []
    for g, t in teams:
        out.append((_team_pattern(t), g, t))
        short = re.sub(r"\s+state$", "", norm(t))
        if short != norm(t) and len(short) >= 4 and short not in names \
                and sum(re.sub(r"\s+state$", "", n) == short for n in names) == 1:
            out.append((_team_pattern(short), g, t))
    return sorted(out, key=lambda x: -len(x[0]))


def extract_leans(reply: str, games: list[dict]) -> list[dict]:
    """[{game, side, fade, said}] for each clear lean in Picky's reply on a slate game that isn't final.
    A fade ('stay away from Boise', 'Boise -13.5 is a trap') leans to the other side. Questions, past-tense
    result talk and games with conflicting leans are skipped."""
    open_games = [g for g in games if not g.get("winner") and not g.get("tiebreaker") and g.get("line") is not None]
    if not reply or not open_games:
        return []
    al = _aliases(open_games)
    found: dict = {}
    for sent in re.split(r"(?<=[.!?])\s+|\n+", str(reply)):
        s = _fold(sent).strip()
        if not s or s.endswith("?"):
            continue
        mentions, taken = [], []
        for pat, g, t in al:
            for m in re.finditer(pat, s):
                if any(a < m.end() and m.start() < b for a, b in taken):
                    continue
                taken.append((m.start(), m.end()))
                num = re.match(r"\s*\(?\s*([-+]\d+(?:\.\d+)?)", s[m.end():])
                mentions.append((m.start(), m.end(), g, t, float(num.group(1)) if num else None))
        if not mentions:
            continue
        cues = ([(m, "lead", False) for m in re.finditer(_LEAD, s, re.I)]
                + [(m, "trail", False) for m in re.finditer(_TRAIL, s, re.I)]
                + [(m, "lead", True) for m in re.finditer(_FADE_LEAD, s, re.I)]
                + [(m, "trail", True) for m in re.finditer(_FADE_TRAIL, s, re.I)])
        if not cues:
            continue
        for cm, kind, fade in cues:
            if kind == "lead":
                cand = [x for x in mentions if x[0] >= cm.end() - 1 and x[0] - cm.end() <= 40]
                pick = min(cand, key=lambda x: x[0]) if cand else None
            else:
                cand = [x for x in mentions if x[1] <= cm.start() + 1 and cm.start() - x[1] <= 40]
                pick = max(cand, key=lambda x: x[1]) if cand else None
            if not pick:
                continue
            _, _, g, t, said = pick
            side = other_side(t, g) if fade else t
            prev = found.get(g["id"])
            if prev and not same(prev["side"], side):
                found[g["id"]] = None  # he argued both sides: no lean
            elif prev is None and g["id"] in found:
                pass
            elif prev:  # same side again ('X is a trap. Take Y.'): a fade anywhere keeps the trap framing
                if fade and not prev["fade"]:
                    prev.update(fade=True, faded=t)
            else:
                found[g["id"]] = {"game": g, "side": side, "fade": fade, "said": said,
                                  "faded": t if fade else None}
    return [v for v in found.values() if v][:4]


def log_lean(chat_id: str, member_team: str, game_id, side: str, line, ts: float | None = None,
             is_dm: bool = False, fade: bool = False, week=None, away: str = "", home: str = "") -> dict | None:
    """One lean Picky gave `member_team` (`line` is the side's pool line). The latest lean per
    chat + member + game wins (he changed his mind)."""
    sc = scope(chat_id, is_dm)
    if not sc or not member_team or game_id is None or not side or not enabled():
        return None
    ts = time.time() if ts is None else float(ts)
    item = {"id": f"l{game_id}:{norm(member_team)[:12]}:{sc[-10:]}", "ts": ts, "scope": sc, "team": member_team,
            "game_id": game_id, "side": side, "line": line, "fade": bool(fade), "week": week,
            "away": away, "home": home, "result": None}
    with _LOCK:
        d = _load()
        d["leans"] = [x for x in d["leans"] if not (x["scope"] == sc and x["team"] == member_team
                                                     and str(x["game_id"]) == str(game_id) and not x.get("result"))]
        d["leans"].append(item)
        _save(d)
    return item


def grade_lean(lean: dict, g: dict) -> dict | None:
    """{'covered', 'margin', 'label', 'faded_label'} once the game is final. `winner` (the side the league
    scored as covered) is the truth; the margin comes from the score and the pool line."""
    if not g or not g.get("winner"):
        return None
    side = g["home"] if same(lean["side"], g["home"]) else g["away"] if same(lean["side"], g["away"]) else None
    if side is None:
        return None
    covered = same(side, g["winner"])
    margin = None
    try:
        hm = float(g["home_pts"]) + float(g["line"]) - float(g["away_pts"])
        m = hm if side == g["home"] else -hm
        if (m > 0 or (m == 0 and side == g["away"])) == covered:  # a stale score that disagrees: no margin
            margin = abs(m)
    except (KeyError, TypeError, ValueError):
        pass
    return {"covered": covered, "margin": margin, "label": side_label(side, g),
            "faded_label": side_label(other_side(side, g), g), "kick": kickoff(g)}


# -----------------------------------------------------------------------------
# after a reply: log + grade
# -----------------------------------------------------------------------------

def _slate(ctx: dict | None, lg: League | None) -> list[dict]:
    if ctx and ctx.get("games_this_week"):
        return [{"id": g["id"], "week": ctx.get("current_week"), "away": g["away"], "home": g["home"],
                 "line": g.get("line"), "winner": g.get("winner"), "start": g.get("start"),
                 "tiebreaker": bool(g.get("tiebreaker"))} for g in ctx["games_this_week"]]
    if not lg:
        return []
    open_weeks = [g["week"] for g in lg.games if not g.get("winner") and not g.get("tiebreaker")
                  and g.get("week") is not None]
    return [g for g in lg.games if open_weeks and g.get("week") == min(open_weeks)]


def grade_pending(now: float | None = None) -> int:
    """Grade every lean whose game is final and every shot whose outcome week is complete. Returns how
    many items it graded. Network (SOURCE) happens outside the state lock."""
    now = time.time() if now is None else now
    d = _load()
    leans = [x for x in d["leans"] if not x.get("result")]
    shots = [s for s in d["shots"] if not s.get("outcome")]
    if not leans and not shots:
        return 0
    lg = SOURCE(set())
    need = {x["game_id"] for x in leans}
    for s in shots:
        wk = _outcome_week(s["ts"], lg.games)
        need |= {g["id"] for g in lg.games if wk is not None and g.get("week") == wk}
    lg = SOURCE(need)
    results, outcomes = {}, {}
    for x in leans:
        r = grade_lean(x, lg.game(x["game_id"]))
        if r:
            p = lg.pick(x["game_id"], x["team"])
            r["member_pick"] = p["pick"] if p else None
            results[x["id"]] = r
    for s in shots:
        wk = _outcome_week(s["ts"], lg.games)
        rec = _week_record(s["team"], wk, lg) if wk is not None else None
        if rec is not None:
            last = max((k for g in lg.games if g.get("week") == wk and (k := kickoff(g))), default=s["ts"])
            outcomes[s["id"]] = {"week": wk, "w": rec[0], "l": rec[1], "done": last}
    with _LOCK:
        d = _load()
        for x in d["leans"]:
            if x["id"] in results and not x.get("result"):
                x["result"] = results[x["id"]]
        for s in d["shots"]:
            if s["id"] in outcomes and not s.get("outcome"):
                s["outcome"] = outcomes[s["id"]]
        d["graded_at"] = now
        _save(d)
    return len(results) + len(outcomes)


def after_reply(chat_id: str, sender_team: str | None, question: str, reply: str, ctx: dict | None = None,
                is_dm: bool = False, ts: float | None = None) -> dict:
    """Log the sender's shot (if the text was one) and any lean Picky's sent reply gave them, then grade
    what's pending. {'shot': bool, 'leans': n, 'graded': n}."""
    out = {"shot": False, "leans": 0, "graded": 0}
    if not enabled() or not sender_team or not scope(chat_id, is_dm):
        return out
    ts = time.time() if ts is None else ts
    if is_shot(question):
        out["shot"] = bool(log_shot(chat_id, sender_team, question, ts=ts, is_dm=is_dm))
    lg = None
    if not (ctx and ctx.get("games_this_week")):
        try:
            lg = SOURCE(set())
        except Exception as e:  # noqa: BLE001  no slate, no leans this time
            log.warning("callbacks: league read failed: %r", e)
    for le in extract_leans(reply, _slate(ctx, lg)):
        g = le["game"]
        if log_lean(chat_id, sender_team, g["id"], le["side"], side_line(le["side"], g), ts=ts, is_dm=is_dm,
                    fade=le["fade"], week=g.get("week"), away=g["away"], home=g["home"]):
            out["leans"] += 1
    try:
        out["graded"] = grade_pending()
    except Exception as e:  # noqa: BLE001
        log.warning("callbacks: grading failed: %r", e)
    return out


def hook(chat_id: str, sender_team: str | None, question: str, reply: str, is_dm: bool = False,
         ctx: dict | None = None) -> threading.Thread | None:
    """group_chat.py's one line after a reply goes out: after_reply in a daemon thread. Never raises,
    never blocks the reply."""
    def run():
        try:
            after_reply(chat_id, sender_team, question, reply, ctx=ctx, is_dm=is_dm)
        except Exception as e:  # noqa: BLE001
            log.warning("callbacks: after_reply failed: %r", e)
    try:
        if not reply or not sender_team or not enabled():
            return None
        t = threading.Thread(target=run, name="picky-callbacks", daemon=True)
        t.start()
        return t
    except Exception as e:  # noqa: BLE001
        log.warning("callbacks: hook failed: %r", e)
        return None


# -----------------------------------------------------------------------------
# the callback line
# -----------------------------------------------------------------------------

def _when(ts: float, now: float) -> str:
    a, b = dt.datetime.fromtimestamp(ts, CT), dt.datetime.fromtimestamp(now, CT)
    days = (b.date() - a.date()).days
    if days <= 0:
        return "earlier today"
    if days == 1:
        return "yesterday"
    if days <= 6:
        return a.strftime("%A")
    if days <= 13:
        return "last " + a.strftime("%A")
    return "a while back"


def _words(t: str) -> set:
    stop = {"you", "u", "ur", "your", "youre", "are", "is", "a", "the", "picky", "so", "just", "and", "i", "im",
            "at", "to", "of", "it", "this", "that", "be", "get", "lol", "lmao", "bro", "dude", "man"}
    return {w for w in re.findall(r"[a-z0-9']+", _fold(t)) if w not in stop}


_BOT = re.compile(r"\b(bot|robot|toaster|ai|computer|program\w*|code)\b", re.I)


def similar_shot(a: str, b: str) -> bool:
    """Same kind of shot: both bot cracks, both plain insults, or mostly the same words."""
    if is_shot(a) and is_shot(b) and bool(_BOT.search(a)) == bool(_BOT.search(b)):
        return True
    wa, wb = _words(a), _words(b)
    return bool(wa and wb) and len(wa & wb) / len(wa | wb) >= 0.5


def lean_line(x: dict) -> str:
    r, m = x["result"], x["result"]["margin"]
    if m == 0:  # landed on the number: the league gives the push to the away side
        hit, miss = " on the push", " on the push (pushes go to the away side)"
    else:
        hit = miss = f" by {_num(m)}" if m is not None else ""
    if r["covered"]:
        line = (f"told you {r['faded_label']} was a trap, it missed the cover{hit}." if x.get("fade")
                else f"told you {r['label']} was the play, covered{hit}.")
    else:
        line = (f"I faded {r['faded_label']} and it covered{miss}. that one's on me." if x.get("fade")
                else f"my bad on {r['label']}, missed the cover{miss}.")
    mp = r.get("member_pick")
    if mp:
        followed = same(mp, x["side"])
        line += {(True, True): " you listened, good.", (True, False): " you went the other way. should've listened.",
                 (False, True): " sorry, you rode it with me.", (False, False): " you faded me and got it right."
                 }[(r["covered"], followed)]
    return line


def shot_line(s: dict, now: float, text: str | None) -> str:
    o, when = s["outcome"], _when(s["ts"], now)
    rec = f"{o['w']}-{o['l']} against the number in week {o['week']}"
    if text and similar_shot(text, s["text"]):
        return (f"you said that {when} too, then went {rec}." if when != "a while back"
                else f"you've said that before, then went {rec}.")
    return (f"last time you came at me ({when}) you went {rec} right after." if when != "a while back"
            else f"last time you came at me you went {rec} right after.")


def _ledger():
    try:
        import no_repeat  # burn lane's module, when it's merged
        return no_repeat.Ledger()
    except Exception:  # noqa: BLE001
        return None


_grading = threading.Lock()


def _grade_soon(d: dict) -> None:
    if time.time() - d.get("graded_at", 0) < GRADE_EVERY or not enabled() or _grading.locked():
        return

    def run():
        if not _grading.acquire(blocking=False):
            return
        try:
            grade_pending()
        except Exception as e:  # noqa: BLE001
            log.warning("callbacks: background grading failed: %r", e)
        finally:
            _grading.release()
    threading.Thread(target=run, name="picky-callbacks-grade", daemon=True).start()


_RECALL = re.compile(r"\bremember (?:what|when|how) you\b|\bremember your\b|\bwhat did you (?:say|tell me|have)\b"
                     r"|\byou (?:said|told me|had me on)\b|\bwhat was your (?:call|lean|pick|take)\b"
                     r"|\bhow(?:'d| did) your (?:call|lean|pick|take)\b", re.I)


def is_recall(text: str | None) -> bool:
    """'remember what you said about Boise?' / 'what did you tell me on UTSA': asks for his own past lean."""
    return bool(text) and bool(_RECALL.search(str(text)))


def _names(x: dict) -> list[str]:
    return [t for t in (x.get("away"), x.get("home"), x.get("side")) if t]


def _mentions_game(text: str | None, x: dict) -> bool:
    """The text names one of the lean's teams (full name, or without a trailing 'State')."""
    t = norm(text or "")
    for name in _names(x):
        for n in {norm(name), re.sub(r"\s+state$", "", norm(name))}:
            if len(n) >= 3 and re.search(rf"(?<![a-z0-9]){re.escape(n)}(?![a-z0-9])", t):
                return True
    return False


def pending_line(x: dict) -> str:
    """An ungraded lean, only for an explicit recall ask naming it: the call, no result."""
    lab = f"{x['side']} {'+' if float(x['line']) > 0 else ''}{_num(float(x['line']))}" if x.get("line") not in (None, 0) \
        else f"{x['side']} (pick'em)" if x.get("line") == 0 else x["side"]
    return (f"I faded {other_side(x['side'], x)} there, so {lab}. not graded yet." if x.get("fade") and x.get("away")
            else f"I had you on {lab}. not graded yet.")


def candidates(chat_id: str, sender_team: str | None, is_dm: bool, text: str | None = None,
               now: float | None = None) -> list[tuple[str, str]]:
    """[(item id, line)] callable for this member in this chat, best first, cooldown not applied.
    Graded material only, except an explicit recall ask naming a game also gets that pending lean."""
    here = scope(chat_id, is_dm)
    if not here or not sender_team:
        return []
    now = time.time() if now is None else now
    d = _load()
    recall = is_recall(text)
    leans = sorted((x for x in d["leans"] if x["team"] == sender_team and x.get("result") and _visible(x, here)
                    and now - (x["result"].get("kick") or x["ts"]) < LEAN_FRESH_DAYS * 86400),
                   key=lambda x: (not (recall and _mentions_game(text, x)), -(x["result"].get("kick") or x["ts"])))
    shots = sorted((s for s in d["shots"] if s["team"] == sender_team and s.get("outcome") and _visible(s, here)
                    and s["outcome"]["l"] >= s["outcome"]["w"] and s["outcome"]["w"] + s["outcome"]["l"] > 0
                    and now - s["ts"] < SHOT_FRESH_DAYS * 86400 and s["outcome"].get("done", 0) <= now),
                   key=lambda s: (not (text and similar_shot(text, s["text"])), -s["ts"]))
    a = [(x["id"], lean_line(x)) for x in leans]
    if recall and any(_mentions_game(text, x) for x in d["leans"] if x["team"] == sender_team):
        # he's asking about a specific game: only that lean (graded, else pending), or nothing. A game he
        # only discussed in another chat gets nothing back, so a DM lean can't even be confirmed here.
        pend = [x for x in d["leans"] if x["team"] == sender_team and not x.get("result") and _visible(x, here)
                and now - x["ts"] < LEAN_FRESH_DAYS * 86400 and _mentions_game(text, x)]
        return ([(x["id"], lean_line(x)) for x in leans if _mentions_game(text, x)]
                + [(x["id"] + ":p", pending_line(x)) for x in sorted(pend, key=lambda x: -x["ts"])])
    b, weeks = [], set()
    for s in shots:  # five shots before the same bad week are one callback, used once
        if s["outcome"]["week"] not in weeks:
            weeks.add(s["outcome"]["week"])
            b.append((f"sw:{norm(sender_team)}:{s['outcome']['week']}", shot_line(s, now, text)))
    return b + a if text and is_shot(text) and not recall else a + b


def _due(d: dict, here: str, banter: bool | None, text: str | None) -> bool:
    """Policy: an explicit recall ask always; a banter reply when it's the Nth since the last callback here."""
    if banter is None or is_recall(text):
        return True
    return bool(banter) and d["policy"].get(here, 0) + 1 >= force_every()


def callback_for(chat_id: str, sender_team: str | None, is_dm: bool, text: str | None = None,
                 now: float | None = None, mark: bool = True, banter: bool | None = None) -> str | None:
    """At most one short, true callback for the member texting Picky in this chat, or None. Group material
    anywhere, DM material only in that DM. Returned items go on cooldown (mark=False to peek). `text` (the
    member's current message, optional) lets a repeat shot get 'you said that Saturday too' and a recall ask
    get the lean it names.

    `banter` turns on the per-reply policy (call once per reply): True/False = is this a banter reply. A line
    comes back only on an explicit recall ask, or on the every-Nth banter reply in this chat where one exists
    (the counter waits for material; a served callback resets it). None = just offer the best one.
    Pure file read: no network on this path (grading runs in the background)."""
    try:
        now = time.time() if now is None else now
        here = scope(chat_id, is_dm)
        with _LOCK:
            d = _load()
            _grade_soon(d)
            write = mark and enabled()
            line, iid = None, None
            if here and _due(d, here, banter, text):
                led = _ledger()
                for iid, cand in candidates(chat_id, sender_team, is_dm, text, now):
                    if now - d["used"].get(iid, 0) < COOLDOWN_DAYS * 86400:
                        continue
                    if led is not None and led.is_repeat(cand, target=sender_team, chat_id=here):
                        continue
                    line = cand
                    if write and led is not None:
                        led.record(cand, target=sender_team, chat_id=here, angle="callback")
                    break
            if write and here and (line or banter):
                if line:
                    d["used"][iid] = now
                    d["policy"][here] = 0
                elif banter:
                    d["policy"][here] = d["policy"].get(here, 0) + 1
                _save(d)
            return line
    except Exception as e:  # noqa: BLE001  a callback is garnish: never break the reply over one
        log.warning("callbacks: callback_for failed: %r", e)
        return None


# -----------------------------------------------------------------------------
# burn sheet (lore): true, roastable lines per member
# -----------------------------------------------------------------------------

_BURN = [  # (angle, regex on a lore line, phrasing) -- every number comes from the lore line itself
    ("blown", re.compile(r"worst blown picks \(favorite that lost outright\): (.+?)\.?$", re.I), None),
    ("lone_miss", re.compile(r"lone-wolf misses: (.+?)\.?$", re.I), None),
    ("winless", re.compile(r"^(?:.*?, )?(\d{4})(?: season| so far)[^:]*:.*?winless weeks: ([^;]+)", re.I), None),
    ("worst_week", re.compile(r"^(?:.*?, )?(\d{4}) so far[^:]*:.*?worst week (\d+)-(\d+) \(([^)]+)\)", re.I), None),
    ("miss_streak", re.compile(r"^(?:.*?, )?(\d{4})(?: season| so far)[^:]*:.*?longest miss streak (\d+)", re.I), None),
    ("oddity", re.compile(r"stats-page oddity: (.+?)\.?$", re.I), None),
    ("h2h", re.compile(r"head to head [^:]*: .*?worst vs (.+?) (\d+)-(\d+)\.?$", re.I), None),
]


def _load_lore() -> dict:
    try:
        return json.loads(LORE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def burn_sheet(team: str, lore: dict | None = None) -> list[dict]:
    """[{id, text, angle, facts, scope}] for `team`: the roastable lines in the lore cache, each a short
    true fact (from lore.py's computed lines, nothing added). League data, so scope 'group' (callable
    anywhere). Worst first-ish: blown chalk, lone-wolf misses, winless weeks, this season's worst week,
    long miss streaks (5+), losing stats-page oddities, losing head-to-head."""
    lines = ((lore if lore is not None else _load_lore()).get("members") or {}).get(team) or []
    out: list[dict] = []

    def add(angle, text, facts):
        out.append({"id": f"burn:{norm(team)}:{angle}:{len(out)}", "text": text, "angle": angle,
                    "facts": facts, "scope": "group"})
    for ln in lines:
        for angle, rx, _ in _BURN:
            m = rx.search(ln)
            if not m:
                continue
            if angle in ("blown", "lone_miss"):
                for part in [p.strip() for p in m.group(1).split(";") if p.strip()]:
                    add(angle, part, {"line": ln})
            elif angle == "winless":
                add(angle, f"went winless in {m.group(2).strip()} ({m.group(1)})", {"season": m.group(1)})
            elif angle == "worst_week":
                w, l = int(m.group(2)), int(m.group(3))
                if l > w:
                    add(angle, f"worst week this season: {w}-{l} ({m.group(4)})", {"w": w, "l": l})
            elif angle == "miss_streak":
                if int(m.group(2)) >= 5:
                    add(angle, f"missed {m.group(2)} picks in a row in {m.group(1)}", {"streak": int(m.group(2))})
            elif angle == "oddity":
                rec = re.findall(r"\b(\d+)-(\d+)\b", m.group(1))
                if (rec and int(rec[0][1]) > int(rec[0][0])) or re.search(r"\blost all \d+", m.group(1)):
                    add(angle, m.group(1), {"line": ln})
            elif angle == "h2h":
                w, l = int(m.group(2)), int(m.group(3))
                if l > w:
                    add(angle, f"{w}-{l} head to head against {m.group(1)}", {"vs": m.group(1), "w": w, "l": l})
    order = [a for a, _, _ in _BURN]
    return sorted(out, key=lambda x: order.index(x["angle"]))


# -----------------------------------------------------------------------------
# exam rider (PICKY_CALLBACK_RIDER=1): group_chat sets CHAT, picky_brain asks rider()
# -----------------------------------------------------------------------------

CHAT: contextvars.ContextVar = contextvars.ContextVar("picky_callbacks_chat", default=None)  # (chat_id, team, is_dm)


def rider_on() -> bool:
    return os.environ.get("PICKY_CALLBACK_RIDER") == "1"


def rider(q: str, intent: str = "") -> str:
    """The draft-prompt line for this reply, or ''. One callback at most, by the policy above."""
    c = CHAT.get()
    if not rider_on() or not c:
        return ""
    chat_id, team, is_dm = c
    line = callback_for(chat_id, team, is_dm, text=q, banter=intent in BANTER_INTENTS)
    if not line:
        return ""
    log.info("callback rider: %s", line[:160])
    return ("CALLBACK (true, from your own log of this league; use it): work this in as ONE short clause in your "
            "own words, keeping every team, line and number exactly as written, aimed at the person texting you: "
            f"\"{line}\"")
