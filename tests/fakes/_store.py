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


def issue(server, email):
    """Nowa para tokenów dla konta, tak jak po logowaniu albo odświeżeniu."""
    n = server["counter"] = server.get("counter", 0) + 1
    access, refresh = f"at-{email}-{n}", f"rt-{email}-{n}"
    server.setdefault("access", {})[access] = email
    server.setdefault("refresh", {})[refresh] = email
    return access, refresh
