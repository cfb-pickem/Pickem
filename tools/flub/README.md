# Flub loop (nightly) + "bad picky"

`server/flub_loop.py` runs every night at 23:00 CT (machine clock is CT) from LaunchAgent
`com.pickem.flub-nightly`. Output (all private, 0600): `/Users/Agent/cfb-pickem-private/flubs/`
(`harvest-`, `facts-`, `classify-`, `added-`, `fix-<date>.json`, `report-<date>.md`). Every regression
item is built from a real member's text, so every item is private: `cfb-pickem-private/flubs/items-private.json`
(review R10, 2026-11-17: nothing is published to the public repo any more; the old public file
`server/eval/flub_items.json` stays empty and is still read). They run in the text exam (`eval/run.py --text`,
category `flub`).

Install (once, after merge):

    cp tools/flub/com.pickem.flub-nightly.plist ~/Library/LaunchAgents/
    launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.pickem.flub-nightly.plist

Try it without Opus, merges or reloads: `server/.venv/bin/python3 -I server/flub_loop.py nightly --dry --date YYYY-MM-DD`.

Switches: `touch /Users/Agent/cfb-pickem-private/FLUB_AUTOFIX_OFF` (or `PICKY_FLUB_AUTOFIX=0`) = no fix
round; `PICKY_NO_ANTHROPIC=1` = code-only signals, no Opus at all.

The fix round (Opus in a throwaway worktree, branch `flub-fix-<date>`) ships to main only when the
model-free suite has no new failures and the text exam (fix on :8805 vs main on :8806, both inside
`/usr/bin/lockf -t 14400 /tmp/picky-exam.lock`) keeps det. pass >= baseline, invented 0, leaks 0 and
flub items passing >= baseline; then it fast-forwards /Users/Agent/cfb-pickem (skipped if that tree is
dirty) and reloads com.pickem.picky-group + com.pickem.chat. It never texts anyone.

"bad picky": `server/.venv/bin/python3 -I server/flag_flub.py --text "<the bad reply>" [--image <path>]
[--note "<what was wrong>"]` queues a flag (`flubs/queue.jsonl`) with the last live exchanges; members
can also text "bad picky" / "@Picky that's wrong" (group sidecar logs it, no reply spam).
