// js/mascots.js — Injects floating mascot emojis for the NCAA game-day feel

const MASCOTS = [
  '🐅', // Tiger (Clemson, LSU, Auburn, Missouri)
  '🐊', // Gator (Florida)
  '🐻', // Bear (Baylor, Cal)
  '🦅', // Eagle (Boston College, Auburn)
  '🐺', // Wolf (NC State)
  '🐗', // Boar/Razorback (Arkansas)
  '🐴', // Horse (SMU, Broncos)
  '🐶', // Bulldog (Georgia, Mississippi St)
  '🦬', // Bison (Colorado)
  '🐏', // Ram (Colorado State)
  '🦉', // Owl (Temple, Rice)
  '🐝', // Bee/Hornet (Georgia Tech)
];

export function initMascots() {
  // Don't add if already present
  if (document.querySelector('.mascot-layer')) return;

  const layer = document.createElement('div');
  layer.className = 'mascot-layer';
  layer.setAttribute('aria-hidden', 'true');

  // Pick 12 mascots, shuffled
  const shuffled = [...MASCOTS].sort(() => Math.random() - 0.5);

  for (let i = 0; i < 12; i++) {
    const el = document.createElement('span');
    el.className = `mascot mascot-${i + 1}`;
    el.textContent = shuffled[i % shuffled.length];
    layer.appendChild(el);
  }

  document.body.prepend(layer);
}
