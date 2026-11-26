"""Early-opener check (genius week 13: 'Pitt went from a 7.5-point underdog to a 9.5-point favorite, what news did I
miss?'). DraftKings posts look-ahead lines weeks before a game; ESPN and CFBD both carry that early number as DK's
"open". When another book's open on the same game is far from DK's, DK's opener was a placeholder, not a market:
the move from it isn't a move. Pitt-Cal: DK opened Cal -7.5, Bovada opened Pitt -9.5 and never moved.

Code only, from the CFBD lines row every caller already has (one row per book with spreadOpen/overUnderOpen)."""

from __future__ import annotations

SPREAD_GAP = 3.5  # books' openers this far apart on the spread = one of them is an early number
TOTAL_GAP = 5.0   # ... or on the total (Michigan-Ohio State: DK opened 46.5, Bovada 38.5)
BOOK = "DraftKings"


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def early_open(lines: list[dict] | None, book: str = BOOK) -> dict | None:
    """{'book', 'spread', 'total', 'vs', 'spread_open', 'total_open'}: `book`'s opener (spread/total, home side)
    disagrees with another book's opener by SPREAD_GAP / TOTAL_GAP, so the honest open is the other book's
    (spread_open/total_open). None when the books agree, or there's no second book to compare with."""
    ls = lines or []
    mine = next((x for x in ls if x.get("provider") == book and _f(x.get("spreadOpen")) is not None), None)
    if not mine:
        return None
    ms, mt = _f(mine.get("spreadOpen")), _f(mine.get("overUnderOpen"))
    now = _f(mine.get("spread"))
    best = None
    for o in ls:
        if o is mine or _f(o.get("spreadOpen")) is None:
            continue
        os_, ot = _f(o.get("spreadOpen")), _f(o.get("overUnderOpen"))
        far = abs(ms - os_) >= SPREAD_GAP or (mt is not None and ot is not None and abs(mt - ot) >= TOTAL_GAP)
        if far and (best is None or (now is not None and abs(os_ - now) < abs(best[0] - now))):
            best = (os_, ot, o.get("provider") or "another book")
    if not best:
        return None
    return {"book": book, "spread": ms, "total": mt, "vs": best[2], "spread_open": best[0],
            "total_open": best[1] if best[1] is not None else mt}


def fix(m: dict, eo: dict | None, open_key: str, now_key: str, otot_key: str | None = None,
        tot_key: str | None = None) -> dict:
    """m with its open swapped for the honest one when m's open is the early number (within a point: ESPN and
    CFBD copy DK's opener a point apart, USC-UCLA 11.5 vs 10.5) or as far from the other book's open, plus
    m['early_open'] = eo (with m's own copy of the early number). Recomputes 'move' / 'total_move' when m carries
    them. Anything else: m unchanged."""
    o = _f(m.get(open_key)) if eo else None
    if o is None or (abs(o - eo["spread"]) > 1.0 and abs(o - eo["spread_open"]) < SPREAD_GAP):
        return m
    eo = dict(eo, spread=o)
    m = dict(m, early_open=eo)
    m[open_key] = eo["spread_open"]
    if "move" in m and _f(m.get(now_key)) is not None:
        m["move"] = round(_f(m[now_key]) - eo["spread_open"], 1)
    if otot_key and eo.get("total_open") is not None and _f(m.get(otot_key)) is not None \
            and eo.get("total") is not None and abs(_f(m[otot_key]) - eo["total"]) <= 0.5:
        m[otot_key] = eo["total_open"]
        if tot_key and "total_move" in m and _f(m.get(tot_key)) is not None:
            m["total_move"] = round(_f(m[tot_key]) - eo["total_open"], 1)
    return m


def _pos(book: str) -> str:
    return book + ("'" if book.endswith("s") else "'s")


def fav(sp: float, away: str, home: str) -> str:
    return (f"{home} {sp:g}" if sp < 0 else f"{away} {-sp:g}") if sp else "a pick'em"


def sentence(eo: dict, now: float | None, away: str, home: str) -> str:
    """'The California -7.5 open was DraftKings' early number. Bovada opened Pittsburgh -9.5 and it's still there,
    so the line hasn't really moved.'"""
    s = f"The {fav(eo['spread'], away, home)} open was {_pos(eo['book'])} early number. {eo['vs']} opened " \
        f"{fav(eo['spread_open'], away, home)}"
    if now is None or now == eo["spread_open"]:
        s += " and it's still there, so the line hasn't really moved."
    else:
        d = abs(now - eo["spread_open"])
        s += f", so the real move is {d:g} toward {home if now < eo['spread_open'] else away}" + \
            (" (barely anything)." if d <= 1 else ".")
    t, to = eo.get("total"), eo.get("total_open")
    if t is not None and to is not None and abs(t - to) >= TOTAL_GAP:
        s += f" Same with the total: {eo['book']} opened {t:g}, {eo['vs']} {to:g}."
    return s
