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
