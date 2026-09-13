// js/jackpot.js — the league's football, at the top of the leaderboard and the
// picks page.
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
// ONE BALL, ONE PLACE. The kickoff reveal does not build a football of its own
// and it does not take over the screen: it grows THIS one, in the strip it
// already occupies at the top of the page. That is the whole point of a shared
// meter - the ball the league has been watching all week is the ball that
// strains - and it means the board underneath is never covered by the thing
// describing it.
//
// READ ONLY. This module never writes. The meter is moved by a trigger in
// Postgres when a slots pick is saved (20260913 migration), because this repo is
// public and the key is anon - a counter the browser could increment is a
// counter anybody can run to 10%.

import { supabase } from './supabaseClient.js';
import { soundEnabled, setSoundEnabled, unlock, lever } from './slotsound.js';

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

// The silhouette. A football is a prolate spheroid, which side-on means two arcs
// meeting at POINTS - not an ellipse, which is what the first version drew and
// which read as a rugby ball at best and an egg at worst. The tips are the whole
// difference, so they are the part with the tightest control points.
const BALL = 'M6,60 C36,6 164,6 194,60 C164,114 36,114 6,60 Z';

/**
 * The ball itself. Inline SVG so it can be styled and animated by CSS alone.
 *
 * Exported because the reveal in js/slots.js grows this same markup rather than
 * drawing a second football. Two hand-written balls would drift apart the first
 * time either was touched.
 *
 * The ids inside are namespaced per instance: two of these on one page - the
 * header and, briefly, anything else - would otherwise both resolve every
 * `url(#...)` to whichever was parsed first, and the second ball would silently
 * borrow the first one's gradients.
 */
export function ballSvg(ns = 'jp') {
  const id = s => ns + '-' + s;
  return (
    '<svg class="jp-ball" viewBox="0 0 200 120" aria-hidden="true" focusable="false">' +
      '<defs>' +
        // Leather: warm on top where the light is, cooling and darkening into
        // the underside. A flat fill is what made the first one look like a
        // sticker.
        '<linearGradient id="' + id('lea') + '" x1="0" y1="0" x2="0" y2="1">' +
          '<stop offset="0%"   stop-color="#a46130"/>' +
          '<stop offset="42%"  stop-color="#7e4018"/>' +
          '<stop offset="100%" stop-color="#40200d"/>' +
        '</linearGradient>' +
        // A second, off-centre light so the ball reads as round rather than as a
        // shape with a gradient in it.
        '<radialGradient id="' + id('lit') + '" cx="34%" cy="24%" r="62%">' +
          '<stop offset="0%"   stop-color="#fff" stop-opacity=".38"/>' +
          '<stop offset="55%"  stop-color="#fff" stop-opacity=".06"/>' +
          '<stop offset="100%" stop-color="#fff" stop-opacity="0"/>' +
        '</radialGradient>' +
        // Pebbling. Two offset dots per tile is enough grain at this size, and
        // costs one paint rather than the hundreds of circles it looks like.
        '<pattern id="' + id('peb') + '" width="7" height="7" patternUnits="userSpaceOnUse">' +
          '<circle cx="1.8" cy="1.8" r="1.05" fill="#2a1408" opacity=".26"/>' +
          '<circle cx="5.3" cy="5.3" r="1.05" fill="#2a1408" opacity=".26"/>' +
          '<circle cx="5.3" cy="1.8" r=".55" fill="#f4d8b6" opacity=".10"/>' +
        '</pattern>' +
        '<clipPath id="' + id('clip') + '"><path d="' + BALL + '"/></clipPath>' +
      '</defs>' +
      '<g class="jp-shell">' +
        '<path class="jp-hide" d="' + BALL + '" fill="url(#' + id('lea') + ')"/>' +
        '<g clip-path="url(#' + id('clip') + ')">' +
          '<rect x="0" y="0" width="200" height="120" fill="url(#' + id('peb') + ')"/>' +
          // The two white bands near the tips. Curved, not straight, because
          // they wrap a round thing.
          '<path class="jp-stripe" d="M44,10 C38,38 38,82 44,110 L53,110 C47,82 47,38 53,10 Z"/>' +
          '<path class="jp-stripe" d="M156,10 C162,38 162,82 156,110 L147,110 C153,82 153,38 147,10 Z"/>' +
          '<rect x="0" y="0" width="200" height="120" fill="url(#' + id('lit') + ')"/>' +
          // One hard specular, small and high. Real leather gives a tight
          // highlight, not a wash.
          '<ellipse class="jp-gloss" cx="74" cy="30" rx="30" ry="9"/>' +
        '</g>' +
        // The panel seam, sitting just above the waist the way it does when a
        // ball is held laces-up.
        '<path class="jp-seam" d="M16,56 C60,38 140,38 184,56" fill="none"/>' +
        '<g class="jp-laces">' +
          '<path class="jp-lace-band" d="M84,60 L116,60"/>' +
          '<path d="M86,52 L86,68"/>' +
          '<path d="M93,51 L93,69"/>' +
          '<path d="M100,50.6 L100,69.4"/>' +
          '<path d="M107,51 L107,69"/>' +
          '<path d="M114,52 L114,68"/>' +
        '</g>' +
        // Drawn last and on top, so the tips stay crisp however hard the shell
        // is being stretched underneath.
        '<path class="jp-rim" d="' + BALL + '" fill="none"/>' +
      '</g>' +
    '</svg>'
  );
}

