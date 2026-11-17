"""
Opus judge for genius-exam runs: one offline `claude -p` per batch (Opus 5.5
via picky_claude.MODEL, no tools, strict empty MCP, no API key; see
dossier.run_claude). Picky never calls Anthropic; this is the grader.

    cd server && ./.venv/bin/python3 -I eval/genius/judge.py eval/genius/results/<tag>.json [more.json]

Scores each item 1-5 on correctness, depth, reasoning, honesty, voice, lists
invented claims and missed must-includes. An item earns its genius point when
the deterministic checks pass and correctness>=4, honesty>=4, reasoning>=3,
depth>=3, voice>=3. Writes results/<tag>.judge.json and reports/<tag>.md.
"""

from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1]))

import run as G  # noqa: E402

BATCH = 9
DIMS = ("correctness", "depth", "reasoning", "honesty", "voice")

RUBRIC = """You grade "Picky", a trash-talking bot that texts with friends in a college-football pick'em league. The league is graded ONLY against the spread (ATS) with a line locked Tuesday morning; who wins straight up doesn't matter. Members ask Picky the questions a sharp bettor asks before making a pick. Picky is supposed to be a true college-football genius: right on the facts, uses the factors that matter, ties them to an ATS lean, admits what he doesn't know, and still sounds like a funny friend in a group text (short, not an essay).

For each item you get the member's question(s), Picky's reply (or replies, for multi-turn pushback), an ANSWER KEY written from verified data (CFBD stats, ESPN odds, cited news) with: must_include (what a genius answer contains), must_not_claim (known errors), the expected lean (or "none"/"any"/"conditional"), extra verified facts, and the CURRENT MARKET pulled when the exam ran (lines move during the week: a number that matches the current market or the key's open/current within ~1 point is correct).

Score each 1-5:
- correctness: are the facts and numbers right vs the key/market/ALSO VERIFIED data? 5 = every checkable claim right; 3 = mostly right with a small slip; 1 = a key fact wrong (wrong QB, wrong favorite, wrong record).
- depth: does it use the factors that matter for THIS question (the must_include items, the relevant stats/ATS/injury/situational facts)? 5 = hits the key factors specifically; 3 = one relevant factor or generic; 1 = nothing relevant, or a dodge.
- reasoning: does it connect the facts to an ATS conclusion correctly (a side vs the number, or an honest 'no strong lean' / conditional when the key says so)? Straight-up 'X wins' framing for a pick question caps this at 2. If the question isn't a pick question, grade whether the logic is sound. 5 = sharp and correct; 1 = wrong logic or wrong conclusion (e.g. calls Clemson the value side when the key says Duke).
- honesty: 5 = no invented facts, and says 'don't know / not confirmed' where the key says it's unknown; 1 = invents injuries, stats, records, rankings or news. Claims not in the key are invented ONLY if they are specific football claims (numbers, injuries, results, names) that you can't verify from the key or well-established CFB knowledge; opinions and jokes are fine. Saying "I don't know / not in my data" is NEVER dishonest (if the key has the answer, that costs correctness and depth, not honesty). Jabs about the member's own league picks or record are checked by other tests: ignore them here unless they contradict the key.
- voice: 5 = sounds like a sharp, funny friend texting: short, specific, confident; 3 = fine but bland or a bit long; 1 = essay, report/bullet format, robotic, or no answer.

Also list: invented (specific false or unverifiable factual claims, quoted briefly), missed (must_include points it skipped, short), and a one-line note.

Return JSON only: {"items": [{"tag": "Q01", "correctness": 4, "depth": 3, "reasoning": 4, "honesty": 5, "voice": 4, "invented": [], "missed": ["..."], "note": "under 20 words"}, ...]} covering every item."""

SCHEMA = {"type": "object", "properties": {"items": {"type": "array", "items": {"type": "object", "properties": {
    "tag": {"type": "string"}, **{d: {"type": "integer"} for d in DIMS},
    "invented": {"type": "array", "items": {"type": "string"}}, "missed": {"type": "array", "items": {"type": "string"}},
    "note": {"type": "string"}}, "required": ["tag", *DIMS, "invented", "missed", "note"]}}}, "required": ["items"]}


def passes(det: list, j: dict | None) -> bool:
    if not j or not all(c[1] for c in det):
        return False
    return (j["correctness"] >= 4 and j["honesty"] >= 4 and j["reasoning"] >= 3
            and j["depth"] >= 3 and j["voice"] >= 3)


def _market_line(market: dict, gid) -> str:
    e = (market.get(str(gid)) or {}).get("espn") or {}
    m = market.get(str(gid)) or {}
    s = f"{e.get('details')} (home spread {e.get('home_spread_now')}, opened {e.get('home_spread_open')}), total {e.get('total_now')} (opened {e.get('total_open')})"
    if m.get("league_line") is not None:
        s += f"; league line {m['league_line']} (home-team adjustment){' POOL GAME' if m.get('pool_game') else ''}"
    return s


_CACHE: dict = {}


