#!/usr/bin/python3
"""Tryb hotspot: adaptacyjny limit wysyłania, gdy Mac wychodzi w świat przez iPhone'a.

Skąd pomysł (2026-10-08, iPhone 16 Pro Max po USB, T-Mobile 5G): łącze ma ~280 Mb/s w dół
i ~40 Mb/s w górę, ale przy wysyłaniu modem trzyma w kolejce nawet 0,8 s danych
(networkQuality: RPM 74 przy uploadzie). Każda tura Claude Code wysyła cały kontekst,
1-2 MB, a kilka sesji naraz zapycha tę kolejkę: ping, nowe połączenia i strumienie innych
sesji czekają za cudzym uploadem.

Co robi demon (root, launchd), tylko gdy domyślna trasa idzie przez hotspot iPhone'a:
- `ifconfig <if> tbr` ogranicza wysyłanie tuż poniżej bieżącej przepustowości uplinku,
  więc kolejka powstaje na Macu, gdzie FQ-CoDel z XNU puszcza małe przepływy (nowe żądania
  do API, ACK, DNS) przed wielkimi uploadami, zamiast w modemie, gdzie wszystko stoi w FIFO;
- przepustowość 5G pływa z minuty na minutę, więc limit idzie za sondami RTT (co 50 ms,
  na zmianę do sześciu reflektorów), jak cake-autorate na OpenWrt: rośnie, gdy łącze jest
  obciążone i czyste, spada o 10%, gdy stoi kolejka, a nasz upload jest tego przyczyną;
- kolejka to co najmniej 200 ms opóźnienia na każdej sondzie: pojedyncze skoki radia
  (HARQ, harmonogram) nie tną limitu, bo prototyp, który je liczył, sam się zdusił do 3 Mb/s;
- sondy trzymają przy okazji radio w stanie połączonym, więc pierwsze żądanie po przerwie
  nie czeka na wybudzenie modemu;
- gdy przez 5 s nie wraca żadna sonda (ICMP zablokowany, łącze padło), limit znika, żeby
  ślepy sterownik nie dusił wysyłania.

Zmierzone (A/B, 3 x 40 s bez i z demonem, prawdziwy ruch innych sesji w tle): ping p90
95 -> 53 ms, p99 367 -> 274 ms, p90 małego żądania do api.anthropic.com 608 -> 550 ms,
a upload 2 MB nie zwolnił (p50 1,01 -> 0,90 s).

  hotspot.py on|off                  włącza/wyłącza (plik hotspot.json, czyta go demon)
  hotspot.py status [--json]
  hotspot.py install|uninstall       demon roota (sudo, Touch ID)
  hotspot.py daemon --config F --state F    tak uruchamia go launchd
  hotspot.py run --iface en8 [--seconds N] [-v]   ręczny przebieg w terminalu (sudo)
"""

import argparse
import collections
import ctypes
import ctypes.util
import hashlib
import json
import os
import select
import signal
import socket
import struct
import subprocess
import sys
import time

HOME = os.path.expanduser("~")
STATE_DIR = os.path.join(HOME, ".local", "share", "claude-acc")
CONFIG = os.path.join(STATE_DIR, "hotspot.json")
STATE = os.path.join(STATE_DIR, "hotspot-state.json")
BIN = "/usr/local/libexec/claude-acc-hotspot"
LABEL = "com.filip.claude-acc.hotspot"
PLIST = "/Library/LaunchDaemons/%s.plist" % LABEL
LOG = "/var/log/claude-acc-hotspot.log"

IPHONE_GATEWAY = "172.20.10.1"  # Personal Hotspot zawsze daje 172.20.10.0/28, też po Wi-Fi
IPHONE_PORT = "iPhone USB"

REFLECTORS = ("1.1.1.1", "1.0.0.1", "8.8.8.8", "8.8.4.4", "9.9.9.9", "149.112.112.112")
PROBE_EVERY = 0.05        # s; reflektory na zmianę, każdy dostaje sondę co 0,3 s
PROBE_TIMEOUT = 1.0       # s; sonda bez odpowiedzi liczy się jako zgubiona
BLIND_S = 5.0             # tyle bez żadnej odpowiedzi i limit znika
IP_BOUND_IF = 25          # <netinet/in.h>: sondy idą przez kształtowany interfejs
AF_LINK = 18

