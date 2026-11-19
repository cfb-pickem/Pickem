"""Big picture code lanes (plan phase 5): the coaching carousel / hot seat / career stops, and the CFP field "if it
ended today". Both read only the fact store (factstore gateway) + ctx and return factstore.answer(text, facts_used);
router.STORE_LANES lists them (test_factstore.py checks that statically). The conference race lane is
router.conf_race_text (title_race.race through factstore.conf_race).

  coach_text(ctx, q)     'who's on the hot seat', 'coaching carousel stuff for this week', 'Kiffin's history',
                         'is X going back to Y': this week's games with a coach story first (fresh slate_intel
                         notes, else news + the code-made insider wire), career stops from CFBD, the team's record
                         and ATS now, and whether it touches this week's line.
  cfp_now_text(ctx, q)   the 12-team field from the checked ESPN CFP ranking + the conference race leaders +
                         cfp_rules.json: seeds, byes, auto bids, bubble, who controls it, the next release.
"""

from __future__ import annotations

import datetime as dt
import re

import factstore

GENERIC_COACH = re.compile(r"\b(carousel|hot seat|hot-seat|on the seat|coaching (news|changes?|moves?|searche?s?|"
                           r"rumou?rs?|vacanc\w*|jobs?|openings?)|open jobs?|who('?s| is| are)\b[^?]{0,30}\b(fired|seat)|"
                           r"(any|which) (coach|coaches)|this week)\b", re.I)
RETURN_ASK = re.compile(r"\b(going back|go back|back to|return(ing)? to|head(ing|ed)? (back )?to|leav\w* for|"
                        r"take the|land(s|ing)? at|end up at)\b", re.I)


def _team_facts(name: str) -> tuple[dict, list[dict]]:
    """(team sheet with today's finals, facts for its overall record + ATS + head coach)."""
    t = factstore.team(name) or {}
    if not t:
        return {}, []
    f = []
    if t.get("record"):
        f.append({"id": f"record.{factstore.norm(name)}.overall.live", "kind": "record", "subject": t["name"],
                  "value": t["record"], "text": f"{t['name']} is {t['record']} overall",
                  "source": "CFBD records" + (" + live finals" if t.get("record_note") else ""),
                  "as_of": t.get("as_of") or ""})
    f += [x for x in t.get("facts") or [] if x["kind"] in ("ats", "coach") or x["kind"] == "rank" and x["id"].endswith(".cfp")]
    return t, f


def _game_for(team: str, rows: list[dict]) -> dict | None:
    return next((g for g in rows if team in (g["awayTeam"], g["homeTeam"])), None)


def _line_bit(g: dict) -> tuple[str, list[dict]]:
    """'LSU at Tennessee (LSU -1.5)' + the market fact."""
    mf = factstore.facts(g, ("market",))[:1]
    fav = mf[0]["text"].split(": ", 1)[1] if mf and ": " in mf[0]["text"] else None
    return f"{g['awayTeam']} at {g['homeTeam']}" + (f" ({fav})" if fav else ""), mf


# =============================================================================================================
# coach lane
# =============================================================================================================

def _named_coach(q: str, careers: dict) -> dict | None:
    t = q or ""
    full = [c for c in careers.values() if re.search(rf"\b{re.escape(c['name'])}\b", t, re.I)]
    if full:
        return full[0]
    hits = [c for c in careers.values() if c.get("last") and len(c["last"]) >= 3
            and re.search(rf"\b{re.escape(c['last'])}('s)?\b", t, re.I)]
    if not hits:
        return None
    hits.sort(key=lambda c: (c["current"], max(s["to"] for s in c["stops"])), reverse=True)
    return hits[0]


def coach_named(q: str) -> dict | None:
    """The head coach (this season or last) a text names, from the store's CFBD careers."""
    return _named_coach(q, factstore.coach_careers())


def _stops_text(c: dict) -> str:
    return c["text"].split(": ", 1)[1] if ": " in c["text"] else c["text"]


def _story(team: str, notes: list[dict], heads: list[dict], who: str | None = None, min_weight: int = 0) -> list[dict]:
    """A team's coach story, best first: the fresh coaching note, then headlines naming the coach, then the team's
    change / hot-seat headlines (insider wire too); chatter below min_weight is dropped."""
    out = [n for n in notes if factstore.norm(n.get("school", "")) == factstore.norm(team)]
    named = [x for x in heads if who and re.search(rf"\b{re.escape(who)}\b", x["title"], re.I)]
    rest = sorted((x for x in heads if team in x["teams"][:1] and x not in named and _weight(x)[0] >= max(1, min_weight)),
                  key=_weight, reverse=True)
    return out + named + rest


