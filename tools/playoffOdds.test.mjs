// node tools/playoffOdds.test.mjs
import assert from 'node:assert/strict';
import { simulatePlayoffOdds, measureAgreement } from '../js/playoffOdds.js';

const teams = ids => ids.map(id => ({ id }));
const sum = m => [...m.values()].reduce((a, v) => a + v.playoff, 0);
let passed = 0;
const t = (name, fn) => { fn(); passed++; console.log('ok -', name); };

t('odds always sum to the number of spots', () => {
  const r = simulatePlayoffOdds({
    teams: teams(['a', 'b', 'c', 'd', 'e', 'f']),
    points: { a: 20, b: 19, c: 19, d: 18, e: 17, f: 12 },
    unknownGames: 24, agreement: 0.62, sims: 4000,
  });
  assert.ok(Math.abs(sum(r) - 4) < 1e-9, String(sum(r)));
  const firsts = [...r.values()].reduce((a, v) => a + v.first, 0);
  assert.ok(Math.abs(firsts - 1) < 1e-9);
});

t('season over: the standings are the answer, ties shared', () => {
  const r = simulatePlayoffOdds({
    teams: teams(['a', 'b', 'c', 'd', 'e']),
    points: { a: 30, b: 28, c: 25, d: 25, e: 10 },
    unknownGames: 0, agreement: 0.6, sims: 100, spots: 3,
  });
  assert.equal(r.get('a').playoff, 1);
  assert.equal(r.get('b').playoff, 1);
  assert.equal(r.get('c').playoff, 0.5);
  assert.equal(r.get('d').playoff, 0.5);
  assert.equal(r.get('e').playoff, 0);
  assert.equal(r.get('a').first, 1);
});

t('mathematically eliminated is exactly zero, clinched exactly one', () => {
  const r = simulatePlayoffOdds({
    teams: teams(['a', 'b', 'c']),
    points: { a: 40, b: 20, c: 5 },
    unknownGames: 8, agreement: 0.6, sims: 3000, spots: 2,
  });
  assert.equal(r.get('a').playoff, 1);
  assert.equal(r.get('c').playoff, 0);
});

t('identical picks on known games move nobody', () => {
  const picks = { a: 'home', b: 'home', c: 'home' };
  const r = simulatePlayoffOdds({
    teams: teams(['a', 'b', 'c']),
    points: { a: 10, b: 9, c: 8 },
    knownGames: [{ picks }, { picks }, { picks }, { picks }],
    unknownGames: 0, agreement: 0.6, sims: 2000, spots: 1,
  });
  assert.equal(r.get('a').playoff, 1);
});

t('more overlap means fewer comebacks', () => {
  const base = { teams: teams(['a', 'b', 'c', 'd', 'e']), points: { a: 22, b: 21, c: 20, d: 19, e: 16 }, unknownGames: 24, sims: 20000 };
  const loose = simulatePlayoffOdds({ ...base, agreement: 0.5 }).get('e').playoff;
  const tight = simulatePlayoffOdds({ ...base, agreement: 0.9 }).get('e').playoff;
  assert.ok(tight < loose, `${tight} !< ${loose}`);
});

t('deterministic for the same inputs', () => {
  const o = { teams: teams(['a', 'b', 'c', 'd', 'e']), points: { a: 5, b: 4, c: 4, d: 3, e: 1 }, unknownGames: 12, agreement: 0.6, sims: 3000 };
  assert.deepEqual([...simulatePlayoffOdds(o)], [...simulatePlayoffOdds(o)]);
});

t('a no-show scores nothing', () => {
  const r = simulatePlayoffOdds({
    teams: teams(['a', 'b']), points: { a: 10, b: 10 },
    unknownGames: 10, agreement: 0.6, submitRate: { b: 0 }, sims: 2000, spots: 1,
  });
  assert.equal(r.get('b').expected, 10);
  assert.ok(r.get('a').playoff > 0.99);
});

t('a missed week costs the whole week, at the turnout rate', () => {
  // Two players level, one turns up half the time. Over one 4-game week the
  // half-timer either plays all four or none, so they can never finish
  // exactly one, two or three points behind by sitting out part of it - and on
  // average they score half as much.
  const r = simulatePlayoffOdds({
    teams: teams(['a', 'b']), points: { a: 0, b: 0 },
    unknownGames: 4, gamesPerWeek: 4, agreement: 0.5, submitRate: { b: 0.5 }, sims: 20000, spots: 1,
  });
  assert.ok(Math.abs(r.get('a').expected - 2) < 0.05, String(r.get('a').expected));
  assert.ok(Math.abs(r.get('b').expected - 1) < 0.05, String(r.get('b').expected));
});

t('whole-week misses swing harder than scattered ones', () => {
  // Same expected attendance, but a week missed in one go hurts the leader's
  // chaser more often than four scattered games would.
  const o = { teams: teams(['a', 'b']), points: { a: 0, b: 1 }, unknownGames: 8, agreement: 0.5, submitRate: { b: 0.75 }, sims: 40000, spots: 1 };
  const weekly = simulatePlayoffOdds({ ...o, gamesPerWeek: 4 }).get('a').playoff;
  const scattered = simulatePlayoffOdds({ ...o, gamesPerWeek: 1 }).get('a').playoff;
  assert.notEqual(weekly.toFixed(3), scattered.toFixed(3));
});

t('players in the same position get exactly the same odds', () => {
  const r = simulatePlayoffOdds({
    teams: teams(['a', 'b', 'c', 'd', 'e', 'f', 'g']),
    points: { a: 23, b: 23, c: 22, d: 22, e: 21, f: 21, g: 21 },
    unknownGames: 24, agreement: 0.66, sims: 5000,
  });
  assert.deepEqual(r.get('a'), r.get('b'));
  assert.deepEqual(r.get('c'), r.get('d'));
  assert.deepEqual(r.get('e'), r.get('g'));
  assert.ok(Math.abs([...r.values()].reduce((x, v) => x + v.playoff, 0) - 4) < 1e-9);
});

t('different visible picks keep level players apart', () => {
  const r = simulatePlayoffOdds({
    teams: teams(['a', 'b', 'c']), points: { a: 10, b: 10, c: 10 },
    knownGames: [{ picks: { a: 'home', b: 'away', c: 'home' } }],
    unknownGames: 0, agreement: 0.6, sims: 4000, spots: 1,
  });
  assert.deepEqual(r.get('a'), r.get('c'));
  assert.notDeepEqual(r.get('a'), r.get('b'));
});

t('a live game counts by how it is going', () => {
  // Level players on opposite sides of a game the home side is all but certain
  // to cover: the home picker is almost certainly ahead.
  const r = simulatePlayoffOdds({
    teams: teams(['a', 'b']), points: { a: 10, b: 10 },
    knownGames: [{ picks: { a: 'home', b: 'away' }, pHome: 0.97 }],
    unknownGames: 0, agreement: 0.6, sims: 20000, spots: 1,
  });
  assert.ok(Math.abs(r.get('a').playoff - 0.97) < 0.01, String(r.get('a').playoff));
});

t('agreement: majority share, with a fallback for thin data', () => {
  assert.equal(measureAgreement([['h', 'h', 'a']]), 0.6);
  const many = Array.from({ length: 20 }, () => ['h', 'h', 'h', 'a']);
  assert.equal(measureAgreement(many), 0.75);
});

console.log(`\n${passed} passed`);
