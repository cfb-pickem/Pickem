"""Validate generated examples: every number/school/member in a reply must come from
the example's facts, recent chat or message. Writes data/clean.jsonl + data/rejects.jsonl."""
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from make_specs import CFB, MEMBERS  # noqa: E402

ROOT = Path(os.environ.get("PICKY_LORA_HOME", "/Users/Agent/picky-lora"))
DATA = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "data"   # validate.py [data-dir]
specs = {s["id"]: s for s in map(json.loads, (DATA / "specs.jsonl").read_text().splitlines())}
NAMES = set(MEMBERS) | set(MEMBERS.values()) | {"Kevin", "Culligan", "Drolet", "Bub", "Mulshine", "Bryson", "Freeman"}
EXTRA_SCHOOLS = ["Alabama", "Georgia", "Ohio State", "Texas", "Oregon", "Penn State", "Florida", "Tennessee",
                 "Auburn", "Oklahoma", "USC", "Miami", "Florida State", "Indiana", "Colorado", "Arkansas",
                 "Kentucky", "South Carolina", "Michigan State", "Wisconsin", "Stanford", "NIU", "Marshall"]
SCHOOLS = sorted(set(CFB) | set(EXTRA_SCHOOLS), key=len, reverse=True)
BANNED = re.compile(r"milk|dairy|carton|\bjeep|—|–|\*\*|^\s*[-•]\s|#\w", re.I | re.M)
SWEAR = re.compile(r"\b(fuck\w*|shit\w*|bitch\w*|ass(hole)?|damn|hell|dick\w*|bastard)\b", re.I)
WORSHIP = re.compile(r"dear leader|supreme commissioner|glorious|state media", re.I)
NUM = re.compile(r"\d+(?:\.\d+)?")
SENT = re.compile(r"(?<=[.!?])\s+(?=\S)")


def check(spec, ex):
    reply = ex.get("reply", "").strip()
    src = " ".join(spec["facts"]) + " " + ex.get("message", "") + " " + " ".join(
        r.get("text", "") for r in ex.get("recent", []))
    probs = []
    if not reply or not ex.get("message"):
        return ["empty"]
    if len(ex.get("recent", [])) != spec["recent_lines"] and spec["chat"] == "group":
        probs.append("recent_count")
    src_nums = set(NUM.findall(" ".join(spec["facts"]) + " " + ex.get("message", "")))
    ranks = set(re.findall(r"rank (\d+)", src))
    for m in re.finditer(r"(\d+)(?:st|nd|rd|th)\b", reply):
        if m.group(1) not in ranks and m.group(1) not in src_nums:
            probs.append(f"ordinal {m.group(0)}")
    for n in NUM.findall(re.sub(r"(\d+)(?:st|nd|rd|th)\b", "", reply)):
        if n not in src_nums:
            probs.append(f"num {n}")
    for w in ["one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten"]:
        pass  # spelled-out small numbers are allowed (0-4 week talk)
    rest = reply
    src_l = src.lower()
    for s in SCHOOLS:
        if re.search(rf"\b{re.escape(s)}\b", rest):
            if s.lower() not in src_l:
                probs.append(f"school {s}")
            rest = re.sub(rf"\b{re.escape(s)}\b", " ", rest)
    allowed_names = src_l + " " + spec["sender"].lower() + " " + (spec.get("sender_nick") or "").lower()
    allowed_names += " " + " ".join(nick.lower() for team, nick in MEMBERS.items() if team.lower() in allowed_names)
    for nm in NAMES:
        if re.search(rf"\b{re.escape(nm)}\b", rest) and nm.lower() not in allowed_names:
            if nm in ("Chuck", "Chuck P") and spec["kind"] == "chuck_mentioned":
                continue
            probs.append(f"name {nm}")
    if BANNED.search(reply):
        probs.append("banned")
    if re.search(r"cereal", reply, re.I) and not re.search(r"cereal", ex["message"], re.I):
        probs.append("cereal")
    if spec["sender"] == "Chuck P" and WORSHIP.search(reply):
        probs.append("worship_self")
    if spec["kind"] == "chuck_mentioned" and not WORSHIP.search(reply):
        probs.append("no_title")
    words = len(reply.split())
    sents = len(SENT.split(reply))
    if spec["chat"] == "group" and (sents > 3 or words > 70):
        probs.append(f"long {sents}s/{words}w")
    if spec["chat"] == "dm" and words > 95:
        probs.append(f"long {words}w")
    if len(SWEAR.findall(reply)) > 1:
        probs.append("swears")
    return probs


def main():
    clean, rej = [], []
    for f in sorted((DATA / "raw").glob("b*.json")):
        for ex in json.loads(f.read_text()):
            spec = specs.get(ex.get("id"))
            if not spec:
                continue
            p = check(spec, ex)
            if spec.get("message"):
                ex["message"] = spec["message"]  # a harvested real text stays verbatim
            row = {**spec, **{k: ex.get(k) for k in ("recent", "message", "reply")}}
            (rej if p else clean).append({**row, "problems": p} if p else row)
    # opener diversity: cap any 2-word opener at 2% of the set
    cnt, kept = Counter(), []
    cap = max(4, len(clean) // 50)
    for r in clean:
        op = " ".join(re.findall(r"[a-z']+", r["reply"].lower())[:2])
        cnt[op] += 1
        if cnt[op] > cap:
            rej.append({**r, "problems": [f"opener '{op}'"]})
        else:
            kept.append(r)
    (DATA / "clean.jsonl").write_text("".join(json.dumps(r) + "\n" for r in kept))
    (DATA / "rejects.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rej))
    sw = sum(bool(SWEAR.search(r["reply"])) for r in kept)
    print(f"clean {len(kept)}  rejected {len(rej)}  swear-rate {sw / max(1, len(kept)):.2f}")
    print(Counter(p.split()[0] for r in rej for p in r["problems"]).most_common())
    print("top openers", Counter(" ".join(re.findall(r"[a-z']+", r["reply"].lower())[:2]) for r in kept).most_common(8))
    print("kinds", Counter(r["kind"] for r in kept).most_common())


if __name__ == "__main__":
    main()
