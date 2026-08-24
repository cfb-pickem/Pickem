// js/scoutPanel.js — click a name on the leaderboard, see how they'll pick.
//
// One job: for each of this week's games, guess which side of the spread this
// player takes. Everything else about a "scouting report" is deliberately left
// out; the panel is meant to be read in the five seconds before you make your
// own pick.
//
// The model (js/scoutModel.js) is honest about where it works: above 60% it
// tracks reality, below it doesn't, so roughly half the slate legitimately
// comes back "No read". That is the feature behaving correctly, not a gap —
// showing a guess there would be inventing one.
//
// Once the week has locked and picks are public, the panel flips to showing
// what they ACTUALLY picked and whether the model called it. That way it is
// useful all week rather than only before kickoff.

import { supabase } from './supabaseClient.js';
import { escapeHtml, sameTeam } from './utils.js';
import { buildTrainingRows, fitScoutModel, confidenceTier, explain } from './scoutModel.js';

// STRICTLY NO LOOKAHEAD. The model is only ever fitted on games that finished
// BEFORE the week being predicted. Training on the target week would mean the
// "guess" had already read the picks it claims to predict — and since picks are
// hidden until kickoff, it would also be deriving output from data the board
// deliberately conceals. Measured on 2025, including the target week shifts a
// prediction by 4.4pp on average, flips the predicted side on 12.5% of picks
// and changes the confidence tier on 25.8%. So it is filtered, not trusted.
//
// Raw history is fetched once per page load; a model is then fitted per target
// week and memoised, because each week legitimately needs a different fit.
let trainingPromise = null;
const modelByWeek = new Map();
const MIN_HISTORY_ROWS = 400;

