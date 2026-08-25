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
//   this model, leave-one-week-out       57.9%      0.6684    0.582
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
// It is also deliberately ASYMMETRIC. Predicting someone will LAY the points is
// well tested - 56-75% right across 390 cross-validated predictions at an edge
// of 0.10 or more, and 75% on the 137 that reach a Strong lean. Predicting they will TAKE the points is not: only five
// predictions in the whole 2025 season landed below 40%, so there is no
// evidence either way. Those come back as "No read" WITH A REASON rather than
// as a lean - and rather than as silence, which reads as having nothing to say.
//
// WHY the ceiling is low — measured, not assumed:
//   * A player's lay-rate has split-half reliability r=0.117 (Spearman-Brown
//     0.209). At ~60 picks a season, personal ATS tendency is mostly noise.
//     This is the single biggest limit, and it fixes itself with more seasons.
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
const L_PLAYER      = 1;    // the player's own lean - weighted hard, see note below
const L_CONFERENCE  = 3;    // league-wide conference pull — the strongest addition
const L_PLAYER_CONF = 1.5;  // their own conference lean

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

// HOW HARD THE PERSON COUNTS AGAINST THE POOL.
//
// L_PLAYER started at 20, which is what the cross-validation likes: personal
// tendency is barely reliable (split-half r=0.117), so the maths wants it
// shrunk almost to nothing. The cost was that all thirteen players came out
// within 8.6 points of each other and never disagreed - a "scouting report"
// that said the same thing about everybody.
//
// So it is deliberately turned up, and 1 is where it stops: the furthest the
// person can be weighted while the model is still BETTER CALIBRATED THAN
// GUESSING. Log loss 0.6684 against a 0.6723 baseline. At 0.5 it becomes 0.6765
// and crosses that line, which is the point where the numbers start lying.
//
//   L_PLAYER   spread across players   log loss   AUC     "Strong lean" tier
//     20              8.6pp             0.6598    0.595   75% right on n=36
//      4             16.0pp             0.6608    0.593   73% on n=95
//      1             25.0pp             0.6684    0.582   75% on n=137
//      0.5           31.6pp             0.6765    0.575   75% on n=147  <- too far
//
// Worth knowing what this does and does not buy. Sides still almost never
// differ between players - the pool's lean on a given game is simply bigger
// than any one person's - so what widens is CONVICTION, not the pick. Ranking
// gets slightly worse (AUC 0.595 -> 0.582) and that is the real price. What
// improves is coverage: four times as many games earn a Strong lean, and that
// badge still calls the right side 75% of the time.

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

// Design row. Kept sparse-friendly: only a handful of entries are ever
// non-zero, which is what keeps the Newton step cheap enough to run on open.
function designRow(r, teamIx, playerIx, confIx, opts) {
  const T = teamIx.size, P = playerIx.size, C = confIx.size;
  const v = new Float64Array(3 + T + P + P + C + P * C);
  v[0] = 1;
  v[1] = r.favHome ? 0.5 : -0.5;                 // favourite home or on the road
  v[2] = (Math.min(r.spread, 21) - 7) / 7;       // centred spread size
  if (!(opts && opts.skipBrand)) {
    v[3 + teamIx.get(r.fav)] += 1;               // brand: backed
    v[3 + teamIx.get(r.dog)] -= 1;               // brand: faded
  }
  v[3 + T + playerIx.get(r.pid)] = 1;            // player's own lay lean
  v[3 + T + P + playerIx.get(r.pid)] = r.favHome ? 0.5 : -0.5; // their home/road lean

  // Conference. Same +1/-1 shape as brand: which conference is being backed,
  // which is being faded. This is the single biggest feature after the spread.
  const base = 3 + T + P + P;
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
  const pen = new Float64Array(3 + T + P + P + C + P * C);
  pen[0] = 0; pen[1] = 0.01; pen[2] = 0.01;
  for (let i = 0; i < T; i++) pen[3 + i] = L_BRAND;
  for (let i = 0; i < P + P; i++) pen[3 + T + i] = L_PLAYER;
  const base = 3 + T + P + P;
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
      return { pull: i == null ? 0 : w[3 + i], n: exposure[team] || 0 };
    },
    /** this player's own lean, over and above the game itself */
    playerLean(pid) {
      const i = playerIx.get(pid);
      return i == null ? 0 : w[3 + T + i];
    },
    /** league-wide pull toward or away from a conference */
    conferencePull(conference) {
      const i = confIx.get(conference);
      return i == null ? 0 : w[3 + T + P + P + i];
    },
    /** one player's own lean toward or away from a conference */
    playerConferencePull(pid, conference) {
      const ci = confIx.get(conference), pi = playerIx.get(pid);
      if (ci == null || pi == null) return 0;
      return w[3 + T + P + P + C + pi * C + ci];
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
        dog: favHome ? game.Away : game.Home
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
  if (p >= 0.75) return { tier: 'strong', label: 'Strong lean', hit: 75 };
  if (p >= 0.65) return { tier: 'clear',  label: 'Clear lean',  hit: 68 };
  if (p >= 0.60) return { tier: 'slight', label: 'Slight lean', hit: 56 };
  // Two very different reasons to stay quiet, and the UI should not render them
  // the same way. Under 0.40 the model has a real opinion - it thinks they will
  // TAKE the points - but across the whole 2025 season only five predictions
  // ever landed there, so that direction has never been tested. That is
  // untested, not absent, and a card saying nothing implies the wrong one.
  // Below 0.40 the model is NOT ambivalent - it is fairly sure they take the
  // points. What is missing is any track record: five predictions all season
  // landed here. "Coin flip" would misdescribe that (it implies no opinion) and
  // so would a lean badge (it implies a tested one). Hence its own label, and
  // no hit rate quoted, because n=5 is not a number worth printing.
  if (p <= 0.40) return { tier: 'untested', label: 'Untested', reason: 'leans-dog' };
  return { tier: 'coin-flip', label: 'Coin flip', hit: 50, reason: 'too-close' };
}

/**
 * Plain-English reasons behind a lean. These are worth more than the number
 * itself: each one is a measured effect the user can sanity-check, and they
 * stay meaningful even when the probability sits near 50%.
 */
export function explain(model, pid, prediction) {
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

  const lean = model.playerLean(pid);
  if (Math.abs(lean) > 0.08) {
    out.push({
      kind: lean > 0 ? 'up' : 'down',
      text: lean > 0 ? 'He leans toward laying the points' : 'He leans toward taking the points',
      detail: 'personal lean — weakest signal here, one season only'
    });
  }
  return out;
}
