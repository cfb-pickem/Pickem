# A hand a week — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a shared weekly blackjack hand to the CFB Pick'em league whose chips are the third regular-season tiebreaker and which never pays a single pick'em point.

**Architecture:** One migration adds two dealer tables (public up-card, private hole card), a hands table, a derived bankroll view, and seven `security definer` functions. All card logic lives in Postgres; the browser only sends hit/stand/double. `js/blackjack.js` mirrors the hand arithmetic for drawing the felt and is unit-tested in Node. The felt and a chips column go on the leaderboard's Tiebreakers view; a reveal plays on the first load after a week settles.

**Tech Stack:** Postgres/Supabase (SQL + plpgsql, RLS, pg_cron), vanilla ES modules, Tailwind (prebuilt) + `css/base.css`, Node for tests (`node tools/*.test.mjs`, no framework).

**Spec:** `docs/superpowers/specs/2026-09-24-a-hand-a-week-design.md`

## Global Constraints

- **Blackjack never pays pick'em points.** No task may touch a scoring path, points column, or sort comparator. Chips are evidence for a manual tiebreaker cascade only.
- Starting bankroll **1,000**, reset every season. Table minimum **25** *or the player's whole stack if less*. Table maximum **500**. Blackjack pays **3:2**. Dealer **stands on all 17s** including soft 17. Doubling on the **first decision only, for the full bet**. **No** splitting, insurance or surrender. Bankroll at exactly **0** = out for the season, **no reload**.
- **A dealer natural voids the week's play** ("original bets only"): the player's original bet loses, a player natural pushes, a double is refunded, drawn cards and busts are void.
- Cards are integers **0..51**. Rank index `card % 13` → `0=A, 1..8 = 2..9, 9=10, 10=J, 11=Q, 12=K`. Suit `card / 13` → `0=♠ 1=♥ 2=♦ 3=♣`.
- **Nothing in the browser decides anything.** This repo is public and the Supabase key is anon. Every chip is written by a `security definer` function.
- **`revoke execute … from public` is mandatory on every new function.** Postgres grants EXECUTE to PUBLIC by default; the slots rollout already shipped this bug once.
- Migrations are applied by hand with `supabase db query -f`, never `db push` (the remote has no `supabase_migrations.schema_migrations` table).
- `css/tailwind.css` is generated. After any markup/class change run `npm run css && npm run stamp` or CI fails.
- Commit messages end with the two attribution trailers used throughout this repo.

---

### Task 1: Card and hand arithmetic in JavaScript

The pure half of `js/blackjack.js`. It mirrors the SQL written in Task 3, and the same test vectors are used on both sides so they cannot disagree about what a hand is worth.

**Files:**
- Create: `js/blackjack.js`
- Create: `tools/blackjack.test.mjs`

**Interfaces:**
- Consumes: nothing.
- Produces: `cardRank(card)`, `cardSuit(card)`, `cardValue(card)`, `cardLabel(card)`, `handTotal(cards)`, `isSoft(cards)`, `isNatural(cards)`, `isBust(cards)`, `TABLE` (`{start, min, max}`), `legalBets(chips)` → `{min,max}|null`, `betIsLegal(bet, chips)`, `settleHand({cards,bet,doubled}, dealerCards)` → `{outcome, payout}`, `dealerPlay(upAndHole, draw)` → `card[]`. Tasks 8, 9 and 10 consume all of these.

- [ ] **Step 1: Write the failing test**

Create `tools/blackjack.test.mjs`:

```js
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
eq(handTotal([KING, 5, 6]), 21, '[K,6,7] is 21');
eq(handTotal([KING, KING, TWO]), 22, 'a real bust stays over 21');
eq(handTotal([]), 0, 'an empty hand is 0');
ok(isSoft([A, 5]), '[A,6] is soft');
ok(!isSoft([A, KING, TWO]), '[A,K,2] is hard 13');
ok(isNatural([A, JACK]), '[A,J] is a natural');
ok(!isNatural([A, 5, 4]), 'three cards to 21 is not a natural');
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
eq(dealerPlay([TEN, 5], () => TWO).length, 4, 'dealer draws 15 -> 17 in two twos');
eq(handTotal(dealerPlay([TEN, 5], () => TWO)), 19, 'and stops once at 17 or better');
eq(dealerPlay([A, 5], () => { throw new Error('drew on soft 17'); }).length, 2,
   'dealer stands on soft 17');
eq(dealerPlay([A, KING], () => { throw new Error('drew on a natural'); }).length, 2,
   'dealer never draws on a natural');

// Settlement.
const hand = (cards, bet = 100, doubled = false) => ({ cards, bet, doubled });
eq(settleHand(hand([KING, NINE]), [KING, 7]).payout, 100, '19 beats 17');
eq(settleHand(hand([KING, NINE]), [KING, NINE]).outcome, 'push', 'equal totals push');
eq(settleHand(hand([KING, 5]), [KING, NINE]).payout, -100, '15 loses to 19');
eq(settleHand(hand([KING, KING, TWO]), [KING, 5]).payout, -100,
   'a bust loses even though the dealer sat on 15');
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
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `node tools/blackjack.test.mjs`
Expected: FAIL — `Cannot find module …/js/blackjack.js`

- [ ] **Step 3: Write the minimal implementation**

Create `js/blackjack.js`:

```js
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
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `node tools/blackjack.test.mjs`
Expected: PASS — `blackjack: all assertions passed`

- [ ] **Step 5: Commit**

```bash
git add js/blackjack.js tools/blackjack.test.mjs
git commit -m "Blackjack hand arithmetic, with the vectors that keep it honest"
```

---

### Task 2: The migration — tables, the bankroll view, and RLS

**Files:**
- Create: `supabase/migrations/20260924120000_a_hand_a_week.sql`

**Interfaces:**
- Consumes: `public.teams(team_id, user_id)`, `public.all_games(cfb_season, week, picked, tiebreaker, "Start (CT)")`.
- Produces: tables `blackjack_weeks`, `blackjack_hole`, `blackjack_hands`; view `blackjack_bankrolls(team_id, cfb_season, chips, hands_played, busted)`; helper `bj_my_team()`. Tasks 3–6 append to this same file.

- [ ] **Step 1: Write the file's header and section 1**

```sql
-- A hand a week.
--
-- One shared dealer hand for the whole league, dealt when the lines lock and
-- settled at the first kickoff. It pays chips, and chips are the third
-- regular-season tiebreaker. IT NEVER PAYS A POINT - that is the constraint the
-- whole feature was designed inside, and nothing in this migration touches a
-- scoring path.
--
-- See docs/superpowers/specs/2026-09-24-a-hand-a-week-design.md.

-- 1. THE TABLES ------------------------------------------------------------

-- Public. One row per week: the up-card everybody argues about all week.
create table if not exists public.blackjack_weeks (
  cfb_season   integer not null,
  week         integer not null,
  dealer_up    smallint not null,
  dealer_final smallint[],
  opened_at    timestamptz not null default now(),
  settled_at   timestamptz,
  primary key (cfb_season, week)
);

-- Private, and this split IS the security model. RLS hides rows, not columns, so
-- a hole card living in a table the league can read is a hole card the league
-- can read. Nothing selects from here but security-definer functions.
create table if not exists public.blackjack_hole (
  cfb_season integer not null,
  week       integer not null,
  hole       smallint not null,
  primary key (cfb_season, week)
);

create table if not exists public.blackjack_hands (
  id         bigserial primary key,
  team_id    bigint  not null references public.teams(team_id) on delete cascade,
  cfb_season integer not null,
  week       integer not null,
  bet        integer not null check (bet > 0),
  doubled    boolean not null default false,
  cards      smallint[] not null,
  state      text not null check (state in ('live','stood','busted','settled')),
  outcome    text check (outcome in ('win','lose','push','blackjack')),
  payout     integer,
  dealt_at   timestamptz not null default now(),
  settled_at timestamptz,
  -- ONE HAND A WEEK, structurally. Not a check in application code that can be
  -- worked around - a constraint that makes a second hand un-insertable, the
  -- same way slots_jackpot's check (id = 1) makes a second meter un-insertable.
  unique (team_id, cfb_season, week)
);

comment on column public.blackjack_hands.bet is
  'The ORIGINAL bet, never doubled in place. Total at risk is bet * 2 when doubled - which is what makes the "original bets only" refund against a dealer natural a one-word rule instead of a reconstruction.';

create index if not exists blackjack_hands_unsettled
  on public.blackjack_hands (cfb_season, week) where settled_at is null;
```

