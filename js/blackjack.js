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

import { supabase } from './supabaseClient.js';

// --- the felt -------------------------------------------------------------
//
// One panel, two states worth code: no hand yet (place a bet) and a hand in
// progress or just settled (hit/stand/double, then a verdict). Every card
// shown here came from an RPC; nothing in this file decides a total or an
// outcome, only formats one that Postgres already computed.
//
// bj_my_team() is authenticated-only and errors outright for a signed-out
// visitor - there is no team to hand back. A visitor with no team gets a
// read-only felt: the dealer's up-card and its sealed hole card, no bet box,
// no buttons. Nothing else in this module distinguishes "signed out" from
// "signed in but errored" because the felt treats both the same way: show
// what is public, offer nothing that would 400.
let state = {
  season: null, week: null, hand: null, dealerUp: null, dealerFinal: null,
  chips: TABLE.start, settled: false, table: [], myTeam: null
};

// Surfaced in the felt rather than alert()'d, so a bad bet doesn't block the
// tab with a modal a phone can't easily dismiss. Cleared on every fresh load.
let lastError = null;

// One RPC in flight at a time. A double-click on DEAL is real (it is the
// example the spec calls out), and a click on STAND while HIT is still in
// flight would race two writes against the same hand; both are closed by
// disabling every action button for the duration of the request, not just
// the one that was clicked.
let busy = false;

export function pitState() { return { ...state }; }

