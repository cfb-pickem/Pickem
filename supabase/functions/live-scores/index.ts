// live-scores - the ten-second score feed for game windows.
//
// Fired once a minute by the `live-scores-edge` pg_cron job (through pg_net, so
// the job holds no connection). Answers at once and keeps working in the
// background: at :10 :20 :30 :40 :50 past the minute it fetches ESPN's
// scoreboard for the dates that have live games and hands each one to
// public.apply_espn_scoreboard(), the same parser sync_espn_scores() uses on
// the minute. See migrations/20261030230000_ten_second_scores.sql for why the
// waiting lives here rather than in Postgres.
//
// Failure is safe by construction: if this never runs, or throws on every pass,
// the on-the-minute sync_espn_scores() job still keeps the board where it was
// before this function existed.
//
// No secrets in this file. SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are
// provided to every edge function by the platform.

import { createClient } from "jsr:@supabase/supabase-js@2";

const ESPN =
  "https://site.api.espn.com/apis/site/v2/sports/football/college-football/scoreboard?limit=400&groups=80&dates=";
const OFFSETS_S = [10, 20, 30, 40, 50];
// ESPN answers a bare Deno fetch from the edge with 403. The same request with
// ordinary browser headers is served, as it is from Postgres's http_get.
const ESPN_HEADERS = {
  "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36",
  "Accept": "application/json, text/plain, */*",
  "Accept-Language": "en-US,en;q=0.9",
};

const sb = createClient(
  Deno.env.get("SUPABASE_URL")!,
  Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
  { auth: { persistSession: false } },
);

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

// Only what apply_espn_scoreboard() reads. A Saturday scoreboard is a few MB of
// JSON; this keeps each RPC body to a few tens of KB.
// deno-lint-ignore no-explicit-any
function trim(body: any) {
  // deno-lint-ignore no-explicit-any
  const events = (body?.events ?? []).map((ev: any) => {
    const c = ev?.competitions?.[0] ?? {};
    return {
      id: ev?.id,
      link: ev?.link,
      // deno-lint-ignore no-explicit-any
      links: (ev?.links ?? []).map((l: any) => ({ rel: l?.rel, href: l?.href })),
      status: {
        period: ev?.status?.period,
        displayClock: ev?.status?.displayClock,
        type: { state: ev?.status?.type?.state, name: ev?.status?.type?.name },
      },
      competitions: [{
        // deno-lint-ignore no-explicit-any
        competitors: (c.competitors ?? []).map((s: any) => ({
          homeAway: s?.homeAway,
          score: s?.score,
          id: s?.id,
        })),
        situation: c.situation
          ? {
            isRedZone: c.situation.isRedZone,
            lastPlay: {
              team: { id: c.situation.lastPlay?.team?.id },
              end: { team: { id: c.situation.lastPlay?.end?.team?.id } },
            },
          }
          : undefined,
        geoBroadcasts: c.geoBroadcasts,
        broadcasts: c.broadcasts,
      }],
    };
  });
  return { events };
}

async function pass(dates: string[], verbose = false) {
  let checked = 0, updated = 0;
  const errors: string[] = [];
  for (const d of dates) {
    try {
      const r = await fetch(ESPN + d, { headers: ESPN_HEADERS, signal: AbortSignal.timeout(6000) });
      if (!r.ok) { errors.push(d + ": espn " + r.status); continue; }
      const body = trim(await r.json());
      const { data, error } = await sb.rpc("apply_espn_scoreboard", { body });
      if (error) {
        console.error("apply failed", d, error.message);
        errors.push(d + ": apply: " + error.message);
        continue;
      }
      const row = Array.isArray(data) ? data[0] : data;
      checked += row?.checked ?? 0;
      updated += row?.updated ?? 0;
    } catch (e) {
      console.error("pass failed", d, String(e));
      errors.push(d + ": " + String(e));
    }
  }
  return verbose || errors.length ? { checked, updated, errors } : { checked, updated };
}

async function run() {
  const minute = Math.floor(Date.now() / 60000) * 60000;

  const claim = await sb.rpc("live_feed_claim");
  if (claim.error || claim.data !== true) {
    return { skipped: claim.error?.message ?? "already running this minute" };
  }

  const dates = await sb.rpc("live_feed_dates");
  if (dates.error) return { error: dates.error.message };
  const list: string[] = dates.data ?? [];
  if (!list.length) return { idle: true };

  const results = [];
  for (const off of OFFSETS_S) {
    const wait = minute + off * 1000 - Date.now();
    if (wait < -4000) continue; // started late; that slot is gone
    if (wait > 0) await sleep(wait);
    results.push({ at: off, ...(await pass(list)) });
  }
  return { dates: list, results };
}

Deno.serve(async (req) => {
  // Diagnostic: one pass, answered synchronously, skipping the claim. Only for
  // the service role, so the public endpoint cannot be used to drive passes.
  const url = new URL(req.url);
  if (url.searchParams.has('debug')) {
    const auth = req.headers.get('Authorization') ?? '';
    if (auth !== 'Bearer ' + Deno.env.get('SUPABASE_SERVICE_ROLE_KEY')) {
      return new Response('forbidden', { status: 403 });
    }
    const dates = await sb.rpc('live_feed_dates');
    const list: string[] = dates.data ?? [];
    const out = { dates: list, datesError: dates.error?.message, pass: list.length ? await pass(list, true) : null };
    return new Response(JSON.stringify(out), { headers: { 'Content-Type': 'application/json' } });
  }

  const work = run()
    .then((r) => console.log(JSON.stringify(r)))
    .catch((e) => console.error("run failed", String(e)));
  // @ts-ignore EdgeRuntime is provided by the Supabase runtime
  EdgeRuntime.waitUntil(work);
  return new Response("accepted", { status: 202 });
});
