#!/bin/bash
# Instaluje claude-acc ze źródeł: buduje aplikację i fanctl, potem setup.sh robi resztę
# (skrypty, komenda `claude-acc`, automaty w launchd, aplikacja w ~/Applications).
set -euo pipefail
cd "$(dirname "$0")"

(cd app && swift build -c release)
BIN="$(cd app && swift build -c release --show-bin-path)"

# pakiet aplikacji: binarka, Info.plist i podpis, który trzyma zgody macOS przez przebudowy
BUNDLE="$(mktemp -d)/Claude Acc.app"
mkdir -p "$BUNDLE/Contents/MacOS"
cp "$BIN/ClaudeAcc" "$BUNDLE/Contents/MacOS/ClaudeAcc"
cp app/Info.plist "$BUNDLE/Contents/Info.plist"
./sign-app.sh "$BUNDLE"

# pomocnik bramy pulpitu: podpis ad hoc, ale ze STABILNYM designated requirement po identyfikatorze,
# żeby zgoda TCC (Dostępność, Nagrywanie ekranu) przetrwała przebudowy mimo zmiany cdhash
DESKTOP_ID="com.filip.claude-acc.desktop"
codesign --force --sign - --identifier "$DESKTOP_ID" \
  -r="designated => identifier \"$DESKTOP_ID\"" "$BIN/claude-acc-desktop"

./setup.sh --app "$BUNDLE" --fanctl "$BIN/fanctl" --hook "$BIN/claude-acc-hook" --desktop "$BIN/claude-acc-desktop"
rm -rf "$(dirname "$BUNDLE")"
