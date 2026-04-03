// js/mascots.js — Place mascot matchup images around the scoreboard

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
  if (document.querySelector('.jumbotron-mascots')) return;

  const shuffled = shuffle(MASCOT_IMAGES);
  const scoreboard = document.getElementById('table-scroll-wrap');

  // === LEADERBOARD PAGE: mascots flank the scoreboard ===
  if (scoreboard) {
    const parent = scoreboard.parentNode;

    // Featured matchup banner above the scoreboard
    const banner = document.createElement('div');
    banner.className = 'featured-matchup';
    banner.setAttribute('aria-hidden', 'true');
    banner.appendChild(makeImg(shuffled[0], 'featured-matchup-img'));
    parent.insertBefore(banner, scoreboard);

    // Wrap scoreboard with flanking mascots
    const wrapper = document.createElement('div');
    wrapper.className = 'jumbotron-mascots';
    wrapper.setAttribute('aria-hidden', 'false');

    const leftMascot = document.createElement('div');
    leftMascot.className = 'jumbotron-side jumbotron-left';
    leftMascot.appendChild(makeImg(shuffled[1], 'jumbotron-img'));

    const rightMascot = document.createElement('div');
    rightMascot.className = 'jumbotron-side jumbotron-right';
    rightMascot.appendChild(makeImg(shuffled[2], 'jumbotron-img'));

    // Move scoreboard into the wrapper
    parent.insertBefore(wrapper, scoreboard);
    wrapper.appendChild(leftMascot);
    wrapper.appendChild(scoreboard);
    wrapper.appendChild(rightMascot);

    return;
  }

  // === OTHER PAGES: a couple of decorative mascots in the margins ===
  const main = document.querySelector('.max-w-7xl') || document.body;
  const deco = document.createElement('div');
  deco.className = 'page-mascot-deco';
  deco.setAttribute('aria-hidden', 'true');

  // One near the top-right, one near the bottom-left
  const top = makeImg(shuffled[0], 'deco-mascot deco-top');
  const bot = makeImg(shuffled[1], 'deco-mascot deco-bottom');
  deco.appendChild(top);
  deco.appendChild(bot);

  main.style.position = 'relative';
  main.appendChild(deco);
}
