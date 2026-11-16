// Crafted-request tests for the playoff pick gate. Run by run.sh against a
// throwaway Postgres + PostgREST holding a read-only copy of prod's teams,
// seeds and games. Every request is what picks.html (supabase-js upsert)
// sends, signed as a real member, so the only thing between a member and the
// table is the database.
import { execFileSync } from 'node:child_process';
import { createHmac } from 'node:crypto';

const { PSQL, MIGRATION, JWT_SECRET, API } = process.env;
const [psqlBin, ...psqlArgs] = PSQL.split(' ');
const sql = q => execFileSync(psqlBin, [...psqlArgs, '-c', q], { encoding: 'utf8' }).trim();
const row = q => { const r = sql(q); return r ? r.split('|') : null; };

const b64 = o => Buffer.from(JSON.stringify(o)).toString('base64url');
function jwt(sub) {
  const head = b64({ alg: 'HS256', typ: 'JWT' });
  const body = b64({ sub, role: 'authenticated', aud: 'authenticated', exp: Math.floor(Date.now() / 1000) + 3600 });
  const sig = createHmac('sha256', JWT_SECRET).update(`${head}.${body}`).digest('base64url');
  return `${head}.${body}.${sig}`;
}

async function upsert(who, rows) {
  const res = await fetch(`${API}/picks?on_conflict=team_id,game_id`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Prefer: 'resolution=merge-duplicates,return=minimal',
               ...(who ? { Authorization: `Bearer ${jwt(who.user_id)}` } : {}) },
    body: JSON.stringify(rows),
  });
  return { status: res.status, body: res.status >= 300 ? await res.json().catch(() => ({})) : null };
}
async function get(who, path) {
  const res = await fetch(`${API}/${path}`, { headers: who ? { Authorization: `Bearer ${jwt(who.user_id)}` } : {} });
  return { status: res.status, rows: await res.json() };
}
const pickRow = (team, g) => ({ team_id: team.team_id, game_id: g.id, pick: g.home, by_slots: false, week: g.week });

let pass = 0, fail = 0;
function check(name, ok, detail = '') {
  if (ok) pass++; else fail++;
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? `  (${detail})` : ''}`);
}
const ok2xx = r => r.status >= 200 && r.status < 300;
const gated = r => r.status >= 400 && /Only the four playoff teams/.test(r.body?.message || '');

// ---- Fixture: real 2025 seeds and members; kickoffs moved so the deadline isn't what says no.
const seeds25 = sql(`select team_id from playoffs where season = 2025 order by seed`).split('\n').map(Number);
const member = where => {
  const r = row(`select team_id, team_name, user_id from teams where user_id is not null and not is_exhibition and ${where} order by team_id limit 1`);
  return r && { team_id: Number(r[0]), team_name: r[1], user_id: r[2] };
};
const P = member(`team_id in (select team_id from playoffs where season = 2025) and not coalesce(commissioner, false)`);
const N = member(`active and team_id not in (select team_id from playoffs where season = 2025) and not coalesce(commissioner, false)`);
const C = member(`commissioner`);
if (seeds25.length !== 4 || !P || !N || !C) throw new Error('fixture: need 2025 seeds, a seeded member, a non-seeded member and the commissioner');
console.log(`2025 seeds ${seeds25.join(',')}; playoff member "${P.team_name}", non-playoff member "${N.team_name}", commissioner "${C.team_name}"`);

// The last 2025 playoff game stays in the past with its real picks (to check eliminated members can read them);
// every other 2025 week 16+ game, and all of 2025 week 15, moves into next week.
const lastPo = Number(sql(`select "GameId" from all_games where cfb_season = 2025 and week > 15 order by "Start (CT)" desc limit 1`));
sql(`update all_games set "Start (CT)" = (now() at time zone 'America/Chicago') + interval '7 days' + (row_number_hack.n * interval '1 hour')
       from (select "GameId" gid, row_number() over (order by "Start (CT)") n from all_games
              where cfb_season = 2025 and (week = 15 or week > 15) and "GameId" <> ${lastPo}) row_number_hack
      where "GameId" = row_number_hack.gid`);
const game = where => {
  const r = row(`select "GameId", "Home", week from all_games where ${where} order by "Start (CT)" limit 1`);
  return r && { id: Number(r[0]), home: r[1], week: Number(r[2]) };
};
const po25 = game(`cfb_season = 2025 and week > 15 and not coalesce(tiebreaker, false) and "GameId" <> ${lastPo}`);
const po25b = game(`cfb_season = 2025 and week > 15 and not coalesce(tiebreaker, false) and "GameId" <> ${lastPo} and "GameId" <> ${po25.id}`);
const tb25 = game(`cfb_season = 2025 and week > 15 and tiebreaker`);
const w15 = game(`cfb_season = 2025 and week = 15 and picked`);

// 2026: no seeds yet. The newest regular week moves into next week too, plus a made-up week 16 game.
const wk26 = Number(sql(`select max(week) from all_games where cfb_season = 2026 and picked and week <= 15`));
sql(`update all_games set "Start (CT)" = (now() at time zone 'America/Chicago') + interval '5 days' where cfb_season = 2026 and week = ${wk26}`);
sql(`insert into all_games ("GameId","Start (CT)","Away","Home","Status",picked,week,cfb_season,tiebreaker)
     values (990000016, (now() at time zone 'America/Chicago') + interval '30 days', 'Away U', 'Home St', 'scheduled', true, 16, 2026, false)`);
const reg26 = game(`cfb_season = 2026 and week = ${wk26} and picked`);
const po26 = game(`"GameId" = 990000016`);

// ---- Before the migration: prod as it was this morning.
let r = await upsert(N, [pickRow(N, po25)]);
check('BEFORE: non-playoff member CAN write a playoff pick (the hole being closed)', ok2xx(r), `HTTP ${r.status}`);
sql(`delete from picks where team_id = ${N.team_id} and game_id = ${po25.id}`);

execFileSync(psqlBin, [...psqlArgs, '-f', MIGRATION]);
console.log('migration applied');

// ---- Playoffs on (2025 has seeds)
r = await upsert(P, [pickRow(P, po25)]);
check('playoff member saves a playoff pick', ok2xx(r), `HTTP ${r.status}`);
r = await upsert(P, [{ ...pickRow(P, po25), pick: sql(`select "Away" from all_games where "GameId" = ${po25.id}`) }]);
check('playoff member changes that pick', ok2xx(r), `HTTP ${r.status}`);
r = await upsert(P, [pickRow(P, tb25)]);
check('playoff member saves the week 16 tiebreaker', ok2xx(r), `HTTP ${r.status}`);

r = await upsert(N, [pickRow(N, po25)]);
check('non-playoff member is refused (crafted upsert)', gated(r), `HTTP ${r.status} ${r.body?.message}`);
r = await upsert(N, [pickRow(N, po25), pickRow(N, po25b)]);
check('non-playoff member is refused (batch of two)', gated(r), `HTTP ${r.status}`);
r = await upsert(N, [{ team_id: N.team_id, game_id: po25.id, pick: null, by_slots: true, week: po25.week }]);
check('non-playoff member is refused (slots pick)', gated(r), `HTTP ${r.status}`);
r = await upsert(N, [pickRow(N, tb25)]);
check('non-playoff member is refused (week 16 tiebreaker)', gated(r), `HTTP ${r.status}`);
r = await fetch(`${API}/picks?team_id=eq.${P.team_id}&game_id=eq.${po25.id}`, {
  method: 'PATCH', headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${jwt(N.user_id)}`, Prefer: 'return=representation' },
  body: JSON.stringify({ team_id: N.team_id }) });
