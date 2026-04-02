// /js/nav.js
import { supabase } from './supabaseClient.js';

const LINKS = [
  { href: './index.html',        key: 'leaderboard',  label: 'Leaderboard' },
  { href: './picks.html',        key: 'picks',        label: 'Make Picks', authOnly: true, id: 'nav-picks' },
  { href: './tiebreakers.html',  key: 'tiebreakers',  label: 'Tiebreakers' },
  { href: './cfb-genius.html',   key: 'genius',       label: 'CFB Genius' },
  { href: './stats.html',        key: 'stats',        label: 'Stats' },
  { href: './commissioner.html', key: 'commissioner', label: 'Commissioner', authOnly: true, id: 'nav-commissioner' }
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

  // Build nav items
  const visibleLinks = LINKS.filter(l => {
    if (l.key === 'commissioner' && !isCommissioner) return false;
    return !l.authOnly || signedIn;
  });

  const items = visibleLinks.map(l => {
    const active = l.key === current;
    const idAttr = l.id ? ` id="${l.id}"` : '';
    return `<li${idAttr}><a href="${l.href}" class="block px-4 py-3 ${clsActive(active)}">${l.label}</a></li>`;
  }).join('');

  mount.innerHTML = `
    <nav class="mb-5 text-sm font-semibold tracking-wider uppercase font-['Oswald',_sans-serif]">
      <!-- Mobile: hamburger + brand row -->
      <div class="flex items-center justify-between border-b border-[rgba(231,231,231,.08)] md:hidden">
        <button id="nav-toggle" class="p-3 text-gray-300 hover:text-[var(--cfp-ivory)] transition-colors" aria-label="Toggle menu">
          <svg id="nav-icon-open" class="w-6 h-6" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
            <path stroke-linecap="round" d="M4 6h16M4 12h16M4 18h16"/>
          </svg>
          <svg id="nav-icon-close" class="w-6 h-6 hidden" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
            <path stroke-linecap="round" d="M6 18L18 6M6 6l12 12"/>
          </svg>
        </button>
        <div class="flex items-center gap-2 pr-3">
          ${signedIn
            ? `<button id="nav-signout-mobile" class="px-3 py-2 text-xs text-gray-300 hover:text-[var(--cfp-ivory)] transition-colors">Sign out</button>`
            : `<a href="./signin.html" class="px-3 py-2 text-xs text-gray-300 hover:text-[var(--cfp-ivory)] transition-colors">Sign in</a>`
          }
        </div>
      </div>

      <!-- Mobile dropdown (hidden by default) -->
      <ul id="nav-mobile-menu" class="md:hidden hidden flex-col border-b border-[rgba(231,231,231,.08)] bg-[var(--cfp-black)]"
          style="transition: max-height .25s ease, opacity .2s ease; max-height: 0; opacity: 0; overflow: hidden;">
        ${visibleLinks.map(l => {
          const active = l.key === current;
          return `<li><a href="${l.href}" class="block px-5 py-3 border-t border-[rgba(231,231,231,.04)] ${active ? 'text-[var(--cfp-gold-2)] bg-[rgba(233,185,73,.06)]' : 'text-gray-300 hover:text-[var(--cfp-ivory)] hover:bg-[rgba(233,185,73,.04)]'} transition-colors">${l.label}</a></li>`;
        }).join('')}
      </ul>

      <!-- Desktop: horizontal nav (hidden on mobile) -->
      <ul class="hidden md:flex flex-wrap items-center gap-2 border-b border-[rgba(231,231,231,.08)]">
        ${items}
        <li class="ml-auto ${signedIn ? 'hidden' : ''}" id="nav-signin">
          <a href="./signin.html" class="block px-4 py-3 text-gray-300 hover:text-[var(--cfp-ivory)] transition-colors">Sign in</a>
        </li>
        <li class="ml-auto ${signedIn ? '' : 'hidden'}" id="nav-signout">
          <button id="sign-out-btn" class="block px-4 py-3 text-gray-300 hover:text-[var(--cfp-ivory)] transition-colors">Sign out</button>
        </li>
      </ul>
    </nav>
  `;

  // Redirect if user is not commissioner but tries to access commissioner.html
  if (current === 'commissioner' && !isCommissioner) {
    window.location.href = './index.html';
  }

  // Wire hamburger toggle
  const toggle = document.getElementById('nav-toggle');
  const mobileMenu = document.getElementById('nav-mobile-menu');
  const iconOpen = document.getElementById('nav-icon-open');
  const iconClose = document.getElementById('nav-icon-close');

  if (toggle && mobileMenu) {
    let menuOpen = false;
    toggle.addEventListener('click', () => {
      menuOpen = !menuOpen;
      if (menuOpen) {
        mobileMenu.classList.remove('hidden');
        mobileMenu.classList.add('flex');
        requestAnimationFrame(() => {
          mobileMenu.style.maxHeight = mobileMenu.scrollHeight + 'px';
          mobileMenu.style.opacity = '1';
        });
      } else {
        mobileMenu.style.maxHeight = '0';
        mobileMenu.style.opacity = '0';
        mobileMenu.addEventListener('transitionend', () => {
          if (!menuOpen) {
            mobileMenu.classList.add('hidden');
            mobileMenu.classList.remove('flex');
          }
        }, { once: true });
      }
      iconOpen?.classList.toggle('hidden', menuOpen);
      iconClose?.classList.toggle('hidden', !menuOpen);
    });
  }

  // Wire sign-out (desktop)
  const signOutBtn = document.getElementById('sign-out-btn');
  signOutBtn?.addEventListener('click', async () => {
    await supabase.auth.signOut();
    window.location.href = './index.html';
  });

  // Wire sign-out (mobile)
  const signOutMobile = document.getElementById('nav-signout-mobile');
  signOutMobile?.addEventListener('click', async () => {
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
