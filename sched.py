#!/usr/bin/env python3
"""Scheduler komend Go, JS i natywnych buildów agentów: wpuszcza joby po pamięci zamiast jednego
zamka na wszystko. Natywne to buildy iOS i Androida (xcodebuild, expo run, eas build --local,
gradle, pod install) i start symulatora; jeden taki build to 6-12 GB, więc czeka jak ciężki Go.

  sched.py run [--timeout S] [--session ID] [--agent NAME] [--via hook|plock|cli]
               (--shell 'KOMENDA' | -- ARGV...)
  sched.py status [--json]
  sched.py classify (--shell 'KOMENDA' | -- ARGV...)
  sched.py depot [--max-age S] [--json]
  sched.py wait [--max S] [--every S] -- 'WARUNEK'
  sched.py rtk-excludes [--write [PATH]]
  sched.py codex install|uninstall|status

`run` klasyfikuje komendę (moduł, czasownik, zakres pakietów), przewiduje jej szczyt pamięci i
czas z historii, a potem:
  - mieści się teraz (szczyt <= dostępne - zapas - miejsce na dev serwer - wzrost biegnących):
    uruchamia od razu, lokalnie;
  - nie mieści się nawet na pustym Macu: wysyła na Depot (scripts/depot-ci.sh albo
    scripts/depot-exec.sh w repo), jeśli jest trasa;
  - musi czekać: zostaje w kolejce, chyba że czekanie + bieg lokalnie minus czas na Depot jest
    warte więcej niż λ × jednostki Depot.
Komenda biegnie jako dziecko tego procesu: agent widzi jej wyjście na żywo i dostaje jej kod
wyjścia. Stan dla panelu: sched/state.json, historia: sched/history.jsonl (docs/sched.md).
Hook PreToolUse (devguard.py admit) owija komendy agentów przez hook_rewrite().
`depot` zapisuje do sched/depot.json biegi Depot CI całej organizacji, także te, które
wystartowały poza schedulerem (bramka pushu, scripts/depot-ci.sh agentów); aplikacja woła go,
dopóki panel jest otwarty.
`wait` czeka na warunek najwyżej --max sekund (domyślnie 270), żeby subagent z 5-minutowym
cache wołał go w kółko zamiast blokować się dłużej, niż żyje jego cache.
"""

import functools  # bez kosztu: `re` i tak go ładuje
import json
import os
import re
import shlex
import sys
import time
import types

# ctypes, subprocess, hashlib, random, threading, signal i fcntl ładują się w funkcjach, które
# ich używają: hook (hook_rewrite) idzie przy każdej komendzie Go agenta i potrzebuje tylko
# klasyfikacji, a te importy to razem ~20 ms (sam ctypes.util z find_library ~10 ms)

HOME = os.path.expanduser("~")
STATE_DIR = os.path.join(HOME, ".local/share/claude-acc")
SCHED_DIR = os.path.join(STATE_DIR, "sched")
STATE_PATH = os.path.join(SCHED_DIR, "state.json")
HISTORY_PATH = os.path.join(SCHED_DIR, "history.jsonl")
LOCK_PATH = os.path.join(SCHED_DIR, "lock")
CONFIG_PATH = os.environ.get("SCHED_CONFIG") or os.path.join(SCHED_DIR, "config.json")
CACHE_PATH = os.path.join(SCHED_DIR, "cache.json")
DEPOT_PATH = os.path.join(SCHED_DIR, "depot.json")
DEPOT_LOCK = os.path.join(SCHED_DIR, "depot.lock")
DEVGUARD_STATE = os.path.join(STATE_DIR, "devguard-state.json")
DEVGUARD_CONFIG = os.path.join(STATE_DIR, "devguard.json")
GUARD_FRESH_S = 60  # strażnik pisze stan co 5 s; starszy niż minuta to strażnik, który stoi
SELF = os.path.join(STATE_DIR, "sched.py")

GB = 1024**3
VERSION = 1
DEFAULTS = {
    "headroom_gb": 4.0,
    "lambda_s_per_unit": 6.0,
    "unit_usd": 0.006,
    "small_gb": 4.5,
    "small_wall_s": 120,
    "starve_s": 120,
    # głowa, której startu nie da się przewidzieć, przepuszcza tylko job, którego prognoza
    # × 2 + 10 s mieści się w tylu sekundach: o tyle najwyżej ją opóźni
    "head_delay_s": 60,
    "drop_count1": True,
    "pause_swap_gb": 0.5,
    "ldflags_w_for_build": True,
    "depot_eta_since": "2026-10-05",
    # pliki pomocników testów (ścieżka w module, prefiks), których exec nie psuje cache testów:
    # szablon testpg czyta migracje, Dockerfile i atlasa w procesie testu (os.ReadFile, LookPath)
    "count1_trusted_exec": ["internal/testhelpers/testpg"],
    "idle_floor_pct": 65,
    # organizacja Depot dla `depot` (pusta: domyślna organizacja CLI; potrzebna przy kilku)
    "depot_org": "",
    "node": True,  # testy, buildy i typecheck JS w kolejce; false wyłącza
    "native": True,  # buildy iOS i Androida, pody i start symulatora w kolejce; false wyłącza
}
PUBLIC_CONFIG = (
    "headroom_gb",
    "lambda_s_per_unit",
    "unit_usd",
    "small_gb",
    "small_wall_s",
)
DEPOT_SIZES = (2, 4, 8, 16, 32, 64)
DEPOT_SETUP_S = 45  # łatka, kolejka i start maszyny
EXIT_TIMEOUT = 75  # jak plock.py: czas oczekiwania minął
EXIT_DEPOT_NEVER_RAN = 125  # depot-exec: komenda nie wystartowała

# szczyt drzewa procesów (GB) i czas (s) z pomiarów 2026-10-04/05 na M4 Max 48 GB
# (docs/perf-research.md); klucz: (moduł, rodzaj, zakres, tylko kompilacja); wartość: {p: (GB, s)}
# albo (GB, s), gdy -p nie zmienia wyniku
PRIORS = {
    ("charter-service", "build", "tree", False): (6.6, 70),
    ("charter-service", "vet", "tree", False): {
        2: (8.3, 61),
        4: (13.7, 41),
        8: (25.7, 54),
    },
    ("charter-service", "test", "tree", True): {
        2: (5.6, 258),
        4: (8.4, 150),
        6: (9.6, 114),
        8: (11.6, 72),
    },
    ("charter-service", "test", "tree", False): (24.0, 1500),
    ("charter-service", "test", "handlers", True): (4.4, 25),
    ("charter-service", "test", "handlers", False): (24.0, 1500),
    ("charter-service", "test", "pkg", True): (4.4, 30),
    ("charter-service", "test", "pkg", False): (3.0, 40),
    ("charter-service", "lint", "tree", False): (14.5, 85),
    ("charter-service", "make", "test-tenant-leakage", False): (8.0, 460),
    ("charter-service", "make", "test", False): (30.0, 1800),
    ("charter-service", "make", "lint", False): (14.5, 120),
}
GENERIC = {
    ("build", "tree"): (6.0, 60),
    ("vet", "tree"): (10.0, 60),
    ("test", "tree"): (10.0, 150),
    ("lint", "tree"): (12.0, 90),
    ("make", None): (8.0, 300),
    ("generate", None): (4.0, 60),
    ("run", None): (4.0, 60),
    ("script", None): (8.0, 300),
}
SMALL_MODULES = ("auth-service",)  # cały moduł nigdy nie przekroczył 1,8 GB
HANDLERS = "internal/handlers"  # jeden pakiet, a pamięci i czasu tyle co cały charter-service
RACE_FACTOR = 1.4
# klasy, które stary hak depot-heavy-go.sh wysyłał na Depot, i ich job Depot CI:
# (moduł, rodzaj, zakres, tylko kompilacja) -> (job, rdzenie, p50 s)
DEPOT_CI_JOBS = {
    ("charter-service", "test", "tree", False): ("full", 64, 510),
    ("auth-service", "test", "tree", False): ("full-auth", 4, 60),
    ("charter-service", "test", "handlers", False): ("handlers", 8, 420),
    ("charter-service", "make", "test-tenant-leakage", False): ("isolation", 16, 504),
    ("charter-service", "make", "test", False): ("full", 64, 510),
}
# pakiety testów, które sięgają po Postgresa (szablon testpg): depot-exec dostaje --with pg
PG_MARKERS = ("/internal/testhelpers", "/internal/testpg", "/internal/testutil", "testcontainers")


def log(msg):
    print(f"[sched] {msg}", file=sys.stderr, flush=True)


def human_s(seconds):
    seconds = int(round(seconds or 0))
    if seconds < 60:
        return f"{seconds}s"
    m, s = divmod(seconds, 60)
    if m < 60:
        return f"{m}m{s:02d}s" if s else f"{m}m"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m"


def pl_gb(value):
    return f"{value:.1f}".replace(".", ",") + " GB"


def load_config():
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_PATH) as f:
            cfg.update(json.load(f))
    except (OSError, ValueError):
        pass
    return cfg


# ---------- pamięć i procesy (ctypes, bez importu perf.py: hook musi być szybki) ----------

_kernel = types.SimpleNamespace(libc=None)


def kernel():
    """ctypes, libc i struktury jądra, ładowane przy pierwszym odczycie pamięci albo procesu."""
    if _kernel.libc is not None:
        return _kernel
    import ctypes
    import ctypes.util

    class RusageV0(ctypes.Structure):
        _fields_ = [("uuid", ctypes.c_uint8 * 16)] + [
            (n, ctypes.c_uint64)
            for n in (
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
            )
        ]

    class Timebase(ctypes.Structure):
        _fields_ = [("numer", ctypes.c_uint32), ("denom", ctypes.c_uint32)]

    class XswUsage(ctypes.Structure):
        _fields_ = [
            ("total", ctypes.c_uint64),
            ("avail", ctypes.c_uint64),
            ("used", ctypes.c_uint64),
            ("pagesize", ctypes.c_uint32),
            ("encrypted", ctypes.c_bool),
        ]

    libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    tb = Timebase()
    libc.mach_timebase_info(ctypes.byref(tb))
    _kernel.ctypes = ctypes
    _kernel.RusageV0 = RusageV0
    _kernel.XswUsage = XswUsage
    _kernel.tick_s = (tb.numer / tb.denom if tb.denom else 1.0) / 1e9
    _kernel.libc = libc
    return _kernel


PROC_PGRP_ONLY = 2
PROC_PPID_ONLY = 6


def sysctl_int(name):
    k = kernel()
    value = k.ctypes.c_uint64(0)
    size = k.ctypes.c_size_t(8)
    if k.libc.sysctlbyname(
        name.encode(), k.ctypes.byref(value), k.ctypes.byref(size), None, 0
    ):
        return None
    return value.value & ((1 << (8 * size.value)) - 1)


def swap_used_gb():
    k = kernel()
    info = k.XswUsage()
    size = k.ctypes.c_size_t(k.ctypes.sizeof(info))
    if k.libc.sysctlbyname(
        b"vm.swapusage", k.ctypes.byref(info), k.ctypes.byref(size), None, 0
    ):
        return 0.0
    return info.used / GB


def proc_usage(pid):
    """(phys_footprint w bajtach, CPU w s) procesu albo None."""
    k = kernel()
    info = k.RusageV0()
    if k.libc.proc_pid_rusage(pid, 0, k.ctypes.byref(info)) != 0:
        return None
    return info.phys_footprint, (info.user_time + info.system_time) * k.tick_s


