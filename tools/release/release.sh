#!/bin/zsh
# Picky's one way to ship to live (review R4, 2026-11-17).
#
#   tools/release/release.sh [ref]              # default: local main of /Users/Agent/cfb-pickem (jobs merge there;
#                                               # origin may lag if GitHub pushes fail)
#   tools/release/release.sh --dry-run [ref]    # prints every step, changes nothing (shows the rollback path)
#   tools/release/release.sh --fail-canary ...  # rollback drill: a real release whose canary is forced to fail
#   tools/release/release.sh --rollback         # put live back on the previous release tag
#
# Steps: game-day freeze check -> model-free suite on a fresh worktree of <ref> (no NEW failures: anything it fails
# must also fail on the live commit, and the HARD tests / fixed-flub lane rows must pass no matter what) -> tag
# picky-live-<stamp> (pushed) -> under the exam lock: switch /Users/Agent/picky-live to the tag -> reload
# the KeepAlive services (bootout + bootstrap, error 5 retried) -> deploy the watcher if it changed ->
# deep /health + a model canary through /group (X-Picky-Test) -> on any failure, roll back to the previous
# tag, reload, re-check. Every run appends one row to cfb-pickem-private/releases.jsonl.
#
# Live runs from /Users/Agent/picky-live (a detached worktree; server/.venv, .venv-flux, cache and logs are
# symlinks to the dev tree's). Nobody edits it by hand: a dirty live tree is logged and reset to the tag.
# Freeze: Fri 18:00 - Sun 04:00 CT nothing ships (exit 3). PICKY_FREEZE_OVERRIDE=1 = a real emergency only.
setopt pipefail
REPO=/Users/Agent/cfb-pickem
LIVE=${PICKY_LIVE_DIR:-/Users/Agent/picky-live}
PRIV=${PICKY_PRIVATE_DIR:-/Users/Agent/cfb-pickem-private}
PY=$REPO/server/.venv/bin/python3
HERE=${0:A:h}
U=gui/$(id -u)
SERVICES=(com.pickem.picky-group com.pickem.chat com.pickem.live-games com.pickem.close-cover)
LOG=$PRIV/releases.jsonl
RLOCK=/tmp/picky-release.lock
ELOCK=/tmp/picky-exam.lock
EXAM_WAIT=${PICKY_RELEASE_EXAM_WAIT:-3600}
# guardrail / leak / live-Claude-switch checks: a failure blocks even if live fails it too (so does a fixed flub's
# lane-table row, "[flub <id>]" in the test_lane_table.py log)
HARD_TESTS=(test_privacy.py test_picky_reveal.py test_reveal_lanes.py test_fact_guard.py test_matchup_guard.py
            test_anthropic_down.py)

DRY=0; FAILC=0; MODE=release; REF=main; PHASE=""
ORIG=("$@")
while [ $# -gt 0 ]; do
  case $1 in
    --dry-run) DRY=1;;
    --fail-canary) FAILC=1;;
    --rollback) MODE=rollback;;
    __live) PHASE=live; shift; TAG=$1; SHA=$2; shift;;
    *) REF=$1;;
  esac
  shift
done

say() { print -r -- "$(date '+%H:%M:%S') release: $*"; }
run() { if [ $DRY = 1 ]; then say "[dry-run] $*"; else "$@"; fi; }
record() {  # record <result> <extra json fields...>
  [ $DRY = 1 ] && { say "[dry-run] would log: $1"; return; }
  print -r -- "{\"ts\": $(date +%s), \"mode\": \"$MODE\", \"tag\": \"${TAG:-}\", \"sha\": \"${SHA:-}\", \"prev\": \"${PREV:-}\", \"result\": \"$1\"${2:+, $2}}" >> $LOG
}

reload() {  # bootout + bootstrap; launchd sometimes says "5: Input/output error" right after a bootout
  local L=$1 P=~/Library/LaunchAgents/$1.plist
  if [ $DRY = 1 ]; then say "[dry-run] reload $L"; return 0; fi
  launchctl bootout $U/$L 2>/dev/null; sleep 2
  launchctl bootstrap $U $P 2>/dev/null || { sleep 3; launchctl bootstrap $U $P 2>/dev/null; } || { sleep 5; launchctl bootstrap $U $P; }
}

healthy() {  # up to 150 s: cheap /health on both, then the sidecar's deep check (code-lane pipeline run)
  [ $DRY = 1 ] && { say "[dry-run] health + deep health"; return 0; }
  local i
  for i in {1..50}; do
    curl -fs -m 3 http://127.0.0.1:8768/health >/dev/null && curl -fs -m 3 http://127.0.0.1:8766/health >/dev/null && break
    sleep 3
  done
  curl -fs -m 90 "http://127.0.0.1:8768/health?deep=1" >/dev/null
}

