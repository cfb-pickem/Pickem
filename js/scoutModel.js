// js/scoutModel.js — how is this player likely to pick against the spread?
//
// Every pick in this league is ATS: `winner` in all_games is the side that
// COVERED, not the side that won. So the question a scouting report has to
// answer is "which side of the number will they take?", and the model below
// predicts exactly one thing: P(this player lays the points).
//
// ------------------------------------------------------------------------
// WHAT THIS MODEL CAN AND CANNOT DO — read before putting a number on screen.
//
// Fitted and cross-validated on the full 2025 season (826 picks, 76 games,
// 13 players), holding out one whole week at a time:
//
//                                       accuracy   log loss   AUC
//   always guess "lays the points"       60.2%      0.6723    0.489
//   this model, leave-one-week-out       60.5%      0.6558    0.605
//
// Leave-one-week-out trains on every OTHER week, including later ones. That is
// fine for comparing features but flatters the real job, which only ever has
// the past. Walk-forward - train strictly on earlier weeks - is the number that
// matches how js/scoutPanel.js actually uses this, and it depends heavily on
// how much history exists:
//
//   prior rows available     accuracy   log loss   AUC   (vs baseline log loss)
//     >= 150                  57.1%      0.6780    0.575   (0.6755)
//     >= 400                  57.4%      0.6775    0.552   (0.6709)
//     >= 600                  61.2%      0.6583    0.589   (0.6653)
//
// So it needs roughly a full prior season behind it to be clearly worth
// showing. scoutPanel refuses to render a lean under 400 rows of history.
//
// That table is the whole story. The model RANKS and CALIBRATES better than
// the naive rule (log loss down, AUC up from coin-flip), but it is WORSE at
// hard yes/no calls. With a 60/40 base rate its probabilities rarely cross
// 50%, so thresholding them throws away the thing that actually improved.
//
// Therefore: render the output as a LEAN with a probability and its reasons.
// Never render it as a prediction, a lock, or a confidence percentage that
// implies we know. `confidenceTier()` exists to keep the UI honest.
//
// The thresholds are NOT symmetric about 0.5, and that is measured rather than
// assumed. Everything here is against the spread, so "favourite" and "underdog"
// look like arbitrary labels - but the POOL is not symmetric about them. It lays
// the points 60% of the time, so predicting somebody takes them is a fight with
// the base rate:
//
//   the model says          n     called the side right
//     will LAY             661          62.9%
//     will TAKE            165          50.9%
//
// A dog call is a coin flip. Not for want of training - 329 of the 826 training
// picks took a dog, 39.8% of them - but because the evidence rarely overcomes
// the prior in that direction. So nothing under 0.60 claims anything, whichever
// side it names, and the card prints the side regardless so it can be judged.
//
// By edge on the lay side: 0.10-0.15 is 56.0% (n=116), 0.15-0.25 is 67.2%
// (n=204), 0.25+ is 78.9% (n=90). That is where Slight, Clear and Strong sit. Predicting they will TAKE the points is not: only five
// predictions in the whole 2025 season landed below 40%, so there is no
// evidence either way. Those come back as "No read" WITH A REASON rather than
// as a lean - and rather than as silence, which reads as having nothing to say.
//
// WHY the ceiling is low — measured, not assumed:
//   * A player's lay-rate has split-half reliability r=0.117 (Spearman-Brown
//     0.209). At ~60 picks a season, personal ATS tendency is mostly noise.
//     This is the single biggest limit, and it fixes itself with more seasons.
//   * ESPN's FPI, as a DISAGREEMENT with the market rather than a tip. FPI
//     cannot beat a closing line (48.7% ATS on the 2025 slate, worse than
//     always backing the favourite) but it does predict this pool: when FPI
//     dislikes the favourite by 7+, the league lays it 16.5pp less than the
//     spread alone implies. Adding it moved accuracy 58.6% -> 60.5%, log loss
//     0.6587 -> 0.6558, AUC 0.601 -> 0.605, and the same direction walking
//     forward. Synced by sync_espn_fpi() on pg_cron.
//   * CONFERENCE is the strongest learned feature after the spread, and it
//     generalises to teams the model has never scored (the map is static), so
//     it still works in week 1. Adding it moved AUC 0.574 -> 0.595. Only
//     conferences with real exposure get a coefficient (MIN_CONFERENCE_ROWS):
//     without that floor a 29-row conference earned a +0.9 pull, and two of
//     those stacking sent a week 0 prediction to 14% - somewhere nothing had
//     ever tested. The floor lifts the split-half correlation of the
//     conference block from 0.09 to 0.59.
//   * Team "brand" bias is real and repeatable: split-half r=0.284. The pool
//     over-backs some names and fades others regardless of the number.
//   * Spread size barely matters (lay-rate is 59-62% across every band), but
//     whether the favourite is home or on the road matters a lot: the pool
//     lays road favourites 69.6% of the time and home favourites 55.0%.
//   * AP RANKING adds nothing once the spread is controlled for. Lay-rate is
//     flat whether the favourite is ranked (61.4%), the underdog is (60.0%),
//     both are (61.6%) or neither is (62.3%), and no individual chases the
//     poll significantly (largest effect 1.5 SE across 13 players). The line
//     already prices the rankings in.
//   * Tested and discarded, all noise: tilt after a bad week (-4.9pp +/-3.9),
//     slate balancing (variance matched binomial), and per-player team
//     affinity (59 teams, 29 of them appear exactly once).
//
// Re-run tools/scoutModel.test.mjs after touching any of this; it re-fits the
// season and fails if the cross-validated numbers drift.

