// Feature flags for Picky.
//
// PICKY_PLAYER_ENABLED: Picky the player (exhibition row on the leaderboard,
// stats and picks pages). On for everyone.
//
// PICKY_ENABLED: the Picky web chat widget. Flip to true to show it to
// everyone. While off, add ?picky=on to any page URL to preview the chat in
// this browser (remembered in localStorage); ?picky=off clears it.
const PICKY_PLAYER_ENABLED = true;
const PICKY_ENABLED = false;

export function pickyPlayerOn() {
  return PICKY_PLAYER_ENABLED;
}

export function pickyOn() {
  try {
    const q = new URLSearchParams(location.search).get('picky');
    if (q === 'on') localStorage.setItem('pickyPreview', '1');
    if (q === 'off') localStorage.removeItem('pickyPreview');
    return PICKY_ENABLED || localStorage.getItem('pickyPreview') === '1';
  } catch {
    return PICKY_ENABLED;
  }
}
