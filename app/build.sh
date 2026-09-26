#!/bin/bash
# Buduje Claude Acc.app, instaluje w ~/Applications i uruchamia.
set -euo pipefail
cd "$(dirname "$0")"

swift build -c release
BIN="$(swift build -c release --show-bin-path)/ClaudeAcc"
APP="$HOME/Applications/Claude Acc.app"

pkill -x ClaudeAcc || true
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS"
cp "$BIN" "$APP/Contents/MacOS/ClaudeAcc"
cp Info.plist "$APP/Contents/Info.plist"
codesign --force --sign - "$APP"
open "$APP"
echo "zainstalowano: $APP"
