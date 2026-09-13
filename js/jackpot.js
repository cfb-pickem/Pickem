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
// WHERE IT LIVES, WHICH IS TWO PLACES FOR ONE REASON.
//
// On the leaderboard the football is not furniture. It TAKES THE MARQUEE: for
// the length of a roll it stands where the "CFB Pick'em Leaderboard" heading is,
// between the two mascots, and when the roll is done it is gone and the title is
// back. That band is already drawn as an LED panel, which makes it the one place
// on the page a slot machine belongs - and a football that is only ever there
// during a roll is an event rather than a widget.
//
// On the picks page there is nothing to roll, so it sits as the ambient meter it
// always was. That is where the lever is, and watching the thing tighten while
// you decide whether to hand a game to the house is the entire reason to pull
// it.
//
// Either way it is never a screen of its own: the board underneath is never
// covered by the thing describing it.
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

/**
 * How full the gauge reads, 0..1. NOT the odds, and deliberately not.
 *
 * The league is never told its chance of popping. What they get is a needle
 * that has been climbing since the last one went, which is the same information
 * in the only form that creates any tension: you can see it is worse than it was
 * on Tuesday and you cannot work out how much worse.
 *
 * It is eased rather than linear, so the early pulls move it visibly - a gauge
 * that sits dead still for the first fortnight is a gauge nobody looks at twice -
 * and it is capped short of the end so it never reads FULL, because a full gauge
 * is a promise and this thing must never make one.
 */
