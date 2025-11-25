// /js/nav.js
import { supabase } from './supabaseClient.js';

const LINKS = [
  { href: './index.html',      key: 'leaderboard',  label: 'Leaderboard' },
  { href: './playoff.html',    key: 'playoff',      label: 'Playoff' },        // 👈 NEW
  { href: './picks.html',      key: 'picks',        label: 'Make Picks', authOnly: true, id: 'nav-picks' },
  { href: './cfb-genius.html', key: 'genius',       label: 'CFB Genius' },
  { href: './stats.html',      key: 'stats',        label: 'Stats' },
  { href: './commissioner.html', key: 'commissioner', label: 'Commissioner', authOnly: true, id: 'nav-commissioner' } // new
];

function clsActive(isActive){
  return isActive
    ? 'text-[var(--cfp-gold-2)] border-b-2 border-[var(--cfp-gold)]'
    : 'text-gray-300 hover:text-[var(--cfp-ivory)] transition-colors';
}

let didSubscribe = false;

export default async function initNav(){
  const mount = document.getElementById('site-nav');
  if (!mount) return;

  const current = document.body?.dataset?.page || '';
  const { data: { session } } = await supabase.auth.getSession();
  const signedIn = !!session;
  const user = session?.user;

  // Check commissioner status
  let isCommissioner = false;
  if (signedIn && user) {
    const { data, error } = await supabase
      .from('teams')
      .select('commissioner')
      .eq('user_id', user.id)
      .single();

    if (error) {
      console.error('Error fetching commissioner status:', error.message);
    } else {
      isCommissioner = !!data?.commissioner;
    }
  }

  // Build nav
  const items = LINKS
    .filter(l => {
      // Hide commissioner link unless user is commissioner
      if (l.key === 'commissioner' && !isCommissioner) return false;
      return !l.authOnly || signedIn;
    })
    .map(l => {
      const active = l.key === current;
      const idAttr = l.id ? ` id="${l.id}"` : '';
      return `<li${idAttr}><a href="${l.href}" class="block px-3 py-3 ${clsActive(active)}">${l.label}</a></li>`;
    }).join('');

  mount.innerHTML = `
    <nav class="mb-5 text-sm font-semibold tracking-wider uppercase font-['Oswald',_sans-serif]">
      <ul class="flex flex-wrap items-center gap-2 border-b border-[rgba(231,231,231,.08)]">
        ${items}
        <li class="ml-auto ${signedIn ? 'hidden' : ''}" id="nav-signin">
          <a href="./signin.html" class="block px-3 py-3 text-gray-300 hover:text-[var(--cfp-ivory)] transition-colors">Sign in</a>
        </li>
        <li class="ml-auto ${signedIn ? '' : 'hidden'}" id="nav-signout">
          <button id="sign-out-btn" class="block px-3 py-3 text-gray-300 hover:text-[var(--cfp-ivory)] transition-colors">Sign out</button>
        </li>
      </ul>
    </nav>
  `;

  // Redirect if user is not commissioner but tries to access commissioner.html
  if (current === 'commissioner' && !isCommissioner) {
    window.location.href = './index.html';
  }

  // Wire sign-out
  const signOutBtn = document.getElementById('sign-out-btn');
  signOutBtn?.addEventListener('click', async () => {
    await supabase.auth.signOut();
    window.location.href = './index.html';
  });

  function setAuthButtons(isSignedIn){
    const signInLi = document.getElementById('nav-signin');
    const signOutLi = document.getElementById('nav-signout');
    if (signInLi)  signInLi.classList.toggle('hidden',  isSignedIn);
    if (signOutLi) signOutLi.classList.toggle('hidden', !isSignedIn);
  }

  function redirectIfOnAuthOnlyPage(isSignedIn){
    if (isSignedIn) return;
    const onAuthOnlyPage = LINKS.some(l => l.authOnly && l.key === current);
    if (onAuthOnlyPage) window.location.href = './index.html';
  }

  if (!didSubscribe) {
    didSubscribe = true;

    supabase.auth.onAuthStateChange((event, newSession) => {
      if (event === 'SIGNED_IN') {
        setAuthButtons(true);
      } else if (event === 'SIGNED_OUT') {
        setAuthButtons(false);
        redirectIfOnAuthOnlyPage(false);
      }
    });
  }
}