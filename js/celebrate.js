// js/celebrate.js — what happens when somebody pops the football.
//
// THIS RUNS ABOUT ONCE A YEAR. At a quarter of a percent a pull climbing to a
// ceiling of ten, with a league that pulls the lever a handful of times a week,
// a jackpot lands once or twice a season and the same person will almost
// certainly never see two. Everything else in this feature is built to be
// watched fifty times without wearing out; this is built to be watched once and
// remembered, which is the opposite problem and wants the opposite restraint.
//
// So it takes the whole screen, it says the name out loud, and it goes on for
// six seconds - all of which would be intolerable weekly and is exactly right
// annually.
//
// Confetti is a canvas rather than elements. A hundred and forty divs being
// transformed every frame is a hundred and forty style recalculations; a canvas
// is one. It also means the pieces can tumble properly - each has its own spin
// and its own flutter - which is the difference between confetti and rain.

const COLOURS = ['#ffc65a', '#f3d27a', '#ffffff', '#c1121f', '#2a5bd7', '#8a640a'];

function reducedMotion() {
  return window.matchMedia?.('(prefers-reduced-motion: reduce)')?.matches ?? false;
}

/**
 * The name to put on the banner.
 *
 * Read off the row rather than passed in, because the cell already knows which
 * row it is in and nothing else in the reveal carries a team name. Falls back to
 * saying nothing at all, which is better than saying "undefined" on the one
 * screen anybody will screenshot.
 */
export function playerNameFor(td) {
  const row = td?.closest?.('tr');
  const cell = row?.children?.[0];
  const name = (cell?.textContent || '').trim();
  return name.length && name.length < 40 ? name : '';
}

function confetti(canvas, ms) {
  const ctx = canvas.getContext?.('2d');
  if (!ctx) return () => {};
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  const w = canvas.clientWidth, h = canvas.clientHeight;
  canvas.width = w * dpr;
  canvas.height = h * dpr;
  ctx.scale(dpr, dpr);

  // Launched from the top edge across the full width, with enough spread in the
  // fall speeds that they never arrive as a curtain.
  const bits = [];
  for (let i = 0; i < 140; i++) {
    bits.push({
      x: Math.random() * w,
      y: -20 - Math.random() * h * 0.6,
      vx: (Math.random() - 0.5) * 1.6,
      vy: 1.8 + Math.random() * 3.4,
      w: 5 + Math.random() * 7,
      h: 8 + Math.random() * 10,
      rot: Math.random() * Math.PI * 2,
      spin: (Math.random() - 0.5) * 0.28,
      flutter: Math.random() * Math.PI * 2,
      colour: COLOURS[i % COLOURS.length],
    });
  }

  let raf = 0;
  const t0 = performance.now();
  const frame = now => {
    const life = now - t0;
    ctx.clearRect(0, 0, w, h);
    // They stop being thrown a second before the end, so the screen is empty
    // again by the time the overlay lifts rather than cut off mid-fall.
    const fade = life > ms - 1200 ? Math.max(0, (ms - life) / 1200) : 1;
    ctx.globalAlpha = fade;
    for (const b of bits) {
      b.x += b.vx + Math.sin(b.flutter) * 0.8;
      b.y += b.vy;
      b.rot += b.spin;
      b.flutter += 0.08;
      if (b.y > h + 30) { b.y = -20; b.x = Math.random() * w; }
      ctx.save();
      ctx.translate(b.x, b.y);
      ctx.rotate(b.rot);
      ctx.fillStyle = b.colour;
      // Scaled on the short axis by the spin, so each piece reads as a flat
      // rectangle tumbling rather than a lozenge rotating.
      ctx.fillRect(-b.w / 2, -b.h / 2, b.w, b.h * Math.abs(Math.cos(b.rot)));
      ctx.restore();
    }
    if (life < ms) raf = requestAnimationFrame(frame);
  };
  raf = requestAnimationFrame(frame);
  return () => cancelAnimationFrame(raf);
}

/**
 * Take the screen for six seconds.
 *
 * Returns a promise that settles when it is over, so the reveal can wait for it
 * rather than tearing the board down underneath a celebration.
 */
export default function celebrateJackpot(name) {
  const MS = 6200;
  const el = document.createElement('div');
  el.className = 'jp-party';
  el.setAttribute('role', 'status');
  el.innerHTML =
    '<canvas class="jp-party-confetti" aria-hidden="true"></canvas>' +
    '<div class="jp-party-mid">' +
      '<div class="jp-party-kicker">The football finally went</div>' +
      '<div class="jp-party-title" data-text="AUTO WIN">AUTO WIN</div>' +
      (name ? '<div class="jp-party-name">' + name + '</div>' : '') +
      '<div class="jp-party-sub">The house pays. That pick is good whatever the game does.</div>' +
    '</div>';
  document.body.appendChild(el);

  if (reducedMotion()) {
    el.dataset.state = 'in';
    return new Promise(res => setTimeout(() => { el.remove(); res(); }, 2600));
  }

  let stop = () => {};
  requestAnimationFrame(() => {
    el.dataset.state = 'in';
    stop = confetti(el.querySelector('.jp-party-confetti'), MS);
  });

  return new Promise(res => {
    setTimeout(() => { el.dataset.state = 'out'; }, MS - 700);
    setTimeout(() => { stop(); el.remove(); res(); }, MS);
  });
}