import { conferenceOf } from './conferences.js';

const BAND = s => (s < 3 ? 0 : s < 7 ? 1 : s < 12 ? 2 : 3);
const sigmoid = z => 1 / (1 + Math.exp(-z));

// Ridge penalties, chosen by the sweep in the test harness. They are strong on
// purpose: at this sample size the unpenalised fit is worse than guessing.
const L_BRAND       = 20;
const L_PLAYER      = 8;    // the player's own lean
const L_CONFERENCE  = 1;    // league-wide conference pull
const L_PLAYER_CONF = 10;   // their own conference lean, shrunk harder

// A conference needs this many appearances before it gets its own coefficient.
// Ridge shrinks by information, not by sample size, so without a floor a
// conference seen 29 times can still earn a ±0.9 pull — and two of those
// stacking on one game sends a prediction somewhere nothing has ever tested.
// Split-half across the season: SEC (590 rows) and Big 12 (266) hold their sign
// and size, while American (29) swings +0.91 -> -0.17. Excluding just the two
// smallest lifts the split-half correlation of the whole conference block from
// 0.09 to 0.59 - i.e. from noise to something that replicates - and costs
// almost nothing in cross-validation (AUC 0.597 -> 0.595). Below the floor a
// conference contributes nothing rather than a confident guess.
const MIN_CONFERENCE_ROWS = 50;
const L_FPI         = 2;    // one coefficient over every row, lightly shrunk
const L_MOVE        = 2;    // line movement, same shape as FPI and same shrinkage

// HOW THESE PENALTIES WERE CHOSEN.
//
// Grid-searched over brand, conference and player weights, scored two ways: hold
// out one week at a time (LOO, n=826) and walk forward on past weeks only, gated
// at 400 rows of history the way scoutPanel gates it (WF, n=404).
//
//   brand conf player    LOO ll    LOO AUC    WF ll     WF AUC
//     20    1     6      0.6590    0.5999    0.6848    0.5373
//     20    1     8      0.6587    0.6009    0.6849    0.5364   <- chosen
//     20    1    12      0.6585    0.6008    0.6854    0.5337
//     20    2     6      0.6595    0.5974    0.6844    0.5385
//     40    1     8      0.6580    0.6001    0.6832    0.5309
//
// Everything in that table sits inside 0.002 of log loss and 0.005 of AUC of
// everything else. That is hyperparameter noise on 826 rows, not signal, so the
// 4th decimal was not chased: this is simply the best AUC under both scorings,
// and AUC is what the panel actually leans on when it ranks a player against
// the field.
//
// One earlier experiment is worth not repeating. L_PLAYER was pushed to 1 to
// force the thirteen players further apart - it worked (spread 8.6pp -> 25pp)
// but cost real ranking quality (LOO AUC 0.595 -> 0.582, WF worse still), and
// the sides barely moved anyway because the pool's lean on a game is bigger
// than any one person's. 8 keeps most of the separation (about 13pp) while
// being the best-ranking setting on the board, so it is not a compromise
// between the two so much as the point where they agree.

