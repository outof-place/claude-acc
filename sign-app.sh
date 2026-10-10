#!/bin/bash
# Podpisuje Claude Acc.app tak, żeby zgody macOS dla dyktowania (Mikrofon, Dostępność, Monitorowanie
# wejścia) przetrwały kolejne buildy. Zwykły podpis ad hoc zmienia się z każdym buildem i macOS po
# cichu je gubi (przełącznik w Ustawieniach dalej świeci, a AXIsProcessTrusted zwraca false).
#
#   $CLAUDE_ACC_SIGN_ID albo pierwszy ważny certyfikat do podpisywania kodu z Pęku kluczy (np. Apple
#   Development): designated requirement wiąże zgody z tym certyfikatem.
#   Bez certyfikatu: ad hoc ze stałym designated requirement po identyfikatorze, jak pomocnik pulpitu.
#   Słabsze: każdy program podpisany ad hoc z tym identyfikatorem dostałby te same zgody.
#
# Zawsze z hardened runtime: bez niego każdy proces tego konta wstrzyknie kod (DYLD_INSERT_LIBRARIES,
# niepodpisana biblioteka) w aplikację ze zgodami Mikrofonu, Dostępności i Monitorowania wejścia.
# Mikrofon pod hardened runtime wymaga uprawnienia audio-input; designated requirement się nie
# zmienia, więc zgody zostają.
#
#   sign-app.sh "<ścieżka do Claude Acc.app>"
set -euo pipefail
APP="$1"
APP_ID="com.filip.claude-acc.menubar"
ENTITLEMENTS="$(mktemp -t claude-acc-entitlements)"
trap 'rm -f "$ENTITLEMENTS"' EXIT
cat > "$ENTITLEMENTS" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>com.apple.security.device.audio-input</key><true/>
</dict>
</plist>
PLIST
id="${CLAUDE_ACC_SIGN_ID:-}"
[ -z "$id" ] && id="$(security find-identity -v -p codesigning 2>/dev/null | awk '/^ *[0-9]+\)/ { print $2; exit }')"
if [ -n "$id" ] && [ "$id" != "-" ]; then
  codesign --force --sign "$id" --options runtime --entitlements "$ENTITLEMENTS" "$APP"
else
  codesign --force --sign - --options runtime --entitlements "$ENTITLEMENTS" --identifier "$APP_ID" \
    -r="designated => identifier \"$APP_ID\"" "$APP"
fi
