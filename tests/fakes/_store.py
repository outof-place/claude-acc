"""Wspólny magazyn atrap: Pęk kluczy i serwer Anthropic jako pliki JSON w $HOME/fake."""
import json
import os

ROOT = os.path.join(os.environ["HOME"], "fake")


def load(name, default):
    path = os.path.join(ROOT, name)
    return json.load(open(path)) if os.path.exists(path) else default


def save(name, data):
    os.makedirs(ROOT, exist_ok=True)
    tmp = os.path.join(ROOT, name + ".tmp")
    json.dump(data, open(tmp, "w"))
    os.replace(tmp, os.path.join(ROOT, name))


def signal_point(what):
    """Punkt przerwania dla testów: każde wywołanie atrapy ma dwa (na starcie i tuż przed
    odpowiedzią), zapisane po kolei w signal-points. Punkt numer FAKE_SIGNAL_AT wysyła
    FAKE_SIGNAL (TERM, INT, HUP, KILL) procesowi, który atrapę wywołał."""
    at = os.environ.get("FAKE_SIGNAL_AT")
    if not at:
        return
    import fcntl
    import signal
    os.makedirs(ROOT, exist_ok=True)
    with open(os.path.join(ROOT, "signal-points"), "a+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.seek(0)
        n = len(f.read().splitlines()) + 1
        f.write(what + "\n")
    if n == int(at):
        sig = getattr(signal, "SIG" + os.environ.get("FAKE_SIGNAL", "TERM"))
        if os.environ.get("FAKE_SIGNAL_GROUP"):
            # jak Ctrl-C albo zamknięty terminal: cała grupa procesów ticku (test daje mu własną)
            os.killpg(os.getpgid(os.getppid()), sig)
        else:
            os.kill(os.getppid(), sig)


def issue(server, email):
    """Nowa para tokenów dla konta, tak jak po logowaniu albo odświeżeniu."""
    n = server["counter"] = server.get("counter", 0) + 1
    access, refresh = f"at-{email}-{n}", f"rt-{email}-{n}"
    server.setdefault("access", {})[access] = email
    server.setdefault("refresh", {})[refresh] = email
    return access, refresh
