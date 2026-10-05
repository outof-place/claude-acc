#!/bin/bash
# Instaluje claude-acc na koncie użytkownika z gotowych plików: skrypty, komenda `claude-acc`,
# automaty w launchd i aplikacja w pasku menu.
#
#   setup.sh --app "<ścieżka do Claude Acc.app>" [--fanctl <ścieżka do fanctl>]
#   setup.sh --uninstall   zdejmuje automaty, aplikację, komendę i hooki pauzy limitów;
#                          stan i konfiguracja zostają
#
# Woła go install.sh po zbudowaniu ze źródeł i `claude-acc-setup` z Homebrew, które podaje
# swoją zbudowaną aplikację. Wiatraki (root) to osobny krok: install-fans.sh.
# CLAUDE_ACC_NO_HOOKS=1 pomija hooki pauzy limitów w settings.json Claude Code.
set -euo pipefail
SRC="$(cd "$(dirname "$0")" && pwd)"

STATE="$HOME/.local/share/claude-acc"
AGENTS="$HOME/Library/LaunchAgents"
CLAUDE_SETTINGS="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/settings.json"
JOBS="com.filip.claude-acc com.filip.claude-acc.janitor com.filip.claude-acc.devguard com.filip.claude-acc.perf"

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
      # hooki pauzy limitów; bez automatu nikt by już pauzy nie zdjął, więc wstrzymane
      # sesje budzimy, kasując jej plik
      for hook in "$STATE/hook.py" "$SRC/hook.py"; do
        if [ -f "$hook" ]; then
          /usr/bin/python3 "$hook" uninstall "$CLAUDE_SETTINGS" || true
          break
        fi
      done
      rm -rf "$STATE/pause.json" "$STATE/pause-marks"
      echo "usunięte: automaty, aplikacja, komenda claude-acc i hooki pauzy. Stan i konfiguracja zostają w $STATE"
      echo "wiatraki (root) zdejmuje osobno: install-fans.sh --uninstall; hook dla agentów usuń z ~/.claude/settings.json"
      exit 0 ;;
    *) echo "nieznana opcja: $1" >&2; exit 2 ;;
  esac
done
[ -d "$APP_SRC" ] || { echo "brak aplikacji: --app \"<Claude Acc.app>\"" >&2; exit 2; }

mkdir -p "$STATE" "$HOME/.local/bin" "$AGENTS" "$HOME/Applications"
cp "$SRC/accswitch.py" "$SRC/janitor.py" "$SRC/devguard.py" "$SRC/perf.py" "$SRC/sched.py" "$STATE/"
# hooki Ultra (szybki npx dla hooków formatowania) leżą obok perf.py
rm -rf "$STATE/hooks.new" && cp -R "$SRC/hooks" "$STATE/hooks.new" && rm -rf "$STATE/hooks" && mv "$STATE/hooks.new" "$STATE/hooks"
[ -n "$FANCTL" ] && cp "$FANCTL" "$STATE/fanctl"

# pauza limitów: hooki w sesjach Claude Code dopisane do settings.json obok Twoich
# (kopia sprzed pierwszej zmiany: settings.json.bak-claude-acc). Paczka bez hook.py
# (starsza formuła Homebrew) albo zepsuty settings.json nie zatrzymują reszty instalacji.
if [ -f "$SRC/hook.py" ]; then
  cp "$SRC/hook.py" "$STATE/hook.py"
  # z CLAUDE_ACC_NO_HOOKS=1 zdejmujemy też hooki dopisane przez wcześniejszą instalację
  action=install
  [ -n "${CLAUDE_ACC_NO_HOOKS:-}" ] && action=uninstall
  /usr/bin/python3 "$STATE/hook.py" "$action" "$CLAUDE_SETTINGS" \
    || echo "hooki pauzy limitów: $action nieudany, szczegóły wyżej" >&2
else
  echo "brak hook.py w $SRC: pauza limitów bez hooków w sesjach Claude Code" >&2
fi
# skąd instalowano: `claude-acc fans install` bierze stamtąd install-fans.sh
echo "$SRC" > "$STATE/source"

# jedna komenda na wszystko: konta, porządki (mac, clean), strażnik (guard), wydajność (perf,
# perf-root), wiatraki (fans),
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
# perf keep co 5 minut (poprawki Ultra wracają na nowe pid i po restarcie)
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
