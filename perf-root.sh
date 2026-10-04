#!/bin/bash
# Poprawki wydajności, do których perf.py nie ma uprawnień. perf.py tylko je opisuje
# (perf.py list), a ten skrypt robi, mierzy i cofa.
#
# shaper: ogranicznik wysyłania na interfejsie (token bucket regulator, `ifconfig <if>
# tbr <rate>`) ustawiony nieco poniżej zmierzonego uploadu. Wąskim gardłem staje się wtedy
# Mac, a nie router: kolejka wysyłania zostaje w fq_codel interfejsu, gdzie jądro dławi
# gniazda (flow control) zamiast wrzucać pakiety do bufora routera, więc ping, DNS i TLS
# innych połączeń nie czekają za uploadem agentów. Pomaga tylko tam, gdzie router puchnie
# (Zyxel przy Orange); przy BE230 sieć pod obciążeniem dokłada 4 ms i nie ma czego ratować.
# Działa na ruch z tego Maca i do restartu albo ponownego podłączenia adaptera.
#
# vnodes: większy cache vnode (kern.maxvnodes). Jądro trzyma 263168 vnode i przy pracy
# agentów odzyskuje ich ponad 1000 na sekundę: metadane jednego drzewa node_modules/.pnpm
# portivo (358 tys. wpisów) nie mieszczą się w cache, więc każde kolejne skanowanie modułów
# (tsc, eslint, Turbopack, git status -uall) zaczyna od zera. Czysty pomiar drugiego
# przebiegu lstat: 3,59 -> 2,51 s. Pomaga tylko metadanym; treści plików wypiera presja
# pamięci, nie odzysk vnode. Koszt: ~1,2 KB pamięci jądra na vnode (vnode, inode APFS,
# vm object, namecache, ubc), czyli +0,63 GB przy 786432. Jądro nigdy nie zwalnia vnode
# (vfs.vnstats.vn_dealloc_level=0): po cofnięciu cache przestaje rosnąć, ale pamięć i
# zajęte vnode zostają do restartu, więc trial bez --keep nie jest pełnym cofnięciem.
# Bez --persist wartość wraca do domyślnej po restarcie.
#
# devtools: aplikacja (domyślnie Orca) na liście Narzędzi deweloperskich. Za każdą nową
# binarką testu Go, `go run` czy natywnym modułem node uruchomionym w terminalu agenta stoi
# Orca; dopóki jej tam nie ma, macOS ocenia każdą taką binarkę przy pierwszym exec (skan
# XProtect i zapytanie do Apple o notaryzację): 196 ms p50 na binarkę, w Terminalu 4 ms.
# SIP nie pozwala dopisać jej skryptem nawet rootowi, więc devtools nie wymaga sudo: otwiera
# panel w Ustawieniach, czeka na kliknięcie "+" i zapisuje zmianę (undo tak samo, "-").
#
# Uruchomienie:
#   sudo ./perf-root.sh trial [--rate 27Mbps] [--if en0] [--keep]
#        pomiar, ogranicznik, drugi pomiar, porównanie; bez --keep ogranicznik znika
#   sudo ./perf-root.sh shaper apply [--rate 27Mbps] [--if en0]
#   sudo ./perf-root.sh shaper undo
#   ./perf-root.sh shaper status
#   sudo ./perf-root.sh vnodes trial [--value 786432] [--keep]
#        lstat drzewa modułów przed i po, bez --keep wraca stara wartość
#   sudo ./perf-root.sh vnodes apply|undo [--value 786432] [--persist]
#   sudo ./perf-root.sh spotlight apps-only|undo
#        Spotlight indeksuje tylko aplikacje: katalogi domowe (poza Applications) i dane
#        systemu idą na listę Prywatności; undo przywraca poprzednią listę
#        --persist: LaunchDaemon ustawia wartość przy każdym starcie; undo go zdejmuje
#   ./perf-root.sh devtools add|undo|status [--app /Applications/Orca.app]
#        bez sudo, w Terminalu (czyta TCC.db): otwiera Ustawienia > Prywatność i ochrona >
#        Narzędzia deweloperskie, czeka na "+" (undo: "-") i zapisuje zmianę w stanie
# Bez --rate limit to `shaper_percent` (90%) uploadu z ostatniego `perf.py bench network`
# przy tej samej bramie. --dry-run pokazuje polecenia bez wykonywania.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
PERF="$HERE/perf.py"
DRY=0
KEEP=0
PERSIST=0
RATE=""
IFACE=""
VNODES=786432
DEVTOOLS_APP=/Applications/Orca.app
ARGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY=1 ;;
    --keep) KEEP=1 ;;
    --persist) PERSIST=1 ;;
    --rate) RATE="$2"; shift ;;
    --if) IFACE="$2"; shift ;;
    --value) VNODES="$2"; shift ;;
    --app) DEVTOOLS_APP="$2"; shift ;;
    -*) echo "nieznana opcja: $1" >&2; exit 2 ;;
    *) ARGS+=("$1") ;;
  esac
  shift