def verified(games: list, facts: dict) -> str:
    """Per-game verified data (the fact pack + sourced availability notes): true claims Picky makes from his
    data sources that the key doesn't list must not count as invented."""
    out = []
    try:
        intel = json.loads((HERE.parents[1] / "cache" / "slate_intel.json").read_text())
    except Exception:  # noqa: BLE001
        intel = {}
    for gid in games or []:
        g = (facts.get("games") or {}).get(str(gid))
        if not g:
            continue
        bits = [f"{g['away']} at {g['home']}, {g['kickoff_ct']}, {g['venue'].get('name')}"]
        for side in ("away", "home"):
            t = g["teams"][side]
            last = "; ".join(f"{x['site']} {x['opp']} {x['score'][0]}-{x['score'][1]} as {x['spread']:+g} {x['ats']}"
                             for x in t["games"][-4:] if x.get("spread") is not None)
            bits.append(f"{t['team']} {t['record']} (conf {t['conf_record']}), SP+ #{t['sp_rank']} (off #{t['sp_off_rank']}, "
                        f"def #{t['sp_def_rank']}), ATS {t['cfbd_ats']} splits {t['ats_splits_computed']}, rest {t['days_rest']}d, "
                        f"next {t['next_week']}; last 4: {last}")
        try:  # the advanced ranks Picky's cards quote (slate_cards/cfbd), so 'rushing EPA #21' is checkable
            import cfbd
            import slate_cards
            idx, adv = cfbd.load_index(), slate_cards._advanced()
            for side in ("away", "home"):
                nm = g["teams"][side]["team"]
                t, a = cfbd.resolve_team(nm, idx) or {}, adv.get(cfbd.norm(nm)) or {}
                o, d = (t.get("adv") or {}).get("offense") or {}, (t.get("adv") or {}).get("defense") or {}
                bits.append(f"{nm} ranks: off EPA #{o.get('ppa_rank')}, def EPA #{d.get('ppa_rank')}, off explosiveness "
                            f"#{o.get('explosive_rank')}, def explosiveness allowed #{d.get('explosive_rank')}, def havoc "
                            f"#{d.get('havoc_rank')}, rushing EPA #{(a.get('_rank') or {}).get('offense_rush')}, rushing EPA "
                            f"allowed #{(a.get('_rank') or {}).get('defense_rush')}, run rate "
                            f"{((a.get('offense') or {}).get('rushingPlays') or {}).get('rate', 0):.0%}, pts per scoring chance "
                            f"{(a.get('offense') or {}).get('pointsPerOpportunity', 0):.1f} (allows "
                            f"{(a.get('defense') or {}).get('pointsPerOpportunity', 0):.1f})")
        except Exception:  # noqa: BLE001
            pass
        try:  # unit edges + travel/altitude/weather the cards now carry (units/travel/weather, code over the cache):
            import slate_cards  # true card facts the key doesn't list ('UCLA red-zone defense #7') aren't invented
            import travel
            import units
            import weather
            tab = units.table()
            for u in ("rush", "pass", "protect", "redzone", "explosive", "eff", "turnovers", "pace"):
                bits += ["unit: " + e["text"] for e in units.edges(u, g["away"], g["home"], tab)]
            if "_gds" not in _CACHE:
                _CACHE["_gds"] = slate_cards._games_dataset()
            cg = _CACHE["_gds"].get(int(gid))
            if cg:
                bits += [x.strip() for x in (travel.travel_line(cg), travel.altitude_line(cg), weather.line(gid)) if x]
        except Exception:  # noqa: BLE001
            pass
        if g.get("h2h_2025"):
            bits.append("2025 meeting: " + "; ".join(f"{h['score']} (home line {h['home_spread']})" for h in g["h2h_2025"]))
        if g.get("weather"):
            bits.append(f"forecast at kickoff: {g['weather']}")
        ig = (intel.get("games") or {}).get(str(gid)) or {}
        for c in (ig.get("facts") or [])[:5]:
            bits.append(f"sourced ({c.get('source_name')}, {c.get('date')}): {c.get('text')}")
        out.append("  " + " | ".join(bits))
    for c in (intel.get("coaching") or [])[:8]:
        out.append(f"  coaching change (sourced {c.get('date')}): {c.get('text')}")
    return "\n".join(out)


def block(tag: str, r: dict, key: dict, market: dict, facts: dict | None = None) -> str:
    convo = "\n".join(f"  Member: {t['q']}\n  Picky: {t['a']}" for t in r["turns"])
    mk = "\n".join(f"  game {g}: {_market_line(market, g)}" for g in r["games"]) or "  (no specific game)"
    return (f"### {tag} [{r['cat']}] asked by league member {r['persona']}\n{convo}\n"
            f"ANSWER KEY: {json.dumps(key, ensure_ascii=False)}\nCURRENT MARKET:\n{mk}"
            + (f"\nALSO VERIFIED (Picky's data sources; a claim matching these is true, not invented):\n{v}"
               if facts is not None and (v := verified(r["games"], facts)) else ""))


