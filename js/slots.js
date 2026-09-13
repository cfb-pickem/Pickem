// js/slots.js — the slot-machine reveal on the leaderboard.
//
// When somebody chooses "let the slots decide" on the picks page, no team is
// saved: the row goes in as an intent, and the database settles it at kickoff
// (resolve_slot_picks, in the 20260911 and 20260913 migrations). The first time
// that cell is seen afterwards, it spins.
//
// THE ANIMATION IS THEATRE OVER A SETTLED RESULT, and that is the only honest
// way to build it. The coin landed in Postgres, once, minutes or hours ago; the
// reel only performs it. A spin that decided anything in the browser could be
// re-rolled by reloading the page until it said something you liked.
//
// So the landing symbol is built from the cell's OWN image. The reel cannot
// disagree with the pick, because it is not consulted about it.
//
// THE REEL, adapted from johakr/html5-slot-machine: a strip of symbols inside an
// overflow-hidden box driven by the Web Animations API, the blur peaking
// mid-spin, and the landing symbol at the END of the strip so the spin arrives
// on it rather than being snapped into place.
//
// Two things are borrowed deliberately. The reference animates `top` rather than
// `transform: translateY`, because Safari will not animate a transform and a
// filter on the same element at the same time — hard-won, so kept. And each reel
// gets its own duration, which is what makes several cells land in sequence
// rather than together.
//
// What is NOT borrowed is the symbols. The reel shows the two teams in that game
// and nobody else: a reel flashing teams who were never in the game is funny
// once and misleading afterwards. The comedy is in mangling the two real logos
// on the way past — squashed, upside down, radioactive, melting — and landing on
// a clean, correct one. The joke is that the punchline is sober.
//
// ----------------------------------------------------------------------------
// AND THEN THE FOOTBALL.
//
// Behind every coin flip there is a jackpot (see the 20260913 migration). When
// it hits, that player gets no team at all — the cell says AUTO WIN and scores a
// point whatever the game does. So after the reels land, the league's football
// rises over the board and strains, and either settles or bursts.
//
// ONE BALL PER BATCH, not one per cell. Eight cells each running a seven-second
// strain would be punishing, and the meter is shared by the whole league anyway,
// so a shared ball is also the truer picture: that is not your football, it is
// everybody's.
//
// THE RULE THAT MAKES IT WORK: every frame before the last moment is identical
// whether it pops or not. If the pop version strained even slightly harder the
// league would learn to read it within three weeks and the tease would be dead.
// Nothing below branches on the outcome until `burst`.

import { markSlotCell } from './utils.js';
import { ballSvg, deflateJackpot } from './jackpot.js';

// One transform and/or one tint per symbol, combined at random, so a dozen
// classes give plenty of distinct nonsense without needing a dozen more. The
// landing symbol gets NONE of them — that is the whole gag.
const WARPS = ['squash', 'stretch', 'flip', 'upside', 'tipsy', 'huge', 'tiny', 'melt', 'wobble'];
const TINTS = ['radioactive', 'negative', 'ghost', 'oldtimey', 'xray', 'hot'];

const pick1 = a => a[Math.floor(Math.random() * a.length)];

// The football's beats are setTimeout rather than the Web Animations API, so
// they are deaf to playbackRate - which would have left the sandbox's speed
// slider silently bending the reels and not the ball. One factor, applied to
// every wait below, keeps the whole sequence under one control.
let revealSpeed = 1;
export function setRevealSpeed(x) {
  const n = Number(x);
  revealSpeed = Number.isFinite(n) && n > 0 ? n : 1;
}
const wait = ms => new Promise(r => setTimeout(r, ms / revealSpeed));

function reducedMotion() {
  return window.matchMedia?.('(prefers-reduced-motion: reduce)')?.matches ?? false;
}

// Replaces the old `cfb-slots-seen` localStorage keys, which showed each reveal
// exactly once per person per game and therefore ate any spin you happened not
// to be looking at. Now it replays on every page LOAD — but not on every render,
// because the board rebuilds itself every minute on live scores and reels
// spinning continuously all Saturday is nobody's idea of a leaderboard. A Set
// that dies with the page is precisely that distinction, expressed.
const spunThisLoad = new Set();

