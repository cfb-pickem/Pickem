"""
Text Picky in Chinese (picky_zh.py + group_chat.answer). No model, no network.

    ./.venv/bin/python3 -I test_picky_zh.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import group_chat as gc  # noqa: E402
import picky_zh as zh  # noqa: E402


def test_cjk_detection_and_script():
    assert zh.mode("谁是第一名?") == "hans"
    assert zh.mode("Ackbar这周怎么样?") == "hans"           # names in Latin, the rest Chinese
    assert zh.mode("誰是第一名？這週誰贏了") == "hant"
    assert zh.mode("Ackbar這週怎麼樣?") == "hant"
    for en in ("who's winning?", "Ackbar is 3-1 this week", "", "lol 好", "Boise State -13.5 vs Oregon State 好"):
        assert zh.mode(en) is None, en
    assert zh.mode("ラーメン食べたい") is None                # Japanese kana: not Chinese
    assert zh.mode("who's winning? reply in Chinese") == "hans"
    assert zh.mode("who's winning? answer in traditional Chinese") == "hant"
    assert zh.mode("说中文") == "hans" and zh.mode("說中文") == "hant"
    assert zh.strip_request("who's winning? reply in Chinese") == "who's winning?"
    assert zh.strip_request("说中文") == ""


def test_number_preservation_check():
    en = "Chuck P is 4-0 this week and 30-18 on the season. I took Boise State -13.5 and it won 46-41, missed by 8.5."
    good = "Chuck P 这周 4-0，赛季 30-18。我选了 Boise State -13.5，结果 46-41 赢了，但差 8.5 分没过盘。"
    assert zh.numbers_kept(en, good) == []
    assert zh.numbers_kept(en, good.replace("4-0", "四胜零负")) == ["missing 4-0"]
    assert zh.numbers_kept(en, good.replace("-13.5", "13.5")) == ["missing -13.5", "new 13.5"]
    assert zh.numbers_kept(en, good + " 他排第2。") == ["new 2"]                   # an added number
    assert zh.numbers_kept(en, good.replace("30-18", "30－18")) == []              # full-width dash is fine
    assert zh.numbers_kept("Chuck P is in first place.", "Chuck P 排第1名。") == []  # 'first' -> 1 is allowed
    assert zh.numbers_kept("You're 3-1 this week.", "你这周3 - 1。") == []
    assert zh.numbers_kept("while you're stuck at 2-2.", "而你还卡在 2-2。") == []      # replay 2026-11-16: '2-2.'
    assert zh.numbers_kept("Boise State -13.5.", "Boise State -13.5。") == []
    assert zh.numbers_kept("Hawai'i +6.5 missed by 1.5.", "Hawai'i +6.5 差1.5分。") == []


def test_translation_retries_once_then_falls_back_to_english():
    en = "You're 4-0 this week, champ."
    calls = []

    async def fake(prefix, tail, user, key, temperature, timeout=60.0):
        calls.append(temperature)
        return outs.pop(0)

    outs = ["你这周四胜零负，冠军。", "你这周 4-0，冠军。"]
    with patch.object(zh, "_call", fake):
        assert asyncio.run(zh.to_chinese(en, "hans")) == "你这周 4-0，冠军。" and len(calls) == 2
    outs, calls[:] = ["你这周四胜零负。", "你这周 5-0。"], []
    with patch.object(zh, "_call", fake):
        out = asyncio.run(zh.to_chinese(en, "hant"))
    assert out.startswith(en) and zh.FALLBACK_NOTE["hant"] in out and len(calls) == 2
    assert "Traditional" in zh.TO_ZH.format(script=zh.SCRIPT_NAME["hant"])
    outs, calls[:] = ["Ackbar 这周 3-1，别指望他。"], []                        # asked for Traditional, got Simplified
    with patch.object(zh, "_call", fake):
        assert asyncio.run(zh.to_chinese("Ackbar is 3-1 this week.", "hant")) == "Ackbar 這周 3-1，別指望他。"


def test_answer_translates_in_and_out_and_leaves_english_alone():
    seen = []

    async def fake_answer(handle, text, chat_id="", dm=False, allowlisted=False, recent=None, web=None):
        seen.append(text)
        return {"reply": "Chuck P is 1st at 30-18, two games up on Ackbar.", "image": None, "mode": "group"}

    async def fake_en(text):
        return "Who's in first place?"

    async def fake_zh(reply, script, prefix=None):
        return f"[{script}] Chuck P 30-18 排第1。"

    with patch.object(gc, "_answer", fake_answer), patch.object(zh, "to_english", fake_en), \
            patch.object(zh, "to_chinese", fake_zh):
        out = asyncio.run(gc.answer("+15555550100", "@Picky 谁是第一名?"))
        assert seen == ["Who's in first place?"] and out["reply"] == "[hans] Chuck P 30-18 排第1。", out
        assert out["reply_en"].startswith("Chuck P is 1st")
        out = asyncio.run(gc.answer("+15555550100", "@Picky who's winning? reply in Chinese"))
        assert seen[-1] == "who's winning?" and out["reply"].startswith("[hans]")
        out = asyncio.run(gc.answer("+15555550100", "@Picky who's winning?"))
        assert seen[-1] == "@Picky who's winning?" and out["reply"].startswith("Chuck P is 1st")  # English: untouched
        out = asyncio.run(gc.answer("+15555550100", "@Picky 说中文"))
        assert out["reply"] == zh.ACK["hans"] and len(seen) == 3                     # no model call for the ask alone
    with patch.object(gc, "load_members", lambda: {}):
        assert asyncio.run(gc.answer("+15555550111", "你好吗", dm=True)) is None      # unknown DM: no model call


if __name__ == "__main__":
    fails = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ok  {name}")
            except Exception as e:  # noqa: BLE001
                fails += 1
                print(f"FAIL  {name}: {e!r}")
    print("\nALL GREEN" if not fails else f"\n{fails} FAILED")
    sys.exit(1 if fails else 0)
