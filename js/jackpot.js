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
// IT IS ONLY EVER THERE DURING A ROLL.
//
// There is no football sitting at the top of any page. For the length of a roll
// it stands where the "CFB Pick'em Leaderboard" heading is - between the two
// mascots, in a band already drawn as an LED panel, which is the one place on
// this site a slot machine belongs - and the moment the roll is over it is gone
// and the heading is back.
//
// A football that is only there during a roll is an event. A football that is
// always there is furniture, and furniture stops being looked at in about a
// week.
//
// THE PICKS PAGE IS THE EXCEPTION, and for the opposite reason. Nothing rolls
// there, so there is no event to have - but that is where the lever is, and the
// whole argument for pulling it is the thing sitting at the top of the page
// getting tighter. So it keeps an animated football, centred, over a plain count
// of what the league has put into it.
//
// WHICH MAKES THE METER SOMETHING THAT HAS TO BE FETCHED. Nothing draws it at
// rest any more, so nothing was loading it either, and the ball would have taken
// the marquee at zero pressure every single time however long the drought had
// run. primeMeter() reads it once on load and the reveal uses what arrived.
//
// READ ONLY. This module never writes. The meter is moved by a trigger in
// Postgres when a slots pick is saved (20260913 migration), because this repo is
// public and the key is anon - a counter the browser could increment is a
// counter anybody can run to 10%.

import { supabase } from './supabaseClient.js';
import { soundEnabled, setSoundEnabled, unlock, lever } from './slotsound.js';

// THE RAMP, mirroring slots_jackpot_odds() in the 20260914 migration.
// Duplicated deliberately and narrowly: the page draws the meter long before it
// would be worth a round trip to ask Postgres to do arithmetic. If it is
// retuned, both move.
//
// Tuned to one auto win a season against a MEASURED pull rate rather than a
// guessed one - 2 slotted picks out of 168 this season, over a 20-week season
// with 4 games and 14 players, comes to about thirteen pulls. A mean of thirteen
// pulls between pops makes that one auto win.
// MUST HIT BY, which is the casino mechanic for "this pays out about once per
// X". Flat and tiny for nearly a whole season's worth of pulls, then climbing
// hard. A straight ramp cannot hit one-a-season at 420 pulls: averaging one pop
// per 420 means averaging a quarter of a percent, and any ramp that spends real
// time at five or ten per cent ends its cycle immediately.
const RAMP = { floor: 0.0005, quietUntil: 445, slope: 0.0025, ceiling: 0.10 };

// Where the odds stop climbing. Derived rather than written down, so the gauge
// and the ball cannot go on implying rising pressure after it has levelled off.
const CAP_AT = RAMP.quietUntil + Math.ceil((RAMP.ceiling - RAMP.floor) / RAMP.slope);

export function jackpotOdds(pulls) {
  const n = Math.max(Number(pulls) || 0, 0);
  return Math.min(RAMP.floor + Math.max(n - RAMP.quietUntil, 0) * RAMP.slope, RAMP.ceiling);
}

/**
 * Which of the four rest states the ball is in.
 *
 * DRIVEN BY HOW FAR THROUGH THE CYCLE IT IS, not by the odds. The odds are flat
 * at five hundredths of a percent for the first four hundred and forty-five
 * pulls and then run away, so a ball driven by them would sit perfectly still
 * for almost the entire season and then go berserk in a fortnight. What the
 * league should be able to see is the thing filling up all year.
 *
 * Banded rather than continuous because the difference between 31% and 34% of a
 * cycle is not something an animation can express, and pretending otherwise
 * produces four indistinguishable wobbles.
 */
