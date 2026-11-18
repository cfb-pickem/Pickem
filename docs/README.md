# docs/

Design notes, plans and research for Picky. This folder is public (GitHub Pages serves it from `main`).

## Rule: no member text in docs/

Never put a league member's messages here, quoted **or paraphrased**, and never put Picky's replies to a
real member here either. Describe a failure by its shape ("a heckle from the leader got a reply that mocked a
good number"), not by what was said. Real exchanges, flub items and fixtures with member text belong in
`/Users/Agent/cfb-pickem-private` only. Charlie's own instructions to the build jobs are fine to quote.

(Decided 2026-11-17 with the architecture review, R10. Unattended jobs write here, so every job spec carries
this rule too.)
