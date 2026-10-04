#!/usr/bin/env python3
"""Porządki na Macu: sprząta odtwarzalny syf i stroi system pod ciężką pracę.

Siedzi obok accswitch.py i trzyma stan w tym samym katalogu. launchd uruchamia
`janitor.py sweep` przy logowaniu i co 3 godziny jako proces w tle: macOS daje
mu tylko rdzenie energooszczędne i dławi jego dysk, więc porządki nie zabierają
mocy pracy.

Kasuje tylko to, co wraca jednym poleceniem (build, install, pobranie), i tylko
wtedy, gdy nikt tego nie używa: żaden proces nie trzyma tam otwartych plików,
żaden program poza powłoką nie ma tam katalogu roboczego, a pliki nie zmieniały
się od ustalonego czasu. Katalog najpierw dostaje nową nazwę, a dopiero potem
jest kasowany, więc dev server startujący w tej samej chwili buduje od zera,
zamiast czytać pół skasowanego cache.

Komendy:
  sweep [--dry-run] [--force]   jeden przebieg porządków (to uruchamia launchd);
                                --force pomija przerwy między zadaniami i baterię
  status [--json]               dysk, ostatnie porządki, ostrzeżenia
  report                        co muli Maca: procesy, Spotlight, pozostałości po aplikacjach
  spotlight                     projekty, których node_modules indeksuje Spotlight,
                                i otwarcie ustawień, w których się je wyklucza
  optimize [--dry-run|--undo]   jednorazowe strojenie systemu, odwracalne
"""

import fcntl
import glob
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import time
from bisect import bisect_left
from datetime import datetime

HOME = os.path.expanduser("~")
STATE_DIR = os.path.join(HOME, ".local/share/claude-acc")
CONFIG_PATH = os.path.join(STATE_DIR, "janitor.json")
STATE_PATH = os.path.join(STATE_DIR, "janitor-state.json")
LOG_PATH = os.path.join(STATE_DIR, "janitor.log")
LOCK_PATH = os.path.join(STATE_DIR, "janitor.lock")
BACKUP_PATH = os.path.join(STATE_DIR, "janitor-optimize.json")
LOG_MAX_BYTES = 512 * 1024
DATA_VOLUME = "/System/Volumes/Data" if os.path.isdir("/System/Volumes/Data") else "/"
# katalog w trakcie kasowania; przerwany przebieg zostawia go następnemu
TRASH_PREFIX = ".janitor-trash-"

GB = 1024**3
HOUR = 3600
DAY = 24 * HOUR
WEEK = 7 * DAY

DEFAULT_CONFIG = {
    # katalogi z projektami; brakujące są pomijane
    "roots": ["~/Documents", "~/Developer", "~/Projects", "~/code", "~/src"],
    # ścieżki, których porządki nigdy nie dotykają (materiały, eksperymenty)
    "protect": [],
    # przebieg z launchd nie powtarza porządków sprzed chwili (np. po ponownym logowaniu)
    "min_hours_between_sweeps": 2,
    # cache buildów Next.js (.next, .next-*) bez zmian od tylu godzin
    "next_idle_hours": 24,
    # .turbo, node_modules/.cache i node_modules/.vite bez zmian od tylu dni
    "cache_idle_days": 7,
    # node_modules projektów, w których nic się nie zmieniło od tylu dni; 0 wyłącza
    "node_modules_idle_days": 30,
    # paczki pobrane przez npx, nieużywane od tylu dni
    "npx_idle_days": 30,
    # cache kompilacji Go czyszczony dopiero powyżej tylu GB
    "go_cache_max_gb": 20,
    "go_cache_keep_percent": 60,
    # Xcode DerivedData bez zmian od tylu dni
    "derived_data_idle_days": 14,
    # logi aplikacji starsze niż tyle dni
    "log_days": 30,
    # na baterii poniżej tylu procent porządki czekają na ładowarkę
    "min_battery_percent": 30,
    # powiadomienie po przebiegu, który zwolnił co najmniej tyle GB
    "notify_min_gb": 2,
    # ostrzeżenie, gdy wolnego miejsca zostaje mniej niż tyle GB
    "low_disk_gb": 40,
    # zadania do pominięcia, np. ["docker", "brew"]; nazwy w TASKS
    "skip": [],
    # katalogi, do których agenci odkładają wyniki bez końca (snapshoty buildów, rendery):
    # [{"path": "~/.cache/portivo-perf/*/builds", "max_gb": 10, "keep": 1}]; wpisy w środku
    # ponad limit idą od najstarszych, `keep` najnowszych zostaje zawsze
    "caps": [],
}

# katalogi, do których skan projektów nie schodzi: zależności, buildy, cache, historia
WALK_SKIP = {
    "node_modules",
    ".git",
    ".vercel",
    ".turbo",
    ".cache",
    ".pnpm-store",
    "Pods",
    ".venv",
    "venv",
    "__pycache__",
    ".gradle",
    "DerivedData",
    ".Trash",
    ".build",
    ".swiftpm",
}
# procesy, których katalog roboczy nie znaczy, że ktoś pracuje w projekcie
SHELLS = {
    "zsh",
    "-zsh",
    "bash",
    "-bash",
    "sh",
    "fish",
    "-fish",
    "login",
    "tmux",
    "screen",
    "nu",
}


# ---------- drobne narzędzia ----------


def log(line, path=LOG_PATH):
    os.makedirs(STATE_DIR, exist_ok=True)
    if os.path.exists(path) and os.path.getsize(path) > LOG_MAX_BYTES:
        with open(path) as f:
            tail = f.readlines()[-1000:]
        with open(path, "w") as f:
            f.writelines(tail)
    with open(path, "a") as f:
        f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}  {line}\n")


def notify(title, text):
    subprocess.run(
        [
            "osascript",
            "-e",
            f"display notification {json.dumps(text)} with title {json.dumps(title)}",
        ],
        capture_output=True,
    )


