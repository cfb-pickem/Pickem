"""
Live replies on Claude: the TEMPORARY switch (Charlie 2026-11-19, exception to ledger M1/M15).

(a) switch off (missing file / on:false / expired / kill switch) = zero Claude calls from the live path
(b) switch on = a member text's generation goes to Claude (fake claude binary), usage logged "live"
(c) a Claude failure (exit, empty, timeout) = None, and chat._ollama_stream falls back to gemma
(d) a usage-limit message flips the switch off; LIVE_MAX_FAILS failures in a row do too
plus: test / exam traffic (ollama_gate low) never reaches Claude even with the switch on.

No network, no real claude, no GPU.   Run:  ./.venv/bin/python3 -I test_live_claude.py
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
TMP = Path(tempfile.mkdtemp(prefix="picky-live-claude-"))
os.environ["PICKY_CLAUDE_USAGE_DIR"] = str(TMP / "usage")
os.environ["PICKY_LIVE_CLAUDE"] = str(TMP / "switch.json")
os.environ.pop("PICKY_NO_ANTHROPIC", None)

import picky_claude as pc  # noqa: E402
import chat  # noqa: E402
import ollama_gate  # noqa: E402

FAILED: list[str] = []


def check(name, ok, got=None):
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else f"\n     got: {got!r}"[:900]))
    if not ok:
        FAILED.append(name)


def fake_claude(body: str) -> Path:
    """A claude stand-in: counts calls, saves argv + stdin, then runs `body` (sh)."""
    f = TMP / "claude"
    f.write_text("#!/bin/sh\nd=\"$(dirname \"$0\")\"\necho x >> \"$d/calls\"\nprintf '%s\\n' \"$@\" > \"$d/argv\"\n"
                 "cat > \"$d/stdin\"\n" + body + "\n")
    f.chmod(0o755)
    return f


def calls() -> int:
    try:
        return len((TMP / "calls").read_text().splitlines())
    except OSError:
        return 0


def switch(cfg: dict | None):
    if cfg is None:
        pc.LIVE_SWITCH.unlink(missing_ok=True)
    else:
        pc.LIVE_SWITCH.write_text(json.dumps(cfg))
    pc._live.update(mtime=None, fails=0)  # two writes inside one mtime tick still re-read


GEMMA = []


async def fake_raw(messages, prefix=None):
    GEMMA.append(messages)
    yield chat._sse({"content": "gemma says hi"})
    yield "data: [DONE]\n\n"


async def collect(messages):
    out = ""
    async for x in chat._ollama_stream(messages):
        if x.startswith("data: {"):
            out += json.loads(x[6:]).get("content", "")
    return out


MSGS = [{"role": "system", "content": "You are Picky."}, {"role": "user", "content": "who wins?"},
        {"role": "assistant", "content": "Iowa."}, {"role": "user", "content": "why?"}]


def live(coro):
    async def go():
        with ollama_gate.priority("live"):
            return await coro()
    return asyncio.run(go())


def low(coro):
    async def go():
        with ollama_gate.priority("low"):
            return await coro()
    return asyncio.run(go())


fake = fake_claude("echo 'Iowa covers, easy.'")
with patch.object(pc, "CLAUDE", str(fake)), patch.object(chat, "_ollama_stream_raw", fake_raw):
    # (a) off = zero calls
    for name, cfg in (("missing file", None), ("on false", {"on": False, "model": "opus"}),
                      ("expired", {"on": True, "until": "2026-01-01T00:00:00-06:00"})):
        switch(cfg)
        GEMMA.clear()
        out = live(lambda: collect(MSGS))
        check(f"(a) switch {name}: gemma answers, zero Claude calls", out == "gemma says hi" and calls() == 0
              and len(GEMMA) == 1, (out, calls()))
    switch({"on": True, "model": "opus"})
    with patch.dict(os.environ, {"PICKY_NO_ANTHROPIC": "1"}):
        out = live(lambda: collect(MSGS))
    check("(a) kill switch beats the live switch", out == "gemma says hi" and calls() == 0, (out, calls()))
    check("default in code is OFF (no file)", pc.LIVE_SWITCH.name == "switch.json" and
          (switch(None) or pc.live_why_not() == "off"))

    # test / exam traffic never reaches Claude
    switch({"on": True, "model": "opus"})
    out = low(lambda: collect(MSGS))
    check("test/exam traffic (low priority) stays on gemma with the switch on", out == "gemma says hi"
          and calls() == 0, (out, calls()))

    # (b) on = Claude
    GEMMA.clear()
    out = live(lambda: collect(MSGS))
    argv = (TMP / "argv").read_text().splitlines()
    stdin = (TMP / "stdin").read_text()
    check("(b) switch on: the reply comes from Claude", out == "Iowa covers, easy." and calls() == 1 and not GEMMA,
          (out, calls(), len(GEMMA)))
    check("(b) claude -p opus, no tools, empty MCP, no settings, system prompt passed",
          argv[argv.index("--model") + 1] == pc.MODEL and argv[argv.index("--tools") + 1] == ""
          and "--strict-mcp-config" in argv and argv[argv.index("--setting-sources") + 1] == ""
          and argv[argv.index("--system-prompt") + 1] == "You are Picky.", argv)
    check("(b) the conversation goes in, newest turn last", stdin.index("who wins?") < stdin.index("Iowa.")
          < stdin.index("why?"), stdin)
    rec = json.loads((pc.USAGE_DIR / f"{dt.datetime.now(pc.CT).date().isoformat()}.jsonl").read_text().splitlines()[-1])
    check("(b) usage log tagged live", rec.get("live") is True and rec["caller"] == "live-draft" and rec["result"] == "ok"
          and not rec.get("booster"), rec)
    check("(b) live calls don't count toward the booster cap", pc.used_today()[0] == 0, pc.used_today())
    switch({"on": True, "model": "sonnet"})
    live(lambda: collect(MSGS))
    argv = (TMP / "argv").read_text().splitlines()
    check("(b) model from the switch, re-read without a restart", argv[argv.index("--model") + 1] == pc.SONNET, argv)

    # (b) structured calls (picky_brain._json_call): JSON schema in, structured_output out
    fake_claude("echo '{\"type\":\"result\",\"is_error\":false,\"structured_output\":{\"line\":\"Iowa by 7.\"}}'")
    switch({"on": True, "model": "opus"})
    raw = live(lambda: pc.live_reply(MSGS[:2], "fact", schema={"type": "object"}))
    check("(b) schema call returns the structured output as JSON", raw and json.loads(raw) == {"line": "Iowa by 7."}, raw)

    # (c) failures fall back to gemma
    for name, body in (("non-zero exit", "echo boom >&2; exit 1"), ("empty output", "true")):
        fake_claude(body)
        switch({"on": True, "model": "opus"})
        GEMMA.clear()
        out = live(lambda: collect(MSGS))
        check(f"(c) {name}: gemma answers that reply", out == "gemma says hi" and len(GEMMA) == 1, out)
        check(f"(c) {name}: one failure doesn't flip the switch", pc.live_config().get("on") is True)
    fake_claude("sleep 5; echo late")
    switch({"on": True, "model": "opus"})
    with patch.object(pc, "LIVE_TIMEOUT", 0.5):
        out = live(lambda: collect(MSGS))
    check("(c) timeout: gemma answers that reply", out == "gemma says hi", out)

    # (d) usage limit flips it off
    fake_claude("echo 'Claude AI usage limit reached|1763600000'; exit 1")
    switch({"on": True, "model": "opus"})
    out = live(lambda: collect(MSGS))
    cfg = json.loads(pc.LIVE_SWITCH.read_text())
    check("(d) usage limit: gemma answers and the switch flips off", out == "gemma says hi" and cfg["on"] is False
          and "usage limit" in cfg["why"], cfg)
    n = calls()
    live(lambda: collect(MSGS))
    check("(d) after the flip: no more Claude calls", calls() == n)
    fake_claude("echo 'You have hit your limit, resets 9pm'")  # exit 0 but a limit line
    switch({"on": True, "model": "opus"})
    out = live(lambda: collect(MSGS))
    check("(d) a limit line on exit 0 also flips it off", out == "gemma says hi" and not pc.live_config().get("on"))
    fake_claude("exit 2")
    switch({"on": True, "model": "opus"})
    for _ in range(pc.LIVE_MAX_FAILS):
        live(lambda: collect(MSGS))
    cfg = json.loads(pc.LIVE_SWITCH.read_text())
    check(f"(d) {pc.LIVE_MAX_FAILS} failures in a row flip it off", cfg["on"] is False and "in a row" in cfg["why"], cfg)

    # the CLI the coordinator runs for "turn off picky claude"
    switch({"on": True, "model": "opus"})
    pc.main(["live", "off"])
    check("CLI `live off` turns it off", pc.live_config().get("on") is False and pc.live_why_not() == "off")
    pc.main(["live", "on", "--model", "sonnet"])
    check("CLI `live on --model sonnet`", pc.live_config() == {**pc.live_config(), "on": True, "model": "sonnet"})
    # a stale "raw": true must not survive `live on` (2026-11-23: it put Picky in raw Opus mode, canary broke)
    switch({"on": False, "model": "opus", "raw": True})
    pc.main(["live", "on", "--model", "opus"])
    check("CLI `live on` resets a stale raw:true to false", pc.live_config().get("raw") is False, pc.live_config())
    pc.main(["live", "on", "--raw"])
    check("CLI `live on --raw` turns raw on", pc.live_config().get("raw") is True, pc.live_config())
    pc.main(["live", "off"])
    check("CLI `live off` leaves raw alone", pc.live_config().get("raw") is True, pc.live_config())
    pc.main(["live", "on"])
    import contextlib
    import io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        pc.main(["live", "status"])
    last = buf.getvalue().strip().splitlines()[-1]
    check("CLI `live status` summary line shows raw", "raw false" in last and pc.live_config().get("raw") is False, last)

if FAILED:
    print(f"\n{len(FAILED)} FAILED")
    sys.exit(1)
print("\nALL PASS")
