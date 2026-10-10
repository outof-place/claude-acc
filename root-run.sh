#!/bin/bash
# Jedyne wejście roota do skryptów claude-acc: kopia w /usr/local/libexec/claude-acc-root, którą stawia
# root-install.sh (claude-acc root install). Komendy claude-acc wołają `sudo <kopia>/root-run.sh <co> ...`,
# więc pod rootem nigdy nie biegnie plik z miejsca zapisywalnego bez roota ($STATE, libexec Homebrew,
# Pod.app w /Applications).
#
#   root-run.sh perf-root ...       perf-root.sh z kopii
#   root-run.sh janitor-root ...    janitor-root.sh z kopii (--dry-run, --high-power)
#   root-run.sh compress-apps [--apps A,B] [--threads N] [--done-ratio R]
#                                   compressapps.py z kopii, z przypiętą kopią afsctool
#
# Przed każdym biegiem: katalog kopii, każdy jej plik i interpreter z root-python (z jego biblioteką
# standardową) należą do roota i nie są zapisywalne dla grupy ani świata, a sumy sha256 zgadzają się
# z pins.sha256 z instalacji. Inaczej odmowa i polecenie ponownej instalacji.
set -euo pipefail

DIR=/usr/local/libexec/claude-acc-root
REINSTALL="kopia roota claude-acc jest niepełna albo zmieniona; zainstaluj ją od nowa: claude-acc root install"

[ "$(id -u)" -eq 0 ] || { echo "root-run.sh tylko przez sudo" >&2; exit 1; }
[ "$(cd "$(dirname "$0")" && pwd -P)" = "$DIR" ] || { echo "root-run.sh biegnie tylko z $DIR" >&2; exit 1; }

# --- funkcje (testy wycinają ten blok) ---
# ścieżka i wszystkie katalogi nadrzędne: właściciel root, bez zapisu dla grupy i świata
root_chain() {
  local p="$1" uid perm
  while :; do
    read -r uid perm < <(/usr/bin/stat -f "%u %Lp" "$p" 2>/dev/null) || return 1
    [ "$uid" = 0 ] || return 1
    [ $((8#$perm & 8#022)) -eq 0 ] || return 1
    [ "$p" = / ] && return 0
    p="$(/usr/bin/dirname "$p")"
  done
}

refuse() {
  echo "$1" >&2
  echo "$REINSTALL" >&2
  exit 1
}

# opcje kompresji: tylko te i tylko takie wartości (reszta, np. własny --afsctool, to odmowa);
# wynik w tablicy ARGS
compress_args() {
  # bez {1,400}: macOS ma RE_DUP_MAX 255, a dłuższe powtórzenie psuje cały wzorzec
  local apps='^[A-Za-z0-9 ._,+-]+$' threads='^[0-9]{1,3}$' ratio='^(0(\.[0-9]{1,4})?|1(\.0{1,4})?)$'
  ARGS=()
  while [ $# -gt 0 ]; do
    case "$1" in
      --apps) [[ "${2:-}" =~ $apps ]] && [ "${#2}" -le 400 ] || { echo "zła wartość --apps" >&2; return 2; } ;;
      --threads) [[ "${2:-}" =~ $threads ]] || { echo "zła wartość --threads" >&2; return 2; } ;;
      --done-ratio) [[ "${2:-}" =~ $ratio ]] || { echo "zła wartość --done-ratio" >&2; return 2; } ;;
      *) echo "nieznana opcja kompresji: $1" >&2; return 2 ;;
    esac
    ARGS+=("$1" "$2")
    shift 2
  done
}
# --- koniec funkcji ---

root_chain "$DIR" || refuse "$DIR nie należy do roota albo jest zapisywalny"
for f in "$DIR"/*; do
  root_chain "$f" || refuse "$f nie należy do roota albo jest zapisywalny"
done
(cd "$DIR" && /usr/bin/shasum -a 256 -s -c pins.sha256) || refuse "sumy sha256 kopii roota się nie zgadzają"
# root-python: pierwsza linia to interpreter, reszta to ścieżki, które on sam podał przy instalacji
PY=""
while IFS= read -r line; do
  [ -n "$line" ] || continue
  [ -n "$PY" ] || PY="$line"
  root_chain "$line" || refuse "interpreter roota: $line nie należy do roota albo jest zapisywalny"
done < "$DIR/root-python"
[ -n "$PY" ] || refuse "brak interpretera w root-python"

what="${1:-}"
[ $# -gt 0 ] && shift
case "$what" in
  perf-root) exec /bin/bash "$DIR/perf-root.sh" "$@" ;;
  janitor-root) exec /bin/bash "$DIR/janitor-root.sh" "$@" ;;
  compress-apps)
    [ -x "$DIR/afsctool" ] || refuse "brak przypiętej kopii afsctool (brew install afsctool, potem claude-acc root install)"
    compress_args "$@" || exit 2
    out="$(/usr/bin/mktemp /var/tmp/claude-acc-compress.XXXXXX)"
    rc=0
    "$PY" -I -B "$DIR/compressapps.py" run --afsctool "$DIR/afsctool" --json-out "$out" ${ARGS[@]+"${ARGS[@]}"} || rc=$?
    # wyniki dla janitora w ostatniej linii; plik tymczasowy jest roota i znika
    printf 'CLAUDE-ACC-RESULTS %s\n' "$(cat "$out" 2>/dev/null || echo '[]')"
    rm -f "$out"
    exit "$rc" ;;
  *)
    echo "root-run.sh perf-root|janitor-root|compress-apps ..." >&2
    exit 2 ;;
esac
