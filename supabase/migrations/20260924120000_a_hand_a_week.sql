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
