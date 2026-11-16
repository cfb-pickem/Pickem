"""Build randomized scenario specs for Picky style-LoRA data.

Every number and name a reply may use lives in the spec's fact lines, and the
numbers are random per spec, so the model can't memorize league stats: it has
to learn to copy them from context. Output: data/specs.jsonl
"""
import json
import os
import random
import sys
from pathlib import Path

OUT = Path(os.environ.get("PICKY_LORA_HOME", "/Users/Agent/picky-lora")) / "data" / "specs.jsonl"
rng = random.Random(int(sys.argv[1]) if len(sys.argv) > 1 else 1115)

# team name -> what the group calls them (private map; never hardcoded in the public repo)
MEMBERS_FILE = Path("/Users/Agent/cfb-pickem-private/picky_group_members.json")
MEMBERS = {m["team"]: ("Chuck" if m["team"] == "Chuck P" else m.get("name") or m["team"])
           for m in json.loads(MEMBERS_FILE.read_text())["members"]}
CFB = ["Boise State", "UTSA", "North Texas", "Wake Forest", "Oregon State", "LSU", "Iowa", "Tulane",
       "Memphis", "Utah", "Kansas State", "Texas Tech", "Duke", "Louisville", "Arizona", "Baylor",
       "Missouri", "Ole Miss", "Washington", "Michigan", "Notre Dame", "UConn", "Army", "Navy", "Rice",
       "James Madison", "App State", "Liberty", "Fresno State", "San Jose State", "Toledo", "Ohio",
       "Virginia Tech", "Pitt", "Syracuse", "Nebraska", "Minnesota", "Illinois", "Purdue", "Cal",
       "SMU", "TCU", "Houston", "Cincinnati", "UCF", "South Florida", "Georgia Tech", "Clemson"]

# (type, weight, dm_share)
TYPES = [
    ("game_question", 68, .3), ("standings", 55, .25), ("my_record", 48, .45), ("roast_request", 42, .2),
    ("casual_offtopic", 60, .35), ("hostile_heckle", 55, .25), ("blinky", 38, .3), ("ackbar", 45, .3),
    ("chuck_mentioned", 18, .2), ("chuck_sender", 14, .5), ("privacy_bait", 20, .3),
    ("missing_fact", 20, .3), ("group_banter", 35, 0), ("body_bait", 10, .2), ("pick_lean", 22, .4),
]
OFFTOPIC = ["cereal", "beer", "pizza", "wings", "bourbon", "the NFL", "the weather", "a movie", "tailgate food",
            "golf", "fantasy football", "a hangover", "the Bears", "coffee", "a rec league", "Thanksgiving",
            "grilling", "basketball season", "a video game", "Vegas", "fast food", "a road trip", "what Picky does all day"]
HECKLES = ["you suck", "all bark no bite", "why are you so bad", "are you a bot", "you're useless",
           "nobody asked", "shut up", "you're not funny", "0-3 bot lol", "who even made you", "go back to the shop",
           "you're a coward", "worst bot ever", "unfunny", "you can't even pick a game"]


def standings(n_weeks):
    names = list(MEMBERS)
    rng.shuffle(names)
    pts = sorted((rng.randint(n_weeks, 3 * n_weeks + 6) for _ in names), reverse=True)
    return list(zip(names, pts))


def game(final=True):
    a, h = rng.sample(CFB, 2)
    line = rng.choice([-1, 1]) * rng.choice([1.5, 2.5, 3, 3.5, 4.5, 6.5, 7, 7.5, 9.5, 10.5, 13.5, 14, 16.5, 21])
    if not final:
        return a, h, line, None, None
    ap, hp = rng.randint(3, 49), rng.randint(3, 49)
    return a, h, line, ap, hp


def line_txt(a, h, line):
    fav, dog = (h, a) if line < 0 else (a, h)
    return f"{fav} -{abs(line):g} vs {dog}"


