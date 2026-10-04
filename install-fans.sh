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

sudo install -d -o root -g wheel -m 755 /usr/local/libexec
sudo install -o root -g wheel -m 755 "$BUILT" "$BIN"
sed "s|__HOME__|$HOME|g" launchd/com.filip.claude-acc.fans.plist.template | sudo tee "$PLIST" >/dev/null
sudo chown root:wheel "$PLIST"
sudo chmod 644 "$PLIST"
sudo launchctl bootout system "$PLIST" 2>/dev/null || true
sudo launchctl bootstrap system "$PLIST"

echo "gotowe: tryb wybierasz w panelu (karta Fans). Odczyt: $BIN read"
