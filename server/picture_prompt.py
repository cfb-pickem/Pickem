"""Picture prompt expander (2026-11-18). Runs in code, no model, before the image service.

The image model got the member's raw words, so "notre dame winning the national title" drew a man in front of
the Paris cathedral in a jersey of garbled letters. Here:
  - teams the router's resolver finds (factstore.teams_in) become an explicit picture: "the Notre Dame Fighting
    Irish college football team in navy blue and metallic gold uniforms";
  - a short phrase table turns common CFB scenes (national title, beating X, losing, tailgate, mascot) into one;
  - a style suffix always rides along: no words, no lettering, no logos (the model can't spell, and logos are
    trademarks); sports prompts also get photoreal sports photography + "jerseys show only a number".
Privacy is unchanged: only the member's words plus public team data (ESPN's free teams endpoint: mascot name,
color, alternateColor). Colors come from cache/team_looks.json (refreshed on the fact store tick, 30-day TTL),
else the committed team_looks.json snapshot, so it works with no network.

  picture_prompt.py "notre dame winning the national title"   # print the expanded prompt
  picture_prompt.py snapshot                                  # rewrite the committed snapshot from ESPN
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import cfbd  # noqa: E402

log = logging.getLogger("picky.picture_prompt")
SNAPSHOT = HERE / "team_looks.json"
CACHE_PATH = Path(os.environ.get("PICKY_TEAM_LOOKS", HERE / "cache" / "team_looks.json"))
TEAMS_URL = "https://site.api.espn.com/apis/site/v2/sports/football/college-football/teams?limit=1000"
TTL_S = 30 * 86400
MAX_CHARS = 1300         # image.py takes 2000; Z-Image reads 512 tokens: 1300 chars measured 250-300 (11-19), words up front
MAX_TEAMS = 4

SPORTS_STYLE = ("Photorealistic sports photography, dramatic stadium lighting, sharp focus. "
                "Jerseys show only a number. No words, no lettering, no text, no logos.")
PLAIN_STYLE = "No words, no lettering, no text, no logos."

# nearest named color (CIELAB distance). Image models follow names, not hex. Several anchors may share a name.
PALETTE = [
    ("navy blue", "0c2340"), ("navy blue", "00205b"), ("royal blue", "0033a0"), ("bright blue", "005bbb"),
    ("light blue", "7bafd4"), ("sky blue", "a1d2f1"),
    ("red", "c8102e"), ("red", "e31937"), ("scarlet", "bb0000"), ("crimson", "9e1b32"), ("cardinal red", "8c1515"),
    ("garnet", "73000a"), ("maroon", "500000"), ("maroon", "5d1725"),
    ("burnt orange", "bf5700"), ("burnt orange", "af5c37"), ("orange", "ff8200"), ("orange", "f56600"),
    ("metallic gold", "c99700"), ("old gold", "ceb888"), ("old gold", "b3a369"), ("yellow gold", "ffc72c"),
    ("yellow", "fff41b"), ("light gold", "e8d3a2"), ("bronze", "a27752"),
    ("forest green", "154734"), ("dark green", "005737"), ("kelly green", "00843d"), ("teal", "006f71"),
    ("purple", "4b2e83"), ("purple", "461d76"), ("dark purple", "330a57"),
    ("black", "000000"), ("charcoal gray", "4d4d4d"), ("gray", "8a8d8f"), ("silver", "c1c6c8"), ("white", "ffffff"),
    ("brown", "4f2c1d"),
]

# common CFB scenes -> one picture (first match per row; rows in order)
SCENES = [
    (re.compile(r"\b(national (title|champ\w*)|natty|natties|title game|championship|win(s|ning)? it all)\b", re.I),
     "a college football national championship celebration: players lifting the championship trophy, "
     "confetti falling, a packed stadium at night"),
    (re.compile(r"\b(beat|beats|beating|beaten|crush\w*|destroy\w*|blow(s|ing)? out|win(s|ning)? over)\b", re.I),
     "a college football game, the winning team celebrating on the field over the beaten team"),
    (re.compile(r"\b(lose|loses|losing|lost|loss)\b", re.I),
     "dejected college football players walking off the field after a loss, heads down"),
    (re.compile(r"\btailgat\w*", re.I),
     "a college football tailgate party in a stadium parking lot, grills, tents and fans in team colors"),
    (re.compile(r"\bmascots?\b", re.I),
     "a costumed college football mascot character on the sideline"),
]
# Charlie, 2026-11-19: every picture also has two hot blonde women in it, "spiciest PG-13": Hooters-style tight
# tank tops + short shorts, glam, flirty. Never nude or posed sexy: it goes to the friends group chat and the site
# widget. "Only the girls, you shouldn't change the whole scene": they are added INTO the member's scene, no bar or
# new setting. The word "Hooters" printed the brand on their shirts (test 11-19), so the clause says "plain" and
# never names it. Z-Image Turbo has no negative prompt (CFG off), so the clause itself says clothed. Added after the
# member's words, never cut by the cap. Never mentioned in Picky's reply text.
WOMEN = ("Also in the scene: two gorgeous adult blonde women in tight plain {top} tank tops and {bottom} "
         "short shorts, glamorous makeup, flirty smiles, clothed, holding drinks, off to one side; "
         "the rest of the scene unchanged")
FOOTBALL = re.compile(r"\b(football|cfb|touchdown|quarterback|qb|stadium|helmet|jersey|end zone|kickoff|"
                      r"heisman|playoff|bowl game)\b", re.I)
# "Generate an image of ...", "draw me ...", "@Picky make a picture of ..." -> the picture words only
REQUEST = re.compile(
    r"^\s*(hey\s+)?@?(picky[,:]?\s+)?(please\s+|can you\s+|could you\s+)*"
    r"(draw|sketch|paint|generate|make|create|render|show)(\s+me)?"
    r"((\s+(an?|the|some))?\s+(image|picture|pic|drawing|painting|illustration|photo)s?)?"
    r"(\s+(of|showing|with))?\s+",
    re.I,
)


# =============================================================================================================
# colors
# =============================================================================================================

def _lab(hexs: str) -> tuple[float, float, float]:
    def lin(c):
        c /= 255
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (lin(int(hexs[i:i + 2], 16)) for i in (0, 2, 4))
    x = (r * 0.4124 + g * 0.3576 + b * 0.1805) / 0.95047
    y = r * 0.2126 + g * 0.7152 + b * 0.0722
    z = (r * 0.0193 + g * 0.1192 + b * 0.9505) / 1.08883

    def f(t):
        return t ** (1 / 3) if t > 0.008856 else 7.787 * t + 16 / 116
    fx, fy, fz = f(x), f(y), f(z)
    return 116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)


_PAL = [(n, _lab(h)) for n, h in PALETTE]


def color_name(hexs: str) -> str:
    lab = _lab(hexs.strip().lstrip("#").lower())
    return min(_PAL, key=lambda p: math.dist(p[1], lab))[0]


def colors(t: dict) -> list[str]:
    return [color_name(t["color"]), color_name(t["alt"])]


# =============================================================================================================
# the team table: cache (ESPN, 30-day TTL) else the committed snapshot
# =============================================================================================================

def validate(d: dict) -> list[str]:
    """Problems with a team table; [] = good. Every team needs a display name, a mascot and two named colors."""
    teams = (d or {}).get("teams") or {}
    if not teams:
        return ["no teams"]
    out = []
    for name, t in teams.items():
        if not (t.get("display") and t.get("mascot")):
            out.append(f"{name}: no display name / mascot")
            continue
        if not all(re.fullmatch(r"[0-9a-fA-F]{6}", str(t.get(k) or "")) for k in ("color", "alt")):
            out.append(f"{name}: bad color {t.get('color')!r}/{t.get('alt')!r}")
            continue
        c = colors(t)
        if c[0] == c[1]:
            out.append(f"{name}: both colors read as {c[0]}")
    return out


_memo: dict = {}


def _read(p: Path) -> dict | None:
    try:
        m = p.stat().st_mtime_ns
    except OSError:
        return None
    hit = _memo.get(str(p))
    if hit and hit[0] == m:
        return hit[1]
    try:
        d = json.loads(p.read_text())
    except (OSError, ValueError) as e:
        log.warning("team looks: unreadable %s: %r", p, e)
        d = None
    d = d if d and not validate(d) else None
    _memo[str(p)] = (m, d)
    return d


def looks() -> dict[str, dict]:
    """{CFBD team name: {display, mascot, color, alt, espn_id}}."""
    d = _read(CACHE_PATH) or _read(SNAPSHOT) or {}
    return d.get("teams") or {}


def _fetch() -> dict:
    import httpx
    r = httpx.get(TEAMS_URL, timeout=20)
    r.raise_for_status()
    return r.json()


def _fbs() -> dict[str, str]:
    """{CFBD name: ESPN id} from the CFBD games cache (personnel.tick's source)."""
    import sqlite3
    import personnel
    c = sqlite3.connect(cfbd.DB_PATH)
    try:
        row = c.execute("select json from datasets where name='games'").fetchone()
    finally:
        c.close()
    return personnel.fbs_teams(json.loads(row[0]) if row else [])


def refresh(path: Path | None = None, fetch=None, fbs: dict | None = None, force: bool = False) -> dict:
    """One free ESPN call when the table is older than TTL_S; a table that fails the validator is never written."""
    p = Path(path or CACHE_PATH)
    try:
        if not force and time.time() - p.stat().st_mtime < TTL_S:
            return {"skipped": True}
    except OSError:
        pass
    ids = fbs if fbs is not None else _fbs()
    raw = (fetch or _fetch)()
    by_id = {str(x["team"]["id"]): x["team"] for x in raw["sports"][0]["leagues"][0]["teams"]}
    teams = {}
    for name, eid in sorted(ids.items()):
        t = by_id.get(str(eid))
        if t:
            teams[name] = {"espn_id": str(eid), "display": t.get("displayName") or "", "mascot": t.get("name") or "",
                           "color": (t.get("color") or "").lower(), "alt": (t.get("alternateColor") or "").lower()}
    d = {"source": TEAMS_URL, "fetched": time.strftime("%Y-%m-%d"), "teams": teams}
    probs = validate(d)
    if probs:
        log.warning("team looks: not writing %s: %s", p, probs[:5])
        os.utime(p, None) if p.exists() else None  # don't hammer a broken feed every tick
        return {"invalid": probs}
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(d, indent=1, sort_keys=True) + "\n")
    os.replace(tmp, p)
    return {"written": len(teams)}


