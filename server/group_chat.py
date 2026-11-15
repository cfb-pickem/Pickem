"""
Picky in the league's iMessage group chat: a localhost-only sidecar.

  POST /group  {"handle": "+1...", "text": "@Picky who's winning?"}
               header X-Picky-Group-Secret: <keychain drake / CFB_PICKEM__PICKY_GROUP_SECRET>
               -> {"reply": "..."}
  GET  /health -> {"ok": true}

The watcher in the `picky` macOS user (tools/picky-imessage/) calls this. It
runs chat.py's own /chat pipeline in-process (imported, never edited, so
com.pickem.chat needs no restart) with these group-chat changes:

  PRIVACY. The group is public to every member. Supabase reads use Picky the
  player's own session, which has no visible league team, so
  filter_visible_picks keeps only picks the site shows any member about
  another member (locked games). The sender's own unlocked picks never enter
  the context, and their submission status is dropped from the you-block.
  The privacy invariant is asserted on every request.

  PERSONA. ctx["you"] is set to the sender's team (from the private handle
  map), so the Blinkstradamus opener, Ackbar nemesis, DaddyFreeman, Dear
  Leader and every other caller-keyed bit fire as on the site.

  FORMAT. A group-text rider plus a hard trim: 1-3 sentences.

  DMs (dm=true from the watcher, no @Picky needed). A mapped league member
  gets the website exactly as if logged in: a real session for THEIR user is
  minted (same magic-link mint as Picky's) and every hook below steps aside,
  so chat.py runs unmodified with their picks and private data. An unmapped
  sender the watcher allowlisted gets the public (teamless) group context.

  HISTORY. Each chat (group guid, or DM handle) keeps a rolling history, the
  last chat.MAX_HISTORY_TURNS turns within HISTORY_TTL, in a private 0600 file
  that survives restarts. Group and DM histories never mix.

  PICTURES. chat.py's picture route runs as on the site; the image comes back
  as base64 and the watcher sends it as an attachment.

The handle map is private: /Users/Agent/cfb-pickem-private/picky_group_members.json
(never in this public repo). Unknown senders are a generic league member.
"""

from __future__ import annotations

import asyncio
import contextvars
import hmac
import json
import logging
import os
import plistlib
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
# Same model settings as the live service, so group answers match the site.
_PLIST = Path.home() / "Library/LaunchAgents/com.pickem.chat.plist"
if _PLIST.exists():
    for _k, _v in (plistlib.loads(_PLIST.read_bytes()).get("EnvironmentVariables") or {}).items():
        if _k != "PORT":
            os.environ.setdefault(_k, str(_v))

import httpx  # noqa: E402
from fastapi import FastAPI, HTTPException, Request  # noqa: E402
from starlette.requests import Request as StarletteRequest  # noqa: E402

import chat  # noqa: E402

log = logging.getLogger("picky-group")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

PORT = int(os.environ.get("PICKY_GROUP_PORT", "8768"))
SECRET_SERVICE = "CFB_PICKEM__PICKY_GROUP_SECRET"
SERVICE_KEY_SERVICE = "CFB_PICKEM__SUPABASE_SERVICE_KEY"
MEMBERS_FILE = Path(os.environ.get(
    "PICKY_GROUP_MEMBERS", "/Users/Agent/cfb-pickem-private/picky_group_members.json"))
HISTORY_FILE = Path(os.environ.get(
    "PICKY_IMESSAGE_HISTORY", "/Users/Agent/cfb-pickem-private/picky_imessage_history.json"))
HISTORY_TTL = 2 * 3600  # seconds; older turns drop out of a chat's history
DM_MAX_CHARS = 1500
MAX_SENTENCES = 3
MAX_CHARS = 480
MAX_TRIES = 3          # rerolls when chat.py's sentence filter cut part of an answer
REROLL_BUDGET = 120.0  # seconds; the watcher gives up at 240

GROUP_RIDER = (
    "\n\nGROUP TEXT: you're answering in the league's iMessage group chat. Every member reads "
    "it. Reply like a group text: 1 to 3 short sentences, no lists, no headers. Never reveal "
    "anyone's picks for games that haven't kicked off, whether anyone has made their picks yet, "
    "or anyone's phone number or email; if asked, say picks unlock at kickoff."
)
DM_RIDER = ("\n\nTEXT MESSAGE: you're texting this member 1-on-1 over iMessage. Plain text only: "
            "no markdown, no headers, no bullet lists.")
