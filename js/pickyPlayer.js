// Picky the player: the bot plays every week as an EXHIBITION team.
//
// On screen he looks like any other player (same rows, sorted in by points),
// but he is never paid and never counts for the jackpot, tiebreakers or the
// playoff cut. The database hides his team and picks from every normal read
// (RLS, see 20261114180000_picky_the_player.sql), so all the existing
// standings math runs exactly as if he didn't exist; the pages only merge him
// in at paint time. This module is the only way the site sees him, and only
// while pickyOn() is true.
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

function pickLabel(p) {
  const ln = Number(p.line);
  if (!Number.isFinite(ln) || ln === 0) return p.pick;
  const fav = ln < 0 ? p.home : p.away;
  return `${p.pick} ${sameTeam(p.pick, fav) ? '-' : '+'}${Math.abs(ln)}`;
}

// HIS TRASH TALK ONLY SHOWS WHEN SOMEONE ASKS FOR IT. A revealed pick of his
// with a blurb becomes a button; tapping it opens one small popover with the
// line, tapping it again, tapping anywhere else, Esc, scrolling or resizing
// closes it. picky_player() only returns revealed picks, so nothing here can
// show a blurb early.
let pop = null, popFor = null;

function closePop(focusBack = false) {
  if (!popFor) return;
  const el = popFor;
  popFor = null;
  el.setAttribute('aria-expanded', 'false');
  el.removeAttribute('aria-controls');
  if (pop) pop.hidden = true;
  if (focusBack && el.isConnected) el.focus();
}
export const closePickyBlurb = () => closePop();

function openPop(el, p) {
  if (!pop) {
    pop = document.createElement('div');
    pop.id = 'picky-pop';
    pop.setAttribute('role', 'tooltip');
    pop.style.cssText = 'position:fixed;z-index:60;max-width:min(17rem,calc(100vw - 1rem));padding:.5rem .7rem;'
      + 'border-radius:.6rem;border:1px solid rgba(231,231,231,.15);background:#111;color:#e5e7eb;'
      + 'font-size:.8rem;line-height:1.35;text-align:left;box-shadow:0 6px 24px rgba(0,0,0,.5)';
    document.body.appendChild(pop);
    document.addEventListener('click', e => {
      if (popFor && !popFor.contains(e.target) && !pop.contains(e.target)) closePop();
    });
    document.addEventListener('keydown', e => { if (e.key === 'Escape') closePop(true); });
    window.addEventListener('scroll', () => closePop(), true);
    window.addEventListener('resize', () => closePop());
  }
  closePop();
  pop.innerHTML = `<div style="font-weight:700;color:#fff">${escapeHtml(pickLabel(p))}</div>`
    + `<div>&ldquo;${escapeHtml(p.blurb)}&rdquo;</div>`;
  pop.hidden = false;
  const r = el.getBoundingClientRect();
  const w = pop.offsetWidth, h = pop.offsetHeight;
  const left = Math.max(8, Math.min(r.left + r.width / 2 - w / 2, innerWidth - w - 8));
  const top = (r.bottom + 6 + h <= innerHeight - 8) ? r.bottom + 6 : Math.max(8, r.top - 6 - h);
  pop.style.left = left + 'px';
  pop.style.top = top + 'px';
  popFor = el;
  el.setAttribute('aria-expanded', 'true');
  el.setAttribute('aria-controls', 'picky-pop');
}

// Makes `el` the way in to his blurb for this game. No-op without a blurb.
export function wirePickyBlurb(el, player, gameId) {
  const p = pickyPickFor(player, gameId);
  if (!p || !p.blurb) return;
  el.setAttribute('role', 'button');
  el.tabIndex = 0;
  el.setAttribute('aria-expanded', 'false');
  el.style.cursor = 'pointer';
  el.addEventListener('click', () => { if (popFor === el) closePop(); else openPop(el, p); });
  el.addEventListener('keydown', e => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); el.click(); }
  });
}

// picks.html: a line on each revealed game card. Idempotent per card.
export function paintPickyCards(player, root = document) {
  for (const p of player?.picks || []) {
    const card = root.querySelector(`[data-game-id="${Number(p.game_id)}"]`);
    if (!card || card.querySelector('.picky-card-pick')) continue;
    const el = document.createElement('div');
    el.className = 'picky-card-pick';
    el.style.cssText = 'margin-top:.6rem;padding-top:.5rem;border-top:1px solid rgba(231,231,231,.08);font-size:.8rem;color:#e5e7eb';
    const res = !p.winner ? '' : sameTeam(p.pick, p.winner)
      ? ' <span style="color:#4ade80">covered</span>' : ' <span style="color:#f87171">missed</span>';
    el.innerHTML = `<strong>${escapeHtml(player.team_name)}</strong> took <strong>${escapeHtml(pickLabel(p))}</strong>${res}`;
    wirePickyBlurb(el.querySelectorAll('strong')[1], player, p.game_id);
    card.appendChild(el);
  }
}
