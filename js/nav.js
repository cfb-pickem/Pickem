// /js/nav.js
import { supabase } from './supabaseClient.js';
import { initPrefetch } from './prefetch.js';

const LINKS = [
  { href: './index.html',        key: 'leaderboard',  label: 'Leaderboard' },
  { href: './picks.html',        key: 'picks',        label: 'Make Picks', authOnly: true, id: 'nav-picks' },
  { href: './cfb-genius.html',   key: 'genius',       label: 'League Info' },
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
  const PAGE_LABELS = {
    leaderboard: 'Leaderboard', picks: 'Make Picks', genius: 'League Info',
    stats: 'Stats', commissioner: 'Commissioner', tiebreakers: 'Tiebreakers'
  };
  const currentLabel = PAGE_LABELS[current] || 'CFB Pick&rsquo;em';
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
      <!-- Mobile bar. The page name lives here on purpose: a hamburger and a
           Sign out button told you nothing about where you were. -->
      <div class="navbar md:hidden">
        <button id="nav-toggle" class="navbar-burger" aria-label="Open menu"
                aria-expanded="false" aria-controls="nav-mobile-menu">
          <svg id="nav-icon-open" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path stroke-linecap="round" d="M4 6h16M4 12h16M4 18h16"/>
          </svg>
          <svg id="nav-icon-close" class="hidden" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path stroke-linecap="round" d="M6 18L18 6M6 6l12 12"/>
          </svg>
        </button>
        <span class="navbar-where">${currentLabel}</span>
        ${signedIn
          ? `<button id="nav-signout-mobile" class="navbar-auth">Sign out</button>`
          : `<a href="./signin.html" class="navbar-auth">Sign In</a>`
        }
      </div>

      <!-- Mobile menu: a sheet over the page rather than a block that shoves it
           down. Backdrop closes it, so does Escape. -->
      <div id="nav-scrim" class="nav-scrim md:hidden" hidden></div>
      <ul id="nav-mobile-menu" class="nav-sheet md:hidden" hidden>
        ${visibleLinks.map(l => {
          const active = l.key === current;
          return `<li><a href="${l.href}" class="nav-sheet-link${active ? ' is-active' : ''}">
            <span>${l.label}</span>
            ${active ? '<span class="nav-sheet-dot" aria-hidden="true"></span>' : ''}
          </a></li>`;
        }).join('')}
      </ul>

      <!-- Desktop: horizontal nav (hidden on mobile) -->
      <ul class="hidden md:flex flex-wrap items-center gap-2 border-b border-[rgba(231,231,231,.08)]">
        ${items}
        <li class="ml-auto ${signedIn ? 'hidden' : ''}" id="nav-signin">
          <a href="./signin.html" class="block px-4 py-3 text-gray-300 hover:text-[var(--cfp-ivory)] transition-colors">Sign Up / Sign In</a>
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

  const scrim = document.getElementById('nav-scrim');

  if (toggle && mobileMenu) {
    let menuOpen = false;

    const setOpen = open => {
      menuOpen = open;
      // `hidden` first so the sheet is out of the tab order when closed, then a
      // class on the next frame so the transition actually runs.
      if (open) {
        mobileMenu.hidden = false;
        if (scrim) scrim.hidden = false;
        requestAnimationFrame(() => {
          mobileMenu.classList.add('is-open');
          scrim?.classList.add('is-open');
        });
        document.body.style.overflow = 'hidden';
      } else {
        mobileMenu.classList.remove('is-open');
        scrim?.classList.remove('is-open');
        document.body.style.overflow = '';
        setTimeout(() => {
          if (!menuOpen) { mobileMenu.hidden = true; if (scrim) scrim.hidden = true; }
        }, 200);
      }
      toggle.setAttribute('aria-expanded', String(open));
      toggle.setAttribute('aria-label', open ? 'Close menu' : 'Open menu');
      iconOpen?.classList.toggle('hidden', open);
      iconClose?.classList.toggle('hidden', !open);
    };

    toggle.addEventListener('click', () => setOpen(!menuOpen));
    scrim?.addEventListener('click', () => setOpen(false));
    document.addEventListener('keydown', e => { if (e.key === 'Escape' && menuOpen) setOpen(false); });
    // Tapping a link navigates, but close anyway so a cached back-nav is tidy.
    mobileMenu.querySelectorAll('a').forEach(a => a.addEventListener('click', () => setOpen(false)));
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

  // Links exist now, so hover/tap prefetching can be wired up.
  initPrefetch();

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
