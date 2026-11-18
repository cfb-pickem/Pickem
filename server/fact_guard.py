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

import datetime as dt
import re

ORD = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7, "eighth": 8,
       "ninth": 9, "tenth": 10, "eleventh": 11, "twelfth": 12, "thirteenth": 13, "fourteenth": 14,
       "fifteenth": 15, "sixteenth": 16}
_REC = re.compile(r"(?<![\d.$:/-])(\d{1,2})\s*-\s*(\d{1,2})(?!\d|\.\d|:|/)")
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
# 'you're 1-3 on Iowa picks' / 'picking against Northwestern' / 'in Texas games': a W-L about one school
_SCHOOL_Q = re.compile(r"\b(on|in|with|taking|backing|picking|picking against|against|fading|vs\.?)\s+(?:the\s+)?"
                       r"([A-Z][\w&'.]+(?:\s[A-Z][\w&'.]+)?)(?:'s)?\s+(?:games?|picks?)\b"
                       r"|\b(picking against|against|fading|taking|backing)\s+(?:the\s+)?([A-Z][\w&'.]+(?:\s[A-Z][\w&'.]+)?)\b")
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
        self.lore_lines: dict[str, list[str]] = {m: [ln for ln in lines if ln.startswith(m)]
                                                 for m, lines in ((lore or {}).get("members") or {}).items()}
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


_SEG = re.compile(r",\s*(?=(?:while|but|and|meanwhile|whereas)\b)|;\s*|\s+(?=while\b)")


def _owners(s: str, f: Facts) -> dict[str, str]:
    """W-L -> the member (or the sender for you/your) named closest before it in the sentence."""
    marks = [(m.start(), name) for name in f.members for m in re.finditer(rf"(?<!\w){re.escape(name)}(?!\w)", s, re.I)]
    if f.me:
        marks += [(m.start(), f.me) for m in _YOU.finditer(s)]
    out = {}
    for m in _REC.finditer(s):
        before = [x for x in marks if x[0] < m.start()]
        if before:
            out[f"{int(m.group(1))}-{int(m.group(2))}"] = max(before)[1]
    return out


def problems(s: str, f: Facts, card_recs: set[str] = frozenset()) -> list[str]:
    """Each clause on its own ('You're 30-18, while I'm 0-3': the 0-3 is Picky's)."""
    if _NEG.search(s):
        return []  # corrections ("you're not 33-15") and questions back are fine
    segs = [x for x in _SEG.split(s) if x and x.strip()]
    if len(segs) <= 1:
        return _problems(s, f, card_recs)
    out, last_who = [], None
    for seg in segs:
        who = subject(seg, f)
        if who is None and last_who and not _ME.search(seg) and _REC.search(seg):
            seg = f"{last_who} {seg}"  # 'Bravo is 2nd, and 28-20': the clause keeps its subject
        out += _problems(seg, f, card_recs)
        last_who = who or last_who
    return out


