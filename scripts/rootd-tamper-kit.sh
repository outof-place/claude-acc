#!/bin/bash
# Builds a throwaway kit that answers one question pod-rootd's design depends on (docs/pod-rootd.md,
# "A tampered helper"): does launchd run an SMAppService daemon as root after someone with write
# access to the app bundle swapped its BundleProgram? Two tiny apps register one daemon each, one
# with a SpawnConstraint in its plist and one without; the daemon only appends a line to
# /var/tmp/pod-rootd-tamper.log. This script only builds and signs in DIR. Registering, approving and
# the kickstarts are the manual steps it prints, best on a scratch Mac or VM.
#
#   scripts/rootd-tamper-kit.sh DIR "<signing identity of team 75Y2KR6P5W>"
set -euo pipefail
DIR="${1:?usage: rootd-tamper-kit.sh DIR IDENTITY}"
IDENTITY="${2:?usage: rootd-tamper-kit.sh DIR IDENTITY}"
TEAM=75Y2KR6P5W
mkdir -p "$DIR"
DIR="$(cd "$DIR" && pwd)"
SRC="$DIR/src"
mkdir -p "$SRC"

cat > "$SRC/probe.c" <<'EOF'
#include <stdio.h>
#include <time.h>
#include <unistd.h>
int main(int argc, char **argv) {
    FILE *log = fopen("/var/tmp/pod-rootd-tamper.log", "a");
    if (!log) return 1;
    fprintf(log, "%ld genuine probe ran uid=%d %s\n", (long)time(NULL), getuid(), argc > 1 ? argv[1] : "");
    fclose(log);
    return 0;
}
EOF
sed 's/genuine probe ran/REPLACED binary ran/' "$SRC/probe.c" > "$SRC/evil.c"
cat > "$SRC/registrar.swift" <<'EOF'
import Foundation
import ServiceManagement

// the app's own executable: SMAppService finds the plist in this app's Contents/Library/LaunchDaemons
let plist = Bundle.main.bundleIdentifier! + ".probe.plist"
let service = SMAppService.daemon(plistName: plist)
do {
    switch CommandLine.arguments.dropFirst().first {
    case "register": try service.register()
    case "unregister": try service.unregister()
    default: break
    }
} catch {
    print("\(plist): \(error)")
}
let names: [SMAppService.Status: String] = [.notRegistered: "notRegistered", .enabled: "enabled",
                                            .requiresApproval: "requiresApproval", .notFound: "notFound"]
print("\(plist): \(names[service.status] ?? "unknown")")
EOF

clang -O2 -o "$DIR/probe" "$SRC/probe.c"
clang -O2 -o "$DIR/evil" "$SRC/evil.c"  # linker-signed ad hoc, like anything an attacker builds
swiftc -O -o "$DIR/registrar" "$SRC/registrar.swift"

for variant in constrained plain; do
  APP="$DIR/$variant.app"
  ID="codes.pod.tamper.$variant"
  LABEL="$ID.probe"
  rm -rf "$APP"
  mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources" "$APP/Contents/Library/LaunchDaemons"
  cp "$DIR/registrar" "$APP/Contents/MacOS/registrar"
  cp "$DIR/probe" "$APP/Contents/Resources/probe"
  /usr/libexec/PlistBuddy -c "Add :CFBundleIdentifier string $ID" -c "Add :CFBundleExecutable string registrar" \
    -c "Add :CFBundlePackageType string APPL" -c "Add :CFBundleName string tamper-$variant" "$APP/Contents/Info.plist" >/dev/null
  P="$APP/Contents/Library/LaunchDaemons/$LABEL.plist"
  /usr/libexec/PlistBuddy -c "Add :Label string $LABEL" -c "Add :BundleProgram string Contents/Resources/probe" \
    -c "Add :ProgramArguments array" -c "Add :ProgramArguments:0 string probe" -c "Add :ProgramArguments:1 string $variant" \
    -c "Add :RunAtLoad bool true" "$P" >/dev/null
  if [ "$variant" = constrained ]; then
    /usr/libexec/PlistBuddy -c "Add :SpawnConstraint dict" -c "Add :SpawnConstraint:team-identifier string $TEAM" \
      -c "Add :SpawnConstraint:signing-identifier string codes.pod.tamper.probe" "$P" >/dev/null
  fi
  codesign --force --sign "$IDENTITY" --options runtime --timestamp=none --identifier codes.pod.tamper.probe "$APP/Contents/Resources/probe"
  codesign --force --sign "$IDENTITY" --options runtime --timestamp=none "$APP"
  codesign --verify --deep --strict "$APP"
done

cat <<EOF
kit in $DIR (nothing registered). Manual steps, best on a scratch Mac or VM:
  K=$DIR
  for v in constrained plain; do "\$K/\$v.app/Contents/MacOS/registrar" register; done   # requiresApproval
  # approve both: System Settings > General > Login Items & Extensions > Allow in the Background
  for v in constrained plain; do "\$K/\$v.app/Contents/MacOS/registrar"; done            # enabled
  for v in constrained plain; do sudo launchctl kickstart -k system/codes.pod.tamper.\$v.probe; done
  cat /var/tmp/pod-rootd-tamper.log      # two "genuine probe ran uid=0" lines
  # the attack: swap the daemon binary as the user, no sudo
  for v in constrained plain; do cp "\$K/evil" "\$K/\$v.app/Contents/Resources/probe"; done
  for v in constrained plain; do sudo launchctl kickstart -k system/codes.pod.tamper.\$v.probe; done
  cat /var/tmp/pod-rootd-tamper.log      # a "REPLACED binary ran uid=0" line: that variant runs tampered code as root
  log show --last 10m --predicate 'process == "launchd" OR subsystem == "com.apple.xpc.launchd" OR subsystem == "com.apple.backgroundtaskmanagement"' | grep -i -E "tamper|constraint|codesign|signature" | tail -30
  # the plist is in the bundle too: drop the constraint, restart, look again
  /usr/libexec/PlistBuddy -c "Delete :SpawnConstraint" "\$K/constrained.app/Contents/Library/LaunchDaemons/codes.pod.tamper.constrained.probe.plist"
  # reboot, then: cat /var/tmp/pod-rootd-tamper.log
  # clean up
  for v in constrained plain; do "\$K/\$v.app/Contents/MacOS/registrar" unregister; done; sudo rm -f /var/tmp/pod-rootd-tamper.log
EOF
