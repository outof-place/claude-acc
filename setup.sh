#!/bin/bash
# Instaluje claude-acc na koncie użytkownika z gotowych plików: skrypty, komenda `claude-acc`,
# automaty w launchd i aplikacja w pasku menu.
#
#   setup.sh --app "<ścieżka do Claude Acc.app>" [--fanctl <ścieżka do fanctl>]
#   setup.sh --uninstall   zdejmuje automaty, aplikację i komendę; stan i konfiguracja zostają
#
# Woła go install.sh po zbudowaniu ze źródeł i `claude-acc-setup` z Homebrew, które podaje
# swoją zbudowaną aplikację. Wiatraki (root) to osobny krok: install-fans.sh.
set -euo pipefail
SRC="$(cd "$(dirname "$0")" && pwd)"

STATE="$HOME/.local/share/claude-acc"
AGENTS="$HOME/Library/LaunchAgents"
JOBS="com.filip.claude-acc com.filip.claude-acc.janitor com.filip.claude-acc.devguard com.filip.claude-acc.perf com.filip.claude-acc.updates"

APP_SRC=""
FANCTL=""
while [ $# -gt 0 ]; do
  case "$1" in
    --app) APP_SRC="$2"; shift 2 ;;
    --fanctl) FANCTL="$2"; shift 2 ;;
    --uninstall)
      for job in $JOBS; do
        launchctl bootout "gui/$(id -u)" "$AGENTS/$job.plist" 2>/dev/null || true
        rm -f "$AGENTS/$job.plist"
      done
      pkill -x ClaudeAcc 2>/dev/null || true
      rm -rf "$HOME/Applications/Claude Acc.app" "$HOME/.local/bin/claude-acc"
      echo "usunięte: automaty, aplikacja i komenda claude-acc. Stan i konfiguracja zostają w $STATE"
      echo "wiatraki (root) zdejmuje osobno: install-fans.sh --uninstall; hook dla agentów usuń z ~/.claude/settings.json"
      exit 0 ;;
    *) echo "nieznana opcja: $1" >&2; exit 2 ;;
  esac
done
[ -d "$APP_SRC" ] || { echo "brak aplikacji: --app \"<Claude Acc.app>\"" >&2; exit 2; }

mkdir -p "$STATE" "$HOME/.local/bin" "$AGENTS" "$HOME/Applications"
cp "$SRC/accswitch.py" "$SRC/janitor.py" "$SRC/devguard.py" "$SRC/perf.py" "$SRC/sched.py" "$SRC/updates.py" "$STATE/"
# hooki Ultra (szybki npx dla hooków formatowania) leżą obok perf.py
rm -rf "$STATE/hooks.new" && cp -R "$SRC/hooks" "$STATE/hooks.new" && rm -rf "$STATE/hooks" && mv "$STATE/hooks.new" "$STATE/hooks"
[ -n "$FANCTL" ] && cp "$FANCTL" "$STATE/fanctl"
# skąd instalowano: `claude-acc fans install` bierze stamtąd install-fans.sh
echo "$SRC" > "$STATE/source"

# jedna komenda na wszystko: konta, porządki (mac, clean), strażnik (guard), wydajność (perf,
# perf-root), wiatraki (fans), aktualizacje (update, updates),
# a `claude-acc uninstall` zdejmuje to, co postawił ten skrypt
cat > "$HOME/.local/bin/claude-acc" <<'EOF'
#!/bin/sh
STATE="$HOME/.local/share/claude-acc"
case "$1" in
  mac) shift; exec /usr/bin/python3 "$STATE/janitor.py" "$@" ;;
  clean) shift; exec /usr/bin/python3 "$STATE/janitor.py" sweep --force "$@" ;;
  guard) shift; exec /usr/bin/python3 "$STATE/devguard.py" "$@" ;;
  perf) shift; exec /usr/bin/python3 "$STATE/perf.py" "$@" ;;
  sched) shift; exec /usr/bin/python3 "$STATE/sched.py" "$@" ;;
  update) shift; exec /usr/bin/python3 "$STATE/updates.py" run --force "$@" ;;
  updates) shift; exec /usr/bin/python3 "$STATE/updates.py" "$@" ;;
  perf-root)
    shift
    # devtools to kliknięcie w Ustawieniach, nie root: skrypt tylko otwiera panel i czeka
    [ "${1:-}" = devtools ] && exec "$(cat "$STATE/source")/perf-root.sh" "$@"
    exec sudo "$(cat "$STATE/source")/perf-root.sh" "$@" ;;
  fans)
    shift
    case "${1:-read}" in
      install) exec "$(cat "$STATE/source")/install-fans.sh" --binary "$STATE/fanctl" ;;
      uninstall) exec "$(cat "$STATE/source")/install-fans.sh" --uninstall ;;
    esac
    BIN=/usr/local/libexec/claude-acc-fanctl
    [ -x "$BIN" ] || BIN="$STATE/fanctl"
    exec "$BIN" "${@:-read}" ;;
  uninstall) exec "$(cat "$STATE/source")/setup.sh" --uninstall ;;
esac
exec /usr/bin/python3 "$STATE/accswitch.py" "$@"
EOF
chmod +x "$HOME/.local/bin/claude-acc"

# automaty: tick kont co 2 minuty, porządki przy logowaniu i co 3 godziny, strażnik dev serwerów cały czas,
# perf keep co 5 minut (poprawki Ultra wracają na nowe pid i po restarcie), aktualizacje o 4:30 co 3 dni
for job in $JOBS; do
  plist="$AGENTS/$job.plist"
  sed "s|__HOME__|$HOME|g" "$SRC/launchd/$job.plist.template" > "$plist"
  launchctl bootout "gui/$(id -u)" "$plist" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$plist"
done

# aplikacja w pasku menu
APP="$HOME/Applications/Claude Acc.app"
pkill -x ClaudeAcc 2>/dev/null || true
rm -rf "$APP"
ditto "$APP_SRC" "$APP"
# tuż po pkill LaunchServices potrafi odrzucić pierwsze open (-600)
open "$APP" 2>/dev/null || { sleep 2; open "$APP"; }

echo
echo "gotowe. Sprawdź: claude-acc status, claude-acc mac status, claude-acc guard status"
echo "wiatraki (root, Touch ID): claude-acc fans install; hook dla agentów: README, sekcja Dev server guard"