def _problems(s: str, f: Facts, card_recs: set[str] = frozenset()) -> list[str]:
    out = []
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
    school = _SCHOOL_Q.search(s)
    if recs and school and who in f.st:
        verb, name = (school.group(1) or school.group(3) or ""), (school.group(2) or school.group(4) or "")
        sch = name.lower()
        against = bool(re.search(r"\b(against|fad\w*|vs\.?)\b", verb, re.I))
        for r in recs:
            lines = [ln for ln in f.lore_lines.get(who, []) if sch in ln.lower() and r in ln]
            good = any(bool(re.search(r"against|fading|picked against", ln, re.I)) == against or
                       re.search(r"whichever side", ln, re.I) for ln in lines)
            if not good:
                out.append(f"{who} {r} on {name} (no such lore line)")
        return out
    if recs and not _NOT_PICK_REC.search(s):
        lab = f.labeled(who, s)
        owners = _owners(s, f)
        for r in recs:
            if owners.get(r, who) != who:
                o = owners[r]  # 'Closest to you (21-23): TG 2 back': the 21-23 is the sender's
                if o in f.st and r not in f.any_true(o) and r not in card_recs:
                    out.append(f"{o} {r}")
                continue

            if r in card_recs and not lab:
                continue
            ok = (r in lab) if lab else (r in f.any_true(who))
            if not ok:
                out.append(f"{who} {r} (true {sorted(x for x in (lab or {f.season(who)}) if x)})")
    rank = f.st[who]["rank"] if who in f.st else None
    if rank is not None and not _WEEKN.search(s) and not _LASTWK.search(s) and not _THISWK.search(s):
        for m in _PLACE.finditer(s):
            # 'ahead of 5th place', 'back of first', '(4th place has 27)': another spot, not the subject's place
            if re.search(r"(\b(of|on|from|than|to|behind|over|past|for the|ahead of|clear of)|\()\s*$", s[:m.start()], re.I) and \
                    not re.search(r"\btied for\s*$", s[:m.start()], re.I):
                continue
            if re.match(r"\s*(has|had|is at|sits at|is on)\b", s[m.end():], re.I):
                continue
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
    pp = ctx.get("picky_player") or {}
    if pp.get("season") and not re.search(r"\b(i'?m|i am|im)\b", q, re.I):  # 'you're 5-3 this year' said to Picky
        ps = f"{pp['season'][0]}-{pp['season'][1]}"
        ok_p = {ps} | ({f"{pp['last_week'][0]}-{pp['last_week'][1]}"} if pp.get("last_week") else set())
        for m in re.finditer(r"\byou(?:'re| are| were| went|re)?\s+(?:at\s+)?(\d{1,2})\s*-\s*(\d{1,2})\b", q, re.I):
            r = f"{int(m.group(1))}-{int(m.group(2))}"
            if r not in ok_p:
                out.append(f"they say Picky is {r}, which is FALSE: Picky (you, the bot) is {ps} on the season")
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
    for g in (ctx.get("last_week") or {}).get("games") or []:  # last week's pool games: their league line
        if g.get("line") is not None and g["home"] not in out and g["away"] not in out:
            out.setdefault(g["home"], set()).add(float(g["line"]))
            out.setdefault(g["away"], set()).add(-float(g["line"]))
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
            for m in re.finditer(rf"(?<!\w){re.escape(team)}(?:\s*\(?|([^.;]{{0,40}}?)\bas an? )([+-]\d{{1,2}}(?:\.5)?)\b", sent, re.I):
                ok_vals = true
                if m.group(1) is not None:  # 'North Texas lost to UTSA 16-35 as a -2.5 favorite': any team named before it
                    before = sent[:m.start(2)]
                    ok_vals = set().union(*[v for o, v in vals.items() if re.search(rf"(?<!\w){re.escape(o)}(?!\w)", before, re.I)])
                if float(m.group(2)) not in ok_vals and not _NEG.search(sent):
                    if log:
                        log.info("line check dropped (%s %s, true %s): %r", team, m.group(2), sorted(true), sent[:100])
                    return False
        return True
    return ok


# ---- team records, scores, ranks (CFBD cache + the prompt) ----------------------------------------------------

_TEAM_RE_CACHE: dict = {}


def _team_regex(idx: dict):
    key = id(idx)
    if key not in _TEAM_RE_CACHE:
        names = sorted({v["name"] for v in (idx.get("teams") or {}).values() if v.get("name") and v.get("games")},
                       key=len, reverse=True)
        _TEAM_RE_CACHE.clear()
        _TEAM_RE_CACHE[key] = (re.compile(r"(?<![\w&])(" + "|".join(re.escape(n) for n in names) + r")(?![\w&])"),
                               {v["name"]: v for v in (idx.get("teams") or {}).values() if v.get("name")})
    return _TEAM_RE_CACHE[key]


def _near(corpus: str, team: str, val: str, span: int = 90) -> bool:
    for m in re.finditer(rf"(?<![\w&]){re.escape(team)}(?![\w&])", corpus or ""):
        if val in corpus[max(0, m.start() - span): m.end() + span]:
            return True
    return False


