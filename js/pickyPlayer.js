// Picky the player: the bot plays every week as an EXHIBITION team.
//
// He is never ranked, never paid, and never counts for the jackpot,
// tiebreakers or the playoff cut. The database hides his team and picks from
// every normal read (RLS, see 20261114180000_picky_the_player.sql), so all the
// existing standings math runs exactly as if he didn't exist. This module is
// the only way the site sees him, and only while pickyOn() is true.
//
// picky_player() applies the same reveal rule as human picks, so anything it
// returns is already safe to show.

import { pickyOn } from './pickyFlag.js';
import { supabase } from './supabaseClient.js';
import { sameTeam, escapeHtml } from './utils.js';

// season -> { at, promise }. Short-lived so a reveal or a final score shows up
// on the next live repaint without a reload.
const cache = new Map();
const TTL_MS = 60_000;

export function loadPickyPlayer(season) {
  if (!pickyOn() || season == null) return Promise.resolve(null);
  const hit = cache.get(season);
  if (hit && Date.now() - hit.at < TTL_MS) return hit.promise;
  const promise = Promise.resolve(
    supabase.rpc('picky_player', { p_season: Number(season) })
  ).then(({ data, error }) => (error || !data || !data.team_name) ? null : data)
   .catch(() => null);
  cache.set(season, { at: Date.now(), promise });
  return promise;
}

// Same scoring as a human (index.html computeSeasonWinsThroughWeek): regular
// season only, tiebreaker games never count, a point when his side covered.
export function pickyRecord(player, { throughWeek = 15, week = null } = {}) {
  let w = 0, l = 0;
  for (const p of player?.picks || []) {
    const wk = Number(p.week);
    if (p.tiebreaker || !p.winner || wk > 15) continue;
    if (week != null ? wk !== Number(week) : wk > throughWeek) continue;
    if (sameTeam(p.pick, p.winner)) w++; else l++;
  }
  return { w, l };
}

export function pickyPickFor(player, gameId) {
  return (player?.picks || []).find(p => Number(p.game_id) === Number(gameId)) || null;
}

let styled = false;
function ensureStyles() {
  if (styled) return;
  styled = true;
  const s = document.createElement('style');
  s.textContent = `
    .picky-bot-badge{display:inline-block;margin-left:.4rem;padding:.05rem .4rem;border-radius:999px;
      font-size:.62rem;font-weight:700;letter-spacing:.06em;text-transform:uppercase;vertical-align:middle;
      color:#111;background:#e9b949;white-space:nowrap}
    tr.picky-row td{background:rgba(233,185,73,.06)}
    tr.picky-row td.sticky-col-1{font-style:italic}
    tr.picky-divider td{padding:.25rem 1rem;font-size:.65rem;letter-spacing:.08em;text-transform:uppercase;
      color:#9ca3af;border-top:1px dashed rgba(233,185,73,.35)}
    .picky-talk{margin-top:1rem;padding:.75rem 1rem;border:1px solid rgba(233,185,73,.25);border-radius:.75rem;
      background:rgba(0,0,0,.25);font-size:.85rem}
    .picky-talk h3{font-family:'Teko','Oswald',sans-serif;font-size:1.2rem;font-weight:700;text-transform:uppercase;
      letter-spacing:.06em;color:#e9b949;margin:0 0 .4rem}
    .picky-talk li{margin:.35rem 0;color:#e5e7eb}
    .picky-talk .pk{font-weight:700;color:#fff}
    .picky-talk .why{display:block;font-size:.72rem;color:#9ca3af}
  `;
  document.head.appendChild(s);
}

export function botBadge() {
  ensureStyles();
  const b = document.createElement('span');
  b.className = 'picky-bot-badge';
  b.textContent = 'Bot · Exhibition';
  b.title = 'Picky plays for fun: never ranked, never paid, never counts for the jackpot, tiebreakers or playoffs.';
  return b;
}

function pickLabel(p) {
  const ln = Number(p.line);
  if (!Number.isFinite(ln) || ln === 0) return p.pick;
  const fav = ln < 0 ? p.home : p.away;
  return `${p.pick} ${sameTeam(p.pick, fav) ? '-' : '+'}${Math.abs(ln)}`;
}

