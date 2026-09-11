// js/sandbox.js — the commissioner's scratch area, overlaid on the leaderboard.
//
// Loaded by index.html and ONLY when the URL carries ?sandbox=1. An ordinary
// visit to the leaderboard never requests this file, so the league pays nothing
// for it — not a byte, not a round trip.
//
// ------------------------------------------------------------------------
// WHAT THIS IS FOR
//
// Trying changes to the leaderboard against the real, current standings without
// touching index.html. Everything experimental lives in this file and in
// css/sandbox.css; index.html stays the single copy of the markup. That is the
// whole point of doing it this way rather than copying the page: there is no
// second 2,900-line file to keep in sync, and shipping an experiment means
// moving the code OUT of here rather than diffing two near-identical pages.
//
// The constraint that buys that: you can restyle, re-render and add to the
// leaderboard, but you cannot restructure its markup. If an experiment ever
// genuinely needs different HTML, that is the moment to talk about a real
// sandbox.html — not the moment to start editing index.html.
//
// ------------------------------------------------------------------------
// READ ONLY
//
// Nothing in this file may write to Supabase. The sandbox renders the live
// league against the live database, and a half-finished experiment must not be
// able to touch a real pick, score or standing. If you want to test a write,
// that is a different tool and it needs a different conversation.
//
// ------------------------------------------------------------------------
// WHAT THE GATE IS, AND WHAT IT IS NOT
//
// It is: the nav link renders only for teams.commissioner, and the overlay below
// runs only for teams.commissioner. Anyone else who types the URL gets a
// perfectly ordinary leaderboard with no hint that the parameter meant anything.
// The check fails closed — a dropped request or a missing team row is "no".
//
// It is NOT secrecy. This is a static site: this file is publicly fetchable and
// readable by anyone who guesses its name, and a determined person can run it
// from devtools. What that person would see is an experimental layout drawn from
// data the anon key can already read (teams, all_games and picks are all
// readable today). So the rule is simple:
//
//   NOTHING MAY APPEAR HERE THAT IS NOT ALREADY PUBLIC.
//
// A genuinely private area needs a server-side gate — an RLS-protected table or
// an Edge Function — not a page and not a query parameter.

import { sessionInfo } from './session.js';

// The gate runs at the BOTTOM of this file, not here. Function declarations
// hoist but `const` and `let` do not, so calling enableSandbox() from up here
// reaches the experiment section's constants while they are still in the
// temporal dead zone and throws before anything renders. Kept as a note because
// the natural place to put this line is exactly where it does not work.

function enableSandbox() {
  // A hook for css/sandbox.css so experiments can be written as plain rules
  // scoped under [data-sandbox] rather than as inline styles from JS.
  document.documentElement.dataset.sandbox = 'on';

  // The stylesheet is pulled in from here rather than linked in index.html, so
  // switching the sandbox on or off stays a one-file decision.
  //
  // CACHE-BUSTED ON EVERY LOAD, and not by a content hash like the rest of the
  // site. tools/stamp-assets.mjs only rewrites href="..." inside .html, so a
  // stylesheet fetched from JS never gets a version and GitHub Pages serves it
  // with max-age=600 — meaning an edit here would take up to ten minutes to
  // show up, which is precisely the wrong behaviour for a scratch area. A
  // timestamp guarantees a fresh file every reload. It costs the league nothing
  // because nobody else ever requests it.
  const css = document.createElement('link');
  css.rel = 'stylesheet';
  css.href = `./css/sandbox.css?v=${Date.now()}`;
  document.head.appendChild(css);

  // The tab title, because the expected way to use this is two tabs side by side
  // and the favicon is identical in both.
  document.title = `SANDBOX — ${document.title}`;

  mountBanner();

  // --------------------------------------------------------------------
  // EXPERIMENTS GO BELOW THIS LINE.
  //
  // The leaderboard has already rendered by the time this runs, so read the DOM
  // and change it; do not try to race index.html's own init. If you need to
  // react to a later re-render (the page re-renders on live score updates), a
  // MutationObserver on #table-scroll-wrap is the honest way to do it.
  // --------------------------------------------------------------------

  initSlotMock();
}

function mountBanner() {
  const bar = document.createElement('div');
  bar.className = 'sandbox-banner';
  bar.setAttribute('role', 'status');
  bar.innerHTML = `
    <span class="sandbox-banner-dot" aria-hidden="true"></span>
    <span class="sandbox-banner-text">Sandbox &mdash; visible to you only</span>
    <a class="sandbox-banner-exit" href="./index.html">Leave</a>
  `;
  document.body.appendChild(bar);
}

