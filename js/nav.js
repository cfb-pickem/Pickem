// /js/nav.js
import { supabase } from './supabaseClient.js';

const LINKS = [
  { href: 'index.html',       key: 'leaderboard', label: "Leaderboard" },
  { href: 'picks.html',       key: 'picks',       label: "Make Picks", authOnly: true, id: 'nav-picks' },
  { href: 'cfb-genius.html',  key: 'genius',      label: "CFB Genius" },
  { href: 'stats.html',       key: 'stats',       label: "Stats" }
];

function clsActive(isActive){
  return isActive
    ? 'text-[var(--cfp-gold-2)] border-b-2 border-[var(--cfp-gold)]'
    : 'text-gray-300 hover:text-[var(--cfp-ivory)] transition-colors';
}

export default async function initNav() {
  const mount = document.getElementById('site-nav');
  if (!mount) return;

  const current = document.body?.dataset?.page || '';
  const { data: { session } } = await supabase.auth.getSession();
  const signedIn = !!session;

  // Build nav HTML
  const items = LINKS
    .filter(l => !l.authOnly || signedIn)
    .map(l => {
      const active = l.key === current;
      return `
        <li ${l.id ? `id="${l.id}"` : ''}>
          <a href="${l.href}" class="block px-3 py-3 ${clsActive(active)}">${l.label}</a>
        </li>
      `;
    }).join('');

  mount.innerHTML = `
    <nav class="mb-5 text-sm font-semibold tracking-wider uppercase font-['Oswald',_sans-serif]">
      <ul class="flex items-center gap-2 border-b border-[rgba(231,231,231,.08)]">
        ${items}
        <li class="ml-auto ${signedIn ? 'hidden' : ''}" id="nav-signin">
          <a href="signin.html" class="block px-3 py-3 text-gray-300 hover:text-[var(--cfp-ivory)] transition-colors">Sign in</a>
        </li>
        <li class="${signedIn ? '' : 'hidden'}" id="nav-signout">
          <button id="sign-out-btn" class="block px-3 py-3 text-gray-300 hover:text-[var(--cfp-ivory)] transition-colors">Sign out</button>
        </li>
      </ul>
    </nav>
  `;

  // Wire sign out
  const signOutBtn = document.getElementById('sign-out-btn');
  signOutBtn?.addEventListener('click', async () => {
    await supabase.auth.signOut();
    // Simple: refresh to let initNav rebuild the menu in signed-out state
    location.href = 'index.html';
  });

  // React to auth changes live (e.g., after sign-in on another page)
  supabase.auth.onAuthStateChange(() => {
    // Re-run to update visibility
    initNav();
  });
}