// A leaderboard row for him, appended BELOW every human (and below the
// projected-playoff line), so it can never move a human's position.
//   cells: [{ gameId, reveal }] in column order
//   logoFor(pick) -> url | null
export function pickyRow(player, { points, cells, logoFor, extraCols = 0 }) {
  ensureStyles();
  const frag = document.createDocumentFragment();

  const div = document.createElement('tr');
  div.className = 'picky-divider';
  const dtd = document.createElement('td');
  dtd.colSpan = 2 + cells.length + extraCols;
  dtd.textContent = 'Exhibition: not ranked, no payouts';
  div.appendChild(dtd);
  frag.appendChild(div);

  const tr = document.createElement('tr');
  tr.className = 'picky-row border-t border-[rgba(231,231,231,.08)]';
  tr.dataset.exhibition = 'true';
  const name = document.createElement('td');
  name.className = 'pl-4 pr-2 py-2 font-semibold text-[var(--cfp-ivory)] sticky-col-1';
  name.textContent = player.team_name;
  name.appendChild(botBadge());
  tr.appendChild(name);

  const pts = document.createElement('td');
  pts.className = 'pl-2 pr-3 py-2 text-center font-semibold text-[var(--cfp-ivory)]';
  pts.textContent = String(points);
  tr.appendChild(pts);

  for (const { gameId, reveal } of cells) {
    const td = document.createElement('td');
    td.className = 'px-3 py-2 text-center text-sm align-middle';
    const p = reveal ? pickyPickFor(player, gameId) : null;
    if (!p) {
      td.classList.add('pick-empty');
      td.textContent = '—';
    } else {
      td.classList.add(!p.winner ? 'pick-empty' : sameTeam(p.pick, p.winner) ? 'pick-win' : 'pick-loss');
      const url = logoFor ? logoFor(p.pick) : null;
      if (url) {
        const img = document.createElement('img');
        img.src = url; img.alt = p.pick; img.loading = 'lazy'; img.className = 'logo';
        td.appendChild(img);
      } else {
        td.textContent = p.pick;
      }
      td.title = `${pickLabel(p)}${p.blurb ? ' | ' + p.blurb : ''}`;
    }
    tr.appendChild(td);
  }
  for (let i = 0; i < extraCols; i++) tr.appendChild(document.createElement('td'));
  frag.appendChild(tr);
  return frag;
}

// "Picky's trash talk" for one week: each revealed pick, why, and his line.
export function renderPickyTalk(el, player, week) {
  if (!el) return;
  const picks = (player?.picks || []).filter(p => Number(p.week) === Number(week));
  if (!picks.length) { el.hidden = true; el.innerHTML = ''; return; }
  ensureStyles();
  const rec = pickyRecord(player, { week });
  el.className = 'picky-talk';
  el.hidden = false;
  el.innerHTML = `
    <h3>Picky's picks <span class="picky-bot-badge">Bot · Exhibition</span></h3>
    <div class="text-xs text-gray-400">Week ${escapeHtml(week)}: ${rec.w}-${rec.l}. For bragging rights only; he can't win money or knock anyone out of a spot.</div>
    <ul>${picks.map(p => `
      <li><span class="pk">${escapeHtml(pickLabel(p))}</span>${p.tiebreaker ? ' <span class="text-xs text-gray-400">(tiebreaker, never counts)</span>' : ''}
        ${p.winner ? (sameTeam(p.pick, p.winner) ? ' <span style="color:#4ade80">covered</span>' : ' <span style="color:#f87171">missed</span>') : ''}
        ${p.blurb ? `<br>&ldquo;${escapeHtml(p.blurb)}&rdquo;` : ''}
        ${p.reason ? `<span class="why">Why: ${escapeHtml(p.reason)}</span>` : ''}</li>`).join('')}
    </ul>`;
}

// picks.html: a line on each revealed game card. Idempotent per card.
export function paintPickyCards(player, root = document) {
  for (const p of player?.picks || []) {
    const card = root.querySelector(`[data-game-id="${Number(p.game_id)}"]`);
    if (!card || card.querySelector('.picky-card-pick')) continue;
    ensureStyles();
    const el = document.createElement('div');
    el.className = 'picky-card-pick';
    el.style.cssText = 'margin-top:.6rem;padding-top:.5rem;border-top:1px dashed rgba(233,185,73,.3);font-size:.8rem;color:#e5e7eb';
    const res = !p.winner ? '' : sameTeam(p.pick, p.winner)
      ? ' <span style="color:#4ade80">covered</span>' : ' <span style="color:#f87171">missed</span>';
    el.innerHTML = `<strong>${escapeHtml(player.team_name)}</strong> took <strong>${escapeHtml(pickLabel(p))}</strong>${res}`
      + (p.blurb ? `<div style="font-style:italic;margin-top:.2rem">&ldquo;${escapeHtml(p.blurb)}&rdquo;</div>` : '');
    el.querySelector('strong').after(botBadge());
    card.appendChild(el);
  }
}

// stats.html "Total Season Win %": his row, appended under the humans.
export function pickySeasonRow(player) {
  const { w, l } = pickyRecord(player);
  const n = w + l;
  const tr = document.createElement('tr');
  tr.className = 'picky-row border-t border-[rgba(231,231,231,.08)]';
  tr.dataset.exhibition = 'true';
  const cells = [player.team_name, `${w}-${l}`, n ? (w / n).toFixed(3).replace(/^0/, '') : '—', '—'];
  cells.forEach((txt, i) => {
    const td = document.createElement('td');
    td.className = 'px-4 py-2';
    td.textContent = txt;
    if (i === 0) td.appendChild(botBadge());
    tr.appendChild(td);
  });
  return tr;
}