PUBLIC_DM_RIDER = (
    "\n\nTEXT MESSAGE: you're texting 1-on-1 over iMessage with someone who isn't matched to a "
    "league team. Plain text, no markdown. Never reveal anyone's picks for games that haven't "
    "kicked off, whether anyone has made their picks yet, or anyone's phone number or email; if "
    "asked, say picks unlock at kickoff."
)


def _keychain(service: str) -> str | None:
    r = subprocess.run(["security", "find-generic-password", "-a", "drake", "-s", service, "-w"],
                       capture_output=True, text=True, timeout=10)
    return r.stdout.strip() or None


# -----------------------------------------------------------------------------
# trigger + handles (the watcher has its own copy of the trigger; both tested)
# -----------------------------------------------------------------------------

TRIGGER = re.compile(r"(?<![\w@])@\s?picky(?!\w)", re.I)


def strip_trigger(text: str) -> str:
    out = re.sub(r"\s+", " ", TRIGGER.sub(" ", text or ""))
    return re.sub(r"\s+([,.!?;:])", r"\1", out).strip(" ,:;-") or "hey"


def norm_handle(h: str) -> str:
    h = (h or "").strip()
    if h.lower().startswith(("mailto:", "tel:")):
        h = h.split(":", 1)[1]
    if "@" in h:
        return h.lower()
    d = re.sub(r"\D", "", h)
    if len(d) == 10:
        d = "1" + d
    return "+" + d if d else ""


@dataclass
class Sender:
    handle: str
    team: str | None = None
    team_id: int | None = None
    user_id: str | None = None
    name: str | None = None


_members_cache: tuple[float, dict] = (0.0, {})


def load_members() -> dict[str, dict]:
    """handle -> {"team", "name"} from the private map (re-read when it changes)."""
    global _members_cache
    try:
        mtime = MEMBERS_FILE.stat().st_mtime
    except OSError:
        return {}
    if mtime == _members_cache[0]:
        return _members_cache[1]
    try:
        data = json.loads(MEMBERS_FILE.read_text())
    except (OSError, ValueError) as e:
        log.warning("members map unreadable: %s", e)
        return _members_cache[1]
    out = {}
    for m in data.get("members") or []:
        for h in m.get("handles") or []:
            if norm_handle(h):
                out[norm_handle(h)] = {"team": m.get("team"), "name": m.get("name") or None}
    _members_cache = (mtime, out)
    return out


# -----------------------------------------------------------------------------
# Supabase: Picky the player's session (no visible league team) for reads
# -----------------------------------------------------------------------------


