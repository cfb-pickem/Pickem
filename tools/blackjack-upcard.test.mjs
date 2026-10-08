// tools/blackjack-upcard.test.mjs — you must not see the dealer's card before
// you decide whether to bet.
//
//   node tools/blackjack-upcard.test.mjs
//
// WHY THIS EXISTS. The pit shipped with bj_open_week() writing the up-card into
// the world-readable blackjack_weeks every Tuesday, so the league spent five
// days looking at the dealer's up-card and only then choosing whether to sit
// down. That is backwards: in blackjack the wager goes down first and the cards
// come out second.
//
// It was not a cosmetic slip. A dealer showing a 6 busts about four times in
// ten and a dealer showing an ace is the worst spot in the game, so picking
// your weeks off the up-card is a large edge - and since there is no ante,
// sitting out is free, which makes "only play the weak up-cards" not just
// strong but OPTIMAL. The league's own commissioner spotted it from the felt:
// "people shouldnt get to decide to play only after seeing the dealers card".
//
// The fix has a server half and a browser half, and this pins the browser one:
//   * the felt must never SELECT dealer_up from blackjack_weeks (the column is
//     retired and permanently null, but a query that still asks for it is a
//     query that would start leaking again the moment anyone refilled it)
//   * with no hand, mid-week: TWO face-down dealer cards and no card face up
//   * with a hand, mid-week: the up-card, from bj_dealer_up(), plus one down
//   * settled: the whole dealer hand, because the week is over and there is
//     nothing left to protect
//
// The server half - bj_dealer_up() returning null to anyone without a hand -
// is enforced in SQL and verified against the live database; it cannot be
// tested from here, because this stub is what stands in for the server.

import { installDom } from './harness/dom.mjs';
import { SCRIPT } from './harness/stub-data.mjs';
import { readFileSync, copyFileSync, mkdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const repo = join(here, '..');
const live = join(repo, 'js', 'blackjack.js');

// Copied fresh every run, so this can never pass against a stale duplicate.
mkdirSync(join(here, 'harness', 'js'), { recursive: true });
copyFileSync(live, join(here, 'harness', 'js', 'blackjack.js'));

installDom();
const bj = await import('./harness/js/blackjack.js');

let fail = 0, ran = 0;
const ok = (c, what) => { ran++; if (!c) { fail++; console.error('  FAIL: ' + what); } };

// Cards are 0..51; rank = card % 13 (0 = ace, 9..12 = ten..king), suit = /13.
const SIX_CLUBS = 44;        // the dealer's worst card, and the one that made this bug matter
const KING = 12, NINE = 8, TEN = 9, TWO = 1;

const WEEK_OPEN   = { cfb_season: 2026, week: 6, dealer_final: null, settled_at: null };
const WEEK_DONE   = { cfb_season: 2026, week: 6, dealer_final: [SIX_CLUBS, TEN, TWO], settled_at: '2026-10-10T17:00:00Z' };
const MY_HAND     = { team_id: 7, bet: 100, doubled: false, cards: [KING, NINE], state: 'live', outcome: null, payout: null };

function setup({ week, hand, upcard, team = 7 }) {
  SCRIPT.calls.length = 0;
  SCRIPT.rpc = {
    bj_live_week:   { data: week.settled_at ? [] : [{ cfb_season: week.cfb_season, week: week.week }], error: null },
    bj_my_team:     team == null ? { data: null, error: { message: 'permission denied' } } : { data: team, error: null },
    bj_settle_week: { data: 0, error: null },
    // The server's gate, modelled: the card comes back only when the caller
    // holds a hand. A test that stubbed this unconditionally would prove
    // nothing about the behaviour being pinned.
    bj_dealer_up:   { data: hand ? upcard : null, error: null }
  };
  SCRIPT.tables = {
    blackjack_weeks: [week],
    blackjack_hands: hand ? [hand] : [],
    blackjack_bankrolls: team == null ? [] : [{ team_id: team, cfb_season: week.cfb_season, chips: 1000, hands_played: 0 }],
    teams: [{ team_id: 7, team_name: 'Charlie' }]
  };
}

async function paint(cfg) {
  setup(cfg);
  const mount = document.createElement('div');
  await bj.default(mount);
  return mount.innerHTML;
}

// A face-down card carries BOTH classes ("bj-card bj-card-down"), so counting
// face-ups by the bj-card class alone counts the hidden ones too - which is a
// test that passes while proving nothing. Total minus hidden is the honest sum.
const allCards  = html => (html.match(/class="bj-card\b/g) || []).length;
const faceDowns = html => (html.match(/bj-card-down/g) || []).length;
const faceUps   = html => allCards(html) - faceDowns(html);

console.log('=== the felt never asks for the retired column ===');
const src = readFileSync(live, 'utf8');
ok(!/select\([^)]*dealer_up/.test(src),
   'loadPit() does not SELECT dealer_up from blackjack_weeks');
ok(/bj_dealer_up/.test(src),
   'the up-card is fetched through bj_dealer_up() instead');

console.log('\n=== mid-week, no bet placed: nothing is showing ===');
{
  const html = await paint({ week: WEEK_OPEN, hand: null, upcard: SIX_CLUBS });
  ok(faceDowns(html) === 2, 'both dealer cards are face down (got ' + faceDowns(html) + ')');
  ok(faceUps(html) === 0, 'no card is face up at all (got ' + faceUps(html) + ')');
  ok(html.indexOf('6♣') < 0, 'the 6 of clubs does not appear anywhere in the markup');
  ok(/sealed until you bet/.test(html), 'the felt says why both cards are down');
  ok(/id="bj-bet"/.test(html), 'a bet box is still offered - you can play, just blind');
}

console.log('\n=== mid-week, bet placed: your up-card, and only yours ===');
{
  const html = await paint({ week: WEEK_OPEN, hand: MY_HAND, upcard: SIX_CLUBS });
  ok(html.indexOf('6♣') >= 0, 'the up-card is revealed once you hold a hand');
  ok(faceDowns(html) === 1, 'the hole card is still down (got ' + faceDowns(html) + ')');
  ok(/hole card sealed/.test(html), 'the label narrows to the hole card alone');
  ok(/data-bj="hit"/.test(html) && /data-bj="stand"/.test(html),
     'hit and stand are offered against the visible up-card');
}

console.log('\n=== a signed-out visitor sees no more than a non-bettor ===');
{
  const html = await paint({ week: WEEK_OPEN, hand: null, upcard: SIX_CLUBS, team: null });
  ok(faceDowns(html) === 2, 'both cards face down for anon too');
  ok(html.indexOf('6♣') < 0, 'no up-card leaks to anon');
  ok(SCRIPT.calls.indexOf('bj_dealer_up') < 0,
     'and the felt does not even ask - no pointless 401 on every anonymous view');
}

console.log('\n=== settled: the whole hand, because the week is over ===');
{
  const html = await paint({ week: WEEK_DONE, hand: null, upcard: SIX_CLUBS });
  ok(html.indexOf('6♣') >= 0, 'the up-card is public at last, via dealer_final');
  ok(faceDowns(html) === 0, 'nothing is still hidden (got ' + faceDowns(html) + ')');
  ok(faceUps(html) >= 3, 'the dealer’s full draw is shown (got ' + faceUps(html) + ')');
}

console.log('\n' + (fail ? fail + ' of ' + ran + ' checks FAILED' : 'blackjack-upcard: all ' + ran + ' assertions passed'));
process.exit(fail ? 1 : 0);
