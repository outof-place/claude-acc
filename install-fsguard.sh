#!/bin/bash
# Strażnik fseventsd (fsguard.py) jako root LaunchDaemon: tylko root widzi pamięć fseventsd
# i może go zrestartować.
#
#   ./install-fsguard.sh              instaluje i uruchamia strażnika (sudo, Touch ID)
#   ./install-fsguard.sh --uninstall  usuwa strażnika
#
# Skrypt trafia do /usr/local/libexec jako root:wheel, więc nikt bez roota go nie podmieni.
# Uruchamia go interpreter należący do roota (rootpy.py), nie /usr/bin/python3: ta zaślepka idzie do
# wybranego Xcode'a, a Xcode z DMG należy do użytkownika, więc jego biblioteka standardowa byłaby
# kodem, który pod rootem może podmienić ktokolwiek na tym koncie.
# Log: /Library/Logs/claude-acc-fsguard.log
set -euo pipefail
cd "$(dirname "$0")"

BIN=/usr/local/libexec/claude-acc-fsguard
PLIST=/Library/LaunchDaemons/com.filip.claude-acc.fsguard.plist

if [ "${1:-}" = "--uninstall" ]; then
  sudo launchctl bootout system "$PLIST" 2>/dev/null || true
  sudo rm -f "$PLIST" "$BIN" /var/db/claude-acc-fsguard.json
  echo "usunięty strażnik fseventsd"
  exit 0
fi

PYTHON="$(/usr/bin/python3 ./rootpy.py)" || { echo "nie instaluję strażnika" >&2; exit 1; }
TMP="$(mktemp -t claude-acc-fsguard-plist)"
sed "s|__PYTHON__|$PYTHON|" launchd/com.filip.claude-acc.fsguard.plist > "$TMP"
sudo install -d -o root -g wheel -m 755 /usr/local/libexec
sudo install -o root -g wheel -m 755 fsguard.py "$BIN"
sudo install -o root -g wheel -m 644 "$TMP" "$PLIST"
rm -f "$TMP"
sudo launchctl bootout system "$PLIST" 2>/dev/null || true
sudo launchctl bootstrap system "$PLIST"
echo "uruchamia go: $PYTHON -I $BIN"

echo "gotowe: strażnik sprawdza fseventsd co minutę. Log: /Library/Logs/claude-acc-fsguard.log"
