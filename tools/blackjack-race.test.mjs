// tools/blackjack-race.test.mjs — the pit's felt obeys the F2 generation
// token; the reveal overlay it triggers has to as well.
//
//   node tools/blackjack-race.test.mjs
//
// WHY THIS EXISTS. A code review found, and reproduced by execution, that
// initPit(...).then(revealHand) called revealHand() unconditionally on
// fulfillment - a stale generation still resolves the promise (just with
// nothing, since a bare `return` resolves `undefined`), and .then(fn) invokes
// fn on any fulfillment regardless of the resolved value. revealHand() can't
// rescue this by re-checking gen itself: it reads `const myGen = gen` fresh
// at the moment it's *called*, which is already the post-navigation value, so
// its own guard trivially passes. The felt obeyed the token (#pit stayed
// empty); the reveal overlay it appends to document.body did not. A player
// who opened Tiebreakers and then clicked away to the weekly leaderboard
// before the RPCs resolved would see a card table drop over the wrong board -
// exactly the distraction the reveal's placement exists to prevent.
//
// The fix has two halves, and this test pins both:
//   1. initPit() resolves `true` only if it painted for the generation it
//      started with, `false` for every early bail-out - a real boolean, not
//      an implicit `undefined`, so a caller has something to check.
//   2. The caller must actually check it: `.then(ok => { if (ok) revealHand() })`,
//      never a bare `.then(revealHand)`.
//
// This file exercises the module exactly the way index.html now does (half
// 2), against the real js/blackjack.js (copied fresh into harness/js/ on
// every run, so this can never drift onto a stale copy), through the stub
// Supabase client in harness/js/supabaseClient.js (half 1's contract is what
// that stub's script drives). A regression in either half turns this red.
import { copyFileSync, mkdirSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { installDom } from './harness/dom.mjs';
import { SCRIPT } from './harness/stub-data.mjs';

const here = dirname(fileURLToPath(import.meta.url));
mkdirSync(join(here, 'harness', 'js'), { recursive: true });
copyFileSync(join(here, '..', 'js', 'blackjack.js'), join(here, 'harness', 'js', 'blackjack.js'));

installDom();
const appended = [];
document.body.appendChild = function (c) { appended.push(c); return c; };
const isStage = e => String(e.className || '').indexOf('bj-stage') >= 0;

const bj = await import('./harness/js/blackjack.js');

let failed = 0;
const ok = (cond, what) => { if (!cond) { failed++; console.error('FAIL: ' + what); } };
const eq = (got, want, what) => ok(got === want, `${what} — got ${JSON.stringify(got)}, want ${JSON.stringify(want)}`);

const WEEK = { cfb_season: 2026, week: 4, dealer_up: 44, dealer_final: [44, 9, 1], settled_at: '2026-09-26T17:00:00Z' };
const HAND = { team_id: 7, bet: 100, doubled: false, cards: [12, 8], state: 'settled', outcome: 'win', payout: 100 };
SCRIPT.rpc = {
  bj_live_week: { data: [{ cfb_season: 2026, week: 4 }], error: null },
  bj_my_team: { data: 7, error: null },
  bj_settle_week: { data: 0, error: null }
};
SCRIPT.tables = {
  blackjack_weeks: [WEEK],
  blackjack_hands: [HAND],
  blackjack_bankrolls: [{ team_id: 7, cfb_season: 2026, chips: 1100, hands_played: 1 }]
};
globalThis.__REDUCED = true;   // the sequencing under test isn't the point; skip the draw-by-draw wait

// --- visit 1: open Tiebreakers, let it run to completion, exactly as the
// production wiring does: `initPit(...).then(ok => { if (ok) revealHand(); })`.
const mount1 = document.createElement('div');
const ok1 = await bj.default(mount1);
eq(ok1, true, 'initPit() resolves true when it actually paints for a live generation');
if (ok1) await bj.revealHand();
eq(appended.filter(isStage).length, 1, 'a completed load with a settled hand shows the reveal once');

// Mark-seen is per (season, week); clear it to model a different player/week
// rather than exercising the once-per-week rule, which is not what this test
// is about.
bj.forgetHands();
appended.length = 0;

// --- visit 2: open Tiebreakers again, then navigate away (resetPit(), same
// as loadWeek()/loadPlayoffWeek()/loadTiebreakerWeek() call on teardown)
// before the in-flight RPCs resolve. This is the reproduction.
const mount2 = document.createElement('div');
let ok2;
const chain = bj.default(mount2).then(o => { ok2 = o; if (o) return bj.revealHand(); });
bj.resetPit();          // the user clicks the weekly leaderboard mid-flight
await chain;

eq(ok2, false, 'initPit() resolves false for the generation that was torn down mid-flight');
eq(mount2.innerHTML, '', 'the torn-down pit mount is never repainted');
eq(appended.filter(isStage).length, 0,
   'no reveal overlay is appended for a load the caller tore down before it finished');

if (failed) { console.error(`\n${failed} assertion(s) failed`); process.exit(1); }
console.log('blackjack-race: all assertions passed');
