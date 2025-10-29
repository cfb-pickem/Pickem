// /js/nav.js  (fixed)
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

// Guard so we subscribe only once
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
      const id = l.id ? ` id="${l.id}"` : '';
      return `<li${id}><a href="${l.href}" class="block px-3 py-3 ${clsActive(active)}">${l.label}</a></li>`;
    }).join('');

  mount.innerHTML = `
    <nav class="mb-5 text-sm font-semibold tracking-wider uppercase font-['Oswald',_sans-serif]">
      <ul class="flex items-center gap-2 border-b border-[rgba(231,231,231,.08)]">
        ${items}
        <li class="ml-auto ${signedIn ? 'hidden' : ''}" id="nav-signin">
          <a href="./signin.html" class="block px-3 py-3 text-gray-300 hover:text-[var(--cfp-ivory)] transition-colors">Sign in</a>
        </li>
        <li class="${signedIn ? '' : 'hidden'}" id="nav-signout">
          <button id="sign-out-btn" class="block px-3 py-3 text-gray-300 hover:text-[var(--cfp-ivory)] transition-colors">Sign out</button>
        </li>
      </ul>
    </nav>
  `;

  // Wire sign-out once nav is in the DOM
  const signOutBtn = document.getElementById('sign-out-btn');
  signOutBtn?.addEventListener('click', async () => {
    await supabase.auth.signOut();
    // simple hard nav so we don’t rely on a live rerender
    window.location.href = './index.html';
  });

  // Subscribe exactly once; on auth change, do a simple reload
  if (!didSubscribe) {
    didSubscribe = true;
    supabase.auth.onAuthStateChange(() => {
      window.location.reload();
    });
  }
}