/* ======================================================================
 * "LET THE SLOTS DECIDE" — front-end mockup
 * ======================================================================
 *
 * WHAT IS BEING MOCKED. A player opts out of picking a side and lets the house
 * choose. The pick itself is settled on the backend the moment they opt in — it
 * is a coin flip between the two teams, and nothing in this file decides it.
 * What this mocks is the REVEAL: the first time that player's board is opened
 * after picks unlock, that one cell spins like a slot machine before showing
 * what the coin flip already said.
 *
 * The animation is therefore theatre over a settled result, which is the only
 * honest way to build it. A spin that decided the pick in the browser could be
 * re-rolled by reloading the page until it said something you liked.
 *
 * NOTHING HERE IS REAL. No pick is written, no table is touched, nobody has
 * opted in. The cells below are chosen at random from whatever the board is
 * already showing, purely so the motion can be judged against real logos at the
 * real size. Wiring this to a genuine `slots` column on `picks` is a separate
 * job and a separate conversation.
 *
 * THE REEL, adapted from johakr/html5-slot-machine: a strip of symbols inside an
 * overflow-hidden box driven by the Web Animations API, the blur peaking
 * mid-spin, and the landing symbol sitting at the END of the strip so the spin
 * arrives on it rather than being snapped into place.
 *
 * Two things are borrowed deliberately. The reference animates `top` rather
 * than `transform: translateY`, because Safari will not animate a transform and
 * a filter on the same element at the same time — hard-won, so kept. And each
 * reel gets its own duration, which is what makes several cells land in
 * sequence rather than together.
 *
 * What is NOT borrowed is the symbols. The reel shows the two teams in that
 * game and nobody else: a reel that flashed teams who were never in the game
 * would be funny once and misleading afterwards. The comedy comes from mangling
 * the two real logos on the way past — squashed, upside down, radioactive,
 * melting — and landing on a clean, correct one. The joke is that the punchline
 * is sober.
 */

const SLOT_SEEN_PREFIX = 'cfb-slots-seen';

// One transform and/or one tint per symbol, combined at random, so a dozen
// classes give plenty of distinct nonsense without needing a dozen more. The
// landing symbol gets NONE of them — that is the whole gag.
const SLOT_WARPS = ['squash', 'stretch', 'flip', 'upside', 'tipsy', 'huge', 'tiny', 'melt', 'wobble'];
const SLOT_TINTS = ['radioactive', 'negative', 'ghost', 'oldtimey', 'xray', 'hot'];

const rand  = n => Math.floor(Math.random() * n);
const pick1 = a => a[rand(a.length)];

function reducedMotion() {
  return window.matchMedia?.('(prefers-reduced-motion: reduce)')?.matches ?? false;
}

/**
 * The two teams in each game column, read straight off the header the board has
 * already rendered. No query: the logos and abbreviations are on screen, which
 * keeps this genuinely read-only and keeps the mock honest about what a real
 * implementation would have to hand anyway.
 *
 * Indexed by column, with nulls for the Team and Points columns.
 */
function columnTeams() {
  return [...document.querySelectorAll('#thead-row th')].map(th => {
    const logos = [...th.querySelectorAll('img.game-hdr-logo')];
    const abbrs = [...th.querySelectorAll('.game-hdr-abbr')].map(e => e.textContent.trim());
    if (logos.length < 2) return null;
    return {
      sides: logos.slice(0, 2).map((img, i) => ({
        src: img.currentSrc || img.src,
        name: img.alt || abbrs[i] || '',
        abbr: abbrs[i] || ''
      }))
    };
  });
}

/** Every pick cell showing a logo — i.e. a revealed pick there is something to spin onto. */
function spinnableCells() {
  const cols = columnTeams();
  const out = [];
  document.querySelectorAll('tbody tr').forEach(tr => {
    if (tr.classList.contains('playoff-divider')) return;
    [...tr.children].forEach((td, col) => {
      if (col < 2 || !cols[col]) return;
      const img = td.querySelector('img.logo');
      if (!img) return;                       // no pick, or not revealed yet
      out.push({ td, img, col, game: cols[col], row: tr });
    });
  });
  return out;
}

/**
 * Replace one cell's logo with a reel and spin it onto the logo that was already
 * there. The landing symbol is built from the cell's OWN image, so the reel
 * cannot disagree with the pick: it lands on what the board already said.
 */
