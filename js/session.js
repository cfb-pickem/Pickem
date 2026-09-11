// js/session.js — one answer to "who is this, and are they the commissioner?"
//
// nav.js asked the teams table this on every page load, and js/sandbox.js needs
// the same answer a moment later. Asking twice means two round trips to settle a
// question that can only ever come back true for one person, so the answer is
// memoised for the life of the page.
//
// THE CACHE IS INVALIDATED ON AUTH CHANGE, which is the only part of this worth
// being careful about: a cached `true` that outlived a sign-out would hand the
// next person at the keyboard a commissioner nav.

import { supabase } from './supabaseClient.js';

let pending = null;

/**
 * { session, user, signedIn, isCommissioner } for the current visitor.
 * Resolves once per page load; safe to call from as many modules as you like.
 */
export function sessionInfo() {
  if (pending) return pending;
  pending = (async () => {
    const { data: { session } } = await supabase.auth.getSession();
    const user = session?.user || null;
    if (!user) return { session: null, user: null, signedIn: false, isCommissioner: false };

    const { data, error } = await supabase
      .from('teams')
      .select('commissioner')
      .eq('user_id', user.id)
      .single();

    // FAIL CLOSED. A signed-in user with no team row, a dropped request, an RLS
    // change — every one of those lands here, and every one of them is "not the
    // commissioner". The gate must never open because a lookup went wrong.
    if (error) console.error('Could not read commissioner status:', error.message);

    return {
      session,
      user,
      signedIn: true,
      isCommissioner: !error && !!data?.commissioner
    };
  })().catch(err => { pending = null; throw err; });
  return pending;
}

supabase.auth.onAuthStateChange(() => { pending = null; });
