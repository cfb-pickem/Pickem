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

t('agreement: majority share, with a fallback for thin data', () => {
  assert.equal(measureAgreement([['h', 'h', 'a']]), 0.6);
  const many = Array.from({ length: 20 }, () => ['h', 'h', 'h', 'a']);
  assert.equal(measureAgreement(many), 0.75);
});

console.log(`\n${passed} passed`);