def _bump(rec: str | None, won: bool) -> str | None:
    m = re.fullmatch(r"(\d+)-(\d+)", rec or "")
    if not m:
        return None
    w, l = int(m.group(1)), int(m.group(2))
    return f"{w + 1}-{l}" if won else f"{w}-{l + 1}"


def finals_overrides(idx: dict | None, live: dict | None, resolve) -> dict[str, dict]:
    """Records derived from finals in live_games.json that the CFBD cache doesn't have yet (review R6).
    CFBD refreshes a few times a day; on a Saturday night a team's true record is the cached one plus
    tonight's result. A final counts as 'not in the cache' when its score isn't in that team's CFBD
    scores. Returns {cfbd name: {"record", "conf_record"?, "score"}}; the stale cached record is replaced,
    so 'Texas is 8-2' after Texas went to 9-2 tonight gets cut and '9-2' passes."""
    out: dict[str, dict] = {}
    if not idx or not live:
        return out
    rec_ts = (idx.get("fetched") or {}).get("records") or 0
    games = live.get("games") or {}
    for g in (games.values() if isinstance(games, dict) else games):
        if g.get("state") != "post":
            continue
        a, h = g.get("away_score"), g.get("home_score")
        if a is None or h is None or a == h:
            continue
        end = g.get("latest_wallclock")
        try:
            end_ts = dt.datetime.fromisoformat(end.replace("Z", "+00:00")).timestamp() if end else (g.get("ts") or 0)
        except ValueError:
            end_ts = g.get("ts") or 0
        if rec_ts and end_ts and rec_ts > end_ts + 1800:
            continue  # CFBD records were pulled well after this final: trust them (never double-count)
        sides = []
        for side, pts, opp in (("away", a, h), ("home", h, a)):
            t = resolve((g.get(side) or {}).get("name") or "", idx)
            sides.append((t, pts, opp))
        conf_game = all(t and t.get("conference") for t, *_ in sides) and sides[0][0].get("conference") == sides[1][0].get("conference")
        for t, pts, opp in sides:
            if not t or not t.get("name"):
                continue
            if (pts, opp) in {tuple(x) for x in t.get("scores") or []}:
                continue  # the cache already has this game
            o = {"record": _bump(t.get("record"), pts > opp), "score": f"{pts}-{opp}"}
            if conf_game and t.get("conf_record"):
                o["conf_record"] = _bump(t.get("conf_record"), pts > opp)
            out[t["name"]] = o
    return out


def team_true(t: dict, over: dict | None = None) -> tuple[set[str], set[str]]:
    """(true W-Ls, true scores 'a-b' both orders) for one CFBD team. `over` = finals_overrides()[name]."""
    over = over or {}
    recs = {over.get("record") or t.get("record"), over.get("conf_record") or t.get("conf_record")}
    ats = t.get("ats")
    if ats and len(ats) >= 2:
        recs.add(f"{ats[0]}-{ats[1]}")
    scores = set()
    for a, b in list(t.get("scores") or []) + ([tuple(over["score"].split("-"))] if over.get("score") else []):
        if a is not None and b is not None:
            scores |= {f"{a}-{b}", f"{b}-{a}"}
    return {r for r in recs if r}, scores


