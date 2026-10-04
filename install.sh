#!/bin/bash
# Instaluje claude-acc: skrypty, komendę `claude-acc`, automaty w launchd i aplikację w pasku menu.
set -euo pipefail
cd "$(dirname "$0")"

STATE="$HOME/.local/share/claude-acc"
AGENT="$HOME/Library/LaunchAgents/com.filip.claude-acc.plist"
JANITOR="$HOME/Library/LaunchAgents/com.filip.claude-acc.janitor.plist"
DEVGUARD="$HOME/Library/LaunchAgents/com.filip.claude-acc.devguard.plist"

mkdir -p "$STATE" "$HOME/.local/bin" "$HOME/Library/LaunchAgents"
cp accswitch.py janitor.py devguard.py "$STATE/"

# `claude-acc mac ...` i `claude-acc clean` idą do porządków, `guard` do strażnika dev serwerów,
# reszta do kont
cat > "$HOME/.local/bin/claude-acc" <<'EOF'
#!/bin/sh
case "$1" in
  mac) shift; exec /usr/bin/python3 "$HOME/.local/share/claude-acc/janitor.py" "$@" ;;
  clean) shift; exec /usr/bin/python3 "$HOME/.local/share/claude-acc/janitor.py" sweep --force "$@" ;;
  guard) shift; exec /usr/bin/python3 "$HOME/.local/share/claude-acc/devguard.py" "$@" ;;
esac
exec /usr/bin/python3 "$HOME/.local/share/claude-acc/accswitch.py" "$@"
EOF
chmod +x "$HOME/.local/bin/claude-acc"

# automaty: tick kont co 2 minuty, porządki przy logowaniu i co 3 godziny, strażnik dev serwerów cały czas
sed "s|__HOME__|$HOME|g" launchd/com.filip.claude-acc.plist.template > "$AGENT"
sed "s|__HOME__|$HOME|g" launchd/com.filip.claude-acc.janitor.plist.template > "$JANITOR"
sed "s|__HOME__|$HOME|g" launchd/com.filip.claude-acc.devguard.plist.template > "$DEVGUARD"
for plist in "$AGENT" "$JANITOR" "$DEVGUARD"; do
  launchctl bootout "gui/$(id -u)" "$plist" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$plist"
done

# aplikacja w pasku menu
app/build.sh

echo
echo "gotowe. Sprawdź: claude-acc status, claude-acc mac status, claude-acc guard status"
echo "hook dla agentów (drugi dev serwer tej samej aplikacji): README, sekcja Strażnik dev serwerów"
