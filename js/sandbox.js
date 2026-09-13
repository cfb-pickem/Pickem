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
import { runSlots, setRevealSpeed, forgetSpins } from './slots.js';
import { markSlotCell } from './utils.js';

// KEEP THE GATE AT THE BOTTOM OF THIS FILE. Function declarations hoist but
// `const` and `let` do not, so calling enableSandbox() from up here would reach
// any constant an experiment declares below while it is still in the temporal
// dead zone, and throw before the page renders. It has happened once already.

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
  //
  // The reveal itself is NOT prototyped here - it shipped in js/slots.js and the
  // lab below imports it rather than keeping a second copy that can disagree.
  // --------------------------------------------------------------------

  initSlotLab();
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
 * THE SLOT LAB — sandbox only
 * ======================================================================
 *
 * A bench for the thing nobody can watch on demand: what the reveal looks like
 * at kickoff. It only happens once per person per game, on a Saturday, for a
 * player who slotted a game days earlier — so without this the only way to see
 * it is to wait for one and not blink.
 *
 * IT DRIVES THE REAL REVEAL. runSlots() is imported from js/slots.js and called
 * as the leaderboard calls it, against cells tagged exactly as renderTable tags
 * them. Nothing here reimplements the spin, so what you are looking at is the
 * shipped animation rather than a second copy of it that can quietly disagree.
 *
 * What the lab adds is only the things production cannot give you on a Tuesday:
 * cells to spin (there are no real slot picks yet), a way to watch it again,
 * and a speed control — which works by setting playbackRate on the animations
 * that are already running, so the timing you judge is the real curve played
 * faster or slower rather than a different animation.
 *
 * READ ONLY, like the rest of the sandbox. It tags cells in the DOM and writes
 * the same localStorage keys the reveal already writes. It touches no database.
 */


const LAB_SEEN_PREFIX = 'cfb-slots-seen';

let labPanel = null;
let labCells = 3;
let labSpeed = 1;
let labPop = false;
// The crest pulled out of a forced-pop cell, so Clear can put it back. A popped
// cell has no team in it, and the lab has to be able to undo that completely -
// a scratch area that cannot restore the board is not a scratch area.
const labStashedCrest = new WeakMap();

function labNote(text) {
  const n = labPanel && labPanel.querySelector('.slot-panel-note');
  if (n) n.textContent = text;
}

/** Every cell on the board that is showing a pick, so it has something to land on. */
function labCandidates() {
  const out = [];
  document.querySelectorAll('tbody tr').forEach(tr => {
    if (tr.classList.contains('playoff-divider')) return;
    [...tr.children].forEach((td, col) => {
      if (col < 2) return;
      if (!td.querySelector('img.logo')) return;
      out.push({ td, col, row: tr });
    });
  });
  return out;
}

/**
 * Forget every reveal this page has shown, so they can all run again.
 *
 * The reveal now replays on each page LOAD and suppresses itself within one, so
 * the thing to clear is that set rather than localStorage. The old keys are
 * swept too: a browser that watched a reveal under the previous rule should not
 * carry that around forever.
 */
function labForget() {
  forgetSpins();
  try {
    for (const k of Object.keys(localStorage)) {
      if (k.indexOf(LAB_SEEN_PREFIX) === 0) localStorage.removeItem(k);
    }
  } catch {}
}

/** Take the board back to how it looked before the lab touched it. */
function labReset() {
  document.querySelectorAll('.slot-box').forEach(b => b.remove());
  document.querySelectorAll('[data-slots]').forEach(td => {
    delete td.dataset.slots;
    delete td.dataset.slotsTeam;
    td.classList.remove('slot-cell', 'slot-landed');
  });
  document.querySelectorAll('[data-jackpot]').forEach(td => {
    td.querySelectorAll('.auto-win').forEach(t => t.remove());
    delete td.dataset.jackpot;
    const crest = labStashedCrest.get(td);
    if (crest) { td.appendChild(crest); labStashedCrest.delete(td); }
  });
  document.querySelectorAll('[data-slot-marked]').forEach(td => {
    td.querySelectorAll('.slot-was').forEach(m => m.remove());
    delete td.dataset.slotMarked;
    td.classList.remove('slot-was-cell');
  });
}

/**
 * THE MARK. After the reels stop the cell goes back to being an ordinary logo,
 * and a week later nobody can tell that pick was the house's rather than the
 * player's. That is the gap this is filling: the spin is a moment, and the mark
 * is what is left of it on the board afterwards.
 *
 * It is the edge of a poker chip: red, black, white and blue spots running
 * round the cell the way they run round a chip. There is one of these and no
 * switch to change it — the alternates it was compared against are gone.
 *
 * NOT GOLD, which is the reason it looks the way it does. Gold is this site's
 * own accent, worn by the header rule and the buttons and the focus outline,
 * so a gold mark reads as one more piece of furniture rather than as a fact
 * about the pick.
 *
 * It is an inset overlay rather than a border on the cell itself, so it never
 * changes the column's box or costs the crest a pixel.
 */
