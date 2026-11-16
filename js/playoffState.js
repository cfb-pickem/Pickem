// js/playoffState.js
//
// The playoff state, as the pages see it. The switch itself is the rows in
// public.playoffs (seeds) - written by the commissioner by hand, or by the
// playoff autopilot in Supabase (supabase/migrations/20261116120000_playoff_autopilot.sql)
// once the regular season is graded and any tie at 4th is settled by the rules.
// public.playoff_autopilot carries the autopilot's state and, while it's
// waiting, why.
//
// ?playoffs=preview dresses the leaderboard in playoff mode before it's real,
// with the field the autopilot would pick if the season ended right now. It
// only changes what this browser shows; nothing is written.

import { escapeHtml } from './utils.js';

export const PLAYOFF_PREVIEW =
  new URLSearchParams(location.search).get('playoffs') === 'preview';

const cache = new Map();   // season -> Promise<state>

export function loadPlayoffState(supabase, season) {
  if (!cache.has(season)) cache.set(season, fetchState(supabase, season));
  return cache.get(season);
}

async function fetchState(supabase, season) {
  const state = { season, seeds: [], autopilot: null, active: false, preview: false };
  try {
    const [seedRes, apRes] = await Promise.all([
      supabase.from('playoffs').select('team_id, seed, teams ( team_name )')
        .eq('season', season).order('seed', { ascending: true }),
      // Missing until the autopilot migration is applied: treated as "no autopilot".
      supabase.from('playoff_autopilot').select('state, enabled, holds, switched_at, switched_by, army_navy_used')
        .eq('season', season).maybeSingle(),
    ]);
    state.seeds = (seedRes.data || []).map(r => ({
      team_id: r.team_id, seed: r.seed, team_name: r.teams?.team_name || `Team ${r.team_id}`,
    }));
    if (!apRes.error) state.autopilot = apRes.data || null;
  } catch (e) {
    console.warn('playoff state unavailable:', e);
  }
  state.active = state.seeds.length > 0;

  if (!state.active && PLAYOFF_PREVIEW) {
    try {
      const { data } = await supabase.rpc('playoff_compute', { p_season: season });
      const q = (data && data.qualifiers) || [];
      if (q.length) {
        state.seeds = q.map(r => ({ team_id: r.team_id, seed: r.seed, team_name: r.team_name }));
        state.active = true;
        state.preview = true;
      }
    } catch (e) {
      console.warn('playoff preview unavailable:', e);
    }
  }
  return state;
}

export function setPlayoffTheme(on) {
  if (on) document.documentElement.setAttribute('data-playoffs', 'on');
  else document.documentElement.removeAttribute('data-playoffs');
}

const TROPHY = `<svg class="po-trophy" viewBox="0 0 64 64" aria-hidden="true" focusable="false">
  <path d="M18 8h28v10c0 9.5-5.6 17-14 18.6C23.6 35 18 27.5 18 18V8z" fill="currentColor"/>
  <path d="M18 12H9c0 8 3.6 13 10.4 14.2M46 12h9c0 8-3.6 13-10.4 14.2" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round"/>
  <path d="M28 36.5h8V46h-8z" fill="currentColor"/>
  <path d="M20 54c0-4.4 3.6-8 8-8h8c4.4 0 8 3.6 8 8v2H20v-2z" fill="currentColor"/>
  <path d="M25 13l3.2 6.4 7 .9-5.1 4.9 1.3 7-6.4-3.4" fill="rgba(0,0,0,.18)"/>
</svg>`;

export function renderPlayoffBanner(el, state, { show }) {
  if (!el) return;
  if (!show || !state || !state.active) { el.hidden = true; el.innerHTML = ''; return; }
  const chips = state.seeds.map(s =>
    `<li class="po-seed"><span class="po-seed-n">${s.seed}</span>${escapeHtml(s.team_name)}</li>`).join('');
  el.innerHTML = `
    <div class="po-glow" aria-hidden="true"></div>
    ${TROPHY}
    <div class="po-copy">
      <div class="po-eyebrow">${state.preview ? 'Preview &middot; if the season ended today' : `${state.season} Playoffs`}</div>
      <div class="po-title">Four left. Points reset.</div>
      <div class="po-sub">${state.preview
        ? 'This is what the board turns into once the field is set. Nothing here is official yet.'
        : 'Pick every playoff game. Each one locks at its own kickoff. Most points takes the trophy.'}</div>
      <ol class="po-seeds">${chips}</ol>
    </div>`;
  el.hidden = false;
}

// Public copy for the autopilot's holds. Only the ones a member cares about;
// data problems (an empty week) stay on the commissioner page.
function holdLine(h) {
  switch (h.code) {
    case 'settling': {
      const t = h.until ? new Date(h.until).toLocaleString('en-US',
        { timeZone: 'America/Chicago', weekday: 'short', hour: 'numeric', minute: '2-digit' }) : null;
      return `Regular season's in the books. The playoff field locks once the scores settle${t ? `, around ${t} CT` : ''}.`;
    }
    case 'ungraded':
      return 'The playoff field locks as soon as the last regular-season scores are in.';
    case 'cut_tie_pending':
    case 'coin_flip':
      return h.message;
    default:
      return null;
  }
}

export function renderHoldNote(el, state, { show }) {
  if (!el) return;
  const ap = state && state.autopilot;
  const lines = (show && ap && ap.state === 'holding' && !state.active)
    ? (ap.holds || []).map(holdLine).filter(Boolean) : [];
  if (!lines.length) { el.hidden = true; el.innerHTML = ''; return; }
  el.innerHTML = `<span class="po-hold-dot" aria-hidden="true"></span><span>${lines.map(escapeHtml).join(' ')}</span>`;
  el.hidden = false;
}
