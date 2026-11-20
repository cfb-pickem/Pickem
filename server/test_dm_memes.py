"""
DM memes (server/dm_memes.py + the group_chat.group() hook): a flagged member's 1-on-1 DM replies carry a meme
GIF after the unchanged answer, never the same one twice in a row; the league group and the site widget never
get one; the off switch works with no restart; an unreadable file falls back to quote + link. No model, no
network, temp files only.

Run:  ./.venv/bin/python3 -I test_dm_memes.py
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
TMP = Path(tempfile.mkdtemp())
os.environ["PICKY_DM_MEMES"] = str(TMP / "dm_memes.json")
os.environ["PICKY_DM_MEMES_STATE"] = str(TMP / "dm_memes_state.json")
os.environ["PICKY_MEMES_DIR"] = str(TMP / "memes")
os.environ.setdefault("PICKY_CALLBACKS_STATE", str(TMP / "callbacks.json"))
os.environ.setdefault("PICKY_NO_REPEAT_STATE", str(TMP / "no_repeat.json"))
import dm_memes  # noqa: E402

fails = 0
FLAGGED, OTHER = 903, 904  # team_ids (made up)
GIF = b"GIF89a" + b"\0" * 16


def check(cond, msg):
    global fails
    print(("ok   " if cond else "FAIL ") + msg)
    fails += not cond


def setup(n: int = 15, on: bool = True) -> None:
    pool = TMP / "memes" / "itysl"
    pool.mkdir(parents=True, exist_ok=True)
    memes = []
    for i in range(n):
        (pool / f"m{i}.gif").write_bytes(GIF + bytes([i]))
        memes.append({"file": f"m{i}.gif", "quote": f"Quote {i}.", "url": f"https://giphy.com/gifs/m{i}"})
    (pool / "pool.json").write_text(json.dumps({"memes": memes}))
    dm_memes.CONFIG.write_text(json.dumps({"members": {str(FLAGGED): {"team": "Flagged Guy", "pool": "itysl",
                                                                       "on": on}}}))
    dm_memes.STATE.unlink(missing_ok=True)


# ---- pick / attach ------------------------------------------------------------------------------------------
setup()
m = dm_memes.pick(FLAGGED)
check(m is not None and base64.b64decode(m["image"]).startswith(b"GIF89a"), "flagged member: a GIF from the pool")
check(dm_memes.pick(OTHER) is None and dm_memes.pick(None) is None, "anyone else: nothing")

seq = [dm_memes.pick(FLAGGED)["file"] for _ in range(300)]
check(all(a != b for a, b in zip(seq, seq[1:])), "300 picks: never the same meme back to back")
check(len(set(seq)) == 15, f"rotates through the whole pool ({len(set(seq))}/15 seen)")

last = json.loads(dm_memes.STATE.read_text())[str(FLAGGED)]
for _ in range(20):
    dm_memes.pick(FLAGGED, record=False)
check(json.loads(dm_memes.STATE.read_text())[str(FLAGGED)] == last, "test traffic leaves the no-repeat state alone")

out = {"reply": "Iowa's 5-1 against the number at home.", "image": None, "lane": "fact"}
got = dm_memes.attach(out, FLAGGED, dm=True)
check(got["reply"] == out["reply"], "DM: the answer text is untouched (helpful first)")
check(bool(got["image"]) and got["image_alt"].startswith('"Quote ') and "giphy.com/gifs/" in got["image_alt"],
      "DM: meme rides as the image, quote + link as image_alt")
check(dm_memes.attach(out, FLAGGED, dm=False) == out, "GROUP: attach is a no-op")
check(dm_memes.attach(out, OTHER, dm=True) == out, "unflagged member's DM: no-op")
check(dm_memes.attach({"reply": None, "image": None}, FLAGGED, dm=True)["image"] is None, "no reply: no meme")
pic = {"reply": "Here you go.", "image": "cGljdHVyZQ=="}
check(dm_memes.attach(pic, FLAGGED, dm=True) == pic, "a picture reply keeps its picture")
with patch.object(dm_memes, "pick", side_effect=RuntimeError("boom")):
    check(dm_memes.attach(out, FLAGGED, dm=True) == out, "a meme failure never breaks the reply")

# unreadable file -> quote on its own line + link, in the text
setup(n=1)
(TMP / "memes" / "itysl" / "m0.gif").unlink()
got = dm_memes.attach(out, FLAGGED, dm=True)
check(got["image"] is None and got["reply"] == out["reply"] + '\n\n"Quote 0."\nhttps://giphy.com/gifs/m0',
      f"missing file: text fallback after the answer ({got['reply'][-60:]!r})")

# ---- the toggle ---------------------------------------------------------------------------------------------
setup()
check(dm_memes.main(["off", "flagged"]) == 0 and dm_memes.pick(FLAGGED) is None, "'off flagged' stops it, no restart")
check(dm_memes.main(["on", str(FLAGGED)]) == 0 and dm_memes.pick(FLAGGED) is not None, "'on <team_id>' turns it back on")
check(dm_memes.main(["off", "nobody"]) == 1, "unknown name: refused, nothing changed")
check(dm_memes.main(["status"]) == 0, "status runs")

# ---- the /group endpoint: DM gets it, the league GROUP never does, /web never does ---------------------------
try:
    import group_chat as gc
except Exception as e:  # noqa: BLE001
    print("FAIL could not import group_chat:", e)
    fails += 1
else:
    GROUP = "any;+;league-test"
    ANSWER = "Iowa covers, they're 5-1 ATS at home."

    class Req:
        def __init__(self, body, headers=None):
            self.headers = {"x-picky-group-secret": "s", **(headers or {})}
            self._b = body

        async def json(self):
            return self._b

    async def fake_answer(handle, text, chat_id="", dm=False, **k):
        return {"reply": ANSWER, "image": None, "mode": "dm" if dm else "group", "lane": "fact"}

    flagged = gc.Sender(handle="+15555550100", team="Flagged Guy", team_id=FLAGGED, user_id="u1")
    common = [patch.object(gc, "SECRET", "s"), patch.object(gc, "answer", fake_answer),
              patch.object(gc.maintenance, "active", return_value=False),
              patch.object(gc.group_outbox, "config", return_value={"group_guid": GROUP}),
              patch.object(gc.callbacks, "hook", lambda *a, **k: None),
              patch.object(gc.gameday, "short_for", return_value=False),
              patch.object(gc.VIEWER, "sender", return_value=flagged)]
    for p in common:
        p.start()
    try:
        setup()
        g = asyncio.run(gc.group(Req({"handle": "+15555550100", "text": "@Picky who covers?", "chat": GROUP})))
        check(g["reply"] == ANSWER and g["image"] is None and "image_alt" not in g,
              "GROUP (league chat, flagged member asking): same reply, no meme, no image")
        g2 = asyncio.run(gc.group(Req({"handle": "+15555550100", "text": "@Picky you sure?", "chat": GROUP})))
        check(g2["image"] is None and not dm_memes.STATE.exists(), "GROUP again: still nothing, no meme state touched")
        d = asyncio.run(gc.group(Req({"handle": "+15555550100", "text": "who covers?", "chat": "any;-;+15555550100",
                                      "dm": True})))
        check(d["reply"] == ANSWER, "DM: answer first, unchanged")
        check(bool(d["image"]) and base64.b64decode(d["image"]).startswith(b"GIF89a") and "giphy.com" in d["image_alt"],
              "DM: the meme GIF comes back as the image (+ image_alt)")
        d2 = asyncio.run(gc.group(Req({"handle": "+15555550100", "text": "and Utah?", "chat": "any;-;+15555550100",
                                       "dm": True})))
        check(d2["image"] and d2["image"] != d["image"], "DM: next reply, a different meme")
        t = asyncio.run(gc.group(Req({"handle": "+15555550100", "text": "who covers?", "chat": "any;-;+15555550100",
                                      "dm": True}, headers={"x-picky-test": "1"})))
        check(bool(t["image"]), "DM test request (dry run): shows the meme too")
        with patch.object(gc.VIEWER, "sender", return_value=gc.Sender(handle="+15555550101", team="Other",
                                                                      team_id=OTHER, user_id="u2")):
            o = asyncio.run(gc.group(Req({"handle": "+15555550101", "text": "who covers?", "chat": "any;-;+15555550101",
                                          "dm": True})))
        check(o["image"] is None and "image_alt" not in o, "another member's DM: no meme")
        dm_memes.main(["off", "flagged"])
        d3 = asyncio.run(gc.group(Req({"handle": "+15555550100", "text": "who covers?", "chat": "any;-;+15555550100",
                                       "dm": True})))
        check(d3["image"] is None and d3["reply"] == ANSWER, "switched off: plain DM reply")
        dm_memes.main(["on", "flagged"])

        # the site widget (POST /web) runs the DM pipeline but never gets a meme
        calls = []

        async def verify(_c, _jwt):
            return "u1"

        async def session(_c):
            return "jwt"

        with patch.object(gc, "_orig_verify_jwt", verify), patch.object(gc.VIEWER, "session", session), \
                patch.object(gc, "web_sender", return_value=flagged), \
                patch.object(gc.dm_memes, "attach", side_effect=lambda *a, **k: calls.append(a) or a[0]):
            wr = asyncio.run(gc.web(Req({"supabase_jwt": "j", "messages": [{"role": "user", "content": "who covers?"}]})))
        check(wr["reply"] == ANSWER and "image" not in wr and not calls, "SITE CHAT (/web): no meme, attach never called")
    finally:
        for p in common:
            p.stop()

print("ALL PASS" if not fails else f"{fails} FAILED")
sys.exit(1 if fails else 0)
