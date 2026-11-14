// Feature flag for Picky. Flip PICKY_ENABLED to true to show him to everyone.
// While off, add ?picky=on to any page URL to preview him in this browser
// (remembered in localStorage); ?picky=off clears it.
const PICKY_ENABLED = false;

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
