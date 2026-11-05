// js/coverOdds.js — is a game live, and how likely is a pick to cover from here.
//
// Moved out of index.html verbatim so the stats page's live playoff odds and the
// leaderboard's live cover shading are one piece of code, not two copies that
// drift. The comments are the originals; "the board" means the leaderboard.

import { sameTeam } from './utils.js';

// IS THIS GAME BEING PLAYED RIGHT NOW?
//
// Everything live on this page hangs off this one question, so it is asked
// in one place. The column is written by a Supabase edge function that is
// deployed rather than versioned in this repo, and ESPN's own feed offers
// two spellings for the same state - `state: "in"` and
// `name: "STATUS_IN_PROGRESS"`. This page has always read 'in_progress' and
// a season of live scores says that is right, but the score-feed fix in
// 0c0b9ba hedged across both, so somebody was not certain. Accepting either
// costs nothing and removes the one failure that would take the whole live
// board down silently.
//
// The period fallback is last and deliberately narrow: a game that is not
// final, not still scheduled, and has a quarter on it is being played,
// whatever the feed decided to call it.
const LIVE_WORDS = new Set(['in_progress', 'in progress', 'inprogress', 'in', 'live']);
const DEAD_WORDS = new Set(['final', 'scheduled', 'pre', 'postponed', 'canceled', 'cancelled', '']);

export const isLiveStatus = st => LIVE_WORDS.has(String(st ?? '').trim().toLowerCase());

export function isLive(row){
  if (!row) return false;
  const st = String(row.Status ?? '').trim().toLowerCase();
  if (isLiveStatus(st)) return true;
  if (DEAD_WORDS.has(st)) return false;
  return Number(row.Period) >= 1;
}

/**
 * The cover model, closed over a sameTeam/isLive pair so a caller can supply its
 * own (index.html passes the module's own, the same ones it used to define).
 */
