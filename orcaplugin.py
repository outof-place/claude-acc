#!/usr/bin/env python3
"""Wtyczka claude-acc dla Orki (katalog orca-plugin/): instalacja w katalogu wtyczek Orki i zdjęcie.

  orcaplugin.py status [--json] [--user-data DIR] [--app PATH]
  orcaplugin.py install [--refresh] [--user-data DIR] [--app PATH] [--live auto|on|off]
  orcaplugin.py uninstall [--user-data DIR]

install kładzie wtyczkę tak, jak robi to instalator Orki: niezmienne drzewo w
<userData>/plugins/outof-place.claude-acc/<sha256 drzewa>/ i plik `current` z tym hashem
(src/main/plugins/plugin-discovery.ts, plugin-content-hash.ts). Poprzednia wersja zostaje do
cofnięcia, starsze znikają. Ustawień Orki (profile-state.db) nie dotyka: system wtyczek
(eksperymentalny) i zgodę na wtyczkę włączasz w Orce, w Settings › Plugins.
--refresh (claude-acc-setup) odświeża tylko wtyczkę zainstalowaną wcześniej, bez Orki nic nie robi.
--live: pasek statusu i panel na żywo potrzebują Orki z `contributes.statusBarItems` i
`panelMessaging`; starsza Orka odrzuca taki manifest w całości, więc `auto` szuka tych nazw w
paczce aplikacji i bez nich instaluje manifest bez nich (komendy, powiadomienia i karty działają).
"""

import hashlib
import json
import os
import shutil
import struct
import sys

HOME = os.path.expanduser("~")
HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)
import orcahost  # noqa: E402

SOURCE = os.path.join(HERE, "orca-plugin")


def plugin_key():
    """`<publisher>.<id>` z manifestu: jedyne miejsce, gdzie stoi tożsamość wtyczki (i nazwa katalogu)."""
    try:
        with open(os.path.join(SOURCE, "orca-plugin.json")) as f:
            m = json.load(f)
        return f"{m['publisher']}.{m['id']}"
    except (OSError, ValueError, KeyError):
        return "outof-place.claude-acc"


PLUGIN_KEY = plugin_key()
# host wtyczki (orcahost.py): Orca albo Pod, z jego katalogiem danych; pakiet też w ~/Applications
HOST = orcahost.resolve()
DEFAULT_USER_DATA = HOST.user_data
APPS = tuple(dict.fromkeys((HOST.app, os.path.join(HOME, "Applications", os.path.basename(HOST.app)))))
# pliki drzewa wtyczki; testy i nakładka manifestu zostają w repo
SHIPPED = ("worker.mjs", "lib", "panel")
TREE_PREFIX = b"orca-plugin-tree-v1\0"
MAX_FILES = 2000
MAX_BYTES = 50 * 1024 * 1024


def tree_files(root):
    """Pliki drzewa w kolejności hasha Orki: wpisy każdego katalogu po nazwie, w głąb."""
    out = []

    def walk(d):
        for entry in sorted(os.scandir(d), key=lambda e: e.name):
            if d == root and entry.name == ".git":
                continue
            if entry.is_symlink():
                raise ValueError(f"symlink w wtyczce: {os.path.relpath(entry.path, root)}")
            if entry.is_dir():
                walk(entry.path)
            elif entry.is_file():
                out.append(entry.path)
            if len(out) > MAX_FILES:
                raise ValueError("wtyczka ma za dużo plików")

    walk(root)
    return out


def tree_hash(root):
    """sha256 jak hashPluginTree w Orce: prefiks, potem dla każdego pliku długość i ścieżka,
    długość i treść; długości jako 8 bajtów big-endian."""
    h = hashlib.sha256(TREE_PREFIX)
    total = 0
    for path in tree_files(root):
        rel = os.path.relpath(path, root).replace(os.sep, "/").encode()
        with open(path, "rb") as f:
            data = f.read()
        total += len(data)
        if total > MAX_BYTES:
            raise ValueError("wtyczka przekracza 50 MB")
        h.update(struct.pack(">Q", len(rel)) + rel + struct.pack(">Q", len(data)) + data)
    return h.hexdigest()


