// js/sortableTables.js — click a column header to sort any table under a root.
//
// Built for the stats page, whose tables are all rendered as innerHTML and
// re-rendered whenever the season changes. So nothing here holds on to rows or
// cells: headers are enhanced as tables appear, the chosen sort is remembered
// per table (by its tbody id, or its position when it has none), and it is put
// back every time the table's rows are replaced.
//
// How a cell sorts, read from its text (or a data-sort attribute, which wins):
//   "20-16"         a record - by wins, then fewer losses
//   ".536" "72.2%"  a number
//   "—" or empty    nothing; always last, whichever way you sort
//   anything else   text, A-Z
// The first click on a number column sorts high-to-low, on a text column A-Z;
// clicking again flips it. Ties keep the order the page gave them.

const state = new Map();   // table key -> { col, dir }  (dir: 1 asc, -1 desc)

function keyFor(table, root) {
  const tb = table.tBodies[0];
  if (tb && tb.id) return tb.id;
  return 'table-' + [...root.querySelectorAll('table')].indexOf(table);
}

function headerCells(table) {
  const head = table.tHead;
  if (!head || !head.rows.length) return [];
  return [...head.rows[head.rows.length - 1].cells];
}

function parse(td) {
  if (!td) return { kind: 'none' };
  const raw = (td.dataset.sort ?? td.textContent ?? '').trim();
  if (raw === '' || raw === '—' || raw === '-' || raw === '—') return { kind: 'none' };
  let m = raw.match(/^(\d+)\s*-\s*(\d+)$/);
  if (m) return { kind: 'num', v: Number(m[1]) - Number(m[2]) / 1000 };
  m = raw.replace(/[,%]/g, '').match(/^[+-]?(\d+\.?\d*|\.\d+)$/);
  if (m) return { kind: 'num', v: parseFloat(raw.replace(/[,%]/g, '')) };
  return { kind: 'text', v: raw.toLowerCase() };
}

function columnIsNumeric(table, col) {
  for (const tr of table.tBodies[0]?.rows || []) {
    const p = parse(tr.cells[col]);
    if (p.kind === 'num') return true;
    if (p.kind === 'text') return false;
  }
  return false;
}

function paintHeaders(table, col, dir) {
  headerCells(table).forEach((th, i) => {
    const on = i === col;
    th.setAttribute('aria-sort', on ? (dir === 1 ? 'ascending' : 'descending') : 'none');
    const arrow = th.querySelector('.sort-arrow');
    if (arrow) arrow.textContent = on ? (dir === 1 ? '▲' : '▼') : '';
  });
}

function sortTable(table, col, dir) {
  const tb = table.tBodies[0];
  if (!tb) return;
  const rows = [...tb.rows];
  // A lone "no data yet" row spans the table; leave it alone.
  if (rows.length < 2 || rows.some(r => r.cells.length < 2)) { paintHeaders(table, col, dir); return; }
  const keyed = rows.map((tr, i) => ({ tr, i, p: parse(tr.cells[col]) }));
  keyed.sort((a, b) => {
    if (a.p.kind === 'none' || b.p.kind === 'none') {
      if (a.p.kind === b.p.kind) return a.i - b.i;
      return a.p.kind === 'none' ? 1 : -1;
    }
    let c = 0;
    if (a.p.kind === 'num' && b.p.kind === 'num') c = a.p.v - b.p.v;
    else c = String(a.p.v).localeCompare(String(b.p.v));
    return c * dir || a.i - b.i;
  });
  for (const k of keyed) tb.appendChild(k.tr);
  paintHeaders(table, col, dir);
}

function enhance(table) {
  headerCells(table).forEach(th => {
    if (th.dataset.sortable) return;
    th.dataset.sortable = '1';
    th.classList.add('th-sortable');
    th.tabIndex = 0;
    th.setAttribute('aria-sort', 'none');
    th.title = 'Sort by ' + th.textContent.trim();
    const arrow = document.createElement('span');
    arrow.className = 'sort-arrow';
    arrow.setAttribute('aria-hidden', 'true');
    th.appendChild(arrow);
  });
}

export function makeTablesSortable(root = document) {
  let observer = null;

  const reapply = () => {
    root.querySelectorAll('table').forEach(table => {
      enhance(table);
      const st = state.get(keyFor(table, root));
      if (st) sortTable(table, st.col, st.dir);
    });
    // Our own reordering is a mutation too; drop it so it doesn't loop back.
    observer?.takeRecords();
  };

  const onPick = th => {
    const table = th.closest('table');
    if (!table || !root.contains(table)) return;
    const col = headerCells(table).indexOf(th);
    if (col < 0) return;
    const key = keyFor(table, root);
    const prev = state.get(key);
    const dir = prev && prev.col === col ? -prev.dir : (columnIsNumeric(table, col) ? -1 : 1);
    state.set(key, { col, dir });
    sortTable(table, col, dir);
    observer?.takeRecords();
  };

  root.addEventListener('click', e => {
    const th = e.target.closest?.('th.th-sortable');
    if (th) onPick(th);
  });
  root.addEventListener('keydown', e => {
    if (e.key !== 'Enter' && e.key !== ' ') return;
    const th = e.target.closest?.('th.th-sortable');
    if (th) { e.preventDefault(); onPick(th); }
  });

  let t = 0;
  observer = new MutationObserver(() => { clearTimeout(t); t = setTimeout(reapply, 0); });
  observer.observe(root === document ? document.body : root, { childList: true, subtree: true });
  reapply();
}
