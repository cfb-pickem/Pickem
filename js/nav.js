// /js/nav.js
import { supabase } from './supabaseClient.js';

const LINKS = [
  { href: './index.html',      key: 'leaderboard', label: 'Leaderboard' },
  { href: './picks.html',      key: 'picks',       label: 'Make Picks', authOnly: true, id: 'nav-picks' },
  { href: './cfb-genius.html', key: 'genius',      label: 'CFB Genius' },
  { href: './stats.html',      key: 'stats',       label: 'Stats' }
];

const CACHE_KEY = 'nav:v1';
const CHANNEL_NAME = 'nav-auth-sync';

function clsActive(isActive){
  return isActive
    ? 'text-[var(--cfp-gold-2)] border-b-2 border-[var(--cfp-gold)]'
    : 'text-gray-300 hover:text-[var(--cfp-ivory)] transition-colors';
}

// Global guard
window.__nav__ = window.__nav__ || { inited: false, sub: null, lastSignedIn: undefined, ch: null };

function renderNavHTML(signedIn, current){
  const items = LINKS
    .filter(l => !l.authOnly || signedIn)
    .map(l => {
      const active = l.key === current;
      const idAttr = l.id ? ` id="${l.id}"` : '';
      return `<li${idAttr}><a href="${l.href}" class="block px-3 py-3 ${clsActive(active)}">${l.label}</a></li>`;
    }).join('');

  return `
    <nav class="mb-5 text-sm font-semibold tracking-wider uppercase font-['Oswald',_sans-serif]">
      <ul class="flex items-center gap-2 border-b border-[rgba(231,231,231,.08)]">
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
}

function cacheWrite({ signedIn, html }){
  localStorage.setItem(CACHE_KEY, JSON.stringify({
    signedIn, html, t: Date.now()
  }));
}
function cacheRead(){
  try {
    const raw = localStorage.getItem(CACHE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    // Optional: expire after 24h
    if (Date.now() - (parsed.t || 0) > 24*60*60*1000) return null;
    return parsed;
  } catch { return null; }
}

function setAuthButtons(isSignedIn){
  const signInLi = document.getElementById('nav-signin');
  const signOutLi = document.getElementById('nav-signout');
  if (signInLi)  signInLi.classList.toggle('hidden',  isSignedIn);
  if (signOutLi) signOutLi.classList.toggle('hidden', !isSignedIn);
}

function redirectIfOnAuthOnlyPage(isSignedIn){
  if (isSignedIn) return;
  const currentKey = document.body?.dataset?.page || '';
  const onAuthOnlyPage = LINKS.some(l => l.authOnly && l.key === currentKey);
  if (!onAuthOnlyPage) return;

  // Defer redirect until the tab is visible to avoid background-tab reload loops
  const go = () => location.replace('./index.html');
  if (document.visibilityState === 'visible') go();
  else {
    const handler = () => { document.removeEventListener('visibilitychange', handler); go(); };
    document.addEventListener('visibilitychange', handler);
  }
}

export default async function initNav(){
  if (window.__nav__.inited) return;
  window.__nav__.inited = true;

  const mount = document.getElementById('site-nav');
  if (!mount) return;

  const current = document.body?.dataset?.page || '';

  // 1) Paint from cache immediately for snappy, consistent UI across tabs
  const cached = cacheRead();
  if (cached?.html) {
    mount.innerHTML = cached.html;
  }

  // 2) Get real session (supabase persists across tabs)
  const { data: { session } } = await supabase.auth.getSession();
  const signedIn = !!session;
  window.__nav__.lastSignedIn = signedIn;

  // If cache didn't exist or was wrong for this page's current tab highlighting, re-render
  const needFresh =
    !cached ||
    cached.signedIn !== signedIn ||
    // if page changed, active class may differ
    !cached.html?.includes(`data-page="${current}"`); // light heuristic; we’ll just re-render below anyway

  if (needFresh) {
    const html = renderNavHTML(signedIn, current);
    mount.innerHTML = html;
    cacheWrite({ signedIn, html });
  }

  // 3) Wire sign-out (once)
  const signOutBtn = document.getElementById('sign-out-btn');
  signOutBtn?.addEventListener('click', async () => {
    try { await supabase.auth.signOut(); }
    finally { location.replace('./index.html'); }
  }, { once: true });

  // 4) Cross-tab sync: BroadcastChannel keeps UI aligned without reloads
  if (!window.__nav__.ch) {
    window.__nav__.ch = new BroadcastChannel(CHANNEL_NAME);
    window.__nav__.ch.onmessage = (e) => {
      if (!e?.data) return;
      if (e.data.type === 'AUTH' && typeof e.data.signedIn === 'boolean') {
        if (e.data.signedIn === window.__nav__.lastSignedIn) return; // no-op
        window.__nav__.lastSignedIn = e.data.signedIn;
        setAuthButtons(e.data.signedIn);
        // Update cache & nav HTML for this page
        const fresh = renderNavHTML(e.data.signedIn, current);
        mount.innerHTML = fresh;
        cacheWrite({ signedIn: e.data.signedIn, html: fresh });
        redirectIfOnAuthOnlyPage(e.data.signedIn);
      }
    };
  }

  // 5) Subscribe once; broadcast simplified state to other tabs
  if (!window.__nav__.sub) {
    const { data: sub } = supabase.auth.onAuthStateChange((event, newSession) => {
      if (event === 'TOKEN_REFRESHED' || event === 'INITIAL_SESSION') return;

      const nowSignedIn = !!newSession;
      if (nowSignedIn === window.__nav__.lastSignedIn) return;

      window.__nav__.lastSignedIn = nowSignedIn;
      setAuthButtons(nowSignedIn);
      const fresh = renderNavHTML(nowSignedIn, current);
      mount.innerHTML = fresh;
      cacheWrite({ signedIn: nowSignedIn, html: fresh });

      // Tell other tabs (no reloads needed)
      try { window.__nav__.ch?.postMessage({ type: 'AUTH', signedIn: nowSignedIn }); } catch {}

      redirectIfOnAuthOnlyPage(nowSignedIn);
    });
    window.__nav__.sub = sub?.subscription ?? sub;
  }

  // 6) One-time check for auth-only page if user is signed out
  redirectIfOnAuthOnlyPage(signedIn);

  // Optional cleanup
  window.addEventListener('beforeunload', () => {
    try { window.__nav__.sub?.unsubscribe?.(); } catch {}
    // Leave cache + channel for other tabs
  }, { once: true });
}