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
// AND THE THIRD SYMBOL: THE FOOTBALL. It is on the reel, it goes past on EVERY
// spin, and landing on it is the jackpot - at which point it bursts, in the
// square, and leaves AUTO WIN behind.
//
// That replaces an arrangement where the reel landed on a TEAM and the marquee
// football then reached down and swapped it for AUTO WIN. Two events describing
// one outcome, and for a second and a half the reel was showing a pick that was
// never made.
//
// Having it fly past a losing spin four or five times is not decoration either.
// It is the whole reason a slot machine is tense: you have to have SEEN the
// thing you did not get.
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
// IT STRAINS WHILE THE REELS ARE STILL TURNING, and resolves before they stop.
// Running it afterwards meant the reels landed, everything went still, and only
// then did the ball turn up - two sequences queueing politely, with a dead beat
// in between. Now the ball is already shaking as the first reel starts, it pops
// or holds while they are all still spinning, and the cells land into the
// answer. If it popped, the reel underneath lands on a team nobody will ever
// see, and the square turns over to AUTO WIN.
//
// AND IT IS THE BALL THAT IS ALREADY THERE. The reveal does not build a football
// and it does not take over the screen - it grows the one sitting in the strip
// at the top of the page, then puts it back. The board underneath is never
// covered by the thing describing it, and the object the league has been
// watching all week is the object that strains.
//
// THE RULE THAT MAKES IT WORK: every frame before the last moment is identical
// whether it pops or not. If the pop version strained even slightly harder the
// league would learn to read it within three weeks and the tease would be dead.
// Nothing below branches on the outcome until `burst`.

import { markSlotCell } from './utils.js';
import { ensureStage, dismissStage, deflateJackpot, primeMeter, ballRig } from './jackpot.js';
import { scheduleClick, scheduleStop, strainStart, strainStop, pop, hold, fanfare } from './slotsound.js';
import celebrateJackpot, { playerNameFor } from './celebrate.js';

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

// How long the landed football sits there before it goes, and how long the going
// takes. Exported to nobody, but footballAct waits MORPH_TOTAL before it says
// the payoff line - the marquee must not announce AUTO WIN while the square it
// is talking about is still showing a football.
const MORPH_HOLD = 700;
const MORPH_BURST = 420;
const MORPH_TOTAL = MORPH_HOLD + MORPH_BURST;

// THE REEL'S SHAPE, in one place, because the motion and the sound both have to
// come out of it. A click happens when a symbol passes the window - a fact about
// DISTANCE - and it has to be scheduled as a TIME, so anything that changes how
// the reel moves has to change the click track by exactly as much or the two
// come apart. Two descriptions of the same curve is two things to keep in step;
// this is one.
// Gentle on purpose. The old curve decelerated so hard that its LAST symbol took
// 1340ms and the crawl then restarted at 229 - a stall followed by a speed-up,
// right at the moment the reel is supposed to be settling. This hands over at
// 196ms into a 229ms first crawl step, so the slowdown is one continuous thing.
const FAST_EASE = [0.3, 0.3, 0.7, 0.9];

// THE CRAWL. The last six symbols take the final forty per cent of the spin,
// each one slower than the one before, so the reel stops creeping rather than
// gliding. This is the part a real machine has and a scrolling div does not: by
// the last two symbols they are arriving one at a time, slowly enough to read,
// and you know what is coming before it gets there.
const CRAWL = 6;
const CRAWL_AT = 0.60;                          // when the creeping starts
const CRAWL_WEIGHTS = [1, 1.35, 1.85, 2.6, 3.7, 5.2];
const CRAWL_TOTAL = CRAWL_WEIGHTS.reduce((a, b) => a + b, 0);

/** A cubic-bezier's y for a given parameter t. P0 and P3 are fixed at 0 and 1. */
function bezAt(t, a, b) {
  const u = 1 - t;
  return 3 * u * u * t * a + 3 * u * t * t * b + t * t * t;
}

/**
 * Where in the SPIN a given fraction of the DISTANCE happens.
 *
 * The timing function maps time to progress; this needs the opposite. Bisection
 * is plenty: the curve is monotonic and twenty steps put it inside a thousandth.
 */