done
CMD="${ARGS[0]:-}"
SUB="${ARGS[1]:-}"

# perf.py i jego stan należą do użytkownika, nie do roota
USER_NAME="${SUDO_USER:-$(id -un)}"
as_user() {
  if [ "$(id -u)" -eq 0 ] && [ "$USER_NAME" != root ]; then
    sudo -u "$USER_NAME" -H /usr/bin/python3 "$PERF" "$@"
  else
    /usr/bin/python3 "$PERF" "$@"
  fi
}

do_it() {
  if [ "$DRY" -eq 1 ]; then echo "  (dry-run) $*"; else "$@"; fi
}

need_root() {
  if [ "$(id -u)" -ne 0 ] && [ "$DRY" -eq 0 ]; then
    echo "uruchom przez sudo: sudo $0 $*" >&2
    exit 1
  fi
}

# limit ustawiony teraz na interfejsie, np. "27.00 Mbps", albo pusto; ifconfig -v pisze
# "uplink rate: 25.10 Mbps [eff] / 27.00 Mbps [tbr] / 1.00 Gbps [max]"
current_tbr() {
  ifconfig -v "$1" 2>/dev/null | sed -n 's/.*uplink rate: .*\/ \([0-9.]* [KMG]*bps\) \[tbr.*/\1/p' | head -1
}

resolve() {
  if [ -z "$IFACE" ] || [ -z "$RATE" ]; then
    local found
    if ! found=$(as_user shaper-rate 2>/dev/null); then
      [ -n "$RATE" ] || { echo "podaj --rate albo zmierz sieć: perf.py bench network" >&2; exit 1; }
      found="$(route -n get default | awk '/interface:/ {print $2}') $RATE"
    fi
    [ -n "$IFACE" ] || IFACE="${found%% *}"
    [ -n "$RATE" ] || RATE="${found##* }"
  fi
}

shaper_apply() {
  need_root shaper apply
  resolve
  local before
  before="$(current_tbr "$IFACE")"
  echo "ogranicznik wysyłania: $IFACE $RATE (wcześniej: ${before:-brak})"
  if [ "$DRY" -eq 1 ]; then
    echo "  (dry-run) ifconfig $IFACE tbr $RATE"
    return
  fi
  # ifconfig kończy się zerem także wtedy, gdy interfejs nie przyjął limitu
  ifconfig "$IFACE" tbr "$RATE"
  if [ -z "$(current_tbr "$IFACE")" ]; then
    echo "$IFACE nie przyjął ogranicznika; nic nie zapisuję" >&2
    exit 1
  fi
  as_user record shaper "$IFACE" "$RATE" "prev=${before// /}"
}

shaper_undo() {
  need_root shaper undo
  local detail iface prev
  detail="$(as_user status --json | /usr/bin/python3 -c 'import json,sys
for t in json.load(sys.stdin)["tweaks"]:
    if t["name"] == "shaper" and t["applied"]:
        print(t["detail"])')"
  if [ -z "$detail" ]; then
    iface="${IFACE:-$(route -n get default | awk '/interface:/ {print $2}')}"
    prev=""
    echo "brak zapisu w perf-state.json; zdejmuję ogranicznik z $iface"
  else
    iface="$(echo "$detail" | awk '{print $1}')"
    prev="$(echo "$detail" | sed -n 's/.*prev=\([^ ]*\).*/\1/p')"
  fi
  if [ -n "$prev" ]; then
    echo "przywracam wcześniejszy limit $iface: $prev"
    do_it ifconfig "$iface" tbr "$prev"
  else
    echo "zdejmuję ogranicznik z $iface"
    do_it ifconfig "$iface" tbr 0
  fi
  [ "$DRY" -eq 1 ] || as_user record shaper --forget
}

