"""
Deterministic checks for the genius exam (no model): numbers Picky quotes
must match the market/fact pack, ATS records must be real, pick answers must
be framed against the spread, and replies must stay text-length.

Each check returns (name, ok, detail).
"""

from __future__ import annotations

import re

KEY_NUMBERS = {3, 7, 10, 14, 17, 21}
LINE_TOL = 1.5  # the market moves between writing the key and running the exam
MAX_CHARS = 700  # a text, not an essay

# "-7.5", "+3", "7.5" (half points are almost always lines/totals in these answers)
_LINE_TOKEN = re.compile(r"(?<![\w.])([+-]\s?\d{1,2}(?:\.5)?|\d{1,2}\.5)(?!\.?\d)"
                         r"(?!\s*(?:%|percent|ppa|yards?|yds|°|degrees|mph|points? per|per (?:scoring )?(?:opp|drive|play|game)))", re.I)
_ATS_REC = re.compile(r"\b(\d{1,2})\s*-\s*(\d{1,2})(?:\s*-\s*(\d{1,2}))?\s+(?:ATS|against the (?:spread|number)|vs\.? the spread)", re.I)
_ATS_WORDS = re.compile(r"cover|spread|\bATS\b|points?\b|\blay(?:ing)?\b|getting|\bline\b|[+-]\s?\d|\bhook\b|\bdog\b|\bfav", re.I)
_STRAIGHT_UP = re.compile(r"\b(\w[\w&.' ]{1,25}) (?:wins?|will win|to win|gets the W)\b", re.I)


def allowed_lines(item: dict, facts: dict, market: dict) -> set[float]:
    """Every spread/total/implied total Picky could legitimately quote for the
    item's games: the key's fact pack (open, current, every book) plus the
    market re-pulled at run time, as absolute values."""
    vals: set[float] = set(KEY_NUMBERS) | {float(x) for x in (item.get("det") or {}).get("numbers") or []}
    for gid in item.get("games") or []:
        for src in ((facts.get("games") or {}).get(str(gid)), (market or {}).get(str(gid))):
            if not src:
                continue
            e = src.get("espn") or {}
            for k in ("home_spread_now", "home_spread_open", "total_now", "total_open"):
                v = _num(e.get(k))
                if v is not None:
                    vals.add(abs(v))
            for ln in src.get("cfbd_lines") or []:
                for k in ("spread", "spreadOpen", "overUnder", "overUnderOpen"):
                    v = _num(ln.get(k))
                    if v is not None:
                        vals.add(abs(v))
            if src.get("league_line") is not None:
                vals.add(abs(float(src["league_line"])))
            sp, tot = _num(e.get("home_spread_now")), _num(e.get("total_now"))
            if sp is not None and tot is not None:  # implied team totals
                vals |= {round((tot - sp) / 2 * 2) / 2, round((tot + sp) / 2 * 2) / 2,
                         (tot - sp) / 2, (tot + sp) / 2}
            for t in (src.get("teams") or {}).values():  # past spreads Picky may cite from results
                for g in t.get("games") or []:
                    if g.get("spread") is not None:
                        vals.add(abs(g["spread"]))
            for h in src.get("h2h_2025") or []:
                if h.get("home_spread") is not None:
                    vals.add(abs(h["home_spread"]))
    return vals


def allowed_ats(item: dict, facts: dict) -> set[str]:
    out: set[str] = set()
    for gid in item.get("games") or []:
        g = (facts.get("games") or {}).get(str(gid)) or {}
        for t in (g.get("teams") or {}).values():
            if t.get("cfbd_ats"):
                w, l, p = t["cfbd_ats"].split("-")
                out |= {f"{w}-{l}", f"{w}-{l}-{p}"}
            for v in (t.get("ats_splits_computed") or {}).values():
                if v and "-" in v:
                    out.add(v)
                    parts = v.split("-")
                    out.add(f"{parts[0]}-{parts[1]}")
    return out


def _num(v) -> float | None:
    if v is None:
        return None
    try:
        return float(str(v).replace("o", "").replace("u", "").replace("+", "").strip())
    except ValueError:
        return None


def check_lines(ans: str, allowed: set[float]) -> tuple:
    bad = []
    for m in _LINE_TOKEN.finditer(ans or ""):
        tok = m.group(1)
        v = abs(float(tok.replace(" ", "")))
        if v == 0:
            continue
        if not any(abs(v - a) <= LINE_TOL for a in allowed):
            bad.append(tok)
    return "lines_match_market", not bad, ("unverified: " + ", ".join(bad)) if bad else ""


def check_ats_records(ans: str, allowed: set[str]) -> tuple:
    bad = []
    for m in _ATS_REC.finditer(ans or ""):
        rec = f"{m.group(1)}-{m.group(2)}" + (f"-{m.group(3)}" if m.group(3) else "")
        if rec not in allowed and f"{m.group(1)}-{m.group(2)}" not in allowed:
            bad.append(rec)
    return "ats_records_real", not bad, ("not in data: " + ", ".join(bad)) if bad else ""


def check_records(ans: str, wanted: list[str]) -> tuple:
    """Game-theory items: the persona's real standings numbers must appear when
    the answer quotes any W-L at all (a wrong W-L is a fail)."""
    got = re.findall(r"\b(\d{1,2})\s*-\s*(\d{1,2})\b", ans or "")
    recs = {f"{a}-{b}" for a, b in got}
    bad = [r for r in recs if r not in set(wanted) and not _ATS_REC.search(ans)]
    return "records_true", not bad, ("wrong W-L: " + ", ".join(sorted(bad))) if bad else ""


def check_ats_frame(answers: list[str]) -> tuple:
    """A pick answer must be against the spread: a number, 'cover', 'points'...
    Straight-up 'X wins' with no spread language fails (the league is ATS)."""
    joined = " ".join(answers)
    ok = bool(_ATS_WORDS.search(joined))
    return "ats_framing", ok, "" if ok else "pick with no spread/cover language"


def check_length(ans: str) -> tuple:
    n = len(ans or "")
    return "text_length", n <= MAX_CHARS, f"{n} chars" if n > MAX_CHARS else ""


def check_replied(ans: str) -> tuple:
    a = (ans or "").strip()
    ok = bool(a) and not a.startswith("[ERROR") and not a.startswith("[HTTP")
    return "replied", ok, "" if ok else a[:80]


def run_checks(item: dict, answers: list[str], facts: dict, market: dict) -> list[tuple]:
    det = item.get("det") or {}
    lines = allowed_lines(item, facts, market)
    ats = allowed_ats(item, facts)
    out = []
    for i, a in enumerate(answers):
        sfx = f"@t{i + 1}" if len(answers) > 1 else ""
        for name, ok, d in (check_replied(a), check_length(a), check_lines(a, lines), check_ats_records(a, ats)):
            out.append((name + sfx, ok, d))
        if det.get("records"):
            name, ok, d = check_records(a, det["records"])
            out.append((name + sfx, ok, d))
    if det.get("ats_frame"):
        out.append(check_ats_frame(answers))
    return out
