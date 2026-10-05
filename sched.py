#!/usr/bin/env python3
"""Scheduler komend Go agentów: wpuszcza joby po pamięci zamiast jednego zamka na wszystko.

  sched.py run [--timeout S] [--session ID] [--agent NAME] [--via hook|plock|cli]
               (--shell 'KOMENDA' | -- ARGV...)
  sched.py status [--json]
  sched.py classify (--shell 'KOMENDA' | -- ARGV...)

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
"""

import ctypes
import ctypes.util
import fcntl
import functools
import hashlib
import json
import os
import random
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time

HOME = os.path.expanduser("~")
STATE_DIR = os.path.join(HOME, ".local/share/claude-acc")
SCHED_DIR = os.path.join(STATE_DIR, "sched")
STATE_PATH = os.path.join(SCHED_DIR, "state.json")
HISTORY_PATH = os.path.join(SCHED_DIR, "history.jsonl")
LOCK_PATH = os.path.join(SCHED_DIR, "lock")
CONFIG_PATH = os.environ.get("SCHED_CONFIG") or os.path.join(SCHED_DIR, "config.json")
CACHE_PATH = os.path.join(SCHED_DIR, "cache.json")
DEVGUARD_STATE = os.path.join(STATE_DIR, "devguard-state.json")
DEVGUARD_CONFIG = os.path.join(STATE_DIR, "devguard.json")
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
    "drop_count1": True,
    "pause_swap_gb": 0.5,
    "ldflags_w_for_build": True,
    "depot_eta_since": "2026-10-05",
    # pliki pomocników testów (ścieżka w module, prefiks), których exec nie psuje cache testów:
    # szablon testpg czyta migracje, Dockerfile i atlasa w procesie testu (os.ReadFile, LookPath)
    "count1_trusted_exec": ["internal/testhelpers/testpg"],
    "idle_floor_pct": 65,
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

_libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)


class _RusageV0(ctypes.Structure):
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


class _Timebase(ctypes.Structure):
    _fields_ = [("numer", ctypes.c_uint32), ("denom", ctypes.c_uint32)]


class _XswUsage(ctypes.Structure):
    _fields_ = [
        ("total", ctypes.c_uint64),
        ("avail", ctypes.c_uint64),
        ("used", ctypes.c_uint64),
        ("pagesize", ctypes.c_uint32),
        ("encrypted", ctypes.c_bool),
    ]


_tb = _Timebase()
_libc.mach_timebase_info(ctypes.byref(_tb))
_TICK_S = (_tb.numer / _tb.denom if _tb.denom else 1.0) / 1e9
PROC_PGRP_ONLY = 2
PROC_PPID_ONLY = 6


def sysctl_int(name):
    value = ctypes.c_uint64(0)
    size = ctypes.c_size_t(8)
    if _libc.sysctlbyname(
        name.encode(), ctypes.byref(value), ctypes.byref(size), None, 0
    ):
        return None
    return value.value & ((1 << (8 * size.value)) - 1)


def swap_used_gb():
    info = _XswUsage()
    size = ctypes.c_size_t(ctypes.sizeof(info))
    if _libc.sysctlbyname(
        b"vm.swapusage", ctypes.byref(info), ctypes.byref(size), None, 0
    ):
        return 0.0
    return info.used / GB


def proc_usage(pid):
    """(phys_footprint w bajtach, CPU w s) procesu albo None."""
    info = _RusageV0()
    if _libc.proc_pid_rusage(pid, 0, ctypes.byref(info)) != 0:
        return None
    return info.phys_footprint, (info.user_time + info.system_time) * _TICK_S


