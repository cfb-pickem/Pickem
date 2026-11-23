"""
League rules: the single source of truth for Picky.

Every rule Picky states comes from here, and as much of it as possible is
READ OUT OF THE SITE'S OWN CODE rather than written by hand:

  site page      cfb-genius.html "League Rules" block: tiebreaker order, payout
                 structure, the Pit's table rules, games a week
  SQL (repo)     the latest definition of each rules function across
                 supabase/migrations: set_winner_on_final (cover rule, push to
                 Away), half_point_line, line_lock_at (Tuesday 11:00 CT),
                 picks_open_for_game (week lock / playoff per-game lock),
                 slots_jackpot_odds(_for), bj_* (chips, bets, 3:2, dealer 17),
                 and the RLS policies (commissioner powers, hidden picks)
  JS             index.html scoring + cut line, js/seasonHistory.js isWin,
                 js/kickoff.js, js/coverOdds.js, stats.html race constants,
                 js/seasonRace.js, commissioner.html playoff seeds
  hand facts     the few things no code says (the $75 buy-in, confirmed by the
                 commissioner 2026-11-14; which week is conference title week)

`python3 league_rules.py --write` regenerates server/league_rules.md (the
digest chat.py's rules rider injects) from those sources, with a facts-json
header the Python consumers (chat.py, playoff_race.py) read their constants
from, and a fingerprint per rules-relevant source.

It ADAPTS: `python3 league_rules.py --check` (LaunchAgent
com.pickem.rules-watch) fingerprints the sources on the deployed branch
(origin/main), the live Supabase function/policy definitions (read-only) and
the deployed rules page. Anything that moved since the last verified state
gets its sections regenerated and re-verified: deterministic checks first
(extraction, node conformance against the site's JS, live SQL), then an
offline Opus review via picky_claude. If Anthropic is down or the review
fails, those sections are replaced with a "RULES CHANGED, VERIFY" notice in
server/cache/league_rules.live.md, so Picky tells people to check the site
instead of reciting a stale rule. chat.py reads that live copy when present.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import html as _html
import json
import math
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

SERVER = Path(__file__).resolve().parent
sys.path.insert(0, str(SERVER))  # launchd runs `python3 -I` (no script dir on the path): the Opus review imports picky_claude
ROOT = SERVER.parent
MD = SERVER / "league_rules.md"
CACHE = SERVER / "cache"


def live_md_path() -> Path:
    return Path(os.environ.get("PICKY_RULES_LIVE", str(CACHE / "league_rules.live.md")))


def state_path() -> Path:
    return Path(os.environ.get("PICKY_RULES_STATE", str(CACHE / "league_rules_state.json")))


SITE_PAGE_URL = "https://cfb-pickem.github.io/Pickem/cfb-genius.html"
SUPABASE_REF = "vopdioszofwdkwnujtiq"

# Section order = digest order. chat.rules_for() matches a question to
# sections with these patterns (each section ~100 tokens; no match = whole).
SECTION_ASK = {
    "FORMAT": r"\bscor|\bpoints?\b|\bline|\bspread|\bpush|half.?point|\bformat\b|how many (games|picks)|\bweeks?\b|bonus|\bcover",
    "DEADLINE AND PRIVACY": r"\block|deadline|\blate\b|\bhidden\b|\bhide\b|\bsee (other|their)|change|forg[eo]t|\bsave|\bdelay|reveal",
    "LET THE SLOTS DECIDE": r"\bslots?\b|jackpot|auto ?win|\bpop|\bcoin\b|football meter",
    "STANDINGS AND PLAYOFFS": r"\bstandings\b|\bplayoffs?\b|\btop (4|four)\b|\bweekly prize|\bseason\b|cut ?line|\bseeds?\b",
    "PAYOUTS": r"\bpay|buy.?in|\bpot\b|\bprize|\bmoney|\bwin (any|anything|money)|place (win|get)|\b(first|second|third|1st|2nd|3rd)\b.*\b(win|get)|winner get|cash|\$",
    "TIEBREAKERS": r"\btie|tiebreak|army|navy|celebration bowl|coin flip|last 8",
    "THE PIT": r"\bpit\b|blackjack|\bchips?\b|\bdealer\b|\bdouble|\bbust",
    "COMMISSIONER": r"commish|commissioner|who (decides|runs|sets|picks the games)|override|dispute",
    "EDGE CASES": r"what if|what happens|\bmiss(ed)?\b|no pick|forg[eo]t|postpone|cancel|pick.?em|\bexhibition|\bpicky\b.*\b(pay|pot|rank)",
}
SECTIONS = list(SECTION_ASK)
FLAG = "RULES CHANGED, VERIFY"

# Things no code says. Each is labelled with who said it.
HAND = {
    "buy_in": 75,  # commissioner (Charlie), 2026-11-14; the site never prints it
    "buy_in_source": "confirmed by the commissioner, 2026-11-14; the site itself doesn't print it",
    "championship_week_note": (
        "When: conference championship week is the last week of the regular season (2026: week 14, Dec 5; "
        "week 15, Army-Navy week, has no pool games; 2025 it was week 15). The league playoffs start after "
        "conference championship week, and the playoff games are the College Football Playoff games, week 16 "
        "on. (2025: the first playoff games were Dec 19-20.)"),
}


class ExtractError(Exception):
    pass


# =============================================================================
# reading the site (working tree or a git ref, e.g. origin/main = deployed)
# =============================================================================

class Repo:
    def __init__(self, root: Path | str = ROOT, ref: str | None = None):
        self.root, self.ref = Path(root), ref
        self._c: dict[str, str | None] = {}

    def read(self, rel: str) -> str | None:
        if rel not in self._c:
            if self.ref:
                p = subprocess.run(["git", "-C", str(self.root), "show", f"{self.ref}:{rel}"],
                                   capture_output=True, text=True)
                self._c[rel] = p.stdout if p.returncode == 0 else None
            else:
                f = self.root / rel
                self._c[rel] = f.read_text() if f.is_file() else None
        return self._c[rel]

    def need(self, rel: str) -> str:
        s = self.read(rel)
        if s is None:
            raise ExtractError(f"missing {rel}")
        return s

    def ls(self, rel_dir: str) -> list[str]:
        if self.ref:
            p = subprocess.run(["git", "-C", str(self.root), "ls-tree", "--name-only", f"{self.ref}:{rel_dir}"],
                               capture_output=True, text=True)
            names = p.stdout.split() if p.returncode == 0 else []
        else:
            d = self.root / rel_dir
            names = [x.name for x in d.iterdir()] if d.is_dir() else []
        return sorted(f"{rel_dir}/{n}" for n in names)


def rules_block(page: str) -> str:
    try:
        return page[page.index("<!-- Rules -->"):page.index("<!-- Champion -->")]
    except ValueError:
        raise ExtractError("cfb-genius.html: Rules block markers not found")


def _text(fragment: str) -> str:
    t = _html.unescape(re.sub(r"<[^>]+>", "", fragment))
    return re.sub(r"\s+", " ", t).strip()


def block_text(block: str) -> str:
    """The rules page as a reader sees it (one line per heading/item)."""
    parts = re.findall(r"<(h1|h2|p|li)[^>]*>(.*?)</\1>", block, re.S)
    return "\n".join(_text(x) for _, x in parts)


def js_function(src: str, name: str) -> str:
    """Source of `function name(...) {...}` (brace-matched; strings/comments
    are plain enough on this site that a brace count is exact)."""
    m = re.search(rf"(?:async\s+)?function\s+{re.escape(name)}\s*\(", src)
    if not m:
        raise ExtractError(f"js function {name} not found")
    i = src.index("{", m.end())
    depth = 0
    for j in range(i, len(src)):
        c = src[j]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return src[m.start():j + 1]
    raise ExtractError(f"js function {name}: unbalanced braces")


# ---- SQL: replay migrations in order --------------------------------------

_FN_RX = re.compile(r"create\s+(?:or\s+replace\s+)?function\s+(?:public\.)?(\w+)\s*\(", re.I)


def _dollar_body_end(sql: str, start: int) -> int:
    """End of a CREATE FUNCTION statement: past its $tag$...$tag$ body."""
    m = re.compile(r"\$(\w*)\$").search(sql, start)
    if not m:
        return sql.find(";", start) + 1 or len(sql)
    tag = m.group(0)
    close = sql.find(tag, m.end())
    if close < 0:
        return len(sql)
    semi = sql.find(";", close + len(tag))
    return semi + 1 if semi >= 0 else len(sql)


def sql_functions(repo: Repo) -> dict[str, str]:
    """name -> the CREATE FUNCTION text that is in force after every migration."""
    out: dict[str, str] = {}
    for rel in repo.ls("supabase/migrations"):
        if not rel.endswith(".sql"):
            continue
        sql = repo.read(rel) or ""
        for m in _FN_RX.finditer(sql):
            out[m.group(1).lower()] = sql[m.start():_dollar_body_end(sql, m.end())]
    return out


_POL_RX = re.compile(r"create\s+policy\s+(\"[^\"]+\"|\w+)\s+on\s+(?:public\.)?(\w+)(.*?);", re.I | re.S)


def sql_policies(repo: Repo) -> dict[str, dict]:
    """policy name -> {table, cmd, restrictive, text} in force after every migration."""
    out: dict[str, dict] = {}
    for rel in repo.ls("supabase/migrations"):
        if not rel.endswith(".sql"):
            continue
        sql = repo.read(rel) or ""
        for m in re.finditer(r"drop\s+policy\s+if\s+exists\s+(\"[^\"]+\"|\w+)\s+on", sql, re.I):
            out.pop(m.group(1).strip('"'), None)
        for m in _POL_RX.finditer(sql):
            body = m.group(3)
            cmd = re.search(r"\bfor\s+(select|insert|update|delete|all)\b", body, re.I)
            out[m.group(1).strip('"')] = {
                "table": m.group(2).lower(), "cmd": (cmd.group(1) if cmd else "all").upper(),
                "restrictive": bool(re.search(r"\bas\s+restrictive\b", body, re.I)), "text": body.strip()}
    return out


def _fn(defs: dict[str, str], name: str) -> str:
    s = defs.get(name)
    if not s:
        raise ExtractError(f"SQL function {name} not defined")
    return s


def _code(sql: str) -> str:
    """SQL without -- comments (values come from code, not prose)."""
    return "\n".join(ln.split("--", 1)[0] for ln in sql.splitlines())


# =============================================================================
# extraction: code -> facts
# =============================================================================

def extract_sql(defs: dict[str, str]) -> dict:
    f: dict = {}
    # --- grading: set_winner_on_final -------------------------------------
    s = _code(_fn(defs, "set_winner_on_final"))
    m = re.search(r"\bif\s+([^\n]+?)\s+then\s*new\.\"?winner\"?\s*:=\s*new\.\"?(\w+)\"?\s*;\s*else\s*"
                  r"new\.\"?winner\"?\s*:=\s*new\.\"?(\w+)\"?", s, re.I | re.S)
    if not m:
        raise ExtractError("set_winner_on_final: cover rule not found")
    cond = re.sub(r"\s+", " ", m.group(1)).strip()
    f["cover"] = {"cond": cond, "true": m.group(2), "else": m.group(3)}
    f["grades_when"] = {"needs_final": bool(re.search(r"\"?Status\"?\s*=\s*'final'", s, re.I)),
                        "needs_line": bool(re.search(r"\"?line\"?\s+is\s+not\s+null", s, re.I)),
                        "tiebreakers_graded": bool(re.search(r"tiebreaker\"?\s*=\s*true", s, re.I))}
    # --- half-point lines ------------------------------------------------
    s = _code(_fn(defs, "half_point_line"))
    f["half_point"] = {
        "whole_moves_random": bool(re.search(r"floor\(new\.line\)\s*\+\s*case\s+when\s+random\(\)", s, re.I)),
        "others_to_floor_half": bool(re.search(r"new\.line\s*:=\s*floor\(new\.line\)\s*\+\s*0\.5\s*;", s, re.I)),
        "keeps_within": float(m.group(1)) if (m := re.search(r"abs\(new\.line\s*-\s*old\.line\)\s*<=\s*([\d.]+)", s)) else None,
    }
    if not (f["half_point"]["whole_moves_random"] and f["half_point"]["others_to_floor_half"]):
        raise ExtractError("half_point_line: rounding rule not found")
    # --- line lock: line_lock_at -----------------------------------------
    s = _code(_fn(defs, "line_lock_at"))
    m = re.search(r"end\)\s*-\s*(\d+)\s*\)\s*::\s*timestamp\s*\+\s*time\s*'(\d{1,2}):(\d{2})'", s, re.I)
    if not m or "when 0 then" not in s.lower():
        raise ExtractError("line_lock_at: lock offset not found")
    f["line_lock"] = {"days_before_saturday": int(m.group(1)), "hour": int(m.group(2)), "minute": int(m.group(3))}
    # --- pick lock: picks_open_for_game ----------------------------------
    s = _code(_fn(defs, "picks_open_for_game"))
    m = re.search(r"when\s+g\.week\s*>\s*(\d+)\s+then", s, re.I)
    if not m:
        raise ExtractError("picks_open_for_game: playoff week split not found")
    f["last_regular_week"] = int(m.group(1))
    f["pick_lock"] = {
        "playoff_per_game": bool(re.search(r"\(g\.\"Start \(CT\)\"\)::timestamp\s*>\s*\(now\(\)", s, re.I)),
        "week_together": bool(re.search(r"x\.week\s*=\s*g\.week", s, re.I)),
        "tiebreakers_lock_week": bool(re.search(r"x\.tiebreaker", s, re.I)),
        "unknown_start_closed": bool(re.search(r"\"Start \(CT\)\"\s+is\s+null\s+then\s+false", s, re.I)),
    }
    # --- weeks: calc_cfb_week --------------------------------------------
    s = _code(_fn(defs, "calc_cfb_week"))
    f["weeks"] = {"week1_is_labor_day_saturday": bool(re.search(r"labor_day\s*-\s*2", s)),
                  "jan_feb_prior_season": bool(re.search(r"in\s*\(1,\s*2\)", s))}
    f["first_week"] = 0 if f["weeks"]["week1_is_labor_day_saturday"] else 1
    # --- slots jackpot -----------------------------------------------------
    s = _code(_fn(defs, "slots_jackpot_odds"))
    m = re.search(r"least\(\s*([\d.]+)\s*\+\s*greatest\(coalesce\(pulls,\s*0\)\s*-\s*(\d+),\s*0\)\s*\*\s*([\d.]+),\s*([\d.]+)\s*\)", s)
    if not m:
        raise ExtractError("slots_jackpot_odds: ramp not found")
    f["jackpot"] = {"base": float(m.group(1)), "quiet_pulls": int(m.group(2)),
                    "step": float(m.group(3)), "max": float(m.group(4))}
    s = _code(_fn(defs, "slots_jackpot_odds_for"))
    m = re.search(r"survive\s+constant\s+numeric\s*:=\s*([\d.]+)", s)
    f["jackpot"]["regular_survive"] = float(m.group(1)) if m else None
    m = re.search(r"min_per_week\s+constant\s+numeric\s*:=\s*([\d.]+)", s)
    f["jackpot"]["regular_min_pulls_per_week"] = float(m.group(1)) if m else None
    f["jackpot"]["one_pop_per_regular_season"] = bool(re.search(r"else\s+return\s+0\s*;", s, re.I))
    f["jackpot"]["regular_weeks_left_ramp"] = bool(re.search(r"weeks_left\s*\*\s*pulls_per_week", s))
    if f["jackpot"]["regular_survive"] is None or f["jackpot"]["regular_min_pulls_per_week"] is None:
        raise ExtractError("slots_jackpot_odds_for: weeks-left ramp not found")
    s = _code(_fn(defs, "resolve_slot_picks"))
    f["jackpot"]["coin_flip_side"] = bool(re.search(r"random\(\)\s*<\s*0\.5\s+then\s+r\.away", s, re.I))
    f["jackpot"]["one_pop_per_batch"] = "paid_out" in s
    # --- the Pit -------------------------------------------------------------
    deal = _code(_fn(defs, "bj_deal"))
    settle = _code(_fn(defs, "bj_settle_week"))
    dbl = _fn(defs, "bj_double")
    start = re.search(r"coalesce\(chips,\s*(\d+)\)", deal)
    lo = re.search(r"lo\s*:=\s*least\((\d+),\s*chips\)", deal)
    hi = re.search(r"hi\s*:=\s*least\((\d+),\s*chips\)", deal)
    stand = re.search(r"bj_total\(dealer\)\s*<\s*(\d+)", settle)
    bj = re.search(r"pay\s*:=\s*\(h\.bet\s*\*\s*(\d+)\)\s*/\s*(\d+)", settle)
    if not (start and lo and hi and stand and bj):
        raise ExtractError("bj_*: table constants not found")
    f["pit"] = {"start_chips": int(start.group(1)), "min_bet": int(lo.group(1)), "max_bet": int(hi.group(1)),
                "dealer_stands_on": int(stand.group(1)), "blackjack_pays": [int(bj.group(1)), int(bj.group(2))],
                "integer_division": True, "double_first_only": "first decision" in dbl,
                "busted_out": bool(re.search(r"chips\s*<=\s*0\s*then\s*raise", deal))}
    return f


def extract_policies(pols: dict[str, dict]) -> dict:
    # Migrations say public.fn(), pg_policies says fn(): compare without it.
    pols = {k: dict(v, text=re.sub(r"\bpublic\.", "", v["text"])) for k, v in pols.items()}

    def has(table, cmd, rx):
        return any(p["table"] == table and p["cmd"] in (cmd, "ALL") and re.search(rx, p["text"], re.I | re.S)
                   and not p["restrictive"] for p in pols.values())

    def restr(table, rx):
        return any(p["table"] == table and p["restrictive"] and re.search(rx, p["text"], re.I | re.S) for p in pols.values())
    c = r"is_commissioner\(\)"
    return {
        "commish_edits_any_pick": has("picks", "UPDATE", c) and has("picks", "INSERT", c) and has("picks", "DELETE", c),
        "commish_past_deadline": restr("picks", r"is_commissioner\(\)\s+or\s+picks_open_for_game"),
        "commish_sees_hidden": restr("picks", r"is_commissioner\(\)\s+or\s+owns_team.*not\s+picks_open_for_game"),
        "hidden_until_lock": restr("picks", r"not\s+picks_open_for_game"),
        "commish_edits_games": has("all_games", "UPDATE", c),
        "commish_sets_seeds": has("playoffs", "ALL", c),
        "jackpot_house_only": restr("picks", r"not\s+jackpot\)\s+or\s+is_commissioner|is_commissioner\(\)\s+or\s+\(?not\s+jackpot"),
        "exhibition_cant_pick": restr("picks", r"exhibition_team_ids"),
    }


# playoff_compute's criterion keys -> a word the rules page's list item has.
CRIT_PAGE_WORD = {"conf": "conference championship", "army_navy": "army", "chips": "chips",
                  "celebration": "celebration", "last8": "last 8"}


def extract_playoffs(defs: dict[str, str], repo: Repo) -> dict:
    """The playoff autopilot (migration 20261116120000) and the playoff-field
    pick rule (20261116140000), read out of their SQL and the pages' JS."""
    s = _code(_fn(defs, "playoff_compute"))
    f: dict = {}
    wk = set(re.findall(r"week\s+between\s+(\d+)\s+and\s+(\d+)", s, re.I))
    if len(wk) != 1:
        raise ExtractError(f"playoff_compute: regular-season week range not found or inconsistent: {wk}")
    lo, hi = (int(x) for x in wk.pop())
    f["regular_weeks"] = [lo, hi]
    n_min = re.search(r"if\s+v_n\s*<\s*(\d+)\s+then", s, re.I)
    slots = re.search(r"v_slots\s*:=\s*(\d+)\s*-\s*v_above", s, re.I)
    if not (n_min and slots) or n_min.group(1) != slots.group(1):
        raise ExtractError("playoff_compute: playoff spots not found or inconsistent")
    f["spots"] = int(slots.group(1))
    m = re.search(r"v_settle\s*:=\s*v_w15_last\s*\+\s*interval\s*'(\d+)\s*hours?'", s, re.I)
    if not m:
        raise ExtractError("playoff_compute: settle delay not found")
    f["settle_hours"] = int(m.group(1))
    m = re.search(r"v_crits\s+text\[\]\s*:=\s*array\[([^\]]+)\]", s, re.I)
    if not m:
        raise ExtractError("playoff_compute: tiebreaker cascade not found")
    f["cascade"] = re.findall(r"'(\w+)'", m.group(1))
    m = re.search(r"v_label\s+jsonb\s*:=\s*jsonb_build_object\((.*?)\);", s, re.I | re.S)
    f["labels"] = dict(re.findall(r"'(\w+)'\s*,\s*'([^']*)'", m.group(1))) if m else {}
    m = re.search(r"blackjack_bankrolls\s+b\s+where[^)]*\)\s*,\s*(\d+)\s*\)", s, re.I)
    f["missing_chips"] = int(m.group(1)) if m else None
    m = re.search(r"rn\s*<=\s*(\d+)", s)
    f["last_n"] = int(m.group(1)) if m else None
    m = re.search(r"when\s+week\s+is\s+null\s+or\s+week\s*<\s*(\d+)\s+then\s+'conf'", s, re.I)
    f["conf_tiebreakers_before_week"] = int(m.group(1)) if m else None
    f["army_navy_is_tiebreaker_game"] = bool(re.search(r"lower\(\"Away\"\)\s+in\s+\('army',\s*'navy'\)", s, re.I))
    f["celebration_is_hbcu_tiebreaker"] = bool(re.search(r"_po_is_hbcu\(\"Away\"\)\s+and\s+_po_is_hbcu\(\"Home\"\)", s, re.I))
    f["excludes_exhibition"] = bool(re.search(r"is_exhibition\s*,\s*false\)\s*=\s*false", s, re.I))
    f["jackpot_counts"] = bool(re.search(r"coalesce\(p\.jackpot\s*,\s*false\)\s+or\s+lower\(trim\(p\.pick\)\)", s, re.I))
    f["pending_only_if_split"] = bool(re.search(r"count\(distinct\s+coalesce\(lower\(trim\(p\.pick\)\)", s, re.I))
    f["coin_flip_held"] = "'coin_flip'" in s
    f["force_refuses_coin_flip"] = bool(re.search(r"p_force.*?'code'\s+in\s+\('coin_flip'", s, re.I | re.S))
    f["hold_codes"] = sorted(set(re.findall(r"'code'\s*,\s*'(\w+)'", s)))
    # -- who can pick in the playoffs
    b = _code(_fn(defs, "playoff_pick_blocked"))
    m = re.search(r"g\.week\s*>\s*(\d+)", b)
    f["field_only_after_week"] = int(m.group(1)) if m else None
    f["field_only_once_seeded"] = bool(re.search(r"exists\s*\(select\s+1\s+from\s+(?:public\.)?playoffs\s+po\s+where\s+po\.season\s*=\s*g\.cfb_season\)", b, re.I))
    f["field_only_seeds_pass"] = bool(re.search(r"po\.team_id\s*=\s*p_team_id", b, re.I))
    t = _code(_fn(defs, "picks_playoff_field_only"))
    f["field_only_commish_exempt"] = bool(re.search(r"if\s+(?:public\.)?is_commissioner\(\)\s+then\s+return\s+new", t, re.I))
    m = re.search(r"raise\s+exception\s+'((?:[^']|'')+)'", t, re.I)
    f["field_only_message"] = m.group(1).replace("''", "'") if m else None
    if not (f["field_only_after_week"] and f["field_only_once_seeded"] and f["field_only_seeds_pass"]):
        raise ExtractError("playoff_pick_blocked: playoff-field rule not found")
    # -- autopilot: schedule, hand seeds win, commissioner actions
    tick = _code(_fn(defs, "playoff_autopilot_tick"))
    f["hand_seeds_win"] = bool(re.search(r"if\s+exists\s*\(select\s+1\s+from\s+playoffs\s+where\s+season\s*=\s*v_season\)\s+then", tick, re.I))
    adm = _code(_fn(defs, "playoff_autopilot_admin"))
    acts = re.findall(r"p_action\s*=\s*'(\w+)'", adm) + [a for g in re.findall(r"p_action\s+in\s*\(([^)]*)\)", adm)
                                                         for a in re.findall(r"'(\w+)'", g)]
    f["admin_actions"] = sorted(set(acts))
    f["undo_pauses"] = bool(re.search(r"delete\s+from\s+playoffs.*?enabled\s*=\s*false", adm, re.I | re.S))
    cron = None
    for rel in repo.ls("supabase/migrations"):
        m = re.search(r"cron\.schedule\(\s*'playoff-autopilot'\s*,\s*'([^']+)'", repo.read(rel) or "")
        cron = m.group(1) if m else cron
    f["cron"] = cron
    m = re.fullmatch(r"\*/(\d+) \* \* \* \*", cron or "")
    f["tick_minutes"] = int(m.group(1)) if m else None
    # -- the pages: the switch is rows in `playoffs`
    ps = repo.read("js/playoffState.js") or ""
    f["switch_is_seed_rows"] = bool(re.search(r"from\('playoffs'\)", ps) and re.search(r"state\.active\s*=\s*state\.seeds\.length\s*>\s*0", ps))
    pk = repo.read("picks.html") or ""
    f["eliminated_view_only"] = "VIEW ONLY" in pk and "Only the four playoff teams" in pk
    return f