function spinCell(cell, order) {
  const { td, img, game } = cell;
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
  // symbols drifting past instead of a reel running. Roughly 8.5 symbols a second
  // is the pace that reads as a slot machine, so more time buys proportionally
  // more strip and the speed stays put.
  const turns = 16 + order * 6;
  const duration = 1900 + order * 560;

  const strip = document.createElement('div');
  strip.className = 'slot-strip';

  // The nonsense, then the truth. Sides alternate, so it reads as a decision
  // between these two and nobody else.
  for (let i = 0; i < turns; i++) {
    const side = game.sides[i % 2];
    const sym = document.createElement('img');
    sym.src = side.src;
    sym.alt = '';
    sym.className = 'slot-sym';
    sym.style.height = h + 'px';
    // Not every symbol is warped. An unbroken run of distortions stops reading
    // as distortion and starts reading as the design.
    if (Math.random() < 0.82) sym.classList.add('slot-warp-' + pick1(SLOT_WARPS));
    if (Math.random() < 0.55) sym.classList.add('slot-tint-' + pick1(SLOT_TINTS));
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
  // note at the top of this section.
  const anim = strip.animate(
    [
      { top: '0px',             filter: 'blur(0px)' },
      {                         filter: 'blur(2.5px)', offset: 0.45 },
      { top: (-turns * h) + 'px', filter: 'blur(0px)' }
    ],
    { duration, easing: 'cubic-bezier(.16,.62,.18,1)', fill: 'forwards' }
  );

  return anim.finished.catch(() => {}).then(land);
}

/** Has this player already watched this cell decide itself? */
function seenKey(rowIdx, col) {
  return SLOT_SEEN_PREFIX + ':' + rowIdx + ':' + col;
}

/**
 * A stable pseudo-random rank for a cell — scattered enough to look arbitrary,
 * identical on every load. Used to choose which cells are slot cells; see the
 * note in runSlots for why that must not be a real shuffle.
 */
function stableRank(rowIdx, col) {
  const h = Math.sin((rowIdx + 1) * 12.9898 + (col + 1) * 78.233) * 43758.5453;
  return h - Math.floor(h);
}

let slotPanel = null;
let slotCount = 3;

async function runSlots(opts) {
  const force = !!(opts && opts.force);
  const all = spinnableCells();
  if (!all.length) {
    setPanelNote('No revealed picks on this board — choose an earlier week above, then Replay.');
    return;
  }

  // At most one cell per row, so it reads as "these players used the slots"
  // rather than as something happening to a whole column.
  const byRow = new Map();
  for (const c of all) if (!byRow.has(c.row)) byRow.set(c.row, c);

  // WHICH cells are slot cells has to be STABLE across loads, and it was not:
  // a random shuffle here picked a different set every visit, so a second visit
  // found cells it had never recorded and span them again — which is precisely
  // the behaviour the once-only rule exists to prevent. In the real feature the
  // set comes from the data (who opted in), never from a dice roll, so the mock
  // has to be stable too or it cannot demonstrate the thing it is mocking.
  const candidates = [...byRow.values()]
    .map(c => ({ c, rowIdx: [...c.row.parentNode.children].indexOf(c.row) }))
    .sort((a, b) => stableRank(a.rowIdx, a.c.col) - stableRank(b.rowIdx, b.c.col))
    .slice(0, Math.min(slotCount, byRow.size));

  const due = candidates.filter(({ c, rowIdx }) => {
    const k = seenKey(rowIdx, c.col);
    if (!force && localStorage.getItem(k)) return false;
    try { localStorage.setItem(k, '1'); } catch {}
    return true;
  }).map(x => x.c);

  if (!due.length) {
    setPanelNote('Every slot cell here has been watched already. Replay clears that.');
    return;
  }

  setPanelNote('Spinning ' + due.length + ' cell' + (due.length === 1 ? '' : 's') + '…');
  await Promise.all(due.map((c, i) => spinCell(c, i)));
  setPanelNote('Landed — ' + due.length + ' cell' + (due.length === 1 ? '' : 's') + ' decided by the house.');
}

function clearSeen() {
  try {
    for (const k of Object.keys(localStorage)) {
      if (k.indexOf(SLOT_SEEN_PREFIX) === 0) localStorage.removeItem(k);
    }
  } catch {}
}

function setPanelNote(text) {
  const n = slotPanel && slotPanel.querySelector('.slot-panel-note');
  if (n) n.textContent = text;
}

function initSlotMock() {
  mountSlotPanel();
  // The board is on screen already, but logos load lazily and a cell whose image
  // has no measurable height yields a zero-height reel. A frame plus a beat is
  // enough, and the panel says so if there turns out to be nothing to spin.
  requestAnimationFrame(() => setTimeout(runSlots, 350));
}

function mountSlotPanel() {
  const p = document.createElement('div');
  p.className = 'slot-panel';
  p.innerHTML =
    '<div class="slot-panel-title">Let the slots decide <span class="slot-panel-tag">mock</span></div>' +
    '<div class="slot-panel-row">' +
      '<button type="button" class="slot-btn" data-act="replay">Replay</button>' +
      '<label class="slot-panel-count">cells' +
        '<input type="number" min="1" max="8" value="' + slotCount + '" class="slot-input" data-act="count">' +
      '</label>' +
    '</div>' +
    '<div class="slot-panel-note">&nbsp;</div>' +
    '<div class="slot-panel-foot">Nothing is written. Cells are picked at random from the board.</div>';
  document.body.appendChild(p);
  slotPanel = p;

  p.querySelector('[data-act="replay"]').addEventListener('click', () => {
    clearSeen();
    // Put any half-finished cell back before starting again.
    document.querySelectorAll('.slot-box').forEach(b => b.remove());
    runSlots({ force: true });
  });
  p.querySelector('[data-act="count"]').addEventListener('change', e => {
    slotCount = Math.max(1, Math.min(8, Number(e.target.value) || 1));
  });
}

// ======================================================================
// THE GATE. Last line on purpose — see the note beside the import.
// ======================================================================
const { isCommissioner } = await sessionInfo();
if (isCommissioner) enableSandbox();
