// js/gameScore.js — how good is this football game?
//
// There's no rankings table, so this scores the three signals that ARE in the
// data and correlate with a game being worth watching. Shared so the
// commissioner's "best games first" sort and the season's Big Game Heroes /
// Sickos leaderboards are ranking by exactly the same yardstick — if they drifted
// apart, "best game" would mean two different things on two pages.

import { TEAM_COLORS } from './utils.js';

// Broadcast tier. A national network is the clearest proxy for a marquee game;
// anything on a "+" streaming tier is almost always a body-bag game.
export const NETWORK_TIER = {
  'ABC': 40, 'CBS': 40, 'FOX': 40, 'NBC': 40,
  'ESPN': 35, 'TNT': 26,
  'ESPN2': 22, 'FS1': 22, 'BTN': 22, 'SEC Network': 22, 'ACC Network': 22,
  'CBSSN': 18, 'USA Net': 18, 'CW': 18, 'Peacock': 18, 'ESPNU': 14,
};

export function networkScore(net) {
  const n = String(net || '').trim();
  if (!n) return 0;
  if (NETWORK_TIER[n] != null) return NETWORK_TIER[n];
  if (n.endsWith('+')) return 3;   // ESPN+, SECN+, MW+ ...
  return 10;                       // some other cable channel
}

// TEAM_COLORS is the 131-team FBS list the rest of the site uses, so a name
// missing from it is an FCS visitor.
export function isFbs(name) {
  return !!TEAM_COLORS[String(name || '').trim()];
}

/**
 * Score a game. Accepts either an all_games row ("Network", Away, Home, line)
 * or a normalised object (network, away, home, line).
 */
export function gameScore(g) {
  if (!g) return 0;
  const net  = g['Network'] ?? g.network;
  const away = g['Away']    ?? g.away;
  const home = g['Home']    ?? g.home;
  const raw  = g.line;

  let score = networkScore(net);

  // A close spread is the strongest single signal of a good game. A pick'em is
  // worth the most; by ~28 points it contributes nothing.
  const ln = raw == null ? null : Number(raw);
  if (ln != null && !Number.isNaN(ln)) score += Math.max(0, 28 - Math.abs(ln));

  // Mismatches drag everything down regardless of who is televising them.
  score += (isFbs(away) && isFbs(home)) ? 25 : -15;

  return score;
}
