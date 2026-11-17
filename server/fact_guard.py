"""
Send-point fact guard for member claims + wrong-premise detection (goal (a), 2026-11-17).

Plain code over chat.py's ctx (standings_ytd, weekly_records, last_week, games_this_week, you) and the lore
cache; no model. Two pieces:

- `member_claim_check(ctx, question, lore, extra_true)` -> sentence -> bool. A sentence that ties a member
  (or "you" = the sender, or "I/my" = Picky) to a W-L, a place or a points gap is dropped unless the claim is
  true. Label-aware: "season/overall" = the season record, "last week" = last week's (or, in the Monday window
  after a week goes final, the week that just ended), "this week" = this week's, "week N" = week N's. An
  unlabeled W-L may be any record that's true about that member (season, a week, last 8, lore lines, h2h).
  Closes the audit gaps: W-Ls summing 5-8 were never checked, "you're third" / "3rd-place X" slipped, and gap
  claims had no sentence check.
- `premise_note(ctx, question, picky=None)` -> rider text or "". When the sender's question asserts something
  false about their own record/place/pick, a cover, a final score or a pool line ("since I'm 33-15 ...",
  "now that I'm in first", "why did I take SMU", "Boise covered right?", "Iowa -15.5 in the pool"), the
  rider says it's false and gives the true value, so the model corrects it instead of running with it.
  `premise_echo_check` drops a reply sentence that repeats the false value as true.
"""

from __future__ import annotations

import re

ORD = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7, "eighth": 8,
       "ninth": 9, "tenth": 10, "eleventh": 11, "twelfth": 12, "thirteenth": 13, "fourteenth": 14,
       "fifteenth": 15, "sixteenth": 16}
_REC = re.compile(r"(?<![\d.$:/-])(\d{1,2})\s*-\s*(\d{1,2})(?![\d.:/])")
_PLACE = re.compile(r"\b(?:(\d{1,2})(?:st|nd|rd|th)|(" + "|".join(ORD) + r"))[- ](?:place|spot)\b"
                    r"|\b(?:in|sitting|sits|sit|tied for|alone in|holding|hold|at|is|are|'re|'s)\s+(?:sole\s+)?"
                    r"(?:(\d{1,2})(?:st|nd|rd|th)|(" + "|".join(ORD) + r"))\b(?![- ](?:quarter|half|down|week|game|pick|kick|time))",
                    re.I)
_LAST_PLACE = re.compile(r"\b(?:in|dead|sitting|sits|alone in|tied for)\s+last\b|\blast[- ]place\b", re.I)
_GAP = re.compile(r"\b(\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten)\s+(?:points?|wins?|games?|picks?)\s+"
                  r"(behind|back|ahead|clear|up|out|off|in front|adrift|short)\b", re.I)
_NUMW = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}
_SEASON = re.compile(r"\b(season|overall|on the year|this year|ytd|for the year|total)\b", re.I)
_LASTWK = re.compile(r"\blast week(end)?\b|\blast saturday\b", re.I)
_THISWK = re.compile(r"\bthis week(end)?\b", re.I)
_WEEKN = re.compile(r"\bweek (\d{1,2})\b", re.I)
_NEG = re.compile(r"\b(not|isn'?t|aren'?t|wasn'?t|weren'?t|nope|nah|wish|dream(ing)?|pretend\w*|fantasy|imaginary|"
                  r"delusion\w*|hallucinat\w*|thought|think|claim\w*|says?|said|sure about)\b|\?", re.I)
# W-Ls that aren't a member's pick record even when a member is named: casino, team/split records, h2h
_NOT_PICK_REC = re.compile(r"\b(blackjack|chips?|the pit|slots?|hands?|in \w+(?: \w+)? games|against|picking (for|against)|"
                           r"vs\.?|versus|head to head|h2h|as (a |an )?(dog|underdog|fav\w*)|on the road|road|at home|home|"
                           r"taking|fading|ATS|outright|won \d|lost \d|final|score|beat|by \d)\b", re.I)
