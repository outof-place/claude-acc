#!/usr/bin/env python3
"""Wydajność Maca: pomiary sieci, CPU i GPU oraz poprawki, które coś dają.

Siedzi obok janitor.py i trzyma stan w tym samym katalogu. Każda poprawka ma
zmierzony efekt (docs/perf-research.md) i dokładne cofnięcie zapisane w stanie,
więc `undo` przywraca to, co było, a nie to, co uważamy za domyślne.

Pomiary:
- sieć: networkQuality od Apple (przepustowość i opóźnienie pod obciążeniem,
  osobno pobieranie i wysyłanie, osobno sieć i kolejka w samym połączeniu), ping
  do bramy i do 1.1.1.1, czas do pierwszego bajtu z api.anthropic.com i to, kto
  w tej chwili zajmuje łącze;
- CPU: ten sam kawałek pracy w jednym procesie i w 16 naraz, z liczników jądra:
  jaka część poszła na rdzenie wydajnościowe i ile czasu proces czekał w kolejce,
  do tego opóźnienie wybudzenia wątku (to, co czujesz jako lag);
- GPU: ile procent czasu GPU zajmuje każdy klient (WindowServer, przeglądarki,
  Electron), ile CPU pali WindowServer i ile czeka malutkie zlecenie Metalu;
- system plików: lstat dużego drzewa node_modules dwa razy pod rząd i ile vnode
  jądro przy tym odzyskuje (czy drzewo mieści się w cache vnode).
- agenci: obrót narzędzi z transkryptów Claude Code (Bash według rodzin poleceń,
  edycje plików, które formatują hooki), czekanie na hooki i polecenia ścięte do 10 min.

Poprawki wymagające roota robi perf-root.sh; tu są tylko opisane.

Ultra to jeden przełącznik dla pracy agentów (Orca, wiele sesji Claude Code i ich dev
serwery): włącza wszystkie poprawki z listy ULTRA, zapisuje, co było przed
nimi, i mierzy przed i po. Stan dla panelu jest w perf-state.json pod "ultra".

Komendy:
  status [--json]                     poprawki i ostatnie pomiary
  bench [network|cpu|gpu|fs|agents|all]  pomiar; wynik ląduje w perf-state.json
        [--runs N] [--json]
  apply <nazwa>|--all [--dry-run]     włącz poprawkę (tylko te bez roota)
  undo <nazwa>|--all                  cofnij
  ultra on|off|status [--json]        wszystkie poprawki dla agentów naraz i ich wyniki
  keep                                pilnuj włączonych poprawek (dla launchd co 5 min)
  link [--json]                       którędy idzie trasa domyślna i czy to tethering (dla panelu)
  list                                poprawki z opisem i zmierzonym efektem
"""

import os
import sys

# bajtkod tylko w $STATE: obok skryptu w paczce Poda (Pod.app/Contents/Resources/claude-acc) __pycache__
# łamie pieczęć aplikacji, czymkolwiek i z jakimikolwiek flagami ten plik uruchomić (1.31.6)
if not os.path.realpath(__file__).startswith(os.path.realpath(os.path.expanduser("~/.local/share/claude-acc")) + "/"):
    sys.dont_write_bytecode = True

import ctypes
import ctypes.util
import glob
import hashlib
import json
import re
import shlex
import shutil
import subprocess
import tempfile
import time

# calendar, plistlib, statistics, tempfile i expat są importowane w funkcjach pomiarów:
# `keep` co 5 minut ich nie używa, a ładowały się przy każdym starcie (~13 ms na 3.9 i
# na 3.14, głównie statistics z decimal, fractions i random)

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import janitor
import orcahost

STATE_DIR = janitor.STATE_DIR
STATE_PATH = os.path.join(STATE_DIR, "perf-state.json")
CONFIG_PATH = os.path.join(STATE_DIR, "perf.json")
LOG_PATH = os.path.join(STATE_DIR, "perf.log")
HISTORY = 30  # tyle ostatnich pomiarów każdego rodzaju zostaje w stanie

MBIT = 1e6


# ---------- liczniki jądra ----------

_libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)


class _Timebase(ctypes.Structure):
    _fields_ = [("numer", ctypes.c_uint32), ("denom", ctypes.c_uint32)]


class _RusageV6(ctypes.Structure):
    """struct rusage_info_v6 z <sys/resource.h> (macOS 12+): czasy w tikach mach."""

    _fields_ = (
        [("uuid", ctypes.c_uint8 * 16)]
        + [
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
                "flags",
                "user_ptime",
                "system_ptime",
                "pinstructions",
                "pcycles",
                "energy_nj",
                "penergy_nj",
                "secure_time_in_system",
                "secure_ptime_in_system",
                "neural_footprint",
                "lifetime_max_neural_footprint",
                "interval_max_neural_footprint",
            )
        ]
        + [("reserved", ctypes.c_uint64 * 9)]
    )


_tb = _Timebase()
_libc.mach_timebase_info(ctypes.byref(_tb))
TICK_NS = _tb.numer / _tb.denom if _tb.denom else 1.0
RUSAGE_INFO_V6 = 6


def rusage(pid):
    """Liczniki procesu: CPU (s), z tego na rdzeniach P, czekanie w kolejce, energia (J).

    None, gdy procesu nie ma albo należy do kogoś innego (jądro wtedy odmawia).
    """
    info = _RusageV6()
    if _libc.proc_pid_rusage(pid, RUSAGE_INFO_V6, ctypes.byref(info)) != 0:
        return None
    tick = TICK_NS / 1e9
    return {
        "cpu": (info.user_time + info.system_time) * tick,
        "pcpu": (info.user_ptime + info.system_ptime) * tick,
        "runnable": info.runnable_time * tick,
        "energy": info.energy_nj / 1e9,
        "footprint": info.phys_footprint,
        "start": info.proc_start_abstime,
    }


def sysctl_int(name):
    value = ctypes.c_uint64(0)
    size = ctypes.c_size_t(8)
    if _libc.sysctlbyname(
        name.encode(), ctypes.byref(value), ctypes.byref(size), None, 0
    ):
        return None
    return value.value & ((1 << (8 * size.value)) - 1)


def cores():
    """(rdzenie wydajnościowe, energooszczędne)."""
    return sysctl_int("hw.perflevel0.logicalcpu") or 0, sysctl_int(
        "hw.perflevel1.logicalcpu"
    ) or 0


def system_load():
    """Obciążenie w chwili pomiaru, żeby wyniki z różnych chwil dało się porównać."""
    load = os.getloadavg()
    swap = janitor.run(["sysctl", "-n", "vm.swapusage"]) or ""
    used = re.search(r"used = ([\d.]+)M", swap)
    return {
        "load1": round(load[0], 2),
        "swap_used_gb": round(float(used.group(1)) / 1024, 2) if used else None,
        "pressure": sysctl_int("kern.memorystatus_vm_pressure_level"),
    }


# ---------- drobne narzędzia ----------


def percentile(values, q):
    if not values:
        return None
    values = sorted(values)
    pos = (len(values) - 1) * q
    low = int(pos)
    high = min(low + 1, len(values) - 1)
    return values[low] + (values[high] - values[low]) * (pos - low)


def median(values):
    import statistics

    return statistics.median(values) if values else None


def rnd(value, digits=1):
    return None if value is None else round(value, digits)


def log(line):
    janitor.log(line, LOG_PATH)


def load_state():
    state = janitor.load_json(STATE_PATH, {})
    state.setdefault("applied", {})
    state.setdefault("bench", {})
    return state


def save_state(state):
    """Zapis tylko zmienionego stanu: `keep` co 5 minut zwykle niczego nie zmienia, a i tak
    przepisywał ~17 KB (zmierzone: trzy przebiegi pod rząd, ten sam plik co do bajtu)."""
    try:
        with open(STATE_PATH) as f:
            if f.read() == json.dumps(state, indent=1):
                return
    except OSError:
        pass
    janitor.write_json(STATE_PATH, state, indent=1)


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(janitor.load_json(CONFIG_PATH, {}))
    return cfg


# ---------- pomiar CPU ----------

CPU_WORK_MB = 1024  # tyle danych przechodzi przez SHA-256 w jednym zadaniu


def cpu_task(work_mb=CPU_WORK_MB):
    """Jedno zadanie: stały kawałek pracy i liczniki jądra tego procesu."""
    block = b"\x5a" * (16 << 20)
    before = rusage(os.getpid())
    started = time.monotonic()
    for _ in range(work_mb // 16):
        hashlib.sha256(block).digest()
    wall = time.monotonic() - started
    after = rusage(os.getpid())
    cpu = after["cpu"] - before["cpu"]
    return {
        "wall": wall,
        "cpu": cpu,
        "pshare": (after["pcpu"] - before["pcpu"]) / cpu if cpu else 0,
        "wait": max(0.0, (after["runnable"] - before["runnable"]) - cpu),
        "energy": after["energy"] - before["energy"],
    }


def wake_latency(count=1000, interval=0.001):
    """O ile później niż trzeba budzi się wątek, który śpi 1 ms (µs)."""
    late = []
    for _ in range(count):
        started = time.perf_counter()
        time.sleep(interval)
        late.append((time.perf_counter() - started - interval) * 1e6)
    return late


def spawn_tasks(count, work_mb=CPU_WORK_MB):
    """`count` zadań naraz, każde w osobnym procesie; wyniki w kolejności startu."""
    here = os.path.dirname(os.path.realpath(__file__))
    code = (
        f"import json,sys; sys.path.insert(0, {here!r}); import perf; "
        f"print(json.dumps(perf.cpu_task({int(work_mb)})))"
    )
    started = time.monotonic()
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", code], stdout=subprocess.PIPE, text=True
        )
        for _ in range(count)
    ]
    results = []
    for proc in procs:
        out, _ = proc.communicate(timeout=300)
        try:
            results.append(json.loads(out))
        except ValueError:
            pass
    return results, time.monotonic() - started


# ostatnia odpowiedź ps: (chwila z time.monotonic(), {pid: linia poleceń})
_OWN_PROCESSES = [None, {}]
OWN_PROCESSES_FRESH = 2  # sekundy


def own_processes():
    """{pid: linia poleceń} procesów tego użytkownika.

    `keep` pytał ps dwa razy w odstępie milisekund (procesy do tła i start Orki), po ~30 ms;
    lista sprzed dwóch sekund jest równie dobra. Pomiary, które czekają dłużej, pytają od nowa.
    """
    at, cached = _OWN_PROCESSES
    if at is not None and time.monotonic() - at < OWN_PROCESSES_FRESH:
        return dict(cached)
    out = janitor.run(["ps", "-U", str(os.getuid()), "-o", "pid=,command="]) or ""
    procs = {}
    for line in out.splitlines():
        pid, _, command = line.strip().partition(" ")
        if pid.isdigit():
            procs[int(pid)] = command.strip()
    if out:
        _OWN_PROCESSES[:] = [time.monotonic(), procs]
    return dict(procs)


def cpu_hogs(seconds=10, top=6):
    """Własne procesy, które w oknie `seconds` zjadły najwięcej rdzeni P i energii.

    Kandydaci do listy `background`: to, co pali rdzenie P, a nikt na to nie czeka.
    """
    procs = own_processes()
    me = os.getpid()
    before = {pid: rusage(pid) for pid in procs if pid != me}
    time.sleep(seconds)
    hogs = []
    for pid, first in before.items():
        last = rusage(pid)
        if not first or not last or last["start"] != first["start"]:
            continue
        cpu = last["cpu"] - first["cpu"]
        hogs.append(
            {
                "pid": pid,
                "name": short_command(procs[pid]),
                "cpu_pct": rnd(cpu / seconds * 100),
                "pcore_pct": rnd((last["pcpu"] - first["pcpu"]) / seconds * 100),
                "watts": rnd((last["energy"] - first["energy"]) / seconds, 2),
            }
        )
    hogs.sort(key=lambda h: -h["watts"])
    return hogs[:top]


def bench_cpu(runs=3):
    """Jeden proces, potem tyle procesów, ile rdzeni, i wybudzenia wątku.

    Wynik w MB/s pracy (więcej = szybciej), udział rdzeni P, czekanie w kolejce
    jako procent czasu pracy i opóźnienie wybudzenia w mikrosekundach.
    """
    perf_cores, eff_cores = cores()
    hogs = cpu_hogs()
    singles = []
    for _ in range(runs):
        results, _wall = spawn_tasks(1)
        singles += results
    many, many_wall = spawn_tasks(perf_cores + eff_cores)
    late = wake_latency()
    single_rate = [CPU_WORK_MB / r["wall"] for r in singles]
    return {
        "single_mbs": rnd(median(single_rate), 0),
        "single_pshare": rnd(median([r["pshare"] for r in singles]) * 100),
        "single_wait_pct": rnd(
            median([r["wait"] / r["cpu"] * 100 for r in singles if r["cpu"]])
        ),
        "multi_mbs": rnd(CPU_WORK_MB * len(many) / many_wall, 0),
        "multi_procs": len(many),
        "multi_pshare": rnd(
            sum(r["pshare"] * r["cpu"] for r in many)
            / max(sum(r["cpu"] for r in many), 1e-9)
            * 100
        ),
        "multi_wait_pct": rnd(
            sum(r["wait"] for r in many) / max(sum(r["cpu"] for r in many), 1e-9) * 100
        ),
        "wake_p50_us": rnd(percentile(late, 0.5), 0),
        "wake_p99_us": rnd(percentile(late, 0.99), 0),
        "wake_max_us": rnd(max(late), 0),
        "hogs": hogs,
    }


# ---------- pomiar GPU ----------


def gpu_clients():
    """Czas GPU (ns) każdego klienta Metalu od jego startu: {"pid 415, WindowServer": ns}."""
    import plistlib
    from xml.parsers.expat import ExpatError

    out = subprocess.run(
        ["ioreg", "-a", "-r", "-c", "AGXDeviceUserClient"],
        capture_output=True,
        check=False,
    ).stdout
    try:
        entries = plistlib.loads(out) if out else []
    except (ValueError, ExpatError):
        return {}
    clients = {}
    for entry in entries:
        who = entry.get("IOUserClientCreator", "?")
        for use in entry.get("AppUsage", []) or []:
            clients[who] = clients.get(who, 0) + use.get("accumulatedGPUTime", 0)
    return clients


def gpu_device():
    """Chwilowe obciążenie GPU w procentach (to samo, co pokazuje Monitor aktywności)."""
    out = janitor.run(["ioreg", "-r", "-c", "IOAccelerator", "-d", "1"]) or ""
    match = re.search(r'"Device Utilization %"=(\d+)', out)
    return int(match.group(1)) if match else None


def cpu_times():
    """Czas CPU (s) każdego procesu z ps: także cudzych, których rusage nie pokaże."""
    out = janitor.run(["ps", "-Ao", "pid=,time=,comm="]) or ""
    times = {}
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        try:
            fields = [float(x) for x in parts[1].split(":")]
        except ValueError:
            continue
        seconds = sum(v * 60**i for i, v in enumerate(reversed(fields)))
        times[int(parts[0])] = (seconds, os.path.basename(parts[2]))
    return times


def client_name(who):
    match = re.match(r"pid (\d+), (.*)", who)
    return (int(match.group(1)), match.group(2)) if match else (0, who)


def bench_gpu(seconds=15):
    """Kto zajmuje GPU i ile CPU pali WindowServer w oknie `seconds` sekund."""
    gpu_before, cpu_before = gpu_clients(), cpu_times()
    started = time.monotonic()
    device = []
    while time.monotonic() - started < seconds:
        value = gpu_device()
        if value is not None:
            device.append(value)
        time.sleep(1)
    gpu_after, cpu_after = gpu_clients(), cpu_times()
    window = time.monotonic() - started
    clients = []
    for who, ns in gpu_after.items():
        busy = (ns - gpu_before.get(who, 0)) / 1e9 / window * 100
        if busy >= 0.1:
            pid, name = client_name(who)
            # IOKit ucina nazwę do 16 znaków ("Brave Browser He"), ps ma pełną
            name = cpu_after.get(pid, (0, name))[1]
            clients.append({"pid": pid, "name": name, "gpu_pct": rnd(busy)})
    clients.sort(key=lambda c: -c["gpu_pct"])
    windowserver = None
    for pid, (seconds_now, name) in cpu_after.items():
        if name == "WindowServer" and pid in cpu_before:
            windowserver = (seconds_now - cpu_before[pid][0]) / window * 100
    return {
        "device_pct": rnd(median(device)),
        "device_max_pct": max(device) if device else None,
        "windowserver_cpu_pct": rnd(windowserver),
        "clients": clients[:8],
        "seconds": round(window),
    }


class _NSRange(ctypes.Structure):
    _fields_ = [("location", ctypes.c_ulong), ("length", ctypes.c_ulong)]


def gpu_latency(count=200, pause=0.01):
    """Ile czeka na GPU malutkie zlecenie (wypełnienie 64 KB), w mikrosekundach.

    Tak samo czeka klatka WindowServera, gdy GPU mieli coś w tle: to jest lag,
    który widać na ekranie. Metal przez ctypes, bez PyObjC. None, gdy GPU nie ma.
    """
    objc = ctypes.CDLL("/usr/lib/libobjc.A.dylib")
    ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
    metal = ctypes.CDLL("/System/Library/Frameworks/Metal.framework/Metal")
    quartz = ctypes.CDLL("/System/Library/Frameworks/QuartzCore.framework/QuartzCore")
    quartz.CACurrentMediaTime.restype = ctypes.c_double
    objc.sel_registerName.restype = ctypes.c_void_p
    objc.sel_registerName.argtypes = [ctypes.c_char_p]
    objc.objc_autoreleasePoolPush.restype = ctypes.c_void_p
    objc.objc_autoreleasePoolPop.argtypes = [ctypes.c_void_p]
    metal.MTLCreateSystemDefaultDevice.restype = ctypes.c_void_p
    msg = objc.objc_msgSend

    def send(obj, sel, *args, restype=ctypes.c_void_p, argtypes=()):
        msg.restype = restype
        msg.argtypes = [ctypes.c_void_p, ctypes.c_void_p] + list(argtypes)
        return msg(obj, objc.sel_registerName(sel.encode()), *args)

    device = metal.MTLCreateSystemDefaultDevice()
    if not device:
        return None
    size = 1 << 16
    queue = send(device, "newCommandQueue")
    buf = send(
        device,
        "newBufferWithLength:options:",
        size,
        0,
        argtypes=[ctypes.c_ulong, ctypes.c_ulong],
    )
    roundtrip, wait = [], []
    for i in range(count):
        pool = objc.objc_autoreleasePoolPush()
        cmd = send(queue, "commandBuffer")
        enc = send(cmd, "blitCommandEncoder")
        send(
            enc,
            "fillBuffer:range:value:",
            buf,
            _NSRange(0, size),
            i & 255,
            restype=None,
            argtypes=[ctypes.c_void_p, _NSRange, ctypes.c_uint8],
        )
        send(enc, "endEncoding", restype=None)
        started = quartz.CACurrentMediaTime()
        send(cmd, "commit", restype=None)
        send(cmd, "waitUntilCompleted", restype=None)
        done = quartz.CACurrentMediaTime()
        gpu_start = send(cmd, "GPUStartTime", restype=ctypes.c_double)
        roundtrip.append((done - started) * 1e6)
        wait.append(max(0.0, gpu_start - started) * 1e6)
        objc.objc_autoreleasePoolPop(pool)
        time.sleep(pause)
    send(buf, "release", restype=None)
    send(queue, "release", restype=None)
    return {
        "roundtrip_p50_us": rnd(percentile(roundtrip, 0.5), 0),
        "roundtrip_p99_us": rnd(percentile(roundtrip, 0.99), 0),
        "wait_p50_us": rnd(percentile(wait, 0.5), 0),
        "wait_p99_us": rnd(percentile(wait, 0.99), 0),
    }


# ---------- pomiar sieci ----------


def default_route():
    """(interfejs, brama) trasy domyślnej."""
    out = janitor.run(["route", "-n", "get", "default"]) or ""
    iface = re.search(r"interface: (\S+)", out)
    gateway = re.search(r"gateway: (\S+)", out)
    return (iface.group(1) if iface else None, gateway.group(1) if gateway else None)


def network_id():
    """Gdzie jesteśmy: interfejs i brama. macOS 27 ukrywa SSID bez zgody na lokalizację
    i tablicę ARP, ale brama wystarcza: 192.168.0.1 to BE230, 10.0.0.1 Zyxel,
    172.20.10.1 hotspot z iPhone'a."""
    iface, gateway = default_route()
    return {"interface": iface, "gateway": gateway}


# porty sprzętowe, którymi Mac idzie przez dane komórkowe telefonu (networksetup)
TETHER_PORTS = ("iPhone USB", "Bluetooth PAN")
HOTSPOT_GATEWAY = "172.20.10.1"  # hotspot z iPhone'a, także po Wi-Fi


def hardware_ports():
    """{urządzenie: port sprzętowy} z networksetup, np. {"en8": "iPhone USB"}."""
    out = janitor.run(["networksetup", "-listallhardwareports"]) or ""
    ports, port = {}, None
    for line in out.splitlines():
        if line.startswith("Hardware Port: "):
            port = line[len("Hardware Port: ") :].strip()
        elif line.startswith("Device: ") and port:
            ports[line[len("Device: ") :].strip()] = port
    return ports


def link_now():
    """Którędy idzie trasa domyślna i czy to tethering z telefonu (2026-10-08: iPhone USB
    z Low Data Mode, upload 16-21 Mb/s, ping 10-248 ms)."""
    iface, gateway = default_route()
    port = hardware_ports().get(iface) if iface else None
    tethered = gateway == HOTSPOT_GATEWAY or bool(
        port and any(port.startswith(p) for p in TETHER_PORTS)
    )
    return {
        "tethered": tethered,
        "port": port,
        "iface": iface,
        "gateway": gateway,
        "at": time.time(),
    }


def interface_tbr(iface):
    """Ogranicznik wysyłania ustawiony na interfejsie (np. "27.00 Mbps") albo None."""
    out = janitor.run(["ifconfig", "-v", iface]) or "" if iface else ""
    # "uplink rate: 25.10 Mbps [eff] / 27.00 Mbps [tbr] / 1.00 Gbps [max]"
    match = re.search(r"uplink rate: .*/ ([\d.]+ [KMG]?bps) \[tbr", out)
    return match.group(1) if match else None


