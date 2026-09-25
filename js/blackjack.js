// js/blackjack.js — the league's blackjack table.
//
// One shared dealer hand a week. The whole league plays it, the chips settle
// nothing until two players finish level on points, and no hand has ever been
// worth a pick'em point. That last part is the entire reason this feature was
// allowed to exist: the slots are fun because they barely pay, and this pays
// less than that.
//
// NOTHING HERE DECIDES ANYTHING. Cards are dealt, drawn and settled by
// security-definer functions in the 20260924120000 migration, because this repo
// is public and the key is anon - a table the browser could deal itself is a
// table anybody can deal a 21 at. The arithmetic below is duplicated from SQL
// deliberately and narrowly, the same way jackpotOdds() mirrors
// slots_jackpot_odds(): the felt needs a total on screen long before it would be
// worth a round trip to ask Postgres to add up two cards. tools/blackjack.test.mjs
// holds the vectors that keep the two honest, and the migration checks the same
// ones.

// A card is 0..51. Rank is card % 13, suit is card / 13. Drawing uniformly from
// that range gives the correct four-in-thirteen chance of a ten-value card,
// which is why there is no shoe anywhere in this feature.
export const RANKS = ['A', '2', '3', '4', '5', '6', '7', '8', '9', '10', 'J', 'Q', 'K'];
export const SUITS = ['♠', '♥', '♦', '♣'];

export function cardRank(card) { return ((card % 13) + 13) % 13; }
export function cardSuit(card) { return Math.floor(card / 13) % 4; }

export function cardValue(card) {
  const r = cardRank(card);
  if (r === 0) return 11;      // an ace, demoted to 1 by handTotal when it has to be
  if (r >= 9) return 10;       // 10, J, Q, K
  return r + 1;
}

export function cardLabel(card) { return RANKS[cardRank(card)] + SUITS[cardSuit(card)]; }

// Aces at eleven, demoted one at a time only while the hand is over 21. Written
// as a loop rather than cleverness because the SQL version is the same loop and
// two clever implementations of the same rule drift.
function count(cards) {
  let raw = 0, aces = 0;
  for (const c of cards) { const v = cardValue(c); raw += v; if (v === 11) aces++; }
  while (raw > 21 && aces > 0) { raw -= 10; aces--; }
  return { total: raw, aces };
}

export function handTotal(cards) { return count(cards).total; }
export function isSoft(cards) { return count(cards).aces > 0; }
export function isBust(cards) { return handTotal(cards) > 21; }

// TWO cards to 21, not three. A hand that gets there the hard way is a 21 and
// pays even money, which is the rule everybody already knows.
export function isNatural(cards) { return cards.length === 2 && handTotal(cards) === 21; }

export const TABLE = { start: 1000, min: 25, max: 500 };

// YOUR LAST CHIPS ARE ALWAYS A LEGAL BET. Without this a 25 minimum and no
// reload means a player stranded on 18 is out while still holding chips, which
// is a fiddly way to die. You are out at exactly zero and nowhere else.
export function legalBets(chips) {
  if (!(chips > 0)) return null;
  return { min: Math.min(TABLE.min, chips), max: Math.min(TABLE.max, chips) };
}

export function betIsLegal(bet, chips) {
  const r = legalBets(chips);
  return !!r && Number.isInteger(bet) && bet >= r.min && bet <= r.max;
}

// Draw to 17 and stand on all of them, soft included. A natural never draws,
// which matters: a dealer holding one has already ended the week.
export function dealerPlay(upAndHole, draw) {
  const hand = [...upAndHole];
  if (isNatural(hand)) return hand;
  while (handTotal(hand) < 17) hand.push(draw());
  return hand;
}

export function settleHand({ cards, bet, doubled }, dealerCards) {
  const stake = bet * (doubled ? 2 : 1);

  // THE PEEK, APPLIED BACKWARDS. A real dealer showing an ace or a ten looks at
  // the hole card and ends the hand on a natural, so nobody can double into a
  // hand that was already over. A hole card sealed from Tuesday to kickoff
  // cannot peek, so the peek is honoured at settlement instead: the week's play
  // is void and only the original bet was ever at risk. US "original bets only".
  if (isNatural(dealerCards)) {
    return isNatural(cards)
      ? { outcome: 'push', payout: 0 }
      : { outcome: 'lose', payout: -bet };
  }

  if (isNatural(cards)) return { outcome: 'blackjack', payout: Math.floor(bet * 3 / 2) };

  const p = handTotal(cards), d = handTotal(dealerCards);
  if (p > 21) return { outcome: 'lose', payout: -stake };
  if (d > 21 || p > d) return { outcome: 'win', payout: stake };
  if (p === d) return { outcome: 'push', payout: 0 };
  return { outcome: 'lose', payout: -stake };
}
