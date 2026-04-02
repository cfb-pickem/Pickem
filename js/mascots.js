// js/mascots.js — Floating cartoon mascot matchup images

const MASCOT_IMAGES = [
  'alabama-vs-georgia.png',
  'army-vs-navy.png',
  'auburn-vs-alabama.png',
  'colorado-vs-arizonastate.png',
  'florida-vs-fsu.png',
  'florida-vs-fsu2.png',
  'florida-vs-georgia.png',
  'lsu-vs-alabama.png',
  'miami-vs-notredame.png',
  'notredame-vs-michiganstate.png',
  'ohiostate-vs-michigan.png',
  'ohiostate-vs-oregon.png',
  'olemiss-vs-lsu.png',
  'pennstate-vs-ohiostate.png',
  'southcarolina-vs-clemson.png',
  'tennessee-vs-georgia.png',
  'texas-vs-oklahoma.png',
  'texas-vs-texasam.png',
  'wisconsin-vs-iowa.png',
];

export function initMascots() {
  if (document.querySelector('.mascot-layer')) return;

  const layer = document.createElement('div');
  layer.className = 'mascot-layer';
  layer.setAttribute('aria-hidden', 'true');

  // Shuffle and pick 8 for the background
  const shuffled = [...MASCOT_IMAGES].sort(() => Math.random() - 0.5);
  const count = Math.min(8, shuffled.length);

  for (let i = 0; i < count; i++) {
    const img = document.createElement('img');
    img.src = './mascots/' + shuffled[i];
    img.alt = '';
    img.loading = 'lazy';
    img.draggable = false;
    img.className = `mascot mascot-${i + 1}`;
    layer.appendChild(img);
  }

  document.body.prepend(layer);
}
