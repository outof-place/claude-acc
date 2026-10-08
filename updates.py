#!/usr/bin/env python3
"""Aktualizacje wszystkiego na Macu co 3 dni: Homebrew, npm, Go, Python, Claude Code.

launchd uruchamia `updates.py run` codziennie o 4:30 (uśpiony Mac nadrabia po obudzeniu);
przebieg rusza, gdy od poprzedniego minęły 3 dni, a po nieudanym próbuje znowu następnej
nocy. Przycisk Update w panelu i `claude-acc update` uruchamiają go od razu.

Co aktualizuje, do najnowszej wersji:
- Homebrew: `brew update`, potem formuły i aplikacje (casks). Bez --greedy: aplikacje
  z własnym aktualizatorem (Chrome, Claude) robią to same. Przypięte (`brew pin`) zostają;
- npm: globalne paczki, także o wersję główną wyżej. `npm_pins` w updates.json trzyma
  paczkę w jednej wersji głównej, np. {"pnpm": "11"};
- Go: programy postawione przez `go install` w GOBIN;
- Python: paczki pip w każdym Pythonie z PYTHONS (domyślny od uv, python.org, Homebrew). Dzielą
  zależności, więc idą razem: jedno `pip install -U --upgrade-strategy eager` po paczkach, których
  nic nie wymaga, i tylko z wheeli (paczka bez wheela, np. llama-cpp-python, zostaje, jaka jest).
  Przed i po: `pip check` i import każdej paczki, której nic nie wymaga. Nowa niezgodność albo paczka,
  która przestała się importować, cofa tego Pythona do wersji sprzed przebiegu. Paczka, której
  resolver nie podbił, bo inna trzyma ją niżej, jest przytrzymana, nie nieudana; `pip_pins` trzyma
  paczkę przy specyfikatorze ({"fb-idb": "==1.1.7"}). Nowy playwright dostaje swoje przeglądarki.
  Nowszą wersję poprawkową Pythona z python.org automat pobiera i sprawdza podpis, a instaluje
  człowiek przyciskiem w panelu (hasło administratora). Pythony z uv idą `uv python upgrade`;
- Claude Code: `claude update`, katalogi wtyczek, wtyczki w każdym zakresie (user, project,
  local; projekty, których już nie ma, są pomijane), skille z `npx skills`. Skill poprawiony
  ręcznie zostaje, a wtyczka, której aktualizacja chce uruchomić polecenie z katalogu, czeka
  na potwierdzenie człowieka.

Każda paczka idzie osobno albo jest sprawdzana po fakcie, więc jedna nieudana (np. aplikacja,
która chce hasła administratora) nie zatrzymuje reszty i w panelu widać, która to była.
Stan dla panelu: updates-state.json; pełne wyjście poleceń: updates.log.

Komendy:
  run [--force] [--dry-run] [--only brew,npm,go,pip,claude]
        --force pomija odstęp 3 dni (przycisk w panelu), --dry-run tylko wypisuje plan,
        --only zawęża przebieg i nie przesuwa terminu następnego
  status [--json]
"""

import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

HOME = os.path.expanduser("~")
STATE_DIR = os.path.join(HOME, ".local/share/claude-acc")
CONFIG_PATH = os.path.join(STATE_DIR, "updates.json")
STATE_PATH = os.path.join(STATE_DIR, "updates-state.json")
LOG_PATH = os.path.join(STATE_DIR, "updates.log")
LOCK_PATH = os.path.join(STATE_DIR, "updates.lock")
LOG_MAX_BYTES = 1024 * 1024

HOUR = 3600
DAY = 24 * HOUR
# godzina z launchd/com.filip.claude-acc.updates.plist.template; stąd panel zna termin następnego
RUN_AT = (4, 30)

DEFAULT_CONFIG = {
    "every_days": 3,
    # po nieudanym przebiegu: następnej nocy, nie za 3 dni
    "retry_hours": 20,
    # kroki do pominięcia: brew, npm, go, pip, claude
    "skip": [],
    # paczka npm -> zakres, w którym ma zostać ("11" to każda 11.x)
    "npm_pins": {},
    # paczka pip -> specyfikator, przy którym ma zostać ("==1.1.7", "<2")
    "pip_pins": {},
    # Python (ścieżka albo lista), którego paczki aktualizować; domyślnie PYTHONS
    "python": None,
}


def tool_path():
    """PATH dla narzędzi: launchd daje procesowi tylko /usr/bin:/bin:/usr/sbin:/sbin."""
    if os.environ.get("CLAUDE_ACC_TOOL_PATH"):  # testy: atrapy brew, npm, go
        return os.environ["CLAUDE_ACC_TOOL_PATH"]
    # ~/.local/bin pierwszy, jak w powłoce: tam jest natywny Claude Code, a starsze kopie z npm
    # w /usr/local/bin albo /opt/homebrew/bin przy `claude update` przestawiają jego konfigurację
    dirs = [os.path.join(HOME, ".local/bin"), "/opt/homebrew/bin", "/opt/homebrew/sbin", "/usr/local/bin",
            "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    return ":".join(d for d in dirs if os.path.isdir(d))


ENV = dict(
    os.environ,
    PATH=tool_path(),
    HOMEBREW_NO_AUTO_UPDATE="1",
    HOMEBREW_NO_ENV_HINTS="1",
    HOMEBREW_NO_ANALYTICS="1",
    npm_config_update_notifier="false",
    npm_config_fund="false",
    npm_config_audit="false",
    # Homebrew i uv znaczą swoje Pythony jako zarządzane z zewnątrz (PEP 668), a paczki w nich i tak
    # stawia człowiek. Zmienna, nie flaga: pip sprzed 23.0 nie zna flagi, a nieznaną zmienną pominie.
    PIP_BREAK_SYSTEM_PACKAGES="1",
    PIP_DISABLE_PIP_VERSION_CHECK="1",
    PIP_NO_INPUT="1",
)


# ---------- drobne narzędzia ----------


def log(line):
    os.makedirs(STATE_DIR, exist_ok=True)
    if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > LOG_MAX_BYTES:
        with open(LOG_PATH) as f:
            tail = f.readlines()[-3000:]
        with open(LOG_PATH, "w") as f:
            f.writelines(tail)
    with open(LOG_PATH, "a") as f:
        f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}  {line}\n")


