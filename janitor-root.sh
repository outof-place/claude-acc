#!/bin/bash
# Porządki, do których janitor.py nie ma uprawnień: systemowe wpisy launchd po
# odinstalowanych aplikacjach i stare raporty awarii. Cache dyld symulatorów iOS
# (/Library/Developer/CoreSimulator/Caches/dyld) chroni SIP nawet przed rootem.
# Z --high-power ustawia też tryb wysokiej wydajności na zasilaczu (MacBook Pro z M Max).
# Uruchomienie: sudo ./janitor-root.sh [--dry-run] [--high-power]
set -euo pipefail

DRY=0
HIGH_POWER=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY=1 ;;
    --high-power) HIGH_POWER=1 ;;
    *) echo "nieznana opcja: $arg" >&2; exit 2 ;;
  esac
done
if [ "$EUID" -ne 0 ] && [ "$DRY" -eq 0 ]; then
  echo "uruchom przez sudo: sudo $0" >&2
  exit 1
fi

do_it() {
  if [ "$DRY" -eq 1 ]; then echo "  (dry-run) $*"; else "$@"; fi
}

# wpisy launchd, których program zniknął razem z aplikacją: launchd i tak nie może ich
# uruchomić, a przy każdym starcie próbuje; odkładamy je obok, nie kasujemy
PARKED="/Library/launchd-disabled-$(date +%Y-%m-%d)"
for dir in /Library/LaunchDaemons /Library/LaunchAgents; do
  for plist in "$dir"/*.plist; do
    [ -e "$plist" ] || continue
    program=$(/usr/libexec/PlistBuddy -c "Print :Program" "$plist" 2>/dev/null \
      || /usr/libexec/PlistBuddy -c "Print :ProgramArguments:0" "$plist" 2>/dev/null || true)
    case "$program" in /*) ;; *) continue ;; esac
    [ -e "$program" ] && continue
    label=$(/usr/libexec/PlistBuddy -c "Print :Label" "$plist" 2>/dev/null || basename "$plist" .plist)
    echo "wpis bez programu: $plist -> $program"
    domain=system
    [ "$dir" = /Library/LaunchAgents ] && domain="gui/$(stat -f %u /dev/console)"
    do_it launchctl bootout "$domain/$label" 2>/dev/null || true
    do_it mkdir -p "$PARKED"
    do_it mv "$plist" "$PARKED/"
  done
done

# raporty awarii i zawieszeń sprzed miesiąca
for dir in /Library/Logs/DiagnosticReports; do
  [ -d "$dir" ] || continue
  count=$(find "$dir" -type f -mtime +30 2>/dev/null | wc -l | tr -d ' ')
  [ "$count" -gt 0 ] || continue
  echo "stare raporty w $dir: $count"
  [ "$DRY" -eq 1 ] || find "$dir" -type f -mtime +30 -delete 2>/dev/null || true
done

# tryb wysokiej wydajności tylko na zasilaczu: na baterii zostaje automatyczny
if [ "$HIGH_POWER" -eq 1 ]; then
  if pmset -g cap | grep -q highpowermode; then
    echo "tryb zasilania na zasilaczu: wysoka wydajność"
    do_it pmset -c powermode 2
  else
    echo "ten Mac nie ma trybu wysokiej wydajności"
  fi
fi

echo "gotowe"
