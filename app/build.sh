#!/bin/bash
# Buduje Claude Acc.app, instaluje w ~/Applications i uruchamia.
set -euo pipefail
cd "$(dirname "$0")"

swift build -c release
BINDIR="$(swift build -c release --show-bin-path)"
BIN="$BINDIR/ClaudeAcc"
APP="$HOME/Applications/Claude Acc.app"

# pomocnik bramy pulpitu ze stabilnym designated requirement (patrz install.sh)
DESKTOP_ID="com.filip.claude-acc.desktop"
codesign --force --sign - --identifier "$DESKTOP_ID" \
  -r="designated => identifier \"$DESKTOP_ID\"" "$BINDIR/claude-acc-desktop"
echo "pomocnik pulpitu: $BINDIR/claude-acc-desktop (podpisany $DESKTOP_ID)"

pkill -x ClaudeAcc || true
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS"
cp "$BIN" "$APP/Contents/MacOS/ClaudeAcc"
cp Info.plist "$APP/Contents/Info.plist"
../sign-app.sh "$APP"
open "$APP"
echo "zainstalowano: $APP"
