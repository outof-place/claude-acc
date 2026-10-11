#!/bin/bash
# Swaps claude-acc's devguard, accswitch-tick and janitor launchd jobs for one acc-cored job, and back.
#
#   scripts/acc-cored-switch.sh status
#   scripts/acc-cored-switch.sh on  [--apply] [--binary PATH]   what it would do; --apply does it
#   scripts/acc-cored-switch.sh off [--apply]                   the undo: acc-cored out, the jobs back
#
# Check-only unless --apply. Standalone installs only (com.filip.claude-acc.*); Pod registers its own
# agents. `on` installs the binary as $STATE/acc-cored (temp file, then rename: overwriting a running
# binary gets its next launch killed), writes com.filip.claude-acc.cored.plist, boots out the three
# jobs (their plists stay where setup.sh wrote them) and bootstraps acc-cored. `off` boots acc-cored
# out and bootstraps the recorded jobs from those plists again. A later setup.sh brings the three jobs
# back beside acc-cored: both guards then share devguard.lock, the ticks the switcher's lock, the
# sweeps janitor.lock, so they take turns rather than act twice; run `off` and `on` again after it.
set -u
cd "$(dirname "$0")/.." || exit 1
SRC=$(pwd)
STATE="$HOME/.local/share/claude-acc"
AGENTS="$HOME/Library/LaunchAgents"
DOMAIN="gui/$(id -u)"
LABEL=com.filip.claude-acc.cored
REPLACED="com.filip.claude-acc.devguard com.filip.claude-acc com.filip.claude-acc.janitor"
RECORD="$STATE/acc-cored-switch.json"
LAUNCHCTL=${ACC_CORED_LAUNCHCTL:-launchctl}
# how long the tick job's bootout waits for the switcher's lock (a tick holds it while it runs)
LOCK_WAIT=${ACC_CORED_LOCK_WAIT:-150}

cmd=${1:-status}
shift || true
APPLY=""
BINARY="$SRC/app/.build/release/acc-cored"
while [ $# -gt 0 ]; do
  case "$1" in
    --apply) APPLY=1; shift ;;
    --binary) BINARY="$2"; shift 2 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

loaded() { "$LAUNCHCTL" print "$DOMAIN/$1" >/dev/null 2>&1; }
step() {
  if [ -n "$APPLY" ]; then
    echo "+ $*"
    "$@"
  else
    echo "would: $*"
  fi
}

case "$cmd" in
  status)
    for job in $LABEL $REPLACED; do
      if loaded "$job"; then echo "loaded      $job"; else echo "not loaded  $job"; fi
    done
    [ -x "$STATE/acc-cored" ] && echo "binary      $STATE/acc-cored" || echo "binary      none"
    [ -f "$RECORD" ] && echo "switched    $(cat "$RECORD")" || echo "switched    no"
    ;;
  on)
    [ -x "$BINARY" ] || { echo "no acc-cored binary at $BINARY (cd app && swift build -c release --product acc-cored)" >&2; exit 1; }
    [ -f "$RECORD" ] && { echo "already switched ($RECORD); run off first" >&2; exit 1; }
    stopped=""
    for job in $REPLACED; do loaded "$job" && stopped="$stopped $job"; done
    step cp "$BINARY" "$STATE/acc-cored.new" || exit 1
    step mv -f "$STATE/acc-cored.new" "$STATE/acc-cored" || exit 1
    # the tick job first, under the switcher's own lock: no tick is between a token refresh and its
    # Keychain write when launchd stops the job (-k keeps the lock file the ticks use). If the lock
    # stays held (a hung `claude-acc status`, say), nothing else changes: a record saying "replaced"
    # with launchd's tick still loaded beside acc-cored would tick twice
    case " $stopped " in
      *" com.filip.claude-acc "*)
        if ! step /usr/bin/lockf -k -t "$LOCK_WAIT" "$STATE/lock" "$LAUNCHCTL" bootout "$DOMAIN/com.filip.claude-acc"; then
          echo "the accswitch tick job didn't go (the switcher's lock held over $LOCK_WAIT s?): nothing else changed" >&2
          exit 1
        fi
        ;;
    esac
    if [ -n "$APPLY" ]; then
      sed "s#__HOME__#$HOME#g" "$SRC/launchd/$LABEL.plist.template" > "$AGENTS/$LABEL.plist"
      echo "+ wrote $AGENTS/$LABEL.plist"
      /usr/bin/python3 -I -c 'import json, sys, time; json.dump({"at": time.time(), "replaced": sys.argv[2:]}, open(sys.argv[1], "w"))' \
        "$RECORD" $stopped
    else
      echo "would: write $AGENTS/$LABEL.plist and $RECORD (replaced:$stopped)"
    fi
    for job in $stopped; do
      [ "$job" = com.filip.claude-acc ] || step "$LAUNCHCTL" bootout "$DOMAIN/$job"
    done
    if ! step "$LAUNCHCTL" bootstrap "$DOMAIN" "$AGENTS/$LABEL.plist"; then
      echo "acc-cored didn't start: $0 off --apply brings the jobs back" >&2
      exit 1
    fi
    [ -n "$APPLY" ] || echo "(check only: add --apply)"
    ;;
  off)
    if [ -f "$RECORD" ]; then
      jobs=$(/usr/bin/python3 -I -c 'import json, sys; print(" ".join(json.load(open(sys.argv[1]))["replaced"]))' "$RECORD")
    else
      jobs=$REPLACED
    fi
    loaded $LABEL && step "$LAUNCHCTL" bootout "$DOMAIN/$LABEL"
    for job in $jobs; do
      if [ -f "$AGENTS/$job.plist" ]; then
        loaded "$job" || step "$LAUNCHCTL" bootstrap "$DOMAIN" "$AGENTS/$job.plist"
      else
        echo "missing $AGENTS/$job.plist: run setup.sh to write it again" >&2
      fi
    done
    step rm -f "$AGENTS/$LABEL.plist" "$RECORD"
    [ -n "$APPLY" ] || echo "(check only: add --apply)"
    ;;
  *) sed -n '2,8p' "$0"; exit 2 ;;
esac