export function strainLevel(fill) {
  if (fill >= 0.80) return 'critical';
  if (fill >= 0.55) return 'high';
  if (fill >= 0.25) return 'stirring';
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
/**
 * How much bigger the ball is than a new one.
 *
 * It starts taut and it does not stop: every pull anybody in the league makes
 * puts more air in, and the ball on the page is physically larger for it. Same
 * curve as the gauge, so the two agree - and the same reason for the easing,
 * that the early pulls have to move it visibly or nobody looks twice.
 *
 * Half again at the top. Beyond that it stops reading as a strained football and
 * starts reading as a different, larger football.
 */
function swell(pulls) {
  return 1 + 0.55 * gaugeFill(pulls);
}

function gaugeFill(pulls) {
  const n = Math.max(Number(pulls) || 0, 0);
  // Progress through the cycle, not probability. It spans the whole thing -
  // from a new ball to the pull at which the odds are pinned at their ceiling -
  // so it climbs visibly all season instead of sitting dead still and then
  // leaping, which is what a needle driven by the odds themselves would do.
  //
  // Eased so the early pulls move it: a gauge that is flat for a month is a
  // gauge nobody looks at twice. And capped short of the end, because a full
  // gauge is a promise and this thing must never make one.
  return Math.min(Math.pow(Math.min(n / CAP_AT, 1), 0.62), 1) * 0.92;
}

// THE SILHOUETTE IS A FUNCTION, NOT A STRING.
//
// A football is a prolate spheroid, which side-on means two arcs meeting at
// POINTS - not an ellipse, which is what the first version drew and which read
// as a rugby ball at best and an egg at worst. The tips are the whole
// difference, so they are the part with the tightest control points.
//
// But the bigger thing is that it BENDS. Everything before this scaled a rigid
// shape, and a rigid shape scaled is a photograph being zoomed: the middle and
// the tips grew by the same amount, which is the one thing an inflating ball
// never does. Under pressure the waist swells hard, the tips are drawn IN as the
// skin is pulled around the bulge, and the whole thing resonates like a membrane
// when it is let go.
//
// So the path is generated per frame from one number. At pressure 0 it is
// exactly the outline this file used to carry as a constant.
const CX = 100, CY = 60;

export function ballPath(pressure = 0, wobble = 0) {
  const p = Math.max(0, Math.min(1, pressure));
  // Half-length shrinks as the waist grows: the skin has to come from somewhere.
  const a = 94 - 6 * p;
  // The waist. Top and bottom flex in opposite directions, which is what makes
  // the wobble read as a membrane rather than a pulsing logo.
  //
  // It stops at ±70 against a half-length of 88 - an aspect of 1.26, which is a
  // fat football. It used to reach 1.01, which is a sphere, and a sphere is not
  // a football under pressure; it is a beach ball.
  const top = 54 + 16 * p + wobble * 9;
  const bot = 54 + 16 * p - wobble * 9;
  // Control points slide toward the tips under pressure, which is what rounds
  // the middle out and sharpens the ends.
  const sx = 0.319 - 0.05 * p;
  const x1 = CX - a, x2 = CX + a;
  const c1 = x1 + a * sx, c2 = x2 - a * sx;
  const r = n => Math.round(n * 100) / 100;
  return `M${r(x1)},${CY} C${r(c1)},${r(CY - top)} ${r(c2)},${r(CY - top)} ${r(x2)},${CY}` +
         ` C${r(c2)},${r(CY + bot)} ${r(c1)},${r(CY + bot)} ${r(x1)},${CY} Z`;
}

/** The two halves, for when it lets go. Each closes along the waist. */
export function ballHalves(pressure = 0) {
  const p = Math.max(0, Math.min(1, pressure));
  const a = 94 - 6 * p, lift = 54 + 16 * p, sx = 0.319 - 0.05 * p;
  const x1 = CX - a, x2 = CX + a, c1 = x1 + a * sx, c2 = x2 - a * sx;
  return [
    `M${x1},${CY} C${c1},${CY - lift} ${c2},${CY - lift} ${x2},${CY} Z`,
    `M${x1},${CY} C${c1},${CY + lift} ${c2},${CY + lift} ${x2},${CY} Z`,
  ];
}

/** The laces spread as the skin under them is stretched. */
export function lacePaths(pressure = 0) {
  const p = Math.max(0, Math.min(1, pressure));
  const half = 16 + 7 * p;                     // the band gets longer
  const gap = 7 + 2.6 * p;                     // and the stitches separate
  const rise = 8 + 3.5 * p;                    // and stand further off the skin
  const out = [`M${CX - half},${CY} L${CX + half},${CY}`];
  for (let i = -2; i <= 2; i++) {
    const x = CX + i * gap;
    const r = rise - Math.abs(i) * 0.6;        // shorter at the ends, as on a ball
    out.push(`M${x},${CY - r} L${x},${CY + r}`);
  }
  return out;
}

/** The panel seam, riding up over the bulge. */
export function seamPath(pressure = 0) {
  const p = Math.max(0, Math.min(1, pressure));
  const lift = 22 + 16 * p;
  return `M${CX - 84},${CY - 4 - 2 * p} C${CX - 40},${CY - lift} ${CX + 40},${CY - lift} ${CX + 84},${CY - 4 - 2 * p}`;
}

// Kept for anything that wants a static one.
const BALL = ballPath(0);

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

let ballSeq = 0;

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
  const u = s => 'url(#' + id(s) + ')';
  return (
    '<svg class="jp-ball" viewBox="0 0 200 120" aria-hidden="true" focusable="false">' +
      '<defs>' +
        // LEATHER, in five stops rather than three. The difference between a
        // three-stop gradient and a five is the difference between a shape with
        // a gradient on it and a thing made of something.
        '<linearGradient id="' + id('lea') + '" x1="0" y1="0" x2="0.15" y2="1">' +
          '<stop offset="0%"   stop-color="#b4703a"/>' +
          '<stop offset="22%"  stop-color="#98552a"/>' +
          '<stop offset="48%"  stop-color="#7c3f19"/>' +
          '<stop offset="78%"  stop-color="#5a2b10"/>' +
          '<stop offset="100%" stop-color="#341a08"/>' +
        '</linearGradient>' +
        // THE EDGE DARKENING. One radial, transparent through the middle and
        // dark at the rim, and the ball stops being a silhouette and starts
        // being round. It is the single cheapest three-dimensional cue there is.
        '<radialGradient id="' + id('ao') + '" cx="46%" cy="42%" r="62%">' +
          '<stop offset="0%"   stop-color="#000" stop-opacity="0"/>' +
          '<stop offset="62%"  stop-color="#000" stop-opacity="0"/>' +
          '<stop offset="88%"  stop-color="#1a0c04" stop-opacity=".55"/>' +
          '<stop offset="100%" stop-color="#0d0602" stop-opacity=".85"/>' +
        '</radialGradient>' +
        // The key light, off-centre and soft.
        '<radialGradient id="' + id('lit') + '" cx="33%" cy="20%" r="58%">' +
          '<stop offset="0%"   stop-color="#ffe9c8" stop-opacity=".46"/>' +
          '<stop offset="45%"  stop-color="#ffd9a8" stop-opacity=".10"/>' +
          '<stop offset="100%" stop-color="#fff" stop-opacity="0"/>' +
        '</radialGradient>' +
        // A RIM LIGHT along the far edge. Bright where the light wraps round the
        // bottom right, gone by the top left. This is what makes it read as lit
        // from somewhere rather than shaded by hand.
        '<linearGradient id="' + id('rimlit') + '" x1="0.1" y1="0" x2="0.9" y2="1">' +
          '<stop offset="0%"   stop-color="#ffcf92" stop-opacity="0"/>' +
          '<stop offset="55%"  stop-color="#ffcf92" stop-opacity="0"/>' +
          '<stop offset="88%"  stop-color="#ffd9a8" stop-opacity=".75"/>' +
          '<stop offset="100%" stop-color="#fff1d8" stop-opacity=".95"/>' +
        '</linearGradient>' +
        // Pebbling: a shadow dot, a lit dot and a fine one, so the grain has a
        // direction instead of being noise.
        '<pattern id="' + id('peb') + '" width="7" height="7" patternUnits="userSpaceOnUse">' +
          '<circle cx="1.8" cy="1.8" r="1.05" fill="#2a1408" opacity=".30"/>' +
          '<circle cx="1.5" cy="1.5" r=".45" fill="#f4d8b6" opacity=".13"/>' +
          '<circle cx="5.3" cy="5.3" r="1.05" fill="#2a1408" opacity=".30"/>' +
          '<circle cx="5.0" cy="5.0" r=".45" fill="#f4d8b6" opacity=".13"/>' +
        '</pattern>' +
        '<clipPath id="' + id('clip') + '"><path d="' + BALL + '"/></clipPath>' +
      '</defs>' +
      '<g class="jp-shell">' +
        '<path class="jp-hide" d="' + BALL + '" fill="' + u('lea') + '"/>' +
        '<g clip-path="' + u('clip') + '">' +
          '<rect x="0" y="0" width="200" height="120" fill="' + u('peb') + '"/>' +
          // The two white bands near the tips, each with its own shading so they
          // sit on the leather rather than on top of the picture.
          '<path class="jp-stripe" d="M44,10 C38,38 38,82 44,110 L53,110 C47,82 47,38 53,10 Z"/>' +
          '<path class="jp-stripe" d="M156,10 C162,38 162,82 156,110 L147,110 C153,82 153,38 147,10 Z"/>' +
          '<rect x="0" y="0" width="200" height="120" fill="' + u('ao') + '"/>' +
          '<rect x="0" y="0" width="200" height="120" fill="' + u('lit') + '"/>' +
          // One tight specular. Real leather gives a small hard highlight, not a
          // wash - the wash is already doing its job above.
          '<ellipse class="jp-gloss" cx="70" cy="27" rx="21" ry="7"/>' +
          '<ellipse class="jp-gloss jp-gloss-2" cx="128" cy="90" rx="26" ry="8"/>' +
        '</g>' +
        // The panel seam, riding up over the bulge.
        '<path class="jp-seam" d="' + seamPath(0) + '" fill="none"/>' +
        '<g class="jp-laces">' +
          lacePaths(0).map(d => '<path d="' + d + '"/>').join('') +
        '</g>' +
        // The rim light goes UNDER the outline, so the dark edge still frames it.
        '<path class="jp-rimlit" d="' + BALL + '" fill="none" stroke="' + u('rimlit') + '"/>' +
        // Drawn last and on top, so the tips stay crisp however hard the shell
        // is being stretched underneath.
        '<path class="jp-rim" d="' + BALL + '" fill="none"/>' +
      '</g>' +
      // What is left when it lets go: the two panels it tears into along the
      // waist, and the shock going out from where it was. Empty and invisible
      // until the moment it bursts.
      '<g class="jp-shards" aria-hidden="true">' +
        '<circle class="jp-shock" cx="100" cy="60" r="30"/>' +
        // Made of the same leather as the ball they came off. Filled flat they
        // were all but black against a dark cabinet, which read as the ball
        // simply disappearing rather than coming apart.
        '<path class="jp-shard jp-shard-top" d="" fill="' + u('lea') + '"/>' +
        '<path class="jp-shard jp-shard-bot" d="" fill="' + u('lea') + '"/>' +
      '</g>' +
    '</svg>'
  );
}