canary() {  # one X-Picky-Test ask through /group, model on (the fact lane's voice line), nothing saved
  if [ $FAILC = 1 ]; then say "canary FORCED to fail (--fail-canary drill)"; return 1; fi
  [ $DRY = 1 ] && { say "[dry-run] canary: $PY -I $LIVE/server/canary.py --model"; return 0; }
  /usr/bin/python3 -I $LIVE/server/canary.py --model
}

changed() {  # changed <from> <to> <path>: did <path> differ between two refs
  [ -z "$1" ] && return 0
  ! git -C $REPO diff --quiet $1 $2 -- $3 2>/dev/null
}

switch_live() {  # switch_live <ref>
  local dirty
  dirty=$(git -C $LIVE status --porcelain --untracked-files=no 2>/dev/null)
  if [ -n "$dirty" ]; then
    say "live tree was edited by hand, resetting it: $dirty"
    [ $DRY = 0 ] && print -r -- "{\"ts\": $(date +%s), \"mode\": \"dirty-live\", \"files\": \"${dirty//$'\n'/; }\"}" >> $LOG
  fi
  run git -C $LIVE checkout -q -f --detach $1
  for x in .venv .venv-flux cache logs; do  # untracked shared links (mkwt.sh); a checkout can drop one
    [ $DRY = 0 ] && [ -e $REPO/server/$x ] && [ ! -e $LIVE/server/$x ] && ln -sfn $REPO/server/$x $LIVE/server/$x
  done
}

deploy_services() {  # deploy_services <from> <to>
  local L
  for L in $SERVICES; do reload $L; done
  if changed "$1" "$2" server/image.py; then reload com.pickem.image; fi
  if changed "$1" "$2" tools/picky-imessage/watcher.py; then
    if [ $DRY = 1 ]; then say "[dry-run] deploy watcher"; else $PY -I $HERE/deploy_watcher.py $LIVE || say "WARNING: watcher deploy not verified"; fi
  fi
}

# ---- phase 2 (under the exam lock): switch, reload, verify, roll back ------------------------------------
if [ "$PHASE" = live ]; then
  PREV=$(git -C $LIVE rev-parse HEAD 2>/dev/null)
  PREV_TAG=$(git -C $REPO describe --tags --exact-match --match 'picky-live-*' $PREV 2>/dev/null || echo $PREV)
  say "live $PREV_TAG -> $TAG"
  switch_live $TAG
  deploy_services "$PREV" "$SHA"
  if healthy && canary; then
    say "LIVE on $TAG (health + canary ok)"
    record shipped "${PICKY_RELEASE_TOLERATED:+\"tolerated\": \"$PICKY_RELEASE_TOLERATED\"}"
    exit 0
  fi
  say "health/canary FAILED on $TAG: rolling back to $PREV_TAG"
  if [ -z "$PREV" ]; then record failed-no-rollback; exit 1; fi
  switch_live "$PREV"
  deploy_services "$SHA" "$PREV"
  FAILC=0
  if healthy && canary; then
    say "rolled back to $PREV_TAG (health + canary ok)"
    record rolled-back "\"rolled_back_to\": \"$PREV_TAG\""
    exit 1
  fi
  say "ROLLBACK ALSO UNHEALTHY on $PREV_TAG: needs a human"
  record rollback-unhealthy
  exit 2
fi

# ---- phase 1: freeze, one release at a time, suite, tag -------------------------------------------------
if [ -z "$PICKY_RELEASE_LOCKED" ] && [ $DRY = 0 ]; then
  PICKY_RELEASE_LOCKED=1 exec /usr/bin/lockf -t 0 $RLOCK $0 "${ORIG[@]}"
fi
if [ ! -d $LIVE/.git ] && [ ! -f $LIVE/.git ] && [ -z "$PICKY_RELEASE_BOOTSTRAP" ]; then
  say "no live worktree at $LIVE yet (cutover not done): nothing shipped"; record no-live-tree; exit 1
fi
if ! /usr/bin/python3 -I $REPO/server/freeze.py check-freeze; then
  if [ "$PICKY_FREEZE_OVERRIDE" = 1 ]; then say "FREEZE OVERRIDE set: shipping anyway (logged)"; else record frozen; exit 3; fi
fi

