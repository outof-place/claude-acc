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

Poprawki wymagające roota robi perf-root.sh; tu są tylko opisane.

Ultra to jeden przełącznik dla pracy agentów (Orca, wiele sesji Claude Code, ich dev
serwery i Docker): włącza wszystkie poprawki z listy ULTRA, zapisuje, co było przed
nimi, i mierzy przed i po. Stan dla panelu jest w perf-state.json pod "ultra".

Komendy:
  status [--json]                     poprawki i ostatnie pomiary
  bench [network|cpu|gpu|fs|all]      pomiar; wynik ląduje w perf-state.json
        [--runs N] [--json]
  apply <nazwa>|--all [--dry-run]     włącz poprawkę (tylko te bez roota)
  undo <nazwa>|--all                  cofnij
  ultra on|off|status [--json]        wszystkie poprawki dla agentów naraz i ich wyniki
  keep                                pilnuj włączonych poprawek (dla launchd co 5 min)
  list                                poprawki z opisem i zmierzonym efektem
"""

import calendar
import ctypes
import ctypes.util
import glob
import hashlib
import json
import os
import plistlib
import re
import statistics
import subprocess
import sys
import time
from xml.parsers.expat import ExpatError

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import janitor

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


def own_processes():
    """{pid: linia poleceń} procesów tego użytkownika."""
    out = janitor.run(["ps", "-U", str(os.getuid()), "-o", "pid=,command="]) or ""
    procs = {}
    for line in out.splitlines():
        pid, _, command = line.strip().partition(" ")
        if pid.isdigit():
            procs[int(pid)] = command.strip()
    return procs


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
CLAUDE_SETTINGS = os.path.join(HOME, ".claude/settings.json")
CLAUDE_PROJECTS = os.path.join(HOME, ".claude/projects")
DEVGUARD_CONFIG = os.path.join(STATE_DIR, "devguard.json")
DOCKER_SETTINGS = os.path.join(
    HOME, "Library/Group Containers/group.com.docker/settings-store.json"
)
COMPILE_CACHE_DIR = os.path.join(HOME, "Library/Caches/node-compile-cache")
DOCKER_CLI = "/Applications/Docker.app/Contents/Resources/bin/docker"
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
    # czeka, a każde wywołanie narzędzia czekało na start node (54 ms p50)
    "async_hooks": [
        {
            "event": "PostToolUse",
            "match": "cavemem/dist/index.js hook run post-tool-use",
        },
        {"event": "Stop", "match": "cavemem/dist/index.js hook run stop"},
    ],
    # limit dev serwerów strażnika w Ultra (procent RAM; strażnik domyślnie ma 35)
    "devguard_budget_percent": 25,
    # pamięć maszyny Dockera w Ultra; zapis tylko przy zamkniętym Dockerze, działa od
    # jego następnego startu (kontenery używały 3,7 GB z 8)
    "docker_memory_mib": 6144,
    # repozytoria, w których Ultra włącza core.untrackedCache i core.fsmonitor; puste,
    # bo jedyne repo z worktree Orki (portivo) zmienia tylko jego właściciel
    "git_repos": [],
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

    def docker_running(self):
        """Docker Desktop trzyma ustawienia w pamięci i nadpisuje plik; piszemy tylko bez niego."""
        out = janitor.run(["pgrep", "-x", "com.docker.backend"])
        return bool(out and out.strip())

    def git(self, repo, *args):
        """Wyjście gita albo None (brak klucza w configu to też None)."""
        return janitor.run(["git", "-C", repo, *args], timeout=120)

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
        "narzędzie; równoległy hook Orki trwa 26/71 ms (transkrypty z 24 h, 12 tys. wywołań)"
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
                    if spec["match"] in hook.get("command", ""):
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


class Deferred(Exception):
    """Cofnięcie musi poczekać (Docker działa); `keep` dokończy je później."""


class GitSpeed:
    """core.untrackedCache i core.fsmonitor w repozytoriach z listy `git_repos`."""

    name = "git-speed"
    group = "dev"
    root = False
    title = "git status bez skanowania drzewa: untrackedCache + fsmonitor (repozytoria z `git_repos`)"
    effect = (
        "klon portivo (14 tys. plików): git status 71 -> 31 ms z untrackedCache, 26 ms z "
        "fsmonitor; feature.manyFiles (index v4) nic nie dodał"
    )
    KEYS = (("core.untrackedCache", "true"), ("core.fsmonitor", "true"))

    def apply(self, cfg, system, record=None):
        repos = dict((record or {}).get("repos", {}))
        changed = []
        for repo in [os.path.expanduser(r) for r in cfg.get("git_repos", [])]:
            if repo in repos or system.git(repo, "rev-parse", "--git-dir") is None:
                continue
            prev = {}
            for key, value in self.KEYS:
                current = system.git(repo, "config", "--local", "--get", key)
                prev[key] = current.strip() if current is not None else None
                system.git(repo, "config", "--local", key, value)
            system.git(repo, "update-index", "--untracked-cache")
            repos[repo] = prev
            changed.append(repo)
        return {"repos": repos}, changed

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
        return restored

    def describe(self, record, system):
        repos = (record or {}).get("repos", {})
        return (
            ", ".join(short_path(r) for r in repos) or "brak repozytoriów w `git_repos`"
        )


class RootTweak:
    """Poprawka, która wymaga roota: perf.py tylko ją opisuje, robi ją perf-root.sh."""

    root = True
    group = "root"

    def __init__(self, name, title, effect, command):
        self.name, self.title, self.effect, self.command = name, title, effect, command


TWEAKS = [
    BackgroundHelpers(),
    AsyncHooks(),
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
    RootTweak(
        "vnodes",
        "większy cache vnode (kern.maxvnodes 263168 -> 786432): drzewa node_modules mieszczą się w nim",
        "lstat 358 tys. wpisów portivo/.pnpm: 3,6 s w każdym przebiegu i 250 tys. vnode z odzysku, "
        "bo cache ma 263 tys.; 28 mln odzysków w 5 h pracy",
        "sudo ./perf-root.sh vnodes trial",
    ),
    RootTweak(
        "shaper",
        "ogranicznik wysyłania na interfejsie (ifconfig tbr): kolejka zostaje w fq_codel "
        "Maca zamiast w buforze routera",
        "przy BE230 sieć pod obciążeniem dokłada 1-4 ms, więc nie ma czego ratować; "
        "przy Zyxelu jeszcze niezmierzone (sprawdź linię „z tego sieć” w bench network)",
        "sudo ./perf-root.sh trial",
    ),
]


def tweak(name):
    for item in TWEAKS:
        if item.name == name:
            return item
    return None


def short_command(command):
    """Czytelna nazwa procesu: plik skryptu albo program, bez ścieżek."""
    parts = command.split()
    for part in parts[1:]:
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
    since,
    until=None,
    events=("PreToolUse", "PostToolUse", "Stop"),
    skip=(),
    born_after=None,
):
    """Ile sesja Claude Code czeka na hooki przy jednym zdarzeniu (ms): najdłuższy z
    równoległych hooków, z transkryptów. Hooki, których komenda zawiera coś z `skip`
    (puszczone w tle), nie wstrzymują sesji, więc się nie liczą. `born_after` bierze
    tylko sesje otwarte po tej chwili: Claude Code czyta hooki raz, przy starcie sesji.

    {"PostToolUse": {"n": ..., "p50": ..., "p90": ...}, ...}
    """
    until = until or time.time()
    calls = {event: {} for event in events}
    for path in glob.glob(os.path.join(CLAUDE_PROJECTS, "*", "*.jsonl")):
        try:
            if os.path.getmtime(path) < since:
                continue
            if born_after and janitor.born(path) < born_after:
                continue
            records = list(hook_records(path))
        except OSError:
            continue
        for event, key, ms, stamp, command in records:
            if event not in calls or not since <= stamp <= until:
                continue
            if any(s in command for s in skip):
                calls[event].setdefault(key, 0)
                continue
            calls[event][key] = max(calls[event].get(key, 0), ms)
    result = {}
    for event, per_call in calls.items():
        values = list(per_call.values())
        if values:
            result[event] = {
                "n": len(values),
                "p50": rnd(percentile(values, 0.5), 0),
                "p90": rnd(percentile(values, 0.9), 0),
            }
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


def git_status_ms(system, repos, runs=5):
    """Mediana `git status --porcelain` (ms) po repozytoriach z listy; bez blokad indeksu."""
    times = []
    for repo in repos:
        for _ in range(runs):
            started = time.perf_counter()
            subprocess.run(
                ["git", "-C", repo, "status", "--porcelain"],
                capture_output=True,
                env=dict(janitor.ENV, GIT_OPTIONAL_LOCKS="0"),
                check=False,
            )
            times.append((time.perf_counter() - started) * 1000)
    return rnd(median(times), 0) if times else None


# ---------- Ultra: jeden przełącznik dla pracy agentów ----------

# kolejność ma znaczenie: najpierw to, co działa od razu
ULTRA = [
    "bg-helpers",
    "claude-hooks-async",
    "node-compile-cache",
    "devguard-budget",
    "docker-vm",
    "git-speed",
]
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


def pending_manual(state):
    """Rzeczy do kliknięcia przez człowieka: Ultra ich nie zrobi sama."""
    names = []
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
    for name in ULTRA:
        item = tweak(name)
        old = state["applied"].get(name)
        if old is not None and name not in ultra["applied"]:
            continue  # włączone ręcznie przed Ultra: nie nasze, nie ruszamy
        before = None
        if name == "bg-helpers" and old is None:
            before = item.measure(cfg, system)
        if name == "git-speed" and old is None and cfg.get("git_repos"):
            before = git_status_ms(
                system, [os.path.expanduser(r) for r in cfg["git_repos"]]
            )
        try:
            record, changed = item.apply(cfg, system, old)
        except (OSError, ValueError, RuntimeError) as err:
            report.append(f"{name}: błąd {err}")
            continue
        record["at"] = old["at"] if old else time.time()
        record["ultra"] = True
        if name == "docker-vm" and record.get("written"):
            # Docker wczyta nową wartość dopiero przy następnym starcie
            record.setdefault("active", bool((old or {}).get("active")))
        state["applied"][name] = record
        if name not in ultra["applied"]:
            ultra["applied"].append(name)
        for what in changed:
            report.append(f"{name}: {what}")
        result = None
        if name == "bg-helpers" and before is not None:
            # planista przenosi wątki na rdzenie E nie od razu
            time.sleep(SETTLE_SECONDS)
            after = item.measure(cfg, system)
            result = {"before": before, "after": after, "unit": "% rdzenia P"}
        elif name == "git-speed" and before is not None:
            after = git_status_ms(
                system, [os.path.expanduser(r) for r in cfg["git_repos"]]
            )
            result = {"before": before, "after": after, "unit": "ms git status"}
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
        elif name not in ultra["results"]:
            result = measure_before_after(item, cfg, system, record)
        if result:
            ultra["results"][name] = result
    ultra["pending_root"] = pending_root(cfg, state)
    ultra["pending_manual"] = pending_manual(state)
    return report


def ultra_off(cfg, system, state):
    """Cofa dokładnie to, co włączyła Ultra; ręcznie włączone poprawki zostają."""
    ultra = ultra_state(state)
    report = []
    for name in reversed(ultra["applied"]):
        record = state["applied"].get(name)
        if record is None:
            continue
        try:
            restored = tweak(name).undo(record, system)
        except Deferred:
            state.setdefault("deferred", {})[name] = record
            report.append(f"{name}: cofnę po zamknięciu Dockera")
        else:
            report += [f"{name}: {what}" for what in restored]
        del state["applied"][name]
    ultra.update(on=False, applied=[], pending_root=[], pending_manual=[])
    return report


def refresh_ultra(cfg, state, system):
    """Wyniki, które przychodzą później: hooki z nowych transkryptów, Docker po restarcie."""
    ultra = ultra_state(state)
    if not ultra["on"]:
        return
    hooks = ultra["results"].get("claude-hooks-async")
    if hooks and hooks.get("after") is None and ultra["since"]:
        # hooki puszczone w tle mogą dalej trafiać do transkryptu, ale sesja na nie nie czeka
        skip = [spec["match"] for spec in cfg.get("async_hooks", [])]
        stats = hook_latency(ultra["since"], skip=skip, born_after=ultra["since"])
        stats = stats.get("PostToolUse")
        if stats and stats["n"] >= HOOK_SAMPLES:
            hooks["after"] = stats["p50"]
            hooks.pop("note", None)
    docker = state["applied"].get("docker-vm")
    if docker and docker.get("written") and not docker.get("active"):
        total = system.docker_memory()
        if total and abs(total / 2**20 - docker["value"]) < 512:
            docker["active"] = True
    if docker and "docker-vm" in ultra["applied"]:
        # notatka idzie za stanem: czeka na zamknięcie, czeka na restart, działa
        result = measure_before_after(tweak("docker-vm"), cfg, system, docker)
        ultra["results"]["docker-vm"] = result
    ultra["pending_manual"] = pending_manual(state)


def cmd_ultra(cfg, args, system=None):
    system = system or System()
    action = args[0] if args else "status"
    state = load_state()
    if action == "on":
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


DESCRIBE = {
    "network": describe_network,
    "cpu": describe_cpu,
    "gpu": describe_gpu,
    "fs": describe_fs,
}
LABELS = {"network": "Sieć", "cpu": "CPU", "gpu": "GPU", "fs": "System plików"}


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
        out = {"tweaks": tweaks, "bench": state["bench"], "ultra": ultra_state(state)}
        print(json.dumps(out, ensure_ascii=False))
        return 0
    ultra = ultra_state(state)
    print(f"Ultra: {'włączona' if ultra['on'] else 'wyłączona'} (perf.py ultra status)")
    print("Poprawki:")
    for entry in tweaks:
        mark = "x" if entry["applied"] else " "
        kind = "root" if entry["root"] else entry["group"]
        print(f"  [{mark}] {entry['name']} [{kind}]: {entry['title']}")
        if entry.get("detail"):
            print(f"        {entry['detail']}")
    for name in state.get("deferred", {}):
        print(f"  ! {name}: cofnięcie czeka na zamknięcie Dockera")
    for kind in ("network", "cpu", "gpu", "fs"):
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
    kinds = [a for a in args if a in ("network", "cpu", "gpu", "fs", "all")] or ["all"]
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
        else:
            result = bench_gpu()
            result["latency"] = gpu_latency()
        # stan czytany tuż przed zapisem: pomiar trwa, a w tym czasie coś mogło go zmienić
        state = load_state()
        record_bench(state, kind, result, started, load)
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
        if item.name in ultra_state(state)["applied"]:
            ultra_state(state)["applied"].remove(item.name)
        log(f"undo {item.name}")
    save_state(state)
    return 0


def cmd_keep(cfg, args, system=None):
    """Pilnuje włączonych poprawek (dla launchd co kilka minut): procesy z listy wstają
    z nowym pid, ktoś nadpisał settings.json, zapis do Dockera czekał na jego zamknięcie;
    kończy też cofnięcia, które musiały poczekać."""
    system = system or System()
    state = load_state()
    for name, record in list(state.get("deferred", {}).items()):
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
            log(f"keep {item.name}: błąd {err!r}")
            continue
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
    if ultra_state(state)["on"]:
        refresh_ultra(cfg, state, system)
    save_state(state)
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


def cmd_record(cfg, args, system=None):
    """Dla perf-root.sh: zapis albo usunięcie poprawki roota w stanie (`record shaper
    <opis>` / `record shaper --forget`), żeby panel i status ją widziały."""
    name = args[0] if args else ""
    if tweak(name) is None or not tweak(name).root:
        return 2
    state = load_state()
    if "--forget" in args:
        state["applied"].pop(name, None)
    else:
        state["applied"][name] = {"at": time.time(), "detail": " ".join(args[1:])}
    save_state(state)
    log(f"record {' '.join(args)}")
    return 0


COMMANDS = {
    "status": cmd_status,
    "bench": cmd_bench,
    "apply": cmd_apply,
    "undo": cmd_undo,
    "keep": cmd_keep,
    "list": cmd_list,
    "ultra": cmd_ultra,
    "shaper-rate": cmd_shaper_rate,
    "record": cmd_record,
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