- [ ] **Step 2: Append the bankroll view and the ownership helper**

```sql
-- 2. THE BANKROLL IS DERIVED -----------------------------------------------

-- No stored balance. A stored balance is a second source of truth that drifts
-- from the hands that produced it, this is a few hundred rows a season, and
-- deriving it means every chip in the tiebreaker traces back to a hand somebody
-- actually played.
create or replace view public.blackjack_bankrolls as
  select t.team_id,
         s.cfb_season,
         1000 + coalesce(sum(h.payout), 0)::integer                    as chips,
         count(h.id) filter (where h.settled_at is not null)::integer  as hands_played,
         (1000 + coalesce(sum(h.payout), 0)) <= 0                      as busted
    from public.teams t
   cross join (select distinct cfb_season from public.blackjack_weeks) s
    left join public.blackjack_hands h
           on h.team_id = t.team_id
          and h.cfb_season = s.cfb_season
          and h.settled_at is not null
   group by t.team_id, s.cfb_season;

comment on view public.blackjack_bankrolls is
  'Chips per player per season: 1000 plus every settled payout. The third regular-season tiebreaker reads this and nothing else.';

-- Which team is the caller? teams.user_id -> auth.uid(), the same mapping
-- owns_team() uses, wrapped so the five functions below read as English.
create or replace function public.bj_my_team()
returns bigint language sql stable security definer set search_path = public as $$
  select t.team_id from public.teams t where t.user_id = auth.uid() limit 1;
$$;
```

- [ ] **Step 3: Append RLS**

```sql
-- 3. WHO CAN SEE WHAT ------------------------------------------------------

alter table public.blackjack_weeks  enable row level security;
alter table public.blackjack_hole   enable row level security;
alter table public.blackjack_hands  enable row level security;

-- The up-card is public because the felt is drawn for signed-out visitors too,
-- and there is nothing in it to protect: it is one card everybody can see.
drop policy if exists blackjack_weeks_readable on public.blackjack_weeks;
create policy blackjack_weeks_readable on public.blackjack_weeks
  for select to anon, authenticated using (true);

-- blackjack_hole gets NO POLICY AT ALL. Not a restrictive one, not a false one -
-- none, so RLS denies every row to every client. The only readers are
-- security-definer functions, which bypass RLS entirely.

-- Your own hand always; everybody else's once the week has settled. Mirrors
-- picks_hidden_until_lock exactly: watching what the league did is most of the
-- entertainment and none of it may leak while it still matters.
drop policy if exists blackjack_hands_readable on public.blackjack_hands;
create policy blackjack_hands_readable on public.blackjack_hands
  for select to anon, authenticated
  using (
    public.owns_team(team_id)
    or public.is_commissioner()
    or exists (select 1 from public.blackjack_weeks w
                where w.cfb_season = blackjack_hands.cfb_season
                  and w.week = blackjack_hands.week
                  and w.settled_at is not null)
  );

-- No insert, update or delete policy on any of the three, on purpose. Every
-- write goes through a security-definer function; there is no path by which a
-- client can author a card or a chip.
revoke insert, update, delete on public.blackjack_weeks, public.blackjack_hole,
                                 public.blackjack_hands from anon, authenticated;
grant select on public.blackjack_weeks, public.blackjack_hands to anon, authenticated;
grant select on public.blackjack_bankrolls to anon, authenticated;
revoke all on public.blackjack_hole from anon, authenticated;
```

- [ ] **Step 4: Verify the file parses without applying it**

Run: `npx supabase db query -f supabase/migrations/20260924120000_a_hand_a_week.sql --dry-run 2>&1 | head -20` — if the CLI has no dry-run, instead check syntax by eye and defer to Task 7, where the whole file is applied inside a rolled-back transaction.
Expected: no syntax errors reported.

- [ ] **Step 5: Commit**

```bash
git add supabase/migrations/20260924120000_a_hand_a_week.sql
git commit -m "Blackjack schema: two dealer tables, derived bankrolls, no write path"
```

---

### Task 3: The migration — card arithmetic in SQL

The authoritative copy. Task 1's JS mirrors it; the same vectors are asserted here so the two cannot drift.

**Files:**
- Modify: `supabase/migrations/20260924120000_a_hand_a_week.sql` (append)

**Interfaces:**
- Consumes: nothing.
- Produces: `bj_card_value(smallint) → integer`, `bj_total(smallint[]) → integer`, `bj_is_natural(smallint[]) → boolean`, `bj_draw_card() → smallint`. Tasks 5 and 6 consume all four.

- [ ] **Step 1: Append the functions**

```sql
-- 4. THE ARITHMETIC --------------------------------------------------------

-- Cards are 0..51: rank is card % 13 (0 = ace, 9..12 = ten through king), suit
-- is card / 13. Drawing uniformly over that range gives the correct
-- four-in-thirteen chance of a ten-value card, which is why this feature has no
-- shoe: a shoe exists so cards can be counted, and nobody is counting across
-- one hand a week.
create or replace function public.bj_card_value(card smallint)
returns integer language sql immutable as $$
  select case
           when card % 13 = 0  then 11        -- ace, demoted by bj_total
           when card % 13 >= 9 then 10        -- 10, J, Q, K
           else (card % 13) + 1
         end;
$$;

-- Aces at eleven, demoted ten at a time only while the hand is over 21. The
-- integer form of "demote ceil((raw - 21) / 10) of them, but never more than we
-- hold".
create or replace function public.bj_total(cards smallint[])
returns integer language sql immutable as $$
  with s as (
    select coalesce(sum(public.bj_card_value(c)), 0)::integer            as raw,
           coalesce(count(*) filter (where c % 13 = 0), 0)::integer      as aces
      from unnest(coalesce(cards, '{}'::smallint[])) as c
  )
  select case when raw <= 21 then raw
              else raw - 10 * least(aces, ((raw - 21) + 9) / 10)
         end
    from s;
$$;

-- TWO cards to 21. Three cards the hard way is a 21 and pays even money.
create or replace function public.bj_is_natural(cards smallint[])
returns boolean language sql immutable as $$
  select coalesce(array_length(cards, 1), 0) = 2 and public.bj_total(cards) = 21;
$$;

create or replace function public.bj_draw_card()
returns smallint language sql volatile as $$
  select floor(random() * 52)::smallint;
$$;

grant execute on function public.bj_card_value(smallint) to anon, authenticated;
grant execute on function public.bj_total(smallint[])    to anon, authenticated;
grant execute on function public.bj_is_natural(smallint[]) to anon, authenticated;
revoke execute on function public.bj_draw_card() from public, anon, authenticated;
```