function gaugeFill(pulls) {
  const n = Math.max(Number(pulls) || 0, 0);
  const CAP = 65;                       // where the odds hit their ceiling
  return Math.min(Math.pow(Math.min(n / CAP, 1), 0.62), 1) * 0.92;
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

/**
 * What a pop DOES. Never what it is worth in odds.
 *
 * A real machine puts its paytable on the glass, and the first version of this
 * did the same - the outcomes with their true percentages beside them. That went
 * because the not-knowing is the entire product here. A league that can read
 * "2.8%" off the cabinet has a statistic; a league that can only see the needle
 * has been climbing since Tuesday has a thing to argue about.
 *
 * What stays is the part nobody can guess and everybody needs: that a pop is an
 * automatic win, and that it is the whole league's football rather than yours.
 * Explaining the rules is not the same as publishing the odds.
 */
function paytable() {
  return (
    '<div class="jp-paytable" hidden>' +
      '<div class="jp-pay-row">' +
        '<b class="jp-pay-sym">POP</b>' +
        '<span class="jp-pay-desc">The football goes and that pick is an ' +
          '<b>automatic win</b> \u2014 one point, whatever the game does. ' +
          'No team, no crest, just AUTO WIN in the square.</span>' +
      '</div>' +
      '<div class="jp-pay-row">' +
        '<b class="jp-pay-sym">HOLD</b>' +
        '<span class="jp-pay-desc">It holds, and the reels decide your team the ' +
          'way they always have \u2014 straight fifty-fifty.</span>' +
      '</div>' +
      '<p class="jp-pay-note">' +
        'One football, the whole league. Every "let the slots decide" pull by ' +
        'anybody puts more air in it, and it stays where it is until somebody ' +
        'pops it \u2014 then it is back to a new ball for everyone. ' +
        '<b>Nobody is told how close it is.</b> That is the point of it.' +
      '</p>' +
    '</div>'
  );
}

function render(el, pulls) {
  const odds = jackpotOdds(pulls);
  const n = Number(pulls) || 0;

  el.dataset.strain = strainLevel(odds);
  el.innerHTML =
    bulbs(18) +
    '<span class="jp-cab">' +
      ballSvg() +
      '<span class="jp-text">' +
        '<span class="jp-label">Pressure</span>' +
        // A gauge rather than a readout. A progressive meter on a real machine is
        // a lit display, so this is one too - it just measures instead of stating.
        '<span class="jp-gauge" aria-hidden="true">' +
          '<i style="width:' + (gaugeFill(n) * 100).toFixed(1) + '%"></i>' +
        '</span>' +
        '<span class="jp-sub">' + n + (n === 1 ? ' pull' : ' pulls') + ' since it last blew</span>' +
        // Written to by the reveal. Empty and collapsed the rest of the time.
        '<span class="jp-note" aria-live="polite"></span>' +
      '</span>' +
      '<button type="button" class="jp-pays" data-act="pays" aria-expanded="false">Pays</button>' +
      '<button type="button" class="jp-sound" data-act="sound" aria-pressed="' +
        (soundEnabled() ? 'true' : 'false') + '" title="Slot machine sound">' +
        (soundEnabled() ? SPEAKER_ON : SPEAKER_OFF) +
      '</button>' +
    '</span>' +
    paytable();

  const pays = el.querySelector('.jp-pays');
  const panel = el.querySelector('.jp-paytable');
  if (pays && panel) {
    // render() runs again every time the meter moves, so the panel has to be put
    // back the way the reader left it. Re-collapsing it under somebody mid-read
    // is the kind of small rudeness that makes a thing feel broken.
    if (el.dataset.paysOpen === '1') {
      panel.hidden = false;
      pays.setAttribute('aria-expanded', 'true');
    }
    pays.addEventListener('click', e => {
      e.stopPropagation();
      const open = panel.hidden;
      panel.hidden = !open;
      pays.setAttribute('aria-expanded', open ? 'true' : 'false');
      el.dataset.paysOpen = open ? '1' : '0';
    });
  }

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

  // A screen reader gets the same thing everybody else does, and no more. Putting
  // the real odds in here because "nobody reads it" would be both a leak and the
  // exact contempt this label exists to avoid.
  el.setAttribute('aria-label',
    'The league football. ' + n + (n === 1 ? ' pull' : ' pulls') +
    ' since it last blew, and getting tighter.');
  el.title = 'Every "let the slots decide" pull puts more air in it. Nobody knows when it goes.';
}

/** Is this the page with the board on it? Static markup, so it is true early. */
function isBoardPage() {
  return !!document.getElementById('table-scroll-wrap');
}

function buildStrip(pulls) {
  const strip = document.createElement('div');
  strip.className = 'jp-strip';
  strip.setAttribute('role', 'status');
  render(strip, pulls);
  return strip;
}

/**
 * The ambient meter, under the nav. Made if it is not there yet.
 *
 * This is the picks page's football, and the sandbox's. The leaderboard does not
 * get one - see ensureStage().
 */
export function ensureStrip(pulls = 0) {
  const existing = document.querySelector('.jp-strip');
  if (existing) return existing;
  const anchor = document.getElementById('site-nav');
  if (!anchor) return null;
  const strip = buildStrip(pulls);
  anchor.after(strip);
  return strip;
}

/**
 * Where the reveal plays: the marquee, if this page has one.
 *
 * Takes over `.jumbotron-title` by hiding the heading and standing in its place,
 * so the football is literally where the words were. Falls back to the ambient
 * strip on any page without a jumbotron, and to nothing at all if there is
 * nowhere at all to put it - a kickoff with no football is a poor answer, but a
 * thrown error in the middle of a reveal is a worse one.
 */
export function ensureStage() {
  const title = document.querySelector('.jumbotron-title');
  if (!title) return ensureStrip();

  const already = title.querySelector('.jp-strip');
  if (already) return already;

  const text = title.querySelector('.jumbotron-title-text');
  if (text) text.hidden = true;

  const strip = buildStrip(0);
  strip.dataset.takeover = '1';
  title.appendChild(strip);
  return strip;
}

/** Give the marquee back. Safe to call when nothing was ever taken. */
export function dismissStage() {
  const strip = document.querySelector('.jp-strip[data-takeover]');
  if (!strip) return;
  const title = strip.parentNode;
  strip.remove();
  const text = title && title.querySelector('.jumbotron-title-text');
  if (text) text.hidden = false;
}

/**
 * Draw the ball at a given meter reading, without asking the database.
 *
 * For the sandbox, which needs to show all four rest states on a Tuesday - the
 * real meter sits on one number for weeks at a time, so it is no use at all for
 * judging how the ball looks as it climbs. It is also the only way an ambient
 * strip appears on the leaderboard at all, where the league's football is an
 * event rather than furniture.
 */
export function previewJackpot(pulls) {
  const strip = ensureStrip(pulls);
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

  // Not on the leaderboard. There the football is an event, not furniture: it
  // appears in the marquee for the length of a roll and then it is gone.
  if (isBoardPage()) return null;

  try {
    const { data, error } = await supabase
      .from('slots_jackpot')
      .select('pulls_since_pop, last_pop_at')
      .eq('id', 1)
      .maybeSingle();
    if (error || !data) return null;
    return ensureStrip(data.pulls_since_pop);
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
  // The one in play first. With an ambient strip AND a marquee takeover on the
  // page - which is exactly what the sandbox produces - querying for the plain
  // class hands back whichever parsed first, and the football that just burst
  // is not necessarily it.
  const strip = document.querySelector('.jp-strip[data-takeover]')
             || document.querySelector('.jp-strip');
  if (!strip) return;
  const beat = strip.dataset.beat;          // the reveal may still be mid-sequence
  render(strip, 0);
  if (beat) strip.dataset.beat = beat;
}