def _listpids(kind, arg):
    buf = (ctypes.c_int * 2048)()
    n = _libc.proc_listpids(kind, arg, buf, ctypes.sizeof(buf))
    if n <= 0:
        return []
    return [p for p in buf[: n // ctypes.sizeof(ctypes.c_int)] if p > 0]


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


def job_usage(root):
    """(GB, CPU s) wszystkich żywych procesów joba."""
    footprint, cpu = 0, 0.0
    for pid in job_pids(root):
        u = proc_usage(pid)
        if u:
            footprint += u[0]
            cpu += u[1]
    return footprint / GB, cpu


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


def probe_memory():
    """Pamięć systemu: level jądra, RAM, swap, presja. SCHED_FAKE_MEMORY (plik JSON) w testach."""
    fake = os.environ.get("SCHED_FAKE_MEMORY")
    if fake:
        try:
            with open(fake) as f:
                data = json.load(f)
            return {
                "level": float(data.get("level", 60)),
                "ram_gb": float(data.get("ram_gb", 48)),
                "swap_gb": float(data.get("swap_gb", 0)),
                "pressure": data.get("pressure", "normal"),
            }
        except (OSError, ValueError):
            pass
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


def devserver_reserve_gb():
    """Miejsce na jeszcze jeden dev serwer: min(max_server_gb, budżet devguarda - zajęte)."""
    max_server = 4.0
    try:
        with open(DEVGUARD_CONFIG) as f:
            max_server = float(json.load(f).get("max_server_gb", max_server))
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    try:
        with open(DEVGUARD_STATE) as f:
            snap = json.load(f).get("snapshot") or {}
        if time.time() - float(snap.get("at", 0)) < 120:
            room = (float(snap.get("budget", 0)) - float(snap.get("total", 0))) / GB
            return round(max(0.0, min(max_server, room)), 2)
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return max_server


# ---------- klasyfikacja komendy ----------

WRAPPERS = {"rtk", "time", "nice", "env", "caffeinate", "command", "exec", "nohup"}
GO_VERBS = {"build", "test", "vet", "run", "install", "generate"}
SKIP_MARKERS = ("sched.py", "plock.py", "depot-exec.sh", "depot-ci.sh", "SCHED_OFF=1")
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


def classify(command, cwd, argv=None):
    """Job z komendy powłoki albo argv; None, gdy nie ma w niej pracy Go dla schedulera."""
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
    found = []
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
        if job:
            job["argv"] = words
            job["env"] = env
            found.append(job)
    jobs = [j for j in (finish_job(j) for j in found) if j]
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


def read_history(limit_bytes=8 * 1024 * 1024):
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
    if not rows:
        return base_gb, base_s, "prior"
    gb = percentile([r["peak_gb"] for r in rows], 0.9) * 1.15
    s = percentile([r["wall_s"] for r in rows], 0.5)
    if len(rows) < 3:
        gb = max(gb, base_gb * 0.8)  # jeden czy dwa biegi to jeszcze nie statystyka
    return round(gb, 2), round(s, 1), f"history:{len(rows)}"


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
        try:
            since = load_config().get("depot_eta_since")
            out = subprocess.run(
                ["/usr/bin/python3", script, "eta", "--json"] + (["--since", since] if since else []),
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


EXEC_IMPORT = re.compile(r'^\s*(?:import\s+)?(?:([\w.]+)\s+)?"os/exec"', re.M)


def exec_calls(path):
    """Czy plik Go uruchamia inny program (exec.Command, exec.CommandContext, os.StartProcess)."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return False
    if "os.StartProcess(" in text or "syscall.Exec(" in text:
        return True
    m = EXEC_IMPORT.search(text)
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
    if job["kind"] != "test":
        return False
    if job["scope"] == "tree":
        return os.path.isdir(os.path.join(job["module_dir"], "internal/testhelpers"))
    inputs = test_inputs(job, cache)
    return bool(inputs and inputs["pg"])


COUNT1 = re.compile(r"(?<![\w-])-count(?:=| )1(?![\w.])")


def drop_count1(command):
    """Komenda bez jedynego -count=1 (albo -count 1); None, gdy nie da się tego zrobić jednoznacznie."""
    if len(COUNT1.findall(command)) != 1:
        return None
    return re.sub(r" ?" + COUNT1.pattern, "", command, count=1)


# ---------- stan ----------


class Locked:
    """flock na sched/lock; block=False: ok=False, gdy ktoś inny trzyma."""

    def __init__(self, block=True):
        self.block = block
        self.fd = None
        self.ok = False

    def __enter__(self):
        os.makedirs(SCHED_DIR, exist_ok=True)
        self.fd = os.open(LOCK_PATH, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | (0 if self.block else fcntl.LOCK_NB))
            self.ok = True
        except BlockingIOError:
            self.ok = False
        return self

    def __exit__(self, *exc):
        if self.ok:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
        os.close(self.fd)


def new_today():
    return {
        "date": time.strftime("%Y-%m-%d"),
        "jobs_local": 0,
        "jobs_depot": 0,
        "wait_s": 0.0,
        "old_lock_wait_s": 0.0,
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
        "_internal": {"swap": [], "avail": [], "vlock_free_at": 0.0},
    }


def load_state(cfg):
    try:
        with open(STATE_PATH) as f:
            state = json.load(f)
        if state.get("version") != VERSION:
            raise ValueError
    except (OSError, ValueError):
        state = empty_state(cfg)
    state.setdefault("_internal", {"swap": [], "avail": [], "vlock_free_at": 0.0})
    if state.get("today", {}).get("date") != time.strftime("%Y-%m-%d"):
        state["today"] = new_today()
        state["_internal"]["vlock_free_at"] = 0.0
    state["config"] = {k: cfg[k] for k in PUBLIC_CONFIG}
    return state


def save_state(state):
    now = time.time()
    state["updated_at"] = now
    busy = state["running"] or state["queue"]
    state["idle_since"] = None if busy else (state.get("idle_since") or now)
    os.makedirs(SCHED_DIR, exist_ok=True)
    tmp = f"{STATE_PATH}.tmp{os.getpid()}"
    with open(tmp, "w") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
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
    with open(tmp, "w") as f:
        json.dump(cache, f)
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
            for sig in (signal.SIGCONT, signal.SIGTERM):
                try:
                    os.killpg(pgid, sig)
                except OSError:
                    pass
    state["running"] = keep
    state["queue"] = [j for j in state["queue"] if alive(j.get("pid"))]


def refresh_memory(state, cfg, mem=None):
    mem = mem or probe_memory()
    now = time.time()
    ram = mem["ram_gb"]
    available = mem["level"] / 100 * ram
    local = [j for j in state["running"] if j["where"] == "local"]
    jobs_now = sum(j.get("mem_now_gb") or 0 for j in local)
    reserved = sum(
        max(0.0, (j.get("mem_predicted_gb") or 0) - (j.get("mem_now_gb") or 0))
        for j in local
        if not j.get("paused")
    )
    dev = devserver_reserve_gb()
    internal = state["_internal"]
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
    }
    today = state["today"]
    today["max_reserved_gb"] = round(max(today.get("max_reserved_gb", 0), reserved), 1)
    today["peak_concurrency"] = max(
        today.get("peak_concurrency", 0), len(state["running"])
    )
    return state["memory"]


def queue_order(state):
    return sorted(state["queue"], key=lambda j: j["enqueued_at"])


def plan(state, cfg, now):
    """Kto z kolejki startuje teraz: {id: ("fits"|"overtake", id wyprzedzonego)}.

    FIFO; głowa startuje, gdy się mieści, a gdy lokalnie nic nie biegnie: bez rezerwy na dev
    serwer, po 30 s czekania w ogóle. Za zablokowaną głową startują tylko małe joby, a gdy głowa
    czeka dłużej niż starve_s, jej pamięć jest zarezerwowana i nikt jej nie wyprzedza."""
    mem = state["memory"]
    free = mem["free_for_admission_gb"]
    any_local = any(j["where"] == "local" for j in state["running"])
    pressure = mem.get("pressure", "normal")
    admitted = {}
    blocked = None
    reserve = 0.0
    if pressure == "critical":
        return admitted
    for job in queue_order(state):
        if (job.get("route") or {}).get("choice") == "depot":
            continue
        need = job["mem_predicted_gb"]
        if blocked is None:
            spare = mem["available_gb"] - cfg["headroom_gb"]
            alone = not any_local and not admitted
            # sam na Macu: bez rezerwy na dev serwer, a po 30 s czekania nawet ponad pamięć
            # (job bez trasy na Depot; nic innego niż on nie zwolni pamięci, pilnuje go SIGSTOP).
            # Przy „warn” bez tego ostatniego: macOS trzyma go tu godzinami przy połowie wolnej
            # pamięci, więc startuje to, co się mieści, ale nic ponad dostępną pamięć.
            overcommit = pressure != "warn" and now - job["enqueued_at"] >= 30
            if need <= free or (alone and (need <= spare or overcommit)):
                admitted[job["id"]] = ("fits", None)
                free -= need
                any_local = True
                continue
            blocked = job
            if now - job["enqueued_at"] > cfg["starve_s"]:
                reserve = need
            continue
        if job.get("small") and need <= free - reserve:
            admitted[job["id"]] = ("overtake", blocked["id"])
            free -= need
    return admitted


def blockers_eta(state, job):
    """(sekundy do chwili, gdy job się zmieści, id jobów, na których koniec czeka)."""
    need = job["mem_predicted_gb"]
    free = state["memory"]["free_for_admission_gb"]
    if need <= free:
        return 0.0, []
    now = time.time()
    finishing = []
    for i, r in enumerate(state["running"]):
        if r["where"] == "local":
            left = (r.get("predicted_wall_s") or 60) - (now - r.get("started_at", now))
            finishing.append((max(5.0, left), i, r))
    finishing.sort(key=lambda x: (x[0], x[1]))
    after = []
    for left, _i, r in finishing:
        free += r.get("mem_predicted_gb") or 0
        after.append(r["id"])
        if need <= free:
            return left, after
    return 3600.0, after


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
    for pos, job in enumerate(queue_order(state), start=1):
        job["position"] = pos
        job["waited_s"] = round(now - job["enqueued_at"], 1)
        wait, after = blockers_eta(state, job)
        job["eta_start_s"] = round(wait) if wait < 3600 else None
        free = max(0.0, mem["free_for_admission_gb"])
        if mem.get("pressure") == "critical":
            code, text = "pressure", "paused: memory pressure is critical"
        elif (
            head_blocked is not None
            and now - head_blocked["enqueued_at"] > cfg["starve_s"]
        ):
            code, text = (
                "head",
                f"waiting: memory is reserved for {head_blocked['label']}",
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
        if head_blocked is None:
            head_blocked = job


def safety(state, cfg):
    """SIGSTOP najmłodszego ciężkiego joba, gdy swap rośnie; SIGCONT, gdy pamięć odpuści."""
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
        heavy = [j for j in local if not j.get("paused") and not j.get("small")]
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
    return f"j-{int(time.time())}-{random.randrange(16**4):04x}"


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
        "cmd": command
        if command is not None
        else " ".join(shlex.quote(a) for a in argv),
        "agent": agent_info(opts.get("session"), opts.get("agent")),
        "where": None,
        "route": None,
        "small": False,
        "count1_dropped": False,
        "pid": os.getpid(),
        "via": opts.get("via", "cli"),
        "enqueued_at": time.time(),
    }


def cmd_run(args):
    opts, command, argv = parse_run_args(args)
    if command is None and not argv:
        print(__doc__)
        return 2
    cfg = load_config()
    cwd = os.getcwd()
    job = classify(command, cwd, argv=argv)
    if job is None and argv and opts.get("via") == "plock":
        job = opaque_job(argv, cwd)
    if job is None:
        return exec_plain(command, argv)
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
    if job["kind"] == "test" and os.path.isfile(os.path.join(job["repo_dir"], "scripts/depot-exec.sh")):
        job["uses_pg"] = uses_pg(job, cache)
    if likely_heavy(job):
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
    while True:
        pid, status, rusage = os.wait4(child.pid, os.WNOHANG)
        if pid == child.pid:
            break
        now_gb, cpu_live = job_usage(child.pid)
        peak = max(peak, now_gb)
        if time.time() - last_beat >= 1.0:
            last_beat = time.time()
            heartbeat(jid, cfg, now_gb, peak, cpu_live, started)
        time.sleep(0.25)
    rc = os.waitstatus_to_exitcode(status)
    cpu = rusage.ru_utime + rusage.ru_stime if rusage else cpu_live
    finish(jid, cfg, rc, time.time() - started, peak, cpu)
    return rc if rc >= 0 else 128 - rc


def heartbeat(jid, cfg, now_gb, peak, cpu, started):
    with Locked(block=False) as lk:
        if not lk.ok:
            return
        state = load_state(cfg)
        me = next((j for j in state["running"] if j["id"] == jid), None)
        if me is None:
            return
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
        today["wait_s"] = round(today["wait_s"] + waited, 1)
        units = cost = None
        if where == "local":
            today["jobs_local"] += 1
            # wirtualny stary zamek: start = max(przyjście, zwolnienie poprzedniego)
            start_v = max(me["enqueued_at"], internal.get("vlock_free_at", 0.0))
            today["old_lock_wait_s"] = round(
                today["old_lock_wait_s"] + start_v - me["enqueued_at"], 1
            )
            internal["vlock_free_at"] = start_v + wall
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
        today["wait_saved_s"] = round(today["old_lock_wait_s"] - today["wait_s"], 1)
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
        }
        with open(HISTORY_PATH, "a") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        refresh_memory(state, cfg)
        update_queue_view(state, cfg)
        save_state(state)


DEPOT_LINE = re.compile(
    r"run (\w+): exit (-?\d+) after (\d+)s on (\d+) cores \(~([\d.]+) units\)"
)


def run_depot(entry, target, cfg, job, command, argv, opts, history, cache):
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
            m = DEPOT_LINE.search(line)
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


RECURSIVE_GREP = re.compile(r"(^|[\s;&|(])grep\s+(-[A-Za-z]*[rR][A-Za-z]*|--recursive)")

def with_rtk(command):
    """Komenda tak, jak przepisałby ją hook rtk bez naszych wyjątków: komendy schedulera są w jego
    exclude_commands (dwa hooki z updatedInput na tej samej komendzie dają losowy wynik), więc
    `rtk` dokładamy tu, w środku opakowania. `rtk rewrite` to jedno źródło jego reguł; config
    z wyjątkami czyta z HOME, więc pytamy go z pustym HOME. Kod 3 („przepisz, ale zapytaj”,
    bo tam nie widzi ustawień Claude) to dla nas zwykłe przepisanie."""
    rtk = shutil.which("rtk")
    if not rtk:
        return command
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
    try:
        job = classify(command, event.get("cwd") or os.getcwd())
    except Exception:  # hook nigdy nie blokuje agenta przez własny błąd
        return None
    if job is None:
        return None
    parts = ["/usr/bin/python3", SELF, "run", "--via", "hook"]
    if event.get("session_id"):
        parts += ["--session", str(event["session_id"])]
    agent = event.get("agent_type") or event.get("subagent_type")
    if agent:
        parts += ["--agent", str(agent)]
    updated = dict(tool_input)
    updated["command"] = (
        " ".join(shlex.quote(p) for p in parts) + " --shell " + shlex.quote(with_rtk(command))
    )
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "updatedInput": updated,
            "additionalContext": (
                f"claude-acc sched: `{job['label']}` runs through the memory scheduler. It may wait for "
                "memory, pick -p or run on Depot; the output and exit code are the command's own."
            ),
        }
    }


def public_state(state):
    return {k: v for k, v in state.items() if not k.startswith("_")}


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
        f"(stary zamek: {human_s(t['old_lock_wait_s'])}); Depot ${t['depot_cost_usd']:.2f}, "
        f"zostało lokalnie ${t['local_kept_usd']:.2f}"
    )
    return 0


def cmd_classify(args):
    _opts, command, argv = parse_run_args(args)
    job = classify(command, os.getcwd(), argv=argv) if (command or argv) else None
    print(json.dumps(job, ensure_ascii=False, indent=1, default=str))
    return 0 if job else 1


COMMANDS = {"run": cmd_run, "status": cmd_status, "classify": cmd_classify}


def main(argv):
    if not argv or argv[0] not in COMMANDS:
        print(__doc__)
        return 2
    return COMMANDS[argv[0]](argv[1:])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
