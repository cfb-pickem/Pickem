// The bookkeeping behind "are my picks actually in?" on the picks page.
//
// WHY THIS EXISTS. A member chose all four games one week, closed the tab, and
// scored nothing: the radios were lit, the local draft had them, and not one row
// had been written. The page knew - existingPicks held nothing for any of those
// games - it just never said so in a way anybody would notice. Everything the
// page now shouts about (the sticky bar, the leave warning, the tally) is
// derived from the two functions below, so there is exactly one definition of
// "unsaved" and it is the one with a test (tools/pickSave.test.mjs).
//
// Pure on purpose: no DOM, no Supabase. The page hands in what the radios say
// and what the database said, and gets back the difference.

/**
 * Compare what is selected on screen with what the database holds.
 *
 *   selected - [{ game_id, pick }] from the checked radios
 *   saved    - Map of String(game_id) -> pick, exactly as last read or written
 *   slateIds - every game this week's slate offers, tiebreakers included
 *   isOpen   - game_id -> can this game still be saved right now?
 *
 * A game counts as UNSAVED only when it has a selection that differs from the
 * saved row and is still open. A game with a saved pick and no selection (the
 * Clear button, or a lever let go) is not unsaved: saving would not delete the
 * row, so the old pick genuinely still stands, and the slots note says so.
 *
 * Games off the slate are ignored entirely, on both sides. That is the stray
 * fifth pick: a row on a game the commissioner has since taken off the board
 * must never be counted as "saved" and inflate the tally.
 */
export function pickTally(selected, saved, slateIds, isOpen = () => true) {
  const slate = new Set([...slateIds].map(String));
  const chosen = new Map();
  for (const s of selected || []) {
    const id = String(s.game_id);
    if (slate.has(id) && s.pick != null && s.pick !== '') chosen.set(id, s.pick);
  }

  const unsaved = [];
  for (const [id, pick] of chosen) {
    if (saved.get(id) !== pick && isOpen(id)) unsaved.push(id);
  }

  let savedCount = 0;
  for (const id of slate) {
    const p = saved.get(id);
    if (p != null && p !== '' && !unsaved.includes(id)) savedCount++;
  }

  return {
    total: slate.size,
    unsaved,                  // game ids, as strings
    savedCount,               // slate games whose saved row is what is on screen
    selectedCount: chosen.size,
    complete: slate.size > 0 && savedCount === slate.size && unsaved.length === 0
  };
}

/**
 * Keep only rows for games on this week's slate. Used on the way OUT (what is
 * about to be upserted) and on the way IN (what the database handed back), so a
 * pick on an un-selected game can neither be written nor counted. The database
 * enforces the write half as well - see the 20261012120000 migration - and this
 * is the half that makes the page say something sensible instead of a 400.
 */
export function onSlate(rows, slateIds) {
  const slate = new Set([...slateIds].map(String));
  return (rows || []).filter(r => slate.has(String(r.game_id)));
}

/** "1 pick" / "3 picks". */
export function plural(n, one, many = one + 's') {
  return n + ' ' + (n === 1 ? one : many);
}
