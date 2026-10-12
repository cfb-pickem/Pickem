// tools/blackjack-open.test.mjs — the table opens with the picks, so the felt
// must draw the right week when more than one has been dealt.
//
//   node tools/blackjack-open.test.mjs
//
// WHY THIS EXISTS. Until 20261012130000 a week's table was dealt at Tuesday's
// line lock, by which point the previous week had always settled, so "the
// newest blackjack_weeks row" and "the week worth showing" were the same row.
// Now the table opens the minute a week's games are ticked `picked`, and two
// unsettled rows can exist side by side:
//   * a commissioner sets week N+1 before week N kicks off - both are dealt,
//     and bj_live_week() (the earliest open week) still names N;
//   * a box ticked in the wrong week and unticked - that week was dealt, and
//     the settler now leaves it sealed rather than flipping it, because it
//     never kicked off.
// When the live lookup misses (the cron minute before a week is dealt, or the
// gap between a kickoff and the next week's picks), the felt falls back. The
// newest row may be one of those dealt-but-not-live weeks, and drawing it
// offers a bet box bj_deal() refuses. The fallback has to be the newest
// SETTLED week.
//
// The server half - which weeks get dealt, and that a week settles only once it
// has kicked off - is SQL, and is pinned by tools/blackjack-open.check.sql
// against the real database inside a transaction that always rolls back.

import { installDom } from './harness/dom.mjs';
import { SCRIPT } from './harness/stub-data.mjs';
import { copyFileSync, mkdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
mkdirSync(join(here, 'harness', 'js'), { recursive: true });
copyFileSync(join(here, '..', 'js', 'blackjack.js'), join(here, 'harness', 'js', 'blackjack.js'));

installDom();
const bj = await import('./harness/js/blackjack.js');

let fail = 0, ran = 0;
const ok = (c, what) => { ran++; if (!c) { fail++; console.error('  FAIL: ' + what); } };

// A just-big-enough PostgREST: eq, not-is-null, order and limit, applied the
// way the real one would. Anything else is ignored. The stub hands the query's
// recorded filters to a table scripted as a function.
const lastQueries = [];
function table(rows) {
  return f => {
    lastQueries.push(f);
    let out = [...rows];
    const orders = [];
    let lim = Infinity;
    for (const [m, ...a] of f) {
      if (m === 'eq') out = out.filter(r => r[a[0]] === a[1]);
      if (m === 'not' && a[1] === 'is' && a[2] === null) out = out.filter(r => r[a[0]] != null);
      if (m === 'order') orders.push([a[0], a[1]?.ascending !== false]);
      if (m === 'limit') lim = a[0];
    }
    out.sort((x, y) => {
      for (const [k, asc] of orders) { if (x[k] !== y[k]) return (x[k] < y[k] ? -1 : 1) * (asc ? 1 : -1); }
      return 0;
    });
    return out.slice(0, lim);
  };
}

const WK6_SETTLED = { cfb_season: 2026, week: 6, dealer_final: [46, 42, 28, 1], settled_at: '2026-10-10T16:00:00Z' };
const WK7_OPEN    = { cfb_season: 2026, week: 7, dealer_final: null, settled_at: null };
const WK8_EARLY   = { cfb_season: 2026, week: 8, dealer_final: null, settled_at: null };
const WK9_ORPHAN  = { cfb_season: 2026, week: 9, dealer_final: null, settled_at: null };

function setup({ live, weeks }) {
  SCRIPT.calls.length = 0;
  lastQueries.length = 0;
  SCRIPT.rpc = {
    bj_live_week:   { data: live ? [live] : [], error: null },
    bj_my_team:     { data: 7, error: null },
    bj_settle_week: { data: 0, error: null },
    bj_dealer_up:   { data: null, error: null }
  };
  SCRIPT.tables = {
    blackjack_weeks: table(weeks),
    blackjack_hands: [],
    blackjack_bankrolls: [{ team_id: 7, cfb_season: 2026, chips: 1000, hands_played: 0 }],
    teams: [{ team_id: 7, team_name: 'Charlie' }]
  };
}

async function paint(cfg) {
  setup(cfg);
  const mount = document.createElement('div');
  await bj.default(mount);
  return { html: mount.innerHTML, state: bj.pitState() };
}

console.log('=== the open week is the live one, even with the next week already dealt ===');
{
  const { html, state } = await paint({
    live: { cfb_season: 2026, week: 7 },
    weeks: [WK6_SETTLED, WK7_OPEN, WK8_EARLY]
  });
  ok(state.week === 7, 'the felt draws week 7, the earliest open week (got ' + state.week + ')');
  ok(/THE PIT · WEEK 7/.test(html), 'the header says week 7');
  ok(/id="bj-bet"/.test(html), 'and offers the bet box for it');
}

console.log('\n=== the cron minute before the live week is dealt: last settled week, not a later dealt one ===');
{
  const { html, state } = await paint({
    live: { cfb_season: 2026, week: 7 },
    weeks: [WK6_SETTLED, WK8_EARLY]
  });
  ok(state.week === 6, 'falls back to week 6, the newest SETTLED week (got ' + state.week + ')');
  ok(state.settled === true, 'and draws it as settled');
  ok(!/id="bj-bet"/.test(html), 'no bet box is offered for a week bj_deal() would not seat');
  ok(!/WEEK 8/.test(html), 'week 8, dealt early but not live, is not drawn');
}

console.log('\n=== no live week at all, with a mistakenly dealt week left sealed ===');
{
  const { html, state } = await paint({ live: null, weeks: [WK6_SETTLED, WK9_ORPHAN] });
  ok(state.week === 6, 'falls back to week 6, not the sealed week 9 (got ' + state.week + ')');
  ok(!/id="bj-bet"/.test(html), 'no bet box for a table that is not open');
  ok(/You sat this week out/.test(html), 'the settled week reads as settled');
}

console.log('\n=== the fallback query itself asks only for settled weeks ===');
{
  await paint({ live: null, weeks: [WK6_SETTLED] });
  const fallback = lastQueries.find(f => f.some(([m]) => m === 'limit'));
  ok(!!fallback, 'a fallback query was made');
  ok(!!fallback && fallback.some(([m, k, op, v]) => m === 'not' && k === 'settled_at' && op === 'is' && v === null),
     'it filters on settled_at is not null');
}

console.log('\n' + (fail ? fail + ' of ' + ran + ' checks FAILED' : 'blackjack-open: all ' + ran + ' assertions passed'));
process.exit(fail ? 1 : 0);
