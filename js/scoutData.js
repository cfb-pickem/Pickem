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
import { buildTrainingRows, fitScoutModel, explain } from './scoutModel.js';

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
        .select('GameId, Away, Home, line, winner, picked, week, cfb_season, fpi_margin')
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
    spread: sample.spread,
    // No player term - this is the field, not a person.
    reasons: explain(model, ids[0], sample, { includePlayer: false })
  };
}
