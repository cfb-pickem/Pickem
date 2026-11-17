"""
League playoff race math. Deterministic, no LLM: every number Picky says
about the playoff race comes from here.

League rules (league_rules.md): season standings are points from weeks 0-15,
week 15 is conference championship week, the top 4 make the league playoffs
(scores reset), ties at the cut go to the regular-season tiebreakers.

Members never play each other: every remaining pool game is a point anyone
can still win, so each member's final total lands anywhere in
[points, points + remaining], independently of everyone else. That makes the
math exact:

  clinched     at most 3 rivals can even reach this member's current total
               (a rival who can only TIE counts against: tiebreakers aren't
               guaranteed), so a top-4 spot is locked with zero more points.
  eliminated   4 or more rivals are already above this member's best case.
  tiebreaker   not eliminated, but the best case is a tie for the last spot.
  magic        fewest more points that lock a spot no matter what anyone
               else does (None when even winning out doesn't).
  help         when winning out isn't enough on its own: which rivals have to
               finish under the member's win-out total, and the most games
               each of them can win for that to happen.

remaining_points(): a week whose picks are locked (regular season: the
week's first kickoff) only leaves points on undecided pool games the member
actually picked; an unlocked week leaves every undecided pool game; a week
with no slate yet counts GAMES_PER_WEEK. Tiebreaker games never score.

load(db) reads the live league with the service key (final games only for
points, so no unrevealed pick is ever read) and returns (results, meta).
"""

from __future__ import annotations

import datetime as dt
import re
import zoneinfo

SPOTS = 4
LAST_REGULAR_WEEK = 15
GAMES_PER_WEEK = 4
CT = zoneinfo.ZoneInfo("America/Chicago")


# -----------------------------------------------------------------------------
# pure math
# -----------------------------------------------------------------------------


def race(points: dict[str, int], remaining: dict[str, int], spots: int = SPOTS) -> dict[str, dict]:
    teams = list(points)
    mx = {t: points[t] + remaining.get(t, 0) for t in teams}
    ordered = sorted(points.values(), reverse=True)
    cut = ordered[spots - 1] if len(ordered) >= spots else (ordered[-1] if ordered else 0)
    out = {}
    for t in teams:
        c, r = points[t], remaining.get(t, 0)
        others = [u for u in teams if u != t]

        def threats(f: int) -> list[str]:
            return [u for u in others if mx[u] >= f]

        clinched = len(threats(c)) < spots
        eliminated = sum(1 for u in others if points[u] > mx[t]) >= spots
        tie_only = not eliminated and sum(1 for u in others if points[u] >= mx[t]) >= spots
        magic = next((k for k in range(r + 1) if len(threats(c + k)) < spots), None)
        f = mx[t]
        th = sorted(threats(f), key=lambda u: (f - 1 - points[u], u), reverse=True)
        need_fall = max(0, len(th) - (spots - 1))
        help_ = [{"team": u, "max_wins": f - 1 - points[u], "left": remaining.get(u, 0), "max": mx[u]}
                 for u in th] if magic is None and not eliminated else []
        status = ("clinched" if clinched else "eliminated" if eliminated else
                  "tiebreaker" if tie_only else "alive")
        out[t] = {"team": t, "points": c, "left": r, "max": f, "rank": 1 + sum(1 for u in others if points[u] > c),
                  "cut": cut, "gap": cut - c, "clinched": clinched, "eliminated": eliminated, "status": status,
                  "magic": 0 if clinched else magic, "win_out": f, "need_fall": need_fall if help_ else 0,
                  "help": help_}
    return out


def remaining_points(teams: list[str], slates: dict[int, dict], first_week: int,
                     last_week: int = LAST_REGULAR_WEEK, per_week: int = GAMES_PER_WEEK) -> dict[str, int]:
    """slates: {week: {"games": [{"id", "tiebreaker", "final"}], "locked": bool,
    "picks": {team: set(game_ids)}}} for weeks that have a slate."""
    rem = {t: 0 for t in teams}
    for w in range(first_week, last_week + 1):
        s = slates.get(w)
        if not s:
            for t in teams:
                rem[t] += per_week
            continue
        live = {g["id"] for g in s["games"] if not g.get("tiebreaker") and not g.get("final")}
        for t in teams:
            rem[t] += len(live & set((s.get("picks") or {}).get(t) or ())) if s.get("locked") else len(live)
    return rem


