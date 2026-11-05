// js/seasonRace.js — everyone's season points, week by week, as one chart.
//
// FORM. Fourteen lines is far past what colour can tell apart (eight hues is the
// ceiling, and a lines chart wants fewer). So this is an emphasis chart: every
// player is a thin grey line, and up to three chosen players are drawn in
// colour. The chips under the title are both the legend and the picker - tap
// a name to bring it forward. A colour belongs to the player while they are
// picked; picking someone else never repaints the others.
//
// The dashed line is fourth place - the playoff cut - so "am I in?" reads off
// the chart without counting.
//
// Hover (or touch) anywhere on the plot for that week's standings. The same
// numbers are in the table under the chart, for anyone who would rather read
// than hover.

const SLOT_COLOURS = ['#3987e5', '#d95926', '#199e70'];   // validated against the dark card surface
const GREY = 'rgba(160,166,178,.32)';
const CUT_SPOTS = 4;
const NS = 'http://www.w3.org/2000/svg';

function el(tag, attrs = {}, parent) {
  const e = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
  if (parent) parent.appendChild(e);
  return e;
}

function esc(s) {
  return String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

/**
 * @param {HTMLElement} mount
 * @param {object} data
 * @param {number[]} data.weeks             weeks with results, ascending
 * @param {Array<{id:any, name:string, cum:number[]}>} data.series  cumulative points per week
 */
export function renderSeasonRace(mount, data) {
  const { weeks, series } = data;
  mount.innerHTML = '';
  if (!weeks.length || !series.length) {
    mount.innerHTML = '<p class="race-empty">No results yet this season.</p>';
    return;
  }

  const last = weeks.length - 1;
  const byLatest = [...series].sort((a, b) => b.cum[last] - a.cum[last] || a.name.localeCompare(b.name));

  // Fourth place at each week: the playoff cut as it stood then.
  const cut = weeks.map((_, w) => {
    const vals = series.map(s => s.cum[w]).sort((a, b) => b - a);
    return vals[Math.min(CUT_SPOTS, vals.length) - 1];
  });

  // id -> colour slot. Defaults to the top three as of the latest week, but a
  // live refresh redraws this chart, and whoever you picked should survive it -
  // so the choice is kept on the mount and reused while those players exist.
  const ids = new Set(series.map(s => s.id));
  const kept = (mount._racePicked || []).filter(([id]) => ids.has(id));
  const picked = new Map(kept.length ? kept : byLatest.slice(0, 3).map((s, i) => [s.id, i]));
  const remember = () => { mount._racePicked = [...picked.entries()]; };
  remember();

  const chips = document.createElement('div');
  chips.className = 'race-chips';
  chips.setAttribute('role', 'group');
  chips.setAttribute('aria-label', 'Highlight players (up to three)');

  const figure = document.createElement('div');
  figure.className = 'race-figure';

  const tip = document.createElement('div');
  tip.className = 'race-tip';
  tip.hidden = true;

  mount.append(chips, figure);
  figure.appendChild(tip);

  function freeSlot() {
    const used = new Set(picked.values());
    for (let i = 0; i < SLOT_COLOURS.length; i++) if (!used.has(i)) return i;
    return -1;
  }

  function toggle(id) {
    if (picked.has(id)) picked.delete(id);
    else {
      let slot = freeSlot();
      if (slot < 0) {
        // Full: the player picked longest ago makes way.
        const [oldest, s] = picked.entries().next().value;
        picked.delete(oldest);
        slot = s;
      }
      picked.set(id, slot);
    }
    remember();
    paintChips();
    draw();
  }

  function paintChips() {
    chips.innerHTML = '';
    for (const s of byLatest) {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'race-chip';
      const on = picked.has(s.id);
      b.setAttribute('aria-pressed', on ? 'true' : 'false');
      if (on) b.style.setProperty('--chip', SLOT_COLOURS[picked.get(s.id)]);
      b.innerHTML = `<span class="race-dot" aria-hidden="true"></span>${esc(s.name)}`;
      b.addEventListener('click', () => toggle(s.id));
      chips.appendChild(b);
    }
  }

  let svg = null;
  function draw() {
    const W = Math.max(300, figure.clientWidth || 600);
    const narrow = W < 520;
    const H = narrow ? 260 : 320;
    const m = { t: 12, r: narrow ? 92 : 128, b: 28, l: 34 };
    const iw = W - m.l - m.r, ih = H - m.t - m.b;

    const maxY = Math.max(1, ...series.map(s => s.cum[last]));
    const step = maxY > 24 ? 5 : maxY > 10 ? 2 : 1;
    const top = Math.ceil(maxY / step) * step;
    const x = w => m.l + (weeks.length === 1 ? iw / 2 : (w / last) * iw);
    const y = v => m.t + ih - (v / top) * ih;

    if (svg) svg.remove();
    svg = el('svg', { viewBox: `0 0 ${W} ${H}`, width: '100%', height: H, role: 'img',
      'aria-label': `Season points by week. Leader ${byLatest[0].name} with ${byLatest[0].cum[last]}.` });
    figure.insertBefore(svg, tip);

    // Grid and y labels - recessive.
    for (let v = 0; v <= top; v += step) {
      el('line', { x1: m.l, x2: m.l + iw, y1: y(v), y2: y(v), class: 'race-grid' }, svg);
      const t = el('text', { x: m.l - 6, y: y(v) + 3.5, class: 'race-axis', 'text-anchor': 'end' }, svg);
      t.textContent = v;
    }
    // Week labels, thinned on a phone.
    const every = narrow ? Math.ceil(weeks.length / 6) : 1;
    weeks.forEach((wk, w) => {
      if (w % every && w !== last) return;
      const t = el('text', { x: x(w), y: H - 8, class: 'race-axis', 'text-anchor': 'middle' }, svg);
      t.textContent = (w === 0 && !narrow ? 'Wk ' : '') + wk;
    });

    const path = vals => vals.map((v, w) => `${w ? 'L' : 'M'}${x(w).toFixed(1)} ${y(v).toFixed(1)}`).join('');

    // Everyone, grey.
    for (const s of series) {
      if (picked.has(s.id)) continue;
      el('path', { d: path(s.cum), fill: 'none', stroke: GREY, 'stroke-width': 1.25, 'stroke-linejoin': 'round' }, svg);
    }

    // The cut.
    el('path', { d: path(cut), fill: 'none', class: 'race-cut' }, svg);

    // The picked, in colour, with a surface ring so crossings stay legible.
    const labels = [];
    for (const [id, slot] of picked) {
      const s = series.find(z => z.id === id);
      if (!s) continue;
      const c = SLOT_COLOURS[slot];
      el('path', { d: path(s.cum), fill: 'none', class: 'race-ring' }, svg);
      el('path', { d: path(s.cum), fill: 'none', stroke: c, 'stroke-width': 2, 'stroke-linejoin': 'round', 'stroke-linecap': 'round' }, svg);
      el('circle', { cx: x(last), cy: y(s.cum[last]), r: 3.5, fill: c, class: 'race-end' }, svg);
      labels.push({ y: y(s.cum[last]), text: `${narrow && s.name.length > 9 ? s.name.slice(0, 8) + '…' : s.name} ${s.cum[last]}`, colour: c, v: s.cum[last] });
    }
    labels.push({ y: y(cut[last]), text: `4th ${cut[last]}`, cut: true, v: cut[last] });

    // Direct labels at the right edge, each with its own swatch - two players
    // level on points share an end point, so the line's own dot can't say
    // which name is whose. Nudged apart so none sit on top of each other.
    labels.sort((a, b) => a.y - b.y || b.v - a.v);
    for (let i = 1; i < labels.length; i++) if (labels[i].y - labels[i - 1].y < 13) labels[i].y = labels[i - 1].y + 13;
    for (const l of labels) {
      const sx = x(last) + 12;
      if (l.cut) el('line', { x1: sx - 4, x2: sx + 4, y1: l.y, y2: l.y, class: 'race-cut' }, svg);
      else el('circle', { cx: sx, cy: l.y, r: 3.5, fill: l.colour }, svg);
      const t = el('text', { x: sx + 8, y: l.y + 4, class: l.cut ? 'race-cut-label' : 'race-end-label' }, svg);
      t.textContent = l.text;
    }

    // Crosshair and hit area.
    const cross = el('line', { y1: m.t, y2: m.t + ih, class: 'race-cross', visibility: 'hidden' }, svg);
    const hit = el('rect', { x: m.l - 10, y: m.t, width: iw + 20, height: ih, fill: 'transparent' }, svg);

    const show = clientX => {
      const r = svg.getBoundingClientRect();
      const px = (clientX - r.left) * (W / r.width);
      const w = Math.max(0, Math.min(last, Math.round(((px - m.l) / iw) * last)));
      cross.setAttribute('x1', x(w)); cross.setAttribute('x2', x(w));
      cross.setAttribute('visibility', 'visible');

      const standing = [...series].sort((a, b) => b.cum[w] - a.cum[w] || a.name.localeCompare(b.name));
      // Rank as the board shows it: one more than the number of players ahead.
      const rankOf = id => {
        const v = series.find(z => z.id === id).cum[w];
        return 1 + series.filter(s => s.cum[w] > v).length;
      };
      const rows = [];
      for (const [id, slot] of picked) {
        const s = series.find(z => z.id === id);
        rows.push(`<div class="race-tip-row"><span class="race-dot" style="--chip:${SLOT_COLOURS[slot]}"></span><span>${esc(s.name)}</span><b>${s.cum[w]}</b><em>#${rankOf(id)}</em></div>`);
      }
      tip.innerHTML = `<div class="race-tip-head">Week ${weeks[w]}</div>${rows.join('')}
        <div class="race-tip-row race-tip-muted"><span class="race-dot race-dot-cut"></span><span>4th place</span><b>${cut[w]}</b><em></em></div>
        <div class="race-tip-row race-tip-muted"><span></span><span>Leader: ${esc(standing[0].name)}</span><b>${standing[0].cum[w]}</b><em></em></div>`;
      tip.hidden = false;
      const left = (x(w) / W) * r.width;
      const tw = tip.offsetWidth;
      tip.style.left = Math.max(4, Math.min(r.width - tw - 4, left + (left > r.width / 2 ? -tw - 12 : 12))) + 'px';
      tip.style.top = '8px';
    };
    const hide = () => { tip.hidden = true; cross.setAttribute('visibility', 'hidden'); };
    hit.addEventListener('pointermove', e => show(e.clientX));
    hit.addEventListener('pointerdown', e => show(e.clientX));
    hit.addEventListener('pointerleave', hide);
  }

  paintChips();
  draw();

  // The table view: the same numbers, readable without hovering.
  const details = document.createElement('details');
  details.className = 'race-table';
  const head = weeks.map(w => `<th class="px-2 py-1 text-center font-semibold">${w}</th>`).join('');
  const body = byLatest.map(s => `<tr class="border-t border-[rgba(231,231,231,.06)]"><td class="px-3 py-1">${esc(s.name)}</td>${
    s.cum.map(v => `<td class="px-2 py-1 text-center">${v}</td>`).join('')}</tr>`).join('');
  details.innerHTML = `<summary>Week-by-week table</summary>
    <div class="overflow-auto"><table class="table min-w-full text-xs"><thead><tr class="bg-[var(--cfp-black)] text-[var(--cfp-ivory)]">
    <th class="px-3 py-1 text-left font-semibold">Team</th>${head}</tr></thead><tbody id="race-table-body">${body}</tbody></table></div>`;
  mount.appendChild(details);

  // Redraw on resize; the chips and table don't need it.
  if (mount._raceObserver) mount._raceObserver.disconnect();
  let lastW = figure.clientWidth;
  mount._raceObserver = new ResizeObserver(() => {
    if (Math.abs(figure.clientWidth - lastW) < 8) return;
    lastW = figure.clientWidth;
    draw();
  });
  mount._raceObserver.observe(figure);
}
