"""Picture prompt expander (2026-11-18): the image service got the member's raw words, so 'notre dame winning the
national title' drew a man in front of the Paris cathedral in a garbled-text jersey. Model-free."""
import asyncio
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

TMP = Path(tempfile.mkdtemp())
os.environ.setdefault("PICKY_NO_ANTHROPIC", "1")
os.environ["PICKY_TEAM_LOOKS"] = str(TMP / "team_looks_cache.json")  # never the live cache
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import picture_prompt as pp  # noqa: E402

ND = "Generate an image of notre dame winning the national title"


def low(s):
    return s.lower()


def test_notre_dame_title_reads_as_football_not_the_cathedral():
    p = pp.expand(ND)
    lp = low(p)
    assert "fighting irish" in lp and "football" in lp
    assert "navy" in lp and "gold" in lp
    assert "trophy" in lp or "confetti" in lp
    assert "cathedral" not in lp and "church" not in lp
    bare = [m.start() for m in re.finditer(r"notre dame", lp) if not lp[m.end():].startswith(" fighting irish")]
    assert not bare, f"bare 'Notre Dame' could be read as the church: {p}"
    assert not lp.startswith("generate"), "the request words aren't part of the picture"


def test_style_suffix_suppresses_lettering_and_logos():
    lp = low(pp.expand(ND))
    assert "photo" in lp and "only a number" in lp and "no logos" in lp and "no lettering" in lp


def test_hard_names_resolve():
    for q, want, colors in (("texas a&m", "texas a&m aggies", ["maroon"]),
                            ("miami", "miami hurricanes", ["orange", "green"]),
                            ("ole miss", "ole miss rebels", ["navy blue"]),
                            ("LSU", "lsu tigers", ["purple", "gold"]),
                            ("draw lsu beating alabama", "lsu tigers", ["crimson"])):
        lp = low(pp.expand(q))
        assert want in lp and "football" in lp, (q, lp)
        assert all(c in lp for c in colors), (q, lp)
    assert "redhawks" not in low(pp.expand("miami")), "bare Miami is the Hurricanes (the resolver's call)"


def test_two_teams_keep_their_order_and_texas_stays_out_of_texas_am():
    lp = low(pp.expand("texas a&m beating texas"))
    assert lp.index("texas a&m aggies") < lp.index("texas longhorns")
    assert "burnt orange" in lp and "maroon" in lp


def test_scenes_expand():
    assert "tailgate" in low(pp.expand("an alabama tailgate")) and "parking lot" in low(pp.expand("an alabama tailgate"))
    assert "loss" in low(pp.expand("michigan losing"))
    assert "celebrating" in low(pp.expand("ohio state beating michigan"))
    assert "mascot" in low(pp.expand("the oregon mascot"))
    assert "trophy" in low(pp.expand("georgia wins the natty"))


def test_unknown_words_pass_through_without_football():
    p = pp.expand("draw me a golden retriever surfing a wave")
    lp = low(p)
    assert "a golden retriever surfing a wave" in lp
    assert "football" not in lp and "stadium" not in lp
    assert "no lettering" in lp, "the no-text suffix still rides along"


def test_overlong_prompt_is_capped_under_image_service_limit():
    img = (HERE / "image.py").read_text()
    limit = int(re.search(r"prompt: str = Field\(.*max_length=(\d+)", img).group(1))
    p = pp.expand("notre dame " + "very long words " * 600)
    assert len(p) <= pp.MAX_CHARS < limit
    assert p.endswith(pp.SPORTS_STYLE), "the cap trims the member's words, never the style suffix"


def test_color_names_from_hex():
    assert pp.color_name("062340") == "navy blue"
    assert pp.color_name("c99700") == "metallic gold"
    assert pp.color_name("9e1b32") == "crimson"
    assert pp.color_name("ffffff") == "white"
    assert pp.color_name("bf5700") == "burnt orange"


def test_snapshot_validator_every_fbs_team_has_a_nickname_and_two_named_colors():
    snap = json.loads(pp.SNAPSHOT.read_text())
    assert pp.validate(snap) == []
    assert len(snap["teams"]) >= 130
    for name, t in snap["teams"].items():
        c = pp.colors(t)
        assert t["mascot"] and len(c) == 2 and c[0] != c[1], (name, t, c)
    import factstore
    st = factstore.load() or {}
    missing = [n for n in st.get("teams") or {} if n not in snap["teams"]]
    assert not missing, f"store teams missing from the snapshot: {missing}"


