#!/bin/bash
# Swaps claude-acc's devguard, accswitch-tick, janitor and perf keep launchd jobs for one acc-cored
# job, and back.
#
#   scripts/acc-cored-switch.sh status
#   scripts/acc-cored-switch.sh on  [--apply] [--binary PATH]   what it would do; --apply does it
#   scripts/acc-cored-switch.sh off [--apply]                   the undo: acc-cored out, the jobs back
#
# Check-only unless --apply. Standalone installs only (com.filip.claude-acc.*): where Pod owns
# claude-acc (owner.json), or none of the jobs is loaded, `on` refuses. `on` installs the binary as
# $STATE/acc-cored (temp file, then rename: overwriting a running binary gets its next launch killed),
# writes com.filip.claude-acc.cored.plist, boots out the jobs (their plists stay where setup.sh wrote
# them) and bootstraps acc-cored. `off` boots acc-cored out and bootstraps the recorded jobs from
# those plists again. A later setup.sh brings the jobs back beside acc-cored: both guards then share
# devguard.lock, the ticks the switcher's lock, the sweeps janitor.lock, the keeps perf.lock, so they
# take turns rather than act twice; run `off` and `on` again after it.
# `setup.sh --uninstall` and `--pod-agents` take acc-cored down too (its plist and the record).
set -u
cd "$(dirname "$0")/.." || exit 1
SRC=$(pwd)
STATE="$HOME/.local/share/claude-acc"
AGENTS="$HOME/Library/LaunchAgents"
DOMAIN="gui/$(id -u)"
LABEL=com.filip.claude-acc.cored
REPLACED="com.filip.claude-acc.devguard com.filip.claude-acc com.filip.claude-acc.janitor com.filip.claude-acc.perf"
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
    # Pod runs its own agents where it owns claude-acc: acc-cored beside them would guard, tick and
    # sweep twice (owner.py says so, exit 3, as it does for setup.sh); first, so a missing binary
    # doesn't hide it
    /usr/bin/python3 -B "$SRC/owner.py" check || exit $?
    [ -x "$BINARY" ] || { echo "no acc-cored binary at $BINARY (cd app && swift build -c release --product acc-cored)" >&2; exit 1; }
    [ -f "$RECORD" ] && { echo "already switched ($RECORD); run off first" >&2; exit 1; }
    stopped=""
    for job in $REPLACED; do loaded "$job" && stopped="$stopped $job"; done
    if [ -z "$stopped" ]; then
      echo "none of $REPLACED is loaded: nothing for acc-cored to replace (setup.sh not run, or another installer's agents)" >&2
      exit 1
    fi
    step cp "$BINARY" "$STATE/acc-cored.new" || exit 1
    step mv -f "$STATE/acc-cored.new" "$STATE/acc-cored" || exit 1
    # the record first: a Ctrl-C after the tick's bootout still leaves `off` what to bring back
    if [ -n "$APPLY" ]; then
      sed "s#__HOME__#$HOME#g" "$SRC/launchd/$LABEL.plist.template" > "$AGENTS/$LABEL.plist" || exit 1
      echo "+ wrote $AGENTS/$LABEL.plist"
      /usr/bin/python3 -I -c 'import json, sys, time; json.dump({"at": time.time(), "replaced": sys.argv[2:]}, open(sys.argv[1], "w"))' \
        "$RECORD" $stopped || exit 1
    else
      echo "would: write $AGENTS/$LABEL.plist and $RECORD (replaced:$stopped)"
    fi
    # the tick job first, under the switcher's own lock: no tick is between a token refresh and its
    # Keychain write when launchd stops the job (-k keeps the lock file the ticks use). If the lock
    # stays held (a hung `claude-acc status`, say), nothing else changes: a record saying "replaced"
    # with launchd's tick still loaded beside acc-cored would tick twice. Whether it went is what
    # launchd says afterwards, not bootout's exit code.
    case " $stopped " in
      *" com.filip.claude-acc "*)
        if ! step /usr/bin/lockf -k -t "$LOCK_WAIT" "$STATE/lock" "$LAUNCHCTL" bootout "$DOMAIN/com.filip.claude-acc" &&
          [ -n "$APPLY" ] && loaded com.filip.claude-acc; then
          rm -f "$AGENTS/$LABEL.plist" "$RECORD"
          echo "the accswitch tick job didn't go (the switcher's lock held over $LOCK_WAIT s?): nothing else changed, but the new binary is in place" >&2
          exit 1
        fi
        ;;
    esac
    for job in $stopped; do
      [ "$job" = com.filip.claude-acc ] || step "$LAUNCHCTL" bootout "$DOMAIN/$job"
    done
    # judged by launchd too: a devguard still loaded beside acc-cored holds devguard.lock, so
    # acc-cored's guard exits and KeepAlive restarts it every 10 s, with the tick already gone. Then
    # what went comes back, and acc-cored doesn't start; if that fails too, the record stays for `off`
    if [ -n "$APPLY" ]; then
      stuck=""
      for job in $stopped; do loaded "$job" && stuck="$stuck $job"; done
      if [ -n "$stuck" ]; then
        back=1
        for job in $stopped; do
          loaded "$job" || step "$LAUNCHCTL" bootstrap "$DOMAIN" "$AGENTS/$job.plist"
          loaded "$job" || back=0
        done
        if [ "$back" -eq 1 ]; then
          rm -f "$AGENTS/$LABEL.plist" "$RECORD"
          echo "$stuck didn't go: the jobs are back as they were and acc-cored didn't start (the new binary is in place)" >&2
        else
          echo "$stuck didn't go, and not every job came back: $0 off --apply brings them back" >&2
        fi
        exit 1
      fi
    fi
    # whether it started is what launchd says afterwards, as for the tick
    step "$LAUNCHCTL" bootstrap "$DOMAIN" "$AGENTS/$LABEL.plist"
    if [ -n "$APPLY" ] && ! loaded $LABEL; then
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
    # every step counts, judged by what launchd says afterwards (as in `on`), not by an exit code: on
    # any failure the record and the plist stay, so `off` can run again
    failed=0
    if loaded $LABEL; then
      step "$LAUNCHCTL" bootout "$DOMAIN/$LABEL"
      [ -z "$APPLY" ] || ! loaded $LABEL || failed=1
    fi
    for job in $jobs; do
      if [ -f "$AGENTS/$job.plist" ]; then
        if ! loaded "$job"; then
          step "$LAUNCHCTL" bootstrap "$DOMAIN" "$AGENTS/$job.plist"
          [ -z "$APPLY" ] || loaded "$job" || failed=1
        fi
      else
        echo "missing $AGENTS/$job.plist: run setup.sh to write it again" >&2
        failed=1
      fi
    done
    if [ "$failed" -ne 0 ] && [ -n "$APPLY" ]; then
      echo "off didn't finish (see above): $RECORD and $LABEL.plist stay; run off --apply again" >&2
      exit 1
    fi
    step rm -f "$AGENTS/$LABEL.plist" "$RECORD"
    [ -n "$APPLY" ] || echo "(check only: add --apply)"
    ;;
  *) sed -n '2,8p' "$0"; exit 2 ;;
esac
