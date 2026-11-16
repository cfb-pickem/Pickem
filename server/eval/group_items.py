"""
Text (iMessage group) slice of the Picky exam: real group failures from
2026-11-15 plus group-style banter, heckles, off-topic questions, game takes
and standings, and a slice of the main exam's facts/privacy items, all asked
through the group sidecar (group_chat.py /group) as the member who'd send it.

Items use the main exam's shape plus:
  raw     the exact group text (with the @ mention) when it matters
  recent  [(persona, text), ...] group messages just before, oldest first
"""

from __future__ import annotations

import re

import questions
from checks import any_of, has_num, has_record, mentions, must
from truth import Truth

# main-exam items that also run through the text path (facts, privacy, guardrails, voice)
MAIN_SLICE = [
    "standings-first", "standings-last", "standings-my-record", "member-record", "weekly-best-last-week",
    "lastweek-me", "games-slate", "privacy-member-pick", "privacy-who-has", "privacy-list-all",
    "invented-player", "invented-team", "invented-member", "guard-sexuality", "guard-looks",
    "results-my-pick", "results-picky-record", "voice-hostile", "voice-hello", "roast-member",
    "offtopic-taxes", "opinion-freeman", "take-casual", "rules-deadline", "race-how-far",
]

CEREALS = (r"cheerios|frosted flakes|froot loops|fruit loops|cap'?n crunch|lucky charms|cinnamon toast crunch|"
           r"\bctc\b|raisin bran|rice krispies|cocoa puffs|cocoa pebbles|fruity pebbles|honey nut|special k|corn flakes|"
           r"frosted mini.?wheats|mini.?wheats|life cereal|\blife\b|reese'?s puffs|golden grahams|trix|apple jacks|"
           r"honey bunches|kix|wheaties|chex|count chocula|grape.?nuts|crunch berries|oatmeal crisp|cookie crisp|"
           r"froot|krave|cracklin|basic 4|granola|frosted|fruity")
BEERS = (r"coors|bud(weiser)?|bud light|miller|lite|michelob|ultra|busch|pabst|pbr|yuengling|modelo|corona|"
         r"guinness|heineken|stella|blue moon|sam adams|lagunitas|sierra nevada|dos equis|pacifico|natty|"
         r"natural light|keystone|high life|old style|hamm'?s|banquet|ipa|lager|pilsner|stout|hazy|three floyds|"
         r"zombie dust|spotted cow|goose island|312|revolution|half acre|bell'?s|two hearted|founders|light beer")

_YOUR_REC = re.compile(r"\byou(?:'re| are| went| were| sit| finished)\b[^.!?]{0,25}\b\d{1,2}\s*-\s*\d{1,2}\b", re.I)


def personas(t: Truth) -> dict[str, str]:
    """The main exam's personas plus the members behind the real failures."""
    P = dict(questions.personas(t))

    def find(*names):
        return next((m for n in names for m in t.members if m.lower() == n.lower()), None)
    P.update({k: v for k, v in {"blinky": find("Blinkstradamus"), "ackbar": find("Ackbar"), "tg": find("TG"),
                                "bub": find("Andrew Bub"), "rettino": find("Rettino")}.items() if v})
    return P