shaper_status() {
  local iface="${IFACE:-$(route -n get default | awk '/interface:/ {print $2}')}"
  local now
  now="$(current_tbr "$iface")"
  echo "$iface: ${now:-bez ogranicznika}"
  ifconfig -v "$iface" | grep -E "scheduler|uplink rate|link rate" || true
}

summary() {
  /usr/bin/python3 -c 'import json,sys
a, b = (json.load(open(p))["network"] for p in sys.argv[1:3])
rows = [("pobieranie Mb/s", "down_mbps"), ("wysyłanie Mb/s", "up_mbps"),
        ("bez obciążenia ms", "idle_ms"),
        ("sieć przy pobieraniu p90 ms", "down_net_p90_ms"),
        ("sieć przy wysyłaniu p90 ms", "up_net_p90_ms"),
        ("responsiveness przy pobieraniu ms", "down_loaded_ms"),
        ("responsiveness przy wysyłaniu ms", "up_loaded_ms")]
print("%-36s %10s %10s" % ("", "bez", "z limitem"))
for label, key in rows:
    print("%-36s %10s %10s" % (label, a.get(key), b.get(key)))' "$1" "$2"
}

trial() {
  need_root trial
  resolve
  local tmp
  tmp="$(mktemp -d)"
  chmod 755 "$tmp"
  echo "1/3 pomiar bez ogranicznika ($IFACE)"
  [ "$DRY" -eq 1 ] || as_user bench network --json > "$tmp/before.json"
  echo "2/3 ogranicznik $RATE"
  shaper_apply
  # przerwany pomiar nie może zostawić limitu, o którym nikt nie wie
  [ "$KEEP" -eq 1 ] || [ "$DRY" -eq 1 ] || trap 'shaper_undo; rm -rf "$tmp"; exit 130' INT TERM
  echo "3/3 pomiar z ogranicznikiem"
  [ "$DRY" -eq 1 ] || as_user bench network --json > "$tmp/after.json"
  if [ "$KEEP" -eq 0 ]; then
    trap - INT TERM
    shaper_undo
  else
    echo "ogranicznik zostaje; cofnięcie: sudo $0 shaper undo"
  fi
  [ "$DRY" -eq 1 ] || summary "$tmp/before.json" "$tmp/after.json"
  rm -rf "$tmp"
}

# jądro zapomina kern.maxvnodes przy restarcie: ten demon ustawia go od nowa przy starcie
VNODES_DAEMON=/Library/LaunchDaemons/com.filip.claude-acc.vnodes.plist

vnodes_persist() {
  echo "przy starcie systemu: kern.maxvnodes=$VNODES ($VNODES_DAEMON)"
  [ "$DRY" -eq 1 ] && return
  cat > "$VNODES_DAEMON" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.filip.claude-acc.vnodes</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/sbin/sysctl</string>
    <string>-w</string>
    <string>kern.maxvnodes=$VNODES</string>
  </array>
  <key>RunAtLoad</key><true/>
</dict>
</plist>
PLIST
  chown root:wheel "$VNODES_DAEMON"
  chmod 644 "$VNODES_DAEMON"
  launchctl bootout system "$VNODES_DAEMON" 2>/dev/null || true
  launchctl bootstrap system "$VNODES_DAEMON"
}

vnodes_apply() {
  need_root vnodes apply
  local before
  before="$(sysctl -n kern.maxvnodes)"
  PREV_VNODES="$before"
  echo "kern.maxvnodes: $before -> $VNODES"
  do_it sysctl -w kern.maxvnodes="$VNODES"
  if [ "$DRY" -eq 0 ]; then
    if [ "$(sysctl -n kern.maxvnodes)" != "$VNODES" ]; then
      echo "jądro nie przyjęło nowej wartości; nic nie zapisuję" >&2
      exit 1
    fi
    as_user record vnodes "$VNODES" "prev=$before"
  fi
  [ "$PERSIST" -eq 1 ] && vnodes_persist
  return 0
}

