// js/kickoff.js — one source of truth for "has this game/week started?"
//
// Why this exists:
//   `all_games."Start (CT)"` holds a NAIVE Central-time wall clock, e.g.
//   "2025-11-01T11:00:00" — no offset. `new Date(thatString)` parses it as the
//   VIEWER's local time, so the same game appeared to start at different
//   instants for different people. The leaderboard used that bare parse to
//   decide when to reveal everyone's picks, while the picks page used a
//   separate (correct) Central calculation to decide when to lock them. The two
//   disagreed, and for anyone east of Central the reveal fired first.
//
// The rule this module enforces:
//   Reveal and lock are THE SAME predicate. A week's picks become visible at
//   exactly the moment they stop being editable — never a second sooner. When
//   we can't tell whether a game has started, we keep picks HIDDEN (safe) while
//   leaving them editable (so a missing kickoff time can't lock a league out).

export const CENTRAL_TZ = 'America/Chicago';

const _dtfCache = new Map();
function dtf(tz) {
  let f = _dtfCache.get(tz);
  if (!f) {
    f = new Intl.DateTimeFormat('en-US', {
      timeZone: tz, year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
    });
    _dtfCache.set(tz, f);
  }
  return f;
}

// How far `tz` is from UTC at a given instant, in ms (negative west of UTC).
function tzOffsetMs(tz, epochMs) {
  const p = dtf(tz).formatToParts(new Date(epochMs))
    .reduce((a, x) => (a[x.type] = x.value, a), {});
  let hour = Number(p.hour);
  if (hour === 24) hour = 0; // some engines emit "24" for midnight
  const asIfUTC = Date.UTC(
    Number(p.year), Number(p.month) - 1, Number(p.day),
    hour, Number(p.minute), Number(p.second)
  );
  return asIfUTC - epochMs;
}

const CT_SHAPE = /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2}))?/;

/**
 * Naive Central wall-clock string -> true epoch ms. DST-correct, and identical
 * for every viewer regardless of their own timezone. Returns null if the value
 * is missing or not in a shape we recognise.
 */
export function ctToEpochMs(raw) {
  if (raw == null) return null;
  const m = CT_SHAPE.exec(String(raw).trim());
  if (!m) return null;

  const asUTC = Date.UTC(
    Number(m[1]), Number(m[2]) - 1, Number(m[3]),
    Number(m[4]), Number(m[5]), Number(m[6] || 0)
  );

  // Treat the wall clock as UTC, then subtract the offset that actually applies.
  // Resolve twice: right at a DST boundary the first guess can be an hour out.
  const off1 = tzOffsetMs(CENTRAL_TZ, asUTC);
  let epoch = asUTC - off1;
  const off2 = tzOffsetMs(CENTRAL_TZ, epoch);
  if (off2 !== off1) epoch = asUTC - off2;

  return Number.isFinite(epoch) ? epoch : null;
}

/** Kickoff of a game row, in true epoch ms. Accepts either field spelling. */
export function kickoffMs(game) {
  if (!game) return null;
  return ctToEpochMs(game['Start (CT)'] ?? game.start ?? null);
}

/** Earliest kickoff across games; null when none has a usable time. */
export function earliestKickoffMs(games) {
  let best = null;
  for (const g of games || []) {
    const ts = kickoffMs(g);
    if (ts == null) continue;
    if (best == null || ts < best) best = ts;
  }
  return best;
}

/**
 * Has this specific game started?
 *
 * A final score or a recorded winner counts as started even when the kickoff
 * time is missing — otherwise a game with no time would stay open forever, and
 * its result would already be public.
 */
export function isGameLocked(game, now = Date.now()) {
  if (!game) return false;

  const status = String(game.Status ?? game.status ?? '').toLowerCase();
  if (status === 'final' || status === 'in' || status === 'live') return true;
  if (game.winner) return true;

  const ts = kickoffMs(game);
  if (ts == null) return false; // unknown: stay editable, but see isWeekLocked
  return now >= ts;
}

/**
 * Has the week started? True as soon as ANY game in it has.
 *
 * Pass EVERY game in the week — regular *and* tiebreaker. A tiebreaker kicking
 * Thursday night starts the week just as surely as a Saturday noon game.
 */
export function isWeekLocked(games, now = Date.now()) {
  for (const g of games || []) {
    if (isGameLocked(g, now)) return true;
  }
  return false;
}

/** Format a kickoff (epoch ms or raw CT string) for display, always in CT. */
export function formatCT(value, opts = {}) {
  const ms = typeof value === 'number' ? value : ctToEpochMs(value);
  if (ms == null || !Number.isFinite(ms)) return '';
  try {
    return new Date(ms).toLocaleTimeString('en-US', {
      hour: '2-digit', minute: '2-digit', timeZone: CENTRAL_TZ, ...opts,
    });
  } catch {
    return '';
  }
}
