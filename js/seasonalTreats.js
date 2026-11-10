// js/seasonalTreats.js — small surprises for the holiday themes.
//
// Both check the theme when they fire rather than when they start, because the
// leaderboard only learns which holiday it is once its games have loaded - long
// after nav.js has called in here. The rest of the year this costs one timer
// and one observer that find nothing to do.
//
// 1. A visitor crosses the screen now and then: a ghost at Halloween, a turkey
//    making a run for it at Thanksgiving, the sleigh at Christmas. Rare on
//    purpose - the point is somebody saying "did you just see that?".
//
// 2. When a game settles, every cell in its column pops a word for how it went
//    (TREAT / TRICK, GRAVY / DRY TURKEY, NICE / NAUGHTY). Once per game per
//    browser, remembered in localStorage, so a refresh or a week switch doesn't
//    replay it - but a game that went final while you were away still greets you.
//
// Styling lives with each theme (css/theme-*.css); the shared mechanics of the
// pop and the flyer are in css/base.css.

const SEEN_KEY = 'cfb_tot_seen_v1';

const GHOST = `<svg viewBox="0 0 60 70" aria-hidden="true"><path d="M30 2C14 2 4 14 4 30v36l8-7 7 7 6-7 5 7 5-7 6 7 7-7 8 7V30C56 14 46 2 30 2Z" fill="#efe9fb"/><ellipse cx="21" cy="28" rx="4.5" ry="6" fill="#1a0b26"/><ellipse cx="39" cy="28" rx="4.5" ry="6" fill="#1a0b26"/><ellipse cx="30" cy="44" rx="5" ry="6.5" fill="#1a0b26"/></svg>`;

// Facing right. Fan of tail feathers behind, a body, a head with a wattle, and
// two legs the run animation scissors.
const TURKEY = `<svg viewBox="0 0 80 70" aria-hidden="true">
<g transform="translate(26 30)">
<path d="M0 0L-22-20A30 30 0 0 1 -6-28Z" fill="#b45309"/><path d="M0 0L-6-28A30 30 0 0 1 10-27Z" fill="#d97706"/>
<path d="M0 0L-26-6A30 30 0 0 1 -22-20Z" fill="#92400e"/><path d="M0 0L-26 8A30 30 0 0 1 -26-6Z" fill="#ca8a04"/>
<path d="M0 0L10-27A30 30 0 0 1 22-18Z" fill="#c2410c"/></g>
<ellipse cx="36" cy="38" rx="17" ry="13" fill="#5b3a1e"/>
<path d="M46 32C50 24 52 18 56 15" stroke="#5b3a1e" stroke-width="6" stroke-linecap="round" fill="none"/>
<circle cx="57" cy="14" r="5.5" fill="#6b4a2a"/><path d="M62 13L67 15L62 16Z" fill="#f59e0b"/>
<circle cx="58.5" cy="12.5" r="1.2" fill="#111"/><path d="M60 17Q63 22 60 24Q58 21 60 17Z" fill="#dc2626"/>
<g class="st-legs" stroke="#f59e0b" stroke-width="2.4" stroke-linecap="round" fill="none">
<path class="st-leg-a" d="M32 49L29 62L25 63"/><path class="st-leg-b" d="M40 49L43 62L47 63"/></g>
</svg>`;

// Sleigh and four reindeer in moonlight silhouette, Rudolph leading with his nose lit.
const DEER = (x) => `<g transform="translate(${x} 0)"><path d="M0 22Q8 18 18 20L22 14L24 15L22 22L22 30L19 30L18 24L8 25L5 31L2 31L3 24Q-2 24 0 22Z"/><path d="M22 14L20 6M21 10L17 8M22 14L26 6M25 10L29 8" stroke="#e8eef8" stroke-width="1.4" fill="none"/></g>`;
const SLEIGH = `<svg viewBox="0 0 220 44" aria-hidden="true"><g fill="#e8eef8">
<path d="M8 20H40L46 10H52L48 26Q44 34 30 34H10Q4 34 4 28Z"/><path d="M2 38H46Q52 38 54 34" stroke="#e8eef8" stroke-width="2" fill="none"/>
<circle cx="24" cy="12" r="6"/><path d="M18 12Q24 2 30 12Z" fill="#dc2626"/>
<path d="M50 22L196 22" stroke="#e8eef8" stroke-width="1" opacity=".7"/>
${DEER(62)}${DEER(96)}${DEER(130)}${DEER(164)}</g>
<circle cx="190" cy="22" r="3" fill="#ff3b3b"/><circle cx="190" cy="22" r="7" fill="#ff3b3b" opacity=".35"/>
</svg>`;