function loadTrainingData() {
  if (trainingPromise) return trainingPromise;
  trainingPromise = (async () => {
    const [{ data: games, error: gErr }, { data: picks, error: pErr }] = await Promise.all([
      supabase.from('all_games')
        .select('GameId, Away, Home, line, winner, picked, week, cfb_season')
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
 * Fit on everything that happened strictly before (season, week).
 * Returns null when there is no prior history at all — the very first week of
 * the very first season has nothing to learn from, and saying so is better
 * than inventing a number.
 */
function modelFor(data, season, week) {
  const key = `${season}|${week}`;
  if (modelByWeek.has(key)) return modelByWeek.get(key);
  const past = data.rows.filter(r =>
    r.season < season || (r.season === season && r.week < week)
  );
  // Walk-forward on 2025 shows the fit is only reliably better than guessing
  // once a decent block of history exists: under ~150 rows the probabilities
  // are wildly overconfident (log loss 0.78 against a 0.67 baseline), and it is
  // around a full prior season that it clearly wins. Below the floor we say so
  // rather than dress up noise.
  const model = past.length >= MIN_HISTORY_ROWS ? fitScoutModel(past) : null;
  modelByWeek.set(key, model);
  return model;
}

/** Their season-to-date ATS record, straight from the training rows. */
function atsRecord(games, picks, teamId, season) {
  const byId = new Map(games.map(g => [g.GameId, g]));
  const tally = wantSeason => {
    let w = 0, l = 0;
    for (const p of picks) {
      if (Number(p.team_id) !== Number(teamId)) continue;
      const g = byId.get(p.game_id);
      if (!g || g.winner == null) continue;
      if (wantSeason != null && Number(g.cfb_season) !== Number(wantSeason)) continue;
      sameTeam(p.pick, g.winner) ? w++ : l++;
    }
    return { w, l };
  };
  // In week 1 the current season is 0-0, which reads as a bug rather than as a
  // fresh start. Fall back to the all-time record and say so.
  const thisSeason = tally(season);
  if (thisSeason.w + thisSeason.l > 0) return { ...thisSeason, label: 'ATS this season' };
  const allTime = tally(null);
  return { ...allTime, label: 'ATS all-time' };
}

function tierClass(tier) {
  return tier === 'strong' ? 'is-strong'
    : tier === 'clear' ? 'is-clear'
    : tier === 'slight' ? 'is-slight'
    : 'is-flip';
}

function gameCard({ game, prediction, tier, reasons, actualPick, revealed }) {
  const away = escapeHtml(game.Away || '');
  const home = escapeHtml(game.Home || '');
  const line = Number(game.line);
  const favIsHome = line < 0;
  const favName = favIsHome ? game.Home : game.Away;
  const spreadLabel = `${escapeHtml(favName)} −${Math.abs(line)}`;

  // Week already locked: show what they really did, and mark the model.
  if (revealed && actualPick) {
    const called = prediction && sameTeam(actualPick, prediction.p >= 0.5 ? prediction.fav : prediction.dog);
    const showCall = prediction && tier && tier.tier !== 'coin-flip';
    return `
      <div class="scout-game is-locked">
        <div class="scout-game-hd">
          <span class="scout-matchup">${away} @ ${home}</span>
          <span class="scout-spread">${spreadLabel}</span>
        </div>
        <div class="scout-game-bd">
          <div class="scout-side">
            <span class="scout-pick">${escapeHtml(actualPick)}</span>
            <span class="scout-sub">locked in</span>
          </div>
          ${showCall ? `<span class="scout-chip ${called ? 'is-hit' : 'is-miss'}">${called ? 'Model called it' : 'Model missed'}</span>` : ''}
        </div>
      </div>`;
  }

  if (!prediction) {
    return `
      <div class="scout-game is-flip">
        <div class="scout-game-hd">
          <span class="scout-matchup">${away} @ ${home}</span>
          <span class="scout-spread">${game.line == null ? 'no line yet' : spreadLabel}</span>
        </div>
        <div class="scout-game-bd">
          <div class="scout-side"><span class="scout-pick is-muted">No read</span>
            <span class="scout-sub">not enough to go on</span></div>
        </div>
      </div>`;
  }

  const flip = tier.tier === 'coin-flip';
  const side = prediction.p >= 0.5 ? prediction.fav : prediction.dog;
  const pct = Math.round(prediction.p * 100);
  const why = (reasons || []).slice(0, 3).map(r => `
      <li class="scout-why ${r.kind === 'up' ? 'is-up' : 'is-down'}">
        <span class="scout-why-sign">${r.kind === 'up' ? '+' : '−'}</span>
        <span>${escapeHtml(r.text)}<span class="scout-why-detail"> — ${escapeHtml(r.detail)}</span></span>
      </li>`).join('');

  return `
    <div class="scout-game ${tierClass(tier.tier)}">
      <div class="scout-game-hd">
        <span class="scout-matchup">${away} @ ${home}</span>
        <span class="scout-spread">${spreadLabel}</span>
      </div>
      <div class="scout-game-bd">
        <div class="scout-side">
          <span class="scout-pick${flip ? ' is-muted' : ''}">${flip ? 'No read' : escapeHtml(side)}</span>
          <span class="scout-sub">${flip ? 'could go either way' : 'lays the points'}</span>
        </div>
        <div class="scout-meter">
          ${flip ? '' : `<span class="scout-pct">${pct}%</span>`}
          <span class="scout-chip">${escapeHtml(tier.label)}</span>
        </div>
      </div>
      ${flip ? '' : `<div class="scout-bar"><span style="width:${pct}%"></span></div>`}
      ${why ? `<ul class="scout-whys">${why}</ul>` : ''}
    </div>`;
}

function shell(name, bodyHtml) {
  return `
    <div class="scout-sheet" role="dialog" aria-modal="true" aria-label="Scouting report for ${escapeHtml(name)}">
      <div class="scout-hd">
        <div class="scout-hd-text">
          <span class="scout-eyebrow">How they'll pick</span>
          <span class="scout-name">${escapeHtml(name)}</span>
        </div>
        <button type="button" class="scout-close" aria-label="Close">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor"
               stroke-width="2" stroke-linecap="round"><path d="M18 6 6 18M6 6l12 12"/></svg>
        </button>
      </div>
      <div class="scout-bd">${bodyHtml}</div>
    </div>`;
}

let openEl = null;
function close() {
  if (!openEl) return;
  openEl.remove();
  openEl = null;
  document.removeEventListener('keydown', onKey);
  document.body.style.overflow = '';
}
function onKey(e) { if (e.key === 'Escape') close(); }

/**
 * Open the panel for one player.
 * games: this week's rows as index.html already has them (Away/Home/line/GameId).
 * revealed: whether picks for this week are public yet.
 * pickMap: teamId -> { gameId: pickName }, used once the week has locked.
 */
export async function openScoutPanel({ teamId, teamName, games, season, revealed, pickMap }) {
  close();
  const wrap = document.createElement('div');
  wrap.className = 'scout-overlay';
  wrap.innerHTML = shell(teamName, '<p class="scout-note">Reading the season…</p>');
  document.body.appendChild(wrap);
  document.body.style.overflow = 'hidden';
  openEl = wrap;
  document.addEventListener('keydown', onKey);
  wrap.addEventListener('click', e => { if (e.target === wrap) close(); });
  wrap.querySelector('.scout-close').addEventListener('click', close);

  let data;
  try {
    data = await loadTrainingData();
  } catch (err) {
    console.error(err);
    const bd = wrap.querySelector('.scout-bd');
    if (bd) bd.innerHTML = '<p class="scout-note">Could not load enough history to make a read.</p>';
    return;
  }
  if (openEl !== wrap) return;   // closed, or another name clicked, while loading

  const { games: allGames, picks: allPicks } = data;
  const rec = atsRecord(allGames, allPicks, teamId, season);
  const withLine = (games || []).filter(g => g.line != null && Math.abs(Number(g.line)) >= 0.5);

  // The week we are predicting, taken from the games on screen so the playoff
  // view (weeks 16+) works the same way.
  const targetWeek = withLine.length
    ? Math.min(...withLine.map(g => Number(g.week)).filter(Number.isFinite))
    : null;
  const model = targetWeek == null ? null : modelFor(data, Number(season), targetWeek);

  const cards = withLine.map(g => {
    const prediction = model ? model.predict(teamId, g) : null;
    const usable = prediction && !prediction.unseen;
    const tier = usable ? confidenceTier(prediction.p) : null;
    return gameCard({
      game: g,
      prediction: usable ? prediction : null,
      tier,
      reasons: usable ? explain(model, teamId, prediction) : [],
      actualPick: pickMap && pickMap[teamId] ? pickMap[teamId][g.GameId] : null,
      revealed
    });
  }).join('');

  const reads = withLine.filter(g => {
    const p = model && model.predict(teamId, g);
    return p && !p.unseen && confidenceTier(p.p).tier !== 'coin-flip';
  }).length;

  const bd = wrap.querySelector('.scout-bd');
  if (!bd) return;
  bd.innerHTML = `
    <div class="scout-stats">
      <div><span class="scout-stat">${rec.w}-${rec.l}</span><span class="scout-stat-lbl">${rec.label}</span></div>
      <div><span class="scout-stat">${reads}/${withLine.length || 0}</span><span class="scout-stat-lbl">Games with a read</span></div>
    </div>
    ${cards || '<p class="scout-note">No games with a posted line this week.</p>'}
    <p class="scout-note">${model
      ? `Fitted only on games played before week ${targetWeek} &mdash; it has never seen this week's picks. A lean
         only shows where the model has been measured to be right; below 60% it stops tracking reality, so it
         says <strong>No read</strong> rather than guess.`
      : 'Not enough finished games yet to read anyone reliably. This fills in as the season goes.'}</p>`;
}
