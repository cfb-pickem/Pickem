-- The table opens with the picks.
--
-- Until now bj_open_week() waited for the LINES to lock at 11:00 Tuesday
-- (line_lock_at()), on the theory that that is the moment a week becomes real.
-- For the league it is not. The week becomes real when the commissioner ticks
-- `picked` on its games, because that is the moment picks.html starts showing
-- it and people start making picks - usually Sunday, two days before Tuesday.
-- Sitting down to pick your games and finding the felt still dark until
-- Tuesday reads as broken, and nothing about blackjack needs a frozen line:
-- a hand is not attached to a game, let alone to a spread.
--
-- So: A WEEK'S TABLE OPENS THE FIRST CRON MINUTE ITS PICKS ARE OPEN. "Picks
-- are open" is the picks page's own definition, mirrored rather than
-- reinvented:
--   * the week has at least one game with picked = true (picks.html selects the
--     week to show with exactly `.eq('picked', true)`), and
--   * nothing in it has kicked off yet - bj_week_open(), which already applies
--     picks_open_for_game()'s regular-season week gate.
--
-- WHAT DOES NOT CHANGE, and must not:
--   * Both dealer cards are still drawn HERE, by cron, at open, into
--     blackjack_hole - never by a signed-in request and never at settlement.
--     Opening earlier only means they are sealed for longer.
--   * You still bet blind. bj_dealer_up() is untouched.
--   * One hand a week is still the unique constraint, not this file.
--   * bj_live_week() is untouched: the EARLIEST open week, and nothing once a
--     week has closed. That ordering is what keeps two dealt weeks from being
--     confused - see "TWO WEEKS DEALT AT ONCE" below.
--   * A normal week still settles at its first kickoff, in the same minute it
--     did before.
--
-- TIEBREAKER-ONLY WEEKS KEEP THE TUESDAY GATE. A week whose only games are
-- `tiebreaker` rows (2025's week 16, Army-Navy, was one) is not a week picks.html
-- ever shows on its own, and tiebreaker flags are the kind of thing that can be
-- set long before the week - a far-future week dealt in October is a week
-- bj_live_week() would hand out in every gap between real weeks. For those the
-- old rule stands, so this migration can only ever open a table EARLIER than
-- before, never later.
--
-- TWO WEEKS DEALT AT ONCE is now an ordinary state rather than an impossible
-- one: a commissioner who sets week N+1 before week N kicks off (or sets every
-- bowl week in one sitting) deals both. That is fine and needs no new code.
-- Both pairs are sealed; bj_live_week() returns week N - the earliest open one
-- - so that is the only week bj_deal() and friends can touch; N settles at its
-- kickoff exactly as before, and from that minute bj_live_week() moves on to
-- N+1, whose dealer has been waiting face down. A previous week that has kicked
-- off is never "open", so a Saturday of week-N games still finishing can never
-- be mistaken for the live table either.

-- 1. OPEN WHEN THE PICKS OPEN ----------------------------------------------

create or replace function public.bj_open_week()
returns integer language plpgsql security definer set search_path = public, pg_temp as $$
declare
  s integer; w integer; first_kick timestamp; has_picked boolean; n integer := 0;
begin
  for s, w, first_kick, has_picked in
    select a.cfb_season, a.week,
           min((a."Start (CT)")::timestamp),
           bool_or(coalesce(a.picked, false))
      from public.all_games a
     where (coalesce(a.picked, false) or coalesce(a.tiebreaker, false))
       and a."Start (CT)" is not null
     group by a.cfb_season, a.week
  loop
    -- Picked games mean the picks page is showing this week: open now. Only a
    -- week with nothing but tiebreakers waits for Tuesday's line lock.
    continue when not has_picked
              and (now() at time zone 'America/Chicago') < public.line_lock_at(first_kick);
    continue when not public.bj_week_open(s, w);

    insert into public.blackjack_weeks (cfb_season, week)
    values (s, w)
    on conflict (cfb_season, week) do nothing;

    if found then n := n + 1; end if;

    -- OUTSIDE the `if found`, always - unchanged from 20261007. A
    -- blackjack_weeks row without its sealed mate is invisible to the settler
    -- for ever. Both cards in one statement, drawn by cron and nobody else.
    insert into public.blackjack_hole (cfb_season, week, up, hole)
    values (s, w, public.bj_draw_card(), public.bj_draw_card())
    on conflict (cfb_season, week) do nothing;
  end loop;
  return n;
end;
$$;

revoke execute on function public.bj_open_week() from public, anon, authenticated;

-- 2. SETTLE AT KICKOFF, NOT AT "NOT OPEN" ----------------------------------

-- WHY THE SETTLER HAS TO CHANGE TOO. It used to settle any dealt week that was
-- not open, and "not open" has two causes: the week kicked off, or the week no
-- longer has any picked games. While the table only opened on Tuesday the
-- second cause was a corner. Now that it opens the minute a box is ticked, it
-- is a commissioner's slip away: tick a game in the wrong week, save, notice,
-- untick. Under the old rule that one minute was enough to deal the wrong week
-- AND settle it the next minute - publishing its dealer's hand as
-- dealer_final and closing that week's table for good, so when its games were
-- picked for real a week later bj_deal() would answer "the table for this week
-- has closed" to the whole league.
--
-- So a week settles only once something in it has actually KICKED OFF. For
-- every real week that is the same minute as before (bj_week_open() goes false
-- at exactly the first kickoff). A week that loses its picked games instead
-- simply stays dealt and sealed, and resumes as the same table - same two
-- cards, same hands - whenever its games are picked again. A hand placed in it
-- while it was live waits with it, unsettled and therefore uncounted by
-- blackjack_bankrolls, rather than being paid out against a dealer hand flipped
-- for no reason; re-picking the week is the commissioner's way to finish it.
--
-- Everything else is the 20261007 settler verbatim: the sealed-row cursor, the
-- for-update lock, the dealer-natural "original bets only" arm, the 3:2 floor,
-- the idempotence guards.
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
       -- CLOSED BY A KICKOFF, not merely by having no picked games left.
       and exists (
             select 1 from public.all_games x
              where x.cfb_season = bw.cfb_season and x.week = bw.week
                and (coalesce(x.picked, false) or coalesce(x.tiebreaker, false))
                and x."Start (CT)" is not null
                and (x."Start (CT)")::timestamp <= (now() at time zone 'America/Chicago')
           )
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
        -- THE PEEK, APPLIED BACKWARDS. A sealed hole card cannot peek, so the
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

    update public.blackjack_weeks
       set dealer_final = dealer, settled_at = now()
     where cfb_season = w.cfb_season and week = w.week and settled_at is null;
  end loop;
  return n;
end;
$$;

comment on function public.bj_settle_week() is
  'Settle every week that has kicked off: flip the sealed pair, draw to 17, pay the table. A dealt week that merely lost its picked games stays sealed. A dealer natural voids the week (original bets only). Idempotent and one-way.';

revoke execute on function public.bj_settle_week() from public, anon;
grant  execute on function public.bj_settle_week() to authenticated;

-- 3. PROVE THE DOOR IS STILL SHUT ------------------------------------------

-- Opening earlier makes the cron-only rule matter for longer, not less: the
-- pair drawn here now sits sealed for five or six days instead of four. A
-- `create or replace` keeps existing grants, but a grant that drifted in some
-- other way is exactly the kind of thing this project would rather fail a
-- migration over than find out about from a suspiciously lucky Wednesday.
do $$
begin
  if has_function_privilege('anon', 'public.bj_open_week()', 'execute')
     or has_function_privilege('authenticated', 'public.bj_open_week()', 'execute') then
    raise exception 'bj_open_week() is callable by a client - the sealed cards must only ever be drawn by cron';
  end if;
  if has_function_privilege('anon', 'public.bj_settle_week()', 'execute') then
    raise exception 'bj_settle_week() is callable by anon';
  end if;
end $$;