# -----------------------------------------------------------------------------
# words (exact numbers, a little Picky)
# -----------------------------------------------------------------------------


def _ord(n: int) -> str:
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def _pl(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _who(team: str, names: dict | None) -> str:
    return (names or {}).get(team) or team


def _list(xs: list[str]) -> str:
    return xs[0] if len(xs) == 1 else ", ".join(xs[:-1]) + " and " + xs[-1] if xs else ""


def _spot(r: dict) -> str:
    tied = ""
    if r["gap"] < 0:
        where = f"{-r['gap']} above the cut line ({r['cut']})"
    elif r["gap"] == 0:
        where = f"right on the cut line ({r['cut']})"
    else:
        where = f"{r['gap']} back of the cut line ({r['cut']})"
    return f"{r['points']} points, {_ord(r['rank'])}{tied}, {where}"


def need_answer(team: str, name: str | None, res: dict, meta: dict, names: dict | None = None,
                you: bool = True) -> str:
    """The exact answer to 'what do I need to make the playoffs' (or 'what
    does X need'). Every number is computed."""
    r = res.get(team)
    if not r:
        return "Couldn't find that team in the standings."
    who = name or team
    subj, has, are = ("you", "you've", "you're") if you else (who, f"{who} has", f"{who} is")
    lead = f"{who}, {are}" if you else f"{who} is"
    left = meta.get("points_left", r["left"])
    if meta.get("weeks_left", 0) <= 0 and r["left"] == 0:
        tail = {"clinched": f"{has} clinched a playoff spot.",
                "eliminated": f"{are} out. Regular season's over.",
                "tiebreaker": "tied on the cut line: it goes to the tiebreakers (conference championship "
                              "game wins first).",
                "alive": "waiting on the last games."}[r["status"]]
        return f"{lead} at {_spot(r)}. {tail[0].upper() + tail[1:]}"
    base = f"{lead} at {_spot(r)}, with {_pl(r['left'], 'point')} still on the table"
    if r["left"] != left:
        base += f" (the league max is {left})"
    if r["status"] == "clinched":
        return f"{base}. {has.capitalize()} already clinched: top 4 no matter what happens."
    if r["status"] == "eliminated":
        return (f"{base}. Even winning out gets {subj} to {r['max']}, and 4 people are already past that. "
                f"Mathematically eliminated.")
    if r["status"] == "tiebreaker":
        return (f"{base}. Best case is winning out to {r['max']} and tying for the last spot: that goes to "
                f"the tiebreakers, conference championship game wins first.")
    if r["magic"] is not None:
        if r["magic"] == r["left"]:
            return f"{base}. Win all {r['left']} and {are} in no matter what anyone else does."
        return (f"{base}. Win {r['magic']} of the last {r['left']} and {are} in no matter what anyone "
                f"else does.")
    hl = r["help"]
    can = [x for x in hl if x["max_wins"] >= 0]
    if r["need_fall"] > len(can):
        return (f"{base}. Even winning out to {r['max']}, too many people are already there: the best "
                f"left is a tie, and ties go to the tiebreakers.")
    need = r["need_fall"]
    if len(can) > 5:
        return (f"{base}. Even winning out to {r['max']} doesn't lock it: {len(can)} people can still get "
                f"there, and {subj} need {need} of them to finish under {r['max']}. Long shot.")
    parts = [f"{_who(x['team'], names)} (wins no more than {x['max_wins']} of {x['left']})" for x in can]
    return (f"{base}. Even winning out to {r['max']} doesn't lock it: {subj} also need "
            f"{'one' if need == 1 else need} of {_list(parts)} to finish under {r['max']}.")


def summary_text(res: dict, meta: dict, names: dict | None = None) -> str:
    """The playoff-math text for the Monday recap."""
    if not res:
        return ""
    weeks, left = meta.get("weeks_left", 0), meta.get("points_left", 0)
    rows = sorted(res.values(), key=lambda r: (-r["points"], r["team"]))
    cut = rows[0]["cut"]
    clinched = [_who(r["team"], names) for r in rows if r["status"] == "clinched"]
    elim = [_who(r["team"], names) for r in rows if r["status"] == "eliminated"]
    tb = [_who(r["team"], names) for r in rows if r["status"] == "tiebreaker"]
    if weeks <= 0:
        top = [_who(r["team"], names) for r in rows if r["status"] == "clinched"]
        out = f"Regular season's done. In: {_list(top) or 'nobody locked yet'}."
        if tb:
            out += f" Tied at the cut ({cut}): {_list(tb)}, tiebreakers decide it."
        return out
    head = (f"Playoff math: {_pl(weeks, 'week')} left, {_pl(left, 'point')} on the table, "
            f"cut line is {cut}.")
    bits = []
    if clinched:
        bits.append(f"Clinched: {_list(clinched)}.")
    magic = [f"{_who(r['team'], names)} {r['magic']}" for r in rows
             if r["status"] == "alive" and r["magic"] is not None]
    if magic:
        bits.append(f"Magic numbers (wins to lock a spot no matter what): {', '.join(magic)}.")
    chase = [r for r in rows if r["status"] == "alive" and r["magic"] is None and r["gap"] <= 3]
    if chase:
        bits.append("Need help even winning out: " + ", ".join(
            f"{_who(r['team'], names)} ({r['points']})" for r in chase) + ".")
    if tb:
        bits.append(f"Best case a tie at the cut: {_list(tb)}.")
    if elim:
        bits.append(f"Eliminated: {_list(elim)}.")
    rest = [r for r in rows if r["status"] == "alive" and r["magic"] is None and r["gap"] > 3]
    if rest:
        bits.append(f"{_pl(len(rest), 'more team')} still alive but needing a lot of help.")
    return head + " " + " ".join(bits)


def model_block(res: dict, meta: dict, names: dict | None = None) -> str:
    """For chat.py's prompt: the exact race, one line per member."""
    if not res:
        return ""
    rows = sorted(res.values(), key=lambda r: (-r["points"], r["team"]))
    lines = [f"PLAYOFF RACE (computed exactly; quote these, never do your own math): top {SPOTS} after week "
             f"{LAST_REGULAR_WEEK} make the league playoffs; {meta.get('weeks_left', 0)} regular-season weeks "
             f"left, {meta.get('points_left', 0)} points still available, cut line {rows[0]['cut']} (4th place)."]
    for r in rows:
        lines.append(f"- {need_answer(r['team'], _who(r['team'], names), res, meta, names, you=False)}")
    return "\n".join(lines)


_NEED_ASK = re.compile(
    r"\b(make|making|get into|get in|reach|clinch\w*|lock( up)?|in) (the |a )?(league )?playoffs?\b|"
    r"\bplayoff (math|chances?|odds|hopes?|picture|race|spot|scenarios?)\b|\bmagic number\b|\bclinch(ed)?\b|"
    r"\b(am i|are we) (eliminated|out|alive|still alive|in)\b|\bis \w+ (eliminated|still alive)\b|"
    r"\bwhat (do|does) \w+ need\b|\bmathematically\b|\beliminated\b|\bstill alive\b", re.I)
_NOT_LEAGUE = re.compile(r"\b(cfp|committee|college football playoff|rank(ed|ings?)|seed\w*|bracket|"
                         r"last (year|season)|who won|need to cover)\b", re.I)  # 'what does Iowa need to cover' = a live game


# "can I still pick the playoff games if I don't make it?" is a rules question
# (league_rules: only the seeded teams pick playoff games), not playoff math.
_PICK_RULE = re.compile(r"\bpick\w*\b.{0,30}\bplayoff (games?|picks?)\b|\bplayoff (games?|picks?)\b.{0,30}\bpick", re.I)


def is_need_question(q: str) -> bool:
    return bool(_NEED_ASK.search(q or "")) and not _NOT_LEAGUE.search(q or "") and not _PICK_RULE.search(q or "")


# -----------------------------------------------------------------------------
# live league (service key; final games for points)
# -----------------------------------------------------------------------------


def _norm(v) -> str:
    import unicodedata
    s = unicodedata.normalize("NFD", str(v or ""))
    s = "".join(c for c in s if not unicodedata.combining(c)).lower().replace("&", " and ").replace(".", "")
    s = re.sub(r"\s+", " ", re.sub(r"[^a-z0-9\s]", " ", s)).strip()
    return re.sub(r"\bst\b", "state", s)


def same(a, b) -> bool:
    return bool(a) and _norm(a) == _norm(b)


def _kick(g: dict) -> dt.datetime | None:
    s = g.get("Start (CT)")
    try:
        t = dt.datetime.fromisoformat(str(s))
    except (TypeError, ValueError):
        return None
    return t.replace(tzinfo=CT) if t.tzinfo is None else t


def week_locked(games: list[dict], now: dt.datetime) -> bool:
    """Regular season, mirroring picks_open_for_game(): the whole week locks
    at the first kickoff of any pool or tiebreaker game."""
    ks = [k for g in games if (g.get("picked") or g.get("tiebreaker")) and (k := _kick(g))]
    return bool(ks) and min(ks) <= now


def load(db, now: dt.datetime | None = None) -> tuple[dict, dict]:
    now = now or dt.datetime.now(CT)
    season = db.get("all_games?select=cfb_season&order=cfb_season.desc&limit=1")[0]["cfb_season"]
    teams = [t["team_name"] for t in db.get("teams?select=team_id,team_name,active,is_exhibition"
                                            "&is_exhibition=is.false&active=is.true")]
    tid = {t["team_id"]: t["team_name"] for t in db.get("teams?select=team_id,team_name&is_exhibition=is.false"
                                                        "&active=is.true")}
    games = db.get_all('all_games?select=GameId,week,winner,Status,tiebreaker,picked,"Start (CT)"'
                       f"&cfb_season=eq.{season}&or=(picked.eq.true,tiebreaker.eq.true)"
                       f"&week=lte.{LAST_REGULAR_WEEK}&order=GameId.asc")
    graded = {g["GameId"]: g for g in games if g.get("winner") and g.get("picked") and not g.get("tiebreaker")}
    pts = {t: 0 for t in teams}
    ids = sorted(graded)
    for i in range(0, len(ids), 150):
        chunk = ",".join(str(x) for x in ids[i:i + 150])
        for p in db.get_all(f"picks?select=team_id,game_id,pick,jackpot&game_id=in.({chunk})&order=id.asc"):
            t = tid.get(p["team_id"])
            if t in pts and (p.get("jackpot") or same(p["pick"], graded[p["game_id"]]["winner"])):
                pts[t] += 1
    open_weeks = sorted({g["week"] for g in games if g.get("picked") and not g.get("tiebreaker")
                         and not g.get("winner") and g.get("week") is not None})
    done_weeks = [g["week"] for g in games if g.get("week") is not None and g.get("picked")]
    first = open_weeks[0] if open_weeks else (max(done_weeks) + 1 if done_weeks else 0)
    slates = {}
    for w in open_weeks:
        wg = [g for g in games if g["week"] == w]
        locked = week_locked(wg, now)
        picks = {}
        if locked:  # revealed at lock, same moment the site shows them
            gids = ",".join(str(g["GameId"]) for g in wg)
            for p in db.get_all(f"picks?select=team_id,game_id&game_id=in.({gids})&order=id.asc"):
                if tid.get(p["team_id"]):
                    picks.setdefault(tid[p["team_id"]], set()).add(p["game_id"])
        slates[w] = {"locked": locked, "picks": picks,
                     "games": [{"id": g["GameId"], "tiebreaker": bool(g.get("tiebreaker")) or not g.get("picked"),
                                "final": bool(g.get("winner"))} for g in wg]}
    rem = remaining_points(teams, slates, first)
    weeks_left = max(0, LAST_REGULAR_WEEK - first + 1)
    meta = {"season": season, "first_week": first, "weeks_left": weeks_left,
            "points_left": max(rem.values()) if rem else 0}
    return race(pts, rem), meta
