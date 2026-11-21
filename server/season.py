"""
The calendar, from dates and data only (zero maintenance, phase 8): which season it is, whether the league is
in its offseason, and what Picky says about "this week" when there is no week. No hardcoded year or week.

  season_year(date)          the CFB season a date belongs to: Jul-Dec = that year, Jan-Jun = the year before
                             (bowls and the CFP title game in January belong to the season that started in August)
  date_in_season(m, d, ref)  a month/day named in a headline ("fired Oct 24") as a date in ref's season
  offseason(ctx, now)        every pool game of the newest week is final and the last one kicked off more than
                             OFFSEASON_GAP days ago (bowl/CFP rounds are never that far apart, and the
                             Sunday-to-Tuesday gap before next week's games post is only days)
  offseason_text(ctx)        the code answer for "this week" questions in the offseason (final standings, when
                             the next season's games show up)
  offseason_rider(ctx, now)  the same fact as one line for the full brain's per-request tail ("" in season)
"""

from __future__ import annotations

import datetime as dt

OFFSEASON_GAP = dt.timedelta(days=21)
# router fact kinds that are about the current week's games (answered with offseason_text in the offseason)
WEEK_KINDS = frozenset({
    "lock", "slate", "pick", "pool_games", "my_picks", "score", "line_move", "lines_locked", "lines_now",
    "spread", "upset", "qb", "flip", "defend", "others", "short", "value", "hidden", "compare", "lock_pick",
    "pick_ctx", "fade", "read", "weeks_left", "hypo_value", "keynum", "qb_status", "lookahead_slate", "channel", "slate_ats",
})


def season_year(d: dt.date | None = None) -> int:
    d = d or dt.date.today()
    return d.year if d.month >= 7 else d.year - 1


def date_in_season(month: int, day: int, ref: dt.date | None = None) -> dt.date:
    """Aug-Dec dates sit in the season's first calendar year, Jan-Jul in the next one."""
    y = season_year(ref)
    return dt.date(y if month >= 7 else y + 1, month, day)


def _start(g: dict) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(str(g.get("start") or "")).replace(tzinfo=None)
    except ValueError:
        return None


def _final(g: dict) -> bool:
    return str(g.get("status") or "").lower() == "final" or bool(g.get("winner"))


def offseason(ctx: dict | None, now: dt.datetime) -> bool:
    if not ctx or ctx.get("error"):
        return False
    games = [g for g in ctx.get("games_this_week") or [] if not g.get("tiebreaker")] or \
        list(ctx.get("games_this_week") or [])
    starts = [s for s in (_start(g) for g in games) if s]
    now = now.replace(tzinfo=None)
    if not games or not starts:
        return False
    return all(_final(g) for g in games) and now - max(starts) > OFFSEASON_GAP


def _standings_line(ctx: dict) -> str:
    rows = sorted(ctx.get("standings_ytd") or [], key=lambda r: r.get("rank") or 99)[:3]
    if not rows:
        return ""
    names = {1: "1st", 2: "2nd", 3: "3rd"}
    return "Final standings: " + ", ".join(
        f"{names.get(r.get('rank'), str(r.get('rank')))} {r['team']}"
        + (f" ({r['w']}-{r['l']})" if r.get("w") is not None and r.get("l") is not None else "") for r in rows) + "."


def offseason_text(ctx: dict) -> str:
    season = ctx.get("season")
    nxt = f"the {int(season) + 1} pool games" if isinstance(season, int) else "next season's pool games"
    head = (f"It's the offseason: the {season} season is in the books, so there are no games, lines or picks "
            "to lock right now." if season else
            "It's the offseason: no games, lines or picks to lock right now.")
    return " ".join(x for x in (head, _standings_line(ctx),
                                f"Nothing to pick until {nxt} go up; college football's back in late August.") if x)


def offseason_rider(ctx: dict, now: dt.datetime) -> str:
    if not offseason(ctx, now):
        return ""
    return ("\nOFFSEASON: the league's last games are long final and there is no current week. If they ask about "
            "this week, games, lines or picks, say it's the offseason; don't present old games as upcoming.")
