// /js/nav.js — fixed, top-level nav that can't be covered by overlays
let supabase = null;
(async () => {
  try { ({ supabase } = await import('./supabaseClient.js')); }
  catch (e) { console.warn('supabase not available; nav renders without auth.', e); }
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
  const mount = document.getElementById('site-nav'); // placeholder (we’ll replace with a fixed bar)
  const current = document.body?.dataset?.page || '';
  let signedIn = false;

  if (supabase) {
    try {
      const { data: { session } } = await supabase.auth.getSession();
      signedIn = !!session;
    } catch {}
  }

  const itemsHtml = LINKS
    .filter(l => !l.authOnly || signedIn)
    .map(l => {
      const active = l.key === current;
      return `
        <li ${l.id ? `id="${l.id}"` : ''}>
          <a data-nav href="${l.href}" class="block px-3 py-3 ${clsActive(active)}">${l.label}</a>
        </li>`;
    }).join('');

  // Build a fixed, top-level bar (outside any stacking context)
  const wrapper = document.createElement('div');
  wrapper.innerHTML = `
    <div id="fixed-nav-wrap"
         class="fixed top-0 left-0 right-0 z-[2147483647] pointer-events-auto">
      <nav class="w-full bg-[rgba(11,11,11,.9)] backdrop-blur mb-0 text-sm font-semibold tracking-wider uppercase font-['Oswald',_sans-serif] border-b border-[rgba(231,231,231,.08)]">
        <ul class="flex items-center gap-2">
          ${itemsHtml}
          <li class="ml-auto ${signedIn ? 'hidden' : ''}" id="nav-signin">
            <a data-nav href="./signin.html" class="block px-3 py-3 text-gray-300 hover:text-[var(--cfp-ivory)] transition-colors">Sign in</a>
          </li>
          <li class="${signedIn ? '' : 'hidden'}" id="nav-signout">
            <button id="sign-out-btn" class="block px-3 py-3 text-gray-300 hover:text-[var(--cfp-ivory)] transition-colors">Sign out</button>
          </li>
        </ul>
      </nav>
    </div>
  `.trim();

  const fixedNav = wrapper.firstElementChild;
  document.body.prepend(fixedNav);

  // Add/adjust spacer so content isn't hidden under fixed bar
  let spacer = document.getElementById('site-nav-spacer');
  if (!spacer) {
    spacer = document.createElement('div');
    spacer.id = 'site-nav-spacer';
    document.body.insertBefore(spacer, document.body.children[1]);
  }
  const h = fixedNav.getBoundingClientRect().height || 56;
  spacer.style.height = `${Math.ceil(h)}px`;

  // Remove placeholder mount to avoid duplicate space
  if (mount) mount.remove();

  // Force navigation on click (works even if some script calls preventDefault)
  fixedNav.querySelectorAll('a[data-nav]').forEach(a => {
    a.addEventListener('click', (e) => {
      e.stopPropagation();
      const url = a.getAttribute('href');
      // Navigate immediately — don’t rely on default
      window.location.assign(url);
    }, { capture: true });
  });

  // Sign out
  const signOutBtn = fixedNav.querySelector('#sign-out-btn');
  if (signOutBtn && supabase) {
    signOutBtn.addEventListener('click', async () => {
      try { await supabase.auth.signOut(); } catch {}
      window.location.assign('./index.html');
    });
  }

  // One auth listener to re-render when auth changes
  if (supabase && !authListenerSet) {
    authListenerSet = true;
    supabase.auth.onAuthStateChange(() => {
      // Remove old fixed nav & spacer, then rebuild
      document.getElementById('fixed-nav-wrap')?.remove();
      document.getElementById('site-nav-spacer')?.remove();
      initNav();
    });
  }
}