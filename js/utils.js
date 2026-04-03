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

export function buildPickMap(rows) {
  const map = {};
  (rows || []).forEach(r => {
    if (!map[r.team_id]) map[r.team_id] = {};
    map[r.team_id][r.game_id] = r.pick;
  });
  return map;
}