// Sickos week: a foam finger, inked black, with the line on the palm. Same
// drawing as the one at the edge of the page (tools/theme-art/sickos.art.mjs).
const FOAM = `<svg viewBox="0 0 150 230" aria-hidden="true">
<rect x="40" y="194" width="72" height="26" rx="4" fill="#111" stroke="#f4f1e8" stroke-width="4"/>
<path d="M48 104V30Q48 12 64 12Q80 12 80 30V96Q86 90 96 92Q106 94 106 104Q112 98 120 102Q128 106 126 118V178Q126 198 104 198H46Q26 198 24 178L20 132Q18 118 30 116Q40 114 48 124Z" fill="#111" stroke="#f4f1e8" stroke-width="5" stroke-linejoin="round"/>
<path d="M88 104Q94 110 104 108M108 118Q114 124 122 120" stroke="#f4f1e8" stroke-width="3" fill="none" stroke-linecap="round"/>
<g font-family="Bangers, Impact, 'Arial Black', sans-serif" fill="#f4f1e8" text-anchor="middle" transform="rotate(-6 75 165)">
<text x="78" y="145" font-size="17">YES...</text><text x="75" y="166" font-size="18">HA HA HA</text><text x="74" y="187" font-size="17">...YES!</text>
<rect x="72" y="190" width="30" height="2.5"/></g></svg>`;

const THEMES = {
  sickos: {
    flyer: { svg: FOAM, path: 'rise', size: [80, 110], ms: [4200, 5200], cls: 'is-foam' },
    good: 'YES!', bad: 'UGLY \u{1F922}',
  },
  halloween: {
    flyer: { svg: GHOST, path: 'sky', size: [46, 64], ms: [8000, 11000], cls: 'is-ghost' },
    good: 'TREAT \u{1F36C}', bad: 'TRICK \u{1F383}',
  },
  thanksgiving: {
    flyer: { svg: TURKEY, path: 'ground', size: [54, 72], ms: [5500, 7500], cls: 'is-turkey' },
    good: 'GRAVY \u{1F357}', bad: 'DRY TURKEY \u{1F983}',
  },
  christmas: {
    flyer: { svg: SLEIGH, path: 'sky', size: [150, 220], ms: [9000, 12000], cls: 'is-sleigh' },
    good: 'NICE \u{1F381}', bad: 'NAUGHTY \u{1FAA8}',
  },
};

function currentTheme() {
  const t = document.documentElement.dataset.theme || document.body?.dataset?.theme;
  return THEMES[t] ? t : null;
}

function reducedMotion() {
  return window.matchMedia?.('(prefers-reduced-motion: reduce)')?.matches ?? false;
}

function rand(min, max) { return min + Math.random() * (max - min); }

/* ---------- The visitor ---------------------------------------------------- */

function fly() {
  const theme = currentTheme();
  if (!theme || document.hidden || reducedMotion()) return;
  const f = THEMES[theme].flyer;

  const el = document.createElement('div');
  el.className = 'st-flyer ' + f.cls;
  el.setAttribute('aria-hidden', 'true');
  el.innerHTML = f.svg;
  document.body.appendChild(el);

  const vw = window.innerWidth, vh = window.innerHeight;
  const size = vw < 640 ? f.size[0] : f.size[1];
  el.style.width = size + 'px';
  const h = el.getBoundingClientRect().height || size;

  // The foam finger comes up from below the bottom edge, pumps twice, and
  // goes back down.
  if (f.path === 'rise') {
    const x = rand(vw * 0.08, vw * 0.92 - size);
    const down = vh + 10, up = vh - h * 0.92;
    const at = (y, r) => `translate(${x}px, ${y}px) rotate(${r}deg)`;
    el.style.transformOrigin = '50% 100%';
    el.animate([
      { transform: at(down, 0), opacity: 1, offset: 0 },
      { transform: at(up, -4), opacity: 1, offset: 0.18 },
      { transform: at(up - 14, 6), opacity: 1, offset: 0.3 },
      { transform: at(up, -3), opacity: 1, offset: 0.42 },
      { transform: at(up - 14, 6), opacity: 1, offset: 0.54 },
      { transform: at(up, 0), opacity: 1, offset: 0.78 },
      { transform: at(down, 0), opacity: 1, offset: 1 },
    ], { duration: rand(f.ms[0], f.ms[1]), easing: 'ease-in-out' })
      .finished.catch(() => {}).finally(() => el.remove());
    return;
  }

  // The turkey always runs for it left to right along the bottom; the others
  // come from either side, bobbing through the sky.
  const ltr = f.path === 'ground' ? true : Math.random() < 0.5;
  const startX = ltr ? -size * 1.2 : vw + size * 0.2;
  const endX = ltr ? vw + size * 0.2 : -size * 1.2;
  const flip = ltr ? 1 : -1;
  const baseY = f.path === 'ground' ? vh - h - 6 : rand(vh * 0.12, vh * 0.5);
  const frames = [];
  const N = f.path === 'ground' ? 24 : 8;
  for (let i = 0; i <= N; i++) {
    const t = i / N;
    const y = f.path === 'ground'
      ? baseY - Math.abs(Math.sin(t * Math.PI * 12)) * 6
      : baseY + Math.sin(t * Math.PI * 3) * (size > 100 ? 14 : 26);
    const op = f.path === 'ground'
      ? (i === 0 || i === N ? 0 : 1)
      : (i === 0 || i === N ? 0 : (i % 3 === 1 ? 0.5 : 0.7));
    frames.push({ transform: `translate(${startX + (endX - startX) * t}px, ${y}px) scaleX(${flip})`, opacity: op });
  }
  el.animate(frames, { duration: rand(f.ms[0], f.ms[1]), easing: 'linear' })
    .finished.catch(() => {}).finally(() => el.remove());
}

