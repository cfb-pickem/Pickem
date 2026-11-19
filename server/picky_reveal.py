"""
When Picky's own picks or leans may show up in the league GROUP.

Charlie, 2026-11-19 15:41 CT: "picks are hidden till kickoff when they lock, not anything to do with all
members." Picky plays as an exhibition team, so a member could copy him. In the group, his side in a pool
game stays hidden until that game's picks LOCK, the site's own lock rule (picks_open_for_game(),
picky_picks.lock_utc): regular season, the whole week locks at its first kickoff; playoffs lock per game.
Who has or hasn't submitted plays no part. Before the lock, posts still go out without his sides, and a
group ask for his pick gets a dodge.

DMs are untouched: his entered picks already reach chat only through picky_player() (revealed at lock,
the human reveal rule), and a DM is the asker's own conversation, like a member reading back their own picks.

Fails closed: a lock that can't be determined = hidden.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
import re
import time

log = logging.getLogger("picky_reveal")


# A reply taking a side: lean words, first-person picks, "take the points", the breakdown's "Lean:" line.
CAP = r"(?-i:[A-Z])"  # a capitalized word next (a team), case-sensitive inside the re.I patterns
LEAN_TALK = re.compile(
    r"(^|\n)\s*lean\s*:|\blean(s|ing|ed)?\b|\bi\s*(would|'d|d)\s+(take|go with|ride|back|lay|play|pick|grab|hammer)\b"
    r"|\bi'?\s*(a)?m\s+(on|taking|riding|backing|going with|picking|rolling with|with)\s+(the\s+)?" + CAP +
    r"|\bi\s+(like|love|took|picked|have|got)\s+(the\s+)?" + CAP +
    r"|\bmy\s+(pick|picks|side|sides|play|plays|lean|leans|lock|card)\b|\b(take|lay|give me|gimme)\s+the\s+points\b"
    r"|\b(hammer|pound|fade|smash|ride|back)\s+(the\s+)?" + CAP + r"|\b(rolling|riding|going)\s+with\b|\bbest\s+(play|bet)\b"
    r"|\b(lock|play|pick)\s+of\s+the\s+week\b|\bi\s+like\s+(the\s+)?(dog|favorite|fav|chalk|under|over|points|home|road)"
    r"|\b(dog|underdog|favorite|chalk|points|under|over)\s+(is|are|'s)\s+(live|the play|the side|the move|the call)\b"
    r"|\b(the\s+)?(play|move|side|call)\s+(here\s+)?(is|'s)\s+(the\s+)?" + CAP +
    r"|\b(should|will|gonna|going to)\s+cover\b|\bcovers?\s+(easy|easily|comfortably)\b"
    r"|\b(the\s+)?edge\s+(goes\s+)?(to|with)\b|\bvalue\s+(is\s+|sits\s+)?(on|with)\b|\bside\s+with\b",
    re.I)


def says_lean(text: str, teams: set[str] | list[str] = (), question: str = "") -> bool:
    """Does `text` take a side in one of `teams`' games (named in the text or in the question)? With no
    team list, any lean talk counts (fail closed)."""
    if not text or not LEAN_TALK.search(text):
        return False
    names = [t for t in teams if t]
    if not names:
        return True
    hay = f"{text}\n{question}"
    return any(re.search(rf"(?<!\w){re.escape(t)}(?!\w)", hay, re.I) for t in names)


# A group ask for Picky's own side in a game.
PICK_ASK = re.compile(
    r"\bwho\s+(do|would|should|will|are|r|d)?\s*(you|u|ya|picky)\s+(like|have|got|take|taking|pick|picking|go with|"
    r"going with|ride|riding|on|backing|lean|leaning)\b|\bwho\s+(you|u|ya)\s+(got|like|taking|have|on|picking)\b"
    r"|\b(what'?s|what\s+(is|are)|whats|who'?s|gimme|give\s+me|show\s+(me|us)|tell\s+(me|us)|drop|post|let'?s\s+see|"
    r"where'?s)\s+(your|ur|picky'?s|pickys)\s+(pick|picks|lean|leans|side|sides|card|play|plays|lock|best bet|best play|call)\b"
    r"|\b(your|ur|picky'?s|pickys)\s+(pick|picks|lean|leans|side|sides|card|play|plays|lock|best bet|best play|call)"
    r"(\s+(in|on|for|this week|here|there|today))?\s*\?"
    r"|\b(your|ur|picky'?s|pickys)\s+(pick|picks|lean|leans|side|sides|play|call)\s+(in|on|for)\s+(?!last\b)"
    r"|\bwhat\s+(do|would|are)\s+(you|u)\s+(take|pick|taking|picking|do)\s+(in|on|with)\b|\bwho\s*'?s?\s+(covers?|covering)\b"
    r"|\bwho\s+covers\b|\b(give|gimme|give me)\s+(me\s+)?(a|your|ur)\s+(pick|lock|lean|side)\b|\block of the week\b"
    r"|\bbest bet\b|\bwho\s+should\s+(i|we)\s+(take|pick|go with|ride)\b|\bshould\s+(i|we)\s+(take|pick|ride|fade)\b"
    r"|\b(you|u)\s+(on|taking|picking|riding)\s+(?-i:[A-Z])|\b(lean|leaning)\s+(on|in|for)\b|\bwhich\s+side\b",
    re.I)


# 'how'd your picks do last week' / 'what's your record': his graded past picks, not this week's sides
PAST_ASK = re.compile(r"\blast (week|weekend|saturday)\b|\bhow('?d|\s+did|\s+have|\s+has|\s+are)\s+(you|u|ur|your)\b"
                      r"|\b(record|season)\b|\bwent\b", re.I)


def wants_pick(q: str, pool_teams: set[str] | list[str] = (), other_teams: set[str] | list[str] = ()) -> bool:
    """A group ask for Picky's side on an open-week pool game. An ask that names only non-pool teams is
    football talk about another game: not gated. Past-picks asks aren't either."""
    if not q or not PICK_ASK.search(q) or PAST_ASK.search(q):
        return False
    pool = [t for t in pool_teams if t]
    if any(re.search(rf"(?<!\w){re.escape(t)}(?!\w)", q, re.I) for t in pool):
        return True
    return not any(re.search(rf"(?<!\w){re.escape(t)}(?!\w)", q, re.I) for t in other_teams if t)


