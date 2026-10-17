#!/usr/bin/env python3
"""Rejestr decyzji maszyny: jeden wiersz JSON na decyzję w $STATE/events.jsonl.

Piszą go hamulec (lastresort.py: `kill`, `runaway`; devguard_core.py: `stage`, `stop`) i scheduler
(sched.py: `admit`, `dequeue`, `pause`, `resume`, `wake`). Wiersz zaczyna się od zegarów tej samej
chwili: `ts` (ściana, time.time), `up` (sekundy od rozruchu bez snu, CLOCK_UPTIME_RAW), `boot`
(kern.boottime, sekunda rozruchu) i rodzaju `ev`, potem pola wołającego.

Zegar bez snu to `time.clock_gettime(time.CLOCK_UPTIME_RAW)` ("does not increment while the system
is asleep", man clock_gettime), a nie `time.monotonic()`: na /usr/bin/python3 3.9.6 monotonic liczy
od startu procesu (0,02 s przy starcie, gdy uptime mówi 93 085 s), a sched.py biegnie pod nim
(plock) i pod Pythonem z $STATE (hak) na wspólnym state.json. Dlatego też tylko biblioteka
standardowa i składnia 3.9.

Zapis idzie pod flock na events.lock (najwyżej LOCK_WAIT_S czekania), jednym write() z O_APPEND,
z zegarem odczytanym pod zamkiem, więc wiersze leżą w pliku w kolejności `up`. Rotacja przy
ROTATE_BYTES do events.jsonl.1 (poprzedni .1 znika) jest własna: janitor obcina devguard.log w
miejscu do ostatnich 1000 linii, a mole czyści każdy wpis pierwszego poziomu w ~/Library/Logs
(clean/user.sh:309), więc rejestr leży w $STATE i nie idzie przez janitor.log. Błąd zapisu nigdy nie
przerywa wołającego: hamulec, hak ani wrapper nie mogą paść przez pomiar.
"""

import os
import sys

# bajtkod tylko w $STATE: obok skryptu w paczce Poda (Pod.app/Contents/Resources/claude-acc) __pycache__
# łamie pieczęć aplikacji, czymkolwiek i z jakimikolwiek flagami ten plik uruchomić (1.31.6)
if not os.path.realpath(__file__).startswith(os.path.realpath(os.path.expanduser("~/.local/share/claude-acc")) + "/"):
    sys.dont_write_bytecode = True

import json
import time

# fcntl i ctypes ładują się w funkcjach: sched.py importuje ten plik dopiero przy pierwszym wierszu
# albo zegarze, ale hook (hook_rewrite) nie może płacić za nic, czego nie używa

STATE_DIR = os.path.join(os.path.expanduser("~"), ".local/share/claude-acc")
NAME = "events.jsonl"
LOCK_NAME = "events.lock"
EVENTS_PATH = os.path.join(STATE_DIR, NAME)
# history.jsonl schedulera rośnie o 16,1 MB w 12 dób, a rejestr ma podobne tempo: ponad 30 dób w
# bieżącym pliku i drugie tyle w .1
ROTATE_BYTES = 32 * 1024 * 1024
LOCK_WAIT_S = 1.0
_boot = []  # kern.boottime tego procesu: czytany raz


def boot():
    """Sekunda rozruchu (kern.boottime.tv_sec) albo None, gdy sysctl się nie uda. Ta sama w każdym
    procesie od rozruchu do rozruchu, więc mówi, czy dwa odczyty uptime da się porównać."""
    if not _boot:
        value = None
        try:
            import ctypes

            libc = ctypes.CDLL(None)
            tv = (ctypes.c_long * 2)()  # struct timeval: tv_sec (long), tv_usec (int) z wyrównaniem
            size = ctypes.c_size_t(ctypes.sizeof(tv))
            if libc.sysctlbyname(b"kern.boottime", tv, ctypes.byref(size), None, ctypes.c_size_t(0)) == 0:
                value = int(tv[0])
        except (OSError, AttributeError, ValueError):
            pass
        _boot.append(value)
    return _boot[0]


def uptime():
    """Sekundy od rozruchu bez snu (CLOCK_UPTIME_RAW): ten sam zegar w każdym procesie i interpreterze."""
    return time.clock_gettime(time.CLOCK_UPTIME_RAW)


def now():
    """Zegary tej chwili: {"ts": ściana, "up": uptime bez snu, "boot": sekunda rozruchu}."""
    return {"ts": time.time(), "up": uptime(), "boot": boot()}


def rotate(path=EVENTS_PATH):
    """Przy ROTATE_BYTES plik idzie do <plik>.1 (poprzedni .1 znika); True, gdy obrócił. Woła się
    pod zamkiem rejestru."""
    try:
        if os.stat(path).st_size >= ROTATE_BYTES:
            os.replace(path, path + ".1")
            return True
    except OSError:
        pass
    return False


def _lock(fd):
    """flock bez blokowania na dłużej niż LOCK_WAIT_S: zatrzymany (SIGSTOP) pisarz z zamkiem nie
    może zatrzymać hamulca."""
    import fcntl

    deadline = time.monotonic() + LOCK_WAIT_S  # tylko w tym procesie: tu monotonic wystarcza
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.002)


def append(state_dir, ev, /, **fields):
    """Wiersz {ts, up, boot, ev, **fields} do <state_dir>/events.jsonl; True, gdy zapisany. Pola
    wołającego nie nadpisują zegarów ani rodzaju. Nigdy nie rzuca."""
    try:
        os.makedirs(state_dir, exist_ok=True)
        path = os.path.join(state_dir, NAME)
        lock = os.open(os.path.join(state_dir, LOCK_NAME), os.O_RDWR | os.O_CREAT, 0o644)
        try:
            if not _lock(lock):
                return False
            row = dict(now(), ev=ev)
            row.update((k, v) for k, v in fields.items() if k not in row)
            data = (json.dumps(row, ensure_ascii=False, default=str) + "\n").encode()
            rotate(path)
            fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
            try:
                os.write(fd, data)
            finally:
                os.close(fd)
        finally:
            os.close(lock)  # zamknięcie zwalnia flock
        return True
    except Exception:  # rejestr to pomiar: nigdy nie przerywa hamulca, haka ani wrappera
        return False

