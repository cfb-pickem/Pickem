// js/playoffOdds.js — each player's chance of making the playoff, by simulation.
//
// THE MODEL, and why it is this simple. Every remaining game is a coin flip
// against the spread. That is not a shortcut: it is the same position the
// leaderboard takes (a pregame line is the market's 50/50), and ATS records over
// a season are mostly noise, so weighting players by their record so far would
// be inventing skill the numbers do not show.
//
// Picks overlap - if everyone takes the same side of a game, nobody gains on
// anybody - so the model plays that out rather than flipping each player's
// picks independently. Measured on this league it moves the odds by a point or
// two, not more; it is there because it is right, not because it is big:
//
//   * A game whose picks are visible (the week has locked, the game has not been
//     settled) is played out with the real picks: one flip for which side
//     covers, and everyone on that side scores.
//   * A game nobody has picked yet (later weeks, or this week before the lock)
//     gets a "popular side" that covers half the time, and each player lands on
//     it with probability `agreement` - this league's own measured rate of
//     siding with the majority.
//
// Turnout is modelled by the WEEK, because that is how people miss: all four
// games at once, not one here and there. Each player's chance of sitting out a
// future week is their misses so far over (weeks played + 5) - so one miss in
// ten weeks reads as about a 7% habit rather than 10%, and a clean record stays
// clean. Scattering misses game by game (the first version) understated how
// much a missed week costs and overstated how often it happens.
//
// Top `spots` advance. A tie across the cut is shared: three players level for
// the last two places each get two-thirds of that simulation, which is what the
// tiebreakers resolve into on average without pretending to know their result.
//
// Deterministic: seeded from the inputs, so a refresh does not wobble the
// numbers by a point and make people think something happened.

function mulberry32(seed) {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6D2B79F5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function hashSeed(parts) {
  let h = 2166136261;
  for (const ch of JSON.stringify(parts)) {
    h ^= ch.charCodeAt(0);
    h = Math.imul(h, 16777619);
  }
  return h >>> 0;
}

/**
 * @param {object}   o
 * @param {Array<{id:any}>} o.teams
 * @param {Map|object} o.points          current regular-season points by team id
 * @param {Array<{picks: Map|object}>} o.knownGames  undecided games with visible picks;
 *                                       picks maps team id -> 'home' | 'away'
 * @param {number}   o.unknownGames      games still to be picked, played in weeks of gamesPerWeek
 * @param {number}   o.agreement         P(a player sides with the majority), 0.5-1
 * @param {Map|object} [o.submitRate]    P(team turns up for a week), default 1
 * @param {number}   [o.gamesPerWeek=4]
 * @param {number}   [o.spots=4]
 * @param {number}   [o.sims=20000]
 * @returns {Map<id, {playoff:number, first:number, expected:number}>}
 */
export function simulatePlayoffOdds(o) {
  const ids = o.teams.map(t => t.id);
  const n = ids.length;
  const get = (m, k, d) => (m instanceof Map ? (m.has(k) ? m.get(k) : d) : (k in (m || {}) ? m[k] : d));
  const base = ids.map(id => Number(get(o.points, id, 0)) || 0);
  const submit = ids.map(id => Math.min(1, Math.max(0, Number(get(o.submitRate || {}, id, 1)))));
  const spots = Math.min(o.spots ?? 4, n);
  const perWeek = Math.max(1, o.gamesPerWeek ?? 4);
  const sims = o.sims ?? 20000;
  const agree = Math.min(1, Math.max(0.5, Number(o.agreement) || 0.5));

  const known = (o.knownGames || []).map(g => ids.map(id => {
    const side = get(g.picks, id, null);
    return side === 'home' ? 1 : side === 'away' ? -1 : 0;
  }));

  const rand = mulberry32(hashSeed([base, known, o.unknownGames, agree, submit, spots, sims, perWeek]));
  const playoff = new Float64Array(n), first = new Float64Array(n), total = new Float64Array(n);
  const pts = new Float64Array(n);
  const order = ids.map((_, i) => i);

  for (let s = 0; s < sims; s++) {
    for (let i = 0; i < n; i++) pts[i] = base[i];

    for (const sides of known) {
      const home = rand() < 0.5 ? 1 : -1;
      for (let i = 0; i < n; i++) if (sides[i] === home) pts[i]++;
    }
    const shows = new Uint8Array(n);
    for (let g = 0; g < o.unknownGames; g++) {
      // A new week: who turns up for it decides all of its games.
      if (g % perWeek === 0) for (let i = 0; i < n; i++) shows[i] = rand() < submit[i] ? 1 : 0;
      const popularCovers = rand() < 0.5;
      for (let i = 0; i < n; i++) {
        if (!shows[i]) continue;
        if ((rand() < agree) === popularCovers) pts[i]++;
      }
    }

    order.sort((a, b) => pts[b] - pts[a]);
    for (let i = 0; i < n; i++) total[i] += pts[i];

    // Who advances, sharing a tie across the cut.
    const cut = pts[order[spots - 1]];
    let above = 0, tied = 0;
    for (let r = 0; r < n; r++) {
      const v = pts[order[r]];
      if (v > cut) above++;
      else if (v === cut) tied++;
      else break;
    }
    const share = (spots - above) / tied;
    for (let r = 0; r < above + tied; r++) {
      const i = order[r];
      playoff[i] += pts[i] > cut ? 1 : share;
    }

    // First place, sharing a tie at the top.
    const top = pts[order[0]];
    let k = 0;
    while (k < n && pts[order[k]] === top) k++;
    for (let r = 0; r < k; r++) first[order[r]] += 1 / k;
  }

  // Players in exactly the same position - same points, same turnout, same
  // visible picks - have exactly the same chance; any gap between their raw
  // counts is sampling noise, and showing 66% beside 65% for two people level on
  // points invites a question with no real answer. So they share one number:
  // the average of their group, which is also the better estimate of it.
  const groups = new Map();
  ids.forEach((_, i) => {
    const key = JSON.stringify([base[i], submit[i], known.map(sides => sides[i])]);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(i);
  });
  const out = new Map();
  for (const members of groups.values()) {
    const avg = arr => members.reduce((acc, i) => acc + arr[i], 0) / members.length / sims;
    const r = { playoff: avg(playoff), first: avg(first), expected: avg(total) };
    for (const i of members) out.set(ids[i], r);
  }
  return out;
}

/**
 * How often a pick sides with the majority, across settled games where at
 * least two people picked. Falls back to a mild 0.6 with too little to go on.
 * @param {Array<Array<string>>} gamesPicks  per game, the list of sides picked
 */
export function measureAgreement(gamesPicks) {
  let on = 0, all = 0;
  for (const sides of gamesPicks) {
    if (sides.length < 2) continue;
    const counts = new Map();
    for (const s of sides) counts.set(s, (counts.get(s) || 0) + 1);
    on += Math.max(...counts.values());
    all += sides.length;
  }
  return all >= 40 ? on / all : 0.6;
}
