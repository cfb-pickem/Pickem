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
  // Empty on purpose. The slot-machine reveal was prototyped here and has since
  // shipped for real in js/slots.js; it was deleted rather than left behind, so
  // there is one implementation instead of two that can disagree.
  // --------------------------------------------------------------------
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

// ======================================================================
// THE GATE. Last line on purpose — see the note beside the import.
// ======================================================================
const { isCommissioner } = await sessionInfo();
if (isCommissioner) enableSandbox();