MIN_KBPS = 6000
MAX_KBPS = 150000
START_KBPS = 30000
REMEMBER_S = 1800         # bezpieczny limit z ostatniej sesji wraca, jeśli jest świeży
DELAY_THR_MS = 30.0       # RTT ponad bazę reflektora, które liczy się jako kolejka
BLOAT_WINDOW = 4          # kolejka podnosi każdą sondę przez 200 ms, skok radia tylko jedną
LOAD_HIGH = 0.75          # wysyłanie / limit
LOAD_BLOAT = 0.4          # kolejka jest nasza dopiero, gdy wysyłamy co najmniej tyle
DOWN_FACTOR = 0.9
DOWN_REFRACTORY = 1.0     # s między cięciami: kolejka musi spłynąć, zanim cięcie coś pokaże
UP_FACTOR = 1.015         # na odpowiedź sondy (~20/s), gdy obciążone i czyste: ~35%/s
SAFE_ALPHA = 0.05         # bezpieczny limit idzie za czystymi limitami pod obciążeniem
IDLE_ALPHA = 0.05         # bez obciążenia limit wraca do bezpiecznego
APPLY_MIN_CHANGE = 0.03
TICK_S = 2.0              # co tyle demon czyta konfigurację, trasę i zapisuje stan
SUMMARY_EVERY_S = 300


# --- liczniki interfejsu i sondy ---------------------------------------------------------------

class _Sockaddr(ctypes.Structure):
    _fields_ = [("sa_len", ctypes.c_uint8), ("sa_family", ctypes.c_uint8)]


class _Ifaddrs(ctypes.Structure):
    pass


_Ifaddrs._fields_ = [
    ("ifa_next", ctypes.POINTER(_Ifaddrs)), ("ifa_name", ctypes.c_char_p), ("ifa_flags", ctypes.c_uint),
    ("ifa_addr", ctypes.POINTER(_Sockaddr)), ("ifa_netmask", ctypes.c_void_p),
    ("ifa_dstaddr", ctypes.c_void_p), ("ifa_data", ctypes.c_void_p)]
_libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)


def if_bytes(name):
    """(odebrane, wysłane) bajty z struct if_data; liczniki 32-bitowe, różnice modulo 2**32."""
    head = ctypes.POINTER(_Ifaddrs)()
    if _libc.getifaddrs(ctypes.byref(head)) != 0:
        return None
    try:
        p, want = head, name.encode()
        while p:
            ifa = p.contents
            if ifa.ifa_name == want and ifa.ifa_addr and ifa.ifa_addr.contents.sa_family == AF_LINK and ifa.ifa_data:
                # if_data: 8 pól u_char, potem u_int32: mtu, metric, baudrate, ipackets, ierrors,
                # opackets, oerrors, collisions, ibytes (offset 40), obytes (44)
                return struct.unpack_from("II", ctypes.string_at(ifa.ifa_data + 40, 8))
            p = ifa.ifa_next
    finally:
        _libc.freeifaddrs(head)
    return None


