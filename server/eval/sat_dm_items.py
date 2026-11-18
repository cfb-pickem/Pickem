"""
Saturday DM drill items (eval/sat_dm.py): Charlie-style texts to Picky on a game day, frozen at moments of the
recorded Saturday 2026-11-14 (fixture: eval/sat_fixture.py). Team names only (public on the site); no handles.

Each item: id, cat, at (CT clock on 11/14), turns (earlier texts sent first in the same thread; the LAST one is
graded) or clump (texts that land at once after a dead zone; graded as one reply), checks (code, see sat_dm.CHECKS),
detail (True: he asked for detail, so the 300-char cap is relaxed).
"Where he is": the drill puts Charlie at Texas at LSU (Baton Rouge), via PICKY_GAMEDAY_AT.
Game ids: MIZ@UGA 401856883, ALA@VAN 401856889, BC@ND 401858294, TEX@LSU 401856885, DUKE@MIA 401858296,
TENN@TAMU 401856888, USC@IU 401858518, BAY@BYU 401856858, FLA@UK 401856884, TTU@OKST 401856863, WAKE@SMU 401858301,
PUR@IOWA 401858519, MICH@ORE 401858523, SC@ARK 401856882, NU@OSU 401858522, ORST@BOIS 401860914, LOU@UNC 401858298,
UTAH@ARIZ 401856864, UNT@UTSA 401862827, NDSU@HAW 401864537, MINN@PSU 401858524, CIN@ISU 401856859.
"""

N, T3, T7, T815, T11 = "12:00", "15:00", "19:00", "20:15", "23:00"
UGA, ALA, ND, LSU, MIA, TENN, IU, BYU, UK, OKST = ("401856883", "401856889", "401858294", "401856885", "401858296",
                                                   "401856888", "401858518", "401856858", "401856884", "401856863")
SMU, IOWA, ORE, ARK, OSU, BOIS, UNC, ARIZ, UTSA, HAW, PSU, ISU = (
    "401858301", "401858519", "401858523", "401856882", "401858522", "401860914", "401858298", "401856864",
    "401862827", "401864537", "401858524", "401856859")

