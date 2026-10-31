# Edge Functions

The first three ran in production for months without being in version control. They
were downloaded off the project with `supabase functions download <slug>` and
committed as they were deployed, so this is a record of what is actually
running, not a reconstruction of what it should be.

That gap was not harmless. `update-final-scores` began returning 502 on every
call on the morning of the 2026 opener, and the replacement written for it
quietly dropped three of the columns it owned — a fortnight passed before anyone
noticed the ESPN links had stopped appearing. Nobody could diff it, because
there was nothing to diff against.

**No secrets live in these files.** Every key is read with `Deno.env.get`, and
that is the rule: the repo is public and GitHub Pages serves it.

## What each one is

| Function | Cron job | State | Does |
|---|---|---|---|
| `dynamic-worker` (deployed as `sync-games`) | `Update from CFB API`, every 2h | active | Pulls the week's schedule from CollegeFootballData — start time, teams — and upserts it into `all_games`. |
| `update-final-scores` | `Update from ESPN`, every minute | **retired** | Used to own the live state. Kept deployed but its cron job is deactivated; see below. |
| `debug-espn-game` | none | on demand | Dumps a simplified view of ESPN's scoreboard. Diagnostic only, reads nothing from the database. |
| `live-scores` | `live-scores-edge`, every minute | active | The 10-second score feed for game windows. Fetches ESPN at :10-:50 and writes through `apply_espn_scoreboard()`. `sync_espn_scores()` on the minute stays the floor. |

## `update-final-scores` is retired, not deleted

It 502'd on every call for six straight hours on the morning of the opener,
taking the score and the clock off the board mid-game. `sync_espn_scores()` now
does that work from inside Postgres — see
`migrations/20260829204500_retire_dead_score_feed.sql`.

Its cron row was **deactivated rather than unscheduled**, because that row holds
a service_role JWT in its command and dropping it would mean retyping the key to
restore the job. To bring it back, if the function is ever fixed:

```sql
select cron.alter_job(1, active := true);
```

Do not do that casually. It is the feed that failed.

## A trap in `dynamic-worker`

It can write `Network`, `espn_url`, `Status`, `Period`, `Clock` and
`FinalizedAt` — but **only when called with `?overlayEspn=true`**. Without that
parameter `ownedFields` is the CFBD set (`Start (CT)`, `Away`, `Home`) and the
ESPN overlay never runs at all. The cron calls it with no query string, so in
production it only ever syncs the schedule.

This is why it did not paper over the missing links when the other feed was
retired, and it is worth knowing before assuming this function covers a column.

## Environment

Set on the project, never in this repo:

- `CFBD_API_KEY` — CollegeFootballData, `dynamic-worker` only
- `SUPABASE_URL`
- `SUPABASE_SERVICE_ROLE_KEY`

## Working on them

```bash
supabase functions download <slug>    # pull what is deployed
supabase functions deploy <slug>      # push a change
```

Downloads land under the directory the CLI treats as the project root, which is
not necessarily this repo — check where they went before copying them in.

## `live-scores`

Written in the repo, unlike the three above. See
`migrations/20261030230000_ten_second_scores.sql` for the design. Two things
worth knowing:

- ESPN answers a bare Deno `fetch` from the edge with **403**. It is served with
  ordinary browser headers (`ESPN_HEADERS`). If the fast feed ever goes quiet,
  check that first.
- `?debug=1` with the service-role key as the bearer runs one pass synchronously
  and returns what happened, errors included. Anything else gets 403 for that
  mode; the normal call is claim-guarded to one run a minute.

Deploy with `npx supabase functions deploy live-scores --use-api --workdir . --project-ref vopdioszofwdkwnujtiq`
(the `--workdir` matters: without it the CLI resolves paths from the home
directory).