def _checksum(data):
    if len(data) % 2:
        data += b"\0"
    s = sum(struct.unpack("!%dH" % (len(data) // 2), data))
    s = (s >> 16) + (s & 0xffff)
    s += s >> 16
    return ~s & 0xffff


class Prober:
    """Echo ICMP z gniazda SOCK_DGRAM: bez raw socketu, przypięte do interfejsu."""

    def __init__(self, iface):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_ICMP)
        self.sock.setsockopt(socket.IPPROTO_IP, IP_BOUND_IF, socket.if_nametoindex(iface))
        self.sock.setblocking(False)
        self.ident = os.getpid() & 0xffff
        self.seq = 0
        self.sent = {}
        self.lost = 0
        self.answered = 0

    def close(self):
        self.sock.close()

    def send(self, dst):
        self.seq = (self.seq + 1) & 0xffff
        body = struct.pack("!d", time.time())
        hdr = struct.pack("!BBHHH", 8, 0, 0, self.ident, self.seq)
        pkt = struct.pack("!BBHHH", 8, 0, _checksum(hdr + body), self.ident, self.seq) + body
        self.sent[self.seq] = (dst, time.monotonic())
        try:
            self.sock.sendto(pkt, (dst, 0))
        except OSError:
            pass

    def replies(self):
        out = []
        while True:
            try:
                data, addr = self.sock.recvfrom(2048)
            except (BlockingIOError, InterruptedError):
                break
            now = time.monotonic()
            # macOS oddaje odpowiedź razem z nagłówkiem IP
            off = (data[0] & 0x0f) * 4 if data and data[0] >> 4 == 4 else 0
            if len(data) < off + 8:
                continue
            typ, _, _, ident, seq = struct.unpack_from("!BBHHH", data, off)
            if typ != 0 or ident != self.ident:
                continue
            ent = self.sent.pop(seq, None)
            if ent and ent[0] == addr[0]:
                self.answered += 1
                out.append((ent[0], (now - ent[1]) * 1000.0))
        cutoff = time.monotonic() - PROBE_TIMEOUT
        for seq in [s for s, (_, t) in self.sent.items() if t < cutoff]:
            del self.sent[seq]
            self.lost += 1
        return out


def set_tbr(iface, kbps):
    arg = "%dKbps" % int(kbps) if kbps else "0"
    subprocess.run(["/sbin/ifconfig", iface, "tbr", arg], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# --- sterownik ---------------------------------------------------------------------------------

class Controller:
    """Limit wysyłania w stylu cake-autorate, tylko z RTT (bez OWD) i tylko dla uplinku."""

    def __init__(self, iface, start_kbps=START_KBPS, min_kbps=MIN_KBPS, max_kbps=MAX_KBPS):
        self.iface = iface
        self.min = float(min_kbps)
        self.max = float(max_kbps)
        self.rate = min(self.max, max(self.min, float(start_kbps)))
        self.safe = self.rate
        self.applied = 0.0
        self.base = {}
        self.window = collections.deque(maxlen=BLOAT_WINDOW)
        self.samples = collections.deque()
        self.last_cut = float("-inf")
        self.cuts = 0
        self.tx_kbps = 0.0
        self.rx_kbps = 0.0
        self.deltas = collections.deque(maxlen=600)  # ostatnie 30 s

    def sample(self, now):
        """Przepływność z ostatnich ~250 ms; None, dopóki okno jest za krótkie."""
        counters = if_bytes(self.iface)
        if counters is None:
            return None
        self.samples.append((now, counters[1], counters[0]))
        while len(self.samples) > 2 and now - self.samples[0][0] > 0.25:
            self.samples.popleft()
        (t0, o0, i0), (t1, o1, i1) = self.samples[0], self.samples[-1]
        if t1 - t0 < 0.1:
            return None
        self.tx_kbps = ((o1 - o0) % 2 ** 32) * 8 / 1000.0 / (t1 - t0)
        self.rx_kbps = ((i1 - i0) % 2 ** 32) * 8 / 1000.0 / (t1 - t0)
        return self.tx_kbps

    def on_rtt(self, reflector, rtt, now):
        base = self.base.get(reflector)
        if base is None:
            self.base[reflector] = base = rtt
        delta = rtt - base
        # baza szybko w dół, powoli w górę, i nigdy nie goni kolejki, którą ma wykrywać
        if delta < 0:
            self.base[reflector] = base + 0.5 * delta
        elif delta < DELAY_THR_MS:
            self.base[reflector] = base + 0.002 * delta
        self.window.append(delta)
        self.deltas.append(delta)
        tx = self.sample(now)
        if tx is None:
            return
        load = tx / self.rate
        bloat = len(self.window) == BLOAT_WINDOW and min(self.window) > DELAY_THR_MS
        if bloat and load >= LOAD_BLOAT:
            if now - self.last_cut >= DOWN_REFRACTORY:
                self.rate = max(self.rate * DOWN_FACTOR, self.min)
                self.safe = min(self.safe, self.rate)
                self.last_cut = now
                self.cuts += 1
        elif not bloat and load >= LOAD_HIGH:
            self.rate = min(self.max, self.rate * UP_FACTOR)
            self.safe += SAFE_ALPHA * (self.rate - self.safe)
        elif load < LOAD_BLOAT:
            # kolejka bez naszego uploadu to pobieranie albo radio: limit wysyłania nic tu nie da
            self.rate += IDLE_ALPHA * (self.safe - self.rate)
        self.apply()

    def apply(self, force=False):
        if force or not self.applied or abs(self.rate - self.applied) / self.applied > APPLY_MIN_CHANGE:
            set_tbr(self.iface, self.rate)
            self.applied = self.rate

    def release(self):
        """Zdejmuje limit; następne apply() postawi go od nowa."""
        set_tbr(self.iface, 0)
        self.applied = 0.0

    def snapshot(self):
        d = sorted(self.deltas)

        def q(p):
            return round(d[min(len(d) - 1, int(p * len(d)))], 1) if d else None

        return {"iface": self.iface, "rate_kbps": int(self.rate), "safe_kbps": int(self.safe), "cuts": self.cuts,
                "tx_kbps": int(self.tx_kbps), "rx_kbps": int(self.rx_kbps), "delay_p50_ms": q(.5),
                "delay_p90_ms": q(.9), "shaping": bool(self.applied),
                "baseline_ms": round(min(self.base.values()), 1) if self.base else None}


class Session:
    """Sterownik z sondami na jednym interfejsie; `step` kręci pętlę do terminu."""

    def __init__(self, iface, start_kbps=START_KBPS, min_kbps=MIN_KBPS, max_kbps=MAX_KBPS):
        self.ctl = Controller(iface, start_kbps, min_kbps, max_kbps)
        self.prober = Prober(iface)
        self.next_probe = time.monotonic()
        self.last_reply = time.monotonic()
        self.i = 0
        self.started = time.time()
        self.ctl.apply(force=True)

    def step(self, until, stop=None):
        while time.monotonic() < until and not (stop and stop()):
            now = time.monotonic()
            if now >= self.next_probe:
                self.prober.send(REFLECTORS[self.i % len(REFLECTORS)])
                self.i += 1
                self.next_probe = max(self.next_probe + PROBE_EVERY, now)
            wait = max(0.0, min(self.next_probe, until) - time.monotonic())
            try:
                select.select([self.prober.sock], [], [], wait)
            except InterruptedError:
                pass
            now = time.monotonic()
            replies = self.prober.replies()
            for reflector, rtt in replies:
                self.ctl.on_rtt(reflector, rtt, now)
            if replies:
                self.last_reply = now
            elif now - self.last_reply > BLIND_S and self.ctl.applied:
                self.ctl.release()

    def close(self):
        self.ctl.release()
        self.prober.close()

    def snapshot(self):
        snap = self.ctl.snapshot()
        snap.update(lost=self.prober.lost, answered=self.prober.answered, since=int(self.started))
        return snap


# --- wykrywanie hotspotu, konfiguracja, stan -----------------------------------------------------

def _run(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def default_route(run=_run):
    """(interfejs, brama) domyślnej trasy IPv4 albo (None, None)."""
    iface = gateway = None
    for line in run(["/sbin/route", "-n", "get", "default"]).splitlines():
        key, _, value = line.strip().partition(":")
        if key == "interface":
            iface = value.strip()
        elif key == "gateway":
            gateway = value.strip()
    return iface, gateway


def iphone_ports(run=_run):
    """Urządzenia z portem sprzętowym "iPhone USB" (en8 i podobne)."""
    devices, port = set(), None
    for line in run(["/usr/sbin/networksetup", "-listallhardwareports"]).splitlines():
        if line.startswith("Hardware Port:"):
            port = line.split(":", 1)[1].strip()
        elif line.startswith("Device:") and port == IPHONE_PORT:
            devices.add(line.split(":", 1)[1].strip())
    return devices


def detect(run=_run, ports=None):
    """(interfejs, "USB"/"Wi-Fi") hotspotu iPhone'a na domyślnej trasie albo None."""
    iface, gateway = default_route(run)
    if not iface:
        return None
    if iface in (ports if ports is not None else iphone_ports(run)):
        return iface, "USB"
    if gateway == IPHONE_GATEWAY:
        return iface, "Wi-Fi"
    return None


def _clamp_mbps(value, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(min(high, max(low, value)) * 1000)


def read_config(path=CONFIG):
    """Konfiguracja z katalogu użytkownika; root czyta ją bez podążania za dowiązaniem."""
    cfg = {"enabled": False, "min_kbps": MIN_KBPS, "max_kbps": MAX_KBPS}
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as f:
            raw = json.loads(f.read(4096) or b"{}")
    except (OSError, ValueError):
        return cfg
    if not isinstance(raw, dict):
        return cfg
    cfg["enabled"] = raw.get("enabled") is True
    cfg["min_kbps"] = _clamp_mbps(raw.get("min_mbps"), 1, 1000) or MIN_KBPS
    cfg["max_kbps"] = _clamp_mbps(raw.get("max_mbps"), 5, 2000) or MAX_KBPS
    cfg["min_kbps"] = min(cfg["min_kbps"], cfg["max_kbps"])
    return cfg


def write_json(path, data):
    """Zapis atomowy; plik tymczasowy nie może być dowiązaniem, bo pisze root."""
    tmp = path + ".tmp"
    try:
        os.unlink(tmp)
    except FileNotFoundError:
        pass
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=1, sort_keys=True)
    try:
        st = os.stat(os.path.dirname(path))
        os.chown(tmp, st.st_uid, st.st_gid)
    except OSError:
        pass
    os.replace(tmp, path)


def read_state(path=STATE):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def log(line):
    print("%s %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), line), file=sys.stderr, flush=True)


def daemon(config_path, state_path):
    stop = []
    signal.signal(signal.SIGTERM, lambda *_: stop.append(1))
    signal.signal(signal.SIGINT, lambda *_: stop.append(1))
    session = None
    via = None
    remembered = {}  # interfejs -> (bezpieczny limit, kiedy)
    ports, ports_at = set(), float("-inf")
    last_summary = time.monotonic()
    log("start")
    try:
        while not stop:
            cfg = read_config(config_path)
            if time.monotonic() - ports_at > 60:
                ports, ports_at = iphone_ports(), time.monotonic()
            target = detect(ports=ports) if cfg["enabled"] else None
            if session and (not target or target[0] != session.ctl.iface or if_bytes(session.ctl.iface) is None):
                snap = session.snapshot()
                remembered[snap["iface"]] = (snap["safe_kbps"], time.time())
                log("koniec na %s: limit %.1f Mb/s, cięć %d" % (
                    snap["iface"], snap["rate_kbps"] / 1000, snap["cuts"]))
                session.close()
                session = None
            if target and not session:
                safe, at = remembered.get(target[0], (START_KBPS, 0))
                start = safe if time.time() - at < REMEMBER_S else START_KBPS
                try:
                    session = Session(target[0], start, cfg["min_kbps"], cfg["max_kbps"])
                    via = target[1]
                    log("hotspot na %s (%s), start %.1f Mb/s" % (target[0], via, start / 1000))
                except OSError as e:
                    log("nie mogę sondować %s: %s" % (target[0], e))
            if session:
                session.ctl.min, session.ctl.max = float(cfg["min_kbps"]), float(cfg["max_kbps"])
                session.step(time.monotonic() + TICK_S, stop=lambda: bool(stop))
            else:
                deadline = time.monotonic() + TICK_S
                while not stop and time.monotonic() < deadline:
                    time.sleep(0.2)
            state = {"at": time.time(), "enabled": cfg["enabled"], "active": bool(session), "pid": os.getpid()}
            if session:
                state.update(session.snapshot(), via=via)
                if time.monotonic() - last_summary >= SUMMARY_EVERY_S:
                    last_summary = time.monotonic()
                    log("limit %(rate_kbps)d kb/s, bezpieczny %(safe_kbps)d, cięć %(cuts)d, opóźnienie p50 "
                        "%(delay_p50_ms)s p90 %(delay_p90_ms)s ms, zgubione sondy %(lost)d" % state)
            try:
                write_json(state_path, state)
            except OSError as e:
                log("stan: %s" % e)
    finally:
        if session:
            session.close()
        log("stop")


def run_foreground(iface, seconds=None, verbose=False):
    session = Session(iface)
    stop = []
    signal.signal(signal.SIGTERM, lambda *_: stop.append(1))
    signal.signal(signal.SIGINT, lambda *_: stop.append(1))
    end = time.monotonic() + seconds if seconds else float("inf")
    try:
        while not stop and time.monotonic() < end:
            session.step(min(end, time.monotonic() + 1), stop=lambda: bool(stop))
            if verbose:
                s = session.snapshot()
                print("%s limit %5.1f bezp. %5.1f Mb/s  tx %5.1f rx %6.1f  cięć %d  opóźnienie p50 %s p90 %s  "
                      "zgubione %d" % (time.strftime("%H:%M:%S"), s["rate_kbps"] / 1000, s["safe_kbps"] / 1000,
                                       s["tx_kbps"] / 1000, s["rx_kbps"] / 1000, s["cuts"], s["delay_p50_ms"],
                                       s["delay_p90_ms"], s["lost"]), flush=True)
    finally:
        session.close()
    return session


# --- polecenia użytkownika -------------------------------------------------------------------------

def file_hash(path):
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()[:12]
    except OSError:
        return None


PLIST_BODY = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{label}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{python}</string>
    <string>-I</string>
    <string>{bin}</string>
    <string>daemon</string>
    <string>--config</string>
    <string>{config}</string>
    <string>--state</string>
    <string>{state}</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>ProcessType</key><string>Interactive</string>
  <key>StandardErrorPath</key><string>{log}</string>
</dict>
</plist>
"""


def installed():
    return os.path.exists(BIN) and os.path.exists(PLIST)


def launched_with():
    """Program z plisty demona: [interpreter, -I, BIN, ...] albo starsze [BIN, ...]; [] bez plisty."""
    try:
        import plistlib

        with open(PLIST, "rb") as f:
            args = plistlib.load(f).get("ProgramArguments")
    except (OSError, ValueError, ImportError):
        return []
    return [str(a) for a in args] if isinstance(args, list) else []


def root_python():
    """(interpreter, None) dla demona albo (None, powód): rootpy.py leży obok tego pliku."""
    sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
    try:
        import rootpy
    except ImportError as err:
        return None, f"brak rootpy.py obok hotspot.py ({err})"
    return rootpy.find()


def cmd_install(args):
    # demon chodzi jako root, więc nie wolno mu startować przez /usr/bin/python3: to zaślepka, która
    # idzie do wybranego Xcode'a, a Xcode z DMG należy do użytkownika (rootpy.py)
    python, why = root_python()
    if not python:
        print("nie instaluję demona: " + (why or "?"), file=sys.stderr)
        return 1
    os.makedirs(STATE_DIR, exist_ok=True)
    plist = os.path.join(STATE_DIR, "hotspot.plist.tmp")
    with open(plist, "w") as f:
        f.write(PLIST_BODY.format(label=LABEL, python=python, bin=BIN, config=CONFIG, state=STATE, log=LOG))
    src = os.path.realpath(__file__)
    script = (
        "set -e; install -d -o root -g wheel -m 755 /usr/local/libexec; "
        "install -o root -g wheel -m 755 '%s' '%s'; install -o root -g wheel -m 644 '%s' '%s'; "
        "launchctl bootout system '%s' 2>/dev/null || true; launchctl bootstrap system '%s'"
        % (src, BIN, plist, PLIST, PLIST, PLIST))
    if args.dry_run:
        os.unlink(plist)
        print("sudo /bin/sh -c \"%s\"" % script)
        return 0
    rc = subprocess.call(["sudo", "/bin/sh", "-c", script])
    os.unlink(plist)
    if rc:
        print("instalacja nie wyszła (sudo: %d)" % rc, file=sys.stderr)
        return rc
    print("demon zainstalowany: %s" % BIN)
    if not read_config()["enabled"]:
        print("włącz: claude-acc hotspot on")
    return 0


def cmd_uninstall(args):
    script = "launchctl bootout system '%s' 2>/dev/null || true; rm -f '%s' '%s'" % (PLIST, PLIST, BIN)
    if args.dry_run:
        print("sudo /bin/sh -c \"%s\"" % script)
        return 0
    rc = subprocess.call(["sudo", "/bin/sh", "-c", script])
    if rc == 0:
        print("demon usunięty; limit wysyłania zdjęty")
    return rc


def set_enabled(on, path=CONFIG):
    cfg = {}
    try:
        with open(path) as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        pass
    if not isinstance(cfg, dict):
        cfg = {}
    cfg["enabled"] = on
    os.makedirs(os.path.dirname(path), exist_ok=True)
    write_json(path, cfg)


def cmd_on(args):
    set_enabled(True)
    if not installed():
        if sys.stdin.isatty() and not args.no_install:
            return cmd_install(args)
        print("włączone, ale demon nie jest zainstalowany: claude-acc hotspot install")
        return 1
    print("tryb hotspot włączony; zadziała, gdy domyślna trasa pójdzie przez iPhone'a")
    return 0


def cmd_off(args):
    set_enabled(False)
    print("tryb hotspot wyłączony; demon zdejmie limit w ciągu ~2 s")
    return 0


def status(config_path=CONFIG, state_path=STATE, now=None):
    now = time.time() if now is None else now
    cfg = read_config(config_path)
    state = read_state(state_path) or {}
    running = bool(state) and now - state.get("at", 0) < 15
    launch = launched_with()
    out = {"enabled": cfg["enabled"], "installed": installed(), "running": running,
           "active": running and bool(state.get("active")),
           "current": file_hash(os.path.realpath(__file__)) == file_hash(BIN) if installed() else None,
           # plista sprzed 1.31.1 startowała demona przez shebang `#!/usr/bin/python3`
           "legacy_launch": bool(launch) and os.path.basename(launch[0]) not in ("python3", "python")}
    if out["active"]:
        for key in ("iface", "via", "rate_kbps", "safe_kbps", "cuts", "tx_kbps", "rx_kbps", "delay_p50_ms",
                    "delay_p90_ms", "baseline_ms", "shaping", "lost", "answered", "since"):
            out[key] = state.get(key)
    return out


def cmd_status(args):
    s = status()
    if args.json:
        print(json.dumps(s, sort_keys=True))
        return 0
    print("tryb hotspot: %s" % ("włączony" if s["enabled"] else "wyłączony"))
    if not s["installed"]:
        print("demon: nie zainstalowany (claude-acc hotspot install)")
    elif not s["running"]:
        print("demon: nie odpowiada (sudo launchctl kickstart -k system/%s, log %s)" % (LABEL, LOG))
    elif s["current"] is False:
        print("demon: starsza wersja niż ta w claude-acc (claude-acc hotspot install)")
    if s["installed"] and s.get("legacy_launch"):
        print("demon: startuje przez /usr/bin/python3, czyli interpreter z Xcode'a użytkownika; "
              "przeinstaluj (claude-acc hotspot install)")
    if s["active"]:
        print("łącze: %s przez %s, limit wysyłania %.1f Mb/s (bezpieczny %.1f), cięć %d%s" % (
            s["iface"], s["via"], s["rate_kbps"] / 1000, s["safe_kbps"] / 1000, s["cuts"],
            "" if s["shaping"] else "; limit zdjęty, sondy nie wracają"))
        print("teraz: wysyłanie %.1f Mb/s, pobieranie %.1f Mb/s; RTT bazowe %s ms, ponad bazę p50 %s p90 %s ms" % (
            s["tx_kbps"] / 1000, s["rx_kbps"] / 1000, s["baseline_ms"], s["delay_p50_ms"], s["delay_p90_ms"]))
    elif s["running"] and s["enabled"]:
        print("czeka: domyślna trasa nie idzie przez hotspot iPhone'a")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="claude-acc hotspot", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("on")
    p.add_argument("--no-install", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    sub.add_parser("off")
    p = sub.add_parser("status")
    p.add_argument("--json", action="store_true")
    for name in ("install", "uninstall"):
        sub.add_parser(name).add_argument("--dry-run", action="store_true")
    p = sub.add_parser("daemon")
    p.add_argument("--config", default=CONFIG)
    p.add_argument("--state", default=STATE)
    p = sub.add_parser("run")
    p.add_argument("--iface", required=True)
    p.add_argument("--seconds", type=float)
    p.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    if args.cmd == "daemon":
        daemon(args.config, args.state)
        return 0
    if args.cmd == "run":
        run_foreground(args.iface, args.seconds, args.verbose)
        return 0
    handlers = {"on": cmd_on, "off": cmd_off, "status": cmd_status, "install": cmd_install,
                "uninstall": cmd_uninstall}
    if args.cmd not in handlers:
        ap.print_help()
        return 2
    return handlers[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