const ESC = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
const esc = s => String(s).replace(/[&<>"']/g, c => ESC[c]);

const card = (c, faceDown = false) =>
  faceDown
    ? '<span class="bj-card bj-card-down" aria-label="face down"></span>'
    : '<span class="bj-card' + (cardSuit(c) === 1 || cardSuit(c) === 2 ? ' bj-red' : '') +
      '">' + cardLabel(c) + '</span>';

const handHtml = (cards, faceDownAfter = Infinity) =>
  cards.map((c, i) => card(c, i >= faceDownAfter)).join('');

async function myTeam() {
  try {
    const res = await supabase.rpc('bj_my_team');
    return res.error ? null : (res.data ?? null);
  } catch {
    return null;
  }
}

export async function loadPit() {
  const wk = await supabase.rpc('bj_live_week');
  const live = Array.isArray(wk.data) ? wk.data[0] : wk.data;

  // A settled week has no open table, so bj_live_week() returns nothing. Fall
  // back to the newest row so the felt shows last week's result rather than
  // going blank between Saturday and Tuesday.
  let season = live?.cfb_season ?? null, week = live?.week ?? null;
  let wrow = null;
  if (season == null) {
    const r = await supabase.from('blackjack_weeks')
      .select('cfb_season, week, dealer_up, dealer_final, settled_at')
      .order('cfb_season', { ascending: false }).order('week', { ascending: false }).limit(1);
    wrow = r.data?.[0] ?? null;
    season = wrow?.cfb_season ?? null; week = wrow?.week ?? null;
  } else {
    const r = await supabase.from('blackjack_weeks')
      .select('cfb_season, week, dealer_up, dealer_final, settled_at')
      .eq('cfb_season', season).eq('week', week).maybeSingle();
    wrow = r.data ?? null;
  }
  if (!wrow) { state = { ...state, season: null, week: null }; return state; }

  const hands = await supabase.from('blackjack_hands')
    .select('team_id, bet, doubled, cards, state, outcome, payout')
    .eq('cfb_season', season).eq('week', week);

  const me = await myTeam();
  // .eq('team_id', null) would ask Postgres "team_id = NULL", which is never
  // true, so a signed-out visitor skips the bankroll round trip entirely
  // rather than relying on that to come back empty.
  const bank = me == null
    ? { data: null }
    : await supabase.from('blackjack_bankrolls')
        .select('chips, hands_played').eq('cfb_season', season).eq('team_id', me).maybeSingle();

  state = {
    season, week,
    dealerUp: wrow.dealer_up,
    dealerFinal: wrow.dealer_final ?? null,
    settled: !!wrow.settled_at,
    chips: bank.data?.chips ?? TABLE.start,
    hand: (hands.data || []).find(h => h.team_id === me) ?? null,
    table: hands.data || [],
    myTeam: me
  };
  return state;
}

function setBusy(mount, v) {
  busy = v;
  mount.querySelectorAll('[data-bj]').forEach(b => { b.disabled = v; });
}

function renderPit(mount) {
  if (state.season == null) { mount.hidden = true; return; }
  mount.hidden = false;

  const s = state;
  const dealer = s.settled && s.dealerFinal
    ? handHtml(s.dealerFinal)
    : handHtml([s.dealerUp]) + card(0, true);
  const dealerTot = s.settled && s.dealerFinal ? handTotal(s.dealerFinal) : null;

  let body;
  if (s.myTeam == null) {
    // No RPC seats a signed-out visitor, and none should - bj_my_team() is
    // authenticated-only on purpose. This is the felt's whole story for them.
    body = '<p class="bj-note">Sign in with a team to play this week&rsquo;s hand.</p>';
  } else if (!s.hand) {
    const r = legalBets(s.chips);
    body = r
      ? '<div class="bj-row"><label class="bj-label" for="bj-bet">BET</label>' +
        '<input id="bj-bet" class="bj-bet" type="number" inputmode="numeric" ' +
        'min="' + r.min + '" max="' + r.max + '" step="1" value="' + r.min + '">' +
        '<button class="bj-btn" data-bj="deal">DEAL</button></div>' +
        '<p class="bj-note">Table ' + r.min + '–' + r.max + '. Blackjack pays 3:2.</p>'
      : '<p class="bj-note bj-out">You are out for the season. No reload.</p>';
  } else {
    const h = s.hand, tot = handTotal(h.cards);
    const canAct = h.state === 'live' && !s.settled;
    body =
      '<div class="bj-row"><span class="bj-who">YOU</span>' + handHtml(h.cards) +
      '<span class="bj-tot">' + tot + (isSoft(h.cards) ? ' soft' : '') + '</span></div>' +
      (canAct
        ? '<div class="bj-row">' +
          '<button class="bj-btn" data-bj="hit">HIT</button>' +
          '<button class="bj-btn" data-bj="stand">STAND</button>' +
          (h.cards.length === 2 && h.bet * 2 <= s.chips
            ? '<button class="bj-btn" data-bj="double">DOUBLE</button>' : '') +
          '</div>'
        : '<p class="bj-note">' + verdict(h) + '</p>');
  }

  mount.innerHTML =
    '<div class="bj-felt scoreboard-wrap" data-bj-felt>' +
      '<div class="scoreboard-header bj-head">THE PIT · WEEK ' + s.week +
        '<span class="bj-bank">' + s.chips.toLocaleString() + ' chips</span></div>' +
      '<div class="bj-body">' +
        '<div class="bj-row"><span class="bj-who">DEALER</span>' + dealer +
          (dealerTot != null ? '<span class="bj-tot">' + dealerTot + '</span>'
                             : '<span class="bj-seal">sealed Tuesday</span>') + '</div>' +
        body +
        (lastError ? '<p class="bj-err">' + esc(lastError) + '</p>' : '') +
      '</div>' +
    '</div>';

  mount.querySelectorAll('[data-bj]').forEach(btn => {
    btn.addEventListener('click', () => act(btn.dataset.bj, mount), { once: true });
  });
}

function verdict(h) {
  if (h.state === 'busted' && h.outcome == null) return 'Bust. Settles at kickoff.';
  if (h.outcome == null) return 'Standing on ' + handTotal(h.cards) + '. Settles at kickoff.';
  const n = h.payout > 0 ? '+' + h.payout : String(h.payout);
  return ({ win: 'Won', lose: 'Lost', push: 'Pushed', blackjack: 'BLACKJACK' })[h.outcome] +
         ' · ' + n + ' chips';
}

// Postgres already writes every one of these for a player to read - see the
// list in the plan. The one exception is a raw unique-violation, which is
// what a double-clicked DEAL produces, and which nobody but a DBA would parse.
function friendlyError(err) {
  const msg = err?.message || String(err);
  if (err?.code === '23505' || /duplicate key/i.test(msg)) {
    return 'You have already played this week’s hand.';
  }
  return msg;
}

async function act(what, mount) {
  if (busy) return;
  setBusy(mount, true);
  const fn = { deal: 'bj_deal', hit: 'bj_hit', stand: 'bj_stand', double: 'bj_double' }[what];
  const args = what === 'deal'
    ? { p_bet: Number(mount.querySelector('#bj-bet')?.value || 0) } : {};
  const res = await supabase.rpc(fn, args);
  lastError = res.error ? friendlyError(res.error) : null;
  await loadPit();
  renderPit(mount);
  busy = false;
}

export default async function initPit(mount) {
  if (!mount) return;
  // Belt and braces, exactly as the leaderboard already does for the slots: a
  // dead cron must not be able to hide a result.
  await supabase.rpc('bj_settle_week').catch(() => {});
  lastError = null;
  await loadPit();
  renderPit(mount);
}