def find_app(path=None):
    for candidate in [path] if path else APPS:
        if candidate and os.path.isdir(candidate):
            return candidate
    return None


def app_supports_live(app, marker):
    """Czy paczka Orki zna status bar i panel na żywo: nazwa uprawnienia w skompilowanym main."""
    if not app:
        return False
    resources = os.path.join(app, "Contents/Resources")
    needle = marker.encode()
    for name in ("app.asar", "app/out/main/index.js"):
        path = os.path.join(resources, name)
        if not os.path.isfile(path):
            continue
        with open(path, "rb") as f:
            tail = b""
            while True:
                chunk = f.read(4 * 1024 * 1024)
                if not chunk:
                    break
                if needle in tail + chunk:
                    return True
                tail = chunk[-len(needle):]
    return False


def manifest(live):
    with open(os.path.join(SOURCE, "orca-plugin.json")) as f:
        base = json.load(f)
    if not live:
        return base
    with open(os.path.join(SOURCE, "live-features.json")) as f:
        extra = json.load(f)
    base["contributes"].update(extra["contributes"])
    base["capabilities"] = base["capabilities"] + extra["capabilities"]
    return base


def plugin_dir(user_data):
    return os.path.join(user_data, "plugins", PLUGIN_KEY)


def read_current(pdir):
    try:
        with open(os.path.join(pdir, "current")) as f:
            return f.read().strip()
    except OSError:
        return None


def write_atomic(path, text):
    tmp = f"{path}.tmp{os.getpid()}"
    with open(tmp, "w") as f:
        f.write(text)
    os.replace(tmp, path)


def is_hash(name):
    return len(name) in (32, 64) and all(c in "0123456789abcdef" for c in name)


def stage(pdir, live):
    """Drzewo wtyczki w katalogu tymczasowym obok docelowego (rename w obrębie dysku)."""
    staging = os.path.join(pdir, f".staging-{os.getpid()}")
    shutil.rmtree(staging, ignore_errors=True)
    os.makedirs(staging)
    for name in SHIPPED:
        src = os.path.join(SOURCE, name)
        if os.path.isdir(src):
            shutil.copytree(src, os.path.join(staging, name), ignore=shutil.ignore_patterns(".*"))
        else:
            shutil.copy2(src, os.path.join(staging, name))
    with open(os.path.join(staging, "orca-plugin.json"), "w") as f:
        json.dump(manifest(live), f, indent=2, ensure_ascii=False)
        f.write("\n")
    return staging


def install(user_data, live):
    pdir = plugin_dir(user_data)
    os.makedirs(pdir, exist_ok=True)
    staging = stage(pdir, live)
    try:
        digest = tree_hash(staging)
        target = os.path.join(pdir, digest)
        if os.path.isdir(target):
            shutil.rmtree(staging)
        else:
            os.rename(staging, target)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    previous = read_current(pdir)
    if previous != digest:
        write_atomic(os.path.join(pdir, "current"), digest)
    # jak Orca: bieżąca wersja i jedna do cofnięcia
    keep = {digest, previous}
    for entry in os.listdir(pdir):
        if is_hash(entry) and entry not in keep:
            shutil.rmtree(os.path.join(pdir, entry), ignore_errors=True)
    return digest, previous


def parse(args):
    opts = {"refresh": False, "json": False, "user_data": None, "app": None, "live": "auto"}
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("--refresh", "--json"):
            opts[a[2:]] = True
        elif a in ("--user-data", "--app", "--live") and i + 1 < len(args):
            opts[a[2:].replace("-", "_")] = args[i + 1]
            i += 1
        else:
            raise SystemExit(f"nieznana opcja: {a}\n{__doc__}")
        i += 1
    if opts["live"] not in ("auto", "on", "off"):
        raise SystemExit("--live: auto, on albo off")
    return opts