def judge_run(path: Path) -> dict:
    import dossier  # run_claude: Opus, no tools, strict empty MCP
    run = json.loads(path.read_text())
    _q, items = G.load_items()
    keys = {i["id"]: i["key"] for i in items}
    res = [r for r in run["results"] if r["id"] in keys]
    facts = G.load_facts(run["week"])
    verdicts = {}
    for b in range(0, len(res), BATCH):
        chunk = res[b:b + BATCH]
        tags = {f"Q{n + 1:02d}": r for n, r in enumerate(chunk)}
        prompt = RUBRIC + "\n\n## ITEMS\n\n" + "\n\n".join(block(t, r, keys[r["id"]], run["market"], facts)
                                                            for t, r in tags.items())
        for attempt in range(2):
            try:
                out = dossier.run_claude(prompt, 900, SCHEMA, "")
                break
            except Exception as e:  # noqa: BLE001
                print(f"  judge batch {b // BATCH + 1} failed ({e!r:.120}); {'retrying' if not attempt else 'giving up'}")
                out = {"items": []}
        for it in out.get("items") or []:
            r = tags.get(it.get("tag"))
            if r:
                verdicts[r["id"]] = {k: it.get(k) for k in (*DIMS, "invented", "missed", "note")}
        print(f"  judged {min(b + BATCH, len(res))}/{len(res)}", flush=True)
    jp = path.with_suffix(".judge.json")
    jp.write_text(json.dumps(verdicts, indent=1))
    return verdicts


def summary(run: dict, verdicts: dict) -> dict:
    res = run["results"]
    pts = [r["id"] for r in res if passes(r["det"], verdicts.get(r["id"]))]
    by_cat = {}
    for r in res:
        c = by_cat.setdefault(r["cat"], [0, 0])
        c[1] += 1
        c[0] += r["id"] in pts
    means = {d: round(statistics.mean(v[d] for v in verdicts.values() if isinstance(v.get(d), int)), 2)
             for d in DIMS if verdicts}
    lat = sorted(t["secs"] for r in res for t in r["turns"])
    return {"score": f"{len(pts)}/{len(res)}", "points": len(pts), "n": len(res), "by_cat": by_cat, "means": means,
            "det_fail": sum(not all(c[1] for c in r["det"]) for r in res),
            "invented_items": sum(bool(v.get("invented")) for v in verdicts.values()),
            "latency_median": lat[len(lat) // 2] if lat else None, "latency_max": lat[-1] if lat else None}


def render(run: dict, verdicts: dict) -> str:
    s = summary(run, verdicts)
    L = [f"# Genius exam {run['tag']} ({run['started']})", "",
         f"Score **{s['score']}**; judge means {s['means']}; det fails {s['det_fail']}; items with invented claims "
         f"{s['invented_items']}; latency median {s['latency_median']}s max {s['latency_max']}s", "",
         "By category: " + ", ".join(f"{c} {p}/{n}" for c, (p, n) in s["by_cat"].items()), "", "## Items", ""]
    for r in run["results"]:
        v = verdicts.get(r["id"]) or {}
        mark = "PASS" if passes(r["det"], v) else "FAIL"
        L.append(f"### {r['id']} [{r['cat']}] {mark}  " + " ".join(f"{d[:4]}={v.get(d)}" for d in DIMS))
        for t in r["turns"]:
            L += [f"- Q: {t['q']}", f"  A ({t['secs']}s): {t['a']}"]
        bad = [f"{c[0]} ({c[2]})" for c in r["det"] if not c[1]]
        if bad:
            L.append(f"- det: {', '.join(bad)}")
        if v:
            L.append(f"- judge: {v.get('note')} | invented: {v.get('invented')} | missed: {v.get('missed')}")
        L.append("")
    return "\n".join(L)


def main(paths: list[str]) -> int:
    G.REPORTS.mkdir(exist_ok=True)
    rows = []
    for p in map(Path, paths):
        run = json.loads(p.read_text())
        jp = p.with_suffix(".judge.json")
        verdicts = json.loads(jp.read_text()) if jp.exists() and "--rejudge" not in sys.argv else judge_run(p)
        (G.REPORTS / f"{run['tag']}.md").write_text(render(run, verdicts))
        rows.append((run["tag"], summary(run, verdicts)))
        if "--history" in sys.argv:  # nightly: one line per run, private (no answers, just scores)
            G.PRIVATE.mkdir(parents=True, exist_ok=True)
            with (G.PRIVATE / "history.jsonl").open("a") as f:
                f.write(json.dumps({"tag": run["tag"], "started": run["started"], "week": run["week"],
                                    "model": (run.get("health") or {}).get("model"), **rows[-1][1]}) + "\n")
    for tag, s in rows:
        print(f"{tag}: {s['score']}  means {s['means']}  det_fail {s['det_fail']}  invented {s['invented_items']}  "
              f"lat med {s['latency_median']}s  by_cat {s['by_cat']}")
    return 0


if __name__ == "__main__":
    sys.exit(main([a for a in sys.argv[1:] if not a.startswith("--")]))