def extract_site(repo: Repo) -> dict:
    f: dict = {}
    block = rules_block(repo.need("cfb-genius.html"))
    f["rules_page_text"] = block_text(block)
    lists = re.findall(r"<ol[^>]*>(.*?)</ol>", block, re.S)
    if len(lists) != 2:
        raise ExtractError("rules page: expected regular + playoff tiebreaker lists")
    f["tiebreakers"] = {k: [_text(li) for li in re.findall(r"<li>(.*?)</li>", ol, re.S)]
                        for k, ol in zip(("regular", "playoff"), lists)}
    txt = f["rules_page_text"]
    m = re.search(r"Every week:\s*(\d+) games against the spread", txt)
    if not m:
        raise ExtractError("rules page: games a week not found")
    f["games_per_week"] = int(m.group(1))
    pay = {}
    for place in ("1st", "2nd", "3rd"):
        m = re.search(rf"^{place}:\s*(.+)$", txt, re.M)
        if not m:
            raise ExtractError(f"rules page: {place} payout not found")
        pay[place] = m.group(1).strip()
    f["payout_text"] = pay
    f["payouts"] = {}
    for place, t in pay.items():
        if re.fullmatch(r"remainder of buy-in", t, re.I):
            f["payouts"][place] = ["remainder"]
        elif m := re.fullmatch(r"buy-in \+ \$(\d+)", t, re.I):
            f["payouts"][place] = ["buy_in_plus", int(m.group(1))]
        elif re.fullmatch(r"buy-in back", t, re.I):
            f["payouts"][place] = ["buy_in"]
        else:
            raise ExtractError(f"rules page: can't read the {place} payout '{t}'")
    m = re.search(r"Top (\d+) advance", txt)
    f["playoff_spots_page"] = int(m.group(1)) if m else None
    f["playoff_scores_reset"] = bool(re.search(r"scores reset", txt, re.I))
    f["line_lock_page"] = (m.group(1) if (m := re.search(r"Lines lock:\s*([^.]+)\.", txt)) else None)
    f["pit_page"] = [ln for ln in txt.splitlines() if re.search(r"chips|dealer|blackjack|bust|bet", ln, re.I)
                     and not re.match(r"Chips — the blackjack", ln)]
    # --- JS ------------------------------------------------------------------
    idx = repo.need("index.html")
    reg = js_function(idx, "computeSeasonWinsThroughWeek")
    m = re.search(r"if\s*\((p\.jackpot\s*\|\|\s*sameTeam\(p\.pick,\s*winner\))\)", reg)
    if not m:
        raise ExtractError("index.html: scoring rule not found")
    f["score_cond"] = m.group(1)
    f["score_excludes_tiebreakers"] = "g.tiebreaker !== true" in reg
    m = re.search(r"Number\(g\.week\)\s*<=\s*(\d+)", reg)
    f["last_regular_week_js"] = int(m.group(1)) if m else None
    po = js_function(idx, "computePlayoffWins")
    m = re.search(r"\.gt\('week',\s*(\d+)\)", po)
    f["playoff_tally_after_week"] = int(m.group(1)) if m else None
    m = re.search(r"pointsArray\[Math\.min\((\d+),", idx)
    f["cut_index_js"] = int(m.group(1)) if m else None
    m = re.search(r"PLAYOFF_LINE_FROM_WEEK\s*=\s*(\d+)", idx)
    f["projected_line_from_week"] = int(m.group(1)) if m else None
    sh = repo.read("js/seasonHistory.js") or ""
    m = re.search(r"export function isWin\(p, winner\)\s*\{\s*return (.+?);\s*\}", sh, re.S)
    f["is_win_js"] = m.group(1).strip() if m else None
    st = repo.read("stats.html") or ""
    m = re.search(r"const GAMES_PER_WEEK = (\d+), LAST_REGULAR_WEEK = (\d+), SPOTS = (\d+);", st)
    f["stats_consts"] = [int(x) for x in m.groups()] if m else None
    m = re.search(r"const CUT_SPOTS = (\d+);", repo.read("js/seasonRace.js") or "")
    f["race_cut_spots"] = int(m.group(1)) if m else None
    m = re.search(r"const seeds = \[([\d,\s]+)\];", repo.read("commissioner.html") or "")
    f["commish_seeds"] = len(m.group(1).split(",")) if m else None
    return f