class Viewer:
    def __init__(self):
        self.key = None
        self.uid = None
        self.jwt, self.exp = None, 0.0
        self.teams: dict[str, dict] = {}
        self.teams_at = 0.0
        self.members: dict[str, tuple[str, float]] = {}

    def _h(self):
        if not self.key:
            self.key = _keychain(SERVICE_KEY_SERVICE)
            if not self.key:
                raise HTTPException(status_code=500, detail="no_service_key")
        return {"apikey": self.key, "Authorization": f"Bearer {self.key}"}

    async def refresh_teams(self, c: httpx.AsyncClient) -> None:
        if time.time() - self.teams_at < 600 and self.teams:
            return
        r = await c.get(f"{chat.REST}/teams?select=team_id,team_name,user_id,is_exhibition",
                        headers=self._h(), timeout=10)
        r.raise_for_status()
        rows = r.json()
        self.teams = {t["team_name"].lower(): t for t in rows if not t["is_exhibition"]}
        picky = next((t for t in rows if t["is_exhibition"]), None)
        if not picky or not picky.get("user_id"):
            raise HTTPException(status_code=500, detail="no_picky_player")
        self.uid = picky["user_id"]
        self.teams_at = time.time()

    async def _mint(self, c: httpx.AsyncClient, uid: str) -> str:
        """A real (RLS-bound) JWT for one user. Same magic-link mint as
        eval/run.py; generate_link sends no email."""
        h = self._h()
        email = (await c.get(f"{chat.AUTH}/admin/users/{uid}", headers=h, timeout=15)).json()["email"]
        link = (await c.post(f"{chat.AUTH}/admin/generate_link", headers=h,
                             json={"type": "magiclink", "email": email}, timeout=15)).json()
        tok = (link.get("properties") or link)["hashed_token"]
        s = (await c.post(f"{chat.AUTH}/verify", headers={"apikey": chat.SUPABASE_ANON},
                          json={"type": "magiclink", "token_hash": tok}, timeout=15)).json()
        return s["access_token"]

    async def session(self, c: httpx.AsyncClient) -> str:
        """Picky the player's session (no visible league team), cached ~50 min."""
        await self.refresh_teams(c)
        if self.jwt and time.time() < self.exp:
            return self.jwt
        self.jwt, self.exp = await self._mint(c, self.uid), time.time() + 3000
        return self.jwt

    async def member_session(self, c: httpx.AsyncClient, uid: str) -> str:
        """A mapped member's own session, for their 1-on-1 DMs only (~50 min cache)."""
        hit = self.members.get(uid)
        if hit and time.time() < hit[1]:
            return hit[0]
        jwt = await self._mint(c, uid)
        self.members[uid] = (jwt, time.time() + 3000)
        return jwt

    def sender(self, handle: str) -> Sender:
        h = norm_handle(handle)
        m = load_members().get(h) or {}
        t = self.teams.get((m.get("team") or "").lower())
        if not t:
            return Sender(handle=h, name=m.get("name"))
        return Sender(handle=h, team=t["team_name"], team_id=t["team_id"], user_id=t["user_id"],
                      name=m.get("name"))


VIEWER = Viewer()
SENDER: contextvars.ContextVar[Sender | None] = contextvars.ContextVar("picky_group_sender", default=None)
# "group" | "dm" (a mapped member's DM: the site, unmodified) | "dm-public"
MODE: contextvars.ContextVar[str] = contextvars.ContextVar("picky_group_mode", default="group")


def _site() -> bool:
    """True in a mapped member's DM: every hook defers to chat.py as is."""
    return MODE.get() == "dm"


# -----------------------------------------------------------------------------
# hooks into chat.py (this process only)
# -----------------------------------------------------------------------------

_orig_verify_jwt = chat._verify_jwt
_orig_build_context = chat._build_context
_orig_you_block = chat.you_block
_orig_system_tail = chat._system_tail
_orig_proactive_note = chat.proactive_note
_orig_self_intrusion = chat.self_intrusion_off_topic


async def _verify_jwt(_client, _jwt) -> str:
    if _site():
        return await _orig_verify_jwt(_client, _jwt)
    s = SENDER.get()
    if s is None:  # only ever called from /group
        raise HTTPException(status_code=401, detail="no_sender")
    return s.user_id or f"group:{s.handle or 'unknown'}"


async def _build_context(client, jwt, user_id, mode="leaderboard", rival_predictions=None):
    if _site():
        return await _orig_build_context(client, jwt, user_id, mode=mode, rival_predictions=rival_predictions)
    ctx = await _orig_build_context(client, jwt, VIEWER.uid, mode="leaderboard", rival_predictions=None)
    if ctx.get("error"):
        return ctx
    # privacy invariant: the reads ran as nobody's team, so only locked picks are in here
    assert ctx["you"]["team_id"] is None, "group context must be built teamless"
    unlocked = {g["id"] for g in ctx["games_this_week"]} - set(ctx["revealed_game_ids"])
    for plist in ctx["revealed_picks_this_week"].values():
        assert not any(p["game_id"] in unlocked for p in plist), "unrevealed pick in group context"
    s = SENDER.get()
    if s and s.team:
        ctx["you"] = {"team_id": s.team_id, "team_name": s.team}
    ctx["you_have_picks_this_week"] = None
    return ctx


def _you_block(ctx: dict) -> str:
    if _site():
        return _orig_you_block(ctx)
    out = _orig_you_block(ctx)
    out = out.replace("'s pick: not made yet", "'s pick: hidden until kickoff (never say if it's made)")
    return "\n".join(ln for ln in out.split("\n") if not re.search(r" has (NOT )?made picks this week\.$", ln))


