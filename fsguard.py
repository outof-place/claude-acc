#!/usr/bin/python3
"""Strażnik fseventsd: systemowy demon zdarzeń plików nie zje pamięci Maca.

Skąd problem (2026-10-06, 48 GB RAM): fseventsd, który rozsyła zdarzenia plików do
wszystkich obserwatorów (Spotlight, git fsmonitor, dev serwery, tsserver, Orca), urósł
z normalnych 5-50 MB do 41,7 GB. Swap doszedł do 48 GB, system przestał odpowiadać i jądro
zrestartowało Maca (panika "watchdog timeout: no checkins from watchdogd"). Rósł przez dobę:
0,05 GB tuż po starcie, 7,2 GB siedem godzin przed awarią, potem około 5 GB na godzinę.
Mechanizmu w środku demona Apple nie widać, a strażnik pamięci użytkownika go nie widzi,
bo fseventsd działa jako root.

Co robi strażnik (launchd uruchamia go jako root co minutę):
- mierzy phys_footprint fseventsd, ten sam licznik, którym jetsam wybiera ofiary;
- gdy dwa kolejne odczyty są ponad limitem (domyślnie 4 GB), wysyła SIGTERM, a po 10 s
  SIGKILL; launchd wznawia demona od razu (KeepAlive). Obserwatory sprzed restartu głuchną
  (sprawdzone 6.10: fs.watch w node nie dostaje już nic), stąd wysoki limit i to, co niżej;
- potem zatrzymuje demony `git fsmonitor--daemon`: ich odpowiedź dla `git status` zależy od
  ciągłego strumienia zdarzeń, a następne polecenie git stawia demona od nowa z pełnym skanem;
- dev serwery postawione przed restartem restartuje devguard, gdy zobaczy last_restart
  w stanie strażnika; edytory i serwery języka (tsserver, gopls) trzeba zrestartować samemu;
- między restartami odczekuje co najmniej 5 minut i zapisuje w logu trend (co godzinę albo
  co 10 minut, gdy demon ma ponad 500 MB) oraz każdy inny proces ponad 8 GB, do diagnozy.

  fsguard.py [--dry-run] [--limit-mb N] [--state PLIK] [--log PLIK]
             [--target NAZWA] [--fsmonitor-match TEKST] [--respawn-wait S]
--target i --fsmonitor-match są dla testów: atrapa procesu zamiast prawdziwego demona.
"""

import os
import sys

# bajtkod tylko w $STATE: obok skryptu w paczce Poda (Pod.app/Contents/Resources/claude-acc) __pycache__
# łamie pieczęć aplikacji, czymkolwiek i z jakimikolwiek flagami ten plik uruchomić (1.31.6)
if not os.path.realpath(__file__).startswith(os.path.realpath(os.path.expanduser("~/.local/share/claude-acc")) + "/"):
    sys.dont_write_bytecode = True

import ctypes
import json
import signal
import subprocess
import time

LIMIT_MB = 4096  # 100 razy więcej niż zwykle, 10 razy mniej niż w noc awarii
MIN_GAP_S = 300  # najkrótszy odstęp między restartami
TREND_EVERY_S = 3600
TREND_BIG_EVERY_S = 600
TREND_BIG_MB = 512
OTHERS_WARN_MB = 8192
STATE = "/var/db/claude-acc-fsguard.json"
LOG = "/Library/Logs/claude-acc-fsguard.log"
FSMONITOR_MATCH = "git fsmonitor--daemon"

libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
libc.proc_listallpids.argtypes = [ctypes.c_void_p, ctypes.c_int]
libc.proc_pid_rusage.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
libc.proc_name.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]


class RUsageInfoV0(ctypes.Structure):  # struct rusage_info_v0, <sys/resource.h>
    _fields_ = [("ri_uuid", ctypes.c_uint8 * 16)] + [(n, ctypes.c_uint64) for n in (
        "ri_user_time", "ri_system_time", "ri_pkg_idle_wkups", "ri_interrupt_wkups", "ri_pageins",
        "ri_wired_size", "ri_resident_size", "ri_phys_footprint", "ri_proc_start_abstime",
        "ri_proc_exit_abstime")]