_YOU = re.compile(r"\b(you'?re|you are|you went|you sit|you were|you finished|you'?ve gone|you|your)\b", re.I)
_ME = re.compile(r"\b(I'?m|I am|I went|I'?ve gone|my record|I sit|I was|I finished|me at|my season|my week)\b")


def _n(x: str) -> int:
    return int(x) if x.isdigit() else _NUMW[x.lower()]


def _mentions(s: str, name: str) -> bool:
    return bool(re.search(rf"(?<!\w){re.escape(name)}(?:'s)?(?!\w)", s, re.I))


class Facts:
    """Everything a member-claim check needs, computed once per request."""

    def __init__(self, ctx: dict, lore: dict | None = None, picky: dict | None = None, monday_window: bool = False):
        self.st = {r["team"]: r for r in ctx.get("standings_ytd") or []}
        self.weekly = ctx.get("weekly_records") or {}
        self.week = ctx.get("current_week")
        self.lw = (ctx.get("last_week") or {}).get("week")
        self.me = (ctx.get("you") or {}).get("team_name")
        self.members = sorted(self.st, key=len, reverse=True)
        self.leader_w = max((r["w"] for r in self.st.values()), default=0)
        ws = sorted((r["w"] for r in self.st.values()), reverse=True)
        self.cut_w = ws[3] if len(ws) >= 4 else None
        self.last_rank = max((r["rank"] for r in self.st.values()), default=0)
        self.monday = monday_window
        self.picky = picky or {}
        self.lore_recs: dict[str, set[str]] = {}
        for m, lines in ((lore or {}).get("members") or {}).items():
            self.lore_recs[m] = {f"{int(a)}-{int(b)}" for ln in lines if ln.startswith(m)
                                 for a, b in re.findall(r"\b(\d{1,2})-(\d{1,2})\b", ln)}

    def season(self, m):
        r = self.st.get(m)
        return f"{r['w']}-{r['l']}" if r else None

    def wk(self, m, w):
        r = (self.weekly.get(m) or {}).get(w)
        if r is None:
            r = (self.weekly.get(m) or {}).get(str(w))
        return f"{r[0]}-{r[1]}" if r else None

    def last_n(self, m) -> set[str]:
        """Sums of the last 1..4 played weeks (an unlabeled '7-1 lately' is the last two weeks)."""
        wk = self.weekly.get(m) or {}
        keys = sorted(int(k) for k in wk)
        out = set()
        for n in range(1, 5):
            sel = keys[-n:]
            if sel:
                w = sum(wk.get(k, wk.get(str(k), [0, 0]))[0] for k in sel)
                l = sum(wk.get(k, wk.get(str(k), [0, 0]))[1] for k in sel)
                out.add(f"{w}-{l}")
        return out

    def any_true(self, m) -> set[str]:
        out = {self.season(m)} | {f"{v[0]}-{v[1]}" for v in (self.weekly.get(m) or {}).values()}
        return {x for x in out if x} | self.last_n(m) | self.lore_recs.get(m, set())

    def labeled(self, m, s) -> set[str] | None:
        """The true W-L(s) for the period the sentence names, or None when it names no period."""
        if _SEASON.search(s):
            return {self.season(m)}
        wn = _WEEKN.search(s)
        if wn:
            return {self.wk(m, int(wn.group(1)))}
        if _LASTWK.search(s):
            out = {self.wk(m, self.lw)} if self.lw is not None else set()
            if self.monday:  # 'last week' on the Monday after = the week that just ended, too
                out.add(self.wk(m, self.week))
            return out
        if _THISWK.search(s):
            out = {self.wk(m, self.week)}
            if self.monday and self.lw is not None:
                out.add(self.wk(m, self.lw))
            return out
        return None

    def picky_true(self) -> set[str]:
        out = set()
        for k in ("season", "last_week"):
            v = self.picky.get(k)
            if v:
                out.add(f"{v[0]}-{v[1]}")
        tw = self.picky.get("this_week_record")
        if tw:
            out.add(f"{tw[0]}-{tw[1]}")
        return out


