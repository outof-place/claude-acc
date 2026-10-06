"""Świat atrap: zainstalowane i najnowsze wersje w $HOME/fake/world.json, wywołania w calls.log."""
import json
import os

ROOT = os.path.join(os.environ["HOME"], "fake")
WORLD = os.path.join(ROOT, "world.json")


def load():
    with open(WORLD) as f:
        return json.load(f)


def save(world):
    tmp = WORLD + ".tmp"
    with open(tmp, "w") as f:
        json.dump(world, f, indent=1)
    os.replace(tmp, WORLD)


def record(tool, args):
    with open(os.path.join(ROOT, "calls.log"), "a") as f:
        f.write(" ".join([tool, *args]) + "\n")


def fails(world, key):
    return key in world.get("fail", [])
