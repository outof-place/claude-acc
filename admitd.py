"""Ciepły `devguard.py admit`: jedno gniazdo $STATE/admit.sock zamiast startu Pythona na zdarzenie.

  devguard.py admitd     (acc.py devguard admitd) pętla dla launchd: agent Pod codes.pod.app.acc.admit

Skąd: claude-acc-hook oddaje Pythonowi co trzecią komendę Bash każdego agenta (bramka całych słów), a
sam start interpretera z importami to 25-40 ms, pod obciążeniem setki. Tu rodzic ładuje devguard i
sched raz. Każde zdarzenie dostaje własny fork(): dziecko bierze środowisko, katalog i stdin
hooka, woła to samo `devguard.main(argv)` co exec (`acc.py devguard admit`), oddaje stdout, stderr
i kod, i kończy się os._exit. Stan jednego zdarzenia nie przecieka do następnego, 25 agentów naraz
nie czeka jeden na drugiego, a wyjątek albo zawieszenie zabiera tylko dziecko.

Protokół v1 (claude-acc-hook i pod-hookd): u32 big-endian długość i JSON w obie strony;
  żądanie    {"v": 1, "argv": ["admit"] | ["admit", "--codex"], "event": surowe zdarzenie,
              "cwd": katalog hooka, "env": całe środowisko hooka}
  odpowiedź  {"v": 1, "stdout": ..., "stderr": ..., "code": ...}
Ramka do 8 MiB. Zła wersja, za duża ramka, obcy uid albo błąd: połączenie zamknięte bez odpowiedzi,
a klient robi exec jak dotąd, więc ciepła ścieżka nigdy nie zmienia odpowiedzi, najwyżej jej czas.
Gniazdo ma 0600, a klient musi mieć nasz uid (LOCAL_PEERCRED). admit niczego nie zapisuje, więc
klient, który nie doczekał się odpowiedzi i zrobił exec, nie robi niczego dwa razy.

Zmiana któregoś z plików, które rodzic załadował (devguard.py, sched.py i ich importy z $STATE):
rodzic nie odpowiada na to połączenie (klient robi exec) i uruchamia się od nowa, więc ciepły kod
jest zawsze tym z dysku. Agent tylko w Pod (POD_JOBS w setup.sh): agent z
~/Library/LaunchAgents to osobna tożsamość TCC i jego dzieci czekałyby na zgodę przy plikach w
~/Documents (zmierzone 2026-10-10 na pod-hookd).
"""

import fcntl
import io
import json
import os
import signal
import socket
import struct
import sys
import tempfile
import traceback

MAX_FRAME = 8 << 20
# sys/un.h: SOL_LOCAL, LOCAL_PEERCRED; struct xucred zaczyna się od cr_version i cr_uid (u_int)
SOL_LOCAL = 0
LOCAL_PEERCRED = 0x001
XUCRED_SIZE = 76
READ_TIMEOUT_S = 10
# zawieszone dziecko kończy SIGALRM; klient i tak robi exec po 3 s
CHILD_LIMIT_S = 30
ARGVS = (["admit"], ["admit", "--codex"])


def here():
    return os.path.dirname(os.path.realpath(__file__))


def load_devguard(state):
    """devguard.py jako moduł (nie __main__: jego `if __name__ == "__main__"` by ruszył), z sched.py
    załadowanym raz zamiast exec_module przy każdym zdarzeniu. devguard_core rodzic nie ładuje: jego
    importy czytają środowisko (FSGUARD_STATE, janitor.HOME i ENV), więc dziecko importuje go samo, ze
    środowiskiem hooka, tak jak exec."""
    from importlib.machinery import SourceFileLoader

    path = os.path.join(state, "devguard.py")
    module = type(sys)("acc_devguard")
    module.__file__ = path
    SourceFileLoader("acc_devguard", path).exec_module(module)
    cache_sched(module, state)
    return module


def cache_sched(devguard, state):
    """sched.py przy imporcie nie czyta plików, tylko HOME i SCHED_CONFIG ze środowiska: gotowy moduł
    odpowiada jak świeży, gdy hook ma te same wartości, a dla innych zostaje ścieżka devguard. Stan
    modułu z jednego wywołania (lru_cache) ginie razem z dzieckiem."""
    from importlib.machinery import SourceFileLoader

    path = os.path.join(state, "sched.py")
    if not os.path.exists(path):
        return
    module = type(sys)("acc_sched")
    module.__file__ = path
    SourceFileLoader("acc_sched", path).exec_module(module)
    key = (os.environ.get("HOME"), os.environ.get("SCHED_CONFIG"))
    fresh = devguard.sched_rewrite

    def sched_rewrite(event):
        if (os.environ.get("HOME"), os.environ.get("SCHED_CONFIG")) != key:
            return fresh(event)
        # jak w devguard: hook nigdy nie blokuje agenta przez własny błąd
        try:
            return module.hook_rewrite(event)
        except Exception:
            return None

    devguard.sched_rewrite = sched_rewrite


def watched(state):
    """Pliki z $STATE, z których rodzic ma kod: każdy załadowany moduł stamtąd i sched.py (ten
    devguard ładuje od nowa przy każdym wywołaniu, ale jego zmiana też ma zrestartować rodzica)."""
    root = os.path.realpath(state) + os.sep
    files = {os.path.join(state, "devguard.py"), os.path.join(state, "sched.py")}
    for module in list(sys.modules.values()):
        path = getattr(module, "__file__", None)
        if path and os.path.realpath(path).startswith(root):
            files.add(path)
    return sorted(files)


