// js/prefetch.js — make in-site navigation feel instant.
//
// Two mechanisms, both progressive enhancements that degrade to normal clicks:
//   1. Speculation Rules  — Chrome/Edge prerender the page on hover intent.
//   2. <link rel=prefetch> — everyone else at least gets the HTML warmed.
//
// Only same-origin .html links inside the site are ever touched.

const PREFETCHED = new Set();

function isEligible(a) {
  if (!a || !a.href) return false;
  const url = new URL(a.href, location.href);
  if (url.origin !== location.origin) return false;
  if (!url.pathname.endsWith('.html')) return false;
  if (url.pathname === location.pathname) return false;
  if (a.hasAttribute('download') || a.target === '_blank') return false;
  return true;
}

function prefetch(a) {
  if (!isEligible(a)) return;
  const href = new URL(a.href, location.href).href;
  if (PREFETCHED.has(href)) return;
  PREFETCHED.add(href);

  const link = document.createElement('link');
  link.rel = 'prefetch';
  link.href = href;
  link.as = 'document';
  document.head.appendChild(link);
}

export function initPrefetch() {
  // Respect data saver / reduced data preferences.
  const conn = navigator.connection;
  if (conn && (conn.saveData || /2g/.test(conn.effectiveType || ''))) return;

  // Native prerendering where supported — this is what makes it feel instant.
  if (HTMLScriptElement.supports?.('speculationrules')) {
    const rules = document.createElement('script');
    rules.type = 'speculationrules';
    rules.textContent = JSON.stringify({
      prerender: [{
        where: { and: [
          { href_matches: '/Pickem/*.html' },
          { not: { href_matches: '/Pickem/commissioner.html' } },
        ]},
        eagerness: 'moderate',   // fires on hover / pointerdown, not on sight
      }],
    });
    document.head.appendChild(rules);
  }

  // Fallback: warm the HTML on hover or first touch.
  const onIntent = e => {
    const a = e.target.closest?.('a[href]');
    if (a) prefetch(a);
  };
  document.addEventListener('pointerenter', onIntent, { capture: true, passive: true });
  document.addEventListener('touchstart',   onIntent, { capture: true, passive: true });
  document.addEventListener('focusin',      onIntent, { capture: true, passive: true });
}
