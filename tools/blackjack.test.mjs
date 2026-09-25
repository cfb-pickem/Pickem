// tools/blackjack.test.mjs — the hand arithmetic, and nothing that needs a browser.
//
//   node tools/blackjack.test.mjs
//
// These vectors are duplicated verbatim in the 20260924120000 migration's own
// checks. That is the point: the felt draws a total in JS and Postgres pays out
// on a total in SQL, and the day those two disagree somebody is paid wrongly.

import {
  cardRank, cardSuit, cardValue, cardLabel,
  handTotal, isSoft, isNatural, isBust,
  TABLE, legalBets, betIsLegal, settleHand, dealerPlay
} from '../js/blackjack.js';

let failed = 0;
const ok = (cond, what) => {
  if (cond) return;
  failed++;
  console.error('FAIL: ' + what);
};
const eq = (got, want, what) =>
  ok(got === want, `${what} — got ${JSON.stringify(got)}, want ${JSON.stringify(want)}`);

// Cards. Ace of spades is 0; king of clubs is 51.
const A = 0, TWO = 1, NINE = 8, TEN = 9, JACK = 10, KING = 12;
eq(cardRank(A), 0, 'rank of ace');
eq(cardRank(KING), 12, 'rank of king');
eq(cardRank(13), 0, 'rank wraps into the next suit');
eq(cardSuit(0), 0, 'first suit');
eq(cardSuit(51), 3, 'last suit');
eq(cardValue(A), 11, 'ace is 11 before demotion');
eq(cardValue(TWO), 2, 'two is 2');
eq(cardValue(NINE), 9, 'nine is 9');
eq(cardValue(TEN), 10, 'ten is 10');
eq(cardValue(JACK), 10, 'jack is 10');
eq(cardValue(KING), 10, 'king is 10');
eq(cardLabel(A), 'A♠', 'ace of spades reads back');
eq(cardLabel(51), 'K♣', 'king of clubs reads back');

// Totals, which is where aces earn their keep.
eq(handTotal([A, KING]), 21, '[A,K] is 21');
eq(handTotal([A, A, NINE]), 21, '[A,A,9] is 21');
eq(handTotal([A, A, A, 7]), 21, '[A,A,A,8] is 21');
eq(handTotal([A, NINE, 4]), 15, '[A,9,5] is 15');
eq(handTotal([KING, 4, 5]), 21, '[K,5,6] is 21');
eq(handTotal([KING, KING, TWO]), 22, 'a real bust stays over 21');
eq(handTotal([]), 0, 'an empty hand is 0');
ok(isSoft([A, 5]), '[A,6] is soft');
ok(!isSoft([A, KING, TWO]), '[A,K,2] is hard 13');
ok(isNatural([A, JACK]), '[A,J] is a natural');
ok(!isNatural([TEN, NINE, TWO]), 'three cards to 21 is not a natural');
ok(isBust([KING, KING, TWO]), '22 busts');
ok(!isBust([A, KING]), '21 does not bust');

// Bet limits, including the rule that your last chips are always playable.
eq(TABLE.start, 1000, 'starting bankroll');
eq(legalBets(1000).min, 25, 'a full stack plays the table minimum');
eq(legalBets(1000).max, 500, 'a full stack plays the table maximum');
eq(legalBets(18).min, 18, 'a short stack may bet exactly what it holds');
eq(legalBets(18).max, 18, 'and no more');
eq(legalBets(0), null, 'a busted player has no legal bet');
ok(!betIsLegal(24, 1000), '24 is under the minimum');
ok(betIsLegal(25, 1000), '25 is the minimum');
ok(betIsLegal(500, 1000), '500 is the maximum');
ok(!betIsLegal(501, 1000), '501 is over the maximum');
ok(!betIsLegal(200, 150), 'you cannot bet more than you hold');
ok(!betIsLegal(25.5, 1000), 'chips are integers');

// Dealer play: draw to 17, stand on all 17s, never draw on a natural.
eq(dealerPlay([TEN, 2], () => TWO).length, 4, 'dealer draws 13 -> 17 in two twos');
eq(handTotal(dealerPlay([TEN, 2], () => TWO)), 17, 'and stops the moment it reaches 17');
eq(dealerPlay([A, 5], () => { throw new Error('drew on soft 17'); }).length, 2,
   'dealer stands on soft 17');
eq(dealerPlay([A, KING], () => { throw new Error('drew on a natural'); }).length, 2,
   'dealer never draws on a natural');

// Settlement.
const hand = (cards, bet = 100, doubled = false) => ({ cards, bet, doubled });
eq(settleHand(hand([KING, NINE]), [KING, 6]).payout, 100, '19 beats 17');
eq(settleHand(hand([KING, NINE]), [KING, NINE]).outcome, 'push', 'equal totals push');
eq(settleHand(hand([KING, 5]), [KING, NINE]).payout, -100, '16 loses to 19');
eq(settleHand(hand([KING, KING, TWO]), [KING, 5]).payout, -100,
   'a bust loses even though the dealer sat on 16');
eq(settleHand(hand([KING, NINE]), [KING, 5, NINE]).payout, 100, 'a dealer bust pays');
eq(settleHand(hand([A, KING]), [KING, NINE]).outcome, 'blackjack', 'a natural is its own outcome');
eq(settleHand(hand([A, KING]), [KING, NINE]).payout, 150, 'and pays 3:2');
eq(settleHand(hand([A, KING], 25), [KING, NINE]).payout, 37, '3:2 on 25 rounds down to 37');
eq(settleHand(hand([KING, NINE], 100, true), [KING, 7]).payout, 200, 'a doubled win pays double');
eq(settleHand(hand([KING, 5], 100, true), [KING, NINE]).payout, -200, 'a doubled loss costs double');

// The peek, applied backwards. A dealer natural voids the week's play.
const dealerNat = [A, KING];
eq(settleHand(hand([KING, NINE]), dealerNat).payout, -100,
   'a dealer natural takes the original bet');
eq(settleHand(hand([KING, NINE], 100, true), dealerNat).payout, -100,
   'and refunds the double — original bets only');
eq(settleHand(hand([KING, KING, TWO], 100, true), dealerNat).payout, -100,
   'a bust against a dealer natural also loses the original only');
eq(settleHand(hand([A, JACK]), dealerNat).outcome, 'push',
   'natural against natural is a push');
eq(settleHand(hand([A, JACK]), dealerNat).payout, 0, 'and costs nothing');

if (failed) { console.error(`\n${failed} assertion(s) failed`); process.exit(1); }
console.log('blackjack: all assertions passed');
