// A stand-in for the real client, resolving from the script the test sets up.
// Deliberately mimics supabase-js's shape INCLUDING the part that caused a
// real outage: rpc() returns a thenable builder with no .catch()/.finally(),
// so any code that chains one still blows up here exactly as it would in a
// browser. That faithfulness is what caught the original bug, so keep it.
import { SCRIPT } from '../stub-data.mjs';

const thenable = (resolve) => ({
  then(onOk, onErr) { return Promise.resolve().then(resolve).then(onOk, onErr); }
  // no .catch, no .finally - on purpose
});

// Every chained call is recorded in q._f as [method, ...args]. A table scripted
// as a plain array ignores them, which is all most tests need; a table scripted
// as a FUNCTION receives them and answers for that exact query, which is how a
// test pins WHICH rows the module asked for (blackjack-open.test.mjs does).
const qb = (table) => {
  const q = { _t: table, _f: [] };
  const chain = ['select', 'eq', 'not', 'order', 'limit', 'in', 'gte', 'filter'];
  for (const m of chain) q[m] = (...a) => { q._f.push([m, ...a]); return q; };
  const rows = () => {
    const t = SCRIPT.tables[table];
    return (typeof t === 'function' ? t(q._f) : t) ?? [];
  };
  q.maybeSingle = () => thenable(() => ({ data: rows()[0] ?? null, error: null }));
  q.then = (ok, err) => Promise.resolve()
    .then(() => ({ data: rows(), error: null }))
    .then(ok, err);
  return q;
};

export const supabase = {
  rpc(fn, args) {
    SCRIPT.calls.push(fn);
    return thenable(() => {
      const v = SCRIPT.rpc[fn];
      if (typeof v === 'function') return v(args);
      return v ?? { data: null, error: { message: 'no stub for ' + fn } };
    });
  },
  from: qb
};
