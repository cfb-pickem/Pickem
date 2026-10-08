-- You bet before you see the dealer's card.
--
-- This shipped backwards. bj_open_week() wrote dealer_up into the publicly
-- readable blackjack_weeks on Tuesday, so the whole league spent five days
-- looking at the dealer's up-card and THEN deciding whether to sit down.
--
-- WHY THAT IS NOT A SMALL THING. A dealer showing a 6 busts about four times in
-- ten; a dealer showing an ace is the worst spot in the game. Choosing your
-- weeks off the up-card is therefore not a flourish, it is a large edge - and
-- because there is no ante, sitting out costs nothing, so "only play the weak
-- up-cards" was not merely a good strategy but the OPTIMAL one. A game whose
-- best line is to not play most weeks is not a game.
--
-- It also made the hoarding problem worse rather than better. The league was
-- already relying on social pressure to stop somebody protecting an untouched
-- 1,000 (see the 3:2 decision in the design doc); handing that player a way to
-- cherry-pick only the favourable weeks gave them a way to hoard AND win.
--
-- SO BOTH DEALER CARDS ARE SEALED NOW. You wager blind, and the up-card is
-- revealed to you at the moment you commit, which is exactly what happens when
-- you push chips across a real felt.
--
-- WHAT DOES NOT CHANGE: once you hold a hand, you see the up-card and play
-- hit/stand/double against it exactly as before. This is only about what you
-- know when you decide to bet, and how much.
--
-- WEEK 6 IS LEFT ALONE ON PURPOSE. Two players had already bet against its
-- visible 8 of clubs when this was written. Re-dealing the dealer's cards now
-- would change a hand somebody had already committed real chips to, which is a
-- worse wrong than the one being fixed. Their cards stand. The up-card stops
-- being public immediately, which takes the edge away from everyone who had not
-- yet bet without altering a single committed wager.

-- 1. THE UP-CARD MOVES BEHIND THE WALL -------------------------------------

-- blackjack_hole is the table with no select policy and no grant - the one that
-- answers "permission denied" to anon and authenticated alike. It held one
-- secret; it holds two now, written in the same statement so they can never
-- disagree about which week they belong to.
alter table public.blackjack_hole
  add column if not exists up smallint check (up between 0 and 51);

-- Backfill from the public column so the settler has ONE place to look rather
-- than branching on which side of this migration a week was dealt.
update public.blackjack_hole bh
   set up = bw.dealer_up
  from public.blackjack_weeks bw
 where bw.cfb_season = bh.cfb_season
   and bw.week = bh.week
   and bh.up is null
   and bw.dealer_up is not null;

-- RETIRED, not dropped. Dropping a column the browser selects by name would
-- 400 every page still being served from a stale cache, and GitHub Pages serves
-- this site with max-age=600 - so a ten minute window where nobody can load the
-- leaderboard. Kept, nulled, and never written again: a cached script reads
-- null and draws a face-down card, which is precisely the new behaviour.
alter table public.blackjack_weeks alter column dealer_up drop not null;
update public.blackjack_weeks set dealer_up = null where dealer_up is not null;

comment on column public.blackjack_weeks.dealer_up is
  'RETIRED, always null. The up-card lived here and was world-readable until 2026-10-07, which let players see it before choosing whether to bet. It is now blackjack_hole.up, revealed per player by bj_dealer_up(). The column stays only so a browser running a cached script that still selects it gets a row back instead of a 400.';

comment on column public.blackjack_hole.up is
  'The dealer''s up-card, sealed until the player commits a bet. Public only after settlement, as dealer_final[1].';

-- 2. DEAL BOTH CARDS INTO THE DARK -----------------------------------------

create or replace function public.bj_open_week()
returns integer language plpgsql security definer set search_path = public, pg_temp as $$
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

    -- No dealer_up any more. The public row is now just "a table opened here":
    -- which week, when, and (after settlement) what the dealer finished with.
    insert into public.blackjack_weeks (cfb_season, week)
    values (s, w)
    on conflict (cfb_season, week) do nothing;

    if found then n := n + 1; end if;

    -- OUTSIDE the `if found`, always, idempotent via on conflict do nothing. A
    -- blackjack_weeks row without its sealed mate is invisible to the settler
    -- for ever, so this must not be gated on the insert above having fired.
    --
    -- BOTH CARDS IN ONE STATEMENT. They were two inserts when the up-card was
    -- public, which left a window where a week could hold one card and not the
    -- other; there is no reason to keep that window now they are both secret.
    insert into public.blackjack_hole (cfb_season, week, up, hole)
    values (s, w, public.bj_draw_card(), public.bj_draw_card())
    on conflict (cfb_season, week) do nothing;
  end loop;
  return n;
end;
$$;