DODGES = [
    "Nice try. My picks stay in the vault till they lock at kickoff.",
    "Not in here. You'll see my sides when picks lock at kickoff. Can't have anybody copying the homework.",
    "My card stays face down in the group till kickoff. Make your own calls first.",
    "Ha, no. My picks show when they lock at kickoff, same as yours.",
]


def dodge(key: str = "") -> str:
    return DODGES[int(hashlib.sha1(key.encode()).hexdigest(), 16) % len(DODGES)]


# -----------------------------------------------------------------------------
# live status for the group chat (one League read a minute at most)
# -----------------------------------------------------------------------------

_CACHE: dict = {"at": 0.0, "val": None}
TTL_S = 60


def group_status(now: dt.datetime | None = None, league_factory=None) -> dict:
    """{"revealed": bool, "week": int|None, "pool_teams": set[str]} for the open week. pool_teams = the
    teams in its pool games that haven't locked yet (playoffs lock per game), revealed = none left.
    Any failure: not revealed, team lists empty (says_lean then treats any lean talk as a leak)."""
    t = time.monotonic()
    if _CACHE["val"] is not None and t - _CACHE["at"] < TTL_S and league_factory is None:
        return _CACHE["val"]
    val = {"revealed": False, "week": None, "pool_teams": set()}
    try:
        if league_factory is None:
            import group_posts as gp
            lg = gp.League(gp.DB(), now or dt.datetime.now(gp.CT))
        else:
            lg = league_factory()
        w = lg.open_week()
        val["week"] = w
        if w is not None:
            hidden = [g for g in lg.week_games(w) if not lg.game_locked(g, w)]
            val["pool_teams"] = {x for g in hidden for x in (g.get("Away"), g.get("Home")) if x}
            val["revealed"] = not hidden
        else:
            val["revealed"] = True  # no open week: nothing of his is pending
    except Exception as e:  # noqa: BLE001  fail closed
        log.warning("reveal status unavailable: %r", e)
    if league_factory is None:
        _CACHE.update(at=t, val=val)
    return val