def write_json(path, data, **kwargs):
    """Zapis przez plik tymczasowy: przerwany proces nie zostawi uciętego JSON-a."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, **kwargs)
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


def expand(path):
    return os.path.realpath(os.path.expanduser(path))


def tool_path():
    """PATH dla narzędzi: launchd daje procesowi tylko /usr/bin:/bin:/usr/sbin:/sbin."""
    dirs = [
        os.path.join(HOME, ".local/bin"),
        "/opt/homebrew/bin",
        "/usr/local/bin",
        os.path.join(HOME, ".docker/bin"),
        os.path.join(HOME, "go/bin"),
        os.path.join(HOME, ".cargo/bin"),
        os.path.join(HOME, ".bun/bin"),
    ]
    # najnowszy node z nvm, bo npm i pnpm go potrzebują
    nodes = sorted(
        glob.glob(os.path.join(HOME, ".nvm/versions/node/*/bin")),
        key=lambda p: [int(x) for x in re.findall(r"\d+", p.split("/")[-2])],
    )
    if nodes:
        dirs.insert(1, nodes[-1])
    dirs += ["/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    return ":".join(d for d in dirs if os.path.isdir(d))


ENV = dict(
    os.environ,
    PATH=tool_path(),
    HOMEBREW_NO_AUTO_UPDATE="1",
    HOMEBREW_NO_ENV_HINTS="1",
    HOMEBREW_NO_ANALYTICS="1",
    HOMEBREW_NO_INSTALL_CLEANUP="1",
)


def which(name):
    return shutil.which(name, path=ENV["PATH"])


def run(cmd, timeout=600):
    """Wyjście polecenia; None, gdy polecenia nie ma, przekroczyło czas albo padło."""
    try:
        done = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, env=ENV
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return done.stdout if done.returncode == 0 else None


def du_bytes(path):
    out = run(["du", "-sk", path], timeout=900)
    try:
        return int(out.split()[0]) * 1024
    except (AttributeError, IndexError, ValueError):
        return 0


def disk():
    st = os.statvfs(DATA_VOLUME)
    return st.f_bavail * st.f_frsize, st.f_blocks * st.f_frsize


def human(size):
    """Rozmiar po polsku: "31,6 GB", "412 MB"."""
    size = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            text = f"{size:.1f}" if unit in ("GB", "TB") else f"{size:.0f}"
            return f"{text.replace('.', ',')} {unit}"
        size /= 1024
    return ""


def parse_size(text):
    """Rozmiar w zapisie Dockera i Homebrew ("1.2GB", "87.4MB", "0B") na bajty."""
    match = re.search(r"([\d.]+)\s*([kKMGT]?i?B)", text or "")
    if not match:
        return 0
    unit = match.group(2).upper().replace("I", "")
    power = {"B": 0, "KB": 1, "MB": 2, "GB": 3, "TB": 4}.get(unit, 0)
    return int(
        float(match.group(1)) * (1000 if "i" not in match.group(2) else 1024) ** power
    )


def recently_changed(path, since, skip=()):
    """Pierwszy plik w drzewie zmieniony po `since`, albo None.

    Liczą się tylko pliki: mtime katalogu zmienia też samo kasowanie
    (choćby nasze), a to nie jest praca w projekcie. Symlinków nie odwiedza.
    """
    stack = [path]
    while stack:
        try:
            entries = os.scandir(stack.pop())
        except OSError:
            continue
        with entries:
            for entry in entries:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        if entry.name not in skip and not entry.name.startswith(
                            ".next"
                        ):
                            stack.append(entry.path)
                    elif entry.stat(follow_symlinks=False).st_mtime > since:
                        return entry.path
                except OSError:
                    continue
    return None


def mtime(path):
    try:
        return os.stat(path, follow_symlinks=False).st_mtime
    except OSError:
        return 0


def born(path):
    """Chwila powstania pliku albo katalogu (APFS ją pamięta, kopiowanie jej nie podrabia)."""
    try:
        st = os.stat(path, follow_symlinks=False)
    except OSError:
        return 0
    return getattr(st, "st_birthtime", st.st_ctime)


def changed(path):
    """Ostatnia zmiana treści albo metadanych; ctime ustawia system, nie program."""
    try:
        st = os.stat(path, follow_symlinks=False)
    except OSError:
        return 0
    return max(st.st_mtime, st.st_ctime)


def uptime():
    out = run(["sysctl", "-n", "kern.boottime"]) or ""
    match = re.search(r"sec = (\d+)", out)
    return time.time() - int(match.group(1)) if match else 1e9


def battery():
    """(na baterii?, procent) z pmset; Mac bez baterii to (False, 100)."""
    out = run(["pmset", "-g", "batt"]) or ""
    match = re.search(r"(\d+)%", out)
    return "Battery Power" in out, int(match.group(1)) if match else 100


def processes():
    """[(pid, ppid, etime w sekundach, %cpu, rss w bajtach, komenda)] wszystkich procesów."""
    out = run(["ps", "-axo", "pid=,ppid=,etime=,pcpu=,rss=,command="]) or ""
    rows = []
    for line in out.splitlines():
        parts = line.split(None, 5)
        if len(parts) < 6:
            continue
        pid, ppid, etime, cpu, rss, command = parts
        days, _, clock = etime.rpartition("-")
        seconds = 0
        for chunk in clock.split(":"):
            seconds = seconds * 60 + int(chunk or 0)
        seconds += int(days or 0) * DAY
        rows.append(
            (int(pid), int(ppid), seconds, float(cpu), int(rss) * 1024, command)
        )
    return rows


class InUse:
    """Pliki otwarte i katalogi robocze procesów tego użytkownika, z jednego wywołania lsof."""

    def __init__(self):
        # bez run(): lsof kończy się kodem 1 przy byle ostrzeżeniu, a wynik i tak ma
        try:
            out = subprocess.run(
                ["lsof", "-nP", "-w", "-u", str(os.getuid()), "-F", "pcfn"],
                capture_output=True,
                text=True,
                timeout=300,
                env=ENV,
            ).stdout
        except (OSError, subprocess.TimeoutExpired):
            out = ""
        # pusty wynik to awaria lsof, nie brak procesów: wtedy wszystko uznajemy za zajęte
        self.ok = bool(out)
        paths, cwds = set(), []
        command, fd = "", ""
        for line in out.splitlines():
            tag, value = line[:1], line[1:]
            if tag == "c":
                command = value
            elif tag == "f":
                fd = value
            elif tag == "n" and value.startswith("/"):
                if fd == "cwd":
                    if command not in SHELLS:
                        cwds.append(value)
                else:
                    paths.add(value)
        self.paths = sorted(paths)
        self.cwds = sorted(set(cwds))
        self.commands = [row[5] for row in processes()]

    @staticmethod
    def _inside(sorted_paths, path):
        """Czy na posortowanej liście jest `path` albo coś w jego drzewie."""
        i = bisect_left(sorted_paths, path)
        if i < len(sorted_paths) and sorted_paths[i] == path:
            return True
        prefix = path.rstrip("/") + "/"
        j = bisect_left(sorted_paths, prefix)
        return j < len(sorted_paths) and sorted_paths[j].startswith(prefix)

    def holds(self, path):
        """Czy któryś proces ma otwarty plik w tym drzewie."""
        return self._inside(self.paths, path)

    def works_in(self, path):
        """Czy któryś program (nie powłoka) ma katalog roboczy w tym drzewie."""
        return self._inside(self.cwds, path)

    def busy(self, path):
        return not self.ok or self.holds(path) or self.works_in(path)

    def mentions(self, text):
        return any(text in command for command in self.commands)


# ---------- przebieg porządków ----------


class Sweep:
    def __init__(self, cfg, dry_run):
        self.cfg = cfg
        self.dry_run = dry_run
        self.items = []  # (zadanie, ścieżka albo opis, bajty)
        self.skipped = []  # (zadanie, ścieżka, powód)
        self.warnings = []
        self.failures = []  # (zadanie, błąd) dla panelu
        self.protect = [expand(p) for p in cfg["protect"]]
        # kasujemy tylko w domu i w katalogu tymczasowym użytkownika
        self.allowed = [
            os.path.realpath(HOME) + "/",
            os.path.realpath(user_tmpdir()) + "/",
        ]
        self._usage = None
        self.pnpm_prune = False

    @property
    def usage(self):
        if self._usage is None:
            self._usage = InUse()
        return self._usage

    def protected(self, path, real=False):
        """`real=True` dla ścieżek ze skanu: już są rzeczywiste, realpath tylko spowalnia spacer."""
        path = path if real else os.path.realpath(path)
        return any(path == p or path.startswith(p + "/") for p in self.protect)

    def skip(self, task, path, reason):
        self.skipped.append((task, path, reason))

    def remove(self, task, path, expect):
        """Skasuj katalog albo plik; `expect(nazwa)` to bezpiecznik przed pomyłką w zadaniu."""
        real = os.path.realpath(path)
        name = os.path.basename(path)
        if not expect(name) or os.path.islink(path):
            log(f"{task}: odmawiam, nieoczekiwana ścieżka {path}")
            return 0
        if not any(real.startswith(base) for base in self.allowed) or self.protected(
            path
        ):
            self.skip(task, path, "chronione")
            return 0
        if tracked_by_git(path):
            # zależności albo build w repozytorium to czyjś wybór, a kasowanie byłoby zmianą w gicie
            self.skip(task, path, "w gicie")
            return 0
        if os.path.isdir(path):
            size = du_bytes(path)
        else:
            try:
                size = os.lstat(path).st_blocks * 512
            except OSError:
                return 0
        if not self.dry_run:
            parent = os.path.dirname(path)
            doomed = os.path.join(parent, f"{TRASH_PREFIX}{name}-{int(time.time())}")
            try:
                os.rename(path, doomed)
            except OSError as err:
                self.skip(task, path, f"nie da się przenieść: {err.strerror}")
                return 0
            wipe(doomed)
        self.items.append((task, path, size))
        return size

    def record(self, task, what, size):
        """Wynik polecenia, które samo sprząta (docker, brew, go clean)."""
        if size > 0:
            self.items.append((task, what, size))

    @property
    def freed(self):
        return sum(size for _, _, size in self.items)


def tracked_by_git(path):
    """Czy git śledzi coś pod tą ścieżką. Poza repozytorium git kończy się błędem, czyli nie."""
    parent, name = os.path.split(path)
    out = run(["git", "-C", parent, "ls-files", "--", name], timeout=30)
    return bool(out and out.strip())


def wipe(path):
    if os.path.isdir(path) and not os.path.islink(path):
        subprocess.run(["rm", "-rf", "--", path], capture_output=True)
    else:
        try:
            os.remove(path)
        except OSError:
            pass


def user_tmpdir():
    try:
        return os.confstr("CS_DARWIN_USER_TEMP_DIR")
    except (ValueError, OSError):
        return os.environ.get("TMPDIR", "/tmp")


class Scan:
    """Jeden spacer po katalogach projektów: wszystko, co zadania projektowe mogą ruszyć."""

    def __init__(self, sweep):
        self.next_dirs = []
        self.turbo_dirs = []
        self.node_modules = []
        self.projects = {}  # korzeń projektu -> [jego node_modules]
        self.trash = []
        for root in sweep.cfg["roots"]:
            root = expand(root)
            if os.path.isdir(root) and not sweep.protected(root):
                self._walk(root, sweep)

    def _walk(self, root, sweep):
        stack = [(root, 0, None)]
        while stack:
            current, depth, project = stack.pop()
            try:
                entries = list(os.scandir(current))
            except OSError:
                continue
            names = {e.name for e in entries}
            if project is None and "package.json" in names and "node_modules" in names:
                project = current
                self.projects[project] = []
            for entry in entries:
                try:
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                name, path = entry.name, entry.path
                if name.startswith(TRASH_PREFIX):
                    self.trash.append(path)
                    continue
                if sweep.protected(path, real=True):
                    continue
                if name == ".next" or name.startswith(".next-"):
                    # tylko obok package.json: to build aplikacji, a nie np. kopia w wynikach Vercela
                    if "package.json" in names:
                        self.next_dirs.append(path)
                    continue
                if name == ".turbo":
                    self.turbo_dirs.append(path)
                    continue
                if name == "node_modules":
                    self.node_modules.append(path)
                    if project:
                        self.projects[project].append(path)
                    continue
                if name in WALK_SKIP or name.endswith(
                    (".noindex", ".app", ".photoslibrary")
                ):
                    continue
                if depth < 12:
                    stack.append((path, depth + 1, project))


def task_trash(sw, scan):
    for path in scan().trash:
        sw.remove("trash", path, lambda n: n.startswith(TRASH_PREFIX))


def task_next(sw, scan):
    since = time.time() - sw.cfg["next_idle_hours"] * HOUR
    for path in scan().next_dirs:
        app = os.path.dirname(path)
        if sw.usage.holds(path) or sw.usage.works_in(app) or not sw.usage.ok:
            sw.skip("next", path, "w użyciu")
        elif recently_changed(path, since):
            sw.skip("next", path, "świeży")
        else:
            sw.remove("next", path, lambda n: n == ".next" or n.startswith(".next-"))


def task_project_caches(sw, scan):
    since = time.time() - sw.cfg["cache_idle_days"] * DAY
    candidates = [(p, ".turbo") for p in scan().turbo_dirs]
    for nm in scan().node_modules:
        for name in (".cache", ".vite"):
            candidates.append((os.path.join(nm, name), name))
    for path, name in candidates:
        if not os.path.isdir(path):
            continue
        if sw.usage.busy(path):
            sw.skip("caches", path, "w użyciu")
        elif recently_changed(path, since):
            sw.skip("caches", path, "świeży")
        else:
            sw.remove("caches", path, lambda n, want=name: n == want)


def project_active(project, since):
    """Czy w projekcie ktoś pracował: zmieniony plik poza zależnościami albo ruch w gicie."""
    git = os.path.join(project, ".git")
    if os.path.isdir(git):
        for marker in ("index", "HEAD", "logs/HEAD"):
            if mtime(os.path.join(git, marker)) > since:
                return True
    return recently_changed(project, since, skip=WALK_SKIP) is not None


def task_node_modules(sw, scan):
    days = sw.cfg["node_modules_idle_days"]
    if not days:
        return
    since = time.time() - days * DAY
    for project, dirs in scan().projects.items():
        if not dirs:
            continue
        if sw.usage.busy(project):
            sw.skip("node_modules", project, "w użyciu")
            continue
        if project_active(project, since):
            continue
        for path in dirs:
            if os.path.isdir(path) and sw.remove(
                "node_modules", path, lambda n: n == "node_modules"
            ):
                sw.pnpm_prune = True


def task_tmp(sw, _scan):
    """Katalogi go-build* po `go test`, które przerwany proces zostawia w $TMPDIR (po kilka GB)."""
    since = time.time() - 6 * HOUR
    for path in glob.glob(os.path.join(user_tmpdir(), "go-build*")):
        if not os.path.isdir(path) or mtime(path) > since:
            continue
        if sw.usage.busy(path) or recently_changed(path, since):
            sw.skip("tmp", path, "w użyciu")
        else:
            sw.remove("tmp", path, lambda n: n.startswith("go-build"))


def trim_oldest(root, keep_bytes, dry_run=False):
    """Usuwa najdawniej używane wpisy cache Go (podkatalogi 00..ff), aż zostanie keep_bytes.

    Go odświeża mtime wpisu przy użyciu (najwyżej raz na godzinę), więc mtime to czas
    ostatniego użycia. Brak pliku wyjścia przy zachowanym wpisie akcji Go traktuje jak
    chybienie i buduje ponownie, więc kolejność kasowania niczego nie psuje."""
    entries, total = [], 0
    for sub in os.listdir(root):
        path = os.path.join(root, sub)
        if len(sub) != 2 or not os.path.isdir(path):
            continue  # README, trim.txt, testexpire.txt zostają
        for name in os.listdir(path):
            full = os.path.join(path, name)
            try:
                st = os.lstat(full)
            except OSError:
                continue
            entries.append((st.st_mtime, st.st_blocks * 512, full))
            total += st.st_blocks * 512
    freed = 0
    for _mtime, size, full in sorted(entries):
        if total - freed <= keep_bytes:
            break
        if not dry_run:
            try:
                os.unlink(full)
            except OSError:
                continue
        freed += size
    return freed


def task_go_cache(sw, _scan):
    go = which("go")
    if not go:
        return
    cache = (run([go, "env", "GOCACHE"]) or "").strip()
    if not cache or not os.path.isdir(cache):
        return
    size = du_bytes(cache)
    if size < sw.cfg["go_cache_max_gb"] * GB:
        return
    if any(
        re.search(r"(^|/)go (build|test|run|install|vet)\b", c)
        for c in sw.usage.commands
    ):
        sw.skip("go", cache, "kompilacja w toku")
        return
    # nie całość: `go clean -cache` zmuszał każdego agenta do budowania wszystkiego od zera
    # (10-03 poleciało 87,5 GB naraz); zostaje świeża część, najstarsze wpisy idą
    keep = sw.cfg["go_cache_max_gb"] * GB * sw.cfg["go_cache_keep_percent"] / 100
    freed = trim_oldest(cache, keep, dry_run=sw.dry_run)
    sw.record("go", "najstarsze wpisy cache kompilacji Go", freed)


def task_npm(sw, _scan):
    npm_dir = os.path.join(HOME, ".npm")
    if not os.path.isdir(npm_dir):
        return
    cacache = os.path.join(npm_dir, "_cacache")
    npm = which("npm")
    if (
        npm
        and os.path.isdir(cacache)
        and not sw.usage.mentions("npm install")
        and not sw.usage.mentions("npm ci")
    ):
        before = du_bytes(cacache)
        if not sw.dry_run:
            run(
                [npm, "cache", "verify"], timeout=900
            )  # zbiera śmieci, zostawia używane paczki
        sw.record("npm", "cache npm (verify)", before - du_bytes(cacache))
    # npx: paczka pobrana raz, a potem leży latami; serwery MCP chodzą prosto z tego katalogu
    since = time.time() - sw.cfg["npx_idle_days"] * DAY
    for path in glob.glob(os.path.join(npm_dir, "_npx", "*")):
        touched = max(
            mtime(path),
            mtime(os.path.join(path, "package.json")),
            mtime(os.path.join(path, "package-lock.json")),
        )
        key = os.path.basename(path)
        if touched > since:
            continue
        if sw.usage.mentions(key) or sw.usage.busy(path):
            sw.skip("npm", path, "w użyciu")
        else:
            sw.remove("npm", path, lambda n, want=key: n == want)
    week_ago = time.time() - WEEK
    for path in glob.glob(os.path.join(npm_dir, "_logs", "*.log")):
        if mtime(path) < week_ago:
            sw.remove("npm", path, lambda n: n.endswith(".log"))


def task_pnpm(sw, _scan):
    pnpm = which("pnpm")
    if not pnpm:
        return
    installing = re.compile(
        r"\bpnpm(\.cjs)?\s+(install|i|add|update|up|remove|rm|fetch|import|dlx)\b"
    )
    if any(installing.search(c) for c in sw.usage.commands):
        sw.skip("pnpm", "store", "pnpm instaluje")
        return
    store = (run([pnpm, "store", "path"]) or "").strip()
    if not store or not os.path.isdir(store):
        return
    before = du_bytes(store)
    if not sw.dry_run:
        run([pnpm, "store", "prune"], timeout=1800)
    sw.record("pnpm", "pnpm store prune", before - du_bytes(store))


def task_docker(sw, _scan):
    docker = which("docker")
    # bez działającego silnika nic nie robimy, a już na pewno go nie uruchamiamy
    if (
        not docker
        or run([docker, "info", "--format", "{{.ServerVersion}}"], timeout=20) is None
    ):
        return
    if sw.dry_run:
        return
    images = run([docker, "image", "prune", "-f"], timeout=600) or ""
    if "reclaimed space:" in images:
        sw.record(
            "docker",
            "osierocone obrazy Dockera",
            parse_size(images.split("reclaimed space:")[-1]),
        )
    builder = (
        run([docker, "builder", "prune", "-f", "--filter", "until=168h"], timeout=600)
        or ""
    )
    total = re.search(r"Total:\s*(\S+)", builder)
    sw.record(
        "docker", "cache buildów Dockera", parse_size(total.group(1)) if total else 0
    )


def task_xcode(sw, _scan):
    derived = os.path.join(HOME, "Library/Developer/Xcode/DerivedData")
    since = time.time() - sw.cfg["derived_data_idle_days"] * DAY
    for path in glob.glob(os.path.join(derived, "*")):
        if not os.path.isdir(path) or path.endswith("ModuleCache.noindex"):
            continue
        if sw.usage.busy(path) or recently_changed(path, since):
            continue
        sw.remove("xcode", path, lambda n: True)
    xcrun = which("xcrun")
    if (
        xcrun
        and not sw.dry_run
        and os.path.isdir(os.path.join(HOME, "Library/Developer/CoreSimulator"))
    ):
        run([xcrun, "simctl", "delete", "unavailable"], timeout=300)


def task_brew(sw, _scan):
    brew = which("brew")
    if (
        not brew
        or sw.usage.mentions("Homebrew/brew.rb")
        or sw.usage.mentions("/brew.sh")
    ):
        return
    out = (
        run(
            [brew, "cleanup", "--prune=14", "-s"] + (["-n"] if sw.dry_run else []),
            timeout=1800,
        )
        or ""
    )
    freed = re.search(r"freed approximately ([\d.]+\s*[KMGT]?B)", out) or re.search(
        r"free approximately ([\d.]+\s*[KMGT]?B)", out
    )
    sw.record("brew", "brew cleanup", parse_size(freed.group(1)) if freed else 0)


def task_uv(sw, _scan):
    uv = which("uv")
    if not uv or sw.usage.mentions("uv pip") or sw.usage.mentions("uv sync"):
        return
    cache = (run([uv, "cache", "dir"]) or "").strip()
    if not cache or not os.path.isdir(cache):
        return
    before = du_bytes(cache)
    if not sw.dry_run:
        run([uv, "cache", "prune"], timeout=900)
    sw.record("uv", "uv cache prune", before - du_bytes(cache))


def task_caps(sw, _scan):
    """Limit na katalogi z wynikami agentów: najstarsze wpisy ponad `max_gb` idą do kasacji.

    Powstało po nocy, w której snapshoty pomiarów wydajności (po 25 GB, bo kopiowały
    rendery filmu) zajęły 50 GB. Wpis zmieniony w ostatnich `fresh_minutes` (domyślnie 10)
    to zapis w toku.
    """
    for cap in sw.cfg["caps"]:
        limit = cap.get("max_gb", 10) * GB
        keep = cap.get("keep", 1)
        fresh = time.time() - cap.get("fresh_minutes", 10) * 60
        for folder in sorted(glob.glob(os.path.expanduser(cap["path"]))):
            if not os.path.isdir(folder):
                continue
            try:
                entries = [
                    e.path
                    for e in os.scandir(folder)
                    if not e.name.startswith(TRASH_PREFIX)
                ]
            except OSError:
                continue
            # kolejność po dacie powstania: rsync -a kopiuje mtime źródła, więc mtime kłamie
            entries.sort(key=born, reverse=True)
            total = 0
            for i, path in enumerate(entries):
                total += du_bytes(path)
                if i < keep or total <= limit or changed(path) > fresh:
                    continue
                if sw.usage.busy(path):
                    sw.skip("caps", path, "w użyciu")
                    continue
                name = os.path.basename(path)
                sw.remove("caps", path, lambda n, want=name: n == want)


def task_logs(sw, _scan):
    since = time.time() - sw.cfg["log_days"] * DAY
    for root, dirs, files in os.walk(os.path.join(HOME, "Library/Logs")):
        for name in files:
            path = os.path.join(root, name)
            if mtime(path) < since and not sw.usage.busy(path):
                sw.remove("logs", path, lambda n: True)


# (nazwa, funkcja, co ile najczęściej); kolejność ma znaczenie: pnpm sprząta po node_modules
TASKS = [
    ("trash", task_trash, 0),
    ("next", task_next, 0),
    ("caches", task_project_caches, DAY),
    ("node_modules", task_node_modules, DAY),
    ("tmp", task_tmp, 0),
    ("caps", task_caps, 0),
    ("go", task_go_cache, DAY),
    ("npm", task_npm, DAY),
    ("pnpm", task_pnpm, WEEK),
    ("docker", task_docker, DAY),
    ("xcode", task_xcode, DAY),
    ("brew", task_brew, WEEK),
    ("uv", task_uv, WEEK),
    ("logs", task_logs, DAY),
]


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


def cmd_sweep(cfg, args):
    dry_run = "--dry-run" in args
    force = "--force" in args
    lock = take_lock()
    if lock is None:
        print("porządki już trwają")
        return 0
    state = load_json(STATE_PATH, {})
    last = (state.get("last_sweep") or {}).get("at", 0)
    if not force and not dry_run:
        if time.time() - last < cfg["min_hours_between_sweeps"] * HOUR:
            return 0
        on_battery, percent = battery()
        if on_battery and percent < cfg["min_battery_percent"]:
            log(f"sweep: czekam na ładowarkę ({percent}%)")
            return 0
        # tuż po starcie systemu wszystko się ładuje; porządki poczekają, aż opadnie
        wait = 300 - uptime()
        if wait > 0:
            time.sleep(wait)

    started = time.time()
    if dry_run:
        return run_sweep(cfg, state, started, dry_run, force)
    # znacznik dla paska menu; znika także wtedy, gdy przebieg padnie
    state["running_since"] = started
    write_json(STATE_PATH, state, indent=1)
    try:
        return run_sweep(cfg, state, started, dry_run, force)
    finally:
        current = load_json(STATE_PATH, {})
        if current.pop("running_since", None) is not None:
            write_json(STATE_PATH, current, indent=1)


def run_sweep(cfg, state, started, dry_run, force):
    sw = Sweep(cfg, dry_run)
    cached = {}

    def scan():
        if "scan" not in cached:
            cached["scan"] = Scan(sw)
        return cached["scan"]

    runs = state.get("task_runs", {})
    for name, func, every in TASKS:
        if name in cfg["skip"]:
            continue
        due = force or dry_run or time.time() - runs.get(name, 0) >= every
        if name == "pnpm" and sw.pnpm_prune:
            due = True
        if not due:
            continue
        before = sw.freed
        try:
            func(sw, scan)
        except Exception as err:  # jedno zadanie nie może zatrzymać reszty
            log(f"{name}: błąd {err!r}")
            sw.warnings.append(f"Zadanie {name} padło: {err}")
            sw.failures.append((name, str(err)))
        if not dry_run:
            runs[name] = time.time()
        if sw.freed > before:
            log(
                f"{name}: {'do zwolnienia' if dry_run else 'zwolniono'} {human(sw.freed - before)}"
            )

    free, total = disk()
    warnings = list(sw.warnings)
    alerts = [{"kind": "task_failed", "task": t, "error": e} for t, e in sw.failures]
    if free < cfg["low_disk_gb"] * GB:
        warnings.append(f"Na dysku zostało tylko {human(free)}")
        alerts.append({"kind": "low_disk", "free": free})
    if "scan" in cached:
        indexed = spotlight_indexed(cached["scan"])
        if indexed:
            names = ", ".join(os.path.basename(p) for p, _ in indexed[:3])
            more = f" i {len(indexed) - 3} innych" if len(indexed) > 3 else ""
            warnings.append(
                f"Spotlight indeksuje node_modules: {names}{more}. "
                "Wyklucz je: claude-acc mac spotlight"
            )
            alerts.append(
                {"kind": "spotlight", "projects": [os.path.basename(p) for p, _ in indexed]}
            )
    elif state.get("warnings"):
        # przebieg bez skanu projektów nie wie nic nowego o Spotlight, zostawia stare ostrzeżenie
        warnings += [w for w in state["warnings"] if w.startswith("Spotlight")]
        alerts += [a for a in state.get("alerts", []) if a.get("kind") == "spotlight"]

    items = sorted(sw.items, key=lambda item: -item[2])
    summary = {
        "at": time.time(),
        "duration": round(time.time() - started),
        "freed": sw.freed,
        "items": len(items),
        "dry_run": dry_run,
        "top": [{"task": t, "path": p, "bytes": b} for t, p, b in items[:15]],
        "skipped": len(sw.skipped),
    }
    if dry_run:
        print_sweep(summary, sw.skipped, free, total)
        return 0

    state = load_json(STATE_PATH, {})
    state.update(
        {
            "last_sweep": summary,
            "task_runs": runs,
            "warnings": warnings,
            "alerts": alerts,
            "freed_total": state.get("freed_total", 0) + sw.freed,
            "disk_free": free,
            "disk_total": total,
        }
    )
    state.pop("running_since", None)
    alerted = state.get("low_disk_alert_at", 0)
    if free < cfg["low_disk_gb"] * GB and time.time() - alerted > 12 * HOUR:
        notify(
            "Mało miejsca na dysku",
            f"Zostało {human(free)}. Janitor zwolnił {human(sw.freed)}.",
        )
        state["low_disk_alert_at"] = time.time()
    elif sw.freed >= cfg["notify_min_gb"] * GB:
        notify("Porządki na Macu", f"Zwolniono {human(sw.freed)}, wolne {human(free)}")
    write_json(STATE_PATH, state, indent=1)
    log(
        f"sweep: zwolniono {human(sw.freed)} w {summary['duration']} s, pominięto {len(sw.skipped)}, "
        f"wolne {human(free)}"
    )
    print_sweep(summary, sw.skipped, free, total)
    return 0


def print_sweep(summary, skipped, free, total):
    verb = "do zwolnienia" if summary["dry_run"] else "zwolniono"
    print(
        f"{verb}: {human(summary['freed'])} ({summary['items']} pozycji) w {summary['duration']} s"
    )
    for item in summary["top"]:
        print(f"  {human(item['bytes']):>9}  {item['task']:<12} {short(item['path'])}")
    busy = [s for s in skipped if s[2] in ("w użyciu", "chronione")]
    if busy:
        print(f"pominięte, bo w użyciu albo chronione: {len(busy)}")
        for task, path, reason in busy[:10]:
            print(f"  {task:<12} {short(path)} ({reason})")
    print(f"wolne: {human(free)} z {human(total)}")


def short(path):
    return path.replace(HOME, "~", 1) if isinstance(path, str) else path


# ---------- Spotlight ----------


def spotlight_indexed(scan, minimum=500):
    """Projekty, których node_modules ląduje w indeksie Spotlight: [(projekt, liczba plików .js)].

    Spotlight omija katalogi z kropką na początku i z sufiksem .noindex. Plik
    .metadata_never_index ani flaga `chflags hidden` nie działają, więc na
    node_modules zostaje lista prywatności w ustawieniach Spotlight.
    """
    mdfind = which("mdfind")
    if not mdfind:
        return []
    found = []
    for project, dirs in scan.projects.items():
        count = 0
        # główny node_modules wystarczy: przy hoistingu to w nim leży prawie wszystko
        for nm in [d for d in dirs if os.path.dirname(d) == project] or dirs:
            out = (
                run([mdfind, "-onlyin", nm, "-count", "-name", ".js"], timeout=60)
                or "0"
            )
            try:
                count += int(out.strip() or 0)
            except ValueError:
                pass
        if count >= minimum:
            found.append((project, count))
    return sorted(found, key=lambda row: -row[1])


def cmd_spotlight(cfg, args):
    sw = Sweep(cfg, dry_run=True)
    indexed = spotlight_indexed(Scan(sw))
    if not indexed:
        print("Spotlight nie indeksuje żadnego node_modules")
        return 0
    print("Spotlight indeksuje node_modules w tych projektach:")
    for project, count in indexed:
        print(f"  {count:>8} plików .js  {short(project)}")
    print(
        "\nDodaj te katalogi w Ustawieniach systemowych: Spotlight > Prywatność wyszukiwania (+)."
    )
    print(
        "Wykluczenie projektu nie psuje buildów, a Spotlight przestaje mielić każdy install."
    )
    if "--no-open" not in args:
        subprocess.run(
            [
                "open",
                "x-apple.systempreferences:com.apple.Spotlight-Settings.extension",
            ],
            capture_output=True,
        )
    return 0


# ---------- status i raport ----------


def cmd_status(cfg, args):
    state = load_json(STATE_PATH, {})
    free, total = disk()
    state["disk_free"], state["disk_total"] = free, total
    if "--json" in args:
        print(json.dumps(state))
        return 0
    print(
        f"Dysk: {human(free)} wolne z {human(total)} ({100 - free * 100 // total}% zajęte)"
    )
    last = state.get("last_sweep")
    if last:
        ago = int(time.time() - last["at"])
        when = f"{ago // 3600} godz. temu" if ago >= 3600 else f"{ago // 60} min temu"
        print(
            f"Ostatnie porządki: {when}, zwolniono {human(last['freed'])} "
            f"({last['items']} pozycji, {last['duration']} s)"
        )
    else:
        print("Porządki jeszcze nie ruszyły")
    print(f"Razem zwolnione przez janitora: {human(state.get('freed_total', 0))}")
    if state.get("running_since"):
        print("Porządki trwają teraz")
    for warning in state.get("warnings", []):
        print(f"! {warning}")
    return 0


def installed_apps():
    """Nazwy i identyfikatory zainstalowanych aplikacji, małymi literami."""
    names, bundles = set(), set()
    roots = [
        "/Applications",
        "/Applications/Utilities",
        "/System/Applications",
        "/System/Applications/Utilities",
        os.path.join(HOME, "Applications"),
    ]
    for root in roots:
        for app in glob.glob(os.path.join(root, "*.app")) + glob.glob(
            os.path.join(root, "*/*.app")
        ):
            names.add(os.path.basename(app)[:-4].lower())
            try:
                with open(os.path.join(app, "Contents/Info.plist"), "rb") as f:
                    bundle = plistlib.load(f).get("CFBundleIdentifier")
                if bundle:
                    bundles.add(bundle.lower())
            except (OSError, ValueError, plistlib.InvalidFileException):
                pass
    return names, bundles


def name_alive(key, squashed, bundles):
    """Czy katalog o zwykłej nazwie ("Telegram Desktop", "rtk") należy do czegoś, co jest.

    Pasuje nazwa aplikacji w obie strony ("telegram" w "telegramdesktop") albo
    polecenie w PATH, bo dane w Application Support trzymają też narzędzia bez .app.
    """
    word = key.replace(" ", "")
    first = re.split(r"[\s_-]", key)[0]
    variants = {word, first} if len(first) >= 3 else {word}
    for v in variants:
        if any(v in n for n in squashed) or any(v in b for b in bundles) or which(v):
            return True
    return any(len(n) >= 4 and n in word for n in squashed)


def leftovers(min_bytes=300 * 1024**2):
    """Duże katalogi danych aplikacji, których już nie ma na dysku: [(ścieżka, bajty)]."""
    names, bundles = installed_apps()
    squashed = {n.replace(" ", "") for n in names}
    vendors = {".".join(b.split(".")[:2]) for b in bundles if b.count(".") >= 2}
    found = []
    # bez Library/Caches: tam siedzą też narzędzia bez aplikacji (go-build, pnpm), a to sprząta sweep
    for base in ("Library/Application Support", "Library/Containers"):
        for path in glob.glob(os.path.join(HOME, base, "*")):
            key = os.path.basename(path).lower()
            if key.startswith(("com.apple.", "group.com.apple")) or not os.path.isdir(
                path
            ):
                continue
            if key.count(".") >= 2:
                # identyfikator pakietu: żywa jest każda aplikacja tego samego wydawcy
                # (com.docker.install należy do com.docker.docker)
                alive = ".".join(key.split(".")[:2]) in vendors
            else:
                alive = name_alive(key, squashed, bundles)
            if alive:
                continue
            size = du_bytes(path)
            if size >= min_bytes:
                found.append((path, size))
    return sorted(found, key=lambda row: -row[1])


def broken_launch_items():
    """Wpisy launchd, których program zniknął razem z aplikacją: [(plik plist, program)]."""
    found = []
    for folder in (
        os.path.join(HOME, "Library/LaunchAgents"),
        "/Library/LaunchAgents",
        "/Library/LaunchDaemons",
    ):
        for plist in glob.glob(os.path.join(folder, "*.plist")):
            try:
                with open(plist, "rb") as f:
                    data = plistlib.load(f)
            except (OSError, ValueError, plistlib.InvalidFileException):
                continue
            program = data.get("Program") or (data.get("ProgramArguments") or [None])[0]
            if (
                isinstance(program, str)
                and program.startswith("/")
                and not os.path.exists(program)
            ):
                found.append((plist, program))
    return found


def cmd_report(cfg, _args):
    rows = processes()
    free, total = disk()
    print(f"Dysk: {human(free)} wolne z {human(total)}")
    swap = run(["sysctl", "-n", "vm.swapusage"]) or ""
    print(f"Swap: {swap.strip()}")

    print("\nNajwięcej CPU teraz:")
    for pid, _ppid, _age, cpu, rss, command in sorted(rows, key=lambda r: -r[3])[:10]:
        print(f"  {cpu:5.1f}%  {human(rss):>8}  {pid:>6}  {command[:90]}")
    spot = sum(
        r[3]
        for r in rows
        if re.search(r"/(mds|mds_stores|mdworker_shared|mdworker)\b", r[5])
    )
    print(f"\nSpotlight (mds, mdworker) łącznie: {spot:.0f}% CPU")

    print("\nNajwięcej pamięci:")
    for pid, _ppid, _age, _cpu, rss, command in sorted(rows, key=lambda r: -r[4])[:8]:
        print(f"  {human(rss):>8}  {pid:>6}  {command[:90]}")

    dev = [
        r
        for r in rows
        if r[1] == 1
        and re.search(r"next dev|vite|webpack serve|turbo run dev|expo start", r[5])
    ]
    if dev:
        print(
            "\nDev serwery bez rodzica (terminal albo agent, który je puścił, już nie żyje):"
        )
        for pid, _ppid, age, _cpu, rss, command in dev:
            print(
                f"  {pid:>6}  od {age // HOUR}h {age % HOUR // 60}min  {human(rss):>8}  {command[:80]}"
            )
        print("  Zabij niepotrzebne: kill <pid>")

    sw = Sweep(cfg, dry_run=True)
    indexed = spotlight_indexed(Scan(sw))
    if indexed:
        print("\nSpotlight indeksuje node_modules (wyklucz: claude-acc mac spotlight):")
        for project, count in indexed:
            print(f"  {count:>8} plików .js  {short(project)}")

    stale = leftovers()
    if stale:
        print(
            "\nDane po odinstalowanych aplikacjach (sprawdź i przenieś do Kosza: trash <ścieżka>):"
        )
        for path, size in stale:
            print(f"  {human(size):>9}  {short(path)}")

    broken = broken_launch_items()
    if broken:
        print("\nWpisy launchd bez programu (aplikacji już nie ma):")
        for plist, program in broken:
            print(f"  {short(plist)} -> {program}")
        print(
            "  Użytkownika wyłącza `claude-acc mac optimize`, systemowe `sudo janitor-root.sh`."
        )
    return 0


# ---------- strojenie ----------

# (domena, klucz, typ, wartość, po co)
TWEAKS = [
    ("com.apple.dock", "autohide-delay", "float", "0", "Dock wysuwa się bez czekania"),
    (
        "com.apple.dock",
        "autohide-time-modifier",
        "float",
        "0.15",
        "szybka animacja Docka",
    ),
    (
        "com.apple.dock",
        "expose-animation-duration",
        "float",
        "0.15",
        "szybkie Mission Control",
    ),
    (
        "com.apple.dock",
        "launchanim",
        "bool",
        "false",
        "ikony nie podskakują przy starcie aplikacji",
    ),
    (
        "NSGlobalDomain",
        "NSAutomaticWindowAnimationsEnabled",
        "bool",
        "false",
        "okna bez animacji otwierania",
    ),
    (
        "NSGlobalDomain",
        "NSWindowResizeTime",
        "float",
        "0.001",
        "arkusze i zmiana rozmiaru bez animacji",
    ),
    (
        "com.apple.finder",
        "DisableAllAnimations",
        "bool",
        "true",
        "Finder bez animacji (po restarcie Findera)",
    ),
    (
        "com.apple.finder",
        "FXRemoveOldTrashItems",
        "bool",
        "true",
        "Kosz sam usuwa pliki po 30 dniach",
    ),
    (
        "com.apple.desktopservices",
        "DSDontWriteNetworkStores",
        "bool",
        "true",
        "bez .DS_Store na dyskach sieciowych",
    ),
    (
        "com.apple.desktopservices",
        "DSDontWriteUSBStores",
        "bool",
        "true",
        "bez .DS_Store na pendrive'ach",
    ),
]


def defaults_read(domain, key):
    out = run(["defaults", "read", domain, key])
    return None if out is None else out.strip()


def same_value(current, kind, value):
    if current is None:
        return False
    if kind == "bool":
        return (
            current in ("1", "true", "YES")
            if value == "true"
            else current in ("0", "false", "NO")
        )
    if kind == "float":
        try:
            return abs(float(current) - float(value)) < 1e-9
        except ValueError:
            return False
    return current == value


def cmd_optimize(cfg, args):
    if "--undo" in args:
        return optimize_undo()
    dry_run = "--dry-run" in args
    backup = load_json(BACKUP_PATH, {"defaults": {}, "agents": []})
    changed = []
    for domain, key, kind, value, why in TWEAKS:
        current = defaults_read(domain, key)
        if same_value(current, kind, value):
            continue
        changed.append(why)
        if dry_run:
            continue
        backup["defaults"].setdefault(
            f"{domain} {key}",
            {"domain": domain, "key": key, "kind": kind, "value": current},
        )
        run(["defaults", "write", domain, key, f"-{kind}", value])

    parked = os.path.join(HOME, f"launchd-disabled-{datetime.now():%Y-%m-%d}")
    agents = [(p, prog) for p, prog in broken_launch_items() if p.startswith(HOME)]
    for plist, program in agents:
        changed.append(
            f"wyłączony wpis bez programu: {os.path.basename(plist)} ({program})"
        )
        if dry_run:
            continue
        subprocess.run(
            ["launchctl", "bootout", f"gui/{os.getuid()}", plist], capture_output=True
        )
        os.makedirs(parked, exist_ok=True)
        target = os.path.join(parked, os.path.basename(plist))
        shutil.move(plist, target)
        backup["agents"].append({"from": plist, "to": target})

    if not changed:
        print("System jest już nastrojony")
        return 0
    print("Zmieniłbym:" if dry_run else "Zmienione:")
    for why in changed:
        print(f"  {why}")
    if not dry_run:
        write_json(BACKUP_PATH, backup, indent=1)
        subprocess.run(["killall", "Dock"], capture_output=True)
        log(f"optimize: {len(changed)} zmian")
        print("Cofnięcie: claude-acc mac optimize --undo")
    return 0


def optimize_undo():
    backup = load_json(BACKUP_PATH, None)
    if not backup:
        print("Nie ma czego cofać")
        return 0
    for entry in backup.get("defaults", {}).values():
        if entry["value"] is None:
            run(["defaults", "delete", entry["domain"], entry["key"]])
        else:
            run(
                [
                    "defaults",
                    "write",
                    entry["domain"],
                    entry["key"],
                    f"-{entry['kind']}",
                    entry["value"],
                ]
            )
    for agent in backup.get("agents", []):
        if os.path.exists(agent["to"]) and not os.path.exists(agent["from"]):
            shutil.move(agent["to"], agent["from"])
            subprocess.run(
                ["launchctl", "bootstrap", f"gui/{os.getuid()}", agent["from"]],
                capture_output=True,
            )
    os.remove(BACKUP_PATH)
    subprocess.run(["killall", "Dock"], capture_output=True)
    log("optimize: cofnięte")
    print("Cofnięte")
    return 0


COMMANDS = {
    "sweep": cmd_sweep,
    "status": cmd_status,
    "report": cmd_report,
    "spotlight": cmd_spotlight,
    "optimize": cmd_optimize,
}


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