revoke execute on function public.bj_open_week() from public, anon, authenticated;

-- 3. THE REVEAL, TO THE PLAYER WHO PAID FOR IT -----------------------------

-- The up-card is the reward for committing, not the information you commit on.
-- So it is readable exactly when you hold a hand in that week, and never before.
-- Takes the week explicitly rather than asking bj_live_week(), because the felt
-- also draws the week that has locked but not yet settled, and that week is no
-- longer "live" by any definition the rest of this feature uses.
create or replace function public.bj_dealer_up(p_season integer, p_week integer)
returns smallint
language sql stable security definer set search_path = public, pg_temp as $$
  select bh.up
    from public.blackjack_hole bh
   where bh.cfb_season = p_season
     and bh.week = p_week
     and exists (
       select 1 from public.blackjack_hands h
        where h.cfb_season = p_season
          and h.week = p_week
          and h.team_id = public.bj_my_team()
     );
$$;

comment on function public.bj_dealer_up(integer, integer) is
  'The dealer''s up-card for one week, but only to a player who has already bet in it. Null for everyone else - including a signed-out visitor, for whom bj_my_team() is null and the exists() cannot match.';

-- The revoke is the load-bearing line, not the grant: Postgres grants EXECUTE
-- to PUBLIC on every new function, and this project has shipped that bug once.
revoke execute on function public.bj_dealer_up(integer, integer) from public, anon;
grant  execute on function public.bj_dealer_up(integer, integer) to authenticated;

-- 4. THE SETTLER READS THE SEALED ROW --------------------------------------

-- Only two things change from the 20260924 version: the cursor takes the
-- up-card from blackjack_hole instead of blackjack_weeks, and it requires the
-- sealed row to actually hold one. Everything else - the for-update lock, the
-- dealer-natural "original bets only" arm, the 3:2 floor, the idempotence
-- guard - is unchanged and must stay that way.
create or replace function public.bj_settle_week()
returns integer language plpgsql security definer set search_path = public, pg_temp as $$
declare
  w record; h record;
  dealer smallint[]; d_total integer; d_natural boolean;
  p_total integer; p_natural boolean; stake integer; pay integer; res text;
  n integer := 0;
begin
  for w in
    select bw.cfb_season, bw.week, bh.up, bh.hole
      from public.blackjack_weeks bw
      join public.blackjack_hole  bh
        on bh.cfb_season = bw.cfb_season and bh.week = bw.week
     where bw.settled_at is null
       and bh.up is not null
       and not public.bj_week_open(bw.cfb_season, bw.week)
     order by bw.cfb_season, bw.week
       for update of bw
  loop
    dealer    := array[w.up, w.hole]::smallint[];
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
         for update
    loop
      p_total   := public.bj_total(h.cards);
      p_natural := public.bj_is_natural(h.cards);
      stake     := h.bet * (case when h.doubled then 2 else 1 end);

      if d_natural then
        -- THE PEEK, APPLIED BACKWARDS. A real dealer showing an ace or a ten
        -- looks and ends the hand on a natural, so nobody can double into a
        -- hand that was already over. A sealed hole card cannot peek, so the
        -- week's play is void instead and only the ORIGINAL bet was ever at
        -- risk: US "original bets only".
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

      update public.blackjack_hands
         set state = 'settled', outcome = res, payout = pay, settled_at = now()
       where id = h.id and settled_at is null;
      if found then n := n + 1; end if;
    end loop;

    -- dealer_final is the up-card made public at last, as element 1. Nothing
    -- else needs to publish it, which is why the retired column stays null.
    update public.blackjack_weeks
       set dealer_final = dealer, settled_at = now()
     where cfb_season = w.cfb_season and week = w.week and settled_at is null;
  end loop;
  return n;
end;
$$;

comment on function public.bj_settle_week() is
  'Settle every closed week: flip the sealed pair, draw to 17, pay the table. A dealer natural voids the week (original bets only). Idempotent and one-way.';

revoke execute on function public.bj_settle_week() from public, anon;
grant  execute on function public.bj_settle_week() to authenticated;

-- 5. PROVE IT, RATHER THAN ASSUME IT ---------------------------------------

do $$
declare leaked integer;
begin
  -- No week may still be publishing an up-card.
  select count(*) into leaked from public.blackjack_weeks where dealer_up is not null;
  if leaked > 0 then
    raise exception 'dealer_up is still set on % week(s) - the up-card is public', leaked;
  end if;

  -- Every week that has a sealed row must hold BOTH cards, or it can never
  -- settle and every hand in it strands unpaid.
  select count(*) into leaked from public.blackjack_hole where up is null or hole is null;
  if leaked > 0 then
    raise exception '% sealed row(s) are missing a card', leaked;
  end if;
end $$;