def _story_text(s: dict) -> str:
    return s["fact"]["text"].rstrip(".")


def coach_text(ctx: dict, q: str) -> str | None:
    careers = factstore.coach_careers()
    notes = factstore.coaching_notes()
    heads = factstore.coach_news()
    rows = factstore.upcoming()
    c = _named_coach(q, careers)
    named = [n for n in factstore.teams_named(q) if not c or n != c["school"] or not RETURN_ASK.search(q or "")]
    if c:
        return _coach_answer(c, q, named, notes, heads, rows)
    if named and not GENERIC_COACH.search(q or ""):
        t = factstore.team(named[0]) or {}
        cc = careers.get(t.get("coach") or "")
        return _coach_answer(cc, q, [], notes, heads, rows) if cc else None
    if not GENERIC_COACH.search(q or ""):
        return None  # a coach we don't have ('is Vince Lombardi getting fired?'): the full brain says so
    pool = {factstore.norm(x) for g in (ctx or {}).get("games_this_week") or [] for x in (g.get("away"), g.get("home")) if x}
    return _carousel_answer(q, notes, heads, rows, pool)


def _coach_answer(c: dict, q: str, named: list[str], notes, heads, rows) -> str | None:
    used = [{"id": f"coach.{factstore.norm(c['name'])}.career", "kind": "coach", "subject": c["name"],
             "value": c["school"], "text": c["text"], "source": "CFBD coaches", "as_of": c["as_of"]}]
    out = []
    target = next((n for n in named if n != c["school"]), None) if RETURN_ASK.search(q or "") else None
    if target:
        stop = next((s for s in c["stops"] if s["school"] == target), None)
        if stop:
            yrs = f"{stop['from']}" if stop["from"] == stop["to"] else f"{stop['from']}-{stop['to']}"
            out.append(f"{c['name']} has been {target}'s head coach before: {yrs}"
                       + (f", {stop['w']}-{stop['l']}" + (f"-{stop['t']}" if stop["t"] else "") if stop["seasons"] else "")
                       + f". His stops: {_stops_text(c)}.")
        else:
            out.append(f"{c['name']} has never been {target}'s head coach (CFBD's FBS records). His stops: {_stops_text(c)}.")
        tt, tf = _team_facts(target)
        st = _story(target, notes, heads, min_weight=2)
        if st:
            out.append(f"The {target} job: {_story_text(st[0])}.")
            used.append(st[0]["fact"])
        elif tt.get("coach"):
            out.append(f"{target} has a head coach right now: {tt['coach']}.")
            used += [x for x in tf if x["kind"] == "coach" and x["id"].endswith(".head")]
        link = [x for x in heads if re.search(rf"\b{re.escape(c['last'])}\b", x["title"], re.I) and target in x["teams"]]
        if link:
            out.append(f"Latest tying them together: {_story_text(link[0])}.")
            used.append(link[0]["fact"])
        else:
            out.append(f"Nothing in this week's headlines ties him to {target}, so that's talk, not news.")
    elif re.search(r"\b(career|stops|résumé|resume|record|history|where has he|coached where)\b", q or "", re.I) or not (
            c["current"] or c["stops"]):
        out.append(f"{c['name']}'s FBS head-coaching stops: {_stops_text(c)}.")  # the list only when asked: it read as a résumé dump
    school = c["school"] if c["current"] else None
    if school:
        t, tf = _team_facts(school)
        used += tf
        ats = (t.get("ats") or {})
        if t.get("record"):
            out.append(f"{school} is {t['record']} this season"
                       + (f" and {ats['w']}-{ats['l']}" + (f"-{ats['p']}" if ats.get("p") else "") + " ATS" if ats.get("w") is not None
                          else "") + ".")
    else:
        last = c["stops"][-1]["school"] if c["stops"] else None
        t, tf = _team_facts(last) if last else ({}, [])
        if t.get("coach") and t["coach"] != c["name"]:  # a false premise: the correction goes first
            out.insert(0, f"{c['name']} isn't {last}'s coach anymore: it's {t['coach']} now"
                          + (f", and {last} is {t['record']}" if t.get("record") else "") + ".")
            used += [x for x in tf if x["kind"] == "coach" and x["id"].endswith(".head")]
        school = last
    if not target:
        st = _story(school, notes, heads, c["last"]) if school else [x for x in heads if re.search(
            rf"\b{re.escape(c['last'])}\b", x["title"], re.I)]
        if st:
            out.append(f"Latest: {_story_text(st[0])}.")
            used.append(st[0]["fact"])
        elif re.search(r"\b(fired|firing|fire him|hot.?seat|on the seat|job)\b", q or "", re.I):
            out.append(f"Nothing in this week's coach headlines has {c['last']} on the hot seat.")
    g = _game_for(school, rows) if school and c["current"] else None
    if g:
        bit, mf = _line_bit(g)
        used += mf
        opp = g["homeTeam"] if g["awayTeam"] == school else g["awayTeam"]
        back = next((x for x in c["stops"] if x["school"] == opp), None)
        if back and not target:  # 'Kiffin going back to Knoxville': this week IS the return
            yrs = f"{back['from']}" if back["from"] == back["to"] else f"{back['from']}-{back['to']}"
            out.insert(0, f"Yeah, this week is {c['name']}'s return to {opp}, where he was head coach in {yrs}"
                          + (f" ({back['w']}-{back['l']})" if back.get("seasons") else "") + " before he left.")
        out.append(f"This week it's {bit}; coaching noise isn't something the number moves on by itself, so it "
                   f"doesn't change how I'd play that line.")
    elif school and c["current"]:
        out.append(f"{school} isn't on this week's slate, so it doesn't touch a line.")
    return factstore.answer(" ".join(out), used)