- [ ] **Step 2: Append the assertions that pin SQL to the JS vectors**

```sql
-- THE SAME VECTORS tools/blackjack.test.mjs asserts in JavaScript. The felt
-- draws a total in JS and Postgres pays out on a total in SQL; the day those
-- disagree, somebody is paid wrongly. This block fails the migration rather
-- than letting that ship.
do $$
begin
  if public.bj_total(array[0,12]::smallint[])      <> 21 then raise exception '[A,K] should be 21'; end if;
  if public.bj_total(array[0,0,8]::smallint[])     <> 21 then raise exception '[A,A,9] should be 21'; end if;
  if public.bj_total(array[0,0,0,7]::smallint[])   <> 21 then raise exception '[A,A,A,8] should be 21'; end if;
  if public.bj_total(array[0,8,4]::smallint[])     <> 15 then raise exception '[A,9,5] should be 15'; end if;
  if public.bj_total(array[12,5,6]::smallint[])    <> 21 then raise exception '[K,6,7] should be 21'; end if;
  if public.bj_total(array[12,12,1]::smallint[])   <> 22 then raise exception '[K,K,2] should be 22'; end if;
  if public.bj_total('{}'::smallint[])             <> 0  then raise exception 'empty hand should be 0'; end if;
  if not public.bj_is_natural(array[0,10]::smallint[])    then raise exception '[A,J] should be a natural'; end if;
  if     public.bj_is_natural(array[0,4,5]::smallint[])   then raise exception '[A,5,6] should not be a natural'; end if;
end $$;
```

- [ ] **Step 3: Commit**

```bash
git add supabase/migrations/20260924120000_a_hand_a_week.sql
git commit -m "Blackjack arithmetic in SQL, pinned to the same vectors as the JS"
```

---

### Task 4: The migration — the week's lock, and opening the table

**Files:**
- Modify: `supabase/migrations/20260924120000_a_hand_a_week.sql` (append)

**Interfaces:**
- Consumes: `public.line_lock_at(timestamp)` from `20260822010000_line_locks_tuesday.sql`; `bj_draw_card()`, `bj_is_natural()` from Task 3.
- Produces: `bj_week_open(integer, integer) → boolean`, `bj_live_week() → (cfb_season integer, week integer)`, `bj_open_week() → integer`. Tasks 5, 6, 8 and 10 consume all three.

- [ ] **Step 1: Append the lock**

```sql
-- 5. WHEN THE TABLE IS OPEN ------------------------------------------------

-- A SECOND DEADLINE DEFINITION, stated rather than buried, because this repo's
-- rule is that there is never a second definition to drift out of step.
--
-- It is a different QUESTION, not a second answer to the same one.
-- picks_open_for_game() answers "is this game still open", and in the playoffs
-- it answers per game. A hand is not attached to a game, so it needs "is this
-- week's table still open" - and for one weekly hand a per-game lock is
-- meaningless. So the regular-season branch of picks_open_for_game() is reused
-- verbatim and applied to playoff weeks too.
--
-- A week with no picked games is never open, which is what stops the table being
-- dealt for a week the league does not play.
create or replace function public.bj_week_open(p_season integer, p_week integer)
returns boolean language sql stable security definer set search_path = public as $$
  select exists (
           select 1 from public.all_games x
            where x.cfb_season = p_season and x.week = p_week
              and (coalesce(x.picked, false) or coalesce(x.tiebreaker, false))
              and x."Start (CT)" is not null
         )
     and not exists (
           select 1 from public.all_games x
            where x.cfb_season = p_season and x.week = p_week
              and (coalesce(x.picked, false) or coalesce(x.tiebreaker, false))
              and x."Start (CT)" is not null
              and (x."Start (CT)")::timestamp <= (now() at time zone 'America/Chicago')
         );
$$;

-- The week the felt plays. The open week if there is one; otherwise the most
-- recently opened table, so that between Saturday's kickoff and next Tuesday's
-- lines lock the felt still shows last week's result instead of going blank.
create or replace function public.bj_live_week()
returns table (cfb_season integer, week integer)
language sql stable security definer set search_path = public as $$
  select g.cfb_season, g.week
    from (select distinct a.cfb_season, a.week
            from public.all_games a
           where coalesce(a.picked, false) or coalesce(a.tiebreaker, false)) g
   where public.bj_week_open(g.cfb_season, g.week)
   order by g.cfb_season desc, g.week asc
   limit 1;
$$;

grant execute on function public.bj_week_open(integer, integer) to anon, authenticated;
grant execute on function public.bj_live_week() to anon, authenticated;
```

- [ ] **Step 2: Append the opener**

```sql
-- 6. DEALING THE WEEK ------------------------------------------------------

-- The table opens when the LINES lock, which is the moment the week becomes
-- real and which line_lock_at() already defines. Idempotent and driven from
-- cron every minute rather than fired once at 11:00 on Tuesday: a scheduled
-- moment that gets missed would leave the league with no table for a week, and
-- a self-healing check costs nothing.
--
-- The hole card is dealt NOW, not at settlement. Dealing it at settlement would
-- let the house choose its second card after watching the table play, and no
-- amount of "the server is trusted" makes that honest. It can be a natural -
-- this is real blackjack - and bj_settle_week() honours the peek that a
-- five-day hand cannot take.
create or replace function public.bj_open_week()
returns integer language plpgsql security definer set search_path = public as $$
declare
  s integer; w integer; first_kick timestamp; n integer := 0;
begin
  for s, w, first_kick in
    select a.cfb_season, a.week, min((a."Start (CT)")::timestamp)
      from public.all_games a
     where (coalesce(a.picked, false) or coalesce(a.tiebreaker, false))
       and a."Start (CT)" is not null
     group by a.cfb_season, a.week
  loop
    continue when (now() at time zone 'America/Chicago') < public.line_lock_at(first_kick);
    continue when not public.bj_week_open(s, w);

    insert into public.blackjack_weeks (cfb_season, week, dealer_up)
    values (s, w, public.bj_draw_card())
    on conflict (cfb_season, week) do nothing;

    if found then
      insert into public.blackjack_hole (cfb_season, week, hole)
      values (s, w, public.bj_draw_card())
      on conflict (cfb_season, week) do nothing;
      n := n + 1;
    end if;
  end loop;
  return n;
end;
$$;

revoke execute on function public.bj_open_week() from public, anon;
grant  execute on function public.bj_open_week() to authenticated;
```

- [ ] **Step 3: Commit**

```bash
git add supabase/migrations/20260924120000_a_hand_a_week.sql
git commit -m "Blackjack: the week's own lock, and opening the table at the lines lock"
```

---

### Task 5: The migration — playing the hand

**Files:**
- Modify: `supabase/migrations/20260924120000_a_hand_a_week.sql` (append)

**Interfaces:**
- Consumes: Tasks 2–4.
- Produces: `bj_deal(integer) → blackjack_hands`, `bj_hit() → blackjack_hands`, `bj_double() → blackjack_hands`, `bj_stand() → blackjack_hands`. Task 8 calls all four by RPC.

- [ ] **Step 1: Append the deal**