/**
 * Normalise a pick/team name the way the rest of the site does, so
 * "Ole Miss" and "ole miss" land on the same coefficient.
 */
const norm = s => String(s || '').toLowerCase().replace(/[^a-z0-9]/g, '');

/**
 * Turn all_games + picks rows into the model's training rows.
 * Only picked games with a real line can be used — the line defines which
 * side is the favourite, and without it "lays the points" has no meaning.
 */
export function buildTrainingRows(games, picks) {
  const byId = new Map(games.map(g => [g.GameId, g]));
  const rows = [];
  for (const p of picks) {
    const g = byId.get(p.game_id);
    if (!g || !g.picked || g.line == null) continue;
    const line = Number(g.line);
    if (!Number.isFinite(line) || Math.abs(line) < 0.5) continue; // pick'em: no favourite

    const pickedHome = norm(p.pick) === norm(g.Home);
    const pickedAway = norm(p.pick) === norm(g.Away);
    if (!pickedHome && !pickedAway) continue;                     // unmatched name

    // line is home-relative: negative means the home team is favoured.
    const favIsHome = line < 0;
    rows.push({
      pid: p.team_id,
      fpiEdge: fpiEdge(g, line, favIsHome),
      lineMove: lineMove(g, line, favIsHome),
      season: Number(g.cfb_season),
      week: Number(g.week),
      laidPoints: (pickedHome === favIsHome) ? 1 : 0,
      spread: Math.abs(line),
      favHome: favIsHome ? 1 : 0,
      fav: favIsHome ? g.Home : g.Away,
      dog: favIsHome ? g.Away : g.Home
    });
  }
  return rows;
}

/**
 * How far ESPN's FPI disagrees with the market, pointing at the FAVOURITE.
 * Positive means FPI likes the favourite MORE than the line does.
 *
 * Null when no projection is stored, which the model treats as no opinion
 * rather than as agreement - an unsynced game must not look like consensus.
 */
function fpiEdge(g, line, favIsHome) {
  const m = g.fpi_margin;
  if (m == null || m === '') return null;
  const homeMargin = Number(m);
  if (!Number.isFinite(homeMargin)) return null;
  const edgeHome = homeMargin - (-line);       // >0: FPI likes home more than the market
  return favIsHome ? edgeHome : -edgeHome;
}

// Squashed and capped: past about ten points of disagreement the difference
// stops meaning anything, and one wild projection should not dominate the fit.
const FPI_SCALE = 10;
const fpiFeature = e => (e == null ? 0 : Math.max(-1, Math.min(1, e / FPI_SCALE)));

/**
 * How far the line has travelled since we first saw it, pointing at the
 * FAVOURITE. Positive means the favourite has been getting more favoured.
 *
 * `line` is home-relative, so the home team's implied margin is -line and the
 * move in home terms is line_open - line.
 *
 * This is deliberately shipped before it can do anything. Every game currently
 * on record opened at its present number, so the feature is zero everywhere and
 * ridge fits a coefficient of zero - it costs nothing and changes nothing.
 * line_history and line_open are filling from now on, and the first week a line
 * actually moves is the week this starts contributing. Nobody has to switch it
 * on.
 */
function lineMove(g, line, favIsHome) {
  const open = g.line_open;
  if (open == null || open === '') return null;
  const openNum = Number(open);
  if (!Number.isFinite(openNum)) return null;
  const moveHome = openNum - line;
  return favIsHome ? moveHome : -moveHome;
}

// Three points is a big move on a college spread, so that is the scale.
const MOVE_SCALE = 3;
const moveFeature = m => (m == null ? 0 : Math.max(-1, Math.min(1, m / MOVE_SCALE)));

