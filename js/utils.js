// js/utils.js — Shared utility functions

export function stripAccents(s) {
  try { return s.normalize('NFD').replace(/\p{Diacritic}/gu, ''); }
  catch { return s; }
}

export function norm(v) {
  if (!v) return '';
  return stripAccents(String(v))
    .toLowerCase()
    .replace(/&/g, ' and ')
    .replace(/\./g, '')
    .replace(/[^a-z0-9\s]/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
}

const stToState = s => s.replace(/\bst\b/g, 'state');
const stateToSt = s => s.replace(/\bstate\b/g, 'st');

export function sameTeam(a, b) {
  const na = norm(a), nb = norm(b);
  return na === nb || stToState(na) === stToState(nb) || stateToSt(na) === stateToSt(nb);
}

export function escapeHtml(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

export const TEAM_COLORS = {
  'Alabama':'#9E1B32','Alabama A&M':'#660000','Alabama State':'#D29F13',
  'App State':'#222','Arizona':'#CC0033','Arizona State':'#8C1D40',
  'Arkansas':'#9D2235','Army':'#D4A048','Auburn':'#0C2340',
  'Baylor':'#154734','Boise State':'#0033A0','Boston College':'#98002E',
  'BYU':'#002E5D','California':'#003262','Central Michigan':'#6A0032',
  'Charlotte':'#00703C','Cincinnati':'#E00122','Clemson':'#F56600',
  'Coastal Carolina':'#006F71','Colorado':'#CFB87C','Colorado State':'#1E4D2B',
  'Duke':'#003087','East Carolina':'#592A8A','Eastern Michigan':'#006633',
  'Florida':'#0021A5','Florida Atlantic':'#003366','Florida International':'#002F56',
  'Florida State':'#782F40','Fresno State':'#DB0032','Georgia':'#BA0C2F',
  'Georgia Southern':'#011E41','Georgia State':'#0039A6','Georgia Tech':'#B3A369',
  'Hawai\'i':'#024731','Houston':'#C8102E','Illinois':'#E84A27',
  'Indiana':'#990000','Iowa':'#FFCD00','Iowa State':'#C8102E',
  'Jacksonville State':'#CC0000','James Madison':'#450084','Kansas':'#0051BA',
  'Kansas State':'#512888','Kennesaw State':'#FDBB30','Kent State':'#002664',
  'Kentucky':'#0033A0','Liberty':'#002D62','Louisiana Lafayette':'#CE181E',
  'Louisiana Monroe':'#840029','Louisiana Tech':'#002F8B','Louisville':'#AD0000',
  'LSU':'#461D7C','Marshall':'#00B140','Maryland':'#E03A3E',
  'Memphis':'#003087','Miami':'#F47321','Miami (OH)':'#B61E2E',
  'Michigan':'#FFCB05','Michigan State':'#18453B','Middle Tennessee':'#0066CC',
  'Minnesota':'#7A0019','Mississippi State':'#660000','Missouri':'#F1B82D',
  'Navy':'#00205B','NC State':'#CC0000','Nebraska':'#E41C38',
  'Nevada':'#003366','New Mexico':'#BA0C2F','New Mexico State':'#8B0D2A',
  'North Carolina':'#7BAFD4','North Dakota State':'#0A5640','North Texas':'#00853E',
  'Northern Illinois':'#BA0C2F','Northwestern':'#4E2A84','Notre Dame':'#0C2340',
  'Ohio':'#00694E','Ohio State':'#BB0000','Oklahoma':'#841617',
  'Oklahoma State':'#FF7300','Old Dominion':'#003057','Ole Miss':'#CE1126',
  'Oregon':'#154733','Oregon State':'#DC4405','Penn State':'#041E42',
  'Pittsburgh':'#003594','Purdue':'#CEB888','Rice':'#002469',
  'Rutgers':'#CC0033','Sam Houston':'#F58025','San Diego State':'#A6192E',
  'San Jose State':'#0055A2','SMU':'#354CA1','South Alabama':'#00205B',
  'South Carolina':'#73000A','South Florida':'#006747','Southern Miss':'#FFAB00',
  'Stanford':'#8C1515','Syracuse':'#F76900','TCU':'#4D1979',
  'Temple':'#9D2235','Tennessee':'#FF8200','Texas':'#BF5700',
  'Texas A&M':'#500000','Texas State':'#501214','Texas Tech':'#CC0000',
  'Toledo':'#003976','Troy':'#8B2332','Tulane':'#006747',
  'Tulsa':'#002D72','UAB':'#1E6B52','UCF':'#BA9B37',
  'UCLA':'#2D68C4','UConn':'#000E2F','UNLV':'#CF0A2C',
  'USC':'#990000','Utah':'#CC0000','Utah State':'#0F2439',
  'UTEP':'#FF8200','UTSA':'#0C2340','Vanderbilt':'#866D4B',
  'Virginia':'#232D4B','Virginia Tech':'#630031','Wake Forest':'#9E7E38',
  'Washington':'#4B2E83','Washington State':'#981E32','West Virginia':'#002855',
  'Western Kentucky':'#B01E24','Western Michigan':'#6C4023','Wisconsin':'#C5050C',
  'Wyoming':'#492F24','Yale':'#00356B',
};

export function getTeamColor(teamName) {
  return TEAM_COLORS[teamName] || null;
}

/**
 * A team's colour, made readable on the scoreboard's near-black panel.
 *
 * Half the palette is a dark navy, maroon or forest green - Virginia is #232D4B,
 * Penn State #041E42, Hawai'i #024731 - and those are close to invisible on
 * #0a0b0d. Painting them on anyway is not "showing the team colour", it is
 * showing nothing at all.
 *
 * So the hue and the saturation are kept exactly as they are and only the
 * lightness is raised, one step at a time, until the text clears a normal
 * contrast bar against the panel. A colour already bright enough - Carolina blue,
 * NC State red - comes back untouched.
 */
const BOARD_BG = [0x0a, 0x0b, 0x0d];
// 3.2, not the 4.5 used for body copy. These are four bold uppercase letters
// sitting next to the team's own logo, not a paragraph - and at 4.5 the lift
// rewrites college football's palette: Alabama's crimson comes out pink and
// Auburn's navy comes out sky blue. 3.2 clears the genuinely invisible ones and
// leaves everything else recognisably itself.
const MIN_CONTRAST = 3.2;

const srgb = c => { const v = c / 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); };
const luminance = ([r, g, b]) => 0.2126 * srgb(r) + 0.7152 * srgb(g) + 0.0722 * srgb(b);
const contrast = (a, b) => {
  const la = luminance(a), lb = luminance(b);
  return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05);
};