```sql
-- 7. PLAYING THE HAND -----------------------------------------------------

-- Every one of these re-derives the caller's team and the live week server-side.
-- Nothing is taken from the client but the bet, and that is validated against a
-- bankroll the client cannot write.
create or replace function public.bj_deal(p_bet integer)
returns public.blackjack_hands
language plpgsql security definer set search_path = public as $$
declare
  me bigint; s integer; w integer; chips integer; lo integer; hi integer;
  cards smallint[]; out_row public.blackjack_hands;
begin
  me := public.bj_my_team();
  if me is null then raise exception 'no team for this account'; end if;

  select cfb_season, week into s, w from public.bj_live_week();
  if s is null then raise exception 'no table is open'; end if;

  -- OPEN MEANS THE DEALER EXISTS, not merely that nothing has kicked off.
  -- bj_week_open() is true on Monday too, and a bet placed before the lines lock
  -- would be a bet against a dealer who has not been dealt.
  if not exists (select 1 from public.blackjack_weeks
                  where cfb_season = s and week = w) then
    raise exception 'the table for this week has not opened yet';
  end if;

  select b.chips into chips from public.blackjack_bankrolls b
   where b.team_id = me and b.cfb_season = s;
  chips := coalesce(chips, 1000);
  if chips <= 0 then raise exception 'you are out for the season'; end if;

  -- Your last chips are always a legal bet; you are out at exactly zero and
  -- nowhere else.
  lo := least(25, chips);
  hi := least(500, chips);
  if p_bet is null or p_bet < lo or p_bet > hi then
    raise exception 'bet must be between % and %', lo, hi;
  end if;

  cards := array[public.bj_draw_card(), public.bj_draw_card()]::smallint[];

  insert into public.blackjack_hands (team_id, cfb_season, week, bet, cards, state)
  values (me, s, w, p_bet, cards,
          case when public.bj_is_natural(cards) then 'stood' else 'live' end)
  returning * into out_row;

  return out_row;
end;
$$;
```

- [ ] **Step 2: Append hit, double and stand**

```sql
-- One private helper so all three share a single definition of "a hand you are
-- still allowed to act on". Without it, three copies of the same four guards.
create or replace function public.bj_my_live_hand()
returns public.blackjack_hands
language plpgsql security definer set search_path = public as $$
declare me bigint; s integer; w integer; h public.blackjack_hands;
begin
  me := public.bj_my_team();
  if me is null then raise exception 'no team for this account'; end if;
  select cfb_season, week into s, w from public.bj_live_week();
  if s is null then raise exception 'the table has closed'; end if;

  select * into h from public.blackjack_hands
   where team_id = me and cfb_season = s and week = w for update;
  if h.id is null then raise exception 'you have no hand this week'; end if;
  if h.state <> 'live' then raise exception 'that hand is already finished'; end if;
  return h;
end;
$$;

create or replace function public.bj_hit()
returns public.blackjack_hands
language plpgsql security definer set search_path = public as $$
declare h public.blackjack_hands; cards smallint[]; out_row public.blackjack_hands;
begin
  h := public.bj_my_live_hand();
  cards := h.cards || public.bj_draw_card();
  update public.blackjack_hands
     set cards = cards,
         state = case when public.bj_total(cards) > 21 then 'busted' else 'live' end
   where id = h.id
  returning * into out_row;
  return out_row;
end;
$$;

create or replace function public.bj_double()
returns public.blackjack_hands
language plpgsql security definer set search_path = public as $$
declare h public.blackjack_hands; chips integer; cards smallint[];
        out_row public.blackjack_hands;
begin
  h := public.bj_my_live_hand();

  -- FIRST DECISION ONLY, which is what "double down" means everywhere.
  if coalesce(array_length(h.cards, 1), 0) <> 2 then
    raise exception 'you can only double on your first decision';
  end if;
  if h.doubled then raise exception 'already doubled'; end if;

  select b.chips into chips from public.blackjack_bankrolls b
   where b.team_id = h.team_id and b.cfb_season = h.cfb_season;
  chips := coalesce(chips, 1000);
  -- No doubling for less. The button is disabled when the stack will not cover
  -- it, and this is the server saying the same thing.
  if h.bet * 2 > chips then raise exception 'not enough chips to double'; end if;

  -- Exactly one card, then the hand is final however it landed.
  cards := h.cards || public.bj_draw_card();
  update public.blackjack_hands
     set cards = cards, doubled = true,
         state = case when public.bj_total(cards) > 21 then 'busted' else 'stood' end
   where id = h.id
  returning * into out_row;
  return out_row;
end;
$$;

create or replace function public.bj_stand()
returns public.blackjack_hands
language plpgsql security definer set search_path = public as $$
declare h public.blackjack_hands; out_row public.blackjack_hands;
begin
  h := public.bj_my_live_hand();
  update public.blackjack_hands set state = 'stood' where id = h.id
  returning * into out_row;
  return out_row;
end;
$$;

revoke execute on function public.bj_deal(integer)    from public, anon;
revoke execute on function public.bj_hit()            from public, anon;
revoke execute on function public.bj_double()         from public, anon;
revoke execute on function public.bj_stand()          from public, anon;
revoke execute on function public.bj_my_live_hand()   from public, anon, authenticated;
revoke execute on function public.bj_my_team()        from public, anon;
grant  execute on function public.bj_deal(integer)    to authenticated;
grant  execute on function public.bj_hit()            to authenticated;
grant  execute on function public.bj_double()         to authenticated;
grant  execute on function public.bj_stand()          to authenticated;
grant  execute on function public.bj_my_team()        to authenticated;
```

- [ ] **Step 3: Commit**

```bash
git add supabase/migrations/20260924120000_a_hand_a_week.sql
git commit -m "Blackjack: deal, hit, double and stand, all decided server-side"
```

---

### Task 6: The migration — settlement, and the peek applied backwards

**Files:**
- Modify: `supabase/migrations/20260924120000_a_hand_a_week.sql` (append)

**Interfaces:**
- Consumes: Tasks 2–4.
- Produces: `bj_settle_week() → integer`. Tasks 8 and 10 call it on page load; cron calls it every minute.

- [ ] **Step 1: Append settlement**

