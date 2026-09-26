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

const qb = (table) => {
  const q = { _t: table };
  const chain = ['select', 'eq', 'order', 'limit', 'in', 'gte', 'filter'];
  for (const m of chain) q[m] = () => q;
  q.maybeSingle = () => thenable(() => {
    const rows = SCRIPT.tables[table] ?? [];
    return { data: rows[0] ?? null, error: null };
  });
  q.then = (ok, err) => Promise.resolve()
    .then(() => ({ data: SCRIPT.tables[table] ?? [], error: null }))
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
