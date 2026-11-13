-- T5. Concurrent resolver safety: two resolver calls racing on the same
-- pending picks should still pay out at most one jackpot, protected by the
-- FOR UPDATE lock on slots_jackpot. pgTAP runs in a single psql session
-- and has no native primitive for driving two concurrent backends against
-- one database, so this test is a documented skip. The invariant is still
-- asserted structurally by the `for update` + `not paid_out` pair in
-- resolve_slot_picks(); a cross-session race test would belong in an
-- integration harness (e.g. a bash script spawning two psql processes),
-- not in pgTAP.

begin;
select plan(1);
select skip('pgTAP cannot drive two concurrent backends in one session; cross-session race coverage belongs in an integration test', 1);
select * from finish();
rollback;