def extract(repo: Repo | None = None, live_defs: dict[str, str] | None = None,
            live_policies: dict[str, dict] | None = None) -> tuple[dict, dict[str, str]]:
    """(facts, errors by section). Values come from the live database
    definitions when given, else from the migrations in the repo."""
    repo = repo or Repo()
    errors: dict[str, str] = {}
    facts: dict = {"hand": HAND}
    defs = live_defs or sql_functions(repo)
    try:
        facts.update(extract_sql(defs))
    except ExtractError as e:
        for s in ("FORMAT", "DEADLINE AND PRIVACY", "LET THE SLOTS DECIDE", "THE PIT", "STANDINGS AND PLAYOFFS"):
            errors[s] = str(e)
    facts["policies"] = extract_policies(live_policies or sql_policies(repo))
    try:
        facts.update(extract_site(repo))
    except ExtractError as e:
        for s in ("FORMAT", "STANDINGS AND PLAYOFFS", "PAYOUTS", "TIEBREAKERS", "THE PIT"):
            errors.setdefault(s, str(e))
    try:
        facts["playoffs"] = extract_playoffs(defs, repo)
    except ExtractError as e:
        for s in ("STANDINGS AND PLAYOFFS", "TIEBREAKERS"):
            errors.setdefault(s, str(e))
    po = facts.get("playoffs") or {}
    # Cross-checks: the same number written in several places must agree.
    lr = facts.get("last_regular_week")
    others = {"index.html": facts.get("last_regular_week_js"),
              "index.html playoff tally": facts.get("playoff_tally_after_week"),
              "stats.html": (facts.get("stats_consts") or [None, None])[1],
              "playoff_compute": (po.get("regular_weeks") or [None, None])[1],
              "playoff_pick_blocked": po.get("field_only_after_week")}
    bad = {k: v for k, v in others.items() if v is not None and v != lr}
    if lr is not None and bad:
        errors.setdefault("STANDINGS AND PLAYOFFS", f"last regular-season week disagrees: SQL {lr} vs {bad}")
    spots = {"index.html cut": (facts["cut_index_js"] + 1) if facts.get("cut_index_js") is not None else None,
             "stats.html": (facts.get("stats_consts") or [None, None, None])[2],
             "seasonRace.js": facts.get("race_cut_spots"), "commissioner seeds": facts.get("commish_seeds"),
             "rules page": facts.get("playoff_spots_page"), "playoff_compute": po.get("spots")}
    vals = {v for v in spots.values() if v is not None}
    if len(vals) == 1:
        facts["playoff_spots"] = vals.pop()
    else:
        errors.setdefault("STANDINGS AND PLAYOFFS", f"playoff spots disagree across the site: {spots}")
        facts["playoff_spots"] = spots.get("rules page") or 4
    gp = (facts.get("stats_consts") or [None])[0]
    if facts.get("games_per_week") and gp and gp != facts["games_per_week"]:
        errors.setdefault("FORMAT", f"games a week: rules page {facts['games_per_week']} vs stats.html {gp}")
    if facts.get("score_cond") and facts.get("is_win_js") and "p.jackpot" not in facts["is_win_js"]:
        errors.setdefault("FORMAT", "seasonHistory.isWin no longer scores a jackpot")
    ll, lp = facts.get("line_lock"), facts.get("line_lock_page")
    if ll and lp:
        m = re.fullmatch(r"(\w+day) (\d{1,2}):(\d{2}) (AM|PM) CT", lp.strip())
        page = (DAYS.index(m.group(1)), int(m.group(2)) % 12 + (12 if m.group(4) == "PM" else 0), int(m.group(3))) if m and m.group(1) in DAYS else None
        code = ((5 - ll["days_before_saturday"]) % 7, ll["hour"], ll["minute"])
        if page != code:
            errors.setdefault("FORMAT", f"line lock: rules page says '{lp}', line_lock_at() says {code}")
    if po and facts.get("first_week") is not None and po["regular_weeks"][0] != facts["first_week"]:
        errors.setdefault("STANDINGS AND PLAYOFFS",
                          f"first week: playoff_compute {po['regular_weeks'][0]} vs calc_cfb_week {facts['first_week']}")
    if po and facts.get("pit") and po.get("missing_chips") not in (None, facts["pit"]["start_chips"]):
        errors.setdefault("TIEBREAKERS", f"chips for a team with no Pit row: playoff_compute {po['missing_chips']} "
                                         f"vs bj_deal start {facts['pit']['start_chips']}")
    if po and facts.get("tiebreakers"):
        err = cascade_mismatch(po, facts["tiebreakers"]["regular"])
        if err:
            errors.setdefault("TIEBREAKERS", err)
    return facts, errors


def cascade_mismatch(po: dict, page_regular: list[str]) -> str | None:
    """playoff_compute's tie cascade must be the rules page's regular-season
    list, in order, with the page's coin flip as the commissioner hold."""
    page = [x.lower() for x in page_regular]
    want = [CRIT_PAGE_WORD.get(c) for c in po["cascade"]]
    if None in want:
        return f"playoff_compute uses a tiebreaker the rules page doesn't name: {po['cascade']}"
    head = page[:len(want)]
    if len(head) != len(want) or not all(w in h for w, h in zip(want, head)):
        return f"tiebreaker order: rules page {page_regular} vs playoff_compute {po['cascade']}"
    rest = page[len(want):]
    if rest != ["coin flip"] or not po.get("coin_flip_held"):
        return f"after the computed tiebreakers the page has {rest}; playoff_compute coin-flip hold: {po.get('coin_flip_held')}"
    m = re.search(r"last (\d+)", " ".join(page))
    if m and po.get("last_n") and int(m.group(1)) != po["last_n"]:
        return f"last-N tiebreaker: page {m.group(1)} vs playoff_compute {po['last_n']}"
    return None


# =============================================================================
# the rules as code (Python ports of the site's logic; tests run them against
# the site's own JS and SQL)
# =============================================================================

def eval_cover_cond(cond: str, home_pts: float, away_pts: float, line: float) -> bool:
    """Evaluate set_winner_on_final's own condition text on numbers."""
    expr = re.sub(r'new\."?HomePts"?', repr(float(home_pts)), cond)
    expr = re.sub(r'new\."?AwayPts"?', repr(float(away_pts)), expr)
    expr = re.sub(r'new\."?line"?', repr(float(line)), expr)
    if not re.fullmatch(r"[\d.\s+\-*/()<>=]+", expr):
        raise ExtractError(f"cover rule has terms this evaluator doesn't know: {cond}")
    return bool(eval(expr.replace("=", "==").replace("<==", "<=").replace(">==", ">="), {"__builtins__": {}}))


def cover_margin(home_pts, away_pts, line) -> float:
    """Points the home side covers by (negative: away covers)."""
    return float(home_pts) + float(line) - float(away_pts)


def cover_winner(home: str, away: str, home_pts, away_pts, line) -> str:
    """The side the league grades as covered. Dead even goes to Away, like
    set_winner_on_final (can't happen on half-point lines)."""
    return home if cover_margin(home_pts, away_pts, line) > 0 else away


def is_point(pick: str | None, winner: str | None, jackpot: bool = False, tiebreaker: bool = False) -> bool:
    """A pick scores when the game is graded and it matches the covering side,
    or the slots jackpot popped on it. Tiebreaker games never score."""
    if tiebreaker or winner is None:
        return False
    return bool(jackpot) or (pick is not None and str(pick).strip().lower() == str(winner).strip().lower())


