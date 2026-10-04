#!/bin/bash
# Uruchamia hook Claude Code z szybkim npx (npx-fast/npx) na początku PATH. Hook i jego
# logika zostają bez zmian. perf.py (poprawka fast-npx-hooks) dopisuje ten plik przed
# komendą hooka w ~/.claude/settings.json i zdejmuje go przy cofnięciu.
here="$(cd "$(dirname "$0")" && pwd)"
PATH="$here/npx-fast:$PATH"
export PATH
exec "$@"
