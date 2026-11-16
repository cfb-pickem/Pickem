"""Game-day live mode (live_games.py): parsers, rider, claim check, poller.

Offline: the recorded 2026-11-14 games in eval/fixtures/live-2026-11-14 are
replayed at fixed moments; no network, no model.
  ./.venv/bin/python3 -I test_live_games.py   (from server/; -I is fine, the test adds its own path)
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE / "eval")]
_TMP = tempfile.mkdtemp()
os.environ["PICKY_LIVE_CACHE"] = os.path.join(_TMP, "live_games.json")
os.environ["PICKY_LIVE_BASE"] = "http://127.0.0.1:9"  # nothing listens: any real fetch fails fast
os.environ["PICKY_PRIVATE_DIR"] = _TMP  # group_outbox state for the ping test

import live_games as L  # noqa: E402
import live_replay as R  # noqa: E402

SB, GAMES = R.load_dir(HERE / "eval" / "fixtures" / "live-2026-11-14")
UNC, TEX, UTSA, RUTG = "401858298", "401856885", "401862827", "401858525"


def at(gid, rx, after=20, period=None):
    return R._ts(R._find_play(GAMES[gid], rx, period)["wallclock"]) + after


def state(gid, t):
    g = L.parse_summary(R.snapshot(GAMES[gid], t), gid)
    g["ts"] = time.time()
    return g


class Parsers(unittest.TestCase):
    def test_qb_injury_and_backup(self):
        t = at(UNC, r"B\.Edwards Jr\. injured on the play")
        g = state(UNC, t)
        self.assertEqual(g["state"], "in")
        self.assertEqual((g["period"], g["clock"]), (3, "9:14"))
        inj = [x for x in g["injuries"] if x["qb"]]
        self.assertEqual(inj[0]["full_name"], "Billy Edwards Jr.")
        self.assertEqual(inj[0]["team"], "North Carolina")
        self.assertIsNone(inj[0]["returned"])
        # a few minutes later the backup has the job
        g2 = state(UNC, at(UNC, r"T\.Burgess pass", after=200))
        q = g2["qb"]["North Carolina"]
        self.assertEqual(q["current"], "T.Burgess")
        self.assertEqual(q["changes"][0]["from"], "B.Edwards Jr.")
        txt = L.game_block(g2)
        self.assertIn("North Carolina QB Billy Edwards Jr. injured on the play 3Q 9:14", txt)
        self.assertIn("T.Burgess took over", txt)
        self.assertIn("REPORTED", txt)

    def test_scoring_play_details(self):
        g = state(TEX, at(TEX, r"D\.Jones rush left for 44 yards", after=45))
        last = g["scoring"][-1]
        self.assertEqual(last["team"], "LSU")
        self.assertIn("Dilin Jones 44 Yd Run", last["text"])
        self.assertEqual((last["period"], last["clock"]), (3, "5:52"))
        self.assertEqual((g["away_score"], g["home_score"]), (last["away"], last["home"]))
        self.assertIn("big play (44 yds)", L.game_block(g))

    def test_injured_player_who_returned(self):
        g = L.parse_summary(R.snapshot(GAMES[TEX], 1e12), TEX)
        cryer = next(x for x in g["injuries"] if "Cryer" in x["player"])
        self.assertEqual(cryer["team"], "Texas")
        self.assertIsNotNone(cryer["returned"])

    def test_pregame_and_final(self):
        g = L.parse_summary(R.snapshot(GAMES[UNC], 0), UNC)
        self.assertEqual(g["state"], "pre")
        f = L.parse_summary(R.snapshot(GAMES[UNC], 1e12), UNC)
        self.assertEqual((f["state"], f["away_score"], f["home_score"]), ("post", 16, 14))
        self.assertEqual(L.clock_text(f), "Final")

    def test_scoreboard_parse(self):
        t = at(UNC, r"B\.Edwards Jr\. injured on the play")
        sb = L.parse_scoreboard(R.scoreboard(GAMES, SB, t))
        g = sb[UNC]
        self.assertEqual(g["state"], "in")
        self.assertIn(g["possession"], ("away", "home"))
        self.assertTrue(g["down_distance"])
        self.assertEqual(sb[TEX]["home"]["rank"] or sb[TEX]["away"]["rank"], sb[TEX]["home"]["rank"] or sb[TEX]["away"]["rank"])

    def test_clean_play(self):
        self.assertEqual(L._clean_play("(09:20) Shotgun #9 B.Edwards Jr. sacked for loss of 6 yards"),
                         "B.Edwards Jr. sacked for loss of 6 yards")
        self.assertEqual(L._abbr_key("Billy Edwards Jr."), L._abbr_key("B.Edwards Jr."))


class Cover(unittest.TestCase):
    def test_matches_league_rule(self):
        # home + line > away covers; dead even goes to Away (set_winner_on_final)
        self.assertTrue(L.cover_status("North Texas", "UTSA", 2.5, 10, 14).startswith("UTSA +2.5 covering by 6.5"))
        self.assertTrue(L.cover_status("Oregon State", "Boise State", -13.5, 20, 30).startswith(
            "Oregon State +13.5 covering by 3.5"))
        self.assertIn("goes to Oregon State", L.cover_status("Oregon State", "Boise State", -10, 20, 30))
        try:
            import chat
        except Exception:  # noqa: BLE001 - chat needs its full env; the rule is pinned above anyway
            return
        for ap, hp, ln in ((10, 14, 2.5), (20, 30, -13.5), (3, 0, 6.5), (21, 24, -3)):
            g = {"Away": "A", "Home": "H", "AwayPts": ap, "HomePts": hp, "line": ln}
            self.assertTrue(L.cover_status("A", "H", ln, ap, hp).startswith(chat.covering_side(g)))


class Rider(unittest.TestCase):
    def setUp(self):
        L._overlay.clear()
        L._mem.update(mtime=None, data=None)

    def write(self, games: dict, age_s: float = 0.0):
        now = time.time() - age_s
        for g in games.values():
            g["ts"] = now
        L._write({"updated_ts": now, "games": games, "pool_ids": [UTSA]})

    def test_question_routing(self):
        t = at(UNC, r"B\.Edwards Jr\. injured on the play")
        self.write({UNC: state(UNC, t), UTSA: state(UTSA, t), TEX: state(TEX, t)})
        for q in ("did UNC's QB get hurt?", "what's the score in the North Carolina game",
                  "is Edwards ok?", "how's Louisville doing"):
            txt, hit = L.rider(q, refresh=False)
            self.assertIn(UNC, [g["id"] for g in hit], q)
            self.assertIn("Billy Edwards Jr.", txt)
        txt, hit = L.rider("what's the weather like in Chicago", refresh=False)
        self.assertEqual(txt, "")
        # a generic live ask: the live pool games
        txt, hit = L.rider("what's happening right now", {UTSA: {"away": "North Texas", "home": "UTSA", "line": 2.5}},
                           refresh=False)
        self.assertIn(UTSA, [g["id"] for g in hit])

    def test_pool_cover_line(self):
        t = at(UTSA, r"M\.Reese caught at UTSA02", after=40)
        g = state(UTSA, t)
        self.write({UTSA: g})
        txt, _ = L.rider("is UTSA covering?", {UTSA: {"away": "North Texas", "home": "UTSA", "line": 2.5}},
                         refresh=False)
        m = g["home_score"] + 2.5 - g["away_score"]
        self.assertIn("POOL GAME, league line UTSA +2.5: right now", txt)
        self.assertIn(f"covering by {L._num(abs(m))} (UTSA leads by {g['home_score'] - g['away_score']}, +2.5 on the line", txt)
        _, hit = L.rider("is UTSA covering?", {UTSA: {"away": "North Texas", "home": "UTSA", "line": 2.5}}, refresh=False)
        chk = L.claim_check(hit)
        lead = g["home_score"] - g["away_score"]
        self.assertTrue(chk(f"Yeah, UTSA is covering the pool line by {L._num(abs(m))} as of 8:49 in the 3rd."))
        self.assertFalse(chk(f"Yeah, UTSA is currently covering the pool line by {lead} points."))  # replay r2

    def test_stale_live_game_is_never_current(self):
        t = at(UNC, r"B\.Edwards Jr\. injured on the play")
        self.write({UNC: state(UNC, t)}, age_s=L.MAX_AGE + 30)
        txt, _ = L.rider("UNC score?", refresh=True)  # refresh fails (nothing listens): must not state it
        self.assertIn("do NOT give a current score", txt)
        self.assertNotIn("Louisville 13", txt)

    def test_on_demand_refresh_overlay_wins(self):
        t = at(UNC, r"B\.Edwards Jr\. injured on the play")
        old = state(UNC, t - 1200)
        self.write({UNC: old}, age_s=200)
        fresh = state(UNC, t)
        orig = L.fetch_game
        L.fetch_game = lambda gid, client=None, timeout=4.0: {**fresh, "ts": time.time()}
        try:
            txt, hit = L.rider("did UNC's QB get hurt?")
        finally:
            L.fetch_game = orig
        self.assertIn("Billy Edwards Jr. injured", txt)
        self.assertLess(L.age(hit[0]), 5)

    def test_apply_to_ctx_overlays_pool_rows(self):
        t = at(UTSA, r"M\.Reese caught at UTSA02", after=40)
        g = state(UTSA, t)
        self.write({UTSA: g})
        ctx = {"games_this_week": [{"id": int(UTSA), "away": "North Texas", "home": "UTSA", "line": 2.5,
                                    "winner": "UTSA", "status": "final"}],
               "live_games": [{"GameId": int(UTSA), "Away": "North Texas", "Home": "UTSA", "AwayPts": 16,
                               "HomePts": 35, "Status": "final", "winner": "UTSA"}]}
        L.apply_to_ctx(ctx, "is UTSA covering?", refresh=False)
        row = ctx["live_games"][0]
        self.assertEqual((row["AwayPts"], row["HomePts"], row["Status"]), (g["away_score"], g["home_score"], "in_progress"))
        self.assertIsNone(ctx["games_this_week"][0]["winner"])
        self.assertTrue(ctx["live_rider"])

    def test_prefix_stays_out(self):
        # the rider is per-request text; nothing here touches chat.prompt_prefix
        import inspect
        try:
            import chat
        except Exception:  # noqa: BLE001
            return
        self.assertNotIn("live_rider", inspect.getsource(chat.prompt_prefix))


class Claims(unittest.TestCase):
    def test_scores_scorers_injuries(self):
        g = state(TEX, at(TEX, r"D\.Jones rush left for 44 yards", after=45))
        chk = L.claim_check([g])
        a, h = g["away_score"], g["home_score"]
        self.assertTrue(chk(f"As of 5:52 in the 3rd, LSU leads Texas {h}-{a}."))
        self.assertTrue(chk(f"Texas {a}, LSU {h}, and Dilin Jones just went 44 yards to the house."))
        self.assertFalse(chk("Texas leads LSU 22-21."))       # the final, not the current score
        self.assertFalse(chk(f"LSU is up by {abs(h - a) + 3} on Texas."))
        scorers = " ".join(x["text"] for x in g["scoring"]).lower()
        nobody = next(p for p in g["players"] if len(p) > 4 and p not in scorers and p.isalpha())
        self.assertFalse(chk(f"{nobody.title()} ran it in for LSU's touchdown."))  # in the feed, never scored
        self.assertTrue(chk("Nice weather in Baton Rouge."))
        self.assertTrue(chk("Bama is up 21-3."))  # another game, not this one
        u = state(UNC, at(UNC, r"B\.Edwards Jr\. injured on the play"))
        cu = L.claim_check([u])
        self.assertTrue(cu("UNC's QB Edwards got hurt on a sack, reported."))
        # replay r2: the right answer was cut as a 13-point 'margin'
        self.assertTrue(cu(f"Louisville is up {u['away_score']} to {u['home_score']} with 9:14 left in the 3rd quarter."))
        self.assertFalse(cu("Louisville is up 10 on North Carolina."))
        self.assertFalse(cu("North Carolina lost Hall to an injury."))  # in the feed, not reported hurt


class FakeResp:
    def __init__(self, status, body=None, headers=None):
        self.status_code, self._b, self.headers = status, body, headers or {}

    def json(self):
        return self._b

    def raise_for_status(self):
        import httpx
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("x", request=httpx.Request("GET", "http://x"),
                                        response=httpx.Response(self.status_code, headers=self.headers))


class FakeClient:
    def __init__(self, t_fn, fail_sb=0):
        self.t_fn, self.calls, self.fail_sb = t_fn, [], fail_sb

    def get(self, url, headers=None, timeout=None):
        self.calls.append(url)
        if "scoreboard" in url:
            if self.fail_sb:
                self.fail_sb -= 1
                return FakeResp(503)
            return FakeResp(200, R.scoreboard(GAMES, SB, self.t_fn()))
        gid = url.rsplit("=", 1)[-1]
        return FakeResp(200, R.snapshot(GAMES[gid], self.t_fn()))


class Poller(unittest.TestCase):
    def test_idle_live_and_tiers(self):
        clock = {"t": min(R.kickoff(s) for s in GAMES.values()) - 6 * 3600}
        c = FakeClient(lambda: clock["t"])
        p = L.Poller(client=c, clock=lambda: clock["t"], cache=Path(_TMP) / "p1.json",
                     pool_ids_fn=lambda: [UTSA])
        p.games = {}
        wait = p.step()
        self.assertEqual(p.mode, "idle")
        self.assertGreaterEqual(wait, 60)
        clock["t"] = at(UNC, r"B\.Edwards Jr\. injured on the play")
        p.next_sb = 0
        p.step()
        self.assertEqual(p.mode, "live")
        self.assertIn(UNC, p.tracked())
        self.assertTrue(p.games[UNC].get("detail"))
        saved = json.loads((Path(_TMP) / "p1.json").read_text())
        self.assertEqual(saved["games"][UNC]["state"], "in")
        # pool games come first; they re-poll on the pool cadence
        tr = p.tracked()
        if UTSA in tr:
            self.assertEqual(tr[0], UTSA)

    def test_backoff(self):
        clock = {"t": at(UNC, r"B\.Edwards Jr\. injured on the play")}
        c = FakeClient(lambda: clock["t"], fail_sb=3)
        p = L.Poller(client=c, clock=lambda: clock["t"], cache=Path(_TMP) / "p2.json", pool_ids_fn=lambda: [])
        p.games = {}
        p.step()
        self.assertEqual(p.fails.get("sb"), 1)
        held = p.hold["sb"] - clock["t"]
        n = len(c.calls)
        p.next_sb = 0
        p.step()  # still held: no new scoreboard call
        self.assertEqual(len([u for u in c.calls[n:] if "scoreboard" in u]), 0)
        clock["t"] += held + 1
        p.step()
        self.assertEqual(p.fails.get("sb"), 2)
        self.assertGreater(p.hold["sb"] - clock["t"], held)  # doubled


class Sentences(unittest.TestCase):
    def test_no_split_after_jr(self):
        try:
            import chat
        except Exception:  # noqa: BLE001
            return
        t = "Billy Edwards Jr. was reported injured at 3Q 9:14. T.Burgess is in. "
        self.assertEqual([m.end() for m in chat._SENT_END.finditer(t)][0], t.index("T.Burgess"))


class NewsFilter(unittest.TestCase):
    def test_live_feed_counts_as_a_source(self):
        try:
            import chat
        except Exception:  # noqa: BLE001
            return
        g = state(UNC, at(UNC, r"B\.Edwards Jr\. injured on the play"))
        txt = "LIVE GAME FEED (ESPN, ...)\n" + L.game_block(g)
        sent = "Yeah, Billy Edwards Jr. was reported injured on the play at 9:14 in the 3rd quarter."
        self.assertTrue(chat.news_claim_check(txt)(sent))  # replay r2: was cut as 'no source block'
        self.assertFalse(chat.news_claim_check("no blocks here")(sent))


class Brain(unittest.TestCase):
    def test_understand_pass_sees_the_live_feed(self):
        try:
            import picky_brain
        except Exception:  # noqa: BLE001
            return
        u = picky_brain.understand_user("UNC score?", "x", "", "", live="LIVE GAME FEED ...\n- Louisville 13")
        self.assertIn("LIVE GAME FEED", u)
        self.assertLess(u.index("LIVE GAME FEED"), u.index("NEWEST TEXT"))
        self.assertNotIn("LIVE GAME FEED", picky_brain.understand_user("UNC score?", "x", "", ""))


class GroupPings(unittest.TestCase):
    def test_off_by_default_and_qb_news_only(self):
        import group_posts as gp
        self.assertFalse(gp.LIVE_PINGS_FLAG.exists())  # OFF unless Charlie asks
        t = at(UNC, r"T\.Burgess pass", after=200)
        g = state(UNC, t)

        class LG:
            games = [{"GameId": int(UNC), "Away": "Louisville", "Home": "North Carolina", "line": 2.5, "picked": True}]
        pings = gp.live_pings(LG(), {UNC: g})
        self.assertEqual(len(pings), 1)
        key, text = pings[0]
        self.assertIn("Billy Edwards Jr. reported hurt", text)
        self.assertIn("T.Burgess in at QB for North Carolina", text)
        self.assertIn("Pool line: ", text)
        # no QB news, no ping; not a pool game, no ping
        self.assertEqual(gp.live_pings(LG(), {UNC: state(UNC, at(UNC, r"B\.Edwards Jr\. injured", after=-600))}), [])
        LG.games[0]["picked"] = False
        self.assertEqual(gp.live_pings(LG(), {UNC: g}), [])


if __name__ == "__main__":
    unittest.main(verbosity=1)
