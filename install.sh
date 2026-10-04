#!/bin/bash
# Instaluje claude-acc ze źródeł: buduje aplikację i fanctl, potem setup.sh robi resztę
# (skrypty, komenda `claude-acc`, automaty w launchd, aplikacja w ~/Applications).
set -euo pipefail
cd "$(dirname "$0")"

(cd app && swift build -c release)
BIN="$(cd app && swift build -c release --show-bin-path)"

# pakiet aplikacji: binarka, Info.plist i podpis ad hoc
BUNDLE="$(mktemp -d)/Claude Acc.app"
mkdir -p "$BUNDLE/Contents/MacOS"
cp "$BIN/ClaudeAcc" "$BUNDLE/Contents/MacOS/ClaudeAcc"
cp app/Info.plist "$BUNDLE/Contents/Info.plist"
codesign --force --sign - "$BUNDLE"

./setup.sh --app "$BUNDLE" --fanctl "$BIN/fanctl"
rm -rf "$(dirname "$BUNDLE")"
