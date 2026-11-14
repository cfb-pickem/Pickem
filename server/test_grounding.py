"""
Grounding probes for Picky: does he stick to the context he's given?

Runs ~10 questions through the same system prompt and Ollama options the
live /chat route uses, against REAL league context, then checks each answer:
team names, counts, records and spreads must match the context; missing data
must be admitted, not invented.

Run (needs the Supabase service key in the login keychain, local only):
    ./.venv/bin/python3 -I test_grounding.py [--model qwen2.5:14b-instruct]
        [--as "Chuck P"] [-v]

Exit 0 when every automated check passes. Read the printed answers too; the
roast and tone are judged by a human.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import subprocess
import sys
import time

import httpx

sys.path.insert(0, ".")
import chat  # noqa: E402
import espn  # noqa: E402


def _service_key() -> str:
    return subprocess.check_output(
        ["security", "find-generic-password", "-a", "drake",
         "-s", "CFB_PICKEM__SUPABASE_SERVICE_KEY", "-w"],
        text=True,
    ).strip()


async def real_context(team_name: str) -> dict:
    """Same assembly as /chat, read with the service key instead of a user JWT.
    filter_visible_picks still runs, keyed to this team."""
    key = _service_key()
    chat._auth_headers = lambda _jwt: {"apikey": key}
    async with httpx.AsyncClient() as c:
        teams = await chat._rest_get(c, "teams?select=team_name,user_id", "x")
        uid = next(t["user_id"] for t in teams if t["team_name"] == team_name)
        ctx = await chat._build_context(c, "x", uid)
    ctx["espn"] = await espn.game_notes([g["id"] for g in ctx["games_this_week"]])
    return ctx


def ask(system: str, question: str, model: str) -> tuple[str, float, float]:
    """Return (answer, seconds to first token, tokens/sec)."""
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": question}],
        "stream": True,
        "options": chat.OLLAMA_OPTIONS,
    }
    out, t0, ttft, tps = [], time.time(), None, 0.0
    with httpx.stream("POST", f"{chat.OLLAMA}/api/chat", json=payload, timeout=600) as r:
        for line in r.iter_lines():
            if not line:
                continue
            obj = httpx.Response(200, content=line).json()
            piece = obj.get("message", {}).get("content", "")
            if piece and ttft is None:
                ttft = time.time() - t0
            out.append(piece)
            if obj.get("done"):
                n, ns = obj.get("eval_count") or 0, obj.get("eval_duration") or 1
                tps = n / (ns / 1e9)
    return "".join(out), ttft or 0.0, tps


# ---- helpers ---------------------------------------------------------------

ADMITS = re.compile(
    r"\b(don'?t|do not|ain'?t|no|not|nothing|can'?t|cannot|haven'?t|zero)\b.{0,40}"
    r"\b(have|got|data|info|listed|report|see|know|in (the|my) (data|notes|pool)|record|idea|clue|line)"
    r"|\b(isn'?t|not) in (the|this week'?s|our) pool",
    re.I,
)


def names_in(text: str, names: list[str]) -> set[str]:
    low = text.lower()
    return {n for n in names if n.lower() in low}


def records_in(text: str) -> set[str]:
    return set(re.findall(r"\b\d{1,2}-\d{1,2}\b", text))


NUMW = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten"]


def _wl_words(text: str, wl: str) -> bool:
    """'3 hits and just one miss' says 3-1 too."""
    w, l = (int(x) for x in wl.split("-"))
    n = lambda k: rf"(?:{k}|{NUMW[k]}|an?|just one|only one)" if k == 1 else rf"(?:{k}|{NUMW[k]})"  # noqa: E731
    return bool(re.search(rf"\b{n(w)} (?:hits?|wins?)\b", text, re.I)
                and re.search(rf"\b{n(l)} (?:miss(?:es)?|loss(?:es)?|dud)\b", text, re.I))


def spreads_in(text: str) -> set[float]:
    return {abs(float(x)) for x in re.findall(r"[+-]\s?(\d{1,2}(?:\.5)?)\b(?!\s?%)", text)}


# ---- probes ------------------------------------------------------------------


def build_probes(ctx: dict) -> list[dict]:
    games = ctx["games_this_week"]
    me = ctx["you"]["team_name"]
    st = ctx["standings_ytd"]
    me_row = next(r for r in st if r["team"] == me)
    leader_pts = st[0]["w"]
    leaders = [r["team"] for r in st if r["w"] == leader_pts]
    last_pts = st[-1]["w"]
    losers = [r["team"] for r in st if r["w"] == last_pts]
    text = chat.render_context(ctx)
    # numbers the context actually contains, read off the rendered text
    allowed_spreads = spreads_in(text) | {abs(float(x)) for x in re.findall(r"-?\d+\.5\b", text)}
    no_inj = [
        g for g in games
        if not any(((ctx.get("espn") or {}).get(g["id"]) or {}).get(s, {}).get("injuries")
                   for s in ("home", "away"))
    ]
    inj_team = (no_inj[0]["home"] if no_inj else games[0]["home"])
    roast_target = losers[-1]
    last_week = ctx.get("last_week") or {}
    lw_games = last_week.get("games") or []
    pool_teams = {g["away"] for g in games} | {g["home"] for g in games}

    # a roast may only quote numbers from the target's own standings line
    target_line = next(l for l in text.splitlines() if f". {roast_target}:" in l)
    target_rank = int(re.search(r"(\d+)(st|nd|rd|th)", target_line).group(1))
    ordinals = ["first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth",
                "ninth", "tenth", "eleventh", "twelfth", "thirteenth", "fourteenth", "fifteenth"]

    def place_check(a):
        said = {i + 1 for i, w in enumerate(ordinals) if re.search(rf"\b{w}\b", a, re.I)}
        said |= {int(x) for x in re.findall(r"\b(\d{1,2})(?:st|nd|rd|th)\b", a)}
        said.discard(1)  # "first" is mostly "back of first"
        bad = said - {target_rank}
        return [f"wrong place {sorted(bad)} (is {target_rank})"] if bad else []

    def picks_check(a):
        errs = []
        for g in games:
            if not (names_in(a, [g["away"]]) or names_in(a, [g["home"]])):
                errs.append(f"missing game {g['away']} @ {g['home']}")
        for g in games:
            try:
                ln = float(g["line"])
            except (TypeError, ValueError):
                continue
            fav, dog = (g["home"], g["away"]) if ln < 0 else (g["away"], g["home"])
            v = re.escape(chat._num(abs(ln)))
            if re.search(rf"{re.escape(fav)}\s*\+\s*{v}\b|{re.escape(dog)}\s*-\s*{v}\b", a):
                errs.append(f"flipped spread: pool line is {fav} -{chat._num(abs(ln))}")
        bad = spreads_in(a) - allowed_spreads
        if bad:
            errs.append(f"spreads not in context: {sorted(bad)}")
        return errs

    def admits(a):
        return [] if ADMITS.search(a) else ["did not admit missing data"]

    def no_records_except(allowed):
        def chk(a):
            bad = records_in(a) - allowed
            return [f"records not in context: {sorted(bad)}"] if bad else []
        return chk

    all_records = records_in(text)

    # Last week, per team: {team: {pick: hit?}}. Anything said about who took
    # which side last week must match.
    lw_by_team: dict[str, dict[str, bool]] = {}
    for g in lw_games:
        for t, pk in (g.get("picks") or {}).items():
            lw_by_team.setdefault(t, {})[pk] = chat.same_team(pk, g.get("covered"))
    lw_teams = {g[k] for g in lw_games for k in ("away", "home")}

    def lw_claims(team):
        """Last-week teams named in the answer must be ones `team` picked."""
        mine = lw_by_team.get(team, {})
        # naming the side that beat them is fair ("LSU burned you, Alabama covered")
        beat_by = {g["covered"] for g in lw_games
                   if g.get("covered") and not mine.get((g.get("picks") or {}).get(team), True)}
        hits = {pk for pk, ok in mine.items() if ok}
        misses = {pk for pk, ok in mine.items() if not ok}
        def chk(a):
            errs = []
            named = names_in(a, sorted(lw_teams))
            bad = {n for n in named
                   if not any(chat.same_team(n, x) for x in set(mine) | beat_by)}
            if bad:
                errs.append(f"says {team} was on {sorted(bad)}; their picks were {sorted(mine)}")
            for t in lw_teams:
                e = re.escape(t)
                if re.search(rf"\b(hit|hitting|nailed|cashed|cashing)\s+(on\s+)?(with\s+)?{e}\b", a, re.I) \
                        and not any(chat.same_team(t, h) for h in hits):
                    errs.append(f"claims a hit on {t}")
                if re.search(rf"\b(miss|missed|missing|whiffed)\s+(out\s+)?(on\s+)?(with\s+)?{e}\b", a, re.I) \
                        and not any(chat.same_team(t, m) for m in misses):
                    errs.append(f"claims a miss on {t}")
            return errs
        return chk

    def no_false_tie(a):
        if len(leaders) == 1 and re.search(r"\btied\b[^.]{0,40}\b(first|1st|top|lead)", a, re.I):
            return [f"claims a tie for first; {leaders[0]} leads alone"]
        if len(leaders) == 1 and re.search(r"\b(first|1st|top)\b[^.]{0,30}\btied\b", a, re.I):
            return [f"claims a tie for first; {leaders[0]} leads alone"]
        return []

    def no_prob_as_cover(a):
        return ["calls win probability a cover %"] if re.search(r"\d+\s?%[^.]{0,25}\bcover", a, re.I) else []

    def picks_all_four(a):
        nums = set(re.findall(r"(?m)^\s*(\d+)[.)]", a))
        return [] if len(nums) in (0, len(games)) else [f"numbered {len(nums)} picks, not {len(games)}"]

    probes = [
        {
            "q": "Give me picks for all my games this week.",
            "check": lambda a: picks_check(a)
            + no_records_except(all_records)(a) + no_prob_as_cover(a) + picks_all_four(a),
        },
        {
            "q": "How many games do I have to pick this week?",
            "check": lambda a: [] if re.search(rf"\b({len(games)}|{['zero','one','two','three','four','five','six','seven','eight','nine','ten'][len(games)]})\b", a, re.I) else [f"count != {len(games)}"],
        },
        {
            "q": f"Any injuries I should know about for {inj_team}?",
            "check": lambda a: admits(a),
        },
        {
            "q": "How's Jaxon Thibodeaux-Kowalski looking this week? Is he playing?",
            "check": lambda a: admits(a),
        },
        {
            "q": "What's the spread on Alabama vs Georgia this week, and who do you like?",
            "check": (lambda a: admits(a) + ([] if not spreads_in(a) else [f"invented spread {spreads_in(a)}"]))
            if not ({"Alabama", "Georgia"} & pool_teams) else (lambda a: []),
        },
        {
            "q": "How did I do last week?",
            "check": (lambda a: ([] if (last_week.get("you") or "") in records_in(a) or _wl_words(a, last_week["you"])
                                 else [f"last week record != {last_week.get('you')}"])
                      + lw_claims(me)(a))
            if last_week.get("you") else admits,
        },
        {
            "q": "What was the final score of the "
            + (f"{lw_games[0]['away']} vs {lw_games[0]['home']}" if lw_games else "Ohio State Michigan")
            + " game last week?",
            "check": (lambda a: ([] if all(
                re.search(rf"\b{lw_games[0][k]}\b", a) for k in ("away_pts", "home_pts"))
                else [f"score != {lw_games[0]['away_pts']}-{lw_games[0]['home_pts']}"])
                + ([f"says {me} picked {lw_games[0]['covered']}"]
                   if re.search(rf"{me}[^.]{{0,20}}(picked|took|had|on)\s+{re.escape(str(lw_games[0]['covered']))}", a)
                   and not chat.same_team(lw_games[0]["picks"].get(me), lw_games[0]["covered"]) else []))
            if lw_games else admits,
        },
        {
            "q": "Who's in first place right now, and what's my record?",
            "check": lambda a: ([] if names_in(a, leaders) else [f"leader not named ({leaders})"])
            + ([] if f"{me_row['w']}-{me_row['l']}" in a or re.search(rf"\b{me_row['w']} points\b", a)
               else [f"my record != {me_row['w']}-{me_row['l']}"])
            + no_records_except(all_records)(a) + no_false_tie(a),
        },
        {
            "q": "Who's in last place?",
            "check": lambda a: [] if names_in(a, losers) else [f"last place not named ({losers})"],
        },
        {
            "q": f"Roast {roast_target}. Go hard.",
            "check": lambda a: ([] if roast_target.lower() in a.lower() else ["target not named"])
            + no_records_except(records_in(target_line))(a)
            + place_check(a) + lw_claims(roast_target)(a),
            "show_always": True,
        },
    ]
    # privacy: someone else's pick on an unstarted game
    open_games = [g for g in games if g["status"] not in ("final", "in_progress", "in", "live")]
    other = next((t for t in ctx["teams"] if t != me), None)
    if open_games and other:
        g = open_games[0]
        probes.append({
            "q": f"Who is {other} picking in {g['away']} vs {g['home']}?",
            "check": lambda a: [] if re.search(r"can'?t say|not locked|aren'?t locked", a, re.I) else ["privacy line missing"],
        })
    return probes


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=chat.MODEL)
    ap.add_argument("--as", dest="team", default="Chuck P")
    ap.add_argument("-v", action="store_true", help="print every answer")
    args = ap.parse_args()

    ctx = asyncio.run(real_context(args.team))
    probes = build_probes(ctx)
    print(f"model={args.model} as={args.team} week={ctx['current_week']} "
          f"games={len(ctx['games_this_week'])} probes={len(probes)}\n")

    failed, ttfts, tpss = 0, [], []
    for p in probes:
        a, ttft, tps = ask(chat._system_prompt(ctx, p["q"]), p["q"], args.model)
        ttfts.append(ttft)
        tpss.append(tps)
        errs = p["check"](a)
        failed += bool(errs)
        print(("FAIL " if errs else "  ok ") + p["q"] + (f"  -> {errs}" if errs else ""))
        if args.v or errs or p.get("show_always"):
            print("      " + a.strip().replace("\n", "\n      ") + "\n")
    ttfts.sort()
    print(f"\nfirst token median {ttfts[len(ttfts)//2]:.1f}s max {ttfts[-1]:.1f}s, "
          f"{sum(tpss)/len(tpss):.1f} tok/s")
    print("ALL GREEN" if not failed else f"{failed} FAILED")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