export function makeCoverOdds({ sameTeam: same = sameTeam, isLive: live = isLive } = {}) {
  // Local names the original code was written against.
  const sameTeamFn = same, isLiveFn = live;
  // LIVE COVER, and why it is the same sum the database does.
  //
  // set_winner_on_final() settles a finished game with `(HomePts + line) >
  // AwayPts`. Evaluate that same expression while the game is still being
  // played and what comes out is the cover margin: how many points the home
  // side is covering by right now, negative if it is the away side. So the
  // shading on the board mid-game is not an approximation of the rule that
  // will decide it - it is that rule, run early.
  //
  // It can never land on zero. Every line in the table ends in .5 by trigger,
  // and an integer score plus .5 is never an integer, so there is no level
  // state to colour - which is the same reason this league needs no push rule.
  function coverMargin(row){
    if (!isLiveFn(row)) return null;
    const ln = row.line    == null ? NaN : Number(row.line);
    const a  = row.AwayPts == null ? NaN : Number(row.AwayPts);
    const h  = row.HomePts == null ? NaN : Number(row.HomePts);
    if (!Number.isFinite(ln) || !Number.isFinite(a) || !Number.isFinite(h)) return null;
    return (h + ln) - a;
  }

  // The same margin from one pick's side of it: positive means this pick is
  // covering by that many points.
  function coverForPick(row, pick){
    const m = coverMargin(row);
    if (m == null || !pick) return null;
    if (sameTeamFn(pick, row.Home)) return m;
    if (sameTeamFn(pick, row.Away)) return -m;
    return null;   // a pick that matches neither side of the game it is on
  }

  // HOW SETTLED IS IT? The margin above says who is covering; on its own it is
  // a bad thing to colour a board with, because at 0-0 it equals the line. Kick
  // off and every cell would take a side instantly, having learned nothing -
  // it would just be restating the spread, which is already printed above it.
  //
  // What the shading should track is the expected FINAL margin, and how much
  // game is left to change it:
  //
  //   expected final margin  =  margin now  -  line x (share of game left)
  //
  // because over a whole game the favourite is expected to win by exactly the
  // line, so what is still to come is the line scaled by the time still to
  // play. At kickoff that comes out at 0 - a coin flip, no colour - and it
  // earns colour only as the game says something the spread did not.
  //
  // The spread is 15.2 points wide, one standard deviation, measured over the
  // 756 completed FBS games of 2025: residual (HomePts + line) - AwayPts had a
  // mean of +1.14, so the market is near enough unbiased, and a stdev of 15.2.
  // Uncertainty shrinks with the root of the time left, so a 3-point cushion
  // is nearly nothing in the first quarter and nearly everything with a minute
  // to go.
  const SIGMA_FULL   = 15.2;   // points, 1 s.d. of final margin around the line
  const SIGMA_FLOOR  = 0.8;    // keeps the last tick of the clock from dividing by zero
  const OT_FRACTION  = 0.05;   // overtime: barely any clock, still plenty of swing
  const REG_MINUTES  = 60;

  function fractionRemaining(row){
    const per = Number(row.Period);
    if (!Number.isFinite(per) || per < 1) return 1;
    if (per >= 5) return OT_FRACTION;
    const m = String(row.Clock || '').trim().match(/^(\d+):(\d{2})$/);
    // No clock on the row yet - treat it as mid-quarter rather than guessing
    // in either direction.
    const mins = m ? Number(m[1]) + Number(m[2]) / 60 : 7.5;
    const left = (4 - per) * 15 + Math.min(15, Math.max(0, mins));
    return Math.min(1, Math.max(0, left / REG_MINUTES));
  }

  // Normal CDF, Abramowitz & Stegun 26.2.17. Max error 7.5e-8, which is many
  // orders of magnitude finer than a colour ramp can show.
  function normalCdf(z){
    const t = 1 / (1 + 0.2316419 * Math.abs(z));
    const d = 0.3989422804014327 * Math.exp(-z * z / 2);
    const p = d * t * (0.319381530 + t * (-0.356563782 + t * (1.781477937 +
              t * (-1.821255978 + t * 1.330274429))));
    return z >= 0 ? 1 - p : p;
  }

  // WHERE THE EXPECTED FINAL MARGIN COMES FROM.
  //
  // First choice is the market. An in-play spread is a full-game number - it
  // already contains the score, the clock, who has the ball and every injury
  // the room knows about - so the gap between our frozen line and the live one
  // IS the expected final cover margin, with no modelling on our side:
  //
  //     expected final  =  line  -  line_live
  //
  // Verified on the tail of real games, where the live number converges on the
  // result: Ohio State at Michigan closed live at 18.5 against a final margin
  // of 18, Grambling at Ohio State at -69.5 against -70, Texas Tech at West
  // Virginia at 46.5 against 49.
  //
  // Held to a much tighter freshness bar than the number the header displays.
  // A stale live line is worse than none: the clock keeps running, so sigma
  // keeps shrinking, and the shading would get more confident about a quote
  // that stopped tracking the game. Past this it falls back to the clock.
  const MARKET_MAX_AGE_MS = 4 * 60 * 1000;

  function marketExpectation(row, sign){
    // Only an in-play quote. A pregame book stops moving once the game starts,
    // so reading its closing number as "where this will finish" would call a
    // blowout an even game - and it is exactly what gets stored whenever ESPN
    // drops its in-play provider mid-game, which it does.
    if (row.line_live_kind !== 'inplay') return null;
    const ln = Number(row.line);
    const lv = row.line_live == null ? NaN : Number(row.line_live);
    if (!Number.isFinite(ln) || !Number.isFinite(lv)) return null;
    const at = row.line_live_at ? Date.parse(row.line_live_at) : NaN;
    if (!Number.isFinite(at) || Date.now() - at > MARKET_MAX_AGE_MS) return null;
    return sign * (ln - lv);
  }

  // Fallback, and the only path the postseason has: ESPN publishes no live
  // provider for neutral-site bowl and playoff games. Over a whole game the
  // favourite is expected to win by exactly the line, so what is still to come
  // is the line scaled by the share of the game left to play.
  function clockExpectation(row, sign, m, f){
    const ln = Number(row.line);
    if (!Number.isFinite(ln)) return null;
    return m - (sign * ln) * f;
  }

  // For one pick: where it stands now, and how likely that is to hold.
  function coverOdds(row, pick){
    const m = coverForPick(row, pick);
    if (m == null) return null;
    const f = fractionRemaining(row);
    const sign = sameTeamFn(pick, row.Home) ? 1 : -1;

    let expected = marketExpectation(row, sign);
    let source = 'live line';
    if (expected == null) {
      expected = clockExpectation(row, sign, m, f);
      source = 'clock';
    }
    if (expected == null) return null;

    // However good the central estimate, the spread of outcomes around it
    // still narrows with the clock rather than with the source.
    const sigma = Math.max(SIGMA_FLOOR, SIGMA_FULL * Math.sqrt(f));
    return { margin: m, p: normalCdf(expected / sigma), source };
  }

  return { coverMargin, coverForPick, coverOdds };
}