def rpm_ms(rpm):
    """Responsiveness z networkQuality (round-trips per minute) jako opóźnienie w ms."""
    return 60000.0 / rpm if rpm else None


def network_quality(flags=(), timeout=120):
    """Jeden przebieg `networkQuality -s` (najpierw pobieranie, potem wysyłanie)."""
    out = janitor.run(["networkQuality", "-s", "-c", *flags], timeout=timeout)
    try:
        data = json.loads(out)
    except (TypeError, ValueError):
        return None
    return {
        "down_mbps": rnd(data.get("dl_throughput", 0) / MBIT),
        "up_mbps": rnd(data.get("ul_throughput", 0) / MBIT),
        "idle_ms": rnd(median(data.get("il_h2_req_resp") or [])),
        "base_rtt_ms": rnd(data.get("base_rtt")),
        # responsiveness z networkQuality: wszystko naraz, sieć i kolejka w połączeniu
        "down_loaded_ms": rnd(rpm_ms(data.get("dl_responsiveness"))),
        "up_loaded_ms": rnd(rpm_ms(data.get("ul_responsiveness"))),
        # sieć pod obciążeniem: zapytania na osobnych połączeniach, czyli bufor routera
        # i łącza (to leczy ogranicznik wysyłania)
        "down_net_p90_ms": rnd(
            percentile(data.get("lud_foreign_dl_h2_req_resp") or [], 0.9)
        ),
        "up_net_p90_ms": rnd(
            percentile(data.get("lud_foreign_ul_h2_req_resp") or [], 0.9)
        ),
        # kolejka w samym obciążonym połączeniu: bufory gniazd TCP na obu końcach
        "down_self_ms": rnd(median(data.get("lud_self_dl_h2_req_resp") or [])),
        "up_self_ms": rnd(median(data.get("lud_self_ul_h2_req_resp") or [])),
        "interface": data.get("interface_name"),
        "endpoint": data.get("test_endpoint"),
        "ecn": sorted((data.get("other") or {}).get("ecn_values", {})),
        "l4s": sorted((data.get("other") or {}).get("l4s_enablement", {})),
    }


def ping(host, count=40, interval=0.2):
    out = janitor.run(
        ["ping", "-n", "-q", "-c", str(count), "-i", str(interval), host],
        timeout=count * interval + 15,
    )
    if not out:
        return None
    loss = re.search(r"([\d.]+)% packet loss", out)
    rtt = re.search(r"= ([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+) ms", out)
    if not rtt:
        return {"loss_pct": float(loss.group(1)) if loss else 100.0}
    return {
        "loss_pct": float(loss.group(1)) if loss else None,
        "avg_ms": float(rtt.group(2)),
        "max_ms": float(rtt.group(3)),
        "jitter_ms": float(rtt.group(4)),
    }


TTFB_URL = "https://api.anthropic.com/"


def ttfb(url=TTFB_URL, count=8):
    """Mediana czasów nowego połączenia z curl: DNS, TCP, TLS, pierwszy bajt (ms)."""
    fmt = (
        "%{time_namelookup} %{time_connect} %{time_appconnect} %{time_starttransfer}\n"
    )
    rows = []
    for _ in range(count):
        out = janitor.run(
            ["curl", "-s", "-o", "/dev/null", "--max-time", "10", "-w", fmt, url],
            timeout=15,
        )
        try:
            rows.append([float(x) * 1000 for x in out.split()])
        except (AttributeError, ValueError):
            continue
    if not rows:
        return None
    cols = list(zip(*rows))
    return {
        "dns_ms": rnd(median(cols[0])),
        "tcp_ms": rnd(median(cols[1])),
        "tls_ms": rnd(median(cols[2])),
        "ttfb_ms": rnd(median(cols[3])),
    }


def talkers(seconds=3, top=5):
    """Procesy, które w oknie `seconds` najwięcej pobierały i wysyłały (Mb/s z nettop).

    Pomiar łącza przy agentach wysyłających w tle jest zaniżony; ta lista mówi, o ile.
    """

    def snapshot():
        out = (
            janitor.run(
                ["nettop", "-P", "-L", "1", "-x", "-J", "bytes_in,bytes_out"],
                timeout=30,
            )
            or ""
        )
        counts = {}
        for line in out.splitlines()[1:]:
            fields = line.split(",")
            try:
                counts[fields[0]] = (int(fields[1]), int(fields[2]))
            except (IndexError, ValueError):
                continue
        return counts

    first = snapshot()
    started = time.monotonic()
    time.sleep(seconds)
    last = snapshot()
    window = time.monotonic() - started
    rates = []
    for key, (got, sent) in last.items():
        if key not in first:
            continue
        # liczniki nettop spadają, gdy proces zamknie połączenie
        down = max(0, got - first[key][0]) * 8 / window / MBIT
        up = max(0, sent - first[key][1]) * 8 / window / MBIT
        if down + up >= 0.1:
            name = key.rsplit(".", 1)[0].strip()
            # Claude Code to jeden plik nazwany wersją, np. "2.1.289"
            name = "claude" if re.fullmatch(r"\d+\.\d+\.\d+", name) else name
            rates.append({"name": name, "down_mbps": rnd(down), "up_mbps": rnd(up)})
    rates.sort(key=lambda r: -(r["down_mbps"] + r["up_mbps"]))
    return rates[:top]


def bench_network(runs=1, flags=()):
    """networkQuality `runs` razy (mediana każdej liczby), ping i TTFB do API Claude."""
    where = network_id()
    busy = talkers()
    quality = [q for q in (network_quality(flags) for _ in range(runs)) if q]
    result = {"where": where, "talkers": busy}
    # pomiar z włączonym ogranicznikiem nie mówi, ile ma łącze (shaper_rate go pomija)
    shaper = interface_tbr(where["interface"])
    if shaper:
        result["shaper"] = shaper
    if quality:
        for key, value in quality[0].items():
            if isinstance(value, (int, float)):
                result[key] = rnd(
                    median([q[key] for q in quality if q.get(key) is not None])
                )
            else:
                result[key] = value
        result["runs"] = len(quality)
    if where["gateway"]:
        result["gateway_ping"] = ping(where["gateway"])
    result["internet_ping"] = ping("1.1.1.1")
    result["anthropic"] = ttfb()
    return result


# ---------- poprawki ----------

HOME = janitor.HOME
# host agentów (orcahost.py): Orca albo Pod, a obok wszystkie hosty z tego Maca, bo hooki
# w settings.json zostawia każdy, który kiedyś działał
HOSTS = orcahost.known()
HOST = HOSTS[0]
# katalogi hooków statusu z nazwą hosta: ~/.orca/agent-hooks (Pod też go używa; z Pod jako właścicielem
# claude-acc podpisany Pod) i katalog, który host zgłosi sam
HOST_HOOKS = orcahost.hook_hosts(HOSTS)
# zdarzenia, na których hook statusu hosta tylko zgłasza stan sesji (async_hooks niżej)
HOST_HOOK_EVENTS = (
    "PreToolUse",
    "PostToolUse",
    "PostToolUseFailure",
    "UserPromptSubmit",
    "Stop",
    "SubagentStart",
    "SubagentStop",
)


def host_async_hooks(dirs):
    """Wpisy async_hooks dla hooków statusu z każdego katalogu (Orca i Pod mają osobne)."""
    return [
        {"event": event, "match": f"{hooks}/claude-hook"}
        for hooks in dict.fromkeys(dirs)
        for event in HOST_HOOK_EVENTS
    ]