vnodes_undo() {
  need_root vnodes undo
  local prev
  prev="$(as_user status --json | /usr/bin/python3 -c 'import json,sys
for t in json.load(sys.stdin)["tweaks"]:
    if t["name"] == "vnodes" and t["applied"]:
        print(t["detail"].split("prev=")[-1])')"
  prev="${prev:-263168}"
  echo "kern.maxvnodes: $(sysctl -n kern.maxvnodes) -> $prev"
  echo "  (jądro nie zwalnia już zajętych vnode: pamięć wraca dopiero po restarcie)"
  do_it sysctl -w kern.maxvnodes="$prev"
  if [ -f "$VNODES_DAEMON" ]; then
    do_it launchctl bootout system "$VNODES_DAEMON" 2>/dev/null || true
    do_it rm -f "$VNODES_DAEMON"
  fi
  [ "$DRY" -eq 1 ] || as_user record vnodes --forget
}

# Lista Prywatności Spotlight siedzi w VolumeConfiguration.plist, ale mds trzyma ją w pamięci
# i przy `mdutil -i` zapisuje swoją wersję z powrotem. Działa: zapis pliku, SIGKILL dla mds
# (bez szansy na zapis), launchd stawia go od nowa z listą z dysku, potem przebudowa indeksu,
# żeby wypadły stare wpisy. `launchctl kickstart` blokuje SIP, zwykły kill nie.
SPOTLIGHT_CONFIG=/System/Volumes/Data/.Spotlight-V100/VolumeConfiguration.plist

spotlight_exclusions() {
  # $1: "apps-only" albo plik z listą do przywrócenia; wypisuje poprzednią listę
  /usr/bin/python3 - "$SPOTLIGHT_CONFIG" "$1" "$(eval echo "~$USER_NAME")" <<'PY'
import json, os, plistlib, sys
path, mode, home = sys.argv[1:4]
raw = open(path, "rb").read()
data = plistlib.loads(raw)
before = data.get("Exclusions", [])
if mode == "apps-only":
    wanted = [os.path.join(home, n) for n in sorted(os.listdir(home))
              if not n.startswith(".") and n != "Applications"
              and os.path.isdir(os.path.join(home, n)) and not os.path.islink(os.path.join(home, n))]
    wanted += [p for p in ("/Library", "/opt", "/usr/local", "/Users/Shared") if os.path.isdir(p)]
else:
    wanted = json.load(open(mode))
data["Exclusions"] = wanted
tmp = path + ".tmp"
with open(tmp, "wb") as f:
    plistlib.dump(data, f, fmt=plistlib.FMT_BINARY if raw.startswith(b"bplist") else plistlib.FMT_XML)
st = os.stat(path)
os.chown(tmp, st.st_uid, st.st_gid)
os.chmod(tmp, st.st_mode & 0o7777)
os.replace(tmp, path)
print(json.dumps(before))
PY
}

spotlight_reload() {
  # mds bez zapisu na wyjściu, launchd go podnosi; potem indeks od zera (mały: same aplikacje)
  do_it pkill -9 -x mds
  sleep 5
  do_it mdutil -E /System/Volumes/Data >/dev/null
}

spotlight_apply() {
  need_root spotlight apps-only
  local state_dir prev
  state_dir="$(eval echo "~$USER_NAME")/.local/share/claude-acc"
  if [ "$DRY" -eq 1 ]; then
    echo "  (dry-run) lista Prywatności: katalogi domowe poza Applications, /Library, /opt, /usr/local"
    return
  fi
  prev="$(spotlight_exclusions apps-only)"
  # poprzednia lista na undo, tylko przy pierwszym zastosowaniu
  [ -f "$state_dir/spotlight-exclusions.json" ] || {
    echo "$prev" > "$state_dir/spotlight-exclusions.json"
    chown "$USER_NAME" "$state_dir/spotlight-exclusions.json"
  }
  spotlight_reload
  as_user record spotlight "apps-only"
  echo "Spotlight indeksuje tylko aplikacje; cofnięcie: sudo $0 spotlight undo"
}

spotlight_undo() {
  need_root spotlight undo
  local state_dir
  state_dir="$(eval echo "~$USER_NAME")/.local/share/claude-acc"
  [ -f "$state_dir/spotlight-exclusions.json" ] || { echo "brak zapisanej listy" >&2; exit 1; }
  [ "$DRY" -eq 1 ] && { echo "  (dry-run) przywracam listę z $state_dir/spotlight-exclusions.json"; return; }
  spotlight_exclusions "$state_dir/spotlight-exclusions.json" >/dev/null
  spotlight_reload
  rm -f "$state_dir/spotlight-exclusions.json"
  as_user record spotlight --forget
  echo "przywrócona poprzednia lista Prywatności Spotlight"
}

