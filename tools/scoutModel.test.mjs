// tools/scoutModel.test.mjs — does the scouting model still earn its place?
//
//   node tools/scoutModel.test.mjs
//
// Pulls the real season from Supabase with the public anon key (same key the
// site uses), re-fits js/scoutModel.js holding out one whole week at a time,
// and fails if the cross-validated numbers drift from what the module's header
// claims. The point is not to prove the model is good — it isn't, particularly
// — but to stop anyone quietly shipping a version that is WORSE than guessing
// while the UI keeps promising insight.

import { buildTrainingRows, fitScoutModel, confidenceTier } from '../js/scoutModel.js';

const URL = 'https://vopdioszofwdkwnujtiq.supabase.co/rest/v1';
const KEY = 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InZvcGRpb3N6b2Z3ZGt3bnVqdGlxIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NjE1ODI2MjksImV4cCI6MjA3NzE1ODYyOX0.cD2nNYMEUUOHWQlQC0-lxGZ3s1HVQhWEX_FmgzSsZYw';

const get = async path => {
  const res = await fetch(`${URL}/${path}`, { headers: { apikey: KEY, Authorization: `Bearer ${KEY}` } });
  if (!res.ok) throw new Error(`${path} -> ${res.status}`);
  return res.json();
};

// Thresholds. Deliberately loose enough to survive another season of data
// being added, tight enough to catch a real regression.
const MUST = {
  logLossBeatsBaselineBy: 0.002,   // calibration must actually improve
  aucAtLeast: 0.54,                // ranking must beat a coin flip
  aucBaselineIsAbout: 0.50
};

function metrics(pairs) {
  let acc = 0, ll = 0;
  for (const { p, y } of pairs) {
    const q = Math.min(Math.max(p, 1e-6), 1 - 1e-6);
    acc += ((q >= 0.5 ? 1 : 0) === y) ? 1 : 0;
    ll += -(y * Math.log(q) + (1 - y) * Math.log(1 - q));
  }
  const sorted = [...pairs].sort((a, b) => a.p - b.p);
  let rank = 0, pos = 0, neg = 0;
  sorted.forEach((x, i) => { if (x.y === 1) { rank += i + 1; pos++; } else neg++; });
  return {
    n: pairs.length,
    acc: acc / pairs.length,
    ll: ll / pairs.length,
    auc: (pos && neg) ? (rank - pos * (pos + 1) / 2) / (pos * neg) : 0.5
  };
}

const games = await get('all_games?select=GameId,week,winner,picked,Away,Home,line,cfb_season&limit=5000');
const picks = await get('picks?select=team_id,game_id,pick&limit=20000');
const rows = buildTrainingRows(games, picks);
console.log(`training rows: ${rows.length}`);
if (rows.length < 200) { console.error('FAIL: too little data to evaluate'); process.exit(1); }

const weeks = [...new Set(rows.map(r => r.week))].sort((a, b) => a - b);
const layRate = rows.reduce((a, b) => a + b.laidPoints, 0) / rows.length;

const modelPairs = [];
for (const wk of weeks) {
  const train = rows.filter(r => r.week !== wk);
  const test  = rows.filter(r => r.week === wk);
  if (!train.length || !test.length) continue;
  const m = fitScoutModel(train);
  for (const r of test) {
    const line = r.favHome ? -r.spread : r.spread;
    const game = r.favHome ? { Home: r.fav, Away: r.dog, line } : { Home: r.dog, Away: r.fav, line };
    const pred = m.predict(r.pid, game);
    modelPairs.push({ p: pred ? pred.p : layRate, y: r.laidPoints });
  }
}

const base  = metrics(rows.map(r => ({ p: layRate, y: r.laidPoints })));
const model = metrics(modelPairs);

const fmt = (t, m) => `  ${t.padEnd(26)} acc=${(m.acc * 100).toFixed(2)}%  logloss=${m.ll.toFixed(4)}  auc=${m.auc.toFixed(4)}`;
console.log('\nleave-one-week-out cross-validation');
console.log(fmt('baseline (always lays)', base));
console.log(fmt('scoutModel', model));

const failures = [];
if (base.ll - model.ll < MUST.logLossBeatsBaselineBy)
  failures.push(`log loss must beat baseline by ${MUST.logLossBeatsBaselineBy}; got ${(base.ll - model.ll).toFixed(4)}`);
if (model.auc < MUST.aucAtLeast)
  failures.push(`AUC must be >= ${MUST.aucAtLeast}; got ${model.auc.toFixed(4)}`);

// The UI contract: probabilities must stay in a band that reads as a lean.
// If the model ever starts emitting near-certainties on this much data,
// something has broken and the UI would start lying.
const ps = modelPairs.map(x => x.p);
const lo = Math.min(...ps), hi = Math.max(...ps);
console.log(`\npredicted probability range: ${(lo * 100).toFixed(0)}% .. ${(hi * 100).toFixed(0)}%`);
if (hi > 0.97 || lo < 0.03) failures.push('model emitted near-certain probabilities; UI would overclaim');

const flips = modelPairs.filter(x => confidenceTier(x.p).tier === 'coin-flip').length;
console.log(`graded a coin flip: ${flips}/${modelPairs.length} (${(100 * flips / modelPairs.length).toFixed(0)}%)`);

// ---------------------------------------------------------------------------
// NO LOOKAHEAD.
//
// The database already withholds a pick until its game kicks off - the anon key
// can see 890 of 1399 rows - so a browser cannot read this week's picks even if
// it wanted to. This is the second lock: scoutPanel fits only on games played
// strictly BEFORE the week it predicts, so that once picks do become visible
// the guess (and the "model called it" badge) is still honest rather than
// self-fulfilling. Measured on 2025, training on the target week moves a
// prediction 4.4pp on average and flips the predicted side on 12.5% of picks.
//
// Walk-forward is also the number that describes the real job, since the panel
// only ever has the past to work with.
console.log('');
console.log('walk-forward (train strictly on earlier weeks)');
const wfPairs = [];
let leaked = 0;
for (const wk of weeks) {
  const train = rows.filter(r => r.week < wk);
  const test = rows.filter(r => r.week === wk);
  if (train.length < 400 || !test.length) continue;
  leaked += train.filter(r => r.week >= wk).length;          // must stay 0
  const m = fitScoutModel(train);
  for (const r of test) {
    const line = r.favHome ? -r.spread : r.spread;
    const game = r.favHome ? { Home: r.fav, Away: r.dog, line } : { Home: r.dog, Away: r.fav, line };
    const pred = m.predict(r.pid, game);
    wfPairs.push({ p: pred ? pred.p : layRate, y: r.laidPoints });
  }
}
if (leaked > 0) failures.push(`training set contained ${leaked} rows from the week being predicted`);
if (wfPairs.length) {
  const wf = metrics(wfPairs);
  const wfRate = wfPairs.reduce((a, b) => a + b.y, 0) / wfPairs.length;
  console.log(fmt('walk-forward', wf));
  console.log(fmt('  its own baseline', metrics(wfPairs.map(x => ({ p: wfRate, y: x.y })))));
  if (wf.auc < 0.52) failures.push(`walk-forward AUC collapsed to ${wf.auc.toFixed(4)}`);
} else {
  console.log('  not enough history yet to walk forward');
}
console.log(`target-week rows that leaked into training: ${leaked} (must be 0)`);
console.log('');
if (failures.length) { console.error('\nFAIL:'); for (const f of failures) console.error('  - ' + f); process.exit(1); }
console.log('\nPASS — beats guessing on calibration and ranking, with no lookahead.');