```sql
-- 8. SETTLEMENT -----------------------------------------------------------

-- Modelled on resolve_slot_picks(): idempotent, one-way, guarded on
-- settled_at is null, cron every minute, and called once on leaderboard load so
-- a dead cron cannot hide a result.
create or replace function public.bj_settle_week()
returns integer language plpgsql security definer set search_path = public as $$
declare
  w record; h record;
  dealer smallint[]; d_total integer; d_natural boolean;
  p_total integer; p_natural boolean; stake integer; pay integer; res text;
  n integer := 0;
begin
  for w in
    select bw.cfb_season, bw.week, bw.dealer_up, bh.hole
      from public.blackjack_weeks bw
      join public.blackjack_hole  bh
        on bh.cfb_season = bw.cfb_season and bh.week = bw.week
     where bw.settled_at is null
       and not public.bj_week_open(bw.cfb_season, bw.week)
     order by bw.cfb_season, bw.week
       for update of bw
  loop
    dealer    := array[w.dealer_up, w.hole]::smallint[];
    d_natural := public.bj_is_natural(dealer);

    -- A natural never draws. The week is already over.
    if not d_natural then
      while public.bj_total(dealer) < 17 loop      -- stands on all 17s, soft included
        dealer := dealer || public.bj_draw_card();
      end loop;
    end if;
    d_total := public.bj_total(dealer);

    for h in
      select id, bet, doubled, cards from public.blackjack_hands
       where cfb_season = w.cfb_season and week = w.week and settled_at is null
       order by id
    loop
      p_total   := public.bj_total(h.cards);
      p_natural := public.bj_is_natural(h.cards);
      stake     := h.bet * (case when h.doubled then 2 else 1 end);

      if d_natural then
        -- THE PEEK, APPLIED BACKWARDS. A real dealer showing an ace or a ten
        -- looks and ends the hand on a natural, so nobody can double into a
        -- hand that was already over. A hole card sealed from Tuesday cannot
        -- peek, so the week's play is void instead and only the ORIGINAL bet was
        -- ever at risk: US "original bets only". A player who doubled to 20 on
        -- Wednesday and a player who busted on Thursday both lose h.bet and no
        -- more, because neither hand should have existed.
        if p_natural then pay := 0;           res := 'push';
        else              pay := -h.bet;      res := 'lose';
        end if;
      elsif p_natural then
        pay := (h.bet * 3) / 2;               res := 'blackjack';   -- 3:2, rounded down
      elsif p_total > 21 then
        pay := -stake;                        res := 'lose';
      elsif d_total > 21 or p_total > d_total then
        pay := stake;                         res := 'win';
      elsif p_total = d_total then
        pay := 0;                             res := 'push';
      else
        pay := -stake;                        res := 'lose';
      end if;

      -- A HAND STILL LIVE AT THE LOCK STANDS WHERE IT IS. You left it on 16, it
      -- plays as 16. The alternatives are punishing somebody for a hand they
      -- did start, or letting them act after the lock.
      update public.blackjack_hands
         set state = 'settled', outcome = res, payout = pay, settled_at = now()
       where id = h.id and settled_at is null;
      if found then n := n + 1; end if;
    end loop;

    update public.blackjack_weeks
       set dealer_final = dealer, settled_at = now()
     where cfb_season = w.cfb_season and week = w.week and settled_at is null;
  end loop;
  return n;
end;
$$;

comment on function public.bj_settle_week() is
  'Settle every closed week: flip the hole card, draw to 17, pay the table. A dealer natural voids the week (original bets only). Idempotent and one-way.';

revoke execute on function public.bj_settle_week() from public, anon;
grant  execute on function public.bj_settle_week() to authenticated;
```

- [ ] **Step 2: Append cron**

```sql
-- 9. CRON -----------------------------------------------------------------

-- Every minute, for the same reason resolve_slot_picks() runs every minute: the
-- leaderboard reveals at kickoff and an unsettled table renders as a blank
-- felt. One job does both halves so they cannot get out of step.
select cron.unschedule('blackjack-table')
 where exists (select 1 from cron.job where jobname = 'blackjack-table');

select cron.schedule('blackjack-table', '* * * * *',
                     $$select public.bj_open_week(); select public.bj_settle_week();$$);
```

- [ ] **Step 3: Commit**

```bash
git add supabase/migrations/20260924120000_a_hand_a_week.sql
git commit -m "Blackjack settlement: draw to 17, and the peek applied backwards"
```

---

### Task 7: Apply and verify against the live database

Nothing below is optional. The slots rollout found `anon` holding EXECUTE on a function that had been "granted to authenticated", and that is the bug this task exists to catch.

**Files:** none changed — this is verification.

- [ ] **Step 1: Apply the migration**

```bash
npx supabase db query -f supabase/migrations/20260924120000_a_hand_a_week.sql
```

Expected: no errors. The `do $$` assertion block in Task 3 raises if the SQL arithmetic disagrees with the JS vectors.

- [ ] **Step 2: Verify the grants are actually what the comments claim**

```sql
select p.proname,
       has_function_privilege('anon',          p.oid, 'execute') as anon,
       has_function_privilege('authenticated', p.oid, 'execute') as auth
  from pg_proc p join pg_namespace n on n.oid = p.pronamespace
 where n.nspname = 'public' and p.proname like 'bj\_%'
 order by 1;
```

Expected: `anon = false` for `bj_deal`, `bj_hit`, `bj_double`, `bj_stand`, `bj_settle_week`, `bj_open_week`, `bj_draw_card`, `bj_my_live_hand`, `bj_my_team`. `anon = true` only for `bj_card_value`, `bj_total`, `bj_is_natural`, `bj_week_open`, `bj_live_week`.

- [ ] **Step 3: Verify the hole card is unreachable**

```sql
begin;
set local role authenticated;
select count(*) from public.blackjack_hole;   -- expect 0 rows, not an error
rollback;
```

Expected: `0`. RLS is enabled with no policy, so every row is denied.

- [ ] **Step 4: Verify a full hand end to end, then roll it back**

```sql
begin;
-- Impersonate a real non-commissioner member.
set local role authenticated;
set local request.jwt.claims to '{"sub":"<a real teams.user_id uuid>","role":"authenticated"}';

select public.bj_deal(25);          -- expect two cards and a live hand
select public.bj_hit();             -- expect a third card
select public.bj_stand();           -- expect state = stood
select public.bj_deal(25);          -- expect: unique violation, one hand a week
select public.bj_deal(24);          -- expect: 'bet must be between 25 and 500'
select public.bj_deal(501);         -- expect: 'bet must be between 25 and 500'
rollback;
```

- [ ] **Step 5: Verify settlement is idempotent and one-way**

```sql
begin;
-- Force a closed week by settling whatever the openers have produced.
select public.bj_settle_week();     -- expect n >= 0
select public.bj_settle_week();     -- expect 0
select public.bj_settle_week();     -- expect 0
rollback;
```

- [ ] **Step 6: Sweep for leftovers**

```sql
select (select count(*) from public.blackjack_hands) as hands,
       (select count(*) from public.blackjack_weeks) as weeks,
       (select count(*) from public.blackjack_hole)  as holes;
```

Expected: `hands = 0`, and `weeks`/`holes` equal only for weeks whose lines have genuinely locked. If any test hand survived a rollback, delete it by id before going further.

- [ ] **Step 7: Verify cron is registered and owned by postgres**

```sql
select jobname, schedule, active, username from cron.job where jobname = 'blackjack-table';
```

- [ ] **Step 8: Commit the verification notes**

Append a `## Rollout — what actually happened` section to the spec recording what each check returned, the way `2026-09-11-let-the-slots-decide-design.md` does, then:

```bash
git add docs/superpowers/specs/2026-09-24-a-hand-a-week-design.md
git commit -m "Blackjack rollout notes: what the live verification found"
```

---

### Task 8: The felt on the Tiebreakers view

**Files:**
- Modify: `js/blackjack.js` (append the UI half)
- Modify: `index.html` — add the mount point near `index.html:456`, import and call from `loadTiebreakerWeek()` (`index.html:1530`)
- Modify: `css/base.css` (append)

**Interfaces:**
- Consumes: Task 1's exports; RPCs `bj_deal`, `bj_hit`, `bj_double`, `bj_stand`, `bj_settle_week`; views `blackjack_bankrolls`, tables `blackjack_weeks`, `blackjack_hands`.
- Produces: `initPit(mountEl)` (default export stays `initBlackjack`), `loadPit()`, `pitState()` → `{season, week, hand, dealerUp, dealerFinal, chips, settled}`. Task 10 consumes `pitState()`.

- [ ] **Step 1: Add the mount point to `index.html`**

Immediately after the `<div id="week-recap" …>` line (`index.html:456`):

