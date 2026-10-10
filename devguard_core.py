"""Strażnik dev serwerów: pomiar, decyzje i akcje.

Wejściem jest devguard.py (komendy, opis i szybka ścieżka hooka `admit`); ten moduł się
importuje, więc jego bajtkod idzie z __pycache__, a nie z kompilacji przy każdym starcie.
"""

import os
import sys

# bajtkod tylko w $STATE: obok skryptu w paczce Poda (Pod.app/Contents/Resources/claude-acc) __pycache__
# łamie pieczęć aplikacji, czymkolwiek i z jakimikolwiek flagami ten plik uruchomić (1.31.6)
if not os.path.realpath(__file__).startswith(os.path.realpath(os.path.expanduser("~/.local/share/claude-acc")) + "/"):
    sys.dont_write_bytecode = True

# Python 3.15 (PEP 810) ładuje je dopiero przy pierwszym użyciu, a starsze pomijają tę nazwę.
# ctypes zostaje: libc i struktury niżej powstają przy imporcie; json i subprocess ładuje janitor.
__lazy_modules__ = ["shlex", "socket", "urllib.parse", "uuid"]

import ctypes
import ctypes.util
import fcntl
import fnmatch
import json
import plistlib
import re
import shlex
import signal
import socket
import subprocess
import time
import uuid
from urllib.parse import urlsplit

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import janitor
import orcahost
from devguard import __doc__ as USAGE
from devguard import dev_starts, sched_rewrite

STATE_DIR = janitor.STATE_DIR
CONFIG_PATH = os.path.join(STATE_DIR, "devguard.json")
STATE_PATH = os.path.join(STATE_DIR, "devguard-state.json")
LOG_PATH = os.path.join(STATE_DIR, "devguard.log")
LOCK_PATH = os.path.join(STATE_DIR, "devguard.lock")
# stan strażnika fseventsd (fsguard.py, root): kiedy ostatnio zrestartował demona zdarzeń plików
FSGUARD_STATE = os.environ.get("CLAUDE_ACC_FSGUARD_STATE", "/var/db/claude-acc-fsguard.json")
# wyjątki dodane z terminala (`pin`): osobny plik, który pętla czyta przy każdym pomiarze
PINS_PATH = os.path.join(STATE_DIR, "devguard-pins.json")

GB = janitor.GB
MB = 1024**2
MINUTE = 60
HOUR = janitor.HOUR

DEFAULT_CONFIG = {
    # "observe": status i log bez zatrzymywania i restartów
    "mode": "enforce",
    "interval_seconds": 5,
    # karty, terminale i agenci z Orki odświeżani co tyle sekund
    "orca_seconds": 10,
    # łączny footprint dev serwerów jako procent RAM; powyżej strażnik zwalnia największy
    "budget_percent": 35,
    # pojedynczy serwer większy niż tyle GB jest spuchnięty: restart przy pierwszej ciszy
    "max_server_gb": 5,
    # swap jako procent RAM: "warn" zwalnia serwery bez podglądu i spuchnięte,
    # "critical" (przy trwającym swapowaniu) także te oglądane przez agentów
    "swap_warn_percent": 12,
    "swap_critical_percent": 20,
    # kern.memorystatus_level: procent pamięci, którą jądro uważa za dostępną
    "available_critical_percent": 10,
    "available_warn_percent": 20,
    # czy liczyć się z poziomem presji jądra (kern.memorystatus_vm_pressure_level)
    "kernel_pressure": True,
    # swap urósł o tyle MB w ostatnich 2 minutach: system właśnie wypycha pamięć
    "swapping_mb": 256,
    # albo kompresor wrzucił do swapu tyle segmentów w tym samym oknie
    "swapouts_burst": 1000,
    # serwer młodszy niż tyle minut jest nietykalny (agent właśnie go postawił)
    "grace_minutes": 3,
    # tyle sekund bez CPU i bez wyjścia w terminalu, zanim strażnik ruszy oglądany serwer
    "quiet_seconds": 30,
    # drugi serwer tej samej aplikacji po tylu minutach bez klientów
    "duplicate_minutes": 5,
    # serwer, którego agent albo terminal już nie żyje
    "orphan_minutes": 10,
    # bez klientów i bez ruchu; razy dwa, gdy agent w tym worktree pracuje
    "idle_minutes": 45,
    # przerwa po każdej akcji, żeby pamięć zdążyła opaść przed kolejną oceną
    "cooldown_seconds": 45,
    # więcej restartów jednej aplikacji w godzinę to pętla HMR: zamiast restartu stop
    "max_recycles_per_hour": 2,
    # serwery, na które nie patrzysz, chodzą w QoS tła (rdzenie E, dławione IO)
    "background_unattended": True,
    "close_tabs": True,
    "orca_comment": True,
    "notify": True,
    # ścieżki (katalog serwera albo nad nim) i porty, których strażnik nie dotyka
    "protect": [],
    # gdy niepuste: strażnik widzi tylko serwery z tych katalogów
    "scope": [],
    # interpretery, pod którymi chodzą dev serwery
    "runtimes": ["node", "bun", "deno"],
    # limity katalogów z wynikami agentów (janitor caps) sprawdzane co tyle minut; 0 wyłącza
    "caps_minutes": 10,
    # przy krytycznej presji, gdy żaden dev serwer nie zostaje do zatrzymania: sieroty,
    # headless przeglądarki, gopls i przebiegi testów agentów (lastresort.py)
    "last_resort": True,
    # serwer zatrzymany z braku pamięci zostaje zatrzymany najwyżej tyle minut: hook nie wpuszcza
    # go z powrotem, dopóki presja nie zejdzie do zera, a swap pod próg ostrzeżenia
    "restart_hold_minutes": 10,
    # symulatory iOS (każdy to 2-4 GB): najwyżej tyle włączonych naraz; ponad limit strażnik
    # wyłącza nieużywane, a `portivo-mobile up` sesji bez symulatora czeka w schedulerze
    "max_booted_simulators": 2,
    # symulator z puli bez żywej dzierżawy i bez widzów wyłączany po tylu minutach
    "simulator_idle_minutes": 30,
    # ponad limitem wystarczy tyle minut bez używania
    "simulator_quiet_minutes": 5,
    # symulatory agentów (pula portivo-mobile); pozostałe są Twoje: liczą się do limitu, ale
    # strażnik ich nie wyłącza
    "simulator_pool_prefix": "Portivo-",
    # dzierżawy portivo-mobile: <udid>.json z procesem sesji, która trzyma symulator
    "simulator_leases": "~/.cache/portivo-mobile/leases",
    # tyle rdzeni CPU całego symulatora to używanie. Pomiar 2026-10-08: bezczynny symulator
    # z aplikacją RN 0,01 rdzenia, ten sam pod flow maestro 0,4-0,7
    "simulator_busy_cores": 0.15,
    # nazwy (także wzorce z * i ?) albo UDID symulatorów, których strażnik nigdy nie wyłącza.
    # Portivo-Perf-*: sesje, które mierzą wydajność aplikacji iOS, trzymają symulator długo bez
    # dzierżawy; wyłączenie w środku pomiaru zabiera im urządzenie i jego stan. Własna lista
    # zastępuje tę, więc wzorzec trzeba w niej powtórzyć.
    "simulator_protect": ["Portivo-Perf-*"],
}

SERVER_KINDS = [
    ("next", re.compile(r"/next(/dist/bin/next)?\s+dev\b")),
    ("vite", re.compile(r"/vite(/bin/vite\.js)?(\s+(dev|serve)\b|\s+--|\s*$)")),
    ("expo", re.compile(r"/expo(/bin/cli)?\s+start\b")),
    ("webpack", re.compile(r"/webpack(-cli)?(/bin/cli\.js)?\s+serve\b")),
    ("astro", re.compile(r"/astro(\.js)?\s+dev\b")),
    ("storybook", re.compile(r"/storybook(/bin/index\.c?js)?\s+dev\b")),
    ("nuxt", re.compile(r"/nuxi?(\.mjs)?\s+dev\b")),
]
# procesy między powłoką a serwerem: to, co wpisał człowiek albo agent
LAUNCHER = re.compile(
    r"^(\S*/)?(pnpm|npm|npx|yarn|bun|bunx|rtk|turbo|corepack|nohup)(\s|$)"
    r"|^(\S*/)?node\s+\S*/(pnpm|npm|npx|yarn|turbo)(\.c?js)?(\s|$)"
    r"|^(/bin/)?(ba|z)?sh\s+-c\s"
)
SHELL = re.compile(r"^-?(\S*/)?(zsh|bash|fish|sh)(\s+-[a-z]+)*\s*$")
AGENT = re.compile(r"(^|/)(claude|codex)(\s|$)")
# tego nie zabijamy nigdy, nawet gdyby trafiło do drzewa serwera; aplikacje hosta (Orca, Pod) daje orcahost
SACRED = re.compile(
    r"(^|/)(claude|codex|login|launchd)(\s|$)|" + orcahost.APPS + r"|^-?(\S*/)?(zsh|bash|fish)(\s|$)"
)
# proces z pakietu hosta: główny, pomocnicy (karty podglądu), CLI
HOST_APP = re.compile(orcahost.APPS)
BROWSER = re.compile(
    r"Brave Browser|Google Chrome(?! for Testing)|Safari|com\.apple\.WebKit|firefox|Arc\.app|Microsoft Edge|Chromium|Vivaldi|Opera"
)
HEADLESS = re.compile(
    r"Chrome for Testing|HeadlessChrome|headless_shell|chrome-headless-shell|ms-playwright|puppeteer"
)
LOOPBACK = {"127.0.0.1", "[::1]", "::1", "localhost", "0.0.0.0", "*"}
# proces z danych symulatora (aplikacja w nim, launchd_sim): UDID urządzenia z jego ścieżki
UDID = re.compile(r"[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}")
SIM_DEVICE = re.compile(rf"/CoreSimulator/Devices/({UDID.pattern})/")
LAUNCHD_SIM = re.compile(r"(^|/)launchd_sim(\s|$)")
# podgląd symulatora w Orce; bez UDID w argumentach ogląda każdy włączony
SERVE_SIM = re.compile(r"(^|/)serve-sim(\s|$)")
# `simctl io booted recordVideo`, `simctl spawn booted log stream`: alias każdego włączonego
SIMCTL_BOOTED = re.compile(r"(^|/|\s)simctl\s.*\bbooted\b")
# zostaje na urządzeniu po maestro; tak jak portivo-mobile nie liczymy go jako aplikacji w użyciu
MAESTRO_DRIVER = "dev.mobile.maestro-driver-iosUITests.xctrunner"
SIM_DEVICES = os.path.join(janitor.HOME, "Library/Developer/CoreSimulator/Devices")
SIMULATOR_APP = "com.apple.iphonesimulator"


def log(line):
    janitor.log(line, path=LOG_PATH)


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(janitor.load_json(CONFIG_PATH, {}))
    return cfg


def short(path):
    return janitor.short(path).replace("/Documents/", "/")


# ---------- odczyty z jądra ----------

_libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)


class _Timebase(ctypes.Structure):
    _fields_ = [("numer", ctypes.c_uint32), ("denom", ctypes.c_uint32)]


class _RusageV4(ctypes.Structure):
    """struct rusage_info_v4 z <sys/resource.h>."""

    _fields_ = [("uuid", ctypes.c_uint8 * 16)] + [
        (name, ctypes.c_uint64)
        for name in (
            "user_time",
            "system_time",
            "pkg_idle_wkups",
            "interrupt_wkups",
            "pageins",
            "wired_size",
            "resident_size",
            "phys_footprint",
            "proc_start_abstime",
            "proc_exit_abstime",
            "child_user_time",
            "child_system_time",
            "child_pkg_idle_wkups",
            "child_interrupt_wkups",
            "child_pageins",
            "child_elapsed_abstime",
            "diskio_bytesread",
            "diskio_byteswritten",
            "qos_default",
            "qos_maintenance",
            "qos_background",
            "qos_utility",
            "qos_legacy",
            "qos_user_initiated",
            "qos_user_interactive",
            "billed_system_time",
            "serviced_system_time",
            "logical_writes",
            "lifetime_max_phys_footprint",
            "instructions",
            "cycles",
            "billed_energy",
            "serviced_energy",
            "interval_max_phys_footprint",
            "runnable_time",
        )
    ]


class _SwapUsage(ctypes.Structure):
    _fields_ = [
        ("total", ctypes.c_uint64),
        ("avail", ctypes.c_uint64),
        ("used", ctypes.c_uint64),
        ("pagesize", ctypes.c_uint32),
        ("encrypted", ctypes.c_bool),
    ]


_tb = _Timebase()
_libc.mach_timebase_info(ctypes.byref(_tb))
TICK_NS = _tb.numer / _tb.denom if _tb.denom else 1.0
_libc.mach_absolute_time.restype = ctypes.c_uint64
RUSAGE_INFO_V4 = 4
PROC_PIDVNODEPATHINFO = 9
VNODEPATHINFO_SIZE = 2352  # dwa vnode_info_path: 152 bajty vnode_info + MAXPATHLEN
KERN_PROCARGS2 = 49


def started_at(abstime, first_seen, now):
    """Epoka startu procesu. Zegar jądra (mach_absolute_time) stoi w czasie snu, więc wynik
    z niego wypada za późno; pierwsza obserwacja strażnika też, więc bierzemy wcześniejszy."""
    if not abstime:
        return first_seen
    awake = (_libc.mach_absolute_time() - abstime) * TICK_NS / 1e9
    return min(first_seen, now - awake)


def usage(pid):
    """Footprint, szczyt, CPU w sekundach, zapisy na dysk i start procesu; None, gdy go nie ma."""
    info = _RusageV4()
    if _libc.proc_pid_rusage(pid, RUSAGE_INFO_V4, ctypes.byref(info)) != 0:
        return None
    return {
        "footprint": info.phys_footprint,
        "peak": info.lifetime_max_phys_footprint,
        "cpu": (info.user_time + info.system_time) * TICK_NS / 1e9,
        "written": info.diskio_byteswritten,
        "start": info.proc_start_abstime,
    }


