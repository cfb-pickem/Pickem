"""
Once-a-day OFFLINE quality check of news.py's plain-code tagging (Charlie,
2026-11-16: Opus only for occasional QA, never in the refresh loop).

LaunchAgent com.pickem.news-qa runs `news_qa.py run` daily at 06:40 CT. It
reads server/cache/news.json (no fetch), shows Opus (picky_claude.MODEL, no
tools, zero MCP, via dossier.run_claude) the newest stories with what the code
derived (teams, players, topics, injury status, reported/confirmed, which
headlines were merged into one story, what superseded what), and asks which
derived fields are wrong given the headline + blurb. Report: one JSON line per
run in cfb-pickem-private/news_qa.log.

If at least ERROR_RATE of the sampled stories have a wrong field, it hands the
examples to feed_monitor.repair("news", why=...): a throwaway worktree on
branch picky-repair/news-<stamp> where Claude may edit only news.py / its
plist, merged only if test_news.py passes (otherwise the branch stays for a
look). Zero maintenance (phase 8): this QA only feeds code repair, which isn't one
of the capped Claude boosters, so the daily run logs "skipped" (news.py's own
refresh logs feed-health lines in code); it runs by hand (PICKY_OPUS_MANUAL=1
news_qa.py run).

  news_qa.py run [--no-repair]     news_qa.py sample   (print what Opus would see)
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))  # run with -I

import news  # noqa: E402

PRIVATE = Path(os.environ.get("PICKY_PRIVATE_DIR", "/Users/Agent/cfb-pickem-private"))
LOG = PRIVATE / "news_qa.log"
SAMPLE = 20
ERROR_RATE = 0.2
TIMEOUT_S = 600
FIELDS = ("teams", "players", "topics", "status", "label", "merged", "superseded")
SCHEMA = {"type": "object", "required": ["errors"], "properties": {"errors": {"type": "array", "items": {
    "type": "object", "required": ["id", "field", "why"],
    "properties": {"id": {"type": "integer"}, "field": {"type": "string", "enum": list(FIELDS)},
                   "should_be": {"type": "string"}, "why": {"type": "string"}}}}}}


def sample(cache: dict, n: int = SAMPLE) -> list[dict]:
    """Newest stories (multi-outlet and superseded ones first) as plain rows."""
    sts = news.stories(cache, include_superseded=True)
    sts.sort(key=lambda s: (s["n_outlets"] > 1 or bool(s["superseded_by"]), s["time"]), reverse=True)
    rows = []
    for i, s in enumerate(sts[:n]):
        lead = s["lead"]
        rows.append({"id": i, "headline": lead["title"], "blurb": lead.get("summary", ""),
                     "teams": lead.get("teams", []), "players": lead.get("players", []),
                     "topics": lead.get("topics", []), "status": s["status"], "label": s["label"],
                     "merged": [x["title"] for x in s["items"][1:]],
                     "superseded": (cache["items"].get(s["superseded_by"]) or {}).get("title")})
    return rows


def prompt_for(rows: list[dict]) -> str:
    return ("You are checking an automatic college football news tagger. For each story below, the code "
            "derived: teams/players named (FBS only), topics (injury, suspension, coaching, portal, recruiting, "
            "rankings, betting, realignment, money), status (an injury story's player status: out, doubtful, "
            "questionable, available), label ('reported' = sourced/expected/not yet official, 'confirmed' = "
            "announced/done, null = neither), merged (other outlets' headlines it treated as the SAME story) and "
            "superseded (a newer story it says replaces this one). Judge ONLY from the headline and blurb shown; "
            "no outside knowledge. List every derived field that is clearly wrong (a missing team that the "
            "headline names counts; extra teams named only in passing don't). Return {\"errors\": []} if all "
            "fine.\n\n" + json.dumps(rows, indent=1))


def run(repair: bool = True, opus=None, fixer=None, cache: dict | None = None) -> dict:
    cache = cache if cache is not None else news.load_cache()
    rows = sample(cache)
    rec = {"ts": dt.datetime.now(news.CT).isoformat(timespec="seconds"), "sampled": len(rows)}
    if not rows:
        rec["result"] = "empty cache"
        return _log(rec)
    if opus is None:
        import dossier
        import picky_claude
        if not (picky_claude.manual() and picky_claude.available()):
            rec.update(result="skipped", reason="hand-only (PICKY_OPUS_MANUAL=1): it feeds code repair, not one of "
                                                "the capped boosters; news.py logs feed health")
            return _log(rec)
        opus = lambda p: dossier.run_claude(p, TIMEOUT_S, SCHEMA, "", caller="news_qa")  # noqa: E731
    try:
        errs = [e for e in (opus(prompt_for(rows)).get("errors") or []) if 0 <= e.get("id", -1) < len(rows)]
    except Exception as e:  # noqa: BLE001 - Anthropic down / kill switch: skip, the pipeline doesn't care
        rec.update(result="skipped", reason=f"{type(e).__name__}: {str(e)[:200]}")
        return _log(rec)
    bad = sorted({e["id"] for e in errs})
    rec.update(errors=len(errs), bad_stories=len(bad), rate=round(len(bad) / len(rows), 2),
               examples=[{**e, "headline": rows[e["id"]]["headline"]} for e in errs[:8]])
    rec["result"] = "ok" if len(bad) / len(rows) < ERROR_RATE else "needs fix"
    if rec["result"] == "needs fix" and repair:
        why = (f"daily QA: {len(bad)}/{len(rows)} sampled stories have wrongly derived fields (tagging regexes / "
               "clustering / superseding in news.py, not the feeds). Examples (field: why, should be):\n"
               + "\n".join(f"- {x['headline']!r} -> {x['field']}: {x['why']} (should be {x.get('should_be', '?')})"
                           for x in rec["examples"])
               + "\nFix the rules in news.py and add each example as a case in test_news.py.")
        if fixer is None:
            import feed_monitor
            fixer = lambda w: feed_monitor.repair("news", why=w)  # noqa: E731
        r = fixer(why)
        rec["repair"] = {k: r.get(k) for k in ("result", "branch", "reason", "sha")}
    return _log(rec)


def _log(rec: dict) -> dict:
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a") as f:
            f.write(json.dumps(rec) + "\n")
    except OSError:
        pass
    print(json.dumps(rec, indent=1))
    return rec


def main(argv: list[str]) -> int:
    if argv[:1] == ["run"]:
        run(repair="--no-repair" not in argv)
        return 0
    if argv[:1] == ["sample"]:
        print(prompt_for(sample(news.load_cache())))
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