vnodes_trial() {
  need_root vnodes trial
  local tmp
  tmp="$(mktemp -d)"
  echo "1/3 lstat drzewa modułów przy obecnym cache"
  [ "$DRY" -eq 1 ] || as_user bench fs --json > "$tmp/before.json"
  echo "2/3 większy cache vnode"
  vnodes_apply
  [ "$KEEP" -eq 1 ] || [ "$DRY" -eq 1 ] || trap 'vnodes_undo; rm -rf "$tmp"; exit 130' INT TERM
  echo "3/3 lstat drzewa modułów z większym cache"
  [ "$DRY" -eq 1 ] || as_user bench fs --json > "$tmp/after.json"
  if [ "$KEEP" -eq 0 ]; then
    trap - INT TERM
    vnodes_undo
  else
    # zmierzony efekt trafia do stanu, żeby panel pokazał przed i po
    [ "$DRY" -eq 1 ] || as_user record vnodes "$VNODES" "prev=$PREV_VNODES" --result \
      "$(/usr/bin/python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["fs"]["warm_s"])' "$tmp/before.json")" \
      "$(/usr/bin/python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["fs"]["warm_s"])' "$tmp/after.json")"
    if [ "$PERSIST" -eq 1 ]; then
      echo "nowa wartość zostaje, także po restarcie; cofnięcie: sudo $0 vnodes undo"
    else
      echo "nowa wartość zostaje do restartu (--persist: na stałe); cofnięcie: sudo $0 vnodes undo"
    fi
  fi
  [ "$DRY" -eq 1 ] || /usr/bin/python3 -c 'import json,sys
a, b = (json.load(open(p))["fs"] for p in sys.argv[1:3])
print("%-34s %10s %10s" % ("", "przed", "po"))
for label, key in (("drugi przebieg lstat s", "warm_s"), ("vnode z odzysku", "warm_recycled"), ("kern.maxvnodes", "maxvnodes")):
    print("%-34s %10s %10s" % (label, a.get(key), b.get(key)))' "$tmp/before.json" "$tmp/after.json"
  rm -rf "$tmp"
}

# devtools: Narzędzia deweloperskie (Prywatność i ochrona) to wpisy kTCCServiceDeveloperTool
# w systemowym TCC.db. Gdy za procesem stoi taka aplikacja, świeżo zbudowana binarka (test
# Go, `go run`, natywny moduł node) startuje bez oceny Gatekeepera: bez skanu XProtect i bez
# zapytania o notaryzację do Apple (~150 ms sieci, limit 3 s). Terminal był na liście, Orca
# nie, więc każda nowa binarka testu w terminalu agenta czekała 196 ms p50 (w Terminalu 4 ms).
# Zapisu nie da się zrobić skryptem: SIP chroni TCC.db także przed rootem ("attempt to write
# a readonly database"), a tccutil umie tylko kasować. Zostaje "+" w Ustawieniach (Touch ID);
# skrypt otwiera właściwy panel, czeka, aż wpis się pojawi, i zapisuje go w stanie perf.py.
# Czytanie bazy nie wymaga roota, tylko Pełnego dostępu do dysku (Terminal go ma).
DEVTOOLS_DB="/Library/Application Support/com.apple.TCC/TCC.db"
DEVTOOLS_PANE="x-apple.systempreferences:com.apple.settings.PrivacySecurity.extension?Privacy_DevTools"
DEVTOOLS_WAIT=300

devtools_bundle() {
  /usr/libexec/PlistBuddy -c "Print :CFBundleIdentifier" "$DEVTOOLS_APP/Contents/Info.plist"
}

devtools_value() {
  # auth_value wpisu aplikacji ($1): 2 dozwolone, 0 wyłączone, pusto gdy jej nie ma na liście
  sqlite3 "$DEVTOOLS_DB" "select auth_value from access where service='kTCCServiceDeveloperTool' and client='$1' and client_type=0"
}

devtools_readable() {
  sqlite3 "$DEVTOOLS_DB" "select 1" >/dev/null 2>&1 && return 0
  echo "nie mogę czytać $DEVTOOLS_DB: ta aplikacja nie ma Pełnego dostępu do dysku," >&2
  echo "uruchom to w Terminalu" >&2
  exit 1
}

devtools_open() {
  if [ "$(id -u)" -eq 0 ] && [ "$USER_NAME" != root ]; then
    do_it sudo -u "$USER_NAME" open "$DEVTOOLS_PANE"
  else
    do_it open "$DEVTOOLS_PANE"
  fi
}