CHANGE = re.compile(r"\b(fires|fired|(?<!grounds for )(?<!be )firing|interim|parts? ways|parted ways|steps? down|resign\w*|hires?|hired|"
                    r"names? .{0,30}coach|coaching search)\b", re.I)


def _weight(s: dict) -> tuple[int, str]:
    """A story's rank: a sourced coaching-change note > a change headline > hot-seat talk > other coach chatter."""
    if "school" in s:
        return 3, s.get("date") or ""
    return (2 if CHANGE.search(s["title"]) else 1 if s["hot"] else 0), s["date"]


def _carousel_answer(q: str, notes, heads, rows, pool: set | None = None) -> str | None:
    """This week's games touched by a coaching story: the league's pool games first (that's what members are
    picking), then the strongest story (sourced change note > change headline > hot seat), newest first."""
    pool = pool or set()
    used, out = [], []
    stories: dict[str, list[dict]] = {}
    for n in notes:
        stories.setdefault(n["school"], []).append(n)
    for x in heads:
        for t in x["teams"][:1]:  # the headline's main team
            if factstore.team(t):
                stories.setdefault(t, []).append(x)
    for v in stories.values():
        v.sort(key=_weight, reverse=True)
    games = []
    for g in rows:
        hit = sorted((t for t in (g["awayTeam"], g["homeTeam"]) if stories.get(t) and _weight(stories[t][0])[0] >= 1),
                     key=lambda t: _weight(stories[t][0]), reverse=True)
        if hit:
            games.append((g, hit[0]))
    def rank(gt):
        g, t = gt
        w = _weight(stories[t][0])
        teams = {factstore.norm(g["awayTeam"]), factstore.norm(g["homeTeam"])}
        return (bool(teams & pool) and w[0] >= 2, w[0], w[1])
    games.sort(key=rank, reverse=True)
    if games:
        bits = []
        for g, t in games[:3]:
            s = stories[t][0]
            bit, mf = _line_bit(g)
            nxt = f"{_story_text(s)}; this week: {bit}"
            budget = 300 if re.search(r"hot.?seat|on the seat", q or "", re.I) else 520  # leave room for the hot-seat talk
            if bits and len(" ".join(bits)) + len(nxt) > budget:
                break
            used += mf + [s["fact"]]
            bits.append(nxt)
        out.append("Coaching stories touching this week's games: " + ". ".join(bits) + ".")
    hot = sorted((x for x in heads if x["hot"] and x["fact"] not in used),
                 key=lambda x: (bool(re.search(r"hot.?seat|job security|on the seat", x["title"], re.I)), x["date"]),
                 reverse=True)
    if re.search(r"hot.?seat|on the seat|fired|firing", q or "", re.I) or not games:
        if hot:
            out.append("Hot-seat talk: " + "; ".join(_story_text(x) for x in hot[:2]) + ".")
            used += [x["fact"] for x in hot[:2]]
    if not out:
        return None
    out.append("None of it moves a number by itself; a team playing for an interim is the one spot to watch, and "
               "that's a small-sample nudge, not a reason.")
    while len(" ".join(out)) > 700 and len(out) > 2:  # one text: the hot-seat chatter goes before a game's story
        out.pop(-2)
    return factstore.answer(" ".join(out), used)


