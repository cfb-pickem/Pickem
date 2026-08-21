// Bundle entry for js/vendor/supabase.js.
// The site only uses createClient() -> .from() and .auth.*, so this is the whole
// public surface we need. Built by `npm run vendor` (see package.json).
export { createClient } from '@supabase/supabase-js';