def notify(title, text):
    subprocess.run(
        ["osascript", "-e", f"display notification {json.dumps(text)} with title {json.dumps(title)}"],
        capture_output=True,
        env=ENV,
    )


def write_json(path, data):
    """Zapis przez plik tymczasowy: przerwany proces nie zostawi uciętego JSON-a."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(load_json(CONFIG_PATH, {}))
    return cfg


def which(name):
    return shutil.which(name, path=ENV["PATH"])


# npm pisze kilka linii "npm error"; kod i ścieżka do logu nic nie mówią człowiekowi
NOISE = re.compile(r"npm (error|ERR!) (code|errno|syscall|A complete log|Log files)|npm (error|ERR!)\s*$")
ERROR = re.compile(r"^(Error|npm error|npm ERR!|go: |fatal|ERROR)")


def error_line(text):
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for line in lines:
        if ERROR.match(line) and not NOISE.match(line):
            return line[:300]
    return (lines[-1] if lines else "unknown error")[:300]


def call(cmd, timeout=600, codes=(0,), quiet=False, merge=False, cwd=HOME, env=None):
    """(udało się, stdout, opis błędu); z merge=True drugie pole to stdout i stderr razem.
    Polecenia, które coś zmieniają, trafiają do logu w całości."""
    try:
        done = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, env=env or ENV, cwd=cwd, stdin=subprocess.DEVNULL
        )
    except subprocess.TimeoutExpired:
        log(f"$ {' '.join(cmd)}: przekroczony czas {timeout} s")
        return False, "", f"timed out after {timeout // 60} min"
    except OSError as err:
        return False, "", str(err)
    ok = done.returncode in codes
    if not quiet or not ok:
        output = (done.stdout + done.stderr).strip()
        log(f"$ {' '.join(cmd)}  (kod {done.returncode})" + (f"\n{output}" if output else ""))
    out = done.stdout + done.stderr if merge else done.stdout
    return ok, out, "" if ok else error_line(done.stderr + "\n" + done.stdout)


def version_key(version):
    """1.10.2 > 1.9.9; v-przedrostek i część po myślniku nie liczą się."""
    return [int(n) for n in re.findall(r"\d+", version.lstrip("v").split("-")[0])[:3]]


class StepError(Exception):
    pass


class Step:
    def __init__(self, name, label):
        self.name, self.label = name, label
        self.updated, self.failed, self.held = [], [], []
        self.error = None

    def to_json(self):
        data = {
            "name": self.name,
            "label": self.label,
            "ok": self.error is None and not self.failed,
            "updated": self.updated,
            "failed": self.failed,
            "held": self.held,
        }
        if self.error:
            data["error"] = self.error
        return data


def pkg(name, old, new, **extra):
    return {"name": name, "from": old, "to": new, **extra}


# ---------- Homebrew ----------


def brew_outdated():
    ok, out, err = call(["brew", "outdated", "--json=v2"], timeout=300, quiet=True)
    if not ok:
        raise StepError(f"brew outdated: {err}")
    try:
        data = json.loads(out[out.find("{"):])
    except ValueError:
        raise StepError("brew outdated: unreadable output")
    found = {}
    for kind in ("formulae", "casks"):
        for item in data.get(kind, []):
            found[item["name"]] = {
                "kind": kind,
                "from": (item.get("installed_versions") or ["?"])[-1],
                "to": item.get("current_version") or "?",
                "pinned": bool(item.get("pinned")),
            }
    return found


def step_brew(st, cfg, dry):
    if not which("brew"):
        return False
    ok, _, err = call(["brew", "update", "--quiet"], timeout=900)
    if not ok:
        st.error = f"brew update: {err}"
        return True
    before = brew_outdated()
    short = lambda name: name.rsplit("/", 1)[-1]  # facebook/fb/idb-companion
    st.held = [pkg(short(n), i["from"], i["to"], why="pin") for n, i in before.items() if i["pinned"]]
    todo = {n: i for n, i in before.items() if not i["pinned"]}
    if dry:
        st.updated = [pkg(short(n), i["from"], i["to"]) for n, i in todo.items()]
        return True
    errors = {}
    formulae = [n for n, i in todo.items() if i["kind"] == "formulae"]
    if formulae:
        ok, _, err = call(["brew", "upgrade", "--formula", *formulae], timeout=2 * HOUR)
        errors.update({n: err for n in formulae if not ok})
    # aplikacje po jednej: ta, która chce hasła administratora, nie blokuje reszty
    for name in [n for n, i in todo.items() if i["kind"] == "casks"]:
        ok, _, err = call(["brew", "upgrade", "--cask", name], timeout=HOUR)
        if not ok:
            errors[name] = err
    try:
        after = brew_outdated()
    except StepError:
        after = errors  # bez drugiego odczytu wierzymy kodom wyjścia
    for name, item in todo.items():
        if name in after:
            error = errors.get(name) or "still outdated after brew upgrade"
            entry = pkg(short(name), item["from"], item["to"], error=error)
            # instalator aplikacji chce hasła administratora, a w tle nie ma kogo zapytać
            if "sudo" in error:
                kind = "cask" if item["kind"] == "casks" else "formula"
                entry.update(admin=True, retry=f"brew upgrade --{kind} {name}")
            st.failed.append(entry)
        else:
            st.updated.append(pkg(short(name), item["from"], item["to"]))
    return True


# ---------- npm ----------


def npm_outdated():
    # npm outdated kończy się kodem 1, gdy jest co aktualizować
    ok, out, err = call(["npm", "outdated", "-g", "--json"], timeout=300, codes=(0, 1), quiet=True)
    try:
        data = json.loads(out or "{}")
    except ValueError:
        raise StepError(f"npm outdated: {err or 'unreadable output'}")
    if not ok or isinstance(data.get("error"), dict):
        detail = data.get("error", {}).get("summary") if isinstance(data.get("error"), dict) else err
        raise StepError(f"npm outdated: {detail}")
    return {n: i for n, i in data.items() if isinstance(i, dict) and i.get("current") and i.get("latest")}


def npm_newest(name, pin):
    """Najwyższa wersja w zakresie przypięcia albo None."""
    ok, out, _ = call(["npm", "view", f"{name}@{pin}", "version", "--json"], timeout=120, quiet=True)
    try:
        versions = json.loads(out) if ok and out.strip() else []
    except ValueError:
        return None
    versions = [versions] if isinstance(versions, str) else versions
    return max(versions, key=version_key) if versions else None


# npm 12 nie uruchamia skryptów instalacyjnych zależności spoza allowScripts; paczka, która ich
# potrzebuje (np. pobiera binarkę), instaluje się bez błędu, a potem nie działa
BLOCKED = re.compile(r"npm warn install-scripts\s+(\S+?)@\S+ \(")


def npm_commands(prefix, name):
    """Komendy paczki z pola bin jej package.json, jako pełne ścieżki w katalogu bin npm."""
    try:
        with open(os.path.join(prefix, "lib/node_modules", name, "package.json")) as f:
            bins = json.load(f).get("bin") or {}
    except (OSError, ValueError):
        return []
    if isinstance(bins, str):
        bins = {name.split("/")[-1]: bins}
    return [os.path.join(prefix, "bin", b) for b in sorted(bins)]


def working(commands):
    """Komendy, które odpowiadają na --version."""
    alive = set()
    for command in commands:
        try:
            done = subprocess.run([command, "--version"], capture_output=True, timeout=20, env=ENV, cwd=HOME,
                                  stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            continue
        if done.returncode == 0:
            alive.add(os.path.basename(command))
    return alive


def npm_install(name, old, new, prefix):
    """Instaluje i sprawdza, czy komendy paczki dalej działają; jeśli któraś przestała, wraca stara
    wersja. Zwraca (błąd albo None, komenda do ponowienia albo None)."""
    commands = npm_commands(prefix, name) if prefix else []
    alive = working(commands)
    ok, out, err = call(["npm", "install", "-g", f"{name}@{new}"], timeout=1200, merge=True)
    if not ok:
        return err, None
    broken = alive - working(npm_commands(prefix, name) if prefix else [])
    if not broken:
        return None, None
    call(["npm", "install", "-g", f"{name}@{old}"], timeout=1200)
    blocked = sorted(set(BLOCKED.findall(out)))
    error = f"{', '.join(sorted(broken))} stopped working after {new}, rolled back"
    if blocked:
        error += f"; npm blocked install scripts of {', '.join(blocked)}"
        return error, f"npm install -g --allow-scripts={','.join(blocked)} {name}@{new}"
    return error, None


def step_npm(st, cfg, dry):
    if not which("npm"):
        return False
    before = npm_outdated()
    pins = cfg.get("npm_pins") or {}
    todo = []
    # npm na końcu: podmienia samego siebie
    for name, info in sorted(before.items(), key=lambda kv: (kv[0] == "npm", kv[0])):
        target = info["latest"]
        if name in pins:
            target = npm_newest(name, pins[name]) or info["current"]
            if target != info["latest"]:
                st.held.append(pkg(name, target, info["latest"], why="pin", pin=pins[name]))
        if target != info["current"]:
            todo.append((name, info["current"], target))
    if dry:
        st.updated = [pkg(n, old, new) for n, old, new in todo]
        return True
    ok, out, _ = call(["npm", "prefix", "-g"], timeout=60, quiet=True)
    prefix = out.strip() if ok and out.strip() else None
    errors, retries = {}, {}
    for name, old, new in todo:
        error, retry = npm_install(name, old, new, prefix)
        if error:
            errors[name] = error
        if retry:
            retries[name] = retry
    try:
        after = npm_outdated() if todo else {}
    except StepError:
        after = None
    for name, old, new in todo:
        if after is None:
            now = old if name in errors else new
        else:  # nieobecna na liście = najnowsza
            now = after[name]["current"] if name in after else before[name]["latest"]
        if now == new:
            st.updated.append(pkg(name, old, new))
        else:
            entry = pkg(name, old, new, error=errors.get(name) or f"still {now} after npm install")
            if name in retries:
                entry["retry"] = retries[name]
            st.failed.append(entry)
    return True


# ---------- Go ----------


def go_bin_dir():
    ok, out, _ = call(["go", "env", "GOBIN", "GOPATH"], quiet=True)
    if not ok:
        return None
    gobin, gopath = (out.splitlines() + ["", ""])[:2]
    return gobin.strip() or os.path.join(gopath.strip().split(":")[0], "bin")


def go_binary(path):
    """Ścieżka pakietu, moduł i wersja z `go version -m`; None dla plików spoza `go install`."""
    ok, out, _ = call(["go", "version", "-m", path], quiet=True)
    if not ok:
        return None
    fields = {}
    for line in out.splitlines():
        parts = line.strip().split("\t")
        if parts[0] in ("path", "mod") and len(parts) > 1:
            fields.setdefault(parts[0], parts[1:])
    mod = fields.get("mod", [])
    if "path" not in fields or len(mod) < 2 or mod[1] == "(devel)":
        return None
    return {"path": fields["path"][0], "module": mod[0], "version": mod[1]}


def step_go(st, cfg, dry):
    if not which("go"):
        return False
    root = go_bin_dir()
    if not root or not os.path.isdir(root):
        return False
    todo = []
    for name in sorted(os.listdir(root)):
        path = os.path.join(root, name)
        if not (os.path.isfile(path) and os.access(path, os.X_OK)):
            continue
        info = go_binary(path)
        if not info:
            continue
        ok, out, err = call(["go", "list", "-m", "-json", f"{info['module']}@latest"], timeout=120, quiet=True)
        try:
            latest = json.loads(out)["Version"] if ok else None
        except (ValueError, KeyError):
            latest, err = None, "unreadable go list output"
        if not latest:
            st.failed.append(pkg(name, info["version"], "?", error=f"couldn't check: {err}"))
        elif latest != info["version"]:
            todo.append((name, path, info, latest))
    for name, path, info, latest in todo:
        if dry:
            st.updated.append(pkg(name, info["version"], latest))
            continue
        ok, _, err = call(["go", "install", f"{info['path']}@{latest}"], timeout=1800)
        now = (go_binary(path) or {}).get("version")
        if now == latest:
            st.updated.append(pkg(name, info["version"], latest))
        else:
            st.failed.append(pkg(name, info["version"], latest, error=err or f"still {now} after go install"))
    return True


# ---------- Python ----------

# Pythony z paczkami stawianymi ręcznie: domyślny python3 od uv (`uv python install --default`),
# python.org i Homebrew. Nie /usr/bin/python3: bez narzędzi Xcode wyskakuje okno instalatora.
PYTHONS = (
    os.path.join(HOME, ".local/bin/python3"),
    "/Library/Frameworks/Python.framework/Versions/Current/bin/python3",
    "/opt/homebrew/bin/python3",
)

# Wersja, prefiks (po nim odpada drugi link do tego samego Pythona), user site i paczki spoza indeksu
# (katalog, git, -e): `pip install -U nazwa` podmieniłby je obcą paczką o tej nazwie z PyPI.
PYTHON_INFO = """
import json, site, sys
from importlib import metadata
local = [d.metadata["Name"] for d in metadata.distributions() if d.read_text("direct_url.json")]
print(json.dumps({"version": "%d.%d" % sys.version_info[:2], "patch": "%d.%d.%d" % sys.version_info[:3],
                  "prefix": sys.prefix,
                  "user_site": site.getusersitepackages(), "local": [n for n in local if n]}))
