// /js/nav.js
import { supabase } from './supabaseClient.js';

const LINKS = [
  { href: './index.html',      key: 'leaderboard', label: 'Leaderboard' },
  { href: './picks.html',      key: 'picks',       label: 'Make Picks', authOnly: true, id: 'nav-picks' },
  { href: './cfb-genius.html', key: 'genius',      label: 'CFB Genius' },
  { href: './stats.html',      key: 'stats',       label: 'Stats' }
];

function clsActive(isActive){
  return isActive
    ? 'text-[var(--cfp-gold-2)] border-b-2 border-[var(--cfp-gold)]'
    : 'text-gray-300 hover:text-[var(--cfp-ivory)] transition-colors';
}

// Guard so we subscribe only once (per page load)
let didSubscribe = false;

export default async function initNav(){
  const mount = document.getElementById('site-nav');
  if (!mount) return;

  const current = document.body?.dataset?.page || '';
  const { data: { session } } = await supabase.auth.getSession();
  const signedIn = !!session;

  const items = LINKS
    .filter(l => !l.authOnly || signedIn)
    .map(l => {
      const active = l.key === current;
      const idAttr = l.id ? ` id="${l.id}"` : '';
      return `<li${idAttr}><a href="${l.href}" class="block px-3 py-3 ${clsActive(active)}">${l.label}</a></li>`;
    }).join('');

  mount.innerHTML = `
    <nav class="mb-5 text-sm font-semibold tracking-wider uppercase font-['Oswald',_sans-serif]">
      <ul class="flex items-center gap-2 border-b border-[rgba(231,231,231,.08)]">
        ${items}
        <!-- Right-aligned auth actions; both carry ml-auto so they occupy the same spot -->
        <li class="ml-auto ${signedIn ? 'hidden' : ''}" id="nav-signin">
          <a href="./signin.html" class="block px-3 py-3 text-gray-300 hover:text-[var(--cfp-ivory)] transition-colors">Sign in</a>
        </li>
        <li class="ml-auto ${signedIn ? '' : 'hidden'}" id="nav-signout">
          <button id="sign-out-btn" class="block px-3 py-3 text-gray-300 hover:text-[var(--cfp-ivory)] transition-colors">Sign out</button>
        </li>
      </ul>
    </nav>
  `;

  // Wire sign-out
  const signOutBtn = document.getElementById('sign-out-btn');
  signOutBtn?.addEventListener('click', async () => {
    await supabase.auth.signOut();
    // After sign-out, send them home
    window.location.href = './index.html';
  });

  // Helper to toggle buttons without reloading the page
  function setAuthButtons(isSignedIn){
    const signInLi = document.getElementById('nav-signin');
    const signOutLi = document.getElementById('nav-signout');
    if (signInLi)  signInLi.classList.toggle('hidden',  isSignedIn);
    if (signOutLi) signOutLi.classList.toggle('hidden', !isSignedIn);
  }

  // If they sign out while on an auth-only page, kick them to home
  function redirectIfOnAuthOnlyPage(isSignedIn){
    if (isSignedIn) return;
    const onAuthOnlyPage = LINKS.some(l => l.authOnly && l.key === (document.body?.dataset?.page || ''));
    if (onAuthOnlyPage) window.location.href = './index.html';
  }

  // Subscribe exactly once; avoid any auto-reload loops
  if (!didSubscribe) {
    didSubscribe = true;

    supabase.auth.onAuthStateChange((event, newSession) => {
      // Only react to true state changes; ignore token refresh / initial session events
      if (event === 'SIGNED_IN') {
        setAuthButtons(true);
      } else if (event === 'SIGNED_OUT') {
        setAuthButtons(false);
        redirectIfOnAuthOnlyPage(false);
      }
      // Events like TOKEN_REFRESHED / INITIAL_SESSION are intentionally ignored
    });
  }
}