def _listpids(kind, arg):
    k = kernel()
    buf = (k.ctypes.c_int * 2048)()
    n = k.libc.proc_listpids(kind, arg, buf, k.ctypes.sizeof(buf))
    if n <= 0:
        return []
    return [p for p in buf[: n // k.ctypes.sizeof(k.ctypes.c_int)] if p > 0]


def job_pids(root):
    """Drzewo procesów pod root i cała jego grupa (dzieci odczepione od drzewa zostają w grupie)."""
    seen, todo = set(), [root]
    while todo:
        pid = todo.pop()
        if pid in seen:
            continue
        seen.add(pid)
        todo.extend(_listpids(PROC_PPID_ONLY, pid))
    seen.update(_listpids(PROC_PGRP_ONLY, root))
    return seen


def pids_usage(pids):
    """(GB, CPU s) tych procesów, które jeszcze żyją."""
    footprint, cpu = 0, 0.0
    for pid in pids:
        u = proc_usage(pid)
        if u:
            footprint += u[0]
            cpu += u[1]
    return footprint / GB, cpu


def job_usage(root):
    """(GB, CPU s) wszystkich żywych procesów joba."""
    return pids_usage(job_pids(root))


PROC_ALL_PIDS = 1
PROC_PIDTBSDINFO = 3
BSDINFO_SIZE = 136  # struct proc_bsdinfo
BSD_PPID, BSD_START = 16, 120  # pbi_ppid, pbi_start_tvsec


def all_pids():
    k = kernel()
    need = k.libc.proc_listpids(PROC_ALL_PIDS, 0, None, 0)
    if need <= 0:
        return []
    buf = (k.ctypes.c_int * (need // 4 + 256))()
    n = k.libc.proc_listpids(PROC_ALL_PIDS, 0, buf, k.ctypes.sizeof(buf))
    return [p for p in buf[: max(n, 0) // 4] if p > 0]


def proc_name(pid):
    """Nazwa procesu (p_comm, do 32 znaków), bez argumentów: jedno wywołanie jądra."""
    k = kernel()
    buf = k.ctypes.create_string_buffer(64)
    n = k.libc.proc_name(pid, buf, 64)
    return buf.raw[:n].decode(errors="replace") if n > 0 else ""


def proc_path(pid):
    k = kernel()
    buf = k.ctypes.create_string_buffer(4096)
    n = k.libc.proc_pidpath(pid, buf, 4096)
    return buf.raw[:n].decode(errors="replace") if n > 0 else ""


def proc_bsd(pid, offset, size=4):
    """Pole struct proc_bsdinfo procesu (ppid, start) albo None."""
    k = kernel()
    buf = k.ctypes.create_string_buffer(BSDINFO_SIZE)
    got = k.libc.proc_pidinfo(pid, PROC_PIDTBSDINFO, k.ctypes.c_uint64(0), buf, BSDINFO_SIZE)
    if got != BSDINFO_SIZE:
        return None
    return int.from_bytes(buf.raw[offset : offset + size], "little")


KERN_PROCARGS2 = 49


def proc_args(pid):
    """argv procesu z KERN_PROCARGS2 albo None (cudzy proces, zombie)."""
    k = kernel()
    mib = (k.ctypes.c_int * 3)(1, KERN_PROCARGS2, pid)
    size = k.ctypes.c_size_t(0)
    if k.libc.sysctl(mib, 3, None, k.ctypes.byref(size), None, 0) or not size.value:
        return None
    buf = k.ctypes.create_string_buffer(size.value)
    if k.libc.sysctl(mib, 3, buf, k.ctypes.byref(size), None, 0):
        return None
    raw = buf.raw[: size.value]
    if len(raw) < 4:
        return None
    argc = int.from_bytes(raw[:4], "little")
    _exe, _, rest = raw[4:].partition(b"\0")
    return [a.decode(errors="replace") for a in rest.lstrip(b"\0").split(b"\0")[:argc]]


def descendants(root):
    seen, todo = set(), [root]
    while todo:
        pid = todo.pop()
        if pid not in seen:
            seen.add(pid)
            todo.extend(_listpids(PROC_PPID_ONLY, pid))
    return seen


# Pod tymi procesami biegnie natywny build: xcodebuild z wiersza poleceń i swift-build; usługa
# buildów Xcode (stara i nowa nazwa) tylko wtedy, gdy ma dzieci: przy otwartym Xcode żyje bez
# przerwy, a build to dopiero jej kompilatory. Po nazwie, a nie po ścieżce do Xcode: clang i ld,
# które linkują testy Go z cgo, nie są natywnym buildem. xcodebuild tylko z akcją, która
# kompiluje: maestro trzyma na symulatorze `xcodebuild test-without-building` przez całą sesję.
NATIVE_DRIVERS = ("xcodebuild", "swift-build")
NATIVE_SERVICES = ("XCBBuildService", "SWBBuildService")
# usługa buildów jest buildem dopiero z jednym z nich pod sobą: bezczynna potrafi trzymać pomocników
NATIVE_COMPILERS = (
    "swift-frontend", "swiftc", "swift-driver", "clang", "clang++", "ld", "libtool",
    "ibtool", "actool", "dsymutil", "codesign",
)  # fmt: skip
NATIVE_QUIET_S = 30  # tyle sekund bez kompilatorów po buildzie: faza buildu skończona


def xcode_compiles(argv):
    """Czy ten xcodebuild kompiluje: klasyfikacja jak w hooku, bez akcji, które nic nie budują."""
    raw = parse_native(argv or ["xcodebuild"], "/")
    return bool(raw) and raw["detail"] not in ("test-without-building", "installsrc")


def native_scan(sims_since=None, builds=True):
    """Natywne buildy na tym Macu: {gb, active, pids} z phys_footprint ich drzew procesów.

    `portivo-mobile up` buduje w procesie odczepionym od sesji (własna sesja, rodzic launchd), a
    symulator włącza CoreSimulatorService: żadne z nich nie leży w drzewie joba. Z sims_since
    liczą się też symulatory (launchd_sim z drzewem) włączone od tej chwili, a z builds=False
    tylko one (job po swojej fazie buildu nie bierze cudzych). Kilka ms: nazwy procesów bez
    argumentów. SCHED_FAKE_MEMORY z kluczem `native` w testach."""
    fake = fake_memory()
    if fake is not None and "native" in fake:
        n = fake.get("native") or {}
        return {"gb": float(n.get("gb", 0)), "active": bool(n.get("active")), "pids": set()}
    roots, services, idle, sims = [], [], set(), []
    for pid in all_pids():
        name = proc_name(pid)
        if not builds and name != "launchd_sim":
            continue
        if name == "xcodebuild":
            (roots.append if xcode_compiles(proc_args(pid)) else idle.add)(pid)
        elif name in NATIVE_DRIVERS:
            roots.append(pid)
        elif name in NATIVE_SERVICES and _listpids(PROC_PPID_ONLY, pid):
            services.append(pid)
        elif sims_since is not None and name == "launchd_sim":
            start = proc_bsd(pid, BSD_START, 8)
            if start and start >= sims_since - 2:
                sims.append(pid)
    # usługa buildów pod xcodebuild, który nic nie kompiluje, też nie jest buildem
    roots += [
        p
        for p in services
        if proc_bsd(p, BSD_PPID) not in idle
        and any(proc_name(c) in NATIVE_COMPILERS for c in descendants(p) - {p})
    ]
    tree = set()
    for root in roots + sims:
        tree |= descendants(root)
    return {"gb": pids_usage(tree)[0], "active": bool(roots), "pids": tree}


def alive(pid):
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def fake_memory():
    """Pamięć z pliku SCHED_FAKE_MEMORY (testy) albo None."""
    path = os.environ.get("SCHED_FAKE_MEMORY")
    if not path:
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def probe_memory():
    """Pamięć systemu: level jądra, RAM, swap, presja. SCHED_FAKE_MEMORY (plik JSON) w testach."""
    data = fake_memory()
    if data is not None:
        out = {
            "level": float(data.get("level", 60)),
            "ram_gb": float(data.get("ram_gb", 48)),
            "swap_gb": float(data.get("swap_gb", 0)),
            "pressure": data.get("pressure", "normal"),
        }
        if "native" in data:
            out["native"] = data["native"] or {}
        return out
    ram = (sysctl_int("hw.memsize") or 16 * GB) / GB
    level = sysctl_int("kern.memorystatus_level")
    kernel = sysctl_int("kern.memorystatus_vm_pressure_level") or 1
    pressure = "critical" if kernel >= 4 else "warn" if kernel >= 2 else "normal"
    return {
        "level": float(level if level is not None else 50),
        "ram_gb": ram,
        "swap_gb": swap_used_gb(),
        "pressure": pressure,
    }


def devguard_snapshot():
    """Ostatni pomiar strażnika dev serwerów (devguard-state.json) albo {}."""
    try:
        with open(DEVGUARD_STATE) as f:
            snap = json.load(f).get("snapshot") or {}
        return snap if isinstance(snap, dict) else {}
    except (OSError, ValueError, AttributeError):
        return {}


def guard_level(snap):
    """Presja wg strażnika (0, 1, 2) z jego świeżego pomiaru, albo None. Strażnik liczy ją także ze
    swapu, który rośnie: poziom jądra potrafi mówić „normal” przy pełnym swapie (README, Dev
    server guard), a tylko ten widzi scheduler sam."""
    try:
        if time.time() - float(snap.get("at", 0)) < GUARD_FRESH_S:
            return int((snap.get("pressure") or {}).get("level"))
    except (TypeError, ValueError, AttributeError):
        pass
    return None


def devserver_reserve_gb(snap=None):
    """Miejsce na dev serwery: na jeszcze jeden (min(max_server_gb, budżet devguarda - zajęte)),
    a co najmniej na powrót serwerów, których strażnik nie zatrzyma (chronione i przypięte), do
    ich zmierzonego szczytu (lifetime_max_phys_footprint). Chroniony stos ponad budżetem dawał
    rezerwę 0, choć rośnie dalej."""
    max_server = 4.0
    try:
        with open(DEVGUARD_CONFIG) as f:
            max_server = float(json.load(f).get("max_server_gb", max_server))
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    snap = devguard_snapshot() if snap is None else snap
    try:
        if time.time() - float(snap.get("at", 0)) < 120:
            room = (float(snap.get("budget", 0)) - float(snap.get("total", 0))) / GB
            # `regrow` strażnik liczy po procesach (lifetime_max - teraz): szczyt całej jednostki
            # to tylko max(największy proces, suma teraz), więc dla stosu nic by nie mówił
            regrow = sum(
                min(max(0.0, float(u.get("regrow") or 0)) / GB, max_server)
                for u in snap.get("units") or []
                if u.get("protected")
            )
            return round(max(0.0, min(max_server, room), regrow), 2)
    except (ValueError, TypeError, AttributeError):
        pass
    return max_server


def simulators_info(snap):
    """Symulatory ze świeżego pomiaru strażnika: ile włączonych, ile w użyciu (dzierżawa żywej
    sesji, ktoś patrzy, symulator człowieka) i limit; None bez pomiaru (wtedy bez limitu)."""
    try:
        if time.time() - float(snap.get("at", 0)) >= 120 or "simulators" not in snap:
            return None
        sims = [s for s in snap.get("simulators") or [] if isinstance(s, dict)]
        used = [s for s in sims if s.get("in_use")]
        # limit agentów liczy tylko pulę bez chronionych: Twoich ani chronionych (Portivo-Perf-*)
        # strażnik nie wyłączy, więc dwa takie zatrzymałyby każdy `portivo-mobile up` na zawsze
        agents = [s for s in used if s.get("pool") and not s.get("protected")]
        return {
            "booted": len(sims),
            "in_use": len(used),
            "agents_in_use": len(agents),
            "cap": int(snap.get("simulator_cap") or 0),
            "gb": round(sum(float(s.get("footprint") or 0) for s in sims) / GB, 2),
            "holders": [s.get("name") or s.get("udid") for s in agents][:4],
        }
    except (ValueError, TypeError, AttributeError):
        return None


def guard_view(snap):
    """(stopień hamulca, długo żyjące w GB, rodziny) z pomiaru strażnika (devguard_snapshot);
    (0, None, []) bez niego. Stopień: 0 spokój, 1 ciasno, 2 hamulec, 3 awaria (lastresort.py),
    tylko z pomiaru młodszego niż 30 s: strażnik przy hamulcu mierzy co 2 s."""
    if not snap:
        return 0, None, []
    try:
        age = time.time() - float(snap.get("at", 0))
        if age >= 120:
            return 0, None, []
        stage = int((snap.get("pressure") or {}).get("stage") or 0) if age < 30 else 0
        inv = snap.get("inventory") or {}
        fams = [
            {"family": name, "gb": round(f["footprint"] / GB, 2), "count": f["count"]}
            for name, f in sorted((inv.get("families") or {}).items(), key=lambda kv: -kv[1]["footprint"])
            if name in LONG_LIVED_FAMILIES
        ]
        long_lived = inv.get("long_lived")
        return stage, (round(long_lived / GB, 2) if long_lived is not None else None), fams
    except (ValueError, TypeError, AttributeError, KeyError):
        return 0, None, []


# rodziny procesów, które strażnik liczy jako długo żyjące (devguard_core.LONG_LIVED)
LONG_LIVED_FAMILIES = ("dev", "metro", "watchers", "simulators", "headless", "lsp", "docker")
STAGE_TEXT = ("normal", "tight", "brake", "emergency")


# ---------- klasyfikacja komendy ----------

WRAPPERS = {"rtk", "time", "nice", "env", "caffeinate", "command", "exec", "nohup"}
GO_VERBS = {"build", "test", "vet", "run", "install", "generate"}
# "acc.py' sched": ta sama komenda, gdy katalog domowy ma spację, a ścieżka idzie w cudzysłowie
SKIP_MARKERS = (
    "sched.py",
    "acc.py sched",
    "acc.py' sched",
    "plock.py",
    "depot-exec.sh",
    "depot-ci.sh",
    "SCHED_OFF=1",
    "claude-acc sched",
    "claude-acc' sched",
)
REDIRECTS = re.compile(r"(?:(?<=\s)|^)(?:\d*>&\d+|&>>?\s*\S+|\d*>>?\s*\S+|\d*<\s*\S+)")
TAKES_VALUE = {
    "-run", "-p", "-count", "-tags", "-timeout", "-o", "-ldflags", "-gcflags", "-skip",
    "-bench", "-cpu", "-parallel", "-coverprofile", "-covermode", "-coverpkg", "-exec",
    "-mod", "-modfile", "-overlay", "-pgo", "-shuffle", "-outputdir", "-benchtime", "-asmflags",
}  # fmt: skip
TAIL_PROGRAMS = ("tail", "head", "grep", "rg", "tee", "true", "cat", "sed", "awk", "wc")


def split_segments(command):
    """Komenda powłoki na człony rozdzielone ;, &&, ||, |; przekierowania wycięte, cudzysłowy zachowane."""
    text = REDIRECTS.sub(" ", command)
    lex = shlex.shlex(text, posix=True, punctuation_chars=";&|")
    lex.whitespace_split = True
    segments, cur = [], []
    try:
        for tok in lex:
            if tok and set(tok) <= set(";&|"):
                segments.append((cur, tok))
                cur = []
            else:
                cur.append(tok)
    except ValueError:
        return None
    segments.append((cur, ""))
    return segments


def strip_prefix(words):
    """Zdejmuje przypisania VAR=wartość i opakowania (rtk proxy, time, env, nice)."""
    env = {}
    i = 0
    while i < len(words):
        w = words[i]
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", w):
            k, _, v = w.partition("=")
            env[k] = v
            i += 1
            continue
        base = os.path.basename(w)
        if base in WRAPPERS:
            i += 1
            if base == "rtk" and i < len(words) and words[i] == "proxy":
                i += 1
            while (
                base in ("nice", "caffeinate", "env")
                and i < len(words)
                and words[i].startswith("-")
            ):
                i += 1
            continue
        break
    return env, words[i:]


def find_up(path, marker, stop_at_git=False):
    d = os.path.abspath(path)
    while True:
        if os.path.exists(os.path.join(d, marker)):
            return d
        if stop_at_git and os.path.exists(os.path.join(d, ".git")):
            return None
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def find_module(path):
    return find_up(path, "go.mod", stop_at_git=True) if os.path.isdir(path) else None


def find_repo(path):
    return find_up(path, ".git")


def parse_go(argv):
    """argv od 'go': (verb, flags, packages, katalog z -C) albo None."""
    i = 1
    cdir = None
    while i < len(argv) and argv[i].startswith("-"):
        if argv[i] == "-C" and i + 1 < len(argv):
            cdir = argv[i + 1]
            i += 2
            continue
        if argv[i].startswith("-C="):
            cdir = argv[i][3:]
        i += 1
    if i >= len(argv) or argv[i] not in GO_VERBS:
        return None
    verb = argv[i]
    rest = argv[i + 1 :]
    flags, pkgs = {}, []
    j = 0
    while j < len(rest):
        a = rest[j]
        if a == "--":
            break
        if a.startswith("-"):
            name, eq, val = a.partition("=")
            name = "-" + name.lstrip("-")
            if not eq and name in TAKES_VALUE and j + 1 < len(rest):
                val = rest[j + 1]
                j += 1
            flags[name] = val if (eq or name in TAKES_VALUE) else True
        else:
            pkgs.append(a)
            if verb == "run":
                break
        j += 1
    return verb, flags, pkgs, cdir


def pattern_rel(pattern, module_dir, here):
    """Ścieżka wzorca względem modułu, bez końcowego /... ("." = cały moduł); None, gdy wzorzec
    ma ... w środku albo wychodzi poza moduł."""
    if pattern.endswith("/..."):
        base = pattern[:-4]
    elif "..." in pattern:
        return None
    else:
        base = pattern
    if base.startswith((".", "/")):
        rel = os.path.relpath(os.path.normpath(os.path.join(here, base)), module_dir)
    else:
        mod = module_path(module_dir)
        if not mod or not (base == mod or base.startswith(mod + "/")):
            return None
        rel = base[len(mod) + 1 :] or "."
    return None if rel == ".." or rel.startswith("../") else rel


def scope_of(pkgs, module_dir, here):
    """(zakres, szczegół). tree: cały moduł (`./...` w jego katalogu); subtree: wzorce z ... na
    części modułu, szczegół to ich ścieżki względem modułu (wzorzec nierozwiązany zostaje jak
    jest); pkgs: kilka pakietów; pkg albo handlers: jeden. Bez chodzenia po katalogach, bo
    klasyfikacja idzie też w hooku."""
    if not pkgs:
        pkgs = ["."]
    if any("..." in p for p in pkgs):
        rels = [pattern_rel(p, module_dir, here) for p in pkgs]
        if "." in rels:
            return "tree", "./..."
        return "subtree", " ".join(r if r is not None else p for r, p in zip(rels, pkgs))
    if len(pkgs) > 1:
        return "pkgs", " ".join(pkgs)
    p = pkgs[0]
    if p.startswith((".", "/")):
        rel = os.path.relpath(os.path.normpath(os.path.join(here, p)), module_dir)
    else:
        rel = p.split("/", 1)[1] if "/" in p else p  # ścieżka importu: bez nazwy modułu
    rel = rel.strip("/")
    rel = "." if rel in ("", ".") else rel
    return ("handlers" if rel == HANDLERS else "pkg"), rel


# ---------- JS: testy, buildy, typecheck ----------

# narzędzie -> rodzaj joba (turbo: z nazwy zadania)
NODE_TOOLS = {
    "vitest": "test", "jest": "test", "playwright": "e2e", "next": "build", "tsc": "typecheck",
    "vue-tsc": "typecheck", "eslint": "lint", "turbo": None, "vite": "build",
}  # fmt: skip
NODE_VERBS = {"playwright": ("test",), "next": ("build", "lint"), "vite": ("build",)}
NODE_SCRIPTS = (
    (re.compile(r"^(e2e|integration|playwright|test[:_-](e2e|integration|playwright))([:_-].*)?$"), "e2e"),
    (re.compile(r"^(test|tests|unit|t|tst)([:_-].*)?$"), "test"),
    (re.compile(r"^(typecheck|type-check|check-types|types|tsc)([:_-].*)?$"), "typecheck"),
    (re.compile(r"^build([:_-].*)?$"), "build"),
    (re.compile(r"^lint([:_-].*)?$"), "lint"),
    (re.compile(r"^(check|verify|validate)([:_-].*)?$"), "check"),
)  # fmt: skip
# skrypty i flagi, które się nie kończą albo czekają na człowieka: nigdy w kolejce
NODE_NEVER = re.compile(r"watch|dev|serve|start|preview|storybook|(^|[:_-])ui($|[:_-])")
NODE_STOP_FLAGS = {"--watch", "--watchAll", "--ui", "--debug", "--version", "-v", "--help", "-h", "--init", "--print-config"}
NODE_WATCH_SHORT = {"tsc": "-w", "vue-tsc": "-w", "vitest": "-w"}
NODE_FILTER_FLAGS = {"-t", "--testNamePattern", "-g", "--grep", "--project"}
NODE_PRIORS = {
    "test": (3.0, 90), "e2e": (3.5, 180), "build": (4.0, 180),
    "typecheck": (2.5, 60), "lint": (2.0, 60), "check": (3.0, 120),
}  # fmt: skip
TURBO_COMMANDS = {"prune", "login", "logout", "link", "unlink", "gen", "generate", "daemon", "ls", "info", "query", "telemetry", "bin", "scan", "watch"}
# wyjątki dla hooka rtk ([hooks] exclude_commands): wszystko, co scheduler owija, i nic więcej
# (test pilnuje obu stron); bez ^ rtk dopasowuje słowo, z ^ to wyrażenie na członie komendy
RTK_EXCLUDES = (
    "go",
    "make",
    "golangci-lint",
    "govulncheck",
    r"^(npx( -y| --yes)? |bunx |(pnpm|yarn|bun) (exec |dlx |x )?|npm exec (-- )?|\S*node_modules/\.bin/)?(vitest|jest|playwright|next|tsc|vue-tsc|eslint|turbo|vite)(\s|$)",
    r"^(pnpm|yarn|npm|bun)( (-r|--recursive|-ws|--workspaces|-s|--silent|--if-present|--\S+=\S+|(-C|--dir|--prefix|--cwd|-F|--filter|--workspace|-w) \S+|-w))* ((run|run-script) )?(t|tst|test|tests|unit|e2e|integration|playwright|build|lint|typecheck|type-check|check-types|types|tsc|check|verify|validate)([:_-]\S*)?(\s|$)",
    # natywne: rtk ma filtry xcodebuild i gradle, a regex rtk (crate regex) nie zna lookaroundów,
    # więc xcodebuild idzie w wyjątki cały, także `-version` (krótkie wyjście, nic nie traci)
    r"^(xcrun )?xcodebuild(\s|$)",
    r"^((npx|bunx)( -y| --yes)? |(pnpm|yarn|bun|npm)( (-C|--dir|--prefix|--cwd|--filter|-F) \S+)*( (exec|dlx|x)( --)?)? |\S*node_modules/\.bin/)?(expo(@\S+)? (run:\S+|prebuild)|react-native (run|build)-(ios|android)|pod-install)(\s|$)",
    r"^((npx|bunx)( -y| --yes)? |(pnpm|yarn|bun|npm) ((exec|dlx|x)( --)? )?)?eas(-cli)? build( \S+)* --local(\s|$)",
    r"^(arch -\S+ )?(bundle exec )?pod (install|update)(\s|$)",
    r"^(\S*/)?gradlew? (\S+ )*(\S*:)?(assemble\S*|bundle\S*|install\S*|build)(\s|$)",
    r"^xcrun simctl (boot|bootstatus( \S+)* -b)(\s|$)",
    r'^open( \S+)* (-a "?Simulator(\.app)?"?|\S*/Simulator\.app)(\s|$)',
    r"^portivo-mobile up(\s|$)",
    # reszta ciężkiej pracy (GENERIC_TOOLS); rtk porównuje je z komendą po swojej normalizacji
    # (`python -m pytest` i `uv run pytest` to dla niego `pytest`), więc test sprawdza je samym rtk
    r"^cargo (build|b|test|t|check|c|clippy|run|r|bench|doc|nextest|install|llvm-cov|tarpaulin|miri)(\s|$)",
    r"^swift (build|test|run)(\s|$)",
    r"^(pytest|py\.test|mypy)(\s|$)",
    r"^docker (buildx build|compose build|image build|build)(\s|$)",
    r"^(just|task)(\s|$)",
    r"^deno (test|task|run|compile|check|bench)(\s|$)",
    r"^(\S*/)?mvnw?(\s|$)",
    r"^nx (run|run-many|affected|build|test|lint|e2e)(\s|$)",
    r"^bun (\S+\.[cm]?[jt]sx?|build)(\s|$)",
    r"^uv run(\s|$)",
    r"^(npx( -y| --yes)? |bunx |(pnpm|yarn|bun) (exec |dlx |x )|npm exec (-- )?)(lighthouse|unlighthouse|cypress|mocha|ava|webpack|rollup|parcel|tsup|astro|nuxt|nuxi|svelte-check|storybook|nx|lerna|cargo|pytest|mypy|pyright|tox|nox)(\s|$)",
    r"^(pnpm|yarn|npm|bun)( (-r|--recursive|-ws|--workspaces|-s|--silent|--if-present|--\S+=\S+|(-C|--dir|--prefix|--cwd|-F|--filter|--workspace|-w) \S+|-w))* (run|run-script) \S",
)


def script_kind(name):
    if not name or NODE_NEVER.search(name):
        return None
    return next((kind for rx, kind in NODE_SCRIPTS if rx.match(name)), None)


def node_filtered(args, verb_words=()):
    """Czy bieg testów jest zawężony (pliki, -t, --grep): wtedy lżejszy niż cały pakiet."""
    positional = [a for a in args if not a.startswith("-") and a not in verb_words]
    return bool(positional) or any(a.split("=", 1)[0] in NODE_FILTER_FLAGS for a in args)


def node_tool(words, base):
    """Wywołanie narzędzia JS (vitest, jest, playwright test...): surowy job albo None."""
    if not words:
        return None
    tool, args = os.path.basename(words[0]), words[1:]
    if tool not in NODE_TOOLS or any(a.split("=", 1)[0] in NODE_STOP_FLAGS for a in args):
        return None
    if NODE_WATCH_SHORT.get(tool) in args:
        return None
    first = next((a for a in args if not a.startswith("-")), None)
    if tool == "turbo":
        tasks = [a for a in args if not a.startswith("-") and a != "run"]
        if not tasks or tasks[0] in TURBO_COMMANDS:
            return None
        kind = script_kind(tasks[0])
        return kind and dict(base, kind=kind, tool="turbo", all=True, filtered=False)
    if tool == "vitest" and first in ("dev", "watch", "list", "init"):
        return None
    if tool in NODE_VERBS and first not in NODE_VERBS[tool]:
        return None
    kind = "lint" if (tool == "next" and first == "lint") else NODE_TOOLS[tool]
    filtered = kind in ("test", "e2e") and node_filtered(args, ("run", "related", "bench", "test"))
    return dict(base, kind=kind, tool=tool, filtered=filtered)


def parse_node(words, here):
    """Człon komendy z pracą JS: surowy job {kind, tool, dir, filtered, all, filter} albo None."""
    prog = os.path.basename(words[0])
    base = {"dir": here, "filtered": False, "all": False, "filter": None, "lang": "node"}
    if prog in ("npx", "bunx"):
        rest = words[1:]
        while rest and rest[0].startswith("-"):
            rest = rest[2:] if rest[0] in ("-p", "--package") else rest[1:]
        return node_tool(rest, base)
    if prog not in ("pnpm", "yarn", "npm", "bun"):
        return node_tool(words, base) if (prog in NODE_TOOLS or "node_modules/.bin/" in words[0]) else None
    i = 1
    while i < len(words) and words[i].startswith("-"):
        opt, _, val = words[i].partition("=")
        valued = opt in ("-C", "--dir", "--prefix", "--cwd", "--filter", "-F", "--workspace") or (
            opt == "-w" and prog == "npm"
        )
        if valued and not val and i + 1 < len(words):
            val = words[i + 1]
            i += 1
        if opt in ("-C", "--dir", "--prefix", "--cwd") and val:
            base["dir"] = os.path.normpath(os.path.join(here, os.path.expanduser(val)))
        elif opt in ("--filter", "-F", "--workspace", "-w") and val:
            base["filter"] = val
        elif opt in ("-r", "--recursive", "--workspaces", "-ws"):
            base["all"] = True
        i += 1
    rest = words[i:]
    if not rest:
        return None
    cmd, args = rest[0], rest[1:]
    if cmd in ("exec", "dlx", "x") or (prog == "npm" and cmd == "exec"):
        return node_tool([a for a in args if a != "--"], base)
    if prog == "bun" and cmd == "test":
        if any(a.split("=", 1)[0] in NODE_STOP_FLAGS for a in args):
            return None
        return dict(base, kind="test", tool="bun", filtered=node_filtered(args))
    if cmd in ("run", "run-script"):
        script, args = (args[0], args[1:]) if args else (None, [])
    elif prog == "npm":
        if cmd not in ("test", "t", "tst"):
            return None
        script = "test"
    elif cmd in NODE_TOOLS:
        return node_tool(rest, base)
    else:
        script = cmd
    kind = script_kind(script)
    if not kind or any(a.split("=", 1)[0] in NODE_STOP_FLAGS for a in args):
        return None
    filtered = kind in ("test", "e2e") and node_filtered([a for a in args if a != "--"])
    return dict(base, kind=kind, tool=script, filtered=filtered)


def project_name(repo):
    """Nazwa projektu dla klasy joba: worktree (`.git` to plik `gitdir: <repo>/.git/worktrees/x`)
    dostaje nazwę głównego repo, więc wszystkie drzewa uczą się z jednej historii. Z nazwą
    katalogu worktree każdy nowy zaczynał od priora (lint 2 GB zamiast zmierzonych 4)."""
    try:
        with open(os.path.join(repo, ".git")) as f:
            line = f.readline().strip()
    except OSError:  # katalog .git: zwykłe repo
        return os.path.basename(repo)
    gitdir = line[len("gitdir:"):].strip() if line.startswith("gitdir:") else ""
    parts = os.path.normpath(gitdir).split(os.sep)
    if len(parts) >= 3 and parts[-3:-1] == [".git", "worktrees"]:
        return os.path.basename(os.sep.join(parts[:-3])) or os.path.basename(repo)
    return os.path.basename(repo)


def finish_node(raw):
    pkg_dir = find_up(raw["dir"], "package.json", stop_at_git=True) if os.path.isdir(raw["dir"]) else None
    if not pkg_dir:
        return None
    repo = find_repo(pkg_dir) or pkg_dir
    rel = os.path.relpath(pkg_dir, repo)
    detail = f"filter={raw['filter']}" if raw["filter"] else rel
    cls = f"{project_name(repo)}:{raw['kind']}:{detail}:{raw['tool']}"
    cls += ":all" if raw["all"] else ""
    cls += ":filtered" if raw["filtered"] else ""
    label = " ".join(
        shlex.quote(a) if re.search(r"[\s'\"$^*|&;]", a) else a for a in raw["argv"]
    )
    return {
        "lang": "node",
        "kind": raw["kind"],
        "tool": raw["tool"],
        "class": cls,
        "module_name": os.path.basename(pkg_dir),
        "module": rel,
        "module_dir": pkg_dir,
        "repo_dir": repo,
        "repo": os.path.basename(repo),
        "scope": "node",
        "scope_detail": detail,
        "compile": False,
        "filtered": raw["filtered"],
        "all": raw["all"],
        "race": False,
        "p_explicit": None,
        "count1": False,
        "flags": {},
        "pkgs": [],
        "argv": raw["argv"],
        "go_dir": pkg_dir,
        "label": label,
    }


def node_prior(job):
    gb, s = NODE_PRIORS.get(job["kind"], (3.0, 120))
    if job.get("all"):
        gb, s = gb * 1.5, s * 2
    if job.get("filtered"):
        gb, s = max(1.0, gb * 0.5), s * 0.4
    return round(gb, 2), round(s)


# ---------- natywne buildy i symulatory ----------

# narzędzie -> (rodzaj, GB, s, pamięć poza drzewem procesów). Liczby to wartości na pierwsze biegi
# klasy, potem p90 z historii jak w Go. Build aplikacji Expo xcodebuildem z pełną równoległością
# to 6-12 GB (2026-10-08: Release build iOS i build klienta deweloperskiego, oba wpuszczone przy
# rosnącym swapie, Mac zamarzł o 18:57); uruchomiony symulator z aplikacją to ~0,5 GB w procesach,
# które widać w raportach JetsamEvent tego Maca, a footprint całego symulatora z aplikacją RN
# bierzemy z zapasem. „Poza drzewem”: pamięć ląduje w procesach, których scheduler nie mierzy
# (symulator pod launchd_sim, demon Gradle, odczepiony build `portivo-mobile`), więc historia może
# prognozę podnieść, ale nie zbić poniżej tej z tabeli: inaczej trzy szybkie starty nauczyłyby ją
# zera, a scheduler wpuszczałby symulatory i buildy na pustą pamięć
NATIVE = {
    "xcodebuild": ("build", 10.0, 600, False),
    "expo-run": ("build", 10.0, 900, True),
    "react-native-run": ("build", 10.0, 900, True),
    "eas-local": ("build", 10.0, 1500, False),
    "gradle": ("build", 6.0, 600, True),
    # skrypt Portivo na tym Macu: dzierżawa i start symulatora, a gdy natywna strona się zmieniła,
    # 10-20 min buildu klienta deweloperskiego w odczepionym procesie
    "portivo-mobile": ("build", 10.0, 900, True),
    "pod": ("pods", 1.5, 180, False),
    "expo-prebuild": ("pods", 2.0, 180, False),
    "simulator": ("simulator", 2.5, 30, True),
}
# xcodebuild bez buildu: informacje, eksport, pobieranie platform
XCODE_INFO = {
    "-version", "-usage", "-help", "-license", "-list", "-showsdks", "-showdestinations",
    "-showTestPlans", "-showBuildSettings", "-showBuildSettingsForIndex", "-find-executable",
    "-find-library", "-checkFirstLaunchStatus", "-runFirstLaunch", "-downloadPlatform",
    "-downloadAllPlatforms", "-importPlatform", "-downloadComponent", "-importComponent",
    "-deleteComponent", "-showComponent", "-exportArchive", "-exportNotarizedApp",
    "-exportLocalizations", "-importLocalizations", "-resolvePackageDependencies",
    "-create-xcframework",
}  # fmt: skip
XCODE_ACTIONS = ("build", "test", "archive", "analyze", "build-for-testing", "test-without-building", "docbuild", "install", "installsrc", "clean")  # fmt: skip
GRADLE_TASK = re.compile(r"^(?::?[\w-]+:)*(?:(?:assemble|bundle|install)\w*|build)$")
HELP_FLAGS = {"--help", "-h", "-help", "-usage", "--version", "-version"}
# programy natywnej pracy; bramka natywnego hooka (devguard.GATE_PROGRAMS) musi znać każdy, a
# xcrun i open przepuszcza tylko przy starcie symulatora (GATE_NATIVE); pilnują tego testy
NATIVE_PROGRAMS = ("xcodebuild", "expo", "react-native", "eas", "eas-cli", "pod", "pod-install", "gradlew", "gradle", "portivo-mobile")  # fmt: skip
PACKAGE_BINS = ("expo", "react-native", "eas", "eas-cli", "pod-install")  # `pnpm expo run:ios`


def parse_native(words, here):
    """Człon komendy z natywnym buildem albo startem symulatora: surowy job albo None."""
    prog, args = os.path.basename(words[0]), words[1:]
    if prog in ("arch", "npx", "bunx") or (prog == "bundle" and args[:1] == ["exec"]):
        # arch -arm64 pod install, bundle exec pod install, npx expo run:ios
        rest = list(args[1:] if prog == "bundle" else args)
        while rest and rest[0].startswith("-"):
            rest = rest[2:] if rest[0] in ("-p", "--package") else rest[1:]
        return parse_native(rest, here) if rest else None
    if prog in ("pnpm", "yarn", "npm", "bun"):
        rest = [a for a in args if a != "--"]
        while rest and rest[0].startswith("-"):
            opt, _, val = rest[0].partition("=")
            if opt in ("-C", "--dir", "--prefix", "--cwd", "--filter", "-F", "--workspace"):
                if not val and len(rest) > 1:
                    val, rest = rest[1], rest[1:]
                if opt in ("-C", "--dir", "--prefix", "--cwd") and val:
                    here = os.path.normpath(os.path.join(here, os.path.expanduser(val)))
            rest = rest[1:]
        if rest[:1] in (["exec"], ["dlx"], ["x"]):
            rest = rest[1:]
        if rest and rest[0].split("@")[0] in PACKAGE_BINS:
            return parse_native(rest, here)
        return None
    if any(a in HELP_FLAGS for a in args):
        return None
    raw = None
    tool = prog.split("@")[0]
    first = next((a for a in args if not a.startswith("-")), None)
    if prog == "xcrun":
        if args[:1] == ["xcodebuild"]:
            return parse_native(args, here)
        if args[:2] == ["simctl", "boot"] or (args[:2] == ["simctl", "bootstatus"] and "-b" in args):
            raw = ("simulator", None)
    elif prog == "xcodebuild":
        actions = [a for a in args if a in XCODE_ACTIONS]
        if not XCODE_INFO.intersection(args) and not (actions and set(actions) == {"clean"}):
            raw = ("xcodebuild", next((a for a in actions if a != "clean"), "build"))
    elif tool == "expo" and first in ("run:ios", "run:android"):
        raw = ("expo-run", first.split(":")[1])
    elif tool == "expo" and first == "prebuild":
        raw = ("expo-prebuild", None)
    elif tool == "react-native" and re.match(r"^(run|build)-(ios|android)$", first or ""):
        raw = ("react-native-run", first.split("-")[1])
    elif tool in ("eas", "eas-cli") and first == "build" and "--local" in args:
        raw = ("eas-local", None)
    elif prog == "pod" and first in ("install", "update"):
        raw = ("pod", None)
    elif prog == "pod-install":
        raw = ("pod", None)
    elif prog in ("gradlew", "gradle") and any(GRADLE_TASK.match(a) for a in args):
        raw = ("gradle", None)
    elif prog == "open" and any(
        a in ("Simulator", "Simulator.app") or a.endswith("/Simulator.app")
        for a in args
    ):
        raw = ("simulator", None)
    elif prog == "portivo-mobile" and first == "up":
        raw = ("portivo-mobile", next((a for a in args[1:] if not a.startswith("-")), None))
    if raw is None:
        return None
    tool, detail = raw
    return {"lang": "native", "tool": tool, "detail": detail, "dir": here}


def native_exclusive(tool, detail, argv):
    if tool == "xcodebuild":
        return detail not in ("test-without-building", "installsrc")
    if tool in ("expo-run", "react-native-run"):
        return detail == "ios"
    if tool == "eas-local":
        return all(a.split("=")[-1] != "android" for a in argv)
    return tool == "portivo-mobile"


def finish_native(raw):
    tool, detail = raw["tool"], raw["detail"]
    kind, gb, wall, outside = NATIVE[tool]
    here = raw["dir"]
    repo = find_repo(here) if os.path.isdir(here) else None
    repo = repo or here
    # symulator to ten sam koszt z każdego repo; build to aplikacja, więc klasa jest per repo
    owner = "mac" if tool == "simulator" else project_name(repo)
    cls = f"{owner}:native:{tool}" + (f":{detail}" if detail else "")
    label = " ".join(
        shlex.quote(a) if re.search(r"[\s'\"$^*|&;]", a) else a for a in raw["argv"]
    )
    rel = os.path.relpath(here, repo)
    return {
        "lang": "native",
        "kind": kind,
        "tool": tool,
        "class": cls,
        "module_name": os.path.basename(repo),
        "module": "." if rel.startswith("..") else rel,
        "module_dir": here,
        "repo_dir": repo,
        "repo": os.path.basename(repo),
        "scope": "native",
        "scope_detail": detail or tool,
        "compile": False,
        "filtered": False,
        "all": False,
        "race": False,
        "p_explicit": None,
        "count1": False,
        "flags": {},
        "pkgs": [],
        "argv": raw["argv"],
        "go_dir": here,
        "label": label,
        "native_prior": (gb, wall),
        "outside": outside,
        # jedno miejsce na natywny build na całym Macu (plan, track_native): buildy iOS, bo tylko
        # ich kompilatory widzi native_scan; pody, symulator i Android (demon Gradle) biegną obok
        "exclusive": native_exclusive(tool, detail, raw["argv"]),
    }


# ---------- reszta ciężkiej pracy: po kształcie komendy, bez znajomości projektu ----------

# Narzędzie -> (rodzaj, podkomendy, które pracują i same się kończą; None: każde wywołanie poza
# flagami informacyjnymi). Wszystko tu jest ciężkie z natury: kompilacja, testy, przeglądarka,
# symulator. Klasa joba to podpis komendy, uczony z historii jak Go i JS.
GENERIC_TOOLS = {
    "cargo": ("build", {"build", "b", "test", "t", "check", "c", "clippy", "run", "r", "bench", "doc", "nextest", "install", "llvm-cov", "tarpaulin", "miri"}),
    "swift": ("build", {"build", "test", "run"}),
    "pytest": ("test", None), "py.test": ("test", None), "tox": ("test", None), "nox": ("test", None),
    "mypy": ("typecheck", None), "pyright": ("typecheck", None),
    "deno": ("test", {"test", "task", "run", "compile", "check", "bench"}),
    "bazel": ("build", {"build", "test", "run", "coverage"}), "bazelisk": ("build", {"build", "test", "run", "coverage"}),
    "mvn": ("build", None), "mvnw": ("build", None),
    "dotnet": ("build", {"build", "test", "run", "publish"}),
    "nx": ("build", {"run", "run-many", "affected", "build", "test", "lint", "e2e"}),
    "lerna": ("build", {"run"}),
    "webpack": ("build", None), "rollup": ("build", None), "parcel": ("build", {"build"}), "tsup": ("build", None),
    "astro": ("build", {"build", "check"}), "nuxt": ("build", {"build", "generate"}), "nuxi": ("build", {"build", "generate"}),
    "svelte-check": ("typecheck", None),
    "cypress": ("e2e", {"run"}), "mocha": ("test", None), "ava": ("test", None),
    "lighthouse": ("e2e", None), "unlighthouse": ("e2e", None),
    # xcodebuild, Gradle, expo i react-native zna parse_native (`expo export` jest tam lekki)
    "storybook": ("build", {"build"}),
    "just": ("script", None), "task": ("script", None), "make": ("script", None),
}  # fmt: skip
# `python -m X`: tylko moduły, które są pracą (testy, typecheck, budowanie paczki)
PYTHON_MODULES = {"pytest": "test", "unittest": "test", "mypy": "typecheck", "pyright": "typecheck",
                  "tox": "test", "nox": "test", "build": "build"}  # fmt: skip
GENERIC_STOP_FLAGS = {"--version", "-V", "--help", "-h", "-version", "-help", "--list", "-l", "--watch",
                      "-w"}  # fmt: skip
# komendy, które się nie kończą albo nic nie liczą: dev serwery, watchery, demony, logi
GENERIC_NEVER = re.compile(
    r"(^|[/:_.-])(dev|serve|server|start|watch|preview|storybook|daemon|tail|logs?|repl|shell|console|emulator)([/:_.-]|$)"
    r"|(serve|server|watch|daemon)$"
)
# podkomendy skryptu, które tylko pytają albo sprzątają (`stack.sh status`, `cli.js help`)
LIGHT_SUBCOMMANDS = re.compile(r"^(status|stop|down|kill|ps|ls|list|help|version|show|whoami|info)$")
# programy po pełnej ścieżce spoza projektu: systemowe, z Homebrew, aplikacje; tę samą listę ma
# bramka hooka (devguard.HOOK_GATE)
SYSTEM_PATHS = ("/usr/", "/bin/", "/sbin/", "/System/", "/opt/homebrew/", "/Library/", "/Applications/")
# treść skryptu z package.json, który stawia serwer albo watcher (`"app": "next dev"`)
SCRIPT_BODY_NEVER = re.compile(r"(^|[\s/:_-])(dev|serve|start|watch|preview|storybook)(\s|$|[:_-])|--watch")
# skrypty menedżera pakietów, które są lekkie z natury: formatowanie, sprzątanie
LIGHT_SCRIPTS = re.compile(r"^(format|fmt|prettier|clean|help|version|prepare|lint-staged)([:_-].*)?$")
PM_BUILTINS = {
    "add", "install", "i", "ci", "update", "up", "upgrade", "remove", "rm", "uninstall", "link", "unlink",
    "import", "rebuild", "prune", "fetch", "patch", "patch-commit", "audit", "list", "ls", "ll", "outdated",
    "why", "licenses", "store", "root", "bin", "env", "setup", "config", "get", "set", "doctor", "server",
    "deploy", "help", "init", "create", "publish", "pack", "login", "logout", "whoami", "version", "info",
    "view", "cache", "workspaces", "plugin", "constraints", "node", "npm", "dedupe", "explain", "search",
    "owner", "dist-tag", "team", "token", "profile", "hook", "completion", "approve-builds", "self-update",
}  # fmt: skip
INTERPRETERS = re.compile(r"^(python(\d(\.\d+)?)?|node|tsx|ts-node|bun|bash|sh|zsh)$")
NODE_VALUE_FLAGS = {"-r", "--require", "--import", "--loader", "--experimental-loader", "--env-file",
                    "--conditions", "-C", "--input-type", "--title"}  # fmt: skip
PYTHON_VALUE_FLAGS = {"-X", "-W", "-Q"}
# przewidywanie do pierwszych biegów klasy (GB, s): ostrożnie, bo pierwszy bieg nieznanej komendy
# może postawić przeglądarkę albo kompilator; potem p90 z historii
GENERIC_PRIORS = {
    "build": (6.0, 300), "test": (4.0, 180), "e2e": (4.0, 240), "typecheck": (3.0, 90),
    "script": (4.0, 300), "run": (3.0, 120),
}  # fmt: skip
# docker: praca dzieje się w maszynie wirtualnej Dockera, nie w drzewie procesów komendy, więc
# historia widzi z niej prawie nic; przewidywanie nie schodzi poniżej tego
DOCKER_FLOOR_GB = 4.0
# zmienna, którą `run` daje swojemu dziecku: `sched run` w środku wpuszczonego joba (skrypt,
# który sam woła `claude-acc sched run`) nie czeka drugi raz na tę samą pamięć
NESTED_ENV = "CLAUDE_ACC_SCHED_JOB"


def generic_tool(words, here):
    """Narzędzie z GENERIC_TOOLS albo docker: surowy job albo None."""
    tool = os.path.basename(words[0])
    args = words[1:]
    if any(a.split("=", 1)[0] in GENERIC_STOP_FLAGS for a in args):
        return None
    positional = [a for a in args if not a.startswith("-")]
    first = positional[0] if positional else None
    if tool == "docker":
        # `docker compose -f c.yml build`: wartość -f też stoi między słowami, więc build gdziekolwiek dalej
        if positional[:1] == ["build"] or (positional[:1] in (["buildx"], ["compose"], ["image"]) and "build" in positional[1:]):
            return {"kind": "build", "tool": "docker", "sig": "docker:build", "dir": here, "floor_gb": DOCKER_FLOOR_GB}
        return None
    if tool not in GENERIC_TOOLS:
        return None
    kind, verbs = GENERIC_TOOLS[tool]
    if verbs is not None:
        if first not in verbs:
            return None
        if tool == "cargo" and first in ("test", "t", "nextest", "bench"):
            kind = "test"
        elif tool in ("swift", "dotnet", "bazel", "bazelisk") and first == "test":
            kind = "test"
        elif tool == "nx" and first in ("test", "e2e", "lint"):
            kind = {"lint": "typecheck"}.get(first, first)
        sig = f"{tool}:{first}"
    elif kind == "script":
        # just/task/make: przepis, uczony z historii; bez celu to cel domyślny
        target = first or "default"
        if GENERIC_NEVER.search(target):
            return None
        sig = f"{tool}:{target}"
    else:
        sig = tool + (f":{first}" if first and re.match(r"^[a-z][\w:-]*$", first) else "")
    if tool in ("webpack", "rollup", "tsup") and (first in ("serve", "watch") or "--watch" in args):
        return None
    return {"kind": kind, "tool": tool, "sig": sig, "dir": here}


def script_sig(path, here):
    """Ścieżka skryptu do podpisu klasy: względem repo, a spoza repo (tmp, scratchpad) sama nazwa,
    bo te ścieżki są inne w każdej sesji."""
    full = os.path.normpath(os.path.join(here, os.path.expanduser(path)))
    repo = find_repo(here) if os.path.isdir(here) else None
    if repo:
        rel = os.path.relpath(full, repo)
        if not rel.startswith(".."):
            return rel
    return os.path.basename(full)


def interpreter_job(words, here):
    """python/node/tsx/bun/bash ze skryptem z pliku: surowy job albo None (-c, -e, REPL, --version)."""
    prog = os.path.basename(words[0])
    tool = re.sub(r"\d.*$", "", prog) if prog.startswith("python") else prog
    args = words[1:]
    i = 0
    while i < len(args) and args[i].startswith("-") and args[i] != "-":
        flag = args[i].split("=", 1)[0]
        if flag in ("-c", "-e", "-p", "--eval", "--print", "--version", "-V", "-v", "--help", "-h", "-i"):
            return None
        if tool == "python" and flag == "-m" and i + 1 < len(args):
            module = args[i + 1]
            kind = PYTHON_MODULES.get(module)
            return kind and {"kind": kind, "tool": module, "sig": module, "dir": here}
        if tool in ("node", "bun") and flag == "--test":
            return {"kind": "test", "tool": tool, "sig": f"{tool}:--test", "dir": here}
        takes = (tool == "python" and flag in PYTHON_VALUE_FLAGS) or (
            tool in ("node", "tsx", "ts-node", "bun") and flag in NODE_VALUE_FLAGS and "=" not in args[i]
        )
        i += 2 if takes else 1
    if i >= len(args) or args[i] == "-":
        return None  # REPL albo skrypt ze stdin (heredoc): zwykle chwila liczenia
    script = args[i]
    if tool in ("bash", "sh", "zsh") and not script.endswith(".sh") and "/" not in script:
        return None
    if tool == "bun" and script in ("run", "x", "build"):
        if script == "build":
            return {"kind": "build", "tool": "bun", "sig": "bun:build", "dir": here}
        return None  # bun run <skrypt> idzie przez parse_node albo pm_script
    if tool == "bun" and not re.search(r"\.[cm]?[jt]sx?$", script):
        return None
    if GENERIC_NEVER.search(os.path.splitext(os.path.basename(script))[0]):
        return None
    if not os.path.isfile(os.path.join(here, os.path.expanduser(script))):
        return None  # nie ma takiego pliku: komenda i tak padnie, nie ma czego wpuszczać
    # pierwsze słowo po skrypcie to zwykle podkomenda (`manage.py test`, `cli.js capture`)
    sub = next((a for a in args[i + 1 : i + 2] if re.match(r"^[a-z][\w:-]*$", a)), None)
    if sub and (GENERIC_NEVER.search(sub) or LIGHT_SUBCOMMANDS.match(sub)):
        return None
    sig = f"{tool}:{script_sig(script, here)}" + (f":{sub}" if sub else "")
    return {"kind": "script", "tool": tool, "sig": sig, "dir": here}


def pm_script(words, here):
    """Skrypt z package.json o nazwie spoza rodzajów JS (`pnpm sm capture`, `npm run e2e:ci`)."""
    prog = os.path.basename(words[0])
    i = 1
    pkg_dir, filtered = here, False
    while i < len(words) and words[i].startswith("-"):
        opt, _, val = words[i].partition("=")
        valued = opt in ("-C", "--dir", "--prefix", "--cwd", "--filter", "-F", "--workspace")
        if valued and not val and i + 1 < len(words):
            val = words[i + 1]
            i += 1
        if opt in ("-C", "--dir", "--prefix", "--cwd") and val:
            pkg_dir = os.path.normpath(os.path.join(here, os.path.expanduser(val)))
        elif opt in ("--filter", "-F", "--workspace", "-r", "--recursive", "-ws", "--workspaces"):
            filtered = True
        i += 1
    rest = words[i:]
    if not rest:
        return None
    if rest[0] in ("run", "run-script"):
        rest = rest[1:]
    elif prog == "npm":
        return None  # npm uruchamia własne skrypty tylko przez `run`
    if not rest or rest[0] in PM_BUILTINS or rest[0].startswith("-"):
        return None
    name, args = rest[0], [a for a in rest[1:] if a != "--"]
    if NODE_NEVER.search(name) or LIGHT_SCRIPTS.match(name) or any(a in NODE_STOP_FLAGS for a in args):
        return None
    if not filtered:
        # bez --filter sprawdzamy, że to naprawdę skrypt; `pnpm foo` bez skryptu to bin z node_modules
        pkg = find_up(pkg_dir, "package.json", stop_at_git=True) if os.path.isdir(pkg_dir) else None
        try:
            with open(os.path.join(pkg, "package.json")) as f:
                scripts = json.load(f).get("scripts") or {}
        except (OSError, ValueError, TypeError, AttributeError):
            return None
        if name not in scripts or SCRIPT_BODY_NEVER.search(str(scripts[name])):
            return None
    sub = next((a for a in args if not a.startswith("-")), None)
    sig = f"{prog}-script:{name}" + (f":{sub}" if sub and re.match(r"^[\w:-]+$", sub) else "")
    return {"kind": "script", "tool": name, "sig": sig, "dir": pkg_dir}


def parse_generic(words, here):
    """Ciężka komenda spoza Go i JS: surowy job {kind, tool, sig, dir} albo None."""
    prog = os.path.basename(words[0])
    if prog in ("npx", "bunx"):
        rest = words[1:]
        while rest and rest[0].startswith("-"):
            rest = rest[2:] if rest[0] in ("-p", "--package") else rest[1:]
        return generic_tool(rest, here) if rest else None
    if prog in ("pnpm", "yarn", "npm", "bun"):
        cmd = next((w for w in words[1:] if not w.startswith("-")), None)
        if cmd in ("exec", "dlx", "x"):
            rest = words[words.index(cmd) + 1 :]
            rest = [w for w in rest if w != "--"]
            return generic_tool(rest, here) if rest else None
        if prog == "bun" and cmd and cmd not in ("run", "x") and cmd not in PM_BUILTINS:
            job = interpreter_job(words, here)
            if job:
                return job
        return pm_script(words, here)
    if prog == "uv" and len(words) > 1 and words[1] == "run":
        rest = words[2:]
        while rest and rest[0].startswith("-"):
            takes = rest[0] in ("--with", "--python", "-p", "--package", "--extra", "--group", "--env-file", "--directory", "--project")
            rest = rest[2:] if takes and "=" not in rest[0] else rest[1:]
        if not rest:
            return None
        inner = parse_generic(rest, here)
        if inner:
            return inner
        if GENERIC_NEVER.search(os.path.basename(rest[0])) or os.path.basename(rest[0]) in ("python", "python3"):
            return None
        return {"kind": "script", "tool": "uv", "sig": f"uv-run:{os.path.basename(rest[0])}", "dir": here}
    if INTERPRETERS.match(prog):
        return interpreter_job(words, here)
    if prog in GENERIC_TOOLS or prog == "docker":
        return generic_tool(words, here)
    path = words[0]
    if prog in ("gradle", "gradlew"):
        return None  # Gradle zna parse_native: buildy aplikacji tak, reszta zadań nie
    if prog in ("claude-acc", "claude", "codex", "orca", "git", "gh", "rtk"):
        return None  # po pełnej ścieżce to dalej te same programy, nie skrypt projektu
    if "/" in path or path.endswith(".sh"):
        # skrypt albo program projektu: ./scripts/e2e.sh, bin/capture, tools/verify
        full = os.path.normpath(os.path.join(here, os.path.expanduser(path)))
        if path.startswith(SYSTEM_PATHS):
            return None  # program systemowy po pełnej ścieżce: znane narzędzia łapie gałąź wyżej
        if not os.path.isfile(full) or GENERIC_NEVER.search(os.path.splitext(os.path.basename(path))[0]):
            return None
        if any(a.split("=", 1)[0] in GENERIC_STOP_FLAGS for a in words[1:]):
            return None
        sub = next((a for a in words[1:2] if re.match(r"^[a-z][\w:-]*$", a)), None)
        if sub and (GENERIC_NEVER.search(sub) or LIGHT_SUBCOMMANDS.match(sub)):
            return None
        sig = f"script:{script_sig(path, here)}" + (f":{sub}" if sub else "")
        return {"kind": "script", "tool": os.path.basename(path), "sig": sig, "dir": here}
    return None


def finish_generic(raw):
    """Surowy job z parse_generic na pełny job; repo z katalogu komendy (albo sam katalog)."""
    here = raw["dir"] if os.path.isdir(raw["dir"]) else os.getcwd()
    repo = find_repo(here) or here
    label = " ".join(shlex.quote(a) if re.search(r"[\s'\"$^*|&;]", a) else a for a in raw["argv"])
    name = project_name(repo)
    return {
        "lang": "generic",
        "kind": raw["kind"],
        "tool": raw["tool"],
        "class": f"{name}:{raw['kind']}:{raw['sig']}",
        "family": f"{name}:{raw['kind']}:{raw['sig'].split(':', 1)[0]}",
        "floor_gb": raw.get("floor_gb"),
        "module_name": name,
        "module": os.path.relpath(here, repo),
        "module_dir": here,
        "repo_dir": repo,
        "repo": name,
        "scope": "generic",
        "scope_detail": raw["sig"],
        "compile": False,
        "filtered": False,
        "all": False,
        "race": False,
        "p_explicit": None,
        "count1": False,
        "flags": {},
        "pkgs": [],
        "argv": raw["argv"],
        "go_dir": here,
        "env": raw.get("env") or {},
        "label": label,
    }


def generic_prior(job):
    gb, s = GENERIC_PRIORS.get(job["kind"], (4.0, 300))
    return max(gb, job.get("floor_gb") or 0), s


def classify(command, cwd, argv=None):
    """Job z komendy powłoki albo argv; None, gdy nie ma w niej ciężkiej pracy dla schedulera: Go,
    JS, natywnej ani niczego, co z kształtu komendy jest pracą (GENERIC_TOOLS, skrypty)."""
    if argv is not None:
        if (
            len(argv) >= 3
            and os.path.basename(argv[0]) in ("bash", "sh", "zsh")
            and argv[1] == "-c"
        ):
            return classify(argv[2], cwd)
        segments = [(list(argv), "")]
        command = " ".join(shlex.quote(a) for a in argv)
    else:
        if any(m in command for m in SKIP_MARKERS):
            return None
        segments = split_segments(command)
        if segments is None:
            return None
    here = cwd
    found, nested = [], []
    cfg = load_config()
    node_on, native_on = bool(cfg.get("node", True)), bool(cfg.get("native", True))
    generic_on = bool(cfg.get("generic", True))
    for words, _sep in segments:
        if not words:
            continue
        env, words = strip_prefix(words)
        if not words:
            continue
        prog = os.path.basename(words[0])
        job = None
        if prog == "cd" and len(words) >= 2:
            here = os.path.normpath(os.path.join(here, os.path.expanduser(words[1])))
            continue
        if prog in ("bash", "sh", "zsh") and len(words) >= 3 and words[1] == "-c":
            inner = classify(words[2], here)
            if inner:
                nested.append(inner)
            continue
        if prog == "go":
            parsed = parse_go(words)
            if parsed:
                verb, flags, pkgs, cdir = parsed
                d = os.path.normpath(os.path.join(here, cdir)) if cdir else here
                job = {"kind": verb, "flags": flags, "pkgs": pkgs, "dir": d}
        elif prog == "golangci-lint" and len(words) > 1 and words[1] == "run":
            job = {"kind": "lint", "flags": {}, "pkgs": [], "dir": here}
            for w in words[2:]:
                m = re.match(r"--concurrency[= ]?(\d+)$", w)
                if m:
                    job["flags"]["-p"] = m.group(1)
        elif prog == "make":
            d, targets, k = here, [], 1
            while k < len(words):
                if words[k] == "-C" and k + 1 < len(words):
                    d = os.path.normpath(os.path.join(here, words[k + 1]))
                    k += 2
                    continue
                if not words[k].startswith("-") and "=" not in words[k]:
                    targets.append(words[k])
                k += 1
            if targets:
                job = {
                    "kind": "make",
                    "flags": {},
                    "pkgs": [],
                    "target": targets[0],
                    "dir": d,
                }
        elif prog == "govulncheck":
            job = {
                "kind": "vet",
                "flags": {},
                "pkgs": [w for w in words[1:] if not w.startswith("-")],
                "dir": here,
            }
        else:
            job = parse_native(words, here) if native_on else None
            if job is None and node_on:
                job = parse_node(words, here)
        if job is None and generic_on and prog not in ("go", "golangci-lint", "govulncheck"):
            job = parse_generic(words, here)
            if job:
                job["lang"] = "generic"
        if job:
            job["argv"] = words
            job["env"] = env
            found.append(job)
    finishers = {"node": finish_node, "native": finish_native, "generic": finish_generic}
    jobs = list(nested)
    for raw in found:
        lang = raw.get("lang")
        done = finishers.get(lang, finish_job)(raw)
        if done is None and lang is None and raw["kind"] == "make" and generic_on and not GENERIC_NEVER.search(raw["target"]):
            # make poza modułem Go: przepis jak każdy inny skrypt
            done = finish_generic(dict(raw, lang="generic", kind="script", tool="make", sig=f"make:{raw['target']}"))
        if done:
            jobs.append(done)
    if not jobs:
        return None
    main = max(jobs, key=lambda j: prior(j, 4)[0])
    main["multi"] = len(jobs) > 1
    main["simple"] = len(jobs) == 1 and simple_command(segments)
    main["cmd"] = command
    return main


def simple_command(segments):
    """Sama praca Go (z cd, rtk proxy, ogonem | tail): tylko taką da się wysłać na Depot jako argv."""
    for words, _sep in segments:
        _, w = strip_prefix(words)
        if not w:
            continue
        if (
            os.path.basename(w[0])
            in ("cd", "go", "golangci-lint", "make") + TAIL_PROGRAMS
        ):
            continue
        return False
    return True


def finish_job(raw):
    module_dir = find_module(raw["dir"])
    if not module_dir:
        return None
    repo = find_repo(module_dir) or module_dir
    name = os.path.basename(module_dir)
    kind, flags = raw["kind"], raw["flags"]
    compile_only = filtered = False
    if kind == "test":
        run = flags.get("-run")
        run = run.strip("'\"") if isinstance(run, str) else run
        compile_only = bool(
            flags.get("-c")
            or flags.get("-list")
            or run in ("^$", "XXX", "^NONE$", "NONE")
        )
        # -run z wzorcem: kompilacja jak przy samej kompilacji, a biegnie tylko kilka testów
        filtered = not compile_only and isinstance(run, str) and bool(run)
    if kind == "make":
        scope = detail = raw["target"]
    elif kind == "lint":
        scope, detail = "tree", "./..."
    else:
        scope, detail = scope_of(raw["pkgs"], module_dir, raw["dir"])
    race = bool(flags.get("-race"))
    p_explicit = None
    if "-p" in flags and str(flags["-p"]).isdigit():
        p_explicit = int(flags["-p"])
    m = re.search(r"-p[= ](\d+)", raw["env"].get("GOFLAGS", ""))
    if m and p_explicit is None:
        p_explicit = int(m.group(1))
    if kind in ("make",):
        cls = f"{name}:make:{detail}"
    elif scope in ("pkg", "handlers"):
        cls = f"{name}:{kind}:pkg:{detail}"
    elif scope == "subtree":
        cls = f"{name}:{kind}:subtree:{detail}"
    else:
        cls = f"{name}:{kind}:{scope}"
    if race:
        cls += ":race"
    if compile_only:
        cls += ":compile"
    elif filtered:
        cls += ":filtered"
    label = " ".join(
        shlex.quote(a) if re.search(r"[\s'\"$^*|&;]", a) else a for a in raw["argv"]
    )
    return {
        "kind": kind,
        "class": cls,
        "module_name": name,
        "module": os.path.relpath(module_dir, repo),
        "module_dir": module_dir,
        "repo_dir": repo,
        "repo": os.path.basename(repo),
        "scope": scope,
        "scope_detail": detail,
        "compile": compile_only,
        "filtered": filtered,
        "race": race,
        "p_explicit": p_explicit,
        "count1": str(flags.get("-count", "")) == "1",
        "flags": flags,
        "pkgs": raw["pkgs"],
        "argv": raw["argv"],
        "go_dir": raw["dir"],
        "env": raw["env"],
        "label": label,
    }


def opaque_job(argv, cwd):
    """Skrypt pod plock.py go, którego środka nie widać: klasa po nazwie skryptu, uczona z historii."""
    repo = find_repo(cwd) or cwd
    name = os.path.basename(argv[0]) if argv else "?"
    return {
        "kind": "script",
        "class": f"{os.path.basename(repo)}:script:{name}",
        "module_name": os.path.basename(repo),
        "module": ".",
        "module_dir": cwd,
        "repo_dir": repo,
        "repo": os.path.basename(repo),
        "scope": "script",
        "scope_detail": name,
        "compile": False,
        "filtered": False,
        "race": False,
        "p_explicit": None,
        "count1": False,
        "flags": {},
        "pkgs": [],
        "argv": list(argv),
        "go_dir": cwd,
        "label": " ".join(shlex.quote(a) for a in argv),
        "multi": False,
        "simple": False,
        "cmd": None,
    }


# ---------- przewidywanie ----------


def p_sensitive(job):
    """Czy -p zmienia pamięć i czas: tylko całe moduły (build/vet/test ./...)."""
    return (
        job["kind"] in ("build", "vet", "test", "install")
        and job["scope"] == "tree"
        and job["module_name"] not in SMALL_MODULES
    )


@functools.lru_cache(maxsize=16)
def go_packages(root):
    """Katalogi pakietów Go pod root, jak dla wzorca root/...: bez testdata, vendor, katalogów
    zaczętych od . albo _ i zagnieżdżonych modułów (node_modules pominięte dla szybkości)."""
    found = []
    for d, subdirs, files in os.walk(root):
        subdirs[:] = [
            s for s in subdirs
            if not s.startswith((".", "_"))
            and s not in ("testdata", "vendor", "node_modules")
            and not os.path.isfile(os.path.join(d, s, "go.mod"))
        ]  # fmt: skip
        if any(f.endswith(".go") for f in files):
            found.append(d)
    return tuple(found)


def subtree_share(job):
    """Część pakietów modułu objęta poddrzewem joba; 1.0, gdy wzorca nie da się rozwiązać."""
    total = go_packages(job["module_dir"])
    dirs = set()
    for rel in job["scope_detail"].split():
        root = os.path.join(job["module_dir"], rel)
        if "..." in rel or not os.path.isdir(root):
            return 1.0
        dirs.update(go_packages(root))
    return min(1.0, len(dirs) / len(total)) if total else 1.0


def prior(job, p):
    """(GB, s) z pomiarów albo ostrożne wartości ogólne."""
    if job.get("lang") == "node":
        return node_prior(job)
    if job.get("lang") == "native":
        return tuple(job["native_prior"])
    if job.get("lang") == "generic":
        return generic_prior(job)
    name, kind, scope, comp = (
        job["module_name"],
        job["kind"],
        job["scope"],
        job["compile"] or job.get("filtered", False),
    )
    if scope == "subtree":
        # między jednym pakietem a całym modułem, w proporcji do liczby pakietów; poddrzewo
        # z internal/handlers (sam waży tyle co cały moduł) nie jest lżejsze od niego
        share = subtree_share(job)
        lo_gb, lo_s = base_prior(name, kind, "pkg", comp, p)
        hi_gb, hi_s = base_prior(name, kind, "tree", comp, p)
        gb = lo_gb + (hi_gb - lo_gb) * share
        s = round(lo_s + (hi_s - lo_s) * share)
        if any(r == HANDLERS or HANDLERS.startswith(r + "/") for r in job["scope_detail"].split()):
            h_gb, h_s = base_prior(name, kind, "handlers", comp, p)
            gb, s = max(gb, h_gb), max(s, h_s)
    else:
        gb, s = base_prior(name, kind, scope, comp, p)
    if job.get("filtered"):
        gb, s = gb * 1.2, s * 1.5  # kompilacja jak przy -run '^$' i kilka testów w procesie
    if job["race"]:
        gb, s = gb * RACE_FACTOR, s * 1.5
    return round(gb, 2), s


def base_prior(name, kind, scope, comp, p):
    if name in SMALL_MODULES and kind != "script":
        val = (
            (2.0, 90)
            if (kind == "test" and scope == "tree" and not comp)
            else (1.5, 40)
        )
    else:
        val = PRIORS.get((name, kind, scope, comp))
        if val is None and kind == "test" and scope in ("pkg", "pkgs"):
            val = PRIORS.get((name, "test", "pkg", comp))
        if val is None:
            if kind == "test" and scope in ("pkg", "pkgs", "handlers"):
                val = (4.5, 30) if comp else (3.0, 60)
            else:
                val = (
                    GENERIC.get((kind, "tree" if scope == "tree" else None))
                    or GENERIC.get((kind, None))
                    or (4.0, 60)
                )
    if isinstance(val, dict):
        if p in val:
            val = val[p]
        else:
            nearest = min(val, key=lambda k: abs(k - p))
            gb, s = val[nearest]
            val = (gb * p / nearest if p > nearest else gb, s)
    return val


def read_history(limit_bytes=8 * 1024 * 1024, needle=None):
    """Wiersze historii; z `needle` tylko te, w których stoi ten tekst (bez parsowania reszty:
    krótka komenda spoza Go nie płaci za cały plik)."""
    try:
        size = os.path.getsize(HISTORY_PATH)
        with open(HISTORY_PATH, "rb") as f:
            if size > limit_bytes:
                f.seek(size - limit_bytes)
                f.readline()
            data = f.read().decode(errors="replace")
    except OSError:
        return []
    rows = []
    for line in data.splitlines():
        if needle and needle not in line:
            continue
        try:
            rows.append(json.loads(line))
        except ValueError:
            pass
    return rows


def percentile(values, q):
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    pos = (len(values) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(values) - 1)
    return values[lo] + (values[hi] - values[lo]) * (pos - lo)


def predict(job, p, history):
    """(GB, s, źródło): p90 szczytu × 1,15 i mediana czasu z ostatnich lokalnych biegów tej klasy."""
    rows = [
        r for r in history
        if r.get("where") == "local" and r.get("class") == job["class"] and r.get("peak_gb") and r.get("wall_s")
    ]  # fmt: skip
    if p_sensitive(job):
        rows = [r for r in rows if r.get("p") == p]
    rows = rows[-20:]
    base_gb, base_s = prior(job, p)
    src = "prior"
    if job.get("lang") == "generic":
        family = family_estimate(job, history)
        if family:
            base_gb, base_s, src = family
    floor = job.get("floor_gb") or 0.0
    if not rows:
        return round(max(base_gb, floor), 2), round(base_s, 1), src
    gb = percentile([r["peak_gb"] for r in rows], 0.9) * 1.15
    s = percentile([r["wall_s"] for r in rows], 0.5)
    if len(rows) < 3:
        gb = max(gb, base_gb * 0.8)  # jeden czy dwa biegi to jeszcze nie statystyka
    if job.get("outside"):
        gb = max(gb, base_gb)  # pamięć poza drzewem: zmierzony szczyt to tylko jej część
    return round(max(gb, floor), 2), round(s, 1), f"history:{len(rows)}"


def family_estimate(job, history):
    """(GB, s, źródło) dla nieznanego jeszcze podpisu z rodziny (to samo repo, rodzaj i narzędzie):
    nowy skrypt Pythona w repo, w którym skrypty Pythona biorą po 0,2 GB, nie czeka na 4 GB.
    p90 × 1,5, bo w rodzinie bywa i skrypt z przeglądarką; None przy mniej niż 5 biegach."""
    fam = job.get("family")
    rows = [
        r for r in history
        if r.get("where") == "local" and r.get("lang") == "generic" and r.get("peak_gb") and r.get("wall_s")
        and ":".join(str(r.get("class", "")).split(":")[:3]) == fam
    ][-40:]  # fmt: skip
    if len(rows) < 5:
        return None
    gb = max(1.0, percentile([r["peak_gb"] for r in rows], 0.9) * 1.5)
    s = percentile([r["wall_s"] for r in rows], 0.5) * 1.5
    return round(gb, 2), round(s, 1), f"family:{len(rows)}"


def choose_p(job, free_gb, history):
    """-p: od agenta, jeśli podał; dla całych modułów najszybsze z 2/4/6/8, które się mieści."""
    if job["p_explicit"]:
        gb, s, src = predict(job, job["p_explicit"], history)
        return job["p_explicit"], "agent", gb, s, src
    if not p_sensitive(job):
        gb, s, src = predict(job, 4, history)
        return None, "default", gb, s, src
    options = [(p,) + predict(job, p, history) for p in (2, 4, 6, 8)]
    fitting = [o for o in options if o[1] <= free_gb]
    if fitting:
        p, gb, s, src = min(fitting, key=lambda o: (o[2], o[1], abs(o[0] - 4)))
    else:
        p, gb, s, src = min(options, key=lambda o: (o[1], o[2], abs(o[0] - 4)))
    return p, "scheduler", gb, s, src


def likely_heavy(job):
    return prior(job, 4)[0] > DEFAULTS["small_gb"]


# ---------- Depot ----------


def depot_key(job):
    """Klucz DEPOT_CI_JOBS; -run z wzorcem nigdy nie trafia do joba z pełnym zestawem testów."""
    comp = "filtered" if job.get("filtered") else job["compile"]
    return (job["module_name"], job["kind"], job["scope"], comp)


# flagi, które piszą plik u agenta: na Depot powstałby na zdalnej maszynie i nie wrócił
OUTPUT_FLAGS = (
    "-o", "-c", "-coverprofile", "-cpuprofile", "-memprofile", "-blockprofile", "-mutexprofile",
    "-trace", "-outputdir",
)  # fmt: skip
# GOFLAGS z samymi tymi flagami nic nie zmienia na Depot (depot-exec i tak ustawia -p)
DEPOT_SAFE_GOFLAGS = ("-p", "-count")


def depot_blocker(job):
    """Dlaczego job musi zostać na tym Macu, albo None. Depot dostaje drzewo repo (śledzone i
    nieśledzone pliki bez .gitignore) pod inną ścieżką, bez zmiennych z komendy, i odsyła tylko
    wyjście: ścieżka bezwzględna, z ~ albo $, względna poza repo albo ignorowana, zmienna
    środowiska i plik zapisany przez -o czy -coverprofile dałyby tam inny wynik niż tutaj."""
    if "depot_blocker" in job:
        return job["depot_blocker"]
    reason = None
    flags = job.get("flags") or {}
    env = job.get("env") or {}
    written = [f for f in OUTPUT_FLAGS if f in flags]
    goflags = (env.get("GOFLAGS") or "").split()
    if written:
        reason = f"{written[0]} writes a file on this Mac"
    elif any(k != "GOFLAGS" for k in env):
        name = next(k for k in env if k != "GOFLAGS")
        reason = f"{name}=… does not reach Depot"
    elif any(f.partition("=")[0] not in DEPOT_SAFE_GOFLAGS for f in goflags):
        reason = "GOFLAGS does not reach Depot"
    else:
        reason = local_path_in(job)
    job["depot_blocker"] = reason
    return reason


def local_path_in(job):
    """Pierwszy argument komendy, który wskazuje plik spoza tego, co jedzie na Depot, albo None."""
    repo, here = job["repo_dir"], job.get("go_dir") or job["repo_dir"]
    inside = []
    for word in list(job.get("argv") or [])[1:]:
        for piece in re.split(r"[=,]", word):
            if not piece or piece.startswith("-"):
                continue
            if piece.startswith(("/", "~", "$")):
                return f"{word} is a path on this Mac"
            if "/" not in piece and not piece.startswith(".."):
                continue
            path = os.path.normpath(os.path.join(here, piece.removesuffix("/...")))
            rel = os.path.relpath(path, repo)
            if rel == ".." or rel.startswith("../"):
                return f"{word} points outside the repo"
            if os.path.exists(path):
                inside.append((rel, word))
    if inside:
        import subprocess

        try:
            r = subprocess.run(
                ["git", "-C", repo, "check-ignore", "--stdin"],
                input="\n".join(rel for rel, _ in inside),
                capture_output=True,
                text=True,
                timeout=3,
            )
            ignored = set(r.stdout.split()) if r.returncode == 0 else set()
        except (OSError, subprocess.SubprocessError):
            ignored = set()
        for rel, word in inside:
            if rel in ignored:
                return f"{word} is gitignored, so it stays on this Mac"
    return None


def depot_target(job, gb, wall, cfg, cache):
    """Dokąd na Depot i za ile: {target, job, cores, eta_s, units, cost_usd, argv, cwd} albo None."""
    if job.get("lang"):
        return None  # Depot tu to tylko joby Go portivo
    if depot_blocker(job):
        return None
    repo = job["repo_dir"]
    etas = (cache.get("depot_eta") or {}).get("jobs") or {}
    ci = DEPOT_CI_JOBS.get(depot_key(job))
    if (
        ci
        and os.path.isfile(os.path.join(repo, "scripts/depot-ci.sh"))
        and not job.get("multi")
    ):
        name, cores, eta = ci
        measured = etas.get(f"go-heavy/{name}") or {}
        eta = float(measured.get("p50_s") or eta) + DEPOT_SETUP_S
        units = cores / 2 * eta / 60
        return {
            "target": "depot-ci",
            "job": name,
            "cores": cores,
            "eta_s": round(eta),
            "units": round(units, 1),
            "cost_usd": round(units * cfg["unit_usd"], 2),
            "argv": ["scripts/depot-ci.sh", "go-heavy.yml", name],
            "cwd": repo,
        }
    if not os.path.isfile(os.path.join(repo, "scripts/depot-exec.sh")) or not job.get(
        "simple"
    ):
        return None
    need = max(gb / 0.85, 1.0)
    want_p = job["p_explicit"] or 2
    cores = next((c for c in DEPOT_SIZES if c * 4 >= need and c >= want_p), None)
    if cores is None:
        return None
    measured = etas.get(f"depot-exec-{cores}/exec") or {}
    eta = float(measured.get("p50_s") or 0) or (DEPOT_SETUP_S + 15 + wall)
    units = cores / 2 * eta / 60
    module = job["module"] if job["module"] != "." else None
    return {
        "target": "depot-exec",
        "job": "exec",
        "cores": cores,
        "eta_s": round(eta),
        "units": round(units, 1),
        "cost_usd": round(units * cfg["unit_usd"], 2),
        "argv": ["scripts/depot-exec.sh", "--cores", str(cores)]
        + (["--with", "pg"] if job.get("uses_pg") else [])
        + (["--dir", module] if module else [])
        + ["--"]
        + list(job["argv"]),
        "cwd": repo,
    }


def refresh_depot_eta(cache, repo_dir):
    """p50 czasów Depot z `scripts/depot-cost.py eta --json`, najwyżej co 30 min (bez collect)."""
    eta = cache.get("depot_eta") or {}
    if time.time() - float(eta.get("at", 0)) < 1800 and eta.get("repo") == repo_dir:
        return False
    script = os.path.join(repo_dir, "scripts/depot-cost.py")
    jobs = {}
    if os.path.isfile(script):
        import subprocess

        # ten sam interpreter co reszta claude-acc: python z setup.sh, bez niego systemowy
        python = os.path.join(STATE_DIR, "python")
        python = python if os.access(python, os.X_OK) else "/usr/bin/python3"
        try:
            since = load_config().get("depot_eta_since")
            out = subprocess.run(
                [python, script, "eta", "--json"] + (["--since", since] if since else []),
                cwd=repo_dir,
                capture_output=True,
                text=True,
                timeout=20,
            ).stdout
            data = json.loads(out or "{}")
            items = (
                data.items()
                if isinstance(data, dict)
                else ((d.get("job"), d) for d in data)
            )
            for k, v in items:
                if k and isinstance(v, dict):
                    jobs[k] = {
                        "p50_s": v.get("p50_s"),
                        "p90_s": v.get("p90_s"),
                        "cores": v.get("cores"),
                    }
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass
    cache["depot_eta"] = {"at": time.time(), "repo": repo_dir, "jobs": jobs}
    return True


# ---------- cache testów: -count=1 ----------


def go_list(args, cwd, timeout=60):
    import subprocess

    try:
        out = subprocess.run(
            ["go", "list"] + args,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout if out.returncode == 0 else None


# wzorce, których hook nie używa, jako tekst: re kompiluje je przy pierwszym użyciu (i trzyma
# w swoim cache), a nie przy każdym załadowaniu modułu przez hook
EXEC_IMPORT = r'(?m)^\s*(?:import\s+)?(?:([\w.]+)\s+)?"os/exec"'


def exec_calls(path):
    """Czy plik Go uruchamia inny program (exec.Command, exec.CommandContext, os.StartProcess)."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return False
    if "os.StartProcess(" in text or "syscall.Exec(" in text:
        return True
    m = re.search(EXEC_IMPORT, text)
    if not m:
        return False
    alias = m.group(1) or "exec"
    if alias == "_":
        return False
    if alias == ".":
        return re.search(r"\bCommand(Context)?\s*\(", text) is not None
    return re.search(rf"\b{re.escape(alias)}\.Command(Context)?\s*\(", text) is not None


def go_files(directory, tests=True):
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return []
    return [
        os.path.join(directory, n)
        for n in names
        if n.endswith(".go") and (tests or not n.endswith("_test.go"))
    ]


def files_signature(paths):
    import hashlib

    sig = []
    for path in paths:
        try:
            st = os.stat(path)
        except OSError:
            continue
        sig.append(f"{path}:{st.st_size}:{int(st.st_mtime_ns)}")
    return hashlib.sha1("|".join(sig).encode()).hexdigest()


def module_path(module_dir):
    try:
        with open(os.path.join(module_dir, "go.mod")) as f:
            for line in f:
                if line.startswith("module "):
                    return line.split()[1]
    except OSError:
        pass
    return None


def test_inputs(job, cache):
    """Co czytają testy pakietów joba: {"exec": bool, "pg": bool} albo None (go list padł).

    exec: pakiet (testy i kod) albo pakiet ściągany tylko przez testy (pomocniki testów i ich
    zależności z modułu) uruchamia inny program; tego, co czyta podproces (git, node, atlas
    migrate lint), cache testów Go nie widzi. pg: testy sięgają po testpg/testhelpers/testcontainers.
    Wynik w cache po podpisie plików (rozmiar, mtime); go list tylko po zmianie go.sum albo
    zestawu plików pakietu."""
    pkgs = job["pkgs"] or ["."]
    key = f"{job['go_dir']}|{' '.join(pkgs)}"
    entry = (cache.get("tests") or {}).get(key)
    try:
        gosum = os.path.getmtime(os.path.join(job["module_dir"], "go.sum"))
    except OSError:
        gosum = 0
    if entry and entry.get("gosum") == gosum:
        target_sig = files_signature([f for d in entry["targets"] for f in go_files(d)])
        if target_sig == entry.get("target_sig"):
            files = [f for d in entry["targets"] for f in go_files(d)]
            files += [f for d in entry["helpers"] for f in go_files(d, tests=False)]
            sig = files_signature(files)
            if sig == entry.get("sig"):
                return entry
    listing = go_list(["-deps", "-test", "-f", "{{.ImportPath}}|{{.Dir}}|{{.Standard}}"] + pkgs, job["go_dir"])
    plain = go_list(["-deps", "-f", "{{.ImportPath}}"] + pkgs, job["go_dir"])
    targets = go_list(["-f", "{{.Dir}}"] + pkgs, job["go_dir"])
    if listing is None or plain is None or targets is None:
        return None
    mod = module_path(job["module_dir"]) or "\x00"
    target_dirs = sorted(set(targets.split()))
    plain_set = set(plain.split())
    helpers, pg = set(), False
    for line in listing.splitlines():
        parts = line.split("|")
        if len(parts) != 3:
            continue
        imp, directory, standard = parts
        base = imp.split(" ")[0]
        if any(m in base for m in PG_MARKERS):
            pg = True
        if standard == "true" or base.endswith(".test"):
            continue
        if not (base == mod or base.startswith(mod + "/")):
            continue
        if directory in target_dirs or base in plain_set:
            continue  # pakiet testowany albo używany też przez kod produkcyjny
        helpers.add(directory)
    files = [f for d in target_dirs for f in go_files(d)]
    files += [f for d in sorted(helpers) for f in go_files(d, tests=False)]
    trusted = tuple(os.path.join(job["module_dir"], t) for t in load_config().get("count1_trusted_exec") or ())
    entry = {
        "exec": any(exec_calls(f) for f in files if not (trusted and f.startswith(trusted))),
        "pg": pg,
        "targets": target_dirs,
        "helpers": sorted(helpers),
        "gosum": gosum,
        "target_sig": files_signature([f for d in target_dirs for f in go_files(d)]),
        "sig": files_signature(files),
        "at": time.time(),
    }
    cache.setdefault("tests", {})[key] = entry
    return entry


def count1_safe(job, cache):
    """Czy w iteracji agenta można zdjąć -count=1 (wynik może przyjść z cache testów Go).

    Tak, gdy ani testy pakietu, ani pakiet, ani jego pomocniki testów nie uruchamiają innych
    programów. Pakiety z bazą są w porządku: testpg czyta migracje, Dockerfile Postgresa i atlasa
    w procesie testu (os.ReadFile, LookPath), a te wejścia cache testów Go śledzi."""
    if job["kind"] != "test" or not job["count1"] or job["scope"] not in ("pkg", "pkgs", "handlers"):
        return False
    allowed = {"-count", "-run", "-v", "-short", "-timeout", "-p", "-failfast", "-skip", "-cpu", "-parallel"}
    if any(k not in allowed for k in job["flags"]):
        return False
    inputs = test_inputs(job, cache)
    return inputs is not None and not inputs["exec"]


def uses_pg(job, cache):
    """Czy testy joba sięgają po Postgresa (dla depot-exec --with pg)."""
    if job["kind"] != "test" or job.get("lang"):
        return False
    if job["scope"] == "tree":
        return os.path.isdir(os.path.join(job["module_dir"], "internal/testhelpers"))
    inputs = test_inputs(job, cache)
    return bool(inputs and inputs["pg"])


COUNT1 = r"(?<![\w-])-count(?:=| )1(?![\w.])"


def drop_count1(command):
    """Komenda bez jedynego -count=1 (albo -count 1); None, gdy nie da się tego zrobić jednoznacznie."""
    if len(re.findall(COUNT1, command)) != 1:
        return None
    return re.sub(r" ?" + COUNT1, "", command, count=1)


# ---------- stan ----------


class Locked:
    """flock na sched/lock; block=False: ok=False, gdy ktoś inny trzyma."""

    def __init__(self, block=True):
        self.block = block
        self.fd = None
        self.ok = False

    def __enter__(self):
        import fcntl

        os.makedirs(SCHED_DIR, exist_ok=True)
        self.fd = os.open(LOCK_PATH, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | (0 if self.block else fcntl.LOCK_NB))
            self.ok = True
        except BlockingIOError:
            self.ok = False
        return self

    def __exit__(self, *exc):
        import fcntl

        if self.ok:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
        os.close(self.fd)


def new_today():
    return {
        "date": time.strftime("%Y-%m-%d"),
        "jobs_local": 0,
        "jobs_depot": 0,
        "wait_s": 0.0,
        "old_lock_wait_s": 0.0,  # czekanie jobów Go przy dawnym `plock go` (old_lock)
        "go_wait_s": 0.0,  # prawdziwe czekanie tych samych jobów
        "wait_saved_s": 0.0,
        "depot_units": 0.0,
        "depot_cost_usd": 0.0,
        "local_kept_usd": 0.0,
        "overtakes": 0,
        "pauses": 0,
        "peak_concurrency": 0,
        "max_reserved_gb": 0.0,
    }


def empty_state(cfg):
    return {
        "version": VERSION,
        "updated_at": time.time(),
        "idle_since": time.time(),
        "host": {
            "ram_gb": round((sysctl_int("hw.memsize") or 0) / GB, 1),
            "cores_p": sysctl_int("hw.perflevel0.physicalcpu") or 0,
            "cores_e": sysctl_int("hw.perflevel1.physicalcpu") or 0,
        },
        "config": {k: cfg[k] for k in PUBLIC_CONFIG},
        "memory": {},
        "running": [],
        "queue": [],
        "overtakes": [],
        "recent": [],
        "today": new_today(),
        "_internal": {"swap": [], "avail": [], "old_lock": new_old_lock()},
    }


def new_old_lock():
    return {"wait_s": 0.0, "free_at": 0.0, "pending": []}


def load_state(cfg):
    try:
        with open(STATE_PATH) as f:
            state = json.load(f)
        if state.get("version") != VERSION:
            raise ValueError
    except (OSError, ValueError):
        state = empty_state(cfg)
    internal = state.setdefault("_internal", {"swap": [], "avail": []})
    if state.get("today", {}).get("date") != time.strftime("%Y-%m-%d"):
        state["today"] = new_today()
        internal["old_lock"] = new_old_lock()
    if "old_lock" not in internal:
        # stan z zamkiem liczonym ze wszystkich jobów w kolejności końca (38278h): bilans od nowa
        internal.pop("vlock_free_at", None)
        internal["old_lock"] = new_old_lock()
        state["today"].update(old_lock_wait_s=0.0, go_wait_s=0.0, wait_saved_s=0.0)
    state["config"] = {k: cfg[k] for k in PUBLIC_CONFIG}
    return state


def save_state(state):
    now = time.time()
    state["updated_at"] = now
    busy = state["running"] or state["queue"]
    state["idle_since"] = None if busy else (state.get("idle_since") or now)
    os.makedirs(SCHED_DIR, exist_ok=True)
    tmp = f"{STATE_PATH}.tmp{os.getpid()}"
    # dumps i jeden zapis: json.dump pisze kawałkami przez koder w Pythonie (3x wolniej)
    data = json.dumps(state, ensure_ascii=False, indent=1)
    with open(tmp, "w") as f:
        f.write(data)
    os.replace(tmp, STATE_PATH)


def load_cache():
    try:
        with open(CACHE_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_cache(cache):
    os.makedirs(SCHED_DIR, exist_ok=True)
    tmp = f"{CACHE_PATH}.tmp{os.getpid()}"
    data = json.dumps(cache)
    with open(tmp, "w") as f:
        f.write(data)
    os.replace(tmp, CACHE_PATH)


def reap(state):
    """Wpisy po martwych wrapperach znikają; ich dziecko (sierota) dostaje SIGTERM."""
    keep = []
    for job in state["running"]:
        if alive(job.get("pid")):
            keep.append(job)
            continue
        pgid = job.get("child_pgid")
        if pgid and alive(pgid):
            import signal

            for sig in (signal.SIGCONT, signal.SIGTERM):
                try:
                    os.killpg(pgid, sig)
                except OSError:
                    pass
    state["running"] = keep
    state["queue"] = [j for j in state["queue"] if alive(j.get("pid"))]


def growth_left(job, now):
    """O ile lokalny job jeszcze urośnie: prognoza minus teraz. Zero dla joba, który biegnie dużo
    dłużej, niż miał (serwer albo watcher, który skrypt zostawił na pierwszym planie): jego pamięć
    jest już w tym, co widzi jądro, a rezerwa na wzrost, który nie przyjdzie, blokowałaby kolejkę."""
    if job.get("paused"):
        return 0.0
    elapsed = now - (job.get("started_at") or now)
    if elapsed > max(600.0, 3 * (job.get("predicted_wall_s") or 0)):
        return 0.0
    return max(0.0, (job.get("mem_predicted_gb") or 0) - (job.get("mem_now_gb") or 0))


def refresh_memory(state, cfg, mem=None):
    mem = mem or probe_memory()
    now = time.time()
    ram = mem["ram_gb"]
    available = mem["level"] / 100 * ram
    local = [j for j in state["running"] if j["where"] == "local"]
    jobs_now = sum(j.get("mem_now_gb") or 0 for j in local)
    reserved = sum(growth_left(j, now) for j in local)
    snap = devguard_snapshot()
    dev = devserver_reserve_gb(snap)
    stage, long_lived, families = guard_view(snap)
    internal = state["_internal"]
    # natywny build spoza schedulera (odczepiony builder portivo-mobile, Xcode): zajmuje miejsce
    # na natywny build i urośnie do szczytu buildu, więc jego wzrost idzie do rezerwy jak wzrost jobów
    nat = internal.get("native") or {}
    if "native" in mem:
        nat = {"at": now, "gb": float(mem["native"].get("gb", 0)), "active": bool(mem["native"].get("active"))}
    elif now - float(nat.get("at", 0)) >= 5:
        scan = native_scan()
        nat = {"at": now, "gb": round(scan["gb"], 2), "active": scan["active"]}
        if scan["active"]:
            nat["in_job"] = native_host(local, scan["pids"])
    internal["native"] = nat
    owner = native_owner(state)
    outside = bool(nat.get("active")) and owner is None
    native_reserve = max(0.0, native_build_gb(internal) - nat["gb"]) if outside else 0.0
    reserved += native_reserve
    swap = [s for s in internal.get("swap", []) if now - s[0] <= 120]
    swap.append([now, mem["swap_gb"]])
    internal["swap"] = swap[-240:]
    day = time.strftime("%Y-%m-%d")
    # ile zmieściłby pusty Mac: uczone tylko z odczytów bez lokalnych jobów (ich footprint liczy
    # też strony skompresowane, więc dostępne + joby potrafi przekroczyć RAM)
    avail = [a for a in internal.get("avail", []) if now - a[2] < 7 * 86400]
    if not local:
        if avail and avail[-1][0] == day:
            avail[-1][1] = max(avail[-1][1], available)
            avail[-1][2] = now
        else:
            avail.append([day, available, now])
    internal["avail"] = avail[-8:]
    # nigdy ponad 85% RAM: tyle macOS realnie oddaje, a stare wpisy (sprzed 3ed7edf) bywały za duże
    idle_top = min(max([a[1] for a in avail] + [cfg["idle_floor_pct"] / 100 * ram]), 0.85 * ram)
    state["host"]["ram_gb"] = round(ram, 1)
    state["memory"] = {
        "level_pct": round(mem["level"]),
        "available_gb": round(available, 2),
        "headroom_gb": cfg["headroom_gb"],
        "devserver_reserve_gb": dev,
        "jobs_now_gb": round(jobs_now, 2),
        "reserved_gb": round(reserved, 2),
        "others_gb": round(max(0.0, ram - available - jobs_now), 2),
        "free_for_admission_gb": round(
            available - cfg["headroom_gb"] - dev - reserved, 2
        ),
        "idle_max_gb": round(idle_top - cfg["headroom_gb"], 1),
        "swap_used_gb": round(mem["swap_gb"], 2),
        "swap_growth_2m_gb": round(mem["swap_gb"] - swap[0][1], 2),
        "pressure": mem["pressure"],
        "guard_level": guard_level(snap),
        "native": {
            "build_gb": round(nat["gb"], 2),
            "active": bool(nat.get("active")),
            "outside": outside,
            "reserve_gb": round(native_reserve, 2),
            "owner": owner["id"] if owner else None,
            "owner_label": owner["label"] if owner else None,
        },
        "simulators": simulators_info(snap),
        # hamulec strażnika i to, co siedzi w pamięci długo (dev serwery, symulatory, watchery,
        # headless przeglądarki, LSP, Docker): jest już w `others_gb`, tu z nazwy
        "brake": STAGE_TEXT[min(stage, 3)],
        "long_lived_gb": long_lived,
        "long_lived": families,
    }
    today = state["today"]
    today["max_reserved_gb"] = round(max(today.get("max_reserved_gb", 0), reserved), 1)
    today["peak_concurrency"] = max(
        today.get("peak_concurrency", 0), len(state["running"])
    )
    return state["memory"]


def native_owner(state):
    """Lokalny job, który trzyma miejsce na natywny build (jedno na Maca), albo None: natywny job
    w fazie buildu albo inny job, w którego drzewie biegnie build (`make ios`, `swift build`)."""
    own = next(
        (
            j
            for j in state["running"]
            if j.get("where") == "local" and j.get("exclusive") and not j.get("native_done")
        ),
        None,
    )
    if own is None:
        host = (state.get("_internal", {}).get("native") or {}).get("in_job")
        own = next((j for j in state["running"] if host and j["id"] == host), None)
    return own


def native_host(local, pids):
    """Id lokalnego joba, w którego drzewie procesów leży natywny build ze skanu, albo None.
    Bez tego skrypt z xcodebuild w środku albo `swift build` (swift-build to dla native_scan
    natywny build) wyglądał jak build spoza schedulera i jego wzrost szedł do rezerwy drugi raz,
    obok przewidywania samego joba."""
    for j in local:
        if j.get("pid") and not j.get("exclusive") and descendants(j["pid"]) & pids:
            return j["id"]
    return None


def native_build_gb(internal):
    """Przewidywany szczyt natywnego buildu: p90 × 1,15 ostatnich zmierzonych buildów (drzewa
    xcodebuild i joba), do pierwszych trzech nie mniej niż 80% wartości z tabeli NATIVE."""
    peaks = [p for p in internal.get("native_peaks", []) if p]
    base = NATIVE["xcodebuild"][1]
    if not peaks:
        return base
    gb = percentile(peaks, 0.9) * 1.15
    if len(peaks) < 3:
        gb = max(gb, base * 0.8)
    return round(gb, 2)


def track_native(me, native, now, internal, now_gb):
    """Faza buildu natywnego joba. Gdy wstaje xcodebuild, prognoza rośnie do szczytu buildu
    (`portivo-mobile up` z samych trafień w cache przewidziałby mało). Gdy kompilatory milkną na
    NATIVE_QUIET_S (albo nic się nie kompiluje przez dwa przewidywane czasy), job oddaje miejsce
    na natywny build i rezerwację: `expo run:ios` po buildzie zostaje z Metro na godziny."""
    if me.get("native_done"):
        return
    if native.get("active"):
        me["native_seen"] = True
        me["native_active_at"] = now
        me["mem_predicted_gb"] = max(me.get("mem_predicted_gb") or 0.0, native_build_gb(internal))
        return
    seen = me.get("native_seen")
    quiet = seen and now - me.get("native_active_at", now) >= NATIVE_QUIET_S
    # zimny build (prebuild, pod install) potrafi długo nie ruszać kompilatorów: czas z tabeli
    # jako dolna granica, bo historia `up` to głównie szybkie biegi z klientem w cache
    floor = NATIVE.get(me.get("native_tool"), (None, 0, 0, None))[2]
    stale = not seen and now - me.get("started_at", now) > 2 * max(me.get("predicted_wall_s") or 0, floor, 300)
    if quiet or stale:
        me["native_done"] = True
        me["native_done_at"] = now
        me["mem_predicted_gb"] = round(now_gb, 2)


def sim_wait(job, mem):
    """Start symulatora czeka, gdy symulatorów agentów (pula portivo-mobile) w użyciu jest tyle,
    ile pozwala strażnik (`max_booted_simulators`): każdy to 2-4 GB, a cudzego, używanego nikt
    nie wyłączy. `portivo-mobile up` sesji, która ma już swój symulator, nic nowego nie włącza.
    Nieużywane wyłącza strażnik; bez jego świeżego pomiaru limitu nie ma."""
    sims = mem.get("simulators") or {}
    return (
        job.get("native_tool") in ("portivo-mobile", "simulator")
        and not job.get("sim_lease")
        and bool(sims.get("cap"))
        and sims.get("agents_in_use", 0) >= sims["cap"]
    )


def queue_order(state):
    return sorted(state["queue"], key=lambda j: j["enqueued_at"])


def plan(state, cfg, now):
    """Kto z kolejki startuje teraz: {id: ("fits"|"overtake", id wyprzedzonego)}.

    FIFO; głowa startuje, gdy się mieści, a gdy lokalnie nic nie biegnie: bez rezerwy na dev
    serwer, po 30 s czekania w ogóle. Za zablokowaną głową startują małe joby, które się mieszczą,
    a gdy głowa czeka dłużej niż starve_s, tylko te, które zostawiają jej miejsce (rezerwacja).

    Mały job (krótki i lekki: testy JS, jeden pakiet Go) wystarczy, że zmieści się w pamięci
    dostępnej teraz: rezerwy długich jobów na wzrost, którego jeszcze nie ma, go nie blokują.
    Liczą się tylko prognozy świeżo wpuszczonych małych jobów, bo te zajmą pamięć za chwilę.
    Głowa czekająca dłużej niż starve_s zostawia sobie miejsce i w pamięci dostępnej teraz, a po
    2 × starve_s rezerwacja jest twarda: strumień krótkich jobów nie zagłodzi dużego.

    Rezerwacja nie trzyma jednak pamięci, której głowa jeszcze nie użyje: każdy job za nią (także
    nie mały) startuje, jeśli nie opóźni jej startu (`backfill`); potrzebuje do tego prognozy z
    własnych biegów. 2026-10-09 głowa `next build` (11,7 GB przy 5,9 wolnych) trzymała tak 25
    krótkich jobów do 25 minut, choć wszystkie skończyłyby się, zanim zwolniła się dla niej pamięć.

    Natywny build i start symulatora startują tylko w pamięci wolnej po rezerwach (sam na Macu:
    w dostępnej minus zapas), nigdy ponad nią i nigdy, gdy strażnik dev serwerów mówi o presji
    krytycznej. Jego pamięć leży częściowo poza drzewem procesów (symulator, demon Gradle), więc
    SIGSTOP nic by tu nie uratował, a rosnący swap jądro zgłasza jako „normal” do samego końca.
    Nie wchodzi też przed głowę przez backfill, a za wstrzymaną nim głową backfillu nie ma."""
    mem = state["memory"]
    free = mem["free_for_admission_gb"]
    now_free = mem["available_gb"] - cfg["headroom_gb"] - sum(
        growth_left(j, now) for j in state["running"] if j["where"] == "local" and j.get("small")
    )
    any_local = any(j["where"] == "local" for j in state["running"])
    pressure = mem.get("pressure", "normal")
    guard_critical = mem.get("guard_level") == 2
    brake = mem.get("brake", "normal")
    admitted = {}
    ahead = []  # wpuszczone przed głową w tym przebiegu: ich końce też zwolnią jej pamięć
    blocked = None
    shade = None  # kiedy głowa się zmieści i ile miejsca zostanie obok niej (shadow)
    stuck = False  # głowa nie wystartowałaby nawet bez jobów, które ją wyprzedziły
    reserve = 0.0
    strict = False
    # jeden natywny build naraz: trzyma go job w fazie buildu albo build spoza schedulera
    slot = native_owner(state) is not None or bool((mem.get("native") or {}).get("outside"))
    if pressure == "critical" or brake == "emergency":
        return admitted
    if brake in ("tight", "brake"):
        # hamulec strażnika: startuje tylko to, co się mieści, bez furtki „sam na Macu”
        pressure = "warn"
    for job in queue_order(state):
        if (job.get("route") or {}).get("choice") == "depot":
            continue
        # czeka na swoją kolej, nie na pamięć: nie blokuje jobów za sobą i nic nie rezerwuje
        if (job.get("exclusive") and slot) or sim_wait(job, mem):
            continue
        need = job["mem_predicted_gb"]
        native = job.get("lang") == "native"
        held = native and guard_critical
        quick = bool(job.get("small")) and not strict and not native and need <= now_free - reserve
        if blocked is None:
            spare = mem["available_gb"] - cfg["headroom_gb"]
            alone = not any_local and not admitted and brake != "brake"
            # sam na Macu: bez rezerwy na dev serwer, a po 30 s czekania nawet ponad pamięć
            # (job bez trasy na Depot; nic innego niż on nie zwolni pamięci, pilnuje go SIGSTOP).
            # Przy „warn” bez tego ostatniego: macOS trzyma go tu godzinami przy połowie wolnej
            # pamięci, więc startuje to, co się mieści, ale nic ponad dostępną pamięć.
            overcommit = pressure != "warn" and not native and now - job["enqueued_at"] >= 30
            if not held and (need <= free or quick or (alone and (need <= spare or overcommit))):
                admitted[job["id"]] = ("fits", None)
                ahead.append((job["id"], need, job.get("predicted_wall_s"), measured(job)))
                free -= need
                now_free -= need
                any_local = True
                slot = slot or bool(job.get("exclusive"))
                continue
            blocked = job
            if now - job["enqueued_at"] > cfg["starve_s"]:
                reserve = need
                strict = now - job["enqueued_at"] > 2 * cfg["starve_s"]
            if not held:
                lone = (spare, overcommit) if brake != "brake" else None
                shade = shadow(state, need, free, now, ahead, lone)
                stuck = not head_fits_without_passers(state, job, free, now, ahead, lone)
            continue
        how = None
        if shade is not None and not native:
            how = backfill(job, free, now_free, shade, stuck, cfg)
        if how or (job.get("small") and not held and (need <= free - reserve or quick)):
            admitted[job["id"]] = ("overtake", blocked["id"])
            free -= need
            now_free -= need
            slot = slot or bool(job.get("exclusive"))
            if shade is not None and how != "ends" and shade["spare_gb"] is not None:
                shade["spare_gb"] -= need  # będzie biec obok głowy
    return admitted


# backfill: prognoza czasu to mediana historii, a czasy mają długi ogon (2026-10-09, 3000 biegów:
# 9-21% trwało ponad 2 × prognoza + 10 s, zależnie od długości), więc job wchodzi przed głowę
# z takim zapasem, a nie z samą prognozą
BACKFILL_SLACK = (2.0, 10.0)


def measured(job):
    """Czy prognoza joba pochodzi z jego własnych biegów (a nie z tabeli albo rodziny)."""
    return str(job.get("predicted_from") or "").startswith("history")


def backfill(job, free, now_free, shade, stuck, cfg):
    """Jak job wejdzie przed zablokowaną głowę, nie opóźniając jej startu, albo None.

    Gdy start głowy da się przewidzieć (shade["sure"]): "ends", jeśli job skończy się przed nim
    (z zapasem BACKFILL_SLACK), albo "beside", jeśli w tej chwili zmieści się obok niej. Gdy nie
    (czeka na job bez zmierzonej prognozy albo taki, który biegnie dłużej, niż miał): "short", jeśli
    job jest lekki (small_gb), jego prognoza z zapasem mieści się w head_delay_s, a głowa nie
    wystartowałaby nawet bez jobów, które ją już wyprzedziły (`stuck`). Opóźni ją wtedy najwyżej o
    swój czas, a gdy to wyprzedzający trzymają jej pamięć, nikt więcej nie wchodzi, więc strumień
    krótkich jobów jej nie zagłodzi. Ciężki krótki job (e2e na 5 GB) tu nie wchodzi: w replayu
    2026-10-09 to on, gdy przeciągnął się 27 razy, trzymał głowie najwięcej pamięci najdłużej.
    Zawsze z prognozą z własnej historii i w pamięci wolnej po rezerwach. Lekki job, który przed
    startem głowy się skończy ("ends", "short"), może wejść też w pamięci dostępnej teraz, jak mały
    job szybką ścieżką, także po 2 × starve_s: rezerwy na wzrost długich jobów i natywnego buildu
    spoza schedulera potrafią zepchnąć wolną pamięć poniżej zera przy kilkunastu GB dostępnych
    (2026-10-09 18:20: ruff i pytest po 0,1 GB stały wtedy za next build)."""
    need = job["mem_predicted_gb"]
    if not measured(job):
        return None
    light = need <= cfg["small_gb"]
    fits = need <= free
    fits_now = fits or (light and need <= now_free)
    slack = BACKFILL_SLACK[0] * (job.get("predicted_wall_s") or 0) + BACKFILL_SLACK[1]
    if shade["sure"]:
        if fits_now and slack <= shade["wait_s"]:
            return "ends"
        if fits and shade["spare_gb"] is not None and need <= shade["spare_gb"]:
            return "beside"
        return None
    if stuck and light and fits_now and slack <= cfg["head_delay_s"]:
        return "short"
    return None


def shadow(state, need, free, now, ahead=(), lone=None):
    """Kiedy job, który się nie mieści, zmieści się według prognoz lokalnych jobów, i ile miejsca
    zostanie wtedy obok niego. Biegnące joby kończą się w kolejności prognoz (najwcześniej za 5 s)
    i każdy zwalnia to, co trzyma, i swoją rezerwę na wzrost; `ahead` to joby wpuszczone w tym
    przebiegu planu, których nie ma jeszcze w `running`: (id, GB, s, zmierzona prognoza). `lone`:
    (dostępne minus zapas, wolno ponad pamięć) dla głowy, która może wystartować sama na Macu:
    gdy nie zmieści się nawet po końcu wszystkich, startuje po końcu ostatniego i nic obok niej.

    {"wait_s": sekundy albo None, gdy końce jobów jej nie wpuszczą, "spare_gb": miejsce obok
    (None: żadne), "after": id jobów, na których koniec czeka, "sure": start da się przewidzieć,
    bo każdy z tych jobów ma prognozę z własnej historii i jeszcze jej nie przekroczył}."""
    ends = []
    for i, r in enumerate(state["running"]):
        if r["where"] != "local":
            continue
        wall = r.get("predicted_wall_s") or 60
        left = wall - (now - r.get("started_at", now))
        held = r.get("mem_now_gb") or 0.0
        ends.append((max(5.0, left), i, r["id"], held + growth_left(r, now), held, measured(r) and left >= 0))
    for k, (jid, gb, wall, sure) in enumerate(ahead):
        ends.append((max(5.0, wall or 60), len(state["running"]) + k, jid, gb, 0.0, sure and bool(wall)))
    ends.sort(key=lambda e: (e[0], e[1]))
    out = {"wait_s": 0.0, "spare_gb": free - need, "after": [], "sure": True}
    if need <= free:
        return out
    released = 0.0
    for left, _i, jid, gb, held, sure in ends:
        free += gb
        released += held
        out["after"].append(jid)
        out["sure"] = out["sure"] and sure
        if need <= free:
            out.update(wait_s=left, spare_gb=free - need)
            return out
    if lone is not None and ends and (lone[1] or need <= lone[0] + released):
        out.update(wait_s=ends[-1][0], spare_gb=None)
        return out
    out.update(wait_s=None, spare_gb=None, sure=False)
    return out


def head_fits_without_passers(state, head, free, now, ahead, lone):
    """Czy głowa wystartowałaby teraz, gdyby nie joby, które ją wyprzedziły (`passed`): wtedy to one
    ją opóźniają i nikt więcej nie powinien wchodzić. Sama na Macu tylko wtedy, gdy poza nimi
    lokalnie nic nie biegnie i nic nie weszło w tym przebiegu."""
    need = head["mem_predicted_gb"]
    local = [r for r in state["running"] if r["where"] == "local"]
    passers = [r for r in local if r.get("passed") == head["id"]]
    gb = sum((r.get("mem_now_gb") or 0.0) + growth_left(r, now) for r in passers)
    if need <= free + gb:
        return True
    others = bool(ahead) or len(passers) < len(local)
    held = sum(r.get("mem_now_gb") or 0.0 for r in passers)
    return lone is not None and not others and (lone[1] or need <= lone[0] + held)


def blockers_eta(state, job):
    """(sekundy do chwili, gdy job się zmieści, id jobów, na których koniec czeka)."""
    s = shadow(state, job["mem_predicted_gb"], state["memory"]["free_for_admission_gb"], time.time())
    return (3600.0 if s["wait_s"] is None else s["wait_s"]), s["after"]


def decide_route(state, job, gb, wall, cfg, cache):
    """(route, target): minimalizuje czas do wyniku + λ × jednostki Depot."""
    mem = state["memory"]
    lam = cfg["lambda_s_per_unit"]
    base = {
        "lambda": lam,
        "local_eta_s": None,
        "depot_eta_s": None,
        "units": None,
        "cost_usd": None,
        "saves_s": None,
    }
    if gb <= mem["free_for_admission_gb"]:
        # mieści się teraz; ciężki job idzie na Depot tylko wtedy, gdy jest tam dużo szybszy,
        # niż kosztuje (cały internal/handlers: ~25 min lokalnie, 8 min na Depot za $0,19)
        target = depot_target(job, gb, wall, cfg, cache) if gb > cfg["small_gb"] else None
        if target and wall - target["eta_s"] > lam * target["units"]:
            saves = wall - target["eta_s"]
            route = dict(
                base,
                choice="depot",
                why="cost",
                local_eta_s=round(wall),
                depot_eta_s=target["eta_s"],
                units=target["units"],
                cost_usd=target["cost_usd"],
                saves_s=round(saves),
                text=f"Depot: saves {human_s(saves)} for ${target['cost_usd']:.2f}",
            )
            return route, target
        return dict(
            base,
            choice="local",
            why="fits",
            local_eta_s=round(wall),
            text="local, fits",
        ), None
    target = depot_target(job, gb, wall, cfg, cache)  # musi czekać: Depot liczy się dla każdego
    if gb > mem["idle_max_gb"]:
        if target:
            route = dict(
                base,
                choice="depot",
                why="cannot_fit",
                depot_eta_s=target["eta_s"],
                units=target["units"],
                cost_usd=target["cost_usd"],
                text=f"Depot: needs {gb:.0f} GB, Mac max ~{max(0, mem['idle_max_gb']):.0f}",
            )
            return route, target
        why_not = f"local only ({depot_blocker(job)})" if depot_blocker(job) else "local: no Depot route"
        text = f"{why_not}: needs {gb:.0f} GB, runs when the Mac is free"
        return dict(base, choice="local", why="no_depot", text=text), None
    wait, _ = blockers_eta(state, {"mem_predicted_gb": gb})
    local_eta = wait + wall
    if not target:
        text = (
            f"local only ({depot_blocker(job)}): waits {human_s(wait)}"
            if depot_blocker(job)
            else f"local: waits {human_s(wait)}, no Depot route"
        )
        return dict(
            base,
            choice="local",
            why="no_depot",
            local_eta_s=round(local_eta),
            text=text,
        ), None
    saves = local_eta - target["eta_s"]
    info = dict(
        base,
        local_eta_s=round(local_eta),
        depot_eta_s=target["eta_s"],
        units=target["units"],
        cost_usd=target["cost_usd"],
        saves_s=round(saves),
    )
    if saves > lam * target["units"]:
        return dict(
            info,
            choice="depot",
            why="cost",
            text=f"Depot: saves {human_s(saves)} for ${target['cost_usd']:.2f}",
        ), target
    text = f"local: waits {human_s(wait)}, Depot would cost ${target['cost_usd']:.2f}"
    return dict(info, choice="local", why="waits", text=text), None


def update_queue_view(state, cfg):
    """Pozycje, powody czekania i ETA startu w kolejce (dla karty)."""
    mem = state["memory"]
    labels = {j["id"]: j["label"] for j in state["running"] + state["queue"]}
    now = time.time()
    head_blocked = None
    owner = native_owner(state)
    native = mem.get("native") or {}
    for pos, job in enumerate(queue_order(state), start=1):
        job["position"] = pos
        job["waited_s"] = round(now - job["enqueued_at"], 1)
        wait, after = blockers_eta(state, job)
        job["eta_start_s"] = round(wait) if wait < 3600 else None
        free = max(0.0, mem["free_for_admission_gb"])
        waits_turn = False  # czeka na kolej, nie na pamięć: nie rezerwuje jej dla siebie
        if mem.get("pressure") == "critical":
            code, text = "pressure", "paused: memory pressure is critical"
        elif mem.get("brake") == "emergency":
            code, text = "pressure", "paused: the memory brake is freeing memory"
        elif job.get("lang") == "native" and mem.get("guard_level") == 2:
            code, text = "pressure", "paused: the dev server guard sees critical memory pressure"
        elif job.get("exclusive") and (owner or native.get("outside")):
            waits_turn, code = True, "native"
            if owner:
                left = (owner.get("predicted_wall_s") or 600) - (now - owner.get("started_at", now))
                job["eta_start_s"] = round(max(5.0, left))
                after = [owner["id"]]
                text = f"waiting: one native build at a time, {owner['label']} is building"
            else:
                job["eta_start_s"] = None
                text = (
                    "waiting: a native build outside the scheduler is running "
                    f"({native.get('build_gb', 0):.1f} GB, xcodebuild)"
                )
        elif sim_wait(job, mem):
            waits_turn, code = True, "simulators"
            sims = mem.get("simulators") or {}
            job["eta_start_s"] = None
            text = (
                f"waiting: {sims['agents_in_use']} agent simulators in use, cap {sims['cap']} "
                f"({', '.join(str(h) for h in sims.get('holders') or [])}); one frees when its "
                "session runs `portivo-mobile release` or ends"
            )
        elif (
            head_blocked is not None
            and now - head_blocked["enqueued_at"] > cfg["starve_s"]
            and job["mem_predicted_gb"] <= free
        ):
            # mieści się, ale plan go nie wpuścił: mógłby opóźnić start głowy (backfill)
            eta = head_blocked.get("eta_start_s")
            code, text = (
                "head",
                f"waiting: {head_blocked['label']} goes first"
                + (f" (starts in about {human_s(eta)})" if eta else "")
                + ", starting now could delay it",
            )
        else:
            code = "memory"
            text = f"waiting for {job['mem_predicted_gb']:.1f} GB, {free:.1f} free"
            names = ", ".join(labels.get(a, a) for a in after[:2])
            if names:
                text += f" · starts when {names} ends" + (
                    f", about {human_s(wait)}" if wait < 3600 else ""
                )
        job["reason"] = {
            "code": code,
            "need_gb": job["mem_predicted_gb"],
            "free_gb": mem["free_for_admission_gb"],
            "after": after,
            "text": text,
        }
        if head_blocked is None and not waits_turn:
            head_blocked = job


def safety(state, cfg):
    """SIGSTOP najmłodszego ciężkiego joba, gdy swap rośnie; SIGCONT, gdy pamięć odpuści."""
    import signal

    mem = state["memory"]
    growth = mem.get("swap_growth_2m_gb", 0)
    local = [
        j for j in state["running"] if j["where"] == "local" and j.get("child_pgid")
    ]
    paused = [j for j in local if j.get("paused")]
    swapping = (growth > cfg["pause_swap_gb"] and mem["level_pct"] < 25) or mem.get(
        "pressure"
    ) == "critical"
    if swapping:
        # natywny nie: jego kompilatory leżą zwykle poza grupą procesów joba (portivo-mobile buduje
        # w odczepionym procesie), a SIGSTOP samego czekającego wrappera nic nie zwalnia
        heavy = [
            j for j in local
            if not j.get("paused") and not j.get("small") and j.get("lang") != "native"
        ]
        if len(local) - len(paused) > 1 and heavy:
            victim = max(heavy, key=lambda j: j.get("started_at", 0))
            try:
                os.killpg(victim["child_pgid"], signal.SIGSTOP)
            except OSError:
                return
            victim["paused"] = True
            victim["paused_at"] = time.time()
            victim["pause_reason"] = f"swap +{growth:.1f} GB in 2 min"
            state["today"]["pauses"] += 1
    elif paused and growth < 0.1 and mem["level_pct"] >= 30:
        job = min(paused, key=lambda j: j.get("paused_at", 0))
        if time.time() - job.get("paused_at", 0) >= 30:
            try:
                os.killpg(job["child_pgid"], signal.SIGCONT)
            except OSError:
                pass
            job["paused"] = False
            job["pause_reason"] = None


# ---------- run ----------


def new_id():
    import random

    return f"j-{int(time.time())}-{random.randrange(16**4):04x}"


PORTIVO_LEASES = os.path.join(HOME, ".cache/portivo-mobile/leases")


def session_lease():
    """Czy sesja Claude nad tym procesem ma już symulator z portivo-mobile (dzierżawa
    leases/<udid>.json z jej pid): wtedy `up` nic nowego nie włączy. Sesję szuka jak
    portivo-mobile: pierwszy proces `claude` w górę drzewa."""
    pid, claude = os.getppid(), None
    for _ in range(40):
        if not pid or pid <= 1:
            break
        path = proc_path(pid)
        if proc_name(pid) == "claude" or os.path.basename(path) == "claude" or "/claude/versions/" in path:
            claude = pid
            break
        pid = proc_bsd(pid, BSD_PPID)
    if not claude:
        return False
    try:
        names = os.listdir(PORTIVO_LEASES)
    except OSError:
        return False
    for name in names:
        try:
            with open(os.path.join(PORTIVO_LEASES, name)) as f:
                owner = json.load(f).get("owner") or {}
        except (OSError, ValueError, AttributeError):
            continue
        if owner.get("pid") == claude:
            return True
    return False


def agent_info(session, name):
    return {
        "session": (session or "")[:8] or None,
        "name": name,
        "worktree": os.getcwd().replace(HOME, "~", 1),
        "pane": os.environ.get("ORCA_PANE_KEY"),
    }


def goflags_with(p, extra=()):
    """GOFLAGS z env albo `go env` (maszynowe -p=4), z naszym -p i dodatkami."""
    base = os.environ.get("GOFLAGS")
    if base is None:
        import subprocess

        try:
            base = subprocess.run(
                ["go", "env", "GOFLAGS"], capture_output=True, text=True, timeout=10
            ).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            base = ""
    parts = [x for x in base.split() if not (p and re.match(r"^-p=?\d*$", x))]
    if p:
        parts.append(f"-p={p}")
    parts.extend(x for x in extra if x not in parts)
    return " ".join(parts)


def parse_run_args(args):
    opts, command, argv = {}, None, None
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            argv = args[i + 1 :]
            break
        if a == "--shell" and i + 1 < len(args):
            command = args[i + 1]
            i += 2
            continue
        if a in ("--timeout", "--session", "--agent", "--via") and i + 1 < len(args):
            opts[a[2:]] = args[i + 1]
            i += 2
            continue
        i += 1
    return opts, command, argv


def shell_argv(command):
    shell = os.environ.get("SHELL") or "/bin/zsh"
    if os.path.basename(shell) not in ("zsh", "bash", "sh"):
        shell = "/bin/zsh"
    return [shell, "-c", command]


def exec_plain(command, argv):
    target = shell_argv(command) if command is not None else argv
    if not target:
        return 2
    os.execvp(target[0], target)
    return 127


def new_entry(job, command, argv, opts):
    return {
        "id": new_id(),
        "class": job["class"],
        "kind": job["kind"],
        "label": job["label"],
        "module": job["module"],
        "repo": job["repo"],
        "scope": job["scope"],
        "compile_only": job["compile"],
        "filtered": job.get("filtered", False),
        "module_name": job["module_name"],
        "lang": job.get("lang"),
        "cmd": command
        if command is not None
        else " ".join(shlex.quote(a) for a in argv),
        "agent": agent_info(opts.get("session"), opts.get("agent")),
        "where": None,
        "route": None,
        "small": False,
        "count1_dropped": False,
        "exclusive": bool(job.get("exclusive")),
        "native_tool": job.get("tool") if job.get("lang") == "native" else None,
        "pid": os.getpid(),
        "via": opts.get("via", "cli"),
        "enqueued_at": time.time(),
    }


def cmd_run(args):
    opts, command, argv = parse_run_args(args)
    if command is None and not argv:
        print(__doc__)
        return 2
    if os.environ.get(NESTED_ENV):
        # w środku wpuszczonego joba (skrypt, który sam woła `sched run`): jego pamięć liczy się już
        # w drzewie zewnętrznego joba, a drugie czekanie na nią mogłoby czekać na samego siebie
        return exec_plain(command, argv)
    cfg = load_config()
    cwd = os.getcwd()
    job = classify(command, cwd, argv=argv)
    if job is None and argv and opts.get("via") == "plock":
        job = opaque_job(argv, cwd)
    if job is None:
        return exec_plain(command, argv)
    if job.get("lang") == "generic":
        # cache to tylko Go (testy, Depot); historia tylko tej rodziny
        history = read_history(needle='"class": "' + job["family"])
        return schedule(new_entry(job, command, argv, opts), job, command, argv, opts, cfg, history, {})
    history = read_history()
    cache = load_cache()
    entry = new_entry(job, command, argv, opts)
    if (
        cfg["drop_count1"]
        and command
        and opts.get("via") == "hook"
        and job["count1"]
        and count1_safe(job, cache)
    ):
        dropped = drop_count1(command)
        if dropped:
            command = dropped
            entry["cmd"] = command
            entry["count1_dropped"] = True
            log(
                "bez -count=1: testy nie uruchamiają innych programów, wynik może przyjść z cache testów"
            )
    if job["kind"] == "test" and not job.get("lang") and os.path.isfile(os.path.join(job["repo_dir"], "scripts/depot-exec.sh")):
        job["uses_pg"] = uses_pg(job, cache)
    if job.get("lang") == "native" and job.get("tool") == "portivo-mobile":
        entry["sim_lease"] = session_lease()
    if likely_heavy(job) and not job.get("lang"):
        refresh_depot_eta(cache, job["repo_dir"])
    save_cache(cache)
    return schedule(entry, job, command, argv, opts, cfg, history, cache)


def schedule(entry, job, command, argv, opts, cfg, history, cache):
    jid = entry["id"]
    timeout = float(opts.get("timeout") or 0)
    start = time.time()
    target = None
    with Locked():
        state = load_state(cfg)
        reap(state)
        refresh_memory(state, cfg)
        p, p_by, gb, wall, src = choose_p(
            job, state["memory"]["free_for_admission_gb"], history
        )
        entry.update(
            p=p,
            p_by=p_by,
            mem_predicted_gb=gb,
            predicted_wall_s=wall,
            predicted_from=src,
        )
        entry["small"] = gb <= cfg["small_gb"] and wall <= cfg["small_wall_s"]
        entry["route"], target = decide_route(state, job, gb, wall, cfg, cache)
        entry["routed_at"] = time.time()
        state["queue"].append(entry)
        if target and entry["route"]["choice"] == "depot":
            start_depot(state, entry, target)
        else:
            admitted = plan(state, cfg, time.time())
            if jid in admitted:
                start_local(state, entry, admitted[jid])
        update_queue_view(state, cfg)
        save_state(state)
    if entry.get("where") is None:
        log(
            f"czeka: {entry.get('reason', {}).get('text') or 'na pamięć'} · {entry['route']['text']}"
        )
        if job.get("lang") == "native":
            log(
                f"natywny build albo symulator (~{pl_gb(entry['mem_predicted_gb'])}) wystartuje sam, "
                "gdy zmieści się w pamięci; nie przerywaj go. Kolejka: claude-acc sched status"
            )
        while entry.get("where") is None:
            time.sleep(0.5)
            if timeout and time.time() - start > timeout:
                with Locked():
                    state = load_state(cfg)
                    state["queue"] = [j for j in state["queue"] if j["id"] != jid]
                    save_state(state)
                log(f"timeout {timeout:.0f}s w kolejce")
                return EXIT_TIMEOUT
            with Locked():
                state = load_state(cfg)
                reap(state)
                refresh_memory(state, cfg)
                me = next((j for j in state["queue"] if j["id"] == jid), None)
                if me is None:
                    me = entry
                    state["queue"].append(me)
                if time.time() - me.get("routed_at", 0) > 15:
                    me["route"], target = decide_route(
                        state,
                        job,
                        me["mem_predicted_gb"],
                        me["predicted_wall_s"],
                        cfg,
                        cache,
                    )
                    me["routed_at"] = time.time()
                    if target and me["route"]["choice"] == "depot":
                        start_depot(state, me, target)
                if me.get("where") is None:
                    admitted = plan(state, cfg, time.time())
                    if jid in admitted:
                        if me.get("p_by") == "scheduler":
                            p, _, gb, wall, src = choose_p(
                                job, state["memory"]["free_for_admission_gb"], history
                            )
                            if gb <= max(
                                state["memory"]["free_for_admission_gb"],
                                me["mem_predicted_gb"],
                            ):
                                me.update(
                                    p=p,
                                    mem_predicted_gb=gb,
                                    predicted_wall_s=wall,
                                    predicted_from=src,
                                )
                        start_local(state, me, admitted[jid])
                update_queue_view(state, cfg)
                save_state(state)
                entry = me
        log(
            f"start po {human_s(time.time() - start)} czekania, {entry['where']}"
            + (f", -p {entry['p']}" if entry.get("p") else "")
        )
    if entry["where"] == "depot":
        return run_depot(entry, target, cfg, job, command, argv, opts, history, cache)
    return run_local(entry, job, command, argv, cfg)


def start_local(state, entry, why):
    reason, passed = why
    now = time.time()
    state["queue"] = [j for j in state["queue"] if j["id"] != entry["id"]]
    entry["where"] = "local"
    entry["started_at"] = now
    entry["waited_s"] = round(now - entry["enqueued_at"], 1)
    entry.update(
        elapsed_s=0.0,
        eta_s=entry["predicted_wall_s"],
        progress=0.0,
        mem_now_gb=0.0,
        mem_peak_gb=0.0,
        cpu_cores=None,
        paused=False,
        pause_reason=None,
        depot=None,
    )
    for key in ("position", "eta_start_s", "reason"):
        entry.pop(key, None)
    state["running"].append(entry)
    if reason == "overtake":
        entry["passed"] = passed  # plan: gdy to wyprzedzający trzymają pamięć głowy, nikt więcej
        passed_job = next((j for j in state["queue"] if j["id"] == passed), {})
        state["overtakes"] = (
            state["overtakes"]
            + [
                {
                    "at": now,
                    "id": entry["id"],
                    "label": entry["label"],
                    "passed": passed,
                    "passed_label": passed_job.get("label"),
                    "mem_gb": entry["mem_predicted_gb"],
                    "wall_s": None,
                }
            ]
        )[-10:]
        state["today"]["overtakes"] += 1


def start_depot(state, entry, target):
    now = time.time()
    state["queue"] = [j for j in state["queue"] if j["id"] != entry["id"]]
    entry["where"] = "depot"
    entry["started_at"] = now
    entry["waited_s"] = round(now - entry["enqueued_at"], 1)
    entry["p"] = target["cores"]
    entry["p_by"] = "depot"
    entry["local_wall_s"] = entry.get("predicted_wall_s")
    entry["predicted_wall_s"] = target["eta_s"]
    entry.update(
        elapsed_s=0.0,
        eta_s=target["eta_s"],
        progress=0.0,
        mem_now_gb=0.0,
        mem_peak_gb=0.0,
        cpu_cores=None,
        paused=False,
        pause_reason=None,
        depot={
            "target": target["target"],
            "job": target["job"],
            "cores": target["cores"],
            "run_id": None,
            "url": None,
            "units": 0.0,
            "cost_usd": 0.0,
        },
    )
    for key in ("position", "eta_start_s", "reason"):
        entry.pop(key, None)
    state["running"].append(entry)


def run_local(entry, job, command, argv, cfg):
    import signal
    import subprocess

    env = dict(os.environ)
    extra = []
    if (
        cfg["ldflags_w_for_build"]
        and job["kind"] == "build"
        and "-o" not in job["flags"]
        and "-ldflags" not in job["flags"]
        and job["scope"] in ("tree", "subtree", "pkgs")
    ):
        extra.append(
            "-ldflags=-w"
        )  # binarki i tak są wyrzucane; bez DWARF nie ma dsymutil
    ours_p = entry.get("p") if entry.get("p_by") == "scheduler" else None
    if ours_p or extra:
        env["GOFLAGS"] = goflags_with(ours_p, extra)
    target = shell_argv(command) if command is not None else argv
    env[NESTED_ENV] = entry["id"]
    try:
        child = subprocess.Popen(target, env=env, preexec_fn=os.setpgrp)
    except OSError as err:
        log(f"nie da się uruchomić: {err}")
        finish(entry["id"], cfg, 127, 0.0, 0.0, None)
        return 127
    jid = entry["id"]
    with Locked():
        state = load_state(cfg)
        me = next((j for j in state["running"] if j["id"] == jid), None)
        if me is not None:
            me["child_pgid"] = child.pid
            save_state(state)

    def forward(sig, _frame):
        for s in (signal.SIGCONT, sig):
            try:
                os.killpg(child.pid, s)
            except OSError:
                pass

    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, forward)
    started = time.time()
    peak, cpu_live, last_beat = 0.0, 0.0, 0.0
    # natywny build: do pamięci joba dochodzą drzewa xcodebuild na Macu (jeden build naraz, więc
    # to jego) i symulator, który `portivo-mobile up` włączył; oba poza drzewem procesów joba
    native = job.get("lang") == "native" and job.get("exclusive")
    sims_since = started if job.get("tool") == "portivo-mobile" else None
    pool, pool_at, built = None, 0.0, False
    while True:
        pid, status, rusage = os.wait4(child.pid, os.WNOHANG)
        if pid == child.pid:
            break
        own = job_pids(child.pid)
        if native and time.time() - pool_at >= 2.0:
            pool, pool_at = native_scan(sims_since, builds=not built), time.time()
        if pool and pool["pids"]:
            now_gb, cpu_live = pids_usage(own | pool["pids"])
        else:
            now_gb, cpu_live = pids_usage(own)
            now_gb += pool["gb"] if pool else 0.0
        peak = max(peak, now_gb)
        if time.time() - last_beat >= 1.0:
            last_beat = time.time()
            built = heartbeat(jid, cfg, now_gb, peak, cpu_live, started, native=pool) or built
        # krótka komenda nie czeka ćwierć sekundy na własny koniec: gęsto na początku, potem rzadziej
        ran = time.time() - started
        time.sleep(0.01 if ran < 0.5 else 0.05 if ran < 3 else 0.25)
    rc = os.waitstatus_to_exitcode(status)
    cpu = rusage.ru_utime + rusage.ru_stime if rusage else cpu_live
    finish(jid, cfg, rc, time.time() - started, peak, cpu)
    return rc if rc >= 0 else 128 - rc


def heartbeat(jid, cfg, now_gb, peak, cpu, started, native=None):
    """Pomiar biegnącego joba do stanu; True, gdy natywny job skończył fazę buildu."""
    with Locked(block=False) as lk:
        if not lk.ok:
            return
        state = load_state(cfg)
        me = next((j for j in state["running"] if j["id"] == jid), None)
        if me is None:
            return
        if native is not None and me.get("exclusive"):
            track_native(me, native, time.time(), state["_internal"], now_gb)
        elapsed = time.time() - started
        wall = me.get("predicted_wall_s") or 60
        me.update(
            elapsed_s=round(elapsed, 1),
            mem_now_gb=round(now_gb, 2),
            mem_peak_gb=round(peak, 2),
            cpu_cores=round(cpu / elapsed, 1) if elapsed > 1 else None,
            eta_s=round(max(0.0, wall - elapsed), 1),
            progress=round(min(0.99, elapsed / wall), 2),
        )
        reap(state)
        refresh_memory(state, cfg)
        safety(state, cfg)
        update_queue_view(state, cfg)
        save_state(state)
        # run_local po fazie buildu przestaje doliczać cudze drzewa xcodebuild
        return bool(me.get("native_done"))


def old_lock(state, me, wall, waited):
    """Bilans dnia: ile czekałyby joby Go przy dawnym `plock go` (jeden job Go naraz, w kolejności
    przyjścia, z prawdziwymi czasami biegów) i ile czekały naprawdę. Skończony job, przed którym
    nie ma już w kolejce ani w biegu starszego joba Go, wchodzi do sumy na stałe; młodsze czekają w
    `pending`, bo starszy, gdy się skończy, stanie w zamku przed nimi."""
    today, lock = state["today"], state["_internal"]["old_lock"]
    today["go_wait_s"] = round(today.get("go_wait_s", 0.0) + waited, 1)
    pending = sorted(lock["pending"] + [[round(me["enqueued_at"], 1), round(wall, 1)]])
    horizon = min(
        (j["enqueued_at"] for j in state["running"] + state["queue"] if not j.get("lang") and j.get("where") != "depot"),
        default=float("inf"),
    )
    wait, free_at, done = lock["wait_s"], lock["free_at"], 0
    for enq, w in pending:
        start = max(enq, free_at)
        wait, free_at = wait + start - enq, start + w
        if enq < horizon:
            lock.update(wait_s=round(wait, 1), free_at=free_at)
            done += 1
    lock["pending"] = pending[done:]
    today["old_lock_wait_s"] = round(wait, 1)


def old_hook_depot(entry):
    """Czy stary hak depot-heavy-go.sh wysłałby tę klasę na Depot (job i koszt)."""
    comp = "filtered" if entry.get("filtered") else entry.get("compile_only", False)
    key = (entry.get("module_name"), entry.get("kind"), entry.get("scope"), comp)
    return DEPOT_CI_JOBS.get(key)


def finish(jid, cfg, rc, wall, peak, cpu, depot_info=None):
    with Locked():
        state = load_state(cfg)
        me = next((j for j in state["running"] if j["id"] == jid), None)
        state["running"] = [j for j in state["running"] if j["id"] != jid]
        if me is None:
            save_state(state)
            return
        today = state["today"]
        internal = state["_internal"]
        where = me["where"]
        waited = me.get("waited_s") or 0.0
        if me.get("native_seen"):
            # szczyt z drzewami xcodebuild: z tego scheduler przewiduje każdy natywny build
            internal["native_peaks"] = (internal.get("native_peaks", []) + [round(peak, 2)])[-10:]
        if me.get("native_done_at") and me.get("started_at"):
            # `expo run:ios` zostaje z Metro: czas buildu, a nie czas życia Metro
            wall = min(wall, me["native_done_at"] - me["started_at"])
        today["wait_s"] = round(today["wait_s"] + waited, 1)
        units = cost = None
        if where == "local":
            today["jobs_local"] += 1
            if not me.get("lang"):  # Go: tylko te szły kiedyś przez `plock go`
                old_lock(state, me, wall, waited)
            ci = old_hook_depot(me)
            if ci:
                _, cores, eta = ci
                kept = cores / 2 * (eta + DEPOT_SETUP_S) / 60 * cfg["unit_usd"]
                today["local_kept_usd"] = round(today["local_kept_usd"] + kept, 2)
        else:
            today["jobs_depot"] += 1
            units = (depot_info or {}).get("units") or me["route"].get("units") or 0.0
            cost = round(units * cfg["unit_usd"], 3)
            today["depot_units"] = round(today["depot_units"] + units, 1)
            today["depot_cost_usd"] = round(today["depot_cost_usd"] + cost, 2)
        today["wait_saved_s"] = round(today["old_lock_wait_s"] - today.get("go_wait_s", 0.0), 1)
        for o in state["overtakes"]:
            if o["id"] == jid:
                o["wall_s"] = round(wall, 1)
        state["recent"] = (
            [
                {
                    "id": jid,
                    "label": me["label"],
                    "where": where,
                    "rc": rc,
                    "finished_at": time.time(),
                    "wall_s": round(wall, 1),
                    "peak_gb": round(peak, 2) if where == "local" else None,
                    "predicted_gb": me["mem_predicted_gb"],
                    "waited_s": waited,
                    "cost_usd": cost,
                    "route_text": me["route"]["text"],
                    "depot_run_id": (depot_info or {}).get("run_id"),
                }
            ]
            + state["recent"]
        )[:8]
        agent = me.get("agent") or {}
        row = {
            "ts": time.time(),
            "id": jid,
            "where": where,
            "class": me["class"],
            "module": me["module"],
            "repo": me["repo"],
            "label": me["label"],
            "p": me.get("p"),
            "peak_gb": round(peak, 2) if where == "local" else None,
            "sys_drop_gb": None,
            "wall_s": round(wall, 1),
            "cpu_s": round(cpu, 1) if cpu else None,
            "wait_s": waited,
            "rc": rc,
            "agent": agent.get("name"),
            "worktree": agent.get("worktree"),
            "depot_run_id": (depot_info or {}).get("run_id"),
            "runner": f"{me['p']}c" if where == "depot" else None,
            "units": units,
            "cost_usd": cost,
            "choice": me["route"]["choice"],
            "why": me["route"]["why"],
            "local_eta_s": me["route"].get("local_eta_s"),
            "depot_eta_s": me["route"].get("depot_eta_s"),
            "lambda": me["route"].get("lambda"),
            "predicted_gb": me["mem_predicted_gb"],
            "predicted_wall_s": me.get("local_wall_s") or me.get("predicted_wall_s"),
            "count1_dropped": me.get("count1_dropped", False),
            "lang": me.get("lang"),
        }
        if me.get("lang") == "native":
            row["native_built"] = bool(me.get("native_seen"))
        with open(HISTORY_PATH, "a") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        refresh_memory(state, cfg)
        update_queue_view(state, cfg)
        save_state(state)


DEPOT_LINE = r"run (\w+): exit (-?\d+) after (\d+)s on (\d+) cores \(~([\d.]+) units\)"


def run_depot(entry, target, cfg, job, command, argv, opts, history, cache):
    import signal
    import subprocess
    import threading

    jid = entry["id"]
    log(
        f"Depot ({target['target']} {target['job']}, {target['cores']} rdzeni): {entry['route']['text']}"
    )
    started = time.time()
    try:
        proc = subprocess.Popen(
            target["argv"],
            cwd=target["cwd"],
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
    except OSError as err:
        log(f"Depot nie wystartował ({err}): uruchamiam lokalnie")
        finish(jid, cfg, EXIT_DEPOT_NEVER_RAN, 0.0, 0.0, None, {"units": 0.0})
        return requeue_local(entry, job, command, argv, opts, cfg, history, cache)
    info = {"run_id": None, "units": None}

    def pump():
        for line in proc.stderr:
            sys.stderr.write(line)
            sys.stderr.flush()
            m = re.search(DEPOT_LINE, line)
            if m:
                info["run_id"], info["units"] = m.group(1), float(m.group(5))
            elif not info["run_id"]:
                m2 = re.search(r"\brun[ =:]+([a-z0-9]{8,})\b", line)
                if m2:
                    info["run_id"] = m2.group(1)

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()

    def forward(sig, _frame):
        try:
            proc.send_signal(sig)
        except OSError:
            pass

    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, forward)
    last = 0.0
    while proc.poll() is None:
        time.sleep(0.5)
        if time.time() - last < 2:
            continue
        last = time.time()
        with Locked(block=False) as lk:
            if not lk.ok:
                continue
            state = load_state(cfg)
            me = next((j for j in state["running"] if j["id"] == jid), None)
            if me:
                elapsed = time.time() - started
                live = target["cores"] / 2 * elapsed / 60
                me.update(
                    elapsed_s=round(elapsed, 1),
                    eta_s=round(max(0.0, me["predicted_wall_s"] - elapsed), 1),
                    progress=round(
                        min(0.99, elapsed / max(me["predicted_wall_s"], 1)), 2
                    ),
                )
                me["depot"].update(
                    run_id=info["run_id"],
                    units=round(live, 1),
                    cost_usd=round(live * cfg["unit_usd"], 2),
                )
                refresh_memory(state, cfg)
                update_queue_view(state, cfg)
                save_state(state)
    reader.join(5)
    rc = proc.returncode
    wall = time.time() - started
    if info["units"] is None:
        info["units"] = round(target["cores"] / 2 * wall / 60, 1)
    finish(jid, cfg, rc, wall, 0.0, None, info)
    if rc == EXIT_DEPOT_NEVER_RAN and target["target"] == "depot-exec":
        log("Depot nie uruchomił komendy (125): uruchamiam lokalnie")
        return requeue_local(entry, job, command, argv, opts, cfg, history, cache)
    return rc


def requeue_local(entry, job, command, argv, opts, cfg, history, cache):
    """Po nieudanym starcie na Depot: ten sam job jeszcze raz, tylko lokalnie."""
    fresh = new_entry(job, command, argv, opts)
    fresh["count1_dropped"] = entry.get("count1_dropped", False)
    no_depot = dict(cfg)
    cache = dict(cache)
    job = dict(job, simple=False, multi=True)  # bez trasy Depot
    return schedule(fresh, job, command, argv, opts, no_depot, history, cache)


# ---------- hook i status ----------


def which(name):
    """shutil.which bez importu shutil, który na Pythonie 3.9 ciągnie bz2 i threading: hook
    pyta o rtk przy każdej owijanej komendzie."""
    for folder in os.get_exec_path():
        path = os.path.join(folder, name)
        if os.access(path, os.X_OK) and not os.path.isdir(path):
            return path
    return None


RECURSIVE_GREP = re.compile(r"(^|[\s;&|(])grep\s+(-[A-Za-z]*[rR][A-Za-z]*|--recursive)")
GIT_GREP = re.compile(
    r"((?:^|[;&|(`{\n])\s*(?:(?:do|then|else|!)\s+)*(?:[A-Za-z_]\w*=\S*\s+)*(?:(?:rtk\s+proxy|time|command|nice)\s+)*"
    r"(?:\S*/)?git(?:\s+-[Cc]\s+\S+)*\s+grep)(?=\s|$)"
)
GREP_BINARY_FLAG = re.compile(r"(^|\s)(-a|--text|--binary-files\S*|-I|-[A-Za-z]*I[A-Za-z]*)(?=\s|$)")


def git_grep_text_only(command):
    """`git grep` bez flagi o plikach binarnych dostaje -I (pomija binarki).

    2026-10-08 `git grep -nE ... <commit>` agenta w repo z 368 MB filmów i obrazów w historii
    urósł do 10 GB w 2 sekundy: wyrażenie -E idzie przez regex macOS, a binarka to jedna linia
    długości megabajtów. Z -I ta sama komenda ma szczyt 270 MB, a agent i tak nie szuka w mp4."""
    out, pos = [], 0
    for m in GIT_GREP.finditer(command):
        rest = command[m.end() :]
        stop = re.search(r"[;&|\n]", rest)
        segment = rest[: stop.start()] if stop else rest
        if GREP_BINARY_FLAG.search(segment):
            continue
        out.append(command[pos : m.end()] + " -I")
        pos = m.end()
    out.append(command[pos:])
    return "".join(out)

def with_rtk(command):
    """Komenda tak, jak przepisałby ją hook rtk bez naszych wyjątków: komendy schedulera są w jego
    exclude_commands (dwa hooki z updatedInput na tej samej komendzie dają losowy wynik), więc
    `rtk` dokładamy tu, w środku opakowania. `rtk rewrite` to jedno źródło jego reguł; config
    z wyjątkami czyta z HOME, więc pytamy go z pustym HOME. Kod 3 („przepisz, ale zapytaj”,
    bo tam nie widzi ustawień Claude) to dla nas zwykłe przepisanie."""
    rtk = which("rtk")
    if not rtk:
        return command
    import subprocess  # dopiero z rtk: bez niego hook nie płaci za ten import

    home = os.path.join(STATE_DIR, "rtk-home")
    try:
        os.makedirs(home, exist_ok=True)
        r = subprocess.run(
            [rtk, "rewrite", command],
            capture_output=True,
            text=True,
            timeout=3,
            env=dict(os.environ, HOME=home, RTK_TELEMETRY_DISABLED="1"),
        )
    except (OSError, subprocess.SubprocessError):
        return command
    out = (r.stdout or "").strip()
    return out if r.returncode in (0, 3) and out else command


def runner():
    """Początek owiniętej komendy: python z setup.sh i acc.py, które uruchamia ten plik z
    bajtkodu (start z samego pliku to 12 ms kompilacji przy każdej komendzie Go), albo
    systemowy python i sam plik, gdy instalacja jest starsza."""
    python = os.path.join(STATE_DIR, "python")
    launcher = os.path.join(STATE_DIR, "acc.py")
    if (
        os.path.dirname(SELF) == STATE_DIR
        and os.access(python, os.X_OK)
        and os.path.isfile(launcher)
    ):
        return [python, launcher, "sched"]
    return ["/usr/bin/python3", SELF]


def hook_rewrite(event):
    """updatedInput dla PreToolUse Bash: cały tool_input z komendą owiniętą w sched.py run."""
    if event.get("tool_name") != "Bash":
        return None
    tool_input = event.get("tool_input") or {}
    command = tool_input.get("command") or ""
    if not command.strip():
        return None
    if RECURSIVE_GREP.search(command):
        return None  # tę komendę przepisuje rg-rewrite.sh; dwa updatedInput to wynik losowy
    fixed = git_grep_text_only(command) if "grep" in command else command
    try:
        job = classify(fixed, event.get("cwd") or os.getcwd())
    except Exception:  # hook nigdy nie blokuje agenta przez własny błąd
        job = None
    if job is None:
        if fixed == command:
            return None
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "updatedInput": dict(tool_input, command=with_rtk(fixed)),
                "additionalContext": (
                    "claude-acc: added -I to `git grep` (skip binary files); a regex over committed "
                    "binaries once grew git to 10 GB in two seconds. Pass -a or --text to search them."
                ),
            }
        }
    command = fixed
    parts = runner() + ["run", "--via", "hook"]
    if event.get("session_id"):
        parts += ["--session", str(event["session_id"])]
    agent = event.get("agent_type") or event.get("subagent_type")
    if agent:
        parts += ["--agent", str(agent)]
    updated = dict(tool_input)
    updated["command"] = (
        " ".join(shlex.quote(p) for p in parts) + " --shell " + shlex.quote(with_rtk(command))
    )
    if job.get("lang") == "native":
        # kolejka natywnych potrafi czekać minutami, dłużej niż domyślny timeout Basha
        how = (
            f"It waits until about {job['native_prior'][0]:g} GB fit in memory (native builds run "
            "one at a time on this Mac, a simulator start also waits for a free simulator slot) and "
            "then starts by itself, which can take minutes: run it with run_in_background and do "
            "not kill it while it waits"
        )
    else:
        how = "It may wait for memory, pick -p or run on Depot"
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "updatedInput": updated,
            "additionalContext": (
                f"claude-acc sched: `{job['label']}` runs through the memory scheduler. {how}; "
                "the output and exit code are the command's own."
            ),
        }
    }


def public_state(state):
    return {k: v for k, v in state.items() if not k.startswith("_")}


# ---------- Depot CI: biegi organizacji dla karty Builds ----------

# Scheduler widzi tylko joby, które sam wysłał na Depot; bramka pushu i scripts/depot-ci.sh albo
# depot-exec.sh odpalone przez agenta omijają go (SKIP_MARKERS), więc karta bierze je z API Depot.
DEPOT_STATUSES = ("queued", "running", "finished", "failed", "cancelled")
DEPOT_FINAL = {"finished": 0, "failed": 1, "cancelled": 130}
DEPOT_LIST_N = 15
DEPOT_RECENT_N = 6


def depot_cli():
    """CLI Depot; aplikacja startuje z ubogim PATH, więc sprawdzamy też Homebrew."""
    import shutil

    found = shutil.which("depot")
    if found:
        return found
    paths = ("/opt/homebrew/bin/depot", "/usr/local/bin/depot")
    return next((p for p in paths if os.access(p, os.X_OK)), None)


def depot_call(cli, args, org):
    """(dane, None) z `depot ARGS -o json` albo (None, ostatnia linia błędu)."""
    import subprocess

    cmd = [cli] + args + (["--org", org] if org else []) + ["-o", "json"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired) as err:
        return None, str(err)
    if r.returncode != 0:
        lines = [x.strip() for x in (r.stderr or r.stdout).splitlines() if x.strip()]
        return None, lines[-1] if lines else f"depot exit {r.returncode}"
    try:
        return json.loads(r.stdout), None
    except ValueError:
        return None, "depot returned unreadable output"


def iso_epoch(text):
    """2026-10-05T22:15:56.626Z -> epoch (UTC); None, gdy pola nie ma."""
    import calendar

    m = re.match(r"(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d)(\.\d+)?", text or "")
    if not m:
        return None
    whole = calendar.timegm(tuple(int(x) for x in m.groups()[:6]) + (0, 0, 0))
    return whole + float(m.group(7) or 0)


def depot_detail(cli, run, org):
    """Nazwy workflow i jobów, link do joba (najpierw czerwonego) i czasy jednego biegu.

    `ci status` niesie nazwy, statusy i view_url, `ci metrics --run` czasy startu i końca;
    bieg skończony pytamy o oba raz, potem leży w depot.json (`final`)."""
    rid = run["run_id"]
    final = run.get("status") in DEPOT_FINAL
    status, _ = depot_call(cli, ["ci", "status", rid], org)
    if status is None:
        return None
    metrics = (
        depot_call(cli, ["ci", "metrics", "--run", rid], org)[0] if final else None
    )
    names, urls, failed_urls, done, total = [], [], [], 0, 0
    for wf in status.get("workflows") or []:
        jobs = wf.get("jobs") or []
        job_names = [j.get("job_display_name") or "?" for j in jobs]
        names.append(
            wf.get("name", "?") + (" · " + ", ".join(job_names) if job_names else "")
        )
        for j in jobs:
            total += 1
            done += j.get("status") in DEPOT_FINAL
            url = ((j.get("attempts") or [{}])[-1]).get("view_url")
            if url:
                (failed_urls if j.get("status") == "failed" else urls).append(url)
    times = (metrics or {}).get("run") or {}
    return {
        "final": final,
        "label": " + ".join(names) or f"Depot CI {rid}",
        "url": (failed_urls + urls or [None])[0],
        "jobs_done": done,
        "jobs_total": total,
        "started_at": iso_epoch(times.get("started_at")),
        "finished_at": iso_epoch(times.get("finished_at")),
    }


def sched_depot_run_ids():
    """Biegi, które scheduler sam wysłał na Depot: karta pokazuje je jako jego joby."""
    try:
        with open(STATE_PATH) as f:
            state = json.load(f)
    except (OSError, ValueError):
        return set()
    ids = {(j.get("depot") or {}).get("run_id") for j in state.get("running") or []}
    ids |= {r.get("depot_run_id") for r in state.get("recent") or []}
    return ids - {None}


def load_depot():
    try:
        with open(DEPOT_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_depot(data):
    os.makedirs(SCHED_DIR, exist_ok=True)
    tmp = f"{DEPOT_PATH}.tmp{os.getpid()}"
    with open(tmp, "w") as f:
        f.write(json.dumps(data, ensure_ascii=False, indent=1))
    os.replace(tmp, DEPOT_PATH)


def sync_depot(prev, cfg, now=None):
    """Nowa zawartość depot.json: running w kształcie joba schedulera, recent w kształcie
    jego `recent` (karta rysuje je tymi samymi wierszami), błąd CLI w `error`."""
    from concurrent.futures import ThreadPoolExecutor

    now = now or time.time()
    out = {
        "version": VERSION,
        "checked_at": now,
        "error": None,
        "running": [],
        "recent": prev.get("recent") or [],
        "_runs": prev.get("_runs") or {},
    }
    cli = depot_cli()
    if not cli:
        out["error"] = "no depot CLI (brew install depot/tap/depot)"
        return out
    org = cfg.get("depot_org") or None
    args = ["ci", "run", "list", "-n", str(DEPOT_LIST_N)]
    for status in DEPOT_STATUSES:
        args += ["--status", status]
    runs, err = depot_call(cli, args, org)
    if runs is None:
        out["error"] = err
        return out
    runs = [r for r in runs if r.get("run_id")]
    cached = out["_runs"]
    need = [r for r in runs if not (cached.get(r["run_id"]) or {}).get("final")]
    with ThreadPoolExecutor(max_workers=8) as pool:
        fetched = dict(
            zip(
                [r["run_id"] for r in need],
                pool.map(lambda r: depot_detail(cli, r, org), need),
            )
        )
    # najpierw wszystkie szczegóły: lista idzie od najnowszego, a ETA biegnącego bierze się
    # ze starszych zielonych biegów tej samej etykiety
    runs_out = {}
    for r in runs:
        rid = r["run_id"]
        d = fetched.get(rid) or cached.get(rid) or {}
        if r.get("status") in DEPOT_FINAL:
            started = d.get("started_at") or iso_epoch(r.get("created_at"))
            finished = d.get("finished_at")
            wall = round(finished - started, 1) if finished and started else None
            d = dict(d, wall_s=wall, ok=r["status"] == "finished")
        runs_out[rid] = d
    own = sched_depot_run_ids()
    running, recent = [], []
    for r in runs:
        rid = r["run_id"]
        if rid in own:
            continue
        d = runs_out[rid]
        created = iso_epoch(r.get("created_at"))
        started = d.get("started_at") or created
        label = d.get("label") or f"Depot CI {rid}"
        depot = {
            "target": "ci",
            "job": label,
            "cores": None,
            "run_id": rid,
            "url": d.get("url"),
            "cost_usd": None,
        }
        if r.get("status") in DEPOT_FINAL:
            if len(recent) < DEPOT_RECENT_N:
                recent.append(
                    {
                        "id": f"depot-{rid}",
                        "label": label,
                        "where": "depot",
                        "rc": DEPOT_FINAL[r["status"]],
                        "finished_at": d.get("finished_at") or created,
                        "wall_s": d.get("wall_s"),
                        "cost_usd": None,
                        "route_text": f"Depot CI run {rid}: {r['status']}",
                        "url": d.get("url"),
                    }
                )
            continue
        elapsed = max(0.0, now - (started or now))
        walls = sorted(
            v["wall_s"]
            for v in dict(cached, **runs_out).values()
            if v.get("label") == label and v.get("ok") and v.get("wall_s")
        )
        p50 = walls[len(walls) // 2] if walls else None
        total = d.get("jobs_total") or 0
        if p50:
            progress = min(0.99, elapsed / p50)
        else:
            progress = (d.get("jobs_done") or 0) / total if total else 0.0
        if r.get("status") == "queued":
            text = "Depot CI: queued"
        else:
            text = (
                f"Depot CI: {d.get('jobs_done') or 0} of {total} jobs done"
                if total > 1
                else "Depot CI: running"
            )
        running.append(
            {
                "id": f"depot-{rid}",
                "kind": "ci",
                "label": label,
                "repo": (r.get("repo") or "").rsplit("/", 1)[-1] or None,
                "agent": None,
                "where": "depot",
                "route": {"choice": "depot", "why": "ci", "text": text},
                "elapsed_s": round(elapsed, 1),
                "eta_s": round(max(0.0, p50 - elapsed), 1) if p50 else None,
                "progress": round(progress, 2),
                "depot": depot,
            }
        )
    out.update(running=running, recent=recent, _runs=runs_out)
    return out


def cmd_depot(args):
    """Odświeża sched/depot.json; z --max-age S nie pyta Depot, gdy plik jest młodszy niż S."""
    import fcntl

    max_age = 0.0
    if "--max-age" in args:
        try:
            max_age = float(args[args.index("--max-age") + 1])
        except (IndexError, ValueError):
            print("usage: sched.py depot [--max-age S] [--json]", file=sys.stderr)
            return 2
    data = load_depot()
    if time.time() - float(data.get("checked_at") or 0) >= max_age:
        os.makedirs(SCHED_DIR, exist_ok=True)
        fd = os.open(DEPOT_LOCK, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)  # inny proces właśnie pyta Depot; jego wynik wyląduje w pliku
            fd = None
        if fd is not None:
            try:
                data = sync_depot(data, load_config())
                save_depot(data)
            finally:
                os.close(fd)
    if "--json" in args:
        print(json.dumps(public_state(data), ensure_ascii=False, indent=1))
        return 0
    if data.get("error"):
        print(f"Depot: {data['error']}")
    for j in data.get("running") or []:
        print(
            f"  biegnie  {j['label']}  {human_s(j.get('elapsed_s'))}  {j['route']['text']}  {j['depot'].get('url') or ''}"
        )
    for r in data.get("recent") or []:
        ago = human_s(time.time() - r["finished_at"]) if r.get("finished_at") else "?"
        wall = human_s(r["wall_s"]) if r.get("wall_s") else "?"
        print(
            f"  {'ok ' if r['rc'] == 0 else 'źle'}  {r['label']}  {wall}, {ago} temu  {r['route_text']}"
        )
    return 0 if not data.get("error") else 1


def cmd_status(args):
    cfg = load_config()
    with Locked():
        state = load_state(cfg)
        reap(state)
        refresh_memory(state, cfg)
        update_queue_view(state, cfg)
        save_state(state)
    if "--json" in args:
        print(json.dumps(public_state(state), ensure_ascii=False, indent=1))
        return 0
    mem = state["memory"]
    print(
        f"Pamięć: do wpuszczenia {pl_gb(mem['free_for_admission_gb'])} (level {mem['level_pct']}%, "
        f"zapas {pl_gb(mem['headroom_gb'])}, dev serwer {pl_gb(mem['devserver_reserve_gb'])}, "
        f"joby urosną jeszcze o {pl_gb(mem['reserved_gb'])}); pusty Mac zmieści {pl_gb(mem['idle_max_gb'])}"
    )
    native = mem.get("native") or {}
    if native.get("owner"):
        slot = f"zajęte: {native['owner_label']}"
    elif native.get("outside"):
        slot = (
            f"zajęte przez build spoza schedulera ({pl_gb(native['build_gb'])}, "
            f"urośnie jeszcze o {pl_gb(native['reserve_gb'])})"
        )
    else:
        slot = "wolne"
    sims = mem.get("simulators")
    sims_text = (
        f"; symulatory: {sims['booted']} włączone ({pl_gb(sims['gb'])}), agentów w użyciu "
        f"{sims.get('agents_in_use', 0)} z limitu {sims['cap']}"
        if sims
        else ""
    )
    print(f"Natywny build (jeden naraz): {slot}{sims_text}")
    if mem.get("long_lived_gb") is not None:
        names = {"dev": "dev serwery", "metro": "expo/metro", "watchers": "watchery", "simulators": "symulatory",
                 "headless": "headless przeglądarki", "lsp": "LSP", "docker": "Docker"}  # fmt: skip
        parts = [f"{names[f['family']]} {pl_gb(f['gb'])}" for f in mem.get("long_lived") or [] if f["gb"] >= 0.05]
        print(f"Długo żyjące: {pl_gb(mem['long_lived_gb'])}" + (" (" + ", ".join(parts) + ")" if parts else "")
              + f"; hamulec strażnika: {mem.get('brake', 'normal')}")
    if not state["running"] and not state["queue"]:
        print("Nic nie biegnie.")
    for j in state["running"]:
        if j["where"] == "local":
            extra = (
                f"{pl_gb(j.get('mem_now_gb') or 0)} z {pl_gb(j['mem_predicted_gb'])}"
            )
        else:
            extra = f"Depot {j['depot']['job']} {j['depot']['cores']}c, ${j['depot']['cost_usd']:.2f}"
        print(
            f"  biegnie  {j['label']}  [{j['module']}]  -p {j.get('p') or '-'}  "
            f"{human_s(j.get('elapsed_s'))} z ~{human_s(j['predicted_wall_s'])}  {extra}"
            + ("  PAUZA" if j.get("paused") else "")
        )
    for j in queue_order(state):
        print(
            f"  czeka {j.get('position')}. {j['label']}  [{j['module']}]  {human_s(j.get('waited_s'))}: {j.get('reason', {}).get('text')}"
        )
    t = state["today"]
    print(
        f"Dziś: {t['jobs_local']} lokalnie, {t['jobs_depot']} na Depot; czekanie {human_s(t['wait_s'])} "
        f"(Go {human_s(t.get('go_wait_s', 0))}, przy starym zamku na Go {human_s(t['old_lock_wait_s'])}); "
        f"Depot ${t['depot_cost_usd']:.2f}, "
        f"zostało lokalnie ${t['local_kept_usd']:.2f}"
    )
    return 0


def cmd_classify(args):
    _opts, command, argv = parse_run_args(args)
    job = classify(command, os.getcwd(), argv=argv) if (command or argv) else None
    print(json.dumps(job, ensure_ascii=False, indent=1, default=str))
    return 0 if job else 1


WAIT_MAX_S = 270  # krócej niż 5-minutowy cache subagenta, z zapasem na samo wywołanie


def cmd_wait(args):
    """Czeka, aż WARUNEK (komenda powłoki) skończy się kodem 0, najwyżej --max sekund. Kod 0:
    spełniony; 75: jeszcze nie, zawołaj ponownie (każde wywołanie odświeża cache agenta)."""
    if "--" not in args or not args[args.index("--") + 1 :]:
        log("użycie: sched.py wait [--max S] [--every S] -- 'WARUNEK'")
        return 64
    opts, condition = args[: args.index("--")], args[args.index("--") + 1 :]
    limit, every = float(WAIT_MAX_S), 5.0
    try:
        for flag, value in zip(opts[::2], opts[1::2]):
            if flag == "--max":
                limit = float(value)
            elif flag == "--every":
                every = float(value)
    except ValueError:
        log("--max i --every to liczby sekund")
        return 64
    import subprocess

    command = condition[0] if len(condition) == 1 else " ".join(shlex.quote(a) for a in condition)
    start = time.time()
    while True:
        done = subprocess.run(
            ["/bin/sh", "-c", command], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        ).returncode == 0
        waited = time.time() - start
        if done:
            print(f"gotowe po {human_s(waited)}")
            return 0
        if waited >= limit:
            print(f"jeszcze nie po {human_s(waited)}: zawołaj ponownie (każde wywołanie odświeża cache agenta)")
            return EXIT_TIMEOUT
        time.sleep(max(0.05, min(every, limit - waited)))


RTK_CONFIG = os.path.join(HOME, "Library/Application Support/rtk/config.toml")


def rtk_excludes_line(extra=()):
    """Linia `exclude_commands` do [hooks] w configu rtk: komendy, które owija scheduler, plus
    `extra` (wzorce dopisane ręcznie). Literały TOML ('...'), więc ukośniki zostają, jak są."""
    quote = lambda p: f"'{p}'" if "'" not in p and "\n" not in p else json.dumps(p)  # noqa: E731
    return "exclude_commands = [" + ", ".join(quote(p) for p in list(RTK_EXCLUDES) + list(extra)) + "]"


def toml_string_array(text, pos):
    """Tablica stringów TOML od `[` na pozycji pos: (stringi, pozycja za `]`), albo None, gdy to
    nie jest prosta tablica stringów (wtedy nie ruszamy pliku). Komentarze i nowe linie w środku
    są dozwolone, nawiasy w stringach nie liczą się do zagnieżdżenia."""
    items, i, depth = [], pos, 0
    while i < len(text):
        c = text[i]
        if c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                return items, i + 1
        elif c in "'\"":
            if text.startswith(c * 3, i):
                return None  # stringi wieloliniowe: nie zgadujemy
            j = i + 1
            while j < len(text) and text[j] != c and text[j] != "\n":
                j += 2 if c == '"' and text[j] == "\\" else 1
            if j >= len(text) or text[j] != c:
                return None
            if c == "'":
                items.append(text[i + 1 : j])
            else:
                try:
                    items.append(json.loads(text[i : j + 1]))
                except ValueError:
                    return None
            i = j
        elif c == "#":
            nl = text.find("\n", i)
            i = len(text) if nl < 0 else nl
            continue
        elif not (c.isspace() or c == ","):
            return None
        i += 1
    return None


def write_rtk_excludes(path=RTK_CONFIG):
    """Wpisuje wzorce schedulera do `exclude_commands` w sekcji [hooks] configu rtk: najpierw
    nasze, potem Twoje, których u nas nie ma (nic nie znika). Sekcję dopisuje, gdy jej nie ma;
    reszta pliku zostaje znak w znak, a przed pierwszą zmianą powstaje kopia
    `config.toml.bak-claude-acc`. Z tomllib (Python 3.11+) wynik musi się parsować i różnić od
    starego tylko tą listą. True, gdy plik się zmienił; ValueError, gdy plik jest nie do ruszenia."""
    try:
        with open(path) as f:
            text = f.read()
    except FileNotFoundError:
        text = ""
    header = re.search(r"^[ \t]*\[hooks\][ \t]*(#.*)?$", text, re.M)
    if header is None:
        if not text.strip() or text.endswith("\n\n"):
            sep = ""
        else:
            sep = "\n" if text.endswith("\n") else "\n\n"
        out = text + sep + "[hooks]\n" + rtk_excludes_line() + "\n"
    else:
        tables = re.compile(r"^[ \t]*\[", re.M)
        nxt = tables.search(text, header.end())
        stop = nxt.start() if nxt else len(text)
        key = re.compile(r"^([ \t]*)exclude_commands[ \t]*=[ \t]*", re.M).search(text, header.end(), stop)
        if key is None:
            out = text[: header.end()] + "\n" + rtk_excludes_line() + text[header.end() :]
        else:
            if text[key.end() : key.end() + 1] != "[":
                raise ValueError("exclude_commands w configu rtk to nie tablica")
            parsed = toml_string_array(text, key.end())
            if parsed is None:
                raise ValueError("exclude_commands w configu rtk to nie prosta tablica stringów")
            theirs, after = parsed
            extra = [p for p in theirs if p not in RTK_EXCLUDES]
            line = rtk_excludes_line(extra)
            out = text[: key.start()] + key.group(1) + line + text[after:]
    if out == text:
        return False
    check_rtk_config(text, out)
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    backup = path + ".bak-claude-acc"
    if text and not os.path.exists(backup):
        with open(backup, "w") as f:
            f.write(text)
    with open(path + ".tmp", "w") as f:
        f.write(out)
    os.replace(path + ".tmp", path)
    return True


def check_rtk_config(before, after):
    """Z tomllib: nowy config się parsuje i różni od starego tylko listą exclude_commands."""
    try:
        import tomllib
    except ImportError:  # Python 3.9 z Xcode: skaner wyżej musi wystarczyć
        return
    try:
        old = tomllib.loads(before)
        new = tomllib.loads(after)
    except tomllib.TOMLDecodeError as err:
        raise ValueError(f"config rtk po zmianie nie jest poprawnym TOML: {err}") from err
    for data in (old, new):
        hooks = data.get("hooks")
        if isinstance(hooks, dict):
            hooks.pop("exclude_commands", None)
            if not hooks:
                data.pop("hooks")
    if old != new:
        raise ValueError("zmiana w configu rtk dotknęłaby czegoś poza exclude_commands")


def cmd_rtk_excludes(args):
    """Bez argumentów drukuje linię `exclude_commands`; `--write [PATH]` wpisuje ją w config rtk
    (domyślnie RTK_CONFIG). Woła to setup.sh przy każdej instalacji."""
    if args[:1] != ["--write"]:
        print(rtk_excludes_line())
        return 0
    path = args[1] if len(args) > 1 else RTK_CONFIG
    try:
        changed = write_rtk_excludes(path)
    except (OSError, ValueError) as err:
        print(f"rtk: nie ruszam {path}: {err}", file=sys.stderr)
        return 1
    print(f"rtk: {'wpisane wyjątki schedulera' if changed else 'wyjątki schedulera bez zmian'} ({path})")
    return 0


CODEX_MARK = "devguard admit --codex"  # po tym poznajemy nasz wpis; native front podaje `codex`
CODEX_NATIVE_MARK = re.compile(r"claude-acc-hook'? codex$")


def codex_hooks_path():
    return os.path.join(os.environ.get("CODEX_HOME") or os.path.join(HOME, ".codex"), "hooks.json")


def codex_hook_command():
    """Komenda hooka dla Codeksa: natywny front, a bez niego Python z acc.py."""
    native = os.path.join(STATE_DIR, "claude-acc-hook")
    if os.access(native, os.X_OK):
        return f"{shlex.quote(native)} codex"
    return " ".join(shlex.quote(a) for a in (os.path.join(STATE_DIR, "python"), os.path.join(STATE_DIR, "acc.py"))) + " " + CODEX_MARK


def is_codex_entry(entry):
    return any(
        CODEX_MARK in (h.get("command") or "") or CODEX_NATIVE_MARK.search(h.get("command") or "")
        for h in (entry.get("hooks") or [])
    )


def cmd_codex(args):
    """`codex install|uninstall|status`: hook PreToolUse w ~/.codex/hooks.json, który owija ciężkie
    komendy Codeksa w scheduler tak samo jak w Claude Code. Codex uruchamia nowy hook dopiero
    po zaufaniu mu w `/hooks`."""
    action = args[0] if args else "status"
    path = codex_hooks_path()
    try:
        with open(path) as f:
            data = json.load(f)
    except FileNotFoundError:
        data = {}
    except (OSError, ValueError) as err:
        log(f"{path}: {err}")
        return 1
    pre = data.setdefault("hooks", {}).setdefault("PreToolUse", [])
    ours = [e for e in pre if is_codex_entry(e)]
    if action == "status":
        print(f"{path}: {'hook schedulera jest' if ours else 'brak hooka schedulera'}")
        return 0 if ours else 1
    if action not in ("install", "uninstall"):
        log("użycie: sched.py codex install|uninstall|status")
        return 64
    if action == "uninstall" and not ours:
        print(f"{path}: brak hooka schedulera")
        return 0
    rest = [e for e in pre if not is_codex_entry(e)]
    if action == "install":
        entry = {"matcher": "Bash", "hooks": [{"type": "command", "command": codex_hook_command(), "timeout": 30}]}
        if ours == [entry]:
            print(f"{path}: bez zmian")
            return 0
        rest.append(entry)
    data["hooks"]["PreToolUse"] = rest
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp{os.getpid()}"
    with open(tmp, "w") as f:
        f.write(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, path)
    if action == "install":
        print(f"{path}: hook schedulera dopisany. Codex uruchomi go po zaufaniu: w Codeksie /hooks.")
    else:
        print(f"{path}: hook schedulera zdjęty")
    return 0


COMMANDS = {
    "codex": cmd_codex,
    "run": cmd_run,
    "status": cmd_status,
    "classify": cmd_classify,
    "wait": cmd_wait,
    "rtk-excludes": cmd_rtk_excludes,
    "depot": cmd_depot,
}


def main(argv):
    if not argv or argv[0] not in COMMANDS:
        print(__doc__)
        return 2
    return COMMANDS[argv[0]](argv[1:])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