def test_validator_catches_bad_rows():
    bad = {"teams": {"A": {"display": "A Aces", "mascot": "", "color": "062340", "alt": "c99700"},
                     "B": {"display": "B Bees", "mascot": "Bees", "color": "zz", "alt": "c99700"}}}
    probs = pp.validate(bad)
    assert any("A" in x for x in probs) and any("B" in x for x in probs)
    assert pp.validate({"teams": {}}), "an empty table is not a valid table"


def test_refresh_writes_the_cache_with_a_long_ttl_and_never_a_bad_table():
    espn = {"sports": [{"leagues": [{"teams": [
        {"team": {"id": "87", "displayName": "Notre Dame Fighting Irish", "name": "Fighting Irish",
                  "color": "062340", "alternateColor": "c99700"}},
        {"team": {"id": "333", "displayName": "Alabama Crimson Tide", "name": "Crimson Tide",
                  "color": "9e1b32", "alternateColor": "ffffff"}}]}]}]}
    calls = []
    cache = TMP / "rf.json"

    def fetch():
        calls.append(1)
        return espn

    fbs = {"Notre Dame": "87", "Alabama": "333"}
    assert pp.refresh(path=cache, fetch=fetch, fbs=fbs)["written"] == 2
    assert json.loads(cache.read_text())["teams"]["Notre Dame"]["mascot"] == "Fighting Irish"
    assert pp.refresh(path=cache, fetch=fetch, fbs=fbs)["skipped"], "inside the TTL: no second call"
    assert len(calls) == 1 and pp.TTL_S >= 14 * 86400
    old = time.time() - pp.TTL_S - 60
    os.utime(cache, (old, old))
    espn["sports"][0]["leagues"][0]["teams"][0]["team"]["name"] = ""  # a broken feed
    r = pp.refresh(path=cache, fetch=fetch, fbs=fbs)
    assert r.get("invalid") and json.loads(cache.read_text())["teams"]["Notre Dame"]["mascot"] == "Fighting Irish"


def test_no_cache_falls_back_to_the_committed_snapshot():
    assert not Path(os.environ["PICKY_TEAM_LOOKS"]).exists()
    assert pp.looks()["Notre Dame"]["mascot"] == "Fighting Irish"


def test_resolves_without_the_fact_store():
    import factstore
    keep = dict(factstore._override)
    factstore._override["path"] = str(TMP / "no-store.json")
    try:
        assert "fighting irish" in low(pp.expand(ND))
    finally:
        factstore._override.clear()
        factstore._override.update(keep)


def test_generate_image_sends_the_expanded_prompt_and_nothing_else():
    import chat
    sent = []

    class Resp:
        status_code = 200

        def json(self):
            return {"image_base64": "aGk="}

    class Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, timeout=None):
            sent.append((url, json))
            return Resp()

    real = chat.httpx.AsyncClient
    chat.httpx.AsyncClient = Client
    try:
        status, b64 = asyncio.run(chat.generate_image(ND, "u1"))
    finally:
        chat.httpx.AsyncClient = real
    body = next(j for u, j in sent if u.endswith("/generate") and "prompt" in j and "user_id" in j)
    assert status == 200 and b64 == "aGk="
    assert body["prompt"] == pp.expand(ND)
    assert set(body) == {"prompt", "user_id", "width", "height"}, "only the prompt + sizing go to the image service"


def test_fact_store_tick_refreshes_team_looks_and_a_failure_never_blocks_the_build():
    import factstore
    import personnel
    import polls
    seen = []
    keep = (polls.refresh, personnel.tick, factstore.build, pp.refresh)
    polls.refresh = lambda **k: None
    personnel.tick = lambda: None
    factstore.build = lambda: {"built": True}

    def boom():
        seen.append(1)
        raise RuntimeError("espn down")
    pp.refresh = boom
    try:
        assert factstore.refresh() == {"built": True} and seen == [1]
    finally:
        polls.refresh, personnel.tick, factstore.build, pp.refresh = keep


if __name__ == "__main__":
    fails = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("ok  ", name)
            except Exception as e:  # noqa: BLE001
                fails += 1
                print("FAIL", name, type(e).__name__, e)
    print("ALL PASS" if not fails else f"{fails} FAILED")
    sys.exit(1 if fails else 0)
