#!/bin/bash
# Instaluje claude-acc na koncie użytkownika z gotowych plików: skrypty, komenda `claude-acc`,
# automaty w launchd i aplikacja w pasku menu.
#
#   setup.sh --app "<ścieżka do Claude Acc.app>" [--fanctl <ścieżka do fanctl>] [--hook <ścieżka do claude-acc-hook>]
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
JOBS="com.filip.claude-acc com.filip.claude-acc.janitor com.filip.claude-acc.devguard com.filip.claude-acc.perf com.filip.claude-acc.updates"

APP_SRC=""
FANCTL=""
HOOK=""
DESKTOP=""
while [ $# -gt 0 ]; do
  case "$1" in
    --app) APP_SRC="$2"; shift 2 ;;
    --fanctl) FANCTL="$2"; shift 2 ;;
    --hook) HOOK="$2"; shift 2 ;;
    --desktop) DESKTOP="$2"; shift 2 ;;
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
      # bramki agentów: MCP, skille i hook podpowiedzi (skrzynki, Pęk kluczy i konfiguracja zostają)
      [ -f "$STATE/mail.py" ] && /usr/bin/python3 "$STATE/mail.py" uninstall >/dev/null 2>&1 || true
      [ -f "$STATE/browser.py" ] && /usr/bin/python3 "$STATE/browser.py" uninstall >/dev/null 2>&1 || true
      # hook schedulera w Codeksie (claude-acc sched codex install), jeśli był
      [ -f "$STATE/sched.py" ] && /usr/bin/python3 "$STATE/sched.py" codex uninstall >/dev/null 2>&1 || true
      [ -f "$STATE/desktop.py" ] && /usr/bin/python3 "$STATE/desktop.py" uninstall >/dev/null 2>&1 || true
      # wspólne serwery MCP wracają do stdio w ~/.claude.json, zanim zniknie ich automat
      [ -f "$STATE/mcpshare.py" ] && /usr/bin/python3 "$STATE/mcpshare.py" unshare --all >/dev/null 2>&1 || true
      echo "usunięte: automaty, aplikacja, komenda claude-acc i hooki pauzy. Stan i konfiguracja zostają w $STATE"
      echo "wiatraki (root) zdejmuje osobno: install-fans.sh --uninstall; hook dla agentów usuń z ~/.claude/settings.json"
      exit 0 ;;
    *) echo "nieznana opcja: $1" >&2; exit 2 ;;
  esac
done
[ -d "$APP_SRC" ] || { echo "brak aplikacji: --app \"<Claude Acc.app>\"" >&2; exit 2; }

