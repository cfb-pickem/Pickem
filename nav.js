// /js/nav.js
let supabase = null;
(async () => {
  try {
    // Import lazily so the nav still renders even if supabase file is missing
    ({ supabase } = await import('./supabaseClient.js'));
  } catch (e) {
    console.warn('supabaseClient.js not found or failed to load. Rendering nav without auth controls.', e);
  }
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

  // Try to read session if supabase loaded
  if (supabase) {
    try {
      const { data: { session } } = await supabase.auth.getSession();
      signedIn = !!session;
    } catch (e) {
      console.warn('getSession failed:', e);
    }
  }

  // Build items (filter authOnly if signed out)
  const itemsHtml = LINKS
    .filter(l => !l.authOnly || signedIn)
    .map(l => {
      const active = l.key === current;
      return `
        <li ${l.id ? `id="${l.id}"` : ''}>
          <a href="${l.href}" class="block px-3 py-3 ${clsActive(active)}">${l.label}</a>
        </li>
      `;
    }).join('');

  // High z-index + pointer events so nothing blocks clicks
  mount.innerHTML = `
    <nav class="relative z-50 mb-5 text-sm font-semibold tracking-wider uppercase font-['Oswald',_sans-serif]">
      <ul class="pointer-events-auto flex items-center gap-2 border-b border-[rgba(231,231,231,.08)]">
        ${itemsHtml}
        <li class="ml-auto ${signedIn ? 'hidden' : ''}" id="nav-signin">
          <a href="./signin.html" class="block px-3 py-3 text-gray-300 hover:text-[var(--cfp-ivory)] transition-colors">Sign in</a>
        </li>
        <li class="${signedIn ? '' : 'hidden'}" id="nav-signout">
          <button id="sign-out-btn" class="block px-3 py-3 text-gray-300 hover:text-[var(--cfp-ivory)] transition-colors">Sign out</button>
        </li>
      </ul>
    </nav>
  `;

  // Wire sign-out if available
  const signOutBtn = document.getElementById('sign-out-btn');
  if (signOutBtn && supabase) {
    signOutBtn.addEventListener('click', async () => {
      try { await supabase.auth.signOut(); } catch {}
      // After sign-out, send them to the leaderboard (works in subfolders)
      location.href = './index.html';
    });
  }

  // Set a single auth listener (avoid multiplying listeners on re-renders)
  if (supabase && !authListenerSet) {
    authListenerSet = true;
    supabase.auth.onAuthStateChange(() => {
      // Re-render the nav when auth changes
      initNav();
    });
  }
}