```html
    <!-- The pit. Hidden on every view but Tiebreakers, because chips are a
         tiebreaker and that is the board where ties are settled. -->
    <div id="pit" class="mt-4" hidden></div>
```

- [ ] **Step 2: Append the felt to `js/blackjack.js`**

```js
import { supabase } from './supabaseClient.js';

// --- the felt -------------------------------------------------------------
//
// One panel, three states: no hand yet (place a bet), a hand in progress (hit,
// stand, double), and a settled week (the whole league's hands face up beside
// what the dealer had). The third is the one people will actually talk about.

let state = {
  season: null, week: null, hand: null, dealerUp: null, dealerFinal: null,
  chips: TABLE.start, settled: false, table: []
};

export function pitState() { return { ...state }; }

const card = (c, faceDown = false) =>
  faceDown
    ? '<span class="bj-card bj-card-down" aria-label="face down"></span>'
    : '<span class="bj-card' + (cardSuit(c) === 1 || cardSuit(c) === 2 ? ' bj-red' : '') +
      '">' + cardLabel(c) + '</span>';

const hand = (cards, faceDownAfter = Infinity) =>
  cards.map((c, i) => card(c, i >= faceDownAfter)).join('');

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

  const me = (await supabase.rpc('bj_my_team')).data ?? null;
  const bank = await supabase.from('blackjack_bankrolls')
    .select('chips, hands_played').eq('cfb_season', season).eq('team_id', me).maybeSingle();

  state = {
    season, week,
    dealerUp: wrow.dealer_up,
    dealerFinal: wrow.dealer_final ?? null,
    settled: !!wrow.settled_at,
    chips: bank.data?.chips ?? TABLE.start,
    hand: (hands.data || []).find(h => h.team_id === me) ?? null,
    table: hands.data || []
  };
  return state;
}

function renderPit(mount) {
  if (state.season == null) { mount.hidden = true; return; }
  mount.hidden = false;

  const s = state;
  const dealer = s.settled && s.dealerFinal
    ? hand(s.dealerFinal)
    : hand([s.dealerUp]) + card(0, true);
  const dealerTot = s.settled && s.dealerFinal ? handTotal(s.dealerFinal) : null;

  let body;
  if (!s.hand) {
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
      '<div class="bj-row"><span class="bj-who">YOU</span>' + hand(h.cards) +
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

async function act(what, mount) {
  const fn = { deal: 'bj_deal', hit: 'bj_hit', stand: 'bj_stand', double: 'bj_double' }[what];
  const args = what === 'deal'
    ? { p_bet: Number(mount.querySelector('#bj-bet')?.value || 0) } : {};
  const res = await supabase.rpc(fn, args);
  if (res.error) { alert(res.error.message); }
  await loadPit();
  renderPit(mount);
}

export default async function initPit(mount) {
  if (!mount) return;
  // Belt and braces, exactly as the leaderboard already does for the slots: a
  // dead cron must not be able to hide a result.
  await supabase.rpc('bj_settle_week').catch(() => {});
  await loadPit();
  renderPit(mount);
}
```

- [ ] **Step 3: Wire it into the Tiebreakers view**

In `index.html`, add to the module imports beside the existing `initJackpot` import:

```js
    import initPit, { loadPit, pitState } from './js/blackjack.js';
```

At the end of `loadTiebreakerWeek()`, immediately after `showTable();` (`index.html:1620`):

```js
      // The pit lives on this board and only this board: chips are a
      // tiebreaker, so they belong where ties are settled.
      initPit(document.getElementById('pit'));
```

And in every other view loader (`loadWeek`, `loadPlayoffWeek`), beside the existing `week-recap` reset:

```js
      { const el = document.getElementById('pit'); if (el) { el.hidden = true; el.innerHTML = ''; } }
```

- [ ] **Step 4: Append the styles to `css/base.css`**

```css
/* THE PIT — the league's blackjack table, on the tiebreaker board.
   Green felt against the same scoreboard chrome everything else uses, because a
   card table that ignored the site's furniture would read as an advert. */
.bj-felt{ background:linear-gradient(180deg,#0d3b24,#08281a); }
.bj-head{ padding:.6rem 1rem; border-bottom:3px solid #444;
  font-family:'Teko','Oswald',sans-serif; font-size:1.3rem; font-weight:700;
  text-transform:uppercase; letter-spacing:.06em; color:#ddd;
  display:flex; align-items:center; gap:.75rem; }
.bj-bank{ margin-left:auto; font-size:.95rem; color:var(--cfp-gold-2); }
.bj-body{ padding:1rem; display:flex; flex-direction:column; gap:.75rem; }
.bj-row{ display:flex; align-items:center; gap:.5rem; flex-wrap:wrap; }
.bj-who{ width:5.5rem; font-size:.7rem; letter-spacing:.12em; color:#9fb8a8;
  text-transform:uppercase; }
.bj-card{ display:inline-flex; align-items:center; justify-content:center;
  min-width:2.4rem; padding:.45rem .4rem; border-radius:.3rem;
  background:#f4f1ea; color:#16181c; font-weight:700; font-size:.95rem;
  box-shadow:0 2px 4px rgba(0,0,0,.45); }
.bj-card.bj-red{ color:#b3202c; }
.bj-card-down{ background:repeating-linear-gradient(45deg,#7d1d24,#7d1d24 4px,#5e1419 4px,#5e1419 8px);
  color:transparent; }
.bj-tot{ margin-left:.4rem; font-family:'Teko','Oswald',sans-serif; font-size:1.3rem;
  color:var(--cfp-ivory); }
.bj-seal{ margin-left:.4rem; font-size:.7rem; color:#7f9a8b; font-style:italic; }
.bj-btn{ padding:.45rem .9rem; border-radius:.3rem; border:1px solid #2f6b4c;
  background:#125c3c; color:#eafff3; font-weight:700; font-size:.8rem;
  letter-spacing:.06em; text-transform:uppercase; cursor:pointer; }
.bj-btn:hover{ background:#17734c; }
.bj-bet{ width:5.5rem; padding:.4rem .5rem; border-radius:.3rem; border:1px solid #2f6b4c;
  background:#07231a; color:#eafff3; }
.bj-note{ font-size:.75rem; color:#9fb8a8; }
.bj-note.bj-out{ color:#e08a8a; }
@media (max-width:640px){
  .bj-who{ width:100%; }
}
```

- [ ] **Step 5: Rebuild the stylesheet and check CI will pass**

Run: `npm run css && npm run stamp && git diff --stat`
Expected: `css/base.css` changed; `css/tailwind.css` either unchanged or regenerated consistently; `.html` cache stamps updated if the stylesheet hash moved. `.github/workflows/css-check.yml` fails the build if either is stale, so this step is not optional.

Then verify by eye: `npx serve .`, open the leaderboard, choose **Tiebreakers** from the week menu.
Expected: the felt appears under the board with the dealer's up-card and one face-down card, a bet box defaulting to 25, and no felt on the weekly or playoff boards.

- [ ] **Step 6: Commit**

```bash
git add js/blackjack.js index.html css/base.css css/tailwind.css
git commit -m "The pit: a felt on the tiebreaker board, dealt and settled by Postgres"
```

---

### Task 9: The chips column on the tiebreaker board

**Files:**
- Modify: `index.html` — `renderHeader()` (`index.html:2220`), `renderTable()` (`index.html:2452`), `loadTiebreakerWeek()` (`index.html:1526-1620`)