// Design row. Kept sparse-friendly: only a handful of entries are ever
// non-zero, which is what keeps the Newton step cheap enough to run on open.
function designRow(r, teamIx, playerIx, confIx, opts) {
  const T = teamIx.size, P = playerIx.size, C = confIx.size;
  const v = new Float64Array(5 + T + P + P + C + P * C);
  v[0] = 1;
  v[1] = r.favHome ? 0.5 : -0.5;                 // favourite home or on the road
  v[2] = (Math.min(r.spread, 21) - 7) / 7;       // centred spread size
  v[3] = fpiFeature(r.fpiEdge);                  // ESPN's model vs the market
  v[4] = moveFeature(r.lineMove);                // which way the line has travelled
  if (!(opts && opts.skipBrand)) {
    v[5 + teamIx.get(r.fav)] += 1;               // brand: backed
    v[5 + teamIx.get(r.dog)] -= 1;               // brand: faded
  }
  v[5 + T + playerIx.get(r.pid)] = 1;            // player's own lay lean
  v[5 + T + P + playerIx.get(r.pid)] = r.favHome ? 0.5 : -0.5; // their home/road lean

  // Conference. Same +1/-1 shape as brand: which conference is being backed,
  // which is being faded. This is the single biggest feature after the spread.
  const base = 5 + T + P + P;
  const cf = confIx.get(conferenceOf(r.fav));
  const cd = confIx.get(conferenceOf(r.dog));
  if (cf != null) v[base + cf] += 1;
  if (cd != null) v[base + cd] -= 1;
  const off = base + C + playerIx.get(r.pid) * C;
  if (cf != null) v[off + cf] += 1;
  if (cd != null) v[off + cd] -= 1;
  return v;
}

function penalties(T, P, C) {
  const pen = new Float64Array(5 + T + P + P + C + P * C);
  pen[0] = 0; pen[1] = 0.01; pen[2] = 0.01; pen[3] = L_FPI; pen[4] = L_MOVE;
  for (let i = 0; i < T; i++) pen[5 + i] = L_BRAND;
  for (let i = 0; i < P + P; i++) pen[5 + T + i] = L_PLAYER;
  const base = 5 + T + P + P;
  for (let i = 0; i < C; i++) pen[base + i] = L_CONFERENCE;
  for (let i = 0; i < P * C; i++) pen[base + C + i] = L_PLAYER_CONF;
  return pen;
}

// Penalised Newton/IRLS. Small and dense enough (~75 coefficients) to solve
// directly; the alternative closed-form shrinkage estimator was measurably
// worse (log loss 0.682 vs 0.666), so the matrix solve earns its keep.
function fitRidge(X, y, pen) {
  const d = pen.length;
  const w = new Float64Array(d);
  for (let iter = 0; iter < 50; iter++) {
    const g = new Float64Array(d);
    const H = Array.from({ length: d }, () => new Float64Array(d));
    for (let i = 0; i < X.length; i++) {
      const xi = X[i];
      let s = 0;
      for (let j = 0; j < d; j++) if (xi[j]) s += w[j] * xi[j];
      const p = sigmoid(s), resid = p - y[i], wt = Math.max(p * (1 - p), 1e-6);
      for (let j = 0; j < d; j++) {
        if (!xi[j]) continue;
        g[j] += resid * xi[j];
        for (let k = 0; k < d; k++) if (xi[k]) H[j][k] += wt * xi[j] * xi[k];
      }
    }
    for (let j = 0; j < d; j++) { g[j] += pen[j] * w[j]; H[j][j] += pen[j] + 1e-8; }

    // Gauss-Jordan on [H | g]
    const A = [];
    for (let j = 0; j < d; j++) { const row = new Float64Array(d + 1); row.set(H[j]); row[d] = g[j]; A.push(row); }
    for (let c = 0; c < d; c++) {
      let piv = c;
      for (let r2 = c + 1; r2 < d; r2++) if (Math.abs(A[r2][c]) > Math.abs(A[piv][c])) piv = r2;
      [A[c], A[piv]] = [A[piv], A[c]];
      if (Math.abs(A[c][c]) < 1e-12) continue;
      for (let r2 = 0; r2 < d; r2++) {
        if (r2 === c) continue;
        const f = A[r2][c] / A[c][c];
        if (!f) continue;
        for (let k = c; k <= d; k++) A[r2][k] -= f * A[c][k];
      }
    }
    let maxStep = 0;
    for (let j = 0; j < d; j++) { const dx = A[j][d] / A[j][j]; w[j] -= dx; maxStep = Math.max(maxStep, Math.abs(dx)); }
    if (maxStep < 1e-9) break;
  }
  return w;
}

/**
 * Fit the model. Returns an object with predict() plus the interpretable
 * pieces the UI needs to explain itself — a bare probability with no reason
 * attached is exactly the kind of number nobody should trust.
 */