def _system_tail(ctx: dict) -> str:
    if _site():
        return _orig_system_tail(ctx) + DM_RIDER
    if MODE.get() == "dm-public":
        return _orig_system_tail(ctx) + PUBLIC_DM_RIDER + (
            " This person isn't matched to a team: treat them as a league member and don't guess who.")
    who = ""
    s = SENDER.get()
    if s and s.team and s.name:
        who = f" In the group, \"{s.team}\" goes by {s.name}."
    elif not (s and s.team):
        who = " This group member isn't matched to a team: treat them as a league member and don't guess who."
    return _orig_system_tail(ctx) + GROUP_RIDER + who


chat._verify_jwt = _verify_jwt
chat._build_context = _build_context
chat.you_block = _you_block
chat._system_tail = _system_tail
# Pictures run as on the site (the watcher sends the image as an attachment).
# The site's every-other-question "fact about the user" hook bolted the
# sender's own week onto group answers ("You're 4-0, Charlie"), even on a game
# question. Off in the group; on in a member's DM, as on the site.
chat.proactive_note = lambda *a, **k: _orig_proactive_note(*a, **k) if _site() else ""

# chat.py hands the model the sender's own graded week ("Say 'you're 4-0'")
# on every results question, then its self-intrusion filter drops exactly that
# sentence when the question isn't about the sender. 'roast Ackbar' opened
# "You're 4-0, while he's 3-1..." every time, lost it, and went out as "He
# took...". Records are public in the group, so let that sentence stand.
chat.self_intrusion_off_topic = (
    lambda *a, **k: _orig_self_intrusion(*a, **k) if MODE.get() != "group" else False)

# chat.py's sentence filter drops bad sentences one at a time. On the site a
# member can ask again; in the group what's left goes out as is, and it reads
# as nonsense ("That's not luck..." after the opener that answered the question
# was dropped). Count drops per attempt so answer() can reroll.
DROPS: contextvars.ContextVar[list | None] = contextvars.ContextVar("picky_group_drops", default=None)
_orig_sentence_ok = chat.sentence_ok


def _sentence_ok(sent, *a, **kw):
    ok = _orig_sentence_ok(sent, *a, **kw)
    d = DROPS.get()
    if not ok and d is not None:
        d.append(sent)
    return ok


chat.sentence_ok = _sentence_ok

# The results guard drops contradicting sentences on its own and then appends
# facts_sentence (or falls back to templated_answer): count those as drops too.
for _name in ("facts_sentence", "templated_answer"):
    def _counted(facts, _f=getattr(chat, _name)):
        d = DROPS.get()
        if d is not None:
            d.append(_f.__name__)
        return _f(facts)
    setattr(chat, _name, _counted)


# -----------------------------------------------------------------------------
# reply shaping
# -----------------------------------------------------------------------------

# Submission status is private (chat.py never gives it to the model), but the
# 9B will still make one up ("No, he hasn't"). Nudge on the way in, and swap
# any claim for a fixed line on the way out.
_STATUS_ASK = re.compile(r"\b(made|make|submit\w*|turn(ed)? in|lock(ed)? in|put in|enter(ed)?|done with|"
                         r"finish(ed)?)\b.{0,30}\bpicks?\b|\bpicks? (in|done|submitted|made)\b|"
                         r"\b(who|anyone|anybody|everyone|everybody)\b.{0,25}\b(picked|pick(ed)? yet|not picked)\b", re.I)
_STATUS_NUDGE = (" (You can't see whether anyone has made picks; nobody can until kickoff. Never say anyone "
                 "has or hasn't picked.)")
_STATUS_CLAIM = re.compile(r"\b(has|have|had)(n'?t| not)\b.{0,25}\b(made|submitted|picked|turned|locked|put|entered)\b|"
                           r"\b(no|yes|nope|yep),? (he|she|they|you)\b.{0,6}\b(has|have|hasn'?t|haven'?t|did|didn'?t)\b|"
                           r"\b(already|has|have|had)\b.{0,12}\b(made|submitted|turned in|locked in|put in|entered)\b"
                           r".{0,20}\bpicks?\b|\b(still )?(hasn'?t|haven'?t|didn'?t) (picked|pick)\b|"
                           r"\b(?!I\b)\w+ (has|have)( not| already| all)? (picked|submitted|turned (them|it|picks) in)\b|"
                           r"\b(nobody|no one|everyone|everybody|someone|somebody)\b.{0,15}\b(picked|picks? in)\b", re.I)