def all_pids():
    buf = (ctypes.c_int * 16384)()
    n = libc.proc_listallpids(buf, ctypes.sizeof(buf))
    return [p for p in buf[:max(n, 0)] if p > 0]


def name_of(pid):
    buf = ctypes.create_string_buffer(256)
    return buf.value.decode(errors="replace") if libc.proc_name(pid, buf, 256) > 0 else ""


def footprint(pid):
    """Bajty albo None (proces zniknął albo należy do innego użytkownika, a my nie jesteśmy rootem)."""
    ri = RUsageInfoV0()
    if libc.proc_pid_rusage(pid, 0, ctypes.byref(ri)) != 0:
        return None
    return ri.ri_phys_footprint


def find(name):
    return next((p for p in all_pids() if name_of(p) == name), None)


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def mb(n):
    return f"{n / 2**20:,.0f} MB".replace(",", " ")


class Guard:
    def __init__(self, args):
        self.dry = "--dry-run" in args
        self.limit = int(float(opt(args, "--limit-mb", LIMIT_MB)) * 2**20)
        self.state_path = opt(args, "--state", STATE)
        self.log_path = opt(args, "--log", LOG)
        self.target = opt(args, "--target", "fseventsd")
        self.fsmonitor = opt(args, "--fsmonitor-match", FSMONITOR_MATCH)
        self.respawn_wait = float(opt(args, "--respawn-wait", 15))
        self.state = load(self.state_path)

    def log(self, msg):
        with open(self.log_path, "a") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S  ") + msg + "\n")

    def run(self):
        now = time.time()
        pid = find(self.target)
        size = footprint(pid) if pid else None
        if size is None:
            self.log(f"{self.target}: brak procesu albo odczytu (pid {pid})")
            return
        self.trend(now, size)
        if size <= self.limit:
            self.state.pop("over_since", None)
        elif "over_since" not in self.state:
            self.state["over_since"] = now
            self.log(f"{self.target} {mb(size)} ponad limitem {mb(self.limit)}, restart przy następnym odczycie")
        elif now - self.state.get("last_restart", 0) < MIN_GAP_S:
            self.log(f"{self.target} {mb(size)} ponad limitem, ale poprzedni restart był przed chwilą")
        else:
            self.restart(pid, size, now)
        self.watch_others(now)
        save(self.state_path, self.state)

    def trend(self, now, size):
        every = TREND_BIG_EVERY_S if size > TREND_BIG_MB * 2**20 else TREND_EVERY_S
        if now - self.state.get("last_trend", 0) >= every:
            self.state["last_trend"] = now
            self.log(f"{self.target} {mb(size)}")

    def restart(self, pid, size, now):
        if self.dry:
            self.log(f"DRY-RUN zrestartowałbym {self.target} (pid {pid}, {mb(size)})")
            return
        self.log(f"RESTART {self.target} pid {pid} przy {mb(size)} (limit {mb(self.limit)})")
        stop(pid)
        self.state.pop("over_since", None)
        self.state["last_restart"] = now
        self.state["restarts"] = self.state.get("restarts", 0) + 1
        new = None
        deadline = time.time() + self.respawn_wait
        while time.time() < deadline and not new:
            time.sleep(0.5)
            new = find(self.target)
        self.log(f"   {self.target} wstał: pid {new}" if new else f"   {self.target} jeszcze nie wstał")
        stopped = self.stop_fsmonitors()
        if stopped:
            self.log(f"   zatrzymane demony git fsmonitor: {len(stopped)} (wstaną przy następnym git)")
        notify(f"fseventsd zjadł {mb(size)} pamięci, zrestartowany. Edytory i serwery języka mogą nie widzieć zmian plików do restartu.", self.log)

    def stop_fsmonitors(self):
        out = subprocess.run(["/bin/ps", "-axo", "pid=,args="], capture_output=True, text=True).stdout
        pids = [int(line.split(None, 1)[0]) for line in out.splitlines()
                if self.fsmonitor in line and line.split(None, 1)[0].isdigit()]
        pids = [p for p in pids if p != os.getpid()]  # nasze argumenty też zawierają wzorzec
        for pid in pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
        return pids

    def watch_others(self, now):
        """Tylko log: inny proces ponad 8 GB to trop na przyszłość, nie powód do zabijania."""
        seen = self.state.setdefault("big", {})
        for pid in all_pids():
            size = footprint(pid)
            if not size or size < OTHERS_WARN_MB * 2**20:
                continue
            key = f"{pid}:{name_of(pid)}"
            if now - seen.get(key, 0) >= TREND_EVERY_S:
                seen[key] = now
                self.log(f"uwaga: {name_of(pid)} (pid {pid}) {mb(size)}")
        for key in [k for k, t in seen.items() if now - t > 6 * TREND_EVERY_S]:
            del seen[key]