**Interfaces:**
- Consumes: `public.blackjack_bankrolls`.
- Produces: `lastChipsMap` (a `team_id → chips` object) read by `renderTable()`.

- [ ] **Step 1: Load the chips in `loadTiebreakerWeek()`**

Before the `teamsSorted` sort, add:

```js
      // Chips are the third regular-season tiebreaker, so they belong on this
      // board beside the football ones. Read only - nothing here can move one.
      let chipsMap = Object.create(null);
      if (!isPlayoffTb) {
        const cr = await supabase.from('blackjack_bankrolls')
          .select('team_id, chips').eq('cfb_season', currentSeason);
        for (const r of (cr.data || [])) chipsMap[r.team_id] = r.chips;
      }
      lastChipsMap = chipsMap;
```

Declare `let lastChipsMap = Object.create(null);` beside the existing `lastSlotsMap` declaration, and reset it to an empty object at the top of `loadWeek()` and `loadPlayoffWeek()` so no other view can render it.

- [ ] **Step 2: Add the header cell**

In `renderHeader()`, where the tiebreaker board's trailing columns are appended, add a final `<th>` when `isTiebreakerView && !isPlayoffView`:

```js
      if (isTiebreakerView && !isPlayoffView){
        html += '<th class="px-2 py-2 text-right whitespace-nowrap" title="Third regular-season tiebreaker">SEASON<br>CHIPS</th>';
      }
```

- [ ] **Step 3: Add the body cell**

At the end of each row in `renderTable()`, matching the same condition:

```js
        if (isTiebreakerView && !playoffMode){
          const c = lastChipsMap[team.team_id];
          const td = document.createElement('td');
          td.className = 'px-2 py-2 text-right whitespace-nowrap bj-chips';
          // A player sitting on exactly the starting stack has never played, and
          // the board is allowed to say so - that is the only thing keeping a
          // hoarder honest, since the table runs at real Vegas odds.
          td.textContent = (c == null) ? '—' : Number(c).toLocaleString();
          if (c === 0) td.classList.add('bj-busted');
          tr.appendChild(td);
        }
```

- [ ] **Step 4: Style it**

Append to `css/base.css`:

```css
.bj-chips{ font-family:'Teko','Oswald',sans-serif; font-size:1.1rem;
  color:var(--cfp-gold-2); }
.bj-chips.bj-busted{ color:#e08a8a; }
```

- [ ] **Step 5: Verify by eye**

Run: `npx serve .`, open `http://localhost:3000/index.html`, choose **Tiebreakers** from the week menu.
Expected: a `SEASON CHIPS` column on the right, `1,000` for everyone before any hand is played, and no chips column on the weekly or playoff boards.

- [ ] **Step 6: Commit**

```bash
npm run css && npm run stamp
git add index.html css/base.css css/tailwind.css
git commit -m "Chips on the tiebreaker board, where ties are actually settled"
```

---

### Task 10: The reveal

**Files:**
- Modify: `js/blackjack.js` (append)
- Modify: `index.html` — call it from the reveal path near `playRevealAnimation()` (`index.html:2866`)
- Modify: `css/base.css` (append)

**Interfaces:**
- Consumes: `pitState()` from Task 8; `isRevealing()` from `js/slots.js:716`.
- Produces: `revealHand()`, `forgetHands()`.

- [ ] **Step 1: Append the reveal**

```js
import { isRevealing } from './slots.js';

// THE REVEAL. On the first load after a week settles, a player who played is
// shown the dealer's hand and their result. Same bargain the slots spin struck:
// a localStorage key rather than a column and a write on every load, because
// seeing your own hand twice is not a problem.
//
// It goes LAST, after the football. The board's picks are the main event and the
// pit is the coda; two takeovers competing for the same first load is worse than
// either. There is no identical-frames rule to keep here, unlike the football -
// the dealer's hand IS the result, so there is nothing to telegraph.
const SEEN = 'cfb-bj-seen';

export function forgetHands() { try { localStorage.removeItem(SEEN); } catch {} }

const seen = () => { try { return JSON.parse(localStorage.getItem(SEEN) || '[]'); } catch { return []; } };
const remember = k => { try { localStorage.setItem(SEEN, JSON.stringify([...seen(), k].slice(-40))); } catch {} };

export async function revealHand() {
  const s = pitState();
  if (!s.settled || !s.hand || !s.dealerFinal) return;
  const key = s.season + ':' + s.week;
  if (seen().includes(key)) return;

  // Wait out the football rather than talking over it.
  while (isRevealing()) await new Promise(r => setTimeout(r, 200));

  const reduced = matchMedia('(prefers-reduced-motion: reduce)').matches;
  const stage = document.createElement('div');
  stage.className = 'bj-stage';
  stage.innerHTML = '<div class="bj-stage-inner"></div>';
  document.body.appendChild(stage);
  const inner = stage.querySelector('.bj-stage-inner');

  const paint = (dealerCards, showVerdict) => {
    inner.innerHTML =
      '<div class="bj-row"><span class="bj-who">DEALER</span>' + hand(dealerCards) +
        '<span class="bj-tot">' + handTotal(dealerCards) + '</span></div>' +
      '<div class="bj-row"><span class="bj-who">YOU</span>' + hand(s.hand.cards) +
        '<span class="bj-tot">' + handTotal(s.hand.cards) + '</span></div>' +
      (showVerdict ? '<p class="bj-verdict">' + verdict(s.hand) + '</p>' : '');
  };

  const close = () => { stage.remove(); remember(key); };
  stage.addEventListener('click', close);

  if (reduced) { paint(s.dealerFinal, true); setTimeout(close, 2500); return; }

  const wait = ms => new Promise(r => setTimeout(r, ms));
  paint([s.dealerFinal[0]], false);                    // the up-card, as it stood all week
  await wait(600);
  paint(s.dealerFinal.slice(0, 2), false);             // the hole card flips
  await wait(800);
  for (let i = 3; i <= s.dealerFinal.length; i++) {    // draw to 17
    paint(s.dealerFinal.slice(0, i), false);
    await wait(700);
  }
  paint(s.dealerFinal, true);
  await wait(1200);
  close();
}
```

- [ ] **Step 2: Call it from the leaderboard's reveal path**

In `index.html`, at the end of `playRevealAnimation()` (after the `tbody.querySelectorAll` loop, `index.html:2884`):

```js
      // The coda. Only fires for a signed-in player who had a hand that week,
      // and only once per browser per week.
      loadPit().then(revealHand).catch(() => {});
```

Extend the import from Task 8:

```js
    import initPit, { loadPit, pitState, revealHand } from './js/blackjack.js';
```

- [ ] **Step 3: Style the stage**

```css
/* The reveal stage. Deliberately lighter than the football's takeover - this is
   the coda, not the event. */
.bj-stage{ position:fixed; inset:0; z-index:70; display:grid; place-items:center;
  background:rgba(4,8,6,.82); backdrop-filter:blur(2px); cursor:pointer; }
.bj-stage-inner{ background:linear-gradient(180deg,#0d3b24,#08281a);
  border:1px solid #2f6b4c; border-radius:.5rem; padding:1.25rem 1.5rem;
  display:flex; flex-direction:column; gap:.6rem; min-width:min(92vw,22rem);
  box-shadow:0 18px 50px rgba(0,0,0,.6); }
.bj-verdict{ font-family:'Teko','Oswald',sans-serif; font-size:1.6rem;
  text-transform:uppercase; letter-spacing:.04em; color:var(--cfp-gold-2);
  text-align:center; margin:.2rem 0 0; }
@media (prefers-reduced-motion: reduce){ .bj-stage{ backdrop-filter:none; } }
```