const cellKey = td => `${td.dataset.slotsTeam}:${td.dataset.slots}`;

/**
 * Forget every spin this page has shown, so they can all run again.
 *
 * For the sandbox's Roll button, which exists to watch the reveal on demand -
 * and which silently did nothing the second time you pressed it until this
 * existed, because the set above had already recorded the cells.
 */
export function forgetSpins() {
  spunThisLoad.clear();
}

/**
 * The two teams in each game column, read off the header the board has already
 * rendered. No query: the logos are on screen, and re-fetching the schedule to
 * learn something the page is currently displaying would be silly.
 */
function columnTeams() {
  return [...document.querySelectorAll('#thead-row th')].map(th => {
    const logos = [...th.querySelectorAll('img.game-hdr-logo')];
    if (logos.length < 2) return null;
    return logos.slice(0, 2).map(img => ({ src: img.currentSrc || img.src }));
  });
}

/**
 * Every cell the slots decided that is ready to be watched.
 *
 * `data-slots` is set by renderTable from what the database said, so there is no
 * sampling and no guessing here — the board is the source of truth. A cell that
 * popped carries `data-jackpot` and has an AUTO WIN tag where the crest would
 * be; it spins like any other and simply never lands on a team.
 */
function dueCells({ only = null } = {}) {
  const cols = columnTeams();
  const out = [];
  document.querySelectorAll('tbody tr').forEach(tr => {
    if (tr.classList.contains('playoff-divider')) return;
    [...tr.children].forEach((td, col) => {
      if (!td.dataset?.slots || !cols[col]) return;
      if (only && td !== only) return;
      if (!only && spunThisLoad.has(cellKey(td))) return;
      if (td.querySelector('.slot-box')) return;          // already mid-spin

      const jackpot = td.dataset.jackpot === '1';
      const img = td.querySelector('img.logo');
      const tag = td.querySelector('.auto-win');
      // Not settled yet: no crest and no tag means the coin has not been
      // flipped. Leave it for the next load rather than spinning on nothing.
      if (!jackpot && !img) return;
      if (jackpot && !tag) return;

      out.push({ td, img, tag, jackpot, sides: cols[col] });
    });
  });
  return out;
}

/**
 * Replace one cell's logo with a reel and spin it onto the logo already there.
 *
 * A popped cell has no logo to spin onto, so it lands on one of the two sides at
 * random. That symbol is never the answer and is about to be destroyed by the
 * football — but it has to be SOMETHING, because a reel that visibly declines to
 * land is a reel announcing the jackpot before the ball has even appeared.
 */
