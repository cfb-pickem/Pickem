// nav.js
import { supabase } from './supabaseClient.js';

let authSubscription = null;   // ensure we only register once
let isMounted = false;         // prevent duplicate init work
let lastUserId = null;         // avoid redundant re-renders for the same user

// Map page keys to hrefs and labels (adjust to your actual pages)
const NAV_ITEMS = [
  { key: 'home',   href: './index.html',      label: 'Home' },
  { key: 'picks',  href: './picks.html',      label: 'Make Picks' },
  { key: 'stats',  href: './stats.html',      label: 'Stats' },
  { key: 'genius', href: './cfb-genius.html', label: 'CFB Genius' },
];

function currentPageKey() {
  // Prefer explicit data attribute set by pages (e.g., <body data-page="picks">)
  const dataPage = document.body?.dataset?.page;
  if (dataPage) return String(dataPage).toLowerCase();

  // Fallback: infer from path
  const path = (location.pathname || '').toLowerCase();
  if (path.includes('picks'))  return 'picks';
  if (path.includes('stats'))  return 'stats';
  if (path.includes('genius')) return 'genius';
  return 'home';
}

function renderNavHTML({ signedIn, userEmail }) {
  const active = currentPageKey();

  const links = NAV_ITEMS.map(item => {
    const isActive = item.key === active;
    const base =
      'px-3 py-2 rounded-md text-sm font-medium hover:bg-white/5 transition';
    const cls = isActive
      ? `${base} text-[var(--cfp-ivory)] bg-white/10`
      : `${base} text-gray-300`;
    return `<a href="${item.href}" class="${cls}">${item.label}</a>`;
  }).join('');

  const rightSide = signedIn
    ? `
      <div class="flex items-center gap-3">
        <span class="text-xs text-gray-300 hidden sm:inline">Signed in${userEmail ? `: ${userEmail}` : ''}</span>
        <button id="sign-out-btn" class="px-3 py-1.5 text-sm font-semibold bg-[var(--cfp-gold)] text-black rounded ring-gold">
          Sign out
        </button>
      </div>
    `
    : `
      <a href="./signin.html" class="px-3 py-1.5 text-sm font-semibold bg-[var(--cfp-gold)] text-black rounded ring-gold">
        Sign in
      </a>
    `;

  return /* html */`
    <nav class="cfp-card gold-shadow mb-4">
      <div class="px-4 py-3 md:px-6 md:py-4 flex items-center justify-between">
        <div class="flex items-center gap-6">
          <a href="./index.html" class="text-[var(--cfp-ivory)] font-bold tracking-wide">
            CFB Pick'em
          </a>
          <div class="hidden md:flex items-center gap-2">
            ${links}
          </div>
        </div>
        ${rightSide}
      </div>
      <!-- Simple mobile row -->
      <div class="px-4 pb-3 md:hidden flex flex-wrap gap-2">
        ${links}
      </div>
    </nav>
  `;
}

async function renderNav() {
  const mount = document.getElementById('site-nav');
  if (!mount) return;

  const { data: { session } } = await supabase.auth.getSession();
  const signedIn = !!session;
  const userEmail = session?.user?.email || '';

  // Short-circuit if same user and we've already rendered
  const currentId = session?.user?.id || null;
  if (lastUserId === currentId && mount.dataset.rendered === '1') {
    return;
  }
  lastUserId = currentId;

  mount.innerHTML = renderNavHTML({ signedIn, userEmail });
  mount.dataset.rendered = '1';

  // Wire up sign-out if shown
  const signOutBtn = document.getElementById('sign-out-btn');
  if (signOutBtn) {
    signOutBtn.addEventListener('click', async () => {
      try {
        await supabase.auth.signOut();
        // Minimal navigation on explicit sign-out:
        // If the current page needs auth, send to signin; otherwise just re-render.
        const needsAuth = document.body?.dataset?.page === 'picks';
        if (needsAuth) {
          location.href = './signin.html';
        } else {
          await renderNav();
        }
      } catch (e) {
        console.error('Sign-out failed:', e);
      }
    });
  }
}

export default async function initNav() {
  if (isMounted) return;
  isMounted = true;

  await renderNav();

  // Register a single global auth listener that *does not* reload the page.
  if (!authSubscription) {
    const { data: { subscription } } = supabase.auth.onAuthStateChange(async (event, session) => {
      // Only re-render on meaningful boundary changes.
      // Do NOT reload on TOKEN_REFRESHED / USER_UPDATED (these often fire on tab focus).
      switch (event) {
        case 'SIGNED_IN':
        case 'SIGNED_OUT':
          await renderNav();
          break;
        // Optional: if you'd like the email to refresh after profile edits:
        // case 'USER_UPDATED':
        //   await renderNav();
        //   break;
        default:
          // TOKEN_REFRESHED, PASSWORD_RECOVERY, etc. → ignore (no reloads).
          break;
      }
    });
    authSubscription = subscription;
  }
}