def half_point(line: float, old: float | None = None, coin: float = 0.0) -> float:
    """half_point_line(): keep a stored k.5 against a restatement within half a
    point; move a whole number half a point (coin < 0.5: up, else down);
    anything else to floor + 0.5."""
    if old is not None and old == math.floor(old) + 0.5 and abs(line - old) <= 0.5:
        return old
    if line == math.floor(line):
        return math.floor(line) + (0.5 if coin < 0.5 else -0.5)
    return math.floor(line) + 0.5


def _saturday_anchor(d: dt.date) -> dt.date:
    wd = (d.weekday() + 1) % 7  # Postgres dow: Sunday 0
    return d - dt.timedelta(days=1) if wd == 0 else d - dt.timedelta(days=2) if wd == 1 else d + dt.timedelta(days=6 - wd)


def line_lock_at(start_ct: dt.datetime, facts: dict | None = None) -> dt.datetime:
    """When this game's line locks (naive CT), as line_lock_at() computes it."""
    ll = (facts or FACTS)["line_lock"]
    d = _saturday_anchor(start_ct.date()) - dt.timedelta(days=ll["days_before_saturday"])
    return dt.datetime.combine(d, dt.time(ll["hour"], ll["minute"]))


def calc_cfb_week(start_ct: dt.datetime) -> int:
    d = start_ct.date()
    season = d.year - 1 if d.month in (1, 2) else d.year
    sep1 = dt.date(season, 9, 1)
    labor = sep1 + dt.timedelta(days=((1 - (sep1.weekday() + 1) % 7) + 7) % 7)
    return (_saturday_anchor(d) - (labor - dt.timedelta(days=2))).days // 7 + 1


def _start(g: dict) -> dt.datetime | None:
    s = g.get("start") or g.get("Start (CT)")
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(str(s).replace("Z", "")[:19])
    except ValueError:
        return None


def game_started(g: dict, now_ct: dt.datetime) -> bool:
    """js/kickoff.js isGameLocked: final/live or a winner counts as started; no
    kickoff time = not started."""
    st = str(g.get("Status") or g.get("status") or "").lower()
    if st in ("final", "in", "live") or g.get("winner"):
        return True
    s = _start(g)
    return s is not None and now_ct >= s


def week_locks_together(week, facts: dict | None = None) -> bool:
    try:
        return int(week) <= (facts or FACTS)["last_regular_week"]
    except (TypeError, ValueError):
        return False  # unknown: per game, which hides more


def week_locked(games: list[dict], now_ct: dt.datetime) -> bool:
    """js/kickoff.js isWeekLocked over every game of the week, tiebreakers too."""
    return any(game_started(g, now_ct) for g in games or [])


def picks_open(game: dict, week_games: list[dict], now_ct: dt.datetime, facts: dict | None = None) -> bool:
    """picks_open_for_game(): unknown week/kickoff is closed; playoffs lock per
    game; a regular week closes when any slate or tiebreaker game kicks off."""
    if game.get("week") is None or _start(game) is None:
        return False
    if not week_locks_together(game["week"], facts):
        return _start(game) > now_ct
    return not any((x.get("picked") or x.get("tiebreaker")) and _start(x) is not None and _start(x) <= now_ct
                   for x in week_games if x.get("week") == game["week"])


def cut_line(points: list[int], spots: int | None = None) -> int | None:
    """Season points of the last playoff spot (index.html: the 4th-best total)."""
    spots = spots or FACTS["playoff_spots"]
    p = sorted(points, reverse=True)
    return p[min(spots, len(p)) - 1] if p else None


def playoff_status(points: dict[str, int], spots: int | None = None) -> dict[str, str]:
    """'in' (clear of the cut), 'tied at the cut' (tiebreakers decide), 'out'."""
    spots = spots or FACTS["playoff_spots"]
    cut = cut_line(list(points.values()), spots)
    at_or_above = sum(1 for v in points.values() if v >= cut) if cut is not None else 0
    out = {}
    for t, v in points.items():
        if v > cut or (v == cut and at_or_above <= spots):
            out[t] = "in"
        elif v == cut:
            out[t] = "tied at the cut"
        else:
            out[t] = "out"
    return out


def jackpot_odds(pulls: int, facts: dict | None = None) -> float:
    j = (facts or FACTS)["jackpot"]
    return min(j["base"] + max((pulls or 0) - j["quiet_pulls"], 0) * j["step"], j["max"])


def regular_season_odds(pulls: int, weeks_left: int, pulls_per_week: float, popped_this_season: bool,
                        facts: dict | None = None) -> float:
    j = (facts or FACTS)["jackpot"]
    if popped_this_season:
        return 0.0
    n = max(weeks_left, 1) * max(pulls_per_week, j["regular_min_pulls_per_week"])
    return max(1 - j["regular_survive"] ** (1 / n), jackpot_odds(pulls, facts))


def blackjack_payout(bet: int, facts: dict | None = None) -> int:
    a, b = (facts or FACTS)["pit"]["blackjack_pays"]
    return (bet * a) // b


def payout_math(n_teams: int, facts: dict | None = None) -> dict:
    f = facts or FACTS
    buy = f["hand"]["buy_in"]
    pot = buy * n_teams
    fixed = {}
    for place, rule in f["payouts"].items():
        if rule[0] == "buy_in_plus":
            fixed[place] = buy + rule[1]
        elif rule[0] == "buy_in":
            fixed[place] = buy
    rem = pot - sum(fixed.values())
    pay = {p: fixed.get(p, rem) for p in f["payouts"]}
    return {"n": n_teams, "pot": pot, "first": pay.get("1st"), "second": pay.get("2nd"), "third": pay.get("3rd")}


def tiebreakers(phase: str = "regular") -> list[str]:
    return list(FACTS["tiebreakers"][phase])


# =============================================================================
# rendering: facts -> the digest Picky reads
# =============================================================================

def _ampm(h: int, m: int) -> str:
    return f"{(h % 12) or 12}:{m:02d} {'AM' if h < 12 else 'PM'}"


DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _eastern(ll: dict) -> str:
    h = ll["hour"] + 1
    return " (noon Eastern)" if (h, ll["minute"]) == (12, 0) else f" ({_ampm(h % 24, ll['minute'])} Eastern)"


def _pct(x: float) -> str:
    s = f"{x * 100:.4f}".rstrip("0").rstrip(".")
    return f"{s}%"


def render_sections(f: dict) -> dict[str, list[str]]:
    first, last, n = f["first_week"], f["last_regular_week"], f["games_per_week"]
    cov = f["cover"]
    ll = f["line_lock"]
    lock_day = DAYS[(5 - ll["days_before_saturday"]) % 7]
    lock_time = f"{lock_day} {_ampm(ll['hour'], ll['minute'])} CT"
    pol, j, pit = f["policies"], f["jackpot"], f["pit"]
    spots = f["playoff_spots"]
    S: dict[str, list[str]] = {}

    home_rule = cov["cond"] == '(new."HomePts" + new."line") > new."AwayPts"' and cov["true"] == "Home"
    S["FORMAT"] = [
        f"- Regular season is weeks {first}-{last}. Every week the commissioner picks {n} games; you pick each one against the spread.",
        "- Scoring: 1 point per correct pick (your team covers). No pick = no point. No bonus points, no confidence weights."
        + (" Tiebreaker games never count for points." if f.get("score_excludes_tiebreakers") else ""),
        ("- The line is the home team's adjustment: home covers if home score + line > away score. Negative line = home favored."
         if home_rule else f"- Grading rule: {cov['true']} covers if {cov['cond']}, else {cov['else']}."),
        "- Every line is a half point (whole numbers get moved half a point), so a push can't happen."
        + (f" If a score ever landed exactly on the number, the grading gives it to the {cov['else'].lower()} team."
           if cov["else"] in ("Away", "Home") else ""),
        f"- Lines lock {lock_time}{_eastern(ll)}. The line at that moment is what the week is scored against. A game with no line yet keeps the first one posted.",
        "- A game is graded when it goes final with a line on it: the side that covered is recorded as the winner, and the points land then.",
    ]
    S["DEADLINE AND PRIVACY"] = [
        "- Regular season: all picks for the week lock at the scheduled start of the week's first game"
        + (", tiebreaker games included" if f["pick_lock"]["tiebreakers_lock_week"] else "")
        + ". A weather or TV delay doesn't buy more time.",
        "- Playoffs: each game locks at its own scheduled start." if f["pick_lock"]["playoff_per_game"] else "",
        ("- Everyone's picks stay hidden until they lock, then are revealed at that moment. You can always see your own."
         if pol["hidden_until_lock"] else ""),
        "- Picks save automatically; a pick only counts once it is saved. Picks can only be made on that week's slate games."
        + (" A game with no kickoff time on file can't be picked yet." if f["pick_lock"]["unknown_start_closed"] else ""),
        "- After the lock, only the commissioner can enter or change a pick." if pol["commish_past_deadline"] else "",
    ]
    S["LET THE SLOTS DECIDE"] = [
        "- Instead of picking a side, you can choose \"Let the slots decide\". At lock, the site flips a coin and picks a side for you."
        if j.get("coin_flip_side") else "- Instead of picking a side, you can choose \"Let the slots decide\".",
        "- Every slots pick is one pull on the league jackpot (the football); flipping a pick back and forth doesn't add pulls. "
        "If it pops on your pull, you get an AUTO WIN: a point for that game no matter who covers, scored when the game is graded.",
        # slots_jackpot_odds_for: max(1 - survive^(1/(weeks_left * pulls/week)), the meter ramp)
        (f"- Regular season: the football pops at most once a season, and an unhit football gets likelier every week "
         f"as the season runs out. Each week's chance of a pop is 1 - {j['regular_survive']:g}^(1/weeks left), counting this "
         f"week: {_pct(1 - j['regular_survive'])} in the last jackpot week (conference championship week), "
         f"{_pct(1 - j['regular_survive'] ** 0.5)} the week before, lower earlier. That is split across the league's "
         f"usual slots picks per week, so every slots pick that week has the same odds. Once it has popped, no more "
         f"regular-season pops."
         if j.get("one_pop_per_regular_season") else ""),
        f"- Playoffs: the odds start at {_pct(j['base'])} a pull and climb after {j['quiet_pulls']} pulls without a pop, up to {_pct(j['max'])}.",
        "- The football on the picks page shows how many pulls since the last pop, not the live odds.",
    ]
    po = f.get("playoffs") or {}
    S["STANDINGS AND PLAYOFFS"] = [
        f"- Season standings = total points from weeks {first}-{last}"
        + (" (correct picks plus slots AUTO WINs)" if po.get("jackpot_counts") else "")
        + ". Tiebreaker games and playoff games never count toward them. The site lists no weekly prize.",
        f"- Who makes the playoffs: the top {spots} in season points after week {last}. A tie for {_ord(spots)} place "
        "goes to the regular-season tiebreakers (see TIEBREAKERS)."
        + (f" The leaderboard draws the projected playoff line from week {f['projected_line_from_week']} on."
           if f.get("projected_line_from_week") else ""),
        (f"- The switch: the site moves to the playoffs on its own (the playoff autopilot checks every "
         f"{po['tick_minutes']} minutes) once week {last} has all kicked off, every picked regular-season game is graded "
         f"and {po['settle_hours']} hours have passed since week {last}'s last kickoff."
         + (" Seeds the commissioner enters by hand always win; the autopilot never touches a season that has them."
            if po.get("hand_seeds_win") else "")
         + (" A tie at the cut that survives every tiebreaker is a coin flip, and the commissioner sets the seeds by hand."
            if po.get("coin_flip_held") else "")
         if po.get("tick_minutes") and po.get("settle_hours") else ""),
        (f"- Seeds 1-{spots} go by points, then the same tiebreakers. They're cosmetic: the rules never use seeds."
         if po.get("cascade") else ""),
        f"- Playoffs: top {spots} advance, scores reset"
        + (f" (only playoff games, week {last + 1} on, count)" if f.get("playoff_tally_after_week") == last else "")
        + ", and they pick all playoff games. Each playoff game locks at its own kickoff.",
        (f"- Who can pick in the playoffs: once the season has its {spots} seeds, only those {spots} teams can pick "
         f"playoff games (week {po['field_only_after_week'] + 1} on, tiebreakers included). The site and the database "
         f"both block everyone else (\"{po.get('field_only_message') or 'playoff teams only'}\")."
         + (" Eliminated members can still see every game, and every pick once it locks (view only)."
            if po.get("eliminated_view_only") else "")
         + " Before the seeds exist, anyone can still pick."
         + (" The commissioner can enter or fix anyone's pick." if po.get("field_only_commish_exempt") else "")
         if po.get("field_only_after_week") is not None and po.get("field_only_once_seeded") else ""),
        ("- Picky (the exhibition player) is never ranked and never takes a playoff spot." if po.get("excludes_exhibition") else ""),
        "- " + f["hand"]["championship_week_note"],
    ]
    pm = payout_math(10, f)  # shape only; chat.py adds the computed pot
    S["PAYOUTS"] = [
        f"- Buy-in: ${f['hand']['buy_in']} per player ({f['hand']['buy_in_source']}).",
        "- " + " ".join(f"{p}: {_payout_words(r)}." for p, r in f["payouts"].items()) if pm else "",
        "- Picky the exhibition player never pays in and never gets paid.",
    ]
    tb = f["tiebreakers"]
    S["TIEBREAKERS"] = (
        ["TIEBREAKERS: how ties in the standings are broken. They use separate tiebreaker picks, which never count as points.",
         "Regular season tiebreakers, in order:"]
        + [f"{i}) {_tb_words(x)}" for i, x in enumerate(tb["regular"], 1)]
        + ["Playoff tiebreakers, in order:"]
        + [f"{i}) {_tb_words(x)}" for i, x in enumerate(tb["playoff"], 1)]
        + _tb_details(f))
    a, b = pit["blackjack_pays"]
    S["THE PIT"] = [
        "- One shared dealer hand a week. The table opens when the week's picks open and settles at first kickoff.",
        f"- Everyone starts the season with {pit['start_chips']:,} chips. Bets {pit['min_bet']}-{pit['max_bet']} "
        f"(anything left under {pit['min_bet']} is always a legal bet). You bet before seeing any cards.",
        f"- Blackjack pays {a}:{b} rounded down. Dealer stands on all {pit['dealer_stands_on']}s."
        + (" Double on your first decision only." if pit["double_first_only"] else "")
        + " Dealer blackjack takes original bets only.",
        "- Bust to zero and you're out of The Pit for the season. No reload." if pit["busted_out"] else "",
        "- Chips never become points. They only break ties.",
    ]
    powers = [x for x, ok in (
        ("picks each week's games and flags the tiebreaker games", pol["commish_edits_games"]),
        ("can set lines and enter scores", pol["commish_edits_games"]),
        ("can enter, change or delete any team's pick at any time, deadline or not", pol["commish_edits_any_pick"] and pol["commish_past_deadline"]),
        ("can see every pick before the lock", pol["commish_sees_hidden"]),
        ("enters the playoff seeds", pol["commish_sets_seeds"]),
    ) if ok]
    S["COMMISSIONER"] = [
        "- The commissioner " + ", ".join(powers[:-1]) + (", and " if len(powers) > 1 else "") + powers[-1] + "." if powers else "",
        "- Only the house (the site or the commissioner) can mark a jackpot AUTO WIN." if pol["jackpot_house_only"] else "",
        _autopilot_words(po),
        "- Anything the written rules don't cover (postponed, canceled or suspended games, disputes): ask the commissioner; it's their call.",
    ]
    S["EDGE CASES"] = [
        "- Missed the lock: no late picks and no default pick, so no point for that game (only the commissioner can enter one).",
        "- A pick'em (0) line gets moved to +0.5 or -0.5 like any whole number.",
        "- Postponed, canceled or suspended games: the written rules don't say. Ask the commissioner.",
        "- Picky (the exhibition player) can't be ranked, can't win money and isn't in the pot." if pol["exhibition_cant_pick"] else "",
        ("- Weeks run Tuesday to Monday around the Saturday; January and February games belong to the previous season."
         if f["weeks"]["jan_feb_prior_season"] else ""),
    ]
    return {k: [ln for ln in v if ln] for k, v in S.items()}