if [ $MODE = rollback ]; then
  CUR=$(git -C $LIVE rev-parse HEAD)
  TAG=$(git -C $REPO tag --list 'picky-live-*' --sort=-creatordate --merged $CUR | while read t; do
          [ "$(git -C $REPO rev-list -n1 $t)" != "$CUR" ] && { echo $t; break; }; done)
  [ -z "$TAG" ] && { say "no earlier release tag to roll back to"; exit 1; }
  SHA=$(git -C $REPO rev-list -n1 $TAG)
  say "manual rollback to $TAG"
  FAILC=0
  if [ $DRY = 1 ]; then say "[dry-run] would switch live to $TAG under the exam lock"; exit 0; fi
  /usr/bin/lockf -t $EXAM_WAIT $ELOCK $0 __live $TAG $SHA; rc=$?
  [ $rc = 75 ] && { say "exam lock busy for ${EXAM_WAIT}s: live NOT switched"; record exam-lock-timeout; }
  exit $rc
fi

run git -C $REPO fetch -q origin --tags || say "WARNING: git fetch failed (GitHub unreachable?); releasing local refs"
SHA=$(git -C $REPO rev-parse --verify "$REF^{commit}") || { say "unknown ref $REF"; exit 1; }
say "candidate $REF = ${SHA:0:10}"
if [ "$(git -C $LIVE rev-parse HEAD 2>/dev/null)" = "$SHA" ] && [ $FAILC = 0 ]; then
  say "live is already on ${SHA:0:10}; nothing to ship"; exit 0
fi

WT=/private/tmp/picky-release-wt
if [ $DRY = 1 ]; then say "[dry-run] suite: $HERE/alltests.sh on a worktree of ${SHA:0:10}"; else
  $HERE/mkwt.sh $WT $SHA >/dev/null || { say "worktree failed"; exit 1; }
  say "model-free suite on $WT (this takes a while)"
  OUT=$(/bin/zsh $WT/tools/release/alltests.sh $WT 2>&1 | tail -1)
  say "$OUT"
  git -C $REPO worktree remove --force $WT
  if [ "$OUT" != "FAILS: none" ]; then  # gate: no NEW failures vs the live commit, HARD ones never
    FAILS=(${=${OUT#FAILS: }}); BAD=(${FAILS:*HARD_TESTS})
    grep -q '^  FAIL .*\[flub ' /tmp/kt-picky-release-wt-test_lane_table.py.log 2>/dev/null && BAD+=test_lane_table.py-flub-row
    BASE=$(git -C $LIVE rev-parse HEAD 2>/dev/null)
    if [ ${#BAD} = 0 ] && [ -n "$BASE" ]; then
      BWT=/private/tmp/picky-release-base
      say "baseline: the same suite on live ${BASE:0:10}"
      $HERE/mkwt.sh $BWT $BASE >/dev/null && BOUT=$(/bin/zsh $BWT/tools/release/alltests.sh $BWT 2>&1 | tail -1)
      git -C $REPO worktree remove --force $BWT 2>/dev/null
      say "baseline $BOUT"
      if [[ $BOUT == "FAILS: "* ]]; then
        BFAILS=(${=${BOUT#FAILS: }}); BAD=(${FAILS:|BFAILS})
      else BAD=(baseline-suite-did-not-run); fi
    elif [ ${#BAD} = 0 ]; then BAD=(no-live-baseline); fi
    if [ ${#BAD} -gt 0 ]; then
      say "BLOCKED by: ${BAD[*]}"
      record suite-failed "\"fails\": \"${OUT#FAILS: }\", \"blocking\": \"${BAD[*]}\""
      exit 1
    fi
    export PICKY_RELEASE_TOLERATED="${FAILS[*]}"
    say "TOLERATED as pre-existing (live ${BASE:0:10} fails them too): ${FAILS[*]}"
  fi
fi

TAG=picky-live-$(date +%Y%m%d-%H%M%S)
run git -C $REPO tag -a $TAG $SHA -m "Picky live release $TAG"
run git -C $REPO push -q origin $TAG || say "WARNING: tag push failed (GitHub?); $TAG stays local, rollback still works"
say "tagged $TAG; waiting for the exam lock (up to ${EXAM_WAIT}s) to switch live"
if [ $DRY = 1 ]; then
  PREV=$(git -C $LIVE rev-parse HEAD 2>/dev/null)
  say "[dry-run] live ${PREV:0:10} -> $TAG"
  switch_live $TAG; deploy_services "$PREV" "$SHA"; healthy
  FAILC=1; canary || { say "[dry-run] canary failed -> rollback to ${PREV:0:10}"; switch_live "$PREV"; deploy_services "$SHA" "$PREV"; FAILC=0; healthy && canary && say "[dry-run] rolled back, health + canary ok"; }
  exit 0
fi
/usr/bin/lockf -t $EXAM_WAIT $ELOCK $0 __live $TAG $SHA $( [ $FAILC = 1 ] && echo --fail-canary ); rc=$?
[ $rc = 75 ] && { say "exam lock busy for ${EXAM_WAIT}s: live NOT switched ($TAG is tagged; rerun to ship it)"; record exam-lock-timeout; }
exit $rc
