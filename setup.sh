#!/bin/bash
# Instaluje claude-acc na koncie użytkownika z gotowych plików: skrypty, komenda `claude-acc`,
# automaty w launchd i aplikacja w pasku menu.
#
#   setup.sh --app "<ścieżka do Claude Acc.app>" [--fanctl <ścieżka do fanctl>] [--hook <ścieżka do claude-acc-hook>]
#            [--orca-plugin]   wtyczka claude-acc w Orce (bez flagi tylko odświeża już zainstalowaną)
#   setup.sh --uninstall   zdejmuje automaty, aplikację, komendę i hooki pauzy limitów;
#                          stan i konfiguracja zostają. Także gdy claude-acc należy do Pod: wtedy
#                          owner.json dostaje nagrobek "none" i Pod zdejmuje swoje agenty
#   --owner pod [--owner-app <Pod.app>]   instaluje aplikacja, która wozi claude-acc w sobie (paczka
#                          z scripts/payload.sh); zapisuje $STATE/owner.json, po czym setup.sh bez
#                          --owner pod (Homebrew, install.sh) odmawia z kodem 3 (owner.py)
#   --pod-agents           (z --owner pod) automaty i aplikację paska menu prowadzi Pod: agenty
#                          codes.pod.app.acc.* z SMAppService, --app to Pod Menu.app w jego
#                          Contents/Library/LoginItems. Nic w ~/Library/LaunchAgents ani ~/Applications:
#                          stare com.filip.claude-acc.* idą precz, kopia Claude Acc.app też
#   --python <python3>     interpreter $STATE/python (Pod: wbudowany python-build-standalone) zamiast uv
#
# Woła go install.sh po zbudowaniu ze źródeł i `claude-acc-setup` z Homebrew, które podaje
# swoją zbudowaną aplikację. Wiatraki (root) to osobny krok: install-fans.sh.
# CLAUDE_ACC_NO_HOOKS=1 pomija hooki pauzy limitów w settings.json Claude Code.
# HOME inny niż katalog domowy konta (izolowany HOME aplikacji albo testu): odmowa z kodem 4, zanim
# cokolwiek dotknie launchd, aplikacji czy $STATE. Testy claude-acc przechodzą ją przez
# CLAUDE_ACC_ALLOW_FOREIGN_HOME=1, ważne tylko z atrapami launchctl i pkill na początku PATH.
set -euo pipefail
SRC="$(cd "$(dirname "$0")" && pwd)"

