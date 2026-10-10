#!/usr/bin/env python3
"""Kto instaluje i aktualizuje claude-acc na tym koncie: `$STATE/owner.json`.

  owner.py show [--json]
  owner.py check [--as pod]      kod 3, gdy claude-acc należy do Pod, a woła ktoś inny (brew, install.sh)
  owner.py write --owner pod --version X [--app PATH] [--menu PATH]
  owner.py menu                  aplikacja paska menu: Pod Menu.app Poda (setup.sh --pod-agents) albo
                                 ~/Applications/Claude Acc.app
  owner.py clear                 oddanie claude-acc z powrotem instalacji z Homebrew albo ze źródeł
                                 (`claude-acc handback`): nagrobek {"owner": "brew"}
  owner.py uninstalled           po setup.sh --uninstall: nagrobek {"owner": "none"}, jeśli należał do Pod
  owner.py forget                usuwa owner.json: Pod przy następnym starcie instaluje claude-acc od nowa

Pliku nie ma: claude-acc instaluje brew (`claude-acc-setup`) albo install.sh, jak dotąd, a Pod robi
pierwszą instalację. Gdy Pod przejmie claude-acc, sam uruchamia setup.sh ze swojej paczki (`--owner pod`)
i zapisuje {"owner": "pod", "version", "app", "at"}, z `--pod-agents` także "menu" (Pod Menu.app w
pakiecie Poda); od tej chwili setup.sh z Homebrew odmawia, bo dwa źródła na zmianę nadpisywałyby sobie
skrypty, hooki i automaty.

Oddanie i odinstalowanie nie kasują pliku, tylko zostawiają nagrobek ("brew" albo "none"): bez pliku Pod
przejąłby claude-acc znowu przy następnym starcie, a z nagrobkiem zostaje z boku i zdejmuje swoje agenty
i Pod Menu. Dla claude-acc nagrobek znaczy to samo co brak właściciela: brew i install.sh instalują.
"""

import json
import os
import sys
import time

STATE_DIR = os.path.join(os.path.expanduser("~"), ".local/share/claude-acc")
OWNER_PATH = os.path.join(STATE_DIR, "owner.json")
OWNERS = ("pod",)
# nagrobki: oddane Homebrew albo źródłom (handback) i odinstalowane, gdy należało do Pod
TOMBSTONES = ("brew", "none")
EXIT_OWNED = 3


def record():
    """owner.json, także nagrobek, albo None (brak pliku, zły JSON, nieznany właściciel)."""
    try:
        with open(OWNER_PATH) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("owner") not in OWNERS + TOMBSTONES:
        return None
    return data


def read():
    """Właściciel z owner.json albo None (brak pliku, zły JSON, nagrobek, nieznany: jak dotąd brew)."""
    data = record()
    return data if data and data["owner"] in OWNERS else None


def tombstone(kind):
    """Nagrobek zamiast pliku: Pod zostaje z boku, a wersja ostatniej instalacji zostaje w zapisie."""
    if kind not in TOMBSTONES:
        raise ValueError(f"nieznany nagrobek: {kind}")
    previous = record() or {}
    data = {"owner": kind, "version": previous.get("version"), "app": None, "at": round(time.time())}
    _save(data)
    return data


def write(owner, version, app=None, menu=None):
    if owner not in OWNERS:
        raise ValueError(f"nieznany właściciel: {owner}")
    data = {"owner": owner, "version": version, "app": app, "at": round(time.time())}
    if menu:
        data["menu"] = menu
    _save(data)
    return data


def _save(data):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = f"{OWNER_PATH}.tmp{os.getpid()}"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1)
        f.write("\n")
    os.replace(tmp, OWNER_PATH)
    return data


def menu_app():
    """Aplikacja paska menu tej instalacji: Pod Menu.app, którą prowadzi Pod, albo kopia setup.sh."""
    data = read() or {}
    menu = data.get("menu")
    if isinstance(menu, str) and menu and os.path.isdir(menu):
        return menu
    return os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(STATE_DIR))), "Applications", "Claude Acc.app")


def option(args, name):
    if name in args:
        i = args.index(name)
        if i + 1 < len(args):
            return args[i + 1]
    return None


def main(argv):
    cmd, args = (argv[0], argv[1:]) if argv else ("show", [])
    if cmd == "show":
        data = record()
        if "--json" in args:
            print(json.dumps(data or {"owner": None}))
        elif data and data["owner"] in OWNERS:
            print(f"claude-acc należy do {data['owner']} ({data.get('app') or '?'}, wersja {data.get('version') or '?'})")
        elif data:
            state = "oddany Homebrew albo źródłom" if data["owner"] == "brew" else "odinstalowany"
            print(f"claude-acc {state} (nagrobek {data['owner']!r} w owner.json): Pod zostaje z boku")
        else:
            print("claude-acc instaluje Homebrew albo install.sh (brak owner.json)")
        return 0
    if cmd == "check":
        data = read()
        caller = option(args, "--as")
        if data and data["owner"] != caller:
            name = data["owner"].capitalize()
            print(
                f"claude-acc należy teraz do {name} ({data.get('app') or OWNER_PATH}): {name} sam go instaluje i "
                f"aktualizuje. Ta instalacja niczego nie zmieniła. Oddanie z powrotem: "
                f"{sys.executable} {os.path.abspath(__file__)} clear, potem jeszcze raz.",
                file=sys.stderr,
            )
            return EXIT_OWNED
        return 0
    if cmd == "write":
        owner, version = option(args, "--owner"), option(args, "--version")
        if not owner or not version:
            print("użycie: owner.py write --owner pod --version X [--app PATH]", file=sys.stderr)
            return 2
        try:
            write(owner, version, option(args, "--app"), option(args, "--menu"))
        except ValueError as err:
            print(err, file=sys.stderr)
            return 2
        return 0
    if cmd == "menu":
        print(menu_app())
        return 0
    if cmd == "clear":
        tombstone("brew")
        print("claude-acc wraca do instalacji z Homebrew albo ze źródeł (claude-acc-setup); Pod zostaje z boku "
              "i zdejmuje swoje agenty (nagrobek \"brew\" w owner.json)")
        return 0
    if cmd == "uninstalled":
        if read():
            tombstone("none")
        return 0
    if cmd == "forget":
        try:
            os.remove(OWNER_PATH)
            print("owner.json usunięty: Pod przy następnym starcie zainstaluje claude-acc od nowa")
        except FileNotFoundError:
            print("owner.json nie ma")
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
