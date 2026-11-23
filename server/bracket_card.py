#!/usr/bin/env python3
"""Picky's CFP bracket as a dark-mode PNG (bcs.py's bracket, drawn in code; no logos, system fonts).

Layout (1200 px wide): title, the four first-round games on the left in bracket order (8v9 -> 1, 5v12 -> 4,
6v11 -> 3, 7v10 -> 2, so each winner's line runs into its bye team), the bye seeds in the quarterfinal column,
"First 4 out" footer. Each slot: seed, team, record, and an auto-bid tag (SEC / G6 / top 12 ...).

Pillow lives in the image service's venv (.venv-flux), not the chat venv: png() draws in-process when PIL imports,
else runs this file under PICKY_PIL_PY (default .venv-flux's python) with the spec on stdin, PNG on stdout.

  bracket_card.py < spec.json > out.png
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PIL_PY = Path(os.environ.get("PICKY_PIL_PY", HERE / ".venv-flux" / "bin" / "python3"))
W = 1200
FONT = "/System/Library/Fonts/Supplemental/Arial.ttf"
FONT_B = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"
BG, SLOT, BYE, LINE = "#0f1115", "#1d2129", "#232a36", "#4a5160"
TEXT, DIM, SEED, TAG_BG, TAG_FG = "#eceff4", "#9aa3b2", "#7fb3ff", "#1f6f43", "#e9fff2"
QF_ORDER = (1, 4, 3, 2)  # top to bottom: 1 and 2 in opposite halves


def spec(b: dict, records: dict[str, str], week, rules: dict) -> dict:
    """The drawing spec from bcs.compute()'s bracket: plain JSON (it crosses a process boundary)."""
    seeds = {int(s): t for s, t in b["seeds"]}
    qf = {q[0]: q for q in rules["quarterfinals"]}
    tags = {}
    for t, why in (b.get("auto") or {}).items():
        tags[t] = "G6 auto" if why == "G6" else ("ND top 12" if why.startswith("top ") else f"{why} champ")
    return {"title": f"Picky's bracket, BCS formula, wk {week}",
            "games": [{"bye": qf[s][0], "a": qf[s][1], "b": qf[s][2]} for s in QF_ORDER if s in qf],
            "seeds": {str(k): v for k, v in seeds.items()}, "records": records, "tags": tags,
            "first_out": [{"team": t, "record": records.get(t, "")} for t in b.get("first_out") or []]}


def draw(sp: dict) -> bytes:
    from PIL import Image, ImageDraw, ImageFont

    def font(size, bold=False):
        try:
            return ImageFont.truetype(FONT_B if bold else FONT, size)
        except OSError:
            return ImageFont.load_default(size)

    f_title, f_team, f_seed, f_rec, f_tag, f_hdr = (font(34, True), font(28, True), font(28, True), font(22),
                                                    font(18, True), font(20, True))
    slot_h, gap, top = 58, 34, 130
    games = sp["games"]
    h = top + len(games) * (2 * slot_h + gap) + 120
    im = Image.new("RGB", (W, h), BG)
    d = ImageDraw.Draw(im)
    d.text((40, 36), sp["title"], font=f_title, fill=TEXT)
    d.text((40, 92), "FIRST ROUND", font=f_hdr, fill=DIM)
    d.text((660, 92), "QUARTERFINALS (BYES)", font=f_hdr, fill=DIM)
    seeds, recs, tags = sp["seeds"], sp["records"], sp["tags"]

    def slot(x, y, w, seed, fill):
        team = seeds.get(str(seed), "TBD")
        d.rounded_rectangle((x, y, x + w, y + slot_h - 6), radius=10, fill=fill)
        d.text((x + 16, y + 11), str(seed), font=f_seed, fill=SEED)
        tag = tags.get(team)
        right = x + w - 14
        if tag:
            tw = d.textlength(tag, font=f_tag) + 18
            d.rounded_rectangle((right - tw, y + 13, right, y + 39), radius=8, fill=TAG_BG)
            d.text((right - tw + 9, y + 16), tag, font=f_tag, fill=TAG_FG)
            right -= tw + 10
        rec = recs.get(team, "")
        rw = d.textlength(rec, font=f_rec)
        d.text((right - rw, y + 15), rec, font=f_rec, fill=DIM)
        name, room = team, right - rw - 12 - (x + 64)
        while d.textlength(name, font=f_team) > room and len(name) > 4:
            name = name[:-2].rstrip() + "."
        d.text((x + 64, y + 11), name, font=f_team, fill=TEXT)

    x1, w1, x2, w2 = 40, 520, 660, 500
    for i, g in enumerate(games):
        y = top + i * (2 * slot_h + gap)
        slot(x1, y, w1, g["a"], SLOT)
        slot(x1, y + slot_h, w1, g["b"], SLOT)
        mid = y + slot_h - 3
        yb = mid - (slot_h - 6) // 2
        d.line((x1 + w1, y + (slot_h - 6) // 2, x1 + w1 + 30, y + (slot_h - 6) // 2), fill=LINE, width=3)
        d.line((x1 + w1, y + slot_h + (slot_h - 6) // 2, x1 + w1 + 30, y + slot_h + (slot_h - 6) // 2), fill=LINE,
               width=3)
        d.line((x1 + w1 + 30, y + (slot_h - 6) // 2, x1 + w1 + 30, y + slot_h + (slot_h - 6) // 2), fill=LINE,
               width=3)
        d.line((x1 + w1 + 30, mid, x2, mid), fill=LINE, width=3)
        slot(x2, yb, w2, g["bye"], BYE)
    y = top + len(games) * (2 * slot_h + gap) + 6
    d.line((40, y, W - 40, y), fill=LINE, width=2)
    out = ", ".join(f"{x['team']} ({x['record']})" if x.get("record") else x["team"] for x in sp["first_out"])
    label = f"First {len(sp['first_out'])} out: "
    d.text((40, y + 24), label, font=f_hdr, fill=DIM)
    d.text((40 + d.textlength(label, font=f_hdr), y + 22), out, font=f_rec, fill=TEXT)
    d.text((40, y + 62), "Projected. Champs tagged; bracket by Picky's BCS formula (AP, Coaches, SP+, FPI, Elo, SRS).",
           font=font(18), fill=DIM)
    buf = io.BytesIO()
    im.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def png(sp: dict, timeout: float = 30.0) -> bytes | None:
    """The PNG bytes, or None (no Pillow anywhere, a draw error): the caller sends the text bracket instead."""
    try:
        return draw(sp)
    except ImportError:
        pass
    if not PIL_PY.exists():
        return None
    try:
        r = subprocess.run([str(PIL_PY), "-I", str(Path(__file__).resolve())], input=json.dumps(sp).encode(),
                           capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout if r.returncode == 0 and r.stdout[:8] == b"\x89PNG\r\n\x1a\n" else None


if __name__ == "__main__":
    sys.stdout.buffer.write(draw(json.loads(sys.stdin.read())))
