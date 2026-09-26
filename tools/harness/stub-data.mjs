// Scripted responses, so a test can drive js/blackjack.js's RPC-touching
// functions (initPit, revealHand, ...) through real module code without a
// live Supabase project behind them. tools/harness/js/supabaseClient.js reads
// from this; a test sets SCRIPT.rpc / SCRIPT.tables before importing
// tools/harness/js/blackjack.js.
export const SCRIPT = { rpc: {}, tables: {}, calls: [] };