# launchd i pasek menu są jedne na sesję konta, a nie na HOME: bootstrap z obcego HOME (2026-10-10
# Pod z izolowanym HOME) podmieniłby automaty prawdziwego konta, a pkill zamknął jego aplikację
real_dir() { [ -n "$1" ] && (cd "$1" 2>/dev/null && pwd -P); }
faked() { case "$(command -v "$1" || true)" in "" | /bin/* | /sbin/* | /usr/bin/* | /usr/sbin/*) return 1 ;; esac; }
# kopia aplikacji do usunięcia: ditto przenosi uprawnienia, więc kopia z pakietu po `chmod a-w` (hotfix
# Pod.app 2026-10-10) ma katalogi tylko do odczytu, a samo rm -rf pada i set -e kończy setup w połowie
remove_app() {
  [ -e "$1" ] || [ -L "$1" ] || return 0
  chmod -R u+w "$1" 2>/dev/null || true
  rm -rf "$1"
}
ACCOUNT_HOME="$(id -P 2>/dev/null | cut -d: -f9)"
HOME_NOW="$(real_dir "${HOME:-}" || true)"
if [ -z "$HOME_NOW" ] || [ "$HOME_NOW" != "$(real_dir "$ACCOUNT_HOME" || true)" ]; then
  if [ "${CLAUDE_ACC_ALLOW_FOREIGN_HOME:-}" = 1 ] && faked launchctl && faked pkill; then
    :  # testy claude-acc: osobny HOME, launchctl i pkill to atrapy
  else
    echo "setup.sh: HOME=${HOME:-} to nie katalog domowy konta $(id -un) (${ACCOUNT_HOME:-nieznany})." >&2
    echo "Odmawiam: automaty w launchd i aplikacja należą do konta, nie do HOME, więc podmieniłbym te prawdziwe." >&2
    echo "Uruchom z prawdziwym HOME. Testy claude-acc: CLAUDE_ACC_ALLOW_FOREIGN_HOME=1 z atrapami launchctl i pkill na PATH." >&2
    exit 4
  fi
fi

# kto jest właścicielem instalacji: z owner.json w $STATE; obcy setup nie nadpisuje skryptów i hooków Pod
OWNER=""
OWNER_APP=""
UNINSTALL=""
ARGS=("$@")
for ((i = 0; i < ${#ARGS[@]}; i++)); do
  case "${ARGS[i]}" in
    --owner) OWNER="${ARGS[i + 1]:-}" ;;
    --owner-app) OWNER_APP="${ARGS[i + 1]:-}" ;;
    --uninstall) UNINSTALL=1 ;;
  esac
done
# odinstalować wolno każdemu (`claude-acc uninstall`); instalować tylko właścicielowi. Python z $SRC
# zawsze z -B: $SRC bywa wnętrzem podpisanej Pod.app, a __pycache__ w niej łamie jej pieczęć
if [ -f "$SRC/owner.py" ] && [ -z "$UNINSTALL" ]; then
  /usr/bin/python3 -B "$SRC/owner.py" check ${OWNER:+--as "$OWNER"} || exit $?
fi

STATE="$HOME/.local/share/claude-acc"
AGENTS="$HOME/Library/LaunchAgents"
CLAUDE_SETTINGS="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/settings.json"
JOBS="com.filip.claude-acc com.filip.claude-acc.janitor com.filip.claude-acc.devguard com.filip.claude-acc.perf com.filip.claude-acc.updates com.filip.claude-acc.jobs com.filip.claude-acc.hotspot-user"
# automaty tylko dla Pod (scripts/pod_agents.py bierze je razem z JOBS): setup.sh nie kładzie ich w
# ~/Library/LaunchAgents, bo taki agent to osobna tożsamość TCC, a admitd wchodzi do katalogów agentów
POD_JOBS="com.filip.claude-acc.admit"

APP_SRC=""
FANCTL=""
HOOK=""
DESKTOP=""
ORCA_PLUGIN=""
POD_AGENTS=""
PYTHON_ARG=""
while [ $# -gt 0 ]; do
  case "$1" in
    --app) APP_SRC="$2"; shift 2 ;;
    --pod-agents) POD_AGENTS=1; shift ;;
    --python) PYTHON_ARG="$2"; shift 2 ;;
    --fanctl) FANCTL="$2"; shift 2 ;;
    --hook) HOOK="$2"; shift 2 ;;
    --desktop) DESKTOP="$2"; shift 2 ;;
    --orca-plugin) ORCA_PLUGIN=1; shift ;;
    --owner|--owner-app) shift 2 ;;
    --uninstall)
      for job in $JOBS; do
        launchctl bootout "gui/$(id -u)" "$AGENTS/$job.plist" 2>/dev/null || true
        rm -f "$AGENTS/$job.plist"
      done
      pkill -x ClaudeAcc 2>/dev/null || true
      remove_app "$HOME/Applications/Claude Acc.app"
      rm -rf "$HOME/.local/bin/claude-acc"
      # hooki pauzy limitów; bez automatu nikt by już pauzy nie zdjął, więc wstrzymane
      # sesje budzimy, kasując jej plik
      for hook in "$STATE/hook.py" "$SRC/hook.py"; do
        if [ -f "$hook" ]; then
          /usr/bin/python3 -B "$hook" uninstall "$CLAUDE_SETTINGS" || true
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
      # wtyczka w katalogu wtyczek Orki (zgoda i ustawienia Orki zostają)
      [ -f "$STATE/orcaplugin.py" ] && /usr/bin/python3 "$STATE/orcaplugin.py" uninstall || true
      # claude-acc Poda: nagrobek "none" zamiast pliku, żeby Pod nie zainstalował go znowu przy
      # następnym starcie (bez owner.json robi pierwszą instalację), tylko zdjął swoje agenty i Pod Menu
      [ -f "$SRC/owner.py" ] && { /usr/bin/python3 -B "$SRC/owner.py" uninstalled || true; }
      echo "usunięte: automaty, aplikacja, komenda claude-acc i hooki pauzy. Stan i konfiguracja zostają w $STATE"
      echo "wiatraki (root) zdejmuje osobno: install-fans.sh --uninstall; hook dla agentów usuń z ~/.claude/settings.json"
      exit 0 ;;
    *) echo "nieznana opcja: $1" >&2; exit 2 ;;
  esac
done
[ -d "$APP_SRC" ] || { echo "brak aplikacji: --app \"<Claude Acc.app>\"" >&2; exit 2; }
if [ -n "$POD_AGENTS" ] && [ "$OWNER" != pod ]; then
  echo "--pod-agents tylko z --owner pod: automaty Pod rejestruje sam Pod" >&2
  exit 2
fi

mkdir -p "$STATE" "$HOME/.local/bin"
[ -n "$POD_AGENTS" ] || mkdir -p "$AGENTS" "$HOME/Applications"
cp "$SRC"/*.py "$STATE/"
# hooki Ultra (szybki npx dla hooków formatowania) leżą obok perf.py
rm -rf "$STATE/hooks.new" && cp -R "$SRC/hooks" "$STATE/hooks.new" && rm -rf "$STATE/hooks" && mv "$STATE/hooks.new" "$STATE/hooks"
# drivery SDK bramki przeglądarki (Python i TypeScript) i `claude-acc browser run`
[ -d "$SRC/sdk" ] && rm -rf "$STATE/sdk.new" && cp -R "$SRC/sdk" "$STATE/sdk.new" && rm -rf "$STATE/sdk" && mv "$STATE/sdk.new" "$STATE/sdk"
# paczka Poda ma plik __pycache__ obok każdego .py (scripts/payload.sh), żeby nic nie pisało bajtkodu w
# Pod.app; w $STATE bajtkod jest mile widziany, więc te pliki tu nie przechodzą
find "$STATE/hooks" "$STATE/sdk" -name __pycache__ -type f -delete 2>/dev/null || true
# wtyczka Orki (orcaplugin.py instaluje ją stąd w katalogu wtyczek Orki); bez testów
if [ -d "$SRC/orca-plugin" ]; then
  rm -rf "$STATE/orca-plugin.new" && cp -R "$SRC/orca-plugin" "$STATE/orca-plugin.new" && rm -rf "$STATE/orca-plugin.new/test"
  rm -rf "$STATE/orca-plugin" && mv "$STATE/orca-plugin.new" "$STATE/orca-plugin"
fi
# dyktowanie w aplikacji: słownik, dźwięki i nagranie do testu; własny słownik (slownik-user.txt),
# nagrania czekające na ponowienie i log czasów zostają
if [ -d "$SRC/dictation" ]; then
  mkdir -p "$STATE/dictation/sounds"
  cp "$SRC/dictation/slownik.txt" "$SRC/dictation/test.wav" "$STATE/dictation/"
  cp "$SRC"/dictation/sounds/*.wav "$STATE/dictation/sounds/"
fi
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
if [ -n "$PYTHON_ARG" ] && [ -x "$PYTHON_ARG" ]; then
  # interpreter aplikacji, która wozi claude-acc (Pod: python-build-standalone w Contents/Resources)
  PY="$PYTHON_ARG"
  UV=""
elif [ -n "$PYTHON_ARG" ]; then
  echo "uwaga: --python $PYTHON_ARG nie jest wykonywalny, biorę uv albo systemowy" >&2
fi
if [ -n "$UV" ]; then
  "$UV" python install 3.14 >/dev/null 2>&1 || true
  found="$("$UV" python find --managed-python 3.14 2>/dev/null || true)"
  [ -x "$found" ] && PY="$found"
fi
# /usr/bin/python3 to shim xcrun, który pod nazwą `python` (link $STATE/python) szuka narzędzia
# `python` i woła instalator narzędzi wiersza poleceń: link idzie do interpretera, który shim uruchamia
if [ "$PY" = /usr/bin/python3 ]; then
  PY="$(/usr/bin/python3 -c 'import os, sys; print(os.path.realpath(sys.executable))' 2>/dev/null || echo /usr/bin/python3)"
fi
ln -sfn "$PY" "$STATE/python"
# bytecode up front, under $STATE/pycache where acc.py looks for it (sys.pycache_prefix), so no start
# compiles one and nothing is written next to a script
"$STATE/python" -X pycache_prefix="$STATE/pycache" -m compileall -q "$STATE"/*.py >/dev/null 2>&1 || true
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
# hook admit obok łańcucha fasthooks (admitchain.py): łańcuch woła claude-acc-hook sam, więc nasz wpis
# znika; łańcuch przestał działać, więc wpis wraca (to samo co 5 minut robi `perf keep`)
if [ -z "${CLAUDE_ACC_NO_HOOKS:-}" ] && [ -f "$STATE/admitchain.py" ]; then
  "$STATE/python" "$STATE/acc.py" admitchain heal "$CLAUDE_SETTINGS" || true
fi
# skąd instalowano: `claude-acc fans install` bierze stamtąd install-fans.sh
echo "$SRC" > "$STATE/source"
# Pod's root helper (docs/pod-rootd.md): when the owner app carries it, `claude-acc rootd` is its CLI
# and the old root installers are not offered
ROOTCTL=""
if [ -n "$POD_AGENTS" ] && [ -x "$OWNER_APP/Contents/Resources/claude-acc/pod-rootctl" ]; then
  ROOTCTL="$OWNER_APP/Contents/Resources/claude-acc/pod-rootctl"
  ln -sfn "$ROOTCTL" "$STATE/pod-rootctl"
else
  rm -f "$STATE/pod-rootctl"
fi
# bramki agentów: poczta (MCP `mail`, odświeżana tylko przy skonfigurowanych skrzynkach) i
# przeglądarka (MCP `browser`, tylko gdy już raz zainstalowana); wspólny hook podpowiedzi
if [ -z "${CLAUDE_ACC_NO_HOOKS:-}" ]; then
  "$STATE/python" "$STATE/acc.py" mail install --refresh >/dev/null 2>&1 || true
  "$STATE/python" "$STATE/acc.py" browser install --refresh >/dev/null 2>&1 || true
  "$STATE/python" "$STATE/acc.py" desktop install --refresh >/dev/null 2>&1 || true
  "$STATE/python" "$STATE/acc.py" hint sync >/dev/null 2>&1 || true
fi
# wspólne serwery MCP: plisty mostów z ustawieniami tej wersji (działające mosty do następnego logowania)
[ -f "$STATE/mcpshare.py" ] && { "$STATE/python" "$STATE/acc.py" mcpshare refresh || true; }
# wtyczka Orki: z --orca-plugin instalacja, bez niej tylko odświeżenie tej, którą już zainstalowano
# (ustawień Orki nie rusza; system wtyczek i zgodę włączasz w Orce)
if [ -d "$STATE/orca-plugin" ]; then
  if [ -n "$ORCA_PLUGIN" ]; then
    "$STATE/python" "$STATE/acc.py" orcaplugin install || echo "wtyczka Orki: instalacja nieudana, szczegóły wyżej" >&2
  else
    "$STATE/python" "$STATE/acc.py" orcaplugin install --refresh || true
  fi
fi

# jedna komenda na wszystko: konta, kredyty API (credits), porządki (mac, clean), strażnik (guard),
# wydajność (perf, perf-root), wiatraki (fans), hotspot iPhone'a (hotspot), aktualizacje (update, updates),
# wspólne serwery MCP (mcp), Stay Awake aplikacji (awake), wtyczka Orki (orca),
# a `claude-acc uninstall` zdejmuje to, co postawił ten skrypt
cat > "$HOME/.local/bin/claude-acc" <<'EOF'
#!/bin/sh
STATE="$HOME/.local/share/claude-acc"
PY="$STATE/python"
[ -x "$PY" ] || PY=/usr/bin/python3
RUN="$STATE/acc.py"
case "$1" in
  mac)
    shift
    # porządki roota z kopii roota (claude-acc root install): nigdy skrypt z $STATE pod sudo
    if [ "${1:-}" = root-clean ]; then
      shift
      # in Pod its root helper does it, without sudo (rootroute.py); 75 falls through to the root copy
      if [ -x "$STATE/pod-rootctl" ]; then
        "$PY" "$STATE/rootroute.py" janitor-root "$@"
        rc=$?
        [ "$rc" -eq 75 ] || exit "$rc"
      fi
      [ -x /usr/local/libexec/claude-acc-root/root-run.sh ] || { echo "najpierw raz: claude-acc root install" >&2; exit 1; }
      exec sudo /usr/local/libexec/claude-acc-root/root-run.sh janitor-root "$@"
    fi
    exec "$PY" "$RUN" janitor "$@" ;;
  clean) shift; exec "$PY" "$RUN" janitor sweep --force "$@" ;;
  guard) shift; exec "$PY" "$RUN" devguard "$@" ;;
  # hook admit obok łańcucha fasthooks: status albo heal (to robi też `perf keep` co 5 minut)
  admitchain) shift; exec "$PY" "$RUN" admitchain "$@" ;;
  perf) shift; exec "$PY" "$RUN" perf "$@" ;;
  sched) shift; exec "$PY" "$RUN" sched "$@" ;;
  # ciężka komenda spoza agentów (terminal, skrypt, automatyzacja Orki) przez scheduler pamięci
  run) shift; exec "$PY" "$RUN" sched run "$@" ;;
  update) shift; exec "$PY" "$RUN" updates run --force "$@" ;;
  updates) shift; exec "$PY" "$RUN" updates "$@" ;;
  mail) shift; exec "$PY" "$RUN" mail "$@" ;;
  browser) shift; exec "$PY" "$RUN" browser "$@" ;;
  # dyktowanie w aplikacji: toggle (domyślnie), start, stop, cancel; w tle, bez fokusu
  dictate) exec open -g "claude-acc://dictate/${2:-toggle}" ;;
  # panel aplikacji paska menu (claude-acc://panel[/sekcja]), jak komenda Poda "claude-acc settings…"
  panel) exec open -g "claude-acc://panel${2:+/$2}" ;;
  # Stay Awake aplikacji: on [--for 2h], off, toggle, lid on|off, status [--json]; przez claude-acc://awake
  awake) shift; exec "$PY" "$RUN" awake "$@" ;;
  # wtyczka claude-acc w Orce: install, uninstall, status
  orca) shift; exec "$PY" "$RUN" orcaplugin "$@" ;;
  desktop) shift; exec "$PY" "$RUN" desktop "$@" ;;
  # demon roota czyta hotspot.json, więc on/off/status idą bez sudo; install pyta o Touch ID
  hotspot) shift; exec "$PY" "$RUN" hotspot "$@" ;;
  # strażnik fseventsd (demon roota): install|uninstall przez install-fsguard.sh (sudo, Touch ID), status bez roota
  fsguard)
    case "${2:-status}" in
      install) exec "$(cat "$STATE/source")/install-fsguard.sh" ;;
      uninstall) exec "$(cat "$STATE/source")/install-fsguard.sh" --uninstall ;;
      *)
        PLIST=/Library/LaunchDaemons/com.filip.claude-acc.fsguard.plist
        if [ ! -f "$PLIST" ]; then echo "strażnik fseventsd: nie zainstalowany (claude-acc fsguard install)"; exit 0; fi
        first="$(/usr/libexec/PlistBuddy -c "Print :ProgramArguments:0" "$PLIST" 2>/dev/null || true)"
        flag="$(/usr/libexec/PlistBuddy -c "Print :ProgramArguments:1" "$PLIST" 2>/dev/null || true)"
        # dobry start to interpreter roota z -I (rootpy.py), nigdy zaślepka /usr/bin/python3: ta idzie
        # do wybranego Xcode'a, a Xcode z DMG należy do użytkownika
        if [ "$first" != /usr/bin/python3 ] && [ "$flag" = -I ] && [ "$(stat -f %u "$first" 2>/dev/null)" = 0 ]; then
          echo "strażnik fseventsd: zainstalowany, startuje przez $first -I"
        else
          echo "strażnik fseventsd: startuje przez ${first:-?} (nie interpreter roota z -I); przeinstaluj: claude-acc fsguard install"
        fi
        exit 0 ;;
    esac ;;
  credits) shift; exec "$PY" "$RUN" credits "$@" ;;
  # biegi blogów bez człowieka: płatnik z puli, licznik, limity czuwania, zapis biegu (jobs.py)
  jobs) shift; exec "$PY" "$RUN" jobs "$@" ;;
  # wspólne serwery MCP: jeden proces stdio dla wszystkich sesji Claude Code
  mcp) shift; exec "$PY" "$RUN" mcpshare "$@" ;;
  perf-root)
    shift
    # in Pod its root helper does it, without sudo (rootroute.py); 75: Pod doesn't own claude-acc,
    # the helper doesn't answer, or an old root daemon still owns the tweak, so the root copy below
    if [ -x "$STATE/pod-rootctl" ]; then
      "$PY" "$STATE/rootroute.py" perf-root "$@"
      rc=$?
      [ "$rc" -eq 75 ] || exit "$rc"
    fi
    # devtools to kliknięcie w Ustawieniach, nie root: skrypt tylko otwiera panel i czeka
    [ "${1:-}" = devtools ] && exec "$(cat "$STATE/source")/perf-root.sh" "$@"
    # stan limitu GPU to tylko odczyt sysctl i plisty demona
    case "${1:-} ${2:-}" in "iogpu status" | "iogpu ") exec "$(cat "$STATE/source")/perf-root.sh" "$@" ;; esac
    # pod rootem tylko kopia roota (claude-acc root install): `source` wskazuje libexec Homebrew albo
    # Pod.app, które może zmienić każdy na tym koncie, a sudo nie sprawdza podpisów
    ROOTRUN=/usr/local/libexec/claude-acc-root/root-run.sh
    [ -x "$ROOTRUN" ] || { echo "najpierw raz: claude-acc root install" >&2; exit 1; }
    # już pod sudo (`sudo claude-acc perf-root ...`): drugie sudo nadpisałoby SUDO_USER rootem
    [ "$(id -u)" -eq 0 ] && exec "$ROOTRUN" perf-root "$@"
    exec sudo "$ROOTRUN" perf-root "$@" ;;
  # kopia roota: install (sudo, Touch ID), status, uninstall
  root)
    case "${2:-status}" in
      install) exec "$(cat "$STATE/source")/root-install.sh" ;;
      uninstall) exec "$(cat "$STATE/source")/root-install.sh" --uninstall ;;
      *) exec "$(cat "$STATE/source")/root-install.sh" --status ;;
    esac ;;
  # Pod's root helper: fans, Stay Awake with the lid closed, Ultra's root tweaks, the old daemons' migration
  rootd)
    shift
    [ -x "$STATE/pod-rootctl" ] || { echo "brak pomocnika roota Poda (przychodzi razem z Podem)" >&2; exit 69; }
    exec "$STATE/pod-rootctl" "$@" ;;
  fans)
    shift
    case "${1:-read}" in
      install | uninstall)
        if [ -x "$STATE/pod-rootctl" ]; then
          echo "wiatraki idą przez pomocnika roota Poda: claude-acc rootd fans auto|<30-100>" >&2
          exit 2
        fi ;;
    esac
    case "${1:-read}" in
      install) exec "$(cat "$STATE/source")/install-fans.sh" --binary "$STATE/fanctl" ;;
      uninstall) exec "$(cat "$STATE/source")/install-fans.sh" --uninstall ;;
    esac
    BIN=/usr/local/libexec/claude-acc-fanctl
    [ -x "$BIN" ] || BIN="$STATE/fanctl"
    exec "$BIN" "${@:-read}" ;;
  uninstall) exec "$(cat "$STATE/source")/setup.sh" --uninstall ;;
  # claude-acc z Pod z powrotem do Homebrew: nagrobek "brew" w owner.json (Pod zdejmuje swoje agenty
  # i Pod Menu przy następnym starcie), potem instalacja z formuły
  handback)
    "$PY" "$STATE/owner.py" clear || exit 1
    for setup in "$(command -v claude-acc-setup 2>/dev/null)" /opt/homebrew/bin/claude-acc-setup; do
      [ -n "$setup" ] && [ -x "$setup" ] && exec "$setup"
    done
    echo "instalacja z Homebrew: brew install outof-place/tap/claude-acc && claude-acc-setup" >&2
    exit 0 ;;
esac
exec "$PY" "$RUN" accswitch "$@"
EOF
chmod +x "$HOME/.local/bin/claude-acc"

# automaty: tick kont co 2 minuty, porządki przy logowaniu i co 3 godziny, strażnik dev serwerów cały czas,
# perf keep co 5 minut (poprawki Ultra wracają na nowe pid i po restarcie), aktualizacje o 4:30 co 3 dni,
# harmonogram blogów (jobs tick) co 2 minuty
# Z --pod-agents te same automaty to agenty Pod (codes.pod.app.acc.*, SMAppService): stare zdejmujemy
for job in $JOBS; do
  plist="$AGENTS/$job.plist"
  if [ -n "$POD_AGENTS" ]; then
    launchctl bootout "gui/$(id -u)/$job" 2>/dev/null || true
    rm -f "$plist"
    continue
  fi
  sed "s|__HOME__|$HOME|g" "$SRC/launchd/$job.plist.template" > "$plist"
  launchctl bootout "gui/$(id -u)" "$plist" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$plist"
done

# aplikacja w pasku menu
APP="$HOME/Applications/Claude Acc.app"
if [ -n "$POD_AGENTS" ]; then
  # Pod Menu.app (ten sam plik ClaudeAcc) uruchamia Pod jako element logowania z własnego pakietu:
  # zamykamy tylko starą kopię po ścieżce, nie `pkill -x ClaudeAcc`, i ją usuwamy (claude-acc
  # handback stawia ją z powrotem)
  pkill -f "$APP/Contents/MacOS/ClaudeAcc" 2>/dev/null || true
  remove_app "$APP"
else
  pkill -x ClaudeAcc 2>/dev/null || true
  remove_app "$APP"
  ditto "$APP_SRC" "$APP"
  # źródło tylko do odczytu daje taką samą kopię: podpis niżej i następny setup muszą w niej pisać
  chmod -R u+w "$APP" 2>/dev/null || true
  # podpis, który trzyma zgody macOS dyktowania (Mikrofon, Dostępność, Monitorowanie wejścia) przez
  # aktualizacje: certyfikat z Pęku kluczy albo ad hoc ze stałym designated requirement (sign-app.sh)
  [ -x "$SRC/sign-app.sh" ] && { "$SRC/sign-app.sh" "$APP" || echo "uwaga: podpis aplikacji nie wyszedł, zgody dyktowania mogą wymagać ponownego nadania" >&2; }
  # tuż po pkill LaunchServices potrafi odrzucić pierwsze open (-600)
  open "$APP" 2>/dev/null || { sleep 2; open "$APP"; }
fi

# właściciel (Pod): od teraz brew i install.sh odmawiają; wersja z VERSION paczki albo z aplikacji.
# Na samym końcu, po wszystkich krokach: setup przerwany wcześniej (set -e) zostawia starą wersję,
# więc Pod widzi zmianę wersji i uruchamia go znowu, zamiast uznać instalację za aktualną (2026-10-10)
if [ -n "$OWNER" ]; then
  version="$(cat "$SRC/VERSION" 2>/dev/null || /usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' "$APP_SRC/Contents/Info.plist")"
  "$STATE/python" "$STATE/owner.py" write --owner "$OWNER" --version "$version" ${OWNER_APP:+--app "$OWNER_APP"} \
    ${POD_AGENTS:+--menu "$APP_SRC"}
fi

echo
echo "gotowe. Sprawdź: claude-acc status, claude-acc mac status, claude-acc guard status"
if [ -n "$ROOTCTL" ]; then
  echo "root (wiatraki, Stay Awake z zamkniętą klapą, Ultra): pomocnik roota Poda, włączany w Podzie; stan: claude-acc rootd status"
  echo "hook dla agentów: README, sekcja Dev server guard"
else
  echo "wiatraki (root, Touch ID): claude-acc fans install; hook dla agentów: README, sekcja Dev server guard"
fi
# kopia roota (perf-root, porządki roota, kompresja aplikacji roota) sprzed tej wersji: root jej nie odświeży sam
if [ -d /usr/local/libexec/claude-acc-root ] && ! "$SRC/root-install.sh" --status >/dev/null 2>&1; then
  echo "kopia roota starsza niż ta wersja: claude-acc root install (sudo, Touch ID)"
fi
# host agentów (Orca albo Pod) według orcahost.py
HOST_APP="$("$STATE/python" "$STATE/orcahost.py" app 2>/dev/null || true)"
if [ -n "$HOST_APP" ] && { [ -d "$HOST_APP" ] || [ -d "$HOME/Applications/$(basename "$HOST_APP")" ]; }; then
  echo "$(basename "$HOST_APP" .app): wtyczka claude-acc (pasek statusu, panel, komendy Cmd-J): claude-acc orca install"
fi