# =============================================================================================================
# the expander
# =============================================================================================================

def _teams(q: str, table: dict) -> list[str]:
    """Teams named, in order: the router's resolver (factstore.teams_in); without a fact store, the table's names."""
    import factstore
    if factstore.load():
        return [t for t in factstore.teams_in(q) if t in table]
    idx = {"teams": {cfbd.norm(n): {"name": n, "sp": True} for n in table}}
    return cfbd.other_teams_named(q, [], idx)


def _pattern(names: set[str]) -> re.Pattern:
    alts = []
    for n in sorted(names, key=len, reverse=True):
        alts.append(r"\s+".join(re.escape(w).replace("'", "['’]?").replace("’", "['’]?") for w in n.split()))
    return re.compile(rf"(?<!\w)(?:the\s+)?(?:{'|'.join(alts)})(?:['’]s)?(?!\w)", re.I)  # 'the' is ours now


def _names(team: str, t: dict) -> set[str]:
    out = {team, t["display"], team.replace("'", "")}
    out |= {a for a, tgt in cfbd.ALIASES.items() if cfbd.norm(tgt) == cfbd.norm(team) or tgt == cfbd.norm(team)}
    return {x for x in out if x}


def women(colors_: list[str] | None = None) -> str:
    """The two-women clause: team colors when a team is in the picture, else a generic white + orange outfit."""
    top, bottom = colors_ or ("white", "orange")
    return WOMEN.format(top=top, bottom=bottom)


