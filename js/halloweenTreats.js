// js/halloweenTreats.js — two small surprises for Halloween weekend.
//
// Both check the theme when they fire rather than when they start, because the
// leaderboard only learns it is Halloween once its games have loaded - long
// after nav.js has called in here. The rest of the year this costs one timer
// and one observer that find nothing to do.
//
// 1. A ghost drifts across the screen now and then. Rare on purpose: the point
//    is somebody saying "did you just see that?", not a screensaver.
//
// 2. Trick or treat. When a game settles, every cell in its column flashes
//    TREAT (your pick came in) or TRICK (it didn't). Once per game per browser,
//    remembered in localStorage, so a refresh or a week switch doesn't replay
//    it - but a game that went final while you were away still greets you.

const SEEN_KEY = 'cfb_tot_seen_v1';

function isHalloween() {
  return document.documentElement.dataset.theme === 'halloween'
    || document.body?.dataset?.theme === 'halloween';
}

function reducedMotion() {
  return window.matchMedia?.('(prefers-reduced-motion: reduce)')?.matches ?? false;
}

function rand(min, max) { return min + Math.random() * (max - min); }

/* ---------- Ghost ---------------------------------------------------------- */

const GHOST_SVG = `<svg viewBox="0 0 60 70" aria-hidden="true"><path d="M30 2C14 2 4 14 4 30v36l8-7 7 7 6-7 5 7 5-7 6 7 7-7 8 7V30C56 14 46 2 30 2Z" fill="#efe9fb"/><ellipse cx="21" cy="28" rx="4.5" ry="6" fill="#1a0b26"/><ellipse cx="39" cy="28" rx="4.5" ry="6" fill="#1a0b26"/><ellipse cx="30" cy="44" rx="5" ry="6.5" fill="#1a0b26"/></svg>`;

function flyGhost() {
  if (!isHalloween() || document.hidden || reducedMotion()) return;
  const ghost = document.createElement('div');
  ghost.className = 'hw-ghost';
  ghost.setAttribute('aria-hidden', 'true');
  ghost.innerHTML = GHOST_SVG;
  document.body.appendChild(ghost);

  const vw = window.innerWidth, vh = window.innerHeight;
  const size = vw < 640 ? 46 : 64;
  const ltr = Math.random() < 0.5;
  const y = rand(vh * 0.15, vh * 0.6);
  const startX = ltr ? -size * 1.5 : vw + size * 0.5;
  const endX = ltr ? vw + size * 0.5 : -size * 1.5;
  const flip = ltr ? 1 : -1;
  const frames = [];
  for (let i = 0; i <= 8; i++) {
    const t = i / 8;
    frames.push({
      transform: `translate(${startX + (endX - startX) * t}px, ${y + Math.sin(t * Math.PI * 3) * 26}px) scaleX(${flip})`,
      opacity: i === 0 || i === 8 ? 0 : (i % 3 === 1 ? 0.42 : 0.6),
    });
  }
  ghost.style.width = size + 'px';
  ghost.animate(frames, { duration: rand(8000, 11000), easing: 'linear' })
    .finished.catch(() => {}).finally(() => ghost.remove());
}

function scheduleGhost(first) {
  const delay = first ? rand(20000, 45000) : rand(70000, 180000);
  setTimeout(() => { flyGhost(); scheduleGhost(false); }, delay);
}

/* ---------- Trick or treat ------------------------------------------------- */

function loadSeen() {
  try { return new Set(JSON.parse(localStorage.getItem(SEEN_KEY) || '[]')); }
  catch { return new Set(); }
}
function saveSeen(seen) {
  try { localStorage.setItem(SEEN_KEY, JSON.stringify([...seen].slice(-200))); }
  catch { /* private mode - it may replay, which is harmless */ }
}

function popLabel(td, treat, delayMs) {
  const r = td.getBoundingClientRect();
  if (r.bottom < 0 || r.top > window.innerHeight || r.width === 0) return;
  const tag = document.createElement('div');
  tag.className = 'hw-tot ' + (treat ? 'is-treat' : 'is-trick');
  tag.setAttribute('aria-hidden', 'true');
  tag.textContent = treat ? 'TREAT \u{1F36C}' : 'TRICK \u{1F383}';
  tag.style.left = (r.left + r.width / 2) + 'px';
  tag.style.top = (r.top + r.height / 2) + 'px';
  tag.style.animationDelay = delayMs + 'ms';
  document.body.appendChild(tag);
  tag.addEventListener('animationend', () => tag.remove(), { once: true });
  setTimeout(() => tag.remove(), delayMs + 4000);
}

let scanTimer = 0;
function scanForResults() {
  if (!isHalloween()) return;
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
    tds.forEach((td, row) => popLabel(td, td.classList.contains('pick-win'), n * 900 + row * 70));
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
export function initHalloweenTreats() {
  if (started) return;
  started = true;
  scheduleGhost(true);
  // Not in the commissioner's sandbox: its winners are hypothetical, and a
  // pretend result must not use up the real one's moment.
  if (!new URLSearchParams(location.search).has('sandbox')) watchBoard();
}
