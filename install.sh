#!/bin/bash
# Instaluje claude-acc: skrypt, komendę `claude-acc`, automat w launchd i aplikację w pasku menu.
set -euo pipefail
cd "$(dirname "$0")"

STATE="$HOME/.local/share/claude-acc"
AGENT="$HOME/Library/LaunchAgents/com.filip.claude-acc.plist"

mkdir -p "$STATE" "$HOME/.local/bin" "$HOME/Library/LaunchAgents"
cp accswitch.py "$STATE/accswitch.py"

cat > "$HOME/.local/bin/claude-acc" <<'EOF'
#!/bin/sh
exec /usr/bin/python3 "$HOME/.local/share/claude-acc/accswitch.py" "$@"
EOF
chmod +x "$HOME/.local/bin/claude-acc"

# automat: tick co 2 minuty
sed "s|__HOME__|$HOME|g" launchd/com.filip.claude-acc.plist.template > "$AGENT"
launchctl bootout "gui/$(id -u)" "$AGENT" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$AGENT"

# aplikacja w pasku menu
app/build.sh

echo
echo "gotowe. Sprawdź: claude-acc status"
