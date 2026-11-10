// Art for css/theme-sickos.css. Each entry is an SVG string; build.mjs
// encodes it and substitutes it for __NAME__ in sickos.tpl.css.
//
// All original drawings - an homage to the Sickos Committee spirit (a dark
// window, someone peering through the blinds at a game nobody else is watching),
// not a copy of anyone's art.

const INK = '#06080b';
const SLAT = '#8e9aab';
const SKIN = '#c79b74';

// Someone in a dark room parting the blinds to look out. The slats are lit by
// the TV behind them; two fingers pull one down, and there are the eyes.
let slats = '';
for (let y = 22; y < 250; y += 13) {
  if (y > 104 && y < 150) continue;   // the gap
  slats += `<rect x="18" y="${y}" width="184" height="8" rx="1.5" fill="${SLAT}"/><rect x="18" y="${y + 6}" width="184" height="2" fill="#5b6574"/>`;
}
const peeper = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 220 270">
<defs><linearGradient id="tv" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#2d4d63"/><stop offset="1" stop-color="#15222d"/></linearGradient></defs>
<rect x="6" y="8" width="208" height="254" rx="4" fill="${INK}" stroke="#2b313b" stroke-width="4"/>
<rect x="14" y="16" width="192" height="238" fill="url(#tv)"/>
${slats}
<path d="M18 104H202V112Q150 108 110 118T18 112Z" fill="${SLAT}"/>
<path d="M18 150H202V142Q150 146 110 136T18 142Z" fill="${SLAT}"/>
<path d="M18 112Q70 119 110 118T202 112V142Q150 146 110 136T18 142Z" fill="#0b1218"/>
<g transform="translate(110 128)">
<path d="M-46-2Q-30-12-14-4" stroke="#1b1410" stroke-width="3.5" fill="none" stroke-linecap="round"/>
<path d="M14-4Q30-12 46-2" stroke="#1b1410" stroke-width="3.5" fill="none" stroke-linecap="round"/>
<ellipse cx="-29" cy="5" rx="11" ry="6.5" fill="#f3efe6"/><ellipse cx="29" cy="5" rx="11" ry="6.5" fill="#f3efe6"/>
<circle cx="-26" cy="5.5" r="4.4" fill="#2a1d14"/><circle cx="32" cy="5.5" r="4.4" fill="#2a1d14"/>
<circle cx="-24.5" cy="4" r="1.4" fill="#fff"/><circle cx="33.5" cy="4" r="1.4" fill="#fff"/>
</g>
<g fill="${SKIN}" stroke="#8a6448" stroke-width="1.2">
<rect x="84" y="96" width="13" height="26" rx="6.5"/><rect x="101" y="94" width="13" height="27" rx="6.5"/>
<rect x="118" y="97" width="12" height="24" rx="6"/></g>
<rect x="6" y="8" width="208" height="254" rx="4" fill="none" stroke="#3a414c" stroke-width="2"/>
</svg>`;

// Film grain: fractal noise, tiled. Rendered once by the browser, then just moved.
const grain = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 220 220">
<filter id="n"><feTurbulence type="fractalNoise" baseFrequency=".85" numOctaves="2" stitchTiles="stitch"/><feColorMatrix values="0 0 0 0 1  0 0 0 0 1  0 0 0 0 1  0 0 0 .5 0"/></filter>
<rect width="220" height="220" filter="url(#n)"/></svg>`;

// The stadium at the foot of the page: a half-full set of bleachers, two light
// towers (one with half its lamps out), drizzle in the light, a scoreboard
// reading 3-2 with seven seconds left, the goalposts, and one fan with a sign.
// The crowd: every seat on the steps that has someone in it has someone with a
// foam finger up. Light ink so they read against the dark stands.
let fans = '';
const steps = [[40, 186], [90, 172], [140, 158], [190, 144], [240, 130]];
const fanAt = (x, y, up) => `<g transform="translate(${x} ${y})"><circle cy="-11" r="4.2" fill="#3a3a3a"/><rect x="-4.5" y="-7" width="9" height="9" rx="3" fill="#3a3a3a"/>${
  up ? `<path d="M3-6L7-18" stroke="#3a3a3a" stroke-width="2.4" stroke-linecap="round"/><path d="M5.5-19V-27Q5.5-30 7.5-30Q9.5-30 9.5-27V-21L11-21Q12-17 9-15H5Z" fill="#d8d4ca"/>` : ''}</g>`;
