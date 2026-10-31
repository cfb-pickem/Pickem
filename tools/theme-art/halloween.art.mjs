// Art for css/theme-halloween.css. Each entry is an SVG string; build.mjs
// encodes it and substitutes it for __NAME__ in halloween.tpl.css.

const INK = '#07030c';
const BAT = 'M0 2C-3-2-6-3-8-1C-11-6-17-7-21-4C-18-3-16-1-16 1C-13 0-10 1-9 4C-6 2-3 3-1 6L0 4L1 6C3 3 6 2 9 4C10 1 13 0 16 1C16-1 18-3 21-4C17-7 11-6 8-1C6-3 3-2 0 2ZM-2 -1L-2.5 -4L-1 -2ZM2 -1L2.5 -4L1 -2Z';

const moon = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 200">
<defs><radialGradient id="m" cx="42%" cy="38%" r="62%"><stop offset="0" stop-color="#fff6dc"/><stop offset=".55" stop-color="#ffd79a"/><stop offset="1" stop-color="#e98f3e"/></radialGradient></defs>
<circle cx="100" cy="100" r="78" fill="url(#m)"/>
<g fill="#b8682a" opacity=".22"><circle cx="72" cy="80" r="14"/><circle cx="118" cy="62" r="8"/><circle cx="128" cy="118" r="18"/><circle cx="82" cy="132" r="9"/><circle cx="148" cy="88" r="6"/><circle cx="102" cy="100" r="5"/></g>
<g fill="#1a0b26" opacity=".72"><ellipse cx="70" cy="148" rx="78" ry="9"/><ellipse cx="150" cy="160" rx="60" ry="7"/></g>
</svg>`;

const bats = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 400 220">
<defs><path id="b" d="${BAT}" fill="${INK}"/></defs>
<use href="#b" transform="translate(120 70) scale(2.1) rotate(-8)"/>
<use href="#b" transform="translate(210 40) scale(1.4) rotate(6)"/>
<use href="#b" transform="translate(300 96) scale(1.1) rotate(-4)"/>
<use href="#b" transform="translate(60 140) scale(.9) rotate(10)"/>
<use href="#b" transform="translate(250 160) scale(.75) rotate(-12)"/>
<use href="#b" transform="translate(350 30) scale(.6)"/>
</svg>`;