function render(el, pulls) {
  const odds = jackpotOdds(pulls);
  const n = Number(pulls) || 0;

  el.dataset.strain = strainLevel(gaugeFill(n));
  // Read by the width rules for both the marquee and the picks page, so the one
  // number drives the size wherever the ball happens to be drawn.
  el.style.setProperty('--jp-swell', swell(n).toFixed(3));
  // A namespace per strip, because ballSvg() takes one and never getting a
  // distinct one defeats the point of it: the sandbox puts a preview ball and a
  // marquee ball on the page at once, and two SVGs sharing gradient ids both
  // resolve every url(#...) to whichever parsed first.
  el.dataset.ns = el.dataset.ns || ('jp' + (++ballSeq));
  el.innerHTML =
    bulbs(18) +
    '<span class="jp-cab">' +
      ballSvg(el.dataset.ns) +
      '<span class="jp-text">' +
        '<span class="jp-label">Progressive Jackpot</span>' +
        // A gauge rather than a readout. A progressive meter on a real machine is
        // a lit display, so this is one too - it just measures instead of stating.
        '<span class="jp-gauge" aria-hidden="true">' +
          '<i style="width:' + (gaugeFill(n) * 100).toFixed(1) + '%"></i>' +
        '</span>' +
        '<span class="jp-sub">' + n + (n === 1 ? ' slot pull' : ' slot pulls') +
          ' since the last jackpot</span>' +
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
    'The league football. ' + n + (n === 1 ? ' slot pull' : ' slot pulls') +
    ' since the last jackpot, and getting tighter.');
  el.title = 'Every "let the slots decide" pull puts more air in it. Nobody knows when it goes.';
}