mkdir -p "$STATE" "$HOME/.local/bin" "$AGENTS" "$HOME/Applications"
cp "$SRC"/*.py "$STATE/"
# hooki Ultra (szybki npx dla hooków formatowania) leżą obok perf.py
rm -rf "$STATE/hooks.new" && cp -R "$SRC/hooks" "$STATE/hooks.new" && rm -rf "$STATE/hooks" && mv "$STATE/hooks.new" "$STATE/hooks"
# drivery SDK bramki przeglądarki (Python i TypeScript) i `claude-acc browser run`
[ -d "$SRC/sdk" ] && rm -rf "$STATE/sdk.new" && cp -R "$SRC/sdk" "$STATE/sdk.new" && rm -rf "$STATE/sdk" && mv "$STATE/sdk.new" "$STATE/sdk"
[ -n "$FANCTL" ] && cp "$FANCTL" "$STATE/fanctl"
# natywny pomocnik bramy pulpitu; podpis (stabilny designated requirement) trzyma uprawnienia TCC
[ -n "$DESKTOP" ] && cp "$DESKTOP" "$STATE/claude-acc-desktop.new" && mv -f "$STATE/claude-acc-desktop.new" "$STATE/claude-acc-desktop"
[ -n "$HOOK" ] && cp "$HOOK" "$STATE/claude-acc-hook.new" && mv -f "$STATE/claude-acc-hook.new" "$STATE/claude-acc-hook"
# hooki pauzy w C leżą obok claude-acc-hook (install.sh: katalog builda, formuła: libexec);
# bez niego hook.py wpisuje `claude-acc-hook pause`, a bez obu krótki skrypt w powłoce
PAUSE_BIN="$(dirname "${HOOK:-.}")/claude-acc-pause"
[ -n "$HOOK" ] && [ -x "$PAUSE_BIN" ] && cp "$PAUSE_BIN" "$STATE/claude-acc-pause.new" \
  && mv -f "$STATE/claude-acc-pause.new" "$STATE/claude-acc-pause"

# interpreter: uv's CPython 3.14 (PGO and LTO, starts in 26 ms where Xcode's 3.9 takes 37),
# linked as $STATE/python, so launchd jobs, the app, the hook and the command share one;
# without uv the system one. The scripts stay Python 3.9, so either runs them
PY=/usr/bin/python3
UV="$(command -v uv || true)"
[ -z "$UV" ] && [ -x /opt/homebrew/bin/uv ] && UV=/opt/homebrew/bin/uv
if [ -n "$UV" ]; then
  "$UV" python install 3.14 >/dev/null 2>&1 || true
  found="$("$UV" python find --managed-python 3.14 2>/dev/null || true)"
  [ -x "$found" ] && PY="$found"
fi
ln -sfn "$PY" "$STATE/python"
# bytecode up front: acc.py runs every script from it, so no start compiles one
"$STATE/python" -m compileall -q "$STATE"/*.py >/dev/null 2>&1 || true
# the hook's native front reads the words that send a command to Python from here
"$STATE/python" "$STATE/acc.py" devguard words > "$STATE/hook-words.json.new" 2>/dev/null \
  && mv -f "$STATE/hook-words.json.new" "$STATE/hook-words.json" || rm -f "$STATE/hook-words.json.new"
# wyjątki rtk: komend, które owija scheduler, hook rtk nie przepisuje (dwa hooki z updatedInput na
# jednej komendzie dają losowy wynik). Linia w [hooks] configu rtk idzie z tej wersji schedulera
if command -v rtk >/dev/null 2>&1 || [ -x /opt/homebrew/bin/rtk ]; then
  "$STATE/python" "$STATE/acc.py" sched rtk-excludes --write \
    || echo "rtk: wyjątki schedulera niewpisane, szczegóły wyżej" >&2
fi

# pauza limitów: hooki w sesjach Claude Code dopisane do settings.json obok Twoich
# (kopia sprzed pierwszej zmiany: settings.json.bak-claude-acc). Paczka bez hook.py
# (starsza formuła Homebrew) albo zepsuty settings.json nie zatrzymują reszty instalacji.
if [ -f "$SRC/hook.py" ]; then
  # z CLAUDE_ACC_NO_HOOKS=1 zdejmujemy też hooki dopisane przez wcześniejszą instalację
  action=install
  [ -n "${CLAUDE_ACC_NO_HOOKS:-}" ] && action=uninstall
  "$STATE/python" "$STATE/hook.py" "$action" "$CLAUDE_SETTINGS" \
    || echo "hooki pauzy limitów: $action nieudany, szczegóły wyżej" >&2
else
  echo "brak hook.py w $SRC: pauza limitów bez hooków w sesjach Claude Code" >&2
fi
# skąd instalowano: `claude-acc fans install` bierze stamtąd install-fans.sh
echo "$SRC" > "$STATE/source"
# bramki agentów: poczta (MCP `mail`, odświeżana tylko przy skonfigurowanych skrzynkach) i
# przeglądarka (MCP `browser`, tylko gdy już raz zainstalowana); wspólny hook podpowiedzi
if [ -z "${CLAUDE_ACC_NO_HOOKS:-}" ]; then
  "$STATE/python" "$STATE/acc.py" mail install --refresh >/dev/null 2>&1 || true
  "$STATE/python" "$STATE/acc.py" browser install --refresh >/dev/null 2>&1 || true
  "$STATE/python" "$STATE/acc.py" desktop install --refresh >/dev/null 2>&1 || true
  "$STATE/python" "$STATE/acc.py" hint sync >/dev/null 2>&1 || true
fi

# jedna komenda na wszystko: konta, kredyty API (credits), porządki (mac, clean), strażnik (guard),
# wydajność (perf, perf-root), wiatraki (fans), hotspot iPhone'a (hotspot), aktualizacje (update, updates),
# wspólne serwery MCP (mcp),
# a `claude-acc uninstall` zdejmuje to, co postawił ten skrypt
cat > "$HOME/.local/bin/claude-acc" <<'EOF'
#!/bin/sh
STATE="$HOME/.local/share/claude-acc"
PY="$STATE/python"
[ -x "$PY" ] || PY=/usr/bin/python3
RUN="$STATE/acc.py"
case "$1" in
  mac) shift; exec "$PY" "$RUN" janitor "$@" ;;
  clean) shift; exec "$PY" "$RUN" janitor sweep --force "$@" ;;
  guard) shift; exec "$PY" "$RUN" devguard "$@" ;;
  perf) shift; exec "$PY" "$RUN" perf "$@" ;;
  sched) shift; exec "$PY" "$RUN" sched "$@" ;;
  # ciężka komenda spoza agentów (terminal, skrypt, automatyzacja Orki) przez scheduler pamięci
  run) shift; exec "$PY" "$RUN" sched run "$@" ;;
  update) shift; exec "$PY" "$RUN" updates run --force "$@" ;;
  updates) shift; exec "$PY" "$RUN" updates "$@" ;;
  mail) shift; exec "$PY" "$RUN" mail "$@" ;;
  browser) shift; exec "$PY" "$RUN" browser "$@" ;;
  desktop) shift; exec "$PY" "$RUN" desktop "$@" ;;
  # demon roota czyta hotspot.json, więc on/off/status idą bez sudo; install pyta o Touch ID
  hotspot) shift; exec "$PY" "$RUN" hotspot "$@" ;;
  credits) shift; exec "$PY" "$RUN" credits "$@" ;;
  # wspólne serwery MCP: jeden proces stdio dla wszystkich sesji Claude Code
  mcp) shift; exec "$PY" "$RUN" mcpshare "$@" ;;
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
exec "$PY" "$RUN" accswitch "$@"
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
