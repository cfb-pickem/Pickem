// Art for css/theme-christmas.css. Each entry is an SVG string; build.mjs
// encodes it and substitutes it for __NAME__ in christmas.tpl.css.

const INK = '#04101a';
const SNOW = '#e6eef7';
const BULBS = ['#ff4d4d', '#ffd166', '#3ddc84', '#5aa9ff'];

// A cold winter moon.
const moon = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 200">
<defs><radialGradient id="m" cx="42%" cy="38%" r="62%"><stop offset="0" stop-color="#ffffff"/><stop offset=".6" stop-color="#e3ecf8"/><stop offset="1" stop-color="#a9bdd8"/></radialGradient></defs>
<circle cx="100" cy="100" r="78" fill="url(#m)"/>
<g fill="#7f97b8" opacity=".22"><circle cx="72" cy="80" r="14"/><circle cx="118" cy="62" r="8"/><circle cx="128" cy="118" r="18"/><circle cx="82" cy="132" r="9"/><circle cx="148" cy="88" r="6"/></g>
</svg>`;

// Stars, as a tile; the layer twinkles as a whole.
let starDots = '';
const pts = [[20, 30, 1.2], [90, 12, .8], [150, 60, 1.5], [230, 24, 1], [300, 70, .9], [370, 18, 1.3], [60, 110, .7], [190, 130, 1.1], [270, 150, .8], [340, 120, 1.4], [120, 190, 1], [30, 220, .9], [250, 230, 1.2], [380, 240, .7]];
for (const [x, y, r] of pts) starDots += `<circle cx="${x}" cy="${y}" r="${r}" fill="#fff" opacity="${(.45 + r / 3).toFixed(2)}"/>`;
const stars = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 400 260">${starDots}</svg>`;

// Snowy pine forest, a cabin with the lights on and smoke going up, a snowman,
// a string of lights on the eaves, and snow on the crossbar of the goalposts.
const pine = (x, h, s = 1) => {
  const w = h * .42 * s, y = 206;
  return `<g transform="translate(${x} ${y})">
<path d="M0 ${-h}L${w * .55} ${-h * .62}H${w * .3}L${w * .8} ${-h * .3}H${w * .45}L${w} 0H${-w}L${-w * .45} ${-h * .3}H${-w * .8}L${-w * .3} ${-h * .62}H${-w * .55}Z" fill="${INK}"/>
<path d="M0 ${-h}L${w * .3} ${-h * .78}L0 ${-h * .82}L${-w * .3} ${-h * .78}Z" fill="${SNOW}"/>
<path d="M${-w * .55} ${-h * .62}Q0 ${-h * .56} ${w * .55} ${-h * .62}L${w * .3} ${-h * .66}Q0 ${-h * .62} ${-w * .3} ${-h * .66}Z" fill="${SNOW}" opacity=".9"/>
<path d="M${-w * .8} ${-h * .3}Q0 ${-h * .24} ${w * .8} ${-h * .3}L${w * .45} ${-h * .34}Q0 ${-h * .3} ${-w * .45} ${-h * .34}Z" fill="${SNOW}" opacity=".85"/>
<rect x="-3" y="0" width="6" height="6" fill="${INK}"/></g>`;
};
let lights = '';
for (let i = 0; i < 9; i++) {
  const x = 1132 + i * 11, y = 150 + Math.sin(i * 1.1) * 1.5 + 3;
  lights += `<circle cx="${x}" cy="${y}" r="2.4" fill="${BULBS[i % 4]}"/><circle cx="${x}" cy="${y}" r="5" fill="${BULBS[i % 4]}" opacity=".3"/>`;
}
const skyline = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1600 240" preserveAspectRatio="xMidYMax slice">
${pine(60, 150)}${pine(130, 110)}${pine(200, 170)}${pine(800, 120)}${pine(870, 150)}${pine(940, 100)}${pine(1340, 140)}${pine(1420, 175)}${pine(1510, 120)}
<g stroke="${INK}" stroke-linecap="round" fill="none" stroke-width="7"><path d="M400 210V130M350 130H450M350 130V40M450 130V40"/></g>
<path d="M346 127H454V132H346Z" fill="${SNOW}"/><path d="M346 128Q360 122 380 127T420 126T454 127" fill="${SNOW}"/>
<g fill="${INK}"><path d="M1120 206V150H1240V206Z"/><rect x="1210" y="112" width="16" height="30"/></g>
<path d="M1110 154L1180 112L1250 154Z" fill="${INK}"/><path d="M1110 154L1180 112L1250 154L1242 156L1180 120L1118 156Z" fill="${SNOW}"/>
<path d="M1208 112h20v-5h-20z" fill="${SNOW}"/>
<g fill="#ffd27a"><rect x="1136" y="166" width="22" height="18"/><rect x="1202" y="166" width="22" height="18"/></g>
<path d="M1147 166V184M1136 175H1158M1213 166V184M1202 175H1224" stroke="${INK}" stroke-width="2"/>
<rect x="1170" y="170" width="20" height="36" fill="#2a1a10"/>
<circle cx="1180" cy="175" r="40" fill="#ffd27a" opacity=".1"/>
${lights}
<g fill="#cfd9e6" opacity=".16"><circle cx="1220" cy="100" r="7"/><circle cx="1228" cy="86" r="9"/><circle cx="1222" cy="68" r="11"/><circle cx="1234" cy="50" r="13"/></g>
<g transform="translate(560 206)"><circle cx="0" cy="-16" r="17" fill="${SNOW}"/><circle cx="0" cy="-44" r="12" fill="${SNOW}"/><circle cx="0" cy="-64" r="9" fill="${SNOW}"/>
<rect x="-9" y="-80" width="18" height="12" fill="${INK}"/><rect x="-13" y="-70" width="26" height="3" fill="${INK}"/>
<path d="M0-63L10-61L0-60Z" fill="#f97316"/><circle cx="-3" cy="-66" r="1.4" fill="${INK}"/><circle cx="3" cy="-66" r="1.4" fill="${INK}"/>
<path d="M-11-46L-30-58M-30-58L-34-56M-30-58L-31-63M11-46L30-56" stroke="#3b2a1a" stroke-width="2" fill="none"/>
<path d="M-10-55Q0-50 10-55L10-51Q0-46-10-51Z" fill="#dc2626"/></g>
<path d="M0 240V206Q240 192 520 202T1060 198T1600 200V240Z" fill="${SNOW}"/>
<path d="M0 240V214Q300 206 640 212T1300 208T1600 212V240Z" fill="#c8d6e8" opacity=".6"/>
</svg>`;

// Holly for the board's top corners, anchored top-left.
const hollyLeaf = (x, y, r) => `<path transform="translate(${x} ${y}) rotate(${r})" d="M0 0Q6-6 12-3Q14-9 20-8Q20-2 26 0Q20 2 20 8Q14 9 12 3Q6 6 0 0Z" fill="#1f7a3e" stroke="#0e4a24" stroke-width=".8"/>`;
const holly = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 110 110"><g opacity=".85">
${hollyLeaf(8, 14, 10)}${hollyLeaf(10, 18, 55)}${hollyLeaf(22, 8, -12)}${hollyLeaf(14, 26, 80)}
<circle cx="12" cy="14" r="4.2" fill="#d32f2f"/><circle cx="19" cy="12" r="3.8" fill="#e53935"/><circle cx="16" cy="19" r="3.8" fill="#c62828"/>
<circle cx="11" cy="13" r="1.1" fill="#fff" opacity=".7"/></g></svg>`;
const hollyR = holly.replace('<g opacity', '<g transform="matrix(-1 0 0 1 110 0)" opacity');