"""

# uruchamiane w docelowym Pythonie: moduły dystrybucji z argv[1] i te, których nie da się zaimportować
IMPORT_CHECK = r"""
import importlib, importlib.metadata as md, json, re, sys
norm = lambda n: re.sub(r"[-_.]+", "-", n).lower()
dist = norm(sys.argv[1])
skip = {"test", "tests", "docs", "doc", "examples", "benchmarks"}
mods = sorted(m for m, ds in md.packages_distributions().items()
              if any(norm(d) == dist for d in ds) and m.isidentifier() and not m.startswith("_") and m not in skip)
failed = []
for m in mods:
    try:
        importlib.import_module(m)
    except BaseException as e:
        failed.append(f"{m}: {type(e).__name__}: {e}"[:200])
print(json.dumps({"modules": mods, "failed": failed}))
"""
PLAYWRIGHT_CACHE = os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or os.path.join(HOME, "Library/Caches/ms-playwright")
PYTHON_RELEASES = os.environ.get(
    "CLAUDE_ACC_PYTHON_RELEASES", "https://www.python.org/api/v2/downloads/release/?is_published=true"
)
PYTHON_FTP = os.environ.get("CLAUDE_ACC_PYTHON_FTP", "https://www.python.org/ftp/python")
DOWNLOADS = os.path.join(STATE_DIR, "downloads")


# "a 1.0 has requirement b<2, but you have b 2.1." albo "a 1.0 requires b, which is not installed."
PIP_CHECK = re.compile(r"^(\S+) \S+ (?:has requirement|requires) ([A-Za-z0-9._-]+)")


def canonical(name):
    """Nazwa paczki według PEP 503: typing_extensions i typing-extensions to jedna paczka."""
    return re.sub(r"[-_.]+", "-", name).lower()


def pip_list(python, *flags):
    """Paczki po nazwie kanonicznej; -v dokłada katalog, w którym każda leży."""
    ok, out, err = call([python, "-m", "pip", "list", "-v", "--format=json", *flags], timeout=600, quiet=True)
    # -v kładzie na stdout także log pip („Link requires a different Python”); JSON to jedna linia
    lines = [line for line in out.splitlines() if line.startswith("[")]
    try:
        items = json.loads(lines[-1]) if ok and lines else None
    except ValueError:
        items, err = None, "unreadable pip output"
    if items is None:
        raise StepError(f"pip list: {err}")
    return {canonical(i["name"]): i for i in items}


def pip_check(python):
    """Niezgodności jako pary (paczka, zależność), bo tekst zmienia się razem z wersjami."""
    _, out, _ = call([python, "-m", "pip", "check"], timeout=300, codes=(0, 1), quiet=True)
    found = {}
    for line in out.splitlines():
        match = PIP_CHECK.match(line.strip())
        if match:
            found[(canonical(match.group(1)), canonical(match.group(2)))] = line.strip()
    return found


def pip_install(python, args, user):
    flags = ["--progress-bar", "off"] + (["--user"] if user else [])
    return call([python, "-m", "pip", "install", *flags, *args], timeout=2 * HOUR)


def pip_restore(python, before, after, in_user):
    """Stan sprzed przebiegu: nowe paczki i kopie postawione w user site znikają, podbite wracają."""
    added = {n for n in after if n not in before or after[n]["location"] != before[n]["location"]}
    if added:
        call([python, "-m", "pip", "uninstall", "-y", *(after[n]["name"] for n in sorted(added))], timeout=HOUR)
    back = [n for n in before if n not in added and (n not in after or after[n]["version"] != before[n]["version"])]
    for user in (False, True):
        pins = [f"{before[n]['name']}=={before[n]['version']}" for n in back if in_user(n) == user]
        if pins:
            pip_install(python, ["--no-deps", *pins], user)


def import_failures(python, dists):
    """Dystrybucje, których moduły nie dają się zaimportować; każda w osobnym procesie, bo zepsuta
    biblioteka natywna potrafi zabić cały interpreter."""

    def check(dist):
        try:
            done = subprocess.run([python, "-c", IMPORT_CHECK, dist], capture_output=True, text=True,
                                  timeout=300, env=ENV, cwd=HOME, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            return dist, "import timed out"
        if done.returncode != 0:
            return dist, error_line(done.stderr) or f"exit {done.returncode}"
        try:
            failed = json.loads(done.stdout.strip().splitlines()[-1])["failed"]
        except (ValueError, IndexError, KeyError):
            return dist, "unreadable import check"
        return dist, "; ".join(failed) or None

    with ThreadPoolExecutor(max_workers=4) as pool:
        return {d: why for d, why in pool.map(check, dists) if why}


def upgrade_python(st, python, info, pins, dry):
    label = info["label"]
    local = {canonical(n) for n in info["local"]}
    outdated = {n: i for n, i in pip_list(python, "--outdated").items() if n not in local}
    pinned = lambda n, now: pkg(outdated[n]["name"], now, outdated[n]["latest_version"], python=label, why="pin")
    if dry:
        st.updated += [pkg(i["name"], i["version"], i["latest_version"], python=label)
                       for n, i in outdated.items() if n not in pins]
        st.held += [pinned(n, i["version"]) for n, i in outdated.items() if n in pins]
        return
    if not set(outdated) - set(pins):
        st.held += [pinned(n, i["version"]) for n, i in outdated.items()]
        return
    before = pip_list(python)
    user_site = os.path.realpath(info["user_site"])
    in_user = lambda n: os.path.realpath(before[n]["location"]) == user_site
    # paczki, których nic nie wymaga, ciągną resztę: resolver widzi wtedy wszystkie ograniczenia naraz;
    # przypięta idzie ze specyfikatorem, także jako zależność innej, bo inaczej eager by ją podbił
    top = set(pip_list(python, "--not-required"))
    todo = sorted((top | set(outdated)) - local)
    broken = pip_check(python)
    # zepsuta biblioteka natywna przechodzi pip check, a nie daje się zaimportować
    importable = sorted(before[n]["name"] for n in top if n in before)
    unimportable = import_failures(python, importable)
    errors = {}
    # najpierw katalog Pythona, potem user site, do którego --user kładzie też nowsze zależności z dołu
    for user in (False, True):
        names = [before[n]["name"] + pins.get(n, "") for n in todo if n in before and in_user(n) == user]
        if names:
            upgrade = ["--upgrade", "--upgrade-strategy", "eager", "--only-binary", ":all:", *names]
            ok, _, err = pip_install(python, upgrade, user)
            if not ok:
                errors[user] = err
    after = pip_list(python)
    bumped = [n for n in before if n in after and after[n]["version"] != before[n]["version"]]
    new = [line for key, line in pip_check(python).items() if key not in broken]
    stopped = sorted((d, why) for d, why in import_failures(python, importable).items() if d not in unimportable)
    if new or stopped:
        pip_restore(python, before, after, in_user)
        error = f"rolled back, pip check: {new[0]}" if new else f"rolled back, {stopped[0][0]} stopped importing: {stopped[0][1]}"
        st.failed += [
            pkg(before[n]["name"], before[n]["version"], after[n]["version"], python=label, error=error) for n in bumped
        ] or [pkg(f"Python {label}", "?", "?", error=error)]
        return
    st.updated += [pkg(after[n]["name"], before[n]["version"], after[n]["version"], python=label) for n in bumped]
    for n, item in outdated.items():
        now = after[n]["version"] if n in after else item["version"]
        if n not in before or now == item["latest_version"]:
            continue
        entry = pkg(item["name"], now, item["latest_version"], python=label)
        if n in pins:
            st.held.append(dict(entry, why="pin"))
        elif in_user(n) in errors:
            st.failed.append(dict(entry, error=errors[in_user(n)]))
        else:  # resolver trzyma ją niżej dla innej paczki albo nowsza nie ma wheela
            st.held.append(dict(entry, why="deps"))
    if "playwright" in bumped:
        playwright_browsers(st, python, label, before["playwright"]["version"], after["playwright"]["version"])


def playwright_browsers(st, python, label, old, new):
    """Nowy playwright szuka przeglądarek w nowych wersjach: bez ich pobrania skrypty padają."""
    if not os.path.isdir(PLAYWRIGHT_CACHE):
        return
    present = os.listdir(PLAYWRIGHT_CACHE)
    browsers = [b for b in ("chromium", "firefox", "webkit") if any(d.startswith(b) for d in present)]
    if not browsers:
        return
    ok, _, err = call([python, "-m", "playwright", "install", *browsers], timeout=1800)
    if not ok:
        st.failed.append(pkg("Playwright browsers", old, new, python=label, error=err,
                             retry=f"{python} -m playwright install {' '.join(browsers)}"))


def fetch(url, timeout=60):
    ok, out, err = call(["curl", "-fsSL", "--max-time", str(timeout), url], timeout=timeout + 10, quiet=True)
    if not ok:
        raise StepError(f"{url}: {err}")
    return out


def python_org(st, info, dry):
    """python.org nie aktualizuje się bez hasła administratora: automat pobiera i sprawdza instalator
    nowszej wersji poprawkowej, a panel daje przycisk, który go otwiera."""
    if not info["prefix"].startswith("/Library/Frameworks/Python.framework/"):
        return
    current, minor = info["patch"], info["version"]
    try:
        releases = json.loads(fetch(PYTHON_RELEASES))
    except (StepError, ValueError) as err:
        st.failed.append(pkg("Python", current, "?", error=f"couldn't check python.org: {err}"))
        return
    found = [r["name"].split()[1] for r in releases if re.fullmatch(rf"Python {re.escape(minor)}\.\d+", r.get("name", ""))]
    latest = max(found, key=version_key, default=current)
    if version_key(latest) <= version_key(current):
        return
    if dry:
        st.held.append(pkg("Python", current, latest, why="install"))
        return
    os.makedirs(DOWNLOADS, exist_ok=True)
    path = os.path.join(DOWNLOADS, f"python-{latest}-macos11.pkg")
    for old in os.listdir(DOWNLOADS):
        if old.startswith("python-") and old.endswith(".pkg") and old != os.path.basename(path):
            os.remove(os.path.join(DOWNLOADS, old))
    if not os.path.exists(path):
        ok, _, err = call(["curl", "-fsSL", "--max-time", "1200", "-o", f"{path}.part",
                           f"{PYTHON_FTP}/{latest}/python-{latest}-macos11.pkg"], timeout=1260, quiet=True)
        if not ok:
            st.failed.append(pkg("Python", current, latest, error=f"download: {err}"))
            return
        os.replace(f"{path}.part", path)
    ok, out, err = call(["pkgutil", "--check-signature", path], quiet=True)
    if not ok or "Developer ID Installer: Python Software Foundation" not in out:
        os.remove(path)
        st.failed.append(pkg("Python", current, latest, error=f"installer signature check failed: {err or out[:200]}"))
        return
    st.held.append(pkg("Python", current, latest, why="install", installer=path))


def uv_pythons():
    """Pythony z uv: wersja minor -> (najnowsza poprawka, katalogi jej instalacji)."""
    ok, out, _ = call(["uv", "python", "list", "--only-installed", "--managed-python", "--output-format", "json"],
                      timeout=60, quiet=True)
    found = {}
    try:
        for p in json.loads(out) if ok else []:
            minor = ".".join(p["version"].split(".")[:2])
            newest, homes = found.get(minor, ("0", set()))
            homes.add(os.path.dirname(os.path.dirname(os.path.realpath(p["path"]))))
            found[minor] = (max(newest, p["version"], key=version_key), homes)
    except (ValueError, KeyError, TypeError):
        pass
    return found


def uv_python_upgrade(st, used, dry):
    """Pythony z uv (projekty, uvx) do najnowszej wersji poprawkowej. Nowa poprawka to nowy katalog,
    a paczki pip postawione w starym zostałyby w nim, więc Pythona, którego paczki aktualizuje ten
    krok (np. domyślny python3 od uv), automat nie przestawia."""
    before = uv_pythons()
    kept = sorted(m for m, (_, homes) in before.items() if homes & used)
    st.held += [pkg(f"Python {m} (uv)", before[m][0], None, why="packages") for m in kept]
    todo = sorted(m for m in before if m not in kept)
    if not todo or dry:
        return
    ok, _, err = call(["uv", "python", "upgrade", *todo], timeout=1200)
    after = uv_pythons()
    for minor in todo:
        old, new = before[minor][0], after.get(minor, before[minor])[0]
        if new != old:
            st.updated.append(pkg(f"Python {minor} (uv)", old, new))
    if not ok:
        st.failed.append(pkg("uv Pythons", None, None, error=err))


def step_pip(st, cfg, dry):
    pythons = cfg.get("python") or PYTHONS
    pins = {canonical(n): spec for n, spec in (cfg.get("pip_pins") or {}).items()}
    seen = set()
    for python in [pythons] if isinstance(pythons, str) else pythons:
        if not os.access(python, os.X_OK):
            continue
        ok, out, err = call([python, "-c", PYTHON_INFO], timeout=120, quiet=True)
        try:
            info = json.loads(out) if ok else None
        except ValueError:
            info, err = None, "unreadable output"
        if info is None:
            st.failed.append(pkg(python, "?", "?", error=f"couldn't start: {err}"))
            continue
        if info["prefix"] in seen:
            continue
        seen.add(info["prefix"])
        # python.org i Homebrew bywają w tej samej wersji: w panelu pypdf (3.14) byłby dwa razy
        where = " Homebrew" if info["prefix"].startswith("/opt/homebrew/") else " uv" if "/uv/python/" in info["prefix"] else ""
        info["label"] = info["version"] + where
        # jeden zepsuty Python nie zatrzymuje pozostałych
        try:
            upgrade_python(st, python, info, pins, dry)
        except StepError as err:
            st.failed.append(pkg(f"Python {info['label']}", "?", "?", error=str(err)))
        python_org(st, info, dry)
    if which("uv"):
        uv_python_upgrade(st, {os.path.realpath(p) for p in seen}, dry)
    return bool(seen or st.failed or st.updated or st.held)


# ---------- Claude Code ----------

SKILLS_DIR = os.path.join(HOME, ".agents/skills")
SKILLS_LOCK = os.path.join(HOME, ".agents/.skill-lock.json")
# Claude Code ucina klon wtyczki po 120 s; updater działa w tle, przy terminalu nikt nie czeka
PLUGIN_GIT_TIMEOUT = 600


def claude_version():
    ok, out, _ = call(["claude", "--version"], timeout=60, quiet=True)
    return out.split()[0] if ok and out.strip() else None


def plugin_installs():
    """Instalacje wtyczek, których projekt jeszcze istnieje; reszta to ślady po skasowanych worktree."""
    ok, out, err = call(["claude", "plugin", "list", "--json"], timeout=120, quiet=True)
    try:
        items = json.loads(out) if ok else None
    except ValueError:
        items = None
    if items is None:
        raise StepError(f"claude plugin list: {err or 'unreadable output'}")
    seen, live = set(), []
    for p in items:
        where = p.get("projectPath") if p.get("scope") != "user" else HOME
        key = (p.get("id"), p.get("scope"), where)
        if not where or not os.path.isdir(where) or key in seen:
            continue
        seen.add(key)
        live.append((p["id"], p["scope"], where))
    return live


def plugin_env():
    """Środowisko dla `claude plugin`. Claude Code (2.1.294) przy `plugin update` klonuje wtyczkę ze źródła
    github tylko po SSH; zapasowy HTTPS ma jedynie `plugin install`. Bez klucza SSH do GitHuba każda taka
    aktualizacja pada na Permission denied (publickey), więc wtedy HTTPS wymuszamy sami."""
    env = dict(ENV)
    env.setdefault("CLAUDE_CODE_PLUGIN_GIT_TIMEOUT_MS", str(PLUGIN_GIT_TIMEOUT * 1000))
    ok, out, _ = call(["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=yes",
                       "git@github.com"], timeout=15, codes=(1,), quiet=True, merge=True)
    if not (ok and "successfully authenticated" in out):
        env.setdefault("CLAUDE_CODE_PLUGIN_PREFER_HTTPS", "1")
    return env


def update_plugins(st):
    env = plugin_env()
    # marketplace'y klonują się po kolei, każdy dostaje do PLUGIN_GIT_TIMEOUT
    ok, _, err = call(["claude", "plugin", "marketplace", "update"], timeout=3 * PLUGIN_GIT_TIMEOUT, env=env)
    if not ok:
        st.failed.append(pkg("plugin marketplaces", None, None, error=err))
    for plugin_id, scope, where in plugin_installs():
        name = plugin_id.split("@")[0]
        # bez -y: polecenie z katalogu wtyczki potwierdza człowiek
        ok, out, err = call(["claude", "plugin", "update", plugin_id, "-s", scope, "--json"],
                            timeout=PLUGIN_GIT_TIMEOUT + 300, cwd=where, quiet=True, env=env)
        try:
            result = json.loads(out.strip().splitlines()[-1])
        except (ValueError, IndexError):
            st.failed.append(pkg(f"{name} plugin", None, None, error=err or "unreadable output"))
            continue
        old, new = result.get("oldVersion"), result.get("newVersion")
        if result.get("outcome") == "ok":
            if old != new and not any(p["name"] == f"{name} plugin" and p["to"] == new for p in st.updated):
                st.updated.append(pkg(f"{name} plugin", old, new))
        elif result.get("shownCommand"):
            st.held.append(pkg(f"{name} plugin", old, new, why="confirm",
                               retry=f"claude plugin update {plugin_id} -s {scope}"))
        else:
            st.failed.append(pkg(f"{name} plugin", old, new, error=result.get("message") or err))


def tree_hash(folder, scratch):
    """Hash drzewa git folderu, ten sam, który CLI skills zapisuje w blokadzie po pobraniu z GitHuba."""
    env = dict(ENV, GIT_DIR=os.path.join(scratch, "repo"), GIT_INDEX_FILE=os.path.join(scratch, "index"),
               GIT_WORK_TREE=folder)
    try:
        if os.path.exists(env["GIT_INDEX_FILE"]):
            os.remove(env["GIT_INDEX_FILE"])
        subprocess.run(["git", "add", "-A", "."], cwd=folder, env=env, capture_output=True, check=True, timeout=60)
        return subprocess.run(["git", "write-tree"], env=env, capture_output=True, text=True, check=True,
                              timeout=60).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None


def update_skills(st, dry):
    """Skille z `npx skills add -g`. Skill poprawiony ręcznie zostaje: aktualizacja by go nadpisała."""
    skills = load_json(SKILLS_LOCK, {}).get("skills", {})
    if not skills or not which("npx") or not which("git"):
        return
    scratch = tempfile.mkdtemp(prefix="claude-acc-skills-")
    try:
        subprocess.run(["git", "init", "-q", "--bare", os.path.join(scratch, "repo")], env=ENV, capture_output=True)
        edited = [n for n, info in sorted(skills.items())
                  if os.path.isdir(os.path.join(SKILLS_DIR, n))
                  and tree_hash(os.path.join(SKILLS_DIR, n), scratch) != info.get("skillFolderHash")]
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    st.held += [pkg(f"{n} skill", None, None, why="edited") for n in edited]
    names = [n for n in sorted(skills) if n not in edited]
    if dry or not names:
        return
    ok, _, err = call(["npx", "-y", "skills", "update", "-g", "-y", *names], timeout=900)
    after = load_json(SKILLS_LOCK, {}).get("skills", {})
    for n in names:
        old, new = skills[n].get("skillFolderHash"), after.get(n, {}).get("skillFolderHash")
        if new and new != old:
            st.updated.append(pkg(f"{n} skill", (old or "?")[:7], new[:7]))
    if not ok:
        st.failed.append(pkg("skills", None, None, error=err))


def step_claude(st, cfg, dry):
    if not which("claude"):
        return False
    before = claude_version()
    if not dry:
        # natywny Claude Code: nowa wersja obok starej, działające sesje zostają na swojej
        ok, _, err = call(["claude", "update"], timeout=900)
        after = claude_version()
        if before and after and after != before:
            st.updated.append(pkg("Claude Code", before, after))
        elif not ok:
            st.failed.append(pkg("Claude Code", before, None, error=err))
        update_plugins(st)
    update_skills(st, dry)
    return True


STEPS = [
    ("brew", "Homebrew", step_brew),
    ("npm", "npm", step_npm),
    ("go", "Go", step_go),
    ("pip", "Python", step_pip),
    ("claude", "Claude Code", step_claude),
]


# ---------- przebieg ----------


def take_lock():
    """Blokada na cały proces: zwolni ją system przy wyjściu, także po awarii."""
    os.makedirs(STATE_DIR, exist_ok=True)
    handle = open(LOCK_PATH, "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def next_due(cfg, state):
    last = state.get("last_run")
    if not last:
        return 0
    wait = cfg["every_days"] * DAY - HOUR if last.get("ok") else cfg["retry_hours"] * HOUR
    return last["at"] + wait


def next_run(cfg, state):
    """Pierwsza 4:30, kiedy przebieg będzie już należny: tak go uruchomi launchd."""
    when = datetime.fromtimestamp(max(next_due(cfg, state), time.time()))
    slot = when.replace(hour=RUN_AT[0], minute=RUN_AT[1], second=0, microsecond=0)
    if slot < when:
        slot += timedelta(days=1)
    return slot.timestamp()


def run_steps(cfg, only, dry):
    results = []
    for name, label, func in STEPS:
        if name in cfg["skip"] or (only and name not in only):
            continue
        st = Step(name, label)
        try:
            present = func(st, cfg, dry)
        except StepError as err:
            st.error, present = str(err), True
        except Exception as err:  # jeden menedżer paczek nie może zatrzymać reszty
            log(f"{name}: błąd {err!r}")
            st.error, present = f"{name}: {err}", True
        if present:
            results.append(st)
    return results


def cmd_run(cfg, args):
    force = "--force" in args
    dry = "--dry-run" in args
    only = set(args[args.index("--only") + 1].split(",")) if "--only" in args else None
    lock = take_lock()
    if lock is None:
        print("aktualizacja już trwa")
        return 0
    state = load_json(STATE_PATH, {})
    if not (force or dry or only) and time.time() < next_due(cfg, state):
        return 0

    started = time.time()
    if dry:
        print_plan(run_steps(cfg, only, dry=True))
        return 0
    # znacznik dla panelu; znika także wtedy, gdy przebieg padnie
    state["running_since"] = started
    write_json(STATE_PATH, state)
    log(f"=== aktualizacja ({'ręczna' if force else 'automatyczna'}{', tylko ' + ','.join(sorted(only)) if only else ''})")
    try:
        steps = [st.to_json() for st in run_steps(cfg, only, dry=False)]
    finally:
        current = load_json(STATE_PATH, {})
        if current.pop("running_since", None) is not None:
            write_json(STATE_PATH, current)

    state = load_json(STATE_PATH, {})
    done = {s["name"] for s in steps}
    order = [n for n, _, _ in STEPS]
    # kroki z poprzednich przebiegów zostają, a tych, których skrypt już nie zna, nie ma
    state["steps"] = steps + [s for s in state.get("steps", []) if s["name"] not in done and s["name"] in order]
    state["steps"].sort(key=lambda s: order.index(s["name"]))
    updated = sum(len(s["updated"]) for s in steps)
    failures = [f"{f['name']} ({s['label']})" for s in steps for f in s["failed"]]
    failures += [s["label"] for s in steps if s.get("error")]
    ok = not failures
    if not only:  # przebieg zawężony nie przesuwa terminu dla reszty
        state["last_run"] = {
            "at": time.time(),
            "duration": round(time.time() - started),
            "trigger": "manual" if force else "auto",
            "ok": ok,
            "updated": updated,
            "failed": len(failures),
        }
        if ok:
            state["last_success"] = state["last_run"]["at"]
        state["next_run"] = next_run(cfg, state)
    # powiadomienie tylko o nowym problemie, nie co noc o tym samym; przy kliknięciu mówi panel
    if failures and sorted(failures) != state.get("notified") and not force:
        notify("Aktualizacje", f"Nie udało się: {', '.join(failures[:4])}. Szczegóły w panelu claude-acc.")
    state["notified"] = sorted(failures)
    write_json(STATE_PATH, state)
    summary = f"zaktualizowano {updated}" + (f", nie udało się {len(failures)}: {', '.join(failures)}" if failures else "")
    log(f"=== koniec: {summary} w {round(time.time() - started)} s")
    print(summary if updated or failures else "wszystko aktualne")
    return 0


WHY = {"pin": "przypięte", "deps": "trzymane przez zależności", "edited": "poprawione ręcznie",
       "confirm": "czeka na potwierdzenie", "install": "instalator do uruchomienia",
       "packages": "ma paczki pip, nowa poprawka ręcznie"}


def print_plan(steps):
    # paczka pip mówi, w którym Pythonie leży: numpy bywa w dwóch
    named = lambda p: p["name"] + (f" ({p['python']})" if p.get("python") else "")
    for st in steps:
        line = f"{st.label}: "
        if st.error:
            line += f"błąd: {st.error}"
        else:
            line += ", ".join(f"{named(p)} {p['from']} → {p['to']}" for p in st.updated) or "aktualne"
        if st.held:
            line += "; przytrzymane: " + ", ".join(
                f"{named(p)} ({WHY.get(p.get('why'), p.get('why'))}" + (f", jest {p['to']})" if p.get("to") else ")")
                for p in st.held)
        if st.failed:
            line += "; nie do sprawdzenia: " + ", ".join(f"{p['name']}: {p['error']}" for p in st.failed)
        print(line)


def cmd_status(cfg, args):
    state = load_json(STATE_PATH, {})
    if "--json" in args:
        print(json.dumps(state))
        return 0
    last = state.get("last_run")
    if not last:
        print("Aktualizacje jeszcze nie ruszyły")
    else:
        when = datetime.fromtimestamp(last["at"]).strftime("%Y-%m-%d %H:%M")
        print(f"Ostatnio: {when}, {'w porządku' if last['ok'] else 'z błędami'}, zaktualizowano {last['updated']}")
        if state.get("next_run"):
            print(f"Następnie: {datetime.fromtimestamp(state['next_run']):%Y-%m-%d %H:%M}")
    if state.get("running_since"):
        print("Aktualizacja trwa teraz")
    for s in state.get("steps", []):
        detail = f"zaktualizowano {len(s['updated'])}" + (f", nie udało się {len(s['failed'])}" if s["failed"] else "")
        print(f"  {s['label']}: {detail}" + (f", błąd: {s['error']}" if s.get("error") else ""))
        for f in s["failed"]:
            print(f"    ! {f['name']}: {f['error']}")
    return 0


COMMANDS = {"run": cmd_run, "status": cmd_status}


def main(argv):
    cmd = argv[0] if argv else "status"
    if cmd not in COMMANDS:
        print(__doc__)
        return 2
    try:
        return COMMANDS[cmd](load_config(), argv[1:])
    except Exception as err:
        log(f"{cmd}: błąd {err!r}")
        print(f"błąd: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