- [ ] **Step 4: Verify with a forced reveal**

In the browser console on the leaderboard:

```js
const m = await import('./js/blackjack.js');
m.forgetHands();
await m.loadPit();
await m.revealHand();
```

Expected: the up-card, then the hole card flipping, then the dealer's draws, then the verdict. Run it twice without `forgetHands()` — the second call must do nothing.

- [ ] **Step 5: Commit**

```bash
npm run css && npm run stamp
git add js/blackjack.js index.html css/base.css css/tailwind.css
git commit -m "The reveal: the hole card that has been face down since Tuesday"
```

---

### Task 11: The rules page, and a lab entry

**Files:**
- Modify: `cfb-genius.html` (`cfb-genius.html:97-115`)
- Modify: `js/sandbox.js`

**Interfaces:**
- Consumes: Task 1's exports.
- Produces: nothing consumed elsewhere.

- [ ] **Step 1: Insert chips into the cascade**

In `cfb-genius.html`, the regular-season tiebreaker list becomes:

```html
            <ol class="list-decimal list-inside space-y-0.5 mb-3">
              <li>Conference championship game wins</li>
              <li>Army v Navy</li>
              <li>Chips &mdash; the blackjack table on the Tiebreakers board</li>
              <li>Celebration Bowl</li>
              <li>Win % last 8 games</li>
              <li>Coin flip</li>
            </ol>
```

- [ ] **Step 2: Add the house rules beside it**

Immediately after that `</ol>`:

```html
            <p class="text-xs text-gray-400 mb-1">The Pit</p>
            <ul class="space-y-0.5 text-xs">
              <li>One shared dealer hand a week. Up-card Tuesday, settles at first kickoff.</li>
              <li>Everyone starts the season with <strong class="text-[var(--cfp-ivory)]">1,000 chips</strong>. Table 25&ndash;500.</li>
              <li>Blackjack pays 3:2. Dealer stands on all 17s. Double on your first decision only.</li>
              <li>A dealer blackjack takes original bets only &mdash; doubles come back.</li>
              <li>Bust to zero and you are out for the season. No reload.</li>
              <li><strong class="text-[var(--cfp-ivory)]">Chips never become points.</strong> They break ties and nothing else.</li>
            </ul>
```

- [ ] **Step 3: Add a lab entry to `js/sandbox.js`**

Following the pattern the slots lab already uses, add a panel that settles an arbitrary matchup locally with no database at all — the point is watching a dealer natural on a Tuesday instead of waiting for one:

```js
import { handTotal, isNatural, settleHand, cardLabel, dealerPlay } from './blackjack.js';

// A blackjack bench. No RPCs, no meter, no week - two hands typed in and the
// same settleHand() the felt uses, so an argument about what a hand pays can be
// settled in ten seconds rather than next April.
function bjBench(mount) {
  const parse = s => s.split(/[\s,]+/).filter(Boolean).map(Number).filter(n => n >= 0 && n <= 51);
  mount.innerHTML =
    '<label>Your cards <input data-bj-you value="0 12"></label>' +
    '<label>Dealer up+hole <input data-bj-dealer value="0 10"></label>' +
    '<label>Bet <input data-bj-bet type="number" value="100"></label>' +
    '<label><input data-bj-dbl type="checkbox"> doubled</label>' +
    '<button data-bj-run>SETTLE</button><pre data-bj-out></pre>';
  mount.querySelector('[data-bj-run]').addEventListener('click', () => {
    const you = parse(mount.querySelector('[data-bj-you]').value);
    const up  = parse(mount.querySelector('[data-bj-dealer]').value);
    const bet = Number(mount.querySelector('[data-bj-bet]').value) || 0;
    const dbl = mount.querySelector('[data-bj-dbl]').checked;
    const dealer = dealerPlay(up, () => Math.floor(Math.random() * 52));
    const r = settleHand({ cards: you, bet, doubled: dbl }, dealer);
    mount.querySelector('[data-bj-out]').textContent =
      'you     ' + you.map(cardLabel).join(' ') + '  = ' + handTotal(you) +
      (isNatural(you) ? '  NATURAL' : '') + '\n' +
      'dealer  ' + dealer.map(cardLabel).join(' ') + '  = ' + handTotal(dealer) +
      (isNatural(dealer) ? '  NATURAL' : '') + '\n\n' +
      r.outcome.toUpperCase() + '  ' + (r.payout > 0 ? '+' : '') + r.payout;
  });
}
```

Mount it beside the existing slots lab panel.

- [ ] **Step 4: Verify**

Run: `npx serve .`, open `commissioner.html`, find the blackjack bench, and settle `0 12` against `0 10` (your `[A,K]` against a dealer `[A,J]`).
Expected: `PUSH  0` — natural against natural.

Then settle `12 8` (a 19) against `0 10`.
Expected: `LOSE  -100`, the original bet only.

- [ ] **Step 5: Commit**

```bash
npm run css && npm run stamp
git add cfb-genius.html js/sandbox.js css/tailwind.css
git commit -m "Chips at tiebreaker three, the house rules, and a bench to argue on"
```

---

## Self-Review

**Spec coverage.** Every section of the spec maps to a task: the cascade → Task 11; the felt's rules → Tasks 1, 5, 6, 11; the bankroll and bet limits → Tasks 1, 2, 5; the dealer natural / backwards peek → Tasks 1, 6, 11; the week and its lock → Task 4; the two dealer tables → Task 2; the hands table and the `unique` rule → Task 2; the derived bankroll → Task 2; no shoe → Tasks 1, 3; the five functions (now seven with `bj_my_live_hand` and `bj_my_team`) → Tasks 4–6; settlement modelled on `resolve_slot_picks()` → Task 6; the mandatory revokes → Tasks 3–6, verified in Task 7; RLS → Task 2, verified in Task 7; the pit on the tiebreaker board → Task 8; the chips column → Task 9; the reveal → Task 10; failure modes → exercised in Task 7; rollout by hand → Task 7.

**Two gaps closed while reviewing.** The spec names five functions; the plan adds `bj_my_team()` and `bj_my_live_hand()`, because three copies of "the hand you may still act on" is how `bj_hit` and `bj_double` end up disagreeing. And the spec says the felt shows the current week without saying what it shows between Saturday's settlement and Tuesday's lines lock — `loadPit()` falls back to the newest `blackjack_weeks` row so it holds last week's result rather than going blank.

**Known deviation from the spec's own test list.** The spec proposes extending `scratchpad/verify`. That directory does not exist in this repo; the actual pattern is a standalone `node tools/*.test.mjs` script (`tools/scoutModel.test.mjs`), so Task 1 follows that instead and the SQL half is pinned by a `do $$` assertion block inside the migration.

**Type consistency.** `handTotal`, `isNatural`, `isSoft`, `cardLabel`, `cardSuit`, `legalBets`, `settleHand`, `dealerPlay`, `TABLE` are defined in Task 1 and used under those exact names in Tasks 8, 10 and 11. `pitState()`/`loadPit()`/`revealHand()`/`forgetHands()` are defined in Tasks 8 and 10 and imported under those names in `index.html`. `bj_*` SQL names are consistent across Tasks 3–7. `state`, `outcome` and `payout` string values match the table's check constraints in Task 2.