def team_fact_check(idx: dict | None, corpus: str = "", members: list[str] | None = None, log=None,
                    finals: dict | None = None):
    """A W-L, score or 'No. N' rank tied to an FBS team must be true per the CFBD cache (record, conference
    record, ATS, a game score) or printed next to that team in the prompt. 'Texas is 9-1' with Texas 8-2,
    an invented 'No. 5 Texas', a wrong final: dropped. `finals` = finals_overrides(): today's finals the
    CFBD cache hasn't caught up on yet."""
    if not idx:
        return None
    rx, by_name = _team_regex(idx)
    mem = [m.lower() for m in members or []]

    def ok(sent: str) -> bool:
        if _NEG.search(sent) or any(m in sent.lower() for m in mem):
            return True  # member W-Ls belong to member_claim_check
        if re.search(r"\b(blackjack|chips?|slots?|in \w+ games|head to head|h2h|over the last|since 20\d\d|all-?time|"
                     r"series|bowl|history|in 20\d\d|20\d\d season)\b", sent, re.I):
            return True  # history / splits / series: not this season's cache
        teams = [(m.group(1), m.start(), m.end()) for m in rx.finditer(sent)]
        if not teams:
            return True
        for m in _REC.finditer(sent):
            val = f"{int(m.group(1))}-{int(m.group(2))}"
            near = min(teams, key=lambda t: min(abs(m.start() - t[2]), abs(t[1] - m.end())))
            if min(abs(m.start() - near[2]), abs(near[1] - m.end())) > 40:
                continue
            ok_vals = set()
            for name, *_ in teams:
                recs, scores = team_true(by_name.get(name) or {}, (finals or {}).get(name))
                ok_vals |= recs | scores
            if val in ok_vals or any(_near(corpus, n, val) for n, *_ in teams):
                continue
            if log:
                log.info("team fact dropped (%s %s): %r", near[0], val, sent[:100])
            return False
        for m in re.finditer(r"(?:\bNo\.\s?|#)(\d{1,2})\s+(?:ranked\s+)?([A-Z][\w&'.]*(?:\s+[A-Z][\w&'.]*){0,2})", sent):
            name = next((n for n, *_ in teams if m.group(2).startswith(n)), None)
            if name and not _near(corpus, name, f"No. {m.group(1)}") and not _near(corpus, name, f"#{m.group(1)}") \
                    and not _near(corpus, name, f"{m.group(1)}. {name}", 5):
                if log:
                    log.info("team rank dropped (%s No. %s): %r", name, m.group(1), sent[:100])
                return False
        return True
    return ok


# ---- other members' pick claims -------------------------------------------------------------------------------

_PICK_VERB = r"(?:took|takes|had|has|picked|is on|was on|on|rode|backed|went with|is riding|faded|is fading)"


def pick_side_check(ctx: dict, same_team=None, log=None):
    """'Bravo took North Texas' / 'Bravo's on Iowa': a member tied to a side of a game whose picks are
    revealed (last week, this week after kickoff) must be on that side."""
    same = same_team or (lambda a, b: (a or "").lower() == (b or "").lower())
    members = sorted((r["team"] for r in ctx.get("standings_ytd") or []), key=len, reverse=True)
    games = []
    for g in (ctx.get("last_week") or {}).get("games") or []:
        games.append((g["away"], g["home"], g.get("picks") or {}))
    rev = ctx.get("revealed_picks_this_week") or {}
    by_id = {g["id"]: g for g in ctx.get("games_this_week") or []}
    tw: dict = {}
    for m, plist in rev.items():
        for p in plist:
            g = by_id.get(p.get("game_id"))
            if g:
                tw.setdefault((g["away"], g["home"]), {})[m] = p.get("pick")
    games += [(a, h, picks) for (a, h), picks in tw.items()]
    if not games or not members:
        return None

    def ok(sent: str) -> bool:
        if _NEG.search(sent):
            return True
        for m in members:
            mm = re.search(rf"(?<!\w){re.escape(m)}(?:'s)?\s+{_PICK_VERB}\s+(?:the\s+)?([A-Z][\w&'.]*(?:\s+[A-Z][\w&'.]*){{0,2}})", sent)
            if not mm:
                continue
            said = mm.group(1)
            for a, h, picks in games:
                hit = next((x for x in (a, h) if said.lower().startswith(x.lower())), None)
                if hit and picks.get(m) and not same(picks[m], hit) and not re.search(r"\bfad", mm.group(0)):
                    if log:
                        log.info("pick side dropped (%s took %s, said %s): %r", m, picks[m], hit, sent[:100])
                    return False
        return True
    return ok


# ---- coaching moves -------------------------------------------------------------------------------------------