function scheduleFly(first) {
  const delay = first ? rand(20000, 45000) : rand(70000, 180000);
  setTimeout(() => { fly(); scheduleFly(false); }, delay);
}

/* ---------- The pop as a game settles --------------------------------------- */

function loadSeen() {
  try { return new Set(JSON.parse(localStorage.getItem(SEEN_KEY) || '[]')); }
  catch { return new Set(); }
}
function saveSeen(seen) {
  try { localStorage.setItem(SEEN_KEY, JSON.stringify([...seen].slice(-200))); }
  catch { /* private mode - it may replay, which is harmless */ }
}

function popLabel(td, good, text, delayMs) {
  const r = td.getBoundingClientRect();
  if (r.bottom < 0 || r.top > window.innerHeight || r.width === 0) return;
  const tag = document.createElement('div');
  tag.className = 'st-tot ' + (good ? 'is-good' : 'is-bad');
  tag.setAttribute('aria-hidden', 'true');
  tag.textContent = text;
  tag.style.left = (r.left + r.width / 2) + 'px';
  tag.style.top = (r.top + r.height / 2) + 'px';
  tag.style.animationDelay = delayMs + 'ms';
  document.body.appendChild(tag);
  tag.addEventListener('animationend', () => tag.remove(), { once: true });
  setTimeout(() => tag.remove(), delayMs + 4000);
}

let scanTimer = 0;
function scanForResults() {
  const theme = currentTheme();
  if (!theme) return;
  const { good, bad } = THEMES[theme];
  const cells = document.querySelectorAll('td[data-game].pick-win, td[data-game].pick-loss');
  if (!cells.length) return;

  const byGame = new Map();
  cells.forEach(td => {
    const id = td.dataset.game;
    if (!byGame.has(id)) byGame.set(id, []);
    byGame.get(id).push(td);
  });

  const seen = loadSeen();
  let n = 0;
  for (const [id, tds] of byGame) {
    if (seen.has(id)) continue;
    seen.add(id);
    // Games one after another, and down each column a row at a time, so a
    // night of results arrives as a ripple rather than one flash.
    tds.forEach((td, row) => {
      const win = td.classList.contains('pick-win');
      popLabel(td, win, win ? good : bad, n * 900 + row * 70);
    });
    n++;
  }
  if (n) saveSeen(seen);
}

function watchBoard() {
  const board = document.getElementById('table-scroll-wrap');
  if (!board) return;
  new MutationObserver(() => {
    clearTimeout(scanTimer);
    scanTimer = setTimeout(scanForResults, 400);
  }).observe(board, { childList: true, subtree: true });
}

/* ---------- Entry ---------------------------------------------------------- */

let started = false;
export function initSeasonalTreats() {
  if (started) return;
  started = true;
  scheduleFly(true);
  // Not in the commissioner's sandbox: its winners are hypothetical, and a
  // pretend result must not use up the real one's moment.
  if (!new URLSearchParams(location.search).has('sandbox')) watchBoard();
}
