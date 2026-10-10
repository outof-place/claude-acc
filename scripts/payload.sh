#!/bin/bash
# Paczka claude-acc dla aplikacji, która go w sobie wozi (Pod): ten sam układ co libexec formuły
# Homebrew plus zbudowana aplikacja i binarki Swift, plik VERSION i tarball z sumą sha256.
#
#   scripts/payload.sh [--out DIR] [--version X] [--products DIR]
#
# --products: gotowe binarki (ClaudeAcc, fanctl, claude-acc-hook, claude-acc-pause, claude-acc-desktop,
# pod-acc-run, pod-rootd, pod-rootctl) zamiast budowania ze źródeł; testy i CI z osobnym krokiem buildu. Domyślnie --out dist.
# Wynik: DIR/claude-acc/ (rozpakowana paczka), DIR/claude-acc-payload-<wersja>.tar.gz i .sha256.
#
# Układ 2 (od 1.31.0, znak: pod-acc-run w korzeniu): aplikacja paska menu to `Pod Menu.app` (ten sam
# bundle id i plik ClaudeAcc), którą Pod kładzie w Contents/Library/LoginItems, a automaty to
# LaunchAgents/codes.pod.app.acc.<job>.plist (scripts/pod_agents.py) z BundleProgram pod-acc-run, które
# Pod rejestruje przez SMAppService. Paczkę instaluje jej własny setup.sh:
#   setup.sh --app "<Pod.app>/Contents/Library/LoginItems/Pod Menu.app" --fanctl claude-acc/fanctl \
#            --hook claude-acc/claude-acc-hook --desktop claude-acc/claude-acc-desktop \
#            --owner pod --owner-app <Pod.app> --pod-agents [--python <Pod.app>/Contents/Resources/python/bin/python3]
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$ROOT/dist"
VERSION=""
PRODUCTS=""
while [ $# -gt 0 ]; do
  case "$1" in
    --out) OUT="$2"; shift 2 ;;
    --version) VERSION="$2"; shift 2 ;;
    --products) PRODUCTS="$2"; shift 2 ;;
    *) echo "nieznana opcja: $1" >&2; exit 2 ;;
  esac
done
[ -n "$VERSION" ] || VERSION="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' "$ROOT/app/Info.plist")"
PRODUCT_NAMES="ClaudeAcc fanctl claude-acc-hook claude-acc-pause claude-acc-desktop pod-acc-run pod-rootd pod-rootctl"

if [ -z "$PRODUCTS" ]; then
  (cd "$ROOT/app" && swift build -c release)
  PRODUCTS="$(cd "$ROOT/app" && swift build -c release --show-bin-path)"
fi
for name in $PRODUCT_NAMES; do
  [ -f "$PRODUCTS/$name" ] || { echo "brak produktu Swift: $PRODUCTS/$name" >&2; exit 1; }
done

mkdir -p "$OUT"
OUT="$(cd "$OUT" && pwd)"
DEST="$OUT/claude-acc"
rm -rf "$DEST" "$DEST.new"
mkdir -p "$DEST.new"
cd "$ROOT"
# to samo, co formuła kładzie w libexec (setup.sh kopiuje każdy *.py, więc nowy skrypt nie wymaga zmian)
cp ./*.py janitor-root.sh perf-root.sh root-install.sh root-run.sh setup.sh install-fans.sh install-fsguard.sh \
  sign-app.sh "$DEST.new/"
for dir in launchd hooks skills sdk dictation orca-plugin; do
  if [ -d "$dir" ]; then cp -R "$dir" "$DEST.new/$dir"; fi
done
rm -rf "$DEST.new/orca-plugin/test"
find "$DEST.new" \( -name __pycache__ -o -name .DS_Store \) -prune -exec rm -rf {} +

# aplikacja paska menu pod nazwą Pod; bundle id i plik wykonywalny zostają, bo na nich wiszą zgody
# TCC dyktowania i `pgrep -x ClaudeAcc` Poda
APP="$DEST.new/Pod Menu.app"
mkdir -p "$APP/Contents/MacOS"
cp "$PRODUCTS/ClaudeAcc" "$APP/Contents/MacOS/ClaudeAcc"
cp app/Info.plist "$APP/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Set :CFBundleName Pod Menu" "$APP/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Delete :CFBundleDisplayName" "$APP/Contents/Info.plist" 2>/dev/null || true
/usr/libexec/PlistBuddy -c "Add :CFBundleDisplayName string Pod Menu" "$APP/Contents/Info.plist"
for name in fanctl claude-acc-hook claude-acc-pause claude-acc-desktop pod-acc-run pod-rootd pod-rootctl; do
  cp "$PRODUCTS/$name" "$DEST.new/$name"
done
# automaty dla SMAppService z szablonów setup.sh (te same harmonogramy i logi)
/usr/bin/python3 scripts/pod_agents.py --out "$DEST.new/LaunchAgents" >/dev/null
# Pod's root helper (docs/pod-rootd.md): its job and the script Pod's release.sh builds the signed
# package with (rootd/rootd-pkg.sh --binary pod-rootd ... --out pod-rootd.pkg)
mkdir -p "$DEST.new/rootd"
cp launchd/codes.pod.app.rootd.plist scripts/rootd-pkg.sh "$DEST.new/rootd/"
# podpisy jak w formule; setup.sh podpisuje aplikację jeszcze raz po skopiowaniu (sign-app.sh)
if [ -z "${PAYLOAD_NO_SIGN:-}" ]; then
  DESKTOP_ID="com.filip.claude-acc.desktop"
  codesign --force --sign - --identifier "$DESKTOP_ID" -r="designated => identifier \"$DESKTOP_ID\"" \
    "$DEST.new/claude-acc-desktop"
  codesign --force --sign - --identifier com.filip.claude-acc.pod-acc-run "$DEST.new/pod-acc-run"
  # the helper's peer requirement names these identifiers and wants the hardened runtime and library
  # validation; Pod signs them again with its team and the same options
  codesign --force --sign - --options runtime,library --identifier codes.pod.rootd "$DEST.new/pod-rootd"
  codesign --force --sign - --options runtime,library --identifier codes.pod.rootctl "$DEST.new/pod-rootctl"
  codesign --force --sign - "$APP"
fi

COMMIT="$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)"
printf '%s\n' "$VERSION" > "$DEST.new/VERSION"
printf '{"version": "%s", "commit": "%s", "layout": 2}\n' "$VERSION" "$COMMIT" > "$DEST.new/payload.json"
mv "$DEST.new" "$DEST"

TARBALL="$OUT/claude-acc-payload-$VERSION.tar.gz"
# bez metadanych właściciela i atrybutów macOS (._ pliki): paczka rozpakowuje się czysto u każdego
COPYFILE_DISABLE=1 tar -C "$OUT" --uid 0 --gid 0 --uname root --gname wheel -czf "$TARBALL" claude-acc
(cd "$OUT" && shasum -a 256 "$(basename "$TARBALL")" > "$(basename "$TARBALL").sha256")
echo "paczka: $DEST"
echo "tarball: $TARBALL ($(cut -d' ' -f1 < "$TARBALL.sha256"))"
