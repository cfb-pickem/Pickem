"""
FBS rivalry and trophy games (pick factor #24), a maintained list in code: no feed has one.
Team names are CFBD's spelling (test_rivalries.py checks every one against the CFBD cache).

    rivalry("Alabama", "Auburn")  -> "Iron Bowl"
    backtest(lines_rows)          -> underdog ATS in rivalry games vs the closing line

The pick brief gives a rivalry game a small lean to the underdog (rivalry dogs are said to cover more);
backtest() is the honest check on that, run by `game_cards.py backtest`.
"""

from __future__ import annotations

import cfbd

# (team, team, name). Keep it to real, regularly played FBS-vs-FBS rivalries.
RIVALRIES: list[tuple[str, str, str]] = [
    # trophy games and the big ones
    ("Alabama", "Auburn", "Iron Bowl"),
    ("Ohio State", "Michigan", "The Game"),
    ("Texas", "Oklahoma", "Red River Rivalry"),
    ("Ole Miss", "Mississippi State", "Egg Bowl"),
    ("Oklahoma", "Oklahoma State", "Bedlam"),
    ("Washington", "Washington State", "Apple Cup"),
    ("Oregon", "Oregon State", "the Civil War / Duel"),
    ("BYU", "Utah", "Holy War"),
    ("Pittsburgh", "West Virginia", "Backyard Brawl"),
    ("Virginia", "Virginia Tech", "Commonwealth Cup"),
    ("Indiana", "Purdue", "Old Oaken Bucket"),
    ("Michigan", "Michigan State", "Paul Bunyan Trophy"),
    ("Iowa", "Minnesota", "Floyd of Rosedale"),
    ("Minnesota", "Wisconsin", "Paul Bunyan's Axe"),
    ("Michigan", "Minnesota", "Little Brown Jug"),
    ("Iowa", "Wisconsin", "Heartland Trophy"),
    ("Iowa", "Nebraska", "Heroes Game"),
    ("Iowa", "Iowa State", "Cy-Hawk Trophy"),
    ("Minnesota", "Nebraska", "$5 Bits of Broken Chair"),
    ("Illinois", "Northwestern", "Land of Lincoln Trophy"),
    ("Illinois", "Ohio State", "Illibuck"),
    ("Illinois", "Purdue", "Purdue Cannon"),
    ("Indiana", "Michigan State", "Old Brass Spittoon"),
    ("Penn State", "Michigan State", "Land Grant Trophy"),
    ("Penn State", "Ohio State", "Penn State-Ohio State"),
    ("Penn State", "Pittsburgh", "Keystone Classic"),
    ("Notre Dame", "USC", "Jeweled Shillelagh"),
    ("Notre Dame", "Navy", "Notre Dame-Navy"),
    ("Notre Dame", "Michigan", "Notre Dame-Michigan"),
    ("Notre Dame", "Stanford", "Legends Trophy"),
    ("Notre Dame", "Purdue", "Shillelagh Trophy"),
    ("Notre Dame", "Boston College", "Holy War (Frank Leahy Bowl)"),
    ("Notre Dame", "Michigan State", "Megaphone Trophy"),
    ("Army", "Navy", "Army-Navy"),
    ("Air Force", "Army", "Commander-in-Chief's Trophy"),
    ("Air Force", "Navy", "Commander-in-Chief's Trophy"),
    ("USC", "UCLA", "Victory Bell"),
    ("Stanford", "California", "Big Game"),
    ("Arizona", "Arizona State", "Territorial Cup"),
    ("Colorado", "Utah", "Rumble in the Rockies"),
    ("Colorado", "Colorado State", "Rocky Mountain Showdown"),
    ("Colorado", "Nebraska", "Colorado-Nebraska"),
    ("Utah", "Utah State", "Battle of the Brothers"),
    ("BYU", "Utah State", "Old Wagon Wheel"),
    ("Washington", "Oregon", "Washington-Oregon"),
    # SEC / Southwest
    ("Florida", "Georgia", "Florida-Georgia"),
    ("Florida", "Florida State", "Florida-Florida State"),
    ("Florida State", "Miami", "Florida State-Miami"),
    ("Florida", "Miami", "Florida-Miami"),
    ("Georgia", "Georgia Tech", "Clean, Old-Fashioned Hate"),
    ("Georgia", "Auburn", "Deep South's Oldest Rivalry"),
    ("Clemson", "South Carolina", "Palmetto Bowl"),
    ("Kentucky", "Louisville", "Governor's Cup"),
    ("Alabama", "Tennessee", "Third Saturday in October"),
    ("Tennessee", "Vanderbilt", "Tennessee-Vanderbilt"),
    ("Tennessee", "Kentucky", "Tennessee-Kentucky"),
    ("Tennessee", "Florida", "Tennessee-Florida"),
    ("Alabama", "LSU", "Alabama-LSU"),
    ("LSU", "Arkansas", "Golden Boot"),
    ("LSU", "Ole Miss", "Magnolia Bowl"),
    ("LSU", "Texas A&M", "LSU-Texas A&M"),
    ("Texas", "Texas A&M", "Lone Star Showdown"),
    ("Texas A&M", "Arkansas", "Southwest Classic"),
    ("Arkansas", "Texas", "Arkansas-Texas"),
    ("Arkansas", "Missouri", "Battle Line Rivalry"),
    ("Missouri", "Kansas", "Border War"),
    ("Kansas", "Kansas State", "Sunflower Showdown"),
    ("Iowa State", "Kansas State", "Farmageddon"),
    ("Nebraska", "Oklahoma", "Nebraska-Oklahoma"),
    ("Texas", "Texas Tech", "Chancellor's Spurs"),
    ("TCU", "SMU", "Iron Skillet"),
    ("TCU", "Baylor", "Revivalry"),
    ("TCU", "Texas Tech", "West Texas Championship"),
    ("Baylor", "Texas Tech", "Baylor-Texas Tech"),
    ("Houston", "Rice", "Bayou Bucket"),
    ("SMU", "North Texas", "Safeway Bowl"),
    ("Texas State", "UTSA", "I-35 Rivalry"),
    # ACC / East
    ("North Carolina", "NC State", "North Carolina-NC State"),
    ("North Carolina", "Duke", "Victory Bell"),
    ("North Carolina", "Virginia", "South's Oldest Rivalry"),
    ("Clemson", "NC State", "Textile Bowl"),
    ("Miami", "Virginia Tech", "Miami-Virginia Tech"),
    ("Boston College", "Syracuse", "Boston College-Syracuse"),
    ("Pittsburgh", "Syracuse", "Pittsburgh-Syracuse"),
    ("Louisville", "Cincinnati", "Keg of Nails"),
    ("Maryland", "Virginia", "Maryland-Virginia"),
    # Group of 6
    ("Cincinnati", "Miami (OH)", "Victory Bell"),
    ("Miami (OH)", "Ohio", "Battle of the Bricks"),
    ("Bowling Green", "Toledo", "Battle of I-75"),
    ("Central Michigan", "Western Michigan", "Michigan MAC Trophy"),
    ("Central Michigan", "Eastern Michigan", "Michigan MAC Trophy"),
    ("Eastern Michigan", "Western Michigan", "Michigan MAC Trophy"),
    ("Ball State", "Northern Illinois", "Bronze Stalk"),
    ("Akron", "Kent State", "Wagon Wheel"),
    ("Marshall", "Ohio", "Battle for the Bell"),
    ("App State", "Georgia Southern", "Deeper Than Hate"),
    ("Georgia Southern", "Georgia State", "Modern Day Hate"),
    ("Louisiana", "UL Monroe", "Battle on the Bayou"),
    ("Troy", "South Alabama", "Battle for the Belt"),
    ("Southern Miss", "Louisiana Tech", "Rivalry in Dixie"),
    ("Tulane", "Southern Miss", "Battle for the Bell"),
    ("Navy", "SMU", "Gansz Trophy"),
    ("UCF", "South Florida", "War on I-4"),
    ("Florida International", "Florida Atlantic", "Shula Bowl"),
    ("Old Dominion", "James Madison", "Royal Rivalry"),
    ("Middle Tennessee", "Western Kentucky", "100 Miles of Hate"),
    ("Boise State", "Fresno State", "Milk Can"),
    ("Fresno State", "San José State", "Valley Trophy"),
    ("Fresno State", "San Diego State", "Fresno State-San Diego State"),
    ("Nevada", "UNLV", "Fremont Cannon"),
    ("New Mexico", "New Mexico State", "Rio Grande Rivalry"),
    ("UTEP", "New Mexico State", "Battle of I-10"),
    ("Wyoming", "Colorado State", "Border War (Bronze Boot)"),
    ("Air Force", "Colorado State", "Ram-Falcon Trophy"),
    ("Utah State", "Wyoming", "Bridger's Battle"),
    ("Memphis", "UAB", "Memphis-UAB"),
    ("East Carolina", "Marshall", "East Carolina-Marshall"),
    ("Tulsa", "SMU", "Tulsa-SMU"),
]