def _payout_words(rule: list) -> str:
    if rule[0] == "remainder":
        return "the remainder of the buy-in pot"
    if rule[0] == "buy_in_plus":
        return f"buy-in back + ${rule[1]}"
    return "buy-in back"


def _tb_words(item: str) -> str:
    t = re.sub(r"\s+—.*$", "", item)  # "Chips — the blackjack table..." -> "Chips"
    t = {"Chips": "chips from The Pit", "Win % last 8 games": "win % over the last 8 games"}.get(t, t)
    return t[0].lower() + t[1:] if not re.match(r"(Army|Navy|Celebration)", t) else t


def _ord(n: int) -> str:
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


_CRIT_WORDS = {
    "conf": "conference championship tiebreaker picks (the tiebreaker games before the playoffs)",
    "army_navy": "Army v Navy",
    "chips": "chips in The Pit (no Pit row counts as {chips:,})",
    "celebration": "the Celebration Bowl (the tiebreaker game between two MEAC/SWAC schools)",
    "last8": "win % over each team's last {n} decided picks",
}


def _tb_details(f: dict) -> list[str]:
    po = f.get("playoffs") or {}
    if not po.get("cascade"):
        return []
    steps = [_CRIT_WORDS.get(c, po.get("labels", {}).get(c, c)).format(chips=po.get("missing_chips") or 1000,
                                                                      n=po.get("last_n") or 8)
             for c in po["cascade"]]
    out = [f"- A tie for the last playoff spot is broken in that order: " + ", then ".join(steps) + "."]
    if po.get("pending_only_if_split"):
        out.append("- A tiebreaker game that isn't graded yet only holds things up if the tied teams picked it "
                   "differently; if they all picked the same side it can't break the tie and is skipped.")
    if po.get("coin_flip_held"):
        out.append("- Still tied after all of that: coin flip. The site never automates it; the commissioner sets "
                   "the seeds by hand.")
    out.append("- Playoff tiebreakers: Army-Navy counts only if it wasn't already used to set the playoff field.")
    return out


def _autopilot_words(po: dict) -> str:
    acts = set(po.get("admin_actions") or [])
    if not {"pause", "resume", "force", "undo"} <= acts:
        return ""
    return ("- Playoff autopilot controls (commissioner page): preview the field, pause or resume the autopilot, "
            "force the switch now (skips the waiting holds but never decides a coin flip), or undo (removes the "
            "season's seeds" + (" and pauses the autopilot" if po.get("undo_pauses") else "") + ").")


def section_text(name: str, lines: list[str]) -> str:
    if name == "TIEBREAKERS":
        return "\n".join(lines)
    head = {"LET THE SLOTS DECIDE": "LET THE SLOTS DECIDE (the jackpot)",
            "THE PIT": "THE PIT (blackjack, tiebreaker only)"}.get(name, name)
    return "\n".join([head] + lines)


def flagged_text(name: str, since: str) -> str:
    head = section_text(name, []).splitlines()[0] if name != "TIEBREAKERS" else "TIEBREAKERS"
    return (f"{head}\n- {FLAG}: the site's code for this part of the rules changed ({since}) and hasn't been "
            "re-checked yet. Don't state these rules from memory: say they may have just changed and to check "
            "the rules on the site or ask the commissioner.")


def page_hash(repo: Repo) -> str:
    return hashlib.sha256(rules_block(repo.need("cfb-genius.html")).encode()).hexdigest()[:16]


def generate_md(facts: dict, fps: dict[str, str], src_hash: str, flags: dict[str, str] | None = None,
                overrides: dict[str, str] | None = None) -> str:
    flags, overrides = flags or {}, overrides or {}
    secs = render_sections(facts)
    body = []
    for name in SECTIONS:
        if name in overrides:
            body.append(overrides[name].strip())
        elif name in flags:
            body.append(flagged_text(name, flags[name]))
        else:
            body.append(section_text(name, secs[name]))
    slim = {k: v for k, v in facts.items() if k not in ("rules_page_text", "pit_page")}
    head = [
        "# League rules for Picky. GENERATED by server/league_rules.py from the site's code: don't edit by hand.",
        "# Regenerate: cd server && ./.venv/bin/python3 -I league_rules.py --write  (test_rules.py fails when stale)",
        "# Lines starting with # are notes for maintainers and are stripped before Picky sees this.",
        f"# source-hash: {src_hash}  (sha256[:16] of the cfb-genius.html Rules block)",
        "# facts-json: " + json.dumps(slim, sort_keys=True, separators=(",", ":")),
    ] + [f"# fp {k} {v}" for k, v in sorted(fps.items())]
    if flags:
        head.append("# flagged: " + ", ".join(sorted(flags)))
    return "\n".join(head) + "\n\n" + "\n\n".join(body) + "\n"


def parse_md(text: str) -> tuple[dict, dict[str, str]]:
    facts, fps = {}, {}
    for ln in text.splitlines():
        if ln.startswith("# facts-json: "):
            facts = json.loads(ln[len("# facts-json: "):])
        elif ln.startswith("# fp "):
            _, _, k, v = ln.split(" ", 3)
            fps[k] = v
    return facts, fps


def strip_comments(text: str) -> str:
    return "\n".join(ln for ln in text.splitlines() if not ln.startswith("#")).strip()


# =============================================================================
# change detection
# =============================================================================

# Which sections each SQL object feeds. New objects are routed by keyword, so
# a function nobody has heard of yet ("auto_seed_playoffs") still flags the
# right section.
SQL_SECTIONS = {
    "picky_push": [], "picky_pull": [],  # Picky's fact-store mirror (public football facts), not a league rule
    "set_winner_on_final": ["FORMAT"], "half_point_line": ["FORMAT", "EDGE CASES"],
    "line_lock_at": ["FORMAT"], "sync_espn_lines": ["FORMAT"],
    "picks_open_for_game": ["DEADLINE AND PRIVACY", "STANDINGS AND PLAYOFFS"],
    "picks_only_on_the_slate": ["DEADLINE AND PRIVACY", "COMMISSIONER"],
    "calc_cfb_week": ["FORMAT", "EDGE CASES"], "is_commissioner": ["COMMISSIONER"],
    "slots_jackpot_odds": ["LET THE SLOTS DECIDE"], "slots_jackpot_odds_for": ["LET THE SLOTS DECIDE"],
    "resolve_slot_picks": ["LET THE SLOTS DECIDE"], "slots_jackpot_pull": ["LET THE SLOTS DECIDE"],
    "bj_deal": ["THE PIT"], "bj_settle_week": ["THE PIT"], "bj_double": ["THE PIT"], "bj_hit": ["THE PIT"],
    "bj_stand": ["THE PIT"], "bj_open_week": ["THE PIT"],
}
KEYWORD_SECTIONS = [
    (r"playoff|seed|bracket|standing|qualif|cut_?line|postseason", ["STANDINGS AND PLAYOFFS"]),
    (r"tiebreak", ["TIEBREAKERS"]),
    (r"payout|buy_?in|\bpot\b|prize", ["PAYOUTS"]),
    (r"winner|grade|cover|score_pick|push", ["FORMAT"]),
    (r"deadline|pick_lock|lock_picks|picks_open|reveal", ["DEADLINE AND PRIVACY"]),
    (r"jackpot|slots?_", ["LET THE SLOTS DECIDE"]),
    (r"^bj_|blackjack|chips", ["THE PIT"]),
    (r"commission", ["COMMISSIONER"]),
]
RULES_PAGE_SECTIONS = ["FORMAT", "DEADLINE AND PRIVACY", "STANDINGS AND PLAYOFFS", "PAYOUTS", "TIEBREAKERS", "THE PIT"]


def _h(s: str | None) -> str:
    return hashlib.sha256((s if s is not None else "<missing>").encode()).hexdigest()[:12]


def route(name: str) -> list[str]:
    if name in SQL_SECTIONS:
        return SQL_SECTIONS[name]
    out = []
    for rx, secs in KEYWORD_SECTIONS:
        if re.search(rx, name, re.I):
            out += [s for s in secs if s not in out]
    return out


def _slice(repo: Repo, rel: str, fn: str | None = None) -> str | None:
    s = repo.read(rel)
    if s is None or fn is None:
        return s
    try:
        return js_function(s, fn)
    except ExtractError:
        return None