let k = 0;
for (const [sx, sy] of steps) for (let x = sx + 6; x < sx + 48; x += 11) fans += fanAt(x, sy, (k++ % 3) !== 1);
for (const [sx, sy] of steps) for (let x = 1600 - sx - 6; x > 1600 - sx - 48; x -= 11) fans += fanAt(x, sy, (k++ % 3) !== 1);
const lamps = (x, y, dead = []) => {
  let s = '';
  for (let r = 0; r < 2; r++) for (let c = 0; c < 4; c++) {
    const i = r * 4 + c, on = !dead.includes(i);
    s += `<rect x="${x + c * 11}" y="${y + r * 9}" width="9" height="7" rx="1" fill="${on ? '#fff4c2' : '#3a3a33'}"/>`;
  }
  return s;
};
let rain = '';
for (let i = 0; i < 26; i++) {
  const x = 480 + (i * 37) % 160, y = 70 + (i * 53) % 120;
  rain += `<path d="M${x} ${y}l-4 12" stroke="#cfe4ff" stroke-width="1" opacity=".35"/>`;
  const x2 = 1080 + (i * 41) % 160, y2 = 70 + (i * 47) % 120;
  rain += `<path d="M${x2} ${y2}l-4 12" stroke="#cfe4ff" stroke-width="1" opacity=".22"/>`;
}
const stadium = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1600 240" preserveAspectRatio="xMidYMax slice">
<defs>
<radialGradient id="cone"><stop offset="0" stop-color="#fff4c2" stop-opacity=".28"/><stop offset="1" stop-color="#fff4c2" stop-opacity="0"/></radialGradient>
<radialGradient id="cone2"><stop offset="0" stop-color="#fff4c2" stop-opacity=".12"/><stop offset="1" stop-color="#fff4c2" stop-opacity="0"/></radialGradient>
</defs>
<ellipse cx="560" cy="120" rx="170" ry="120" fill="url(#cone)"/><ellipse cx="1160" cy="120" rx="150" ry="110" fill="url(#cone2)"/>
${rain}
<g fill="${INK}">
<path d="M0 240V200H40V186H90V172H140V158H190V144H240V130H300V200H380V240Z"/>
<path d="M1600 240V200H1560V186H1510V172H1460V158H1410V144H1360V130H1300V200H1220V240Z"/>
<rect x="556" y="70" width="8" height="140"/><rect x="1156" y="70" width="8" height="140"/>
<rect x="534" y="52" width="52" height="24" rx="2"/><rect x="1134" y="52" width="52" height="24" rx="2"/>
</g>
${lamps(538, 56)}${lamps(1138, 56, [1, 2, 5, 7])}
${fans}
<g transform="translate(250 150)"><circle r="5" fill="#2a313c"/><rect x="-5" y="5" width="10" height="12" rx="3" fill="#2a313c"/>
<path d="M-3 2V-14M3 2V-14" stroke="#2a313c" stroke-width="2"/>
<rect x="-17" y="-34" width="34" height="20" rx="1" fill="#f3efe6"/><text x="0" y="-19" font-family="Impact, Arial Black, sans-serif" font-size="13" text-anchor="middle" fill="#111">YES.</text></g>
<g transform="translate(800 92)">
<rect x="-70" y="0" width="140" height="62" rx="3" fill="#0b0e12" stroke="#2b313b" stroke-width="3"/>
<text x="-36" y="17" font-family="monospace" font-size="10" text-anchor="middle" fill="#8b93a0">HOME</text>
<text x="36" y="17" font-family="monospace" font-size="10" text-anchor="middle" fill="#8b93a0">AWAY</text>
<text x="-36" y="46" font-family="monospace" font-size="28" font-weight="bold" text-anchor="middle" fill="#ffe14a">3</text>
<text x="36" y="46" font-family="monospace" font-size="28" font-weight="bold" text-anchor="middle" fill="#ffe14a">2</text>
<text x="0" y="57" font-family="monospace" font-size="8" text-anchor="middle" fill="#ff5d5d">4TH 0:07</text>
<rect x="-4" y="62" width="8" height="58" fill="${INK}"/></g>
<g stroke="${INK}" stroke-linecap="round" fill="none" stroke-width="7"><path d="M1000 212V150M960 150H1040M960 150V70M1040 150V70"/></g>
<path d="M0 240V208Q400 198 800 206T1600 204V240Z" fill="${INK}"/>
</svg>`;


// A foam finger - the stadium kind - inked black with a white outline, with
// the line every sicko knows lettered on the palm. Exported as FOAM_SVG too,
// because js/seasonalTreats.js raises the same finger from the bottom of the
// screen (it keeps its own copy; this file only feeds the stylesheet).
const FOAM_PATH = 'M48 104V30Q48 12 64 12Q80 12 80 30V96Q86 90 96 92Q106 94 106 104Q112 98 120 102Q128 106 126 118V178Q126 198 104 198H46Q26 198 24 178L20 132Q18 118 30 116Q40 114 48 124Z';
export const FOAM_SVG = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 150 230">
<rect x="40" y="194" width="72" height="26" rx="4" fill="#111" stroke="#f4f1e8" stroke-width="4"/>
<path d="${FOAM_PATH}" fill="#111" stroke="#f4f1e8" stroke-width="5" stroke-linejoin="round"/>
<path d="M88 104Q94 110 104 108M108 118Q114 124 122 120" stroke="#f4f1e8" stroke-width="3" fill="none" stroke-linecap="round"/>
<path d="M60 28Q58 50 60 80" stroke="#f4f1e8" stroke-width="3" fill="none" opacity=".55" stroke-linecap="round"/>
<g font-family="Impact, Arial Black, sans-serif" fill="#f4f1e8" text-anchor="middle" transform="rotate(-6 75 165)">
<text x="78" y="145" font-size="17">YES...</text>
<text x="75" y="166" font-size="18">HA HA HA</text>
<text x="74" y="187" font-size="17">...YES!</text>
<rect x="72" y="190" width="30" height="2.5"/></g>
</svg>`;