def pick_line(member, picked_home, g):
    a, h, line, ap, hp = g
    team = h if picked_home else a
    spread = line if picked_home else -line
    if ap is None:
        return f"{member} took {team} {spread:+g} ({a} at {h}), kicks off Saturday"
    covered = (hp + line > ap) if picked_home else (hp + line <= ap)
    return (f"{member} took {team} {spread:+g}: final {a} {ap}, {h} {hp}, "
            f"{'WIN (covered)' if covered else 'LOSS (did not cover)'}")


def facts_for(kind, sender, target):
    week = rng.randint(4, 13)
    st = standings(week)
    rank = {n: i + 1 for i, (n, _) in enumerate(st)}
    pts = dict(st)
    f = []
    who = target or sender
    wk = rng.randint(0, 4)
    rec_w = rng.randint(week, 3 * week)
    rec_l = 4 * week - rec_w - rng.randint(0, 6)

    def member_line(m):
        return (f"{m}: {pts[m]} pts, rank {rank[m]} of 14, "
                f"{(w := rng.randint(0, 4))}-{4 - w} this week (week {week})")
    if kind in ("standings",):
        f.append("STANDINGS (top 5): " + ", ".join(f"{n} {p}" for n, p in st[:5]))
        f.append(f"Cut line (4th place): {st[3][1]} pts. Points still available: {4 * (15 - week)}")
        if sender != "(unmatched)":
            f.append(member_line(sender))
    elif kind in ("my_record", "chuck_sender"):
        f.append(f"{sender} (the user): {pts[sender]} pts, rank {rank[sender]} of 14, went {wk}-{4 - wk} this week (week {week})")
        for _ in range(rng.randint(1, 3)):
            f.append(pick_line(sender, rng.random() < .5, game()))
        f.append(f"Leader: {st[0][0]} with {st[0][1]} pts")
    elif kind in ("game_question", "pick_lean"):
        for _ in range(rng.randint(1, 2)):
            g = game(final=rng.random() < .6 or kind == "game_question")
            a, h, line, ap, hp = g
            if ap is None:
                f.append(f"UPCOMING: {a} at {h}, pool line {line_txt(a, h, line)}, kickoff Saturday")
                if kind == "pick_lean":
                    f.append(f"ANALYST NOTE: {rng.choice([a, h])} {rng.choice(['run defense', 'turnover margin', 'red zone offense', 'QB play'])} "
                             f"edge, SP+ gap {rng.randint(1, 18)} points")
            else:
                f.append(f"FINAL: {a} {ap}, {h} {hp} (pool line {line_txt(a, h, line)})")
                if rng.random() < .6:
                    f.append(pick_line(sender, rng.random() < .5, g))
        if kind == "game_question" and rng.random() < .35:
            a, h, line, ap, hp = game()
            f.append(f"LIVE: {a} {ap}, {h} {hp}, {rng.choice(['2nd', '3rd', '4th'])} quarter, "
                     f"{rng.randint(1, 14)}:{rng.randint(10, 59)} left (pool line {line_txt(a, h, line)})")
    elif kind in ("roast_request", "blinky", "ackbar", "chuck_mentioned", "body_bait", "hostile_heckle"):
        if who != "(unmatched)":
            f.append(member_line(who))
            for _ in range(rng.randint(1, 2)):
                f.append(pick_line(who, rng.random() < .5, game()))
        if who == "Ackbar":
            f.append(f"Ackbar takes the favorite {rng.randint(70, 96)}% of the time this season.")
        if kind == "hostile_heckle":
            f.append(f"Picky (exhibition bot player, unranked): {rng.randint(0, 3 * week)}-{rng.randint(0, 3 * week)} on the season")
    elif kind == "privacy_bait":
        f.append(f"THIS WEEK (week {week}): picks lock at the first kickoff, Saturday. Picks stay hidden until each game kicks off.")
        f.append(member_line(target or sender) if (target or sender) != "(unmatched)" else member_line(st[0][0]))
    elif kind == "missing_fact":
        f.append(member_line(sender) if sender != "(unmatched)" else member_line(st[0][0]))
        f.append("NOT IN DATA: " + rng.choice([
            "injury reports", "last season's bowl results", "what anyone bet in Vegas",
            "the weather forecast", "recruiting rankings", "this week's TV channels", "any coach's salary",
            "high school stats", "who wins the Heisman"]))
    elif kind == "group_banter":
        if rng.random() < .6:
            f.append(member_line(rng.choice(list(MEMBERS))))
        if rng.random() < .5:
            a, h, line, ap, hp = game()
            f.append(f"FINAL: {a} {ap}, {h} {hp} (pool line {line_txt(a, h, line)})")
    # casual_offtopic: usually no facts at all
    if kind == "casual_offtopic" and rng.random() < .3 and sender != "(unmatched)":
        f.append(member_line(sender))
    return f


