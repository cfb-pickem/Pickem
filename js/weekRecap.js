// js/weekRecap.js — how the prediction model actually did last week.
//
// The stats page already hands out Lock of the Week, Trap Game and Lone Wolf,
// so this deliberately does not. What nothing else answers is whether the thing
// making leans on the leaderboard and the picks page is any good, which is
// exactly the question a confident-looking percentage invites.
//
// Every number here is graded the same way the model is used: fitted only on
// weeks that finished BEFORE the one being reviewed, so it is scored on picks
// it had never seen. Nobody has to run anything - it appears once a week has
// finished and disappears while one is still in progress.

import { escapeHtml } from './utils.js';
import { confidenceTier } from './scoutModel.js';
import { loadTrainingData, modelFor } from './scoutData.js';

const cache = new Map();

const ordinalName = (map, id) => map.get(Number(id)) || `Team ${id}`;

/**
 * @param mount   element to render into
 * @param season  cfb_season being viewed
 * @param week    the week being viewed
 * @param games   that week's rows (needs GameId, Away, Home, line, winner)
 * @param teams   [{team_id, team_name}]
 */
export async function renderWeekRecap(mount, { season, week, games, teams }) {
  if (!mount) return;
  mount.innerHTML = '';
  mount.hidden = true;

  const withLine = (games || []).filter(g =>
    g.line != null && Math.abs(Number(g.line)) >= 0.5 && g.winner != null);
  // Only review a week that is actually over. A half-finished one produces
  // numbers that change under the reader, which is worse than no numbers.
  if (!withLine.length || withLine.length !== (games || []).filter(g => g.line != null).length) return;

  const key = `${season}|${week}`;
  let recap = cache.get(key);
  if (!recap) {
    let data;
    try { data = await loadTrainingData(); } catch { return; }
    const model = modelFor(data, Number(season), Number(week));
    if (!model) return;                      // not enough history to have had a view

    const rows = data.rows.filter(r => r.season === Number(season) && r.week === Number(week));
    if (!rows.length) return;

    const perGame = new Map();
    const perPlayer = new Map();
    let right = 0, total = 0;
    const byTier = {};

    for (const r of rows) {
      const line = r.favHome ? -r.spread : r.spread;
      const game = r.favHome
        ? { Home: r.fav, Away: r.dog, line }
        : { Home: r.dog, Away: r.fav, line };
      if (r.fpiEdge != null) {
        const eh = r.favHome ? r.fpiEdge : -r.fpiEdge;
        game.fpi_margin = eh + (-line);
      }
      if (r.lineMove != null) {
        const mh = r.favHome ? r.lineMove : -r.lineMove;
        game.line_open = mh + line;
      }
      const p = model.predict(r.pid, game);
      if (!p || p.unseen) continue;

      const hit = (p.p >= 0.5 ? 1 : 0) === r.laidPoints;
      total++; if (hit) right++;

      const tier = confidenceTier(p.p).tier;
      byTier[tier] = byTier[tier] || { n: 0, hit: 0 };
      byTier[tier].n++; if (hit) byTier[tier].hit++;

      const gk = `${r.fav}|${r.dog}`;
      // Away @ home, which is not the same as dog @ fav - the favourite is on
      // the road often enough that getting this wrong would show.
      const gs = perGame.get(gk) || {
        n: 0, hit: 0,
        label: r.favHome ? `${r.dog} @ ${r.fav}` : `${r.fav} @ ${r.dog}`
      };
      gs.n++; if (hit) gs.hit++; perGame.set(gk, gs);

      const ps = perPlayer.get(r.pid) || { n: 0, hit: 0 };
      ps.n++; if (hit) ps.hit++; perPlayer.set(r.pid, ps);
    }

    if (total < 8) return;
    recap = { right, total, byTier, perGame: [...perGame.values()], perPlayer: [...perPlayer.entries()] };
    cache.set(key, recap);
  }

  const names = new Map((teams || []).map(t => [Number(t.team_id), t.team_name]));
  const games_ = recap.perGame.filter(g => g.n >= 3).sort((a, b) => (b.hit / b.n) - (a.hit / a.n));
  const players = recap.perPlayer.filter(([, v]) => v.n >= 3)
    .sort((a, b) => (b[1].hit / b[1].n) - (a[1].hit / a[1].n));

  const pct = (h, n) => `${Math.round(100 * h / n)}%`;
  const tierRow = (k, label) => {
    const t = recap.byTier[k];
    if (!t || !t.n) return '';
    return `<div class="recap-tier"><span class="recap-tier-lbl">${label}</span>
      <span class="recap-tier-val">${t.hit}/${t.n}</span></div>`;
  };

  const best = games_[0], worst = games_[games_.length - 1];
  const easiest = players[0], hardest = players[players.length - 1];

  mount.hidden = false;
  mount.innerHTML = `
    <div class="recap">
      <div class="recap-head">
        <span class="recap-title">How the model read Week ${escapeHtml(String(week))}</span>
        <span class="recap-score">${recap.right}/${recap.total} <span class="recap-score-pct">${pct(recap.right, recap.total)}</span></span>
      </div>
      <div class="recap-tiers">
        ${tierRow('strong', 'Strong')}${tierRow('clear', 'Clear')}
        ${tierRow('slight', 'Slight')}${tierRow('coin-flip', 'Coin flip')}
      </div>
      <div class="recap-notes">
        ${best && best.n ? `<div class="recap-note"><span class="recap-note-lbl">Read best</span>
          ${escapeHtml(best.label)} &mdash; called ${best.hit} of ${best.n}</div>` : ''}
        ${worst && worst !== best ? `<div class="recap-note"><span class="recap-note-lbl">Read worst</span>
          ${escapeHtml(worst.label)} &mdash; called ${worst.hit} of ${worst.n}</div>` : ''}
        ${easiest ? `<div class="recap-note"><span class="recap-note-lbl">Most predictable</span>
          ${escapeHtml(ordinalName(names, easiest[0]))} &mdash; ${easiest[1].hit} of ${easiest[1].n}</div>` : ''}
        ${hardest && hardest !== easiest ? `<div class="recap-note"><span class="recap-note-lbl">Hardest to read</span>
          ${escapeHtml(ordinalName(names, hardest[0]))} &mdash; ${hardest[1].hit} of ${hardest[1].n}</div>` : ''}
      </div>
      <p class="recap-foot">Graded on picks the model had never seen &mdash; it was fitted only on weeks
      that finished before this one.</p>
    </div>`;
}