def cmd_install(opts):
    app = find_app(opts["app"])
    user_data = opts["user_data"] or DEFAULT_USER_DATA
    if not app and not opts["user_data"]:
        if opts["refresh"]:
            return 0
        print(f"brak aplikacji {HOST.name} ({HOST.app}); wskaż ją: --app PATH albo --user-data DIR")
        return 1
    if opts["refresh"] and not os.path.isdir(plugin_dir(user_data)):
        return 0
    with open(os.path.join(SOURCE, "live-features.json")) as f:
        marker = json.load(f)["marker"]
    live = opts["live"] == "on" or (opts["live"] == "auto" and app_supports_live(app, marker))
    digest, previous = install(user_data, live)
    what = "z paskiem statusu i panelem na żywo" if live else "bez paska statusu i panelu na żywo (ta Orca ich nie ma)"
    if previous == digest:
        print(f"wtyczka Orki claude-acc bez zmian ({digest[:12]}, {what})")
    else:
        print(f"wtyczka Orki claude-acc zainstalowana {what}: {plugin_dir(user_data)}/{digest[:12]}…")
        if not opts["refresh"]:
            print("W Orce: Settings › Plugins: włącz system wtyczek (eksperymentalny) i zatwierdź Claude Acc;")
            print("działająca Orka zobaczy nową wersję po ponownym wczytaniu wtyczek albo po restarcie.")
    return 0


def cmd_uninstall(opts):
    pdir = plugin_dir(opts["user_data"] or DEFAULT_USER_DATA)
    if not os.path.isdir(pdir):
        print("wtyczki Orki claude-acc nie ma")
        return 0
    shutil.rmtree(pdir)
    print(f"zdjęta: {pdir} (zgoda i dane wtyczki zostają w Orce; usuniesz je w Settings › Plugins)")
    return 0


def cmd_status(opts):
    app = find_app(opts["app"])
    user_data = opts["user_data"] or DEFAULT_USER_DATA
    pdir = plugin_dir(user_data)
    current = read_current(pdir)
    installed_manifest = None
    if current:
        try:
            with open(os.path.join(pdir, current, "orca-plugin.json")) as f:
                installed_manifest = json.load(f)
        except (OSError, ValueError):
            pass
    with open(os.path.join(SOURCE, "live-features.json")) as f:
        marker = json.load(f)["marker"]
    out = {
        "orca_app": app,
        "orca_supports_live": app_supports_live(app, marker),
        "user_data": user_data,
        "installed": bool(installed_manifest),
        "hash": current,
        "version": (installed_manifest or {}).get("version"),
        "live": bool(installed_manifest and (installed_manifest.get("contributes") or {}).get("statusBarItems")),
        "source_version": manifest(False)["version"],
    }
    if opts["json"]:
        print(json.dumps(out, ensure_ascii=False))
        return 0
    if not app:
        print("Orca: nie znaleziona")
    else:
        print(f"Orca: {app}" + (", ma pasek statusu i panel na żywo" if out["orca_supports_live"] else ", bez paska statusu i panelu na żywo"))
    if out["installed"]:
        stale = out["version"] != out["source_version"] or out["live"] != out["orca_supports_live"]
        print(f"wtyczka: {out['version']} ({current[:12]}), " + ("na żywo" if out["live"] else "bez paska statusu")
              + (f"; do odświeżenia: claude-acc orca install" if stale else ""))
    else:
        print("wtyczka: niezainstalowana (claude-acc orca install)")
    return 0


def main(argv):
    if not argv or argv[0] not in ("status", "install", "uninstall"):
        print(__doc__)
        return 2
    opts = parse(argv[1:])
    return {"status": cmd_status, "install": cmd_install, "uninstall": cmd_uninstall}[argv[0]](opts)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
