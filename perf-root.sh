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
# agentów odzyskuje ich ponad 1000 na sekundę: jedno drzewo node_modules/.pnpm portivo
# (358 tys. wpisów) nie mieści się w cache, więc każde kolejne skanowanie modułów (tsc,
# eslint, Turbopack, git status -uall) zaczyna od zera. Koszt: około 1,1 KB pamięci
# jądra na vnode (vnode, inode APFS, namecache), czyli ~0,6 GB przy 786432. Wartość
# wraca do domyślnej po restarcie.
#
# Uruchomienie:
#   sudo ./perf-root.sh trial [--rate 27Mbps] [--if en0] [--keep]
#        pomiar, ogranicznik, drugi pomiar, porównanie; bez --keep ogranicznik znika
#   sudo ./perf-root.sh shaper apply [--rate 27Mbps] [--if en0]
#   sudo ./perf-root.sh shaper undo
#   ./perf-root.sh shaper status
#   sudo ./perf-root.sh vnodes trial [--value 786432] [--keep]
#        lstat drzewa modułów przed i po, bez --keep wraca stara wartość
#   sudo ./perf-root.sh vnodes apply|undo [--value 786432]
# Bez --rate limit to `shaper_percent` (90%) uploadu z ostatniego `perf.py bench network`
# przy tej samej bramie. --dry-run pokazuje polecenia bez wykonywania.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
PERF="$HERE/perf.py"
DRY=0
KEEP=0
RATE=""
IFACE=""
VNODES=786432
ARGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY=1 ;;
    --keep) KEEP=1 ;;
    --rate) RATE="$2"; shift ;;
    --if) IFACE="$2"; shift ;;
    --value) VNODES="$2"; shift ;;
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

vnodes_apply() {
  need_root vnodes apply
  local before
  before="$(sysctl -n kern.maxvnodes)"
  echo "kern.maxvnodes: $before -> $VNODES"
  do_it sysctl -w kern.maxvnodes="$VNODES"
  if [ "$DRY" -eq 0 ]; then
    if [ "$(sysctl -n kern.maxvnodes)" != "$VNODES" ]; then
      echo "jądro nie przyjęło nowej wartości; nic nie zapisuję" >&2
      exit 1
    fi
    as_user record vnodes "$VNODES" "prev=$before"
  fi
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
  do_it sysctl -w kern.maxvnodes="$prev"
  [ "$DRY" -eq 1 ] || as_user record vnodes --forget
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
    echo "nowa wartość zostaje do restartu; cofnięcie: sudo $0 vnodes undo"
  fi
  [ "$DRY" -eq 1 ] || /usr/bin/python3 -c 'import json,sys
a, b = (json.load(open(p))["fs"] for p in sys.argv[1:3])
print("%-34s %10s %10s" % ("", "przed", "po"))
for label, key in (("drugi przebieg lstat s", "warm_s"), ("vnode z odzysku", "warm_recycled"), ("kern.maxvnodes", "maxvnodes")):
    print("%-34s %10s %10s" % (label, a.get(key), b.get(key)))' "$tmp/before.json" "$tmp/after.json"
  rm -rf "$tmp"
}

case "$CMD $SUB" in
  "shaper apply") shaper_apply ;;
  "shaper undo") shaper_undo ;;
  "shaper status" | "shaper ") shaper_status ;;
  "trial ") trial ;;
  "vnodes trial") vnodes_trial ;;
  "vnodes apply") vnodes_apply ;;
  "vnodes undo") vnodes_undo ;;
  *)
    awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0"
    exit 2
    ;;
esac