function timeOfProgress(y, [x1, y1, x2, y2]) {
  let lo = 0, hi = 1, t = y;
  for (let i = 0; i < 20; i++) {
    t = (lo + hi) / 2;
    if (bezAt(t, y1, y2) < y) lo = t; else hi = t;
  }
  return bezAt(t, x1, x2);
}

/**
 * The fraction of the spin at which symbol `k` of `turns` reaches the window.
 *
 * Piecewise on purpose: everything up to the last CRAWL symbols is the ordinary
 * decelerating sweep, and the last few are handed a fixed, widening slice of
 * what is left. THE SAME FUNCTION lays out the keyframes and schedules the
 * clicks, which is the only way they cannot drift apart.
 */
function symbolTime(k, turns) {
  const fast = turns - CRAWL;
  if (k <= fast) {
    return CRAWL_AT * timeOfProgress(k / fast, FAST_EASE);
  }
  let w = 0;
  for (let i = 0; i < k - fast; i++) w += CRAWL_WEIGHTS[i];
  return CRAWL_AT + (1 - CRAWL_AT) * (w / CRAWL_TOTAL);
}

/**
 * The keyframes, built from that same schedule.
 *
 * A keyframe per crawl symbol is what makes the creeping visible: without them
 * the browser interpolates straight through the last six and they slide past as
 * one movement, however slow the easing is.
 */
function reelFrames(turns, h) {
  const stop = -turns * h;
  const fast = turns - CRAWL;
  const frames = [
    { top: '0px', filter: 'blur(0px)', offset: 0, easing: 'cubic-bezier(.28,.05,.5,.7)' },
    { filter: 'blur(3px)', offset: 0.3 },
    { top: (-fast * h) + 'px', filter: 'blur(1px)', offset: CRAWL_AT, easing: 'linear' },
  ];
  // One per crawl symbol, each easing to a near-stop before the next begins.
  for (let k = 1; k <= CRAWL - 1; k++) {
    frames.push({
      top: (-(fast + k) * h) + 'px',
      filter: 'blur(0px)',
      offset: symbolTime(fast + k, turns),
      easing: 'cubic-bezier(.2,.7,.25,1)',
    });
  }
  // THE DETENT. It comes up a symbol-edge short and then drops the rest of the
  // way, so it arrives at a stop instead of gliding to one. Six pixels and a
  // tenth of a second, and most of the difference between a reel and a div.
  frames.push({ top: (stop + 6) + 'px', offset: 0.965, easing: 'cubic-bezier(.3,.6,.4,1)' });
  frames.push({ top: stop + 'px', offset: 1, easing: 'cubic-bezier(.5,0,.7,1.5)' });
  return frames;
}

/**
 * Schedule one reel's worth of detent clicks, from the same schedule.
 *
 * ALL AT ONCE, against the audio clock, rather than fired from timers as the
 * spin goes. A click track driven by setTimeout drifts by a few milliseconds a
 * time under load, and drift is exactly what stops it sounding mechanical.
 *
 * The fast part of a real reel is a rattle rather than countable clicks, so
 * anything closer than MIN_GAP is dropped and the survivors get louder the
 * slower they are - which is what turns the crawl into six deliberate, separate
 * clunks instead of just fewer noises.
 */
