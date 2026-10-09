#!/bin/bash
# Sterowanie wiatrakami: fanctl jako root LaunchDaemon (zapis do SMC wymaga roota).
#
#   ./install-fans.sh              buduje fanctl i instaluje demona (sudo, Touch ID)
#   ./install-fans.sh --binary F   instaluje gotowy fanctl (Homebrew)
#   ./install-fans.sh --uninstall  oddaje wiatraki macOS i usuwa demona
#
# Binarka trafia do /usr/local/libexec jako root:wheel, więc nikt bez roota jej nie podmieni.
# Demon czyta tryb z ~/.local/share/claude-acc/fans.json (pisze go panel) i niczego nie
# zmienia, dopóki trybu nikt nie wybrał.
set -euo pipefail
cd "$(dirname "$0")"

BIN=/usr/local/libexec/claude-acc-fanctl
PLIST=/Library/LaunchDaemons/com.filip.claude-acc.fans.plist

if [ "${1:-}" = "--uninstall" ]; then
  if [ -x "$BIN" ]; then sudo "$BIN" set auto || true; fi
  sudo launchctl bootout system "$PLIST" 2>/dev/null || true
  sudo rm -f "$PLIST" "$BIN"
  echo "usunięte; wiatraki wróciły do macOS"
  exit 0
fi

if [ "${1:-}" = "--binary" ]; then
  BUILT="$2"  # gotowy fanctl, np. z Homebrew
else
  (cd app && swift build -c release --product fanctl)
  BUILT="$(cd app && swift build -c release --show-bin-path)/fanctl"
fi
mkdir -p "$HOME/.local/share/claude-acc"
# Zamknięta pokrywa (Lid) słucha aplikacji po podpisie: zespół 75Y2KR6P5W demon zna sam, aplikację
# podpisaną innym certyfikatem (sign-app.sh bierze pierwszy z Pęku kluczy) dopisujemy przez --team
TEAM="$(codesign -dv "$HOME/Applications/Claude Acc.app" 2>&1 | sed -n 's/^TeamIdentifier=\([A-Z0-9]*\)$/\1/p' || true)"
TEAM_ARGS=""
if [ -n "$TEAM" ] && [ "$TEAM" != 75Y2KR6P5W ]; then
  TEAM_ARGS="<string>--team</string><string>$TEAM</string>"
fi

sudo install -d -o root -g wheel -m 755 /usr/local/libexec
sudo install -o root -g wheel -m 755 "$BUILT" "$BIN"
sed -e "s|__HOME__|$HOME|g" -e "s|__TEAM__|$TEAM_ARGS|" launchd/com.filip.claude-acc.fans.plist.template \
  | sudo tee "$PLIST" >/dev/null
sudo chown root:wheel "$PLIST"
sudo chmod 644 "$PLIST"
sudo launchctl bootout system "$PLIST" 2>/dev/null || true
sudo launchctl bootstrap system "$PLIST"

echo "gotowe: tryb wybierasz w panelu (karta Fans). Odczyt: $BIN read"