function spinCell(cell, order) {
  const { td, img, tag, jackpot, sides } = cell;

  // Measured rather than hard-coded at 32px, because the same cell is 24px on a
  // phone and a reel built to the wrong height shows two symbols at once.
  const probe = img || tag;
  const h = Math.round(probe.getBoundingClientRect().height) || 32;

  const landingSrc = img ? (img.currentSrc || img.src) : pick1(sides).src;
  const landingAlt = img ? (img.alt || '') : '';

  // A longer strip and a longer spin for each successive cell, so a board with
  // several of them lands one after another instead of all at once.
  //
  // The two numbers move TOGETHER on purpose. Stretching the duration on its own
  // would not lengthen the spin so much as slow it down — the same handful of
  // symbols drifting past instead of a reel running. Roughly 8.5 symbols a
  // second is the pace that reads as a slot machine, so more time buys
  // proportionally more strip and the speed stays put.
  const turns = 32 + order * 12;
  const duration = 3800 + order * 1120;

  const strip = document.createElement('div');
  strip.className = 'slot-strip';

  // The nonsense, then the truth. Sides alternate, so it reads as a decision
  // between these two and nobody else.
  for (let i = 0; i < turns; i++) {
    const sym = document.createElement('img');
    sym.src = sides[i % 2].src;
    sym.alt = '';
    sym.className = 'slot-sym';
    sym.style.height = h + 'px';
    // Not every symbol is warped. An unbroken run of distortions stops reading
    // as distortion and starts reading as the design.
    if (Math.random() < 0.82) sym.classList.add('slot-warp-' + pick1(WARPS));
    if (Math.random() < 0.55) sym.classList.add('slot-tint-' + pick1(TINTS));
    strip.appendChild(sym);
  }

  const finalSym = document.createElement('img');
  finalSym.src = landingSrc;
  finalSym.alt = landingAlt;
  finalSym.className = 'slot-sym slot-sym-final';
  finalSym.style.height = h + 'px';
  strip.appendChild(finalSym);

  const box = document.createElement('div');
  box.className = 'slot-box';
  box.style.height = h + 'px';
  box.appendChild(strip);

  // A popped cell's AUTO WIN tag is HIDDEN rather than removed, and the reel is
  // slid in beside it. The tag is what the board already rendered from what the
  // database said; putting it back at the end is the reveal. Building a second
  // one here would be a second source of truth for the only fact that scores.
  if (img) {
    img.replaceWith(box);
  } else {
    tag.hidden = true;
    td.insertBefore(box, tag);
  }
  td.classList.add('slot-cell');

  const land = () => {
    td.classList.remove('slot-cell');
    td.classList.add('slot-landed');
    // The reel leaves the crest behind for an ordinary pick. For a popped one it
    // leaves the decoy, which the football is about to take away.
    if (img) box.replaceWith(img);
    else box.classList.add('slot-box-decoy');
    setTimeout(() => td.classList.remove('slot-landed'), 900);
  };

  // Reduced motion gets the result and none of the theatre. This is the most
  // movement anywhere on the page, so if somebody has asked for less it is the
  // first thing that should go.
  if (reducedMotion()) { land(); return Promise.resolve(); }

  // `top`, not `transform`, and the blur as its own midpoint keyframe — see the
  // note at the top of this file.
  const anim = strip.animate(
    [
      { top: '0px',               filter: 'blur(0px)' },
      {                           filter: 'blur(2.5px)', offset: 0.45 },
      { top: (-turns * h) + 'px', filter: 'blur(0px)' }
    ],
    { duration, easing: 'cubic-bezier(.16,.62,.18,1)', fill: 'forwards' }
  );

  return anim.finished.catch(() => {}).then(land);
}

/**
 * THE FOOTBALL. Rises over the board once the reels have stopped, strains for
 * far longer than is comfortable, and then either settles or bursts.
 *
 * The stages are plain classes on a timer rather than one long keyframe set,
 * because the interesting part is the RHYTHM — a bulge that holds too long, a
 * settle, a squeak, a bigger bulge — and a rhythm is much easier to tune as a
 * list of beats than as percentages of a single animation.
 */
async function footballAct(popped) {
  const stage = document.createElement('div');
  stage.className = 'jp-stage';
  stage.setAttribute('role', 'status');
  stage.innerHTML =
    '<div class="jp-stage-inner">' +
      ballSvg() +
      '<div class="jp-stage-note" aria-live="polite"></div>' +
    '</div>';
  document.body.appendChild(stage);

  const note = stage.querySelector('.jp-stage-note');
  const say = t => { note.textContent = t; };

  // Reduced motion: the outcome, and none of the seven seconds.
  if (reducedMotion()) {
    stage.dataset.beat = popped ? 'burst' : 'settle';
    say(popped ? 'The football popped. Automatic win.' : 'The football held.');
    await wait(1400);
    stage.remove();
    return;
  }

  // Every beat below is identical in both branches. Only `burst` differs, and
  // only at the very end.
  const beats = [
    ['rise',    900],   // it comes up over the board
    ['pump',    900],   // hiss — one more notch of air
    ['creak',  1100],   // the seam whitens, a 2px flinch
    ['bulge1', 1200],   // swells hard, holds far too long...
    ['ease',    700],   // ...and eases back. A squeak of escaping air.
    ['bulge2', 1500],   // bigger, faster, laces visibly separating
  ];

  stage.dataset.beat = 'idle';
  await wait(30);       // one frame, so the first transition actually runs

  for (const [beat, ms] of beats) {
    stage.dataset.beat = beat;
    if (beat === 'pump') say('The house takes a breath…');
    if (beat === 'bulge2') say('Oh, that is not good.');
    await wait(ms);
  }

  if (popped) {
    stage.dataset.beat = 'burst';
    say('POP. The house pays.');
    document.body.classList.add('jp-shake');
    setTimeout(() => document.body.classList.remove('jp-shake'), 700);
    deflateJackpot();
    await wait(2200);
  } else {
    stage.dataset.beat = 'settle';
    say('It held. This time.');
    await wait(1300);
  }

  stage.dataset.beat = 'exit';
  await wait(500);
  stage.remove();
}

