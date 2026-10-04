#!/usr/bin/env python3
"""Strażnik dev serwerów: agenci w Orce nie zajadą Maca serwerami `next dev`.

Skąd problem (zmierzone 2026-10-04 na 48 GB RAM):
- `next dev` z Turbopackiem trzyma graf modułów w natywnej pamięci Rusta i po godzinie
  pracy agenta dobija do 7-8 GB footprintu. Wbudowany watchdog Next.js patrzy tylko na
  stertę V8 (restart przy 80% heap), więc tych gigabajtów nie widzi;
- otwarty podgląd (karta w Orce, przeglądarka) trzyma websocket HMR: każdy plik zapisany
  przez agenta to rekompilacja i przeładowanie strony (jedna zmiana tokens.css = 10 s CPU),
  a serwer bez podglądu na tę samą zmianę prawie nie reaguje;
- kilka worktree, w każdym agent ze swoim serwerem i kartą, i swap rośnie do pełna, a jądro
  do końca zgłasza presję "normalną". Potem jetsam ubija procesy z powodem low-swap.

Co robi strażnik:
- mierzy to, co mierzy jądro: phys_footprint z proc_pid_rusage (ten sam licznik, którym
  jetsam wybiera ofiary), czas CPU i zapisy na dysk, bez forkowania niczego na proces;
- wie, kto ogląda serwer: klientów TCP z jednego lsof (Orca, przeglądarka, headless)
  i karty podglądu z Orki, a także to, czy patrzysz na nią w tej chwili;
- presję liczy ze swapu i z licznika swapoutów kompresora; poziom presji jądra jest
  tylko dodatkiem, bo przy pełnym swapie potrafi dalej mówić "normal";
- serwer, na który nie patrzysz, dostaje QoS tła Darwina (PRIO_DARWIN_BG: tylko rdzenie
  energooszczędne, dławione IO), więc burze rekompilacji po edycjach agentów nie lagują
  interfejsu; gdy przełączysz się na jego podgląd, wraca do zwykłego priorytetu;
- spuchnięty serwer restartuje w tym samym terminalu Orki: Turbopack wstaje z cache
  na dysku w kilka sekund, a karta podglądu sama się podłącza;
- zatrzymuje duplikaty tej samej aplikacji, sieroty po zamkniętych agentach i serwery,
  których nikt nie ogląda; serwera, na który patrzysz, nigdy nie zatrzymuje, najwyżej
  restartuje przy krytycznej presji;
- `admit` to hook PreToolUse dla Claude Code: agent nie postawi drugiego serwera tej
  samej aplikacji, tylko dostanie adres tego, który już działa.

Komendy:
  run                   pętla dla launchd: pomiar co kilka sekund, najwyżej jedna akcja naraz
  status [--json]       serwery, pamięć, kto je ogląda i co strażnik z nimi zrobi
  once [--dry-run]      jeden pomiar i co najwyżej jedna akcja
  stop <pid|:port>      zatrzymaj serwer tak, jak robi to strażnik
  recycle <pid|:port>   restart w tym samym terminalu Orki
  admit                 hook PreToolUse (Bash) dla Claude Code; zdarzenie czyta z stdin
"""

import sys

DEV_WORDS = ("dev", "vite", "expo", "serve")
GO_WORDS = ("go ", "golangci-lint", "make", "govulncheck")