// Graveyard skyline: dead tree, headstones, iron fence, a pair of goalposts
// with a ghost drifting through them, and two lit jack-o'-lanterns.
let fence = '';
for (let x = 880; x <= 1110; x += 13) {
  const y = 150 + Math.round(4*Math.sin(x/37));
  fence += `<rect x="${x}" y="${y}" width="3" height="${190-y}"/><path d="M${x-2} ${y}L${x+1.5} ${y-8}L${x+5} ${y}Z"/>`;
}
const stone = (x, h, w, r=0) => `<path transform="rotate(${r} ${x+w/2} 200)" d="M${x} 200V${200-h+w/2}A${w/2} ${w/2} 0 0 1 ${x+w} ${200-h+w/2}V200Z"/>`;
const cross = (x, h, r=0) => `<g transform="rotate(${r} ${x} 200)"><rect x="${x-3}" y="${200-h}" width="6" height="${h}"/><rect x="${x-12}" y="${200-h+10}" width="24" height="6"/></g>`;
const pumpkin = (x, y, s) => `<g transform="translate(${x} ${y}) scale(${s})">
<circle r="34" fill="url(#glow)"/>
<ellipse cx="-8" cy="0" rx="11" ry="13" fill="#b9470b"/><ellipse cx="8" cy="0" rx="11" ry="13" fill="#b9470b"/><ellipse cx="0" cy="0" rx="10" ry="14" fill="#e0620f"/>
<path d="M-1-13Q0-19 4-20" stroke="#2c3a12" stroke-width="3" fill="none"/>
<path d="M-9-4L-4-4L-6.5-8ZM9-4L4-4L6.5-8ZM-9 4Q0 11 9 4L5 5L3 3L0 6L-3 3L-5 5Z" fill="#ffe08a"/></g>`;
const skyline = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1600 240" preserveAspectRatio="xMidYMax slice">
<defs><radialGradient id="glow"><stop offset="0" stop-color="#ff9a2e" stop-opacity=".55"/><stop offset="1" stop-color="#ff7518" stop-opacity="0"/></radialGradient></defs>
<g fill="${INK}">
<path d="M0 240V196Q180 170 400 190T820 184T1240 192T1600 176V240Z"/>
${stone(330,46,30,-6)}${stone(395,34,24,4)}${cross(470,58,-3)}${stone(540,40,28,8)}${stone(640,30,22,-10)}
${cross(760,46,7)}${stone(1180,42,28,-4)}${cross(1250,52,5)}${stone(1520,36,26,6)}
${fence}<rect x="878" y="160" width="236" height="3"/><rect x="878" y="178" width="236" height="3"/>
</g>
<g stroke="${INK}" stroke-linecap="round" fill="none">
<path d="M150 210C146 160 158 118 140 62" stroke-width="15"/>
<path d="M146 130C116 108 92 104 66 72M148 102C172 86 192 74 206 40M142 76C128 52 132 38 118 16" stroke-width="7"/>
<path d="M80 88L56 80M190 66L212 60M182 80L200 92M126 40L106 30M66 72L60 50" stroke-width="3.5"/>
<path d="M1400 196V128M1350 128H1450M1350 128V30M1450 128V30" stroke-width="7"/>
</g>
<g transform="translate(1418 76)" fill="#e9e3f5" opacity=".2"><path d="M-14 22V-2A14 14 0 0 1 14-2V22L9 17L4 22L0 17L-4 22L-9 17Z"/><circle cx="-5" cy="-2" r="2.6" fill="${INK}"/><circle cx="5" cy="-2" r="2.6" fill="${INK}"/></g>
${pumpkin(600,188,1)}${pumpkin(1300,190,.8)}${pumpkin(250,196,.7)}
</svg>`;

// Corner cobweb, anchored top-left. Mirror it with a transform for the right.
let web = '';
const spokes = [0, 15, 32, 50, 68, 90].map(d => d*Math.PI/180);
for (const a of spokes) web += `M0 0L${(150*Math.cos(a)).toFixed(1)} ${(150*Math.sin(a)).toFixed(1)}`;
for (const r of [26, 52, 80, 110, 140]) {
  const pts = spokes.map(a => [r*Math.cos(a), r*Math.sin(a)]);
  web += `M${pts[0][0].toFixed(1)} ${pts[0][1].toFixed(1)}`;
  for (let i = 1; i < pts.length; i++) {
    const [px,py] = pts[i-1], [x,y] = pts[i];
    web += `Q${((px+x)/2*0.86).toFixed(1)} ${((py+y)/2*0.86).toFixed(1)} ${x.toFixed(1)} ${y.toFixed(1)}`;
  }
}
const webSvg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 150 150"><path d="${web}" stroke="#d9cfe6" stroke-width=".9" fill="none" opacity=".55"/></svg>`;

const webR = webSvg.replace('<path', '<path transform="matrix(-1 0 0 1 150 0)"');
const spider = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 40 140">
<path d="M20 0V108" stroke="#d9cfe6" stroke-width=".8" opacity=".6"/>
<g stroke="#6a4386" stroke-width="2.2" fill="none" stroke-linecap="round">
<path d="M17 114L8 106L3 110M17 117L6 115L2 121M17 120L7 125L4 132M18 123L11 132L10 138"/>
<path d="M23 114L32 106L37 110M23 117L34 115L38 121M23 120L33 125L36 132M22 123L29 132L30 138"/></g>
<ellipse cx="20" cy="112" rx="5" ry="5" fill="#55306e"/><ellipse cx="20" cy="123" rx="7" ry="9" fill="#55306e"/>
<circle cx="18" cy="111" r="1.5" fill="#ff7518"/><circle cx="22" cy="111" r="1.5" fill="#ff7518"/>
</svg>`;

const drips = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 300 26" preserveAspectRatio="none">
<path fill="#ff7518" d="M0 0H300V4C292 4 290 8 289 14C288 20 282 20 281 14C280 8 276 5 266 5C252 5 250 9 249 22C248 27 241 27 240 22C239 10 232 5 216 5C200 5 196 8 195 12C194 16 189 16 188 12C187 7 180 5 160 5C140 5 135 9 134 18C133 24 125 24 124 18C123 9 116 5 98 5C80 5 76 8 75 13C74 18 68 18 67 13C66 8 58 5 44 5C30 5 27 9 26 20C25 26 18 26 17 20C16 9 10 4 0 4Z"/>
</svg>`;


export default { MOON: moon, BATS: bats, SKYLINE: skyline, WEB: webSvg, WEBR: webR, SPIDER: spider, DRIPS: drips };