export function fitScoutModel(rows) {
  if (!rows.length) return null;
  const teamNames  = [...new Set(rows.flatMap(r => [r.fav, r.dog]))].sort();
  const playerIds  = [...new Set(rows.map(r => r.pid))].sort((a, b) => a - b);
  const confSeen = {};
  for (const r of rows) {
    for (const t of [r.fav, r.dog]) {
      const c = conferenceOf(t);
      if (c) confSeen[c] = (confSeen[c] || 0) + 1;
    }
  }
  const confNames = Object.keys(confSeen).filter(c => confSeen[c] >= MIN_CONFERENCE_ROWS).sort();
  const teamIx     = new Map(teamNames.map((t, i) => [t, i]));
  const playerIx   = new Map(playerIds.map((p, i) => [p, i]));
  const confIx     = new Map(confNames.map((c, i) => [c, i]));
  const T = teamIx.size, P = playerIx.size, C = confIx.size;

  const X = rows.map(r => designRow(r, teamIx, playerIx, confIx));
  const y = rows.map(r => r.laidPoints);
  const w = fitRidge(X, y, penalties(T, P, C));

  const leagueLayRate = y.reduce((a, b) => a + b, 0) / y.length;
  const exposure = {};
  for (const r of rows) { exposure[r.fav] = (exposure[r.fav] || 0) + 1; exposure[r.dog] = (exposure[r.dog] || 0) + 1; }

  return {
    leagueLayRate,
    teamIx, playerIx, confIx, weights: w,
    /** log-odds nudge for backing this team, and how much data stands behind it */
    brand(team) {
      const i = teamIx.get(team);
      return { pull: i == null ? 0 : w[5 + i], n: exposure[team] || 0 };
    },
    /** this player's own lean, over and above the game itself */
    playerLean(pid) {
      const i = playerIx.get(pid);
      return i == null ? 0 : w[5 + T + i];
    },
    /** league-wide pull toward or away from a conference */
    conferencePull(conference) {
      const i = confIx.get(conference);
      return i == null ? 0 : w[5 + T + P + P + i];
    },
    /** one player's own lean toward or away from a conference */
    playerConferencePull(pid, conference) {
      const ci = confIx.get(conference), pi = playerIx.get(pid);
      if (ci == null || pi == null) return 0;
      return w[5 + T + P + P + C + pi * C + ci];
    },
    /**
     * P(player lays the points) for one upcoming game.
     * game: { Home, Away, line }
     */
    predict(pid, game) {
      const line = Number(game.line);
      if (!Number.isFinite(line) || Math.abs(line) < 0.5) return null;
      const favHome = line < 0;
      const r = {
        pid,
        spread: Math.abs(line),
        favHome: favHome ? 1 : 0,
        fav: favHome ? game.Home : game.Away,
        dog: favHome ? game.Away : game.Home,
        fpiEdge: fpiEdge(game, line, favHome),
        lineMove: lineMove(game, line, favHome)
      };
      // A player we have never seen has no profile at all — nothing to say.
      if (!playerIx.has(pid)) {
        return { p: this.leagueLayRate, fav: r.fav, dog: r.dog, spread: r.spread, unseen: true };
      }
      // A team we have never seen is NOT a dead end. Its brand coefficient is
      // simply unknown, but its CONFERENCE is known from the static map, and
      // the spread, home/road and player terms all still apply. Week 1 of a new
      // season is full of teams the model has never scored, so degrading
      // gracefully here is the difference between a usable feature and a blank
      // one. `newTeams` tells the UI to soften its language.
      const newTeams = [r.fav, r.dog].filter(t => !teamIx.has(t));
      const v = designRow(r, teamIx, playerIx, confIx, { skipBrand: newTeams.length > 0 });
      let s = 0;
      for (let j = 0; j < v.length; j++) if (v[j]) s += w[j] * v[j];
      return {
        p: sigmoid(s), fav: r.fav, dog: r.dog, spread: r.spread, favHome,
        unseen: false, newTeams
      };
    }
  };
}

/**
 * Keep the UI honest. The cross-validated model is barely better than a coin
 * at hard calls, so anything inside 45-55% must read as a coin flip, and
 * nothing may ever read as certain.
 */
