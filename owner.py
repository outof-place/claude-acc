#!/usr/bin/env python3
"""Kto instaluje i aktualizuje claude-acc na tym koncie: `$STATE/owner.json`.

  owner.py show [--json]
  owner.py check [--as pod]      kod 3, gdy claude-acc należy do Pod, a woła ktoś inny (brew, install.sh)
  owner.py write --owner pod --version X [--app PATH]
  owner.py clear                 oddanie claude-acc z powrotem instalacji z Homebrew albo ze źródeł

Pliku nie ma: claude-acc instaluje brew (`claude-acc-setup`) albo install.sh, jak dotąd. Gdy Pod
przejmie claude-acc, sam uruchamia setup.sh ze swojej paczki (`--owner pod`) i zapisuje
{"owner": "pod", "version", "app", "at"}; od tej chwili setup.sh z Homebrew odmawia, bo dwa źródła
na zmianę nadpisywałyby sobie skrypty, hooki i automaty.
"""

import json
import os
import sys
import time

STATE_DIR = os.path.join(os.path.expanduser("~"), ".local/share/claude-acc")
OWNER_PATH = os.path.join(STATE_DIR, "owner.json")
OWNERS = ("pod",)
EXIT_OWNED = 3


def read():
    """Zawartość owner.json albo None (brak pliku, zły JSON, nieznany właściciel: jak dotąd brew)."""
    try:
        with open(OWNER_PATH) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("owner") not in OWNERS:
        return None
    return data


def write(owner, version, app=None):
    if owner not in OWNERS:
        raise ValueError(f"nieznany właściciel: {owner}")
    os.makedirs(STATE_DIR, exist_ok=True)
    data = {"owner": owner, "version": version, "app": app, "at": round(time.time())}
    tmp = f"{OWNER_PATH}.tmp{os.getpid()}"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1)
        f.write("\n")
    os.replace(tmp, OWNER_PATH)
    return data


def option(args, name):
    if name in args:
        i = args.index(name)
        if i + 1 < len(args):
            return args[i + 1]
    return None


def main(argv):
    cmd, args = (argv[0], argv[1:]) if argv else ("show", [])
    if cmd == "show":
        data = read()
        if "--json" in args:
            print(json.dumps(data or {"owner": None}))
        elif data:
            print(f"claude-acc należy do {data['owner']} ({data.get('app') or '?'}, wersja {data.get('version') or '?'})")
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
            write(owner, version, option(args, "--app"))
        except ValueError as err:
            print(err, file=sys.stderr)
            return 2
        return 0
    if cmd == "clear":
        try:
            os.remove(OWNER_PATH)
            print("owner.json usunięty: claude-acc wraca do instalacji z Homebrew albo ze źródeł (claude-acc-setup)")
        except FileNotFoundError:
            print("owner.json nie ma")
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