def stop(pid, grace=10):
    """SIGTERM, a po `grace` sekundach SIGKILL."""
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.time() + grace
    while time.time() < deadline:
        if not alive(pid):
            return
        time.sleep(0.2)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def notify(text, log=None):
    """Powiadomienie w sesji osoby przy konsoli; strażnik działa jako root poza nią."""
    try:
        uid = os.stat("/dev/console").st_uid
        if os.geteuid() != 0 or uid == 0:
            return
        # tekst idzie jako argument skryptu, nie w jego treści: cudzysłów albo \ w nim psuły skrypt
        out = subprocess.run(["/bin/launchctl", "asuser", str(uid), "/usr/bin/osascript",
                              "-e", "on run argv",
                              "-e", "display notification (item 1 of argv) with title \"Strażnik fseventsd\"",
                              "-e", "end run", "--", text],
                             capture_output=True, text=True, timeout=10)
        if out.returncode != 0 and log:
            log(f"   osascript nie pokazał powiadomienia ({out.returncode}): {out.stderr.strip()}")
    except (OSError, subprocess.SubprocessError):
        pass


def opt(args, flag, default):
    return args[args.index(flag) + 1] if flag in args and args.index(flag) + 1 < len(args) else default


def load(path):
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save(path, data):
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def interpreter_problem(paths=None):
    """Pierwsza ścieżka interpretera tego procesu, która nie należy do roota albo jest zapisywalna dla
    grupy lub świata (plik, prawdziwa ścieżka, biblioteka standardowa i ich katalogi nadrzędne), albo
    None. Ta sama reguła co rootpy.unsafe_reason przy instalacji: demon sprawdza ją jeszcze raz przy
    każdym starcie, bo interpreter mógł się zmienić od instalacji. `paths` podstawiają testy."""
    if paths is None:
        import sysconfig

        paths = [sys.executable, os.path.realpath(sys.executable), sysconfig.get_paths()["stdlib"]]
    for path in paths:
        step = os.path.abspath(path)
        while True:
            try:
                info = os.lstat(step)
            except OSError:
                return step
            if info.st_uid != 0 or info.st_mode & 0o022:
                return step
            parent = os.path.dirname(step)
            if parent == step:
                break
            step = parent
    return None


def main(args):
    if os.geteuid() == 0:
        bad = interpreter_problem()
        if bad:
            print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} odmawiam: {bad} nie należy do roota albo jest "
                  "zapisywalny dla innych; przeinstaluj strażnika (install-fsguard.sh)", file=sys.stderr)
            return 78
    guard = Guard(args)
    try:
        guard.run()
    except Exception as err:  # strażnik, który pada, nikogo nie chroni; launchd i tak uruchomi go za minutę
        guard.log(f"błąd: {err!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
