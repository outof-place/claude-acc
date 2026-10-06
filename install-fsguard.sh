#!/bin/bash
# Strażnik fseventsd (fsguard.py) jako root LaunchDaemon: tylko root widzi pamięć fseventsd
# i może go zrestartować.
#
#   ./install-fsguard.sh              instaluje i uruchamia strażnika (sudo, Touch ID)
#   ./install-fsguard.sh --uninstall  usuwa strażnika
#
# Skrypt trafia do /usr/local/libexec jako root:wheel, więc nikt bez roota go nie podmieni.
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

sudo install -d -o root -g wheel -m 755 /usr/local/libexec
sudo install -o root -g wheel -m 755 fsguard.py "$BIN"
sudo install -o root -g wheel -m 644 launchd/com.filip.claude-acc.fsguard.plist "$PLIST"
sudo launchctl bootout system "$PLIST" 2>/dev/null || true
sudo launchctl bootstrap system "$PLIST"

echo "gotowe: strażnik sprawdza fseventsd co minutę. Log: /Library/Logs/claude-acc-fsguard.log"