def repo_sources(repo: Repo) -> dict[str, tuple[str | None, list[str]]]:
    """source key -> (content, sections). Narrow slices, so a style edit to
    index.html doesn't flag the scoring rules."""
    out: dict[str, tuple[str | None, list[str]]] = {}
    page = repo.read("cfb-genius.html")
    try:
        out["page:rules-block"] = (rules_block(page) if page else None, RULES_PAGE_SECTIONS)
    except ExtractError:
        out["page:rules-block"] = (None, RULES_PAGE_SECTIONS)
    out["page:tiebreakers.html"] = (repo.read("tiebreakers.html"), ["TIEBREAKERS"])
    idx = repo.read("index.html") or ""
    for fn, secs in (("computeSeasonWinsThroughWeek", ["FORMAT", "STANDINGS AND PLAYOFFS"]),
                     ("computePlayoffWins", ["STANDINGS AND PLAYOFFS"])):
        out[f"js:index.html#{fn}"] = (_slice(repo, "index.html", fn), secs)
    # The board loaders are mostly UI; only their week cut-offs are rules.
    cut = sorted({ln.strip() for ln in idx.splitlines()
                  if re.search(r"week\)?\s*(<=|>=|<|>)\s*\d+|\.(gt|gte|lt|lte)\('week'", ln)})
    out["js:index.html#week-cutoffs"] = ("\n".join(cut) or None, ["STANDINGS AND PLAYOFFS", "TIEBREAKERS"])
    m = re.search(r"const pointsArray = .*?const shouldInsertDivider[^;]*;", idx, re.S)
    out["js:index.html#cut-line"] = (m.group(0) if m else None, ["STANDINGS AND PLAYOFFS"])
    m = re.search(r"\(function renderRace\(\)\{.{0,400}", repo.read("stats.html") or "", re.S)
    out["js:stats.html#renderRace"] = (m.group(0) if m else None, ["STANDINGS AND PLAYOFFS"])
    for rel, secs in (("js/kickoff.js", ["DEADLINE AND PRIVACY"]), ("js/coverOdds.js", ["FORMAT"]),
                      ("js/seasonHistory.js", ["FORMAT"]), ("js/seasonRace.js", ["STANDINGS AND PLAYOFFS"]),
                      ("js/playoffOdds.js", ["STANDINGS AND PLAYOFFS"]), ("js/pickSave.js", ["DEADLINE AND PRIVACY"])):
        out[f"js:{rel}"] = (repo.read(rel), secs)
    # playoff mode = seed rows (the rest of playoffState.js is theme/banner UI)
    out["js:js/playoffState.js#fetchState"] = (_slice(repo, "js/playoffState.js", "fetchState"), ["STANDINGS AND PLAYOFFS"])
    m = re.search(r".{0,300}Only the four playoff teams.{0,200}", repo.read("picks.html") or "", re.S)
    out["js:picks.html#playoff-field"] = (m.group(0) if m else None, ["STANDINGS AND PLAYOFFS"])
    com = repo.read("commissioner.html") or ""
    m = re.search(r"const seeds = \[[^\]]*\];", com)
    out["js:commissioner.html#seeds"] = (m.group(0) if m else None, ["STANDINGS AND PLAYOFFS", "COMMISSIONER"])
    # Any other site JS whose name says it's about rules (a new playoff module, say).
    known = {"js/playoffOdds.js", "js/seasonRace.js", "js/playoffState.js", "js/playoffAutopilot.js"}
    for rel in repo.ls("js"):
        nm = Path(rel).stem
        if rel.endswith(".js") and rel not in known and (secs := [s for rx, ss in KEYWORD_SECTIONS[:4]
                                                                   if re.search(rx, nm, re.I) for s in ss]):
            out[f"js:{rel}"] = (repo.read(rel), secs)
    defs = sql_functions(repo)
    for name, body in sorted(defs.items()):
        if secs := route(name):
            out[f"sql:{name}"] = (body, secs)
    pols = sql_policies(repo)
    for table in ("picks", "all_games", "playoffs", "teams"):
        txt = "\n".join(f"{k} {v['cmd']} {v['restrictive']} {v['text']}" for k, v in sorted(pols.items()) if v["table"] == table)
        out[f"sql:policies:{table}"] = (txt, ["COMMISSIONER", "DEADLINE AND PRIVACY"])
    # New tables/columns with rules-ish names (playoff_state, auto_playoff...).
    ddl = []
    for rel in repo.ls("supabase/migrations"):
        for m in re.finditer(r"(create\s+table(?:\s+if\s+not\s+exists)?|alter\s+table)\s+(?:public\.)?(\w+)", repo.read(rel) or "", re.I):
            if route(m.group(2)) and m.group(2) not in ("picks", "all_games", "teams"):
                ddl.append(f"{Path(rel).name}:{m.group(2)}")
    for sec in {s for d in ddl for s in route(d.split(":")[1])}:
        out[f"sql:tables:{sec.replace(' ', '_')}"] = ("\n".join(d for d in ddl if sec in route(d.split(":")[1])), [sec])
    for rel in repo.ls("supabase/functions"):
        nm = Path(rel).name
        if secs := (route(nm) or (["FORMAT"] if nm == "update-final-scores" else [])):
            out[f"edge:{nm}"] = (repo.read(f"{rel}/index.ts"), secs)
    return out


def fingerprints(sources: dict[str, tuple[str | None, list[str]]]) -> dict[str, str]:
    return {k: _h(v[0]) for k, v in sources.items()}


# ---- live: Supabase (read-only) and the deployed rules page -----------------

def _token() -> str | None:
    try:
        return subprocess.check_output(["security", "find-generic-password", "-a", "drake", "-s",
                                        "CFB_PICKEM__SUPABASE_ACCESS_TOKEN", "-w"], text=True,
                                       stderr=subprocess.DEVNULL, timeout=10).strip() or None
    except Exception:
        return None


def live_query(sql: str, timeout: float = 30) -> list[dict]:
    """One read-only SELECT via the Management API. Never DDL: refuses
    anything that isn't a plain SELECT, and asks the API for read_only."""
    if not re.match(r"\s*(select|with)\b", sql, re.I) or re.search(r";\s*\S", sql):
        raise ValueError("live_query is SELECT-only")
    import httpx
    tok = _token()
    if not tok:
        raise RuntimeError("no Supabase access token")
    r = httpx.post(f"https://api.supabase.com/v1/projects/{SUPABASE_REF}/database/query",
                   headers={"Authorization": f"Bearer {tok}"}, json={"query": sql, "read_only": True}, timeout=timeout)
    r.raise_for_status()
    return r.json()


def live_db() -> tuple[dict[str, str], dict[str, dict]]:
    """(function defs, policies) as the database has them right now."""
    fns = live_query("select p.proname as name, pg_get_functiondef(p.oid) as def from pg_proc p "
                     "join pg_namespace n on n.oid = p.pronamespace where n.nspname = 'public' and p.prokind = 'f'")
    pols = live_query("select policyname, tablename, cmd, permissive, coalesce(qual, '') as qual, "
                      "coalesce(with_check, '') as wc from pg_policies where schemaname = 'public'")
    defs = {r["name"].lower(): r["def"] for r in fns}
    policies = {r["policyname"]: {"table": r["tablename"], "cmd": r["cmd"], "restrictive": r["permissive"] == "RESTRICTIVE",
                                  "text": f"using ({r['qual']}) with check ({r['wc']})"} for r in pols}
    return defs, policies


def live_sources(defs: dict[str, str], policies: dict[str, dict]) -> dict[str, tuple[str | None, list[str]]]:
    out = {f"live:rpc:{n}": (d, route(n)) for n, d in sorted(defs.items()) if route(n)}
    for table in ("picks", "all_games", "playoffs", "teams"):
        txt = "\n".join(f"{k} {v['cmd']} {v['restrictive']} {v['text']}" for k, v in sorted(policies.items()) if v["table"] == table)
        out[f"live:policies:{table}"] = (txt, ["COMMISSIONER", "DEADLINE AND PRIVACY"])
    return out


def live_page() -> str | None:
    import httpx
    r = httpx.get(SITE_PAGE_URL, timeout=15, follow_redirects=True)
    r.raise_for_status()
    return rules_block(r.text)


# =============================================================================
# verification
# =============================================================================

CONFORMANCE_JS = SERVER / "rules_conformance.mjs"
JS_FILES = ["js/kickoff.js", "js/coverOdds.js", "js/utils.js", "js/seasonHistory.js", "js/leagueHistory.js"]


def conformance_fixtures(facts: dict) -> dict:
    """Inputs for the site's JS, and what the Python rules say about each."""
    covers = [(31, 17, -7.5), (24, 17, -7.5), (20, 21, 3.5), (21, 21, -0.5), (10, 13, 2.5), (35, 0, -35.5), (14, 14, 0.5)]
    picks = [("Utah", "Utah", False), ("Iowa", "Utah", False), (None, "Utah", True), ("Iowa", "Utah", True),
             ("Iowa", None, True), (None, None, False)]
    t0 = dt.datetime(2026, 11, 14, 11, 0)
    weeks = [
        ([{"start": "2026-11-14T11:00:00"}, {"start": "2026-11-14T18:30:00"}], dt.datetime(2026, 11, 14, 10, 59)),
        ([{"start": "2026-11-14T11:00:00"}, {"start": "2026-11-14T18:30:00"}], t0),
        ([{"start": "2026-11-13T19:00:00"}, {"start": "2026-11-14T18:30:00"}], dt.datetime(2026, 11, 13, 19, 1)),
        ([{"start": None}, {"start": "2026-11-14T18:30:00"}], dt.datetime(2026, 11, 14, 12, 0)),
        ([{"start": None, "status": "final"}], dt.datetime(2026, 11, 1, 0, 0)),
        ([{"start": "2026-11-14T11:00:00", "winner": "Utah"}], dt.datetime(2026, 11, 1, 0, 0)),
    ]
    return {
        "covers": [{"home": h, "away": a, "line": ln, "py_margin": cover_margin(h, a, ln),
                    "py_winner": cover_winner("Home", "Away", h, a, ln)} for h, a, ln in covers],
        "score_cond": facts.get("score_cond"),
        "picks": [{"pick": p, "winner": w, "jackpot": jp, "py": is_point(p, w, jp)} for p, w, jp in picks],
        "weeks": [{"games": g, "now": now.isoformat(), "py": week_locked(g, now)} for g, now in weeks],
    }


def run_conformance(repo: Repo, facts: dict) -> list[str]:
    """Run the site's own JS on fixtures and compare with the Python rules.
    Returns mismatch messages ('node missing' counts as one)."""
    node = next((p for p in ("/opt/homebrew/bin/node", "/usr/local/bin/node", "/usr/bin/node") if Path(p).exists()), None)
    if not node:
        return ["node not installed: conformance not run"]
    fx = conformance_fixtures(facts)
    with tempfile.TemporaryDirectory() as td:
        for rel in JS_FILES:
            src = repo.read(rel)
            if src is None:
                return [f"{rel} missing"]
            (Path(td) / rel).parent.mkdir(parents=True, exist_ok=True)
            (Path(td) / rel).write_text(src)
        p = subprocess.run([node, str(CONFORMANCE_JS), td], input=json.dumps(fx), capture_output=True,
                           text=True, timeout=120)
    if p.returncode != 0:
        return [f"conformance script failed: {(p.stderr or p.stdout)[-300:]}"]
    return json.loads(p.stdout).get("mismatches", [])


def check_sql_semantics(facts: dict) -> list[str]:
    """The cover rule text from set_winner_on_final, evaluated, vs cover_winner."""
    bad = []
    for h, a, ln in [(31, 17, -7.5), (24, 17, -7.5), (20, 21, 3.5), (21, 21, 0.0), (10, 13, 2.5)]:
        sql_home = eval_cover_cond(facts["cover"]["cond"], h, a, ln)
        sql = facts["cover"]["true"] if sql_home else facts["cover"]["else"]
        py = cover_winner("Home", "Away", h, a, ln)
        if sql != py:
            bad.append(f"cover {h}-{a} line {ln}: SQL {sql}, Python {py}")
    return bad


def live_sql_checks(facts: dict) -> list[str]:
    """Pure SQL functions run in the live database (SELECT only) vs Python."""
    days = [dt.datetime(2026, 11, d, 11, 0) for d in (10, 13, 14, 15, 16)] + [dt.datetime(2026, 8, 29, 11), dt.datetime(2027, 1, 9, 19)]
    vals = ", ".join(f"('{d.isoformat(sep=' ')}'::timestamp)" for d in days)
    rows = live_query(f"select s::text as s, public.line_lock_at(s)::text as lock, public.calc_cfb_week(s) as wk "
                      f"from (values {vals}) v(s) order by s")
    bad = []
    for r in rows:
        d = dt.datetime.fromisoformat(r["s"])
        if dt.datetime.fromisoformat(r["lock"]) != line_lock_at(d, facts):
            bad.append(f"line_lock_at({d}): db {r['lock']}, python {line_lock_at(d, facts)}")
        if int(r["wk"]) != calc_cfb_week(d):
            bad.append(f"calc_cfb_week({d}): db {r['wk']}, python {calc_cfb_week(d)}")
    rows = live_query("select n, public.slots_jackpot_odds(n)::float8 as o from (values (0),(150),(200),(201),(260),(400),(1000)) v(n)")
    for r in rows:
        if abs(float(r["o"]) - jackpot_odds(int(r["n"]), facts)) > 1e-9:
            bad.append(f"slots_jackpot_odds({r['n']}): db {r['o']}, python {jackpot_odds(int(r['n']), facts)}")
    return bad