// Snow piled along the top of the board.
const drift = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 300 18" preserveAspectRatio="none">
<path fill="${SNOW}" d="M0 0H300V6C288 6 284 11 270 11C258 11 256 7 240 7C228 7 226 13 208 13C192 13 190 8 172 8C160 8 156 12 140 12C126 12 122 6 106 6C94 6 90 11 74 11C60 11 58 7 44 7C30 7 28 12 14 12C6 12 3 8 0 6Z"/>
<path fill="#ffffff" opacity=".6" d="M0 0H300V2H0Z"/></svg>`;

// A light string hanging in swags, bulbs with a glow.
const bulb = (x, y, c) => `<circle cx="${x}" cy="${y + 6}" r="7" fill="${c}" opacity=".28"/><rect x="${x - 1.8}" y="${y - 1}" width="3.6" height="3" fill="#2d3b2f"/><ellipse cx="${x}" cy="${y + 5}" rx="3" ry="4.2" fill="${c}"/><ellipse cx="${x - 1}" cy="${y + 3.6}" rx=".9" ry="1.4" fill="#fff" opacity=".7"/>`;
const string = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 120 28">
<path d="M0 4Q30 16 60 4T120 4" stroke="#1d3a26" stroke-width="1.4" fill="none"/>
${bulb(15, 8.5, BULBS[0])}${bulb(45, 8.5, BULBS[1])}${bulb(75, 8.5, BULBS[2])}${bulb(105, 8.5, BULBS[3])}
</svg>`;

// An ornament on a ribbon, hanging over the title.
const ornament = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 40 120">
<defs><radialGradient id="o" cx="38%" cy="35%" r="65%"><stop offset="0" stop-color="#ff8a8a"/><stop offset=".5" stop-color="#d32f2f"/><stop offset="1" stop-color="#7f1010"/></radialGradient></defs>
<path d="M20 0V86" stroke="#e8c37a" stroke-width="1.2"/>
<rect x="15" y="84" width="10" height="6" rx="1" fill="#e8c37a"/>
<circle cx="20" cy="103" r="15" fill="url(#o)"/>
<path d="M6 100Q20 106 34 100" stroke="#ffe0a3" stroke-width="1.6" fill="none" opacity=".9"/>
<circle cx="15" cy="97" r="3" fill="#fff" opacity=".55"/>
</svg>`;

export default { MOON: moon, STARS: stars, SKYLINE: skyline, HOLLY: holly, HOLLYR: hollyR, DRIFT: drift, LIGHTS: string, ORNAMENT: ornament };