/**
 * The marquee bulbs along the top of the cabinet.
 *
 * Real ones, as elements, rather than a repeating-linear-gradient - because they
 * have to chase, and a gradient can only slide. Each carries its own delay so
 * the light runs along the row.
 */
function bulbs(count) {
  let out = '<span class="jp-bulbs" aria-hidden="true">';
  for (let i = 0; i < count; i++) {
    out += '<i style="animation-delay:' + (i * 0.09).toFixed(2) + 's"></i>';
  }
  return out + '</span>';
}

const SPEAKER_ON =
  '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 9v6h4l5 4V5L8 9H4z"/>' +
  '<path d="M16 8.5a4.5 4.5 0 010 7" fill="none" stroke-width="2"/>' +
  '<path d="M18.5 6a8 8 0 010 12" fill="none" stroke-width="2"/></svg>';
const SPEAKER_OFF =
  '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 9v6h4l5 4V5L8 9H4z"/>' +
  '<path d="M16 9.5l5 5M21 9.5l-5 5" fill="none" stroke-width="2"/></svg>';

function render(el, pulls) {
  const odds = jackpotOdds(pulls);
  const n = Number(pulls) || 0;
  const pct = fmtPct(odds);

  el.dataset.strain = strainLevel(odds);
  el.innerHTML =
    bulbs(18) +
    '<span class="jp-cab">' +
      ballSvg() +
      '<span class="jp-text">' +
        '<span class="jp-label">Jackpot</span>' +
        // An inset, glowing panel rather than plain text: a progressive meter on
        // a real machine is a lit display, and it is the one part of this that
        // states a fact, so it is the part that should look instrumented.
        '<b class="jp-pct">' + pct + '</b>' +
        '<span class="jp-sub">' + n + (n === 1 ? ' pull' : ' pulls') + ' since the last pop</span>' +
        // Written to by the reveal. Empty and collapsed the rest of the time.
        '<span class="jp-note" aria-live="polite"></span>' +
      '</span>' +
      '<button type="button" class="jp-sound" data-act="sound" aria-pressed="' +
        (soundEnabled() ? 'true' : 'false') + '" title="Slot machine sound">' +
        (soundEnabled() ? SPEAKER_ON : SPEAKER_OFF) +
      '</button>' +
    '</span>';

  const btn = el.querySelector('.jp-sound');
  if (btn) btn.addEventListener('click', e => {
    e.stopPropagation();
    // The click IS the gesture that unlocks the audio context, so it has to do
    // the turning-on and the unlocking in the same handler. Done later - on the
    // first spin, say - the browser has forgotten there was ever a gesture and
    // refuses, silently.
    const on = setSoundEnabled(!soundEnabled());
    if (on) { unlock(); lever(); }
    btn.setAttribute('aria-pressed', on ? 'true' : 'false');
    btn.innerHTML = on ? SPEAKER_ON : SPEAKER_OFF;
  });

  // Said in full to a screen reader, which gets none of the wobbling.
  el.setAttribute('aria-label',
    'Jackpot football: ' + pct + ' chance the next slots pull pops it. ' +
    n + (n === 1 ? ' pull' : ' pulls') + ' since the last pop.');
  el.title = 'Every "let the slots decide" pull inflates it. Pop it and that pick is an automatic win.';
}

/**
 * The strip, made if it is not there yet.
 *
 * The reveal needs somewhere to play even when the meter could not be read - a
 * migration not yet run, a dropped request - and a kickoff with no football at
 * all would be a worse answer than a football with no number on it. So this
 * always returns an element, and initJackpot() fills in the number when it can.
 */
export function ensureStrip() {
  let strip = document.querySelector('.jp-strip');
  if (strip) return strip;

  const anchor = document.getElementById('site-nav');
  if (!anchor) return null;

  strip = document.createElement('div');
  strip.className = 'jp-strip';
  strip.setAttribute('role', 'status');
  render(strip, 0);
  anchor.after(strip);
  return strip;
}

/**
 * Draw the ball at a given meter reading, without asking the database.
 *
 * For the sandbox, which needs to show all four rest states on a Tuesday - the
 * real meter sits at one number for weeks at a time, so it is no use at all for
 * judging whether the ball looks right as it climbs.
 */
export function previewJackpot(pulls) {
  const strip = ensureStrip();
  if (strip) render(strip, pulls);
  return strip;
}

/**
 * Draw the football into the top of whatever page called this.
 *
 * Fails quietly. This is furniture on a page whose job is picks and standings:
 * if the meter cannot be read, the right outcome is a leaderboard with no
 * football, not a leaderboard with an error on it.
 */
export default async function initJackpot() {
  const anchor = document.getElementById('site-nav');
  if (!anchor || document.querySelector('.jp-strip')) return null;

  try {
    const { data, error } = await supabase
      .from('slots_jackpot')
      .select('pulls_since_pop, last_pop_at')
      .eq('id', 1)
      .maybeSingle();
    if (error || !data) return null;
    const strip = ensureStrip();
    if (strip) render(strip, data.pulls_since_pop);
    return strip;
  } catch {
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
  const beat = strip.dataset.beat;          // the reveal may still be mid-sequence
  render(strip, 0);
  if (beat) strip.dataset.beat = beat;
}
