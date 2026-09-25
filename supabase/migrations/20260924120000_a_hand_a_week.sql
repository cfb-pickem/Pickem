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
  if public.bj_total(array[12,4,5]::smallint[])    <> 21 then raise exception '[K,5,6] should be 21'; end if;
  if public.bj_total(array[12,12,1]::smallint[])   <> 22 then raise exception '[K,K,2] should be 22'; end if;
  if public.bj_total('{}'::smallint[])             <> 0  then raise exception 'empty hand should be 0'; end if;
  if not public.bj_is_natural(array[0,10]::smallint[])    then raise exception '[A,J] should be a natural'; end if;
  if     public.bj_is_natural(array[0,4,5]::smallint[])   then raise exception '[A,5,6] should not be a natural'; end if;
end $$;

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
-- NOT named `cards`: a local with a column's name makes `set cards = cards`
  -- ambiguous and plpgsql refuses to run it.
  declare h public.blackjack_hands; new_cards smallint[]; out_row public.blackjack_hands;
begin
  h := public.bj_my_live_hand();
  new_cards := h.cards || public.bj_draw_card();
  update public.blackjack_hands
     set cards = new_cards,
         state = case when public.bj_total(new_cards) > 21 then 'busted' else 'live' end
   where id = h.id
  returning * into out_row;
  return out_row;
end;
$$;

create or replace function public.bj_double()
returns public.blackjack_hands
language plpgsql security definer set search_path = public as $$
declare h public.blackjack_hands; chips integer; new_cards smallint[];
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
  new_cards := h.cards || public.bj_draw_card();
  update public.blackjack_hands
     set cards = new_cards, doubled = true,
         state = case when public.bj_total(new_cards) > 21 then 'busted' else 'stood' end
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

-- 9. CRON -----------------------------------------------------------------

-- Every minute, for the same reason resolve_slot_picks() runs every minute: the
-- leaderboard reveals at kickoff and an unsettled table renders as a blank
-- felt. One job does both halves so they cannot get out of step.
select cron.unschedule('blackjack-table')
 where exists (select 1 from cron.job where jobname = 'blackjack-table');

select cron.schedule('blackjack-table', '* * * * *',
                     $$select public.bj_open_week(); select public.bj_settle_week();$$);