export function confidenceTier(p) {
  // These thresholds are MEASURED, not chosen for feel. Binning the
  // cross-validated predictions against what actually happened:
  //
  //   predicted   actual     verdict
  //     36%        50%       wrong direction  <- do not show
  //     47%        53%       wrong direction  <- do not show
  //     57%        51%       no signal        <- do not show
  //     62%        63%       tracks reality
  //     67%        66%       tracks reality
  //     74%        73%       tracks reality
  //     87%        86%       tracks reality
  //
  // The model can spot a player who is likely to LAY the points. It cannot
  // spot one who is likely to TAKE them — below ~60% the predictions do not
  // track reality at all. So everything under 0.60 is a coin flip, however
  // far under it sits, and the band is deliberately asymmetric.
  // Every game names a side. The badge is what says how much that name is worth,
  // and `hit` is not a vibe - it is how often this badge picked the right side
  // across all 826 cross-validated predictions from last season. A coin flip is
  // labelled a coin flip precisely so nobody reads it as a call.
  if (p >= 0.75) return { tier: 'strong', label: 'Strong lean', hit: 79 };
  if (p >= 0.65) return { tier: 'clear',  label: 'Clear lean',  hit: 67 };
  if (p >= 0.60) return { tier: 'slight', label: 'Slight lean', hit: 56 };
  // No special case below 0.5 any more. There used to be an "Untested" tier for
  // dog calls, on the grounds that almost none had ever been graded. Enough have
  // now - 165 of them - and they come in at 50.9%. Tested, and worthless. One
  // coin-flip tier covers both directions, and the card still names a side.
  return { tier: 'coin-flip', label: 'Coin flip', hit: 52 };
}

/**
 * Plain-English reasons behind a lean. These are worth more than the number
 * itself: each one is a measured effect the user can sanity-check, and they
 * stay meaningful even when the probability sits near 50%.
 */
export function explain(model, pid, prediction, opts) {
  if (!model || !prediction || prediction.unseen) return [];
  const out = [];

  const favBrand = model.brand(prediction.fav);
  const dogBrand = model.brand(prediction.dog);
  if (favBrand.n >= 20 && Math.abs(favBrand.pull) > 0.12) {
    out.push({
      kind: favBrand.pull > 0 ? 'up' : 'down',
      text: `The pool ${favBrand.pull > 0 ? 'over-backs' : 'fades'} ${prediction.fav}`,
      detail: `across ${favBrand.n} exposures`
    });
  }
  if (dogBrand.n >= 20 && Math.abs(dogBrand.pull) > 0.12) {
    out.push({
      kind: dogBrand.pull > 0 ? 'down' : 'up',
      text: `The pool ${dogBrand.pull > 0 ? 'over-backs' : 'fades'} ${prediction.dog}`,
      detail: `across ${dogBrand.n} exposures`
    });
  }
  // Conference pull — the strongest single addition after the spread itself.
  const favConf = conferenceOf(prediction.fav);
  const dogConf = conferenceOf(prediction.dog);
  if (favConf && favConf !== dogConf) {
    const pull = model.conferencePull(favConf) + model.playerConferencePull(pid, favConf);
    if (Math.abs(pull) > 0.06) {
      out.push({
        kind: pull > 0 ? 'up' : 'down',
        text: `${pull > 0 ? 'Backs' : 'Fades'} the ${favConf}`,
        detail: `${prediction.fav}'s conference`
      });
    }
  }
  if (dogConf && dogConf !== favConf) {
    const pull = model.conferencePull(dogConf) + model.playerConferencePull(pid, dogConf);
    if (Math.abs(pull) > 0.06) {
      out.push({
        kind: pull > 0 ? 'down' : 'up',
        text: `${pull > 0 ? 'Backs' : 'Fades'} the ${dogConf}`,
        detail: `${prediction.dog}'s conference`
      });
    }
  }

  out.push(prediction.favHome
    ? { kind: 'down', text: 'Favourite is at home', detail: 'the pool lays home favourites just 55%' }
    : { kind: 'up',   text: 'Favourite is on the road', detail: 'the pool lays road favourites 70%' });

  // The picks page asks for a FIELD read, where a single player's lean has no
  // business appearing. Everything above it is about the game and applies either
  // way.
  const lean = (opts && opts.includePlayer === false) ? 0 : model.playerLean(pid);
  if (Math.abs(lean) > 0.08) {
    out.push({
      kind: lean > 0 ? 'up' : 'down',
      text: lean > 0 ? 'He leans toward laying the points' : 'He leans toward taking the points',
      detail: 'personal lean — weakest signal here, one season only'
    });
  }
  return out;
}
