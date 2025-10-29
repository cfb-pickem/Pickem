// /js/supabaseClient.js
import { createClient } from 'https://esm.sh/@supabase/supabase-js@2';

export const supabase = createClient(
  'https://vopdioszofwdkwnujtiq.supabase.co',
  'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InZvcGRpb3N6b2Z3ZGt3bnVqdGlxIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NjE1ODI2MjksImV4cCI6MjA3NzE1ODYyOX0.cD2nNYMEUUOHWQlQC0-lxGZ3s1HVQhWEX_FmgzSsZYw'
);