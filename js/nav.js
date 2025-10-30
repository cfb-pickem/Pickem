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

// ensure we only subscribe once per page
let didSubscribe = false;

export default async function initNav(){
  const mount = document.getElementById('site-nav');
  if (!mount) return;

  const current = document.body?.dataset?.page || '';

  // --- session ---
  const { data: { session } } = await supabase.auth.getSession();
  const signedIn = !!session;

  // --- commissioner check (table: team) ---
  // expects table 'team' with columns: email (text), commissioner (boolean)
  let isCommissioner = false;
  if (signedIn && session?.user?.email) {
    try {
      const email = session.user.email.toLowerCase();
      const { data: teamRow, error } = await supabase
        .from('team')
        .select('commissioner')
        .eq('email', email)
        .single();
      if (!error && teamRow?.commissioner === true) {
        isCommissioner = true;
      }
    } catch (e) {
      console.warn('Commissioner check failed:', e);
    }
  }

  // --- build links (append Commissioner only if allowed) ---
  const dynamicLinks = [...LINKS];
  if (isCommissioner) {
    dynamicLinks.push({
      href: './commissioner.html',
      key: 'commissioner',
      label: 'Commissioner',
      authOnly: true,
      id: 'nav-commissioner'
    });
  }

  const items = dynamicLinks
    .filter(l => !l.authOnly || signedIn)
    .map(l => {
      const active = l.key === current;
      const idAttr = l.id ? ` id="${l.id}"` : '';
      return `<li${idAttr}><a href="${l.href}" class="block px-3 py-3 ${clsActive(active)}">${l.label}</a></li>`;
    }).join('');

  // --- render ---
  mount.innerHTML = `
    <nav class="mb-5 text-sm font-semibold tracking-wider uppercase font-['Oswald',_sans-serif]">
      <ul class="flex items-center gap-2 border-b border-[rgba(231,231,231,.08)]">
        ${items}
        <!-- right-aligned auth actions -->
        <li class="ml-auto ${signedIn ? 'hidden' : ''}" id="nav-signin">
          <a href="./signin.html" class="block px-3 py-3 text-gray-300 hover:text-[var(--cfp-ivory)] transition-colors">Sign in</a>
        </li>
        <li class="ml-auto ${signedIn ? '' : 'hidden'}" id="nav-signout">
          <button id="sign-out-btn" class="block px-3 py-3 text-gray-300 hover:text-[var(--cfp-ivory)] transition-colors">Sign out</button>
        </li>
      </ul>
    </nav>
  `;

  // --- sign out wiring ---
  const signOutBtn = document.getElementById('sign-out-btn');
  signOutBtn?.addEventListener('click', async () => {
    await supabase.auth.signOut();
    window.location.href = './index.html';
  });

  // toggle auth buttons (no full re-render)
  function setAuthButtons(isSignedIn){
    const signInLi = document.getElementById('nav-signin');
    const signOutLi = document.getElementById('nav-signout');
    if (signInLi)  signInLi.classList.toggle('hidden',  isSignedIn);
    if (signOutLi) signOutLi.classList.toggle('hidden', !isSignedIn);
  }

  // if user is on an auth-only page and signs out, kick to home
  function redirectIfOnAuthOnlyPage(isSignedIn){
    if (isSignedIn) return;
    const onAuthOnlyPage = dynamicLinks.some(l => l.authOnly && l.key === (document.body?.dataset?.page || ''));
    if (onAuthOnlyPage) window.location.href = './index.html';
  }

  // subscribe once
  if (!didSubscribe) {
    didSubscribe = true;
    supabase.auth.onAuthStateChange((event) => {
      if (event === 'SIGNED_IN') {
        // commissioner visibility may change; easiest is to reload
        window.location.reload();
      } else if (event === 'SIGNED_OUT') {
        setAuthButtons(false);
        redirectIfOnAuthOnlyPage(false);
      }
      // ignore token refresh/initial
    });
  }
}