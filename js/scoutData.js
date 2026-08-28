// js/scoutData.js — the fitted model, shared by the leaderboard and the picks page.
//
// Both surfaces want the same thing: what does the pool historically do with a
// game like this one. Fitting is not free (a Newton solve over ~900 rows), and
// fetching the history twice would be worse, so the raw data is loaded once per
// page and a model is memoised per target week.
//
// STRICTLY NO LOOKAHEAD, and it matters more here than on the leaderboard. The
// picks page is open while the week is still live, so a model that had seen this
// week's picks would be reflecting other players' hidden choices straight back
// at you. modelFor() only ever trains on weeks that finished before the one
// being asked about.

import { supabase } from './supabaseClient.js';
import { buildTrainingRows, fitScoutModel, explain, confidenceTier } from './scoutModel.js';

let trainingPromise = null;
const modelByWeek = new Map();

// Below this much history the fit is not worth showing: walk-forward on 2025,
// under ~150 rows the probabilities are wildly overconfident (log loss 0.78
// against a 0.67 baseline), and it takes roughly a full prior season to beat
// guessing convincingly.
export const MIN_HISTORY_ROWS = 400;

export function loadTrainingData() {
  if (trainingPromise) return trainingPromise;
  trainingPromise = (async () => {
    const [{ data: games, error: gErr }, { data: picks, error: pErr }] = await Promise.all([
      supabase.from('all_games')
        .select('GameId, Away, Home, line, line_open, winner, picked, week, cfb_season, fpi_margin')
        .eq('picked', true),
      supabase.from('picks').select('team_id, game_id, pick')
    ]);
    if (gErr || pErr) throw (gErr || pErr);
    const rows = buildTrainingRows(games || [], picks || []);
    return { rows, games: games || [], picks: picks || [] };
  })().catch(err => { trainingPromise = null; throw err; });
  return trainingPromise;
}

/**
 * Fit on everything that finished strictly before (season, week).
 * Returns null when there is not enough history to say anything honest.
 */
export function modelFor(data, season, week) {
  const key = `${season}|${week}`;
  if (modelByWeek.has(key)) return modelByWeek.get(key);
  const past = data.rows.filter(r =>
    r.season < season || (r.season === season && r.week < week)
  );
  const model = past.length >= MIN_HISTORY_ROWS ? fitScoutModel(past) : null;
  modelByWeek.set(key, model);
  return model;
}

/**
 * What is the rest of the league likely to do with this game?
 *
 * `exclude` is the viewer's own team, and leaving it out is deliberate: this is
 * meant to describe your OPPONENTS. Including yourself would also hand you your
 * own number, which is the one thing the scouting report refuses to show you -
 * knowing the model's read on you is an invitation to pick against it purely to
 * stay unreadable.
 *
 * game: { Home, Away, line }
 */
export function fieldRead(model, game, exclude) {
  if (!model) return null;
  const ids = [...model.playerIx.keys()].filter(id => Number(id) !== Number(exclude));
  if (ids.length < 3) return null;

  const preds = ids.map(id => model.predict(id, game)).filter(q => q && !q.unseen);
  if (preds.length < 3) return null;

  const mean = preds.reduce((a, q) => a + q.p, 0) / preds.length;
  const laying = mean >= 0.5;
  const sample = preds[0];
  return {
    n: preds.length,
    side: laying ? sample.fav : sample.dog,
    // The share of the field expected on that side, which is what a pick'em
    // player actually wants: how contrarian am I about to be.
    pct: Math.round((laying ? mean : 1 - mean) * 100),
    laying,
    // Whether this read sits in the direction the model has actually been graded
    // on. It is well tested predicting the field LAYS the points; the other way
    // round it has almost no record (17 predictions all last season, 53% right),
    // so the picks page says so rather than quoting a bare number.
    tested: mean > 0.40,
    // Bigger than any spread the league has ever picked, so the model is
    // extrapolating rather than reading. Carried out here so the card can say
    // so instead of printing a confident-looking split.
    beyondSpread: !!sample.beyondSpread,
    spread: sample.spread,
    // No player term - this is the field, not a person.
    reasons: explain(model, ids[0], sample, { includePlayer: false })
  };
}


/**
 * What each confidence badge is ACTUALLY worth, measured on this league's own
 * results rather than quoted from a backtest that shipped with the code.
 *
 * The numbers baked into confidenceTier() came from cross-validating the 2025
 * season while building the thing. They were honest then and they go stale the
 * moment this league plays a week they do not include - so the panel prefers
 * this, and falls back to the baked-in figures only until there is enough
 * played to say anything better.
 *
 * Method: hold out the most recently completed weeks, fit on everything before
 * them, and grade the held-out picks. One extra fit rather than the ~20 a full
 * leave-one-week-out would need, because this runs in somebody's browser while
 * they are trying to read a scouting report.
 *
 * Returns null when there is not enough played, which is the correct answer
 * early in a first season.
 */
const HOLDOUT_WEEKS = 8;
const MIN_GRADED = 150;
let accuracyPromise = null;

export function badgeAccuracy(data) {
  if (accuracyPromise) return accuracyPromise;
  accuracyPromise = (async () => {
    const cached = readCachedAccuracy(data.rows.length);
    if (cached) return cached;

    // Chronological, across seasons: 2025 week 14 comes before 2026 week 1.
    const keyOf = r => r.season * 100 + r.week;
    const keys = [...new Set(data.rows.map(keyOf))].sort((a, b) => a - b);
    if (keys.length <= HOLDOUT_WEEKS) return null;

    const heldFrom = keys[keys.length - HOLDOUT_WEEKS];
    const train = data.rows.filter(r => keyOf(r) < heldFrom);
    const test  = data.rows.filter(r => keyOf(r) >= heldFrom);
    if (train.length < MIN_HISTORY_ROWS || test.length < MIN_GRADED) return null;

    const model = fitScoutModel(train);
    if (!model) return null;

    const tally = {};
    for (const r of test) {
      const line = r.favHome ? -r.spread : r.spread;
      const game = r.favHome
        ? { Home: r.fav, Away: r.dog, line }
        : { Home: r.dog, Away: r.fav, line };
      if (r.fpiEdge != null) {
        const edgeHome = r.favHome ? r.fpiEdge : -r.fpiEdge;
        game.fpi_margin = edgeHome + (-line);
      }
      if (r.lineMove != null) {
        const moveHome = r.favHome ? r.lineMove : -r.lineMove;
        game.line_open = moveHome + line;
      }
      const p = model.predict(r.pid, game);
      if (!p || p.unseen) continue;
      const tier = confidenceTier(p.p).tier;
      tally[tier] = tally[tier] || { n: 0, hit: 0 };
      tally[tier].n++;
      if ((p.p >= 0.5 ? 1 : 0) === r.laidPoints) tally[tier].hit++;
    }

    const out = { tiers: tally, weeks: HOLDOUT_WEEKS, graded: test.length };
    writeCachedAccuracy(data.rows.length, out);
    return out;
  })().catch(() => null);
  return accuracyPromise;
}

// Cached against the row count, so it recomputes exactly when a new week of
// picks lands and not on every page view.
const ACC_KEY = 'cfb-badge-accuracy-v1';
function readCachedAccuracy(rowCount) {
  try {
    const raw = sessionStorage.getItem(ACC_KEY);
    if (!raw) return null;
    const v = JSON.parse(raw);
    return v && v.rowCount === rowCount ? v.value : null;
  } catch { return null; }
}
function writeCachedAccuracy(rowCount, value) {
  try { sessionStorage.setItem(ACC_KEY, JSON.stringify({ rowCount, value })); } catch {}
}