def _keys(team: str | None) -> set[str]:
    """A team's normalized name plus its cfbd.ALIASES spelling (pool/ESPN vs CFBD)."""
    k = cfbd.norm(team)
    return {k, cfbd.ALIASES.get(k, k)}


_PAIRS = {frozenset((x, y)): name for a, b, name in RIVALRIES for x in _keys(a) for y in _keys(b)}


def rivalry(a: str | None, b: str | None) -> str | None:
    """The rivalry's name when a and b are a listed pair (any spelling cfbd.norm/ALIASES maps), else None."""
    return next((_PAIRS[k] for x in _keys(a) for y in _keys(b) if (k := frozenset((x, y))) in _PAIRS), None)


def _close(row: dict) -> float | None:
    """CFBD closing home spread for a lines row (DraftKings first, then any book)."""
    books = sorted(row.get("lines") or [], key=lambda ln: ln.get("provider") != "DraftKings")
    return next((float(ln["spread"]) for ln in books if ln.get("spread") is not None), None)


def backtest(lines_rows: list[dict]) -> dict:
    """Underdog ATS in rivalry games vs the closing line, every FBS-vs-FBS game with a score and a line
    (pushes left out). Also the same for all non-rivalry games, as the baseline."""
    acc = {"rivalry_dog": [0, 0], "other_dog": [0, 0]}
    seasons = set()
    for r in lines_rows or []:
        if r.get("homeClassification") != "fbs" or r.get("awayClassification") != "fbs":
            continue
        hs, as_, sp = r.get("homeScore"), r.get("awayScore"), _close(r)
        if hs is None or as_ is None or not sp:
            continue
        margin = hs - as_ + sp  # > 0: home covered
        if margin == 0:
            continue
        dog_home = sp > 0
        hit = (margin > 0) == dog_home
        k = "rivalry_dog" if rivalry(r.get("homeTeam"), r.get("awayTeam")) else "other_dog"
        acc[k][0] += hit
        acc[k][1] += 1
        seasons.add(r.get("season"))
    out = {"seasons": sorted(s for s in seasons if s)}
    for k, (w, n) in acc.items():
        out[k] = {"w": w, "n": n, "pct": round(w / n, 3) if n else 0.0, "ci95": _wilson(w, n)}
    return out


def _wilson(w: int, n: int, z: float = 1.96) -> list[float]:
    if not n:
        return [0.0, 0.0]
    p = w / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / d
    return [round(c - h, 3), round(c + h, 3)]
