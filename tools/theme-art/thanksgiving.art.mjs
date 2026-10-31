// Art for css/theme-thanksgiving.css. Each entry is an SVG string; build.mjs
// encodes it and substitutes it for __NAME__ in thanksgiving.tpl.css.

const INK = '#140b05';
const LEAF_COLOURS = ['#c2410c', '#d97706', '#b45309', '#ca8a04', '#9a3412', '#a16207'];

const MAPLE = 'M0-14L3-7L8-9L6-3L13-3L8 2L10 6L3 4L1 12L0 8L-1 12L-3 4L-10 6L-8 2L-13-3L-6-3L-8-9L-3-7Z';
const OAK = 'M0-13C3-11 6-10 5-6C8-6 9-3 6-1C9 0 9 4 5 4C6 8 3 10 0 13C-3 10-6 8-5 4C-9 4-9 0-6-1C-9-3-8-6-5-6C-6-10-3-11 0-13Z';
const leaf = (x, y, r, s, c, shape = MAPLE) =>
  `<g transform="translate(${x} ${y}) rotate(${r}) scale(${s})"><path d="${shape}" fill="${c}"/><path d="M0 12V-8" stroke="#3b1d06" stroke-width=".8" opacity=".5"/></g>`;

// Low harvest sun with streaks of cloud across it.
const sun = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 200">
<defs><radialGradient id="s" cx="45%" cy="40%" r="60%"><stop offset="0" stop-color="#fff1c2"/><stop offset=".45" stop-color="#ffb347"/><stop offset="1" stop-color="#e2581c"/></radialGradient></defs>
<circle cx="100" cy="100" r="78" fill="url(#s)"/>
<g fill="#3a1a0a" opacity=".55"><rect x="0" y="118" width="170" height="7" rx="3.5"/><rect x="40" y="136" width="160" height="9" rx="4.5"/><rect x="20" y="158" width="140" height="6" rx="3"/></g>
</svg>`;

// Geese in a loose V.
const GOOSE = 'M-14-2Q-7-7 0 0Q7-7 14-2Q7-3 0 2Q-7-3-14-2Z';
const geesePos = [[200, 40, 1.2], [176, 54, 1], [152, 68, .95], [128, 82, .9], [104, 96, .85], [224, 56, 1], [248, 70, .95], [272, 84, .9], [296, 98, .85]];
const geese = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 400 140">
${geesePos.map(([x, y, s], i) => `<path d="${GOOSE}" fill="${INK}" transform="translate(${x} ${y}) scale(${s}) rotate(${i % 2 ? 4 : -4})"/>`).join('')}
</svg>`;

// Falling-leaf tile. Two densities so the layers read as depth.
const leavesNear = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 560 560">
${leaf(60, 70, 20, 1.15, LEAF_COLOURS[0])}${leaf(330, 40, -35, .95, LEAF_COLOURS[1], OAK)}${leaf(500, 230, 70, 1.05, LEAF_COLOURS[2])}
${leaf(170, 330, -60, 1.25, LEAF_COLOURS[3], OAK)}${leaf(410, 470, 15, .85, LEAF_COLOURS[4])}${leaf(40, 500, 130, .9, LEAF_COLOURS[5], OAK)}
</svg>`;
const leavesFar = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 380 380">
${leaf(50, 60, 45, .55, LEAF_COLOURS[5])}${leaf(260, 150, -20, .5, LEAF_COLOURS[1], OAK)}${leaf(140, 300, 100, .6, LEAF_COLOURS[2])}
</svg>`;