OPUS_SCHEMA = {
    "type": "object", "required": ["sections"],
    "properties": {"sections": {"type": "object", "additionalProperties": {
        "type": "object", "required": ["verdict"],
        "properties": {"verdict": {"enum": ["ok", "corrected", "unsure"]},
                       "text": {"type": "string"}, "why": {"type": "string"}}}},
        "notes": {"type": "string"}},
}


def opus_verify(sections: list[str], generated: dict[str, str], sources: dict[str, tuple[str | None, list[str]]],
                changed: list[str]) -> dict:
    """Offline Opus review of the regenerated sections against the code they
    come from. Raises when Anthropic is off or the run fails."""
    import picky_claude
    picky_claude.guard()
    parts = []
    for k, (content, secs) in sources.items():
        if any(s in sections for s in secs) and content:
            parts.append(f"=== {k}{'  (CHANGED)' if k in changed else ''}\n{content[:12000]}")
    prompt = (
        "You verify the written league rules for a college football pick'em site against the site's actual "
        "code. Below: (1) rules sections generated from the code, (2) the code they come from; sources marked "
        "CHANGED changed since the rules were last verified. For EACH section listed, answer: 'ok' if every "
        "line is true to the code and nothing rules-relevant in the code is missing; 'corrected' with the full "
        "corrected section text (same header line, same '- ' bullet style, plain words a league member "
        "understands, only facts the code or page states, no em dashes) if anything is wrong, stale or missing; "
        "'unsure' if the code doesn't settle it. Keep the TIEBREAKERS layout exactly ('Regular season "
        "tiebreakers, in order:' / 'Playoff tiebreakers, in order:' then '1) ...').\n\n"
        "Key the 'sections' object by these exact names: " + ", ".join(sections) + ".\n\n"
        "SECTIONS TO VERIFY:\n\n" + "\n\n".join(generated[s] for s in sections) +
        "\n\nCODE:\n\n" + "\n\n".join(parts)[:150000])
    out = _run_claude(prompt, OPUS_SCHEMA)
    # "THE PIT (blackjack, tiebreaker only)" -> "THE PIT": key by section name
    secs = {}
    for k, v in (out.get("sections") or {}).items():
        name = next((n for n in sorted(SECTIONS, key=len, reverse=True) if str(k).strip().upper().startswith(n)), k)
        secs[name] = v
    return dict(out, sections=secs)


def _run_claude(prompt: str, schema: dict, timeout: int = 900) -> dict:
    import picky_claude  # Opus, guard(), empty MCP, no API key, daily usage log
    work = CACHE / "rules-claude"
    work.mkdir(parents=True, exist_ok=True)
    p = picky_claude.run(prompt, timeout=timeout, caller="league_rules", cwd=work, tools="", schema=schema)
    if p.returncode != 0:
        raise RuntimeError(f"claude exit {p.returncode}: {(p.stderr or p.stdout)[-300:]}")
    out = json.loads(p.stdout)
    if out.get("is_error") or not isinstance(out.get("structured_output"), dict):
        raise RuntimeError(f"claude result not usable: {str(out.get('result'))[:300]}")
    return out["structured_output"]


# =============================================================================
# the tick
# =============================================================================

def _load_state() -> dict:
    try:
        return json.loads(state_path().read_text())
    except (OSError, ValueError):
        return {}


def _save_state(st: dict) -> None:
    p = state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=1, sort_keys=True))
    tmp.replace(p)


def _write_live(text: str) -> None:
    p = live_md_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(text)
    tmp.replace(p)


def _bare(text: str) -> str:
    """A section without its 'RULES CHANGED, VERIFY' flag line, whitespace-normalized (for comparisons)."""
    return re.sub(r"\s+", " ", "\n".join(ln for ln in (text or "").splitlines() if FLAG not in ln)).strip()


def check(repo: Repo | None = None, use_live: bool = True, use_opus: bool | None = None,
          now: dt.datetime | None = None, verifier=None) -> dict:
    """Detect rules changes, regenerate, re-verify, write the live digest.
    Returns a report dict (also saved in the state file).

    use_opus None = picky_claude.available(): never on the hourly schedule (zero maintenance, phase 8).
    Without an Opus verdict, the code is the verifier: a changed section that extracted cleanly, passed the
    deterministic checks and whose regenerated text CHANGED with the code is stated as regenerated; a change
    the generator doesn't see (same text out) stays flagged so Picky hedges."""
    if use_opus is None:
        import picky_claude
        use_opus = picky_claude.available()
    repo = repo or Repo()
    now = now or dt.datetime.now()
    stamp = now.strftime("%Y-%m-%d %H:%M")
    st = _load_state()
    committed_text = MD.read_text() if MD.exists() else ""
    committed_facts, committed_fps = parse_md(committed_text) if committed_text else ({}, {})
    verified: dict[str, str] = {**committed_fps, **st.get("verified", {})}
    sources = repo_sources(repo)
    live_defs = live_pols = None
    live_errs: list[str] = []
    if use_live:
        try:
            live_defs, live_pols = live_db()
            sources.update(live_sources(live_defs, live_pols))
        except Exception as e:  # network/token: repo-only this tick, nothing flagged for it
            live_errs.append(f"live db unavailable: {type(e).__name__}")
        try:
            sources["live:page:rules-block"] = (live_page(), RULES_PAGE_SECTIONS)
        except Exception as e:
            live_errs.append(f"live page unavailable: {type(e).__name__}")
    fps = fingerprints(sources)
    # Live sources are first seen without a baseline: adopt them (the
    # deterministic live checks below still have to pass).
    for k, v in fps.items():
        if k.startswith("live:") and k not in verified:
            verified[k] = v
    changed = sorted(k for k, v in fps.items() if verified.get(k) != v)
    gone = sorted(k for k in verified if k not in fps and not k.startswith("live:"))
    changed += gone
    pending = {s for k in changed for s in (sources.get(k, (None, route(k.split(":")[-1]) or SECTIONS))[1])}

    facts, errors = extract(repo)
    if live_defs:
        try:
            lf, lerr = extract(repo, live_defs, live_pols)
            for key in ("cover", "line_lock", "last_regular_week", "pick_lock", "jackpot", "pit", "policies"):
                if lf.get(key) != facts.get(key):
                    sec = {"cover": "FORMAT", "line_lock": "FORMAT", "last_regular_week": "STANDINGS AND PLAYOFFS",
                           "pick_lock": "DEADLINE AND PRIVACY", "jackpot": "LET THE SLOTS DECIDE", "pit": "THE PIT",
                           "policies": "COMMISSIONER"}[key]
                    errors.setdefault(sec, f"live database differs from the repo migrations on {key}")
            facts = {**facts, **{k: lf[k] for k in ("cover", "line_lock", "last_regular_week", "pick_lock",
                                                    "jackpot", "pit", "policies") if k in lf}}
            live_errs += [f"live checks: {m}" for m in live_sql_checks(facts)]
        except Exception as e:
            live_errs.append(f"live extract failed: {type(e).__name__}: {e}")
    det = []
    if "cover" in facts:
        det += check_sql_semantics(facts)
    if not errors:
        det += [m for m in run_conformance(repo, facts) if "not installed" not in m]
    for m in det:
        errors.setdefault("FORMAT" if "cover" in m or "score" in m else "STANDINGS AND PLAYOFFS"
                          if "standing" in m else "DEADLINE AND PRIVACY", m)
    for m in live_errs:
        if m.startswith("live checks:"):
            errors.setdefault("FORMAT" if "line_lock" in m or "calc_cfb" in m else "LET THE SLOTS DECIDE", m)
    # Overrides from an earlier Opus correction live while their sources stand.
    overrides = {s: o["text"] for s, o in (st.get("overrides") or {}).items()
                 if all(fps.get(k) == h for k, h in o.get("for", {}).items())}
    if not errors and facts.get("cover"):
        generated = {s: section_text(s, ln) for s, ln in render_sections(facts).items()}
    else:
        generated = {}
    to_verify = sorted((pending | set(errors)) - set(overrides))
    flags: dict[str, str] = {}
    opus_note = ""
    result = None
    if to_verify:
        if use_opus and generated:
            try:
                result = (verifier or opus_verify)(to_verify, generated, sources, changed)
            except Exception as e:
                opus_note = f"opus unavailable: {type(e).__name__}: {str(e)[:200]}"
        for s in to_verify:
            v = ((result or {}).get("sections") or {}).get(s) or {}
            if s in errors and v.get("verdict") != "corrected":
                flags[s] = f"{stamp}: {errors[s]}"
            elif v.get("verdict") == "ok" and s not in errors:
                pass
            elif result is None and s in generated and s not in errors and \
                    _bare(generated[s]) != _bare(st.get("stated", {}).get(s) or section(s, committed_text)):
                pass  # code-verified: the rule text moved with the code it's generated from
            elif v.get("verdict") == "corrected" and (v.get("text") or "").strip():
                txt = v["text"].strip()
                head = section_text(s, []).splitlines()[0] if s != "TIEBREAKERS" else "TIEBREAKERS"
                if not txt.startswith(head.split(" (")[0]):
                    txt = head + "\n" + txt
                overrides[s] = txt
                st.setdefault("overrides", {})[s] = {"text": txt, "at": stamp, "why": v.get("why", ""),
                                                     "for": {k: fps[k] for k, (_, secs) in sources.items() if s in secs}}
            else:
                flags[s] = f"changed {stamp}" + (f"; {opus_note}" if opus_note else "")
    # Verified = every source whose sections are all clean now.
    bad_secs = set(flags)
    for k, v in fps.items():
        if not set(sources[k][1]) & bad_secs:
            verified[k] = v
    for k in gone:
        if not (set(route(k.split(":")[-1])) & bad_secs):
            verified.pop(k, None)
    if generated or committed_facts:
        use = facts if generated else committed_facts
        try:
            text = generate_md(use, fps, page_hash(repo), flags, overrides)
        except Exception:
            text = generate_md(committed_facts, committed_fps, "", {s: f"changed {stamp}" for s in SECTIONS})
        _write_live(text)
    verdicts = {s: {"verdict": v.get("verdict"), "why": (v.get("why") or "")[:300]}
                for s, v in (((result or {}).get("sections") or {}) if to_verify else {}).items() if isinstance(v, dict)}
    report = {"at": stamp, "changed": changed, "pending": sorted(pending), "errors": errors, "flagged": flags,
              "overrides": sorted(overrides), "live": live_errs, "opus": opus_note or ("ran" if to_verify and use_opus else ""),
              "opus_verdicts": verdicts}
    for s_ in SECTIONS:  # the last text stated per section: the next code-only check compares against it
        if s_ in generated and s_ not in flags:
            st.setdefault("stated", {})[s_] = overrides.get(s_) or generated[s_]
    st.update({"verified": verified, "last": report})
    st["overrides"] = {s: o for s, o in (st.get("overrides") or {}).items() if s in overrides}
    _save_state(st)
    return report


# =============================================================================
# runtime: what Picky reads
# =============================================================================

def _read_rules_file() -> str:
    """The live copy (written by --check) when it is at least as new as the
    committed digest; a freshly deployed league_rules.md wins over an older
    live copy until the next check rewrites it."""
    lp = live_md_path()
    try:
        if lp.exists() and (not MD.exists() or lp.stat().st_mtime >= MD.stat().st_mtime):
            return lp.read_text()
    except OSError:
        pass
    return MD.read_text() if MD.exists() else ""


_cache: dict = {"key": None, "text": "", "raw": "", "facts": {}}


def current_raw() -> str:
    lp = live_md_path()
    key = (lp.stat().st_mtime if lp.exists() else None, MD.stat().st_mtime if MD.exists() else None, str(lp))
    if _cache["key"] != key:
        raw = _read_rules_file()
        facts, _ = parse_md(raw)
        if not facts and MD.exists():
            facts, _ = parse_md(MD.read_text())
        _cache.update(key=key, raw=raw, text=strip_comments(raw), facts=facts)
    return _cache["raw"]


