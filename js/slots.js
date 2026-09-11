// js/slots.js — the slot-machine reveal on the leaderboard.
//
// When somebody chooses "let the slots decide" on the picks page, no team is
// saved: the row goes in as an intent, and the database flips the coin at
// kickoff (resolve_slot_picks, in the 20260911 migration). The first time that
// cell is seen afterwards, it spins.
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

const SEEN_PREFIX = 'cfb-slots-seen';

// One transform and/or one tint per symbol, combined at random, so a dozen
// classes give plenty of distinct nonsense without needing a dozen more. The
// landing symbol gets NONE of them — that is the whole gag.
const WARPS = ['squash', 'stretch', 'flip', 'upside', 'tipsy', 'huge', 'tiny', 'melt', 'wobble'];
const TINTS = ['radioactive', 'negative', 'ghost', 'oldtimey', 'xray', 'hot'];

const pick1 = a => a[Math.floor(Math.random() * a.length)];

function reducedMotion() {
  return window.matchMedia?.('(prefers-reduced-motion: reduce)')?.matches ?? false;
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
 * Every cell the slots decided that this browser has not already watched.
 *
 * `data-slots` is set by renderTable from what the database said, so there is no
 * sampling and no guessing here — the board is the source of truth.
 */
function dueCells() {
  const cols = columnTeams();
  const out = [];
  document.querySelectorAll('tbody tr').forEach(tr => {
    if (tr.classList.contains('playoff-divider')) return;
    [...tr.children].forEach((td, col) => {
      if (!td.dataset?.slots || !cols[col]) return;
      const img = td.querySelector('img.logo');
      if (!img) return;                       // coin not flipped yet; next load
      const key = `${SEEN_PREFIX}:${td.dataset.slotsTeam}:${td.dataset.slots}`;
      let seen = false;
      try { seen = !!localStorage.getItem(key); } catch {}
      if (seen) return;
      out.push({ td, img, sides: cols[col], key });
    });
  });
  return out;
}

/**
 * Replace one cell's logo with a reel and spin it onto the logo already there.
 */
function spinCell(cell, order) {
  const { td, img, sides } = cell;
  const landingSrc = img.currentSrc || img.src;
  const landingAlt = img.alt || '';

  // Measured rather than hard-coded at 32px, because the same cell is 24px on a
  // phone and a reel built to the wrong height shows two symbols at once.
  const h = Math.round(img.getBoundingClientRect().height) || 32;

  // A longer strip and a longer spin for each successive cell, so a board with
  // several of them lands one after another instead of all at once.
  //
  // The two numbers move TOGETHER on purpose. Stretching the duration on its own
  // would not lengthen the spin so much as slow it down — the same handful of
  // symbols drifting past instead of a reel running. Roughly 8.5 symbols a
  // second is the pace that reads as a slot machine, so more time buys
  // proportionally more strip and the speed stays put.
  //
  // Doubled from 1.9s/2.5s/3.0s. This is the reveal rather than the choosing:
  // the player has been waiting since Thursday to find out what the house did
  // with their game, and the board is worth holding on for a moment longer than
  // the lever was.
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

  img.replaceWith(box);
  td.classList.add('slot-cell');

  const land = () => {
    td.classList.remove('slot-cell');
    td.classList.add('slot-landed');
    box.replaceWith(img);
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
 * Spin whatever is due.
 *
 * The seen-record is written BEFORE the spin rather than after. If the tab is
 * closed mid-spin the player misses it, which is a smaller wrong than a cell
 * that spins again on every load because nothing ever got as far as recording
 * it.
 */
export async function runSlots() {
  const due = dueCells();
  if (!due.length) return 0;
  for (const c of due) {
    try { localStorage.setItem(c.key, String(Date.now())); } catch {}
  }
  await Promise.all(due.map((c, i) => spinCell(c, i)));
  return due.length;
}

/**
 * The board re-renders whenever a score moves, which rebuilds every cell — so a
 * one-shot call on load would miss a slots cell that arrived with the reveal,
 * and re-running blindly would fight the render. Watching the table and asking
 * again is the honest way round it: runSlots() is idempotent because the
 * seen-record is already written by the time anything re-renders.
 */
export default function initSlots() {
  const board = document.getElementById('table-scroll-wrap');
  if (!board) return;

  let queued = false;
  const ask = () => {
    if (queued) return;
    queued = true;
    // One frame plus a beat: lazily-loaded logos have no measurable height the
    // instant they are inserted, and a reel built to a height of zero shows
    // nothing at all.
    requestAnimationFrame(() => setTimeout(() => { queued = false; runSlots(); }, 350));
  };

  ask();
  new MutationObserver(ask).observe(board, { childList: true, subtree: true });
}