// The farm: rolling field, corn, a fence, a scarecrow, a turkey, hay bales,
// a barn with its loft lit, a silo, pumpkins, and goalposts at the edge of the field.
let corn = '';
for (let i = 0; i < 9; i++) {
  const x = 40 + i * 11 + (i % 2) * 3, h = 70 + (i * 37 % 25);
  corn += `<path d="M${x} 205V${205 - h}" stroke-width="3"/><path d="M${x} ${205 - h * .55}q-12-4-16-14M${x} ${205 - h * .75}q12-4 15-13M${x} ${205 - h * .35}q11-2 14-10" stroke-width="2"/>`;
}
let fence = '';
for (let x = 560; x <= 760; x += 28) fence += `<rect x="${x}" y="168" width="5" height="34"/>`;
const bale = (x, r) => `<circle cx="${x}" cy="${206 - r}" r="${r}"/><circle cx="${x}" cy="${206 - r}" r="${r * .55}" fill="none" stroke="#2a1708" stroke-width="2"/>`;
const pumpkin = (x, s) => `<g transform="translate(${x} 200) scale(${s})"><ellipse cx="-6" cy="0" rx="9" ry="10" fill="#b45309"/><ellipse cx="6" cy="0" rx="9" ry="10" fill="#b45309"/><ellipse cx="0" cy="0" rx="8" ry="11" fill="#d97706"/><path d="M0-10Q1-15 4-16" stroke="#3f4a14" stroke-width="2.5" fill="none"/></g>`;
const skyline = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1600 240" preserveAspectRatio="xMidYMax slice">
<defs><radialGradient id="lg"><stop offset="0" stop-color="#ffb347" stop-opacity=".35"/><stop offset="1" stop-color="#ffb347" stop-opacity="0"/></radialGradient></defs>
<circle cx="1110" cy="118" r="60" fill="url(#lg)"/>
<g fill="${INK}">
<path d="M0 240V204Q260 178 520 198T1040 192T1600 186V240Z"/>
${fence}<rect x="556" y="176" width="210" height="4"/><rect x="556" y="190" width="210" height="4"/>
${bale(860, 18)}${bale(900, 14)}${bale(1440, 16)}
<path d="M1060 206V128L1110 96L1160 128V206Z"/><path d="M1050 132L1110 88L1170 132L1162 136L1110 98L1058 136Z"/>
<rect x="1172" y="110" width="34" height="96"/><path d="M1172 110A17 17 0 0 1 1206 110Z"/>
</g>
<rect x="1100" y="112" width="20" height="16" fill="#ffb347"/><rect x="1100" y="112" width="20" height="16" fill="none" stroke="${INK}" stroke-width="2"/>
<path d="M1110 112V128M1100 120H1120" stroke="${INK}" stroke-width="1.5"/>
<rect x="1094" y="150" width="32" height="56" fill="#2a1708"/><path d="M1094 150L1126 206M1126 150L1094 206" stroke="${INK}" stroke-width="2"/>
<g stroke="${INK}" stroke-linecap="round" fill="none">${corn}</g>
<g stroke="${INK}" stroke-linecap="round" fill="none" stroke-width="4">
<path d="M480 204V128M454 150H506"/><path d="M466 150L462 172M494 150L498 172" stroke-width="3"/>
<path d="M220 196V130M180 130H260M180 130V40M260 130V40" stroke-width="7"/></g>
<g fill="${INK}"><circle cx="480" cy="120" r="9"/><path d="M466 114H494L486 104H474Z"/><path d="M468 150H492L496 176H464Z"/></g>
<g transform="translate(780 176)" fill="${INK}"><path d="M0 0L-16-18A24 24 0 0 1 4-24Z"/><path d="M0 0L4-24A24 24 0 0 1 18-14Z"/><path d="M0 0L-22-4A24 24 0 0 1 -16-18Z"/>
<ellipse cx="8" cy="6" rx="13" ry="10"/><path d="M16 2Q20-8 23-11" stroke="${INK}" stroke-width="4" fill="none" stroke-linecap="round"/><circle cx="24" cy="-12" r="4"/>
<path d="M5 15L4 28M11 15L12 28" stroke="${INK}" stroke-width="2"/></g>
${pumpkin(380, 1)}${pumpkin(404, .7)}${pumpkin(1260, .9)}${pumpkin(950, .8)}
</svg>`;

// Wheat sheaf for the board's top corners, anchored top-left.
let wheat = '';
for (let i = 0; i < 6; i++) {
  const a = (8 + i * 13) * Math.PI / 180, L = 120 - i * 4;
  const x = Math.cos(a) * L, y = Math.sin(a) * L;
  wheat += `<path d="M0 0L${x.toFixed(1)} ${y.toFixed(1)}" stroke="#d9a441" stroke-width="1.4"/>`;
  for (let k = 0; k < 5; k++) {
    const t = .72 + k * .06, gx = x * t, gy = y * t;
    wheat += `<ellipse cx="${gx.toFixed(1)}" cy="${gy.toFixed(1)}" rx="4.2" ry="1.9" transform="rotate(${(a * 180 / Math.PI + 25).toFixed(0)} ${gx.toFixed(1)} ${gy.toFixed(1)})" fill="#e8b84f"/>`;
    wheat += `<ellipse cx="${gx.toFixed(1)}" cy="${gy.toFixed(1)}" rx="4.2" ry="1.9" transform="rotate(${(a * 180 / Math.PI - 25).toFixed(0)} ${gx.toFixed(1)} ${gy.toFixed(1)})" fill="#d9a441"/>`;
  }
}
const wheatSvg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 130 130"><g opacity=".7">${wheat}</g></svg>`;
const wheatR = wheatSvg.replace('<g opacity', '<g transform="matrix(-1 0 0 1 130 0)" opacity');

// A swag of twine with leaves hanging off it, tiled along the top of the board.
const garland = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 160 30">
<path d="M0 3Q40 14 80 3T160 3" stroke="#8a5a2b" stroke-width="1.5" fill="none"/>
${leaf(20, 13, 160, .75, LEAF_COLOURS[0])}${leaf(50, 14, 200, .7, LEAF_COLOURS[3], OAK)}${leaf(80, 9, 180, .8, LEAF_COLOURS[1])}
${leaf(110, 14, 165, .7, LEAF_COLOURS[2], OAK)}${leaf(140, 13, 195, .75, LEAF_COLOURS[4])}
</svg>`;

export default { SUN: sun, GEESE: geese, LEAVES_NEAR: leavesNear, LEAVES_FAR: leavesFar, SKYLINE: skyline, WHEAT: wheatSvg, WHEATR: wheatR, GARLAND: garland };