function labMarkCell(td) {
  td.dataset.slotMarked = '1';
  markSlotCell(td);
}

async function labRun() {
  labReset();
  labForget();

  const all = labCandidates();
  if (!all.length) {
    labNote('No revealed picks on this board — choose an earlier week, then Roll.');
    return;
  }

  // One per row at most, so it reads as "these players used the slots" rather
  // than something happening to a whole column.
  const byRow = new Map();
  for (const c of all) if (!byRow.has(c.row)) byRow.set(c.row, c);
  const chosen = [...byRow.values()].sort(() => Math.random() - 0.5)
    .slice(0, Math.min(labCells, byRow.size));

  // Tagged exactly the way renderTable tags a real slot pick, because that is
  // the only thing js/slots.js reads.
  chosen.forEach((c, i) => {
    c.td.dataset.slots = String(9000 + i);
    c.td.dataset.slotsTeam = String(8000 + i);
  });

  // FORCING THE POP. A real jackpot lands about twice a season, so without this
  // the only way to see the thing the whole feature is built around is to wait
  // for one - which is exactly the problem the lab exists to solve.
  //
  // It builds the cell the way renderTable builds a popped one: the crest comes
  // out, because there is no team behind an AUTO WIN, and the tag goes in.
  if (labPop && chosen.length) {
    const c = chosen[0];
    c.td.dataset.jackpot = '1';
    const crest = c.td.querySelector('img.logo');
    if (crest) { labStashedCrest.set(c.td, crest); crest.remove(); }
    const tag = document.createElement('span');
    tag.className = 'auto-win';
    tag.textContent = 'AUTO WIN';
    c.td.appendChild(tag);
  }

  labNote('Rolling ' + chosen.length + ' cell' + (chosen.length === 1 ? '' : 's') + '…');

  // Start the real reveal, then bend the animations that are now running. The
  // spin keeps its own easing curve and only the rate changes, so a slow pass
  // is this animation slowed down rather than a different one.
  // The football's beats are timers rather than animations, so the slider has
  // to be handed to them directly - playbackRate only ever reached the reels.
  setRevealSpeed(labSpeed);
  const spinning = runSlots();
  if (labSpeed !== 1) {
    requestAnimationFrame(() => {
      document.getAnimations?.().forEach(a => {
        if (a.effect?.target?.classList?.contains('slot-strip')) a.playbackRate = labSpeed;
      });
    });
  }

  const n = await spinning;
  // Not the popped one: it says AUTO WIN in words, and a chip edge round it
  // would be the board explaining the same fact twice in two different codes.
  chosen.forEach(c => { if (c.td.dataset.jackpot !== '1') labMarkCell(c.td); });
  labNote('Landed. ' + n + ' cell' + (n === 1 ? '' : 's') + ' decided by the house.');
}

function mountLabPanel() {
  const p = document.createElement('div');
  p.className = 'slot-panel';
  p.innerHTML =
    '<div class="slot-panel-title">Slot lab <span class="slot-panel-tag">sandbox</span></div>' +
    '<div class="slot-panel-row">' +
      '<button type="button" class="slot-btn" data-act="roll">Roll it</button>' +
      '<button type="button" class="slot-btn" data-act="reset">Clear</button>' +
    '</div>' +
    '<label class="slot-panel-field">Cells' +
      '<input type="number" min="1" max="8" value="' + labCells + '" class="slot-input" data-act="cells">' +
    '</label>' +
    '<label class="slot-panel-field">Speed <span data-speed-out>1.0&times;</span>' +
      '<input type="range" min="25" max="200" step="5" value="100" class="slot-range" data-act="speed">' +
    '</label>' +
    '<label class="slot-panel-field slot-panel-check">' +
      '<input type="checkbox" class="slot-check" data-act="pop"> Force the football to pop' +
    '</label>' +
    '<div class="slot-panel-note">&nbsp;</div>' +
    '<div class="slot-panel-foot">Drives the real reveal in js/slots.js. Nothing is written to the database.</div>';
  document.body.appendChild(p);
  labPanel = p;

  p.querySelector('[data-act="roll"]').addEventListener('click', labRun);
  p.querySelector('[data-act="reset"]').addEventListener('click', () => {
    labReset(); labNote('Board back to normal.');
  });
  p.querySelector('[data-act="cells"]').addEventListener('change', e => {
    labCells = Math.max(1, Math.min(8, Number(e.target.value) || 1));
  });
  p.querySelector('[data-act="pop"]').addEventListener('change', e => {
    labPop = !!e.target.checked;
  });
  p.querySelector('[data-act="speed"]').addEventListener('input', e => {
    labSpeed = Number(e.target.value) / 100;
    p.querySelector('[data-speed-out]').innerHTML = labSpeed.toFixed(2) + '&times;';
  });
}

function initSlotLab() {
  mountLabPanel();
  labNote('Roll it to watch a kickoff reveal.');
}

// ======================================================================
// THE GATE. Last line on purpose — see the note beside the import.
// ======================================================================
const { isCommissioner } = await sessionInfo();
if (isCommissioner) enableSandbox();
