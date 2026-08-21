// /js/supabaseClient.js
// supabase-js is bundled locally (js/vendor/supabase.js, built by `npm run vendor`)
// rather than loaded from esm.sh. The CDN version resolved into a 17-request
// serial dependency chain that delayed the first database query by ~1.3s.
import { createClient } from './vendor/supabase.js';

export const supabase = createClient(
  'https://vopdioszofwdkwnujtiq.supabase.co',
  'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InZvcGRpb3N6b2Z3ZGt3bnVqdGlxIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NjE1ODI2MjksImV4cCI6MjA3NzE1ODYyOX0.cD2nNYMEUUOHWQlQC0-lxGZ3s1HVQhWEX_FmgzSsZYw'
);