STATUS_LINE = "Who has or hasn't picked stays in the vault until kickoff. Same rules for everybody."


def status_guard(question: str, reply: str) -> str:
    if _STATUS_CLAIM.search(reply or ""):
        lead = chat.BLINKSTRADAMUS_LINE + " " if (reply or "").lower().startswith(chat.BLINKSTRADAMUS_LINE) else ""
        return lead + STATUS_LINE
    return reply

_SENT = re.compile(r"(?<=[.!?])[\"')\]]*\s+(?=\S)")


def group_trim(text: str) -> str:
    text = re.sub(r"\s+", " ", (text or "").replace("**", "")).strip()
    parts = [p for p in _SENT.split(text) if p.strip()]
    keep = MAX_SENTENCES + (1 if parts and parts[0].strip().lower() == chat.BLINKSTRADAMUS_LINE else 0)
    out = " ".join(parts[:keep]).strip()
    if len(out) > MAX_CHARS:
        cut = out[:MAX_CHARS]
        out = cut[: max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? ")) + 1] or cut.rsplit(" ", 1)[0] + "..."
    return out


def dm_trim(text: str) -> str:
    """iMessage shows markdown literally: drop it, cap the length."""
    text = re.sub(r"^\s{0,3}#{1,6}\s*", "", (text or "").replace("**", ""), flags=re.M).strip()
    if len(text) > DM_MAX_CHARS:
        cut = text[:DM_MAX_CHARS]
        text = cut[: max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? ")) + 1] or cut.rsplit(" ", 1)[0] + "..."
    return text


async def _collect(resp) -> tuple[str, str | None]:
    """(text, image data-URL or None) out of chat.py's SSE stream."""
    out, image = [], None
    async for chunk in resp.body_iterator:
        for line in (chunk if isinstance(chunk, str) else chunk.decode()).split("\n"):
            if not line.startswith("data:"):
                continue
            p = line[5:].strip()
            if p == "[DONE]":
                continue
            try:
                obj = json.loads(p)
            except ValueError:
                continue
            if obj.get("content"):
                out.append(obj["content"])
            if obj.get("image"):
                image = obj["image"]
    return "".join(out), image


# -----------------------------------------------------------------------------
# per-chat history (private file, survives restarts)
# -----------------------------------------------------------------------------

_HIST_LOCK = asyncio.Lock()


def _read_history() -> dict:
    try:
        return json.loads(HISTORY_FILE.read_text())
    except (OSError, ValueError):
        return {}


def load_history(key: str | None, now: float | None = None) -> list[dict]:
    if not key:
        return []
    now = time.time() if now is None else now
    turns = [t for t in _read_history().get(key) or [] if now - t.get("ts", 0) < HISTORY_TTL]
    turns = turns[-chat.MAX_HISTORY_TURNS * 2:]
    while turns and turns[0].get("role") != "user":
        turns = turns[1:]
    return [{"role": t["role"], "content": t["content"]} for t in turns]


async def save_turn(key: str | None, question: str, reply: str) -> None:
    if not key or not reply:
        return
    async with _HIST_LOCK:
        now = time.time()
        data = _read_history()
        turns = [t for t in data.get(key) or [] if now - t.get("ts", 0) < HISTORY_TTL]
        turns += [{"role": "user", "content": question, "ts": now},
                  {"role": "assistant", "content": reply, "ts": now}]
        data[key] = turns[-chat.MAX_HISTORY_TURNS * 2:]
        data = {k: v for k, v in data.items() if v and now - v[-1]["ts"] < HISTORY_TTL}
        HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = HISTORY_FILE.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        os.replace(tmp, HISTORY_FILE)


def history_key(chat_id: str, handle: str, dm: bool) -> str | None:
    """Group: the chat guid. DM: the sender's handle. None (no history) when
    an older watcher sends no chat id."""
    if dm:
        return "dm:" + norm_handle(handle) if norm_handle(handle) else None
    return "group:" + chat_id if chat_id else None


async def answer(handle: str, text: str, chat_id: str = "", dm: bool = False,
                 allowlisted: bool = False) -> dict | None:
    """{"reply", "image"}, or None when a DM sender isn't allowed."""
    async with httpx.AsyncClient() as c:
        await VIEWER.session(c)  # also refreshes teams + Picky's uid
        s = VIEWER.sender(handle)
        if dm and s.user_id:
            mode, jwt = "dm", await VIEWER.member_session(c, s.user_id)
        elif dm and allowlisted:
            mode, jwt, s = "dm-public", VIEWER.jwt, Sender(handle=s.handle)
        elif dm:
            return None
        else:
            mode, jwt = "group", VIEWER.jwt
    token, mtok = SENDER.set(s), MODE.set(mode)
    try:
        q = strip_trigger(text)[:2000]
        # the group calls him "blinky": tag the real team name, the way chat.py
        # tags follow-ups, so he isn't OFF-TOPIC and his results get checked
        if chat._BLINKS_ASK.search(q) and not re.search(r"blinkstradamus", q, re.I):
            q += " (Blinkstradamus)"
        q_hist = q
        if mode != "dm" and _STATUS_ASK.search(q):
            q += _STATUS_NUDGE
        key = history_key(chat_id, handle, dm)
        messages = load_history(key) + [{"role": "user", "content": q}]
        body = json.dumps({"messages": messages, "supabase_jwt": jwt, "mode": "leaderboard"}).encode()

        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}

        def shape(raw: str) -> str:
            if mode == "group":
                return group_trim(status_guard(q, raw))
            return dm_trim(raw if mode == "dm" else status_guard(q, raw))

        # keep the first answer the filter left whole; else the one it cut least
        t0, best = time.time(), None
        for i in range(MAX_TRIES):
            drops = []
            dtok = DROPS.set(drops)
            try:
                req = StarletteRequest({"type": "http", "method": "POST", "path": "/chat", "headers": [],
                                        "query_string": b""}, receive)
                raw, image = await _collect(await chat.chat(req))
                reply = shape(raw)
            except HTTPException:
                if best is None:
                    raise
                break  # a reroll hit the rate limit: send the best so far
            finally:
                DROPS.reset(dtok)
            if best is None or len(drops) < best[0]:
                best = (len(drops), reply, image)
            if image or not drops or time.time() - t0 > REROLL_BUDGET:
                break
            log.info("%s reroll %d: filter dropped %d sentence(s)", mode, i + 1, len(drops))
        _, reply, image = best
        await save_turn(key, q_hist, reply)
        return {"reply": reply, "image": image.split(",", 1)[1] if image else None, "mode": mode}
    finally:
        MODE.reset(mtok)
        SENDER.reset(token)


# -----------------------------------------------------------------------------
# app (127.0.0.1 only)
# -----------------------------------------------------------------------------

app = FastAPI(title="Picky group chat")
SECRET = os.environ.get("PICKY_GROUP_SECRET") or _keychain(SECRET_SERVICE)


@app.get("/health")
async def health():
    return {"ok": True, "members_mapped": len(load_members())}


@app.post("/group")
async def group(req: Request):
    got = req.headers.get("x-picky-group-secret") or ""
    if not SECRET or not hmac.compare_digest(got.encode(), SECRET.encode()):
        raise HTTPException(status_code=401, detail="bad_secret")
    body = await req.json()
    handle, text = str(body.get("handle") or ""), str(body.get("text") or "")
    chat_id, dm = str(body.get("chat") or ""), bool(body.get("dm"))
    if not dm and not TRIGGER.search(text):  # groups need @Picky; DMs always get a reply
        return {"reply": None}
    t0 = time.time()
    out = await answer(handle, text, chat_id=chat_id, dm=dm, allowlisted=bool(body.get("allowlisted")))
    if out is None:
        log.info("dm skipped sender=%s: not a mapped member or allowlisted", handle[-4:] or "?")
        return {"reply": None}
    log.info("%s reply sender=%s mapped=%s %.1fs chars=%d image=%s", out["mode"], handle[-4:] or "?",
             bool(VIEWER.sender(handle).team), time.time() - t0, len(out["reply"] or ""), bool(out["image"]))
    return {"reply": out["reply"] or None, "image": out["image"]}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="info")
