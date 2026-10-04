#!/bin/bash
# Instaluje claude-acc: skrypty, komendę `claude-acc`, automaty w launchd i aplikację w pasku menu.
set -euo pipefail
cd "$(dirname "$0")"

STATE="$HOME/.local/share/claude-acc"
AGENT="$HOME/Library/LaunchAgents/com.filip.claude-acc.plist"
JANITOR="$HOME/Library/LaunchAgents/com.filip.claude-acc.janitor.plist"

mkdir -p "$STATE" "$HOME/.local/bin" "$HOME/Library/LaunchAgents"
cp accswitch.py janitor.py "$STATE/"

# `claude-acc mac ...` i `claude-acc clean` idą do porządków, reszta do kont
cat > "$HOME/.local/bin/claude-acc" <<'EOF'
#!/bin/sh
case "$1" in
  mac) shift; exec /usr/bin/python3 "$HOME/.local/share/claude-acc/janitor.py" "$@" ;;
  clean) shift; exec /usr/bin/python3 "$HOME/.local/share/claude-acc/janitor.py" sweep --force "$@" ;;
esac
exec /usr/bin/python3 "$HOME/.local/share/claude-acc/accswitch.py" "$@"
EOF
chmod +x "$HOME/.local/bin/claude-acc"

# automaty: tick kont co 2 minuty, porządki przy logowaniu i co 3 godziny
sed "s|__HOME__|$HOME|g" launchd/com.filip.claude-acc.plist.template > "$AGENT"
sed "s|__HOME__|$HOME|g" launchd/com.filip.claude-acc.janitor.plist.template > "$JANITOR"
for plist in "$AGENT" "$JANITOR"; do
  launchctl bootout "gui/$(id -u)" "$plist" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$plist"
done

# aplikacja w pasku menu
app/build.sh

echo
echo "gotowe. Sprawdź: claude-acc status, claude-acc mac status"
