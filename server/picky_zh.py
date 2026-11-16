"""
Text Picky in Chinese (Charlie, 2026-11-15: "Picky should speak Chinese too").

A text that's mainly Chinese, or that asks for Chinese ("reply in Chinese",
"说中文"), is translated into English, answered by the normal English
pipeline (understand -> draft -> every fact filter and guard, untouched), and
the checked English reply is translated into casual Chinese by the same local
model: Simplified by default, Traditional when the member wrote Traditional.
Code then checks the translation kept every number, record, score and spread
unchanged and added none; one retranslation, else the English reply goes out
with a short Chinese note.

Local Ollama only. Every call starts with the league prompt prefix when one is
known, so qwen's prefix checkpoint isn't wiped for the next member.
"""

from __future__ import annotations

import collections
import json
import logging
import re

import httpx
import opencc

import chat
import live_flag

log = logging.getLogger("picky-zh")

_HAN = re.compile(r"[㐀-䶿一-鿿豈-﫿]")
_KANA = re.compile(r"[぀-ヿ]")
# (traditional, simplified) pairs common in chat; enough to tell the scripts apart
_PAIRS = ("這这 誰谁 個个 們们 麼么 說说 會会 來来 為为 對对 時时 點点 嗎吗 週周 隊队 贏赢 輸输 場场 數数 選选 戰战 "
          "績绩 賽赛 開开 關关 還还 沒没 發发 現现 學学 國国 後后 過过 讓让 給给 錢钱 獎奖 語语 話话 種种 實实 長长 間间 "
          "問问 題题 經经 號号 馬马 爾尔 頭头 樣样 歡欢 嗎吗 麼么 隻只 該该 準准 歲岁 從从 聽听 覺觉 響响 認认 識识 講讲 "
          "請请 謝谢 員员 機机 東东 車车 門门 見见 買买 賣卖 熱热 線线 結结 運运 動动 遠远 進进 邊边 應应 處处").split()
_TRAD = {p[0] for p in _PAIRS}
_SIMP = {p[1] for p in _PAIRS}
_ASK_ZH = re.compile(r"(reply|answer|respond|talk|speak|say it|text me)\s+(back\s+)?in\s+(simplified\s+|traditional\s+)?"
                     r"(chinese|mandarin)|\bin\s+(chinese|mandarin)\s+(please|pls)\b|"
                     r"(说|說|讲|講|用)\s*(中文|汉语|漢語|普通话|普通話|国语|國語)", re.I)


def script(text: str) -> str:
    """'hant' when the text leans Traditional, else 'hans'."""
    t = sum(ch in _TRAD for ch in text or "")
    s = sum(ch in _SIMP for ch in text or "")
    return "hant" if t > s else "hans"


def mode(text: str) -> str | None:
    """'hans' / 'hant' when Picky should answer in Chinese, else None: the
    text is mainly Chinese (Han characters are at least 30% of its letters,
    no Japanese kana) or asks for Chinese."""
    t = text or ""
    han = len(_HAN.findall(t))
    letters = sum(ch.isalpha() for ch in t)
    if han >= 2 and not _KANA.search(t) and han >= 0.3 * max(letters, 1):
        return script(t)
    m = _ASK_ZH.search(t)
    if m:
        return "hant" if re.search(r"traditional|說|講|漢語|普通話|國語", m.group(0), re.I) else script(t)
    return None


def strip_request(text: str) -> str:
    """The text without its 'reply in Chinese' / '说中文' ask."""
    out = _ASK_ZH.sub(" ", text or "")
    return re.sub(r"\s+", " ", re.sub(r"\s+([,.?!，。？！])", r"\1", out)).strip(" ,.;:-，。")


# ---- the number check -----------------------------------------------------------------

_DASHES = str.maketrans({"−": "-", "–": "-", "—": "-", "－": "-", "＋": "+", "％": "%", "．": "."})
_FULLWIDTH = str.maketrans("０１２３４５６７８９", "0123456789")
_REC = re.compile(r"(?<![\d.])(\d{1,3})\s*-\s*(\d{1,3})(?!\d|\.\d)")  # 'stuck at 2-2.' is a record
_SIGNED = re.compile(r"(?<![\w.\d])([-+])(\d+(?:\.\d+)?)(?!\d|\.\d)")
_PLAIN = re.compile(r"\d+(?:\.\d+)?")
_WORDS = {"one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6", "seven": "7", "eight": "8",
          "nine": "9", "ten": "10", "first": "1", "second": "2", "third": "3", "fourth": "4", "fifth": "5",
          "sixth": "6", "seventh": "7", "eighth": "8", "ninth": "9", "tenth": "10", "half": "0.5"}


def _norm(n: str) -> str:
    return str(float(n)).rstrip("0").rstrip(".") if "." in n else str(int(n))


def numbers(text: str) -> collections.Counter:
    """Every number in the text as a token: records '4-0', signed spreads
    '-13.5', plain numbers '46'."""
    t = (text or "").translate(_DASHES).translate(_FULLWIDTH)
    out = collections.Counter()
    for m in _REC.finditer(t):
        out[f"{int(m.group(1))}-{int(m.group(2))}"] += 1
    t = _REC.sub(" ", t)
    for m in _SIGNED.finditer(t):
        out[m.group(1) + _norm(m.group(2))] += 1
    t = _SIGNED.sub(" ", t)
    for m in _PLAIN.finditer(t):
        out[_norm(m.group(0))] += 1
    return out