def subject(s: str, f: Facts) -> str | None:
    """The one member a sentence is about: a single named member, else 'you' = the sender."""
    names = [m for m in f.members if _mentions(s, m)]
    if len(names) == 1:
        return names[0]
    if not names and f.me and _YOU.search(s):
        return f.me
    return None


def problems(s: str, f: Facts, card_recs: set[str] = frozenset()) -> list[str]:
    out = []
    if _NEG.search(s):
        return out  # corrections ("you're not 33-15") and questions back are fine
    who = subject(s, f)
    recs = [f"{int(a)}-{int(b)}" for a, b in _REC.findall(s)]
    # Picky's own record ('I'm 0-3', 'my 25-10 season')
    if _ME.search(s) and not who and recs and not re.search(r"\b(you|your)\b", s, re.I):
        pt = f.picky_true()
        for r in recs:
            if pt and r not in pt:
                out.append(f"Picky {r} (true {sorted(pt)})")
        return out
    if not who:
        return out
    if recs and not _NOT_PICK_REC.search(s):
        lab = f.labeled(who, s)
        for r in recs:
            if r in card_recs and not lab:
                continue
            ok = (r in lab) if lab else (r in f.any_true(who))
            if not ok:
                out.append(f"{who} {r} (true {sorted(x for x in (lab or {f.season(who)}) if x)})")
    rank = f.st[who]["rank"] if who in f.st else None
    if rank is not None and not _WEEKN.search(s) and not _LASTWK.search(s) and not _THISWK.search(s):
        for m in _PLACE.finditer(s):
            g = [x for x in m.groups() if x]
            n = int(g[0]) if g[0].isdigit() else ORD[g[0].lower()]
            if n != rank and n <= len(f.st):
                out.append(f"{who} place {n} (is {rank})")
        if _LAST_PLACE.search(s) and rank != f.last_rank:
            out.append(f"{who} last place (is {rank})")
    if who in f.st:
        mine = f.st[who]["w"]
        true_gaps = {abs(mine - r["w"]) for r in f.st.values()}  # vs anyone, incl. leader and 4th
        for m in _GAP.finditer(s):
            n = _n(m.group(1))
            tail = s[m.end():m.end() + 40].lower()
            if re.search(r"\b(first|the lead|1st|top|leader)\b", tail):
                ok = n == f.leader_w - mine or (mine == f.leader_w and n == mine - sorted(
                    (r["w"] for r in f.st.values()), reverse=True)[1])
            elif re.search(r"\b(4th|fourth|playoff|cut|bubble|top four|top 4)\b", tail) and f.cut_w is not None:
                ok = n == abs(f.cut_w - mine)
            else:
                ok = n in true_gaps
            if not ok:
                out.append(f"{who} gap {n}")
    return out


def member_claim_check(ctx: dict, question: str = "", lore: dict | None = None, picky: dict | None = None,
                       monday_window: bool = False, card: str = "", log=None):
    f = Facts(ctx, lore, picky, monday_window)
    if not f.st:
        return None
    card_recs = set(re.findall(r"\b\d{1,2}-\d{1,2}\b", card or ""))

    def ok(sent: str) -> bool:
        p = problems(sent, f, card_recs)
        if p and log:
            log.info("member claim dropped (%s): %r", "; ".join(p), sent[:100])
        return not p
    return ok


# ---- wrong premises -----------------------------------------------------------------------------------------