_COACH_MOVE = re.compile(r"\b(fired|firing|got canned|let go|parted ways|dismissed|axed|hot seat|on the hot seat|"
                         r"hired|resign\w*|stepp(ed|ing) down|out as (head )?coach|interim)\b", re.I)
_NAME = re.compile(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z']+)+)\b")


def coach_move_check(corpus: str = "", log=None):
    """A firing / hiring / hot-seat claim about a named person must name someone the prompt carries (news,
    coach note, insider notes). 'Whittingham fired' with no such line: dropped."""
    low = (corpus or "").lower()

    def ok(sent: str) -> bool:
        if not _COACH_MOVE.search(sent):
            return True
        names = [n for n in _NAME.findall(sent) if n.split()[0] not in ("The", "College", "Notre", "Big", "Ohio", "Penn",
                                                                       "Texas", "Oregon", "Boise", "North", "South")]
        segs = re.split(r"[.;\n]|\s-\s", low)
        for n in names:
            last = n.split()[-1].lower()
            wins = [w for w in segs if last in w]
            backed = any(_COACH_MOVE.search(w) and not re.search(r"still the coach|no firing|not fired|isn'?t on", w)
                         for w in wins)
            if len(last) >= 4 and not backed:
                if log:
                    log.info("coach move dropped (%s not in the prompt): %r", n, sent[:100])
                return False
        return True
    return ok


# ---- stats, ranks, projections tied to a team (prompt proximity) ------------------------------------------------

_RANKY = re.compile(r"(?:#|No\.\s?)(\d{1,3})\b|\b(?:ranked|ranks|ranking)\s+(?:No\.\s?|#)?(\d{1,3})\b"
                    r"|\b(\d{1,3})(?:st|nd|rd|th)\s+(?:in|nationally|overall|best|worst|in the country|in fbs)\b")
_STATY = re.compile(r"(?<![\w.+-])(\d{1,3}(?:\.\d+)?)\s*(%|percent\b)|(?<![\w+-])\$\s?(\d[\d,.]*)\s*(million|billion|m\b)?"
                    r"|\b(\d+(?:\.\d+)?)\s+(million|billion)\b|(?<![\w.+-])(\d{1,2}\.\d)\b(?!\s*-)"
                    r"|\b(\d{1,2})\s+days?\s+(?:of\s+)?(?:rest|off)\b", re.I)
_PROJ = re.compile(r"\b(?:projects?|projected|projection|model|SP\+|FPI|SRS|EPA)\b[^.]{0,40}?\bby\s+(?:about\s+)?(\d{1,2}(?:\.\d)?)\b",
                   re.I)


def _has_tok(text: str, token: str) -> bool:
    return bool(re.search(rf"(?<![\d.]){re.escape(token.strip())}(?![\d]|\.\d)", text or "", re.I))


def _in_corpus_near(corpus: str, names: list[str], token: str, span: int = 160) -> bool:
    if not names:
        return _has_tok(corpus, token)
    for n in names:
        for m in re.finditer(rf"(?<![\w&]){re.escape(n)}(?![\w&])", corpus or ""):
            if _has_tok(corpus[max(0, m.start() - span): m.end() + span], token):
                return True
    return False


def stat_rank_check(idx: dict | None, corpus: str = "", log=None):
    """A rank (#N, No. N, 'Nth in'), a percent, a decimal stat, a dollar amount, 'projects X by N' or 'N days
    of rest' in a sentence about a team must be printed near that team in the prompt (card, intel, news).
    Picky's own arithmetic and memory don't count: 'Wake Forest SP+ #7', 'hitting 44% of FGs', 'a $50
    million buyout' with no such line get dropped."""
    if not corpus:
        return None
    rx, _ = _team_regex(idx) if idx else (None, {})

    def ok(sent: str) -> bool:
        if _NEG.search(sent) and not re.search(r"\b(not|isn'?t)\b.{0,30}\b(top|best|worst)\b", sent, re.I):
            return True
        names = sorted({m.group(1) for m in rx.finditer(sent)}, key=len, reverse=True) if rx else []
        toks = []
        for m in _RANKY.finditer(sent):
            n = next(x for x in m.groups() if x)
            if m.group(1) and re.search(r"\b(seed|pick|overall pick|draft)\b", sent[m.end():m.end() + 12], re.I):
                continue
            toks.append((m.group(0), [f"#{n}", f"No. {n}", f"{n}th", f"{n}st", f"{n}nd", f"{n}rd", f"rank {n}",
                                       f"ranked {n}", f"({n})"]))
        for m in _STATY.finditer(sent):
            v = next(x for x in m.groups() if x)
            if m.group(7) and re.search(r"\bby\s*$", sent[:m.start()], re.I):
                continue  # 'missed (by 21.5)': a cover margin, result_check's job
            if m.group(8):  # 'N days of rest'
                toks.append((m.group(0), [f"{v} days", f"{v}-day", f"{v} day"]))
            else:
                toks.append((m.group(0), [v, v.replace(",", "")]))
        for m in _PROJ.finditer(sent):
            toks.append((m.group(0), [f"by {m.group(1)}", f"by about {m.group(1)}"]))
        for raw, forms in toks:
            if not any(_in_corpus_near(corpus, names, f) for f in forms):
                if log:
                    log.info("stat/rank dropped (%r, teams %s): %r", raw, names, sent[:100])
                return False
        return True
    return ok


# ---- ATS split labels (game cards) ------------------------------------------------------------------------------

_CARD_ATS = re.compile(r"([A-Z][\w&'.]*(?: [A-Z][\w&'.]*){0,3})(?: \(\d+-\d+\))?: ATS (\d+-\d+(?:-\d+)?) \(fav (\d+-\d+(?:-\d+)?), "
                       r"dog (\d+-\d+(?:-\d+)?), home (\d+-\d+(?:-\d+)?), road (\d+-\d+(?:-\d+)?)\)")
_SPLIT_WORDS = (("fav", r"as (?:a |the )?fav\w*|when favored|laying points|as chalk"),
                ("dog", r"as (?:a |an |the )?(?:under)?dog|getting points|when getting"),
                ("home", r"at home|home (?:games?|split)"), ("road", r"on the road|road (?:games?|split)|away from home"))


def ats_split_check(corpus: str = "", log=None):
    """'Georgia is 8-2 ATS as a favorite' when its card says fav 7-2 (8-2 is overall): a W-L labeled with a
    split must be that split from the team's card line."""
    splits = {}
    for m in _CARD_ATS.finditer(corpus or ""):
        team = m.group(1).split(": ")[-1].strip()
        splits[team] = {"all": m.group(2), "fav": m.group(3), "dog": m.group(4), "home": m.group(5), "road": m.group(6)}
    if not splits:
        return None

    def ok(sent: str) -> bool:
        if _NEG.search(sent):
            return True
        for team, sp in splits.items():
            tm = re.search(rf"(?<![\w&]){re.escape(team)}(?![\w&])", sent)
            if not tm:
                continue
            for rm in re.finditer(r"(?<![\d.-])(\d{1,2}-\d{1,2}(?:-\d)?)(?!\d)", sent):
                tail = sent[rm.end(): rm.end() + 40]
                head = sent[max(0, rm.start() - 30): rm.start()]
                if not re.search(r"\bATS\b|against the spread|cover", tail + head, re.I):
                    continue
                label = next((k for k, pat in _SPLIT_WORDS if re.search(pat, tail, re.I)), None)
                want = sp[label] if label else None
                if label and rm.group(1) != want:
                    if log:
                        log.info("ATS split dropped (%s %s %s, card %s): %r", team, rm.group(1), label, want, sent[:100])
                    return False
        return True
    return ok


# ---- attributed statements --------------------------------------------------------------------------------------

_SAYS = re.compile(r"\b(said|says|admitted|admits|told|claims?|claimed|insisted|confirmed|announced|tweeted|posted)\b", re.I)


def attribution_check(corpus: str = "", log=None):
    """'Kiffin admitted he forgot his Tennessee history' / 'Ferentz said Nov. 10 ...': a statement put in a
    named person's mouth needs a prompt line where that person says something (news, intel, insider notes)."""
    segs = [x for x in re.split(r"[.;\n]", (corpus or "").lower()) if x.strip()]

    def ok(sent: str) -> bool:
        m = _SAYS.search(sent)
        if not m or _NEG.search(sent):
            return True
        before = sent[:m.start()]
        names = re.findall(r"\b([A-Z][a-z'A-Z]+(?:\s+[A-Z][a-z'A-Z]+)*)\b", before)
        names = [n for n in names if n.split()[-1].lower() not in {"i", "the", "he", "she", "they", "espn", "cbs", "per"}]
        if not names:
            return True
        last = names[-1].split()[-1].lower()
        if len(last) < 4:
            return True
        if any(last in x and _SAYS.search(x) for x in segs):
            return True
        if log:
            log.info("attribution dropped (%s): %r", names[-1], sent[:100])
        return False
    return ok


# ---- pool results: cover margins and winners (last week's graded games) ------------------------------------------


def _lw_games(ctx: dict) -> list[dict]:
    out = []
    for g in (ctx.get("last_week") or {}).get("games") or []:
        if g.get("away_pts") is None or g.get("line") is None:
            continue
        a, h, ln = int(g["away_pts"]), int(g["home_pts"]), float(g["line"])
        cm_home = h + ln - a  # home's cover margin (push goes to away, set_winner_on_final)
        out.append({"away": g["away"], "home": g["home"], "a": a, "h": h, "line": ln,
                    "winner": g["home"] if h > a else g["away"] if a > h else None,
                    "cover": {g["home"]: cm_home, g["away"]: -cm_home}})
    return out


def result_check(ctx: dict, log=None):
    """For last week's pool games: 'X covered by N' / 'missed (the cover) by N' must be X's real cover margin
    (not the game margin), 'X won/beat Y' must name the winner, 'X won 41-46' can't hand the win to the loser."""
    games = _lw_games(ctx)
    if not games:
        return None

    def ok(sent: str) -> bool:
        if _NEG.search(sent):
            return True
        sent = re.sub(r"(?<=\d)\.(?=\d)", "_", sent)  # '+13.5' must not end a [^.;] span
        for g in games:
            named = [t for t in (g["away"], g["home"]) if re.search(rf"(?<!\w){re.escape(t)}(?!\w)", sent, re.I)]
            if not named:
                continue
            for t in named:
                for m in re.finditer(rf"(?<!\w){re.escape(t)}(?!\w)[^.;]{{0,50}}?\bcover(?:ed|s)?\b[^.;]{{0,30}}?\bby\s+(\d+(?:_5)?)\b",
                                     sent, re.I):
                    n = float(m.group(1).replace("_", "."))
                    if g["cover"][t] > 0 and abs(n - g["cover"][t]) > 0.01:
                        return _drop(log, f"{t} covered by {n:g} (true {g['cover'][t]:g})", sent)
                for m in re.finditer(rf"(?<!\w){re.escape(t)}(?!\w)[^.;]{{0,50}}?\bmiss(?:ed|es)?\b[^.;]{{0,25}}?\bby\s+(\d+(?:_5)?)\b",
                                     sent, re.I):
                    n = float(m.group(1).replace("_", "."))
                    if g["cover"][t] < 0 and abs(n + g["cover"][t]) > 0.01:
                        return _drop(log, f"{t} missed by {n:g} (true {-g['cover'][t]:g})", sent)
                w = re.search(rf"(?<!\w){re.escape(t)}(?!\w)\s+(?:won|beat|took down|knocked off|edged|outlasted)\b", sent, re.I)
                if w and g["winner"] and t != g["winner"] and not re.search(r"\bcover", sent[w.end():w.end() + 25], re.I):
                    return _drop(log, f"{t} won (winner {g['winner']})", sent)
                lost = re.search(rf"(?<!\w){re.escape(t)}(?!\w)\s+(?:lost|fell)\b", sent, re.I)
                if lost and g["winner"] == t:
                    return _drop(log, f"{t} lost (they won)", sent)
        return True
    return ok


def _drop(log, why: str, sent: str) -> bool:
    if log:
        log.info("result check dropped (%s): %r", why, sent[:100])
    return False


# ---- casino claims ----------------------------------------------------------------------------------------------

_CASINO = re.compile(r"\b(blackjack|the pit|chips?|slots?|hard \d{1,2}|soft \d{1,2}|doubled|double down|split|"
                     r"hit on|stood on|stand on|busted|bust(ed)? out|hands?)\b", re.I)


def casino_check(ctx: dict, lore: dict | None, log=None):
    """'You doubled on a hard 12 and busted' to someone who never did (fact exam 2026-11-17: Blinkstradamus's
    blunder handed to Wedge): a casino claim about a member must come from that member's casino lore lines."""
    cas = ((lore or {}).get("casino") or {}).get("members") or {}
    st = ctx.get("standings_ytd") or []
    if not st:
        return None
    members = sorted((r["team"] for r in st), key=len, reverse=True)
    me = (ctx.get("you") or {}).get("team_name")

    def ok(sent: str) -> bool:
        if not _CASINO.search(sent) or _NEG.search(sent):
            return True
        names = [m for m in members if _mentions(sent, m)]
        who = names[0] if len(names) == 1 else (me if not names and _YOU.search(sent) else None)
        if not who:
            return True
        lines = " ".join(cas.get(who) or []).lower()
        for m in re.finditer(r"\b(hard|soft)\s+(\d{1,2})\b", sent, re.I):
            if f"{m.group(1).lower()} {m.group(2)}" not in lines:
                return _cdrop(log, f"{who} {m.group(0)}", sent)
        for verb, key in (("doubled", "doubled"), ("split", "split"), (r"hit on", "hit on"), (r"stood on|stand on", "stood on"),
                          (r"busted|bust", "bust")):
            if re.search(rf"\b({verb})\b", sent, re.I) and key not in lines:
                return _cdrop(log, f"{who} {verb}", sent)
        for m in re.finditer(r"\b(\d{1,3}(?:,\d{3})*)\s+chips\b", sent, re.I):
            if m.group(1) not in lines:
                return _cdrop(log, f"{who} {m.group(0)}", sent)
        return True
    return ok


def _cdrop(log, why: str, sent: str) -> bool:
    if log:
        log.info("casino check dropped (%s): %r", why, sent[:100])
    return False


# ---- results claimed for games that haven't been played ----------------------------------------------------------


def unplayed_result_check(ctx: dict, log=None):
    """'You hit the Minnesota +2.5' when Northwestern-Minnesota kicks off Saturday (fact exam 2026-11-17, a
    follow-up that pasted this week's games into week 9): a hit/miss/cover/win/loss for a pool team whose game
    hasn't started is dropped. Teams that also played last week are left alone (that result is real)."""
    lw_teams = {x for g in (ctx.get("last_week") or {}).get("games") or [] for x in (g["away"], g["home"])}
    pending = [x for g in ctx.get("games_this_week") or [] if not g.get("winner") and not g.get("status")
               for x in (g["away"], g["home"]) if x not in lw_teams]
    if not pending:
        return None
    rx = re.compile(r"\b(hit|hits|missed|misses|covered|cashed|won|lost|beat|burned)\b[^.;]{0,25}?\b(" +
                    "|".join(re.escape(t) for t in sorted(pending, key=len, reverse=True)) + r")\b"
                    r"|\b(" + "|".join(re.escape(t) for t in sorted(pending, key=len, reverse=True)) +
                    r")\b[^.;]{0,25}?\b(covered|cashed|hit|won|lost|missed)\b", re.I)

    def ok(sent: str) -> bool:
        m = rx.search(sent)
        if m and not re.search(r"\b(if|would|will|could|needs?|to cover|gonna|going to|should|might|when)\b", sent, re.I):
            if log:
                log.info("unplayed result dropped (%s): %r", m.group(0), sent[:100])
            return False
        return True
    return ok