// What the league has done to the football, read once and kept. Zero is the
// honest default: a ball drawn at the floor is wrong by less than a ball drawn
// at a number we never actually asked for.
let meterPulls = 0;

/**
 * Read the meter, once, so the reveal has something true to draw.
 *
 * Fire-and-forget on page load. If it never arrives the ball is simply drawn
 * slack, which is a worse picture than the truth but not a broken one - and a
 * leaderboard that will not render because a decoration could not read a counter
 * would be far worse than both.
 */
export async function primeMeter() {
  try {
    const { data, error } = await supabase
      .from('slots_jackpot')
      .select('pulls_since_pop')
      .eq('id', 1)
      .maybeSingle();
    if (!error && data) meterPulls = Number(data.pulls_since_pop) || 0;
  } catch {}
  return meterPulls;
}

/**
 * Drive one ball's geometry from a pressure value.
 *
 * A spring rather than a transition, because a transition only ever travels
 * between two resting states and an inflating ball does not rest - it overshoots
 * and resonates. `kick()` is what the reveal calls on each bulge, and the wobble
 * that follows is the ball settling rather than an animation ending.
 *
 * Returns null if there is nothing to drive, so callers can ignore it entirely.
 */
export function ballRig(root) {
  const svg = root?.querySelector?.('.jp-ball');
  if (!svg) return null;

  const hide = svg.querySelector('.jp-hide');
  const rim = svg.querySelector('.jp-rim');
  const rimlit = svg.querySelector('.jp-rimlit');
  const clip = svg.querySelector('clipPath path');
  const inner = svg.querySelector('[clip-path]');
  const seam = svg.querySelector('.jp-seam');
  const laces = [...svg.querySelectorAll('.jp-laces path')];
  const shardTop = svg.querySelector('.jp-shard-top');
  const shardBot = svg.querySelector('.jp-shard-bot');

  let pressure = 0, vel = 0, target = 0;
  let wob = 0, wobVel = 0;
  let raf = 0, running = true;

  function draw() {
    const d = ballPath(pressure, wob);
    hide?.setAttribute('d', d);
    rim?.setAttribute('d', d);
    rimlit?.setAttribute('d', d);
    clip?.setAttribute('d', d);
    seam?.setAttribute('d', seamPath(pressure));
    const lp = lacePaths(pressure);
    laces.forEach((el, i) => { if (lp[i]) el.setAttribute('d', lp[i]); });
    // The pebbling and the stripes are painted inside the outline, so they have
    // to stretch with it or the leather slides under its own edge.
    inner?.setAttribute('transform',
      `translate(${CX} ${CY}) scale(${1 - 0.03 * pressure} ${1 + 0.28 * pressure}) translate(${-CX} ${-CY})`);
  }

  function step() {
    if (!running) return;
    // Spring toward the target, then let the wobble decay on its own.
    vel = (vel + (target - pressure) * 0.14) * 0.78;
    pressure += vel;
    wobVel = (wobVel - wob * 0.30) * 0.94;
    wob += wobVel;
    draw();
    raf = requestAnimationFrame(step);
  }

  draw();
  raf = requestAnimationFrame(step);

  return {
    set(p) { target = Math.max(0, Math.min(1, p)); },
    kick(a = 1) { wobVel += a; },
    /** Freeze where it is and hand back the shape, for the burst to tear up. */
    rupture() {
      running = false;
      cancelAnimationFrame(raf);
      const [t, b] = ballHalves(pressure);
      shardTop?.setAttribute('d', t);
      shardBot?.setAttribute('d', b);
      return pressure;
    },
    stop() { running = false; cancelAnimationFrame(raf); },
  };
}