# =============================================================================================================
# CFP lane
# =============================================================================================================

def _md(iso: str) -> str:
    d = dt.date.fromisoformat(iso[:10])
    return f"{d:%b} {d.day}"


def _wd(iso: str) -> str:
    return f"{dt.date.fromisoformat(iso[:10]):%A}"


def cfp_field() -> dict | None:
    """The field if it ended today: auto bids (power-conference race leaders, the top-ranked Group of Six team,
    Notre Dame in the top 12), at-larges by rank, straight seeding (auto bids outside the top 12 at the bottom),
    byes, first-round games, last in / first out, the next release. None without a checked ranking or rules."""
    rk = factstore.rankings("CFP")
    rules = factstore.cfp_rules()
    if not rk or not rk.get("ranks") or not rules:
        return None
    rank = {r["team"]: r["rank"] for r in rk["ranks"]}
    ordered = [r["team"] for r in sorted(rk["ranks"], key=lambda r: r["rank"])]
    asof = rk.get("date") or ""
    facts = [{"id": "cfp.release", "kind": "rank", "subject": "CFP", "value": asof,
              "text": f"CFP rankings released {_md(asof)}" if asof else "CFP rankings", "source": "ESPN CFP",
              "as_of": asof}]
    ab = rules["auto_bids"]
    champs, why, notes = {}, {}, []
    for conf in ab["power4_champions"]:
        race = factstore.conf_race(conf)
        if not race or not race["standings"]:
            continue
        lead = race["leader"]
        tie = next((t for t in race["ties"] if lead in t["teams"] and t["settled_by"] is None), None)
        if tie:
            lead = min(tie["teams"], key=lambda t: rank.get(t, 99))
            notes.append(f"{' and '.join(tie['teams'][:2])} are tied atop the {conf}, so I used the higher-ranked {lead}")
        champs[conf] = lead
        why[lead] = f"{conf} leader"
        facts += [f for f in race["facts"] if f["subject"] == lead]
    g6c = set(ab["group_of_six"]["conferences"])
    conf_of = {t: (factstore.team(t) or {}).get("conference") for t in ordered}
    g6 = next((t for t in ordered if conf_of.get(t) in g6c), None)
    auto = list(dict.fromkeys(champs.values()))
    if g6 and g6 not in auto:
        auto.append(g6)
        why[g6] = "top Group of Six team"
    nd = ab.get("notre_dame") or {}
    if nd and rank.get(nd.get("team"), 99) <= nd.get("max_rank", 12) and nd["team"] not in auto:
        auto.append(nd["team"])
        why[nd["team"]] = f"top {nd.get('max_rank', 12)}"
    n = rules["teams"]
    at_large = [t for t in ordered if t not in auto][:max(0, n - len(auto))]
    field = auto + at_large
    top = n
    seeded = sorted([t for t in field if rank.get(t, 99) <= top], key=lambda t: rank[t])
    seeded += sorted([t for t in field if rank.get(t, 99) > top], key=lambda t: rank.get(t, 99))
    seeds = list(enumerate(seeded, 1))
    sd = {t: i for i, t in seeds}
    for i, t in seeds:
        facts.append({"id": f"cfp.seed.{i}", "kind": "computed", "subject": t, "value": i,
                      "text": f"{t} is the No. {i} seed if it ended today", "source": "ESPN CFP + cfp_rules.json",
                      "as_of": asof})
    for t in field + [x for x in ordered if x not in field][:3]:
        facts += [f for f in (factstore.team(t) or {}).get("facts") or [] if f["id"].endswith(".cfp")]
    by_seed = {i: t for i, t in seeds}
    rel = [d for d in rules.get("releases") or [] if d > asof[:10]]
    nxt = rel[0] if rel else None
    src = (rules.get("sources") or [{}])[-1]
    facts.append({"id": "cfp.rules", "kind": "rule", "subject": "CFP format", "value": n,
                  "text": (f"{n}-team field: the {', '.join(ab['power4_champions'])} champions, the top Group of Six "
                           f"team and Notre Dame if top {nd.get('max_rank', 12)}; seeds {', '.join(map(str, rules['byes']))} "
                           f"get byes; first round " + ", ".join(f"{a} vs {b}" for a, b in rules["first_round"])
                           + f"; next ranking {_md(nxt) if nxt else 'none left'}; Selection Day "
                           f"{_md(rules['selection_day'])}"),
                  "source": src.get("url") or "cfp_rules.json", "as_of": rules.get("verified") or ""})
    out_list = [t for t in ordered if t not in field]
    return {"date": asof, "rank": rank, "champs": champs, "why": why, "g6": g6, "auto": auto, "at_large": at_large,
            "seeds": seeds, "seed_of": sd, "byes": [by_seed[i] for i in rules["byes"] if i in by_seed],
            "first_round": [(by_seed.get(a), by_seed.get(b)) for a, b in rules["first_round"]],
            "last_in": [t for t in reversed(at_large)][:2], "first_out": out_list[:3], "next_release": nxt,
            "selection_day": rules["selection_day"], "notes": notes, "facts": facts}