function scheduleReelSound(turns, duration) {
  const MIN_GAP = 0.045;
  let last = -1;
  // Stops one short of the landing symbol on purpose: scheduleStop() sounds its
  // own click on top of the thunk, and three noises at one instant is a splat
  // rather than an arrival.
  for (let k = 1; k < turns; k++) {
    const at = symbolTime(k, turns) * duration / 1000;
    const gap = at - last;
    if (last >= 0 && gap < MIN_GAP) continue;
    // Fatness is simply how slow this click is, normalised against the longest
    // gap the crawl produces. It is the same number twice - louder AND lower -
    // because that is what a decelerating mechanism actually does.
    const fat = Math.max(0, Math.min(1, (gap - 0.08) / 0.9));
    scheduleClick(at, Math.min(1, 0.45 + gap * 4), fat);
    last = at;
  }
  scheduleStop(duration / 1000);
}

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
function dueCells({ only = null, replay = false } = {}) {
  const cols = columnTeams();
  const out = [];
  document.querySelectorAll('tbody tr').forEach(tr => {
    if (tr.classList.contains('playoff-divider')) return;
    [...tr.children].forEach((td, col) => {
      if (!td.dataset?.slots || !cols[col]) return;
      if (only && td !== only) return;
      // A deliberate replay ignores the claimed set for THIS call only. Clearing
      // it globally - which is what the lab used to do - unclaims the cells for
      // the MutationObserver too, and the observer then queues a second reveal
      // of its own on top of the one being asked for.
      if (!only && !replay && spunThisLoad.has(cellKey(td))) return;
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
/** One team crest on the reel, mangled or not. */
function teamSymbol(src, h, clean) {
  const sym = document.createElement('img');
  sym.src = src;
  sym.alt = '';
  sym.className = 'slot-sym';
  sym.style.height = h + 'px';
  if (!clean) {
    // Not every symbol is warped. An unbroken run of distortions stops reading
    // as distortion and starts reading as the design.
    if (Math.random() < 0.82) sym.classList.add('slot-warp-' + pick1(WARPS));
    if (Math.random() < 0.55) sym.classList.add('slot-tint-' + pick1(TINTS));
  }
  return sym;
}

/**
 * The jackpot symbol: a football.
 *
 * A FLAT one, not the marquee's. That ball carries four gradients and a pattern
 * behind namespaced ids, and there are eight of these on every reel and three
 * reels on screen - two dozen copies of all that, at 28 pixels, where none of it
 * would be visible anyway. This is five elements and no ids at all.
 *
 * Never mangled, either. It is the one thing on the reel that is worth
 * something, and a melting one would read as another joke.
 */
function ballSymbol(h) {
  const sym = document.createElement('span');
  sym.className = 'slot-sym slot-sym-ball';
  sym.style.height = h + 'px';
  sym.innerHTML =
    '<svg viewBox="0 0 200 120" aria-hidden="true" focusable="false">' +
      '<path class="ss-hide" d="M6,60 C36,6 164,6 194,60 C164,114 36,114 6,60 Z"/>' +
      '<path class="ss-stripe" d="M44,14 C38,40 38,80 44,106 L52,106 C46,80 46,40 52,14 Z"/>' +
      '<path class="ss-stripe" d="M156,14 C162,40 162,80 156,106 L148,106 C154,80 154,40 148,14 Z"/>' +
      '<g class="ss-laces">' +
        '<path d="M84,60 L116,60"/>' +
        '<path d="M88,52 L88,68"/><path d="M96,51 L96,69"/>' +
        '<path d="M104,51 L104,69"/><path d="M112,52 L112,68"/>' +
      '</g>' +
    '</svg>';
  return sym;
}

function spinCell(cell, order) {
  const { td, img, tag, jackpot, sides } = cell;

  // Measured rather than hard-coded at 32px, because the same cell is 24px on a
  // phone and a reel built to the wrong height shows two symbols at once.
  const probe = img || tag;
  const h = Math.round(probe.getBoundingClientRect().height) || 32;

  const landingSrc = img ? (img.currentSrc || img.src) : null;
  const landingAlt = img ? (img.alt || '') : '';

  // A longer strip and a longer spin for each successive cell, so a board with
  // several of them lands one after another instead of all at once.
  //
  // The two numbers move TOGETHER on purpose. Stretching the duration on its own
  // would not lengthen the spin so much as slow it down — the same handful of
  // symbols drifting past instead of a reel running. Roughly 8.5 symbols a
  // second is the pace that reads as a slot machine, so more time buys
  // proportionally more strip and the speed stays put.
  // LONGER, AND LANDING CLOSER TOGETHER. The two numbers move together, as the
  // note above says: roughly 8.5 symbols a second is the pace that reads as a
  // slot machine, so more time has to buy proportionally more strip or the reel
  // gets slower rather than longer. 76 over nine seconds is that same pace.
  //
  // The stagger came DOWN while the length went up - 900ms between cells instead
  // of 1800. A long tail of reels trickling in one every two seconds is not
  // suspense, it is waiting. The suspense is the nine seconds before any of them
  // lands; once the first one goes the rest should come down on top of each
  // other.
  const turns = 76 + order * 8;
  // DIVIDED BY THE SAME FACTOR THE FOOTBALL USES. It was in real milliseconds
  // while the ball's beats were scaled, so the sandbox's speed slider drove the
  // two halves at different rates and they came apart - which is precisely what
  // "the rolling does not match up" looks like. One clock, both halves.
  const duration = (9000 + order * 900) / revealSpeed;

  const strip = document.createElement('div');
  strip.className = 'slot-strip';

  // The nonsense, then the truth. Sides alternate, so it reads as a decision
  // between these two and nobody else - with the football coming round every
  // seventh symbol or so, offset per reel so the cells do not flash it in unison.
  //
  // Seven is chosen to be SEEN rather than counted: often enough to go past four
  // or five times in a six-second spin, rare enough that it still registers as
  // the odd one out every time it does.
  const BALL_EVERY = 7;
  const offset = Math.floor(Math.random() * BALL_EVERY);
  let side = 0;
  for (let i = 0; i < turns; i++) {
    strip.appendChild(i % BALL_EVERY === offset
      ? ballSymbol(h)
      : teamSymbol(sides[side++ % 2].src, h, false));
  }

  // The landing symbol, and the only one on the reel that is clean. A popped
  // pick has no team behind it, so the reel stops on the football itself.
  const finalSym = jackpot ? ballSymbol(h) : teamSymbol(landingSrc, h, true);
  finalSym.classList.add('slot-sym-final');
  if (!jackpot) finalSym.alt = landingAlt;
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
    if (img) {
      box.replaceWith(img);
    } else {
      // It stopped on the football. Leave it sitting there for a beat - long
      // enough to read what it stopped on - and then let it go, and what is
      // underneath is AUTO WIN.
      const ball = ballSymbol(h);
      ball.classList.add('slot-ball-landed');
      box.replaceWith(ball);
      setTimeout(() => {
        ball.classList.add('slot-ball-bursting');
        setTimeout(() => {
          ball.replaceWith(tag);
          tag.hidden = false;
          tag.classList.add('auto-win-landing');
          setTimeout(() => tag.classList.remove('auto-win-landing'), 1600);
        }, MORPH_BURST / revealSpeed);
      }, MORPH_HOLD / revealSpeed);
    }
    setTimeout(() => td.classList.remove('slot-landed'), 900);
  };

  // Reduced motion gets the result and none of the theatre. This is the most
  // movement anywhere on the page, so if somebody has asked for less it is the
  // first thing that should go.
  if (reducedMotion()) { land(); return Promise.resolve(); }

  // `top`, not `transform`, and the blur as its own keyframe — see the note at
  // the top of this file. The shape comes from reelFrames(), which is the same
  // schedule the click track is built from.
  const anim = strip.animate(reelFrames(turns, h), { duration, fill: 'forwards' });

  scheduleReelSound(turns, duration);

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
async function footballAct(popped, reels, poppedCells = []) {
  // The marquee, on the page that has one: for the length of this roll the
  // football stands where "CFB Pick'em Leaderboard" is.
  const strip = ensureStage();
  if (!strip) { await reels; return; }      // nowhere to play; still wait it out

  let rig = null;

  // Looked up per call rather than captured once. Anything that re-renders the
  // strip mid-reveal swaps this element out underneath us, and a held reference
  // would then be writing the whole back half of the sequence into a node that
  // is no longer on the page.
  const say = t => {
    const n = strip.querySelector('.jp-note');
    if (n) n.textContent = t;
  };
  const done = () => {
    delete strip.dataset.beat;
    say('');
    strainStop();          // never leave the drone running if a beat threw
    rig?.stop();           // and stop driving a ball that is no longer there
    dismissStage();        // and give the title back; it was only ever borrowed
  };

  // If the page is scrolled down the board, the marquee is above it. Bring it
  // into view - gently, and only as far as it takes. A reveal nobody can see is
  // the problem this whole thing was built to fix.
  try { strip.scrollIntoView?.({ behavior: 'smooth', block: 'nearest' }); } catch {}

  // Reduced motion: the outcome, and none of the theatre. It still waits for the
  // reels, because saying what happened before the board shows it is the one
  // thing this sequence must never do.
  if (reducedMotion()) {
    strip.dataset.beat = popped ? 'burst' : 'settle';
    if (popped) pop(); else hold();
    say(popped ? 'The football popped. Automatic win.' : 'The football held.');
    await reels;
    done();
    return;
  }

  // TIMED AGAINST THE REELS. The first cell lands at 9000ms and everything up to
  // the resolution has to fit inside that, because the whole point is that the
  // ball answers while they are all still spinning.
  //
  // THREE BULGES, TWO RELIEFS, over eight and a half seconds. The old version
  // ran this in five and it was over before it had started: a build only works
  // if it outlasts your patience for it. Each bulge goes further than the last
  // and each relief lands higher than the last, so the trend is relentlessly
  // upward even while it is backing down - and by the third one nobody knows
  // which way it ends, which is the only state worth being in.
  // Beat, how long it lasts, how hard the skin is stretched, and how big a
  // shove the membrane gets going into it. The pressure is what the ball's
  // GEOMETRY is driven from - the waist swells, the tips draw in, the laces
  // separate - and the kick is what makes it resonate afterwards instead of
  // gliding between two resting shapes.
  const beats = [
    ['rise',    900, 0.16, 0.4],
    ['pump',   1000, 0.38, 1.1],
    ['creak',  1200, 0.45, 0.5],
    ['bulge1', 1300, 0.70, 1.7],
    ['hold1',   900, 0.50, 0.9],   // it held, you think
    ['bulge2', 1200, 0.82, 2.0],
    ['ease',    800, 0.63, 1.0],   // ...it held again?
    ['bulge3', 1200, 1.00, 2.6],
  ];

  rig = ballRig(strip);

  strip.dataset.beat = 'idle';
  await wait(30);       // one frame, so the first transition actually runs

  // One held note climbing for exactly as long as the strain lasts, which is the
  // sound every machine ever built uses to say something is about to happen. It
  // is doing the same job as the tremor, in the other channel: where it has got
  // to IS how close the ball is.
  strainStart(beats.reduce((a, b) => a + b[1], 0) / 1000 / revealSpeed);

  // NOTHING IS SAID WHILE IT STRAINS. There was a line of commentary on every
  // beat - "Oh. Oh no.", "...it is holding." - and it was doing the tension's job
  // for it, badly. A machine does not narrate itself. The ball is shaking, the
  // seam is splitting, the reels are roaring: if that is not enough then more
  // words will not fix it, and if it is enough then the words are in the way.
  for (const [beat, ms, press, kick] of beats) {
    strip.dataset.beat = beat;
    rig?.set(press);
    rig?.kick(kick);
    await wait(ms);
  }

  // It answers here, with the reels still turning. What it must NOT do is
  // announce the payoff here: the AUTO WIN square does not exist until the reel
  // above it lands, and a marquee that says "the house pays" over a board still
  // spinning is the machine talking about something nobody can see yet.
  // It answers here, with the reels still turning. The only things said out loud
  // in the whole sequence are this and the payoff, and both are for screen
  // readers - the note is off-screen unless somebody has asked for reduced
  // motion, in which case it is all they are getting.
  // It answers here, with the reels still turning. The only things said out loud
  // in the whole sequence are this and the payoff, and both are for screen
  // readers - the note is off-screen unless somebody has asked for reduced
  // motion, in which case it is all they are getting.
  if (popped) {
    // Freeze the shape it had reached and hand the two halves to the shards, so
    // what tears apart is the ball that was actually on screen rather than a
    // generic one.
    rig?.rupture();
    strip.dataset.beat = 'burst';
    say('The football went. Automatic win.');
    pop();
    deflateJackpot();
    await wait(900);              // the burst needs room to actually burst
  } else {
    strip.dataset.beat = 'settle';
    say('The football held.');
    rig?.set(0.08);
    rig?.kick(1.4);          // the air going out of it, and the skin flexing
    hold();
  }

  await reels;

  // NOTHING TO CELEBRATE, SO IT DOES NOT LINGER. When the ball holds, the
  // football starts leaving the moment the last reel stops - it used to sit
  // there for another two seconds having a quiet moment about a thing that did
  // not happen, and that dead air was the longest part of the whole reveal.
  //
  // A pop is different: the cell it is talking about is still showing the
  // football it landed on for another beat while that bursts into AUTO WIN, and
  // saying the payoff over it would put the marquee back in front of the board.
  if (popped) await wait(MORPH_TOTAL);

  say(popped ? 'The house pays. That pick is an automatic win.'
             : 'The reels decided it, fifty-fifty.');

  // AND THEN THE WHOLE SCREEN, once a year. Everything else in this reveal is
  // built to survive being watched fifty times; this is built to be watched once
  // and remembered, which wants the opposite restraint entirely.
  if (popped) {
    fanfare();
    await celebrateJackpot(playerNameFor(poppedCells[0]?.td));
  }

  strip.dataset.beat = 'exit';
  await wait(popped ? 600 : 400);
  done();
}

// ONE REVEAL AT A TIME, and this is not a nicety.
//
// There is a single marquee. Two footballAct()s running together both write
// data-beat and both write the note, on the same element - so the ball jumps
// between their two sequences and the caption contradicts itself. Watched in a
// real browser it read:
//
//   rise -> pump -> hold1 -> bulge2 -> creak -> ease -> bulge1 -> settle ->
//   hold1 -> bulge2 -> ease -> burst
//
// with "It held." and "The reels have it, straight fifty-fifty" arriving three
// seconds before "IT WENT." That is two reveals interleaved, and it is what
// "the jackpot does not line up with how they roll" looks like from the outside.
//
// It is easy to hit: the board re-renders every minute on live scores, and any
// re-render that brings a cell the previous one did not have starts a second
// run while the first is still going.
//
// So runs are CHAINED rather than dropped. The cells are claimed immediately -
// nothing can pick them up twice - and their reveal waits its turn.
let revealChain = Promise.resolve();
let revealing = false;

async function performReveal(due) {
  revealing = true;
  // Announced on the body rather than through an import, so index.html can hold
  // its automatic repaints back without either file having to know about the
  // other's internals. A re-render replaces every cell in the table, including
  // the ones being spun - see the note on 'slots:idle' below.
  try { document.body.dataset.slotsRevealing = '1'; } catch {}
  try {
    const popped = due.filter(c => c.jackpot);

    // Both at once, deliberately. The ball is shaking before the first reel has
    // got going and has answered before the first one lands - and it is handed
    // the reels so it can hold its tongue until the board can back it up.
    const reels = Promise.all(due.map((c, i) => spinCell(c, i)));
    const ball  = footballAct(popped.length > 0, reels, popped);

    await reels;

    // The chip edge, so an ordinary slots pick still says whose choice it was a
    // week later. A popped cell says AUTO WIN in plain words and needs no border
    // to explain itself.
    due.forEach(c => { if (!c.jackpot) markSlotCell(c.td); });

    // The reels are done but the ball may still be deflating.
    await ball;
  } finally {
    revealing = false;
    try {
      delete document.body.dataset.slotsRevealing;
      // Whoever held a repaint back can run it now.
      document.dispatchEvent(new CustomEvent('slots:idle'));
    } catch {}
  }
  return due.length;
}

/**
 * Spin whatever is due, then run the football over the whole batch.
 *
 * The spun-record is written BEFORE anything starts, so a re-render arriving
 * mid-spin cannot queue the same cell a second time.
 */
export function runSlots(opts = {}) {
  const due = dueCells(opts);
  if (!due.length) return Promise.resolve(0);
  due.forEach(c => spunThisLoad.add(cellKey(c.td)));

  revealChain = revealChain
    .catch(() => {})                       // one bad run must not stop the next
    .then(() => performReveal(due));
  return revealChain;
}

/** Is a reveal on screen right now? For the sandbox, and for click-to-replay. */
export function isRevealing() {
  return revealing;
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

  // Nothing draws the meter at rest any more, so nothing else would load it -
  // and the ball would take the marquee at zero pressure however long the
  // drought had run. Fired and forgotten: the reveal is seconds away at the
  // earliest, and if it never lands the ball is just drawn slack.
  primeMeter();

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
    if (revealing) return;                 // one football at a time
    runSlots({ only: td });
  });
}