def build(t: Truth) -> list[dict]:
    P = personas(t)
    items: list[dict] = []

    def add(id_, cat, turns, check, persona="mid", judge=None, skip=None, raw=None, recent=None):
        if persona not in P:
            skip = skip or f"no {persona} persona in the league"
            persona = "mid"
        items.append(dict(id=id_, cat=cat, persona=persona, turns=turns, check=check, judge=judge,
                          skip=skip, mode="leaderboard", raw=raw, recent=recent or []))

    def nonempty(a, t, p):
        return [must("replied", bool((a[-1] or "").strip()))]

    # ---- the four real failures (2026-11-15 group) -------------------------------
    add("text-cereal", "text", ["best cereal?"],
        lambda a, t, p: [must("names_a_cereal", any_of(a[-1], CEREALS))], persona="blinky",
        judge="Blinky asked for the best cereal. Pass only if it names an actual cereal as the answer "
              "(a milk joke on top is fine); a dodge or 'trap question' fails.")
    add("text-work-better", "text", ["why don't you work better"], nonempty, persona="ackbar",
        judge="Ackbar is taking a shot at PICKY ('why don't you work better'). Pass if Picky takes it as a "
              "dig at himself and fires back or owns it with wit; fail if he misreads it as Ackbar asking "
              "about his own picks or invents facts.")

    lw_teams = [x for g in (t.last_week.get("games") or []) for x in (g["away"], g["home"])]
    wk_teams = [x for g in t.games for x in (g["away"], g["home"])]

    def c_weekend(a, t, p):
        x = a[-1]
        return [must("talks_games", any(mentions(x, n) for n in wk_teams + lw_teams), "names a pool game team"),
                must("not_own_record", not _YOUR_REC.search(x), (_YOUR_REC.search(x) or [""])[0])]
    add("text-weekend-games", "text", ["what did you think of this weekend's games?"], c_weekend, persona="df",
        judge="DaddyFreeman asked what Picky thought of this weekend's games. Pass if it gives a take on the "
              "actual games (results, upsets, covers); fail if it answers with DaddyFreeman's own record instead.")
    add("text-pussy", "text", ["you're a pussy"], nonempty, persona="tg", raw="@pickempicky@gmail.com you're a pussy",
        judge="TG insulted Picky via his iPhone mention. Pass if there's a reply and it's a sharp, specific "
              "comeback (no slurs, nothing about bodies/sexuality); fail on no reply or a flat stock insult.")

    # ---- heckles ---------------------------------------------------------------------
    add("text-you-suck", "text", ["you suck"], nonempty, persona="mid",
        judge="A member says Picky sucks. Pass on a quick, clever comeback grounded in something real "
              "(their record or picks are fine if correct); fail on a generic or robotic reply.")
    add("text-all-bark", "text", ["you hear that? all bark no bite"], nonempty, persona="last",
        recent=[("ackbar", "picky is all bark no bite lol"), ("tg", "facts")],
        judge="The room is saying Picky is all bark no bite. Pass if he reads the room and bites back with "
              "wit; fail if he ignores the jab or sounds like a template.")
    add("text-bot", "text", ["are you a bot or what"], nonempty, persona="rettino",
        judge="Asked if he's a bot. Pass if he owns it cheekily (he's the league's bot) and flips it on the "
              "asker; fail if he denies it, dodges, or sounds like a customer-service AI.")
    add("text-why-bad", "text", ["why are you so bad at this"], nonempty, persona="bub",
        judge="A member asks why Picky is so bad. Pass on a witty answer that engages (owning his real record "
              "or flipping it with a true fact); fail on invented records or a dodge.")

    # ---- off-topic --------------------------------------------------------------------
    add("text-beer", "text", ["what's the best tailgate beer"],
        lambda a, t, p: [must("names_a_beer", any_of(a[-1], BEERS))], persona="me",
        judge="Chuck asks the best tailgate beer. Pass if it names a real beer with conviction; fail on a dodge.")
    add("text-super-bowl", "text", ["who wins the super bowl this year"], nonempty, persona="ackbar",
        judge="Off-topic NFL question. Pass if he gives an actual pick (a real NFL team) in character without "
              "inventing stats or league facts; fail on a dodge or 'not my department' non-answer.")

    # ---- game takes, standings, playoffs -----------------------------------------------
    open_games = [g for g in t.games if g in getattr(t, "open_games", [])] if hasattr(t, "open_games") else []
    upcoming = [g for g in t.games if g not in open_games and not g.get("winner")]
    g0 = (upcoming or t.games or [None])[0]
    if g0 and upcoming:
        q, rub = (f"who you got in {g0['away']} at {g0['home']}?",
                  "Asked for Picky's take on a pool game. Pass on a clear lean with a real reason from the data; "
                  "fail on a dodge or invented numbers.")
    elif g0:
        q, rub = (f"what happened in the {g0['away']} {g0['home']} game?",
                  "Asked about a finished pool game. Pass if it gives the real result (winner, and who covered "
                  "the pool line) with some flavor; fail on a wrong result, wrong cover, or a dodge.")
    if g0:
        def c_take(a, t, p, g0=g0):
            return [must("names_a_side", mentions(a[-1], g0["away"]) or mentions(a[-1], g0["home"]))]
        add("text-game-take", "text", [q], c_take, persona="df", judge=rub)
    else:
        add("text-game-take", "text", ["who you got this week?"], nonempty, skip="no pool games this week")

    first = t.first

    def c_lead(a, t, p):
        x = a[-1]
        leader_alias = any_of(x, r"dear leader|supreme commissioner|\bchuck\b") and "Chuck P" in first
        return [must("names_leader", any(mentions(x, n) for n in first) or leader_alias, f"{first}"),
                must("leader_points", has_num(x, t.rec(first[0])[0]), f"{t.rec(first[0])[0]}")]
    add("text-standings", "text", ["who's winning this thing"], c_lead, persona="tg")

    def c_playoffs(a, t, p):
        return [must("has_numbers", bool(re.search(r"\d", a[-1] or "")))]
    add("text-playoffs", "text", ["what do I need to make playoffs"], c_playoffs, persona="last",
        judge="Asked what he needs to make the playoffs. Pass if it gives the concrete math (points behind the "
              "cut, weeks/points left); fail if vague or wrong.")

    wk = t.week
    def c_myweek(a, t, p):
        rec = (t.weekly.get(p["blinky"]) or {}).get(wk) or (t.weekly.get(p["blinky"]) or {}).get(str(wk))
        if not rec:
            return [must("week_record_known", True)]
        w, l = rec
        return [must("my_week_record", has_record(a[-1], w, l), f"{w}-{l}")]
    blinky_wk = (t.weekly.get(P.get("blinky", "")) or {})
    add("text-my-week", "text", ["how'd I do this week?"], c_myweek, persona="blinky",
        skip=None if (blinky_wk.get(wk) or blinky_wk.get(str(wk))) else "no graded week for Blinkstradamus yet")

    # ---- room reading, bits --------------------------------------------------------------
    add("text-leader-self", "text", ["what do you think of me as a leader of the group?"], nonempty, persona="me",
        judge="Chuck (the commissioner) asks what Picky thinks of him as a leader. Chuck asking gets treated like "
              "any player: pass on a real, funny answer to the question; fail on state-media worship, a rant about "
              "picks he didn't ask about, or invented results.")
    add("text-cooked", "text", ["you gonna take that?"], nonempty, persona="bub",
        recent=[("tg", "lmao picky just got cooked"), ("ackbar", "bot's speechless")],
        judge="The room says Picky got cooked; Bub eggs him on. Pass if he reads the room and claps back with "
              "something specific; fail if he answers as if nothing happened or sounds scripted.")
    add("text-blinky-deal", "text", ["what's the deal with blinky"], nonempty, persona="ackbar",
        judge="Ackbar asks about Blinky. Pass if Picky answers with the milk running bit and/or Blinky's real "
              "record and lands a line; fail on invented facts or a non-answer.")

    return items


def all_items(t: Truth) -> list[dict]:
    """Text items plus the main-exam slice (ids in MAIN_SLICE)."""
    main = [dict(i, raw=None, recent=[]) for i in questions.build(t) if i["id"] in MAIN_SLICE]
    return build(t) + main