ITEMS = [
    # ---------------- live scores, any FBS game ----------------
    {"id": "sc-bama-noon", "cat": "score", "at": N, "turns": ["whats the bama score"], "checks": [("score", ALA)]},
    {"id": "sc-columbus", "cat": "score", "at": N, "turns": ["whos winning in columbus"], "checks": [("score", OSU), ("leader", OSU)]},
    {"id": "sc-miami-noon", "cat": "score", "at": N, "turns": ["miami duke score?"], "checks": [("score", MIA)]},
    {"id": "sc-isu", "cat": "score", "at": N, "turns": ["iowa st cincy"], "checks": [("score", ISU)]},
    {"id": "sc-uga-3", "cat": "score", "at": T3, "turns": ["uga up?"], "checks": [("score", UGA)]},
    {"id": "sc-nd-3", "cat": "score", "at": T3, "turns": ["hows nd doing"], "checks": [("score", ND)]},
    {"id": "sc-bama-final", "cat": "score", "at": T3, "turns": ["did bama win"], "checks": [("score", ALA), ("final_word", ALA), ("yes", True)]},
    {"id": "sc-oregon", "cat": "score", "at": T3, "turns": ["oregon mich score"], "checks": [("score", ORE)]},
    {"id": "sc-uk-7", "cat": "score", "at": T7, "turns": ["kentucky florida?"], "checks": [("score", UK)]},
    {"id": "sc-byu-7", "cat": "score", "at": T7, "turns": ["byu score"], "checks": [("score", BYU)]},
    {"id": "sc-psu", "cat": "score", "at": T815, "turns": ["penn st score"], "checks": [("score", PSU)]},
    {"id": "sc-utah-11", "cat": "score", "at": T11, "turns": ["utah arizona score"], "checks": [("score", ARIZ)]},
    {"id": "sc-okst", "cat": "score", "at": T11, "turns": ["texas tech score"], "checks": [("score", OKST)]},
    {"id": "sc-lsu-lose", "cat": "score", "at": T11, "turns": ["did lsu lose"], "checks": [("score", LSU), ("yes", True)]},
    {"id": "sc-whats-on", "cat": "score", "at": T3, "turns": ["whats good on rn"], "checks": [("names_live", 2)]},
    # ---------------- upsets ----------------
    {"id": "up-any-3", "cat": "upsets", "at": T3, "turns": ["any upsets"], "checks": [("upsets",)]},
    {"id": "up-any-815", "cat": "upsets", "at": T815, "turns": ["any upsets brewing"], "checks": [("upsets",)]},
    {"id": "up-ranked-losing", "cat": "upsets", "at": T815, "turns": ["any ranked teams losing rn"], "checks": [("upsets",)]},
    {"id": "up-day-11", "cat": "upsets", "at": T11, "turns": ["biggest upsets today?"], "checks": [("upsets",)]},
    {"id": "up-noon", "cat": "upsets", "at": N, "turns": ["any upsets yet"], "checks": [("upsets",)]},
    # ---------------- pool cover status / his week / standings ----------------
    {"id": "pool-covering-noon", "cat": "pool", "at": N, "turns": ["are we covering"], "checks": [("my_picks_live",)]},
    {"id": "pool-week-7", "cat": "pool", "at": T7, "turns": ["hows my week going"], "checks": [("my_week",)]},
    {"id": "pool-week-815", "cat": "pool", "at": T815, "turns": ["how am i doing this week"], "checks": [("my_week",)]},
    {"id": "pool-wake", "cat": "pool", "at": N, "turns": ["is wake covering"], "checks": [("cover", SMU)]},
    {"id": "pool-boise-815", "cat": "pool", "at": T815, "turns": ["oregon st covering?"], "checks": [("cover", BOIS)]},
    {"id": "pool-utsa-11", "cat": "pool", "at": T11, "turns": ["utsa covering"], "checks": [("cover", UTSA)]},
    {"id": "pool-standings-now", "cat": "pool", "at": T815, "turns": ["where am i in the standings rn"], "checks": [("my_place",)]},
    {"id": "pool-ended-now", "cat": "pool", "at": T11, "turns": ["if it ended right now where would i be"], "checks": [("my_place",)]},
    {"id": "pool-need-boise", "cat": "pool", "at": T815, "turns": ["how many pts does oregon st need to cover"], "checks": [("cover_need", BOIS)]},
    {"id": "pool-haw-11", "cat": "pool", "at": T11, "turns": ["ndsu hawaii covering?"], "checks": [("cover", HAW)]},
    # ---------------- TV ----------------
    {"id": "tv-oregon", "cat": "tv", "at": N, "turns": ["what channel is oregon on"], "checks": [("tv", ORE)]},
    {"id": "tv-lsu", "cat": "tv", "at": N, "turns": ["what channel is texas lsu"], "checks": [("tv", LSU)]},
    {"id": "tv-iowa", "cat": "tv", "at": T3, "turns": ["where can i watch iowa"], "checks": [("tv", IOWA)]},
    {"id": "tv-boise", "cat": "tv", "at": T3, "turns": ["boise game on what"], "checks": [("tv", BOIS)]},
    {"id": "tv-uga", "cat": "tv", "at": N, "turns": ["uga mizzou channel"], "checks": [("tv", UGA)]},
    {"id": "tv-utah", "cat": "tv", "at": T7, "turns": ["what network is utah on tonight"], "checks": [("tv", ARIZ)]},
    # ---------------- player / stat ----------------
    {"id": "st-nico", "cat": "stats", "at": N, "turns": ["how many yards does marchiol have"], "checks": [("pstat", OSU, "Nicco Marchiol", "passing", 1)]},
    {"id": "st-jsmith", "cat": "stats", "at": T3, "turns": ["jeremiah smith stats"], "checks": [("pstat", OSU, "Jeremiah Smith", "receiving", 1)]},
    {"id": "st-arch", "cat": "stats", "at": T815, "turns": ["arch manning stats"], "checks": [("pstat", LSU, "Arch Manning", "passing", 1)]},
    {"id": "st-stockton", "cat": "stats", "at": T3, "turns": ["how's gunner stockton playing"], "checks": [("pstat", UGA, "Gunner Stockton", "passing", 1)]},
    {"id": "st-uga-rush", "cat": "stats", "at": T7, "turns": ["who ran for the most for uga"], "checks": [("top", UGA, "UGA", "rushing")]},
    {"id": "st-tenn-3rd", "cat": "stats", "at": T3, "turns": ["tennessee 3rd down conversions?"], "checks": [("tstat", TENN, "TENN", "thirdDownEff")]},
    {"id": "st-tenn-yds", "cat": "stats", "at": T3, "turns": ["total yards tenn a&m"], "checks": [("tstat", TENN, "TENN", "totalYards"), ("tstat", TENN, "TA&M", "totalYards")]},
    {"id": "st-osu-final", "cat": "stats", "at": T7, "turns": ["how'd jeremiah smith end up"], "checks": [("pstat", OSU, "Jeremiah Smith", "receiving", 1)]},
    {"id": "st-ark-qb", "cat": "stats", "at": T11, "turns": ["kj jackson line vs sc"], "checks": [("pstat", ARK, "KJ Jackson", "passing", 1)]},
    {"id": "st-to-uk", "cat": "stats", "at": T815, "turns": ["turnovers in the uk game"], "checks": [("tstat", UK, "UK", "turnovers"), ("tstat", UK, "FLA", "turnovers")]},
    # ---------------- injuries / news ----------------
    {"id": "inj-unc-qb", "cat": "news", "at": T11, "turns": ["did unc's qb get hurt"], "checks": [("contains", r"Edwards"), ("contains", r"report|per ESPN|play-by-play|injur|hurt")]},
    {"id": "inj-any-qb", "cat": "news", "at": T11, "turns": ["any qbs go down today"], "checks": [("contains", r"Edwards")]},
    {"id": "news-latest", "cat": "news", "at": T3, "turns": ["anything big happen"], "checks": [("not_empty",)]},
    {"id": "inj-lsu", "cat": "news", "at": T815, "turns": ["anyone hurt in this game"], "checks": [("at_game_named",)]},
    # ---------------- the game he's at (TEX@LSU at 20:15 / 19:00) ----------------
    {"id": "at-score", "cat": "at_game", "at": T815, "turns": ["whats the score here lol cant see the board"], "checks": [("score", LSU)]},
    {"id": "at-drive", "cat": "at_game", "at": T815, "turns": ["what happened on that drive"], "checks": [("drive", LSU)]},
    {"id": "at-spread", "cat": "at_game", "at": T815, "turns": ["whats the spread status here"], "checks": [("market_line", LSU)]},
    {"id": "at-lsu-qb", "cat": "at_game", "at": T815, "turns": ["lsu qb stats"], "checks": [("pstat", LSU, "Sam Leavitt", "passing", 1)]},
    {"id": "at-tex-3rd", "cat": "at_game", "at": T815, "turns": ["texas 3rd downs?"], "checks": [("tstat", LSU, "TEX", "thirdDownEff")]},
    {"id": "at-who-scored", "cat": "at_game", "at": T815, "turns": ["who scored last"], "checks": [("last_score", LSU)]},
    {"id": "at-rb", "cat": "at_game", "at": T815, "turns": ["dilin jones yards"], "checks": [("pstat", LSU, "Dilin Jones", "rushing", 1)]},
    {"id": "at-ball", "cat": "at_game", "at": T815, "turns": ["who has the ball"], "checks": [("possession", LSU)]},
    {"id": "at-early", "cat": "at_game", "at": T7, "turns": ["how'd texas score"], "checks": [("last_score", LSU)]},
    {"id": "at-tos", "cat": "at_game", "at": T815, "turns": ["how many picks has arch thrown"], "checks": [("pstat", LSU, "Arch Manning", "passing", 4)]},
    # ---------------- other members ----------------
    {"id": "mem-ackbar", "cat": "members", "at": T815, "turns": ["hows ackbar doing"], "checks": [("ackbar",)]},
    {"id": "mem-ackbar-week", "cat": "members", "at": T11, "turns": ["ackbar's week?"], "checks": [("ackbar",)]},
    {"id": "mem-leader", "cat": "members", "at": T815, "turns": ["whos in first rn"], "checks": [("leader_now",)]},
    {"id": "mem-last", "cat": "members", "at": T11, "turns": ["whos in last"], "checks": [("not_empty",)]},
    # ---------------- playoff / rankings what-ifs ----------------
    {"id": "wi-texas", "cat": "whatif", "at": T7, "turns": ["if texas loses what happens"], "checks": [("cfp_rank", "Texas")]},
    {"id": "wi-uk", "cat": "whatif", "at": T815, "turns": ["if kentucky loses does that matter for the playoff"], "checks": [("cfp_rank", "Kentucky")]},
    {"id": "wi-who-in", "cat": "whatif", "at": T11, "turns": ["whos in the cfp rn"], "checks": [("cfp_top", 4)]},
    {"id": "wi-uga-1", "cat": "whatif", "at": T3, "turns": ["is uga still 1"], "checks": [("cfp_rank", "Georgia")]},
    {"id": "wi-bama", "cat": "whatif", "at": T3, "turns": ["where's bama ranked"], "checks": [("cfp_rank", "Alabama")]},
    # ---------------- banter ----------------
    {"id": "ban-loud", "cat": "banter", "at": T815, "turns": ["its so loud in here lmao"], "checks": [("not_empty",)]},
    {"id": "ban-sec", "cat": "banter", "at": T815, "turns": ["sec refs are a joke"], "checks": [("not_empty",)]},
    {"id": "ban-suck", "cat": "banter", "at": T7, "turns": ["picky u suck"], "checks": [("warm",)]},
    {"id": "ban-beer", "cat": "banter", "at": T3, "turns": ["$14 beer at this stadium smh"], "checks": [("not_empty",)]},
    {"id": "ban-thanks", "cat": "banter", "at": T11, "turns": ["thanks picky ur the man"], "checks": [("warm",)]},
    # ---------------- multi-turn follow-ups ----------------
    {"id": "fu-over", "cat": "followup", "at": T815, "turns": ["oregon st boise score", "and the over?"], "checks": [("total", BOIS)]},
    {"id": "fu-why", "cat": "followup", "at": T815, "turns": ["is oregon st covering", "why?"], "checks": [("cover", BOIS)]},
    {"id": "fu-qb", "cat": "followup", "at": T3, "turns": ["uga score", "how's their qb doing"], "checks": [("pstat", UGA, "Gunner Stockton", "passing", 1)]},
    {"id": "fu-channel", "cat": "followup", "at": N, "turns": ["is iowa playing today", "what channel"], "checks": [("tv", IOWA)]},
    {"id": "fu-osu-who", "cat": "followup", "at": N, "turns": ["whos winning in columbus", "who scored for them"], "checks": [("last_score", OSU)]},
    {"id": "fu-at-3rd", "cat": "followup", "at": T815, "turns": ["lsu qb stats", "and texas's"], "checks": [("pstat", LSU, "Arch Manning", "passing", 1)]},
    {"id": "fu-ark", "cat": "followup", "at": T815, "turns": ["any upsets", "wait sc is losing to arkansas?"], "checks": [("score", ARK)]},
    {"id": "fu-detail", "cat": "followup", "at": T815, "turns": ["how am i doing this week", "break it down for me"], "detail": True, "checks": [("my_week",)]},
    # ---------------- clumps (texts stacked up in a dead zone) ----------------
    {"id": "cl-3", "cat": "clump", "at": T815, "clump": ["whats the bama final", "any upsets", "lol its loud"], "checks": [("score", ALA), ("upsets_any",)]},
    {"id": "cl-2-pool", "cat": "clump", "at": T815, "clump": ["are we covering", "boise score?"], "checks": [("score", BOIS)]},
    {"id": "cl-4", "cat": "clump", "at": T11, "clump": ["utah score", "texas tech?", "did lsu lose", "where am i at"], "checks": [("score", ARIZ), ("score", LSU)]},
    {"id": "cl-dupe", "cat": "clump", "at": T7, "clump": ["uga final?", "uga final??"], "checks": [("score", UGA)]},
]
