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
#   sign-app.sh "<ścieżka do Claude Acc.app>"
set -euo pipefail
APP="$1"
APP_ID="com.filip.claude-acc.menubar"
id="${CLAUDE_ACC_SIGN_ID:-}"
[ -z "$id" ] && id="$(security find-identity -v -p codesigning 2>/dev/null | awk '/^ *[0-9]+\)/ { print $2; exit }')"
if [ -n "$id" ] && [ "$id" != "-" ]; then
  codesign --force --sign "$id" "$APP"
else
  codesign --force --sign - --identifier "$APP_ID" -r="designated => identifier \"$APP_ID\"" "$APP"
fi