def sched_rewrite(event):
    """Komenda Go agenta owinięta w scheduler (sched.py obok tego pliku): wyjście hooka z
    updatedInput albo None. Jedyny hook, który przepisuje komendy Bash; nigdy nie blokuje."""
    try:
        import importlib.util
        import os as _os

        path = _os.path.join(_os.path.dirname(_os.path.realpath(__file__)), "sched.py")
        if not _os.path.exists(path):
            return None
        spec = importlib.util.spec_from_file_location("acc_sched", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.hook_rewrite(event)
    except Exception:  # hook nigdy nie blokuje agenta przez własny błąd
        return None


# Hook idzie przy każdym poleceniu Bash każdego agenta, więc komenda bez śladu dev serwera
# ani Go kończy się tutaj, zanim załadują się ctypes, janitor i wyrażenia regularne. Słowa to
# minimum, które ma każda komenda pasująca do START (dev, vite, expo, webpack serve);
# fałszywy alarm (np. "dev" w nazwie pliku) idzie po prostu pełną ścieżką. Komenda z Go,
# a bez dev serwera, idzie od razu do schedulera, bez reszty tego pliku.
if __name__ == "__main__" and sys.argv[1:2] == ["admit"]:
    import io
    import json

    _event = sys.stdin.read()
    try:
        _parsed = json.loads(_event)
        _command = (_parsed.get("tool_input") or {}).get("command") or ""
    except (ValueError, AttributeError):
        sys.exit(0)
    if not any(word in _command for word in DEV_WORDS):
        if any(word in _command for word in GO_WORDS):
            _out = sched_rewrite(_parsed)
            if _out:
                print(json.dumps(_out))
        sys.exit(0)
    sys.stdin = io.StringIO(_event)

import ctypes
import ctypes.util
import fcntl
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
from urllib.parse import urlsplit

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import janitor

STATE_DIR = janitor.STATE_DIR
CONFIG_PATH = os.path.join(STATE_DIR, "devguard.json")
STATE_PATH = os.path.join(STATE_DIR, "devguard-state.json")
LOG_PATH = os.path.join(STATE_DIR, "devguard.log")
LOCK_PATH = os.path.join(STATE_DIR, "devguard.lock")

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
# tego nie zabijamy nigdy, nawet gdyby trafiło do drzewa serwera
SACRED = re.compile(
    r"(^|/)(claude|codex|login|launchd)(\s|$)|Orca\.app|^-?(\S*/)?(zsh|bash|fish)(\s|$)"
)
BROWSER = re.compile(
    r"Brave Browser|Google Chrome(?! for Testing)|Safari|com\.apple\.WebKit|firefox|Arc\.app|Microsoft Edge|Chromium|Vivaldi|Opera"
)
HEADLESS = re.compile(
    r"Chrome for Testing|HeadlessChrome|headless_shell|ms-playwright|puppeteer"
)
LOOPBACK = {"127.0.0.1", "[::1]", "::1", "localhost", "0.0.0.0", "*"}


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
RUSAGE_INFO_V4 = 4
PROC_PIDVNODEPATHINFO = 9
VNODEPATHINFO_SIZE = 2352  # dwa vnode_info_path: 152 bajty vnode_info + MAXPATHLEN
KERN_PROCARGS2 = 49


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


def proc_argv(pid):
    """Dokładne argv procesu z KERN_PROCARGS2; ps skleja argumenty spacjami i gubi cudzysłowy."""
    mib = (ctypes.c_int * 3)(1, KERN_PROCARGS2, pid)
    size = ctypes.c_size_t(0)
    if _libc.sysctl(mib, 3, None, ctypes.byref(size), None, 0) or not size.value:
        return None
    buf = ctypes.create_string_buffer(size.value)
    if _libc.sysctl(mib, 3, buf, ctypes.byref(size), None, 0):
        return None
    raw = buf.raw[: size.value]
    argc = int.from_bytes(raw[:4], "little")
    _exe, _, rest = raw[4:].partition(b"\0")
    args = rest.lstrip(b"\0").split(b"\0")[:argc]
    return [a.decode(errors="replace") for a in args] if len(args) == argc else None


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
        self.available = sysctl_int("kern.memorystatus_level")
        self.swap_total, self.swap_used = swap_usage()
        self.compressed = sysctl_int("vm.compressor_bytes_used") or 0
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
        if self.kernel >= 4:
            self.reasons.append("jądro: presja krytyczna")
        if available <= cfg["available_critical_percent"]:
            self.reasons.append(f"dostępne tylko {available}% pamięci")
        if self.swap_used >= critical and self.swapping:
            self.reasons.append(f"swap {janitor.human(self.swap_used)} i rośnie")
        if self.reasons:
            self.level = 2
            return
        if self.kernel >= 2:
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

    def summary(self):
        return {
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


class Orca:
    """Karty podglądu, worktree, agenci i terminale z działającej Orki, przez jej CLI.

    Orki nie uruchamia: gdy aplikacja nie chodzi, strażnik działa bez tej wiedzy.
    """

    def __init__(self):
        # DEVGUARD_ORCA podmienia CLI (testy); pusta wartość wyłącza Orkę
        self.bin = os.environ.get("DEVGUARD_ORCA", janitor.which("orca"))
        self.ok = False
        self.at = 0
        self.sessions_at = 0
        self.tabs = []
        self.worktrees = []
        self.terminals = []
        self.sessions = {}  # pid procesu terminala (login) -> ptyId

    def call(self, *args, timeout=10):
        if not self.bin:
            return None
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

    def refresh(self, rows, now, every, sessions_every=60):
        if not self.bin or not any(
            "Orca.app/Contents/MacOS/Orca" in r[5] for r in rows
        ):
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


def sockets():
    """Gniazda TCP użytkownika z jednego lsof: ({port: {pid}}, [(pid klienta, port serwera)])."""
    try:
        out = subprocess.run(
            ["lsof", "-nP", "-w", "-a", "-u", str(os.getuid()), "-iTCP", "-FpnT"],
            capture_output=True,
            text=True,
            timeout=30,
            env=janitor.ENV,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return {}, []
    listen, links = {}, []
    pid, name = None, ""
    for line in out.splitlines():
        tag, value = line[:1], line[1:]
        if tag == "p":
            pid = int(value)
        elif tag == "n":
            name = value
        elif tag == "T" and value.startswith("ST="):
            state = value[3:]
            if state == "LISTEN":
                port = name.rpartition(":")[2]
                if port.isdigit():
                    listen.setdefault(int(port), set()).add(pid)
            elif state == "ESTABLISHED" and "->" in name:
                local, remote = name.split("->", 1)
                local_host = local.rpartition(":")[0]
                remote_host, _, port = remote.rpartition(":")
                if port.isdigit() and (
                    remote_host in LOOPBACK or remote_host == local_host
                ):
                    links.append((pid, int(port)))
    return listen, links


def client_kind(command):
    if "Orca.app" in command:
        return "orca"
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
        self.background = False
        self.cpu = sum(s.cpu for s in servers)
        self.ports = sorted({p for s in servers for p in s.ports})
        self.clients = []  # (pid, rodzaj, nazwa)
        self.tabs = []
        self.terminal = None
        self.worktree = None
        self.consumers = []  # worktree, z których agenci oglądają serwer
        self.protected = False
        # z historii
        self.age = 0
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
            "host": self.host,
            "command": shlex.join(self.argv) if self.argv else None,
            "terminal": (self.terminal or {}).get("title"),
            "worktree": (self.worktree or {}).get("path"),
            "clients": [{"pid": p, "kind": k, "name": n} for p, k, n in self.clients],
            "tabs": [
                {"url": t.get("url"), "focused": t.get("focused")} for t in self.tabs
            ],
            "attended": self.attended,
            "agent_working": self.agent_working,
            "recyclable": self.recyclable,
            "age": round(self.age),
            "quiet": round(self.quiet),
            "protected": self.protected,
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
    table = {pid: (ppid, command) for pid, ppid, _age, _cpu, _rss, command in rows}
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


class World:
    """Wszystko, co strażnik wie w jednej chwili."""

    def __init__(self, cfg, state, orca, now, use_orca=True):
        self.now = now
        rows = janitor.processes()
        self.units, self.table = discover(cfg, rows)
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
        if use_orca and self.units:
            orca.refresh(rows, now, cfg["orca_seconds"])
        self.orca = orca if orca.ok else None
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
            unit.quiet = now - busy
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
        self.action = action  # "stop", "recycle", "warn"
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

    for plan in plans:
        if plan.action != "recycle":
            continue
        count = recent_recycles(state, plan.unit.app_key, now)
        if count >= cfg["max_recycles_per_hour"]:
            plan.data["restarts"] = count
            if plan.unit.attended:
                plan.action, plan.code = "warn", "loop_watched"
                plan.reason += (
                    f"; {count} restarty w godzinę, nie ruszam, bo go oglądasz"
                )
            else:
                plan.action, plan.code = "stop", "loop"
                plan.reason += f"; {count} restarty w godzinę to pętla, zatrzymuję"

    best = {}
    for plan in sorted(plans, key=lambda p: -p.priority):
        best.setdefault(plan.unit.key, plan)
    return sorted(best.values(), key=lambda p: -p.priority)


# ---------- akcje ----------


def terminate(unit, table, grace=10):
    """SIGTERM do całego drzewa naraz, po `grace` sekundach SIGKILL dla tych, które zostały."""
    targets = []
    for pid in unit.pids:
        command = table.get(pid, (0, ""))[1]
        info = usage(pid)
        if info and not SACRED.search(command) and pid != os.getpid():
            targets.append((pid, info["start"]))
    for pid, _start in targets:
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    deadline = time.time() + grace
    while time.time() < deadline:
        left = [(p, s) for p, s in targets if alive(p, s)]
        if not left:
            return []
        time.sleep(0.25)
    for pid, start in targets:
        if alive(pid, start):
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
    return [p for p, s in targets if alive(p, s)]


def shell_idle(shell):
    """Czy powłoka wróciła do promptu, czyli nie ma już żadnych dzieci."""
    for _ in range(40):
        rows = janitor.processes()
        if not any(ppid == shell for _pid, ppid, *_ in rows):
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
        return False, "nie mam terminala Orki, w którym mógłbym go postawić"
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
        return False, f"Orca nie przyjęła komendy; wpisz ręcznie: {text}"
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
    if current and not current.startswith("devguard"):
        return
    orca.call(
        "worktree",
        "set",
        "--worktree",
        f"id:{worktree['worktreeId']}",
        "--comment",
        text[:200],
    )


def execute(cfg, plan, world, state):
    unit, now = plan.unit, world.now
    size = janitor.human(unit.footprint)
    if plan.action == "warn":
        warned = state.setdefault("warned", {})
        if now - warned.get(unit.app_key, 0) < HOUR:
            return
        warned[unit.app_key] = now
        log(f"uwaga {unit.label} {size}: {plan.reason}")
        if cfg["notify"]:
            janitor.notify("Dev serwer puchnie", f"{unit.label}: {plan.reason}")
        return
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


def tick(cfg, state, orca, dry_run=False, now=None, qos=False):
    now = now or time.time()
    world = World(cfg, state, orca, now)
    check_pending(world, state)
    plans = decide(cfg, world, state)
    enforce = cfg["mode"] == "enforce" and not dry_run
    acted = None
    if (
        plans
        and enforce
        and now - state.get("last_action", 0) >= cfg["cooldown_seconds"]
    ):
        plan = plans[0]
        execute(cfg, plan, world, state)
        if plan.action != "warn":
            state["last_action"] = now
            # przyrost swapu sprzed akcji nie może wywołać następnej: pomiar od nowa
            state["swap_history"] = []
            acted = plan
    history = state.setdefault("history", [])
    if not history or now - history[-1][0] >= 30:
        p = world.pressure
        history.append(
            [round(now), sum(u.footprint for u in world.units), p.swap_used, p.compressed]
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
        "plans": [p.summary() for p in plans],
        "acted": acted.summary() if acted else None,
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
            time.sleep(cfg["interval_seconds"])
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
    print(
        f"Dev serwery: {h(snap['total'])} z budżetu {h(snap['budget'])}"
        f" | Orca: {'tak' if snap['orca'] else 'nie'} | tryb: {snap['mode']}"
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
        if u["protected"]:
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


# ---------- hook dla agentów ----------

# komenda stawiająca dev serwer, po zdjęciu opakowań (zmienne, rtk proxy, npx, pnpm exec)
START = re.compile(
    r"^(?:next\s+dev|vite(?:\s+(?:dev|serve))?(?=\s+-|\s*$)|expo\s+start"
    r"|webpack(?:-cli)?\s+serve|astro\s+dev|nuxi?\s+dev"
    r"|(?:pnpm|yarn)\s+(?:--filter|-F)[\s=](?P<pkg>\S+)\s+(?:run\s+)?dev(?::\S+)?"
    r"|(?:pnpm|npm|yarn|bun)\s+(?:run\s+)?dev(?::\S+)?|turbo\s+(?:run\s+)?dev)(?=\s|$)"
)
WRAPPERS = re.compile(
    r"^(?:\w+=\S*\s+|(?:rtk\s+proxy|nohup|exec|time|env|caffeinate(?:\s+-\w+)*)\s+"
    r"|(?:npx|bunx|(?:pnpm|yarn|npm)(?:\s+(?:-C|--dir|--prefix|--cwd)[\s=]\S+)*\s+(?:exec|dlx))"
    r"\s+(?:--\S+\s+)*)+"
)
DIR_FLAG = re.compile(r"\s(-C|--dir|--prefix|--cwd)[\s=](\S+)")
# komenda w cudzysłowie, którą uruchomi ktoś inny: `orca terminal create --command`, `sh -c`
NESTED = re.compile(
    r"""(?:--command|\b(?:ba|z)?sh\s+-c)[\s=](?:"((?:[^"\\]|\\.)*)"|'([^']*)')"""
)


def dev_starts(command, cwd):
    """[(katalog, filtr pakietu albo None, cały stos?)] dla każdej komendy stawiającej dev serwer."""
    found = []
    for inner in NESTED.findall(command):
        found += dev_starts(inner[0] or inner[1], cwd)
    for segment in re.split(r"&&|\|\||[;|\n&]", command):
        segment = segment.strip().strip("()").strip()
        cd = re.match(r"^cd\s+(\S+)$", segment)
        if cd:
            cwd = os.path.normpath(
                os.path.join(cwd, os.path.expanduser(cd.group(1).strip("'\"")))
            )
            continue
        bare = WRAPPERS.sub("", segment)
        match = START.match(bare)
        if not match:
            continue
        target = cwd
        flag = DIR_FLAG.search(" " + segment)
        if flag:
            target = os.path.normpath(
                os.path.join(cwd, os.path.expanduser(flag.group(2).strip("'\"")))
            )
        package = match.group("pkg")
        # skrypt `dev` z package.json albo turbo: stawia to, co zdefiniował projekt
        stack = not package and bool(re.match(r"^(pnpm|npm|yarn|bun|turbo)\s", bare))
        found.append((target, package, stack))
    return found


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


def describe(unit):
    url = f"http://localhost:{unit.ports[0]}" if unit.ports else f"pid {unit.root}"
    where = f", terminal Orki „{unit.terminal.get('title')}”" if unit.terminal else ""
    return (
        f"{url} ({short(unit.servers[0].cwd)}, {janitor.human(unit.footprint)}{where})"
    )


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
    for target, package, stack in starts:
        app = package_dir(target, package) if package else target
        app = os.path.realpath(app or target)
        same = [
            u
            for u in world.units
            if any(s.cwd == app for s in u.servers)
            or (stack and os.path.realpath(u.launch_cwd) == app)
        ]
        if same:
            return (
                f"Strażnik dev serwerów: dla {short(app)} już działa {describe(same[0])}. "
                "Użyj tego adresu, nie stawiaj drugiego serwera tej samej aplikacji: "
                "drugi zjada kolejne gigabajty i dubluje rekompilacje przy każdej edycji."
            )
    total = sum(u.footprint for u in world.units)
    budget = cfg["budget_percent"] / 100 * world.pressure.ram
    if world.units and (world.pressure.level == 2 or total + 1.5 * GB > budget):
        listing = "; ".join(
            describe(u) for u in sorted(world.units, key=lambda u: -u.footprint)[:5]
        )
        why = (
            "pamięć na krytycznym poziomie (" + ", ".join(world.pressure.reasons) + ")"
            if world.pressure.level == 2
            else f"dev serwery zajmują już {janitor.human(total)} z budżetu {janitor.human(budget)}"
        )
        return (
            f"Strażnik dev serwerów: {why}. Działają: {listing}. Użyj któregoś z nich albo poproś "
            "użytkownika o zgodę; do zrzutów ekranu i pomiarów wystarczy `next build && next start`. "
            "Tylko na wyraźne polecenie użytkownika poprzedź komendę DEVGUARD_ALLOW=1."
        )
    return None


COMMANDS = {
    "run": cmd_run,
    "once": cmd_once,
    "status": cmd_status,
    "stop": cmd_manual("stop"),
    "recycle": cmd_manual("recycle"),
    "admit": cmd_admit,
}


def main(argv):
    cmd = argv[0] if argv else "status"
    if cmd not in COMMANDS:
        print(__doc__)
        return 2
    try:
        return COMMANDS[cmd](load_config(), argv[1:])
    except Exception as err:
        if cmd == "admit":
            return 0  # hook nigdy nie blokuje agenta przez własny błąd
        log(f"{cmd}: błąd {err!r}")
        print(f"błąd: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
