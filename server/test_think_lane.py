"""
Offline tests for think_lane.py (M10 retry: capped gemma thinking on full-brain analysis drafts).
No model, no network: a mock Ollama behind httpx.MockTransport.

    ./.venv/bin/python3 -I test_think_lane.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx  # noqa: E402

import router  # noqa: E402
import think_lane as tl  # noqa: E402

_REAL_CLIENT = httpx.AsyncClient


def _st(q, lane="full", **kw):
    return {"q": q, "route": router.Route(lane, why="no rule"), "ctx": kw.pop("ctx", {}), **kw}


def test_wanted_only_full_brain_analysis():
    yes = ["Ohio State or Nebraska, who you got?", "Is 16.5 too many for Georgia?", "Utah -7 at TCU. Motivation angles?",
           "Give me a side in Memphis-Navy.", "Duke -13.5, should I lay it?", "Does the wind matter for Iowa-Illinois?"]
    for q in yes:
        assert tl.wanted(_st(q), "question"), q
    assert not tl.wanted(_st("Is 16.5 too many for Georgia?", lane="fact"), "question")    # code lanes stay
    assert not tl.wanted(_st("Is 16.5 too many for Georgia?", lane="banter"), "question")
    assert not tl.wanted(_st("Is 16.5 too many for Georgia?"), "banter")                    # brief said banter
    assert not tl.wanted(_st("whats the latest on the LSU coach search?"), "question")      # news lookup
    assert not tl.wanted(_st("best cereal?"), "question")                                    # not football analysis
    assert not tl.wanted(_st("who you got in Iowa-Illinois?", ctx={"live_rider": "LIVE: ..."}), "question")
    assert not tl.wanted(_st("who you got in Iowa-Illinois?", thought=True), "question")    # once per text
    old, tl.ENABLED = tl.ENABLED, False
    try:
        assert not tl.wanted(_st("who you got in Iowa-Illinois?"), "question")              # the kill switch
    finally:
        tl.ENABLED = old


def _ollama(chunks, delay=0.0):
    """A mock Ollama: the prime gets a JSON reply; the chat stream yields `chunks` ((kind, text) pairs)."""
    async def body():
        for kind, text in chunks:
            if delay:
                await asyncio.sleep(delay)
            yield (json.dumps({"message": {kind: text}, "done": False}) + "\n").encode()
        yield (json.dumps({"message": {}, "done": True, "eval_count": len(chunks), "prompt_eval_count": 40,
                           "prompt_eval_duration": 1e8}) + "\n").encode()

    async def handler(req: httpx.Request):
        p = json.loads(req.content)
        if not p.get("stream"):
            return httpx.Response(200, json={"prompt_eval_duration": 1e8})
        assert p["think"] is True and p["options"]["num_predict"] > tl.TOKENS
        return httpx.Response(200, content=body())
    return httpx.MockTransport(handler)


def _run(transport, **caps):
    old = {k: getattr(tl, k) for k in caps}
    for k, v in caps.items():
        setattr(tl, k, v)
    tl.httpx.AsyncClient = lambda **kw: _REAL_CLIENT(transport=transport, **kw)
    try:
        return asyncio.run(tl.draft([{"role": "system", "content": "s"}, {"role": "user", "content": "q"}],
                                    "prefix", {"temperature": 0.2}))
    finally:
        tl.httpx.AsyncClient = _REAL_CLIENT
        for k, v in old.items():
            setattr(tl, k, v)


def test_draft_returns_only_the_answer():
    text, info = _run(_ollama([("thinking", "Georgia ")] * 5 + [("content", "Lean Georgia, "), ("content", "16.5 is fair.")]))
    assert text == "Lean Georgia, 16.5 is fair." and info["think"] == 5 and info["done"] and not info["why"]


def test_runaway_thinking_falls_back():
    text, info = _run(_ollama([("thinking", "hmm ")] * 50 + [("content", "late answer")]), TOKENS=20)
    assert text is None and info["why"] == "think cap"


def test_slow_answer_times_out_and_falls_back():
    text, info = _run(_ollama([("thinking", "x ")] * 10 + [("content", "too late")], delay=0.05), TIMEOUT=0.2)
    assert text is None and info["why"] == "timeout" and info["secs"] < 1.0


def test_unreachable_ollama_falls_back():
    def handler(req):
        raise httpx.ConnectError("down")
    text, info = _run(httpx.MockTransport(handler))
    assert text is None and info["why"]


def test_empty_answer_falls_back():
    text, info = _run(_ollama([("thinking", "x ")] * 3))
    assert text is None and info["why"] == "empty"


def test_brain_uses_the_thought_answer_or_falls_back():
    import picky_brain as pb

    calls = []

    async def fake_orig(messages, prefix=None):
        calls.append("plain")
        yield 'data: {"content": "plain draft"}\n\n'
        yield "data: [DONE]\n\n"

    async def run(think_text):
        async def fake_draft(messages, prefix, options):
            calls.append("think")
            return think_text, {}
        st = {"q": "Is 16.5 too many for Georgia?", "route": router.Route("full", why="no rule"), "ctx": {},
              "brief": pb.Brief(asking="is 16.5 too many", intent="question"), "prefix": "P", "idx": 0,
              "options": {}, "stance": None}
        tok = pb.STATE.set(st)
        old = (pb._orig_ollama_stream, pb._REAL_ORIG_STREAM, tl.draft)
        pb._orig_ollama_stream = pb._REAL_ORIG_STREAM = fake_orig
        tl.draft = fake_draft
        try:
            out = [x async for x in pb._ollama_stream([{"role": "system", "content": "s"},
                                                       {"role": "user", "content": st["q"]}], "P")]
        finally:
            pb._orig_ollama_stream, pb._REAL_ORIG_STREAM, tl.draft = old
            pb.STATE.reset(tok)
        return "".join(out), st

    out, st = asyncio.run(run("Lean Georgia."))
    assert "Lean Georgia." in out and calls == ["think"] and st["thought"]
    calls.clear()
    out, st = asyncio.run(run(None))
    assert "plain draft" in out and calls == ["think", "plain"]


if __name__ == "__main__":
    fails = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("ok  ", name)
            except Exception as e:  # noqa: BLE001
                fails += 1
                print("FAIL", name, repr(e))
    print("FAILS:", fails or "none")
    sys.exit(1 if fails else 0)