// OUR SICKO. Not the Sickos Committee's character - an original fan of ours,
// drawn in the same ink: face-painted, sweatband on, rain poncho over the
// jersey, eyes pointed two different directions, and a foam finger in the air.
const SICKO_SVG = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 240 320">
<g stroke="#f4f1e8" stroke-width="4" stroke-linejoin="round" stroke-linecap="round">
<path d="M170 168L196 96" stroke-width="16"/>
<path d="M170 168L196 96" stroke="#111" stroke-width="9"/>
<g transform="translate(156 -6) scale(.62)"><rect x="40" y="194" width="72" height="26" rx="4" fill="#111"/>
<path d="M48 104V30Q48 12 64 12Q80 12 80 30V96Q86 90 96 92Q106 94 106 104Q112 98 120 102Q128 106 126 118V178Q126 198 104 198H46Q26 198 24 178L20 132Q18 118 30 116Q40 114 48 124Z" fill="#111" stroke-width="6"/>
<g font-family="Impact, Arial Black, sans-serif" fill="#f4f1e8" stroke="none" text-anchor="middle" transform="rotate(-6 75 165)"><text x="76" y="152" font-size="22">YES!</text><text x="75" y="180" font-size="19">HA HA</text></g></g>
<path d="M44 196Q120 160 196 196L226 320H14Z" fill="#111"/>
<path d="M58 206Q120 182 182 206" fill="none" opacity=".5"/>
<text x="120" y="270" font-family="Impact, Arial Black, sans-serif" font-size="40" fill="#f4f1e8" stroke="#111" stroke-width="1.5" text-anchor="middle" transform="rotate(-4 120 260)">YES!!</text>
<path d="M60 200L40 250" stroke-width="14"/><path d="M60 200L40 250" stroke="#111" stroke-width="7"/>
<path d="M60 74L72 42L84 66L96 30L108 62L122 28L132 62L148 36L154 70L170 52L164 86Z" fill="#111"/>
<circle cx="116" cy="118" r="56" fill="#f4f1e8" stroke="#111" stroke-width="5"/>
<rect x="62" y="74" width="108" height="16" rx="7" fill="#111" stroke-width="3"/>
<path d="M68 82H164" stroke-width="2.5"/>
<path d="M76 104L102 96M130 96L158 106" stroke="#111" stroke-width="6"/>
<circle cx="94" cy="118" r="16" fill="#fff" stroke="#111" stroke-width="4"/>
<circle cx="140" cy="118" r="18" fill="#fff" stroke="#111" stroke-width="4"/>
<circle cx="88" cy="114" r="5" fill="#111" stroke="none"/>
<circle cx="148" cy="125" r="5.5" fill="#111" stroke="none"/>
<path d="M78 136L86 146M90 138L96 148M136 138L142 148M148 136L156 146" stroke="#111" stroke-width="4"/>
<path d="M78 142Q116 152 156 142Q154 186 116 188Q80 186 78 142Z" fill="#111" stroke="#111"/>
<path d="M84 146Q116 154 150 146V156Q116 162 84 156Z" fill="#fff" stroke="#111" stroke-width="2"/>
<path d="M95 149V159M106 151V161M117 151V161M128 151V161M139 149V158" stroke="#111" stroke-width="2"/>
<path d="M98 176Q116 168 134 176Q116 184 98 176Z" fill="#e05a5a" stroke="none"/>
<g fill="#111" stroke="none"><circle cx="72" cy="150" r="1.4"/><circle cx="70" cy="160" r="1.4"/><circle cx="76" cy="168" r="1.4"/><circle cx="160" cy="150" r="1.4"/><circle cx="163" cy="160" r="1.4"/><circle cx="158" cy="168" r="1.4"/></g>
<path d="M58 118Q52 124 58 132M174 118Q180 124 174 132" fill="none" stroke="#111" stroke-width="4"/>
</g>
<g fill="#f4f1e8"><path d="M30 40l4-10 4 10 10 4-10 4-4 10-4-10-10-4z"/><path d="M206 214l3-7 3 7 7 3-7 3-3 7-3-7-7-3z"/></g>
</svg>`;

export default { PEEPER: peeper, GRAIN: grain, STADIUM: stadium, FOAM: FOAM_SVG, SICKO: SICKO_SVG };