CLAUDE_SETTINGS = os.path.join(HOME, ".claude/settings.json")
CLAUDE_PROJECTS = os.path.join(HOME, ".claude/projects")
DEVGUARD_CONFIG = os.path.join(STATE_DIR, "devguard.json")
DOCKER_SETTINGS = os.path.join(
    HOME, "Library/Group Containers/group.com.docker/settings-store.json"
)
COMPILE_CACHE_DIR = os.path.join(HOME, "Library/Caches/node-compile-cache")
RG_CONFIG = os.path.join(STATE_DIR, "ripgreprc")
DOCKER_CLI = "/Applications/Docker.app/Contents/Resources/bin/docker"
# skrypty hooków dostarczane z perf.py (hooks/ obok niego) i ich kopie w katalogu stanu,
# na które wskazuje settings.json, żeby nie zależał od miejsca, z którego uruchomiono perf.py
REPO_HOOKS = os.path.join(os.path.dirname(os.path.realpath(__file__)), "hooks")
HOOKS_DIR = os.path.join(STATE_DIR, "hooks")
# komenda hooka, którą wolno owinąć: ścieżka i proste argumenty, bez składni powłoki
PLAIN_COMMAND = re.compile(r"[\w./~$@%+=:,-]+(\s+[\w./~$@%+=:,-]+)*")
# hooki pauzy limitów (hook.py, ten sam znacznik co jego MARKER) zostają synchroniczne:
# w tle ich polecenie dla sesji i odmowa dla nowych subagentów przepadają bez śladu
PAUSE_HOOKS = "claude-acc/hook.py"
# ...także wtedy, gdy hook.py wpisał je bez powłoki: claude-acc-pause [tryb] albo (bez programu
# w C) claude-acc-hook z argumentami ["pause", tryb]
NATIVE_HOOK = "claude-acc/claude-acc-hook"
PAUSE_NATIVE = "claude-acc/claude-acc-pause"
# hooki przed każdą komendą Bash, które mają natywny odpowiednik (claude-hooks-native):
# devguard wprost albo przez acc.py, i skrypt rtk, którego instalator rtk sam uznaje za
# przestarzały na rzecz `rtk hook claude`
DEVGUARD_ADMIT = re.compile(r"(?:^|\s)\S*(?:devguard\.py|acc\.py devguard) admit$")
RTK_SCRIPT = re.compile(r"^\S*/rtk-rewrite\.sh$")
RTK_NATIVE = "rtk hook claude"
# hook bez powłoki (exec form, pole `args`, Claude Code od 2.1.139): program podany ścieżką
# bezwzględną albo od $HOME, z prostymi argumentami; bez zmiennych, cudzysłowów i operatorów
EXEC_PROGRAM = re.compile(r"(?:/|\$HOME/|~/)[\w./@%+:,-]+")
EXEC_ARG = re.compile(r"[\w./@%+=:,-]+")
# początek pliku, który execve uruchomi sam: skrypt z #! albo binarka Mach-O
EXEC_MAGIC = (b"#!", b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe", b"\xca\xfe\xba\xbe", b"\xfe\xed\xfa\xcf")
# w zapisie poprzedniej wartości: klucza wcześniej nie było
MISSING = {"__missing__": True}
# `change` w edit_json_file: plik ma zniknąć (powstał przez nas i znowu jest pusty)
DELETE = "delete"

DEFAULT_CONFIG = {
    # procesy pomocnicze, które bez przerwy palą CPU, a nikt na nie nie czeka, dostają
    # QoS tła Darwina: tylko rdzenie energooszczędne i dławione IO. Wzorce to wyrażenia
    # regularne na pełnej linii poleceń. cavemem worker skanuje swoją 2 GB bazę SQLite
    # bez końca (zmierzone 2026-10-04: 24-32% rdzenia P, około 1 W)
    "background": [r"/cavemem/dist/index\.js worker"],
    # hooki Claude Code puszczane w tle ("async": true): nie zwracają nic, na co sesja
    # czeka, a każde wywołanie narzędzia czekało na start node (54 ms p50). Hook Orki tylko
    # zgłasza jej status sesji i zawsze wypisuje {}: 36-39 ms p50 przed i po każdym
    # narzędziu (6.10). SessionStart, SessionEnd i PermissionRequest Orki zostają
    # synchroniczne: przy końcu sesji hook w tle mógłby nie zdążyć
    "async_hooks": [
        {
            "event": "PostToolUse",
            "match": "cavemem/dist/index.js hook run post-tool-use",
        },
        {"event": "Stop", "match": "cavemem/dist/index.js hook run stop"},
    ]
    + host_async_hooks(HOST_HOOKS),
    # limity dev serwerów strażnika w Ultra: procent RAM na wszystkie (strażnik domyślnie
    # ma 35) i GB, powyżej których jeden serwer jest spuchnięty (domyślnie 5)
    "devguard_budget_percent": 25,
    "devguard_max_server_gb": 4,
    # pamięć maszyny Dockera dla `perf.py apply docker-vm` (poza Ultra, tylko na życzenie);
    # zapis tylko przy zamkniętym Dockerze, działa od jego następnego startu
    "docker_memory_mib": 6144,
    # `perf.py apply docker-idle` (poza Ultra, tylko na życzenie): projekty Dockera bez
    # żadnego połączenia z Maca przez tyle godzin stają; 2 h jak stopIdle wspólnych klastrów
    # testpg w portivo (restart ~1 s, szablon budowany od nowa w 10-40 s)
    "docker_idle_hours": 2,
    "docker_idle_keep": [],
    # repozytoria, w których Ultra włącza core.untrackedCache i core.fsmonitor: lista
    # ścieżek albo "orca" (wszystkie repozytoria z worktree w Orce). Domyślnie pusta, bo
    # jedyne takie repo (portivo) zmienia tylko jego właściciel
    "git_repos": [],
    # git-speed globalnie (~/.gitconfig): równoległy checkout (worktree add w repo z 18 tys.
    # plików 2,0 -> 1,55 s) i commit-graph przy każdym fetch (log, merge-base, rebase)
    "git_global": {"checkout.workers": "0", "fetch.writeCommitGraph": "true"},
    # git-speed w repozytoriach z `git_repos`: `git maintenance` (launchd co godzinę prefetch,
    # commit-graph i pliki luzem, codziennie przyrostowy repack), zamiast gc w trakcie pracy
    "git_maintenance": True,
    # interfejs Claude Code w Ultra (~/.claude/settings.json, code.claude.com/docs/en/
    # settings-reference): mniej animacji i bez podpowiedzi pod spinnerem; przy kilku sesjach
    # w jednym oknie Orki (xterm.js) każda klatka animacji to przerysowanie
    "claude_ui": {"prefersReducedMotion": True, "spinnerTipsEnabled": False},
    # hooki Claude Code (fragment komendy), które Ultra uruchamia z szybkim npx: w monorepo
    # pnpm `npx --no-install` szukał formattera 3,5-8,8 s przy każdej edycji
    "npx_fast_hooks": [
        "/.claude/hooks/auto-format.sh",
        "/.claude/hooks/ts-typecheck.sh",
    ],
    # limity Claude Code podnoszone w Ultra (env w ~/.claude/settings.json). Tylko sufity,
    # nie zachowanie: 10-minutowy sufit Bash ściął w 2 doby ~40 poleceń, którym agent sam
    # dał dłuższy timeout (testy Go pod zamkiem, czekanie na buildy na Depot)
    "claude_limits": {"BASH_MAX_TIMEOUT_MS": 3600000, "MAX_MCP_OUTPUT_TOKENS": 50000},
    # rozmiar workflow, na jaki model planuje (small <5, medium <10, large <50 agentów);
    # domyślne medium; twarde limity runtime zostają (docs: code.claude.com/docs/en/workflows)
    "workflow_size_guideline": "large",
    # cache promptu subagentów i członków zespołu (subagentPromptCacheTtl). Domyślne 5 min
    # nie wytrzymuje przerw: 47% zapisów cache subagentów to ponowny zapis 300-900 tys.
    # tokenów kontekstu po 5-60 min ciszy (1-8.10, zanim było 1 h)
    "subagent_prompt_cache_ttl": "1h",
    # env Claude Code tylko na tetheringu z telefonu: auto-updater ściąga ~236 MB na każde
    # wydanie (updates.py zaktualizuje Claude Code na zwykłym łączu), podpowiedzi promptu i
    # streszczenia po powrocie to osobne zapytania do modelu w tle
    # wątki rg dla agentów: na Macu 16 wątków walczy o blokady jądra przy przechodzeniu
    # drzewa (sys 3,9 s na jedno wyszukiwanie w portivo), 4 wychodzą najszybciej
    "rg_threads": 4,
    "tether_env": {
        "DISABLE_AUTOUPDATER": "1",
        "CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION": "false",
        "CLAUDE_CODE_ENABLE_AWAY_SUMMARY": "0",
    },
    # katalogi, które Spotlight indeksuje bez potrzeby; wykluczenie jest tylko w Ustawieniach
    "spotlight_noise": ["~/Library/pnpm", "~/go"],
    # drzewo do pomiaru `bench fs` (lstat wszystkiego, dwa przebiegi)
    "fs_bench_path": "~/Documents/portivo-app/Untitled/node_modules/.pnpm",
    # ogranicznik wysyłania z perf-root.sh dostaje tyle procent zmierzonego uploadu
    "shaper_percent": 90,
}

PRIO_DARWIN_PROCESS = 4
PRIO_DARWIN_BG = 0x1000
BACKGROUND_PRI = 4  # tak ps pokazuje proces z PRIO_DARWIN_BG


class System:
    """Wszystko, czym poprawki dotykają systemu; testy podstawiają atrapę."""

    def processes(self):
        """[(pid, start, linia poleceń)] procesów tego użytkownika."""
        found = []
        for pid, command in own_processes().items():
            info = rusage(pid)
            if info:
                found.append((pid, info["start"], command))
        return found

    def start(self, pid):
        info = rusage(pid)
        return info["start"] if info else None

    def background(self, pid):
        """Czy proces ma QoS tła; jądro nie zdradza tej flagi, ale ps widzi priorytet 4."""
        out = janitor.run(["ps", "-o", "pri=", "-p", str(pid)]) or ""
        return out.strip() == str(BACKGROUND_PRI)

    def set_background(self, pid, on):
        try:
            os.setpriority(PRIO_DARWIN_PROCESS, pid, PRIO_DARWIN_BG if on else 0)
        except OSError:
            return False
        return True

    def link(self):
        return link_now()

    def claude_plugin(self, *args):
        """`claude plugin ...` tak, jak robi to człowiek: (kod wyjścia, wyjście)."""
        claude = (
            os.environ.get("CLAUDE_ACC_CLAUDE_BIN")
            or janitor.which("claude")
            or os.path.join(HOME, ".local/bin/claude")
        )
        try:
            done = subprocess.run(
                [claude, "plugin", *args],
                capture_output=True,
                text=True,
                timeout=180,
                env=janitor.ENV,
                stdin=subprocess.DEVNULL,
            )
        except (OSError, subprocess.TimeoutExpired) as err:
            return 127, str(err)
        return done.returncode, (done.stdout or "") + (done.stderr or "")

    def docker_running(self):
        """Docker Desktop trzyma ustawienia w pamięci i nadpisuje plik; piszemy tylko bez niego."""
        out = janitor.run(["pgrep", "-x", "com.docker.backend"])
        return bool(out and out.strip())

    def git(self, repo, *args):
        """Wyjście gita albo None (brak klucza w configu to też None)."""
        return janitor.run(["git", "-C", repo, *args], timeout=120)

    def git_global(self, *args):
        """Wyjście gita bez repozytorium (config --global) albo None."""
        return janitor.run(["git", *args], timeout=120)

    def maintenance_scheduled(self):
        """Czy launchd ma już harmonogram `git maintenance` (plisty org.git-scm.git.*)."""
        return os.path.exists(GIT_MAINTENANCE_PLIST)

    def maintenance_schedule(self, repo, on):
        """`git maintenance start` (rejestracja repo i harmonogram launchd) albo `stop`."""
        if on:
            return janitor.run(["git", "-C", repo, "maintenance", "start"], timeout=120)
        return janitor.run(["git", "maintenance", "stop"], timeout=120)

    def rtk_path(self):
        """Ścieżka rtk z wbudowanym hookiem Claude Code albo None."""
        return janitor.which("rtk") if self.rtk_hook() else None

    def rtk_hook(self):
        """Czy rtk ma wbudowany hook Claude Code: `rtk hook claude` czyta zdarzenie ze stdin."""
        rtk = janitor.which("rtk")
        if not rtk:
            return False
        try:
            done = subprocess.run(
                [rtk, "hook", "claude"],
                input='{"tool_name": "Bash", "tool_input": {"command": "true"}}',
                capture_output=True,
                text=True,
                timeout=10,
                env=janitor.ENV,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return done.returncode == 0

    def docker_containers(self):
        """Działające kontenery: [{"id", "name", "project", "ports", "started"}]; [] bez Dockera."""
        cli = janitor.which("docker") or DOCKER_CLI
        ids = (janitor.run([cli, "ps", "-q"], timeout=20) or "").split()
        if not ids:
            return []
        try:
            data = json.loads(janitor.run([cli, "inspect", *ids], timeout=30) or "[]")
        except ValueError:
            return []
        found = []
        for c in data:
            labels = (c.get("Config") or {}).get("Labels") or {}
            test = ((c.get("Config") or {}).get("Healthcheck") or {}).get("Test") or []
            # healthcheck idzie przez exec tak jak psql agenta; jego komenda w zdarzeniu
            # to "/bin/sh -c <cmd>" (CMD-SHELL) albo argumenty po spacji (CMD)
            if test[:1] == ["CMD-SHELL"]:
                health = "/bin/sh -c " + " ".join(test[1:])
            elif test[:1] == ["CMD"]:
                health = " ".join(test[1:])
            else:
                health = None
            ports = set()
            for binds in ((c.get("NetworkSettings") or {}).get("Ports") or {}).values():
                for bind in binds or []:
                    if str(bind.get("HostPort", "")).isdigit():
                        ports.add(int(bind["HostPort"]))
            found.append(
                {
                    "id": c.get("Id", "")[:12],
                    "name": (c.get("Name") or "").lstrip("/"),
                    "project": labels.get("com.docker.compose.project")
                    or (c.get("Name") or "").lstrip("/"),
                    "ports": sorted(ports),
                    "started": iso_epoch((c.get("State") or {}).get("StartedAt")) or 0,
                    "health": health,
                }
            )
        return found

    def connected_ports(self):
        """Porty, do których ktoś na Macu ma teraz otwarte połączenie TCP (strona klienta;
        backend Dockera, który trzyma drugi koniec, się nie liczy)."""
        out = janitor.run(["lsof", "-nP", "-iTCP", "-sTCP:ESTABLISHED", "-F", "cn"], timeout=30) or ""
        ports, command = set(), ""
        for line in out.splitlines():
            if line.startswith("c"):
                command = line[1:]
            elif line.startswith("n") and "->" in line and not command.startswith(("com.docke", "vpnkit")):
                remote = line.rsplit("->", 1)[1]
                port = remote.rsplit(":", 1)[-1]
                if port.isdigit():
                    ports.add(int(port))
        return ports

    def docker_execs(self, since):
        """{(kontener, komenda)} z `docker exec` od `since`: psql agentów, ale też healthchecki."""
        cli = janitor.which("docker") or DOCKER_CLI
        out = janitor.run(
            [cli, "events", "--since", str(int(since)), "--until", str(int(time.time())),
             "--filter", "type=container", "--filter", "event=exec_start",
             "--format", "{{.Actor.Attributes.name}}\t{{.Action}}"],
            timeout=30,
        ) or ""
        found = set()
        for line in out.splitlines():
            name, _, action = line.partition("\t")
            if name.strip():
                found.add((name.strip(), action.partition("exec_start: ")[2].strip()))
        return found

    def docker_stop(self, ids):
        cli = janitor.which("docker") or DOCKER_CLI
        return janitor.run([cli, "stop", "-t", "30", *ids], timeout=300) is not None

    def docker_start(self, ids):
        cli = janitor.which("docker") or DOCKER_CLI
        return janitor.run([cli, "start", *ids], timeout=300) is not None

    def docker_memory(self):
        """Pamięć maszyny Dockera w bajtach z `docker info`, gdy Docker działa."""
        cli = janitor.which("docker") or DOCKER_CLI
        out = janitor.run([cli, "info", "--format", "{{.MemTotal}}"], timeout=20)
        try:
            return int(out.strip())
        except (AttributeError, ValueError):
            return None


# ---------- pliki JSON cudzych narzędzi ----------


def read_json_file(path):
    """(dane, mtime); brak pliku to ({}, None)."""
    try:
        with open(path) as f:
            return json.load(f), os.stat(path).st_mtime_ns
    except FileNotFoundError:
        return {}, None


def edit_json_file(path, change, indent=2):
    """Czyta, zmienia i zapisuje atomowo, tylko gdy `change(data)` zwróci True (DELETE
    usuwa plik). Gdy ktoś zapisał plik w międzyczasie (Claude Code potrafi), zaczyna od
    nowa, więc `change` musi dać się powtórzyć."""
    for _ in range(5):
        data, stamp = read_json_file(path)
        result = change(data)
        if not result:
            return result
        _, now = read_json_file(path)
        if now != stamp:
            continue
        if result == DELETE:
            os.remove(path)
            return result
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{os.getpid()}.perf-tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=indent, ensure_ascii=False)
            if trailing_newline(path):
                f.write("\n")
        if stamp is not None:
            os.chmod(tmp, os.stat(path).st_mode & 0o7777)
        os.replace(tmp, path)
        return result
    raise RuntimeError(f"{path} zmienia się bez przerwy, spróbuj później")


def trailing_newline(path):
    """Czy plik kończy się nową linią (nowy plik: tak), żeby zapis nie zmieniał nic poza kluczem."""
    try:
        with open(path, "rb") as f:
            f.seek(-1, os.SEEK_END)
            return f.read(1) == b"\n"
    except FileNotFoundError:
        return True
    except OSError:
        return False  # pusty plik


def restore_key(data, key, previous, ours):
    """Przywraca klucz, ale tylko gdy dalej ma naszą wartość; inaczej ktoś go zmienił
    po nas i to jego decyzja zostaje."""
    if data.get(key, MISSING) != ours:
        return False
    if previous == MISSING:
        data.pop(key, None)
    else:
        data[key] = previous
    return True


class BackgroundHelpers:
    name = "bg-helpers"
    group = "cpu"
    root = False
    title = "procesy pomocnicze z listy `background` w tle (rdzenie E, dławione IO)"
    effect = (
        "cavemem worker: rdzeń P 24-32% -> 0,2%, energia 0,9-1,25 W -> 0,1-0,19 W; "
        "ta sama praca trwa na rdzeniach E 2x dłużej (2026-10-04)"
    )

    def targets(self, cfg, system):
        patterns = [re.compile(p) for p in cfg.get("background", [])]
        me = os.getpid()
        return [
            (pid, start, command)
            for pid, start, command in system.processes()
            if pid != me and any(p.search(command) for p in patterns)
        ]

    def apply(self, cfg, system, record=None):
        """Nowe procesy z listy w tle; record trzyma, co trzeba cofnąć (bez tych,
        które były w tle już wcześniej). Martwe wpisy wypadają."""
        kept = [
            entry
            for entry in (record or {}).get("procs", [])
            if system.start(entry["pid"]) == entry["start"]
        ]
        known = {(e["pid"], e["start"]) for e in kept}
        changed = []
        for pid, start, command in self.targets(cfg, system):
            if (pid, start) in known:
                if not system.background(pid):
                    system.set_background(pid, True)
                    changed.append(command)
                continue
            if system.background(pid):
                continue  # ktoś inny dał go do tła; cofnięcie nie jest nasze
            if system.set_background(pid, True):
                kept.append({"pid": pid, "start": start, "command": command[:200]})
                changed.append(command)
        return {"procs": kept}, [short_command(c) for c in changed]

    def undo(self, record, system):
        restored = []
        for entry in record.get("procs", []):
            if system.start(entry["pid"]) != entry["start"]:
                continue
            if system.set_background(entry["pid"], False):
                restored.append(short_command(entry["command"]))
        return restored

    def describe(self, record, system):
        alive = [
            e
            for e in (record or {}).get("procs", [])
            if system.start(e["pid"]) == e["start"]
        ]
        if not alive:
            return "brak działających procesów z listy"
        return ", ".join(f"{short_command(e['command'])} ({e['pid']})" for e in alive)

    def measure(self, cfg, system, seconds=3):
        """Ile rdzenia P (%) zjadają teraz procesy z listy; None, gdy żadnego nie ma."""
        targets = self.targets(cfg, system)
        first = {pid: rusage(pid) for pid, _, _ in targets}
        if not any(first.values()):
            return None
        time.sleep(seconds)
        total = 0.0
        for pid, info in first.items():
            last = rusage(pid)
            if info and last and last["start"] == info["start"]:
                total += last["pcpu"] - info["pcpu"]
        return rnd(total / seconds * 100)


class AsyncHooks:
    """Hooki Claude Code w tle: sesja nie czeka na ich koniec przy każdym narzędziu."""

    name = "claude-hooks-async"
    group = "claude"
    root = False
    title = (
        "hooki z listy `async_hooks` w ~/.claude/settings.json puszczone w tle (async)"
    )
    effect = (
        "PostToolUse czekał na start node w hooku cavemem: 54 ms p50, 99 ms p90 na każde "
        "narzędzie; hook Orki 36-39 ms p50 przed i po każdym narzędziu (transkrypty, 6.10)"
    )
    path = None  # testy podstawiają plik; None to CLAUDE_SETTINGS

    def settings_path(self):
        return self.path or CLAUDE_SETTINGS

    def matching(self, data, cfg):
        """[(zdarzenie, hook)] pasujące do listy, wprost ze struktury settings.json."""
        found = []
        for spec in cfg.get("async_hooks", []):
            for group in (data.get("hooks") or {}).get(spec["event"], []) or []:
                for hook in group.get("hooks", []) or []:
                    command = hook.get("command", "")
                    if spec["match"] in command and not pause_hook(hook):
                        found.append((spec["event"], hook))
        return found

    def apply(self, cfg, system, record=None):
        entries = list((record or {}).get("hooks", []))
        changed = []

        def change(data):
            del changed[:]
            known = {(e["event"], e["command"]) for e in entries}
            for event, hook in self.matching(data, cfg):
                if hook.get("async") is True:
                    continue
                if (event, hook["command"]) not in known:
                    entries.append(
                        {
                            "event": event,
                            "command": hook["command"],
                            "prev": hook.get("async", MISSING),
                        }
                    )
                hook["async"] = True
                changed.append(f"{event}: {hook_label(hook['command'])}")
            return bool(changed)

        if os.path.exists(self.settings_path()):
            edit_json_file(self.settings_path(), change)
        return {"hooks": entries}, changed

    def undo(self, record, system):
        restored = []
        wanted = {
            (e["event"], e["command"]): e["prev"] for e in record.get("hooks", [])
        }

        def change(data):
            del restored[:]
            for event, groups in (data.get("hooks") or {}).items():
                for group in groups or []:
                    for hook in group.get("hooks", []) or []:
                        key = (event, hook.get("command"))
                        if key not in wanted:
                            continue
                        if restore_key(hook, "async", wanted[key], True):
                            restored.append(f"{event}: {hook_label(hook['command'])}")
            return bool(restored)

        if wanted and os.path.exists(self.settings_path()):
            edit_json_file(self.settings_path(), change)
        return restored

    def describe(self, record, system):
        hooks = (record or {}).get("hooks", [])
        if not hooks:
            return "brak pasujących hooków"
        return ", ".join(f"{e['event']}: {hook_label(e['command'])}" for e in hooks)


def hook_label(command):
    if "cavemem" in command and "hook run " in command:
        return "cavemem " + command.split("hook run ")[-1].split()[0]
    hooks = next((d for d in HOST_HOOKS if f"{d}/" in command), None)
    if hooks:
        return HOST_HOOKS[hooks]
    # transkrypt zapisuje hook bez powłoki jako program i argumenty po spacji
    if PAUSE_HOOKS in command or f"{NATIVE_HOOK} pause " in command or f"{PAUSE_NATIVE} " in command:
        return "pauza limitów"
    parts = command.split()
    # program z podkomendą (`fasthooks read-guard`, `rtk hook claude`): nazwa i podkomenda
    if len(parts) > 1 and re.fullmatch(r"[a-z][\w-]*", parts[1]) and "." not in os.path.basename(parts[0]):
        return f"{os.path.basename(parts[0])} {parts[1]}"
    return short_command(command)


class ClaudeEnv:
    """Zmienna środowiskowa dla sesji Claude Code i wszystkiego, co uruchamiają (env w
    ~/.claude/settings.json). Działa w sesjach otwartych po zmianie."""

    root = False
    group = "claude"
    path = None

    def __init__(self, name, var, value, title, effect):
        self.name, self.var, self.value = name, var, value
        self.title, self.effect = title, effect

    def settings_path(self):
        return self.path or CLAUDE_SETTINGS

    def apply(self, cfg, system, record=None):
        if not os.path.exists(self.settings_path()):
            return dict(record or {}), []
        seen = {}

        def change(data):
            env = data.get("env") or {}
            seen["prev"] = env.get(self.var, MISSING)
            if seen["prev"] == self.value:
                return False
            env[self.var] = self.value
            data["env"] = env
            return True

        edit_json_file(self.settings_path(), change)
        changed = [] if seen["prev"] == self.value else [f"{self.var}={self.value}"]
        if record and "prev" in record:
            return dict(
                record
            ), changed  # poprzednia wartość zapisana przy pierwszym razie
        return {"value": self.value, "prev": seen["prev"]}, changed

    def undo(self, record, system):
        restored = []
        if record.get("prev") == record.get("value"):
            return restored  # wartość była taka sama przed nami

        def change(data):
            env = data.get("env") or {}
            if not restore_key(env, self.var, record["prev"], record["value"]):
                return False
            restored.append(self.var)
            if env:
                data["env"] = env
            else:
                data.pop("env", None)
            return True

        if os.path.exists(self.settings_path()):
            edit_json_file(self.settings_path(), change)
        return restored

    def describe(self, record, system):
        return f"{self.var}={self.value}"


class RipgrepThreads(ClaudeEnv):
    """Plik konfiguracji rg z `--threads` i RIPGREP_CONFIG_PATH w env sesji Claude Code:
    rg z Bash agentów i wbudowany Grep czytają go przy każdym starcie, jawne `-j` wygrywa."""

    def __init__(self):
        super().__init__(
            "rg-threads",
            "RIPGREP_CONFIG_PATH",
            RG_CONFIG,
            "rg agentów na 4 wątkach zamiast 16 (RIPGREP_CONFIG_PATH w env "
            "~/.claude/settings.json, plik ripgreprc w katalogu claude-acc)",
            "portivo (17,7 tys. plików): rg -n 428 ms, 3,9 s czasu jądra -> 179 ms, 0,7 s "
            "z -j4 (-j2 242, -j6 272, -j12 434 ms); claude-acc 6,5 -> 5,8 ms; ~4300 wywołań "
            "rg/grep na dobę (2026-10-08)",
        )

    def contents(self, cfg):
        return f"# claude-acc perf rg-threads\n--threads={int(cfg['rg_threads'])}\n"

    def apply(self, cfg, system, record=None):
        wanted = self.contents(cfg)
        try:
            with open(self.value) as f:
                current = f.read()
        except FileNotFoundError:
            current = None
        if current is not None and not current.startswith("# claude-acc perf rg-threads"):
            return dict(record or {}), []  # cudzy plik pod tą ścieżką: nie ruszamy
        changed = []
        if current != wanted:
            os.makedirs(os.path.dirname(self.value), exist_ok=True)
            tmp = f"{self.value}.{os.getpid()}.perf-tmp"
            with open(tmp, "w") as f:
                f.write(wanted)
            os.replace(tmp, self.value)
            changed.append(wanted.splitlines()[-1])
        record, more = super().apply(cfg, system, record)
        return record, changed + more

    def undo(self, record, system):
        restored = super().undo(record, system)
        try:
            with open(self.value) as f:
                ours = f.read().startswith("# claude-acc perf rg-threads")
            if ours:
                os.remove(self.value)
                restored.append(os.path.basename(self.value))
        except FileNotFoundError:
            pass
        return restored


class JsonSetting:
    """Jeden klucz w pliku JSON innego narzędzia, z dokładnym cofnięciem."""

    root = False

    def __init__(self, name, group, path, key, config_key, title, effect, defer=False):
        self.name, self.group, self.path, self.key = name, group, path, key
        self.config_key, self.defer = config_key, defer
        self.title, self.effect = title, effect

    def blocked(self, system):
        """Docker nadpisuje swój plik ustawień; dopóki działa, zapis czeka."""
        return self.defer and system.docker_running()

    def apply(self, cfg, system, record=None):
        value = cfg[self.config_key]
        if self.blocked(system):
            if record and record.get("written"):
                return dict(record), []  # już zapisane; zmiana wartości poczeka
            return dict(record or {}, value=value, written=False), []
        seen = {}

        def change(data):
            seen["prev"] = data.get(self.key, MISSING)
            seen["created"] = not os.path.exists(self.path)
            if seen["prev"] == value:
                return False
            data[self.key] = value
            return True

        edit_json_file(self.path, change)
        if record and record.get("written"):
            prev, created = record["prev"], record.get("created", False)
        else:
            prev, created = seen["prev"], seen["created"]
        changed = [] if seen["prev"] == value else [f"{self.key}={value}"]
        result = {"value": value, "prev": prev, "created": created, "written": True}
        if record and "active" in record:
            result["active"] = record["active"]
        return result, changed

    def undo(self, record, system):
        if not record.get("written") or record.get("prev") == record.get("value"):
            return []
        if self.blocked(system):
            raise Deferred(self.name)
        restored = []

        def change(data):
            if not restore_key(data, self.key, record["prev"], record["value"]):
                return False
            restored.append(self.key)
            return DELETE if record.get("created") and not data else True

        if os.path.exists(self.path):
            edit_json_file(self.path, change)
        return restored

    def describe(self, record, system):
        record = record or {}
        if not record.get("written"):
            return f"{self.key}={record.get('value')} czeka na zamknięcie Dockera"
        return f"{self.key}={record.get('value')}"


class DockerIdle:
    """Projekty Dockera (compose albo pojedynczy kontener), do których nikt z Maca się nie
    łączy, zatrzymane po `docker_idle_hours`: VM Dockera oddaje CPU i pamięć, a Resource
    Saver usypia ją, gdy nic już nie działa. `docker start` przywraca je w kilka sekund."""

    name = "docker-idle"
    group = "docker"
    root = False
    title = (
        "zatrzymanie projektów Dockera bez połączeń z Maca od `docker_idle_hours` "
        "(compose albo kontener; poza `docker_idle_keep`)"
    )
    effect = (
        "VM Dockera 8,6 GB RSS i 55% CPU przy 26 kontenerach, do których od godzin nikt się "
        "nie łączył (portivo od 8 h: 0 połączeń poza mailpit); wspólne klastry testpg same "
        "zatrzymują się po 2 h dopiero przy następnym teście (2026-10-08)"
    )

    def apply(self, cfg, system, record=None):
        checked = (record or {}).get("checked")
        record = {
            "seen": dict((record or {}).get("seen", {})),
            "stopped": dict((record or {}).get("stopped", {})),
            "checked": time.time(),
        }
        containers = system.docker_containers()
        if not containers:
            return record, []
        now, limit = record["checked"], float(cfg["docker_idle_hours"]) * 3600
        keep = set(cfg.get("docker_idle_keep") or [])
        # połączenie otwarte w chwili sprawdzenia albo `docker exec` od poprzedniego
        # sprawdzenia; krótkie połączenia między próbkami co 5 min umykają, stąd zapas godzin
        used = system.connected_ports()
        execs = system.docker_execs(checked or now - 300)
        projects = {}
        for c in containers:
            projects.setdefault(c["project"], []).append(c)
        changed = []
        for project, items in projects.items():
            # ktoś go znowu uruchomił: to już nie nasze zatrzymanie
            record["stopped"].pop(project, None)
            # zegar bezczynności rusza od pierwszego spojrzenia, nie od startu kontenera:
            # bez historii połączeń nie wiadomo, co działo się wcześniej
            last = max([record["seen"].get(project, now)] + [c["started"] for c in items])
            if any(port in used for c in items for port in c["ports"]) or any(
                name == c["name"] and command != c.get("health")
                for c in items
                for name, command in execs
            ):
                last = now
            record["seen"][project] = last
            if project in keep or now - last < limit:
                continue
            ids = [c["id"] for c in items]
            if system.docker_stop(ids):
                record["stopped"][project] = {"ids": ids, "at": now}
                hours = (now - last) / 3600
                changed.append(f"{project}: zatrzymany ({len(ids)} kontenerów, {hours:.1f} h bez połączeń)")
        # projekty, które zniknęły (usunięte), nie wiszą w stanie
        record["seen"] = {k: v for k, v in record["seen"].items() if k in projects or k in record["stopped"]}
        return record, changed

    def undo(self, record, system):
        running = {c["id"] for c in system.docker_containers()}
        restored = []
        for project, entry in (record or {}).get("stopped", {}).items():
            ids = [i for i in entry.get("ids", []) if i not in running]
            if ids and system.docker_start(ids):
                restored.append(project)
        return restored

    def describe(self, record, system):
        stopped = (record or {}).get("stopped", {})
        if not stopped:
            return f"pilnuje {len((record or {}).get('seen', {}))} projektów, nic nie zatrzymane"
        return "zatrzymane: " + ", ".join(
            f"{name} ({ago(entry['at'])}; docker start {' '.join(entry['ids'][:3])}"
            f"{' …' if len(entry['ids']) > 3 else ''})"
            for name, entry in stopped.items()
        )


class Deferred(Exception):
    """Cofnięcie musi poczekać (Docker działa); `keep` dokończy je później."""


ORCA_DATA = os.path.join(HOST.user_data, HOST.data_file)
ORCA_APP = HOST.app
# `git maintenance start` stawia harmonogram jako LaunchAgenty org.git-scm.git.{hourly,daily,weekly}
GIT_MAINTENANCE_PLIST = os.path.join(HOME, "Library/LaunchAgents/org.git-scm.git.hourly.plist")


def git_repos(cfg):
    """Ścieżki z `git_repos`; "orca" to wszystkie repozytoria, z których Orka robi worktree."""
    wanted = cfg.get("git_repos") or []
    if wanted == "orca":
        data, _ = read_json_file(ORCA_DATA)
        repos = data.get("repos") or []
        items = repos.values() if isinstance(repos, dict) else repos
        return [r["path"] for r in items if isinstance(r, dict) and r.get("path")]
    return [os.path.expanduser(r) for r in wanted]


class GitSpeed:
    """core.untrackedCache, core.fsmonitor i `git maintenance` w repozytoriach z listy
    `git_repos`, a globalnie klucze z `git_global` (równoległy checkout, commit-graph)."""

    name = "git-speed"
    group = "dev"
    root = False
    title = (
        "git bez skanowania drzewa: untrackedCache + fsmonitor i `git maintenance` (repozytoria "
        "z `git_repos`), globalnie równoległy checkout i commit-graph przy fetch"
    )
    effect = (
        "klon portivo (14 tys. plików): git status 71 -> 31 ms z untrackedCache, 26 ms z "
        "fsmonitor; worktree add (18 tys. plików) 2,0 -> 1,55 s z checkout.workers=0; "
        "feature.manyFiles (index v4, skipHash) nic nie dodał, commit-graph pomaga logowi, nie statusowi"
    )
    KEYS = (("core.untrackedCache", "true"), ("core.fsmonitor", "true"))
    # `git maintenance register` ustawia je w repo (auto=false wyłącza gc --auto po poleceniach)
    MAINTENANCE_KEYS = ("maintenance.auto", "maintenance.strategy")

    @staticmethod
    def stripped(out):
        return out.strip() if out is not None else None

    @staticmethod
    def drop_empty(system, section, repo=None):
        """Usuwa sekcję, z której cofnięcie zabrało ostatni klucz (git zostawia pusty nagłówek)."""
        if repo is None:
            run, scope = system.git_global, "--global"
        else:
            run, scope = (lambda *a: system.git(repo, *a)), "--local"
        if not run("config", scope, "--get-regexp", f"^{section}\\."):
            run("config", scope, "--remove-section", section)

    def registered(self, system):
        """Repozytoria z `maintenance.repo` w globalnym configu (ścieżki rzeczywiste)."""
        out = system.git_global("config", "--global", "--get-all", "maintenance.repo") or ""
        return {os.path.realpath(line) for line in out.splitlines() if line.strip()}

    def apply(self, cfg, system, record=None):
        record = record or {}
        repos = dict(record.get("repos", {}))
        changed = []
        for repo in git_repos(cfg):
            if repo in repos or system.git(repo, "rev-parse", "--git-dir") is None:
                continue
            prev = {}
            for key, value in self.KEYS:
                prev[key] = self.stripped(system.git(repo, "config", "--local", "--get", key))
                system.git(repo, "config", "--local", key, value)
            system.git(repo, "update-index", "--untracked-cache")
            repos[repo] = prev
            changed.append(repo)
        # globalne klucze: poprzednia wartość z pierwszego razu, ponowne ustawienie, gdy ktoś
        # nadpisał ~/.gitconfig (keep), jak przy settings.json
        found = dict(record.get("global", {}))
        for key, value in (cfg.get("git_global") or {}).items():
            value = str(value)
            current = self.stripped(system.git_global("config", "--global", "--get", key))
            if key not in found or found[key]["value"] != value:
                found[key] = {"value": value, "prev": found[key]["prev"] if key in found else current}
            if current != value:
                system.git_global("config", "--global", key, value)
                changed.append(f"{key}={value}")
        maintenance = dict(record.get("maintenance", {}))
        scheduled = record.get("scheduled", False)
        if cfg.get("git_maintenance", True):
            registered = self.registered(system)
            for repo in repos:
                if repo in maintenance:
                    continue
                prev = {
                    key: self.stripped(system.git(repo, "config", "--local", "--get", key))
                    for key in self.MAINTENANCE_KEYS
                }
                before = os.path.realpath(repo) in registered
                if not system.maintenance_scheduled():
                    # start rejestruje repo i stawia harmonogram; zdejmujemy go tylko, gdy był nasz
                    system.maintenance_schedule(repo, True)
                    scheduled = scheduled or system.maintenance_scheduled()
                elif not before:
                    system.git(repo, "maintenance", "register")
                maintenance[repo] = {"registered": before, "prev": prev}
                if not before:
                    changed.append(f"git maintenance: {short_path(repo)}")
        result = {"repos": repos, "global": found, "maintenance": maintenance}
        if scheduled:
            result["scheduled"] = True
        return result, changed

    def undo(self, record, system):
        restored = []
        for repo, prev in record.get("repos", {}).items():
            system.git(repo, "fsmonitor--daemon", "stop")
            for key, _ in self.KEYS:
                if prev.get(key) is None:
                    system.git(repo, "config", "--local", "--unset", key)
                else:
                    system.git(repo, "config", "--local", key, prev[key])
            if prev.get("core.untrackedCache") is None:
                system.git(repo, "update-index", "--no-untracked-cache")
            restored.append(repo)
        for repo, entry in record.get("maintenance", {}).items():
            if entry.get("registered"):
                continue  # było w maintenance przed nami
            system.git(repo, "maintenance", "unregister")
            for key, value in entry.get("prev", {}).items():
                if value is None:
                    system.git(repo, "config", "--local", "--unset", key)
                else:
                    system.git(repo, "config", "--local", key, value)
            self.drop_empty(system, "maintenance", repo)
            self.drop_empty(system, "maintenance")
            restored.append(f"git maintenance: {short_path(repo)}")
        if record.get("scheduled") and not self.registered(system):
            system.maintenance_schedule(None, False)  # nasz harmonogram, nikt już z niego nie korzysta
        for key, entry in record.get("global", {}).items():
            if entry["prev"] == entry["value"]:
                continue
            current = self.stripped(system.git_global("config", "--global", "--get", key))
            if current != entry["value"]:
                continue  # ktoś zmienił po nas: jego wartość zostaje
            if entry["prev"] is None:
                system.git_global("config", "--global", "--unset", key)
                self.drop_empty(system, key.rsplit(".", 1)[0])
            else:
                system.git_global("config", "--global", key, entry["prev"])
            restored.append(key)
        return restored

    def describe(self, record, system):
        record = record or {}
        parts = [", ".join(short_path(r) for r in record.get("repos", {})) or "brak repozytoriów w `git_repos`"]
        parts += [f"{k}={v['value']}" for k, v in record.get("global", {}).items()]
        ours = [r for r, e in record.get("maintenance", {}).items() if not e.get("registered")]
        if ours:
            parts.append(f"git maintenance: {len(ours)} repo")
        return "; ".join(parts)


class ClaudeEnvSet:
    """Kilka zmiennych w env ~/.claude/settings.json naraz, każda z własnym cofnięciem."""

    root = False
    group = "claude"
    path = None

    def __init__(self, name, config_key, title, effect):
        self.name, self.config_key, self.title, self.effect = (
            name,
            config_key,
            title,
            effect,
        )

    def settings_path(self):
        return self.path or CLAUDE_SETTINGS

    def apply(self, cfg, system, record=None):
        wanted = {k: str(v) for k, v in cfg.get(self.config_key, {}).items()}
        known = dict((record or {}).get("vars", {}))
        changed = []
        if not os.path.exists(self.settings_path()):
            return {"vars": known}, []

        def change(data):
            del changed[:]
            env = data.get("env") or {}
            for var, value in wanted.items():
                current = env.get(var, MISSING)
                if current == value:
                    known.setdefault(var, {"value": value, "prev": value})
                    continue
                if var not in known or known[var]["value"] != value:
                    prev = known[var]["prev"] if var in known else current
                    known[var] = {"value": value, "prev": prev}
                env[var] = value
                changed.append(f"{var}={value}")
            if changed:
                data["env"] = env
            return bool(changed)

        edit_json_file(self.settings_path(), change)
        return {"vars": known}, changed

    def undo(self, record, system):
        restored = []
        todo = {
            var: entry
            for var, entry in record.get("vars", {}).items()
            if entry["prev"] != entry["value"]
        }

        def change(data):
            del restored[:]
            env = data.get("env") or {}
            for var, entry in todo.items():
                if restore_key(env, var, entry["prev"], entry["value"]):
                    restored.append(var)
            if env:
                data["env"] = env
            else:
                data.pop("env", None)
            return bool(restored)

        if todo and os.path.exists(self.settings_path()):
            edit_json_file(self.settings_path(), change)
        return restored

    def describe(self, record, system):
        found = (record or {}).get("vars", {})
        return ", ".join(f"{k}={v['value']}" for k, v in found.items()) or "nic"


class ClaudeUi:
    """Klucze interfejsu Claude Code z `claude_ui` na najwyższym poziomie ~/.claude/settings.json,
    każdy z własnym cofnięciem. Działa w sesjach otwartych po zmianie."""

    name = "claude-ui"
    group = "claude"
    root = False
    path = None
    title = (
        "spokojniejszy interfejs Claude Code z `claude_ui`: prefersReducedMotion (spinner, shimmer "
        "i błyski ograniczone) i spinnerTipsEnabled false (bez podpowiedzi pod spinnerem)"
    )
    effect = (
        "polityka, nie pomiar: przy kilku sesjach w jednym oknie Orki (xterm.js) każda klatka "
        "animacji to przerysowanie panelu; klucze z code.claude.com/docs/en/settings-reference"
    )

    def settings_path(self):
        return self.path or CLAUDE_SETTINGS

    def apply(self, cfg, system, record=None):
        wanted = dict(cfg.get("claude_ui") or {})
        known = dict((record or {}).get("keys", {}))
        changed = []
        if not os.path.exists(self.settings_path()):
            return {"keys": known}, []

        def change(data):
            del changed[:]
            for key, value in wanted.items():
                current = data.get(key, MISSING)
                if current == value and type(current) is type(value):
                    known.setdefault(key, {"value": value, "prev": value})
                    continue
                if key not in known or known[key]["value"] != value:
                    known[key] = {"value": value, "prev": known[key]["prev"] if key in known else current}
                data[key] = value
                changed.append(f"{key}={json.dumps(value)}")
            return bool(changed)

        edit_json_file(self.settings_path(), change)
        return {"keys": known}, changed

    def undo(self, record, system):
        restored = []
        todo = {k: e for k, e in record.get("keys", {}).items() if e["prev"] != e["value"]}

        def change(data):
            del restored[:]
            for key, entry in todo.items():
                if restore_key(data, key, entry["prev"], entry["value"]):
                    restored.append(key)
            return bool(restored)

        if todo and os.path.exists(self.settings_path()):
            edit_json_file(self.settings_path(), change)
        return restored

    def describe(self, record, system):
        found = (record or {}).get("keys", {})
        return ", ".join(f"{k}={json.dumps(v['value'])}" for k, v in found.items()) or "nic"


class TetherProfile(ClaudeEnvSet):
    """Zmienne z `tether_env` tylko wtedy, gdy trasa domyślna idzie przez telefon; keep
    zdejmuje je po przejściu na kabel albo zwykłe Wi-Fi i zakłada przy następnym
    tetheringu."""

    def __init__(self):
        super().__init__(
            "tether-profile",
            "tether_env",
            "na tetheringu z telefonu sesje Claude Code bez auto-updatera i zapytań w tle "
            "(`tether_env` w env ~/.claude/settings.json, zdejmowane na zwykłym łączu)",
            "iPhone USB z Low Data Mode: upload 16-21 Mb/s, ping 10-248 ms; auto-updater "
            "ściągał ~236 MB na każde wydanie Claude Code (2026-10-08)",
        )

    def apply(self, cfg, system, record=None):
        link = system.link()
        if link.get("tethered"):
            record, changed = super().apply(cfg, system, record)
        else:
            undone = super().undo(record, system) if record else []
            record, changed = {"vars": {}}, [f"{v} zdjęte" for v in undone]
        record["link"] = {k: link.get(k) for k in ("tethered", "port", "iface")}
        return record, changed

    def describe(self, record, system):
        link = (record or {}).get("link") or {}
        where = link.get("port") or link.get("iface") or "?"
        if not link.get("tethered"):
            return f"czeka na tethering (teraz: {where})"
        return f"{where}: {super().describe(record, system)}"


CLAUDE_DIR = os.path.join(HOME, ".claude")
OFFICIAL_SECURITY = "security-guidance@claude-plugins-official"
# ręczna wersja tej poprawki sprzed claude-acc 1.26 (2026-10-09): ta sama rzecz, więc ją zastępujemy
SECURITY_PREDECESSOR = re.compile(r"sec-patterns@[\w.-]+")


class SecuritySlim:
    """Z security-guidance zostają tylko regexy na Edit/Write, w jednym Pythonie bez powłoki.

    Oficjalna wtyczka przy wyłączonym przeglądzie LLM i tak daje tylko te ostrzeżenia, ale płaci
    za nie bashem i dwoma Pythonami na każdej edycji i wiadomości, a jej 7 hooków Bash z `if:
    Bash(git commit:*)` odpala się przy każdym poleceniu, którego Claude Code nie umie rozebrać
    (`$(...)`, `$VAR`, `for`), czyli przy ~15% wywołań Basha.

    claude-acc nie rozprowadza kodu Anthropic: przy włączeniu kopiuje `patterns.py` z
    zainstalowanej wtyczki do własnego lokalnego marketplace'u (`plugins/` w katalogu stanu),
    obok kładzie swój hook (hooks/sec_slim.py) i `source.py` z wersją źródła, rejestruje
    marketplace i wtyczkę przez `claude plugin`, a oficjalną wyłącza w zakresie użytkownika.
    hooks.json wskazuje pliki w marketplace ścieżką bezwzględną, nie kopię w cache Claude Code,
    więc keep po aktualizacji security-guidance tylko podmienia `patterns.py`. Ręczną
    poprzedniczkę (`sec-patterns@...`) wyłącza tak samo i przywraca przy cofnięciu."""

    name = "security-slim"
    group = "claude"
    root = False
    title = (
        "security-guidance bez kosztu: te same ostrzeżenia na Edit/Write z jednego Pythona "
        "(wtyczka security-slim@claude-acc z patterns.py zainstalowanej wersji), oficjalna "
        "wyłączona w zakresie użytkownika"
    )
    effect = (
        "edycja 90 -> 27 ms, wiadomość 225 -> 0 ms, hooki Bash i Stop znikają, ~7,6% -> ~0,1% "
        "rdzenia w pracy; 17/17 identycznych odpowiedzi na próbkach (2026-10-09)"
    )
    MARKET = "claude-acc"
    PLUGIN = "security-slim"
    ID = "security-slim@claude-acc"
    path = None  # ~/.claude/settings.json; testy podstawiają kopię
    claude_dir = None  # ~/.claude (installed_plugins.json, ustawienia projektów)
    out_dir = None  # lokalny marketplace claude-acc
    python = None  # interpreter hooka; domyślnie ten sam co skryptów claude-acc

    def settings_path(self):
        return self.path or CLAUDE_SETTINGS

    def market_dir(self):
        return self.out_dir or os.path.join(STATE_DIR, "plugins")

    def hooks_dir(self):
        return os.path.join(self.market_dir(), self.PLUGIN, "hooks")

    def interpreter(self):
        if self.python:
            return self.python
        linked = os.path.join(STATE_DIR, "python")
        return linked if os.access(linked, os.X_OK) else sys.executable

    def installed(self):
        """{id wtyczki: [wpisy instalacji]} z installed_plugins.json Claude Code."""
        data, _ = read_json_file(os.path.join(self.claude_dir or CLAUDE_DIR, "plugins/installed_plugins.json"))
        plugins = data.get("plugins") if isinstance(data, dict) else None
        return plugins if isinstance(plugins, dict) else {}

    def source(self):
        """Zainstalowany security-guidance (najpierw zakres użytkownika): {"version", "path",
        "sha256" patterns.py} albo None."""
        entries = [e for e in self.installed().get(OFFICIAL_SECURITY) or [] if isinstance(e, dict)]
        for entry in sorted(entries, key=lambda e: e.get("scope") != "user"):
            path = entry.get("installPath") or ""
            try:
                with open(os.path.join(path, "hooks/patterns.py"), "rb") as f:
                    digest = hashlib.sha256(f.read()).hexdigest()
            except OSError:
                continue
            return {"version": str(entry.get("version") or ""), "path": path, "sha256": digest}
        return None

    def reenabled_by(self):
        """Pliki ustawień projektu i lokalne, które włączają oficjalną wtyczkę z powrotem
        (wygrywają z zakresem użytkownika): ścieżki projektów z installed_plugins.json."""
        names = {"project": "settings.json", "local": "settings.local.json"}
        found = []
        for entry in self.installed().get(OFFICIAL_SECURITY) or []:
            name = names.get((entry or {}).get("scope"))
            if not name or not entry.get("projectPath"):
                continue
            path = os.path.join(entry["projectPath"], ".claude", name)
            data, _ = read_json_file(path) if os.path.isfile(path) else ({}, None)
            plugins = data.get("enabledPlugins") if isinstance(data, dict) else None
            if isinstance(plugins, dict) and plugins.get(OFFICIAL_SECURITY) is True and path not in found:
                found.append(path)
        return found

    def files(self, src, python):
        """{ścieżka: bajty} wszystkiego, co leży w marketplace."""
        market, hooks = self.market_dir(), self.hooks_dir()
        runner = os.path.join(hooks, "sec_slim.py")
        with open(os.path.join(src["path"], "hooks/patterns.py"), "rb") as f:
            patterns = f.read()
        with open(os.path.join(REPO_HOOKS, "sec_slim.py"), "rb") as f:
            code = f.read()
        tag = "[from security-guidance@claude-code-plugins plugin]"
        try:
            with open(os.path.join(src["path"], "hooks/_base.py")) as f:
                found = re.search(r'^PROVENANCE_TAG = ("[^"\n]*")$', f.read(), re.M)
            tag = json.loads(found.group(1)) if found else tag
        except (OSError, ValueError):
            pass
        try:
            major, minor, patch = (int(x) for x in src["version"].split(".")[:3])
            pv = major * 10000 + minor * 100 + patch
        except ValueError:
            pv = 0
        about = f"the pattern warnings of {OFFICIAL_SECURITY} {src['version']}, without the rest"

        def dump(data):
            return (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode()

        return {
            os.path.join(market, ".claude-plugin/marketplace.json"): dump({
                "name": self.MARKET,
                "description": "Plugins generated by claude-acc on this Mac",
                "owner": {"name": "claude-acc"},
                "plugins": [{"name": self.PLUGIN, "source": "./" + self.PLUGIN, "description": about}],
            }),
            os.path.join(market, self.PLUGIN, ".claude-plugin/plugin.json"): dump(
                {"name": self.PLUGIN, "version": "1.0.0", "description": about}
            ),
            os.path.join(hooks, "hooks.json"): dump({
                "description": about,
                "hooks": {"PostToolUse": [{
                    "matcher": "Edit|Write|MultiEdit",
                    "hooks": [{"type": "command", "command": python, "args": ["-I", "-S", runner]}],
                }]},
            }),
            os.path.join(hooks, "source.py"): (
                f"# generated by claude-acc (perf security-slim) from {src['path']}\n"
                f"VERSION = {json.dumps(src['version'])}\nPV = {pv}\n"
                f"PROVENANCE_TAG = {json.dumps(tag, ensure_ascii=False)}\n"
            ).encode(),
            runner: code,
            os.path.join(hooks, "patterns.py"): patterns,
        }

    def generate(self, src, python):
        """Zapisuje pliki, które się różnią; zwraca ich nazwy."""
        changed = []
        for path, data in self.files(src, python).items():
            try:
                with open(path, "rb") as f:
                    if f.read() == data:
                        continue
            except OSError:
                pass
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = f"{path}.{os.getpid()}.perf-tmp"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, path)
            changed.append(os.path.relpath(path, self.market_dir()))
        return changed

    def check(self, python):
        """Hook ma wystartować tym Pythonem (importy patterns.py i source.py): z wyłączonymi
        przypomnieniami odpowiada samą metryką, niczego nie czyta i nie zapisuje."""
        runner = os.path.join(self.hooks_dir(), "sec_slim.py")
        env = dict(janitor.ENV, ENABLE_SECURITY_REMINDER="0")
        try:
            done = subprocess.run(
                [python, "-I", "-S", runner], input=b"{}", capture_output=True, timeout=30, env=env
            )
            ok = done.returncode == 0 and json.loads(done.stdout)["metrics"]["skipped"] is True
        except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
            ok, done = False, None
        if not ok:
            err = (done.stderr or b"").decode(errors="replace").strip()[-300:] if done else ""
            raise RuntimeError(f"hook security-slim nie startuje z {python}: {err or 'brak odpowiedzi'}")

    def cli(self, system, *args):
        code, out = system.claude_plugin(*args)
        if code != 0:
            raise RuntimeError(f"claude plugin {' '.join(args)}: {out.strip()[-300:]}")

    def flags(self, record, on):
        """enabledPlugins w ustawieniach użytkownika: oficjalna i poprzedniczka wyłączone, nasza
        włączona (on) albo poprzednie wartości (not on, tylko te, które dalej są nasze)."""
        wanted = {OFFICIAL_SECURITY: record.get("official")} if record.get("official") else {}
        wanted.update(record.get("predecessors") or {})
        changed = []

        def change(data):
            del changed[:]
            plugins = data.get("enabledPlugins")
            if not isinstance(plugins, dict):
                return False
            for key, entry in wanted.items():
                if on and plugins.get(key, MISSING) != entry["value"]:
                    plugins[key] = entry["value"]
                    changed.append(f"{key} wyłączona")
                elif not on and restore_key(plugins, key, entry["prev"], entry["value"]):
                    changed.append(f"{key} przywrócona")
            if on and plugins.get(self.ID) is not True and record.get("installed"):
                plugins[self.ID] = True
                changed.append(f"{self.ID} włączona")
            return bool(changed)

        edit_json_file(self.settings_path(), change)
        return changed

    def apply(self, cfg, system, record=None):
        record = dict(record or {})
        if not os.path.exists(self.settings_path()):
            return record, []
        src = self.source()
        if not record.get("plugin"):
            settings, _ = read_json_file(self.settings_path())
            plugins = settings.get("enabledPlugins")
            plugins = plugins if isinstance(plugins, dict) else {}
            official = plugins.get(OFFICIAL_SECURITY, MISSING)
            before = [k for k, v in plugins.items() if v is True and SECURITY_PREDECESSOR.fullmatch(k)]
            # wyłączona przez użytkownika i bez poprzedniczki: nie chce tych ostrzeżeń
            if src is None or (official is not True and not before):
                return {}, []
            record = {
                "plugin": self.ID,
                "had": {k: k in settings for k in ("enabledPlugins", "extraKnownMarketplaces")},
                "official": {"value": False, "prev": official} if official is True else None,
                "predecessors": {k: {"value": False, "prev": True} for k in before},
            }
            try:
                return record, self.converge(record, src, system)
            except (OSError, ValueError, RuntimeError):
                self.undo(record, system)  # nic na pół: następny keep zaczyna od zera
                raise
        return record, self.converge(record, src, system)

    def converge(self, record, src, system):
        """Pliki z bieżącego źródła, marketplace i wtyczka zarejestrowane, flagi ustawione.
        Bez zainstalowanego źródła zostaje ostatnia kopia."""
        changed = []
        python = record.get("python") or self.interpreter()
        if not os.access(python, os.X_OK):
            python = self.interpreter()
        src = src or (record.get("source") if os.path.isdir(self.hooks_dir()) else None)
        if src is None:
            raise RuntimeError("brak security-guidance i brak kopii jego patterns.py")
        if src.get("path") and os.path.isdir(src["path"]):
            written = self.generate(src, python)
            if written:
                self.check(python)
                version = src["version"] or "?"
                changed.append(f"patterns.py z security-guidance {version} ({', '.join(written)})")
            record["source"], record["python"] = src, python
        settings, _ = read_json_file(self.settings_path())
        markets = settings.get("extraKnownMarketplaces")
        if not (isinstance(markets, dict) and self.MARKET in markets):
            self.cli(system, "marketplace", "add", self.market_dir())
            record["marketplace"] = True
            changed.append(f"marketplace {self.MARKET} dodany")
        if not any(
            isinstance(e, dict) and e.get("scope") == "user" for e in self.installed().get(self.ID) or []
        ):
            self.cli(system, "install", self.ID, "--scope", "user")
            record["installed"] = True
            changed.append(f"{self.ID} zainstalowana")
        changed += self.flags(record, True)
        return changed

    def undo(self, record, system):
        restored = self.flags(record, False) if os.path.exists(self.settings_path()) else []
        if record.get("installed"):
            self.cli(system, "uninstall", self.ID, "--scope", "user")
            restored.append(f"{self.ID} odinstalowana")
            cache = os.path.join(self.claude_dir or CLAUDE_DIR, "plugins/cache", self.MARKET)
            shutil.rmtree(cache, ignore_errors=True)
        if record.get("marketplace"):
            self.cli(system, "marketplace", "remove", self.MARKET)
            restored.append(f"marketplace {self.MARKET} usunięty")
        if record.get("plugin"):
            shutil.rmtree(self.market_dir(), ignore_errors=True)
        had = record.get("had") or {}

        def change(data):
            # `claude plugin` zostawia puste słowniki, których przed nami nie było
            gone = [k for k, was in had.items() if not was and data.get(k) == {}]
            for key in gone:
                del data[key]
            return bool(gone)

        if had and os.path.exists(self.settings_path()):
            edit_json_file(self.settings_path(), change)
        return restored

    def describe(self, record, system):
        if not (record or {}).get("plugin"):
            return "security-guidance nie jest włączony, nie ma czego odchudzać"
        version = (record.get("source") or {}).get("version") or "?"
        text = f"{self.ID} z patterns.py security-guidance {version}, oficjalna wyłączona"
        others = self.reenabled_by()
        if others:
            text += "; włączają ją z powrotem: " + ", ".join(short_path(p) for p in others)
        return text

    def measure(self, record, runs=5):
        """Czas hooka na edycji pliku bez trafień (p50 ms): oficjalnego i naszego; None bez źródła."""
        src = (record.get("source") or {}).get("path")
        hooks, _ = read_json_file(os.path.join(src or "", "hooks/hooks.json"))
        official = None
        for group in ((hooks.get("hooks") or {}).get("PostToolUse") or []) if src else []:
            if "Edit" in (group.get("matcher") or ""):
                official = [h.get("command") for h in group.get("hooks") or [] if h.get("command")]
                break
        if not official:
            return None
        work = tempfile.mkdtemp(prefix="security-slim-")
        try:
            state = os.path.join(work, "state")
            os.makedirs(state)
            # bez tego oficjalny hook zaczyna budować środowisko SDK do przeglądu LLM
            open(os.path.join(state, ".sdk_bootstrap_spawned"), "w").close()
            env = dict(
                janitor.ENV, CLAUDE_PLUGIN_ROOT=src, SECURITY_WARNINGS_STATE_DIR=state,
                ENABLE_CODE_SECURITY_REVIEW="0", CLAUDE_PROJECT_DIR=work,
            )
            event = json.dumps({
                "session_id": "perf-security-slim", "cwd": work, "hook_event_name": "PostToolUse",
                "tool_name": "Edit", "tool_input": {
                    "file_path": os.path.join(work, "a.ts"), "old_string": "a", "new_string": "const b = 1",
                }, "tool_response": {},
            }).encode()
            runner = os.path.join(self.hooks_dir(), "sec_slim.py")
            slim = [record.get("python") or self.interpreter(), "-I", "-S", runner]

            def timed(argv):
                times = []
                for _ in range(runs + 1):
                    t = time.perf_counter()
                    subprocess.run(argv, input=event, capture_output=True, timeout=60, env=env, cwd=work)
                    times.append((time.perf_counter() - t) * 1000)
                return median(times[1:])  # pierwszy rozgrzewa cache dysku

            before = max(timed(["/bin/sh", "-c", c]) for c in official)
            return {"before": rnd(before, 0), "after": rnd(timed(slim), 0), "unit": "ms hooka na edycję (p50)"}
        except (OSError, subprocess.SubprocessError):
            return None
        finally:
            shutil.rmtree(work, ignore_errors=True)


class HookWrap:
    """Hooki formatowania uruchamiane przez hooks/npx-fast-wrap.sh: ten sam skrypt hooka,
    tylko `npx --no-install <narzędzie>` bierze narzędzie wprost z node_modules/.bin.

    Komenda hooka w settings.json dostaje przed sobą ścieżkę do wrappera; oryginał jest
    zapisany i wraca przy cofnięciu. Skrypty hooków użytkownika zostają nietknięte.
    """

    name = "fast-npx-hooks"
    group = "claude"
    root = False
    title = (
        "hooki formatowania (auto-format, ts-typecheck) z szybkim npx: narzędzie wprost z "
        "node_modules/.bin, bez czytania całego drzewa modułów przez npm"
    )
    effect = (
        "auto-format na plikach portivo, gdzie formatterów nie ma: .json 3,3 s -> 39 ms, "
        ".ts 6,6 s -> 82 ms; npx tsc 236 -> 54 ms; edycja .ts czekała 5,4 s p50"
    )
    path = None
    FILES = ("npx-fast-wrap.sh", "npx-fast/npx")

    def settings_path(self):
        return self.path or CLAUDE_SETTINGS

    def install(self):
        """Wrapper i atrapa npx w HOOKS_DIR; zwraca ścieżkę wrappera i to, co utworzył."""
        created = []
        for rel in self.FILES:
            source, target = os.path.join(REPO_HOOKS, rel), os.path.join(HOOKS_DIR, rel)
            if os.path.realpath(source) == os.path.realpath(target):
                continue  # perf.py działa z katalogu instalacji, pliki już tam są
            if not os.path.exists(target):
                created.append(target)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.copyfile(source, target)
            os.chmod(target, 0o755)
        return os.path.join(HOOKS_DIR, self.FILES[0]), created

    def apply(self, cfg, system, record=None):
        entries = list((record or {}).get("hooks", []))
        created = list((record or {}).get("created", []))
        if not os.path.exists(self.settings_path()):
            return {"hooks": entries, "created": created}, []
        wrap, new_files = self.install()
        created += [f for f in new_files if f not in created]
        prefix = shlex.quote(wrap) + " "
        targets = cfg.get("npx_fast_hooks", [])
        changed = []

        def change(data):
            del changed[:]
            known = {e["wrapped"] for e in entries}
            for event, groups in (data.get("hooks") or {}).items():
                for group in groups or []:
                    for hook in group.get("hooks", []) or []:
                        command = hook.get("command", "")
                        # hook bez powłoki (args) nie przyjmie wrappera przed komendą
                        if "args" in hook or pause_hook(hook):
                            continue
                        if command.startswith(prefix) or not PLAIN_COMMAND.fullmatch(
                            command
                        ):
                            continue
                        if not any(t in command for t in targets):
                            continue
                        hook["command"] = prefix + command
                        if hook["command"] not in known:
                            entries.append(
                                {
                                    "event": event,
                                    "original": command,
                                    "wrapped": hook["command"],
                                }
                            )
                        changed.append(
                            f"{event}: {os.path.basename(command.split()[0])}"
                        )
            return bool(changed)

        edit_json_file(self.settings_path(), change)
        return {"hooks": entries, "created": created}, changed

    def undo(self, record, system):
        restored = []
        wanted = {e["wrapped"]: e["original"] for e in record.get("hooks", [])}

        def change(data):
            del restored[:]
            for groups in (data.get("hooks") or {}).values():
                for group in groups or []:
                    for hook in group.get("hooks", []) or []:
                        if hook.get("command") in wanted:
                            hook["command"] = wanted[hook["command"]]
                            restored.append(
                                os.path.basename(hook["command"].split()[0])
                            )
            return bool(restored)

        if wanted and os.path.exists(self.settings_path()):
            edit_json_file(self.settings_path(), change)
        if os.path.realpath(REPO_HOOKS) == os.path.realpath(HOOKS_DIR):
            return restored  # pliki z instalacji claude-acc, nie nasze kopie
        for path in record.get("created", []):
            try:
                os.remove(path)
            except OSError:
                pass
        for folder in (os.path.join(HOOKS_DIR, "npx-fast"), HOOKS_DIR):
            try:
                os.rmdir(folder)  # tylko gdy pusty
            except OSError:
                pass
        return restored

    def describe(self, record, system):
        hooks = (record or {}).get("hooks", [])
        names = sorted({os.path.basename(e["original"].split()[0]) for e in hooks})
        return ", ".join(names) or "brak hooków z listy `npx_fast_hooks`"


def pause_hook(hook):
    """Hook pauzy limitów: w powłoce ze znacznikiem hook.py albo bez niej (claude-acc-pause,
    `claude-acc-hook pause`)."""
    command = hook.get("command") or ""
    args = hook.get("args")
    native = command.endswith(NATIVE_HOOK) and isinstance(args, list) and args[:1] == ["pause"]
    return PAUSE_HOOKS in command or native or command.endswith(PAUSE_NATIVE)


def exec_form(command):
    """(program, argumenty) dla hooka, który da się uruchomić bez powłoki, albo None.

    Tylko gdy powłoka nic by w nim nie zrobiła poza rozwinięciem $HOME na początku: program
    podany ścieżką (nazwa z PATH zostaje w powłoce, bo zamrożenie ścieżki zmieniłoby wersję
    po przełączeniu node czy pythona) i plik, który execve uruchomi sam (#! albo Mach-O)."""
    parts = command.split()
    if not parts or not EXEC_PROGRAM.fullmatch(parts[0]):
        return None
    if not all(EXEC_ARG.fullmatch(a) for a in parts[1:]):
        return None
    program = parts[0]
    for prefix in ("$HOME/", "~/"):
        if program.startswith(prefix):
            program = os.path.join(os.path.expanduser("~"), program[len(prefix):])
    try:
        with open(program, "rb") as f:
            head = f.read(4)
    except OSError:
        return None
    if not os.access(program, os.X_OK) or not head.startswith(EXEC_MAGIC):
        return None
    return program, parts[1:]


class NativeHooks:
    """Hooki na natywnych programach i bez powłoki: ten sam wynik bez startu `sh -c`, jq
    i Pythona przy każdym narzędziu każdego agenta.

    - devguard: `python3 .../devguard.py admit` (także przez acc.py) zamienia się na
      claude-acc-hook, natywny front tego samego admit: zwykłe komendy przepuszcza sam,
      komendę z całym słowem od dev serwera albo od schedulera oddaje Pythonowi;
    - rtk: skrypt rtk-rewrite.sh zamienia się na `rtk hook claude`, hook wbudowany w rtk.
      Skrypt zostaje nietknięty, bo rtk sprawdza jego sha256 i po zmianie odmawia pracy;
    - każdy inny prosty hook z programem podanym ścieżką (np. własna binarka w Go) idzie
      bez powłoki: exec form, czyli `command` to sam program, a `args` jego argumenty.
      `sh -c` kosztuje 3-4 ms na każde wywołanie (6.10, Mac pod obciążeniem).

    Zamiana tylko wtedy, gdy program jest na miejscu; oryginał wraca przy cofnięciu.
    """

    name = "claude-hooks-native"
    group = "claude"
    root = False
    title = (
        "hooki na natywnych programach i bez powłoki: claude-acc-hook zamiast devguard.py "
        "admit, `rtk hook claude` zamiast rtk-rewrite.sh, proste hooki bez `sh -c`"
    )
    effect = (
        "na każdą komendę Bash: devguard 25-57 -> 5-8 ms, rtk 57-80 -> 12-14 ms (6.10; "
        "decyzje identyczne na 10 i 18 przypadkach); bez `sh -c` 3-4 ms mniej na hook"
    )
    path = None

    def settings_path(self):
        return self.path or CLAUDE_SETTINGS

    def targets(self, system):
        """[(wzorzec komendy, program, argumenty)] dla programów, które są zainstalowane."""
        found = []
        hook = os.path.join(STATE_DIR, "claude-acc-hook")
        if os.access(hook, os.X_OK):
            found.append((DEVGUARD_ADMIT, hook, ["admit"]))
        rtk = system.rtk_path()
        if rtk:
            found.append((RTK_SCRIPT, rtk, ["hook", "claude"]))
        return found

    def apply(self, cfg, system, record=None):
        entries = list((record or {}).get("hooks", []))
        targets = self.targets(system)
        if not os.path.exists(self.settings_path()):
            return {"hooks": entries}, []
        changed = []
        # zamiany z 1.7, jeszcze w powłoce ("rtk hook claude", ścieżka claude-acc-hook); klucze
        # liczone raz, bo edit_json_file może wołać change drugi raz po cudzym zapisie
        upgrades = [((e["event"], e["native"]), e) for e in entries if "args" not in e]

        def owned(event, command):
            """Hook, który zmienia inna poprawka: async albo wrapper szybkiego npx."""
            if any(t in command for t in cfg.get("npx_fast_hooks", [])):
                return True
            return any(
                spec["event"] == event and spec["match"] in command
                for spec in cfg.get("async_hooks", [])
            )

        def change(data):
            del changed[:]
            known = {(e["event"], e["original"]) for e in entries}
            older = {}
            for key, e in upgrades:
                older.setdefault(key, []).append(e)
            for event, groups in (data.get("hooks") or {}).items():
                for group in groups or []:
                    for hook in group.get("hooks", []) or []:
                        command = hook.get("command", "")
                        if "args" in hook or not PLAIN_COMMAND.fullmatch(command):
                            continue
                        waiting = older.get((event, command))
                        if waiting:
                            entry = waiting.pop(0)
                            # wpis zostaje ten sam: cofnięcie dalej przywraca prawdziwy oryginał
                            target = next(
                                (t for t in targets if t[0].search(entry["original"])),
                                None,
                            )
                            if not target:
                                continue
                            hook["command"], hook["args"] = target[1], list(target[2])
                            entry.update(native=target[1], args=list(target[2]))
                            changed.append(f"{event}: {hook_label(command)} bez powłoki")
                            continue
                        found = next(
                            ((prog, args) for pattern, prog, args in targets if pattern.search(command)),
                            None,
                        ) or (None if owned(event, command) else exec_form(command))
                        if not found:
                            continue
                        hook["command"], hook["args"] = found[0], list(found[1])
                        if (event, command) not in known:
                            entries.append(
                                {
                                    "event": event,
                                    "original": command,
                                    "native": found[0],
                                    "args": list(found[1]),
                                }
                            )
                        native = " ".join([found[0], *found[1]])
                        changed.append(f"{event}: {hook_label(command)} -> {hook_label(native)}")
            return bool(changed)

        edit_json_file(self.settings_path(), change)
        return {"hooks": entries}, changed

    def undo(self, record, system):
        restored = []
        entries = record.get("hooks", [])

        def change(data):
            del restored[:]
            # dwa różne oryginały mogły dostać ten sam zamiennik: wracają w kolejności zapisu
            queue = {}
            for e in entries:
                key = (e["event"], e["native"], tuple(e["args"]) if "args" in e else None)
                queue.setdefault(key, []).append(e["original"])
            for event, groups in (data.get("hooks") or {}).items():
                for group in groups or []:
                    for hook in group.get("hooks", []) or []:
                        args = hook.get("args")
                        key = (event, hook.get("command"), tuple(args) if isinstance(args, list) else None)
                        waiting = queue.get(key)
                        if waiting:
                            hook["command"] = waiting.pop(0)
                            hook.pop("args", None)
                            restored.append(f"{event}: {hook_label(hook['command'])}")
            return bool(restored)

        if entries and os.path.exists(self.settings_path()):
            edit_json_file(self.settings_path(), change)
        return restored

    def describe(self, record, system):
        hooks = (record or {}).get("hooks", [])
        pairs = sorted(
            {
                f"{hook_label(e['original'])} -> {hook_label(' '.join([e['native'], *e.get('args', [])]))}"
                for e in hooks
            }
        )
        return ", ".join(pairs) or "brak hooków z natywnym odpowiednikiem"


class RootTweak:
    """Poprawka, która wymaga roota: perf.py tylko ją opisuje, robi ją perf-root.sh."""

    root = True
    group = "root"

    def __init__(self, name, title, effect, command):
        self.name, self.title, self.effect, self.command = name, title, effect, command


TWEAKS = [
    BackgroundHelpers(),
    AsyncHooks(),
    NativeHooks(),
    ClaudeEnv(
        "node-compile-cache",
        "NODE_COMPILE_CACHE",
        COMPILE_CACHE_DIR,
        "cache kompilacji V8 dla node uruchamianego przez sesje Claude (tsc, eslint, serwery MCP, hooki)",
        "require('typescript') 87 -> 40 ms, eslint 77 -> 67 ms; next i cavemem bez zmian",
    ),
    JsonSetting(
        "devguard-budget",
        "dev",
        DEVGUARD_CONFIG,
        "budget_percent",
        "devguard_budget_percent",
        "ciaśniejszy limit dev serwerów w strażniku (devguard.json budget_percent)",
        "polityka, nie pomiar: 35% RAM (16,8 GB) -> 25% (12 GB); dev serwery zajmowały 3,2 GB",
    ),
    JsonSetting(
        "devguard-max-server",
        "dev",
        DEVGUARD_CONFIG,
        "max_server_gb",
        "devguard_max_server_gb",
        "wcześniejszy restart spuchniętego dev serwera (devguard.json max_server_gb)",
        "polityka, nie pomiar: 5 -> 4 GB; Turbopack po godzinie pracy dobija do 7-9 GB",
    ),
    JsonSetting(
        "docker-vm",
        "docker",
        DOCKER_SETTINGS,
        "MemoryMiB",
        "docker_memory_mib",
        "mniejsza maszyna Dockera (settings-store.json MemoryMiB), od następnego startu Dockera",
        "VM trzymała 8,0 GB, 21 kontenerów używało 3,7 GB; Virtualization.framework nie oddaje pamięci",
        defer=True,
    ),
    GitSpeed(),
    HookWrap(),
    ClaudeEnvSet(
        "claude-limits",
        "claude_limits",
        "wyższe sufity Claude Code z `claude_limits` (env w ~/.claude/settings.json); "
        "domyślne zachowanie bez zmian",
        "BASH_MAX_TIMEOUT_MS 600000 -> 3600000: w 2 doby ~40 poleceń z timeoutem 15-60 min "
        "ściętych do 10 min i przeniesionych w tło",
    ),
    JsonSetting(
        "workflow-size",
        "claude",
        CLAUDE_SETTINGS,
        "workflowSizeGuideline",
        "workflow_size_guideline",
        "workflowSizeGuideline w ~/.claude/settings.json: workflowy planowane na duży rozmiar",
        "medium (<10 agentów) -> large (<50); limit runtime na agentów i ostrzeżenia zostają",
    ),
    ClaudeUi(),
    JsonSetting(
        "subagent-cache-1h",
        "claude",
        CLAUDE_SETTINGS,
        "subagentPromptCacheTtl",
        "subagent_prompt_cache_ttl",
        "godzinny cache promptu subagentów i członków zespołu (subagentPromptCacheTtl "
        "w ~/.claude/settings.json); główna sesja bez zmian",
        "zapisy cache subagentów 300-500 mln tokenów na dobę (5 min, 1-3.10) -> 45-115 mln "
        "(1 h, 5-8.10); przy 5 min 47% zapisów to kontekst zapisywany od nowa po 5-60 min "
        "przerwy (678 zapytań po 8,1 s p50 w tydzień)",
    ),
    TetherProfile(),
    RipgrepThreads(),
    SecuritySlim(),
    DockerIdle(),
    RootTweak(
        "vnodes",
        "większy cache vnode (kern.maxvnodes 263168 -> 786432): metadane drzew node_modules "
        "(lstat, open, lookup) mieszczą się w nim; treści plików nie, tę wypiera presja pamięci",
        "lstat 358 tys. wpisów portivo/.pnpm, drugi przebieg: 3,59 -> 2,51 s, odzysk 253 tys. -> 4,5 tys. "
        "vnode (czysty pomiar). Koszt ~1,2 KB pamięci jądra na vnode, +0,63 GB. Jądro nie zwalnia "
        "vnode: po cofnięciu pamięć i cache zostają do restartu",
        "claude-acc perf-root vnodes trial",
    ),
    RootTweak(
        "iogpu",
        "więcej pamięci dla GPU (iogpu.wired_limit_mb): lokalne modele (MLX, llama.cpp, Ollama) "
        "mieszczą się w GPU; domyślnie macOS daje Metalowi około 2/3 RAM",
        "48 GB: domyślnie 37,4 GiB dla GPU, z limitem 40960 MLX widzi 42,9 zamiast 40,2 GB, "
        "llama.cpp 40960 MiB; system zachowuje 8 GB. Sysctl opisany w README mlx-lm (macOS 15+)",
        "claude-acc perf-root iogpu set",
    ),
    RootTweak(
        "spotlight",
        "Spotlight tylko dla aplikacji: wszystkie katalogi domowe poza Applications oraz /Library, "
        "/opt, /usr/local i /Users/Shared na liście Prywatności",
        "Spotlight indeksował 510 tys. plików magazynu pnpm i modułów Go, a wyniki z plików i "
        "folderów i tak są wyłączone; aplikacje (/Applications, ~/Applications) zostają",
        "claude-acc perf-root spotlight apps-only",
    ),
    RootTweak(
        "devtools",
        f"{HOST.name} na liście Narzędzi deweloperskich: binarki zbudowane przez agentów (testy Go, "
        "go run, natywne moduły node) startują bez oceny Gatekeepera",
        "pierwsze uruchomienie nowej binarki Go w terminalu Orki 196 ms p50, w Terminalu "
        "(narzędzie deweloperskie) 4 ms; ocena to skan XProtect i zapytanie do Apple o notaryzację",
        "claude-acc perf-root devtools add (bez sudo, kliknięcie + w Ustawieniach)",
    ),
    RootTweak(
        "shaper",
        "ogranicznik wysyłania na interfejsie (ifconfig tbr): kolejka zostaje w fq_codel "
        "Maca zamiast w buforze routera",
        "przy BE230 sieć pod obciążeniem dokłada 1-4 ms, więc nie ma czego ratować; "
        "przy Zyxelu jeszcze niezmierzone (sprawdź linię „z tego sieć” w bench network)",
        "claude-acc perf-root trial",
    ),
]


def tweak(name):
    for item in TWEAKS:
        if item.name == name:
            return item
    return None


# skrypty claude-acc: wprost (`/usr/bin/python3 <STATE>/devguard.py run`) albo przez
# launcher z bajtkodem w cache (`<STATE>/python <STATE>/acc.py devguard run`)
ACC_SCRIPTS = ("accswitch", "devguard", "janitor", "perf", "sched", "updates")


def short_command(command):
    """Czytelna nazwa procesu: plik skryptu albo program, bez ścieżek. Skrypt claude-acc
    ma tę samą nazwę w obu formach uruchomienia (wprost i przez acc.py)."""
    parts = command.split()
    for i, part in enumerate(parts[1:], 1):
        name = os.path.basename(part)
        if name == "acc.py" and i + 1 < len(parts) and parts[i + 1] in ACC_SCRIPTS:
            return parts[i + 1]
        if name[:-3] in ACC_SCRIPTS and name.endswith(".py"):
            return name[:-3]
        if part.endswith((".js", ".py", ".mjs", ".ts")):
            return os.path.basename(os.path.dirname(os.path.dirname(part))) or part
    return os.path.basename(parts[0]) if parts else command


def short_path(path):
    return path.replace(HOME, "~", 1) if path.startswith(HOME) else path


# ---------- pomiary pracy agentów ----------


def stat_walk(root):
    """lstat każdego wpisu w drzewie (jak rozwiązywanie modułów albo git status -uall)."""
    count = 0
    stack = [root]
    while stack:
        try:
            entries = os.scandir(stack.pop())
        except OSError:
            continue
        with entries:
            for entry in entries:
                try:
                    entry.stat(follow_symlinks=False)
                    is_dir = entry.is_dir(follow_symlinks=False)
                except OSError:
                    continue
                count += 1
                if is_dir:
                    stack.append(entry.path)
    return count


def bench_fs(cfg, passes=2):
    """Dwa przebiegi lstat po dużym drzewie node_modules. Gdy drugi jest tak samo wolny jak
    pierwszy, a jądro odzyskuje vnode tyle, ile było wpisów, drzewo nie mieści się w cache
    vnode (kern.maxvnodes) i każde narzędzie skanujące moduły płaci za to od nowa."""
    root = os.path.expanduser(cfg["fs_bench_path"])
    if not os.path.isdir(root):
        return {"error": f"brak {root}"}
    runs = []
    for _ in range(passes):
        recycled = sysctl_int("kern.num_recycledvnodes") or 0
        started = time.monotonic()
        entries = stat_walk(root)
        runs.append(
            {
                "seconds": round(time.monotonic() - started, 2),
                "recycled": (sysctl_int("kern.num_recycledvnodes") or 0) - recycled,
            }
        )
    return {
        "path": short_path(root),
        "entries": entries,
        "first_s": runs[0]["seconds"],
        "warm_s": runs[-1]["seconds"],
        "warm_recycled": runs[-1]["recycled"],
        "maxvnodes": sysctl_int("kern.maxvnodes"),
    }


def iso_epoch(stamp):
    import calendar

    try:
        return calendar.timegm(time.strptime(stamp[:19], "%Y-%m-%dT%H:%M:%S"))
    except (TypeError, ValueError):
        return None


def hook_records(path):
    """(zdarzenie, klucz wywołania, ms, chwila, komenda) z jednego transkryptu Claude Code.

    Każdy hook ma osobny wpis hook_success z durationMs. Przy narzędziu kluczem jest
    toolUseID; Stop go nie ma, więc hooki jednej tury łączy to, że kończą się w tej
    samej sekundzie. Transkrypty mają setki MB: czytane linia po linii.
    """
    last_stop = (None, 0)
    with open(path, errors="replace") as handle:
        for line in handle:
            if '"hook_success"' not in line:
                continue
            try:
                entry = json.loads(line)
                attachment = entry["attachment"]
                ms = float(attachment["durationMs"])
            except (ValueError, KeyError, TypeError):
                continue
            stamp = iso_epoch(entry.get("timestamp"))
            if stamp is None:
                continue
            key = attachment.get("toolUseID")
            if not key:
                if last_stop[0] and stamp - last_stop[1] < 2:
                    key = last_stop[0]
                else:
                    key = f"{path}:{entry.get('uuid')}"
                last_stop = (key, stamp)
            yield (
                attachment.get("hookEvent"),
                key,
                ms,
                stamp,
                attachment.get("command", ""),
            )


def hook_latency(
    since, until=None, events=("PreToolUse", "PostToolUse", "Stop"), skip=()
):
    """Ile sesja Claude Code czeka na hooki przy jednym zdarzeniu (ms): najdłuższy z
    równoległych hooków, z transkryptów. Hooki, których komenda zawiera coś z `skip`
    (puszczone w tle), nie wstrzymują sesji, więc się nie liczą.

    {"PostToolUse": {"n": ..., "p50": ..., "p90": ...}, ...}
    """
    return hook_stats(since, until, events, skip)[0]


def stats_of(values):
    return {
        "n": len(values),
        "p50": rnd(percentile(values, 0.5), 0),
        "p90": rnd(percentile(values, 0.9), 0),
    }


def hook_stats(
    since, until=None, events=("PreToolUse", "PostToolUse", "Stop"), skip=()
):
    """(czekanie na zdarzenie jak w hook_latency, czas każdego hooka z osobna) z jednego
    czytania transkryptów. Drugie: {(zdarzenie, etykieta hooka): {"n", "p50", "p90"}}.
    Transkrypt zapisuje tylko hooki, które coś wypisały: cichy hook (devguard przy
    zwykłej komendzie) w tych liczbach nie występuje."""
    until = until or time.time()
    calls = {event: {} for event in events}
    per_hook = {}
    for path in glob.glob(os.path.join(CLAUDE_PROJECTS, "*", "*.jsonl")):
        try:
            if os.path.getmtime(path) < since:
                continue
            records = list(hook_records(path))
        except OSError:
            continue
        for event, key, ms, stamp, command in records:
            if event not in calls or not since <= stamp <= until:
                continue
            per_hook.setdefault((event, hook_label(command)), []).append(ms)
            if any(s in command for s in skip):
                calls[event].setdefault(key, 0)
                continue
            calls[event][key] = max(calls[event].get(key, 0), ms)
    waits = {e: stats_of(list(c.values())) for e, c in calls.items() if c}
    return waits, {key: stats_of(values) for key, values in per_hook.items()}


def tool_calls(since, until=None):
    """Wywołania narzędzi z transkryptów Claude Code (także subagentów) w oknie czasu:
    (narzędzie, wejście, ms od tool_use do tool_result, początek tekstu wyniku).

    Czas to obrót z punktu widzenia sesji: wykonanie razem z hookami Pre i Post.
    Czytane są tylko transkrypty zmienione w oknie.
    """
    until = until or time.time()
    for path in glob.glob(
        os.path.join(CLAUDE_PROJECTS, "**", "*.jsonl"), recursive=True
    ):
        try:
            if os.path.getmtime(path) < since:
                continue
            yield from transcript_tools(path, since, until)
        except OSError:
            continue


def transcript_tools(path, since, until):
    """tool_calls dla jednego transkryptu; setki MB, więc linia po linii."""
    pending = {}
    with open(path, errors="replace") as handle:
        for line in handle:
            if '"tool_use"' not in line and '"tool_result"' not in line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            stamp = iso_epoch(entry.get("timestamp"))
            content = (entry.get("message") or {}).get("content")
            if stamp is None or not isinstance(content, list):
                continue
            stamp += iso_fraction(entry.get("timestamp"))
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use":
                    pending[block.get("id")] = (
                        block.get("name"),
                        block.get("input") or {},
                        stamp,
                    )
                elif block.get("type") == "tool_result":
                    start = pending.pop(block.get("tool_use_id"), None)
                    if start and since <= start[2] <= until:
                        text = json.dumps(block.get("content"), ensure_ascii=False)
                        yield start[0], start[1], (stamp - start[2]) * 1000, text[:600]


def iso_fraction(stamp):
    """Milisekundy znacznika ISO jako ułamek sekundy ("...:05.432Z" -> 0.432)."""
    try:
        return float("0" + stamp[19:23]) if stamp[19] == "." else 0.0
    except (TypeError, IndexError, ValueError):
        return 0.0


BASH_FAMILIES = [
    ("go test", r"\bgo\s+test\b"),
    ("go build/vet/run", r"\bgo\s+(build|vet|run|install|generate|mod)\b"),
    ("golangci-lint", r"golangci-lint"),
    ("make", r"(^|[;&|(]\s*)make\b"),
    ("pnpm/npm/npx", r"\b(pnpm|npm|npx|yarn|bun)\b"),
    ("tsc/eslint/vitest/next", r"\b(tsc|eslint|vitest|jest|next|turbo|playwright)\b"),
    ("node", r"\bnode\b"),
    ("git", r"\bgit\b"),
    ("rg/grep/find", r"\b(rg|grep|find|fd)\b"),
    ("docker", r"\bdocker\b"),
    ("python", r"\bpython3?\b"),
    ("sleep/czekanie", r"\b(sleep|until|wait)\b"),
    (
        "pliki (ls/cat/sed)",
        r"\b(ls|cat|sed|head|tail|wc|mkdir|cp|mv|rm|awk|cut|sort)\b",
    ),
]
# rozszerzenia, które formatują hooki (prettier, eslint, tsc)
FORMATTED = (
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".mjs",
    ".cjs",
    ".json",
    ".md",
    ".mdx",
    ".css",
    ".yml",
    ".yaml",
    ".html",
)


def tool_family(name, args):
    if name == "Bash":
        command = re.sub(r"\brtk\s+(proxy\s+)?", "", str(args.get("command", "")))
        for family, pattern in BASH_FAMILIES:
            if re.search(pattern, command):
                return f"Bash: {family}"
        return "Bash: inne"
    if name in ("Edit", "Write", "MultiEdit"):
        ext = os.path.splitext(str(args.get("file_path", "")))[1].lower()
        return f"{name} {'formatowane' if ext in FORMATTED else 'inne'}"
    if name and name.startswith("mcp__"):
        return "MCP " + name.split("__")[1]
    return name or "?"


def ms_stats(values):
    return {
        "n": len(values),
        "p50": rnd(percentile(values, 0.5), 0),
        "p90": rnd(percentile(values, 0.9), 0),
    }


def capped_bash(text, args):
    """Polecenie, któremu agent dał więcej niż 10 min, a Claude Code ściął je do 600 s."""
    asked = args.get("timeout") or 0
    try:
        asked = int(asked)
    except (TypeError, ValueError):
        asked = 0
    return asked > 600000 and (
        "within its 600s timeout" in text or "timed out after 10m" in text
    )


def agent_turnaround(since, until=None):
    """Czasy narzędzi agentów w oknie: rodziny Bash, edycje plików formatowanych i nie,
    polecenia ścięte do 10 min wbrew prośbie agenta."""
    families = {}
    formatted = []
    quick = []
    capped = 0
    for name, args, ms, text in tool_calls(since, until):
        family = tool_family(name, args)
        families.setdefault(family, []).append(ms)
        if family.endswith(" formatowane"):
            formatted.append(ms)
        if name == "Bash" and capped_bash(text, args):
            capped += 1
        if name == "Bash" and ms < 5000:
            quick.append(ms)
    return {
        "families": {k: ms_stats(v) for k, v in families.items()},
        "formatted_edits": ms_stats(formatted),
        "capped": capped,
        # podłoga wywołania Bash: samo polecenie to 5-20 ms, reszta to Claude Code,
        # powłoka ze snapshotem i hooki (2026-10-08: p10 120 ms, p50 260 ms)
        "bash_floor": {
            "n": len(quick),
            "p10": rnd(percentile(quick, 0.1), 0),
            "p50": rnd(percentile(quick, 0.5), 0),
        },
        "total_s": {k: rnd(sum(v) / 1000, 0) for k, v in families.items()},
    }


STAMP = re.compile(r'"timestamp":"([^"]+)"')
# progi kontekstu (tokeny) dla opóźnienia modelu: rośnie z kontekstem (2026-10-08, 7 dni:
# p50 2,6 s poniżej 30 tys., 5,9 s przy 200-400 tys., 6,8 s powyżej 400 tys.)
CTX_BUCKETS = [
    (0, 30000, "<30k"),
    (30000, 100000, "30-100k"),
    (100000, 200000, "100-200k"),
    (200000, 400000, "200-400k"),
    (400000, 700000, "400-700k"),
    (700000, float("inf"), "700k+"),
]
CACHE_TTL_S = 300  # cache promptu bez subagentPromptCacheTtl
COLD_WRITE = 50000  # tyle tokenów zapisanych w jednym zapytaniu to kontekst od nowa


def model_requests(since, until=None):
    """Zapytania do modelu z transkryptów Claude Code (także subagentów) w oknie czasu,
    każde jako {"sub", "gap", "ms", "first_ms", "ctx", "created", "read", "out"}.

    Zapytanie zaczyna wpis tuż przed jego pierwszym blokiem (wynik narzędzia, prompt), a
    kończy ostatni blok; Claude Code zapisuje blok gotowy, więc first_ms to pierwszy blok
    razem z myśleniem. gap to cisza od końca poprzedniego zapytania w tym transkrypcie.
    """
    until = until or time.time()
    for path in glob.glob(
        os.path.join(CLAUDE_PROJECTS, "**", "*.jsonl"), recursive=True
    ):
        try:
            if os.path.getmtime(path) < since:
                continue
            yield from transcript_requests(path, since, until)
        except OSError:
            continue


def transcript_requests(path, since, until):
    """model_requests dla jednego transkryptu: json tylko dla wpisów modelu, reszcie
    wystarcza znacznik czasu (wyniki narzędzi mają po kilka MB)."""
    sub = f"{os.sep}subagents{os.sep}" in path
    found, order = {}, []
    prev = last_end = None
    with open(path, errors="replace") as handle:
        for line in handle:
            match = STAMP.search(line)
            stamp = iso_epoch(match.group(1)) if match else None
            if stamp is None:
                continue
            stamp += iso_fraction(match.group(1))
            entry = None
            if '"type":"assistant"' in line:
                try:
                    entry = json.loads(line)
                except ValueError:
                    entry = None
            if entry and entry.get("type") == "assistant":
                message = entry.get("message") or {}
                key = entry.get("requestId") or message.get("id")
                item = found.get(key)
                if item is None:
                    gap = None if last_end is None else (prev or stamp) - last_end
                    item = found[key] = [prev or stamp, stamp, stamp, {}, gap]
                    order.append(key)
                item[2] = stamp
                item[3] = message.get("usage") or item[3]
                last_end = stamp
            prev = stamp
    for key in order:
        start, first, last, usage, gap = found[key]
        if not since <= start <= until:
            continue
        created = usage.get("cache_creation_input_tokens") or 0
        read = usage.get("cache_read_input_tokens") or 0
        yield {
            "sub": sub,
            "gap": gap,
            "ms": (last - start) * 1000,
            "first_ms": (first - start) * 1000,
            "ctx": (usage.get("input_tokens") or 0) + created + read,
            "created": created,
            "read": read,
            "out": usage.get("output_tokens") or 0,
        }


def model_latency(requests):
    """Opóźnienie modelu (ms) w progach kontekstu: {"200-400k": {"n", "p50", "p90", "first_p50"}}."""
    out = {}
    for low, high, label in CTX_BUCKETS:
        chosen = [r for r in requests if low <= r["ctx"] < high]
        if chosen:
            out[label] = dict(
                ms_stats([r["ms"] for r in chosen]),
                first_p50=rnd(percentile([r["first_ms"] for r in chosen], 0.5), 0),
            )
    return out


def cold_cache(requests):
    """Kontekst zapisany do cache od nowa po ciszy dłuższej niż 5-minutowy cache, osobno
    dla głównych sesji i subagentów: {"sub": {"requests", "mtok", "s"}, "main": ...}.
    Liczy tylko przerwy krótsze niż godzina: tyle uratuje godzinny cache."""
    out = {}
    for who in ("sub", "main"):
        cold = [
            r
            for r in requests
            if r["sub"] == (who == "sub")
            and r["gap"] is not None
            and CACHE_TTL_S <= r["gap"] < 3600
            and r["created"] > COLD_WRITE
        ]
        out[who] = {
            "requests": len(cold),
            "mtok": rnd(sum(r["created"] for r in cold) / 1e6, 1),
            "s": rnd(sum(r["ms"] for r in cold) / 1000, 0),
        }
    return out


def cold_per_day(since, until):
    """Mln tokenów zimnego cache subagentów na dobę w oknie (wynik subagent-cache-1h)."""
    days = max((until - since) / 86400, 1 / 24)
    return rnd(cold_cache(list(model_requests(since, until)))["sub"]["mtok"] / days, 1)


def bench_agents(hours=24):
    """Obrót narzędzi agentów z ostatnich `hours` godzin i czekanie na hooki."""
    since = time.time() - hours * 3600
    data = agent_turnaround(since)
    families = data["families"]
    top = sorted(families.items(), key=lambda kv: -kv[1]["n"] * (kv[1]["p50"] or 0))
    waits, per_hook = hook_stats(since)
    # najwięcej łącznego czasu: hook do przepisania na program albo do puszczenia w tle
    slow = sorted(per_hook.items(), key=lambda kv: -kv[1]["n"] * (kv[1]["p50"] or 0))
    requests = list(model_requests(since))
    totals = sorted(data["total_s"].items(), key=lambda kv: -kv[1])
    return {
        "hours": hours,
        "tools": dict(top[:16]),
        "formatted_edits": data["formatted_edits"],
        "capped_per_day": rnd(data["capped"] * 24 / hours, 1),
        "hooks": waits,
        "slow_hooks": [
            dict(st, event=event, hook=label) for (event, label), st in slow[:8]
        ],
        # gdzie idzie czas: model wobec narzędzi, oba zsumowane po równoległych agentach
        "model": {
            "requests": len(requests),
            "sub_share": rnd(
                100 * sum(r["sub"] for r in requests) / max(len(requests), 1), 0
            ),
            "s": rnd(sum(r["ms"] for r in requests) / 1000, 0),
            "by_context": model_latency(requests),
        },
        "tools_s": rnd(sum(data["total_s"].values()), 0),
        "tools_by_total_s": dict(totals[:10]),
        "bash_floor": data["bash_floor"],
        "cold_cache": cold_cache(requests),
    }


GO_PROBE = "package main\n\nconst v = %d\n\nfunc main() { _ = v }\n"


def responsible_app(pid=None):
    """Aplikacja, którą macOS uważa za odpowiedzialną za proces (TCC, Gatekeeper)."""
    try:
        fn = _libc.responsibility_get_pid_responsible_for_pid
    except AttributeError:
        return None
    fn.restype, fn.argtypes = ctypes.c_int, [ctypes.c_int]
    owner = fn(pid or os.getpid())
    out = janitor.run(["ps", "-o", "comm=", "-p", str(owner)]) or ""
    path = out.strip()
    app = re.search(r"/([^/]+)\.app/", path)
    return app.group(1) if app else os.path.basename(path) or None


def first_exec(count=8):
    """Pierwsze i drugie uruchomienie świeżo zbudowanych binarek Go (ms, mediany).

    Każda wersja ma inną stałą, więc inny hash: tak jak test Go po każdej zmianie. Różnica
    między pierwszym a drugim uruchomieniem to ocena Gatekeepera przy pierwszym exec."""
    import tempfile

    go = janitor.which("go")
    if not go:
        return None
    work = tempfile.mkdtemp(prefix="perf-gk-")
    first, second = [], []
    try:
        with open(os.path.join(work, "go.mod"), "w") as f:
            f.write("module gk\n\ngo 1.21\n")
        for i in range(count):
            with open(os.path.join(work, "main.go"), "w") as f:
                f.write(GO_PROBE % time.time_ns())
            binary = os.path.join(work, f"bin{i}")
            if janitor.run([go, "build", "-C", work, "-o", binary, "."], timeout=120) is None:
                return None
            for runs in (first, second):
                started = time.perf_counter()
                subprocess.run([binary], capture_output=True, check=False)
                runs.append((time.perf_counter() - started) * 1000)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return {"first_ms": rnd(median(first), 1), "second_ms": rnd(median(second), 1)}


DEVTOOLS_BEFORE_MS = 196.2  # pierwszy exec w terminalu Orki spoza Narzędzi deweloperskich
# kara Gatekeepera poniżej tej wartości znaczy, że Orka już jest zwolniona (bez zwolnienia
# 190 ms, kopia binarki o tym samym hashu 77 ms)
GATEKEEPER_EXEMPT_MS = 30


def record_gatekeeper(cfg, state, result):
    """Pierwszy pomiar z terminala Orki uruchomionej po zapisie devtools to wynik "po"."""
    record = state["applied"].get("devtools")
    if (
        not record
        or record.get("result")
        or result.get("responsible") != orcahost.bundle_name(ORCA_APP)
        or result.get("first_ms") is None
    ):
        return False
    started = orca_started()
    if not started or started < record["at"]:
        return False  # ta sama Orca co przed zmianą: to jeszcze pomiar "przed"
    record["result"] = {"before": DEVTOOLS_BEFORE_MS, "after": result["first_ms"]}
    sync_root(cfg, state)
    return True


def bench_gatekeeper():
    """Ile kosztuje pierwsze uruchomienie nowej binarki tam, gdzie działa ten proces.

    Odpowiedzialna aplikacja decyduje: z Narzędzia deweloperskiego (Terminal) binarka startuje
    od razu, z Orki spoza tej listy czeka na skan XProtect i zapytanie do Apple."""
    result = {"responsible": responsible_app()}
    probe = first_exec()
    if probe:
        result.update(probe)
        result["penalty_ms"] = rnd(probe["first_ms"] - probe["second_ms"], 1)
    return result


def typescript_load(cache_dir=None, runs=5):
    """Mediana czasu `require('typescript')` w node (ms), z cache kompilacji albo bez."""
    node = janitor.which("node")
    candidates = sorted(
        glob.glob(
            os.path.join(
                HOME,
                "Documents/*/node_modules/.pnpm/typescript@[1-6]*/node_modules/typescript",
            )
        )
        + glob.glob(
            os.path.join(
                HOME,
                "Documents/*/*/node_modules/.pnpm/typescript@[1-6]*/node_modules/typescript",
            )
        )
        + glob.glob(
            os.path.join(HOME, ".nvm/versions/node/*/lib/node_modules/typescript")
        )
    )
    if not node or not candidates:
        return None
    env = dict(janitor.ENV)
    env.pop("NODE_COMPILE_CACHE", None)
    if cache_dir:
        env["NODE_COMPILE_CACHE"] = cache_dir
    code = f"require({json.dumps(os.path.realpath(candidates[-1]))})"
    times = []
    for i in range(runs + (1 if cache_dir else 0)):
        started = time.perf_counter()
        done = subprocess.run(
            [node, "-e", code], env=env, capture_output=True, check=False
        )
        if done.returncode != 0:
            return None
        if cache_dir and i == 0:
            continue  # pierwszy przebieg zapisuje cache
        times.append((time.perf_counter() - started) * 1000)
    return rnd(median(times), 0)


def git_status_ms(system, repos, runs=5, write_index=False):
    """Mediana `git status --porcelain` (ms) po repozytoriach z listy; bez blokad indeksu.
    `write_index` pozwala gitowi zapisać indeks, jak zwykły status agenta: dopiero wtedy
    untrackedCache i token fsmonitora trafiają do indeksu."""
    times = []
    for repo in repos:
        for _ in range(runs):
            started = time.perf_counter()
            subprocess.run(
                ["git", "-C", repo, "status", "--porcelain"],
                capture_output=True,
                env=janitor.ENV
                if write_index
                else dict(janitor.ENV, GIT_OPTIONAL_LOCKS="0"),
                check=False,
            )
            times.append((time.perf_counter() - started) * 1000)
    return rnd(median(times), 0) if times else None


# ---------- Ultra: jeden przełącznik dla pracy agentów ----------

# kolejność ma znaczenie: najpierw to, co działa od razu
ULTRA = [
    "bg-helpers",
    "claude-hooks-async",
    "claude-hooks-native",
    "node-compile-cache",
    "devguard-budget",
    "devguard-max-server",
    "git-speed",
    "fast-npx-hooks",
    "claude-limits",
    "workflow-size",
    "claude-ui",
    "subagent-cache-1h",
    "tether-profile",
    "rg-threads",
    "security-slim",
]
# wyniki z transkryptów liczone najwyżej raz na tyle sekund (doba transkryptów to ~10 s)
AGENTS_CHECK_SECONDS = 1800
# "po" dla limitu Bash dopiero po tylu godzinach od włączenia: ścięć jest kilka na dobę
CAPPED_AFTER_HOURS = 6
# "po" dla cache subagentów dopiero po pełnej dobie: noc i dzień mają inne przerwy
COLD_AFTER_HOURS = 24
# co sprawdzić co najwyżej raz na tyle sekund (mdfind trwa około sekundy)
SPOTLIGHT_CHECK_SECONDS = 600
# poprawka hooków liczy się dopiero po tylu zdarzeniach od włączenia
HOOK_SAMPLES = 30
SETTLE_SECONDS = 2


def ultra_state(state):
    ultra = state.setdefault("ultra", {})
    ultra.setdefault("on", False)
    ultra.setdefault("since", None)
    ultra.setdefault("applied", [])
    ultra.setdefault("pending_root", [])
    ultra.setdefault("pending_manual", [])
    ultra.setdefault("results", {})
    # poprawki Ultry cofnięte ręcznie przy włączonej Ultrze: keep ich nie przywraca
    ultra.setdefault("declined", [])
    return ultra


def pending_root(cfg, state):
    """Poprawki roota, które w tej chwili mają sens; robi je perf-root.sh."""
    names = []
    if "vnodes" not in state["applied"]:
        names.append("vnodes")
    last = state["bench"].get("network", {}).get("result", {})
    bloat = (last.get("up_net_p90_ms") or 0) - (last.get("idle_ms") or 0)
    if bloat > 50 and "shaper" not in state["applied"]:
        names.append("shaper")  # sieć puchnie przy wysyłaniu: router bez SQM
    return names


def spotlight_indexed(cfg):
    """Ile plików Spotlight trzyma w indeksie pod katalogami z `spotlight_noise`."""
    total = 0
    for path in cfg.get("spotlight_noise", []):
        path = os.path.expanduser(path)
        if not os.path.isdir(path):
            continue
        out = janitor.run(
            ["mdfind", "-onlyin", path, "-count", 'kMDItemDisplayName == "*"c'],
            timeout=60,
        )
        try:
            total += int(out.strip())
        except (AttributeError, ValueError):
            continue
    return total


def spotlight_result(cfg, state, force=False):
    """Wynik do kliknięcia w Ustawieniach: pliki pakietów i modułów w indeksie Spotlight.

    mdfind trwa około sekundy, więc liczymy najwyżej raz na SPOTLIGHT_CHECK_SECONDS.
    Gdy użytkownik wykluczy katalogi, liczba spada i wynik dostaje "after".
    """
    ultra = ultra_state(state)
    result = ultra["results"].get("spotlight-privacy")
    if result and result.get("after") is not None:
        return
    checked = state.get("spotlight_checked", 0)
    if not force and time.time() - checked < SPOTLIGHT_CHECK_SECONDS:
        return
    state["spotlight_checked"] = time.time()
    count = spotlight_indexed(cfg)
    if result is None:
        if count > 0:
            paths = ", ".join(cfg.get("spotlight_noise", []))
            ultra["results"]["spotlight-privacy"] = {
                "before": count,
                "after": None,
                "unit": "plików w indeksie Spotlight",
                "note": f"dodaj w Ustawienia > Spotlight > Prywatność: {paths}",
            }
    elif count < result["before"] * 0.1:
        result["after"] = count
        result.pop("note", None)


def orca_started():
    """Kiedy (epoch) wystartował główny proces Orki; None, gdy nie działa."""
    main = os.path.join(ORCA_APP, "Contents/MacOS", HOST.executable)
    for pid, command in own_processes().items():
        if command != main and not command.startswith(main + " "):
            continue
        # etime: [[dd-]hh:]mm:ss
        out = janitor.run(["ps", "-o", "etime=", "-p", str(pid)]) or ""
        days, _, clock = out.strip().rpartition("-")
        parts = clock.split(":")
        if not all(part.isdigit() for part in parts):
            return None
        seconds = 0
        for part in parts:
            seconds = seconds * 60 + int(part)
        return time.time() - seconds - int(days or 0) * 86400
    return None


def pending_manual(state):
    """Rzeczy do kliknięcia przez człowieka: Ultra ich nie zrobi sama."""
    names = []
    # SIP nie wpuszcza nawet roota do TCC.db, więc Orkę na listę dodaje "+" w Ustawieniach
    devtools = state["applied"].get("devtools")
    # Orka dodana ręcznie, bez perf-root: pomiar z jej terminala bez kary jest dowodem
    gatekeeper = state["bench"].get("gatekeeper", {}).get("result", {})
    exempt = (
        gatekeeper.get("responsible") == orcahost.bundle_name(ORCA_APP)
        and gatekeeper.get("penalty_ms") is not None
        and gatekeeper["penalty_ms"] < GATEKEEPER_EXEMPT_MS
    )
    if os.path.isdir(ORCA_APP) and not devtools and not exempt:
        names.append("devtools")
    elif devtools:
        # zwolnienie z oceny Gatekeepera dostaje dopiero Orca uruchomiona po zmianie
        started = orca_started()
        if started and started < devtools["at"]:
            names.append("devtools-restart")
    spotlight = ultra_state(state)["results"].get("spotlight-privacy")
    # krok ręczny znika, gdy perf-root.sh przełączył Spotlight na same aplikacje
    if (
        spotlight
        and spotlight.get("after") is None
        and "spotlight" not in state["applied"]
    ):
        names.append("spotlight-privacy")
    record = state["applied"].get("docker-vm")
    if record and record.get("written") and not record.get("active"):
        names.append("docker-restart")
    elif record and not record.get("written"):
        names.append("docker-quit")
    return names


def measure_before_after(item, cfg, system, record):
    """Wynik do panelu: {"before", "after", "unit"} albo None, gdy nie ma czego mierzyć."""
    name = item.name
    if name == "bg-helpers":
        return None  # mierzone przy włączaniu, patrz ultra_on
    if name == "node-compile-cache":
        before = typescript_load()
        after = typescript_load(COMPILE_CACHE_DIR)
        if before is None or after is None:
            return None
        return {"before": before, "after": after, "unit": "ms require('typescript')"}
    if name == "devguard-budget":
        prev = record.get("prev")
        before = 35 if prev == MISSING or prev is None else prev
        return {
            "before": before,
            "after": record.get("value"),
            "unit": "% RAM na dev serwery",
        }
    if name == "devguard-max-server":
        prev = record.get("prev")
        before = 5 if prev == MISSING or prev is None else prev
        return {
            "before": before,
            "after": record.get("value"),
            "unit": "GB na jeden dev serwer",
        }
    if name == "security-slim":
        return item.measure(record) if record.get("plugin") else None
    if name == "docker-vm":
        prev = record.get("prev")
        before = 8192 if prev == MISSING or prev is None else prev
        result = {
            "before": round(before / 1024, 1),
            "after": round(record.get("value", before) / 1024, 1),
            "unit": "GB RAM maszyny Dockera",
        }
        if not record.get("written"):
            result["note"] = "zapisze się po zamknięciu Dockera"
        elif not record.get("active"):
            result["note"] = "zadziała po restarcie Dockera"
        return result
    return None


def ultra_on(cfg, system, state):
    """Włącza wszystko z ULTRA, co jeszcze nie działa; drugi raz niczego nie psuje."""
    ultra = ultra_state(state)
    if not ultra["on"]:
        ultra["since"] = time.time()
        ultra["applied"] = []
        ultra["results"] = {}
    ultra["on"] = True
    report = []
    # pozycje, których ta wersja nie zna (nałożyła je inna, np. deweloperska), zostają nietknięte
    for name in [n for n in ultra["applied"] if n not in ULTRA and tweak(n) is not None]:
        record = state["applied"].pop(name, None)
        ultra["applied"].remove(name)
        ultra["results"].pop(name, None)
        if record is None:
            continue
        try:
            restored = tweak(name).undo(record, system)
        except Deferred:
            state.setdefault("deferred", {})[name] = record
            restored = ["cofnę po zamknięciu Dockera"]
        report += [f"{name} (już nie w Ultra): {what}" for what in restored]
    for name in ULTRA:
        if name not in ultra["declined"]:
            report += ultra_apply(name, cfg, system, state)
    spotlight_result(cfg, state, force=True)
    sync_root(cfg, state)
    ultra["pending_manual"] = pending_manual(state)
    return report


def ultra_apply(name, cfg, system, state):
    """Włącza jedną poprawkę Ultry (albo pilnuje włączonej) i mierzy ją; zwraca zmiany."""
    ultra = ultra_state(state)
    item = tweak(name)
    old = state["applied"].get(name)
    if old is not None and name not in ultra["applied"]:
        return []  # włączone ręcznie przed Ultra: nie nasze, nie ruszamy
    before = None
    if name == "bg-helpers" and old is None:
        before = item.measure(cfg, system)
    # także gdy repozytoria doszły do włączonej już poprawki (pusta lista `git_repos`)
    if name == "git-speed" and not (old or {}).get("repos") and git_repos(cfg):
        before = git_status_ms(system, git_repos(cfg))
    try:
        record, changed = item.apply(cfg, system, old)
    except (OSError, ValueError, RuntimeError) as err:
        return [f"{name}: błąd {err}"]
    record["at"] = old["at"] if old else time.time()
    record["ultra"] = True
    if name == "docker-vm" and record.get("written"):
        # Docker wczyta nową wartość dopiero przy następnym starcie
        record.setdefault("active", bool((old or {}).get("active")))
    state["applied"][name] = record
    if name not in ultra["applied"]:
        ultra["applied"].append(name)
    result = None
    if name == "bg-helpers" and before is not None:
        # planista przenosi wątki na rdzenie E nie od razu
        time.sleep(SETTLE_SECONDS)
        after = item.measure(cfg, system)
        result = {"before": before, "after": after, "unit": "% rdzenia P"}
    elif name == "git-speed" and before is not None:
        # fsmonitor startuje demona i buduje cache przy pierwszym statusie: mierzymy
        # dopiero rozgrzane repo, tak jak zobaczy je następne polecenie agenta
        git_status_ms(system, git_repos(cfg), runs=2, write_index=True)
        time.sleep(SETTLE_SECONDS)
        after = git_status_ms(system, git_repos(cfg))
        result = {"before": before, "after": after, "unit": "ms git status"}
    elif (
        name in ("fast-npx-hooks", "claude-limits") and name not in ultra["results"]
    ):
        result = transcript_before(name, ultra["since"])
    elif name == "claude-hooks-async" and name not in ultra["results"]:
        since = ultra["since"]
        stats = hook_latency(since - 86400, since).get("PostToolUse")
        if stats:
            result = {
                "before": stats["p50"],
                "after": None,
                "unit": "ms hooków na narzędzie (p50)",
                "note": f"po {HOOK_SAMPLES} wywołaniach od włączenia",
            }
    elif (
        name == "subagent-cache-1h"
        and name not in ultra["results"]
        and record.get("prev") != record.get("value")
    ):
        # "przed" z dwóch dób transkryptów przed włączeniem tej poprawki (nie całej Ultry);
        # ustawione już wcześniej nie ma czego porównać
        at = record["at"]
        result = {
            "before": cold_per_day(at - 2 * 86400, at),
            "after": None,
            "unit": "mln tokenów cache subagentów od nowa na dobę",
            "note": f"po {COLD_AFTER_HOURS} h od włączenia",
        }
    elif name not in ultra["results"]:
        result = measure_before_after(item, cfg, system, record)
    if result:
        ultra["results"][name] = result
    return [f"{name}: {what}" for what in changed]


def transcript_before(name, since):
    """Wynik "przed" z transkryptów sprzed włączenia; "po" dokłada refresh_ultra."""
    if name == "fast-npx-hooks":
        stats = agent_turnaround(since - 86400, since)["formatted_edits"]
        if not stats["n"]:
            return None
        return {
            "before": stats["p50"],
            "after": None,
            "unit": "ms edycji pliku formatowanego (p50)",
            "note": "po 20 edycjach .ts/.md/.json od włączenia",
        }
    capped = agent_turnaround(since - 2 * 86400, since)["capped"]
    return {
        "before": rnd(capped / 2, 1),
        "after": None,
        "unit": "poleceń ściętych do 10 min na dobę",
        "note": f"po {CAPPED_AFTER_HOURS} h od włączenia",
    }


def transcript_after(ultra, state, force=False):
    """Wyniki "po" z transkryptów od włączenia Ultry, najwyżej raz na AGENTS_CHECK_SECONDS."""
    waiting = [
        n
        for n in ("fast-npx-hooks", "claude-limits", "subagent-cache-1h")
        if n in ultra["results"] and ultra["results"][n].get("after") is None
    ]
    if not waiting or not ultra["since"]:
        return
    if (
        not force
        and time.time() - state.get("agents_checked", 0) < AGENTS_CHECK_SECONDS
    ):
        return
    state["agents_checked"] = time.time()
    cache = ultra["results"].get("subagent-cache-1h")
    at = state["applied"].get("subagent-cache-1h", {}).get("at")
    if cache and cache.get("after") is None and at:
        if time.time() - at >= COLD_AFTER_HOURS * 3600:
            cache["after"] = cold_per_day(at, time.time())
            cache.pop("note", None)
    if not [n for n in waiting if n != "subagent-cache-1h"]:
        return
    data = agent_turnaround(ultra["since"])
    elapsed = time.time() - ultra["since"]
    fast = ultra["results"].get("fast-npx-hooks")
    if fast and fast.get("after") is None and data["formatted_edits"]["n"] >= 20:
        fast["after"] = data["formatted_edits"]["p50"]
        fast.pop("note", None)
    limits = ultra["results"].get("claude-limits")
    if limits and limits.get("after") is None and elapsed >= CAPPED_AFTER_HOURS * 3600:
        limits["after"] = rnd(data["capped"] * 86400 / elapsed, 1)
        limits.pop("note", None)


def ultra_off(cfg, system, state):
    """Cofa dokładnie to, co włączyła Ultra; ręcznie włączone poprawki zostają."""
    ultra = ultra_state(state)
    report = []
    for name in reversed(ultra["applied"]):
        record = state["applied"].get(name)
        if record is None:
            continue
        if tweak(name) is None:
            report.append(f"{name}: nieznana tej wersji, zostawiam (cofnie ją wersja, która ją nałożyła)")
            continue
        try:
            restored = tweak(name).undo(record, system)
        except Deferred:
            state.setdefault("deferred", {})[name] = record
            report.append(f"{name}: cofnę po zamknięciu Dockera")
        else:
            report += [f"{name}: {what}" for what in restored]
        del state["applied"][name]
    ultra.update(on=False, applied=[], declined=[], pending_root=[], pending_manual=[])
    return report


# jednostka zmierzonego efektu poprawki roota, jak "unit" w wynikach Ultra
ROOT_UNITS = {
    "vnodes": "s drugi przebieg lstat node_modules",
    "devtools": "ms pierwszego uruchomienia nowej binarki",
    "shaper": "ms kolejki wysyłania",
    "spotlight": "plików w indeksie poza aplikacjami",
    "iogpu": "MiB pamięci dla GPU",
}


def sync_root(cfg, state):
    """Poprawki roota nie należą do Ultra (cofa je tylko perf-root.sh), ale panel pokazuje je
    obok niej: `root_applied`, ich wyniki i świeżą listę tego, co jeszcze czeka na roota."""
    ultra = ultra_state(state)
    names = [n for n in ROOT_UNITS if n in state["applied"]]
    ultra["root_applied"] = names
    for name in ROOT_UNITS:
        result = state["applied"].get(name, {}).get("result")
        if name in names and result:
            ultra["results"][name] = dict(result, unit=ROOT_UNITS[name])
        elif name not in ULTRA:
            ultra["results"].pop(name, None)
    ultra["pending_root"] = pending_root(cfg, state) if ultra["on"] else []


def refresh_ultra(cfg, state, system):
    """Wyniki, które przychodzą później: hooki z nowych transkryptów, Docker po restarcie."""
    ultra = ultra_state(state)
    if not ultra["on"]:
        return
    hooks = ultra["results"].get("claude-hooks-async")
    if hooks and hooks.get("after") is None and ultra["since"]:
        # działające sesje Claude Code łapią zmianę hooków w locie (zmierzone: po włączeniu
        # wpisy cavemem znikają z transkryptów także starych sesji); wpisy, które jeszcze
        # przyszły, nie wstrzymują już sesji
        skip = [spec["match"] for spec in cfg.get("async_hooks", [])]
        stats = hook_latency(ultra["since"], skip=skip)
        stats = stats.get("PostToolUse")
        if stats and stats["n"] >= HOOK_SAMPLES:
            hooks["after"] = stats["p50"]
            hooks.pop("note", None)
    docker = state["applied"].get("docker-vm")
    if docker and docker.get("written") and not docker.get("active"):
        total = system.docker_memory()
        if total and abs(total / 2**20 - docker["value"]) < 512:
            docker["active"] = True
    spotlight_result(cfg, state)
    transcript_after(ultra, state)
    if docker and "docker-vm" in ultra["applied"]:
        # notatka idzie za stanem: czeka na zamknięcie, czeka na restart, działa
        result = measure_before_after(tweak("docker-vm"), cfg, system, docker)
        ultra["results"]["docker-vm"] = result
    ultra["pending_manual"] = pending_manual(state)
    sync_root(cfg, state)


def cmd_ultra(cfg, args, system=None):
    system = system or System()
    action = args[0] if args else "status"
    state = load_state()
    if action == "on":
        ultra_state(state)["declined"] = []  # "wszystko" znaczy także to, co cofnięto ręcznie
        report = ultra_on(cfg, system, state)
        save_state(state)
        log(f"ultra on: {len(report)} zmian")
    elif action == "off":
        report = ultra_off(cfg, system, state)
        save_state(state)
        log(f"ultra off: {len(report)} cofnięć")
    elif action == "status":
        report = []
        refresh_ultra(cfg, state, system)
        save_state(state)
    else:
        print("użycie: perf.py ultra on|off|status [--json]", file=sys.stderr)
        return 2
    ultra = ultra_state(state)
    if "--json" in args:
        print(json.dumps(ultra, ensure_ascii=False))
        return 0
    for line in report:
        print(line)
    print(f"Ultra: {'włączona' if ultra['on'] else 'wyłączona'}")
    if ultra["on"] and ultra["since"]:
        print(f"  od {ago(ultra['since'])}")
    for name in ultra["applied"]:
        item, record = tweak(name), state["applied"].get(name, {})
        if item is None:
            print(f"  [?] {name}: nieznana tej wersji claude-acc (nałożyła ją inna wersja), zostawiam")
            continue
        print(f"  [x] {name}: {item.describe(record, system)}")
        result = ultra["results"].get(name)
        if result:
            after = "?" if result.get("after") is None else fmt(result["after"])
            note = f" ({result['note']})" if result.get("note") else ""
            print(f"        {fmt(result['before'])} -> {after} {result['unit']}{note}")
    for name in ultra["pending_root"]:
        print(f"  [ ] {name} (root): {tweak(name).command}")
    for name in ultra["pending_manual"]:
        print(f"  [ ] {name}: {MANUAL[name]}")
    return 0


MANUAL = {
    "devtools": f"Ustawienia > Prywatność i ochrona > Narzędzia deweloperskie > + > {HOST.name} "
    "(`claude-acc perf-root devtools add` w Terminalu otwiera panel i zapisuje zmianę)",
    "devtools-restart": f"zrestartuj {HOST.name}: Narzędzia deweloperskie działają dopiero dla aplikacji "
    "uruchomionej po zmianie (restart zamyka sesje w jej terminalach)",
    "spotlight-privacy": "Ustawienia > Spotlight > Prywatność wyszukiwania: dodaj katalogi "
    "z `spotlight_noise` (magazyn pnpm, moduły Go)",
    "docker-restart": "nowa pamięć maszyny Dockera zadziała po jego restarcie (Docker > Restart)",
    "docker-quit": "zapis limitu pamięci Dockera czeka, aż Docker będzie zamknięty",
}


# ---------- komendy ----------


def ago(stamp):
    seconds = int(time.time() - stamp)
    if seconds >= 86400:
        return f"{seconds // 86400} dni temu"
    if seconds >= 3600:
        return f"{seconds // 3600} godz. temu"
    return f"{max(seconds // 60, 0)} min temu"


def fmt(value, unit=""):
    if value is None:
        return "?"
    text = f"{value:.0f}" if abs(value) >= 10 else f"{value:.1f}"
    return text.replace(".", ",") + unit


def describe_network(r):
    where = r.get("where", {})
    lines = []
    lines.append(
        f"{fmt(r.get('down_mbps'))}/{fmt(r.get('up_mbps'))} Mb/s "
        f"({where.get('interface')}, brama {where.get('gateway')})"
    )
    lines.append(
        f"opóźnienie: bez obciążenia {fmt(r.get('idle_ms'), ' ms')}, "
        f"przy pobieraniu {fmt(r.get('down_loaded_ms'), ' ms')}, "
        f"przy wysyłaniu {fmt(r.get('up_loaded_ms'), ' ms')}"
    )
    if r.get("down_net_p90_ms") is not None:
        lines.append(
            f"z tego sieć (osobne połączenia, p90): {fmt(r.get('down_net_p90_ms'), ' ms')}"
            f" / {fmt(r.get('up_net_p90_ms'), ' ms')}; kolejka w połączeniu: "
            f"{fmt(r.get('down_self_ms'), ' ms')} / {fmt(r.get('up_self_ms'), ' ms')}"
        )
    if r.get("shaper"):
        lines.append(f"z ogranicznikiem wysyłania {r['shaper']}")
    gateway, internet = r.get("gateway_ping") or {}, r.get("internet_ping") or {}
    if gateway.get("avg_ms") is not None or internet.get("avg_ms") is not None:
        lines.append(
            f"ping: brama {fmt(gateway.get('avg_ms'), ' ms')} "
            f"(max {fmt(gateway.get('max_ms'), ' ms')}), "
            f"1.1.1.1 {fmt(internet.get('avg_ms'), ' ms')} "
            f"(max {fmt(internet.get('max_ms'), ' ms')})"
        )
    busy = r.get("talkers") or []
    if busy:
        names = ", ".join(
            f"{t['name']} {fmt(t['down_mbps'])}/{fmt(t['up_mbps'])} Mb/s"
            for t in busy[:3]
        )
        lines.append(f"w tle w chwili pomiaru: {names}")
    api = r.get("anthropic") or {}
    if api:
        lines.append(
            f"api.anthropic.com: TCP {fmt(api.get('tcp_ms'), ' ms')}, "
            f"TLS {fmt(api.get('tls_ms'), ' ms')}, "
            f"pierwszy bajt {fmt(api.get('ttfb_ms'), ' ms')}"
        )
    return lines


def describe_cpu(r):
    lines = []
    lines.append(
        f"1 proces: {fmt(r.get('single_mbs'))} MB/s, "
        f"{fmt(r.get('single_pshare'))}% na rdzeniach P, "
        f"w kolejce {fmt(r.get('single_wait_pct'))}% czasu"
    )
    lines.append(
        f"{r.get('multi_procs')} procesów: {fmt(r.get('multi_mbs'))} MB/s razem, "
        f"{fmt(r.get('multi_pshare'))}% na rdzeniach P, "
        f"w kolejce {fmt(r.get('multi_wait_pct'))}%"
    )
    lines.append(
        f"wybudzenie wątku po 1 ms: p50 {fmt(r.get('wake_p50_us'), ' µs')}, "
        f"p99 {fmt(r.get('wake_p99_us'), ' µs')}, "
        f"max {fmt(r.get('wake_max_us'), ' µs')} spóźnienia"
    )
    if r.get("hogs"):
        names = ", ".join(
            f"{h['name']} {fmt(h['watts'], ' W')} ({fmt(h['pcore_pct'])}% rdzenia P)"
            for h in r["hogs"][:4]
        )
        lines.append(f"najwięcej energii: {names}")
    return lines


def describe_gpu(r):
    lines = []
    lines.append(
        f"GPU zajęte {fmt(r.get('device_pct'))}% "
        f"(max {fmt(r.get('device_max_pct'))}%), "
        f"WindowServer {fmt(r.get('windowserver_cpu_pct'))}% CPU"
    )
    lat = r.get("latency") or {}
    if lat:
        lines.append(
            f"małe zlecenie na GPU: p50 {fmt(lat.get('roundtrip_p50_us'), ' µs')}, "
            f"p99 {fmt(lat.get('roundtrip_p99_us'), ' µs')} w obie strony"
        )
    clients = r.get("clients") or []
    if clients:
        names = ", ".join(f"{c['name']} {fmt(c['gpu_pct'])}%" for c in clients[:5])
        lines.append(f"czas GPU: {names}")
    return lines


def describe_fs(r):
    if r.get("error"):
        return [r["error"]]
    walk = (
        f"lstat {r.get('entries')} wpisów w {r.get('path')}: pierwszy przebieg "
        f"{fmt(r.get('first_s'), ' s')}, drugi {fmt(r.get('warm_s'), ' s')}"
    )
    cache = (
        f"vnode z odzysku w drugim przebiegu: {r.get('warm_recycled')} "
        f"(kern.maxvnodes {r.get('maxvnodes')})"
    )
    return [walk, cache]


def describe_agents(r):
    lines = [
        f"obrót narzędzi agentów z ostatnich {r.get('hours')} h (ms p50 / p90, liczba):"
    ]
    for name, st in (r.get("tools") or {}).items():
        lines.append(f"  {name}: {fmt(st['p50'])} / {fmt(st['p90'])} ({st['n']})")
    edits = r.get("formatted_edits") or {}
    if edits.get("n"):
        lines.append(
            f"edycja pliku formatowanego (.ts/.md/.json...): {fmt(edits['p50'])} / "
            f"{fmt(edits['p90'])} ms ({edits['n']})"
        )
    hooks = r.get("hooks") or {}
    if hooks:
        parts = ", ".join(
            f"{k} {fmt(v['p50'])}/{fmt(v['p90'])} ms" for k, v in hooks.items()
        )
        lines.append(f"czekanie na hooki (p50/p90): {parts}")
    slow = r.get("slow_hooks") or []
    if slow:
        lines.append("hooki, które kosztują najwięcej (tylko te, które coś wypisały; ms p50 / p90, liczba):")
        for h in slow:
            lines.append(f"  {h['hook']} ({h['event']}): {fmt(h['p50'])} / {fmt(h['p90'])} ({h['n']})")
    lines.append(
        f"polecenia ścięte do 10 min wbrew timeoutowi agenta: {fmt(r.get('capped_per_day'))} na dobę"
    )
    model = r.get("model") or {}
    if model.get("requests"):
        lines.append(
            f"model: {model['requests']} zapytań ({fmt(model.get('sub_share'))}% subagenci), "
            f"{fmt(model['s'] / 3600, ' h')}; narzędzia {fmt(r.get('tools_s', 0) / 3600, ' h')} "
            "(suma po równoległych agentach)"
        )
        lines.append(
            "opóźnienie modelu wg kontekstu (s p50 / p90, pierwszy blok p50, liczba):"
        )
        for label, st in (model.get("by_context") or {}).items():
            lines.append(
                f"  {label}: {fmt(st['p50'] / 1000)} / {fmt(st['p90'] / 1000)}, "
                f"{fmt(st['first_p50'] / 1000)} ({st['n']})"
            )
    floor = r.get("bash_floor") or {}
    if floor.get("n"):
        lines.append(
            f"podłoga Bash (polecenia < 5 s): p10 {fmt(floor['p10'], ' ms')}, "
            f"p50 {fmt(floor['p50'], ' ms')} ({floor['n']})"
        )
    totals = r.get("tools_by_total_s") or {}
    if totals:
        parts = ", ".join(
            f"{k} {fmt(v / 60, ' min')}" for k, v in list(totals.items())[:6]
        )
        lines.append(f"najwięcej czasu łącznie: {parts}")
    for who, label in (("sub", "subagenci"), ("main", "główne sesje")):
        cold = (r.get("cold_cache") or {}).get(who) or {}
        if cold.get("requests"):
            lines.append(
                f"cache od nowa po 5-60 min ciszy ({label}): {cold['requests']} zapytań, "
                f"{fmt(cold['mtok'])} mln tokenów, {fmt(cold['s'] / 60, ' min')}"
            )
    return lines


def describe_gatekeeper(r):
    if r.get("first_ms") is None:
        return [f"brak pomiaru (go w PATH?), odpowiedzialna aplikacja: {r.get('responsible')}"]
    return [
        f"odpowiedzialna aplikacja: {r.get('responsible')}",
        f"nowa binarka Go: pierwsze uruchomienie {fmt(r['first_ms'], ' ms')}, drugie "
        f"{fmt(r['second_ms'], ' ms')}; ocena Gatekeepera {fmt(r['penalty_ms'], ' ms')} na binarkę",
    ]


DESCRIBE = {
    "network": describe_network,
    "cpu": describe_cpu,
    "gpu": describe_gpu,
    "fs": describe_fs,
    "agents": describe_agents,
    "gatekeeper": describe_gatekeeper,
}
LABELS = {
    "network": "Sieć",
    "cpu": "CPU",
    "gpu": "GPU",
    "fs": "System plików",
    "agents": "Agenci",
    "gatekeeper": "Gatekeeper",
}


def cmd_status(cfg, args, system=None):
    system = system or System()
    state = load_state()
    tweaks = []
    for item in TWEAKS:
        record = state["applied"].get(item.name)
        entry = {
            "name": item.name,
            "group": item.group,
            "title": item.title,
            "effect": item.effect,
            "root": item.root,
            "applied": record is not None,
            "ultra": bool(record and record.get("ultra")),
        }
        if record:
            entry["at"] = record.get("at")
            if not item.root:
                entry["detail"] = item.describe(record, system)
            else:
                entry["detail"] = record.get("detail", "")
        tweaks.append(entry)
    if "--json" in args:
        out = {
            "tweaks": tweaks,
            "bench": state["bench"],
            "ultra": ultra_state(state),
            "link": state.get("link"),
        }
        print(json.dumps(out, ensure_ascii=False))
        return 0
    ultra = ultra_state(state)
    print(f"Ultra: {'włączona' if ultra['on'] else 'wyłączona'} (perf.py ultra status)")
    link = state.get("link")
    if link:
        kind = "tethering" if link.get("tethered") else "zwykłe łącze"
        print(
            f"Łącze ({ago(link['at'])}): {link.get('port') or '?'} ({link.get('iface')}, "
            f"brama {link.get('gateway')}), {kind}"
        )
    print("Poprawki:")
    for entry in tweaks:
        mark = "x" if entry["applied"] else " "
        kind = "root" if entry["root"] else entry["group"]
        print(f"  [{mark}] {entry['name']} [{kind}]: {entry['title']}")
        if entry.get("detail"):
            print(f"        {entry['detail']}")
    for name in state.get("deferred", {}):
        print(f"  ! {name}: cofnięcie czeka na zamknięcie Dockera")
    for kind in ("network", "cpu", "gpu", "fs", "agents"):
        last = state["bench"].get(kind)
        if not last:
            print(f"{LABELS[kind]}: jeszcze bez pomiaru (perf.py bench {kind})")
            continue
        load = last.get("load", {})
        print(
            f"{LABELS[kind]} ({ago(last['at'])}, load {fmt(load.get('load1'))}, "
            f"swap {fmt(load.get('swap_used_gb'), ' GB')}):"
        )
        for line in DESCRIBE[kind](last["result"]):
            print(f"  {line}")
    return 0


def record_bench(state, kind, result, started, load=None):
    """Wynik pomiaru w stanie; `load` to obciążenie Maca tuż przed pomiarem."""
    entry = {"at": started, "load": load or {}, "result": result}
    state["bench"][kind] = entry
    history = state.setdefault("history", {}).setdefault(kind, [])
    history.append(entry)
    del history[:-HISTORY]


def cmd_bench(cfg, args, system=None):
    kinds = [
        a
        for a in args
        if a in ("network", "cpu", "gpu", "fs", "agents", "gatekeeper", "all")
    ] or ["all"]
    if "all" in kinds:
        kinds = ["network", "cpu", "gpu"]
    runs = 1
    if "--runs" in args:
        runs = max(1, int(args[args.index("--runs") + 1]))
    results = {}
    for kind in kinds:
        started = time.time()
        load = system_load()
        print(f"Mierzę: {LABELS[kind]}...", file=sys.stderr)
        if kind == "network":
            result = bench_network(runs)
        elif kind == "cpu":
            result = bench_cpu(max(runs, 3))
        elif kind == "fs":
            result = bench_fs(cfg)
        elif kind == "agents":
            hours = 24
            if "--hours" in args:
                hours = max(1, int(args[args.index("--hours") + 1]))
            result = bench_agents(hours)
        elif kind == "gatekeeper":
            result = bench_gatekeeper()
        else:
            result = bench_gpu()
            result["latency"] = gpu_latency()
        # stan czytany tuż przed zapisem: pomiar trwa, a w tym czasie coś mogło go zmienić
        state = load_state()
        record_bench(state, kind, result, started, load)
        if kind == "gatekeeper" and record_gatekeeper(cfg, state, result):
            print("zapisane jako wynik devtools", file=sys.stderr)
        save_state(state)
        results[kind] = result
        log(f"bench {kind}: {json.dumps(result)}")
    if "--json" in args:
        print(json.dumps(results))
        return 0
    for kind, result in results.items():
        print(f"{LABELS[kind]}:")
        for line in DESCRIBE[kind](result):
            print(f"  {line}")
    return 0


def selected(args):
    if "--all" in args:
        return [t for t in TWEAKS if not t.root]
    names = [a for a in args if not a.startswith("-")]
    found = []
    for name in names:
        item = tweak(name)
        if item is None:
            raise SystemExit(f"nie ma poprawki {name}; lista: perf.py list")
        found.append(item)
    if not found:
        raise SystemExit("podaj nazwę poprawki albo --all; lista: perf.py list")
    return found


def cmd_apply(cfg, args, system=None):
    system = system or System()
    dry_run = "--dry-run" in args
    state = load_state()
    code = 0
    for item in selected(args):
        if item.root:
            print(f"{item.name} wymaga roota: {item.command}")
            code = 2
            continue
        if dry_run:
            print(f"{item.name}: {item.title}")
            if isinstance(item, BackgroundHelpers):
                targets = item.targets(cfg, system)
                names = ", ".join(f"{short_command(c)} ({p})" for p, _, c in targets)
                print(f"  w tle: {names or 'nic (brak procesów z listy)'}")
            continue
        old = state["applied"].get(item.name)
        record, changed = item.apply(cfg, system, old)
        record["at"] = old["at"] if old else time.time()
        if old and old.get("ultra"):
            record["ultra"] = True
        state["applied"][item.name] = record
        for what in changed:
            print(f"{item.name}: {what}")
        if not changed:
            print(f"{item.name}: bez zmian ({item.describe(record, system)})")
        log(f"apply {item.name}: {len(changed)} zmian")
    if not dry_run:
        save_state(state)
    return code


def cmd_undo(cfg, args, system=None):
    system = system or System()
    state = load_state()
    if "--all" in args:
        items = [t for t in TWEAKS if t.name in state["applied"] and not t.root]
    else:
        items = selected(args)
    for item in items:
        record = state["applied"].get(item.name)
        if record is None:
            print(f"{item.name}: nie jest włączona")
            continue
        if item.root:
            print(f"{item.name} cofa root: {item.command.replace('trial', 'undo')}")
            continue
        try:
            restored = item.undo(record, system)
        except Deferred:
            state.setdefault("deferred", {})[item.name] = record
            print(f"{item.name}: cofnę, gdy Docker będzie zamknięty (perf.py keep)")
            restored = []
        else:
            print(f"{item.name}: cofnięte ({', '.join(restored) or 'nic do zmiany'})")
        del state["applied"][item.name]
        ultra = ultra_state(state)
        if item.name in ultra["applied"]:
            ultra["applied"].remove(item.name)
        if ultra["on"] and item.name in ULTRA and item.name not in ultra["declined"]:
            ultra["declined"].append(item.name)
        log(f"undo {item.name}")
    save_state(state)
    return 0


def cmd_keep(cfg, args, system=None):
    """Pilnuje włączonych poprawek (dla launchd co kilka minut): procesy z listy wstają
    z nowym pid, ktoś nadpisał settings.json, zapis do Dockera czekał na jego zamknięcie;
    kończy też cofnięcia, które musiały poczekać."""
    system = system or System()
    state = load_state()
    # łącze dla updates.py: na tetheringu zaplanowane aktualizacje czekają
    try:
        state["link"] = system.link()
    except (OSError, ValueError) as err:
        log(f"keep: łącze nieznane {err!r}")
    errors = state.setdefault("keep_errors", {})
    for name, record in list(state.get("deferred", {}).items()):
        if tweak(name) is None:
            continue
        try:
            tweak(name).undo(record, system)
        except Deferred:
            continue
        del state["deferred"][name]
        log(f"keep {name}: dokończone cofnięcie")
    for item in TWEAKS:
        old = state["applied"].get(item.name)
        if old is None or item.root:
            continue
        try:
            record, changed = item.apply(cfg, system, old)
        except (OSError, ValueError, RuntimeError) as err:
            # ten sam błąd co 5 min (docker-vm: macOS nie wpuszcza do kontenera Dockera)
            # trafia do logu raz, nowy znowu
            if errors.get(item.name) != repr(err):
                log(f"keep {item.name}: błąd {err!r}")
            errors[item.name] = repr(err)
            continue
        if errors.pop(item.name, None):
            log(f"keep {item.name}: znowu działa")
        record["at"] = old["at"]
        if old.get("ultra"):
            record["ultra"] = True
        if (
            item.name == "docker-vm"
            and record.get("written")
            and not old.get("written")
        ):
            record["active"] = False
        state["applied"][item.name] = record
        if changed:
            log(f"keep {item.name}: {', '.join(changed)}")
    ultra = ultra_state(state)
    if ultra["on"]:
        # aktualizacja claude-acc dokłada poprawki do Ultry: włączona Ultra je przejmuje
        for name in ULTRA:
            if name in ultra["applied"] or name in ultra["declined"]:
                continue
            if name in state["applied"]:
                continue  # włączona ręcznie, pilnuje jej pętla wyżej
            changed = ultra_apply(name, cfg, system, state)
            log(f"keep {name}: nowa w Ultra, {', '.join(changed) or 'bez zmian'}")
        refresh_ultra(cfg, state, system)
    save_state(state)
    return 0


def cmd_link(cfg, args, system=None):
    """Którędy idzie trasa domyślna i czy to tethering: ta sama odpowiedź, na którą działa
    tether-profile (link_now). Panel pyta przy każdej zmianie ścieżki sieciowej, zamiast
    zgadywać po swojemu; niczego nie zapisuje, bo perf-state.json należy do keep."""
    link = (system or System()).link()
    if "--json" in args:
        print(json.dumps(link, ensure_ascii=False))
        return 0
    kind = "tethering" if link.get("tethered") else "zwykłe łącze"
    print(f"{link.get('port') or '?'} ({link.get('iface')}, brama {link.get('gateway')}), {kind}")
    return 0


def cmd_list(cfg, args, system=None):
    for item in TWEAKS:
        root = " (root: " + item.command + ")" if item.root else ""
        ultra = " [Ultra]" if item.name in ULTRA else ""
        print(f"{item.name} [{item.group}]{ultra}{root}")
        print(f"  {item.title}")
        print(f"  zmierzone: {item.effect}")
    return 0


def shaper_rate(cfg, state, where):
    """Limit wysyłania dla tej sieci: procent uploadu z ostatniego pomiaru przy tej bramie."""
    for entry in reversed(state.get("history", {}).get("network", [])):
        result = entry.get("result", {})
        if result.get("shaper"):
            continue
        if result.get("where", {}).get("gateway") == where["gateway"] and result.get(
            "up_mbps"
        ):
            return int(result["up_mbps"] * cfg["shaper_percent"] / 100)
    return None


def cmd_shaper_rate(cfg, args, system=None):
    """Dla perf-root.sh: interfejs i limit w Mb/s, albo błąd, gdy brak pomiaru tej sieci."""
    where = network_id()
    rate = shaper_rate(cfg, load_state(), where)
    if not rate or not where["interface"]:
        print(
            "brak pomiaru tej sieci; najpierw: perf.py bench network", file=sys.stderr
        )
        return 1
    print(f"{where['interface']} {rate}Mbps")
    return 0


def iogpu_default_mb(total_bytes):
    """Proponowany iogpu.wired_limit_mb dla Maca z `total_bytes` RAM: cały RAM bez 8 GB dla
    systemu, najwyżej 85%. 0, gdy to nie więcej niż domyślne macOS (około 2/3 RAM): wtedy
    limit trzeba zostawić, bo mniejszy zabrałby GPU pamięć."""
    total = total_bytes // 1048576
    value = min(total - 8192, total * 85 // 100)
    return value if value > total * 2 // 3 else 0


def cmd_iogpu_default(cfg, args, system=None):
    """Dla perf-root.sh: proponowany iogpu.wired_limit_mb tego Maca (0: zostaw domyślne)."""
    print(iogpu_default_mb(sysctl_int("hw.memsize") or 0))
    return 0


def cmd_record(cfg, args, system=None):
    """Dla perf-root.sh: zapis albo usunięcie poprawki roota w stanie (`record shaper
    <opis>` / `record shaper --forget`), żeby panel i status ją widziały. `--result
    PRZED PO` dokłada zmierzony efekt (perf-root.sh trial --keep)."""
    name = args[0] if args else ""
    if tweak(name) is None or not tweak(name).root:
        return 2
    args = list(args[1:])
    result = None
    if "--result" in args:
        i = args.index("--result")
        try:
            result = {"before": float(args[i + 1]), "after": float(args[i + 2])}
        except (IndexError, ValueError):
            return 2
        del args[i : i + 3]
    state = load_state()
    if "--forget" in args:
        state["applied"].pop(name, None)
    else:
        old = state["applied"].get(name, {})
        detail = " ".join(args)
        # ten sam opis to ta sama zmiana (np. dopisany wynik): jej chwila się nie przesuwa
        at = old["at"] if old.get("detail") == detail and "at" in old else time.time()
        record = {"at": at, "detail": detail}
        if result or old.get("result"):
            record["result"] = result or old["result"]
        state["applied"][name] = record
    sync_root(cfg, state)
    save_state(state)
    log(f"record {' '.join(args)}")
    return 0


def cmd_keep_launchd(cfg, args):
    """`keep` z launchd: poprawki, a po nich hook admit obok łańcucha fasthooks (admitchain.py).
    Testy wołają cmd_keep wprost, więc prawdziwy settings.json zostaje poza nimi."""
    rc = cmd_keep(cfg, args)
    try:
        import admitchain

        change = admitchain.heal()
    except Exception as err:  # noqa: BLE001 - poprawki są ważniejsze niż ten krok
        log(f"keep: admitchain: błąd {err!r}")
    else:
        if change:
            log(f"keep: {change}")
    return rc


COMMANDS = {
    "status": cmd_status,
    "bench": cmd_bench,
    "apply": cmd_apply,
    "undo": cmd_undo,
    "keep": cmd_keep_launchd,
    "link": cmd_link,
    "list": cmd_list,
    "ultra": cmd_ultra,
    "shaper-rate": cmd_shaper_rate,
    "record": cmd_record,
    "iogpu-default": cmd_iogpu_default,
}


def main(argv):
    cmd = argv[0] if argv else "status"
    if cmd not in COMMANDS:
        print(__doc__)
        return 2
    try:
        return COMMANDS[cmd](load_config(), argv[1:])
    except SystemExit:
        raise
    except Exception as err:
        log(f"{cmd}: błąd {err!r}")
        print(f"błąd: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
