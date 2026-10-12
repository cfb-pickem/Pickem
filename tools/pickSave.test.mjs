// tools/pickSave.test.mjs — what the picks page calls "unsaved", and the slate filter.
//
//   node tools/pickSave.test.mjs
//
// The bar, the leave warning and the tally on picks.html all come from
// pickTally(). If it ever says "saved" for something the database does not
// hold, a player loses a week again - which is how this file came to exist.

import { pickTally, onSlate, plural } from '../js/pickSave.js';

let failed = 0;
const ok = (cond, what) => { if (!cond) { failed++; console.error('FAIL: ' + what); } };
const eq = (got, want, what) =>
  ok(JSON.stringify(got) === JSON.stringify(want), `${what} — got ${JSON.stringify(got)}, want ${JSON.stringify(want)}`);

const SLOTS = '__slots__';
const slate = [101, 102, 103, 104];
const saved = m => new Map(Object.entries(m));

// The week that started all this: four picks on screen, nothing written.
{
  const t = pickTally(
    [{ game_id: 101, pick: 'A' }, { game_id: 102, pick: 'B' }, { game_id: 103, pick: 'C' }, { game_id: 104, pick: 'D' }],
    saved({}), slate);
  eq(t.unsaved, ['101', '102', '103', '104'], 'nothing saved: every pick is unsaved');
  eq(t.savedCount, 0, 'nothing saved: count is zero');
  eq(t.complete, false, 'nothing saved: not complete');
}

// Everything in.
{
  const t = pickTally(
    [{ game_id: 101, pick: 'A' }, { game_id: 102, pick: SLOTS }, { game_id: 103, pick: 'C' }, { game_id: 104, pick: 'D' }],
    saved({ 101: 'A', 102: SLOTS, 103: 'C', 104: 'D' }), slate);
  eq(t.unsaved, [], 'all saved: nothing unsaved');
  eq(t.savedCount, 4, 'all saved: four of four');
  eq(t.complete, true, 'all saved: complete');
}

// A changed pick is unsaved even though a row exists, and does not count as saved.
{
  const t = pickTally([{ game_id: 101, pick: 'B' }], saved({ 101: 'A', 102: 'X' }), slate);
  eq(t.unsaved, ['101'], 'changed pick is unsaved');
  eq(t.savedCount, 1, 'changed pick is not counted as saved; the untouched saved one is');
}

// Team -> slots and slots -> team are both changes.
{
  eq(pickTally([{ game_id: 101, pick: SLOTS }], saved({ 101: 'A' }), slate).unsaved, ['101'], 'team to slots is a change');
  eq(pickTally([{ game_id: 101, pick: 'A' }], saved({ 101: SLOTS }), slate).unsaved, ['101'], 'slots to team is a change');
}

// Cleared on screen but saved in the database: the saved pick still stands.
{
  const t = pickTally([], saved({ 101: 'A', 102: 'B' }), slate);
  eq(t.unsaved, [], 'cleared selection is not unsaved (nothing to save, row stands)');
  eq(t.savedCount, 2, 'cleared selection: saved rows still count');
}

// Locked games are never "unsaved" - no Save button can help them.
{
  const t = pickTally([{ game_id: 101, pick: 'B' }, { game_id: 102, pick: 'C' }], saved({}), slate,
    id => id !== '101');
  eq(t.unsaved, ['102'], 'locked game is excluded from unsaved');
}

// THE STRAY FIFTH PICK. A saved row on a game off the slate is not counted,
// and a selection for one is neither unsaved nor saveable.
{
  const t = pickTally(
    [{ game_id: 101, pick: 'A' }, { game_id: 999, pick: 'Z' }],
    saved({ 101: 'A', 102: 'B', 103: 'C', 104: 'D', 999: 'Z' }), slate);
  eq(t.savedCount, 4, 'stray saved row does not inflate the count past the slate');
  eq(t.total, 4, 'total is the slate, not the rows');
  eq(t.unsaved, [], 'stray selection is not reported as unsaved');
  eq(t.complete, true, 'complete despite the stray');
}

// String and number ids are the same game.
{
  const t = pickTally([{ game_id: '101', pick: 'A' }], saved({ 101: 'A' }), ['101', 102]);
  eq(t.unsaved, [], 'string/number ids compare equal');
  eq(t.total, 2, 'mixed-type slate');
}

// Empty slate.
eq(pickTally([], saved({}), []).complete, false, 'empty slate is never complete');

// onSlate.
eq(onSlate([{ game_id: 101 }, { game_id: 999 }, { game_id: '102' }], slate).map(r => String(r.game_id)),
   ['101', '102'], 'onSlate drops off-slate rows');
eq(onSlate(null, slate), [], 'onSlate tolerates null');

eq(plural(1, 'pick'), '1 pick', 'plural one');
eq(plural(3, 'pick'), '3 picks', 'plural many');
eq(plural(2, 'unsaved pick'), '2 unsaved picks', 'plural phrase');

if (failed) { console.error(`${failed} failed`); process.exit(1); }
console.log('pickSave: all checks passed');