function hexToRgb(hex) {
  const raw = String(hex || '').trim().replace(/^#/, '');
  const six = raw.length === 3 ? raw.split('').map(c => c + c).join('') : raw;
  if (!/^[0-9a-f]{6}$/i.test(six)) return null;
  const n = parseInt(six, 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}
const toHex = ([r, g, b]) =>
  '#' + [r, g, b].map(v => Math.round(Math.min(255, Math.max(0, v))).toString(16).padStart(2, '0')).join('');

function rgbToHsl([r, g, b]) {
  r /= 255; g /= 255; b /= 255;
  const max = Math.max(r, g, b), min = Math.min(r, g, b), l = (max + min) / 2;
  if (max === min) return [0, 0, l];
  const d = max - min;
  const s = l > 0.5 ? d / (2 - max - min) : d / (max + min);
  let h;
  if (max === r) h = ((g - b) / d + (g < b ? 6 : 0)) / 6;
  else if (max === g) h = ((b - r) / d + 2) / 6;
  else h = ((r - g) / d + 4) / 6;
  return [h, s, l];
}
function hslToRgb([h, s, l]) {
  if (s === 0) { const v = l * 255; return [v, v, v]; }
  const q = l < 0.5 ? l * (1 + s) : l + s - l * s, p = 2 * l - q;
  const hue = tt => {
    let x = tt; if (x < 0) x += 1; if (x > 1) x -= 1;
    if (x < 1 / 6) return p + (q - p) * 6 * x;
    if (x < 1 / 2) return q;
    if (x < 2 / 3) return p + (q - p) * (2 / 3 - x) * 6;
    return p;
  };
  return [hue(h + 1 / 3) * 255, hue(h) * 255, hue(h - 1 / 3) * 255];
}

export function teamColorOnDark(teamName) {
  const rgb = hexToRgb(getTeamColor(teamName));
  if (!rgb) return null;
  if (contrast(rgb, BOARD_BG) >= MIN_CONTRAST) return toHex(rgb);

  // Climb from the colour's OWN lightness in small steps and stop at the first
  // one that clears the bar. Starting the search at some fixed midpoint is what
  // turns a deep crimson into a pink: it overshoots on the first try.
  const [h, s, l0] = rgbToHsl(rgb);
  for (let l = l0 + 0.02; l <= 0.9; l += 0.02) {
    const lifted = hslToRgb([h, s, l]);
    if (contrast(lifted, BOARD_BG) >= MIN_CONTRAST) return toHex(lifted);
  }
  return toHex(hslToRgb([h, s, 0.9]));
}

export function buildPickMap(rows) {
  const map = {};
  (rows || []).forEach(r => {
    if (!map[r.team_id]) map[r.team_id] = {};
    map[r.team_id][r.game_id] = r.pick;
  });
  return map;
}