def proc_cwd(pid):
    buf = ctypes.create_string_buffer(VNODEPATHINFO_SIZE)
    got = _libc.proc_pidinfo(
        pid, PROC_PIDVNODEPATHINFO, ctypes.c_uint64(0), buf, VNODEPATHINFO_SIZE
    )
    if got != VNODEPATHINFO_SIZE:
        return None
    return buf.raw[152:1176].split(b"\0", 1)[0].decode(errors="replace") or None


# argumenty prawie każdego procesu mieszczą się w jednym odczycie; dłuższe idą drugim
_args = ctypes.create_string_buffer(64 * 1024)


def procargs(pid):
    """Argumenty procesu z KERN_PROCARGS2: [argc, ścieżka, argv...]; None dla cudzego albo zombie."""
    raw = _procargs_raw(pid)
    if raw is None:
        return None
    argc = int.from_bytes(raw[:4], "little")
    _exe, _, rest = raw[4:].partition(b"\0")
    return [argc] + rest.lstrip(b"\0").split(b"\0")[:argc]


def proc_env(pid):
    """Środowisko procesu ({nazwa: wartość}) z tego samego bloku KERN_PROCARGS2, który trzyma
    argumenty; {} dla cudzego albo zombie. Po środowisku jądro dokłada własne zmienne Apple
    (executable_path=...) za pustym wpisem, więc czytamy tylko do niego."""
    raw = _procargs_raw(pid)
    if raw is None:
        return {}
    argc = int.from_bytes(raw[:4], "little")
    _exe, _, rest = raw[4:].partition(b"\0")
    env = {}
    for item in rest.lstrip(b"\0").split(b"\0")[argc:]:
        if not item:
            break
        name, sep, value = item.partition(b"=")
        if sep:
            env[name.decode(errors="replace")] = value.decode(errors="replace")
    return env


def _procargs_raw(pid):
    """Surowy blok KERN_PROCARGS2 (argc, ścieżka, argv, środowisko); None dla cudzego albo zombie."""
    mib = (ctypes.c_int * 3)(1, KERN_PROCARGS2, pid)
    size = ctypes.c_size_t(len(_args))
    if _libc.sysctl(mib, 3, _args, ctypes.byref(size), None, 0):
        return None
    if size.value < len(_args):
        raw = _args[: size.value]
    else:
        # pełny bufor może znaczyć ucięte argumenty (jądro tnie po cichu): rozmiar i drugi odczyt
        size = ctypes.c_size_t(0)
        if _libc.sysctl(mib, 3, None, ctypes.byref(size), None, 0) or not size.value:
            return None
        buf = ctypes.create_string_buffer(size.value)
        if _libc.sysctl(mib, 3, buf, ctypes.byref(size), None, 0):
            return None
        raw = buf[: size.value]
    return raw if len(raw) >= 4 else None


def proc_argv(pid):
    """Dokładne argv procesu z KERN_PROCARGS2; ps skleja argumenty spacjami i gubi cudzysłowy."""
    args = procargs(pid)
    if not args or len(args) - 1 != args[0]:
        return None
    return [a.decode(errors="replace") for a in args[1:]]


KERN_PROC = 14
KERN_PROC_ALL = 0
# struct kinfo_proc z <sys/sysctl.h> i offsetof jego pól (arm64 i x86_64 tak samo)
KINFO_SIZE = 648
KP_STAT, KP_PID, KP_COMM, KP_UID, KP_PPID, KP_TDEV = 36, 40, 243, 420, 560, 572
# znaki sterujące w argumentach tak, jak pokazuje je ps: tab i nowa linia ósemkowo, reszta ^X
PS_VIS = {c: "^" + chr(c + 64) for c in range(32)}
PS_VIS.update({9: "\\011", 10: "\\012", 127: "^?"})