def _and_list(xs: list[str]) -> str:
    return xs[0] if len(xs) == 1 else ", ".join(xs[:-1]) + " and " + xs[-1] if xs else ""


def cfp_now_text(ctx: dict, q: str) -> str | None:
    f = cfp_field()
    if not f:
        return None
    r, sd = f["rank"], f["seed_of"]
    out = []
    asked = [t for t in factstore.teams_named(q) if t in r or t in sd][:1]
    for t in asked:
        if t in sd:
            role = ("with a first-round bye" if t in f["byes"] else
                    "hosting a first-round game" if sd[t] <= 8 else "on the road in the first round")
            out.append(f"{t} is No. {r[t]} in the CFP" + (f" and in as the No. {sd[t]} seed, {role}" if t in r else
                                                         f" and in as the No. {sd[t]} seed") + ".")
        else:
            out.append(f"{t} is No. {r[t]} in the CFP and on the outside if it ended today.")
    out.append(f"If it ended today, off the {_md(f['date'])} CFP rankings: "
               + ", ".join(t for t in f["byes"][:-1]) + f" and {f['byes'][-1]} get the byes. First round: "
               + ", ".join(f"{sd[b]} {b} at {sd[a]} {a}" for a, b in f["first_round"] if a and b) + ".")
    leaders = [t for t in f["auto"] if f["why"][t].endswith(" leader")]
    extra = []
    if f["g6"] in f["auto"]:
        extra.append(f"{f['g6']} as the top Group of Six team" + (f" (No. {r[f['g6']]})" if f["g6"] in r else ""))
    extra += [f"{t} on its top-12 spot" for t in f["auto"] if f["why"][t].startswith("top ") and t != f["g6"]]
    out.append(f"{_and_list(leaders)} are in as conference leaders" + (", plus " + " and ".join(extra) if extra else "")
               + "." + (" " + "; ".join(f["notes"]) + "." if f["notes"] else ""))
    bub = []
    if f["last_in"]:
        bub.append(f"Last in: {' and '.join(f['last_in'])}")
    if f["first_out"]:
        bumped = {t for t in f["first_out"] if r.get(t, 99) <= 12}
        bub.append("first out: " + " and ".join(f"{t} (No. {r[t]}" + (", bumped by the auto bids)" if t in bumped else ")")
                                               for t in f["first_out"][:2]))
    if bub:
        out.append("; ".join(bub) + ".")
    out.append("A power-conference title is an auto bid whatever the ranking; the bubble teams need wins and help.")
    if f["next_release"]:
        out.append(f"Next ranking {_wd(f['next_release'])}, {_md(f['next_release'])}.")
    else:
        out.append(f"Selection Day is {_wd(f['selection_day'])}, {_md(f['selection_day'])}.")
    return factstore.answer(" ".join(out), f["facts"])
