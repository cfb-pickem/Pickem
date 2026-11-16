// js/playoffAutopilot.js - the commissioner's panel for the playoff autopilot.
//
// The autopilot itself runs in Supabase (playoff_autopilot_tick, every 10 min;
// supabase/migrations/20261116120000_playoff_autopilot.sql). This panel shows
// what it sees and what it would do, and gives the commissioner the overrides:
// turn it on or pause it, switch now (force), and undo a switch. Every action
// goes through playoff_autopilot_admin(), which re-checks commissioner rights
// on the server and logs who did what.

import { escapeHtml } from './utils.js';

const STATE_LABEL = { regular: 'Regular season', holding: 'Waiting', playoffs: 'Playoffs on' };

function fmt(ts) {
  if (!ts) return '';
  return new Date(ts).toLocaleString('en-US', { timeZone: 'America/Chicago', month: 'short', day: 'numeric',
    hour: 'numeric', minute: '2-digit' }) + ' CT';
}

export function createAutopilotPanel({ supabase, mount, getSeason, onSeedsChanged, status }) {
  let busy = false;

  async function act(action, confirmText) {
    const season = getSeason();
    if (!Number.isFinite(season) || busy) return;
    if (confirmText && !window.confirm(confirmText)) return;
    busy = true; paintBusy();
    const { data, error } = await supabase.rpc('playoff_autopilot_admin', { p_season: season, p_action: action });
    busy = false;
    if (error) status(`Autopilot: ${error.message}`, true);
    else status(`Autopilot: ${data?.message || 'done'}`, data?.ok === false);
    await refresh();
    if (action === 'force' || action === 'undo' || action === 'resume') await onSeedsChanged?.();
  }

  function paintBusy() {
    mount.querySelectorAll('button').forEach(b => { b.disabled = busy || b.dataset.off === '1'; });
  }

  async function refresh() {
    const season = getSeason();
    if (!Number.isFinite(season)) { mount.innerHTML = ''; return; }
    const [rowRes, prevRes, logRes] = await Promise.all([
      supabase.from('playoff_autopilot').select('*').eq('season', season).maybeSingle(),
      supabase.rpc('playoff_compute', { p_season: season }),
      supabase.from('playoff_autopilot_log').select('at, actor, action, detail').eq('season', season)
        .order('at', { ascending: false }).limit(6),
    ]);
    if (prevRes.error) {
      mount.innerHTML = `<div class="px-4 py-3 text-sm text-gray-300">Playoff autopilot isn't installed on the database yet.</div>`;
      return;
    }
    render(rowRes.data || null, prevRes.data, logRes.data || []);
  }

  function render(row, prev, log) {
    const enabled = !!row?.enabled;
    const state = prev.already_seeded ? 'playoffs' : (row?.state || (prev.phase === 'regular' ? 'regular' : 'holding'));
    const holds = prev.holds || [];
    const q = prev.qualifiers || [];
    const cut = prev.cut_tie;
    const seededByAuto = row?.switched_by && state === 'playoffs';

    const holdHtml = holds.length
      ? `<ul class="list-disc list-inside space-y-1">${holds.map(h => `<li>${escapeHtml(h.message)}</li>`).join('')}</ul>`
      : (prev.already_seeded ? '<p>Seeds are set. Playoff mode is on across the site.</p>'
         : `<p class="text-[var(--cfp-gold-2)]">Ready. ${enabled ? 'It switches on the next check (every 10 minutes).' : 'It would switch now if it were on.'}</p>`);

    const rows = q.map(s => `
      <tr class="border-t border-[rgba(231,231,231,.08)]">
        <td class="px-4 py-2 font-semibold text-[var(--cfp-ivory)]">${s.seed}</td>
        <td class="px-3 py-2">${escapeHtml(s.team_name)}</td>
        <td class="px-3 py-2 text-center">${s.points}</td>
        <td class="px-3 py-2 text-gray-400 text-xs">${escapeHtml(s.decided_by === 'points' ? 'points' : s.decided_by_label || '')}</td>
      </tr>`).join('');

    const cutHtml = cut ? `<p class="mt-2 text-xs text-gray-300">Tie at 4th: ${escapeHtml(cut.teams.join(', '))} at ${cut.points}.
      ${cut.decided_by ? `Settled by ${escapeHtml(prev.qualifiers.find(x => x.seed === 4)?.decided_by_label || cut.decided_by)}.`
                       : cut.waiting_on ? 'Waiting on the next tiebreaker.' : 'Nothing breaks it: coin flip.'}</p>` : '';

    const logHtml = log.length ? `<ul class="space-y-0.5">${log.map(l =>
      `<li><span class="text-gray-500">${fmt(l.at)}</span> ${escapeHtml(l.actor)}: ${escapeHtml(l.action.replace('_', ' '))}</li>`).join('')}</ul>`
      : '<p class="text-gray-500">Nothing yet.</p>';

    const canForce = !prev.already_seeded;
    const canUndo = prev.already_seeded;

    mount.innerHTML = `
      <div class="px-4 py-3 border-b border-[rgba(231,231,231,.08)] text-sm text-gray-300 flex flex-wrap items-center gap-3 justify-between">
        <div class="flex items-center gap-3 flex-wrap">
          <strong class="text-[var(--cfp-ivory)]">Playoff Autopilot</strong>
          <span class="text-xs px-2 py-0.5 border ${enabled ? 'border-[var(--cfp-gold)] text-[var(--cfp-gold-2)]' : 'border-gray-600 text-gray-400'}">${enabled ? 'On' : 'Paused'}</span>
          <span class="text-xs text-gray-400">${STATE_LABEL[state] || state}${row?.checked_at ? ` &middot; checked ${fmt(row.checked_at)}` : ''}</span>
        </div>
        <div class="flex items-center gap-2 flex-wrap">
          <button data-act="${enabled ? 'pause' : 'resume'}" class="border border-[var(--cfp-gold)] text-[var(--cfp-gold-2)] px-3 py-1.5 text-sm hover:bg-[rgba(233,185,73,.08)] disabled:opacity-40">${enabled ? 'Pause' : 'Turn on'}</button>
          <button data-act="refresh" class="border border-gray-600 text-gray-200 px-3 py-1.5 text-sm hover:bg-white/5 disabled:opacity-40">Refresh</button>
          <button data-act="force" data-off="${canForce ? '0' : '1'}" class="bg-[var(--cfp-gold)] text-black font-semibold px-3 py-1.5 text-sm hover:opacity-90 disabled:opacity-40">Switch now</button>
          <button data-act="undo" data-off="${canUndo ? '0' : '1'}" class="border border-red-400/60 text-red-200 px-3 py-1.5 text-sm hover:bg-red-500/10 disabled:opacity-40">Undo switch</button>
        </div>
      </div>
      <div class="grid grid-cols-1 lg:grid-cols-2 gap-0">
        <div class="px-4 py-3 text-sm text-gray-200 space-y-2">
          <p class="text-xs text-gray-400">Switches to the playoffs by itself once week 15 is graded (12 hours after its last kickoff),
            with the top 4 by the league rules: points, then conference title games, Army v Navy, chips,
            Celebration Bowl, last 8. It never flips a coin. It writes the same seeds as Playoff Teams above,
            and seeds you save there always win.</p>
          ${holdHtml}
          ${cutHtml}
          ${seededByAuto ? `<p class="text-xs text-gray-400">Switched ${fmt(row.switched_at)} by ${escapeHtml(row.switched_by)}.${row.army_navy_used ? ' Army v Navy was used to set the field, so it is not a playoff tiebreaker this year.' : ''}</p>` : ''}
          <div class="text-xs text-gray-400 pt-1"><div class="text-gray-300 mb-1">Log</div>${logHtml}</div>
        </div>
        <div class="border-t lg:border-t-0 lg:border-l border-[rgba(231,231,231,.08)]">
          <table class="table min-w-full text-sm">
            <thead class="bg-[var(--cfp-black)]"><tr class="border-b">
              <th class="text-left px-4 py-2 font-semibold text-[var(--cfp-ivory)] w-16">Seed</th>
              <th class="text-left px-3 py-2 font-semibold text-[var(--cfp-ivory)]">${prev.already_seeded ? 'Computed field' : 'Field if it ended today'}</th>
              <th class="text-center px-3 py-2 font-semibold text-[var(--cfp-ivory)]">Pts</th>
              <th class="text-left px-3 py-2 font-semibold text-[var(--cfp-ivory)]">Ahead on</th>
            </tr></thead>
            <tbody>${rows}</tbody>
          </table>
        </div>
      </div>`;

    mount.querySelectorAll('button[data-act]').forEach(b => {
      b.addEventListener('click', () => {
        const a = b.dataset.act;
        if (a === 'refresh') return refresh();
        if (a === 'force') return act('force', holds.length
          ? `Switch to the playoffs now, even though:\n\n${holds.map(h => '- ' + h.message).join('\n')}\n\nThis seeds: ${q.map(s => s.seed + ' ' + s.team_name).join(', ')}.`
          : `Switch to the playoffs now? Seeds: ${q.map(s => s.seed + ' ' + s.team_name).join(', ')}.`);
        if (a === 'undo') return act('undo', 'Remove this season\'s playoff seeds and go back to the regular season? The autopilot will pause so it doesn\'t switch straight back.');
        return act(a);
      });
    });
    paintBusy();
  }

  return { refresh };
}
