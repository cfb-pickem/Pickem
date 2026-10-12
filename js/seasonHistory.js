// js/seasonHistory.js — every season this league has played, in one table.
//
// Two sources, deliberately kept apart until the last moment:
//
//   2022-2024   js/leagueHistory.js, transcribed from the Splash Sports weekly
//               results grid. Wins per week only - the picks themselves are gone.
//   2025 on     computed live from Supabase, the same way the leaderboard does.
//
// The live seasons are counted over weeks 1-15 only, because that is all the
// Splash grid covers. Comparing a 15-week total against one that quietly
// included six playoff weeks would flatter the recent years for no reason.

import { escapeHtml, sameTeam } from './utils.js';
import { LEAGUE_HISTORY, HISTORY_SEASONS, historyFor } from './leagueHistory.js';

// PostgREST hands back at most 1000 rows per request, silently. The picks table
// passed that in 2026, and because the rows come back oldest-first the newest
// season was the one cut off - its column read 0-5 wins when the real numbers
// were 9-14. Anything that can grow past a page goes through here. `build`
// must return a fresh query with a stable .order() so the pages don't overlap.
export async function selectAll(build, pageSize = 1000) {
  const out = [];
  for (let from = 0; ; from += pageSize) {
    const { data, error } = await build().range(from, from + pageSize - 1);
    if (error) return { data: null, error };
    out.push(...(data || []));
    if (!data || data.length < pageSize) return { data: out, error: null };
  }
}

// The leaderboard's rule, verbatim: a pick scores when its game has a winner
// and either the pick matches it or the slots paid out a jackpot.
export function isWin(p, winner) {
  return winner != null && (!!p.jackpot || sameTeam(p.pick, winner));
}

export async function renderSeasonHistory(mount, supabase) {
  if (!mount) return;

  const [{ data: games }, { data: picks }, { data: teams }] = await Promise.all([
    selectAll(() => supabase.from('all_games').select('GameId, week, winner, cfb_season').eq('picked', true).order('GameId')),
    selectAll(() => supabase.from('picks').select('id, team_id, game_id, pick, jackpot').order('id')),
    supabase.from('teams').select('team_id, team_name, active')
  ]);
  if (!games || !picks || !teams) return;

  const byId = new Map(games.map(g => [g.GameId, g]));
  const live = {};                                  // season -> team_id -> wins
  const liveSeasons = new Set();
  for (const p of picks) {
    const g = byId.get(p.game_id);
    if (!g || g.winner == null) continue;
    const wk = Number(g.week);
    if (!(wk >= 1 && wk <= 15)) continue;            // match the Splash grid
    const s = Number(g.cfb_season);
    liveSeasons.add(s);
    live[s] = live[s] || {};
    live[s][p.team_id] = live[s][p.team_id] || 0;
    if (isWin(p, g.winner)) live[s][p.team_id]++;
  }

  const seasons = [...new Set([...liveSeasons, ...HISTORY_SEASONS])].sort((a, b) => b - a);

  // Anyone still in the league, plus anyone who appears in the old grids under
  // a name that still maps to a team - which is the same rule the rest of the
  // site uses for "is this person one of ours".
  const rows = teams
    .filter(t => t.active !== false || seasons.some(s => live[s] && live[s][t.team_id] != null))
    .map(t => {
      const cells = seasons.map(s => {
        if (live[s] && live[s][t.team_id] != null) return { season: s, wins: live[s][t.team_id], live: true };
        const h = historyFor(t.team_name, s);
        return h ? { season: s, wins: h.total, live: false } : { season: s, wins: null };
      });
      const played = cells.filter(c => c.wins != null);
      return {
        name: t.team_name,
        cells,
        career: played.reduce((a, c) => a + c.wins, 0),
        seasonsPlayed: played.length
      };
    })
    .filter(r => r.seasonsPlayed > 0)
    .sort((a, b) => b.career - a.career || a.name.localeCompare(b.name));

  if (!rows.length) { mount.hidden = true; return; }

  const head = seasons.map(s => `<th class="px-3 py-2 text-center font-semibold">${s}</th>`).join('');
  const body = rows.map(r => `
    <tr class="border-t border-[rgba(231,231,231,.06)]">
      <td class="px-4 py-2 hist-name">${escapeHtml(r.name)}</td>
      ${r.cells.map(c => c.wins == null
        ? '<td class="px-3 py-2 text-center hist-none">&mdash;</td>'
        : `<td class="px-3 py-2 text-center hist-win${c.live ? '' : ' is-archive'}">${c.wins}</td>`).join('')}
      <td class="px-3 py-2 text-center hist-career">${r.career}</td>
      <td class="px-3 py-2 text-center hist-seasons">${r.seasonsPlayed}</td>
    </tr>`).join('');

  mount.hidden = false;
  mount.innerHTML = `
    <div class="cfp-card gold-shadow">
      <div class="stat-head px-4 md:px-6 py-3">
        <span class="trophy-oval" aria-hidden="true"></span>
        <div><h2 class="stat-title">Season by season</h2></div>
      </div>
      <div class="overflow-auto">
        <table class="table min-w-full text-sm">
          <thead>
            <tr class="bg-[var(--cfp-black)] text-[var(--cfp-ivory)]">
              <th class="px-4 py-2 text-left font-semibold">Team</th>
              ${head}
              <th class="px-3 py-2 text-center font-semibold">Career</th>
              <th class="px-3 py-2 text-center font-semibold">Seasons</th>
            </tr>
          </thead>
          <tbody>${body}</tbody>
        </table>
      </div>
      <p class="hist-foot px-4 md:px-6 pb-4">
        Wins across weeks 1&ndash;15, so every season is counted the same way.
        ${HISTORY_SEASONS.join(', ')} are carried over from Splash Sports and show
        <span class="hist-win is-archive">in gold</span>; those years recorded results only, not the
        picks behind them, so they cannot feed the pick model.
      </p>
    </div>`;
}