_Q_REC = re.compile(r"\b(?:i'?m|i am|im|i'?ve gone|i went|i sit at|i'?m sitting at|at|with my|my)\s+(\d{1,2})\s*-\s*(\d{1,2})\b", re.I)
_Q_PLACE = re.compile(r"\b(?:i'?m|i am|im|i'?m sitting|i sit)\s+(?:in\s+)?(?:(\d{1,2})(?:st|nd|rd|th)|(" + "|".join(ORD) + r")|(last))\b", re.I)
_Q_TOOK = re.compile(r"\b(?i:i|me)\s+(?i:took|take|picked|pick|had|went with|rode|backed)\s+([A-Z][\w&'.]*(?:\s+[A-Z][\w&'.]*){0,2})")
_Q_COVER = re.compile(r"\b([A-Z][\w&'.]*(?:\s+[A-Z][\w&'.]*){0,2})\s+(?:covered|didn'?t cover|failed to cover)\b")
_Q_LINE = re.compile(r"\b([A-Z][\w&'.]*(?:\s+[A-Z][\w&'.]*){0,2})\s+([+-]\d{1,2}(?:\.5)?)\b")


def _team_in(name: str, teams: list[str]) -> str | None:
    n = name.strip().lower()
    for t in sorted(teams, key=len, reverse=True):
        if n == t.lower() or n.endswith(t.lower()) or t.lower() == n.split(" ")[-1]:
            return t
    return None


def premise_facts(ctx: dict, question: str, same_team=None) -> list[str]:
    """False claims in the sender's question, each with the truth, e.g.
    'they say they're 33-15; their real season record is 30-18'. [] when nothing false is asserted."""
    q = question or ""
    st = {r["team"]: r for r in ctx.get("standings_ytd") or []}
    me = (ctx.get("you") or {}).get("team_name")
    same = same_team or (lambda a, b: (a or "").lower() == (b or "").lower())
    out = []
    if me in st:
        row = st[me]
        true_season = f"{row['w']}-{row['l']}"
        weekly = ctx.get("weekly_records", {}).get(me) or {}
        true_any = {true_season} | {f"{v[0]}-{v[1]}" for v in weekly.values()}
        for m in _Q_REC.finditer(q):
            r = f"{int(m.group(1))}-{int(m.group(2))}"
            if r not in true_any:
                out.append(f"they say they're {r}, which is FALSE: their real season record is {true_season} "
                           f"({_ordinal(row['rank'])} place)")
        for m in _Q_PLACE.finditer(q):
            last_rank = max(r["rank"] for r in st.values())
            if m.group(3):
                claim = last_rank
            else:
                claim = int(m.group(1)) if m.group(1) else ORD[m.group(2).lower()]
            if claim != row["rank"]:
                out.append(f"they say they're in {'last' if m.group(3) else _ordinal(claim)}, which is FALSE: they're "
                           f"{_ordinal(row['rank'])} at {true_season}")
    lw = ctx.get("last_week") or {}
    lw_games = lw.get("games") or []
    teams = [x for g in lw_games for x in (g["away"], g["home"])]
    if me and re.search(r"\blast week\b|\bwhy did i\b", q, re.I):
        for m in _Q_TOOK.finditer(q):
            t = _team_in(m.group(1), teams)
            if not t:
                continue
            g = next(g for g in lw_games if t in (g["away"], g["home"]))
            pk = (g.get("picks") or {}).get(me)
            if pk and not same(pk, t):
                out.append(f"they say they took {t} last week, which is FALSE: they took {pk} "
                           f"({g['away']} at {g['home']})")
    for m in _Q_COVER.finditer(q):
        t = _team_in(m.group(1), teams)
        if not t:
            continue
        g = next(g for g in lw_games if t in (g["away"], g["home"]))
        cov = g.get("covered")
        said_cov = not re.search(r"didn'?t|failed", m.group(0), re.I)
        if cov and (same(cov, t) != said_cov):
            out.append(f"they say {t} {'covered' if said_cov else 'did not cover'} last week, which is FALSE: "
                       f"{cov} covered ({g['away']} {g.get('away_pts')}, {g['home']} {g.get('home_pts')})")
    for g in lw_games:
        if g.get("away_pts") is None or not (re.search(re.escape(g["away"]), q, re.I) or re.search(re.escape(g["home"]), q, re.I)):
            continue
        a, h = int(g["away_pts"]), int(g["home_pts"])
        for x, y in re.findall(r"\b(\d{1,2})\s*-\s*(\d{1,2})\b", q):
            if {int(x), int(y)} != {a, h} and max(int(x), int(y)) >= 7 and re.search(r"\b(ended|final|won|lost|score)\b", q, re.I):
                out.append(f"they say it ended {x}-{y}, which is FALSE: final was {g['away']} {a}, {g['home']} {h}")
    pool = ctx.get("games_this_week") or []
    pteams = [x for g in pool for x in (g["away"], g["home"])]
    if re.search(r"\b(pool|league|our line|the line|this week)\b", q, re.I):
        for m in _Q_LINE.finditer(q):
            t = _team_in(m.group(1), pteams)
            if not t:
                continue
            g = next(g for g in pool if t in (g["away"], g["home"]))
            if g.get("line") is None:
                continue
            true = float(g["line"]) if t == g["home"] else -float(g["line"])
            if float(m.group(2)) != true:
                out.append(f"they say {t} {m.group(2)} in the pool, which is FALSE: the league line is {t} {true:+g}")
    return out


