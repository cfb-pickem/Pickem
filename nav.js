// /js/nav.js
let supabase = null;
(async () => {
  try { ({ supabase } = await import('./supabaseClient.js')); }
  catch (e) { console.warn('supabaseClient.js not available; rendering nav without auth.', e); }
})();

const LINKS = [
  { href: './index.html',      key: 'leaderboard', label: 'Leaderboard' },
  { href: './picks.html',      key: 'picks',       label: 'Make Picks', authOnly: true, id: 'nav-picks' },
  { href: './cfb-genius.html', key: 'genius',      label: 'CFB Genius' },
  { href: './stats.html',      key: 'stats',       label: 'Stats' },
];

function clsActive(isActive){
  return isActive
    ? 'text-[var(--cfp-gold-2)] border-b-2 border-[var(--cfp-gold)]'
    : 'text-gray-300 hover:text-[var(--cfp-ivory)] transition-colors';
}

let authListenerSet = false;

export default async function initNav() {
  const mount = document.getElementById('site-nav');
  if (!mount) return;

  const current = document.body?.dataset?.page || '';
  let signedIn = false;

  if (supabase) {
    try {
      const { data: { session } } = await supabase.auth.getSession();
      signedIn = !!session;
    } catch (e) { console.warn('getSession failed:', e); }
  }

  const itemsHtml = LINKS
    .filter(l => !l.authOnly || signedIn)
    .map(l => {
      const active = l.key === current;
      return `
        <li ${l.id ? `id="${l.id}"` : ''}>
          <a data-nav href="${l.href}"
             class="block px-3 py-3 ${clsActive(active)}"
             role="link">${l.label}</a>
        </li>`;
    }).join('');

  // High z-index and pointer events so nothing blocks clicks
  mount.innerHTML = `
    <nav class="relative z-[9999] mb-5 text-sm font-semibold tracking-wider uppercase font-['Oswald',_sans-serif]">
      <ul class="pointer-events-auto flex items-center gap-2 border-b border-[rgba(231,231,231,.08)]">
        ${itemsHtml}
        <li class="ml-auto ${signedIn ? 'hidden' : ''}" id="nav-signin">
          <a data-nav href="./signin.html" class="block px-3 py-3 text-gray-300 hover:text-[var(--cfp-ivory)] transition-colors">Sign in</a>
        </li>
        <li class="${signedIn ? '' : 'hidden'}" id="nav-signout">
          <button id="sign-out-btn" class="block px-3 py-3 text-gray-300 hover:text-[var(--cfp-ivory)] transition-colors">Sign out</button>
        </li>
      </ul>
    </nav>
  `;

  // Force navigation even if some other script calls preventDefault
  mount.querySelectorAll('a[data-nav]').forEach(a => {
    a.addEventListener('click', (e) => {
      // If another handler tried to cancel, override by navigating explicitly.
      // Don't rely on default; just drive the navigation.
      const url = a.getAttribute('href');
      // Stop bubbling in case a parent is blocking anchors.
      e.stopPropagation();
      // Let the click ripple for a tick, then navigate.
      setTimeout(() => { window.location.href = url; }, 0);
    }, { capture: true });
  });

  // Sign out (if supabase loaded)
  const signOutBtn = document.getElementById('sign-out-btn');
  if (signOutBtn && supabase) {
    signOutBtn.addEventListener('click', async () => {
      try { await supabase.auth.signOut(); } catch {}
      window.location.href = './index.html';
    });
  }

  // Only set one auth listener
  if (supabase && !authListenerSet) {
    authListenerSet = true;
    supabase.auth.onAuthStateChange(() => {
      initNav();
    });
  }
}