def processes():
    """[(pid, ppid, komenda)] wszystkich procesów: to samo, co `ps -axo pid=,ppid=,command=`,
    tylko z jednego sysctl KERN_PROC_ALL i KERN_PROCARGS2 na proces, bez forkowania ps
    (35 ms CPU na każdy pomiar). Jedna różnica: ps ma setuid root i widzi argumenty
    procesów innych użytkowników (root, _windowserver), a my, jak każdy bez roota, tylko
    ich nazwę; dostają ją w nawiasach, tak jak u ps proces, którego argumentów nie ma."""
    mib = (ctypes.c_int * 3)(1, KERN_PROC, KERN_PROC_ALL)
    raw = b""
    for _ in range(5):
        size = ctypes.c_size_t(0)
        if _libc.sysctl(mib, 3, None, ctypes.byref(size), None, 0):
            return []
        # zapas na procesy, które powstaną między pytaniem o rozmiar a odczytem: przy
        # kompilacjach Go potrafi ich przybyć setki, a pusta tabela to tick bez serwerów
        size.value += max(64 * KINFO_SIZE, size.value // 4)
        buf = ctypes.create_string_buffer(size.value)
        if not _libc.sysctl(mib, 3, buf, ctypes.byref(size), None, 0):
            raw = buf[: size.value]
            break
    uid = os.getuid()
    found = []
    for off in range(0, len(raw) - KINFO_SIZE + 1, KINFO_SIZE):
        pid = int.from_bytes(raw[off + KP_PID : off + KP_PID + 4], "little")
        if not pid:
            continue  # kernel_task: ps -ax go nie pokazuje
        ppid = int.from_bytes(raw[off + KP_PPID : off + KP_PPID + 4], "little")
        owner = int.from_bytes(raw[off + KP_UID : off + KP_UID + 4], "little")
        # argumenty cudzego procesu jądro i tak odmawia (EINVAL): bez pytania
        args = procargs(pid) if uid == 0 or owner == uid else None
        if args and len(args) > 1:
            command = b" ".join(args[1:]).decode(errors="replace").translate(PS_VIS)
        elif raw[off + KP_STAT] == SZOMB:
            command = "<defunct>"
        else:
            comm = raw[off + KP_COMM : off + KP_COMM + 17].split(b"\0", 1)[0]
            command = f"({comm.decode(errors='replace')})"
        tdev = int.from_bytes(
            raw[off + KP_TDEV : off + KP_TDEV + 4], "little", signed=True
        )
        found.append((tdev, pid, ppid, command))
    # kolejność ps: terminal (bez terminala pierwsze), potem pid; od niej zależy kolejność
    # serwerów w jednostce, a więc i to, który z nich daje jej nazwę
    found.sort(key=lambda row: row[:2])
    return [(pid, ppid, command) for _tdev, pid, ppid, command in found]


def sysctl_int(name):
    value = ctypes.c_uint64(0)
    size = ctypes.c_size_t(8)
    if _libc.sysctlbyname(
        name.encode(), ctypes.byref(value), ctypes.byref(size), None, 0
    ):
        return None
    return value.value & ((1 << (8 * size.value)) - 1)


def swap_usage():
    info = _SwapUsage()
    size = ctypes.c_size_t(ctypes.sizeof(info))
    if _libc.sysctlbyname(
        b"vm.swapusage", ctypes.byref(info), ctypes.byref(size), None, 0
    ):
        return 0, 0
    return info.total, info.used


PROC_PIDTBSDINFO = 3
BSDINFO_SIZE = 136
SZOMB = 5


def alive(pid, start):
    """Czy to wciąż ten sam, żywy proces: pid mógł zostać użyty ponownie, a zombie,
    którego rodzic jeszcze nie odebrał, rusage dalej widzi (bsdinfo już nie)."""
    buf = ctypes.create_string_buffer(BSDINFO_SIZE)
    got = _libc.proc_pidinfo(
        pid, PROC_PIDTBSDINFO, ctypes.c_uint64(0), buf, BSDINFO_SIZE
    )
    if got != BSDINFO_SIZE or int.from_bytes(buf.raw[4:8], "little") == SZOMB:
        return False
    info = usage(pid)
    return info is not None and info["start"] == start


PRIO_DARWIN_PROCESS = 4
PRIO_DARWIN_BG = 0x1000
# pid -> (start procesu, czy w tle); jądro nie zdradza tej flagi dla cudzego procesu
_background = {}


def set_background(pid, start, on):
    """QoS tła dla procesu (to samo co `taskpolicy -b`), albo powrót do zwykłego."""
    if _background.get(pid) == (start, on):
        return
    try:
        os.setpriority(PRIO_DARWIN_PROCESS, pid, PRIO_DARWIN_BG if on else 0)
    except OSError:
        return
    _background[pid] = (start, on)


def restore_background():
    """Przy wyjściu strażnika nic nie może zostać przyklejone do rdzeni E."""
    for pid, (start, on) in list(_background.items()):
        if on and alive(pid, start):
            set_background(pid, start, False)


# ---------- presja pamięci ----------


class Pressure:
    """Czy Macowi brakuje pamięci teraz, a nie czy kiedyś brakowało.

    Swap na macOS opada dopiero, gdy ktoś dotknie wypchniętych stron, więc sama jego
    wielkość po zwolnieniu pamięci długo straszy. Krytycznie jest dopiero wtedy, gdy swap
    jest duży i dalej rośnie albo licznik swapoutów kompresora idzie w górę.
    """

    def __init__(self, cfg, state, now):
        self.ram = sysctl_int("hw.memsize") or 16 * GB
        self.kernel = sysctl_int("kern.memorystatus_vm_pressure_level") or 1
        # poziom jądra jest tylko dodatkiem; testy i ci, którym jego ostrzeżenia hałasują,
        # wyłączają go w konfiguracji
        kernel = self.kernel if cfg.get("kernel_pressure", True) else 1
        self.available = sysctl_int("kern.memorystatus_level")
        self.swap_total, self.swap_used = swap_usage()
        self.compressed = sysctl_int("vm.compressor_bytes_used") or 0
        # segmenty kompresora i ich limit: przy 98% limitu jądro ogłasza "compressor space
        # shortage" (zamrożenie 08.10); na starszym macOS tych nazw nie ma i hamulec ich nie liczy
        self.segments = sysctl_int("vm.compressor.segment.total")
        self.segments_limit = sysctl_int("vm.compressor.segment.limit")
        swapouts = sysctl_int("vm.compressor.compactor.swapouts_queued_pressure")
        history = [h for h in state.get("swap_history", []) if now - h[0] <= 120]
        history.append([now, self.swap_used, swapouts])
        state["swap_history"] = history
        first = history[0]
        self.swap_growth = self.swap_used - first[1]
        self.swapouts = (
            (swapouts - first[2])
            if swapouts is not None and first[2] is not None
            else 0
        )
        # swap rośnie albo kompresor wypycha strony seriami: system dusi się teraz
        self.swapping = (
            self.swap_growth >= cfg["swapping_mb"] * MB
            or self.swapouts >= cfg["swapouts_burst"]
        )
        warn = cfg["swap_warn_percent"] / 100 * self.ram
        critical = cfg["swap_critical_percent"] / 100 * self.ram
        available = self.available if self.available is not None else 100
        self.reasons = []
        self.notes = []
        if kernel >= 4:
            self.reasons.append("jądro: presja krytyczna")
        if available <= cfg["available_critical_percent"]:
            self.reasons.append(f"dostępne tylko {available}% pamięci")
        if self.swap_used >= critical and self.swapping:
            self.reasons.append(f"swap {janitor.human(self.swap_used)} i rośnie")
        if self.reasons:
            self.level = 2
            self._stage(cfg, kernel)
            return
        if kernel >= 2:
            self.reasons.append("jądro: ostrzeżenie o presji")
        if available <= cfg["available_warn_percent"]:
            self.reasons.append(f"dostępne {available}% pamięci")
        if self.swap_used >= warn and self.swapping:
            self.reasons.append(f"swap {janitor.human(self.swap_used)} i rośnie")
        self.level = 1 if self.reasons else 0
        if not self.swapping and self.swap_used >= warn:
            # pełny swap, który stoi, to ślad po dawnej presji: strony wracają dopiero
            # przy dotknięciu, więc gaszenie serwerów niczego tu nie zwolni
            self.notes.append(f"swap {janitor.human(self.swap_used)} stoi")
        self._stage(cfg, kernel)

    def _stage(self, cfg, kernel):
        """Stopień hamulca (lastresort.stage): 0 spokój, 1 ciasno, 2 hamulec, 3 awaria."""
        import lastresort

        self.stage, self.stage_reasons = lastresort.stage(
            {
                "ram": self.ram,
                "compressed": self.compressed,
                "segments": self.segments,
                "segments_limit": self.segments_limit,
                "swap_used": self.swap_used,
                "swap_growth": self.swap_growth,
                "kernel": kernel,
                "available": self.available,
                "guard_level": self.level,
            },
            cfg,
        )

    def summary(self):
        return {
            "stage": self.stage,
            "stage_reasons": self.stage_reasons,
            "segments": self.segments,
            "segments_limit": self.segments_limit,
            "level": self.level,
            "reasons": self.reasons,
            "notes": self.notes,
            "kernel": self.kernel,
            "available": self.available,
            "swap_used": self.swap_used,
            "swap_total": self.swap_total,
            "swap_growth": self.swap_growth,
            "swapping": self.swapping,
            "compressed": self.compressed,
            "ram": self.ram,
        }


# ---------- Orca ----------


def port_of(url):
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        if host in ("localhost", "127.0.0.1", "::1", "0.0.0.0") or host.endswith(
            ".localhost"
        ):
            return parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError:
        pass
    return None


# odczyty, które pętla powtarza co kilka sekund: komenda CLI -> (metoda RPC, parametry), czyli
# dokładnie to, co dla tych komend z --json wysyła samo CLI Orki (out/cli/handlers)
ORCA_READS = {
    ("tab", "list", "--worktree", "all"): ("browser.tabList", {}),
    ("worktree", "ps"): ("worktree.ps", {}),
    ("terminal", "list"): ("terminal.list", {"includeVisualLayouts": False}),
    ("diagnostics", "memory"): ("diagnostics.memory", None),
}


class Orca:
    """Karty podglądu, worktree, agenci i terminale z działającej Orki.

    Odczyty idą gniazdem, którego używa jej CLI, zmiany (zamknięcie karty, komentarz,
    komenda w terminalu) przez samo CLI. Orki nie uruchamia: gdy aplikacja nie chodzi,
    strażnik działa bez tej wiedzy.
    """

    def __init__(self):
        self.host = None
        self.attach(orcahost.host())
        # gniazdo tylko przy prawdziwym CLI i lokalnej Orce: podróbka w testach i Orka
        # zdalna (parowanie, środowisko) idą zawsze przez CLI
        self.direct = "DEVGUARD_ORCA" not in os.environ and not any(
            os.environ.get(name) for name in orcahost.REMOTE_ENV
        )
        self.ok = False
        self.at = 0
        self.sessions_at = 0
        self.tabs = []
        self.worktrees = []
        self.terminals = []
        self.sessions = {}  # pid procesu terminala (login) -> ptyId

    def attach(self, host):
        """Orca albo Pod (orcahost.py): CLI, gniazdo z orca-runtime.json i proces główny. Odświeżenie
        woła to co cykl, więc przejście z Orki na Pod nie wymaga restartu strażnika."""
        if host == self.host:
            return
        self.host = host
        # DEVGUARD_ORCA podmienia CLI (testy); pusta wartość wyłącza Orkę
        self.bin = os.environ.get("DEVGUARD_ORCA", orcahost.cli_path(host, janitor.which))
        self.metadata = orcahost.runtime_path(host)
        self.marker = orcahost.main_marker(host)

    def call(self, *args, timeout=10):
        if not self.bin:
            return None
        read = ORCA_READS.get(args) if self.direct else None
        data = self.rpc(*read, timeout=timeout) if read else None
        if data is None:
            try:
                done = subprocess.run(
                    [self.bin, *args, "--json"],
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    env=janitor.ENV,
                )
                data = json.loads(done.stdout)
            except (OSError, subprocess.TimeoutExpired, ValueError):
                return None
        return data.get("result") if isinstance(data, dict) and data.get("ok") else None

    def rpc(self, method, params, timeout=10):
        """Odpowiedź Orki (jak z CLI z --json) wprost z gniazda, którego używa jej CLI; None
        przy każdym kłopocie, wtedy idzie CLI. CLI to Electron startujący na każde wywołanie:
        ~150 ms CPU, a pętla robiła ich 19 na minutę; gniazdo to ułamek milisekundy."""
        try:
            with open(self.metadata) as f:
                meta = json.load(f)
            transports = meta.get("transports")
            if not isinstance(transports, list):
                transports = [meta.get("transport") or {}]
            endpoint = next(
                (
                    t["endpoint"]
                    for t in transports
                    if t.get("kind") in ("unix", "named-pipe")
                ),
                None,
            )
            if not endpoint:
                return None
            request = {
                "id": str(uuid.uuid4()),
                "authToken": meta["authToken"],
                "method": method,
            }
            if params is not None:
                request["params"] = params
            deadline = time.time() + timeout
            with socket.socket(socket.AF_UNIX) as conn:
                conn.settimeout(timeout)
                conn.connect(endpoint)
                conn.sendall(json.dumps(request).encode() + b"\n")
                buf = bytearray()
                while True:
                    end = buf.find(b"\n")
                    if end < 0:
                        chunk = conn.recv(1 << 16) if time.time() < deadline else b""
                        if not chunk:
                            return None
                        buf += chunk
                        continue
                    line, buf = bytes(buf[:end]), buf[end + 1 :]
                    if not line.strip():
                        continue
                    frame = json.loads(line)
                    if frame.get("_keepalive"):
                        continue
                    runtime = (frame.get("_meta") or {}).get("runtimeId")
                    # jak CLI: cudza odpowiedź albo Orka podmieniona w trakcie to błąd
                    if frame.get("id") != request["id"] or (
                        runtime and runtime != meta.get("runtimeId")
                    ):
                        return None
                    return frame
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return None

    def refresh(self, rows, now, every, sessions_every=60):
        self.attach(orcahost.host())
        if not self.bin or not any(self.marker in command for _pid, _ppid, command in rows):
            self.ok = False
            return
        # także po nieudanym odczycie: Orka, która nie odpowiada, nie dostaje 3 wywołań co 5 s
        if self.at and now - self.at < every:
            return
        tabs = self.call("tab", "list", "--worktree", "all")
        worktrees = self.call("worktree", "ps")
        terminals = self.call("terminal", "list")
        self.ok = tabs is not None and worktrees is not None
        self.at = now
        self.tabs = [
            dict(t, port=port_of(t.get("url", "")))
            for t in (tabs or {}).get("tabs", [])
        ]
        self.worktrees = (worktrees or {}).get("worktrees", [])
        self.terminals = (terminals or {}).get("terminals", [])
        if now - self.sessions_at >= sessions_every:
            self.load_sessions()
            self.sessions_at = now

    def load_sessions(self):
        memory = self.call("diagnostics", "memory") or {}
        self.sessions = {
            s["pid"]: s["sessionId"]
            for w in memory.get("worktrees", [])
            for s in w.get("sessions", [])
            if s.get("pid")
        }

    def worktree_for(self, path):
        """Worktree, w którym leży ścieżka (najdłuższe pasujące)."""
        best = None
        for w in self.worktrees:
            root = w.get("path") or ""
            if root and (path == root or path.startswith(root.rstrip("/") + "/")):
                if best is None or len(root) > len(best["path"]):
                    best = w
        return best

    def worktree_by_id(self, wid):
        return next((w for w in self.worktrees if w.get("worktreeId") == wid), None)

    def terminal_for(self, ancestors):
        """Terminal Orki, w którym działa proces o tych przodkach."""
        for pid in ancestors:
            pty = self.sessions.get(pid)
            if pty:
                return next((t for t in self.terminals if t.get("ptyId") == pty), None)
        return None

    def focused(self, tab):
        """Karta, na którą patrzysz: aktywna w worktree wybranym w Orce."""
        worktree = self.worktree_by_id(tab.get("worktreeId"))
        return bool(tab.get("active") and worktree and worktree.get("isActive"))


# ---------- obraz świata ----------


PROC_UID_ONLY = 4
PROC_PIDLISTFDS = 1
PROC_FDINFO_SIZE = 8  # struct proc_fdinfo: fd, typ
PROX_FDTYPE_SOCKET = 2
PROC_PIDFDSOCKETINFO = 3
# struct socket_fdinfo z <sys/proc_info.h> i offsetof pól gniazda TCP
SOCKET_FDINFO_SIZE = 792
SOI_KIND, TCPSI_STATE, INSI_FPORT, INSI_LPORT = 256, 344, 264, 268
INSI_VFLAG, INSI_FADDR, INSI_LADDR = 288, 296, 312
SOCKINFO_TCP = 2
TSI_S_LISTEN = 1
TSI_S_ESTABLISHED = 4
INI_IPV4 = 1
_fds = ctypes.create_string_buffer(PROC_FDINFO_SIZE * 4096)
_socket = ctypes.create_string_buffer(SOCKET_FDINFO_SIZE)


def tcp_host(addr, ipv4):
    """Adres z in_sockinfo tak, jak pisze go `lsof -n`: 127.0.0.1, [::1], * dla dowolnego."""
    if ipv4:
        host = socket.inet_ntop(socket.AF_INET, addr[12:16])
        return "*" if host == "0.0.0.0" else host
    host = socket.inet_ntop(socket.AF_INET6, addr)
    return "*" if host == "::" else f"[{host}]"


def sockets():
    """Gniazda TCP użytkownika: ({port: {pid}}, [(pid klienta, port serwera)]).

    To samo, co dawał `lsof -nP -a -u $UID -iTCP` (76 ms CPU na pomiar), tylko wprost z
    jądra: lista deskryptorów każdego procesu i proc_pidfdinfo dla gniazd, kilka ms."""
    uid = os.getuid()
    need = _libc.proc_listpids(PROC_UID_ONLY, uid, None, 0)
    if need <= 0:
        return {}, []
    pids = (ctypes.c_int * (need // 4 + 64))()
    got = _libc.proc_listpids(PROC_UID_ONLY, uid, pids, ctypes.sizeof(pids))
    listen, links = {}, []
    # rosnąco, jak lsof: kolejność klientów w stanie zostaje ta sama
    for pid in sorted(p for p in pids[: max(got, 0) // 4] if p > 0):
        fds = _fds
        size = _libc.proc_pidinfo(
            pid, PROC_PIDLISTFDS, ctypes.c_uint64(0), fds, len(fds)
        )
        if size == len(fds):
            # pełny bufor: proces ma więcej deskryptorów, niż się zmieściło
            more = _libc.proc_pidinfo(pid, PROC_PIDLISTFDS, ctypes.c_uint64(0), None, 0)
            fds = ctypes.create_string_buffer(max(more, size) + 64 * PROC_FDINFO_SIZE)
            size = _libc.proc_pidinfo(
                pid, PROC_PIDLISTFDS, ctypes.c_uint64(0), fds, len(fds)
            )
        if size <= 0:
            continue
        table = fds[:size]
        for off in range(0, size - PROC_FDINFO_SIZE + 1, PROC_FDINFO_SIZE):
            if int.from_bytes(table[off + 4 : off + 8], "little") != PROX_FDTYPE_SOCKET:
                continue
            fd = int.from_bytes(table[off : off + 4], "little")
            if (
                _libc.proc_pidfdinfo(
                    pid, fd, PROC_PIDFDSOCKETINFO, _socket, SOCKET_FDINFO_SIZE
                )
                != SOCKET_FDINFO_SIZE
            ):
                continue
            info = _socket[:SOCKET_FDINFO_SIZE]
            if int.from_bytes(info[SOI_KIND : SOI_KIND + 4], "little") != SOCKINFO_TCP:
                continue
            state = int.from_bytes(info[TCPSI_STATE : TCPSI_STATE + 4], "little")
            if state == TSI_S_LISTEN:
                port = int.from_bytes(info[INSI_LPORT : INSI_LPORT + 2], "big")
                listen.setdefault(port, set()).add(pid)
            elif state == TSI_S_ESTABLISHED:
                ipv4 = bool(info[INSI_VFLAG] & INI_IPV4)
                local = tcp_host(info[INSI_LADDR : INSI_LADDR + 16], ipv4)
                remote = tcp_host(info[INSI_FADDR : INSI_FADDR + 16], ipv4)
                if remote in LOOPBACK or remote == local:
                    port = int.from_bytes(info[INSI_FPORT : INSI_FPORT + 2], "big")
                    links.append((pid, port))
    return listen, links


def client_kind(command):
    if SIM_DEVICE.search(command):
        return "simulator"  # aplikacja w symulatorze iOS, zwykle z Metro
    if HOST_APP.search(command):
        return "orca"  # rodzaj klienta, także dla Pod: tak czyta go aplikacja
    if HEADLESS.search(command):
        return "headless"
    if BROWSER.search(command):
        return "browser"
    return "tool"


class Server:
    """Jeden dev serwer: proces z `next dev` (albo vite, expo...) i jego dzieci."""

    def __init__(self, pid, kind, command, tree):
        self.pid = pid
        self.kind = kind
        self.command = command
        self.tree = tree
        self.cwd = proc_cwd(pid) or "?"
        stats = [s for s in (usage(p) for p in tree) if s]
        self.footprint = sum(s["footprint"] for s in stats)
        self.peak = max((s["peak"] for s in stats), default=0)
        # o ile procesy serwera mogą jeszcze urosnąć: każdy do swojego zmierzonego szczytu
        self.regrow = sum(max(0, s["peak"] - s["footprint"]) for s in stats)
        self.cpu = sum(s["cpu"] for s in stats)
        self.written = sum(s["written"] for s in stats)
        self.ports = []


class Unit:
    """To, co strażnik zatrzymuje albo restartuje: komenda wpisana w powłokę i wszystkie
    dev serwery pod nią (zwykle jeden, przy `turbo run dev` cały stos)."""

    def __init__(self, root, servers, table, children):
        self.root = root
        self.servers = servers
        # wszystko, co postawiła ta komenda: pnpm, rtk, turbo i jego pozostałe zadania
        self.pids = sorted(descendants(root, children))
        info = usage(root) or {}
        self.start = info.get("start", 0)
        self.key = f"{root}:{self.start}"
        self.argv = proc_argv(root)
        self.launch_cwd = proc_cwd(root) or servers[0].cwd
        parent = table.get(root, (0, ""))[0]
        parent_cmd = table.get(parent, (0, ""))[1]
        self.shell = parent if SHELL.match(parent_cmd) else None
        if parent <= 1:
            self.host = "orphan"
        elif self.shell:
            self.host = "shell"
        elif AGENT.search(parent_cmd):
            self.host = "agent"
        else:
            self.host = "other"
        self.ancestors = ancestors(root, table)
        self.footprint = sum(s.footprint for s in servers)
        self.biggest = max(s.footprint for s in servers)
        # szczyt pojedynczego procesu; suma drzewa bywa od niego większa
        self.peak = max(max(s.peak for s in servers), self.footprint)
        self.regrow = sum(getattr(s, "regrow", 0) for s in servers)
        self.background = False
        self.cpu = sum(s.cpu for s in servers)
        self.ports = sorted({p for s in servers for p in s.ports})
        self.clients = []  # (pid, rodzaj, nazwa)
        # aplikacje z symulatorów, których nikt nie używa: połączone, ale to nie widzowie
        self.idle_sim_clients = []
        self.tabs = []
        self.terminal = None
        self.worktree = None
        self.consumers = []  # worktree, z których agenci oglądają serwer
        self.protected = False
        self.pin = None  # przypięcie z `pin`, które obejmuje tę jednostkę
        # z historii
        self.age = 0
        self.started = 0  # epoka startu komendy
        self.quiet = 0
        self.last_watched = 0

    @property
    def app_key(self):
        return "|".join(sorted({s.cwd for s in self.servers}))

    @property
    def label(self):
        port = f":{self.ports[0]}" if self.ports else f"pid {self.servers[0].pid}"
        if len(self.servers) == 1:
            return f"{port} {short(self.servers[0].cwd)}"
        ports = ",".join(f":{p}" for p in self.ports[:6])
        return f"stos {len(self.servers)} serwerów {ports or port} {short(self.launch_cwd)}"

    @property
    def watched(self):
        return bool(self.clients or self.tabs)

    @property
    def attended(self):
        """Ty na niego patrzysz: przeglądarka spoza Orki albo karta w wybranym worktree."""
        return any(kind == "browser" for _, kind, _ in self.clients) or any(
            t.get("focused") for t in self.tabs
        )

    @property
    def agent_working(self):
        worktrees = [self.worktree] + self.consumers
        return any(
            w
            and (
                w.get("status") == "working"
                or any(a.get("state") == "working" for a in w.get("agents", []))
            )
            for w in worktrees
        )

    @property
    def recyclable(self):
        return bool(
            self.host == "shell"
            and self.terminal
            and not self.terminal.get("agentIdentity")
            and self.argv
        )

    def summary(self):
        return {
            "key": self.key,
            "label": self.label,
            "root": self.root,
            "pids": self.pids,
            "ports": self.ports,
            "kinds": sorted({s.kind for s in self.servers}),
            "cwd": [s.cwd for s in self.servers],
            "footprint": self.footprint,
            "biggest": self.biggest,
            "peak": self.peak,
            "regrow": self.regrow,
            "host": self.host,
            "command": shlex.join(self.argv) if self.argv else None,
            "terminal": (self.terminal or {}).get("title"),
            "worktree": (self.worktree or {}).get("path"),
            "clients": [{"pid": p, "kind": k, "name": n} for p, k, n in self.clients],
            "idle_sim_clients": len(self.idle_sim_clients),
            "tabs": [
                {"url": t.get("url"), "focused": t.get("focused")} for t in self.tabs
            ],
            "attended": self.attended,
            "agent_working": self.agent_working,
            "recyclable": self.recyclable,
            "age": round(self.age),
            "quiet": round(self.quiet),
            "protected": self.protected,
            "pin": self.pin,
            "background": self.background,
            "servers": len(self.servers),
            "launch_cwd": self.launch_cwd,
        }


def ancestors(pid, table):
    out = []
    while pid > 1 and pid in table and len(out) < 64:
        out.append(pid)
        pid = table[pid][0]
    return out


def descendants(pid, children):
    out, stack = [pid], [pid]
    while stack:
        for child in children.get(stack.pop(), []):
            out.append(child)
            stack.append(child)
    return out


def runtime_of(command, runtimes):
    exe = command.split(None, 1)[0] if command else ""
    return os.path.basename(exe) in runtimes


def discover(cfg, rows):
    """Dev serwery i jednostki (komendy, które je postawiły) z tabeli procesów."""
    table = {pid: (ppid, command) for pid, ppid, command in rows}
    children = {}
    for pid, (ppid, _command) in table.items():
        children.setdefault(ppid, []).append(pid)
    found = {}
    for pid, (ppid, command) in table.items():
        if not runtime_of(command, cfg["runtimes"]):
            continue
        kind = next((k for k, rx in SERVER_KINDS if rx.search(command)), None)
        if kind:
            found[pid] = kind
    scope = [janitor.expand(p) for p in cfg["scope"]]
    servers = []
    for pid, kind in found.items():
        # serwer pod innym serwerem tego samego rodzaju to jego dziecko, nie osobny serwer
        if any(a in found for a in ancestors(table[pid][0], table)):
            continue
        server = Server(pid, kind, table[pid][1], descendants(pid, children))
        if scope and not any(
            server.cwd == p or server.cwd.startswith(p + "/") for p in scope
        ):
            continue
        servers.append(server)
    groups = {}
    for server in servers:
        root = server.pid
        while True:
            parent = table.get(root, (0, ""))[0]
            if parent <= 1 or not LAUNCHER.search(table.get(parent, (0, ""))[1]):
                break
            root = parent
        groups.setdefault(root, []).append(server)
    return [Unit(root, group, table, children) for root, group in groups.items()], table


# ---------- symulatory iOS ----------

# ścieżka device.plist -> nazwa urządzenia; nie zmienia się, dopóki urządzenie istnieje
_sim_names = {}


def sim_name(udid):
    path = os.path.join(SIM_DEVICES, udid, "device.plist")
    if path not in _sim_names:
        try:
            with open(path, "rb") as f:
                _sim_names[path] = str(plistlib.load(f).get("name") or udid)
        except (OSError, ValueError, plistlib.InvalidFileException):
            return udid
    return _sim_names[path]


def proc_start_epoch(pid):
    """Start procesu w sekundach epoki (pbi_start_tvsec z proc_bsdinfo); None, gdy go nie ma
    albo to zombie."""
    buf = ctypes.create_string_buffer(BSDINFO_SIZE)
    got = _libc.proc_pidinfo(
        pid, PROC_PIDTBSDINFO, ctypes.c_uint64(0), buf, BSDINFO_SIZE
    )
    if got != BSDINFO_SIZE or int.from_bytes(buf.raw[4:8], "little") == SZOMB:
        return None
    return int.from_bytes(buf.raw[120:128], "little")


def read_lease(cfg, udid):
    path = os.path.join(janitor.expand(cfg["simulator_leases"]), udid + ".json")
    lease = janitor.load_json(path, None)
    return lease if isinstance(lease, dict) else None


def lease_alive(lease):
    """Czy sesja z dzierżawy portivo-mobile żyje: ten sam pid i ten sam start, który
    portivo-mobile zapisał z `ps -o lstart=`. Start w nieznanym formacie (inny język
    systemu) niczego nie przesądza: wtedy wystarczy żywy pid."""
    owner = lease.get("owner") if isinstance(lease, dict) else None
    if not isinstance(owner, dict):
        return False
    try:
        pid = int(owner.get("pid"))
    except (TypeError, ValueError):
        return False
    started = proc_start_epoch(pid) if pid > 0 else None
    if started is None:
        return False
    text = " ".join(str(owner.get("start") or "").split())
    if not text:
        return True
    try:
        want = time.mktime(time.strptime(text, "%a %b %d %H:%M:%S %Y"))
    except (ValueError, OverflowError):
        return True
    return abs(want - started) <= 2


class Simulator:
    """Włączony symulator iOS: jego launchd_sim i wszystko pod nim (aplikacje, demony).
    W użyciu jest, gdy trzyma go żywa sesja (dzierżawa portivo-mobile), gdy ogląda go
    proces spoza niego (maestro, build z `-destination id=UDID`, serve-sim) albo gdy nie
    jest z puli agentów, czyli jest Twój."""

    def __init__(self, cfg, udid, root, tree, watchers):
        self.udid = udid
        self.root = root
        self.pids = sorted(tree)
        self.name = sim_name(udid)
        self.key = self.app_key = f"sim:{udid}"
        self.label = f"symulator {self.name}"
        self.pool = self.name.startswith(cfg["simulator_pool_prefix"])
        self.lease = read_lease(cfg, udid)
        self.lease_alive = lease_alive(self.lease)
        self.watchers = sorted(watchers)
        self.protected = any(
            udid == p or fnmatch.fnmatchcase(self.name, p) for p in cfg["simulator_protect"]
        )
        stats = [s for s in (usage(p) for p in tree) if s]
        self.footprint = sum(s["footprint"] for s in stats)
        self.cpu = sum(s["cpu"] for s in stats)
        self.start = (usage(root) or {}).get("start", 0)
        # pola, których oczekują wspólne ścieżki planów i zdarzeń
        self.ports = []
        self.argv = None
        self.launch_cwd = ""
        # z historii
        self.age = 0
        self.quiet = 0

    @property
    def watched(self):
        return bool(self.watchers)

    @property
    def in_use(self):
        return self.lease_alive or self.watched or not self.pool or self.protected

    def summary(self):
        owner = (self.lease or {}).get("owner") or {}
        return {
            "key": self.key,
            "udid": self.udid,
            "name": self.name,
            "pool": self.pool,
            "footprint": self.footprint,
            "processes": len(self.pids),
            "lease_app": (self.lease or {}).get("app"),
            "lease_session": owner.get("session") or None,
            "lease_alive": self.lease_alive,
            "watchers": self.watchers,
            "in_use": self.in_use,
            "protected": self.protected,
            "age": round(self.age),
            "quiet": round(self.quiet),
        }


class SimulatorGroup:
    """Wszystkie włączone symulatory naraz: adresat ostrzeżenia o limicie."""

    key = app_key = "simulators"

    def __init__(self, sims):
        self.footprint = sum(s.footprint for s in sims)
        self.label = f"{len(sims)} włączone symulatory"
        self.ports = []


def discover_simulators(cfg, rows):
    """Włączone symulatory z tabeli procesów: każdy ma własny launchd_sim (dziecko launchd)
    z UDID urządzenia w argumentach. Widzowie to procesy spoza symulatora z jego UDID."""
    children = {}
    for pid, ppid, _command in rows:
        children.setdefault(ppid, []).append(pid)
    roots = {}
    for pid, ppid, command in rows:
        if ppid == 1 and LAUNCHD_SIM.search(command):
            found = SIM_DEVICE.search(command)
            if found:
                roots[pid] = found.group(1)
    if not roots:
        return []
    trees = {pid: set(descendants(pid, children)) for pid in roots}
    inside = set().union(*trees.values())
    outside = [(pid, command) for pid, _ppid, command in rows if pid not in inside]
    viewers = [
        pid
        for pid, command in outside
        if not UDID.search(command)
        and (SERVE_SIM.search(command) or SIMCTL_BOOTED.search(command))
    ]
    sims = []
    for root, udid in roots.items():
        watchers = {pid for pid, command in outside if udid in command} | set(viewers)
        sims.append(Simulator(cfg, udid, root, trees[root], watchers))
    return sims


def track_simulators(cfg, state, sims, now):
    """Wiek i cisza symulatorów z historii stanu. Cisza liczy się od ostatniego użycia: CPU
    ponad `simulator_busy_cores`, żywa dzierżawa albo widz."""
    history = state.setdefault("sims", {})
    busy_cores = cfg["simulator_busy_cores"]
    seen = set()
    for sim in sims:
        key = f"{sim.udid}:{sim.start}"
        seen.add(key)
        h = history.get(key)
        if h is None:
            h = history[key] = {"first": now, "cpu": sim.cpu, "at": now, "busy": now}
        dt = max(now - h["at"], 0.001)
        if (sim.cpu - h["cpu"]) / dt >= busy_cores or sim.lease_alive or sim.watched:
            h["busy"] = now
        h["cpu"], h["at"] = sim.cpu, now
        sim.age = now - h["first"]
        sim.quiet = now - h["busy"]
    for key in list(history):
        if key not in seen:
            del history[key]


def drop_unused_simulator_clients(unit, commands, sims):
    """Aplikacja w symulatorze, którego nikt nie używa, trzyma połączenie z Metro i tylko udaje
    widza: bez tego Metro sesji, która umarła, nigdy nie wypada jako sierota ani bezczynny.
    Symulator spoza pomiaru zostaje widzem (lepiej nie ruszyć, niż ruszyć cudzy)."""
    kept, idle = [], []
    for client in unit.clients:
        found = SIM_DEVICE.search(commands.get(client[0], "")) if client[1] == "simulator" else None
        sim = sims.get(found.group(1)) if found else None
        (idle if sim is not None and not sim.in_use else kept).append(client)
    unit.clients = kept
    unit.idle_sim_clients = idle


def frontmost_bundle():
    """Bundle aplikacji na pierwszym planie (lsappinfo); None, gdy nie wiadomo."""
    try:
        asn = subprocess.run(
            ["lsappinfo", "front"], capture_output=True, text=True, timeout=5
        ).stdout.strip()
        if not asn:
            return None
        out = subprocess.run(
            ["lsappinfo", "info", "-only", "bundleid", asn],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    found = re.search(r'"CFBundleIdentifier"="([^"]*)"', out)
    return found.group(1) if found else None


class World:
    """Wszystko, co strażnik wie w jednej chwili."""

    def __init__(self, cfg, state, orca, now, use_orca=True):
        self.now = now
        rows = processes()
        self.units, self.table = discover(cfg, rows)
        self.simulators = discover_simulators(cfg, rows)
        track_simulators(cfg, state, self.simulators, now)
        by_udid = {s.udid: s for s in self.simulators}
        self.pressure = Pressure(cfg, state, now)
        commands = {pid: command for pid, (_ppid, command) in self.table.items()}
        if self.units:
            listen, links = sockets()
            owner = {}
            for unit in self.units:
                for server in unit.servers:
                    server.ports = sorted(
                        p for p, pids in listen.items() if pids & set(server.tree)
                    )
                # porty całego drzewa komendy: przy `pnpm dev` z korzenia także backend
                # (Go na :8003), z którego korzystają agenci, choć to nie dev serwer Node
                tree = set(unit.pids)
                unit.ports = sorted(p for p, pids in listen.items() if pids & tree)
                for port in unit.ports:
                    owner[port] = unit
            for pid, port in links:
                unit = owner.get(port)
                if unit and pid not in unit.pids:
                    command = commands.get(pid, "?")
                    entry = (
                        pid,
                        client_kind(command),
                        os.path.basename(command.split(" -")[0])[:40],
                    )
                    if entry not in unit.clients:
                        unit.clients.append(entry)
            for unit in self.units:
                drop_unused_simulator_clients(unit, commands, by_udid)
        if use_orca and self.units:
            orca.refresh(rows, now, cfg["orca_seconds"])
        self.orca = orca if orca.ok else None
        self.fsevents_restart = (janitor.load_json(FSGUARD_STATE, {}) or {}).get("last_restart", 0)
        protect = [
            janitor.expand(p)
            for p in cfg["protect"]
            if isinstance(p, str) and not p.startswith(":")
        ]
        protected_ports = {
            int(str(p).lstrip(":"))
            for p in cfg["protect"]
            if str(p).lstrip(":").isdigit()
        }
        pins = load_pins(now)
        history = state.setdefault("units", {})
        seen = set()
        for unit in self.units:
            if self.orca:
                unit.worktree = self.orca.worktree_for(unit.launch_cwd)
                unit.terminal = self.orca.terminal_for(unit.ancestors)
                for tab in self.orca.tabs:
                    if tab.get("port") in unit.ports:
                        unit.tabs.append(dict(tab, focused=self.orca.focused(tab)))
                consumer_ids = {t.get("worktreeId") for t in unit.tabs}
                unit.consumers = [
                    w
                    for w in self.orca.worktrees
                    if w.get("worktreeId") in consumer_ids
                ]
            unit.protected = bool(set(unit.ports) & protected_ports) or any(
                s.cwd == p or s.cwd.startswith(p + "/")
                for s in unit.servers
                for p in protect
            )
            unit.pin = next((p for p in pins if pin_matches(p, unit)), None)
            if unit.pin:
                unit.protected = True
            seen.add(unit.key)
            h = history.get(unit.key)
            if h is None:
                h = history[unit.key] = {
                    "first": now,
                    "cpu": unit.cpu,
                    "at": now,
                    "busy": now,
                    "watched": now,
                }
            dt = max(now - h["at"], 0.001)
            if (
                unit.cpu - h["cpu"]
            ) / dt >= 0.05:  # 5% rdzenia: kompiluje albo obsługuje żądania
                h["busy"] = now
            if unit.watched:
                h["watched"] = now
            h["cpu"], h["at"] = unit.cpu, now
            output = (unit.terminal or {}).get("lastOutputAt")
            busy = max(h["busy"], output / 1000 if output else 0)
            unit.age = now - h["first"]
            unit.started = started_at(unit.start, h["first"], now)
            # wyjście terminala z Orki bywa o kilka sekund nowsze niż `now` z początku pomiaru
            unit.quiet = max(0.0, now - busy)
            unit.last_watched = h["watched"]
        for key in list(history):
            if key not in seen:
                del history[key]


# ---------- decyzje ----------


class Plan:
    """Zamiar wobec jednostki. `reason` to zdanie do logu i terminala, `code` i `data`
    to to samo dla panelu w pasku menu, który składa własny tekst po angielsku."""

    def __init__(self, unit, action, priority, reason, code="manual", **data):
        self.unit = unit
        self.action = action  # "stop", "recycle", "warn"; dla symulatora "shutdown"
        self.priority = priority
        self.reason = reason
        self.code = code
        self.data = data

    def summary(self):
        return {
            "unit": self.unit.key,
            "label": self.unit.label,
            "action": self.action,
            "reason": self.reason,
            "code": self.code,
            "data": self.data,
        }


def minutes(seconds):
    return f"{int(seconds // 60)} min"


def recent_recycles(state, app_key, now):
    return sum(
        1 for e in state.get("recycles", []) if e[1] == app_key and now - e[0] < HOUR
    )


def decide(cfg, world, state):
    """Lista planów od najważniejszego; wykonuje się co najwyżej pierwszy."""
    now, pressure, units = world.now, world.pressure, world.units
    grace = cfg["grace_minutes"] * MINUTE
    max_fp = cfg["max_server_gb"] * GB
    mature = [u for u in units if not u.protected and u.age >= grace]
    plans = []

    by_app = {}
    for unit in units:
        for server in unit.servers:
            by_app.setdefault(server.cwd, []).append(unit)
    for app, group in by_app.items():
        group = list({u.key: u for u in group}.values())
        if len(group) < 2:
            continue
        keep = max(
            group, key=lambda u: (u.attended, u.watched, u.last_watched, u.start)
        )
        for unit in group:
            if unit is keep or unit not in mature or unit.watched:
                continue
            if now - unit.last_watched >= cfg["duplicate_minutes"] * MINUTE:
                plans.append(
                    Plan(
                        unit,
                        "stop",
                        60,
                        f"drugi serwer {short(app)}, zostaje {keep.label}",
                        "duplicate",
                        keep=keep.ports[0] if keep.ports else None,
                    )
                )

    for unit in mature:
        if (
            unit.host == "orphan"
            and not unit.watched
            and now - unit.last_watched >= cfg["orphan_minutes"] * MINUTE
        ):
            plans.append(
                Plan(
                    unit,
                    "stop",
                    50,
                    "agent albo terminal, który go postawił, już nie żyje",
                    "orphan",
                )
            )
        idle = cfg["idle_minutes"] * MINUTE * (2 if unit.agent_working else 1)
        if not unit.watched and unit.quiet >= idle and now - unit.last_watched >= idle:
            plans.append(
                Plan(
                    unit,
                    "stop",
                    40,
                    f"nikt go nie ogląda i nic nie robi od {minutes(unit.quiet)}",
                    "idle",
                    minutes=int(unit.quiet // MINUTE),
                )
            )
        if unit.biggest >= max_fp:
            # serwer, na który patrzysz, restartujemy dopiero po dłuższej ciszy
            needed = cfg["quiet_seconds"] * (10 if unit.attended else 1)
            why = f"spuchł do {janitor.human(unit.biggest)} (limit {cfg['max_server_gb']} GB)"
            if unit.quiet < needed:
                continue
            size = dict(size=unit.biggest, limit=max_fp)
            if unit.recyclable:
                plans.append(Plan(unit, "recycle", 70, why, "bloated", **size))
            elif not unit.watched:
                plans.append(Plan(unit, "stop", 70, why, "bloated", **size))
            else:
                plans.append(
                    Plan(
                        unit,
                        "warn",
                        10,
                        why + "; nie mam jak go zrestartować",
                        "bloated_unmanaged",
                        **size,
                    )
                )

    # przypięte: nigdy stop; spuchnięte dostają restart (wstaje z cache w kilka sekund),
    # chyba że przypięcie mówi --no-restart
    for unit in units:
        pin = getattr(unit, "pin", None)
        if not pin or unit.age < grace or unit.biggest < max_fp:
            continue
        if pin.get("level") == "hold" or not unit.recyclable:
            continue
        if unit.quiet < cfg["quiet_seconds"] * (10 if unit.attended else 1):
            continue
        plans.append(
            Plan(
                unit,
                "recycle",
                70,
                f"spuchł do {janitor.human(unit.biggest)} (limit {cfg['max_server_gb']} GB); "
                "przypięty, więc restart zamiast zatrzymania",
                "bloated",
                size=unit.biggest,
                limit=max_fp,
            )
        )

    total = sum(u.footprint for u in units)
    budget = cfg["budget_percent"] / 100 * pressure.ram
    idle_pool = cfg["idle_minutes"] * MINUTE / 3
    if pressure.level or total > budget:
        pool = []
        for unit in mature:
            bloated = unit.biggest >= max_fp
            if pressure.level == 2:
                if unit.attended and not unit.recyclable:
                    continue
            elif unit.attended or unit.quiet < cfg["quiet_seconds"]:
                continue
            elif pressure.level == 1:
                # ostrzeżenie: serwery bez podglądu i spuchnięte
                if unit.watched and not bloated:
                    continue
            elif not bloated and (unit.watched or unit.quiet < idle_pool):
                # sam budżet, Mac się nie dusi: tylko spuchnięte albo naprawdę bezczynne.
                # Tani, żywy stos nie płaci za cudze 8 GB (tak padł `pnpm dev` z korzenia)
                continue
            pool.append(unit)

        def score(u):
            return (
                u.footprint
                * (1 + min(u.quiet / 1800, 2))
                * (0.5 if u.watched else 1)
                * (0.7 if u.agent_working else 1)
            )

        if pool:
            top = max(pool, key=score)
            if pressure.level:
                why = "brak pamięci: " + ", ".join(pressure.reasons)
            else:
                why = f"dev serwery zajmują {janitor.human(total)}, budżet {janitor.human(budget)}"
            action = (
                "recycle"
                if top.recyclable and (top.watched or top.attended)
                else "stop"
            )
            if pressure.level:
                code, data = "pressure", dict(level=pressure.level)
            else:
                code, data = "budget", dict(total=total, budget=budget)
            plans.append(
                Plan(top, action, 90 if pressure.level == 2 else 80, why, code, **data)
            )
        elif pressure.level == 2:
            # Mac się dusi, a zostały tylko przypięte: restart największego, nigdy stop
            pinned = [
                u
                for u in units
                if getattr(u, "pin", None) and u.recyclable and u.age >= grace
            ]
            if pinned:
                top = max(pinned, key=lambda u: u.footprint)
                plans.append(
                    Plan(
                        top,
                        "recycle",
                        90,
                        "brak pamięci: "
                        + ", ".join(pressure.reasons)
                        + "; przypięty, więc restart zamiast zatrzymania",
                        "pressure",
                        level=2,
                    )
                )

    # strażnik fseventsd zrestartował demona zdarzeń plików: obserwatory sprzed restartu są
    # głuche, więc serwer nie widzi edycji (HMR stoi), dopóki nie wstanie od nowa. Serwerów
    # chronionych i bez terminala Orki nie ruszamy; przypięty dostaje restart jak spuchnięty,
    # chyba że przypięcie mówi --no-restart
    restart = world.fsevents_restart
    for unit in units:
        pin = getattr(unit, "pin", None)
        allowed = pin.get("level") != "hold" if pin else not unit.protected
        if restart and unit.recyclable and allowed and unit.started < restart:
            plans.append(
                Plan(unit, "recycle", 95, "po restarcie fseventsd nie widzi zmian plików", "fsevents")
            )

    plans += simulator_plans(cfg, getattr(world, "simulators", []), pressure, grace)

    for plan in plans:
        if plan.action != "recycle":
            continue
        count = recent_recycles(state, plan.unit.app_key, now)
        if count >= cfg["max_recycles_per_hour"]:
            plan.data["restarts"] = count
            if plan.unit.attended or getattr(plan.unit, "pin", None):
                plan.action, plan.code = "warn", "loop_watched"
                why = "go oglądasz" if plan.unit.attended else "jest przypięty"
                plan.reason += f"; {count} restarty w godzinę, nie ruszam, bo {why}"
            else:
                plan.action, plan.code = "stop", "loop"
                plan.reason += f"; {count} restarty w godzinę to pętla, zatrzymuję"

    best = {}
    for plan in sorted(plans, key=lambda p: -p.priority):
        best.setdefault(plan.unit.key, plan)
    return sorted(best.values(), key=lambda p: -p.priority)


def simulator_plans(cfg, sims, pressure, grace):
    """Wyłączenia symulatorów. Kandydat jest z puli agentów, bez żywej dzierżawy, bez widza,
    nie chroniony i starszy niż `grace_minutes`. Taki idzie po `simulator_idle_minutes`
    ciszy; ponad limitem włączonych albo przy braku pamięci już po `simulator_quiet_minutes`
    (najdłużej cichy). Ponad limitem bez kandydata zostaje ostrzeżenie."""
    if not sims:
        return []
    cap = cfg["max_booted_simulators"]
    idle = cfg["simulator_idle_minutes"] * MINUTE
    safe = [s for s in sims if not s.in_use and not s.protected and s.age >= grace]
    plans = [
        Plan(
            s,
            "shutdown",
            45,
            f"nikt go nie używa od {minutes(s.quiet)}",
            "simulator_idle",
            minutes=int(s.quiet // MINUTE),
        )
        for s in safe
        if s.quiet >= idle
    ]
    over = bool(cap) and len(sims) > cap
    if not (over or pressure.level):
        return plans
    ready = [s for s in safe if s.quiet >= cfg["simulator_quiet_minutes"] * MINUTE]
    if ready:
        top = max(ready, key=lambda s: (s.quiet, s.footprint))
        unused = f"nieużywany od {minutes(top.quiet)}"
        if over:
            why = f"{len(sims)} włączone symulatory, limit {cap}; ten {unused}"
            code, data = "simulator_cap", dict(booted=len(sims), cap=cap)
        else:
            why = "brak pamięci: " + ", ".join(pressure.reasons) + f"; symulator {unused}"
            code, data = "pressure", dict(level=pressure.level)
        plans.append(Plan(top, "shutdown", 85, why, code, minutes=int(top.quiet // MINUTE), **data))
    elif over:
        group = SimulatorGroup(sims)
        plans.append(
            Plan(
                group,
                "warn",
                10,
                f"{len(sims)} włączone symulatory ({janitor.human(group.footprint)}), limit {cap}; "
                "każdy jest w użyciu albo dopiero wstał",
                "simulator_cap",
                booted=len(sims),
                cap=cap,
            )
        )
    return plans


# ---------- akcje ----------


def terminate(unit, table, grace=10, reap=10):
    """SIGTERM do całego drzewa naraz, po `grace` sekundach SIGKILL dla tych, które zostały, i do
    `reap` sekund na ich koniec. Pod presją proces po SIGKILL kończy się sekundami (jądro zwalnia
    jego strony, także te w swapie i w kompresorze), więc żywy tuż po sygnale nie znaczy, że
    przeżył: 2026-10-08 log mówił „nie chcą zginąć” o Metro, które za chwilę zniknęło.
    Przed SIGTERM idzie SIGCONT: proces wstrzymany (scheduler pauzuje joby przy rosnącym swapie,
    ktoś zrobił Ctrl+Z) nie obsłuży SIGTERM, dopóki stoi, i ginąłby dopiero od SIGKILL."""
    targets = []
    for pid in unit.pids:
        command = table.get(pid, (0, ""))[1]
        info = usage(pid)
        if info and not SACRED.search(command) and pid != os.getpid():
            targets.append((pid, info["start"]))
    for sig in (signal.SIGCONT, signal.SIGTERM):
        for pid, _start in targets:
            try:
                os.kill(pid, sig)
            except (ProcessLookupError, PermissionError):
                pass
    deadline = time.time() + grace
    while time.time() < deadline:
        left = [(p, s) for p, s in targets if alive(p, s)]
        if not left:
            return []
        time.sleep(0.25)
    left = [(p, s) for p, s in targets if alive(p, s)]
    for pid, _start in left:
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    deadline = time.time() + reap
    while left and time.time() < deadline:
        time.sleep(0.25)
        left = [(p, s) for p, s in left if alive(p, s)]
    return [p for p, _s in left]


def notify_quiet(title, text):
    """Powiadomienie bez czekania na osascript (przy duszącym się Macu startuje sekundami) i bez
    zabierania fokusu: `display notification` nie aktywuje żadnej aplikacji. Tekst i tytuł idą jako
    argumenty skryptu: json.dumps zamieniłby "ą" na \\u0105, którego AppleScript nie rozumie."""
    try:
        subprocess.Popen(
            [
                "osascript",
                "-e",
                "on run argv",
                "-e",
                "display notification (item 1 of argv) with title (item 2 of argv)",
                "-e",
                "end run",
                "--",
                text,
                title,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        pass


def shell_idle(shell):
    """Czy powłoka wróciła do promptu, czyli nie ma już żadnych dzieci."""
    for _ in range(40):
        rows = processes()
        if not any(ppid == shell for _pid, ppid, _command in rows):
            return True
        time.sleep(0.25)
    return False


def port_free(ports, wait=8):
    deadline = time.time() + wait
    while time.time() < deadline:
        listen, _ = sockets()
        if not set(ports) & set(listen):
            return True
        time.sleep(0.5)
    return False


def stop(unit, world):
    left = terminate(unit, world.table)
    return not left, "zatrzymany" if not left else f"nie chcą zginąć: {left}"


def recycle(unit, world, state):
    orca = world.orca
    if not (unit.recyclable and orca):
        return False, f"nie mam terminala {orcahost.host().name}, w którym mógłbym go postawić"
    shell_cwd = proc_cwd(unit.shell)
    left = terminate(unit, world.table)
    unit.killed = True
    if left:
        return False, f"nie chcą zginąć: {left}"
    if not shell_idle(unit.shell):
        return False, "powłoka nie wróciła do promptu, nie wpisuję komendy"
    port_free(unit.ports)
    text = shlex.join(unit.argv)
    if shell_cwd != unit.launch_cwd:
        text = f"cd {shlex.quote(unit.launch_cwd)} && {text}"
    sent = orca.call(
        "terminal",
        "send",
        "--terminal",
        unit.terminal["handle"],
        "--text",
        text,
        "--enter",
    )
    if not (sent and sent.get("send", {}).get("accepted")):
        return False, f"{orcahost.host().name} nie przyjmuje komendy; wpisz ręcznie: {text}"
    state.setdefault("recycles", []).append([world.now, unit.app_key])
    state.setdefault("pending", []).append(
        {"at": world.now, "app": unit.app_key, "label": unit.label, "command": text}
    )
    return (
        True,
        f"restart w terminalu „{unit.terminal.get('title') or unit.terminal['handle'][:13]}”",
    )


def after_stop(cfg, unit, world, reason):
    """Porządek w Orce po zatrzymaniu: karty w tle bez serwera tylko by się przeładowywały."""
    orca = world.orca
    if not orca:
        return
    if cfg["close_tabs"]:
        for tab in unit.tabs:
            if not tab.get("focused") and tab.get("browserPageId"):
                orca.call("tab", "close", "--page", tab["browserPageId"])
    note(cfg, orca, unit, f"devguard: zatrzymałem {unit.label} ({reason})")


def note(cfg, orca, unit, text):
    """Komentarz na karcie worktree w Orce, ale tylko gdy nie nadpisuje cudzego."""
    worktree = unit.worktree
    if not (cfg["orca_comment"] and orca and worktree):
        return
    current = worktree.get("comment") or ""
    # wtyczka Orki claude-acc pisze swoją linię "claude-acc: ..." i po notce dopisze ją z powrotem
    if current and not current.startswith(("devguard", "claude-acc:")):
        return
    orca.call(
        "worktree",
        "set",
        "--worktree",
        f"id:{worktree['worktreeId']}",
        "--comment",
        text[:200],
    )


def held_back(state, key, now, text):
    """Akcja wstrzymana (Simulator z przodu, sesja wzięła symulator, działa w nim aplikacja):
    wpis w logu najwyżej co 10 minut na jednostkę, bo pętla pyta co kilka sekund."""
    seen = state.setdefault("held", {})
    if now - seen.get(key, 0) >= 10 * MINUTE:
        seen[key] = now
        log(text)


def portivo_lock(cfg):
    """Zamek dzierżaw portivo-mobile (`up` trzyma go, wybierając i dzierżawiąc symulator).
    Uchwyt, gdy wolny; False, gdy ktoś go trzyma; None, gdy portivo-mobile tu nie ma."""
    base = os.path.dirname(janitor.expand(cfg["simulator_leases"]).rstrip("/"))
    if not os.path.isdir(base):
        return None
    handle = open(os.path.join(base, "lease.lock"), "a")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return False
    return handle


def running_apps(udid):
    """Bundle id aplikacji użytkownika działających w symulatorze (`launchctl list` w nim, jak
    portivo-mobile); systemowe com.apple.* działają w każdym. None, gdy nie da się sprawdzić."""
    try:
        out = subprocess.run(
            ["xcrun", "simctl", "spawn", udid, "launchctl", "list"],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return {
        m.group(1)
        for m in re.finditer(r"UIKitApplication:([\w.\-]+)\[", out.stdout)
        if not m.group(1).startswith("com.apple.")
    }


def shutdown_simulator(cfg, plan, world, state):
    """`simctl shutdown`, ale nigdy, gdy okno Simulatora jest na pierwszym planie, ani gdy
    sesja właśnie wzięła ten symulator (dzierżawa czytana od nowa pod zamkiem portivo-mobile)."""
    sim, now = plan.unit, world.now
    size = janitor.human(sim.footprint)
    front = frontmost_bundle()
    if front in (SIMULATOR_APP, None):
        why = "patrzysz na Simulator" if front else "nie wiem, co jest na pierwszym planie"
        held_back(state, sim.key, now, f"czekam z wyłączeniem {sim.label}: {why}")
        return False
    lock = portivo_lock(cfg)
    if lock is False:
        return False  # `portivo-mobile up` właśnie dzierżawi; następny pomiar
    try:
        lease = read_lease(cfg, sim.udid)
        if lease_alive(lease):
            held_back(state, sim.key, now, f"nie wyłączam {sim.label}: sesja właśnie go wzięła")
            return False
        # jak portivo-mobile: symulator po martwej sesji z jej aplikacją (i sterownikiem maestro)
        # jest wolny, a bez dzierżawy żadna aplikacja użytkownika nie może w nim działać, bo ktoś
        # go używa spoza dzierżaw (skrypty perf przez idb, Ty)
        allowed = {lease.get("bundle"), MAESTRO_DRIVER} if lease else set()
        apps = running_apps(sim.udid)
        if apps is None or apps - allowed:
            what = ", ".join(sorted(apps - allowed)) if apps else "nie wiem, co w nim działa"
            held_back(state, sim.key, now, f"nie wyłączam {sim.label}: działa w nim {what}")
            return False
        try:
            done = subprocess.run(
                ["xcrun", "simctl", "shutdown", sim.udid],
                capture_output=True,
                text=True,
                timeout=120,
            )
            ok = done.returncode == 0
            result = "wyłączony" if ok else f"simctl: {(done.stderr or done.stdout).strip()[:200]}"
        except (OSError, subprocess.SubprocessError) as err:
            ok, result = False, f"simctl: {err}"
    finally:
        if lock:
            lock.close()
    log(f"shutdown {sim.label} {size}: {plan.reason} -> {result}")
    events = state.setdefault("events", [])
    events.append(
        {
            "at": now,
            "action": "shutdown",
            "label": sim.name,
            "size": sim.footprint,
            "reason": plan.reason,
            "code": plan.code,
            "data": plan.data,
            "ports": [],
            "cwd": "",
            "result": result,
            "ok": ok,
        }
    )
    del events[:-20]
    if cfg["notify"]:
        janitor.notify("Strażnik: wyłączony symulator", f"{sim.name} ({size}): {plan.reason}")
    return True


def execute(cfg, plan, world, state):
    """Wykonuje plan; False, gdy akcja została wstrzymana i nic się nie stało."""
    unit, now = plan.unit, world.now
    size = janitor.human(unit.footprint)
    if plan.action == "warn":
        warned = state.setdefault("warned", {})
        if now - warned.get(unit.app_key, 0) < HOUR:
            return False
        warned[unit.app_key] = now
        log(f"uwaga {unit.label} {size}: {plan.reason}")
        if cfg["notify"]:
            title = "Za dużo symulatorów" if plan.code == "simulator_cap" else "Dev serwer puchnie"
            janitor.notify(title, f"{unit.label}: {plan.reason}")
        return True
    if plan.action == "shutdown":
        return shutdown_simulator(cfg, plan, world, state)
    if getattr(unit, "idle_sim_clients", None) and frontmost_bundle() == SIMULATOR_APP:
        # Metro z aplikacją w symulatorze: patrzysz na Simulator, więc może na nią
        held_back(state, unit.key, now, f"czekam z {plan.action} {unit.label}: patrzysz na Simulator")
        return False
    if plan.action == "recycle":
        ok, result = recycle(unit, world, state)
        if not ok and getattr(unit, "killed", False):
            # serwer już nie żyje, a restart się nie udał: wyszedł z tego stop
            after_stop(cfg, unit, world, plan.reason)
        elif ok:
            note(
                cfg, world.orca, unit, f"devguard: restart {unit.label} ({plan.reason})"
            )
    else:
        ok, result = stop(unit, world)
        if ok:
            after_stop(cfg, unit, world, plan.reason)
    verb = {"recycle": "restart", "stop": "stop"}[plan.action]
    line = f"{verb} {unit.label} {size}: {plan.reason} -> {result}"
    if unit.argv and plan.action == "stop":
        line += f" | wznowienie: cd {shlex.quote(unit.launch_cwd)} && {shlex.join(unit.argv)}"
    log(line)
    events = state.setdefault("events", [])
    events.append(
        {
            "at": now,
            "action": verb,
            "label": unit.label,
            "size": unit.footprint,
            "reason": plan.reason,
            "code": plan.code,
            "data": plan.data,
            "ports": unit.ports,
            "cwd": unit.launch_cwd,
            "apps": sorted({s.cwd for s in unit.servers}),
            "result": result,
            "ok": ok,
        }
    )
    del events[:-20]
    if cfg["notify"]:
        title = (
            "Strażnik: restart dev serwera"
            if verb == "restart"
            else "Strażnik: zatrzymany dev serwer"
        )
        janitor.notify(title, f"{unit.label} ({size}): {plan.reason}")
    return True


def check_pending(world, state):
    """Czy serwer po restarcie wstał; po minucie bez niego zostaje ślad w logu i powiadomienie."""
    pending = []
    apps = {u.app_key for u in world.units}
    for entry in state.get("pending", []):
        if entry["app"] in apps:
            log(
                f"wstał po restarcie {entry['label']} w {round(world.now - entry['at'])} s"
            )
        elif world.now - entry["at"] > 60:
            log(f"nie wstał po restarcie {entry['label']}; komenda: {entry['command']}")
            janitor.notify(
                "Dev serwer nie wstał", f"{entry['label']}: {entry['command']}"
            )
        else:
            pending.append(entry)
    state["pending"] = pending


def check_caps(cfg, state, now, dry_run):
    """Limity katalogów z wynikami agentów: zadanie janitora, ale co kilka minut, a nie co 3 h."""
    every = cfg["caps_minutes"] * MINUTE
    if not every or now - state.get("caps_at", 0) < every:
        return
    state["caps_at"] = now
    jcfg = janitor.load_config()
    if not jcfg.get("caps"):
        return
    sweep = janitor.Sweep(jcfg, dry_run)
    janitor.task_caps(sweep, None)
    if sweep.freed:
        verb = "do zwolnienia" if dry_run else "zwolniono"
        log(
            f"caps: {verb} {janitor.human(sweep.freed)}: "
            + ", ".join(short(p) for _, p, _ in sweep.items)
        )


def shape(cfg, world):
    """QoS tła dla serwerów, na które nie patrzysz; zwykły priorytet, gdy na nie patrzysz."""
    for unit in world.units:
        unit.background = bool(
            cfg["background_unattended"] and not unit.attended and not unit.protected
        )
        for pid in unit.pids:
            command = world.table.get(pid, (0, ""))[1]
            info = usage(pid)
            if info and not SACRED.search(command):
                set_background(pid, info["start"], unit.background)
    for pid, (start, _on) in list(_background.items()):
        if not alive(pid, start):
            del _background[pid]


# ---------- długo żyjące procesy ----------

# rodziny procesów, które siedzą w pamięci długo i zmniejszają to, co scheduler może wpuścić;
# kolejność rozstrzyga, gdzie trafia proces pasujący do kilku
FAMILIES = (
    ("headless", HEADLESS),
    ("simulators", re.compile(r"launchd_sim|CoreSimulator|Simulator\.app|SimulatorTrampoline|/Developer/CoreSimulator/")),
    ("metro", re.compile(r"/expo(/bin/cli)?\s+start\b|/metro\b|react-native\s+start\b")),
    ("watchers", re.compile(r"--watch(All)?\b|(^|/)nodemon(\s|$)|(^|\s)-w(\s|$)|/vitest(\.mjs)?\s+(watch|dev)\b")),
    ("lsp", re.compile(r"(^|/)gopls(\s|$)|tsserver\.js|typescript-language-server|rust-analyzer|sourcekit-lsp|clangd|pyright-langserver")),
    ("docker", re.compile(r"com\.docker|Docker\.app|com\.apple\.Virtualization")),
    ("git", re.compile(r"^(\S*/)?git(\s|$)")),
    ("agents", AGENT),
    ("browsers", BROWSER),
)
FAMILY_NAMES = {
    "dev": "dev serwery", "jobs": "joby schedulera", "headless": "headless przeglądarki",
    "simulators": "symulatory", "metro": "expo/metro", "watchers": "watchery", "lsp": "LSP",
    "docker": "Docker", "git": "git", "agents": "agenci", "browsers": "przeglądarki", "rest": "reszta",
}  # fmt: skip
# to liczy się jako długo żyjące: nie skończy się samo, a scheduler widzi je tylko jako mniej pamięci
LONG_LIVED = ("dev", "metro", "watchers", "simulators", "headless", "lsp", "docker")


def inventory(table, units, now):
    """Pamięć procesów tego użytkownika po rodzinach: {at, families: {rodzina: {count, footprint,
    top: [pid, rozmiar, komenda]}}, long_lived, biggest: [pid, rozmiar, komenda]}."""
    dev = {p for u in units for p in u.pids}
    jobs = set()
    for job in janitor.load_json(os.path.join(STATE_DIR, "sched", "state.json"), {}).get("running", []) or []:
        if job.get("child_pgid"):
            jobs.update(descendants(job["child_pgid"], _children_of(table)))
    families, biggest = {}, [0, 0, ""]
    for pid, (_ppid, command) in table.items():
        info = usage(pid)
        if not info or not info["footprint"]:
            continue
        size = info["footprint"]
        if pid in dev:
            name = "dev"
        elif pid in jobs:
            name = "jobs"
        else:
            name = next((n for n, rx in FAMILIES if rx.search(command)), "rest")
        f = families.setdefault(name, {"count": 0, "footprint": 0, "top": [0, 0, ""]})
        f["count"] += 1
        f["footprint"] += size
        if size > f["top"][1]:
            f["top"] = [pid, size, command[:120]]
        if size > biggest[1] and not SACRED.search(command):
            biggest = [pid, size, command[:120]]
    return {
        "at": now,
        "families": families,
        "long_lived": sum(f["footprint"] for n, f in families.items() if n in LONG_LIVED),
        "biggest": biggest,
    }


def _children_of(table):
    out = {}
    for pid, (ppid, _command) in table.items():
        out.setdefault(ppid, []).append(pid)
    return out


def inventory_line(inv):
    """Jedna linia do statusu: długo żyjące rodziny od największej."""
    fams = inv.get("families") or {}
    parts = [
        f"{FAMILY_NAMES[n]} {janitor.human(f['footprint'])}" + (f" ({f['count']})" if f["count"] > 1 else "")
        for n, f in sorted(fams.items(), key=lambda kv: -kv[1]["footprint"])
        if n in LONG_LIVED and f["footprint"] >= 50 * MB
    ]
    return f"Długo żyjące: {janitor.human(inv.get('long_lived', 0))}" + (": " + " · ".join(parts) if parts else "")


def brake(cfg, world, state, now, enforce, acted):
    """Hamulec pamięci (lastresort): jedno drzewo mniej, gdy stopień tego wymaga; opis albo None.

    Hamulec (2) działa, gdy strażnik w tym przebiegu nic nie zrobił i minęła jego przerwa
    między akcjami; awaria (3) nie czeka na nic poza własną krótką przerwą. Proces ponad pół
    RAM ginie na każdym stopniu."""
    import lastresort

    if not (enforce and cfg.get("last_resort", True)):
        return None
    p = world.pressure
    level = getattr(p, "stage", None)
    if level is None:  # starszy kształt Pressure (testy): krytyczna presja strażnika to hamulec
        level = 2 if p.level >= 2 else 0
    s = lastresort.settings(cfg)
    inv = state.get("inventory") or {}
    runaway = (inv.get("biggest") or [0, 0])[1] >= s["runaway_percent"] / 100 * getattr(p, "ram", 0) > 0
    if level < 2 and not runaway:
        return None
    gap = s["emergency_cooldown_seconds"] if level >= 3 else s["brake_cooldown_seconds"]
    if now - state.get("brake_at", 0) < gap:
        return None
    if level < 3 and not runaway and (acted is not None or now - state.get("last_action", 0) < cfg["cooldown_seconds"]):
        return None
    reaped = lastresort.reap(world, state, sys.modules[__name__], level=level, cfg=cfg)
    if reaped:
        state["brake_at"] = now
        state["last_action"] = now
        # przyrost swapu sprzed akcji nie może wywołać następnej: pomiar od nowa
        state["swap_history"] = []
    return reaped


def tick(cfg, state, orca, dry_run=False, now=None, qos=False):
    now = now or time.time()
    # przy awarii bez Orki: jej CLI to node, który przy duszącym się Macu startuje sekundami
    previous = ((state.get("snapshot") or {}).get("pressure") or {}).get("stage", 0)
    world = World(cfg, state, orca, now, use_orca=previous < 3)
    check_pending(world, state)
    plans = decide(cfg, world, state)
    enforce = cfg["mode"] == "enforce" and not dry_run
    acted = None
    if (
        plans
        and enforce
        and now - state.get("last_action", 0) >= cfg["cooldown_seconds"]
    ):
        # pierwszy plan, który coś zrobił: wstrzymany (patrzysz na Simulator, `up` dzierżawi)
        # nie może blokować tych pod nim, np. zatrzymania serwera przy presji
        for plan in plans:
            if execute(cfg, plan, world, state) is False:
                continue
            if plan.action != "warn":
                state["last_action"] = now
                # przyrost swapu sprzed akcji nie może wywołać następnej: pomiar od nowa
                state["swap_history"] = []
                acted = plan
            break
    stage = getattr(world.pressure, "stage", 0)
    inv = state.get("inventory") or {}
    if hasattr(world, "table") and world.table and (stage >= 1 or now - inv.get("at", 0) >= 30):
        state["inventory"] = inventory(world.table, world.units, now)
    reaped = brake(cfg, world, state, now, enforce, acted)
    history = state.setdefault("history", [])
    if not history or now - history[-1][0] >= 30:
        p = world.pressure
        history.append(
            [
                round(now),
                sum(u.footprint for u in world.units),
                p.swap_used,
                p.compressed,
            ]
        )
        del history[:-240]
    if enforce and qos:
        # tylko pętla w tle: jednorazowe `once` nie zostawia nikogo przyklejonego do rdzeni E
        shape(cfg, world)
        check_caps(cfg, state, now, dry_run=False)
    state["snapshot"] = {
        "at": now,
        "mode": cfg["mode"],
        "pressure": world.pressure.summary(),
        "budget": cfg["budget_percent"] / 100 * world.pressure.ram,
        "total": sum(u.footprint for u in world.units),
        "orca": world.orca is not None,
        "units": [u.summary() for u in sorted(world.units, key=lambda u: -u.footprint)],
        "simulators": [s.summary() for s in getattr(world, "simulators", [])],
        "simulator_cap": cfg["max_booted_simulators"],
        "plans": [p.summary() for p in plans],
        "acted": acted.summary() if acted else None,
        "last_resort": reaped,
        "inventory": state.get("inventory"),
    }
    return world, plans, acted


# ---------- komendy ----------


def take_lock():
    os.makedirs(STATE_DIR, exist_ok=True)
    handle = open(LOCK_PATH, "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def save_state(state):
    state["writer"] = os.getpid()
    state["saved_at"] = time.time()
    janitor.write_json(STATE_PATH, state)


def merge_foreign(state):
    """Ręczne `stop`/`recycle` zapisują stan obok pętli: jej kopia w pamięci nie może
    zgubić ich zdarzeń, a zwłaszcza licznika restartów, który wykrywa pętlę HMR."""
    disk = janitor.load_json(STATE_PATH, {})
    if disk.get("writer") in (None, os.getpid()) or disk.get(
        "saved_at", 0
    ) <= state.get("saved_at", 0):
        return
    for key in ("events", "recycles", "pending"):
        mine = state.get(key, [])
        seen = {json.dumps(item, sort_keys=True) for item in mine}
        extra = [
            i for i in disk.get(key, []) if json.dumps(i, sort_keys=True) not in seen
        ]
        if extra:
            at = (lambda i: i["at"]) if key != "recycles" else (lambda i: i[0])
            state[key] = sorted(mine + extra, key=at)[-20:]
    state["last_action"] = max(state.get("last_action", 0), disk.get("last_action", 0))


def cmd_run(_cfg, _args):
    lock = take_lock()
    if lock is None:
        print("strażnik już działa")
        return 0
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    state = janitor.load_json(STATE_PATH, {})
    orca = Orca()
    log("start strażnika")
    try:
        while True:
            cfg = load_config()
            try:
                merge_foreign(state)
                tick(cfg, state, orca, qos=True)
            except Exception as err:  # pętla nie może paść przez jeden zły pomiar
                log(f"błąd {err!r}")
            save_state(state)
            # przy ciasnej pamięci co 2 s: między pomiarami co 5 s swap potrafi urosnąć o gigabajt
            stage = ((state.get("snapshot") or {}).get("pressure") or {}).get("stage", 0)
            time.sleep(min(cfg["interval_seconds"], 2) if stage >= 1 else cfg["interval_seconds"])
    finally:
        restore_background()
        log("koniec strażnika")


def cmd_once(cfg, args):
    dry_run = "--dry-run" in args
    lock = take_lock()
    if lock is None:
        print("strażnik działa w tle; to jego stan:\n")
        return cmd_status(cfg, [])
    state = janitor.load_json(STATE_PATH, {})
    _world, _plans, acted = tick(cfg, state, Orca(), dry_run=dry_run)
    if not dry_run:
        save_state(state)
    print_status(state["snapshot"])
    if acted:
        print(f"\nwykonane: {acted.action} {acted.unit.label}")
    return 0


def cmd_caps(cfg, args):
    """Limity katalogów z wynikami agentów raz, teraz: to samo, co pętla robi co caps_minutes.
    acc-cored (natywny strażnik) woła to we własnym rytmie, bo sam katalogów nie kasuje."""
    check_caps(cfg, {}, time.time(), dry_run="--dry-run" in args)
    return 0


def cmd_status(cfg, args):
    state = janitor.load_json(STATE_PATH, {})
    snap = state.get("snapshot")
    fresh = snap and time.time() - snap["at"] < max(3 * cfg["interval_seconds"], 20)
    if not fresh:
        # strażnik nie chodzi: pomiar na żywo, bez akcji i bez zapisu historii
        _world, _plans, _acted = tick(cfg, state, Orca(), dry_run=True)
        snap = state["snapshot"]
    if "--json" in args:
        print(json.dumps(snap))
        return 0
    if not fresh:
        print("(strażnik nie działa w tle; pomiar jednorazowy, bez historii ciszy)\n")
    print_status(snap)
    for event in state.get("events", [])[-5:]:
        when = time.strftime("%H:%M", time.localtime(event["at"]))
        print(
            f"  {when} {event['action']} {event['label']}: {event['reason']} -> {event['result']}"
        )
    for event in state.get("lastresort", [])[-5:]:
        when = time.strftime("%H:%M", time.localtime(event["at"]))
        print(
            f"  {when} hamulec {event['code']} pid {event['pid']} {janitor.human(event['size'])} -> {event['result']}"
            + (f" | wznowienie: {event['resume']}" if event.get("resume") else "")
        )
    return 0


def cmd_brake(cfg, args):
    """`brake [--stage N] [--within PID] [--json]`: co hamulec zrobiłby teraz, bez sygnałów.

    --stage udaje stopień (np. 3, żeby zobaczyć ofiarę awarii przy spokojnym Macu); --within
    ogranicza tabelę procesów do drzewa pod PID i procesów, które się do niego przyznają
    (CLAUDE_PID), np. na sztucznym drzewie w próbie."""
    import lastresort

    state = janitor.load_json(STATE_PATH, {})
    world = World(cfg, dict(state), Orca(), time.time(), use_orca=False)
    p = world.pressure
    level = p.stage
    if "--stage" in args:
        level = int(args[args.index("--stage") + 1])
    if "--within" in args:
        root = int(args[args.index("--within") + 1])
        keep = set(descendants(root, _children_of(world.table)))
        keep |= {pid for pid in world.table if proc_env(pid).get("CLAUDE_ACC_BRAKE_PROBE") == str(root)}
        world.table = {pid: row for pid, row in world.table.items() if pid in keep}
        world.units = [u for u in world.units if set(u.pids) & keep]
    pick = lastresort.reap(world, {}, sys.modules[__name__], level=level, cfg=cfg, dry_run=True)
    out = {"stage": p.stage, "reasons": p.stage_reasons, "as_stage": level, "pick": pick}
    if "--json" in args:
        print(json.dumps(out, ensure_ascii=False))
        return 0
    print(f"Stopień teraz: {lastresort.STAGE_NAMES[p.stage]}" + (": " + "; ".join(p.stage_reasons) if p.stage_reasons else ""))
    print(f"Hamulec na stopniu „{lastresort.STAGE_NAMES[min(level, 3)]}” wybrałby: {pick or 'nic'}")
    return 0


def print_status(snap):
    p = snap["pressure"]
    h = janitor.human
    swap = f"swap {h(p['swap_used'])}/{h(p['swap_total'])}"
    if p["swap_growth"] > 64 * MB:
        swap += f" (+{h(p['swap_growth'])} w 2 min)"
    print(
        f"Pamięć: {('w normie', 'ostrzeżenie', 'KRYTYCZNA')[p['level']]} | {swap} | kompresor {h(p['compressed'])}"
        f" | dostępne {p['available']}% | jądro {p['kernel']}"
    )
    if p["reasons"] or p.get("notes"):
        print("  " + "; ".join(p["reasons"] + p.get("notes", [])))
    import lastresort

    stage = p.get("stage", 0)
    seg = ""
    if p.get("segments_limit"):
        seg = f", segmenty kompresora {p['segments'] / p['segments_limit'] * 100:.0f}% limitu"
    print(
        f"Hamulec: {lastresort.STAGE_NAMES[stage]}{seg}"
        + (": " + "; ".join(p.get("stage_reasons") or []) if p.get("stage_reasons") else "")
    )
    if snap.get("inventory"):
        print(inventory_line(snap["inventory"]))
    print(
        f"Dev serwery: {h(snap['total'])} z budżetu {h(snap['budget'])}"
        f" | {orcahost.host().name}: {'tak' if snap['orca'] else 'nie'} | tryb: {snap['mode']}"
    )
    plans = {pl["unit"]: pl for pl in snap["plans"]}
    for u in snap["units"]:
        who = []
        kinds = sorted({c["kind"] for c in u["clients"]})
        if kinds:
            who.append("+".join(kinds))
        if any(t["focused"] for t in u["tabs"]):
            who.append("patrzysz")
        elif u["tabs"]:
            who.append(f"{len(u['tabs'])} kart")
        flags = []
        if u["agent_working"]:
            flags.append("agent pracuje")
        if u.get("pin"):
            flags.append(pin_phrase(u["pin"]))
        elif u["protected"]:
            flags.append("chroniony")
        if u.get("background"):
            flags.append("QoS tła")
        where = f"terminal „{u['terminal']}”" if u["terminal"] else u["host"]
        print(
            f"\n  {u['label']}  {h(u['footprint'])} (szczyt {h(u['peak'])})"
            f"  cisza {u['quiet'] // 60}:{u['quiet'] % 60:02d}  oglądają: {', '.join(who) or 'nikt'}"
            f"  {where}{'  [' + ', '.join(flags) + ']' if flags else ''}"
        )
        plan = plans.get(u["key"])
        if plan:
            verb = {"stop": "zatrzymam", "recycle": "zrestartuję", "warn": "ostrzegę"}[
                plan["action"]
            ]
            print(f"    -> {verb}: {plan['reason']}")
    if not snap["units"]:
        print("  (żaden dev serwer nie działa)")
    sims = snap.get("simulators")
    if sims is None:
        return
    print(
        f"\nSymulatory: {len(sims)} włączone ({h(sum(s['footprint'] for s in sims))})"
        f", limit {snap.get('simulator_cap') or 'brak'}"
    )
    for s in sims:
        who = []
        if s["lease_alive"]:
            who.append(f"dzierżawa {s.get('lease_app') or '?'}")
        elif s.get("lease_app"):
            who.append("dzierżawa po martwej sesji")
        if s["watchers"]:
            who.append(f"ogląda {len(s['watchers'])} proc.")
        if not s["pool"]:
            who.append("Twój")
        if s["protected"]:
            who.append("chroniony")
        print(
            f"\n  {s['name']}  {h(s['footprint'])}  cisza {s['quiet'] // 60}:{s['quiet'] % 60:02d}"
            f"  {', '.join(who) or 'nieużywany'}"
        )
        plan = plans.get(s["key"])
        if plan:
            print(f"    -> wyłączę: {plan['reason']}")
    group = plans.get("simulators")
    if group:
        print(f"    -> ostrzegę: {group['reason']}")


# ---------- przypięcia ----------

DEFAULT_PIN_HOURS = 12


def load_pins(now=None):
    """Aktywne przypięcia; wygasłe pętla po prostu pomija, sprząta je dopiero `pin`/`unpin`."""
    now = now or time.time()
    pins = janitor.load_json(PINS_PATH, {}).get("pins", [])
    return [
        p
        for p in pins
        if isinstance(p, dict)
        and p.get("target")
        and (p.get("until") is None or p["until"] > now)
    ]


def save_pins(pins):
    os.makedirs(STATE_DIR, exist_ok=True)
    janitor.write_json(PINS_PATH, {"pins": pins})


def pin_target(raw, cwd=None):
    """`:3747` dla portu, bezwzględna ścieżka dla katalogu (serwera albo nad nim)."""
    raw = raw.strip()
    if raw.lstrip(":").isdigit():
        return ":" + raw.lstrip(":")
    return os.path.realpath(os.path.join(cwd or os.getcwd(), os.path.expanduser(raw)))


def pin_matches(pin, unit):
    target = pin["target"]
    if target.startswith(":"):
        return target[1:].isdigit() and int(target[1:]) in unit.ports
    return any(s.cwd == target or s.cwd.startswith(target + "/") for s in unit.servers)


def parse_duration(text):
    """`90m`, `12h`, `2d`, `1,5h` -> sekundy."""
    match = re.fullmatch(r"(\d+(?:[.,]\d+)?)\s*([mhd])", text.strip().lower())
    if not match:
        raise ValueError(f"nie rozumiem czasu {text!r}: podaj np. 90m, 12h albo 2d")
    value = float(match.group(1).replace(",", "."))
    return value * {"m": MINUTE, "h": HOUR, "d": 24 * HOUR}[match.group(2)]


def pin_phrase(pin):
    parts = ["przypięty"]
    if pin.get("until"):
        parts.append("do " + time.strftime("%d.%m %H:%M", time.localtime(pin["until"])))
    else:
        parts.append("bez terminu")
    if pin.get("level") == "hold":
        parts.append("bez restartów")
    text = " ".join(parts)
    return f"{text}: {pin['reason']}" if pin.get("reason") else text


def cmd_pin(_cfg, args):
    usage = "użycie: pin <:port|katalog> [--for 12h | --forever] [--reason TEKST] [--no-restart]"
    if not args or args[0].startswith("--"):
        print(usage)
        return 2
    now = time.time()
    target = pin_target(args[0])
    until, reason, level = now + DEFAULT_PIN_HOURS * HOUR, "", "keep"
    rest = list(args[1:])
    while rest:
        flag = rest.pop(0)
        if flag == "--for" and rest:
            until = now + parse_duration(rest.pop(0))
        elif flag == "--forever":
            until = None
        elif flag == "--reason" and rest:
            reason = rest.pop(0)
        elif flag == "--no-restart":
            level = "hold"
        else:
            print(usage)
            return 2
    pin = {
        "target": target,
        "until": until,
        "reason": reason,
        "level": level,
        "at": now,
    }
    pins = [p for p in load_pins(now) if p["target"] != target] + [pin]
    save_pins(pins)
    log(f"przypięcie {short(target)}: {pin_phrase(pin)}")
    print(f"{short(target)}: {pin_phrase(pin)}")
    return 0


def cmd_unpin(_cfg, args):
    if not args:
        print("użycie: unpin <:port|katalog|all>")
        return 2
    now = time.time()
    pins = load_pins(now)
    if args[0] == "all":
        gone, kept = pins, []
    else:
        target = pin_target(args[0])
        gone = [p for p in pins if p["target"] == target]
        kept = [p for p in pins if p["target"] != target]
    save_pins(kept)
    if not gone:
        print(f"nie ma przypięcia {args[0]}")
        return 1
    for pin in gone:
        log(f"zdjęte przypięcie {short(pin['target'])}")
    print(f"zdjęte: {', '.join(short(p['target']) for p in gone)}")
    return 0


def cmd_pins(_cfg, args):
    pins = load_pins()
    if "--json" in args:
        print(json.dumps(pins))
        return 0
    if not pins:
        print("(brak przypięć)")
    for pin in pins:
        print(f"  {short(pin['target'])}  {pin_phrase(pin)}")
    return 0


def find_unit(world, target):
    if target.startswith(":") and target[1:].isdigit():
        port = int(target[1:])
        return next((u for u in world.units if port in u.ports), None)
    if target.isdigit():
        pid = int(target)
        return next((u for u in world.units if pid in u.pids), None)
    return None


def cmd_manual(action):
    def run(cfg, args):
        if not args:
            print(f"użycie: {action} <pid|:port>")
            return 2
        state = janitor.load_json(STATE_PATH, {})
        orca = Orca()
        world = World(cfg, state, orca, time.time())
        unit = find_unit(world, args[0])
        if not unit:
            print(f"nie znam dev serwera {args[0]}")
            return 1
        execute(
            dict(cfg, notify=False), Plan(unit, action, 100, "na żądanie"), world, state
        )
        state["last_action"] = world.now  # pętla w tle też odczeka swoje
        save_state(state)
        print(state["events"][-1]["result"] if state.get("events") else "gotowe")
        return 0

    return run


# ---------- hook dla agentów (szybka ścieżka i dev_starts: devguard.py) ----------


def package_dir(root, package):
    """Katalog pakietu o tej nazwie w monorepo (apps/*, packages/*)."""
    for group in ("apps", "packages", "."):
        base = os.path.join(root, group)
        try:
            entries = os.scandir(base)
        except OSError:
            continue
        with entries:
            for entry in entries:
                manifest = os.path.join(entry.path, "package.json")
                if not entry.is_dir() or not os.path.exists(manifest):
                    continue
                name = janitor.load_json(manifest, {}).get("name", "")
                if package in (name, entry.name, name.split("/")[-1]):
                    return entry.path
    return None


def deny(reason):
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            }
        )
    )
    return 0


def describe(unit, app=None):
    """Adres i katalog jednego serwera jednostki: z `app` tego, który serwuje tę aplikację, bo w
    stosie `pnpm dev` pierwszy port i pierwszy katalog to zwykle dwie różne, inne aplikacje."""
    server = next((s for s in unit.servers if app and s.cwd == app), unit.servers[0])
    ports = server.ports or unit.ports
    url = f"http://localhost:{ports[0]}" if ports else f"pid {unit.root}"
    more = f", w stosie {len(unit.servers)} serwerów" if len(unit.servers) > 1 else ""
    where = f", terminal Orki „{unit.terminal.get('title')}”" if unit.terminal else ""
    return f"{url} ({short(server.cwd)}, {janitor.human(unit.footprint)}{more}{where})"


def cmd_admit(cfg, _args):
    try:
        event = json.load(sys.stdin)
    except ValueError:
        return 0
    if event.get("tool_name") != "Bash":
        return 0
    reason = devserver_refusal(cfg, event)
    if reason:
        return deny(reason)
    out = sched_rewrite(event)
    if out:
        print(json.dumps(out))
    return 0


def memory_refusal(cfg, world, state, app=None):
    """Dlaczego pamięć nie wpuści teraz nowego dev serwera (aplikacji w katalogu `app`), albo None.

    Dwa powody, oba mijają z czasem, więc odmowa podaje agentowi warunek do czekania (`room`):
    presja krytyczna, także gdy nie działa już żaden dev serwer (strażnik właśnie zatrzymał
    ostatni), i serwer tej aplikacji, który strażnik zatrzymał z braku pamięci mniej niż
    `restart_hold_minutes` temu, póki Mac nie odetchnął. Bez tego drugiego zatrzymany serwer
    wracał od razu: po akcji strażnik mierzy przyrost swapu od nowa, więc przez chwilę presja
    wygląda na mniejszą (2026-10-08: Metro zatrzymane o 18:45 przy 11,2 GB swapu, agent postawił
    je znowu, o 18:52 swap 17,9 GB, o 18:57 Mac zamarzł)."""
    pressure = world.pressure
    if pressure.level == 2:
        return "pamięć na krytycznym poziomie (" + ", ".join(pressure.reasons) + ")"
    if app is None:
        return None
    if pressure.level == 0 and pressure.swap_used < cfg["swap_warn_percent"] / 100 * pressure.ram:
        return None
    hold = cfg["restart_hold_minutes"] * MINUTE
    for event in reversed(state.get("events", [])):
        if world.now - event.get("at", 0) >= hold:
            break
        if event.get("action") != "stop" or event.get("code") != "pressure":
            continue
        paths = [event.get("cwd")] + list(event.get("apps") or [])
        if not any(p and os.path.realpath(p) == app for p in paths):
            continue
        now_why = ", ".join(pressure.reasons) or f"swap {janitor.human(pressure.swap_used)}"
        return (
            f"strażnik zatrzymał serwer {short(app)} o "
            f"{time.strftime('%H:%M', time.localtime(event['at']))} z braku pamięci "
            f"({event.get('reason', '').removeprefix('brak pamięci: ')}), a Mac jeszcze nie "
            f"odetchnął ({now_why})"
        )
    return None


def wait_hint(app):
    """Dokładna komenda, którą agent czeka, aż pamięć wpuści serwer."""
    condition = f"claude-acc guard room {shlex.quote(app)}"
    return (
        f"Poczekaj: `claude-acc sched wait -- {shlex.quote(condition)}` (kod 75: zawołaj jeszcze "
        "raz), potem uruchom komendę ponownie."
    )


def devserver_refusal(cfg, event):
    """Powód odmowy startu dev serwera albo None."""
    command = (event.get("tool_input") or {}).get("command") or ""
    if "DEVGUARD_ALLOW=1" in command:
        return None
    cwd = event.get("cwd") or os.getcwd()
    starts = dev_starts(command, cwd)
    if not starts:
        return None
    state = janitor.load_json(STATE_PATH, {})
    world = World(cfg, state, Orca(), time.time(), use_orca=False)
    apps = []
    for target, package, stack, reuses in starts:
        app = package_dir(target, package) if package else target
        app = os.path.realpath(app or target)
        apps.append(app)
        if reuses:
            continue  # `expo run:ios` sam weźmie Metro, które już serwuje tę aplikację
        same = [
            u
            for u in world.units
            if any(s.cwd == app for s in u.servers)
            or (stack and os.path.realpath(u.launch_cwd) == app)
        ]
        if same:
            return (
                f"Strażnik dev serwerów: dla {short(app)} już działa {describe(same[0], app)}. "
                "Użyj tego adresu, nie stawiaj drugiego serwera tej samej aplikacji: "
                "drugi zjada kolejne gigabajty i dubluje rekompilacje przy każdej edycji."
            )
    allow = "Tylko na wyraźne polecenie użytkownika poprzedź komendę DEVGUARD_ALLOW=1."
    listing = "; ".join(
        describe(u) for u in sorted(world.units, key=lambda u: -u.footprint)[:5]
    )
    for app in apps:
        why = memory_refusal(cfg, world, state, app)
        if why:
            running = f" Działają: {listing}; użyj któregoś z nich albo poczekaj." if listing else ""
            return f"Strażnik dev serwerów: {why}.{running} {wait_hint(app)} {allow}"
    total = sum(u.footprint for u in world.units)
    budget = cfg["budget_percent"] / 100 * world.pressure.ram
    if world.units and total + 1.5 * GB > budget:
        return (
            f"Strażnik dev serwerów: dev serwery zajmują już {janitor.human(total)} z budżetu "
            f"{janitor.human(budget)}. Działają: {listing}. Użyj któregoś z nich albo poproś "
            "użytkownika o zgodę; do zrzutów ekranu i pomiarów wystarczy `next build && next start`. "
            + allow
        )
    return None


def cmd_room(cfg, args):
    """Kod 0, gdy pamięć wpuści nowy dev serwer (aplikacji w podanym katalogu), 1 z powodem."""
    app = os.path.realpath(os.path.expanduser(args[0])) if args else None
    state = janitor.load_json(STATE_PATH, {})
    world = World(cfg, state, Orca(), time.time(), use_orca=False)
    why = memory_refusal(cfg, world, state, app)
    print(why or "jest miejsce")
    return 1 if why else 0


COMMANDS = {
    "run": cmd_run,
    "once": cmd_once,
    "caps": cmd_caps,
    "status": cmd_status,
    "stop": cmd_manual("stop"),
    "recycle": cmd_manual("recycle"),
    "pin": cmd_pin,
    "unpin": cmd_unpin,
    "pins": cmd_pins,
    "admit": cmd_admit,
    "room": cmd_room,
    "brake": cmd_brake,
}


def main(argv):
    cmd = argv[0] if argv else "status"
    if cmd not in COMMANDS:
        print(USAGE)
        return 2
    try:
        return COMMANDS[cmd](load_config(), argv[1:])
    except Exception as err:
        if cmd == "admit":
            return 0  # hook nigdy nie blokuje agenta przez własny błąd
        if cmd == "room":
            # agent czeka na tym w pętli: własny błąd go nie zatrzymuje, hook i tak oceni komendę
            print(f"błąd: {err}", file=sys.stderr)
            return 0
        log(f"{cmd}: błąd {err!r}")
        print(f"błąd: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
