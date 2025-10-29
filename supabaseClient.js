// js/supabaseClient.js
import { createClient } from '@supabase/supabase-js';

/**
 * Supply Supabase URL/Key in any of these ways (first non-empty wins):
 *  1) <meta name="supabase-url" content="https://xxxx.supabase.co">
 *     <meta name="supabase-key" content="public-anon-key">
 *  2) window.__SUPABASE = { url: '...', key: '...' }
 *  3) Hardcoded strings below (safe for demos; consider environment injection for production)
 */
function readMeta(name) {
  const el = document.querySelector(`meta[name="${name}"]`);
  return el?.getAttribute('content')?.trim() || '';
}

const META_URL = readMeta('supabase-url');
const META_KEY = readMeta('supabase-key');

const FALLBACK = (window.__SUPABASE && {
  url: window.__SUPABASE.url,
  key: window.__SUPABASE.key,
}) || { url: '', key: '' };

const SUPABASE_URL =
  META_URL ||
  FALLBACK.url ||
  'https://YOUR_PROJECT.supabase.co';

const SUPABASE_ANON_KEY =
  META_KEY ||
  FALLBACK.key ||
  'YOUR_PUBLIC_ANON_KEY';

if (!SUPABASE_URL || !SUPABASE_ANON_KEY) {
  // Helpful console hint if creds are missing
  // (won't break your app; you'll just see auth queries fail)
  console.warn(
    '[supabaseClient] Missing URL or anon key. ' +
      'Add <meta name="supabase-url"> and <meta name="supabase-key">, ' +
      'or set window.__SUPABASE = { url, key }, or hardcode here.'
  );
}

export const supabase = createClient(SUPABASE_URL, SUPABASE_ANON_KEY, {
  auth: {
    persistSession: true,
    autoRefreshToken: true,
    detectSessionInUrl: true,
    flowType: 'pkce',              // solid default for browser apps
    storageKey: 'cfb_pickem_auth', // avoid collisions with other projects on the same domain
  },
  // Optional niceties:
  // global: { headers: { 'x-application-name': 'cfb-pickem' } },
  // realtime: { params: { eventsPerSecond: 2 } },
});

/**
 * Gentle “nudge” to ensure tokens refresh silently when the tab regains focus.
 * Supabase already auto-refreshes, but this avoids edge cases after long sleeps.
 * No reloads — ever.
 */
let _refreshing = false;
async function nudgeSessionRefresh() {
  if (_refreshing) return;
  _refreshing = true;
  try {
    // getSession() will refresh if needed; it’s a no-op if still valid
    await supabase.auth.getSession();
  } catch (e) {
    // Non-fatal; auth UI will handle signed-out state if it happens
    console.debug('[supabaseClient] refresh nudge failed:', e?.message || e);
  } finally {
    _refreshing = false;
  }
}
window.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible') nudgeSessionRefresh();
});
window.addEventListener('focus', nudgeSessionRefresh);