#!/bin/bash
# Kopia roota claude-acc: skrypty, które biegną pod sudo, w /usr/local/libexec/claude-acc-root
# (root:wheel), z sumami sha256, które root-run.sh sprawdza przed każdym biegiem. Pod rootem nigdy nie
# biegnie plik z miejsca zapisywalnego bez roota: $STATE, libexec Homebrew ani Pod.app w /Applications
# (aplikacje w /Applications zwykle należą do użytkownika, a sudo nie sprawdza podpisów).
#
#   ./root-install.sh              instaluje albo odświeża kopię (sudo, Touch ID); `claude-acc root install`
#   ./root-install.sh --status     co jest zainstalowane i czy zgadza się z tym katalogiem (bez sudo)
#   ./root-install.sh --uninstall  usuwa kopię (sudo)
#
# W kopii: root-run.sh (jedyne wejście), perf-root.sh, janitor-root.sh, compressapps.py, rootpy.py,
# afsctool z Homebrew, jeśli jest (kopia przypięta sumą: późniejsza zmiana w /opt/homebrew nic nie
# zmienia), root-python (interpreter z rootpy.py i ścieżki, które sam podał) i pins.sha256.
set -euo pipefail
cd "$(dirname "$0")"
SRC="$(pwd -P)"
DIR=/usr/local/libexec/claude-acc-root
SCRIPTS="root-run.sh perf-root.sh janitor-root.sh"
MODULES="compressapps.py rootpy.py"

find_afsctool() {
  local p
  for p in "$(command -v afsctool 2>/dev/null || true)" /opt/homebrew/bin/afsctool /usr/local/bin/afsctool; do
    [ -n "$p" ] && [ -x "$p" ] && { /usr/bin/python3 -B -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' "$p"; return 0; }
  done
  return 1
}

status() {
  if [ ! -f "$DIR/pins.sha256" ]; then
    echo "kopia roota: nie zainstalowana (claude-acc root install)"
    return 1
  fi
  echo "kopia roota: $DIR"
  echo "interpreter: $(head -n 1 "$DIR/root-python" 2>/dev/null || echo '?')"
  [ -x "$DIR/afsctool" ] && echo "afsctool: przypięty" || echo "afsctool: brak (kompresja aplikacji roota wyłączona)"
  local stale="" f
  for f in $SCRIPTS $MODULES; do
    cmp -s "$SRC/$f" "$DIR/$f" || stale="$stale $f"
  done
  if [ -n "$stale" ]; then
    echo "starsza niż ta w claude-acc:$stale (claude-acc root install)"
    return 2
  fi
  echo "zgodna z $SRC"
}

case "${1:-}" in
  --status) status; exit $? ;;
  --uninstall)
    sudo /bin/rm -rf "$DIR"
    echo "usunięta kopia roota $DIR"
    exit 0 ;;
  "") ;;
  *) echo "root-install.sh [--status|--uninstall]" >&2; exit 2 ;;
esac

PY="$(/usr/bin/python3 -B ./rootpy.py)" || { echo "nie instaluję kopii roota" >&2; exit 1; }
TMP="$(mktemp -d -t claude-acc-root)"
trap 'rm -rf "$TMP"' EXIT
{
  echo "$PY"
  "$PY" -I -S -c 'import os, sys, sysconfig
print(sys.executable)
print(os.path.realpath(sys.executable))
print(sysconfig.get_paths()["stdlib"])'
} > "$TMP/root-python"
AFSC="$(find_afsctool || true)"
[ -n "$AFSC" ] || echo "afsctool nie znaleziony: kompresja aplikacji roota zostaje wyłączona (brew install afsctool, potem jeszcze raz)"

# jeden sudo; ścieżki idą jako argumenty, nie w treści skryptu. Sumy liczy root z kopii, które już są jego
sudo /bin/bash -c '
set -euo pipefail
src="$1"; tmp="$2"; afsc="$3"; dir="$4"; scripts="$5"; modules="$6"
/usr/bin/install -d -o root -g wheel -m 755 "$dir"
/bin/rm -f "$dir"/*.new
for f in $scripts; do /usr/bin/install -o root -g wheel -m 755 "$src/$f" "$dir/$f.new"; done
for f in $modules; do /usr/bin/install -o root -g wheel -m 644 "$src/$f" "$dir/$f.new"; done
/usr/bin/install -o root -g wheel -m 644 "$tmp/root-python" "$dir/root-python.new"
if [ -n "$afsc" ]; then /usr/bin/install -o root -g wheel -m 755 "$afsc" "$dir/afsctool.new"; else /bin/rm -f "$dir/afsctool"; fi
for f in "$dir"/*.new; do /bin/mv -f "$f" "${f%.new}"; done
cd "$dir"
files=""
for f in $scripts $modules root-python afsctool; do [ -e "$f" ] && files="$files $f"; done
/usr/bin/shasum -a 256 $files > pins.sha256.new
/usr/sbin/chown root:wheel pins.sha256.new
/bin/chmod 644 pins.sha256.new
/bin/mv -f pins.sha256.new pins.sha256
' root-install "$SRC" "$TMP" "$AFSC" "$DIR" "$SCRIPTS" "$MODULES"
echo "kopia roota zainstalowana w $DIR (interpreter $PY)"
status
