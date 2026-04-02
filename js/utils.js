// js/utils.js — Shared utility functions

export function stripAccents(s) {
  try { return s.normalize('NFD').replace(/\p{Diacritic}/gu, ''); }
  catch { return s; }
}

export function norm(v) {
  if (!v) return '';
  return stripAccents(String(v))
    .toLowerCase()
    .replace(/&/g, ' and ')
    .replace(/\./g, '')
    .replace(/[^a-z0-9\s]/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
}

const stToState = s => s.replace(/\bst\b/g, 'state');
const stateToSt = s => s.replace(/\bstate\b/g, 'st');

export function sameTeam(a, b) {
  const na = norm(a), nb = norm(b);
  return na === nb || stToState(na) === stToState(nb) || stateToSt(na) === stateToSt(nb);
}

export function escapeHtml(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

export function buildPickMap(rows) {
  const map = {};
  (rows || []).forEach(r => {
    if (!map[r.team_id]) map[r.team_id] = {};
    map[r.team_id][r.game_id] = r.pick;
  });
  return map;
}