def _ordinal(n: int) -> str:
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def premise_note(ctx: dict, question: str, same_team=None) -> str:
    facts = premise_facts(ctx, question, same_team)
    if not facts:
        return ""
    return ("PREMISE CHECK (code-verified; correct it first, in one short plain clause, then answer; never repeat "
            "the false number as true):\n" + "\n".join(f"- {x}" for x in facts))


def premise_echo_check(ctx: dict, question: str, same_team=None):
    """Drop a reply sentence that repeats a false premise value as if true."""
    bad_vals = []
    for x in premise_facts(ctx, question, same_team):
        m = re.search(r"they say (?:they're |it ended |they're in |they took |)([^,]+?)(?:,| last week| in the pool)", x)
        if m:
            bad_vals.append(m.group(1).strip())
    if not bad_vals:
        return None

    def ok(sent: str) -> bool:
        if _NEG.search(sent) or re.search(r"\b(actually|really|real|true|try|nope|no)\b", sent, re.I):
            return True
        return not any(re.search(rf"(?<![\w-]){re.escape(v)}(?![\w])", sent, re.I) for v in bad_vals)
    return ok


# ---- pool spread numbers ------------------------------------------------------------------------------------


def line_values(ctx: dict, cards: dict | None = None, corpus: str = "") -> dict[str, set[float]]:
    """pool team -> every signed number that's true for it: the league line, the market now/open from its game
    card, and any signed number printed next to that team in the prompt (past lines in lore/results rows)."""
    out: dict[str, set[float]] = {}
    for g in ctx.get("games_this_week") or []:
        home, away = g["home"], g["away"]
        vals = []
        if g.get("line") is not None:
            vals.append(float(g["line"]))
        c = (cards or {}).get(str(g.get("id"))) or (cards or {}).get(g.get("id")) or {}
        for k in ("home_now", "home_open"):
            v = (c.get("market") or {}).get(k)
            if isinstance(v, (int, float)):
                vals.append(float(v))
        out.setdefault(home, set()).update(vals)
        out.setdefault(away, set()).update(-v for v in vals)
    for team in list(out):
        for m in re.finditer(rf"{re.escape(team)}\s*\(?([+-]\d{{1,2}}(?:\.5)?)\b", corpus or "", re.I):
            out[team].add(float(m.group(1)))
    return out


def line_num_check(ctx: dict, cards: dict | None = None, corpus: str = "", log=None):
    """A signed number right after a pool team ('Iowa -6.5') must be true for that team."""
    vals = line_values(ctx, cards, corpus)
    if not vals:
        return None

    def ok(sent: str) -> bool:
        for team, true in vals.items():
            for m in re.finditer(rf"(?<!\w){re.escape(team)}\s*\(?([+-]\d{{1,2}}(?:\.5)?)\b", sent, re.I):
                if float(m.group(1)) not in true and not _NEG.search(sent):
                    if log:
                        log.info("line check dropped (%s %s, true %s): %r", team, m.group(1), sorted(true), sent[:100])
                    return False
        return True
    return ok
