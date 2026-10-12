#!/bin/bash
# Pod's root helper as an installer package (docs/pod-rootd.md, "The package"): the program in
# /Library/PrivilegedHelperTools and the job in /Library/LaunchDaemons, both root:wheel, which no
# process of an admin user can write; preinstall boots an earlier helper out, postinstall records
# the Pod.app the package came from (self-updates look there first) and boots the job in. Pod's
# release.sh signs it with the Developer ID Installer identity and notarizes it; tests build it
# unsigned. Never installs anything itself.
#
#   rootd-pkg.sh --binary PATH --version N --out PKG [--plist PATH] [--sign IDENTITY]
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
BINARY="" VERSION="" OUT="" SIGN=""
# in the payload the plist sits next to this script; in the repository it is in launchd/
PLIST="$HERE/codes.pod.app.rootd.plist"
[ -f "$PLIST" ] || PLIST="$HERE/../launchd/codes.pod.app.rootd.plist"
PACKAGE_ID="codes.pod.rootd.pkg"
PACKAGE_NAME="pod-rootd.pkg"

die() { echo "rootd-pkg.sh: $*" >&2; exit 2; }

while [ $# -gt 0 ]; do
  case "$1" in
    --binary) BINARY="${2:-}"; shift 2 ;;
    --version) VERSION="${2:-}"; shift 2 ;;
    --out) OUT="${2:-}"; shift 2 ;;
    --plist) PLIST="${2:-}"; shift 2 ;;
    --sign) SIGN="${2:-}"; shift 2 ;;
    *) die "unknown option: $1" ;;
  esac
done
[ -f "$BINARY" ] || die "--binary: no such file: $BINARY"
[ -n "$OUT" ] || die "--out is missing"
case "$VERSION" in ''|*[!0-9.]*|.*|*.) die "--version is dotted numbers, not: $VERSION" ;; esac
LABEL="$(/usr/bin/plutil -extract Label raw -o - "$PLIST")"
PROGRAM="$(/usr/bin/plutil -extract Program raw -o - "$PLIST")"
case "$LABEL" in ''|*[!A-Za-z0-9.-]*) die "odd Label in $PLIST: $LABEL" ;; esac
[ "$PROGRAM" = "/Library/PrivilegedHelperTools/$LABEL" ] || die "Program in $PLIST is not /Library/PrivilegedHelperTools/$LABEL"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
ROOT="$WORK/root"
SCRIPTS="$WORK/scripts"
mkdir -p "$ROOT/Library/PrivilegedHelperTools" "$ROOT/Library/LaunchDaemons" "$SCRIPTS"
# the modes macOS gives these directories, so the install doesn't change them
chmod 755 "$ROOT/Library" "$ROOT/Library/LaunchDaemons"
chmod 1755 "$ROOT/Library/PrivilegedHelperTools"
install -m 755 "$BINARY" "$ROOT$PROGRAM"
install -m 644 "$PLIST" "$ROOT/Library/LaunchDaemons/$LABEL.plist"

cat > "$SCRIPTS/preinstall" <<EOF
#!/bin/sh
# an earlier helper goes first; its state in /var/db/$LABEL stays for the new one
/bin/launchctl bootout system/$LABEL 2>/dev/null || true
exit 0
EOF
cat > "$SCRIPTS/postinstall" <<EOF
#!/bin/sh
# \$1 is the package. Opened from a Pod.app, that app is where the helper's updates come from.
state=/var/db/$LABEL
case "\$1" in
  */*.app/Contents/Resources/claude-acc/$PACKAGE_NAME)
    app="\${1%/Contents/Resources/claude-acc/$PACKAGE_NAME}"
    /bin/mkdir -p -m 700 "\$state"
    /usr/bin/printf '%s\n' "\$app" > "\$state/app.new"
    /bin/chmod 600 "\$state/app.new"
    /bin/mv -f "\$state/app.new" "\$state/app"
    ;;
esac
/bin/launchctl bootstrap system /Library/LaunchDaemons/$LABEL.plist
EOF
chmod 755 "$SCRIPTS/preinstall" "$SCRIPTS/postinstall"
# the files' com.apple.provenance goes into the Bom as ._ entries, which Installer folds back into
# extended attributes (pkgutil --expand-full shows no ._ files)

/usr/bin/pkgbuild --quiet --root "$ROOT" --identifier "$PACKAGE_ID" --version "$VERSION" --scripts "$SCRIPTS" \
  --ownership recommended --install-location / "$WORK/rootd.pkg"
if [ -n "$SIGN" ]; then
  /usr/bin/productbuild --quiet --package "$WORK/rootd.pkg" --sign "$SIGN" "$OUT"
else
  /usr/bin/productbuild --quiet --package "$WORK/rootd.pkg" "$OUT"
fi
echo "$OUT: $PACKAGE_ID $VERSION ($LABEL)${SIGN:+, signed by $SIGN}"