/** Put a popped cell into its final state: no team, AUTO WIN, and it scores. */
function finishJackpotCell(td) {
  td.querySelectorAll('.slot-box').forEach(b => b.remove());
  const tag = td.querySelector('.auto-win');
  if (tag) {
    tag.hidden = false;
    tag.classList.add('auto-win-landing');
    setTimeout(() => tag.classList.remove('auto-win-landing'), 1600);
  }
}

/**
 * Spin whatever is due, then run the football over the whole batch.
 *
 * The spun-record is written BEFORE the spin rather than after, so that a
 * re-render arriving mid-spin cannot start a second one on the same cell.
 */
export async function runSlots(opts = {}) {
  const due = dueCells(opts);
  if (!due.length) return 0;
  due.forEach(c => spunThisLoad.add(cellKey(c.td)));

  await Promise.all(due.map((c, i) => spinCell(c, i)));

  const popped = due.filter(c => c.jackpot);
  await footballAct(popped.length > 0);
  popped.forEach(c => finishJackpotCell(c.td));

  // The chip edge, so an ordinary slots pick still says whose choice it was a
  // week later. A popped cell says AUTO WIN in plain words and needs no border
  // to explain itself.
  due.forEach(c => { if (!c.jackpot) markSlotCell(c.td); });

  return due.length;
}

/**
 * The board re-renders whenever a score moves, which rebuilds every cell — so a
 * one-shot call on load would miss a slots cell that arrived with the reveal,
 * and re-running blindly would fight the render. Watching the table and asking
 * again is the honest way round it: runSlots() is idempotent within a page load
 * because `spunThisLoad` is already written by the time anything re-renders.
 *
 * AUTO-REPLAY IS BOUNDED TO THE WEEK BEING PLAYED. Unbounded, opening the board
 * in week 12 would mean forty cells spinning at once and a minute of football
 * before anybody could read a score. index.html sets `data-live-week` when the
 * week on screen is the live one.
 *
 * Which is only safe because of the second half: ANY slots cell can be replayed
 * on demand by clicking it, in any week, forever. Nothing is ever lost — it just
 * waits to be asked for. That is the half that means you cannot miss a spin
 * again.
 */
export default function initSlots() {
  const board = document.getElementById('table-scroll-wrap');
  if (!board) return;

  let queued = false;
  const ask = () => {
    if (queued) return;
    if (document.body.dataset.liveWeek !== '1') return;
    queued = true;
    // One frame plus a beat: lazily-loaded logos have no measurable height the
    // instant they are inserted, and a reel built to a height of zero shows
    // nothing at all.
    requestAnimationFrame(() => setTimeout(() => { queued = false; runSlots(); }, 350));
  };

  ask();
  new MutationObserver(ask).observe(board, { childList: true, subtree: true });

  // Click to watch it again. Delegated, because the board replaces its own rows
  // every minute and a listener bound to a cell would not survive the week.
  board.addEventListener('click', e => {
    const td = e.target?.closest?.('td[data-slots]');
    if (!td || td.querySelector('.slot-box')) return;
    if (document.querySelector('.jp-stage')) return;   // one football at a time
    runSlots({ only: td });
  });
}
