// js/jackpot.js — the league's football, drawn at the top of the leaderboard
// and the picks page.
//
// It is the jackpot meter made visible, and that is the whole idea. A
// progressive jackpot is a number, and a number nobody looks at is not a
// feature. A football that has been visibly straining since Tuesday is one
// people talk about on Thursday.
//
// THE BALL IS ALWAYS NEAR BURSTING. It does not start new and inflate over the
// season. It is drawn taut from day one - leather stretched, seam already pale -
// and what the meter drives is how badly it MISBEHAVES at rest: a lazy creak
// every twenty seconds at the floor, a continuous barely-contained vibration at
// the ceiling. A ball that has looked about to go for six weeks is funny rather
// than dishonest, which is the effect we want.
//
// The honest channel is the label: "17 pulls since the last pop - 2.8%". Anyone
// who wants the real number can have it, which is exactly what frees the drawing
// to be pure theatre.
//
// READ ONLY. This module never writes. The meter is moved by a trigger in
// Postgres when a slots pick is saved (20260913 migration), because this repo is
// public and the key is anon - a counter the browser could increment is a
// counter anybody can run to 10%.

import { supabase } from './supabaseClient.js';

// Mirrors slots_jackpot_odds() in the migration. Duplicated deliberately and
// narrowly: the page draws the meter long before it would be worth a round trip
// to ask Postgres to do arithmetic. If the ramp is retuned, both move.
export function jackpotOdds(pulls) {
  return Math.min(0.0025 + 0.0015 * Math.max(Number(pulls) || 0, 0), 0.10);
}

/**
 * Which of the four rest states the ball is in.
 *
 * Banded rather than continuous because the difference between 3.1% and 3.4%
 * is not something an animation can express, and pretending otherwise just
 * produces four indistinguishable wobbles.
 */
export function strainLevel(odds) {
  if (odds >= 0.085) return 'critical';
  if (odds >= 0.055) return 'high';
  if (odds >= 0.025) return 'stirring';
  return 'calm';
}

function fmtPct(odds) {
  return (odds * 100).toFixed(odds < 0.01 ? 2 : 1) + '%';
}

/**
 * The ball itself. Inline SVG so it can be styled and animated by CSS alone.
 *
 * Exported because the reveal in js/slots.js draws the same ball, large, over the
 * board. Two hand-written footballs would drift apart the first time either was
 * touched, and the whole point is that the thing straining over the board is the
 * one that has been sitting in the header all week.
 */
export function ballSvg() {
  return (
    '<svg class="jp-ball" viewBox="0 0 64 40" aria-hidden="true" focusable="false">' +
      '<defs>' +
        '<radialGradient id="jp-leather" cx="38%" cy="32%" r="72%">' +
          '<stop offset="0%" stop-color="#9c5a2a"/>' +
          '<stop offset="55%" stop-color="#7a3f18"/>' +
          '<stop offset="100%" stop-color="#4a2410"/>' +
        '</radialGradient>' +
      '</defs>' +
      // The shell. Scaled by CSS, so the strain reads as the ball straining
      // rather than the whole graphic zooming.
      '<g class="jp-shell">' +
        '<ellipse class="jp-hide" cx="32" cy="20" rx="29" ry="17" fill="url(#jp-leather)"/>' +
        // The seam, which whitens and splits as the meter climbs.
        '<path class="jp-seam" d="M8 20 Q32 11 56 20 Q32 29 8 20Z" fill="none"/>' +
        '<g class="jp-laces">' +
          '<line x1="26" y1="20" x2="38" y2="20"/>' +
          '<line x1="28" y1="16.4" x2="28" y2="23.6"/>' +
          '<line x1="32" y1="15.8" x2="32" y2="24.2"/>' +
          '<line x1="36" y1="16.4" x2="36" y2="23.6"/>' +
        '</g>' +
        // Two hard highlights, because one reads as a sticker and two read as
        // something inflated.
        '<ellipse class="jp-gloss" cx="22" cy="12" rx="9" ry="4"/>' +
        '<ellipse class="jp-gloss jp-gloss-2" cx="45" cy="27" rx="6" ry="2.4"/>' +
      '</g>' +
    '</svg>'
  );
}

function render(el, pulls) {
  const odds = jackpotOdds(pulls);
  const level = strainLevel(odds);
  const pct = fmtPct(odds);
  const n = Number(pulls) || 0;

  el.dataset.strain = level;
  el.innerHTML =
    ballSvg() +
    '<span class="jp-text">' +
      '<b class="jp-pct">' + pct + '</b>' +
      '<span class="jp-sub">' + n + (n === 1 ? ' pull' : ' pulls') + ' since the last pop</span>' +
    '</span>';

  // Said in full to a screen reader, which gets none of the wobbling.
  el.setAttribute('aria-label',
    'Jackpot football: ' + pct + ' chance the next slots pull pops it. ' +
    n + (n === 1 ? ' pull' : ' pulls') + ' since the last pop.');
  el.title = 'Every "let the slots decide" pull inflates it. Pop it and that pick is an automatic win.';
}

/**
 * Draw the football into the top of whatever page called this.
 *
 * Fails silently and completely. This is decoration on a page whose job is
 * picks and standings: if the meter cannot be read, the right outcome is a
 * leaderboard with no football, not a leaderboard with an error on it.
 */
export default async function initJackpot() {
  const anchor = document.getElementById('site-nav');
  if (!anchor || document.querySelector('.jp-strip')) return null;

  const strip = document.createElement('div');
  strip.className = 'jp-strip';
  strip.setAttribute('role', 'status');
  strip.hidden = true;
  anchor.after(strip);

  try {
    const { data, error } = await supabase
      .from('slots_jackpot')
      .select('pulls_since_pop, last_pop_at')
      .eq('id', 1)
      .maybeSingle();
    if (error || !data) { strip.remove(); return null; }
    render(strip, data.pulls_since_pop);
    strip.hidden = false;
    return strip;
  } catch {
    strip.remove();
    return null;
  }
}

/**
 * Put the ball back to the floor after a pop, without a round trip.
 *
 * Called by the reveal the moment a cell pops, so the meter on screen agrees
 * with the board underneath it. The next page load reads the real number.
 */
export function deflateJackpot() {
  const strip = document.querySelector('.jp-strip');
  if (!strip) return;
  strip.classList.add('jp-just-popped');
  render(strip, 0);
  setTimeout(() => strip.classList.remove('jp-just-popped'), 2600);
}
