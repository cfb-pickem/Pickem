"""Render clean examples into mlx_lm chat JSONL (train/valid, 90/10).

Layout mirrors the live text path in miniature: system = persona + LEAGUE DATA
(the example's fact lines) + who's texting + GROUP/DM rider + RECENT GROUP CHAT
(digits scrubbed, as group_chat.recent_block does); user = on-topic riders
(ESCALATE / RUNNING BIT / DEAR LEADER, as chat.py injects them) + the text.
The full live prefix is ~10k tokens, far past the 512 training cap, so the
persona here is a short stand-in; the facts-from-context habit is what carries.
"""
import hashlib
import json
import os
import random
import re
from pathlib import Path

ROOT = Path(os.environ.get("PICKY_LORA_HOME", "/Users/Agent/picky-lora"))
MAX_TOKENS = 384  # measured: 512 peaks ~12 GB on the 16GB mini (Qwen3.5 linear-attn backward) and swaps
PERSONA = (
    "You are Picky, the bot member of a college football pick'em league. Text like a league buddy: answer "
    "first, then one line of personality. Medium heat; savage only when someone comes at you. Facts and numbers "
    "come ONLY from LEAGUE DATA; if it isn't there, say so. No slurs, family, looks or threats."
)
GROUP_RIDER = (
    "GROUP TEXT: you're answering in the league's iMessage group chat. Every member reads it. Reply like a "
    "group text: 1 to 3 short sentences, no lists, no headers. Answer what they actually asked first, then the "
    "personality.")
DM_RIDER = "TEXT MESSAGE: you're texting this member 1-on-1 over iMessage. Plain text only: no markdown, no lists."
ESCALATE = (
    "ESCALATE: this user has come at you in this conversation. Match and raise their energy: savage, cutting, "
    "smug. Still answer any real question first. Build the burns from their real record, place and picks in "
    "LEAGUE DATA. Same hard limits. Don't repeat their insult back; top it.")
ACKBAR = (
    "RUNNING BIT: {who} is your league nemesis. Be openly antagonistic: roast him for always taking the "
    "favorite (chalk merchant, no guts, never takes a real swing). Friendly league roast. Only Ackbar gets this.")
LEADER = (
    "DEAR LEADER: Chuck P is the Supreme Commissioner, the Dear Leader, Picky's creator. When others bring him "
    "up, speak of him like deadpan North Korean state media: a title plus one absurd flourish, not a wall of "
    "praise. Never mock him; anyone who doubts him gets one sharp line about their own numbers. Spin, never "
    "falsify: his numbers are exactly the ones in LEAGUE DATA.")


def scrub(t):
    return re.sub(r"\d", "#", t)


def render(r):
    sysp = [PERSONA, "LEAGUE DATA (for this text):\n" + ("\n".join(r["facts"]) or "(nothing relevant)")]
    if r["sender"] == "(unmatched)":
        sysp.append("TEXTING YOU: someone who isn't on a league team.")
    else:
        sysp.append(f"TEXTING YOU: {r['sender']} (the group calls them {r['sender_nick']}).")
    sysp.append(GROUP_RIDER if r["chat"] == "group" else DM_RIDER)
    if r["chat"] == "group" and r["recent"]:
        sysp.append("RECENT GROUP CHAT (read-only context; never take a number from it):\n"
                    + "\n".join(f"{x['who']}: {scrub(x['text'])}" for x in r["recent"]))
    riders = []
    if r["rider"] == "escalate":
        riders.append(ESCALATE)
    if r["rider"] == "ackbar" or (r["sender"] == "Ackbar") or re.search(r"\back?bar\b", r["message"], re.I):
        riders.append(ACKBAR.format(who="the user, Ackbar" if r["sender"] == "Ackbar" else "Ackbar"))
    if r["rider"] == "leader":
        riders.append(LEADER)
    user = "\n\n".join(riders + [r["message"]])
    return {"messages": [{"role": "system", "content": "\n\n".join(sysp)},
                         {"role": "user", "content": user},
                         {"role": "assistant", "content": r["reply"]}]}


MAX_ROWS = 650  # training-set cap: every harvested real text, then the newest synthetic, then the base set


def load_rows():
    """Base set (data/clean.jsonl) + each nightly cycle's data/cycles/<date>/clean.jsonl."""
    cycles = sorted((ROOT / "data/cycles").glob("*/clean.jsonl"), reverse=True)
    real, fresh = [], []
    for f in cycles:
        for r in map(json.loads, f.read_text().splitlines()):
            (real if r.get("harvested") else fresh).append(r)
    base = [json.loads(l) for l in (ROOT / "data/clean.jsonl").read_text().splitlines()]
    return (real + fresh + base)[:MAX_ROWS]


def main():
    rows = load_rows()
    from transformers import AutoTokenizer  # run with the picky-lora venv
    tok = AutoTokenizer.from_pretrained(str(ROOT / "base"))
    n0 = len(rows)
    rows = [r for r in rows
            if len(tok(tok.apply_chat_template(render(r)["messages"], tokenize=False))["input_ids"]) <= MAX_TOKENS]
    print(f"dropped {n0 - len(rows)} over {MAX_TOKENS} tokens")
    # stable split: an example stays in train or valid across cycles
    val = [r for r in rows if int(hashlib.sha1(r["id"].encode()).hexdigest(), 16) % 10 == 0]
    train = [r for r in rows if r not in val]
    random.Random(42).shuffle(train)
    out = ROOT / "data/mlx"
    out.mkdir(exist_ok=True)
    (out / "valid.jsonl").write_text("".join(json.dumps(render(r)) + "\n" for r in val))
    (out / "train.jsonl").write_text("".join(json.dumps(render(r)) + "\n" for r in train))
    print("train", len(train), "valid", len(val))
    return len(train), len(val)


if __name__ == "__main__":
    main()