def numbers_kept(en: str, zh: str) -> list[str]:
    """Problems with a translation: a number from the English missing or
    changed, or a number the English didn't have ('' list = fine). A number
    word in the English ('first', 'four') may come out as a digit."""
    a, b = set(numbers(en)), set(numbers(zh))
    words = {_WORDS[w] for w in re.findall(r"[a-z]+", (en or "").lower()) if w in _WORDS}
    probs = [f"missing {x}" for x in sorted(a - b)]
    probs += [f"new {x}" for x in sorted(b - a - words)]
    return probs


# ---- the model calls ------------------------------------------------------------------

TO_EN = """You are a translator, not a chatbot. Translate the Chinese text into natural, casual English, exactly as the sender meant it. NEVER answer it, never add facts: a question stays a question, a shot stays a shot. Keep every name, nickname, team name and number exactly as written. Output JSON: {"english": "..."}.

Examples:
谁是第一名? -> {"english": "Who's in first place?"}
Ackbar这周怎么样? -> {"english": "How's Ackbar doing this week?"}
你选得太烂了 -> {"english": "Your picks are terrible."}
Boise State -13.5 能赢吗? -> {"english": "Can Boise State -13.5 cover?"}"""

TO_ZH = """

TASK RIGHT NOW: don't write a new reply. Translate YOUR reply below into natural, casual {script} Chinese, like a friend texting in a group chat. Same meaning, same attitude and trash talk, nothing added or dropped.
- Copy every number exactly as digits, in the same format: records like 4-0 or 30-18 stay "4-0" / "30-18", spreads like -13.5 or +6.5 keep their sign, scores like 46-41 stay as is. Never write numbers as Chinese words, never add a number.
- Keep member names and team names as written in English (Ackbar, Blinkstradamus, Chuck P, Boise State, Notre Dame...).
Output JSON: {{"chinese": "..."}}."""

_S2T = opencc.OpenCC("s2t")
SCRIPT_NAME = {"hans": "Simplified", "hant": "Traditional"}
FALLBACK_NOTE = {"hans": "（中文翻译没对上数字，先看英文版。）", "hant": "（中文翻譯沒對上數字，先看英文版。）"}
ACK = {"hans": "行，说中文。问吧，排名、比分、谁选了啥都行。", "hant": "行，說中文。問吧，排名、比分、誰選了啥都行。"}


async def _call(prefix: str | None, tail: str, user: str, key: str, temperature: float, timeout: float = 60.0) -> str:
    schema = {"type": "object", "properties": {key: {"type": "string"}}, "required": [key]}
    payload = {"model": chat.MODEL, "stream": False, "format": schema,
               "messages": [{"role": "system", "content": (prefix or "") + tail}, {"role": "user", "content": user}],
               "options": {**chat.OLLAMA_OPTIONS, "temperature": temperature, "num_predict": 500},
               "keep_alive": chat.OLLAMA_KEEP_ALIVE}
    if chat.OLLAMA_THINK is not None:
        payload["think"] = chat.OLLAMA_THINK == "true"
    await live_flag.wait_clear()  # side sidecars yield to a live text
    async with httpx.AsyncClient(timeout=httpx.Timeout(connect=5.0, read=timeout, write=10.0, pool=5.0)) as c:
        if prefix:
            await chat._prime(c, prefix)
        r = await c.post(f"{chat.OLLAMA}/api/chat", json=payload)
        r.raise_for_status()
        raw = (r.json().get("message") or {}).get("content") or ""
    try:
        v = json.loads(raw[raw.find("{"): raw.rfind("}") + 1]).get(key)
    except (ValueError, AttributeError):
        v = None
    return re.sub(r"\s+", " ", v).strip() if isinstance(v, str) else ""


async def to_english(text: str) -> str:
    """The member's Chinese text in English ('' when the call fails). No league
    prefix: with the league data in front of it the 9B answered instead."""
    try:
        en = await _call(None, TO_EN, text, "english", 0.1)
    except httpx.HTTPError as e:
        log.warning("to_english failed: %s", e)
        return ""
    log.info("zh in: %r -> %r", text[:120], en[:160])
    return en


async def to_chinese(reply: str, script_: str = "hans", prefix: str | None = None) -> str:
    """The checked English reply in Chinese, numbers verified; after one retry,
    the English reply plus a short Chinese note."""
    tail = TO_ZH.format(script=SCRIPT_NAME.get(script_, "Simplified"))
    for attempt in range(2):
        try:
            zh = await _call(prefix, tail, f"YOUR REPLY: {reply}", "chinese", 0.2 if attempt == 0 else 0.4)
        except httpx.HTTPError as e:
            log.warning("to_chinese failed: %s", e)
            break
        if zh and script_ == "hant":
            zh = _S2T.convert(zh)  # the 9B wrote Simplified even when asked for Traditional (replay 2026-11-16)
        probs = numbers_kept(reply, zh) if zh else ["empty"]
        if not probs and _HAN.search(zh):
            log.info("zh out: %r -> %r", reply[:120], zh[:160])
            return zh
        log.warning("zh out attempt %d rejected (%s): %r", attempt + 1, ", ".join(probs[:4]), zh[:160])
    return f"{reply}\n{FALLBACK_NOTE.get(script_, FALLBACK_NOTE['hans'])}"