devtools_wait() {
  # czeka, aż auth_value aplikacji $1 będzie równe $2 (pusto: brak wpisu)
  local waited=0
  while [ "$(devtools_value "$1")" != "$2" ]; do
    if [ "$waited" -ge "$DEVTOOLS_WAIT" ]; then
      echo "po ${DEVTOOLS_WAIT} s nic się nie zmieniło; uruchom ponownie, gdy skończysz" >&2
      exit 1
    fi
    sleep 2
    waited=$((waited + 2))
  done
}

devtools_apply() {
  local bundle prev
  bundle="$(devtools_bundle)"
  case "$bundle" in *[!A-Za-z0-9.-]*|"") echo "dziwny identyfikator aplikacji: $bundle" >&2; exit 1 ;; esac
  devtools_readable
  prev="$(devtools_value "$bundle")"
  if [ "$prev" != 2 ]; then
    echo "Narzędzia deweloperskie: dodaj $DEVTOOLS_APP"
    echo "  Ustawienia systemowe > Prywatność i ochrona > Narzędzia deweloperskie >"
    if [ -z "$prev" ]; then
      echo "  \"+\" > $DEVTOOLS_APP > Otwórz (Touch ID)"
    else
      echo "  włącz przełącznik przy $(basename "$DEVTOOLS_APP" .app) (Touch ID)"
    fi
    devtools_open
    [ "$DRY" -eq 1 ] && return 0
    echo "czekam na zmianę (do ${DEVTOOLS_WAIT} s)..."
    devtools_wait "$bundle" 2
  fi
  echo "Narzędzia deweloperskie: $bundle dozwolone (wcześniej: ${prev:-brak})"
  # zwolnienie dostaje proces uruchomiony po zmianie (log syspolicyd: dalej GK performScan)
  echo "zrestartuj $(basename "$DEVTOOLS_APP" .app), żeby zadziałało (zamknie sesje w jej terminalach)"
  [ "$DRY" -eq 1 ] || as_user record devtools "$bundle" "prev=${prev:-none}"
}

devtools_undo() {
  local detail bundle prev name
  detail="$(as_user status --json | /usr/bin/python3 -c 'import json,sys
for t in json.load(sys.stdin)["tweaks"]:
    if t["name"] == "devtools" and t["applied"]:
        print(t["detail"])')"
  bundle="${detail%% *}"
  [ -n "$bundle" ] || bundle="$(devtools_bundle)"
  prev="$(printf '%s' "$detail" | sed -n 's/.*prev=\([^ ]*\).*/\1/p')"
  devtools_readable
  name="$(basename "$DEVTOOLS_APP" .app)"
  [ "$prev" = none ] && prev=""
  if [ "$(devtools_value "$bundle")" != "$prev" ]; then
    echo "Narzędzia deweloperskie: przywróć $bundle"
    echo "  Ustawienia systemowe > Prywatność i ochrona > Narzędzia deweloperskie >"
    if [ -z "$prev" ]; then
      echo "  zaznacz $name > \"-\" (Touch ID)"
    else
      echo "  wyłącz przełącznik przy $name (Touch ID)"
    fi
    devtools_open
    [ "$DRY" -eq 1 ] && return 0
    echo "czekam na zmianę (do ${DEVTOOLS_WAIT} s)..."
    devtools_wait "$bundle" "$prev"
  fi
  [ "$DRY" -eq 1 ] || as_user record devtools --forget
}

devtools_status() {
  devtools_readable
  sqlite3 "$DEVTOOLS_DB" "select client, auth_value from access where service='kTCCServiceDeveloperTool'" |
    sed 's/|2$/ (dozwolone)/; s/|0$/ (wyłączone)/'
}

case "$CMD $SUB" in
  "shaper apply") shaper_apply ;;
  "shaper undo") shaper_undo ;;
  "shaper status" | "shaper ") shaper_status ;;
  "trial ") trial ;;
  "vnodes trial") vnodes_trial ;;
  "vnodes apply") vnodes_apply ;;
  "vnodes undo") vnodes_undo ;;
  "spotlight apps-only") spotlight_apply ;;
  "spotlight undo") spotlight_undo ;;
  "devtools add") devtools_apply ;;
  "devtools undo") devtools_undo ;;
  "devtools status" | "devtools ") devtools_status ;;
  *)
    awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0"
    exit 2
    ;;
esac