def stamps(files):
    out = []
    for path in files:
        try:
            st = os.stat(path)
            out.append((path, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns))
        except OSError:
            out.append((path, None))
    return out


def peer_uid(conn):
    raw = conn.getsockopt(SOL_LOCAL, LOCAL_PEERCRED, XUCRED_SIZE)
    return struct.unpack_from("=II", raw)[1]


def read_exact(conn, n):
    chunks = []
    while n:
        chunk = conn.recv(min(n, 1 << 20))
        if not chunk:
            return None
        chunks.append(chunk)
        n -= len(chunk)
    return b"".join(chunks)


def read_frame(conn):
    head = read_exact(conn, 4)
    if head is None:
        return None
    (n,) = struct.unpack(">I", head)
    return None if n > MAX_FRAME else read_exact(conn, n)


def write_frame(conn, obj):
    data = json.dumps(obj).encode()
    conn.sendall(struct.pack(">I", len(data)) + data)


def request(data):
    """Żądanie v1 albo None: wszystko, co nie jest dokładnie tym kształtem, idzie do exec."""
    try:
        req = json.loads(data)
    except ValueError:
        return None
    if not isinstance(req, dict) or req.get("v") != 1 or req.get("argv") not in ARGVS:
        return None
    env, event, cwd = req.get("env"), req.get("event"), req.get("cwd")
    if (
        not isinstance(event, str)
        or not isinstance(cwd, str)
        or not isinstance(env, dict)
    ):
        return None
    if not all(
        isinstance(k, str)
        and isinstance(v, str)
        and k
        and "=" not in k
        and "\0" not in v
        for k, v in env.items()
    ):
        return None
    return req


def run_event(conn, devguard, req):
    """Dziecko po fork(): proces hooka, tyle że bez startu Pythona."""
    os.environ.clear()
    os.environ.update(req["env"])
    if req["cwd"]:
        os.chdir(req["cwd"])
    # deskryptory 1 i 2 na pliki tymczasowe: print() i zapis wprost na fd łapią się tak samo jak w exec
    out, err = tempfile.TemporaryFile(), tempfile.TemporaryFile()
    sys.stdout.flush()
    sys.stderr.flush()
    os.dup2(out.fileno(), 1)
    os.dup2(err.fileno(), 2)
    sys.stdin = io.StringIO(req["event"])
    sys.argv = [os.path.join(here(), "devguard.py"), *req["argv"]]
    code = 0
    try:
        code = devguard.main(list(req["argv"])) or 0
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
    except BaseException:
        traceback.print_exc()
        code = 1
    sys.stdout.flush()
    sys.stderr.flush()
    texts = []
    for f in (out, err):
        f.seek(0)
        texts.append(f.read().decode("utf-8", "replace"))
    write_frame(conn, {"v": 1, "stdout": texts[0], "stderr": texts[1], "code": code})


def child(conn, devguard):
    try:
        signal.signal(signal.SIGCHLD, signal.SIG_DFL)
        signal.alarm(CHILD_LIMIT_S)
        conn.settimeout(READ_TIMEOUT_S)
        data = read_frame(conn)
        req = request(data) if data is not None else None
        if req is not None:
            run_event(conn, devguard, req)
    except BaseException:
        pass
    finally:
        os._exit(0)


def bind(path):
    """Gniazdo pod nazwą tymczasową, 0600, potem rename na miejsce: klient nie trafia na lukę."""
    tmp = f"{path}.{os.getpid()}"
    try:
        os.unlink(tmp)
    except FileNotFoundError:
        pass
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(tmp)
    os.chmod(tmp, 0o600)
    sock.listen(128)
    os.rename(tmp, path)
    return sock


def serve(state=None):
    state = state or here()
    path = os.path.join(state, "admit.sock")
    if len(path.encode()) >= 104:
        print(f"admitd: ścieżka gniazda za długa: {path}", file=sys.stderr)
        return 1
    # jeden serwer na katalog stanu: drugi czeka, aż pierwszy zniknie
    lock = open(os.path.join(state, "admit.lock"), "a")
    fcntl.flock(lock, fcntl.LOCK_EX)
    devguard = load_devguard(state)
    files = watched(state)
    seen = stamps(files)
    # dzieci sprząta jądro (SIGCHLD ignorowany: bez zombie)
    signal.signal(signal.SIGCHLD, signal.SIG_IGN)
    sock = bind(path)
    uid = os.getuid()
    while True:
        conn, _ = sock.accept()
        try:
            if peer_uid(conn) != uid:
                continue
            if stamps(files) != seen:
                # nowy kod na dysku: to połączenie robi exec, a rodzic startuje od nowa z tym kodem
                conn.close()
                sock.close()
                lock.close()
                # Python 3.9 (/usr/bin/python3) nie ma orig_argv: devguard.py admitd wprost
                argv = getattr(sys, "orig_argv", None) or [sys.executable, *sys.argv]
                os.execv(sys.executable, argv)
            sys.stdout.flush()
            sys.stderr.flush()
            if os.fork() == 0:
                sock.close()
                child(conn, devguard)
        except OSError:
            pass
        finally:
            conn.close()


if __name__ == "__main__":
    sys.exit(serve(sys.argv[1] if len(sys.argv) > 1 else None))
