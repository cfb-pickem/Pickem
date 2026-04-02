// js/mascots.js — Injects CSS-drawn cartoon animal mascots

const MASCOT_DEFS = [
  { name: 'bear',    bg: '#5c3a1e', ear: '#3d2510', nose: '#222', accent: '#8B6914' },
  { name: 'tiger',   bg: '#e8a020', ear: '#111',    nose: '#222', accent: '#fff' },
  { name: 'eagle',   bg: '#f5f0e0', ear: '#DAA520', nose: '#e8a020', accent: '#444' },
  { name: 'bulldog', bg: '#aaa',    ear: '#777',    nose: '#333', accent: '#c44' },
  { name: 'gator',   bg: '#2d7d46', ear: '#1a5c30', nose: '#111', accent: '#90ee90' },
  { name: 'wolf',    bg: '#777',    ear: '#555',    nose: '#222', accent: '#ccc' },
  { name: 'wildcat', bg: '#6a4fa0', ear: '#4a3580', nose: '#222', accent: '#fff' },
  { name: 'hawk',    bg: '#8B0000', ear: '#DAA520', nose: '#e8a020', accent: '#fff' },
];

function createMascotSVG(m) {
  // Build a simple cartoon animal face as inline SVG
  // Generic round head with ears, eyes, nose — style varies per animal
  const isRound = ['bear','bulldog','wildcat','wolf'].includes(m.name);
  const isBird = ['eagle','hawk'].includes(m.name);

  if (isBird) {
    return `<svg viewBox="0 0 80 80" xmlns="http://www.w3.org/2000/svg">
      <!-- head -->
      <ellipse cx="40" cy="42" rx="28" ry="24" fill="${m.bg}" />
      <!-- crest/tuft -->
      <polygon points="30,18 40,6 50,18 45,20 40,12 35,20" fill="${m.ear}" />
      <!-- eyes -->
      <circle cx="30" cy="38" r="5" fill="#fff"/>
      <circle cx="50" cy="38" r="5" fill="#fff"/>
      <circle cx="31" cy="38" r="2.5" fill="#111"/>
      <circle cx="51" cy="38" r="2.5" fill="#111"/>
      <!-- beak -->
      <polygon points="32,48 40,60 48,48" fill="${m.nose}" />
      <line x1="32" y1="52" x2="48" y2="52" stroke="${m.accent}" stroke-width="1.5" opacity=".6"/>
    </svg>`;
  }

  // Ear shape varies
  const earY = m.name === 'gator' ? 28 : 14;
  const earR = m.name === 'gator' ? 4 : 9;

  return `<svg viewBox="0 0 80 80" xmlns="http://www.w3.org/2000/svg">
    <!-- ears -->
    <circle cx="18" cy="${earY}" r="${earR}" fill="${m.ear}"/>
    <circle cx="62" cy="${earY}" r="${earR}" fill="${m.ear}"/>
    ${m.name === 'bear' ? `<circle cx="18" cy="${earY}" r="5" fill="${m.accent}" opacity=".5"/>
    <circle cx="62" cy="${earY}" r="5" fill="${m.accent}" opacity=".5"/>` : ''}
    <!-- head -->
    <ellipse cx="40" cy="42" rx="26" ry="${isRound ? 24 : 22}" fill="${m.bg}"/>
    ${m.name === 'tiger' ? `
    <line x1="20" y1="32" x2="30" y2="36" stroke="#111" stroke-width="2.5" stroke-linecap="round"/>
    <line x1="60" y1="32" x2="50" y2="36" stroke="#111" stroke-width="2.5" stroke-linecap="round"/>
    <line x1="22" y1="38" x2="32" y2="40" stroke="#111" stroke-width="2" stroke-linecap="round"/>
    <line x1="58" y1="38" x2="48" y2="40" stroke="#111" stroke-width="2" stroke-linecap="round"/>` : ''}
    ${m.name === 'gator' ? `
    <ellipse cx="40" cy="42" rx="22" ry="18" fill="none" stroke="#1a5c30" stroke-width="2"/>
    <rect x="22" y="54" width="36" height="6" rx="3" fill="#1a5c30"/>` : ''}
    <!-- eyes -->
    <circle cx="30" cy="38" r="5" fill="#fff"/>
    <circle cx="50" cy="38" r="5" fill="#fff"/>
    <circle cx="31" cy="38" r="2.5" fill="#111"/>
    <circle cx="51" cy="38" r="2.5" fill="#111"/>
    <!-- eye shine -->
    <circle cx="32" cy="37" r="1" fill="#fff" opacity=".8"/>
    <circle cx="52" cy="37" r="1" fill="#fff" opacity=".8"/>
    <!-- nose/snout -->
    ${m.name === 'gator'
      ? `<ellipse cx="40" cy="50" rx="6" ry="3" fill="${m.nose}"/>
         <circle cx="36" cy="48" r="1.5" fill="${m.accent}" opacity=".5"/>
         <circle cx="44" cy="48" r="1.5" fill="${m.accent}" opacity=".5"/>`
      : `<ellipse cx="40" cy="50" rx="8" ry="5" fill="${m.nose}" opacity=".9"/>
         <ellipse cx="40" cy="49" rx="4" ry="2.5" fill="${m.accent}" opacity=".3"/>`
    }
    <!-- mouth -->
    <path d="M 35 54 Q 40 58 45 54" fill="none" stroke="${m.nose}" stroke-width="1.5" opacity=".6"/>
  </svg>`;
}

export function initMascots() {
  if (document.querySelector('.mascot-layer')) return;

  const layer = document.createElement('div');
  layer.className = 'mascot-layer';
  layer.setAttribute('aria-hidden', 'true');

  // Shuffle and place 12 mascots
  const shuffled = [...MASCOT_DEFS].sort(() => Math.random() - 0.5);

  for (let i = 0; i < 12; i++) {
    const def = shuffled[i % shuffled.length];
    const el = document.createElement('div');
    el.className = `mascot mascot-${i + 1}`;
    el.innerHTML = createMascotSVG(def);
    layer.appendChild(el);
  }

  document.body.prepend(layer);
}