const stolen = await r.json();
check('non-playoff member cannot take over a playoff team\'s row (PATCH team_id)', !(Array.isArray(stolen) && stolen.length), `HTTP ${r.status}`);
r = await upsert(null, [pickRow(N, po25)]);
check('signed-out request is refused', r.status >= 400, `HTTP ${r.status}`);
check('nothing from the non-playoff member landed',
  sql(`select count(*) from picks p join all_games g on g."GameId" = p.game_id where p.team_id = ${N.team_id} and g.cfb_season = 2025 and g.week > 15`) === '0');

r = await upsert(C, [pickRow(N, po25)]);
check('commissioner override: commissioner enters a pick for a non-playoff team', ok2xx(r), `HTTP ${r.status}`);
sql(`delete from picks where team_id = ${N.team_id} and game_id = ${po25.id}`);

let g = await get(N, `all_games?select=GameId&cfb_season=eq.2025&week=gt.15`);
check('non-playoff member still sees every playoff game', g.status === 200 && g.rows.length > 1, `${g.rows.length} games`);
g = await get(N, `picks?select=team_id,pick&game_id=eq.${lastPo}`);
const realPicks = Number(sql(`select count(*) from picks where game_id = ${lastPo}`));
check('non-playoff member still sees the playoff teams\' picks once a game locks', g.status === 200 && g.rows.length === realPicks && realPicks > 0, `${g.rows.length}/${realPicks}`);

r = await upsert(N, [pickRow(N, w15)]);
check('seeded season, regular week 15: non-playoff member can still pick', ok2xx(r), `HTTP ${r.status}`);

// ---- 2026: regular season, no seeds
r = await upsert(N, [pickRow(N, reg26)]);
check(`2026 week ${wk26} (no seeds): non-playoff member picks as before`, ok2xx(r), `HTTP ${r.status}`);
r = await upsert(P, [pickRow(P, reg26)]);
check(`2026 week ${wk26} (no seeds): another member picks as before`, ok2xx(r), `HTTP ${r.status}`);
r = await upsert(N, [pickRow(N, po26)]);
check('2026 week 16 before any seeds: nobody is gated yet', ok2xx(r), `HTTP ${r.status}`);

// The switch flips (as the autopilot would write it): four seeds that don't include N.
sql(`insert into playoffs (team_id, season, seed, source)
     select team_id, 2026, row_number() over (order by team_id), 'auto' from teams
      where user_id is not null and not is_exhibition and active and team_id <> ${N.team_id} order by team_id limit 4`);
const in26 = member(`team_id in (select team_id from playoffs where season = 2026) and not coalesce(commissioner, false)`);
r = await upsert(N, [{ ...pickRow(N, po26), pick: 'Away U' }]);
check('2026 seeds written: non-playoff member can no longer change their week 16 pick', gated(r), `HTTP ${r.status}`);
r = await upsert(in26, [pickRow(in26, po26)]);
check('2026 seeds written: a 2026 playoff member picks week 16', ok2xx(r), `HTTP ${r.status}`);
r = await upsert(N, [{ ...pickRow(N, reg26), pick: sql(`select "Away" from all_games where "GameId" = ${reg26.id}`) }]);
check(`2026 seeds written: week ${wk26} regular-season pick still editable for everyone`, ok2xx(r), `HTTP ${r.status}`);

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