def current_text() -> str:
    """The digest Picky reads: the live (re-verified/flagged) copy when the
    rules watcher has written one, else the committed league_rules.md."""
    current_raw()
    return _cache["text"]


def current_facts() -> dict:
    current_raw()
    return _cache["facts"]


def flagged_sections() -> list[str]:
    return [s for s in SECTIONS if re.search(rf"^{re.escape(s)}[^\n]*\n- {FLAG}", current_text(), re.M)]


def section(name: str, text: str | None = None) -> str:
    """One section of the current digest (or of `text`), header line included."""
    blocks, cur = {}, None
    for ln in (current_text() if text is None else text).splitlines():
        key = next((k for k in SECTIONS if ln.startswith(k)), None)
        if key:
            cur = key
            blocks[cur] = [ln]
        elif cur:
            blocks[cur].append(ln)
    return "\n".join(blocks.get(name, [])).strip()


def _load_facts() -> dict:
    return current_facts() or {}


FACTS: dict = _load_facts()
BUY_IN: int = FACTS.get("hand", HAND)["buy_in"]
REGULAR_SEASON_LAST_WEEK: int = FACTS.get("last_regular_week", 15)
FIRST_WEEK: int = FACTS.get("first_week", 0)
PLAYOFF_SPOTS: int = FACTS.get("playoff_spots", 4)
GAMES_PER_WEEK: int = FACTS.get("games_per_week", 4)


# =============================================================================
# answers: typical rules questions, answered from the facts in code. chat.py
# puts the answer in front of the model ("give this"); a section the watcher
# flagged gets a hedge instead, never the stale rule.
# =============================================================================

RULES_PAGE = "the League Rules on the site (cfb-genius page)"


def hedge(topic: str) -> str:
    return (f"RULES CHANGED, VERIFY: the site's code for {topic} changed recently and hasn't been re-checked. "
            f"Don't state that rule; say it may have just changed and to check {RULES_PAGE} or ask the commissioner.")


def _a_push(f, q: str = ""):
    c = f["cover"]
    tail = (f" If a final ever landed exactly on the number, the grading gives it to the {c['else'].lower()} team."
            if c.get("else") in ("Away", "Home") else "")
    out = ("No pushes in this league: every line is a half point (the site moves whole numbers half a point), "
           "so a game can't land on the number.")
    m = re.search(r"(?<![\d.])[-+]?(\d{1,2})(?![\d.])(?=[^?]*\b(exactly|push|tie|by \d))", q or "")
    hp = f.get("half_point") or {}
    if m and int(m.group(1)) > 0 and hp.get("keeps_within") == 0.5:
        n = int(m.group(1))
        way = "either way at random" if hp.get("whole_moves_random") else "half a point"
        return (out + f" A {n}-point line gets moved {way}: at -{n - 0.5:g} a {n}-point win covers, at -{n + 0.5:g} "
                f"it doesn't, so check which number the league posted.")
    return out + tail


def _a_line_lock(f):
    ll = f["line_lock"]
    day = DAYS[(5 - ll["days_before_saturday"]) % 7]
    return (f"Lines lock {day} {_ampm(ll['hour'], ll['minute'])} CT{_eastern(ll)}. Whatever the line says then is "
            "what the week is scored against; a game with no line yet keeps the first one posted.")


def _a_pick_lock(f):
    pl = f["pick_lock"]
    return ("Regular season: every pick for the week locks at once, at the scheduled kickoff of the week's first game"
            + (" (tiebreaker games count)" if pl.get("tiebreakers_lock_week") else "")
            + ". A weather or TV delay doesn't buy more time. "
            + ("Playoffs: each game locks at its own kickoff. " if pl.get("playoff_per_game") else "")
            + "Everyone's picks stay hidden until they lock. After the lock only the commissioner can change a pick.")


def _a_playoff_pick(f):
    po, spots = f.get("playoffs") or {}, f["playoff_spots"]
    if not po.get("field_only_once_seeded"):
        return None
    return (f"Only if your team made the playoffs. Once the {spots} seeds are set, only those {spots} teams can pick "
            f"playoff games (week {po['field_only_after_week'] + 1} on, tiebreakers too); the site and the database "
            "both block everyone else."
            + (" Everyone else can still watch every game and see every pick once it locks." if po.get("eliminated_view_only") else "")
            + " Before the seeds exist, anyone can still pick week 16+ games.")


def _a_who_makes(f):
    po, spots, last = f.get("playoffs") or {}, f["playoff_spots"], f["last_regular_week"]
    s = (f"The top {spots} in regular-season points (weeks {f['first_week']}-{last}) make the league playoffs. "
         f"A tie for {_ord(spots)} goes to the tiebreakers: "
         + ", ".join(_tb_words(x) for x in f["tiebreakers"]["regular"]) + ". ")
    if po.get("settle_hours"):
        s += (f"The site flips to the playoffs on its own about {po['settle_hours']} hours after week {last}'s last "
              "kickoff once every game is graded (a coin flip waits for the commissioner). ")
    return s + f"Then scores reset and only playoff games count."


def _a_tiebreakers(f, q=""):
    tb = f["tiebreakers"]
    reg = ", ".join(f"{i}) {_tb_words(x)}" for i, x in enumerate(tb["regular"], 1))
    po = ", ".join(f"{i}) {_tb_words(x)}" for i, x in enumerate(tb["playoff"], 1))
    if re.search(r"\bplayoffs?\b", q, re.I) and not re.search(r"\b(make|spot|cut|field|get in|qualif)", q, re.I):
        return f"Playoff ties: {po}. Army-Navy only counts if it wasn't already used to set the field."
    s = f"Regular season, in order: {reg}. Tiebreaker picks never count as points."
    if (f.get("playoffs") or {}).get("pending_only_if_split"):
        s += " An ungraded tiebreaker only matters if the tied teams picked it differently."
    return s + f" Playoffs: {po}."


def _a_scoring(f):
    return (f"{f['games_per_week']} games a week against the spread, 1 point per pick that covers. No pick, no point; "
            "no bonuses or confidence weights. Tiebreaker games never count. A slots AUTO WIN is a point no matter who covers.")


def _a_payouts(f, n_teams=None):
    words = " ".join(f"{p}: {_payout_words(r)}." for p, r in f["payouts"].items())
    s = f"Buy-in is ${f['hand']['buy_in']} a player. {words}"
    if n_teams:
        m = payout_math(n_teams, f)
        s += (f" With {n_teams} paying teams: ${m['pot']:,} pot, 1st ${m['first']:,}, 2nd ${m['second']:,}, "
              f"3rd ${m['third']:,}.")
    return s + " Picky never pays in or gets paid."


def _a_missed(f):
    return ("Missed the lock = no pick and no point for that game. No late picks, no default pick; only the "
            "commissioner can enter one after the lock.")


def _a_uncovered(f):
    return "The written rules don't cover postponed, canceled or suspended games. That's the commissioner's call."


def _a_commish(f):
    return section("COMMISSIONER").split("\n", 1)[-1].replace("- ", "").replace("\n", " ") or None


# (topic, question pattern, section it comes from, answer). First match wins,
# so the narrow ones go first ("when do the lines lock" isn't the pick lock).
ANSWERS = [
    ("pushes", r"\bpush(es|ed)?\b|lands? (exactly )?on the (number|line)|\btie (against|vs\.?) the spread", "FORMAT", _a_push),
    ("line lock", r"\blines? (lock|locks|locked|get locked|set|freeze)\b|\bwhen (do|does) (the )?(lines?|spreads?) "
                  r"(lock|get set|freeze)|\bline lock", "FORMAT", _a_line_lock),
    ("postponed games", r"postpone|cancel+ed|suspend", "EDGE CASES", _a_uncovered),
    ("missed picks", r"\b(forgot|forget|miss(ed)?)\b.{0,15}\b(pick|deadline|lock|week)|didn'?t (make|get) (my|a) pick", "EDGE CASES", _a_missed),
    ("who can pick in the playoffs",
     r"\b(can|could|do|does|am|are|who)\b.{0,40}\bpick\b.{0,40}\bplayoffs?\b|\bplayoffs? (games?|picks?)\b.{0,40}"
     r"\b(pick|eliminated|everyone|anyone|non.?playoff|still)\b|\beliminated\b.{0,40}\bpick", "STANDINGS AND PLAYOFFS", _a_playoff_pick),
    ("pick deadlines", r"\b(picks?|deadline)\b.{0,30}\block|\block\w*\b.{0,20}\bpicks?\b|\bdeadline\b|picks? due|"
                       r"how late can i", "DEADLINE AND PRIVACY", _a_pick_lock),
    ("tiebreakers", r"tie ?-?break|\bties?\b.{0,50}\b(broken|decided|settled)|break(s|ing)? (a |the )?ties?\b|\btied\b|"
                    r"\bwho wins (a|the) tie", "TIEBREAKERS", _a_tiebreakers),
    ("the playoff cut", r"\bwho (makes|gets in|gets into|qualifies|is in)\b.{0,20}\bplayoffs?|\bmake (the )?playoffs?\b|"
                        r"\bhow (do|does) (you|i|we|a team|someone) (make|get into|qualify)|\bplayoff (format|cut|spots?)\b|"
                        r"\bcut ?line\b|how many (teams )?make|how do (the )?(league )?playoffs work", "STANDINGS AND PLAYOFFS", _a_who_makes),
    ("payouts", r"\bpayouts?\b|buy.?in|\bpot\b|\b(first|second|third|1st|2nd|3rd)( place)? (wins?|gets?|pays?)\b|"
                r"\bwhat do(es)? (the )?(winner|first|second|third)", "PAYOUTS", _a_payouts),
    ("scoring", r"\bhow (does|do) (the )?scoring|\bhow (are|do) (we|you|picks) (score|scored|graded)|\bpoints? (per|for (a|each)) "
                r"(pick|win|cover)|\bhow many points\b", "FORMAT", _a_scoring),
    ("commissioner powers", r"\bcommish|\bcommissioner\b.{0,30}\b(can|power|do|allowed|change|override)", "COMMISSIONER", _a_commish),
]


def answer(question: str, n_teams: int | None = None, facts: dict | None = None,
           flagged: list[str] | None = None) -> str | None:
    """A short, computed answer to a typical rules question, or None. A topic
    whose section the watcher flagged gets hedge() instead of the rule."""
    q = question or ""
    f = facts or current_facts()
    fl = flagged_sections() if flagged is None else flagged
    for topic, rx, sec, fn in ANSWERS:
        if not re.search(rx, q, re.I):
            continue
        if sec in fl:
            return hedge(topic)
        try:
            out = (fn(f, q) if fn in (_a_tiebreakers, _a_push) else fn(f, n_teams) if fn is _a_payouts else fn(f))
        except (KeyError, TypeError, IndexError):
            return None  # facts missing: the digest still rides along
        return out or None
    return None


def write(repo: Repo | None = None) -> str:
    repo = repo or Repo()
    facts, errors = extract(repo)
    if errors:
        raise SystemExit("can't generate, extraction failed:\n" + "\n".join(f"  {k}: {v}" for k, v in errors.items()))
    det = check_sql_semantics(facts) + run_conformance(repo, facts)
    if det:
        raise SystemExit("can't generate, the site's logic disagrees with league_rules.py:\n  " + "\n  ".join(det))
    text = generate_md(facts, fingerprints(repo_sources(repo)), page_hash(repo))
    MD.write_text(text)
    return text


if __name__ == "__main__":
    a = sys.argv[1:]
    if "--write" in a:
        write()
        print(f"wrote {MD}")
    elif "--check" in a:
        ref = a[a.index("--ref") + 1] if "--ref" in a else None
        if ref and "--no-fetch" not in a:
            subprocess.run(["git", "-C", str(ROOT), "fetch", "--quiet", "origin", "main"], timeout=60,
                           capture_output=True)
        rp = Repo(ref=ref) if ref and subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--verify", "-q", ref],
                                                     capture_output=True).returncode == 0 else Repo()
        rep = check(rp, use_live="--no-live" not in a, use_opus="--no-opus" not in a)
        print(json.dumps(rep, indent=1))
    elif "--facts" in a:
        print(json.dumps(extract()[0], indent=1, sort_keys=True))
    else:
        print(__doc__)