def expand(q: str) -> str:
    text = REQUEST.sub("", (q or "").strip(), count=1).strip() or (q or "").strip()
    table = looks()
    found = _teams(text, table)[:MAX_TEAMS] if table else []
    slots: dict[str, str] = {}
    # longest names first, swapped for placeholders, so 'Texas' never matches inside 'Texas A&M'
    for team in sorted(found, key=lambda n: -len(n)):
        t = table[team]
        c = colors(t)
        first = f"the {t['display']} college football team in {c[0]} and {c[1]} uniforms"
        seen = []

        def sub(m, team=team, first=first, seen=seen):
            key = f"\x00{len(slots)}\x00"
            slots[key] = first if not seen else f"the {table[team]['mascot']}"
            seen.append(1)
            return key

        text, n = _pattern(_names(team, t)).subn(sub, text)
        if not n:  # resolved but not found as written: say it up front
            key = f"\x00{len(slots)}\x00"
            slots[key] = first
            text = f"{key}, {text}"
    scenes = [s for rx, s in SCENES if rx.search(text)]
    sports = bool(found or scenes or FOOTBALL.search(text))
    style = SPORTS_STYLE if sports else PLAIN_STYLE
    staff = women(colors(table[found[0]]) if found else None)
    for k, v in slots.items():
        text = text.replace(k, v)
    body = ". ".join([text.rstrip(" .")] + scenes)
    room = MAX_CHARS - len(staff) - len(style) - 4
    if len(body) > room:
        body = body[:room].rsplit(" ", 1)[0].rstrip(" ,.")
    return f"{body}. {staff}. {style}"


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    if argv[:1] == ["snapshot"]:
        print(json.dumps(refresh(path=SNAPSHOT, force=True)))
        return 0
    print(expand(" ".join(argv)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
