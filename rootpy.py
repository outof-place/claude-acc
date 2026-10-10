#!/usr/bin/env python3
"""Interpreter Pythona, który wolno uruchomić jako root.

  rootpy.py [--json]        ścieżka bezpiecznego interpretera albo kod 1 i powód na stderr

Skrypty claude-acc, które chodzą pod rootem (demon fseventsd, demon hotspotu), nie mogą startować
przez `/usr/bin/python3`: to zaślepka xcrun, która idzie za `/var/db/xcode_select_link` do wybranego
Xcode'a. Xcode zainstalowany z DMG należy do użytkownika, więc jego biblioteka standardowa jest
zapisywalna bez roota, a `import json` w procesie roota uruchomiłby kod podstawiony przez kogokolwiek
na tym koncie. To samo dotyczy interpreterów z Homebrew (`/opt/homebrew`) i z `$STATE`.

Bezpieczny interpreter to taki, którego plik, prawdziwa ścieżka, katalog biblioteki standardowej i
każdy katalog nadrzędny należą do roota i nie są zapisywalne dla grupy ani dla świata. Demony
uruchamiają go z `-I`: bez katalogu skryptu i PYTHONPATH w `sys.path`.
"""

import json
import os
import stat
import sys

# kolejność preferencji: narzędzia wiersza poleceń Apple (pakiet .pkg, właściciel root), potem
# framework systemowy, gdyby wrócił. `/usr/bin/python3` celowo nie jest kandydatem: to zaślepka
CANDIDATES = (
    "/Library/Developer/CommandLineTools/usr/bin/python3",
    "/System/Library/Frameworks/Python3.framework/Versions/Current/bin/python3",
)
OVERRIDE_ENV = "CLAUDE_ACC_ROOT_PYTHON"
WRITABLE = stat.S_IWGRP | stat.S_IWOTH


def _owned_by_root(path):
    """Powód, dla którego ścieżka nie jest bezpieczna, albo None: właściciel i prawa zapisu."""
    try:
        info = os.lstat(path)
    except OSError as err:
        return f"{path}: {err.strerror}"
    if info.st_uid != 0:
        return f"{path}: właściciel uid {info.st_uid}, nie root"
    if info.st_mode & WRITABLE:
        return (
            f"{path}: zapisywalny dla grupy albo świata ({stat.filemode(info.st_mode)})"
        )
    return None


def _chain(path):
    """Ścieżka i wszystkie jej katalogi nadrzędne, od korzenia."""
    path = os.path.abspath(path)
    found = [path]
    while True:
        parent = os.path.dirname(path)
        if parent == path:
            break
        found.append(parent)
        path = parent
    return list(reversed(found))


def unsafe_reason(path, probe=None):
    """Powód, dla którego tego interpretera nie wolno uruchomić jako root, albo None.

    Sprawdza sam plik, jego prawdziwą ścieżkę (dowiązania), a przede wszystkim to, co interpreter
    sam o sobie mówi: `sys.executable` i katalog biblioteki standardowej. Pyta o to jego samego,
    bo `/usr/bin/python3` to zaślepka: należy do roota i nie jest dowiązaniem, ale uruchamia
    interpreter z wybranego Xcode'a i jego bibliotekę standardową. Każdy z tych katalogów i każdy
    ich katalog nadrzędny musi należeć do roota. `probe` podstawia się w testach.
    """
    if not os.path.isabs(path):
        return f"{path}: ścieżka nie jest bezwzględna"
    if not os.access(path, os.X_OK):
        return f"{path}: nie da się uruchomić"
    targets = [path, os.path.realpath(path)]
    reported, why = (probe or _probe)(path)
    if why:
        return why
    targets += reported
    checked = set()
    for target in targets:
        for step in _chain(target):
            if step in checked:
                continue
            checked.add(step)
            reason = _owned_by_root(step)
            if reason:
                return reason
    return None


# o to pytamy kandydata: gdzie naprawdę jest on sam i jego biblioteka standardowa
_ASK = "import sys, sysconfig; print(sys.executable); print(sysconfig.get_paths()['stdlib'])"


def _probe(path):
    """([sys.executable, stdlib], None) kandydata albo ([], powód), gdy nie da się go o to spytać."""
    import subprocess

    try:
        done = subprocess.run([path, "-I", "-S", "-c", _ASK], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as err:
        return [], f"{path}: nie odpowiada ({err})"
    lines = [line.strip() for line in done.stdout.splitlines() if line.strip()]
    if done.returncode != 0 or len(lines) != 2 or not all(os.path.isabs(line) for line in lines):
        first = (done.stderr or done.stdout).strip().splitlines()
        return [], f"{path}: nie podał swoich ścieżek ({first[0] if first else 'bez wyjścia'})"[:200]
    return lines, None


def find(candidates=CANDIDATES, env=None):
    """(ścieżka, None) pierwszego bezpiecznego interpretera albo (None, powody wszystkich kandydatów).

    `CLAUDE_ACC_ROOT_PYTHON` wskazuje jednego kandydata i też przechodzi te same sprawdzenia.
    """
    env = os.environ if env is None else env
    pinned = env.get(OVERRIDE_ENV)
    checked = (pinned,) if pinned else tuple(candidates)
    reasons = []
    for path in checked:
        reason = unsafe_reason(path)
        if reason is None:
            return path, None
        reasons.append(reason if reason.startswith(path) else f"{path}: {reason}")
    return None, "; ".join(reasons)


def main(argv):
    path, why = find()
    if "--json" in argv:
        print(json.dumps({"python": path, "why": why}))
        return 0 if path else 1
    if path:
        print(path)
        return 0
    print(
        "brak interpretera Pythona, który wolno uruchomić jako root: "
        + (why or "?")
        + "\n"
        "Zainstaluj narzędzia wiersza poleceń Apple (xcode-select --install) albo wskaż własny przez "
        f"{OVERRIDE_ENV}. /usr/bin/python3 nie wchodzi w grę: to zaślepka, która idzie do wybranego Xcode'a.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