/**
 * Set what the meter reads, without asking the database.
 *
 * For the sandbox's Meter slider. The real meter sits on one number for weeks at
 * a time, so it is no use for judging how the ball behaves as it swells - and
 * the only place the ball is ever seen on the leaderboard is mid-roll, so the
 * slider has to change what the NEXT roll draws rather than anything on screen.
 */
export function setMeter(pulls) {
  meterPulls = Math.max(Number(pulls) || 0, 0);
  // If a strip is on screen and not mid-reveal, keep it honest.
  const at_rest = document.querySelector('.jp-strip:not([data-takeover])');
  if (at_rest && !at_rest.dataset.beat) render(at_rest, meterPulls);
  return meterPulls;
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

  // At whatever the league has actually got it up to. This is the only moment
  // anybody sees the football, so it is the only moment the pressure can show.
  const strip = buildStrip(meterPulls);
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
export function previewJackpot(pulls, mount = null) {
  if (mount) {
    let strip = mount.querySelector('.jp-strip');
    if (!strip) {
      strip = buildStrip(pulls);
      strip.dataset.center = '1';
      mount.appendChild(strip);
    }
    render(strip, pulls);
    return strip;
  }
  const strip = ensureStrip(pulls);
  if (strip) render(strip, pulls);
  return strip;
}

/**
 * The picks page's football: centred at the top, animated, over the count.
 *
 * Read straight from the database rather than from primeMeter()'s cache, because
 * this is the number somebody is looking at while they decide whether to hand a
 * game to the house. It should be what the meter actually says, not what it said
 * when the tab was opened.
 *
 * Fails quietly and completely. This is the reason to pull the lever, not the
 * lever itself: if the meter cannot be read the right outcome is a picks page
 * with no football, never a picks page with an error on it.
 */
export default async function initJackpot() {
  const anchor = document.getElementById('site-nav');
  if (!anchor || document.querySelector('.jp-strip')) return null;
  // Not the leaderboard. There it is an event, and it takes the marquee.
  if (document.getElementById('table-scroll-wrap')) return null;

  try {
    const { data, error } = await supabase
      .from('slots_jackpot')
      .select('pulls_since_pop')
      .eq('id', 1)
      .maybeSingle();
    if (error || !data) return null;

    const strip = buildStrip(Number(data.pulls_since_pop) || 0);
    strip.dataset.center = '1';
    anchor.after(strip);
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
  // The one in play first. With an ambient strip AND a marquee takeover on the
  // page - which is exactly what the sandbox produces - querying for the plain
  // class hands back whichever parsed first, and the football that just burst
  // is not necessarily it.
  meterPulls = 0;                           // it is a new ball for everybody now

  // NOT WHILE IT IS PLAYING. Re-rendering a strip mid-reveal replaces its
  // innerHTML, which orphans the note element footballAct is holding - so every
  // line after the pop was written to a detached node and the marquee went blank
  // for the one outcome anybody cares about. It also restarted the burst
  // animation from its first frame on a brand-new element.
  //
  // There is nothing to redraw there anyway: that ball is bursting and the strip
  // is about to be torn down. What matters is the ambient one on the picks page,
  // which is showing a count that just became wrong.
  const ambient = document.querySelector('.jp-strip:not([data-takeover])');
  if (ambient && !ambient.dataset.beat) render(ambient, 0);
}
