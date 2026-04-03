// js/mascots.js — Build a jumbotron frame with mascots above the scoreboard

const MASCOT_IMAGES = [
  'alabama-vs-georgia.png',
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

function shuffle(arr) {
  const a = [...arr];
  for (let i = a.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [a[i], a[j]] = [a[j], a[i]];
  }
  return a;
}

function makeImg(src, cls) {
  const img = document.createElement('img');
  img.src = './mascots/' + src;
  img.alt = '';
  img.loading = 'lazy';
  img.draggable = false;
  img.className = cls;
  return img;
}

export function initMascots() {
  if (document.querySelector('.jumbotron-header')) return;

  const shuffled = shuffle(MASCOT_IMAGES);
  const scoreboard = document.getElementById('table-scroll-wrap');

  // === LEADERBOARD PAGE: jumbotron header with mascots + title ===
  if (scoreboard) {
    const header = document.createElement('div');
    header.className = 'jumbotron-header';

    // Left mascot
    header.appendChild(makeImg(shuffled[0], 'jumbotron-mascot jumbotron-mascot-left'));

    // Title
    const title = document.createElement('div');
    title.className = 'jumbotron-title';
    const page = document.body?.dataset?.page || '';
    const heading = page === 'tiebreakers' ? 'CFB Pick\'em Tiebreakers' : 'CFB Pick\'em Leaderboard';
    title.innerHTML = '<span class="jumbotron-title-text">' + heading + '</span>';
    header.appendChild(title);

    // Right mascot
    header.appendChild(makeImg(shuffled[1], 'jumbotron-mascot jumbotron-mascot-right'));

    scoreboard.parentNode.insertBefore(header, scoreboard);

    return;
  }

  // === OTHER PAGES: subtle decorative mascots ===
  const main = document.querySelector('.max-w-7xl') || document.body;
  const deco = document.createElement('div');
  deco.className = 'page-mascot-deco';
  deco.setAttribute('aria-hidden', 'true');
  deco.appendChild(makeImg(shuffled[0], 'deco-mascot deco-top'));
  deco.appendChild(makeImg(shuffled[1], 'deco-mascot deco-bottom'));
  main.style.position = 'relative';
  main.appendChild(deco);
}