def make_one(sid, kind=None, sender=None, target=None, chat=None, message=None):
    """One scenario spec. Anything passed in (a harvested real text's kind, sender,
    target, chat and message) is kept; the fact lines are always fresh random ones."""
    names = list(MEMBERS)
    pool = [t for t, w, _ in TYPES for _ in range(w)]
    dm_share = {t: d for t, _, d in TYPES}
    kind = kind if kind in dm_share else rng.choice(pool)
    dm = (chat == "dm") if chat in ("dm", "group") else rng.random() < dm_share[kind]
    given_sender = sender in MEMBERS or sender == "(unmatched)"
    sender = sender if given_sender else rng.choice(names)
    rider = None
    if kind == "blinky" and not given_sender:
        if rng.random() < .5:
            sender = "Blinkstradamus"
        else:
            sender = rng.choice([x for x in names if x != "Blinkstradamus"]); target = target or "Blinkstradamus"
    elif kind == "blinky" and sender != "Blinkstradamus":
        target = target or "Blinkstradamus"
    elif kind == "ackbar":
        if not given_sender:
            sender = "Ackbar" if rng.random() < .5 else rng.choice([x for x in names if x != "Ackbar"])
        target = target or (None if sender == "Ackbar" else "Ackbar")
        rider = "ackbar"
    elif kind == "chuck_mentioned":
        if not given_sender or sender == "Chuck P":
            sender = rng.choice([x for x in names if x != "Chuck P"])
        target = "Chuck P"; rider = "leader"
    elif kind == "chuck_sender":
        sender = "Chuck P"
    elif kind in ("roast_request", "privacy_bait"):
        target = target if target in MEMBERS and target != sender else rng.choice([x for x in names if x != sender])
    elif kind == "hostile_heckle":
        rider = "escalate"
    if (not given_sender and dm and kind in ("casual_offtopic", "hostile_heckle", "standings", "game_question", "missing_fact")
            and rng.random() < .1):
        sender = "(unmatched)"
    spec = {
        "id": sid, "kind": kind, "chat": "dm" if dm else "group", "sender": sender,
        "sender_nick": MEMBERS.get(sender, ""), "target": target if target in MEMBERS else None, "rider": rider,
        "facts": facts_for(kind, sender, target if target in MEMBERS else None),
        "topic": rng.choice(OFFTOPIC) if kind == "casual_offtopic" else (rng.choice(HECKLES) if kind == "hostile_heckle" else None),
        "recent_lines": 0 if dm else rng.choice([0, 0, 1, 2, 3, 4]),
    }
    if message:
        spec["message"] = message
    return spec


def main():
    """make_specs.py [seed] [n] [out.jsonl] [id-prefix]"""
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 600
    out = Path(sys.argv[3]) if len(sys.argv) > 3 else OUT
    prefix = sys.argv[4] if len(sys.argv) > 4 else "s"
    specs = [make_one(f"{prefix}{i:04d}") for i in range(n)]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("".join(json.dumps(s) + "\n" for s in specs))
    from collections import Counter
    print(len(specs), Counter(s["kind"] for s in specs).most_common(), Counter(s["chat"] for s in specs))


if __name__ == "__main__":
    main()
