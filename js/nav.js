// /js/nav.js
import { supabase } from './supabaseClient.js';

const LINKS = [
  { href: './index.html',      key: 'leaderboard', label: 'Leaderboard' },
  { href: './picks.html',      key: 'picks',       label: 'Make Picks', authOnly: true, id: 'nav-picks' },
  { href: './cfb-genius.html', key: 'genius',      label: 'CFB Genius' },
  { href: './stats.html',      key: 'stats',       label: 'Stats' },
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

  // Determine commissioner
  let isCommissioner = false;
  if (signedIn && user) {
    const { data } = await supabase
      .from('teams')
      .select('commissioner')
      .eq('user_id', user.id)
      .single();

    isCommissioner = !!data?.commissioner;
  }

  // Desktop nav items
  const visibleLinks = LINKS.filter(l => {
    if (l.key === 'commissioner' && !isCommissioner) return false;
    return !l.authOnly || signedIn;
  });

  const items = visibleLinks
    .map(l => {
      const active = l.key === current;
      const idAttr = l.id ? ` id="${l.id}"` : '';
      return `<li${idAttr}><a href="${l.href}" class="block px-3 py-3 ${clsActive(active)}">${l.label}</a></li>`;
    })
    .join('');

  // Mobile dropdown
  let mobileOptions = visibleLinks
    .map(l => {
      const active = l.key === current;
      return `<option value="${l.href}" ${active ? 'selected' : ''}>${l.label}</option>`;
    })
    .join('');

  if (!signedIn) {
    mobileOptions += `<option value="./signin.html">Sign in</option>`;
  } else {
    mobileOptions += `<option value="__signout">Sign out</option>`;
  }

  // Render nav
  mount.innerHTML = `
    <nav class="mb-5 text-xs sm:text-sm font-semibold tracking-wider uppercase font-['Oswald',_sans-serif]">

      <!-- Desktop Navigation -->
      <div class="hidden sm:block">
        <ul class="flex flex-wrap sm:flex-nowrap items-center gap-2 border-b border-[rgba(231,231,231,.08)]">

          ${items}

          <li class="ml-auto ${signedIn ? 'hidden' : ''}" id="nav-signin">
            <a href="./signin.html" class="block px-3 py-3 text-gray-300 hover:text-[var(--cfp-ivory)] transition-colors">
              Sign in
            </a>
          </li>

          <li class="ml-auto ${signedIn ? '' : 'hidden'}" id="nav-signout">
            <button id="sign-out-btn" class="block px-3 py-3 text-gray-300 hover:text-[var(--cfp-ivory)] transition-colors">
              Sign out
            </button>
          </li>

        </ul>
      </div>

      <!-- Mobile Dropdown -->
      <div class="sm:hidden border-b border-[rgba(231,231,231,.08)] py-2">
        <select id="mobile-nav"
                class="w-full bg-transparent text-[0.72rem] tracking-[0.18em] uppercase px-2 py-2 border border-[rgba(231,231,231,.25)]">
          ${mobileOptions}
        </select>
      </div>

    </nav>
  `;

  // Protect Commissioner page
  if (current === 'commissioner' && !isCommissioner) {
    window.location.href = './index.html';
  }

  // Desktop sign-out
  document.getElementById('sign-out-btn')
    ?.addEventListener('click', async () => {
      await supabase.auth.signOut();
      window.location.href = './index.html';
    });

  // Mobile nav
  const mobileNav = document.getElementById('mobile-nav');
  mobileNav?.addEventListener('change', async e => {
    const value = e.target.value;

    if (value === '__signout') {
      await supabase.auth.signOut();
      window.location.href = './index.html';
      return;
    }

    if (value) window.location.href = value;
  });

  // Auth change listener
  if (!didSubscribe) {
    didSubscribe = true;

    supabase.auth.onAuthStateChange((event) => {
      if (event === 'SIGNED_IN') {
        document.getElementById('nav-signin')?.classList.add('hidden');
        document.getElementById('nav-signout')?.classList.remove('hidden');
      }

      if (event === 'SIGNED_OUT') {
        document.getElementById('nav-signout')?.classList.add('hidden');
        document.getElementById('nav-signin')?.classList.remove('hidden');

        if (visibleLinks.some(l => l.authOnly && l.key === current)) {
          window.location.href = './index.html';
        }
      }
    });
  }
}